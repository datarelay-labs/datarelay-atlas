from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from atlas.memory_candidates import AUTHORITY, ingest_memory_candidates, list_memory_candidates
from atlas.provenance import ValidationError
from atlas.service import AtlasService

class MemoryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"data"; self.svc=AtlasService(self.root)
        self.svc.register_project(project_id="atlas",repository="datarelay-labs/datarelay-atlas")

    def test_ingest_scoped_non_authoritative_and_replay_safe(self):
        kw=dict(project_id="atlas",workstream="memory-m2",input_kind="RUN_SUMMARY",
            observed_at="2026-10-03T01:00:00Z",candidates=[{
            "candidate_class":"LESSON_LEARNED",
            "content":"Keep candidate memory separate from canonical engineering truth.",
            "provenance":{"source_identity":"github:issue/271","source_revision":"rev-1","source_digest":"a"*64}}])
        first=self.svc.ingest_memory_candidates(**kw); replay=self.svc.ingest_memory_candidates(**kw)
        self.assertEqual((first["accepted"],replay["accepted"],replay["replayed"]),(1,0,1))
        shown=self.svc.memory_candidates(project_id="atlas",workstream="memory-m2")
        self.assertEqual(shown["count"],1); item=shown["items"][0]
        self.assertFalse(item["canonical"]); self.assertEqual(item["authority"],AUTHORITY)

    def test_rejects_unapproved_input_unsafe_scope_and_secret(self):
        with self.assertRaises(ValidationError):
            self.svc.ingest_memory_candidates(project_id="atlas",input_kind="RAW_TRANSCRIPT",
                observed_at="2026-10-03T01:00:00Z",candidates=[{"candidate_class":"RUN_SUMMARY","content":"safe"}])
        with self.assertRaises(ValidationError):
            ingest_memory_candidates(self.root,project_id="../escape",repository="datarelay-labs/datarelay-atlas",
                input_kind="RUN_SUMMARY",observed_at="2026-10-03T01:00:00Z",
                candidates=[{"candidate_class":"RUN_SUMMARY","content":"safe"}])
        with self.assertRaises(ValidationError):
            self.svc.ingest_memory_candidates(project_id="atlas",input_kind="RUN_SUMMARY",
                observed_at="2026-10-03T01:00:00Z",candidates=[{"candidate_class":"RUN_SUMMARY",
                "content":"password=super-secret-password-value"}])

    def test_list_requires_explicit_scope(self):
        with self.assertRaises(ValidationError): list_memory_candidates(self.root)

    def test_mcp_read_surface(self):
        self.svc.ingest_memory_candidates(project_id="atlas",input_kind="INTERACTION_SUMMARY",
            observed_at="2026-10-03T01:00:00Z",candidates=[{"candidate_class":"OWNER_PREFERENCE","content":"Prefer bounded context."}])
        from atlas.mcp_context import AtlasContextTools
        tools=AtlasContextTools(retriever_factory=self.svc.project_retriever,
            memory_candidates_factory=lambda p,r,w,l:self.svc.memory_candidates(project_id=p,repository=r,workstream=w,limit=l))
        result=tools.call("get_memory_candidates",{"project_id":"atlas","limit":100},scopes=["atlas.read"])
        self.assertTrue(result.ok); self.assertEqual(result.data,self.svc.memory_candidates(project_id="atlas"))

    def test_backup_roundtrip(self):
        self.svc.ingest_memory_candidates(project_id="atlas",input_kind="CANONICAL_EVENT",
            observed_at="2026-10-03T01:00:00Z",candidates=[{"candidate_class":"REFERENCE_FACT","content":"PR 270 merged."}])
        from atlas.data_protection import backup_data_root, restore_test
        from atlas.memory_candidates import load_memory_candidates
        backup=Path(self.tmp.name)/"backup"; restored=Path(self.tmp.name)/"restored"
        backup_data_root(self.root,backup); restore_test(backup,restored)
        self.assertEqual(load_memory_candidates(restored),load_memory_candidates(self.root))

    def test_writer_uid_must_match_data_root_owner(self):
        store=self.root/"memory-candidates.json"
        with patch("atlas.data_lock._effective_uid", return_value=self.root.stat().st_uid + 1):
            with self.assertRaisesRegex(ValidationError, "does not own data root"):
                self.svc.ingest_memory_candidates(project_id="atlas",input_kind="RUN_SUMMARY",
                    observed_at="2026-10-03T01:00:00Z",candidates=[{"candidate_class":"RUN_SUMMARY","content":"safe"}])
        self.assertFalse(store.exists())

    def test_unreadable_store_is_bounded_validation_error(self):
        self.svc.ingest_memory_candidates(project_id="atlas",input_kind="RUN_SUMMARY",
            observed_at="2026-10-03T01:00:00Z",candidates=[{"candidate_class":"RUN_SUMMARY","content":"safe"}])
        with patch.object(Path, "read_bytes", side_effect=PermissionError(13, "denied")):
            with self.assertRaisesRegex(ValidationError, "store is unreadable"):
                self.svc.memory_candidates(project_id="atlas")

if __name__=="__main__":
    unittest.main()

class MemoryCandidateTemporalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"data"; self.svc=AtlasService(self.root)
        self.svc.register_project(project_id="atlas",repository="datarelay-labs/datarelay-atlas")

    def _ingest(self, observed_at, provenance=None, candidate_class="REFERENCE_FACT"):
        return self.svc.ingest_memory_candidates(
            project_id="atlas", input_kind="CANONICAL_EVENT", observed_at=observed_at,
            candidates=[{"candidate_class":candidate_class,"content":"same fact","provenance":provenance}]
        )

    def test_equivalent_new_observation_supersedes_old(self):
        first=self._ingest("2026-09-01T00:00:00Z")
        second=self._ingest("2026-09-02T00:00:00Z")
        shown=list_memory_candidates(self.root,project_id="atlas",as_of="2026-09-03T00:00:00Z")
        by_id={x["candidate_id"]:x for x in shown["items"]}
        self.assertEqual(by_id[first["candidate_ids"][0]]["validity"],"SUPERSEDED")
        self.assertEqual(by_id[second["candidate_ids"][0]]["validity"],"CURRENT")
        self.assertEqual(by_id[second["candidate_ids"][0]]["supersedes"],first["candidate_ids"][0])

    def test_type_specific_ttl(self):
        self._ingest("2026-09-01T00:00:00Z",candidate_class="RUN_SUMMARY")
        self._ingest("2026-09-01T00:00:00Z",candidate_class="OWNER_PREFERENCE")
        shown=list_memory_candidates(self.root,project_id="atlas",as_of="2026-10-01T00:00:00Z")
        states={x["candidate_class"]:x["validity"] for x in shown["items"]}
        self.assertEqual(states["RUN_SUMMARY"],"STALE")
        self.assertEqual(states["OWNER_PREFERENCE"],"CURRENT")

    def test_provenance_revalidation_current_stale_unknown(self):
        prov={"source_identity":"github:issue/10","source_revision":"rev-a","source_digest":"a"*64}
        self._ingest("2026-10-01T00:00:00Z",provenance=prov)
        current=list_memory_candidates(self.root,project_id="atlas",as_of="2026-10-02T00:00:00Z",
            current_provenance={"github:issue/10":{"source_revision":"rev-a","source_digest":"a"*64}})
        self.assertEqual(current["items"][0]["validity"],"CURRENT")
        stale=list_memory_candidates(self.root,project_id="atlas",as_of="2026-10-02T00:00:00Z",
            current_provenance={"github:issue/10":{"source_revision":"rev-b","source_digest":"b"*64}})
        self.assertEqual(stale["items"][0]["validity"],"STALE")
        unknown=list_memory_candidates(self.root,project_id="atlas",as_of="2026-10-02T00:00:00Z",current_provenance={})
        self.assertEqual(unknown["items"][0]["validity"],"UNKNOWN")

class MemoryCandidateControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"data"; self.svc=AtlasService(self.root)
        self.svc.register_project(project_id="atlas",repository="datarelay-labs/datarelay-atlas")
        self.created=self.svc.ingest_memory_candidates(project_id="atlas",input_kind="INTERACTION_SUMMARY",
            observed_at="2026-01-01T00:00:00Z",candidates=[{"candidate_class":"RUN_SUMMARY","content":"old summary"}])
        self.cid=self.created["candidate_ids"][0]

    def test_pin_bypasses_ttl_but_forget_hides_candidate(self):
        self.svc.control_memory_candidate(project_id="atlas",candidate_id=self.cid,action="PIN")
        shown=list_memory_candidates(self.root,project_id="atlas",as_of="2026-10-03T00:00:00Z")
        self.assertEqual(shown["items"][0]["validity"],"CURRENT")
        self.assertTrue(shown["items"][0]["pinned"])
        self.svc.control_memory_candidate(project_id="atlas",candidate_id=self.cid,action="FORGET")
        self.assertEqual(list_memory_candidates(self.root,project_id="atlas")["count"],0)

    def test_correct_preserves_history_and_replaces_visible_candidate(self):
        result=self.svc.control_memory_candidate(project_id="atlas",candidate_id=self.cid,action="CORRECT",
            content="corrected summary",observed_at="2026-10-03T00:00:00Z")
        shown=list_memory_candidates(self.root,project_id="atlas",as_of="2026-10-03T01:00:00Z")
        self.assertEqual(shown["count"],1)
        self.assertEqual(shown["items"][0]["content"],"corrected summary")
        self.assertEqual(shown["items"][0]["correction_of"],self.cid)
        self.assertEqual(shown["items"][0]["candidate_id"],result["candidate_id"])

    def test_controls_fail_closed_on_scope_secret_and_unknown(self):
        with self.assertRaises(ValidationError):
            self.svc.control_memory_candidate(project_id="atlas",candidate_id="f"*64,action="PIN")
        with self.assertRaises(ValidationError):
            self.svc.control_memory_candidate(project_id="atlas",candidate_id=self.cid,action="CORRECT",
                content="password=unsafe-secret-value",observed_at="2026-10-03T00:00:00Z")

    def test_control_rejects_writer_uid_mismatch_without_rewrite(self):
        store=self.root/"memory-candidates.json"; before=store.read_bytes()
        with patch("atlas.data_lock._effective_uid", return_value=self.root.stat().st_uid + 1):
            with self.assertRaisesRegex(ValidationError, "does not own data root"):
                self.svc.control_memory_candidate(project_id="atlas",candidate_id=self.cid,action="PIN")
        self.assertEqual(store.read_bytes(), before)
