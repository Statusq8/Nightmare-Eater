"""Run Studio 2.0 planning and hand sealed requests to the configured media worker."""

import os
import subprocess
import sys
from pathlib import Path

from studio_bot import DATA, atomic_json, digest, load_json


def prepare_plans(project):
    home = DATA / "ledger"
    for path in sorted((DATA / "chapters").glob("*/request.json")):
        folder = path.parent
        if (folder / "plan.json").exists():
            continue
        req = load_json(path)
        n = int(req["chapter"])
        source = folder / "source.md"
        cmd = [sys.executable, "-B", str(project / "tools/studio2.py"), "--home", str(home)]
        subprocess.run([*cmd, "enqueue", str(n), str(source)], cwd=project, check=True, timeout=90)
        subprocess.run([*cmd, "work", "--chapter", str(n), "--mode", "plan"], cwd=project, check=True, timeout=180)
        plans = list((home / "runs").glob(f"{n:03}-plan-*/plan.json"))
        if len(plans) != 1:
            raise RuntimeError("Studio 2.0 не вернула однозначный план главы.")
        plan = load_json(plans[0])
        atomic_json(folder / "plan.json", {"chapter_blob": req["chapter_blob"], "scenes": plan["scenes"]})


def produce(generator):
    for path in sorted((DATA / "jobs").glob("*/state.json")):
        state = load_json(path)
        if state["status"] != "queued":
            continue
        root = path.parent
        req = load_json(root / "request.json")
        if req["request_id"] != state["request_id"]:
            continue
        result = root / f"result-{req['request_id']}.json"
        if result.exists():
            continue
        if not generator:
            continue
        approved = req.get("approved_reference")
        if req["phase"] == "scene" and not approved:
            raise RuntimeError("Попытка производства сцены без утверждённых референсов.")
        try:
            for item in (approved or {}).get("files", []):
                candidate = (root / item["file"]).resolve()
                if not candidate.is_relative_to(root.resolve()) or digest(candidate) != item["sha256"]:
                    raise RuntimeError("Контрольная сумма утверждённого референса изменилась.")
            environment = {k: v for k, v in os.environ.items()
                           if k not in {"TELEGRAM_TOKEN", "NE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}}
            subprocess.run([sys.executable, str(generator), str(root / "request.json"), str(result)],
                           cwd=root, env=environment, check=True, timeout=7200)
            if not result.exists():
                raise RuntimeError("Производственный адаптер не вернул материалы.")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            atomic_json(result, {"request_id": req["request_id"], "blocked": str(error)})


def run():
    project = Path(os.environ["NE_PROJECT"]).resolve()
    if not (project / "tools/studio2.py").is_file():
        raise SystemExit("NE_PROJECT должен указывать на рабочую копию Nightmare-Eater со Studio 2.0.")
    prepare_plans(project)
    generator = os.getenv("NE_MEDIA_WORKER")
    produce(Path(generator).resolve(strict=True) if generator else None)


if __name__ == "__main__":
    run()
