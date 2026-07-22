// LiveOps — a live, deck-agnostic feed of JARVIS's REAL tool executions. It opens its own
// WebSocket to the backend and renders one card per `agent_tool` broadcast: the tool name,
// the exact arguments, a running→done/failed status, and the actual returned output.
//
// Why it exists: the LLM can *narrate* actions it never took (fake scans, fake logs, fake
// findings). This panel is the ground truth — it only shows tool calls the backend actually
// ran. If JARVIS claims it scanned something and no card appears here, it didn't happen.
import { useEffect, useRef, useState } from "react";

type ToolEvent = {
  id: string;
  step: number;
  action: string;
  args: Record<string, unknown>;
  observation: string;
  status: "running" | "done" | "failed";
  at: number;
};

const uid = () => Math.random().toString(36).slice(2);
const ACCENT = "#41ff9e";
const FAIL = "#ff6b6b";

function looksFailed(obs: string): boolean {
  return /^(⛔|refused|error|tool .* failed|docker isn't|not a valid|unavailable|invalid|no output)/i.test(
    (obs || "").trim(),
  );
}

export function LiveOps() {
  const [events, setEvents] = useState<ToolEvent[]>([]);
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [unseen, setUnseen] = useState(0);
  const listRef = useRef<HTMLDivElement | null>(null);
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stop = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const wsUrl = () =>
      `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;

    // Capped exponential backoff + jitter, and NEVER reconnect while the window is hidden.
    // The old flat 1500ms retry (no backoff, no visibility guard) hammered the backend forever
    // when it was down — even in the background — which was a real CPU/console-spam offender.
    const scheduleRetry = () => {
      if (stop || retry) return;
      if (document.hidden) return; // resumed by the visibilitychange handler below
      const base = Math.min(1000 * 2 ** Math.min(attempt, 5), 30000);
      const delay = Math.floor(base * (0.5 + Math.random() * 0.5));
      retry = setTimeout(() => {
        retry = null;
        attempt += 1;
        connect();
      }, delay);
    };

    const connect = () => {
      if (stop || document.hidden) return;
      // Drop any prior socket so reconnect can't stack duplicate handlers.
      if (ws) {
        try {
          ws.onclose = null;
          ws.onerror = null;
          ws.onmessage = null;
          ws.close();
        } catch {
          /* ignore */
        }
        ws = null;
      }
      const sock = new WebSocket(wsUrl());
      ws = sock;
      sock.onopen = () => {
        if (ws === sock) attempt = 0; // reset backoff on a successful connect
      };
      sock.onmessage = (ev) => {
        if (ws !== sock) return;
        let d: Record<string, unknown>;
        try {
          d = JSON.parse(ev.data);
        } catch {
          return;
        }

        // A tool started (backend emits "Running <name>..." before it runs).
        if (d.type === "state" && d.status === "thinking" && typeof d.text === "string") {
          const m = /Running\s+([a-z_]+)/i.exec(d.text);
          if (m) {
            const action = m[1];
            setEvents((p) => {
              if (p.some((e) => e.status === "running" && e.action === action)) return p;
              return [
                ...p,
                {
                  id: uid(),
                  step: p.length + 1,
                  action,
                  args: {},
                  observation: "",
                  status: "running",
                  at: Date.now(),
                },
              ];
            });
          }
        }

        // A tool finished — this is the ground-truth record (name, args, real output).
        if (d.type === "agent_tool" && d.step) {
          const s = d.step as {
            step: number;
            action: string;
            args: Record<string, unknown>;
            observation: string;
          };
          setEvents((p) => {
            const failed = looksFailed(s.observation);
            const running = [...p]
              .reverse()
              .find((e) => e.status === "running" && e.action === s.action);
            const card: ToolEvent = {
              id: running?.id ?? uid(),
              step: s.step,
              action: s.action,
              args: s.args || {},
              observation: s.observation || "",
              status: failed ? "failed" : "done",
              at: Date.now(),
            };
            const next = running ? p.map((e) => (e.id === running.id ? card : e)) : [...p, card];
            return next.slice(-40);
          });
          if (!openRef.current) setUnseen((n) => n + 1);
        }
      };
      sock.onclose = () => {
        if (!stop && ws === sock) scheduleRetry();
      };
      sock.onerror = () => {
        try {
          sock.close();
        } catch {
          /* ignore */
        }
      };
    };

    // When the window becomes visible again, reconnect immediately (reset backoff) if needed.
    const onVisible = () => {
      if (stop || document.hidden) return;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
      if (retry) {
        clearTimeout(retry);
        retry = null;
      }
      attempt = 0;
      connect();
    };
    document.addEventListener("visibilitychange", onVisible);

    connect();
    return () => {
      stop = true;
      document.removeEventListener("visibilitychange", onVisible);
      if (retry) clearTimeout(retry);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
  }, []);

  useEffect(() => {
    if (open) {
      setUnseen(0);
      listRef.current?.scrollTo({ top: 9e6, behavior: "smooth" });
    }
  }, [events.length, open]);

  return (
    <>
      {/* toggle tab — always visible, right edge */}
      <button
        className="no-drag"
        onClick={() => setOpen((o) => !o)}
        title="Live tool activity (ground truth)"
        style={{
          position: "fixed",
          right: open ? 372 : 0,
          top: "42%",
          zIndex: 100000,
          transform: "translateY(-50%)",
          transition: "right 0.25s ease",
          background: "rgba(6,12,10,0.95)",
          color: ACCENT,
          border: `1px solid ${ACCENT}55`,
          borderRight: open ? "none" : undefined,
          borderRadius: open ? "8px 0 0 8px" : "8px 0 0 8px",
          padding: "10px 7px",
          cursor: "pointer",
          fontFamily: "'JetBrains Mono', ui-monospace, monospace",
          fontSize: 9.5,
          letterSpacing: "0.2em",
          writingMode: "vertical-rl",
          textOrientation: "mixed",
          boxShadow: `0 0 18px ${ACCENT}22`,
        }}
      >
        ● LIVE OPS{unseen > 0 && !open ? ` (${unseen})` : ""}
      </button>

      {/* drawer */}
      <div
        className="no-drag"
        style={{
          position: "fixed",
          top: 0,
          bottom: 0,
          right: open ? 0 : -380,
          width: 372,
          zIndex: 99999,
          transition: "right 0.25s ease",
          background: "rgba(5,9,8,0.97)",
          borderLeft: `1px solid ${ACCENT}33`,
          backdropFilter: "blur(10px)",
          display: "flex",
          flexDirection: "column",
          fontFamily: "'JetBrains Mono', ui-monospace, monospace",
          color: "#dfeee7",
          boxShadow: "-8px 0 40px rgba(0,0,0,0.5)",
        }}
      >
        <div style={{ padding: "14px 14px 10px", borderBottom: `1px solid ${ACCENT}22` }}>
          <div style={{ fontSize: 12, letterSpacing: "0.28em", color: ACCENT, fontWeight: 700 }}>
            ● LIVE OPS
          </div>
          <div style={{ fontSize: 9.5, color: "#7fa094", marginTop: 5, lineHeight: 1.45 }}>
            Real tool executions only. If JARVIS claims it did something and there's no card here,
            it didn't happen.
          </div>
        </div>

        <div
          ref={listRef}
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "10px 12px",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          {events.length === 0 && (
            <div style={{ fontSize: 11, color: "#5c7268", textAlign: "center", marginTop: 30 }}>
              No tool activity yet. Ask JARVIS to recon or scan something and watch it run here,
              live.
            </div>
          )}
          {events.map((e) => {
            const col = e.status === "failed" ? FAIL : e.status === "running" ? "#ffd24a" : ACCENT;
            const isOpen = expanded[e.id];
            const obs = e.observation || "";
            const argStr = Object.entries(e.args)
              .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
              .join("  ·  ");
            return (
              <div
                key={e.id}
                style={{
                  border: `1px solid ${col}33`,
                  borderRadius: 7,
                  background: "rgba(255,255,255,0.02)",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "8px 10px",
                    background: `${col}12`,
                  }}
                >
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      background: col,
                      boxShadow: `0 0 6px ${col}`,
                      animation: e.status === "running" ? "liveopsPulse 1s infinite" : "none",
                    }}
                  />
                  <span
                    style={{ fontSize: 11.5, fontWeight: 700, color: col, letterSpacing: "0.04em" }}
                  >
                    {e.action}
                  </span>
                  <span
                    style={{
                      marginLeft: "auto",
                      fontSize: 8.5,
                      letterSpacing: "0.14em",
                      color: col,
                      textTransform: "uppercase",
                    }}
                  >
                    {e.status === "running" ? "running…" : e.status}
                  </span>
                </div>
                {argStr && (
                  <div
                    style={{
                      padding: "6px 10px 0",
                      fontSize: 10,
                      color: "#9fc2b6",
                      wordBreak: "break-word",
                    }}
                  >
                    {argStr}
                  </div>
                )}
                {obs && (
                  <>
                    <pre
                      style={{
                        margin: "6px 10px 4px",
                        fontSize: 10,
                        lineHeight: 1.45,
                        color: "#c8ddd4",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                        maxHeight: isOpen ? "none" : 84,
                        overflow: "hidden",
                        fontFamily: "inherit",
                      }}
                    >
                      {obs}
                    </pre>
                    {obs.length > 180 && (
                      <button
                        onClick={() => setExpanded((x) => ({ ...x, [e.id]: !isOpen }))}
                        style={{
                          background: "transparent",
                          border: "none",
                          color: col,
                          cursor: "pointer",
                          fontSize: 9.5,
                          letterSpacing: "0.1em",
                          padding: "0 10px 8px",
                        }}
                      >
                        {isOpen ? "▲ collapse" : "▼ show full output"}
                      </button>
                    )}
                  </>
                )}
              </div>
            );
          })}
        </div>
        <style>{`@keyframes liveopsPulse { 0%,100%{opacity:1;} 50%{opacity:0.3;} }`}</style>
      </div>
    </>
  );
}
