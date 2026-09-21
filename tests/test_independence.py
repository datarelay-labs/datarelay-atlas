import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class IndependenceTests(unittest.TestCase):
    def test_independence_script_passes(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate-athena-independence.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ATHENA_INDEPENDENCE=PASS", proc.stdout)

    def test_no_athena_git_submodule(self):
        self.assertFalse((ROOT / ".gitmodules").exists())


if __name__ == "__main__":
    unittest.main()
