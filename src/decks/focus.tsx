// Focus — a third UI preset: minimal, distraction-free, cool-toned.
// A deliberate departure from the two amber command-deck HUDs: single centered
// column, big breathing mic orb, calm typography. Fully wired to the same backend
// WS protocol (command / start_listening / tts_start|end) so it's a real preset,
// not a mockup. Rendered by src/routes/index.tsx as a plain component.
import { useCallback, useEffect, useRef, useState } from "react";

type Role = "user" | "agent" | "system";
type Line = { id: string; role: Role; text: string };
type Mood = { enabled?: boolean; emotion?: string; colour?: string; intensity?: number } | null;

const uid = () => Math.random().toString(36).slice(2);
const ACCENT = "oklch(0.74 0.13 205)";   // cool cyan — distinct from the amber decks
const BG = "#070b0e";

export default function FocusDeck() {
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking]   = useState(false);
  const [input, setInput]         = useState("");
  const [lines, setLines]         = useState<Line[]>([{ id: uid(), role: "system", text: "JARVIS online. Ask, or tap the orb to speak." }]);
  const [stream, setStream]       = useState("");
  const [mood, setMood]           = useState<Mood>(null);
  const [level, setLevel]         = useState(0);

  const wsRef    = useRef<WebSocket | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const addRef   = useRef<(r: Role, t: string) => void>(null!);
  const connRef  = useRef<() => void>(null!);

  const add = useCallback((role: Role, text: string) => {
    if (!text.trim()) return;
    setLines(p => [...p.slice(-120), { id: uid(), role, text }]);
  }, []);
  addRef.current = add;

  const playTts = useCallback((b64: string) => {
    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
    const blob = new Blob([Uint8Array.from(atob(b64), c => c.charCodeAt(0))], { type: "audio/mpeg" });
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    audioRef.current = a;
    setSpeaking(true);
    const end = () => {
      setSpeaking(false);
      URL.revokeObjectURL(url);
      audioRef.current = null;
      wsRef.current?.send(JSON.stringify({ action: "tts_end" }));
    };
    a.onended = end; a.onerror = end;
    a.play().then(() => wsRef.current?.send(JSON.stringify({ action: "tts_start" }))).catch(end);
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(`ws://${window.location.host}/ws`);
    wsRef.current = ws;
    ws.onopen = () => {
      setConnected(true);
      fetch("/api/agent/status").then(r => r.json()).then(d => { if (d?.emotion) setMood(d.emotion); }).catch(() => {});
    };
    ws.onclose = () => { setConnected(false); setListening(false); setSpeaking(false); };
    ws.onerror = () => { setTimeout(() => connRef.current(), 5000); };
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        const txt: string = d.text ?? d.message ?? "";
        if (d.type === "state" || d.type === "status") setSpeaking(d.status === "speaking");
        if (d.type === "emotion" && d.emotion) setMood(d.emotion);
        if (d.type === "transcription" || d.type === "transcript") addRef.current("user", txt);
        if (d.type === "llm_chunk" && d.text) setStream(p => p + (d.text as string));
        if (d.type === "llm_response" || d.type === "response") { setStream(""); addRef.current("agent", txt); }
        if (d.type === "tts_audio" && d.data) playTts(d.data as string);
        if (d.type === "tts_stop") {
          if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
          setSpeaking(false);
          wsRef.current?.send(JSON.stringify({ action: "tts_end" }));
        }
        if (d.type === "audio_level") setLevel(Number(d.level) || 0);
      } catch { /* ignore malformed */ }
    };
  }, [playTts]);
  connRef.current = connect;

  useEffect(() => {
    connect();
    return () => {
      if (audioRef.current) audioRef.current.pause();
      wsRef.current?.close();
    };
  }, [connect]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6, behavior: "smooth" });
  }, [lines.length, stream]);

  const send = useCallback((cmd = input) => {
    const t = cmd.trim();
    if (!t) return;
    add("user", t);
    setInput("");
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: "command", text: t }));
    } else {
      fetch("/api/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command: t }) })
        .then(r => r.json()).then(d => add("agent", d.response ?? "Done.")).catch(() => add("system", "No backend connection."));
    }
  }, [input, add]);

  const toggleMic = () => {
    if (!connected) { connect(); return; }
    const next = !listening;
    setListening(next);
    wsRef.current?.send(JSON.stringify({ action: next ? "start_listening" : "stop_listening" }));
  };

  const orbState = speaking ? "speaking" : listening ? "listening" : "idle";
  const orbScale = 1 + (listening ? Math.min(level / 32000, 1) * 0.35 : 0);

  return (
    <div style={{
      position: "fixed", inset: 0, background: BG, color: "#e8eef2",
      fontFamily: "JetBrains Mono, ui-monospace, monospace",
      display: "flex", flexDirection: "column", alignItems: "center",
    }}>
      {/* top bar (className="drag" makes the frameless window movable here) */}
      <div className="drag" style={{
        width: "100%", maxWidth: 760, display: "flex", alignItems: "center",
        justifyContent: "space-between", padding: "16px 20px", gap: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: connected ? ACCENT : "#555",
            boxShadow: connected ? `0 0 8px ${ACCENT}` : "none" }} />
          <span style={{ fontSize: 13, letterSpacing: "0.32em", fontWeight: 600 }}>JARVIS</span>
          <span style={{ fontSize: 9, opacity: 0.4, letterSpacing: "0.2em", textTransform: "uppercase" }}>focus</span>
        </div>
        {mood?.enabled && (
          <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 10, opacity: 0.7 }}>
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: ACCENT,
              opacity: 0.5 + (mood.intensity ?? 0) * 0.5 }} />
            <span style={{ textTransform: "lowercase", letterSpacing: "0.08em" }}>{mood.emotion}</span>
          </div>
        )}
      </div>

      {/* conversation */}
      <div ref={scrollRef} style={{
        flex: 1, width: "100%", maxWidth: 760, overflowY: "auto", padding: "8px 20px 20px",
        display: "flex", flexDirection: "column", gap: 14,
      }}>
        {lines.map(l => <Bubble key={l.id} role={l.role} text={l.text} />)}
        {stream && <Bubble role="agent" text={stream} streaming />}
      </div>

      {/* mic orb */}
      <button onClick={toggleMic} title={listening ? "Stop listening" : "Speak"} style={{
        border: "none", background: "transparent", cursor: "pointer", marginBottom: 8,
      }}>
        <span style={{
          display: "block", width: 60, height: 60, borderRadius: "50%",
          transition: "transform 0.12s ease, box-shadow 0.3s ease",
          transform: `scale(${orbScale})`,
          background: orbState === "idle" ? "transparent" : `radial-gradient(circle, ${ACCENT}44, transparent 70%)`,
          border: `2px solid ${orbState === "idle" ? "#2a3a42" : ACCENT}`,
          boxShadow: orbState === "speaking" ? `0 0 26px ${ACCENT}` : orbState === "listening" ? `0 0 16px ${ACCENT}88` : "none",
          animation: orbState === "speaking" ? "focusPulse 1.1s ease-in-out infinite" : "none",
        }} />
      </button>
      <div style={{ fontSize: 9, opacity: 0.35, letterSpacing: "0.18em", textTransform: "uppercase", marginBottom: 6 }}>
        {orbState === "speaking" ? "speaking" : orbState === "listening" ? "listening" : "tap to speak"}
      </div>

      {/* input */}
      <div style={{ width: "100%", maxWidth: 760, padding: "0 20px 22px" }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 10, padding: "10px 16px",
          background: "#0d1418", border: "1px solid #1c2831", borderRadius: 14,
        }}>
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") send(); }}
            placeholder="Message JARVIS…"
            autoFocus
            style={{
              flex: 1, background: "transparent", border: "none", outline: "none",
              color: "#e8eef2", fontFamily: "inherit", fontSize: 13,
            }}
          />
          <button onClick={() => send()} style={{
            border: "none", background: ACCENT, color: BG, cursor: "pointer",
            borderRadius: 9, padding: "6px 14px", fontFamily: "inherit", fontSize: 11, fontWeight: 700,
          }}>Send</button>
        </div>
      </div>

      <style>{`@keyframes focusPulse { 0%,100% { box-shadow: 0 0 20px ${ACCENT}; } 50% { box-shadow: 0 0 34px ${ACCENT}; } }`}</style>
    </div>
  );
}

function Bubble({ role, text, streaming }: { role: Role; text: string; streaming?: boolean }) {
  if (role === "system") {
    return <div style={{ alignSelf: "center", fontSize: 10, opacity: 0.4, letterSpacing: "0.06em", textAlign: "center" }}>{text}</div>;
  }
  const me = role === "user";
  return (
    <div style={{
      alignSelf: me ? "flex-end" : "flex-start", maxWidth: "82%",
      background: me ? "#132028" : "#0e1418",
      border: `1px solid ${me ? "#1e3038" : "#182028"}`,
      borderRadius: 14, padding: "10px 14px", fontSize: 13, lineHeight: 1.5,
      color: me ? "#dff0f5" : "#d8e2e8", whiteSpace: "pre-wrap", wordBreak: "break-word",
    }}>
      {text}{streaming && <span style={{ opacity: 0.5 }}>▋</span>}
    </div>
  );
}
