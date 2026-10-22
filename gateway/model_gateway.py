"""Model gateway: the only route from the lab's internal network to the local model API.

It plays the role a VPC endpoint plus endpoint policy plays in a cloud deployment:
requests pass only if both the API path and the model are on an allowlist, cloud-routed
models are refused outright, and every request leaves one audit line (sizes, never bodies).
Standard library only, so the container needs no package installs.
"""

import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

READ_PATHS = frozenset({"/api/tags", "/api/version"})
MODEL_PATHS = frozenset({"/api/chat", "/api/embed"})


def normalize_model(name: str) -> str:
    name = name.strip()
    return name if ":" in name else f"{name}:latest"


def parse_allowlist(raw: str) -> frozenset:
    return frozenset(normalize_model(part) for part in raw.split(",") if part.strip())


def is_cloud_model(model: str) -> bool:
    return model.rsplit(":", 1)[-1].endswith("cloud")


def evaluate_request(method: str, path: str, body: bytes, allowed_models: frozenset):
    """Return (allowed, reason, model) for one request."""
    if method == "GET" and path in READ_PATHS:
        return True, "read_path", None
    if method != "POST" or path not in MODEL_PATHS:
        return False, "path_not_allowed", None
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return False, "invalid_json", None
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str) or not model.strip():
        return False, "model_missing", None
    model = normalize_model(model)
    if is_cloud_model(model):
        return False, "cloud_model_blocked", model
    if model not in allowed_models:
        return False, "model_not_allowed", model
    return True, "model_allowed", model


def filter_tags(upstream_body: bytes, allowed_models: frozenset) -> bytes:
    """Hide every model that is not on the allowlist from /api/tags."""
    try:
        models = json.loads(upstream_body).get("models", [])
    except (json.JSONDecodeError, AttributeError):
        models = []
    kept = [m for m in models if isinstance(m, dict) and normalize_model(str(m.get("name", ""))) in allowed_models]
    return json.dumps({"models": kept}).encode()


def append_audit(audit_path: str, record: dict) -> None:
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class GatewayEndpoint(BaseHTTPRequestHandler):
    upstream = "http://host.docker.internal:11434"
    allowed_models = frozenset()
    audit_path = "/logs/gateway-audit.jsonl"

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def do_PUT(self):
        self._relay("PUT")

    def do_DELETE(self):
        self._relay("DELETE")

    def log_message(self, format, *args):
        # The JSONL audit log replaces the default stderr access log.
        return

    def _relay(self, method: str) -> None:
        started = time.monotonic()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        allowed, reason, model = evaluate_request(method, path, body, self.allowed_models)
        if allowed:
            status, data = self._forward(method, path, body)
            if path == "/api/tags" and status == 200:
                data = filter_tags(data, self.allowed_models)
        else:
            status, data = 403, json.dumps({"error": reason}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        append_audit(self.audit_path, {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "method": method,
            "path": path,
            "model": model,
            "decision": "allow" if allowed else "deny",
            "reason": reason,
            "status": status,
            "request_bytes": len(body),
            "response_bytes": len(data),
            "latency_ms": round((time.monotonic() - started) * 1000),
        })

    def _forward(self, method: str, path: str, body: bytes):
        req = urlrequest.Request(
            self.upstream + path,
            data=body if method == "POST" else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlrequest.urlopen(req, timeout=300) as resp:
                return resp.status, resp.read()
        except urlerror.HTTPError as exc:
            return exc.code, exc.read()
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            detail = str(getattr(exc, "reason", exc))
            return 502, json.dumps({"error": "upstream_unreachable", "detail": detail}).encode()


def main() -> None:
    GatewayEndpoint.upstream = os.environ.get("UPSTREAM", GatewayEndpoint.upstream).rstrip("/")
    GatewayEndpoint.allowed_models = parse_allowlist(os.environ.get("LAB_MODELS", ""))
    GatewayEndpoint.audit_path = os.environ.get("AUDIT_LOG", GatewayEndpoint.audit_path)
    port = int(os.environ.get("PORT", "8080"))
    print(f"model-gateway on :{port} -> {GatewayEndpoint.upstream} allow={sorted(GatewayEndpoint.allowed_models)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), GatewayEndpoint).serve_forever()


if __name__ == "__main__":
    main()
