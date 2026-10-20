"""No-production-effect tests for the ingress-aware transaction state machine."""

from __future__ import annotations

from pathlib import Path
from unittest import TestCase, mock
import json
import tempfile

from scripts import atlas_ingress_cutover_transaction as release


class FakeOperations:
    def __init__(self, fail: str | None = None, *, repeat: bool = False) -> None:
        self.trace: list[str] = []
        self.fail = fail
        self.repeat = repeat
        self.failed_once = False

    def _step(self, name: str) -> None:
        self.trace.append(name)
        if self.fail == name and (self.repeat or not self.failed_once):
            self.failed_once = True
            raise RuntimeError("synthetic step failure")

    def preflight(self) -> None:
        self._step("preflight")

    def capture_fresh_backup(self) -> None:
        self._step("fresh_backup")

    def stop(self, name: str) -> None:
        self._step("stop:" + name)

    def start(self, name: str) -> None:
        self._step("start:" + name)

    def reset_ingress(self) -> None:
        self._step("reset_ingress")

    def swap_code(self) -> None:
        self._step("swap_code")

    def swap_env(self) -> None:
        self._step("swap_env")

    def restore_code(self) -> None:
        self._step("restore_code")

    def restore_env(self) -> None:
        self._step("restore_env")

    def verify(self, *, old: bool) -> None:
        self._step("verify_old" if old else "verify_new")


FORWARD = [
    "preflight",
    "fresh_backup",
    "stop:" + release.INGRESS_SOCKET,
    "stop:" + release.INGRESS_SERVICE,
    "stop:" + release.WEB,
    "stop:" + release.MCP,
    "swap_code",
    "swap_env",
    "start:" + release.MCP,
    "start:" + release.WEB,
    "reset_ingress",
    "start:" + release.INGRESS_SOCKET,
    "start:" + release.INGRESS_SERVICE,
    "verify_new",
]
ROLLBACK = [
    "stop:" + release.INGRESS_SOCKET,
    "stop:" + release.INGRESS_SERVICE,
    "stop:" + release.WEB,
    "stop:" + release.MCP,
    "restore_code",
    "restore_env",
    "start:" + release.MCP,
    "start:" + release.WEB,
    "reset_ingress",
    "start:" + release.INGRESS_SOCKET,
    "start:" + release.INGRESS_SERVICE,
    "verify_old",
]


class TransactionOrderingTests(TestCase):
    def test_success_has_exact_dependency_order(self) -> None:
        actions = FakeOperations()
        release.Cutover(actions).execute()
        self.assertEqual(actions.trace, FORWARD)

    def test_preflight_failure_cannot_stop_running_services(self) -> None:
        actions = FakeOperations("preflight")
        with self.assertRaises(RuntimeError):
            release.Cutover(actions).execute()
        self.assertEqual(actions.trace, ["preflight"])

    def test_fresh_backup_failure_cannot_stop_services(self) -> None:
        actions = FakeOperations("fresh_backup")
        with self.assertRaises(RuntimeError):
            release.Cutover(actions).execute()
        self.assertEqual(actions.trace, ["preflight", "fresh_backup"])

    def test_any_forward_failure_attempts_full_rollback(self) -> None:
        for name in FORWARD[2:]:
            with self.subTest(step=name):
                actions = FakeOperations(name)
                with self.assertRaises(release.RolledBack):
                    release.Cutover(actions).execute()
                self.assertEqual(actions.trace, FORWARD[:FORWARD.index(name)+1] + ROLLBACK)

    def test_failed_rollback_never_claims_success(self) -> None:
        actions = FakeOperations("verify_old")
        class BrokenForward(FakeOperations):
            pass
        def fail_new(old: bool) -> None:
            actions._step("verify_old" if old else "verify_new")
            if not old:
                raise RuntimeError("synthetic public failure")
        actions.verify = fail_new
        with self.assertRaisesRegex(release.IncidentUnrecovered, "INCIDENT_UNRECOVERED"):
            release.Cutover(actions).execute()
        self.assertIn("verify_old", actions.trace)

    def test_rollback_keeps_processing_after_independent_errors(self) -> None:
        actions = FakeOperations()
        def stop(name: str) -> None:
            actions.trace.append("stop:" + name)
            raise RuntimeError("synthetic stop error")
        actions.stop = stop
        with self.assertRaises(release.IncidentUnrecovered):
            release.Cutover(actions).rollback()
        self.assertIn("restore_code", actions.trace)
        self.assertIn("restore_env", actions.trace)
        self.assertIn("verify_old", actions.trace)
        self.assertIn("start:" + release.INGRESS_SERVICE, actions.trace)

    def test_keyboard_interrupt_during_mutation_still_rolls_back(self) -> None:
        actions = FakeOperations()
        def interrupt_env() -> None:
            actions.trace.append("swap_env")
            raise KeyboardInterrupt("synthetic interrupt")
        actions.swap_env = interrupt_env
        with self.assertRaises(release.RolledBack):
            release.Cutover(actions).execute()
        self.assertEqual(
            actions.trace,
            FORWARD[:FORWARD.index("swap_env") + 1] + ROLLBACK,
        )

    def test_exact_unit_identity_and_sni_hosts(self) -> None:
        self.assertEqual(release.INGRESS_SOCKET, "datarelay-atlas-ingress.socket")
        self.assertEqual(release.INGRESS_SERVICE, "datarelay-atlas-ingress.service")
        self.assertEqual(release.MCP_HEALTH, "https://mcp.atlas.datarelay.run/healthz")
        self.assertEqual(release.AUTH_DISCOVERY, release.ISSUER + "/.well-known/openid-configuration")

    def test_argv_gate_disallows_non_frozen_candidate_without_effects(self) -> None:
        with mock.patch.object(release, "SystemOperations") as ops:
            self.assertEqual(
                release.main(["--execute", "--expected-head", "a" * 40]), 2
            )
            ops.assert_not_called()


class PublicHealthTests(TestCase):
    def test_both_public_hostnames_and_exact_issuer_are_checked(self) -> None:
        def fetch(url: str):
            if url == release.MCP_HEALTH:
                return {"status": "ok"}
            if url == release.AUTH_DISCOVERY:
                return {
                    "issuer": release.ISSUER,
                    "authorization_endpoint": release.ISSUER + "/protocol/openid-connect/auth",
                    "token_endpoint": release.ISSUER + "/protocol/openid-connect/token",
                }
            raise AssertionError("unexpected host")
        with mock.patch.object(release, "_public_json", side_effect=fetch) as fn:
            release._public_ready()
        self.assertEqual(fn.call_count, 2)

    def test_invalid_keycloak_issuer_is_never_ready(self) -> None:
        with mock.patch.object(release, "_public_json", side_effect=[
            {"status": "ok"},
            {"issuer": "https://wrong.invalid"},
        ]):
            with self.assertRaisesRegex(release.CutoverError, "AUTH_ISSUER_MISMATCH"):
                release._public_ready()

    def test_invalid_keycloak_endpoint_is_never_ready(self) -> None:
        with mock.patch.object(release, "_public_json", side_effect=[
            {"status": "ok"},
            {
                "issuer": release.ISSUER,
                "authorization_endpoint": release.ISSUER + ":9443/login",
                "token_endpoint": release.ISSUER + "/protocol/openid-connect/token",
            },
        ]):
            with self.assertRaisesRegex(release.CutoverError, "AUTH_ENDPOINT_MISMATCH"):
                release._public_ready()


    def test_web_root_timeout_is_diagnostic_and_never_accepted(self) -> None:
        with mock.patch.object(release, "urlopen", side_effect=TimeoutError("local UI took too long")) as fetch:
            with self.assertRaisesRegex(release.CutoverError, "^LOOPBACK_WEB_ROOT_TIMEOUT$"):
                release._web_ready()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0].full_url, "http://127.0.0.1:8788/")
        self.assertEqual(fetch.call_args.kwargs["timeout"], 8)

    def test_web_root_connection_failure_is_not_masked_by_other_routes(self) -> None:
        from urllib.error import URLError
        with mock.patch.object(release, "urlopen", side_effect=URLError("no response")) as fetch:
            with self.assertRaisesRegex(release.CutoverError, "^LOOPBACK_WEB_CONNECTION_FAILED$"):
                release._web_ready()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0].full_url, "http://127.0.0.1:8788/")


    def test_legacy_web_root_has_bounded_distinct_timeout(self) -> None:
        with mock.patch.object(release, "urlopen", side_effect=TimeoutError("slow old UI")) as fetch:
            with self.assertRaisesRegex(release.CutoverError, "^LOOPBACK_WEB_ROOT_TIMEOUT$"):
                release._web_ready(timeout=release.LEGACY_WEB_ROOT_TIMEOUT)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0].full_url, "http://127.0.0.1:8788/")
        self.assertEqual(fetch.call_args.kwargs["timeout"], 20)
        with mock.patch.object(release, "urlopen") as fetch:
            with self.assertRaisesRegex(release.CutoverError, "^WEB_GATE_TIMEOUT_INVALID$"):
                release._web_ready(timeout=60)
            fetch.assert_not_called()

    def test_old_and_new_health_require_root_under_distinct_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "live").mkdir()
            env = root / "env"
            backup = root / "env-backup"
            env.write_text("ORIGINAL=1\n")
            backup.write_bytes(env.read_bytes())
            with mock.patch.multiple(
                release, LIVE=root / "live", ENV=env, ENV_BACKUP=backup,
            ), mock.patch.object(release, "_run"), mock.patch.object(
                release, "_public_ready"
            ), mock.patch.object(release, "_web_ready") as probe, mock.patch.object(
                release, "_manifest"
            ), mock.patch.object(release, "_flags"), mock.patch.object(
                release, "_old_code_digest"
            ):
                release.SystemOperations().verify(old=True)
                probe.assert_called_once_with(timeout=release.LEGACY_WEB_ROOT_TIMEOUT)
                probe.reset_mock()
                release.SystemOperations().verify(old=False)
                probe.assert_called_once_with(timeout=release.NEW_WEB_ROOT_TIMEOUT)


class LocalPathGateTests(TestCase):
    def test_release_root_must_be_world_traversable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp).chmod(0o755)  # empty disposable test-only parent
            path = Path(tmp)/"candidate"
            path.mkdir(mode=0o755)
            release._directory(path, exact_mode=0o755)
            path.chmod(0o750)
            with self.assertRaises(release.CutoverError):
                release._directory(path, exact_mode=0o755)

    def test_manifest_cannot_be_replaced_by_wrong_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"sha256"
            path.write_text("wrong bytes")
            with mock.patch.object(release, "MANIFEST", path):
                with self.assertRaisesRegex(release.CutoverError, "MANIFEST_SHA_MISMATCH"):
                    release._manifest(Path(tmp))

    def test_old_code_digest_must_be_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "atlas"
            src.mkdir()
            (src / "mcp_http.py").write_text("wrong old code")
            with self.assertRaisesRegex(release.CutoverError, "OLD_CODE_IDENTITY_HASH_MISMATCH"):
                release._old_code_digest(Path(tmp))

    def test_execution_not_allowed_for_non_root(self) -> None:
        with mock.patch.object(release.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(release.CutoverError, "ROOT_OPERATOR_REQUIRED"):
                release._require_root()


if __name__ == "__main__":
    import unittest
    unittest.main()
