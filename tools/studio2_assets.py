"""Generate missing view candidates without mutating established reference versions."""
import json

from studio import file_hash, fingerprint, write
from studio2_qc import check_candidate
from world_memory import archive_asset, references, snapshot

VIEWS = {
    "character": ("front", "profile", "back"),
    "anomaly": ("establishing", "profile", "detail"),
    "location": ("establishing", "reverse", "floor-plan"),
    "prop": ("front", "side", "detail"),
}
DIRECTIONS = {
    "front": "neutral front view", "profile": "clean side profile", "back": "rear turnaround view",
    "establishing": "wide establishing view", "reverse": "reverse angle in the same room geometry",
    "floor-plan": "orthographic overhead view of the identical room geometry",
    "side": "side view", "detail": "canonical close detail",
}


def reference_requests(db, chapter, rows):
    requested = {}
    for row in rows:
        for item in row["entities"]:
            ident, kind, when = item["id"], item["kind"], item["at"]
            existing = references(db, ident, chapter, when, include_candidates=True)
            if "visual/model-sheet" in existing:
                continue
            for view in VIEWS.get(kind, ()):
                slot = f"visual/{view}"
                if slot not in existing:
                    requested.setdefault((ident, slot), {"entity": ident, "name": item["name"], "kind": kind,
                                                         "slot": slot, "view": view, "position": when,
                                                         "scene": row["id"]})
    return list(requested.values())


def single_candidate(db, home, chapter, request, provider, direction, style_reference):
    entity, slot, position = (request[key] for key in ("entity", "slot", "position"))
    current = references(db, entity, chapter, position, include_candidates=True)
    if slot in current:
        return current[slot]
    canon = snapshot(db, entity, chapter, position)
    layout = current.get("layout/topology")
    geometry = None
    if layout:
        stored = home/"library"/(layout["hash"] + layout["extension"])
        if file_hash(stored) != layout["hash"]:
            raise ValueError("Layout file does not match its registry hash.")
        geometry = json.loads(stored.read_text(encoding="utf-8"))
        canon["candidate_layout"] = geometry
    anchor = next((item for key, item in current.items() if key.startswith("visual/")), None)
    reference = home/"library"/(anchor["hash"] + anchor["extension"]) if anchor else style_reference
    if anchor and file_hash(reference) != anchor["hash"]:
        raise ValueError("Reference anchor file does not match its registry hash.")
    view = DIRECTIONS[request["view"]]
    prompt = (
        f"{view} of {request['name']} ({request['kind']}). Known appearance and geometry: {canon}. "
        f"Scene context: {direction}. Cinematic adult Korean ink manhwa, cold graphite and blue slate, "
        "realistic proportions, controlled red accents. One image, no lettering. "
        "Do not copy the attached style subject if this is a new entity."
    )
    work = home/"candidate-work"
    work.mkdir(exist_ok=True)
    failures = []
    for attempt in range(2):
        constraint = (" Match the recorded solid door and dark room. No bright opening or extra wall window."
                      if geometry and attempt else "")
        key = fingerprint([request, chapter, prompt, file_hash(reference), attempt])[:16]
        output = work/(key+".png")
        if not output.is_file():
            provider.image(prompt + constraint, int(key[:8], 16), reference, output, work/(key+".job.json"))
        review = work/(key+".review.json")
        try:
            measures = check_candidate(output, geometry)
            if any(asset["hash"] == file_hash(output) for asset in current.values()):
                raise ValueError("Candidate repeats another view.")
        except ValueError as error:
            reason = str(error)
            write(review, {"status": "rejected", "attempt": attempt + 1, "reason": reason})
            failures.append(reason)
            continue
        write(review, {"status": "candidate", "attempt": attempt + 1, **measures})
        archive_asset(db, home/"library", entity, slot, chapter, position, output,
                      "Local ComfyUI candidate for " + request["name"], "Local SDXL OpenRAIL-M source weights",
                      "candidate")
        return references(db, entity, chapter, position, include_candidates=True)[slot]
    raise ValueError("Reference held after two candidates: " + " | ".join(failures))

