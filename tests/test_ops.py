"""Service configuration and health regressions. No systemd and no root."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from atlas.cli import main
from atlas.ops import assess_service_environment, data_root_runtime_ready, stage_unit
from atlas.provenance import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SECRET = "ops-introspection-secret"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o750)


def _write_env(directory: Path, *, secret: str = SECRET, extra: str = "") -> Path:
    data = directory / "data"
    _private_dir(data)
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    secret_file = directory / "introspection-client-secret"
    cert.write_text("cert\n", encoding="utf-8")
    key.write_text("key\n", encoding="utf-8")
    secret_file.write_text(secret + "\n", encoding="utf-8")
    os.chmod(cert, 0o644)
    os.chmod(key, 0o640)
    os.chmod(secret_file, 0o640)
    env = directory / "service.env"
    env.write_text(
        "\n".join(
            [
                f"ATLAS_DATA_ROOT={data}",
                "ATLAS_MCP_BIND_HOST=127.0.0.1",
                "ATLAS_MCP_BIND_PORT=8443",
                "ATLAS_MCP_RESOURCE_URL=https://127.0.0.1:8443/mcp",
                "ATLAS_MCP_ISSUER_URL=https://issuer.example",
                "ATLAS_MCP_INTROSPECTION_URL=https://issuer.example/oauth/introspect",
                "ATLAS_MCP_INTROSPECTION_CLIENT_ID=atlas-resource",
                f"ATLAS_MCP_INTROSPECTION_CLIENT_SECRET_FILE={secret_file}",
                f"ATLAS_MCP_TLS_CERT={cert}",
                f"ATLAS_MCP_TLS_KEY={key}",
                extra,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(env, 0o640)
    return env


class OpsCheckTests(unittest.TestCase):
    def test_ready_report_omits_secret_and_ignores_ambient_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _write_env(Path(tmp))
            report = assess_service_environment(
                {"ATLAS_MCP_INTROSPECTION_CLIENT_SECRET": "ambient-secret-value"},
                env_file=env,
            )
            encoded = json.dumps(report)
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["github_sync_credential"], "absent")
            self.assertEqual(report["semantic"], "disabled")
            self.assertNotIn(SECRET, encoded)
            self.assertNotIn("ambient-secret-value", encoded)
            self.assertNotIn(str(env), encoded)

    def test_missing_and_world_readable_secret_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _write_env(root)
            text = env.read_text(encoding="utf-8")
            text = text.replace(
                "ATLAS_MCP_INTROSPECTION_CLIENT_SECRET_FILE=",
                "ATLAS_MCP_INTROSPECTION_CLIENT_SECRET_FILE_DISABLED=",
            )
            text += f"ATLAS_MCP_INTROSPECTION_CLIENT_SECRET={SECRET}\n"
            env.write_text(text, encoding="utf-8")
            os.chmod(env, 0o644)
            report = assess_service_environment({}, env_file=env)
            encoded = json.dumps(report)
            self.assertEqual(report["status"], "not_ready")
            self.assertIn("env_file_permissions", report["invalid"])
            self.assertIn("ATLAS_MCP_INTROSPECTION_CLIENT_SECRET_FILE_DISABLED", report["unknown_keys"])
            self.assertNotIn(SECRET, encoded)

    def test_world_readable_tls_key_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _write_env(root)
            os.chmod(root / "key.pem", 0o644)
            report = assess_service_environment({}, env_file=env)
            self.assertEqual(report["status"], "not_ready")
            self.assertIn("tls_key_permissions", report["invalid"])
            self.assertNotIn(str(root / "key.pem"), json.dumps(report))

    def test_relative_data_root_and_partial_semantic_config_are_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _write_env(root)
            text = env.read_text(encoding="utf-8").replace(str(root / "data"), "relative-data")
            text += "ATLAS_EMBEDDING_ENDPOINT=https://embeddings.example\n"
            env.write_text(text, encoding="utf-8")
            os.chmod(env, 0o640)
            report = assess_service_environment({}, env_file=env)
            self.assertEqual(report["status"], "not_ready")
            self.assertIn("data_root", report["invalid"])
            self.assertIn("semantic_configuration", report["invalid"])
            self.assertEqual(report["semantic"], "configured")

    def test_unsupported_registry_is_not_runtime_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _write_env(root)
            data = root / "data"
            (data / "registry.json").write_text(
                '{"schema_version": 2, "projects": {}}\n',
                encoding="utf-8",
            )
            self.assertFalse(data_root_runtime_ready(data))
            report = assess_service_environment({}, env_file=env)
            self.assertEqual(report["status"], "not_ready")
            self.assertIn("runtime_not_ready", report["invalid"])
            self.assertNotIn("schema_version", json.dumps(report))

    def test_cli_check_reports_github_credential_without_echoing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _write_env(Path(tmp), extra=f"GITHUB_TOKEN={SECRET}\n")
            stdout = StringIO()
            with patch("sys.stdout", stdout):
                code = main(["ops", "check", "--env-file", str(env)])
            self.assertEqual(code, 0)
            self.assertNotIn(SECRET, stdout.getvalue())
            self.assertIn('"github_sync_credential": "present"', stdout.getvalue())

    def test_staged_unit_is_non_root_and_has_no_backup_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = stage_unit(Path(tmp))
            text = target.read_text(encoding="utf-8")
            self.assertIn("User=atlas\n", text)
            self.assertNotIn("User=root", text)
            self.assertIn("NoNewPrivileges=true\n", text)
            self.assertIn("python -m atlas mcp serve\n", text)
            self.assertNotIn("backup", text)
            self.assertNotIn("rollback", text)
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode, 0o644)

    def test_backup_and_production_profile_remain_out_of_scope(self):
        project = (ROOT / ".engineering" / "project.yaml").read_text(encoding="utf-8")
        self.assertIn("production_oriented: false", project)
        self.assertIn("runbook_required: false", project)
        self.assertIn("incident_response_required: false", project)
        self.assertIn(
            'backup_command: cp -a "${ATLAS_DATA_ROOT:-.atlas-data}"',
            project,
        )
        self.assertIn("upgrade_command: ''", project)
        self.assertIn("rollback_command: ''", project)
        self.assertIn("atlas ops check --env-file", project)
        with self.assertRaises(SystemExit):
            main(["ops", "backup"])

    def test_missing_env_file_fails_closed(self):
        with self.assertRaises(ValidationError):
            assess_service_environment({}, env_file=Path("/tmp/atlas-missing-service.env"))


if __name__ == "__main__":
    unittest.main()
