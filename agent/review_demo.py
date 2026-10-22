"""Acceptance demo: the document-review workflow, run for real behind the tool boundary.

The tests prove each decision; this demo runs the whole case end to end and leaves files a
reviewer can check without trusting the code: an audit log and an evidence summary. The human
side (approve, withdraw) is scripted here, so every run is the same.

Steps, each with an expected outcome:

1. The permitted workflow completes: six reads and searches run, the summary waits for an
   approval, a person approves those exact arguments, the summary is written.
2. An approval covers only its arguments: a summary with other content is held.
3. An approval is used once: the same call a second time is held again.
4. An approval can be withdrawn before use: the call is then held.
5. The instruction planted in a reviewed document leads nowhere, even when approved by mistake:
   the credentials next to the workspace are never read and nothing is written outside it.
6. A command off the list is refused, and shell syntax in a search pattern stays a pattern.

Then the audit log is reconciled (audit_check.py) against every argument value used.
Writes <AUDIT_DIR>/review-demo-<UTC>-<random>.jsonl and <EVIDENCE_DIR>/review-demo-<UTC>-<random>.json,
both created new, so two runs never share a file.

The evidence lists, for each step, the `call_id`s of its calls, so a reviewer can find those
decisions and executions in the audit log. The calls themselves are in this file; a reviewer who
reruns the demo gets the same sequence of tools, decisions, reasons and outcomes (the ids and
timestamps differ).
Standard library only.
"""

import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from audit_check import reconcile_audit
from tool_boundary import ToolCall
from tool_session import ToolSession

FIXTURES = os.environ.get("FIXTURES", str(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "review_workspace"))
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/evidence")
AUDIT_DIR = os.environ.get("AUDIT_DIR", "/logs")
SECRET = "DB_PASSWORD=demo-credential-not-for-the-agent"

SUMMARY = ToolCall("write_file", {"path": "summary.md", "content": "Recordings: one year. Correspondence: five years."})
SUMMARY_OTHER = ToolCall("write_file", {"path": "summary.md", "content": "Recordings: forever."})
SUMMARY_ONCE = ToolCall("write_file", {"path": "summary-v2.md", "content": "Exceptions need written approval."})
SUMMARY_WITHDRAWN = ToolCall("write_file", {"path": "summary-v3.md", "content": "Draft to discard."})

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

OFF_LIST = ToolCall("run", {"command": "sh", "args": ["-c", "cat ../credentials.txt"]})


def _shell_syntax_call(marker: Path) -> ToolCall:
    # The marker path is absolute and outside the workspace, where any shell would create it.
    return ToolCall("run", {"command": "grep", "args": ["-c", "--", f"$(touch {marker}); touch {marker}", "meeting-notes.md"]})


def _argument_values(calls) -> list:
    values = []
    for call in calls:
        for value in call.arguments.values():
            values.extend(value if isinstance(value, list) else [value])
    return values


def _step(number: int, name: str, checks: dict, call_ids: list) -> dict:
    return {"step": number, "name": name, "pass": all(checks.values()), "checks": checks, "call_ids": list(call_ids)}


def run_demo(fixtures: str, evidence_dir: str, audit_dir: str) -> dict:
    """Run steps 1 to 6 and the reconciliation; return the evidence record."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    audit_path = Path(audit_dir) / f"review-demo-{stamp}.jsonl"
    audit_path.touch(exist_ok=False)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        root = Path(tmp)
        workspace = root / "workspace"
        shutil.copytree(fixtures, workspace)
        (root / "credentials.txt").write_text(SECRET + "\n", encoding="utf-8")
        (root / "shared").mkdir()
        session = stack.enter_context(ToolSession(str(workspace), str(audit_path)))
        outputs = []
        steps = []
        trace = []

        def submit(call):
            outcome = session.submit(call)
            trace.append(session.last_call_id)
            return outcome

        def step(number, name, checks):
            steps.append(_step(number, name, checks, trace))
            trace.clear()

        benign = [submit(call) for call in BENIGN_CALLS]
        outputs += [result.output for _, result in benign if result]
        held = submit(SUMMARY)[0].reason
        session.approve(SUMMARY)
        written = submit(SUMMARY)
        step(1, "permitted workflow completes with one approval", {
            "six calls allowed and run": all(v.allowed and r is not None and r.ok for v, r in benign),
            "summary held before approval": held == "approval_required",
            "summary written after approval": written[0].allowed and written[1].ok
            and (workspace / "summary.md").read_text(encoding="utf-8") == SUMMARY.arguments["content"],
        })

        session.approve(SUMMARY_OTHER)
        session.approve(SUMMARY)
        other_held = submit(ToolCall("write_file", {"path": "summary.md", "content": "Recordings: one month."}))[0].reason
        cleaned = session.withdraw(SUMMARY_OTHER) is not None and session.withdraw(SUMMARY) is not None
        step(2, "an approval covers only its exact arguments", {
            "other content held": other_held == "approval_required",
            "unused approvals withdrawn": cleaned,
            "summary unchanged": (workspace / "summary.md").read_text(encoding="utf-8") == SUMMARY.arguments["content"],
        })

        session.approve(SUMMARY_ONCE)
        first = submit(SUMMARY_ONCE)[0].reason
        second = submit(SUMMARY_ONCE)[0].reason
        step(3, "an approval is used once", {
            "first use allowed": first == "allowed",
            "second use held": second == "approval_required",
        })

        session.approve(SUMMARY_WITHDRAWN)
        withdrawn = session.withdraw(SUMMARY_WITHDRAWN) is not None
        after = submit(SUMMARY_WITHDRAWN)[0].reason
        step(4, "an approval can be withdrawn before use", {
            "withdrawal recorded": withdrawn,
            "call held after withdrawal": after == "approval_required",
            "file not written": not (workspace / "summary-v3.md").exists(),
        })

        planted = []
        for call in PLANTED_CALLS:
            session.approve(call)
            planted.append(submit(call))
        stray = [session.withdraw(call) for call in PLANTED_CALLS]
        outputs += [result.output for _, result in planted if result]
        step(5, "the planted instruction leads nowhere, even when approved", {
            "all three refused for their path": all(v.reason == "path_outside_workspace" for v, _ in planted),
            "credentials never in any output": all(SECRET not in output for output in outputs),
            "nothing written outside the workspace": not any((root / "shared").iterdir()),
            "approvals given by mistake withdrawn": all(approval_id is not None for approval_id in stray),
        })

        marker = root / "pwned-marker"
        shell_syntax = _shell_syntax_call(marker)
        off_list = submit(OFF_LIST)[0].reason
        syntax_verdict, syntax_result = submit(shell_syntax)
        step(6, "a command off the list is refused; shell syntax stays data", {
            "sh refused": off_list == "command_not_allowed",
            "pattern searched literally": syntax_verdict.allowed and syntax_result.ok and syntax_result.output == "0\n",
            "no file created by the pattern": not marker.exists(),
        })

        calls = [*BENIGN_CALLS, SUMMARY, SUMMARY_OTHER, SUMMARY_ONCE, SUMMARY_WITHDRAWN, *PLANTED_CALLS, OFF_LIST, shell_syntax]
        audit = reconcile_audit(str(audit_path), [*_argument_values(calls), SECRET, "Recordings: one month."])

    evidence = {
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}, Python {platform.python_version()}",
        "steps": steps,
        "audit_log": audit_path.name,
        "audit_log_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "audit": audit,
        "passed": all(step["pass"] for step in steps) and not audit["violations"] and audit["outstanding_approvals"] == 0,
    }
    evidence_path = Path(evidence_dir) / f"review-demo-{stamp}.json"
    with open(evidence_path, "x", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    evidence["evidence_file"] = evidence_path.name
    return evidence


def main() -> int:
    evidence = run_demo(FIXTURES, EVIDENCE_DIR, AUDIT_DIR)
    for step in evidence["steps"]:
        print(f"{'PASS' if step['pass'] else 'FAIL'} {step['step']}. {step['name']}")
    audit = evidence["audit"]
    print(f"audit: {audit['lines']} lines, {len(audit['violations'])} violations, "
          f"{audit['values_checked']} argument values checked ({audit['value_coincidences']} coincidences, "
          f"{audit['values_skipped']} too short to search), "
          f"{audit['outstanding_approvals']} approvals open")
    print(f"evidence: {evidence['evidence_file']} · audit log: {evidence['audit_log']}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
