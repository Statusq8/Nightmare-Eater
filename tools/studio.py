"""Build a local video package from a chapter. Default mode only prints a plan."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import math
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import time

from render import FPS, ffmpeg, render
from studio_providers import ComfyUI, Ollama, Piper, Prepared, Synthetic

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1"
TEXT_PROVIDERS = {"ollama": Ollama, "prepared": Prepared}
IMAGE_PROVIDERS = {"comfyui": ComfyUI, "prepared": Prepared}
VOICE_PROVIDERS = {"piper": Piper, "prepared": Prepared}
MOTIONS = {"static", "push", "pull", "pan"}
STAGES = "plan, panels, voice, subtitles, render, thumbnail, metadata, verification"


def read(path):
    return path.read_text(encoding="utf-8-sig")


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else (
            iter_hash(stream)
        )


def iter_hash(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def settings():
    overrides = {}
    dotenv = ROOT / ".env"
    if dotenv.is_file():
        for line in read(dotenv).splitlines():
            key, _, value = line.partition("=")
            if key.strip().startswith("STUDIO_"):
                overrides[key.strip()] = value.strip().strip('"').strip("'")
    defaults = dict(
        text_provider="ollama", image_provider="comfyui", tts_provider="piper",
        ollama_url="http://127.0.0.1:11434", ollama_model="", vision_model="",
        comfy_url="http://127.0.0.1:8188", comfy_workflow="", comfy_output_node="",
        comfy_trusted_nodes="", piper_executable="", piper_python="", piper_model="", prepared_dir="",
        reference="assets/characters/jinhyeok-approved.png", language="Russian",
    )
    config = {name: overrides.get("STUDIO_" + name.upper(), value) for name, value in defaults.items()}
    for key in ("reference", "comfy_workflow", "piper_model", "prepared_dir", "piper_python"):
        if config[key]:
            config[key] = str((ROOT / config[key]).resolve())
    return config


def blockers(config):
    missing = []
    for kind, adapters in (("text", TEXT_PROVIDERS), ("image", IMAGE_PROVIDERS), ("tts", VOICE_PROVIDERS)):
        if config[kind + "_provider"] not in adapters:
            missing.append("Unsupported provider: " + kind)
    required_files = ["reference"]
    required_settings = []
    if config["image_provider"] == "comfyui":
        required_files.append("comfy_workflow")
        required_settings.append("comfy_output_node")
    if config["text_provider"] == "ollama":
        required_settings.append("ollama_model")
    if config["tts_provider"] == "piper":
        required_files.append("piper_model")
        if config["piper_model"] and not Path(config["piper_model"] + ".json").is_file():
            missing.append("Missing Piper voice configuration (.onnx.json).")
        executable = config["piper_executable"]
        if executable and not shutil.which(executable):
            missing.append("Missing Piper executable.")
        elif config["piper_python"] and not Path(config["piper_python"]).is_file():
            missing.append("Missing isolated Piper Python.")
        elif not executable and not config["piper_python"]:
            from importlib.util import find_spec

            if find_spec("piper") is None:
                missing.append("Missing piper-tts package in the active Python environment.")
    if any(config[key] == "prepared" for key in ("text_provider", "image_provider", "tts_provider")):
        if not config["prepared_dir"] or not Path(config["prepared_dir"]).is_dir():
            missing.append("Missing prepared assets directory.")
    for key in required_files:
        if not config[key] or not Path(config[key]).is_file():
            missing.append("Missing local file: " + key)
    for key in required_settings:
        if not config[key]:
            missing.append("Missing configuration: " + key)
    if config["reference"] and Path(config["reference"]).is_file():
        with Path(config["reference"]).open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                missing.append("Approved reference must be a PNG.")
    return missing


@contextmanager
def exclusive(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Run:
    def __init__(self, folder):
        self.folder = folder
        self.manifest = folder / "manifest.json"
        self.state = json.loads(read(self.manifest)) if self.manifest.exists() else {}

    def log(self, name, status, detail=""):
        with (self.folder / "journal.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "stage": name, "status": status, "detail": detail}) + "\n")

    def stage(self, name, inputs, outputs, work, attempts=1):
        key = fingerprint(inputs)
        paths = [self.folder / path for path in outputs]
        previous = self.state.get(name, {})
        if previous.get("key") == key and all(
            path.is_file() and file_hash(path) == previous.get("outputs", {}).get(path.name)
            for path in paths
        ):
            self.log(name, "reused")
            return
        for attempt in range(attempts):
            try:
                with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=self.folder) as temp:
                    staging = [Path(temp) / path.name for path in paths]
                    work(*staging)
                    if not all(path.is_file() and path.stat().st_size for path in staging):
                        raise ValueError("Missing or empty stage output.")
                    for source, target in zip(staging, paths):
                        source.replace(target)
                self.state[name] = {"key": key, "outputs": {path.name: file_hash(path) for path in paths}}
                temporary = self.manifest.with_suffix(".tmp")
                write(temporary, self.state)
                temporary.replace(self.manifest)
                self.log(name, "complete")
                return
            except Exception as error:
                self.log(name, "failed", type(error).__name__)
                if attempt + 1 == attempts:
                    raise
                time.sleep(1)


def validate_plan(plan, chapter, limit, full=False):
    for key, maximum in (("title", 100), ("description", 5000)):
        if not isinstance(plan[key], str) or not 0 < len(plan[key].strip()) <= maximum:
            raise ValueError("Invalid YouTube " + key)
    if not isinstance(plan["tags"], list) or any(not isinstance(tag, str) for tag in plan["tags"]):
        raise ValueError("Tags must be strings.")
    shots = plan["shots"]
    if not isinstance(shots, list) or not 1 <= len(shots) <= limit:
        raise ValueError("Invalid number of shots.")
    if type(plan["cover_shot"]) != int or not 1 <= plan["cover_shot"] <= len(shots):
        raise ValueError("Cover shot is out of range.")
    positions = []
    aliases = {}
    for shot in shots:
        if shot["camera"] not in MOTIONS:
            raise ValueError("Unsupported camera movement.")
        for field in ("source_quote", "narration", "image_prompt"):
            if not isinstance(shot[field], str) or not shot[field].strip():
                raise ValueError("Missing shot field: " + field)
        quote = shot["source_quote"].strip()
        start = chapter.find(quote, positions[-1] if positions else 0)
        if start < 0:
            raise ValueError("Source quote is missing or out of order: " + quote[:50])
        positions.append(start)
        names = shot["characters"]
        if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("Character labels must be nonempty strings.")
        for name in names:
            key = re.sub(r"\W+", "", name.casefold())
            if key in aliases and aliases[key] != name:
                raise ValueError("Inconsistent spelling for " + name)
            aliases[key] = name
    if full and (len(shots) < min(limit, 8) or positions[0] > len(chapter) * 0.25
                 or positions[-1] < len(chapter) * 0.70):
        raise ValueError("A full episode needs enough shots and source anchors near both ends.")


def probe(path):
    return json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))


def media_check(path, kind):
    data = probe(path)
    if not any(stream["codec_type"] == kind for stream in data["streams"]):
        raise ValueError(f"No {kind} stream in {path.name}")
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], check=True)
    return data


def timestamp(milliseconds):
    seconds, ms = divmod(milliseconds, 1000)
    minutes, s = divmod(seconds, 60)
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def assemble(folder, scene_rows, voice_file, srt_file, storyboard_file):
    clips, captions, shots = [], [], []
    elapsed = 0
    for index, (shot, image, sound) in enumerate(scene_rows, 1):
        duration = float(probe(sound)["format"]["duration"])
        frames = max(1, math.ceil(duration * FPS) + 6)
        clips.append(sound)
        chunks = textwrap.wrap(" ".join(shot["narration"].split()), 40, break_long_words=False)
        pairs = ["\n".join(chunks[i:i + 2]) for i in range(0, len(chunks), 2)]
        weight = sum(map(len, pairs))
        cursor = 0
        for lines in pairs:
            start = round((elapsed / FPS + duration * cursor / weight) * 1000)
            cursor += len(lines)
            end = round((elapsed / FPS + duration * cursor / weight) * 1000)
            if start < end:
                captions.append(f"{len(captions)+1}\n{timestamp(start)} --> {timestamp(end)}\n{lines}\n")
        shots.append({field: shot[field] for field in ("camera", "characters", "narration", "source_quote", "image_prompt")}
                     | {"id": f"{index:03d}", "image": image.name, "duration": frames / FPS})
        elapsed += frames

    # Use the same frame grid for the narration, subtitles and shot durations.
    inputs, audio_filters = [], []
    for index, (path, shot) in enumerate(zip(clips, shots)):
        inputs += ["-i", path.name]
        audio_filters.append(f"[{index}:a]aresample=48000,aformat=channel_layouts=mono,"
                             f"apad,atrim=duration={shot['duration']:.8f}[a{index}]")
    audio_filters.append("".join(f"[a{i}]" for i in range(len(clips))) +
                         f"concat=n={len(clips)}:v=0:a=1[out]")
    ffmpeg(*inputs, "-filter_complex", ";".join(audio_filters), "-map", "[out]", voice_file, cwd=folder)
    srt_file.write_text("\n".join(captions), encoding="utf-8")
    write(storyboard_file, {"shots": shots, "voiceover": voice_file.name,
                           "subtitles": srt_file.name})


def verify(folder, rows, synthetic, vision, target):
    video = folder / "episode.mp4"
    data = media_check(video, "video")
    streams = {stream["codec_type"]: stream for stream in data["streams"]}
    picture, sound = streams["video"], streams.get("audio", {})
    if (picture["width"], picture["height"], picture["avg_frame_rate"], picture["codec_name"],
            sound.get("codec_name")) != (1920, 1080, "30/1", "h264", "aac"):
        raise ValueError("Unexpected output format.")
    expected = sum(row["duration"] for row in json.loads(read(folder / "storyboard.json"))["shots"])
    if abs(float(data["format"]["duration"]) - expected) > 0.1:
        raise ValueError("Video duration differs from storyboard.")
    volume = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", check=True,
    )
    match = re.search(r"mean_volume:\s+(-?[\d.]+) dB", volume.stderr)
    if not match or float(match[1]) <= -60:
        raise ValueError("Voice track is silent or too quiet.")
    subtitles = read(folder / "captions.srt")
    cues = re.findall(r"(?m)^(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})$", subtitles)
    if (not cues or any(a >= b for a, b in cues) or
            any(first[1] > second[0] for first, second in zip(cues, cues[1:])) or
            cues[-1][1] > timestamp(round(expected * 1000))):
        raise ValueError("Invalid subtitle timings.")
    hashes = [file_hash(image) for _, image, _ in rows]
    if len(hashes) != len(set(hashes)):
        raise ValueError("Duplicate panel files detected.")
    media_check(folder / "thumbnail.jpg", "video")
    write(target, {"mode": "synthetic" if synthetic else "chapter_draft",
                   "video": {"width": 1920, "height": 1080, "duration": data["format"]["duration"]},
                   "shots": len(rows), "subtitle_cues": len(cues), "audio_mean_db": float(match[1]),
                   "checked": ["chronological source quotes", "character label spelling", "unique panel files",
                               "audio level", "subtitle timings", "format", "full decode"],
                   "visual_review": "not applicable" if synthetic else (vision or "pending editorial review"),
                   "editorial_review_required": not synthetic,
                   "publish": "manual only"})


def visual_review(ollama, config, reference, shot, image, canon, target):
    prompt = json.dumps({"characters": shot["characters"], "expected_panel": shot["image_prompt"],
                         "visual_canon": canon, "source_quote": shot["source_quote"],
                         "narration": shot["narration"]}, ensure_ascii=False)
    result = ollama.generate(config["vision_model"],
        "First image is the approved character reference. Second is the generated panel. "
        "Compare clothing, face and character count where relevant. Assess panel and narration "
        "against the supplied expectations and quote. Return JSON with verdict (pass or review) and notes.",
        prompt, [base64.b64encode(path.read_bytes()).decode() for path in (reference, image)])
    if result.get("verdict") not in {"pass", "review"} or not isinstance(result.get("notes"), str):
        raise ValueError("Malformed visual review.")
    write(target, result)


def execute(folder, config, options, source, identity):
    chapter, lore, canon = source
    if not options.synthetic:
        from art_style_lock import require_backend
        require_backend(config["image_provider"])
    synthetic = options.synthetic
    pilot = getattr(options, "pilot", False)
    fixture = Synthetic()
    text = fixture if synthetic else TEXT_PROVIDERS[config["text_provider"]](config)
    image_provider = fixture if synthetic else IMAGE_PROVIDERS[config["image_provider"]](config)
    voice = fixture if synthetic else VOICE_PROVIDERS[config["tts_provider"]](config)
    ref = None if synthetic else Path(config["reference"])
    quote = next(line.strip() for line in chapter.splitlines() if line.strip() and not line.startswith("#"))
    with exclusive(folder):
        run = Run(folder)
        run.stage("inputs", identity, ["inputs.json"], lambda path: write(path, identity))

        def plan_out(path):
            plan = text.plan(chapter, lore, canon, quote, options.max_shots, config["language"])
            validate_plan(plan, chapter, options.max_shots, full=not synthetic and not pilot and options.max_shots > 3)
            write(path, plan)

        run.stage("plan", identity, ["plan.json"], plan_out, attempts=2)
        plan = json.loads(read(folder / "plan.json"))
        rows, reviews = [], []
        for index, shot in enumerate(plan["shots"], 1):
            seed = int(fingerprint([identity, index])[:8], 16)
            panel = folder / f"panel-{index:03d}.png"
            audio = folder / f"voice-{index:03d}.wav"

            def panel_out(path):
                image_provider.image(shot["image_prompt"], seed, ref, path,
                                     folder / f"comfy-{index:03d}-{fingerprint([shot['image_prompt'], seed])[:12]}.json")
                media_check(path, "video")

            def voice_out(path):
                voice.speech(shot["narration"], path)
                media_check(path, "audio")

            run.stage(f"panel-{index}", [shot["image_prompt"], seed], [panel.name], panel_out)
            run.stage(f"voice-{index}", [shot["narration"], identity], [audio.name], voice_out, attempts=2)
            rows.append((shot, panel, audio))
            if not synthetic and config["vision_model"] and shot["characters"]:
                name = f"visual-{index:03d}.json"
                run.stage(f"visual-{index}", [identity, shot, file_hash(panel)], [name],
                          lambda path: visual_review(Ollama(config), config, ref, shot, panel, canon, path), attempts=2)
                reviews.append({"shot": index, **json.loads(read(folder / name))})

        materials = [[shot, file_hash(panel), file_hash(audio)] for shot, panel, audio in rows]
        run.stage("timeline", materials, ["narration.wav", "captions.srt", "storyboard.json"],
                  lambda *paths: assemble(folder, rows, *paths))
        timeline = [file_hash(folder / name) for name in ("narration.wav", "captions.srt", "storyboard.json")]
        if pilot:
            seconds = sum(shot["duration"] for shot in json.loads(read(folder / "storyboard.json"))["shots"])
            if not 30 <= seconds <= 60:
                raise ValueError(f"Pilot narration runs {seconds:.1f}s. Required range: 30–60s.")
        run.stage("render", [materials, timeline], ["episode.mp4"],
                  lambda path: render(folder / "storyboard.json", path))
        cover = rows[plan["cover_shot"] - 1][1]
        run.stage("thumbnail", file_hash(cover), ["thumbnail.jpg"], lambda path: ffmpeg(
            "-i", cover, "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
            "-frames:v", 1, "-update", 1, "-q:v", 3, path, cwd=folder))
        public_fields = {field: plan[field] for field in ("title", "description", "tags")}
        run.stage("metadata", public_fields, ["youtube.json"], lambda path: write(path, public_fields))
        run.stage("qa", [file_hash(folder / "episode.mp4"), file_hash(folder / "thumbnail.jpg"),
                         materials, timeline, reviews], ["report.json"],
                  lambda path: verify(folder, rows, synthetic, reviews or None, path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chapter", nargs="?", default="001", help="Three-digit chapter number")
    parser.add_argument("--run", action="store_true", help="Execute; default is dry-run")
    parser.add_argument("--synthetic", action="store_true", help="Use synthetic test fixtures")
    parser.add_argument("--max-shots", type=int, default=24)
    parser.add_argument("--pilot", action="store_true", help="Use the prepared Russian opening of chapter 001")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{3}", args.chapter) or not 1 <= args.max_shots <= 60:
        parser.error("Use a three-digit chapter and 1–60 shots.")
    config = settings()
    if args.pilot:
        if args.chapter != "001" or args.synthetic:
            parser.error("--pilot only supports the real chapter 001.")
        args.max_shots = 7
        config["text_provider"] = "prepared"
        config["prepared_dir"] = str(ROOT / "youtube" / "storyboards")
        config["prepared_plan"] = "001-pilot.plan.json"
        config["language"] = "Russian"
    missing = [] if args.synthetic else blockers(config)
    paths = [ROOT / name for name in (f"chapters/{args.chapter}.md",
                                     "lore/story-bible.md", "lore/character-design.md")]
    if not args.synthetic:
        missing += [f"Missing source: {path.relative_to(ROOT)}" for path in paths if not path.is_file()]
    missing += [f"Missing executable: {name}" for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    print(f"{'RUN' if args.run else 'DRY RUN'} | chapter {args.chapter} | {STAGES}")
    print(f"Paid services: USD 0.00 (hard limit); up to {args.max_shots} shots; local compute only.")
    for problem in missing:
        print(problem)
    if not args.run:
        return
    if missing:
        parser.exit(2, "Resolve the listed prerequisites before execution.\n")
    source = ("# Fixture\n\nСинтетический исходник.", "", "") if args.synthetic else tuple(map(read, paths))
    local_files = {}
    if not args.synthetic:
        local_files["reference"] = Path(config["reference"])
        if config["image_provider"] == "comfyui":
            local_files["workflow"] = Path(config["comfy_workflow"])
        if config["tts_provider"] == "piper":
            local_files["voice"] = Path(config["piper_model"])
            local_files["voice_config"] = Path(config["piper_model"] + ".json")
            if config["piper_executable"]:
                local_files["piper_executable"] = Path(shutil.which(config["piper_executable"]))
        if any(config[key] == "prepared" for key in ("text_provider", "image_provider", "tts_provider")):
            for path in Path(config["prepared_dir"]).iterdir():
                if (path.name == config.get("prepared_plan", "plan.json") or
                        re.fullmatch(r"panel-\d{3}\.png|voice-\d{3}\.wav", path.name)) and path.is_file():
                    local_files["prepared/" + path.name] = path
    identity = {"version": VERSION, "chapter": args.chapter, "synthetic": args.synthetic, "pilot": args.pilot,
                "max_shots": args.max_shots, "engine": {name: file_hash(ROOT / "tools" / name) for name in
                    ("studio.py", "studio_providers.py", "render.py")}, "sources": [fingerprint(item) for item in source],
                "files": {key: file_hash(path) for key, path in local_files.items()},
                "providers": {key: config[key] for key in ("text_provider", "image_provider", "tts_provider",
                              "ollama_model", "vision_model", "language", "comfy_output_node",
                              "comfy_trusted_nodes", "ollama_url", "comfy_url")} if not args.synthetic else {}}
    mode = "synthetic" if args.synthetic else (args.chapter + "-pilot" if args.pilot else args.chapter)
    folder = ROOT / "output" / "studio" / f"{mode}-{fingerprint(identity)[:16]}"
    try:
        execute(folder, config, args, source, identity)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Run failed ({type(error).__name__}): {error}\nResume with the same command.\n")
    print(folder / "report.json")


if __name__ == "__main__":
    main()
