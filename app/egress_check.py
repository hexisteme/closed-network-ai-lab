"""Egress check, run from a container that sits only on the lab's internal network.

Pass means: the internet and the host model API are unreachable directly, and the only
working route is the model gateway, which still refuses downloads and cloud models.
Writes one JSON evidence file per run. Standard library only.
"""

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from urllib import error, request

GATEWAY = os.environ.get("GATEWAY", "http://model-gateway:8080").rstrip("/")
CHAT_MODELS = [m.strip() for m in os.environ.get("CHAT_MODELS", "qwen3:4b,gemma3:4b").split(",") if m.strip()]
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
BLOCKED_MODEL = os.environ.get("BLOCKED_MODEL", "nemotron-3-nano:30b-cloud")
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/evidence")


def connect_error(host: str, port: int, timeout: float = 3.0):
    """Return None if a TCP connection opens, else the error text."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def network_gateway_address(routes: str):
    """Docker's gateway on this network, the first address of the subnet in /proc/net/route, or None.

    Docker lets containers on an internal network reach the host on that address, so a host
    service listening on all interfaces there would bypass the model gateway.
    """
    for line in routes.splitlines()[1:]:
        fields = line.split()
        try:
            destination, mask = int(fields[1], 16), int(fields[7], 16)
        except (IndexError, ValueError):
            continue
        if destination and mask:
            network = socket.ntohl(destination)
            return socket.inet_ntoa((network + 1).to_bytes(4, "big"))
    return None


def call_gateway(method: str, path: str, payload=None, timeout: float = 300.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = request.Request(GATEWAY + path, data=data, method=method, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (error.URLError, OSError) as exc:
        return None, {"error": str(getattr(exc, "reason", exc))}, round((time.monotonic() - started) * 1000)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw[:200].decode("utf-8", "replace")}
    return status, parsed, round((time.monotonic() - started) * 1000)


def wait_for_gateway(deadline_s: float = 30.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        status, _, _ = call_gateway("GET", "/api/version", timeout=3)
        if status == 200:
            return True
        time.sleep(1)
    return False


def diagnose_chat_answer(status, body: dict):
    """Return None for a usable chat answer, else the reason it is not usable.

    Reasoning models such as qwen3 must keep their reasoning in message.thinking. With
    "think": false, Ollama 0.32.14 stops parsing it, so the raw reasoning lands in the answer.
    """
    if status != 200:
        return f"status_{status}"
    if body.get("done_reason") == "length":
        return "answer_truncated"
    content = (body.get("message") or {}).get("content") or ""
    if "<think>" in content or "</think>" in content:
        return "reasoning_in_answer"
    if not content.strip():
        return "empty_answer"
    return None


def record(checks: list, name: str, expect: str, passed: bool, observed) -> None:
    checks.append({"name": name, "expect": expect, "passed": bool(passed), "observed": observed})


def run_checks() -> list:
    checks = []

    err = connect_error("1.1.1.1", 443)
    record(checks, "internet_ip_blocked", "TCP to 1.1.1.1:443 fails", err is not None, err or "connected")

    try:
        req = request.Request("https://example.com", method="HEAD")
        with request.urlopen(req, timeout=5) as resp:
            observed, passed = f"HTTP {resp.status}", False
    except (error.URLError, OSError) as exc:
        observed, passed = str(getattr(exc, "reason", exc)), True
    record(checks, "internet_https_blocked", "HTTPS to example.com fails", passed, observed)

    err = connect_error("host.docker.internal", 11434)
    record(checks, "host_model_api_unreachable", "direct TCP to host Ollama fails (no gateway bypass)", err is not None, err or "connected")

    try:
        with open("/proc/net/route", encoding="ascii") as f:
            bridge = network_gateway_address(f.read())
    except OSError:
        bridge = None
    err = connect_error(bridge, 11434) if bridge else "no subnet route found"
    record(checks, "host_model_port_closed_on_bridge", "TCP to the network's gateway address on the Ollama port fails",
           bridge is not None and err is not None, {"address": bridge, "result": err or "connected"})

    record(checks, "gateway_ready", "gateway answers /api/version within 30 s", wait_for_gateway(), GATEWAY)

    status, body, _ = call_gateway("GET", "/api/tags")
    names = sorted(m.get("name") for m in body.get("models", [])) if status == 200 else body
    listed_cloud = [n for n in names if isinstance(n, str) and n.rsplit(":", 1)[-1].endswith("cloud")] if status == 200 else []
    record(checks, "gateway_lists_only_allowed_models", "200 and no cloud models listed", status == 200 and not listed_cloud, names)

    status, body, _ = call_gateway("POST", "/api/pull", {"model": CHAT_MODELS[0]})
    record(checks, "model_download_denied", "403 path_not_allowed", status == 403, {"status": status, "body": body})

    status, body, _ = call_gateway("POST", "/api/chat", {"model": BLOCKED_MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": False})
    record(checks, "cloud_model_denied", "403 cloud_model_blocked", status == 403 and body.get("error") == "cloud_model_blocked", {"status": status, "body": body})

    for model in CHAT_MODELS:
        # "think" is left out on purpose: Ollama then splits a reasoning model's thinking from
        # its answer, and models without thinking support answer normally. The budget leaves
        # room for the reasoning to finish before the answer.
        status, body, latency = call_gateway("POST", "/api/chat", {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with one word: ready"}],
            "stream": False,
            "options": {"num_predict": 512, "temperature": 0},
        })
        problem = diagnose_chat_answer(status, body)
        message = (body.get("message") or {}) if status == 200 else {}
        record(checks, f"chat_via_gateway:{model}", "200, finished answer, no reasoning text in it", problem is None, {
            "status": status,
            "problem": problem,
            "answer": (message.get("content") or "").strip()[:80],
            "thinking_chars": len(message.get("thinking") or ""),
            "done_reason": body.get("done_reason") if status == 200 else None,
            "eval_count": body.get("eval_count") if status == 200 else None,
            "latency_ms": latency,
            "error": None if status == 200 else body,
        })

    status, body, latency = call_gateway("POST", "/api/embed", {"model": EMBED_MODEL, "input": "closed network check"})
    vectors = body.get("embeddings") if status == 200 else None
    dim = len(vectors[0]) if vectors else 0
    record(checks, f"embed_via_gateway:{EMBED_MODEL}", "200 and a non-empty vector", status == 200 and dim > 0, {
        "status": status, "dimension": dim, "latency_ms": latency, "error": None if status == 200 else body,
    })
    return checks


def main() -> int:
    checks = run_checks()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = {
        "run_at": stamp,
        "network": "lab_internal (Docker internal network, no gateway to the internet)",
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    path = os.path.join(EVIDENCE_DIR, f"egress-check-{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    for c in checks:
        print(f"{'PASS' if c['passed'] else 'FAIL'}  {c['name']}")
    print(f"evidence: {path}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
