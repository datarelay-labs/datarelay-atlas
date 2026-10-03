import json,tempfile,unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from atlas.cli import main

class MemoryCliWritebackTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
  out=StringIO()
  with redirect_stdout(out):
   rc=main(["--data-root",str(self.root),"project","register","p","--display-name","P","--repository","datarelay-labs/p"])
  self.assertEqual(rc,0)
 def call(self,args):
  out=StringIO()
  with redirect_stdout(out): rc=main(["--data-root",str(self.root),*args])
  return rc,json.loads(out.getvalue()) if out.getvalue().strip().startswith(("{","[")) else out.getvalue()
 def test_candidate_ingest_and_effectiveness_record(self):
  rc,x=self.call(["memory-candidates","ingest","--project-id","p","--workstream","w","--input-kind","RUN_SUMMARY","--observed-at","2026-10-03T00:00:00Z","--candidate-class","LESSON_LEARNED","--content","Use bounded Atlas context after canonical state."])
  self.assertEqual(rc,0);self.assertEqual(x["accepted"],1);self.assertFalse(x["canonical"])
  rc,x=self.call(["memory-effectiveness","record","--project-id","p","--repository","datarelay-labs/p","--workstream","w","--observed-at","2026-10-03T00:00:00Z","--important-expected","1","--important-recalled","1","--stale-injected","0","--irrelevant-injected","0","--duplicate-injected","0","--injected-context-bytes","512","--repeated-owner-explanations","0","--first-pass-success"])
  self.assertEqual(rc,0);self.assertEqual(x["state"],"RECORDED");self.assertFalse(x["policy_mutated"])
  self.assertTrue((self.root/"memory-candidates.json").exists());self.assertTrue((self.root/"memory-effectiveness.json").exists())
 def test_secret_candidate_rejected(self):
  rc,_=self.call(["memory-candidates","ingest","--project-id","p","--input-kind","RUN_SUMMARY","--observed-at","2026-10-03T00:00:00Z","--candidate-class","RUN_SUMMARY","--content","password=unsafe-secret-value"])
  self.assertNotEqual(rc,0);self.assertFalse((self.root/"memory-candidates.json").exists())
if __name__=="__main__":unittest.main()
