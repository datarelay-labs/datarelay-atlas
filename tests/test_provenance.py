import unittest

from atlas.provenance import (
    CanonicalSource,
    ValidationError,
    provenance_from_source,
    render_derived_document,
    validate_source,
)


SOURCE = CanonicalSource(
    source_id="canonical-source-of-truth",
    project_id="data-relay-link",
    provider="github",
    repository="datarelay-labs/data-relay-link",
    ref="main",
    source_path="knowledge/CANONICAL_SOURCE_OF_TRUTH.md",
    title="Data Relay Link — Canonical Source of Truth",
)


class ProvenanceTests(unittest.TestCase):
    def test_accepts_canonical_project_source(self):
        validate_source(SOURCE)

    def test_rejects_traversal(self):
        with self.assertRaises(ValidationError):
            validate_source(
                CanonicalSource(
                    **{**SOURCE.__dict__, "source_path": "../secret"}
                )
            )

    def test_render_marks_derived_and_preserves_metadata(self):
        text = render_derived_document(SOURCE, "# Canonical\n\nDecision body", "abc123")
        self.assertIn("Derived knowledge. Do not treat this projection as canonical.", text)
        self.assertIn("GitHub/repository artifacts remain authoritative", text)
        self.assertIn("datarelay-labs/data-relay-link", text)
        self.assertIn("abc123", text)
        self.assertIn("Canonical: `false`", text)
        self.assertIn("Derived: `true`", text)
        self.assertIn("# Canonical", text)
        self.assertIn("Decision body", text)
        self.assertNotIn("wiki_path", text)

    def test_provenance_fields(self):
        prov = provenance_from_source(SOURCE, "abc123")
        self.assertEqual(prov.project_id, "data-relay-link")
        self.assertEqual(prov.source_revision, "abc123")
        self.assertTrue(prov.derived)
        self.assertFalse(prov.canonical)


if __name__ == "__main__":
    unittest.main()
