"""Read-only normalization of bounded lifecycle evidence for Atlas UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from atlas.cursor_usage import SCHEMA_VERSION as GITHUB_SNAPSHOT_SCHEMA_VERSION, assert_content_free, load_github_reconciliation_snapshot
from atlas.data_lock import atomic_write_text, data_root_fd_path, data_root_write_lock
from atlas.provenance import ValidationError

_HEAD = re.compile(r"^[0-9a-f]{40}$")
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
_CHANNELS = ("ci", "tests", "release")
_HUMAN_GATES = ("surface_reconciliation", "full_user_e2e")
_OUTCOMES = {"PASS", "FAIL", "BLOCKED"}
_EVIDENCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@#?=&%+-]{0,511}$")
_MAX_EVIDENCE_BYTES = 32 * 1024
_GITHUB_LIFECYCLE_FILENAME = "github-lifecycle.json"
_GITHUB_LIFECYCLE_MAX_AGE_SECONDS = 3600
_GITHUB_LIFECYCLE_MAX_FUTURE_SKEW_SECONDS = 300

@dataclass(frozen=True)
class EvidenceState:
    state: str
    detail: str
    candidate_head: str | None = None
    evidence_ref: str | None = None

@dataclass(frozen=True)
class WorkPacketState:
    issue_number: int
    packet_status: str
    branch: str
    head: str
    pr_number: int | None
    pr_state: str
    pr_head: str | None
    canonical: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleView:
    work: EvidenceState
    work_packets: tuple[WorkPacketState, ...]
    ci: EvidenceState
    tests: EvidenceState
    release: EvidenceState
    surface_reconciliation: EvidenceState
    full_user_e2e: EvidenceState


def _github_snapshot_payload(
    observations: list[dict],
    metadata: dict,
) -> dict[str, object]:
    return {
        "schema_version": GITHUB_SNAPSHOT_SCHEMA_VERSION,
        "kind": "cursor_github_reconciliation",
        "observed_at": metadata["observed_at"],
        "repositories": list(metadata["repositories"]),
        "observations": observations,
        "summary": {
            "observed_count": metadata["observed_count"],
            "canonical_count": metadata["canonical_count"],
            "noncanonical_count": metadata["noncanonical_count"],
        },
    }


def _github_snapshot_time(value: object) -> datetime:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        raise ValidationError("GitHub lifecycle snapshot observed_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("GitHub lifecycle snapshot observed_at is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValidationError("GitHub lifecycle snapshot observed_at is invalid")
    return parsed


def publish_github_lifecycle_snapshot(
    data_root: Path,
    snapshot_path: Path,
    *,
    expected_repositories: tuple[str, ...],
    observed_now: datetime | None = None,
) -> dict[str, object]:
    """Validate and atomically publish fresh, monotonic GitHub lifecycle cache state."""
    source = Path(snapshot_path)
    if source.is_symlink() or not source.is_file():
        raise ValidationError("GitHub lifecycle snapshot input is unsafe")
    _, observations, metadata = load_github_reconciliation_snapshot(source)
    normalized_expected = tuple(sorted(set(expected_repositories)))
    if tuple(metadata["repositories"]) != normalized_expected:
        raise ValidationError(
            "GitHub lifecycle snapshot repository set does not match the registered GitHub projects"
        )
    payload = _github_snapshot_payload(observations, metadata)
    assert_content_free(payload)

    now = observed_now or datetime.now(timezone.utc)
    if now.utcoffset() is None or now.utcoffset().total_seconds() != 0:
        raise ValidationError("GitHub lifecycle publish clock must be UTC")
    incoming_time = _github_snapshot_time(metadata["observed_at"])
    if incoming_time < now - timedelta(seconds=_GITHUB_LIFECYCLE_MAX_AGE_SECONDS):
        raise ValidationError("GitHub lifecycle snapshot is too old to publish")
    if incoming_time > now + timedelta(seconds=_GITHUB_LIFECYCLE_MAX_FUTURE_SKEW_SECONDS):
        raise ValidationError("GitHub lifecycle snapshot is too far in the future")

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    root = Path(data_root)
    with data_root_write_lock(root) as root_fd:
        bound_root = data_root_fd_path(root_fd)
        destination = bound_root / _GITHUB_LIFECYCLE_FILENAME
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValidationError("GitHub lifecycle destination is unsafe")
        if destination.exists():
            _, current_observations, current_metadata = load_github_reconciliation_snapshot(
                destination
            )
            current_time = _github_snapshot_time(current_metadata["observed_at"])
            if incoming_time < current_time:
                raise ValidationError(
                    "GitHub lifecycle snapshot is older than the current cache"
                )
            if incoming_time == current_time:
                current_payload = _github_snapshot_payload(
                    current_observations, current_metadata
                )
                if payload != current_payload:
                    raise ValidationError(
                        "GitHub lifecycle snapshot conflicts at the current observed_at"
                    )
                return {
                    "state": "UNCHANGED",
                    "observed_at": metadata["observed_at"],
                    "repository_count": len(metadata["repositories"]),
                    "observed_count": metadata["observed_count"],
                    "canonical_count": metadata["canonical_count"],
                    "noncanonical_count": metadata["noncanonical_count"],
                    "authority": "DERIVED_READ_ONLY",
                }
        atomic_write_text(destination, encoded)
        destination.chmod(0o600)
    return {
        "state": "PUBLISHED",
        "observed_at": metadata["observed_at"],
        "repository_count": len(metadata["repositories"]),
        "observed_count": metadata["observed_count"],
        "canonical_count": metadata["canonical_count"],
        "noncanonical_count": metadata["noncanonical_count"],
        "authority": "DERIVED_READ_ONLY",
    }

def _unknown(channel: str) -> EvidenceState:
    label = {"ci": "CI", "tests": "test", "release": "release"}[channel]
    return EvidenceState("UNKNOWN", f"no exact-candidate {label} evidence loaded")

def _human_unknown(gate: str) -> EvidenceState:
    label = {
        "surface_reconciliation": "Surface Reconciliation",
        "full_user_e2e": "Full User E2E",
    }[gate]
    return EvidenceState(
        "UNKNOWN",
        f"no {label} configuration or execution evidence loaded",
    )

def _work_state(snapshot: Path, repository: str) -> tuple[EvidenceState, frozenset[str], tuple[WorkPacketState, ...]]:
    if not snapshot.is_file():
        return EvidenceState("UNKNOWN", "no trusted local lifecycle evidence"), frozenset(), ()
    try:
        _, observations, metadata, source_stat = load_github_reconciliation_snapshot(
            snapshot,
            include_file_stat=True,
        )
        modified_at = datetime.fromtimestamp(source_stat.st_mtime, timezone.utc)
    except (OSError, ValidationError):
        return EvidenceState("UNAVAILABLE", "local lifecycle evidence failed validation"), frozenset(), ()
    now = datetime.now(timezone.utc)
    if modified_at > now + timedelta(seconds=_GITHUB_LIFECYCLE_MAX_FUTURE_SKEW_SECONDS):
        return EvidenceState("UNAVAILABLE", "local lifecycle cache timestamp is in the future"), frozenset(), ()
    if modified_at < now - timedelta(seconds=_GITHUB_LIFECYCLE_MAX_AGE_SECONDS):
        return EvidenceState("STALE", "local lifecycle cache expired"), frozenset(), ()
    matching = [item for item in observations if item.get("repository") == repository]
    if not matching:
        return EvidenceState("UNKNOWN", f"snapshot {metadata['observed_at']} has no project observation"), frozenset(), ()
    canonical = [item for item in matching if item.get("canonical_fact") is True]
    noncanonical = [item for item in matching if item.get("canonical_fact") is not True]
    packets = tuple(
        WorkPacketState(
            issue_number=int(item["issue_number"]),
            packet_status=str(item["packet_status"]),
            branch=str(item["branch"]),
            head=str(item["head"]),
            pr_number=int(item["pr_number"]) if item.get("pr_number") else None,
            pr_state=str(item.get("pr_state") or "NONE"),
            pr_head=str(item["pr_head"]) if item.get("pr_head") else None,
            canonical=item.get("canonical_fact") is True,
            reasons=tuple(sorted(str(reason) for reason in item.get("reasons", []))),
        )
        for item in sorted(
            matching,
            key=lambda value: (
                value.get("canonical_fact") is not True,
                int(value["issue_number"]),
            ),
        )
    )
    if not canonical:
        reasons = sorted({reason for item in noncanonical for reason in item.get("reasons", [])})
        suffix = f": {', '.join(reasons)}" if reasons else ""
        return EvidenceState("STALE", f"no canonical lifecycle fact{suffix}"), frozenset(), packets

    priority = {"ACTIVE": 0, "BLOCKED": 1, "PAUSED": 2, "COMPLETE": 3}
    canonical.sort(key=lambda value: (priority.get(str(value.get("packet_status")), 9), int(value["issue_number"])))
    details: list[str] = []
    heads: set[str] = set()
    for item in canonical:
        status, branch, head = str(item["packet_status"]), str(item["branch"]), str(item["head"])
        heads.add(head)
        if item.get("pr_number"):
            pr = f"PR #{item['pr_number']} {item['pr_state']}"
            if item.get("pr_head"):
                pr += f" @ {item['pr_head']}"
        else:
            pr = "no PR"
        details.append(f"AI Work #{item['issue_number']} {status} · {branch} · {head} · {pr}")
    detail = " ; ".join(details) + f" · snapshot {metadata['observed_at']}"
    if noncanonical:
        detail += f" · {len(noncanonical)} noncanonical observation(s)"
    unique_head = next(iter(heads)) if len(heads) == 1 else None
    return EvidenceState("OBSERVED", detail, unique_head), frozenset(heads), packets

def _invalid_all() -> tuple[dict[str, EvidenceState], dict[str, EvidenceState]]:
    channels = {channel: EvidenceState("UNAVAILABLE", "local lifecycle evidence failed validation") for channel in _CHANNELS}
    gates = {gate: EvidenceState("UNAVAILABLE", "local lifecycle evidence failed validation") for gate in _HUMAN_GATES}
    return channels, gates

def _gate_state(
    gate: str,
    item: object,
    head: str,
    expected_heads: frozenset[str],
) -> EvidenceState:
    label = {
        "surface_reconciliation": "Surface Reconciliation",
        "full_user_e2e": "Full User E2E",
    }[gate]
    if not isinstance(item, dict) or set(item) != {"required", "configured", "outcome", "detail", "evidence_ref"}:
        return EvidenceState("UNAVAILABLE", f"{label} evidence failed validation")
    required, configured = item.get("required"), item.get("configured")
    outcome, detail, evidence_ref = item.get("outcome"), item.get("detail"), item.get("evidence_ref")
    if not isinstance(required, bool) or not isinstance(configured, bool):
        return EvidenceState("UNAVAILABLE", f"{label} evidence failed validation")
    if outcome is not None and outcome not in _OUTCOMES:
        return EvidenceState("UNAVAILABLE", f"{label} evidence failed validation")
    if evidence_ref is not None and (not isinstance(evidence_ref, str) or _EVIDENCE_REF.fullmatch(evidence_ref) is None):
        return EvidenceState("UNAVAILABLE", f"{label} evidence failed validation")
    if outcome is None:
        if detail is not None or evidence_ref is not None:
            return EvidenceState("UNAVAILABLE", f"{label} evidence failed validation")
    elif not isinstance(detail, str) or not detail or len(detail) > 256 or not configured:
        return EvidenceState("UNAVAILABLE", f"{label} evidence failed validation")

    facts = f"required={str(required).lower()} · configured={str(configured).lower()}"
    if not expected_heads:
        return EvidenceState("UNKNOWN", f"{facts} · candidate {head} cannot be bound to a current canonical Work Packet head", head)
    if head not in expected_heads:
        suffix = f" · {outcome}: {detail}" if outcome else ""
        return EvidenceState("STALE_DIFFERENT_HEAD", f"{facts} · evidence is for different candidate {head}{suffix}", head, evidence_ref)
    if outcome is not None:
        return EvidenceState(f"EXECUTION_{outcome}", f"{facts} · {outcome}: {detail}", head, evidence_ref)
    if not required:
        return EvidenceState("NOT_REQUIRED", facts, head)
    if not configured:
        return EvidenceState("REQUIRED_UNCONFIGURED", facts, head)
    return EvidenceState("CONTRACT_CONFIGURED", f"{facts} · no execution evidence loaded", head)

def _load_lifecycle_evidence(
    path: Path,
    repository: str,
    expected_heads: frozenset[str],
) -> tuple[dict[str, EvidenceState], dict[str, EvidenceState]]:
    states = {channel: _unknown(channel) for channel in _CHANNELS}
    gates = {gate: _human_unknown(gate) for gate in _HUMAN_GATES}
    if not path.is_file():
        return states, gates
    try:
        if path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise ValidationError("lifecycle evidence exceeds bounded size")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert_content_free(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        return _invalid_all()
    exact_keys = {"schema_version", "kind", "observed_at", "repository", "candidate_head", "channels", "human_equivalent_user_tests"}
    if not isinstance(raw, dict) or set(raw) != exact_keys:
        return _invalid_all()
    head, channels = raw.get("candidate_head"), raw.get("channels")
    observed_at, human = raw.get("observed_at"), raw.get("human_equivalent_user_tests")
    if (
        raw.get("schema_version") != 2
        or raw.get("kind") != "atlas_lifecycle_evidence"
        or raw.get("repository") != repository
        or not isinstance(observed_at, str)
        or _UTC.fullmatch(observed_at) is None
        or not isinstance(head, str)
        or _HEAD.fullmatch(head) is None
        or not isinstance(channels, dict)
        or set(channels).difference(_CHANNELS)
        or not isinstance(human, dict)
        or set(human).difference(_HUMAN_GATES)
    ):
        return _invalid_all()

    for channel in _CHANNELS:
        item = channels.get(channel)
        if item is None:
            continue
        if (
            not isinstance(item, dict)
            or set(item) != {"outcome", "detail", "evidence_ref"}
            or item.get("outcome") not in _OUTCOMES
            or not isinstance(item.get("detail"), str)
            or not item["detail"]
            or len(item["detail"]) > 256
            or not isinstance(item.get("evidence_ref"), str)
            or _EVIDENCE_REF.fullmatch(item["evidence_ref"]) is None
        ):
            states[channel] = EvidenceState("UNAVAILABLE", f"{channel} evidence failed validation")
            continue
        outcome, detail, evidence_ref = str(item["outcome"]), str(item["detail"]), str(item["evidence_ref"])
        if not expected_heads:
            states[channel] = EvidenceState("UNKNOWN", f"{outcome} for unbound candidate {head}: {detail}", head, evidence_ref)
        elif head not in expected_heads:
            states[channel] = EvidenceState("STALE", f"{outcome} for different candidate {head}: {detail}", head, evidence_ref)
        else:
            states[channel] = EvidenceState("OBSERVED", f"{outcome}: {detail}", head, evidence_ref)

    for gate in _HUMAN_GATES:
        item = human.get(gate)
        if item is not None:
            gates[gate] = _gate_state(gate, item, head, expected_heads)
    return states, gates

def lifecycle_view_payload(data_root: Path, repository: str) -> dict[str, object]:
    view = lifecycle_view(data_root, repository)
    def item(value: EvidenceState) -> dict[str, object]:
        return {"state": value.state, "detail": value.detail, "candidate_head": value.candidate_head, "evidence_ref": value.evidence_ref}
    return {
        "repository": repository,
        "work": item(view.work),
        "work_packets": [
            {
                "issue_number": packet.issue_number,
                "packet_status": packet.packet_status,
                "branch": packet.branch,
                "head": packet.head,
                "pr_number": packet.pr_number,
                "pr_state": packet.pr_state,
                "pr_head": packet.pr_head,
                "canonical": packet.canonical,
                "reasons": list(packet.reasons),
            }
            for packet in view.work_packets
        ],
        "ci": item(view.ci),
        "tests": item(view.tests),
        "release": item(view.release),
        "surface_reconciliation": item(view.surface_reconciliation),
        "full_user_e2e": item(view.full_user_e2e),
    }

def lifecycle_view(data_root: Path, repository: str) -> LifecycleView:
    root = Path(data_root)
    work, expected_heads, work_packets = _work_state(root / "github-lifecycle.json", repository)
    channels, gates = _load_lifecycle_evidence(root / "lifecycle-evidence.json", repository, expected_heads)
    return LifecycleView(
        work=work,
        work_packets=work_packets,
        ci=channels["ci"],
        tests=channels["tests"],
        release=channels["release"],
        surface_reconciliation=gates["surface_reconciliation"],
        full_user_e2e=gates["full_user_e2e"],
    )
