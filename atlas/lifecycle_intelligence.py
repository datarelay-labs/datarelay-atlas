"""Read-only normalization of bounded lifecycle evidence for Atlas UI."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from atlas.cursor_usage import assert_content_free, load_github_reconciliation_snapshot
from atlas.provenance import ValidationError

_HEAD = re.compile(r"^[0-9a-f]{40}$")
_CHANNELS = ("ci", "tests", "release", "browser")
_OUTCOMES = {"PASS", "FAIL", "BLOCKED"}
_MAX_EVIDENCE_BYTES = 32 * 1024

@dataclass(frozen=True)
class EvidenceState:
    state: str
    detail: str
    candidate_head: str | None = None

@dataclass(frozen=True)
class LifecycleView:
    work: EvidenceState
    ci: EvidenceState
    tests: EvidenceState
    release: EvidenceState
    browser: EvidenceState

def _unknown(channel: str) -> EvidenceState:
    if channel == "browser":
        return EvidenceState("UNKNOWN", "browser gates are configured but no execution evidence is loaded")
    label = {"ci": "CI", "tests": "test", "release": "release"}[channel]
    return EvidenceState("UNKNOWN", f"no exact-candidate {label} evidence loaded")

def _work_state(snapshot: Path, repository: str) -> EvidenceState:
    if not snapshot.is_file():
        return EvidenceState("UNKNOWN", "no trusted local lifecycle evidence")
    try:
        _, observations, metadata = load_github_reconciliation_snapshot(snapshot)
    except ValidationError:
        return EvidenceState("UNAVAILABLE", "local lifecycle evidence failed validation")
    matching = [item for item in observations if item.get("repository") == repository]
    if not matching:
        return EvidenceState("UNKNOWN", f"snapshot {metadata['observed_at']} has no project observation")
    canonical = [item for item in matching if item.get("canonical_fact") is True]
    noncanonical = [item for item in matching if item.get("canonical_fact") is not True]
    if not canonical:
        reasons = sorted({reason for item in noncanonical for reason in item.get("reasons", [])})
        suffix = f": {', '.join(reasons)}" if reasons else ""
        return EvidenceState("STALE", f"no canonical lifecycle fact{suffix}")
    priority = {"ACTIVE": 0, "BLOCKED": 1, "PAUSED": 2, "COMPLETE": 3}
    item = sorted(canonical, key=lambda value: (priority.get(str(value.get("packet_status")), 9), int(value["issue_number"])))[0]
    status, branch, head = str(item["packet_status"]), str(item["branch"]), str(item["head"])
    pr = f"PR #{item['pr_number']} {item['pr_state']}" if item.get("pr_number") else "no PR"
    detail = f"AI Work #{item['issue_number']} {status} · {branch} · {head} · {pr} · snapshot {metadata['observed_at']}"
    if noncanonical:
        detail += f" · {len(noncanonical)} noncanonical observation(s)"
    return EvidenceState("OBSERVED", detail, head)

def _load_channel_evidence(path: Path, repository: str, expected_head: str | None) -> dict[str, EvidenceState]:
    states = {channel: _unknown(channel) for channel in _CHANNELS}
    if not path.is_file():
        return states
    try:
        if path.stat().st_size > _MAX_EVIDENCE_BYTES:
            raise ValidationError("lifecycle evidence exceeds bounded size")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert_content_free(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        return {channel: EvidenceState("UNAVAILABLE", "local lifecycle evidence failed validation") for channel in _CHANNELS}
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "kind", "observed_at", "repository", "candidate_head", "channels"}:
        return {channel: EvidenceState("UNAVAILABLE", "local lifecycle evidence failed validation") for channel in _CHANNELS}
    head = raw.get("candidate_head")
    channels = raw.get("channels")
    if raw.get("schema_version") != 1 or raw.get("kind") != "atlas_lifecycle_evidence" or raw.get("repository") != repository or not isinstance(raw.get("observed_at"), str) or not raw["observed_at"] or not isinstance(head, str) or not _HEAD.fullmatch(head) or not isinstance(channels, dict) or set(channels).difference(_CHANNELS):
        return {channel: EvidenceState("UNAVAILABLE", "local lifecycle evidence failed validation") for channel in _CHANNELS}
    stale = expected_head is not None and head != expected_head
    for channel in _CHANNELS:
        item = channels.get(channel)
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != {"outcome", "detail"} or item.get("outcome") not in _OUTCOMES or not isinstance(item.get("detail"), str) or not item["detail"] or len(item["detail"]) > 256:
            states[channel] = EvidenceState("UNAVAILABLE", f"{channel} evidence failed validation")
            continue
        outcome = str(item["outcome"])
        detail = str(item["detail"])
        if stale:
            states[channel] = EvidenceState("STALE", f"{outcome} for different candidate {head}: {detail}", head)
        else:
            states[channel] = EvidenceState("OBSERVED", f"{outcome}: {detail}", head)
    return states

def lifecycle_view_payload(data_root: Path, repository: str) -> dict[str, object]:
    view = lifecycle_view(data_root, repository)
    def item(value: EvidenceState) -> dict[str, object]:
        return {"state": value.state, "detail": value.detail, "candidate_head": value.candidate_head}
    return {"repository": repository, "work": item(view.work), "ci": item(view.ci), "tests": item(view.tests), "release": item(view.release), "browser": item(view.browser)}


def lifecycle_view(data_root: Path, repository: str) -> LifecycleView:
    root = Path(data_root)
    work = _work_state(root / "github-lifecycle.json", repository)
    channels = _load_channel_evidence(root / "lifecycle-evidence.json", repository, work.candidate_head)
    return LifecycleView(work=work, ci=channels["ci"], tests=channels["tests"], release=channels["release"], browser=channels["browser"])
