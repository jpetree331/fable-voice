# FABLE VOICE — Build Brief (Path B: realtime conversational voice on subscription Claude)

> **STATUS 2026-07-15: BUILT AND VERIFIED.** Phase 0 gate PASSED
> (`bench_results.md`). Report of record: `BUILD_REPORT.md`. Ops:
> `RUNBOOK.md`. Two deviations from this brief, both from Jess's kickoff
> message: the voice-audition stop was superseded (she supplied the voice
> sample), and Phase 4 watch-buddy senses were built (she put them in scope).

**For:** a fresh Claude Code thread in `E:\git\fable-voice`
**From:** Fable (campfire thread) + Jess, 2026-07-15
**Format:** Recon → Build → Verify. Report deviations honestly per the
autonomy clause — if the spec's assumption is wrong, fix the true seam and
document it.

## Mission

Build a realtime voice companion — talk out loud, get spoken replies, with
barge-in — whose brain is **Claude on Jess's Max subscription** (NOT API
credits; that's the entire point vs. questie.ai). Brain model must be
**tunable**: `claude-opus-4-8` or `claude-fable-5` via config. Port the
proven architecture from Rowan's voice stack rather than inventing one.

**Donor codebase: `E:\git\rowan-voice` — STRICTLY READ-ONLY.** Study
everything, copy patterns freely into this repo, but never modify, move, or
run anything of Rowan's. His services (ports 8000/8010/5005/5006), DBs, and
scheduled tasks must be untouched and unconflicted. Also never touch
`E:\git\Hindsight` in any way.

## Phase 0 — THE GATE: persistent-brain latency benchmark

The go/no-go. Already probed from the campfire thread:

- Naive `claude -p "..."` spawn-per-turn: **11.9s cold / 11.0s warm** for a
  one-word reply — i.e., ~11s of pure overhead. **FAILS** voice (target:
  first token < 6s; Rowan's Kimi brain runs 3.6–8s TTFT for comparison).
- Therefore the design REQUIRES a **persistent brain process**: ONE
  long-running headless claude session per voice session, turns streamed in,
  tokens streamed out. Benchmark THIS:
  - `claude -p --input-format stream-json --output-format stream-json
    --verbose` as a long-lived subprocess: feed a user message JSON line,
    measure time-to-first-assistant-token, then feed follow-up turns and
    measure steady-state TTFT. (Alternative if smoother: the Python
    `claude-agent-sdk` — it drives the same CLI and holds sessions open;
    verify it uses subscription auth, not an API key.)
  - Matrix: {claude-opus-4-8, claude-fable-5} × {first turn, later turns} ×
    {short reply, longer reply}. 5 samples each, report a table like
    Rowan's RUNBOOK latency table.
  - **PASS bar: steady-state TTFT ≤ 4s.** (Fillers cover the rest — Rowan's
    threshold: waits > 2.5s get a filler clip.)
  - If FAIL after honest tuning: STOP. Write the numbers to this file and
    report to Jess. Do not build a laggy voice bot.
- Economics note for the config docs: Opus 4.8 runs on subscription
  indefinitely. Fable-5 runs on subscription **through July 19, 2026**;
  after that Fable-5 turns bill prepaid credits (Anthropic has said Fable
  returns to subs when compute allows). The `brain_model` config comment
  should say this so future-Jess isn't surprised by a meter.

## Phase 1 — Donor recon (read-only)

Read, in order: `PROJECT_SUMMARY.md`, `RUNBOOK.md` (latency table + service
management), `DECISIONS.md` (why everything is the way it is — this file is
gold), then `voice_bot.py`, `echo_loop.py`, `tts_smoke.py`,
`make_fillers.py`, `voice_design.py`, `bench_models.py`.

Inventory to extract: Pipecat version + pipeline shape; faster-whisper
large-v3-turbo config; Chatterbox-Turbo install recipe and its instance
footprint; the PttGate (server-side audio gate — NEVER client track tricks;
`track.enabled=false` starves RTP permanently); filler-clip generation
pipeline; barge-in mechanics; half-duplex option; the voice web page.

Deliverable: a short recon report mapping PORTABLE (pipeline, STT, TTS,
PTT, fillers, page) vs ROWAN-SPECIFIC (his SSE brain endpoint, his p254
voice, his Hindsight wiring) vs NEW (the persistent-claude brain bridge).

**Pre-paid tuition — hard-won lessons from the donor docs, honor them all:**
- numpy 2 breaks Chatterbox (pin < 2)
- emoji are TTS engine poison — strip before synthesis
- cp1252 breaks emoji prints on Windows — PYTHONUTF8=1 everywhere
- un-awaited cancels race their own next request
- Windows reuses PIDs and kills low-level hooks in busy processes
- fillers cover any wait > ~2.5s; pre-bake them in the chosen voice
- never-overlap rules between concurrent audio sources; PTT press IS the
  interruption signal — the paddle always wins

## Phase 2 — The brain bridge (the one novel component)

A `brain.py` that owns a persistent headless claude subprocess:
- Session lifecycle: spawn on voice-session start with
  `--model {config.brain_model}`; keep alive across turns; `--resume` /
  session-id handling for reconnects; clean teardown.
- **Run it with cwd = `E:\Talk`.** This is deliberate and load-bearing:
  the user-level CLAUDE.md makes the brain FABLE (identity, memory rules),
  and the SessionStart/Stop hooks give every voice session automatic
  memory recall and retention in the family bank, plus the Postgres raw
  backup. The voice bot gets a brain that already knows Jess. Verify hooks
  fire in headless mode; if they don't, document and wire retention
  manually via the transcript.
- Streaming: parse stream-json assistant deltas → sentence-boundary chunker
  → TTS queue. First sentence goes to synthesis the moment it completes.
- A voice-turn system addendum (like Rowan's `ephemeral_context`): "You are
  speaking aloud via TTS. 1-3 sentences per turn unless asked for more. No
  emoji, no markdown, no code blocks. Plain warm speech."
- Cancellation: barge-in must abort the in-flight turn (interrupt/kill the
  streaming response cleanly WITHOUT killing the persistent process — if
  the CLI can't cancel mid-stream, document the fallback: discard tokens
  and mute TTS; never let a cancel race the next request).

## Phase 3 — Pipeline port

New Pipecat pipeline in this repo, structure mirroring the donor:
- Voice web page on **port 8011** (8010 is Rowan's; also avoid
  8000/8888/8020/5005/5006/5173/9999). Tailscale-serve compatible like his.
- **Push-to-talk by DEFAULT** (kid-aggro solution on the input side —
  Jess's requirement; open-mic optional later). Server-side gate per donor.
- STT: faster-whisper large-v3-turbo, OWN instance (do not touch Rowan's).
- TTS: **Chatterbox-Turbo** (Jess's explicit pick for realtime — Orpheus is
  the Reader's engine, not this). New voice for Fable: generate an audition
  set (donor `voice_design.py` pattern) and STOP for Jess to choose by ear
  — voice selection is HER ritual, never auto-picked. Then bake filler
  clips in the chosen voice (`make_fillers.py` pattern).
- Barge-in + never-overlap per donor patterns.
- Autostart: `FableVoiceBot` scheduled task at logon, 3 restarts, mirroring
  `RowanVoiceBot` — plus a process mutex so two schedulers can never stack
  (see the 2026-07-15 double-heartbeat incident; the fleet UI lies about
  Task Scheduler services).

## Phase 3.5 — VRAM budget (measure, don't assume)

Rowan's full stack: 16.5/24.5 GB with ~8 GB headroom (his RUNBOOK). A
second whisper large-v3-turbo (~1.5 GB) + a second Chatterbox instance +
brain process must fit WITHOUT degrading his services. Measure with
`nvidia-smi` before and after each component loads; if headroom is tight,
options in order: share nothing but downshift OUR whisper to a smaller
model; lazy-load TTS; never touch his allocations. Report actuals.

## Phase 4 (stretch — separate sign-off) — Watch-buddy senses

The brain can optionally read watch-buddy's world: latest observations from
`E:\git\1-Retired Projects\ClaudeWatch\watch_buddy\watch_buddy.db` and the
day's `transcripts/*.log` — so "what's happening on stream?" is answerable
by voice. Read-only file access, no MCP coupling. Design first, ask Jess.

## Verify (before "done")

- Latency table in RUNBOOK style: PTT-release → STT text; → first brain
  token; → first audio; steady-state turn gaps. Compare vs Rowan's table
  and vs the Phase 0 bar.
- Barge-in: interrupt mid-sentence 10×; no orphaned audio, no cancel races.
- 30-minute continuous session: stable, no VRAM creep, Rowan's services
  still healthy afterward (check his ports respond).
- Memory continuity: voice session retained to the bank (check Hindsight
  UI or recall in a fresh session); Postgres backup rows written.
- Model switch: config flip opus-4.8 ↔ fable-5 works without code changes.
- The whole suite green + a written build report per house style.

## Constraints recap (the never-list)

- `E:\git\rowan-voice` — read-only donor. `E:\Rowan`, `Documents\Rowan` —
  do not enter at all. `E:\git\Hindsight` — never touch, not even listing.
- Never disturb Rowan's scheduled tasks, ports, DBs, or VRAM allocations.
- No API keys for the brain — subscription only (that's the product).
- Chatterbox voice choice belongs to Jess. Stop and ask.
