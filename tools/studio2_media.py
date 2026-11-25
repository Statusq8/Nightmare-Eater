"""Reusable scene encodes, separate audio stems and a shared bilingual master."""
import json
import math
from pathlib import Path
import shutil
import wave

from pilot_audio import read_audio
from render import ffmpeg
from studio import Run, file_hash, write
from studio2_qc import LANGS, verify_master, verify_scene
from studio_director import ordered_ranges

STEMS = ("music", "ambience", "effects")
BYTES_PER_FRAME = 6400  # 48000 Hz / 30 fps, stereo signed 16-bit


def fixture_assets(folder, row):
    folder.mkdir(exist_ok=True)
    n = row["id"]
    color = f"{(30+n*9)%256:02x}{(47+n*13)%256:02x}{(90+n*7)%256:02x}"
    ffmpeg("-f", "lavfi", "-i", f"color=c=0x{color}:s=320x180",
           "-frames:v", 1, "-update", 1, folder/"panel.png", cwd=folder)
    for i, lang in enumerate(LANGS):
        ffmpeg("-f", "lavfi", "-i",
               f"sine=frequency={250+n*13+i*60}:sample_rate=48000:duration={.44+i*.19}",
               folder/(lang+".wav"), cwd=folder)


def caption_time(milliseconds, separator):
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"


def wav(path, content):
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(content)


def picture(folder, panel, target, frames, motion):
    if motion == "static":
        vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1"
        arguments = ("-loop", 1, "-framerate", 30)
    else:
        easing = f"(0.5-0.5*cos(PI*on/{max(1, frames-1)}))"
        zoom = f"1+0.045*{easing}" if motion == "push" else "1.045"
        x = "(iw-iw/zoom)/2" if motion == "push" else f"(iw-iw/zoom)*(0.25+0.5*{easing})"
        vf = ("scale=3840:2160:force_original_aspect_ratio=increase,crop=3840:2160,"
              f"zoompan=z='{zoom}':x='{x}':y='(ih-ih/zoom)/2':d={frames}:s=1920x1080:fps=30,setsar=1")
        arguments = ()
    ffmpeg(*arguments, "-i", panel, "-vf", vf, "-frames:v", frames, "-an", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", 18, "-pix_fmt", "yuv420p",
           "-video_track_timescale", 90000, "-movie_timescale", 90000, target, cwd=folder)


def encode_scene(assets, target, shots, frames):
    if len(shots) == 1:
        picture(target.parent, assets/shots[0]["file"], target, frames, shots[0]["camera"])
        return
    parts = []
    for index, shot in enumerate(shots, 1):
        part = target.parent / f"part-{index:03}.mp4"
        picture(target.parent, assets/shot["file"], part, shot["frames"], shot["camera"])
        parts.append(part.name)
    edit = target.parent/"parts.ffconcat"
    edit.write_text("ffconcat version 1.0\n" + "".join(f"file '{part}'\n" for part in parts), encoding="ascii")
    ffmpeg("-f", "concat", "-safe", 1, "-i", edit.name, "-map", "0:v", "-c", "copy", target, cwd=target.parent)


def cues_for(row, ticket, lang, frames, fixture):
    if fixture:
        return [{"start": 0, "end": frames, "text": f"TEST FIXTURE {row['id']:02} {lang.upper()}"}]
    cues = ticket["captions"][lang]
    if not cues:
        raise ValueError("Missing caption cues.")
    previous = 0
    for cue in cues:
        if (type(cue["start"]) is not int or type(cue["end"]) is not int or
                not previous <= cue["start"] < cue["end"] <= frames or not cue["text"].strip()):
            raise ValueError("Invalid caption position.")
        previous = cue["end"]
    if lang == "ru":
        import re
        spans = ordered_ranges(row["text"], [cue["text"] for cue in cues])
        cursor = 0
        for start, end in [*spans, (len(row["text"]), len(row["text"]))]:
            if re.search(r"[^\s#*>-]", row["text"][cursor:start]):
                raise ValueError("Prepared captions omit Russian source text.")
            cursor = end
    return cues


def render_episode(folder, rows, source_sha, prepared=None, fixture=False, invalidated=()):
    run = Run(folder)
    for scene in invalidated:
        run.state.pop(f"picture-{scene:03}", None)
    streams = {lang: bytearray() for lang in LANGS}
    captions = {lang: [] for lang in LANGS}
    frames, clips = 0, []
    for row in rows:
        number = row["id"]
        assets = (folder if fixture else prepared) / f"scene-{number:03}"
        if fixture and not all((assets/name).exists() for name in ("panel.png", "ru.wav", "en.wav")):
            fixture_assets(assets, row)
        ticket = verify_scene(assets, row, source_sha, fixture=fixture)
        dialogue = {lang: read_audio(assets / (lang+".wav")) for lang in LANGS}
        if any(len(content) % 4 or not content or not any(content) for content in dialogue.values()):
            raise ValueError("Missing or silent dialogue.")
        duration = max(24 if fixture else 75, math.ceil(max(map(len, dialogue.values())) / BYTES_PER_FRAME) + 6)
        shots = ticket["picture_entries"]
        if "shots" in ticket:
            planned_frames = sum(shot["frames"] for shot in shots)
            if planned_frames < duration:
                raise ValueError("Shot timings cut off dialogue or the final hold.")
            duration = planned_frames
        for lang, content in dialogue.items():
            streams[lang].extend(content)
            streams[lang].extend(bytes(duration * BYTES_PER_FRAME - len(content)))
            for cue in cues_for(row, ticket, lang, duration, fixture):
                captions[lang].append({**cue, "start": frames+cue["start"], "end": frames+cue["end"]})
        clipname = f"picture-{number:03}.mp4"
        picture_inputs = [[shot, file_hash(assets/shot["file"])] for shot in shots]
        run.stage(f"picture-{number:03}", [picture_inputs, duration, file_hash(Path(__file__))], [clipname],
                  lambda target: encode_scene(assets, target, shots, duration))
        clips.append(clipname)
        frames += duration
    (folder/"edit.ffconcat").write_text("ffconcat version 1.0\n" + "".join(
        f"file '{clip}'\n" for clip in clips), encoding="ascii")
    ffmpeg("-f", "concat", "-safe", 1, "-i", "edit.ffconcat", "-map", "0:v", "-c", "copy",
           "picture.mp4", cwd=folder)
    if fixture:
        credits = {name: {"source": "Synthetic silence", "license": "Project fixture"} for name in STEMS}
        for name in STEMS:
            wav(folder/(name+".wav"), bytes(frames*BYTES_PER_FRAME))
    else:
        credits = json.loads((prepared/"credits.json").read_text(encoding="utf-8"))
        for name in STEMS:
            note = credits[name]
            if not note["source"] or not note["license"] or note.get("commercial_use") is not True:
                raise ValueError(f"Missing commercial-use provenance: {name}.")
            content = read_audio(prepared/(name+".wav"))
            if len(content) != frames * BYTES_PER_FRAME:
                raise ValueError(f"Unsynchronized {name} stem.")
            wav(folder/(name+".wav"), content)
    write(folder/"credits.json", credits)
    for lang in LANGS:
        voice = folder/f"dialogue.{lang}.wav"
        wav(voice, streams[lang])
        args = [arg for stem in STEMS for arg in ("-i", folder/(stem+".wav"))]
        ffmpeg("-i", voice, *args, "-filter_complex",
               "[0:a][1:a][2:a][3:a]amix=inputs=4:duration=first:normalize=0,"
               "alimiter=limit=0.84:level=0:latency=1", "-ar", 48000, "-ac", 2,
               folder/f"mix.{lang}.wav", cwd=folder)
        for kind in ("srt", "vtt"):
            separator = "," if kind == "srt" else "."
            lines = ["WEBVTT", ""] if kind == "vtt" else []
            for i, cue in enumerate(captions[lang], 1):
                start, end = (caption_time(round(cue[pos] * 1000 / 30), separator) for pos in ("start", "end"))
                lines.extend((str(i), f"{start} --> {end}", cue["text"], ""))
            (folder/f"captions.{lang}.{kind}").write_text("\n".join(lines), encoding="utf-8")
    path = folder/"episode.mp4"
    ffmpeg("-i", "picture.mp4", "-i", "mix.ru.wav", "-i", "mix.en.wav", "-map", "0:v",
           "-map", "1:a", "-map", "2:a", "-c:v", "copy", "-c:a", "aac",
           "-metadata:s:a:0", "language=rus", "-metadata:s:a:1", "language=eng",
           "-movflags", "+faststart", path, cwd=folder)
    return verify_master(path, frames, fixture)

