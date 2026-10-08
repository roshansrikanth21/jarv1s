// Compact microphone status and controls. The same component runs in the main
// window and the always-on-top desktop pill.
import { useEffect, useRef, useState } from "react";

type VoiceState =
  | "off"
  | "connecting"
  | "listening"
  | "heard"
  | "waiting"
  | "ready"
  | "hearing"
  | "transcribing"
  | "preparing"
  | "speaking"
  | "armed_wait"
  | "armed"
  | "accepted"
  | "unaddressed"
  | "not_heard"
  | "conversation"
  | "thinking"
  | "executing";

const BARS = 12;
const VOICE_STATES = new Set<VoiceState>([
  "off",
  "connecting",
  "listening",
  "heard",
  "hearing",
  "transcribing",
  "preparing",
  "speaking",
  "armed_wait",
  "armed",
  "accepted",
  "unaddressed",
  "not_heard",
  "conversation",
  "thinking",
  "executing",
]);

function isPillSurface() {
  return new URLSearchParams(window.location.search).get("surface") === "pill";
}

export function MicMonitor() {
  const dock = isPillSurface();
  const [state, setState] = useState<VoiceState>("connecting");
  const [wakeRequired, setWakeRequired] = useState(true);
  const [wakeWord, setWakeWord] = useState("jarvis");
  const [heard, setHeard] = useState("");
  // The backend's own wording for the current state ("Opening the search…", "Listening — go
  // ahead…"). Preferred over the generic labels below whenever it's present.
  const [detail, setDetail] = useState("");
  const [open, setOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const heardTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const statusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const energyRef = useRef(0);
  const threshRef = useRef(650);
  const hearingRef = useRef(false);
  const barsRef = useRef<HTMLDivElement | null>(null);
  const stateRef = useRef<VoiceState>("connecting");
  const utteranceRef = useRef(-1);
  const wakeRequiredRef = useRef(true);
  stateRef.current = state;
  wakeRequiredRef.current = wakeRequired;

  useEffect(() => {
    if (!dock) return;
    (
      window as Window & { electronAPI?: { resizePill?: (open: boolean) => void } }
    ).electronAPI?.resizePill?.(open);
  }, [dock, open]);

  useEffect(() => {
    if (!dock) return;
    const prev = document.body.style.background;
    document.documentElement.style.background = "transparent";
    document.body.style.background = "transparent";
    return () => {
      document.body.style.background = prev;
    };
  }, [dock]);

  useEffect(() => {
    let stop = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const url = () => `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;
    const connect = () => {
      if (stop) return;
      setConnected(false);
      setState("connecting");
      const ws = new WebSocket(url());
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onmessage = (e) => {
        let d: Record<string, unknown>;
        try {
          d = JSON.parse(e.data);
        } catch {
          return;
        }
        if (d.type === "audio_level") {
          const en = Number(d.energy) || 0;
          energyRef.current = energyRef.current * 0.55 + en * 0.45;
          if (d.thresh) threshRef.current = Number(d.thresh);
          hearingRef.current = Boolean(d.hearing);
        }
        if (
          d.type === "state" &&
          d.status === "speaking" &&
          stateRef.current !== "off" &&
          stateRef.current !== "connecting"
        ) {
          setState("speaking");
          setDetail("");
        }
        if (d.type === "mic") {
          const active = Boolean(d.listening);
          if (typeof d.wake_required === "boolean") {
            wakeRequiredRef.current = d.wake_required;
            setWakeRequired(d.wake_required);
          }
          if (typeof d.wake_word === "string" && d.wake_word.trim()) {
            setWakeWord(d.wake_word.trim().toLowerCase());
          }
          utteranceRef.current = -1;
          setState(active ? (d.wake_required === false ? "ready" : "waiting") : "off");
          setDetail("");
        }
        if (
          d.type === "voice" &&
          typeof d.state === "string" &&
          VOICE_STATES.has(d.state as VoiceState)
        ) {
          const id = typeof d.utterance_id === "number" ? d.utterance_id : null;
          if (id !== null && id < utteranceRef.current) return;
          if (id !== null && id > utteranceRef.current) utteranceRef.current = id;
          const next = d.state as VoiceState;
          if (next === "heard") {
            if (typeof d.text === "string") {
              setHeard(d.text);
              if (heardTimer.current) clearTimeout(heardTimer.current);
              heardTimer.current = setTimeout(() => setHeard(""), 12000);
            }
            return;
          }
          // Backend's generic "listening" means the mic is live and waiting for
          // speech. Make the wake-word requirement visible instead of saying only
          // "Listening".
          const visible =
            next === "listening" ? (wakeRequiredRef.current ? "waiting" : "ready") : next;
          if (["accepted", "unaddressed", "not_heard"].includes(visible) && statusTimer.current) {
            clearTimeout(statusTimer.current);
          }
          setState(visible as VoiceState);
          setDetail(typeof d.text === "string" ? d.text : "");
          if (["accepted", "unaddressed", "not_heard"].includes(visible)) {
            const seenId = id;
            statusTimer.current = setTimeout(() => {
              if (stateRef.current !== visible) return;
              if (seenId !== null && utteranceRef.current !== seenId) return;
              setState(wakeRequiredRef.current ? "waiting" : "ready");
            }, 5000);
          }
        }
        if ((d.type === "transcription" || d.type === "transcript") && d.text) {
          setHeard(String(d.text));
          if (heardTimer.current) clearTimeout(heardTimer.current);
          heardTimer.current = setTimeout(() => setHeard(""), 12000);
        }
      };
      ws.onclose = () => {
        setConnected(false);
        setState("connecting");
        setDetail("");
        if (!stop) retry = setTimeout(connect, 1500);
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      };
    };
    connect();

    let raf = 0;
    let last = 0;
    const paint = (now: number) => {
      raf = requestAnimationFrame(paint);
      if (document.hidden && !dock) return;
      if (now - last < 50) return;
      last = now;
      const el = barsRef.current;
      if (!el) return;
      const rel = Math.min(1, energyRef.current / Math.max(120, threshRef.current * 1.5));
      const over = hearingRef.current || energyRef.current > threshRef.current;
      const children = el.children;
      for (let i = 0; i < children.length; i++) {
        const b = children[i] as HTMLElement;
        const shape = 0.45 + 0.55 * Math.sin((i / (BARS - 1)) * Math.PI);
        const h = 6 + rel * 18 * shape;
        b.style.height = `${h}px`;
        b.style.background = over ? "#d4c8b0" : "#707a88";
      }
    };
    raf = requestAnimationFrame(paint);

    return () => {
      stop = true;
      if (retry) clearTimeout(retry);
      if (heardTimer.current) clearTimeout(heardTimer.current);
      if (statusTimer.current) clearTimeout(statusTimer.current);
      cancelAnimationFrame(raf);
      try {
        wsRef.current?.close();
      } catch {
        /* ignore */
      }
    };
  }, [dock]);

  const send = (payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
  };

  const wakePhrase = `Hey ${wakeWord.charAt(0).toUpperCase()}${wakeWord.slice(1)}`;
  const fallback =
    state === "conversation"
      ? "Listening — just keep talking"
      : state === "thinking"
        ? "Thinking…"
        : state === "executing"
          ? "Working on it…"
          : null;
  const label =
    state !== "off" && state !== "connecting" && detail
      ? detail
      : fallback
        ? fallback
        : state === "off"
          ? "Mic off"
          : state === "connecting"
            ? "Connecting…"
            : state === "waiting"
              ? `Waiting for “${wakePhrase}”`
              : state === "ready"
                ? "Ready — say something"
                : state === "hearing"
                  ? "Hearing you…"
                  : state === "transcribing"
                    ? "Checking what you said…"
                    : state === "preparing"
                      ? "Preparing reply audio…"
                      : state === "speaking"
                        ? "JARVIS speaking — say “Hey Jarvis” to interrupt"
                        : state === "armed_wait"
                          ? "Wait for my cue…"
                          : state === "armed"
                            ? "Your turn — speak now"
                            : state === "accepted"
                              ? "Request received"
                              : state === "unaddressed"
                                ? `Heard speech — say “${wakePhrase}”`
                                : "Didn't catch that — try again";
  const dot =
    state === "hearing"
      ? "#68d391"
      : state === "transcribing" ||
          state === "preparing" ||
          state === "armed_wait" ||
          state === "thinking" ||
          state === "executing"
        ? "#c8a050"
        : state === "accepted" || state === "armed" || state === "conversation"
          ? "#8ad7d2"
          : state === "off" || state === "connecting"
            ? "#626b78"
            : "#8a96a6";
  const isActive = state !== "off" && state !== "connecting";

  const ask = (text: string) => {
    send({ action: "command", text });
    setOpen(false);
    const api = (window as Window & { electronAPI?: { restoreWindow?: () => void } }).electronAPI;
    api?.restoreWindow?.();
  };

  const actions: { name: string; hint: string; run: () => void }[] = [
    {
      name: "Open",
      hint: "Bring the deck forward",
      run: () =>
        (
          window as Window & { electronAPI?: { restoreWindow?: () => void } }
        ).electronAPI?.restoreWindow?.(),
    },
    {
      name: "Weather",
      hint: "Ask for the local forecast",
      run: () => ask("what is the weather here"),
    },
    {
      name: "Status",
      hint: "CPU, memory, battery",
      run: () => ask("give me cpu, memory, and battery"),
    },
    {
      name: isActive ? "Pause microphone" : "Turn microphone on",
      hint: isActive ? "JARVIS will stop listening" : "JARVIS will wait for your voice",
      run: () => {
        send({ action: isActive ? "stop_listening" : "start_listening" });
        setState(isActive ? "off" : "connecting");
        setOpen(false);
      },
    },
  ];

  return (
    <div
      className="no-drag"
      style={{
        position: "fixed",
        top: 7,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 100001,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        fontFamily: "Segoe UI, sans-serif",
        pointerEvents: "auto",
      }}
    >
      <div
        style={{
          width: open ? 276 : undefined,
          maxWidth: "calc(100vw - 20px)",
          background: "rgba(8,10,14,0.94)",
          border: "1px solid rgba(255,255,255,0.14)",
          borderRadius: open ? 22 : 999,
          boxShadow: "0 10px 30px rgba(0,0,0,0.35)",
          backdropFilter: "blur(16px)",
          overflow: "hidden",
        }}
      >
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label={`JARVIS microphone: ${label}`}
          title={label}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            minHeight: 32,
            width: "100%",
            background: "transparent",
            border: "none",
            padding: "0 12px 0 10px",
            color: "rgba(232,236,240,0.96)",
            cursor: "pointer",
          }}
        >
          <span
            style={{ width: 7, height: 7, borderRadius: "50%", background: dot, flexShrink: 0 }}
          />
          <div
            ref={barsRef}
            style={{ display: "flex", alignItems: "center", gap: 2, height: 16, flexShrink: 0 }}
          >
            {Array.from({ length: BARS }).map((_, i) => (
              <span
                key={i}
                style={{
                  width: 2,
                  height: 4,
                  borderRadius: 1,
                  background: "rgba(232,236,240,0.35)",
                }}
              />
            ))}
          </div>
          <span
            style={{
              fontSize: 12,
              letterSpacing: "0.01em",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {label}
          </span>
        </button>
        {open && (
          <div style={{ padding: "7px 10px 9px" }}>
            <div
              style={{
                fontSize: 11,
                lineHeight: 1.45,
                color: "rgba(232,236,240,0.7)",
                padding: "0 4px 7px",
              }}
            >
              {state === "off"
                ? "The microphone is off."
                : !connected
                  ? "Connecting to JARVIS…"
                  : state === "speaking"
                    ? "JARVIS is speaking. Say “Hey Jarvis” to interrupt."
                    : state === "conversation"
                      ? "Conversation mode — keep talking, no wake word needed. Say “that's all” to end it."
                      : state === "preparing"
                        ? "JARVIS is preparing speech. Say the wake phrase if you need to interrupt."
                        : wakeRequired
                          ? `Say “${wakePhrase}” and your request together. Or say the wake phrase, wait for “Your turn,” then speak.`
                          : "The microphone is on. Say your request whenever the status says “Ready.”"}
              {heard && (
                <div style={{ marginTop: 5, color: "rgba(232,236,240,0.9)" }}>
                  Last heard: “{heard}”
                </div>
              )}
            </div>
            {actions.map((item) => (
              <button
                key={item.name}
                type="button"
                onClick={item.run}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "flex-start",
                  width: "100%",
                  background: "transparent",
                  color: "rgba(232,236,240,0.92)",
                  border: "none",
                  borderRadius: 12,
                  padding: "7px 10px",
                  cursor: "pointer",
                  textAlign: "left",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "rgba(255,255,255,0.06)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                }}
              >
                <span style={{ fontSize: 13 }}>{item.name}</span>
                <span style={{ fontSize: 11, color: "rgba(232,236,240,0.48)", marginTop: 1 }}>
                  {item.hint}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
