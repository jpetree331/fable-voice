# fable-voice — RUNBOOK

Operational reference. Written at build closeout, 2026-07-15. Companion
docs: `RECON.md` (donor recon + port map), `bench_results.md` (Phase 0
gate), `BUILD_REPORT.md` (the build's report of record).

## Service and port

| Service | Port | What | Managed by |
|---|---|---|---|
| fable voice_bot | **8011** | Pipecat pipeline + PTT page (`/`) + Playground (`/client/`) | Task Scheduler `FableVoiceBot` (at logon, 3 auto-restarts @1min) |

Neighbors that must never be disturbed: Rowan voice 8010 · Rowan API 8000 ·
Reader 5005 · Orpheus 5006 · TEI 8020 · Hindsight 8888/9999 · fleet 9000 ·
Chaos Gremlin 8002 · RPG Director 5008. Full inventory: donor RUNBOOK.

## Start / stop / restart

```powershell
# start manually (same as the scheduled task):
E:\git\fable-voice\start_voice_bot.cmd
# or foreground for debugging:
cd E:\git\fable-voice; $env:PYTHONUTF8='1'; .venv\Scripts\python voice_bot.py -t webrtc --host 0.0.0.0 --port 8011
# stop: kill the listener on 8011
Stop-Process -Id (Get-NetTCPConnection -State Listen -LocalPort 8011).OwningProcess -Force
# scheduled task control:
Start-ScheduledTask FableVoiceBot / Stop-ScheduledTask FableVoiceBot
```

A named mutex (`Global\FableVoiceBot`) inside voice_bot.py makes a second
instance exit immediately — two schedulers can never stack.

**Fleet dashboard (preferred day-to-day):** fable-voice is an agent card on
http://localhost:9000 (entry in `E:\git\agent-fleet\agents.yaml`, backup at
`agents.yaml.bak-20260715`) — Start/Stop/Restart buttons there. The ⚙ gear
on the PTT page has a "restart voice server" button that calls the fleet's
restart endpoint (use after .env edits). Note there are TWO managers: the
`FableVoiceBot` scheduled task owns the at-logon start; the fleet owns
manual lifecycle. They coexist — fleet Stop kills a task-started instance
via its port-listener fallback, and the mutex prevents stacking — but if a
card ever says "port in use, click Stop first", that's a task-started
instance the fleet hasn't adopted yet.

## Phone access (Tailscale)

Rowan's voice page owns `tailscale serve` 443→8010. To give the PTT page a
tailnet URL, add a second serve port (e.g. `tailscale serve --bg --https=8443
8011`) — pick a port that doesn't collide with existing serve config, and
verify Funnel stays OFF (`tailscale serve status` must say tailnet only).
Not configured by the build — needs Jess's call on the port. getUserMedia
requires HTTPS off-localhost, so the phone NEEDS the tailscale-serve URL.

## Logs

| What | Where |
|---|---|
| voice_bot (scheduled task) | `E:\git\fable-voice\logs\voice_bot.log` |
| Turn timings | same log: `brain: first token …s` / `fable: turn done in …s` |
| Brain session state | `data\last_session.txt` (session id + timestamp for resume) |
| Soak/verify artifacts | `out\` (e2e recordings, soak_results.md) |

## Env knobs (.env — all documented in .env.example)

`FABLE_BRAIN_MODEL` (claude-opus-4-8 default; claude-fable-5 bills PREPAID
CREDITS after 2026-07-19 — the bot logs a loud warning past that date;
spellings like "Fable-5"/"OPUS-4.8" are normalized to the canonical ids) ·
`FABLE_BRAIN_SESSION_ID` (empty default — set to an E:\Talk claude session id
to pin the voice to that thread: it talks from the thread's loaded context
and every reconnect continues the lineage; ids = .jsonl filenames in
`C:\Users\user\.claude\projects\E--Talk\`) ·
`FABLE_RESUME_WINDOW_MIN` (30 — unpinned only: reconnect resumes the last
voice session) ·
`FABLE_WATCH_SENSES` (true) · `FABLE_WATCH_BUDDY_DIR` ·
`VOICE_HALF_DUPLEX` (false) · `FILLER_FIRST_S` (2.5) · `FILLER_PROB` (0.6) ·
`FABLE_WHISPER_MODEL` (large-v3-turbo) ·
`FABLE_BRAIN_ALLOWED_TOOLS` (mcp__watch-buddy,mcp__claude-code-memory —
headless brains can't answer permission prompts; unlisted gated tools fail) ·
`FABLE_GLOBAL_PTT_KEY` (f8 — machine-wide hold-to-talk via GetAsyncKeyState
polling, works from any app/tab; avoid f9 = Rowan's paddle; empty disables) ·
`FABLE_EYES_TARGET` / `FABLE_EARS_ON` / `FABLE_EARS_DEVICE` (standing senses —
written by the ⚙ settings gear on the PTT page; eyes = one fresh frame of the
chosen window/screen attached to each spoken turn, ears = a standing
EarStream on the chosen output device. NOTE: standing_senses.py IMPORTS
watch-buddy's capture/window_finder/ears modules — deeper coupling than the
original file-tap design, per Jess's settings-gear request 2026-07-15. If
watch-buddy's ears also run in Claude Desktop, transcript lines double).

## VRAM budget (measured 2026-07-15, Phase 3.5)

Rowan's full stack resident: **17.0 GB**. Our models: Chatterbox-Turbo
**+3.1 GB**, whisper large-v3-turbo **+2.0 GB** → idle-loaded board total
**22.1 / 24.5 GB**. A live session adds ~0.3–0.5 GB (soak peak: see
`out\soak_results.md`). Headroom ~2 GB — fine for daily use; if OBS +
gaming push it, downshift `FABLE_WHISPER_MODEL` (medium ≈ −1 GB) before
touching anything of Rowan's. The brain subprocess is CPU-only (0 VRAM).

## Voice maintenance

- **Swap the voice**: replace `voices\fable_reference.wav` (~10–20s clean
  speech), run `.venv\Scripts\python make_fillers.py`, restart the bot.
- **Audition/regression**: `tts_smoke.py` renders + STT-verifies a line in
  the current voice.
- **Voice-turn system addendum**: `VOICE_ADDENDUM` in `brain.py` (working
  draft — Jess tunes after hearing it).

## Known failure modes → fixes

| Symptom | Cause → fix |
|---|---|
| chatterbox crash: mel filterbank "expected Double but found Float" | numpy 2 got installed → `pip install numpy==1.26.4 opencv-python==4.10.0.84` |
| startup UnicodeEncodeError (cp1252) | run via the .cmd wrapper (sets PYTHONUTF8=1) |
| faster-whisper cuDNN/cublas DLL errors | `import torch` must stay FIRST in voice_bot/services/tts_smoke |
| Chatterbox import: pkg_resources missing | setuptools ≥81 → `pip install setuptools==80.9.0` |
| All connects fail "Method Not Allowed" | someone replaced the runner — must run via `pipecat.runner` (`-t webrtc`), never a hand-rolled server |
| Bot never speaks; log shows brain stderr | `claude` CLI missing/broken on PATH, or logged-out CLI → run `claude` interactively once to re-auth |
| Brain replies but nothing about today | resume window expired (fresh session is correct); recall tool still reaches the bank |
| Second instance exits immediately | the mutex — that's the design (check Task Scheduler didn't double-fire) |
| Mic dead on the PTT page after PTT | client must NEVER gate its own track; page keeps track live by design — if a fork re-adds `track.enabled=false`, revert it |
| fable-5 billing surprise after 2026-07-19 | switch FABLE_BRAIN_MODEL to claude-opus-4-8 (see .env.example) |

## Never-list (inherited from the brief)

`E:\git\rowan-voice` read-only donor · `E:\Rowan`, `Documents\Rowan` do not
enter · `E:\git\Hindsight` never touch · Rowan's tasks/ports/DBs/VRAM
untouched · no API keys for the brain · voice choice belongs to Jess.
