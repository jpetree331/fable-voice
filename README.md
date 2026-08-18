# fable-voice

**Talk to Fable out loud.** A realtime voice companion whose brain is a
persistent headless Claude session running on Jess's Max subscription — no
API keys, no per-token billing, ever. The brain runs with `cwd=E:\Talk`, so
it wakes up as Fable: identity from CLAUDE.md, automatic memory recall at
session start, automatic retention of every exchange to the family bank and
the Postgres archive. The voice already knows you.

Architecture ported from the proven donor at `E:\git\rowan-voice` (studied
read-only); the one novel component is the brain bridge.

```
browser (PTT page, :8011) ── WebRTC ──┐
                                       │
   voice_bot.py: PttGate → Silero VAD → BargeIn → faster-whisper (STT)
        → FableBrainService ──stdin/stdout stream-json──> claude -p
        → Chatterbox-Turbo (cloned voice) → browser audio    (cwd=E:\Talk)
                                       │
   senses.py: read-only taps of watch-buddy's observations db + transcripts
```

## Quick start

```powershell
cd E:\git\fable-voice
$env:PYTHONUTF8='1'
.venv\Scripts\python voice_bot.py -t webrtc --host 0.0.0.0 --port 8011
```

Open **http://localhost:8011/** — the push-to-talk page (default surface).
Hold the circle (or Space) to talk; release to send; pressing while Fable
speaks interrupts him. The open-mic Pipecat Playground lives at `/client/`
for debugging. Normally none of this is needed: the **FableVoiceBot**
scheduled task starts everything at logon.

## The pieces

| File | What |
|---|---|
| `brain.py` | Persistent headless Claude subprocess (stream-json), sentence chunker, interrupt handling, `--resume` support |
| `voice_bot.py` | The Pipecat pipeline + PTT gate + fillers + senses injection |
| `services.py` | Chatterbox-Turbo TTS + shared whisper STT + VRAM-accounted preload |
| `senses.py` | Watch-buddy senses (read-only db + transcript taps) |
| `static/ptt.html` | The push-to-talk web page |
| `make_fillers.py` | Regenerate filler clips (rerun if the voice changes) |
| `bench_brain.py` | Phase 0 latency benchmark (results: `bench_results.md`) |
| `test_brain.py` / `test_e2e.py` / `test_soak.py` | Verification suite |

## Voice

Fable's voice is a Chatterbox-Turbo zero-shot clone of
`voices/fable_reference.wav` — a 14s segment auto-selected (by voice
activity) from the sample Jess supplied 2026-07-15 (`189467_voice-sample.wav`,
publicly available free voice for non-commercial use; full copy kept at
`voices/fable_reference_full.wav`). To change the voice: replace
`voices/fable_reference.wav`, run `make_fillers.py`, restart the bot.

## Config

Everything lives in `.env` — see `.env.example` for every knob documented,
especially the `FABLE_BRAIN_MODEL` economics note (opus-4-8 = subscription
forever; fable-5 = subscription only through 2026-07-19, prepaid credits
after). An empty `.env` is a working config.

## Ops

See `RUNBOOK.md`. Report of record for the build: `BUILD_REPORT.md`.

## Note for cloners: bring your own voice

The reference voice clip and pre-baked filler audio are not included in
this repo (license caution — use any voice sample you have rights to).
Drop your clip at `voices/fable_reference.wav`, then run
`make_fillers.py` to bake filler clips in your chosen voice. Everything
else about the pipeline is voice-agnostic.
