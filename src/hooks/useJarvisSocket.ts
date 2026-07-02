// Shared JARVIS communication layer — the canonical WebSocket lifecycle, message
// routing, TTS playback, reconnect, and command/mic actions used by UI presets.
// A preset consumes this hook and renders; it must not re-implement the protocol.
// (The original classic/overhaul decks predate this hook and are intentionally
// left as-is; new presets like Focus build on it.)
import { useCallback, useEffect, useRef, useState } from "react";

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
  send: (text: string) => void;
  toggleMic: () => void;
  addLine: (role: Role, text: string) => void;
};

export function useJarvisSocket(greeting = "JARVIS online."): JarvisSocket {
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking]   = useState(false);
  const [lines, setLines]         = useState<Line[]>([{ id: uid(), role: "system", text: greeting }]);
  const [stream, setStream]       = useState("");
  const [mood, setMood]           = useState<Mood>(null);
  const [level, setLevel]         = useState(0);

  const wsRef    = useRef<WebSocket | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const addRef   = useRef<(r: Role, t: string) => void>(null!);
  const connRef  = useRef<() => void>(null!);

  const add = useCallback((role: Role, text: string) => {
    if (!text.trim()) return;
    setLines(p => [...p.slice(-120), { id: uid(), role, text }]);
  }, []);
  addRef.current = add;

  // Decode + play a base64 MP3 TTS chunk; report the exact playback window to the
  // backend (tts_start/tts_end) so it can mute the mic and avoid a feedback loop.
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
    a.play().then(() => wsRef.current?.send(JSON.stringify({ action: "tts_start" }))).catch(end);
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(`ws://${window.location.host}/ws`);
    wsRef.current = ws;
    ws.onopen = () => {
      setConnected(true);
      fetch("/api/agent/status").then(r => r.json()).then(d => { if (d?.emotion) setMood(d.emotion); }).catch(() => {});
    };
    ws.onclose = () => { setConnected(false); setListening(false); setSpeaking(false); };
    ws.onerror = () => { setTimeout(() => connRef.current(), 5000); };
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        const txt: string = d.text ?? d.message ?? "";
        if (d.type === "state" || d.type === "status") setSpeaking(d.status === "speaking");
        if (d.type === "emotion" && d.emotion) setMood(d.emotion);
        if (d.type === "transcription" || d.type === "transcript") addRef.current("user", txt);
        if (d.type === "llm_chunk" && d.text) setStream(p => p + (d.text as string));
        if (d.type === "llm_response" || d.type === "response") { setStream(""); addRef.current("agent", txt); }
        if (d.type === "tts_audio" && d.data) playTts(d.data as string);
        if (d.type === "tts_stop") {
          if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
          setSpeaking(false);
          wsRef.current?.send(JSON.stringify({ action: "tts_end" }));
        }
        if (d.type === "audio_level") setLevel(Number(d.level) || 0);
      } catch { /* ignore malformed packet */ }
    };
  }, [playTts]);
  connRef.current = connect;

  useEffect(() => {
    connect();
    return () => {
      if (audioRef.current) audioRef.current.pause();
      wsRef.current?.close();
    };
  }, [connect]);

  const send = useCallback((text: string) => {
    const t = text.trim();
    if (!t) return;
    add("user", t);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: "command", text: t }));
    } else {
      fetch("/api/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command: t }) })
        .then(r => r.json()).then(d => add("agent", d.response ?? "Done.")).catch(() => add("system", "No backend connection."));
    }
  }, [add]);

  const toggleMic = useCallback(() => {
    if (!connected) { connRef.current(); return; }
    const next = !listening;
    setListening(next);
    wsRef.current?.send(JSON.stringify({ action: next ? "start_listening" : "stop_listening" }));
  }, [connected, listening]);

  return { connected, listening, speaking, lines, stream, mood, level, send, toggleMic, addLine: add };
}
