"""Host-local Cursor resume worker (Issue #47 slice A).

Maps a repository to a local worktree and a durable Cursor Chat ID. The Chat
ID, worktree, and host id stay in host-local configuration. Canonical branch
and a self-contained prompt are supplied at runtime. ``--resume`` names a
transport slot; it does not prove that a chat exists, and chat history is
not authority. This module does not call a model and does not create a chat.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from atlas.chat_audit import (
    CheckpointStore,
    Identity,
    _claim_is_genuinely_live,
    assert_checkpoint_matches_identity,
    require_exact_commit_sha,
)
from atlas.provenance import ValidationError
from atlas.secrets import redact_sensitive_audit_text
from atlas.work_controller import (
    GitRunner,
    PersistSession,
    default_list_persist_sessions,
    list_persist_trust_processes,
    normalize_github_repository,
    sessions_for_worktree,
    validate_clean_worktree_identity,
)

DESCRIPTOR_SCHEMA_VERSION = 1
MAX_PROMPT_CHARS = 8192
MIN_CURSOR_RESUME_TIMEOUT_SEC = 30
DEFAULT_CURSOR_RESUME_TIMEOUT_SEC = 1800
MAX_CURSOR_RESUME_TIMEOUT_SEC = 7200
_HISTORY_ONLY_PROMPTS = frozenset({"/work-resume", "/clear", "/chat-audit-resume"})
_CREATE_ARGV_TOKENS = frozenset({"persist", "create-chat", "-p"})
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_HOST_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_ROOT_KEYS = frozenset(
    {"schema_version", "state_root", "host_id", "projects", "cursor_resume_timeout_sec"}
)
_PROJECT_KEYS = frozenset({"repository", "worktree", "cursor_chat_id"})
SessionList = Callable[[], list[PersistSession]]
ProcessList = Callable[[str], list[tuple[int, str]]]

Spawn = Callable[[list[str], str], int]
HostProbe = Callable[[], str]


@dataclass(frozen=True)
class ProjectDescriptor:
    """Host-local project binding. Not a GitHub checkpoint record."""

    repository: str
    worktree: str
    cursor_chat_id: str

    def github_projection(self) -> dict[str, str]:
        """Canonical repository identity only. Chat ID and host paths stay local."""
        return {"repository": self.repository}


@dataclass(frozen=True)
class HostWorkerConfig:
    state_root: str
    host_id: str
    projects: tuple[ProjectDescriptor, ...]
    cursor_resume_timeout_sec: int = DEFAULT_CURSOR_RESUME_TIMEOUT_SEC


def normalize_host_id(value: str) -> str:
    raw = str(value or "").strip().lower().split(".")[0]
    if not _HOST_ID_RE.fullmatch(raw):
        raise ValidationError("host identity is missing or invalid")
    return raw


def actual_host_id() -> str:
    """Short local hostname. Caller-supplied --host-id is not consulted."""
    return normalize_host_id(socket.gethostname())


def _require_chat_id(chat_id: str) -> str:
    value = str(chat_id or "").strip()
    if not _CHAT_ID_RE.fullmatch(value):
        raise ValidationError("cursor_chat_id is missing or not a durable chat id")
    return value


def _require_bounded_prompt(prompt: str) -> str:
    value = str(prompt or "")
    if value != value.strip() or not value.strip():
        raise ValidationError(
            "resume requires an explicit self-contained canonical prompt"
        )
    if value in _HISTORY_ONLY_PROMPTS:
        raise ValidationError(
            "canonical prompt must be self-contained and must not rely on chat history"
        )
    if len(value) > MAX_PROMPT_CHARS:
        raise ValidationError(
            f"canonical prompt exceeds {MAX_PROMPT_CHARS} characters"
        )
    if value.startswith("-"):
        raise ValidationError("canonical prompt must not look like a CLI flag")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError("canonical prompt contains control characters")
    return value


def _require_resume_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("cursor_resume_timeout_sec must be an integer")
    if (
        value < MIN_CURSOR_RESUME_TIMEOUT_SEC
        or value > MAX_CURSOR_RESUME_TIMEOUT_SEC
    ):
        raise ValidationError(
            "cursor_resume_timeout_sec must be between "
            f"{MIN_CURSOR_RESUME_TIMEOUT_SEC} and {MAX_CURSOR_RESUME_TIMEOUT_SEC}"
        )
    return value


def _reject_unknown_keys(item: dict, allowed: frozenset[str], *, label: str) -> None:
    unknown = sorted(set(item).difference(allowed))
    if unknown:
        raise ValidationError(
            f"unknown {label} field: " + ", ".join(unknown)
        )


def load_host_worker_config(path: Path) -> HostWorkerConfig:
    """Load an allowlisted host-local descriptor. Packet branch is not stored."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError("project descriptor file is not JSON") from exc
    if not isinstance(raw, dict):
        raise ValidationError("project descriptor file must be a JSON object")
    _reject_unknown_keys(raw, _ROOT_KEYS, label="descriptor")
    if raw.get("schema_version") != DESCRIPTOR_SCHEMA_VERSION:
        raise ValidationError(
            f"unsupported project descriptor schema_version: {raw.get('schema_version')}"
        )
    state_root = Path(str(raw.get("state_root") or "")).expanduser()
    if not state_root.is_absolute():
        raise ValidationError("state_root must be an absolute path")
    host_id = normalize_host_id(str(raw.get("host_id") or ""))
    if "cursor_resume_timeout_sec" in raw:
        timeout_sec = _require_resume_timeout(raw.get("cursor_resume_timeout_sec"))
    else:
        timeout_sec = DEFAULT_CURSOR_RESUME_TIMEOUT_SEC
    projects = raw.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValidationError("project descriptors require a non-empty projects list")
    loaded: list[ProjectDescriptor] = []
    seen_repos: set[str] = set()
    seen_chats: set[str] = set()
    for index, item in enumerate(projects):
        if not isinstance(item, dict):
            raise ValidationError(f"projects[{index}] must be an object")
        _reject_unknown_keys(item, _PROJECT_KEYS, label=f"projects[{index}]")
        missing = [
            key
            for key in ("repository", "worktree", "cursor_chat_id")
            if not str(item.get(key) or "").strip()
        ]
        if missing:
            raise ValidationError(
                f"projects[{index}] missing required fields: {', '.join(missing)}"
            )
        repository = normalize_github_repository(str(item["repository"]))
        chat_id = _require_chat_id(str(item["cursor_chat_id"]))
        if repository in seen_repos:
            raise ValidationError(f"duplicate descriptor repository: {repository}")
        if chat_id in seen_chats:
            raise ValidationError("duplicate descriptor cursor_chat_id")
        seen_repos.add(repository)
        seen_chats.add(chat_id)
        worktree = Path(str(item["worktree"]).strip()).expanduser()
        if not worktree.is_absolute():
            raise ValidationError(f"projects[{index}] worktree must be absolute")
        loaded.append(
            ProjectDescriptor(
                repository=repository,
                worktree=str(worktree),
                cursor_chat_id=chat_id,
            )
        )
    return HostWorkerConfig(
        state_root=str(state_root.resolve()),
        host_id=host_id,
        projects=tuple(loaded),
        cursor_resume_timeout_sec=timeout_sec,
    )


def chat_lock_path(state_root: str | Path, chat_id: str) -> Path:
    """State-root lock file for one Chat ID. Equivalent roots resolve together."""
    durable_id = _require_chat_id(chat_id)
    root = Path(state_root).expanduser().resolve()
    if not root.is_absolute():
        raise ValidationError("state_root must be an absolute path")
    digest = hashlib.sha256(durable_id.encode("utf-8")).hexdigest()
    return root / "chat-locks" / f"{digest}.lock"


def chat_domain_lock_path(chat_id: str) -> Path:
    """Host-wide concurrency domain for one Chat ID. Not caller-selectable."""
    durable_id = _require_chat_id(chat_id)
    digest = hashlib.sha256(durable_id.encode("utf-8")).hexdigest()
    return (
        Path.home()
        / ".local"
        / "state"
        / "datarelay-atlas"
        / "host-worker-locks"
        / f"{digest}.lock"
    )


def build_headless_resume_argv(
    chat_id: str,
    *,
    workspace: str,
    prompt: str,
) -> list[str]:
    """Fixed argv. No shell. ``--resume`` does not prove the chat exists.

    The Chat ID is not authority. ``persist`` and ``create-chat`` are absent.
    The bounded prompt is the whole instruction for this invocation.
    """
    durable_id = _require_chat_id(chat_id)
    canonical_prompt = _require_bounded_prompt(prompt)
    workspace_path = Path(workspace)
    if not workspace_path.is_absolute():
        raise ValidationError("cursor workspace must be an absolute path")
    argv = [
        "agent",
        "--print",
        "--resume",
        durable_id,
        "--force",
        "--trust",
        "--workspace",
        str(workspace_path),
        canonical_prompt,
    ]
    if _CREATE_ARGV_TOKENS.intersection(argv):
        raise ValidationError("headless resume argv must not create a chat")
    if argv[2] != "--resume" or argv[3] != durable_id:
        raise ValidationError("headless resume argv must resume the configured chat")
    if argv[6] != "--workspace" or argv[7] != str(workspace_path):
        raise ValidationError("headless resume argv must bind --workspace")
    if argv[-1] != canonical_prompt:
        raise ValidationError("headless resume argv lost the canonical prompt")
    return argv


def _bounded_cursor_diagnostic(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    try:
        return redact_sensitive_audit_text(text, max_chars=300)
    except Exception:
        return "<redacted>"


def spawn_agent_argv(
    argv: list[str],
    cwd: str,
    *,
    timeout_sec: int = DEFAULT_CURSOR_RESUME_TIMEOUT_SEC,
) -> int:
    """Run a fixed argv list. Nonzero, timeout, and start failure fail closed."""
    timeout_sec = _require_resume_timeout(timeout_sec)
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        raise ValidationError("cursor invocation requires an argv list")
    if any("\x00" in part for part in argv):
        raise ValidationError("refusing non-literal cursor argv")
    if _CREATE_ARGV_TOKENS.intersection(argv):
        raise ValidationError("refusing to create a new Cursor chat")
    if "--workspace" not in argv:
        raise ValidationError("cursor argv missing --workspace")
    workspace = argv[argv.index("--workspace") + 1]
    if str(Path(workspace).resolve()) != str(Path(cwd).resolve()):
        raise ValidationError("cursor workspace does not match the validated worktree")
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        detail = _bounded_cursor_diagnostic(exc.stderr or exc.stdout)
        raise ValidationError(f"cursor resume timed out: {detail}") from exc
    except OSError as exc:
        detail = _bounded_cursor_diagnostic(exc)
        raise ValidationError(f"cursor resume failed to start: {detail}") from exc
    if completed.returncode != 0:
        detail = _bounded_cursor_diagnostic(completed.stderr or completed.stdout)
        raise ValidationError(
            f"cursor resume exited {completed.returncode}: {detail}"
        )
    return 0


@contextmanager
def host_worker_run_lock(lock_path: Path) -> Iterator[None]:
    """Non-blocking exclusive lock for one derived Chat ID path."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValidationError("host worker already running") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _select_descriptor(
    projects: tuple[ProjectDescriptor, ...] | list[ProjectDescriptor],
    *,
    repository: str,
) -> ProjectDescriptor:
    expected = normalize_github_repository(repository)
    matches = [item for item in projects if item.repository == expected]
    if len(matches) != 1:
        raise ValidationError(f"expected one local descriptor for {expected}")
    return matches[0]


def persistent_cursor_active(
    worktree: str,
    *,
    list_sessions: SessionList | None = None,
    list_processes: ProcessList | None = None,
) -> bool:
    """True when a live persist session already owns this worktree.

    Observation only. This does not signal, stop, or otherwise mutate sessions.
    """
    sessions = (list_sessions or default_list_persist_sessions)()
    if sessions_for_worktree(sessions, worktree):
        return True
    processes = (list_processes or list_persist_trust_processes)(worktree)
    return bool(processes)


def _default_spawn(timeout_sec: int) -> Spawn:
    def spawn(argv: list[str], cwd: str) -> int:
        return spawn_agent_argv(argv, cwd, timeout_sec=timeout_sec)

    return spawn


class HeadlessCursorDispatcher:
    """Resume one configured binding after an immediate clean-worktree recheck.

    ``--resume`` is not an existence check. The argv never creates a chat.
    """

    def __init__(
        self,
        spawn: Spawn | None = None,
        *,
        timeout_sec: int = DEFAULT_CURSOR_RESUME_TIMEOUT_SEC,
    ) -> None:
        self._spawn = spawn or _default_spawn(timeout_sec)
        self.invocations: list[list[str]] = []

    def resume(
        self,
        descriptor: ProjectDescriptor,
        *,
        branch: str,
        expected_head: str,
        prompt: str,
        git_runner: GitRunner,
    ) -> list[str]:
        identity = validate_clean_worktree_identity(
            descriptor.worktree,
            repository=descriptor.repository,
            branch=branch,
            expected_head=expected_head,
            git_runner=git_runner,
        )
        argv = build_headless_resume_argv(
            descriptor.cursor_chat_id,
            workspace=identity.worktree_path,
            prompt=prompt,
        )
        if (
            argv[0] != "agent"
            or "--resume" not in argv
            or _CREATE_ARGV_TOKENS.intersection(argv)
        ):
            raise ValidationError("refusing to create a new Cursor chat")
        code = self._spawn(argv, identity.worktree_path)
        if code != 0:
            raise ValidationError(f"cursor resume exited {code}")
        self.invocations.append(list(argv))
        return argv


def _bind_checkpoint(
    store: CheckpointStore,
    *,
    repository: str,
    branch: str,
    expected_head: str,
) -> None:
    packet = store.load()
    if packet is None:
        return
    assert_checkpoint_matches_identity(
        packet,
        Identity(
            repository=normalize_github_repository(repository),
            branch=branch,
            head=require_exact_commit_sha(expected_head, label="expected_head"),
        ),
        require_head=True,
    )
    if _claim_is_genuinely_live(packet.slice_claim):
        raise ValidationError("active slice claim held; duplicate invocation refused")


def run_once(
    *,
    config: HostWorkerConfig,
    resume_requested: bool = False,
    repository: str | None = None,
    canonical_branch: str | None = None,
    expected_head: str | None = None,
    prompt: str | None = None,
    store: CheckpointStore | None = None,
    git_runner: GitRunner | None = None,
    spawn: Spawn | None = None,
    host_probe: HostProbe | None = None,
    list_sessions: SessionList | None = None,
    list_processes: ProcessList | None = None,
) -> dict[str, Any]:
    """Run one pass. Idle does no git, model, or Cursor work.

    One Chat ID has one host-wide lock domain, plus the configured state-root
    lock. A caller cannot choose a different lock path. A live persist session
    on the target worktree is a no-op and is not stopped. Nonzero Cursor
    status is not ``resumed``.
    """
    observed_host = normalize_host_id((host_probe or actual_host_id)())
    if observed_host != config.host_id:
        raise ValidationError(
            f"host identity mismatch: observed={observed_host} expected={config.host_id}"
        )
    if not resume_requested:
        return {
            "action": "idle_noop",
            "cursor_calls": 0,
            "model_calls": 0,
            "checkpoint_writes": 0,
        }
    if not repository or not str(canonical_branch or "").strip() or not expected_head:
        raise ValidationError(
            "resume requires repository, canonical_branch, and expected_head"
        )
    if prompt is None:
        raise ValidationError(
            "resume requires an explicit self-contained canonical prompt"
        )
    branch = str(canonical_branch).strip()
    canonical_prompt = _require_bounded_prompt(prompt)
    descriptor = _select_descriptor(config.projects, repository=repository)
    with host_worker_run_lock(chat_domain_lock_path(descriptor.cursor_chat_id)):
        with host_worker_run_lock(
            chat_lock_path(config.state_root, descriptor.cursor_chat_id)
        ):
            if store is not None:
                _bind_checkpoint(
                    store,
                    repository=repository,
                    branch=branch,
                    expected_head=expected_head,
                )
            if persistent_cursor_active(
                descriptor.worktree,
                list_sessions=list_sessions,
                list_processes=list_processes,
            ):
                return {
                    "action": "cursor_active_noop",
                    "cursor_calls": 0,
                    "model_calls": 0,
                    "checkpoint_writes": 0,
                    "sessions_stopped": 0,
                }
            if git_runner is None:
                raise ValidationError("resume requires a git runner")
            dispatcher = HeadlessCursorDispatcher(
                spawn=spawn,
                timeout_sec=config.cursor_resume_timeout_sec,
            )
            argv = dispatcher.resume(
                descriptor,
                branch=branch,
                expected_head=expected_head,
                prompt=canonical_prompt,
                git_runner=git_runner,
            )
        projection = descriptor.github_projection()
        if descriptor.cursor_chat_id in json.dumps(projection, sort_keys=True):
            raise ValidationError("refusing to project Cursor chat id to GitHub")
        return {
            "action": "resumed",
            "cursor_calls": len(dispatcher.invocations),
            "model_calls": 0,
            "checkpoint_writes": 0,
            "argv": argv,
            "github_projection": projection,
        }
