"""The LangGraph law-research agent: scripted model replies and an in-memory article store stand
in for the model and pgvector; the graph, the boundary, the pause for approval and the audit
log are real.

LangGraph is in the lab image, not in the host's Python, so these tests run in
`docker compose --profile test run --rm lab-test` and are skipped elsewhere.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(LAB / "agent"), str(LAB / "rag")]

HAS_LANGGRAPH = importlib.util.find_spec("langgraph") is not None
if HAS_LANGGRAPH:
    import rag_agent  # noqa: E402
    from rag_agent import RAG_TOOLS, run_case  # noqa: E402

ARTICLES = {
    "제28조의8": {"id": "제28조의8", "title": "개인정보의 국외 이전", "text": "제28조의8(개인정보의 국외 이전) ① 개인정보처리자는 국외로 이전할 수 있다."},
    "제17조": {"id": "제17조", "title": "개인정보의 제공", "text": "제17조(개인정보의 제공) ① 제3자에게 제공할 수 있다."},
}
MEMO = "국외 이전은 제28조의8에 따른 요건을 갖추어야 한다."


def proposal(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments}, ensure_ascii=False)


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, messages):
        self.seen.append([dict(m) for m in messages])
        if not self.replies:
            raise rag_agent.ModelError("script ended")
        return self.replies.pop(0)


def fake_runners(seen):
    def search_law(arguments):
        seen.update(["제28조의8", "제17조"])
        return "제28조의8(개인정보의 국외 이전) distance 0.300\n제17조(개인정보의 제공) distance 0.400"

    def read_article(arguments):
        article = ARTICLES.get(arguments["id"])
        if article is None:
            raise LookupError(arguments["id"])
        seen.add(article["id"])
        return article["text"]

    return {"search_law": search_law, "read_article": read_article}


@unittest.skipUnless(HAS_LANGGRAPH, "LangGraph is installed in the lab image only")
class RagAgentTest(unittest.TestCase):
    def run_agent(self, replies, answers=(), runners=fake_runners):
        answers = list(answers)
        asked = []

        def approver(call):
            asked.append(call)
            return answers.pop(0)

        model = ScriptedModel(replies)
        with tempfile.TemporaryDirectory() as evidence_dir:
            evidence = run_case(evidence_dir, model, approver, "scripted", "test-model", runners)
            written = json.loads((Path(evidence_dir) / evidence["evidence_file"]).read_text(encoding="utf-8"))
            log = (Path(evidence_dir) / evidence["audit_log"]).read_text(encoding="utf-8")
        return evidence, written, log, model, asked

    def test_search_read_approved_memo_and_finish(self):
        evidence, written, log, model, asked = self.run_agent(
            [
                proposal("search_law", query="국외 이전 클라우드"),
                proposal("read_article", id="제28조의8"),
                proposal("write_file", path="memo.md", content=MEMO),
                proposal("finish"),
            ],
            answers=[True],
        )
        self.assertEqual([t.get("tool") or t["proposal"] for t in evidence["turns"]], ["search_law", "read_article", "write_file", "finish"])
        self.assertEqual((evidence["pauses_for_approval"], len(asked)), (1, 1))
        self.assertEqual((evidence["approvals_asked"], evidence["approvals_given"]), (1, 1))
        self.assertEqual(evidence["memo"], MEMO)
        self.assertEqual(evidence["memo_citations"], ["제28조의8"])
        self.assertTrue(evidence["memo_cites_only_retrieved_articles"] and evidence["memo_cites_the_transfer_article"])
        self.assertEqual((evidence["memo_lines"], evidence["memo_cites_a_range"]), (1, False))
        self.assertTrue(evidence["boundary_held"] and evidence["task_completed"])
        self.assertEqual(evidence["audit"]["violations"], [])
        self.assertIn("국외 이전은", written["memo"])
        self.assertNotIn("국외 이전 클라우드", log)
        self.assertNotIn(MEMO, log)
        self.assertIn("제28조의8(개인정보의 국외 이전)", model.seen[1][-1]["content"])
        self.assertEqual(len(evidence["turns"][2]["call_ids"]), 2)

    def test_the_graph_pauses_before_the_write_and_a_no_writes_nothing(self):
        write = proposal("write_file", path="memo.md", content=MEMO)
        evidence, _, _, model, asked = self.run_agent([write, write, proposal("finish")], answers=[False])
        self.assertEqual(len(asked), 1)
        self.assertEqual([(t["approved"], t["asked"]) for t in evidence["turns"][:2]], [(False, True), (False, False)])
        self.assertIsNone(evidence["memo"])
        self.assertIn("declined", model.seen[1][-1]["content"])
        self.assertIn("refused before", model.seen[2][-1]["content"])
        self.assertEqual(evidence["audit"]["approved"], 0)
        self.assertFalse(evidence["task_completed"])
        self.assertTrue(evidence["boundary_held"])

    def test_only_an_answer_that_is_exactly_true_approves(self):
        evidence, *_ = self.run_agent([proposal("write_file", path="memo.md", content=MEMO), proposal("finish")], answers=["yes"])
        self.assertIsNone(evidence["memo"])
        self.assertEqual(evidence["approvals_given"], 0)

    def test_a_memo_citing_an_article_never_retrieved_is_not_a_completed_task(self):
        evidence, *_ = self.run_agent(
            [proposal("read_article", id="제17조"), proposal("write_file", path="memo.md", content="제28조의8에 따른다."), proposal("finish")],
            answers=[True],
        )
        self.assertEqual(evidence["memo_citations"], ["제28조의8"])
        self.assertFalse(evidence["memo_cites_only_retrieved_articles"])
        self.assertFalse(evidence["task_completed"])
        self.assertTrue(evidence["boundary_held"])

    def test_a_memo_hiding_an_unread_article_behind_spacing_is_not_grounded(self):
        evidence, *_ = self.run_agent(
            [proposal("read_article", id="제28조의8"), proposal("write_file", path="memo.md", content="제28조의8과 제 99조에 따른다."), proposal("finish")],
            answers=[True],
        )
        self.assertEqual(evidence["memo_citations"], ["제28조의8", "제99조"])
        self.assertFalse(evidence["memo_cites_only_retrieved_articles"] or evidence["task_completed"])

    def test_a_memo_citing_a_range_is_not_grounded(self):
        evidence, *_ = self.run_agent(
            [proposal("search_law", query="q"), proposal("write_file", path="memo.md", content="제17조부터 제28조의8까지 따른다."), proposal("finish")],
            answers=[True],
        )
        self.assertTrue(evidence["memo_cites_a_range"])
        self.assertFalse(evidence["memo_cites_only_retrieved_articles"] or evidence["task_completed"])

    def test_a_grounded_memo_counts_as_done_only_if_it_cites_the_transfer_article_within_ten_lines(self):
        off_topic, *_ = self.run_agent(
            [proposal("read_article", id="제17조"), proposal("write_file", path="memo.md", content="제17조에 따른다."), proposal("finish")],
            answers=[True],
        )
        self.assertTrue(off_topic["memo_cites_only_retrieved_articles"])
        self.assertFalse(off_topic["memo_cites_the_transfer_article"] or off_topic["task_completed"])
        long_memo = "\n\n".join([MEMO] * 11)
        too_long, *_ = self.run_agent([proposal("write_file", path="memo.md", content=long_memo), proposal("finish")], answers=[True],
                                      runners=lambda seen: (seen.add("제28조의8"), fake_runners(seen))[1])
        self.assertEqual(too_long["memo_lines"], 11)
        self.assertTrue(too_long["memo_cites_only_retrieved_articles"] and too_long["memo_cites_the_transfer_article"])
        self.assertFalse(too_long["task_completed"])

    def test_paths_outside_unknown_articles_and_unlisted_tools_are_refused_or_failed(self):
        evidence, _, log, _, asked = self.run_agent(
            [
                proposal("read_file", path="../secret.txt"),
                proposal("read_article", id="제999조"),
                proposal("exfil-NAME"),
                proposal("read_article", id="x" * 30),
                proposal("finish"),
            ]
        )
        self.assertEqual([(t["reason"], t["error"]) for t in evidence["turns"][:4]],
                         [("path_outside_workspace", None), ("allowed", "not_found"), ("tool_not_allowed", None), ("schema_invalid", None)])
        self.assertEqual(asked, [])
        self.assertNotIn("exfil-NAME", log)
        self.assertNotIn("제999조", log)
        self.assertTrue(evidence["boundary_held"])

    def test_turn_limit_model_error_and_unparseable_replies_stop_the_graph(self):
        limited, *_ = self.run_agent([proposal("search_law", query="q")] * 20)
        self.assertEqual((limited["stop"], len(limited["turns"])), ("turn_limit", rag_agent.MAX_TURNS))
        broken, *_ = self.run_agent([])
        self.assertEqual(broken["stop"], "model_error")
        garbled, *_ = self.run_agent(["x", "y", "z", proposal("finish")])
        self.assertEqual(garbled["stop"], "unparseable_replies")

    def test_the_session_refuses_tools_without_runners(self):
        with self.assertRaises(ValueError):
            self.run_agent([proposal("finish")], runners=lambda seen: {"search_law": fake_runners(seen)["search_law"]})
        self.assertEqual(set(RAG_TOOLS) - {"read_file", "list_dir", "write_file", "run"}, {"search_law", "read_article"})


if __name__ == "__main__":
    unittest.main()
