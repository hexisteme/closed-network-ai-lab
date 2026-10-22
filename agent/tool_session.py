"""Tool session: run what the boundary allowed, and leave an audit log file.

The boundary (tool_boundary.py) decides a call; this module runs it and writes the record.
One session is one agent in one workspace:

1. `approve` and `withdraw` are the human's side: each writes one audit line.
2. `submit` asks the boundary, writes the decision, and runs the call only if it was allowed.
   The execution writes its own line, tied to the decision by a random `call_id`.
3. The workspace directory is opened once when the session starts. Every path is resolved the
   way the boundary resolved it and refused if the result changed since the check; the result
   is then opened from that descriptor one component at a time, never following a symlink and
   never through `..`, so the walk only descends from the directory the session opened.
4. Only regular files with a single link are read or written, so a hard link to a file outside
   the workspace is refused. A write truncates the file only after that check.
5. `run` starts the program from a fixed directory list as an argv list, without a shell, with
   a two-variable environment, no stdin and a time limit. File operands are opened here, the
   same way, and handed to the program as already-open descriptors: the program runs in a fresh
   private directory where each operand name is a link to `/dev/fd/N`, so it never resolves a
   workspace path itself. It gets the options, then `--`, then the operands, each file named
   `./<its path inside the workspace>`, so no operand can read as an option. An argument that
   starts with `-` before any `--` is an option wherever it stands, as the boundary reads it.
   Every option on the boundary's lists is a flag without an argument; an option that takes
   one would need the operand split below to know about it. Output and stderr are cut to
   MAX_OUTPUT and MAX_STDERR while they are read.
6. A closed session refuses every further call, approval and withdrawal.

Audit lines hold sizes, outcomes, enumerated reasons and random ids, never argument values or
output. audit_check.py reads the file back and checks exactly that.

Standard library only, like the boundary.
"""

import contextlib
import errno
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from tool_boundary import (
    LAB_TOOLS,
    ToolCall,
    Verdict,
    approval_record,
    audit_record,
    confine_path,
    evaluate_call,
    fingerprint_call,
    grant_approval,
)

MAX_OUTPUT = 65536
MAX_STDERR = 4096
RUN_TIMEOUT = 10
RUN_PATH = "/usr/bin:/bin"
RUN_ENV = {"PATH": RUN_PATH, "LC_ALL": "C"}
# Commands whose first operand is a pattern, not a file. Every other operand is a file.
PATTERN_FIRST = frozenset({"grep"})
UNLISTED_TOOL = "unlisted"
EXECUTION_ERRORS = frozenset({
    "path_changed",
    "not_found",
    "not_a_file",
    "linked_file",
    "argv_mismatch",
    "duplicate_operand",
    "command_missing",
    "timeout",
    "os_error",
    "tool_error",
})


@dataclass(frozen=True)
class Execution:
    ok: bool
    output: str = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None
    stderr: str = ""


class Refused(Exception):
    """An execution-time check failed; the argument is one of EXECUTION_ERRORS."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _open_root(root: str) -> int:
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY)


def _checked_parts(raw: str, expected: Optional[str], root: str) -> list:
    """Components of `raw` below `root`, refused if its resolution differs from the check's.

    A component that is empty, `.` or `..` is refused too: the walk from the root descriptor
    must only descend, whatever happened to the workspace path in between.
    """
    resolved = confine_path(raw, root)
    if resolved is None or resolved != expected:
        raise Refused("path_changed")
    relative = os.path.relpath(resolved, root)
    if relative == ".":
        return []
    parts = relative.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise Refused("path_changed")
    return parts


def _open_beneath(root_fd: int, parts: list, flags: int) -> int:
    """Open `parts` from the workspace descriptor with O_NOFOLLOW at every component."""
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        if not parts:
            return os.dup(directory)
        return os.open(parts[-1], flags | os.O_NOFOLLOW, 0o644, dir_fd=directory)
    finally:
        os.close(directory)


def _open_file(root_fd: int, parts: list, write: bool = False) -> int:
    # O_NONBLOCK keeps a FIFO from blocking the open; it has no effect on a regular file.
    flags = (os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY) | os.O_NONBLOCK
    fd = _open_beneath(root_fd, parts, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("not_a_file")
        if info.st_nlink != 1:
            raise Refused("linked_file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_file(call: ToolCall, verdict: Verdict, root: str, root_fd: int) -> Execution:
    fd = _open_file(root_fd, _checked_parts(call.arguments["path"], verdict.resolved_path, root))
    with os.fdopen(fd, "rb") as f:
        return Execution(True, f.read(MAX_OUTPUT).decode("utf-8", "replace"))


def _list_dir(call: ToolCall, verdict: Verdict, root: str, root_fd: int) -> Execution:
    parts = _checked_parts(call.arguments["path"], verdict.resolved_path, root)
    fd = _open_beneath(root_fd, parts, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # A name that is not valid UTF-8 (possible on Linux) is shown with replacement characters.
        names = sorted(os.fsencode(name).decode("utf-8", "replace") for name in os.listdir(fd))
        return Execution(True, "\n".join(names))
    finally:
        os.close(fd)


def _write_file(call: ToolCall, verdict: Verdict, root: str, root_fd: int) -> Execution:
    data = call.arguments["content"].encode("utf-8")
    fd = _open_file(root_fd, _checked_parts(call.arguments["path"], verdict.resolved_path, root), write=True)
    with os.fdopen(fd, "wb") as f:
        os.ftruncate(f.fileno(), 0)
        f.write(data)
    return Execution(True)


def _capture(argv: list, cwd: str, pass_fds: list, timeout: float = RUN_TIMEOUT) -> tuple:
    """Run argv and return (exit code, stdout, stderr), keeping only the first bytes of each."""
    kept = {}

    def drain(name, pipe, limit):
        data = bytearray()
        while chunk := pipe.read1(65536):
            data += chunk[: limit - len(data)]
        kept[name] = bytes(data)

    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=RUN_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        pass_fds=pass_fds,
        start_new_session=True,
    ) as process:
        readers = [
            threading.Thread(target=drain, args=("stdout", process.stdout, MAX_OUTPUT)),
            threading.Thread(target=drain, args=("stderr", process.stderr, MAX_STDERR)),
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
        except BaseException:
            # The program and anything it started form one new process group. Ending the group
            # closes every copy of the pipes, so the readers below reach the end and return.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            raise
        finally:
            for reader in readers:
                reader.join()
    return process.returncode, kept["stdout"], kept["stderr"]


def _run(call: ToolCall, verdict: Verdict, root: str, root_fd: int) -> Execution:
    command = call.arguments["command"]
    if verdict.argv != [command, *call.arguments["args"]] or "/" in command:
        raise Refused("argv_mismatch")
    program = shutil.which(command, path=RUN_PATH)
    if program is None or os.path.dirname(program) not in RUN_PATH.split(":"):
        raise Refused("command_missing")

    flags, operands, operands_only = [], [], False
    for arg in call.arguments["args"]:
        if not operands_only and arg.startswith("-"):
            operands_only = arg == "--"
            if not operands_only:
                flags.append(arg)
        else:
            operands.append(arg)
    if len(operands) != len(verdict.resolved_operands):
        raise Refused("argv_mismatch")
    split = 1 if command in PATTERN_FIRST else 0
    pattern = operands[:split]

    fds, names = {}, []
    with tempfile.TemporaryDirectory(prefix="run-") as stage:
        try:
            for name, expected in zip(operands[split:], verdict.resolved_operands[split:]):
                parts = _checked_parts(name, expected, root)
                if not parts:
                    raise Refused("not_a_file")
                relative = os.path.join(*parts)
                # On macOS, opening /dev/fd/N duplicates the descriptor and shares its offset, so a
                # file named twice would read as empty the second time. One name per file.
                if relative in fds:
                    raise Refused("duplicate_operand")
                fds[relative] = _open_file(root_fd, parts)
                os.makedirs(os.path.join(stage, *parts[:-1]), exist_ok=True)
                os.symlink(f"/dev/fd/{fds[relative]}", os.path.join(stage, relative))
                names.append(os.path.join(".", relative))
            exit_code, stdout, stderr = _capture([program, *flags, "--", *pattern, *names], stage, list(fds.values()))
        except subprocess.TimeoutExpired:
            return Execution(False, error="timeout")
        finally:
            for fd in fds.values():
                os.close(fd)
    return Execution(True, stdout.decode("utf-8", "replace"), exit_code=exit_code, stderr=stderr.decode("utf-8", "replace"))


RUNNERS = {"read_file": _read_file, "list_dir": _list_dir, "write_file": _write_file, "run": _run}


def execute(call: ToolCall, verdict: Verdict, workspace: str, root_fd: Optional[int] = None) -> Execution:
    """Run one call the boundary allowed. Raises PermissionError for any other call.

    `root_fd` is the workspace descriptor a ToolSession opened when it started. Without it the
    workspace is opened here, so a workspace path swapped before this call is not detected.
    """
    if not verdict.allowed:
        raise PermissionError("the boundary did not allow this call")
    root = os.path.realpath(workspace)
    own_root = root_fd is None
    try:
        if own_root:
            root_fd = _open_root(root)
        return RUNNERS[call.name](call, verdict, root, root_fd)
    except Refused as refusal:
        return Execution(False, error=refusal.args[0])
    except FileNotFoundError:
        return Execution(False, error="not_found")
    except OSError as error:
        # ELOOP and ENOTDIR are what O_NOFOLLOW and O_DIRECTORY give when a component became a link.
        return Execution(False, error="path_changed" if error.errno in (errno.ELOOP, errno.ENOTDIR) else "os_error")
    finally:
        if own_root and root_fd is not None:
            os.close(root_fd)


def _run_extra(runner, call: ToolCall) -> Execution:
    """Run a tool that is not a file tool. Its failure text could carry its arguments, so only the
    kind of failure is reported: a LookupError is not_found, anything else tool_error."""
    try:
        output = runner(call.arguments)
    except LookupError:
        return Execution(False, error="not_found")
    except Exception:
        return Execution(False, error="tool_error")
    if not isinstance(output, str):
        return Execution(False, error="tool_error")
    return Execution(True, output=output)


class ToolSession:
    """One agent in one workspace: decide, run, and log every call and approval.

    `runners` adds tools that are not file tools, such as a search over a store: each gets its
    arguments only after the boundary allowed the call, under the same schema, approval and log
    rules. Every listed tool needs a runner, and the file tools cannot be replaced.
    """

    def __init__(self, workspace: str, audit_path: str, tools: dict = LAB_TOOLS, runners: Optional[dict] = None):
        runners = dict(runners or {})
        if set(runners) & set(RUNNERS) or set(tools) - set(RUNNERS) != set(runners):
            raise ValueError("every listed tool other than the file tools needs exactly one runner")
        self.runners = runners
        self.workspace = workspace
        self.audit_path = audit_path
        self.tools = tools
        self.approvals = {}
        self.root_fd = _open_root(os.path.realpath(workspace))
        self.last_call_id = None

    def close(self) -> None:
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _require_open(self) -> None:
        # Without the pinned descriptor, execute() would open the workspace path anew.
        if self.root_fd is None:
            raise ValueError("the tool session is closed")

    def _log(self, record: dict) -> None:
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _tool_label(self, call: ToolCall) -> str:
        # An unlisted tool name is text the model chose, so it is not written to the log.
        return call.name if call.name in self.tools else UNLISTED_TOOL

    def approve(self, call: ToolCall) -> str:
        """A human approves this exact call. A previous approval for it is withdrawn first."""
        self._require_open()
        self.withdraw(call)
        approval_id = grant_approval(call, self.approvals)
        self._log({**approval_record(call, approval_id), "tool": self._tool_label(call)})
        return approval_id

    def withdraw(self, call: ToolCall) -> Optional[str]:
        """Withdraw an unused approval; returns its id, or None if there was none."""
        self._require_open()
        approval_id = self.approvals.pop(fingerprint_call(call), None)
        if approval_id is not None:
            self._log({"ts": _now(), "tool": self._tool_label(call), "event": "withdrawn", "approval_id": approval_id})
        return approval_id

    def submit(self, call: ToolCall) -> tuple:
        """Decide and, if allowed, run one call. Returns (Verdict, Execution or None)."""
        self._require_open()
        verdict = evaluate_call(call, self.tools, self.workspace, self.approvals)
        call_id = self.last_call_id = secrets.token_hex(8)
        tool = self._tool_label(call)
        self._log({**audit_record(call, verdict), "tool": tool, "event": "decision", "call_id": call_id})
        if not verdict.allowed:
            return verdict, None
        if call.name in self.runners:
            result = _run_extra(self.runners[call.name], call)
        else:
            result = execute(call, verdict, self.workspace, self.root_fd)
        record = {
            "ts": _now(),
            "tool": tool,
            "event": "executed",
            "call_id": call_id,
            "ok": result.ok,
            "output_bytes": len(result.output.encode("utf-8")),
        }
        if result.exit_code is not None:
            record["exit_code"] = result.exit_code
        if result.error is not None:
            record["error"] = result.error
        if result.stderr:
            record["stderr_bytes"] = len(result.stderr.encode("utf-8"))
        self._log(record)
        return verdict, result
