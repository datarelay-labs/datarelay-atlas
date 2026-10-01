"""Model-aware instruction governance without an Atlas prompting fork.

Engineering System AGENT_BASE and behavior scenarios are immutable external
references. Atlas inventories repository-owned managed surfaces, binds exact
revision/profile identity, and records advisory behavior/candidate-diff evidence.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

from atlas.cursor_usage import assert_content_free
from atlas.data_lock import atomic_write_text, data_root_write_lock
from atlas.provenance import ValidationError

SCHEMA_VERSION = 1
PROFILE_KIND = "instruction_governance_profile"
RESULT_KIND = "instruction_governance_audit_result"
LEDGER_KIND = "instruction_governance_ledger"
FILENAME = "instruction-governance.json"
AUTHORITY = "ADVISORY_ONLY"
TRIGGERS = frozenset({"MODEL_CHANGE", "HARNESS_CHANGE", "INSTRUCTION_CHANGE", "MANUAL_AUDIT"})
RESULTS = frozenset({"PASS", "FAIL", "UNKNOWN"})
OUTCOMES = frozenset({"NO_CHANGE", "CANARY_READY", "REJECTED", "HUMAN_REQUIRED"})
_MAX_REFERENCE_BYTES = 256 * 1024
_MAX_LEDGER_BYTES = 1024 * 1024
_MAX_SURFACES = 256
_MAX_CHANGES = 32
_MAX_AUDITS = 500
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
_SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|ghp_|github_pat_|AKIA|Bearer |-----BEGIN)")

_EXACT_SURFACES = {
    "AGENTS.md": "AGENT_RULES",
    ".engineering/project.yaml": "ENGINEERING_METADATA",
    ".engineering/tests.yaml": "ENGINEERING_TEST_POLICY",
    ".engineering/release.yaml": "ENGINEERING_RELEASE_POLICY",
    ".github/ISSUE_TEMPLATE/ai-work-packet.md": "WORK_PACKET_TEMPLATE",
    ".cursorignore": "PROVIDER_CONTEXT_RULE",
    "atlas/mcp_context.py": "MCP_TOOL_DESCRIPTIONS",
    "atlas/mcp_http.py": "MCP_TOOL_DESCRIPTIONS",
    "scripts/awc-completion-hook.sh": "HOOK",
    "schemas/skills-contract.schema.json": "SKILL_CONTRACT",
    "tools/skills-contract.py": "SKILL_CONTRACT",
}
_PREFIX_SURFACES = (
    (".cursor/commands/", "PROVIDER_COMMAND"),
    (".cursor/rules/", "PROVIDER_RULE"),
    ("prompts/", "PROMPT_TEMPLATE"),
    ("ai/", "AI_INSTRUCTION"),
    ("skills/", "SKILL"),
)


def _reject(message: str) -> None:
    raise ValidationError(message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("instruction governance payload is not canonical JSON") from exc
    return _sha256(raw)


def _identity(value: object, *, label: str, provider: bool = False) -> str:
    pattern = _PROVIDER if provider else _ID
    if not isinstance(value, str) or pattern.fullmatch(value) is None or _SECRET.search(value):
        _reject(f"instruction governance {label} is invalid")
    return value


def _utc(value: object) -> str:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        _reject("instruction governance timestamp must be UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("instruction governance timestamp must be UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _reject("instruction governance timestamp must be UTC")
    return value

def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("instruction governance git identity is unavailable") from exc
    return completed.stdout.strip()


def _git_repository(root: Path) -> str:
    remote = _git(root, "config", "--get", "remote.origin.url")
    value = remote.strip()
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@github.com:"):
        value = value[len("git@github.com:"):]
    elif value.startswith("https://github.com/"):
        value = value[len("https://github.com/"):]
    elif value.startswith("ssh://git@github.com/"):
        value = value[len("ssh://git@github.com/"):]
    else:
        raise ValidationError("instruction governance origin is not canonical GitHub")
    if _REPO.fullmatch(value) is None:
        raise ValidationError("instruction governance repository identity is invalid")
    return value


def _surface_category(path: str) -> str | None:
    if path in _EXACT_SURFACES:
        return _EXACT_SURFACES[path]
    for prefix, category in _PREFIX_SURFACES:
        if path.startswith(prefix):
            return category
    name = Path(path).name.lower()
    if path.startswith(".github/") and ("template" in name or "prompt" in name):
        return "GITHUB_TEMPLATE"
    if path.startswith("scripts/") and "hook" in name:
        return "HOOK"
    return None




def _tracked_managed_surface_paths(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-z")
    paths = []
    for relative in raw.split("\0"):
        if not relative:
            continue
        if _surface_category(relative) is not None:
            paths.append(relative)
    return sorted(set(paths))


def discover_managed_surfaces(repo_root: Path) -> list[dict[str, object]]:
    root = Path(repo_root).resolve()
    surfaces: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or relative.startswith(".venv/") or relative.startswith("tests/"):
            continue
        category = _surface_category(relative)
        if category is None:
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValidationError("instruction governance managed surface is unreadable") from exc
        surfaces.append(
            {
                "path": relative,
                "category": category,
                "content_digest": _sha256(data),
                "size_bytes": len(data),
            }
        )
    if not surfaces or len(surfaces) > _MAX_SURFACES:
        raise ValidationError("instruction governance managed surface inventory is invalid")
    return surfaces

def _reference_bytes(path: Path, *, label: str) -> bytes:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValidationError(f"instruction governance {label} reference is unsafe")
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ValidationError(f"instruction governance {label} reference is unreadable") from exc
    if not data or len(data) > _MAX_REFERENCE_BYTES:
        raise ValidationError(f"instruction governance {label} reference exceeds bounds")
    return data


def _behavior_scenarios(data: bytes) -> list[dict[str, object]]:
    if yaml is None:
        raise ValidationError("PyYAML is required for instruction governance behavior scenarios")
    try:
        payload = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValidationError("instruction governance behavior scenarios are invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("scenarios"), list)
    ):
        raise ValidationError("instruction governance behavior scenarios are invalid")
    scenarios = []
    for raw in payload["scenarios"]:
        if not isinstance(raw, dict):
            raise ValidationError("instruction governance behavior scenario is invalid")
        scenario_id = _identity(raw.get("id"), label="behavior scenario id")
        mandatory = raw.get("mandatory")
        safety = raw.get("safety")
        checker = _identity(raw.get("checker"), label="behavior checker")
        if type(mandatory) is not bool or type(safety) is not bool:
            raise ValidationError("instruction governance behavior scenario flags are invalid")
        scenarios.append(
            {
                "id": scenario_id,
                "mandatory": mandatory,
                "safety": safety,
                "checker": checker,
            }
        )
    ids = [item["id"] for item in scenarios]
    if not scenarios or len(ids) != len(set(ids)):
        raise ValidationError("instruction governance behavior scenario identities are invalid")
    return sorted(scenarios, key=lambda item: str(item["id"]))


def validate_instruction_profile(payload: object) -> dict[str, object]:
    keys = {
        "schema_version", "kind", "target_repository", "expected_target_head",
        "engineering_system_repository", "engineering_system_revision",
        "agent_base_digest", "behavior_scenarios_digest", "trigger_kind",
        "trigger_revision", "model_provider", "model_name", "model_profile",
        "harness_id", "harness_revision"
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        _reject("instruction governance profile schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool):
        _reject("instruction governance profile schema_version is unsupported")
    if payload.get("kind") != PROFILE_KIND:
        _reject("instruction governance profile kind is invalid")
    target_repository = payload.get("target_repository")
    engineering_repository = payload.get("engineering_system_repository")
    if not isinstance(target_repository, str) or _REPO.fullmatch(target_repository) is None:
        _reject("instruction governance target repository is invalid")
    if engineering_repository != "datarelay-labs/engineering-system":
        _reject("instruction governance Engineering System repository is invalid")
    target_head = payload.get("expected_target_head")
    engineering_revision = payload.get("engineering_system_revision")
    if not isinstance(target_head, str) or _SHA40.fullmatch(target_head) is None:
        _reject("instruction governance target head is invalid")
    if not isinstance(engineering_revision, str) or _SHA40.fullmatch(engineering_revision) is None:
        _reject("instruction governance Engineering System revision is invalid")
    for key in ("agent_base_digest", "behavior_scenarios_digest"):
        if not isinstance(payload.get(key), str) or _SHA256.fullmatch(payload[key]) is None:
            _reject(f"instruction governance {key} is invalid")
    trigger = payload.get("trigger_kind")
    if trigger not in TRIGGERS:
        _reject("instruction governance trigger_kind is unsupported")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PROFILE_KIND,
        "target_repository": target_repository,
        "expected_target_head": target_head,
        "engineering_system_repository": engineering_repository,
        "engineering_system_revision": engineering_revision,
        "agent_base_digest": payload["agent_base_digest"],
        "behavior_scenarios_digest": payload["behavior_scenarios_digest"],
        "trigger_kind": trigger,
        "trigger_revision": _identity(payload.get("trigger_revision"), label="trigger_revision"),
        "model_provider": _identity(payload.get("model_provider"), label="model_provider", provider=True),
        "model_name": _identity(payload.get("model_name"), label="model_name"),
        "model_profile": _identity(payload.get("model_profile"), label="model_profile"),
        "harness_id": _identity(payload.get("harness_id"), label="harness_id"),
        "harness_revision": _identity(payload.get("harness_revision"), label="harness_revision"),
    }

def build_instruction_governance_profile(
    *,
    repo_root: Path,
    engineering_system_revision: str,
    agent_base_path: Path,
    behavior_scenarios_path: Path,
    trigger_kind: str,
    trigger_revision: str,
    model_provider: str,
    model_name: str,
    model_profile: str,
    harness_id: str,
    harness_revision: str,
) -> dict[str, object]:
    """Build a profile from exact local target identity and external reference bytes."""
    root = Path(repo_root).resolve()
    agent_base = _reference_bytes(Path(agent_base_path), label="AGENT_BASE")
    scenarios = _reference_bytes(Path(behavior_scenarios_path), label="behavior scenarios")
    # Parse scenarios now so profile creation fails before a later audit if the
    # supplied canonical behavior reference is malformed.
    _behavior_scenarios(scenarios)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": PROFILE_KIND,
        "target_repository": _git_repository(root),
        "expected_target_head": _git(root, "rev-parse", "HEAD"),
        "engineering_system_repository": "datarelay-labs/engineering-system",
        "engineering_system_revision": engineering_system_revision,
        "agent_base_digest": _sha256(agent_base),
        "behavior_scenarios_digest": _sha256(scenarios),
        "trigger_kind": trigger_kind,
        "trigger_revision": trigger_revision,
        "model_provider": model_provider,
        "model_name": model_name,
        "model_profile": model_profile,
        "harness_id": harness_id,
        "harness_revision": harness_revision,
    }
    return validate_instruction_profile(payload)


def build_instruction_candidate_change(
    *,
    repo_root: Path,
    managed_path: str,
    candidate_path: Path,
) -> dict[str, str]:
    """Return path + before/after digests without retaining candidate content."""
    root = Path(repo_root).resolve()
    surfaces = {str(item["path"]): item for item in discover_managed_surfaces(root)}
    current = surfaces.get(managed_path)
    if current is None:
        raise ValidationError("instruction governance candidate path is not a managed surface")
    candidate = _reference_bytes(Path(candidate_path), label="candidate surface")
    after = _sha256(candidate)
    before = str(current["content_digest"])
    if after == before:
        raise ValidationError("instruction governance candidate surface is unchanged")
    return {
        "path": managed_path,
        "before_digest": before,
        "after_digest": after,
    }


def _load_json(path: Path, *, label: str, max_bytes: int = _MAX_REFERENCE_BYTES) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValidationError(f"instruction governance {label} file is unsafe")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValidationError(f"instruction governance {label} file is unreadable") from exc
    if len(raw) > max_bytes:
        raise ValidationError(f"instruction governance {label} file exceeds bounds")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"instruction governance {label} file is invalid JSON") from exc
    assert_content_free(payload)
    return payload


def _ledger_empty() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "authority": AUTHORITY,
        "audits": [],
    }


def _load_ledger(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / FILENAME
    if not path.exists():
        return _ledger_empty()
    if path.is_symlink() or not path.is_file():
        raise ValidationError("instruction governance ledger path is unsafe")
    payload = _load_json(path, label="ledger", max_bytes=_MAX_LEDGER_BYTES)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "authority", "audits"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or payload.get("authority") != AUTHORITY
        or not isinstance(payload.get("audits"), list)
        or len(payload["audits"]) > _MAX_AUDITS
    ):
        raise ValidationError("instruction governance ledger schema is invalid")
    audits = []
    seen: set[str] = set()
    for item in payload["audits"]:
        if not isinstance(item, dict):
            raise ValidationError("instruction governance ledger audit is invalid")
        audit_id = item.get("audit_identity")
        if not isinstance(audit_id, str) or _SHA256.fullmatch(audit_id) is None or audit_id in seen:
            raise ValidationError("instruction governance ledger audit identity is invalid")
        seen.add(audit_id)
        audits.append(dict(item))
    return {**_ledger_empty(), "audits": audits}

def instruction_governance_preflight(
    data_root: Path,
    *,
    repo_root: Path,
    profile: object,
    agent_base_path: Path,
    behavior_scenarios_path: Path,
) -> dict[str, object]:
    root = Path(repo_root).resolve()
    normalized_profile = validate_instruction_profile(profile)
    repository = _git_repository(root)
    head = _git(root, "rev-parse", "HEAD")
    if repository != normalized_profile["target_repository"]:
        raise ValidationError("instruction governance target repository does not match worktree")
    if head != normalized_profile["expected_target_head"]:
        raise ValidationError("instruction governance target head does not match worktree")

    agent_base = _reference_bytes(Path(agent_base_path), label="AGENT_BASE")
    scenarios_data = _reference_bytes(Path(behavior_scenarios_path), label="behavior scenarios")
    if _sha256(agent_base) != normalized_profile["agent_base_digest"]:
        raise ValidationError("instruction governance AGENT_BASE digest mismatch")
    if _sha256(scenarios_data) != normalized_profile["behavior_scenarios_digest"]:
        raise ValidationError("instruction governance behavior-scenarios digest mismatch")
    scenarios = _behavior_scenarios(scenarios_data)

    surfaces = discover_managed_surfaces(root)
    surface_paths = sorted(
        set(str(item["path"]) for item in surfaces)
        | set(_tracked_managed_surface_paths(root))
    )
    dirty = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *surface_paths,
    )
    if dirty:
        raise ValidationError("instruction governance managed surfaces are dirty")
    inventory_digest = _canonical_digest(surfaces)

    identity_payload = {
        "profile": normalized_profile,
        "target_head": head,
        "inventory_digest": inventory_digest,
        "engineering_reference": {
            "agent_base_digest": _sha256(agent_base),
            "behavior_scenarios_digest": _sha256(scenarios_data),
        },
    }
    audit_identity = _canonical_digest(identity_payload)
    ledger = _load_ledger(Path(data_root))
    existing = next(
        (item for item in ledger["audits"] if item.get("audit_identity") == audit_identity),
        None,
    )
    return {
        "state": "DUPLICATE_NOOP" if existing is not None else "AUDIT_REQUIRED",
        "authority": AUTHORITY,
        "audit_identity": audit_identity,
        "target_repository": repository,
        "target_head": head,
        "engineering_system_revision": normalized_profile["engineering_system_revision"],
        "model_provider": normalized_profile["model_provider"],
        "model_name": normalized_profile["model_name"],
        "model_profile": normalized_profile["model_profile"],
        "harness_id": normalized_profile["harness_id"],
        "harness_revision": normalized_profile["harness_revision"],
        "trigger_kind": normalized_profile["trigger_kind"],
        "trigger_revision": normalized_profile["trigger_revision"],
        "inventory_digest": inventory_digest,
        "managed_surfaces": surfaces,
        "behavior_scenarios": scenarios,
        "existing_audit": existing,
    }

def _validate_audit_result(payload: object) -> dict[str, object]:
    keys = {
        "schema_version", "kind", "audit_identity", "evaluated_at",
        "behavior_results", "candidate_changes", "evaluation_ref"
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValidationError("instruction governance audit result schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool):
        raise ValidationError("instruction governance audit result schema_version is unsupported")
    if payload.get("kind") != RESULT_KIND:
        raise ValidationError("instruction governance audit result kind is invalid")
    audit_identity = payload.get("audit_identity")
    if not isinstance(audit_identity, str) or _SHA256.fullmatch(audit_identity) is None:
        raise ValidationError("instruction governance audit result identity is invalid")
    evaluated_at = _utc(payload.get("evaluated_at"))
    evaluation_ref = _identity(payload.get("evaluation_ref"), label="evaluation_ref")

    behavior_results = payload.get("behavior_results")
    if not isinstance(behavior_results, list) or len(behavior_results) > 128:
        raise ValidationError("instruction governance behavior results are invalid")
    normalized_results = []
    seen: set[str] = set()
    for item in behavior_results:
        if not isinstance(item, dict) or set(item) != {"scenario_id", "outcome"}:
            raise ValidationError("instruction governance behavior result is invalid")
        scenario_id = _identity(item.get("scenario_id"), label="behavior scenario id")
        outcome = item.get("outcome")
        if outcome not in RESULTS or scenario_id in seen:
            raise ValidationError("instruction governance behavior result is invalid")
        seen.add(scenario_id)
        normalized_results.append({"scenario_id": scenario_id, "outcome": outcome})

    changes = payload.get("candidate_changes")
    if not isinstance(changes, list) or len(changes) > _MAX_CHANGES:
        raise ValidationError("instruction governance candidate changes are invalid")
    normalized_changes = []
    for item in changes:
        if not isinstance(item, dict) or set(item) != {"path", "before_digest", "after_digest"}:
            raise ValidationError("instruction governance candidate change is invalid")
        path = _identity(item.get("path"), label="candidate path")
        before = item.get("before_digest")
        after = item.get("after_digest")
        if (
            not isinstance(before, str)
            or _SHA256.fullmatch(before) is None
            or not isinstance(after, str)
            or _SHA256.fullmatch(after) is None
            or before == after
        ):
            raise ValidationError("instruction governance candidate change digest is invalid")
        normalized_changes.append({"path": path, "before_digest": before, "after_digest": after})
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "audit_identity": audit_identity,
        "evaluated_at": evaluated_at,
        "behavior_results": sorted(normalized_results, key=lambda item: item["scenario_id"]),
        "candidate_changes": sorted(normalized_changes, key=lambda item: item["path"]),
        "evaluation_ref": evaluation_ref,
    }

def record_instruction_governance_audit(
    data_root: Path,
    *,
    repo_root: Path,
    profile: object,
    agent_base_path: Path,
    behavior_scenarios_path: Path,
    result: object,
) -> dict[str, object]:
    preflight = instruction_governance_preflight(
        data_root,
        repo_root=repo_root,
        profile=profile,
        agent_base_path=agent_base_path,
        behavior_scenarios_path=behavior_scenarios_path,
    )
    if preflight["state"] == "DUPLICATE_NOOP":
        return {
            "state": "DUPLICATE_NOOP",
            "authority": AUTHORITY,
            "audit": preflight["existing_audit"],
        }

    normalized = _validate_audit_result(result)
    if normalized["audit_identity"] != preflight["audit_identity"]:
        raise ValidationError("instruction governance result is bound to a different audit identity")

    known_scenarios = {item["id"]: item for item in preflight["behavior_scenarios"]}
    results = {item["scenario_id"]: item["outcome"] for item in normalized["behavior_results"]}
    if not set(results).issubset(set(known_scenarios)):
        raise ValidationError("instruction governance result references unknown behavior scenario")
    mandatory = {
        scenario_id
        for scenario_id, item in known_scenarios.items()
        if item["mandatory"] is True
    }
    missing_mandatory = sorted(mandatory - set(results))

    surface_by_path = {
        item["path"]: item for item in preflight["managed_surfaces"]
    }
    for change in normalized["candidate_changes"]:
        current = surface_by_path.get(change["path"])
        if current is None:
            raise ValidationError("instruction governance candidate change targets unmanaged surface")
        if change["before_digest"] != current["content_digest"]:
            raise ValidationError("instruction governance candidate before digest is stale")

    outcomes = list(results.values())
    if "FAIL" in outcomes:
        outcome = "REJECTED"
    elif "UNKNOWN" in outcomes or missing_mandatory:
        outcome = "HUMAN_REQUIRED"
    elif normalized["candidate_changes"]:
        outcome = "CANARY_READY"
    else:
        outcome = "NO_CHANGE"

    audit = {
        "audit_identity": preflight["audit_identity"],
        "evaluated_at": normalized["evaluated_at"],
        "authority": AUTHORITY,
        "outcome": outcome,
        "target_repository": preflight["target_repository"],
        "target_head": preflight["target_head"],
        "engineering_system_revision": preflight["engineering_system_revision"],
        "model_provider": preflight["model_provider"],
        "model_name": preflight["model_name"],
        "model_profile": preflight["model_profile"],
        "harness_id": preflight["harness_id"],
        "harness_revision": preflight["harness_revision"],
        "trigger_kind": preflight["trigger_kind"],
        "trigger_revision": preflight["trigger_revision"],
        "inventory_digest": preflight["inventory_digest"],
        "behavior_results": normalized["behavior_results"],
        "missing_mandatory_scenarios": missing_mandatory,
        "candidate_changes": normalized["candidate_changes"],
        "evaluation_ref": normalized["evaluation_ref"],
        "canonical_mutation": False,
    }
    assert_content_free(audit)

    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("instruction governance data root is not a directory")
    with data_root_write_lock(root):
        ledger = _load_ledger(root)
        if len(ledger["audits"]) >= _MAX_AUDITS:
            raise ValidationError("instruction governance ledger audit limit reached")
        if any(item.get("audit_identity") == audit["audit_identity"] for item in ledger["audits"]):
            return {"state": "DUPLICATE_NOOP", "authority": AUTHORITY, "audit": audit}
        ledger["audits"].append(audit)
        ledger["audits"].sort(key=lambda item: (str(item.get("evaluated_at", "")), str(item["audit_identity"])))
        atomic_write_text(root / FILENAME, json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    return {"state": "RECORDED", "authority": AUTHORITY, "audit": audit}

def instruction_governance_dashboard(data_root: Path, *, repo_root: Path) -> dict[str, object]:
    surfaces = discover_managed_surfaces(Path(repo_root))
    ledger = _load_ledger(Path(data_root))
    audits = list(ledger["audits"])
    outcome_counts = {outcome: 0 for outcome in sorted(OUTCOMES)}
    for audit in audits:
        outcome = audit.get("outcome")
        if outcome in outcome_counts:
            outcome_counts[outcome] += 1
    latest = audits[-1] if audits else None
    return {
        "state": "OBSERVED" if audits else "INVENTORY_ONLY",
        "authority": AUTHORITY,
        "managed_surface_count": len(surfaces),
        "managed_surfaces": surfaces,
        "inventory_digest": _canonical_digest(surfaces),
        "audit_count": len(audits),
        "outcome_counts": outcome_counts,
        "latest_audit": latest,
        "audits": audits[-50:],
        "mutation_authority": "NONE",
    }


ROUTING_AUTHORITY = "ROUTING_ADVISORY_ONLY"
ROUTE_ACTIONS = frozenset({"NO_CHANGE", "REJECTED", "HUMAN_REQUIRED", "CANARY_PR_REQUIRED"})


def _validated_routing_audit(payload: object) -> dict[str, object]:
    keys = {
        "audit_identity", "evaluated_at", "authority", "outcome",
        "target_repository", "target_head", "engineering_system_revision",
        "model_provider", "model_name", "model_profile", "harness_id",
        "harness_revision", "trigger_kind", "trigger_revision",
        "inventory_digest", "behavior_results", "missing_mandatory_scenarios",
        "candidate_changes", "evaluation_ref", "canonical_mutation",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValidationError("instruction governance stored audit schema is invalid")
    if (
        not isinstance(payload.get("audit_identity"), str)
        or _SHA256.fullmatch(str(payload["audit_identity"])) is None
        or payload.get("authority") != AUTHORITY
        or payload.get("outcome") not in OUTCOMES
        or payload.get("canonical_mutation") is not False
    ):
        raise ValidationError("instruction governance stored audit authority is invalid")
    repository = payload.get("target_repository")
    if not isinstance(repository, str) or _REPO.fullmatch(repository) is None:
        raise ValidationError("instruction governance stored audit repository is invalid")
    for key in ("target_head", "engineering_system_revision"):
        value = payload.get(key)
        if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
            raise ValidationError(f"instruction governance stored audit {key} is invalid")
    digest = payload.get("inventory_digest")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValidationError("instruction governance stored audit inventory digest is invalid")
    _utc(payload.get("evaluated_at"))
    _identity(payload.get("model_provider"), label="model_provider", provider=True)
    for key in (
        "model_name", "model_profile", "harness_id", "harness_revision",
        "trigger_revision", "evaluation_ref",
    ):
        _identity(payload.get(key), label=key)
    if payload.get("trigger_kind") not in TRIGGERS:
        raise ValidationError("instruction governance stored audit trigger_kind is invalid")

    behavior = payload.get("behavior_results")
    if not isinstance(behavior, list) or len(behavior) > 128:
        raise ValidationError("instruction governance stored audit behavior results are invalid")
    behavior_ids: list[str] = []
    for item in behavior:
        if (
            not isinstance(item, dict)
            or set(item) != {"scenario_id", "outcome"}
            or item.get("outcome") not in RESULTS
        ):
            raise ValidationError("instruction governance stored audit behavior result is invalid")
        behavior_ids.append(_identity(item.get("scenario_id"), label="behavior scenario id"))
    if len(behavior_ids) != len(set(behavior_ids)):
        raise ValidationError("instruction governance stored audit behavior results are duplicated")

    missing = payload.get("missing_mandatory_scenarios")
    if not isinstance(missing, list) or len(missing) > 128:
        raise ValidationError("instruction governance stored audit missing scenarios are invalid")
    missing_ids = [_identity(item, label="behavior scenario id") for item in missing]
    if len(missing_ids) != len(set(missing_ids)):
        raise ValidationError("instruction governance stored audit missing scenarios are duplicated")

    changes = payload.get("candidate_changes")
    if not isinstance(changes, list) or len(changes) > _MAX_CHANGES:
        raise ValidationError("instruction governance stored audit candidate changes are invalid")
    paths: list[str] = []
    for item in changes:
        if not isinstance(item, dict) or set(item) != {"path", "before_digest", "after_digest"}:
            raise ValidationError("instruction governance stored audit candidate change is invalid")
        path = _identity(item.get("path"), label="candidate path")
        before = item.get("before_digest")
        after = item.get("after_digest")
        if (
            not isinstance(before, str) or _SHA256.fullmatch(before) is None
            or not isinstance(after, str) or _SHA256.fullmatch(after) is None
            or before == after
        ):
            raise ValidationError("instruction governance stored audit candidate change digest is invalid")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise ValidationError("instruction governance stored audit candidate paths are duplicated")
    return dict(payload)


def _routing_fail_closed(
    root: Path,
    dashboard: dict[str, object],
    *,
    reason: str,
) -> dict[str, object]:
    current_head = _git(root, "rev-parse", "HEAD")
    return {
        "state": "UNKNOWN" if reason == "NO_AUDIT_EVIDENCE" else "STALE_OR_INVALID",
        "authority": ROUTING_AUTHORITY,
        "mutation_authority": "NONE",
        "route_action": "HUMAN_REQUIRED",
        "reasons": [reason],
        "audit_identity": None,
        "audit_outcome": None,
        "target_repository": _git_repository(root),
        "target_head": None,
        "current_head": current_head,
        "current_inventory_digest": dashboard["inventory_digest"],
        "audit_inventory_digest": None,
        "engineering_system_revision": None,
        "model_provider": None,
        "model_name": None,
        "model_profile": None,
        "harness_id": None,
        "harness_revision": None,
        "evaluation_ref": None,
        "candidate_changes": [],
        "behavior_results": [],
        "next_effect": "NONE",
    }


def instruction_governance_routing(
    data_root: Path,
    *,
    repo_root: Path,
) -> dict[str, object]:
    """Derive one current non-mutating routing decision from the latest audit."""
    root = Path(repo_root).resolve()
    dashboard = instruction_governance_dashboard(Path(data_root), repo_root=root)
    latest = dashboard["latest_audit"]
    if not isinstance(latest, dict):
        return _routing_fail_closed(root, dashboard, reason="NO_AUDIT_EVIDENCE")
    try:
        latest = _validated_routing_audit(latest)
    except ValidationError:
        return _routing_fail_closed(root, dashboard, reason="AUDIT_EVIDENCE_INVALID")

    reasons: list[str] = []
    current_head = _git(root, "rev-parse", "HEAD")
    current_inventory = str(dashboard["inventory_digest"])
    if latest.get("target_head") != current_head:
        reasons.append("TARGET_HEAD_STALE")
    if latest.get("inventory_digest") != current_inventory:
        reasons.append("MANAGED_INVENTORY_STALE")

    outcome = latest.get("outcome")
    changes = latest.get("candidate_changes")
    if not isinstance(changes, list):
        reasons.append("CANDIDATE_CHANGE_STATE_INVALID")
        changes = []
    if outcome == "CANARY_READY" and not changes:
        reasons.append("CANARY_READY_WITHOUT_CHANGES")
    if outcome == "NO_CHANGE" and changes:
        reasons.append("NO_CHANGE_WITH_CANDIDATE_CHANGES")

    route_map = {
        "NO_CHANGE": "NO_CHANGE",
        "REJECTED": "REJECTED",
        "HUMAN_REQUIRED": "HUMAN_REQUIRED",
        "CANARY_READY": "CANARY_PR_REQUIRED",
    }
    route_action = route_map.get(str(outcome), "HUMAN_REQUIRED")
    if route_action == "HUMAN_REQUIRED" and outcome not in OUTCOMES:
        reasons.append("AUDIT_OUTCOME_INVALID")
    if reasons:
        route_action = "HUMAN_REQUIRED"

    result = {
        "state": "CURRENT" if not reasons else "STALE_OR_INVALID",
        "authority": ROUTING_AUTHORITY,
        "mutation_authority": "NONE",
        "route_action": route_action,
        "reasons": reasons,
        "audit_identity": latest.get("audit_identity"),
        "audit_outcome": outcome,
        "target_repository": latest.get("target_repository"),
        "target_head": latest.get("target_head"),
        "current_head": current_head,
        "current_inventory_digest": current_inventory,
        "audit_inventory_digest": latest.get("inventory_digest"),
        "engineering_system_revision": latest.get("engineering_system_revision"),
        "model_provider": latest.get("model_provider"),
        "model_name": latest.get("model_name"),
        "model_profile": latest.get("model_profile"),
        "harness_id": latest.get("harness_id"),
        "harness_revision": latest.get("harness_revision"),
        "evaluation_ref": latest.get("evaluation_ref"),
        "candidate_changes": list(changes),
        "behavior_results": list(latest.get("behavior_results") or []),
        "next_effect": (
            "NONE"
            if route_action in {"NO_CHANGE", "REJECTED", "HUMAN_REQUIRED"}
            else "SEPARATE_CANARY_AND_PR_GOVERNANCE_REQUIRED"
        ),
    }
    assert_content_free(result)
    return result

DISPOSITION_AUTHORITY = "PR_HANDOFF_ADVISORY_ONLY"
DISPOSITION_KIND = "instruction_governance_disposition"
DISPOSITION_LEDGER_KIND = "instruction_governance_disposition_ledger"
DISPOSITION_FILENAME = "instruction-governance-dispositions.json"
DISPOSITIONS = frozenset({"NO_CHANGE", "PR_CANDIDATE", "HUMAN_REQUIRED", "REJECTED"})
_MAX_DISPOSITIONS = 500


def _empty_disposition_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DISPOSITION_LEDGER_KIND,
        "authority": DISPOSITION_AUTHORITY,
        "dispositions": [],
    }


def _validate_pr_handoff(payload: object) -> dict[str, object] | None:
    if payload is None:
        return None
    keys = {
        "kind", "target_repository", "target_head", "audit_identity",
        "engineering_system_revision", "evaluation_ref", "managed_changes",
        "required_route", "merge_authority", "release_authority", "handoff_digest",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValidationError("instruction governance PR handoff schema is invalid")
    if (
        payload.get("kind") != "ordinary_pr_adoption_handoff"
        or payload.get("required_route") != "ORDINARY_PR_OR_MANAGED_ADOPTION"
        or payload.get("merge_authority") != "NONE"
        or payload.get("release_authority") != "NONE"
    ):
        raise ValidationError("instruction governance PR handoff authority is invalid")
    repository = payload.get("target_repository")
    if not isinstance(repository, str) or _REPO.fullmatch(repository) is None:
        raise ValidationError("instruction governance PR handoff repository is invalid")
    for key in ("target_head", "engineering_system_revision"):
        value = payload.get(key)
        if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
            raise ValidationError(f"instruction governance PR handoff {key} is invalid")
    audit_identity = payload.get("audit_identity")
    if not isinstance(audit_identity, str) or _SHA256.fullmatch(audit_identity) is None:
        raise ValidationError("instruction governance PR handoff audit identity is invalid")
    _identity(payload.get("evaluation_ref"), label="evaluation_ref")
    changes = payload.get("managed_changes")
    if not isinstance(changes, list) or not changes or len(changes) > _MAX_CHANGES:
        raise ValidationError("instruction governance PR handoff managed changes are invalid")
    normalized_changes = []
    seen_paths: set[str] = set()
    for item in changes:
        if not isinstance(item, dict) or set(item) != {"path", "before_digest", "after_digest"}:
            raise ValidationError("instruction governance PR handoff managed change is invalid")
        path = _identity(item.get("path"), label="candidate path")
        before = item.get("before_digest")
        after = item.get("after_digest")
        if (
            path in seen_paths
            or not isinstance(before, str) or _SHA256.fullmatch(before) is None
            or not isinstance(after, str) or _SHA256.fullmatch(after) is None
            or before == after
        ):
            raise ValidationError("instruction governance PR handoff managed change is invalid")
        seen_paths.add(path)
        normalized_changes.append(
            {"path": path, "before_digest": before, "after_digest": after}
        )
    normalized_changes.sort(key=lambda item: str(item["path"]))
    digest = payload.get("handoff_digest")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValidationError("instruction governance PR handoff digest is invalid")
    body = {key: value for key, value in payload.items() if key != "handoff_digest"}
    if _canonical_digest(body) != digest:
        raise ValidationError("instruction governance PR handoff digest mismatch")
    return {**body, "managed_changes": normalized_changes, "handoff_digest": digest}


def validate_instruction_governance_disposition(payload: object) -> dict[str, object]:
    keys = {
        "schema_version", "kind", "authority", "mutation_authority",
        "disposition", "reasons", "audit_identity", "audit_outcome",
        "target_repository", "target_head", "inventory_digest",
        "engineering_system_revision", "model_provider", "model_name",
        "model_profile", "harness_id", "harness_revision", "evaluation_ref",
        "candidate_changes", "behavior_results", "missing_mandatory_scenarios",
        "pr_handoff", "disposition_digest",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValidationError("instruction governance disposition schema is invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("kind") != DISPOSITION_KIND
        or payload.get("authority") != DISPOSITION_AUTHORITY
        or payload.get("mutation_authority") != "NONE"
        or payload.get("disposition") not in DISPOSITIONS
        or payload.get("audit_outcome") not in OUTCOMES
    ):
        raise ValidationError("instruction governance disposition authority/state is invalid")
    audit_identity = payload.get("audit_identity")
    if not isinstance(audit_identity, str) or _SHA256.fullmatch(audit_identity) is None:
        raise ValidationError("instruction governance disposition audit identity is invalid")
    repository = payload.get("target_repository")
    if not isinstance(repository, str) or _REPO.fullmatch(repository) is None:
        raise ValidationError("instruction governance disposition repository is invalid")
    for key in ("target_head", "engineering_system_revision"):
        value = payload.get(key)
        if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
            raise ValidationError(f"instruction governance disposition {key} is invalid")
    inventory = payload.get("inventory_digest")
    if not isinstance(inventory, str) or _SHA256.fullmatch(inventory) is None:
        raise ValidationError("instruction governance disposition inventory digest is invalid")
    _identity(payload.get("model_provider"), label="model_provider", provider=True)
    for key in (
        "model_name", "model_profile", "harness_id", "harness_revision",
        "evaluation_ref",
    ):
        _identity(payload.get(key), label=key)
    reasons = payload.get("reasons")
    if (
        not isinstance(reasons, list)
        or len(reasons) > 16
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in reasons)
        or len(reasons) != len(set(reasons))
    ):
        raise ValidationError("instruction governance disposition reasons are invalid")
    changes = payload.get("candidate_changes")
    behavior = payload.get("behavior_results")
    missing = payload.get("missing_mandatory_scenarios")
    if not isinstance(changes, list) or len(changes) > _MAX_CHANGES:
        raise ValidationError("instruction governance disposition candidate changes are invalid")
    normalized_changes = []
    seen_paths: set[str] = set()
    for item in changes:
        if not isinstance(item, dict) or set(item) != {"path", "before_digest", "after_digest"}:
            raise ValidationError("instruction governance disposition candidate change is invalid")
        path = _identity(item.get("path"), label="candidate path")
        before = item.get("before_digest")
        after = item.get("after_digest")
        if (
            path in seen_paths
            or not isinstance(before, str) or _SHA256.fullmatch(before) is None
            or not isinstance(after, str) or _SHA256.fullmatch(after) is None
            or before == after
        ):
            raise ValidationError("instruction governance disposition candidate change is invalid")
        seen_paths.add(path)
        normalized_changes.append(
            {"path": path, "before_digest": before, "after_digest": after}
        )
    normalized_changes.sort(key=lambda item: str(item["path"]))

    if not isinstance(behavior, list) or len(behavior) > 128:
        raise ValidationError("instruction governance disposition behavior results are invalid")
    normalized_behavior = []
    seen_scenarios: set[str] = set()
    for item in behavior:
        if (
            not isinstance(item, dict)
            or set(item) != {"scenario_id", "outcome"}
            or item.get("outcome") not in RESULTS
        ):
            raise ValidationError("instruction governance disposition behavior result is invalid")
        scenario_id = _identity(item.get("scenario_id"), label="behavior scenario id")
        if scenario_id in seen_scenarios:
            raise ValidationError("instruction governance disposition behavior result is duplicated")
        seen_scenarios.add(scenario_id)
        normalized_behavior.append(
            {"scenario_id": scenario_id, "outcome": item["outcome"]}
        )
    normalized_behavior.sort(key=lambda item: str(item["scenario_id"]))

    if not isinstance(missing, list) or len(missing) > 128:
        raise ValidationError("instruction governance disposition missing scenarios are invalid")
    normalized_missing = [_identity(item, label="behavior scenario id") for item in missing]
    if len(normalized_missing) != len(set(normalized_missing)):
        raise ValidationError("instruction governance disposition missing scenarios are duplicated")
    normalized_missing.sort()

    handoff = _validate_pr_handoff(payload.get("pr_handoff"))
    if payload["disposition"] == "PR_CANDIDATE":
        if handoff is None:
            raise ValidationError("instruction governance PR candidate requires handoff")
        if not normalized_changes or not normalized_behavior:
            raise ValidationError("instruction governance PR candidate evidence is incomplete")
        if any(item["outcome"] != "PASS" for item in normalized_behavior):
            raise ValidationError("instruction governance PR candidate behavior is not all PASS")
        if normalized_missing:
            raise ValidationError("instruction governance PR candidate has missing mandatory scenarios")
        expected_handoff_binding = {
            "target_repository": payload["target_repository"],
            "target_head": payload["target_head"],
            "audit_identity": payload["audit_identity"],
            "engineering_system_revision": payload["engineering_system_revision"],
            "evaluation_ref": payload["evaluation_ref"],
            "managed_changes": normalized_changes,
        }
        if any(handoff[key] != value for key, value in expected_handoff_binding.items()):
            raise ValidationError("instruction governance PR handoff binding is invalid")
    elif handoff is not None:
        raise ValidationError("non-PR disposition cannot contain PR handoff")
    digest = payload.get("disposition_digest")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValidationError("instruction governance disposition digest is invalid")
    body = {key: value for key, value in payload.items() if key != "disposition_digest"}
    if _canonical_digest(body) != digest:
        raise ValidationError("instruction governance disposition digest mismatch")
    assert_content_free(payload)
    return dict(payload)


def _load_disposition_ledger(data_root: Path) -> dict[str, object]:
    path = Path(data_root) / DISPOSITION_FILENAME
    if not path.exists():
        return _empty_disposition_ledger()
    if path.is_symlink() or not path.is_file():
        raise ValidationError("instruction governance disposition ledger path is unsafe")
    payload = _load_json(path, label="disposition ledger", max_bytes=_MAX_LEDGER_BYTES)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "authority", "dispositions"}
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != DISPOSITION_LEDGER_KIND
        or payload.get("authority") != DISPOSITION_AUTHORITY
        or not isinstance(payload.get("dispositions"), list)
        or len(payload["dispositions"]) > _MAX_DISPOSITIONS
    ):
        raise ValidationError("instruction governance disposition ledger schema is invalid")
    normalized = []
    seen: set[str] = set()
    for item in payload["dispositions"]:
        disposition = validate_instruction_governance_disposition(item)
        identity = str(disposition["audit_identity"])
        if identity in seen:
            raise ValidationError("instruction governance disposition audit identity is duplicated")
        seen.add(identity)
        normalized.append(disposition)
    return {**_empty_disposition_ledger(), "dispositions": normalized}


def _audit_by_identity(data_root: Path, audit_identity: str) -> dict[str, object]:
    if not isinstance(audit_identity, str) or _SHA256.fullmatch(audit_identity) is None:
        raise ValidationError("instruction governance disposition audit identity is invalid")
    ledger = _load_ledger(Path(data_root))
    matches = [
        item for item in ledger["audits"]
        if item.get("audit_identity") == audit_identity
    ]
    if len(matches) != 1:
        raise ValidationError("instruction governance disposition audit is not found")
    return _validated_routing_audit(matches[0])


def build_instruction_governance_disposition(
    data_root: Path,
    *,
    repo_root: Path,
    audit_identity: str,
) -> dict[str, object]:
    """Build one deterministic audit-bound disposition without mutation authority."""
    root = Path(repo_root).resolve()
    audit = _audit_by_identity(Path(data_root), audit_identity)
    current_dashboard = instruction_governance_dashboard(Path(data_root), repo_root=root)
    current_head = _git(root, "rev-parse", "HEAD")
    current_inventory = str(current_dashboard["inventory_digest"])
    reasons: list[str] = []
    if audit["target_repository"] != _git_repository(root):
        reasons.append("TARGET_REPOSITORY_MISMATCH")
    if audit["target_head"] != current_head:
        reasons.append("TARGET_HEAD_STALE")
    if audit["inventory_digest"] != current_inventory:
        reasons.append("MANAGED_INVENTORY_STALE")

    outcome = str(audit["outcome"])
    behavior = list(audit["behavior_results"])
    changes = list(audit["candidate_changes"])
    missing = list(audit["missing_mandatory_scenarios"])
    disposition_map = {
        "NO_CHANGE": "NO_CHANGE",
        "CANARY_READY": "PR_CANDIDATE",
        "HUMAN_REQUIRED": "HUMAN_REQUIRED",
        "REJECTED": "REJECTED",
    }
    disposition = disposition_map[outcome]

    if disposition == "PR_CANDIDATE":
        if not changes:
            reasons.append("PR_CANDIDATE_WITHOUT_CHANGES")
        if not behavior:
            reasons.append("PR_CANDIDATE_WITHOUT_BEHAVIOR_EVIDENCE")
        if any(
            not isinstance(item, dict) or item.get("outcome") != "PASS"
            for item in behavior
        ):
            reasons.append("PR_CANDIDATE_BEHAVIOR_NOT_ALL_PASS")
        if missing:
            reasons.append("PR_CANDIDATE_MISSING_MANDATORY_SCENARIOS")
    if reasons:
        disposition = "HUMAN_REQUIRED"

    handoff = None
    if disposition == "PR_CANDIDATE":
        handoff_body = {
            "kind": "ordinary_pr_adoption_handoff",
            "target_repository": audit["target_repository"],
            "target_head": audit["target_head"],
            "audit_identity": audit["audit_identity"],
            "engineering_system_revision": audit["engineering_system_revision"],
            "evaluation_ref": audit["evaluation_ref"],
            "managed_changes": changes,
            "required_route": "ORDINARY_PR_OR_MANAGED_ADOPTION",
            "merge_authority": "NONE",
            "release_authority": "NONE",
        }
        handoff = {
            **handoff_body,
            "handoff_digest": _canonical_digest(handoff_body),
        }

    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": DISPOSITION_KIND,
        "authority": DISPOSITION_AUTHORITY,
        "mutation_authority": "NONE",
        "disposition": disposition,
        "reasons": reasons,
        "audit_identity": audit["audit_identity"],
        "audit_outcome": audit["outcome"],
        "target_repository": audit["target_repository"],
        "target_head": audit["target_head"],
        "inventory_digest": audit["inventory_digest"],
        "engineering_system_revision": audit["engineering_system_revision"],
        "model_provider": audit["model_provider"],
        "model_name": audit["model_name"],
        "model_profile": audit["model_profile"],
        "harness_id": audit["harness_id"],
        "harness_revision": audit["harness_revision"],
        "evaluation_ref": audit["evaluation_ref"],
        "candidate_changes": changes,
        "behavior_results": behavior,
        "missing_mandatory_scenarios": missing,
        "pr_handoff": handoff,
    }
    result = {**body, "disposition_digest": _canonical_digest(body)}
    return validate_instruction_governance_disposition(result)


def publish_instruction_governance_disposition(
    data_root: Path,
    *,
    repo_root: Path,
    audit_identity: str,
) -> dict[str, object]:
    """Publish one derived disposition; an exact replay is a deterministic no-op."""
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("instruction governance disposition data root is not a directory")
    disposition = build_instruction_governance_disposition(
        root,
        repo_root=repo_root,
        audit_identity=audit_identity,
    )
    with data_root_write_lock(root):
        ledger = _load_disposition_ledger(root)
        existing = next(
            (
                item for item in ledger["dispositions"]
                if item["audit_identity"] == disposition["audit_identity"]
            ),
            None,
        )
        if existing is not None:
            if existing["disposition_digest"] != disposition["disposition_digest"]:
                raise ValidationError("instruction governance disposition replay drifted")
            return {
                "state": "DUPLICATE_NOOP",
                "authority": DISPOSITION_AUTHORITY,
                "disposition": existing,
            }
        if len(ledger["dispositions"]) >= _MAX_DISPOSITIONS:
            raise ValidationError("instruction governance disposition ledger limit reached")
        ledger["dispositions"].append(disposition)
        atomic_write_text(
            root / DISPOSITION_FILENAME,
            json.dumps(ledger, indent=2, sort_keys=True) + "\n",
        )
    return {
        "state": "PUBLISHED",
        "authority": DISPOSITION_AUTHORITY,
        "disposition": disposition,
    }


def instruction_governance_disposition_dashboard(
    data_root: Path,
    *,
    repo_root: Path,
) -> dict[str, object]:
    """Read the derived handoff ledger and rebind every entry to source audit evidence."""
    root = Path(data_root)
    ledger = _load_disposition_ledger(root)
    dispositions = list(ledger["dispositions"])
    for item in dispositions:
        source = _audit_by_identity(root, str(item["audit_identity"]))
        for key in (
            "audit_outcome", "target_repository", "target_head", "inventory_digest",
            "engineering_system_revision", "model_provider", "model_name",
            "model_profile", "harness_id", "harness_revision", "evaluation_ref",
            "candidate_changes", "behavior_results", "missing_mandatory_scenarios",
        ):
            source_key = "outcome" if key == "audit_outcome" else key
            if item[key] != source[source_key]:
                raise ValidationError("instruction governance disposition source binding is invalid")
    counts = {name: 0 for name in sorted(DISPOSITIONS)}
    for item in dispositions:
        counts[str(item["disposition"])] += 1
    latest = dispositions[-1] if dispositions else None
    current_binding = None
    if isinstance(latest, dict):
        current = build_instruction_governance_disposition(
            root,
            repo_root=repo_root,
            audit_identity=str(latest["audit_identity"]),
        )
        current_binding = (
            "CURRENT"
            if current["disposition_digest"] == latest["disposition_digest"]
            else "STALE"
        )
    return {
        "state": "OBSERVED" if dispositions else "UNKNOWN",
        "authority": DISPOSITION_AUTHORITY,
        "mutation_authority": "NONE",
        "disposition_count": len(dispositions),
        "disposition_counts": counts,
        "binding_state": current_binding or "UNKNOWN",
        "latest_disposition": latest,
        "dispositions": dispositions[-50:],
    }
