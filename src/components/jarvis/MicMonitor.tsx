// MicMonitor — an always-visible, deck-agnostic voice-input indicator. It opens its own
// WebSocket and reacts LIVE to the mic: a waveform that moves with your voice, a status
// ("Listening" → "Hearing you" → "Transcribing"), and the last thing it heard. The whole
// point: you can SEE whether the mic is picking you up, instead of guessing.
import { useEffect, useRef, useState } from "react";

type VoiceState = "off" | "listening" | "hearing" | "transcribing";

const BARS = 16;

export function MicMonitor() {
  const [state, setState] = useState<VoiceState>("off");
  const [heard, setHeard] = useState("");
  const heardTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // live audio values live in refs; a rAF loop paints the bars so it stays smooth
  const energyRef = useRef(0);
  const threshRef = useRef(650);
  const hearingRef = useRef(false);
  const barsRef = useRef<HTMLDivElement | null>(null);
  const stateRef = useRef<VoiceState>("off");
  stateRef.current = state;

  useEffect(() => {
    let ws: WebSocket | null = null, stop = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const url = () => `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;
    const connect = () => {
      if (stop) return;
      ws = new WebSocket(url());
      ws.onmessage = (e) => {
        let d: Record<string, unknown>;
        try { d = JSON.parse(e.data); } catch { return; }
        if (d.type === "audio_level") {
          const en = Number(d.energy) || 0;
          energyRef.current = energyRef.current * 0.55 + en * 0.45; // smooth the jitter
          if (d.thresh) threshRef.current = Number(d.thresh);
          hearingRef.current = !!d.hearing;
          if (stateRef.current === "off") setState("listening");
        }
        if (d.type === "voice" && typeof d.state === "string") {
          setState(d.state as VoiceState);
          if (d.thresh) threshRef.current = Number(d.thresh);
        }
        if ((d.type === "transcription" || d.type === "transcript") && d.text) {
          setHeard(String(d.text));
          setState("listening");
          if (heardTimer.current) clearTimeout(heardTimer.current);
          heardTimer.current = setTimeout(() => setHeard(""), 7000);
        }
      };
      ws.onclose = () => { if (!stop) retry = setTimeout(connect, 1500); };
      ws.onerror = () => { try { ws?.close(); } catch { /* ignore */ } };
    };
    connect();

    // paint loop — waveform bars react to the live energy relative to the trigger threshold
    let raf = 0;
    const bars = barsRef.current;
    const paint = () => {
      const rel = Math.min(1, energyRef.current / Math.max(120, threshRef.current * 1.5));
      const over = energyRef.current > threshRef.current;
      const el = barsRef.current || bars;
      if (el) {
        const children = el.children;
        for (let i = 0; i < children.length; i++) {
          const b = children[i] as HTMLElement;
          // per-bar shape (center taller) + a little life so it reads as a waveform
          const shape = 0.45 + 0.55 * Math.sin((i / (BARS - 1)) * Math.PI);
          const jitter = 0.7 + 0.3 * Math.random();
          const h = 8 + rel * 30 * shape * jitter;
          b.style.height = `${h}px`;
          b.style.background = over ? "#41ff9e" : rel > 0.15 ? "#5fdcff" : "#3a5566";
          b.style.opacity = String(0.35 + rel * 0.65);
        }
      }
      raf = requestAnimationFrame(paint);
    };
    raf = requestAnimationFrame(paint);

    return () => {
      stop = true;
      if (retry) clearTimeout(retry);
      if (heardTimer.current) clearTimeout(heardTimer.current);
      cancelAnimationFrame(raf);
      try { ws?.close(); } catch { /* ignore */ }
    };
  }, []);

  if (state === "off") return null;

  const label =
    state === "hearing" ? "Hearing you…" :
    state === "transcribing" ? "Transcribing…" : "Listening — say “Jarvis”";
  const dot =
    state === "hearing" ? "#41ff9e" :
    state === "transcribing" ? "#ffd24a" : "#5fdcff";

  return (
    <div
      className="no-drag"
      style={{
        position: "fixed", top: 12, left: "50%", transform: "translateX(-50%)", zIndex: 100001,
        display: "flex", alignItems: "center", gap: 12,
        background: "rgba(6,12,18,0.92)", border: `1px solid ${dot}44`, borderRadius: 999,
        padding: "7px 16px 7px 12px", backdropFilter: "blur(10px)",
        boxShadow: `0 4px 24px rgba(0,0,0,0.45), 0 0 16px ${dot}22`,
        fontFamily: "'JetBrains Mono', ui-monospace, monospace", maxWidth: "min(92vw, 620px)",
      }}
    >
      {/* status dot */}
      <span style={{
        width: 9, height: 9, borderRadius: "50%", background: dot, flexShrink: 0,
        boxShadow: `0 0 8px ${dot}`,
        animation: state !== "listening" ? "micPulse 0.9s ease-in-out infinite" : "none",
      }} />
      {/* live waveform */}
      <div ref={barsRef} style={{ display: "flex", alignItems: "center", gap: 2, height: 40, flexShrink: 0 }}>
        {Array.from({ length: BARS }).map((_, i) => (
          <span key={i} style={{ width: 3, height: 8, borderRadius: 2, background: "#3a5566",
            transition: "height 0.06s linear, background 0.1s linear" }} />
        ))}
      </div>
      {/* label + last heard */}
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column", lineHeight: 1.2 }}>
        <span style={{ fontSize: 11, letterSpacing: "0.06em", color: dot, whiteSpace: "nowrap" }}>{label}</span>
        {heard && (
          <span style={{ fontSize: 10.5, color: "#c8ddd4", opacity: 0.85, overflow: "hidden",
            textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 320 }}>
            heard: “{heard}”
          </span>
        )}
      </div>
      <style>{`@keyframes micPulse { 0%,100%{ transform:scale(1); opacity:1; } 50%{ transform:scale(1.5); opacity:0.5; } }`}</style>
    </div>
  );
}
