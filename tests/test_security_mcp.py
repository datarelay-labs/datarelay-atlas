import unittest

from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import Provenance
from atlas.retrieval import IndexedDocument, KeywordIndex, Retriever
from atlas.security import (
    READ_SCOPE,
    WRITE_SCOPE,
    assert_safe_source_path,
    authorize_tool,
    reject_canonical_mutation,
)
from atlas.provenance import ValidationError


class SecurityTests(unittest.TestCase):
    def test_rejects_path_traversal(self):
        with self.assertRaises(ValidationError):
            assert_safe_source_path("../secret")

    def test_write_tool_requires_write_scope(self):
        self.assertFalse(
            authorize_tool("create_note", [READ_SCOPE], write_tools={"create_note"})
        )
        self.assertTrue(
            authorize_tool("create_note", [WRITE_SCOPE], write_tools={"create_note"})
        )

    def test_rejects_canonical_mutation(self):
        with self.assertRaises(ValidationError):
            reject_canonical_mutation("github")


class McpContextTests(unittest.TestCase):
    def setUp(self):
        index = KeywordIndex()
        prov = Provenance(
            project_id="demo",
            provider="github",
            repository="datarelay-labs/datarelay-atlas",
            ref="main",
            source_path="README.md",
            source_revision="abc",
        )
        index.add(
            IndexedDocument(
                project_id="demo",
                path="README.md",
                title="README",
                text="DataRelay Atlas engineering knowledge platform",
                provenance=prov,
            )
        )
        self.tools = AtlasContextTools(
            Retriever(index, provenance_by_path={("demo", "README.md"): prov})
        )

    def test_search_returns_provenance(self):
        result = self.tools.call(
            "search_project",
            {"project_id": "demo", "query": "engineering", "limit": 5},
            scopes=default_read_scopes(),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data[0]["provenance"]["source_revision"], "abc")
        self.assertFalse(result.data[0]["provenance"]["canonical"])

    def test_write_without_scope_unauthorized(self):
        result = self.tools.call(
            "create_note",
            {"project_id": "demo", "text": "x", "target": "derived"},
            scopes=default_read_scopes(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unauthorized")

    def test_list_tools_hides_write_without_scope(self):
        names = {t["name"] for t in self.tools.list_tools(default_read_scopes())}
        self.assertIn("search_project", names)
        self.assertNotIn("create_note", names)

    def test_provenance_round_trip_uses_search_path_when_unique(self):
        result = self.tools.call(
            "search_project",
            {"project_id": "demo", "query": "engineering"},
            scopes=default_read_scopes(),
        )
        hit = result.data[0]
        fetched = self.tools.call(
            "get_provenance",
            {"project_id": "demo", "path": hit["path"]},
            scopes=default_read_scopes(),
        )
        self.assertTrue(fetched.ok)
        self.assertEqual(fetched.data["source_revision"], "abc")

    def test_shared_source_path_requires_identity(self):
        index = KeywordIndex()
        main = Provenance(
            project_id="demo",
            provider="github",
            repository="datarelay-labs/datarelay-atlas",
            ref="main",
            source_path="docs/shared.md",
            source_revision="rev-main",
        )
        release = Provenance(
            project_id="demo",
            provider="github",
            repository="datarelay-labs/datarelay-atlas",
            ref="v1",
            source_path="docs/shared.md",
            source_revision="rev-release",
        )
        index.add(
            IndexedDocument(
                project_id="demo",
                path="from-main@main",
                title="docs/shared.md",
                text="shared-token main",
                provenance=main,
            )
        )
        index.add(
            IndexedDocument(
                project_id="demo",
                path="from-release@v1",
                title="docs/shared.md",
                text="shared-token release",
                provenance=release,
            )
        )
        tools = AtlasContextTools(
            Retriever(
                index,
                provenance_by_path={
                    ("demo", "from-main@main"): main,
                    ("demo", "from-release@v1"): release,
                },
            )
        )
        result = tools.call(
            "search_project",
            {"project_id": "demo", "query": "shared-token"},
            scopes=default_read_scopes(),
        )
        self.assertTrue(result.ok)
        self.assertEqual({hit["path"] for hit in result.data}, {"docs/shared.md"})
        self.assertEqual(
            {hit["identity"] for hit in result.data},
            {"from-main@main", "from-release@v1"},
        )
        ambiguous = tools.call(
            "get_provenance",
            {"project_id": "demo", "path": "docs/shared.md"},
            scopes=default_read_scopes(),
        )
        self.assertFalse(ambiguous.ok)
        self.assertEqual(ambiguous.error, "ambiguous")
        by_identity = {
            hit["identity"]: tools.call(
                "get_provenance",
                {"project_id": "demo", "identity": hit["identity"]},
                scopes=default_read_scopes(),
            )
            for hit in result.data
        }
        self.assertEqual(by_identity["from-main@main"].data["source_revision"], "rev-main")
        self.assertEqual(by_identity["from-release@v1"].data["source_revision"], "rev-release")

    def test_path_lookup_ignores_identity_key_collision(self):
        index = KeywordIndex()
        keyed = Provenance(
            project_id="demo",
            provider="github",
            repository="datarelay-labs/datarelay-atlas",
            ref="main",
            source_path="docs/real.md",
            source_revision="rev-keyed",
        )
        pathed = Provenance(
            project_id="demo",
            provider="github",
            repository="datarelay-labs/datarelay-atlas",
            ref="main",
            source_path="foo@main",
            source_revision="rev-path",
        )
        index.add(
            IndexedDocument(
                project_id="demo",
                path="foo@main",
                title="docs/real.md",
                text="collision-token keyed",
                provenance=keyed,
            )
        )
        index.add(
            IndexedDocument(
                project_id="demo",
                path="other@main",
                title="foo@main",
                text="collision-token path",
                provenance=pathed,
            )
        )
        tools = AtlasContextTools(
            Retriever(
                index,
                provenance_by_path={
                    ("demo", "foo@main"): keyed,
                    ("demo", "other@main"): pathed,
                },
            )
        )
        by_path = tools.call(
            "get_provenance",
            {"project_id": "demo", "path": "foo@main"},
            scopes=default_read_scopes(),
        )
        self.assertTrue(by_path.ok)
        self.assertEqual(by_path.data["source_revision"], "rev-path")
        self.assertEqual(by_path.data["source_path"], "foo@main")
        by_identity = tools.call(
            "get_provenance",
            {"project_id": "demo", "identity": "foo@main"},
            scopes=default_read_scopes(),
        )
        self.assertTrue(by_identity.ok)
        self.assertEqual(by_identity.data["source_revision"], "rev-keyed")
        self.assertEqual(by_identity.data["source_path"], "docs/real.md")

    def test_read_tools_reject_write_only_scope(self):
        result = self.tools.call(
            "search_project",
            {"project_id": "demo", "query": "engineering"},
            scopes=[WRITE_SCOPE],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unauthorized")


if __name__ == "__main__":
    unittest.main()
