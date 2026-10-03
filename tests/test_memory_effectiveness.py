import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from atlas.service import AtlasService
from atlas.memory_effectiveness import effectiveness_report
from atlas.provenance import ValidationError
from atlas.data_protection import backup_data_root, restore_test

class MemoryEffectivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"data"; self.svc=AtlasService(self.root)
        self.svc.register_project(project_id="atlas",repository="datarelay-labs/datarelay-atlas")

    def obs(self, success=True):
        return {"project_id":"atlas","repository":"datarelay-labs/datarelay-atlas","workstream":"m5",
        "observed_at":"2026-10-03T00:00:00Z","important_expected":4,"important_recalled":3,
        "stale_injected":1,"irrelevant_injected":1,"duplicate_injected":1,"injected_context_bytes":12000,
        "repeated_owner_explanations":1,"first_pass_success":success}

    def test_no_data_insufficient_and_measured(self):
        self.assertEqual(self.svc.memory_effectiveness_report("atlas")["state"],"NO_DATA")
        self.svc.record_memory_effectiveness(self.obs())
        r=self.svc.memory_effectiveness_report("atlas"); self.assertEqual(r["state"],"INSUFFICIENT")
        self.assertEqual(r["metrics"]["important_memory_recall_rate"],0.75)
        for i in range(9): self.svc.record_memory_effectiveness(self.obs(success=i%2==0))
        r=self.svc.memory_effectiveness_report("atlas"); self.assertEqual(r["state"],"MEASURED")
        self.assertFalse(r["policy_mutated"]); self.assertEqual(r["observation_count"],10)

    def test_rejects_invalid_scope_and_secret(self):
        bad=self.obs(); bad["repository"]="other/repo"
        with self.assertRaises(ValidationError): self.svc.record_memory_effectiveness(bad)
        bad=self.obs(); bad["workstream"]="password=unsafe-secret-value"
        with self.assertRaises(ValidationError): self.svc.record_memory_effectiveness(bad)

    def test_backup_preserves_measurements(self):
        self.svc.record_memory_effectiveness(self.obs())
        backup=Path(self.tmp.name)/"backup"; restored=Path(self.tmp.name)/"restored"
        backup_data_root(self.root,backup); restore_test(backup,restored)
        other=AtlasService(restored)
        self.assertEqual(other.memory_effectiveness_report("atlas")["observation_count"],1)

    def test_writer_uid_must_match_data_root_owner(self):
        store=self.root/"memory-effectiveness.json"
        with patch("atlas.data_lock._effective_uid", return_value=self.root.stat().st_uid + 1):
            with self.assertRaisesRegex(ValidationError, "does not own data root"):
                self.svc.record_memory_effectiveness(self.obs())
        self.assertFalse(store.exists())

    def test_unreadable_store_is_bounded_validation_error(self):
        self.svc.record_memory_effectiveness(self.obs())
        with patch.object(Path, "read_text", side_effect=PermissionError(13, "denied")):
            with self.assertRaisesRegex(ValidationError, "store is unreadable"):
                effectiveness_report(self.root, project_id="atlas")
