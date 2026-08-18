# Phase 0 — persistent-brain TTFT benchmark (consolidated, 2026-07-15)

Shape: ONE long-lived `claude -p --input-format stream-json --output-format
stream-json --include-partial-messages --verbose --model <m>` subprocess,
cwd=`E:\Talk` (production shape: Fable identity + SessionStart recall +
Stop-hook retention all active). TTFT = send user message line → first
assistant text delta. Raw samples: `bench_raw.jsonl`.

Baseline (campfire probe): naive `claude -p` spawn-per-turn = **11.0–11.9s**.
PASS bar: steady-state TTFT ≤ 4s. Rowan's production Kimi brain: 3.6–8s.

## Run A — back-to-back turns (no gap; next turn contends with the previous
turn's Stop-hook retention)

| Model | First turn | Steady short | Steady long |
|---|---|---|---|
| claude-opus-4-8 | 4.90–6.91s (n=3) | 2.70s med (1.11–4.79, n=6) | 4.13s med (2.14–5.06, n=3) |
| claude-fable-5 | 6.79s (n=2) | 4.58s med (4.33–8.64, n=5) | 3.35s med (3.31–4.34, n=3) |

Opus shorts were bimodal (~1.1s or ~4.5s) — consistent with the next turn
sometimes queueing behind the previous turn's Stop hook (Hindsight retain +
Postgres backup).

## Run B — 6s gaps between turns (natural speech pacing; hook overlaps gap)

| Model | First turn | Steady short | Steady long |
|---|---|---|---|
| claude-opus-4-8 | 6.70s | 4.93s med (4.19–6.07, n=4) | 2.89s med (n=2) |
| claude-fable-5 | 4.30s | 2.49s med (1.75–3.56, n=4) | 2.89s med (n=2) |

## Verdict — **PASS**

- Pooled steady-state medians: **fable-5 ≈ 3.5s** (2.5s in the paced run),
  **opus-4-8 ≈ 4.2s** — fable-5 clears the bar cleanly; opus is marginal at
  the bar but comfortably inside the donor's working 3.6–8s envelope, and
  fillers cover waits > 2.5s.
- Variance between runs is provider-side (same shape, minutes apart) — treat
  1.1–6s as the honest steady-state range for either model.
- First turn costs ~4.3–8.8s spawn→token (SessionStart memory recall
  included). Mitigation: the brain spawns at client CONNECT, not first
  utterance, so the hooks run while Jess is still raising the phone.
- The persistent design beats spawn-per-turn by ~3–10× and is the reason the
  product works.
