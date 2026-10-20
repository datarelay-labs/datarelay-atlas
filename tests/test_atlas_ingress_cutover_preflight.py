"""Preflight uses disposable data and validates live unit dependencies without effects."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import atlas_ingress_cutover_transaction as release


UNIT_SHOW = (
    "DynamicUser=yes\n"
    "Requires=datarelay-atlas.service\n"
    "After=datarelay-atlas.service keycloak.service\n"
    "ExecStart={ path=/opt/datarelay-atlas/.venv/bin/python ; argv[]=/opt/datarelay-atlas/.venv/bin/python -m atlas.sni_ingress }\n"
)
SOCKET_CAT = "[Socket]\nListenStream=0.0.0.0:443\nService=datarelay-atlas-ingress.service\n"


class UnitGraphTests(unittest.TestCase):
    def test_expected_ingress_and_socket_semantics_pass(self):
        def cmd(args, **_):
            return SOCKET_CAT if args[1] == "cat" else UNIT_SHOW
        with mock.patch.object(release, "_run", side_effect=cmd):
            release._ingress_unit_contract()

    def test_dynamic_user_or_requires_removal_fails_closed(self):
        for text in (
            UNIT_SHOW.replace("DynamicUser=yes", "DynamicUser=no"),
            UNIT_SHOW.replace("Requires=datarelay-atlas.service", "Requires="),
            UNIT_SHOW.replace("After=datarelay-atlas.service keycloak.service", "After=keycloak.service"),
            UNIT_SHOW.replace("atlas.sni_ingress", "atlas.unknown_ingress"),
        ):
            with self.subTest(unit=text):
                def cmd(args, **_):
                    return SOCKET_CAT if args[1] == "cat" else text
                with mock.patch.object(release, "_run", side_effect=cmd):
                    with self.assertRaises(release.CutoverError):
                        release._ingress_unit_contract()

    def test_socket_not_bound_to_443_fails_closed(self):
        def cmd(args, **_):
            return SOCKET_CAT.replace("0.0.0.0:443", "127.0.0.1:8443") if args[1] == "cat" else UNIT_SHOW
        with mock.patch.object(release, "_run", side_effect=cmd):
            with self.assertRaisesRegex(release.CutoverError, "INGRESS_SOCKET_CONTRACT"):
                release._ingress_unit_contract()


class ProofIsolatedPreflightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="atlas-safe-preflight-test-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.live = self.base / "live-code"
        self.live.mkdir()
        self.source = self.base / "candidate"
        self.source.mkdir()
        self.stage = self.base / "stage"
        self.stage.mkdir()
        self.code_backup = self.base / "protected-code-backup"
        (self.code_backup / ".venv/bin").mkdir(parents=True)
        (self.code_backup / ".venv/bin/python").write_text("stub")
        self.env = self.base / "live.env"
        self.env.write_text("key=unchanged\n")
        self.env_backup = self.base / "env.backup"
        self.env_backup.write_bytes(self.env.read_bytes())
        self.env_stage = self.base / "stage.env"
        self.env_stage.write_text("flag=no\n")
        self.data_backup = self.base / "data-backup"
        self.data_backup.mkdir()
        (self.data_backup / "manifest.json").write_text("{}")
        self.data_live = self.base / "live-durable-data"
        self.data_live.mkdir()

    def test_schema_probes_only_touch_disposable_restored_data(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.multiple(
                release, LIVE=self.live, SOURCE=self.source, STAGE=self.stage,
                CODE_BACKUP=self.code_backup, DATA_BACKUP=self.data_backup,
                DATA=self.data_live, ENV=self.env, ENV_STAGE=self.env_stage,
                ENV_BACKUP=self.env_backup,
            ))
            stack.enter_context(mock.patch.object(release.os, "geteuid", return_value=0))
            web_probe = None
            for name in (
                "_directory", "_manifest", "_protected_env", "_flags",
                "_old_code_digest", "_public_ready", "_web_ready",
                "_ingress_unit_contract",
            ):
                observer = stack.enter_context(mock.patch.object(release, name))
                if name == "_web_ready":
                    web_probe = observer
            commands = []
            def cmd(args, **kwargs):
                commands.append(args)
                return "ok"
            stack.enter_context(mock.patch.object(release, "_run", side_effect=cmd))
            stack.enter_context(mock.patch.object(
                release, "_atlas_command", return_value={"status": "ready"}
            ))
            schema_calls = []
            def returned(args, *, env=None):
                schema_calls.append(args)
                if "restore-test" in args:
                    return {"status": "ok"}
                return {"status": "ok", "rewritten": False}
            stack.enter_context(mock.patch.object(release, "_check_json", side_effect=returned))
            release.SystemOperations().preflight()
            self.assertIsNotNone(web_probe)
            web_probe.assert_called_once_with(timeout=release.LEGACY_WEB_ROOT_TIMEOUT)

        self.assertEqual(len(schema_calls), 3)
        restore, upgrade, rollback = schema_calls
        self.assertIn("restore-test", restore)
        self.assertIn("upgrade", upgrade)
        self.assertIn("rollback", rollback)
        proof = restore[restore.index("--dest") + 1]
        self.assertEqual(upgrade[upgrade.index("--data-root") + 1], proof)
        self.assertEqual(rollback[rollback.index("--data-root") + 1], proof)
        for call in schema_calls:
            self.assertNotIn(str(self.data_live), call)
        self.assertEqual(self.env.read_bytes(), self.env_backup.read_bytes())
        self.assertTrue(any(command[0] == "systemd-run" for command in commands))

    def test_schema_probe_rewrite_flag_blocks_preflight(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.multiple(
                release, LIVE=self.live, SOURCE=self.source, STAGE=self.stage,
                CODE_BACKUP=self.code_backup, DATA_BACKUP=self.data_backup,
                DATA=self.data_live, ENV=self.env, ENV_STAGE=self.env_stage,
                ENV_BACKUP=self.env_backup,
            ))
            stack.enter_context(mock.patch.object(release.os, "geteuid", return_value=0))
            for name in (
                "_directory", "_manifest", "_protected_env", "_flags",
                "_old_code_digest", "_public_ready", "_web_ready",
                "_ingress_unit_contract",
            ):
                stack.enter_context(mock.patch.object(release, name))
            stack.enter_context(mock.patch.object(release, "_run", return_value="ok"))
            stack.enter_context(mock.patch.object(
                release, "_atlas_command", return_value={"status": "ready"}
            ))
            def returned(args, *, env=None):
                return {"status": "ok"} if "restore-test" in args else {
                    "status": "ok", "rewritten": True
                }
            stack.enter_context(mock.patch.object(release, "_check_json", side_effect=returned))
            with self.assertRaisesRegex(release.CutoverError, "SCHEMA_COMPATIBILITY_NOT_QUALIFIED"):
                release.SystemOperations().preflight()


if __name__ == "__main__":
    unittest.main()
