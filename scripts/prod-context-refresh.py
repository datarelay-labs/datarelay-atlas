#!/usr/bin/env python3
"""Refresh derived production Atlas GitHub lifecycle/context from the live registry.

Runs on the authenticated operator host. GitHub credentials never move to prod-atlas.
The production registry decides the complete enabled GitHub-backed repository/project
set on every run so adding a registered GitHub project cannot silently stale this job.
Private GitHub content is fetched on the operator host and streamed once to the
existing production Atlas projection writer; the credential itself never crosses.
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
from typing import Any, Callable

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
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_TARGETS = 32
MAX_SOURCES = 256
MAX_SOURCE_CONTENT_BYTES = 1024 * 1024
MAX_SOURCE_PAYLOAD_BYTES = 8 * 1024 * 1024

_REMOTE_TARGETS = r"""
from atlas.provenance import GITHUB_PROVIDER
from atlas.registry import ProjectRegistry
from pathlib import Path
import json

registry = ProjectRegistry(Path("/var/lib/datarelay-atlas"))
repositories = set()
projects = []
for project in registry.list_projects():
    if not project.enabled:
        continue
    github_sources = []
    for source in sorted(project.sources.values(), key=lambda item: item.source_id):
        if not source.enabled or source.provider != GITHUB_PROVIDER:
            continue
        github_sources.append({
            "source_id": source.source_id,
            "source_path": source.source_path,
            "ref": source.ref or project.default_ref,
        })
    if not github_sources:
        continue
    repositories.add(project.repository)
    projects.append({
        "project_id": project.project_id,
        "repository": project.repository,
        "sources": github_sources,
    })
print(json.dumps({
    "repositories": sorted(repositories),
    "projects": sorted(projects, key=lambda item: item["project_id"]),
}, sort_keys=True))
"""

_REMOTE_SYNC = r"""
from atlas.github_sync import FetchedSource
from atlas.local_markdown import fetch_local_markdown
from atlas.provenance import GITHUB_PROVIDER, LOCAL_MARKDOWN_PROVIDER
from atlas.registry import ProjectRegistry
from atlas.service import AtlasService
from pathlib import Path
import json
import os
import re
import sys

MAX_SOURCE_PAYLOAD_BYTES = 8 * 1024 * 1024
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

def no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out

raw = sys.stdin.buffer.read(MAX_SOURCE_PAYLOAD_BYTES + 1)
if not raw or len(raw) > MAX_SOURCE_PAYLOAD_BYTES:
    raise SystemExit("operator source payload size is invalid")
try:
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
    raise SystemExit("operator source payload is invalid") from exc
if not isinstance(payload, dict) or set(payload) != {"schema_version", "targets", "sources"}:
    raise SystemExit("operator source payload fields are invalid")
if payload["schema_version"] != 1:
    raise SystemExit("operator source payload schema is invalid")

registry = ProjectRegistry(Path("/var/lib/datarelay-atlas"))
repositories = set()
projects = []
for project in registry.list_projects():
    if not project.enabled:
        continue
    github_sources = []
    for source in sorted(project.sources.values(), key=lambda item: item.source_id):
        if not source.enabled or source.provider != GITHUB_PROVIDER:
            continue
        github_sources.append({
            "source_id": source.source_id,
            "source_path": source.source_path,
            "ref": source.ref or project.default_ref,
        })
    if not github_sources:
        continue
    repositories.add(project.repository)
    projects.append({
        "project_id": project.project_id,
        "repository": project.repository,
        "sources": github_sources,
    })
current = {
    "repositories": sorted(repositories),
    "projects": sorted(projects, key=lambda item: item["project_id"]),
}
if current != payload["targets"]:
    raise SystemExit("production refresh targets changed during execution")

expected = {}
for project in current["projects"]:
    for source in project["sources"]:
        expected[(project["project_id"], source["source_id"])] = {
            "project_id": project["project_id"],
            "source_id": source["source_id"],
            "repository": project["repository"],
            "ref": source["ref"],
            "source_path": source["source_path"],
        }

items = payload["sources"]
if not isinstance(items, list) or len(items) != len(expected):
    raise SystemExit("operator source payload coverage is incomplete")
fetched = {}
for item in items:
    fields = {
        "project_id", "source_id", "repository", "ref", "source_path",
        "source_revision", "content",
    }
    if not isinstance(item, dict) or set(item) != fields:
        raise SystemExit("operator source payload source fields are invalid")
    key = (item["project_id"], item["source_id"])
    if key in fetched or key not in expected:
        raise SystemExit("operator source payload source identity is invalid")
    identity = {name: item[name] for name in ("project_id", "source_id", "repository", "ref", "source_path")}
    if identity != expected[key]:
        raise SystemExit("operator source payload source identity changed")
    if not isinstance(item["source_revision"], str) or SHA40_RE.fullmatch(item["source_revision"]) is None:
        raise SystemExit("operator source payload revision is invalid")
    if not isinstance(item["content"], str):
        raise SystemExit("operator source payload content is invalid")
    fetched[key] = item
if set(fetched) != set(expected):
    raise SystemExit("operator source payload source set is incomplete")

os.environ.pop("GITHUB_TOKEN", None)
service = AtlasService(Path("/var/lib/datarelay-atlas"))

def fetch(source, _token):
    if source.provider == GITHUB_PROVIDER:
        key = (source.project_id, source.source_id)
        item = fetched.get(key)
        if item is None:
            raise RuntimeError("operator source payload missing registered GitHub source")
        if (
            source.repository != item["repository"]
            or source.ref != item["ref"]
            or source.source_path != item["source_path"]
        ):
            raise RuntimeError("registered GitHub source changed during sync")
        return FetchedSource(
            content=item["content"],
            source_revision=item["source_revision"],
        )
    if source.provider == LOCAL_MARKDOWN_PROVIDER:
        return fetch_local_markdown(service.snapshot_root, source)
    raise RuntimeError("unsupported source provider during operator-mediated sync")

for project in current["projects"]:
    records = service.sync_project(project["project_id"], fetch=fetch)
    states = sorted({record.sync_state for record in records})
    if any(record.sync_state == "error" for record in records):
        raise SystemExit("operator-mediated project sync failed")
    print(f"SYNCED_PROJECT={project['project_id']} STATES={','.join(states)}")
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


def _ssh(
    host: str,
    script: str,
    *,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
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


def _validate_source_path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise RefreshError("production refresh source path is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RefreshError("production refresh source path is invalid")
    return value


def normalize_targets(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"repositories", "projects"}:
        raise RefreshError("production refresh target payload is invalid")
    repositories = payload["repositories"]
    projects = payload["projects"]
    if not isinstance(repositories, list) or not isinstance(projects, list):
        raise RefreshError("production refresh target lists are invalid")
    if not repositories or not projects:
        raise RefreshError("production refresh target set is empty")
    if len(repositories) > MAX_TARGETS or len(projects) > MAX_TARGETS:
        raise RefreshError("production refresh target set is too large")
    if len(set(repositories)) != len(repositories):
        raise RefreshError("production refresh repository set contains duplicates")
    if any(
        not isinstance(item, str) or REPOSITORY_RE.fullmatch(item) is None
        for item in repositories
    ):
        raise RefreshError("production refresh repository identity is invalid")

    normalized_projects: list[dict[str, Any]] = []
    project_ids: set[str] = set()
    total_sources = 0
    seen_repositories: set[str] = set()
    for project in projects:
        if not isinstance(project, dict) or set(project) != {
            "project_id",
            "repository",
            "sources",
        }:
            raise RefreshError("production refresh project fields are invalid")
        project_id = project["project_id"]
        repository = project["repository"]
        sources = project["sources"]
        if (
            not isinstance(project_id, str)
            or PROJECT_ID_RE.fullmatch(project_id) is None
            or project_id in project_ids
        ):
            raise RefreshError("production refresh project identity is invalid")
        if (
            not isinstance(repository, str)
            or REPOSITORY_RE.fullmatch(repository) is None
            or repository not in repositories
        ):
            raise RefreshError("production refresh project repository is invalid")
        if not isinstance(sources, list) or not sources:
            raise RefreshError("production refresh project sources are invalid")
        project_ids.add(project_id)
        seen_repositories.add(repository)
        source_ids: set[str] = set()
        normalized_sources: list[dict[str, str]] = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                "source_id",
                "source_path",
                "ref",
            }:
                raise RefreshError("production refresh source fields are invalid")
            source_id = source["source_id"]
            ref = source["ref"]
            if (
                not isinstance(source_id, str)
                or SOURCE_ID_RE.fullmatch(source_id) is None
                or source_id in source_ids
            ):
                raise RefreshError("production refresh source identity is invalid")
            if not isinstance(ref, str) or REF_RE.fullmatch(ref) is None:
                raise RefreshError("production refresh source ref is invalid")
            source_ids.add(source_id)
            normalized_sources.append(
                {
                    "source_id": source_id,
                    "source_path": _validate_source_path(source["source_path"]),
                    "ref": ref,
                }
            )
            total_sources += 1
            if total_sources > MAX_SOURCES:
                raise RefreshError("production refresh source set is too large")
        normalized_projects.append(
            {
                "project_id": project_id,
                "repository": repository,
                "sources": sorted(
                    normalized_sources,
                    key=lambda item: item["source_id"],
                ),
            }
        )
    if seen_repositories != set(repositories):
        raise RefreshError("production refresh repository coverage is inconsistent")
    return {
        "repositories": sorted(repositories),
        "projects": sorted(
            normalized_projects,
            key=lambda item: item["project_id"],
        ),
    }


def fetch_targets(host: str) -> dict[str, Any]:
    completed = _ssh(host, _target_query_script())
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RefreshError(
            "production refresh target discovery returned invalid JSON"
        ) from exc
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


def _github_token() -> str:
    completed = _run(["gh", "auth", "token"], capture_output=True)
    token = completed.stdout.strip()
    if not token or len(token) > 4096 or any(ch.isspace() for ch in token):
        raise RefreshError("operator GitHub credential is unavailable")
    return token


def build_source_payload(
    targets: dict[str, Any],
    *,
    token: str,
    fetcher: Callable[..., Any] | None = None,
) -> str:
    from atlas.github_sync import fetch_github_file
    from atlas.projection import _contains_unsafe_github_projection_secret
    from atlas.provenance import CanonicalSource

    fetch = fetcher or fetch_github_file
    sources: list[dict[str, str]] = []
    for project in targets["projects"]:
        for item in project["sources"]:
            source = CanonicalSource(
                source_id=item["source_id"],
                project_id=project["project_id"],
                provider="github",
                repository=project["repository"],
                ref=item["ref"],
                source_path=item["source_path"],
            )
            fetched = fetch(source, token)
            content = fetched.content
            revision = fetched.source_revision
            if not isinstance(content, str):
                raise RefreshError("operator GitHub source content is invalid")
            if len(content.encode("utf-8")) > MAX_SOURCE_CONTENT_BYTES:
                raise RefreshError("operator GitHub source content is too large")
            if not isinstance(revision, str) or SHA40_RE.fullmatch(revision) is None:
                raise RefreshError("operator GitHub source revision is invalid")
            if _contains_unsafe_github_projection_secret(content):
                raise RefreshError(
                    "operator GitHub source content failed Atlas secret screening"
                )
            sources.append(
                {
                    "project_id": project["project_id"],
                    "source_id": item["source_id"],
                    "repository": project["repository"],
                    "ref": item["ref"],
                    "source_path": item["source_path"],
                    "source_revision": revision,
                    "content": content,
                }
            )
    payload = {
        "schema_version": 1,
        "targets": targets,
        "sources": sources,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if len(encoded.encode("utf-8")) > MAX_SOURCE_PAYLOAD_BYTES:
        raise RefreshError("operator source payload is too large")
    if token in encoded:
        raise RefreshError("operator credential leaked into source payload")
    return encoded


def _remote_publish_script(remote_snapshot: str) -> str:
    return f"""set -euo pipefail
chown atlas:atlas {shlex.quote(remote_snapshot)}
chmod 600 {shlex.quote(remote_snapshot)}
runuser -u atlas -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(REMOTE_ROOT)} \
  {shlex.quote(REMOTE_PYTHON)} -m atlas --data-root {shlex.quote(REMOTE_DATA_ROOT)} \
  lifecycle publish-github-snapshot --snapshot {shlex.quote(remote_snapshot)}
"""


def _remote_sync_command() -> str:
    return (
        "runuser -u atlas -- env -u GITHUB_TOKEN "
        f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(REMOTE_ROOT)} "
        f"{shlex.quote(REMOTE_PYTHON)} -c {shlex.quote(_REMOTE_SYNC)}"
    )


def apply_source_payload(host: str, payload: str) -> subprocess.CompletedProcess[str]:
    return _run(
        ["ssh", "-o", "BatchMode=yes", host, _remote_sync_command()],
        input_text=payload,
        capture_output=True,
    )


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
        raise RefreshError(
            f"operator Atlas interpreter is unavailable: {atlas_python}"
        )

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("PROD_REFRESH=SKIP_LOCKED")
            return 0

        targets = fetch_targets(host)
        print("TARGET_REPOSITORIES=" + ",".join(targets["repositories"]))
        print(
            "TARGET_PROJECTS="
            + ",".join(project["project_id"] for project in targets["projects"])
        )

        token = _github_token()
        source_payload = build_source_payload(targets, token=token)

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
            published = _ssh(
                host,
                _remote_publish_script(remote_snapshot),
                capture_output=True,
            )
            if published.stdout:
                print(published.stdout, end="")
            synced = apply_source_payload(host, source_payload)
            if synced.stdout:
                print(synced.stdout, end="")
            _run(
                ["curl", "-fsS", "https://mcp.atlas.datarelay.run/healthz"],
                capture_output=True,
            )
            print("PROD_REFRESH=PASS")
            return 0
        finally:
            source_payload = ""
            token = ""
            if local_snapshot is not None:
                local_snapshot.unlink(missing_ok=True)
            _cleanup_remote(host, remote_snapshot)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(
            os.environ.get("ATLAS_OPERATOR_REPO", str(DEFAULT_REPO_ROOT))
        ),
    )
    parser.add_argument(
        "--atlas-python",
        type=Path,
        default=Path(
            os.environ.get("ATLAS_OPERATOR_PYTHON", str(DEFAULT_ATLAS_PYTHON))
        ),
    )
    parser.add_argument(
        "--prod-host",
        default=os.environ.get("ATLAS_PROD_HOST", DEFAULT_PROD_HOST),
    )
    parser.add_argument(
        "--print-plan",
        action="store_true",
        help=(
            "Read and validate the current production GitHub refresh target set "
            "without mutation."
        ),
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
