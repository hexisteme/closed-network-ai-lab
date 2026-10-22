import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from gateway_load import nearest_rank, parse_levels, summarize_level  # noqa: E402


def call(status, latency_ms, eval_count=0):
    return {"status": status, "latency_ms": latency_ms, "eval_count": eval_count}


class NearestRankTest(unittest.TestCase):
    def test_the_median_and_95th_of_ten_values(self):
        values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        self.assertEqual((nearest_rank(values, 50), nearest_rank(values, 95)), (50, 100))

    def test_order_does_not_matter_and_one_value_is_every_percentile(self):
        self.assertEqual(nearest_rank([30, 10, 20], 50), 20)
        self.assertEqual(nearest_rank([7], 95), 7)

    def test_no_values_gives_none(self):
        self.assertIsNone(nearest_rank([], 50))


class ParseLevelsTest(unittest.TestCase):
    def test_comma_separated_positive_numbers_in_order(self):
        self.assertEqual(parse_levels("1, 2,4,8"), [1, 2, 4, 8])

    def test_zero_negative_or_words_are_refused(self):
        for raw in ("0", "1,-2", "one", ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_levels(raw)


class SummarizeLevelTest(unittest.TestCase):
    def test_latency_counts_only_answered_calls_and_shed_calls_are_counted_apart(self):
        calls = [call(200, 1000, 50), call(200, 3000, 50), call(503, 2), call(502, 5), call(None, 300000)]
        summary = summarize_level(4, calls, wall_s=4.0)
        self.assertEqual(summary["concurrency"], 4)
        self.assertEqual((summary["requests"], summary["ok"], summary["shed_503"], summary["other_errors"]), (5, 2, 1, 2))
        self.assertEqual((summary["p50_ms"], summary["p95_ms"], summary["max_ms"]), (1000, 3000, 3000))
        self.assertEqual((summary["ok_per_s"], summary["output_tokens_per_s"]), (0.5, 25.0))

    def test_a_level_where_nothing_was_answered_has_no_latency(self):
        summary = summarize_level(8, [call(503, 1)] * 3, wall_s=0.5)
        self.assertEqual((summary["ok"], summary["shed_503"], summary["p50_ms"], summary["ok_per_s"]), (0, 3, None, 0.0))


if __name__ == "__main__":
    unittest.main()
