// Terminal — a green-phosphor CRT UI preset: monochrome, scanlines, a classic
// shell prompt. Distinct from the amber HUDs and the cyan Focus deck. Like Focus,
// it is view-only and drives all backend I/O through the shared useJarvisSocket
// hook (no protocol re-implementation). Rendered by src/routes/index.tsx.
import { useEffect, useRef, useState } from "react";
import { WindowControls } from "@/components/jarvis/WindowControls";
import { ToolApprovalBanner } from "@/components/jarvis/ToolApprovalBanner";
import { useJarvisSocket, type Role } from "@/hooks/useJarvisSocket";

const GREEN = "#41ff6e";
const DIM = "#1c7a3a";
const BG = "#020604";

export default function TerminalDeck() {
  const {
    connected,
    listening,
    speaking,
    lines,
    stream,
    mood,
    send,
    toggleMic,
    showReconnectHint,
    pendingApproval,
    respondApproval,
  } = useJarvisSocket("JARVIS terminal ready. Type a command or [speak].");
  const [input, setInput] = useState("");
  const [userName, setUserName] = useState("guest");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    try {
      const n = localStorage.getItem("jarvis_user_name")?.trim();
      if (n) setUserName(n.toLowerCase().replace(/\s+/g, ""));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6 });
  }, [lines.length, stream]);

  const submit = () => {
    send(input);
    setInput("");
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: BG,
        color: GREEN,
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 13,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        paddingBottom: 48, // reserve room for the global UI switcher docked at the bottom
      }}
    >
      <ToolApprovalBanner request={pendingApproval} onRespond={respondApproval} />
      {/* title line (drag region for the frameless window) */}
      <div style={{ position: "relative", width: "100%", flexShrink: 0 }}>
        <div
          className="drag"
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "8px 52px 8px 14px",
            borderBottom: `1px solid ${DIM}`,
            fontSize: 11,
          }}
        >
          <span style={{ letterSpacing: "0.15em" }}>JARVIS://terminal</span>
          <span style={{ opacity: 0.85 }}>
            {mood?.enabled ? `[${mood.emotion}] ` : ""}
            {connected
              ? speaking
                ? "● speaking"
                : listening
                  ? "● listening"
                  : "● online"
              : "○ offline"}
          </span>
        </div>
        <div className="no-drag" style={{ position: "absolute", top: 4, right: 8, zIndex: 2 }}>
          <WindowControls accent={GREEN} />
        </div>
      </div>

      {!connected && showReconnectHint && (
        <div className="no-drag" style={{ padding: "4px 14px", fontSize: 11, color: DIM }}>
          # waking up…
        </div>
      )}

      {/* log */}
      <div
        ref={scrollRef}
        style={{ flex: 1, overflowY: "auto", padding: "12px 14px", lineHeight: 1.55 }}
      >
        {lines.map((l) => (
          <LogLine key={l.id} role={l.role} text={l.text} />
        ))}
        {stream && <LogLine role="agent" text={stream} streaming />}
      </div>

      {/* prompt */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "10px 14px",
          borderTop: `1px solid ${DIM}`,
        }}
      >
        <button
          onClick={toggleMic}
          title="Voice input"
          aria-label={listening ? "Stop listening" : "Start voice input"}
          style={{
            background: listening ? GREEN : "transparent",
            color: listening ? BG : GREEN,
            border: `1px solid ${GREEN}`,
            borderRadius: 3,
            cursor: "pointer",
            fontFamily: "inherit",
            fontSize: 10,
            padding: "3px 7px",
            letterSpacing: "0.1em",
          }}
        >
          {listening ? "REC" : "MIC"}
        </button>
        <span style={{ opacity: 0.85 }}>{userName}@jarvis:~$</span>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          aria-label="Terminal command"
          autoFocus
          spellCheck={false}
          style={{
            flex: 1,
            background: "transparent",
            border: "none",
            outline: "none",
            color: GREEN,
            fontFamily: "inherit",
            fontSize: 13,
            caretColor: GREEN,
          }}
        />
      </div>

      {/* CRT scanline overlay */}
      <div
        style={{
          position: "fixed",
          inset: 0,
          pointerEvents: "none",
          zIndex: 5,
          background: "repeating-linear-gradient(transparent 0 2px, rgba(0,0,0,0.28) 2px 4px)",
          opacity: 0.5,
        }}
      />
      <style>{`@keyframes termBlink { 0%,49% { opacity: 1; } 50%,100% { opacity: 0; } }`}</style>
    </div>
  );
}

function LogLine({ role, text, streaming }: { role: Role; text: string; streaming?: boolean }) {
  const prefix = role === "user" ? "> " : role === "system" ? "# " : "";
  const color = role === "system" ? DIM : role === "user" ? "#8affb0" : GREEN;
  return (
    <div style={{ color, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {prefix}
      {text}
      {streaming && <span style={{ animation: "termBlink 1s step-end infinite" }}>█</span>}
    </div>
  );
}
