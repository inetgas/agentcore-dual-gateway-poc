"""Tool calls via AgentCore Gateway (the MCP gateway), with workload-identity-minted
scoped JWTs.

Each tool call opens an MCP session to the Gateway presenting a token scoped to JUST
that tool (tool.retrieve / tool.submit_ticket); the Gateway validates the inbound JWT
and enforces the per-tool scope before running the tool. The scoped token never enters
agent state/logs/spans — it's injected by @requires_access_token.

(Model/reasoning calls still go through Kong — see model.py.)
"""

from __future__ import annotations

import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from identity import requires_access_token

GATEWAY_MCP_URL = os.environ.get("GATEWAY_MCP_URL", "http://mock-agentcore-gateway:9000/mcp")


async def _call_gateway_tool(tool: str, args: dict, access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with streamablehttp_client(GATEWAY_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return json.loads(result.content[0].text)


@requires_access_token(provider_name="okta-orchestrator", scopes=["tool.retrieve"], auth_flow="M2M")
async def _retrieve(*, query: str, top_k: int, access_token: str) -> dict:
    return await _call_gateway_tool("retrieve_documents", {"query": query, "top_k": top_k}, access_token)


async def retrieve_via_gateway(query: str, top_k: int = 5) -> list[dict]:
    return (await _retrieve(query=query, top_k=top_k)).get("chunks", [])


@requires_access_token(provider_name="okta-orchestrator", scopes=["tool.submit_ticket"], auth_flow="M2M")
async def _submit(*, payload: dict, access_token: str) -> dict:
    return await _call_gateway_tool("submit_ticket", payload, access_token)


async def submit_ticket_via_gateway(payload: dict) -> dict:
    return await _submit(payload=payload)
