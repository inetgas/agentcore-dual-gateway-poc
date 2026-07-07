# Dual-Gateway AgentCore PoC — Runbook (run locally → take to real infrastructure)

A self-contained guide for someone handed the **`agentcore-dual-gateway-poc/`** folder: run it locally, then
take it to real infrastructure (enterprise Kong, AWS AgentCore, Okta, Bedrock,
Datadog/Langfuse). Read top-to-bottom once; then use the phase checklists.

---

## 1. What this is

A chat-based IT/engineering virtual agent that proves the end-to-end auth chain:

```
Browser ──(1) Okta OIDC Authorization Code + PKCE──▶ Okta ──▶ USER JWT (RS256, sub, aud)
   │
   └──(POST /invoke, Authorization: Bearer <user JWT>)──▶ AgentCore Runtime
                                       (2) INBOUND AUTHORIZER: validate vs JWKS / audience / scope (else 401)
                                       (3) claims.sub ──▶ AgentCore Memory actor_id (per-user memory)
                                       (4) WORKLOAD IDENTITY (auto-created with the runtime) ──▶ token vault
                                              @requires_access_token (M2M) ──▶ scoped JWT
                                       ▼
              model:  Kong (ai-proxy) ──model.invoke──▶ Bedrock (mock)
              tools:  AgentCore Gateway (MCP) ──tool.retrieve──▶ pgvector
                                              ──tool.submit_ticket──▶ ticketing
```

**Inbound user auth** (user → runtime): Okta OIDC PKCE → user JWT → the runtime's
**inbound JWT authorizer** validates it before the entrypoint → the JWT `sub` is the
AgentCore Memory `actor_id`.

**Outbound auth** (runtime → model/tools), two layers:
- **Layer A** — the model/tool callers are decorated `@requires_access_token(scopes=[...], auth_flow="M2M")`;
  the runtime's **workload identity** exchanges M2M creds for a **scoped JWT** (cached in
  the token vault) and injects it. The token never enters state/logs/spans.
- **Layer B** — the gateway validates the JWT and enforces scope: **Kong** on the model
  route; **AgentCore Gateway** on each MCP `tools/call` (fine-grained per-tool scope).

`conversation_id == AgentCore Memory session_id` and is a **UUID**.

---

## 2. What's in this folder

```
agentcore-dual-gateway-poc/
├── docker-compose.yml            # the whole PoC stack (one command)
├── frontend/                     # React SPA — Okta OIDC Authorization Code + PKCE login
├── services/
│   ├── mock-okta/                # OIDC provider: PKCE → RS256 user tokens + JWKS; client_credentials → HS256 M2M
│   ├── runtime/                  # AgentCore Runtime stand-in (ASGI app):
│   │                             #   authorizer.py (inbound JWT authorizer), app.py (entrypoint),
│   │                             #   agent.py (LangGraph ReAct graph), model.py (tool-calling model),
│   │                             #   tools.py (tool clients → gateway), identity.py (workload identity + vault),
│   │                             #   memory_client.py, gateway.py (Kong model + AgentCore Gateway MCP), otel_setup.py
│   ├── mock-agentcore-gateway/   # MCP TOOL gateway (stands in for AWS AgentCore Gateway):
│   │                             #   server.py (FastMCP + inbound JWT + per-tool scope), rag.py + seed.py (pgvector)
│   ├── mock-bedrock/             # OpenAI-compatible model upstream (behind Kong)
│   ├── mock-memory/              # AgentCore Memory contract (Postgres-backed)
│   ├── otel-collector/           # config.yaml — single always-on config (→ Jaeger raw + Langfuse PII-redacted)
│   └── kong/kong.yaml            # real Kong 3.14 config — MODEL gateway only (ai-proxy)
├── corpus/                       # sample RAG corpus
├── tests/                        # proof suite — 15/15 (inbound auth + workload identity + ReAct)
└── docs/                         # ← you are here: RUNBOOK.md, INDEX.md, images/, screenshots/
```

Local **mocks** stand in only for the cloud-only services (Okta, AgentCore Identity,
Bedrock, AgentCore Memory). **Kong, pgvector, the retrieve tool, and the MCP server are
real.**

---

## 3. Run it locally (no cloud credentials)

Prereq: Docker. Run from the **`agentcore-dual-gateway-poc/`** folder.

```bash
docker compose up --build
#   Chat     http://localhost:5173   (Sign in with Okta → Alice/Bob → chat)
#   Jaeger   http://localhost:16686  (traces; service "mvp-orchestrator")
#   mock-okta http://localhost:8081  ·  runtime http://localhost:8080

# Proof suite (10/10): inbound auth + sub→actor_id + workload identity
docker compose --profile test run --rm tests

# Stop
docker compose down -v
```

**With Langfuse** (gen_ai/LLM-observability view, in addition to Jaeger) — add the
override file; the OTel Collector then also forwards a **PII-redacted** copy of every
trace to a self-contained Langfuse (Jaeger keeps the raw copy):

```bash
docker compose -f docker-compose.yml -f docker-compose.langfuse.yml up --build
#   Langfuse http://localhost:3000   (login demo@example.com / demodemo123)
#     project "mvp-orchestrator" → Tracing
```
First boot pulls the Langfuse v3 stack (web, worker, its own postgres, clickhouse,
minio, redis) and runs migrations — allow ~60-90s before traces appear.

> **Browser PKCE needs a secure context** — use `http://localhost:5173` (not an IP or
> other host); `crypto.subtle`/`crypto.randomUUID` only work on `https` or `localhost`.

### Observability
The runtime emits OTLP (spans **plus the prompt/response content**) to a single
**OTel Collector** (`services/otel-collector/config.yaml`), the only OTel egress. It runs
**one always-on config with two pipelines**: raw → **Jaeger** (`http://localhost:16686`,
always bundled), and a **PII-redacted** copy (a `transform` processor: email/SSN/phone/card
→ `[EMAIL]`/`[SSN]`/…) → **Langfuse** (`http://localhost:3000`). The Collector clones the
data, so Jaeger sees raw and Langfuse sees redacted — the two-tier of **Decision 4 (redact
at the Collector)**. The Langfuse override only *starts* the Langfuse containers; when
they're down (the lightweight default), the Langfuse exporter fail-fast no-ops, so Jaeger
is unaffected. (One config in all modes — no compose command can misconfigure forwarding.)

The orchestrator is a LangGraph ReAct agent, so `llm` and `action` repeat in the loop:
`orchestrator.invoke → inbound_authorizer → entry_node → (llm → action)* → llm → response_node`.
Every span carries `conversation_id` (+ `user_id`/`session.id`); `orchestrator.invoke` has
`langfuse.trace.input`/`output` (the prompt/answer), `inbound_authorizer` has
`auth.sub`/`auth.result`, `entry_node` has `memory.actor_id`, each `llm` has `gen_ai.*`
(model via Kong) + its own input/output, each `action` has `via=agentcore-gateway` +
`mcp.*` + `auth.scope`. A multi-step turn shows ≥2 `llm` + ≥2 `action` spans. In Langfuse,
content shows in each observation's **Input/Output** (PII-redacted) and attributes under
`metadata.attributes`; filter by `conversation_id` to follow one chat across every layer.

### Inspecting a conversation (the user prompt + final response)

Traces now carry the prompt/response content (raw in Jaeger, redacted in Langfuse). The
**durable** conversation record still lives in **AgentCore Memory** as `(prompt, USER)` /
`(answer, ASSISTANT)` events, keyed by `actor_id` (= the user JWT `sub`) and `session_id`
(= the `conversation_id` UUID). Read it back:

```bash
docker compose exec -T runtime python -c "
import memory_client
for e in memory_client.list_events('alice@example.com', '<conversation_id>'):
    print(f\"[{e['role']}] {e['text']}\")
"
```

Example output:

```
[USER] What is the remote work policy?
[ASSISTANT] [1] (dl-reader-entitlement.md) # DL-Reader Entitlement The DL-Reader entitlement grants read-only access ...
[USER] Please open a ticket for DL-Reader access on prod
[ASSISTANT] Ticket DL-2026-0434 created for DL-Reader access on prod. Your manager is CC'd.
```

`actor_id` is the user's JWT `sub`, so this is also the per-user isolation boundary —
a different `sub` over the same `conversation_id` sees a separate history.

> **PII redaction — two independent layers.** *(1) Observability path (demonstrated here):*
> the OTel Collector redacts PII (email/SSN/phone/card) before forwarding to Langfuse, so
> Jaeger holds raw content and Langfuse holds redacted — verified by sending a prompt with
> an email + SSN and seeing `[EMAIL]`/`[SSN]` in Langfuse only. *(2) Model path (not in the
> PoC):* scrubbing prompts before the model is Kong's **`ai-sanitizer`**, which is **Kong
> Enterprise–only**; the OSS `ai-proxy` sends prompts to the model verbatim, so enable
> `ai-sanitizer` on Kong's model routes in prod. Two consumers (the observability backend,
> the model provider), two rule sets — Decision 4.

---

## 4. Local mock → production component (the swap map)

To go to real infra you replace each mock with its production component; the
**contracts stay the same** (endpoints swap by env).

| Concern | Local (this PoC) | Production |
|---|---|---|
| Identity provider | `services/mock-okta` (PKCE → RS256 user tokens + JWKS; client_credentials → HS256 M2M) | **Okta tenant** (RS256 + JWKS) |
| Inbound user auth | `services/runtime/authorizer.py` (validates user JWT vs JWKS/aud/scope before the entrypoint) | **AgentCore Runtime inbound JWT authorizer** (configured with the Okta discovery URL + audience; allowed-scopes empty) |
| Workload identity | `services/runtime/identity.py` (auto-created on startup; used for token-vault retrieval) | **AWS AgentCore Identity** auto-creates it when the Runtime is created |
| `@requires_access_token` | local shim decorator | `bedrock_agentcore.identity.auth.requires_access_token` |
| **Model gateway** | **real Kong 3.14 free mode** — `ai-proxy` (+ `jwt`) → mock-bedrock | **Kong + Konnect, Enterprise license** (`openid-connect`, `ai-proxy-advanced`) |
| **MCP tool gateway** | `services/mock-agentcore-gateway` — FastMCP + inbound JWT + per-tool scope; tools over MCP | **AWS AgentCore Gateway** (managed MCP gateway; inbound OAuth + fine-grained per-tool authz) |
| Model JWT validation | OSS `jwt` plugin (HS256 shared secret) | enterprise `openid-connect` (RS256/JWKS) |
| **PII redaction** | **none** (OSS Kong has no sanitizer — model prompts/responses pass through verbatim) | enterprise **`ai-sanitizer`** on Kong's model routes (service-backed LLM scrubbing) |
| Reasoning / embedding model | `services/mock-bedrock` (OpenAI shape) | **Bedrock** Claude / Titan via `ai-proxy-advanced` |
| Vector store | **real pgvector** container | **RDS Postgres + pgvector** |
| Memory | `services/mock-memory` (same `create_event`/`list_events` contract, Postgres-backed, durable) | **AWS AgentCore Memory** (point boto3 `bedrock-agentcore` at AWS) |
| Tools | exposed as **MCP tools by the gateway** (`retrieve_documents` over pgvector; `submit_ticket`) | targets behind **AWS AgentCore Gateway** (REST/Lambda → MCP), egress via AgentCore Identity |
| Observability egress | `services/otel-collector` (OTLP in → Jaeger raw; Langfuse redacted) | **OTel Collector** (redaction processor; tail sampling; fan-out) |
| Tracing backends | Jaeger (raw) + Langfuse (redacted) | Datadog + Langfuse + CloudWatch, joined by `conversation_id` |

> **Biggest gotcha:** `openid-connect`, `ai-proxy-advanced`, and `ai-sanitizer` are **Kong
> Enterprise** plugins — they validate in `kong config parse` but **Kong refuses to load
> them without a license**. This PoC proves the **model** path with the OSS `ai-proxy`
> (+ `jwt`); production **requires a Kong Enterprise / Konnect entitlement** for the AI +
> OIDC + PII plugins. (Tools no longer use Kong MCP plugins — they go through **AgentCore
> Gateway**, a separate managed AWS service.)

---

## 5. Path to real infrastructure

Prereqs: an **AWS account** with Bedrock model access in an AgentCore-Runtime GA region;
an **Okta tenant**; a **Kong Konnect** account with the **Enterprise/AI-gateway
entitlement**; somewhere for **Datadog/Langfuse**; `terraform`, `aws`, `deck`, the
`bedrock-agentcore` CLI, `psql`, Node 20, Python 3.11.

1. **Okta** — register the chat UI as a public OIDC app (Authorization Code + PKCE) and a
   service principal for the orchestrator (client_credentials) with the scopes the agent
   mints (`tool.retrieve`, `tool.submit_ticket`, and `model.invoke` for the reasoning hop)
   and your gateway audience. Store the M2M client_id/secret in a secret manager.
2. **Data** — RDS Postgres 16 + pgvector in a private subnet (IAM auth). Create the
   `documents` table + ivfflat index + a `UNIQUE(source_uri, chunk_index)` index (the
   indexer upserts on it).
3. **Model gateway** — Kong data plane joined to Konnect; apply the declarative config for
   the **model** routes (enterprise `openid-connect` / `ai-proxy-advanced`, and
   `ai-sanitizer` for PII — note it's **service-backed**, needs an external PII-detection
   service). Kong no longer fronts tools.
4. **AgentCore** — create the Runtime (IAM execution role via terraform; Runtime/Identity/
   Gateway/Memory via the `agentcore` CLI + boto3 — the AWS terraform provider does not
   manage them). Configure the **inbound JWT authorizer** with the Okta discovery URL +
   audience. AgentCore Identity auto-creates the **workload identity**; register the M2M
   credential provider; create the Memory resource.
5. **Tools (AgentCore Gateway)** — create an **AgentCore Gateway** and register the tool
   targets: `retrieve_documents` (a REST/Lambda target over RDS pgvector — grant it
   `rds-db:connect`) and `submit_ticket` (the ticketing API/Lambda). Configure the
   Gateway's inbound OAuth (Okta) and fine-grained per-tool access; the orchestrator
   connects to the Gateway's single MCP endpoint with its workload-identity token.
6. **Corpus** — curate the real corpus (no PII), index it with **Bedrock Titan via Kong**,
   confirm **recall@5 ≥ 80%**.
7. **Orchestrator** — deploy `services/runtime/app.py`'s logic on AgentCore Runtime using
   the **real** `bedrock_agentcore` SDK (the local `authorizer.py`/`identity.py` shims map
   1:1 to the runtime's inbound authorizer + AgentCore Identity). Set `MEMORY_ID`, the Kong
   URL, region.
8. **Observability** — run an OTel Collector with the PII-redaction processor (Decision 4)
   and fan out to Datadog + Langfuse; ship Kong, runtime, and tool logs with
   `conversation_id`; verify one turn shows the same `conversation_id`
   across every layer.
9. **UI** — point the frontend's OIDC config at the real Okta app and the API base at the
   deployed Runtime endpoint; build + deploy the static bundle.

---

## 6. Findings & gotchas (don't re-discover these)

1. **Kong AI/MCP/OIDC plugins are Enterprise-only at runtime** (free mode lists them but
   refuses to load them) → budget a Konnect/Enterprise entitlement. This PoC uses OSS
   equivalents.
2. **AgentCore is not in the AWS terraform provider** — Runtime/Identity/Gateway/Memory are
   created via the `agentcore` CLI + boto3 (terraform owns only the IAM execution role).
   `CreateMemory` requires `eventExpiryDuration`.
   - **Tools go through AgentCore Gateway (MCP), not Kong.** The agent connects to the
     Gateway's single MCP endpoint with a workload-identity token; the Gateway enforces
     inbound auth + fine-grained per-tool authz. Kong is the **model** gateway only.
3. **`ai-proxy-advanced`**: no `llm/v1/rerank` route_type, and it rejects mixing chat +
   embeddings in one plugin instance → one instance per route. (Reranker is descoped.)
4. **`ai-sanitizer` is service-backed** (calls an external PII-detection service), not
   inline rules.
5. **pgvector** needs a `UNIQUE(source_uri, chunk_index)` index for the idempotent indexer.
6. **HS256 vs RS256**: this PoC validates **user** tokens as RS256 via JWKS (prod-faithful);
   the M2M tokens to Kong stay HS256 because the OSS `jwt` plugin can't fetch JWKS (the
   enterprise `openid-connect` plugin does).
7. **Browser PKCE needs a secure context** — use `localhost`. The token issuer string is
   browser-facing while the runtime fetches JWKS from a cluster-internal URL; these are
   decoupled (`EXPECTED_ISSUER` vs `JWKS_URI`) — in prod both come from Okta discovery.
8. **`conversation_id`/`session_id` are UUIDs**; the proof suite uses readable literal ids
   for debuggability, so traces show a mix. Restart `jaeger` to clear its in-memory history.
9. **Compose**: the UI nginx re-resolves the backend per request (`resolver 127.0.0.11`) so
   it survives a backend restart; use `docker compose run --build` so a stale image doesn't
   run old code; Kong routes need `protocols: ["http"]` for local HTTP.
10. **Bundled Langfuse stops ingesting when the Docker disk fills** — it uploads each
    trace to its bundled MinIO, which rejects writes near the disk threshold (the OTLP
    endpoint returns 500, health stays 200, traces silently stop appearing). Fix:
    `docker system prune -af`. (Prod uses real S3.) Also allow ~60-90s on first boot for
    Langfuse migrations before traces show — and the Langfuse override is a separate `-f`
    file, so plain `docker compose up` is Jaeger-only and won't start it.
11. **MCP gateway behind another host → 421 Misdirected Request.** The MCP SDK's
    DNS-rebinding protection only allows `localhost` by default, so a request arriving with
    a different `Host` (e.g. `mock-agentcore-gateway:9000`) is rejected. Disable it on the
    gateway's `FastMCP` (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`);
    set `allowed_hosts` in prod.
12. **Enforcing per-tool scope on MCP at the gateway:** do it in a *plain ASGI* middleware
    (validate the JWT on every request; on a POST `tools/call`, require the tool's scope) —
    buffer the body and **replay it, then delegate to the real `receive`** for disconnects.
    A `BaseHTTPMiddleware` + contextvar approach breaks (the session manager runs the tool
    in a different task), and a naive replay that emits fake `http.request` events hangs the
    streamable-HTTP SSE response.

---

## 7. Verification & sign-off

- **Inbound auth + workload identity:** `docker compose --profile test run --rm tests` →
  **10/10** (OIDC PKCE, inbound authorizer 401s, `sub`→actor_id isolation, workload identity).
- **Live:** sign in at `http://localhost:5173`, run a research turn and a "open the ticket"
  turn, watch the trace in Jaeger.
- A change is "done" only when its acceptance criteria are demonstrably met (or explicitly
  deferred with a written reason).

---

## 8. Cost / region notes
- Region: an AgentCore-Runtime GA region.
- Rough monthly dev cost: NAT, RDS `db.t4g.medium`, Kong DP on Fargate, the Konnect + Kong
  Enterprise entitlement (commercial), Bedrock per-token, Datadog/Langfuse hosting.
- Hardening backlog: per-AZ NAT, VPC endpoints, RDS Multi-AZ, secret rotation.
