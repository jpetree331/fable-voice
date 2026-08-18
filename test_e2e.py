r"""End-to-end machine test: a real WebRTC client talks to the running
voice_bot (port 8011) exactly like the PTT page does.

Flow: connect (brain spawns) -> ptt-close announced -> wait for warmup ->
ptt-open -> stream a spoken question (rendered wav) -> ptt-close -> record
Fable's audio reply -> STT the recording -> assert real speech came back.

Optionally (--barge-in N): after the reply starts, press the paddle N times
across turns to prove interruption never orphans audio or races a cancel.

Run (server must be up):  .venv\Scripts\python test_e2e.py [--barge-in 10]
"""
from __future__ import annotations

import torch  # noqa: F401  — torch first (cuDNN for ctranslate2 STT check)

import argparse
import asyncio
import fractions
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from av import AudioFrame
import httpx

ROOT = Path(__file__).resolve().parent
QUESTION_WAV = ROOT / "out" / "e2e_question.wav"
REPLY_WAV = ROOT / "out" / "e2e_reply.wav"

SAMPLE_RATE = 48000
FRAME_MS = 20
SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


class GatedFileTrack(AudioStreamTrack):
    """Silence until armed; then streams a wav (mono, resampled to 48k);
    silence again after. Mirrors a human holding the PTT circle."""

    def __init__(self, wav_path: Path):
        super().__init__()
        data, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != SAMPLE_RATE:
            x_old = np.linspace(0, 1, len(data))
            x_new = np.linspace(0, 1, int(len(data) * SAMPLE_RATE / sr))
            data = np.interp(x_new, x_old, data).astype(np.float32)
        peak = float(np.abs(data).max() or 1.0)
        data = data * (0.85 / peak)  # loud enough for VAD min_volume 0.35
        self._pcm = (data * 32767).astype(np.int16)
        self._pos = 0
        self._armed = asyncio.Event()
        self._pts = 0
        self.finished = asyncio.Event()

    def arm(self):
        self._armed.set()

    @property
    def duration_s(self) -> float:
        return len(self._pcm) / SAMPLE_RATE

    async def recv(self) -> AudioFrame:
        # pace to real time
        await asyncio.sleep(FRAME_MS / 1000)
        if self._armed.is_set() and self._pos < len(self._pcm):
            chunk = self._pcm[self._pos:self._pos + SAMPLES]
            self._pos += SAMPLES
            if self._pos >= len(self._pcm):
                self.finished.set()
            if len(chunk) < SAMPLES:
                chunk = np.pad(chunk, (0, SAMPLES - len(chunk)))
        else:
            chunk = np.zeros(SAMPLES, dtype=np.int16)
        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        frame.planes[0].update(chunk.tobytes())
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._pts += SAMPLES
        return frame


class ReplyRecorder:
    """Collects inbound audio frames to a mono float32 buffer."""

    def __init__(self):
        self.chunks: list[np.ndarray] = []
        self.task: asyncio.Task | None = None

    def start(self, track):
        self.task = asyncio.create_task(self._pull(track))

    async def _pull(self, track):
        try:
            while True:
                frame = await track.recv()
                arr = frame.to_ndarray()  # packed s16: (1, samples*channels)
                ch = len(frame.layout.channels)
                mono = (arr.reshape(-1, ch).astype(np.float32).mean(axis=1)
                        / 32768.0)
                self.chunks.append((mono, frame.sample_rate))
        except Exception:
            pass

    def save(self, path: Path) -> float:
        if not self.chunks:
            return 0.0
        sr = self.chunks[0][1]
        data = np.concatenate([c for c, _ in self.chunks])
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), data, sr, subtype="PCM_16")
        return len(data) / sr

    def speech_seconds(self, thresh: float = 0.01) -> float:
        """Rough voiced-audio measure (RMS over 100ms windows)."""
        if not self.chunks:
            return 0.0
        sr = self.chunks[0][1]
        data = np.concatenate([c for c, _ in self.chunks])
        w = sr // 10
        voiced = sum(
            1 for i in range(0, len(data) - w, w)
            if np.sqrt((data[i:i + w] ** 2).mean()) > thresh)
        return voiced * 0.1


def render_question(text: str) -> None:
    """Speak the test question in Chatterbox's default voice (NOT Fable's
    reference — the 'user' should not sound like the bot). CPU on purpose:
    the running server owns the GPU (22.1/24.5 GB) and a second Chatterbox
    instance would OOM the board."""
    import torchaudio
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    model = ChatterboxTurboTTS.from_pretrained(device="cpu")
    wav = model.generate(text)
    QUESTION_WAV.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(QUESTION_WAV), wav.cpu(), model.sr)
    print(f"question rendered: {wav.shape[-1] / model.sr:.1f}s")


async def run(barge_in: int) -> None:
    pc = RTCPeerConnection()
    track = GatedFileTrack(QUESTION_WAV)
    pc.addTrack(track)
    recorder = ReplyRecorder()
    bot_speaking = asyncio.Event()
    bot_stopped_again = asyncio.Event()

    @pc.on("track")
    def on_track(t):
        if t.kind == "audio":
            recorder.start(t)

    dc = pc.createDataChannel("chat")
    ping_task: asyncio.Task | None = None

    @dc.on("open")
    def on_open():
        nonlocal ping_task
        dc.send(json.dumps({"type": "ptt-close"}))

        async def pinger():
            while True:
                dc.send(f"ping-{time.time()}")
                await asyncio.sleep(1)
        ping_task = asyncio.create_task(pinger())

    @dc.on("message")
    def on_message(m):
        if not isinstance(m, str) or m.startswith("pong"):
            return
        try:
            msg = json.loads(m)
        except json.JSONDecodeError:
            return
        t = msg.get("type", "")
        if t == "bot-started-speaking":
            bot_speaking.set()
            bot_stopped_again.clear()
        elif t == "bot-stopped-speaking":
            bot_stopped_again.set()
        if t in ("bot-started-speaking", "bot-stopped-speaking",
                 "user-transcription", "bot-tts-text"):
            print(f"  server msg: {m[:120]}")

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://127.0.0.1:8011/api/offer",
            json={"sdp": pc.localDescription.sdp,
                  "type": pc.localDescription.type},
            timeout=30)
        resp.raise_for_status()
        answer = resp.json()
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    print("connected — waiting 10s for brain spawn + warmup")
    await asyncio.sleep(10)

    print(f"PTT open — streaming question ({track.duration_s:.1f}s)")
    dc.send(json.dumps({"type": "ptt-open"}))
    track.arm()
    await track.finished.wait()
    await asyncio.sleep(1.0)  # VAD stop_secs margin before releasing
    dc.send(json.dumps({"type": "ptt-close"}))
    print("PTT closed — waiting for the reply")

    try:
        await asyncio.wait_for(bot_speaking.wait(), timeout=45)
        print("bot started speaking")
    except asyncio.TimeoutError:
        print("WARNING: no bot-started-speaking event within 45s")
    await asyncio.sleep(20)  # let the reply play out

    interrupted_ok = 0
    for i in range(barge_in):
        # New turn, then paddle-press mid-reply.
        bot_speaking.clear()
        dc.send(json.dumps({"type": "ptt-open"}))
        track._pos = 0
        track.finished.clear()
        await track.finished.wait()
        await asyncio.sleep(1.0)
        dc.send(json.dumps({"type": "ptt-close"}))
        try:
            await asyncio.wait_for(bot_speaking.wait(), timeout=45)
        except asyncio.TimeoutError:
            print(f"  barge-in {i + 1}: bot never spoke — FAIL")
            continue
        await asyncio.sleep(1.5)  # let a sentence get going
        dc.send(json.dumps({"type": "ptt-interrupt"}))
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(bot_stopped_again.wait(), timeout=8)
            dt = time.perf_counter() - t0
            print(f"  barge-in {i + 1}: audio stopped {dt:.2f}s after press")
            interrupted_ok += 1
        except asyncio.TimeoutError:
            print(f"  barge-in {i + 1}: audio did NOT stop within 8s — FAIL")
        await asyncio.sleep(2)

    if ping_task:
        ping_task.cancel()
    await pc.close()

    dur = recorder.save(REPLY_WAV)
    voiced = recorder.speech_seconds()
    print(f"\nrecorded {dur:.1f}s ({voiced:.1f}s voiced) -> {REPLY_WAV}")
    if barge_in:
        print(f"barge-in: {interrupted_ok}/{barge_in} clean interruptions")

    if voiced > 1.0:
        from faster_whisper import WhisperModel
        # CPU int8: the live server owns the GPU.
        stt = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
        segments, _ = stt.transcribe(str(REPLY_WAV))
        text = " ".join(s.text.strip() for s in segments)
        print(f"reply transcript: {text!r}")
        print("\nE2E: PASS" if text.strip() else "\nE2E: FAIL (no transcript)")
    else:
        print("\nE2E: FAIL — no voiced reply audio recorded")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--barge-in", type=int, default=0)
    ap.add_argument("--question", default=(
        "Hey Fable, it's the end to end test speaking. Can you hear me? "
        "Tell me one short thing you remember about Jess."))
    ap.add_argument("--skip-render", action="store_true")
    args = ap.parse_args()
    if not args.skip_render or not QUESTION_WAV.exists():
        render_question(args.question)
    asyncio.run(run(args.barge_in))


if __name__ == "__main__":
    main()
