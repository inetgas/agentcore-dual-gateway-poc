"""Tool-calling model for the ReAct agent.

LANGGRAPH_MODEL=mock (default): a deterministic BaseChatModel that
  (a) makes its reasoning hop through Kong -> mock-bedrock (proving the model call
      genuinely traverses the gateway, with a scoped workload-identity JWT), and
  (b) decides tool calls deterministically from the conversation, using relocated
      keyword logic — so behaviour is reproducible offline and in tests.

LANGGRAPH_MODEL=bedrock: swaps in ChatBedrockConverse (real tool_calls), same graph.

The mock emits the exact LangChain tool_calls shape that real Bedrock emits, so the
graph cannot tell them apart.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional, Sequence

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from identity import requires_access_token

KONG_URL = os.environ.get("KONG_URL", "http://kong:8000").rstrip("/")
MODEL_NAME = "mock-bedrock-claude"
_SENT = re.compile(r"(?<=[.!?])\s+")

# Relocated from router.py — the keyword decision logic, now the mock model's brain.
_TICKET_CUES = (
    "open the ticket", "open a ticket", "file a ticket", "raise a ticket",
    "create a ticket", "submit the ticket", "submit a ticket", "submit it",
    "go ahead and open", "please open", "open it for me",
)
_ACCESS_VERBS = ("request", "need", "grant", "get me", "give me", "provision")
_RESEARCH_CUES = ("how", "what", "where", "why", "which", "who", "can i", "do i", "?")


def classify(message: str) -> str:
    """research | submit_ticket | clarify.

    Order matters: an explicit ticket cue wins; otherwise a *question* (research cue)
    is informational even if it mentions "access"; only an imperative access request
    with no question becomes a ticket.
    """
    t = message.lower().strip()
    if any(cue in t for cue in _TICKET_CUES):
        return "submit_ticket"
    if any(cue in t for cue in _RESEARCH_CUES):
        return "research"
    if "access" in t and any(v in t for v in _ACCESS_VERBS):
        return "submit_ticket"
    return "clarify"


def _synth(text: str) -> str:
    if not text:
        return "I couldn't find anything relevant in the documentation."
    body = " ".join(text.split())
    return " ".join(_SENT.split(body)[:3]).strip()


def _ticket_resource(message: str) -> str:
    return "DL-Reader access on prod" if "prod" in message.lower() else "the requested access"


def _scan(messages: Sequence[BaseMessage]) -> tuple[str, list[str]]:
    """Latest user request + which tools have already run for this turn."""
    idx = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    user_text = messages[idx].content if idx >= 0 else ""
    tools_done = [m.name for m in messages[idx + 1:] if isinstance(m, ToolMessage)]
    return user_text, tools_done


def _last_tool_text(messages: Sequence[BaseMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            return str(m.content)
    return ""


@requires_access_token(provider_name="okta-orchestrator", scopes=["model.invoke"], auth_flow="M2M")
def _reason(*, payload: dict, access_token: str) -> dict:
    """The reasoning hop: orchestrator -> Kong (ai-proxy) -> mock Bedrock."""
    r = httpx.post(f"{KONG_URL}/v1/chat/completions",
                   headers={"Authorization": f"Bearer {access_token}"},
                   json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


class MockToolCallingModel(BaseChatModel):
    """Deterministic tool-calling model. Reasoning traverses Kong; decisions are rules."""

    @property
    def _llm_type(self) -> str:
        return "mock-tool-calling"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "MockToolCallingModel":
        # The deterministic mock ignores the schemas but keeps the bind_tools contract,
        # so the graph wiring is identical to a real tool-calling model.
        return self

    def _reason_via_kong(self, messages: Sequence[BaseMessage]) -> dict:
        """Best-effort: proves the model call traverses Kong. Result is not used for
        the deterministic decision, so the agent still works if the gateway is down."""
        try:
            payload = {"model": MODEL_NAME, "messages": [
                {"role": "user" if isinstance(m, HumanMessage) else "assistant",
                 "content": str(m.content)}
                for m in messages if str(m.content)][-6:]}
            return _reason(payload=payload).get("usage", {})
        except Exception:
            return {}

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        usage = self._reason_via_kong(messages)
        user_text, tools_done = _scan(messages)
        intent = classify(user_text)
        n = sum(len(m.tool_calls) for m in messages if isinstance(m, AIMessage))

        def call(name: str, args: dict) -> AIMessage:
            return AIMessage(content="", tool_calls=[
                {"name": name, "args": args, "id": f"call_{n}", "type": "tool_call"}])

        if intent == "research":
            if "retrieve_documents" not in tools_done:
                ai = call("retrieve_documents", {"query": user_text})
            else:
                ai = AIMessage(content=_synth(_last_tool_text(messages)))
        elif intent == "submit_ticket":
            if "retrieve_documents" not in tools_done:
                # Multi-step: gather grounding FIRST, then file the ticket.
                ai = call("retrieve_documents", {"query": user_text})
            elif "submit_ticket" not in tools_done:
                ai = call("submit_ticket", {
                    "summary": user_text[:80],
                    "requested_resource": _ticket_resource(user_text),
                    "justification": user_text})
            else:
                ai = AIMessage(content=f"{_last_tool_text(messages)} Your manager is CC'd.")
        else:
            ai = AIMessage(content="Could you clarify what you'd like me to do?")

        ai.response_metadata["gen_ai.system"] = "kong->mock-bedrock"
        ai.response_metadata["gen_ai.request.model"] = MODEL_NAME
        ai.response_metadata["usage"] = usage
        return ChatResult(generations=[ChatGeneration(message=ai)])


def get_model() -> BaseChatModel:
    if os.environ.get("LANGGRAPH_MODEL", "mock").lower() == "bedrock":
        from langchain_aws import ChatBedrockConverse  # bedrock-only dependency
        return ChatBedrockConverse(
            model=os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return MockToolCallingModel()
