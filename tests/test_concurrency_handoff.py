from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from atlas.concurrency_admission import SNAPSHOT_FILENAME, plan_concurrency_admission
from atlas.concurrency_authorization import publish_concurrency_dispatch_authorization
from atlas.concurrency_effect import commit_concurrency_dispatch_effect
from atlas.concurrency_handoff import (
    ADAPTER_ID,
    ConcurrencyWorkPacketHandoffPort,
    get_concurrency_handoff_authorization,
    validate_concurrency_handoff_authorization,
)
from atlas.data_protection import backup_data_root
from atlas.provenance import ValidationError
from atlas.work_controller import GitHubWorkPacketAdapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs" / "contracts" / "fixtures"
CONTRACTS = ROOT / "docs" / "contracts"
REPO = "datarelay-labs/datarelay-atlas"


def _snapshot() -> dict:
    payload = json.loads(
        (FIXTURES / "concurrency-admission-snapshot.example.json").read_text()
    )
    # Both assignments use the approved external Chat handoff runtime in this
    # acceptance fixture; provider/route identity remains assignment-specific.
    payload["execution_slots"][1]["runtime"] = "chat_ssh"
    payload["execution_slots"][1]["route_id"] = "chat-secondary"
    return payload


def _prepare(root: Path, snapshot: dict | None = None) -> dict:
    snapshot = deepcopy(snapshot or _snapshot())
    (root / SNAPSHOT_FILENAME).write_text(json.dumps(snapshot), encoding="utf-8")
    plan = plan_concurrency_admission(snapshot)
    request = {
        "schema_version": 1,
        "kind": "concurrency_dispatch_authorization_request",
        "authorization_id": "handoff-auth-1",
        "expected_plan_digest": plan["plan_digest"],
        "evaluated_at": "2026-09-30T12:30:00Z",
    }
    dashboard = publish_concurrency_dispatch_authorization(root, request)
    return dashboard["authorization"]


def _packet_body(assignment: dict, workstream: str) -> str:
    return (
        "PACKET_VERSION=2\n"
        f"TARGET_REPO={assignment['repository']}\n"
        f"WORKSTREAM={workstream}\n"
        "STATUS=PAUSED\n"
        "QUEUE_STATE=QUEUED\n"
        f"BRANCH={assignment['branch']}\n"
        "TASK_KIND=DEVELOPMENT\n"
        "OWNER_INTENT=Execute exact concurrency assignment.\n"
        f"LAST_VERIFIED_HEAD={assignment['head']}\n"
        "IMPLEMENTER=CHATGPT_CHAT\n"
        "CHANGE_RISK=MEDIUM\n"
        "INTENT_REVISION=2\n"
    )


class MemoryPacketAdapter(GitHubWorkPacketAdapter):
    def __init__(
        self,
        assignments: list[dict],
        workstreams: dict[str, str],
        *,
        extra_active: list[int] | None = None,
    ) -> None:
        self.packets: dict[int, dict] = {}
        for assignment in assignments:
            issue = int(assignment["issue_number"])
            node_id = str(assignment["node_id"])
            self.packets[issue] = {
                "assignment": deepcopy(assignment),
                "workstream": workstreams[node_id],
                "status": "PAUSED",
                "queue_state": "QUEUED",
                "body": _packet_body(assignment, workstreams[node_id]),
            }
        self.extra_active = sorted(extra_active or [])
        self.activation_order: list[int] = []

    def _assert_ai_work_issue(self, payload: dict, *, issue_number: int) -> None:
        if payload.get("number") != issue_number:
            raise ValidationError("issue mismatch")

    def _require_trusted_issue_author(self, repository: str, payload: dict) -> None:
        if repository != REPO:
            raise ValidationError("repo mismatch")

    def _view_issue(self, repository: str, issue_number: int) -> dict:
        packet = self.packets[issue_number]
        return {
            "number": issue_number,
            "title": "[AI Work] concurrency packet",
            "state": "OPEN",
            "body": packet["body"],
            "updatedAt": f"2026-09-30T12:3{len(self.activation_order)}:00Z",
            "author": {"login": "trusted-author"},
        }

    def reread_trusted_queued_execution_packet(
        self, repository: str, issue_number: int
    ) -> dict:
        packet = self.packets[issue_number]
        assignment = packet["assignment"]
        if packet["status"] != "PAUSED" or packet["queue_state"] != "QUEUED":
            raise ValidationError("not queued")
        return {
            "repository": repository,
            "issue_number": issue_number,
            "branch": assignment["branch"],
            "workstream": packet["workstream"],
            "head": assignment["head"],
            "packet_status": "PAUSED",
            "queue_state": "QUEUED",
            "implementer": "CHATGPT_CHAT",
            "change_risk": "MEDIUM",
            "intent_revision": 2,
            "author_permission": "admin",
            "updated_at": "2026-09-30T12:30:00Z",
        }

    def _trusted_repository_active_issue_numbers(self, repository: str) -> list[int]:
        active = [
            issue
            for issue, packet in self.packets.items()
            if packet["status"] == "ACTIVE"
        ]
        return sorted([*self.extra_active, *active])

    def _cas_replace_issue_body(
        self,
        repository: str,
        issue_number: int,
        *,
        original_body: str,
        original_updated_at: str,
        new_body: str,
        require_trusted_author: bool,
        require_open_ai_work: bool,
        before_edit,
    ) -> None:
        packet = self.packets[issue_number]
        if packet["body"] != original_body:
            raise ValidationError("cas body drift")
        before_edit()
        packet["body"] = new_body
        packet["status"] = "ACTIVE"
        packet["queue_state"] = "NONE"
        self.activation_order.append(issue_number)

    def read_readiness_packet_fact(
        self, repository: str, issue_number: int
    ) -> dict:
        packet = self.packets[issue_number]
        assignment = packet["assignment"]
        return {
            "repository": repository,
            "issue_number": issue_number,
            "branch": assignment["branch"],
            "head": assignment["head"],
            "packet_status": packet["status"],
            "queue_state": packet["queue_state"],
        }

    def reread_trusted_active_packet(
        self, repository: str, issue_number: int
    ) -> dict:
        packet = self.packets[issue_number]
        assignment = packet["assignment"]
        if packet["status"] != "ACTIVE":
            raise ValidationError("not active")
        return {
            "repository": repository,
            "issue_number": issue_number,
            "branch": assignment["branch"],
            "workstream": packet["workstream"],
            "head": assignment["head"],
            "audit_base": "",
            "implementer": "CHATGPT_CHAT",
            "status": "ACTIVE",
            "updated_at": "2026-09-30T12:35:00Z",
        }


def _git_runner(bindings: dict[str, dict[str, str]]):
    def runner(argv: list[str], cwd: str) -> str:
        row = bindings[str(Path(cwd).resolve())]
        if argv == ["git", "rev-parse", "--show-toplevel"]:
            return str(Path(cwd).resolve())
        if argv == ["git", "remote", "get-url", "origin"]:
            return "https://github.com/datarelay-labs/datarelay-atlas.git"
        if argv == ["git", "branch", "--show-current"]:
            return row["branch"]
        if argv == ["git", "rev-parse", "HEAD"]:
            return row["head"]
        if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return row.get("status", "")
        raise AssertionError(argv)
    return runner


def _registry(base: Path, auth: dict) -> tuple[dict, dict[str, dict[str, str]]]:
    registry = {}
    bindings: dict[str, dict[str, str]] = {}
    for index, assignment in enumerate(auth["assignments"], start=1):
        path = base / f"worktree-{index}"
        path.mkdir()
        node = str(assignment["node_id"])
        registry[node] = {
            "workstream": f"concurrency-node-{index}",
            "worktree_path": str(path),
            "implementer_profile": "CHATGPT_CHAT",
            "adapter_id": ADAPTER_ID,
        }
        bindings[str(path.resolve())] = {
            "branch": str(assignment["branch"]),
            "head": str(assignment["head"]),
            "status": "",
        }
    return registry, bindings


class ConcurrencyHandoffTests(unittest.TestCase):
    def test_public_schema_fixture_and_runtime_parity(self) -> None:
        schema = json.loads(
            (CONTRACTS / "concurrency-work-packet-handoff-authorization.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "concurrency-work-packet-handoff-authorization.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        self.assertEqual(
            validate_concurrency_handoff_authorization(fixture),
            fixture,
        )

    def test_two_assignments_activate_under_max_wip_and_emit_distinct_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            auth = _prepare(data)
            registry, bindings = _registry(base, auth)
            workstreams = {
                node: row["workstream"] for node, row in registry.items()
            }
            adapter = MemoryPacketAdapter(
                auth["assignments"],
                workstreams,
            )
            port = ConcurrencyWorkPacketHandoffPort(
                data_root=data,
                packet_adapter=adapter,
                target_registry=registry,
                git_runner=_git_runner(bindings),
            )
            receipt = commit_concurrency_dispatch_effect(
                data,
                effect_id="handoff-effect-1",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            self.assertEqual(receipt["result"], "DISPATCHED")
            self.assertEqual(receipt["dispatched_count"], 2)
            self.assertEqual(set(adapter.activation_order), {201, 202})
            refs = [item["dispatch_ref"] for item in receipt["receipts"]]
            self.assertEqual(len(set(refs)), 2)
            for item, assignment in zip(receipt["receipts"], auth["assignments"]):
                handoff = get_concurrency_handoff_authorization(
                    data, item["dispatch_ref"]
                )
                self.assertEqual(handoff["node_id"], assignment["node_id"])
                self.assertEqual(handoff["repository"], assignment["repository"])
                self.assertEqual(handoff["issue_number"], assignment["issue_number"])
                self.assertEqual(handoff["branch"], assignment["branch"])
                self.assertEqual(handoff["head"], assignment["head"])
                self.assertEqual(handoff["provider"], assignment["provider"])
                self.assertEqual(handoff["runtime"], assignment["runtime"])
                self.assertEqual(handoff["route_id"], assignment["route_id"])
                self.assertEqual(handoff["adapter_id"], ADAPTER_ID)
                self.assertEqual(handoff["pass_authority"], "NONE")
                self.assertEqual(handoff["release_authority"], "NONE")
                self.assertFalse(handoff["spawned"])

    def test_worktree_or_packet_binding_mismatch_yields_no_handoff_for_node(self) -> None:
        for mismatch in ("branch", "implementer"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                data = base / "data"
                data.mkdir()
                auth = _prepare(data)
                registry, bindings = _registry(base, auth)
                workstreams = {
                    node: row["workstream"] for node, row in registry.items()
                }
                adapter = MemoryPacketAdapter(auth["assignments"], workstreams)
                first = auth["assignments"][0]
                node = str(first["node_id"])
                if mismatch == "branch":
                    path = str(Path(registry[node]["worktree_path"]).resolve())
                    bindings[path]["branch"] = "feat/wrong-branch"
                else:
                    registry[node]["implementer_profile"] = "CURSOR"
                port = ConcurrencyWorkPacketHandoffPort(
                    data_root=data,
                    packet_adapter=adapter,
                    target_registry=registry,
                    git_runner=_git_runner(bindings),
                )
                receipt = commit_concurrency_dispatch_effect(
                    data,
                    effect_id=f"mismatch-{mismatch}",
                    expected_authorization_digest=auth["authorization_digest"],
                    effect_port=port,
                )
                first_receipt = next(
                    item for item in receipt["receipts"]
                    if item["node_id"] == node
                )
                self.assertEqual(first_receipt["result"], "HUMAN_REQUIRED")
                self.assertIsNone(first_receipt["dispatch_ref"])
                self.assertNotIn(int(first["issue_number"]), adapter.activation_order)
                self.assertEqual(receipt["dispatched_count"], 1)
                self.assertEqual(len(adapter.activation_order), 1)

    def test_non_chat_runtime_is_bounded_to_one_node_without_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            snapshot = json.loads(
                (FIXTURES / "concurrency-admission-snapshot.example.json").read_text()
            )
            auth = _prepare(data, snapshot)
            registry, bindings = _registry(base, auth)
            adapter = MemoryPacketAdapter(
                auth["assignments"],
                {node: row["workstream"] for node, row in registry.items()},
            )
            port = ConcurrencyWorkPacketHandoffPort(
                data_root=data,
                packet_adapter=adapter,
                target_registry=registry,
                git_runner=_git_runner(bindings),
            )
            receipt = commit_concurrency_dispatch_effect(
                data,
                effect_id="runtime-mismatch-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            by_runtime = {
                item["runtime"]: item
                for item in receipt["receipts"]
            }
            self.assertEqual(by_runtime["chat_ssh"]["result"], "DISPATCHED")
            self.assertEqual(by_runtime["codex"]["result"], "HUMAN_REQUIRED")
            self.assertEqual(receipt["dispatched_count"], 1)
            self.assertEqual(receipt["human_required_count"], 1)
            self.assertEqual(len(adapter.activation_order), 1)

    def test_unrelated_active_drift_blocks_all_mutation_for_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            auth = _prepare(data)
            registry, bindings = _registry(base, auth)
            adapter = MemoryPacketAdapter(
                auth["assignments"],
                {node: row["workstream"] for node, row in registry.items()},
                extra_active=[999],
            )
            port = ConcurrencyWorkPacketHandoffPort(
                data_root=data,
                packet_adapter=adapter,
                target_registry=registry,
                git_runner=_git_runner(bindings),
            )
            receipt = commit_concurrency_dispatch_effect(
                data,
                effect_id="active-drift-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            self.assertEqual(receipt["result"], "HUMAN_REQUIRED")
            self.assertEqual(receipt["dispatched_count"], 0)
            self.assertEqual(adapter.activation_order, [])

    def test_target_registry_must_exactly_cover_authorized_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            auth = _prepare(data)
            registry, bindings = _registry(base, auth)
            registry.pop(next(iter(registry)))
            adapter = MemoryPacketAdapter(
                auth["assignments"],
                {
                    str(item["node_id"]): f"concurrency-node-{index}"
                    for index, item in enumerate(auth["assignments"], start=1)
                },
            )
            port = ConcurrencyWorkPacketHandoffPort(
                data_root=data,
                packet_adapter=adapter,
                target_registry=registry,
                git_runner=_git_runner(bindings),
            )
            receipt = commit_concurrency_dispatch_effect(
                data,
                effect_id="registry-gap-effect",
                expected_authorization_digest=auth["authorization_digest"],
                effect_port=port,
            )
            self.assertEqual(receipt["result"], "HUMAN_REQUIRED")
            self.assertEqual(adapter.activation_order, [])

    def test_handoff_ledger_is_excluded_from_backup_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            data.mkdir()
            # Backup needs its durable registry source.
            (data / "registry.json").write_text(
                '{"schema_version":1,"projects":{}}\n',
                encoding="utf-8",
            )
            (data / "concurrency-work-packet-handoffs.json").write_text(
                '{"schema_version":1,"kind":"concurrency_work_packet_handoff_ledger","handoffs":[]}\n',
                encoding="utf-8",
            )
            dest = base / "backup"
            result = backup_data_root(data, dest)
            self.assertNotIn(
                "concurrency-work-packet-handoffs.json",
                json.dumps(result),
            )
            self.assertFalse(
                (dest / "concurrency-work-packet-handoffs.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
