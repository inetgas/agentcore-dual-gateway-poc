# Design — Replace the orchestrator's hardcoded routing with a LangGraph ReAct agent

**Date:** 2026-06-09
**Status:** Proposed (awaiting review)
**Scope:** `agentcore-dual-gateway-poc/` orchestrator only — backend rewrite, UI and auth/observability proof preserved.

## 1. Motivation

The PoC's stack mandate is *"plain LangGraph on AgentCore Runtime,"* but the orchestrator
(`services/runtime/app.py`) is currently **imperative `if/elif`**: a keyword `classify()`
router (`router.py`) picks one of two fixed nodes (`research_node`, `submit_ticket_node`).
There is no graph.

The reference pattern is the DeepLearning.AI *"AI Agents in LangGraph"* course. L3
(`Lesson_3_Student.ipynb`) covers *agentic search tools*; the agent **graph pattern** it
plugs into is the Lesson-2 **ReAct loop**: a single LLM node bound to tools decides which
tool(s) to call and loops through an action node until it stops calling tools.

This change makes the orchestrator a genuine LangGraph ReAct agent — the LLM drives tool
selection (and can **chain** tools in one turn) instead of a keyword router picking one of two
branches. It closes the "plain LangGraph" gap and demonstrates multi-step tool use.

### Non-negotiable constraints (preserved by this change)
- **Credential-free by default.** The local demo and the offline test suite must keep working
  with no AWS/Okta/Bedrock credentials.
- **The entire auth + observability proof survives unchanged:** inbound JWT authorizer,
  `sub` → AgentCore Memory `actor_id`, auto-created workload identity, per-tool scoped JWT
  minted via `@requires_access_token` and routed through Kong, and the `gen_ai.*` / `mcp.*`
  OTel spans joinable by `conversation_id`.
- **AgentCore Memory stays the source of truth** for conversation history (no LangGraph
  checkpointer — YAGNI).
- **UI untouched.** The frontend reads only `answer`, `route_taken`, `citations`; the response
  stays backward-compatible.

## 2. Decisions (from brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| 1 | LLM tool-calling engine, credential-free | **Deterministic mock, pluggable to Bedrock.** Mock emits real `tool_calls`, routes its reasoning hop through Kong→mock-bedrock; `LANGGRAPH_MODEL=bedrock` swaps in real Claude. |
| 2 | Agent ambition | **Multi-step:** one turn can retrieve, reason, then submit a ticket (or retrieve repeatedly), looping `action → llm` until done. |
| 3 | UI / response contract | **Backend-only:** prove the loop via Jaeger/Langfuse + tests; keep the response contract backward-compatible so the UI is untouched. |

## 3. Graph architecture

A compiled LangGraph `StateGraph`, faithful to the Lesson-2 pattern, in a new
`services/runtime/agent.py`:

```python
AgentState = TypedDict {
    messages:  Annotated[list[AnyMessage], operator.add],
    citations: Annotated[list, operator.add],
}

graph = StateGraph(AgentState)
  node "llm"     -> call_model    # model.bind_tools([retrieve_documents, submit_ticket]).invoke(messages)
  node "action"  -> take_action   # execute each tool_call -> ToolMessage (+ collect citations)
  conditional_edges("llm", exists_action, {True: "action", False: END})
  edge("action", "llm")           # loop until the model stops calling tools
  set_entry_point("llm")
agent = graph.compile()
```

`app.py` keeps everything *outside* the loop (all proven, unchanged):

1. **inbound authorizer** (`require_user` FastAPI dependency) runs before the entrypoint.
2. `sub` → `actor_id`; baggage (`conversation_id`/`user_id`/`session.id`) set before invoke.
3. **entry_node**: load prior turns from AgentCore Memory → seed `AgentState.messages`.
4. **invoke the compiled graph** with seeded state + the new user message.
5. **response_node**: persist the new turn to AgentCore Memory; build the response.

The system prompt names the two tools and how to chain them; the LLM drives routing instead
of `classify()`.

## 4. The model (deterministic, pluggable) and tools

### Tools — `services/runtime/tools.py`
Thin `@tool` wrappers over the existing Kong-routed, `@requires_access_token`-decorated
functions in `gateway.py`. The decorator, scoped-JWT mint, Kong path, and spans are untouched
underneath:

```python
@tool
def retrieve_documents(query: str) -> str:
    """Search internal IT/policy docs. Use for any informational question."""
    chunks = gateway.retrieve_via_kong(query)        # via=kong, scope tool.retrieve
    # record citations on the span / state; return text for the model to read
    return format_chunks(chunks)

@tool
def submit_ticket(summary: str, requested_resource: str, justification: str) -> str:
    """File an access/IT ticket. Use only after gathering the needed details."""
    return asyncio.run(gateway.submit_ticket_via_kong({...}))   # mcp.*, scope tool.submit_ticket
```

### Model — `services/runtime/model.py`
A LangChain `BaseChatModel` whose `_generate` returns an `AIMessage` carrying `tool_calls`
in the same OpenAI/Anthropic shape real Bedrock emits — the graph can't tell mock from real.

- **`LANGGRAPH_MODEL=mock` (default):** deterministic. Its reasoning HTTP call still goes
  **through Kong → mock-bedrock** (so the reasoning hop stays proven). It decides tool calls
  from the conversation using the **relocated `router.py` keyword logic**:
  - informational question → one `retrieve_documents` call, then a tool-free answer;
  - grounded access request → **sequence**: `retrieve_documents` first; on the next `llm`
    turn (now seeing the `ToolMessage`) → `submit_ticket`; then a final tool-free answer →
    this is the multi-step loop, deterministically;
  - vague message → no tool calls → clarify (loop ends immediately at `END`).
- **`LANGGRAPH_MODEL=bedrock`:** swaps in `ChatBedrockConverse` (real `tool_calls`). Same
  graph, no other change. Exercised only with AWS creds.

New deps: `langgraph`, `langchain-core` (+ `langchain-aws`, bedrock-only).

## 5. Memory bridge, response contract, observability

### Memory bridge
AgentCore Memory remains the source of truth. `entry_node` seeds the graph; `response_node`
persists only the new turn (tool/ToolMessages stay inside the loop, not written to long-term
memory — matches today, keeps per-user isolation):

```python
events = memory_client.list_events(actor_id, conv)
seed = [HumanMessage(e.text) if e.role == "USER" else AIMessage(e.text) for e in events]
state = {"messages": [SystemMessage(SYSTEM)] + seed + [HumanMessage(user_msg)], "citations": []}
final = agent.invoke(state)
# ... persist (user_msg, USER) and (final_answer, ASSISTANT)
```

### Backward-compatible response (derived from what the loop did)
- `answer` = last `AIMessage` with no tool_calls.
- `citations` = aggregated from every `retrieve_documents` call (via the `citations` reducer).
- `route_taken` = `"submit_ticket"` if a ticket was filed, else `"research"` if any retrieval
  happened, else `"clarify"`. The existing UI badge keeps working.
- `trace_summary` = the real step sequence (UI ignores it today; present for traces/tests).

### Observability — same attributes, remapped to the loop
```
orchestrator.invoke
 ├─ inbound_authorizer        auth.sub, auth.result            (unchanged, pre-graph)
 ├─ entry_node                memory.actor_id, prior_turns
 ├─ llm   (×N iterations)     gen_ai.* on each model call
 ├─ action (×N)               retrieve → via=kong, auth.scope=tool.retrieve
 │                            ticket  → mcp.method.name/tool.name/transport/server.route, ticket_id
 └─ response_node
```
Baggage flows to every span. A multi-step turn shows **multiple `llm` + `action` spans** —
the visible proof of the loop in Jaeger/Langfuse.

## 6. Testing & verification

The existing **10/10 suite stays green untouched** (auth chain isn't changed) and runs first as
a regression gate. New `tests/test_agent.py` (offline, deterministic):

1. `test_graph_is_real_langgraph` — a compiled `StateGraph` with `llm`/`action` nodes and the
   `exists_action` conditional edge (proves it's no longer if/elif).
2. `test_single_retrieve_turn` — informational question → exactly one `retrieve_documents`
   call → answer with citations; `route_taken == "research"`.
3. `test_multi_step_loop` — **headline:** grounded access request → loop calls
   `retrieve_documents` **then** `submit_ticket` in one turn → `route_taken == "submit_ticket"`,
   citations present, ticket id returned; asserts on the **sequence**.
4. `test_no_tool_turn` — vague message → zero tool calls → clarify; loop terminates at `END`.
5. `test_tool_calls_still_traverse_kong_with_scope` — each tool hop minted the correctly-scoped
   JWT (`tool.retrieve` / `tool.submit_ticket`) and went through Kong (reuses the identity
   `retrievals` proof).

**Trace-level proof (scripted):** fire the multi-step turn through the live runtime, query
Jaeger/Langfuse, assert the trace has **≥2 `llm` spans and ≥2 `action` spans** with the expected
`gen_ai.*` / `mcp.*` attributes; capture into a results file (M0-style pattern).

All wired into the `tests` compose profile: `docker compose --profile test run --rm tests`.

## 7. File-level change plan

**New**
- `services/runtime/agent.py` — compiled `StateGraph` (AgentState, `call_model`, `take_action`,
  `exists_action`); `build_agent()` / `run_turn(state)`.
- `services/runtime/tools.py` — `@tool`-wrapped tools over `gateway.py`.
- `services/runtime/model.py` — deterministic mock `BaseChatModel` + `bedrock` branch.
- `tests/test_agent.py` — the 5 new tests.

**Modified**
- `services/runtime/app.py` — `/invoke` keeps inbound auth + baggage + spans; `entry_node`
  seeds messages; invokes the compiled graph; `response_node` persists + builds the derived
  backward-compatible response. The `if/elif` block is removed.
- `services/runtime/requirements.txt` — add `langgraph`, `langchain-core` (+ `langchain-aws`).
- `README.md`, `docs/RUNBOOK.md` — orchestrator description → "LangGraph ReAct agent"; add the
  multi-step trace as a verification artifact.

**Deleted**
- `services/runtime/router.py` — logic relocated into `model.py`.

## 8. Rollout (TDD via executing-plans, batches of 3, verify between)

1. Add deps + `tools.py` + `model.py` (mock) → unit-test the model emits expected `tool_calls`.
2. Add `agent.py` graph + rewire `app.py` → run new `test_agent.py` **and** regression-run the
   existing 10/10.
3. `docker compose up --build`; fire single + multi-step turns; capture Jaeger/Langfuse loop
   traces into a results file; update docs; commit.

Rollback is clean — all work is on a git branch over the current `main` commit.

## 9. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| `bind_tools` contract differs between the mock and real Bedrock | Mock emits the exact LangChain `tool_calls` shape; `bedrock` mode is integration-tested separately when creds exist. |
| Infinite loop (model keeps calling tools) | Deterministic mock terminates by construction; add a max-iterations guard in `take_action` as a backstop for bedrock mode. |
| `asyncio.run` inside a sync tool within the graph | Keep the existing pattern (already used in `gateway.submit_ticket_via_kong`); the graph node is sync. |
| Response contract drift breaks UI | Derived `route_taken`/`citations` keep the exact fields the frontend reads; covered by a contract test. |
