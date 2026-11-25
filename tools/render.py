"""Render a local storyboard using Python's standard library and FFmpeg."""
import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

WIDTH, HEIGHT, FPS = 1920, 1080, 30


def ffmpeg(*args, cwd):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        cwd=cwd, check=True,
    )


def input_file(root, name):
    path = (root / name).resolve()
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    return path


def render(manifest, output):
    root = manifest.parent
    data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    shots = data["shots"]
    ids = [shot["id"] for shot in shots]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Provide at least one shot and use unique IDs.")
    audio = input_file(root, data["voiceover"])
    subtitles = input_file(root, data["subtitles"])
    output.parent.mkdir(parents=True, exist_ok=True)
    motions = {
        "static": ("1", "0.5"),
        "push": ("1+0.10*P", "0.5"),
        "pull": ("1.10-0.10*P", "0.5"),
        "pan": ("1.10", "0.12+0.76*P"),
    }
    with tempfile.TemporaryDirectory(prefix="nightmare-render-") as temp:
        cwd = Path(temp)
        shutil.copyfile(subtitles, cwd / "captions.srt")
        clips, sounds = [], []
        elapsed = 0
        for index, shot in enumerate(shots):
            duration = float(shot["duration"])
            if not math.isfinite(duration) or duration < 1 / FPS:
                raise ValueError(f"Invalid duration for shot {shot['id']}.")
            frames = round(duration * FPS)
            zoom, position = motions[shot["camera"]]
            progress = f"(on/{max(frames - 1, 1)})"
            zoom, position = zoom.replace("P", progress), position.replace("P", progress)
            motion = (
                "scale=3840:2160:force_original_aspect_ratio=increase,crop=3840:2160,"
                f"zoompan=z='{zoom}':x='(iw-iw/zoom)*({position})':"
                f"y='(ih-ih/zoom)/2':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
                "setsar=1,format=yuv420p"
            )
            clip = f"shot-{index:04}.mp4"
            ffmpeg("-i", input_file(root, shot["image"]), "-vf", motion,
                   "-frames:v", frames, "-an", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", 20, clip, cwd=cwd)
            clips.append(clip)
            if shot.get("sound"):
                sounds.append((input_file(root, shot["sound"]), elapsed, frames))
            elapsed += frames

        (cwd / "shots.txt").write_text("".join(f"file '{clip}'\n" for clip in clips), encoding="ascii")
        args = ["-f", "concat", "-safe", "1", "-i", "shots.txt", "-i", audio]
        filters = ["[1:a]aresample=48000,apad[voice]"]
        for index, (sound, start, length) in enumerate(sounds):
            args.extend(("-i", sound))
            filters.append(
                f"[{index + 2}:a]atrim=duration={length / FPS:.6f},aresample=48000,"
                f"volume=0.25,adelay={round(start * 1000 / FPS)}:all=1[sfx{index}]"
            )
        if sounds:
            labels = "[voice]" + "".join(f"[sfx{i}]" for i in range(len(sounds)))
            filters.append(f"{labels}amix=inputs={len(sounds) + 1}:normalize=0[audio]")
        else:
            filters.append("[voice]anull[audio]")
        ffmpeg(*args, "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[audio]",
               "-vf", "subtitles=captions.srt", "-t", f"{elapsed / FPS:.6f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", 20,
               "-c:a", "aac", "-b:a", "192k", "-ac", 2, "-movflags", "+faststart",
               "result.mp4", cwd=cwd)
        shutil.copyfile(cwd / "result.mp4", output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("storyboard", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        render(args.storyboard.resolve(), args.output.resolve())
    except (OSError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Render failed: {error}\n")


if __name__ == "__main__":
    main()
