from __future__ import annotations

import contextlib
import io
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.data_protection import backup_data_root
from atlas.provider_dashboard import provider_dashboard, publish_provider_dashboard_snapshot
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provider_route_quality import (
    FILENAME,
    aggregate_provider_route_outcomes,
    build_provider_route_quality_snapshot,
    provider_route_quality_dashboard,
    publish_provider_route_quality_snapshot,
    validate_provider_route_outcome,
    validate_provider_route_quality_snapshot,
)
from atlas.provenance import ValidationError
from atlas.service import AtlasService
from atlas.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"


def observation(
    *,
    observation_id: str = "obs-1",
    route_id: str = "chat-primary",
    provider: str = "openai",
    runtime: str = "chat_ssh",
    usage_mode: str = "interactive",
    adapter: str = "chat_remote_tools",
    result: str = "PASS",
    first_pass: bool = True,
    findings: int = 0,
    reworks: int = 0,
    owner: int = 0,
    wall_ms: int = 1000,
    quota: int = 0,
    observed_cost: int | None = None,
) -> dict:
    cost = (
        {"status": "UNKNOWN"}
        if observed_cost is None
        else {
            "status": "OBSERVED",
            "milliunits": observed_cost,
            "source_ref": "provider:billing/export",
            "source_digest": "b" * 64,
        }
    )
    return {
        "schema_version": 1,
        "kind": "provider_route_outcome_observation",
        "observation_id": observation_id,
        "route": {
            "route_id": route_id,
            "provider": provider,
            "runtime": runtime,
            "usage_mode": usage_mode,
            "adapter": adapter,
        },
        "work_packet": {
            "repository": "datarelay-labs/datarelay-atlas",
            "issue_number": 184,
            "subject_head": "4" * 40,
            "task_kind": "DEVELOPMENT",
        },
        "outcome": {
            "verified_result": result,
            "first_pass": first_pass,
            "audit_finding_count": findings,
            "rework_count": reworks,
            "owner_intervention_count": owner,
            "wall_time_ms": wall_ms,
            "quota_interruption_count": quota,
        },
        "cost": cost,
        "verification": {
            "source_ref": "github:datarelay-labs/datarelay-atlas#184",
            "source_digest": hashlib.sha256(observation_id.encode("utf-8")).hexdigest(),
            "observed_at": "2026-09-30T12:30:00Z",
        },
    }


class ProviderRouteQualityTests(unittest.TestCase):
    def test_public_schema_fixture_runtime_parity(self):
        schema = json.loads(
            (CONTRACTS / "provider-route-outcome-observation.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "provider-route-outcome-observation.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(validate_provider_route_outcome(fixture), fixture)

    def test_deterministic_aggregate_tracks_quality_and_unknown_cost(self):
        rows = [
            observation(observation_id="a", observed_cost=120, wall_ms=1000),
            observation(
                observation_id="b",
                first_pass=False,
                findings=2,
                reworks=1,
                owner=1,
                wall_ms=3000,
                quota=1,
            ),
        ]
        aggregate = aggregate_provider_route_outcomes(rows)
        self.assertEqual(len(aggregate), 1)
        route = aggregate[0]
        self.assertEqual(route["sample_count"], 2)
        self.assertEqual(route["verified_pass_count"], 2)
        self.assertEqual(route["verified_pass_rate_basis_points"], 10000)
        self.assertEqual(route["first_pass_count"], 1)
        self.assertEqual(route["first_pass_rate_basis_points"], 5000)
        self.assertEqual(route["audit_finding_total"], 2)
        self.assertEqual(route["rework_total"], 1)
        self.assertEqual(route["owner_intervention_total"], 1)
        self.assertEqual(route["quota_interruption_total"], 1)
        self.assertEqual(route["wall_time_total_ms"], 4000)
        self.assertEqual(route["wall_time_average_ms"], 2000)
        self.assertEqual(route["observed_cost_count"], 1)
        self.assertEqual(route["observed_cost_total_milliunits"], 120)
        self.assertEqual(route["unknown_cost_count"], 1)

    def test_invalid_outcome_cost_and_duplicate_identity_fail_closed(self):
        bad = observation(result="FAILED", first_pass=True)
        with self.assertRaises(ValidationError):
            validate_provider_route_outcome(bad)

        bad = observation()
        bad["cost"] = {"status": "UNKNOWN", "milliunits": 1}
        with self.assertRaises(ValidationError):
            validate_provider_route_outcome(bad)

        with self.assertRaisesRegex(ValidationError, "observation_id"):
            build_provider_route_quality_snapshot(
                [observation(observation_id="same"), observation(observation_id="same")]
            )

        conflict = observation(observation_id="b", runtime="different_runtime")
        with self.assertRaisesRegex(ValidationError, "conflicting identity"):
            build_provider_route_quality_snapshot(
                [observation(observation_id="a"), conflict]
            )

    def test_rework_requires_rework_count(self):
        with self.assertRaisesRegex(ValidationError, "rework_count"):
            validate_provider_route_outcome(
                observation(result="REWORK", first_pass=False, reworks=0)
            )
        valid = validate_provider_route_outcome(
            observation(result="REWORK", first_pass=False, reworks=1)
        )
        self.assertEqual(valid["outcome"]["verified_result"], "REWORK")

    def test_snapshot_is_input_order_independent_and_rejects_duplicate_evidence(self):
        first = observation(observation_id="a")
        second = observation(observation_id="b", route_id="other-route")
        second["verification"]["source_digest"] = "c" * 64
        forward = build_provider_route_quality_snapshot([first, second])
        reverse = build_provider_route_quality_snapshot([second, first])
        self.assertEqual(forward, reverse)

        duplicate = observation(observation_id="different-id")
        duplicate["verification"]["source_digest"] = first["verification"]["source_digest"]
        with self.assertRaisesRegex(ValidationError, "verification evidence"):
            build_provider_route_quality_snapshot([first, duplicate])

    def test_snapshot_tamper_fails_closed(self):
        snapshot = build_provider_route_quality_snapshot([observation()])
        self.assertEqual(validate_provider_route_quality_snapshot(snapshot), snapshot)
        tampered = deepcopy(snapshot)
        tampered["routes"][0]["verified_pass_count"] = 0
        with self.assertRaisesRegex(ValidationError, "derived state mismatch"):
            validate_provider_route_quality_snapshot(tampered)

    def test_publish_cli_dashboard_and_backup_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            obs = base / "obs.json"
            obs.write_text(json.dumps(observation()), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(
                        [
                            "--data-root",
                            str(data),
                            "providers",
                            "quality-publish",
                            "--observation",
                            str(obs),
                        ]
                    ),
                    0,
                )
            dashboard = json.loads(out.getvalue())
            self.assertEqual(dashboard["state"], "OBSERVED")
            self.assertEqual(dashboard["broker_influence"], "NONE")
            self.assertEqual(dashboard["observation_count"], 1)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(["--data-root", str(data), "providers", "quality-show"]),
                    0,
                )
            self.assertEqual(json.loads(out.getvalue())["authority"], "EVIDENCE_ONLY")

            # The snapshot is rebuildable cache, not durable backup authority.
            backup_data_root(data, base / "backup")
            self.assertFalse((base / "backup" / FILENAME).exists())

    def test_provider_dashboard_broker_plan_unchanged_by_quality_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            candidate = base / "candidate.json"
            candidate.write_text(
                (FIXTURES / "provider-route-codex.example.json").read_text(),
                encoding="utf-8",
            )
            publish_provider_dashboard_snapshot(
                data,
                candidate_paths=[candidate],
                observed_at="2026-09-28T00:00:00Z",
                required_capability="CODE_REVIEW",
                strategy="CAPABILITY_FIRST",
                max_evidence_age_seconds=0,
            )
            before = provider_dashboard(data)
            before_plan = deepcopy(before["plan"])
            self.assertEqual(before["route_quality"]["state"], "UNKNOWN")

            obs = base / "obs.json"
            row = observation(
                route_id="codex-primary",
                provider="codex",
                runtime="codex_cli",
                usage_mode="chatgpt_plan",
                adapter="CodexAuditProvider",
            )
            obs.write_text(json.dumps(row), encoding="utf-8")
            publish_provider_route_quality_snapshot(data, [obs])
            after = provider_dashboard(data)
            self.assertEqual(after["plan"], before_plan)
            self.assertEqual(after["route_quality"]["state"], "OBSERVED")
            self.assertEqual(after["route_quality"]["broker_influence"], "NONE")

    def test_web_and_mcp_read_only_surfaces_expose_quality_without_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc = AtlasService(root)
            obs = root / "input.json"
            obs.write_text(json.dumps(observation()), encoding="utf-8")
            svc.publish_provider_route_quality([obs])

            app = create_app(root)
            env = {}
            setup_testing_defaults(env)
            env.update(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/providers",
                    "QUERY_STRING": "",
                    "HTTP_HOST": "127.0.0.1:8788",
                }
            )
            state = {}
            def start(status, headers):
                state.update(status=status, headers=dict(headers))
            body = b"".join(app(env, start)).decode()
            self.assertEqual(state["status"], "200 OK")
            self.assertIn("Verified route outcomes", body)
            self.assertIn("EVIDENCE_ONLY", body)
            self.assertIn("broker influence NONE", body)
            self.assertIn("chat-primary", body)

            tools = AtlasContextTools(
                retriever_factory=svc.project_retriever,
                provider_route_quality_factory=svc.provider_route_quality_dashboard,
            )
            names = {tool["name"] for tool in tools.list_tools(default_read_scopes())}
            self.assertIn("get_provider_route_quality", names)
            result = tools.call(
                "get_provider_route_quality",
                {},
                scopes=default_read_scopes(),
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.data["authority"], "EVIDENCE_ONLY")
            self.assertEqual(result.data["broker_influence"], "NONE")
            self.assertEqual(result.data["routes"][0]["route_id"], "chat-primary")

    def test_service_surface_reads_same_quality_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc = AtlasService(root)
            obs = root / "input.json"
            obs.write_text(json.dumps(observation()), encoding="utf-8")
            svc.publish_provider_route_quality([obs])
            self.assertEqual(
                svc.provider_route_quality_dashboard(),
                provider_route_quality_dashboard(root),
            )


if __name__ == "__main__":
    unittest.main()
