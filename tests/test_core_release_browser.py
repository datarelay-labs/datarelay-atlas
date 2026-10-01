from __future__ import annotations

import inspect
import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from atlas.core_release_browser import (
    AUTHORITY,
    MISSION_IDS,
    RELEASE_AUTHORITY,
    REQUEST_KIND,
    RESULT_KIND,
    SURFACE_IDS,
    _compose_core_release_result,
    fixture_contract,
    prepare_core_release_browser_fixture,
    request_from_manifest,
    run_core_release_browser,
    validate_core_release_browser_request,
    validate_core_release_browser_result,
    verify_exact_clean_candidate,
)
from atlas.provenance import ValidationError
from atlas.service import AtlasService

ROOT = Path(__file__).resolve().parents[1]
MARKER = "ATLAS_CORE_RELEASE_BROWSER_RESULT="
def make_request(head: str) -> dict:
    return {
        "schema_version": 1,
        "kind": REQUEST_KIND,
        "run_id": "core-release-browser-1",
        "source_revision": head,
        "work_packet_issue": 245,
        "targets": {
            "primary": "http://127.0.0.1:18788/",
            "empty": "http://127.0.0.1:18789/",
            "corrupt": "http://127.0.0.1:18790/",
        },
        "fixture": fixture_contract(),
        "required_surfaces": list(SURFACE_IDS),
        "required_missions": list(MISSION_IDS),
    }


def raw_pass() -> dict:
    return {
        "result": "PASS",
        "browser_engine": "chromium",
        "browser_version": "153.0",
        "actual_browser_process": True,
        "duration_ms": 1000,
        "surfaces": [
            {
                "surface_id": surface_id,
                "outcome": "PASS",
                "http_status": 200,
                "detail": f"{surface_id} reconciled in Chromium.",
            }
            for surface_id in SURFACE_IDS
        ],
        "missions": [
            {
                "mission_id": mission_id,
                "outcome": "PASS",
                "detail": f"{mission_id} passed in Chromium.",
            }
            for mission_id in MISSION_IDS
        ],
        "cleanup": {"context_closed": True, "browser_closed": True},
        "trace_ref": "evidence/browser-verification/core-release-browser-1/core-release.zip",
        "screenshot_ref": None,
        "error_code": None,
        "detail": "Core release Surface Reconciliation and Full User E2E passed.",
    }


def command_result(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["node"],
        0,
        stdout=MARKER + json.dumps(payload) + "\n",
        stderr="",
    )


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()
def make_git_candidate(tmp: str) -> tuple[Path, str]:
    root = Path(tmp) / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "atlas-tests@example.invalid")
    git(root, "config", "user.name", "Atlas Tests")
    runner = root / "tools" / "browser-verification"
    runner.mkdir(parents=True)
    (runner / "core-release-runner.mjs").write_text(
        "// test runner placeholder\n",
        encoding="utf-8",
    )
    (root / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "candidate")
    return root, git(root, "rev-parse", "HEAD")


class CoreReleaseBrowserTests(unittest.TestCase):
    def test_request_requires_loopback_distinct_targets_and_complete_inventories(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head = make_git_candidate(tmp)
            request = make_request(head)
            self.assertEqual(
                validate_core_release_browser_request(request),
                request,
            )
            cases = []
            bad = deepcopy(request)
            bad["targets"]["primary"] = "https://example.com/"
            cases.append(bad)
            bad = deepcopy(request)
            bad["targets"]["empty"] = bad["targets"]["primary"]
            cases.append(bad)
            bad = deepcopy(request)
            bad["required_surfaces"] = bad["required_surfaces"][:-1]
            cases.append(bad)
            bad = deepcopy(request)
            bad["required_missions"] = bad["required_missions"][:-1]
            cases.append(bad)
            bad = deepcopy(request)
            bad["source_revision"] = "short"
            cases.append(bad)
            for payload in cases:
                with self.subTest(payload=payload):
                    with self.assertRaises(ValidationError):
                        validate_core_release_browser_request(payload)

    def test_prepare_fixture_reuses_core_contract_and_separates_personal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "browser-fixture"
            manifest = prepare_core_release_browser_fixture(base)
            self.assertEqual(manifest["fixture"], fixture_contract())
            primary = AtlasService(Path(manifest["roots"]["primary"]))
            search = primary.search_across_projects(
                fixture_contract()["engineering_query"],
                project_ids=[
                    fixture_contract()["primary_project_id"],
                    fixture_contract()["peer_project_id"],
                ],
                source_class="engineering",
                limit_per_project=4,
            )
            self.assertEqual(
                {group["project_id"] for group in search["groups"]},
                {"core-alpha", "core-beta"},
            )
            isolated = primary.search_across_projects(
                fixture_contract()["isolated_query"],
                project_ids=[
                    fixture_contract()["primary_project_id"],
                    fixture_contract()["peer_project_id"],
                ],
                source_class="engineering",
                limit_per_project=4,
            )
            self.assertEqual(
                {
                    group["project_id"]
                    for group in isolated["groups"]
                    if group["hits"]
                },
                {"core-alpha"},
            )
            personal = primary.personal_search(
                fixture_contract()["personal_project_id"],
                fixture_contract()["personal_query"],
                limit=4,
            )
            self.assertTrue(personal)
            self.assertTrue(
                all(
                    hit.provenance.get("source_class") == "personal"
                    and hit.provenance.get("engineering_authority") is False
                    for hit in personal
                )
            )
            self.assertEqual(
                (base / "corrupt" / "projections" / "projections.json").read_text(),
                "{broken",
            )
    def test_exact_candidate_gate_rejects_dirty_and_different_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head = make_git_candidate(tmp)
            verify_exact_clean_candidate(repo, head)
            with self.assertRaisesRegex(ValidationError, "differs"):
                verify_exact_clean_candidate(repo, "f" * 40)
            (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "dirty"):
                verify_exact_clean_candidate(repo, head)

    def test_exact_candidate_gate_rejects_hidden_index_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head = make_git_candidate(tmp)
            git(repo, "update-index", "--skip-worktree", "tracked.txt")
            with self.assertRaisesRegex(ValidationError, "hidden index"):
                verify_exact_clean_candidate(repo, head)

    def test_complete_actual_browser_result_is_normalized_and_digest_bound(self):
        head = "4" * 40
        normalized = validate_core_release_browser_request(make_request(head))
        result = _compose_core_release_result(
            normalized,
            raw_pass(),
            servers_stopped=True,
        )
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["authority"], AUTHORITY)
        self.assertEqual(result["release_authority"], RELEASE_AUTHORITY)
        self.assertTrue(result["browser"]["actual_process"])
        self.assertEqual(
            result["server_binding"],
            {
                "mode": "VERIFIED_CHECKOUT_SUBPROCESS",
                "candidate_head": head,
                "server_count": 3,
                "servers_stopped": True,
            },
        )
        self.assertEqual(
            [item["surface_id"] for item in result["surfaces"]],
            list(SURFACE_IDS),
        )
        self.assertEqual(
            [item["mission_id"] for item in result["missions"]],
            list(MISSION_IDS),
        )

        tampered = deepcopy(result)
        tampered["server_binding"]["candidate_head"] = "5" * 40
        with self.assertRaisesRegex(ValidationError, "server binding HEAD"):
            validate_core_release_browser_result(tampered)

        tampered = deepcopy(result)
        tampered["detail"] = "tampered"
        with self.assertRaisesRegex(ValidationError, "digest"):
            validate_core_release_browser_result(tampered)

    def test_synthetic_or_incomplete_pass_fails_closed(self):
        normalized = validate_core_release_browser_request(make_request("4" * 40))
        mutations = []
        missing_surface = raw_pass()
        missing_surface["surfaces"] = missing_surface["surfaces"][:-1]
        mutations.append(missing_surface)

        missing_mission = raw_pass()
        missing_mission["missions"] = missing_mission["missions"][:-1]
        mutations.append(missing_mission)

        synthetic = raw_pass()
        synthetic["actual_browser_process"] = False
        mutations.append(synthetic)

        no_cleanup = raw_pass()
        no_cleanup["cleanup"]["browser_closed"] = False
        mutations.append(no_cleanup)

        for raw in mutations:
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    _compose_core_release_result(
                        normalized,
                        raw,
                        servers_stopped=True,
                    )

        cleanup_failed = _compose_core_release_result(
            normalized,
            raw_pass(),
            servers_stopped=False,
        )
        self.assertEqual(cleanup_failed["result"], "FAIL")
        self.assertEqual(
            cleanup_failed["error_code"],
            "CANDIDATE_SERVER_CLEANUP_FAILED",
        )

    def test_candidate_failure_occurs_before_server_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head = make_git_candidate(tmp)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with patch(
                "atlas.core_release_browser._launch_candidate_servers"
            ) as launch:
                with self.assertRaisesRegex(ValidationError, "dirty"):
                    run_core_release_browser(
                        make_request(head),
                        repo_root=repo,
                        fixture_roots={},
                    )
                launch.assert_not_called()

    def test_request_from_manifest_binds_fixed_fixture_contract_and_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head = make_git_candidate(tmp)
            manifest = prepare_core_release_browser_fixture(
                Path(tmp) / "fixture",
                candidate_head=head,
            )
            request = request_from_manifest(
                manifest,
                run_id="core-release-browser-2",
                source_revision=head,
                work_packet_issue=245,
                primary_url="http://127.0.0.1:28788/",
                empty_url="http://127.0.0.1:28789/",
                corrupt_url="http://127.0.0.1:28790/",
            )
            self.assertEqual(request["fixture"], fixture_contract())

            bad = deepcopy(manifest)
            bad["fixture"]["primary_project_id"] = "other"
            with self.assertRaises(ValidationError):
                request_from_manifest(
                    bad,
                    run_id="core-release-browser-3",
                    source_revision=head,
                    work_packet_issue=245,
                    primary_url="http://127.0.0.1:28788/",
                    empty_url="http://127.0.0.1:28789/",
                    corrupt_url="http://127.0.0.1:28790/",
                )

            bad = deepcopy(manifest)
            bad["roots"]["empty"] = bad["roots"]["primary"]
            with self.assertRaisesRegex(ValidationError, "roots must be distinct"):
                request_from_manifest(
                    bad,
                    run_id="core-release-browser-4",
                    source_revision=head,
                    work_packet_issue=245,
                    primary_url="http://127.0.0.1:28788/",
                    empty_url="http://127.0.0.1:28789/",
                    corrupt_url="http://127.0.0.1:28790/",
                )
    def test_candidate_servers_are_bound_to_verified_checkout(self):
        source = (ROOT / "atlas" / "core_release_browser.py").read_text(
            encoding="utf-8"
        )
        signature = inspect.signature(run_core_release_browser)
        self.assertNotIn("command_runner", signature.parameters)
        self.assertIn("subprocess.Popen(", source)
        self.assertIn('"PYTHONPATH": str(repo_root)', source)
        self.assertIn('"VERIFIED_CHECKOUT_SUBPROCESS"', source)
        self.assertGreaterEqual(
            source.count("verify_exact_clean_candidate(root"),
            3,
        )

    def test_node_runner_reuses_hardened_loopback_evidence_boundary(self):
        source = (
            ROOT
            / "tools"
            / "browser-verification"
            / "core-release-runner.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("installLoopbackRequestGuard", source)
        self.assertIn("assertLoopbackUrl", source)
        self.assertIn("ensureEvidenceDir", source)
        self.assertIn("chromium.launch({ headless: true })", source)
        self.assertIn("context.tracing.start", source)
        self.assertIn("function contentSurface(", source)
        self.assertNotIn("function headingSurface(", source)
        self.assertIn("request.fixture.isolated_query", source)
        self.assertIn("freshContext = await browser.newContext()", source)
        self.assertNotIn("@browserbasehq/stagehand", source)


if __name__ == "__main__":
    unittest.main()
