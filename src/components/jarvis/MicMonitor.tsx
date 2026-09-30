// Compact voice pill. Inside the main window it sits at the bottom edge.
// When Electron minimizes the deck, a separate always-on-top window loads
// ?surface=pill so the same control stays on the desktop.
import { useEffect, useRef, useState } from "react";

type VoiceState = "off" | "listening" | "hearing" | "transcribing";

const BARS = 12;

function isPillSurface() {
  return new URLSearchParams(window.location.search).get("surface") === "pill";
}

export function MicMonitor() {
  const dock = isPillSurface();
  const [state, setState] = useState<VoiceState>(dock ? "listening" : "off");
  const [heard, setHeard] = useState("");
  const [open, setOpen] = useState(false);
  const heardTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const energyRef = useRef(0);
  const threshRef = useRef(650);
  const barsRef = useRef<HTMLDivElement | null>(null);
  const stateRef = useRef<VoiceState>("off");
  stateRef.current = state;

  useEffect(() => {
    if (!dock) return;
    (window as Window & { electronAPI?: { resizePill?: (open: boolean) => void } }).electronAPI?.resizePill?.(open);
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
      const ws = new WebSocket(url());
      wsRef.current = ws;
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
          if (stateRef.current === "off") setState("listening");
        }
        if (d.type === "voice" && typeof d.state === "string") {
          setState(d.state as VoiceState);
        }
        if ((d.type === "transcription" || d.type === "transcript") && d.text) {
          setHeard(String(d.text));
          setState("listening");
          if (heardTimer.current) clearTimeout(heardTimer.current);
          heardTimer.current = setTimeout(() => setHeard(""), 8000);
        }
      };
      ws.onclose = () => {
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
      const over = energyRef.current > threshRef.current;
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

  if (!dock && state === "off") return null;

  const label =
    state === "hearing"
      ? "Hearing you"
      : state === "transcribing"
        ? "Transcribing"
        : state === "off"
          ? "Mic off"
          : "Listening";
  const dot = state === "hearing" ? "#d4c8b0" : state === "transcribing" ? "#c8a050" : "#8a96a6";

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
        (window as Window & { electronAPI?: { restoreWindow?: () => void } }).electronAPI?.restoreWindow?.(),
    },
    { name: "Weather", hint: "Ask for the local forecast", run: () => ask("what is the weather here") },
    { name: "Status", hint: "CPU, memory, battery", run: () => ask("give me cpu, memory, and battery") },
    {
      name: state === "off" ? "Listen" : "Pause",
      hint: state === "off" ? "Turn the microphone on" : "Stop listening",
      run: () => {
        send({ action: state === "off" ? "start_listening" : "stop_listening" });
        setState(state === "off" ? "listening" : "off");
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
          width: open ? 232 : undefined,
          background: "rgba(8,10,14,0.92)",
          border: "1px solid rgba(255,255,255,0.12)",
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
          aria-label="JARVIS voice"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            height: 28,
            width: "100%",
            background: "transparent",
            border: "none",
            padding: "0 12px 0 10px",
            color: "rgba(232,236,240,0.92)",
            cursor: "pointer",
          }}
        >
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: dot, flexShrink: 0 }} />
          <div ref={barsRef} style={{ display: "flex", alignItems: "center", gap: 2, height: 16 }}>
            {Array.from({ length: BARS }).map((_, i) => (
              <span key={i} style={{ width: 2, height: 4, borderRadius: 1, background: "rgba(232,236,240,0.35)" }} />
            ))}
          </div>
          <span style={{ fontSize: 12, letterSpacing: "0.01em", whiteSpace: "nowrap" }}>
            {heard || label}
          </span>
        </button>
        {open && (
          <div style={{ padding: "2px 6px 8px" }}>
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
                <span style={{ fontSize: 11, color: "rgba(232,236,240,0.45)", marginTop: 1 }}>{item.hint}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
