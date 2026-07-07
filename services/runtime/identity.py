"""AgentCore Identity (local stand-in) — workload identity + M2M token vault.

Models the production behavior:
  - When the AgentCore Runtime is created, AgentCore Identity AUTO-CREATES a
    WORKLOAD IDENTITY for the runtime (its machine identity inside AWS).
  - The workload identity is what authorizes retrieval of credentials from the
    token vault: @requires_access_token exchanges the M2M creds for a SCOPED JWT
    from Okta, caches it in the vault, and injects it.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import threading

import httpx

# --- workload identity (auto-created with the runtime) ----------------------
_workload_identity: dict = {}


def ensure_workload_identity(runtime_name: str) -> str:
    """Auto-create the runtime's workload identity on startup (idempotent)."""
    if not _workload_identity:
        wid = "wi-" + hashlib.sha256(runtime_name.encode()).hexdigest()[:16]
        _workload_identity.update(runtime=runtime_name, workload_identity_id=wid)
    return _workload_identity["workload_identity_id"]


def get_workload_identity() -> dict:
    return dict(_workload_identity)


# --- M2M credential provider + token vault ----------------------------------
PROVIDERS = {
    "okta-orchestrator": {
        "client_id": os.environ.get("OKTA_CLIENT_ID", "mvp-orchestrator-client"),
        "client_secret": os.environ.get("OKTA_CLIENT_SECRET", "mvp-orchestrator-secret"),
        "token_url": os.environ.get("OKTA_TOKEN_URL", "http://mock-okta:8080/oauth2/default/v1/token"),
    }
}
_vault: dict = {}
_lock = threading.Lock()
token_endpoint_calls = 0
# records which workload identity performed each retrieval (proof hook)
retrievals: list[dict] = []


def get_resource_oauth2_token(provider_name: str, scopes: list[str],
                              force_authentication: bool = False) -> str:
    global token_endpoint_calls
    wid = _workload_identity.get("workload_identity_id")
    if not wid:
        raise RuntimeError("no workload identity — runtime not initialized")
    key = (provider_name, tuple(sorted(scopes)))
    with _lock:
        if not force_authentication and key in _vault:
            retrievals.append({"workload_identity": wid, "provider": provider_name,
                               "scopes": sorted(scopes), "cache": "hit"})
            return _vault[key]
    p = PROVIDERS[provider_name]
    resp = httpx.post(p["token_url"], data={
        "grant_type": "client_credentials", "client_id": p["client_id"],
        "client_secret": p["client_secret"], "scope": " ".join(scopes)}, timeout=10)
    resp.raise_for_status()
    token_endpoint_calls += 1
    tok = resp.json()["access_token"]
    with _lock:
        _vault[key] = tok
    retrievals.append({"workload_identity": wid, "provider": provider_name,
                       "scopes": sorted(scopes), "cache": "miss"})
    return tok


def requires_access_token(*, provider_name: str, scopes: list[str],
                          auth_flow: str = "M2M", force_authentication: bool = False):
    def deco(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*a, **k):
                t = get_resource_oauth2_token(provider_name, scopes, force_authentication)
                return await fn(*a, access_token=t, **k)
            return aw

        @functools.wraps(fn)
        def w(*a, **k):
            t = get_resource_oauth2_token(provider_name, scopes, force_authentication)
            return fn(*a, access_token=t, **k)
        return w
    return deco
