"""Mock Bedrock — OpenAI-compatible chat + embeddings upstream.

Stands in for Bedrock behind Kong's ai-proxy. ai-proxy (provider=openai) sends
OpenAI-shaped requests here and transforms responses back for the orchestrator,
so the orchestrator's model calls genuinely traverse Kong → this upstream.
"""

from __future__ import annotations

import hashlib
from fastapi import FastAPI, Request

app = FastAPI(title="mock-bedrock")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    msgs = body.get("messages", [])
    user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
    answer = f"[mock-bedrock] reasoning over: {user[:120]}"
    return {
        "id": "chatcmpl-mock", "object": "chat.completion", "model": body.get("model", "mock"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inp = body.get("input", "")
    texts = inp if isinstance(inp, list) else [inp]
    data = []
    for i, t in enumerate(texts):
        # deterministic 1024-dim vector from the text (mock, but stable)
        seed = int.from_bytes(hashlib.md5(str(t).encode()).digest()[:4], "big")
        vec = [((seed >> (j % 24)) & 0xFF) / 255.0 for j in range(1024)]
        data.append({"object": "embedding", "index": i, "embedding": vec})
    return {"object": "list", "data": data, "model": body.get("model", "mock"),
            "usage": {"prompt_tokens": 8, "total_tokens": 8}}
