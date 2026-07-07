// Okta OIDC — Authorization Code + PKCE (public client, no secret), per the brief.
const OKTA = (import.meta as any).env?.VITE_OKTA ?? "http://localhost:8081/oauth2/default";
const CLIENT_ID = "mvp-chat-ui";
const SCOPE = "openid profile chat.invoke";
const REDIRECT = window.location.origin + "/";

function b64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomVerifier(): string {
  return b64url(crypto.getRandomValues(new Uint8Array(48)));
}
async function challenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(new Uint8Array(digest));
}

export async function login() {
  const verifier = randomVerifier();
  sessionStorage.setItem("pkce_verifier", verifier);
  const c = await challenge(verifier);
  const q = new URLSearchParams({
    response_type: "code", client_id: CLIENT_ID, redirect_uri: REDIRECT,
    scope: SCOPE, code_challenge: c, code_challenge_method: "S256", state: "poc",
  });
  window.location.href = `${OKTA}/v1/authorize?${q}`;
}

export async function handleCallback(): Promise<string | null> {
  const code = new URLSearchParams(window.location.search).get("code");
  if (!code) return null;
  const verifier = sessionStorage.getItem("pkce_verifier") || "";
  const body = new URLSearchParams({
    grant_type: "authorization_code", code, code_verifier: verifier, redirect_uri: REDIRECT,
  });
  const r = await fetch(`${OKTA}/v1/token`, {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body,
  });
  if (!r.ok) throw new Error("token exchange failed");
  const tok = (await r.json()).access_token as string;
  sessionStorage.setItem("user_token", tok);
  window.history.replaceState({}, "", "/");
  return tok;
}

export function token(): string | null { return sessionStorage.getItem("user_token"); }
export function logout() { sessionStorage.clear(); window.location.href = "/"; }
export function claims(): any {
  const t = token(); if (!t) return null;
  try { return JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { return null; }
}
