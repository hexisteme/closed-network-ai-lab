"""The acceptance demo passes end to end and leaves a clean audit log and an evidence file."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from review_demo import FIXTURES, SECRET, run_demo  # noqa: E402


class ReviewDemoTest(unittest.TestCase):
    def test_demo_passes_and_its_files_check_out(self):
        with tempfile.TemporaryDirectory() as evidence_dir, tempfile.TemporaryDirectory() as audit_dir:
            evidence = run_demo(FIXTURES, evidence_dir, audit_dir)
            failed = [(s["step"], name) for s in evidence["steps"] for name, ok in s["checks"].items() if not ok]
            self.assertEqual(failed, [])
            self.assertEqual(evidence["audit"]["violations"], [])
            self.assertTrue(evidence["passed"])
            self.assertEqual(len(evidence["steps"]), 6)

            written = json.loads((Path(evidence_dir) / evidence["evidence_file"]).read_text(encoding="utf-8"))
            self.assertEqual(written["audit_log_sha256"], evidence["audit_log_sha256"])
            log = (Path(audit_dir) / evidence["audit_log"]).read_text(encoding="utf-8")
            logged = {json.loads(line)["call_id"] for line in log.splitlines() if "call_id" in json.loads(line)}
            listed = [call_id for s in evidence["steps"] for call_id in s["call_ids"]]
            self.assertEqual(set(listed), logged)
            self.assertEqual(len(listed), len(set(listed)))
            self.assertNotIn(SECRET, log)
            self.assertNotIn("credentials", log)
            self.assertNotIn(SECRET, json.dumps(written))

    def test_two_runs_into_the_same_folders_keep_separate_files(self):
        with tempfile.TemporaryDirectory() as evidence_dir, tempfile.TemporaryDirectory() as audit_dir:
            runs = [run_demo(FIXTURES, evidence_dir, audit_dir) for _ in range(2)]
            self.assertNotEqual(runs[0]["audit_log"], runs[1]["audit_log"])
            self.assertEqual(len(list(Path(evidence_dir).glob("*.json"))), 2)
            for evidence in runs:
                self.assertTrue(evidence["passed"])
                log = (Path(audit_dir) / evidence["audit_log"]).read_text(encoding="utf-8").splitlines()
                logged = {json.loads(line)["call_id"] for line in log if "call_id" in json.loads(line)}
                self.assertEqual(logged, {call_id for s in evidence["steps"] for call_id in s["call_ids"]})


if __name__ == "__main__":
    unittest.main()
