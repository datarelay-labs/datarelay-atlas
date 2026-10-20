"""Immutable release manifest builder regression tests."""
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.atlas_release_manifest import build_manifest, main


class ReleaseManifestTests(unittest.TestCase):
    def test_exact_commit_manifest_ignores_dirty_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "file.txt").write_text("canonical\n")
            subprocess.run(["git", "-C", str(root), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
            sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            (root / "file.txt").write_text("tampered\n")
            (root / "untracked.txt").write_text("extra")
            data, count = build_manifest(root, sha)
            self.assertEqual(count, 1)
            self.assertEqual(data, (hashlib.sha256(b"canonical\n").hexdigest() + "  file.txt\n").encode())
            with self.assertRaises(ValueError):
                build_manifest(root, "HEAD")

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "target").write_text("target")
            (root / "link").symlink_to("target")
            subprocess.run(["git", "-C", str(root), "add", "target", "link"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "symlink"], check=True)
            sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            with self.assertRaisesRegex(ValueError, "nonregular"):
                build_manifest(root, sha)


if __name__ == "__main__":
    unittest.main()
