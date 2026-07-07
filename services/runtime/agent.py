"""LangGraph ReAct agent (the Lesson-2 pattern).

An `llm` node bound to tools, an `action` node that executes the model's tool_calls,
and an `exists_action` conditional edge that loops `action -> llm` until the model
stops calling tools. The LLM drives tool selection (and can chain tools in one turn),
replacing the old keyword router + two fixed branches.

Spans: each `llm` iteration carries gen_ai.*; each `action` carries the per-tool
attributes (retrieve -> via=kong, auth.scope=tool.retrieve; ticket -> mcp.* +
auth.scope=tool.submit_ticket, ticket_id). Baggage (conversation_id/user_id/session.id)
is attached by the caller, so every span here inherits it.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from opentelemetry import trace

from model import get_model
from tools import TOOLS, TOOLS_BY_NAME

_tracer = trace.get_tracer("agentcore-runtime")

SYSTEM = (
    "You are the Example Corp IT / Engineering assistant. You have two tools: "
    "retrieve_documents (search internal policy/runbook docs) and submit_ticket "
    "(file an access/IT ticket). For informational questions, retrieve and answer "
    "with citations. For access requests, first retrieve the relevant policy to "
    "ground the request, then submit_ticket. If the request is unclear, ask for "
    "clarification. Do not invent facts."
)

# Every tool now goes through AgentCore Gateway as an MCP tool.
_SCOPE_FOR = {"retrieve_documents": "tool.retrieve", "submit_ticket": "tool.submit_ticket"}
_MAX_ITERS = 6  # backstop against a runaway loop (matters mainly for bedrock mode)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    citations: Annotated[list, operator.add]


class ReActAgent:
    def __init__(self, model: Any = None):
        self.model = (model or get_model()).bind_tools(TOOLS)
        g = StateGraph(AgentState)
        g.add_node("llm", self.call_model)
        g.add_node("action", self.take_action)
        g.add_conditional_edges("llm", self.exists_action, {True: "action", False: END})
        g.add_edge("action", "llm")
        g.set_entry_point("llm")
        self.graph = g.compile()

    def exists_action(self, state: AgentState) -> bool:
        msgs = state["messages"]
        # Count only THIS turn's llm iterations (AIMessages after the last user message)
        # — not the prior turns seeded from durable memory — so the backstop doesn't
        # trip early in a long conversation.
        last_human = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
        turn_iters = sum(1 for m in msgs[last_human + 1:] if isinstance(m, AIMessage))
        if turn_iters > _MAX_ITERS:
            return False
        return bool(getattr(msgs[-1], "tool_calls", None))

    def call_model(self, state: AgentState) -> dict:
        with _tracer.start_as_current_span("llm") as sp:
            ai = self.model.invoke(state["messages"])
            md = getattr(ai, "response_metadata", {}) or {}
            sp.set_attribute("gen_ai.system", md.get("gen_ai.system", "kong->mock-bedrock"))
            sp.set_attribute("gen_ai.operation.name", "chat")
            sp.set_attribute("gen_ai.request.model", md.get("gen_ai.request.model", "mock-bedrock-claude"))
            out = (md.get("usage") or {}).get("completion_tokens") or max(1, len(str(ai.content)) // 4)
            sp.set_attribute("gen_ai.usage.output_tokens", out)
            sp.set_attribute("tool_calls", len(getattr(ai, "tool_calls", []) or []))
            # Per-llm content (raw; the Collector redacts PII on the Langfuse pipeline).
            last_user = next((str(m.content) for m in reversed(state["messages"])
                              if isinstance(m, HumanMessage)), "")
            tcs = getattr(ai, "tool_calls", []) or []
            llm_out = str(ai.content) if ai.content else \
                "→ tool_calls: " + ", ".join(t["name"] for t in tcs)
            sp.set_attribute("langfuse.observation.input", last_user)
            sp.set_attribute("langfuse.observation.output", llm_out)
        return {"messages": [ai]}

    def take_action(self, state: AgentState) -> dict:
        calls = state["messages"][-1].tool_calls
        results, citations = [], []
        for tc in calls:
            with _tracer.start_as_current_span("action") as sp:
                # All tools are MCP tools fronted by AgentCore Gateway.
                sp.set_attribute("tool.name", tc["name"])
                sp.set_attribute("via", "agentcore-gateway")
                sp.set_attribute("mcp.method.name", "tools/call")
                sp.set_attribute("mcp.tool.name", tc["name"])
                sp.set_attribute("mcp.transport", "streamable-http")
                sp.set_attribute("mcp.server", "agentcore-gateway")
                if tc["name"] in _SCOPE_FOR:
                    sp.set_attribute("auth.scope", _SCOPE_FOR[tc["name"]])
                if tc["name"] not in TOOLS_BY_NAME:
                    msg = ToolMessage(content="bad tool name, retry", tool_call_id=tc["id"], name=tc["name"])
                else:
                    msg = TOOLS_BY_NAME[tc["name"]].invoke(tc)  # ToolMessage (with .artifact)
                    art = getattr(msg, "artifact", None) or {}
                    if art.get("citations"):
                        citations.extend(art["citations"])
                    if art.get("ticket_id"):
                        sp.set_attribute("ticket_id", art["ticket_id"])
                results.append(msg)
        return {"messages": results, "citations": citations}

    def run(self, seed_messages: list) -> AgentState:
        state: AgentState = {"messages": [SystemMessage(content=SYSTEM)] + seed_messages, "citations": []}
        return self.graph.invoke(state)

    def describe(self) -> dict:
        """Graph introspection (for tests / debugging)."""
        return {"nodes": ["llm", "action"], "entry": "llm",
                "conditional_edge": {"from": "llm", "fn": "exists_action",
                                     "true": "action", "false": "END"},
                "edges": [["action", "llm"]], "tools": list(TOOLS_BY_NAME)}
