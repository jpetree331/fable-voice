r"""Pre-generate filler acknowledgment clips in Fable's cloned voice.

Generates TAKES sampling takes per phrase — Chatterbox's randomness gives each
a different delivery, so the fillers don't sound canned. Rerun whenever the
voice reference changes. Output: fillers/<phrase>_t<n>.wav

Phrases follow the donor's proven set (short waits stay silent; "checking_notes"
is reserved by voice_bot.py for tool rounds — memory recall, mostly).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF = ROOT / "voices" / "fable_reference.wav"
OUT_DIR = ROOT / "fillers"
TAKES = 3

FILLERS = {
    "mm": "Mm.",
    "mmhm": "Mmhm.",
    "hmm": "Hmm...",
    "one_sec": "One sec.",
    "sec": "Uh — gimme a sec.",
    "let_me_think": "Let me think.",
    "checking_notes": "Hang on — checking my memory.",
}


def main() -> None:
    import torch
    import torchaudio
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    assert torch.cuda.is_available(), "CUDA not available"
    assert REF.exists(), f"missing reference {REF}"
    model = ChatterboxTurboTTS.from_pretrained(device="cuda")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.wav"):
        old.unlink()

    for name, text in FILLERS.items():
        for take in range(1, TAKES + 1):
            wav = model.generate(text, audio_prompt_path=str(REF))
            path = OUT_DIR / f"{name}_t{take}.wav"
            torchaudio.save(str(path), wav.cpu(), model.sr)
            print(f"{path.name}: {text!r} ({wav.shape[-1] / model.sr:.2f}s)")


if __name__ == "__main__":
    main()
