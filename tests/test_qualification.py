"""Qualification harness regressions. No production credentials or release-flag changes."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from atlas.github_sync import FetchedSource
from atlas.qualification import (
    PIN_BASELINE,
    PIN_VERSION,
    PROD_ENDPOINT,
    PROD_PROJECT_ID,
    PROD_QUERY,
    PROD_REPOSITORY,
    PROD_SOURCE_ID,
    PROD_SOURCE_PATH,
    _engineering_system_pin,
    _pin_reason,
    main,
    run_operational_e2e,
    run_public_smoke,
)
from atlas.service import AtlasService

ROOT = Path(__file__).resolve().parents[1]
PROD_URL = "https://mcp.atlas.datarelay.run"
PIN = "14150e424c922ff3a930b45dcf31d3a3d3ba28b2"
STALE_HEAD = "b" * 40


class QualificationTests(unittest.TestCase):
    def test_pin_reader_accepts_current_project_yaml_and_fails_closed(self):
        profile = (ROOT / ".engineering" / "project.yaml").read_text(encoding="utf-8")
        self.assertIn("\n- methodology\n", profile)
        later_list = profile + "\nqualification_probe:\n- later-top-level-list\n"
        self.assertEqual(_engineering_system_pin(profile), (PIN_VERSION, PIN_BASELINE))
        self.assertEqual(_engineering_system_pin(later_list), (PIN_VERSION, PIN_BASELINE))
        self.assertIsNone(
            _engineering_system_pin(
                profile + "\nengineering_system:\n  version: 1.6.5\n  baseline: " + PIN_BASELINE + "\n"
            )
        )
        self.assertIsNone(_pin_reason(ROOT))
        smoke = run_public_smoke({}, repo_root=ROOT)
        self.assertIn("ATLAS_PUBLIC_BASE_URL", smoke["reason"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_profile(root, profile.replace("version: 1.6.5", "version: 1.6.4", 1))
            mismatched = run_public_smoke({}, repo_root=root)
            self.assertEqual(mismatched["status"], "FAIL_CLOSED")
            self.assertIn("14150e424c922ff3a930b45dcf31d3a3d3ba28b2", mismatched["reason"])
            self.assertNotIn("ATLAS_PUBLIC_BASE_URL", mismatched["reason"])

            self._write_profile(root, "engineering_system:\n  version: 1.6.5\n")
            missing_baseline = run_public_smoke({}, repo_root=root)
            self.assertEqual(missing_baseline["reason"], "engineering system pin is unreadable")

            self._write_profile(root, "engineering_system: 1.6.5\ndomains:\n- methodology\n")
            scalar = run_public_smoke({}, repo_root=root)
            self.assertEqual(scalar["reason"], "engineering system pin is unreadable")

            self._write_profile(
                root,
                profile.replace(
                    "baseline: 14150e424c922ff3a930b45dcf31d3a3d3ba28b2",
                    "baseline: 0000000000000000000000000000000000000000",
                    1,
                ),
            )
            wrong_baseline = _pin_reason(root)
            self.assertIsNotNone(wrong_baseline)
            self.assertIn("not v1.6.5 baseline", wrong_baseline or "")

    def test_local_journey_passes_without_a_production_claim(self):
        evidence = run_operational_e2e("local", {}, repo_root=ROOT)
        self.assertEqual(evidence["status"], "PASS")
        self.assertFalse(evidence["production_claim"])
        self.assertEqual(evidence["mode"], "local-deterministic")
        self.assertEqual(evidence["engineering_system"]["baseline"], PIN)
        names = [step["name"] for step in evidence["steps"]]
        self.assertEqual(
            names,
            [
                "register_sync",
                "retrieval",
                "mcp_query",
                "restart_recovery",
                "backup_restore",
                "upgrade_rollback",
            ],
        )
        rendered = json.dumps(evidence)
        self.assertNotIn("qualification-marker", rendered)
        self._assert_release_flags_unchanged()

    def test_public_smoke_fails_closed_without_an_https_url(self):
        missing = run_public_smoke({}, repo_root=ROOT)
        self.assertEqual(missing["status"], "FAIL_CLOSED")
        self.assertFalse(missing["production_claim"])
        self.assertIn("ATLAS_PUBLIC_BASE_URL", missing["reason"])

        insecure = run_public_smoke(
            {"ATLAS_PUBLIC_BASE_URL": "http://127.0.0.1/"},
            repo_root=ROOT,
        )
        self.assertEqual(insecure["status"], "FAIL_CLOSED")
        self.assertEqual(insecure["reason"], "public url is not https")

        wrong_host = run_public_smoke(
            {
                "ATLAS_PUBLIC_BASE_URL": "https://127.0.0.1/",
                "ATLAS_QUALIFICATION_CONFIRM_PROD": "yes",
            },
            repo_root=ROOT,
        )
        self.assertEqual(wrong_host["status"], "FAIL_CLOSED")
        self.assertFalse(wrong_host["production_claim"])
        self.assertIn("prod-atlas endpoint", wrong_host["reason"])

    def test_public_smoke_checks_local_https_without_claiming_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            _write_cert(cert, key)
            port = _free_port()
            server = _serve(port, cert, key)
            try:
                evidence = run_public_smoke(
                    {
                        "ATLAS_PUBLIC_BASE_URL": f"https://127.0.0.1:{port}/",
                        "ATLAS_PUBLIC_SMOKE_CA_FILE": str(cert),
                    },
                    repo_root=ROOT,
                )
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(evidence["status"], "PASS")
        self.assertFalse(evidence["production_claim"])
        self.assertEqual(
            [step["status"] for step in evidence["steps"]],
            ["PASS", "PASS"],
        )
        self.assertNotIn("BEGIN CERTIFICATE", json.dumps(evidence))

    def test_prod_mode_fails_closed_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            backup = root / "backup"
            evidence = run_operational_e2e(
                "prod",
                {
                    "ATLAS_PUBLIC_BASE_URL": PROD_URL,
                    "ATLAS_DATA_ROOT": str(data),
                    "ATLAS_BACKUP_DEST": str(backup),
                    "ATLAS_RESTORE_PROOF_DEST": str(root / "restore"),
                    "ATLAS_ROLLBACK_TARGET": str(ROOT),
                    "ATLAS_QUALIFICATION_RESTART_COMMAND": "python3 -c pass",
                    "ATLAS_CURSOR_MCP_EVIDENCE": str(root / "missing.json"),
                },
                repo_root=ROOT,
            )
            self.assertEqual(evidence["status"], "FAIL_CLOSED")
            self.assertFalse(evidence["production_claim"])
            self.assertIn("ATLAS_QUALIFICATION_CONFIRM_PROD", evidence["reason"])
            self.assertFalse(backup.exists())

    def test_prod_mode_rejects_unbound_cursor_evidence_before_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            evidence_path = root / "cursor.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "client": "cursor",
                        "status": "PASS",
                        "host": "mcp.atlas.datarelay.run",
                        "production_claim": True,
                        "tools": ["search_project", "get_provenance"],
                    }
                ),
                encoding="utf-8",
            )
            backup = root / "backup"
            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, "python3 -c pass"),
                repo_root=ROOT,
            )
            self.assertEqual(evidence["status"], "FAIL_CLOSED")
            self.assertFalse(evidence["production_claim"])
            self.assertIn("bound", evidence["reason"])
            self.assertFalse(backup.exists())
            self.assertFalse((data / "registry.json").exists())

    def test_prod_mode_rejects_secret_cursor_evidence_before_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            evidence_path = root / "cursor.json"
            evidence_path.write_text(
                json.dumps({**_bound_cursor("rev-prod-bind-1"), "token": "super-secret-token"}),
                encoding="utf-8",
            )
            backup = root / "backup"
            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, "python3 -c pass"),
                repo_root=ROOT,
            )
            self.assertEqual(evidence["status"], "FAIL_CLOSED")
            self.assertIn("secret", evidence["reason"])
            rendered = json.dumps(evidence)
            self.assertNotIn("super-secret-token", rendered)
            self.assertNotIn("qual-github-credential", rendered)
            self.assertFalse(backup.exists())
            self.assertFalse((data / "registry.json").exists())

    def test_prod_mode_fails_closed_on_conflicting_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            AtlasService(data).register_project(
                project_id=PROD_PROJECT_ID,
                repository="datarelay-labs/other",
            )
            evidence_path = root / "cursor.json"
            evidence_path.write_text(json.dumps(_bound_cursor("rev-prod-bind-1")), encoding="utf-8")
            backup = root / "backup"
            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, "python3 -c pass"),
                repo_root=ROOT,
                smoke_client=_ready_smoke(),
            )
            self.assertEqual(evidence["status"], "FAIL_CLOSED")
            self.assertEqual(evidence["reason"], "conflicting project registration")
            self.assertFalse(evidence["production_claim"])
            self.assertFalse(backup.exists())
            shown = AtlasService(data).show_project(PROD_PROJECT_ID)
            self.assertEqual(shown["project"]["repository"], "datarelay-labs/other")

    def test_prod_mode_fails_closed_on_conflicting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            service = AtlasService(data)
            service.register_project(project_id=PROD_PROJECT_ID, repository=PROD_REPOSITORY)
            service.add_source(
                PROD_PROJECT_ID,
                source_id=PROD_SOURCE_ID,
                source_path="docs/other.md",
            )
            evidence_path = root / "cursor.json"
            evidence_path.write_text(json.dumps(_bound_cursor("rev-prod-bind-1")), encoding="utf-8")
            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, "python3 -c pass"),
                repo_root=ROOT,
                smoke_client=_ready_smoke(),
            )
            self.assertEqual(evidence["status"], "FAIL_CLOSED")
            self.assertEqual(evidence["reason"], "conflicting source registration")
            paths = [
                source.source_path
                for source in AtlasService(data).list_sources(PROD_PROJECT_ID)
            ]
            self.assertEqual(paths, ["docs/other.md"])
            self.assertFalse((root / "backup").exists())

    def test_prod_journey_binds_sync_retrieval_and_cursor_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            revision = "rev-prod-bind-1"
            evidence_path = root / "cursor.json"
            evidence_path.write_text(json.dumps(_bound_cursor(revision)), encoding="utf-8")
            command, identity_command = _changing_restart(root)
            seen: list[str | None] = []

            def fetch(source, token):
                seen.append(token)
                self.assertEqual(source.repository, PROD_REPOSITORY)
                self.assertEqual(source.source_path, PROD_SOURCE_PATH)
                return FetchedSource(
                    content=PROD_QUERY + "\n",
                    source_revision=revision,
                )

            evidence = run_operational_e2e(
                "prod",
                _prod_env(
                    root,
                    data,
                    evidence_path,
                    command,
                    identity_command=identity_command,
                ),
                repo_root=ROOT,
                smoke_client=_ready_smoke(),
                fetch=fetch,
            )
            self.assertEqual(evidence["status"], "PASS", evidence)
            self.assertTrue(evidence["production_claim"])
            self.assertEqual(seen, ["qual-github-credential"])
            self.assertEqual((root / "service-identity").read_text(encoding="utf-8"), "boot-2\n")
            names = [step["name"] for step in evidence["steps"]]
            self.assertIn("register_source", names)
            self.assertIn("sync", names)
            self.assertIn("retrieval", names)
            self.assertIn("cursor_mcp", names)
            self.assertIn("retrieval_after_restart", names)
            retrieval = next(step for step in evidence["steps"] if step["name"] == "retrieval")
            recovered = next(
                step for step in evidence["steps"] if step["name"] == "retrieval_after_restart"
            )
            self.assertEqual(retrieval["source_revision"], revision)
            self.assertEqual(retrieval["identity"], f"{PROD_SOURCE_ID}@main")
            self.assertEqual(recovered["source_revision"], revision)
            self.assertEqual(recovered["identity"], retrieval["identity"])
            rendered = json.dumps(evidence)
            self.assertNotIn("qual-github-credential", rendered)
            stored = (data / "projections" / "projections.json").read_text(encoding="utf-8")
            self.assertNotIn("qual-github-credential", stored)
            shown = AtlasService(data).show_project(PROD_PROJECT_ID)
            self.assertEqual(shown["project"]["repository"], PROD_REPOSITORY)
        self._assert_release_flags_unchanged()

    def test_prod_sync_leaves_a_failing_unrelated_source_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            service = AtlasService(data)
            service.register_project(project_id=PROD_PROJECT_ID, repository=PROD_REPOSITORY)
            service.add_source(
                PROD_PROJECT_ID,
                source_id="notes",
                source_path="docs/notes.md",
                title="Notes",
            )
            notes = next(
                source
                for source in service.registry.canonical_sources(PROD_PROJECT_ID)
                if source.source_id == "notes"
            )
            service.projections.sync_one(
                notes,
                fetch=lambda source, token: FetchedSource(  # noqa: ARG005
                    content="notes-prior-marker\n",
                    source_revision="rev-notes-prior",
                ),
            )
            notes_path = data / "projections" / PROD_PROJECT_ID / "notes.md"
            before_bytes = notes_path.read_bytes()
            before_meta = next(
                record
                for record in service.projection_records(PROD_PROJECT_ID)
                if record["source_id"] == "notes"
            )
            revision = "rev-prod-bind-1"
            evidence_path = root / "cursor.json"
            evidence_path.write_text(json.dumps(_bound_cursor(revision)), encoding="utf-8")
            command, identity_command = _changing_restart(root)
            seen: list[str] = []

            def fetch(source, token):  # noqa: ARG001
                seen.append(source.source_id)
                if source.source_id != PROD_SOURCE_ID:
                    raise RuntimeError(f"unrelated source fetched {token}")
                return FetchedSource(content=PROD_QUERY + "\n", source_revision=revision)

            evidence = run_operational_e2e(
                "prod",
                _prod_env(
                    root,
                    data,
                    evidence_path,
                    command,
                    identity_command=identity_command,
                ),
                repo_root=ROOT,
                smoke_client=_ready_smoke(),
                fetch=fetch,
            )
            self.assertEqual(evidence["status"], "PASS", evidence)
            self.assertEqual(seen, [PROD_SOURCE_ID])
            self.assertEqual(notes_path.read_bytes(), before_bytes)
            after_meta = next(
                record
                for record in AtlasService(data).projection_records(PROD_PROJECT_ID)
                if record["source_id"] == "notes"
            )
            self.assertEqual(after_meta["source_revision"], "rev-notes-prior")
            self.assertEqual(after_meta["sync_state"], before_meta["sync_state"])
            self.assertEqual(after_meta["content_digest"], before_meta["content_digest"])
            hits = AtlasService(data).search(PROD_PROJECT_ID, "notes-prior-marker", limit=4)
            self.assertEqual(hits[0].provenance["source_revision"], "rev-notes-prior")
            self.assertNotIn("qual-github-credential", json.dumps(evidence))

    def test_prod_restart_rejects_a_noop_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            evidence_path = root / "cursor.json"
            evidence_path.write_text(
                json.dumps(_bound_cursor("rev-prod-bind-1")),
                encoding="utf-8",
            )
            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, f"{sys.executable} -c pass"),
                repo_root=ROOT,
                smoke_client=_ready_smoke(),
                fetch=lambda source, token: FetchedSource(  # noqa: ARG005
                    content=PROD_QUERY + "\n",
                    source_revision="rev-prod-bind-1",
                ),
            )
            self.assertEqual(evidence["status"], "FAIL")
            self.assertFalse(evidence["production_claim"])
            self.assertEqual(evidence["reason"], "restart did not change service identity")
            self.assertNotIn("boot-constant", json.dumps(evidence))

    def test_stale_deployed_head_fails_closed_before_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            evidence_path = root / "cursor.json"
            stale = _bound_cursor("rev-prod-bind-1")
            stale["code_head"] = STALE_HEAD
            evidence_path.write_text(json.dumps(stale), encoding="utf-8")
            env = _prod_env(root, data, evidence_path, f"{sys.executable} -c pass")
            env["ATLAS_QUALIFICATION_DEPLOYED_HEAD"] = STALE_HEAD
            evidence = run_operational_e2e(
                "prod",
                env,
                repo_root=ROOT,
            )
            self.assertEqual(evidence["status"], "FAIL_CLOSED")
            self.assertFalse(evidence["production_claim"])
            self.assertIn("deployed code head", evidence["reason"])
            self.assertFalse((data / "registry.json").exists())
            self.assertNotIn(STALE_HEAD, json.dumps(evidence))

    def test_checkout_without_head_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            engineering = root / ".engineering"
            engineering.mkdir()
            (engineering / "project.yaml").write_text(
                (ROOT / ".engineering" / "project.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            evidence_path = root / "cursor.json"
            evidence_path.write_text(
                json.dumps(_bound_cursor("rev-prod-bind-1")),
                encoding="utf-8",
            )
            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, f"{sys.executable} -c pass"),
                repo_root=root,
            )
            self.assertEqual(evidence["status"], "FAIL_CLOSED")
            self.assertEqual(evidence["reason"], "deployed code head is unavailable")
            self.assertFalse((data / "registry.json").exists())

    def test_cryptography_is_a_direct_bounded_dependency(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("cryptography>=46.0.0,<52\n", text)

    def test_prod_cursor_evidence_must_match_the_synced_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            evidence_path = root / "cursor.json"
            evidence_path.write_text(json.dumps(_bound_cursor("rev-other")), encoding="utf-8")

            def fetch(source, token):  # noqa: ARG001
                return FetchedSource(content=PROD_QUERY + "\n", source_revision="rev-prod-bind-1")

            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, "python3 -c pass"),
                repo_root=ROOT,
                smoke_client=_ready_smoke(),
                fetch=fetch,
            )
            self.assertEqual(evidence["status"], "FAIL")
            self.assertFalse(evidence["production_claim"])
            self.assertIn("synced revision", evidence["reason"])
            self.assertFalse((root / "backup").exists())
            self.assertNotIn("qual-github-credential", json.dumps(evidence))

    def test_prod_sync_failure_does_not_store_the_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            evidence_path = root / "cursor.json"
            evidence_path.write_text(json.dumps(_bound_cursor("rev-prod-bind-1")), encoding="utf-8")

            def fetch(source, token):  # noqa: ARG001
                raise RuntimeError(f"authorization failed for {token}")

            evidence = run_operational_e2e(
                "prod",
                _prod_env(root, data, evidence_path, "python3 -c pass"),
                repo_root=ROOT,
                smoke_client=_ready_smoke(),
                fetch=fetch,
            )
            self.assertEqual(evidence["status"], "FAIL")
            self.assertFalse(evidence["production_claim"])
            self.assertNotIn("qual-github-credential", json.dumps(evidence))
            stored = (data / "projections" / "projections.json").read_text(encoding="utf-8")
            self.assertNotIn("qual-github-credential", stored)
            self.assertFalse((root / "backup").exists())

    def test_cli_local_mode_exits_zero_and_prints_no_production_claim(self):
        from io import StringIO
        from unittest.mock import patch

        buffer = StringIO()
        with patch("sys.stdout", buffer):
            code = main(["operational-e2e", "--mode", "local"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["status"], "PASS")
        self.assertFalse(payload["production_claim"])

    def _write_profile(self, root: Path, text: str) -> None:
        engineering = root / ".engineering"
        engineering.mkdir(parents=True, exist_ok=True)
        (engineering / "project.yaml").write_text(text, encoding="utf-8")

    def _assert_release_flags_unchanged(self) -> None:
        project = (ROOT / ".engineering" / "project.yaml").read_text(encoding="utf-8")
        release = (ROOT / ".engineering" / "release.yaml").read_text(encoding="utf-8")
        self.assertIn("production_oriented: false", project)
        self.assertIn(f"baseline: {PIN}", project)
        self.assertIn("public_smoke_required: false", release)
        self.assertIn("operational_e2e_required: false", release)
        self.assertIn("full_e2e_passes: 0", release)
        self.assertIn("public_smoke_command: ''", release)
        self.assertIn("operational_e2e_command: ''", release)


def _prod_env(
    root: Path,
    data: Path,
    evidence: Path,
    command: str,
    *,
    identity_command: str | None = None,
) -> dict[str, str]:
    token = root / "github-token"
    token.write_text("qual-github-credential\n", encoding="utf-8")
    os.chmod(token, 0o600)
    if identity_command is None:
        identity_command = f"{sys.executable} -c \"print('boot-constant')\""
    return {
        "ATLAS_QUALIFICATION_CONFIRM_PROD": "yes",
        "ATLAS_PUBLIC_BASE_URL": PROD_URL,
        "ATLAS_DATA_ROOT": str(data),
        "ATLAS_BACKUP_DEST": str(root / "backup"),
        "ATLAS_RESTORE_PROOF_DEST": str(root / "restore"),
        "ATLAS_ROLLBACK_TARGET": str(ROOT),
        "ATLAS_QUALIFICATION_RESTART_COMMAND": command,
        "ATLAS_QUALIFICATION_RESTART_IDENTITY_COMMAND": identity_command,
        "ATLAS_CURSOR_MCP_EVIDENCE": str(evidence),
        "ATLAS_QUALIFICATION_GITHUB_TOKEN_FILE": str(token),
    }


def _changing_restart(root: Path) -> tuple[str, str]:
    identity = root / "service-identity"
    identity.write_text("boot-1\n", encoding="utf-8")
    marker = root / "restarted"
    identity_command = f"{sys.executable} -c \"print(open({str(identity)!r}).read().strip())\""
    command = (
        f"{sys.executable} -c \"[open({str(identity)!r}, 'w').write('boot-2\\n'), "
        f"open({str(marker)!r}, 'w').write('ok')]\""
    )
    return command, identity_command


def _checkout_head(repo: Path = ROOT) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    head = completed.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise AssertionError("checkout head is not a full sha")
    return head


def _bound_cursor(revision: str) -> dict[str, object]:
    return {
        "client": "cursor",
        "status": "PASS",
        "endpoint": PROD_ENDPOINT,
        "project_id": PROD_PROJECT_ID,
        "query": PROD_QUERY,
        "identity": f"{PROD_SOURCE_ID}@main",
        "source_revision": revision,
        "code_head": _checkout_head(),
        "repository": PROD_REPOSITORY,
        "source_path": PROD_SOURCE_PATH,
        "tools": ["search_project", "get_provenance"],
    }


def _ready_smoke():
    def smoke(method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
        del method, body
        if url.endswith("/healthz"):
            return 200, b'{"status":"ready"}'
        if url.endswith("/mcp"):
            return 401, b""
        return 404, b""

    return smoke


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_cert(cert_path: Path, key_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"status":"ready"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if self.path.split("?", 1)[0] != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(401)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        return


def _serve(port: int, cert: Path, key: Path) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    unittest.main()
