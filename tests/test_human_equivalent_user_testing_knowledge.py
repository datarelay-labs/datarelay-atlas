import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / ".engineering" / "project.yaml"


class HumanEquivalentUserTestingKnowledgeTests(unittest.TestCase):
    def test_adr_and_roadmap_preserve_two_release_gates(self):
        adr = (ROOT / "docs/decisions/ADR-0016-human-equivalent-user-release-testing.md").read_text(encoding="utf-8")
        roadmap = (ROOT / "docs/roadmap/ROADMAP.md").read_text(encoding="utf-8")
        charter = (ROOT / "docs/product/PRODUCT-CHARTER.md").read_text(encoding="utf-8")

        for term in ("Surface Reconciliation", "Full User E2E"):
            self.assertIn(term, adr)
            self.assertIn(term, roadmap)
            self.assertIn(term, charter)

        self.assertIn("ACTUAL_BROWSER_PROCESS_REQUIRED=YES", adr)
        self.assertIn("JSDOM_COMPONENT_TEST_SUBSTITUTE=NO", adr)
        self.assertIn("CONTRACT_CONFIGURED", adr)
        self.assertIn("EXECUTION_PASS", adr)
        self.assertIn("same exact release candidate", adr.lower())
        self.assertIn("Engineering System remains the canonical methodology authority", adr)
        self.assertNotIn("For every user-facing product, release qualification uses", adr)

        project = PROJECT.read_text(encoding="utf-8")
        self.assertIn("user_facing: false", project)
        self.assertIn("primary_user_surface: none", project)

        migration = (ROOT / "docs/migration/athena-capability-inventory.json").read_text(encoding="utf-8")
        self.assertIn("Atlas human UI is a later phase", migration)
        self.assertIn("before any user-facing Atlas production release", roadmap)


if __name__ == "__main__":
    unittest.main()
