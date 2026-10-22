"""Rollback rehearsal: the active release, a bad next release, and the rollback, against a real gateway.

Offline, on the host or in the test container. The gateway from model_gateway.py runs in this
process on 127.0.0.1 and is restarted after every switch, as `docker compose restart
model-gateway` would. Behind it, a stand-in for the model host answers only for the models in
a captured /api/tags listing (tests/fixtures/ollama-tags-20261023.json, from the lab host).

The bad release is the active one with one plausible edit: bge-m3 is dropped from the allowlist
while the RAG services still embed with it. It passes validation, so the switch accepts it; the
checks after the switch catch it, and rollback restores the previous release. The repository's
releases/ directory is copied, never changed. Writes evidence/release-rehearsal-<stamp>-<id>.json
and the switch log next to it. Standard library only.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from model_gateway import GatewayEndpoint, load_gateway_policy, normalize_model
from release import POINTER, active_release_id, compare_pins, load_release, rollback_release, switch_release

LAB = Path(__file__).resolve().parents[1]
# The models the lab's services call (compose.yaml): qwen3:4b for the agents and RAG answers,
# gemma3:4b in the egress check, bge-m3 for the RAG embeddings.
SERVICE_CHAT_MODELS = ("qwen3:4b", "gemma3:4b")
SERVICE_EMBED_MODEL = "bge-m3:latest"
DROPPED_MODEL = SERVICE_EMBED_MODEL
CLOUD_MODEL = "nemotron-3-nano:30b-cloud"


class ModelHostStandIn(BaseHTTPRequestHandler):
    """Answers like Ollama for the installed models only; anything else is 404, as Ollama does."""

    listing = {"models": []}

    def log_message(self, format, *args):
        return

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, self.listing)
        elif self.path == "/api/version":
            self._send(200, {"version": "stand-in"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        model = normalize_model(str(json.loads(self.rfile.read(length) or b"{}").get("model", "")))
        installed = {normalize_model(m["name"]) for m in self.listing["models"]}
        if model not in installed:
            self._send(404, {"error": f"model '{model}' not found"})
        elif self.path == "/api/chat":
            self._send(200, {"model": model, "message": {"role": "assistant", "content": "ready"}, "done": True, "done_reason": "stop"})
        elif self.path == "/api/embed":
            self._send(200, {"model": model, "embeddings": [[0.1, 0.2, 0.3]]})
        else:
            self._send(404, {"error": "not found"})


class LocalServer:
    """One HTTP server on 127.0.0.1 with a free port, served from a daemon thread."""

    def __init__(self, endpoint):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), endpoint)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def start_gateway(releases_dir, upstream_url: str, audit_path) -> LocalServer:
    """The gateway as its container starts it: policy from RELEASE_DIR, read once at start."""
    release_id, allowed = load_gateway_policy({"RELEASE_DIR": str(releases_dir)})
    endpoint = type("RehearsalGateway", (GatewayEndpoint,), {
        "upstream": upstream_url,
        "allowed_models": allowed,
        "release_id": release_id,
        "audit_path": str(audit_path),
    })
    return LocalServer(endpoint)


def call(url: str, method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urlrequest.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read()), resp.headers.get("X-Lab-Release")
    except urlerror.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), exc.headers.get("X-Lab-Release")


# Computed from the installed listing; every other check is one request through the gateway.
LOCAL_CHECKS = frozenset({"pinned_weights_installed"})


def audit_matches(serving: str, lines: list, expected_requests: int) -> bool:
    """A step's gateway audit has one line per request the checks made, each naming the release served."""
    return (expected_requests > 0 and len(lines) == expected_requests
            and all(json.loads(line).get("release") == serving for line in lines))


def check_serving_release(gateway_url: str, expected: str, release_data: dict, installed: dict) -> list:
    """The acceptance checks after a switch. Each is a dict with name, expect, passed, observed."""
    checks = []

    def record(name, expect, passed, observed):
        checks.append({"name": name, "expect": expect, "passed": bool(passed), "observed": observed})

    status, _, serving = call(gateway_url, "GET", "/api/version")
    record("serving_release", f"the gateway reports release {expected}", status == 200 and serving == expected, serving)

    problems = compare_pins(release_data, installed)
    record("pinned_weights_installed", "every pinned digest equals the installed weights", not problems, problems)

    for model in SERVICE_CHAT_MODELS:
        status, body, _ = call(gateway_url, "POST", "/api/chat", {"model": model, "messages": [{"role": "user", "content": "ready?"}]})
        record(f"chat:{model}", "200 through the gateway", status == 200, {"status": status, "error": body.get("error")})

    status, body, _ = call(gateway_url, "POST", "/api/embed", {"model": SERVICE_EMBED_MODEL, "input": "rollback check"})
    record(f"embed:{SERVICE_EMBED_MODEL}", "200 through the gateway", status == 200, {"status": status, "error": body.get("error")})

    status, body, _ = call(gateway_url, "POST", "/api/chat", {"model": CLOUD_MODEL, "messages": []})
    record("cloud_model_denied", "403 cloud_model_blocked", status == 403 and body.get("error") == "cloud_model_blocked", status)

    status, body, _ = call(gateway_url, "POST", "/api/pull", {"model": SERVICE_CHAT_MODELS[0]})
    record("model_download_denied", "403 path_not_allowed", status == 403 and body.get("error") == "path_not_allowed", status)
    return checks


def next_release_id(releases_dir, base: str) -> str:
    match = re.fullmatch(r"r(\d+)", base)
    if not match:
        raise ValueError(f"cannot number the release after {base}")
    candidate = f"r{int(match.group(1)) + 1}"
    while (Path(releases_dir) / f"{candidate}.json").exists():
        candidate = f"r{int(candidate[1:]) + 1}"
    return candidate


def write_bad_release(releases_dir, base_data: dict, release_id: str) -> dict:
    bad = {
        "release": release_id,
        "note": f"Rehearsal only: {base_data['release']} with {DROPPED_MODEL} removed from the allowlist, "
                "while the RAG services still embed with it.",
        "models": [m for m in base_data["models"] if normalize_model(m["name"]) != DROPPED_MODEL],
    }
    (Path(releases_dir) / f"{release_id}.json").write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    return bad


def run_rehearsal(releases_src, installed_path, evidence_dir) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"release-rehearsal-{stamp}-{secrets.token_hex(3)}"
    switch_log = Path(evidence_dir) / f"{name}.jsonl"
    installed_bytes = Path(installed_path).read_bytes()
    installed = json.loads(installed_bytes)
    ModelHostStandIn.listing = installed
    Path(evidence_dir).mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as work:
        releases = Path(work) / "releases"
        releases.mkdir()
        for src in Path(releases_src).iterdir():
            if src.suffix == ".json" or src.name == POINTER:
                shutil.copy2(src, releases / src.name)
        gateway_audit = Path(work) / "gateway-audit.jsonl"
        good_id = active_release_id(releases)
        bad_id = next_release_id(releases, good_id)
        write_bad_release(releases, load_release(releases, good_id), bad_id)

        steps = []

        def serve_and_check(step: str) -> None:
            serving = active_release_id(releases)
            seen_before = len(gateway_audit.read_text(encoding="utf-8").splitlines()) if gateway_audit.exists() else 0
            with start_gateway(releases, host.url, gateway_audit) as gateway:
                checks = check_serving_release(gateway.url, serving, load_release(releases, serving), installed)
            lines = gateway_audit.read_text(encoding="utf-8").splitlines()[seen_before:]
            steps.append({
                "step": step,
                "serving_release": next(c["observed"] for c in checks if c["name"] == "serving_release"),
                "checks_passed": all(c["passed"] for c in checks),
                "failed_checks": [c["name"] for c in checks if not c["passed"]],
                "checks": checks,
                "gateway_requests": len(lines),
                "gateway_audit_releases": sorted({json.loads(line)["release"] for line in lines}),
                "audit_matches": audit_matches(serving, lines, sum(c["name"] not in LOCAL_CHECKS for c in checks)),
            })

        with LocalServer(ModelHostStandIn) as host:
            serve_and_check("baseline")
            switch_release(releases, bad_id, switch_log, action="activate", reason=f"rehearsal: deploy {bad_id}")
            serve_and_check("bad_release")
            rollback_release(releases, switch_log, reason=f"rehearsal: checks failed on {bad_id}")
            serve_and_check("after_rollback")
        final = active_release_id(releases)

    switches = [json.loads(line) for line in switch_log.read_text(encoding="utf-8").splitlines()]
    by_step = {s["step"]: s for s in steps}
    evidence = {
        "run_at": stamp,
        "kind": "release-rehearsal",
        "installed_listing": {"file": Path(installed_path).name, "sha256": hashlib.sha256(installed_bytes).hexdigest()},
        "good_release": good_id,
        "bad_release": bad_id,
        "bad_change": f"{DROPPED_MODEL} removed from the allowlist",
        "steps": steps,
        "switches": switches,
        "switch_log": switch_log.name,
        "switch_log_sha256": hashlib.sha256(switch_log.read_bytes()).hexdigest(),
        "final_active_release": final,
        "passed": (by_step["baseline"]["checks_passed"]
                   and not by_step["bad_release"]["checks_passed"]
                   and by_step["after_rollback"]["checks_passed"]
                   and [s["serving_release"] for s in steps] == [good_id, bad_id, good_id]
                   and all(s["audit_matches"] for s in steps)
                   and [r["action"] for r in switches] == ["activate", "rollback"]
                   and final == good_id),
        "evidence_file": f"{name}.json",
    }
    with open(Path(evidence_dir) / evidence["evidence_file"], "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=2)
    return evidence


def main() -> int:
    evidence = run_rehearsal(
        os.environ.get("RELEASE_DIR", str(LAB / "releases")),
        os.environ.get("INSTALLED_TAGS", str(LAB / "tests" / "fixtures" / "ollama-tags-20261023.json")),
        os.environ.get("EVIDENCE_DIR", str(LAB / "evidence")),
    )
    for step in evidence["steps"]:
        failed = ", ".join(step["failed_checks"]) or "-"
        print(f"{step['step']:<15} serving {step['serving_release']}  checks {'PASS' if step['checks_passed'] else 'FAIL'}  failed: {failed}")
    for r in evidence["switches"]:
        print(f"switch  {r['action']:<8} {r['from']} -> {r['to']}")
    print(f"rehearsal {'PASS' if evidence['passed'] else 'FAIL'}  evidence: {evidence['evidence_file']}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
