from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from atlas.provenance import ValidationError
from atlas.readiness_graph import MAX_GRAPH_BYTES, plan_readiness, plan_readiness_file

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/dependency-readiness-graph.schema.json"
FIXTURE = ROOT / "docs/contracts/fixtures/dependency-readiness-graph.example.json"


def node(
    node_id: str,
    issue_number: int,
    *,
    packet_status: str = "PAUSED",
    queue_state: str = "QUEUED",
    dependencies: list[str] | None = None,
    resources: list[str] | None = None,
    authority_state: str = "TRUSTED",
    owner_gate: bool = False,
    human_required: bool = False,
    priority: int = 100,
    repository: str = "datarelay-labs/datarelay-atlas",
    branch: str | None = None,
) -> dict:
    return {
        "node_id": node_id,
        "issue_number": issue_number,
        "repository": repository,
        "branch": branch or f"feature/{node_id}",
        "head": f"{issue_number:040x}"[-40:],
        "packet_status": packet_status,
        "queue_state": queue_state,
        "dependencies": [
            {"node_id": dependency, "relation": "REQUIRES_COMPLETE"}
            for dependency in (dependencies or [])
        ],
        "resources": resources or [],
        "authority_state": authority_state,
        "owner_gate": owner_gate,
        "human_required": human_required,
        "priority": priority,
    }


def graph(*nodes: dict, max_wip: int = 2) -> dict:
    return {
        "schema_version": 1,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": list(nodes),
    }


def indexed(plan: dict) -> dict[str, dict]:
    return {item["node_id"]: item for item in plan["nodes"]}


class ReadinessGraphTests(unittest.TestCase):
    def test_example_fixture_matches_machine_schema(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(fixture)
        for invalid_branch in (
            "feature//bad",
            "feature/.hidden",
            "feature/foo.lock/bar",
        ):
            invalid = deepcopy(fixture)
            invalid["nodes"][0]["branch"] = invalid_branch
            self.assertTrue(list(validator.iter_errors(invalid)))

    def test_example_fixture_selects_independent_nodes(self) -> None:
        plan = plan_readiness_file(FIXTURE)
        self.assertEqual(plan["graph_state"], "READY")
        self.assertEqual(plan["active_count"], 1)
        self.assertEqual(plan["available_slots"], 2)
        self.assertEqual(
            plan["selected_node_ids"], ["independent-a", "independent-b"]
        )
        states = indexed(plan)
        self.assertEqual(states["foundation"]["readiness"], "COMPLETE")
        self.assertEqual(states["current"]["readiness"], "ACTIVE")

    def test_unknown_dependency_fails_closed(self) -> None:
        plan = plan_readiness(
            graph(node("candidate", 1, dependencies=["missing"]))
        )
        item = indexed(plan)["candidate"]
        self.assertEqual(plan["graph_state"], "HUMAN_REQUIRED")
        self.assertIn("UNKNOWN_DEPENDENCY", plan["graph_reasons"])
        self.assertEqual(plan["selected_node_ids"], [])
        self.assertEqual(item["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(item["reasons"], ["UNKNOWN_DEPENDENCY"])
        self.assertEqual(item["blocked_by"], ["missing"])
        self.assertFalse(item["selected"])

    def test_cycle_stops_selection(self) -> None:
        plan = plan_readiness(
            graph(
                node("a", 1, dependencies=["b"]),
                node("b", 2, dependencies=["a"]),
            )
        )
        self.assertEqual(plan["graph_state"], "HUMAN_REQUIRED")
        self.assertIn("DEPENDENCY_CYCLE", plan["graph_reasons"])
        self.assertEqual(plan["selected_node_ids"], [])
        for item in plan["nodes"]:
            self.assertEqual(item["readiness"], "HUMAN_REQUIRED")
            self.assertIn("DEPENDENCY_CYCLE", item["reasons"])

    def test_lifecycle_dependency_fixed_point_is_order_independent(self) -> None:
        not_done = node("not-done", 1)
        bad_complete = node(
            "bad-complete",
            2,
            packet_status="COMPLETE",
            queue_state="NONE",
            dependencies=["not-done"],
        )
        top_complete = node(
            "top-complete",
            3,
            packet_status="COMPLETE",
            queue_state="NONE",
            dependencies=["bad-complete"],
        )
        candidate = node("candidate", 4, dependencies=["top-complete"])
        first = plan_readiness(
            graph(top_complete, candidate, bad_complete, not_done, max_wip=2)
        )
        second = plan_readiness(
            graph(not_done, bad_complete, candidate, top_complete, max_wip=2)
        )
        self.assertEqual(first, second)
        states = indexed(first)
        self.assertEqual(states["bad-complete"]["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(states["top-complete"]["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(states["candidate"]["readiness"], "BLOCKED")
        self.assertEqual(
            states["candidate"]["reasons"], ["DEPENDENCY_HUMAN_REQUIRED"]
        )

    def test_active_human_required_still_consumes_wip(self) -> None:
        active = node(
            "active",
            1,
            packet_status="ACTIVE",
            queue_state="NONE",
            authority_state="STALE",
        )
        candidate = node("candidate", 2, repository="datarelay-labs/other")
        plan = plan_readiness(graph(active, candidate, max_wip=1))
        self.assertEqual(plan["active_count"], 1)
        self.assertEqual(plan["available_slots"], 0)
        self.assertEqual(plan["selected_node_ids"], [])
        self.assertEqual(plan["graph_state"], "HUMAN_REQUIRED")
        self.assertIn("ACTIVE_NODE_HUMAN_REQUIRED", plan["graph_reasons"])

    def test_active_resource_conflict_fails_whole_plan_closed(self) -> None:
        active_a = node(
            "active-a",
            1,
            packet_status="ACTIVE",
            queue_state="NONE",
            resources=["host:dev-atlas"],
        )
        active_b = node(
            "active-b",
            2,
            packet_status="ACTIVE",
            queue_state="NONE",
            resources=["host:dev-atlas"],
            branch="feature/active-b",
        )
        plan = plan_readiness(graph(active_a, active_b, max_wip=3))
        self.assertEqual(plan["graph_state"], "HUMAN_REQUIRED")
        self.assertIn("ACTIVE_RESOURCE_CONFLICT", plan["graph_reasons"])
        self.assertEqual(plan["selected_node_ids"], [])
        states = indexed(plan)
        self.assertEqual(states["active-a"]["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(states["active-b"]["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(states["active-a"]["blocked_by"], ["active-b"])
        self.assertEqual(states["active-b"]["blocked_by"], ["active-a"])

    def test_candidate_conflicting_with_active_resource_is_blocked(self) -> None:
        active = node(
            "active",
            1,
            packet_status="ACTIVE",
            queue_state="NONE",
            resources=["host:dev-atlas"],
        )
        candidate = node(
            "candidate",
            2,
            resources=["host:dev-atlas"],
            repository="datarelay-labs/other",
        )
        plan = plan_readiness(graph(active, candidate, max_wip=2))
        item = indexed(plan)["candidate"]
        self.assertEqual(item["readiness"], "BLOCKED")
        self.assertEqual(item["reasons"], ["RESOURCE_CONFLICT"])
        self.assertEqual(item["blocked_by"], ["active"])
        self.assertFalse(item["selected"])

    def test_same_batch_resource_conflict_uses_priority(self) -> None:
        earlier = node(
            "earlier",
            1,
            resources=["host:shared"],
            priority=10,
            repository="datarelay-labs/one",
        )
        later = node(
            "later",
            2,
            resources=["host:shared"],
            priority=20,
            repository="datarelay-labs/two",
        )
        plan = plan_readiness(graph(later, earlier, max_wip=2))
        self.assertEqual(plan["selected_node_ids"], ["earlier"])
        states = indexed(plan)
        self.assertTrue(states["earlier"]["selected"])
        self.assertEqual(states["later"]["readiness"], "BLOCKED")
        self.assertEqual(states["later"]["reasons"], ["RESOURCE_CONFLICT"])
        self.assertEqual(states["later"]["blocked_by"], ["earlier"])

    def test_same_branch_is_an_implicit_resource_conflict(self) -> None:
        first = node(
            "first",
            1,
            priority=10,
            branch="feature/shared",
        )
        second = node(
            "second",
            2,
            priority=20,
            branch="feature/shared",
        )
        plan = plan_readiness(graph(second, first, max_wip=2))
        self.assertEqual(plan["selected_node_ids"], ["first"])
        self.assertEqual(indexed(plan)["second"]["readiness"], "BLOCKED")
        self.assertEqual(indexed(plan)["second"]["blocked_by"], ["first"])

    def test_max_wip_blocks_additional_ready_node(self) -> None:
        active = node(
            "active",
            1,
            packet_status="ACTIVE",
            queue_state="NONE",
        )
        candidate = node(
            "candidate",
            2,
            repository="datarelay-labs/other",
        )
        plan = plan_readiness(graph(active, candidate, max_wip=1))
        item = indexed(plan)["candidate"]
        self.assertEqual(item["readiness"], "BLOCKED")
        self.assertEqual(item["reasons"], ["WIP_LIMIT"])
        self.assertEqual(plan["selected_node_ids"], [])

    def test_independent_candidates_fill_capacity_deterministically(self) -> None:
        items = [
            node("c", 3, priority=30, repository="datarelay-labs/c"),
            node("a", 1, priority=10, repository="datarelay-labs/a"),
            node("b", 2, priority=20, repository="datarelay-labs/b"),
        ]
        plan = plan_readiness(graph(*items, max_wip=2))
        self.assertEqual(plan["selected_node_ids"], ["a", "b"])
        self.assertEqual(indexed(plan)["c"]["reasons"], ["WIP_LIMIT"])

    def test_owner_human_and_authority_gates_cannot_be_bypassed(self) -> None:
        plan = plan_readiness(
            graph(
                node("owner", 1, owner_gate=True, priority=0),
                node("human", 2, human_required=True, priority=0),
                node("stale", 3, authority_state="STALE", priority=0),
                node(
                    "safe",
                    4,
                    repository="datarelay-labs/independent",
                    priority=10,
                ),
                max_wip=4,
            )
        )
        states = indexed(plan)
        self.assertEqual(plan["graph_state"], "HUMAN_REQUIRED")
        self.assertIn("AUTHORITY_UNSAFE", plan["graph_reasons"])
        self.assertEqual(states["owner"]["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(states["human"]["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(states["stale"]["readiness"], "HUMAN_REQUIRED")
        self.assertEqual(states["safe"]["readiness"], "BLOCKED")
        self.assertEqual(states["safe"]["reasons"], ["GRAPH_UNSAFE"])
        self.assertEqual(plan["selected_node_ids"], [])

    def test_replay_and_input_order_are_idempotent(self) -> None:
        a = node("a", 1, priority=10, repository="datarelay-labs/a")
        b = node("b", 2, priority=20, repository="datarelay-labs/b")
        first_payload = graph(a, b, max_wip=2)
        second_payload = graph(b, a, max_wip=2)
        first = plan_readiness(first_payload)
        self.assertEqual(first, plan_readiness(deepcopy(first_payload)))
        self.assertEqual(first, plan_readiness(second_payload))

    def test_schema_validation_rejects_ambiguous_inputs(self) -> None:
        valid = graph(node("candidate", 1))
        cases: list[dict] = []

        extra = deepcopy(valid)
        extra["unexpected"] = True
        cases.append(extra)

        boolean_schema_version = deepcopy(valid)
        boolean_schema_version["schema_version"] = True
        cases.append(boolean_schema_version)

        float_schema_version = deepcopy(valid)
        float_schema_version["schema_version"] = 1.0
        cases.append(float_schema_version)

        duplicate = graph(node("same", 1), node("same", 2))
        cases.append(duplicate)

        duplicate_issue = graph(
            node("one", 1),
            node("two", 1, repository="datarelay-labs/datarelay-atlas"),
        )
        cases.append(duplicate_issue)

        for invalid_branch in (
            "feature//bad",
            "feature/.hidden",
            "feature/foo.lock/bar",
        ):
            bad_branch = deepcopy(valid)
            bad_branch["nodes"][0]["branch"] = invalid_branch
            cases.append(bad_branch)

        duplicate_resource = deepcopy(valid)
        duplicate_resource["nodes"][0]["resources"] = ["host:a", "host:a"]
        cases.append(duplicate_resource)

        boolean_priority = deepcopy(valid)
        boolean_priority["nodes"][0]["priority"] = True
        cases.append(boolean_priority)

        bad_relation = deepcopy(valid)
        bad_relation["nodes"][0]["dependencies"] = [
            {"node_id": "other", "relation": "AFTER"}
        ]
        cases.append(bad_relation)

        unhashable_relation = deepcopy(valid)
        unhashable_relation["nodes"][0]["dependencies"] = [
            {"node_id": "other", "relation": []}
        ]
        cases.append(unhashable_relation)

        for field in ("packet_status", "queue_state", "authority_state"):
            malformed_enum = deepcopy(valid)
            malformed_enum["nodes"][0][field] = []
            cases.append(malformed_enum)

        duplicate_dependency = deepcopy(valid)
        duplicate_dependency["nodes"][0]["dependencies"] = [
            {"node_id": "other", "relation": "REQUIRES_COMPLETE"},
            {"node_id": "other", "relation": "REQUIRES_COMPLETE"},
        ]
        cases.append(duplicate_dependency)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    plan_readiness(payload)

    def test_file_loader_rejects_duplicate_json_keys(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            handle.write('{"schema_version":1,"schema_version":1}')
            duplicate_path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
                plan_readiness_file(duplicate_path)
        finally:
            duplicate_path.unlink(missing_ok=True)

    def test_file_loader_enforces_actual_byte_limit(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False
        ) as handle:
            handle.write(b"{" + (b" " * MAX_GRAPH_BYTES))
            oversized_path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValidationError, "bounded import size"):
                plan_readiness_file(oversized_path)
        finally:
            oversized_path.unlink(missing_ok=True)

    def test_file_loader_normalizes_json_numeric_limit_errors(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            handle.write('{"schema_version":' + ("9" * 5000) + "}")
            numeric_path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValidationError, "supported UTF-8 JSON"):
                plan_readiness_file(numeric_path)
        finally:
            numeric_path.unlink(missing_ok=True)

    def test_cli_plan_is_read_only_and_machine_parseable(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "atlas",
                "readiness",
                "plan",
                "--graph",
                str(FIXTURE),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kind"], "dependency_readiness_plan")
        self.assertEqual(
            payload["selected_node_ids"], ["independent-a", "independent-b"]
        )


if __name__ == "__main__":
    unittest.main()
