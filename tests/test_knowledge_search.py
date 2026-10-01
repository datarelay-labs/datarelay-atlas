from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from atlas.cli import main
from atlas.github_sync import FetchedSource
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app


class KnowledgeSearchTests(unittest.TestCase):
    def _seed(self, root: Path) -> AtlasService:
        svc = AtlasService(root)
        svc.register_project(project_id="alpha", repository="datarelay-labs/alpha", display_name="Alpha")
        svc.register_project(project_id="beta", repository="datarelay-labs/beta", display_name="Beta")
        svc.add_source("alpha", source_id="eng", source_path="docs/eng.md", title="Engineering")
        svc.add_source("beta", source_id="eng", source_path="docs/eng.md", title="Beta engineering")
        svc.import_root.mkdir(parents=True, exist_ok=True)
        (svc.import_root / "alpha-personal.md").write_text("shared-quill personal-alpha-only", encoding="utf-8")
        svc.import_personal_markdown(
            "alpha",
            source_id="personal",
            source_path="alpha-personal.md",
            title="Alpha personal",
        )

        def fetch(source, token):  # noqa: ARG001
            marker = "engineering-alpha-only" if source.project_id == "alpha" else "engineering-beta-only"
            return FetchedSource(content=f"shared-quill {marker}", source_revision=f"rev-{source.project_id}")

        for project_id in ("alpha", "beta"):
            for source in svc.registry.canonical_sources(project_id):
                svc.projections.sync_one(
                    source,
                    fetch=fetch if source.provider == "github" else None,
                )
        return svc

    def test_source_class_filters_before_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(Path(tmp))
            all_results = svc.search_across_projects(
                "shared-quill",
                project_ids=["alpha", "beta"],
                source_class="all",
            )
            self.assertEqual(all_results["total"], 3)
            self.assertEqual(all_results["class_counts"], {"engineering": 2, "personal": 1})

            personal = svc.search_across_projects(
                "shared-quill",
                project_ids=["alpha", "beta"],
                source_class="personal",
            )
            self.assertEqual(personal["total"], 1)
            hit = next(group["hits"][0] for group in personal["groups"] if group["hits"])
            self.assertEqual(hit["project_id"], "alpha")
            self.assertEqual(hit["provenance"]["source_class"], "personal")
            self.assertIs(hit["provenance"]["engineering_authority"], False)

            engineering = svc.search_across_projects(
                "shared-quill",
                project_ids=["alpha", "beta"],
                source_class="engineering",
            )
            self.assertEqual(engineering["total"], 2)
            self.assertEqual(engineering["class_counts"], {"engineering": 2, "personal": 0})
            self.assertTrue(
                all(
                    hit["provenance"].get("source_class", "engineering") == "engineering"
                    for group in engineering["groups"]
                    for hit in group["hits"]
                )
            )

    def test_explicit_project_scope_and_invalid_filter_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._seed(Path(tmp))
            scoped = svc.search_across_projects(
                "shared-quill",
                project_ids=["beta"],
                source_class="all",
            )
            self.assertEqual(scoped["project_ids"], ["beta"])
            self.assertEqual(scoped["total"], 1)
            with self.assertRaises(ValidationError):
                svc.search_across_projects("x", project_ids=[], source_class="all")
            with self.assertRaises(ValidationError):
                svc.search_across_projects("x", project_ids=["alpha"], source_class="unknown")

    def test_cli_and_mcp_share_same_scoped_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc = self._seed(root)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(
                        [
                            "--data-root", str(root),
                            "search-all", "shared-quill",
                            "--source-class", "personal",
                            "--project-id", "alpha",
                            "--project-id", "beta",
                        ]
                    ),
                    0,
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["source_class"], "personal")

            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                knowledge_search_factory=lambda query, project_ids, source_class, limit: svc.search_across_projects(
                    query,
                    project_ids=project_ids,
                    source_class=source_class,
                    limit_per_project=limit,
                ),
            )
            result = tools.call(
                "search_knowledge",
                {
                    "project_ids": ["alpha", "beta"],
                    "query": "shared-quill",
                    "source_class": "engineering",
                    "limit_per_project": 5,
                },
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["total"], 2)
            self.assertEqual(result.data["class_counts"]["personal"], 0)

    def test_web_search_selector_filters_personal_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed(root)
            app = create_app(root)
            env = {}
            setup_testing_defaults(env)
            env.update(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/search",
                    "QUERY_STRING": "q=shared-quill&source_class=personal",
                    "HTTP_HOST": "127.0.0.1:8788",
                }
            )
            state = {}

            def start(status, headers):
                state.update(status=status, headers=dict(headers))

            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Personal/reference only", body)
            self.assertIn("personal reference / non-authoritative", body)
            self.assertIn("alpha-personal.md", body)
            self.assertIn("personal@snapshot", body)
            self.assertNotIn("docs/eng.md", body)
            self.assertIn("engineering 0 · personal 1 · filter personal", body)


if __name__ == "__main__":
    unittest.main()
