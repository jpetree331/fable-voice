r"""Shared pipeline services + model preload (port of the donor's proven M3
layer: E:\git\rowan-voice\echo_loop.py, studied read-only).

Importing this module loads Chatterbox-Turbo and faster-whisper once per
process, with VRAM accounting logged (Phase 3.5 wants honest numbers).
"""
# IMPORTANT: torch first — its bundled cuDNN 9 / cuBLAS DLLs must be loaded
# before ctranslate2 (faster-whisper) initializes CUDA. Donor tuition.
import torch

import asyncio
import os
import subprocess
from pathlib import Path

import numpy as np
from loguru import logger

from chatterbox.tts_turbo import ChatterboxTurboTTS
from pipecat.frames.frames import Frame, VADUserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.services.whisper.stt import WhisperSTTService

ROOT = Path(__file__).resolve().parent
# Fable's voice: Jess-supplied reference clip (2026-07-15), publicly available
# free voice for non-commercial use. The cloned voice IS this clip.
REF_VOICE = str(ROOT / "voices" / "fable_reference.wav")


def gpu_used_mb() -> int:
    """Total board VRAM in use (WDDM hides per-process numbers)."""
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    return int(out.stdout.strip().splitlines()[0])


class SharedWhisperSTTService(WhisperSTTService):
    """One loaded whisper model shared across connections (a reconnect must
    not cost a ~16s model reload)."""

    _shared_model = None

    def _load(self):
        if SharedWhisperSTTService._shared_model is None:
            super()._load()
            SharedWhisperSTTService._shared_model = self._model
        else:
            self._model = SharedWhisperSTTService._shared_model


class ChatterboxTurboTTSService(TTSService):
    """Chatterbox-Turbo TTS in Fable's cloned voice."""

    def __init__(self, chatterbox: ChatterboxTurboTTS, ref_voice: str, **kwargs):
        super().__init__(sample_rate=chatterbox.sr, **kwargs)
        self._chatterbox = chatterbox
        self._ref_voice = ref_voice
        # Store-mode settings must have real values (None = unsupported) or
        # pipecat logs a validate_complete ERROR per session.
        self._settings.model = "chatterbox-turbo"
        self._settings.voice = Path(ref_voice).stem
        self._settings.language = None

    def can_generate_metrics(self) -> bool:
        return True

    def _generate_pcm16(self, text: str) -> bytes:
        with torch.inference_mode():
            wav = self._chatterbox.generate(text, audio_prompt_path=self._ref_voice)
        pcm = wav.squeeze(0).clamp(-1.0, 1.0).cpu().numpy()
        return (pcm * 32767).astype(np.int16).tobytes()

    async def run_tts(self, text: str, context_id: str):
        logger.debug(f"ChatterboxTurbo TTS [{text}]")
        await self.start_tts_usage_metrics(text)

        # Chatterbox generation is a blocking CUDA call — not cancellable
        # mid-utterance. On barge-in the generator closes at the next yield, so
        # a finished-but-unwanted utterance is discarded unplayed.
        async def audio_iter():
            pcm = await asyncio.to_thread(self._generate_pcm16, text)
            # 100ms chunks give the pipeline frequent cancellation points.
            CHUNK = int(self._chatterbox.sr * 0.1) * 2
            for i in range(0, len(pcm), CHUNK):
                yield pcm[i:i + CHUNK]

        async for frame in self._stream_audio_frames_from_iterator(
            audio_iter(), in_sample_rate=self._chatterbox.sr, context_id=context_id
        ):
            await self.stop_ttfb_metrics()
            yield frame


class BargeIn(FrameProcessor):
    """Broadcasts an InterruptionFrame when the user starts speaking. Needed
    because this pipeline has no LLM context aggregator (pipecat 1.4 fires the
    broadcast from there normally) — donor M3 root cause."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            logger.info("barge-in: VAD user-speech start -> broadcasting interruption")
            await self.broadcast_interruption()
        await self.push_frame(frame, direction)


# ---- Model preload (once per process) with VRAM accounting -----------------
_LAZY = os.environ.get("FABLE_SKIP_PRELOAD", "").strip().lower() == "true"

CHATTERBOX: ChatterboxTurboTTS | None = None

if not _LAZY:
    vram0 = gpu_used_mb()
    logger.info(f"VRAM baseline: {vram0} MiB")

    CHATTERBOX = ChatterboxTurboTTS.from_pretrained(device="cuda")
    vram1 = gpu_used_mb()
    logger.info(f"VRAM after Chatterbox-Turbo: {vram1} MiB (+{vram1 - vram0})")

    _warm = SharedWhisperSTTService(
        model=os.environ.get("FABLE_WHISPER_MODEL", "large-v3-turbo"),
        device="cuda", compute_type="float16")
    _warm._load()
    vram2 = gpu_used_mb()
    logger.info(f"VRAM after whisper: {vram2} MiB (+{vram2 - vram1})")
    logger.info(f"COMBINED models delta: {vram2 - vram0} MiB | board total: {vram2} MiB")
