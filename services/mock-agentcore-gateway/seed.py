"""Seed the demo corpus into pgvector (one chunk per short doc). Idempotent."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

import rag


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            " id BIGSERIAL PRIMARY KEY, source_uri TEXT NOT NULL, chunk_index INT NOT NULL,"
            " chunk_text TEXT NOT NULL, metadata JSONB NOT NULL, embedding vector(1024) NOT NULL)"
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS documents_uri_chunk ON documents (source_uri, chunk_index)")
    conn.commit()


def parse_doc(path: Path):
    """Return (meta, body, embed_text). body is clean (no frontmatter) for display;
    embed_text includes the title/tags to strengthen lexical retrieval."""
    raw = path.read_text(encoding="utf-8")
    meta, body = {}, raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            meta = yaml.safe_load(raw[3:end]) or {}
            body = raw[end + 4:].strip()
    meta.setdefault("title", path.stem.replace("-", " ").title())
    embed_text = f"{meta.get('title','')} {' '.join(meta.get('tags', []))} {body}"
    return meta, body, embed_text


def seed(corpus_dir: str) -> int:
    conn = rag.connect()
    try:
        ensure_schema(conn)
        files = sorted(Path(corpus_dir).rglob("*.md"))
        n = 0
        with conn.cursor() as cur:
            for f in files:
                meta, body, embed_text = parse_doc(f)
                source_uri = f.name  # mockup shows bare filenames
                cur.execute(
                    "INSERT INTO documents (source_uri, chunk_index, chunk_text, metadata, embedding)"
                    " VALUES (%s,0,%s,%s,%s::vector)"
                    " ON CONFLICT (source_uri, chunk_index) DO UPDATE SET"
                    "   chunk_text=EXCLUDED.chunk_text, metadata=EXCLUDED.metadata, embedding=EXCLUDED.embedding",
                    (source_uri, body, json.dumps(meta), rag.vector_literal(rag.embed(embed_text))),
                )
                n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def count(conn) -> int:
    with conn.cursor() as cur:
        return cur.execute("SELECT count(*) FROM documents").fetchone()[0]


if __name__ == "__main__":
    d = os.environ.get("CORPUS_DIR", "/corpus")
    print(f"seeded {seed(d)} docs from {d}")
