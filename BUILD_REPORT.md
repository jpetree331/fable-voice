# fable-voice — Build Report (report of record)

**Built and verified 2026-07-15, one session, per `BRIEF.md`** (Recon → Build
→ Verify). Jess was out; autonomy clause exercised throughout. Donor
`E:\git\rowan-voice` studied strictly read-only — nothing of Rowan's was
modified, moved, or run.

## What shipped

A realtime voice companion on port **8011**: hold-to-talk web page → WebRTC →
server-side PTT gate → Silero VAD → faster-whisper large-v3-turbo →
**persistent headless Claude brain** (stream-json subprocess, cwd=`E:\Talk`,
so the brain is Fable with automatic memory recall/retention) →
sentence-chunked Chatterbox-Turbo TTS in the cloned voice from Jess's sample
→ browser audio. Fillers cover thinking waits; barge-in interrupts cleanly;
watch-buddy senses ride in read-only. Autostart: `FableVoiceBot` scheduled
task (at logon, 3 restarts, mirrors RowanVoiceBot) + a named mutex checked
before any model loads.

## Phase 0 — the gate (PASSED, full data in `bench_results.md`)

Persistent session vs the 11s spawn-per-turn baseline: steady-state TTFT
**1.1–6s** (median ~3.5s fable-5, ~4.2s opus-4.8), measured in production
shape (identity + hooks active). Discovery: back-to-back turns can queue
behind the previous turn's Stop-hook retention; natural speech pacing absorbs
it.

## Latency table (measured, warm — donor-RUNBOOK style)

| Stage / turn | Measured | Notes |
|---|---|---|
| PTT release → transcription | ~0.9–1.2s | VAD stop 0.8s + whisper ~0.1–0.3s |
| → first brain token | 1.1–5.7s (median 4.1s in soak session) | provider variance; fillers cover >2.5s |
| → first audio (voice-to-voice) | **median 3.29s, min 2.80, max 6.44** (soak, n=18) | vs Rowan's 3.6–8s TTFT |
| First turn after connect | +~4–9s once | SessionStart memory recall; brain spawns at connect, not first utterance |
| TTS first sentence | ~0.9–1.1s warm | Chatterbox-Turbo RTF 0.32–0.54 warm |

## Verify checklist (BRIEF.md) — all run

- **Latency table**: above. Beats the Phase 0 bar and the donor's envelope.
- **Barge-in ×10**: `test_e2e.py --barge-in 10` → **10/10 clean** — audio
  stopped instantly on the press, no orphaned audio, every follow-up turn
  worked (no cancel races; the turn lock + awaited interrupt design).
- **30-min continuous session**: `test_soak.py` → 18/18 turns, 0 timeouts,
  VRAM 22,262 → 22,739 MiB (**+477 MiB, asymptoting — last 5 turns +20 MiB**;
  session buffers, not a leak). `out/soak_results.md`.
- **Rowan's services**: checked before, during, after — API 8000 (401=auth
  alive), voice 8010, Reader 5005, Hindsight 8888 all healthy throughout.
- **Memory continuity**: Hindsight recall surfaces the voice sessions
  (verified from this build session); Postgres `talk_exchanges` gained 68
  cwd=E:\Talk rows across both models during testing. Resume verified: a
  reconnect within 30 min resumed session `13bd68b3…` via `--resume`.
- **Model switch**: `.env` flip only — `test_brain.py claude-fable-5` ran the
  identical bridge green (spawn/stream/interrupt/follow-up all pass).
- **Suite**: `test_brain.py` (both models), `test_e2e.py` (+ STT round-trip
  of the reply audio), `test_soak.py`, `tts_smoke.py` (STT-verified render),
  `bench_brain.py` — all green.

## VRAM budget (Phase 3.5, measured)

Rowan full stack resident 17.0 GB → +3,100 MiB Chatterbox-Turbo + 1,984 MiB
whisper = **22.1 GB idle-loaded, 22.7 GB peak in a live session** (of 24.5).
~1.8 GB headroom at peak: fine for daily use; if OBS + a game join, downshift
`FABLE_WHISPER_MODEL` first (RUNBOOK). Brain subprocess is CPU-only. Nothing
of Rowan's was resized or degraded (his services healthy at 22.7 GB peak).

## Deviations from the brief (autonomy clause, reported honestly)

1. **No voice-audition stop.** The brief reserved voice choice for Jess's
   ear ritual; her kickoff message superseded it by supplying the voice
   sample ("I have a voice clip sample… that I want you to use"). I
   auto-trimmed the most voice-active 14s (`voices/fable_reference.wav`);
   `tts_smoke.py` output (`out/smoke.wav`) is the audition-equivalent take.
   **Fillers were baked in this voice unheard by Jess — rerun
   `make_fillers.py` after any reference swap.**
2. **Phase 4 built without the design-first pause.** The brief said "design
   first, ask Jess"; her kickoff put watch-buddy integration in scope. Built
   as specced: read-only file taps, no MCP coupling, proven live (a senses
   block was attached during E2E and the brain used it sanely).
3. **`.env` defaults to claude-opus-4-8**, not fable-5, although fable-5
   measured faster — no-surprise-billing wins while Jess is away (fable-5
   exits the subscription 2026-07-19). One-line flip documented in
   `.env.example`; the bot logs a loud warning if fable-5 is set past that
   date.
4. **Tailscale serve for the phone NOT configured** — Rowan's page owns
   443→8010, and adding a second serve port is a shared-config change worth
   Jess's eyes. Recipe in RUNBOOK.
5. **pip resolver refused the donor's requirements.txt as one transaction**
   (pipecat vs pinned opencv); installed staged (pipecat first, then forced
   numpy/opencv pins) — `pip check` clean, matching the donor's true seam.

## Post-ship fix — turn desync (2026-07-15 evening)

Live failure on Jess's long thread: after a barge-in, every reply answered
the PREVIOUS question ("first token 0.00s" in the log = the next turn
consuming the interrupted turn's stale result event). Root cause: cancelling
the consumer task abandoned the in-flight turn's remaining stdout; the pipe
stayed one turn ahead of the readers. Fix: `stream_turn` now guarantees an
abandoned turn is DRAINED to its result event before the next turn writes
(drain task + await-under-lock; poisoned-pipe fallback respawns with
`--resume`). Regression: `test_brain_desync.py` — which, unlike the original
barge-in test, asks DIFFERENT questions per turn and checks the answer
matches the question. **Testing lesson recorded: the original 10/10 barge-in
verify asked the identical question every round, so one-behind answers were
invisible. Repetition-blind tests pass desync bugs.**

## Honest residuals / flags

- Benchmark + test turns were retained to the memory bank by the Stop hooks
  (production shape = real retention). They're labeled as tests in their own
  text; harmless, but the bank has some "soak test turn" entries dated today.
- The `retain` MCP with `scope="project"` saved to claude-master-mem only —
  the per-repo notebook for this new repo may not be provisioned yet.
  Flagging rather than touching Hindsight.
- Voice quality judgment (does it sound like Fable?) is Jess's — the machine
  can only verify intelligibility (STT round-trips were verbatim).
- `VOICE_ADDENDUM` wording in `brain.py` is a working draft for Jess to tune
  after hearing it.

## What you need to do once (Jess)

1. Open **http://localhost:8011/**, hold the circle, say hi. Judge the voice.
2. If the voice pleases: nothing. If not: swap `voices/fable_reference.wav`,
   run `make_fillers.py`, restart.
3. Decide the phone story (tailscale serve port — RUNBOOK recipe).
4. Optionally flip `FABLE_BRAIN_MODEL=claude-fable-5` until 2026-07-19.
