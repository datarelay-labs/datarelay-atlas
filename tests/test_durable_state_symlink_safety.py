from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atlas.provenance import ValidationError
import atlas.concurrency_join as concurrency_join
import atlas.concurrency_claim_join as concurrency_claim_join
import atlas.concurrency_execution as concurrency_execution
import atlas.concurrency_admission as concurrency_admission
import atlas.concurrency_authorization as concurrency_authorization
import atlas.instruction_governance as instruction_governance
import atlas.instruction_governance_canary as instruction_governance_canary
import atlas.decision_plane as decision_plane
import atlas.decision_plane_canary as decision_plane_canary
import atlas.provider_route_quality as provider_route_quality
import atlas.provider_dashboard as provider_dashboard


class DurableStateSymlinkSafetyTests(unittest.TestCase):
    def _dangling(self, root: Path, name: str) -> Path:
        path = root / name
        path.symlink_to(root / f"missing-{Path(name).name}")
        return path

    def test_durable_authority_loaders_reject_dangling_symlinks(self) -> None:
        cases = [
            (
                concurrency_join.FILENAME,
                lambda root: concurrency_join._load_ledger(root),
            ),
            (
                concurrency_claim_join.FILENAME,
                lambda root: concurrency_claim_join._load_ledger(root),
            ),
            (
                concurrency_execution.FILENAME,
                lambda root: concurrency_execution._load_ledger(root),
            ),
            (
                concurrency_admission.SNAPSHOT_FILENAME,
                lambda root: concurrency_admission._load_snapshot(root),
            ),
            (
                concurrency_admission.RUNS_FILENAME,
                lambda root: concurrency_admission._load_runs(root),
            ),
            (
                concurrency_authorization.SNAPSHOT_FILENAME,
                lambda root: concurrency_authorization._load_snapshot(root),
            ),
            (
                concurrency_authorization.FILENAME,
                lambda root: concurrency_authorization._load_authorization(root),
            ),
            (
                instruction_governance.FILENAME,
                lambda root: instruction_governance._load_ledger(root),
            ),
            (
                instruction_governance.DISPOSITION_FILENAME,
                lambda root: instruction_governance._load_disposition_ledger(root),
            ),
            (
                instruction_governance_canary.FILENAME,
                lambda root: instruction_governance_canary._load_ledger(root),
            ),
            (
                decision_plane.FILENAME,
                lambda root: decision_plane._load_ledger(root / decision_plane.FILENAME),
            ),
            (
                decision_plane_canary.FILENAME,
                lambda root: decision_plane_canary._load_admission(
                    root / decision_plane_canary.FILENAME
                ),
            ),
            (
                provider_route_quality.FILENAME,
                lambda root: provider_route_quality.provider_route_quality_dashboard(root),
            ),
            (
                provider_dashboard.FILENAME,
                lambda root: provider_dashboard._validated_snapshot_inputs(root),
            ),
        ]
        for filename, loader in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._dangling(root, filename)
                with self.assertRaises(ValidationError):
                    loader(root)


if __name__ == "__main__":
    unittest.main()
