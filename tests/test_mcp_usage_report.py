import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "atlas_mcp_usage_report", ROOT / "scripts" / "atlas_mcp_usage_report.py"
)
assert SPEC and SPEC.loader
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)

def journal(message: str, timestamp: str = "1795027200000000") -> str:
    return json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": timestamp})

class AtlasMcpUsageReportTests(unittest.TestCase):
    def test_counts_receipts_without_double_counting_duplicate_errors(self):
        entries = [
            journal("INFO: ATLAS_MCP_TOOL_EXECUTION tool=bootstrap_datarelay_context outcome=SUCCESS"),
            journal("INFO: ATLAS_MCP_TOOL_EXECUTION tool=bootstrap_datarelay_context outcome=BLOCK"),
            journal("Tool 'bootstrap_datarelay_context' failed: 'workstream is invalid for user-secret'"),
            journal("INFO: ATLAS_MCP_TOOL_EXECUTION tool=search_project outcome=SUCCESS"),
            journal("This is an unrelated log line with an access credential"),
        ]
        result = usage.usage_report(entries)
        self.assertEqual(result["execution_receipt_count"], 3)
        self.assertEqual(result["success_rate"], 0.6667)
        self.assertEqual(result["tools"]["bootstrap_datarelay_context"], {"BLOCK": 1, "SUCCESS": 1})
        self.assertEqual(result["failure_message_count_not_independent_calls"], 1)
        self.assertEqual(result["sanitized_failure_reasons"], {"invalid_workstream": 1})
        self.assertEqual(result["task_productivity"], "NOT_MEASURED")
        self.assertNotIn("user-secret", json.dumps(result))
        self.assertNotIn("access credential", json.dumps(result))
        self.assertIn("2026-11", result["months_utc"])

    def test_empty_or_unrelated_logs_remain_unmeasured(self):
        result = usage.usage_report([journal("unrelated"), "not-json"])
        self.assertEqual(result["state"], "NO_DATA")
        self.assertEqual(result["execution_receipt_count"], 0)
        self.assertIsNone(result["success_rate"])

    def test_plain_logs_are_supported_and_have_no_fabricated_dates(self):
        result = usage.usage_report([
            "ATLAS_MCP_TOOL_EXECUTION tool=list_projects outcome=SUCCESS",
            "Tool 'get_task_context' failed: 'unknown repository: PRIVATE-PATH'",
        ])
        self.assertEqual(result["execution_receipt_count"], 1)
        self.assertEqual(result["months_utc"], {})
        self.assertEqual(result["sanitized_failure_reasons"], {"unknown_repository": 1})
        self.assertNotIn("PRIVATE-PATH", json.dumps(result))

    def test_nonstring_or_invalid_timestamp_is_safe(self):
        result = usage.usage_report([
            '{"MESSAGE": 42, "__REALTIME_TIMESTAMP": "bad"}',
            journal("ATLAS_MCP_TOOL_EXECUTION tool=list_projects outcome=SUCCESS", "999999999999999999999"),
        ])
        self.assertEqual(result["execution_receipt_count"], 1)
        self.assertEqual(result["months_utc"], {})

if __name__ == "__main__":
    unittest.main()
