"""Private-network RAG, graded: embed the corpus into pgvector, then for each question retrieve,
answer from what was retrieved, and check the answer's citations.

    docker compose --profile rag run --rm rag-eval

Every model call goes through the gateway (bge-m3 for vectors, qwen3:4b for answers); the store
and this container sit on lab_internal, with no route out. The evidence file records the corpus
hash, the retrieved ids per question, the answers and citations, and these measures:

- recall@k: the share of questions with a relevant article among the first k retrieved
- MRR: the mean of 1 / rank of the first relevant article (0 when none is retrieved)
- grounded: the answer cites at least one article, and every article it names, in its citations or
  in its text, is one the model was shown. Every question in the set has an answer in the act, so
  an answer that cites nothing is not counted as grounded; answers_without_citations counts those.
- cites a relevant article: one of the citations is an article that answers the question

The answer measures are over the questions that got an answer; `answered` and `answer_errors` say
how many did and did not.

The questions and their relevant articles were written by an AI from the article texts (see
eval_set.json); they are a smoke test of the pipeline, not a legal benchmark.
"""

import hashlib
import json
import os
import platform
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import retrieval

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus" / "pipa.json"
EVAL_SET = HERE / "eval_set.json"
EVIDENCE_DIR = os.environ.get("EVIDENCE_DIR", "/evidence")
TOP_K = 5


def evaluate(questions: list, search_fn, embed_fn, chat_fn, k: int = TOP_K) -> list:
    """Retrieve and answer each question; returns one result per question."""
    results = []
    for q in questions:
        vector = embed_fn([q["question"]])[0]
        hits = search_fn(vector, k)
        ranked = [h["id"] for h in hits]
        result = {
            "id": q["id"],
            "language": q["language"],
            "relevant": q["relevant"],
            "retrieved": ranked,
            "distances": [round(h["distance"], 4) for h in hits],
            "rank": retrieval.first_relevant_rank(ranked, q["relevant"]),
        }
        shown = ranked[:retrieval.CONTEXT_ARTICLES]
        try:
            reply = chat_fn(retrieval.answer_messages(q["question"], hits))
        except retrieval.ModelError as error:
            result["answer_error"] = str(error)
        else:
            citations = retrieval.normalize_citations(reply.get("citations"))
            text = reply.get("answer") if isinstance(reply.get("answer"), str) else ""
            mentioned = retrieval.normalize_citations([text])
            result["answer"] = {"text": text, "citations": citations, "mentioned": mentioned,
                                **retrieval.grade_answer(citations, shown, q["relevant"], mentioned)}
        results.append(result)
    return results


def main() -> int:
    # store needs psycopg, which only the lab image has; evaluate() does not.
    import store

    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    questions = json.loads(EVAL_SET.read_text(encoding="utf-8"))["questions"]
    articles = corpus["articles"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"corpus: {corpus['law']} ({len(articles)} articles); questions: {len(questions)}; "
          f"embed {retrieval.EMBED_MODEL}, answer {retrieval.CHAT_MODEL} via {retrieval.GATEWAY}")

    with store.connect() as conn:
        store.create_schema(conn)
        began = time.monotonic()
        vectors = retrieval.embed([retrieval.passage(a) for a in articles])
        rows = store.ingest(conn, articles, vectors)
        ingest_seconds = round(time.monotonic() - began, 1)
        print(f"ingested {rows} rows in {ingest_seconds} s")
        began = time.monotonic()
        results = evaluate(
            questions,
            lambda vector, k: store.search(conn, vector, k),
            retrieval.embed,
            lambda messages: retrieval.chat_json(messages, retrieval.ANSWER_SCHEMA),
        )
        eval_seconds = round(time.monotonic() - began, 1)

    summary = {"all": retrieval.summarize(results)}
    for language in sorted({q["language"] for q in questions}):
        summary[language] = retrieval.summarize([r for r in results if r["language"] == language])
    ordered = all(r["distances"] == sorted(r["distances"]) and len(set(r["retrieved"])) == len(r["retrieved"]) == TOP_K for r in results)
    evidence = {
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}, Python {platform.python_version()}",
        "embed_model": retrieval.EMBED_MODEL,
        "answer_model": retrieval.CHAT_MODEL,
        "corpus": {
            "law": corpus["law"],
            "promulgation_number": corpus["promulgation_number"],
            "effective": corpus["effective"],
            "articles": len(articles),
            "sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        },
        "store": {"rows": rows, "search": "exact, cosine distance", "results_ordered_and_distinct": ordered},
        "top_k": TOP_K,
        "articles_shown_to_the_answer_model": retrieval.CONTEXT_ARTICLES,
        "ingest_seconds": ingest_seconds,
        "eval_seconds": eval_seconds,
        "summary": summary,
        "results": results,
    }
    evidence_path = Path(EVIDENCE_DIR) / f"rag-eval-{stamp}.json"
    evidence["evidence_file"] = evidence_path.name
    with open(evidence_path, "x", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False, indent=1) + "\n")

    for r in results:
        answer = r.get("answer")
        graded = f"cited {answer['citations']} grounded={answer['grounded']} hit={answer['hit']}" if answer else r.get("answer_error")
        print(f"{r['id']}: rank {r['rank']} of {r['retrieved'][:3]}...; {graded}")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"rows {rows} · results ordered and distinct: {ordered} · evidence: {evidence_path.name}")
    return 0 if rows == len(articles) and ordered else 1


if __name__ == "__main__":
    sys.exit(main())
