import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))

from model_gateway import evaluate_request, filter_tags, parse_allowlist  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
