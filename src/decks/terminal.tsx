// Terminal — a green-phosphor CRT UI preset: monochrome, scanlines, a classic
// shell prompt. Distinct from the amber HUDs and the cyan Focus deck. Like Focus,
// it is view-only and drives all backend I/O through the shared useJarvisSocket
// hook (no protocol re-implementation). Rendered by src/routes/index.tsx.
import { useEffect, useRef, useState } from "react";
import { useJarvisSocket, type Role } from "@/hooks/useJarvisSocket";

const GREEN = "#41ff6e";
const DIM = "#1c7a3a";
const BG = "#020604";

export default function TerminalDeck() {
  const { connected, listening, speaking, lines, stream, mood, send, toggleMic } =
    useJarvisSocket("JARVIS terminal ready. Type a command or [speak].");
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6 });
  }, [lines.length, stream]);

  const submit = () => { send(input); setInput(""); };

  return (
    <div style={{
      position: "fixed", inset: 0, background: BG, color: GREEN,
      fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 13,
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      {/* title line (drag region for the frameless window) */}
      <div className="drag" style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        padding: "8px 14px", borderBottom: `1px solid ${DIM}`, fontSize: 11,
      }}>
        <span style={{ letterSpacing: "0.15em" }}>JARVIS://terminal</span>
        <span style={{ opacity: 0.7 }}>
          {mood?.enabled ? `[${mood.emotion}] ` : ""}
          {connected ? (speaking ? "● speaking" : listening ? "● listening" : "● online") : "○ offline"}
        </span>
      </div>

      {/* log */}
      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "12px 14px", lineHeight: 1.55 }}>
        {lines.map(l => <LogLine key={l.id} role={l.role} text={l.text} />)}
        {stream && <LogLine role="agent" text={stream} streaming />}
      </div>

      {/* prompt */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 14px", borderTop: `1px solid ${DIM}` }}>
        <button onClick={toggleMic} title="Voice input" style={{
          background: listening ? GREEN : "transparent", color: listening ? BG : GREEN,
          border: `1px solid ${GREEN}`, borderRadius: 3, cursor: "pointer",
          fontFamily: "inherit", fontSize: 10, padding: "3px 7px", letterSpacing: "0.1em",
        }}>{listening ? "REC" : "MIC"}</button>
        <span style={{ opacity: 0.85 }}>guest@jarvis:~$</span>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") submit(); }}
          autoFocus
          spellCheck={false}
          style={{
            flex: 1, background: "transparent", border: "none", outline: "none",
            color: GREEN, fontFamily: "inherit", fontSize: 13, caretColor: GREEN,
          }}
        />
      </div>

      {/* CRT scanline overlay */}
      <div style={{
        position: "fixed", inset: 0, pointerEvents: "none", zIndex: 5,
        background: "repeating-linear-gradient(transparent 0 2px, rgba(0,0,0,0.28) 2px 4px)",
        opacity: 0.5,
      }} />
      <style>{`@keyframes termBlink { 0%,49% { opacity: 1; } 50%,100% { opacity: 0; } }`}</style>
    </div>
  );
}

function LogLine({ role, text, streaming }: { role: Role; text: string; streaming?: boolean }) {
  const prefix = role === "user" ? "> " : role === "system" ? "# " : "";
  const color = role === "system" ? DIM : role === "user" ? "#8affb0" : GREEN;
  return (
    <div style={{ color, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {prefix}{text}
      {streaming && <span style={{ animation: "termBlink 1s step-end infinite" }}>█</span>}
    </div>
  );
}
