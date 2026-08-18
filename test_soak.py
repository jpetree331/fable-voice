r"""30-minute continuous-session soak: one WebRTC client, a spoken turn every
~100s, VRAM sampled per turn, voice-to-voice latency logged per turn.

Verify goals (BRIEF.md): stable session, no VRAM creep, Rowan's services
still healthy afterward (health checked by run_verify.ps1, not here).

Run (server must be up): .venv\Scripts\python test_soak.py [--minutes 30]
Writes: logs/soak.log (append) + out/soak_results.md
"""
from __future__ import annotations

import torch  # noqa: F401

import argparse
import asyncio
import json
import statistics
import subprocess
import time
from pathlib import Path

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription

from test_e2e import GatedFileTrack, ReplyRecorder, QUESTION_WAV, render_question

ROOT = Path(__file__).resolve().parent
SOAK_WAV = ROOT / "out" / "soak_question.wav"
RESULTS = ROOT / "out" / "soak_results.md"

SOAK_QUESTION = (
    "Soak test turn, still here. Reply with one short sentence of your choice.")


def vram_mb() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10)
    return int(out.stdout.strip().splitlines()[0])


async def run(minutes: float, period_s: float) -> None:
    pc = RTCPeerConnection()
    track = GatedFileTrack(SOAK_WAV)
    pc.addTrack(track)
    recorder = ReplyRecorder()
    bot_started = asyncio.Event()

    @pc.on("track")
    def on_track(t):
        if t.kind == "audio":
            recorder.start(t)

    dc = pc.createDataChannel("chat")
    ping_task = None

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
        if isinstance(m, str) and '"bot-started-speaking"' in m:
            bot_started.set()

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://127.0.0.1:8011/api/offer",
            json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            timeout=30)
        resp.raise_for_status()
        answer = resp.json()
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    print("connected — 12s warmup", flush=True)
    await asyncio.sleep(12)

    t_end = time.time() + minutes * 60
    turn = 0
    rows: list[tuple[int, float | None, int]] = []
    while time.time() < t_end:
        turn += 1
        bot_started.clear()
        dc.send(json.dumps({"type": "ptt-open"}))
        track._pos = 0
        track.finished.clear()
        track.arm()
        await track.finished.wait()
        await asyncio.sleep(1.0)
        dc.send(json.dumps({"type": "ptt-close"}))
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(bot_started.wait(), timeout=60)
            v2v = time.perf_counter() - t0
        except asyncio.TimeoutError:
            v2v = None
        vram = vram_mb()
        rows.append((turn, v2v, vram))
        print(f"turn {turn}: release->first-audio "
              f"{f'{v2v:.2f}s' if v2v else 'TIMEOUT'} | VRAM {vram} MiB",
              flush=True)
        await asyncio.sleep(max(0, period_s - track.duration_s - 3))

    if ping_task:
        ping_task.cancel()
    await pc.close()

    ok = [v for _, v, _ in rows if v is not None]
    vrams = [v for _, _, v in rows]
    lines = [
        "# 30-minute soak results",
        f"turns: {len(rows)}, completed: {len(ok)}, timeouts: {len(rows) - len(ok)}",
        (f"release->first-audio: median {statistics.median(ok):.2f}s, "
         f"min {min(ok):.2f}s, max {max(ok):.2f}s" if ok else "no completed turns"),
        f"VRAM first {vrams[0]} MiB -> last {vrams[-1]} MiB "
        f"(peak {max(vrams)}) — creep = {vrams[-1] - vrams[0]:+d} MiB",
        "",
        "| turn | v2v (s) | VRAM (MiB) |",
        "|---|---|---|",
    ] + [f"| {t} | {f'{v:.2f}' if v else 'timeout'} | {m} |" for t, v, m in rows]
    RESULTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:5]), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--period", type=float, default=100)
    args = ap.parse_args()
    if not SOAK_WAV.exists():
        import shutil
        render_question(SOAK_QUESTION)  # writes QUESTION_WAV; moved to ours
        shutil.move(str(QUESTION_WAV), str(SOAK_WAV))
    asyncio.run(run(args.minutes, args.period))


if __name__ == "__main__":
    main()
