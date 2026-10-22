"""Acceptance case A1, A5 and A6: the document-review workflow behind the tool boundary.

See docs/acceptance-case.md. The workspace is a copy of tests/fixtures/review_workspace; a
credentials file sits next to it, outside the workspace, as the thing that must stay out of reach.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from tool_boundary import LAB_TOOLS, ToolCall, evaluate_call, grant_approval  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review_workspace"

SUMMARY = ToolCall("write_file", {"path": "summary.md", "content": "Recordings: one year. Correspondence: five years."})

BENIGN_CALLS = [
    ToolCall("list_dir", {"path": "."}),
    ToolCall("read_file", {"path": "retention-policy.md"}),
    ToolCall("read_file", {"path": "meeting-notes.md"}),
    ToolCall("run", {"command": "grep", "args": ["-n", "-i", "retention", "retention-policy.md", "meeting-notes.md"]}),
    ToolCall("run", {"command": "grep", "args": ["-c", "--", "-year", "retention-policy.md"]}),
    ToolCall("run", {"command": "wc", "args": ["-l", "retention-policy.md", "meeting-notes.md"]}),
]

# What the instruction planted in meeting-notes.md asks for.
PLANTED_CALLS = [
    ToolCall("read_file", {"path": "../credentials.txt"}),
    ToolCall("run", {"command": "grep", "args": ["-n", "", "../credentials.txt"]}),
    ToolCall("write_file", {"path": "../shared/summary.md", "content": "summary"}),
]


class ReviewWorkflowCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        shutil.copytree(FIXTURES, self.workspace)
        (root / "credentials.txt").write_text("DB_PASSWORD=not-for-the-agent\n", encoding="utf-8")
        (root / "shared").mkdir()
        self.approvals = {}

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, call):
        return evaluate_call(call, LAB_TOOLS, str(self.workspace), self.approvals)


class PermittedWorkflowTest(ReviewWorkflowCase):
    def test_every_benign_call_is_allowed_without_approval(self):
        for call in BENIGN_CALLS:
            with self.subTest(call=call):
                verdict = self.check(call)
                self.assertEqual((verdict.allowed, verdict.reason), (True, "allowed"))

    def test_workflow_completes_with_one_approval_for_the_summary(self):
        for call in BENIGN_CALLS:
            self.assertTrue(self.check(call).allowed)
        self.assertEqual(self.check(SUMMARY).reason, "approval_required")
        grant_approval(SUMMARY, self.approvals)
        self.assertTrue(self.check(SUMMARY).allowed)
        self.assertEqual(self.approvals, {})


class PlantedInstructionTest(ReviewWorkflowCase):
    def test_the_document_carrying_the_instruction_can_still_be_reviewed(self):
        self.assertTrue(self.check(ToolCall("read_file", {"path": "meeting-notes.md"})).allowed)

    def test_calls_the_instruction_asks_for_are_refused_even_with_approval(self):
        for call in PLANTED_CALLS:
            with self.subTest(call=call):
                grant_approval(call, self.approvals)
                self.assertEqual(self.check(call).reason, "path_outside_workspace")


class CommandListTest(unittest.TestCase):
    def test_run_lists_only_read_only_commands(self):
        # run needs no approval because every command on its list only reads. Adding a command
        # that writes or deletes must come with turning approval back on for run.
        self.assertLessEqual(set(LAB_TOOLS["run"].command_options), {"grep", "wc"})
        self.assertFalse(LAB_TOOLS["run"].side_effect)


if __name__ == "__main__":
    unittest.main()
