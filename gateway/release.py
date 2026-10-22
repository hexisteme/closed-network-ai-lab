"""Releases of the gateway configuration, and the switch that activates or rolls one back.

A release is one JSON file, releases/<id>.json, naming the models the gateway may forward
to, each pinned to the digest of the weights installed on the lab host. releases/ACTIVE
holds the id the gateway loads at its next start. A switch holds a lock on the releases
directory, validates the target, replaces ACTIVE in one rename and appends one line to the
switch log, syncing both before it returns; a failure after the rename is reported as a switch
that happened. Rollback returns to the "from" of the newest log line, and refuses when that line
does not end at the active release; archive-log moves a damaged log aside. Prompts and the tool
policy live in code and roll back with git; Terraform changes are outside this mechanism.

Usage (on the host): python gateway/release.py status | check [id] | activate <id> --reason TEXT | rollback --reason TEXT | archive-log
Then restart the gateway so it loads the new release: docker compose restart model-gateway
Standard library only.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from model_gateway import is_cloud_model, normalize_model

LAB = Path(__file__).resolve().parents[1]
POINTER = "ACTIVE"
RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,31}")
DIGEST = re.compile(r"[0-9a-f]{64}")


class ReleaseError(Exception):
    """A release that cannot be loaded or switched to."""


class ArchiveIncomplete(ReleaseError):
    """The switch log was moved aside, and syncing its directory failed."""

    def __init__(self, target: Path, cause: Exception):
        super().__init__(f"archived to {target}, but could not sync the log's directory: {cause}")
        self.target = target


class SwitchIncomplete(ReleaseError):
    """ACTIVE was replaced, and a later step of the switch failed. `logged` says whether the log line was written."""

    def __init__(self, previous: str, target: str, step: str, logged: bool, cause: Exception):
        super().__init__(f"switched {previous} -> {target}, but could not {step}: {cause}")
        self.previous, self.target, self.step, self.logged = previous, target, step, logged


def validate_release(data, release_id: str) -> list:
    """Return the problems that keep this manifest from being served; empty when there are none."""
    if not isinstance(data, dict):
        return ["manifest is not an object"]
    problems = []
    if data.get("release") != release_id:
        problems.append(f"release id {data.get('release')} does not match file {release_id}")
    models = data.get("models")
    if not isinstance(models, list) or not models:
        return problems + ["no models"]
    seen = set()
    for entry in models:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            problems.append("model without a name")
            continue
        name = normalize_model(name)
        if name in seen:
            problems.append(f"model listed twice: {name}")
        seen.add(name)
        if is_cloud_model(name):
            problems.append(f"cloud model in release: {name}")
        if not isinstance(entry.get("digest"), str) or not DIGEST.fullmatch(entry["digest"]):
            problems.append(f"bad digest for {name}")
    return problems


def _release_path(releases_dir, release_id: str) -> Path:
    if not isinstance(release_id, str) or not RELEASE_ID.fullmatch(release_id):
        raise ReleaseError(f"not a release id: {release_id!r}")
    return Path(releases_dir) / f"{release_id}.json"


def load_release(releases_dir, release_id: str) -> dict:
    path = _release_path(releases_dir, release_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers bad JSON and bytes that are not UTF-8
        raise ReleaseError(f"cannot read release {release_id}: {exc}") from exc
    problems = validate_release(data, release_id)
    if problems:
        raise ReleaseError(f"release {release_id} is invalid: {'; '.join(problems)}")
    return data


def active_release_id(releases_dir) -> str:
    try:
        release_id = (Path(releases_dir) / POINTER).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"no active release in {releases_dir}: {exc}") from exc
    _release_path(releases_dir, release_id)
    return release_id


def release_models(data: dict) -> frozenset:
    return frozenset(normalize_model(m["name"]) for m in data["models"])


def compare_pins(data: dict, installed_tags: dict) -> list:
    """Compare a release's pins with an /api/tags listing; one entry per model that does not match."""
    installed = {}
    for entry in installed_tags.get("models", []):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            installed[normalize_model(entry["name"])] = entry.get("digest")
    problems = []
    for entry in data["models"]:
        name = normalize_model(entry["name"])
        found = installed.get(name)
        if found is None:
            problems.append({"model": name, "problem": "not_installed", "pinned": entry["digest"], "installed": None})
        elif found != entry["digest"]:
            problems.append({"model": name, "problem": "digest_mismatch", "pinned": entry["digest"], "installed": found})
    return problems


def _sync_directory(directory) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_pointer(releases_dir, release_id: str) -> None:
    """Replace ACTIVE in one rename, so a reader sees the old id or the new one, never a partial file.
    A failure here leaves ACTIVE as it was; the caller syncs the directory after the rename."""
    fd, tmp = tempfile.mkstemp(prefix=".ACTIVE-", dir=releases_dir)
    try:
        # mkstemp makes the file 0600; the release files are readable, and so is the pointer.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(release_id + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, Path(releases_dir) / POINTER)
    except BaseException:
        os.unlink(tmp)
        raise


def _write_line(log, record: dict) -> None:
    """Write one line to the unbuffered log, so nothing is left to flush at close."""
    view = memoryview((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    while view:
        view = view[log.write(view):]


@contextmanager
def _switch_lock(releases_dir):
    """One switch at a time: reading the current state, switching and logging happen under one lock."""
    try:
        fd = os.open(releases_dir, os.O_RDONLY)
    except OSError as exc:
        raise ReleaseError(f"cannot open releases directory {releases_dir}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _switch(releases_dir, current: str, release_id: str, audit_path, action: str, reason: str) -> dict:
    if release_id == current:
        raise ReleaseError(f"{release_id} is already active; nothing to switch")
    load_release(releases_dir, release_id)
    digest = hashlib.sha256(_release_path(releases_dir, release_id).read_bytes()).hexdigest()
    log_dir = Path(audit_path).resolve().parent
    # Opened before the pointer moves: a log that cannot be opened stops the switch with nothing changed.
    with open(audit_path, "ab", buffering=0) as log:
        _write_pointer(releases_dir, release_id)
        # ACTIVE now names release_id; any failure from here on is reported as a switch that happened.
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "action": action,
            "from": current,
            "to": release_id,
            "release_sha256": digest,
            "reason": reason,
        }
        step, written = "sync the releases directory", False
        try:
            _sync_directory(releases_dir)
            step = "write the switch log line"
            _write_line(log, record)
            step, written = "sync the switch log", True
            os.fsync(log.fileno())
            # Every time, not only when the log is new: a retry after a failed sync is then covered too.
            step = "sync the switch log's directory and its parent"
            _sync_directory(log_dir)
            _sync_directory(log_dir.parent)
        except OSError as exc:
            raise SwitchIncomplete(current, release_id, step, written, exc) from exc
    return record


def switch_release(releases_dir, release_id: str, audit_path, action: str, reason: str) -> dict:
    """Make release_id the active release and append the switch to the log. Returns the log record."""
    with _switch_lock(releases_dir):
        return _switch(releases_dir, active_release_id(releases_dir), release_id, audit_path, action, reason)


def _switch_record(line: str, number: int) -> dict:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        record = None
    if not isinstance(record, dict) or record.get("action") not in ("activate", "rollback"):
        raise ReleaseError(f"switch log line {number} is not a switch record")
    for key in ("from", "to"):
        if not isinstance(record.get(key), str) or not RELEASE_ID.fullmatch(record[key]):
            raise ReleaseError(f"switch log line {number}: {key!r} is not a release id")
    if record["from"] == record["to"]:
        raise ReleaseError(f"switch log line {number} switches {record['to']} to itself")
    return record


def previous_release_id(audit_path, current: str) -> str:
    """The release the newest switch left. Every line must be a switch record, and the newest one
    must end at `current`; otherwise the log does not describe how `current` became active."""
    try:
        lines = Path(audit_path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"no switch log to roll back from: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"the switch log is not UTF-8 text: {exc}") from exc
    records = [_switch_record(line, number) for number, line in enumerate(lines, start=1)]
    if not records:
        raise ReleaseError("the switch log is empty; nothing to roll back to")
    if records[-1]["to"] != current:
        raise ReleaseError(f"the switch log ends at {records[-1]['to']} but the active release is {current}; "
                           "choose a release with activate")
    return records[-1]["from"]


def rollback_release(releases_dir, audit_path, reason: str) -> dict:
    with _switch_lock(releases_dir):
        current = active_release_id(releases_dir)
        target = previous_release_id(audit_path, current)
        return _switch(releases_dir, current, target, audit_path, action="rollback", reason=reason)


def archive_switch_log(releases_dir, audit_path) -> Path:
    """Move the switch log aside, under the switch lock, so a damaged history stops blocking rollback.
    The next switch starts a new log; until then rollback has nothing to return to."""
    source = Path(audit_path)
    with _switch_lock(releases_dir):
        if not source.exists():
            raise ReleaseError(f"no switch log at {source}")
        target = source.with_name(f"{source.name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.archived")
        if target.exists():
            raise ReleaseError(f"{target} already exists")
        os.replace(source, target)
        try:
            _sync_directory(source.parent)
        except OSError as exc:
            raise ArchiveIncomplete(target, exc) from exc
    return target


def fetch_installed_tags(upstream: str) -> dict:
    """GET /api/tags from the model host. Reads the listing only; no model is loaded."""
    with urlrequest.urlopen(upstream.rstrip("/") + "/api/tags", timeout=10) as resp:
        return json.loads(resp.read())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Show, check, activate or roll back gateway releases.")
    parser.add_argument("--releases", default=os.environ.get("RELEASE_DIR", str(LAB / "releases")))
    parser.add_argument("--log", default=os.environ.get("RELEASE_LOG", str(LAB / "logs" / "release-switches.jsonl")))
    parser.add_argument("--upstream", default=os.environ.get("UPSTREAM", "http://localhost:11434"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    check = sub.add_parser("check")
    check.add_argument("release_id", nargs="?")
    activate = sub.add_parser("activate")
    activate.add_argument("release_id")
    activate.add_argument("--reason", required=True)
    back = sub.add_parser("rollback")
    back.add_argument("--reason", required=True)
    sub.add_parser("archive-log")
    args = parser.parse_args(argv)

    try:
        if args.command == "status":
            current = active_release_id(args.releases)
            print(f"active: {current} models: {sorted(release_models(load_release(args.releases, current)))}")
            return 0
        if args.command == "check":
            release_id = args.release_id or active_release_id(args.releases)
            problems = compare_pins(load_release(args.releases, release_id), fetch_installed_tags(args.upstream))
            for p in problems:
                print(f"FAIL  {p['model']}: {p['problem']} pinned={p['pinned'][:12]} installed={(p['installed'] or '-')[:12]}")
            print(f"{release_id}: {'pins match the installed weights' if not problems else f'{len(problems)} problem(s)'}")
            return 0 if not problems else 1
        if args.command == "archive-log":
            print(f"switch log archived to {archive_switch_log(args.releases, args.log)}; rollback starts from the next switch")
            return 0
        # One level only, so the switch syncs every directory it may have created: the log's and its parent.
        Path(args.log).parent.mkdir(exist_ok=True)
        if args.command == "activate":
            record = switch_release(args.releases, args.release_id, args.log, action="activate", reason=args.reason)
        else:
            record = rollback_release(args.releases, args.log, reason=args.reason)
    except ArchiveIncomplete as exc:
        print(f"{exc}\nthe log was moved; that the move survives a crash is unconfirmed", file=sys.stderr)
        return 3
    except SwitchIncomplete as exc:
        if exc.logged:
            note = "the log line was written; that it survives a crash is unconfirmed"
        else:
            note = ("the log has no complete line for this switch, so rollback refuses; choose a release with "
                    "activate, or run archive-log first if the log now ends in a partial line")
        print(f"{exc}\n{note}\nrestart the gateway to serve {exc.target}", file=sys.stderr)
        return 3
    except (ReleaseError, urlerror.URLError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"{record['action']}: {record['from']} -> {record['to']} (logged to {args.log})")
    print("restart the gateway to serve it: docker compose restart model-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
