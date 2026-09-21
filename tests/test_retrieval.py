import unittest

from atlas.retrieval import ClassicHit, SemanticHit, merge_search_hits, snippet


class MergeSearchHitsTests(unittest.TestCase):
    def test_classic_only_survives(self):
        merged = merge_search_hits(
            [ClassicHit(path="it/ssh-key-gitlab", title="SSH key", description="ed25519")],
            [
                SemanticHit(
                    path="it/overview",
                    title="Overview",
                    content="SSH in general",
                    headings={"h1": "Overview"},
                )
            ],
            8,
        )
        paths = [h.path for h in merged]
        self.assertIn("it/ssh-key-gitlab", paths)
        self.assertIn("it/overview", paths)
        self.assertEqual(next(h for h in merged if h.path == "it/ssh-key-gitlab").match, "classic")

    def test_semantic_only_survives_with_headings(self):
        merged = merge_search_hits(
            [],
            [
                SemanticHit(
                    path="it/ssh-key-gitlab",
                    title="SSH key",
                    content="ssh-keygen -t ed25519",
                    headings={"h2": "Key"},
                )
            ],
            8,
        )
        self.assertEqual(merged[0].match, "semantic")
        self.assertEqual(merged[0].headings["h2"], "Key")

    def test_both_ranks_first(self):
        merged = merge_search_hits(
            [
                ClassicHit(path="it/upstream", title="Upstream", description="VPN"),
                ClassicHit(path="it/ssh-key-gitlab", title="SSH key", description="ed25519"),
            ],
            [
                SemanticHit(
                    path="it/ssh-key-gitlab",
                    title="SSH key",
                    content="IdentityFile ~/.ssh/id_ed25519_gitlab",
                    headings={"h1": "SSH key"},
                ),
                SemanticHit(path="sport/running", title="Running", content="Tuesdays"),
            ],
            8,
        )
        self.assertEqual(merged[0].path, "it/ssh-key-gitlab")
        self.assertEqual(merged[0].match, "both")
        self.assertIn("id_ed25519_gitlab", merged[0].content)
        self.assertEqual({h.match for h in merged}, {"both", "classic", "semantic"})

    def test_limit_and_one_hit_per_path(self):
        merged = merge_search_hits(
            [ClassicHit(path="a/one", title="One", description="x")],
            [
                SemanticHit(path="a/one", title="One", content="first"),
                SemanticHit(path="a/one", title="One", content="second"),
                SemanticHit(path="b/two", title="Two", content="other"),
            ],
            1,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].path, "a/one")
        self.assertEqual(merged[0].content, "first")

    def test_slash_normalization(self):
        merged = merge_search_hits(
            [ClassicHit(path="/a/one/", title="One", description="x")],
            [SemanticHit(path="a/one", title="One", content="chunk")],
            8,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].match, "both")

    def test_empty_paths_dropped(self):
        self.assertEqual(merge_search_hits([ClassicHit(path="")], [SemanticHit(path="")], 8), [])

    def test_empty_input(self):
        self.assertEqual(merge_search_hits([], [], 8), [])


class SnippetTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(snippet("a   b\n\nc\t d"), "a b c d")

    def test_truncates(self):
        out = snippet("z" * 500, 10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))


if __name__ == "__main__":
    unittest.main()
