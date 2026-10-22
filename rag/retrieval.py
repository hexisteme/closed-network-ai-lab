"""Retrieval pieces that need no database: the embedding and chat calls through the gateway, the
answer prompt, and the measures a run is graded by.

The vector store is in store.py; this module stays standard library only so that its tests run
anywhere.
"""

import json
import os
import re
from urllib import error as urlerror
from urllib import request as urlrequest

GATEWAY = os.environ.get("GATEWAY", "http://model-gateway:8080")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
CHAT_MODEL = os.environ.get("MODEL", "qwen3:4b")
DIMENSIONS = 1024
EMBED_BATCH = 16
TIMEOUT = 300
CONTEXT_ARTICLES = 3
ARTICLE_CHARS = 2000

# 제28조의8, also written 제 28조의 8 or 28조의8, and Article 28-8 or Articles 17, 18-2, and 21. Reading
# too much as a citation only makes the grounding check stricter; reading too little would hide one.
_ENGLISH_SEPARATOR = r"\s*(?:,|;|&|\band\b|\bor\b|\bto\b|\bthrough\b|[–—~])\s*"
CITATION = re.compile(
    r"(?:제\s*)?(?P<number>\d+)\s*조(?:\s*의\s*(?P<branch>\d+))?"
    r"|\bArticles?\s+(?P<english>\d+(?:-\d+)?(?:(?:" + _ENGLISH_SEPARATOR + r")+\d+(?:-\d+)?)*)",
    re.IGNORECASE,
)
# A range names articles between its ends without listing them, so no check can say they were read.
KOREAN_RANGE = re.compile(r"조(?:\s*의\s*\d+)?\s*(?:부터|에서|[~∼〜–—-])\s*(?:제\s*)?\d+\s*조")
ENGLISH_RANGE = re.compile(r"\d\s*(?:\bto\b|\bthrough\b|[–—~])\s*\d", re.IGNORECASE)
HYPHEN_RANGE = re.compile(r"\bArticles?\s+\d+-\d+-\d+", re.IGNORECASE)
ENGLISH_NUMBER = re.compile(r"(\d+)(?:-(\d+))?")

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "citations"],
}

ANSWER_PROMPT = """You answer questions about Korea's Personal Information Protection Act (개인정보 보호법).
Use only the articles given below. Answer in the language of the question, in at most three sentences.
List in "citations" the ids of the articles you relied on, written exactly as given (for example 제15조 or 제39조의2).
If the articles do not answer the question, say so and leave "citations" empty."""


class ModelError(Exception):
    """The gateway or the model gave no usable answer."""


def _post(path: str, body: dict, gateway: str, timeout: float) -> dict:
    request = urlrequest.Request(
        gateway.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read())
    except urlerror.HTTPError as error:
        raise ModelError(f"gateway status {error.code}") from None
    except (urlerror.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise ModelError(type(error).__name__) from None
    if not isinstance(reply, dict):
        raise ModelError("reply is not an object")
    return reply


def embed(texts: list, gateway: str = GATEWAY, model: str = EMBED_MODEL, timeout: float = TIMEOUT) -> list:
    """One vector per text, in order, each of DIMENSIONS floats."""
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        reply = _post("/api/embed", {"model": model, "input": batch}, gateway, timeout)
        got = reply.get("embeddings")
        if not isinstance(got, list) or len(got) != len(batch):
            raise ModelError("embedding count does not match the input")
        for vector in got:
            if not isinstance(vector, list) or len(vector) != DIMENSIONS or not all(isinstance(x, (int, float)) for x in vector):
                raise ModelError(f"embedding is not {DIMENSIONS} numbers")
        vectors.extend(got)
    return vectors


def chat_json(messages: list, schema: dict, gateway: str = GATEWAY, model: str = CHAT_MODEL, timeout: float = TIMEOUT) -> dict:
    """One model turn constrained to a JSON schema, thinking off; returns the parsed object."""
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "format": schema,
        "options": {"temperature": 0, "num_predict": 512, "num_ctx": 8192},
    }
    content = _post("/api/chat", body, gateway, timeout).get("message", {}).get("content")
    if not isinstance(content, str):
        raise ModelError("no message content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise ModelError("reply is not JSON") from None
    if not isinstance(parsed, dict):
        raise ModelError("reply is not an object")
    return parsed


def passage(article: dict) -> str:
    """The text that is embedded and shown for one article; it starts with the id and title."""
    return article["text"]


def answer_messages(question: str, articles: list) -> list:
    """The prompt for one answer: the first CONTEXT_ARTICLES retrieved articles, each cut to ARTICLE_CHARS."""
    context = "\n\n".join(f"[{a['id']}]\n{a['text'][:ARTICLE_CHARS]}" for a in articles[:CONTEXT_ARTICLES])
    return [
        {"role": "system", "content": ANSWER_PROMPT},
        {"role": "user", "content": f"Articles:\n{context}\n\nQuestion: {question}"},
    ]


def normalize_citations(raw) -> list:
    """Article ids found in the model's citations, in order, without repeats."""
    found = []
    for item in raw if isinstance(raw, list) else []:
        for match in CITATION.finditer(item if isinstance(item, str) else ""):
            if match["english"] is None:
                numbers = [(match["number"], match["branch"])]
            else:
                numbers = ENGLISH_NUMBER.findall(match["english"])
            for number, branch in numbers:
                article_id = f"제{number}조" + (f"의{branch}" if branch else "")
                if article_id not in found:
                    found.append(article_id)
    return found


def cites_a_range(text: str) -> bool:
    """Whether the text cites a range of articles (제17조부터 제19조까지, Articles 17 to 19)."""
    if KOREAN_RANGE.search(text) or HYPHEN_RANGE.search(text):
        return True
    return any(m["english"] and ENGLISH_RANGE.search(m["english"]) for m in CITATION.finditer(text))


def first_relevant_rank(ranked: list, relevant: list):
    """1-based rank of the first relevant id, or None if none was retrieved."""
    for rank, article_id in enumerate(ranked, 1):
        if article_id in relevant:
            return rank
    return None


def grade_answer(citations: list, shown: list, relevant: list, mentioned=()) -> dict:
    """Whether the answer cites something and names only articles the model was shown, in its citations
    and in its text (mentioned), and whether a citation is relevant."""
    return {
        "cited": len(citations),
        "grounded": bool(citations) and all(c in shown for c in [*citations, *mentioned]),
        "hit": any(c in relevant for c in citations),
    }


def summarize(results: list, ks=(1, 3, 5)) -> dict:
    """Recall at each k and mean reciprocal rank over the questions, plus the answer rates."""
    n = len(results)
    if n == 0:
        return {"questions": 0}
    ranks = [r["rank"] for r in results]
    summary = {"questions": n}
    for k in ks:
        summary[f"recall_at_{k}"] = round(sum(1 for rank in ranks if rank is not None and rank <= k) / n, 3)
    summary["mrr"] = round(sum(1 / rank for rank in ranks if rank is not None) / n, 3)
    answered = [r for r in results if "answer" in r]
    # The answer rates are over the answered questions; answer_errors counts the rest.
    summary["answered"] = len(answered)
    summary["answer_errors"] = n - len(answered)
    if answered:
        summary["answers_grounded"] = round(sum(1 for r in answered if r["answer"]["grounded"]) / len(answered), 3)
        summary["answers_citing_a_relevant_article"] = round(sum(1 for r in answered if r["answer"]["hit"]) / len(answered), 3)
        summary["answers_without_citations"] = sum(1 for r in answered if r["answer"]["cited"] == 0)
    return summary
