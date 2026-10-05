#!/usr/bin/env python3
"""Refresh derived production Atlas GitHub lifecycle/context from the live registry.

Runs on the authenticated operator host. GitHub credentials never move to prod-atlas.
The production registry decides the complete enabled GitHub-backed repository/project
set on every run so adding a registered GitHub project cannot silently stale this job.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_REPO_ROOT = Path("/home/aella/datarelay-atlas")
DEFAULT_ATLAS_PYTHON = Path(
    "/home/aella/.local/share/datarelay-atlas/prod-refresh-venv/bin/python"
)
DEFAULT_PROD_HOST = "prod-atlas"
LOCK_PATH = Path("/home/aella/.cache/datarelay-atlas/prod-refresh.lock")
REMOTE_DATA_ROOT = "/var/lib/datarelay-atlas"
REMOTE_ROOT = "/opt/datarelay-atlas"
REMOTE_PYTHON = "/opt/datarelay-atlas/.venv/bin/python"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_TARGETS = 32

_REMOTE_TARGETS = r"""
from atlas.provenance import GITHUB_PROVIDER
from atlas.registry import ProjectRegistry
from pathlib import Path
import json

registry = ProjectRegistry(Path("/var/lib/datarelay-atlas"))
repositories = set()
project_ids = []
for project in registry.list_projects():
    if not project.enabled:
        continue
    github_sources = [
        source
        for source in project.sources.values()
        if source.enabled and source.provider == GITHUB_PROVIDER
    ]
    if not github_sources:
        continue
    repositories.add(project.repository)
    project_ids.append(project.project_id)
print(json.dumps({
    "repositories": sorted(repositories),
    "project_ids": sorted(project_ids),
}, sort_keys=True))
"""


class RefreshError(RuntimeError):
    pass


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    capture_output: bool = False,
    stdout: Any = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=capture_output,
        stdout=stdout,
        check=check,
    )


def _ssh(host: str, script: str, *, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(
        ["ssh", "-o", "BatchMode=yes", host, "/bin/bash", "-s"],
        input_text=script,
        capture_output=capture_output,
    )


def _target_query_script() -> str:
    return (
        "set -euo pipefail\n"
        f"runuser -u atlas -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(REMOTE_ROOT)} "
        f"{shlex.quote(REMOTE_PYTHON)} - <<'PY'\n{_REMOTE_TARGETS}PY\n"
    )


def normalize_targets(payload: Any) -> dict[str, list[str]]:
    if not isinstance(payload, dict) or set(payload) != {"repositories", "project_ids"}:
        raise RefreshError("production refresh target payload is invalid")
    repositories = payload["repositories"]
    project_ids = payload["project_ids"]
    if not isinstance(repositories, list) or not isinstance(project_ids, list):
        raise RefreshError("production refresh target lists are invalid")
    if not repositories or not project_ids:
        raise RefreshError("production refresh target set is empty")
    if len(repositories) > MAX_TARGETS or len(project_ids) > MAX_TARGETS:
        raise RefreshError("production refresh target set is too large")
    if len(set(repositories)) != len(repositories) or len(set(project_ids)) != len(project_ids):
        raise RefreshError("production refresh target set contains duplicates")
    if any(not isinstance(item, str) or REPOSITORY_RE.fullmatch(item) is None for item in repositories):
        raise RefreshError("production refresh repository identity is invalid")
    if any(not isinstance(item, str) or PROJECT_ID_RE.fullmatch(item) is None for item in project_ids):
        raise RefreshError("production refresh project identity is invalid")
    return {
        "repositories": sorted(repositories),
        "project_ids": sorted(project_ids),
    }


def fetch_targets(host: str) -> dict[str, list[str]]:
    completed = _ssh(host, _target_query_script())
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RefreshError("production refresh target discovery returned invalid JSON") from exc
    return normalize_targets(payload)


def snapshot_command(
    *,
    atlas_python: Path,
    repositories: list[str],
) -> list[str]:
    command = [str(atlas_python), "-m", "atlas", "usage", "github-snapshot"]
    for repository in repositories:
        command.extend(["--repository", repository])
    return command


def _remote_apply_script(remote_snapshot: str, expected: dict[str, list[str]]) -> str:
    expected_json = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    sync_python = f"""
from atlas.provenance import GITHUB_PROVIDER
from atlas.registry import ProjectRegistry
from pathlib import Path
import json
import os
import subprocess
import sys

expected = json.loads({expected_json!r})
registry = ProjectRegistry(Path({REMOTE_DATA_ROOT!r}))
repositories = set()
project_ids = []
for project in registry.list_projects():
    if not project.enabled:
        continue
    github_sources = [
        source
        for source in project.sources.values()
        if source.enabled and source.provider == GITHUB_PROVIDER
    ]
    if not github_sources:
        continue
    repositories.add(project.repository)
    project_ids.append(project.project_id)
current = {{
    "repositories": sorted(repositories),
    "project_ids": sorted(project_ids),
}}
if current != expected:
    raise SystemExit("production refresh targets changed during execution")
env = dict(os.environ)
env.pop("GITHUB_TOKEN", None)
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["PYTHONPATH"] = {REMOTE_ROOT!r}
for project_id in current["project_ids"]:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "atlas",
            "--data-root",
            {REMOTE_DATA_ROOT!r},
            "sync",
            project_id,
        ],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
    )
    print(f"SYNCED_PROJECT={{project_id}}")
"""
    return f"""set -euo pipefail
chown atlas:atlas {shlex.quote(remote_snapshot)}
chmod 600 {shlex.quote(remote_snapshot)}
runuser -u atlas -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(REMOTE_ROOT)} \
  {shlex.quote(REMOTE_PYTHON)} -m atlas --data-root {shlex.quote(REMOTE_DATA_ROOT)} \
  lifecycle publish-github-snapshot --snapshot {shlex.quote(remote_snapshot)}
runuser -u atlas -- env -u GITHUB_TOKEN PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(REMOTE_ROOT)} \
  {shlex.quote(REMOTE_PYTHON)} - <<'PY'
{sync_python}PY
curl -fsS https://mcp.atlas.datarelay.run/healthz >/dev/null
"""


def _cleanup_remote(host: str, remote_snapshot: str) -> None:
    script = f"rm -f {shlex.quote(remote_snapshot)}\n"
    _run(
        ["ssh", "-o", "BatchMode=yes", host, "/bin/bash", "-s"],
        input_text=script,
        capture_output=True,
        check=False,
    )


def refresh(*, repo_root: Path, atlas_python: Path, host: str) -> int:
    if not repo_root.is_dir():
        raise RefreshError(f"operator repository is unavailable: {repo_root}")
    if not atlas_python.is_file():
        raise RefreshError(f"operator Atlas interpreter is unavailable: {atlas_python}")

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("PROD_REFRESH=SKIP_LOCKED")
            return 0

        targets = fetch_targets(host)
        print("TARGET_REPOSITORIES=" + ",".join(targets["repositories"]))
        print("TARGET_PROJECTS=" + ",".join(targets["project_ids"]))

        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root)
        remote_snapshot = f"/tmp/atlas-github-lifecycle.{os.getpid()}.json"
        local_snapshot: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="atlas-github-lifecycle.",
                suffix=".json",
                dir="/tmp",
                delete=False,
            ) as tmp:
                local_snapshot = Path(tmp.name)
                os.chmod(local_snapshot, 0o600)
                _run(
                    snapshot_command(
                        atlas_python=atlas_python,
                        repositories=targets["repositories"],
                    ),
                    cwd=repo_root,
                    env=env,
                    stdout=tmp,
                )
            _run(
                ["scp", "-q", str(local_snapshot), f"{host}:{remote_snapshot}"],
                capture_output=True,
            )
            applied = _ssh(
                host,
                _remote_apply_script(remote_snapshot, targets),
                capture_output=True,
            )
            if applied.stdout:
                print(applied.stdout, end="")
            print("PROD_REFRESH=PASS")
            return 0
        finally:
            if local_snapshot is not None:
                local_snapshot.unlink(missing_ok=True)
            _cleanup_remote(host, remote_snapshot)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(os.environ.get("ATLAS_OPERATOR_REPO", str(DEFAULT_REPO_ROOT))),
    )
    parser.add_argument(
        "--atlas-python",
        type=Path,
        default=Path(os.environ.get("ATLAS_OPERATOR_PYTHON", str(DEFAULT_ATLAS_PYTHON))),
    )
    parser.add_argument(
        "--prod-host",
        default=os.environ.get("ATLAS_PROD_HOST", DEFAULT_PROD_HOST),
    )
    parser.add_argument(
        "--print-plan",
        action="store_true",
        help="Read and validate the current production GitHub refresh target set without mutation.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.print_plan:
            targets = fetch_targets(args.prod_host)
            print(json.dumps(targets, sort_keys=True))
            return 0
        return refresh(
            repo_root=args.repo_root.resolve(),
            atlas_python=args.atlas_python.resolve(),
            host=args.prod_host,
        )
    except (OSError, subprocess.CalledProcessError, RefreshError) as exc:
        print(f"PROD_REFRESH=FAIL reason={exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
