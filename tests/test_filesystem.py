"""Tests for the verified filesystem tool (jarvis/act/filesystem.py): every op confirms the
real state and reports honestly, delete is confirm-gated, and a multi-step sequence stops
honestly when a step can't be done. No LLM/UI.
Run: venv\\Scripts\\python -m unittest tests.test_filesystem -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.act import filesystem as fs


class VerifiedFilesystem(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_create_folder_then_file_then_verify(self):
        folder = os.path.join(self.d, "ProjectX")
        self.assertIn("Created folder", fs.run("create_folder", {"path": folder}))
        self.assertTrue(os.path.isdir(folder))
        f = os.path.join(folder, "notes.txt")
        self.assertIn("Created file", fs.run("create_file", {"path": f, "content": "hello"}))
        self.assertEqual(open(f, encoding="utf-8").read(), "hello")
        self.assertIn("is a file", fs.run("exists", {"path": f}))

    def test_open_missing_is_honest(self):
        out = fs.run("open", {"path": os.path.join(self.d, "nope", "ghost.txt")})
        self.assertIn("nothing at", out.lower())

    def test_delete_requires_confirm(self):
        f = os.path.join(self.d, "x.txt")
        fs.run("create_file", {"path": f})
        dry = fs.run("delete", {"path": f})
        self.assertIn("confirm", dry.lower())
        self.assertTrue(os.path.exists(f))            # dry-run must NOT delete
        done = fs.run("delete", {"path": f, "confirm": True})
        self.assertIn("Deleted", done)
        self.assertFalse(os.path.exists(f))

    def test_delete_missing_is_honest(self):
        self.assertIn("nothing to delete", fs.run("delete", {"path": os.path.join(self.d, "gone"), "confirm": True}).lower())

    def test_create_file_failure_is_reported_not_faked(self):
        # A path whose parent is a FILE can't become a directory → create must fail honestly.
        blocker = os.path.join(self.d, "blocker")
        open(blocker, "w").close()
        out = fs.run("create_file", {"path": os.path.join(blocker, "child.txt")})
        self.assertRegex(out.lower(), r"couldn't|isn't there")
        self.assertNotIn("Created file", out)

    def test_unknown_action(self):
        self.assertIn("unknown action", fs.run("frobnicate", {"path": self.d}))


if __name__ == "__main__":
    unittest.main()
