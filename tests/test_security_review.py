from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.data_protection import backup_data_root
from atlas.mcp_context import AtlasContextTools, default_read_scopes
from atlas.provenance import ValidationError
from atlas.registry import ProjectRegistry
from atlas.security_review import (
    FILENAME,
    MAX_EVIDENCE_BYTES,
    REQUIRED_CONTROL_IDS,
    build_security_review_evidence_from_facts,
    derive_review_outcome,
    load_security_review_evidence,
    publish_security_review_evidence,
    security_review_dashboard,
    validate_security_review_evidence,
)
from atlas.service import AtlasService
from atlas.sbom import _source_facts
from atlas.web_ui import render_operations


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"
HEAD_A = "a" * 40
HEAD_B = "b" * 40

CONTROL_REFS = {
    "MCP_AUTHORIZATION_SCOPE": "tests:test_security_mcp",
    "MCP_TOKEN_ISSUER_RESOURCE_BINDING": "tests:test_mcp_http",
    "TLS_AND_SECRET_FILE_PERMISSIONS": "tests:test_ops",
    "DEPLOYMENT_INGRESS_HARDENING": "contract:deploy-systemd",
    "DATA_ROOT_PATH_AND_SYMLINK_SAFETY": "tests:test_data_protection",
    "BACKUP_RESTORE_FAIL_CLOSED": "tests:test_data_protection.restore",
    "CONTENT_FREE_ERROR_AND_EVIDENCE_BOUNDARY": "tests:test_runtime_observability",
}


def _controls(outcome: str = "PASS") -> list[dict[str, object]]:
    return [
        {
            "control_id": control_id,
            "outcome": outcome,
            "evidence_ref": CONTROL_REFS[control_id] if outcome != "UNKNOWN" else None,
        }
        for control_id in REQUIRED_CONTROL_IDS
    ]


def _evidence(
    *,
    head: str = HEAD_A,
    controls: list[dict[str, object]] | None = None,
    findings: list[dict[str, object]] | None = None,
    reviewer_reference: str = "github:issue-221",
) -> dict[str, object]:
    return build_security_review_evidence_from_facts(
        source_facts={
            "repository": "datarelay-labs/datarelay-atlas",
            "source_revision": head,
            "clean": True,
        },
        reviewed_at="2026-10-01T00:00:00Z",
        reviewer={"kind": "CHATGPT_CHAT", "reference": reviewer_reference},
        controls=controls if controls is not None else _controls(),
        findings=findings if findings is not None else [],
    )


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _make_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "atlas-test@example.invalid"], repo)
    _git(["config", "user.name", "Atlas Test"], repo)
    (repo / "README.md").write_text("security review fixture\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "fixture"], repo)
    _git(
        [
            "remote",
            "add",
            "origin",
            "https://github.com/datarelay-labs/datarelay-atlas.git",
        ],
        repo,
    )
    return repo


class SecurityReviewEvidenceTests(unittest.TestCase):
    def test_public_schema_fixture_and_runtime_parity(self) -> None:
        schema = json.loads(
            (CONTRACTS / "atlas-security-review-evidence.schema.json").read_text()
        )
        fixture = json.loads(
            (FIXTURES / "atlas-security-review-evidence.example.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(fixture)
        validated = validate_security_review_evidence(
            fixture,
            require_current_source=False,
        )
        self.assertEqual(validated, fixture)
        self.assertEqual(derive_review_outcome(validated), "NO_BLOCKING_FINDINGS")
        self.assertEqual(validated["authority"], "EVIDENCE_ONLY")

    def test_outcome_is_derived_without_granting_authority(self) -> None:
        good = _evidence()
        self.assertEqual(derive_review_outcome(good), "NO_BLOCKING_FINDINGS")

        unknown_controls = _controls()
        unknown_controls[0] = {
            "control_id": REQUIRED_CONTROL_IDS[0],
            "outcome": "UNKNOWN",
            "evidence_ref": None,
        }
        incomplete = _evidence(controls=unknown_controls)
        self.assertEqual(derive_review_outcome(incomplete), "INCOMPLETE")
        self.assertEqual(incomplete["authority"], "EVIDENCE_ONLY")

        failed_controls = _controls()
        failed_controls[1] = {
            "control_id": REQUIRED_CONTROL_IDS[1],
            "outcome": "FAIL",
            "evidence_ref": CONTROL_REFS[REQUIRED_CONTROL_IDS[1]],
        }
        blocked = _evidence(controls=failed_controls)
        self.assertEqual(derive_review_outcome(blocked), "BLOCKING_FINDINGS")

        high_finding = _evidence(
            findings=[
                {
                    "finding_id": "SEC-001",
                    "severity": "HIGH",
                    "disposition": "OPEN",
                    "evidence_ref": "github:issue-999",
                }
            ]
        )
        self.assertEqual(derive_review_outcome(high_finding), "BLOCKING_FINDINGS")

    def test_tamper_and_source_mismatch_fail_closed(self) -> None:
        evidence = _evidence()
        tampered = deepcopy(evidence)
        tampered["reviewed_at"] = "2026-10-01T00:00:01Z"
        with self.assertRaisesRegex(ValidationError, "digest mismatch"):
            validate_security_review_evidence(
                tampered,
                require_current_source=False,
            )

        with self.assertRaisesRegex(ValidationError, "does not match current source"):
            validate_security_review_evidence(
                evidence,
                source_facts={
                    "repository": "datarelay-labs/datarelay-atlas",
                    "source_revision": HEAD_B,
                    "clean": True,
                },
            )
        with self.assertRaisesRegex(ValidationError, "unavailable or dirty"):
            validate_security_review_evidence(
                evidence,
                source_facts={
                    "repository": "datarelay-labs/datarelay-atlas",
                    "source_revision": HEAD_A,
                    "clean": False,
                },
            )

    def test_secret_shaped_reference_is_rejected_without_echo(self) -> None:
        secret = "sk-" + ("A" * 24)
        with self.assertRaises(ValidationError) as caught:
            _evidence(reviewer_reference=secret)
        self.assertNotIn(secret, str(caught.exception))

    def test_duplicate_key_and_oversized_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":1,"schema_version":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
                load_security_review_evidence(
                    duplicate,
                    require_current_source=False,
                )

            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (MAX_EVIDENCE_BYTES + 1))
            with self.assertRaisesRegex(ValidationError, "bounded input size"):
                load_security_review_evidence(
                    oversized,
                    require_current_source=False,
                )

    def test_cli_validate_and_publish_bind_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            data_root.mkdir()
            evidence = _evidence()
            evidence_path = root / "review.json"
            evidence_path.write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current = {
                "repository": "datarelay-labs/datarelay-atlas",
                "source_revision": HEAD_A,
                "clean": True,
            }
            with patch("atlas.security_review._source_facts", return_value=current):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(
                        [
                            "--data-root",
                            str(data_root),
                            "ops",
                            "security-review",
                            "validate",
                            "--evidence",
                            str(evidence_path),
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(stderr.getvalue(), "")
                validated = json.loads(stdout.getvalue())
                self.assertEqual(validated["state"], "VALIDATED_EVIDENCE")
                self.assertEqual(validated["authority"], "EVIDENCE_ONLY")
                self.assertEqual(
                    validated["review_outcome"],
                    "NO_BLOCKING_FINDINGS",
                )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = main(
                        [
                            "--data-root",
                            str(data_root),
                            "ops",
                            "security-review",
                            "publish",
                            "--evidence",
                            str(evidence_path),
                        ]
                    )
                self.assertEqual(code, 0)
                published = json.loads(stdout.getvalue())
                self.assertEqual(published["state"], "VALIDATED_EVIDENCE")
                self.assertEqual(
                    published["evidence_digest"],
                    evidence["evidence_digest"],
                )
                self.assertTrue((data_root / FILENAME).is_file())

    def test_publish_dashboard_and_backup_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_repo(base)
            source = _source_facts(repo, require_clean=True)
            evidence = build_security_review_evidence_from_facts(
                source_facts=source,
                reviewed_at="2026-10-01T00:00:00Z",
                reviewer={"kind": "CODEX", "reference": "audit:security-1"},
                controls=_controls(),
                findings=[],
            )
            evidence_path = base / "review.json"
            evidence_path.write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            data_root = base / "data"
            data_root.mkdir()
            ProjectRegistry(data_root).register(
                project_id="demo",
                repository="datarelay-labs/demo",
            )

            published = publish_security_review_evidence(
                data_root,
                evidence_path,
                repo_root=repo,
            )
            shown = security_review_dashboard(data_root, repo_root=repo)
            self.assertEqual(published, shown)
            self.assertEqual(shown["state"], "VALIDATED_EVIDENCE")
            self.assertEqual(shown["review_outcome"], "NO_BLOCKING_FINDINGS")
            self.assertEqual(shown["authority"], "EVIDENCE_ONLY")
            self.assertEqual(shown["reviewer_attribution"], "DECLARED_ONLY")

            backup = base / "backup"
            backup_data_root(data_root, backup)
            self.assertFalse(any(path.name == FILENAME for path in backup.rglob("*")))

    def test_stale_or_unsafe_published_evidence_is_not_presented_as_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            (root / FILENAME).symlink_to(root / "missing-review")
            shown = security_review_dashboard(root, repo_root=ROOT)
            self.assertEqual(shown["state"], "INVALID_OR_STALE_EVIDENCE")
            self.assertEqual(shown["review_outcome"], "UNKNOWN")
            self.assertEqual(shown["authority"], "EVIDENCE_ONLY")

    def test_symlinked_data_root_never_surfaces_valid_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_repo(base)
            source = _source_facts(repo, require_clean=True)
            evidence = build_security_review_evidence_from_facts(
                source_facts=source,
                reviewed_at="2026-10-01T00:00:00Z",
                reviewer={"kind": "CODEX", "reference": "audit:security-root"},
                controls=_controls(),
                findings=[],
            )
            target = base / "target-data"
            target.mkdir()
            (target / FILENAME).write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            linked = base / "linked-data"
            linked.symlink_to(target, target_is_directory=True)

            shown = security_review_dashboard(linked, repo_root=repo)
            self.assertEqual(shown["state"], "INVALID_OR_STALE_EVIDENCE")
            self.assertEqual(shown["review_outcome"], "UNKNOWN")
            self.assertEqual(shown["authority"], "EVIDENCE_ONLY")

    def test_cli_show_on_absent_evidence_is_bounded_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["--data-root", tmp, "ops", "security-review", "show"])
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["state"], "UNKNOWN")
            self.assertEqual(payload["review_outcome"], "UNKNOWN")
            self.assertEqual(payload["authority"], "EVIDENCE_ONLY")

    def test_operations_web_and_mcp_surface_same_review_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = {
                "state": "VALIDATED_EVIDENCE",
                "review_outcome": "NO_BLOCKING_FINDINGS",
                "authority": "EVIDENCE_ONLY",
                "repository": "datarelay-labs/datarelay-atlas",
                "source_revision": HEAD_A,
                "reviewed_at": "2026-10-01T00:00:00Z",
                "reviewer": {"kind": "CODEX", "reference": "audit:security-1"},
                "scope_version": 1,
                "control_counts": {"PASS": 7, "FAIL": 0, "UNKNOWN": 0},
                "finding_counts": {
                    "CRITICAL": 0,
                    "HIGH": 0,
                    "MEDIUM": 0,
                    "LOW": 0,
                },
                "finding_count": 0,
                "evidence_digest": "c" * 64,
                "detail": (
                    "exact-source bounded security review evidence validates; "
                    "this evidence does not grant release, deploy, merge, or PASS authority"
                ),
            }
            service = AtlasService(root)
            with patch(
                "atlas.operations_readiness.security_review_dashboard",
                return_value=review,
            ):
                readiness = service.operations_readiness()
                self.assertEqual(readiness["security_review"], review)
                self.assertEqual(
                    readiness["release_readiness"]["state"],
                    "NOT_CLAIMED",
                )

                response = render_operations(service)
                html = response.body.decode("utf-8")
                self.assertEqual(response.status, "200 OK")
                self.assertIn("VALIDATED_EVIDENCE", html)
                self.assertIn("NO_BLOCKING_FINDINGS", html)
                self.assertIn("c" * 64, html)

                tools = AtlasContextTools(
                    retriever_factory=service.project_retriever,
                    operations_readiness_factory=service.operations_readiness,
                )
                result = tools.call(
                    "get_operations_readiness",
                    {},
                    scopes=default_read_scopes(),
                )
                self.assertTrue(result.ok)
                self.assertEqual(result.data["security_review"], review)


if __name__ == "__main__":
    unittest.main()
