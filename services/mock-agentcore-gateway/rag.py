"""Demo RAG core: deterministic embedder + pgvector retrieval + extractive synthesis.

Real retrieval-augmented generation, runnable with zero cloud credentials:
  - embeddings: deterministic hashed bag-of-words (1024-dim), L2-normalised
  - vector store: real Postgres + pgvector (cosine `<=>`)
  - synthesis: EXTRACTIVE — the answer is grounded in the top retrieved chunk(s)
    with citations. Pluggable: set SYNTHESIZER=bedrock to use an LLM later.

This mirrors the production retrieve_documents contract and the documents schema;
the only swap for prod is the embedder (Titan via Kong) and the synthesizer
(Bedrock Claude).
"""

from __future__ import annotations

import hashlib
import math
import os
import re

import psycopg

EMBED_DIM = 1024
_WORD = re.compile(r"[a-z0-9]+")
_SENT = re.compile(r"(?<=[.!?])\s+")


def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for tok in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def connect():
    return psycopg.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "rag"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
        sslmode=os.environ.get("PGSSLMODE", "disable"),
    )


def retrieve(conn, query: str, k: int = 5) -> list[dict]:
    vec = vector_literal(embed(query))
    sql = (
        "SELECT source_uri, chunk_text, metadata, (embedding <=> %s::vector) AS distance "
        "FROM documents ORDER BY embedding <=> %s::vector LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (vec, vec, k))
        rows = cur.fetchall()
    out = []
    for source_uri, chunk_text, metadata, distance in rows:
        out.append({
            "source_uri": source_uri,
            "text": chunk_text,
            "metadata": metadata,
            "relevance_score": round(max(0.0, 1.0 - float(distance)), 2),
        })
    return out


def _lead_sentences(text: str, n: int = 3) -> str:
    # drop a leading markdown heading line if present
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    body = " ".join(body.split())
    sents = _SENT.split(body)
    return " ".join(sents[:n]).strip()


def synthesize(query: str, chunks: list[dict]) -> str:
    """Extractive answer grounded in the top chunk(s). (SYNTHESIZER=bedrock would
    replace this with a Claude call over the same chunks.)"""
    if not chunks:
        return "I couldn't find anything relevant in the documentation."
    return _lead_sentences(chunks[0]["text"], 3)
