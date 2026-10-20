#!/usr/bin/env python3
"""Owner-operated entrypoint for immutable Atlas offline bundles.

Run from an approved root console, never as a route around a refused tool.
The default action only inspects bytes. GitHub/incident checks are independent
technical prerequisites, not user-E2E attestation or release authorization.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import subprocess
import sys
import tarfile
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_ARCHIVE = "/home/aella/atlas-owner-deploy-826c266-r3-20261019.tar.gz"
DEFAULT_SHA256 = "a3630b24598f7b05a09b5677d2249e97fd4f844e44d93e66d0677a6afa2a0891"
REPOSITORY = "datarelay-labs/datarelay-atlas"
INCIDENT_NUMBER = 356
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_UNPACKED = 192 * 1024 * 1024
MAX_MEMBER = 80 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_HEAD = re.compile(r"[0-9a-f]{40}\Z")
REQUIRED_CHECKS = frozenset({
    "governance-floor / governance-floor",
    "adoption-compliance / compliance",
    "enforcement-reconcile / reconcile",
    "affected-tests / affected",
})
ACTIONS = ("inspect", "unpack", "release-check", "prepare", "preflight", "deploy")


class Refused(RuntimeError):
    """A failed prerequisite. No force/skip switch is provided."""


@dataclass(frozen=True)
class Bundle:
    archive_sha256: str
    app_head: str
    files: dict[str, bytes]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative_name(name: str) -> str:
    if (not name or name.startswith(("/", "./")) or "\\" in name
            or any(ord(c) < 32 for c in name)
            or any(p in ("", ".", "..") for p in name.split("/"))):
        raise Refused("UNSAFE_ARCHIVE_PATH")
    return PurePosixPath(name).as_posix()


def read_regular(path: Path, limit: int = MAX_MEMBER) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(source.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise Refused("NOT_BOUNDED_REGULAR_FILE:" + path.name)
        raw = source.read(limit + 1)
        if len(raw) > limit:
            raise Refused("FILE_TOO_LARGE:" + path.name)
        return raw


def inspect_archive(path: Path, expected_sha256: str) -> Bundle:
    if SHA256.fullmatch(expected_sha256) is None:
        raise Refused("EXPECTED_ARCHIVE_SHA256_INVALID")
    raw = read_regular(path, MAX_ARCHIVE)
    if digest(raw) != expected_sha256:
        raise Refused("ARCHIVE_SHA256_MISMATCH")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            name = relative_name(member.name.rstrip("/") if member.isdir() else member.name)
            if name in seen:
                raise Refused("DUPLICATE_ARCHIVE_ENTRY:" + name)
            seen.add(name)
            if member.isdir():
                if name != "wheels":
                    raise Refused("UNEXPECTED_ARCHIVE_DIRECTORY")
                continue
            if not member.isfile() or member.sparse is not None:
                raise Refused("ARCHIVE_LINK_OR_SPECIAL_FILE")
            if member.size < 0 or member.size > MAX_MEMBER:
                raise Refused("ARCHIVE_MEMBER_TOO_LARGE")
            total += member.size
            if total > MAX_UNPACKED or len(seen) > 128:
                raise Refused("ARCHIVE_LIMIT_EXCEEDED")
            source = archive.extractfile(member)
            if source is None:
                raise Refused("ARCHIVE_MEMBER_UNREADABLE")
            content = source.read(member.size + 1)
            if len(content) != member.size:
                raise Refused("ARCHIVE_MEMBER_TRUNCATED")
            files[name] = content
    if "bundle.json" not in files:
        raise Refused("BUNDLE_MANIFEST_MISSING")
    data = json.loads(files["bundle.json"])
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise Refused("BUNDLE_SCHEMA_INVALID")
    head = data.get("app_head")
    hashes = data.get("files")
    if not isinstance(head, str) or GIT_HEAD.fullmatch(head) is None:
        raise Refused("BUNDLE_HEAD_INVALID")
    if not isinstance(hashes, dict) or set(hashes) != set(files) - {"bundle.json"}:
        raise Refused("BUNDLE_FILE_INVENTORY_MISMATCH")
    required = {"app.tar", "tracked.sha256", "operator.py", "deploy.py", "uv", "requirements.lock", "README.md"}
    if not required.issubset(files):
        raise Refused("BUNDLE_REQUIRED_FILE_MISSING")
    wheels = [n for n in hashes if n.startswith("wheels/")]
    if len(wheels) != 29 or any(len(n.split("/")) != 2 or not n.endswith(".whl") for n in wheels):
        raise Refused("OFFLINE_WHEEL_SET_INVALID")
    for name, expected in hashes.items():
        relative_name(name)
        if not isinstance(expected, str) or SHA256.fullmatch(expected) is None or digest(files[name]) != expected:
            raise Refused("BUNDLE_MEMBER_SHA256_MISMATCH:" + name)
    return Bundle(expected_sha256, head, files)


def trusted_path(path: Path, *, directory: bool) -> None:
    for entry in (path, *path.parents):
        meta = entry.lstat()
        if stat.S_ISLNK(meta.st_mode) or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) & 0o022:
            raise Refused("ROOT_OWNED_PROTECTED_PATH_REQUIRED:" + entry.name)
    if directory and not path.is_dir():
        raise Refused("DIRECTORY_REQUIRED")
    if not directory and not path.is_file():
        raise Refused("REGULAR_FILE_REQUIRED")


def require_operator() -> None:
    if os.geteuid() != 0:
        raise Refused("ROOT_OPERATOR_REQUIRED")
    if socket.gethostname().split(".")[0] != "prod-atlas":
        raise Refused("WRONG_HOST:expected_prod-atlas")
    if sys.version_info[:2] != (3, 12) or not sys.platform.startswith("linux"):
        raise Refused("LINUX_PYTHON_3_12_REQUIRED")
    trusted_path(Path(__file__).absolute(), directory=False)


def kit_root(bundle: Bundle) -> Path:
    return Path("/root") / ("atlas-owner-console-" + bundle.archive_sha256[:16])


@contextmanager
def operator_lock(path: Path = Path("/run/atlas-owner-console.lock")) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
            raise Refused("OPERATOR_LOCK_UNSAFE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("ANOTHER_OPERATOR_IS_RUNNING") from exc
        yield
    finally:
        os.close(fd)


def verify_installed(root: Path, bundle: Bundle) -> None:
    trusted_path(root, directory=True)
    expected = set(bundle.files)
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise Refused("INSTALLED_SYMLINK")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir() or path.relative_to(root).as_posix() != "wheels":
            raise Refused("INSTALLED_UNEXPECTED_DIRECTORY_OR_SPECIAL_FILE")
    if actual != expected:
        raise Refused("INSTALLED_FILE_INVENTORY_MISMATCH")
    for name, content in bundle.files.items():
        path = root / name
        trusted_path(path, directory=False)
        if read_regular(path) != content:
            raise Refused("INSTALLED_FILE_CHANGED:" + name)


def unpack(root: Path, bundle: Bundle) -> None:
    if root.exists() or root.is_symlink():
        verify_installed(root, bundle)
        print("UNPACK_ALREADY_VERIFIED_NO_OVERWRITE")
        return
    trusted_path(root.parent, directory=True)
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    for name, content in bundle.files.items():
        target = root / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700 if name == "uv" else 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    verify_installed(root, bundle)
    print("UNPACKED_VERIFIED_NO_SERVICE_CHANGE")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("GITHUB_REDIRECT_REFUSED")


def github_get(suffix: str) -> object:
    url = "https://api.github.com/repos/" + REPOSITORY + suffix
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Atlas-owner-console"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise Refused("GITHUB_RESPONSE_TOO_LARGE")
        return json.loads(raw)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise Refused("GITHUB_RELEASE_FACTS_UNAVAILABLE:" + type(exc).__name__) from None


def technical_release_blockers(bundle: Bundle, fetch: Callable[[str], object] = github_get) -> list[str]:
    main = fetch("/commits/main")
    incident = fetch("/issues/" + str(INCIDENT_NUMBER))
    rules = fetch("/rules/branches/main")
    blockers = []
    if not isinstance(main, dict) or main.get("sha") != bundle.app_head:
        blockers.append("GITHUB_MAIN_HEAD_MISMATCH:rebuild_from_final_merged_main")
    if not isinstance(incident, dict):
        blockers.append("INCIDENT_STATE_UNAVAILABLE")
    else:
        header = str(incident.get("body", "")).split("\n\n", 1)[0].splitlines()
        if incident.get("state") != "closed" or "SAFETY_FREEZE=ON" in header:
            blockers.append("INCIDENT_356_NOT_RELEASED")
    kinds = {r.get("type") for r in rules if isinstance(r, dict)} if isinstance(rules, list) else set()
    if not {"pull_request", "required_status_checks", "non_fast_forward", "deletion"}.issubset(kinds):
        blockers.append("MAIN_PROTECTION_INCOMPLETE")
    checks = set()
    strict = False
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            params = rule.get("parameters") or {}
            if not isinstance(params, dict):
                continue
            strict |= params.get("strict_required_status_checks_policy") is True
            entries = params.get("required_status_checks") or []
            if isinstance(entries, list):
                checks.update(c.get("context") for c in entries if isinstance(c, dict) and c.get("integration_id") == 15368)
    if not strict or not REQUIRED_CHECKS.issubset(checks):
        blockers.append("REQUIRED_GITHUB_ACTIONS_CHECK_POLICY_INCOMPLETE")
    return blockers


def release_check(bundle: Bundle) -> None:
    blockers = technical_release_blockers(bundle)
    for item in blockers:
        print("BLOCKED=" + item)
    print("USER_E2E_AND_RELEASE_AUTHORITY=NOT_ATTESTED_BY_THIS_PROGRAM")
    if blockers:
        raise Refused("RELEASE_PRECONDITIONS_NOT_MET")
    print("TECHNICAL_PRECONDITIONS_MET:owner_release_acceptance_still_required")


def state_path(root: Path) -> Path:
    return root.parent / (root.name + "-state.json")


def read_state(root: Path, bundle: Bundle) -> dict:
    path = state_path(root)
    if not path.exists():
        return {}
    trusted_path(path, directory=False)
    data = json.loads(read_regular(path, 16384))
    if (not isinstance(data, dict) or data.get("schema") != 1
            or data.get("bundle_sha256") != bundle.archive_sha256
            or data.get("app_head") != bundle.app_head):
        raise Refused("PHASE_STATE_IDENTITY_MISMATCH")
    return data


def record_state(root: Path, bundle: Bundle, completed: str) -> None:
    path = state_path(root)
    if path.exists() or path.is_symlink():
        trusted_path(path, directory=False)
    temporary = path.with_name(path.name + ".new-" + str(os.getpid()))
    raw = json.dumps({"schema": 1, "bundle_sha256": bundle.archive_sha256, "app_head": bundle.app_head, "completed": completed}, sort_keys=True).encode() + b"\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run_phase(root: Path, bundle: Bundle, action: str) -> int:
    verify_installed(root, bundle)
    release_check(bundle)
    previous = read_state(root, bundle).get("completed")
    if action == "preflight" and previous not in {"prepare", "preflight"}:
        raise Refused("PREPARE_MUST_COMPLETE_FIRST")
    if action == "deploy":
        if previous != "preflight":
            raise Refused("PREFLIGHT_MUST_COMPLETE_FIRST")
        if not sys.stdin.isatty():
            raise Refused("INTERACTIVE_TERMINAL_REQUIRED")
    # Clear a previous preflight receipt BEFORE starting another effect.
    # A failed/interrupted deploy must never retain a success-ready receipt.
    pending = {"prepare": "prepare-started", "preflight": "prepare", "deploy": "deploy-started"}[action]
    record_state(root, bundle, pending)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "deploy.py"), action],
        cwd=root, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": "/root", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode:
        raise Refused("UNDERLYING_PHASE_FAILED:" + action + ":" + str(result.returncode))
    record_state(root, bundle, action)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=ACTIONS, default="inspect")
    adjacent = Path(__file__).absolute().parent / Path(DEFAULT_ARCHIVE).name
    parser.add_argument("--archive", type=Path, default=adjacent if adjacent.exists() else Path(DEFAULT_ARCHIVE))
    parser.add_argument("--sha256", default=DEFAULT_SHA256, help="Previously verified outer archive digest; not a release authorization.")
    args = parser.parse_args(argv)
    try:
        bundle = inspect_archive(args.archive, args.sha256)
        print("ARCHIVE_SHA256=" + bundle.archive_sha256)
        print("APPLICATION_HEAD=" + bundle.app_head)
        print("BUNDLE_FILES=" + str(len(bundle.files)))
        if args.action == "inspect":
            print("INSPECT_ONLY:NO_EXTRACTION_NO_SERVICE_CHANGE")
            return 0
        if args.action == "release-check":
            release_check(bundle)
            return 0
        require_operator()
        root = kit_root(bundle)
        with operator_lock():
            if args.action == "unpack":
                unpack(root, bundle)
                print("BUNDLE_DIRECTORY=" + str(root))
                return 0
            return run_phase(root, bundle, args.action)
    except (Refused, OSError, ValueError, tarfile.TarError) as exc:
        print("STOP:" + str(exc)[:240], file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("INTERRUPTED:inspect_underlying_operator_result;do_not_delete_stages", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
