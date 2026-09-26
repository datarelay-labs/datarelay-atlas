"""Quiesced data-root backup and restore-test regressions. No production mutation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from atlas.cli import main
from atlas.data_lock import data_root_write_lock
from atlas.data_protection import backup_data_root, restore_test
from atlas.github_sync import FetchedSource
from atlas.provenance import ValidationError
from atlas.registry import ProjectRegistry
from atlas.service import AtlasService

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp_" + ("a" * 20)


def _sample_root(root: Path) -> None:
    service = AtlasService(root)
    service.register_project(
        project_id="alpha",
        repository="datarelay-labs/alpha",
        display_name="Alpha",
    )
    service.add_source(
        "alpha",
        source_id="charter",
        source_path="docs/charter.md",
        title="Charter",
    )

    def fetch(source, token):  # noqa: ARG001
        return FetchedSource(content="alpha body", source_revision="rev-alpha")

    service.sync_project("alpha", fetch=fetch)


class DataProtectionTests(unittest.TestCase):
    def test_backup_and_restore_round_trip_representative_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            before = (root / "registry.json").read_bytes()
            report = backup_data_root(root, base / "snapshot")
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["project_count"], 1)
            self.assertGreaterEqual(report["file_count"], 3)
            self.assertEqual((root / "registry.json").read_bytes(), before)
            snapshot = base / "snapshot"
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((snapshot / "registry.json").stat().st_mode),
                0o600,
            )
            restored = restore_test(snapshot, base / "proof")
            self.assertEqual(restored["project_count"], 1)
            self.assertEqual((root / "registry.json").read_bytes(), before)
            proof = ProjectRegistry(base / "proof")
            self.assertEqual(proof.get("alpha").repository, "datarelay-labs/alpha")
            document = (base / "proof" / "projections" / "alpha" / "charter.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("alpha body", document)
            self.assertIn("rev-alpha", document)

    def test_cooperating_writer_blocks_until_lock_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            ProjectRegistry(root).register(
                project_id="alpha",
                repository="datarelay-labs/alpha",
            )
            attempt = base / "attempt"
            done = base / "done"
            script = textwrap.dedent(
                """
                import sys
                from pathlib import Path
                from atlas.registry import ProjectRegistry
                root, attempt, done = (Path(arg) for arg in sys.argv[1:])
                attempt.write_text("1", encoding="utf-8")
                ProjectRegistry(root).register(
                    project_id="beta",
                    repository="datarelay-labs/beta",
                )
                done.write_text("1", encoding="utf-8")
                """
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            with data_root_write_lock(root):
                proc = subprocess.Popen(
                    [sys.executable, "-c", script, str(root), str(attempt), str(done)],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.time() + 5
                while not attempt.exists():
                    if time.time() > deadline:
                        proc.kill()
                        self.fail("writer did not reach the lock")
                    time.sleep(0.01)
                time.sleep(0.2)
                self.assertFalse(done.exists())
                self.assertNotIn("beta", (root / "registry.json").read_text(encoding="utf-8"))
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertIn("beta", (root / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(stdout, "")

    def test_backup_rejects_corrupt_secret_partial_and_unsupported_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            (root / "registry.json").write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "registry.json is corrupt"):
                backup_data_root(root, base / "bad-json")

            (root / "registry.json").write_text(
                '{"schema_version": 2, "projects": {}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "registry schema is unsupported"):
                backup_data_root(root, base / "bad-schema")

            (root / "registry.json").unlink()
            _sample_root(root)
            (root / "notes.txt").write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unexpected entries"):
                backup_data_root(root, base / "extra")
            (root / "notes.txt").unlink()

            (root / "registry.json.tmp").write_text("partial\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "incomplete write marker"):
                backup_data_root(root, base / "tmp")
            (root / "registry.json.tmp").unlink()

            link = root / "linked.json"
            link.symlink_to(root / "registry.json")
            os.replace(link, root / "registry.json")
            with self.assertRaisesRegex(ValidationError, "not a regular file"):
                backup_data_root(root, base / "link")

    def test_backup_rejects_secret_like_projection_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            document = root / "projections" / "alpha" / "charter.md"
            body = document.read_text(encoding="utf-8") + TOKEN + "\n"
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            document.write_text(body, encoding="utf-8")
            meta_path = root / "projections" / "projections.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["projections"]["alpha/charter"]["content_digest"] = digest
            meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "looks secret"):
                backup_data_root(root, base / "secret")

    def test_restore_test_rejects_tampered_and_partial_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            snapshot = base / "snapshot"
            backup_data_root(root, snapshot)
            registry = snapshot / "registry.json"
            registry.write_bytes(registry.read_bytes() + b" ")
            with self.assertRaisesRegex(ValidationError, "digest mismatch"):
                restore_test(snapshot, base / "torn")
            self.assertFalse((base / "torn").exists())

            raw = b"{"
            registry.write_bytes(raw)
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            for entry in manifest["files"]:
                if entry["path"] == "registry.json":
                    entry["bytes"] = len(raw)
                    entry["sha256"] = hashlib.sha256(raw).hexdigest()
            (snapshot / "manifest.json").write_text(
                json.dumps(manifest) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "corrupt"):
                restore_test(snapshot, base / "corrupt")

            backup_data_root(root, base / "clean")
            clean = base / "clean"
            (clean / "projections" / "extra.md").write_text("x\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unexpected files"):
                restore_test(clean, base / "extra")
            (clean / "projections" / "extra.md").unlink()
            (clean / "projections" / "alpha" / "charter.md").unlink()
            with self.assertRaisesRegex(ValidationError, "missing"):
                restore_test(clean, base / "missing")

            manifest = json.loads((clean / "manifest.json").read_text(encoding="utf-8"))
            manifest["backup_schema_version"] = 2
            (clean / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                restore_test(clean, base / "schema")

    def test_commands_refuse_overlapping_and_existing_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            with self.assertRaisesRegex(ValidationError, "outside the data root"):
                backup_data_root(root, root / "nested")
            snapshot = base / "snapshot"
            backup_data_root(root, snapshot)
            with self.assertRaisesRegex(ValidationError, "already exists"):
                backup_data_root(root, snapshot)
            with self.assertRaisesRegex(ValidationError, "outside the backup"):
                restore_test(snapshot, snapshot / "nested")

    def test_cli_prints_counts_without_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            stdout = StringIO()
            with patch("sys.stdout", stdout):
                code = main(
                    [
                        "ops",
                        "backup",
                        "--data-root",
                        str(root),
                        "--dest",
                        str(base / "snapshot"),
                    ]
                )
            self.assertEqual(code, 0)
            printed = stdout.getvalue()
            self.assertNotIn("alpha body", printed)
            self.assertIn('"status": "ok"', printed)
            stdout.seek(0)
            stdout.truncate(0)
            with patch("sys.stdout", stdout):
                code = main(
                    [
                        "ops",
                        "restore-test",
                        "--backup",
                        str(base / "snapshot"),
                        "--dest",
                        str(base / "proof"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertNotIn("alpha body", stdout.getvalue())
            self.assertIn('"project_count": 1', stdout.getvalue())

    def test_projection_digest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            _sample_root(root)
            document = root / "projections" / "alpha" / "charter.md"
            document.write_text(document.read_text(encoding="utf-8") + "torn\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "digest mismatch"):
                backup_data_root(root, base / "snapshot")


if __name__ == "__main__":
    unittest.main()
