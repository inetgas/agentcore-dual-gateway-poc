"""Mock AgentCore Memory — same data-plane contract as AWS AgentCore Memory.

Implements the two operations the orchestrator uses (entry_node reads prior turns,
response_node writes the new turn):
  create_event(memoryId, actorId, sessionId, messages=[(text, role)])
  list_events(memoryId, actorId, sessionId) -> ordered conversational events

Durable: backed by Postgres (the same RDS-equivalent the demo already runs), so
conversation memory survives an orchestrator restart — proving real persistence
through the AgentCore Memory interface. Swap for AWS in prod = point the client
at boto3 `bedrock-agentcore` instead of this service.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from pydantic import BaseModel


def connect():
    return psycopg.connect(
        host=os.environ.get("PGHOST", "pgvector"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "rag"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
        sslmode=os.environ.get("PGSSLMODE", "disable"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS memory_events ("
                " id BIGSERIAL PRIMARY KEY, memory_id TEXT NOT NULL, actor_id TEXT NOT NULL,"
                " session_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,"
                " created TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS memory_events_lookup "
                        "ON memory_events (memory_id, actor_id, session_id, id)")
        conn.commit()
    finally:
        conn.close()
    yield


app = FastAPI(title="mock-agentcore-memory", lifespan=lifespan)


class CreateEvent(BaseModel):
    memoryId: str
    actorId: str
    sessionId: str
    messages: list[tuple[str, str]]  # [(text, role)]


class ListEvents(BaseModel):
    memoryId: str
    actorId: str
    sessionId: str
    maxResults: int = 100


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/create_event")
def create_event(req: CreateEvent):
    conn = connect()
    try:
        with conn.cursor() as cur:
            for text, role in req.messages:
                cur.execute(
                    "INSERT INTO memory_events (memory_id, actor_id, session_id, role, text)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (req.memoryId, req.actorId, req.sessionId, role, text),
                )
        conn.commit()
    finally:
        conn.close()
    return {"stored": len(req.messages)}


@app.post("/list_events")
def list_events(req: ListEvents):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, text FROM memory_events"
                " WHERE memory_id=%s AND actor_id=%s AND session_id=%s"
                " ORDER BY id LIMIT %s",
                (req.memoryId, req.actorId, req.sessionId, req.maxResults),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    # AgentCore-style conversational events
    return {"events": [{"payload": [{"conversational": {"role": r, "content": {"text": t}}}]}
                       for r, t in rows]}
