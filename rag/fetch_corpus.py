"""Fetch the Personal Information Protection Act and keep its articles as the retrieval corpus.

Runs once on the host, outside the lab: the lab itself has no route to the internet. Statutes are
not protected works under the Copyright Act, Article 7(1), so the text can ship with the lab.

    python3 rag/fetch_corpus.py                      # fetch and write rag/corpus/pipa.json
    python3 rag/fetch_corpus.py --xml saved.xml      # parse a saved copy of the same response

One chunk per article. Amendment notes such as <개정 2023.3.14> are dropped from the text, and
deleted articles are left out and counted. Standard library only.
"""

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib import parse as urlparse
from urllib import request as urlrequest

LAW_NAME = "개인정보 보호법"
SOURCE = "https://www.law.go.kr/DRF/lawService.do?" + urlparse.urlencode(
    {"OC": "test", "target": "law", "type": "XML", "LM": LAW_NAME}
)
OUTPUT = Path(__file__).resolve().parent / "corpus" / "pipa.json"

# <개정 2023.3.14>, <신설 2020.2.4, 2023.3.14>, [본조신설 2023.3.14], [시행일: 2026. 9. 11.]
AMENDMENT_NOTE = re.compile(r"\s*(<[^<>]*\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}[^<>]*>|\[[^\[\]]*\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}[^\[\]]*\])")
DELETED = re.compile(r"^제\d+조(의\d+)?\s*삭제")


def clean(text: str) -> str:
    return AMENDMENT_NOTE.sub("", text or "").strip()


def _article_id(unit) -> str:
    branch = (unit.findtext("조문가지번호") or "").strip()
    return f"제{unit.findtext('조문번호').strip()}조" + (f"의{branch}" if branch else "")


def _article_text(unit) -> str:
    lines = [clean(unit.findtext("조문내용"))]
    for paragraph in unit.findall("항"):
        lines.append(clean(paragraph.findtext("항내용")))
        for item in paragraph.findall("호"):
            lines.append(clean(item.findtext("호내용")))
            lines.extend(clean(sub.findtext("목내용")) for sub in item.findall("목"))
    return "\n".join(line for line in lines if line)


def _date(raw: str) -> str:
    raw = (raw or "").strip()
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else raw


def parse_law(xml_bytes: bytes) -> dict:
    """The law's identity and its articles, in order; deleted articles are only counted."""
    root = ET.fromstring(xml_bytes)
    articles, deleted = [], 0
    for unit in root.iter("조문단위"):
        if unit.findtext("조문여부") != "조문":
            continue
        text = _article_text(unit)
        if DELETED.match(text):
            deleted += 1
            continue
        articles.append({"id": _article_id(unit), "title": (unit.findtext("조문제목") or "").strip(), "text": text})
    ids = [a["id"] for a in articles]
    if len(ids) != len(set(ids)):
        raise ValueError("article ids are not unique")
    return {
        "law": (root.findtext(".//법령명_한글") or "").strip(),
        "law_id": (root.findtext(".//법령ID") or "").strip(),
        "promulgated": _date(root.findtext(".//공포일자")),
        "promulgation_number": (root.findtext(".//공포번호") or "").strip(),
        "effective": _date(root.findtext(".//시행일자")),
        "deleted_articles": deleted,
        "articles": articles,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xml", help="parse this saved response instead of fetching")
    parser.add_argument("--out", default=str(OUTPUT))
    options = parser.parse_args()
    if options.xml:
        raw = Path(options.xml).read_bytes()
    else:
        with urlrequest.urlopen(SOURCE, timeout=60) as response:
            raw = response.read()
    law = parse_law(raw)
    if law["law"] != LAW_NAME or not law["articles"]:
        print(f"unexpected response: law={law['law']!r}, articles={len(law['articles'])}", file=sys.stderr)
        return 1
    corpus = {
        **{k: v for k, v in law.items() if k != "articles"},
        "source": SOURCE,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "fetched": date.today().isoformat(),
        "license_note": "Statutes are not protected works: Copyright Act (저작권법) Article 7(1).",
        "articles": law["articles"],
    }
    Path(options.out).write_text(json.dumps(corpus, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{law['law']} ({law['promulgation_number']}, effective {law['effective']}): "
          f"{len(law['articles'])} articles, {law['deleted_articles']} deleted -> {options.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
