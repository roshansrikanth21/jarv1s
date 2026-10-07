// c0mr4de — a red-on-black offensive-security console preset. Same shared backend
// as every other deck (useJarvisSocket, no protocol re-implementation); this is a
// pentest-focused skin: monospace log, an authorization banner (c0mr4de's house
// rule — only ever run against systems you own or are authorized to test), a
// command prompt, and a compact recon/exploit quick-bar that just pre-fills common
// asks. It drives the live JARVIS agent; wiring it to invoke the standalone c0mr4de
// Python agent is a backend bridge, tracked separately. Rendered by routes/index.tsx.
import { useEffect, useRef, useState } from "react";
import { WindowControls } from "@/components/jarvis/WindowControls";
import { ToolApprovalBanner } from "@/components/jarvis/ToolApprovalBanner";
import { useJarvisSocket, type Role } from "@/hooks/useJarvisSocket";

const RED = "#ff3b3b";
const DIM = "#7a1c1c";
const TEXT = "#ff6b6b";
const BG = "#0a0303";
const OK = "#ff9d66";

// Quick asks that pre-fill the prompt — a nudge toward c0mr4de's passive-first flow.
const QUICK: { label: string; fill: string }[] = [
  { label: "recon", fill: "recon the target attack surface: " },
  { label: "whois", fill: "whois/RDAP lookup for " },
  { label: "subdomains", fill: "enumerate subdomains (crt.sh) for " },
  { label: "intel", fill: "threat-intel reputation (virustotal/abuseipdb/greynoise) for " },
  { label: "report", fill: "write up the findings so far as a pentest report" },
];

export default function Comr4deDeck() {
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
  } = useJarvisSocket("c0mr4de console online. Authorized targets only. Type a task or [MIC].");
  const [input, setInput] = useState("");
  const [modelLabel, setModelLabel] = useState<string>("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6 });
  }, [lines.length, stream]);

  // Show which model is active, and (if none is) what the advisor suggests picking -
  // including already-downloaded local models it found on disk.
  useEffect(() => {
    let alive = true;
    const load = () => {
      fetch("/api/models")
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (!alive || !d) return;
          const active = d.active as { deep?: string; enabled?: boolean } | undefined;
          const sug = d.suggestion as { name?: string; ready?: boolean; kind?: string } | undefined;
          if (active?.enabled && active.deep) {
            setModelLabel(`local: ${active.deep}`);
          } else if (sug?.name) {
            setModelLabel(sug.ready ? `pick: ${sug.name}` : `import: ${sug.name}`);
          } else {
            setModelLabel("cloud brain");
          }
        })
        .catch(() => {});
    };
    load();
    const t = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const submit = () => {
    if (!input.trim()) return;
    send(input);
    setInput("");
  };

  const fill = (text: string) => {
    setInput(text);
    inputRef.current?.focus();
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: BG,
        color: TEXT,
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 13,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        paddingBottom: 48, // room for the global UI switcher docked at the bottom
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
          <span>
            <span style={{ letterSpacing: "0.2em", color: RED, fontWeight: 700 }}>c0mr4de</span>
            {modelLabel && (
              <span style={{ marginLeft: 10, fontSize: 10, color: OK, opacity: 0.9 }}>
                {modelLabel}
              </span>
            )}
          </span>
          <span style={{ opacity: 0.85 }}>
            {mood?.enabled ? `[${mood.emotion}] ` : ""}
            {connected
              ? speaking
                ? "● speaking"
                : listening
                  ? "● listening"
                  : "● armed"
              : "○ offline"}
          </span>
        </div>
        <div className="no-drag" style={{ position: "absolute", top: 4, right: 8, zIndex: 2 }}>
          <WindowControls accent={RED} />
        </div>
      </div>

      {/* authorization banner — c0mr4de's standing rule */}
      <div
        className="no-drag"
        style={{
          flexShrink: 0,
          padding: "5px 14px",
          borderBottom: `1px solid ${DIM}`,
          fontSize: 10.5,
          letterSpacing: "0.04em",
          color: OK,
          background: "rgba(255,59,59,0.06)",
        }}
      >
        ⚠ AUTHORIZED ENGAGEMENTS ONLY — run against systems you own or have written permission to test.
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

      {/* quick-bar */}
      <div
        className="no-drag"
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          padding: "6px 14px 0",
        }}
      >
        {QUICK.map((q) => (
          <button
            key={q.label}
            onClick={() => fill(q.fill)}
            style={{
              background: "transparent",
              color: TEXT,
              border: `1px solid ${DIM}`,
              borderRadius: 3,
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 10,
              padding: "3px 8px",
              letterSpacing: "0.08em",
            }}
          >
            {q.label}
          </button>
        ))}
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
            background: listening ? RED : "transparent",
            color: listening ? BG : RED,
            border: `1px solid ${RED}`,
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
        <span style={{ opacity: 0.85, color: RED }}>root@c0mr4de:~#</span>
        <input
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          aria-label="c0mr4de command"
          autoFocus
          spellCheck={false}
          style={{
            flex: 1,
            background: "transparent",
            border: "none",
            outline: "none",
            color: TEXT,
            fontFamily: "inherit",
            fontSize: 13,
            caretColor: RED,
          }}
        />
      </div>

      {/* subtle scanline overlay */}
      <div
        style={{
          position: "fixed",
          inset: 0,
          pointerEvents: "none",
          zIndex: 5,
          background: "repeating-linear-gradient(transparent 0 2px, rgba(0,0,0,0.3) 2px 4px)",
          opacity: 0.4,
        }}
      />
      <style>{`@keyframes cmrBlink { 0%,49% { opacity: 1; } 50%,100% { opacity: 0; } }`}</style>
    </div>
  );
}

function LogLine({ role, text, streaming }: { role: Role; text: string; streaming?: boolean }) {
  const prefix = role === "user" ? "> " : role === "system" ? "# " : "";
  const color = role === "system" ? OK : role === "user" ? "#ffb3b3" : TEXT;
  return (
    <div style={{ color, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {prefix}
      {text}
      {streaming && <span style={{ animation: "cmrBlink 1s step-end infinite" }}>█</span>}
    </div>
  );
}
