"""PoC proof suite — the inbound user-auth chain + workload identity.

Proves:
  1) Okta OIDC Authorization Code + PKCE -> user JWT
  2) AgentCore Runtime inbound JWT authorizer (validate before entrypoint)
  3) user JWT `sub` -> AgentCore Memory actor_id (per-user isolation)
  4) workload identity auto-created for the runtime + used for token-vault retrieval
"""

import base64
import hashlib
import os
import secrets
import urllib.parse

import httpx
import jwt
import pytest

OKTA = os.environ["OKTA_INTERNAL"]          # http://mock-okta:8080/oauth2/default
RUNTIME = os.environ["RUNTIME_URL"]          # http://runtime:8080
REDIRECT = "http://localhost:5173/callback"


def _pkce():
    v = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def _authorize_code(user, code_challenge):
    r = httpx.post(f"{OKTA}/v1/login", data={
        "user": user, "redirect_uri": REDIRECT, "state": "xyz",
        "scope": "openid profile chat.invoke", "code_challenge": code_challenge},
        follow_redirects=False, timeout=10)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    return urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]


def login(user="alice") -> str:
    """Full Authorization Code + PKCE flow -> user access token."""
    verifier, challenge = _pkce()
    code = _authorize_code(user, challenge)
    r = httpx.post(f"{OKTA}/v1/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": verifier, "redirect_uri": REDIRECT}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


_OKTA_BASE = OKTA.rsplit("/oauth2/default", 1)[0]  # /test/mint is at the root


def mint(**params) -> str:
    return httpx.post(f"{_OKTA_BASE}/test/mint", params=params, timeout=10).json()["access_token"]


def invoke(token, message, conversation_id="c1"):
    return httpx.post(f"{RUNTIME}/invoke",
                      headers={"Authorization": f"Bearer {token}"} if token else {},
                      json={"conversation_id": conversation_id, "message": message}, timeout=30)


# ---- 1) OIDC Authorization Code + PKCE -------------------------------------

def test_pkce_login_yields_user_jwt():
    tok = login("alice")
    claims = jwt.decode(tok, options={"verify_signature": False})
    assert claims["sub"] == "alice@example.com"
    assert claims["aud"] == "mvp-runtime"
    assert "chat.invoke" in claims["scp"]
    assert jwt.get_unverified_header(tok)["alg"] == "RS256"


def test_pkce_wrong_verifier_rejected():
    _, challenge = _pkce()
    code = _authorize_code("alice", challenge)
    r = httpx.post(f"{OKTA}/v1/token", data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": "WRONG-VERIFIER-THAT-DOES-NOT-MATCH-CHALLENGE",
        "redirect_uri": REDIRECT}, timeout=10)
    assert r.status_code == 400  # PKCE enforced


# ---- 2) Inbound JWT authorizer ---------------------------------------------

def test_valid_user_jwt_reaches_entrypoint():
    r = invoke(login("alice"), "How do I request access to the data lake?")
    assert r.status_code == 200, r.text
    assert r.json()["route_taken"] == "research"


def test_no_token_401():
    assert invoke(None, "hi").status_code == 401


def test_bad_signature_401():
    assert invoke(mint(sign="bad"), "hi").status_code == 401


def test_wrong_audience_401():
    assert invoke(mint(aud="some-other-api"), "hi").status_code == 401


def test_expired_401():
    assert invoke(mint(exp_offset=-30), "hi").status_code == 401


# ---- 3) sub -> actor_id (per-user memory isolation) ------------------------

def test_sub_becomes_actor_id_and_isolates_memory():
    alice, bob = login("alice"), login("bob")
    conv = "shared-conversation-" + secrets.token_hex(4)  # fresh (memory is durable)

    a1 = invoke(alice, "How do I request access to the data lake?", conv).json()
    assert a1["actor_id"] == "alice@example.com"
    assert a1["prior_turns"] == 0
    a2 = invoke(alice, "And what about for production data?", conv).json()
    assert a2["prior_turns"] == 1  # alice's own prior turn loaded from memory

    # bob uses the SAME conversation_id but is a different sub -> separate memory
    b1 = invoke(bob, "How do I request access to the data lake?", conv).json()
    assert b1["actor_id"] == "bob@example.com"
    assert b1["prior_turns"] == 0  # bob sees none of alice's turns


# ---- 4) Workload identity ---------------------------------------------------

def test_workload_identity_auto_created():
    wi = httpx.get(f"{RUNTIME}/workload-identity", timeout=10).json()
    assert wi["runtime"] == "mvp-orchestrator"
    assert wi["workload_identity_id"].startswith("wi-")


def test_workload_identity_used_for_token_vault():
    # a research turn triggers a tool.retrieve token retrieval via the workload identity
    # (the ReAct loop also retrieves a model.invoke token for its Kong reasoning hop)
    invoke(login("alice"), "How do I request access to the data lake?", "wi-check")
    wi = httpx.get(f"{RUNTIME}/workload-identity", timeout=10).json()
    rets = wi["token_vault_retrievals"]
    assert rets, "expected at least one token-vault retrieval"
    assert all(r["workload_identity"] == wi["workload_identity_id"] for r in rets)
    assert all(r["provider"] == "okta-orchestrator" for r in rets)
    # the retrieve tool's scoped JWT was minted by the workload identity
    assert any("tool.retrieve" in r["scopes"] for r in rets)
