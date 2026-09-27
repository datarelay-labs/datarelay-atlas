"""Personal Markdown snapshot boundaries for Slice A."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from atlas.cli import main
from atlas.data_protection import backup_data_root, restore_test
from atlas.github_sync import FetchedSource
from atlas.projection_retrieval import build_keyword_retriever
from atlas.provenance import ValidationError
from atlas.service import AtlasService

LEGACY_GITHUB_PROVENANCE_KEYS = {
    "project_id",
    "provider",
    "repository",
    "ref",
    "source_path",
    "source_revision",
    "derived",
    "canonical",
}
PHRASE = "personal-snapshot-phrase-quill"


class LocalMarkdownTests(unittest.TestCase):
    def _project(self, root: Path) -> AtlasService:
        svc = AtlasService(root)
        svc.register_project(project_id="notes", repository="datarelay-labs/notes")
        return svc

    def _stage(self, svc: AtlasService, relative: str, text: str) -> Path:
        path = svc.import_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_import_root_is_fixed_and_outside_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            svc = self._project(root)
            outside = base / "outside"
            outside.mkdir()
            secret = outside / "page.md"
            secret.write_text("outside-only-phrase\n", encoding="utf-8")
            (outside / "link-target.md").write_text("linked-outside\n", encoding="utf-8")

            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    main(
                        [
                            "--data-root",
                            str(root),
                            "source",
                            "import",
                            "notes",
                            "page",
                            "--import-root",
                            str(outside),
                            "--path",
                            "page.md",
                        ]
                    )
            with self.assertRaises(ValidationError):
                svc.import_personal_markdown("notes", source_id="page", source_path="page.md")
            with self.assertRaises(ValidationError):
                svc.import_personal_markdown(
                    "notes",
                    source_id="page",
                    source_path="../outside/page.md",
                )
            with self.assertRaises(ValidationError):
                svc.import_personal_markdown(
                    "notes",
                    source_id="page",
                    source_path=str(secret),
                )
            svc.import_root.mkdir(parents=True, exist_ok=True)
            (svc.import_root / "link.md").symlink_to(secret)
            with self.assertRaises(ValidationError):
                svc.import_personal_markdown("notes", source_id="page", source_path="link.md")
            with self.assertRaises(ValidationError):
                svc.import_personal_markdown("notes", source_id="page", source_path="notes.txt")
            self.assertFalse(svc.snapshot_root.exists())
            self.assertNotIn("outside-only-phrase", self._tree_text(root))

    def test_digest_unchanged_rebuild_provenance_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            svc = self._project(root)
            staged = self._stage(svc, "notes/page.md", f"# Page\n\n{PHRASE}\n")
            source = svc.import_personal_markdown(
                "notes",
                source_id="page",
                source_path="notes/page.md",
                title="Page",
            )
            self.assertEqual(source.provider, "local-markdown")
            self.assertEqual(source.source_class, "personal")
            snapshot = svc.snapshot_root / "notes" / "page.md"
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            os.utime(staged, (1_700_000_000, 1_700_000_000))
            os.utime(snapshot, (1_700_000_000, 1_700_000_000))

            first = svc.sync_project("notes")
            self.assertEqual(first[0].sync_state, "success")
            self.assertEqual(first[0].source_revision, digest)
            body = (svc.projections.root / first[0].projection_path).read_text(encoding="utf-8")
            self.assertIn("canonical engineering truth", body)
            self.assertIn("Source class: `personal`", body)
            self.assertIn("Engineering authority: `false`", body)

            second = svc.sync_project("notes")
            self.assertEqual(second[0].sync_state, "unchanged")
            self.assertEqual(second[0].source_revision, digest)
            rebuilt = svc.rebuild_project("notes")
            self.assertEqual(rebuilt[0].source_revision, digest)
            rebuilt_body = (svc.projections.root / rebuilt[0].projection_path).read_text(
                encoding="utf-8"
            )
            self.assertEqual(rebuilt_body, body)

            hits = svc.search("notes", PHRASE)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].provenance["source_class"], "personal")
            self.assertIs(hits[0].provenance["engineering_authority"], False)
            self.assertIs(hits[0].provenance["canonical"], False)
            self.assertEqual(hits[0].provenance["repository"], "local/markdown")

            report = backup_data_root(root, base / "backup")
            self.assertEqual(report["status"], "ok")
            stored = base / "backup" / "personal-snapshots" / "notes" / "page.md"
            self.assertEqual(stored.read_bytes(), snapshot.read_bytes())
            self.assertFalse((base / "backup" / "personal-import").exists())
            restore_test(base / "backup", base / "proof")
            self.assertEqual(
                (base / "proof" / "personal-snapshots" / "notes" / "page.md").read_bytes(),
                snapshot.read_bytes(),
            )

    def test_missing_snapshot_does_not_mutate_and_is_not_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            svc = self._project(root)
            self._stage(svc, "notes/page.md", f"# Page\n\n{PHRASE}\n")
            svc.import_personal_markdown("notes", source_id="page", source_path="notes/page.md")
            svc.sync_project("notes")
            shutil.rmtree(svc.snapshot_root)
            before = sorted(path.name for path in root.iterdir())
            record = svc.sync_project("notes")[0]
            after = sorted(path.name for path in root.iterdir())
            self.assertEqual(record.sync_state, "error")
            self.assertEqual(before, after)
            self.assertFalse(svc.snapshot_root.exists())
            self.assertEqual(svc.search("notes", PHRASE), [])

    def test_personal_class_and_authority_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            svc = self._project(root)
            self._stage(svc, "notes/page.md", f"# Page\n\n{PHRASE}\n")
            svc.import_personal_markdown("notes", source_id="page", source_path="notes/page.md")
            svc.sync_project("notes")
            self._tamper_projection(svc, "Engineering authority: `false`", "Engineering authority: `true`")
            with self.assertRaises(ValidationError):
                build_keyword_retriever(svc.projections, "notes")
            self._tamper_projection(svc, "Engineering authority: `true`", "Engineering authority: `false`")
            build_keyword_retriever(svc.projections, "notes")
            self._tamper_projection(svc, "Source class: `personal`", "Source class: `engineering`")
            with self.assertRaises(ValidationError):
                build_keyword_retriever(svc.projections, "notes")

    def test_github_retrieval_provenance_shape_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = AtlasService(Path(tmp))
            svc.register_project(project_id="alpha", repository="datarelay-labs/alpha")
            svc.add_source("alpha", source_id="charter", source_path="docs/charter.md")

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(content="github charter body\n", source_revision="rev-gh")

            svc.sync_project("alpha", fetch=fetch)
            body = next(svc.projections.root.rglob("charter.md")).read_text(encoding="utf-8")
            self.assertNotIn("Source class:", body)
            self.assertNotIn("Engineering authority:", body)
            hit = svc.search("alpha", "charter")[0]
            self.assertEqual(set(hit.provenance), LEGACY_GITHUB_PROVENANCE_KEYS)
            self.assertEqual(hit.provenance["provider"], "github")
            self.assertIs(hit.provenance["canonical"], False)

    def _tamper_projection(self, svc: AtlasService, old: str, new: str) -> None:
        meta_path = svc.projections.root / "projections.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        record = meta["projections"]["notes/page"]
        path = svc.projections.root / record["projection_path"]
        text = path.read_text(encoding="utf-8").replace(old, new, 1)
        data = text.encode("utf-8")
        path.write_text(text, encoding="utf-8")
        record["content_digest"] = hashlib.sha256(data).hexdigest()
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _tree_text(self, root: Path) -> str:
        if not root.exists():
            return ""
        chunks: list[str] = []
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        return "\n".join(chunks)


if __name__ == "__main__":
    unittest.main()
