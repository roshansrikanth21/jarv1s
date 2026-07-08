// Shared JARVIS communication layer — the canonical WebSocket lifecycle, message
// routing, TTS playback, reconnect, and command/mic actions used by UI presets.
// A preset consumes this hook and renders; it must not re-implement the protocol.
// (The original classic/overhaul decks predate this hook and are intentionally
// left as-is; new presets like Focus build on it.)
import { useCallback, useEffect, useRef, useState } from "react";
import { notifyNative } from "@/lib/utils";

export type Role = "user" | "agent" | "system";
export type Line = { id: string; role: Role; text: string };
export type Mood = { enabled?: boolean; emotion?: string; colour?: string; intensity?: number } | null;

const uid = () => Math.random().toString(36).slice(2);

export type JarvisSocket = {
  connected: boolean;
  listening: boolean;
  speaking: boolean;
  lines: Line[];
  stream: string;      // in-progress streamed assistant text (before the final line)
  mood: Mood;
  level: number;       // mic input level (0..32767) during listening
  /** True only once a disconnect has lasted past a short grace period — lets
   *  the UI show a calm "waking up" state instead of flashing an error on
   *  every brief, self-healing blip. Never mention backend/WebSocket/retry. */
  showReconnectHint: boolean;
  send: (text: string) => void;
  toggleMic: () => void;
  addLine: (role: Role, text: string) => void;
  /** Send any protocol action (set_mode, pull_model, trigger_sleep, …). */
  sendAction: (action: string, payload?: Record<string, unknown>) => void;
  /** Tap the raw message stream (governor_decision, model_pull, …) beyond the
   *  core routing above. Returns an unsubscribe function. */
  subscribe: (handler: (msg: Record<string, unknown>) => void) => () => void;
};

export function useJarvisSocket(greeting = "JARVIS online."): JarvisSocket {
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking]   = useState(false);
  const [lines, setLines]         = useState<Line[]>([{ id: uid(), role: "system", text: greeting }]);
  const [stream, setStream]       = useState("");
  const [mood, setMood]           = useState<Mood>(null);
  const [level, setLevel]         = useState(0);
  const [showReconnectHint, setShowReconnectHint] = useState(false);
  const reconnectHintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const wsRef    = useRef<WebSocket | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const addRef   = useRef<(r: Role, t: string) => void>(null!);
  const connRef  = useRef<() => void>(null!);
  const tapsRef  = useRef<Set<(msg: Record<string, unknown>) => void>>(new Set());
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const manualCloseRef = useRef(false);
  const pendingActionsRef = useRef<{ action: string; payload?: Record<string, unknown> }[]>([]);
  const pendingCommandsRef = useRef<string[]>([]);

  const flushPendingActions = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    while (pendingActionsRef.current.length) {
      const item = pendingActionsRef.current.shift();
      if (!item) break;
      ws.send(JSON.stringify({ action: item.action, ...(item.payload ?? {}) }));
    }
    while (pendingCommandsRef.current.length) {
      const text = pendingCommandsRef.current.shift();
      if (!text) break;
      ws.send(JSON.stringify({ action: "command", text }));
    }
  }, []);

  const wsUrl = () =>
    `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;

  const scheduleReconnect = useCallback((reason: string) => {
    if (manualCloseRef.current) return;
    if (reconnectTimerRef.current) return;
    const attempt = reconnectAttemptRef.current;
    if (attempt >= 12) return;
    const base = Math.min(1000 * 2 ** attempt, 30000);
    const delay = Math.floor(base * (0.5 + Math.random() * 0.5));
    void reason;
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      reconnectAttemptRef.current += 1;
      connRef.current();
    }, delay);
  }, []);

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
    a.play()
      .then(() => wsRef.current?.send(JSON.stringify({ action: "tts_start" })))
      .catch(() => {
        addRef.current(
          "system",
          "Audio blocked by the browser — click the orb or send a message first, then try again.",
        );
        end();
      });
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    if (wsRef.current?.readyState === WebSocket.CONNECTING) return;
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => {
      reconnectAttemptRef.current = 0;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      setConnected(true);
      flushPendingActions();
      fetch("/api/agent/status").then(r => r.json()).then(d => { if (d?.emotion) setMood(d.emotion); }).catch(() => {});
    };
    ws.onclose = (ev) => {
      setConnected(false);
      setListening(false);
      setSpeaking(false);
      if (!manualCloseRef.current && ev.code !== 1000) scheduleReconnect("onclose");
    };
    ws.onerror = () => {
      scheduleReconnect("onerror");
    };
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        const txt: string = d.text ?? d.message ?? "";
        if (d.type === "state" || d.type === "status") {
          if (d.status === "speaking") setSpeaking(true);
          // Backend emits idle as soon as audio is queued — keep orb in speaking
          // until the browser actually finishes playback (playTts / tts_stop).
          else if (!audioRef.current) setSpeaking(false);
        }
        if (d.type === "emotion" && d.emotion) setMood(d.emotion);
        if (d.type === "transcription" || d.type === "transcript") addRef.current("user", txt);
        if (d.type === "llm_chunk" && d.text) setStream(p => p + (d.text as string));
        if (d.type === "llm_response" || d.type === "response") { setStream(""); addRef.current("agent", txt); }
        if (d.type === "tts_audio" && d.data) playTts(d.data as string);
        if ((d.type === "system" || d.type === "tts_error") && txt.trim()) {
          addRef.current("system", txt);
        }
        if (d.type === "tts_stop") {
          if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
          setSpeaking(false);
          setStream("");
          wsRef.current?.send(JSON.stringify({ action: "tts_end" }));
        }
        if (d.type === "open_trading") {
          window.electronAPI?.openTrading?.()
            .then((r: { ok?: boolean; error?: string } | undefined) => {
              if (r && r.ok === false && r.error) addRef.current("system", r.error);
            })
            .catch(() => addRef.current("system", "Could not open the trading terminal."));
        }
        // Authoritative mic state from the backend — the single source of truth. Fixes the
        // ALWAYS_LISTEN desync (UI showing "tap to speak" while the mic was already hot) and
        // reconciles the optimistic toggle below if it ever guessed wrong.
        if (d.type === "mic") setListening(Boolean(d.listening));
        if (d.type === "audio_level") setLevel(Number(d.level) || 0);
        if (d.type === "system_alert" && txt.trim()) notifyNative("JARVIS", txt);
        tapsRef.current.forEach(fn => { try { fn(d); } catch { /* tap error is not ours */ } });
      } catch { /* ignore malformed packet */ }
    };
  }, [playTts, scheduleReconnect, flushPendingActions]);
  connRef.current = connect;

  useEffect(() => {
    manualCloseRef.current = false;
    connect();
    return () => {
      manualCloseRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (audioRef.current) audioRef.current.pause();
      wsRef.current?.close(1000);
    };
  }, [connect]);

  // A disconnect only becomes user-visible after a short grace period — most
  // reconnects (a brief blip, a backend restart after saving a key) resolve
  // well within it, so the UI never has to flash an error for something the
  // app already recovers from on its own.
  useEffect(() => {
    if (connected) {
      setShowReconnectHint(false);
      if (reconnectHintTimerRef.current) { clearTimeout(reconnectHintTimerRef.current); reconnectHintTimerRef.current = null; }
      return;
    }
    reconnectHintTimerRef.current = setTimeout(() => setShowReconnectHint(true), 2500);
    return () => {
      if (reconnectHintTimerRef.current) { clearTimeout(reconnectHintTimerRef.current); reconnectHintTimerRef.current = null; }
    };
  }, [connected]);

  const send = useCallback((text: string) => {
    const t = text.trim();
    if (!t) return;
    add("user", t);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: "command", text: t }));
      return;
    }
    if (pendingCommandsRef.current.length < 8) {
      pendingCommandsRef.current.push(t);
    }
    add("system", "Got it — one moment, then I'll reply.");
    connRef.current();
  }, [add]);

  const toggleMic = useCallback(() => {
    if (!connected) { connRef.current(); return; }
    // While JARVIS is speaking, tapping the mic/orb means "stop talking," not
    // "start listening over you" (which would just feed the echo guard) — this
    // surfaces the backend's dedicated interrupt action without new UI.
    if (speaking) {
      wsRef.current?.send(JSON.stringify({ action: "stop" }));
      return;
    }
    const next = !listening;
    setListening(next);
    wsRef.current?.send(JSON.stringify({ action: next ? "start_listening" : "stop_listening" }));
  }, [connected, listening, speaking]);

  const sendAction = useCallback((action: string, payload?: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action, ...(payload ?? {}) }));
      return;
    }
    if (pendingActionsRef.current.length < 24) {
      pendingActionsRef.current.push({ action, payload });
    }
    connRef.current();
  }, []);

  const subscribe = useCallback((handler: (msg: Record<string, unknown>) => void) => {
    tapsRef.current.add(handler);
    return () => { tapsRef.current.delete(handler); };
  }, []);

  return { connected, listening, speaking, lines, stream, mood, level, showReconnectHint, send, toggleMic, addLine: add, sendAction, subscribe };
}
