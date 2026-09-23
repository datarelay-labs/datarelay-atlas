"""Completion-hook spawn-dispatch default / opt-out regressions."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "awc-completion-hook.sh"


class AwcCompletionHookSpawnTests(unittest.TestCase):
    def _run_hook(
        self,
        *,
        spawn_dispatch: str | None,
        audit_adapter: str = "fixed",
        work_packet_adapter: str | None = None,
    ) -> str:
        """Run the hook with a PATH shim that captures drain-inbox argv."""
        self.assertTrue(HOOK.is_file(), msg=f"missing hook: {HOOK}")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            capture = tmp_path / "drain-argv.txt"
            real_python = Path("/usr/bin/python3")
            if not real_python.is_file():
                real_python = Path(
                    subprocess.check_output(["which", "python3"], text=True).strip()
                )
            shim = bin_dir / "python3"
            shim.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
args=("$@")
for arg in "${{args[@]}}"; do
  if [[ "$arg" == "drain-inbox" ]]; then
    printf '%s\\n' "${{args[*]}}" > "{capture}"
    exit 0
  fi
done
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "atlas" ]]; then
  exit 0
fi
exec "{real_python}" "$@"
""",
                encoding="utf-8",
            )
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["AWC_WORKSTREAM"] = "autonomous-work-controller-poc"
            env["AWC_ISSUE_NUMBER"] = "12"
            env["AWC_ATTEMPT"] = "1"
            env["AWC_DRAIN"] = "1"
            env["AWC_AUDIT_ADAPTER"] = audit_adapter
            env["AWC_AUDIT_VERDICT"] = "PASS"
            env["ATLAS_DATA_ROOT"] = str(tmp_path / "data")
            env.pop("AWC_SPAWN_DISPATCH", None)
            env.pop("AWC_WORK_PACKET_ADAPTER", None)
            if spawn_dispatch is not None:
                env["AWC_SPAWN_DISPATCH"] = spawn_dispatch
            if work_packet_adapter is not None:
                env["AWC_WORK_PACKET_ADAPTER"] = work_packet_adapter

            completed = subprocess.run(
                ["bash", str(HOOK)],
                cwd=str(ROOT),
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )
            self.assertTrue(capture.is_file(), msg="drain-inbox was not invoked")
            return capture.read_text(encoding="utf-8")

    def test_default_codex_passes_spawn_dispatch(self):
        argv = self._run_hook(spawn_dispatch=None, audit_adapter="codex")
        self.assertIn("--spawn-dispatch", argv)

    def test_default_fixed_omits_spawn_without_github_packet(self):
        argv = self._run_hook(spawn_dispatch=None, audit_adapter="fixed")
        self.assertNotIn("--spawn-dispatch", argv)
        self.assertIn("--work-packet-adapter recording", argv)

    def test_fixed_spawn_requires_explicit_github_packet(self):
        argv = self._run_hook(
            spawn_dispatch="1",
            audit_adapter="fixed",
            work_packet_adapter="github",
        )
        self.assertIn("--spawn-dispatch", argv)
        self.assertIn("--work-packet-adapter github", argv)

    def test_opt_out_omits_spawn_dispatch(self):
        argv = self._run_hook(spawn_dispatch="0")
        self.assertNotIn("--spawn-dispatch", argv)
        self.assertIn("--work-packet-adapter recording", argv)

    def test_hook_preserves_work_resume_contract_in_tree(self):
        work_controller = (ROOT / "atlas" / "work_controller.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('RESUME_PROMPT = "/work-resume"', work_controller)
        self.assertIn(
            '["agent", "--force", "persist", "--trust", RESUME_PROMPT]',
            work_controller,
        )


if __name__ == "__main__":
    unittest.main()
