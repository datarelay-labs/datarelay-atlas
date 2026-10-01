from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.concurrency_admission import (
    SNAPSHOT_FILENAME,
    plan_concurrency_admission,
)
from atlas.concurrency_authorization import (
    FILENAME,
    build_concurrency_dispatch_authorization,
    concurrency_dispatch_authorization_dashboard,
    publish_concurrency_dispatch_authorization,
    validate_concurrency_authorization_request,
    validate_concurrency_dispatch_authorization,
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


def _request(snapshot: dict, *, evaluated_at: str = "2026-09-30T12:30:00Z") -> dict:
    plan = plan_concurrency_admission(snapshot)
    return {
        "schema_version": 1,
        "kind": "concurrency_dispatch_authorization_request",
        "authorization_id": "multi-node-authorization-1",
        "expected_plan_digest": plan["plan_digest"],
        "evaluated_at": evaluated_at,
    }


def _write_snapshot(root: Path, snapshot: dict) -> None:
    (root / SNAPSHOT_FILENAME).write_text(json.dumps(snapshot), encoding="utf-8")


class ConcurrencyAuthorizationTests(unittest.TestCase):
    def test_public_schema_fixtures_and_runtime_parity(self):
        request_schema = json.loads(
            (CONTRACTS / "concurrency-dispatch-authorization-request.schema.json").read_text()
        )
        authorization_schema = json.loads(
            (CONTRACTS / "concurrency-dispatch-authorization.schema.json").read_text()
        )
        request_fixture = json.loads(
            (FIXTURES / "concurrency-dispatch-authorization-request.example.json").read_text()
        )
        authorization_fixture = json.loads(
            (FIXTURES / "concurrency-dispatch-authorization.example.json").read_text()
        )
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(authorization_schema)
        Draft202012Validator(request_schema).validate(request_fixture)
        Draft202012Validator(authorization_schema).validate(authorization_fixture)
        self.assertEqual(
            validate_concurrency_authorization_request(request_fixture),
            request_fixture,
        )
        self.assertEqual(
            validate_concurrency_dispatch_authorization(authorization_fixture),
            authorization_fixture,
        )

    def test_two_independent_assignments_authorize_without_dispatch_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _snapshot()
            _write_snapshot(root, snapshot)
            request = _request(snapshot)
            authorization = build_concurrency_dispatch_authorization(root, request)
            self.assertEqual(authorization["authority"], "DISPATCH_AUTHORIZATION_ONLY")
            self.assertEqual(authorization["dispatch_effect_authority"], "NONE")
            self.assertEqual(authorization["graph_state"], "READY")
            self.assertEqual(authorization["assignment_count"], 2)
            self.assertEqual(
                {item["node_id"] for item in authorization["assignments"]},
                {
                    "datarelay-labs/datarelay-atlas#201",
                    "datarelay-labs/datarelay-atlas#202",
                },
            )
            self.assertEqual(
                len({item["slot_id"] for item in authorization["assignments"]}),
                2,
            )

    def test_wrong_digest_stale_evidence_single_assignment_and_conflict_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _snapshot()
            _write_snapshot(root, snapshot)

            wrong = _request(snapshot)
            wrong["expected_plan_digest"] = "0" * 64
            with self.assertRaisesRegex(ValidationError, "plan digest mismatch"):
                build_concurrency_dispatch_authorization(root, wrong)

            stale = _request(snapshot, evaluated_at="2026-09-30T14:30:01Z")
            with self.assertRaisesRegex(ValidationError, "stale"):
                build_concurrency_dispatch_authorization(root, stale)

            single = deepcopy(snapshot)
            single["policy"]["max_parallel_admission"] = 1
            _write_snapshot(root, single)
            with self.assertRaises(ValidationError):
                build_concurrency_dispatch_authorization(root, _request(single))

            conflict = deepcopy(snapshot)
            conflict["graph"]["nodes"][1]["resources"] = ["component:retrieval"]
            _write_snapshot(root, conflict)
            with self.assertRaises(ValidationError):
                build_concurrency_dispatch_authorization(root, _request(conflict))

    def test_published_authorization_becomes_stale_after_plan_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _snapshot()
            _write_snapshot(root, snapshot)
            dashboard = publish_concurrency_dispatch_authorization(
                root,
                _request(snapshot),
            )
            self.assertEqual(dashboard["binding_state"], "CURRENT")

            changed = deepcopy(snapshot)
            changed["execution_slots"][0]["route_id"] = "chat-primary-v2"
            _write_snapshot(root, changed)
            stale = concurrency_dispatch_authorization_dashboard(root)
            self.assertEqual(stale["binding_state"], "STALE")

    def test_resigned_tampered_authorization_is_stale_against_current_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _snapshot()
            _write_snapshot(root, snapshot)
            publish_concurrency_dispatch_authorization(root, _request(snapshot))
            payload = json.loads((root / FILENAME).read_text())
            payload["assignments"][0]["worker_id"] = "other-worker"
            body = {
                key: value
                for key, value in payload.items()
                if key != "authorization_digest"
            }
            payload["authorization_digest"] = hashlib.sha256(
                json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            (root / FILENAME).write_text(json.dumps(payload), encoding="utf-8")
            dashboard = concurrency_dispatch_authorization_dashboard(root)
            self.assertEqual(dashboard["binding_state"], "STALE")

    def test_cli_web_mcp_and_backup_cache_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            root.mkdir()
            snapshot = _snapshot()
            _write_snapshot(root, snapshot)
            request_path = base / "request.json"
            request_path.write_text(json.dumps(_request(snapshot)), encoding="utf-8")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(
                        [
                            "--data-root",
                            str(root),
                            "concurrency",
                            "authorize",
                            "--request",
                            str(request_path),
                        ]
                    ),
                    0,
                )
            cli = json.loads(out.getvalue())
            self.assertEqual(cli["binding_state"], "CURRENT")

            svc = AtlasService(root)
            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                concurrency_authorization_factory=svc.concurrency_dispatch_authorization_dashboard,
            )
            names = {item["name"] for item in tools.list_tools(default_read_scopes())}
            self.assertIn("get_concurrency_dispatch_authorization", names)
            mcp = tools.call(
                "get_concurrency_dispatch_authorization",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(mcp.ok)
            self.assertEqual(mcp.data["binding_state"], "CURRENT")
            self.assertEqual(mcp.data["dispatch_effect_authority"], "NONE")

            app = create_app(root)
            env = {}
            setup_testing_defaults(env)
            env.update(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/concurrency",
                    "QUERY_STRING": "",
                    "HTTP_HOST": "127.0.0.1:8788",
                }
            )
            state = {}

            def start(status, headers):
                state.update(status=status, headers=dict(headers))

            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Multi-node dispatch authorization", body)
            self.assertIn("DISPATCH_AUTHORIZATION_ONLY", body)
            self.assertIn("Dispatch effect authority", body)

            backup_data_root(root, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
