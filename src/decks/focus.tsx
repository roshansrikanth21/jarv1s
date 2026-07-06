// Focus — a minimal, distraction-free UI preset: single centered column, a big
// breathing mic orb, calm cool-cyan palette (a deliberate departure from the two
// amber command-deck HUDs). All backend I/O goes through the shared
// useJarvisSocket hook — this file is view-only and re-implements no protocol.
// Rendered by src/routes/index.tsx as a plain component.
import { useEffect, useRef, useState } from "react";
import { WindowControls } from "@/components/jarvis/WindowControls";
import { useJarvisSocket, type Role } from "@/hooks/useJarvisSocket";

const ACCENT = "oklch(0.74 0.13 205)";   // cool cyan — distinct from the amber decks
const BG = "#070b0e";

export default function FocusDeck() {
  const { connected, listening, speaking, lines, stream, mood, level, send, toggleMic, showReconnectHint } =
    useJarvisSocket("JARVIS online. Ask, or tap the orb to speak.");
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6, behavior: "smooth" });
  }, [lines.length, stream]);

  const submit = () => { send(input); setInput(""); };

  const orbState = speaking ? "speaking" : listening ? "listening" : "idle";
  const orbScale = 1 + (listening ? Math.min(level / 32000, 1) * 0.35 : 0);

  return (
    <div style={{
      position: "fixed", inset: 0, background: BG, color: "#e8eef2",
      fontFamily: "JetBrains Mono, ui-monospace, monospace",
      display: "flex", flexDirection: "column", alignItems: "center",
    }}>
      {/* full-width header — controls pinned to window top-right */}
      <div style={{ position: "relative", width: "100%", flexShrink: 0 }}>
        <div className="drag" style={{
          width: "100%", display: "flex", alignItems: "center",
          justifyContent: "center", padding: "16px 52px 16px 20px", gap: 12,
        }}>
          <div style={{ width: "100%", maxWidth: 760, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: connected ? ACCENT : "#555",
                boxShadow: connected ? `0 0 8px ${ACCENT}` : "none" }} />
              <span style={{ fontSize: 13, letterSpacing: "0.32em", fontWeight: 600 }}>JARVIS</span>
              <span style={{ fontSize: 9, color: "rgba(232, 238, 242, 0.55)", letterSpacing: "0.2em", textTransform: "uppercase" }}>focus</span>
            </div>
            {mood?.enabled && (
              <div className="no-drag" style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 10, opacity: 0.7 }}>
                <span style={{ width: 6, height: 6, borderRadius: "50%", background: ACCENT,
                  opacity: 0.5 + (mood.intensity ?? 0) * 0.5 }} />
                <span style={{ textTransform: "lowercase", letterSpacing: "0.08em" }}>{mood.emotion}</span>
              </div>
            )}
          </div>
        </div>
        <div className="no-drag" style={{ position: "absolute", top: 12, right: 10, zIndex: 2 }}>
          <WindowControls accent={ACCENT} />
        </div>
      </div>

      {!connected && showReconnectHint && (
        <div className="no-drag" style={{
          width: "100%", maxWidth: 760, padding: "0 20px 8px",
          fontSize: 10, color: "rgba(232, 238, 242, 0.55)", letterSpacing: "0.06em",
        }}>
          Waking up…
        </div>
      )}

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
            onKeyDown={e => { if (e.key === "Enter") submit(); }}
            placeholder="Message JARVIS…"
            autoFocus
            style={{
              flex: 1, background: "transparent", border: "none", outline: "none",
              color: "#e8eef2", fontFamily: "inherit", fontSize: 13,
            }}
          />
          <button onClick={submit} style={{
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
