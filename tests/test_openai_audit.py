"""OpenAI Responses audit adapter contract regressions."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from atlas.provenance import ValidationError
from atlas.work_controller import (
    AuditResult,
    CompletionEvent,
    FixedAuditAdapter,
    OpenAIHttpTransport,
    OpenAIResponsesAuditAdapter,
    RecordingCursorDispatcher,
    RecordingWorkPacketAdapter,
    WorkController,
    WorkstreamRecord,
    build_openai_audit_request,
    drain_completion_inbox,
    enqueue_completion_event,
    extract_response_output_text,
    parse_audit_verdict_payload,
)


HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


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


class OpenAIAuditContractTests(unittest.TestCase):
    def _event(self) -> CompletionEvent:
        return CompletionEvent(
            event_id="evt-1",
            workstream="awc-poc",
            issue_number=12,
            branch="feature/x",
            head=HEAD,
            attempt=1,
        )

    def _record(self, tmp: str) -> WorkstreamRecord:
        return WorkstreamRecord(
            workstream="awc-poc",
            repository="datarelay-labs/datarelay-atlas",
            issue_number=12,
            branch="feature/x",
            worktree_path=tmp,
            expected_head=HEAD,
            state="AUDITING",
            attempt=0,
            max_attempts=3,
        )

    def test_build_request_has_background_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = build_openai_audit_request(self._event(), self._record(tmp))
            dump = json.dumps(body)
            self.assertTrue(body["background"])
            self.assertFalse(body["store"])
            self.assertNotIn("OPENAI", dump)
            self.assertNotIn("api_key", dump.lower())
            self.assertIn("/work-resume", dump)

    def test_parse_verdict_and_output_text_extraction(self):
        payload = {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"verdict":"REWORK","findings":"gap"}',
                        }
                    ]
                }
            ]
        }
        text = extract_response_output_text(payload)
        result = parse_audit_verdict_payload(text)
        self.assertEqual(result.verdict, "REWORK")
        self.assertEqual(result.findings, "gap")

    def test_background_poll_maps_pass(self):
        transport = FakeTransport(
            [
                {
                    "response": {
                        "id": "resp_1",
                        "status": "queued",
                    }
                },
                {
                    "response": {
                        "id": "resp_1",
                        "status": "in_progress",
                    }
                },
                {
                    "response": {
                        "id": "resp_1",
                        "status": "completed",
                        "output_text": '{"verdict":"PASS","findings":"ok"}',
                    }
                },
            ]
        )
        sleeps: list[float] = []
        adapter = OpenAIResponsesAuditAdapter(
            transport=transport,  # type: ignore[arg-type]
            poll_interval_sec=0.01,
            sleeper=sleeps.append,
        )
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENAI_API_KEY"] = "test-key-not-real"
            try:
                result = adapter.audit(self._event(), self._record(tmp))
            finally:
                del os.environ["OPENAI_API_KEY"]
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(adapter.last_response_id, "resp_1")
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["POST", "GET", "GET"],
        )
        self.assertTrue(all(call["api_key"] == "test-key-not-real" for call in transport.calls))
        # Key must not enter request body.
        self.assertNotIn("test-key-not-real", json.dumps(transport.calls[0]["body"]))

    def test_failed_terminal_maps_human_required(self):
        transport = FakeTransport(
            [
                {
                    "response": {
                        "id": "resp_2",
                        "status": "failed",
                        "error": {"message": "boom"},
                    }
                }
            ]
        )
        adapter = OpenAIResponsesAuditAdapter(
            transport=transport,  # type: ignore[arg-type]
            sleeper=lambda _s: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENAI_API_KEY"] = "test-key-not-real"
            try:
                result = adapter.audit(self._event(), self._record(tmp))
            finally:
                del os.environ["OPENAI_API_KEY"]
        self.assertEqual(result.verdict, "HUMAN_REQUIRED")
        self.assertIn("failed", result.findings)

    def test_create_retry_then_success(self):
        transport = FakeTransport(
            [
                {"error": "temporary network blip"},
                {
                    "response": {
                        "id": "resp_3",
                        "status": "completed",
                        "output_text": '{"verdict":"REWORK","findings":"retry path"}',
                    }
                },
            ]
        )
        adapter = OpenAIResponsesAuditAdapter(
            transport=transport,  # type: ignore[arg-type]
            create_retries=1,
            sleeper=lambda _s: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENAI_API_KEY"] = "test-key-not-real"
            try:
                result = adapter.audit(self._event(), self._record(tmp))
            finally:
                del os.environ["OPENAI_API_KEY"]
        self.assertEqual(result.verdict, "REWORK")
        self.assertEqual(len(transport.calls), 2)

    def test_missing_api_key_fail_closed(self):
        adapter = OpenAIResponsesAuditAdapter(
            transport=FakeTransport([]),  # type: ignore[arg-type]
        )
        os.environ.pop("OPENAI_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError):
                adapter.audit(self._event(), self._record(tmp))

    def test_http_transport_rejects_empty_key(self):
        transport = OpenAIHttpTransport()
        with self.assertRaises(ValidationError):
            transport.request_json("GET", "/responses/x", api_key="")


class CompletionInboxTests(unittest.TestCase):
    def test_enqueue_and_drain_pass_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            ctl = WorkController(
                data_root,
                audit=FixedAuditAdapter(AuditResult(verdict="PASS", findings="ok")),
                work_packet=RecordingWorkPacketAdapter(),
                dispatcher=RecordingCursorDispatcher(),
                enforce_worktree_identity=False,
            )
            ctl.register_workstream(
                workstream="awc-poc",
                repository="datarelay-labs/datarelay-atlas",
                issue_number=12,
                branch="feature/x",
                worktree_path=str(worktree),
                expected_head=HEAD,
            )
            event = {
                "event_id": "inbox-1",
                "workstream": "awc-poc",
                "issue_number": 12,
                "branch": "feature/x",
                "head": HEAD,
                "attempt": 1,
            }
            path = enqueue_completion_event(data_root, event)
            self.assertTrue(path.exists())
            outcomes = drain_completion_inbox(ctl, data_root)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["state"], "PASSED")
            self.assertFalse(path.exists())
            processed = list((data_root / "completion-processed").glob("*.json"))
            self.assertEqual(len(processed), 1)


if __name__ == "__main__":
    unittest.main()
