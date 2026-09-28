from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.provenance import ValidationError
from atlas.readiness_authorization import (
    authorize_github_single_effect_file,
    authorize_single_effect,
)
from atlas.readiness_graph import plan_readiness

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs/contracts/readiness-single-effect-authorization.schema.json"
FIXTURE = ROOT / "docs/contracts/fixtures/readiness-single-effect-authorization.example.json"
REPO = "datarelay-labs/datarelay-atlas"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def node(
    node_id: str,
    issue_number: int,
    *,
    repository: str = REPO,
    branch: str = "feature/candidate",
    head: str = HEAD_A,
    packet_status: str = "PAUSED",
    queue_state: str = "QUEUED",
    dependencies: list[str] | None = None,
    owner_gate: bool = False,
    human_required: bool = False,
    priority: int = 10,
) -> dict:
    return {
        "node_id": node_id,
        "issue_number": issue_number,
        "repository": repository,
        "branch": branch,
        "head": head,
        "packet_status": packet_status,
        "queue_state": queue_state,
        "dependencies": [
            {"node_id": dependency, "relation": "REQUIRES_COMPLETE"}
            for dependency in (dependencies or [])
        ],
        "resources": [],
        "authority_state": "TRUSTED",
        "owner_gate": owner_gate,
        "human_required": human_required,
        "priority": priority,
    }


def graph(*nodes: dict, max_wip: int = 1) -> dict:
    return {
        "schema_version": 1,
        "kind": "dependency_readiness_graph",
        "max_wip": max_wip,
        "nodes": list(nodes),
    }


def exact_fact(item: dict) -> dict:
    return {
        "repository": item["repository"],
        "issue_number": item["issue_number"],
        "branch": item["branch"],
        "head": item["head"],
        "packet_status": item["packet_status"],
        "queue_state": item["queue_state"],
    }


def write_graph(payload: dict) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    )
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


class ReadinessAuthorizationTests(unittest.TestCase):
    def test_contract_fixture_and_runtime_shapes_match_schema(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(fixture)

        allow_plan = plan_readiness(graph(node("candidate", 103)))
        allow = authorize_single_effect(lambda: deepcopy(allow_plan))
        validator.validate(allow)

        deny = authorize_single_effect(lambda: {})
        validator.validate(deny)

    def test_exact_double_reconciliation_allows_one_content_free_effect(self) -> None:
        candidate = node("candidate", 103)
        path = write_graph(graph(candidate))
        calls: list[tuple[str, int]] = []

        def read_fact(repository: str, issue_number: int) -> dict:
            calls.append((repository, issue_number))
            return exact_fact(candidate)

        try:
            result = authorize_github_single_effect_file(path, read_fact)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(calls, [(REPO, 103), (REPO, 103)])
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["reasons"], [])
        self.assertRegex(result["plan_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            result["selected_node"],
            {
                "node_id": "candidate",
                "repository": REPO,
                "issue_number": 103,
                "branch": "feature/candidate",
                "head": HEAD_A,
            },
        )
        encoded = json.dumps(result)
        for forbidden in (
            "packet_status",
            "queue_state",
            "OWNER_INTENT",
            "## Goal",
            "prompt",
            "transcript",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_max_wip_not_one_denies_before_github_read(self) -> None:
        path = write_graph(graph(node("candidate", 103), max_wip=2))
        calls = 0

        def read_fact(_repository: str, _issue_number: int) -> dict:
            nonlocal calls
            calls += 1
            raise AssertionError("GitHub must not be read")

        try:
            result = authorize_github_single_effect_file(path, read_fact)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(calls, 0)
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["reasons"], ["MAX_WIP_NOT_ONE"])
        self.assertIsNone(result["plan_digest"])
        self.assertIsNone(result["selected_node"])

    def test_changed_second_plan_denies_without_stale_provenance(self) -> None:
        first = plan_readiness(graph(node("candidate", 103)))
        changed = deepcopy(first)
        changed["nodes"][0]["head"] = HEAD_B
        plans = iter([first, changed])

        result = authorize_single_effect(lambda: next(plans))

        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["reasons"], ["PLAN_CHANGED_BETWEEN_READS"])
        self.assertIsNone(result["plan_digest"])
        self.assertIsNone(result["selected_node"])

    def test_stale_authority_and_owner_gate_cannot_authorize(self) -> None:
        candidate = node("candidate", 103)
        path = write_graph(graph(candidate))

        def stale_fact(_repository: str, _issue_number: int) -> dict:
            value = exact_fact(candidate)
            value["head"] = HEAD_B
            return value

        try:
            stale = authorize_github_single_effect_file(path, stale_fact)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(stale["decision"], "DENY")
        self.assertIn("GRAPH_NOT_READY", stale["reasons"])
        self.assertIn("GRAPH_AUTHORITY_UNSAFE", stale["reasons"])
        self.assertRegex(stale["plan_digest"], r"^[0-9a-f]{64}$")
        self.assertIsNone(stale["selected_node"])

        gated = node("gated", 104, owner_gate=True)
        path = write_graph(graph(gated))
        try:
            owner = authorize_github_single_effect_file(
                path, lambda _repo, _issue: exact_fact(gated)
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(owner["decision"], "DENY")
        self.assertEqual(owner["reasons"], ["SELECTED_NODE_COUNT_NOT_ONE"])

    def test_dependency_and_resource_gates_cannot_authorize(self) -> None:
        active = node(
            "active",
            101,
            branch="feature/shared",
            packet_status="ACTIVE",
            queue_state="NONE",
            priority=0,
        )
        candidate = node(
            "candidate",
            103,
            branch="feature/shared",
            priority=1,
        )
        payload = graph(active, candidate)
        facts = {
            (REPO, 101): exact_fact(active),
            (REPO, 103): exact_fact(candidate),
        }
        path = write_graph(payload)
        try:
            result = authorize_github_single_effect_file(
                path, lambda repository, issue: facts[(repository, issue)]
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["reasons"], ["SELECTED_NODE_COUNT_NOT_ONE"])

        waiting = node("waiting", 103, dependencies=["missing"])
        path = write_graph(graph(waiting))
        try:
            result = authorize_github_single_effect_file(
                path, lambda _repository, _issue: exact_fact(waiting)
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["decision"], "DENY")
        self.assertIn("GRAPH_UNKNOWN_DEPENDENCY", result["reasons"])

    def test_reconciliation_error_and_malformed_plan_fail_closed(self) -> None:
        def failed() -> dict:
            raise ValidationError("provider unavailable")

        failed_result = authorize_single_effect(failed)
        self.assertEqual(failed_result["decision"], "DENY")
        self.assertEqual(failed_result["reasons"], ["RECONCILIATION_ERROR"])

        malformed = {
            "schema_version": True,
            "kind": "dependency_readiness_plan",
        }
        malformed_result = authorize_single_effect(lambda: deepcopy(malformed))
        self.assertEqual(malformed_result["decision"], "DENY")
        self.assertEqual(malformed_result["reasons"], ["PLAN_INVALID"])

        inconsistent = plan_readiness(graph(node("candidate", 103)))
        inconsistent["selected_node_ids"] = []
        inconsistent_result = authorize_single_effect(
            lambda: deepcopy(inconsistent)
        )
        self.assertEqual(inconsistent_result["decision"], "DENY")
        self.assertEqual(inconsistent_result["reasons"], ["PLAN_INVALID"])

    def test_forged_capacity_and_ready_state_cannot_authorize(self) -> None:
        forged_capacity = plan_readiness(graph(node("candidate", 103)))
        forged_capacity["active_count"] = 1
        forged_capacity["available_slots"] = 0
        result = authorize_single_effect(lambda: deepcopy(forged_capacity))
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["reasons"], ["PLAN_INVALID"])

        forged_ready = plan_readiness(
            graph(
                node(
                    "first",
                    103,
                    repository="datarelay-labs/a",
                    branch="feature/a",
                    priority=10,
                ),
                node(
                    "second",
                    104,
                    repository="datarelay-labs/b",
                    branch="feature/b",
                    priority=20,
                ),
                max_wip=2,
            )
        )
        forged_ready["max_wip"] = 1
        forged_ready["available_slots"] = 1
        forged_ready["selected_node_ids"] = ["first"]
        forged_ready["nodes"][1]["selected"] = False
        forged_ready["nodes"][1]["readiness"] = "READY"
        forged_ready["nodes"][1]["reasons"] = []
        result = authorize_single_effect(lambda: deepcopy(forged_ready))
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["reasons"], ["PLAN_INVALID"])

    def test_digest_is_canonical_and_replay_is_idempotent(self) -> None:
        plan = plan_readiness(graph(node("candidate", 103)))
        reordered = {key: plan[key] for key in reversed(list(plan))}
        plans = iter([plan, reordered])
        first = authorize_single_effect(lambda: next(plans))

        plans = iter([deepcopy(reordered), deepcopy(plan)])
        second = authorize_single_effect(lambda: next(plans))

        self.assertEqual(first, second)
        self.assertEqual(first["decision"], "ALLOW")
        self.assertRegex(first["plan_digest"], r"^[0-9a-f]{64}$")

    def test_two_eligible_nodes_still_authorize_only_deterministic_single_selection(self):
        first = node(
            "first",
            103,
            repository="datarelay-labs/a",
            branch="feature/a",
            priority=10,
        )
        second = node(
            "second",
            104,
            repository="datarelay-labs/b",
            branch="feature/b",
            priority=20,
        )
        facts = {
            ("datarelay-labs/a", 103): exact_fact(first),
            ("datarelay-labs/b", 104): exact_fact(second),
        }
        path = write_graph(graph(second, first, max_wip=1))
        try:
            result = authorize_github_single_effect_file(
                path, lambda repository, issue: facts[(repository, issue)]
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["selected_node"]["node_id"], "first")

    def test_cli_exposes_read_only_authorization_without_effect_methods(self) -> None:
        candidate = node("candidate", 103)
        path = write_graph(graph(candidate))
        output = io.StringIO()
        reads = 0

        class FakeAdapter:
            def read_readiness_packet_fact(
                self, repository: str, issue_number: int
            ) -> dict:
                nonlocal reads
                reads += 1
                self.assert_identity = (repository, issue_number)
                return exact_fact(candidate)

            def activate(self, *_args, **_kwargs):
                raise AssertionError("authorization must not mutate")

            def dispatch(self, *_args, **_kwargs):
                raise AssertionError("authorization must not dispatch")

        try:
            with patch("atlas.cli.GitHubWorkPacketAdapter", FakeAdapter):
                with redirect_stdout(output):
                    code = main(
                        [
                            "readiness",
                            "github-authorize",
                            "--graph",
                            str(path),
                        ]
                    )
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(code, 0)
        self.assertEqual(reads, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["selected_node"]["head"], HEAD_A)


if __name__ == "__main__":
    unittest.main()
