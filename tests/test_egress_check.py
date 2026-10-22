import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from egress_check import diagnose_chat_answer  # noqa: E402


def chat(content, done_reason="stop", thinking=None):
    message = {"role": "assistant", "content": content}
    if thinking is not None:
        message["thinking"] = thinking
    return {"message": message, "done_reason": done_reason}


class DiagnoseChatAnswerTest(unittest.TestCase):
    def test_clean_answer_passes(self):
        self.assertIsNone(diagnose_chat_answer(200, chat("ready")))

    def test_reasoning_kept_in_thinking_field_passes(self):
        self.assertIsNone(diagnose_chat_answer(200, chat("ready", thinking="Hmm, the user wants one word.")))

    def test_reasoning_cut_off_by_token_budget_fails(self):
        # Observed 2026-10-17 with "think": false and num_predict 16 on qwen3:4b.
        body = chat('Hmm, the user just asked me to reply with one word: "ready".', done_reason="length")
        self.assertEqual(diagnose_chat_answer(200, body), "answer_truncated")

    def test_raw_reasoning_block_in_answer_fails(self):
        # Observed with "think": false and a 512-token budget: reasoning and answer share content.
        body = chat("Hmm, they want a single word.\n</think>\n\nready")
        self.assertEqual(diagnose_chat_answer(200, body), "reasoning_in_answer")

    def test_empty_answer_fails(self):
        self.assertEqual(diagnose_chat_answer(200, chat("  ")), "empty_answer")

    def test_upstream_error_fails(self):
        self.assertEqual(diagnose_chat_answer(400, {"error": '"gemma3:4b" does not support thinking'}), "status_400")


if __name__ == "__main__":
    unittest.main()
