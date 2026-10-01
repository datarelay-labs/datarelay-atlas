from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from atlas.browser_verification import (
    AUTHORITY,
    REQUEST_KIND,
    RESULT_KIND,
    SCENARIO,
    run_browser_verification,
    validate_browser_verification_request,
    validate_browser_verification_result,
)
from atlas.provenance import ValidationError

ROOT = Path(__file__).resolve().parents[1]
HEAD = "4" * 40


def request(
    provider: str = "playwright",
    *,
    variation: str = "BASELINE",
) -> dict:
    return {
        "schema_version": 1,
        "kind": REQUEST_KIND,
        "run_id": "browser-poc-1",
        "scenario_id": SCENARIO,
        "provider": provider,
        "target_url": "http://127.0.0.1:8788/",
        "environment": "LOCAL",
        "source_revision": HEAD,
        "work_packet_issue": 19,
        "variation": variation,
    }


def raw_pass() -> dict:
    return {
        "result": "PASS",
        "browser_engine": "chromium",
        "browser_version": "123.0",
        "actual_browser_process": True,
        "duration_ms": 123,
        "steps": [
            {
                "step_id": "overview_loaded",
                "outcome": "PASS",
                "detail": "Atlas Overview rendered in the actual browser.",
                "evidence_ref": None,
            },
            {
                "step_id": "concurrency_loaded",
                "outcome": "PASS",
                "detail": "Measured concurrency rendered after browser navigation.",
                "evidence_ref": None,
            },
        ],
        "model_metrics": {
            "state": "NOT_APPLICABLE",
            "provider": None,
            "model": None,
            "llm_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_microusd": 0,
        },
        "trace_ref": "evidence/browser-verification/browser-poc-1/playwright.zip",
        "screenshot_ref": None,
        "error_code": None,
        "detail": "Deterministic Playwright journey passed.",
    }


def command_result(payload: dict, *, returncode: int = 0):
    return subprocess.CompletedProcess(
        ["node"],
        returncode,
        stdout="log line\nATLAS_BROWSER_RESULT=" + json.dumps(payload) + "\n",
        stderr="ignored provider stderr",
    )


class BrowserVerificationTests(unittest.TestCase):
    def test_public_schemas_fixtures_and_runtime_parity(self) -> None:
        contracts = ROOT / "docs" / "contracts"
        fixtures = contracts / "fixtures"
        request_schema = json.loads(
            (contracts / "browser-verification-request.schema.json").read_text()
        )
        result_schema = json.loads(
            (contracts / "browser-verification-result.schema.json").read_text()
        )
        request_fixture = json.loads(
            (fixtures / "browser-verification-request.example.json").read_text()
        )
        result_fixture = json.loads(
            (fixtures / "browser-verification-result.example.json").read_text()
        )
        Draft202012Validator.check_schema(request_schema)
        Draft202012Validator.check_schema(result_schema)
        Draft202012Validator(request_schema).validate(request_fixture)
        Draft202012Validator(result_schema).validate(result_fixture)
        self.assertEqual(
            validate_browser_verification_request(request_fixture),
            request_fixture,
        )
        self.assertEqual(
            validate_browser_verification_result(result_fixture),
            result_fixture,
        )

    def test_request_is_exact_loopback_and_content_free(self) -> None:
        self.assertEqual(
            validate_browser_verification_request(request()),
            request(),
        )
        cases = []
        bad = request()
        bad["schema_version"] = True
        cases.append(bad)
        bad = request()
        bad["target_url"] = "https://example.com/"
        cases.append(bad)
        bad = request()
        bad["source_revision"] = "short"
        cases.append(bad)
        bad = request()
        bad["run_id"] = "path/collision"
        cases.append(bad)
        bad = request()
        bad["scenario_id"] = "ARBITRARY_PROMPT"
        cases.append(bad)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_browser_verification_request(payload)

    def test_secret_bearing_target_url_is_rejected_before_spawn(self) -> None:
        payload = request()
        payload["target_url"] = (
            "http://127.0.0.1:8788/?OPENAI_API_KEY=sk-"
            + "a" * 32
        )
        calls = []

        def runner(_argv, _cwd, _stdin, _env):
            calls.append(True)
            return command_result(raw_pass())

        with self.assertRaisesRegex(ValidationError, "target_url"):
            run_browser_verification(
                payload,
                repo_root=ROOT,
                command_runner=runner,
            )
        self.assertEqual(calls, [])

    def test_provider_child_environment_is_minimal_and_explicit(self) -> None:
        payload = raw_pass()
        payload.update(
            {
                "result": "HUMAN_REQUIRED",
                "browser_engine": "UNKNOWN",
                "browser_version": "UNKNOWN",
                "actual_browser_process": False,
                "duration_ms": 1,
                "steps": [],
                "model_metrics": {
                    "state": "UNAVAILABLE",
                    "provider": "openai",
                    "model": "openai/gpt-5.4-mini",
                    "llm_call_count": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "cost_microusd": None,
                },
                "trace_ref": None,
                "error_code": "STAGEHAND_MODEL_CREDENTIAL_UNAVAILABLE",
                "detail": "Stagehand semantic execution requires an approved model credential.",
            }
        )
        observed = {}

        def runner(_argv, _cwd, _stdin, env):
            observed.update(env)
            return command_result(payload)

        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "ghp_" + "x" * 32,
                "AWS_SECRET_ACCESS_KEY": "unsafe-parent-secret",
                "OPENAI_API_KEY": "sk-" + "p" * 32,
                "ATLAS_STAGEHAND_MODEL": "parent/model-must-not-leak",
                "PATH": "/tmp/evil-node:/usr/bin",
                "HOME": "/tmp/ambient-home",
                "LD_LIBRARY_PATH": "/tmp/ambient-libs",
            },
            clear=False,
        ):
            result = run_browser_verification(
                request("stagehand"),
                repo_root=ROOT,
                command_runner=runner,
                runtime_env={"LD_LIBRARY_PATH": "/tmp/explicit-libs"},
                stagehand_api_key="explicit-approved-key",
                stagehand_model="openai/gpt-5.4-mini",
            )

        self.assertEqual(result["result"], "HUMAN_REQUIRED")
        self.assertEqual(observed["OPENAI_API_KEY"], "explicit-approved-key")
        self.assertEqual(
            observed["ATLAS_STAGEHAND_MODEL"],
            "openai/gpt-5.4-mini",
        )
        self.assertEqual(observed["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertEqual(observed["LD_LIBRARY_PATH"], "/tmp/explicit-libs")
        self.assertNotIn("HOME", observed)
        self.assertNotIn("GITHUB_TOKEN", observed)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", observed)
        self.assertNotIn("unsafe-parent-secret", json.dumps(observed))
        self.assertNotIn("explicit-approved-key", json.dumps(result))

        observed.clear()
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-" + "z" * 32},
            clear=False,
        ):
            run_browser_verification(
                request("stagehand"),
                repo_root=ROOT,
                command_runner=runner,
            )
        self.assertNotIn("OPENAI_API_KEY", observed)
        self.assertEqual(observed["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertNotIn("HOME", observed)
        self.assertNotIn("LD_LIBRARY_PATH", observed)

        with self.assertRaisesRegex(ValidationError, "runtime environment"):
            run_browser_verification(
                request(),
                repo_root=ROOT,
                command_runner=runner,
                runtime_env={"GITHUB_TOKEN": "not-allowed"},
            )

    def test_runner_revalidates_loopback_after_both_navigation_steps(self) -> None:
        source = (
            ROOT / "tools" / "browser-verification" / "runner.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'await assertLoopbackPageUrl(page, "initial navigation");',
            source,
        )
        self.assertIn(
            'await assertLoopbackPageUrl(page, "concurrency navigation");',
            source,
        )
        self.assertGreaterEqual(
            source.count("await installLoopbackRequestGuard("),
            2,
        )

    def test_node_runner_rejects_external_navigation_and_unsafe_evidence_ancestors(self) -> None:
        runner = ROOT / "tools" / "browser-verification" / "runner.mjs"
        with tempfile.TemporaryDirectory() as tmp:
            test_root = Path(tmp)
            outside = test_root / "outside"
            outside.mkdir()
            (test_root / "evidence").symlink_to(
                outside,
                target_is_directory=True,
            )
            script = f"""
import {{
  assertLoopbackUrl,
  ensureEvidenceDir,
  installLoopbackRequestGuard,
}} from {json.dumps(runner.as_uri())};
assertLoopbackUrl("http://127.0.0.1:8788/");
assertLoopbackUrl("https://localhost:9443/concurrency");
let externalRejected = false;
try {{
  assertLoopbackUrl("https://example.com/concurrency");
}} catch {{
  externalRejected = true;
}}
if (!externalRejected) process.exit(21);

let routeHandler = null;
await installLoopbackRequestGuard({{
  route: async (pattern, handler) => {{
    if (pattern !== "**/*") process.exit(24);
    routeHandler = handler;
  }},
}});
if (!routeHandler) process.exit(25);
const routeEvents = [];
const fakeRoute = (url) => ({{
  request: () => ({{ url: () => url }}),
  continue: async () => routeEvents.push("continue"),
  abort: async () => routeEvents.push("abort"),
}});
await routeHandler(fakeRoute("https://example.com/concurrency"));
await routeHandler(fakeRoute("http://127.0.0.1:8788/concurrency"));
await routeHandler(fakeRoute("data:text/plain,ok"));
if (routeEvents.join(",") !== "abort,continue,continue") process.exit(26);

let symlinkRejected = false;
try {{
  await ensureEvidenceDir(
    {{ relDir: "evidence/browser-verification/browser-poc-1" }},
    {json.dumps(str(test_root))},
  );
}} catch {{
  symlinkRejected = true;
}}
if (!symlinkRejected) process.exit(22);
"""
            completed = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr[-1000:],
            )

            (test_root / "evidence").unlink()
            (test_root / "evidence").write_text(
                "not a directory",
                encoding="utf-8",
            )
            script = f"""
import {{ ensureEvidenceDir }} from {json.dumps(runner.as_uri())};
let rejected = false;
try {{
  await ensureEvidenceDir(
    {{ relDir: "evidence/browser-verification/browser-poc-1" }},
    {json.dumps(str(test_root))},
  );
}} catch {{
  rejected = true;
}}
if (!rejected) process.exit(23);
"""
            completed = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr[-1000:],
            )

    def test_playwright_pass_is_digest_bound_and_evidence_only(self) -> None:
        def runner(_argv: list[str], _cwd: str, _stdin: str, _env: dict[str, str]):
            return command_result(raw_pass())

        result = run_browser_verification(
            request(),
            repo_root=ROOT,
            command_runner=runner,
        )
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["authority"], AUTHORITY)
        self.assertEqual(result["deterministic_gate_override"], "NONE")
        self.assertTrue(result["browser"]["actual_process"])
        self.assertEqual(result["browser"]["mode"], "HEADLESS")
        self.assertEqual(result["model_metrics"]["state"], "NOT_APPLICABLE")
        self.assertEqual(
            validate_browser_verification_result(result),
            result,
        )
        tampered = deepcopy(result)
        tampered["result"] = "FAIL"
        with self.assertRaisesRegex(ValidationError, "digest mismatch"):
            validate_browser_verification_result(tampered)

    def test_stagehand_human_required_is_normalized_without_fake_pass(self) -> None:
        payload = raw_pass()
        payload.update(
            {
                "result": "HUMAN_REQUIRED",
                "browser_engine": "UNKNOWN",
                "browser_version": "UNKNOWN",
                "actual_browser_process": False,
                "duration_ms": 1,
                "steps": [],
                "model_metrics": {
                    "state": "UNAVAILABLE",
                    "provider": "openai",
                    "model": "openai/gpt-5.4-mini",
                    "llm_call_count": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "cost_microusd": None,
                },
                "trace_ref": None,
                "error_code": "STAGEHAND_MODEL_CREDENTIAL_UNAVAILABLE",
                "detail": "Stagehand semantic execution requires an approved model credential.",
            }
        )

        def runner(_argv: list[str], _cwd: str, _stdin: str, _env: dict[str, str]):
            return command_result(payload)

        result = run_browser_verification(
            request("stagehand"),
            repo_root=ROOT,
            command_runner=runner,
        )
        self.assertEqual(result["result"], "HUMAN_REQUIRED")
        self.assertFalse(result["browser"]["actual_process"])
        self.assertEqual(
            result["error_code"],
            "STAGEHAND_MODEL_CREDENTIAL_UNAVAILABLE",
        )

    def test_provider_process_failure_is_human_required_not_pass(self) -> None:
        def runner(_argv: list[str], _cwd: str, _stdin: str, _env: dict[str, str]):
            return subprocess.CompletedProcess(
                ["node"], 2, stdout="", stderr="OPENAI_API_KEY=sk-live-secret"
            )

        result = run_browser_verification(
            request(),
            repo_root=ROOT,
            command_runner=runner,
        )
        self.assertEqual(result["result"], "HUMAN_REQUIRED")
        self.assertEqual(result["error_code"], "PROVIDER_PROCESS_FAILED")
        self.assertNotIn("sk-live-secret", json.dumps(result))

    def test_pass_without_actual_browser_fails_closed(self) -> None:
        payload = raw_pass()
        payload["actual_browser_process"] = False

        def runner(_argv: list[str], _cwd: str, _stdin: str, _env: dict[str, str]):
            return command_result(payload)

        with self.assertRaisesRegex(
            ValidationError,
            "actual-browser PASS evidence",
        ):
            run_browser_verification(
                request(),
                repo_root=ROOT,
                command_runner=runner,
            )

    def test_secret_like_provider_detail_is_redacted(self) -> None:
        payload = raw_pass()
        payload["detail"] = "OPENAI_API_KEY=sk-live-secret"

        def runner(_argv: list[str], _cwd: str, _stdin: str, _env: dict[str, str]):
            return command_result(payload)

        result = run_browser_verification(
            request(),
            repo_root=ROOT,
            command_runner=runner,
        )
        self.assertNotIn("sk-live-secret", result["detail"])
        self.assertNotIn("sk-live-secret", json.dumps(result))

    def test_optional_node_runtime_is_pinned_and_not_python_dependency(self) -> None:
        package = json.loads(
            (ROOT / "tools/browser-verification/package.json").read_text()
        )
        self.assertTrue(package["private"])
        self.assertEqual(
            package["dependencies"]["@browserbasehq/stagehand"],
            "4.1.0",
        )
        self.assertEqual(package["dependencies"]["playwright"], "1.63.0")
        requirements = (ROOT / "requirements.txt").read_text()
        self.assertNotIn("stagehand", requirements.lower())
        self.assertNotIn("playwright", requirements.lower())


if __name__ == "__main__":
    unittest.main()
