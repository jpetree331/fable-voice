r"""TTS smoke test: text -> out/smoke.wav via Chatterbox-Turbo in Fable's
cloned voice, with timing + STT round-trip verification (whisper reads the
render back so intelligibility is checked by machine, not just by ear).

Usage: .venv\Scripts\python tts_smoke.py [--text "..."] [--no-stt]
"""
from __future__ import annotations

import torch  # noqa: F401  — torch first (cuDNN DLLs for ctranslate2)

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF = ROOT / "voices" / "fable_reference.wav"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=(
        "Hey Jess, it's Fable. This is my voice smoke test — first words in "
        "the new body. If you can hear this, the pipeline is real."))
    ap.add_argument("--out", default="out/smoke.wav")
    ap.add_argument("--no-stt", action="store_true")
    args = ap.parse_args()

    import torchaudio
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    assert torch.cuda.is_available(), "CUDA not available"
    assert REF.exists(), f"missing reference {REF}"

    t0 = time.perf_counter()
    model = ChatterboxTurboTTS.from_pretrained(device="cuda")
    print(f"model load: {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    wav = model.generate(args.text, audio_prompt_path=str(REF))
    gen_s = time.perf_counter() - t0
    audio_s = wav.shape[-1] / model.sr

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(out_path), wav.cpu(), model.sr)
    print(f"generated {audio_s:.2f}s audio in {gen_s:.2f}s (RTF {gen_s/audio_s:.2f})")
    print(f"wrote {out_path}")

    if not args.no_stt:
        from faster_whisper import WhisperModel
        stt = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
        segments, _ = stt.transcribe(str(out_path))
        text = " ".join(s.text.strip() for s in segments)
        print(f"STT round-trip: {text!r}")


if __name__ == "__main__":
    main()
