"""Slice B bounded Responses audit provider regressions."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any

from unittest.mock import patch

from atlas.chat_audit import AuditControlPacket
from atlas.codex_audit import incomplete_evidence_verdict
from atlas.final_audit import (
    FINAL_AUDIT_DEVELOPER_PROMPT,
    FINAL_AUDIT_MODEL,
    MAX_AUDIT_OUTPUT_TOKENS,
    AuditBudget,
    BoundedResponsesAuditProvider,
    build_final_audit_request,
    checkpoint_evidence_excerpt,
    estimate_audit_cost_usd,
    request_cost_ceiling_usd,
    run_bounded_final_audit,
)
from atlas.provenance import ValidationError
from atlas.work_controller import CompletionEvent, WorkstreamRecord


HEAD = "a" * 40
OTHER = "b" * 40
SECRET = "sk-testsecretvalue1234567890"


class FakeTransport:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        body: dict | None = None,
    ) -> dict:
        self.calls.append(
            {"method": method, "path": path, "api_key": api_key, "body": body}
        )
        if not self.script:
            raise ValidationError("fake transport script exhausted")
        step = self.script.pop(0)
        if "error" in step:
            raise ValidationError(step["error"])
        return dict(step["response"])


def _bundle(**overrides: object) -> dict:
    bundle: dict[str, object] = {
        "schema": "awc.codex_evidence_bundle.v1",
        "identity": {
            "repository": "datarelay-labs/datarelay-atlas",
            "branch": "feature/autonomous-local-supervisor-gpt56-audit",
            "head": HEAD,
            "toplevel": "wt",
        },
        "git": {
            "evidence_status": "OK",
            "base": OTHER,
            "head": HEAD,
            "diff": "diff --git a/atlas/final_audit.py b/atlas/final_audit.py\n+provider\n",
            "changed_files": ["atlas/final_audit.py"],
        },
        "work_packet": {
            "status": "OK",
            "body": "Acceptance: Slice B returns a structured disposition.",
        },
        "tests": {"status": "PASS", "detail": "tests.test_final_audit OK"},
        "ci": {"status": "OK", "detail": "affected-tests=SUCCESS head=" + HEAD},
        "pr_reviews": {"status": "OK", "body": "optional first-pass review: none"},
        "prior_checkpoint": "status=IDLE",
    }
    bundle.update(overrides)
    return bundle


def _event(*, head: str = HEAD) -> CompletionEvent:
    return CompletionEvent(
        event_id="evt-47",
        workstream="issue-47",
        issue_number=47,
        branch="feature/autonomous-local-supervisor-gpt56-audit",
        head=head,
        attempt=1,
    )


def _record(worktree: str) -> WorkstreamRecord:
    return WorkstreamRecord(
        workstream="issue-47",
        repository="datarelay-labs/datarelay-atlas",
        issue_number=47,
        branch="feature/autonomous-local-supervisor-gpt56-audit",
        worktree_path=worktree,
        expected_head=HEAD,
    )


def _completed(verdict: str, *, sha: str = HEAD, usage: dict | None = None) -> dict:
    body = {"verdict": verdict, "findings": "bounded finding", "target_sha": sha}
    payload = {
        "id": "resp_b",
        "status": "completed",
        "output_text": json.dumps(body),
        "usage": usage
        or {
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 250, "cache_write_tokens": 10},
        },
    }
    return {"response": payload}


class FinalAuditSliceBTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key-not-real"

    def tearDown(self) -> None:
        if self._prior_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._prior_key

    def _provider(self, script: list[dict], **kwargs: object) -> tuple:
        transport = FakeTransport(script)
        kwargs.setdefault("require_identity", False)
        provider = BoundedResponsesAuditProvider(
            transport=transport,  # type: ignore[arg-type]
            sleeper=lambda _s: None,
            **kwargs,  # type: ignore[arg-type]
        )
        return provider, transport

    def test_request_schema_is_strict_and_delta_first(self) -> None:
        first = build_final_audit_request(_bundle())
        changed = _bundle()
        changed["work_packet"] = {
            "status": "OK",
            "body": "Acceptance: A different acceptance line.",
        }
        second = build_final_audit_request(changed)
        self.assertEqual(first["model"], FINAL_AUDIT_MODEL)
        self.assertEqual(first["input"][0]["content"], FINAL_AUDIT_DEVELOPER_PROMPT)
        self.assertEqual(first["input"][0]["content"], second["input"][0]["content"])
        self.assertNotIn(HEAD, first["input"][0]["content"])
        suffix = json.loads(first["input"][1]["content"])
        self.assertEqual(suffix["schema"], "awc.codex_evidence_bundle.v1")
        self.assertEqual(suffix["identity"]["head"], HEAD)
        self.assertEqual(suffix["git"]["base"], OTHER)
        self.assertIn("Acceptance:", suffix["work_packet"]["body"])
        self.assertIn("diff", suffix["git"])
        self.assertEqual(suffix["tests"]["status"], "PASS")
        self.assertEqual(suffix["ci"]["status"], "OK")
        self.assertIn("prior_checkpoint", suffix)
        self.assertNotIn("repository_dump", suffix)
        schema = first["text"]["format"]
        self.assertEqual(schema["type"], "json_schema")
        self.assertTrue(schema["strict"])
        self.assertEqual(first["max_output_tokens"], MAX_AUDIT_OUTPUT_TOKENS)
        self.assertEqual(
            schema["schema"]["properties"]["verdict"]["enum"],
            ["PASS", "REWORK", "HUMAN_REQUIRED"],
        )
        self.assertNotIn("test-key-not-real", json.dumps(first))

    def test_model_is_configurable(self) -> None:
        body = build_final_audit_request(_bundle(), model="gpt-5.6-sol")
        self.assertEqual(body["model"], "gpt-5.6-sol")

    def test_evidence_bounds_and_stale_head_refuse_before_call(self) -> None:
        provider, transport = self._provider([_completed("PASS")])
        stale_bundle = _bundle()
        stale_bundle["identity"] = dict(stale_bundle["identity"])
        stale_bundle["identity"]["head"] = OTHER
        with self.assertRaises(ValidationError) as stale:
            provider.audit_bundle(stale_bundle, event=_event())
        self.assertIn("stale HEAD", str(stale.exception))
        oversized = _bundle()
        oversized["git"] = dict(oversized["git"])
        oversized["git"]["diff"] = "x" * 12001
        with self.assertRaises(ValidationError):
            build_final_audit_request(oversized)
        bounded = provider.audit_bundle(oversized)
        self.assertEqual(bounded.verdict, "HUMAN_REQUIRED")
        self.assertIn("exceeds collector limit", bounded.findings)
        packet = _bundle()
        packet["work_packet"] = {"status": "OK", "body": "y" * 8001}
        with self.assertRaises(ValidationError):
            build_final_audit_request(packet)
        self.assertEqual(transport.calls, [])

    def test_malformed_and_refusal_are_human_required(self) -> None:
        malformed, transport = self._provider(
            [
                {
                    "response": {
                        "id": "resp_bad",
                        "status": "completed",
                        "output_text": "not-json",
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    }
                }
            ]
        )
        result = malformed.audit_bundle(_bundle())
        self.assertEqual(result.verdict, "HUMAN_REQUIRED")
        self.assertIn("malformed", result.findings)
        refusal, _transport = self._provider(
            [
                {
                    "response": {
                        "id": "resp_refuse",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "refusal", "refusal": "no"}],
                            }
                        ],
                        "usage": {"input_tokens": 4, "output_tokens": 1},
                    }
                }
            ]
        )
        refused = refusal.audit_bundle(_bundle())
        self.assertEqual(refused.verdict, "HUMAN_REQUIRED")
        self.assertIn("refusal", refused.findings)
        self.assertEqual(len(transport.calls), 1)

    def test_usage_cost_and_cached_tokens(self) -> None:
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000,
            "input_tokens_details": {
                "cached_tokens": 250_000,
                "cache_write_tokens": 100,
            },
        }
        provider, _transport = self._provider(
            [_completed("REWORK", usage=usage)],
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=50.0,
                preflight_usd=0.05,
            ),
        )
        result = provider.audit_bundle(_bundle())
        self.assertEqual(result.verdict, "REWORK")
        telemetry = provider.last_telemetry
        assert telemetry is not None
        self.assertEqual(telemetry.cached_tokens, 250_000)
        self.assertEqual(telemetry.cache_write_tokens, 100)
        expected = estimate_audit_cost_usd(
            input_tokens=1_000_000,
            output_tokens=1_000,
            cached_tokens=250_000,
            cache_write_tokens=100,
        )
        self.assertAlmostEqual(telemetry.estimated_cost_usd, expected)
        self.assertAlmostEqual(expected, 2.9996 + 0.1 + 0.0005 + 0.02)
        record = json.dumps(telemetry.public_record())
        self.assertNotIn("test-key-not-real", record)
        self.assertNotIn(SECRET, record)
        self.assertNotIn("diff --git", record)

    def test_budget_blocks_before_call_and_after_usage(self) -> None:
        blocked, transport = self._provider(
            [_completed("PASS")],
            budget=AuditBudget(
                per_run_hard_usd=0.5,
                monthly_hard_usd=10,
                preflight_usd=1.0,
            ),
        )
        result = blocked.audit_bundle(_bundle())
        self.assertEqual(result.verdict, "HUMAN_REQUIRED")
        self.assertIn("per-run budget exceeded", result.findings)
        self.assertEqual(transport.calls, [])

        ceiling = request_cost_ceiling_usd(build_final_audit_request(_bundle()))
        after, after_transport = self._provider(
            [
                _completed(
                    "PASS",
                    usage={"input_tokens": 0, "output_tokens": 100_000},
                )
            ],
            budget=AuditBudget(
                per_run_hard_usd=10.0,
                monthly_hard_usd=ceiling,
                preflight_usd=0.0,
            ),
        )
        reconciled = after.audit_bundle(_bundle())
        self.assertEqual(len(after_transport.calls), 1)
        self.assertEqual(reconciled.verdict, "HUMAN_REQUIRED")
        self.assertIn("monthly budget exceeded after usage", reconciled.findings)

    def test_missing_key_is_human_required_without_call(self) -> None:
        os.environ.pop("OPENAI_API_KEY", None)
        provider, transport = self._provider([_completed("PASS")])
        result = provider.audit_bundle(_bundle())
        self.assertEqual(result.verdict, "HUMAN_REQUIRED")
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn("Gate B", result.findings)
        self.assertEqual(transport.calls, [])

    def test_idle_makes_zero_api_calls(self) -> None:
        provider, transport = self._provider([_completed("PASS")])
        outcome = run_bounded_final_audit(
            actionable=False,
            evidence=_bundle(),
            provider=provider,
        )
        self.assertEqual(outcome["action"], "idle_noop")
        self.assertEqual(outcome["api_calls"], 0)
        self.assertEqual(transport.calls, [])

    def test_secrets_are_redacted_from_request_and_findings(self) -> None:
        leak = f"OPENAI_API_KEY={SECRET}"
        leaked = _bundle()
        leaked["git"] = dict(leaked["git"])
        leaked["git"]["diff"] = leak + "\n+ok\n"
        body = build_final_audit_request(leaked)
        self.assertNotIn(SECRET, json.dumps(body))
        provider, _transport = self._provider(
            [
                {
                    "response": {
                        "id": "resp_leak",
                        "status": "completed",
                        "output_text": json.dumps(
                            {
                                "verdict": "REWORK",
                                "findings": leak,
                                "target_sha": HEAD,
                            }
                        ),
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    }
                }
            ]
        )
        result = provider.audit_bundle(_bundle())
        self.assertNotIn(SECRET, result.findings)
        assert provider.last_telemetry is not None
        self.assertNotIn(SECRET, json.dumps(provider.last_telemetry.public_record()))

    def test_prior_checkpoint_excerpt_is_bounded_metadata(self) -> None:
        packet = AuditControlPacket(
            target_repository="datarelay-labs/datarelay-atlas",
            target_branch="feature/autonomous-local-supervisor-gpt56-audit",
            current_target_sha=HEAD,
            audit_status="IDLE",
            next_action="run_next_audit_slice",
        )
        excerpt = checkpoint_evidence_excerpt(packet)
        self.assertIn(HEAD, excerpt)
        self.assertIn("IDLE", excerpt)
        self.assertNotIn(SECRET, excerpt)
        self.assertLessEqual(len(excerpt), 4000)

    def test_incomplete_evidence_and_deterministic_gate_make_no_call(self) -> None:
        incomplete = _bundle()
        incomplete["git"] = dict(incomplete["git"])
        incomplete["git"]["evidence_status"] = "INCOMPLETE"
        incomplete["git"]["detail"] = "diff unavailable"
        provider, transport = self._provider([_completed("PASS")])
        result = provider.audit_bundle(incomplete)
        self.assertEqual(result.verdict, "HUMAN_REQUIRED")
        self.assertIn("git evidence status=INCOMPLETE", result.findings)
        packet = _bundle()
        packet["work_packet"] = {"status": "ERROR", "detail": "packet fetch failed"}
        packet_result = provider.audit_bundle(packet)
        self.assertEqual(packet_result.verdict, "HUMAN_REQUIRED")
        self.assertIn("Work Packet evidence status=ERROR", packet_result.findings)
        failing = _bundle()
        failing["tests"] = {"status": "FAIL", "detail": "assertion failed"}
        rework = provider.audit_bundle(failing)
        self.assertEqual(rework.verdict, "REWORK")
        self.assertIn("tests FAIL", rework.findings)
        self.assertEqual(transport.calls, [])

    def test_dirty_snapshot_revalidation_blocks_before_call(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as worktree:
            def git_runner(argv: list[str], cwd: str) -> str:
                if argv[1:] == ["rev-parse", "--show-toplevel"]:
                    return cwd
                if argv[1:] == ["remote", "get-url", "origin"]:
                    return "https://github.com/datarelay-labs/datarelay-atlas.git"
                if argv[1:] == ["branch", "--show-current"]:
                    return "feature/autonomous-local-supervisor-gpt56-audit"
                if argv[1:] == ["rev-parse", "HEAD"]:
                    return HEAD
                if argv[1:] == ["status", "--porcelain", "--untracked-files=all"]:
                    return " M atlas/final_audit.py"
                raise ValidationError(f"unexpected git argv {argv}")

            provider, transport = self._provider(
                [_completed("PASS")],
                require_identity=True,
                git_runner=git_runner,
            )
            result = provider.audit_bundle(
                _bundle(),
                event=_event(),
                record=_record(worktree),
            )
        self.assertEqual(result.verdict, "HUMAN_REQUIRED")
        self.assertIn("clean autonomous", result.findings)
        self.assertEqual(transport.calls, [])

    def test_audit_consumes_collected_bundle(self) -> None:
        provider, transport = self._provider([_completed("PASS")])
        with patch(
            "atlas.final_audit.collect_audit_evidence_bundle",
            return_value=_bundle(),
        ) as collected:
            result = provider.audit(_event(), _record("/tmp/unused-worktree"))
        collected.assert_called_once()
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(provider.last_evidence_bundle["schema"], "awc.codex_evidence_bundle.v1")
        self.assertEqual(len(transport.calls), 1)

    def test_incomplete_findings_redact_credential_material(self) -> None:
        secret = "OPENAI_API_KEY=sk-fake-secret-1234567890"
        cases = (
            ("work_packet", "ERROR"),
            ("pr_reviews", "INCOMPLETE"),
        )
        for section, status in cases:
            bundle = _bundle()
            bundle[section] = {"status": status, "detail": secret}
            result = incomplete_evidence_verdict(bundle)
            assert result is not None
            self.assertEqual(result.verdict, "HUMAN_REQUIRED")
            self.assertNotIn(secret, result.findings)
            self.assertNotIn("sk-fake-secret", result.findings)
        git_bundle = _bundle()
        git_bundle["git"] = dict(git_bundle["git"])
        git_bundle["git"]["evidence_status"] = "ERROR"
        git_bundle["git"]["detail"] = secret
        git_result = incomplete_evidence_verdict(git_bundle)
        assert git_result is not None
        self.assertNotIn("sk-fake-secret", git_result.findings)
        provider, transport = self._provider([_completed("PASS")])
        closed = provider.audit_bundle(git_bundle)
        self.assertEqual(closed.verdict, "HUMAN_REQUIRED")
        self.assertNotIn("sk-fake-secret", closed.findings)
        self.assertEqual(transport.calls, [])

    def test_request_ceiling_blocks_before_paid_call(self) -> None:
        ceiling = request_cost_ceiling_usd(build_final_audit_request(_bundle()))
        self.assertGreater(ceiling, 0.05)
        per_run_budget = AuditBudget(
            per_run_hard_usd=0.05,
            monthly_hard_usd=25.0,
            preflight_usd=0.04,
        )
        self.assertIsNone(per_run_budget.blocked_before_call())
        per_run, transport = self._provider(
            [_completed("PASS")],
            budget=per_run_budget,
        )
        blocked = per_run.audit_bundle(_bundle())
        self.assertEqual(blocked.verdict, "HUMAN_REQUIRED")
        self.assertIn("per-run budget exceeded", blocked.findings)
        self.assertEqual(transport.calls, [])
        self.assertIsNone(per_run.last_request_body)

        monthly_budget = AuditBudget(
            per_run_hard_usd=25.0,
            monthly_hard_usd=0.05,
            preflight_usd=0.01,
        )
        self.assertIsNone(monthly_budget.blocked_before_call())
        monthly, monthly_transport = self._provider(
            [_completed("PASS")],
            budget=monthly_budget,
        )
        monthly_blocked = monthly.audit_bundle(_bundle())
        self.assertEqual(monthly_blocked.verdict, "HUMAN_REQUIRED")
        self.assertIn("monthly budget exceeded", monthly_blocked.findings)
        self.assertEqual(monthly_transport.calls, [])

    def test_default_constructor_revalidates_identity_before_call(self) -> None:
        transport = FakeTransport([_completed("PASS")])
        provider = BoundedResponsesAuditProvider(
            transport=transport,  # type: ignore[arg-type]
            sleeper=lambda _s: None,
            evidence_bundle_override=_bundle(),
        )
        self.assertTrue(provider.require_identity)
        with self.assertRaises(ValidationError):
            provider.audit(_event(), _record("/tmp/missing-final-audit-worktree"))
        self.assertEqual(transport.calls, [])
        closed = provider.audit_bundle(
            _bundle(),
            event=_event(),
            record=_record("/tmp/missing-final-audit-worktree"),
        )
        self.assertEqual(closed.verdict, "HUMAN_REQUIRED")
        self.assertNotEqual(closed.verdict, "PASS")
        self.assertIn("clean autonomous", closed.findings)
        self.assertEqual(transport.calls, [])
        self.assertIsNone(provider.last_request_body)


if __name__ == "__main__":
    unittest.main()
