"""Bounded ingress-aware Atlas release transaction.

This module is a candidate for separate privileged operator review, NOT an
alternative way around a denied ChatGPT tool command. Never execute it via a
different tool/host/account to bypass a platform refusal. It has no operation
that edits Keycloak or Atlas durable data. The default CLI is help-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

HEAD = "9177c976a591143b045ebdeabdbccd0d48b87c6c"
MANIFEST_SHA256 = "f49f8327236b94b48912e6b5df8ca4a72469ab50911775d1bfd2ae750d18235e"
OLD_CODE_MCP_HTTP_SHA256 = "189ab2b16991a87b8dbc5512db84a9d711a7848e0a8e6d82eb916db6f07a9fcb"
SUFFIX = "9177c976-20261017"
LIVE = Path("/opt/datarelay-atlas")
SOURCE = Path("/opt/datarelay-atlas-failed-retry-" + SUFFIX)
STAGE = Path("/opt/datarelay-atlas-stage-safe-" + SUFFIX)
OLD = Path("/opt/datarelay-atlas-previous-safe-" + SUFFIX)
FAILED = Path("/opt/datarelay-atlas-failed-safe-" + SUFFIX)
ENV = Path("/etc/datarelay-atlas/service.env")
ENV_STAGE = Path("/etc/datarelay-atlas/service.env.stage-safe-" + SUFFIX)
ENV_OLD = Path("/etc/datarelay-atlas/service.env.previous-safe-" + SUFFIX)
MANIFEST = Path("/tmp/atlas-immutable-main-" + SUFFIX + ".sha256")
DATA = Path("/var/lib/datarelay-atlas")
DATA_BACKUP = Path("/var/backups/datarelay-atlas/atlas-mcp-write-" + SUFFIX.replace("-20261017", "") + "-precutover-20261017")
CODE_BACKUP = Path("/var/backups/datarelay-atlas/active-code-before-pr347-20261017T0249Z")
ENV_BACKUP = Path("/var/backups/datarelay-atlas/atlas-mcp-write-" + SUFFIX.replace("-20261017", "") + "-code-env-20261017/service.env.before")
MCP = "datarelay-atlas.service"
WEB = "datarelay-atlas-web.service"
INGRESS_SOCKET = "datarelay-atlas-ingress.socket"
INGRESS_SERVICE = "datarelay-atlas-ingress.service"
KEYCLOAK = "keycloak.service"
UNIT_ORDER = (MCP, WEB, INGRESS_SOCKET, INGRESS_SERVICE, KEYCLOAK)
ISSUER = "https://auth.atlas.datarelay.run/realms/atlas"
AUTH_DISCOVERY = ISSUER + "/.well-known/openid-configuration"
MCP_HEALTH = "https://mcp.atlas.datarelay.run/healthz"
FLAGS = ("ATLAS_MCP_MEMORY_WRITE_ENABLED", "ATLAS_MCP_SOURCE_WRITE_ENABLED")


class CutoverError(RuntimeError):
    pass


class RolledBack(CutoverError):
    pass


class IncidentUnrecovered(CutoverError):
    pass


def _run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Run a fixed argv, no shell; redact output in errors."""
    result = subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True, check=False,
        timeout=90,
    )
    if result.returncode:
        raise CutoverError("COMMAND_FAILED:" + Path(argv[0]).name + ":" + str(result.returncode))
    return result.stdout


def _require_root() -> None:
    if os.geteuid() != 0:
        raise CutoverError("ROOT_OPERATOR_REQUIRED")


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _directory(path: Path, *, exact_mode: int | None = None) -> None:
    if path.is_symlink() or not path.is_dir():
        raise CutoverError("DIRECTORY_NOT_QUALIFIED:" + path.name)
    if exact_mode is not None and stat.S_IMODE(path.stat().st_mode) != exact_mode:
        raise CutoverError("DIRECTORY_MODE_INVALID:" + path.name)
    for ancestor in (path, *path.parents):
        if ancestor == Path("/"):
            break
        if not stat.S_IMODE(ancestor.stat().st_mode) & 0o001:
            raise CutoverError("DYNAMICUSER_TRAVERSAL_DENIED:" + ancestor.name)


def _protected_env(path: Path) -> None:
    if not _regular(path):
        raise CutoverError("ENV_MISSING:" + path.name)
    meta = path.stat()
    import grp
    if meta.st_uid != 0 or meta.st_gid != grp.getgrnam("atlas").gr_gid:
        raise CutoverError("ENV_OWNER_INVALID:" + path.name)
    if stat.S_IMODE(meta.st_mode) != 0o640:
        raise CutoverError("ENV_MODE_INVALID:" + path.name)


def _flags(path: Path) -> None:
    _protected_env(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    for name in FLAGS:
        actual = [line.strip() for line in lines if line.startswith(name + "=")]
        if actual != [name + "=1"]:
            raise CutoverError("FEATURE_FLAG_MISMATCH:" + name)


def _old_code_digest(path: Path) -> None:
    anchor = path / "atlas/mcp_http.py"
    if not _regular(anchor):
        raise CutoverError("OLD_CODE_IDENTITY_FILE_MISSING")
    if hashlib.sha256(anchor.read_bytes()).hexdigest() != OLD_CODE_MCP_HTTP_SHA256:
        raise CutoverError("OLD_CODE_IDENTITY_HASH_MISMATCH")


def _manifest(path: Path) -> None:
    if not _regular(MANIFEST):
        raise CutoverError("MANIFEST_NOT_PRESENT")
    content = MANIFEST.read_bytes()
    if hashlib.sha256(content).hexdigest() != MANIFEST_SHA256:
        raise CutoverError("MANIFEST_SHA_MISMATCH")
    items = content.decode("utf-8").splitlines()
    if len(items) != 421:
        raise CutoverError("MANIFEST_TRACKED_COUNT_MISMATCH")
    _directory(path, exact_mode=0o755)
    _run(["sha256sum", "--check", "--status", str(MANIFEST)], cwd=path)
    non_venv_files = 0
    for file in path.rglob("*"):
        if ".venv" in file.relative_to(path).parts:
            continue
        if file.is_symlink():
            raise CutoverError("UNTRACKED_RELEASE_SYMLINK")
        if file.is_file():
            non_venv_files += 1
    if non_venv_files != 421:
        raise CutoverError("UNTRACKED_RELEASE_BYTES_PRESENT")
    if not (path / ".venv/bin/python").exists():
        raise CutoverError("CANDIDATE_INTERPRETER_MISSING")


def _check_json(argv: list[str], *, env: dict[str, str] | None = None) -> dict:
    data = json.loads(_run(argv, env=env))
    if not isinstance(data, dict):
        raise CutoverError("INVALID_OPS_RESULT")
    return data


def _atlas_command(command: list[str], *, code: Path) -> dict:
    result = _check_json([
        "runuser", "-u", "atlas", "--", "env",
        "PYTHONPATH=" + str(code), "PYTHONDONTWRITEBYTECODE=1",
        str(code / ".venv/bin/python"), "-m", "atlas", "ops", *command,
    ])
    return result


def _ingress_unit_contract() -> None:
    raw = _run([
        "systemctl", "show", INGRESS_SERVICE,
        "--property=DynamicUser,Requires,After,ExecStart",
    ])
    properties = dict(
        line.split("=", 1) for line in raw.splitlines() if "=" in line
    )
    if properties.get("DynamicUser") != "yes":
        raise CutoverError("INGRESS_DYNAMIC_USER_NOT_SET")
    for key, service in (("Requires", MCP), ("After", MCP), ("After", KEYCLOAK)):
        if service not in properties.get(key, "").split():
            raise CutoverError("INGRESS_DEPENDENCY_INVALID:" + key)
    actual_start = properties.get("ExecStart", "")
    if (
        "/opt/datarelay-atlas/.venv/bin/python" not in actual_start
        or "atlas.sni_ingress" not in actual_start
    ):
        raise CutoverError("INGRESS_EXECSTART_MISMATCH")
    sock = _run(["systemctl", "cat", INGRESS_SOCKET])
    if "ListenStream=0.0.0.0:443" not in sock or "Service=datarelay-atlas-ingress.service" not in sock:
        raise CutoverError("INGRESS_SOCKET_CONTRACT_INVALID")


def _public_json(url: str) -> dict:
    # Verifies CA/TLS and SNI. Never bypass certificate validation.
    req = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(req, timeout=8) as response:
        if response.status != 200 or response.url != url:
            raise CutoverError("PUBLIC_HTTPS_NOT_READY")
        raw = response.read(32769)
    if len(raw) > 32768:
        raise CutoverError("PUBLIC_RESPONSE_OVERSIZED")
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise CutoverError("PUBLIC_RESULT_INVALID")
    return d


def _public_ready() -> None:
    _public_json(MCP_HEALTH)
    auth = _public_json(AUTH_DISCOVERY)
    if auth.get("issuer") != ISSUER:
        raise CutoverError("AUTH_ISSUER_MISMATCH")
    for field in ("authorization_endpoint", "token_endpoint"):
        endpoint = auth.get(field)
        if not isinstance(endpoint, str) or not endpoint.startswith(ISSUER + "/"):
            raise CutoverError("AUTH_ENDPOINT_MISMATCH")


def _web_ready() -> None:
    # The Human UI is intentionally loopback-only, never published on 443.
    url = "http://127.0.0.1:8788/"
    with urlopen(Request(url, method="GET"), timeout=8) as response:
        if response.status != 200 or response.url != url:
            raise CutoverError("LOOPBACK_WEB_NOT_READY")


class SystemOperations:
    """Only explicit paths and systemd unit names, no free-form shell input."""

    def prepare_stage(self) -> None:
        _require_root()
        _directory(SOURCE, exact_mode=0o755)
        if STAGE.exists() or STAGE.is_symlink() or ENV_STAGE.exists():
            raise CutoverError("STAGE_ALREADY_PRESENT")
        _manifest(SOURCE)
        _protected_env(ENV)
        _protected_env(ENV_BACKUP)
        if ENV.read_bytes() != ENV_BACKUP.read_bytes():
            raise CutoverError("LIVE_ENV_CHANGED_FROM_BACKUP")
        shutil.copytree(SOURCE, STAGE, symlinks=True)
        os.chmod(STAGE, 0o755)
        _manifest(STAGE)
        lines = ENV.read_text(encoding="utf-8").splitlines()
        for name in FLAGS:
            matching = [i for i, line in enumerate(lines) if line.startswith(name + "=")]
            if len(matching) > 1:
                raise CutoverError("DUPLICATE_FEATURE_FLAG:" + name)
            if matching:
                lines[matching[0]] = name + "=1"
            else:
                lines.append(name + "=1")
        text = "\n".join(lines) + "\n"
        fd = os.open(ENV_STAGE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(text)
            import grp
            os.chown(ENV_STAGE, 0, grp.getgrnam("atlas").gr_gid)
            os.chmod(ENV_STAGE, 0o640)
        finally:
            pass
        _flags(ENV_STAGE)

    def preflight(self) -> None:
        _require_root()
        _directory(LIVE, exact_mode=0o755)
        _directory(SOURCE, exact_mode=0o755)
        _manifest(STAGE)
        _flags(ENV_STAGE)
        _protected_env(ENV)
        _protected_env(ENV_BACKUP)
        if ENV.read_bytes() != ENV_BACKUP.read_bytes():
            raise CutoverError("LIVE_ENV_NOT_ORIGINAL")
        if OLD.exists() or FAILED.exists() or ENV_OLD.exists():
            raise CutoverError("ROLLBACK_PATH_ALREADY_OCCUPIED")
        # Old rollback backup is intentionally root-private (0700); only the
        # active candidate needs DynamicUser world traversal.
        if CODE_BACKUP.is_symlink() or not CODE_BACKUP.is_dir():
            raise CutoverError("OLD_CODE_BACKUP_MISSING")
        if not (CODE_BACKUP / ".venv/bin/python").exists():
            raise CutoverError("OLD_CODE_BACKUP_NOT_EXECUTABLE")
        if (LIVE / "atlas/mcp_write.py").exists():
            raise CutoverError("LIVE_ALREADY_NEW_CODE")
        _old_code_digest(LIVE)
        _old_code_digest(CODE_BACKUP)
        if not _regular(DATA_BACKUP / "manifest.json"):
            raise CutoverError("DATA_BACKUP_MISSING")
        # Every data-format compatibility probe operates on the disposable
        # restored backup, NEVER on the live durable Atlas data root.
        with tempfile.TemporaryDirectory(prefix="atlas-safe-restore-") as tmp:
            root_env = dict(os.environ)
            root_env["PYTHONPATH"] = str(STAGE)
            root_env["PYTHONDONTWRITEBYTECODE"] = "1"
            base = [str(STAGE / ".venv/bin/python"), "-m", "atlas", "ops"]
            proof = str(Path(tmp) / "proof")
            restored = _check_json(
                [*base, "restore-test", "--backup", str(DATA_BACKUP),
                 "--dest", proof],
                env=root_env,
            )
            if restored.get("status") != "ok":
                raise CutoverError("RESTORE_TEST_NOT_OK")
            upgrade = _check_json(
                [*base, "upgrade", "--data-root", proof], env=root_env
            )
            rollback = _check_json(
                [*base, "rollback", "--data-root", proof,
                 "--target-code", str(CODE_BACKUP)],
                env=root_env,
            )
            if any(
                report.get("status") != "ok" or report.get("rewritten") is not False
                for report in (upgrade, rollback)
            ):
                raise CutoverError("SCHEMA_COMPATIBILITY_NOT_QUALIFIED")
        for unit in UNIT_ORDER:
            _run(["systemctl", "is-active", "--quiet", unit])
        for unit in (MCP, WEB, INGRESS_SOCKET):
            _run(["systemctl", "is-enabled", "--quiet", unit])
        _ingress_unit_contract()
        _public_ready()
        _web_ready()
        # Actual sandboxed ingress interpreter execution under DynamicUser.
        _run([
            "systemd-run", "--wait", "--collect",
            "--unit=atlas-ingress-qualification-" + HEAD[:12],
            "-p", "DynamicUser=yes", "-p", "ProtectSystem=strict",
            "-p", "ProtectHome=yes", "-p", "NoNewPrivileges=yes",
            "-p", "PrivateTmp=yes", "-p", "RestrictAddressFamilies=AF_INET",
            "-p", "IPAddressAllow=localhost", "-p", "IPAddressDeny=any",
            "-p", "Environment=PYTHONPATH=" + str(STAGE),
            str(STAGE / ".venv/bin/python"), "-c", "import atlas.sni_ingress",
        ])
        prod_check = _atlas_command(
            ["check", "--prod", "--env-file", str(ENV_STAGE)], code=STAGE
        )
        if prod_check.get("status") != "ready":
            raise CutoverError("PROD_CHECK_NOT_READY")
        # No preflight operation runs upgrade/rollback over live DATA.

    def stop(self, unit: str) -> None:
        _run(["systemctl", "stop", unit])

    def start(self, unit: str) -> None:
        _run(["systemctl", "start", unit])

    def reset_ingress(self) -> None:
        _run(["systemctl", "reset-failed", INGRESS_SOCKET, INGRESS_SERVICE])

    def swap_code(self) -> None:
        os.rename(LIVE, OLD)
        os.rename(STAGE, LIVE)
        _directory(LIVE, exact_mode=0o755)

    def swap_env(self) -> None:
        os.rename(ENV, ENV_OLD)
        os.rename(ENV_STAGE, ENV)
        _flags(ENV)

    def restore_code(self) -> None:
        if OLD.is_dir():
            if LIVE.exists():
                if FAILED.exists():
                    raise CutoverError("FAILED_SLOT_OCCUPIED")
                os.rename(LIVE, FAILED)
            os.rename(OLD, LIVE)
        if not LIVE.is_dir() or (LIVE / "atlas/mcp_write.py").exists():
            raise CutoverError("OLD_CODE_RESTORE_FAILED")
        _old_code_digest(LIVE)

    def restore_env(self) -> None:
        if ENV_OLD.exists():
            os.replace(ENV_OLD, ENV)
        else:
            shutil.copy2(ENV_BACKUP, ENV)
        _protected_env(ENV)
        if ENV.read_bytes() != ENV_BACKUP.read_bytes():
            raise CutoverError("OLD_ENV_RESTORE_FAILED")

    def verify(self, *, old: bool) -> None:
        for attempt in range(20):
            try:
                for unit in UNIT_ORDER:
                    _run(["systemctl", "is-active", "--quiet", unit])
                _public_ready()
                _web_ready()
                if old:
                    if (LIVE / "atlas/mcp_write.py").exists():
                        raise CutoverError("OLD_CODE_IDENTITY_MISMATCH")
                    _old_code_digest(LIVE)
                    if ENV.read_bytes() != ENV_BACKUP.read_bytes():
                        raise CutoverError("OLD_ENV_IDENTITY_MISMATCH")
                else:
                    _manifest(LIVE)
                    _flags(ENV)
                for unit in (MCP, WEB, INGRESS_SOCKET):
                    _run(["systemctl", "is-enabled", "--quiet", unit])
                return
            except (CutoverError, OSError, ValueError):
                if attempt == 19:
                    raise
                time.sleep(1)


class Cutover:
    """Dependency-ordered transaction. No rollout PASS from a failed rollback."""

    def __init__(self, actions: SystemOperations) -> None:
        self.a = actions

    def rollback(self) -> None:
        # Ensure another normal TERM/INT does not interrupt recovery.
        original_handlers = {
            sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
        }
        for sig in original_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            self._rollback_protected()
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)

    def _rollback_protected(self) -> None:
        errors: list[str] = []
        for unit in (INGRESS_SOCKET, INGRESS_SERVICE, WEB, MCP):
            try:
                self.a.stop(unit)
            except Exception:
                errors.append("STOP_" + unit)
        for action in ("restore_code", "restore_env"):
            try:
                getattr(self.a, action)()
            except Exception:
                errors.append(action.upper())
        for step in (lambda: self.a.start(MCP), lambda: self.a.start(WEB),
                     self.a.reset_ingress, lambda: self.a.start(INGRESS_SOCKET),
                     lambda: self.a.start(INGRESS_SERVICE)):
            try:
                step()
            except Exception:
                errors.append("RESTART_FAILURE")
        try:
            self.a.verify(old=True)
        except Exception:
            errors.append("OLD_PUBLIC_HEALTH_OR_IDENTITY")
        if errors:
            raise IncidentUnrecovered("INCIDENT_UNRECOVERED:" + ",".join(errors))

    def execute(self) -> None:
        # No mutating operation occurs before the full preflight.
        self.a.preflight()
        try:
            self.a.stop(INGRESS_SOCKET)
            self.a.stop(INGRESS_SERVICE)
            self.a.stop(WEB)
            self.a.stop(MCP)
            self.a.swap_code()
            self.a.swap_env()
            self.a.start(MCP)
            self.a.start(WEB)
            self.a.reset_ingress()
            self.a.start(INGRESS_SOCKET)
            self.a.start(INGRESS_SERVICE)
            self.a.verify(old=False)
        except BaseException as error:
            try:
                self.rollback()
            except IncidentUnrecovered:
                raise
            raise RolledBack("CUTOVER_ROLLED_BACK_AFTER:" + type(error).__name__) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atlas reviewed production transaction, not an approval channel")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true", help="Prepare isolated files; no service switch")
    group.add_argument("--preflight-only", action="store_true", help="Qualification without active code switch")
    group.add_argument("--execute", action="store_true", help="Privileged cutover; only via separately authorized operator")
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args(argv)
    if args.expected_head != HEAD:
        print("REFUSED:WRONG_CANDIDATE", file=sys.stderr)
        return 2
    try:
        ops = SystemOperations()
        if args.prepare:
            ops.prepare_stage()
            print("PREPARED_NO_ACTIVE_CODE_SWITCH")
        elif args.preflight_only:
            ops.preflight()
            print("PREFLIGHT_QUALIFIED_NO_ACTIVE_CODE_SWITCH")
        else:
            # An ordinary SIGTERM/SIGINT must raise inside the transaction,
            # so that the same rollback path runs as for any failed step.
            def interrupt(signum: int, _frame: object) -> None:
                raise CutoverError("INTERRUPTED_SIGNAL:" + str(signum))
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                for sig in previous:
                    signal.signal(sig, interrupt)
                Cutover(ops).execute()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            print("CODE_SWITCH_FINISHED_NEEDS_NATIVE_CHATGPT_MCP_E2E")
        return 0
    except (CutoverError, OSError, ValueError, subprocess.TimeoutExpired) as err:
        print(type(err).__name__ + ":" + str(err), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
