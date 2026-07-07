"""LangGraph tools — thin @tool wrappers over the AgentCore Gateway MCP calls in
gateway.py (each decorated with @requires_access_token for its per-tool scope).

`bind_tools` advertises these schemas to the model; execution keeps the scoped-JWT
mint, the AgentCore Gateway (MCP) path, and the spans intact underneath. Each tool
returns content_and_artifact: the string content is what the model reads on the next
turn; the artifact carries structured data (citations / ticket id) the agent collects
out of band for the response and the span attributes.
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool

import gateway


@tool(response_format="content_and_artifact")
def retrieve_documents(query: str):
    """Search internal IT / policy / runbook documentation. Use for any informational
    question, or to gather grounding before filing a ticket."""
    chunks = asyncio.run(gateway.retrieve_via_gateway(query))
    citations = [{"source_uri": c["source_uri"], "relevance_score": c["relevance_score"]}
                 for c in chunks]
    if not chunks:
        return "No relevant documents were found.", {"citations": []}
    content = "\n\n".join(f"[{i + 1}] ({c['source_uri']}) {c['text']}"
                          for i, c in enumerate(chunks[:4]))
    return content, {"citations": citations}


@tool(response_format="content_and_artifact")
def submit_ticket(summary: str, requested_resource: str, justification: str):
    """File an access / IT support ticket. Use only after gathering the details needed
    (the resource and a justification). Returns the created ticket id."""
    result = asyncio.run(gateway.submit_ticket_via_gateway({
        "summary": summary, "description": justification,
        "requested_resource": requested_resource, "justification": justification}))
    tid = result.get("ticket_id", "unknown")
    return (f"Ticket {tid} created for {requested_resource}.",
            {"ticket_id": tid, "requested_resource": requested_resource})


TOOLS = [retrieve_documents, submit_ticket]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
