"""Real filesystem rename / recovery tests inside a disposable test directory.

No SSH, systemctl, privileged filesystem, user modification or prod-atlas access.
Checks incomplete code+env rename boundaries that are not represented by
FakeOperations' abstract transition model.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
from pathlib import Path
from unittest import TestCase, mock
import tempfile

from scripts import atlas_ingress_cutover_transaction as transaction


class FilesystemRollbackTests(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="atlas-cutover-fs-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root.chmod(0o755)
        self.live = self.root / "live"
        self.stage = self.root / "stage"
        self.previous = self.root / "previous"
        self.failed = self.root / "failed"
        self.env = self.root / "service.env"
        self.env_stage = self.root / "service.env.stage"
        self.env_old = self.root / "service.env.previous"
        self.env_backup = self.root / "service.env.before"
        self.orig_source = b"fixture-old-mcp-http-code\n"
        self.live.mkdir(mode=0o755)
        self.stage.mkdir(mode=0o755)
        (self.live / "atlas").mkdir()
        (self.live / "atlas" / "mcp_http.py").write_bytes(self.orig_source)
        (self.stage / "atlas").mkdir()
        (self.stage / "atlas" / "mcp_write.py").write_text("fixture-new\n")
        self.env.write_text("ORIGINAL_CONFIG=1\n")
        self.env_backup.write_bytes(self.env.read_bytes())
        self.env_stage.write_text("ORIGINAL_CONFIG=1\nATLAS_MCP_MEMORY_WRITE_ENABLED=1\nATLAS_MCP_SOURCE_WRITE_ENABLED=1\n")

    def patches(self) -> ExitStack:
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.multiple(
            transaction,
            LIVE=self.live,
            STAGE=self.stage,
            OLD=self.previous,
            FAILED=self.failed,
            ENV=self.env,
            ENV_STAGE=self.env_stage,
            ENV_OLD=self.env_old,
            ENV_BACKUP=self.env_backup,
            OLD_CODE_MCP_HTTP_SHA256=hashlib.sha256(self.orig_source).hexdigest(),
        ))
        stack.enter_context(mock.patch.object(transaction, "_protected_env"))
        stack.enter_context(mock.patch.object(transaction, "_flags"))
        return stack

    def test_real_code_and_env_move_then_restore_original_bytes(self) -> None:
        with self.patches():
            ops = transaction.SystemOperations()
            ops.swap_code()
            ops.swap_env()
            self.assertTrue((self.live / "atlas" / "mcp_write.py").is_file())
            self.assertTrue(self.env.read_bytes().endswith(b"ATLAS_MCP_SOURCE_WRITE_ENABLED=1\n"))
            self.assertTrue(self.previous.is_dir())
            self.assertTrue(self.env_old.is_file())
            ops.restore_code()
            ops.restore_env()
            self.assertEqual((self.live / "atlas" / "mcp_http.py").read_bytes(), self.orig_source)
            self.assertEqual(self.env.read_bytes(), self.env_backup.read_bytes())
            self.assertFalse(self.previous.exists())
            self.assertFalse(self.env_old.exists())
            self.assertTrue((self.failed / "atlas" / "mcp_write.py").is_file())

    def test_missing_stage_after_original_rename_restores_original(self) -> None:
        with self.patches():
            ops = transaction.SystemOperations()
            # Simulate an absent staging path at the second rename.
            self.stage.rename(self.root / "stage-unavailable")
            with self.assertRaises(OSError):
                ops.swap_code()
            self.assertTrue(self.previous.is_dir())
            self.assertFalse(self.live.exists())
            ops.restore_code()
            ops.restore_env()
            self.assertEqual((self.live / "atlas" / "mcp_http.py").read_bytes(), self.orig_source)
            self.assertEqual(self.env.read_bytes(), self.env_backup.read_bytes())
            self.assertFalse(self.previous.exists())

    def test_missing_env_stage_after_original_move_restores_original(self) -> None:
        with self.patches():
            ops = transaction.SystemOperations()
            ops.swap_code()
            self.env_stage.rename(self.root / "env-stage-unavailable")
            with self.assertRaises(OSError):
                ops.swap_env()
            self.assertFalse(self.env.exists())
            self.assertTrue(self.env_old.exists())
            ops.restore_code()
            ops.restore_env()
            self.assertEqual((self.live / "atlas" / "mcp_http.py").read_bytes(), self.orig_source)
            self.assertEqual(self.env.read_bytes(), self.env_backup.read_bytes())
            self.assertFalse(self.env_old.exists())
            self.assertTrue((self.failed / "atlas" / "mcp_write.py").is_file())

    def test_occupied_failed_slot_refuses_overwrite_and_preserves_old(self) -> None:
        with self.patches():
            ops = transaction.SystemOperations()
            ops.swap_code()
            (self.failed / "do-not-touch").parent.mkdir()
            (self.failed / "do-not-touch").write_text("preserve")
            with self.assertRaisesRegex(transaction.CutoverError, "FAILED_SLOT_OCCUPIED"):
                ops.restore_code()
            self.assertEqual((self.failed / "do-not-touch").read_text(), "preserve")
            self.assertTrue((self.previous / "atlas" / "mcp_http.py").is_file())
            self.assertTrue((self.live / "atlas" / "mcp_write.py").is_file())

    def test_wrong_old_backup_sha_causes_loud_failure(self) -> None:
        with self.patches():
            ops = transaction.SystemOperations()
            ops.swap_code()
            (self.previous / "atlas" / "mcp_http.py").write_text("wrong previous content")
            with self.assertRaisesRegex(transaction.CutoverError, "OLD_CODE_IDENTITY_HASH_MISMATCH"):
                ops.restore_code()
            self.assertTrue((self.failed / "atlas" / "mcp_write.py").is_file())
            self.assertTrue((self.live / "atlas" / "mcp_http.py").is_file())


if __name__ == "__main__":
    import unittest
    unittest.main()
