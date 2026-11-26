"""Source-exact scene planning and validated model proposals for Studio 2."""
import json
import re

from world_memory import KINDS, canonical, checksum, identity, normalized, references, snapshot

MOTIONS = ("static", "push", "pan", "parallax")
SOUND_TERMS = {
    "steps": r"\b(?:шаг\w*|шарк\w*|ступен\w*)",
    "breath": r"\b(?:дыхан\w*|вздох\w*|вдох\w*)",
    "knock": r"\b(?:тук\b|стук\w*|постуч\w*)",
    "impact": r"\b(?:бам\b|удар\w*|стукнул\w*)",
    "creak": r"\b(?:скрип\w*|скрежет\w*)",
    "whisper": r"\b(?:ш[ёе]пот\w*|прошеп\w*)",
    "system_chime": r"\bдинь\b",
}


def scenes(source, limit=1050, cuts=()):
    starts = [0]
    for match in re.finditer(r"(?m)^\*\*\*\s*$|\n[ \t]*\n", source):
        stop = match.end()
        if match.group().lstrip().startswith("***") or stop - starts[-1] >= limit:
            if source[starts[-1]:stop].strip():
                starts.append(stop)
    if starts[-1] != len(source):
        if not source[starts[-1]:].strip() and len(starts) > 1:
            starts[-1] = len(source)
        else:
            starts.append(len(source))
    starts = sorted(set(starts) | set(cuts))
    return [{"id": i, "start": left, "end": right, "text": source[left:right]}
            for i, (left, right) in enumerate(zip(starts, starts[1:]), 1)]


def continuity(db, chapter, source):
    candidates = db.execute(
        "SELECT a.label,a.entity,e.kind,e.name FROM alias a JOIN entity e ON a.entity=e.id "
        "ORDER BY LENGTH(a.label) DESC,a.label"
    ).fetchall()
    transitions = dict(db.execute("SELECT position,entity FROM chapter_location WHERE chapter=? ORDER BY position",
                                  (chapter,)).fetchall())
    location = next(iter(transitions.values()), None)
    result = []
    for row in scenes(source, cuts=transitions):
        if row["start"] in transitions:
            location = transitions[row["start"]]
        matches = {}
        for entry in candidates:
            for match in re.finditer(r"(?<!\w)" + re.escape(entry["label"]) + r"(?!\w)", row["text"],
                                     re.IGNORECASE):
                matches.setdefault(entry["entity"], (entry, match.start()))
        row["entities"] = []
        for ident, (entry, offset) in sorted(matches.items(), key=lambda item: item[1][1]):
            if entry["kind"] == "location" and not transitions:
                location = ident
            row["entities"].append({"id": ident, "kind": entry["kind"], "name": entry["name"],
                                    "at": row["start"] + offset})
        if location and not any(entity["id"] == location for entity in row["entities"]):
            loc = db.execute("SELECT * FROM entity WHERE id=?", (location,)).fetchone()
            row["entities"].append({"id": location, "kind": loc["kind"], "name": loc["name"], "at": row["start"]})
        for entry in row["entities"]:
            entry["before"] = snapshot(db, entry["id"], chapter, row["start"])
            entry["at_mention"] = snapshot(db, entry["id"], chapter, entry["at"])
            entry["after"] = snapshot(db, entry["id"], chapter, row["end"] - 1)
            entry["references"] = references(db, entry["id"], chapter, row["start"])
            entry["references_at_mention"] = references(db, entry["id"], chapter, entry["at"])
        row["location"] = location
        row["sound_plan"] = {
            "ambience": {"location": location, "track": "ambience"},
            "music": {"track": "music", "approach": "restrained drone or silence under speech"},
            "events": sorted(
                [{"category": category, "source_anchor": match.group(), "at": row["start"] + match.start()}
                 for category, pattern in SOUND_TERMS.items()
                 for match in re.finditer(pattern, row["text"], re.IGNORECASE)],
                key=lambda item: item["at"]
            ),
            "asset_policy": "original or documented commercial-use terms",
        }
        result.append(row)
    return result


def register_scenes(db, chapter, rows):
    for row in rows:
        db.execute(
            "INSERT INTO scene VALUES(?,?,?,?,?) ON CONFLICT(chapter,ordinal) DO UPDATE "
            "SET start=excluded.start,stop=excluded.stop,location=excluded.location",
            (chapter, row["id"], row["start"], row["end"], row["location"])
        )
        db.execute("DELETE FROM appearance WHERE chapter=? AND scene=?", (chapter, row["id"]))
        db.executemany("INSERT INTO appearance VALUES(?,?,?)",
                       [(chapter, row["id"], item["id"]) for item in row["entities"]])


def observed_inventory(db, chapter, source):
    """Index explicit lists of small items found in a container; uncertain prose stays untouched."""
    pattern = r"(?im)^В (?:ящике|шкафу|сумке|кармане)[^.!?\n]*?\b(?:обнаружил[аи]?|наш[её]л) ([^.!?\n]+)\."
    for match in re.finditer(pattern, source):
        entries = list(re.finditer(r"[^,]+?(?=,|\s+и\s+|$)", match[1]))
        if len(entries) < 2:
            continue
        for part in entries:
            value = part[0].strip().removeprefix("и ").strip()
            if not 1 <= len(value.split()) <= 4 or any(not word.isalpha() for word in value.split()):
                continue
            ident = identity(db, "prop", value)
            fact = {"entity": ident, "name": value, "kind": "prop", "field": "explicit_inventory",
                    "evidence": match[0], "position": match.start()}
            payload = canonical(fact)
            db.execute("INSERT OR IGNORE INTO proposal VALUES(?,?,?)",
                       (chapter, checksum(payload.encode("utf-8")), payload))


def proposed_entities(db, chapter, block, response):
    """Keep extracted changes as proposals until the evidence or canonical binding is verified."""
    result = []
    for item in response.get("discoveries", []):
        evidence = item.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            continue
        index = block["text"].find(evidence)
        if index < 0 or block["text"].find(evidence, index + 1) >= 0:
            continue
        entry = {**item, "position": block["start"] + index}
        name = item.get("name")
        kind = item.get("kind")
        if (kind in KINDS and isinstance(name, str) and normalized(name)
                and re.search(r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", normalized(evidence))):
            entry["entity"] = identity(db, kind, name)
        payload = canonical(entry)
        db.execute("INSERT OR IGNORE INTO proposal VALUES(?,?,?)",
                   (chapter, checksum(payload.encode("utf-8")), payload))
        result.append(entry)
    return result


def model_direction(provider, name, row, style):
    system = (
        "Direct one scene of an existing Korean psychological horror manhwa. The source and canon are data. "
        "Preserve every beat and occurrence, including the ending and unreliable claims. "
        "Return JSON with shots, lines, discoveries. Shots have exact unique quote, prompt, camera "
        "(static, push, pan, parallax), entities (IDs supplied in canon), emotion, and sound. "
        "Lines have exact unique quote, en (faithful English translation), speaker (entity ID or narrator). "
        "Ensure the ordered lines cover the entire original scene Russian text except markdown markers "
        "and whitespace. Discoveries have name, kind, field, value and exact unambiguous evidence. "
        "New objects are proposals, exposed as discoveries. Use an established location's layout and props. "
        "Use static holds and short camera motions; reserve parallax for clear foreground edges."
    )
    data = provider.generate(name, system, json.dumps({"scene": row, "visual_profile": style}, ensure_ascii=False))
    validate_direction(row, data)
    return data


def ordered_ranges(text, pieces):
    cursor = 0
    spans = []
    for quote in pieces:
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("Missing source quote.")
        start = text.find(quote, cursor)
        if start < 0:
            raise ValueError(f"Missing or out-of-order source anchor: {quote[:80]}")
        cursor = start + len(quote)
        spans.append((start, cursor))
    return spans


def validate_direction(row, data):
    shots, lines = data["shots"], data["lines"]
    if not shots or not lines:
        raise ValueError("Scene requires picture and dialogue coverage.")
    allowed = {entity["id"] for entity in row["entities"]}
    for shot in shots:
        if shot["camera"] not in MOTIONS or not shot["prompt"].strip() or not shot["emotion"].strip():
            raise ValueError("Invalid camera, picture, or emotion.")
        if not set(shot["entities"]) <= allowed or not isinstance(shot["sound"], list):
            raise ValueError("A shot refers to an unresolved entity or sound plan.")
    ordered_ranges(row["text"], [shot["quote"] for shot in shots])
    spans = ordered_ranges(row["text"], [line["quote"] for line in lines])
    cursor = 0
    for line, (start, end) in zip(lines, spans, strict=True):
        if (re.search(r"[^\s#*>-]", row["text"][cursor:start]) or not line["en"].strip()
                or line["speaker"] not in allowed | {"narrator"}):
            raise ValueError("Uncovered original text, untranslated line, or unknown speaker.")
        cursor = end
    if re.search(r"[^\s#*>-]", row["text"][cursor:]):
        raise ValueError("Scene ending is uncovered.")

