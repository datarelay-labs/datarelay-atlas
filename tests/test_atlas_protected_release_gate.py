"""No-network regressions for the fail-closed protected Atlas release boundary."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import atlas_protected_release_gate as gate


class ProtectedRunnerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate_root = Path(tempfile.mkdtemp()).resolve()
        self.sha = "a" * 40
        self.env = {
            "EXPECTED_SHA": self.sha,
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "datarelay-labs/datarelay-atlas",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_PROTECTED": "true",
            "RUNNER_ENVIRONMENT": "self-hosted",
            "ENGINEERING_RELEASE_CONTEXT": "protected-production",
            "AUTHORIZED_EXECUTION_CONTEXT": "protected-production",
            "RELEASE_PHASE": "qualify",
            "CANDIDATE_ROOT": str(self.candidate_root),
        }

    def verify(self, *, env=None, actual_head=None, context="protected-production", phase="qualify"):
        return gate.verify_boundary(
            self.env if env is None else env,
            actual_head=self.sha if actual_head is None else actual_head,
            root=self.candidate_root,
            execution_context=context,
            phase=phase,
        )

    def test_valid_is_boundary_only(self):
        result = self.verify()
        self.assertEqual(result["boundary"], "PROTECTED_RUNNER_CONTEXT_VERIFIED")
        self.assertEqual(result["production_deployed"], "NOT_ASSERTED")

    def test_each_trust_fact_fails_closed(self):
        for key in (
            "GITHUB_ACTIONS",
            "GITHUB_REPOSITORY",
            "GITHUB_EVENT_NAME",
            "GITHUB_REF",
            "GITHUB_REF_PROTECTED",
            "RUNNER_ENVIRONMENT",
            "ENGINEERING_RELEASE_CONTEXT",
            "AUTHORIZED_EXECUTION_CONTEXT",
            "RELEASE_PHASE",
        ):
            with self.subTest(key=key):
                updated = dict(self.env, **{key: "untrusted"})
                with self.assertRaisesRegex(gate.ReleaseBoundaryError, "BOUNDARY_"):
                    self.verify(env=updated)

    def test_each_trust_fact_missing_fails_closed(self):
        for key in list(self.env):
            with self.subTest(key=key):
                updated = dict(self.env)
                updated.pop(key)
                with self.assertRaises(gate.ReleaseBoundaryError):
                    self.verify(env=updated)

    def test_disallows_short_or_nonnumeric_candidate_sha(self):
        for sha in ("main", "abc", "ABC" * 13 + "X", "g" * 40, "a" * 39):
            with self.subTest(sha=sha):
                with self.assertRaisesRegex(gate.ReleaseBoundaryError, "CANDIDATE_SHA_INVALID"):
                    self.verify(env=dict(self.env, EXPECTED_SHA=sha))

    def test_checkout_head_must_match_exact_sha(self):
        with self.assertRaisesRegex(gate.ReleaseBoundaryError, "CANDIDATE_HEAD_MISMATCH"):
            self.verify(actual_head="b" * 40)

    def test_release_profile_must_be_protected(self):
        with self.assertRaisesRegex(gate.ReleaseBoundaryError, "RELEASE_PROFILE_CONTEXT_INVALID"):
            self.verify(context="github-hosted")

    def test_candidate_root_must_match(self):
        with self.assertRaisesRegex(gate.ReleaseBoundaryError, "CANDIDATE_ROOT_MISMATCH"):
            self.verify(env=dict(self.env, CANDIDATE_ROOT="/tmp/other-candidate"))

    def test_rejects_invalid_phase(self):
        with self.assertRaisesRegex(gate.ReleaseBoundaryError, "BOUNDARY_RELEASE_PHASE_INVALID|BOUNDARY_PHASE_INVALID"):
            self.verify(phase="deploy")

    def test_post_release_phase_requires_matching_environment(self):
        with self.assertRaisesRegex(gate.ReleaseBoundaryError, "BOUNDARY_RELEASE_PHASE_INVALID"):
            self.verify(phase="post-release")
        result = self.verify(
            env=dict(self.env, RELEASE_PHASE="post-release"), phase="post-release"
        )
        self.assertEqual(result["phase"], "post-release")

    def test_no_environment_is_not_authorized(self):
        with self.assertRaisesRegex(gate.ReleaseBoundaryError, "BOUNDARY_GITHUB_ACTIONS_INVALID"):
            self.verify(env={})


class PublicSNIObservationTests(unittest.TestCase):
    def test_public_endpoints_are_distinct_and_checked(self):
        def fetch(url):
            if url == gate.MCP_HEALTH_URL:
                return {"status": "ok"}
            if url == gate.AUTH_DISCOVERY_URL:
                return {
                    "issuer": gate.ISSUER,
                    "authorization_endpoint": gate.ISSUER + "/protocol/openid-connect/auth",
                    "token_endpoint": gate.ISSUER + "/protocol/openid-connect/token",
                }
            raise AssertionError("unexpected hostname")

        with mock.patch.object(gate, "_read_json", side_effect=fetch) as probe:
            result = gate.public_smoke()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(result["public_mcp"], "HTTP200")
        self.assertEqual(result["public_auth"], "HTTP200")
        self.assertEqual(result["native_mcp_write_acceptance"], "NOT_TESTED")

    def test_keycloak_issuer_must_match(self):
        with mock.patch.object(gate, "_read_json", side_effect=[
            {},
            {"issuer": "https://attacker.invalid/realms/atlas"},
        ]):
            with self.assertRaisesRegex(gate.ReleaseBoundaryError, "KEYCLOAK_ISSUER_MISMATCH"):
                gate.public_smoke()

    def test_metadata_must_not_point_to_old_9443(self):
        with mock.patch.object(gate, "_read_json", side_effect=[
            {},
            {
                "issuer": gate.ISSUER,
                "authorization_endpoint": gate.ISSUER + ":9443/auth",
                "token_endpoint": gate.ISSUER + "/protocol/openid-connect/token",
            },
        ]):
            with self.assertRaisesRegex(gate.ReleaseBoundaryError, "KEYCLOAK_METADATA_ORIGIN_MISMATCH"):
                gate.public_smoke()

    def test_unreachable_public_mcp_is_not_pass(self):
        with mock.patch.object(gate, "_read_json", side_effect=OSError("public TLS inaccessible")):
            with self.assertRaises(OSError):
                gate.public_smoke()

    def test_public_smoke_is_read_only(self):
        with mock.patch.object(gate, "_read_json", side_effect=[
            {"status": "ready"},
            {
                "issuer": gate.ISSUER,
                "authorization_endpoint": gate.ISSUER + "/protocol/openid-connect/auth",
                "token_endpoint": gate.ISSUER + "/protocol/openid-connect/token",
            },
        ]):
            self.assertEqual(gate.public_smoke()["operational_cutover"], "NOT_ASSERTED")


if __name__ == "__main__":
    unittest.main()
