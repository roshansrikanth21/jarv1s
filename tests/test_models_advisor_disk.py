"""Unit tests for the on-disk GGUF model discovery added to models_advisor -
recognizing already-downloaded local models that Ollama never registered.

Run from repo root:
  .\\venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.host import models_advisor as ma


class DiskModelScanTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "qwen3-8b-instruct.Q4_K_M.gguf").write_bytes(b"x" * 2048)
        sub = self.dir / "nested" / "deeper"
        sub.mkdir(parents=True)
        (sub / "Meta-Llama-3.1-8B.gguf").write_bytes(b"y" * 4096)
        (self.dir / "readme.txt").write_text("not a model")

    def test_scan_finds_gguf_recursively_and_ignores_other_files(self):
        found = ma.scan_disk_models([self.dir])
        names = sorted(m["name"] for m in found)
        self.assertEqual(names, ["Meta-Llama-3.1-8B", "qwen3-8b-instruct.Q4_K_M"])
        self.assertTrue(all(m["source"] == "gguf" for m in found))
        self.assertTrue(all(m["path"].endswith(".gguf") for m in found))

    def test_family_detection(self):
        fams = {m["name"]: m["family"] for m in ma.scan_disk_models([self.dir])}
        self.assertEqual(fams["qwen3-8b-instruct.Q4_K_M"], "qwen")
        self.assertEqual(fams["Meta-Llama-3.1-8B"], "llama")

    def test_max_depth_prunes(self):
        # the nested llama is at depth 2; with max_depth=1 it must not be found
        found = ma.scan_disk_models([self.dir], max_depth=1)
        names = [m["name"] for m in found]
        self.assertIn("qwen3-8b-instruct.Q4_K_M", names)
        self.assertNotIn("Meta-Llama-3.1-8B", names)

    def test_annotate_flags_fit_and_ollama_membership(self):
        dev = {"ram_gb": 16, "ram_available_gb": 12, "headroom": 0.8}
        scanned = ma.scan_disk_models([self.dir])
        ann = ma.annotate_disk_models(dev, scanned, {"qwen3:8b"})
        for m in ann:
            self.assertIn("runnable_now", m)
            self.assertIn("needs_gb", m)
            self.assertTrue(m["importable"])
            self.assertIsNone(m["tools"])        # unknown until imported

    def test_common_model_dirs_honours_env(self):
        marker = tempfile.mkdtemp()
        os.environ["JARVIS_MODEL_DIRS"] = marker + os.pathsep + "/nonexistent/path"
        try:
            dirs = [str(p) for p in ma.common_model_dirs()]
            self.assertIn(marker, dirs)
        finally:
            os.environ.pop("JARVIS_MODEL_DIRS", None)

    def test_suggest_prefers_runnable_ollama_over_disk(self):
        dev = {"ram_gb": 32, "ram_available_gb": 24, "headroom": 0.9}
        installed = [{"name": "qwen3:8b", "gb": 5.2, "tools": True}]
        disk = ma.annotate_disk_models(dev, [{"name": "big", "path": "/x/big.gguf",
                                              "gb": 9.0, "source": "gguf", "family": "qwen"}], set())
        sug = ma.suggest_local(dev, installed, disk)
        self.assertEqual(sug["kind"], "ollama")
        self.assertTrue(sug["ready"])

    def test_import_gguf_rejects_bad_input(self):
        self.assertFalse(ma.import_gguf("/no/such/file.gguf")["ok"])
        txt = self.dir / "x.txt"
        txt.write_text("hi")
        self.assertFalse(ma.import_gguf(str(txt))["ok"])


if __name__ == "__main__":
    unittest.main()
