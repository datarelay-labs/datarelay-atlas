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
    sanitize_finding,
    sanitize_packet_for_persistence,
)
from atlas.provenance import ValidationError
from atlas.secrets import sanitize_durable_text
from atlas.work_controller import normalize_github_repository

DEFAULT_CHECKPOINT_BRANCH = "atlas/chat-audit-control"
HANDOFF_MARKER = "<!-- atlas-chat-audit-finding-id:"
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

    def _put_contents(
        self,
        *,
        packet: AuditControlPacket,
        expected_sha: str | None,
    ) -> str:
        raw = json.dumps(packet.to_dict(), indent=2, sort_keys=True) + "\n"
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
        if completed.returncode != 0:
            if _is_contents_conflict(completed):
                raise CheckpointCasConflict(
                    "checkpoint compare-and-set failed: contents blob SHA "
                    "conflict (stale or concurrent writer)"
                )
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500] or "gh api contents PUT failed for audit checkpoint"
            )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError("gh api contents PUT returned non-JSON") from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        new_sha = None
        if isinstance(content, dict):
            new_sha = content.get("sha")
        if not new_sha and isinstance(payload, dict):
            new_sha = payload.get("sha")
        if not isinstance(new_sha, str) or not new_sha.strip():
            # Deterministic fallback when API omits sha in mocked/minimal replies.
            new_sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return new_sha.strip()

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
            # Concurrent bootstrap: treat already-exists as success.
            if "Reference already exists" in detail or "422" in detail:
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
        if self.cache is not None:
            self.cache.save(packet)
        return packet

    def save(self, packet: AuditControlPacket) -> None:
        """Persist via Contents API PUT bound to the loaded blob SHA.

        Does not perform a client-side re-read/check before write: concurrency
        safety comes from GitHub rejecting a stale expected ``sha``.
        """
        if not self._cas_loaded:
            # Require an explicit load (or prior successful save) so writers bind
            # to a server-observed CAS token rather than inventing one.
            raise ValidationError(
                "checkpoint save requires a prior load to bind Contents CAS sha"
            )
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
        if self.cache is not None:
            self.cache.save(safe)


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
        if completed.returncode != 0:
            if _is_contents_conflict(completed):
                raise CheckpointCasConflict(
                    "handoff claim compare-and-set failed: concurrent creator"
                )
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(detail[:500] or "handoff claim PUT failed")
        payload = json.loads(completed.stdout or "{}")
        content = payload.get("content") if isinstance(payload, dict) else None
        new_sha = None
        if isinstance(content, dict):
            new_sha = content.get("sha")
        if not new_sha and isinstance(payload, dict):
            new_sha = payload.get("sha")
        if not isinstance(new_sha, str) or not new_sha.strip():
            new_sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return new_sha.strip()

    def upsert_implementation_packet(
        self, packet: AuditControlPacket, finding: AuditFinding
    ) -> dict[str, Any]:
        safe = sanitize_finding(finding)
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
            number = int(claim["issue_number"])
            self._edit_issue(number, title=title, body=body)
            record = {
                "action": "updated",
                "issue_number": number,
                "title": title,
                "repository": self.repository,
                "finding": safe.to_dict(),
                "url": claim.get("url"),
                "claim": "existing",
            }
            self.handoffs.append(record)
            return record

        owner_token = str(uuid.uuid4())
        pending = {
            "finding_id": safe.finding_id,
            "owner_token": owner_token,
            "issue_number": None,
            "url": None,
            "run_key": packet.idempotency_run_key,
            "head": packet.current_target_sha,
        }
        try:
            claim_sha = self._put_claim(
                safe.finding_id, pending, expected_sha=claim_sha
            )
            claim = pending
        except CheckpointCasConflict:
            import time

            claim = None
            for _ in range(20):
                claim, claim_sha = self._get_claim(safe.finding_id)
                if claim and claim.get("issue_number"):
                    break
                time.sleep(0.01)
            if claim and claim.get("issue_number"):
                number = int(claim["issue_number"])
                self._edit_issue(number, title=title, body=body)
                record = {
                    "action": "updated",
                    "issue_number": number,
                    "title": title,
                    "repository": self.repository,
                    "finding": safe.to_dict(),
                    "url": claim.get("url"),
                    "claim": "lost_create_race",
                }
                self.handoffs.append(record)
                return record
            raise ValidationError(
                "handoff claim held by concurrent writer without issue_number; "
                "retry required"
            )

        # Only the claim owner may create the GitHub issue.
        number, url = self._create_issue(title=title, body=body)
        claim = {
            **claim,
            "issue_number": number,
            "url": url,
            "owner_token": owner_token,
        }
        self._put_claim(safe.finding_id, claim, expected_sha=claim_sha)
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
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ValidationError(
                detail[:500]
                or "gh issue create failed; refusing local-only finding handoff"
            )
        url = (completed.stdout or "").strip().splitlines()[-1].strip()
        match = re.search(r"/issues/(\d+)\s*$", url)
        if not match:
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
        reasons: list[str] = []
        evidence: dict[str, Any] = {
            "collector": "atlas.chat_audit_github.GitHubCoordinationRefresher",
            "target_sha": packet.current_target_sha,
        }
        issue = self.work_packet_issue
        if issue is None:
            discovered = discover_checkpoint_issue(
                repository=self.repository, command_runner=self._runner, cwd=self._cwd
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
                evidence["work_packet"] = {
                    "status": status,
                    "state": payload.get("state"),
                    "number": payload.get("number"),
                    "updated_at": payload.get("updatedAt"),
                }
                if status in {"PAUSED", "BLOCKED"}:
                    reasons.append(f"work_packet_{status.lower()}")
                if str(payload.get("state") or "").upper() not in {"OPEN", ""}:
                    reasons.append("work_packet_issue_not_open")
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
                # Branch has an open PR but not at exact audited HEAD.
                reasons.append("pr_head_mismatch")
                evidence["pr"] = {"status": "MISMATCH", "candidates": items}
            elif chosen is None:
                evidence["pr"] = {"status": "ABSENT"}
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
                if checks.returncode == 0:
                    evidence["ci"] = {"status": "OK"}
                elif checks.returncode == 8:
                    evidence["ci"] = {"status": "PENDING"}
                    reasons.append("ci_pending")
                else:
                    evidence["ci"] = {
                        "status": "FAIL",
                        "exit_code": checks.returncode,
                    }
                    reasons.append("ci_fail")
                reviews = self._run(
                    [
                        "gh",
                        "api",
                        "-H",
                        "Accept: application/vnd.github+json",
                        f"repos/{self.repository}/pulls/{chosen.get('number')}/reviews",
                    ]
                )
                actionable = False
                if reviews.returncode == 0:
                    try:
                        review_items = json.loads(reviews.stdout or "[]")
                    except json.JSONDecodeError:
                        review_items = []
                        reasons.append("reviews_unparseable")
                    for item in review_items if isinstance(review_items, list) else []:
                        state = str(item.get("state") or "").upper()
                        body = str(item.get("body") or "")
                        if state in {"CHANGES_REQUESTED", "DISMISSED"}:
                            actionable = True
                        if re.search(r"\bP[012]\b", body) or "REWORK" in body.upper():
                            actionable = True
                    evidence["reviews"] = {
                        "status": "OK",
                        "actionable": actionable,
                        "count": len(review_items)
                        if isinstance(review_items, list)
                        else 0,
                    }
                    if actionable:
                        reasons.append("actionable_review")
                else:
                    evidence["reviews"] = {"status": "ERROR"}
                    reasons.append("reviews_unavailable")
        else:
            evidence["pr"] = {"status": "ERROR"}
            reasons.append("pr_list_failed")

        if reasons:
            evidence["status"] = "HUMAN_REQUIRED"
            evidence["outcome"] = "HUMAN_REQUIRED"
            evidence["reasons"] = reasons
        else:
            evidence["status"] = "OK"
            evidence["outcome"] = "PASSED"
            evidence["reasons"] = []
        return evidence


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

    # 1) Explicit pointer file on control branch.
    pointer = _run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repo}/contents/{ACTIVE_CHECKPOINT_POINTER_PATH}?ref={branch}",
        ]
    )
    if pointer.returncode == 0:
        payload = json.loads(pointer.stdout)
        encoded = str(payload.get("content") or "")
        raw = base64.b64decode(encoded, validate=False).decode("utf-8")
        data = json.loads(raw)
        number = int(data.get("issue_number"))
        if number < 1:
            raise ValidationError("ACTIVE_CHECKPOINT_ISSUE issue_number invalid")
        return number

    # 2) Open [AI Work] issue with matching WORKSTREAM marker.
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
            'in:title "[AI Work]"',
            "--json",
            "number,title,body",
            "--limit",
            "50",
        ]
    )
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()
        raise ValidationError(
            detail[:500] or "failed to discover chat-audit checkpoint issue"
        )
    items = json.loads(listed.stdout or "[]")
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and CHECKPOINT_WORKSTREAM_RE.search(str(item.get("body") or ""))
        and re.search(r"(?m)^STATUS=ACTIVE\s*$", str(item.get("body") or ""))
    ]
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
        "workstream": "continuous-chat-audit-supervisor-poc",
    }
    raw_packet = AuditControlPacket(
        target_repository=normalize_github_repository(repository),
        target_branch="atlas/chat-audit-control",
        current_target_sha="0" * 40,
        audit_queue=[
            "changed_code",
            "affected_contracts",
            "affected_tests_ci",
            "security_impact",
            "docs_spec_drift",
        ],
        idempotency_run_key=hashlib.sha256(
            f"pointer|{issue_number}".encode()
        ).hexdigest()[:24],
        canonical_revision=0 if expected is None else 1,
        next_action="pointer",
    )
    # Bypass packet schema for pointer: write raw JSON via _put_contents helper
    # by temporarily swapping serialization — use direct PUT instead.
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
    if completed.returncode != 0 and not _is_contents_conflict(completed):
        # Conflict is acceptable if pointer already matches.
        detail = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode != 0:
            raise ValidationError(detail[:500] or "failed to publish checkpoint pointer")
    return {"issue_number": int(issue_number), "path": ACTIVE_CHECKPOINT_POINTER_PATH}


def resolve_checkpoint_store(
    *,
    data_root: Path,
    repository: str | None,
    checkpoint_issue: int | None,
    require_github: bool = True,
    checkpoint_branch: str | None = None,
) -> CheckpointStore:
    """Prefer GitHub Contents CAS store; local file is cache when configured."""
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
