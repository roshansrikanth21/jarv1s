// OpsConsole — the Command Deck's live pentest cockpit. A large, readable, terminal-style
// feed of JARVIS's REAL tool executions, narrated step by step as they happen: which tool,
// against what target, running → done/failed, and the actual output. This is the ground
// truth — if JARVIS claims an action and there's no step here, it didn't happen.
//
// Presentational: the deck feeds it the agent trace (from `agent_tool` events) plus the
// currently-running tool (from the "Running <name>…" state), and a close handler.
import { motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";

export type OpsStep = { step: number; action: string; args: Record<string, unknown>; observation: string };

const TOOL_META: Record<string, { label: string; color: string }> = {
  recon:       { label: "RECON",  color: "var(--c-blue)" },
  pentest:     { label: "ATTACK", color: "var(--c-danger)" },
  scope:       { label: "SCOPE",  color: "var(--c-gold)" },
  browse:      { label: "BROWSE", color: "var(--c-green)" },
  search_web:  { label: "SEARCH", color: "var(--c-amber)" },
  capture_screen: { label: "VISION", color: "var(--c-green)" },
};
const meta = (a: string) => TOOL_META[a] ?? { label: a.toUpperCase(), color: "var(--c-amber)" };

function failed(obs: string): boolean {
  return /^(⛔|refused|error|.*\bfailed\b|docker isn't|not (a valid|in )|unavailable|invalid|no output|couldn't)/i.test(
    (obs || "").trim(),
  );
}

// A short human title for the operation, e.g. "example.com · full" or "add 10.0.0.0/24".
function subject(s: OpsStep): string {
  const a = s.args || {};
  if (s.action === "recon" || s.action === "pentest")
    return [a.target, a.task].filter(Boolean).join(" · ");
  if (s.action === "scope")
    return [a.action, a.target].filter(Boolean).join(" ");
  const first = Object.values(a).find((v) => typeof v === "string" && v.length < 80);
  return typeof first === "string" ? first : "";
}

function Step({ s, running }: { s: OpsStep; running?: boolean }) {
  const m = meta(s.action);
  const isFail = !running && failed(s.observation);
  const col = running ? "var(--c-gold)" : isFail ? "var(--c-danger)" : m.color;
  const [open, setOpen] = useState(false);
  const obs = s.observation || "";
  const long = obs.length > 260;

  return (
    <div style={{ display: "flex", gap: 10, position: "relative" }}>
      {/* timeline rail */}
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flexShrink: 0, width: 14 }}>
        <span style={{
          width: 10, height: 10, borderRadius: "50%", background: col, marginTop: 3,
          boxShadow: `0 0 8px ${col}`,
          animation: running ? "opsPulse 1s infinite" : "none",
        }} />
        <span style={{ flex: 1, width: 1, background: "var(--c-line)", marginTop: 2 }} />
      </div>

      <div style={{ flex: 1, minWidth: 0, paddingBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{
            fontSize: 9.5, fontWeight: 700, letterSpacing: "0.16em", color: "var(--c-bg)",
            background: col, padding: "2px 7px", borderRadius: 3,
          }}>{m.label}</span>
          <span style={{ fontSize: 12, color: "var(--c-fg)", fontWeight: 600 }}>{subject(s)}</span>
          <span style={{ marginLeft: "auto", fontSize: 9, letterSpacing: "0.14em", textTransform: "uppercase", color: col }}>
            {running ? "running…" : isFail ? "refused/failed" : "done"}
          </span>
        </div>

        {!running && obs && (
          <>
            <pre style={{
              margin: "7px 0 3px", padding: "9px 11px", background: "oklch(0.12 0.015 28 / 0.7)",
              border: `1px solid ${col}22`, borderLeft: `2px solid ${col}`, borderRadius: 5,
              fontSize: 10.5, lineHeight: 1.5, color: "var(--c-fg)", whiteSpace: "pre-wrap",
              wordBreak: "break-word", fontFamily: "var(--font-mono)",
              maxHeight: open ? "none" : 150, overflow: "hidden",
            }}>{obs}</pre>
            {long && (
              <button onClick={() => setOpen((o) => !o)} style={{
                background: "transparent", border: "none", color: col, cursor: "pointer",
                fontSize: 9.5, letterSpacing: "0.1em", padding: "1px 0",
              }}>{open ? "▲ collapse" : "▼ full output"}</button>
            )}
          </>
        )}
        {running && (
          <div style={{ fontSize: 10.5, color: "var(--c-muted)", marginTop: 6, fontStyle: "italic" }}>
            executing in isolated container…
          </div>
        )}
      </div>
    </div>
  );
}

export function OpsConsole({
  trace, running, onClose, accent = "var(--c-amber)",
}: {
  trace: OpsStep[];
  running: { action: string; args?: Record<string, unknown> } | null;
  onClose: () => void;
  accent?: string;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6, behavior: "smooth" });
  }, [trace.length, running]);

  // Show an in-flight placeholder step whenever a tool is running; the deck clears
  // `running` the moment the tool's agent_tool result arrives, so the real step replaces it.
  const showRunning = !!running;

  const opCount = trace.filter((s) => ["recon", "pentest", "scope", "browse"].includes(s.action)).length;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
      style={{
        position: "absolute", inset: 0, zIndex: 20, display: "flex", flexDirection: "column",
        background: "oklch(0.09 0.013 28 / 0.97)", backdropFilter: "blur(6px)",
        border: `1px solid ${accent}33`, borderRadius: 8, overflow: "hidden",
      }}
    >
      <div style={{
        display: "flex", alignItems: "center", gap: 10, padding: "11px 14px",
        borderBottom: `1px solid ${accent}22`, flexShrink: 0,
      }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: accent,
          boxShadow: `0 0 10px ${accent}`, animation: running ? "opsPulse 1s infinite" : "none" }} />
        <span style={{ fontSize: 12, letterSpacing: "0.26em", fontWeight: 700, color: accent }}>LIVE OPS CONSOLE</span>
        <span style={{ fontSize: 9, color: "var(--c-muted)", letterSpacing: "0.06em" }}>
          {opCount} operation{opCount === 1 ? "" : "s"} · ground truth
        </span>
        <button onClick={onClose} title="Back to conversation" className="no-drag" style={{
          marginLeft: "auto", background: "transparent", border: `1px solid ${accent}44`,
          color: accent, borderRadius: 5, padding: "3px 10px", cursor: "pointer",
          fontFamily: "var(--font-mono)", fontSize: 10, letterSpacing: "0.1em",
        }}>✕ CLOSE</button>
      </div>

      <div style={{ fontSize: 9.5, color: "var(--c-muted)", padding: "7px 14px 0", lineHeight: 1.4 }}>
        Real tool executions, live. If JARVIS claims it did something and there's no step here, it didn't happen.
      </div>

      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "14px 14px 8px" }}>
        {trace.length === 0 && !running && (
          <div style={{ color: "var(--c-muted)", fontSize: 12, textAlign: "center", marginTop: 40, lineHeight: 1.6 }}>
            No operations yet.<br />
            Ask JARVIS to <span style={{ color: accent }}>recon</span> a target or run a{" "}
            <span style={{ color: accent }}>pentest</span> — every step it actually runs shows here, live.
          </div>
        )}
        {trace.map((s, i) => <Step key={`${s.step}-${i}`} s={s} />)}
        {showRunning && <Step s={{ step: trace.length + 1, action: running!.action, args: running!.args || {}, observation: "" }} running />}
      </div>

      <style>{`@keyframes opsPulse { 0%,100%{opacity:1;} 50%{opacity:0.3;} }`}</style>
    </motion.div>
  );
}
