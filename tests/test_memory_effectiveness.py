import fcntl, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from atlas.service import AtlasService
from atlas.memory_effectiveness import effectiveness_report
from atlas.provenance import ValidationError
from atlas.data_protection import backup_data_root, restore_test
from atlas.data_lock import data_root_write_lock

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

    def test_owner_change_while_waiting_is_rejected_after_lock(self):
        actual_uid=self.root.stat().st_uid; state={"locked":False}; original=fcntl.flock
        def observed_uid():
            return actual_uid + 1 if state["locked"] else actual_uid
        def flock_then_change_owner(fd, operation):
            result=original(fd, operation)
            if operation == fcntl.LOCK_EX: state["locked"]=True
            elif operation == fcntl.LOCK_UN: state["locked"]=False
            return result
        store=self.root/"memory-effectiveness.json"
        with patch("atlas.data_lock.fcntl.flock", side_effect=flock_then_change_owner), \
             patch("atlas.data_lock._effective_uid", side_effect=observed_uid):
            with self.assertRaisesRegex(ValidationError, "does not own data root"):
                self.svc.record_memory_effectiveness(self.obs())
        self.assertFalse(store.exists())

    def test_owner_drift_before_publish_cleans_temp(self):
        actual_uid=self.root.stat().st_uid
        with patch("atlas.data_lock._effective_uid", side_effect=[actual_uid, actual_uid, actual_uid + 1]):
            with self.assertRaisesRegex(ValidationError, "does not own data root"):
                self.svc.record_memory_effectiveness(self.obs())
        self.assertFalse((self.root/"memory-effectiveness.json").exists())
        self.assertFalse((self.root/"memory-effectiveness.json.tmp").exists())

    def test_data_root_path_swap_while_waiting_fails_closed(self):
        lock_root=Path(self.tmp.name)/"swap-root"; moved=Path(self.tmp.name)/"swap-old"; original=fcntl.flock
        swapped={"done":False}
        def flock_then_swap(fd, operation):
            result=original(fd, operation)
            if operation == fcntl.LOCK_EX and not swapped["done"]:
                swapped["done"]=True; lock_root.rename(moved); lock_root.mkdir()
            return result
        with patch("atlas.data_lock.fcntl.flock", side_effect=flock_then_swap):
            with self.assertRaisesRegex(ValidationError, "identity changed"):
                with data_root_write_lock(lock_root):
                    self.fail("swapped data root must never enter write section")
