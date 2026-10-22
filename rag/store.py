"""The vector store: one PostgreSQL table with a pgvector column, on lab_internal only.

On AWS the same table maps to Aurora PostgreSQL with pgvector, a store Bedrock Knowledge Bases
can use. Search is an exact scan ordered by cosine distance: with 125 rows an approximate index
(HNSW) would change nothing measurable, and exact results keep runs comparable.

The password comes from a file (a compose secret), never from the environment or the source.
"""

import os

import psycopg

from retrieval import DIMENSIONS

HOST = os.environ.get("VECTOR_DB_HOST", "vector-db")
DATABASE = os.environ.get("VECTOR_DB_NAME", "rag")
USER = os.environ.get("VECTOR_DB_USER", "rag")
PASSWORD_FILE = os.environ.get("VECTOR_DB_PASSWORD_FILE", "/run/secrets/vector_db_password")


def connect(host: str = HOST, password_file: str = PASSWORD_FILE) -> psycopg.Connection:
    with open(password_file, encoding="utf-8") as f:
        password = f.read().strip()
    return psycopg.connect(host=host, dbname=DATABASE, user=USER, password=password, connect_timeout=10, autocommit=True)


def vector_literal(values: list) -> str:
    """pgvector's text form, so no client-side vector package is needed."""
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def create_schema(conn: psycopg.Connection) -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS chunks ("
        f" id text PRIMARY KEY, title text NOT NULL, body text NOT NULL,"
        f" embedding vector({DIMENSIONS}) NOT NULL)"
    )


def ingest(conn: psycopg.Connection, articles: list, vectors: list) -> int:
    """Insert or replace one row per article; returns the row count afterwards."""
    if len(articles) != len(vectors):
        raise ValueError("one vector per article")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (id, title, body, embedding) VALUES (%s, %s, %s, %s::vector)"
            " ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, body = EXCLUDED.body, embedding = EXCLUDED.embedding",
            [(a["id"], a["title"], a["text"], vector_literal(v)) for a, v in zip(articles, vectors)],
        )
    return count(conn)


def count(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT count(*) FROM chunks").fetchone()[0]


def ensure_ingested(conn: psycopg.Connection, articles: list, embed_fn) -> int:
    """Create the table and embed the corpus unless every article is already there."""
    create_schema(conn)
    if count(conn) == len(articles):
        return len(articles)
    return ingest(conn, articles, embed_fn([a["text"] for a in articles]))


def get(conn: psycopg.Connection, article_id: str):
    """One article by id, or None."""
    row = conn.execute("SELECT id, title, body FROM chunks WHERE id = %s", (article_id,)).fetchone()
    return None if row is None else {"id": row[0], "title": row[1], "text": row[2]}


def search(conn: psycopg.Connection, vector: list, k: int) -> list:
    """The k nearest articles by cosine distance, nearest first."""
    rows = conn.execute(
        "SELECT id, title, body, embedding <=> %s::vector AS distance FROM chunks ORDER BY distance, id LIMIT %s",
        (vector_literal(vector), k),
    ).fetchall()
    return [{"id": r[0], "title": r[1], "text": r[2], "distance": float(r[3])} for r in rows]
