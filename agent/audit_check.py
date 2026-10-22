"""Reconcile a tool session's audit log file.

Reads the JSONL file a ToolSession wrote and reports every line that breaks one of these:

1. Each line is a JSON object without duplicate fields, written in the session's canonical form,
   with only the fields its event allows, and every field is an enumerated word, a count, a
   timestamp or a random id of fixed form. So a line that passes cannot carry an argument value
   as text. A number or an id could still encode one: this check cannot tell where it came from.
2. As a cross-check, the given argument values are searched for in every line. A match in a
   line that failed (1) is a violation; a match in a line that passed is a coincidence with the
   log's own vocabulary (a file named `read_file`, a search for `true`) and is only counted.
   Values shorter than MIN_VALUE_LENGTH are not searched for, only counted as skipped.
3. An approval is granted once, then used once or withdrawn for the same tool; never used after
   withdrawal, and used only by a call whose arguments have the approved size.
4. A tool with side effects is allowed only with an approval for that tool, and a tool without
   side effects never carries one. An unlisted tool is only ever denied as not allowed.
5. Every allowed call is executed exactly once, and nothing runs without an allow decision.

    python3 agent/audit_check.py [--tool-set lab|rag] logs/audit.jsonl [argument values...]

The tool set is the one the session ran with: `lab` (the default, review agent and demo) or
`rag` (the RAG agent, which adds two read-only searches).

Run it on the log of a closed session: the command fails on any violation and on any approval
left open. It checks the recorded events against each other; it cannot show that the log is
complete, since an action that was never logged leaves nothing to check. Standard library only. Violations name line numbers, value indexes and field counts,
never the values or unknown field names.
"""

import argparse
import json
import re

from tool_boundary import LAB_TOOLS, RAG_TOOLS
from tool_session import EXECUTION_ERRORS, UNLISTED_TOOL

REASONS = frozenset({
    "allowed",
    "tool_not_allowed",
    "schema_invalid",
    "path_outside_workspace",
    "command_not_allowed",
    "option_not_allowed",
    "approval_required",
})
FIELDS = {
    "approved": ({"ts", "tool", "event", "approval_id", "argument_bytes"}, set()),
    "withdrawn": ({"ts", "tool", "event", "approval_id"}, set()),
    "decision": ({"ts", "tool", "event", "call_id", "decision", "reason", "argument_bytes"}, {"approval_id"}),
    "executed": ({"ts", "tool", "event", "call_id", "ok", "output_bytes"}, {"exit_code", "error", "stderr_bytes"}),
}
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00")
RANDOM_ID = re.compile(r"[0-9a-f]{16}")
MIN_VALUE_LENGTH = 4


class DuplicateField(ValueError):
    pass


def _without_duplicates(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise DuplicateField()
    return dict(pairs)


def _word(value, allowed) -> bool:
    return isinstance(value, str) and value in allowed


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _field_problems(record: dict, tools: dict) -> list:
    event = record.get("event")
    if not _word(event, FIELDS):
        return ["unknown event"]
    required, optional = FIELDS[event]
    problems = [f"missing field {name}" for name in sorted(required - set(record))]
    # An unknown field name is text the writer chose, so only the count is reported.
    unexpected = len(set(record) - required - optional)
    if unexpected:
        problems.append(f"unexpected fields: {unexpected}")
    checks = {
        "ts": lambda v: isinstance(v, str) and TIMESTAMP.fullmatch(v) is not None,
        "tool": lambda v: _word(v, tools) or v == UNLISTED_TOOL,
        "approval_id": lambda v: isinstance(v, str) and RANDOM_ID.fullmatch(v) is not None,
        "call_id": lambda v: isinstance(v, str) and RANDOM_ID.fullmatch(v) is not None,
        "decision": lambda v: _word(v, ("allow", "deny")),
        "reason": lambda v: _word(v, REASONS),
        "argument_bytes": _is_count,
        "output_bytes": _is_count,
        "stderr_bytes": _is_count,
        "ok": lambda v: isinstance(v, bool),
        "exit_code": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "error": lambda v: _word(v, EXECUTION_ERRORS),
    }
    for name, check in checks.items():
        if name in record and not check(record[name]):
            problems.append(f"unexpected {name}")
    return problems


def reconcile_audit(path: str, argument_values, tools: dict = LAB_TOOLS) -> dict:
    """Check one audit log file. Returns counts and a list of violations (empty when clean)."""
    with open(path, encoding="utf-8") as f:
        raw_lines = f.read().splitlines()

    violations = []
    approvals = {}  # approval_id -> {"state": open|withdrawn|used, "tool": name, "argument_bytes": n}
    decisions = {}  # call_id -> {"allowed": bool, "tool": name, "executed": bool}
    counts = {"lines": len(raw_lines), "approved": 0, "withdrawn": 0, "allowed": 0, "denied": 0, "executed": 0}

    invalid_lines = set()
    for number, raw in enumerate(raw_lines, start=1):
        try:
            record = json.loads(raw, object_pairs_hook=_without_duplicates)
        except DuplicateField:
            record = "duplicate"
        except json.JSONDecodeError:
            record = None
        if record == "duplicate":
            violations.append(f"line {number}: duplicate field")
            invalid_lines.add(number)
            continue
        if not isinstance(record, dict):
            violations.append(f"line {number}: not a JSON object")
            invalid_lines.add(number)
            continue
        problems = _field_problems(record, tools)
        if raw != json.dumps(record, sort_keys=True):
            problems.append("not in canonical form")
        violations += [f"line {number}: {problem}" for problem in problems]
        if problems:
            invalid_lines.add(number)
            continue

        event, tool = record["event"], record["tool"]
        if event == "approved":
            counts["approved"] += 1
            if record["approval_id"] in approvals:
                violations.append(f"line {number}: approval id granted twice")
            approvals[record["approval_id"]] = {"state": "open", "tool": tool, "argument_bytes": record["argument_bytes"]}
        elif event == "withdrawn":
            counts["withdrawn"] += 1
            approval = approvals.get(record["approval_id"])
            if approval is None or approval["state"] != "open":
                violations.append(f"line {number}: withdrawn without an open approval")
            elif approval["tool"] != tool:
                violations.append(f"line {number}: withdrawn approval was for another tool")
            else:
                approval["state"] = "withdrawn"
        elif event == "decision":
            allowed = record["decision"] == "allow"
            counts["allowed" if allowed else "denied"] += 1
            if allowed != (record["reason"] == "allowed"):
                violations.append(f"line {number}: decision and reason disagree")
            if (tool == UNLISTED_TOOL) != (record["reason"] == "tool_not_allowed"):
                violations.append(f"line {number}: reason does not fit the tool")
            if record["call_id"] in decisions:
                violations.append(f"line {number}: call id decided twice")
                continue
            decisions[record["call_id"]] = {"allowed": allowed, "tool": tool, "executed": False}
            approval_id = record.get("approval_id")
            side_effect = tools.get(tool) is not None and tools[tool].side_effect
            if approval_id is not None and not side_effect:
                violations.append(f"line {number}: approval id on a tool without side effects")
            if not allowed and approval_id is not None:
                violations.append(f"line {number}: denied call carries an approval id")
            if allowed and approval_id is not None:
                approval = approvals.get(approval_id)
                if approval is None:
                    violations.append(f"line {number}: allow uses an approval that was never granted")
                elif approval["state"] == "withdrawn":
                    violations.append(f"line {number}: allow uses a withdrawn approval")
                elif approval["state"] == "used":
                    violations.append(f"line {number}: approval used twice")
                elif approval["tool"] != tool:
                    violations.append(f"line {number}: approval was for another tool")
                elif approval["argument_bytes"] != record["argument_bytes"]:
                    violations.append(f"line {number}: approval was for arguments of another size")
                if approval is not None:
                    approval["state"] = "used"
            if allowed and approval_id is None and side_effect:
                violations.append(f"line {number}: side effect allowed without approval")
        elif event == "executed":
            counts["executed"] += 1
            decision = decisions.get(record["call_id"])
            if decision is None or not decision["allowed"]:
                violations.append(f"line {number}: executed with no allow decision")
            elif decision["executed"]:
                violations.append(f"line {number}: call executed twice")
            else:
                decision["executed"] = True
                if decision["tool"] != tool:
                    violations.append(f"line {number}: executed tool differs from the decision")

    for call_id, decision in decisions.items():
        if decision["allowed"] and not decision["executed"]:
            violations.append(f"call {call_id}: allowed but never executed")

    checked = skipped = coincidences = 0
    for index, value in enumerate(argument_values):
        if not isinstance(value, str) or len(value) < MIN_VALUE_LENGTH:
            skipped += 1
            continue
        checked += 1
        for number, raw in enumerate(raw_lines, start=1):
            if value not in raw:
                continue
            if number in invalid_lines:
                violations.append(f"line {number}: argument value #{index} appears in the log")
            else:
                coincidences += 1

    return {
        **counts,
        "outstanding_approvals": sum(1 for a in approvals.values() if a["state"] == "open"),
        "values_checked": checked,
        "values_skipped": skipped,
        "value_coincidences": coincidences,
        "violations": violations,
    }


TOOL_SETS = {"lab": LAB_TOOLS, "rag": RAG_TOOLS}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile a tool session's audit log file.")
    parser.add_argument("--tool-set", choices=sorted(TOOL_SETS), default="lab")
    parser.add_argument("log")
    parser.add_argument("values", nargs="*", help="argument values that must not appear in the log")
    args = parser.parse_args()
    report = reconcile_audit(args.log, args.values, TOOL_SETS[args.tool_set])
    print(json.dumps(report, indent=1))
    raise SystemExit(1 if report["violations"] or report["outstanding_approvals"] else 0)
