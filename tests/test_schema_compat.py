"""Upgrade and rollback refuse unsupported durable schemas without rewriting."""

from __future__ import annotations

import json
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from atlas.cli import main
from atlas.provenance import ValidationError
from atlas.registry import ProjectRegistry
from atlas.schema_compat import rollback_data_root, upgrade_data_root
from atlas.work_controller import CONTROLLER_SCHEMA_VERSION, WorkControllerStore


class SchemaCompatTests(unittest.TestCase):
    def test_supported_upgrade_and_rollback_leave_schema_one_unchanged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            ProjectRegistry(root).register(
                project_id="alpha",
                repository="datarelay-labs/alpha",
            )
            WorkControllerStore(root)
            (root / "work-controller.json").write_text(
                json.dumps(
                    {"schema_version": CONTROLLER_SCHEMA_VERSION, "workstreams": {}}
                )
                + "\n",
                encoding="utf-8",
            )
            before = _durable(root)
            upgraded = upgrade_data_root(root)
            self.assertEqual(upgraded["status"], "ok")
            self.assertFalse(upgraded["rewritten"])
            self.assertEqual(upgraded["registry_schema"], 1)
            self.assertEqual(ProjectRegistry(root).get("alpha").repository, "datarelay-labs/alpha")
            rolled = rollback_data_root(root)
            self.assertEqual(rolled["status"], "ok")
            self.assertFalse(rolled["rewritten"])
            self.assertEqual(_durable(root), before)

    def test_newer_schema_refuses_upgrade_and_rollback_without_rewrite(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            registry = root / "registry.json"
            registry.write_text(
                '{"schema_version": 2, "projects": {}}\n',
                encoding="utf-8",
            )
            controller = root / "work-controller.json"
            controller.write_text(
                '{"schema_version": 1, "workstreams": {}}\n',
                encoding="utf-8",
            )
            before = _durable(root)
            with self.assertRaisesRegex(ValidationError, "upgrade refused: registry schema is unsupported"):
                upgrade_data_root(root)
            self.assertEqual(_durable(root), before)
            registry.write_text(
                '{"schema_version": 1, "projects": {}}\n',
                encoding="utf-8",
            )
            controller.write_text(
                '{"schema_version": 2, "workstreams": {}}\n',
                encoding="utf-8",
            )
            before = _durable(root)
            with self.assertRaisesRegex(
                ValidationError,
                "rollback refused: work-controller schema is newer than this code can read",
            ):
                rollback_data_root(root)
            self.assertEqual(_durable(root), before)

    def test_corrupt_registry_is_not_rewritten(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            registry = root / "registry.json"
            registry.write_text("{", encoding="utf-8")
            before = registry.read_bytes()
            with self.assertRaisesRegex(ValidationError, "upgrade refused: registry is corrupt"):
                upgrade_data_root(root)
            self.assertEqual(registry.read_bytes(), before)

    def test_cli_rollback_of_newer_registry_exits_nonzero(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            (root / "registry.json").write_text(
                '{"schema_version": 2, "projects": {}}\n',
                encoding="utf-8",
            )
            stderr = StringIO()
            with patch("sys.stderr", stderr):
                code = main(["ops", "rollback", "--data-root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("newer than this code can read", stderr.getvalue())
            self.assertNotIn("projects", stderr.getvalue())


def _durable(root: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(root.iterdir())
        if path.is_file() and path.name in {"registry.json", "work-controller.json"}
    }


if __name__ == "__main__":
    unittest.main()
