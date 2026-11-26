"""Nightmare Eater Studio 2: register a chapter, process the queue, inspect and back up."""
import argparse
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile

from studio import Run, exclusive, file_hash, settings, write
from studio2_assets import reference_requests, single_candidate
from studio2_media import render_episode
from studio_director import continuity, model_direction, observed_inventory, proposed_entities, register_scenes
from studio_providers import ComfyUI, Ollama
from world_memory import archive_asset, fact, identity, ingest, ledger, snapshot

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "lore/studio2-seed.json"
MODES = ("plan", "direct", "prepared", "fixture")
ATTEMPTS = 2
CATCHABLE = (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, subprocess.CalledProcessError)


class JobFailed(RuntimeError):
    """The failure is recorded and, if eligible, the job is queued for another attempt."""


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def init_canon(db, home, chapter):
    seed = read_json(SEED)
    bank = ROOT / "output/pilot-v2/voice-bank-e15f5a2e6d42"
    voice_manifest = bank / "manifest.json"
    voices = read_json(voice_manifest).get("sha256", {}) if voice_manifest.is_file() else {}
    for item in seed["entities"]:
        ident = identity(db, item["kind"], item["name"], item.get("aliases", []))
        for key, value in item.get("canon", {}).items():
            fact(db, ident, key, value, 0, "lore/character-design.md", locked=True)
        for asset in item.get("assets", []):
            if asset.get("optional") and not (ROOT/asset["path"]).is_file():
                continue
            archive_asset(db, home/"library", ident, asset["slot"], 0, -1, ROOT/asset["path"],
                          asset["source"], asset["license"], asset["state"])
        if chapter == 1:
            for key, value, evidence in item.get("facts", []):
                fact(db, ident, key, value, 1, evidence)
        if item.get("voice"):
            for language in ("ru", "en"):
                audio = f"{item['voice']}.{language}.wav"
                target = bank/audio
                if audio in voices and target.is_file():
                    if file_hash(target) != voices[audio]:
                        raise ValueError("Existing voice bank failed integrity verification.")
                    archive_asset(db, home/"library", ident, f"voice/{language}", 0, -1, target,
                                  "Locally designed VoxCPM2 pilot reference", "VoxCPM2 Apache-2.0 model; "
                                  "original synthetic voice reference", "candidate")
    if chapter == 1:
        source = db.execute("SELECT source FROM chapter WHERE number=1").fetchone()["source"]
        for location, evidence in seed["chapter_001_locations"]:
            position = source.find(evidence)
            if position < 0 or source.find(evidence, position + 1) >= 0:
                raise ValueError("A layout transition needs a unique source anchor.")
            ident = identity(db, "location", location)
            db.execute("INSERT OR IGNORE INTO chapter_location VALUES(?,?,?,?)", (chapter, position, ident, evidence))
    return seed["profile"]


def journal(db, number, mode, status, detail=""):
    db.execute("INSERT INTO journal(chapter,mode,status,detail) VALUES(?,?,?,?)",
               (number, mode, status, detail))


def enqueue(home, number, path, mode):
    with ledger(home/"world.sqlite") as db:
        sha = ingest(db, number, path.read_text(encoding="utf-8-sig"))
        changed = db.execute("INSERT OR IGNORE INTO job(chapter,mode,status) VALUES(?,?,'queued')",
                             (number, mode)).rowcount
        if changed:
            journal(db, number, mode, "queued", sha)
    return sha


def production_plan(home, job):
    number, mode, sha = job["chapter"], job["mode"], job["sha"]
    with ledger(home/"world.sqlite") as db:
        profile = init_canon(db, home, number)
        observed_inventory(db, number, job["source"])
        rows = continuity(db, number, job["source"])
        if "".join(scene["text"] for scene in rows) != job["source"]:
            raise ValueError("Source coverage failed.")
        register_scenes(db, number, rows)
        requests = reference_requests(db, number, rows)
    folder = home/"runs"/f"{number:03d}-{mode}-{sha[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    write(folder/"plan.json", {
        "chapter": number, "source_sha": sha, "source_coverage": "exact",
        "style": profile, "scenes": rows, "paid_budget_usd": 0,
    })
    write(folder/"reference-requests.json", requests)
    return folder, rows, profile


def process(home, job, start_ollama, invalidated):
    folder, rows, profile = production_plan(home, job)
    number, mode, sha = job["chapter"], job["mode"], job["sha"]
    if mode in {"direct", "prepared"}:
        config = settings()
        if mode == "direct":
            from setup_windows import run_ollama
        with run_ollama() if mode == "direct" and start_ollama else nullcontext():
            if mode == "direct":
                run = Run(folder)
                model = config["ollama_model"]
                provider = Ollama(config)
                for row in rows:
                    name = f"direction-{row['id']:03}.json"
                    run.stage(name[:-5], [row, profile, model, file_hash(ROOT/"tools/studio_director.py")], [name],
                              lambda path: write(path, model_direction(provider, model, row, profile)))
                    with ledger(home/"world.sqlite") as db:
                        proposed_entities(db, number, row, read_json(folder/name))
                with ledger(home/"world.sqlite") as db:
                    discovered_rows = continuity(db, number, job["source"])
                    register_scenes(db, number, discovered_rows)
                    write(folder/"reference-requests.json", reference_requests(db, number, discovered_rows))
                    plan = read_json(folder/"plan.json")
                    write(folder/"plan.json", {**plan, "scenes": discovered_rows})
    if mode in {"fixture", "prepared"}:
        prepared = home/"prepared"/f"{number:03d}"
        if mode == "prepared" and not prepared.is_dir():
            raise ValueError(f"Missing reviewed scene assets: {prepared}")
        report = render_episode(folder, rows, sha, prepared, fixture=mode == "fixture", invalidated=invalidated)
        if mode == "prepared":
            youtube = read_json(prepared/"youtube.json")
            for lang in ("ru", "en"):
                meta = youtube[lang]
                if not 0 < len(meta["title"].strip()) <= 100 or not 0 < len(meta["description"].strip()) <= 5000:
                    raise ValueError("Incomplete localized YouTube metadata.")
        else:
            youtube = {lang: {"title": "TEST FIXTURE", "description": "Synthetic production check."}
                       for lang in ("ru", "en")}
        write(folder/"youtube.json", youtube)
    else:
        report = {"mode": mode, "source_coverage": "exact", "scenes": len(rows),
                  "media": "not run", "release": "requires owner approval"}
    write(folder/"report.json", report)
    return folder


def work(home, start_ollama=False, target=None, mode=None, reset=False, invalidated=()):
    path = home/"world.sqlite"
    with exclusive(home/"worker"):
        with ledger(path) as db:
            interrupted = db.execute("SELECT * FROM job WHERE status='running'").fetchall()
            for row in interrupted:
                journal(db, row["chapter"], row["mode"], "interrupted", "Resumed after worker exit.")
            db.execute("UPDATE job SET status='queued' WHERE status='running'")
            if reset:
                db.execute("UPDATE job SET status='queued', failures=0, detail='' WHERE chapter=? AND mode=?",
                           (target, mode))
                journal(db, target, mode, "requeued")
            row = db.execute(
                "SELECT j.*,c.source,c.sha FROM job j JOIN chapter c ON j.chapter=c.number "
                "WHERE j.status='queued' AND j.failures<? AND (? IS NULL OR j.chapter=?) "
                "AND (? IS NULL OR j.mode=?) ORDER BY j.chapter,j.mode LIMIT 1",
                (ATTEMPTS, target, target, mode, mode)
            ).fetchone()
            if not row:
                return None
            number, jobmode = row["chapter"], row["mode"]
            db.execute("UPDATE job SET status='running' WHERE chapter=? AND mode=?", (number, jobmode))
            journal(db, number, jobmode, "running")
        try:
            folder = process(home, row, start_ollama, invalidated)
        except CATCHABLE as error:
            with ledger(path) as db:
                failures = row["failures"] + 1
                status = "blocked" if failures >= ATTEMPTS else "queued"
                detail = f"{type(error).__name__}: {error}"
                db.execute("UPDATE job SET status=?,failures=?,detail=? WHERE chapter=? AND mode=?",
                           (status, failures, detail, number, jobmode))
                journal(db, number, jobmode, status, detail)
            raise JobFailed(detail) from error
        with ledger(path) as db:
            db.execute("UPDATE job SET status='done',detail='' WHERE chapter=? AND mode=?", (number, jobmode))
            journal(db, number, jobmode, "done")
        return folder


def backup(home, target):
    target = target.resolve()
    if target.exists():
        raise ValueError("Backup already exists.")
    with exclusive(home/"worker"):
        scratch = home/"worker"/"backup.sqlite"
        with ledger(home/"world.sqlite", readonly=True) as source, closing(sqlite3.connect(scratch)) as copy:
            source.backup(copy)
            items = copy.execute("SELECT DISTINCT hash,extension FROM asset ORDER BY hash").fetchall()
        stage = target.with_suffix(target.suffix+".partial")
        with ZipFile(stage, "x", compression=ZIP_DEFLATED) as out:
            out.write(scratch, "world.sqlite")
            out.write(SEED, "lore/studio2-seed.json")
            for sha, ext in items:
                name = sha + ext
                asset = home/"library"/name
                if file_hash(asset) != sha:
                    raise ValueError("Backup source failed content verification.")
                out.write(asset, "library/" + name)
        with ZipFile(stage) as out:
            if out.testzip():
                raise ValueError("Backup archive verification failed.")
        stage.rename(target)


def generate_references(home, chapter, limit, entity_name, launch=False):
    from setup_windows import run_comfy
    from art_style_lock import require_backend
    require_backend("local-comfyui")

    with exclusive(home/"worker"):
        with ledger(home/"world.sqlite") as db:
            item = db.execute("SELECT source,sha FROM chapter WHERE number=?", (chapter,)).fetchone()
            if not item:
                raise ValueError("Chapter is not registered.")
        folder, rows, _ = production_plan(home, {**dict(item), "chapter": chapter, "mode": "references"})
        requests = read_json(folder/"reference-requests.json")
        selected = [entry for entry in requests if not entity_name or entry["name"] == entity_name][:limit]
        if not selected:
            return []
        results = []
        with run_comfy() if launch else nullcontext():
            provider = ComfyUI(settings())
            for entry in selected:
                try:
                    with ledger(home/"world.sqlite") as db:
                        result = single_candidate(db, home, chapter, entry, provider,
                                                  rows[entry["scene"]-1]["text"],
                                                  ROOT/"assets/characters/jinhyeok-approved.png")
                        results.append(result)
                        journal(db, chapter, "references", "candidate", entry["name"] + " " + entry["slot"])
                except CATCHABLE as error:
                    with ledger(home/"world.sqlite") as db:
                        journal(db, chapter, "references", "candidate_failed", str(error))
                    raise
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=ROOT/"output/studio2")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("enqueue")
    add.add_argument("chapter", type=int)
    add.add_argument("source", type=Path)
    add.add_argument("--mode", choices=MODES, default="plan")
    run = commands.add_parser("work")
    run.add_argument("--chapter", type=int)
    run.add_argument("--mode", choices=MODES)
    run.add_argument("--start-ollama", action="store_true")
    run.add_argument("--requeue", action="store_true")
    run.add_argument("--drain", action="store_true")
    run.add_argument("--rerender-scene", type=int, action="append", default=[])
    commands.add_parser("status")
    save = commands.add_parser("backup")
    save.add_argument("destination", type=Path)
    refs = commands.add_parser("references")
    refs.add_argument("chapter", type=int)
    refs.add_argument("--limit", type=int, choices=range(1, 9), default=3)
    refs.add_argument("--entity")
    refs.add_argument("--start-comfy", action="store_true")
    args = parser.parse_args()
    root = args.home.resolve()
    try:
        if args.command == "enqueue":
            print(enqueue(root, args.chapter, args.source, args.mode))
        elif args.command == "work":
            if (args.requeue or args.rerender_scene) and not (args.chapter and args.mode):
                parser.error("Specify --chapter and --mode for explicit reruns.")
            if args.drain and (args.requeue or args.rerender_scene):
                parser.error("Use an explicit rerun separately from --drain.")
            if args.drain:
                while True:
                    try:
                        result = work(root, args.start_ollama, args.chapter, args.mode)
                    except JobFailed as issue:
                        print(f"Worker attempt: {issue}", flush=True)
                        with ledger(root/"world.sqlite", readonly=True) as db:
                            next_job = db.execute("SELECT 1 FROM job WHERE status='queued' AND failures<? "
                                                  "AND (? IS NULL OR chapter=?) AND (? IS NULL OR mode=?)",
                                                  (ATTEMPTS, args.chapter, args.chapter, args.mode, args.mode)).fetchone()
                        if not next_job:
                            break
                        continue
                    if not result:
                        break
                    print(result, flush=True)
                with ledger(root/"world.sqlite", readonly=True) as db:
                    pending = db.execute("SELECT 1 FROM job WHERE status='blocked' "
                                         "AND (? IS NULL OR chapter=?) AND (? IS NULL OR mode=?)",
                                         (args.chapter,args.chapter,args.mode,args.mode)).fetchone()
                if pending:
                    parser.exit(2, "The queue has blocked jobs. See status.\n")
                return
            result = work(root, args.start_ollama, args.chapter, args.mode,
                          args.requeue or bool(args.rerender_scene), args.rerender_scene)
            print(result or "Queue empty.")
        elif args.command == "status":
            with ledger(root/"world.sqlite", readonly=True) as db:
                print(json.dumps([dict(row) for row in db.execute("SELECT * FROM job ORDER BY chapter,mode")],
                                 ensure_ascii=False, indent=2))
        elif args.command == "references":
            print(json.dumps(generate_references(root, args.chapter, args.limit, args.entity, args.start_comfy),
                             ensure_ascii=False, indent=2))
        else:
            backup(root, args.destination)
    except CATCHABLE as error:
        parser.exit(2, f"{type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()

