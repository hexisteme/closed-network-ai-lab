"""Load test of the model gateway, run from a container on the lab's internal network.

At each concurrency level it sends the same chat call REQUESTS_PER_LEVEL times, LEVEL at a
time, and records each call's status and latency. Every call asks for the same fixed number of
output tokens, so the levels differ only in how many calls compete for the one local model.
One call before the first level loads the model and is not counted. Writes one JSON evidence
file. Standard library only.
"""

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib import error, request

GATEWAY = os.environ.get("GATEWAY", "http://model-gateway:8080").rstrip("/")
MODEL = os.environ.get("MODEL", "qwen3:4b")
NUM_PREDICT = int(os.environ.get("NUM_PREDICT", "64"))
REQUESTS_PER_LEVEL = int(os.environ.get("REQUESTS_PER_LEVEL", "16"))
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/evidence")
PROMPT = "List the planets of the solar system in order from the Sun, one per line."


def parse_levels(raw: str) -> list:
    levels = [int(part) for part in raw.split(",") if part.strip()]
    if not levels or min(levels) < 1:
        raise ValueError(f"LEVELS must be positive whole numbers, got {raw!r}")
    return levels


def nearest_rank(values: list, percentile: float):
    """The nearest-rank percentile of the values, or None if there are none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(percentile / 100 * len(ordered)), 1) - 1]


def summarize_level(concurrency: int, calls: list, wall_s: float) -> dict:
    answered = [c for c in calls if c["status"] == 200]
    latencies = [c["latency_ms"] for c in answered]
    return {
        "concurrency": concurrency,
        "requests": len(calls),
        "ok": len(answered),
        "shed_503": sum(c["status"] == 503 for c in calls),
        "other_errors": sum(c["status"] not in (200, 503) for c in calls),
        "p50_ms": nearest_rank(latencies, 50),
        "p95_ms": nearest_rank(latencies, 95),
        "max_ms": max(latencies) if latencies else None,
        "wall_s": round(wall_s, 2),
        "ok_per_s": round(len(answered) / wall_s, 3),
        "output_tokens_per_s": round(sum(c["eval_count"] for c in answered) / wall_s, 1),
    }


def chat_once() -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "options": {"num_predict": NUM_PREDICT, "temperature": 0, "seed": 7},
    }
    req = request.Request(GATEWAY + "/api/chat", data=json.dumps(payload).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
    started = time.monotonic()
    status, body, retry_after = None, {}, None
    try:
        with request.urlopen(req, timeout=330) as resp:
            status, body = resp.status, json.loads(resp.read())
    except error.HTTPError as exc:
        status, retry_after = exc.code, exc.headers.get("Retry-After")
    except (error.URLError, OSError, json.JSONDecodeError):
        status = None
    return {
        "status": status,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "eval_count": int(body.get("eval_count") or 0),
        "retry_after": retry_after,
    }


def run_level(concurrency: int) -> dict:
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        calls = list(pool.map(lambda _: chat_once(), range(REQUESTS_PER_LEVEL)))
    summary = summarize_level(concurrency, calls, time.monotonic() - started)
    summary["retry_after_seen"] = sorted({c["retry_after"] for c in calls if c["retry_after"]})
    return summary


def main() -> int:
    levels = parse_levels(os.environ.get("LEVELS", "1,2,4,8"))
    warmup = chat_once()
    if warmup["status"] != 200:
        print(f"warm-up call failed: {warmup}", flush=True)
        return 1
    results = []
    for level in levels:
        summary = run_level(level)
        results.append(summary)
        print(json.dumps(summary), flush=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = {
        "kind": "gateway-load",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gateway": GATEWAY,
        "gateway_max_in_flight": os.environ.get("GATEWAY_MAX_IN_FLIGHT"),
        "model": MODEL,
        "num_predict": NUM_PREDICT,
        "requests_per_level": REQUESTS_PER_LEVEL,
        "warmup_ms": warmup["latency_ms"],
        "levels": results,
    }
    path = os.path.join(EVIDENCE_DIR, f"gateway-load-{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=1)
        fh.write("\n")
    print(f"evidence: {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
