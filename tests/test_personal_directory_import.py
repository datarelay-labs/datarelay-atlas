from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atlas.cli import main
from atlas.data_protection import backup_data_root, restore_test
from atlas.personal_directory_import import directory_source_id
from atlas.provenance import ValidationError
from atlas.service import AtlasService


class PersonalDirectoryImportTests(unittest.TestCase):
    def _service(self, root: Path) -> AtlasService:
        svc = AtlasService(root)
        svc.register_project(
            project_id="personal",
            repository="datarelay-labs/personal",
            display_name="Personal",
        )
        return svc

    def test_imports_nested_markdown_with_personal_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            (vault / "Projects").mkdir(parents=True)
            (vault / "Projects" / "Atlas.md").write_text(
                "# Atlas note\n\nobsidian-personal-marker\n",
                encoding="utf-8",
            )
            (vault / "image.png").write_bytes(b"not imported")

            result = svc.import_personal_markdown_directory(
                "personal",
                source_root=vault,
                collection_id="rick-notes",
            )
            self.assertEqual(result["counts"]["imported"], 1)
            self.assertEqual(result["counts"]["rejected"], 1)
            source_id = directory_source_id("rick-notes", "Projects/Atlas.md")
            source = {s.source_id: s for s in svc.list_sources("personal")}[source_id]
            self.assertEqual(source.source_path, "rick-notes/Projects/Atlas.md")
            self.assertEqual(source.source_class, "personal")

            hits = svc.personal_search("personal", "obsidian-personal-marker")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].provenance["source_class"], "personal")
            self.assertIs(hits[0].provenance["engineering_authority"], False)
            self.assertEqual(
                hits[0].provenance["source_path"],
                "rick-notes/Projects/Atlas.md",
            )

    def test_reimport_is_idempotent_updates_changed_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            note = vault / "note.md"
            note.write_text("first-marker\n", encoding="utf-8")

            first = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(first["counts"]["imported"], 1)

            second = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(second["counts"]["unchanged"], 1)
            self.assertEqual(len(svc.list_sources("personal")), 1)

            note.write_text("second-marker\n", encoding="utf-8")
            third = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(third["counts"]["updated"], 1)
            self.assertEqual(len(svc.personal_search("personal", "second-marker")), 1)
            self.assertEqual(svc.personal_search("personal", "first-marker"), [])

            note.unlink()
            fourth = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(fourth["counts"]["missing"], 1)
            self.assertEqual(len(svc.list_sources("personal")), 1)
            self.assertEqual(len(svc.personal_search("personal", "second-marker")), 1)

    def test_secret_like_markdown_is_quarantined_without_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            (vault / "safe.md").write_text("safe-personal-marker\n", encoding="utf-8")
            (vault / "secret.md").write_text(
                'PASSWORD="hunter2"\n',
                encoding="utf-8",
            )

            result = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(result["counts"]["imported"], 1)
            self.assertEqual(result["counts"]["quarantined"], 1)
            serialized = json.dumps(result)
            self.assertNotIn("hunter2", serialized)
            secret_id = directory_source_id("vault", "secret.md")
            self.assertNotIn(
                secret_id,
                {source.source_id for source in svc.list_sources("personal")},
            )
            self.assertFalse(
                (svc.snapshot_root / "personal" / f"{secret_id}.md").exists()
            )

    def test_existing_source_that_turns_secret_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            note = vault / "note.md"
            note.write_text("safe-before-secret\n", encoding="utf-8")
            svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            note.write_text('PASSWORD="hunter2"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unreadable or rejected"):
                svc.import_personal_markdown_directory(
                    "personal", source_root=vault, collection_id="vault"
                )
            self.assertEqual(
                len(svc.personal_search("personal", "safe-before-secret")),
                1,
            )
            self.assertEqual(
                svc.personal_search("personal", "hunter2"),
                [],
            )

    def test_non_utf8_filename_is_bounded_rejected_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            raw_path = os.path.join(os.fsencode(vault), b"bad-\xff.md")
            fd = os.open(raw_path, os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, b"safe body\n")
            finally:
                os.close(fd)

            result = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(result["counts"]["rejected"], 1)
            self.assertEqual(result["items"][0]["relative_path"], "[NON_UTF8_PATH]")
            self.assertEqual(result["items"][0]["reason_code"], "NON_UTF8_PATH")
            json.dumps(result).encode("utf-8")
            self.assertEqual(svc.list_sources("personal"), [])

    def test_actual_read_byte_limit_is_enforced_after_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            note = vault / "note.md"
            note.write_text("small\n", encoding="utf-8")

            with patch(
                "atlas.personal_directory_import._scan_directory",
                return_value=([("note.md", 1)], []),
            ):
                note.write_bytes(b"x" * 33)
                with patch(
                    "atlas.personal_directory_import._MAX_FILE_BYTES",
                    32,
                ):
                    result = svc.import_personal_markdown_directory(
                        "personal", source_root=vault, collection_id="vault"
                    )
            self.assertEqual(result["counts"]["rejected"], 1)
            self.assertEqual(result["counts"]["imported"], 0)
            self.assertFalse(svc.list_sources("personal"))

    def test_actual_aggregate_bytes_are_rechecked_after_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            (vault / "a.md").write_text("aaaa", encoding="utf-8")
            (vault / "b.md").write_text("bbbb", encoding="utf-8")

            with patch(
                "atlas.personal_directory_import._scan_directory",
                return_value=([("a.md", 1), ("b.md", 1)], []),
            ), patch(
                "atlas.personal_directory_import._MAX_TOTAL_BYTES",
                7,
            ):
                with self.assertRaisesRegex(ValidationError, "bytes exceed"):
                    svc.import_personal_markdown_directory(
                        "personal", source_root=vault, collection_id="vault"
                    )
            self.assertEqual(svc.list_sources("personal"), [])
            self.assertEqual(
                svc.personal_search("personal", "aaaa"),
                [],
            )

    def test_rejected_file_bytes_still_count_toward_actual_aggregate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            (vault / "secret.md").write_text(
                'PASSWORD="hunter2"\n',
                encoding="utf-8",
            )
            (vault / "safe.md").write_text("safe\n", encoding="utf-8")

            with patch(
                "atlas.personal_directory_import._scan_directory",
                return_value=([("secret.md", 1), ("safe.md", 1)], []),
            ), patch(
                "atlas.personal_directory_import._MAX_TOTAL_BYTES",
                20,
            ):
                with self.assertRaisesRegex(ValidationError, "bytes exceed"):
                    svc.import_personal_markdown_directory(
                        "personal", source_root=vault, collection_id="vault"
                    )

            self.assertEqual(svc.list_sources("personal"), [])
            snapshot_project = svc.snapshot_root / "personal"
            self.assertFalse(snapshot_project.exists())

    def test_secret_like_path_metadata_fails_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            (vault / 'PASSWORD=hunter2.md').write_text(
                "safe body\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as caught:
                svc.import_personal_markdown_directory(
                    "personal", source_root=vault, collection_id="vault"
                )
            self.assertIn("path metadata looks secret", str(caught.exception))
            self.assertNotIn("hunter2", str(caught.exception))
            self.assertEqual(svc.list_sources("personal"), [])

    def test_public_importer_holds_data_root_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            (vault / "note.md").write_text("lock-marker\n", encoding="utf-8")
            entered = {"value": False}

            class Guard:
                def __enter__(self):
                    entered["value"] = True
                def __exit__(self, exc_type, exc, tb):
                    entered["value"] = False

            def assert_inside(**kwargs):
                self.assertTrue(entered["value"])
                return {"counts": {"conflicts": 0}}

            with patch(
                "atlas.personal_directory_import.data_root_write_lock",
                return_value=Guard(),
            ), patch(
                "atlas.personal_directory_import._import_personal_markdown_directory_locked",
                side_effect=assert_inside,
            ):
                result = svc.import_personal_markdown_directory(
                    "personal", source_root=vault, collection_id="vault"
                )
            self.assertEqual(result["counts"]["conflicts"], 0)
            self.assertFalse(entered["value"])

    def test_deep_tree_is_iterative_without_recursion_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            current = vault
            for _ in range(120):
                current = current / "d"
                current.mkdir()
            (current / "note.md").write_text("deep-marker\n", encoding="utf-8")

            old_limit = sys.getrecursionlimit()
            try:
                sys.setrecursionlimit(100)
                result = svc.import_personal_markdown_directory(
                    "personal", source_root=vault, collection_id="vault"
                )
            finally:
                sys.setrecursionlimit(old_limit)

            self.assertEqual(result["counts"]["imported"], 1)
            self.assertEqual(len(svc.personal_search("personal", "deep-marker")), 1)

    def test_symlink_and_data_root_overlap_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            target = base / "outside.md"
            target.write_text("outside\n", encoding="utf-8")
            (vault / "linked.md").symlink_to(target)

            with self.assertRaisesRegex(ValidationError, "symlink"):
                svc.import_personal_markdown_directory(
                    "personal", source_root=vault, collection_id="vault"
                )

            with self.assertRaisesRegex(ValidationError, "overlaps"):
                svc.import_personal_markdown_directory(
                    "personal", source_root=base, collection_id="vault"
                )
            with self.assertRaisesRegex(ValidationError, "overlaps"):
                svc.import_personal_markdown_directory(
                    "personal",
                    source_root=svc.data_root,
                    collection_id="vault",
                )

    def test_existing_incompatible_source_is_reported_as_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            (vault / "note.md").write_text("conflict-marker\n", encoding="utf-8")
            source_id = directory_source_id("vault", "note.md")
            svc.add_source(
                "personal",
                source_id=source_id,
                source_path="docs/engineering.md",
            )

            result = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(result["counts"]["conflicts"], 1)
            self.assertEqual(result["counts"]["imported"], 0)
            self.assertFalse(
                (svc.snapshot_root / "personal" / f"{source_id}.md").exists()
            )

    def test_directory_import_reuses_existing_backup_restore_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            svc = self._service(root)
            vault = base / "vault"
            vault.mkdir()
            (vault / "note.md").write_text(
                "backup-personal-marker\n",
                encoding="utf-8",
            )
            svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="notes"
            )

            backup = base / "backup"
            backup_data_root(root, backup)
            restored = base / "restored"
            restore_test(backup, restored)
            restored_svc = AtlasService(restored)
            hits = restored_svc.personal_search("personal", "backup-personal-marker")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].provenance["source_class"], "personal")
            self.assertIs(hits[0].provenance["engineering_authority"], False)

    def test_missing_legacy_secret_like_path_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            svc = self._service(base / "data")
            vault = base / "vault"
            vault.mkdir()
            svc.registry.add_personal_snapshot(
                "personal",
                source_id="vault-legacy",
                source_path="vault/PASSWORD=hunter2.md",
                title="Legacy personal source",
            )

            result = svc.import_personal_markdown_directory(
                "personal", source_root=vault, collection_id="vault"
            )
            self.assertEqual(result["counts"]["missing"], 1)
            self.assertEqual(result["items"][0]["relative_path"], "[REDACTED_PATH]")
            self.assertEqual(
                result["items"][0]["reason_code"],
                "SOURCE_FILE_ABSENT_REDACTED_PATH",
            )
            self.assertNotIn("hunter2", json.dumps(result))

    def test_cli_import_dir_returns_bounded_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            self._service(root)
            vault = base / "vault"
            vault.mkdir()
            (vault / "cli.md").write_text("cli-personal-marker\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main([
                    "--data-root", str(root),
                    "personal", "import-dir", "personal",
                    "--root", str(vault),
                    "--collection-id", "notes",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["authority"], "PERSONAL_REFERENCE_ONLY")
            self.assertEqual(payload["counts"]["imported"], 1)
            self.assertNotIn("cli-personal-marker", out.getvalue())


if __name__ == "__main__":
    unittest.main()
