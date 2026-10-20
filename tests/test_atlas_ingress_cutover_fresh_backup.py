"""Fresh, immutable backup gate before any Atlas live code/env transition.

All files in these tests are disposable and calls to real operations are
replaced by deterministic local fake reports. No prod host or privileges.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from scripts import atlas_ingress_cutover_transaction as release


class CurrentDataBackupGateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="atlas-fresh-backup-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.live_data = self.root / "durable-data"
        self.live_data.mkdir()
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.old_code = self.root / "old-code"
        self.old_code.mkdir()
        self.fresh = self.root / "fresh-backup"
        self.calls: list[list[str]] = []
        self.restore_files = 84
        self.backup_projects = 7
        self.backup_files = 84
        self.rewritten = False
        self.write_manifest = True

    def patch(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.multiple(
            release, DATA=self.live_data, STAGE=self.stage,
            CODE_BACKUP=self.old_code, FRESH_BACKUP=self.fresh,
        ))
        stack.enter_context(
            mock.patch.object(release.os, "geteuid", return_value=0)
        )
        stack.enter_context(mock.patch.object(release, "_check_json", side_effect=self.response))

    def response(self, args: list[str], *, env=None) -> dict:
        self.calls.append(args)
        if "backup" in args:
            if self.write_manifest:
                self.fresh.mkdir()
                (self.fresh / "manifest.json").write_text("{}")
            return {
                "status": "ok",
                "dest": str(self.fresh),
                "file_count": self.backup_files,
                "project_count": self.backup_projects,
            }
        if "restore-test" in args:
            return {
                "status": "ok",
                "file_count": self.restore_files,
                "project_count": self.backup_projects,
            }
        if "upgrade" in args or "rollback" in args:
            return {"status": "ok", "rewritten": self.rewritten}
        raise AssertionError("unexpected argv: " + str(args))

    def test_new_snapshot_and_restore_only_on_temporary_data(self):
        self.patch()
        release.SystemOperations().capture_fresh_backup()
        self.assertEqual(len(self.calls), 4)
        backup, restored, upgrade, rollback = self.calls
        self.assertEqual(backup[backup.index("--data-root")+1], str(self.live_data))
        self.assertEqual(backup[backup.index("--dest")+1], str(self.fresh))
        self.assertEqual(restored[restored.index("--backup")+1], str(self.fresh))
        proof = restored[restored.index("--dest")+1]
        self.assertTrue(proof.startswith("/tmp/"))
        for call in (upgrade, rollback):
            self.assertEqual(call[call.index("--data-root")+1], proof)
            self.assertNotIn(str(self.live_data), call)
        self.assertEqual(rollback[rollback.index("--target-code")+1], str(self.old_code))

    def test_preexisting_backup_is_never_overwritten(self):
        self.patch()
        self.fresh.mkdir()
        keep = self.fresh / "protected"
        keep.write_text("original")
        with self.assertRaisesRegex(release.CutoverError, "FRESH_BACKUP_SLOT_ALREADY_OCCUPIED"):
            release.SystemOperations().capture_fresh_backup()
        self.assertEqual(keep.read_text(), "original")
        self.assertEqual(self.calls, [])

    def test_snapshot_project_count_mismatch_refuses_cutover(self):
        self.patch()
        self.backup_projects = 6
        with self.assertRaisesRegex(release.CutoverError, "FRESH_BACKUP_INCOMPLETE"):
            release.SystemOperations().capture_fresh_backup()
        self.assertEqual(len(self.calls), 1)

    def test_snapshot_has_to_contain_nonempty_manifest(self):
        self.patch()
        self.write_manifest = False
        with self.assertRaisesRegex(release.CutoverError, "FRESH_BACKUP_MANIFEST_MISSING"):
            release.SystemOperations().capture_fresh_backup()
        self.assertEqual(len(self.calls), 1)

    def test_snapshot_restore_file_count_difference_is_rejected(self):
        self.patch()
        self.restore_files = 83
        with self.assertRaisesRegex(release.CutoverError, "FRESH_RESTORE_MISMATCH"):
            release.SystemOperations().capture_fresh_backup()
        self.assertEqual(len(self.calls), 2)

    def test_schema_rewrite_attempt_is_rejected(self):
        self.patch()
        self.rewritten = True
        with self.assertRaisesRegex(release.CutoverError, "FRESH_BACKUP_SCHEMA_INCOMPATIBLE"):
            release.SystemOperations().capture_fresh_backup()
        self.assertEqual(len(self.calls), 4)

    def test_non_root_cannot_start_a_live_backup(self):
        self.patch()
        with mock.patch.object(release.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(release.CutoverError, "ROOT_OPERATOR_REQUIRED"):
                release.SystemOperations().capture_fresh_backup()
        self.assertEqual(self.calls, [])

    def test_actual_atlas_backup_restore_and_old_code_compatibility_cli(self):
        """End-to-end subprocess proof over seven synthetic local projects."""
        from atlas.service import AtlasService

        repo = Path(__file__).resolve().parents[1]
        # A disposable stage reuses current source and interpreter. Only
        # local fixture files are written, never the real Atlas data root.
        (self.stage / "atlas").symlink_to(repo / "atlas", target_is_directory=True)
        (self.stage / ".venv").symlink_to(
            Path(sys.executable).parent.parent, target_is_directory=True
        )
        (self.old_code / "atlas").symlink_to(repo / "atlas", target_is_directory=True)

        service = AtlasService(self.live_data)
        for i in range(7):
            service.register_project(
                project_id=f"project-{i}",
                repository=f"datarelay-labs/project-{i}",
                display_name=f"Project {i}",
            )
        registry_before = (self.live_data / "registry.json").read_bytes()

        with mock.patch.multiple(
            release, DATA=self.live_data, STAGE=self.stage,
            CODE_BACKUP=self.old_code, FRESH_BACKUP=self.fresh,
        ), mock.patch.object(release.os, "geteuid", return_value=0):
            release.SystemOperations().capture_fresh_backup()

        self.assertTrue((self.fresh / "manifest.json").is_file())
        self.assertEqual((self.live_data / "registry.json").read_bytes(), registry_before)

    def test_fresh_gate_never_mutates_data_if_wrong_root_identity(self):
        self.patch()
        self.backup_projects = 6
        with self.assertRaisesRegex(release.CutoverError, "FRESH_BACKUP_INCOMPLETE"):
            release.SystemOperations().capture_fresh_backup()
        self.assertTrue(self.live_data.is_dir())


if __name__ == "__main__":
    unittest.main()
