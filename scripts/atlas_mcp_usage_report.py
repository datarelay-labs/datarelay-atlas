#!/usr/bin/env python3
"""Read-only, privacy-minimal Atlas MCP invocation accounting.

Input: systemd journal JSONL or plaintext. No raw request/error content emitted.
Reports tool usage, never task productivity or memory usefulness.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import re
import sys

EVENT = re.compile(
    r"\bATLAS_MCP_TOOL_EXECUTION\s+tool=([a-z][a-z0-9_]*)\s+"
    r"outcome=(SUCCESS|BLOCK|ERROR|FAILURE|FAILED)\b"
)
FAILED = re.compile(r"\bTool '([a-z][a-z0-9_]*)' failed:")
REASONS = (
    ("workstream is invalid", "invalid_workstream"),
    ("ambiguous repository", "ambiguous_repository"),
    ("unknown repository", "unknown_repository"),
    ("exactly one of project_id or repository", "scope_argument_error"),
    ("unknown project_id", "unknown_project"),
    ("unknown source_id", "unknown_source"),
)

def _record(line: str) -> tuple[str, str | None]:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return line, None
    if not isinstance(value, dict) or not isinstance(value.get("MESSAGE"), str):
        return "", None
    day = None
    raw_time = value.get("__REALTIME_TIMESTAMP")
    if isinstance(raw_time, str) and raw_time.isdecimal():
        try:
            day = datetime.fromtimestamp(
                int(raw_time) / 1_000_000, tz=timezone.utc
            ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return value["MESSAGE"], day

def usage_report(lines: object) -> dict[str, object]:
    """Count actual execution receipts; duplicated failure log lines are separate."""
    results = Counter()
    by_tool = Counter()
    by_month = Counter()
    error_kinds = Counter()
    failure_log_count = 0
    for line in lines:
        message, day = _record(line)
        match = EVENT.search(message)
        if match:
            tool, outcome = match.groups()
            results[outcome] += 1
            by_tool[(tool, outcome)] += 1
            if day:
                by_month[(day[:7], outcome)] += 1
        elif FAILED.search(message):
            failure_log_count += 1
            lowered = message.lower()
            kind = next((label for term, label in REASONS if term in lowered), "other")
            error_kinds[kind] += 1
    total = sum(results.values())
    return {
        "state": "OBSERVED" if total else "NO_DATA",
        "authority": "OBSERVABILITY_ONLY",
        "execution_receipt_count": total,
        "success_rate": round(results["SUCCESS"] / total, 4) if total else None,
        "outcomes": dict(sorted(results.items())),
        "tools": {
            tool: dict(sorted((status, count)
                              for (name, status), count in by_tool.items()
                              if name == tool))
            for tool in sorted({name for name, _ in by_tool})
        },
        "months_utc": {
            month: dict(sorted((status, count)
                               for (candidate, status), count in by_month.items()
                               if candidate == month))
            for month in sorted({month for month, _ in by_month})
        },
        "failure_message_count_not_independent_calls": failure_log_count,
        "sanitized_failure_reasons": dict(sorted(error_kinds.items())),
        "task_productivity": "NOT_MEASURED",
        "note": "Test and production calls are not distinguishable from these receipts. Saved time, token reduction and task outcomes are not inferred.",
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Atlas MCP journal receipts without payloads")
    parser.add_argument("journal", nargs="?", default="-", help="Journal JSONL file (default: stdin)")
    args = parser.parse_args()
    if args.journal == "-":
        result = usage_report(sys.stdin)
    else:
        with open(args.journal, "r", encoding="utf-8") as handle:
            result = usage_report(handle)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
