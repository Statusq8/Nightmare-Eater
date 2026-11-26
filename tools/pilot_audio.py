"""Mix prepared dialogue and sound stems against a shared frame timeline."""
import json
import subprocess
import wave

from render import ffmpeg

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 1600
LANGUAGES = ("ru", "en")
STEMS = ("music", "ambience", "effects")
GAINS = {"music": 0.28, "ambience": 0.38, "effects": 0.55}
VOICE_FILTERS = {
    "system": "highpass=f=155,equalizer=f=2600:t=q:w=1.2:g=3",
    "anomaly": "highpass=f=100,lowpass=f=1850,pan=stereo|c0=0.73*c0|c1=c1",
    "mother": "highpass=f=80,lowpass=f=3900,pan=stereo|c0=0.8*c0|c1=c1",
}


def read_audio(path, channels=2, treatment=None):
    if not path.is_file():
        raise ValueError(f"Missing audio source: {path}")
    command = ["ffmpeg", "-v", "error", "-i", str(path), "-vn"]
    if treatment:
        command.extend(("-af", "aformat=channel_layouts=stereo," + treatment))
    command.extend(("-ac", str(channels), "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"))
    return subprocess.run(command, check=True, capture_output=True).stdout


def write_dialogue(plan, source, lang, output):
    voice = bytearray(plan["frames"] * SAMPLES_PER_FRAME * 4)
    for index, cue in enumerate(plan["dialogue"], 1):
        line = read_audio(source / lang / f"line-{index:03}.wav", treatment=VOICE_FILTERS.get(cue["speaker"]))
        start = cue["start"] * SAMPLES_PER_FRAME * 4
        capacity = (cue["end"] - cue["start"]) * SAMPLES_PER_FRAME * 4
        if not line or len(line) > capacity or line.count(0) == len(line):
            raise ValueError(f"{lang} line {index:03} is silent or exceeds its slot.")
        voice[start:start + len(line)] = line
    with wave.open(str(output), "wb") as track:
        track.setparams((2, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        track.writeframes(voice)


def assemble_audio(plan, source, out):
    credits = json.loads((source / "credits.json").read_text(encoding="utf-8"))
    for stem in STEMS:
        evidence = credits[stem]
        if not evidence.get("source") or not evidence.get("license"):
            raise ValueError(f"Missing provenance for {stem}.")
    (out / "credits.json").write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")
    duration = plan["frames"] / plan["fps"]
    tracks = []
    for stem in STEMS:
        original = source / f"{stem}.wav"
        if not original.is_file():
            raise ValueError(f"Missing sound stem: {original}")
        target = out / f"{stem}.wav"
        ffmpeg("-i", original, "-vn", "-af", f"volume={GAINS[stem]},apad",
               "-t", duration, "-ar", SAMPLE_RATE, "-ac", 2, target, cwd=out)
        tracks.append(target)
    for lang in LANGUAGES:
        voice = out / f"dialogue.{lang}.wav"
        write_dialogue(plan, source, lang, voice)
        inputs = [arg for file in (voice, *tracks) for arg in ("-i", file)]
        ffmpeg(*inputs, "-filter_complex",
               "[0:a][1:a][2:a][3:a]amix=inputs=4:duration=first:normalize=0,"
               "volume=4dB,alimiter=limit=0.84:level=0:latency=1",
               "-t", duration, "-ar", SAMPLE_RATE, "-ac", 2, out / f"mix.{lang}.wav", cwd=out)


def mux(out):
    ffmpeg("-i", out / "picture.mp4", "-i", out / "mix.ru.wav", "-i", out / "mix.en.wav",
           "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0", "-c:v", "copy", "-c:a", "aac",
           "-b:a", "192k", "-metadata:s:a:0", "language=rus", "-metadata:s:a:1", "language=eng",
           "-movflags", "+faststart", out / "review.mp4", cwd=out)

