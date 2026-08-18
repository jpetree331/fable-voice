r"""Regression test for the 2026-07-15 turn-desync bug.

The failure: cancelling the consumer task mid-turn (what barge-in does)
abandoned the turn's remaining stdout events; the NEXT turn read the stale
result instantly ('first token 0.00s') and every reply shifted one question
behind. The original barge-in test asked the same question every round, so
off-by-one answers looked correct — this test asks DIFFERENT questions and
checks the answer content matches the question that was actually asked.

Run: .venv\Scripts\python test_brain_desync.py
"""
from __future__ import annotations

import asyncio
import time

from brain import FableBrain


async def consume(brain: FableBrain, text: str, out: list):
    async for ev in brain.stream_turn(text):
        out.append(ev)


async def main() -> None:
    brain = FableBrain(model="claude-opus-4-8")
    await brain.start()

    # Turn 1: start a long reply, then CANCEL the consumer task mid-stream —
    # exactly what FableBrainService._cancel_tasks does on barge-in.
    events1: list = []
    t1 = asyncio.create_task(consume(
        brain, "Turn ONE of a desync regression test: describe the moon in "
               "six long sentences, slowly.", events1))
    while not any(e["kind"] == "sentence" for e in events1):
        await asyncio.sleep(0.05)
        if t1.done():
            break
    await brain.interrupt()
    t1.cancel()
    try:
        await t1
    except asyncio.CancelledError:
        pass
    print(f"turn 1: cancelled after {sum(e['kind'] == 'sentence' for e in events1)} sentence(s)")

    # Turn 2: a DIFFERENT question. Pre-fix, this read turn 1's stale result
    # (ttft None/0.00s, no sentences) and its own answer went to turn 3.
    events2: list = []
    t0 = time.perf_counter()
    await consume(
        brain, "Turn TWO of the regression test: what is two plus two? "
               "Answer with just the number word.", events2)
    took = time.perf_counter() - t0
    sentences = [e["text"] for e in events2 if e["kind"] == "sentence"]
    done = [e for e in events2 if e["kind"] == "done"][-1]
    reply = " ".join(sentences).lower()
    print(f"turn 2 reply ({took:.2f}s, ttft {done['ttft_s']}): {reply!r}")

    ok = True
    if "four" not in reply:
        print("FAIL: turn 2 reply does not answer turn 2's question")
        ok = False
    if done["ttft_s"] is None or done["ttft_s"] < 0.05:
        print(f"FAIL: suspicious ttft {done['ttft_s']} — stale-result symptom")
        ok = False
    if "moon" in reply:
        print("FAIL: turn 1 content leaked into turn 2 (classic desync)")
        ok = False

    # Turn 3: no interruption anywhere — plain sanity follow-up.
    events3: list = []
    await consume(brain,
                  "Turn THREE: what color is a stop sign? One word.", events3)
    reply3 = " ".join(e["text"] for e in events3 if e["kind"] == "sentence").lower()
    print(f"turn 3 reply: {reply3!r}")
    if "red" not in reply3:
        print("FAIL: turn 3 reply does not answer turn 3's question")
        ok = False

    await brain.close()
    print("\nDESYNC REGRESSION: " + ("PASS" if ok else "FAIL"))


if __name__ == "__main__":
    asyncio.run(main())
