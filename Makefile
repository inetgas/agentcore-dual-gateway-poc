# Convenience wrappers. The OTel Collector runs ONE always-on config, so these are all
# safe to mix — no command can misconfigure Langfuse forwarding. `obs` just adds the
# Langfuse containers; everything else is the lightweight (Jaeger-only) stack.
COMPOSE     = docker compose
COMPOSE_OBS = docker compose -f docker-compose.yml -f docker-compose.langfuse.yml

.PHONY: help up obs test token down logs ps
help:   ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

up:     ## Start the stack (Jaeger only — lightweight; http://localhost:5173)
	$(COMPOSE) up -d --build

obs:    ## Start the stack WITH Langfuse (PII-redacted traces) — http://localhost:3000
	$(COMPOSE_OBS) up -d --build

test:   ## Run the proof suite (15/15)
	$(COMPOSE) --profile test run --rm --build tests

token:  ## Mint a demo user JWT (for headless POST /invoke)
	@curl -s -X POST http://localhost:8081/test/mint -H 'content-type: application/json' \
	  -d '{"sub":"alice@example.com","name":"Alice","aud":"mvp-runtime","scope":"openid profile","sign":"good"}' \
	  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'

down:   ## Stop everything (including Langfuse); add ARGS=-v to wipe volumes
	$(COMPOSE_OBS) down $(ARGS)

logs:   ## Tail runtime + collector logs
	$(COMPOSE_OBS) logs -f runtime otel-collector

ps:     ## Show stack status
	$(COMPOSE_OBS) ps
