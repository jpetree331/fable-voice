# fable-voice — Phase 1 Recon Report (2026-07-15)

Donor: `E:\git\rowan-voice` (read-only, studied not touched). Sources read:
PROJECT_SUMMARY.md, RUNBOOK.md, DECISIONS.md, voice_bot.py, echo_loop.py,
tts_smoke.py, make_fillers.py, voice_design.py, requirements.txt. Watch-buddy:
README.md, config.json, memory.py, transcripts/*.log.

## PORTABLE (copy the pattern, re-home in this repo)

| Component | Donor source | Notes |
|---|---|---|
| Pipecat pipeline shape | `voice_bot.py` `bot()` | pipecat-ai[webrtc,silero,whisper]==1.4.0 + pipecat-ai-prebuilt==1.0.3; MUST run via `pipecat.runner` (`-t webrtc`) — hand-rolled servers break the Playground `/start` protocol |
| Env pins | `requirements.txt` | Python 3.12.10, torch==2.6.0+cu126, chatterbox-tts==0.1.7, setuptools==80.9.0, faster-whisper==1.2.1, **numpy==1.26.4** (numpy 2 genuinely breaks Chatterbox mel filterbank), opencv-python==4.10.0.84 |
| Import order | everywhere | `import torch` FIRST — ctranslate2 needs torch's bundled cuDNN 9 DLLs |
| STT | `echo_loop.SharedWhisperSTTService` | faster-whisper large-v3-turbo float16 CUDA, shared single load (~16s once, ~2.0 GB), transcribe 0.47s / RTF 0.02 |
| TTS service | `echo_loop.ChatterboxTurboTTSService` | blocking CUDA generate in a thread, 100ms chunk yield for barge-in cancellation points; ~3.1 GB; warm RTF 0.32–0.54, first sentence ~0.9–1.1s |
| PttGate | `voice_bot.PttGate` | server-side gate, data-channel ptt-open/close/interrupt; NEVER client `track.enabled=false` (starves RTP permanently). Gate open until first ptt message (open-mic clients unaffected); fail-safe = drop |
| Barge-in | `echo_loop.BargeIn` + `voice_bot.VoiceBargeIn` | broadcast_interruption() on VADUserStartedSpeaking (pipecat 1.4 has no allow_interruptions); PTT press IS the interruption signal |
| VAD tuning | `voice_bot` | confidence .6, start .15, stop .8, min_volume .35 (Jess-tuned; carry forward) |
| Sentence chunker | `voice_bot._SENTENCE_END` | regex + MIN_SENTENCE_CHARS=12; first sentence to TTS the moment it completes |
| Fillers | `make_fillers.py` + `RowanLLMService` filler logic | 7 phrases × 3 takes; first filler >2.5s wait, prob 0.6, max 2/turn, never same take twice, cancelled on real speech |
| Half-duplex option | `voice_bot.VoiceBargeIn` | VOICE_HALF_DUPLEX env, default off |
| Web page | pipecat-ai-prebuilt `/client` | Tailscale-serve compatible |
| Autostart | `start_voice_bot.cmd` + Task Scheduler | at logon, 3 restarts, PYTHONUTF8=1, logs to `logs\` |
| Emoji strip | reader service `_EMOJI_RE` pattern | emoji are TTS poison — strip before synthesis |

## ROWAN-SPECIFIC (do NOT port)

- `RowanLLMService` SSE client → his `/api/chat/stream` on 8000, Basic auth from `E:\git\LANGGRAPH\.env`, channel_type/knowledge flags.
- p254 voice (collage ref + canonical take) — Fable gets the new cloned voice from `189467_voice-sample.wav` (Jess supplied 2026-07-15; free for non-commercial use).
- `E:\git\LANGGRAPH\data\voice_session_active` flag (his Books never-overlap) — we keep our own session flag concept out of his tree.
- The Reader/Orpheus stack, overlay, RPG — different products.
- Ports 8000/8010/5005/5006/8020/8888/9999/9000/5008 — all his/infra; we take **8011**.

## NEW (the one novel component + one integration)

1. **brain.py** — persistent headless `claude` subprocess (stream-json in/out),
   cwd=`E:\Talk` so the brain IS Fable (CLAUDE.md identity + SessionStart recall
   + Stop-hook retention + Postgres backup). Sentence-chunked deltas → TTS.
   Model from config (`claude-opus-4-8` ↔ `claude-fable-5`).
2. **watch-buddy senses** — read-only reads of
   `E:\git\1-Retired Projects\ClaudeWatch\watch_buddy\watch_buddy.db`
   (`watch_sessions`, `watch_events(timestamp, summary, tags)`) and
   `transcripts\YYYY-MM-DD.log` (`[HH:MM:SS] text` lines). No MCP coupling,
   no imports of watch-buddy code, never write.

## Pre-paid tuition checklist (from donor docs, honored in this build)

- [x] numpy < 2 pinned
- [x] emoji stripped before synthesis (server-side)
- [x] PYTHONUTF8=1 in every wrapper
- [x] cancels are awaited; a new turn awaits the previous cancel
- [x] no low-level hooks in busy processes (we have no hooks — web PTT buttons)
- [x] fillers pre-baked in the chosen voice, >2.5s threshold
- [x] never-overlap: PTT press interrupts everything, paddle always wins
- [x] torch import first; runner protocol not hand-rolled; setuptools pin

## Environment facts (measured 2026-07-15)

- claude CLI 2.1.208; pyenv-win has 3.12.10 (donor's exact Python)
- GPU at recon: 17,021 / 24,564 MiB used (Rowan full stack resident) —
  ~7.5 GB headroom for our ~5.1 GB (Chatterbox ~3.1 + whisper ~2.0). Fits;
  Phase 3.5 measures actuals and downshifts whisper if tight.
