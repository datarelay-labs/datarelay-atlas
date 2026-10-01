"""Work-Packet-bound provider-neutral handoff port for concurrency effects."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any

from atlas.concurrency_admission import concurrency_effect_context
from atlas.concurrency_authorization import (
    concurrency_dispatch_authorization_dashboard,
    validate_concurrency_dispatch_authorization,
)
from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    WORKSTREAM_RE,
    WorktreeIdentity,
    validate_clean_worktree_identity,
)

SCHEMA_VERSION = 1
FILENAME = "concurrency-work-packet-handoffs.json"
LEDGER_KIND = "concurrency_work_packet_handoff_ledger"
AUTHORIZATION_KIND = "concurrency_work_packet_handoff_authorization"
AUTHORITY = "EXTERNAL_CHAT_HANDOFF_ONLY"
ADAPTER_ID = "CHATGPT_EXTERNAL_HANDOFF"
IMPLEMENTER = "CHATGPT_CHAT"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#+-]{0,255}$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_MAX_HANDOFFS = 512


def _reject(message: str) -> None:
    raise ValidationError(message)


def _canonical_digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def concurrency_handoff_worktree_identity_digest(
    identity: WorktreeIdentity,
) -> str:
    """Content-address the bounded exact worktree identity used by handoffs."""
    return _canonical_digest(
        {
            "worktree_path": identity.worktree_path,
            "repository": identity.repository,
            "branch": identity.branch,
            "head": identity.head,
            "toplevel": identity.toplevel,
        }
    )


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        _reject(f"concurrency handoff {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"concurrency handoff {label} is invalid")
    return value


def _validate_registry_entry(value: object) -> dict[str, str]:
    expected = {
        "workstream",
        "worktree_path",
        "implementer_profile",
        "adapter_id",
    }
    if not isinstance(value, dict) or set(value) != expected:
        _reject("concurrency handoff target registry entry is invalid")
    workstream = str(value.get("workstream") or "").strip()
    worktree_path = str(value.get("worktree_path") or "").strip()
    implementer = str(value.get("implementer_profile") or "").strip()
    adapter_id = str(value.get("adapter_id") or "").strip()
    if not WORKSTREAM_RE.fullmatch(workstream):
        _reject("concurrency handoff workstream is invalid")
    if not worktree_path or not Path(worktree_path).is_absolute():
        _reject("concurrency handoff worktree_path must be absolute")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", implementer):
        _reject("concurrency handoff implementer profile is invalid")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", adapter_id):
        _reject("concurrency handoff adapter id is invalid")
    return {
        "workstream": workstream,
        "worktree_path": worktree_path,
        "implementer_profile": implementer,
        "adapter_id": adapter_id,
    }


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "handoffs": [],
    }


def validate_concurrency_handoff_authorization(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "authority",
        "pass_authority",
        "release_authority",
        "spawned",
        "effect_id",
        "authorization_id",
        "authorization_digest",
        "replay_key",
        "node_id",
        "repository",
        "issue_number",
        "branch",
        "head",
        "workstream",
        "worktree_path",
        "worktree_identity_digest",
        "implementer",
        "adapter_id",
        "provider",
        "runtime",
        "route_id",
        "slot_id",
        "worker_id",
        "slot_evidence_ref",
        "intent_revision",
        "change_risk",
        "author_permission",
        "handoff_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency handoff authorization schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != AUTHORIZATION_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("pass_authority") != "NONE"
        or payload.get("release_authority") != "NONE"
        or payload.get("spawned") is not False
        or payload.get("implementer") != IMPLEMENTER
        or payload.get("adapter_id") != ADAPTER_ID
    ):
        _reject("concurrency handoff authorization authority is invalid")
    for key in (
        "effect_id",
        "authorization_id",
        "replay_key",
        "node_id",
        "branch",
        "workstream",
        "provider",
        "runtime",
        "slot_id",
        "worker_id",
        "slot_evidence_ref",
    ):
        _identity(payload.get(key), label=key)
    repository = payload.get("repository")
    if (
        not isinstance(repository, str)
        or repository.count("/") != 1
        or any(not part for part in repository.split("/", 1))
    ):
        _reject("concurrency handoff repository is invalid")
    issue_number = payload.get("issue_number")
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
    ):
        _reject("concurrency handoff issue_number is invalid")
    head = payload.get("head")
    if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
        _reject("concurrency handoff head is invalid")
    worktree_path = payload.get("worktree_path")
    if not isinstance(worktree_path, str) or not worktree_path.startswith("/"):
        _reject("concurrency handoff worktree_path is invalid")
    _digest(payload.get("authorization_digest"), label="authorization_digest")
    _digest(
        payload.get("worktree_identity_digest"),
        label="worktree_identity_digest",
    )
    route_id = payload.get("route_id")
    if route_id is not None:
        _identity(route_id, label="route_id")
    revision = payload.get("intent_revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        _reject("concurrency handoff intent_revision is invalid")
    if payload.get("change_risk") not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        _reject("concurrency handoff change_risk is invalid")
    if payload.get("author_permission") not in {"write", "maintain", "admin"}:
        _reject("concurrency handoff author_permission is invalid")
    digest = _digest(payload.get("handoff_digest"), label="handoff_digest")
    basis = {key: value for key, value in payload.items() if key != "handoff_digest"}
    if _canonical_digest(basis) != digest:
        _reject("concurrency handoff digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def validate_concurrency_handoff_ledger(payload: object) -> dict[str, object]:
    """Validate the complete replay-blocking external handoff ledger."""
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or set(payload) != {"schema_version", "kind", "handoffs"}
        or not isinstance(payload.get("handoffs"), list)
    ):
        _reject("concurrency handoff ledger schema is invalid")
    handoffs = [
        validate_concurrency_handoff_authorization(item)
        for item in payload["handoffs"]
    ]
    refs = [str(item["handoff_digest"]) for item in handoffs]
    replay_keys = [str(item["replay_key"]) for item in handoffs]
    effect_nodes = [
        (str(item["effect_id"]), str(item["node_id"]))
        for item in handoffs
    ]
    if len(refs) != len(set(refs)):
        _reject("concurrency handoff ledger contains duplicate refs")
    if len(replay_keys) != len(set(replay_keys)):
        _reject("concurrency handoff ledger contains duplicate replay keys")
    if len(effect_nodes) != len(set(effect_nodes)):
        _reject("concurrency handoff ledger contains duplicate effect nodes")
    return {**_empty_ledger(), "handoffs": handoffs}


def _load_ledger(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / FILENAME
    if not path.exists():
        return _empty_ledger()
    if path.is_symlink() or not path.is_file():
        _reject("concurrency handoff ledger path is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValidationError("concurrency handoff ledger is unreadable") from exc
    return validate_concurrency_handoff_ledger(payload)


def get_concurrency_handoff_authorization(
    data_root: Path,
    dispatch_ref: str,
) -> dict[str, object]:
    ref = _digest(dispatch_ref, label="dispatch_ref")
    for item in _load_ledger(Path(data_root))["handoffs"]:
        if item["handoff_digest"] == ref:
            return dict(item)
    raise ValidationError("concurrency handoff dispatch_ref is unknown")


class ConcurrencyWorkPacketHandoffPort:
    """Real concurrency effect port that emits durable external Chat handoffs."""

    def __init__(
        self,
        *,
        data_root: Path,
        packet_adapter: GitHubWorkPacketAdapter,
        target_registry: dict[str, dict[str, str]],
        git_runner=None,
    ) -> None:
        root = Path(data_root)
        if root.is_symlink() or not root.is_dir():
            raise ValidationError("concurrency handoff data root is not a directory")
        if not isinstance(packet_adapter, GitHubWorkPacketAdapter):
            raise ValidationError(
                "concurrency handoff requires GitHubWorkPacketAdapter"
            )
        if not isinstance(target_registry, dict) or not target_registry:
            raise ValidationError("concurrency handoff target registry is required")
        normalized: dict[str, dict[str, str]] = {}
        for node_id, entry in target_registry.items():
            node = _identity(node_id, label="registry node_id")
            if node in normalized:
                raise ValidationError(
                    "concurrency handoff target registry node is duplicated"
                )
            normalized[node] = _validate_registry_entry(entry)
        self.data_root = root
        self.packet_adapter = packet_adapter
        self.target_registry = normalized
        self.git_runner = git_runner
        self._context_lock = Lock()
        self._context: dict[str, object] | None = None
        self._repository_locks: dict[str, Lock] = {}
        self._activated_by_repository: dict[str, set[int]] = {}

    def _effect_context(self, request: dict[str, object]) -> dict[str, object]:
        with self._context_lock:
            if self._context is not None:
                context = self._context
                for key in ("effect_id", "authorization_id", "authorization_digest"):
                    if request.get(key) != context[key]:
                        raise ValidationError(
                            "concurrency handoff effect identity changed"
                        )
                return context

            expected_request = {
                "effect_id",
                "authorization_id",
                "authorization_digest",
                "replay_key",
                "assignment",
            }
            if set(request) != expected_request:
                raise ValidationError("concurrency handoff effect request is invalid")
            effect_id = _identity(request.get("effect_id"), label="effect_id")
            authorization_id = _identity(
                request.get("authorization_id"),
                label="authorization_id",
            )
            authorization_digest = _digest(
                request.get("authorization_digest"),
                label="authorization_digest",
            )
            dashboard = concurrency_dispatch_authorization_dashboard(
                self.data_root
            )
            if dashboard.get("binding_state") != "CURRENT":
                raise ValidationError(
                    "concurrency handoff authorization is not current"
                )
            authorization = dashboard.get("authorization")
            authorization = validate_concurrency_dispatch_authorization(
                authorization
            )
            if (
                authorization["authorization_digest"] != authorization_digest
                or authorization["authorization_id"] != authorization_id
            ):
                raise ValidationError(
                    "concurrency handoff authorization identity mismatch"
                )
            context_facts = concurrency_effect_context(
                self.data_root,
                expected_plan_digest=str(authorization["plan_digest"]),
            )
            assignments = {
                str(item["node_id"]): dict(item)
                for item in authorization["assignments"]
            }
            if set(assignments) != set(self.target_registry):
                raise ValidationError(
                    "concurrency handoff target registry does not match assignments"
                )
            if context_facts["assignments"] != authorization["assignments"]:
                raise ValidationError(
                    "concurrency handoff admission assignments drifted"
                )
            limits = {
                str(item["repository"]): int(item["max_wip"])
                for item in context_facts["project_limits"]
            }
            active = {
                str(item["repository"]): set(int(x) for x in item["issue_numbers"])
                for item in context_facts["active_issue_numbers"]
            }
            context = {
                "effect_id": effect_id,
                "authorization_id": authorization_id,
                "authorization_digest": authorization_digest,
                "plan_digest": authorization["plan_digest"],
                "assignments": assignments,
                "project_limits": limits,
                "preexisting_active": active,
            }
            self._context = context
            for repository in limits:
                self._repository_locks.setdefault(repository, Lock())
                self._activated_by_repository.setdefault(repository, set())
            return context

    def dispatch(self, request: dict[str, Any]) -> dict[str, object]:
        """Activate one exact assignment and return a content-addressed handoff ref."""
        try:
            context = self._effect_context(request)
            assignment_raw = request.get("assignment")
            if not isinstance(assignment_raw, dict):
                raise ValidationError(
                    "concurrency handoff assignment is invalid"
                )
            node_id = _identity(
                assignment_raw.get("node_id"),
                label="assignment node_id",
            )
            assignment = context["assignments"].get(node_id)
            if assignment is None or assignment_raw != assignment:
                raise ValidationError(
                    "concurrency handoff assignment is not authorized"
                )
            expected_replay_key = hashlib.sha256(
                (str(context["authorization_digest"]) + ":" + node_id).encode("utf-8")
            ).hexdigest()
            if request.get("replay_key") != expected_replay_key:
                raise ValidationError("concurrency handoff replay key mismatch")

            target = self.target_registry[node_id]
            if (
                target["implementer_profile"] != IMPLEMENTER
                or target["adapter_id"] != ADAPTER_ID
            ):
                raise ValidationError(
                    "concurrency handoff execution adapter is not configured"
                )
            if (
                assignment.get("provider") != "openai"
                or assignment.get("runtime") != "chat_ssh"
            ):
                raise ValidationError(
                    "concurrency handoff adapter attribution mismatch"
                )
            repository = str(assignment["repository"])
            if repository not in context["project_limits"]:
                raise ValidationError(
                    "concurrency handoff repository policy is missing"
                )
            worktree = Path(target["worktree_path"]).resolve()
            if not worktree.is_dir():
                raise ValidationError(
                    "concurrency handoff worktree is unavailable"
                )
            lock = self._repository_locks[repository]
            with lock:
                preexisting = set(
                    context["preexisting_active"].get(repository, set())
                )
                activated = self._activated_by_repository[repository]
                expected_active = sorted(preexisting | activated)
                max_wip = int(context["project_limits"][repository])
                if len(expected_active) >= max_wip:
                    raise ValidationError(
                        "concurrency handoff project WIP is exhausted"
                    )

                identity_before = validate_clean_worktree_identity(
                    str(worktree),
                    repository=repository,
                    branch=str(assignment["branch"]),
                    expected_head=str(assignment["head"]),
                    git_runner=self.git_runner,
                )
                active_packet = self.packet_adapter.activate_concurrency_execution_packet(
                    repository,
                    int(assignment["issue_number"]),
                    branch=str(assignment["branch"]),
                    head=str(assignment["head"]),
                    workstream=target["workstream"],
                    expected_active_issue_numbers=expected_active,
                    max_wip=max_wip,
                )
                activated.add(int(assignment["issue_number"]))
                identity_after = validate_clean_worktree_identity(
                    str(worktree),
                    repository=repository,
                    branch=str(assignment["branch"]),
                    expected_head=str(assignment["head"]),
                    git_runner=self.git_runner,
                )
                if identity_after != identity_before:
                    raise ValidationError(
                        "concurrency handoff worktree identity changed"
                    )
                if any(
                    active_packet.get(key) != value
                    for key, value in {
                        "repository": repository,
                        "issue_number": int(assignment["issue_number"]),
                        "branch": str(assignment["branch"]),
                        "head": str(assignment["head"]),
                        "workstream": target["workstream"],
                        "status": "ACTIVE",
                        "queue_state": "NONE",
                        "implementer": target["implementer_profile"],
                    }.items()
                ):
                    raise ValidationError(
                        "concurrency handoff active packet drifted"
                    )
                worktree_identity_digest = (
                    concurrency_handoff_worktree_identity_digest(identity_after)
                )
                basis = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": AUTHORIZATION_KIND,
                    "authority": AUTHORITY,
                    "pass_authority": "NONE",
                    "release_authority": "NONE",
                    "spawned": False,
                    "effect_id": context["effect_id"],
                    "authorization_id": context["authorization_id"],
                    "authorization_digest": context["authorization_digest"],
                    "replay_key": expected_replay_key,
                    "node_id": node_id,
                    "repository": repository,
                    "issue_number": int(assignment["issue_number"]),
                    "branch": str(assignment["branch"]),
                    "head": str(assignment["head"]),
                    "workstream": target["workstream"],
                    "worktree_path": identity_after.worktree_path,
                    "worktree_identity_digest": worktree_identity_digest,
                    "implementer": target["implementer_profile"],
                    "adapter_id": target["adapter_id"],
                    "provider": str(assignment["provider"]),
                    "runtime": str(assignment["runtime"]),
                    "route_id": assignment["route_id"],
                    "slot_id": str(assignment["slot_id"]),
                    "worker_id": str(assignment["worker_id"]),
                    "slot_evidence_ref": str(assignment["slot_evidence_ref"]),
                    "intent_revision": int(active_packet["intent_revision"]),
                    "change_risk": str(active_packet["change_risk"]),
                    "author_permission": str(active_packet["author_permission"]),
                }
                handoff = {
                    **basis,
                    "handoff_digest": _canonical_digest(basis),
                }
                handoff = validate_concurrency_handoff_authorization(handoff)
                with data_root_write_lock(self.data_root):
                    ledger = _load_ledger(self.data_root)
                    if len(ledger["handoffs"]) >= _MAX_HANDOFFS:
                        raise ValidationError(
                            "concurrency handoff ledger limit reached"
                        )
                    if any(
                        item["handoff_digest"] == handoff["handoff_digest"]
                        for item in ledger["handoffs"]
                    ):
                        raise ValidationError(
                            "concurrency handoff authorization already exists"
                        )
                    ledger["handoffs"].append(handoff)
                    atomic_write_text(
                        self.data_root / FILENAME,
                        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
                    )
                return {
                    "result": "DISPATCHED",
                    "dispatch_ref": handoff["handoff_digest"],
                }
        except ValidationError:
            return {"result": "HUMAN_REQUIRED", "dispatch_ref": None}
