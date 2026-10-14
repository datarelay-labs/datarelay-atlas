"""Product acceptance mapping regressions; not live user-gate evidence."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]

class McpPrimaryContractTests(unittest.TestCase):
    def setUp(self):
        self.project = yaml.safe_load((ROOT / '.engineering/project.yaml').read_text())
        self.release = yaml.safe_load((ROOT / '.engineering/release.yaml').read_text())
        self.surface = (ROOT / 'docs/contracts/ATLAS_SURFACE_RECONCILIATION.md').read_text()
        self.e2e = (ROOT / 'docs/contracts/ATLAS_FULL_USER_E2E.md').read_text()

    def test_mixed_primary_surface_keeps_real_browser_and_both_mandatory_gates(self):
        self.assertTrue(self.project['project']['user_facing'])
        self.assertEqual(self.project['project']['primary_user_surface'], 'mixed')
        gates = self.release['human_equivalent_user_tests']
        self.assertEqual(gates['primary_user_surface'], 'mixed')
        for key in ('actual_user_surface_required', 'actual_browser_process_required',
                    'same_candidate_required', 'same_head_quality_closure_required'):
            self.assertIs(gates[key], True)
        for name in ('surface_reconciliation', 'full_user_e2e'):
            self.assertIs(gates[name]['mandatory'], True)
            self.assertGreaterEqual(gates[name]['minimum_passes'], 1)

    def test_actual_mcp_user_workflow_is_required_in_both_contracts(self):
        for text in (self.surface, self.e2e):
            for name in ('list_projects', 'bootstrap_datarelay_context', 'get_task_context',
                         'search_project', 'get_provenance', 'get_source_detail',
                         'search_knowledge', 'personal_search', 'get_operations_readiness'):
                self.assertIn(name, text)
            self.assertIn('CHATGPT_CHAT', text)
            self.assertIn('AI_DIRECT', text)

    def test_security_and_negative_ui_coverage_not_removed(self):
        for text in (self.surface, self.e2e):
            for token in ('Chromium/Chrome', 'POST/write', 'UNKNOWN', 'cleanup'):
                self.assertIn(token, text)
            self.assertIn('FAIL/PARTIAL/BLOCKED', text)
        self.assertIn('Unauthorized/wrong-scope', self.e2e)
        self.assertIn('Browser failure still blocks', self.e2e)
        self.assertIn('persistent data unchanged', self.e2e)

    def test_real_use_is_distinct_from_http_or_machine_evidence(self):
        self.assertIn('actual next repository action', self.e2e)
        self.assertIn('native connected', self.e2e)
        self.assertIn('not automatically incremented', self.e2e)
        self.assertIn('not an application tool execution', self.surface)
        self.assertIn('not live user-gate evidence', __doc__)

    def test_optional_human_mode_does_not_switch_runtime(self):
        self.assertEqual(self.release['human_equivalent_user_tests']['executor'], 'EXECUTION_PROFILE')
        self.assertIn('Human-owner mode remains an explicit optional run choice', self.e2e)
        self.assertIn('HUMAN_OWNER_WITH_CHATGPT_AUDIT', self.surface)
        profile = yaml.safe_load((ROOT / '.engineering/execution-profile.yaml').read_text())
        self.assertEqual(profile['runtime']['primary'], 'CHATGPT_CHAT')

if __name__ == '__main__':
    unittest.main()
