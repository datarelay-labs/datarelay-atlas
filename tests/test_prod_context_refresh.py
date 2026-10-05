from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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


def target_fixture():
    return {
        "repositories": [
            "datarelay-labs/datarelay-product-foundation",
            "datarelay-labs/datarelay-atlas",
        ],
        "projects": [
            {
                "project_id": "datarelay-product-foundation",
                "repository": "datarelay-labs/datarelay-product-foundation",
                "sources": [
                    {
                        "source_id": "roadmap",
                        "source_path": "docs/ROADMAP.md",
                        "ref": "main",
                    },
                    {
                        "source_id": "readme",
                        "source_path": "README.md",
                        "ref": "main",
                    },
                ],
            },
            {
                "project_id": "datarelay-atlas",
                "repository": "datarelay-labs/datarelay-atlas",
                "sources": [
                    {
                        "source_id": "product-charter",
                        "source_path": "docs/product/PRODUCT-CHARTER.md",
                        "ref": "main",
                    }
                ],
            },
        ],
    }


class ProdContextRefreshTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_normalize_targets_sorts_projects_sources_and_repositories(self):
        targets = self.module.normalize_targets(target_fixture())
        self.assertEqual(
            targets["repositories"],
            [
                "datarelay-labs/datarelay-atlas",
                "datarelay-labs/datarelay-product-foundation",
            ],
        )
        self.assertEqual(
            [item["project_id"] for item in targets["projects"]],
            ["datarelay-atlas", "datarelay-product-foundation"],
        )
        foundation = targets["projects"][1]
        self.assertEqual(
            [item["source_id"] for item in foundation["sources"]],
            ["readme", "roadmap"],
        )

    def test_normalize_targets_rejects_duplicate_invalid_or_incomplete_identity(self):
        duplicate = target_fixture()
        duplicate["repositories"] = ["datarelay-labs/datarelay-atlas"] * 2
        with self.assertRaises(self.module.RefreshError):
            self.module.normalize_targets(duplicate)

        invalid = target_fixture()
        invalid["projects"][0]["project_id"] = "../../bad"
        with self.assertRaises(self.module.RefreshError):
            self.module.normalize_targets(invalid)

        missing_repo = target_fixture()
        missing_repo["repositories"] = ["datarelay-labs/datarelay-atlas"]
        with self.assertRaises(self.module.RefreshError):
            self.module.normalize_targets(missing_repo)

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
        self.assertIn("source.provider != GITHUB_PROVIDER", text)
        self.assertIn("project.repository", text)
        self.assertIn("source.source_path", text)
        self.assertIn("source.ref or project.default_ref", text)
        self.assertNotIn("datarelay-product-foundation", text)
        self.assertNotIn("datarelay-grant", text)

    def test_build_source_payload_fetches_on_operator_and_never_serializes_token(self):
        targets = self.module.normalize_targets(target_fixture())
        token = "operator-token-must-not-cross"

        def fetcher(source, supplied_token):
            self.assertEqual(supplied_token, token)
            return SimpleNamespace(
                content=f"# {source.project_id}/{source.source_id}\n",
                source_revision="a" * 40,
            )

        encoded = self.module.build_source_payload(
            targets,
            token=token,
            fetcher=fetcher,
        )
        self.assertNotIn(token, encoded)
        payload = json.loads(encoded)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["targets"], targets)
        self.assertEqual(len(payload["sources"]), 3)
        self.assertEqual(
            {item["repository"] for item in payload["sources"]},
            {
                "datarelay-labs/datarelay-atlas",
                "datarelay-labs/datarelay-product-foundation",
            },
        )

    def test_build_source_payload_rejects_bad_revision_and_oversize(self):
        targets = self.module.normalize_targets(
            {
                "repositories": ["datarelay-labs/datarelay-atlas"],
                "projects": [
                    {
                        "project_id": "datarelay-atlas",
                        "repository": "datarelay-labs/datarelay-atlas",
                        "sources": [
                            {
                                "source_id": "readme",
                                "source_path": "README.md",
                                "ref": "main",
                            }
                        ],
                    }
                ],
            }
        )

        def bad_revision(_source, _token):
            return SimpleNamespace(content="safe", source_revision="not-a-sha")

        with self.assertRaises(self.module.RefreshError):
            self.module.build_source_payload(
                targets,
                token="token",
                fetcher=bad_revision,
            )

        def too_large(_source, _token):
            return SimpleNamespace(
                content="x" * (self.module.MAX_SOURCE_CONTENT_BYTES + 1),
                source_revision="b" * 40,
            )

        with self.assertRaises(self.module.RefreshError):
            self.module.build_source_payload(
                targets,
                token="token",
                fetcher=too_large,
            )

    def test_remote_sync_revalidates_identity_uses_existing_writer_and_has_no_github_token(self):
        text = self.module._REMOTE_SYNC
        self.assertIn('os.environ.pop("GITHUB_TOKEN", None)', text)
        self.assertIn('if current != payload["targets"]', text)
        self.assertIn("service.sync_project", text)
        self.assertIn("FetchedSource", text)
        self.assertIn("operator source payload source identity changed", text)
        self.assertIn("operator-mediated project sync failed", text)
        command = self.module._remote_sync_command()
        self.assertIn("env -u GITHUB_TOKEN", command)
        self.assertIn("/opt/datarelay-atlas/.venv/bin/python", command)

    def test_remote_publish_is_lifecycle_only(self):
        text = self.module._remote_publish_script("/tmp/snapshot.json")
        self.assertIn("lifecycle publish-github-snapshot", text)
        self.assertNotIn(" sync ", text)
        self.assertNotIn("GITHUB_TOKEN", text)

    def test_canonical_systemd_and_installer_contract(self):
        service = (ROOT / "deploy/systemd/atlas-prod-refresh.service").read_text()
        timer = (ROOT / "deploy/systemd/atlas-prod-refresh.timer").read_text()
        installer = (
            ROOT / "scripts/install-prod-context-refresh-systemd.sh"
        ).read_text()
        self.assertIn(
            "ExecStart=/usr/local/lib/datarelay-atlas/prod-context-refresh.py",
            service,
        )
        self.assertIn("OnUnitActiveSec=15min", timer)
        self.assertIn(
            "/var/backups/datarelay-atlas-operator/prod-refresh",
            installer,
        )
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
        self.assertIn(
            "Environment=ATLAS_OPERATOR_REPO=/usr/local/lib/datarelay-atlas/operator-src",
            service,
        )
        self.assertIn(
            "WorkingDirectory=/usr/local/lib/datarelay-atlas/operator-src",
            service,
        )
        self.assertIn('git -C "$ROOT" archive --format=tar "$SOURCE_HEAD" atlas', installer)
        self.assertIn("printf '%s\\n' \"$SOURCE_HEAD\" > \"$RUNTIME_DST/REVISION\"", installer)
        self.assertIn('chown -R root:root "$RUNTIME_DST"', installer)

    def test_scripts_parse(self):
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(SCRIPT)],
            check=True,
            cwd=ROOT,
        )
        subprocess.run(
            [
                "bash",
                "-n",
                str(ROOT / "scripts/install-prod-context-refresh-systemd.sh"),
            ],
            check=True,
            cwd=ROOT,
        )
