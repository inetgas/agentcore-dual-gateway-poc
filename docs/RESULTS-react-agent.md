# Verification — LangGraph ReAct agent (multi-step tool use)

**Date:** 2026-06-09
**Change:** the orchestrator is now a real LangGraph ReAct agent (`llm ⇄ action` loop)
instead of an imperative keyword router + two fixed branches. Design:
[`plans/2026-06-09-langgraph-react-agent-design.md`](./plans/2026-06-09-langgraph-react-agent-design.md).

How to reproduce everything below:

```bash
cd agentcore-dual-gateway-poc
docker compose -f docker-compose.yml -f docker-compose.langfuse.yml up -d --build
docker compose --profile test run --rm tests          # 15/15
```

## 1. The orchestrator is a real LangGraph StateGraph

`GET /graph` (introspection):

```json
{ "nodes": ["llm", "action"], "entry": "llm",
  "conditional_edge": {"from": "llm", "fn": "exists_action", "true": "action", "false": "END"},
  "edges": [["action", "llm"]],
  "tools": ["retrieve_documents", "submit_ticket"] }
```

## 2. Proof suite — 15/15 (10 inbound-auth regression + 5 new ReAct)

```
test_agent.py::test_graph_is_real_langgraph PASSED
test_agent.py::test_single_retrieve_turn PASSED
test_agent.py::test_multi_step_loop PASSED
test_agent.py::test_no_tool_turn PASSED
test_agent.py::test_tool_calls_traverse_gateway_with_scope PASSED
test_poc.py::test_pkce_login_yields_user_jwt PASSED
test_poc.py::test_pkce_wrong_verifier_rejected PASSED
test_poc.py::test_valid_user_jwt_reaches_entrypoint PASSED
test_poc.py::test_no_token_401 PASSED
test_poc.py::test_bad_signature_401 PASSED
test_poc.py::test_wrong_audience_401 PASSED
test_poc.py::test_expired_401 PASSED
test_poc.py::test_sub_becomes_actor_id_and_isolates_memory PASSED
test_poc.py::test_workload_identity_auto_created PASSED
test_poc.py::test_workload_identity_used_for_token_vault PASSED
============================== 15 passed ==============================
```

## 3. Multi-step loop — live turn

Input (one user turn): `"Please open a ticket for DL-Reader access on prod"`

Response:
- `route_taken`: `submit_ticket`
- `answer`: `Ticket DL-2026-0429 created for DL-Reader access on prod. Your manager is CC'd.`
- `citations`: 5 (grounding gathered before filing)
- `trace_summary` (the loop chained two tools in one turn):

```
inbound_authorizer  sub=alice@example.com result=allow
entry_node          prior_turns=0
llm                 tool_calls=[retrieve_documents]
action              tool=retrieve_documents
llm                 tool_calls=[submit_ticket]
action              tool=submit_ticket
llm                 answer=True
response_node
```

## 4. Trace evidence — Jaeger (`mvp-orchestrator`)

One trace, span counts: `orchestrator.invoke:1, inbound_authorizer:1, entry_node:1,
llm:3, action:2, response_node:1`. Per-span attributes:

```
llm     gen_ai.system=kong->mock-bedrock  gen_ai.request.model=mock-bedrock-claude
        gen_ai.usage.output_tokens=12  tool_calls=1     ← model via Kong
action  tool.name=retrieve_documents  via=agentcore-gateway  mcp.tool.name=retrieve_documents  auth.scope=tool.retrieve
llm     (gen_ai.* …)  tool_calls=1
action  tool.name=submit_ticket  via=agentcore-gateway  auth.scope=tool.submit_ticket
        mcp.method.name=tools/call  mcp.tool.name=submit_ticket  ticket_id=DL-2026-0429
llm     (gen_ai.* …)  tool_calls=0     ← model stops calling tools → END
```

Every span also carries the `conversation_id` / `user_id` / `session.id` baggage.

## 5. Trace evidence — Langfuse (via the OTel Collector)

Same trace ingested into the bundled Langfuse:
`spans={orchestrator.invoke:1, inbound_authorizer:1, entry_node:1, llm:3, action:2,
response_node:1}`, `llm.gen_ai=True`, `action.mcp=True` — confirming the loop and the
`gen_ai.*` / `mcp.*` attributes under each observation's `metadata.attributes`.

## 6. What this proves

- The orchestrator is genuine **plain LangGraph** (the stack mandate), not `if/elif`.
- The LLM **drives tool selection and chains tools** in a single turn (retrieve → reason →
  submit_ticket → answer) — the payoff over fixed routing.
- The reasoning hop genuinely **traverses Kong** (`gen_ai.system=kong->mock-bedrock`).
- Tools go through **AgentCore Gateway** (MCP); the entire prior proof is intact: inbound
  authorizer, `sub`→`actor_id`, workload identity, **per-tool scoped JWTs** enforced by the
  gateway (`tool.retrieve` / `tool.submit_ticket`), and `≥2 llm` + `≥2 action` spans visible
  in both Jaeger and Langfuse.
- Swap to real Bedrock with `LANGGRAPH_MODEL=bedrock` — same graph, real `tool_calls`.
