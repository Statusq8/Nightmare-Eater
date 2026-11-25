"""Checks runnable on Windows with only FFmpeg and Python."""
from argparse import Namespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import studio
from studio_providers import ComfyUI, Ollama, local_call


class StudioTest(unittest.TestCase):
    def test_dry_run_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(studio, "ROOT", Path(tmp)):
            with patch("sys.argv", ["studio.py", "001", "--synthetic"]), patch.object(studio, "execute") as execute:
                studio.main()
                execute.assert_not_called()
                self.assertFalse((Path(tmp) / "output").exists())

    def test_resumes_after_failed_stage_and_checks_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = studio.Run(root)
            first.stage("one", [1], ["one.txt"], lambda p: p.write_text("one"))
            with self.assertRaises(RuntimeError):
                first.stage("two", [2], ["two.txt"], lambda _: self.fail_stage())
            self.assertFalse((root / "two.txt").exists())
            second = studio.Run(root)
            second.stage("one", [1], ["one.txt"], lambda _: self.fail("completed stage ran again"))
            second.stage("two", [2], ["two.txt"], lambda p: p.write_text("two"))
            (root / "one.txt").write_text("corrupted")
            studio.Run(root).stage("one", [1], ["one.txt"], lambda p: p.write_text("one"))
            self.assertEqual((root / "one.txt").read_text(), "one")
            self.assertEqual((root / "two.txt").read_text(), "two")

    @staticmethod
    def fail_stage():
        raise RuntimeError("test failure")

    def test_synthetic_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = Namespace(synthetic=True, max_shots=2)
            source = ("# Fixture\n\nСинтетический исходник.", "", "")
            config = {"language": "Russian"}
            studio.execute(root, config, args, source, {"test": "synthetic-v1"})
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["mode"], "synthetic")
            self.assertEqual(report["shots"], 2)
            self.assertEqual(report["subtitle_cues"], 2)
            self.assertGreater(report["audio_mean_db"], -60)
            self.assertEqual(report["publish"], "manual only")
            for name in ("episode.mp4", "thumbnail.jpg", "youtube.json", "storyboard.json", "manifest.json"):
                self.assertGreater((root / name).stat().st_size, 0)

    def test_rejects_remote_or_redirecting_origin(self):
        for address in ("https://example.com", "http://127.0.0.1.example.com", "http://127.0.0.1/proxy"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                local_call(address, "/api/generate", {"prompt": "private"})

    def test_comfy_uses_reference_and_resumes_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = {
                "1": {"class_type": "LoadImage", "inputs": {"image": "$REFERENCE"}},
                "2": {"class_type": "TestSampler", "inputs": {
                    "image": ["1", 0], "prompt": "$PROMPT", "seed": "$SEED"}},
                "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
            }
            studio.write(root / "graph.json", workflow)
            ref = root / "reference.png"
            ref.write_bytes(b"reference fixture")
            config = {"comfy_workflow": str(root / "graph.json"), "comfy_url": "http://localhost:8188",
                      "comfy_output_node": "3", "comfy_trusted_nodes": ""}
            adapter = ComfyUI(config)
            job_id = "a65b639a-32e4-41f6-b0df-c398f2e95059"
            history = {job_id: {"outputs": {"3": {"images": [
                {"filename": "image.png", "subfolder": "", "type": "output"}]}}}}
            routes = []
            def reply(base, path, payload=None, mime=None, binary=False):
                routes.append(path)
                if path == "/object_info":
                    return {node["class_type"]: {"python_module": "nodes"} for node in workflow.values()}
                if path == "/upload/image":
                    self.assertIn(b"reference fixture", payload)
                    return {"name": "ref.png"}
                if path == "/prompt":
                    self.assertEqual(payload["prompt"]["1"]["inputs"]["image"], "ref.png")
                    self.assertEqual(payload["prompt"]["2"]["inputs"]["seed"], 42)
                    return {"prompt_id": job_id}
                if path.startswith("/history/"):
                    return history
                if path.startswith("/view?"):
                    return b"image fixture"
                self.fail(path)
            with patch("studio_providers.local_call", side_effect=reply):
                for _ in range(2):
                    adapter.image("panel description", 42, ref, root / "panel.png", root / "job.json")
            self.assertEqual(routes.count("/prompt"), 1)
            self.assertEqual(routes.count("/upload/image"), 1)
            self.assertEqual((root / "panel.png").read_bytes(), b"image fixture")

    def test_ollama_blocks_remote_model(self):
        adapter = Ollama({"ollama_url": "http://localhost:11434"})
        with patch("studio_providers.local_call", return_value={"remote_host": "provider"}) as request:
            with self.assertRaisesRegex(ValueError, "Remote Ollama"):
                adapter.generate("apparently-local", "system", "source")
            self.assertEqual(request.call_count, 1)

    def test_rejects_ungrounded_script(self):
        from studio_providers import Synthetic

        chapter = "Настоящий текст."
        plan = Synthetic().plan(chapter, "", "", "Чужой текст.", 1, "Russian")
        with self.assertRaisesRegex(ValueError, "Source quote"):
            studio.validate_plan(plan, chapter, 1)


if __name__ == "__main__":
    unittest.main()
