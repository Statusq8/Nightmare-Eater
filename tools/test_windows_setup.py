"""Exercise the pinned profile without downloading or starting model servers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import setup_windows as setup
from studio import read, validate_plan
from studio_providers import ComfyUI


class SetupTests(unittest.TestCase):
    def test_resumed_fetch_keeps_the_downloaded_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            part = cache / "asset.bin.part"
            part.write_bytes(b"abc")

            def get_range(args, text):
                self.assertEqual(args[args.index("--range") + 1], "3-5")
                Path(args[args.index("--output") + 1]).write_bytes(b"def")
                return "206"

            with patch.object(setup, "CACHE", cache), patch.object(
                setup.subprocess, "check_output", side_effect=get_range
            ):
                result = setup.download("https://example.test/file", "asset.bin",
                                        "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721", 6)
            self.assertEqual(result.read_bytes(), b"abcdef")
            self.assertFalse(part.exists())

    def test_pilot_anchors_and_reference_graph(self):
        root = setup.ROOT
        chapter = read(root / "chapters/001.md")
        plan = json.loads(read(root / "youtube/storyboards/001-pilot.plan.json"))
        validate_plan(plan, chapter, 7)
        self.assertEqual(len(plan["shots"]), 7)
        self.assertIn("Пожиратель", plan["title"])
        config = {"comfy_workflow": str(root / "tools/workflows/sdxl-ipadapter.json"),
                  "comfy_url": "http://127.0.0.1:8189", "comfy_output_node": "11",
                  "comfy_trusted_nodes": "IPAdapterModelLoader,IPAdapterAdvanced"}
        adapter = ComfyUI(config)
        info = {node["class_type"]: {"python_module": "nodes"} for node in adapter.workflow.values()}
        with patch("studio_providers.local_call", return_value=info):
            adapter.validate()
        self.assertTrue(adapter.checked)


if __name__ == "__main__":
    unittest.main()
