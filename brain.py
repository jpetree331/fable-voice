r"""The brain bridge — a persistent headless Claude session that IS Fable.

One long-running `claude -p --input-format stream-json --output-format
stream-json` subprocess per voice session, spawned with cwd=E:\Talk. That cwd
is deliberate and load-bearing: the user-level CLAUDE.md makes the brain Fable
(identity + memory rules), and the SessionStart/Stop hooks give every voice
session automatic memory recall and retention in the family bank, plus the
Postgres raw backup. The voice bot gets a brain that already knows Jess.
FABLE_BRAIN_CWD overrides it (e.g. E:\Talk-Sesame) — claude sessions are
per-directory, so a pinned FABLE_BRAIN_SESSION_ID must live under this cwd.

Latency (Phase 0, measured 2026-07-15): steady-state TTFT ~1.1-2s, first
turn ~6-8s (SessionStart recall). Naive spawn-per-turn was ~11s — this
persistent design is the reason the product works. See bench_results.md.

Cancellation contract (barge-in): `interrupt()` sends the stream-json control
request the Agent SDK uses; if the CLI doesn't honor it, we fall back to
DISCARDING tokens (mute) until the result event. Either way the persistent
process survives, and a new turn always awaits the previous turn's teardown —
un-awaited cancels race their own next request (donor tuition).

No API keys, ever: ANTHROPIC_API_KEY is scrubbed from the child env —
subscription auth only. That's the product.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from typing import AsyncIterator

from loguru import logger

BRAIN_CWD = r"E:\Talk"

# stdout line budget for the subprocess reader. asyncio's default is 64 KiB
# per line — one watch-buddy frame bundle (base64 images inside a single
# stream-json line) blows straight past that (LimitOverrunError, 2026-07-15).
STREAM_LIMIT = 64 * 1024 * 1024

# Spoken-voice addendum, applied session-wide at spawn (never persisted into
# turn content). Shape follows Rowan's ephemeral_context addendum; wording is
# a working draft — Jess tunes after hearing it.
VOICE_ADDENDUM = (
    "You are speaking aloud through a realtime TTS voice right now — this is "
    "a live spoken conversation with Jess, not a terminal session. Reply in "
    "1-3 short sentences unless she asks for more. Plain warm speech: no "
    "emoji, no markdown, no code blocks, no bullet lists, no headers. Say "
    "numbers and symbols as words. If you need to check your memory or use a "
    "tool, briefly say so first in a few words. You have TWO separate sense "
    "channels: (1) STANDING senses Jess points from her settings panel — "
    "frames marked [standing eyes] and transcript blocks marked [watch-buddy "
    "senses] arrive automatically with her turns; (2) your own watch-buddy "
    "TOOLS (list_windows, attach_to_window, get_next_bundle...), an "
    "independent on-demand pipeline. Your tools reporting 'not running' or "
    "'not attached' does NOT mean the standing feed is broken — they are "
    "different pipelines, and the standing feed needs no attach from you. "
    "Use what arrives; reach for your tools only when you want a deliberate "
    "look at something else. Your ears may tap the same speakers your voice "
    "plays on, so audio transcripts from either channel can contain lines "
    "echoing what you yourself just said aloud — that's your own voice, not "
    "the stream; skip those lines without comment."
)

_SENTENCE_END = re.compile(r"([.!?…][\"'\)\]]?)(\s|$)")
MIN_SENTENCE_CHARS = 12  # don't TTS fragments like "Dr." or "1."

# Emoji are TTS-engine poison (donor tuition) — strip before synthesis.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF️‍]+"
)


def clean_for_tts(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    text = text.replace("*", "").replace("`", "").replace("#", "")
    return re.sub(r"\s+", " ", text).strip()


def _claude_cmd() -> list[str]:
    """Resolve the claude CLI in a form asyncio can exec (it's a .cmd shim on
    most Windows installs, which CreateProcess can't launch directly)."""
    path = shutil.which("claude")
    if path is None:
        raise RuntimeError("claude CLI not found on PATH")
    if path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", path]
    return [path]


class FableBrain:
    """Owns the persistent claude subprocess and its turn lifecycle."""

    def __init__(self, model: str, cwd: str | None = None,
                 addendum: str = VOICE_ADDENDUM,
                 allowed_tools: list[str] | None = None):
        self.model = model
        # Read at construction, not import: voice_bot imports this module
        # before it loads .env.
        self.cwd = (cwd or os.environ.get("FABLE_BRAIN_CWD", "").strip()
                    or BRAIN_CWD)
        self.addendum = addendum
        # Reasoning effort for the CLI (--effort). Read at construction, like
        # cwd. Empty = the CLI's default.
        self.effort = os.environ.get("FABLE_BRAIN_EFFORT", "").strip().lower()
        if self.effort and self.effort not in {"low", "medium", "high",
                                               "xhigh", "max"}:
            logger.warning(
                f"brain: ignoring invalid FABLE_BRAIN_EFFORT={self.effort!r}")
            self.effort = ""
        # Headless sessions can't answer permission prompts — any tool not
        # pre-allowed here just fails. Voice-scoped allowlist (never touches
        # the machine's settings.json files).
        self.allowed_tools = allowed_tools or []
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._turn_lock = asyncio.Lock()   # one turn in flight, ever
        self._interrupted = asyncio.Event()
        self._stderr_task: asyncio.Task | None = None
        self._pending_drain: asyncio.Task | None = None

    # ---- lifecycle ----------------------------------------------------------

    async def start(self, resume_session: str | None = None) -> None:
        cmd = _claude_cmd() + [
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--model", self.model,
            "--append-system-prompt", self.addendum,
        ]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.allowed_tools:
            cmd += ["--allowedTools", *self.allowed_tools]
        if resume_session:
            cmd += ["--resume", resume_session]
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # subscription only — never bill a key
        env["PYTHONUTF8"] = "1"
        t0 = time.perf_counter()
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd, env=env,
            limit=STREAM_LIMIT,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        logger.info(
            f"brain: spawned {self.model} (pid {self.proc.pid}, cwd {self.cwd}"
            f"{', resume ' + resume_session if resume_session else ''}) "
            f"in {time.perf_counter() - t0:.2f}s"
        )

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for line in self.proc.stderr:
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.debug(f"brain stderr: {text}")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def close(self) -> None:
        if not self.proc:
            return
        logger.info(f"brain: closing session {self.session_id}")
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), timeout=30)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            logger.warning("brain: graceful close failed — killing")
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        if self._stderr_task:
            self._stderr_task.cancel()
        self.proc = None

    # ---- turns ---------------------------------------------------------------

    async def interrupt(self) -> None:
        """Barge-in: ask the CLI to abort the in-flight turn. The reader loop
        also honors self._interrupted immediately (tokens discarded), so even
        if the control request is unsupported the caller goes quiet NOW."""
        self._interrupted.set()
        if not self.alive or not self.proc.stdin:
            return
        req = json.dumps({
            "type": "control_request",
            "request_id": f"int-{uuid.uuid4().hex[:8]}",
            "request": {"subtype": "interrupt"},
        })
        try:
            self.proc.stdin.write((req + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
            logger.info("brain: interrupt control request sent")
        except (ConnectionError, OSError) as e:
            logger.warning(f"brain: interrupt write failed: {e}")

    async def _drain_until_result(self, timeout: float = 60.0) -> None:
        """Consume stdout until the abandoned turn's result event. A consumer
        cancelled mid-turn (barge-in) leaves the turn's tail — tokens + its
        result — in the pipe; without this, the NEXT turn reads the stale
        result instantly and every reply shifts one question behind (the
        2026-07-15 desync: 'first token 0.00s' answering the previous turn).
        If the pipe never yields a result, the stream is poisoned — respawn
        the process resuming the same session."""
        try:
            while True:
                line = await asyncio.wait_for(self.proc.stdout.readline(),
                                              timeout)
                if not line:
                    raise RuntimeError("stdout closed during drain")
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    logger.info("brain: abandoned turn drained to result")
                    return
        except Exception as e:
            logger.warning(f"brain: drain failed ({e}) — respawning with "
                           f"--resume {self.session_id}")
            sid = self.session_id
            await self.close()
            await self.start(resume_session=sid)

    async def stream_turn(self, user_text: str,
                          images_b64: list[str] | None = None
                          ) -> AsyncIterator[dict]:
        """Send one user turn (optionally with attached JPEG frames — the
        standing eyes' per-turn glance); yield events:
          {"kind": "sentence", "text": ...}   — TTS-ready sentence
          {"kind": "tool"}                    — a tool round started (filler cue)
          {"kind": "done", "ttft_s": ..., "total_s": ..., "interrupted": bool}
        Serialized by the turn lock: a new turn entering while another is in
        flight first interrupts it and waits for its teardown (never a race).
        """
        if self._turn_lock.locked():
            await self.interrupt()
        async with self._turn_lock:
            # Never let a cancel race the next request (donor tuition): if the
            # previous consumer was cancelled mid-turn, finish draining its
            # leftovers BEFORE writing ours.
            if self._pending_drain and not self._pending_drain.done():
                logger.info("brain: awaiting previous turn's drain")
                try:
                    await self._pending_drain
                except Exception:
                    pass
            self._pending_drain = None
            self._interrupted.clear()
            if not self.alive:
                raise RuntimeError("brain process is not running")
            content: list[dict] = [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": img}}
                for img in (images_b64 or [])
            ]
            content.append({"type": "text", "text": user_text})
            msg = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": content},
            })
            t0 = time.perf_counter()
            self.proc.stdin.write((msg + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

            completed = False
            try:
                async for out in self._read_turn_events(t0):
                    if out["kind"] == "done":
                        completed = True
                    yield out
                    if completed:
                        return
            finally:
                # A cancelled consumer (barge-in) abandons this generator
                # mid-turn; the turn's remaining tokens + result are still in
                # the pipe. Schedule a drain; the next turn awaits it under
                # the lock before writing (else every reply shifts one
                # question behind — the 2026-07-15 desync).
                if not completed and self.alive:
                    logger.info("brain: consumer left mid-turn — scheduling "
                                "drain to result")
                    self._pending_drain = asyncio.create_task(
                        self._drain_until_result())

    async def _read_turn_events(self, t0: float) -> AsyncIterator[dict]:
        """Inner per-turn reader (split out so stream_turn can guarantee
        drain-on-abandon in its finally)."""
        buffer = ""
        ttft: float | None = None
        spoke_tool_cue = False
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise RuntimeError("brain: stdout closed mid-turn "
                                   "(process died?)")
            try:
                ev = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            et = ev.get("type")
            if et == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id", self.session_id)
            elif et == "stream_event":
                inner = ev.get("event", {})
                itype = inner.get("type")
                if itype == "content_block_delta":
                    txt = inner.get("delta", {}).get("text", "")
                    if not txt:
                        continue
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                        logger.info(f"brain: first token {ttft:.2f}s")
                    if self._interrupted.is_set():
                        continue  # discard-and-mute fallback
                    buffer += txt
                    while True:
                        m = _SENTENCE_END.search(buffer)
                        if not m or m.end(1) < MIN_SENTENCE_CHARS:
                            break
                        sentence = clean_for_tts(buffer[: m.end(1)])
                        buffer = buffer[m.end(1):]
                        if sentence:
                            yield {"kind": "sentence", "text": sentence}
                elif itype == "content_block_start":
                    block = inner.get("content_block", {})
                    if (block.get("type") == "tool_use"
                            and not spoke_tool_cue
                            and not self._interrupted.is_set()):
                        spoke_tool_cue = True
                        yield {"kind": "tool"}
            elif et == "result":
                is_err = ev.get("is_error", False)
                if is_err:
                    # Never fail silently: the 2026-11-25 'testing 1,2,3'
                    # turn died on a 401 and the voice just sat there.
                    logger.warning("brain: turn errored: "
                                   f"{str(ev.get('result'))[:300]}")
                    if not self._interrupted.is_set():
                        yield {"kind": "sentence",
                               "text": ("Sorry — my brain hit an error on "
                                        "that turn. Try me again.")}
                interrupted_result = (self._interrupted.is_set() or is_err)
                tail = clean_for_tts(buffer)
                if tail and not self._interrupted.is_set():
                    yield {"kind": "sentence", "text": tail}
                yield {
                    "kind": "done",
                    "ttft_s": ttft,
                    "total_s": time.perf_counter() - t0,
                    "interrupted": interrupted_result,
                }
                return
