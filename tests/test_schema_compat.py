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
from atlas.service import AtlasService
from atlas.registry import ProjectRegistry
from atlas.schema_compat import probe_durable_state, rollback_data_root, upgrade_data_root
from atlas.work_controller import CONTROLLER_SCHEMA_VERSION, WorkControllerStore

_REPO = Path(__file__).resolve().parents[1]
_EVENT = {
    "event_id": "evt-1",
    "workstream": "awc-poc",
    "issue_number": 43,
    "branch": "feature/phase2-production-data-protection",
    "head": "eb9748c309c2a97ad24125ee72247593d44e7384",
    "attempt": 1,
}


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
            rolled = rollback_data_root(root, _REPO)
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
                rollback_data_root(root, _REPO)
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
                code = main(
                    [
                        "ops",
                        "rollback",
                        "--data-root",
                        str(root),
                        "--target-code",
                        str(_REPO),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("newer than this code can read", stderr.getvalue())
            self.assertNotIn("projects", stderr.getvalue())

    def test_malformed_controller_number_refuses_without_leaking_value(self):
        secret = "SECRET-NOTINT"
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            (root / "work-controller.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workstreams": {
                            "ws": {
                                "workstream": "ws",
                                "repository": "datarelay-labs/datarelay-atlas",
                                "issue_number": secret,
                                "branch": "feature/x",
                                "worktree_path": "/tmp/x",
                                "expected_head": "abc",
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = (root / "work-controller.json").read_bytes()
            with self.assertRaises(ValidationError) as caught:
                upgrade_data_root(root)
            self.assertEqual(str(caught.exception), "upgrade refused: work-controller is unsupported")
            self.assertNotIn(secret, str(caught.exception))
            with self.assertRaises(ValidationError) as rolled:
                probe_durable_state(root)
            self.assertEqual(
                str(rolled.exception),
                "rollback refused: work-controller is unsupported",
            )
            self.assertNotIn(secret, str(rolled.exception))
            self.assertEqual((root / "work-controller.json").read_bytes(), before)
            stderr = StringIO()
            with patch("sys.stderr", stderr):
                code = main(["ops", "upgrade", "--data-root", str(root)])
            self.assertEqual(code, 1)
            output = stderr.getvalue()
            self.assertIn("upgrade refused: work-controller is unsupported", output)
            self.assertNotIn(secret, output)
            self.assertNotIn("Traceback", output)
            self.assertEqual((root / "work-controller.json").read_bytes(), before)

    def test_corrupt_completion_events_refuse_without_rewrite(self):
        for dirname in ("completion-inbox", "completion-processed"):
            with self.subTest(dirname=dirname):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp) / "data"
                    root.mkdir()
                    (root / "registry.json").write_text(
                        '{"schema_version": 1, "projects": {}}\n',
                        encoding="utf-8",
                    )
                    (root / "work-controller.json").write_text(
                        '{"schema_version": 1, "workstreams": {}}\n',
                        encoding="utf-8",
                    )
                    event = root / dirname / "bad.json"
                    event.parent.mkdir()
                    event.write_text("{}\n", encoding="utf-8")
                    before = _durable(root)
                    with self.assertRaisesRegex(
                        ValidationError,
                        "upgrade refused: completion event is unsupported",
                    ):
                        upgrade_data_root(root)
                    self.assertEqual(_durable(root), before)
                    with self.assertRaisesRegex(
                        ValidationError,
                        "rollback refused: completion event is unsupported",
                    ):
                        rollback_data_root(root, _REPO)
                    self.assertEqual(_durable(root), before)

    def test_valid_completion_events_stay_readable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            (root / "registry.json").write_text(
                '{"schema_version": 1, "projects": {}}\n',
                encoding="utf-8",
            )
            inbox = root / "completion-inbox"
            processed = root / "completion-processed"
            inbox.mkdir()
            processed.mkdir()
            (inbox / "evt.json").write_text(json.dumps(_EVENT) + "\n", encoding="utf-8")
            (processed / "evt.json").write_text(json.dumps(_EVENT) + "\n", encoding="utf-8")
            before = _durable(root)
            upgraded = upgrade_data_root(root)
            self.assertEqual(upgraded["status"], "ok")
            self.assertFalse(upgraded["rewritten"])
            self.assertEqual(_durable(root), before)

    def test_rollback_uses_staged_target_code(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            (root / "registry.json").write_text(
                '{"schema_version": 1, "projects": {}}\n',
                encoding="utf-8",
            )
            target = Path(tmp) / "older"
            package = target / "atlas"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "schema_compat.py").write_text(
                "def probe_durable_state(data_root):\n"
                "    raise RuntimeError('stub-target-cannot-read')\n",
                encoding="utf-8",
            )
            before = _durable(root)
            upgraded = upgrade_data_root(root)
            self.assertEqual(upgraded["status"], "ok")
            with self.assertRaisesRegex(ValidationError, "stub-target-cannot-read"):
                rollback_data_root(root, target)
            self.assertEqual(_durable(root), before)
            (package / "schema_compat.py").write_text(
                "def probe_durable_state(data_root):\n"
                "    return {\n"
                "        'controller_schema': None,\n"
                "        'probe': 'stub-target',\n"
                "        'registry_schema': 1,\n"
                "        'rewritten': False,\n"
                "        'status': 'ok',\n"
                "    }\n",
                encoding="utf-8",
            )
            rolled = rollback_data_root(root, target)
            self.assertEqual(rolled["probe"], "stub-target")
            self.assertFalse(rolled["rewritten"])
            self.assertEqual(_durable(root), before)

    def test_rollback_refuses_personal_source_on_github_only_target(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            service = AtlasService(root)
            service.register_project(project_id="notes", repository="datarelay-labs/notes")
            staged = service.import_root / "notes"
            staged.mkdir(parents=True)
            (staged / "page.md").write_text("# Page\n\nhello\n", encoding="utf-8")
            service.import_personal_markdown(
                "notes",
                source_id="page",
                source_path="notes/page.md",
            )
            service.sync_project("notes")
            snapshot = service.snapshot_root / "notes" / "page.md"
            before_snapshot = snapshot.read_bytes()
            before = _durable(root)
            with self.assertRaisesRegex(ValidationError, "unsupported provider"):
                rollback_data_root(root, _github_only_target(base / "old-target"))
            self.assertEqual(_durable(root), before)
            self.assertEqual(snapshot.read_bytes(), before_snapshot)
            rolled = rollback_data_root(root, _REPO)
            self.assertEqual(rolled["status"], "ok")
            self.assertFalse(rolled["rewritten"])
            self.assertEqual(_durable(root), before)
            self.assertEqual(snapshot.read_bytes(), before_snapshot)


def _github_only_target(target: Path) -> Path:
    package = target / "atlas"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "schema_compat.py").write_text(
        "def probe_durable_state(data_root):\n"
        "    return {\n"
        "        'controller_schema': None,\n"
        "        'registry_schema': 1,\n"
        "        'rewritten': False,\n"
        "        'status': 'ok',\n"
        "    }\n",
        encoding="utf-8",
    )
    (package / "provenance.py").write_text(
        "class ValidationError(ValueError):\n"
        "    pass\n"
        "def validate_source(source):\n"
        "    if getattr(source, 'provider', None) != 'github':\n"
        "        raise ValidationError(f'unsupported provider: {source.provider}')\n",
        encoding="utf-8",
    )
    (package / "registry.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "class ProjectRegistry:\n"
        "    def __init__(self, data_root):\n"
        "        self.path = Path(data_root) / 'registry.json'\n"
        "    def list_projects(self):\n"
        "        data = json.loads(self.path.read_text(encoding='utf-8'))\n"
        "        return [type('P', (), {'project_id': pid})() for pid in data.get('projects', {})]\n"
        "    def canonical_sources(self, project_id):\n"
        "        data = json.loads(self.path.read_text(encoding='utf-8'))\n"
        "        sources = data['projects'][project_id].get('sources') or {}\n"
        "        return [type('S', (), {'provider': src.get('provider', 'github')})() for src in sources.values()]\n",
        encoding="utf-8",
    )
    return target


def _durable(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in {"registry.json", "work-controller.json"} or path.suffix == ".json":
            found[str(path.relative_to(root))] = path.read_bytes()
    return found


if __name__ == "__main__":
    unittest.main()
