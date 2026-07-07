"""AgentCore Runtime — INBOUND JWT authorizer.

Models the AgentCore Runtime's inbound JWT authorizer: every request to the
entrypoint must carry `Authorization: Bearer <user JWT>`. The authorizer
validates the token BEFORE the entrypoint runs:
  - signature against Okta's JWKS (RS256, discovered from the issuer)
  - issuer == configured issuer
  - audience == configured audience
  - not expired
  - allowed-scopes (empty by default → any valid token; per brief §1.5)
On success it returns the claims; `sub` becomes the AgentCore Memory actor_id.
"""

from __future__ import annotations

import os

import httpx
import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException

EXPECTED_ISSUER = os.environ.get("EXPECTED_ISSUER", "http://localhost:8081/oauth2/default")
EXPECTED_AUDIENCE = os.environ.get("EXPECTED_AUDIENCE", "mvp-runtime")
DISCOVERY_URL = os.environ.get("DISCOVERY_URL", "http://mock-okta:8080/oauth2/default/.well-known/openid-configuration")
# The issuer string in tokens is browser-facing (localhost:8081); the JWKS must be
# fetched from a cluster-internal URL. In prod both come from discovery directly.
JWKS_URI = os.environ.get("JWKS_URI", "http://mock-okta:8080/oauth2/default/v1/keys")
# allowed-scopes: empty => any valid token passes (brief §1.5)
ALLOWED_SCOPES = [s for s in os.environ.get("ALLOWED_SCOPES", "").split(",") if s]

_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # Faithful: confirm the issuer's discovery doc is reachable, then use the
        # cluster-internal JWKS URL for key retrieval.
        httpx.get(DISCOVERY_URL, timeout=10).raise_for_status()
        _jwks_client = PyJWKClient(JWKS_URI)
    return _jwks_client


def validate(token: str) -> dict:
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, signing_key, algorithms=["RS256"],
                            audience=EXPECTED_AUDIENCE, issuer=EXPECTED_ISSUER)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(401, "invalid audience")
    except jwt.InvalidIssuerError:
        raise HTTPException(401, "invalid issuer")
    except Exception as e:
        raise HTTPException(401, f"invalid token: {type(e).__name__}")

    if ALLOWED_SCOPES:
        scp = set(claims.get("scp", []))
        if not set(ALLOWED_SCOPES) & scp:
            raise HTTPException(403, f"missing required scope (need one of {ALLOWED_SCOPES})")
    return claims


def require_user(authorization: str = Header(default="")) -> dict:
    """FastAPI dependency — runs before the entrypoint, exactly like the
    AgentCore Runtime inbound authorizer."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return validate(authorization.split(" ", 1)[1])
