"""Instruction-governance exact-worktree cleanliness regressions."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from atlas.instruction_governance import (
    AUTHORITY,
    FILENAME,
    LEDGER_KIND,
    SCHEMA_VERSION,
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
    def test_matching_invalid_stored_audit_is_not_duplicate_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            data_root = base / "data"
            first = instruction_governance_preflight(
                data_root,
                repo_root=repo,
                profile=profile,
                agent_base_path=agent_base,
                behavior_scenarios_path=scenarios,
            )
            data_root.mkdir(parents=True, exist_ok=True)
            duplicate_change = {
                "path": "AGENTS.md",
                "before_digest": "a" * 64,
                "after_digest": "b" * 64,
            }
            audit = {
                "audit_identity": first["audit_identity"],
                "evaluated_at": "2026-10-01T00:00:00Z",
                "authority": AUTHORITY,
                "outcome": "CANARY_READY",
                "target_repository": first["target_repository"],
                "target_head": first["target_head"],
                "engineering_system_revision": first["engineering_system_revision"],
                "model_provider": first["model_provider"],
                "model_name": first["model_name"],
                "model_profile": first["model_profile"],
                "harness_id": first["harness_id"],
                "harness_revision": first["harness_revision"],
                "trigger_kind": first["trigger_kind"],
                "trigger_revision": first["trigger_revision"],
                "inventory_digest": first["inventory_digest"],
                "behavior_results": [],
                "missing_mandatory_scenarios": [],
                "candidate_changes": [duplicate_change, dict(duplicate_change)],
                "evaluation_ref": "github:issue-225",
                "canonical_mutation": False,
            }
            (data_root / FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "kind": LEDGER_KIND,
                        "authority": AUTHORITY,
                        "audits": [audit],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError, "matching stored audit is invalid"
            ):
                instruction_governance_preflight(
                    data_root,
                    repo_root=repo,
                    profile=profile,
                    agent_base_path=agent_base,
                    behavior_scenarios_path=scenarios,
                )

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

    def test_ignored_discovered_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            exclude = repo / ".git" / "info" / "exclude"
            exclude.write_text("ai/\n", encoding="utf-8")
            managed = repo / "ai" / "rules.md"
            managed.parent.mkdir()
            managed.write_text("# ignored local rules\n", encoding="utf-8")
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_untracked_symlink_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            managed = repo / "ai" / "rules.md"
            managed.parent.mkdir()
            target = base / "alternate-rules.md"
            target.write_text("# alternate\n", encoding="utf-8")
            managed.symlink_to(target)
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_ignored_untracked_symlink_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            exclude = repo / ".git" / "info" / "exclude"
            exclude.write_text("ai/\n", encoding="utf-8")
            managed = repo / "ai" / "rules.md"
            managed.parent.mkdir()
            target = base / "alternate-rules.md"
            target.write_text("# alternate\n", encoding="utf-8")
            managed.symlink_to(target)
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_optional_exact_managed_path_directory_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            (repo / ".cursorignore").mkdir()
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_managed_prefix_root_regular_file_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            (repo / "prompts").write_text(
                "not a directory\n",
                encoding="utf-8",
            )
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_managed_parent_regular_file_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            engineering = repo / ".engineering"
            for child in engineering.iterdir():
                child.unlink()
            engineering.rmdir()
            engineering.write_text(
                "not a directory\n",
                encoding="utf-8",
            )
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_skip_worktree_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            _git(repo, "update-index", "--skip-worktree", "AGENTS.md")
            (repo / "AGENTS.md").write_text(
                "# local override\n",
                encoding="utf-8",
            )
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_assume_unchanged_managed_surface_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            _git(repo, "update-index", "--assume-unchanged", "AGENTS.md")
            (repo / "AGENTS.md").write_text(
                "# local override\n",
                encoding="utf-8",
            )
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

    def test_symlinked_managed_prefix_root_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            target = base / "alternate-ai"
            target.mkdir()
            (target / "rules.md").write_text(
                "# alternate managed tree\n",
                encoding="utf-8",
            )
            (repo / "ai").symlink_to(target, target_is_directory=True)
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_symlinked_dynamic_managed_container_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            scripts = repo / "scripts"
            scripts.mkdir()
            target = base / "alternate-scripts"
            target.mkdir()
            (target / "runtime-hook.sh").write_text(
                "#!/bin/sh\n",
                encoding="utf-8",
            )
            (scripts / "generated").symlink_to(
                target,
                target_is_directory=True,
            )
            self._assert_dirty_rejected(
                repo,
                agent_base,
                scenarios,
                profile,
                base / "data",
            )

    def test_symlinked_managed_parent_directory_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, agent_base, scenarios, profile = _repo_fixture(base)
            engineering = repo / ".engineering"
            target = base / "alternate-engineering"
            engineering.rename(target)
            engineering.symlink_to(target, target_is_directory=True)
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
