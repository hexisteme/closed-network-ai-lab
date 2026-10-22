"""Releases of the gateway configuration, and the switch that activates or rolls one back.

A release is one JSON file, releases/<id>.json, naming the models the gateway may forward
to, each pinned to the digest of the weights installed on the lab host. releases/ACTIVE
holds the id the gateway serves. A switch validates the target, replaces ACTIVE in one
rename and appends one line to the switch log; rollback returns to the release that was
active before the current one. Prompts and the tool policy live in code and roll back with
git; Terraform changes are outside this mechanism.

Usage (on the host): python gateway/release.py status | check [id] | activate <id> --reason TEXT | rollback --reason TEXT
Then restart the gateway so it loads the new release: docker compose restart model-gateway
Standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
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
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read release {release_id}: {exc}") from exc
    problems = validate_release(data, release_id)
    if problems:
        raise ReleaseError(f"release {release_id} is invalid: {'; '.join(problems)}")
    return data


def active_release_id(releases_dir) -> str:
    try:
        release_id = (Path(releases_dir) / POINTER).read_text(encoding="utf-8").strip()
    except OSError as exc:
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


def _write_pointer(releases_dir, release_id: str) -> None:
    """Replace ACTIVE in one rename, so a reader sees the old id or the new one, never a partial file."""
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


def switch_release(releases_dir, release_id: str, audit_path, action: str, reason: str) -> dict:
    """Make release_id the active release and append the switch to the log. Returns the log record."""
    current = active_release_id(releases_dir)
    load_release(releases_dir, release_id)
    digest = hashlib.sha256(_release_path(releases_dir, release_id).read_bytes()).hexdigest()
    _write_pointer(releases_dir, release_id)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "action": action,
        "from": current,
        "to": release_id,
        "release_sha256": digest,
        "reason": reason,
    }
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def previous_release_id(audit_path, current: str) -> str:
    """The release that was active before `current`, from the newest switch that made it active."""
    try:
        lines = Path(audit_path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"no switch log to roll back from: {exc}") from exc
    for line in reversed(lines):
        record = json.loads(line)
        if record.get("to") == current:
            return record["from"]
    raise ReleaseError(f"the switch log never activated {current}; nothing to roll back to")


def rollback_release(releases_dir, audit_path, reason: str) -> dict:
    target = previous_release_id(audit_path, active_release_id(releases_dir))
    return switch_release(releases_dir, target, audit_path, action="rollback", reason=reason)


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
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        if args.command == "activate":
            record = switch_release(args.releases, args.release_id, args.log, action="activate", reason=args.reason)
        else:
            record = rollback_release(args.releases, args.log, reason=args.reason)
    except (ReleaseError, urlerror.URLError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"{record['action']}: {record['from']} -> {record['to']} (logged to {args.log})")
    print("restart the gateway to serve it: docker compose restart model-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
