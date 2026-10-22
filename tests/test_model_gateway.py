import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))

from model_gateway import (  # noqa: E402
    DEFAULT_MAX_IN_FLIGHT,
    RETRY_AFTER_SECONDS,
    GatewayEndpoint,
    evaluate_request,
    filter_tags,
    parse_allowlist,
    parse_max_in_flight,
)

ALLOWED = parse_allowlist("qwen3:4b, gemma3:4b, bge-m3")


def body(payload):
    return json.dumps(payload).encode()


class EvaluateRequestTest(unittest.TestCase):
    def test_read_paths_are_allowed(self):
        self.assertEqual(evaluate_request("GET", "/api/tags", b"", ALLOWED)[:2], (True, "read_path"))
        self.assertEqual(evaluate_request("GET", "/api/version", b"", ALLOWED)[:2], (True, "read_path"))

    def test_model_download_is_denied(self):
        allowed, reason, _ = evaluate_request("POST", "/api/pull", body({"model": "qwen3:4b"}), ALLOWED)
        self.assertEqual((allowed, reason), (False, "path_not_allowed"))

    def test_unknown_method_on_model_path_is_denied(self):
        self.assertEqual(evaluate_request("DELETE", "/api/chat", b"", ALLOWED)[:2], (False, "path_not_allowed"))

    def test_cloud_model_is_denied_even_if_listed(self):
        listed = parse_allowlist("qwen3:4b,nemotron-3-nano:30b-cloud")
        allowed, reason, model = evaluate_request(
            "POST", "/api/chat", body({"model": "nemotron-3-nano:30b-cloud"}), listed
        )
        self.assertEqual((allowed, reason, model), (False, "cloud_model_blocked", "nemotron-3-nano:30b-cloud"))

    def test_unlisted_local_model_is_denied(self):
        allowed, reason, _ = evaluate_request("POST", "/api/chat", body({"model": "llama3:8b"}), ALLOWED)
        self.assertEqual((allowed, reason), (False, "model_not_allowed"))

    def test_listed_model_without_tag_resolves_to_latest(self):
        allowed, reason, model = evaluate_request("POST", "/api/embed", body({"model": "bge-m3"}), ALLOWED)
        self.assertEqual((allowed, reason, model), (True, "model_allowed", "bge-m3:latest"))

    def test_invalid_json_is_denied(self):
        self.assertEqual(evaluate_request("POST", "/api/chat", b"{not json", ALLOWED)[:2], (False, "invalid_json"))

    def test_missing_model_is_denied(self):
        self.assertEqual(evaluate_request("POST", "/api/chat", body({"messages": []}), ALLOWED)[:2], (False, "model_missing"))


class FilterTagsTest(unittest.TestCase):
    def test_only_allowlisted_models_are_listed(self):
        upstream = body({"models": [{"name": "qwen3:4b"}, {"name": "gpt-oss:120b-cloud"}, {"name": "bge-m3:latest"}]})
        names = [m["name"] for m in json.loads(filter_tags(upstream, ALLOWED))["models"]]
        self.assertEqual(names, ["qwen3:4b", "bge-m3:latest"])

    def test_unparseable_upstream_listing_is_replaced_by_empty_list(self):
        self.assertEqual(json.loads(filter_tags(b"oops", ALLOWED)), {"models": []})



class MaxInFlightTest(unittest.TestCase):
    def test_absent_or_blank_gives_the_default(self):
        self.assertEqual((parse_max_in_flight(None), parse_max_in_flight(" ")), (DEFAULT_MAX_IN_FLIGHT, DEFAULT_MAX_IN_FLIGHT))

    def test_a_positive_whole_number_is_the_limit(self):
        self.assertEqual(parse_max_in_flight("2"), 2)

    def test_anything_else_stops_the_gateway(self):
        for raw in ("0", "-1", "1.5", "two"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_max_in_flight(raw)


class SlowUpstream(BaseHTTPRequestHandler):
    """Holds each chat call until released; answers reads at once."""
    arrived = threading.Semaphore(0)
    release = threading.Event()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        SlowUpstream.arrived.release()
        SlowUpstream.release.wait(10)
        self._send(b'{"done": true}')

    def do_GET(self):
        self._send(json.dumps({"models": [{"name": "qwen3:4b"}]}).encode())

    def _send(self, data):
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


class CutOffUpstream(BaseHTTPRequestHandler):
    """Promises a longer reply than it sends, then hangs up."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b'{"done"')

    def log_message(self, format, *args):
        return


class OverloadTest(unittest.TestCase):
    """Model calls over the limit are refused at once instead of queueing behind the model."""

    def setUp(self):
        SlowUpstream.arrived = threading.Semaphore(0)
        SlowUpstream.release = threading.Event()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowUpstream)
        self.audit = Path(tempfile.mkdtemp()) / "audit.jsonl"
        endpoint = type("LimitedEndpoint", (GatewayEndpoint,), {
            "upstream": f"http://127.0.0.1:{self.upstream.server_address[1]}",
            "allowed_models": ALLOWED,
            "audit_path": str(self.audit),
            "model_slots": threading.BoundedSemaphore(1),
        })
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), endpoint)
        for server in (self.upstream, self.gateway):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)

    def stop(self):
        SlowUpstream.release.set()
        for server in (self.gateway, self.upstream):
            server.shutdown()
            server.server_close()

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urlrequest.Request(f"http://127.0.0.1:{self.gateway.server_address[1]}{path}", data=data, method=method)
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, json.loads(resp.read())
        except urlerror.HTTPError as exc:
            return exc.code, exc.headers, json.loads(exc.read())

    def hold_the_slot(self):
        result = {}
        thread = threading.Thread(target=lambda: result.update(first=self.call("POST", "/api/chat", {"model": "qwen3:4b"})))
        thread.start()
        self.assertTrue(SlowUpstream.arrived.acquire(timeout=10), "the first call never reached the model")
        return thread, result

    def audit_lines(self):
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def test_a_call_over_the_limit_is_shed_with_retry_after_and_an_audit_line(self):
        thread, result = self.hold_the_slot()
        status, headers, data = self.call("POST", "/api/embed", {"model": "bge-m3"})
        self.assertEqual((status, data, headers["Retry-After"]), (503, {"error": "overloaded"}, str(RETRY_AFTER_SECONDS)))
        shed = self.audit_lines()[-1]
        self.assertEqual((shed["decision"], shed["reason"], shed["status"], shed["model"]), ("shed", "overloaded", 503, "bge-m3:latest"))
        SlowUpstream.release.set()
        thread.join(10)
        self.assertEqual(result["first"][0], 200)

    def test_the_slot_is_free_again_once_the_call_returns(self):
        thread, _ = self.hold_the_slot()
        SlowUpstream.release.set()
        thread.join(10)
        self.assertEqual(self.call("POST", "/api/chat", {"model": "qwen3:4b"})[0], 200)

    def test_reads_and_refusals_do_not_wait_for_a_slot(self):
        thread, _ = self.hold_the_slot()
        self.assertEqual(self.call("GET", "/api/tags")[0], 200)
        self.assertEqual(self.call("POST", "/api/chat", {"model": "llama3:8b"})[:3:2], (403, {"error": "model_not_allowed"}))
        SlowUpstream.release.set()
        thread.join(10)

    def test_an_unreachable_model_frees_the_slot(self):
        self.gateway.RequestHandlerClass.upstream = "http://127.0.0.1:9"
        self.assertEqual([self.call("POST", "/api/chat", {"model": "qwen3:4b"})[0] for _ in range(2)], [502, 502])

    def test_a_reply_cut_off_upstream_is_a_502_with_an_audit_line_and_frees_the_slot(self):
        cut = ThreadingHTTPServer(("127.0.0.1", 0), CutOffUpstream)
        threading.Thread(target=cut.serve_forever, daemon=True).start()
        self.addCleanup(cut.server_close)
        self.addCleanup(cut.shutdown)
        self.gateway.RequestHandlerClass.upstream = f"http://127.0.0.1:{cut.server_address[1]}"
        replies = [self.call("POST", "/api/chat", {"model": "qwen3:4b"}) for _ in range(2)]
        self.assertEqual([(status, data["error"]) for status, _, data in replies], [(502, "upstream_incomplete")] * 2)
        self.assertEqual([(a["decision"], a["status"]) for a in self.audit_lines()], [("allow", 502)] * 2)


if __name__ == "__main__":
    unittest.main()
