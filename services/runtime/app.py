"""AgentCore Runtime (local stand-in) — the orchestrator entrypoint.

Request flow (proves the inbound chain):
  UI ──Bearer user JWT──▶ INBOUND AUTHORIZER (validate vs Okta JWKS/aud/scope)
                          ──▶ entrypoint  (sub → actor_id for AgentCore Memory)
                              LangGraph ReAct agent (llm ⇄ action loop)
                              → AgentCore Gateway (MCP tools, scoped JWT) → memory
                              → Kong (model reasoning, scoped JWT)

The agent is a real LangGraph StateGraph (see agent.py): the model decides which
tool(s) to call and loops until done, instead of a keyword router picking one branch.
Tools are exposed by AgentCore Gateway (MCP); the model goes through Kong.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from opentelemetry import baggage, context as octx, trace
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import identity
import memory_client
import otel_setup
from agent import ReActAgent
from authorizer import require_user

RUNTIME_NAME = "mvp-orchestrator"
_tracer = None
_agent: ReActAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tracer, _agent
    otel_setup.setup()  # -> Jaeger (+ Langfuse if configured)
    _tracer = trace.get_tracer("agentcore-runtime")
    # AgentCore Identity auto-creates the runtime's workload identity on creation.
    wid = identity.ensure_workload_identity(RUNTIME_NAME)
    print(f"[runtime] workload identity auto-created: {wid}", flush=True)
    _agent = ReActAgent()  # compile the LangGraph ReAct graph once
    print(f"[runtime] LangGraph ReAct agent compiled: {_agent.describe()['nodes']}", flush=True)
    yield


app = FastAPI(title="mvp-agentcore-runtime", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class TurnReq(BaseModel):
    conversation_id: str
    message: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/graph")
def graph():
    """Proof the orchestrator is a real LangGraph ReAct agent (not if/elif routing)."""
    return _agent.describe()


@app.get("/workload-identity")
def workload_identity():
    """Proof: the runtime's auto-created machine identity + its token-vault retrievals."""
    return {**identity.get_workload_identity(),
            "token_vault_retrievals": identity.retrievals[-10:]}


def _derive_route(messages: list) -> str:
    tools_run = [m.name for m in messages if isinstance(m, ToolMessage)]
    if "submit_ticket" in tools_run:
        return "submit_ticket"
    if "retrieve_documents" in tools_run:
        return "research"
    return "clarify"


def _trace_summary(actor_id: str, prior: int, messages: list) -> list[dict]:
    steps = [{"node": "inbound_authorizer", "sub": actor_id, "result": "allow"},
             {"node": "entry_node", "prior_turns": prior}]
    for m in messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            steps.append({"node": "llm", "tool_calls": [tc["name"] for tc in m.tool_calls]})
        elif isinstance(m, AIMessage):
            steps.append({"node": "llm", "answer": True})
        elif isinstance(m, ToolMessage):
            steps.append({"node": "action", "tool": m.name})
    steps.append({"node": "response_node"})
    return steps


@app.post("/invoke")
def invoke(req: TurnReq, claims: dict = Depends(require_user)):
    # The inbound authorizer already validated the user JWT. Its sub is the actor.
    actor_id = claims["sub"]
    conv = req.conversation_id or str(uuid.uuid4())  # session_id == conversation_id (UUID)

    # Baggage -> every span (conversation_id flows through every layer; user_id = sub).
    ctx = octx.get_current()
    for k, v in {"conversation_id": conv, "user_id": actor_id, "session.id": conv}.items():
        ctx = baggage.set_baggage(k, v, context=ctx)
    tok = octx.attach(ctx)
    try:
        with _tracer.start_as_current_span("orchestrator.invoke") as root:
            # The user prompt is the trace input. It is emitted RAW here; the OTel
            # Collector redacts PII on the Langfuse pipeline (Jaeger keeps it raw).
            root.set_attribute("langfuse.trace.input", req.message)
            root.set_attribute("langfuse.observation.input", req.message)

            with _tracer.start_as_current_span("inbound_authorizer") as a:
                a.set_attribute("auth.sub", actor_id)
                a.set_attribute("auth.result", "allow")

            with _tracer.start_as_current_span("entry_node") as e:
                events = memory_client.list_events(actor_id, conv)
                seed = [HumanMessage(content=ev["text"]) if ev["role"] == "USER"
                        else AIMessage(content=ev["text"]) for ev in events]
                prior = sum(1 for ev in events if ev["role"] == "USER")
                e.set_attribute("memory.actor_id", actor_id)
                e.set_attribute("prior_turns", prior)

            # The LangGraph ReAct loop: llm ⇄ action until the model stops calling tools.
            final = _agent.run(seed + [HumanMessage(content=req.message)])
            messages = final["messages"]
            answer = next((str(m.content) for m in reversed(messages)
                           if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None)),
                          "Could you clarify what you'd like me to do?")
            citations = final.get("citations", [])
            route = _derive_route(messages)
            # The assistant reply is the trace output (raw; redacted on the Langfuse pipeline).
            root.set_attribute("langfuse.trace.output", answer)
            root.set_attribute("langfuse.observation.output", answer)

            with _tracer.start_as_current_span("response_node"):
                memory_client.create_event(actor_id, conv,
                                           [(req.message, "USER"), (answer, "ASSISTANT")])
    finally:
        octx.detach(tok)

    tp = trace.get_tracer_provider()
    if hasattr(tp, "force_flush"):
        tp.force_flush()  # land the trace in Jaeger/Langfuse immediately

    return {"answer": answer, "route_taken": route, "sub_agent": route,
            "citations": citations, "actor_id": actor_id, "user": claims.get("name", actor_id),
            "conversation_id": conv, "prior_turns": prior,
            "workload_identity": identity.get_workload_identity().get("workload_identity_id"),
            "trace_summary": _trace_summary(actor_id, prior, messages)}
