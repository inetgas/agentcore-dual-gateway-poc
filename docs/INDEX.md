# PoC — Document Index

Everything needed to understand, run, verify, and productionize this PoC — all inside
the `agentcore-dual-gateway-poc/` folder. Start at the top and go down.

## 1. Run & understand the PoC
- The **companion blog post** explains the *why* (dual-gateway pattern, per-tool identity
  scoping, `conversation_id` join key, PII redaction at the Collector) and the production
  system this POC is a reference implementation of.
- [`../README.md`](../README.md) — the blog-companion README: what it proves, the four
  patterns mapped to real files, quick-start, and the local→prod swap map.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — **start here for the visual:** the local
  deployment diagram (containers, ports, real-vs-mock), the request walkthrough, and the
  local→real-infra mapping.
- **Run (from `agentcore-dual-gateway-poc/`):** `docker compose up --build` → chat at http://localhost:5173
- **Proof suite (15/15):** `docker compose --profile test run --rm tests`
- **Observability:** Jaeger http://localhost:16686 (always bundled). For the Langfuse
  view, add the override: `docker compose -f docker-compose.yml -f docker-compose.langfuse.yml up --build`
  → Langfuse http://localhost:3000 (demo@example.com / demodemo123).

## 2. Path to real infrastructure
- [`RUNBOOK.md`](./RUNBOOK.md) — run locally → real Okta / AWS AgentCore / enterprise Kong /
  Bedrock / Datadog+Langfuse. The local-mock→prod swap map, the phased path, the findings &
  gotchas (incl. the Kong-enterprise-license gate), cost/region notes.

## 3. What's proven
The proof suite (`tests/`, **15/15**) covers the inbound chain, workload identity, and the
LangGraph ReAct agent:
- OIDC Authorization Code + PKCE → user JWT (and PKCE-failure rejected)
- inbound JWT authorizer: valid allowed; no-token / bad-signature / wrong-audience / expired → 401
- `sub` → AgentCore Memory `actor_id` (Alice ≠ Bob, same conversation id)
- workload identity auto-created with the runtime + used for token-vault retrieval
- the orchestrator is a real LangGraph ReAct loop that **chains tools** in one turn
  (retrieve → submit_ticket), with per-tool scoped JWTs through Kong

Run it: `docker compose --profile test run --rm tests`.

## 3a. The ReAct agent change
- [`plans/2026-06-09-langgraph-react-agent-design.md`](./plans/2026-06-09-langgraph-react-agent-design.md) — the design (decisions, graph, memory bridge, tests, rollout).
- [`RESULTS-react-agent.md`](./RESULTS-react-agent.md) — the verification evidence (15/15, the multi-step loop in Jaeger + Langfuse).

## 4. Visuals
- [`images/local-architecture.png`](./images/local-architecture.png) — local deployment diagram (rendered; source in [`diagrams/`](./diagrams/README.md))
- [`images/aws-target-architecture.png`](./images/aws-target-architecture.png) — target AWS multi-account diagram (illustrative; rendered)
- [`images/chat-mockup.png`](./images/chat-mockup.png) — the target chat UX
- [`images/mvp-architecture-multi-account.png`](./images/mvp-architecture-multi-account.png) — target AWS architecture (authoritative, from the brief)
- [`screenshots/poc-1-login.png`](./screenshots/poc-1-login.png) — frontend landing (Sign in with Okta)
- [`screenshots/poc-2-okta-authorize.png`](./screenshots/poc-2-okta-authorize.png) — Okta SSO (Authorization Code + PKCE)
- [`screenshots/poc-3-chat.png`](./screenshots/poc-3-chat.png) — chat as `alice@example.com` (research + ticket)

## 5. Observability (end-to-end trace screenshots)
- [`OBSERVABILITY.md`](./OBSERVABILITY.md) — one multi-step turn in **Jaeger** + **Langfuse**:
  the full ReAct span tree, `gen_ai.*` (model via Kong), and `via=agentcore-gateway` + `mcp.*`
  + per-tool scopes (tools via AgentCore Gateway), all joinable by `conversation_id`.

## Conventions
- `conversation_id == AgentCore Memory session_id` — a **UUID** (frontend `crypto.randomUUID()`;
  the runtime generates one if the client omits it).
- `actor_id == the user JWT sub` (per-user memory isolation).
- Local mocks stand in only for cloud-only services (Okta, AgentCore Identity/Memory, Bedrock);
  Kong, pgvector, the retrieve tool, and the MCP server are real.
