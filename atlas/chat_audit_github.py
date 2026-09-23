"""GitHub-backed Audit Control Packet and [AI Work] finding handoff (ADR-0007).

Canonical durable checkpoint state uses the GitHub Contents API with
server-enforced blob-SHA compare-and-set. Issue bodies are not an atomic
mutation surface. Local file stores remain derived cache / offline tests only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator
from contextlib import contextmanager

from atlas.chat_audit import (
    AuditControlPacket,
    AuditFinding,
    CheckpointCasConflict,
    CheckpointStore,
    FileCheckpointStore,
    finding_for_handoff_persistence,
    sanitize_packet_for_persistence,
)
from atlas.provenance import ValidationError
from atlas.secrets import sanitize_durable_text
from atlas.work_controller import normalize_github_repository

DEFAULT_CHECKPOINT_BRANCH = "atlas/chat-audit-control"
HANDOFF_MARKER = "<!-- atlas-chat-audit-finding-id:"
HANDOFF_CLAIM_LEASE_SECONDS = 120.0
CHECKPOINT_WORKSTREAM = "continuous-chat-audit-supervisor-poc"
REQUIRED_PR_CHECK_NAMES = frozenset(
    {
        "adoption-compliance",
        "enforcement-reconcile",
        "affected-tests",
    }
)
SUPPORTED_WORK_PACKET_STATUSES = frozenset({"ACTIVE"})
# Cheap-path PASS requires an explicit terminal pass/ready gate. REWORK,
# FINAL_AUDIT, IMPLEMENTATION, and any other non-terminal gate block PASS.
WORK_PACKET_PASS_GATES = frozenset({"PASS", "PASSED", "READY", "COMPLETE"})
# GitHub Contents GET omits inline `content` at 1 MiB. Stay strictly below
# that boundary so a successful PUT remains readable by load().
CONTENTS_INLINE_MAX_BYTES = 1_000_000
CHECKPOINT_MAX_BYTES = CONTENTS_INLINE_MAX_BYTES - 1
# Exact marker search page. A full page is not authoritative absence.
HANDOFF_MARKER_SEARCH_LIMIT = 100
CHECKPOINT_DISCOVERY_SEARCH_LIMIT = 100


def checkpoint_json_text(packet: AuditControlPacket) -> str:
    return json.dumps(packet.to_dict(), indent=2, sort_keys=True) + "\n"


def assert_checkpoint_inline_size(raw: str) -> None:
    """Refuse a canonical payload GitHub Contents would not return inline."""
    size = len(raw.encode("utf-8"))
    if size > CHECKPOINT_MAX_BYTES:
        raise ValidationError(
            "checkpoint JSON exceeds GitHub Contents inline limit "
            f"({size} > {CHECKPOINT_MAX_BYTES} bytes); refusing PUT so a "
            "canonical checkpoint cannot become unreadable"
        )
CommandRunner = Callable[[list[str], str], subprocess.CompletedProcess[str]]


def _default_runner(
    argv: list[str], cwd: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(f"command timed out: {' '.join(argv[:4])}") from exc
    except FileNotFoundError as exc:
        raise ValidationError(f"command not found: {argv[0]}") from exc


def checkpoint_contents_path(issue_number: int) -> str:
    return f".atlas/chat-audit/checkpoints/issue-{int(issue_number)}.json"


def _is_contents_conflict(completed: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{completed.stderr or ''}\n{completed.stdout or ''}".lower()
    if "http 409" in detail or '"status":"409"' in detail.replace(" ", ""):
        return True
    if "http 422" in detail and (
        "sha" in detail or "conflict" in detail or "already exists" in detail
    ):
        return True
    if "is at " in detail and "but expected" in detail:
        return True
    return False


def _contents_response_sha(payload: Any) -> str | None:
    """Return a server-provided blob SHA from a Contents API response."""
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    new_sha = content.get("sha") if isinstance(content, dict) else None
    if not new_sha:
        new_sha = payload.get("sha")
    if isinstance(new_sha, str) and new_sha.strip():
        return new_sha.strip()
    return None


def _classify_contents_payload(
    payload: dict[str, Any] | None, intended_raw: str
) -> tuple[str, str | None]:
    """Classify a Contents GET against the exact bytes we intended to commit.

    Returns ``(status, blob_sha)`` where status is ``match``, ``differ``,
    ``absent``, ``unverified``, or ``unknown``. A match requires the server
    blob SHA; this function never invents one.
    """
    if payload is None:
        return "absent", None
    if not isinstance(payload, dict):
        return "unknown", None
    encoded = str(payload.get("content") or "")
    blob = str(payload.get("sha") or "").strip()
    if not encoded:
        return "unknown", None
    try:
        decoded = base64.b64decode(encoded, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return "unknown", None
    if decoded == intended_raw and blob:
        return "match", blob
    if decoded == intended_raw:
        return "unverified", None
    return "differ", None


def _sha_from_contents_put(
    *,
    completed: subprocess.CompletedProcess[str],
    intended_raw: str,
    observe: Callable[[], tuple[str, str | None]],
    conflict_message: str,
    failure_label: str,
) -> str:
    """Resolve a Contents PUT to the server blob SHA.

    Transport failures and responses that omit ``content.sha`` are reconciled
    by re-reading canonical contents. Exact committed bytes adopt the server
    SHA. Different canonical bytes are a conflict. Nothing here synthesizes a
    SHA from local JSON.
    """

    def _adopt_match() -> str | None:
        status, sha = observe()
        if status == "match" and sha:
            return sha
        return None

    if completed.returncode != 0:
        status, sha = observe()
        if status == "match" and sha:
            return sha
        if _is_contents_conflict(completed) or status == "differ":
            raise CheckpointCasConflict(conflict_message)
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValidationError(detail[:500] or failure_label)
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        adopted = _adopt_match()
        if adopted:
            return adopted
        raise ValidationError(f"{failure_label} returned non-JSON") from exc
    new_sha = _contents_response_sha(payload)
    if new_sha:
        return new_sha
    adopted = _adopt_match()
    if adopted:
        return adopted
    raise ValidationError(
        f"{failure_label} omitted the server blob SHA and canonical "
        "re-read did not confirm the committed bytes"
    )


class GitHubContentsCheckpointStore:
    """Canonical checkpoint store via Contents API blob-SHA CAS.

    GitHub Issue body read-modify-write is not atomic across hosts. Mutations
    bind to the blob SHA observed at load and use PUT
    /repos/.../contents/... so the server rejects a stale expected SHA.
    """

    def __init__(
        self,
        *,
        repository: str,
        issue_number: int,
        branch: str | None = None,
        path: str | None = None,
        cache: FileCheckpointStore | None = None,
        command_runner: CommandRunner | None = None,
        cwd: str | None = None,
    ):
        self.repository = normalize_github_repository(repository)
        if int(issue_number) < 1:
            raise ValidationError(f"invalid checkpoint issue_number: {issue_number}")
        self.issue_number = int(issue_number)
        env_branch = os.environ.get("ATLAS_CHAT_AUDIT_CHECKPOINT_BRANCH", "").strip()
        self.branch = (
            (branch or env_branch or DEFAULT_CHECKPOINT_BRANCH).strip()
            or DEFAULT_CHECKPOINT_BRANCH
        )
        self.path = path or checkpoint_contents_path(self.issue_number)
        self.cache = cache
        self._runner = command_runner or _default_runner
        self._cwd = cwd or str(Path.cwd())
        # Blob SHA observed by the latest successful load/save (None = absent).
        self._cas_blob_sha: str | None = None
        self._cas_loaded = False
        # Derived-cache failures are telemetry. They never fail canonical I/O.
        self.last_cache_error: str | None = None

    @contextmanager
    def lock(self) -> Iterator[None]:
        if self.cache is not None:
            with self.cache.lock():
                yield
        else:
            yield

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(argv, self._cwd)

    def _api_endpoint(self) -> str:
        return f"repos/{self.repository}/contents/{self.path}"

    def _get_contents(self) -> dict[str, Any] | None:
        completed = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"{self._api_endpoint()}?ref={self.branch}",
            ]
        )
        detail = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        if completed.returncode != 0:
            if "404" in detail or "Not Found" in detail:
                return None
            raise ValidationError(
                (completed.stderr or completed.stdout or "").strip()[:500]
                or "gh api contents GET failed for audit checkpoint"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("gh api contents GET returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("gh api contents GET returned non-object JSON")
        payload_type = payload.get("type")
        if payload_type is not None and payload_type != "file":
            raise ValidationError(
                f"checkpoint path must be a file, got {payload_type!r}"
            )
        return payload

    def _observe_contents(self, intended_raw: str) -> tuple[str, str | None]:
        try:
            payload = self._get_contents()
        except ValidationError:
            return "unknown", None
        return _classify_contents_payload(payload, intended_raw)

    def _write_cache(self, packet: AuditControlPacket) -> None:
        """Best-effort derived cache. Canonical success does not depend on it."""
        if self.cache is None:
            return
        try:
            self.cache.save(packet)
        except Exception as exc:
            self.last_cache_error = f"{type(exc).__name__}: {exc}"[:500]
            return
        self.last_cache_error = None

    def _put_contents(
        self,
        *,
        packet: AuditControlPacket,
        expected_sha: str | None,
    ) -> str:
        raw = checkpoint_json_text(packet)
        assert_checkpoint_inline_size(raw)
        body: dict[str, Any] = {
            "message": (
                f"atlas chat-audit checkpoint issue-{self.issue_number} "
                f"rev={packet.canonical_revision}"
            ),
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if expected_sha:
            body["sha"] = expected_sha
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(body, handle)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "PUT",
                    "-H",
                    "Accept: application/vnd.github+json",
                    self._api_endpoint(),
                    "--input",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        return _sha_from_contents_put(
            completed=completed,
            intended_raw=raw,
            observe=lambda: self._observe_contents(raw),
            conflict_message=(
                "checkpoint compare-and-set failed: contents blob SHA "
                "conflict (stale or concurrent writer)"
            ),
            failure_label="gh api contents PUT failed for audit checkpoint",
        )

    def ensure_control_branch(self) -> dict[str, Any]:
        """Idempotently create the control branch from the repo default branch."""
        completed = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repository}",
            ]
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500] or "gh api repo metadata failed for control branch"
            )
        try:
            meta = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("repo metadata returned non-JSON") from exc
        default_branch = str(meta.get("default_branch") or "").strip()
        if not default_branch:
            raise ValidationError("repository default_branch is required")
        ref_completed = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repository}/git/ref/heads/{default_branch}",
            ]
        )
        if ref_completed.returncode != 0:
            detail = (ref_completed.stderr or ref_completed.stdout or "").strip()
            raise ValidationError(
                detail[:500] or "failed to resolve default branch SHA"
            )
        try:
            ref_payload = json.loads(ref_completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("default branch ref returned non-JSON") from exc
        obj = ref_payload.get("object") if isinstance(ref_payload, dict) else None
        base_sha = str((obj or {}).get("sha") or "").strip()
        if not base_sha:
            raise ValidationError("default branch SHA missing")
        # Probe control branch.
        probe = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repository}/git/ref/heads/{self.branch}",
            ]
        )
        if probe.returncode == 0:
            return {"action": "exists", "branch": self.branch, "base_sha": base_sha}
        body = {
            "ref": f"refs/heads/{self.branch}",
            "sha": base_sha,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(body, handle)
            path = handle.name
        try:
            created = self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"repos/{self.repository}/git/refs",
                    "--input",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if created.returncode != 0:
            detail = f"{created.stderr or ''}\n{created.stdout or ''}"
            # Concurrent bootstrap: only the already-exists conflict is success.
            already_exists = (
                "Reference already exists" in detail
                or '"message":"Reference already exists"' in detail
                or (
                    "already exists" in detail.lower() and "422" in detail
                )
            )
            if already_exists:
                verify = self._run(
                    [
                        "gh",
                        "api",
                        "-H",
                        "Accept: application/vnd.github+json",
                        f"repos/{self.repository}/git/ref/heads/{self.branch}",
                    ]
                )
                if verify.returncode == 0:
                    return {
                        "action": "exists_race",
                        "branch": self.branch,
                        "base_sha": base_sha,
                    }
            raise ValidationError(
                (created.stderr or created.stdout or "").strip()[:500]
                or "failed to bootstrap chat-audit control branch"
            )
        return {"action": "created", "branch": self.branch, "base_sha": base_sha}

    def load(self) -> AuditControlPacket | None:

        payload = self._get_contents()
        if payload is None:
            self._cas_blob_sha = None
            self._cas_loaded = True
            return None
        encoded = str(payload.get("content") or "")
        if not encoded:
            raise ValidationError("checkpoint contents payload missing content")
        try:
            decoded = base64.b64decode(encoded, validate=False).decode("utf-8")
            raw = json.loads(decoded)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("checkpoint contents JSON is invalid") from exc
        if not isinstance(raw, dict):
            raise ValidationError("checkpoint contents JSON must be an object")
        packet = AuditControlPacket.from_dict(raw)
        blob_sha = str(payload.get("sha") or "").strip()
        if not blob_sha:
            raise ValidationError("checkpoint contents response missing blob sha")
        self._cas_blob_sha = blob_sha
        self._cas_loaded = True
        self._write_cache(packet)
        return packet

    def save(self, packet: AuditControlPacket) -> None:
        """Persist via Contents API PUT bound to the loaded blob SHA.

        Concurrency safety comes from GitHub rejecting a stale expected
        ``sha``. Ambiguous PUT results (transport failure or a success body
        that omits the blob SHA) re-read canonical contents and adopt the
        server SHA only when those bytes match. Derived cache writes are
        best-effort and cannot turn a committed PUT into a failure.
        """
        if not self._cas_loaded:
            # Require an explicit load (or prior successful save) so writers bind
            # to a server-observed CAS token rather than inventing one.
            raise ValidationError(
                "checkpoint save requires a prior load to bind Contents CAS sha"
            )
        # Reject an already-oversized packet before durable-text scanning.
        # That scan is not practical at the Contents inline boundary.
        assert_checkpoint_inline_size(checkpoint_json_text(packet))
        safe = sanitize_packet_for_persistence(packet)
        expected_sha = self._cas_blob_sha
        if expected_sha is None:
            if int(safe.canonical_revision) != 0:
                raise CheckpointCasConflict(
                    "checkpoint compare-and-set failed: create requires "
                    "canonical_revision=0 when contents are absent"
                )
            next_revision = 1
            self.ensure_control_branch()
        else:
            next_revision = int(safe.canonical_revision) + 1
        # Mutate only the sanitized copy until canonical PUT succeeds so a
        # transient failure cannot poison the caller-visible revision/CAS token.
        safe.canonical_revision = next_revision
        new_sha = self._put_contents(packet=safe, expected_sha=expected_sha)
        packet.canonical_revision = next_revision
        self._cas_blob_sha = new_sha
        self._cas_loaded = True
        # Cache failure must not roll back the CAS token or caller revision.
        self._write_cache(safe)


# Backward-compatible alias for older imports/docs.
GitHubIssueCheckpointStore = GitHubContentsCheckpointStore


class GitHubAIWorkHandoff:
    """Idempotent GitHub [AI Work] create/update handoff via Contents claim CAS."""

    def __init__(
        self,
        *,
        repository: str,
        command_runner: CommandRunner | None = None,
        cwd: str | None = None,
        checkpoint_branch: str | None = None,
    ):
        self.repository = normalize_github_repository(repository)
        self._runner = command_runner or _default_runner
        self._cwd = cwd or str(Path.cwd())
        env_branch = os.environ.get("ATLAS_CHAT_AUDIT_CHECKPOINT_BRANCH", "").strip()
        self.branch = (
            (checkpoint_branch or env_branch or DEFAULT_CHECKPOINT_BRANCH).strip()
            or DEFAULT_CHECKPOINT_BRANCH
        )
        self.handoffs: list[dict[str, Any]] = []
        self._control = GitHubContentsCheckpointStore(
            repository=self.repository,
            issue_number=1,
            branch=self.branch,
            path=".atlas/chat-audit/handoff-claims/_bootstrap.json",
            command_runner=self._runner,
            cwd=self._cwd,
        )

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(argv, self._cwd)

    def _claim_path(self, finding_id: str) -> str:
        digest = hashlib.sha256(finding_id.encode("utf-8")).hexdigest()[:24]
        return f".atlas/chat-audit/handoff-claims/{digest}.json"

    def _get_claim(self, finding_id: str) -> tuple[dict[str, Any] | None, str | None]:
        path = self._claim_path(finding_id)
        completed = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repository}/contents/{path}?ref={self.branch}",
            ]
        )
        detail = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        if completed.returncode != 0:
            if "404" in detail or "Not Found" in detail:
                return None, None
            raise ValidationError(
                (completed.stderr or completed.stdout or "").strip()[:500]
                or "handoff claim GET failed"
            )
        payload = json.loads(completed.stdout)
        encoded = str(payload.get("content") or "")
        raw = base64.b64decode(encoded, validate=False).decode("utf-8")
        claim = json.loads(raw)
        if not isinstance(claim, dict):
            raise ValidationError("handoff claim must be an object")
        return claim, str(payload.get("sha") or "").strip() or None

    def _observe_claim_file(
        self, path: str, intended_raw: str
    ) -> tuple[str, str | None]:
        completed = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{self.repository}/contents/{path}?ref={self.branch}",
            ]
        )
        detail = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        if completed.returncode != 0:
            if "404" in detail or "Not Found" in detail:
                return "absent", None
            return "unknown", None
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return "unknown", None
        if not isinstance(payload, dict):
            return "unknown", None
        return _classify_contents_payload(payload, intended_raw)

    def _put_claim(
        self,
        finding_id: str,
        claim: dict[str, Any],
        *,
        expected_sha: str | None,
    ) -> str:
        path = self._claim_path(finding_id)
        raw = json.dumps(claim, indent=2, sort_keys=True) + "\n"
        body: dict[str, Any] = {
            "message": f"atlas chat-audit handoff claim {finding_id}",
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if expected_sha:
            body["sha"] = expected_sha
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(body, handle)
            tmp = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "PUT",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"repos/{self.repository}/contents/{path}",
                    "--input",
                    tmp,
                ]
            )
        finally:
            Path(tmp).unlink(missing_ok=True)
        return _sha_from_contents_put(
            completed=completed,
            intended_raw=raw,
            observe=lambda: self._observe_claim_file(path, raw),
            conflict_message=(
                "handoff claim compare-and-set failed: concurrent creator"
            ),
            failure_label="handoff claim PUT failed",
        )

    def upsert_implementation_packet(
        self, packet: AuditControlPacket, finding: AuditFinding
    ) -> dict[str, Any]:
        # Same persistence boundary as FileWorkPacketHandoff. Executor checks
        # are not a substitute when a caller invokes this adapter directly.
        safe = finding_for_handoff_persistence(finding)
        marker = f"{HANDOFF_MARKER}{safe.finding_id} -->"
        title = f"[AI Work] Audit finding: {safe.finding_id}"[:240]
        body = (
            f"{marker}\n"
            f"PACKET_VERSION=2\n"
            f"TARGET_REPO={packet.target_repository}\n"
            f"STATUS=ACTIVE\n"
            f"BRANCH={packet.target_branch}\n"
            f"TASK_KIND=DEVELOPMENT\n"
            f"OWNER_INTENT=Implement bounded audit finding {safe.finding_id}\n"
            f"LAST_VERIFIED_HEAD={packet.current_target_sha}\n"
            f"GATE=IMPLEMENTATION\n\n"
            f"## Goal\n\n{sanitize_durable_text(safe.summary)}\n\n"
            f"## Finding\n\n"
            f"- id: `{safe.finding_id}`\n"
            f"- unit: `{safe.unit}`\n"
            f"- severity: `{safe.severity}`\n"
            f"- head: `{packet.current_target_sha}`\n\n"
            f"## Next Action\n\n"
            f"1. Implement the bounded audit finding in Cursor.\n"
            f"2. Do not modify product code from Chat.\n"
            f"3. Re-audit exact HEAD after the fix lands.\n"
        )
        self._control.ensure_control_branch()
        claim, claim_sha = self._get_claim(safe.finding_id)

        if claim and claim.get("issue_number"):
            return self._update_existing_claim(
                claim=claim,
                packet=packet,
                safe=safe,
                title=title,
                body=body,
                marker=marker,
                claim_sha=claim_sha,
            )

        # Crash/ambiguity reconcile before any create: durable marker search.
        existing = self._find_issue_by_marker(marker)
        if existing is not None:
            number, url = existing
            finalized = {
                "finding_id": safe.finding_id,
                "owner_token": str((claim or {}).get("owner_token") or uuid.uuid4()),
                "issue_number": number,
                "url": url,
                "run_key": packet.idempotency_run_key,
                "head": packet.current_target_sha,
                "state": "finalized",
                "repository": self.repository,
            }
            try:
                self._put_claim(
                    safe.finding_id, finalized, expected_sha=claim_sha
                )
            except CheckpointCasConflict:
                claim, _ = self._get_claim(safe.finding_id)
                if claim and claim.get("issue_number"):
                    return self._update_existing_claim(
                        claim=claim,
                        packet=packet,
                        safe=safe,
                        title=title,
                        body=body,
                        marker=marker,
                        claim_sha=claim_sha,
                    )
                raise
            self._edit_issue(number, title=title, body=body)
            record = {
                "action": "updated",
                "issue_number": number,
                "title": title,
                "repository": self.repository,
                "finding": safe.to_dict(),
                "url": url,
                "claim": "reconciled_marker",
            }
            self.handoffs.append(record)
            return record

        # Pending claim ownership: live pending is non-stealable.
        if claim and not claim.get("issue_number"):
            owner = str(claim.get("owner_token") or "")
            claimed_at = float(claim.get("claimed_at") or 0)
            lease = float(
                claim.get("lease_seconds") or HANDOFF_CLAIM_LEASE_SECONDS
            )
            age = time.time() - claimed_at if claimed_at else lease + 1
            if owner and age < lease:
                for _ in range(40):
                    time.sleep(0.01)
                    claim, claim_sha = self._get_claim(safe.finding_id)
                    if claim and claim.get("issue_number"):
                        return self._update_existing_claim(
                            claim=claim,
                            packet=packet,
                            safe=safe,
                            title=title,
                            body=body,
                            marker=marker,
                            claim_sha=claim_sha,
                        )
                    if claim:
                        claimed_at = float(claim.get("claimed_at") or 0)
                        lease = float(
                            claim.get("lease_seconds")
                            or HANDOFF_CLAIM_LEASE_SECONDS
                        )
                        if (
                            str(claim.get("owner_token") or "") == owner
                            and claimed_at
                            and (time.time() - claimed_at) < lease
                        ):
                            continue
                        break
                raise ValidationError(
                    "handoff claim pending held by another owner; "
                    "refusing steal; retry required"
                )

        owner_token = str(uuid.uuid4())
        pending = {
            "finding_id": safe.finding_id,
            "owner_token": owner_token,
            "issue_number": None,
            "url": None,
            "run_key": packet.idempotency_run_key,
            "head": packet.current_target_sha,
            "state": "pending",
            "claimed_at": time.time(),
            "lease_seconds": HANDOFF_CLAIM_LEASE_SECONDS,
            "repository": self.repository,
        }
        try:
            claim_sha = self._put_claim(
                safe.finding_id, pending, expected_sha=claim_sha
            )
            claim = pending
        except CheckpointCasConflict:
            claim = None
            for _ in range(40):
                claim, claim_sha = self._get_claim(safe.finding_id)
                if claim and claim.get("issue_number"):
                    break
                time.sleep(0.01)
            if claim and claim.get("issue_number"):
                return self._update_existing_claim(
                    claim=claim,
                    packet=packet,
                    safe=safe,
                    title=title,
                    body=body,
                    marker=marker,
                    claim_sha=claim_sha,
                )
            raise ValidationError(
                "handoff claim held by concurrent writer without issue_number; "
                "retry required"
            )

        if str(claim.get("owner_token") or "") != owner_token:
            raise ValidationError(
                "handoff claim ownership lost before issue create"
            )

        existing = self._find_issue_by_marker(marker)
        if existing is not None:
            number, url = existing
        else:
            number, url = self._create_issue(title=title, body=body)
        claim = {
            **claim,
            "issue_number": number,
            "url": url,
            "owner_token": owner_token,
            "state": "finalized",
            "run_key": packet.idempotency_run_key,
            "head": packet.current_target_sha,
        }
        try:
            self._put_claim(safe.finding_id, claim, expected_sha=claim_sha)
        except CheckpointCasConflict:
            latest, latest_sha = self._get_claim(safe.finding_id)
            if latest and latest.get("issue_number"):
                if int(latest["issue_number"]) != int(number):
                    raise ValidationError(
                        "handoff claim finalized to a different issue after create; "
                        "manual reconciliation required"
                    )
            else:
                self._put_claim(safe.finding_id, claim, expected_sha=latest_sha)
        record = {
            "action": "created",
            "issue_number": number,
            "title": title,
            "repository": self.repository,
            "finding": safe.to_dict(),
            "url": url,
            "claim": "created",
        }
        self.handoffs.append(record)
        return record

    def _update_existing_claim(
        self,
        *,
        claim: dict[str, Any],
        packet: AuditControlPacket,
        safe: AuditFinding,
        title: str,
        body: str,
        marker: str,
        claim_sha: str | None = None,
    ) -> dict[str, Any]:
        if "issue_number" not in claim or claim.get("issue_number") is None:
            raise ValidationError("finalized handoff claim missing issue_number")
        number = int(claim["issue_number"])
        claim_finding = str(claim.get("finding_id") or "").strip()
        if not claim_finding:
            raise ValidationError(
                "finalized handoff claim missing finding_id; refusing issue_number-only trust"
            )
        if claim_finding != safe.finding_id:
            raise ValidationError("handoff claim finding_id mismatch")
        claimed_run = str(claim.get("run_key") or "")
        claimed_head = str(claim.get("head") or "").lower()
        # Always fetch the referenced Issue before trusting issue_number.
        viewed = self._run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                self.repository,
                "--json",
                "number,title,state,body,url",
            ]
        )
        if viewed.returncode != 0:
            raise ValidationError(
                f"handoff claim issue #{number} unavailable; refusing stale claim"
            )
        payload = json.loads(viewed.stdout or "{}")
        state = str(payload.get("state") or "").upper()
        issue_body = str(payload.get("body") or "")
        issue_title = str(payload.get("title") or "")
        if state != "OPEN":
            raise ValidationError(
                f"handoff claim issue #{number} is not OPEN; refusing update"
            )
        if marker not in issue_body:
            raise ValidationError(
                f"handoff claim issue #{number} missing finding marker"
            )
        expected_title = f"[AI Work] Audit finding: {safe.finding_id}"[:240]
        if issue_title.strip() != expected_title:
            raise ValidationError(
                f"handoff claim issue #{number} title is not the audit-finding identity"
            )
        repo_line = re.search(r"(?m)^TARGET_REPO=(\S+)\s*$", issue_body)
        if not repo_line:
            raise ValidationError(
                f"handoff claim issue #{number} missing TARGET_REPO"
            )
        try:
            declared_repo = normalize_github_repository(repo_line.group(1))
        except ValidationError as exc:
            raise ValidationError(
                f"handoff claim issue #{number} missing/mismatched TARGET_REPO"
            ) from exc
        if declared_repo != self.repository or declared_repo != normalize_github_repository(
            packet.target_repository
        ):
            raise ValidationError(
                f"handoff claim issue #{number} missing/mismatched TARGET_REPO"
            )
        self._edit_issue(number, title=title, body=body)
        recurrence = (
            (claimed_run and claimed_run != packet.idempotency_run_key)
            or (
                claimed_head
                and claimed_head != packet.current_target_sha.lower()
            )
        )
        # Explicit recurrence across new run/head: refresh claim identity after
        # verified Issue update (never infer success from issue_number alone).
        refreshed = {
            **claim,
            "finding_id": safe.finding_id,
            "issue_number": number,
            "url": claim.get("url") or payload.get("url"),
            "run_key": packet.idempotency_run_key,
            "head": packet.current_target_sha,
            "state": "finalized",
            "repository": self.repository,
        }
        try:
            sha = claim_sha
            if sha is None:
                _, sha = self._get_claim(safe.finding_id)
            self._put_claim(safe.finding_id, refreshed, expected_sha=sha)
        except CheckpointCasConflict:
            # Issue body already updated; concurrent claim refresh is acceptable
            # when the Issue lifecycle was verified in this call.
            pass
        record = {
            "action": "updated",
            "issue_number": number,
            "title": title,
            "repository": self.repository,
            "finding": safe.to_dict(),
            "url": refreshed.get("url"),
            "claim": "recurrence_update" if recurrence else "existing",
            "prior_run_key": claimed_run or None,
            "prior_head": claimed_head or None,
            "issue_viewed": True,
        }
        self.handoffs.append(record)
        return record

    def _find_issue_by_marker(self, marker: str) -> tuple[int, str] | None:
        """Locate an existing issue by exact durable finding marker.

        Search is server-side on the marker itself. A bounded title listing of
        recent ``[AI Work]`` issues is not authoritative absence.
        """
        listed = self._run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repository,
                "--state",
                "open",
                "--search",
                f'"{marker}" in:body',
                "--json",
                "number,title,body,url",
                "--limit",
                str(HANDOFF_MARKER_SEARCH_LIMIT),
            ]
        )
        if listed.returncode != 0:
            detail = (listed.stderr or listed.stdout or "").strip()
            raise ValidationError(
                "handoff marker lookup unavailable: "
                + (detail[:400] or "gh issue list failed")
            )
        try:
            items = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "handoff marker lookup returned non-JSON"
            ) from exc
        if not isinstance(items, list):
            raise ValidationError("handoff marker lookup returned non-list JSON")
        if len(items) >= HANDOFF_MARKER_SEARCH_LIMIT:
            raise ValidationError(
                "handoff marker lookup truncated; refusing to treat a full "
                "result page as authoritative absence"
            )
        matches = [
            item
            for item in items
            if isinstance(item, dict) and marker in str(item.get("body") or "")
        ]
        if len(matches) == 1:
            number = int(matches[0]["number"])
            url = str(
                matches[0].get("url")
                or f"https://github.com/{self.repository}/issues/{number}"
            )
            return number, url
        if len(matches) > 1:
            raise ValidationError(
                "multiple open handoff issues share the same finding marker"
            )
        return None

    def _create_issue(self, *, title: str, body: str) -> tuple[int, str]:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            handle.write(body)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    self.repository,
                    "--title",
                    title,
                    "--body-file",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        marker_line = body.splitlines()[0] if body else ""
        if completed.returncode != 0:
            existing = (
                self._find_issue_by_marker(marker_line) if marker_line else None
            )
            if existing is not None:
                return existing
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or "gh issue create failed; refusing local-only finding handoff"
            )
        url = (completed.stdout or "").strip().splitlines()[-1].strip()
        match = re.search(r"/issues/(\d+)\s*$", url)
        if not match:
            existing = (
                self._find_issue_by_marker(marker_line) if marker_line else None
            )
            if existing is not None:
                return existing
            raise ValidationError("gh issue create did not return an issue URL")
        return int(match.group(1)), url

    def _edit_issue(self, number: int, *, title: str, body: str) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            handle.write(body)
            path = handle.name
        try:
            completed = self._run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(number),
                    "--repo",
                    self.repository,
                    "--title",
                    title,
                    "--body-file",
                    path,
                ]
            )
        finally:
            Path(path).unlink(missing_ok=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or "gh issue edit failed; refusing local-only finding handoff"
            )



class GitHubCoordinationRefresher:
    """Same-HEAD Work Packet / PR / CI / review refresher for cheap path."""

    def __init__(
        self,
        *,
        repository: str,
        work_packet_issue: int | None = None,
        command_runner: CommandRunner | None = None,
        cwd: str | None = None,
    ):
        self.repository = normalize_github_repository(repository)
        self.work_packet_issue = work_packet_issue
        self._runner = command_runner or _default_runner
        self._cwd = cwd or str(Path.cwd())

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(argv, self._cwd)

    def refresh(self, packet: AuditControlPacket) -> dict[str, Any]:
        from atlas.codex_audit import collect_pr_review_evidence

        reasons: list[str] = []
        evidence: dict[str, Any] = {
            "collector": "atlas.chat_audit_github.GitHubCoordinationRefresher",
            "target_sha": packet.current_target_sha,
        }
        issue = self.work_packet_issue
        if issue is None:
            discovered = discover_checkpoint_issue(
                repository=self.repository,
                command_runner=self._runner,
                cwd=self._cwd,
            )
            issue = discovered
        if issue is not None:
            wp = self._run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(issue),
                    "--repo",
                    self.repository,
                    "--json",
                    "number,title,state,body,updatedAt",
                ]
            )
            if wp.returncode != 0:
                reasons.append("work_packet_unavailable")
                evidence["work_packet"] = {"status": "ERROR"}
            else:
                payload = json.loads(wp.stdout)
                body = str(payload.get("body") or "")
                status_match = re.search(r"(?m)^STATUS=(\S+)", body)
                status = status_match.group(1) if status_match else "UNKNOWN"
                gate_match = re.search(r"(?m)^GATE=(\S+)", body)
                gate = gate_match.group(1).strip() if gate_match else ""
                issue_state = str(payload.get("state") or "").upper()
                evidence["work_packet"] = {
                    "status": status,
                    "gate": gate or "ABSENT",
                    "state": payload.get("state"),
                    "number": payload.get("number"),
                    "updated_at": payload.get("updatedAt"),
                }
                if issue_state != "OPEN":
                    reasons.append("work_packet_issue_not_open")
                if status not in SUPPORTED_WORK_PACKET_STATUSES:
                    reasons.append(f"work_packet_{status.lower()}")
                if not gate:
                    reasons.append("work_packet_gate_missing")
                elif gate.upper() not in WORK_PACKET_PASS_GATES:
                    reasons.append(f"work_packet_gate_{gate.lower()}")
        else:
            evidence["work_packet"] = {"status": "ABSENT"}
            reasons.append("work_packet_undiscovered")

        prs = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repository,
                "--head",
                packet.target_branch,
                "--state",
                "open",
                "--json",
                "number,title,state,headRefOid,url",
                "--limit",
                "5",
            ]
        )
        chosen = None
        if prs.returncode == 0:
            items = json.loads(prs.stdout or "[]")
            head_l = packet.current_target_sha.lower()
            for item in items if isinstance(items, list) else []:
                oid = str(item.get("headRefOid") or "").lower()
                if oid == head_l:
                    chosen = item
                    break
            if chosen is None and items:
                reasons.append("pr_head_mismatch")
                evidence["pr"] = {
                    "status": "MISMATCH",
                    "candidates": [
                        {
                            "number": item.get("number"),
                            "title": item.get("title"),
                            "state": item.get("state"),
                            "headRefOid": item.get("headRefOid"),
                            "url": item.get("url"),
                        }
                        for item in items
                        if isinstance(item, dict)
                    ],
                }
            elif chosen is None:
                evidence["pr"] = {"status": "ABSENT"}
                reasons.append("pr_absent")
            else:
                evidence["pr"] = {
                    "status": "OK",
                    "number": chosen.get("number"),
                    "state": chosen.get("state"),
                    "headRefOid": chosen.get("headRefOid"),
                    "url": chosen.get("url"),
                }
                checks = self._run(
                    [
                        "gh",
                        "pr",
                        "checks",
                        str(chosen.get("number")),
                        "--repo",
                        self.repository,
                    ]
                )
                ci_status, ci_reasons, ci_meta = _evaluate_required_pr_checks(
                    checks.returncode, checks.stdout or ""
                )
                # Persist only allowlisted CI fields; details live in reasons.
                evidence["ci"] = {"status": ci_status}
                if "exit_code" in ci_meta:
                    evidence["ci"]["exit_code"] = ci_meta["exit_code"]
                reasons.extend(ci_reasons)

                review_ev = collect_pr_review_evidence(
                    repository=self.repository,
                    pr_number=int(chosen.get("number")),
                    command_runner=self._runner,
                    target_sha=packet.current_target_sha,
                    exclude_control_comments=True,
                )
                review_status = str(review_ev.get("status") or "ERROR").upper()
                if review_status != "OK":
                    evidence["reviews"] = {
                        "status": review_status,
                        "actionable": True,
                        "count": 0,
                    }
                    reasons.append(
                        "reviews_incomplete"
                        if review_status == "INCOMPLETE"
                        else "reviews_unavailable"
                    )
                else:
                    actionable, count = _reviews_actionable_for_head(
                        review_ev, target_sha=packet.current_target_sha
                    )
                    evidence["reviews"] = {
                        "status": "OK",
                        "actionable": actionable,
                        "count": count,
                    }
                    if actionable:
                        reasons.append("actionable_review")
        else:
            evidence["pr"] = {"status": "ERROR"}
            reasons.append("pr_list_failed")

        # Mandatory surfaces for PASS: WP/PR/CI/reviews must be present.
        for surface in ("work_packet", "pr", "ci", "reviews"):
            if surface not in evidence:
                evidence[surface] = {"status": "ABSENT"}
                reasons.append(f"{surface}_absent")

        if reasons:
            evidence["status"] = "HUMAN_REQUIRED"
            evidence["outcome"] = "HUMAN_REQUIRED"
            evidence["reasons"] = reasons
        else:
            evidence["status"] = "OK"
            evidence["outcome"] = "PASSED"
            evidence["reasons"] = []
        return evidence


def _evaluate_required_pr_checks(
    exit_code: int, stdout: str
) -> tuple[str, list[str], dict[str, Any]]:
    """Require Engineering System check jobs; unrelated-only green is not PASS.

    GitHub Actions reusable-workflow check names are typically
    ``<job-id> / <nested-job>`` (e.g. ``adoption-compliance / compliance``).
    Match required job ids as exact names or validated prefixes.
    """
    reasons: list[str] = []
    rows: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        name = parts[0].strip()
        state = parts[1].strip().lower() if len(parts) > 1 else ""
        rows[name] = state

    def _covers(required: str) -> list[tuple[str, str]]:
        matches: list[tuple[str, str]] = []
        req = required.lower()
        for name, state in rows.items():
            n = name.lower()
            if n == req or n.startswith(req + " /") or n.startswith(req + "/"):
                matches.append((name, state))
        return matches

    missing: list[str] = []
    failing: list[str] = []
    pending: list[str] = []
    for required in sorted(REQUIRED_PR_CHECK_NAMES):
        matches = _covers(required)
        if not matches:
            missing.append(required)
            continue
        if any(
            state in {"pending", "queued", "in_progress", "waiting"}
            for _, state in matches
        ):
            pending.append(required)
        elif not any(
            state in {"pass", "success", "skipped"} for _, state in matches
        ):
            failing.append(required)
    meta: dict[str, Any] = {
        "required": sorted(REQUIRED_PR_CHECK_NAMES),
        "observed": sorted(rows),
    }
    if missing:
        reasons.append("ci_required_checks_missing")
        meta["missing"] = missing
        return "ERROR", reasons, meta
    if pending or exit_code == 8:
        reasons.append("ci_pending")
        meta["pending"] = pending
        return "PENDING", reasons, meta
    if failing:
        reasons.append("ci_fail")
        meta["failing"] = failing
        return "FAIL", reasons, meta
    if exit_code != 0:
        reasons.append("ci_fail")
        return "FAIL", reasons, meta
    return "OK", [], meta


def _commit_matches_head(commit_id: object, target_sha: str) -> bool:
    if commit_id is None:
        return False
    value = str(commit_id).strip().lower()
    head = target_sha.strip().lower()
    if not value or not head:
        return False
    return value == head or value.startswith(head[:12]) or head.startswith(value[:12])


def _body_actionable(text: str) -> bool:
    upper = (text or "").upper()
    if re.search(r"\bP[012]\b", text or "") or "REWORK" in upper:
        if re.search(r"(?i)\bRESOLUTION\s*=\s*RESOLVED\b", text or ""):
            return False
        if re.search(r"(?i)\bresolved:\s*true\b", text or ""):
            return False
        return True
    return False


def _is_control_request_comment(item: dict[str, Any]) -> bool:
    from atlas.codex_audit import _is_control_request_comment as _shared

    return _shared(item)


def _reviews_actionable_for_head(
    review_ev: dict[str, Any], *, target_sha: str
) -> tuple[bool, int]:
    """Inspect reviews/inline/conversation; bind commit-addressable items to HEAD."""
    count = 0
    actionable = False
    for label in ("reviews", "inline_comments", "conversation_comments"):
        items = review_ev.get(label) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            body = str(item.get("body") or "")
            state = str(item.get("state") or "").upper()
            if label == "conversation_comments":
                if _is_control_request_comment(item):
                    continue
                count += 1
                if _body_actionable(body):
                    actionable = True
                continue
            commit = item.get("commit_id") or item.get("original_commit_id")
            if commit is not None and not _commit_matches_head(commit, target_sha):
                # Historical review against another HEAD — ignore for current PASS.
                continue
            count += 1
            if state in {"CHANGES_REQUESTED"}:
                actionable = True
            if _body_actionable(body):
                if commit is None or _commit_matches_head(commit, target_sha):
                    actionable = True
    return actionable, count



ACTIVE_CHECKPOINT_POINTER_PATH = ".atlas/chat-audit/ACTIVE_CHECKPOINT_ISSUE.json"
CHECKPOINT_WORKSTREAM_RE = re.compile(
    r"(?m)^WORKSTREAM=continuous-chat-audit-supervisor-poc\s*$"
)


def discover_checkpoint_issue(
    *,
    repository: str,
    command_runner: CommandRunner | None = None,
    cwd: str | None = None,
    checkpoint_branch: str | None = None,
) -> int:
    """Deterministically discover the chat-audit checkpoint issue number."""
    runner = command_runner or _default_runner
    workdir = cwd or str(Path.cwd())
    env_branch = os.environ.get("ATLAS_CHAT_AUDIT_CHECKPOINT_BRANCH", "").strip()
    branch = (
        (checkpoint_branch or env_branch or DEFAULT_CHECKPOINT_BRANCH).strip()
        or DEFAULT_CHECKPOINT_BRANCH
    )
    repo = normalize_github_repository(repository)

    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return runner(argv, workdir)

    def _verify_work_packet_issue(number: int) -> bool:
        viewed = _run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,state,body",
            ]
        )
        if viewed.returncode != 0:
            detail = (viewed.stderr or viewed.stdout or "").strip()
            raise ValidationError(
                f"checkpoint issue #{number} verification unavailable: "
                + (detail[:400] or "gh issue view failed")
            )
        try:
            payload = json.loads(viewed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"checkpoint issue #{number} verification returned non-JSON"
            ) from exc
        if str(payload.get("state") or "").upper() != "OPEN":
            return False
        body = str(payload.get("body") or "")
        if not CHECKPOINT_WORKSTREAM_RE.search(body):
            return False
        if not re.search(r"(?m)^STATUS=ACTIVE\s*$", body):
            return False
        target = re.search(r"(?m)^TARGET_REPO=(\S+)\s*$", body)
        if target:
            try:
                if normalize_github_repository(target.group(1)) != repo:
                    return False
            except ValidationError:
                return False
        return True

    # 1) Explicit pointer file on control branch — validate before trust.
    pointer = _run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repo}/contents/{ACTIVE_CHECKPOINT_POINTER_PATH}?ref={branch}",
        ]
    )
    if pointer.returncode != 0:
        detail = f"{pointer.stderr or ''}\n{pointer.stdout or ''}"
        absent = "404" in detail or "Not Found" in detail
        if not absent:
            raise ValidationError(
                "ACTIVE_CHECKPOINT_ISSUE pointer unavailable: "
                + (detail.strip()[:400] or "contents GET failed")
            )
    else:
        try:
            payload = json.loads(pointer.stdout or "")
            encoded = str(payload.get("content") or "")
            raw = base64.b64decode(encoded, validate=False).decode("utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValidationError(
                "ACTIVE_CHECKPOINT_ISSUE pointer is malformed"
            ) from exc
        if not isinstance(data, dict):
            raise ValidationError("ACTIVE_CHECKPOINT_ISSUE pointer must be an object")
        workstream = str(data.get("workstream") or "").strip()
        if workstream != CHECKPOINT_WORKSTREAM:
            # Stale/wrong pointer: fall through to verified Issue search.
            pass
        else:
            try:
                number = int(data.get("issue_number"))
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    "ACTIVE_CHECKPOINT_ISSUE issue_number invalid"
                ) from exc
            if number < 1:
                raise ValidationError("ACTIVE_CHECKPOINT_ISSUE issue_number invalid")
            if _verify_work_packet_issue(number):
                return number
            # Pointer exists but referenced Issue is stale/closed/wrong.

    # 2) Authoritative absence only: server-side workstream search. A full
    # result page is not proof the Issue is missing.
    listed = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            f'"WORKSTREAM={CHECKPOINT_WORKSTREAM}" in:body',
            "--json",
            "number,title,body",
            "--limit",
            str(CHECKPOINT_DISCOVERY_SEARCH_LIMIT),
        ]
    )
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()
        raise ValidationError(
            "checkpoint discovery lookup unavailable: "
            + (detail[:400] or "gh issue list failed")
        )
    try:
        items = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "checkpoint discovery lookup returned non-JSON"
        ) from exc
    if not isinstance(items, list):
        raise ValidationError("checkpoint discovery lookup returned non-list JSON")
    if len(items) >= CHECKPOINT_DISCOVERY_SEARCH_LIMIT:
        raise ValidationError(
            "checkpoint discovery lookup truncated; pass --checkpoint-issue "
            "explicitly"
        )
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and CHECKPOINT_WORKSTREAM_RE.search(str(item.get("body") or ""))
        and re.search(r"(?m)^STATUS=ACTIVE\s*$", str(item.get("body") or ""))
    ]
    # TARGET_REPO is an exclusion filter. An explicit other-repo candidate is
    # never restored. Legacy bodies without TARGET_REPO are used only when no
    # explicit match for this repository exists.
    explicit_matches = []
    legacy = []
    for item in matches:
        body = str(item.get("body") or "")
        target = re.search(r"(?m)^TARGET_REPO=(\S+)\s*$", body)
        if target is None:
            legacy.append(item)
            continue
        try:
            if normalize_github_repository(target.group(1)) == repo:
                explicit_matches.append(item)
        except ValidationError:
            continue
    matches = explicit_matches or legacy
    if len(matches) == 1:
        return int(matches[0]["number"])
    if len(matches) > 1:
        raise ValidationError(
            "multiple ACTIVE continuous-chat-audit work packets; pass "
            "--checkpoint-issue explicitly"
        )
    raise ValidationError(
        "unable to discover chat-audit checkpoint issue; pass "
        "--checkpoint-issue or set ATLAS_CHAT_AUDIT_ISSUE, or publish "
        f"{ACTIVE_CHECKPOINT_POINTER_PATH} on {branch}"
    )



def publish_active_checkpoint_pointer(
    *,
    repository: str,
    issue_number: int,
    command_runner: CommandRunner | None = None,
    cwd: str | None = None,
    checkpoint_branch: str | None = None,
) -> dict[str, Any]:
    """Create/update the repo-managed ACTIVE_CHECKPOINT_ISSUE pointer."""
    store = GitHubContentsCheckpointStore(
        repository=repository,
        issue_number=issue_number,
        branch=checkpoint_branch,
        path=ACTIVE_CHECKPOINT_POINTER_PATH,
        command_runner=command_runner,
        cwd=cwd,
    )
    store.ensure_control_branch()
    # Load current pointer (may be absent) then CAS write.
    payload = store._get_contents()
    expected = None
    if payload is not None:
        expected = str(payload.get("sha") or "").strip() or None
    store._cas_loaded = True
    store._cas_blob_sha = expected
    body = {
        "issue_number": int(issue_number),
        "workstream": CHECKPOINT_WORKSTREAM,
    }
    # Bypass packet schema for pointer: write raw JSON via Contents PUT.
    raw = json.dumps(body, indent=2, sort_keys=True) + "\n"
    put_body: dict[str, Any] = {
        "message": f"atlas chat-audit active checkpoint pointer -> #{issue_number}",
        "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
        "branch": store.branch,
    }
    if expected:
        put_body["sha"] = expected
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        json.dump(put_body, handle)
        tmp = handle.name
    try:
        completed = store._run(
            [
                "gh",
                "api",
                "--method",
                "PUT",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{store.repository}/contents/{ACTIVE_CHECKPOINT_POINTER_PATH}",
                "--input",
                tmp,
            ]
        )
    finally:
        Path(tmp).unlink(missing_ok=True)
    if completed.returncode == 0:
        return {
            "issue_number": int(issue_number),
            "path": ACTIVE_CHECKPOINT_POINTER_PATH,
        }
    if not _is_contents_conflict(completed):
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValidationError(detail[:500] or "failed to publish checkpoint pointer")
    # Conflict: success only if canonical pointer already matches request.
    reloaded = store._get_contents()
    if reloaded is None:
        raise CheckpointCasConflict(
            "ACTIVE_CHECKPOINT_ISSUE pointer conflict and canonical pointer absent"
        )
    encoded = str(reloaded.get("content") or "")
    try:
        current = json.loads(
            base64.b64decode(encoded, validate=False).decode("utf-8")
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "ACTIVE_CHECKPOINT_ISSUE pointer conflict with unreadable canonical value"
        ) from exc
    if not isinstance(current, dict):
        raise ValidationError(
            "ACTIVE_CHECKPOINT_ISSUE pointer conflict with non-object canonical value"
        )
    current_issue = current.get("issue_number")
    current_ws = str(current.get("workstream") or "").strip()
    if (
        int(current_issue) == int(issue_number)
        and current_ws == CHECKPOINT_WORKSTREAM
    ):
        return {
            "issue_number": int(issue_number),
            "path": ACTIVE_CHECKPOINT_POINTER_PATH,
            "already_matched": True,
        }
    raise CheckpointCasConflict(
        "ACTIVE_CHECKPOINT_ISSUE pointer conflict: canonical "
        f"issue_number={current_issue!r} workstream={current_ws!r} "
        f"does not match requested #{issue_number}"
    )


def resolve_checkpoint_store(
    *,
    data_root: Path,
    repository: str | None,
    checkpoint_issue: int | None,
    require_github: bool = True,
    checkpoint_branch: str | None = None,
) -> CheckpointStore:
    """Prefer GitHub Contents CAS store; local file is cache when configured.

    Offline mode never selects or writes the GitHub canonical checkpoint.
    An explicit issue or ``ATLAS_CHAT_AUDIT_ISSUE`` in that mode fails closed.
    """
    if not require_github:
        env_issue = os.environ.get("ATLAS_CHAT_AUDIT_ISSUE", "").strip()
        if checkpoint_issue is not None or env_issue:
            raise ValidationError(
                "offline mode cannot select a GitHub canonical checkpoint "
                "(--checkpoint-issue or ATLAS_CHAT_AUDIT_ISSUE)"
            )
        return FileCheckpointStore(data_root)
    issue = checkpoint_issue
    if issue is None:
        env = os.environ.get("ATLAS_CHAT_AUDIT_ISSUE", "").strip()
        if env:
            issue = int(env)
    cache = FileCheckpointStore(data_root)
    if issue is None and require_github and repository:
        issue = discover_checkpoint_issue(
            repository=repository, checkpoint_branch=checkpoint_branch
        )
    if issue is not None:
        if not repository:
            raise ValidationError(
                "repository is required for GitHub-backed audit checkpoint"
            )
        return GitHubContentsCheckpointStore(
            repository=repository,
            issue_number=issue,
            branch=checkpoint_branch,
            cache=cache,
        )
    if require_github:
        raise ValidationError(
            "GitHub-backed Audit Control Packet required: pass "
            "--checkpoint-issue or set ATLAS_CHAT_AUDIT_ISSUE, or publish "
            "ACTIVE_CHECKPOINT_ISSUE.json / an ACTIVE workstream Issue "
            "(local chat-audit.json is cache/test-only)"
        )
    return cache
