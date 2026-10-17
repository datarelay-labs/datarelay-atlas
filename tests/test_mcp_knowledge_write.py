"""Bounded MCP write application contract: no canonical mutation from memory."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atlas.github_sync import FetchedSource
from atlas.mcp_write import ingest_candidate_memory, register_project_github_markdown
from atlas.provenance import ValidationError
from atlas.service import AtlasService


REV = "a" * 40


def candidate(content="A bounded lesson about E2E reuse"):
    return [{"candidate_class": "LESSON_LEARNED", "content": content}]


class BoundedMcpWriteServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = AtlasService(self.root)
        self.service.register_project(
            project_id="alpha", repository="datarelay-labs/alpha"
        )
        self.service.register_project(
            project_id="beta", repository="datarelay-labs/beta"
        )

    def test_memory_candidate_persists_noncanonical_and_replays_idempotently(self):
        stamp = "2026-10-16T01:00:00Z"
        args = dict(
            project_id="alpha", input_kind="RUN_SUMMARY",
            observed_at=stamp, candidates=candidate(), workstream="e2e",
        )
        first = ingest_candidate_memory(self.service, **args)
        repeat = ingest_candidate_memory(self.service, **args)
        self.assertEqual(first["accepted"], 1)
        self.assertEqual(repeat["accepted"], 0)
        self.assertEqual(repeat["replayed"], 1)
        self.assertFalse(first["canonical"])
        items = self.service.memory_candidates(project_id="alpha")["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["repository"], "datarelay-labs/alpha")
        self.assertFalse(items[0]["canonical"])
        self.assertEqual(self.service.memory_candidates(project_id="beta")["count"], 0)

    def test_memory_rejects_credentials_future_timestamp_and_disallowed_event(self):
        base = dict(project_id="alpha", input_kind="INTERACTION_SUMMARY",
                    observed_at="2026-10-16T01:00:00Z")
        for extra in (
            {"candidates": candidate("password=super-secret-string")},
            {"candidates": []},
            {"candidates": candidate(), "input_kind": "CANONICAL_EVENT"},
            {"candidates": candidate(), "observed_at": "not-a-time"},
            {"candidates": candidate(), "observed_at": (
                datetime.now(timezone.utc) + timedelta(days=3)).isoformat()},
        ):
            with self.subTest(extra=str(extra)[:50]):
                with self.assertRaises(ValidationError):
                    ingest_candidate_memory(
                        self.service, **{**base, "candidates": candidate(), **extra}
                    )
        self.assertFalse((self.root / "memory-candidates.json").exists())

    def test_source_preflight_scopes_to_repository_default_ref_and_is_idempotent(self):
        seen = []

        def fetch(source, token):
            seen.append(source)
            self.assertIsNone(token)
            return FetchedSource(
                content="# Document\n\nAuthoritative GitHub runbook E2E-MCP-NEW.\n",
                source_revision=REV,
            )

        args = dict(
            project_id="alpha", source_id="new-runbook",
            source_path="docs/runbooks/access.md", title="Access runbook", fetch=fetch,
        )
        first = register_project_github_markdown(self.service, **args)
        again = register_project_github_markdown(self.service, **args)
        self.assertEqual(first["state"], "SYNCED")
        self.assertEqual(again["state"], "UNCHANGED")
        self.assertEqual(len(self.service.list_sources("alpha")), 1)
        self.assertEqual(first["source_revision"], REV)
        self.assertEqual(first["repository"], "datarelay-labs/alpha")
        self.assertEqual(first["ref"], "main")
        self.assertFalse(first["canonical"])
        self.assertEqual([x.repository for x in seen], ["datarelay-labs/alpha"] * 2)
        self.assertEqual([x.ref for x in seen], ["main"] * 2)
        hits = self.service.search("alpha", "E2E-MCP-NEW")
        self.assertTrue(hits)

    def test_source_rejects_arbitrary_paths_without_fetch_or_mutation(self):
        called = []
        def fetch(source, token):
            called.append(source)
            return FetchedSource("data", REV)
        base = dict(
            project_id="alpha", source_id="runbook",
            source_path="docs/runbooks/access.md", title="Access", fetch=fetch,
        )
        for extra in (
            {"source_path": "../../credentials.env"},
            {"source_path": ".github/workflows/x.md"},
            {"source_path": "docs/.secrets/a.md"},
            {"source_path": "docs/x.txt"},
            {"source_path": "https://example.net/x.md"},
            {"source_path": "docs/../bad.md"},
            {"source_id": "Bad/ID"},
            {"title": "password=super-secret-string"},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ValidationError):
                    register_project_github_markdown(self.service, **{**base, **extra})
        self.assertEqual(called, [])
        self.assertEqual(self.service.list_sources("alpha"), [])

    def test_source_conflict_and_preflight_failure_do_not_change_registry(self):
        args = dict(
            project_id="alpha", source_id="docs", title="Docs",
            source_path="docs/runbooks/access.md",
        )
        def fail(_source, _token):
            raise RuntimeError("DO_NOT_SHOW_IN_MCP")
        with self.assertRaisesRegex(ValidationError, "fetch failed") as caught:
            register_project_github_markdown(self.service, **args, fetch=fail)
        self.assertNotIn("DO_NOT_SHOW_IN_MCP", str(caught.exception))
        self.assertFalse(self.service.list_sources("alpha"))
        def ok(_source, _token):
            return FetchedSource("Clean canonical runbook", REV)
        register_project_github_markdown(self.service, **args, fetch=ok)
        with self.assertRaisesRegex(ValidationError, "conflicts"):
            register_project_github_markdown(
                self.service, **{**args, "source_path": "docs/different.md"},
                fetch=ok,
            )
        self.assertEqual(self.service.list_sources("alpha")[0].source_path, args["source_path"])


if __name__ == "__main__":
    unittest.main()
