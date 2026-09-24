"""Projection-to-keyword retrieval and operator search CLI regressions."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from atlas.cli import main
from atlas.github_sync import FetchedSource
from atlas.provenance import ValidationError
from atlas.service import AtlasService

UNIQUE_PHRASE = "phase2-search-token-charter-quill"
OTHER_PHRASE = "phase2-search-token-other-project"


class ProjectionRetrievalTests(unittest.TestCase):
    def _seed(self, tmp: str) -> AtlasService:
        svc = AtlasService(Path(tmp))
        svc.register_project(
            project_id="datarelay-atlas",
            repository="datarelay-labs/datarelay-atlas",
        )
        svc.add_source(
            "datarelay-atlas",
            source_id="charter",
            source_path="docs/product/PRODUCT-CHARTER.md",
            title="Product Charter",
        )
        svc.add_source(
            "datarelay-atlas",
            source_id="disabled-note",
            source_path="docs/product/DISABLED.md",
            enabled=False,
        )

        def fetch(source, token):  # noqa: ARG001
            if source.source_id == "charter":
                return FetchedSource(
                    content=f"# Charter\n\n{UNIQUE_PHRASE}\n",
                    source_revision="rev-charter",
                )
            return FetchedSource(content="should-not-index", source_revision="rev-disabled")

        svc.sync_project("datarelay-atlas", fetch=fetch)
        return svc

    def test_projected_canonical_content_is_searchable_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            stored = svc.projection_records("datarelay-atlas")
            charter = next(row for row in stored if row["source_id"] == "charter")
            hits = svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(len(hits), 1)
            hit = hits[0]
            self.assertEqual(hit.project_id, "datarelay-atlas")
            self.assertEqual(hit.path, "docs/product/PRODUCT-CHARTER.md")
            self.assertEqual(hit.match, "classic")
            body = (svc.projections.root / charter["projection_path"]).read_text(encoding="utf-8")
            self.assertIn(UNIQUE_PHRASE, body)
            self.assertEqual(hit.provenance["source_revision"], "rev-charter")
            self.assertEqual(hit.provenance["repository"], "datarelay-labs/datarelay-atlas")
            self.assertEqual(hit.provenance["source_path"], charter["provenance"]["source_path"])
            self.assertEqual(hit.provenance["ref"], charter["provenance"]["ref"])
            self.assertFalse(hit.provenance["canonical"])
            self.assertTrue(hit.provenance["derived"])
            self.assertEqual(hit.provenance, {
                key: charter["provenance"][key]
                for key in (
                    "project_id",
                    "provider",
                    "repository",
                    "ref",
                    "source_path",
                    "source_revision",
                    "derived",
                    "canonical",
                )
            })

    def test_project_scope_excludes_other_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            svc.register_project(
                project_id="other-proj",
                repository="datarelay-labs/datarelay-atlas",
            )
            svc.add_source(
                "other-proj",
                source_id="note",
                source_path="docs/product/OTHER.md",
            )

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(content=OTHER_PHRASE, source_revision="rev-other")

            svc.sync_project("other-proj", fetch=fetch)
            own = svc.search("datarelay-atlas", OTHER_PHRASE)
            other = svc.search("other-proj", OTHER_PHRASE)
            self.assertEqual(own, [])
            self.assertEqual(len(other), 1)
            self.assertEqual(other[0].project_id, "other-proj")
            self.assertNotIn(UNIQUE_PHRASE, other[0].content)

    def test_disabled_and_error_projections_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            hits = svc.search("datarelay-atlas", "should-not-index")
            self.assertEqual(hits, [])

            def ok(source, token):  # noqa: ARG001
                if source.source_id == "fragile":
                    return FetchedSource(content="recoverable-body", source_revision="r1")
                return FetchedSource(
                    content=f"# Charter\n\n{UNIQUE_PHRASE}\n",
                    source_revision="rev-charter",
                )

            def boom(source, token):  # noqa: ARG001
                if source.source_id == "fragile":
                    raise ValidationError("GitHub returned HTTP 503")
                return ok(source, token)

            svc.add_source(
                "datarelay-atlas",
                source_id="fragile",
                source_path="docs/product/FRAGILE.md",
            )
            svc.sync_project("datarelay-atlas", fetch=ok)
            svc.sync_project("datarelay-atlas", fetch=boom)
            self.assertEqual(svc.search("datarelay-atlas", "recoverable-body"), [])
            self.assertEqual(len(svc.search("datarelay-atlas", UNIQUE_PHRASE)), 1)

    def test_unchanged_projection_stays_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(
                    content=f"# Charter\n\n{UNIQUE_PHRASE}\n",
                    source_revision="rev-charter",
                )

            state = svc.sync_project("datarelay-atlas", fetch=fetch)
            charter = next(row for row in state if row.source_id == "charter")
            self.assertEqual(charter.sync_state, "unchanged")
            hits = svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].provenance["source_revision"], "rev-charter")

    def test_empty_and_unknown_query_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            self.assertEqual(svc.search("datarelay-atlas", ""), [])
            self.assertEqual(svc.search("datarelay-atlas", "   "), [])
            self.assertEqual(svc.search("datarelay-atlas", "no-such-token"), [])

    def test_unknown_project_and_invalid_limit_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            with self.assertRaises(ValidationError):
                svc.search("missing-proj", UNIQUE_PHRASE)
            with self.assertRaises(ValidationError):
                svc.search("datarelay-atlas", UNIQUE_PHRASE, limit=0)

    def test_missing_bytes_and_malformed_provenance_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            rel = svc.projection_records("datarelay-atlas")
            charter = next(row for row in rel if row["source_id"] == "charter")
            path = svc.projections.root / charter["projection_path"]
            path.unlink()
            with self.assertRaises(ValidationError) as missing:
                svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(
                str(missing.exception),
                "projection bytes missing: datarelay-atlas/charter",
            )

            path.write_text("restored\n", encoding="utf-8")
            meta_path = svc.projections.root / "projections.json"
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            payload["projections"]["datarelay-atlas/charter"]["provenance"] = {"project_id": "datarelay-atlas"}
            meta_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValidationError) as malformed:
                svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(
                str(malformed.exception),
                "malformed projection provenance: datarelay-atlas/charter",
            )

            meta_path.write_text("{", encoding="utf-8")
            with self.assertRaises(ValidationError) as corrupt:
                svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(str(corrupt.exception), "corrupt projection metadata")

    def test_tampered_projection_bytes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            rel = svc.projection_records("datarelay-atlas")
            charter = next(row for row in rel if row["source_id"] == "charter")
            path = svc.projections.root / charter["projection_path"]
            path.write_bytes(b"TAMPERED_TOKEN")
            with self.assertRaises(ValidationError) as mismatch:
                svc.search("datarelay-atlas", "TAMPERED_TOKEN")
            self.assertEqual(
                str(mismatch.exception),
                "projection bytes digest mismatch: datarelay-atlas/charter",
            )
            with self.assertRaises(ValidationError) as original:
                svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(
                str(original.exception),
                "projection bytes digest mismatch: datarelay-atlas/charter",
            )

    def test_metadata_source_revision_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            rel = svc.projection_records("datarelay-atlas")
            charter = next(row for row in rel if row["source_id"] == "charter")
            self.assertEqual(charter["source_revision"], "rev-charter")
            meta_path = svc.projections.root / "projections.json"
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            entry = payload["projections"]["datarelay-atlas/charter"]
            entry["provenance"]["source_revision"] = "forged-rev"
            meta_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValidationError) as mismatch:
                svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(
                str(mismatch.exception),
                "projection provenance mismatch: datarelay-atlas/charter",
            )
            stored = json.loads(meta_path.read_text(encoding="utf-8"))
            kept = stored["projections"]["datarelay-atlas/charter"]
            self.assertEqual(kept["source_revision"], "rev-charter")
            body = (svc.projections.root / charter["projection_path"]).read_text(encoding="utf-8")
            self.assertIn("rev-charter", body)
            self.assertNotIn("forged-rev", body)

    def test_metadata_source_path_misattribution_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(tmp)
            rel = svc.projection_records("datarelay-atlas")
            charter = next(row for row in rel if row["source_id"] == "charter")
            meta_path = svc.projections.root / "projections.json"
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            entry = payload["projections"]["datarelay-atlas/charter"]
            entry["provenance"]["source_path"] = "docs/forged.md"
            meta_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValidationError) as mismatch:
                svc.search("datarelay-atlas", UNIQUE_PHRASE)
            self.assertEqual(
                str(mismatch.exception),
                "projection provenance mismatch: datarelay-atlas/charter",
            )
            stored = json.loads(meta_path.read_text(encoding="utf-8"))
            kept = stored["projections"]["datarelay-atlas/charter"]
            self.assertEqual(kept["projection_path"], charter["projection_path"])
            body = (svc.projections.root / charter["projection_path"]).read_text(encoding="utf-8")
            self.assertIn("docs/product/PRODUCT-CHARTER.md", body)
            self.assertNotIn("docs/forged.md", body)

    def test_cli_search_returns_attributable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    ["--data-root", tmp, "search", "datarelay-atlas", UNIQUE_PHRASE]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["provenance"]["source_revision"], "rev-charter")
            self.assertEqual(payload[0]["path"], "docs/product/PRODUCT-CHARTER.md")

            empty = io.StringIO()
            with contextlib.redirect_stdout(empty):
                empty_code = main(
                    ["--data-root", tmp, "search", "datarelay-atlas", "no-such-token"]
                )
            self.assertEqual(empty_code, 0)
            self.assertEqual(json.loads(empty.getvalue()), [])

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                err = main(["--data-root", tmp, "search", "missing-proj", "x"])
            self.assertEqual(err, 1)
            self.assertIn("unknown project_id", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
