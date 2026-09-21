"""Phase 1 project registry, adoption, sync, and rebuild regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas.adoption import parse_adoption_yaml
from atlas.github_sync import FetchedSource
from atlas.provenance import ValidationError
from atlas.service import AtlasService

PROJECT_YAML = """
engineering_system:
  version: "1.6.0"
  mode: adopted
  baseline: "673c3b339a0765657214d40a22400a04ed54425b"
  ci_mode: shared
project:
  name: "datarelay-atlas"
  type: "engineering-platform"
"""


class Phase1RegistryTests(unittest.TestCase):
    def _svc(self, tmp: str) -> AtlasService:
        return AtlasService(Path(tmp))

    def test_valid_project_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            project = svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
                display_name="DataRelay Atlas",
            )
            self.assertEqual(project.project_id, "datarelay-atlas")
            listed = svc.list_projects()
            self.assertEqual(len(listed), 1)
            shown = svc.show_project("datarelay-atlas")
            self.assertEqual(shown["project"]["repository"], "datarelay-labs/datarelay-atlas")

    def test_invalid_and_duplicate_project_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            with self.assertRaises(ValidationError):
                svc.register_project(
                    project_id="BAD_ID",
                    repository="datarelay-labs/datarelay-atlas",
                )
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )
            with self.assertRaises(ValidationError):
                svc.register_project(
                    project_id="datarelay-atlas",
                    repository="datarelay-labs/datarelay-atlas",
                )

    def test_repo_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )
            with self.assertRaises(ValidationError):
                svc.registry.assert_repository_identity(
                    "datarelay-atlas",
                    "datarelay-labs/other",
                )

    def test_engineering_system_metadata_parse(self):
        adoption = parse_adoption_yaml(
            PROJECT_YAML, source_path=".engineering/project.yaml"
        )
        self.assertEqual(adoption.project_name, "datarelay-atlas")
        self.assertEqual(adoption.engineering_system_version, "1.6.0")
        self.assertEqual(
            adoption.engineering_system_baseline,
            "673c3b339a0765657214d40a22400a04ed54425b",
        )

    def test_path_traversal_rejection_on_source_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )
            with self.assertRaises(ValidationError):
                svc.add_source(
                    "datarelay-atlas",
                    source_id="evil",
                    source_path="../secrets",
                )

    def test_authenticated_fetch_contract_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )
            svc.add_source(
                "datarelay-atlas",
                source_id="charter",
                source_path="docs/product/PRODUCT-CHARTER.md",
                title="Charter",
            )

            seen_tokens: list[str | None] = []

            def fetch(source, token):  # noqa: ARG001
                seen_tokens.append(token)
                return FetchedSource(content="# Charter\n\nbody", source_revision="rev-a")

            records = svc.sync_project("datarelay-atlas", token="secret-token", fetch=fetch)
            self.assertEqual(records[0].sync_state, "success")
            self.assertEqual(records[0].source_revision, "rev-a")
            self.assertEqual(seen_tokens, ["secret-token"])
            meta = svc.projection_records("datarelay-atlas")[0]
            self.assertEqual(meta["provenance"]["source_revision"], "rev-a")
            self.assertNotIn("secret-token", str(meta))

    def test_deterministic_no_change_resync_and_changed_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )
            svc.add_source(
                "datarelay-atlas",
                source_id="charter",
                source_path="docs/product/PRODUCT-CHARTER.md",
            )
            payloads = [
                FetchedSource(content="one", source_revision="r1"),
                FetchedSource(content="one", source_revision="r1"),
                FetchedSource(content="two", source_revision="r2"),
            ]

            def fetch(source, token):  # noqa: ARG001
                return payloads.pop(0)

            first = svc.sync_project("datarelay-atlas", fetch=fetch)[0]
            second = svc.sync_project("datarelay-atlas", fetch=fetch)[0]
            third = svc.sync_project("datarelay-atlas", fetch=fetch)[0]
            self.assertEqual(first.sync_state, "success")
            self.assertEqual(second.sync_state, "unchanged")
            self.assertEqual(first.content_digest, second.content_digest)
            self.assertEqual(third.sync_state, "success")
            self.assertEqual(third.source_revision, "r2")
            self.assertNotEqual(first.content_digest, third.content_digest)

    def test_explicit_fetch_failure_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )
            svc.add_source(
                "datarelay-atlas",
                source_id="charter",
                source_path="docs/product/PRODUCT-CHARTER.md",
            )

            def ok(source, token):  # noqa: ARG001
                return FetchedSource(content="ok", source_revision="r1")

            def boom(source, token):  # noqa: ARG001
                raise ValidationError("GitHub returned HTTP 503")

            svc.sync_project("datarelay-atlas", fetch=ok)
            failed = svc.sync_project("datarelay-atlas", fetch=boom)[0]
            self.assertEqual(failed.sync_state, "error")
            # Prior bytes may remain on disk, but list_documents ignores error state.
            self.assertEqual(svc.projections.list_documents("datarelay-atlas"), [])

    def test_project_namespace_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            for project_id, body in (
                ("alpha", "alpha-body"),
                ("beta", "beta-body"),
            ):
                svc.register_project(
                    project_id=project_id,
                    repository="datarelay-labs/datarelay-atlas",
                )
                svc.add_source(
                    project_id,
                    source_id="doc",
                    source_path="docs/product/PRODUCT-CHARTER.md",
                )

                def fetch(source, token, content=body):  # noqa: ARG001
                    return FetchedSource(content=content, source_revision="r1")

                svc.sync_project(project_id, fetch=fetch)

            alpha_docs = svc.projections.list_documents("alpha")
            beta_docs = svc.projections.list_documents("beta")
            self.assertEqual(len(alpha_docs), 1)
            self.assertEqual(len(beta_docs), 1)
            self.assertIn("alpha-body", alpha_docs[0][2])
            self.assertIn("beta-body", beta_docs[0][2])
            self.assertNotIn("beta-body", alpha_docs[0][2])

    def test_rebuild_from_registered_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )
            svc.add_source(
                "datarelay-atlas",
                source_id="charter",
                source_path="docs/product/PRODUCT-CHARTER.md",
            )
            svc.add_source(
                "datarelay-atlas",
                source_id="arch",
                source_path="docs/architecture/ARCHITECTURE.md",
            )

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(
                    content=f"body:{source.source_id}",
                    source_revision=f"rev-{source.source_id}",
                )

            first = svc.sync_project("datarelay-atlas", fetch=fetch)
            self.assertEqual(len(first), 2)
            rebuilt = svc.rebuild_project("datarelay-atlas", fetch=fetch)
            self.assertEqual(len(rebuilt), 2)
            self.assertTrue(all(r.sync_state == "success" for r in rebuilt))
            docs = {sid: text for _, sid, text in svc.projections.list_documents("datarelay-atlas")}
            self.assertIn("body:charter", docs["charter"])
            self.assertIn("body:arch", docs["arch"])

    def test_adoption_reader_via_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            svc.register_project(
                project_id="datarelay-atlas",
                repository="datarelay-labs/datarelay-atlas",
            )

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(content=PROJECT_YAML, source_revision="meta1")

            adoption = svc.read_adoption("datarelay-atlas", fetch=fetch)
            self.assertEqual(adoption.engineering_system_version, "1.6.0")

            bad_yaml = PROJECT_YAML.replace("datarelay-atlas", "other-project")

            def fetch_bad(source, token):  # noqa: ARG001
                return FetchedSource(content=bad_yaml, source_revision="meta2")

            with self.assertRaises(ValidationError):
                svc.read_adoption("datarelay-atlas", fetch=fetch_bad)


if __name__ == "__main__":
    unittest.main()
