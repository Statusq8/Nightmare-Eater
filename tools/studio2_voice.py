"""Synthesize a line pair with a versioned Studio 2 voice, using local VoxCPM2."""
import argparse
from pathlib import Path

from studio import file_hash
from world_memory import ledger, normalized, references

ROOT = Path(__file__).resolve().parents[1]


def registered_voice(db, home, label, chapter, position, language, candidate=False):
    rows = db.execute("SELECT entity FROM alias WHERE label=?", (normalized(label),)).fetchall()
    if len(rows) != 1:
        raise ValueError("Speaker must resolve to one registered entity.")
    assets = references(db, rows[0]["entity"], chapter, position, include_candidates=candidate)
    asset = assets.get("voice/" + language)
    if not asset:
        raise ValueError(f"No eligible {language} voice for {label}.")
    path = home/"library"/(asset["hash"] + asset["extension"])
    if file_hash(path) != asset["hash"]:
        raise ValueError("Voice reference hash mismatch.")
    return path


class VoxCPM:
    def __init__(self, directory):
        from voxcpm import VoxCPM as Engine

        self.engine = Engine.from_pretrained(str(directory.resolve(strict=True)), load_denoiser=False,
                                             local_files_only=True, optimize=False)

    def line(self, text, reference, target, acting="restrained, articulate, natural"):
        import numpy as np
        import soundfile as sf
        import torch

        torch.manual_seed(13013)
        audio = self.engine.generate(text=f"({acting}){text}", reference_wav_path=str(reference),
                                     max_len=320, inference_timesteps=10)
        if not np.isfinite(audio).all() or not np.any(np.abs(audio) > .005):
            raise ValueError("Invalid synthesized waveform.")
        with target.open("xb") as sink:
            sf.write(sink, audio, self.engine.tts_model.sample_rate, format="WAV", subtype="PCM_16")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("speaker")
    parser.add_argument("chapter", type=int)
    parser.add_argument("position", type=int)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ru", required=True)
    parser.add_argument("--en", required=True)
    parser.add_argument("--allow-candidate", action="store_true")
    parser.add_argument("--home", type=Path, default=ROOT/"output/studio2")
    parser.add_argument("--model", type=Path, default=ROOT/".studio-local/VoxCPM2")
    args = parser.parse_args()
    with ledger(args.home/"world.sqlite", readonly=True) as db:
        voices = {lang: registered_voice(db, args.home.resolve(), args.speaker, args.chapter, args.position,
                                         lang, args.allow_candidate) for lang in ("ru", "en")}
    args.output.mkdir(parents=True, exist_ok=True)
    synth = VoxCPM(args.model)
    for lang, ref in voices.items():
        synth.line(getattr(args, lang), ref, args.output/(lang+".wav"))
        print(f"{lang}: {ref.stem}", flush=True)


if __name__ == "__main__":
    main()

