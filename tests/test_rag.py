"""The RAG pieces that run without a database: corpus parsing, the committed corpus and question
set, the gateway calls, the measures, and the evaluation loop with a fake store and model.

The pgvector store itself is exercised by `docker compose --profile rag run --rm rag-eval`.
"""

import json
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag"))

import retrieval  # noqa: E402
from fetch_corpus import parse_law  # noqa: E402
from rag_eval import CORPUS, EVAL_SET, evaluate  # noqa: E402

LAW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<법령><기본정보><법령ID>1</법령ID><공포일자>20260310</공포일자><공포번호>21445</공포번호>
<법령명_한글><![CDATA[개인정보 보호법]]></법령명_한글><시행일자>20260911</시행일자></기본정보>
<조문>
<조문단위><조문번호>3</조문번호><조문여부>전문</조문여부><조문내용>제2장 개인정보 보호정책의 수립 등</조문내용></조문단위>
<조문단위><조문번호>7</조문번호><조문가지번호>2</조문가지번호><조문여부>조문</조문여부><조문제목>보호위원회의 구성 등</조문제목>
<조문내용>제7조의2(보호위원회의 구성 등)</조문내용>
<항><항내용>① 보호위원회는 위원으로 구성한다. &lt;개정 2023.3.14&gt;</항내용>
<호><호내용>1. 첫째 사항</호내용><목><목내용>가. 세부 사항</목내용></목></호></항>
</조문단위>
<조문단위><조문번호>8</조문번호><조문여부>조문</조문여부><조문내용>제8조 삭제 &lt;2020. 2. 4.&gt;</조문내용></조문단위>
<조문단위><조문번호>9</조문번호><조문여부>조문</조문여부><조문제목>기본계획</조문제목>
<조문내용>제9조(기본계획) 보호위원회는 3년마다 기본계획을 수립한다. [본조신설 2023.3.14]</조문내용></조문단위>
</조문></법령>""".encode("utf-8")


class FetchCorpusTest(unittest.TestCase):
    def test_articles_are_kept_with_their_items_and_without_amendment_notes(self):
        law = parse_law(LAW_XML)
        self.assertEqual((law["law"], law["promulgation_number"], law["effective"]), ("개인정보 보호법", "21445", "2026-09-11"))
        self.assertEqual([a["id"] for a in law["articles"]], ["제7조의2", "제9조"])
        self.assertEqual(law["deleted_articles"], 1)
        first = law["articles"][0]
        self.assertEqual(first["text"].splitlines(), ["제7조의2(보호위원회의 구성 등)", "① 보호위원회는 위원으로 구성한다.", "1. 첫째 사항", "가. 세부 사항"])
        self.assertEqual(law["articles"][1]["text"], "제9조(기본계획) 보호위원회는 3년마다 기본계획을 수립한다.")

    def test_repeated_article_ids_are_refused(self):
        unit = "<조문단위><조문번호>9</조문번호><조문여부>조문</조문여부><조문내용>제9조(가) 내용</조문내용></조문단위>"
        with self.assertRaises(ValueError):
            parse_law(f"<법령><조문>{unit}{unit}</조문></법령>".encode("utf-8"))


class CommittedDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        cls.questions = json.loads(EVAL_SET.read_text(encoding="utf-8"))["questions"]
        cls.articles = {a["id"]: a for a in cls.corpus["articles"]}

    def test_corpus_is_the_act_with_unique_ids_and_no_amendment_notes(self):
        self.assertEqual(self.corpus["law"], "개인정보 보호법")
        self.assertEqual(len(self.articles), len(self.corpus["articles"]))
        self.assertGreater(len(self.articles), 100)
        for article in self.corpus["articles"]:
            self.assertTrue(article["text"].startswith(article["id"]), article["id"])
            self.assertIsNone(re.search(r"<[^<>]*\d{4}\s*\.", article["text"]), article["id"])

    def test_every_question_names_existing_articles_and_does_not_quote_their_titles(self):
        self.assertEqual(len({q["id"] for q in self.questions}), len(self.questions))
        self.assertEqual({q["language"] for q in self.questions}, {"ko", "en"})
        for q in self.questions:
            for article_id in q["relevant"]:
                self.assertIn(article_id, self.articles, q["id"])
                self.assertNotIn(self.articles[article_id]["title"], q["question"], q["id"])


class MeasureTest(unittest.TestCase):
    def test_rank_recall_and_mrr(self):
        results = [{"rank": 1}, {"rank": 3}, {"rank": None}, {"rank": 5}]
        summary = retrieval.summarize(results)
        self.assertEqual((summary["recall_at_1"], summary["recall_at_3"], summary["recall_at_5"]), (0.25, 0.5, 0.75))
        self.assertEqual(summary["mrr"], round((1 + 1 / 3 + 1 / 5) / 4, 3))
        self.assertEqual(retrieval.first_relevant_rank(["제1조", "제15조"], ["제15조", "제17조"]), 2)
        self.assertIsNone(retrieval.first_relevant_rank(["제1조"], ["제15조"]))

    def test_citations_are_extracted_once_and_graded_against_what_was_shown(self):
        self.assertEqual(retrieval.normalize_citations(["제15조 제1항", "Article 제39조의2", "제15조", 7]), ["제15조", "제39조의2"])
        self.assertEqual(retrieval.normalize_citations("제15조"), [])
        self.assertEqual(retrieval.grade_answer(["제15조"], ["제15조", "제17조"], ["제17조"]), {"cited": 1, "grounded": True, "hit": False})
        self.assertEqual(retrieval.grade_answer(["제99조"], ["제15조"], ["제99조"]), {"cited": 1, "grounded": False, "hit": True})
        self.assertFalse(retrieval.grade_answer([], ["제15조"], ["제15조"])["grounded"])
        mentioned = retrieval.grade_answer(["제15조"], ["제15조"], ["제15조"], mentioned=["제99조"])
        self.assertEqual((mentioned["grounded"], mentioned["hit"]), (False, True))

    def test_citations_written_with_spaces_or_in_english_are_still_found(self):
        self.assertEqual(retrieval.normalize_citations(["제 99조, 제28조의 8 and Article 28-8", "Articles 17, 18-2 and 21(1)", "28조의2"]),
                         ["제99조", "제28조의8", "제17조", "제18조의2", "제21조", "제28조의2"])
        self.assertEqual(retrieval.normalize_citations(["제28조의 규정에 따라"]), ["제28조"])
        self.assertEqual(retrieval.normalize_citations(["Articles 17, 18, and 99; Article 21 & 22"]), ["제17조", "제18조", "제99조", "제21조", "제22조"])

    def test_a_range_of_articles_is_flagged_because_its_middle_cannot_be_listed(self):
        for text in ("제17조부터 제19조까지", "Articles 28-8–28-10", "Articles 17 to 19", "Articles 17 through 19", "제17조~제19조"):
            with self.subTest(text=text):
                self.assertTrue(retrieval.cites_a_range(text))
        for text in ("제17조, 제18조 및 제19조", "Article 17 to transfer data", "Article 28-8(1)"):
            with self.subTest(text=text):
                self.assertFalse(retrieval.cites_a_range(text))

    def test_the_answer_prompt_shows_only_the_first_articles_cut_to_length(self):
        hits = [{"id": f"제{n}조", "text": f"제{n}조 " + "가" * 5000} for n in range(1, 6)]
        prompt = retrieval.answer_messages("질문", hits)[1]["content"]
        self.assertEqual(re.findall(r"^\[(제\d+조)\]$", prompt, re.M), ["제1조", "제2조", "제3조"])
        self.assertLess(len(prompt), retrieval.CONTEXT_ARTICLES * (retrieval.ARTICLE_CHARS + 20) + 100)


class FakeGatewayTest(unittest.TestCase):
    def serve(self, reply):
        received = []

        class Fake(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, body))
                status, payload = reply(body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", received

    def test_embed_batches_the_texts_and_keeps_their_order(self):
        url, received = self.serve(lambda body: (200, {"embeddings": [[float(len(t))] * retrieval.DIMENSIONS for t in body["input"]]}))
        with mock.patch.object(retrieval, "EMBED_BATCH", 2):
            vectors = retrieval.embed(["a", "bb", "ccc"], gateway=url, model="bge-m3", timeout=5)
        self.assertEqual([v[0] for v in vectors], [1.0, 2.0, 3.0])
        self.assertEqual([(path, body["model"], body["input"]) for path, body in received],
                         [("/api/embed", "bge-m3", ["a", "bb"]), ("/api/embed", "bge-m3", ["ccc"])])

    def test_embeddings_of_the_wrong_shape_or_count_are_refused(self):
        for payload in ({"embeddings": [[0.0] * 3]}, {"embeddings": []}, {"embeddings": [["x"] * retrieval.DIMENSIONS]}, {"error": "no"}):
            with self.subTest(payload=str(payload)[:40]):
                url, _ = self.serve(lambda body, p=payload: (200, p))
                with self.assertRaises(retrieval.ModelError):
                    retrieval.embed(["a"], gateway=url, timeout=5)

    def test_chat_asks_for_the_schema_without_thinking_and_rejects_non_json(self):
        url, received = self.serve(lambda body: (200, {"message": {"content": json.dumps({"answer": "a", "citations": []})}}))
        self.assertEqual(retrieval.chat_json([{"role": "user", "content": "q"}], retrieval.ANSWER_SCHEMA, gateway=url, model="qwen3:4b", timeout=5),
                         {"answer": "a", "citations": []})
        body = received[0][1]
        self.assertEqual((received[0][0], body["format"], body["think"], body["stream"]), ("/api/chat", retrieval.ANSWER_SCHEMA, False, False))
        bad, _ = self.serve(lambda body: (200, {"message": {"content": "not json"}}))
        with self.assertRaises(retrieval.ModelError):
            retrieval.chat_json([], retrieval.ANSWER_SCHEMA, gateway=bad, timeout=5)
        refused, _ = self.serve(lambda body: (403, {"error": "model_not_allowed"}))
        with self.assertRaises(retrieval.ModelError):
            retrieval.embed(["a"], gateway=refused, timeout=5)


class EvaluateTest(unittest.TestCase):
    def test_each_question_is_retrieved_answered_and_graded(self):
        questions = [
            {"id": "q1", "language": "ko", "question": "파기", "relevant": ["제21조"]},
            {"id": "q2", "language": "en", "question": "transfer", "relevant": ["제28조의8"]},
            {"id": "q3", "language": "ko", "question": "파기", "relevant": ["제21조"]},
        ]
        hits = {
            "파기": ["제21조", "제20조", "제22조", "제23조", "제24조"],
            "transfer": ["제17조", "제18조", "제19조", "제28조의8", "제29조"],
        }
        seen = []

        def search(vector, k):
            return [{"id": i, "text": i, "distance": n / 10} for n, i in enumerate(hits[vector][:k])]

        def chat(messages):
            seen.append(messages[1]["content"])
            if len(seen) == 2:
                raise retrieval.ModelError("gateway status 503")
            if len(seen) == 3:
                return {"answer": "제99조에 따라 가능하다", "citations": ["제21조"]}
            return {"answer": "파기한다", "citations": ["제21조", "제21조"]}

        results = evaluate(questions, search, lambda texts: [texts[0]], chat)
        self.assertEqual([r["rank"] for r in results], [1, 4, 1])
        self.assertEqual(results[0]["answer"], {"text": "파기한다", "citations": ["제21조"], "mentioned": [],
                                                "cited": 1, "grounded": True, "hit": True})
        self.assertEqual(results[1]["answer_error"], "gateway status 503")
        self.assertEqual((results[2]["answer"]["mentioned"], results[2]["answer"]["grounded"]), (["제99조"], False))
        self.assertNotIn("제28조의8", seen[1])
        summary = retrieval.summarize(results)
        self.assertEqual((summary["recall_at_1"], summary["recall_at_5"]), (round(2 / 3, 3), 1.0))
        self.assertEqual((summary["answered"], summary["answer_errors"], summary["answers_grounded"]), (2, 1, 0.5))


if __name__ == "__main__":
    unittest.main()
