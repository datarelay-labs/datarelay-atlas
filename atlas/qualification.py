"""Prod-atlas qualification harness.

Design gate: ADR-0013. The Engineering System pin remains v1.6.5 baseline
14150e424c922ff3a930b45dcf31d3a3d3ba28b2. Local success is not a production
pass, and this module does not change the release contract flags.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import ssl
import stat
import subprocess
import sys
import tempfile
from http.client import HTTPSConnection
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from atlas.data_protection import backup_data_root, restore_test
from atlas.github_sync import FetchedSource, FetchFn, fetch_github_file
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.ops import data_root_runtime_ready
from atlas.provenance import ValidationError
from atlas.schema_compat import rollback_data_root, upgrade_data_root
from atlas.secrets import contains_unsafe_secret
from atlas.service import AtlasService

PIN_VERSION = "1.6.5"
PIN_BASELINE = "14150e424c922ff3a930b45dcf31d3a3d3ba28b2"
PROD_HOST = "mcp.atlas.datarelay.run"
PROD_ENDPOINT = "https://mcp.atlas.datarelay.run"
PROD_PROJECT_ID = "datarelay-atlas"
PROD_REPOSITORY = "datarelay-labs/datarelay-atlas"
PROD_SOURCE_ID = "product-charter"
PROD_SOURCE_PATH = "docs/product/PRODUCT-CHARTER.md"
PROD_QUERY = "Engineering Knowledge & Lifecycle Platform"
_PROJECT_ID = "qual"
_QUERY = "qualification-marker"
_REVISION = "rev-qual-1"
_SHELLS = {"sh", "bash", "dash", "zsh", "sudo"}
_SMOKE_BODY = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'

SmokeClient = Callable[[str, str, bytes | None], tuple[int, bytes]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prod-qualification")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("public-smoke")
    operational = sub.add_parser("operational-e2e")
    operational.add_argument("--mode", choices=("local", "prod"), default="local")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    if args.command == "public-smoke":
        evidence = run_public_smoke(os.environ, repo_root=repo_root)
    else:
        evidence = run_operational_e2e(args.mode, os.environ, repo_root=repo_root)
    sys.stdout.write(json.dumps(evidence, sort_keys=True) + "\n")
    if evidence["status"] == "PASS":
        return 0
    if evidence["status"] == "FAIL_CLOSED":
        return 2
    return 1


def run_operational_e2e(
    mode: str,
    environ: dict[str, str],
    *,
    repo_root: Path,
    smoke_client: SmokeClient | None = None,
    fetch: FetchFn | None = None,
) -> dict:
    if mode == "local":
        return _local_operational_e2e(repo_root)
    if mode == "prod":
        return _prod_operational_e2e(
            environ,
            repo_root=repo_root,
            smoke_client=smoke_client,
            fetch=fetch,
        )
    return _evidence(
        "operational-e2e",
        mode,
        "FAIL_CLOSED",
        "qualification mode is unsupported",
        [],
    )


def run_public_smoke(
    environ: dict[str, str],
    *,
    repo_root: Path,
    smoke_client: SmokeClient | None = None,
) -> dict:
    pin_reason = _pin_reason(repo_root)
    if pin_reason:
        return _evidence("public-smoke", "endpoint", "FAIL_CLOSED", pin_reason, [])
    confirmed = environ.get("ATLAS_QUALIFICATION_CONFIRM_PROD") == "yes"
    base = (environ.get("ATLAS_PUBLIC_BASE_URL") or "").strip()
    if not base:
        return _evidence(
            "public-smoke",
            "endpoint",
            "FAIL_CLOSED",
            "ATLAS_PUBLIC_BASE_URL is absent",
            [],
        )
    parsed = _parse_base_url(base)
    if isinstance(parsed, str):
        return _evidence("public-smoke", "endpoint", "FAIL_CLOSED", parsed, [])
    prod_host = parsed.hostname == PROD_HOST and parsed.port in (None, 443)
    if confirmed and not prod_host:
        return _evidence(
            "public-smoke",
            "endpoint",
            "FAIL_CLOSED",
            "public host is not the prod-atlas endpoint",
            [],
        )
    ca_file = (environ.get("ATLAS_PUBLIC_SMOKE_CA_FILE") or "").strip()
    if ca_file and not Path(ca_file).is_file():
        return _evidence(
            "public-smoke",
            "endpoint",
            "FAIL_CLOSED",
            "ATLAS_PUBLIC_SMOKE_CA_FILE is unreadable",
            [],
        )
    client = smoke_client or _https_client(Path(ca_file) if ca_file else None)
    try:
        health_status, health_body = client("GET", _url(parsed, "/healthz"), None)
        mcp_status, _mcp_body = client("POST", _url(parsed, "/mcp"), _SMOKE_BODY)
    except OSError:
        return _evidence(
            "public-smoke",
            "endpoint",
            "FAIL_CLOSED",
            "endpoint unreachable",
            [],
        )
    steps = [
        _step("health", "PASS" if _health_ready(health_status, health_body) else "FAIL"),
        _step("mcp_anonymous", "PASS" if mcp_status == 401 else "FAIL"),
    ]
    if any(step["status"] != "PASS" for step in steps):
        return _evidence("public-smoke", "endpoint", "FAIL", "https surface check failed", steps)
    return _evidence(
        "public-smoke",
        "endpoint",
        "PASS",
        "https surface check passed",
        steps,
        production_claim=confirmed and prod_host,
    )


def _local_operational_e2e(repo_root: Path) -> dict:
    pin_reason = _pin_reason(repo_root)
    if pin_reason:
        return _evidence("operational-e2e", "local-deterministic", "FAIL_CLOSED", pin_reason, [])
    steps: list[dict[str, str]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir(mode=0o700)
            service = AtlasService(data)
            service.register_project(
                project_id=_PROJECT_ID,
                repository="datarelay-labs/qual-fixture",
            )
            service.add_source(
                _PROJECT_ID,
                source_id="qual",
                source_path="docs/qual.md",
                title="Qualification",
            )
            service.sync_project(_PROJECT_ID, fetch=_fetch)
            steps.append(_step("register_sync", "PASS"))
            hits = service.search(_PROJECT_ID, _QUERY, limit=1)
            revision = _attributable(hits)
            if revision is None:
                steps.append(_step("retrieval", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    "local-deterministic",
                    "FAIL",
                    "retrieval is not attributable",
                    steps,
                )
            steps.append(_step("retrieval", "PASS", source_revision=revision))
            if not _mcp_query(service):
                steps.append(_step("mcp_query", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    "local-deterministic",
                    "FAIL",
                    "mcp query is not attributable",
                    steps,
                )
            steps.append(_step("mcp_query", "PASS", source_revision=revision))
            recovered = _fresh_search(data, repo_root)
            if recovered != revision:
                steps.append(_step("restart_recovery", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    "local-deterministic",
                    "FAIL",
                    "restart recovery did not preserve provenance",
                    steps,
                )
            steps.append(_step("restart_recovery", "PASS", source_revision=revision))
            backup = root / "backup"
            restored = root / "restored"
            backup_data_root(data, backup)
            restore_test(backup, restored)
            if _fresh_search(restored, repo_root) != revision:
                steps.append(_step("backup_restore", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    "local-deterministic",
                    "FAIL",
                    "restore-test did not preserve provenance",
                    steps,
                )
            steps.append(_step("backup_restore", "PASS"))
            upgrade = upgrade_data_root(data)
            rollback = rollback_data_root(data, repo_root)
            if upgrade.get("status") != "ok" or rollback.get("status") != "ok":
                steps.append(_step("upgrade_rollback", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    "local-deterministic",
                    "FAIL",
                    "upgrade or rollback hook failed",
                    steps,
                )
            if upgrade.get("rewritten") is not False or rollback.get("rewritten") is not False:
                steps.append(_step("upgrade_rollback", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    "local-deterministic",
                    "FAIL",
                    "upgrade or rollback hook failed",
                    steps,
                )
            steps.append(_step("upgrade_rollback", "PASS"))
    except (OSError, ValidationError, subprocess.SubprocessError):
        steps.append(_step("harness", "FAIL"))
        return _evidence(
            "operational-e2e",
            "local-deterministic",
            "FAIL",
            "local qualification journey failed",
            steps,
        )
    return _evidence(
        "operational-e2e",
        "local-deterministic",
        "PASS",
        "local qualification journey passed",
        steps,
    )


def _prod_operational_e2e(
    environ: dict[str, str],
    *,
    repo_root: Path,
    smoke_client: SmokeClient | None,
    fetch: FetchFn | None,
) -> dict:
    pin_reason = _pin_reason(repo_root)
    if pin_reason:
        return _evidence("operational-e2e", "prod", "FAIL_CLOSED", pin_reason, [])
    missing = _missing_prod_inputs(environ)
    if missing:
        names = ", ".join(missing)
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            f"prod inputs absent: {names}",
            [],
        )
    token, token_reason = _read_github_token(Path(environ["ATLAS_QUALIFICATION_GITHUB_TOKEN_FILE"]))
    if token_reason or token is None:
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            token_reason or "github credential file is unreadable",
            [],
        )
    evidence_path = Path(environ["ATLAS_CURSOR_MCP_EVIDENCE"])
    static_reason = _cursor_static_reason(evidence_path)
    if static_reason:
        return _evidence("operational-e2e", "prod", "FAIL_CLOSED", static_reason, [])
    restart = _restart_argv(environ["ATLAS_QUALIFICATION_RESTART_COMMAND"])
    if restart is None:
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            "restart command is not a direct argv",
            [],
        )
    data_root = Path(environ["ATLAS_DATA_ROOT"])
    backup_dest = Path(environ["ATLAS_BACKUP_DEST"])
    restore_dest = Path(environ["ATLAS_RESTORE_PROOF_DEST"])
    rollback_target = Path(environ["ATLAS_ROLLBACK_TARGET"])
    if not data_root.is_dir() or data_root.is_symlink():
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            "ATLAS_DATA_ROOT is not a directory",
            [],
        )
    if not (rollback_target / "atlas" / "schema_compat.py").is_file():
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            "ATLAS_ROLLBACK_TARGET is not an Atlas tree",
            [],
        )
    smoke = run_public_smoke(environ, repo_root=repo_root, smoke_client=smoke_client)
    steps = [_step("public_smoke", smoke["status"])]
    if smoke.get("production_claim") is not True:
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            "public smoke did not claim the prod-atlas endpoint",
            steps,
        )
    service = AtlasService(data_root)
    try:
        _ensure_prod_registration(service)
    except ValidationError as exc:
        reason = str(exc)
        if reason not in {
            "conflicting project registration",
            "conflicting source registration",
        }:
            reason = "prod registration failed"
        steps.append(_step("register_source", "FAIL"))
        return _evidence("operational-e2e", "prod", "FAIL_CLOSED", reason, steps)
    steps.append(_step("register_source", "PASS"))
    guarded = _guard_fetch(fetch or fetch_github_file, token)
    try:
        records = service.sync_project(PROD_PROJECT_ID, token=token, fetch=guarded)
    except (OSError, ValidationError, subprocess.SubprocessError):
        steps.append(_step("sync", "FAIL"))
        return _evidence("operational-e2e", "prod", "FAIL", "github sync failed", steps)
    revision = _synced_revision(records)
    if revision is None:
        steps.append(_step("sync", "FAIL"))
        return _evidence("operational-e2e", "prod", "FAIL", "github sync failed", steps)
    steps.append(_step("sync", "PASS", source_revision=revision))
    bound = _bound_hit(service.search(PROD_PROJECT_ID, PROD_QUERY, limit=8), revision)
    if bound is None:
        steps.append(_step("retrieval", "FAIL", source_revision=revision))
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL",
            "retrieval is not attributable",
            steps,
        )
    identity = bound["identity"]
    steps.append(_step("retrieval", "PASS", source_revision=revision, identity=identity))
    binding = _cursor_binding_reason(evidence_path, revision=revision, identity=identity)
    if binding:
        steps.append(_step("cursor_mcp", "FAIL", source_revision=revision, identity=identity))
        return _evidence("operational-e2e", "prod", "FAIL", binding, steps)
    steps.append(_step("cursor_mcp", "PASS", source_revision=revision, identity=identity))
    try:
        upgrade = upgrade_data_root(data_root)
        rollback = rollback_data_root(data_root, rollback_target)
        if (
            upgrade.get("status") != "ok"
            or rollback.get("status") != "ok"
            or upgrade.get("rewritten") is not False
            or rollback.get("rewritten") is not False
        ):
            steps.append(_step("upgrade_rollback", "FAIL"))
            return _evidence(
                "operational-e2e",
                "prod",
                "FAIL",
                "upgrade or rollback hook failed",
                steps,
            )
        steps.append(_step("upgrade_rollback", "PASS"))
        backup_data_root(data_root, backup_dest)
        restore_test(backup_dest, restore_dest)
        steps.append(_step("backup_restore", "PASS"))
        completed = subprocess.run(
            restart,
            check=False,
            capture_output=True,
            timeout=60,
            env=_scrubbed_env(),
        )
        if completed.returncode != 0:
            steps.append(_step("restart_recovery", "FAIL"))
            return _evidence(
                "operational-e2e",
                "prod",
                "FAIL",
                "restart command failed",
                steps,
            )
        steps.append(_step("restart_recovery", "PASS"))
    except (OSError, ValidationError, subprocess.SubprocessError):
        steps.append(_step("data_protection", "FAIL"))
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL",
            "prod qualification hooks failed",
            steps,
        )
    again = run_public_smoke(environ, repo_root=repo_root, smoke_client=smoke_client)
    steps.append(_step("public_smoke_after_restart", again["status"]))
    if again.get("production_claim") is not True:
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL",
            "public smoke failed after restart",
            steps,
        )
    recovered = _prod_fresh_hit(data_root, repo_root)
    if recovered != {"identity": identity, "source_revision": revision}:
        steps.append(_step("retrieval_after_restart", "FAIL"))
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL",
            "restart recovery did not preserve provenance",
            steps,
        )
    steps.append(
        _step(
            "retrieval_after_restart",
            "PASS",
            source_revision=revision,
            identity=identity,
        )
    )
    rebound = _cursor_binding_reason(evidence_path, revision=revision, identity=identity)
    if rebound:
        steps.append(_step("cursor_mcp_after_restart", "FAIL"))
        return _evidence("operational-e2e", "prod", "FAIL", rebound, steps)
    steps.append(
        _step(
            "cursor_mcp_after_restart",
            "PASS",
            source_revision=revision,
            identity=identity,
        )
    )
    return _evidence(
        "operational-e2e",
        "prod",
        "PASS",
        "prod qualification journey passed",
        steps,
        production_claim=True,
    )


def _fetch(source, token):  # noqa: ARG001
    if token:
        raise ValidationError("local qualification sync does not accept a token")
    return FetchedSource(content="qualification-marker\n", source_revision=_REVISION)


def _mcp_query(service: AtlasService) -> bool:
    tools = AtlasContextTools(retriever_factory=service.project_retriever)
    found = tools.call(
        "search_project",
        {"project_id": _PROJECT_ID, "query": _QUERY, "limit": 1},
        scopes=default_read_scopes(),
    )
    if not found.ok or not found.data:
        return False
    hit = found.data[0]
    provenance = hit.get("provenance") if isinstance(hit, dict) else None
    if not isinstance(provenance, dict) or provenance.get("source_revision") != _REVISION:
        return False
    proven = tools.call(
        "get_provenance",
        {"project_id": _PROJECT_ID, "identity": hit.get("identity")},
        scopes=default_read_scopes(),
    )
    return (
        proven.ok
        and isinstance(proven.data, dict)
        and proven.data.get("source_revision") == _REVISION
    )


def _fresh_search(data_root: Path, repo_root: Path) -> str | None:
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            _FRESH_SEARCH,
            str(data_root),
            _PROJECT_ID,
            _QUERY,
        ],
        cwd=str(repo_root),
        env={
            "PYTHONPATH": str(repo_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if payload.get("ready") is not True or payload.get("hits") != 1:
        return None
    revision = payload.get("source_revision")
    if revision != _REVISION:
        return None
    return revision


def _attributable(hits: list) -> str | None:
    if len(hits) != 1:
        return None
    provenance = hits[0].provenance
    if not isinstance(provenance, dict):
        return None
    if provenance.get("repository") != "datarelay-labs/qual-fixture":
        return None
    if provenance.get("source_path") != "docs/qual.md":
        return None
    revision = provenance.get("source_revision")
    if revision != _REVISION:
        return None
    return revision


def _ensure_prod_registration(service: AtlasService) -> None:
    existing = next(
        (project for project in service.list_projects() if project.project_id == PROD_PROJECT_ID),
        None,
    )
    if existing is None:
        service.register_project(
            project_id=PROD_PROJECT_ID,
            repository=PROD_REPOSITORY,
            display_name="DataRelay Atlas",
        )
    elif (
        existing.repository != PROD_REPOSITORY
        or existing.default_ref != "main"
        or not existing.enabled
    ):
        raise ValidationError("conflicting project registration")
    sources = service.list_sources(PROD_PROJECT_ID)
    matched = next((source for source in sources if source.source_id == PROD_SOURCE_ID), None)
    if matched is None:
        service.add_source(
            PROD_PROJECT_ID,
            source_id=PROD_SOURCE_ID,
            source_path=PROD_SOURCE_PATH,
            title="Product Charter",
        )
        return
    if (
        matched.source_path != PROD_SOURCE_PATH
        or not matched.enabled
        or matched.provider != "github"
        or (matched.ref is not None and matched.ref != "main")
    ):
        raise ValidationError("conflicting source registration")


def _guard_fetch(fetch: FetchFn, token: str) -> FetchFn:
    def wrapped(source, supplied):  # noqa: ARG001
        try:
            return fetch(source, token)
        except ValidationError as exc:
            if token in str(exc):
                raise ValidationError("github sync failed") from None
            raise
        except Exception:
            raise ValidationError("github sync failed") from None

    return wrapped


def _synced_revision(records: list) -> str | None:
    matched = [record for record in records if record.source_id == PROD_SOURCE_ID]
    if len(matched) != 1:
        return None
    record = matched[0]
    if record.sync_state not in {"success", "unchanged"}:
        return None
    revision = record.source_revision
    if not isinstance(revision, str) or not revision.strip():
        return None
    return revision


def _bound_hit(hits: list, revision: str) -> dict[str, str] | None:
    matches: list[dict[str, str]] = []
    for hit in hits:
        provenance = hit.provenance if isinstance(hit.provenance, dict) else {}
        identity = hit.identity if isinstance(hit.identity, str) else ""
        if (
            provenance.get("project_id") == PROD_PROJECT_ID
            and provenance.get("repository") == PROD_REPOSITORY
            and provenance.get("source_path") == PROD_SOURCE_PATH
            and provenance.get("source_revision") == revision
            and identity
        ):
            matches.append({"identity": identity, "source_revision": revision})
    if len(matches) != 1:
        return None
    return matches[0]


def _read_github_token(path: Path) -> tuple[str | None, str | None]:
    if path.is_symlink() or not path.is_file():
        return None, "github credential file is missing"
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, "github credential file is unreadable"
    if mode & 0o077:
        return None, "github credential file is too open"
    if not token or any(char.isspace() for char in token):
        return None, "github credential file is unreadable"
    return token, None


def _missing_prod_inputs(environ: dict[str, str]) -> list[str]:
    required = (
        "ATLAS_QUALIFICATION_CONFIRM_PROD",
        "ATLAS_PUBLIC_BASE_URL",
        "ATLAS_DATA_ROOT",
        "ATLAS_BACKUP_DEST",
        "ATLAS_RESTORE_PROOF_DEST",
        "ATLAS_ROLLBACK_TARGET",
        "ATLAS_QUALIFICATION_RESTART_COMMAND",
        "ATLAS_CURSOR_MCP_EVIDENCE",
        "ATLAS_QUALIFICATION_GITHUB_TOKEN_FILE",
    )
    missing = [name for name in required if not (environ.get(name) or "").strip()]
    if (
        "ATLAS_QUALIFICATION_CONFIRM_PROD" not in missing
        and environ.get("ATLAS_QUALIFICATION_CONFIRM_PROD") != "yes"
    ):
        missing.append("ATLAS_QUALIFICATION_CONFIRM_PROD")
    return missing


def _load_cursor_evidence(path: Path) -> tuple[dict | None, str | None]:
    if path.is_symlink() or not path.is_file():
        return None, "cursor evidence is missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, "cursor evidence is unreadable"
    if contains_unsafe_secret(text):
        return None, "cursor evidence contains a secret"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "cursor evidence is unreadable"
    if not isinstance(data, dict):
        return None, "cursor evidence is not bound to the prod project"
    return data, None


def _cursor_static_reason(path: Path) -> str | None:
    data, reason = _load_cursor_evidence(path)
    if reason or data is None:
        return reason or "cursor evidence is unreadable"
    if data.get("client") != "cursor" or data.get("status") != "PASS":
        return "cursor evidence is not bound to the prod project"
    if data.get("endpoint") != PROD_ENDPOINT:
        return "cursor evidence is not bound to the prod endpoint"
    if data.get("project_id") != PROD_PROJECT_ID or data.get("query") != PROD_QUERY:
        return "cursor evidence is not bound to the prod project"
    if (
        data.get("repository") != PROD_REPOSITORY
        or data.get("source_path") != PROD_SOURCE_PATH
    ):
        return "cursor evidence is not bound to the canonical source"
    if data.get("tools") != ["search_project", "get_provenance"]:
        return "cursor evidence is not bound to the prod project"
    identity = data.get("identity")
    revision = data.get("source_revision")
    if not isinstance(identity, str) or not identity.strip():
        return "cursor evidence is not bound to the prod project"
    if not isinstance(revision, str) or not revision.strip():
        return "cursor evidence is not bound to the synced revision"
    return None


def _cursor_binding_reason(path: Path, *, revision: str, identity: str) -> str | None:
    static = _cursor_static_reason(path)
    if static:
        return static
    data, reason = _load_cursor_evidence(path)
    if reason or data is None:
        return reason or "cursor evidence is unreadable"
    if data.get("identity") != identity or data.get("source_revision") != revision:
        return "cursor evidence is not bound to the synced revision"
    return None


def _prod_fresh_hit(data_root: Path, repo_root: Path) -> dict[str, str] | None:
    env = _scrubbed_env()
    env["PYTHONPATH"] = str(repo_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            _FRESH_BOUND_SEARCH,
            str(data_root),
            PROD_PROJECT_ID,
            PROD_QUERY,
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if payload.get("ready") is not True:
        return None
    hits = payload.get("hits")
    if not isinstance(hits, list):
        return None
    matches = [
        hit
        for hit in hits
        if isinstance(hit, dict)
        and hit.get("project_id") == PROD_PROJECT_ID
        and hit.get("repository") == PROD_REPOSITORY
        and hit.get("source_path") == PROD_SOURCE_PATH
        and isinstance(hit.get("identity"), str)
        and isinstance(hit.get("source_revision"), str)
    ]
    if len(matches) != 1:
        return None
    return {
        "identity": matches[0]["identity"],
        "source_revision": matches[0]["source_revision"],
    }


def _scrubbed_env() -> dict[str, str]:
    blocked = {"GITHUB_TOKEN", "GH_TOKEN", "ATLAS_QUALIFICATION_GITHUB_TOKEN_FILE"}
    kept: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if (
            key in blocked
            or "TOKEN" in upper
            or "SECRET" in upper
            or "PASSWORD" in upper
        ):
            continue
        kept[key] = value
    kept["PYTHONDONTWRITEBYTECODE"] = "1"
    return kept


def _restart_argv(command: str) -> list[str] | None:
    if any(char in command for char in "\n\r;&|$`<>"):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) < 1 or Path(argv[0]).name in _SHELLS:
        return None
    return argv


def _pin_reason(repo_root: Path) -> str | None:
    path = repo_root / ".engineering" / "project.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "engineering system pin is unreadable"
    pin = _engineering_system_pin(text)
    if pin is None:
        return "engineering system pin is unreadable"
    version, baseline = pin
    if version != PIN_VERSION or baseline != PIN_BASELINE:
        return (
            "engineering system pin is not v1.6.5 baseline "
            "14150e424c922ff3a930b45dcf31d3a3d3ba28b2"
        )
    return None


def _engineering_system_pin(text: str) -> tuple[str, str] | None:
    """Read only engineering_system.version and baseline from a project profile.

    The Phase 1 canonical-source adoption parser rejects list items in the
    rest of `.engineering/project.yaml`. This reader stops at the next root
    key and does not interpret domains, platforms, or operations.
    """
    lines = text.splitlines()
    start = _engineering_system_index(lines)
    if start is None:
        return None
    version: str | None = None
    baseline: str | None = None
    child_indent: int | None = None
    end = start + 1
    for end, raw in enumerate(lines[start + 1 :], start=start + 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            return None
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            break
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        stripped = raw.strip()
        if stripped.startswith("-") or ":" not in stripped:
            return None
        key, _, value = stripped.partition(":")
        key = key.strip()
        if key not in {"version", "baseline"}:
            continue
        scalar = _plain_scalar(value.strip())
        if scalar is None:
            return None
        if key == "version":
            if version is not None:
                return None
            version = scalar
        else:
            if baseline is not None:
                return None
            baseline = scalar
    else:
        end = len(lines)
    if version is None or baseline is None:
        return None
    if _engineering_system_index(lines[end:]) is not None:
        return None
    return version, baseline


def _engineering_system_index(lines: list[str]) -> int | None:
    found: int | None = None
    for index, raw in enumerate(lines):
        if not raw.strip() or raw.lstrip().startswith("#") or raw[0] in {" ", "\t"}:
            continue
        if raw.startswith("-"):
            continue
        if "\t" in raw:
            return None
        key, separator, value = raw.partition(":")
        if separator != ":" or key != "engineering_system":
            continue
        if value.strip() or found is not None:
            return None
        found = index
    return found


def _plain_scalar(value: str) -> str | None:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if not value or any(char.isspace() for char in value):
        return None
    if any(char in value for char in "{}[]&*!|>%@`,"):
        return None
    return value


def _parse_base_url(base: str):
    if any(char.isspace() for char in base):
        return "public url is not https"
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname:
        return "public url is not https"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "public url is not https"
    if parsed.path not in ("", "/"):
        return "public url is not https"
    return parsed


def _url(parsed, path: str) -> str:
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{parsed.hostname}{port}{path}"


def _health_ready(status: int, body: bytes) -> bool:
    if status != 200 or len(body) > 256:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return payload == {"status": "ready"}


def _https_client(ca_file: Path | None) -> SmokeClient:
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)

    def exchange(method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise OSError("public url is not https")
        connection = HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            context=context,
            timeout=10,
        )
        try:
            connection.request(
                method,
                parsed.path or "/",
                body=body,
                headers={"Content-Type": "application/json"} if body else {},
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                response.read(256)
                raise OSError("redirect refused")
            return response.status, response.read(4096)
        finally:
            connection.close()

    return exchange


def _evidence(
    harness: str,
    mode: str,
    status: str,
    reason: str,
    steps: list[dict[str, str]],
    *,
    production_claim: bool = False,
) -> dict:
    return {
        "engineering_system": {"baseline": PIN_BASELINE, "version": PIN_VERSION},
        "harness": harness,
        "mode": mode,
        "production_claim": production_claim,
        "reason": reason,
        "status": status,
        "steps": steps,
    }


def _step(name: str, status: str, **fields: str) -> dict[str, str]:
    step = {"name": name, "status": status}
    step.update(fields)
    return step


_FRESH_SEARCH = """
import json
import sys
from pathlib import Path

from atlas.ops import data_root_runtime_ready
from atlas.service import AtlasService

root = Path(sys.argv[1])
hits = AtlasService(root).search(sys.argv[2], sys.argv[3], limit=1)
payload = {"ready": data_root_runtime_ready(root), "hits": len(hits)}
if hits:
    provenance = hits[0].provenance if isinstance(hits[0].provenance, dict) else {}
    payload["source_revision"] = provenance.get("source_revision")
sys.stdout.write(json.dumps(payload))
"""

_FRESH_BOUND_SEARCH = """
import json
import sys
from pathlib import Path

from atlas.ops import data_root_runtime_ready
from atlas.service import AtlasService

root = Path(sys.argv[1])
ready = data_root_runtime_ready(root)
hits = AtlasService(root).search(sys.argv[2], sys.argv[3], limit=8)
slim = []
for hit in hits:
    provenance = hit.provenance if isinstance(hit.provenance, dict) else {}
    slim.append(
        {
            "identity": hit.identity,
            "source_revision": provenance.get("source_revision"),
            "repository": provenance.get("repository"),
            "source_path": provenance.get("source_path"),
            "project_id": provenance.get("project_id"),
        }
    )
sys.stdout.write(json.dumps({"ready": ready, "hits": slim}))
"""
