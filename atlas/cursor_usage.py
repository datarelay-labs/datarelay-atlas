"""Read-only Cursor worker inventory and usage-event summary (ADR-0014).

Resident persist sessions are not token spend. Process command text is
classified and discarded. Scheduler state is local runtime evidence only
and is never inference. This module does not signal, stop, or resume a
session and does not record hook receipts.
"""

from __future__ import annotations

import csv
import io
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Iterable, Mapping

from atlas.provenance import ValidationError
from atlas.provider_capacity import build_provider_capacity_input
from atlas.secrets import contains_unsafe_secret
from atlas.work_controller import (
    GitHubWorkPacketAdapter,
    GitRunner,
    PersistSession,
    default_list_persist_sessions,
    normalize_github_repository,
)

SCHEMA_VERSION = 1
MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_CSV_ROWS = 10000
MAX_LABEL_CHARS = 128
BURST_WINDOW_SEC = 600
HEAVY_EVENT_TOKENS = 5_000_000
EXTREME_EVENT_TOKENS = 10_000_000
MAX_TOKEN_DIGITS = 18
MAX_PROCESS_SCAN = 20000
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SNAPSHOT_WORKERS = 100
MAX_GITHUB_SNAPSHOT_REPOSITORIES = 100
MAX_GITHUB_SNAPSHOT_OBSERVATIONS = 2000

WORKER_STATES = frozenset(
    {
        "RUNNING_AUTHORIZED",
        "IDLE_REUSABLE",
        "DUPLICATE_WORKTREE",
        "TERMINAL_WORK_SURVIVOR",
        "ORPHAN_OR_UNKNOWN",
    }
)
RECOMMENDATIONS = frozenset(
    {
        "CONTINUE",
        "CHECKPOINT",
        "SUMMARIZE",
        "CLEAR_RECOMMENDED",
        "YIELD",
        "HUMAN_REQUIRED",
        "UNKNOWN",
    }
)
PACKET_STATUSES = frozenset({"ACTIVE", "PAUSED", "BLOCKED", "COMPLETE"})

_REQUIRED_COLUMNS = (
    "Date",
    "Input (w/ Cache Write)",
    "Input (w/o Cache Write)",
    "Cache Read",
    "Output Tokens",
    "Total Tokens",
)
_OPTIONAL_COLUMNS = (
    "Kind",
    "Model",
    "Max Mode",
    "Cost",
    "Cost to you",
    "Cloud Agent ID",
    "Automation ID",
)
_ALLOWED_COLUMNS = frozenset(_REQUIRED_COLUMNS + _OPTIONAL_COLUMNS)
_PACKET_KEYS = frozenset({"repository", "branch", "status", "head", "issue_number"})
_SNAPSHOT_TOP_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "host_id",
        "observed_at",
        "workers",
        "advisor",
        "reconciliation",
        "reconciliation_summary",
        "snapshots",
    }
)
_GITHUB_SNAPSHOT_TOP_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "observed_at",
        "repositories",
        "observations",
        "summary",
    }
)
_GITHUB_OBSERVATION_KEYS = frozenset(
    {
        "repository",
        "issue_number",
        "issue_state",
        "issue_updated_at",
        "author_trust",
        "packet_status",
        "branch",
        "head",
        "pr_number",
        "pr_state",
        "pr_head",
        "canonical_fact",
        "reasons",
    }
)
_GITHUB_OBSERVATION_REASONS = frozenset(
    {
        "AUTHOR_UNTRUSTED",
        "PACKET_METADATA_INVALID",
        "TARGET_REPO_MISMATCH",
        "BRANCH_MISSING_OR_INVALID",
        "NONCANONICAL_STATUS",
        "HEAD_MISSING_OR_INVALID",
        "ISSUE_STATE_STATUS_CONFLICT",
        "PR_AMBIGUOUS",
        "PR_HEAD_MISMATCH",
        "PACKET_PR_STATE_CONFLICT",
    }
)
_GITHUB_PR_STATES = frozenset(
    {"NONE", "UNKNOWN", "OPEN", "CLOSED", "MERGED", "AMBIGUOUS"}
)
_SNAPSHOT_WORKER_KEYS = frozenset(
    {
        "session_id",
        "workspace",
        "resident",
        "attachment",
        "inference_activity",
        "runtime",
        "state",
        "repository",
        "branch",
        "head",
        "dirty",
        "packet_status",
        "host_id",
    }
)
_TOKEN_RE = r"^(0|[1-9][0-9]*)$"
_COST_RE = r"^(0|[1-9][0-9]*)(\.[0-9]+)?$"
_HEAD_RE_TEXT = r"^[0-9a-f]{40}$"
_RESTORE_FLAG = "--cursor-persist-restore"
_NODE_CA_FLAG = "--use-system-ca"
_RESTORE_TOKEN_RE_TEXT = r"^[0-9a-f]{32}$"
_CURSOR_SESSION_ID_RE_TEXT = (
    r"^cursor-(?:[A-Za-z0-9]+-)+[0-9a-f]{10}-[0-9a-f]-[0-9a-f]{6}$"
)
_HOST_ID_RE_TEXT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"

_TOKEN_PATTERN = re.compile(_TOKEN_RE)
_COST_PATTERN = re.compile(_COST_RE)
_HEAD_PATTERN = re.compile(_HEAD_RE_TEXT)
_RESTORE_TOKEN_PATTERN = re.compile(_RESTORE_TOKEN_RE_TEXT)
_CURSOR_SESSION_ID_PATTERN = re.compile(_CURSOR_SESSION_ID_RE_TEXT)
_HOST_ID_PATTERN = re.compile(_HOST_ID_RE_TEXT)
_MAX_MODE_TRUE = frozenset({"yes", "true"})
_MAX_MODE_FALSE = frozenset({"no", "false"})


@dataclass(frozen=True)
class UsageEvent:
    """One provider usage row. Absent optional fields stay None."""

    timestamp: str
    kind: str | None
    model: str | None
    max_mode: bool | None
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    total_tokens: int
    cost: str | None
    cost_to_you: str | None
    cloud_agent_id: str | None
    automation_id: str | None


@dataclass(frozen=True)
class ProcessFact:
    """Content-free process observation. Command text is already discarded.

    ``runtime`` is busy, quiescent, or unknown. It is not inference.
    """

    session_id: str | None
    workspace: str | None
    ambiguous: bool
    runtime: str


@dataclass(frozen=True)
class GitIdentity:
    repository: str
    branch: str
    head: str
    dirty: bool


@dataclass(frozen=True)
class PacketFact:
    repository: str
    branch: str
    status: str
    head: str
    issue_number: int | None = None


def _reject(message: str) -> None:
    raise ValidationError(message)


def _bounded_cell(value: str, *, label: str, row_number: int) -> str:
    text = value.strip()
    if len(text) > MAX_LABEL_CHARS or any(ord(char) < 32 or ord(char) == 127 for char in text):
        _reject(f"row {row_number}: {label} is not a bounded provider label")
    return text


def _optional_label(value: str | None, *, label: str, row_number: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return _bounded_cell(text, label=label, row_number=row_number)


def _require_token_count(value: str | None, *, label: str, row_number: int) -> int:
    text = "" if value is None else value.strip()
    if not _TOKEN_PATTERN.fullmatch(text) or len(text) > MAX_TOKEN_DIGITS:
        _reject(f"row {row_number}: {label} is not a bounded non-negative integer")
    try:
        return int(text)
    except ValueError as exc:
        raise ValidationError(
            f"row {row_number}: {label} is not a bounded non-negative integer"
        ) from exc


def _optional_cost(value: str | None, *, label: str, row_number: int) -> str | None:
    """Preserve bounded provider cost labels; cost never drives control."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text in {"Free", "Included", "-"}:
        return text
    if not _COST_PATTERN.fullmatch(text):
        _reject(
            f"row {row_number}: {label} is not a supported provider cost label"
        )
    return text


def _parse_timestamp(value: str | None, *, row_number: int) -> str:
    text = "" if value is None else value.strip()
    if "T" not in text:
        _reject(f"row {row_number}: timestamp must be timezone-aware ISO-8601")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        _reject(f"row {row_number}: timestamp must be timezone-aware ISO-8601")
    if parsed.tzinfo is None:
        _reject(f"row {row_number}: timestamp must be timezone-aware ISO-8601")
    utc = parsed.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"


def _parse_max_mode(value: str | None, *, row_number: int) -> bool | None:
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text in _MAX_MODE_TRUE:
        return True
    if text in _MAX_MODE_FALSE:
        return False
    _reject(f"row {row_number}: Max Mode is not yes/no")


def parse_usage_csv(path: Path) -> list[UsageEvent]:
    """Parse a Cursor Usage Events CSV. Unknown columns and bad types fail."""
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ValidationError("usage CSV is not readable") from exc
    if size > MAX_CSV_BYTES:
        _reject("usage CSV exceeds the bounded import size")
    try:
        raw_text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValidationError("usage CSV is not readable UTF-8") from exc
    if "\x00" in raw_text:
        _reject("usage CSV contains NUL bytes")
    reader = csv.DictReader(io.StringIO(raw_text))
    if reader.fieldnames is None:
        _reject("usage CSV is missing a header row")
    headers = [name.strip() if name is not None else "" for name in reader.fieldnames]
    if any(not name for name in headers) or len(headers) != len(set(headers)):
        _reject("usage CSV header is missing or duplicated")
    unknown = sorted(set(headers).difference(_ALLOWED_COLUMNS))
    if unknown:
        _reject("unsupported usage column: " + ", ".join(unknown))
    missing = [name for name in _REQUIRED_COLUMNS if name not in headers]
    if missing:
        _reject("usage CSV missing required column: " + ", ".join(missing))

    events: list[UsageEvent] = []
    for row_number, row in enumerate(reader, start=1):
        if row_number > MAX_CSV_ROWS:
            _reject("usage CSV exceeds the bounded row count")
        if row is None or None in row:
            _reject(f"row {row_number}: column count does not match the header")
        normalized = {
            (key.strip() if key is not None else ""): value for key, value in row.items()
        }
        token_columns = (
            "Input (w/ Cache Write)",
            "Input (w/o Cache Write)",
            "Cache Read",
            "Output Tokens",
            "Total Tokens",
        )
        raw_tokens = [normalized.get(name) for name in token_columns]
        all_blank = all(value is None or not value.strip() for value in raw_tokens)
        if all_blank:
            kind_label = (normalized.get("Kind") or "").strip()
            cost_label = (normalized.get("Cost") or "").strip()
            if kind_label != "Included" or cost_label != "Free":
                _reject(
                    f"row {row_number}: blank token bundle is not an Included/Free event"
                )
            cache_write = fresh_input = cache_read = output = total = 0
        else:
            cache_write = _require_token_count(
                normalized.get("Input (w/ Cache Write)"),
                label="cache_write_tokens",
                row_number=row_number,
            )
            fresh_input = _require_token_count(
                normalized.get("Input (w/o Cache Write)"),
                label="input_tokens",
                row_number=row_number,
            )
            cache_read = _require_token_count(
                normalized.get("Cache Read"),
                label="cache_read_tokens",
                row_number=row_number,
            )
            output = _require_token_count(
                normalized.get("Output Tokens"),
                label="output_tokens",
                row_number=row_number,
            )
            total = _require_token_count(
                normalized.get("Total Tokens"),
                label="total_tokens",
                row_number=row_number,
            )
        if total != fresh_input + cache_write + cache_read + output:
            _reject(f"row {row_number}: total_tokens does not match the token parts")
        events.append(
            UsageEvent(
                timestamp=_parse_timestamp(normalized.get("Date"), row_number=row_number),
                kind=_optional_label(normalized.get("Kind"), label="kind", row_number=row_number),
                model=_optional_label(
                    normalized.get("Model"), label="model", row_number=row_number
                ),
                max_mode=_parse_max_mode(normalized.get("Max Mode"), row_number=row_number),
                input_tokens=fresh_input,
                cache_write_tokens=cache_write,
                cache_read_tokens=cache_read,
                output_tokens=output,
                total_tokens=total,
                cost=_optional_cost(normalized.get("Cost"), label="cost", row_number=row_number),
                cost_to_you=_optional_cost(
                    normalized.get("Cost to you"),
                    label="cost_to_you",
                    row_number=row_number,
                ),
                cloud_agent_id=_optional_label(
                    normalized.get("Cloud Agent ID"),
                    label="cloud_agent_id",
                    row_number=row_number,
                ),
                automation_id=_optional_label(
                    normalized.get("Automation ID"),
                    label="automation_id",
                    row_number=row_number,
                ),
            )
        )
    return events


def _share(part: int, whole: int) -> str | None:
    if whole <= 0:
        return None
    value = (Decimal(part) / Decimal(whole)).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    return f"{value:.6f}"


def _percentile(sorted_values: list[int], percent: int) -> int | None:
    if not sorted_values:
        return None
    count = len(sorted_values)
    rank = (percent * count + 99) // 100
    rank = min(max(rank, 1), count)
    return sorted_values[rank - 1]


def _parse_event_time(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def summarize_usage(events: Iterable[UsageEvent]) -> dict:
    """Content-free aggregates. Cache ratio is descriptive only."""
    rows = list(events)
    input_tokens = sum(item.input_tokens for item in rows)
    cache_write = sum(item.cache_write_tokens for item in rows)
    cache_read = sum(item.cache_read_tokens for item in rows)
    output_tokens = sum(item.output_tokens for item in rows)
    total_tokens = sum(item.total_tokens for item in rows)
    ordered = sorted(rows, key=lambda item: (item.timestamp, item.total_tokens))
    totals = sorted(item.total_tokens for item in rows)
    heavy_5 = [item for item in rows if item.total_tokens >= HEAVY_EVENT_TOKENS]
    heavy_10 = [item for item in rows if item.total_tokens >= EXTREME_EVENT_TOKENS]
    mix: dict[str, list[int]] = {}
    for item in rows:
        label = item.model if item.model else "UNKNOWN"
        bucket = mix.setdefault(label, [0, 0])
        bucket[0] += 1
        bucket[1] += item.total_tokens

    max_burst: dict | None = None
    burst_windows_ge_5m = 0
    if ordered:
        stamps = [_parse_event_time(item.timestamp) for item in ordered]
        right = 0
        running = 0
        for left, start in enumerate(stamps):
            limit = start.timestamp() + BURST_WINDOW_SEC
            while right < len(ordered) and stamps[right].timestamp() < limit:
                running += ordered[right].total_tokens
                right += 1
            window_events = right - left
            if running >= HEAVY_EVENT_TOKENS:
                burst_windows_ge_5m += 1
            if max_burst is None or running > max_burst["total_tokens"]:
                max_burst = {
                    "window_sec": BURST_WINDOW_SEC,
                    "start": ordered[left].timestamp,
                    "end": ordered[right - 1].timestamp,
                    "event_count": window_events,
                    "total_tokens": running,
                }
            running -= ordered[left].total_tokens

    return {
        "event_count": len(rows),
        "input_tokens": input_tokens,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "max_event_tokens": max(totals) if totals else 0,
        "percentiles": {
            "p50": _percentile(totals, 50),
            "p95": _percentile(totals, 95),
            "p99": _percentile(totals, 99),
        },
        "model_mix": [
            {"model": label, "event_count": mix[label][0], "total_tokens": mix[label][1]}
            for label in sorted(mix)
        ],
        "heavy_events": {
            "ge_5000000": {
                "count": len(heavy_5),
                "total_tokens": sum(item.total_tokens for item in heavy_5),
                "token_share": _share(
                    sum(item.total_tokens for item in heavy_5), total_tokens
                ),
            },
            "ge_10000000": {
                "count": len(heavy_10),
                "total_tokens": sum(item.total_tokens for item in heavy_10),
                "token_share": _share(
                    sum(item.total_tokens for item in heavy_10), total_tokens
                ),
            },
        },
        "max_burst": max_burst,
        "burst_windows_ge_5000000": burst_windows_ge_5m,
        "cache_read_ratio": _share(cache_read, total_tokens),
    }


def cursor_capacity_input(events: Iterable[UsageEvent]) -> dict:
    """Normalize authoritative Cursor CSV usage into provider-neutral evidence.

    The Usage Events CSV proves aggregate usage facts only. Remaining quota,
    reset timing, pool state, and active inference WIP therefore stay UNKNOWN.
    """
    rows = list(events)
    summary = summarize_usage(rows)
    timestamps = sorted(item.timestamp for item in rows)
    return build_provider_capacity_input(
        provider="cursor",
        scope="ACCOUNT_AGGREGATE",
        source_kind="usage_events_csv",
        window_start=timestamps[0] if timestamps else None,
        window_end=timestamps[-1] if timestamps else None,
        event_count=summary["event_count"],
        total_tokens=summary["total_tokens"],
    )


def canonical_workspace(path: str | None) -> str | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None
    try:
        return str(candidate.resolve())
    except OSError:
        return None


def _attachment(status: str) -> str:
    text = status.strip().lower()
    if text.startswith("attached"):
        return "attached"
    if text.startswith("detached"):
        return "detached"
    return "unknown"


def _packet_index(facts: Iterable[PacketFact]) -> dict[tuple[str, str, str], str]:
    """Index packet facts by repository, branch, and exact HEAD."""
    index: dict[tuple[str, str, str], str] = {}
    for fact in facts:
        if fact.status not in PACKET_STATUSES:
            _reject("packet fact status is not an allowlisted Work Packet status")
        if not _HEAD_PATTERN.fullmatch(fact.head):
            _reject("packet fact head is not an exact commit")
        try:
            repository = normalize_github_repository(fact.repository)
        except ValidationError as exc:
            raise ValidationError("packet fact repository is invalid") from exc
        key = (repository, fact.branch, fact.head)
        if key in index:
            index[key] = "AMBIGUOUS"
        else:
            index[key] = fact.status
    return index


def _runtime_value(facts: list[ProcessFact], *, ambiguous: bool) -> str:
    """Local scheduler observation. Never an inference activity."""
    if ambiguous or not facts:
        return "unknown"
    observed = {item.runtime for item in facts}
    if not observed <= {"busy", "quiescent", "unknown"}:
        _reject("process runtime is not allowlisted")
    if "busy" in observed:
        return "busy"
    if observed == {"quiescent"}:
        return "quiescent"
    return "unknown"


def _worker_row(
    *,
    session_id: str | None,
    workspace: str | None,
    resident: bool,
    attachment: str,
    runtime: str,
    state: str,
    identity: GitIdentity | None,
    packet_status: str | None,
) -> dict:
    if state not in WORKER_STATES:
        _reject("worker state is not allowlisted")
    if runtime not in {"busy", "quiescent", "unknown"}:
        _reject("process runtime is not allowlisted")
    return {
        "session_id": session_id,
        "workspace": workspace,
        "resident": resident,
        "attachment": attachment,
        "inference_activity": "UNKNOWN",
        "runtime": runtime,
        "state": state,
        "repository": None if identity is None else identity.repository,
        "branch": None if identity is None else identity.branch,
        "head": None if identity is None else identity.head,
        "dirty": None if identity is None else identity.dirty,
        "packet_status": packet_status,
    }


def classify_workers(
    sessions: Iterable[PersistSession],
    processes: Iterable[ProcessFact],
    identities: Mapping[str, GitIdentity | None],
    packet_facts: Iterable[PacketFact] | None = None,
) -> list[dict]:
    """Classify resident workers. Ambiguous evidence becomes ORPHAN_OR_UNKNOWN."""
    prepared: list[tuple[PersistSession, str | None]] = [
        (session, canonical_workspace(session.workspace)) for session in sessions
    ]
    counts: dict[str, int] = {}
    for _session, workspace in prepared:
        if workspace is not None:
            counts[workspace] = counts.get(workspace, 0) + 1
    packet_list = list(packet_facts or [])
    packets = _packet_index(packet_list)
    packet_branches = {
        (normalize_github_repository(fact.repository), fact.branch)
        for fact in packet_list
    }
    by_session: dict[str, list[ProcessFact]] = {}
    unbound: list[ProcessFact] = []
    session_ids = {session.session_id for session, _workspace in prepared if session.session_id}

    def bind(session_id: str, fact: ProcessFact, workspace: str | None) -> None:
        session_workspace = next(
            (
                item_workspace
                for item, item_workspace in prepared
                if item.session_id == session_id
            ),
            None,
        )
        if (
            fact.workspace is not None
            and session_workspace is not None
            and fact.workspace != session_workspace
        ):
            by_session.setdefault(session_id, []).append(
                ProcessFact(
                    session_id=fact.session_id,
                    workspace=fact.workspace,
                    ambiguous=True,
                    runtime="unknown",
                )
            )
            return
        del workspace
        by_session.setdefault(session_id, []).append(fact)

    for fact in processes:
        if fact.session_id and fact.session_id in session_ids:
            bind(fact.session_id, fact, fact.workspace)
            continue
        if fact.session_id:
            unbound.append(fact)
            continue
        matches = [
            session.session_id
            for session, workspace in prepared
            if workspace is not None and workspace == fact.workspace
        ]
        if len(matches) == 1 and fact.workspace is not None:
            bind(matches[0], fact, fact.workspace)
        else:
            unbound.append(fact)

    workers: list[dict] = []
    for session, workspace in prepared:
        facts = by_session.get(session.session_id, [])
        ambiguous_process = any(item.ambiguous for item in facts)
        runtime = _runtime_value(facts, ambiguous=ambiguous_process)
        identity = identities.get(workspace) if workspace is not None else None
        if not isinstance(identity, GitIdentity):
            identity = None
        packet_status = None
        packet_ambiguous = False
        if identity is not None:
            observed = packets.get((identity.repository, identity.branch, identity.head))
            if observed == "AMBIGUOUS":
                packet_ambiguous = True
            elif observed is not None:
                packet_status = observed
            elif (identity.repository, identity.branch) in packet_branches:
                # A canonical packet exists for this repo/branch but not this
                # worker HEAD. Treat stale exact-head evidence as uncertainty,
                # never as an implicitly reusable idle worker.
                packet_ambiguous = True
        duplicate = workspace is not None and counts.get(workspace, 0) > 1
        if not session.session_id or workspace is None:
            state = "ORPHAN_OR_UNKNOWN"
            runtime = "unknown"
            packet_status = None
        elif duplicate:
            state = "DUPLICATE_WORKTREE"
        elif identity is None or ambiguous_process or packet_ambiguous or not facts:
            state = "ORPHAN_OR_UNKNOWN"
            if packet_ambiguous or ambiguous_process or not facts:
                runtime = "unknown"
            if packet_ambiguous:
                packet_status = None
        elif packet_status == "COMPLETE":
            state = "TERMINAL_WORK_SURVIVOR"
        else:
            state = "IDLE_REUSABLE"
        workers.append(
            _worker_row(
                session_id=session.session_id or None,
                workspace=workspace,
                resident=True,
                attachment=_attachment(session.status),
                runtime=runtime,
                state=state,
                identity=identity,
                packet_status=packet_status,
            )
        )

    for fact in unbound:
        workers.append(
            _worker_row(
                session_id=fact.session_id,
                workspace=fact.workspace,
                resident=False,
                attachment="unknown",
                runtime="unknown" if fact.ambiguous else fact.runtime,
                state="ORPHAN_OR_UNKNOWN",
                identity=None,
                packet_status=None,
            )
        )
    workers.sort(key=lambda item: (item["workspace"] or "", item["session_id"] or ""))
    return workers


def recommend(workers: Iterable[dict], *, current_observed: bool) -> dict:
    """Current-state warning only. Historical CSV metrics do not select an action.

    Phase 0 has no native context or quota telemetry. Heavy-event and burst
    figures stay in the usage summary. This function emits CONTINUE or
    HUMAN_REQUIRED from observed worker state, or UNKNOWN when that state
    was not observed.
    """
    if not current_observed:
        recommendation = "UNKNOWN"
        reasons = ["NO_TRUSTWORTHY_CURRENT_STATE"]
    else:
        rows = list(workers)
        reasons = []
        states = {item["state"] for item in rows}
        if "ORPHAN_OR_UNKNOWN" in states:
            reasons.append("ORPHAN_OR_UNKNOWN_PRESENT")
        if "DUPLICATE_WORKTREE" in states:
            reasons.append("DUPLICATE_WORKTREE_PRESENT")
        if "TERMINAL_WORK_SURVIVOR" in states:
            reasons.append("TERMINAL_WORK_SURVIVOR_PRESENT")
        recommendation = "HUMAN_REQUIRED" if reasons else "CONTINUE"
    if recommendation not in RECOMMENDATIONS:
        _reject("recommendation is not allowlisted")
    return {"recommendation": recommendation, "reasons": sorted(set(reasons))}


def _apply_reconciliation_uncertainty(
    workers: list[dict], observations: Iterable[dict]
) -> None:
    """Fail closed for branch-specific or trusted open repository-wide ambiguity."""
    uncertain: set[tuple[str, str]] = set()
    repository_uncertain: set[str] = set()
    for item in observations:
        if not isinstance(item, dict) or item.get("canonical_fact") is True:
            continue
        repository = str(item.get("repository") or "").strip()
        branch = str(item.get("branch") or "").strip()
        if not repository:
            continue
        try:
            normalized = normalize_github_repository(repository)
        except ValidationError:
            continue
        if branch:
            uncertain.add((normalized, branch))
        elif (
            item.get("author_trust") == "trusted"
            and item.get("issue_state") == "OPEN"
            and item.get("packet_status") == "ACTIVE"
        ):
            # /work-resume treats a missing BRANCH on a trusted open ACTIVE
            # packet as matching any current branch. Mirror that uncertainty.
            repository_uncertain.add(normalized)
    for worker in workers:
        repository = worker.get("repository")
        branch = worker.get("branch")
        if not isinstance(repository, str) or not isinstance(branch, str):
            continue
        if repository in repository_uncertain or (repository, branch) in uncertain:
            worker["state"] = "ORPHAN_OR_UNKNOWN"
            worker["packet_status"] = None


def _reconcile_imported_workers(
    workers: list[dict],
    packet_facts: Iterable[PacketFact],
    *,
    reconciled_repositories: Iterable[str] | None = None,
) -> None:
    """Re-evaluate imported lifecycle facts against current canonical GitHub facts.

    reconciled_repositories records repositories for which the current GitHub
    observation is authoritative even when it yielded zero canonical packet
    facts. That absence clears stale snapshot lifecycle state.
    """
    facts = list(packet_facts)
    packets = _packet_index(facts)
    packet_branches = {
        (normalize_github_repository(fact.repository), fact.branch) for fact in facts
    }
    packet_repositories = {
        normalize_github_repository(fact.repository) for fact in facts
    }
    coverage = set(packet_repositories)
    for repository in reconciled_repositories or []:
        coverage.add(normalize_github_repository(str(repository)))
    for worker in workers:
        repository = worker.get("repository")
        branch = worker.get("branch")
        head = worker.get("head")
        if (
            not isinstance(repository, str)
            or not isinstance(branch, str)
            or not isinstance(head, str)
            or repository not in coverage
        ):
            continue
        structural = worker.get("state") in {
            "DUPLICATE_WORKTREE",
            "ORPHAN_OR_UNKNOWN",
        }
        observed = packets.get((repository, branch, head))
        if observed == "AMBIGUOUS" or (
            observed is None and (repository, branch) in packet_branches
        ):
            worker["state"] = "ORPHAN_OR_UNKNOWN"
            worker["packet_status"] = None
        elif observed is not None:
            worker["packet_status"] = observed
            if not structural:
                worker["state"] = (
                    "TERMINAL_WORK_SURVIVOR"
                    if observed == "COMPLETE"
                    else "IDLE_REUSABLE"
                )
        else:
            worker["packet_status"] = None
            if worker.get("state") == "TERMINAL_WORK_SURVIVOR":
                worker["state"] = "IDLE_REUSABLE"


def _select_reconciliation(
    observations: list[dict], workers: Iterable[dict]
) -> tuple[list[dict], dict]:
    """Keep open packets and observations relevant to current worker branches."""
    relevant: set[tuple[str, str]] = set()
    for worker in workers:
        repository = worker.get("repository")
        branch = worker.get("branch")
        if isinstance(repository, str) and isinstance(branch, str):
            relevant.add((repository, branch))
    selected: list[dict] = []
    for item in observations:
        repository = item.get("repository")
        branch = item.get("branch")
        issue_state = item.get("issue_state")
        if issue_state == "OPEN" or (
            isinstance(repository, str)
            and isinstance(branch, str)
            and (repository, branch) in relevant
        ):
            selected.append(dict(item))
    summary = {
        "observed_count": len(observations),
        "canonical_count": sum(
            1 for item in observations if item.get("canonical_fact") is True
        ),
        "noncanonical_count": sum(
            1 for item in observations if item.get("canonical_fact") is not True
        ),
        "returned_count": len(selected),
    }
    return selected, summary


def build_report(
    sessions: Iterable[PersistSession],
    processes: Iterable[ProcessFact],
    identities: Mapping[str, GitIdentity | None],
    packet_facts: Iterable[PacketFact] | None = None,
    summary: dict | None = None,
    *,
    packet_observations: Iterable[dict] | None = None,
    context_advice: dict | None = None,
    local_host_id: str | None = None,
    imported_workers: Iterable[dict] | None = None,
    snapshot_summaries: Iterable[dict] | None = None,
    github_snapshot_summary: dict | None = None,
    reconciled_repositories: Iterable[str] | None = None,
) -> dict:
    packet_list = list(packet_facts or [])
    workers = classify_workers(sessions, processes, identities, packet_list)
    if local_host_id is not None:
        if not _HOST_ID_PATTERN.fullmatch(local_host_id):
            _reject("local host_id is invalid")
        for worker in workers:
            worker["host_id"] = local_host_id

    imported = [dict(item) for item in (imported_workers or [])]
    _reconcile_imported_workers(
        imported,
        packet_list,
        reconciled_repositories=reconciled_repositories,
    )
    workers.extend(imported)

    observations = (
        None if packet_observations is None else [dict(item) for item in packet_observations]
    )
    reconciliation = None
    reconciliation_summary = None
    if observations is not None:
        _apply_reconciliation_uncertainty(workers, observations)
        reconciliation, reconciliation_summary = _select_reconciliation(
            observations, workers
        )

    workers.sort(
        key=lambda item: (
            str(item.get("host_id") or ""),
            str(item.get("workspace") or ""),
            str(item.get("session_id") or ""),
        )
    )
    usage = None if summary is None else dict(summary)
    context = None if context_advice is None else dict(context_advice)
    snapshots = [dict(item) for item in (snapshot_summaries or [])]
    github_snapshot = (
        None if github_snapshot_summary is None else dict(github_snapshot_summary)
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cursor_usage_report",
        "workers": workers,
        "usage": usage,
        "reconciliation": reconciliation,
        "reconciliation_summary": reconciliation_summary,
        "context_epoch": context,
        "snapshots": snapshots,
        "github_snapshot": github_snapshot,
        "advisor": control_advisor(
            workers,
            current_observed=True,
            context_advice=context,
        ),
    }
    assert_content_free(report)
    return report


def load_packet_facts(path: Path) -> list[PacketFact]:
    """Load operator-supplied packet facts. Titles and bodies are not accepted."""
    import json

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("packet facts file is not JSON") from exc
    if not isinstance(raw, list):
        _reject("packet facts file must be a JSON list")
    facts: list[PacketFact] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            _reject(f"packet facts[{index}] must be an object")
        unknown = sorted(set(item).difference(_PACKET_KEYS))
        if unknown:
            _reject("unknown packet fact field: " + ", ".join(unknown))
        missing = [
            key
            for key in ("repository", "branch", "status", "head")
            if not str(item.get(key) or "").strip()
        ]
        if missing:
            _reject(f"packet facts[{index}] missing required fields")
        try:
            repository = normalize_github_repository(str(item["repository"]))
        except ValidationError as exc:
            raise ValidationError(f"packet facts[{index}] repository is invalid") from exc
        status = str(item["status"]).strip()
        if status not in PACKET_STATUSES:
            _reject(f"packet facts[{index}] status is not allowlisted")
        head = str(item["head"]).strip()
        if not _HEAD_PATTERN.fullmatch(head):
            _reject(f"packet facts[{index}] head is not an exact commit")
        issue_number = None
        if "issue_number" in item:
            number = item["issue_number"]
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                _reject(f"packet facts[{index}] issue_number is invalid")
            issue_number = number
        facts.append(
            PacketFact(
                repository=repository,
                branch=str(item["branch"]).strip(),
                status=status,
                head=head,
                issue_number=issue_number,
            )
        )
    return facts


def _bounded_snapshot_text(
    value: object, *, label: str, max_chars: int
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _reject(f"snapshot {label} must be a string or null")
    text = value.strip()
    if (
        not text
        or len(text) > max_chars
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        _reject(f"snapshot {label} is not bounded")
    return text


def _snapshot_observed_at(value: object) -> str:
    text = _bounded_snapshot_text(value, label="observed_at", max_chars=64)
    assert text is not None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("snapshot observed_at is not ISO-8601") from exc
    if parsed.tzinfo is None:
        _reject("snapshot observed_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_worker_snapshot(path: Path) -> tuple[list[dict], dict]:
    """Load one content-free host inventory for central read-only aggregation."""
    import json

    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ValidationError("worker snapshot is not readable") from exc
    if size > MAX_SNAPSHOT_BYTES:
        _reject("worker snapshot exceeds the bounded import size")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("worker snapshot is not JSON") from exc
    if not isinstance(raw, dict):
        _reject("worker snapshot must be a JSON object")
    assert_content_free(raw)
    unknown = sorted(set(raw).difference(_SNAPSHOT_TOP_KEYS))
    if unknown:
        _reject("unknown worker snapshot field: " + ", ".join(unknown))
    if raw.get("schema_version") != SCHEMA_VERSION:
        _reject("worker snapshot schema_version is unsupported")
    if raw.get("kind") != "cursor_worker_inventory":
        _reject("worker snapshot kind is not cursor_worker_inventory")
    host_id = _bounded_snapshot_text(raw.get("host_id"), label="host_id", max_chars=63)
    if host_id is None or not _HOST_ID_PATTERN.fullmatch(host_id):
        _reject("worker snapshot host_id is invalid")
    observed_at = _snapshot_observed_at(raw.get("observed_at"))
    raw_workers = raw.get("workers")
    if not isinstance(raw_workers, list):
        _reject("worker snapshot workers must be a list")
    if len(raw_workers) > MAX_SNAPSHOT_WORKERS:
        _reject("worker snapshot exceeds the bounded worker count")

    workers: list[dict] = []
    for index, item in enumerate(raw_workers):
        if not isinstance(item, dict):
            _reject(f"worker snapshot workers[{index}] must be an object")
        unknown_worker = sorted(set(item).difference(_SNAPSHOT_WORKER_KEYS))
        missing_worker = sorted(_SNAPSHOT_WORKER_KEYS.difference(item))
        if unknown_worker or missing_worker:
            _reject(f"worker snapshot workers[{index}] schema is invalid")
        worker_host_id = _bounded_snapshot_text(
            item.get("host_id"), label="host_id", max_chars=63
        )
        if worker_host_id != host_id:
            _reject(f"worker snapshot workers[{index}] host_id is inconsistent")
        session_id = _bounded_snapshot_text(
            item.get("session_id"), label="session_id", max_chars=255
        )
        workspace = _bounded_snapshot_text(
            item.get("workspace"), label="workspace", max_chars=4096
        )
        if workspace is not None and not workspace.startswith("/"):
            _reject(f"worker snapshot workers[{index}] workspace is not absolute")
        resident = item.get("resident")
        if not isinstance(resident, bool):
            _reject(f"worker snapshot workers[{index}] resident is invalid")
        attachment = item.get("attachment")
        if attachment not in {"attached", "detached", "unknown"}:
            _reject(f"worker snapshot workers[{index}] attachment is invalid")
        if item.get("inference_activity") != "UNKNOWN":
            _reject(
                f"worker snapshot workers[{index}] inference_activity is not UNKNOWN"
            )
        runtime = item.get("runtime")
        if runtime not in {"busy", "quiescent", "unknown"}:
            _reject(f"worker snapshot workers[{index}] runtime is invalid")
        state = item.get("state")
        if state not in WORKER_STATES:
            _reject(f"worker snapshot workers[{index}] state is invalid")

        repository = item.get("repository")
        branch = item.get("branch")
        head = item.get("head")
        dirty = item.get("dirty")
        if repository is None:
            if branch is not None or head is not None or dirty is not None:
                _reject(
                    f"worker snapshot workers[{index}] git identity is inconsistent"
                )
            normalized_repository = None
            normalized_branch = None
            normalized_head = None
        else:
            if not isinstance(repository, str):
                _reject(f"worker snapshot workers[{index}] repository is invalid")
            normalized_repository = normalize_github_repository(repository)
            normalized_branch = _bounded_snapshot_text(
                branch, label="branch", max_chars=255
            )
            normalized_head = _bounded_snapshot_text(
                head, label="head", max_chars=40
            )
            if (
                normalized_branch is None
                or normalized_head is None
                or not _HEAD_PATTERN.fullmatch(normalized_head)
                or not isinstance(dirty, bool)
            ):
                _reject(
                    f"worker snapshot workers[{index}] git identity is invalid"
                )

        packet_status = item.get("packet_status")
        if packet_status is not None and packet_status not in PACKET_STATUSES:
            _reject(f"worker snapshot workers[{index}] packet_status is invalid")
        workers.append(
            {
                "session_id": session_id,
                "workspace": workspace,
                "resident": resident,
                "attachment": attachment,
                "inference_activity": "UNKNOWN",
                "runtime": runtime,
                "state": state,
                "repository": normalized_repository,
                "branch": normalized_branch,
                "head": normalized_head,
                "dirty": dirty,
                "packet_status": packet_status,
                "host_id": host_id,
            }
        )

    return workers, {
        "host_id": host_id,
        "observed_at": observed_at,
        "worker_count": len(workers),
    }


def packet_facts_from_observations(observations: Iterable[dict]) -> list[PacketFact]:
    """Promote only fully reconciled GitHub observations into worker facts."""
    facts: list[PacketFact] = []
    for item in observations:
        if not isinstance(item, dict) or item.get("canonical_fact") is not True:
            continue
        repository = str(item.get("repository") or "").strip()
        branch = str(item.get("branch") or "").strip()
        status = str(item.get("packet_status") or "").strip()
        head = str(item.get("head") or "").strip().lower()
        number = item.get("issue_number")
        if (
            not repository
            or not branch
            or status not in PACKET_STATUSES
            or not _HEAD_PATTERN.fullmatch(head)
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
        ):
            _reject("canonical GitHub packet observation is malformed")
        facts.append(
            PacketFact(
                repository=normalize_github_repository(repository),
                branch=branch,
                status=status,
                head=head,
                issue_number=number,
            )
        )
    return facts


def collect_github_packet_observations(
    repositories: Iterable[str],
    *,
    adapter: GitHubWorkPacketAdapter | None = None,
) -> tuple[list[PacketFact], list[dict]]:
    """Read canonical GitHub packet/PR state without retaining Issue bodies."""
    reader = adapter or GitHubWorkPacketAdapter()
    observations: list[dict] = []
    normalized: set[str] = set()
    for repository in repositories:
        normalized.add(normalize_github_repository(str(repository)))
    for repository in sorted(normalized):
        observations.extend(reader.read_packet_observations(repository))
    observations.sort(
        key=lambda item: (
            str(item.get("repository") or ""),
            int(item.get("issue_number") or 0),
        )
    )
    facts = packet_facts_from_observations(observations)
    assert_content_free(observations)
    return facts, observations



def _github_snapshot_summary(observations: list[dict]) -> dict:
    return {
        "observed_count": len(observations),
        "canonical_count": sum(
            1 for item in observations if item.get("canonical_fact") is True
        ),
        "noncanonical_count": sum(
            1 for item in observations if item.get("canonical_fact") is not True
        ),
    }


def build_github_reconciliation_snapshot(
    repositories: Iterable[str],
    *,
    adapter: GitHubWorkPacketAdapter | None = None,
) -> dict:
    """Capture bounded, content-free GitHub lifecycle facts on an authenticated host."""
    normalized = sorted(
        {normalize_github_repository(str(repository)) for repository in repositories}
    )
    if not normalized:
        _reject("GitHub snapshot requires at least one repository")
    if len(normalized) > MAX_GITHUB_SNAPSHOT_REPOSITORIES:
        _reject("GitHub snapshot exceeds the bounded repository count")
    _, observations = collect_github_packet_observations(
        normalized, adapter=adapter
    )
    if len(observations) > MAX_GITHUB_SNAPSHOT_OBSERVATIONS:
        _reject("GitHub snapshot exceeds the bounded observation count")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cursor_github_reconciliation",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repositories": normalized,
        "observations": observations,
        "summary": _github_snapshot_summary(observations),
    }
    assert_content_free(payload)
    return payload


def _validate_github_observation(
    item: object,
    *,
    index: int,
    repositories: set[str],
) -> dict:
    if not isinstance(item, dict):
        _reject(f"GitHub snapshot observations[{index}] must be an object")
    unknown = sorted(set(item).difference(_GITHUB_OBSERVATION_KEYS))
    missing = sorted(_GITHUB_OBSERVATION_KEYS.difference(item))
    if unknown or missing:
        _reject(f"GitHub snapshot observations[{index}] schema is invalid")

    try:
        repository = normalize_github_repository(str(item["repository"]))
    except ValidationError as exc:
        raise ValidationError(
            f"GitHub snapshot observations[{index}] repository is invalid"
        ) from exc
    if repository not in repositories:
        _reject(f"GitHub snapshot observations[{index}] repository is undeclared")

    issue_number = item["issue_number"]
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
    ):
        _reject(f"GitHub snapshot observations[{index}] issue_number is invalid")
    issue_state = item["issue_state"]
    if issue_state not in {"OPEN", "CLOSED"}:
        _reject(f"GitHub snapshot observations[{index}] issue_state is invalid")

    updated = item["issue_updated_at"]
    if updated is not None:
        updated = _bounded_snapshot_text(
            updated, label="issue_updated_at", max_chars=64
        )

    author_trust = item["author_trust"]
    if author_trust not in {"trusted", "untrusted"}:
        _reject(f"GitHub snapshot observations[{index}] author_trust is invalid")

    packet_status = item["packet_status"]
    if packet_status is not None:
        packet_status = _bounded_snapshot_text(
            packet_status, label="packet_status", max_chars=64
        )

    branch = item["branch"]
    if branch is not None:
        branch = _bounded_snapshot_text(branch, label="branch", max_chars=255)

    head = item["head"]
    if head is not None:
        head = _bounded_snapshot_text(head, label="head", max_chars=40)
        if not _HEAD_PATTERN.fullmatch(head):
            _reject(f"GitHub snapshot observations[{index}] head is invalid")

    pr_number = item["pr_number"]
    if pr_number is not None and (
        isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number < 1
    ):
        _reject(f"GitHub snapshot observations[{index}] pr_number is invalid")

    pr_state = item["pr_state"]
    if pr_state not in _GITHUB_PR_STATES:
        _reject(f"GitHub snapshot observations[{index}] pr_state is invalid")

    pr_head = item["pr_head"]
    if pr_head is not None:
        pr_head = _bounded_snapshot_text(pr_head, label="pr_head", max_chars=40)
        if not _HEAD_PATTERN.fullmatch(pr_head):
            _reject(f"GitHub snapshot observations[{index}] pr_head is invalid")

    canonical = item["canonical_fact"]
    if not isinstance(canonical, bool):
        _reject(f"GitHub snapshot observations[{index}] canonical_fact is invalid")

    reasons = item["reasons"]
    if not isinstance(reasons, list) or len(reasons) > 16:
        _reject(f"GitHub snapshot observations[{index}] reasons is invalid")
    normalized_reasons: list[str] = []
    for reason in reasons:
        if not isinstance(reason, str) or reason not in _GITHUB_OBSERVATION_REASONS:
            _reject(f"GitHub snapshot observations[{index}] reason is invalid")
        normalized_reasons.append(reason)
    if len(normalized_reasons) != len(set(normalized_reasons)):
        _reject(f"GitHub snapshot observations[{index}] reasons are duplicated")
    normalized_reasons.sort()

    if canonical:
        canonical_conflict = (
            bool(normalized_reasons)
            or author_trust != "trusted"
            or packet_status not in PACKET_STATUSES
            or branch is None
            or head is None
            or (issue_state == "CLOSED" and packet_status != "COMPLETE")
            or pr_state in {"UNKNOWN", "AMBIGUOUS"}
            or (
                pr_state == "NONE"
                and (pr_number is not None or pr_head is not None)
            )
            or (
                pr_state in {"OPEN", "CLOSED", "MERGED"}
                and (pr_number is None or pr_head is None)
            )
            or (pr_head is not None and pr_head != head)
            or (packet_status == "ACTIVE" and pr_state == "MERGED")
            or (packet_status == "COMPLETE" and pr_state == "OPEN")
        )
        if canonical_conflict:
            _reject(
                f"GitHub snapshot observations[{index}] canonical fact is inconsistent"
            )

    return {
        "repository": repository,
        "issue_number": issue_number,
        "issue_state": issue_state,
        "issue_updated_at": updated,
        "author_trust": author_trust,
        "packet_status": packet_status,
        "branch": branch,
        "head": head,
        "pr_number": pr_number,
        "pr_state": pr_state,
        "pr_head": pr_head,
        "canonical_fact": canonical,
        "reasons": normalized_reasons,
    }


def _read_github_snapshot_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    source = Path(path)
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        _reject("GitHub snapshot path safety is unsupported")
    try:
        if not stat.S_ISREG(os.lstat(source).st_mode):
            _reject("GitHub snapshot path is unsafe")
        fd = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as exc:
        raise ValidationError("GitHub snapshot path is unsafe or unreadable") from exc
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            _reject("GitHub snapshot path is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(64 * 1024, MAX_SNAPSHOT_BYTES - total + 1))
            if not block:
                break
            total += len(block)
            if total > MAX_SNAPSHOT_BYTES:
                _reject("GitHub snapshot exceeds the bounded import size")
            chunks.append(block)
        final_stat = os.fstat(fd)
        if (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
        ) != (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        ):
            _reject("GitHub snapshot changed while being read")
        return b"".join(chunks), final_stat
    finally:
        os.close(fd)


def load_github_reconciliation_snapshot(
    path: Path,
    *,
    include_file_stat: bool = False,
):
    """Load one bounded GitHub reconciliation snapshot without GitHub access."""
    import json

    try:
        raw_bytes, source_stat = _read_github_snapshot_bytes(Path(path))
        raw = json.loads(raw_bytes.decode("utf-8"))
    except ValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("GitHub snapshot is not JSON") from exc
    if not isinstance(raw, dict):
        _reject("GitHub snapshot must be a JSON object")
    if contains_unsafe_secret(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    ):
        _reject("GitHub snapshot contains unsafe secret")
    assert_content_free(raw)
    unknown = sorted(set(raw).difference(_GITHUB_SNAPSHOT_TOP_KEYS))
    missing = sorted(_GITHUB_SNAPSHOT_TOP_KEYS.difference(raw))
    if unknown or missing:
        _reject("GitHub snapshot schema is invalid")
    if raw.get("schema_version") != SCHEMA_VERSION:
        _reject("GitHub snapshot schema_version is unsupported")
    if raw.get("kind") != "cursor_github_reconciliation":
        _reject("GitHub snapshot kind is invalid")
    observed_at = _snapshot_observed_at(raw.get("observed_at"))

    raw_repositories = raw.get("repositories")
    if not isinstance(raw_repositories, list) or not raw_repositories:
        _reject("GitHub snapshot repositories must be a non-empty list")
    if len(raw_repositories) > MAX_GITHUB_SNAPSHOT_REPOSITORIES:
        _reject("GitHub snapshot exceeds the bounded repository count")
    repositories: list[str] = []
    for index, repository in enumerate(raw_repositories):
        if not isinstance(repository, str):
            _reject(f"GitHub snapshot repositories[{index}] is invalid")
        repositories.append(normalize_github_repository(repository))
    if repositories != sorted(set(repositories)):
        _reject("GitHub snapshot repositories must be unique and sorted")
    repository_set = set(repositories)

    raw_observations = raw.get("observations")
    if not isinstance(raw_observations, list):
        _reject("GitHub snapshot observations must be a list")
    if len(raw_observations) > MAX_GITHUB_SNAPSHOT_OBSERVATIONS:
        _reject("GitHub snapshot exceeds the bounded observation count")
    observations = [
        _validate_github_observation(
            item, index=index, repositories=repository_set
        )
        for index, item in enumerate(raw_observations)
    ]
    observations.sort(
        key=lambda item: (
            str(item["repository"]),
            int(item["issue_number"]),
        )
    )

    summary = raw.get("summary")
    expected_summary = _github_snapshot_summary(observations)
    if summary != expected_summary:
        _reject("GitHub snapshot summary does not match observations")

    facts = packet_facts_from_observations(observations)
    metadata = {
        "observed_at": observed_at,
        "repositories": repositories,
        **expected_summary,
    }
    assert_content_free(metadata)
    if include_file_stat:
        return facts, observations, metadata, source_stat
    return facts, observations, metadata


def load_context_advice(path: Path) -> dict:
    """Evaluate bounded Engineering System context facts without executing control."""
    import json

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("context facts file is not JSON") from exc
    if not isinstance(raw, dict):
        _reject("context facts file must be a JSON object")
    allowed = {
        "in_flight",
        "unreconciled_mutation",
        "durable_checkpoint",
        "same_atomic_task",
        "logical_boundary",
        "workstream_changed",
        "next_action_changed",
        "profile_change_pending",
        "repeated_failure",
        "precompact",
    }
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        _reject("unknown context fact field: " + ", ".join(unknown))
    precompact = raw.get("precompact")
    if precompact is not None:
        if not isinstance(precompact, dict):
            _reject("precompact context fact must be an object")
        precompact_allowed = {
            "context_tokens",
            "context_window_size",
            "message_count",
            "messages_to_compact",
            "context_usage_percent",
            "trigger",
            "is_first_compaction",
        }
        precompact_unknown = sorted(set(precompact).difference(precompact_allowed))
        if precompact_unknown:
            _reject(
                "unknown precompact context fact field: "
                + ", ".join(precompact_unknown)
            )

    import subprocess
    import sys

    helper = Path(__file__).resolve().parents[1] / "tools" / "context_epoch.py"
    if not helper.is_file():
        raise ValidationError("managed context_epoch helper is unavailable")
    try:
        completed = subprocess.run(
            [sys.executable, str(helper), "epoch-decide", "--facts", "-"],
            cwd=str(helper.parent.parent),
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            input=json.dumps(raw, sort_keys=True, separators=(",", ":")),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("managed context_epoch helper is unavailable") from exc
    if completed.returncode != 0:
        raise ValidationError("context facts rejected by managed context_epoch helper")
    if len(completed.stdout.encode("utf-8")) > 4096:
        _reject("context epoch decision is malformed")
    try:
        decision = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("context epoch decision is malformed") from exc
    if not isinstance(decision, dict):
        _reject("context epoch decision is malformed")
    source_action = str(decision.get("action") or "")
    reason = str(decision.get("reason") or "")
    action_map = {
        "CONTINUE": "CONTINUE",
        "CHECKPOINT_REQUIRED": "CHECKPOINT",
        "SUMMARIZE": "SUMMARIZE",
        "CLEAR": "CLEAR_RECOMMENDED",
    }
    recommendation = action_map.get(source_action)
    if recommendation not in RECOMMENDATIONS:
        _reject("context epoch action is not allowlisted")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", reason):
        _reject("context epoch reason is not a bounded label")
    return {
        "recommendation": recommendation,
        "reasons": [reason],
        "source": "ENGINEERING_SYSTEM_CONTEXT_EPOCH",
        "source_action": source_action,
    }


def control_advisor(
    workers: Iterable[dict],
    *,
    current_observed: bool,
    context_advice: dict | None = None,
) -> dict:
    """Combine worker safety with a bounded context recommendation.

    Worker ambiguity always wins. Context CLEAR is recommendation-only; this
    module never runs destructive context commands, stop, kill, or session mutation.
    """
    base = recommend(workers, current_observed=current_observed)
    if base["recommendation"] == "HUMAN_REQUIRED" or context_advice is None:
        return base
    recommendation = str(context_advice.get("recommendation") or "")
    reasons = context_advice.get("reasons")
    if recommendation not in RECOMMENDATIONS or not isinstance(reasons, list):
        _reject("context advice is malformed")
    if recommendation == "UNKNOWN":
        return base
    bounded_reasons: list[str] = []
    for reason in reasons:
        text = str(reason)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", text):
            _reject("context advice reason is not a bounded label")
        bounded_reasons.append(text)
    return {
        "recommendation": recommendation,
        "reasons": sorted(set(bounded_reasons)),
        "source": str(context_advice.get("source") or "CONTEXT"),
        "source_action": str(context_advice.get("source_action") or ""),
    }


def read_git_identity(workspace: str, git_runner: GitRunner | None = None) -> GitIdentity | None:
    """Read repository, branch, HEAD, and dirty bit. Porcelain text is dropped."""
    runner = git_runner or _default_git_runner
    canonical = canonical_workspace(workspace)
    if canonical is None:
        return None

    def _run(argv: list[str]) -> str | None:
        try:
            return runner(argv, canonical).strip()
        except (OSError, ValidationError):
            return None

    top = _run(["git", "rev-parse", "--show-toplevel"])
    origin = _run(["git", "remote", "get-url", "origin"])
    branch = _run(["git", "branch", "--show-current"])
    head = _run(["git", "rev-parse", "HEAD"])
    porcelain = _run(["git", "status", "--porcelain", "--untracked-files=all"])
    if top is None or origin is None or not branch or head is None or porcelain is None:
        return None
    if canonical_workspace(top) != canonical:
        return None
    if not _HEAD_PATTERN.fullmatch(head):
        return None
    try:
        repository = normalize_github_repository(origin)
    except ValidationError:
        return None
    return GitIdentity(
        repository=repository,
        branch=branch,
        head=head,
        dirty=bool(porcelain.strip()),
    )


def _default_git_runner(argv: list[str], cwd: str) -> str:
    import subprocess

    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ValidationError("git identity unavailable") from exc
    if completed.returncode != 0:
        raise ValidationError("git identity unavailable")
    return completed.stdout


def _sched_state(stat_text: str) -> str:
    close = stat_text.rfind(")")
    if close < 0 or close + 2 >= len(stat_text):
        return "unreadable"
    char = stat_text[close + 2]
    if char == "R":
        return "running"
    if char in {"S", "D", "I"}:
        return "sleeping"
    if char in {"Z", "T", "t", "X", "x"}:
        return "other"
    return "unreadable"


def _descendant_pids(parents: Mapping[int, int], root_pid: int, blocked: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    found = {root_pid}
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child in found or child in blocked:
                continue
            found.add(child)
            stack.append(child)
    return found


def _persist_observation(
    cmdline: bytes, known_session_ids: set[str]
) -> tuple[bool, str | None, bool]:
    """Return (is_restore, session_id, ambiguous). Command bytes are not returned.

    Host-proven restore argv is exactly::

        node --use-system-ca <index.js> --cursor-persist-restore <32 hex> <session id>

    The hex token is discarded. The session id is kept only when it is the
    final argument, matches the persist-list id shape, and is already in
    ``known_session_ids``. Any other node restore-shaped argv is ambiguous.
    """
    if not cmdline:
        return False, None, False
    parts = [part.decode("utf-8", "replace") for part in cmdline.split(b"\x00") if part]
    if len(parts) < 4 or Path(parts[0]).name != "node":
        return False, None, False
    if _RESTORE_FLAG not in parts:
        return False, None, False
    restore_at = parts.index(_RESTORE_FLAG)
    if not any(Path(part).name == "index.js" for part in parts[:restore_at]):
        return False, None, False
    if (
        len(parts) == 6
        and parts[1] == _NODE_CA_FLAG
        and Path(parts[2]).name == "index.js"
        and parts[3] == _RESTORE_FLAG
        and _RESTORE_TOKEN_PATTERN.fullmatch(parts[4])
    ):
        session_id = parts[5]
        if (
            session_id in known_session_ids
            and _CURSOR_SESSION_ID_PATTERN.fullmatch(session_id)
        ):
            return True, session_id, False
    return True, None, True


def collect_process_facts(
    *,
    proc_root: Path | None = None,
    known_session_ids: set[str] | None = None,
) -> list[ProcessFact]:
    """Observe persist processes and drop command text immediately."""
    root = proc_root or Path("/proc")
    if not root.is_dir():
        return []
    known = set(known_session_ids or set())
    entries = [entry for entry in root.iterdir() if entry.name.isdigit()]
    if len(entries) > MAX_PROCESS_SCAN:
        _reject("process scan exceeded the bounded observation limit")
    parents: dict[int, int] = {}
    sched: dict[int, str] = {}
    persist: list[tuple[int, str | None, str | None, bool]] = []
    for entry in entries:
        pid = int(entry.name)
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            stat_text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ppid = None
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    ppid = int(line.split()[1])
                except (IndexError, ValueError):
                    ppid = None
                break
        if ppid is None:
            continue
        parents[pid] = ppid
        sched[pid] = _sched_state(stat_text)
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        is_persist, session_id, ambiguous = _persist_observation(cmdline, known)
        del cmdline
        if not is_persist:
            continue
        try:
            workspace = canonical_workspace(str((entry / "cwd").resolve()))
        except OSError:
            workspace = None
        persist.append((pid, session_id, workspace, ambiguous))

    persist_pids = {pid for pid, _session, _workspace, _ambiguous in persist}
    facts: list[ProcessFact] = []
    for pid, session_id, workspace, ambiguous in persist:
        if ambiguous:
            runtime = "unknown"
        else:
            members = _descendant_pids(parents, pid, persist_pids.difference({pid}))
            states = [sched.get(member, "unreadable") for member in members]
            if "running" in states:
                runtime = "busy"
            elif "unreadable" in states:
                runtime = "unknown"
            else:
                runtime = "quiescent"
        facts.append(
            ProcessFact(
                session_id=session_id,
                workspace=workspace,
                ambiguous=ambiguous,
                runtime=runtime,
            )
        )
    return facts


def identities_for_workspaces(
    workspaces: Iterable[str],
    *,
    git_runner: GitRunner | None = None,
    reader: Callable[[str], GitIdentity | None] | None = None,
) -> dict[str, GitIdentity | None]:
    identity_reader = reader or (lambda path: read_git_identity(path, git_runner))
    found: dict[str, GitIdentity | None] = {}
    for workspace in workspaces:
        canonical = canonical_workspace(workspace)
        if canonical is None or canonical in found:
            continue
        found[canonical] = identity_reader(canonical)
    return found


def live_sessions() -> list[PersistSession]:
    return default_list_persist_sessions()


def inventory_report(
    sessions: list[PersistSession],
    processes: list[ProcessFact],
    identities: Mapping[str, GitIdentity | None],
    packet_facts: list[PacketFact] | None = None,
    summary: dict | None = None,
) -> dict:
    report = build_report(sessions, processes, identities, packet_facts, summary)
    if summary is None:
        report["kind"] = "cursor_worker_inventory"
        report.pop("usage", None)
    return report


def summary_report(events: list[UsageEvent]) -> dict:
    summary = summarize_usage(events)
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cursor_usage_summary",
        **summary,
        "advisor": recommend([], current_observed=False),
    }
    assert_content_free(report)
    return report


def assert_content_free(payload: object) -> None:
    """Fail closed if a report keeps command, prompt, or environment text keys."""
    forbidden = {
        "argv",
        "cmdline",
        "command",
        "env",
        "environment",
        "prompt",
        "task",
        "transcript",
        "tool_payload",
    }
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if str(key).lower() in forbidden:
                    _reject("usage report retained forbidden content")
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
