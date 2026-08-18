r"""Phase 0 — THE GATE: persistent-brain latency benchmark.

Spawns ONE long-lived headless claude session per model
(`claude -p --input-format stream-json --output-format stream-json
--include-partial-messages --verbose --model <m>`, cwd=E:\Talk — the
production shape: Fable identity + memory hooks active) and measures
time-to-first-assistant-token per turn, streamed in over stdin.

PASS bar (BRIEF.md): steady-state TTFT <= 4s.
Naive spawn-per-turn baseline (campfire probe): ~11s. This must beat it.

Run:  python bench_brain.py [--models claude-opus-4-8 claude-fable-5]
                            [--short-turns 5] [--long-turns 3] [--spawns 2]
Output: bench_results.md table + raw JSON lines in bench_raw.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

BRAIN_CWD = r"E:\Talk"
HERE = Path(__file__).resolve().parent

SHORT_PROMPT = (
    "Fable, this is a voice-latency benchmark turn (your future voice body's "
    "Phase 0 gate). Reply with exactly one short word, like: ok"
)
LONG_PROMPT = (
    "Fable, voice-latency benchmark, longer-reply case: in 3 to 4 plain "
    "sentences, no lists, say what you'd want your spoken voice to sound like."
)


def build_cmd(model: str) -> list[str]:
    return [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--model", model,
    ]


class BrainProc:
    """A persistent headless claude session we can feed turns."""

    def __init__(self, model: str):
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # subscription only — never bill a key
        env["PYTHONUTF8"] = "1"
        self.proc = subprocess.Popen(
            build_cmd(model),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=BRAIN_CWD, env=env, text=True, encoding="utf-8", bufsize=1,
            shell=True if os.name == "nt" else False,  # claude is a .cmd shim on Windows
        )
        self.session_id: str | None = None
        self._stderr_lines: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self._stderr_lines.append(line.rstrip())

    def send_turn(self, text: str, timeout: float = 180.0) -> dict:
        """Send one user message; return timing dict."""
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        })
        t0 = time.perf_counter()
        self.proc.stdin.write(msg + "\n")
        self.proc.stdin.flush()

        first_token_s = None
        first_text = ""
        n_events = 0
        deadline = t0 + timeout
        while True:
            if time.perf_counter() > deadline:
                raise TimeoutError(f"no result event within {timeout}s")
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    "brain process closed stdout; stderr tail: "
                    + "\n".join(self._stderr_lines[-15:])
                )
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_events += 1
            et = ev.get("type")
            if et == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id", self.session_id)
            elif et == "stream_event":
                inner = ev.get("event", {})
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta", {})
                    txt = delta.get("text", "")
                    if txt and first_token_s is None:
                        first_token_s = time.perf_counter() - t0
                    first_text += txt
            elif et == "assistant" and first_token_s is None:
                # fallback if partial events are absent
                content = ev.get("message", {}).get("content", [])
                if any(b.get("type") == "text" and b.get("text") for b in content):
                    first_token_s = time.perf_counter() - t0
                    first_text = "".join(
                        b.get("text", "") for b in content if b.get("type") == "text")
            elif et == "result":
                total_s = time.perf_counter() - t0
                return {
                    "ttft_s": first_token_s,
                    "total_s": total_s,
                    "reply_head": first_text[:80],
                    "is_error": ev.get("is_error", False),
                    "n_events": n_events,
                }

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


def fmt(vals: list[float | None]) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "—"
    if len(vals) == 1:
        return f"{vals[0]:.2f}s"
    return (f"{statistics.median(vals):.2f}s med "
            f"({min(vals):.2f}–{max(vals):.2f}, n={len(vals)})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+",
                    default=["claude-opus-4-8", "claude-fable-5"])
    ap.add_argument("--short-turns", type=int, default=5)
    ap.add_argument("--long-turns", type=int, default=3)
    ap.add_argument("--spawns", type=int, default=2,
                    help="sessions per model (first-turn samples)")
    ap.add_argument("--gap", type=float, default=0.0,
                    help="seconds between turns (simulates natural speech "
                         "pacing so Stop-hook retention overlaps the gap)")
    args = ap.parse_args()

    raw_path = HERE / "bench_raw.jsonl"
    results: dict[str, dict[str, list]] = {}
    with raw_path.open("a", encoding="utf-8") as raw:
        for model in args.models:
            r = results[model] = {"first": [], "short": [], "long": [],
                                  "spawn_s": [], "errors": []}
            for spawn_i in range(args.spawns):
                print(f"\n=== {model} — session {spawn_i + 1}/{args.spawns} ===")
                t0 = time.perf_counter()
                brain = BrainProc(model)
                try:
                    # First turn (includes session init + SessionStart hooks)
                    m = brain.send_turn(SHORT_PROMPT)
                    m["spawn_to_first_token_s"] = time.perf_counter() - t0
                    r["first"].append(m["ttft_s"])
                    r["spawn_s"].append(m["spawn_to_first_token_s"])
                    print(f"  first turn: ttft={m['ttft_s']:.2f}s "
                          f"(spawn->token {m['spawn_to_first_token_s']:.2f}s) "
                          f"reply={m['reply_head']!r}")
                    raw.write(json.dumps({"model": model, "kind": "first",
                                          "spawn": spawn_i, **m}) + "\n")
                    # Steady state, only on the first spawned session
                    if spawn_i == 0:
                        for i in range(args.short_turns):
                            time.sleep(args.gap)
                            m = brain.send_turn(SHORT_PROMPT)
                            r["short"].append(m["ttft_s"])
                            print(f"  short #{i+1}: ttft={m['ttft_s']:.2f}s "
                                  f"total={m['total_s']:.2f}s reply={m['reply_head']!r}")
                            raw.write(json.dumps({"model": model, "kind": "short",
                                                  "i": i, **m}) + "\n")
                        for i in range(args.long_turns):
                            time.sleep(args.gap)
                            m = brain.send_turn(LONG_PROMPT)
                            r["long"].append(m["ttft_s"])
                            print(f"  long  #{i+1}: ttft={m['ttft_s']:.2f}s "
                                  f"total={m['total_s']:.2f}s")
                            raw.write(json.dumps({"model": model, "kind": "long",
                                                  "i": i, **m}) + "\n")
                except Exception as e:
                    r["errors"].append(f"session {spawn_i}: {e}")
                    print(f"  ERROR: {e}", file=sys.stderr)
                finally:
                    brain.close()
                raw.flush()

    lines = ["# Phase 0 — persistent-brain TTFT benchmark",
             f"(cwd={BRAIN_CWD}, production shape, hooks active; "
             "naive spawn-per-turn baseline ~11s; PASS bar: steady-state <= 4s)",
             "",
             "| Model | First turn TTFT | Steady short TTFT | Steady long TTFT |",
             "|---|---|---|---|"]
    for model, r in results.items():
        lines.append(f"| {model} | {fmt(r['first'])} | {fmt(r['short'])} "
                     f"| {fmt(r['long'])} |")
        for err in r["errors"]:
            lines.append(f"| {model} ERROR | {err} | | |")
    steady = [v for r in results.values() for v in r["short"] + r["long"]
              if v is not None]
    verdict = ("PASS" if steady and statistics.median(steady) <= 4.0 else "FAIL")
    lines += ["", f"**Median steady-state TTFT: "
              f"{statistics.median(steady):.2f}s — {verdict}**" if steady
              else "**No steady-state samples — FAIL**"]
    out = "\n".join(lines) + "\n"
    (HERE / "bench_results.md").write_text(out, encoding="utf-8")
    print("\n" + out)


if __name__ == "__main__":
    main()
