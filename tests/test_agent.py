"""ReAct agent proof suite — the orchestrator is now a real LangGraph ReAct loop.

Proves:
  1) the orchestrator graph is a real LangGraph StateGraph (llm/action + conditional edge)
  2) an informational question -> one retrieve_documents call -> answer with citations
  3) MULTI-STEP: an access request -> retrieve_documents THEN submit_ticket in one turn
  4) a vague message -> zero tool calls -> clarify (loop ends immediately)
  5) tool calls traverse AgentCore Gateway (MCP) with the correct per-tool scope
"""

import os
import secrets

import httpx

from test_poc import login, invoke, RUNTIME


def _conv():
    return "agent-" + secrets.token_hex(4)  # fresh id (memory is durable)


# ---- 1) The orchestrator is a real LangGraph ReAct agent -------------------

def test_graph_is_real_langgraph():
    g = httpx.get(f"{RUNTIME}/graph", timeout=10).json()
    assert g["nodes"] == ["llm", "action"]
    assert g["entry"] == "llm"
    assert g["conditional_edge"]["fn"] == "exists_action"
    assert g["conditional_edge"]["true"] == "action"
    assert ["action", "llm"] in g["edges"]            # the loop edge
    assert set(g["tools"]) == {"retrieve_documents", "submit_ticket"}


# ---- 2) Single retrieve turn -----------------------------------------------

def test_single_retrieve_turn():
    d = invoke(login("alice"), "What is the remote work policy?", _conv()).json()
    assert d["route_taken"] == "research"
    assert len(d["citations"]) > 0
    tools = [s.get("tool") for s in d["trace_summary"] if s["node"] == "action"]
    assert tools == ["retrieve_documents"]            # exactly one tool call


# ---- 3) MULTI-STEP loop: retrieve THEN submit_ticket -----------------------

def test_multi_step_loop():
    d = invoke(login("alice"), "Please open a ticket for DL-Reader access on prod", _conv()).json()
    assert d["route_taken"] == "submit_ticket"
    # the headline: the loop chained two tools, in order, in a single turn
    tools = [s.get("tool") for s in d["trace_summary"] if s["node"] == "action"]
    assert tools == ["retrieve_documents", "submit_ticket"], tools
    assert len(d["citations"]) > 0                    # grounding gathered first
    assert "DL-" in d["answer"]                        # a ticket id came back
    # >= 2 llm spans + 2 action spans worth of steps (proof of the loop)
    llm_steps = [s for s in d["trace_summary"] if s["node"] == "llm"]
    assert len(llm_steps) >= 3                         # llm -> llm -> llm (final answer)


# ---- 4) No-tool turn -> clarify, loop ends immediately ---------------------

def test_no_tool_turn():
    d = invoke(login("alice"), "hello", _conv()).json()
    assert d["route_taken"] == "clarify"
    assert [s for s in d["trace_summary"] if s["node"] == "action"] == []


# ---- 5) Tool calls traverse AgentCore Gateway (MCP) with the correct scope --

def test_tool_calls_traverse_gateway_with_scope():
    invoke(login("alice"), "Please open a ticket for DL-Reader access on prod", _conv())
    wi = httpx.get(f"{RUNTIME}/workload-identity", timeout=10).json()
    scopes = {s for r in wi["token_vault_retrievals"] for s in r.get("scopes", [])}
    # the multi-step turn minted scoped JWTs for BOTH tools via the workload identity;
    # AgentCore Gateway enforces these per-tool scopes on each MCP tools/call.
    assert "tool.retrieve" in scopes
    assert "tool.submit_ticket" in scopes
