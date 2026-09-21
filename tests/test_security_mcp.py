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


if __name__ == "__main__":
    unittest.main()
