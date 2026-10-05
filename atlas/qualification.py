"""Prod-atlas qualification harness.

Design gate: ADR-0013. The Engineering System pin is v1.7.0 baseline
ab7fafa8722fb6a6b30f1a65dfa48882db3b0c2d. Local success is not a production
pass, and this module does not change the release contract flags.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shlex
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from http.client import HTTPSConnection
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from atlas.data_protection import backup_data_root, restore_test
from atlas.github_sync import FetchedSource, FetchFn, fetch_github_file
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.lifecycle_intelligence import lifecycle_view
from atlas.ops import data_root_runtime_ready
from atlas.provenance import ValidationError
from atlas.schema_compat import rollback_data_root, upgrade_data_root
from atlas.secrets import contains_unsafe_secret
from atlas.semantic_retrieval import EmbeddingConfig
from atlas.service import AtlasService
from atlas.web_ui import render_intelligence, render_lifecycle, render_project

PIN_VERSION = "1.7.0"
PIN_BASELINE = "ab7fafa8722fb6a6b30f1a65dfa48882db3b0c2d"
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
_MULTI_QUERY = "multi-project-operational"
_MULTI_PROJECTS = (
    {
        "project_id": "qual-alpha",
        "repository": "datarelay-labs/qual-alpha",
        "source_id": "alpha",
        "source_path": "docs/alpha.md",
        "unique_query": "alpha-only-marker",
        "revision": "rev-multi-alpha",
    },
    {
        "project_id": "qual-beta",
        "repository": "datarelay-labs/qual-beta",
        "source_id": "beta",
        "source_path": "docs/beta.md",
        "unique_query": "beta-only-marker",
        "revision": "rev-multi-beta",
    },
)
_CORE_PROJECT_ID = "core-alpha"
_CORE_PEER_ID = "core-beta"
_CORE_QUERY = "core-product-e2e-marker"
_CORE_SEMANTIC_QUERY = "semantic-only-core-e2e"
_CORE_DECISION = "ADR-9001"
_CORE_REVISION = "1" * 40
_CORE_ADR_REVISION = "2" * 40
_CORE_METADATA_REVISION = "3" * 40
_CORE_PEER_REVISION = "4" * 40
_CORE_GAP_SOURCE_ID = "missing-runbook"
_CORE_ADOPTION_YAML = f"""engineering_system:
  version: "{PIN_VERSION}"
  mode: adopted
  baseline: "{PIN_BASELINE}"
  ci_mode: shared
project:
  name: "{_CORE_PROJECT_ID}"
  type: "engineering-platform"
"""
_CORE_ARCHITECTURE = f"""# Core product qualification

{_CORE_QUERY}
semantic-target-marker
See {_CORE_DECISION}.
CONTRADICTION: deterministic-core-e2e-conflict
"""
_CORE_ADR = f"""# {_CORE_DECISION}: Core product qualification decision

Deterministic qualification target.
"""
_CORE_PEER_NOTE = f"""# Peer project

{_CORE_QUERY}
"""

_SHELLS = {"sh", "bash", "dash", "zsh", "sudo"}
_CODE_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_SMOKE_BODY = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'

SmokeClient = Callable[[str, str, bytes | None], tuple[int, bytes]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prod-qualification")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("public-smoke")
    operational = sub.add_parser("operational-e2e")
    operational.add_argument(
        "--mode",
        choices=("local", "multi-project-local", "core-local", "prod"),
        default="local",
    )
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
    if mode == "multi-project-local":
        return _multi_project_operational_e2e(repo_root)
    if mode == "core-local":
        return _core_local_operational_e2e(repo_root)
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


def _public_smoke_after_restart(
    environ: dict[str, str],
    *,
    repo_root: Path,
    smoke_client: SmokeClient | None,
) -> dict:
    """Retry only while the public endpoint is still coming back after restart."""
    last = run_public_smoke(environ, repo_root=repo_root, smoke_client=smoke_client)
    for _ in range(4):
        if last.get("production_claim") is True or last.get("reason") != "endpoint unreachable":
            return last
        time.sleep(1)
        last = run_public_smoke(environ, repo_root=repo_root, smoke_client=smoke_client)
    return last


class _CoreDeterministicEmbedder:
    """Harness-only embedding client exercising the production semantic contract."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            if text.strip() == _CORE_SEMANTIC_QUERY or "semantic-target-marker" in text:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


def _core_fetch(source, token):  # noqa: ARG001
    if token:
        raise ValidationError("core-local qualification does not accept a token")
    key = (source.project_id, source.source_id)
    fixtures = {
        (_CORE_PROJECT_ID, "engineering-system"): (_CORE_ADOPTION_YAML, _CORE_METADATA_REVISION),
        (_CORE_PROJECT_ID, "architecture"): (_CORE_ARCHITECTURE, _CORE_REVISION),
        (_CORE_PROJECT_ID, "decision"): (_CORE_ADR, _CORE_ADR_REVISION),
        (_CORE_PEER_ID, "peer-note"): (_CORE_PEER_NOTE, _CORE_PEER_REVISION),
    }
    fixture = fixtures.get(key)
    if fixture is None:
        raise ValidationError("core-local qualification source is unknown")
    return FetchedSource(content=fixture[0], source_revision=fixture[1])


def _core_mcp_snapshot(service: AtlasService) -> dict[str, object] | None:
    tools = AtlasContextTools(
        retriever_factory=service.project_retriever,
        intelligence_factory=service.project_intelligence,
        intelligence_overview_factory=service.intelligence_overview,
        knowledge_search_factory=lambda query, project_ids, source_class, limit: service.search_across_projects(
            query,
            project_ids=project_ids,
            source_class=source_class,
            limit_per_project=limit,
        ),
    )
    found = tools.call(
        "search_project",
        {"project_id": _CORE_PROJECT_ID, "query": _CORE_QUERY, "limit": 4},
        scopes=default_read_scopes(),
    )
    if not found.ok or not isinstance(found.data, list):
        return None
    hit = next(
        (
            item for item in found.data
            if isinstance(item, dict)
            and isinstance(item.get("provenance"), dict)
            and item["provenance"].get("source_revision") == _CORE_REVISION
        ),
        None,
    )
    if hit is None:
        return None
    proven = tools.call(
        "get_provenance",
        {"project_id": _CORE_PROJECT_ID, "identity": hit.get("identity")},
        scopes=default_read_scopes(),
    )
    intelligence = tools.call(
        "get_project_intelligence",
        {"project_id": _CORE_PROJECT_ID},
        scopes=default_read_scopes(),
    )
    if not proven.ok or not isinstance(proven.data, dict) or not intelligence.ok or not isinstance(intelligence.data, dict):
        return None
    return {
        "identity": hit.get("identity"),
        "revision": proven.data.get("source_revision"),
        "intelligence": intelligence.data,
    }


def _write_core_lifecycle_fixture(data_root: Path) -> None:
    current_head = "a" * 40
    stale_head = "b" * 40
    observed_at = datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    snapshot = {
        "schema_version": 1,
        "kind": "cursor_github_reconciliation",
        "observed_at": observed_at,
        "repositories": ["datarelay-labs/core-alpha"],
        "observations": [
            {
                "repository": "datarelay-labs/core-alpha",
                "issue_number": 238,
                "issue_state": "OPEN",
                "issue_updated_at": "2026-10-01T00:00:00Z",
                "author_trust": "trusted",
                "packet_status": "ACTIVE",
                "branch": "feat/core-product-e2e-qualification",
                "head": current_head,
                "pr_number": None,
                "pr_state": "NONE",
                "pr_head": None,
                "canonical_fact": True,
                "reasons": [],
            }
        ],
        "summary": {
            "observed_count": 1,
            "canonical_count": 1,
            "noncanonical_count": 0,
        },
    }
    evidence = {
        "schema_version": 2,
        "kind": "atlas_lifecycle_evidence",
        "observed_at": "2026-10-01T00:01:00Z",
        "repository": "datarelay-labs/core-alpha",
        "candidate_head": stale_head,
        "channels": {
            "ci": {
                "outcome": "PASS",
                "detail": "deterministic stale-candidate fixture",
                "evidence_ref": "ci:core-e2e-stale",
            }
        },
        "human_equivalent_user_tests": {},
    }
    (data_root / "github-lifecycle.json").write_text(
        json.dumps(snapshot, sort_keys=True),
        encoding="utf-8",
    )
    (data_root / "lifecycle-evidence.json").write_text(
        json.dumps(evidence, sort_keys=True),
        encoding="utf-8",
    )


def _core_cross_project_ok(service: AtlasService) -> bool:
    payload = service.search_across_projects(
        _CORE_QUERY,
        project_ids=[_CORE_PROJECT_ID, _CORE_PEER_ID],
        source_class="engineering",
        limit_per_project=4,
    )
    if payload.get("project_ids") != sorted([_CORE_PROJECT_ID, _CORE_PEER_ID]):
        return False
    found: dict[str, str] = {}
    for group in payload.get("groups", []):
        if not isinstance(group, dict):
            return False
        project_id = str(group.get("project_id") or "")
        for hit in group.get("hits", []):
            if not isinstance(hit, dict) or not isinstance(hit.get("provenance"), dict):
                continue
            revision = hit["provenance"].get("source_revision")
            if project_id == _CORE_PROJECT_ID and revision == _CORE_REVISION:
                found[project_id] = str(revision)
            elif project_id == _CORE_PEER_ID and revision == _CORE_PEER_REVISION:
                found[project_id] = str(revision)
    return found == {_CORE_PROJECT_ID: _CORE_REVISION, _CORE_PEER_ID: _CORE_PEER_REVISION}


def _fresh_core_snapshot(data_root: Path, repo_root: Path) -> bool:
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            _FRESH_CORE_SNAPSHOT,
            str(data_root),
            _CORE_PROJECT_ID,
            _CORE_QUERY,
            _CORE_REVISION,
            _CORE_DECISION,
            _CORE_GAP_SOURCE_ID,
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
        return False
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False
    return payload == {
        "backlink": True,
        "contradiction": "DETECTED",
        "gap": True,
        "ready": True,
        "revision": _CORE_REVISION,
    }


def _core_local_operational_e2e(repo_root: Path) -> dict:
    pin_reason = _pin_reason(repo_root)
    mode = "core-local-deterministic"
    if pin_reason:
        return _evidence("operational-e2e", mode, "FAIL_CLOSED", pin_reason, [])
    steps: list[dict[str, str]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir(mode=0o700)
            service = AtlasService(data)
            service.register_project(
                project_id=_CORE_PROJECT_ID,
                repository="datarelay-labs/core-alpha",
                display_name="Core Alpha",
            )
            for source_id, source_path in (
                ("engineering-system", ".engineering/project.yaml"),
                ("architecture", "docs/architecture.md"),
                ("decision", "docs/decisions/ADR-9001-core-e2e.md"),
            ):
                service.add_source(_CORE_PROJECT_ID, source_id=source_id, source_path=source_path)
            service.sync_project(_CORE_PROJECT_ID, fetch=_core_fetch)
            service.add_source(
                _CORE_PROJECT_ID,
                source_id=_CORE_GAP_SOURCE_ID,
                source_path="docs/runbooks/missing-core-e2e.md",
            )
            service.register_project(
                project_id=_CORE_PEER_ID,
                repository="datarelay-labs/core-beta",
                display_name="Core Beta",
            )
            service.add_source(_CORE_PEER_ID, source_id="peer-note", source_path="docs/peer.md")
            service.sync_project(_CORE_PEER_ID, fetch=_core_fetch)
            engineering = service.engineering_system_observation(_CORE_PROJECT_ID)
            if engineering.get("state") != "OBSERVED" or engineering.get("baseline") != PIN_BASELINE:
                steps.append(_step("register_sync", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Engineering System projection is not observed", steps)
            steps.append(_step("register_sync", "PASS", project_count="2"))

            keyword_hits = service.search(_CORE_PROJECT_ID, _CORE_QUERY, limit=4)
            keyword_ok = any(hit.provenance.get("source_revision") == _CORE_REVISION for hit in keyword_hits)
            semantic_hits = service.search(
                _CORE_PROJECT_ID,
                _CORE_SEMANTIC_QUERY,
                limit=4,
                embedding=EmbeddingConfig(endpoint="http://127.0.0.1:9", model="core-e2e"),
                embedder=_CoreDeterministicEmbedder(),
            )
            semantic_ok = bool(semantic_hits) and semantic_hits[0].provenance.get("source_revision") == _CORE_REVISION and semantic_hits[0].match in {"semantic", "both"}
            if not keyword_ok or not semantic_ok:
                steps.append(_step("keyword_semantic_retrieval", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Core keyword/semantic retrieval is not attributable", steps)
            steps.append(_step("keyword_semantic_retrieval", "PASS"))

            if not _core_cross_project_ok(service):
                steps.append(_step("cross_project_search", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Core cross-project retrieval is not attributable", steps)
            steps.append(_step("cross_project_search", "PASS", project_count="2"))

            _write_core_lifecycle_fixture(data)
            mcp = _core_mcp_snapshot(service)
            project_ui = render_project(service, _CORE_PROJECT_ID, _CORE_QUERY)
            if mcp is None or project_ui.status != "200 OK":
                steps.append(_step("ui_mcp_context", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Human UI or MCP Core context is unavailable", steps)
            ui_body = project_ui.body.decode("utf-8")
            if mcp.get("identity") not in ui_body or mcp.get("revision") != _CORE_REVISION or _CORE_REVISION not in ui_body:
                steps.append(_step("ui_mcp_context", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Human UI and MCP provenance disagree", steps)
            steps.append(_step("ui_mcp_context", "PASS", provenance_agreement="true"))

            lifecycle = lifecycle_view(data, "datarelay-labs/core-alpha")
            lifecycle_ui = render_lifecycle(service, _CORE_PROJECT_ID)
            lifecycle_states = {
                lifecycle.work.state, lifecycle.ci.state, lifecycle.tests.state, lifecycle.release.state,
                lifecycle.surface_reconciliation.state, lifecycle.full_user_e2e.state,
            }
            required_states = {"OBSERVED", "STALE", "UNKNOWN"}
            if (
                lifecycle_ui.status != "200 OK"
                or not required_states.issubset(lifecycle_states)
                or b"Engineering System compliance" not in lifecycle_ui.body
                or b"STALE" not in lifecycle_ui.body
            ):
                steps.append(_step("lifecycle_visibility", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Lifecycle observed/stale/unknown state is not visible", steps)
            steps.append(
                _step(
                    "lifecycle_visibility",
                    "PASS",
                    observed_preserved="true",
                    stale_preserved="true",
                    unknown_preserved="true",
                )
            )

            intelligence = service.project_intelligence(_CORE_PROJECT_ID)
            intelligence_ui = render_intelligence(service, _CORE_PROJECT_ID)
            gaps = {item.get("source_id") for item in intelligence.get("knowledge_gaps", []) if isinstance(item, dict)}
            mcp_intelligence = mcp.get("intelligence") if isinstance(mcp.get("intelligence"), dict) else {}
            derived_ok = (
                intelligence.get("contradictions", {}).get("state") == "DETECTED"
                and _CORE_DECISION in intelligence.get("decision_backlinks", {})
                and _CORE_GAP_SOURCE_ID in gaps
                and mcp_intelligence.get("contradictions", {}).get("state") == "DETECTED"
                and intelligence_ui.status == "200 OK"
                and _CORE_DECISION.encode("utf-8") in intelligence_ui.body
            )
            if not derived_ok:
                steps.append(_step("derived_intelligence", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Core derived intelligence is incomplete", steps)
            steps.append(_step("derived_intelligence", "PASS", contradiction="DETECTED", knowledge_gap="observed", decision_backlink="observed"))

            if not _fresh_core_snapshot(data, repo_root):
                steps.append(_step("restart_recovery", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Core restart recovery lost attributable state", steps)
            steps.append(_step("restart_recovery", "PASS"))

            backup = root / "backup"
            restored = root / "restored"
            backup_data_root(data, backup)
            restore_test(backup, restored)
            if not _fresh_core_snapshot(restored, repo_root):
                steps.append(_step("backup_restore", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Core restore lost attributable state", steps)
            steps.append(_step("backup_restore", "PASS"))

            upgrade = upgrade_data_root(data)
            rollback = rollback_data_root(data, repo_root)
            if upgrade.get("status") != "ok" or rollback.get("status") != "ok" or not _fresh_core_snapshot(data, repo_root):
                steps.append(_step("upgrade_rollback", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Core upgrade/rollback did not preserve state", steps)
            steps.append(_step("upgrade_rollback", "PASS"))

            if lifecycle.surface_reconciliation.state != "UNKNOWN" or lifecycle.full_user_e2e.state != "UNKNOWN":
                steps.append(_step("release_gate_boundary", "FAIL"))
                return _evidence("operational-e2e", mode, "FAIL", "Deterministic Core qualification must not synthesize browser release PASS", steps)
            steps.append(_step("release_gate_boundary", "PASS", external_browser_gates="required"))
    except (OSError, ValidationError, subprocess.SubprocessError, UnicodeError):
        steps.append(_step("harness", "FAIL"))
        return _evidence("operational-e2e", mode, "FAIL", "Core qualification journey failed", steps)
    return _evidence("operational-e2e", mode, "PASS", "Core qualification journey passed without a production or browser-release claim", steps)


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


def _multi_project_fetch(source, token):  # noqa: ARG001
    if token:
        raise ValidationError("local multi-project qualification does not accept a token")
    fixture = next(
        (
            item
            for item in _MULTI_PROJECTS
            if item["project_id"] == source.project_id
        ),
        None,
    )
    if fixture is None:
        raise ValidationError("multi-project qualification source is unknown")
    return FetchedSource(
        content=f"{_MULTI_QUERY} {fixture['unique_query']}\n",
        source_revision=str(fixture["revision"]),
    )


def _multi_project_hit_matches(hit, fixture: dict[str, str]) -> bool:
    provenance = hit.provenance if hasattr(hit, "provenance") else None
    return (
        isinstance(provenance, dict)
        and hit.project_id == fixture["project_id"]
        and provenance.get("repository") == fixture["repository"]
        and provenance.get("source_path") == fixture["source_path"]
        and provenance.get("source_revision") == fixture["revision"]
    )


def _multi_project_isolated(service: AtlasService) -> bool:
    for fixture in _MULTI_PROJECTS:
        own = service.search(
            str(fixture["project_id"]),
            str(fixture["unique_query"]),
            limit=2,
        )
        if len(own) != 1 or not _multi_project_hit_matches(own[0], fixture):
            return False
        for peer in _MULTI_PROJECTS:
            if peer["project_id"] == fixture["project_id"]:
                continue
            leaked = service.search(
                str(fixture["project_id"]),
                str(peer["unique_query"]),
                limit=1,
            )
            if leaked:
                return False
    return True


def _multi_project_revisions(payload: object) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    expected = {str(item["project_id"]): item for item in _MULTI_PROJECTS}
    groups = payload.get("groups")
    if (
        payload.get("project_ids") != sorted(expected)
        or payload.get("total") != len(expected)
        or not isinstance(groups, list)
        or len(groups) != len(expected)
    ):
        return None
    revisions: dict[str, str] = {}
    for group in groups:
        if not isinstance(group, dict):
            return None
        project_id = group.get("project_id")
        fixture = expected.get(str(project_id))
        hits = group.get("hits")
        if (
            fixture is None
            or group.get("result_count") != 1
            or not isinstance(hits, list)
            or len(hits) != 1
            or not isinstance(hits[0], dict)
        ):
            return None
        hit = hits[0]
        provenance = hit.get("provenance")
        if (
            hit.get("project_id") != project_id
            or not isinstance(provenance, dict)
            or provenance.get("repository") != fixture["repository"]
            or provenance.get("source_path") != fixture["source_path"]
            or provenance.get("source_revision") != fixture["revision"]
        ):
            return None
        revisions[str(project_id)] = str(fixture["revision"])
    return revisions if set(revisions) == set(expected) else None


def _multi_project_cross_search(service: AtlasService) -> dict[str, str] | None:
    payload = service.search_across_projects(
        _MULTI_QUERY,
        project_ids=[str(item["project_id"]) for item in _MULTI_PROJECTS],
        source_class="engineering",
        limit_per_project=1,
    )
    return _multi_project_revisions(payload)


def _multi_project_mcp_search(service: AtlasService) -> dict[str, str] | None:
    tools = AtlasContextTools(
        retriever_factory=service.project_retriever,
        knowledge_search_factory=lambda query, project_ids, source_class, limit: service.search_across_projects(
            query,
            project_ids=project_ids,
            source_class=source_class,
            limit_per_project=limit,
        ),
    )
    result = tools.call(
        "search_knowledge",
        {
            "query": _MULTI_QUERY,
            "project_ids": [str(item["project_id"]) for item in _MULTI_PROJECTS],
            "source_class": "engineering",
            "limit_per_project": 1,
        },
        scopes=default_read_scopes(),
    )
    if not result.ok:
        return None
    return _multi_project_revisions(result.data)




def _fresh_multi_project_search(
    data_root: Path,
    repo_root: Path,
) -> dict[str, str] | None:
    project_ids = [str(item["project_id"]) for item in _MULTI_PROJECTS]
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            _FRESH_MULTI_PROJECT_SEARCH,
            str(data_root),
            _MULTI_QUERY,
            json.dumps(project_ids),
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
    if (
        payload.get("ready") is not True
        or payload.get("project_ids") != sorted(project_ids)
        or payload.get("total") != len(project_ids)
        or not isinstance(payload.get("revisions"), dict)
    ):
        return None
    revisions = {
        str(project_id): str(revision)
        for project_id, revision in payload["revisions"].items()
    }
    expected = {
        str(item["project_id"]): str(item["revision"])
        for item in _MULTI_PROJECTS
    }
    return revisions if revisions == expected else None


def _multi_project_operational_e2e(repo_root: Path) -> dict:
    pin_reason = _pin_reason(repo_root)
    mode = "multi-project-local-deterministic"
    if pin_reason:
        return _evidence("operational-e2e", mode, "FAIL_CLOSED", pin_reason, [])
    steps: list[dict[str, str]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir(mode=0o700)
            service = AtlasService(data)
            for fixture in _MULTI_PROJECTS:
                project_id = str(fixture["project_id"])
                service.register_project(
                    project_id=project_id,
                    repository=str(fixture["repository"]),
                )
                service.add_source(
                    project_id,
                    source_id=str(fixture["source_id"]),
                    source_path=str(fixture["source_path"]),
                )
                service.sync_project(project_id, fetch=_multi_project_fetch)
            steps.append(_step("register_sync", "PASS", project_count="2"))

            if not _multi_project_isolated(service):
                steps.append(_step("project_isolation", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    mode,
                    "FAIL",
                    "multi-project scope isolation failed",
                    steps,
                )
            steps.append(_step("project_isolation", "PASS", project_count="2"))

            revisions = _multi_project_cross_search(service)
            if revisions is None:
                steps.append(_step("cross_project_search", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    mode,
                    "FAIL",
                    "explicit cross-project search is not attributable",
                    steps,
                )
            steps.append(
                _step(
                    "cross_project_search",
                    "PASS",
                    project_count=str(len(revisions)),
                )
            )

            mcp_revisions = _multi_project_mcp_search(service)
            if mcp_revisions != revisions:
                steps.append(_step("mcp_cross_project", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    mode,
                    "FAIL",
                    "MCP explicit multi-project scope is not attributable",
                    steps,
                )
            steps.append(
                _step(
                    "mcp_cross_project",
                    "PASS",
                    project_count=str(len(mcp_revisions)),
                )
            )

            if _fresh_multi_project_search(data, repo_root) != revisions:
                steps.append(_step("restart_recovery", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    mode,
                    "FAIL",
                    "multi-project restart recovery failed",
                    steps,
                )
            steps.append(_step("restart_recovery", "PASS", project_count="2"))

            backup = root / "backup"
            restored = root / "restored"
            backup_data_root(data, backup)
            restore_test(backup, restored)
            if _fresh_multi_project_search(restored, repo_root) != revisions:
                steps.append(_step("backup_restore", "FAIL"))
                return _evidence(
                    "operational-e2e",
                    mode,
                    "FAIL",
                    "multi-project restore did not preserve provenance",
                    steps,
                )
            steps.append(_step("backup_restore", "PASS", project_count="2"))
    except (OSError, ValidationError, subprocess.SubprocessError):
        steps.append(_step("harness", "FAIL"))
        return _evidence(
            "operational-e2e",
            mode,
            "FAIL",
            "multi-project local qualification journey failed",
            steps,
        )
    return _evidence(
        "operational-e2e",
        mode,
        "PASS",
        "multi-project local qualification journey passed",
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
    deployed_head = _checkout_code_head(repo_root)
    if deployed_head is None:
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            "deployed code head is unavailable",
            [],
        )
    evidence_path = Path(environ["ATLAS_CURSOR_MCP_EVIDENCE"])
    static_reason = _cursor_static_reason(evidence_path, code_head=deployed_head)
    if static_reason:
        return _evidence("operational-e2e", "prod", "FAIL_CLOSED", static_reason, [])
    restart = _restart_argv(environ["ATLAS_QUALIFICATION_RESTART_COMMAND"])
    identity_command = _restart_argv(environ["ATLAS_QUALIFICATION_RESTART_IDENTITY_COMMAND"])
    if restart is None:
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            "restart command is not a direct argv",
            [],
        )
    if identity_command is None:
        return _evidence(
            "operational-e2e",
            "prod",
            "FAIL_CLOSED",
            "restart identity command is not a direct argv",
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
        revision = _sync_charter_only(service, token=token, fetch=guarded)
    except (OSError, ValidationError, subprocess.SubprocessError):
        steps.append(_step("sync", "FAIL"))
        return _evidence("operational-e2e", "prod", "FAIL", "github sync failed", steps)
    if revision is None:
        steps.append(_step("sync", "FAIL"))
        return _evidence("operational-e2e", "prod", "FAIL", "github sync failed", steps)
    steps.append(_step("sync", "PASS", source_revision=revision))
    owner_reason = _preserve_data_root_owner(data_root)
    if owner_reason:
        steps.append(_step("data_root_owner", "FAIL"))
        return _evidence("operational-e2e", "prod", "FAIL", owner_reason, steps)
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
    binding = _cursor_binding_reason(
        evidence_path,
        revision=revision,
        identity=identity,
        code_head=deployed_head,
    )
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
        owner_reason = _preserve_data_root_owner(data_root)
        if owner_reason:
            steps.append(_step("restart_recovery", "FAIL"))
            return _evidence("operational-e2e", "prod", "FAIL", owner_reason, steps)
        restart_reason = _prove_service_restart(restart, identity_command)
        if restart_reason:
            steps.append(_step("restart_recovery", "FAIL"))
            return _evidence("operational-e2e", "prod", "FAIL", restart_reason, steps)
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
    again = _public_smoke_after_restart(
        environ,
        repo_root=repo_root,
        smoke_client=smoke_client,
    )
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
    rebound = _cursor_binding_reason(
        evidence_path,
        revision=revision,
        identity=identity,
        code_head=deployed_head,
    )
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
    tools = AtlasContextTools(retriever_factory=service.project_retriever, intelligence_factory=service.project_intelligence, intelligence_overview_factory=service.intelligence_overview, source_detail_factory=service.source_detail, operations_readiness_factory=service.operations_readiness, provider_dashboard_factory=service.provider_dashboard, provider_transition_preview_factory=service.provider_transition_preview, decision_plane_factory=service.decision_plane_dashboard, decision_context_candidates_factory=service.decision_plane_optional_context_candidates, decision_check_candidates_factory=service.decision_plane_focused_check_candidates, instruction_governance_factory=service.instruction_governance_dashboard, concurrency_factory=service.concurrency_dashboard)
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


def _sync_charter_only(service: AtlasService, *, token: str, fetch: FetchFn) -> str | None:
    """Fetch only the qualification charter. Other enabled sources stay untouched."""
    matches = [
        source
        for source in service.registry.canonical_sources(PROD_PROJECT_ID)
        if source.source_id == PROD_SOURCE_ID
    ]
    if len(matches) != 1:
        return None
    record = service.projections.sync_one(matches[0], token=token, fetch=fetch)
    return _synced_revision([record])


def _preserve_data_root_owner(data_root: Path) -> str | None:
    """Keep files created by a root-run harness readable by the service user."""
    try:
        root_stat = os.stat(data_root, follow_symlinks=False)
    except OSError:
        return "data root owner was not preserved"
    for dirpath, dirnames, filenames in os.walk(data_root, followlinks=False):
        for name in (*dirnames, *filenames):
            path = os.path.join(dirpath, name)
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError:
                return "data root owner was not preserved"
            if current.st_uid == root_stat.st_uid and current.st_gid == root_stat.st_gid:
                continue
            try:
                os.chown(path, root_stat.st_uid, root_stat.st_gid, follow_symlinks=False)
            except OSError:
                return "data root owner was not preserved"
    return None


def _prove_service_restart(restart: list[str], identity_command: list[str]) -> str | None:
    """Return a failure reason unless restart changes a service identity marker.

    Exit status alone is not evidence. The identity command is collected before
    and after the restart command and must differ. Its output is not recorded.
    """
    before, reason = _service_identity(identity_command)
    if reason or before is None:
        return reason or "restart identity is unavailable"
    try:
        completed = subprocess.run(
            restart,
            check=False,
            capture_output=True,
            timeout=60,
            env=_scrubbed_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return "restart command failed"
    if completed.returncode != 0:
        return "restart command failed"
    after, reason = _service_identity(identity_command)
    if reason or after is None:
        return reason or "restart identity is unavailable"
    if before == after:
        return "restart did not change service identity"
    return None


def _service_identity(argv: list[str]) -> tuple[str | None, str | None]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=30,
            env=_scrubbed_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None, "restart identity is unavailable"
    if completed.returncode != 0:
        return None, "restart identity is unavailable"
    try:
        text = completed.stdout.decode("utf-8").strip()
    except UnicodeError:
        return None, "restart identity is unavailable"
    if not text or any(char.isspace() for char in text) or contains_unsafe_secret(text):
        return None, "restart identity is unavailable"
    return text, None


def _checkout_code_head(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD^{commit}"],
            check=False,
            capture_output=True,
            timeout=30,
            env=_scrubbed_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        text = completed.stdout.decode("utf-8").strip()
    except UnicodeError:
        return None
    if _CODE_HEAD_RE.fullmatch(text):
        return text
    return None


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
        "ATLAS_QUALIFICATION_RESTART_IDENTITY_COMMAND",
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


def _cursor_static_reason(path: Path, *, code_head: str) -> str | None:
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
    if data.get("code_head") != code_head:
        return "cursor evidence is not bound to the deployed code head"
    return None


def _cursor_binding_reason(
    path: Path,
    *,
    revision: str,
    identity: str,
    code_head: str,
) -> str | None:
    static = _cursor_static_reason(path, code_head=code_head)
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
            f"engineering system pin is not v{PIN_VERSION} baseline {PIN_BASELINE}"
        )
    return None


def _engineering_system_pin(text: str) -> tuple[str, str] | None:
    """Read only engineering_system.version and baseline from a project profile.

    The search stops at the first root ``engineering_system:`` mapping. Direct
    children are read until the next indent-0 root key. Later lists, including
    ``domains``, are ignored. A second root ``engineering_system:`` fails closed.
    """
    lines = text.splitlines()
    start = _find_engineering_system_root(lines)
    if start is None:
        return None
    parsed = _engineering_system_children(lines, start)
    if parsed is None:
        return None
    version, baseline, end = parsed
    if _duplicate_engineering_system_root(lines, end):
        return None
    return version, baseline


def _find_engineering_system_root(lines: list[str]) -> int | None:
    """Return the first root engineering_system mapping and stop scanning."""
    for index, raw in enumerate(lines):
        if _ignored_root_line(raw):
            continue
        if "\t" in raw:
            return None
        key, separator, value = raw.partition(":")
        if separator != ":" or key != "engineering_system":
            continue
        if value.strip():
            return None
        return index
    return None


def _engineering_system_children(
    lines: list[str], start: int
) -> tuple[str, str, int] | None:
    version: str | None = None
    baseline: str | None = None
    child_indent: int | None = None
    end = len(lines)
    for index, raw in enumerate(lines[start + 1 :], start=start + 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            return None
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            end = index
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
    if version is None or baseline is None:
        return None
    return version, baseline, end


def _duplicate_engineering_system_root(lines: list[str], start: int) -> bool:
    """True when another root engineering_system key exists after the block."""
    for raw in lines[start:]:
        if _ignored_root_line(raw):
            continue
        key, separator, _value = raw.partition(":")
        if separator == ":" and key == "engineering_system":
            return True
    return False


def _ignored_root_line(raw: str) -> bool:
    """Skip blanks, comments, indented lines, and list items outside the block."""
    if not raw.strip() or raw.lstrip().startswith("#"):
        return True
    return raw[0] in {" ", "\t"} or raw.startswith("-")


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


_FRESH_CORE_SNAPSHOT = """
import json
import sys
from pathlib import Path

from atlas.ops import data_root_runtime_ready
from atlas.service import AtlasService

root = Path(sys.argv[1])
project_id, query, revision, decision, gap_source = sys.argv[2:7]
service = AtlasService(root)
hits = service.search(project_id, query, limit=4)
matched = any(
    isinstance(hit.provenance, dict) and hit.provenance.get("source_revision") == revision
    for hit in hits
)
intelligence = service.project_intelligence(project_id)
gaps = {
    item.get("source_id")
    for item in intelligence.get("knowledge_gaps", [])
    if isinstance(item, dict)
}
payload = {
    "ready": data_root_runtime_ready(root),
    "revision": revision if matched else None,
    "contradiction": intelligence.get("contradictions", {}).get("state"),
    "backlink": decision in intelligence.get("decision_backlinks", {}),
    "gap": gap_source in gaps,
}
sys.stdout.write(json.dumps(payload, sort_keys=True))
"""


_FRESH_MULTI_PROJECT_SEARCH = """
import json
import sys
from pathlib import Path

from atlas.ops import data_root_runtime_ready
from atlas.service import AtlasService

root = Path(sys.argv[1])
query = sys.argv[2]
project_ids = json.loads(sys.argv[3])
payload = AtlasService(root).search_across_projects(
    query,
    project_ids=project_ids,
    source_class="engineering",
    limit_per_project=1,
)
revisions = {}
for group in payload.get("groups", []):
    hits = group.get("hits") if isinstance(group, dict) else None
    if not isinstance(hits, list) or len(hits) != 1:
        continue
    provenance = hits[0].get("provenance") if isinstance(hits[0], dict) else None
    if isinstance(provenance, dict):
        revisions[str(group.get("project_id"))] = provenance.get("source_revision")
result = {
    "ready": data_root_runtime_ready(root),
    "project_ids": payload.get("project_ids"),
    "total": payload.get("total"),
    "revisions": revisions,
}
sys.stdout.write(json.dumps(result, sort_keys=True))
"""


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
