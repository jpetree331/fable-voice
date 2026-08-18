r"""Standing senses — Jess-directed eyes and ears for the voice brain.

The settings gear on the PTT page drives this: SHE points the eyes at a
window or screen and the ears at an output device, and the brain gets them
hands-free — no tool calls, no attach latency:

  - EYES: a glance per turn. When Jess speaks, ONE fresh frame of the chosen
    target is grabbed and attached to the brain turn as an image. No
    background capture loop, no per-frame token cost.
  - EARS: a standing EarStream (watch-buddy's own module) on the chosen
    output device, writing the shared transcripts/*.log — which the per-turn
    senses injection (senses.py) already delivers as text.

NOTE (design honesty): this module IMPORTS watch-buddy code (capture /
window_finder / ears), deepening the coupling beyond the original
file-tap-only design — Jess asked for it (settings gear, 2026-07-15).
Watch-buddy stays the source of truth for those modules; nothing here
modifies its state, config, or db. If watch-buddy's ears also run elsewhere
(Claude Desktop), transcript lines double — pick one owner at a time.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import threading
from pathlib import Path

from loguru import logger

WATCH_BUDDY_DIR = Path(os.environ.get(
    "FABLE_WATCH_BUDDY_DIR",
    r"E:\git\1-Retired Projects\ClaudeWatch\watch_buddy"))
if str(WATCH_BUDDY_DIR) not in sys.path:
    sys.path.insert(0, str(WATCH_BUDDY_DIR))

MAX_FRAME_W = 1280
JPEG_QUALITY = 80


class StandingSenses:
    """Owns the Jess-directed eye target and ear stream."""

    def __init__(self):
        self._lock = threading.Lock()
        self.eyes_target: str | None = None   # "screen:1" | window substring
        self.ears_device: str | None = None   # None = system default output
        self.ears_on: bool = False
        self._ears = None

    # ---- discovery -----------------------------------------------------------

    def list_targets(self) -> dict:
        screens: list[str] = []
        windows: list[str] = []
        try:
            import mss
            with mss.mss() as sct:
                for i, mon in enumerate(sct.monitors[1:], start=1):
                    screens.append(f"screen:{i} ({mon['width']}x{mon['height']})")
        except Exception as e:
            logger.warning(f"standing eyes: screen enumeration failed: {e}")
        try:
            from window_finder import list_windows
            windows = list_windows()
        except Exception as e:
            logger.warning(f"standing eyes: window enumeration failed: {e}")
        return {"screens": screens, "windows": windows}

    def list_devices(self) -> list[str]:
        try:
            from ears import list_loopback_devices
            return list_loopback_devices()
        except Exception as e:
            logger.warning(f"standing ears: device enumeration failed: {e}")
            return []

    # ---- eyes ------------------------------------------------------------------

    def set_eyes(self, target: str | None) -> str:
        with self._lock:
            self.eyes_target = (target or "").strip() or None
        if self.eyes_target is None:
            return "eyes off"
        # Validate now so the UI gets an honest answer.
        region, label = self._resolve_region(self.eyes_target)
        return f"eyes on {label}" if region else (
            f"target '{self.eyes_target}' not found right now — will retry "
            "each turn")

    def _resolve_region(self, target: str) -> tuple[dict | None, str]:
        if target.startswith("screen:"):
            try:
                import mss
                n = int(target.split(":", 1)[1].split()[0])
                with mss.mss() as sct:
                    mon = sct.monitors[n]  # 1-based; 0 is the combined screen
                return ({"x": mon["left"], "y": mon["top"],
                         "width": mon["width"], "height": mon["height"]},
                        f"screen {n}")
            except Exception:
                return None, target
        try:
            from window_finder import get_window_region
            region = get_window_region(target)
            if region:
                return region, region.get("window_title", target)
        except Exception as e:
            logger.warning(f"standing eyes: window lookup failed: {e}")
        return None, target

    def glance_jpeg(self) -> tuple[bytes | None, str]:
        """One fresh frame of the current target (region re-resolved per call
        — windows move). Returns (jpeg bytes | None, label)."""
        with self._lock:
            target = self.eyes_target
        if not target:
            return None, ""
        region, label = self._resolve_region(target)
        if not region:
            return None, label
        try:
            import mss
            from PIL import Image
            with mss.mss() as sct:
                shot = sct.grab({"top": region["y"], "left": region["x"],
                                 "width": region["width"],
                                 "height": region["height"]})
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if img.width > MAX_FRAME_W:
                img = img.resize(
                    (MAX_FRAME_W, int(img.height * MAX_FRAME_W / img.width)))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=JPEG_QUALITY)
            return buf.getvalue(), label
        except Exception as e:
            logger.warning(f"standing eyes: glance failed: {e}")
            return None, label

    # ---- ears ------------------------------------------------------------------

    def set_ears(self, device: str | None, enabled: bool) -> str:
        with self._lock:
            self.ears_device = (device or "").strip() or None
            self.ears_on = bool(enabled)
            if self._ears is not None:
                try:
                    self._ears.stop()
                except Exception:
                    pass
                self._ears = None
            if not self.ears_on:
                return "ears off"
            try:
                from ears import EarStream
                cfg = {"enabled": True, "log_to_file": True}
                if self.ears_device:
                    cfg["device"] = self.ears_device
                self._ears = EarStream(cfg).start()
                return self._ears.status
            except Exception as e:
                logger.warning(f"standing ears failed to start: {e}")
                self.ears_on = False
                return f"ears failed: {e}"

    def stop(self) -> None:
        self.set_ears(None, False)

    # ---- status ----------------------------------------------------------------

    def status(self) -> dict:
        return {
            "eyes_target": self.eyes_target,
            "ears_on": self.ears_on,
            "ears_device": self.ears_device or "",
            "ears_status": (self._ears.status if self._ears else "off"),
        }


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
