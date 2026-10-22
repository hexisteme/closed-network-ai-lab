"""Execution behind the boundary and the audit log file it leaves.

A tool session decides each call with the boundary, runs only allowed calls, and writes one
JSONL line per approval, withdrawal, decision and execution. The reconciliation check reads that
file back and proves the log is consistent and carries no argument values.
"""

import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from audit_check import reconcile_audit  # noqa: E402
import tool_session  # noqa: E402
from tool_boundary import LAB_TOOLS, ToolCall, ToolSpec, Verdict, _object_schema, evaluate_call, grant_approval  # noqa: E402
from tool_session import RUN_ENV, RUN_TIMEOUT, ToolSession, execute  # noqa: E402


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "notes.txt").write_text("alpha\nbeta retention\n", encoding="utf-8")
        (root / "outside.txt").write_text("SECRET-OUTSIDE\n", encoding="utf-8")
        self.audit_path = root / "audit.jsonl"
        self.session = ToolSession(str(self.workspace), str(self.audit_path))
        self.addCleanup(self.session.close)

    def tearDown(self):
        self._tmp.cleanup()

    def lines(self):
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]


class ExecuteTest(SessionCase):
    def run_call(self, call):
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), {})
        self.assertTrue(verdict.allowed, verdict.reason)
        return execute(call, verdict, str(self.workspace))

    def test_refuses_a_call_the_boundary_did_not_allow(self):
        call = ToolCall("read_file", {"path": "../outside.txt"})
        with self.assertRaises(PermissionError):
            execute(call, Verdict(False, "path_outside_workspace"), str(self.workspace))

    def test_read_file_returns_the_file(self):
        result = self.run_call(ToolCall("read_file", {"path": "notes.txt"}))
        self.assertEqual((result.ok, result.output), (True, "alpha\nbeta retention\n"))

    def test_list_dir_returns_sorted_names(self):
        (self.workspace / "a.md").write_text("", encoding="utf-8")
        result = self.run_call(ToolCall("list_dir", {"path": "."}))
        self.assertEqual(result.output, "a.md\nnotes.txt")

    def test_run_passes_shell_syntax_to_the_program_as_a_literal(self):
        marker = self.workspace / "pwned"
        pattern = f"$(touch {marker}); touch {marker}"
        result = self.run_call(ToolCall("run", {"command": "grep", "args": ["-c", "--", pattern, "notes.txt"]}))
        self.assertTrue(result.ok)
        self.assertEqual((result.exit_code, result.output), (1, "0\n"))
        self.assertFalse(marker.exists())

    def test_run_reports_the_exit_code_and_output(self):
        result = self.run_call(ToolCall("run", {"command": "grep", "args": ["-n", "retention", "notes.txt"]}))
        self.assertEqual((result.ok, result.exit_code, result.output), (True, 0, "2:beta retention\n"))

    def test_run_starts_the_program_without_shell_environment_or_stdin(self):
        with mock.patch.object(tool_session.subprocess, "Popen", wraps=subprocess.Popen) as spy:
            self.run_call(ToolCall("run", {"command": "wc", "args": ["-l", "notes.txt"]}))
        argv, kwargs = spy.call_args.args[0], spy.call_args.kwargs
        self.assertEqual(os.path.dirname(argv[0]) in ("/usr/bin", "/bin"), True)
        self.assertEqual((kwargs["shell"], kwargs["env"], kwargs["stdin"]), (False, RUN_ENV, subprocess.DEVNULL))
        self.assertEqual(inspect.signature(tool_session._capture).parameters["timeout"].default, RUN_TIMEOUT)
        self.assertEqual(set(RUN_ENV), {"PATH", "LC_ALL"})

    def test_run_hands_file_operands_over_as_open_descriptors_under_their_names(self):
        seen = {}
        real_popen = subprocess.Popen

        def record_stage(argv, **kwargs):
            stage = kwargs["cwd"]
            seen["links"] = {
                os.path.relpath(os.path.join(top, name), stage): os.readlink(os.path.join(top, name))
                for top, _, names in os.walk(stage) for name in names
            }
            seen["pass_fds"], seen["argv"] = kwargs["pass_fds"], argv
            return real_popen(argv, **kwargs)

        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "b.txt").write_text("alpha\nalpha\n", encoding="utf-8")
        with mock.patch.object(tool_session.subprocess, "Popen", side_effect=record_stage):
            result = self.run_call(ToolCall("run", {"command": "grep", "args": ["-c", "alpha", "notes.txt", "./sub/b.txt"]}))
        self.assertEqual(seen["argv"][1:], ["-c", "--", "alpha", "./notes.txt", "./sub/b.txt"])
        self.assertEqual(seen["links"]["notes.txt"], f"/dev/fd/{seen['pass_fds'][0]}")
        self.assertEqual(seen["links"]["sub/b.txt"], f"/dev/fd/{seen['pass_fds'][1]}")
        self.assertEqual(result.output, "./notes.txt:1\n./sub/b.txt:2\n")

    def test_run_file_name_that_looks_like_an_option_stays_a_file(self):
        (self.workspace / "-c").write_text("alpha\nalpha\n", encoding="utf-8")
        result = self.run_call(ToolCall("run", {"command": "grep", "args": ["-n", "alpha", "./-c"]}))
        self.assertEqual((result.exit_code, result.output), (0, "1:alpha\n2:alpha\n"))
        options_first = self.run_call(ToolCall("run", {"command": "wc", "args": ["notes.txt", "-l"]}))
        self.assertEqual(options_first.output.split(), ["2", "./notes.txt"])

    def test_run_output_is_cut_while_it_is_read(self):
        (self.workspace / "big.txt").write_text("line\n" * 50000, encoding="utf-8")
        with mock.patch.object(tool_session, "MAX_OUTPUT", 100):
            result = self.run_call(ToolCall("run", {"command": "grep", "args": ["line", "big.txt"]}))
        self.assertEqual((result.exit_code, len(result.output)), (0, 100))

    def test_capture_kills_a_program_past_its_time_limit(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            tool_session._capture(["/bin/sleep", "5"], self._tmp.name, [], timeout=0.2)

    def test_capture_time_limit_also_ends_a_child_that_holds_the_pipes(self):
        forks = [sys.executable, "-c", "import os, time; os.fork(); time.sleep(5)"]
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            tool_session._capture(forks, self._tmp.name, [], timeout=0.2)
        self.assertLess(time.monotonic() - started, 2)

    def test_list_dir_name_that_is_not_utf8_is_shown_and_logged(self):
        with mock.patch.object(tool_session.os, "listdir", return_value=["bad-\udcff"]):
            verdict, result = self.session.submit(ToolCall("list_dir", {"path": "."}))
        self.assertEqual((result.ok, result.output), (True, "bad-\ufffd"))
        self.assertEqual(self.lines()[-1]["event"], "executed")

    def test_run_refuses_the_same_file_named_twice(self):
        os.symlink("notes.txt", self.workspace / "alias.txt")
        for args in (["-c", "alpha", "notes.txt", "notes.txt"], ["-c", "alpha", "notes.txt", "alias.txt"]):
            with self.subTest(args=args):
                result = self.run_call(ToolCall("run", {"command": "grep", "args": args}))
                self.assertEqual((result.ok, result.error), (False, "duplicate_operand"))

    def test_run_output_text_that_looks_like_a_descriptor_path_is_left_alone(self):
        (self.workspace / "fd.txt").write_text("see /dev/fd/3 and /dev/fd/4\n", encoding="utf-8")
        result = self.run_call(ToolCall("run", {"command": "grep", "args": ["-n", "see", "fd.txt", "notes.txt"]}))
        self.assertEqual(result.output, "./fd.txt:1:see /dev/fd/3 and /dev/fd/4\n")

    def test_run_returns_stderr_and_the_log_counts_it(self):
        verdict, result = self.session.submit(ToolCall("run", {"command": "grep", "args": ["-c", "--", "[", "notes.txt"]}))
        self.assertTrue(verdict.allowed)
        self.assertEqual(result.exit_code, 2)
        self.assertNotEqual(result.stderr, "")
        self.assertEqual(self.lines()[-1]["stderr_bytes"], len(result.stderr.encode()))
        self.assertEqual(reconcile_audit(str(self.audit_path), [])["violations"], [])

    def test_run_operand_swapped_for_a_link_after_the_check_is_refused(self):
        call = ToolCall("run", {"command": "wc", "args": ["-l", "notes.txt"]})
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), {})
        os.remove(self.workspace / "notes.txt")
        os.symlink(Path(self._tmp.name) / "outside.txt", self.workspace / "notes.txt")
        with mock.patch.object(tool_session, "confine_path", lambda raw, ws: os.path.join(os.path.realpath(ws), raw)):
            result = execute(call, verdict, str(self.workspace))
        self.assertEqual((result.ok, result.error), (False, "path_changed"))

    def test_run_timeout_and_missing_command_are_reported(self):
        call = ToolCall("run", {"command": "wc", "args": ["-l", "notes.txt"]})
        with mock.patch.object(tool_session, "_capture", side_effect=subprocess.TimeoutExpired("wc", 10)):
            self.assertEqual(self.run_call(call).error, "timeout")
        with mock.patch.object(tool_session.shutil, "which", return_value=None):
            self.assertEqual(self.run_call(call).error, "command_missing")

    def test_run_refuses_an_argv_that_differs_from_the_call(self):
        call = ToolCall("run", {"command": "wc", "args": []})
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), {})
        verdict.argv[:] = [sys.executable, "-c", "pass"]
        self.assertEqual(execute(call, verdict, str(self.workspace)).error, "argv_mismatch")

    def test_run_operand_retargeted_inside_the_workspace_after_the_check_is_refused(self):
        (self.workspace / "other.txt").write_text("OTHER\n", encoding="utf-8")
        call = ToolCall("run", {"command": "wc", "args": ["-l", "notes.txt"]})
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), {})
        os.remove(self.workspace / "notes.txt")
        os.symlink("other.txt", self.workspace / "notes.txt")
        self.assertEqual(execute(call, verdict, str(self.workspace)).error, "path_changed")

    def test_resolution_outside_the_root_is_never_walked(self):
        # As if the workspace path were retargeted between two resolutions: the path still
        # matches the check, but it lies above the root, so it would need a `..` component.
        outside = str(Path(self._tmp.name).resolve() / "outside.txt")
        call = ToolCall("read_file", {"path": "notes.txt"})
        with mock.patch.object(tool_session, "confine_path", lambda raw, root: outside):
            result = execute(call, Verdict(True, "allowed", resolved_path=outside), str(self.workspace))
        self.assertEqual((result.ok, result.error, result.output), (False, "path_changed", ""))

    def test_write_with_content_that_cannot_be_encoded_is_refused_before_the_file_is_touched(self):
        call = ToolCall("write_file", {"path": "notes.txt", "content": "\ud800"})
        approvals = {}
        grant_approval(call, approvals)
        self.assertEqual(evaluate_call(call, LAB_TOOLS, str(self.workspace), approvals).reason, "schema_invalid")
        self.assertEqual((self.workspace / "notes.txt").read_text(encoding="utf-8"), "alpha\nbeta retention\n")

    def test_path_with_a_nul_byte_is_denied_and_logged(self):
        verdict, result = self.session.submit(ToolCall("read_file", {"path": "notes.txt\x00"}))
        self.assertEqual((verdict.reason, result), ("path_outside_workspace", None))
        self.assertEqual(self.lines()[-1]["reason"], "path_outside_workspace")

    def test_no_descriptor_is_left_open_when_a_write_fails(self):
        call = ToolCall("write_file", {"path": "summary.md", "content": "text"})
        approvals = {}
        grant_approval(call, approvals)
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), approvals)
        before = len(os.listdir("/dev/fd"))
        with mock.patch.object(tool_session.os, "ftruncate", side_effect=OSError(5, "I/O error")):
            self.assertEqual(execute(call, verdict, str(self.workspace)).error, "os_error")
        with mock.patch.object(tool_session.os, "fstat", side_effect=OSError(5, "I/O error")):
            self.assertEqual(execute(call, verdict, str(self.workspace)).error, "os_error")
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_write_refuses_a_symlink_swapped_in_after_the_check(self):
        call = ToolCall("write_file", {"path": "summary.md", "content": "text"})
        approvals = {}
        grant_approval(call, approvals)
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), approvals)
        self.assertTrue(verdict.allowed)
        os.symlink(Path(self._tmp.name) / "outside.txt", self.workspace / "summary.md")
        result = execute(call, verdict, str(self.workspace))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path_changed")
        self.assertEqual((Path(self._tmp.name) / "outside.txt").read_text(encoding="utf-8"), "SECRET-OUTSIDE\n")

    def test_read_refuses_a_final_symlink_even_inside_the_workspace(self):
        verdict = evaluate_call(ToolCall("read_file", {"path": "notes.txt"}), LAB_TOOLS, str(self.workspace), {})
        os.remove(self.workspace / "notes.txt")
        os.symlink(Path(self._tmp.name) / "outside.txt", self.workspace / "notes.txt")
        result = execute(ToolCall("read_file", {"path": "notes.txt"}), verdict, str(self.workspace))
        self.assertEqual((result.ok, result.error, result.output), (False, "path_changed", ""))


class SwapAfterCheckTest(SessionCase):
    """Swaps that happen after every path re-check: only the descriptor walk can refuse them."""

    def unchecked(self):
        # Make the execution-time re-checks see the pre-swap path, as if the swap came right after.
        return mock.patch.object(tool_session, "confine_path", lambda raw, root: os.path.join(root, raw))

    def test_final_component_swapped_for_a_link_is_refused(self):
        verdict = evaluate_call(ToolCall("read_file", {"path": "notes.txt"}), LAB_TOOLS, str(self.workspace), {})
        os.remove(self.workspace / "notes.txt")
        os.symlink(Path(self._tmp.name) / "outside.txt", self.workspace / "notes.txt")
        with self.unchecked():
            result = execute(ToolCall("read_file", {"path": "notes.txt"}), verdict, str(self.workspace))
        self.assertEqual((result.ok, result.error), (False, "path_changed"))

    def test_intermediate_directory_swapped_for_a_link_is_refused(self):
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "notes.txt").write_text("inside\n", encoding="utf-8")
        outside_dir = Path(self._tmp.name) / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "notes.txt").write_text("SECRET-OUTSIDE\n", encoding="utf-8")
        call = ToolCall("read_file", {"path": "sub/notes.txt"})
        verdict = evaluate_call(call, LAB_TOOLS, str(self.workspace), {})
        os.rename(self.workspace / "sub", self.workspace / "sub-old")
        os.symlink(outside_dir, self.workspace / "sub")
        with self.unchecked():
            result = execute(call, verdict, str(self.workspace))
        self.assertEqual((result.ok, result.error), (False, "path_changed"))

    def test_workspace_swapped_for_a_link_after_the_session_started_stays_pinned(self):
        outside_dir = Path(self._tmp.name) / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "notes.txt").write_text("SECRET-OUTSIDE\n", encoding="utf-8")
        os.rename(self.workspace, Path(self._tmp.name) / "workspace-moved")
        os.symlink(outside_dir, self.workspace)
        verdict, result = self.session.submit(ToolCall("read_file", {"path": "notes.txt"}))
        self.assertTrue(verdict.allowed)
        self.assertEqual(result.output, "alpha\nbeta retention\n")

    def test_closed_session_refuses_instead_of_opening_the_workspace_again(self):
        self.session.close()
        call = ToolCall("read_file", {"path": "notes.txt"})
        for action in (self.session.submit, self.session.approve, self.session.withdraw):
            with self.subTest(action=action.__name__), self.assertRaises(ValueError):
                action(call)
        self.assertFalse(self.audit_path.exists())


class FileKindTest(SessionCase):
    def test_hard_link_to_an_outside_file_is_neither_read_nor_written(self):
        outside = Path(self._tmp.name) / "outside.txt"
        os.link(outside, self.workspace / "hosts")
        read = self.session.submit(ToolCall("read_file", {"path": "hosts"}))[1]
        write = ToolCall("write_file", {"path": "hosts", "content": "overwritten"})
        self.session.approve(write)
        written = self.session.submit(write)[1]
        self.assertEqual((read.error, written.error), ("linked_file", "linked_file"))
        self.assertEqual(outside.read_text(encoding="utf-8"), "SECRET-OUTSIDE\n")

    def test_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.workspace / "pipe")
        self.assertEqual(self.session.submit(ToolCall("read_file", {"path": "pipe"}))[1].error, "not_a_file")
        self.assertEqual(
            self.session.submit(ToolCall("run", {"command": "wc", "args": ["-l", "pipe"]}))[1].error, "not_a_file"
        )

    def test_unlisted_tool_name_is_not_written_to_the_log(self):
        verdict, result = self.session.submit(ToolCall("exfil-SECRET-NAME", {}))
        self.assertEqual((verdict.reason, result), ("tool_not_allowed", None))
        self.assertNotIn("exfil-SECRET-NAME", self.audit_path.read_text(encoding="utf-8"))
        self.assertEqual(self.lines()[0]["tool"], "unlisted")


class SessionTest(SessionCase):
    def test_allowed_call_is_run_and_logged_as_decision_then_execution(self):
        verdict, result = self.session.submit(ToolCall("read_file", {"path": "notes.txt"}))
        self.assertTrue(verdict.allowed and result.ok)
        decision, executed = self.lines()
        self.assertEqual((decision["event"], decision["decision"]), ("decision", "allow"))
        self.assertEqual(executed["event"], "executed")
        self.assertEqual(executed["call_id"], decision["call_id"])
        self.assertEqual(executed["output_bytes"], len("alpha\nbeta retention\n".encode()))

    def test_denied_call_is_not_run(self):
        verdict, result = self.session.submit(ToolCall("read_file", {"path": "../outside.txt"}))
        self.assertEqual((verdict.reason, result), ("path_outside_workspace", None))
        self.assertEqual([line["event"] for line in self.lines()], ["decision"])

    def test_approval_is_used_once_and_withdrawal_is_logged(self):
        write = ToolCall("write_file", {"path": "summary.md", "content": "one"})
        other = ToolCall("write_file", {"path": "summary.md", "content": "two"})
        approval_id = self.session.approve(write)
        self.assertEqual(self.session.submit(other)[0].reason, "approval_required")
        self.assertTrue(self.session.submit(write)[0].allowed)
        self.assertEqual(self.session.submit(write)[0].reason, "approval_required")
        second_id = self.session.approve(other)
        self.assertEqual(self.session.withdraw(other), second_id)
        self.assertEqual(self.session.submit(other)[0].reason, "approval_required")
        self.assertIsNone(self.session.withdraw(other))
        events = [(line["event"], line.get("approval_id")) for line in self.lines()]
        self.assertIn(("approved", approval_id), events)
        self.assertIn(("withdrawn", second_id), events)
        self.assertEqual((self.workspace / "summary.md").read_text(encoding="utf-8"), "one")

    def test_log_lines_carry_no_argument_values(self):
        values = ["notes.txt", "retention", "summary-secret-content", "summary.md"]
        self.session.submit(ToolCall("run", {"command": "grep", "args": ["-n", "retention", "notes.txt"]}))
        write = ToolCall("write_file", {"path": "summary.md", "content": "summary-secret-content"})
        self.session.approve(write)
        self.session.submit(write)
        text = self.audit_path.read_text(encoding="utf-8")
        for value in values:
            self.assertNotIn(value, text)


class ExtraToolTest(SessionCase):
    """Tools that are not file tools run through a runner the session is given, under the same rules."""

    def setUp(self):
        super().setUp()
        self.calls = []

        def search(arguments):
            self.calls.append(arguments)
            if arguments["query"] == "missing":
                raise LookupError(arguments["query"])
            if arguments["query"] == "broken":
                raise RuntimeError("store down")
            if arguments["query"] == "odd":
                return {"not": "text"}
            return "제15조 hit"

        def publish(arguments):
            self.calls.append(arguments)
            return "published"

        self.tools = {
            **LAB_TOOLS,
            "search": ToolSpec(_object_schema(["query"], query={"type": "string", "maxLength": 50})),
            "publish": ToolSpec(_object_schema(["text"], text={"type": "string", "maxLength": 50}), side_effect=True),
        }
        self.extra = ToolSession(str(self.workspace), str(self.audit_path), tools=self.tools, runners={"search": search, "publish": publish})
        self.addCleanup(self.extra.close)

    def test_a_read_only_extra_tool_runs_once_and_is_logged(self):
        verdict, result = self.extra.submit(ToolCall("search", {"query": "retention"}))
        self.assertEqual((verdict.reason, result.ok, result.output), ("allowed", True, "제15조 hit"))
        self.assertEqual(self.calls, [{"query": "retention"}])
        self.assertEqual([(r["tool"], r["event"]) for r in self.lines()], [("search", "decision"), ("search", "executed")])
        self.assertNotIn("retention", self.audit_path.read_text(encoding="utf-8"))

    def test_runner_failures_become_execution_errors(self):
        for query, error in (("missing", "not_found"), ("broken", "tool_error"), ("odd", "tool_error")):
            with self.subTest(query=query):
                _, result = self.extra.submit(ToolCall("search", {"query": query}))
                self.assertEqual((result.ok, result.error), (False, error))
        self.assertEqual(reconcile_audit(str(self.audit_path), ["missing", "broken"], self.tools)["violations"], [])

    def test_schema_and_approval_rules_apply_before_the_runner(self):
        self.assertEqual(self.extra.submit(ToolCall("search", {"query": "x" * 51}))[0].reason, "schema_invalid")
        publish = ToolCall("publish", {"text": "memo"})
        self.assertEqual(self.extra.submit(publish)[0].reason, "approval_required")
        self.assertEqual(self.calls, [])
        self.extra.approve(publish)
        self.assertEqual(self.extra.submit(publish)[1].output, "published")
        self.assertEqual(self.calls, [{"text": "memo"}])
        report = reconcile_audit(str(self.audit_path), ["memo"], self.tools)
        self.assertEqual((report["violations"], report["outstanding_approvals"]), ([], 0))

    def test_a_listed_tool_without_a_runner_is_a_session_error(self):
        tools = {**LAB_TOOLS, "search": self.tools["search"]}
        with self.assertRaises(ValueError):
            ToolSession(str(self.workspace), str(self.audit_path), tools=tools)


class ReconcileTest(SessionCase):
    VALUES = ["notes.txt", "summary.md", "draft-content-xyz", "../outside.txt"]

    def write_clean_log(self):
        self.session.submit(ToolCall("list_dir", {"path": "."}))
        self.session.submit(ToolCall("read_file", {"path": "../outside.txt"}))
        write = ToolCall("write_file", {"path": "summary.md", "content": "draft-content-xyz"})
        self.session.approve(write)
        self.session.submit(write)
        held = ToolCall("write_file", {"path": "notes.txt", "content": "draft-content-xyz"})
        self.session.approve(held)
        self.session.withdraw(held)

    def append(self, record):
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def append_raw(self, text):
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    def violations(self, values=()):
        return reconcile_audit(str(self.audit_path), list(values))["violations"]

    def test_clean_log_reconciles(self):
        self.write_clean_log()
        report = reconcile_audit(str(self.audit_path), self.VALUES)
        self.assertEqual(report["violations"], [])
        self.assertEqual(
            (report["approved"], report["withdrawn"], report["allowed"], report["denied"], report["executed"]),
            (2, 1, 2, 1, 2),
        )
        self.assertEqual(report["outstanding_approvals"], 0)

    def test_unknown_field_is_a_violation(self):
        self.write_clean_log()
        line = self.lines()[0]
        line["arguments"] = {"path": "."}
        self.append(line)
        self.assertTrue(any("unexpected field" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))

    def test_unknown_field_name_is_not_repeated_in_the_report(self):
        self.write_clean_log()
        line = self.lines()[0]
        line["synthetic-secret-value"] = 0
        self.append(line)
        report = reconcile_audit(str(self.audit_path), ["synthetic-secret-value"])
        self.assertIn("line 9: unexpected fields: 1", report["violations"])
        self.assertNotIn("synthetic-secret-value", json.dumps(report))

    def test_allowed_unlisted_tool_is_a_violation(self):
        self.write_clean_log()
        lines = self.lines()
        decision = dict(next(line for line in lines if line["event"] == "decision" and line["tool"] == "list_dir"))
        decision.update(tool="unlisted", call_id="8" * 16)
        self.append(decision)
        executed = dict(next(line for line in lines if line["event"] == "executed"))
        executed.update(tool="unlisted", call_id="8" * 16)
        self.append(executed)
        self.assertIn("line 9: reason does not fit the tool", self.violations())

    def test_approval_id_on_a_tool_without_side_effects_is_a_violation(self):
        self.write_clean_log()
        lines = self.lines()
        approved = dict(next(line for line in lines if line["event"] == "approved"))
        approved.update(tool="read_file", approval_id="9" * 16)
        self.append(approved)
        decision = dict(next(line for line in lines if line["event"] == "decision" and line["tool"] == "list_dir"))
        decision.update(tool="read_file", approval_id="9" * 16, call_id="a" * 16)
        self.append(decision)
        executed = dict(next(line for line in lines if line["event"] == "executed"))
        executed.update(tool="read_file", call_id="a" * 16)
        self.append(executed)
        self.assertIn("line 10: approval id on a tool without side effects", self.violations())

    def test_withdrawal_under_another_tool_is_a_violation(self):
        self.write_clean_log()
        approved = dict(next(line for line in self.lines() if line["event"] == "approved"))
        approved["approval_id"] = "b" * 16
        self.append(approved)
        withdrawn = dict(next(line for line in self.lines() if line["event"] == "withdrawn"))
        withdrawn.update(approval_id="b" * 16, tool="read_file")
        self.append(withdrawn)
        self.assertIn("line 10: withdrawn approval was for another tool", self.violations())

    def test_approval_used_for_arguments_of_another_size_is_a_violation(self):
        self.write_clean_log()
        lines = self.lines()
        approved = dict(next(line for line in lines if line["event"] == "approved"))
        approved.update(approval_id="c" * 16, argument_bytes=approved["argument_bytes"] + 1)
        self.append(approved)
        decision = dict(next(line for line in lines if line["event"] == "decision" and "approval_id" in line))
        decision.update(approval_id="c" * 16, call_id="d" * 16)
        self.append(decision)
        executed = dict(next(line for line in lines if line["event"] == "executed"))
        executed["call_id"] = "d" * 16
        self.append(executed)
        self.assertIn("line 10: approval was for arguments of another size", self.violations())

    def test_short_values_are_counted_as_skipped(self):
        self.write_clean_log()
        report = reconcile_audit(str(self.audit_path), ["sh", "-c", ".", "notes.txt"])
        self.assertEqual((report["values_checked"], report["values_skipped"]), (1, 3))

    def test_command_fails_on_an_approval_left_open(self):
        self.write_clean_log()
        self.session.approve(ToolCall("write_file", {"path": "late.md", "content": "x"}))
        script = Path(__file__).resolve().parents[1] / "agent" / "audit_check.py"
        done = subprocess.run([sys.executable, str(script), str(self.audit_path)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["outstanding_approvals"], 1)

    def test_argument_value_in_the_log_is_a_violation(self):
        self.write_clean_log()
        line = self.lines()[0]
        line["reason"] = "draft-content-xyz"
        self.append(line)
        report = reconcile_audit(str(self.audit_path), self.VALUES)
        self.assertTrue(any("argument value" in v for v in report["violations"]))
        self.assertNotIn("draft-content-xyz", json.dumps(report))

    def test_execution_without_an_allow_decision_is_a_violation(self):
        self.write_clean_log()
        executed = next(line for line in self.lines() if line["event"] == "executed")
        executed["call_id"] = "0" * 16
        self.append(executed)
        self.assertTrue(any("no allow decision" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))

    def test_second_execution_of_one_call_is_a_violation(self):
        self.write_clean_log()
        executed = next(line for line in self.lines() if line["event"] == "executed")
        self.append(executed)
        self.assertTrue(any("executed twice" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))

    def test_allow_with_a_withdrawn_approval_is_a_violation(self):
        self.write_clean_log()
        lines = self.lines()
        withdrawn_id = next(line["approval_id"] for line in lines if line["event"] == "withdrawn")
        decision = dict(next(line for line in lines if line["event"] == "decision" and "approval_id" in line))
        decision["approval_id"] = withdrawn_id
        decision["call_id"] = "1" * 16
        self.append(decision)
        self.assertTrue(any("withdrawn" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))

    def test_reused_approval_is_a_violation(self):
        self.write_clean_log()
        decision = dict(next(line for line in self.lines() if line["event"] == "decision" and "approval_id" in line))
        decision["call_id"] = "2" * 16
        self.append(decision)
        self.assertTrue(any("used twice" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))

    def test_allowed_call_without_execution_is_a_violation(self):
        self.write_clean_log()
        decision = dict(next(line for line in self.lines() if line["event"] == "decision" and line["decision"] == "allow"))
        decision["call_id"] = "3" * 16
        decision.pop("approval_id", None)
        self.append(decision)
        self.assertTrue(any("never executed" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))

    def test_side_effect_allowed_without_approval_is_a_violation(self):
        self.write_clean_log()
        decision = dict(next(line for line in self.lines() if line["event"] == "decision" and "approval_id" in line))
        decision.pop("approval_id")
        decision["call_id"] = "4" * 16
        self.append(decision)
        executed = dict(next(line for line in self.lines() if line["event"] == "executed"))
        executed["call_id"] = "4" * 16
        self.append(executed)
        self.assertTrue(any("without approval" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))

    def test_duplicate_field_hiding_a_value_is_a_violation(self):
        self.write_clean_log()
        self.append_raw(
            '{"argument_bytes": 0, "call_id": "5555555555555555", "decision": "deny", "event": "decision", '
            '"reason": "\\u0053ECRET", "reason": "schema_invalid", "tool": "read_file", "ts": "2026-10-22T00:00:00.000+00:00"}'
        )
        self.assertIn("line 9: duplicate field", self.violations(["SECRET"]))

    def test_line_not_in_canonical_form_is_a_violation(self):
        self.write_clean_log()
        line = self.lines()[0]
        self.append_raw(json.dumps(line, sort_keys=True, indent=1).replace("\n", ""))
        self.assertTrue(any("canonical form" in v for v in self.violations()))

    def test_malformed_field_types_are_reported_without_stopping(self):
        self.write_clean_log()
        self.append_raw('{"event": []}')
        self.append_raw('{"event": "approved", "tool": []}')
        decision = dict(next(line for line in self.lines() if line["event"] == "decision" and "approval_id" in line))
        self.append(decision)
        found = self.violations()
        self.assertIn("line 9: unknown event", found)
        self.assertTrue(any(v.startswith("line 10:") for v in found))
        self.assertTrue(any(v.startswith("line 11:") and "decided twice" in v for v in found))
        self.assertFalse(any("never executed" in v for v in found))

    def test_value_that_matches_the_log_vocabulary_is_a_coincidence_not_a_violation(self):
        (self.workspace / "read_file").write_text("x\n", encoding="utf-8")
        self.session.submit(ToolCall("read_file", {"path": "read_file"}))
        self.session.submit(ToolCall("run", {"command": "grep", "args": ["-c", "true", "notes.txt"]}))
        report = reconcile_audit(str(self.audit_path), ["read_file", "true", "notes.txt"])
        self.assertEqual(report["violations"], [])
        self.assertGreater(report["value_coincidences"], 0)

    def test_approval_granted_twice_is_a_violation(self):
        self.write_clean_log()
        self.append(next(line for line in self.lines() if line["event"] == "approved"))
        self.assertTrue(any("granted twice" in v for v in self.violations()))

    def test_approval_used_for_another_tool_is_a_violation(self):
        self.write_clean_log()
        lines = self.lines()
        approved = dict(next(line for line in lines if line["event"] == "approved"))
        approved["approval_id"] = "6" * 16
        self.append(approved)
        decision = dict(next(line for line in lines if line["event"] == "decision" and line["tool"] == "list_dir"))
        decision.update(approval_id="6" * 16, call_id="7" * 16)
        self.append(decision)
        executed = dict(next(line for line in lines if line["event"] == "executed"))
        executed["call_id"] = "7" * 16
        self.append(executed)
        self.assertTrue(any("another tool" in v for v in self.violations()))

    def test_unparseable_line_is_a_violation(self):
        self.write_clean_log()
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write("not json\n")
        self.assertTrue(any("not a JSON object" in v for v in reconcile_audit(str(self.audit_path), [])["violations"]))


class ToolSetCommandTest(unittest.TestCase):
    """The command checks a log against the tool set that wrote it: the review agent's or the RAG agent's."""

    SCRIPT = Path(__file__).resolve().parents[1] / "agent" / "audit_check.py"
    # A copy of the RAG agent's evidence log, so the test also runs where evidence/ is not mounted.
    RAG_LOG = Path(__file__).resolve().parent / "fixtures" / "rag-agent-20261022T082220Z-30c48f.jsonl"

    def run_check(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args], capture_output=True, text=True)

    def test_the_rag_tool_set_is_the_lab_tools_plus_two_read_only_searches(self):
        from tool_boundary import RAG_TOOLS
        self.assertEqual(set(RAG_TOOLS) - set(LAB_TOOLS), {"search_law", "read_article"})
        self.assertFalse(RAG_TOOLS["search_law"].side_effect or RAG_TOOLS["read_article"].side_effect)

    def test_a_rag_log_passes_with_the_rag_tool_set(self):
        done = self.run_check("--tool-set", "rag", str(self.RAG_LOG))
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(json.loads(done.stdout)["violations"], [])

    def test_a_rag_log_checked_against_the_lab_tools_names_the_unexpected_tools(self):
        done = self.run_check(str(self.RAG_LOG))
        self.assertEqual(done.returncode, 1)
        self.assertTrue(all("unexpected tool" in v for v in json.loads(done.stdout)["violations"]))

    def test_an_unknown_tool_set_is_refused(self):
        self.assertEqual(self.run_check("--tool-set", "other", str(self.RAG_LOG)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
