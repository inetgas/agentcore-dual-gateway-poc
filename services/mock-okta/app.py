"""Mock Okta — full OIDC provider for the PoC.

Two flows from one IdP:
  USER login (inbound to the AgentCore Runtime):
    GET  /authorize    Authorization Code + PKCE (S256) — minimal login page
    POST /login        completes login, redirects with ?code
    POST /oauth2/default/v1/token  grant_type=authorization_code (+PKCE verify)
                       -> RS256 USER JWT (sub, aud=mvp-runtime, scp)
  WORKLOAD (orchestrator -> tools, M2M):
    POST /oauth2/default/v1/token  grant_type=client_credentials
                       -> HS256 token (aud=mvp-kong-gateway) for Kong's OSS jwt plugin

  GET /oauth2/default/v1/keys                       JWKS (RS256 public key)
  GET /oauth2/default/.well-known/openid-configuration
  GET /oauth2/default/v1/userinfo                   (bearer user token)

User tokens are RS256 so the runtime's inbound authorizer validates them via JWKS
(production-faithful). M2M tokens stay HS256 for the local OSS Kong jwt plugin.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

ISSUER = os.environ.get("OKTA_ISSUER", "http://localhost:8081/oauth2/default")
USER_AUDIENCE = os.environ.get("USER_AUDIENCE", "mvp-runtime")
M2M_AUDIENCE = os.environ.get("M2M_AUDIENCE", "mvp-kong-gateway")
HS_SECRET = os.environ.get("OKTA_HS_SECRET", "mock-okta-hs256-shared-secret")
KID = "mock-okta-rsa-1"

# Demo SSO users (the "directory")
USERS = {
    "alice": {"sub": "alice@example.com", "name": "Alice Analyst"},
    "bob": {"sub": "bob@example.com", "name": "Bob Builder"},
}
USER_SCOPES = ["openid", "profile", "chat.invoke"]

# M2M service principal (workload identity creds)
M2M_CLIENTS = {
    "mvp-orchestrator-client": {"secret": "mvp-orchestrator-secret",
                                "allowed_scopes": {"tool.retrieve", "tool.submit_ticket"}},
}

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _key.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
_pub = _key.public_key().public_numbers()
_codes: dict[str, dict] = {}  # code -> {sub, code_challenge, redirect_uri, scope}

app = FastAPI(title="mock-okta-oidc")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _b64u_int(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# OIDC discovery + JWKS
# --------------------------------------------------------------------------- #
@app.get("/oauth2/default/.well-known/openid-configuration")
def discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/v1/authorize",
        "token_endpoint": f"{ISSUER}/v1/token",
        "jwks_uri": f"{ISSUER}/v1/keys",
        "userinfo_endpoint": f"{ISSUER}/v1/userinfo",
        "response_types_supported": ["code", "token"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": USER_SCOPES + ["tool.retrieve", "tool.submit_ticket"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
    }


@app.get("/oauth2/default/v1/keys")
def jwks():
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID,
                      "n": _b64u_int(_pub.n), "e": _b64u_int(_pub.e)}]}


# --------------------------------------------------------------------------- #
# Authorization Code + PKCE — user login
# --------------------------------------------------------------------------- #
@app.get("/oauth2/default/v1/authorize", response_class=HTMLResponse)
def authorize(client_id: str, redirect_uri: str, code_challenge: str,
              state: str = "", scope: str = "openid profile chat.invoke",
              code_challenge_method: str = "S256", response_type: str = "code"):
    if code_challenge_method != "S256":
        raise HTTPException(400, "only S256 PKCE supported")
    buttons = "".join(
        f'<form method="post" action="/oauth2/default/v1/login">'
        f'<input type="hidden" name="user" value="{u}">'
        f'<input type="hidden" name="redirect_uri" value="{redirect_uri}">'
        f'<input type="hidden" name="state" value="{state}">'
        f'<input type="hidden" name="scope" value="{scope}">'
        f'<input type="hidden" name="code_challenge" value="{code_challenge}">'
        f'<button type="submit" style="padding:10px 18px;margin:6px;font-size:15px">'
        f'Sign in as {info["name"]} ({u})</button></form>'
        for u, info in USERS.items())
    return (f'<html><body style="font-family:sans-serif;max-width:480px;margin:60px auto">'
            f'<h2>🔐 Okta SSO (mock)</h2><p>Authorization Code + PKCE for <b>{client_id}</b></p>'
            f'{buttons}</body></html>')


@app.post("/oauth2/default/v1/login")
def login(user: str = Form(...), redirect_uri: str = Form(...), state: str = Form(""),
          scope: str = Form("openid profile chat.invoke"), code_challenge: str = Form(...)):
    if user not in USERS:
        raise HTTPException(400, "unknown user")
    code = uuid.uuid4().hex
    _codes[code] = {"user": user, "code_challenge": code_challenge,
                    "redirect_uri": redirect_uri, "scope": scope}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


# --------------------------------------------------------------------------- #
# Token endpoint — both grants
# --------------------------------------------------------------------------- #
@app.post("/oauth2/default/v1/token")
async def token(grant_type: str = Form(...),
                # authorization_code
                code: str = Form(None), code_verifier: str = Form(None), redirect_uri: str = Form(None),
                # client_credentials
                client_id: str = Form(None), client_secret: str = Form(None), scope: str = Form(""),
                authorization: str = Header(None)):
    now = int(time.time())

    if grant_type == "authorization_code":
        rec = _codes.pop(code, None)
        if not rec:
            raise HTTPException(400, "invalid_grant: bad code")
        # PKCE: base64url(sha256(verifier)) must equal the stored challenge
        if not code_verifier:
            raise HTTPException(400, "invalid_request: missing code_verifier")
        calc = _b64u(hashlib.sha256(code_verifier.encode()).digest())
        if calc != rec["code_challenge"]:
            raise HTTPException(400, "invalid_grant: PKCE verification failed")
        if redirect_uri and redirect_uri != rec["redirect_uri"]:
            raise HTTPException(400, "invalid_grant: redirect_uri mismatch")
        u = USERS[rec["user"]]
        claims = {"iss": ISSUER, "aud": USER_AUDIENCE, "sub": u["sub"], "name": u["name"],
                  "iat": now, "exp": now + 3600, "jti": uuid.uuid4().hex,
                  "scp": rec["scope"].split()}
        access = jwt.encode(claims, _priv_pem, algorithm="RS256", headers={"kid": KID})
        return {"access_token": access, "id_token": access, "token_type": "Bearer",
                "expires_in": 3600, "scope": rec["scope"]}

    if grant_type == "client_credentials":
        cid, sec = client_id, client_secret
        if authorization and authorization.lower().startswith("basic "):
            raw = base64.b64decode(authorization.split(" ", 1)[1]).decode()
            cid, _, sec = raw.partition(":")
        c = M2M_CLIENTS.get(cid)
        if not c or c["secret"] != sec:
            raise HTTPException(401, "invalid_client")
        req = set(scope.split()) if scope else set()
        granted = sorted((req & c["allowed_scopes"]) if req else c["allowed_scopes"])
        m2m = jwt.encode({"iss": ISSUER, "aud": M2M_AUDIENCE, "sub": cid, "cid": cid,
                          "iat": now, "exp": now + 3600, "scp": granted},
                         HS_SECRET, algorithm="HS256", headers={"kid": "m2m-hs-1"})
        return {"access_token": m2m, "token_type": "Bearer", "expires_in": 3600,
                "scope": " ".join(granted)}

    raise HTTPException(400, "unsupported_grant_type")


@app.get("/oauth2/default/v1/userinfo")
def userinfo(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing token")
    tok = authorization.split(" ", 1)[1]
    claims = jwt.decode(tok, options={"verify_signature": False})
    return {"sub": claims.get("sub"), "name": claims.get("name")}


@app.post("/test/mint")
def test_mint(sub: str = "alice@example.com", aud: str = USER_AUDIENCE,
              exp_offset: int = 3600, scopes: str = "openid profile chat.invoke",
              sign: str = "good"):
    """TEST-ONLY: mint an RS256 user token with custom claims so the runtime's
    inbound authorizer can be checked against wrong-audience / expired / bad-sig."""
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": aud, "sub": sub, "iat": now,
              "exp": now + exp_offset, "scp": scopes.split()}
    key = _priv_pem
    if sign == "bad":
        # sign with a throwaway key -> signature won't verify against the JWKS
        bad = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = bad.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return {"access_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})}


def issue_user_token_for(user_key: str) -> str:
    """Test helper: mint a user RS256 token without the browser flow."""
    now = int(time.time())
    u = USERS[user_key]
    return jwt.encode({"iss": ISSUER, "aud": USER_AUDIENCE, "sub": u["sub"], "name": u["name"],
                       "iat": now, "exp": now + 3600, "scp": USER_SCOPES},
                      _priv_pem, algorithm="RS256", headers={"kid": KID})
