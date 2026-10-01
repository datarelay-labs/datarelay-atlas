"""Instruction-governance exact-worktree cleanliness regressions."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.instruction_governance import (
    build_instruction_governance_profile,
    instruction_governance_preflight,
)
from atlas.provenance import ValidationError


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _repo_fixture(base: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "atlas-test@example.invalid")
    _git(repo, "config", "user.name", "Atlas Test")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/datarelay-labs/datarelay-atlas.git",
    )
    (repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    engineering = repo / ".engineering"
    engineering.mkdir()
    (engineering / "project.yaml").write_text(
        "engineering_system:\n  version: 1.7.0\n",
        encoding="utf-8",
    )
    _git(repo, "add", "AGENTS.md", ".engineering/project.yaml")
    _git(repo, "commit", "-m", "fixture")

    agent_base = base / "AGENT_BASE.md"
    agent_base.write_text("# canonical agent base\n", encoding="utf-8")
    scenarios = base / "behavior.yaml"
    scenarios.write_text(
        "version: 1\n"
        "scenarios:\n"
        "- id: scenario-1\n"
        "  mandatory: true\n"
        "  safety: true\n"
        "  checker: checker-1\n",
        encoding="utf-8",
    )
    profile = build_instruction_governance_profile(
        repo_root=repo,
        engineering_system_revision="c" * 40,
        agent_base_path=agent_base,
        behavior_scenarios_path=scenarios,
        trigger_kind="MANUAL_AUDIT",
        trigger_revision="manual-1",
        model_provider="openai",
        model_name="gpt-5.6",
        model_profile="sol",
        harness_id="chat",
        harness_revision="1",
    )
    return repo, agent_base, scenarios, profile


class InstructionGovernancePreflightTests(unittest.TestCase):
    def _assert_dirty_rejected(
        self,
        repo: Path,
        agent_base: Path,
        scenarios: Path,
        profile: dict[str, object],
        data_root: Path,
    ) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "managed surfaces are dirty",
        ):
            instruction_governance_preflight(
                data_root,
                repo_root=repo,
                profile=profile,
                agent_base_path=agent_base,
                behavior_scenarios_path=scenarios,
            )

    def test_deleted_tracked_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            (repo / "AGENTS.md").unlink()
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_staged_deleted_tracked_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            _git(repo, "rm", "AGENTS.md")
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_symlink_replaced_tracked_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            agents = repo / "AGENTS.md"
            agents.unlink()
            target = base / "alternate-rules.md"
            target.write_text("# alternate\n", encoding="utf-8")
            agents.symlink_to(target)
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )


if __name__ == "__main__":
    unittest.main()
