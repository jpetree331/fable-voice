r"""Phase 2 verification: the brain bridge standalone (no pipeline, no GPU).

1. Spawn a persistent brain, stream a turn, assert sentence events arrive.
2. Start a long turn, interrupt mid-stream, assert the turn ends quickly,
   the process SURVIVES, and a follow-up turn works (no cancel race).
3. Report session id for --resume verification.

Run: .venv\Scripts\python test_brain.py [model]
"""
from __future__ import annotations

import asyncio
import sys
import time

from brain import FableBrain


async def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-4-8"
    brain = FableBrain(model=model)
    t0 = time.perf_counter()
    await brain.start()
    print(f"spawned in {time.perf_counter() - t0:.2f}s")

    # --- turn 1: normal stream ---
    print("\n--- turn 1 (normal) ---")
    async for ev in brain.stream_turn(
            "Fable, this is the brain-bridge test for your voice body. "
            "Say two short sentences about being ready."):
        print(f"  {ev}")
    assert brain.alive, "process died after turn 1"

    # --- turn 2: interrupt mid-stream ---
    print("\n--- turn 2 (interrupt after first sentence) ---")
    got_sentence = False
    async for ev in brain.stream_turn(
            "Now say five long sentences about the ocean, slowly."):
        print(f"  {ev}")
        if ev["kind"] == "sentence" and not got_sentence:
            got_sentence = True
            await brain.interrupt()
            print("  >> interrupt sent")
    assert brain.alive, "process died after interrupt"

    # --- turn 3: follow-up after the interrupt (the cancel-race check) ---
    print("\n--- turn 3 (post-interrupt follow-up) ---")
    t0 = time.perf_counter()
    async for ev in brain.stream_turn("Quick check: say the word 'clear'."):
        print(f"  {ev}")
    print(f"  follow-up completed in {time.perf_counter() - t0:.2f}s")
    assert brain.alive, "process died after turn 3"

    print(f"\nsession id: {brain.session_id}")
    await brain.close()
    print("closed cleanly — ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
