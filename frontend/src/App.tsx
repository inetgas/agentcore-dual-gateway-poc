import { useEffect, useRef, useState } from "react";
import { claims, handleCallback, login, logout, token } from "./oidc";

const RUNTIME = (import.meta as any).env?.VITE_RUNTIME ?? "http://localhost:8080";

// conversation_id == AgentCore Memory session_id — a UUID per conversation.
function uuid(): string {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
      });
}

interface Msg { role: "user" | "assistant"; text: string; route?: string; citations?: any[]; }

export default function App() {
  const [authed, setAuthed] = useState<boolean>(!!token());
  const [user, setUser] = useState<any>(claims());
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const convId = useRef(uuid());
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    handleCallback().then((t) => { if (t) { setAuthed(true); setUser(claims()); } }).catch(console.error);
  }, []);

  async function send() {
    const text = input.trim(); if (!text || busy) return;
    setMsgs((m) => [...m, { role: "user", text }]); setInput(""); setBusy(true);
    try {
      const r = await fetch(`${RUNTIME}/invoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token()}` },
        body: JSON.stringify({ conversation_id: convId.current, message: text }),
      });
      if (r.status === 401) { setMsgs((m) => [...m, { role: "assistant", text: "⚠️ 401 — inbound authorizer rejected the token (re-login)." }]); return; }
      const d = await r.json();
      setMsgs((m) => [...m, { role: "assistant", text: d.answer, route: d.route_taken, citations: d.citations }]);
    } finally { setBusy(false); setTimeout(() => end.current?.scrollIntoView({ behavior: "smooth" }), 0); }
  }

  if (!authed) {
    return (
      <div style={{ fontFamily: "sans-serif", maxWidth: 460, margin: "80px auto", textAlign: "center" }}>
        <h2>IT / Engineering Assistant</h2>
        <p style={{ color: "#64748b" }}>Sign in with Okta SSO (Authorization Code + PKCE) to chat.</p>
        <button onClick={login} style={{ padding: "12px 22px", fontSize: 16, background: "#2563eb", color: "#fff", border: 0, borderRadius: 8 }}>
          🔐 Sign in with Okta
        </button>
      </div>
    );
  }

  return (
    <div style={{ fontFamily: "sans-serif", maxWidth: 760, margin: "0 auto", height: "100vh", display: "flex", flexDirection: "column" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", borderBottom: "1px solid #e2e8f0" }}>
        <b>IT / Engineering Assistant</b>
        <span style={{ fontSize: 13, color: "#475569" }}>
          {user?.name} · <span style={{ fontFamily: "monospace" }}>{user?.sub}</span> · <a onClick={logout} style={{ color: "#2563eb", cursor: "pointer" }}>sign out</a>
        </span>
      </header>
      <div style={{ flex: 1, overflowY: "auto", padding: 16 }}>
        {msgs.length === 0 && <div style={{ color: "#94a3b8", textAlign: "center", marginTop: 40 }}>Ask about access, runbooks… or "open the ticket".</div>}
        {msgs.map((m, i) => (
          <div key={i} style={{ display: "flex", justifyContent: m.role === "user" ? "flex-end" : "flex-start", margin: "8px 0" }}>
            <div style={{ maxWidth: "75%", padding: "8px 12px", borderRadius: 14, background: m.role === "user" ? "#2563eb" : "#f1f5f9", color: m.role === "user" ? "#fff" : "#0f172a" }}>
              {m.route && <div style={{ fontSize: 11, fontWeight: 600, color: "#2563eb", marginBottom: 4 }}>{m.route === "submit_ticket" ? "🎫 Ticket" : m.route === "research" ? "🔍 Research" : "💬 Clarify"}</div>}
              <div style={{ fontSize: 14, whiteSpace: "pre-wrap" }}>{m.text}</div>
              {m.citations && m.citations.length > 0 && (
                <div style={{ fontSize: 12, color: "#64748b", marginTop: 6 }}>
                  Sources: {m.citations.map((c: any) => `${c.source_uri} (${c.relevance_score})`).join(", ")}
                </div>
              )}
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>
      <div style={{ display: "flex", gap: 8, padding: 12, borderTop: "1px solid #e2e8f0" }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask about access, runbooks, policies…" style={{ flex: 1, padding: 10, borderRadius: 8, border: "1px solid #cbd5e1" }} />
        <button onClick={send} disabled={busy} style={{ padding: "10px 18px", background: "#2563eb", color: "#fff", border: 0, borderRadius: 8 }}>{busy ? "…" : "Send"}</button>
      </div>
      <div style={{ fontSize: 11, color: "#64748b", textAlign: "center", padding: "6px", borderTop: "1px solid #e2e8f0", background: "#f8fafc" }}>
        Every request carries your Okta user JWT as <code>Authorization: Bearer</code> → AgentCore Runtime inbound authorizer → <code>sub</code> = Memory actor_id
      </div>
    </div>
  );
}
