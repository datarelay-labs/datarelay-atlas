"""Staging and feature-flag tests on disposable local files only."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import stat
import tempfile

from scripts import atlas_ingress_cutover_transaction as release


class StageTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="atlas-safe-stage-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root.chmod(0o755)
        self.source = self.root / "release-source"
        self.source.mkdir(mode=0o755)
        (self.source / "atlas").mkdir()
        (self.source / "atlas" / "mcp_write.py").write_text("new\n")
        self.stage = self.root / "release-stage"
        self.env = self.root / "env-original"
        self.env_backup = self.root / "env-backup"
        self.env_stage = self.root / "env-stage"
        self.env.write_text("ATLAS_RESOURCE_URL=https://mcp.atlas.datarelay.run/mcp\n")
        self.env_backup.write_bytes(self.env.read_bytes())

    def patch(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.multiple(
            release, SOURCE=self.source, STAGE=self.stage,
            ENV=self.env, ENV_STAGE=self.env_stage, ENV_BACKUP=self.env_backup
        ))
        manifest = stack.enter_context(mock.patch.object(release, "_manifest"))
        stack.enter_context(mock.patch.object(release, "_protected_env"))
        stack.enter_context(mock.patch.object(release.os, "geteuid", return_value=0))
        stack.enter_context(mock.patch.object(release.os, "chown"))
        stack.enter_context(mock.patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=4321)))
        return manifest

    def test_preparation_only_creates_isolated_stage_and_guarded_flags(self):
        checks = self.patch()
        release.SystemOperations().prepare_stage()
        staged = self.env_stage.read_text()
        self.assertEqual(staged.count("ATLAS_MCP_MEMORY_WRITE_ENABLED=1"), 1)
        self.assertEqual(staged.count("ATLAS_MCP_SOURCE_WRITE_ENABLED=1"), 1)
        self.assertEqual(stat.S_IMODE(self.env_stage.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.stage.stat().st_mode), 0o755)
        self.assertEqual(self.env.read_bytes(), self.env_backup.read_bytes())
        self.assertEqual(
            (self.stage / "atlas" / "mcp_write.py").read_text(), "new\n"
        )
        self.assertEqual(checks.call_count, 2)

    def test_existing_zero_flags_are_replaced_not_duplicated(self):
        self.patch()
        original = ("ATLAS_MCP_MEMORY_WRITE_ENABLED=0\n"
                    "ATLAS_MCP_SOURCE_WRITE_ENABLED=0\n")
        self.env.write_text(original)
        self.env_backup.write_text(original)
        release.SystemOperations().prepare_stage()
        staged = self.env_stage.read_text()
        self.assertEqual(staged.count("ATLAS_MCP_MEMORY_WRITE_ENABLED"), 1)
        self.assertEqual(staged.count("ATLAS_MCP_SOURCE_WRITE_ENABLED"), 1)
        self.assertEqual(staged.count("=1"), 2)

    def test_duplicate_feature_flags_are_rejected_before_env_install(self):
        self.patch()
        original = ("ATLAS_MCP_MEMORY_WRITE_ENABLED=0\n"
                    "ATLAS_MCP_MEMORY_WRITE_ENABLED=0\n")
        self.env.write_text(original)
        self.env_backup.write_text(original)
        with self.assertRaisesRegex(release.CutoverError, "DUPLICATE_FEATURE_FLAG"):
            release.SystemOperations().prepare_stage()
        self.assertFalse(self.env_stage.exists())
        self.assertEqual(self.env.read_text(), original)

    def test_existing_stage_is_preserved_without_overwrite(self):
        self.patch()
        self.stage.mkdir(mode=0o755)
        artifact = self.stage / "keep.txt"
        artifact.write_text("preserve")
        with self.assertRaisesRegex(release.CutoverError, "STAGE_ALREADY_PRESENT"):
            release.SystemOperations().prepare_stage()
        self.assertEqual(artifact.read_text(), "preserve")

    def test_root_requirement_prevents_preparation(self):
        self.patch()
        with mock.patch.object(release.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(release.CutoverError, "ROOT_OPERATOR_REQUIRED"):
                release.SystemOperations().prepare_stage()
        self.assertFalse(self.stage.exists())
        self.assertFalse(self.env_stage.exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
