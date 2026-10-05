from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prod-context-refresh.py"


def load_module():
    spec = importlib.util.spec_from_file_location("prod_context_refresh", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProdContextRefreshTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_normalize_targets_sorts_and_keeps_complete_set(self):
        targets = self.module.normalize_targets(
            {
                "repositories": [
                    "datarelay-labs/datarelay-product-foundation",
                    "datarelay-labs/datarelay-atlas",
                ],
                "project_ids": ["datarelay-product-foundation", "datarelay-atlas"],
            }
        )
        self.assertEqual(
            targets,
            {
                "repositories": [
                    "datarelay-labs/datarelay-atlas",
                    "datarelay-labs/datarelay-product-foundation",
                ],
                "project_ids": ["datarelay-atlas", "datarelay-product-foundation"],
            },
        )

    def test_normalize_targets_rejects_duplicate_or_invalid_identity(self):
        with self.assertRaises(self.module.RefreshError):
            self.module.normalize_targets(
                {
                    "repositories": ["datarelay-labs/datarelay-atlas"] * 2,
                    "project_ids": ["datarelay-atlas"],
                }
            )
        with self.assertRaises(self.module.RefreshError):
            self.module.normalize_targets(
                {
                    "repositories": ["datarelay-labs/datarelay-atlas"],
                    "project_ids": ["../../bad"],
                }
            )

    def test_snapshot_command_includes_every_repository_once(self):
        command = self.module.snapshot_command(
            atlas_python=Path("/opt/python"),
            repositories=[
                "datarelay-labs/datarelay-atlas",
                "datarelay-labs/datarelay-product-foundation",
            ],
        )
        self.assertEqual(
            command,
            [
                "/opt/python",
                "-m",
                "atlas",
                "usage",
                "github-snapshot",
                "--repository",
                "datarelay-labs/datarelay-atlas",
                "--repository",
                "datarelay-labs/datarelay-product-foundation",
            ],
        )

    def test_remote_target_discovery_is_provider_aware_not_project_allowlisted(self):
        text = self.module._REMOTE_TARGETS
        self.assertIn("source.provider == GITHUB_PROVIDER", text)
        self.assertIn("project.repository", text)
        self.assertNotIn("datarelay-product-foundation", text)
        self.assertNotIn("datarelay-grant", text)

    def test_remote_apply_revalidates_targets_and_drops_github_token(self):
        expected = {
            "repositories": ["datarelay-labs/datarelay-atlas"],
            "project_ids": ["datarelay-atlas"],
        }
        text = self.module._remote_apply_script("/tmp/snapshot.json", expected)
        self.assertIn("current != expected", text)
        self.assertIn('env.pop("GITHUB_TOKEN", None)', text)
        self.assertIn("publish-github-snapshot", text)
        self.assertIn("SYNCED_PROJECT=", text)

    def test_canonical_systemd_and_installer_contract(self):
        service = (ROOT / "deploy/systemd/atlas-prod-refresh.service").read_text()
        timer = (ROOT / "deploy/systemd/atlas-prod-refresh.timer").read_text()
        installer = (ROOT / "scripts/install-prod-context-refresh-systemd.sh").read_text()
        self.assertIn("ExecStart=/usr/local/lib/datarelay-atlas/prod-context-refresh.py", service)
        self.assertIn("OnUnitActiveSec=15min", timer)
        self.assertIn("/var/backups/datarelay-atlas-operator/prod-refresh", installer)
        self.assertIn("prod-context-refresh.py", installer)
        self.assertLess(
            installer.index("systemctl stop atlas-prod-refresh.timer"),
            installer.index('install -o root -g root -m 0755 "$SCRIPT_SRC"'),
        )
        self.assertLess(
            installer.index("systemctl stop atlas-prod-refresh.service"),
            installer.index('install -o root -g root -m 0755 "$SCRIPT_SRC"'),
        )
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", service)

    def test_scripts_parse(self):
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(SCRIPT)],
            check=True,
            cwd=ROOT,
        )
        subprocess.run(
            ["bash", "-n", str(ROOT / "scripts/install-prod-context-refresh-systemd.sh")],
            check=True,
            cwd=ROOT,
        )
