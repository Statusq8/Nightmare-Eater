"""Regression coverage for continuity, extraction and a resumable chapter queue."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from zipfile import ZipFile

from studio2 import JobFailed, backup, enqueue, work
from studio import probe
from render import ffmpeg
from studio2_assets import single_candidate
from studio2_media import encode_scene, fixture_assets
from studio_director import scenes, validate_direction
from world_memory import archive_asset, fact, identity, ingest, ledger, references, snapshot


class Studio2Test(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.home = Path(self.fixture.name)

    def test_scene_partition_retains_every_character(self):
        source = "# Глава\n\nПервый абзац.\n\n***\n\nФинал.\n"
        rows = scenes(source, limit=10, cuts=[source.index("Финал.")])
        self.assertEqual("".join(row["text"] for row in rows), source)
        self.assertEqual(rows[-1]["end"], len(source))

    def test_locked_identity_story_changes_and_future_snapshot(self):
        with ledger(self.home/"world.sqlite") as db:
            hero = identity(db, "character", "Кан Джинхёк", ["Джинхёк"])
            self.assertEqual(identity(db, "character", "джинхёк"), hero)
            fact(db, hero, "outfit", "old", 0, "approved evidence", locked=True)
            ingest(db, 1, "The coat tore. He took a different coat.")
            with self.assertRaisesRegex(ValueError, "locked canon"):
                fact(db, hero, "outfit", "new", 1, "He took a different coat.")
            fact(db, hero, "outfit", "new", 1, "He took a different coat.", story_change=True)
            self.assertEqual(snapshot(db, hero, 1, 0)["outfit"], "old")
            self.assertEqual(snapshot(db, hero, 2, 0)["outfit"], "new")
            with self.assertRaisesRegex(ValueError, "immutable"):
                fact(db, hero, "outfit", "third", 1, "He took a different coat.", story_change=True)

    def test_approved_asset_is_stable_and_backup_contains_blob(self):
        a, b = self.home/"a.png", self.home/"b.png"
        a.write_bytes(b"tested canonical bytes")
        b.write_bytes(b"different candidate bytes")
        with ledger(self.home/"world.sqlite") as db:
            obj = identity(db, "prop", "door")
            archived = archive_asset(db, self.home/"library", obj, "front", 0, -1, a, "test", "test", "approved")
            archive_asset(db, self.home/"library", obj, "front", 1, 0, b, "test", "test")
            self.assertEqual(references(db,obj,2,0)["front"]["hash"], archived.stem)
            with self.assertRaisesRegex(ValueError, "cannot be replaced"):
                archive_asset(db, self.home/"library", obj, "front", 0, -1, b, "test", "test", "approved")
        target = self.home/"backup.zip"
        backup(self.home, target)
        with ZipFile(target) as saved:
            self.assertIn("world.sqlite", saved.namelist())
            self.assertEqual(saved.read("library/"+archived.name), a.read_bytes())

    def test_missing_source_and_unknown_character_rejected(self):
        row = {"text": "Начало. Финал.", "entities": []}
        shot = {"camera": "static", "prompt": "scene", "emotion": "tense", "sound": [], "entities": [],
                "quote": "Начало."}
        data = {"shots": [shot], "lines": [{"speaker": "narrator", "quote": "Начало.", "en": "Beginning."}]}
        with self.assertRaisesRegex(ValueError, "ending"):
            validate_direction(row, data)
        data["lines"].append({"speaker": "narrator", "quote": "Финал.", "en": "End."})
        validate_direction(row, data)
        shot["entities"] = ["unknown"]
        with self.assertRaisesRegex(ValueError, "unresolved"):
            validate_direction(row, data)

    def test_interrupted_queue_resumes_and_source_cannot_be_replaced(self):
        chapter = self.home/"002.md"
        chapter.write_text("Неизменный исходник. Финальная строка.", encoding="utf-8")
        enqueue(self.home, 2, chapter, "plan")
        with ledger(self.home/"world.sqlite") as db:
            db.execute("UPDATE job SET status='running'")
        result = work(self.home)
        self.assertIsNotNone(result)
        plan = json.loads((result/"plan.json").read_text("utf-8"))
        self.assertEqual("".join(row["text"] for row in plan["scenes"]), chapter.read_text("utf-8"))
        self.assertIsNone(work(self.home))
        with ledger(self.home/"world.sqlite", readonly=True) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT status FROM journal ORDER BY id")],
                             ["queued", "interrupted", "running", "done"])
        chapter.write_text("Чужой исходник.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different source"):
            enqueue(self.home, 2, chapter, "plan")

    def test_missing_production_media_blocks_after_two_attempts(self):
        source = self.home/"002.md"
        source.write_text("Проверка незавершённого задания.", encoding="utf-8")
        enqueue(self.home, 2, source, "prepared")
        for _ in range(2):
            with self.assertRaisesRegex(JobFailed, "Missing reviewed scene assets"):
                work(self.home)
        self.assertIsNone(work(self.home))
        with ledger(self.home/"world.sqlite", readonly=True) as db:
            self.assertEqual(tuple(db.execute("SELECT status,failures FROM job").fetchone()), ("blocked", 2))

    def test_multishot_scene_has_exact_picture_frame_count(self):
        for number in (1, 2):
            folder = self.home/str(number)
            fixture_assets(folder, {"id": number})
            (folder/"panel.png").rename(self.home/f"shot-{number:03}.png")
        target = self.home/"scene.mp4"
        encode_scene(self.home, target, [
            {"file": "shot-001.png", "camera": "static", "frames": 6},
            {"file": "shot-002.png", "camera": "push", "frames": 9},
        ], 15)
        self.assertEqual(probe(target)["streams"][0]["nb_frames"], "15")

    def test_reference_qc_retries_bright_aperture_and_keeps_rejection(self):
        for name, color in (("bright.png", "white"), ("dark.png", "0x192432")):
            ffmpeg("-f", "lavfi", "-i", "color=c="+color+":s=1024x576", "-frames:v", 1,
                   "-update", 1, self.home/name, cwd=self.home)
        layout = self.home/"layout.json"
        layout.write_text('{"maximum_bright_fraction": 0.065}', encoding="utf-8")

        class FakeComfy:
            calls = 0

            def image(inner, prompt, seed, reference, target, journal):
                inner.calls += 1
                shutil.copyfile(self.home/("bright.png" if inner.calls == 1 else "dark.png"), target)

        with ledger(self.home/"world.sqlite") as db:
            room = identity(db, "location", "Палата")
            archive_asset(db, self.home/"library", room, "layout/topology", 0, -1, layout,
                          "test layout", "synthetic")
            provider = FakeComfy()
            item = single_candidate(db, self.home, 1, {"entity": room, "slot": "visual/establishing",
                                    "position": 0, "kind": "location", "name": "Палата",
                                    "view": "establishing"}, provider, "Dark ward.", self.home/"dark.png")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(item["state"], "candidate")
        statuses = [json.loads(p.read_text("utf-8"))["status"]
                    for p in (self.home/"candidate-work").glob("*.review.json")]
        self.assertCountEqual(statuses, ["rejected", "candidate"])


if __name__ == "__main__":
    unittest.main()

