"""Owner-controlled Atlas release bundle entrypoint.

This script does not bypass Desktop Commander command policy. It may only be
used in a separately approved, genuinely authorized production operator
session. It never changes Keycloak or canonical GitHub data.

Usage (inside a ROOT-OWNED, mode 0700 extracted bundle on prod-atlas):
  python3 deploy.py inspect
  python3 deploy.py prepare
  python3 deploy.py preflight
  python3 deploy.py deploy     # explicit interactive authorization required

No command defaults to deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import socket
import stat
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True

APP_HEAD = "6118cd26b991008df39f7dc5b65aaea6c77fbf37"
APP_SHA256 = "4399a92a1141d9e8de2f11e4a6e26c3355da10ef430619453dc29ec8badad775"
MANIFEST_SHA256 = "1b267a4d17e630f97dad8206d77044aaa6f7bcb93f79e3c161fd9df28738d386"
OPERATOR_SHA256 = "f1573263a63d436a8d37709d48b8e8065ca2945d92e58cd33e1ffb5f405cef29"
EXPECTED_TRACKED = 437
EXPECTED_HOST = "prod-atlas"
RELEASE_CONFIRM = "DEPLOY 6118CD2 TO PROD-ATLAS"
PYTHON = Path("/usr/bin/python3.12")

BUNDLE = Path(__file__).absolute().parent


class ReleaseRefused(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseRefused("NOT_REGULAR:" + path.name)


def _safe_relative(raw: str) -> Path:
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or raw.startswith("./")
        or "\\" in raw
        or any(part in ("", ".", "..") for part in raw.split("/"))
        or path.is_absolute()
    ):
        raise ReleaseRefused("UNSAFE_BUNDLE_PATH")
    return Path(*path.parts)


def inspect_bundle(base: Path = BUNDLE) -> dict:
    """Reject added files, symlinks, modified code, or altered wheel bytes."""
    marker = base / "bundle.json"
    _regular(marker)
    data = json.loads(marker.read_text("utf-8"))
    if data.get("schema") != 1 or data.get("app_head") != APP_HEAD:
        raise ReleaseRefused("BUNDLE_IDENTITY_MISMATCH")
    provided = data.get("files")
    if not isinstance(provided, dict) or len(provided) < 8:
        raise ReleaseRefused("BUNDLE_MANIFEST_INVALID")
    expected = {}
    for raw, sha in provided.items():
        rel = _safe_relative(raw)
        if str(rel) in expected or not isinstance(sha, str) or len(sha) != 64:
            raise ReleaseRefused("BUNDLE_ENTRY_INVALID")
        expected[str(rel)] = sha
    required = {
        "app.tar": APP_SHA256,
        "tracked.sha256": MANIFEST_SHA256,
        "operator.py": OPERATOR_SHA256,
    }
    for name, sha in required.items():
        if expected.get(name) != sha:
            raise ReleaseRefused("FROZEN_ARTIFACT_IDENTITY_INVALID:" + name)
    for filename in ("deploy.py", "README.md", "requirements.lock", "uv"):
        if filename not in expected:
            raise ReleaseRefused("MISSING_RELEASE_FILE:" + filename)
    wheels = [name for name in expected if name.startswith("wheels/")]
    if len(wheels) != 29 or any(len(Path(name).parts) != 2 or not name.endswith(".whl") for name in wheels):
        raise ReleaseRefused("OFFLINE_WHEEL_SET_INVALID")
    actual = set()
    for folder, dirs, files in os.walk(base, followlinks=False):
        directory = Path(folder)
        for dirname in dirs:
            if (directory / dirname).is_symlink():
                raise ReleaseRefused("SYMLINK_IN_RELEASE_BUNDLE")
        for filename in files:
            path = directory / filename
            _regular(path)
            name = path.relative_to(base).as_posix()
            if name == "bundle.json":
                continue
            actual.add(name)
    if actual != set(expected):
        raise ReleaseRefused("RELEASE_FILE_INVENTORY_MISMATCH")
    for name, sha in expected.items():
        path = base / _safe_relative(name)
        if _sha(path) != sha:
            raise ReleaseRefused("RELEASE_HASH_MISMATCH:" + name)
    print("BUNDLE_SHA256=PASS")
    print("FROZEN_APPLICATION=" + APP_HEAD)
    print("TRACKED_FILES=" + str(EXPECTED_TRACKED))
    print("OFFLINE_WHEELS=" + str(len(wheels)))
    return data


def _require_trusted_operator(base: Path = BUNDLE) -> None:
    if os.geteuid() != 0:
        raise ReleaseRefused("ROOT_OPERATOR_REQUIRED")
    if socket.gethostname().split(".")[0] != EXPECTED_HOST:
        raise ReleaseRefused("WRONG_HOST")
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ReleaseRefused("UNSUPPORTED_PLATFORM")
    if not PYTHON.is_file() or sys.version_info[:2] != (3, 12):
        raise ReleaseRefused("PYTHON_3_12_REQUIRED")
    if base.is_symlink() or not base.is_dir():
        raise ReleaseRefused("BUNDLE_DIRECTORY_INVALID")
    # Never run verified code from a user-writable or symlinked ancestor.
    for part in (base, *base.parents):
        if part.is_symlink():
            raise ReleaseRefused("INSECURE_BUNDLE_ANCESTOR")
        info = part.stat()
        if info.st_uid != 0 or (stat.S_IMODE(info.st_mode) & 0o022):
            raise ReleaseRefused("BUNDLE_MUST_BE_ROOT_OWNED_PROTECTED")
    if stat.S_IMODE(base.stat().st_mode) != 0o700:
        raise ReleaseRefused("BUNDLE_DIRECTORY_MODE_NOT_0700")


def _load_operator(base: Path = BUNDLE):
    path = base / "operator.py"
    _regular(path)
    if _sha(path) != OPERATOR_SHA256:
        raise ReleaseRefused("OPERATOR_HASH_MISMATCH")
    spec = importlib.util.spec_from_file_location("atlas_frozen_operator", path)
    if spec is None or spec.loader is None:
        raise ReleaseRefused("OPERATOR_IMPORT_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.HEAD != APP_HEAD or module.TRACKED_FILE_COUNT != EXPECTED_TRACKED:
        raise ReleaseRefused("OPERATOR_APPLICATION_IDENTITY_MISMATCH")
    if module.MANIFEST_SHA256 != MANIFEST_SHA256:
        raise ReleaseRefused("OPERATOR_MANIFEST_IDENTITY_MISMATCH")
    return module


def _require_baseline(operator) -> None:
    operator._directory(operator.LIVE, exact_mode=0o755)
    if (operator.LIVE / "atlas/mcp_write.py").exists():
        raise ReleaseRefused("LIVE_ALREADY_MODIFIED")
    operator._old_code_digest(operator.LIVE)
    if operator.CODE_BACKUP.is_symlink() or not operator.CODE_BACKUP.is_dir():
        raise ReleaseRefused("ROLLBACK_CODE_BACKUP_MISSING")
    operator._old_code_digest(operator.CODE_BACKUP)
    if not (operator.CODE_BACKUP / ".venv/bin/python").exists():
        raise ReleaseRefused("ROLLBACK_INTERPRETER_MISSING")
    operator._protected_env(operator.ENV)
    operator._protected_env(operator.ENV_BACKUP)
    if operator.ENV.read_bytes() != operator.ENV_BACKUP.read_bytes():
        raise ReleaseRefused("BASELINE_ENV_DIVERGENCE")
    if not (operator.DATA_BACKUP / "manifest.json").is_file():
        raise ReleaseRefused("HISTORIC_DATA_BACKUP_MISSING")
    for p in (
        operator.SOURCE, operator.STAGE, operator.OLD,
        operator.FAILED, operator.ENV_STAGE, operator.ENV_OLD,
        operator.FRESH_BACKUP,
    ):
        if p.exists() or p.is_symlink():
            raise ReleaseRefused("RELEASE_SLOT_OCCUPIED:" + p.name)


def _save_manifest(operator, base: Path = BUNDLE) -> None:
    path = operator.MANIFEST
    content = (base / "tracked.sha256").read_bytes()
    if hashlib.sha256(content).hexdigest() != MANIFEST_SHA256:
        raise ReleaseRefused("MANIFEST_HASH_MISMATCH")
    if path.exists() or path.is_symlink():
        _regular(path)
        metadata = path.stat()
        if (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600
                or _sha(path) != MANIFEST_SHA256):
            raise ReleaseRefused("INSTALLED_MANIFEST_CONFLICT")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
    path.chmod(0o600)
    if path.stat().st_uid != 0 or _sha(path) != MANIFEST_SHA256:
        raise ReleaseRefused("INSTALLED_MANIFEST_FAILED")


def _extract_exact_source(archive: Path, root: Path) -> None:
    root.mkdir(mode=0o755)  # must not overwrite anything, including a failed attempt
    root.chmod(0o755)  # explicit mode even when the operator's umask is 0077
    count = 0
    with tarfile.open(archive, mode="r:") as package:
        for member in package:
            rel = _safe_relative(member.name.rstrip("/"))
            if ".venv" in rel.parts or not (member.isdir() or member.isfile()):
                raise ReleaseRefused("NONREGULAR_SOURCE_ARCHIVE")
            target = root / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if target.exists() or target.is_symlink():
                raise ReleaseRefused("DUPLICATE_SOURCE_ENTRY")
            if member.size > 20 * 1024 * 1024:
                raise ReleaseRefused("SOURCE_MEMBER_OVERSIZED")
            content_stream = package.extractfile(member)
            if content_stream is None:
                raise ReleaseRefused("UNREADABLE_SOURCE_MEMBER")
            content = content_stream.read(member.size + 1)
            if len(content) != member.size:
                raise ReleaseRefused("SOURCE_MEMBER_TRUNCATED")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            count += 1
    if count != EXPECTED_TRACKED:
        raise ReleaseRefused("ARCHIVE_FILE_COUNT_MISMATCH")


def _execute_fixed(argv: list[str], *, timeout: int = 180) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root" if os.geteuid() == 0 else os.environ.get("HOME", "/tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_NO_PROGRESS": "1", "UV_OFFLINE": "true",
    }
    result = subprocess.run(
        argv, env=env, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if result.returncode:
        raise ReleaseRefused(
            "LOCAL_PREPARATION_FAILED:" + Path(argv[0]).name + ":" + str(result.returncode)
        )


def _create_offline_runtime(root: Path, base: Path = BUNDLE) -> None:
    uv = base / "uv"
    python = root / ".venv/bin/python"
    _execute_fixed([str(uv), "venv", "--offline", "--no-python-downloads",
                    "--python", str(PYTHON), str(root / ".venv")])
    _execute_fixed([str(uv), "pip", "install", "--offline", "--no-index",
                    "--find-links", str(base / "wheels"), "--python",
                    str(python), "-r", str(base / "requirements.lock")])
    result = subprocess.run(
        [str(python), "-c", "import atlas.mcp_http, atlas.mcp_auth, atlas.runtime_observability"],
        cwd=root,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root),
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise ReleaseRefused("FROZEN_RUNTIME_IMPORT_FAILURE")


def prepare(operator, base: Path = BUNDLE) -> int:
    _require_baseline(operator)
    _save_manifest(operator, base)
    temporary = operator.SOURCE.with_name(operator.SOURCE.name + ".building")
    if temporary.exists() or temporary.is_symlink():
        raise ReleaseRefused("PARTIAL_BUILD_EXISTS")
    print("PREPARING_ISOLATED_CANDIDATE", flush=True)
    _extract_exact_source(base / "app.tar", temporary)
    _create_offline_runtime(temporary, base)
    operator._manifest(temporary)
    os.rename(temporary, operator.SOURCE)
    print(f"SOURCE_{EXPECTED_TRACKED}_VERIFIED", flush=True)
    result = operator.main(["--prepare", "--expected-head", APP_HEAD])
    if result != 0:
        raise ReleaseRefused("OPERATOR_PREPARE_FAILED:" + str(result))
    print("PREPARED_NO_ACTIVE_CODE_SWITCH", flush=True)
    return 0


def _require_staged(operator) -> None:
    operator._manifest(operator.SOURCE)
    operator._manifest(operator.STAGE)
    operator._flags(operator.ENV_STAGE)


def preflight(operator) -> int:
    _require_staged(operator)
    result = operator.main(["--preflight-only", "--expected-head", APP_HEAD])
    if result != 0:
        raise ReleaseRefused("OPERATOR_PREFLIGHT_FAILED:" + str(result))
    print("PREFLIGHT_QUALIFIED_NO_ACTIVE_CODE_SWITCH", flush=True)
    return 0


def deploy(operator) -> int:
    if not sys.stdin.isatty():
        raise ReleaseRefused("INTERACTIVE_TERMINAL_REQUIRED")
    _require_staged(operator)
    print("WARNING: THIS WILL STOP ATLAS MCP, WEB AND INGRESS BRIEFLY.")
    print("Keycloak will not be restarted. Real production data is snapshotted first.")
    print("Type exactly " + RELEASE_CONFIRM + " to proceed:")
    answer = input("> ").strip()
    if answer != RELEASE_CONFIRM:
        raise ReleaseRefused("OPERATOR_DID_NOT_AUTHORIZE_DEPLOYMENT")
    result = operator.main(["--execute", "--expected-head", APP_HEAD])
    if result != 0:
        raise ReleaseRefused("CUTOVER_FAILED_OR_ROLLED_BACK:" + str(result))
    print("CODE_SWITCH_COMPLETED; NATIVE_CHATGPT_MCP_WRITE_E2E_PENDING", flush=True)
    return 0


def main(argv: list[str] | None = None, *, base: Path = BUNDLE) -> int:
    parser = argparse.ArgumentParser(description="Atlas owner release; never auto-deploy")
    parser.add_argument(
        "action", nargs="?", choices=("inspect", "prepare", "preflight", "deploy"),
        default="inspect",
    )
    action = parser.parse_args(argv).action
    try:
        inspect_bundle(base)
        if action == "inspect":
            print("INSPECT_ONLY_NO_PRODUCTION_CHANGES")
            return 0
        _require_trusted_operator(base)
        operator = _load_operator(base)
        if action == "prepare":
            return prepare(operator, base)
        if action == "preflight":
            return preflight(operator)
        return deploy(operator)
    except (
        ReleaseRefused, OSError, ValueError, KeyError, RuntimeError,
        subprocess.TimeoutExpired, EOFError,
    ) as exc:
        # Preserve the exact failing gate but never echo secret env contents.
        print(type(exc).__name__ + ":" + str(exc)[:300], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
