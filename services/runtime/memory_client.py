"""AgentCore Memory client — actor_id = the user JWT `sub` (per-user memory)."""

from __future__ import annotations

import os
import httpx

MEMORY_URL = os.environ.get("MEMORY_URL", "http://mock-memory:8080").rstrip("/")
MEMORY_ID = os.environ.get("MEMORY_ID", "mvp-memory")


def create_event(actor_id: str, session_id: str, messages: list[tuple[str, str]]) -> None:
    httpx.post(f"{MEMORY_URL}/create_event", json={
        "memoryId": MEMORY_ID, "actorId": actor_id, "sessionId": session_id,
        "messages": messages}, timeout=10).raise_for_status()


def list_events(actor_id: str, session_id: str) -> list[dict]:
    r = httpx.post(f"{MEMORY_URL}/list_events", json={
        "memoryId": MEMORY_ID, "actorId": actor_id, "sessionId": session_id}, timeout=10)
    r.raise_for_status()
    out = []
    for ev in r.json().get("events", []):
        for p in ev.get("payload", []):
            c = p.get("conversational", {})
            out.append({"role": c.get("role"), "text": c.get("content", {}).get("text", "")})
    return out
