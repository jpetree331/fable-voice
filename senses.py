r"""Watch-buddy senses — READ-ONLY file access to watch-buddy's world.

Reads the observations SQLite db and the day's transcript logs so the brain
can answer "what's happening on stream?" by voice. Deliberately loose
coupling: no watch-buddy code imported, no MCP, never a write. The db is
opened with SQLite's read-only URI mode so even a bug here cannot touch it.

Data shapes (recon 2026-07-15):
  watch_sessions(id, stream_title, started_at, ended_at)
  watch_events(id, session_id, timestamp, summary, tags)   -- UTC ISO
  transcripts/YYYY-MM-DD.log lines: "[HH:MM:SS] text"      -- local time
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

DEFAULT_DIR = Path(r"E:\git\1-Retired Projects\ClaudeWatch\watch_buddy")

MAX_OBSERVATIONS = 5
MAX_TRANSCRIPT_CHARS = 1500
FRESH_WINDOW = timedelta(hours=2)   # older sensory data isn't "now"

# Filler clips the bot plays while thinking — their loopback echo is noise.
_FILLER_TEXTS = {"mm", "mmhm", "hmm", "one sec", "uh gimme a sec",
                 "let me think", "hang on checking my memory"}

_TS_PREFIX = re.compile(r"^\[\d\d:\d\d:\d\d\]\s*")
_WORDS = re.compile(r"[a-z0-9']+")


def _norm(text: str) -> str:
    return " ".join(_WORDS.findall(text.lower()))


def is_bot_echo(line: str, recent_bot_speech: list[str]) -> bool:
    """True when a transcript line is the bot's OWN voice leaking back in
    through the speaker loopback (the ears hear the same device the voice
    plays on). We know every sentence the bot spoke — a line that closely
    matches recent bot speech is echo, not the world."""
    text = _norm(_TS_PREFIX.sub("", line))
    if not text:
        return False
    if text in _FILLER_TEXTS:
        return True
    words = text.split()
    if len(words) < 4 or not recent_bot_speech:
        return False
    for said in recent_bot_speech:
        said_n = _norm(said)
        if not said_n:
            continue
        # whole-line similarity, or the line being contained in the sentence
        if difflib.SequenceMatcher(None, text, said_n).ratio() >= 0.75:
            return True
        if text in said_n or said_n in text:
            return True
    # word-coverage against everything said recently (whisper splits lines
    # differently than our sentence chunker)
    bot_words = set(w for s in recent_bot_speech for w in _WORDS.findall(s.lower()))
    coverage = sum(1 for w in words if w in bot_words) / len(words)
    return coverage >= 0.8


class WatchBuddySenses:
    """Per-voice-session senses reader with a since-last-turn cursor."""

    def __init__(self, buddy_dir: Path | str = DEFAULT_DIR):
        self.dir = Path(buddy_dir)
        self.db_path = self.dir / "watch_buddy.db"
        self.transcripts = self.dir / "transcripts"
        self._last_obs_ts: str = ""       # ISO UTC of newest observation seen
        self._last_line_no: dict[str, int] = {}  # per-day-file line cursor

    @property
    def available(self) -> bool:
        return self.db_path.exists()

    # ---- observations (SQLite, read-only) -----------------------------------

    def _read_observations(self) -> list[dict]:
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT e.timestamp, e.summary, s.stream_title "
                    "FROM watch_events e JOIN watch_sessions s ON s.id = e.session_id "
                    "ORDER BY e.id DESC LIMIT ?",
                    (MAX_OBSERVATIONS,),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.warning(f"senses: observations read failed: {e}")
            return []

    # ---- transcript (per-day log files) --------------------------------------

    def _read_new_transcript_lines(self) -> list[str]:
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.transcripts / f"{today}.log"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            logger.warning(f"senses: transcript read failed: {e}")
            return []
        start = self._last_line_no.get(today)
        if start is None:
            # First look this session: only the recent tail, not the whole day.
            start = max(0, len(lines) - 8)
        new = lines[start:len(lines)]
        self._last_line_no = {today: len(lines)}  # drop stale day cursors
        return new

    # ---- composition ----------------------------------------------------------

    def compose_block(self, recent_bot_speech: list[str] | None = None) -> str:
        """A compact senses block with only what's fresh AND new since the
        last turn; empty string when there is nothing worth saying.
        recent_bot_speech: sentences the bot spoke recently — transcript
        lines matching them are the bot's own loopback echo and are dropped.
        """
        if not self.available:
            return ""
        now_utc = datetime.utcnow()
        parts: list[str] = []

        obs = self._read_observations()
        fresh_obs = []
        for o in obs:
            try:
                ts = datetime.fromisoformat(o["timestamp"]).replace(tzinfo=None)
            except ValueError:
                continue
            if now_utc - ts <= FRESH_WINDOW and o["timestamp"] > self._last_obs_ts:
                t = o["timestamp"][11:16]
                fresh_obs.append(f"- [{t} UTC] ({o['stream_title']}) {o['summary']}")
        if fresh_obs:
            self._last_obs_ts = obs[0]["timestamp"]
            parts.append("recent observations:\n" + "\n".join(reversed(fresh_obs)))

        lines = self._read_new_transcript_lines()
        if lines and recent_bot_speech:
            kept = [l for l in lines if not is_bot_echo(l, recent_bot_speech)]
            if len(kept) != len(lines):
                logger.info(f"senses: dropped {len(lines) - len(kept)} "
                            f"self-echo transcript line(s)")
            lines = kept
        if lines:
            text = "\n".join(lines)
            if len(text) > MAX_TRANSCRIPT_CHARS:
                text = "…" + text[-MAX_TRANSCRIPT_CHARS:]
            parts.append("stream audio since your last look:\n" + text)

        if not parts:
            return ""
        return ("[watch-buddy senses — what your screen-watching eyes/ears "
                "picked up]\n" + "\n\n".join(parts))
