"""Independent continuity, input-media and final-master checks."""
import json
import re
import subprocess

from studio import file_hash, probe

LANGS = ("ru", "en")
VISUAL_CHECKS = {"identity", "layout", "anatomy", "style", "motion"}
AUDIO_CHECKS = {"words", "voice_identity", "performance"}


def check_candidate(path, layout=None):
    picture = probe(path)["streams"][0]
    if picture["width"] < 720 or picture["height"] < 480:
        raise ValueError("Candidate resolution too low.")
    gray = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=96:54,format=gray",
         "-frames:v", "1", "-f", "rawvideo", "-"]
    )
    if len(gray) != 96 * 54:
        raise ValueError("Candidate cannot be decoded.")
    fraction = sum(value >= 210 for value in gray) / len(gray)
    ceiling = (layout or {}).get("maximum_bright_fraction")
    if ceiling is not None and fraction > ceiling:
        raise ValueError(f"Bright aperture exceeds layout allowance ({fraction:.1%} > {ceiling:.1%}).")
    return {"bright_fraction": round(fraction, 4)}


def require_fields(data, required, label):
    if not isinstance(data, dict) or any(data.get(name) is not True for name in required):
        raise ValueError(f"Unresolved {label} checks.")


def verify_scene(folder, row, source_sha, *, fixture=False):
    files = {name: folder / name for name in ("ru.wav", "en.wav")}
    if not fixture:
        ticket = json.loads((folder / "review.json").read_text(encoding="utf-8-sig"))
        if ticket["chapter_sha"] != source_sha or ticket["scene"] != row["id"]:
            raise ValueError("Media review points to a different scene.")
        if not ticket.get("reviewer") or not ticket.get("references"):
            raise ValueError("Media review must identify its reviewer and references.")
        for entity in row["entities"]:
            visual = {asset["hash"] for slot, asset in entity["references"].items() if slot.startswith("visual/")}
            if visual and ticket["references"].get(entity["id"]) not in visual:
                raise ValueError("Visual review does not use the scene's approved reference version.")
        require_fields(ticket["visual"], VISUAL_CHECKS, "visual")
        for lang in LANGS:
            require_fields(ticket["speech"][lang], AUDIO_CHECKS, lang + " speech")
    else:
        ticket = {"camera": ("static", "push", "pan")[row["id"] % 3]}
    shots = ticket.get("shots")
    if shots is not None:
        if not isinstance(shots, list) or not shots:
            raise ValueError("Expected a nonempty shot list.")
        for shot in shots:
            if (not re.fullmatch(r"shot-\d{3}\.png", shot["file"]) or type(shot["frames"]) is not int
                    or shot["frames"] < 1):
                raise ValueError("Invalid planned shot filename or duration.")
            require_fields(shot["visual"], VISUAL_CHECKS, "individual shot")
    else:
        shots = [{"file": "panel.png", "camera": ticket.get("camera", "static")}]
    picture_hashes = set()
    for shot in shots:
        if shot["camera"] not in {"static", "push", "pan"}:
            raise ValueError("Parallax requires a separate deformation-tested renderer.")
        path = files[shot["file"]] = folder / shot["file"]
        signature = file_hash(path)
        if signature in picture_hashes:
            raise ValueError("Repeated drawing inside the scene.")
        picture_hashes.add(signature)
        panel = probe(path)["streams"]
        if len(panel) != 1 or panel[0]["codec_type"] != "video":
            raise ValueError("Expected one picture stream per illustration.")
        if panel[0]["width"] < (320 if fixture else 960) or panel[0]["height"] < (180 if fixture else 540):
            raise ValueError("Illustration resolution is insufficient.")
    if not fixture and any(file_hash(path) != ticket["files"].get(name) for name, path in files.items()):
        raise ValueError("Media changed after review.")
    for lang in LANGS:
        direction = probe(files[lang + ".wav"])["streams"]
        if len(direction) != 1 or direction[0]["codec_type"] != "audio":
            raise ValueError("Invalid voice track.")
    for path in files.values():
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], check=True)
    return {**ticket, "picture_entries": shots}


def verify_master(path, frames, fixture):
    streams = probe(path)["streams"]
    if [stream["codec_type"] for stream in streams] != ["video", "audio", "audio"]:
        raise ValueError("Master must have one picture and two audio streams.")
    video = streams[0]
    if (video["width"], video["height"], video["avg_frame_rate"], int(video["nb_frames"])) != (
        1920, 1080, "30/1", frames
    ):
        raise ValueError("Master picture has the wrong resolution, frame rate or duration.")
    for lang, stream in zip(("rus", "eng"), streams[1:], strict=True):
        if stream.get("tags", {}).get("language") != lang or abs(float(stream["duration"]) - frames / 30) > .034:
            raise ValueError("Language or audio duration mismatch.")
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0", "-f", "null", "-"], check=True)
    return {"technical": "pass", "mode": "synthetic_fixture" if fixture else "for_owner_review",
            "frames": frames, "duration_seconds": frames / 30, "languages": list(LANGS),
            "release": "requires owner approval"}

