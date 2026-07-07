"""Mock AgentCore Gateway — the MCP gateway for tools.

Stands in for Amazon Bedrock AgentCore Gateway: a single managed **MCP** endpoint that
exposes tools to the agent. Mirrors the real behaviour the PoC cares about:

  - **Inbound auth:** every MCP request must carry a valid bearer JWT (the orchestrator's
    workload-identity M2M token). Validated here (HS256, same key Okta signs with).
  - **Fine-grained per-tool authz:** a `tools/call` is allowed only if the token's `scp`
    contains the scope that tool requires (retrieve_documents → tool.retrieve;
    submit_ticket → tool.submit_ticket). Enforced at the gateway edge.
  - **Tools as MCP:** retrieve_documents (pgvector) and submit_ticket are exposed as MCP
    tools over streamable-HTTP.

In production the Gateway turns REST/Lambda targets into MCP tools and uses AgentCore
Identity for egress; here the tool logic is implemented inline (self-contained mock).
"""

from __future__ import annotations

import itertools
import json
import os

import jwt
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import rag
import seed as seed_mod

HS_SECRET = os.environ.get("OKTA_HS_SECRET", "mock-okta-hs256-shared-secret")
CORPUS_DIR = os.environ.get("CORPUS_DIR", "/corpus")
PORT = int(os.environ.get("PORT", "9000"))

# Which scope each tool requires (the gateway's fine-grained access control).
SCOPE_FOR = {"retrieve_documents": "tool.retrieve", "submit_ticket": "tool.submit_ticket"}

_seq = itertools.count(421)

# Reached over the internal network from the orchestrator; the gateway (not a host
# check) is the trust boundary, so disable the MCP SDK's DNS-rebinding protection.
mcp = FastMCP("agentcore-gateway", streamable_http_path="/mcp",
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


@mcp.tool(description="Search internal IT/policy/runbook docs (pgvector). Returns chunks with citations.")
def retrieve_documents(query: str, top_k: int = 5) -> dict:
    conn = rag.connect()
    try:
        chunks = rag.retrieve(conn, query, k=top_k)
    finally:
        conn.close()
    return {"chunks": chunks, "stats": {"candidates_considered": len(chunks)}}


@mcp.tool(description="Create a ticket in the IT/engineering ticketing system. Returns the ticket id.")
def submit_ticket(summary: str, description: str, requested_resource: str, justification: str) -> dict:
    return {"ticket_id": f"DL-2026-{next(_seq):04d}", "status": "created",
            "summary": summary, "requested_resource": requested_resource}


# --- Inbound auth + per-tool scope, enforced at the gateway edge (ASGI middleware) ----

async def _send_json(send, status: int, obj: dict):
    body = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def gateway_auth(app):
    """Plain ASGI middleware (runs in the request task — no contextvar issues):
      - validate the bearer JWT on every HTTP request (inbound auth);
      - on a POST tools/call, also require the tool's scope (fine-grained authz).
    Only POST bodies are peeked (and faithfully replayed, then delegated to the real
    receive); GET/SSE streams pass through untouched so streamable-HTTP isn't broken."""
    async def mw(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)  # lifespan/etc → MCP app (runs session mgr)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        authz = headers.get("authorization", "")
        token = authz[7:].strip() if authz.lower().startswith("bearer ") else ""
        if not token:
            return await _send_json(send, 401, {"error": "missing token"})
        try:
            claims = jwt.decode(token, HS_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        except Exception as e:  # noqa: BLE001
            return await _send_json(send, 401, {"error": f"invalid token: {e}"})

        if scope.get("method", "GET").upper() != "POST":
            return await app(scope, receive, send)  # SSE GET / others: token ok, pass through

        # POST: peek the body to enforce per-tool scope on tools/call.
        chunks, body = [], b""
        while True:
            m = await receive()
            chunks.append(m)
            body += m.get("body", b"")
            if not m.get("more_body", False):
                break
        try:
            rpc = json.loads(body) if body else {}
            if isinstance(rpc, dict) and rpc.get("method") == "tools/call":
                tool = (rpc.get("params") or {}).get("name")
                need = SCOPE_FOR.get(tool)
                if need and need not in (claims.get("scp") or []):
                    return await _send_json(send, 403, {"error": f"forbidden: missing scope {need}"})
        except (ValueError, TypeError):
            pass  # not JSON-RPC we gate; let the MCP app handle it

        _it = iter(chunks)

        async def replay():
            try:
                return next(_it)            # replay the buffered body chunks...
            except StopIteration:
                return await receive()       # ...then the real receive (disconnect, etc.)

        return await app(scope, replay, send)

    return mw


app = gateway_auth(mcp.streamable_http_app())


if __name__ == "__main__":
    # Seed the corpus into pgvector (idempotent) before serving.
    conn = rag.connect()
    try:
        seed_mod.ensure_schema(conn)
        if seed_mod.count(conn) == 0:
            print(f"[gateway] seeded {seed_mod.seed(CORPUS_DIR)} docs into pgvector", flush=True)
    finally:
        conn.close()
    import uvicorn
    print(f"[gateway] AgentCore Gateway (MCP) on :{PORT}/mcp — tools: {list(SCOPE_FOR)}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
