"""SQLite continuity ledger. Narrative state changes only with exact chapter evidence."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata
import uuid

KINDS = {"character", "location", "prop", "anomaly", "voice", "system"}
NAMESPACE = uuid.UUID("8b7cbeb0-8b7e-4596-b617-957a35b6902b")
SCHEMA = """
CREATE TABLE IF NOT EXISTS chapter(
 number INTEGER PRIMARY KEY, source TEXT NOT NULL, sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, UNIQUE(kind,name)
);
CREATE TABLE IF NOT EXISTS alias(
 kind TEXT NOT NULL, label TEXT NOT NULL, entity TEXT NOT NULL REFERENCES entity(id),
 PRIMARY KEY(kind,label)
);
CREATE TABLE IF NOT EXISTS fact(
 entity TEXT NOT NULL REFERENCES entity(id), field TEXT NOT NULL, chapter INTEGER NOT NULL,
 position INTEGER NOT NULL, value TEXT NOT NULL, evidence TEXT NOT NULL, locked INTEGER NOT NULL,
 PRIMARY KEY(entity,field,chapter,position)
);
CREATE TABLE IF NOT EXISTS scene(
 chapter INTEGER NOT NULL REFERENCES chapter(number), ordinal INTEGER NOT NULL,
 start INTEGER NOT NULL, stop INTEGER NOT NULL, location TEXT REFERENCES entity(id),
 PRIMARY KEY(chapter,ordinal)
);
CREATE TABLE IF NOT EXISTS chapter_location(
 chapter INTEGER NOT NULL REFERENCES chapter(number), position INTEGER NOT NULL,
 entity TEXT NOT NULL REFERENCES entity(id), evidence TEXT NOT NULL,
 PRIMARY KEY(chapter,position)
);
CREATE TABLE IF NOT EXISTS appearance(
 chapter INTEGER NOT NULL, scene INTEGER NOT NULL, entity TEXT NOT NULL REFERENCES entity(id),
 PRIMARY KEY(chapter,scene,entity), FOREIGN KEY (chapter,scene) REFERENCES scene(chapter,ordinal)
);
CREATE TABLE IF NOT EXISTS asset(
 entity TEXT NOT NULL REFERENCES entity(id), slot TEXT NOT NULL, chapter INTEGER NOT NULL,
 position INTEGER NOT NULL, hash TEXT NOT NULL, extension TEXT NOT NULL, state TEXT NOT NULL,
 source TEXT NOT NULL, license TEXT NOT NULL, PRIMARY KEY(entity,slot,chapter,position,hash)
);
CREATE TABLE IF NOT EXISTS proposal(
 chapter INTEGER NOT NULL REFERENCES chapter(number), hash TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(chapter,hash)
);
CREATE TABLE IF NOT EXISTS job(
 chapter INTEGER NOT NULL REFERENCES chapter(number), mode TEXT NOT NULL,
 status TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(chapter,mode)
);
CREATE TABLE IF NOT EXISTS journal(
 id INTEGER PRIMARY KEY, time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 chapter INTEGER NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
);
"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checksum(blob):
    return hashlib.sha256(blob).hexdigest()


def normalized(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


@contextmanager
def ledger(path, *, readonly=False):
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        db.executescript(SCHEMA)
    try:
        with db:
            yield db
    finally:
        db.close()


def identity(db, kind, name, aliases=()):
    if kind not in KINDS or not isinstance(name, str) or not normalized(name):
        raise ValueError("Invalid entity.")
    key = normalized(name)
    existing = db.execute("SELECT entity FROM alias WHERE kind=? AND label=?", (kind, key)).fetchone()
    ident = existing[0] if existing else str(uuid.uuid5(NAMESPACE, kind + ":" + key))
    db.execute("INSERT OR IGNORE INTO entity VALUES(?,?,?)", (ident, kind, name))
    for label in {key, *(normalized(a) for a in aliases)}:
        row = db.execute("SELECT entity FROM alias WHERE kind=? AND label=?", (kind, label)).fetchone()
        if not label or (row and row[0] != ident):
            raise ValueError(f"Ambiguous {kind} alias: {label}")
        db.execute("INSERT OR IGNORE INTO alias VALUES(?,?,?)", (kind, label, ident))
    return ident


def ingest(db, number, text):
    if type(number) is not int or number < 1 or not text.strip():
        raise ValueError("Expected a numbered, nonempty chapter.")
    sha = checksum(text.encode("utf-8"))
    existing = db.execute("SELECT sha FROM chapter WHERE number=?", (number,)).fetchone()
    if existing and existing["sha"] != sha:
        raise ValueError("Chapter already registered with a different source; preserve or explicitly migrate it.")
    db.execute("INSERT OR IGNORE INTO chapter VALUES(?,?,?)", (number, text, sha))
    return sha


def snapshot(db, ident, chapter, position):
    rows = db.execute(
        "SELECT field,value FROM fact WHERE entity=? AND (chapter,position)<=(?,?) ORDER BY chapter,position",
        (ident, chapter, position)
    )
    result = {}
    for row in rows:
        result[row["field"]] = json.loads(row["value"])
    return result


def fact(db, ident, field, value, chapter, evidence, *, locked=False, story_change=False, offset=None):
    if chapter == 0:
        position = -1
        if not locked or not evidence:
            raise ValueError("Chapter zero is reserved for sourced, locked canon.")
    else:
        row = db.execute("SELECT source FROM chapter WHERE number=?", (chapter,)).fetchone()
        source = row[0] if row else ""
        position = source.find(evidence) if offset is None else offset
        if (not evidence.strip() or position < 0 or not source.startswith(evidence, position)):
            raise ValueError("Fact has no exact source evidence.")
        if offset is None and source.find(evidence, position + 1) >= 0:
            raise ValueError("Repeated evidence requires an exact offset.")
    values = (ident, field, chapter, position, canonical(value), evidence, int(locked))
    prior = db.execute(
        "SELECT value,evidence,locked FROM fact WHERE entity=? AND field=? AND chapter=? AND position=?",
        values[:4]
    ).fetchone()
    if prior:
        if tuple(prior) != values[4:]:
            raise ValueError("A fact version is immutable.")
        return
    prior_lock = db.execute(
        "SELECT 1 FROM fact WHERE entity=? AND field=? AND (chapter,position)<=(?,?) AND locked=1",
        values[:4]
    ).fetchone()
    if chapter and prior_lock and snapshot(db, ident, chapter, position).get(field) != value and not story_change:
        raise ValueError("Changing locked canon requires a verified story change.")
    db.execute("INSERT INTO fact VALUES(?,?,?,?,?,?,?)", values)


def archive_asset(db, library, ident, slot, chapter, position, path, source, license, state="candidate"):
    if state not in {"approved", "candidate"} or not source or not license:
        raise ValueError("Asset provenance is required.")
    if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".webp", ".wav", ".json"}:
        raise ValueError(f"Unsupported asset: {path}")
    content = path.read_bytes()
    sha = checksum(content)
    extension = path.suffix.lower()
    target = library / (sha + extension)
    library.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if checksum(target.read_bytes()) != sha:
            raise ValueError("Corrupt asset library.")
    else:
        staging = target.with_suffix(".partial")
        staging.write_bytes(content)
        staging.replace(target)
    previous = db.execute(
        "SELECT hash FROM asset WHERE entity=? AND slot=? AND chapter=? AND position=? AND state='approved'",
        (ident, slot, chapter, position)
    ).fetchone()
    if state == "approved" and previous and previous["hash"] != sha:
        raise ValueError("An approved asset version cannot be replaced.")
    db.execute(
        "INSERT INTO asset VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(entity,slot,chapter,position,hash) "
        "DO UPDATE SET state='approved' WHERE excluded.state='approved'",
        (ident, slot, chapter, position, sha, extension, state, source, license)
    )
    return target


def references(db, ident, chapter, position, *, include_candidates=False):
    rows = db.execute(
        "SELECT * FROM asset WHERE entity=? AND (chapter,position)<=(?,?) "
        "AND (state='approved' OR ?=1) ORDER BY chapter,position,state DESC,hash",
        (ident, chapter, position, int(include_candidates))
    )
    return {row["slot"]: dict(row) for row in rows}

