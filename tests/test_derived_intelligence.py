import tempfile
import unittest
from pathlib import Path

from atlas.derived_intelligence import derived_intelligence_payload
from atlas.github_sync import FetchedSource
from atlas.provenance import ValidationError
from atlas.service import AtlasService

class DerivedIntelligenceTests(unittest.TestCase):
    def seed(self,tmp):
        svc=AtlasService(Path(tmp))
        svc.register_project(project_id="alpha",repository="datarelay-labs/alpha")
        svc.add_source("alpha",source_id="design",source_path="docs/DESIGN.md")
        content="# Design\n\n## Rollout Model\n\nSee datarelay-labs/beta and docs/decisions/ADR-0016-human.md.\n\nQUESTION: Who owns rollout?\nTODO: define rollback signal\n"
        svc.sync_project("alpha",fetch=lambda s,t: FetchedSource(content=content,source_revision="a"*40))
        svc.register_project(project_id="beta",repository="datarelay-labs/beta")
        svc.add_source("beta",source_id="readme",source_path="README.md")
        svc.sync_project("beta",fetch=lambda s,t: FetchedSource(content="# Beta\nSee datarelay-labs/alpha.",source_revision="b"*40))
        return svc
    def test_explicit_links_decisions_and_questions_are_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc=self.seed(tmp); payload=derived_intelligence_payload(svc.projections,["alpha","beta"])
            self.assertFalse(payload["canonical"]); self.assertTrue(payload["derived"])
            self.assertEqual(payload["contradictions"]["state"],"NONE_OBSERVED")
            self.assertEqual(payload["contradictions"]["semantic_state"],"UNKNOWN")
            kinds={(i["kind"],i["value"],i["source_project_id"]) for i in payload["items"]}
            self.assertIn(("cross_project_link","datarelay-labs/beta","alpha"),kinds)
            self.assertIn(("cross_project_link","datarelay-labs/alpha","beta"),kinds)
            self.assertIn(("concept_heading","Design","alpha"),kinds)
            self.assertIn(("concept_heading","Rollout Model","alpha"),kinds)
            self.assertIn(("decision_backlink","ADR-0016","alpha"),kinds)
            self.assertEqual(payload["decision_backlinks"]["ADR-0016"],[{"source_project_id":"alpha","source_identity":"design@main"}])
            self.assertIn(("unanswered_question","Who owns rollout?","alpha"),kinds)
            self.assertIn(("unanswered_question","define rollback signal","alpha"),kinds)
            for item in payload["items"]:
                self.assertIn("source_revision",item["provenance"]); self.assertTrue(item["provenance"]["derived"]); self.assertFalse(item["provenance"]["canonical"])
    def test_plain_question_sentence_is_not_promoted_to_tracking_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc=AtlasService(Path(tmp)); svc.register_project(project_id="alpha",repository="datarelay-labs/alpha"); svc.add_source("alpha",source_id="note",source_path="NOTE.md")
            svc.sync_project("alpha",fetch=lambda s,t: FetchedSource(content="Is this merely prose?\nQUESTION: tracked explicitly?",source_revision="a"*40))
            values=[i["value"] for i in derived_intelligence_payload(svc.projections,["alpha"])["items"] if i["kind"]=="unanswered_question"]
            self.assertEqual(values,["tracked explicitly?"])

    def test_contradiction_state_survives_item_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = AtlasService(Path(tmp))
            svc.register_project(
                project_id="alpha",
                repository="datarelay-labs/alpha",
            )
            svc.add_source(
                "alpha",
                source_id="dense",
                source_path="DENSE.md",
            )
            headings = chr(10).join(
                f"## Heading {index:03d}" for index in range(500)
            )
            content = (
                "# Dense" + chr(10) * 2 + headings
                + chr(10) * 2
                + "CONTRADICTION: actual conflict after the cap"
                + chr(10)
            )
            svc.sync_project(
                "alpha",
                fetch=lambda s, t: FetchedSource(
                    content=content,
                    source_revision="c" * 40,
                ),
            )
            payload = derived_intelligence_payload(
                svc.projections, ["alpha"]
            )
            self.assertEqual(len(payload["items"]), 500)
            self.assertEqual(payload["contradictions"]["state"], "DETECTED")
            self.assertEqual(
                payload["contradictions"]["semantic_state"], "UNKNOWN"
            )
            self.assertIn(
                "outside the bounded display set",
                payload["contradictions"]["detail"],
            )

    def test_project_intelligence_does_not_leak_other_project_contradictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = AtlasService(Path(tmp))
            svc.register_project(
                project_id="alpha",
                repository="datarelay-labs/alpha",
            )
            svc.add_source(
                "alpha",
                source_id="alpha-note",
                source_path="ALPHA.md",
            )
            svc.sync_project(
                "alpha",
                fetch=lambda s, t: FetchedSource(
                    content="# Alpha",
                    source_revision="a" * 40,
                ),
            )
            svc.register_project(
                project_id="beta",
                repository="datarelay-labs/beta",
            )
            svc.add_source(
                "beta",
                source_id="beta-note",
                source_path="BETA.md",
            )
            svc.sync_project(
                "beta",
                fetch=lambda s, t: FetchedSource(
                    content="# Beta" + chr(10) + "CONTRADICTION: beta only",
                    source_revision="b" * 40,
                ),
            )

            alpha = svc.project_intelligence("alpha")
            beta = svc.project_intelligence("beta")
            self.assertEqual(alpha["contradictions"]["state"], "NONE_OBSERVED")
            self.assertEqual(alpha["contradictions"]["items"], [])
            self.assertEqual(beta["contradictions"]["state"], "DETECTED")
            self.assertTrue(
                all(
                    item["source_project_id"] == "beta"
                    for item in beta["contradictions"]["items"]
                )
            )

    def test_rebuild_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc=self.seed(tmp)
            self.assertEqual(derived_intelligence_payload(svc.projections,["beta","alpha"]),derived_intelligence_payload(svc.projections,["alpha","beta"]))
    def test_tampered_projection_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc=self.seed(tmp); rec=svc.projection_records("alpha")[0]; path=svc.projections.root/rec["projection_path"]; path.write_text(path.read_text()+"tamper")
            with self.assertRaisesRegex(ValidationError,"digest mismatch"): derived_intelligence_payload(svc.projections,["alpha"])
    def test_own_repository_reference_is_not_cross_project_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc=AtlasService(Path(tmp)); svc.register_project(project_id="alpha",repository="datarelay-labs/alpha"); svc.add_source("alpha",source_id="self",source_path="SELF.md")
            svc.sync_project("alpha",fetch=lambda s,t: FetchedSource(content="datarelay-labs/alpha",source_revision="a"*40))
            payload=derived_intelligence_payload(svc.projections,["alpha"]); self.assertEqual(payload["items"],[])

if __name__=="__main__": unittest.main()
