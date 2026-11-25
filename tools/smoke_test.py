"""Build and verify a synthetic 3-second video without project assets."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from render import ffmpeg, render


def fixture(folder):
    for name, color in (("blue", "blue"), ("red", "red")):
        ffmpeg("-f", "lavfi", "-i", f"color={color}:s=640x360",
               "-vf", "drawbox=x=180:y=100:w=140:h=120:color=white:t=fill",
               "-frames:v", 1, "-update", 1, f"{name}.png", cwd=folder)
    for name, frequency, duration in (("voice", 440, 2), ("effect", 660, 0.2)):
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}",
               f"{name}.wav", cwd=folder)
    (folder / "test.srt").write_text(
        "1\n00:00:00,300 --> 00:00:02,500\nСинтетический тест\n", encoding="utf-8"
    )
    manifest = folder / "storyboard.json"
    manifest.write_text(json.dumps({
        "voiceover": "voice.wav",
        "subtitles": "test.srt",
        "shots": [
            {"id": "test-1", "image": "blue.png", "duration": 1, "camera": "push"},
            {"id": "test-2", "image": "red.png", "duration": 2, "camera": "pan", "sound": "effect.wav"},
        ],
    }), encoding="utf-8")
    return manifest


def verify(output):
    data = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)
    ]))
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
    assert (video["width"], video["height"], video["codec_name"]) == (1920, 1080, "h264")
    assert video["avg_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert abs(float(data["format"]["duration"]) - 3) <= 0.1
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
                   check=True)
    def frame_digest(seconds):
        return subprocess.check_output([
            "ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(output),
            "-an", "-frames:v", "1", "-f", "md5", "-"
        ])

    assert frame_digest(0.4) != frame_digest(0.8), "Push must change the frame."
    assert frame_digest(1.2) != frame_digest(1.8), "Pan must change the frame."
    print(f"OK: {output} (1920x1080, 30 fps, H.264/AAC, {data['format']['duration']} s)")


if __name__ == "__main__":
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "output/smoke.mp4").resolve()
    with tempfile.TemporaryDirectory(prefix="nightmare-smoke-") as temp:
        render(fixture(Path(temp)), output)
    verify(output)
