"""Protect the approved drawing and screen candidates without changing source assets."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "lore/ART_STYLE_LOCK.json"
DIGEST = "b9322206ba0ae8aea29f225bacd5a0474ae47f842eb6ec0c355f6e60a24f7938"

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def profile():
    raw = PROFILE.read_bytes().replace(b"\r\n", b"\n")
    if sha256(raw) != DIGEST:
        raise ValueError("ART_STYLE_LOCK integrity check failed")
    return json.loads(raw)

def require_backend(name):
    if name != profile()["backend"]:
        raise ValueError(f"ART_STYLE_LOCK prevents generation with {name}")

def restore_approved(shot, destination):
    row = next((row for row in profile()["baseline"] if row["shot"] == shot), None)
    if row is None:
        raise ValueError("No approved frame for this shot")
    approved = (ROOT / "output/visual-pilot-30").resolve()
    destination = Path(destination).resolve()
    if not destination.is_relative_to(ROOT / "output") or destination.is_relative_to(approved):
        raise ValueError("Rollback requires a separate output directory")
    raw = (approved / row["file"]).read_bytes()
    if sha256(raw) != row["sha256"]:
        raise ValueError("Approved frame integrity check failed")
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw)
            handle.close()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

def screen(path, baseline, other_frames=()):
    from PIL import Image, ImageStat, ImageChops

    def samples(path):
        with Image.open(path) as frame:
            rgb = frame.convert("RGB").resize((160, 90), Image.Resampling.LANCZOS)
            gray = rgb.convert("L")
            red, _, blue = ImageStat.Stat(rgb).mean
            return frame.size, gray, ImageStat.Stat(gray).mean[0], blue - red

    (width, height), gray, light, cold = samples(path)
    _, ref, ref_light, ref_cold = samples(baseline)
    failures = []
    if abs(width / height - 16 / 9) > .015:
        failures.append("aspect ratio")
    if abs(light - ref_light) > 22 or abs(cold - ref_cold) > 13:
        failures.append("lighting or palette drift")
    for other in (ref, *(samples(frame)[1] for frame in other_frames)):
        if ImageStat.Stat(ImageChops.difference(gray, other)).mean[0] < 10:
            failures.append("near duplicate")
            break
    return {"light": round(light, 2), "blue_minus_red": round(cold, 2), "failures": failures}



def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shot", type=int)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--rollback-to", type=Path)
    args = parser.parse_args()
    locked = profile()
    row = next((row for row in locked["baseline"] if row["shot"] == args.shot), None)
    if row is None:
        parser.error("No approved frame for this shot")
    baseline = ROOT / "output/visual-pilot-30" / row["file"]
    if sha256(baseline.read_bytes()) != row["sha256"]:
        parser.error("Approved frame integrity check failed")
    try:
        result = screen(args.candidate, baseline)
    except OSError as error:
        result = {"failures": [str(error)]}
    if args.backend != locked["backend"]:
        result["failures"].append("backend differs from ART_STYLE_LOCK")
    if result["failures"] and args.rollback_to is not None:
        restore_approved(args.shot, args.rollback_to)
        result["restored"] = str(args.rollback_to)
    print(json.dumps(result))
    return int(bool(result["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
