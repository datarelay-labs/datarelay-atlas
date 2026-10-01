from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from atlas.cli import main
from atlas.data_protection import backup_data_root, restore_test
from atlas.github_sync import FetchedSource
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.personal_knowledge import MANIFEST_FILENAME
from atlas.provenance import ValidationError
from atlas.service import AtlasService


class PersonalKnowledgeTests(unittest.TestCase):
    def _seed(self, root: Path) -> AtlasService:
        svc = AtlasService(root)
        svc.register_project(project_id="mixed", repository="datarelay-labs/mixed")
        svc.add_source("mixed", source_id="engineering", source_path="docs/engineering.md")
        svc.import_root.mkdir(parents=True, exist_ok=True)
        (svc.import_root / "personal.md").write_text("personal-only-quill", encoding="utf-8")
        svc.import_personal_markdown(
            "mixed", source_id="personal", source_path="personal.md", title="Personal note"
        )

        def fetch(source, token):  # noqa: ARG001
            return FetchedSource(
                content="engineering-only-quill",
                source_revision="rev-engineering",
            )

        for source in svc.registry.canonical_sources("mixed"):
            svc.projections.sync_one(
                source,
                fetch=fetch if source.provider == "github" else None,
            )
        return svc

    def test_dashboard_and_search_keep_personal_authority_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(Path(tmp))
            dashboard = svc.personal_knowledge_dashboard()
            self.assertEqual(dashboard["authority"], "PERSONAL_REFERENCE_ONLY")
            self.assertIs(dashboard["engineering_authority"], False)
            self.assertEqual(dashboard["totals"]["personal_sources"], 1)
            self.assertEqual(dashboard["totals"]["successful_projections"], 1)
            self.assertEqual(dashboard["totals"]["snapshots_present"], 1)

            personal = svc.personal_search("mixed", "personal-only-quill")
            self.assertEqual(len(personal), 1)
            self.assertEqual(personal[0].provenance["source_class"], "personal")
            self.assertIs(personal[0].provenance["engineering_authority"], False)
            self.assertEqual(svc.personal_search("mixed", "engineering-only-quill"), [])

    def test_manifest_exposes_only_bounded_metadata_and_rejects_content_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc = self._seed(root)
            manifest = {
                "schema_version": 1,
                "kind": "personal_knowledge_import_manifest",
                "source_system": "tela",
                "observed_at": "2026-09-30T12:00:00Z",
                "items": [
                    {
                        "source_id": "personal",
                        "external_id": "4305",
                        "title": "AI Access / MCP Bridge",
                        "state": "SANITIZED",
                        "reason_code": "SECRET_GUARD_FALSE_POSITIVE",
                    },
                    {
                        "external_id": "9999",
                        "title": "Rejected",
                        "state": "QUARANTINED",
                        "reason_code": "SECRET_GUARD",
                    },
                ],
            }
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            dashboard = svc.personal_knowledge_dashboard()
            self.assertEqual(dashboard["import_manifest"]["state"], "OBSERVED")
            self.assertEqual(dashboard["import_manifest"]["counts"]["sanitized"], 1)
            self.assertEqual(dashboard["import_manifest"]["counts"]["quarantined"], 1)
            self.assertNotIn("content", json.dumps(dashboard["import_manifest"]))

            manifest["items"][1]["content"] = "must-not-be-retained"
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValidationError):
                svc.personal_knowledge_dashboard()

            del manifest["items"][1]["content"]
            manifest["content"] = "must-not-be-retained"
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValidationError):
                svc.personal_knowledge_dashboard()
            del manifest["content"]

            manifest["schema_version"] = True
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                svc.personal_knowledge_dashboard()
            manifest["schema_version"] = 1

            manifest["items"][1]["state"] = []
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "state is invalid"):
                svc.personal_knowledge_dashboard()
            manifest["items"][1]["state"] = "QUARANTINED"

            manifest["items"][1]["title"] = (
                "PASS" + "WORD=" + chr(34) + "hunter2" + chr(10) + "more-secret" + chr(34)
            )
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "contains unsafe secret metadata"
            ):
                svc.personal_knowledge_dashboard()

            manifest["items"][1]["title"] = "Bearer abc.def"
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "contains unsafe secret metadata"
            ):
                svc.personal_knowledge_dashboard()

            manifest["items"][1]["title"] = "Be" + "arer" + "\n " + "abc.def"
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "contains unsafe secret metadata"
            ):
                svc.personal_knowledge_dashboard()

            manifest["items"][1]["title"] = "Rejected"
            manifest["source_system"] = "Authorization: Bearer abc.def"
            (root / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "contains unsafe secret metadata"
            ):
                svc.personal_knowledge_dashboard()

            (root / MANIFEST_FILENAME).unlink()
            (root / MANIFEST_FILENAME).symlink_to(root / "missing-manifest.json")
            with self.assertRaisesRegex(ValidationError, "manifest is unsafe"):
                svc.personal_knowledge_dashboard()

    def test_import_manifest_is_preserved_by_existing_backup_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            svc = self._seed(root)
            manifest = {
                "schema_version": 1,
                "kind": "personal_knowledge_import_manifest",
                "source_system": "tela",
                "observed_at": "2026-09-30T12:00:00Z",
                "items": [
                    {
                        "source_id": "personal",
                        "external_id": "4305",
                        "title": "AI Access / MCP Bridge",
                        "state": "SANITIZED",
                        "reason_code": "SECRET_GUARD_FALSE_POSITIVE",
                    }
                ],
            }
            manifest_path = root / MANIFEST_FILENAME
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            backup_data_root(root, base / "backup")
            restored = base / "restore"
            restore_test(base / "backup", restored)
            self.assertEqual(
                json.loads((restored / MANIFEST_FILENAME).read_text(encoding="utf-8")),
                manifest,
            )
            self.assertEqual(
                AtlasService(restored).personal_knowledge_dashboard()["import_manifest"]["state"],
                "OBSERVED",
            )

            manifest["content"] = "raw rejected body must not enter backup"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValidationError):
                backup_data_root(root, base / "invalid-backup")
            del manifest["content"]

            duplicate = json.dumps(manifest).replace(
                '"source_system": "tela"',
                '"source_system": "tela", "source_system": "tela"',
                1,
            )
            manifest_path.write_text(duplicate, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate JSON keys"):
                backup_data_root(root, base / "duplicate-backup")

            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            valid_backup = base / "restore-validation-backup"
            backup_data_root(root, valid_backup)
            backed_manifest = valid_backup / MANIFEST_FILENAME
            invalid_manifest = json.loads(backed_manifest.read_text(encoding="utf-8"))
            invalid_manifest["content"] = "legacy raw body"
            invalid_bytes = json.dumps(invalid_manifest).encode("utf-8")
            backed_manifest.write_bytes(invalid_bytes)
            backup_manifest_path = valid_backup / "manifest.json"
            backup_manifest = json.loads(backup_manifest_path.read_text(encoding="utf-8"))
            for entry in backup_manifest["files"]:
                if entry["path"] == MANIFEST_FILENAME:
                    import hashlib
                    entry["bytes"] = len(invalid_bytes)
                    entry["sha256"] = hashlib.sha256(invalid_bytes).hexdigest()
                    break
            backup_manifest_path.write_text(
                json.dumps(backup_manifest, indent=2, sort_keys=True) + chr(10),
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                restore_test(valid_backup, base / "invalid-restore")

    def test_cli_and_mcp_have_dedicated_personal_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc = self._seed(root)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--data-root", str(root), "personal", "show"]), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["authority"], "PERSONAL_REFERENCE_ONLY")

            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                personal_knowledge_factory=svc.personal_knowledge_dashboard,
                personal_search_factory=lambda project_id, query, limit: svc.personal_search(
                    project_id, query, limit=limit
                ),
            )
            names = {tool["name"] for tool in tools.list_tools(default_read_scopes())}
            self.assertIn("get_personal_knowledge", names)
            self.assertIn("search_personal_knowledge", names)
            result = tools.call(
                "search_personal_knowledge",
                {"project_id": "mixed", "query": "personal-only-quill"},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(len(result.data), 1)
            self.assertEqual(result.data[0]["provenance"]["source_class"], "personal")
            engineering = tools.call(
                "search_personal_knowledge",
                {"project_id": "mixed", "query": "engineering-only-quill"},
                scopes=default_read_scopes(),
            )
            self.assertTrue(engineering.ok)
            self.assertEqual(engineering.data, [])


if __name__ == "__main__":
    unittest.main()
