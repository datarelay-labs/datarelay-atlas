from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch
from copy import deepcopy
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.concurrency_admission import (
    SNAPSHOT_FILENAME,
    plan_concurrency_admission,
)
from atlas.concurrency_effect import FILENAME as EFFECT_FILENAME
from atlas.concurrency_execution import (
    FILENAME,
    concurrency_execution_dashboard,
    start_concurrency_execution,
    validate_concurrency_execution_record,
)
from atlas.data_protection import backup_data_root
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def _snapshot() -> dict:
    return json.loads(
        (FIXTURES / "concurrency-admission-snapshot.example.json").read_text()
    )


def _prepare(root: Path) -> dict:
    snapshot = _snapshot()
    (root / SNAPSHOT_FILENAME).write_text(
        json.dumps(snapshot), encoding="utf-8"
    )
    plan = plan_concurrency_admission(snapshot)
    request = {
        "schema_version": 1,
        "kind": "concurrency_dispatch_authorization_request",
        "authorization_id": "surface-auth",
        "expected_plan_digest": plan["plan_digest"],
        "evaluated_at": "2026-09-30T12:30:00Z",
    }
    dashboard = start_concurrency_execution(
        root,
        cycle_id="surface-cycle",
        authorization_request=request,
        effect_id="surface-effect",
        effect_port=RecordingPort(),
    )
    return dashboard


class RecordingPort:
    def __init__(self):
        self.calls = []

    def dispatch(self, request):
        self.calls.append(deepcopy(request))
        index = len(self.calls)
        return {
            "result": "DISPATCHED",
            "dispatch_ref": f"surface-session-{index}",
        }


class ConcurrencyExecutionSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch(
            "atlas.concurrency_effect._trusted_effect_time",
            return_value=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_schema_fixture_and_runtime_validation(self):
        schema = json.loads(
            (CONTRACTS / "concurrency-execution-cycle.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "concurrency-execution-cycle.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(
            validate_concurrency_execution_record(fixture),
            fixture,
        )

    def test_cli_web_mcp_read_surfaces_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            _prepare(root)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main([
                        "--data-root",
                        str(root),
                        "concurrency",
                        "executions-show",
                    ]),
                    0,
                )
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["execution_count"], 1)
            self.assertEqual(
                cli["latest_execution"]["stage"],
                "AWAITING_JOIN",
            )
            self.assertEqual(cli["pass_authority"], "NONE")

            svc = AtlasService(root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                concurrency_execution_factory=svc.concurrency_execution_dashboard,
            )
            names = {
                item["name"]
                for item in tools.list_tools(default_read_scopes())
            }
            self.assertIn("get_concurrency_execution_cycles", names)
            result = tools.call(
                "get_concurrency_execution_cycles",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["execution_count"], 1)

            app = create_app(root)
            env = {}
            setup_testing_defaults(env)
            env.update({
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/concurrency",
                "QUERY_STRING": "",
                "HTTP_HOST": "127.0.0.1:8788",
            })
            state = {}

            def start(status, headers):
                state.update(status=status, headers=dict(headers))

            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Execution cycles", body)
            self.assertIn("ORCHESTRATION_EVIDENCE_ONLY", body)
            self.assertIn("PASS authority", body)

            backup_data_root(root, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())

    def test_tampered_cycle_or_effect_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _prepare(root)
            ledger = json.loads((root / FILENAME).read_text())
            ledger["records"][0]["effect_result"] = "PARTIAL"
            (root / FILENAME).write_text(
                json.dumps(ledger), encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                concurrency_execution_dashboard(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _prepare(root)
            effects = json.loads((root / EFFECT_FILENAME).read_text())
            effects["effects"][0]["receipt"]["receipts"][0][
                "worker_id"
            ] = "tampered-worker"
            (root / EFFECT_FILENAME).write_text(
                json.dumps(effects), encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                concurrency_execution_dashboard(root)


if __name__ == "__main__":
    unittest.main()
