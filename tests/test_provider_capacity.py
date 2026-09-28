"""Provider-neutral capacity input contract regressions."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from atlas.cli import main
from atlas.cursor_usage import UsageEvent, cursor_capacity_input
from atlas.provider_capacity import (
    build_provider_capacity_input,
    validate_provider_capacity_input,
)
from atlas.provenance import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/provider-capacity-input.schema.json"
FIXTURE = ROOT / "docs/contracts/fixtures/provider-capacity-input.example.json"
CSV_HEADER = (
    "Date,Input (w/ Cache Write),Input (w/o Cache Write),"
    "Cache Read,Output Tokens,Total Tokens"
)


def _write_csv(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".csv", delete=False
    )
    handle.write(text)
    handle.close()
    return Path(handle.name)


def _event(timestamp: str, total_tokens: int) -> UsageEvent:
    return UsageEvent(
        timestamp=timestamp,
        kind=None,
        model="composer-2.5",
        max_mode=None,
        input_tokens=total_tokens,
        cache_write_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        total_tokens=total_tokens,
        cost=None,
        cost_to_you=None,
        cloud_agent_id=None,
        automation_id=None,
    )


class ProviderCapacityContractTests(unittest.TestCase):
    def test_fixture_matches_machine_schema_and_runtime_validator(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(validate_provider_capacity_input(fixture), fixture)
    def test_builder_keeps_unavailable_capacity_signals_unknown(self):
        payload = build_provider_capacity_input(
            provider="cursor",
            source_kind="usage_events_csv",
            window_start="2026-09-22T00:00:00.000000Z",
            window_end="2026-09-22T00:10:00.000000Z",
            event_count=2,
            total_tokens=300,
        )
        self.assertEqual(payload["scope"], "ACCOUNT_AGGREGATE")
        self.assertEqual(
            set(payload["signals"]),
            {
                "remaining_capacity",
                "reset_at",
                "capacity_pool",
                "active_inference_wip",
            },
        )
        for signal in payload["signals"].values():
            self.assertEqual(signal, {"status": "UNKNOWN"})

    def test_unknown_signal_cannot_carry_invented_value(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["signals"]["remaining_capacity"] = {
            "status": "UNKNOWN",
            "value": "50",
        }
        with self.assertRaises(ValidationError):
            validate_provider_capacity_input(payload)
    def test_scope_and_observation_window_fail_closed(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

        wrong_scope = deepcopy(payload)
        wrong_scope["scope"] = "SESSION"
        with self.assertRaises(ValidationError):
            validate_provider_capacity_input(wrong_scope)

        reversed_window = deepcopy(payload)
        reversed_window["evidence"]["window_start"] = "2026-09-22T01:00:00Z"
        reversed_window["evidence"]["window_end"] = "2026-09-22T00:00:00Z"
        with self.assertRaises(ValidationError):
            validate_provider_capacity_input(reversed_window)

        impossible_empty = deepcopy(payload)
        impossible_empty["evidence"].update(
            event_count=0,
            total_tokens=1,
            window_start=None,
            window_end=None,
        )
        with self.assertRaises(ValidationError):
            validate_provider_capacity_input(impossible_empty)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        with self.assertRaises(JsonSchemaValidationError):
            Draft202012Validator(schema).validate(impossible_empty)

    def test_cursor_adapter_uses_exact_aggregate_without_capacity_inference(self):
        payload = cursor_capacity_input(
            [
                _event("2026-09-22T00:10:00.000000Z", 35),
                _event("2026-09-22T00:00:00.000000Z", 25),
            ]
        )
        self.assertEqual(payload["provider"], "cursor")
        self.assertEqual(payload["scope"], "ACCOUNT_AGGREGATE")
        self.assertEqual(payload["evidence"]["source_kind"], "usage_events_csv")
        self.assertEqual(payload["evidence"]["event_count"], 2)
        self.assertEqual(payload["evidence"]["total_tokens"], 60)
        self.assertEqual(
            payload["evidence"]["window_start"],
            "2026-09-22T00:00:00.000000Z",
        )
        self.assertEqual(
            payload["evidence"]["window_end"],
            "2026-09-22T00:10:00.000000Z",
        )
        for signal in payload["signals"].values():
            self.assertEqual(signal, {"status": "UNKNOWN"})
        self.assertNotIn("recommendation", payload)
        self.assertNotIn("route", payload)

    def test_cursor_adapter_empty_usage_has_no_claimed_window(self):
        payload = cursor_capacity_input([])
        self.assertEqual(payload["evidence"]["event_count"], 0)
        self.assertEqual(payload["evidence"]["total_tokens"], 0)
        self.assertIsNone(payload["evidence"]["window_start"])
        self.assertIsNone(payload["evidence"]["window_end"])

    def test_capacity_input_cli_uses_existing_strict_cursor_csv_parser(self):
        csv_path = _write_csv(
            "\n".join(
                [
                    CSV_HEADER,
                    "2026-09-22T00:10:00+00:00,0,10,20,5,35",
                    "2026-09-22T00:00:00Z,5,5,10,5,25",
                ]
            )
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(["usage", "capacity-input", "--csv", str(csv_path)])
        self.assertEqual(rc, 0, stderr.getvalue())

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["kind"], "provider_capacity_input")
        self.assertEqual(payload["provider"], "cursor")
        self.assertEqual(payload["scope"], "ACCOUNT_AGGREGATE")
        self.assertEqual(payload["evidence"]["event_count"], 2)
        self.assertEqual(payload["evidence"]["total_tokens"], 60)
        self.assertEqual(
            payload["evidence"]["window_start"],
            "2026-09-22T00:00:00.000000Z",
        )
        self.assertEqual(
            payload["evidence"]["window_end"],
            "2026-09-22T00:10:00.000000Z",
        )
        for signal in payload["signals"].values():
            self.assertEqual(signal, {"status": "UNKNOWN"})


if __name__ == "__main__":
    unittest.main()
