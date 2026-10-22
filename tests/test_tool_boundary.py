import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from tool_boundary import (  # noqa: E402
    LAB_TOOLS,
    ToolCall,
    approval_record,
    audit_record,
    evaluate_call,
    fingerprint_call,
    grant_approval,
    withdraw_approval,
)


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "notes.txt").write_text("hello\n", encoding="utf-8")
        self.outside = root / "outside.txt"
        self.outside.write_text("secret\n", encoding="utf-8")
        self.approvals = {}

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, name, arguments):
        return evaluate_call(ToolCall(name, arguments), LAB_TOOLS, str(self.workspace), self.approvals)


class SchemaTest(WorkspaceCase):
    def test_unknown_tool_is_denied(self):
        self.assertEqual(self.check("delete_file", {"path": "notes.txt"}).reason, "tool_not_allowed")

    def test_missing_required_argument_is_denied(self):
        self.assertEqual(self.check("read_file", {}).reason, "schema_invalid")

    def test_wrong_argument_type_is_denied(self):
        self.assertEqual(self.check("read_file", {"path": ["notes.txt"]}).reason, "schema_invalid")

    def test_unexpected_argument_is_denied(self):
        self.assertEqual(self.check("read_file", {"path": "notes.txt", "mode": "rw"}).reason, "schema_invalid")


class PathConfinementTest(WorkspaceCase):
    """A schema that only says "string" accepts every one of these; the boundary must not."""

    def test_relative_path_inside_workspace_is_allowed_without_approval(self):
        verdict = self.check("read_file", {"path": "notes.txt"})
        self.assertEqual((verdict.allowed, verdict.reason), (True, "allowed"))
        self.assertEqual(verdict.resolved_path, str((self.workspace / "notes.txt").resolve()))

    def test_parent_traversal_is_denied(self):
        self.assertEqual(self.check("read_file", {"path": "../outside.txt"}).reason, "path_outside_workspace")

    def test_absolute_path_outside_workspace_is_denied(self):
        self.assertEqual(self.check("read_file", {"path": str(self.outside)}).reason, "path_outside_workspace")

    def test_symlink_pointing_outside_is_denied(self):
        os.symlink(self.outside, self.workspace / "link.txt")
        self.assertEqual(self.check("read_file", {"path": "link.txt"}).reason, "path_outside_workspace")

    def test_sibling_directory_with_shared_prefix_is_denied(self):
        sibling = Path(self._tmp.name) / "workspace-evil"
        sibling.mkdir()
        self.assertEqual(self.check("list_dir", {"path": "../workspace-evil"}).reason, "path_outside_workspace")


class CommandTest(WorkspaceCase):
    def test_command_off_the_allowlist_is_denied(self):
        self.assertEqual(self.check("run", {"command": "curl", "args": ["http://x"]}).reason, "command_not_allowed")

    def test_command_given_as_path_is_denied(self):
        self.assertEqual(self.check("run", {"command": "/bin/sh", "args": ["-c", "id"]}).reason, "command_not_allowed")

    def test_operand_outside_workspace_is_denied(self):
        verdict = self.check("run", {"command": "grep", "args": ["-n", "x", "../outside.txt"]})
        self.assertEqual(verdict.reason, "path_outside_workspace")

    def test_option_carrying_a_file_is_denied(self):
        """No shell is involved, yet grep would still read the outside file (argument injection)."""
        for smuggled in ("--file=/etc/passwd", "-f/etc/passwd", "-f"):
            with self.subTest(smuggled=smuggled):
                verdict = self.check("run", {"command": "grep", "args": [smuggled, "notes.txt"]})
                self.assertEqual(verdict.reason, "option_not_allowed")

    def test_allowlisted_command_is_returned_as_argv_without_a_shell(self):
        arguments = {"command": "grep", "args": ["-n", "x; rm -rf /", "notes.txt"]}
        grant_approval(ToolCall("run", arguments), self.approvals)
        verdict = self.check("run", arguments)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.argv, ["grep", "-n", "x; rm -rf /", "notes.txt"])


class ApprovalTest(WorkspaceCase):
    def test_side_effect_without_approval_is_held(self):
        verdict = self.check("write_file", {"path": "out.txt", "content": "a"})
        self.assertEqual((verdict.allowed, verdict.reason), (False, "approval_required"))

    def test_approval_for_other_arguments_does_not_carry_over(self):
        grant_approval(ToolCall("write_file", {"path": "out.txt", "content": "a"}), self.approvals)
        verdict = self.check("write_file", {"path": "out.txt", "content": "b"})
        self.assertEqual(verdict.reason, "approval_required")

    def test_matching_approval_allows_once(self):
        arguments = {"path": "out.txt", "content": "a"}
        grant_approval(ToolCall("write_file", arguments), self.approvals)
        self.assertTrue(self.check("write_file", arguments).allowed)
        self.assertEqual(self.check("write_file", arguments).reason, "approval_required")

    def test_path_check_runs_before_approval(self):
        arguments = {"path": "../outside.txt", "content": "a"}
        grant_approval(ToolCall("write_file", arguments), self.approvals)
        self.assertEqual(self.check("write_file", arguments).reason, "path_outside_workspace")

    def test_withdrawn_approval_no_longer_allows(self):
        call = ToolCall("write_file", {"path": "out.txt", "content": "a"})
        grant_approval(call, self.approvals)
        self.assertTrue(withdraw_approval(call, self.approvals))
        self.assertEqual(self.check("write_file", call.arguments).reason, "approval_required")
        self.assertFalse(withdraw_approval(call, self.approvals))


class AuditTest(WorkspaceCase):
    def test_audit_line_carries_no_argument_values(self):
        call = ToolCall("write_file", {"path": "out.txt", "content": "API_KEY=sk-live-123"})
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), self.approvals)
        line = json.dumps(audit_record(call, verdict))
        self.assertNotIn("sk-live-123", line)
        self.assertNotIn("out.txt", line)
        record = json.loads(line)
        self.assertEqual(record["tool"], "write_file")
        self.assertEqual(record["decision"], "deny")
        self.assertEqual(record["reason"], "approval_required")
        self.assertEqual(record["argument_bytes"], len(json.dumps(call.arguments, sort_keys=True).encode()))

    def test_approval_and_the_call_it_allowed_share_an_id(self):
        call = ToolCall("write_file", {"path": "out.txt", "content": "API_KEY=sk-live-123"})
        approval_id = grant_approval(call, self.approvals)
        granted = approval_record(call, approval_id)
        decided = audit_record(call, evaluate_call(call, LAB_TOOLS, str(self.workspace), self.approvals))
        self.assertEqual((granted["event"], decided["decision"]), ("approved", "allow"))
        self.assertEqual(granted["approval_id"], decided["approval_id"])
        for line in (json.dumps(granted), json.dumps(decided)):
            self.assertNotIn("sk-live-123", line)
            self.assertNotIn("out.txt", line)

    def test_approval_id_is_not_derived_from_the_arguments(self):
        call = ToolCall("write_file", {"path": "out.txt", "content": "a"})
        approval_id = grant_approval(call, self.approvals)
        self.assertNotIn(approval_id, fingerprint_call(call))
        withdraw_approval(call, self.approvals)
        self.assertNotEqual(grant_approval(call, self.approvals), approval_id)


if __name__ == "__main__":
    unittest.main()
