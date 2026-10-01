"""Exact-candidate actual-browser release gate for Atlas Core."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from atlas.browser_verification import (
    _canonical_digest,
    _default_command_runner,
    _evidence_ref,
    _identity,
    _provider_environment,
    _safe_text,
    _target_url,
)
from atlas.github_sync import FetchedSource
from atlas.provenance import ValidationError
from atlas.qualification import (
    _CORE_GAP_SOURCE_ID,
    _CORE_PEER_ID,
    _CORE_PROJECT_ID,
    _CORE_QUERY,
    _core_fetch,
)
from atlas.service import AtlasService

SCHEMA_VERSION = 1
REQUEST_KIND = "core_release_browser_request"
RESULT_KIND = "core_release_browser_result"
FIXTURE_KIND = "core_release_browser_fixture"
AUTHORITY = "HUMAN_EQUIVALENT_RELEASE_EVIDENCE"
RELEASE_AUTHORITY = "NO_RELEASE_AUTHORITY"
_RESULT_MARKER = "ATLAS_CORE_RELEASE_BROWSER_RESULT="
_HEAD = re.compile(r"^[0-9a-f]{40}$")

SURFACE_IDS = (
    "overview", "intelligence_overview", "operations", "concurrency",
    "personal", "instruction_governance", "decision_plane", "providers",
    "search", "project", "project_lifecycle", "project_intelligence",
    "source_detail", "decision_detail", "empty_overview", "empty_search",
    "empty_personal", "unknown_route", "unknown_project", "invalid_search",
    "corrupt_state", "post_rejection", "security_headers",
    "escaped_source_markup", "read_only_no_write_controls",
    "personal_authority_separation", "lifecycle_truth_states",
)
MISSION_IDS = (
    "engineering_navigation_and_search", "cross_project_retrieval",
    "personal_reference_separation", "lifecycle_observed_unknown_unavailable",
    "negative_invalid_recovery", "escaped_markup_safety",
    "reload_and_new_context_persistence", "read_only_post_recovery",
)
def _reject(message: str) -> None:
    raise ValidationError(message)


def fixture_contract() -> dict[str, str]:
    return {
        "primary_project_id": _CORE_PROJECT_ID,
        "peer_project_id": _CORE_PEER_ID,
        "personal_project_id": _CORE_PROJECT_ID,
        "engineering_query": _CORE_QUERY,
        "isolated_query": "core-alpha-isolation-marker",
        "personal_query": "personal-browser-marker",
        "escape_query": "browser-escape-marker",
        "decision_id": "ADR-9001",
        "source_id": "browser-escape",
        "source_revision": "5" * 40,
    }


def _exact_ids(value: object, expected: tuple[str, ...], label: str) -> list[str]:
    if not isinstance(value, list) or value != list(expected):
        _reject(f"core release browser {label} is incomplete")
    return list(value)


def _base_target(value: object, label: str) -> str:
    target = _target_url(value)
    parsed = urlsplit(target)
    if parsed.path not in {"", "/"} or parsed.query:
        _reject(f"core release browser {label} must be a loopback base URL")
    return target.rstrip("/") + "/"


def _targets(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"primary", "empty", "corrupt"}:
        _reject("core release browser targets are invalid")
    result = {
        key: _base_target(value.get(key), key)
        for key in ("primary", "empty", "corrupt")
    }
    if len(set(result.values())) != 3:
        _reject("core release browser targets must be distinct")
    return result


def validate_core_release_browser_request(payload: object) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "run_id", "source_revision",
        "work_packet_issue", "targets", "fixture",
        "required_surfaces", "required_missions",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("core release browser request schema is invalid")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != REQUEST_KIND:
        _reject("core release browser request version/kind is invalid")
    head = payload.get("source_revision")
    issue = payload.get("work_packet_issue")
    if not isinstance(head, str) or _HEAD.fullmatch(head) is None:
        _reject("core release browser source_revision is invalid")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        _reject("core release browser work_packet_issue is invalid")

    if payload.get("fixture") != fixture_contract():
        _reject("core release browser fixture contract differs")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "run_id": _identity(payload.get("run_id"), label="run_id"),
        "source_revision": head,
        "work_packet_issue": issue,
        "targets": _targets(payload.get("targets")),
        "fixture": fixture_contract(),
        "required_surfaces": _exact_ids(
            payload.get("required_surfaces"), SURFACE_IDS, "surface inventory"
        ),
        "required_missions": _exact_ids(
            payload.get("required_missions"), MISSION_IDS, "mission inventory"
        ),
    }


def _git_text(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        cwd="/",
        env={
            "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
            "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
        },
        text=True,

        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if completed.returncode:
        _reject("core release browser candidate git state is unavailable")
    return completed.stdout.rstrip("\n")


def verify_exact_clean_candidate(repo_root: Path, expected_head: str) -> None:
    root = Path(repo_root).resolve()
    if _git_text(root, "rev-parse", "--verify", "HEAD^{commit}") != expected_head:
        _reject("core release browser candidate HEAD differs from request")
    index_rows = _git_text(root, "ls-files", "-v").splitlines()
    if any(row and (row[0] == "S" or row[0].islower()) for row in index_rows):
        _reject("core release browser candidate has hidden index state")
    if _git_text(root, "status", "--porcelain=v1", "--untracked-files=all"):
        _reject("core release browser candidate worktree is dirty")


def _validated_fixture_roots(value: object) -> dict[str, Path]:
    if not isinstance(value, dict) or set(value) != {"primary", "empty", "corrupt"}:
        _reject("core release browser fixture roots are invalid")
    roots: dict[str, Path] = {}
    for key in ("primary", "empty", "corrupt"):
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.startswith("/"):
            _reject("core release browser fixture root is invalid")
        path = Path(raw)
        if path.is_symlink() or not path.is_dir():
            _reject("core release browser fixture root is unavailable")
        resolved = path.resolve(strict=True)
        if resolved != path:
            _reject("core release browser fixture root is not canonical")
        roots[key] = path
    if len(set(roots.values())) != 3:
        _reject("core release browser fixture roots must be distinct")
    return roots


def _target_server_specs(targets: dict[str, str]) -> dict[str, tuple[str, int]]:
    specs: dict[str, tuple[str, int]] = {}
    for key in ("primary", "empty", "corrupt"):
        parsed = urlsplit(targets[key])
        host = parsed.hostname or ""
        port = parsed.port
        if parsed.scheme != "http" or host != "127.0.0.1":
            _reject("core release browser server target must be 127.0.0.1 HTTP")
        if port is None or not 1024 <= port <= 65535:
            _reject("core release browser server target requires an explicit unprivileged port")
        specs[key] = (host, port)
    if len(set(specs.values())) != 3:
        _reject("core release browser server targets must be distinct")
    return specs


def _process_owns_listener(process: subprocess.Popen[str], port: int) -> bool:
    fd_root = Path(f"/proc/{process.pid}/fd")
    net_tcp = Path("/proc/net/tcp")
    if not fd_root.is_dir() or not net_tcp.is_file():
        _reject("core release browser listener ownership boundary is unavailable")
    socket_inodes: set[str] = set()
    try:
        for fd in fd_root.iterdir():
            try:
                target = fd.readlink().as_posix()
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                socket_inodes.add(target[8:-1])
        for line in net_tcp.read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            local_port = int(fields[1].split(":", 1)[1], 16)
            if local_port == port and fields[9] in socket_inodes:
                return True
    except (OSError, ValueError, UnicodeError):
        _reject("core release browser listener ownership could not be verified")
    return False


def _wait_for_server(process: subprocess.Popen[str], host: str, port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _reject("core release browser candidate server exited before readiness")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                if _process_owns_listener(process, port):
                    return
        except OSError:
            pass
        time.sleep(0.05)
    _reject("core release browser candidate server readiness timed out")


def _launch_candidate_servers(
    repo_root: Path,
    fixture_roots: dict[str, Path],
    targets: dict[str, str],
) -> list[subprocess.Popen[str]]:
    specs = _target_server_specs(targets)
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONPATH": str(repo_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    processes: list[subprocess.Popen[str]] = []
    try:
        for key in ("primary", "empty", "corrupt"):
            host, port = specs[key]
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "atlas",
                    "--data-root",
                    str(fixture_roots[key]),
                    "web",
                    "serve",
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
                cwd=str(repo_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            processes.append(process)
            _wait_for_server(process, host, port)
        return processes
    except Exception:
        _stop_candidate_servers(processes)
        raise


def _stop_candidate_servers(processes: list[subprocess.Popen[str]]) -> bool:
    ok = True
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ok = False
            process.kill()
            process.wait(timeout=5)
        if process.returncode not in {0, -15}:
            ok = False
    return ok


def _fixture_fetch(source, token):
    if source.source_id == fixture_contract()["source_id"]:
        if token:
            raise ValidationError("core release browser fixture does not accept a token")
        return FetchedSource(
            content="<script>browser-escape-marker</script> " + _CORE_QUERY,
            source_revision=fixture_contract()["source_revision"],
        )
    fetched = _core_fetch(source, token)
    if source.project_id == _CORE_PROJECT_ID and source.source_id == "architecture":
        return FetchedSource(
            content=fetched.content + "\n" + fixture_contract()["isolated_query"] + "\n",
            source_revision=fetched.source_revision,
        )
    return fetched


def _seed_primary(root: Path, candidate_head: str) -> None:
    service = AtlasService(root)

    service.register_project(
        project_id=_CORE_PROJECT_ID,
        repository="datarelay-labs/core-alpha",
        display_name="Core Alpha",
    )
    for source_id, source_path in (
        ("engineering-system", ".engineering/project.yaml"),
        ("architecture", "docs/architecture.md"),
        ("decision", "docs/decisions/ADR-9001-core-e2e.md"),
        ("browser-escape", "docs/browser-escape.md"),
    ):
        service.add_source(
            _CORE_PROJECT_ID,
            source_id=source_id,
            source_path=source_path,
        )
    service.sync_project(_CORE_PROJECT_ID, fetch=_fixture_fetch)
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
    service.add_source(
        _CORE_PEER_ID,
        source_id="peer-note",
        source_path="docs/peer.md",
    )

    service.sync_project(_CORE_PEER_ID, fetch=_fixture_fetch)

    service.import_root.mkdir(parents=True, exist_ok=True)
    note = service.import_root / "browser-personal.md"
    note.write_text(fixture_contract()["personal_query"], encoding="utf-8")
    service.import_personal_markdown(
        _CORE_PROJECT_ID,
        source_id="personal-browser",
        source_path="browser-personal.md",
        title="Browser personal reference",
    )
    personal_source = next(
        source
        for source in service.registry.canonical_sources(_CORE_PROJECT_ID)
        if source.source_id == "personal-browser"
    )
    service.projections.sync_one(personal_source)

    snapshot = {
        "schema_version": 1,
        "kind": "cursor_github_reconciliation",
        "observed_at": "2026-10-01T00:00:00Z",
        "repositories": ["datarelay-labs/core-alpha"],
        "observations": [{
            "repository": "datarelay-labs/core-alpha",
            "issue_number": 245,
            "issue_state": "OPEN",
            "issue_updated_at": "2026-10-01T00:00:00Z",
            "author_trust": "trusted",
            "packet_status": "ACTIVE",
            "branch": "feat/core-release-browser-runner-direct2",
            "head": candidate_head,
            "pr_number": None,
            "pr_state": "NONE",
            "pr_head": None,

            "canonical_fact": True,
            "reasons": [],
        }],
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
        "candidate_head": candidate_head,
        "channels": {
            "ci": {
                "outcome": "PASS",
                "detail": "browser fixture observed CI",
                "evidence_ref": "ci:browser-fixture",
            },
            "tests": {
                "outcome": "INVALID",
                "detail": "intentionally malformed browser fixture",
                "evidence_ref": "tests:browser-fixture",
            },
        },
        "human_equivalent_user_tests": {},
    }

    (root / "github-lifecycle.json").write_text(
        json.dumps(snapshot, sort_keys=True),
        encoding="utf-8",
    )
    (root / "lifecycle-evidence.json").write_text(
        json.dumps(evidence, sort_keys=True),
        encoding="utf-8",
    )


def prepare_core_release_browser_fixture(
    base: Path,
    *,
    candidate_head: str = "a" * 40,
) -> dict[str, object]:
    if not isinstance(candidate_head, str) or _HEAD.fullmatch(candidate_head) is None:
        _reject("core release browser fixture candidate HEAD is invalid")
    base = Path(base)
    if base.exists():
        _reject("core release browser fixture destination already exists")
    primary = base / "primary"
    empty = base / "empty"
    corrupt = base / "corrupt"
    primary.mkdir(parents=True, mode=0o700)
    empty.mkdir(parents=True, mode=0o700)
    _seed_primary(primary, candidate_head)
    shutil.copytree(primary, corrupt)
    projection_index = corrupt / "projections" / "projections.json"
    projection_index.write_text("{broken", encoding="utf-8")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": FIXTURE_KIND,
        "roots": {
            "primary": str(primary),
            "empty": str(empty),
            "corrupt": str(corrupt),
        },
        "fixture": fixture_contract(),
    }


def request_from_manifest(
    manifest: object,
    *,
    run_id: str,
    source_revision: str,
    work_packet_issue: int,
    primary_url: str,
    empty_url: str,
    corrupt_url: str,
) -> dict[str, object]:
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("kind") != FIXTURE_KIND
        or manifest.get("fixture") != fixture_contract()
        or not isinstance(manifest.get("roots"), dict)
    ):
        _reject("core release browser fixture manifest is invalid")
    _validated_fixture_roots(manifest["roots"])

    return validate_core_release_browser_request({
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "run_id": run_id,
        "source_revision": source_revision,
        "work_packet_issue": work_packet_issue,
        "targets": {
            "primary": primary_url,
            "empty": empty_url,
            "corrupt": corrupt_url,
        },
        "fixture": fixture_contract(),
        "required_surfaces": list(SURFACE_IDS),
        "required_missions": list(MISSION_IDS),
    })


def _surface_results(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(SURFACE_IDS):
        _reject("core release browser surface results are incomplete")
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "surface_id", "outcome", "http_status", "detail"
        }:
            _reject("core release browser surface result schema is invalid")
        surface_id = item.get("surface_id")
        status = item.get("http_status")

        if (
            surface_id not in SURFACE_IDS
            or surface_id in seen
            or item.get("outcome") not in {"PASS", "FAIL"}
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            _reject("core release browser surface result is invalid")
        seen.add(str(surface_id))
        result.append({
            "surface_id": str(surface_id),
            "outcome": str(item["outcome"]),
            "http_status": status,
            "detail": _safe_text(
                item.get("detail"), label="surface detail", maximum=512
            ),
        })
    if seen != set(SURFACE_IDS):
        _reject("core release browser surface results are incomplete")
    return result


def _mission_results(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != len(MISSION_IDS):
        _reject("core release browser mission results are incomplete")
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {

            "mission_id", "outcome", "detail"
        }:
            _reject("core release browser mission result schema is invalid")
        mission_id = item.get("mission_id")
        if (
            mission_id not in MISSION_IDS
            or mission_id in seen
            or item.get("outcome") not in {"PASS", "FAIL"}
        ):
            _reject("core release browser mission result is invalid")
        seen.add(str(mission_id))
        result.append({
            "mission_id": str(mission_id),
            "outcome": str(item["outcome"]),
            "detail": _safe_text(
                item.get("detail"), label="mission detail", maximum=512
            ),
        })
    if seen != set(MISSION_IDS):
        _reject("core release browser mission results are incomplete")
    return result


def _validate_raw_result(payload: object) -> dict[str, object]:
    expected = {
        "result", "browser_engine", "browser_version", "actual_browser_process",
        "duration_ms", "surfaces", "missions", "cleanup", "trace_ref",
        "screenshot_ref", "error_code", "detail",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("core release browser provider result schema is invalid")

    outcome = payload.get("result")
    actual = payload.get("actual_browser_process")
    cleanup = payload.get("cleanup")
    if outcome not in {"PASS", "FAIL", "HUMAN_REQUIRED"} or not isinstance(actual, bool):
        _reject("core release browser provider outcome is invalid")
    if not isinstance(cleanup, dict) or set(cleanup) != {
        "context_closed", "browser_closed"
    }:
        _reject("core release browser cleanup schema is invalid")
    if any(not isinstance(cleanup[key], bool) for key in cleanup):
        _reject("core release browser cleanup state is invalid")
    surfaces = _surface_results(payload.get("surfaces"))
    missions = _mission_results(payload.get("missions"))
    trace_ref = _evidence_ref(payload.get("trace_ref"), label="trace_ref")
    if outcome == "PASS" and (
        not actual
        or any(item["outcome"] != "PASS" for item in surfaces)
        or any(item["outcome"] != "PASS" for item in missions)
        or not cleanup["context_closed"]
        or not cleanup["browser_closed"]
        or trace_ref is None
    ):
        _reject("core release browser PASS lacks complete actual-browser evidence")
    duration = payload.get("duration_ms")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 <= duration <= 3_600_000
    ):
        _reject("core release browser duration is invalid")

    return {
        "result": outcome,
        "browser_engine": _safe_text(
            payload.get("browser_engine"), label="browser engine", maximum=64
        ),
        "browser_version": _safe_text(
            payload.get("browser_version"), label="browser version", maximum=128
        ),
        "actual_browser_process": actual,
        "duration_ms": duration,
        "surfaces": surfaces,
        "missions": missions,
        "cleanup": dict(cleanup),
        "trace_ref": trace_ref,
        "screenshot_ref": _evidence_ref(
            payload.get("screenshot_ref"), label="screenshot_ref"
        ),
        "error_code": (
            None
            if payload.get("error_code") is None
            else _identity(payload.get("error_code"), label="error_code")
        ),
        "detail": _safe_text(
            payload.get("detail"), label="detail", maximum=600
        ),
    }


def _provider_output(stdout: str) -> object:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(_RESULT_MARKER):

            try:
                return json.loads(line[len(_RESULT_MARKER):])
            except (ValueError, RecursionError) as exc:
                raise ValidationError(
                    "core release browser provider output is invalid JSON"
                ) from exc
    _reject("core release browser provider result marker is missing")


def _result_request_part(payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "run_id": payload.get("run_id"),
        "source_revision": payload.get("source_revision"),
        "work_packet_issue": payload.get("work_packet_issue"),
        "targets": payload.get("targets"),
        "fixture": payload.get("fixture"),
        "required_surfaces": payload.get("required_surfaces"),
        "required_missions": payload.get("required_missions"),
    }


def _validate_server_binding(value: object, expected_head: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "mode", "candidate_head", "server_count", "servers_stopped"
    }:
        _reject("core release browser server binding is invalid")
    if value.get("mode") != "VERIFIED_CHECKOUT_SUBPROCESS":
        _reject("core release browser server binding mode is invalid")
    if value.get("candidate_head") != expected_head:
        _reject("core release browser server binding HEAD mismatch")
    if value.get("server_count") != 3:
        _reject("core release browser server binding count is invalid")
    if not isinstance(value.get("servers_stopped"), bool):
        _reject("core release browser server cleanup state is invalid")
    return dict(value)


def validate_core_release_browser_result(payload: object) -> dict[str, object]:
    expected = {
        "schema_version", "kind", "authority", "release_authority",
        "run_id", "source_revision", "work_packet_issue", "targets", "fixture",
        "required_surfaces", "required_missions", "server_binding", "result",
        "browser", "duration_ms", "surfaces", "missions", "cleanup", "trace_ref",
        "screenshot_ref", "error_code", "detail", "result_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        _reject("core release browser result schema is invalid")
    validate_core_release_browser_request(_result_request_part(payload))
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != RESULT_KIND
        or payload.get("authority") != AUTHORITY
        or payload.get("release_authority") != RELEASE_AUTHORITY
    ):
        _reject("core release browser result authority is invalid")
    binding = _validate_server_binding(
        payload.get("server_binding"),
        str(payload.get("source_revision") or ""),
    )
    browser = payload.get("browser")
    if not isinstance(browser, dict) or set(browser) != {
        "engine", "version", "mode", "actual_process"
    }:
        _reject("core release browser browser schema is invalid")
    if browser.get("mode") not in {"HEADLESS", "NONE"}:
        _reject("core release browser browser mode is invalid")
    raw = _validate_raw_result({
        "result": payload.get("result"),
        "browser_engine": browser.get("engine"),
        "browser_version": browser.get("version"),
        "actual_browser_process": browser.get("actual_process"),
        "duration_ms": payload.get("duration_ms"),
        "surfaces": payload.get("surfaces"),
        "missions": payload.get("missions"),
        "cleanup": payload.get("cleanup"),
        "trace_ref": payload.get("trace_ref"),
        "screenshot_ref": payload.get("screenshot_ref"),
        "error_code": payload.get("error_code"),
        "detail": payload.get("detail"),
    })
    if raw["result"] == "PASS" and not binding["servers_stopped"]:
        _reject("core release browser PASS lacks candidate-server cleanup evidence")
    digest = payload.get("result_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        _reject("core release browser result digest is invalid")
    body = {
        key: value
        for key, value in payload.items()
        if key != "result_digest"
    }
    if _canonical_digest(body) != digest:
        _reject("core release browser result digest mismatch")
    return dict(payload)


def _compose_core_release_result(
    normalized: dict[str, object],
    raw_result: object,
    *,
    servers_stopped: bool,
) -> dict[str, object]:
    raw = _validate_raw_result(raw_result)
    if raw["result"] == "PASS" and not servers_stopped:
        raw = {
            **raw,
            "result": "FAIL",
            "error_code": "CANDIDATE_SERVER_CLEANUP_FAILED",
            "detail": "Candidate-bound browser servers did not clean up completely.",
        }
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "authority": AUTHORITY,
        "release_authority": RELEASE_AUTHORITY,
        "run_id": normalized["run_id"],
        "source_revision": normalized["source_revision"],
        "work_packet_issue": normalized["work_packet_issue"],
        "targets": normalized["targets"],
        "fixture": normalized["fixture"],
        "required_surfaces": normalized["required_surfaces"],
        "required_missions": normalized["required_missions"],
        "server_binding": {
            "mode": "VERIFIED_CHECKOUT_SUBPROCESS",
            "candidate_head": normalized["source_revision"],
            "server_count": 3,
            "servers_stopped": servers_stopped,
        },
        "result": raw["result"],
        "browser": {
            "engine": raw["browser_engine"],
            "version": raw["browser_version"],
            "mode": "HEADLESS" if raw["actual_browser_process"] else "NONE",
            "actual_process": raw["actual_browser_process"],
        },
        "duration_ms": raw["duration_ms"],
        "surfaces": raw["surfaces"],
        "missions": raw["missions"],
        "cleanup": raw["cleanup"],
        "trace_ref": raw["trace_ref"],
        "screenshot_ref": raw["screenshot_ref"],
        "error_code": raw["error_code"],
        "detail": raw["detail"],
    }
    result = {**body, "result_digest": _canonical_digest(body)}
    return validate_core_release_browser_result(result)


def run_core_release_browser(
    request: object,
    *,
    repo_root: Path,
    fixture_roots: object,
    runtime_env: dict[str, str] | None = None,
) -> dict[str, object]:
    normalized = validate_core_release_browser_request(request)
    root = Path(repo_root).resolve()
    verify_exact_clean_candidate(root, str(normalized["source_revision"]))
    roots = _validated_fixture_roots(fixture_roots)
    runner_path = root / "tools" / "browser-verification" / "core-release-runner.mjs"
    if not runner_path.is_file():
        _reject("core release browser runner is missing")

    env = _provider_environment(
        "playwright",
        runtime_env=runtime_env,
        stagehand_api_key=None,
        stagehand_model=None,
    )
    processes: list[subprocess.Popen[str]] = []
    raw_result: object | None = None
    servers_stopped = False
    try:
        processes = _launch_candidate_servers(
            root,
            roots,
            dict(normalized["targets"]),
        )
        verify_exact_clean_candidate(root, str(normalized["source_revision"]))
        try:
            completed = _default_command_runner(
                ["node", str(runner_path)],
                str(root),
                json.dumps(normalized, sort_keys=True),
                env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationError(
                "core release browser runner process is unavailable"
            ) from exc
        if completed.returncode != 0:
            _reject("core release browser runner process failed")
        verify_exact_clean_candidate(root, str(normalized["source_revision"]))
        raw_result = _provider_output(completed.stdout)
    finally:
        servers_stopped = _stop_candidate_servers(processes)

    if raw_result is None:
        _reject("core release browser runner returned no result")
    return _compose_core_release_result(
        normalized,
        raw_result,
        servers_stopped=servers_stopped,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="core-release-browser")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare-fixture")
    prepare.add_argument("--base", required=True)
    prepare.add_argument("--candidate-head", required=True)

    run = sub.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--source-revision", required=True)
    run.add_argument("--work-packet-issue", required=True, type=int)
    run.add_argument("--primary-url", required=True)
    run.add_argument("--empty-url", required=True)
    run.add_argument("--corrupt-url", required=True)
    run.add_argument("--runtime-lib-dir")

    args = parser.parse_args(argv)
    if args.command == "prepare-fixture":
        manifest = prepare_core_release_browser_fixture(
            Path(args.base),
            candidate_head=args.candidate_head,
        )
        print(json.dumps(manifest, sort_keys=True))
        return 0

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    request = request_from_manifest(
        manifest,
        run_id=args.run_id,
        source_revision=args.source_revision,
        work_packet_issue=args.work_packet_issue,
        primary_url=args.primary_url,
        empty_url=args.empty_url,
        corrupt_url=args.corrupt_url,
    )
    runtime_env = (
        {"LD_LIBRARY_PATH": args.runtime_lib_dir}
        if args.runtime_lib_dir
        else None
    )
    result = run_core_release_browser(
        request,
        repo_root=Path(__file__).resolve().parents[1],
        fixture_roots=manifest["roots"],
        runtime_env=runtime_env,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
