"""One-shot claim/start evidence for external concurrency handoffs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from atlas.concurrency_effect import get_concurrency_dispatch_effect_entry
from atlas.concurrency_handoff import (
    ADAPTER_ID,
    IMPLEMENTER,
    concurrency_handoff_worktree_identity_digest,
    get_concurrency_handoff_authorization,
    validate_concurrency_handoff_authorization,
)
from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    validate_clean_worktree_identity,
)

SCHEMA_VERSION = 1
REQUEST_KIND = "concurrency_handoff_claim_request"
RECEIPT_KIND = "concurrency_handoff_claim_receipt"
LEDGER_KIND = "concurrency_handoff_claim_ledger"
FILENAME = "concurrency-handoff-claims.json"
AUTHORITY = "START_EVIDENCE_ONLY"
RESULT = "CLAIMED"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#+-]{0,255}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_MAX_CLAIMS = 512


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


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        _reject(f"concurrency claim {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _reject(f"concurrency claim {label} is invalid")
    return value


def validate_concurrency_handoff_claim_request(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "claim_id",
        "handoff_digest",
        "effect_id",
        "node_id",
        "repository",
        "issue_number",
        "branch",
        "head",
        "worktree_path",
        "adapter_id",
        "provider",
        "runtime",
        "route_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency claim request schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != REQUEST_KIND
    ):
        _reject("concurrency claim request version/kind is invalid")
    repository = payload.get("repository")
    issue_number = payload.get("issue_number")
    head = payload.get("head")
    worktree_path = payload.get("worktree_path")
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        _reject("concurrency claim repository is invalid")
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
    ):
        _reject("concurrency claim issue_number is invalid")
    if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
        _reject("concurrency claim head is invalid")
    if (
        not isinstance(worktree_path, str)
        or not worktree_path.startswith("/")
        or len(worktree_path) > 1024
        or any(ch in worktree_path for ch in "\r\n\0")
    ):
        _reject("concurrency claim worktree_path is invalid")
    route_id = payload.get("route_id")
    if route_id is not None:
        _identity(route_id, label="route_id")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "claim_id": _identity(payload.get("claim_id"), label="claim_id"),
        "handoff_digest": _digest(
            payload.get("handoff_digest"),
            label="handoff_digest",
        ),
        "effect_id": _identity(payload.get("effect_id"), label="effect_id"),
        "node_id": _identity(payload.get("node_id"), label="node_id"),
        "repository": repository,
        "issue_number": issue_number,
        "branch": _identity(payload.get("branch"), label="branch"),
        "head": head,
        "worktree_path": worktree_path,
        "adapter_id": _identity(payload.get("adapter_id"), label="adapter_id"),
        "provider": _identity(payload.get("provider"), label="provider"),
        "runtime": _identity(payload.get("runtime"), label="runtime"),
        "route_id": route_id,
    }
    assert_content_free(normalized)
    return normalized


def validate_concurrency_handoff_claim_receipt(
    payload: object,
) -> dict[str, object]:
    expected = {
        "schema_version",
        "kind",
        "result",
        "authority",
        "spawned",
        "completion_authority",
        "pass_authority",
        "release_authority",
        "claim_id",
        "handoff_digest",
        "effect_id",
        "effect_receipt_digest",
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
        "claim_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("concurrency claim receipt schema is invalid")
    if (
        isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != RECEIPT_KIND
        or payload.get("result") != RESULT
        or payload.get("authority") != AUTHORITY
        or payload.get("spawned") is not False
        or payload.get("completion_authority") != "NONE"
        or payload.get("pass_authority") != "NONE"
        or payload.get("release_authority") != "NONE"
        or payload.get("implementer") != IMPLEMENTER
        or payload.get("adapter_id") != ADAPTER_ID
        or payload.get("provider") != "openai"
        or payload.get("runtime") != "chat_ssh"
    ):
        _reject("concurrency claim receipt authority/adapter is invalid")
    for key in (
        "claim_id",
        "effect_id",
        "authorization_id",
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
    issue_number = payload.get("issue_number")
    head = payload.get("head")
    worktree_path = payload.get("worktree_path")
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        _reject("concurrency claim receipt repository is invalid")
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
    ):
        _reject("concurrency claim receipt issue_number is invalid")
    if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
        _reject("concurrency claim receipt head is invalid")
    if (
        not isinstance(worktree_path, str)
        or not worktree_path.startswith("/")
        or len(worktree_path) > 1024
        or any(ch in worktree_path for ch in "\r\n\0")
    ):
        _reject("concurrency claim receipt worktree_path is invalid")
    route_id = payload.get("route_id")
    if route_id is not None:
        _identity(route_id, label="route_id")
    for key in (
        "handoff_digest",
        "effect_receipt_digest",
        "authorization_digest",
        "replay_key",
        "worktree_identity_digest",
    ):
        _digest(payload.get(key), label=key)
    claim_digest = _digest(payload.get("claim_digest"), label="claim_digest")
    basis = {
        key: value for key, value in payload.items()
        if key != "claim_digest"
    }
    if _canonical_digest(basis) != claim_digest:
        _reject("concurrency claim digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "claims": [],
    }


def validate_concurrency_claim_ledger(payload: object) -> dict[str, object]:
    """Validate the complete replay-blocking handoff-claim ledger."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "claims"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or not isinstance(payload.get("claims"), list)
    ):
        _reject("concurrency claim ledger schema is invalid")
    claims: list[dict[str, object]] = []
    claim_ids: set[str] = set()
    handoff_digests: set[str] = set()
    claim_digests: set[str] = set()
    for item in payload["claims"]:
        expected = {
            "claim_id",
            "handoff_digest",
            "state",
            "receipt",
            "claim_digest",
        }
        if not isinstance(item, dict) or set(item) != expected:
            _reject("concurrency claim ledger entry schema is invalid")
        claim_id = _identity(item.get("claim_id"), label="claim_id")
        handoff_digest = _digest(
            item.get("handoff_digest"),
            label="handoff_digest",
        )
        if claim_id in claim_ids or handoff_digest in handoff_digests:
            _reject("concurrency claim ledger replay identity is duplicated")
        state = item.get("state")
        if state == "IN_PROGRESS":
            if item.get("receipt") is not None or item.get("claim_digest") is not None:
                _reject("concurrency claim in-progress reservation is invalid")
        elif state == "TERMINAL":
            claim_digest = _digest(item.get("claim_digest"), label="claim_digest")
            receipt = validate_concurrency_handoff_claim_receipt(
                item.get("receipt")
            )
            if (
                receipt["claim_id"] != claim_id
                or receipt["handoff_digest"] != handoff_digest
                or receipt["claim_digest"] != claim_digest
            ):
                _reject("concurrency claim terminal ledger binding is invalid")
            if claim_digest in claim_digests:
                _reject("concurrency claim ledger digest is duplicated")
            claim_digests.add(claim_digest)
        else:
            _reject("concurrency claim ledger state is invalid")
        claim_ids.add(claim_id)
        handoff_digests.add(handoff_digest)
        claims.append(dict(item))
    return {**_empty_ledger(), "claims": claims}


def _load_ledger(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / FILENAME
    if path.is_symlink():
        _reject("concurrency claim ledger path is unsafe")
    if not path.exists():
        return _empty_ledger()
    if not path.is_file():
        _reject("concurrency claim ledger path is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValidationError("concurrency claim ledger is unreadable") from exc
    return validate_concurrency_claim_ledger(payload)


def _validate_request_matches_handoff(
    request: dict[str, object],
    handoff: dict[str, object],
) -> None:
    bindings = {
        "handoff_digest": "handoff_digest",
        "effect_id": "effect_id",
        "node_id": "node_id",
        "repository": "repository",
        "issue_number": "issue_number",
        "branch": "branch",
        "head": "head",
        "worktree_path": "worktree_path",
        "adapter_id": "adapter_id",
        "provider": "provider",
        "runtime": "runtime",
        "route_id": "route_id",
    }
    for request_key, handoff_key in bindings.items():
        if request[request_key] != handoff[handoff_key]:
            _reject(
                f"concurrency claim {request_key} does not match handoff"
            )


def _validate_source_effect(
    data_root: Path,
    handoff: dict[str, object],
) -> str:
    entry = get_concurrency_dispatch_effect_entry(
        Path(data_root),
        str(handoff["effect_id"]),
    )
    if entry.get("state") != "TERMINAL":
        _reject("concurrency claim source effect is not terminal")
    receipt = entry.get("receipt")
    receipt_digest = _digest(
        entry.get("receipt_digest"),
        label="effect_receipt_digest",
    )
    if not isinstance(receipt, dict):
        _reject("concurrency claim source effect receipt is missing")
    if (
        receipt.get("effect_id") != handoff["effect_id"]
        or receipt.get("authorization_id") != handoff["authorization_id"]
        or receipt.get("authorization_digest") != handoff["authorization_digest"]
    ):
        _reject("concurrency claim source effect identity drifted")
    matches = [
        item for item in receipt.get("receipts", [])
        if isinstance(item, dict) and item.get("node_id") == handoff["node_id"]
    ]
    if len(matches) != 1:
        _reject("concurrency claim source effect node is unavailable")
    source = matches[0]
    expected = {
        "slot_id": handoff["slot_id"],
        "worker_id": handoff["worker_id"],
        "provider": handoff["provider"],
        "runtime": handoff["runtime"],
        "route_id": handoff["route_id"],
        "head": handoff["head"],
        "replay_key": handoff["replay_key"],
        "result": "DISPATCHED",
        "dispatch_ref": handoff["handoff_digest"],
    }
    if any(source.get(key) != value for key, value in expected.items()):
        _reject("concurrency claim source dispatch receipt drifted")
    return receipt_digest


def _validate_packet_and_worktree(
    handoff: dict[str, object],
    *,
    packet_adapter: GitHubWorkPacketAdapter,
    git_runner=None,
) -> None:
    repository = str(handoff["repository"])
    issue_number = int(handoff["issue_number"])
    fact = packet_adapter.read_readiness_packet_fact(
        repository,
        issue_number,
    )
    expected_fact = {
        "repository": repository,
        "issue_number": issue_number,
        "branch": handoff["branch"],
        "head": handoff["head"],
        "packet_status": "ACTIVE",
        "queue_state": "NONE",
    }
    if fact != expected_fact:
        _reject("concurrency claim canonical packet lifecycle drifted")
    active = packet_adapter.reread_trusted_active_packet(
        repository,
        issue_number,
    )
    for key, value in {
        "repository": repository,
        "issue_number": issue_number,
        "branch": handoff["branch"],
        "head": handoff["head"],
        "workstream": handoff["workstream"],
        "implementer": handoff["implementer"],
        "status": "ACTIVE",
    }.items():
        if active.get(key) != value:
            _reject("concurrency claim canonical packet identity drifted")
    identity = validate_clean_worktree_identity(
        str(handoff["worktree_path"]),
        repository=repository,
        branch=str(handoff["branch"]),
        expected_head=str(handoff["head"]),
        git_runner=git_runner,
    )
    digest = concurrency_handoff_worktree_identity_digest(identity)
    if digest != handoff["worktree_identity_digest"]:
        _reject("concurrency claim worktree identity digest drifted")


def _revalidate_sources(
    data_root: Path,
    handoff: dict[str, object],
    *,
    packet_adapter: GitHubWorkPacketAdapter,
    git_runner=None,
) -> str:
    current = get_concurrency_handoff_authorization(
        Path(data_root),
        str(handoff["handoff_digest"]),
    )
    current = validate_concurrency_handoff_authorization(current)
    if current != handoff:
        _reject("concurrency claim handoff authorization drifted")
    effect_receipt_digest = _validate_source_effect(Path(data_root), handoff)
    _validate_packet_and_worktree(
        handoff,
        packet_adapter=packet_adapter,
        git_runner=git_runner,
    )
    return effect_receipt_digest


def _reserve_claim(
    data_root: Path,
    *,
    claim_id: str,
    handoff_digest: str,
) -> None:
    ledger = _load_ledger(data_root)
    if len(ledger["claims"]) >= _MAX_CLAIMS:
        _reject("concurrency claim ledger limit reached")
    if any(
        item["claim_id"] == claim_id
        or item["handoff_digest"] == handoff_digest
        for item in ledger["claims"]
    ):
        _reject("concurrency handoff claim replay is not allowed")
    ledger["claims"].append(
        {
            "claim_id": claim_id,
            "handoff_digest": handoff_digest,
            "state": "IN_PROGRESS",
            "receipt": None,
            "claim_digest": None,
        }
    )
    atomic_write_text(
        data_root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )


def _finalize_claim(
    data_root: Path,
    receipt: dict[str, object],
) -> None:
    ledger = _load_ledger(data_root)
    matches = [
        item for item in ledger["claims"]
        if item["claim_id"] == receipt["claim_id"]
    ]
    if (
        len(matches) != 1
        or matches[0]["state"] != "IN_PROGRESS"
        or matches[0]["handoff_digest"] != receipt["handoff_digest"]
    ):
        _reject("concurrency claim reservation is not current")
    validated = validate_concurrency_handoff_claim_receipt(receipt)
    updated: list[dict[str, object]] = []
    for item in ledger["claims"]:
        if item["claim_id"] == receipt["claim_id"]:
            updated.append(
                {
                    "claim_id": receipt["claim_id"],
                    "handoff_digest": receipt["handoff_digest"],
                    "state": "TERMINAL",
                    "receipt": validated,
                    "claim_digest": validated["claim_digest"],
                }
            )
        else:
            updated.append(item)
    ledger["claims"] = updated
    atomic_write_text(
        data_root / FILENAME,
        json.dumps(ledger, indent=2, sort_keys=True) + "\n",
    )


def claim_concurrency_handoff(
    data_root: Path,
    request: object,
    *,
    packet_adapter: GitHubWorkPacketAdapter,
    git_runner=None,
) -> dict[str, object]:
    """Claim one exact external handoff once, without spawning execution."""
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        _reject("concurrency claim data root is not a directory")
    if not isinstance(packet_adapter, GitHubWorkPacketAdapter):
        _reject("concurrency claim requires GitHubWorkPacketAdapter")
    normalized = validate_concurrency_handoff_claim_request(request)
    handoff = get_concurrency_handoff_authorization(
        root,
        str(normalized["handoff_digest"]),
    )
    handoff = validate_concurrency_handoff_authorization(handoff)
    _validate_request_matches_handoff(normalized, handoff)
    if (
        handoff["adapter_id"] != ADAPTER_ID
        or handoff["implementer"] != IMPLEMENTER
        or handoff["provider"] != "openai"
        or handoff["runtime"] != "chat_ssh"
    ):
        _reject("concurrency claim source adapter is not claimable")

    _revalidate_sources(
        root,
        handoff,
        packet_adapter=packet_adapter,
        git_runner=git_runner,
    )
    with data_root_write_lock(root):
        _reserve_claim(
            root,
            claim_id=str(normalized["claim_id"]),
            handoff_digest=str(handoff["handoff_digest"]),
        )

    # Reservation is durable before the external caller receives CLAIMED.
    # Recheck all mutable source facts after reservation; failure leaves an
    # IN_PROGRESS record that blocks replay and requires explicit recovery.
    effect_receipt_digest = _revalidate_sources(
        root,
        handoff,
        packet_adapter=packet_adapter,
        git_runner=git_runner,
    )
    basis = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "result": RESULT,
        "authority": AUTHORITY,
        "spawned": False,
        "completion_authority": "NONE",
        "pass_authority": "NONE",
        "release_authority": "NONE",
        "claim_id": normalized["claim_id"],
        "handoff_digest": handoff["handoff_digest"],
        "effect_id": handoff["effect_id"],
        "effect_receipt_digest": effect_receipt_digest,
        "authorization_id": handoff["authorization_id"],
        "authorization_digest": handoff["authorization_digest"],
        "replay_key": handoff["replay_key"],
        "node_id": handoff["node_id"],
        "repository": handoff["repository"],
        "issue_number": handoff["issue_number"],
        "branch": handoff["branch"],
        "head": handoff["head"],
        "workstream": handoff["workstream"],
        "worktree_path": handoff["worktree_path"],
        "worktree_identity_digest": handoff["worktree_identity_digest"],
        "implementer": handoff["implementer"],
        "adapter_id": handoff["adapter_id"],
        "provider": handoff["provider"],
        "runtime": handoff["runtime"],
        "route_id": handoff["route_id"],
        "slot_id": handoff["slot_id"],
        "worker_id": handoff["worker_id"],
        "slot_evidence_ref": handoff["slot_evidence_ref"],
    }
    receipt = {
        **basis,
        "claim_digest": _canonical_digest(basis),
    }
    receipt = validate_concurrency_handoff_claim_receipt(receipt)
    with data_root_write_lock(root):
        _finalize_claim(root, receipt)

    # Do not acknowledge CLAIMED to the external execution context if source
    # identity drifts during final publication. The terminal claim remains
    # durable/replay-blocking and requires explicit operator reconciliation.
    final_effect_digest = _revalidate_sources(
        root,
        handoff,
        packet_adapter=packet_adapter,
        git_runner=git_runner,
    )
    if final_effect_digest != effect_receipt_digest:
        _reject("concurrency claim source effect changed after finalization")
    return get_concurrency_handoff_claim(root, str(receipt["claim_digest"]))


def get_concurrency_handoff_claim(
    data_root: Path,
    claim_digest: str,
) -> dict[str, object]:
    """Return one terminal claim and rebind it to current source evidence."""
    digest = _digest(claim_digest, label="claim_digest")
    ledger = _load_ledger(Path(data_root))
    matches = [
        item for item in ledger["claims"]
        if item.get("state") == "TERMINAL"
        and item.get("claim_digest") == digest
    ]
    if len(matches) != 1:
        _reject("concurrency handoff claim is not found")
    receipt = validate_concurrency_handoff_claim_receipt(
        matches[0]["receipt"]
    )
    handoff = get_concurrency_handoff_authorization(
        Path(data_root),
        str(receipt["handoff_digest"]),
    )
    if any(
        receipt.get(key) != handoff.get(key)
        for key in (
            "handoff_digest",
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
        )
    ):
        _reject("concurrency claim receipt drifted from source handoff")
    effect_receipt_digest = _validate_source_effect(
        Path(data_root),
        handoff,
    )
    if receipt["effect_receipt_digest"] != effect_receipt_digest:
        _reject("concurrency claim source effect receipt changed")
    return dict(receipt)
