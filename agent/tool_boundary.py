"""Tool boundary: the check between a model's tool call and its execution.

A JSON schema on a tool says what shape an argument has, not what it may touch: a
`"type": "string"` path accepts `../outside.txt`, and a string handed to a shell runs
as a command. This boundary adds what the schema cannot express, in this order:

1. the tool is on the lab's list and the arguments match its schema exactly;
2. every path resolves (symlinks included) inside the workspace;
3. commands run as an argv list, never through a shell, with per-command option
   allowlists so an option cannot smuggle in a file outside the workspace;
4. a tool with side effects runs only with a human approval for these exact
   arguments; each approval is used once, can be withdrawn before use, and carries a
   random id so the audit log can match it to the call it allowed.

Standard library only, like the gateway.
"""

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

MAX_STRING = 4096


@dataclass(frozen=True)
class ToolSpec:
    schema: dict
    path_params: tuple = ()
    side_effect: bool = False
    command_options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    resolved_path: Optional[str] = None
    argv: Optional[list] = None
    approval_id: Optional[str] = None
    # run: every non-option argument as resolved at the check, in order (grep's pattern first).
    resolved_operands: tuple = ()


def _object_schema(required, **properties):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string", "maxLength": MAX_STRING}
STRINGS = {"type": "array", "items": STRING}

LAB_TOOLS = {
    "read_file": ToolSpec(_object_schema(["path"], path=STRING), path_params=("path",)),
    "list_dir": ToolSpec(_object_schema(["path"], path=STRING), path_params=("path",)),
    "write_file": ToolSpec(
        _object_schema(["path", "content"], path=STRING, content=STRING),
        path_params=("path",),
        side_effect=True,
    ),
    # Every command on run's list only reads, so run needs no approval. A command that writes
    # or deletes must not be added without turning approval back on.
    "run": ToolSpec(
        _object_schema(["command", "args"], command=STRING, args=STRINGS),
        command_options={
            "grep": frozenset({"-n", "-i", "-c", "-l", "-w", "-v", "--"}),
            "wc": frozenset({"-l", "-w", "-c", "--"}),
        },
    ),
}

# The RAG agent (rag_agent.py) adds two read-only searches over the statute to the lab tools.
RAG_TOOLS = {
    **LAB_TOOLS,
    "search_law": ToolSpec(_object_schema(["query"], query={"type": "string", "maxLength": 500})),
    "read_article": ToolSpec(_object_schema(["id"], id={"type": "string", "maxLength": 20})),
}


def _encodable(value: str) -> bool:
    # A lone surrogate cannot be written to a file or passed to a program.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def matches_schema(value, schema: dict) -> bool:
    """A strict subset of JSON Schema: object, string and array of strings."""
    kind = schema.get("type")
    if kind == "string":
        return isinstance(value, str) and len(value) <= schema.get("maxLength", MAX_STRING) and _encodable(value)
    if kind == "array":
        return isinstance(value, list) and all(matches_schema(item, schema["items"]) for item in value)
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema["properties"]
        if set(schema.get("required", ())) - set(value):
            return False
        if not schema.get("additionalProperties", True) and set(value) - set(properties):
            return False
        return all(matches_schema(value[key], properties[key]) for key in value if key in properties)
    return False


def confine_path(raw: str, workspace: str) -> Optional[str]:
    """Resolve `raw` against the workspace; None if the real path lands outside it."""
    root = os.path.realpath(workspace)
    try:
        resolved = os.path.realpath(os.path.join(root, raw))
    except ValueError:
        # A NUL byte cannot name a file.
        return None
    if resolved == root or resolved.startswith(root + os.sep):
        return resolved
    return None


def fingerprint_call(call: ToolCall) -> str:
    """What a human approves: this tool with exactly these arguments."""
    canonical = json.dumps({"tool": call.name, "arguments": call.arguments}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def grant_approval(call: ToolCall, approvals: dict) -> str:
    """Record a human approval for exactly this call and return its id."""
    approval_id = secrets.token_hex(8)
    approvals[fingerprint_call(call)] = approval_id
    return approval_id


def withdraw_approval(call: ToolCall, approvals: dict) -> bool:
    """Withdraw an unused approval; False if there was none."""
    return approvals.pop(fingerprint_call(call), None) is not None


def evaluate_call(call: ToolCall, tools: dict, workspace: str, approvals: dict) -> Verdict:
    """Decide one tool call. A matching approval is consumed when the call is allowed."""
    spec = tools.get(call.name)
    if spec is None:
        return Verdict(False, "tool_not_allowed")
    if not matches_schema(call.arguments, spec.schema):
        return Verdict(False, "schema_invalid")

    resolved_path = None
    for param in spec.path_params:
        resolved_path = confine_path(call.arguments[param], workspace)
        if resolved_path is None:
            return Verdict(False, "path_outside_workspace")

    argv = None
    if spec.command_options:
        command = call.arguments["command"]
        options = spec.command_options.get(command)
        if options is None:
            return Verdict(False, "command_not_allowed")
        operands_only = False
        operands = []
        for arg in call.arguments["args"]:
            if not operands_only and arg.startswith("-"):
                if arg not in options:
                    return Verdict(False, "option_not_allowed")
                operands_only = arg == "--"
                continue
            operands.append(confine_path(arg, workspace))
            if operands[-1] is None:
                return Verdict(False, "path_outside_workspace")
        argv = [command, *call.arguments["args"]]

    approval_id = None
    if spec.side_effect:
        approval_id = approvals.pop(fingerprint_call(call), None)
        if approval_id is None:
            return Verdict(False, "approval_required")

    return Verdict(
        True,
        "allowed",
        resolved_path=resolved_path,
        argv=argv,
        approval_id=approval_id,
        resolved_operands=tuple(operands) if spec.command_options else (),
    )


def _argument_bytes(call: ToolCall) -> int:
    return len(json.dumps(call.arguments, sort_keys=True).encode())


def audit_record(call: ToolCall, verdict: Verdict) -> dict:
    """One audit line per decision: sizes and outcome, never argument values."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tool": call.name,
        "decision": "allow" if verdict.allowed else "deny",
        "reason": verdict.reason,
        "argument_bytes": _argument_bytes(call),
    }
    if verdict.approval_id:
        record["approval_id"] = verdict.approval_id
    return record


def approval_record(call: ToolCall, approval_id: str) -> dict:
    """One audit line per approval, matched to the decision by approval_id."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tool": call.name,
        "event": "approved",
        "approval_id": approval_id,
        "argument_bytes": _argument_bytes(call),
    }
