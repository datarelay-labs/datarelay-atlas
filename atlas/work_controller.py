"""Autonomous Work Controller PoC v0 (ADR-0006).

Persists one local workstream, accepts idempotent Cursor completion events,
runs an independent audit, and either stops or dispatches a fresh /work-resume.
"""

from __future__ import annotations

import base64
import json
import os
import pty
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from atlas.provenance import ValidationError

GitRunner = Callable[[list[str], str], str]

CONTROLLER_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 3
RESUME_PROMPT = "/work-resume"
HEAD_RE = re.compile(r"^[0-9a-f]{7,40}$")
WORKSTREAM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
COMPLETION_INBOX_DIRNAME = "completion-inbox"
COMPLETION_PROCESSED_DIRNAME = "completion-processed"
DEFAULT_OPENAI_API_BASE = "https://api.openai.com/v1"
DEFAULT_AUDIT_MODEL = "gpt-4.1-mini"
OPENAI_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "incomplete"}
)
OPENAI_PENDING_STATUSES = frozenset({"queued", "in_progress"})

AuditVerdict = str  # PASS | REWORK | HUMAN_REQUIRED
ControllerState = str


ALLOWED_STATES = frozenset(
    {
        "IDLE",
        "AWAITING_AUDIT",
        "AUDITING",
        "REWORK_DISPATCHED",
        "PASSED",
        "HUMAN_REQUIRED",
    }
)
TERMINAL_STATES = frozenset({"PASSED", "HUMAN_REQUIRED"})
AUDIT_VERDICTS = frozenset({"PASS", "REWORK", "HUMAN_REQUIRED"})


@dataclass(frozen=True)
class WorktreeIdentity:
    worktree_path: str
    repository: str
    branch: str
    head: str
    toplevel: str


def normalize_github_repository(value: str) -> str:
    """Normalize clone URL or slug to `owner/repo`."""
    raw = value.strip()
    if not raw:
        raise ValidationError("repository identity is empty")
    cleaned = raw.removesuffix(".git")
    if cleaned.startswith("git@"):
        cleaned = cleaned.split(":", 1)[-1]
    elif "github.com/" in cleaned:
        cleaned = cleaned.split("github.com/", 1)[1]
    cleaned = cleaned.strip().strip("/")
    if cleaned.count("/") != 1:
        raise ValidationError(f"unsupported repository identity: {value!r}")
    owner, repo = cleaned.split("/", 1)
    if not owner or not repo:
        raise ValidationError(f"unsupported repository identity: {value!r}")
    return f"{owner}/{repo}"


def require_canonical_target_repo(target: str, repository: str) -> str:
    """Require TARGET_REPO as exact `owner/repo` matching *repository*.

    `/work-resume` selects packets by exact TARGET_REPO string match against the
    resolved owner/repo slug. Accepting clone-URL forms here would mutate and
    dispatch while resume finds zero packets.
    """
    repo = normalize_github_repository(repository)
    raw = (target or "").strip()
    if not raw:
        raise ValidationError("work packet missing TARGET_REPO metadata")
    try:
        observed = normalize_github_repository(raw)
    except ValidationError as exc:
        raise ValidationError(
            f"work packet TARGET_REPO must be canonical owner/repo slug "
            f"(got {target!r})"
        ) from exc
    if raw != observed:
        raise ValidationError(
            f"work packet TARGET_REPO must be canonical owner/repo slug "
            f"(got {target!r}; expected {repo!r})"
        )
    if observed != repo:
        raise ValidationError(
            f"work packet TARGET_REPO mismatch: {observed} != {repo}"
        )
    return repo


def heads_match(expected: str, observed: str) -> bool:
    exp = expected.strip().lower()
    obs = observed.strip().lower()
    if not HEAD_RE.match(exp) or not HEAD_RE.match(obs):
        return False
    return exp == obs or exp.startswith(obs) or obs.startswith(exp)


def default_git_runner(argv: list[str], cwd: str) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValidationError(
            f"git command failed ({' '.join(argv)}): {detail or completed.returncode}"
        )
    return completed.stdout.strip()


def validate_worktree_identity(
    worktree_path: str,
    *,
    repository: str,
    branch: str,
    expected_head: str,
    git_runner: GitRunner | None = None,
) -> WorktreeIdentity:
    """Fail closed unless local worktree matches exact repo/branch/HEAD."""
    runner = git_runner or default_git_runner
    worktree = Path(worktree_path).resolve()
    if not worktree.is_dir():
        raise ValidationError(f"worktree_path is not a directory: {worktree}")
    cwd = str(worktree)

    toplevel = Path(runner(["git", "rev-parse", "--show-toplevel"], cwd)).resolve()
    if toplevel != worktree:
        raise ValidationError(
            f"worktree toplevel mismatch: observed={toplevel} expected={worktree}"
        )

    origin = runner(["git", "remote", "get-url", "origin"], cwd)
    observed_repo = normalize_github_repository(origin)
    expected_repo = normalize_github_repository(repository)
    if observed_repo != expected_repo:
        raise ValidationError(
            f"repository mismatch: observed={observed_repo} expected={expected_repo}"
        )

    observed_branch = runner(["git", "branch", "--show-current"], cwd)
    if not observed_branch:
        raise ValidationError(
            "worktree is detached; expected an exact branch checkout "
            f"matching {branch!r}"
        )
    if observed_branch != branch:
        raise ValidationError(
            f"branch mismatch: observed={observed_branch} expected={branch}"
        )

    observed_head = runner(["git", "rev-parse", "HEAD"], cwd).lower()
    if not heads_match(expected_head, observed_head):
        raise ValidationError(
            f"head mismatch: observed={observed_head} expected={expected_head.lower()}"
        )

    return WorktreeIdentity(
        worktree_path=cwd,
        repository=expected_repo,
        branch=observed_branch,
        head=observed_head,
        toplevel=str(toplevel),
    )


def require_clean_porcelain(
    worktree_path: str,
    *,
    git_runner: GitRunner | None = None,
) -> None:
    """Fail closed unless ``git status --porcelain --untracked-files=all`` is empty.

    Autonomous audit/dispatch paths require a committed, clean worktree.
    Explicit ``--untracked-files=all`` prevents ``status.showUntrackedFiles=no``
    (or similar config) from hiding untracked files. A status digest is not
    accepted as a substitute for clean porcelain.
    """
    runner = git_runner or default_git_runner
    cwd = str(Path(worktree_path).resolve())
    porcelain = runner(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd,
    )
    if str(porcelain or "").strip():
        raise ValidationError(
            "worktree is dirty; git status --porcelain --untracked-files=all "
            "is not empty"
        )


def validate_clean_worktree_identity(
    worktree_path: str,
    *,
    repository: str,
    branch: str,
    expected_head: str,
    git_runner: GitRunner | None = None,
) -> WorktreeIdentity:
    """Validate exact repo/branch/HEAD and require a clean porcelain worktree.

    Porcelain is checked after the first identity read. A commit in that
    window can be clean at a new HEAD, so identity is read again and the
    post-clean identity is what callers receive.
    """
    validate_worktree_identity(
        worktree_path,
        repository=repository,
        branch=branch,
        expected_head=expected_head,
        git_runner=git_runner,
    )
    require_clean_porcelain(worktree_path, git_runner=git_runner)
    return validate_worktree_identity(
        worktree_path,
        repository=repository,
        branch=branch,
        expected_head=expected_head,
        git_runner=git_runner,
    )


@dataclass(frozen=True)
class CompletionEvent:
    event_id: str
    workstream: str
    issue_number: int
    branch: str
    head: str
    attempt: int
    session_id: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "CompletionEvent":
        missing = [
            key
            for key in (
                "event_id",
                "workstream",
                "issue_number",
                "branch",
                "head",
                "attempt",
            )
            if key not in raw or raw[key] in (None, "")
        ]
        if missing:
            raise ValidationError(
                f"completion event missing required fields: {', '.join(missing)}"
            )
        try:
            issue_number = int(raw["issue_number"])
            attempt = int(raw["attempt"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("issue_number and attempt must be integers") from exc
        return cls(
            event_id=str(raw["event_id"]).strip(),
            workstream=str(raw["workstream"]).strip(),
            issue_number=issue_number,
            branch=str(raw["branch"]).strip(),
            head=str(raw["head"]).strip().lower(),
            attempt=attempt,
            session_id=(
                str(raw["session_id"]).strip()
                if raw.get("session_id") not in (None, "")
                else None
            ),
        )


@dataclass(frozen=True)
class AuditResult:
    verdict: str
    findings: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in AUDIT_VERDICTS:
            raise ValidationError(f"invalid audit verdict: {self.verdict}")


@dataclass(frozen=True)
class DispatchRequest:
    workstream: str
    worktree_path: str
    branch: str
    issue_number: int
    attempt: int
    repository: str
    expected_head: str
    resume_prompt: str = RESUME_PROMPT


@dataclass(frozen=True)
class DispatchResult:
    session_id: str
    command: list[str]
    resource_preflight_result: str = ""
    resource_preflight_reason: str = ""


@dataclass
class WorkstreamRecord:
    workstream: str
    repository: str
    issue_number: int
    branch: str
    worktree_path: str
    expected_head: str
    state: str = "IDLE"
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    last_event_id: str | None = None
    last_session_id: str | None = None
    last_audit_verdict: str | None = None
    last_findings: str = ""
    last_outcome: dict = field(default_factory=dict)
    processed_event_ids: list[str] = field(default_factory=list)
    pending_event: dict | None = None


class AuditPort(Protocol):
    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        ...


class WorkPacketPort(Protocol):
    def apply_rework_findings(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str,
        workstream: str,
        findings: str,
        attempt: int,
        head: str,
    ) -> None:
        ...

    def apply_dispatch_blocked(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str,
        workstream: str,
        findings: str,
        attempt: int,
        head: str,
        reason: str,
    ) -> None:
        ...


class CursorDispatchPort(Protocol):
    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        ...


class ObserverPort(Protocol):
    def observe(self, message: str, payload: dict) -> None:
        ...


class NullObserver:
    def observe(self, message: str, payload: dict) -> None:
        return None


class RecordingObserver:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    def observe(self, message: str, payload: dict) -> None:
        self.messages.append((message, payload))


class FixedAuditAdapter:
    """Deterministic audit adapter for tests and offline dogfood."""

    def __init__(self, result: AuditResult) -> None:
        self._result = result
        self.calls: list[CompletionEvent] = []

    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        self.calls.append(event)
        return self._result


DEFAULT_MAX_REWORK_FINDINGS_CHARS = 4000
_WORK_PACKET_SECTION_HEADINGS = (
    "Goal",
    "Current State",
    "Next Action",
    "Constraints",
    "Canonical References",
    "Latest Evidence",
    "Blockers",
)


def _credential_name_pattern() -> str:
    """Shared credential-name alternation for assignment detection/redaction."""
    return (
        r"(?:"
        r"OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|"
        r"AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|"
        r"AZURE_CLIENT_SECRET|NPM_TOKEN|"
        # Standalone names first so bare `password` / `secret` / `token` /
        # camelCase `apiKey` and hyphenated `api-key` / `X-API-Key` match.
        r"PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY_ID|ACCESS_KEY|apiKey|"
        r"X-API-Key|API-Key|"
        r"[A-Za-z_][A-Za-z0-9_-]*?(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY|ACCESS_KEY_ID|ApiKey|API-Key)"
        r")"
    )


def _normalize_json_quote_escapes(text: str) -> str:
    """Peel nested ``json.dumps`` quote-escapes in linear time.

    Collapses ``\\\\\"`` runs produced by repeated serialization into plain
    quotes so detectors stay linear-time, while preserving a single ``\\\"``
    escape inside credential values.
    """
    cur = text or ""
    prev = None
    while prev != cur:
        prev = cur
        cur = cur.replace('\\\\"', '"').replace("\\\\'", "'")
    return cur


# Explicit annotation atoms only. Capitalized identifiers are not annotations
# unless listed here (``SecretStr`` is the justified pydantic annotation).
_COLON_TYPE_ATOMS = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "object",
        "type",
        "list",
        "dict",
        "set",
        "tuple",
        "None",
        "True",
        "False",
        "Any",
        "Optional",
        "Union",
        "Literal",
        "Final",
        "ClassVar",
        "Annotated",
        "List",
        "Dict",
        "Set",
        "Tuple",
        "Mapping",
        "Sequence",
        "Callable",
        "Iterable",
        "Iterator",
        "SecretStr",
    }
)
_COLON_TYPE_MAX_DEPTH = 2


def _skip_annotation_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _read_annotation_identifier(text: str, index: int) -> tuple[str, int]:
    if index >= len(text) or not (text[index].isalpha() or text[index] == "_"):
        return "", index
    end = index + 1
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    return text[index:end], end


def _parse_type_primary(text: str, index: int, depth: int) -> tuple[bool, int]:
    """Parse one explicit atom, optionally with bounded generic arguments."""
    index = _skip_annotation_space(text, index)
    name, index = _read_annotation_identifier(text, index)
    if name not in _COLON_TYPE_ATOMS:
        return False, index
    if index >= len(text) or text[index] != "[":
        return True, index
    if depth >= _COLON_TYPE_MAX_DEPTH:
        return False, index
    index += 1
    ok, index = _parse_type_expr(text, index, depth + 1)
    if not ok:
        return False, index
    while True:
        cursor = _skip_annotation_space(text, index)
        if cursor < len(text) and text[cursor] == ",":
            ok, index = _parse_type_expr(text, cursor + 1, depth + 1)
            if not ok:
                return False, index
            continue
        index = cursor
        break
    if index >= len(text) or text[index] != "]":
        return False, index
    return True, index + 1


def _parse_type_expr(text: str, index: int, depth: int) -> tuple[bool, int]:
    """Parse explicit atoms, generics, and ``|`` unions. No other identifiers."""
    ok, index = _parse_type_primary(text, index, depth)
    if not ok:
        return False, index
    while True:
        cursor = _skip_annotation_space(text, index)
        if cursor >= len(text) or text[cursor] != "|":
            return True, index
        ok, index = _parse_type_primary(text, cursor + 1, depth)
        if not ok:
            return False, index


def _annotation_tail_is_structural(rest: str, end: int) -> bool:
    """True when nothing after the annotation is more scalar payload.

    A newline ends the scalar. Horizontal whitespace may precede a structural
    closer, but ``token: str = ...`` and any other same-line payload are not
    annotations. ``)`` ``]`` ``}`` may close an outer form.
    """
    index = end
    while index < len(rest):
        char = rest[index]
        if char in " \t":
            index += 1
            continue
        if char in "\r\n":
            return True
        if char in ")]}":
            index += 1
            continue
        return False
    return True


def _colon_value_is_type_annotation(rest: str) -> tuple[bool, int]:
    """Return whether an unquoted colon value is only an explicit annotation.

    The grammar must consume the whole scalar. ``password: str,hunter2`` is
    not an annotation. An empty value is not a credential. A quoted value is
    left to the quoted-credential matcher.
    """
    if not rest or rest[0].isspace() or rest[0] in "\"'":
        return True, 0
    ok, end = _parse_type_expr(rest, 0, 0)
    if not ok or end <= 0:
        return False, 0
    if not _annotation_tail_is_structural(rest, end):
        return False, 0
    return True, end


def _is_colon_type_or_prose_value(value: str) -> bool:
    """True when ``value`` is entirely an explicit type annotation.

    ``token: str`` and ``apiKey: SecretStr`` stay exempt. Arbitrary
    capitalized values such as ``password: DummySecret`` do not.
    """
    text = value or ""
    if not text.strip():
        return True
    is_annotation, consumed = _colon_value_is_type_annotation(text.strip())
    return is_annotation and consumed == len(text.strip())


def _looks_like_secret(text: str) -> bool:
    """Detect likely live credentials, not mere documentation mentions."""
    name = _credential_name_pattern()
    scan = _normalize_json_quote_escapes(text)
    # Quoted values may contain whitespace/commas and the opposite quote
    # character; only the selected delimiter ends the value (escapes allowed).
    # Nested dumps are peeled first; at most one JSON ``\\\"`` remains.
    key_q = r'((?:\\?["\'])?)'
    val_q = r'(\\?["\'])'
    if re.search(
        rf'(?i){key_q}({name})\1\s*[:=]\s*{val_q}((?:\\.|(?!\3).)*)\3',
        scan,
    ):
        return True
    # Bare equals assignments without whitespace in the value.
    if re.search(
        rf'(?i){key_q}({name})\1\s*=\s*([^\s,"\'}}\]]+)',
        scan,
    ):
        return True
    # Bare colon: exempt only explicit type annotations, not arbitrary
    # capitalized identifiers (``password: DummySecret`` is a credential).
    for match in re.finditer(rf'(?i){key_q}({name})\1\s*:\s*', scan):
        is_annotation, _consumed = _colon_value_is_type_annotation(scan[match.end() :])
        if not is_annotation:
            return True
    if re.search(r"\bsk-[A-Za-z0-9]{20,}\b", scan):
        return True
    if re.search(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b", scan):
        return True
    if re.search(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", scan):
        return True
    if re.search(r"\bAKIA[0-9A-Z]{16}\b", scan):
        return True
    if re.search(r"(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*", scan):
        return True
    if _has_live_basic_credential(scan):
        return True
    if _has_unsafe_url_userinfo(scan):
        return True
    if _PEM_PRIVATE_KEY_RE.search(text or ""):
        return True
    return False


# Multi-segment absolute POSIX/Windows paths. Lookbehind excludes URL authorities
# (`https://...`) by rejecting a match that starts immediately after `:` or `/`.
_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/])(/(?:[^/\s\"'`]{1,255}/){1,}[^/\s\"'`]{1,255})"
    r"|([A-Za-z]:\\(?:[^\\\s\"'`]+\\)+[^\\\s\"'`]+)"
)
_URL_RE = re.compile(r"https?://[^\s\"'`]+", re.IGNORECASE)
# Credential-bearing URI userinfo (any scheme), e.g. postgresql://u:p@host/db.
_URL_USERINFO_RE = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/\s\"'`]+@)"
)
# scheme://<redacted>@host is safe only when that @ is the authority's only one.
_SAFE_REDACTED_URL_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://<redacted>@(?=[^@\s/?#]*(?:[/?#]|\s|$))"
)


def _has_unsafe_url_userinfo(text: str) -> bool:
    """True when a URL authority still carries live userinfo.

    ``scheme://<redacted>@host`` is the safe redaction form. A second ``@``
    or any other userinfo body is not.
    """
    for match in _URL_USERINFO_RE.finditer(text or ""):
        userinfo = match.group("userinfo")
        if not userinfo.endswith("@"):
            return True
        body = userinfo[:-1]
        if body != "<redacted>" or "@" in body:
            return True
    return False
_PEM_PRIVATE_KEY_RE = re.compile(
    # Full PEM label grammar for private keys: optional hyphenated tokens
    # before "PRIVATE KEY", with a matching END label.
    r"-----BEGIN ((?:[A-Z0-9][A-Z0-9-]* )*)PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END \1PRIVATE KEY-----"
)

_CREDENTIAL_NAME = _credential_name_pattern()
# Quoted value first so whitespace/commas and the opposite quote inside the
# selected delimiter are fully captured (including escaped delimiters).
# Bound escape depth (no ``\\\\*``) to keep redaction linear-time; detection
# normalizes deeper nesting before matching.
_SECRET_KV_QUOTED_RE = re.compile(
    rf'(?i)(?P<kq>(?:(?:\\){{0,8}}["\'])?)(?P<key>{_CREDENTIAL_NAME})(?P=kq)'
    rf'\s*[:=]\s*(?P<vq>(?:\\){{0,8}}["\'])(?P<val>(?:\\.|(?!(?P=vq)).)*)(?P=vq)'
)
# Bare key=value. Spaces stay in the value so ``KEY=<redacted> live`` is
# removed together; a leading quote belongs to the quoted matcher.
_SECRET_KV_BARE_RE = re.compile(
    rf'(?i)(?P<kq>(?:(?:\\){{0,8}}["\'])?)(?P<key>{_CREDENTIAL_NAME})(?P=kq)'
    rf'\s*=\s*(?P<val>[^\r\n\"\']+)'
)
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(
    r"(?i)\bBasic\s+(?P<token>[A-Za-z0-9+/_-]{4,}={0,2})(?![A-Za-z0-9+/_-])"
)
_BASIC_AUTH_HEADER_PREFIX_RE = re.compile(
    r"(?i)(?:^|[\s\"'])(?:Proxy-)?Authorization\s*[:=]\s*$"
)


def _basic_token_decodes_to_userinfo(token: str) -> bool:
    """True when the token is valid base64 for ``user:password`` material."""
    padded = token + ("=" * ((-len(token)) % 4))
    try:
        raw = base64.b64decode(padded, validate=True)
    except (ValueError, TypeError):
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.isprintable() or ":" not in text:
        return False
    user, _, password = text.partition(":")
    return bool(user) and bool(password)


def _basic_match_is_credential(match: re.Match[str]) -> bool:
    """Authorization headers are credentials; bare Basic uses decoded userinfo.

    ``Authorization: Basic Yjph`` is a real credential even though the token
    is title case. Bare prose such as ``Basic authentication`` does not decode
    to ``user:password``.
    """
    prefix = match.string[max(0, match.start() - 80) : match.start()]
    if _BASIC_AUTH_HEADER_PREFIX_RE.search(prefix):
        return True
    return _basic_token_decodes_to_userinfo(match.group("token"))


def _has_live_basic_credential(text: str) -> bool:
    return any(
        _basic_match_is_credential(match)
        for match in _BASIC_AUTH_RE.finditer(text or "")
    )


def _redact_basic_auth(match: re.Match[str]) -> str:
    if not _basic_match_is_credential(match):
        return match.group(0)
    return "Basic <redacted>"


def redact_absolute_paths(text: str) -> str:
    """Replace host-local absolute paths before GitHub Work Packet persistence.

    Canonical ``http(s)://`` URLs are preserved; only filesystem paths are
    replaced with ``<local-path>``.
    """
    urls: list[str] = []

    def _park_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"__URL_{len(urls) - 1}__"

    protected = _URL_RE.sub(_park_url, text or "")
    protected = _ABS_PATH_RE.sub("<local-path>", protected)
    for index, url in enumerate(urls):
        protected = protected.replace(f"__URL_{index}__", url)
    return protected


def _redact_secret_kv(match: re.Match[str]) -> str:
    """Preserve credential key syntax; replace only the secret value."""
    key_q = match.group("kq") or ""
    key = match.group("key")
    val_q = match.groupdict().get("vq") or ""
    after_key = match.group(0)[len(key_q) + len(key) + len(key_q) :]
    separator = ":" if after_key.lstrip().startswith(":") else "="
    if separator == ":" and not val_q:
        bare_val = match.groupdict().get("val") or ""
        if _is_colon_type_or_prose_value(bare_val):
            return match.group(0)
    return f"{key_q}{key}{key_q}{separator}{val_q}<redacted>{val_q}"


def _redact_unquoted_colon_credentials(text: str) -> str:
    """Redact unquoted ``key: value`` credentials; keep explicit annotations."""
    name = _credential_name_pattern()
    pattern = re.compile(
        rf'(?i)(?P<kq>(?:(?:\\){{0,8}}["\'])?)(?P<key>{name})(?P=kq)\s*:\s*'
    )
    pieces: list[str] = []
    pos = 0
    for match in pattern.finditer(text):
        is_annotation, _consumed = _colon_value_is_type_annotation(text[match.end() :])
        if is_annotation:
            continue
        bare = re.match(r"[^\r\n]*", text[match.end() :])
        value_len = len(bare.group(0)) if bare else 0
        key_q = match.group("kq") or ""
        key = match.group("key")
        pieces.append(text[pos : match.start()])
        pieces.append(f"{key_q}{key}{key_q}:<redacted>")
        pos = match.end() + value_len
    pieces.append(text[pos:])
    return "".join(pieces)


def redact_sensitive_audit_text(text: str, *, max_chars: int = 300) -> str:
    """Redact secrets and absolute paths for durable AuditResult findings."""
    cleaned = redact_absolute_paths(text or "")
    cleaned = _PEM_PRIVATE_KEY_RE.sub("<redacted-private-key>", cleaned)
    # Normalize nested JSON quote-escapes before KV matching so deep dumps
    # still redact without unbounded backtracking.
    cleaned = _normalize_json_quote_escapes(cleaned)
    cleaned = _SECRET_KV_QUOTED_RE.sub(_redact_secret_kv, cleaned)
    cleaned = _redact_unquoted_colon_credentials(cleaned)
    cleaned = _SECRET_KV_BARE_RE.sub(_redact_secret_kv, cleaned)
    cleaned = _SECRET_TOKEN_RE.sub("<redacted>", cleaned)
    cleaned = _BEARER_RE.sub("Bearer <redacted>", cleaned)
    cleaned = _BASIC_AUTH_RE.sub(_redact_basic_auth, cleaned)
    cleaned = _URL_USERINFO_RE.sub(r"\g<scheme><redacted>@", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars]


def _strip_safe_redaction_placeholders(text: str) -> str:
    """Remove exact safe redaction markers before secret classification.

    Assignments like ``OPENAI_API_KEY=<redacted>``, ``Bearer <redacted>``, and
    ``<redacted-private-key>`` are intentional sanitizer output and must not
    trip the Codex prompt guard or packet persistence rejector.
    """
    cleaned = _normalize_json_quote_escapes(text)
    name = _credential_name_pattern()
    # Value must end at the placeholder. JSON ``"key":`` after a comma is only
    # accepted for colon assignments (not shell ``PASSWORD="...", "x":``).
    key_q = r'((?:\\?["\'])?)'
    val_q = r'(\\?["\'])'
    # Horizontal whitespace terminates a placeholder only when no further
    # same-line scalar follows. ``password: <redacted> hunter2`` stays live.
    common_end = (
        r'$|[\r\n]|\\["n]'
        r'|[ \t]*(?:$|[\r\n])'
        r'|[\}\]](?=$|[\s,\}\]]|\\["n])'
        r'|\\?["\'](?=$|[\s,\}\]]|\\["n])'
    )
    value_end_eq = (
        rf'(?={common_end}|,(?=$|[\s\}}]|\\["n]))'
    )
    value_end_colon = (
        rf'(?={common_end}|,(?:$|[\s\}}]|\\["n]|\\?["\'][^"\']+?\\?["\']\s*:))'
    )
    for sep, value_end in (("=", value_end_eq), (":", value_end_colon)):
        cleaned = re.sub(
            rf'(?i){key_q}({name})\1\s*{re.escape(sep)}\s*{val_q}<redacted>\3'
            rf'{value_end}',
            "",
            cleaned,
        )
        cleaned = re.sub(
            rf'(?i){key_q}({name})\1\s*{re.escape(sep)}\s*<redacted>{value_end}',
            "",
            cleaned,
        )
    cleaned = re.sub(
        rf'(?i)Bearer\s+<redacted>(?={common_end}|,(?=$|[\s\}}]|\\["n]))',
        "",
        cleaned,
    )
    cleaned = re.sub(
        rf'(?i)Basic\s+<redacted>(?={common_end}|,(?=$|[\s\}}]|\\["n]))',
        "",
        cleaned,
    )
    # Safe URL form after userinfo redaction: scheme://<redacted>@host with
    # no second authority marker. A later @ still carries live userinfo.
    cleaned = _SAFE_REDACTED_URL_RE.sub("", cleaned)
    cleaned = cleaned.replace("<redacted-private-key>", "")
    return cleaned


def _contains_unsafe_secret(text: str) -> bool:
    """True when text still looks like a live credential after safe markers."""
    cleaned = _strip_safe_redaction_placeholders(text)
    if _looks_like_secret(cleaned):
        return True
    # Any ``Bearer <redacted>`` that survived stripping still has a live suffix
    # (for example ``Bearer <redacted>,hunter2`` or ``Bearer <redacted>"hunter2"``).
    if re.search(r"(?i)Bearer\s+<redacted>", cleaned):
        return True
    if re.search(r"(?i)Basic\s+<redacted>", cleaned):
        return True
    if re.search(r"(?i)\b[a-z][a-z0-9+.-]*://<redacted>@[^/\s?#]*@", cleaned):
        return True
    if re.search(r"<redacted>[ \t]+\S", cleaned):
        return True
    return False


def sanitize_rework_findings(
    findings: str, *, max_chars: int = DEFAULT_MAX_REWORK_FINDINGS_CHARS
) -> str:
    """Bound findings for Work Packet mutation; never executed as code/shell.

    Neutralize ATX headings and fence openers so interpolated findings cannot
    create or steal packet-level ``##`` sections during later replacement.
    Reject credential-like text and redact absolute local paths so secrets and
    host paths never land in GitHub Issues.
    """
    cleaned = "".join(
        ch for ch in (findings or "").replace("\r\n", "\n").replace("\r", "\n")
        if ch == "\n" or (ord(ch) >= 32 or ch == "\t")
    ).strip()
    neutralized: list[str] = []
    for line in cleaned.split("\n"):
        if re.match(r"^#{1,6}(\s|$)", line):
            # Prefix so the line is no longer a Markdown ATX heading.
            line = "› " + line
        if line.lstrip().startswith("```"):
            line = line.replace("```", "'''")
        neutralized.append(line)
    cleaned = redact_absolute_paths("\n".join(neutralized).strip())
    if _contains_unsafe_secret(cleaned):
        raise ValidationError(
            "refusing to persist findings that look like secrets"
        )
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars] + "\n...[truncated]...\n"


def _flatten_paginated_issue_pages(raw: object) -> list[dict]:
    """Flatten ``gh api --paginate --slurp`` issue pages.

    Fail closed unless every page is a JSON array of objects, so a partial
    window cannot be treated as the full open-issue set.
    """
    if not isinstance(raw, list):
        raise ValidationError("gh api issues --slurp returned non-array")
    flat: list[dict] = []
    for page_idx, page in enumerate(raw):
        if not isinstance(page, list):
            raise ValidationError(
                f"gh api issues --slurp page {page_idx} is not a JSON array"
            )
        for item in page:
            if not isinstance(item, dict):
                raise ValidationError(
                    f"gh api issues page {page_idx} returned non-object entry"
                )
            flat.append(item)
    return flat


_TRUSTED_WORK_PACKET_AUTHOR_PERMISSIONS = frozenset({"write", "maintain", "admin"})
_WEAKER_WORK_PACKET_AUTHOR_PERMISSIONS = frozenset({"read", "triage", "none"})


def _github_login_from_issue(payload: dict) -> str:
    """Return the issue author login, or fail closed when it is unusable.

    REST issue lists use ``user.login``. ``gh issue view`` uses ``author.login``.
    Either shape is accepted; disagreeing identities fail closed.
    ``author_association`` is intentionally ignored. Authorization uses only
    the authenticated collaborators permission API.
    """
    found: list[str] = []
    for key in ("user", "author"):
        node = payload.get(key)
        login = ""
        if isinstance(node, dict):
            login = str(node.get("login") or "").strip()
        elif key == "author" and isinstance(node, str):
            login = node.strip()
        if login and login not in found:
            found.append(login)
    if len(found) != 1:
        raise ValidationError("WORK_PACKET_AUTHOR_UNTRUSTED: issue author missing")
    login = found[0]
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
        raise ValidationError("WORK_PACKET_AUTHOR_UNTRUSTED: issue author missing")
    return login


def _classify_collaborator_permission(parsed: object) -> str:
    """Return ``trusted`` or ``untrusted``; unknown results fail closed.

    Known weaker permissions (``read``, ``triage``, ``none``) are untrusted
    and must not create uniqueness ambiguity. Any other value, including a
    missing field, is unverifiable and must not be discarded.
    """
    if not isinstance(parsed, dict):
        raise ValidationError(
            "WORK_PACKET_AUTHOR_UNTRUSTED: permission lookup returned non-object"
        )
    raw = parsed.get("permission")
    permission = str(raw or "").strip().lower()
    if raw is None or not permission:
        raise ValidationError(
            "WORK_PACKET_AUTHOR_UNTRUSTED: permission missing"
        )
    if permission in _TRUSTED_WORK_PACKET_AUTHOR_PERMISSIONS:
        return "trusted"
    if permission in _WEAKER_WORK_PACKET_AUTHOR_PERMISSIONS:
        return "untrusted"
    raise ValidationError(
        "WORK_PACKET_AUTHOR_UNTRUSTED: "
        f"permission {permission!r} is unknown"
    )


def _leading_packet_metadata_text(body: str) -> str:
    """Return only the leading KEY=VALUE metadata block (before blank/##)."""
    lines: list[str] = []
    for line in (body or "").splitlines():
        if not line.strip() or line.startswith("## "):
            break
        lines.append(line)
    return "\n".join(lines)


def _parse_leading_packet_metadata(body: str) -> dict[str, str]:
    """Parse unique KEY=VALUE fields from the leading metadata block only."""
    values: dict[str, str] = {}
    for line in _leading_packet_metadata_text(body).splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        if key in values:
            raise ValidationError(f"duplicate work packet metadata field: {key}")
        values[key] = value.strip()
    return values


def _packet_metadata_value(body: str, key: str) -> str | None:
    return _parse_leading_packet_metadata(body).get(key)


def _set_packet_metadata_line(body: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    leading = _leading_packet_metadata_text(body)
    remainder = (body or "")[len(leading) :]
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if leading and pattern.search(leading):
        new_leading = pattern.sub(line, leading, count=1)
        return new_leading + remainder
    if leading:
        return leading.rstrip("\n") + "\n" + line + remainder
    first_break = (body or "").find("\n\n")
    if first_break == -1:
        return (body or "").rstrip() + "\n" + line + "\n"
    return (body or "")[:first_break] + "\n" + line + (body or "")[first_break:]


def _require_unique_managed_sections(body: str) -> None:
    """Fail closed when a managed packet heading appears more than once."""
    text = body or ""
    for heading in _WORK_PACKET_SECTION_HEADINGS:
        pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
        if len(pattern.findall(text)) > 1:
            raise ValidationError(f"duplicate work packet section: {heading}")


def _require_v2_packet_metadata(body: str) -> None:
    """Enforce version-specific required fields before mutation/dispatch."""
    version_raw = _packet_metadata_value(body, "PACKET_VERSION")
    if version_raw is None or not str(version_raw).strip():
        return
    try:
        version = int(str(version_raw).strip())
    except ValueError as exc:
        raise ValidationError(
            f"invalid PACKET_VERSION metadata: {version_raw!r}"
        ) from exc
    if version < 2:
        return
    for key in ("TASK_KIND", "OWNER_INTENT"):
        value = _packet_metadata_value(body, key)
        if value is None or not value.strip():
            raise ValidationError(
                f"work packet missing {key} metadata required for "
                f"PACKET_VERSION>={version}"
            )


def _replace_packet_section(body: str, heading: str, content: str) -> str:
    if heading not in _WORK_PACKET_SECTION_HEADINGS:
        raise ValidationError(f"unsupported work packet section: {heading}")
    replacement = f"## {heading}\n\n{content.rstrip()}\n\n"
    pattern = re.compile(
        rf"(^## {re.escape(heading)}\s*\n)(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    matches = list(pattern.finditer(body))
    if len(matches) > 1:
        raise ValidationError(f"duplicate work packet section: {heading}")
    if matches:
        # Callable replacement keeps content literal: backslash sequences such as
        # \d+ or \1 must not be parsed as re.sub templates/backreferences.
        return pattern.sub(lambda _match: replacement, body, count=1)
    return body.rstrip() + "\n\n" + replacement


def render_rework_work_packet_body(
    body: str,
    *,
    repository: str,
    branch: str,
    workstream: str,
    findings: str,
    attempt: int,
    head: str,
) -> str:
    """Rewrite canonical packet sections for a REWORK handoff.

    Preserves Goal/Constraints/Canonical References unless missing section
    headings force append-only updates. Findings are sanitized text only.
    """
    raw = (body or "").strip()
    if not raw:
        raise ValidationError("work packet body is empty")
    _require_unique_managed_sections(raw)
    _require_v2_packet_metadata(raw)
    status = _packet_metadata_value(raw, "STATUS")
    if status != "ACTIVE":
        raise ValidationError(
            "work packet STATUS must be ACTIVE for REWORK mutation"
        )
    require_canonical_target_repo(
        _packet_metadata_value(raw, "TARGET_REPO") or "",
        repository,
    )
    expected_branch = branch.strip()
    if not expected_branch:
        raise ValidationError("branch is required for Work Packet mutation")
    packet_branch = _packet_metadata_value(raw, "BRANCH")
    if packet_branch is None:
        raise ValidationError("work packet missing BRANCH metadata")
    if packet_branch != expected_branch:
        raise ValidationError(
            f"work packet BRANCH mismatch: {packet_branch!r} != {expected_branch!r}"
        )
    expected_workstream = workstream.strip()
    if not expected_workstream:
        raise ValidationError("workstream is required for Work Packet mutation")
    packet_workstream = _packet_metadata_value(raw, "WORKSTREAM")
    if packet_workstream is None:
        raise ValidationError("work packet missing WORKSTREAM metadata")
    if packet_workstream != expected_workstream:
        raise ValidationError(
            f"work packet WORKSTREAM mismatch: "
            f"{packet_workstream!r} != {expected_workstream!r}"
        )
    # Sanitize before any section rewrite so findings cannot inject headings.
    safe_findings = sanitize_rework_findings(findings)
    if not safe_findings:
        safe_findings = "(no findings text provided)"
    # Quote findings inside fences so residual markdown cannot steal sections.
    quoted_findings = "```text\n" + safe_findings + "\n```"
    updated = _set_packet_metadata_line(raw, "LAST_VERIFIED_HEAD", head.strip().lower())
    updated = _replace_packet_section(
        updated,
        "Current State",
        (
            f"- Controller audit verdict: REWORK\n"
            f"- Next attempt: {attempt}\n"
            f"- Audited HEAD: `{head.strip().lower()}`\n"
            f"- Branch: `{expected_branch}`\n"
            f"- Findings:\n"
            f"{quoted_findings}\n"
            f"- Canonical Work Packet mutated before `/work-resume` dispatch."
        ),
    )
    updated = _replace_packet_section(
        updated,
        "Next Action",
        (
            f"Address the REWORK findings below on attempt {attempt} "
            f"at HEAD `{head.strip().lower()}` (branch `{expected_branch}`):\n\n"
            f"{quoted_findings}\n\n"
            "Re-run affected deterministic validation, update this same Work Packet, "
            "then continue the AWC completion loop."
        ),
    )
    updated = _replace_packet_section(
        updated,
        "Latest Evidence",
        (
            "```text\n"
            f"HEAD={head.strip().lower()}\n"
            f"BRANCH={expected_branch}\n"
            f"ATTEMPT={attempt}\n"
            "VERDICT=REWORK\n"
            f"FINDINGS=\n{safe_findings}\n"
            "WORK_PACKET_MUTATION=PENDING_DISPATCH\n"
            "```"
        ),
    )
    updated = _replace_packet_section(updated, "Blockers", "NONE")
    return updated.rstrip() + "\n"


def render_dispatch_blocked_work_packet_body(
    body: str,
    *,
    repository: str,
    branch: str,
    workstream: str,
    findings: str,
    attempt: int,
    head: str,
    reason: str,
) -> str:
    """Correct a packet after mutation when Cursor dispatch did not start."""
    raw = (body or "").strip()
    if not raw:
        raise ValidationError("work packet body is empty")
    _require_unique_managed_sections(raw)
    _require_v2_packet_metadata(raw)
    status = _packet_metadata_value(raw, "STATUS")
    if status != "ACTIVE":
        raise ValidationError(
            "work packet STATUS must be ACTIVE for compensating mutation"
        )
    require_canonical_target_repo(
        _packet_metadata_value(raw, "TARGET_REPO") or "",
        repository,
    )
    expected_branch = branch.strip()
    if not expected_branch:
        raise ValidationError("branch is required for Work Packet mutation")
    packet_branch = _packet_metadata_value(raw, "BRANCH")
    if packet_branch is None:
        raise ValidationError("work packet missing BRANCH metadata")
    if packet_branch != expected_branch:
        raise ValidationError(
            f"work packet BRANCH mismatch: {packet_branch!r} != {expected_branch!r}"
        )
    expected_workstream = workstream.strip()
    if not expected_workstream:
        raise ValidationError("workstream is required for Work Packet mutation")
    packet_workstream = _packet_metadata_value(raw, "WORKSTREAM")
    if packet_workstream is None:
        raise ValidationError("work packet missing WORKSTREAM metadata")
    if packet_workstream != expected_workstream:
        raise ValidationError(
            f"work packet WORKSTREAM mismatch: "
            f"{packet_workstream!r} != {expected_workstream!r}"
        )
    safe_findings = sanitize_rework_findings(findings) or "(no findings text provided)"
    safe_reason = sanitize_rework_findings(reason, max_chars=1000) or "dispatch blocked"
    quoted = "```text\n" + safe_findings + "\n```"
    updated = _set_packet_metadata_line(raw, "LAST_VERIFIED_HEAD", head.strip().lower())
    updated = _replace_packet_section(
        updated,
        "Current State",
        (
            f"- Controller outcome: HUMAN_REQUIRED (dispatch blocked after packet mutation)\n"
            f"- Attempt: {attempt}\n"
            f"- Audited HEAD: `{head.strip().lower()}`\n"
            f"- Branch: `{expected_branch}`\n"
            f"- Reason: {safe_reason}\n"
            f"- Findings:\n{quoted}"
        ),
    )
    updated = _replace_packet_section(
        updated,
        "Next Action",
        (
            "Dispatch did **not** start. Resolve the dispatch boundary failure, "
            "then either re-run the completion hook with a real `--spawn-dispatch` "
            "path or continue manually with `/work-resume` after correcting the tree.\n\n"
            f"Boundary reason:\n```text\n{safe_reason}\n```\n\n"
            f"Prior REWORK findings:\n{quoted}"
        ),
    )
    updated = _replace_packet_section(
        updated,
        "Latest Evidence",
        (
            "```text\n"
            f"HEAD={head.strip().lower()}\n"
            f"BRANCH={expected_branch}\n"
            f"ATTEMPT={attempt}\n"
            "VERDICT=HUMAN_REQUIRED\n"
            "WORK_PACKET_MUTATION=DISPATCH_BLOCKED\n"
            f"REASON={safe_reason}\n"
            f"FINDINGS=\n{safe_findings}\n"
            "```"
        ),
    )
    updated = _replace_packet_section(
        updated,
        "Blockers",
        f"Dispatch blocked before Cursor spawn: {safe_reason}",
    )
    return updated.rstrip() + "\n"


class RecordingWorkPacketAdapter:
    """Test/offline Work Packet adapter. Do not use on the production path."""

    def __init__(self) -> None:
        self.updates: list[dict] = []

    def apply_rework_findings(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str,
        workstream: str,
        findings: str,
        attempt: int,
        head: str,
    ) -> None:
        self.updates.append(
            {
                "kind": "rework",
                "repository": repository,
                "issue_number": issue_number,
                "branch": branch,
                "workstream": workstream,
                "findings": findings,
                "attempt": attempt,
                "head": head,
            }
        )

    def apply_dispatch_blocked(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str,
        workstream: str,
        findings: str,
        attempt: int,
        head: str,
        reason: str,
    ) -> None:
        self.updates.append(
            {
                "kind": "dispatch_blocked",
                "repository": repository,
                "issue_number": issue_number,
                "branch": branch,
                "workstream": workstream,
                "findings": findings,
                "attempt": attempt,
                "head": head,
                "reason": reason,
            }
        )


class GitHubWorkPacketAdapter:
    """Mutate the same GitHub AI Work Packet via safe ``gh`` argv (no shell)."""

    def __init__(
        self,
        *,
        command_runner: Callable[
            [list[str], str], subprocess.CompletedProcess[str]
        ]
        | None = None,
        cwd: str | None = None,
        timeout_sec: int = 60,
        max_findings_chars: int = DEFAULT_MAX_REWORK_FINDINGS_CHARS,
    ) -> None:
        self._command_runner = command_runner
        self._cwd = cwd or str(Path.cwd())
        self._timeout_sec = timeout_sec
        self._max_findings_chars = max_findings_chars

    def apply_rework_findings(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str,
        workstream: str,
        findings: str,
        attempt: int,
        head: str,
    ) -> None:
        repo = normalize_github_repository(repository)
        if int(issue_number) < 1:
            raise ValidationError(f"invalid issue_number: {issue_number}")
        expected_branch = branch.strip()
        if not expected_branch:
            raise ValidationError("branch is required for Work Packet mutation")
        expected_workstream = workstream.strip()
        if not expected_workstream:
            raise ValidationError("workstream is required for Work Packet mutation")

        self._require_unique_active_packet(
            repo,
            issue_number=int(issue_number),
            branch=expected_branch,
        )
        payload = self._view_issue(repo, int(issue_number))
        original_body = str(payload.get("body") or "")
        original_updated_at = str(
            payload.get("updatedAt") or payload.get("updated_at") or ""
        )
        self._assert_ai_work_issue(payload, issue_number=int(issue_number))
        self._require_trusted_issue_author(repo, payload)
        new_body = render_rework_work_packet_body(
            original_body,
            repository=repo,
            branch=expected_branch,
            workstream=expected_workstream,
            findings=sanitize_rework_findings(
                findings, max_chars=self._max_findings_chars
            ),
            attempt=int(attempt),
            head=head,
        )

        # Best-effort conflict check. GitHub Issues PATCH rejects conditional
        # headers (If-Match / If-Unmodified-Since → HTTP 400), so a residual
        # TOCTOU remains between this recheck and the unconditional edit.
        recheck = self._view_issue(repo, int(issue_number))
        recheck_body = str(recheck.get("body") or "")
        recheck_updated_at = str(
            recheck.get("updatedAt") or recheck.get("updated_at") or ""
        )
        if recheck_body != original_body or (
            original_updated_at
            and recheck_updated_at
            and recheck_updated_at != original_updated_at
        ):
            raise ValidationError(
                "work packet changed during mutation; refusing overwrite"
            )

        # Write body via file path argv only — never shell-interpolate findings.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".md",
            delete=False,
        ) as handle:
            handle.write(new_body)
            body_path = handle.name
        try:
            try:
                edit = self._run(
                    [
                        "gh",
                        "issue",
                        "edit",
                        str(int(issue_number)),
                        "--repo",
                        repo,
                        "--body-file",
                        body_path,
                    ]
                )
            except ValidationError as exc:
                # Timeout after GitHub may have accepted the body: reconcile.
                if "timed out" in str(exc).lower():
                    landed = self._view_issue(repo, int(issue_number))
                    if str(landed.get("body") or "") == new_body:
                        return
                raise
        finally:
            Path(body_path).unlink(missing_ok=True)
        if edit.returncode != 0:
            detail = (edit.stderr or edit.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or f"gh issue edit failed with exit {edit.returncode}"
            )

    def apply_dispatch_blocked(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str,
        workstream: str,
        findings: str,
        attempt: int,
        head: str,
        reason: str,
    ) -> None:
        repo = normalize_github_repository(repository)
        if int(issue_number) < 1:
            raise ValidationError(f"invalid issue_number: {issue_number}")
        expected_branch = branch.strip()
        if not expected_branch:
            raise ValidationError("branch is required for Work Packet mutation")
        expected_workstream = workstream.strip()
        if not expected_workstream:
            raise ValidationError("workstream is required for Work Packet mutation")
        self._require_unique_active_packet(
            repo,
            issue_number=int(issue_number),
            branch=expected_branch,
        )
        payload = self._view_issue(repo, int(issue_number))
        original_body = str(payload.get("body") or "")
        original_updated_at = str(
            payload.get("updatedAt") or payload.get("updated_at") or ""
        )
        self._assert_ai_work_issue(payload, issue_number=int(issue_number))
        self._require_trusted_issue_author(repo, payload)
        new_body = render_dispatch_blocked_work_packet_body(
            original_body,
            repository=repo,
            branch=expected_branch,
            workstream=expected_workstream,
            findings=sanitize_rework_findings(
                findings, max_chars=self._max_findings_chars
            ),
            attempt=int(attempt),
            head=head,
            reason=reason,
        )
        recheck = self._view_issue(repo, int(issue_number))
        recheck_body = str(recheck.get("body") or "")
        recheck_updated_at = str(
            recheck.get("updatedAt") or recheck.get("updated_at") or ""
        )
        if recheck_body != original_body or (
            original_updated_at
            and recheck_updated_at
            and recheck_updated_at != original_updated_at
        ):
            raise ValidationError(
                "work packet changed during compensating mutation; refusing overwrite"
            )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".md",
            delete=False,
        ) as handle:
            handle.write(new_body)
            body_path = handle.name
        try:
            try:
                edit = self._run(
                    [
                        "gh",
                        "issue",
                        "edit",
                        str(int(issue_number)),
                        "--repo",
                        repo,
                        "--body-file",
                        body_path,
                    ]
                )
            except ValidationError as exc:
                if "timed out" in str(exc).lower():
                    landed = self._view_issue(repo, int(issue_number))
                    if str(landed.get("body") or "") == new_body:
                        return
                raise
        finally:
            Path(body_path).unlink(missing_ok=True)
        if edit.returncode != 0:
            detail = (edit.stderr or edit.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or f"gh issue edit failed with exit {edit.returncode}"
            )

    def _lookup_author_trust(self, repository: str, payload: dict) -> str:
        """Return ``trusted`` or ``untrusted`` for one issue author.

        Lookup failure, malformed JSON, a missing author, or an unknown
        permission raises. ``author_association`` is not read.
        """
        login = _github_login_from_issue(payload)
        path = f"repos/{repository}/collaborators/{login}/permission"
        result = self._run(["gh", "api", path])
        if result.returncode != 0:
            raise ValidationError(
                "WORK_PACKET_AUTHOR_UNTRUSTED: permission lookup failed"
            )
        try:
            parsed = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "WORK_PACKET_AUTHOR_UNTRUSTED: permission lookup returned non-JSON"
            ) from exc
        return _classify_collaborator_permission(parsed)

    def _require_trusted_issue_author(self, repository: str, payload: dict) -> None:
        """Reject mutation unless the author has write, maintain, or admin.

        Defense in depth after candidate selection. A permission downgrade
        between selection and mutation still blocks the edit.
        """
        if self._lookup_author_trust(repository, payload) != "trusted":
            raise ValidationError(
                "WORK_PACKET_AUTHOR_UNTRUSTED: "
                "permission is not write, maintain, or admin"
            )

    def _view_issue(self, repository: str, issue_number: int) -> dict:
        view = self._run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repository,
                "--json",
                "number,title,state,body,updatedAt,author",
            ]
        )
        if view.returncode != 0:
            detail = (view.stderr or view.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or f"gh issue view failed with exit {view.returncode}"
            )
        try:
            payload = json.loads(view.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("gh issue view returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("gh issue view returned non-object JSON")
        return payload

    def _list_open_ai_work_issues(self, repository: str) -> list[dict]:
        """List every open ``[AI Work]`` issue, following GitHub pagination.

        ``gh issue list --limit`` cannot prove repository-wide uniqueness: the
        CLI cap hides older matches. ``gh api --paginate`` follows Link headers
        until the set is complete; a truncated or malformed page fails closed.
        The issues API also returns pull requests, which are excluded.
        """
        path = f"repos/{repository}/issues?state=open&per_page=100"
        listed = self._run(["gh", "api", "--paginate", "--slurp", path])
        if listed.returncode != 0:
            detail = (listed.stderr or listed.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or f"gh api issues failed with exit {listed.returncode}"
            )
        try:
            payload = json.loads(listed.stdout or "")
        except json.JSONDecodeError as exc:
            raise ValidationError("gh api issues returned non-JSON") from exc
        issues: list[dict] = []
        for item in _flatten_paginated_issue_pages(payload):
            if "pull_request" in item:
                continue
            title = str(item.get("title") or "")
            if title.startswith("[AI Work]"):
                issues.append(item)
        return issues

    @staticmethod
    def _active_packet_matches(
        body: str,
        *,
        repository: str,
        branch: str,
    ) -> bool:
        """Match /work-resume selection: TARGET_REPO + STATUS=ACTIVE + BRANCH."""
        try:
            meta = _parse_leading_packet_metadata(body)
        except ValidationError:
            return False
        if meta.get("STATUS") != "ACTIVE":
            return False
        target = meta.get("TARGET_REPO")
        if not target:
            return False
        try:
            require_canonical_target_repo(target, repository)
        except ValidationError:
            return False
        packet_branch = meta.get("BRANCH")
        # Missing BRANCH matches any current branch (/work-resume.md:31).
        if packet_branch not in (None, "") and packet_branch != branch:
            return False
        return True

    def _require_unique_active_packet(
        self,
        repository: str,
        *,
        issue_number: int,
        branch: str,
    ) -> None:
        """Fail closed unless exactly one trusted ACTIVE packet matches.

        Metadata matches ``.cursor/commands/work-resume.md`` (TARGET_REPO,
        STATUS=ACTIVE, BRANCH). Only authors with effective ``write``,
        ``maintain``, or ``admin`` count toward uniqueness. A known weaker
        permission cannot create ambiguity. An unverifiable author fails
        closed instead of being ignored. WORKSTREAM is validated separately
        on the configured issue before mutation.
        """
        trusted: list[int] = []
        saw_metadata_match = False
        for issue in self._list_open_ai_work_issues(repository):
            number = issue.get("number")
            try:
                number_i = int(number)
            except (TypeError, ValueError):
                number_i = None
            body = str(issue.get("body") or "")
            if not self._active_packet_matches(
                body,
                repository=repository,
                branch=branch,
            ):
                continue
            if number_i is None:
                raise ValidationError(
                    "WORK_PACKET_AUTHOR_UNTRUSTED: candidate issue number missing"
                )
            saw_metadata_match = True
            try:
                trust = self._lookup_author_trust(repository, issue)
            except ValidationError as exc:
                raise ValidationError(
                    "WORK_PACKET_AUTHOR_UNTRUSTED: "
                    f"candidate #{number_i} unverifiable ({exc})"
                ) from exc
            if trust == "trusted":
                trusted.append(number_i)
        if not saw_metadata_match:
            raise ValidationError(
                "no ACTIVE Work Packet matches repository/branch"
            )
        if not trusted:
            raise ValidationError(
                "no trusted ACTIVE Work Packet matches repository/branch"
            )
        if len(trusted) > 1:
            listed = ", ".join(f"#{n}" for n in sorted(trusted))
            raise ValidationError(
                f"ambiguous ACTIVE Work Packets for repository/branch: {listed}"
            )
        if trusted[0] != int(issue_number):
            raise ValidationError(
                f"configured issue #{issue_number} is not the unique ACTIVE "
                f"Work Packet match (found #{trusted[0]})"
            )

    @staticmethod
    def _assert_ai_work_issue(payload: dict, *, issue_number: int) -> None:
        title = str(payload.get("title") or "")
        if not title.startswith("[AI Work]"):
            raise ValidationError(
                f"issue #{issue_number} is not an [AI Work] packet: {title!r}"
            )
        # GitHub REST returns lowercase "open"; gh issue view may return "OPEN".
        if str(payload.get("state") or "").lower() != "open":
            raise ValidationError(
                f"work packet issue #{issue_number} is not OPEN"
            )

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if self._command_runner is not None:
            return self._command_runner(argv, self._cwd)
        try:
            return subprocess.run(
                argv,
                cwd=self._cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
            )
        except FileNotFoundError as exc:
            missing = argv[0] if argv else "command"
            return subprocess.CompletedProcess(
                argv,
                127,
                stdout="",
                stderr=f"{missing} not found on PATH: {exc}",
            )
        except OSError as exc:
            # PermissionError and other pre-exec OS failures must stay at the
            # ValidationError boundary so controller state can finalize safely.
            raise ValidationError(
                f"{argv[0] if argv else 'command'} failed to start: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ValidationError(
                f"{argv[0] if argv else 'command'} timed out after "
                f"{self._timeout_sec}s"
            ) from exc


class RecordingCursorDispatcher:
    def __init__(self, session_prefix: str = "session") -> None:
        self.requests: list[DispatchRequest] = []
        self._n = 0
        self.session_prefix = session_prefix

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self._n += 1
        self.requests.append(request)
        command = build_persist_resume_command(request)
        return DispatchResult(
            session_id=f"{self.session_prefix}-{self._n}",
            command=command,
        )


class AuditOnlyCursorDispatcher:
    """Refuse to claim Cursor dispatch when ``--spawn-dispatch`` is absent.

    Production audit-only CLI wiring uses this instead of
    ``RecordingCursorDispatcher`` so REWORK cannot finalize as
    ``REWORK_DISPATCHED`` without a real GitHub mutation + PTY spawn.
    Offline unit tests may still inject ``RecordingCursorDispatcher``.
    """

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        raise ValidationError(
            "audit-only mode cannot claim Cursor dispatch; "
            "pass --spawn-dispatch with --work-packet-adapter github for REWORK"
        )


class DispatchSpawnedButUnobservedError(ValidationError):
    """Raised when a spawn started but no Cursor session/process was confirmed.

    Distinct from pre-spawn boundary ValidationError for diagnostics. The
    controller treats this as ``HUMAN_REQUIRED`` with Work Packet compensation
    only after the owned spawn group was confirmed gone.
    """

    def __init__(
        self,
        message: str,
        *,
        session_hint: str,
        command: list[str],
    ) -> None:
        super().__init__(message)
        self.session_hint = session_hint
        self.command = list(command)


class DispatchSpawnCleanupUncertainError(ValidationError):
    """Owned spawn cleanup failed or the process group was not confirmed gone.

    Not a subclass of the unobserved error: the controller must not compensate
    the canonical packet as if no late session can appear.
    """

    def __init__(
        self,
        message: str,
        *,
        session_hint: str,
        command: list[str],
        cleanup_error: str,
    ) -> None:
        super().__init__(message)
        self.session_hint = session_hint
        self.command = list(command)
        self.cleanup_error = cleanup_error


class SpawnCleanupUncertainError(OSError):
    """Signal or exit check could not prove the owned process group is gone."""


@dataclass(frozen=True)
class PersistSession:
    session_id: str
    workspace: str
    status: str = ""
    task: str = ""


def build_persist_resume_command(request: DispatchRequest) -> list[str]:
    """Fixed argv for a fresh unattended /work-resume in a validated worktree.

    Installed Cursor CLI ``--force`` is Run Everything and follows ``persist``.
    Host-proven argv is ``agent persist --force --trust /work-resume``.
    Placing ``--force`` before ``persist`` is an unknown command. ``--trust``
    still trusts the workspace, and the prompt stays ``/work-resume``.
    Non-interactive ``agent -p`` / ``--print`` is not an acceptable persistence
    substitute.
    """
    prompt = request.resume_prompt or RESUME_PROMPT
    if prompt != RESUME_PROMPT:
        raise ValidationError(
            f"resume_prompt must be {RESUME_PROMPT!r}, got {prompt!r}"
        )
    command = ["agent", "persist", "--force", "--trust", RESUME_PROMPT]
    if "--print" in command or "-p" in command:
        raise ValidationError("non-persistent print mode is prohibited")
    return command


class ResourcePreflightBlocked(ValidationError):
    """New persistent session refused by the resource preflight.

    Existing Cursor sessions must not be stopped or otherwise mutated.
    """

    def __init__(self, message: str, *, result: str, reason: str, exit_code: int) -> None:
        super().__init__(message)
        self.preflight_result = result
        self.preflight_reason = reason
        self.exit_code = exit_code


def _explicit_preflight_script(env_name: str) -> Path | None:
    """Return a configured script path, or None when that path is not a file.

    An explicit variable that is set but missing fails closed. Callers must
    not fall through to another location after this returns None for a
    non-empty value; the caller distinguishes unset from missing.
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return None
    path = Path(raw.strip())
    return path if path.is_file() else None


def resolve_cursor_resource_preflight_script() -> Path | None:
    """Locate the Engineering System preflight without a hardcoded checkout.

    Precedence:
    1. ``ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD`` — canonical explicit script
       path named by ``/work-resume``.
    2. ``ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT`` — compatibility alias,
       used only when the canonical variable is unset.
    3. ``ENGINEERING_SYSTEM_ROOT``/``tools/cursor-resource-preflight.py`` when
       neither explicit path is set.

    A set explicit path that is not an existing file is unavailable. A missing
    or unset location must fail closed.
    """
    if os.environ.get("ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD", "").strip():
        return _explicit_preflight_script("ENGINEERING_SYSTEM_CURSOR_RESOURCE_GUARD")
    if os.environ.get("ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT", "").strip():
        return _explicit_preflight_script(
            "ENGINEERING_SYSTEM_CURSOR_RESOURCE_PREFLIGHT"
        )
    root = os.environ.get("ENGINEERING_SYSTEM_ROOT", "").strip()
    if not root:
        return None
    path = Path(root) / "tools" / "cursor-resource-preflight.py"
    return path if path.is_file() else None


def _parse_preflight_report(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (output or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def interpret_resource_preflight(exit_code: int, output: str) -> tuple[bool, str, str]:
    """Return ``(may_spawn, RESULT, REASON)``.

    Exit 0 with ``PASS`` or ``WARN`` may spawn. Any other exit, missing report,
    or non-allow result is ``BLOCK``.
    """
    fields = _parse_preflight_report(output)
    result = fields.get("RESULT", "").upper()
    reason = fields.get("REASON", "").strip()
    if exit_code == 0 and result in {"PASS", "WARN"}:
        return True, result, reason or "resource preflight allowed spawn"
    if not reason:
        reason = f"resource preflight unavailable or unreadable (exit {exit_code})"
    return False, "BLOCK", reason


def default_cursor_resource_preflight() -> tuple[int, str]:
    """Run the canonical preflight. Unavailable tools are BLOCK, not a guess."""
    script = resolve_cursor_resource_preflight_script()
    if script is None:
        return (
            3,
            "RESULT=BLOCK\nEXIT_CODE=3\nREASON=cursor resource preflight unavailable\n",
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = redact_absolute_paths(str(exc))[:300]
        return (
            3,
            "RESULT=BLOCK\nEXIT_CODE=3\n"
            f"REASON=cursor resource preflight failed to start: {detail}\n",
        )
    output = completed.stdout or ""
    if not output.strip() and completed.stderr:
        output = completed.stderr
    return int(completed.returncode), output


def _bounded_preflight_reason(reason: str) -> str:
    return redact_absolute_paths(reason or "")[:300]


def parse_persist_list(output: str) -> list[PersistSession]:
    """Parse `agent persist list` text into session records."""
    sessions: list[PersistSession] = []
    task = ""
    status = ""
    session_id = ""
    workspace = ""

    def flush() -> None:
        nonlocal task, status, session_id, workspace
        if session_id and workspace:
            sessions.append(
                PersistSession(
                    session_id=session_id,
                    workspace=workspace,
                    status=status,
                    task=task,
                )
            )
        task = ""
        status = ""
        session_id = ""
        workspace = ""

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if line.startswith("Task:"):
            flush()
            task = line.split(":", 1)[1].strip()
        elif line.strip().startswith("Status:"):
            status = line.split(":", 1)[1].strip()
        elif line.strip().startswith("Session:"):
            session_id = line.split(":", 1)[1].strip()
        elif line.strip().startswith("Workspace:"):
            workspace = line.split(":", 1)[1].strip()
    flush()
    return sessions


def sessions_for_worktree(
    sessions: list[PersistSession], worktree_path: str
) -> list[PersistSession]:
    target = str(Path(worktree_path).resolve())
    matched: list[PersistSession] = []
    for item in sessions:
        try:
            if str(Path(item.workspace).resolve()) == target:
                matched.append(item)
        except OSError:
            if item.workspace == target or item.workspace == worktree_path:
                matched.append(item)
    return matched


class SubprocessCursorDispatcher:
    """Spawn the fixed persist argv via an injected runner (no shell)."""

    def __init__(
        self,
        runner: Callable[[list[str], str], str] | None = None,
    ) -> None:
        self._runner = runner or _default_spawn_runner
        self.requests: list[DispatchRequest] = []

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self.requests.append(request)
        command = build_persist_resume_command(request)
        session_id = self._runner(command, request.worktree_path)
        if not session_id or not str(session_id).strip():
            raise ValidationError("cursor dispatcher returned empty session id")
        return DispatchResult(session_id=str(session_id).strip(), command=command)


class PtyPersistCursorDispatcher:
    """Create a fresh observable `agent persist` session that runs /work-resume.

    Transport only: PTY/`script` spawn. Durable success requires a new
    ``agent persist list`` session for the target worktree that is named by
    the owned spawn's process tree. Another new session in the same worktree
    is not this launch. A target process is diagnostic only and never a
    successful dispatch. Does not attach to, stop, or otherwise mutate
    sessions outside the owned spawn group.

    Immediately before spawn, revalidates exact repository/branch/HEAD and
    requires clean porcelain so a stale or dirty tree cannot launch Cursor.
    """

    def __init__(
        self,
        *,
        list_sessions: Callable[[], list[PersistSession]] | None = None,
        spawn: Callable[[list[str], str], int] | None = None,
        list_target_procs: Callable[[str], list[tuple[int, str]]] | None = None,
        git_runner: GitRunner | None = None,
        resource_preflight: Callable[[], tuple[int, str]] | None = None,
        terminate_process_group: Callable[[int], None] | None = None,
        owned_session_ids: Callable[[int, set[str]], set[str]] | None = None,
        poll_interval_sec: float = 0.5,
        poll_timeout_sec: float = 45.0,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._list_sessions = list_sessions or default_list_persist_sessions
        self._spawn = spawn or script_pty_spawn_persist
        self._list_target_procs = list_target_procs or list_persist_trust_processes
        self._git_runner = git_runner
        self._resource_preflight = resource_preflight or default_cursor_resource_preflight
        self.last_resource_preflight: dict[str, str] = {}
        self.last_process_observation = ""
        self._terminate_process_group = (
            terminate_process_group or terminate_spawned_process_group
        )
        self._owned_session_ids = (
            owned_session_ids or persist_session_ids_in_spawn_tree
        )
        self._poll_interval_sec = poll_interval_sec
        self._poll_timeout_sec = poll_timeout_sec
        self._sleep = sleeper or time.sleep
        self.requests: list[DispatchRequest] = []
        self.spawned_pids: list[int] = []

    def _fail_unobserved(
        self,
        pid: int,
        *,
        message: str,
        command: list[str],
        cause: BaseException | None = None,
    ) -> None:
        """Stop the owned spawn group, then raise unobserved or uncertain.

        Compensation is allowed only when termination returns, which means the
        owned group was confirmed gone. A signal or verification failure stays
        uncertain so a live process cannot be treated as a finished dispatch.
        """
        try:
            self._terminate_process_group(pid)
        except Exception as exc:
            detail = redact_absolute_paths(str(exc))[:300]
            raise DispatchSpawnCleanupUncertainError(
                f"{message}; owned spawn cleanup uncertain: {detail}",
                session_hint=f"proc:{pid}",
                command=command,
                cleanup_error=detail,
            ) from exc
        if cause is None:
            raise DispatchSpawnedButUnobservedError(
                message,
                session_hint=f"proc:{pid}",
                command=command,
            )
        raise DispatchSpawnedButUnobservedError(
            message,
            session_hint=f"proc:{pid}",
            command=command,
        ) from cause

    def start_resume(self, request: DispatchRequest) -> DispatchResult:
        self.requests.append(request)
        worktree = str(Path(request.worktree_path).resolve())
        if not Path(worktree).is_dir():
            raise ValidationError(f"worktree_path is not a directory: {worktree}")
        if not str(request.repository or "").strip():
            raise ValidationError("dispatch request missing repository")
        if not str(request.expected_head or "").strip():
            raise ValidationError("dispatch request missing expected_head")
        command = build_persist_resume_command(request)
        try:
            before_ids = {
                item.session_id
                for item in sessions_for_worktree(self._list_sessions(), worktree)
            }
            before_pids = {pid for pid, _cmd in self._list_target_procs(worktree)}
            try:
                exit_code, output = self._resource_preflight()
            except Exception as exc:
                exit_code, output = (
                    3,
                    "RESULT=BLOCK\nEXIT_CODE=3\nREASON="
                    + _bounded_preflight_reason(
                        f"cursor resource preflight failed to start: {exc}"
                    )
                    + "\n",
                )
            may_spawn, result, reason = interpret_resource_preflight(exit_code, output)
            reason = _bounded_preflight_reason(reason)
            self.last_resource_preflight = {
                "result": result,
                "reason": reason,
                "exit_code": str(exit_code),
            }
            if not may_spawn:
                raise ResourcePreflightBlocked(
                    f"resource preflight RESULT={result} REASON={reason}",
                    result=result,
                    reason=reason,
                    exit_code=exit_code,
                )
            # Final identity/porcelain check immediately before spawn — no external
            # observation between this validation and _spawn (TOCTOU close).
            validate_clean_worktree_identity(
                worktree,
                repository=request.repository,
                branch=request.branch,
                expected_head=request.expected_head,
                git_runner=self._git_runner,
            )
            pid = self._spawn(command, worktree)
        except DispatchSpawnedButUnobservedError:
            raise
        except ValidationError:
            raise
        except OSError as exc:
            # Pre-spawn OS failures (missing agent/script, denied exec, etc.) must
            # remain boundary ValidationErrors so the controller can compensate the
            # Work Packet away from PENDING_DISPATCH.
            raise ValidationError(f"cursor spawn failed before start: {exc}") from exc
        self.spawned_pids.append(pid)
        seen_process = ""
        unattributed: list[str] = []
        deadline = time.monotonic() + self._poll_timeout_sec
        while time.monotonic() < deadline:
            try:
                current = sessions_for_worktree(self._list_sessions(), worktree)
                new_sessions = [
                    item for item in current if item.session_id not in before_ids
                ]
                if new_sessions:
                    owned = self._owned_session_ids(
                        pid, {item.session_id for item in new_sessions}
                    )
                    attributed = [
                        item for item in new_sessions if item.session_id in owned
                    ]
                    if attributed:
                        chosen = attributed[-1]
                        return DispatchResult(
                            session_id=chosen.session_id,
                            command=command,
                            resource_preflight_result=self.last_resource_preflight.get(
                                "result", ""
                            ),
                            resource_preflight_reason=self.last_resource_preflight.get(
                                "reason", ""
                            ),
                        )
                    for item in new_sessions:
                        if item.session_id not in unattributed:
                            unattributed.append(item.session_id)
                for proc_pid, cmd in self._list_target_procs(worktree):
                    if proc_pid not in before_pids:
                        seen_process = f"proc:{proc_pid} {cmd}".strip()
                        self.last_process_observation = seen_process
            except DispatchSpawnedButUnobservedError:
                raise
            except Exception as exc:
                self._fail_unobserved(
                    pid,
                    message=(
                        f"post-spawn observation failed after pid={pid}: {exc}"
                    ),
                    command=command,
                    cause=exc,
                )
            self._sleep(self._poll_interval_sec)
        diagnostic = ""
        if seen_process:
            diagnostic = f"; process observation {seen_process} is diagnostic only"
        if unattributed:
            diagnostic += (
                "; unattributed same-worktree session(s) "
                + ",".join(unattributed)
                + " are not the owned spawn"
            )
        self._fail_unobserved(
            pid,
            message=(
                "agent persist list did not show a session owned by the spawned "
                f"process for the target worktree within {self._poll_timeout_sec}s"
                f"{diagnostic} (cwd={worktree}, argv={command!r}, pid={pid})"
            ),
            command=command,
        )
        raise AssertionError("unreachable")  # pragma: no cover


def _live_process_group_members(pgid: int) -> list[int]:
    """Live, non-zombie pids whose process group is ``pgid``.

    ``/proc`` is required to prove the group is empty. A missing procfs is
    uncertain rather than a successful cleanup.
    """
    if pgid <= 0:
        return []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise SpawnCleanupUncertainError(
            "cannot verify owned process group: proc unavailable"
        )
    members: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found_pgid: int | None = None
        for line in status.splitlines():
            if line.startswith("NSpgid:"):
                found_pgid = int(line.split()[1])
                break
        if found_pgid != pgid:
            continue
        marker = stat.rfind(")")
        if marker == -1 or marker + 2 >= len(stat):
            raise SpawnCleanupUncertainError(
                f"cannot verify owned pid {pid}: unreadable status"
            )
        if stat[marker + 2] == "Z":
            continue
        members.append(pid)
    return members


def _signal_owned_process_group(pgid: int, sig: signal.Signals) -> None:
    """Signal one owned group. An empty group is success; a denied signal is not."""
    try:
        os.killpg(pgid, sig)
        return
    except ProcessLookupError:
        return
    except OSError as exc:
        group_error: OSError = exc
    members = _live_process_group_members(pgid)
    if not members:
        return
    for member in members:
        try:
            os.kill(member, sig)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise SpawnCleanupUncertainError(
                f"cannot signal owned process group {pgid}: {exc}"
            ) from exc
    if _live_process_group_members(pgid):
        raise SpawnCleanupUncertainError(
            f"cannot signal owned process group {pgid}: {group_error}"
        ) from group_error


def terminate_spawned_process_group(pid: int, *, wait_sec: float = 2.0) -> None:
    """SIGTERM then SIGKILL an owned ``start_new_session`` process group.

    Returns only after every live member of that group is gone. A leader exit
    with a surviving child is not success. Permission or verification failure
    raises ``SpawnCleanupUncertainError``. Unrelated sessions are not signaled.
    """
    if pid <= 0:
        return
    pgid = pid
    _signal_owned_process_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, wait_sec)
    while time.monotonic() < deadline:
        if not _live_process_group_members(pgid):
            return
        time.sleep(0.05)
    _signal_owned_process_group(pgid, signal.SIGKILL)
    verify_deadline = time.monotonic() + 0.5
    while time.monotonic() < verify_deadline:
        if not _live_process_group_members(pgid):
            return
        time.sleep(0.05)
    remaining = _live_process_group_members(pgid)
    if remaining:
        raise SpawnCleanupUncertainError(
            f"owned process group {pgid} still has members after SIGKILL: {remaining}"
        )


def _cmdline_has_ordered_tokens(parts: list[str], tokens: tuple[str, ...]) -> bool:
    start = 0
    for token in tokens:
        try:
            start = parts.index(token, start) + 1
        except ValueError:
            return False
    return True


def _is_agent_persist_trust_cmdline(cmdline: str) -> bool:
    """True when cmdline is the real Run Everything ``agent`` child.

    The ``script(1)`` wrapper is not a confirmation. ``persist`` must precede
    ``--force`` so the unknown ``agent --force persist`` form is ignored.
    """
    argv0 = cmdline.split("\x00", 1)[0]
    if Path(argv0).name != "agent":
        return False
    parts = [part for part in cmdline.split("\x00") if part]
    if "--print" in parts or "-p" in parts:
        return False
    prompt = RESUME_PROMPT if RESUME_PROMPT in parts else "/work-resume"
    if prompt not in parts:
        return False
    return _cmdline_has_ordered_tokens(
        parts, ("persist", "--force", "--trust", prompt)
    )


def _descendant_pids(root_pid: int) -> set[int]:
    """Return root_pid and every process whose parent chain reaches it."""
    if root_pid <= 0:
        return set()
    parents: dict[int, int] = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return {root_pid}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ppid = None
        for line in status.splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
                break
        if ppid is not None:
            parents[int(entry.name)] = ppid
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    found = {root_pid}
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _cmdline_tokens(pid: int) -> set[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return set()
    tokens = {part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part}
    flattened: set[str] = set()
    for token in tokens:
        flattened.add(token)
        flattened.update(part for part in token.split() if part)
    return flattened


def persist_session_ids_in_spawn_tree(
    spawn_pid: int, candidate_ids: set[str]
) -> set[str]:
    """Session ids named by the owned spawn or its descendants.

    Host Cursor persist processes record the session id as their own argv
    token (``--cursor-persist-restore <id>`` or tmux ``-t <id>``). A new
    persist-list row is this launch only when that id appears there. No
    matching process evidence means the session is unattributed.
    """
    wanted = {item for item in candidate_ids if item}
    if spawn_pid <= 0 or not wanted:
        return set()
    found: set[str] = set()
    for pid in _descendant_pids(spawn_pid):
        found.update(wanted.intersection(_cmdline_tokens(pid)))
        if found == wanted:
            break
    return found


def list_persist_trust_processes(worktree_path: str) -> list[tuple[int, str]]:
    """List `agent persist --force --trust /work-resume` processes for one worktree.

    Excludes the ``script(1)`` PTY wrapper whose command line only embeds the
    agent argv as a string — confirmation requires the real ``agent`` executable.
    """
    target = str(Path(worktree_path).resolve())
    found: list[tuple[int, str]] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return found
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cwd = str((entry / "cwd").resolve())
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if cwd != target:
            continue
        if not _is_agent_persist_trust_cmdline(cmdline):
            continue
        found.append((pid, cmdline.replace("\x00", " ").strip()))
    return found


def default_list_persist_sessions() -> list[PersistSession]:
    try:
        completed = subprocess.run(
            ["agent", "persist", "list"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ValidationError(f"agent persist list failed to start: {exc}") from exc
    if completed.returncode != 0:
        raise ValidationError(
            "agent persist list failed: "
            + (completed.stderr or completed.stdout or f"exit {completed.returncode}")
        )
    return parse_persist_list(completed.stdout)


def script_pty_spawn_persist(command: list[str], worktree_path: str) -> int:
    """Spawn argv under `script(1)` PTY in the target worktree (host-proven).

    Live Cursor CLI sessions are started as:
    `script -qec 'agent persist --force --trust <prompt>' /dev/null` with
    cwd=worktree. ``--force`` is Run Everything.
    """
    if not command or command[0] != "agent":
        raise ValidationError(f"refusing to spawn non-agent command: {command!r}")
    quoted = " ".join(shlex.quote(part) for part in command)
    script_cmd = ["script", "-qec", quoted, "/dev/null"]
    try:
        proc = subprocess.Popen(
            script_cmd,
            cwd=worktree_path,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise ValidationError(
            f"script pty spawn failed to start agent persist: {exc}"
        ) from exc
    if proc.pid <= 0:
        raise ValidationError("script pty spawn failed to start agent persist")
    return int(proc.pid)


def pty_spawn_persist(command: list[str], worktree_path: str) -> int:
    """Spawn argv under a raw PTY in the target worktree; do not wait for exit."""
    if not command or command[0] != "agent":
        raise ValidationError(f"refusing to spawn non-agent command: {command!r}")
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        raise ValidationError(f"pty open failed: {exc}") from exc
    try:
        try:
            proc = subprocess.Popen(
                command,
                cwd=worktree_path,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise ValidationError(
                f"pty spawn failed to start agent persist: {exc}"
            ) from exc
    finally:
        os.close(slave_fd)
        os.close(master_fd)
    if proc.pid <= 0:
        raise ValidationError("pty spawn failed to start agent persist")
    return int(proc.pid)


def _default_spawn_runner(command: list[str], worktree_path: str) -> str:
    """Fail closed unless a real runner/dispatcher is injected."""
    raise ValidationError(
        "no cursor spawn runner configured; use PtyPersistCursorDispatcher "
        f"or inject a runner (planned argv={command!r} cwd={worktree_path!r})"
    )


class WorkControllerStore:
    """Filesystem JSON store for controller state (ADR-0006)."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.path = self.data_root / "work-controller.json"

    def _empty(self) -> dict:
        return {"schema_version": CONTROLLER_SCHEMA_VERSION, "workstreams": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        version = data.get("schema_version")
        if version != CONTROLLER_SCHEMA_VERSION:
            raise ValidationError(
                f"unsupported work-controller schema_version: {version}; "
                f"expected {CONTROLLER_SCHEMA_VERSION}"
            )
        if "workstreams" not in data or not isinstance(data["workstreams"], dict):
            raise ValidationError("work-controller.json missing workstreams map")
        return data

    def _save(self, data: dict) -> None:
        payload = {
            "schema_version": CONTROLLER_SCHEMA_VERSION,
            "workstreams": data.get("workstreams", {}),
        }
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def list_workstreams(self) -> list[WorkstreamRecord]:
        data = self._load()
        return [self._from_dict(raw) for raw in data["workstreams"].values()]

    def get(self, workstream: str) -> WorkstreamRecord:
        data = self._load()
        raw = data["workstreams"].get(workstream)
        if raw is None:
            raise ValidationError(f"unknown workstream: {workstream}")
        return self._from_dict(raw)

    def put(self, record: WorkstreamRecord) -> WorkstreamRecord:
        data = self._load()
        data["workstreams"][record.workstream] = self._to_dict(record)
        self._save(data)
        return record

    def _from_dict(self, raw: dict) -> WorkstreamRecord:
        state = raw.get("state", "IDLE")
        if state not in ALLOWED_STATES:
            raise ValidationError(f"invalid controller state: {state}")
        return WorkstreamRecord(
            workstream=raw["workstream"],
            repository=raw["repository"],
            issue_number=int(raw["issue_number"]),
            branch=raw["branch"],
            worktree_path=raw["worktree_path"],
            expected_head=str(raw["expected_head"]).lower(),
            state=state,
            attempt=int(raw.get("attempt", 0)),
            max_attempts=int(raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
            last_event_id=raw.get("last_event_id"),
            last_session_id=raw.get("last_session_id"),
            last_audit_verdict=raw.get("last_audit_verdict"),
            last_findings=raw.get("last_findings", ""),
            last_outcome=dict(raw.get("last_outcome") or {}),
            processed_event_ids=list(raw.get("processed_event_ids") or []),
            pending_event=raw.get("pending_event"),
        )

    def _to_dict(self, record: WorkstreamRecord) -> dict:
        return {
            "workstream": record.workstream,
            "repository": record.repository,
            "issue_number": record.issue_number,
            "branch": record.branch,
            "worktree_path": record.worktree_path,
            "expected_head": record.expected_head,
            "state": record.state,
            "attempt": record.attempt,
            "max_attempts": record.max_attempts,
            "last_event_id": record.last_event_id,
            "last_session_id": record.last_session_id,
            "last_audit_verdict": record.last_audit_verdict,
            "last_findings": record.last_findings,
            "last_outcome": record.last_outcome,
            "processed_event_ids": record.processed_event_ids,
            "pending_event": record.pending_event,
        }


class WorkController:
    """Bounded local dogfood control loop."""

    def __init__(
        self,
        data_root: Path,
        *,
        audit: AuditPort,
        work_packet: WorkPacketPort,
        dispatcher: CursorDispatchPort,
        observer: ObserverPort | None = None,
        enforce_worktree_identity: bool = True,
        git_runner: GitRunner | None = None,
    ) -> None:
        self.store = WorkControllerStore(data_root)
        self.audit = audit
        self.work_packet = work_packet
        self.dispatcher = dispatcher
        self.observer = observer or NullObserver()
        self.enforce_worktree_identity = enforce_worktree_identity
        self._git_runner = git_runner

    def register_workstream(
        self,
        *,
        workstream: str,
        repository: str,
        issue_number: int,
        branch: str,
        worktree_path: str,
        expected_head: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> WorkstreamRecord:
        self._validate_workstream_id(workstream)
        self._validate_head(expected_head)
        if max_attempts < 1:
            raise ValidationError("max_attempts must be >= 1")
        worktree = Path(worktree_path).resolve()
        if not worktree.is_dir():
            raise ValidationError(f"worktree_path is not a directory: {worktree}")
        repository_norm = normalize_github_repository(repository)
        if self.enforce_worktree_identity:
            identity = validate_worktree_identity(
                str(worktree),
                repository=repository_norm,
                branch=branch.strip(),
                expected_head=expected_head.strip().lower(),
                git_runner=self._git_runner,
            )
            worktree = Path(identity.worktree_path)
            expected_head = identity.head
            branch = identity.branch
            repository_norm = identity.repository
        existing = {
            item.workstream for item in self.store.list_workstreams()
        }
        if workstream in existing:
            raise ValidationError(f"workstream already registered: {workstream}")
        record = WorkstreamRecord(
            workstream=workstream,
            repository=repository_norm,
            issue_number=int(issue_number),
            branch=branch.strip(),
            worktree_path=str(worktree),
            expected_head=expected_head.strip().lower(),
            state="IDLE",
            attempt=0,
            max_attempts=int(max_attempts),
        )
        return self.store.put(record)

    def show(self, workstream: str) -> dict:
        return asdict(self.store.get(workstream))

    def list_workstreams(self) -> list[dict]:
        return [asdict(item) for item in self.store.list_workstreams()]

    def handle_completion(self, raw_event: dict) -> dict:
        event = CompletionEvent.from_dict(raw_event)
        record = self.store.get(event.workstream)

        if event.event_id in record.processed_event_ids:
            outcome = dict(record.last_outcome or {})
            outcome["idempotent_replay"] = True
            self.observer.observe("completion_idempotent", outcome)
            return outcome

        self._assert_event_identity(record, event)

        record.state = "AWAITING_AUDIT"
        record.pending_event = asdict(event)
        record.last_event_id = event.event_id
        if event.session_id:
            record.last_session_id = event.session_id
        self.store.put(record)

        return self._run_audit_and_advance(record.workstream)

    def reconcile(self, workstream: str | None = None) -> list[dict]:
        """Recover after controller restart.

        Unfinished audit states re-run the pending event once. Terminal and
        REWORK_DISPATCHED states are left unchanged.
        """
        targets = (
            [self.store.get(workstream)]
            if workstream
            else self.store.list_workstreams()
        )
        outcomes: list[dict] = []
        for record in targets:
            if record.state in {"AWAITING_AUDIT", "AUDITING"} and record.pending_event:
                outcomes.append(self._run_audit_and_advance(record.workstream))
            else:
                outcomes.append(
                    {
                        "workstream": record.workstream,
                        "state": record.state,
                        "reconciled": False,
                        "reason": "no_pending_audit",
                    }
                )
        return outcomes

    def _run_audit_and_advance(self, workstream: str) -> dict:
        record = self.store.get(workstream)
        if not record.pending_event:
            raise ValidationError(f"no pending event for workstream: {workstream}")
        event = CompletionEvent.from_dict(record.pending_event)

        record.state = "AUDITING"
        self.store.put(record)

        audit_result = self.audit.audit(event, record)
        record = self.store.get(workstream)
        record.last_audit_verdict = audit_result.verdict
        record.last_findings = audit_result.findings

        if audit_result.verdict == "PASS":
            # Common boundary for every audit adapter (codex/openai/fixed): never
            # finalize PASSED unless the audited worktree is still a clean
            # autonomous snapshot. Adapter-local checks are not sufficient.
            blocked = self._reject_if_unclean_snapshot(
                event,
                record,
                prefix=(
                    "PASS rejected: audited worktree not a clean autonomous "
                    "snapshot at controller PASS gate"
                ),
            )
            if blocked is not None:
                audit_result = blocked
                record.last_audit_verdict = audit_result.verdict
                record.last_findings = audit_result.findings
        if audit_result.verdict == "PASS":
            outcome = self._finalize(
                record,
                event,
                state="PASSED",
                action="stop",
                verdict="PASS",
            )
        elif audit_result.verdict == "HUMAN_REQUIRED":
            outcome = self._finalize(
                record,
                event,
                state="HUMAN_REQUIRED",
                action="stop",
                verdict="HUMAN_REQUIRED",
            )
        else:
            next_attempt = event.attempt + 1
            if next_attempt > record.max_attempts:
                record.last_findings = (
                    f"{audit_result.findings}\n"
                    f"retry exhausted at attempt {event.attempt}/{record.max_attempts}"
                ).strip()
                outcome = self._finalize(
                    record,
                    event,
                    state="HUMAN_REQUIRED",
                    action="stop",
                    verdict="HUMAN_REQUIRED",
                    extra={"reason": "retry_exhausted"},
                )
            else:
                # Common REWORK pre-mutation gate for every adapter: never mutate
                # the canonical Work Packet (or dispatch) on a dirty/drifted tree.
                blocked = self._reject_if_unclean_snapshot(
                    event,
                    record,
                    prefix=(
                        "REWORK rejected: audited worktree not a clean autonomous "
                        "snapshot before Work Packet mutation"
                    ),
                )
                if blocked is not None:
                    audit_result = blocked
                    record.last_audit_verdict = audit_result.verdict
                    record.last_findings = audit_result.findings
                    outcome = self._finalize(
                        record,
                        event,
                        state="HUMAN_REQUIRED",
                        action="stop",
                        verdict="HUMAN_REQUIRED",
                        extra={"reason": "rework_unclean_snapshot"},
                    )
                else:
                    try:
                        self.work_packet.apply_rework_findings(
                            repository=record.repository,
                            issue_number=record.issue_number,
                            branch=record.branch,
                            workstream=record.workstream,
                            findings=audit_result.findings,
                            attempt=next_attempt,
                            head=event.head,
                        )
                    except ValidationError as exc:
                        record.last_findings = (
                            f"{audit_result.findings}\n"
                            f"work packet mutation failed before dispatch: {exc}"
                        ).strip()
                        outcome = self._finalize(
                            record,
                            event,
                            state="HUMAN_REQUIRED",
                            action="stop",
                            verdict="HUMAN_REQUIRED",
                            extra={"reason": "work_packet_mutation_failed"},
                        )
                    else:
                        try:
                            dispatch = self.dispatcher.start_resume(
                                DispatchRequest(
                                    workstream=record.workstream,
                                    worktree_path=record.worktree_path,
                                    branch=record.branch,
                                    issue_number=record.issue_number,
                                    attempt=next_attempt,
                                    repository=record.repository,
                                    expected_head=event.head,
                                    resume_prompt=RESUME_PROMPT,
                                )
                            )
                        except DispatchSpawnCleanupUncertainError as exc:
                            # The owned process may still be alive. Do not
                            # rewrite the packet into a dispatch-blocked state.
                            boundary_reason = redact_absolute_paths(str(exc))
                            record.last_findings = (
                                f"{audit_result.findings}\n"
                                "rework spawn cleanup uncertain; canonical packet "
                                "was not compensated because the owned process may "
                                "still become a session: "
                                f"{boundary_reason} "
                                f"(session_hint={exc.session_hint})"
                            ).strip()
                            outcome = self._finalize(
                                record,
                                event,
                                state="HUMAN_REQUIRED",
                                action="stop",
                                verdict="HUMAN_REQUIRED",
                                extra={
                                    "reason": "spawn_cleanup_uncertain",
                                    "dispatch_session_hint": exc.session_hint,
                                    "dispatch_command": exc.command,
                                    "cleanup_error": exc.cleanup_error,
                                },
                            )
                        except DispatchSpawnedButUnobservedError as exc:
                            # Wrapper may exist, but no Cursor session/process was
                            # confirmed — compensate the packet and stop for human.
                            boundary_reason = redact_absolute_paths(str(exc))
                            try:
                                self.work_packet.apply_dispatch_blocked(
                                    repository=record.repository,
                                    issue_number=record.issue_number,
                                    branch=record.branch,
                                    workstream=record.workstream,
                                    findings=audit_result.findings,
                                    attempt=next_attempt,
                                    head=event.head,
                                    reason=boundary_reason,
                                )
                            except ValidationError as packet_exc:
                                boundary_reason = (
                                    f"{boundary_reason}; compensating packet update "
                                    f"also failed: {packet_exc}"
                                )
                            record.last_findings = (
                                f"{audit_result.findings}\n"
                                f"rework spawn unobserved (no confirmed session): "
                                f"{boundary_reason}"
                            ).strip()
                            outcome = self._finalize(
                                record,
                                event,
                                state="HUMAN_REQUIRED",
                                action="stop",
                                verdict="HUMAN_REQUIRED",
                                extra={
                                    "reason": "spawned_but_unobserved",
                                    "dispatch_session_hint": exc.session_hint,
                                    "dispatch_command": exc.command,
                                },
                            )
                        except ValidationError as exc:
                            boundary_reason = redact_absolute_paths(str(exc))
                            try:
                                self.work_packet.apply_dispatch_blocked(
                                    repository=record.repository,
                                    issue_number=record.issue_number,
                                    branch=record.branch,
                                    workstream=record.workstream,
                                    findings=audit_result.findings,
                                    attempt=next_attempt,
                                    head=event.head,
                                    reason=boundary_reason,
                                )
                            except ValidationError as packet_exc:
                                boundary_reason = (
                                    f"{boundary_reason}; compensating packet update "
                                    f"also failed: {packet_exc}"
                                )
                            record.last_findings = (
                                f"{audit_result.findings}\n"
                                f"rework dispatch blocked at boundary: {boundary_reason}"
                            ).strip()
                            extra = {"reason": "dispatch_boundary_failed"}
                            if isinstance(exc, ResourcePreflightBlocked):
                                extra = {
                                    "reason": "resource_preflight_blocked",
                                    "resource_preflight_result": exc.preflight_result,
                                    "resource_preflight_reason": _bounded_preflight_reason(
                                        exc.preflight_reason
                                    ),
                                }
                            outcome = self._finalize(
                                record,
                                event,
                                state="HUMAN_REQUIRED",
                                action="stop",
                                verdict="HUMAN_REQUIRED",
                                extra=extra,
                            )
                        else:
                            record.attempt = next_attempt
                            record.last_session_id = dispatch.session_id
                            record.expected_head = event.head
                            record.last_findings = audit_result.findings
                            extra = {
                                "dispatch_session_id": dispatch.session_id,
                                "dispatch_command": dispatch.command,
                                "next_attempt": next_attempt,
                                "resume_prompt": RESUME_PROMPT,
                            }
                            if dispatch.resource_preflight_result:
                                extra["resource_preflight_result"] = (
                                    dispatch.resource_preflight_result
                                )
                                extra["resource_preflight_reason"] = (
                                    dispatch.resource_preflight_reason
                                )
                            outcome = self._finalize(
                                record,
                                event,
                                state="REWORK_DISPATCHED",
                                action="rework_dispatched",
                                verdict="REWORK",
                                extra=extra,
                            )
        self.observer.observe("completion_handled", outcome)
        return outcome

    def _reject_if_unclean_snapshot(
        self,
        event: CompletionEvent,
        record: WorkstreamRecord,
        *,
        prefix: str,
    ) -> AuditResult | None:
        """Reject PASS/REWORK unless the worktree is still a clean snapshot.

        Returns a HUMAN_REQUIRED AuditResult when identity/porcelain checks fail;
        None when the gate may proceed. Skipped when identity enforcement is
        disabled (deterministic unit tests that inject FixedAuditAdapter without
        git).
        """
        if not self.enforce_worktree_identity:
            return None
        try:
            validate_clean_worktree_identity(
                record.worktree_path,
                repository=record.repository,
                branch=event.branch,
                expected_head=event.head,
                git_runner=self._git_runner,
            )
        except ValidationError as exc:
            detail = redact_absolute_paths(str(exc))
            prior = (record.last_findings or "").strip()
            findings = f"{prefix} ({detail[:300]})"
            if prior:
                findings = f"{prior}\n{findings}"
            return AuditResult(verdict="HUMAN_REQUIRED", findings=findings)
        return None

    def _finalize(
        self,
        record: WorkstreamRecord,
        event: CompletionEvent,
        *,
        state: str,
        action: str,
        verdict: str,
        extra: dict | None = None,
    ) -> dict:
        if event.event_id not in record.processed_event_ids:
            record.processed_event_ids.append(event.event_id)
        record.state = state
        record.pending_event = None
        outcome = {
            "workstream": record.workstream,
            "event_id": event.event_id,
            "state": state,
            "action": action,
            "verdict": verdict,
            "attempt": event.attempt,
            "head": event.head,
            "findings": record.last_findings,
            "idempotent_replay": False,
        }
        if extra:
            outcome.update(extra)
        record.last_outcome = outcome
        self.store.put(record)
        return outcome

    def _assert_event_identity(
        self, record: WorkstreamRecord, event: CompletionEvent
    ) -> None:
        self._validate_head(event.head)
        if record.state in TERMINAL_STATES:
            raise ValidationError(
                f"workstream {record.workstream} is terminal ({record.state}); "
                "register a new cycle before accepting completions"
            )
        if event.issue_number != record.issue_number:
            raise ValidationError(
                f"issue_number mismatch: event={event.issue_number} "
                f"registered={record.issue_number}"
            )
        if event.branch != record.branch:
            raise ValidationError(
                f"branch mismatch: event={event.branch} registered={record.branch}"
            )
        if self.enforce_worktree_identity:
            validate_worktree_identity(
                record.worktree_path,
                repository=record.repository,
                branch=event.branch,
                expected_head=event.head,
                git_runner=self._git_runner,
            )
        if record.state == "REWORK_DISPATCHED":
            # Rework may produce a new HEAD; accept any well-formed sha and
            # lock it as expected_head once this event is accepted.
            if event.attempt != record.attempt:
                raise ValidationError(
                    f"attempt mismatch after rework: event={event.attempt} "
                    f"expected={record.attempt}"
                )
            record.expected_head = event.head
            self.store.put(record)
            return
        if event.head != record.expected_head:
            raise ValidationError(
                f"stale or invalid head: event={event.head} "
                f"expected={record.expected_head}"
            )
        if record.state == "IDLE" and record.attempt == 0:
            if event.attempt != 1:
                raise ValidationError(
                    f"first completion attempt must be 1, got {event.attempt}"
                )
            return
        raise ValidationError(
            f"refusing completion in state={record.state} attempt={record.attempt}"
        )

    @staticmethod
    def _validate_workstream_id(workstream: str) -> None:
        if not WORKSTREAM_RE.match(workstream):
            raise ValidationError(f"invalid workstream id: {workstream}")

    @staticmethod
    def _validate_head(head: str) -> None:
        if not HEAD_RE.match(head.strip().lower()):
            raise ValidationError(f"invalid head sha: {head}")


def default_data_root() -> Path:
    env = os.environ.get("ATLAS_DATA_ROOT", "").strip()
    if env:
        return Path(env)
    return Path.cwd() / ".atlas-data"


def load_completion_event(path: Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValidationError("completion event must be a JSON object")
    return raw


def completion_inbox_dir(data_root: Path) -> Path:
    return Path(data_root) / COMPLETION_INBOX_DIRNAME


def completion_processed_dir(data_root: Path) -> Path:
    return Path(data_root) / COMPLETION_PROCESSED_DIRNAME


def enqueue_completion_event(data_root: Path, event: dict, *, filename: str | None = None) -> Path:
    """Write a completion event into the local inbox (Cursor hook path)."""
    CompletionEvent.from_dict(event)  # validate early
    inbox = completion_inbox_dir(data_root)
    inbox.mkdir(parents=True, exist_ok=True)
    name = filename or f"{event['event_id']}.json"
    if "/" in name or name.startswith("."):
        raise ValidationError(f"invalid completion inbox filename: {name}")
    path = inbox / name
    if path.exists():
        raise ValidationError(f"completion inbox file already exists: {path.name}")
    path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def drain_completion_inbox(controller: WorkController, data_root: Path) -> list[dict]:
    """Process queued completion events from the local inbox directory."""
    inbox = completion_inbox_dir(data_root)
    processed = completion_processed_dir(data_root)
    processed.mkdir(parents=True, exist_ok=True)
    if not inbox.exists():
        return []
    outcomes: list[dict] = []
    for path in sorted(inbox.glob("*.json")):
        event = load_completion_event(path)
        outcome = controller.handle_completion(event)
        outcome = dict(outcome)
        outcome["inbox_file"] = path.name
        dest = processed / path.name
        if dest.exists():
            dest = processed / f"{path.stem}-{os.getpid()}{path.suffix}"
        path.replace(dest)
        outcomes.append(outcome)
    return outcomes


AUDIT_DEVELOPER_PROMPT = """You are an independent engineering auditor for DataRelay Atlas.
Return ONLY a JSON object with keys:
  verdict: one of PASS, REWORK, HUMAN_REQUIRED
  findings: concise evidence-backed string
Do not include secrets, credentials, or absolute local home paths.
PASS only when the stated completion evidence satisfies the workstream goal.
REWORK when actionable defects remain that a fresh /work-resume cycle can fix.
HUMAN_REQUIRED when owner judgment, credentials, or out-of-scope decisions are needed.
"""


def build_openai_audit_request(
    event: CompletionEvent,
    record: WorkstreamRecord,
    *,
    model: str = DEFAULT_AUDIT_MODEL,
) -> dict:
    """Construct a Responses API background request body (no credentials)."""
    user_payload = {
        "workstream": record.workstream,
        "repository": record.repository,
        "issue_number": record.issue_number,
        "branch": event.branch,
        "head": event.head,
        "attempt": event.attempt,
        "max_attempts": record.max_attempts,
        "event_id": event.event_id,
        "controller_state_before_audit": record.state,
        "resume_prompt": RESUME_PROMPT,
    }
    return {
        "model": model,
        "background": True,
        "store": False,
        "input": [
            {
                "role": "developer",
                "content": AUDIT_DEVELOPER_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(user_payload, sort_keys=True),
            },
        ],
    }


def extract_response_output_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


def parse_audit_verdict_payload(text: str) -> AuditResult:
    """Normalize model output into AuditResult; fail closed on ambiguity."""
    raw = text.strip()
    if not raw:
        raise ValidationError("audit response output was empty")
    candidate = raw
    if not candidate.startswith("{"):
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if not match:
            raise ValidationError("audit response did not contain a JSON object")
        candidate = match.group(0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValidationError("audit response JSON was invalid") from exc
    if not isinstance(data, dict):
        raise ValidationError("audit response JSON must be an object")
    verdict = str(data.get("verdict", "")).strip().upper()
    findings = str(data.get("findings", "")).strip()
    if verdict not in AUDIT_VERDICTS:
        raise ValidationError(f"audit response verdict invalid: {verdict!r}")
    return AuditResult(verdict=verdict, findings=findings)


class OpenAIHttpTransport:
    """Minimal HTTPS JSON transport for the OpenAI Responses API."""

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_OPENAI_API_BASE,
        opener: Callable[..., object] | None = None,
        timeout_sec: float = 60.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self.timeout_sec = timeout_sec

    def request_json(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        body: dict | None = None,
    ) -> dict:
        if not api_key:
            raise ValidationError("OpenAI API key is required")
        url = f"{self.api_base}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "datarelay-atlas-work-controller",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(req, timeout=self.timeout_sec) as response:  # type: ignore[arg-type]
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise ValidationError(
                f"OpenAI HTTP {exc.code} for {method} {path}: {detail[:300]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ValidationError(f"OpenAI network error: {exc.reason}") from exc
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValidationError("OpenAI response must be a JSON object")
        return payload


class OpenAIResponsesAuditAdapter:
    """Production audit adapter using OpenAI Responses background lifecycle.

    Credentials are read from the runtime environment only and never returned or
    persisted. Deterministic tests must inject a fake transport or use
    FixedAuditAdapter.
    """

    def __init__(
        self,
        *,
        transport: OpenAIHttpTransport | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        model: str = DEFAULT_AUDIT_MODEL,
        poll_interval_sec: float = 2.0,
        max_polls: int = 60,
        create_retries: int = 2,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.transport = transport or OpenAIHttpTransport()
        self._api_key_env = api_key_env
        self.model = model
        self.poll_interval_sec = poll_interval_sec
        self.max_polls = max_polls
        self.create_retries = create_retries
        self._sleep = sleeper or time.sleep
        self.last_request_body: dict | None = None
        self.last_response_id: str | None = None
        self.poll_statuses: list[str] = []

    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        api_key = os.environ.get(self._api_key_env, "").strip()
        if not api_key:
            raise ValidationError(
                f"{self._api_key_env} is required for OpenAI Responses audit adapter"
            )
        body = build_openai_audit_request(event, record, model=self.model)
        self.last_request_body = body
        created = self._create_with_retries(api_key, body)
        response_id = str(created.get("id", "")).strip()
        if not response_id:
            raise ValidationError("OpenAI create response missing id")
        self.last_response_id = response_id
        status = str(created.get("status", "")).strip()
        payload = created
        polls = 0
        while status in OPENAI_PENDING_STATUSES:
            if polls >= self.max_polls:
                return AuditResult(
                    verdict="HUMAN_REQUIRED",
                    findings=f"openai response {response_id} still {status} after max polls",
                )
            self._sleep(self.poll_interval_sec)
            payload = self.transport.request_json(
                "GET", f"/responses/{response_id}", api_key=api_key
            )
            status = str(payload.get("status", "")).strip()
            self.poll_statuses.append(status)
            polls += 1
        return self._map_terminal(response_id, status, payload)

    def _create_with_retries(self, api_key: str, body: dict) -> dict:
        last_error: Exception | None = None
        attempts = max(1, self.create_retries + 1)
        for index in range(attempts):
            try:
                return self.transport.request_json(
                    "POST", "/responses", api_key=api_key, body=body
                )
            except ValidationError as exc:
                last_error = exc
                if index + 1 >= attempts:
                    break
                self._sleep(self.poll_interval_sec)
        assert last_error is not None
        raise ValidationError(f"openai create failed after retries: {last_error}") from last_error

    def _map_terminal(self, response_id: str, status: str, payload: dict) -> AuditResult:
        if status == "completed":
            text = extract_response_output_text(payload)
            try:
                return parse_audit_verdict_payload(text)
            except ValidationError as exc:
                return AuditResult(
                    verdict="HUMAN_REQUIRED",
                    findings=f"openai response {response_id} completed but verdict parse failed: {exc}",
                )
        if status in {"failed", "cancelled", "incomplete"}:
            detail = str(payload.get("error") or payload.get("incomplete_details") or status)
            return AuditResult(
                verdict="HUMAN_REQUIRED",
                findings=f"openai response {response_id} terminal status={status}: {detail}",
            )
        return AuditResult(
            verdict="HUMAN_REQUIRED",
            findings=f"openai response {response_id} unknown status={status!r}",
        )


class OpenAIAuditAdapter(OpenAIResponsesAuditAdapter):
    """Backward-compatible name for the production Responses audit adapter."""

    def __init__(
        self,
        *,
        execute: Callable[[CompletionEvent, WorkstreamRecord, str], AuditResult] | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        **kwargs,
    ) -> None:
        # Legacy injectable execute callback remains supported for older callers.
        self._execute = execute
        super().__init__(api_key_env=api_key_env, **kwargs)

    def audit(self, event: CompletionEvent, record: WorkstreamRecord) -> AuditResult:
        if self._execute is not None:
            api_key = os.environ.get(self._api_key_env, "").strip()
            if not api_key:
                raise ValidationError(
                    f"{self._api_key_env} is required for OpenAI audit adapter"
                )
            return self._execute(event, record, api_key)
        return super().audit(event, record)
