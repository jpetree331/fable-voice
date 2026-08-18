r"""fable-voice: realtime voice companion whose brain is a persistent headless
Claude session on Jess's subscription (no API keys — that's the product).

Pipeline (port of the donor architecture at E:\git\rowan-voice, read-only):
  SmallWebRTCTransport -> PttGate -> Silero VAD -> VoiceBargeIn
  -> faster-whisper large-v3-turbo -> FableBrainService (brain.py)
  -> Chatterbox-Turbo (Fable's cloned voice) -> browser audio.

Run:   $env:PYTHONUTF8='1'; .venv\Scripts\python voice_bot.py -t webrtc --port 8011
Page:  http://localhost:8011/         (push-to-talk page, the default)
       http://localhost:8011/client/  (open-mic Pipecat Playground)

Port 8011 — 8010 is Rowan's; see RECON.md for the machine port map.
"""
# ---- single-instance mutex (BEFORE any heavy import) -------------------------
# Two schedulers must never stack (the 2026-07-15 double-heartbeat incident:
# the fleet UI lies about Task Scheduler services). This runs before torch /
# services import so a losing instance exits without loading 5 GB into VRAM.
import ctypes
import sys

_MUTEX_NAME = "Global\\FableVoiceBot"
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    print("FableVoiceBot is already running (mutex held) — exiting.")
    sys.exit(0)

import torch  # noqa: F401,E402  (torch first — cuDNN DLLs for ctranslate2)

import asyncio
import datetime as _dt
import os
import random
import re
import time
import weakref
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from brain import FableBrain
from senses import WatchBuddySenses
from standing_senses import StandingSenses, b64
from services import (
    CHATTERBOX,
    REF_VOICE,
    BargeIn,
    ChatterboxTurboTTSService,
    SharedWhisperSTTService,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# ---- config -------------------------------------------------------------------
def _normalize_model(name: str) -> str:
    """Forgive human spellings: 'Fable-5', 'OPUS-4.8', 'opus 4.8' all resolve
    to the canonical CLI ids (claude-fable-5 / claude-opus-4-8)."""
    m = name.strip().lower().replace(" ", "-").replace(".", "-")
    if m and not m.startswith("claude-"):
        m = "claude-" + m
    return m or "claude-opus-4-8"


BRAIN_MODEL = _normalize_model(os.environ.get("FABLE_BRAIN_MODEL",
                                              "claude-opus-4-8"))


def _display_name(model: str) -> str:
    """Human name for the PTT page header: claude-opus-4-8 -> 'Opus 4.8',
    claude-fable-5 -> 'Fable 5'. Falls back to 'Claude'."""
    parts = model.removeprefix("claude-").split("-")
    words = [p for p in parts if not p.isdigit()]
    version = [p for p in parts if p.isdigit()]
    name = " ".join(w.capitalize() for w in words) or "Claude"
    return f"{name} {'.'.join(version)}".strip()


BRAIN_DISPLAY_NAME = _display_name(BRAIN_MODEL)
# Global push-to-talk key: hold it ANYWHERE on the machine (any tab, any
# app) to open the mic; release to send. Polled via GetAsyncKeyState — not a
# low-level hook, so Windows can't assassinate it (donor tuition). Empty
# disables. F9 is Rowan's paddle; Tab/modifiers are discouraged (they're
# navigation keys — holding them fights every app).
_VK = {**{f"f{i}": 0x6F + i for i in range(1, 25)},
       "pause": 0x13, "scroll_lock": 0x91, "tab": 0x09,
       "caps_lock": 0x14, "num_lock": 0x90}
GLOBAL_PTT_KEY = os.environ.get("FABLE_GLOBAL_PTT_KEY", "f8").strip().lower()
GLOBAL_PTT_VK = _VK.get(GLOBAL_PTT_KEY)

# Tools the voice brain may call WITHOUT a permission prompt (headless
# sessions can't answer prompts, so unlisted gated tools just fail).
# `mcp__<server>` allows every tool on that server.
BRAIN_ALLOWED_TOOLS = [t.strip() for t in os.environ.get(
    "FABLE_BRAIN_ALLOWED_TOOLS",
    "mcp__watch-buddy,mcp__claude-code-memory").split(",") if t.strip()]
# Pin the voice to a particular thread: a claude session id from E:\Talk.
# When set, the voice ALWAYS talks from that thread's loaded context — the
# first connect resumes it, and every later connect resumes the newest
# descendant of that lineage (no time window, never falls back to empty).
BRAIN_SESSION_PIN = os.environ.get("FABLE_BRAIN_SESSION_ID", "").strip()
VOICE_HALF_DUPLEX = os.environ.get("VOICE_HALF_DUPLEX", "").strip().lower() == "true"
FILLER_FIRST_S = float(os.environ.get("FILLER_FIRST_S", "2.5"))
FILLER_PROB = float(os.environ.get("FILLER_PROB", "0.6"))
WATCH_SENSES = os.environ.get("FABLE_WATCH_SENSES", "true").strip().lower() == "true"
WATCH_BUDDY_DIR = os.environ.get(
    "FABLE_WATCH_BUDDY_DIR",
    r"E:\git\1-Retired Projects\ClaudeWatch\watch_buddy")
RESUME_WINDOW_MIN = float(os.environ.get("FABLE_RESUME_WINDOW_MIN", "30"))

# Standing senses (settings-gear feature): Jess points the eyes/ears, the
# brain gets them hands-free. Restored from .env at startup.
STANDING = StandingSenses()
if os.environ.get("FABLE_EYES_TARGET", "").strip():
    logger.info("standing eyes: "
                + STANDING.set_eyes(os.environ["FABLE_EYES_TARGET"]))
if os.environ.get("FABLE_EARS_ON", "").strip().lower() == "true":
    logger.info("standing ears: " + STANDING.set_ears(
        os.environ.get("FABLE_EARS_DEVICE", ""), True))


def _update_env(key: str, value: str) -> None:
    """Persist a settings-gear choice to .env (replace or append the line)."""
    env_path = ROOT / ".env"
    lines = (env_path.read_text(encoding="utf-8").splitlines()
             if env_path.exists() else [])
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# Economics honesty (BRIEF.md): fable-5 rides the subscription only through
# 2026-07-19; after that it bills prepaid credits. Warn loudly, don't block.
if "fable" in BRAIN_MODEL and _dt.date.today() > _dt.date(2026, 7, 19):
    logger.warning(
        f"FABLE_BRAIN_MODEL={BRAIN_MODEL}: after 2026-07-19 Fable-5 turns bill "
        "PREPAID CREDITS, not the subscription. Switch to claude-opus-4-8 in "
        ".env if that's not intended (Anthropic has said Fable returns to "
        "subscriptions when compute allows).")

_SESSION_STATE = ROOT / "data" / "last_session.txt"

_FILLER_DIR = ROOT / "fillers"
_TOOL_PHRASE = "checking_notes"


def _load_fillers() -> dict[str, tuple[bytes, int]]:
    """Filler wavs as 16-bit PCM (torchaudio writes float32 WAVs stdlib wave
    can't read — soundfile handles both)."""
    import numpy as np
    import soundfile as sf

    out: dict[str, tuple[bytes, int]] = {}
    if not _FILLER_DIR.exists():
        logger.warning("no fillers/ directory — run make_fillers.py; "
                       "waits will just be silent")
        return out
    for path in sorted(_FILLER_DIR.glob("*.wav")):
        data, rate = sf.read(str(path), dtype="float32", always_2d=False)
        pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        out[path.stem] = (pcm, rate)
    logger.info(f"loaded {len(out)} filler takes: {', '.join(out)}")
    return out


def _phrase_of(take_name: str) -> str:
    return take_name.rsplit("_t", 1)[0]


FILLERS = _load_fillers()
GENERAL_TAKES = [n for n in FILLERS if _phrase_of(n) != _TOOL_PHRASE]
TOOL_TAKES = [n for n in FILLERS if _phrase_of(n) == _TOOL_PHRASE]


class BotSpeakingState:
    def __init__(self):
        self.speaking = False


BOT_SPEAKING = BotSpeakingState()

# Everything the bot spoke recently (sentences + timestamps). The senses
# echo-filter uses this to drop the bot's own voice from ear transcripts —
# the ears tap the same device the voice plays on (self-echo, 2026-07-15:
# Opus heard his own Polish-stream translation and got confused).
_RECENT_SPEECH: list[tuple[float, str]] = []
_RECENT_SPEECH_WINDOW_S = 120.0


def _note_bot_speech(text: str) -> None:
    now = time.time()
    _RECENT_SPEECH.append((now, text))
    cutoff = now - _RECENT_SPEECH_WINDOW_S
    while _RECENT_SPEECH and _RECENT_SPEECH[0][0] < cutoff:
        _RECENT_SPEECH.pop(0)


def _recent_bot_speech() -> list[str]:
    cutoff = time.time() - _RECENT_SPEECH_WINDOW_S
    return [s for t, s in _RECENT_SPEECH if t >= cutoff]

# Live PttGate instances (weak: gates die with their pipeline). The global
# hotkey poller drives every connected client's gate.
_PTT_GATES: "weakref.WeakSet" = weakref.WeakSet()
_ptt_poller_task: asyncio.Task | None = None

# ---- the Mouth (/api/say) ---------------------------------------------------
# Count of live WebRTC voice sessions. /api/say refuses while one is active:
# that mouth is already speaking to Jess, and two audio sources must never
# overlap (donor rule).
_voice_clients = 0
_say_lock = asyncio.Lock()

_SPEECH_JUNK_RE = re.compile(
    # emoji + symbol planes, joiners, variation selectors — TTS engine poison
    "[\\U0001F000-\\U0001FAFF\\U00002600-\\U000027BF\\U0001F1E6-\\U0001F1FF"
    "\\u2B00-\\u2BFF\\uFE0F\\u200D]+"
)


def _sanitize_speech(text: str) -> str:
    """Make arbitrary text-session prose safe for Chatterbox: strip emoji and
    markdown furniture, collapse whitespace."""
    t = _SPEECH_JUNK_RE.sub("", text)
    t = re.sub(r"[*_`#>|~]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _split_speech(text: str) -> list[str]:
    """Sentence-ish chunks; Chatterbox likes one sentence at a time."""
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return [p for p in (s.strip() for s in parts) if p]


async def _global_ptt_poller(_initial_vk: int) -> None:
    """Poll the global PTT key (~20ms). GetAsyncKeyState, not a hook —
    immune to Windows' low-level-hook assassination. Reads GLOBAL_PTT_VK
    live so the settings gear can rebind without a restart."""
    user32 = ctypes.windll.user32
    logger.info(f"global PTT: hold '{GLOBAL_PTT_KEY}' to talk from any app")
    was_down = False
    while True:
        vk = GLOBAL_PTT_VK
        if not vk:
            await asyncio.sleep(0.25)
            continue
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        if down != was_down:
            was_down = down
            for gate in list(_PTT_GATES):
                try:
                    if down:
                        await gate.external_open()
                    else:
                        await gate.external_close()
                except Exception:
                    logger.exception("global PTT: gate update failed")
        await asyncio.sleep(0.02)


class PttGate(FrameProcessor):
    """Server-side push-to-talk gate + explicit barge-in (donor O2 design).

    Why server-side: gating the client track (track.enabled=false) stops RTP
    entirely and the connection's idle watcher permanently disables the audio
    receiver after 2s — audio never recovers when PTT opens. So the client
    keeps its track live and sends data-channel messages; this gate drops
    InputAudioRawFrames while closed.

    Per-connection self-selection: gate is OPEN until the first ptt message
    (open-mic clients like /client never send one). Our PTT page announces by
    sending 'ptt-close' immediately on connect. Fail-safe direction: once a
    client announces PTT, a desync means audio is DROPPED, never leaked.

    'ptt-interrupt' (press while the bot speaks) broadcasts the interruption —
    the press itself is the barge-in signal; the paddle always wins.
    """

    def __init__(self):
        super().__init__()
        self._ptt_client = False
        self._gate_open = True
        _PTT_GATES.add(self)

    async def external_open(self) -> None:
        """Global-hotkey press: open the gate; the press interrupts the bot
        (the paddle always wins). Mirrors the ptt-open/ptt-interrupt path."""
        self._ptt_client = True
        self._gate_open = True
        if BOT_SPEAKING.speaking:
            logger.info("ptt(global): press while bot speaking — interrupting")
            await self.broadcast_interruption()
        await self.push_frame(OutputTransportMessageUrgentFrame(
            message={"type": "ptt-external", "data": {"open": True}}))
        logger.debug("ptt(global): gate OPEN")

    async def external_close(self) -> None:
        self._ptt_client = True
        self._gate_open = False
        await self.push_frame(OutputTransportMessageUrgentFrame(
            message={"type": "ptt-external", "data": {"open": False}}))
        logger.debug("ptt(global): gate CLOSED")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputTransportMessageFrame):
            msg = str(getattr(frame, "message", ""))
            if "ptt-interrupt" in msg:
                logger.info("ptt: press while bot speaking — broadcasting interruption")
                await self.broadcast_interruption()
                return
            if "ptt-open" in msg:
                if not self._ptt_client:
                    logger.info("ptt: client announced PTT mode")
                self._ptt_client = True
                self._gate_open = True
                logger.debug("ptt: gate OPEN")
                return
            if "ptt-close" in msg:
                if not self._ptt_client:
                    logger.info("ptt: client announced PTT mode")
                self._ptt_client = True
                self._gate_open = False
                logger.debug("ptt: gate CLOSED")
                return

        if self._ptt_client and not self._gate_open and isinstance(frame, InputAudioRawFrame):
            return  # gated: drop mic audio while PTT not engaged

        await self.push_frame(frame, direction)


class VoiceBargeIn(BargeIn):
    """BargeIn honoring half-duplex mode: while the bot speaks, user speech
    does NOT broadcast an interruption (background noise can't cut in)."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, BotStartedSpeakingFrame):
            BOT_SPEAKING.speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            BOT_SPEAKING.speaking = False

        if (
            VOICE_HALF_DUPLEX
            and BOT_SPEAKING.speaking
            and isinstance(frame, VADUserStartedSpeakingFrame)
        ):
            logger.debug("half-duplex: user speech during bot speech — no interruption")
            await FrameProcessor.process_frame(self, frame, direction)
            await self.push_frame(frame, direction)
            return

        await super().process_frame(frame, direction)


class FableBrainService(FrameProcessor):
    """Streams Fable's reply for each final transcription; speaks sentence by
    sentence. On barge-in the in-flight brain turn is interrupted (the
    persistent process survives); a new turn always awaits the old teardown.
    """

    def __init__(self, brain: FableBrain, senses: WatchBuddySenses | None):
        super().__init__()
        self._brain = brain
        self._senses = senses
        self._task: asyncio.Task | None = None
        self._filler_task: asyncio.Task | None = None
        self._last_filler: str | None = None
        self._fillers_this_turn = 0
        self._spoke_this_turn = False

    def _cancel_tasks(self) -> None:
        for t in (self._task, self._filler_task):
            if t and not t.done():
                t.cancel()

    async def _client_msg(self, type_: str, data: dict | None = None) -> None:
        """Status/caption event for the PTT page (static/ptt.html)."""
        await self.push_frame(OutputTransportMessageUrgentFrame(
            message={"type": type_, "data": data or {}}))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            BOT_SPEAKING.speaking = True
            await self._client_msg("bot-started-speaking")
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            BOT_SPEAKING.speaking = False
            await self._client_msg("bot-stopped-speaking")
            await self.push_frame(frame, direction)
        elif isinstance(frame, InterruptionFrame):
            if (self._task and not self._task.done()) or (
                    self._filler_task and not self._filler_task.done()):
                logger.info("fable: barge-in — interrupting in-flight turn + fillers")
                await self._brain.interrupt()
            self._cancel_tasks()
            await self.push_frame(frame, direction)
        elif isinstance(frame, TranscriptionFrame):
            if VOICE_HALF_DUPLEX and BOT_SPEAKING.speaking:
                logger.info(f"half-duplex: dropping transcript during bot speech: {frame.text!r}")
                return
            logger.info(f"fable <- {frame.text!r}")
            await self._client_msg("user-transcription",
                                   {"text": frame.text, "final": True})
            if self._task and not self._task.done():
                await self._brain.interrupt()
            self._cancel_tasks()
            self._fillers_this_turn = 0
            self._spoke_this_turn = False
            self._task = asyncio.create_task(self._stream_reply(frame.text))
            self._filler_task = asyncio.create_task(self._filler_watchdog())
        else:
            await self.push_frame(frame, direction)

    # ---- fillers ----
    async def _play_filler(self, take_name: str) -> None:
        if self._fillers_this_turn >= 2 or self._spoke_this_turn or not FILLERS:
            return
        pcm, rate = FILLERS[take_name]
        self._fillers_this_turn += 1
        self._last_filler = take_name
        logger.info(f"fable: filler #{self._fillers_this_turn} -> {take_name}")
        chunk = int(rate * 0.1) * 2  # 100ms of 16-bit mono — interruptible
        for i in range(0, len(pcm), chunk):
            await self.push_frame(TTSAudioRawFrame(pcm[i:i + chunk], rate, 1))

    def _pick_take(self, pool: list[str]) -> str | None:
        if not pool:
            return None
        options = [n for n in pool if n != self._last_filler] or pool
        return random.choice(options)

    async def _filler_watchdog(self) -> None:
        try:
            await asyncio.sleep(FILLER_FIRST_S)
            if random.random() < FILLER_PROB:
                take = self._pick_take(GENERAL_TAKES)
                if take:
                    await self._play_filler(take)
            else:
                logger.debug("fable: eligible pause, staying quiet (probabilistic)")
        except asyncio.CancelledError:
            pass

    async def _say(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            self._spoke_this_turn = True
            if self._filler_task and not self._filler_task.done():
                self._filler_task.cancel()
            logger.info(f"fable -> speak: {sentence!r}")
            _note_bot_speech(sentence)
            await self._client_msg("bot-tts-text", {"text": sentence})
            await self.push_frame(TTSSpeakFrame(sentence))

    async def _stream_reply(self, user_text: str) -> None:
        if self._senses is not None:
            block = self._senses.compose_block(
                recent_bot_speech=_recent_bot_speech())
            if block:
                logger.info(f"fable: attaching senses block ({len(block)} chars)")
                user_text = f"{block}\n\n{user_text}"
        images: list[str] = []
        if STANDING.eyes_target:
            jpeg, label = await asyncio.to_thread(STANDING.glance_jpeg)
            if jpeg:
                images.append(b64(jpeg))
                user_text = (f"[standing eyes: a live frame of {label}, "
                             f"grabbed as Jess spoke, is attached]\n\n"
                             + user_text)
                logger.info(f"fable: eyes glance attached ({label}, "
                            f"{len(jpeg) // 1024} KB)")
            else:
                user_text = (f"[standing eyes: target "
                             f"'{STANDING.eyes_target}' unavailable right "
                             f"now ({label})]\n\n" + user_text)
        try:
            async for ev in self._brain.stream_turn(user_text, images or None):
                if ev["kind"] == "sentence":
                    await self._say(ev["text"])
                elif ev["kind"] == "tool" and not self._spoke_this_turn:
                    take = self._pick_take(TOOL_TAKES)
                    if take:
                        await self._play_filler(take)
                elif ev["kind"] == "done":
                    ttft = ev["ttft_s"]
                    logger.info(
                        f"fable: turn done in {ev['total_s']:.2f}s "
                        f"(ttft {f'{ttft:.2f}s' if ttft is not None else 'n/a'}, "
                        f"interrupted={ev['interrupted']})")
        except asyncio.CancelledError:
            logger.info("fable: turn task cancelled (barge-in or new turn)")
            raise
        except Exception:
            logger.exception("fable: brain turn failed")
            await self._say("Sorry, I hit an error reaching my brain.")


def _load_resume_id() -> str | None:
    """Which session to resume, if any.

    Pinned (FABLE_BRAIN_SESSION_ID set): always continue that thread's
    lineage — the pin itself on first use (or when the pin changed), else the
    newest descendant we saved. No time window; never falls back to empty.

    Unpinned: resume the previous voice session only if the reconnect is
    within FABLE_RESUME_WINDOW_MIN; otherwise start fresh.
    """
    session_id, ts, seed = None, 0.0, ""
    try:
        raw = _SESSION_STATE.read_text(encoding="utf-8").strip().split("|")
        session_id, ts = raw[0] or None, float(raw[1])
        seed = raw[2] if len(raw) > 2 else ""
    except (OSError, ValueError, IndexError):
        pass
    if BRAIN_SESSION_PIN:
        if seed == BRAIN_SESSION_PIN and session_id:
            return session_id          # newest descendant of the pinned thread
        return BRAIN_SESSION_PIN       # first use of this pin (or pin changed)
    if session_id and (time.time() - ts) < RESUME_WINDOW_MIN * 60:
        return session_id
    return None


def _save_session_id(session_id: str | None) -> None:
    if not session_id:
        return
    try:
        _SESSION_STATE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_STATE.write_text(
            f"{session_id}|{time.time()}|{BRAIN_SESSION_PIN}",
            encoding="utf-8")
    except OSError:
        pass


async def bot(runner_args: RunnerArguments) -> None:
    assert isinstance(runner_args, SmallWebRTCRunnerArguments), "webrtc transport only"

    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_10ms_chunks=2,
        ),
    )

    brain = FableBrain(model=BRAIN_MODEL, allowed_tools=BRAIN_ALLOWED_TOOLS)
    senses = WatchBuddySenses(WATCH_BUDDY_DIR) if WATCH_SENSES else None

    # Donor-tuned VAD (Jess's ears, M3 sign-off).
    vad_params = VADParams(confidence=0.6, start_secs=0.15, stop_secs=0.8, min_volume=0.35)
    pipeline = Pipeline([
        transport.input(),
        PttGate(),
        VADProcessor(vad_analyzer=SileroVADAnalyzer(params=vad_params)),
        VoiceBargeIn(),
        SharedWhisperSTTService(
            model=os.environ.get("FABLE_WHISPER_MODEL", "large-v3-turbo"),
            device="cuda", compute_type="float16"),
        FableBrainService(brain, senses),
        ChatterboxTurboTTSService(CHATTERBOX, REF_VOICE),
        transport.output(),
    ])
    task = PipelineTask(pipeline)

    @transport.event_handler("on_client_connected")
    async def on_connected(t, c):
        global _ptt_poller_task, _voice_clients
        _voice_clients += 1
        if GLOBAL_PTT_VK and (_ptt_poller_task is None or _ptt_poller_task.done()):
            _ptt_poller_task = asyncio.create_task(_global_ptt_poller(GLOBAL_PTT_VK))
        resume = _load_resume_id()
        logger.info(
            f"client connected — Fable voice live (brain={BRAIN_MODEL}, "
            f"senses={'on' if senses else 'off'}, "
            f"half-duplex={'on' if VOICE_HALF_DUPLEX else 'off'}, "
            f"resume={resume or 'fresh'})")
        try:
            await brain.start(resume_session=resume)
        except Exception:
            logger.exception("brain failed to start")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(t, c):
        global _voice_clients
        _voice_clients = max(0, _voice_clients - 1)
        logger.info("client disconnected — stopping pipeline")
        _save_session_id(brain.session_id)
        await brain.close()
        await task.cancel()

    await PipelineRunner().run(task)


if __name__ == "__main__":
    from pipecat.runner.run import app, main
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel

    _PTT_PAGE = ROOT / "static" / "ptt.html"

    @app.get("/", include_in_schema=False)
    async def ptt_page():
        """The push-to-talk page (default surface), with the live brain model
        name injected so the header says who you're actually talking to.
        The open-mic Playground stays at /client/ for debugging."""
        html = _PTT_PAGE.read_text(encoding="utf-8").replace(
            "__BRAIN_NAME__", BRAIN_DISPLAY_NAME)
        return HTMLResponse(html)

    # ---- settings gear API ---------------------------------------------------

    @app.get("/api/health")
    async def fleet_health():
        """Fleet-dashboard health convention (agent-fleet status.py)."""
        return {"status": "ok"}

    @app.get("/api/senses")
    async def senses_get():
        targets = STANDING.list_targets()
        return {
            **targets,
            "devices": STANDING.list_devices(),
            "current": STANDING.status(),
            "ptt_key": GLOBAL_PTT_KEY if GLOBAL_PTT_VK else "",
            "ptt_keys": sorted(_VK, key=lambda k: (len(k), k)),
        }

    class EyesBody(BaseModel):
        target: str | None = None

    @app.post("/api/senses/eyes")
    async def senses_eyes(body: EyesBody):
        status = STANDING.set_eyes(body.target)
        _update_env("FABLE_EYES_TARGET", (body.target or "").strip())
        logger.info(f"settings: {status}")
        return {"status": status}

    class EarsBody(BaseModel):
        device: str | None = None
        enabled: bool = True

    @app.post("/api/senses/ears")
    async def senses_ears(body: EarsBody):
        status = STANDING.set_ears(body.device, body.enabled)
        _update_env("FABLE_EARS_ON", "true" if body.enabled else "false")
        _update_env("FABLE_EARS_DEVICE", (body.device or "").strip())
        logger.info(f"settings: {status}")
        return {"status": status}

    # ---- the Mouth -----------------------------------------------------------

    class SayBody(BaseModel):
        text: str

    @app.post("/api/say")
    async def api_say(body: SayBody):
        """Voice notes from text sessions: POST a line, it plays on the
        default output in Fable's cloned voice (same reference clip, same
        loaded Chatterbox — zero extra VRAM). Refused while a live voice
        session is connected: that mouth is already in use, and two audio
        sources never overlap."""
        if _voice_clients:
            return JSONResponse(
                {"status": "busy", "detail": "live voice session active"},
                status_code=409)
        text = _sanitize_speech(body.text)
        if not text:
            return {"status": "empty"}
        if len(text) > 800:
            return JSONResponse(
                {"status": "too_long",
                 "detail": "voice notes cap at 800 chars — this is a mouth, "
                           "not a Reader"},
                status_code=413)

        import numpy as _np
        import sounddevice as _sd

        def _render_and_play() -> float:
            chunks = []
            for sentence in _split_speech(text):
                with torch.inference_mode():
                    wav = CHATTERBOX.generate(
                        sentence, audio_prompt_path=REF_VOICE)
                chunks.append(wav.squeeze(0).clamp(-1.0, 1.0).cpu().numpy())
            if not chunks:
                return 0.0
            pcm = _np.concatenate(chunks)
            _sd.play(pcm, CHATTERBOX.sr, blocking=True)
            return len(pcm) / CHATTERBOX.sr

        async with _say_lock:
            if _voice_clients:  # a session connected while we queued
                return JSONResponse(
                    {"status": "busy", "detail": "live voice session active"},
                    status_code=409)
            logger.info(f"mouth: /api/say [{text[:80]}...]" if len(text) > 80
                        else f"mouth: /api/say [{text}]")
            seconds = await asyncio.to_thread(_render_and_play)
        return {"status": "spoke", "seconds": round(seconds, 2)}

    class PttBody(BaseModel):
        key: str

    @app.post("/api/senses/ptt")
    async def senses_ptt(body: PttBody):
        global GLOBAL_PTT_KEY, GLOBAL_PTT_VK, _ptt_poller_task
        key = body.key.strip().lower()
        if key and key not in _VK:
            return {"status": f"unknown key '{key}' — options: "
                              f"{', '.join(sorted(_VK))}"}
        GLOBAL_PTT_KEY, GLOBAL_PTT_VK = key, _VK.get(key)
        _update_env("FABLE_GLOBAL_PTT_KEY", key)
        if GLOBAL_PTT_VK and (_ptt_poller_task is None or _ptt_poller_task.done()):
            _ptt_poller_task = asyncio.create_task(
                _global_ptt_poller(GLOBAL_PTT_VK))
        status = f"global PTT: {key or 'off'}"
        logger.info(f"settings: {status}")
        return {"status": status}

    main()
