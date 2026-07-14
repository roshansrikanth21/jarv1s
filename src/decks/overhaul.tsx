import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import {
  Activity,
  Brain,
  CandlestickChart,
  CheckCircle2,
  ChevronRight,
  Clock,
  CopyCheck,
  Cpu,
  Database,
  Eye,
  HelpCircle,
  Lock,
  LineChart,
  Maximize2,
  Mic,
  MicOff,
  Minimize2,
  Minus,
  Radio,
  Send,
  Server,
  Settings,
  Terminal,
  Trash2,
  UserRound,
  Volume2,
  Wrench,
  X,
  Zap,
  ListTodo,
  GitBranch,
  Boxes,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArcReactor } from "@/components/jarvis/ArcReactor";
import { HudAmbient } from "@/components/jarvis/HudAmbient";
import { ShaderBackdrop } from "@/components/jarvis/ShaderBackdrop";
import { OpsConsole } from "@/components/jarvis/OpsConsole";
import { notifyNative } from "@/lib/utils";

// Rendered as a UI preset by src/routes/index.tsx (not a standalone route).

// ── Types ─────────────────────────────────────────────────
type Tone = "online" | "warn" | "idle";
type LineRole = "user" | "agent" | "system" | "tool";
type Line = { id: string; role: LineRole; text: string; at: string };
type Task = { id: number; t: string; eta?: string; status: "queued" | "active" | "done"; at?: string };
type ToolInfo = { name: string; description: string };
type AgentTrace = { step: number; action: string; args: Record<string, unknown>; observation: string };
type AgentStatus = {
  brain?: { primary_llm: string; local_model: string; reasoning?: string; max_agent_steps: number };
  conversation?: { turns: number };
  emotion?: { enabled: boolean; emotion: string; colour: string; intensity: number; sarcasm: string };
  council?: { panel: string[]; chair: string };
  voice?: { current: string; options: { id: string; label: string }[] };
  user?: { name: string; onboarded: boolean };
  watch?: { watching: boolean; watchlist: string[]; interval_min: number; tf: string };
  memory?: { available: boolean; count: number };
  governor?: { mode: string; available: string[]; metrics: { distribution: Record<string, number>; avg_latency_s: number | null; decisions: number } };
  homeostasis?: Homeostasis | null;
  device_tier?: string;
  local?: { enabled: boolean; fast: string; deep: string };
  tools?: ToolInfo[];
  tasks?: Task[];
  trace?: AgentTrace[];
};
type CouncilState = { active: boolean; panel: string[]; proposals: { model: string; text: string }[]; verdict: string };
type TradePlan = { side: string; entry?: number; sl?: number; tp?: number; rr?: number; text: string };
type IctRead = {
  ok: boolean; error?: string; netblock?: boolean;
  symbol?: string; tv?: string; interval?: string; last?: number;
  bias?: "bullish" | "bearish" | "neutral"; structure?: string;
  bos?: string; sweep?: string; order_block?: string; read?: string;
  fvgs?: { dir: string; lo: number; hi: number }[];
  buyside?: number[]; sellside?: number[];
  equilibrium?: number; zone?: "premium" | "discount"; score?: number;
  htf_bias?: "bullish" | "bearish" | "neutral"; confluence?: string;
  plan?: TradePlan; session?: { open: boolean; note: string; ist: string };
};
type GovDecision = {
  id: string; rung: string; label: string; kind: string; difficulty: number;
  factors?: Record<string, number>; lambda_eff: number; rationale: string;
  candidates?: { id: string; util: number }[];
};
type Homeostasis = { energy: number; mood: string; label: string; on_ac: boolean; tts_rate?: string };
type DeviceBrief = {
  tier?: string; power_state?: string; battery?: { percent: number; plugged: boolean } | null;
  headroom?: number; ram_available_gb?: number; cpu_percent?: number;
};
type Rung = { id: string; label: string; kind: string; tier: number; quality: number; energy: number; latency: number; available: boolean };
type InstalledModel = { name: string; gb: number | null; params?: string; quant?: string; tools?: boolean;
  runnable?: boolean; block_reason?: string | null };
type ModelRec = {
  tag: string; params: string; gb: number; ctx?: string; tools?: boolean;
  best?: boolean; installed?: boolean; best_for?: string; limits?: string; note?: string;
  needs_gb?: number;
};
type ModelBudget = {
  ram_total_gb: number; ram_available_gb: number; ram_reserve_gb: number;
  budget_gb: number; vram_gb: number; gpu_accel: boolean;
  instant_tight?: boolean; local_viable?: boolean;
  cpu_score?: number; gpu_score?: number; compute_score?: number;
  compute_label?: string; max_params_b?: number;
  cpu_cores?: number; cpu_threads?: number; cpu_ghz?: number | null;
  gpu_name?: string | null;
};
type ModelsData = {
  ollama: boolean; version?: string; tier?: string;
  installed?: InstalledModel[];
  running?: { name: string; gb: number | null; on_gpu: boolean }[];
  recommended?: ModelRec[];
  active?: { fast: string; deep: string; enabled: boolean };
  pinned?: string | null;
  budget?: ModelBudget;
};
type MemItem = { id: number; content: string; category: string; importance: number; source: string };

// "openai/gpt-oss-120b" -> "gpt-oss-120b"
const shortModel = (m: string) => (m || "").split("/").pop()!.replace("-instruct", "");
const MODE_LABEL: Record<string, string> = { auto: "Auto", eco: "Eco", local: "Local", cloud: "Cloud" };

type ApiKeyStatus = { secure: boolean; groq: boolean; anthropic: boolean; mem0: boolean };

declare global {
  interface Window {
    electronAPI?: {
      minimizeWindow?: () => void;
      toggleMaximize?: () => void;
      closeWindow?: () => void;
      restartBackend?: () => Promise<void>;
      openTrading?: () => Promise<{ ok: boolean; error?: string }>;
      onMaximizeChange?: (cb: (isMax: boolean) => void) => (() => void) | void;
      getApiKeyStatus?: () => Promise<ApiKeyStatus>;
      setApiKeys?: (keys: Record<string, string>) => Promise<ApiKeyStatus>;
      openExternal?: (url: string) => void;
    };
  }
}

// ── Constants ─────────────────────────────────────────────
const QUICK = [
  { label: "What's on screen", cmd: "what is on my screen right now",             icon: Eye       },
  { label: "Fix my screen",    cmd: "look at my screen and tell me what to fix",  icon: CopyCheck },
  { label: "Latest news",      cmd: "get me the latest tech and security news",   icon: Radio     },
  { label: "What you know",    cmd: "what do you remember about me",              icon: Database  },
];

const ROLE_META: Record<LineRole, { color: string; label: string }> = {
  user:   { color: "hud-blue",  label: "YOU"    },
  agent:  { color: "hud-amber", label: "JARVIS" },
  system: { color: "hud-muted", label: "SYS"   },
  tool:   { color: "hud-gold",  label: "TOOL"   },
};

const SIDEBAR_VARIANTS = {
  hidden: {},
  show:   { transition: { staggerChildren: 0.06, delayChildren: 0.1 } },
};
const ITEM_VARIANTS = {
  hidden: { opacity: 0, y: 12 },
  show:   { opacity: 1, y: 0, transition: { duration: 0.35, ease: "easeOut" as const } },
};

function mkLine(role: LineRole, text: string): Line {
  return {
    id: `${Date.now()}-${Math.random()}`,
    role,
    text,
    at: new Date().toLocaleTimeString("en-US", { hour12: false }),
  };
}

// ── Entry ─────────────────────────────────────────────────
export default function Page() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return <BootScreen />;
  return <CommandDeck />;
}

function BootScreen() {
  return (
    <div className="hud-boot">
      <motion.div initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.6 }}>
        <div className="hud-boot-ring" />
        <div className="hud-boot-text">
          <span className="text-amber">J.A.R.V.I.S</span>
          <span className="hud-boot-sub">JUST A RATHER VERY INTELLIGENT SYSTEM<span className="cursor-blink" /></span>
        </div>
      </motion.div>
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────
function CommandDeck() {
  const reduced = useReducedMotion();

  const [connected, setConnected]   = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [speaking, setSpeaking]     = useState(false);
  const [listening, setListening]   = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [input, setInput]           = useState("");
  const [error, setError]           = useState<string | null>(null);
  const [lines, setLines]           = useState<Line[]>([
    mkLine("system", "Ready. Type a command or use the mic."),
  ]);
  const [tasks, setTasks]           = useState<Task[]>([]);
  const [agentStatus, setAgentStatus] = useState<AgentStatus>({});
  const [sysStats, setSysStats]     = useState({ cpu: 0, ram: 0, disk: 0 });
  const [cmdHistory, setCmdHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx]       = useState(-1);
  const [rightTab, setRightTab]     = useState<"governor" | "rig" | "memory" | "tasks" | "trace" | "markets">("governor");
  // Live Ops console — the pentest cockpit. Auto-opens when a security tool fires.
  const [opsOpen, setOpsOpen]       = useState(false);
  const [runningTool, setRunningTool] = useState<{ action: string } | null>(null);
  const [reactorFlash, setReactorFlash] = useState(false);
  const [streamLine, setStreamLine] = useState("");
  const [maximized, setMaximized]   = useState(false);
  const [council, setCouncil]       = useState<CouncilState>({ active: false, panel: [], proposals: [], verdict: "" });
  const [voiceId, setVoiceId]       = useState("");
  // Settings is now ONE global panel (see routes/index.tsx) so it's identical on every
  // deck. Every in-deck "open settings" request routes there; the old in-deck SettingsModal
  // below is retired — it only ever renders with open=false, so it's inert.
  const settingsOpen = false;
  const setSettingsOpen = (v: boolean) => {
    if (v) window.dispatchEvent(new CustomEvent("jarvis:open-settings"));
  };
  const [helpOpen, setHelpOpen]                 = useState(false);
  const [mktSymbol, setMktSymbol]   = useState("nifty");
  const [mktData, setMktData]       = useState<IctRead | null>(null);
  const [mktLoading, setMktLoading] = useState(false);
  const [watching, setWatching]     = useState(false);
  const [alerts, setAlerts]         = useState<{ symbol: string; text: string; at: string }[]>([]);
  const [govDecision, setGovDecision] = useState<GovDecision | null>(null);
  const [homeostasis, setHomeostasis] = useState<Homeostasis | null>(null);
  const [deviceBrief, setDeviceBrief] = useState<DeviceBrief | null>(null);
  const [govMode, setGovMode]         = useState("auto");
  const [rungs, setRungs]             = useState<Rung[]>([]);
  const [models, setModels]           = useState<ModelsData | null>(null);
  const [memItems, setMemItems]       = useState<MemItem[]>([]);
  const [pulls, setPulls]             = useState<Record<string, { status: string; pct: number }>>({});
  const [bench, setBench]             = useState<Record<string, { tok?: number; status: string }>>({});
  const [sleepMsg, setSleepMsg]       = useState<string | null>(null);

  const wsRef       = useRef<WebSocket | null>(null);
  const speakTmr    = useRef<number | null>(null);
  const audioRef    = useRef<HTMLAudioElement | null>(null);
  const scrollRef   = useRef<HTMLDivElement | null>(null);
  const inputRef    = useRef<HTMLInputElement | null>(null);
  const fetchModelsRef = useRef<() => void>(null!);
  const fetchMemRef    = useRef<() => void>(null!);
  // Reconnect guards: dedup onclose/onerror both firing for one dropped
  // connection, and suppress reconnect once we've intentionally closed the
  // socket on unmount (otherwise a 5s-later reconnect opens a WebSocket
  // nothing will ever close).
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A disconnect only becomes user-visible after a short grace period — most
  // reconnects resolve within it, so brief blips never flash an error.
  const reconnectHintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const manualCloseRef    = useRef(false);

  // Stable refs so callbacks never recreate (avoids useEffect re-run loop)
  const addLineRef      = useRef<(role: LineRole, text: string) => void>(null!);
  const flashReactorRef = useRef<() => void>(null!);
  const refreshRef      = useRef<() => Promise<void>>(null!);
  const connectWsRef    = useRef<() => void>(null!);

  const addLine = useCallback((role: LineRole, text: string) => {
    if (!text.trim()) return;
    setLines(prev => [...prev.slice(-150), mkLine(role, text)]);
  }, []);
  addLineRef.current = addLine;

  const flashReactor = useCallback(() => {
    setReactorFlash(true);
    setTimeout(() => setReactorFlash(false), 600);
  }, []);
  flashReactorRef.current = flashReactor;

  // No deps — always fetches, never recreates
  const refreshStatus = useCallback(async () => {
    try {
      const r = await fetch("/api/agent/status");
      if (!r.ok) return;
      const d = await r.json();
      setAgentStatus(d);
      if (d.sys) setSysStats(d.sys);
      if (Array.isArray(d.tasks)) setTasks(d.tasks);
    } catch { /* silent */ }
  }, []);
  refreshRef.current = refreshStatus;

  // Stable — uses refs internally, never recreates
  const connectWs = useCallback(() => {
    const cur = wsRef.current;
    if (cur && (cur.readyState === WebSocket.OPEN || cur.readyState === WebSocket.CONNECTING)) return;
    if (cur) {
      cur.onclose = null;
      cur.onerror = null;
      try { cur.close(); } catch { /* stale socket */ }
      wsRef.current = null;
    }
    setConnecting(true);
    // Always go through Vite proxy (or same-host in prod) — avoids cross-origin WS issues
    const url = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true); setConnecting(false); setError(null);
      if (reconnectHintTimerRef.current) { clearTimeout(reconnectHintTimerRef.current); reconnectHintTimerRef.current = null; }
      refreshRef.current();
    };
    const scheduleReconnect = () => {
      if (!reconnectHintTimerRef.current) {
        reconnectHintTimerRef.current = setTimeout(() => {
          reconnectHintTimerRef.current = null;
          setError("Waking up…");
        }, 2500);
      }
      if (manualCloseRef.current || reconnectTimerRef.current) return;
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        connectWsRef.current();
      }, 5000);
    };
    ws.onclose = () => {
      setConnected(false); setListening(false); setSpeaking(false); setConnecting(false);
      scheduleReconnect();
    };
    ws.onerror = () => {
      setConnecting(false);
      scheduleReconnect();
    };
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        const txt: string = d.text ?? d.message ?? "";
        if (d.type === "state" || d.type === "status") {
          if (d.status === "speaking") setSpeaking(true);
          else if (!audioRef.current) setSpeaking(false);
          if (txt) addLineRef.current("system", txt);
          // "Running <tool>…" → show it as an in-flight step in the Ops console.
          const rm = /Running\s+([a-z_]+)/i.exec(txt || "");
          if (rm) {
            setRunningTool({ action: rm[1] });
            if (["recon", "pentest", "scope", "browse", "report"].includes(rm[1])) setOpsOpen(true);
          } else if (d.status === "idle") {
            setRunningTool(null);
          }
        }
        if (d.type === "emotion" && d.emotion) {
          setAgentStatus(prev => ({ ...prev, emotion: d.emotion }));
        }
        if (d.type === "transcription" || d.type === "transcript") addLineRef.current("user", txt);
        if (d.type === "llm_chunk" && d.text) {
          setStreamLine(prev => prev + (d.text as string));
        }
        if (d.type === "llm_response" || d.type === "response") {
          setStreamLine("");
          flashReactorRef.current();
          addLineRef.current("agent", txt);
          refreshRef.current();
        }
        if (d.type === "tts_audio" && d.data) {
          if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
          const blob = new Blob(
            [Uint8Array.from(atob(d.data as string), c => c.charCodeAt(0))],
            { type: "audio/mpeg" }
          );
          const url = URL.createObjectURL(blob);
          const audio = new Audio(url);
          audioRef.current = audio;
          setSpeaking(true);
          // Tell the backend the exact playback window so it mutes the mic and
          // never transcribes JARVIS's own voice (feedback-loop prevention).
          const ttsEnd = () => {
            setSpeaking(false);
            URL.revokeObjectURL(url);
            audioRef.current = null;
            wsRef.current?.send(JSON.stringify({ action: "tts_end" }));
          };
          audio.onended = ttsEnd;
          audio.onerror = ttsEnd;
          audio.play()
            .then(() => wsRef.current?.send(JSON.stringify({ action: "tts_start" })))
            .catch(() => ttsEnd());
        }
        if (d.type === "tts_stop") {
          if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
          setSpeaking(false);
          setStreamLine("");
          wsRef.current?.send(JSON.stringify({ action: "tts_end" }));
        }
        if (d.type === "system" && txt.trim()) {
          addLineRef.current("system", txt);
        }
        if (d.type === "content_panel" && d.title) {
          addLineRef.current("system", d.body ? `${d.title}\n${d.body}` : String(d.title));
        }
        if (d.type === "system_alert" && d.text) {
          notifyNative("JARVIS", String(d.text));
        }
        if (d.type === "council_start") {
          setCouncil({ active: true, panel: (d.panel as string[]) ?? [], proposals: [], verdict: "" });
          addLineRef.current("system", `Convening panel: ${((d.panel as string[]) ?? []).join(", ")}`);
        }
        if (d.type === "council_proposal") {
          setCouncil(c => ({ ...c, proposals: [...c.proposals, { model: String(d.model), text: txt }] }));
          addLineRef.current("tool", `[${d.model}] ${txt}`);
        }
        if (d.type === "council_verdict") {
          setCouncil(c => ({ ...c, active: false, verdict: txt }));
        }
        if (d.type === "voice_changed" && d.voice) setVoiceId(String(d.voice));
        if (d.type === "open_trading") {
          window?.electronAPI?.openTrading?.()
            .then((r) => {
              if (r?.ok === false && r.error) addLineRef.current("system", r.error);
              else addLineRef.current("system", "Opening trading terminal…");
            })
            .catch(() => addLineRef.current("system", "Could not open the trading terminal."));
        }
        if (d.type === "name_changed" && d.name) {
          setAgentStatus(prev => ({ ...prev, user: { name: String(d.name), onboarded: true } }));
          addLineRef.current("system", `Operator set to ${d.name}.`);
        }
        if (d.type === "watch_state") setWatching(Boolean(d.watching));
        if (d.type === "ict_alert") {
          const at = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          setAlerts(a => [{ symbol: String(d.symbol), text: txt, at }, ...a].slice(0, 8));
          flashReactorRef.current();
        }
        if (d.type === "governor_decision") {
          if (d.decision) setGovDecision(d.decision as GovDecision);
          if (d.homeostasis) setHomeostasis(d.homeostasis as Homeostasis);
          if (d.device) setDeviceBrief(d.device as DeviceBrief);
          flashReactorRef.current();
        }
        if (d.type === "governor_mode" && d.mode) setGovMode(String(d.mode));
        if (d.type === "model_pull") {
          setPulls(p => ({ ...p, [String(d.model)]: { status: String(d.status ?? ""), pct: Number(d.pct) || 0 } }));
          const done = d.status === "success" || d.status === "done" || (Number(d.pct) || 0) >= 100;
          if (d.status === "error" && d.error) addLineRef.current("system", String(d.error));
          if (done) fetchModelsRef.current?.();
        }
        if (d.type === "model_bench") setBench(b => ({ ...b, [String(d.model)]: { tok: typeof d.tok_per_sec === "number" ? d.tok_per_sec : undefined, status: String(d.status ?? "") } }));
        if (d.type === "model_delete" || d.type === "local_model_set") {
          if (d.type === "local_model_set" && d.ok === false && d.error) {
            addLineRef.current("system", String(d.error));
          }
          fetchModelsRef.current?.();
        }
        if (d.type === "sleep") {
          setSleepMsg(d.state === "start" ? "Consolidating memory…" : (txt || "rested"));
          if (d.state === "done") { fetchMemRef.current?.(); window.setTimeout(() => setSleepMsg(null), 6000); }
        }
        if (d.type === "memory_update") fetchMemRef.current?.();
        if (d.type === "audio_level") setAudioLevel(Number(d.level) || 0);
        if (d.type === "tasks" && Array.isArray(d.tasks)) setTasks(d.tasks);
        if (d.type === "agent_tool" && d.step) {
          const s = d.step as AgentTrace;
          addLineRef.current("tool", `[${s.action}] ${s.observation}`);
          setAgentStatus(prev => ({
            ...prev,
            trace: [...(prev.trace ?? []), s].slice(-30),
          }));
          setRightTab("trace");
          setRunningTool(null);
          // Security work belongs in the big Ops console, not a side tab — pop it open.
          if (["recon", "pentest", "scope", "browse", "report"].includes(s.action)) setOpsOpen(true);
        }
      } catch { addLineRef.current("system", "Malformed backend packet."); }
    };
  }, []); // empty deps — stable forever
  connectWsRef.current = connectWs;

  // Run once on mount only — no dependency array churn
  useEffect(() => {
    connectWs();
    const id = setInterval(() => refreshRef.current(), 15000);
    return () => {
      clearInterval(id);
      if (speakTmr.current) clearTimeout(speakTmr.current);
      if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
      manualCloseRef.current = true;
      if (reconnectTimerRef.current) { clearTimeout(reconnectTimerRef.current); reconnectTimerRef.current = null; }
      if (reconnectHintTimerRef.current) { clearTimeout(reconnectHintTimerRef.current); reconnectHintTimerRef.current = null; }
      if (wsRef.current) { wsRef.current.onclose = null; wsRef.current.onerror = null; wsRef.current.close(); }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6, behavior: reduced ? "auto" : "smooth" });
  }, [lines.length, reduced]);

  // Keep the maximize/restore icon in sync with the actual window state.
  useEffect(() => {
    const off = window?.electronAPI?.onMaximizeChange?.((isMax) => setMaximized(isMax));
    return () => { if (typeof off === "function") off(); };
  }, []);

  // Fetch the ICT read whenever the Markets tab is open or the symbol changes;
  // refresh every 60s while it's visible.
  const fetchMarket = useCallback(async (sym: string) => {
    setMktLoading(true);
    try {
      const r = await fetch(`/api/ict?symbol=${encodeURIComponent(sym)}&interval=15m`);
      setMktData(await r.json());
    } catch { setMktData({ ok: false, error: "Couldn't reach the market service." }); }
    finally { setMktLoading(false); }
  }, []);

  useEffect(() => {
    if (rightTab !== "markets") return;
    fetchMarket(mktSymbol);
    const id = setInterval(() => fetchMarket(mktSymbol), 60000);
    return () => clearInterval(id);
  }, [rightTab, mktSymbol, fetchMarket]);

  // Sync watcher state from periodic status refreshes.
  useEffect(() => { setWatching(Boolean(agentStatus.watch?.watching)); }, [agentStatus.watch?.watching]);

  // Governor / Model Advisor / Memory data.
  const fetchModels = useCallback(async () => {
    try { const r = await fetch("/api/models"); setModels(await r.json()); } catch { /* silent */ }
  }, []);
  fetchModelsRef.current = fetchModels;
  const fetchMemory = useCallback(async () => {
    try { const r = await fetch("/api/memory"); const d = await r.json(); setMemItems(d.memories ?? []); } catch { /* silent */ }
  }, []);
  const fetchGovernor = useCallback(async () => {
    try {
      const r = await fetch("/api/governor"); const d = await r.json();
      if (Array.isArray(d.rungs)) setRungs(d.rungs);
      if (d.mode) setGovMode(d.mode);
      if (d.homeostasis) setHomeostasis(d.homeostasis);
    } catch { /* silent */ }
  }, []);
  fetchMemRef.current = fetchMemory;

  useEffect(() => { fetchGovernor(); }, [fetchGovernor]);
  useEffect(() => { if (rightTab === "governor") fetchGovernor(); }, [rightTab, fetchGovernor]);
  useEffect(() => {
    if (rightTab !== "rig") return;
    fetchModels(); const id = setInterval(fetchModels, 8000); return () => clearInterval(id);
  }, [rightTab, fetchModels]);
  useEffect(() => { if (rightTab === "memory") fetchMemory(); }, [rightTab, fetchMemory]);
  useEffect(() => { if (agentStatus.governor?.mode) setGovMode(agentStatus.governor.mode); }, [agentStatus.governor?.mode]);
  useEffect(() => { if (agentStatus.homeostasis) setHomeostasis(agentStatus.homeostasis); }, [agentStatus.homeostasis]);

  const setMode = useCallback((m: string) => {
    setGovMode(m);
    wsRef.current?.send(JSON.stringify({ action: "set_mode", mode: m }));
  }, []);
  const onPull  = useCallback((tag: string) => {
    const ok = models?.recommended?.some(r => r.tag === tag);
    if (!ok) return;
    wsRef.current?.send(JSON.stringify({ action: "pull_model", model: tag }));
  }, [models?.recommended]);
  const onBench = useCallback((name: string) => { wsRef.current?.send(JSON.stringify({ action: "benchmark_model", model: name })); }, []);
  const onUse   = useCallback((name: string) => {
    const mod = models?.installed?.find(m => m.name === name);
    if (mod && mod.runnable === false) return;
    wsRef.current?.send(JSON.stringify({ action: "set_local_model", model: name }));
  }, [models?.installed]);
  const onDelete = useCallback((name: string) => {
    if (!window.confirm(`Delete ${name} from disk? You'll need to download it again to use it.`)) return;
    wsRef.current?.send(JSON.stringify({ action: "delete_model", model: name }));
    setBench(b => { const n = { ...b }; delete n[name]; return n; });
  }, []);
  const onForget = useCallback((id: number) => {
    wsRef.current?.send(JSON.stringify({ action: "forget_memory", id }));
    setMemItems(prev => prev.filter(m => m.id !== id));
  }, []);
  const onSleep = useCallback(() => { wsRef.current?.send(JSON.stringify({ action: "trigger_sleep" })); }, []);

  const sendCommand = useCallback(async (cmd = input) => {
    const text = cmd.trim();
    if (!text) return;
    addLine("user", text);
    setInput(""); setHistIdx(-1);
    setCmdHistory(h => [text, ...h.slice(0, 49)]);
    inputRef.current?.focus();
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: "command", text }));
      return;
    }
    try {
      const r = await fetch("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: text }),
      });
      if (!r.ok) throw new Error();
      // /api/command only acknowledges — the reply streams back over the WebSocket,
      // which is down if we're on this path. Bring the uplink back to receive it.
      addLine("system", "Got it — one moment, then I'll reply.");
      connectWsRef.current?.();
    } catch { setError("Waking up…"); }
  }, [addLine, input]);

  const toggleListen = () => {
    if (!connected) { connectWs(); return; }
    // While JARVIS is speaking, tapping mic means "stop talking," not "start
    // listening over you" — surfaces the backend's dedicated interrupt action.
    if (speaking) {
      wsRef.current?.send(JSON.stringify({ action: "stop" }));
      return;
    }
    const next = !listening;
    setListening(next);
    wsRef.current?.send(JSON.stringify({ action: next ? "start_listening" : "stop_listening" }));
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") { sendCommand(); return; }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      const i = Math.min(histIdx + 1, cmdHistory.length - 1);
      setHistIdx(i); setInput(cmdHistory[i] ?? "");
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const i = histIdx - 1;
      setHistIdx(i); setInput(i < 0 ? "" : (cmdHistory[i] ?? ""));
    }
  };

  const tools     = agentStatus.tools  ?? [];
  const trace     = agentStatus.trace  ?? [];
  const memCount  = agentStatus.memory?.count ?? 0;
  const memOk     = Boolean(agentStatus.memory?.available);
  const convoTurns = agentStatus.conversation?.turns ?? 0;
  const voiceOptions = agentStatus.voice?.options ?? [];
  const currentVoice = voiceId || agentStatus.voice?.current || "";
  const userName     = agentStatus.user?.name ?? "";
  const activeTsk = tasks.find(t => t.status === "active");
  const qTasks    = tasks.filter(t => t.status === "queued");
  const doneTasks = tasks.filter(t => t.status === "done").slice(-4).reverse();

  const connTone: Tone = connected ? "online" : connecting ? "warn" : "idle";
  const connLabel = connected
    ? listening ? "listening" : "online"
    : connecting ? "linking…" : "offline";

  const cloudOn = (agentStatus.governor?.available ?? []).some(r => r === "cloud_fast" || r === "cloud_deep" || r === "council");
  const localOn = Boolean(agentStatus.local?.enabled);
  const brainSummary = !connected ? (connecting ? "connecting…" : "offline")
    : cloudOn ? "cloud + local AI" : localOn ? `local AI · ${shortModel(agentStatus.local?.fast ?? "model")}` : "no AI configured yet";

  return (
    <div className="hud-root" style={{ paddingBottom: 48 }}>
      {/* Background layers (deepest first): living shader wash → grid → reactive motes */}
      <div className="hud-bg" aria-hidden>
        <ShaderBackdrop state={speaking ? "speaking" : listening ? "listening" : "idle"} />
        <div className="hud-bg-grid" />
        <HudAmbient
          state={speaking ? "speaking" : listening ? "listening" : "idle"}
          intensity={agentStatus.emotion?.enabled ? (agentStatus.emotion.intensity ?? 0.5) : 0.4}
        />
        <div className="hud-bg-scanline" />
        <motion.div
          className="hud-scan-sweep"
          animate={{ y: ["-100%", "100vh"] }}
          transition={{ duration: 8, repeat: Infinity, ease: "linear", repeatDelay: 4 }}
        />
      </div>

      {/* ═══ HEADER ═══ */}
      <header className="hud-header drag">
        <div className="hud-header-logo no-drag">
          <div className="hud-corner-box">
            <Terminal className="w-3.5 h-3.5 text-amber" />
          </div>
          <div>
            <p className="hud-logo-name">JARVIS</p>
            <p className="hud-logo-sub">personal AI</p>
          </div>
        </div>

        <div className="hud-statusbar no-drag">
          <StatusDot tone={connTone} pulse />
          <span className="hud-statusbar-main">{brainSummary}</span>
          {connected && !cloudOn && (
            <button className="hud-statusbar-nudge" onClick={() => setSettingsOpen(true)}>
              add a free API key for faster, smarter answers →
            </button>
          )}
        </div>

        <div className="hud-header-controls no-drag">
          <MoodChip emotion={agentStatus.emotion} />
          <IconBtn onClick={() => setOpsOpen(o => !o)} title="Live Ops — pentest console (step-by-step tool activity)">
            <Radio className="w-3.5 h-3.5" style={opsOpen ? { color: "var(--c-amber)" } : undefined} />
          </IconBtn>
          <IconBtn onClick={() => setHelpOpen(true)} title="What is this? — quick guide"><HelpCircle className="w-3.5 h-3.5" /></IconBtn>
          <IconBtn onClick={refreshStatus} title="Refresh"><Activity className="w-3.5 h-3.5" /></IconBtn>
          <IconBtn onClick={() => setSettingsOpen(true)} title="Settings"><Settings className="w-3.5 h-3.5" /></IconBtn>
          <IconBtn onClick={() => window?.electronAPI?.openTrading?.()} title="Open trading terminal"><LineChart className="w-3.5 h-3.5" /></IconBtn>
          <IconBtn onClick={() => window?.electronAPI?.restartBackend?.()} title="Restart backend"><Zap className="w-3.5 h-3.5" /></IconBtn>
          <div className="hud-sep" />
          <IconBtn onClick={() => window?.electronAPI?.minimizeWindow?.()} title="Minimize"><Minus className="w-3.5 h-3.5" /></IconBtn>
          <IconBtn onClick={() => window?.electronAPI?.toggleMaximize?.()} title={maximized ? "Restore" : "Maximize"}>
            {maximized ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          </IconBtn>
          <IconBtn danger onClick={() => window?.electronAPI?.closeWindow?.()} title="Close"><X className="w-3.5 h-3.5" /></IconBtn>
        </div>
      </header>

      {/* ═══ BODY ═══ */}
      <div className="hud-body">

        {/* ── LEFT SIDEBAR ── */}
        <motion.aside
          className="hud-left"
          variants={SIDEBAR_VARIANTS}
          initial="hidden"
          animate="show"
        >
          {/* Reactor hero */}
          <motion.div className="hud-reactor-hero" variants={ITEM_VARIANTS}>
            <motion.div
              animate={reactorFlash ? { opacity: [1, 0.3, 1] } : {}}
              transition={{ duration: 0.4 }}
            >
              <ArcReactor active={connected} speaking={speaking || listening} energy={homeostasis?.energy ?? 1} size="sm" />
            </motion.div>
            <div className="hud-reactor-label">
              <StatusDot tone={connTone} pulse />
              <span>{connLabel.toUpperCase()}</span>
            </div>
            <VoiceMeter
              userActive={listening && !speaking}
              jarvisActive={speaking}
              level={audioLevel}
            />
          </motion.div>

          {/* Energy — homeostasis (scales with battery/heat/load) */}
          <motion.div className="hud-card" variants={ITEM_VARIANTS}>
            <CardHeader title="Energy" active={Boolean(homeostasis)} />
            <BodyCard h={homeostasis} dev={deviceBrief} />
          </motion.div>

          {/* System vitals */}
          <motion.div className="hud-card" variants={ITEM_VARIANTS}>
            <CardHeader title="System Vitals" />
            <div className="hud-vitals">
              <Vital label="CPU" value={sysStats.cpu} />
              <Vital label="RAM" value={sysStats.ram} />
              <Vital label="DISK" value={sysStats.disk} />
            </div>
          </motion.div>

          {/* Agent core */}
          <motion.div className={`hud-card ${connected ? "hud-card--active" : ""}`} variants={ITEM_VARIANTS}>
            <CardHeader title="Status" active={connected} />
            <div className="hud-agent-rows">
              <AgentRow icon={UserRound} label="Operator" val={userName || "unset — open settings"} />
              <AgentRow icon={Brain}    label="Routing" val={`${MODE_LABEL[govMode] ?? govMode}${govDecision ? " · " + govDecision.label : ""}`} />
              <AgentRow icon={Database} label="Memory"  val={memOk ? `${memCount} records` : "offline"} />
              <AgentRow icon={Wrench}   label="Tools"   val={`${tools.length} registered`} />
              <AgentRow icon={Boxes}    label="Context" val={`${convoTurns} turns held`} />
            </div>
          </motion.div>

          {/* Council — only appears when you actually convene a panel ("deliberate …") */}
          {(council.active || council.verdict) && (
            <motion.div className={`hud-card ${council.active ? "hud-card--active" : ""}`} variants={ITEM_VARIANTS}>
              <CardHeader title="Council" active={council.active} />
              <Council council={council} idlePanel={agentStatus.council?.panel ?? []} />
            </motion.div>
          )}
        </motion.aside>

        {/* ── CENTER: TERMINAL ── */}
        <section className="hud-center" style={{ position: "relative" }}>

          {/* Live Ops console — the pentest cockpit. Overlays the conversation when a
              security tool runs; toggle with the Ops button in the header. */}
          <AnimatePresence>
            {opsOpen && (
              <OpsConsole
                key="ops"
                trace={trace}
                running={runningTool}
                accent="var(--c-amber)"
                onClose={() => setOpsOpen(false)}
              />
            )}
          </AnimatePresence>

          {/* Stream header */}
          <div className="hud-stream-header">
            <div className="flex items-center gap-2">
              <span className="text-amber text-[10px]">›</span>
              <span className="hud-label">Conversation</span>
              <span className="hud-badge">{lines.length}</span>
            </div>
            <div className="flex items-center gap-2">
              <motion.span
                className="hud-tx-label"
                animate={{ opacity: speaking ? [1, 0.4, 1] : 1 }}
                transition={{ duration: 0.8, repeat: speaking ? Infinity : 0 }}
              >
                {speaking ? "TX" : connected ? "RX" : "IDLE"}
              </motion.span>
              <IconBtn onClick={() => setLines([mkLine("system", "Terminal cleared.")])} title="Clear">
                <Trash2 className="w-3 h-3" />
              </IconBtn>
            </div>
          </div>

          {/* Messages */}
          <div ref={scrollRef} className="hud-messages">
            <AnimatePresence initial={false}>
              {lines.map((line) => (
                <motion.div
                  key={line.id}
                  className={`hud-msg hud-msg--${line.role}`}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.2, ease: "easeOut" }}
                >
                  <span className="hud-msg-time">{line.at}</span>
                  <span className={`hud-msg-role hud-msg-role--${ROLE_META[line.role].color}`}>
                    {ROLE_META[line.role].label}
                  </span>
                  <span className="hud-msg-text">{line.text}</span>
                </motion.div>
              ))}
            </AnimatePresence>

            {streamLine && (
              <div className="hud-msg hud-msg--agent" style={{ opacity: 0.85 }}>
                <span className="hud-msg-time">{new Date().toTimeString().slice(0, 8)}</span>
                <span className="hud-msg-role hud-msg-role--amber">JARVIS</span>
                <span className="hud-msg-text">
                  {streamLine}
                  <motion.span
                    animate={{ opacity: [1, 0] }}
                    transition={{ duration: 0.6, repeat: Infinity, ease: "easeInOut" }}
                    style={{ display: "inline-block", marginLeft: 2, width: 6, height: "1em",
                             background: "oklch(0.68 0.22 38)", verticalAlign: "middle" }}
                  />
                </span>
              </div>
            )}
          </div>

          {/* Quick actions */}
          <div className="hud-quick-row">
            {QUICK.map((q) => (
              <motion.button
                key={q.label}
                className="hud-quick"
                onClick={() => sendCommand(q.cmd)}
                whileHover={{ y: -2 }}
                whileTap={{ scale: 0.97 }}
                transition={{ type: "spring", stiffness: 400, damping: 20 }}
              >
                <q.icon className="w-3.5 h-3.5 shrink-0" />
                <span>{q.label}</span>
              </motion.button>
            ))}
          </div>

          {/* Command bar */}
          <div className="hud-input-wrap">
            <motion.div
              className="hud-input-bar"
              animate={speaking ? {
                boxShadow: [
                  "0 0 0 1px oklch(0.68 0.22 38 / 0.25)",
                  "0 0 18px oklch(0.68 0.22 38 / 0.45)",
                  "0 0 0 1px oklch(0.68 0.22 38 / 0.25)",
                ],
              } : {}}
              transition={{ duration: 1.5, repeat: Infinity }}
            >
              <ChevronRight className="w-3.5 h-3.5 text-amber shrink-0" />
              <input
                ref={inputRef}
                className="hud-input"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Ask, delegate, search, remember, act…"
                autoFocus
              />
              <IconBtn onClick={() => sendCommand()} active={speaking} title="Send [Enter]">
                <Send className="w-3.5 h-3.5" />
              </IconBtn>
              <IconBtn onClick={toggleListen} active={listening} title="Voice input">
                {listening ? <MicOff className="w-3.5 h-3.5" /> : <Mic className="w-3.5 h-3.5" />}
              </IconBtn>
            </motion.div>

            <AnimatePresence>
              {error && (
                <motion.p
                  className="hud-error"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                >
                  {error}
                </motion.p>
              )}
            </AnimatePresence>
            <p className="hud-hint">Enter · send &nbsp;|&nbsp; ↑↓ · history</p>
          </div>
        </section>

        {/* ── RIGHT: TABBED PANEL ── */}
        <aside className="hud-right">
          {/* Tab bar */}
          <div className="hud-tabs">
            {(
              [
                { id: "governor", icon: Cpu,             label: "Brain",    badge: undefined },
                { id: "rig",      icon: Server,           label: "Models",   badge: undefined },
                { id: "memory",   icon: Database,         label: "Memory",   badge: memCount > 0 ? String(memCount) : undefined },
                { id: "tasks",    icon: ListTodo,         label: "Tasks",    badge: qTasks.length > 0 ? String(qTasks.length) : undefined },
                { id: "trace",    icon: GitBranch,        label: "Activity", badge: trace.length > 0  ? String(trace.length)  : undefined },
                { id: "markets",  icon: CandlestickChart, label: "Markets",  badge: alerts.length > 0 ? String(alerts.length) : undefined },
              ] as const
            ).map((tab) => (
              <button
                key={tab.id}
                className={`hud-tab ${rightTab === tab.id ? "hud-tab--active" : ""}`}
                onClick={() => setRightTab(tab.id)}
                title={tab.label}
              >
                <tab.icon className="w-3 h-3 shrink-0" />
                <span>{tab.label}</span>
                {tab.badge && <span className="hud-tab-badge">{tab.badge}</span>}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="hud-tab-content">
            <AnimatePresence mode="wait" initial={false}>
              {rightTab === "tasks" && (
                <motion.div
                  key="tasks"
                  className="hud-tab-panel"
                  initial={{ opacity: 0, x: 12 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -12 }}
                  transition={{ duration: 0.2 }}
                >
                  <PanelIntro text="Things you've asked JARVIS to track." />
                  {activeTsk && (
                    <div className="hud-task hud-task--active">
                      <div className="hud-task-indicator" />
                      <div className="flex-1 min-w-0">
                        <p className="hud-task-label">{activeTsk.t}</p>
                        <p className="hud-task-meta">active · {activeTsk.at ?? "–"}</p>
                      </div>
                    </div>
                  )}
                  {!activeTsk && qTasks.length === 0 && doneTasks.length === 0 && (
                    <EmptyPane text="No tasks yet. Give me a goal." />
                  )}
                  {qTasks.slice(0, 6).map(t => (
                    <AnimatedTask key={t.id} task={t} />
                  ))}
                  {doneTasks.length > 0 && (
                    <p className="hud-section-divider">completed</p>
                  )}
                  {doneTasks.map(t => (
                    <AnimatedTask key={t.id} task={t} done />
                  ))}
                </motion.div>
              )}

              {rightTab === "trace" && (
                <motion.div
                  key="trace"
                  className="hud-tab-panel"
                  initial={{ opacity: 0, x: 12 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -12 }}
                  transition={{ duration: 0.2 }}
                >
                  <PanelIntro text="The tools JARVIS just used, step by step." />
                  {trace.length === 0
                    ? <EmptyPane text="Nothing yet — tools JARVIS uses will show here." />
                    : [...trace].reverse().map((s, i) => (
                      <motion.div
                        key={`${s.step}-${i}`}
                        className="hud-trace-row"
                        initial={{ opacity: 0, y: -6 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ delay: i * 0.03 }}
                      >
                        <div className="flex items-center gap-2 mb-1">
                          <span className="hud-trace-action">{s.action}</span>
                          <span className="hud-trace-step">#{s.step}</span>
                        </div>
                        <p className="hud-trace-obs line-clamp-3">{s.observation}</p>
                      </motion.div>
                    ))
                  }
                </motion.div>
              )}

              {rightTab === "governor" && (
                <motion.div key="governor" className="hud-tab-panel"
                  initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -12 }} transition={{ duration: 0.2 }}>
                  <GovernorPanel mode={govMode} setMode={setMode} decision={govDecision}
                    rungs={rungs} metrics={agentStatus.governor?.metrics} />
                </motion.div>
              )}

              {rightTab === "rig" && (
                <motion.div key="rig" className="hud-tab-panel"
                  initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -12 }} transition={{ duration: 0.2 }}>
                  <RigPanel models={models} pulls={pulls} bench={bench}
                    onPull={onPull} onBench={onBench} onUse={onUse} onDelete={onDelete} />
                </motion.div>
              )}

              {rightTab === "memory" && (
                <motion.div key="memory" className="hud-tab-panel"
                  initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -12 }} transition={{ duration: 0.2 }}>
                  <MemoryPanel items={memItems} onForget={onForget} onSleep={onSleep}
                    sleepMsg={sleepMsg} count={agentStatus.memory?.count ?? memItems.length} />
                </motion.div>
              )}

              {rightTab === "markets" && (
                <motion.div
                  key="markets"
                  className="hud-tab-panel"
                  initial={{ opacity: 0, x: 12 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -12 }}
                  transition={{ duration: 0.2 }}
                >
                  <PanelIntro text="Smart-money read on Indian indices. Analysis only — never places trades." />
                  <MarketsPanel
                    symbol={mktSymbol}
                    setSymbol={setMktSymbol}
                    data={mktData}
                    loading={mktLoading}
                    watching={watching}
                    watchlist={agentStatus.watch?.watchlist ?? []}
                    intervalMin={agentStatus.watch?.interval_min ?? 5}
                    alerts={alerts}
                    onRefresh={() => fetchMarket(mktSymbol)}
                    onToggleWatch={() =>
                      wsRef.current?.send(JSON.stringify({ action: watching ? "stop_watch" : "start_watch" }))
                    }
                    onDeliberate={(q) => sendCommand(`deliberate: ${q}`)}
                  />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </aside>
      </div>

      <SettingsModal
        open={settingsOpen}
        name={userName}
        voiceOptions={voiceOptions}
        currentVoice={currentVoice}
        onClose={() => setSettingsOpen(false)}
        onSave={(nm, voice) => {
          if (nm && nm !== userName) wsRef.current?.send(JSON.stringify({ action: "set_name", name: nm }));
          if (voice && voice !== currentVoice) {
            setVoiceId(voice);
            wsRef.current?.send(JSON.stringify({ action: "set_voice", voice }));
          }
          setSettingsOpen(false);
        }}
      />
      <HelpModal open={helpOpen} onClose={() => setHelpOpen(false)} onOpenSettings={() => setSettingsOpen(true)} />
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────

function VoiceMeter({ userActive, jarvisActive, level }: {
  userActive: boolean; jarvisActive: boolean; level: number;
}) {
  const norm = Math.max(5, Math.min(100, (level / 26000) * 100));
  return (
    <div className="hud-voice-meter">
      <VoiceChannel label="YOU"    accent="blue"  active={userActive}   level={userActive ? norm : 0} />
      <VoiceChannel label="JARVIS" accent="amber" active={jarvisActive} level={jarvisActive ? 40 : 0} />
    </div>
  );
}

function VoiceChannel({ label, accent, active, level }: {
  label: string; accent: "blue" | "amber"; active: boolean; level: number;
}) {
  const bars = Array.from({ length: 18 });
  return (
    <div className={`hud-vchan hud-vchan--${accent} ${active ? "hud-vchan--on" : ""}`}>
      <div className="hud-vchan-label">
        {accent === "blue" ? <Mic className="w-2.5 h-2.5" /> : <Volume2 className="w-2.5 h-2.5" />}
        {label}
        <span className="hud-vchan-status">{active ? "LIVE" : "–"}</span>
      </div>
      <div className="hud-vbars">
        {bars.map((_, i) => {
          const seed = Math.abs(Math.sin(i * 0.9)) * 35;
          const h = active ? Math.min(100, level * 0.5 + seed + 10) : 6 + (i % 3) * 4;
          return (
            <motion.span
              key={i}
              animate={{ height: `${h}%` }}
              transition={{ duration: 0.09, delay: i * 0.015 }}
            />
          );
        })}
      </div>
    </div>
  );
}

function Vital({ label, value }: { label: string; value: number }) {
  const color = value > 85 ? "danger" : value > 70 ? "warn" : "ok";
  return (
    <div className="hud-vital">
      <div className="hud-vital-header">
        <span>{label}</span>
        <motion.span
          className={`hud-vital-val hud-vital-val--${color}`}
          animate={{ color: value > 85 ? "var(--c-danger)" : "var(--c-muted)" }}
        >
          {value ? `${value}%` : "--"}
        </motion.span>
      </div>
      <div className="hud-vital-track">
        <motion.div
          className={`hud-vital-fill hud-vital-fill--${color}`}
          animate={{ width: `${value}%` }}
          transition={{ duration: 1.2, ease: [0.22, 1, 0.36, 1] }}
        />
      </div>
    </div>
  );
}

function AgentRow({ icon: Icon, label, val }: { icon: React.ElementType; label: string; val: string }) {
  return (
    <div className="hud-agent-row">
      <div className="hud-agent-icon"><Icon className="w-3 h-3" /></div>
      <div className="min-w-0">
        <p className="hud-agent-label">{label}</p>
        <p className="hud-agent-val truncate">{val}</p>
      </div>
    </div>
  );
}

function Council({ council, idlePanel }: { council: CouncilState; idlePanel: string[] }) {
  const AMBER = "oklch(0.68 0.22 38)";
  const panel = council.panel.length ? council.panel : idlePanel;
  const propOf = (m: string) => council.proposals.find(p => p.model === m);

  if (!panel.length && !council.verdict) {
    return (
      <p style={{ fontSize: 11, opacity: 0.55, lineHeight: 1.5 }}>
        Idle. Say <span style={{ color: AMBER }}>“deliberate …”</span> to convene a panel of
        models that debate and return one decision.
      </p>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {panel.map((m) => {
        const answered = Boolean(propOf(m));
        return (
          <div key={m} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <motion.span
              style={{
                width: 6, height: 6, borderRadius: "50%", flexShrink: 0,
                background: answered ? AMBER : "transparent", border: `1px solid ${AMBER}`,
              }}
              animate={{ opacity: council.active && !answered ? [0.3, 1, 0.3] : 1 }}
              transition={{ duration: 1.1, repeat: council.active && !answered ? Infinity : 0 }}
            />
            <span style={{ fontSize: 10.5, opacity: answered ? 0.9 : 0.6,
              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{m}</span>
          </div>
        );
      })}
      {council.active && !council.verdict && (
        <p style={{ fontSize: 10, opacity: 0.5, marginTop: 2 }}>Deliberating…</p>
      )}
      {council.verdict && (
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: `1px solid ${AMBER}22` }}>
          <p style={{ fontSize: 9, letterSpacing: "0.12em", textTransform: "uppercase",
            color: AMBER, opacity: 0.7, marginBottom: 3 }}>Verdict</p>
          <p style={{ fontSize: 11, lineHeight: 1.45, opacity: 0.9 }}>{council.verdict}</p>
        </div>
      )}
    </div>
  );
}

function TradingViewChart({ tv }: { tv: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || !tv) return;
    el.innerHTML = '<div class="tradingview-widget-container__widget" style="height:200px;width:100%"></div>';
    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.async = true;
    script.innerHTML = JSON.stringify({
      symbol: tv, interval: "15", timezone: "Asia/Kolkata", theme: "dark", style: "1",
      locale: "en", hide_top_toolbar: true, hide_legend: true, allow_symbol_change: false,
      save_image: false, width: "100%", height: 200,
    });
    el.appendChild(script);
    return () => { el.innerHTML = ""; };
  }, [tv]);
  return <div ref={ref} style={{ height: 200, width: "100%", borderRadius: 6, overflow: "hidden" }} />;
}

function MarketsPanel(p: {
  symbol: string; setSymbol: (s: string) => void; data: IctRead | null; loading: boolean;
  watching: boolean; watchlist: string[]; intervalMin: number;
  alerts: { symbol: string; text: string; at: string }[];
  onRefresh: () => void; onToggleWatch: () => void; onDeliberate: (q: string) => void;
}) {
  const AMBER = "oklch(0.68 0.22 38)";
  const GREEN = "oklch(0.74 0.18 150)";
  const RED   = "oklch(0.64 0.21 25)";
  const [custom, setCustom] = useState("");
  const d = p.data;
  const biasColor = d?.bias === "bullish" ? GREEN : d?.bias === "bearish" ? RED : AMBER;
  const chip = (active: boolean) => ({
    fontSize: 9.5, padding: "3px 7px", borderRadius: 5, cursor: "pointer",
    border: `1px solid ${AMBER}${active ? "" : "33"}`,
    background: active ? `${AMBER}22` : "transparent",
    color: active ? AMBER : "inherit", opacity: active ? 1 : 0.7,
  } as const);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
        {["nifty", "sensex", "banknifty"].map(s => (
          <button key={s} style={chip(p.symbol.toLowerCase() === s)} onClick={() => p.setSymbol(s)}>
            {s.toUpperCase()}
          </button>
        ))}
        <input
          value={custom} onChange={e => setCustom(e.target.value.toUpperCase())}
          onKeyDown={e => { if (e.key === "Enter" && custom.trim()) { p.setSymbol(custom.trim()); setCustom(""); } }}
          placeholder="NSE or US…" spellCheck={false}
          style={{ flex: 1, minWidth: 48, background: "transparent", color: AMBER, fontSize: 9.5,
            border: `1px solid ${AMBER}33`, borderRadius: 5, padding: "3px 5px", outline: "none", fontFamily: "inherit" }}
        />
      </div>

      {d?.ok && d.tv && <TradingViewChart tv={d.tv} />}

      {p.loading && !d && <p style={{ fontSize: 11, opacity: 0.5 }}>Reading the tape…</p>}
      {d && !d.ok && (
        <p style={{ fontSize: 10.5, opacity: d.netblock ? 0.85 : 0.55, lineHeight: 1.45,
          color: d.netblock ? AMBER : undefined, border: d.netblock ? `1px solid ${AMBER}33` : undefined,
          padding: d.netblock ? "8px 10px" : 0, borderRadius: d.netblock ? 6 : 0 }}>
          {d.error ?? "Market data unavailable right now — the market may be closed or offline. Live reads work during NSE hours (9:15–15:30 IST)."}
        </p>
      )}

      {d?.ok && (
        <>
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>{d.symbol} · {d.last}</span>
            <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.1em",
              textTransform: "uppercase", color: biasColor }}>{d.bias}</span>
          </div>
          <p style={{ fontSize: 10.5, opacity: 0.7, lineHeight: 1.4 }}>{d.structure}</p>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <span style={{ fontSize: 9.5, opacity: 0.75 }}>
              Daily: <span style={{ color: d.htf_bias === "bullish" ? GREEN : d.htf_bias === "bearish" ? RED : AMBER }}>{d.htf_bias}</span>
            </span>
            {d.confluence && (
              <Tag color={d.confluence === "aligned" ? GREEN : d.confluence === "conflicting" ? RED : AMBER}
                   label={d.confluence === "aligned" ? "✓ HTF aligned" : d.confluence === "conflicting" ? "⚠ HTF conflict" : "HTF neutral"} />
            )}
          </div>
          {d.session && (
            <p style={{ fontSize: 9.5, opacity: 0.6 }}>
              <span style={{ color: d.session.open ? GREEN : AMBER }}>●</span> Market {d.session.note} · {d.session.ist}
            </p>
          )}
          {typeof d.score === "number" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9.5, opacity: 0.75 }}>
                <span>CONFLUENCE {d.score}/100</span>
                {d.zone && (
                  <span style={{ color: d.zone === "discount" ? GREEN : RED, textTransform: "uppercase" }}>
                    {d.zone}{typeof d.equilibrium === "number" ? ` · eq ${d.equilibrium}` : ""}
                  </span>
                )}
              </div>
              <div style={{ height: 3, background: `${AMBER}22`, borderRadius: 2, overflow: "hidden" }}>
                <div style={{ height: "100%", width: `${d.score}%`,
                  background: d.score >= 70 ? GREEN : d.score >= 40 ? AMBER : RED }} />
              </div>
            </div>
          )}
          {d.bos && <Tag color={biasColor} label={d.bos} />}
          {d.sweep && <Tag color={AMBER} label={"sweep: " + d.sweep} />}
          {d.order_block && <Tag color={biasColor} label={d.order_block} />}
          {d.fvgs && d.fvgs.length > 0 && (
            <p style={{ fontSize: 10, opacity: 0.8 }}>
              FVGs: {d.fvgs.map(f => `${f.dir[0].toUpperCase()} ${f.lo}-${f.hi}`).join(" · ")}
            </p>
          )}
          {d.buyside && d.buyside.length > 0 && (
            <p style={{ fontSize: 10, opacity: 0.8 }}>↑ liq: {d.buyside.join(", ")}</p>
          )}
          {d.sellside && d.sellside.length > 0 && (
            <p style={{ fontSize: 10, opacity: 0.8 }}>↓ liq: {d.sellside.join(", ")}</p>
          )}
          <p style={{ fontSize: 10.5, opacity: 0.85, lineHeight: 1.45,
            borderTop: `1px solid ${AMBER}22`, paddingTop: 6 }}>{d.read}</p>
          {d.plan && d.plan.side !== "wait" && (
            <div style={{ border: `1px solid ${biasColor}55`, background: `${biasColor}12`,
              borderRadius: 6, padding: "6px 8px", display: "flex", flexDirection: "column", gap: 3 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.08em",
                  textTransform: "uppercase", color: biasColor }}>{d.plan.side} idea</span>
                <span style={{ fontSize: 9.5, opacity: 0.8 }}>R:R {d.plan.rr}</span>
              </div>
              <div style={{ display: "flex", gap: 10, fontSize: 10 }}>
                <span>entry <b>{d.plan.entry}</b></span>
                <span style={{ color: RED }}>SL {d.plan.sl}</span>
                <span style={{ color: GREEN }}>TP {d.plan.tp}</span>
              </div>
              <span style={{ fontSize: 8.5, opacity: 0.5 }}>Draft levels — you place the trade.</span>
            </div>
          )}
          {d.plan && d.plan.side === "wait" && (
            <p style={{ fontSize: 10, opacity: 0.6, fontStyle: "italic" }}>{d.plan.text}</p>
          )}
          <button onClick={() => p.onDeliberate(`${d.symbol} ${d.bias}, ${d.read}. Should I take it?`)}
            style={{ ...chip(false), alignSelf: "flex-start", padding: "4px 8px" }}>
            Ask the council ⚖
          </button>
        </>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 6, borderTop: `1px solid ${AMBER}22`, paddingTop: 8 }}>
        <button onClick={p.onToggleWatch}
          style={{ ...chip(p.watching), padding: "4px 8px" }}>
          {p.watching ? "◉ Watching" : "○ Start watcher"}
        </button>
        <span style={{ fontSize: 9, opacity: 0.55 }}>
          {p.watchlist.join(", ") || "—"} · {p.intervalMin}m
        </span>
      </div>

      {p.alerts.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {p.alerts.map((a, i) => (
            <div key={i} style={{ fontSize: 10, lineHeight: 1.35 }}>
              <span style={{ color: AMBER, opacity: 0.6 }}>{a.at}</span>{" "}{a.text}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Tag({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ fontSize: 9.5, padding: "2px 6px", borderRadius: 4, alignSelf: "flex-start",
      border: `1px solid ${color}55`, color, background: `${color}14` }}>{label}</span>
  );
}

function KeyField({ label, set, disabled, value, onChange, placeholder, onGet }: {
  label: string; set: boolean; disabled: boolean; value: string;
  onChange: (v: string) => void; placeholder: string; onGet: () => void;
}) {
  return (
    <div className="hud-key-row">
      <div className="hud-key-head">
        <span>{label}{set && <span className="hud-key-set"> · set</span>}</span>
        <button type="button" className="hud-key-link" onClick={onGet}>Get key ↗</button>
      </div>
      <input
        className="hud-modal-input" type="password" autoComplete="off" spellCheck={false}
        value={value} disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        placeholder={set ? "•••••••• (leave blank to keep)" : placeholder}
      />
    </div>
  );
}

function SettingsModal({
  open, name, voiceOptions, currentVoice, onClose, onSave,
}: {
  open: boolean; name: string;
  voiceOptions: { id: string; label: string }[]; currentVoice: string;
  onClose: () => void; onSave: (name: string, voice: string) => void;
}) {
  const hasElectron = typeof window !== "undefined" && !!window.electronAPI?.setApiKeys;
  const [nm, setNm] = useState(name);
  const [voice, setVoice] = useState(currentVoice);
  const [keyStatus, setKeyStatus] = useState<ApiKeyStatus | null>(null);
  const [groqKey, setGroqKey] = useState("");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [keyErr, setKeyErr] = useState("");

  useEffect(() => {
    if (!open) return;
    setNm(name);
    setVoice(currentVoice || voiceOptions[0]?.id || "");
    setGroqKey(""); setAnthropicKey(""); setKeyErr("");
    if (hasElectron) window.electronAPI!.getApiKeyStatus!().then(setKeyStatus).catch(() => {});
  }, [open, name, currentVoice, voiceOptions, hasElectron]);

  const openKeyLink = (url: string) => {
    if (window.electronAPI?.openExternal) window.electronAPI.openExternal(url);
    else window.open(url, "_blank", "noopener,noreferrer");
  };

  const handleSave = async () => {
    if (hasElectron) {
      const keys: Record<string, string> = {};
      if (groqKey.trim()) keys.GROQ_API_KEY = groqKey.trim();
      if (anthropicKey.trim()) keys.ANTHROPIC_API_KEY = anthropicKey.trim();
      if (Object.keys(keys).length) {
        try { setKeyStatus(await window.electronAPI!.setApiKeys!(keys)); }
        catch (e) { setKeyErr(e instanceof Error ? e.message : "Couldn't save keys."); return; }
      }
    }
    onSave(nm.trim(), voice);
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="hud-modal-overlay"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="hud-modal"
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ duration: 0.2, ease: "easeOut" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="hud-modal-header">
              <span>Settings</span>
              <IconBtn onClick={onClose} title="Close"><X className="w-3 h-3" /></IconBtn>
            </div>

            <div className="hud-modal-body">
              <p className="hud-modal-intro" style={{ marginBottom: 12 }}>
                Change how JARVIS addresses you, which voice it uses, or add API keys for
                faster cloud routing. Local models are managed in the <b style={{ color: "var(--c-amber)" }}>Models</b> tab.
              </p>

              <label className="hud-field-label">Your name</label>
              <input
                className="hud-modal-input"
                value={nm}
                onChange={(e) => setNm(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && nm.trim()) onSave(nm.trim(), voice); }}
                placeholder="e.g. Tony"
                maxLength={40}
                autoFocus
                spellCheck={false}
              />

              <label className="hud-field-label" style={{ marginTop: 12 }}>Voice</label>
              <select className="hud-modal-input" value={voice} onChange={(e) => setVoice(e.target.value)}>
                {voiceOptions.map((v) => (
                  <option key={v.id} value={v.id} style={{ background: "#120a06", color: "#eee" }}>
                    {v.label}
                  </option>
                ))}
              </select>

              <div className="hud-modal-divider" />
              <label className="hud-field-label">API Keys</label>
              {!hasElectron && (
                <p className="hud-modal-intro" style={{ margin: "4px 0 8px" }}>
                  Secure key storage runs in the desktop app. In the browser/dev, set keys in your{" "}
                  <code>.env</code> file.
                </p>
              )}
              {hasElectron && keyStatus && !keyStatus.secure && (
                <p className="hud-modal-warn">OS secure storage is unavailable — keys can't be saved safely here.</p>
              )}
              <KeyField
                label="Groq" set={Boolean(keyStatus?.groq)} disabled={!hasElectron}
                value={groqKey} onChange={setGroqKey} placeholder="gsk_…"
                onGet={() => openKeyLink("https://console.groq.com/keys")}
              />
              <KeyField
                label="Anthropic (Claude)" set={Boolean(keyStatus?.anthropic)} disabled={!hasElectron}
                value={anthropicKey} onChange={setAnthropicKey} placeholder="sk-ant-…"
                onGet={() => openKeyLink("https://console.anthropic.com/settings/keys")}
              />
              {keyErr && <p className="hud-modal-warn">{keyErr}</p>}
            </div>

            <div className="hud-modal-footer">
              <button className="hud-modal-btn hud-modal-btn--ghost" onClick={onClose}>Cancel</button>
              <button
                className="hud-modal-btn hud-modal-btn--primary"
                disabled={!nm.trim()}
                onClick={handleSave}
              >
                Save
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ── Governor / Body / Rig / Memory ────────────────────────
const C_AMBER = "oklch(0.68 0.22 38)";
const C_GREEN = "oklch(0.74 0.18 150)";
const C_RED   = "oklch(0.64 0.21 25)";
const energyColor = (e: number) => (e > 0.66 ? C_GREEN : e > 0.33 ? C_AMBER : C_RED);
const benBtn: React.CSSProperties = {
  fontSize: 8.5, padding: "2px 7px", borderRadius: 4, cursor: "pointer",
  border: `1px solid ${C_AMBER}44`, background: "transparent", color: C_AMBER,
  fontFamily: "inherit", textTransform: "uppercase", letterSpacing: "0.08em",
};

function MiniBar({ value, color }: { value: number; color: string }) {
  return (
    <div style={{ height: 3, background: `${color}22`, borderRadius: 2, overflow: "hidden", flex: 1 }}>
      <div style={{ height: "100%", width: `${Math.max(0, Math.min(100, Math.round(value * 100)))}%`, background: color }} />
    </div>
  );
}

function BodyCard({ h, dev }: { h: Homeostasis | null; dev: DeviceBrief | null }) {
  const energy = h?.energy ?? 1;
  const col = energyColor(energy);
  const bat = dev?.battery;
  return (
    <div style={{ padding: "9px 10px", display: "flex", flexDirection: "column", gap: 7 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span style={{ fontSize: 11, color: col, textTransform: "uppercase", letterSpacing: "0.14em" }}>{h?.label ?? "—"}</span>
        <span style={{ fontSize: 8.5, opacity: 0.55, textTransform: "uppercase", letterSpacing: "0.1em" }}>{dev?.tier ?? "—"}</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ fontSize: 8, opacity: 0.55, width: 40, textTransform: "uppercase", letterSpacing: "0.1em" }}>energy</span>
        <MiniBar value={energy} color={col} />
        <span style={{ fontSize: 9, color: col, width: 26, textAlign: "right" }}>{Math.round(energy * 100)}%</span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, opacity: 0.65 }}>
        <span>{dev?.power_state === "battery" ? `battery ${bat?.percent ?? "?"}%` : "on AC"}</span>
        <span>{typeof dev?.ram_available_gb === "number" ? `${dev.ram_available_gb} GB free` : ""}</span>
      </div>
    </div>
  );
}

function GovernorPanel(p: {
  mode: string; setMode: (m: string) => void; decision: GovDecision | null; rungs: Rung[];
  metrics?: { distribution: Record<string, number>; avg_latency_s: number | null; decisions: number };
}) {
  const dec = p.decision;
  const dist = p.metrics?.distribution ?? {};
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <PanelIntro text="Which AI model handled each request — and why JARVIS chose it." />
      <div style={{ display: "flex", gap: 4 }}>
        {["auto", "eco", "local", "cloud"].map(m => (
          <button key={m} onClick={() => p.setMode(m)}
            style={{ flex: 1, fontSize: 9, padding: "5px 0", textTransform: "uppercase", letterSpacing: "0.08em",
              cursor: "pointer", borderRadius: 4, border: `1px solid ${C_AMBER}${p.mode === m ? "" : "33"}`,
              background: p.mode === m ? `${C_AMBER}22` : "transparent",
              color: p.mode === m ? C_AMBER : "inherit", opacity: p.mode === m ? 1 : 0.6 }}>
            {MODE_LABEL[m]}
          </button>
        ))}
      </div>
      <p style={{ fontSize: 9, opacity: 0.5, marginTop: -4, lineHeight: 1.4 }}>
        Auto = JARVIS decides · Eco = save battery · Local = private/offline · Cloud = best quality
      </p>

      {dec ? (
        <div style={{ border: `1px solid ${C_AMBER}33`, padding: 10, display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <span style={{ fontSize: 12.5, fontWeight: 600, color: C_AMBER }}>{dec.label}</span>
            <span style={{ fontSize: 8.5, opacity: 0.55, textTransform: "uppercase" }}>{dec.kind}</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 8, opacity: 0.55, width: 52, textTransform: "uppercase" }}>difficulty</span>
            <MiniBar value={dec.difficulty} color={dec.difficulty > 0.66 ? C_RED : dec.difficulty > 0.33 ? C_AMBER : C_GREEN} />
            <span style={{ fontSize: 9, width: 26, textAlign: "right" }}>{Math.round(dec.difficulty * 100)}</span>
          </div>
          <p style={{ fontSize: 10, opacity: 0.75, lineHeight: 1.45 }}>{dec.rationale}</p>
          {dec.factors && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {Object.entries(dec.factors).filter(([, v]) => v > 0).map(([k, v]) => (
                <span key={k} style={{ fontSize: 8, padding: "1px 5px", borderRadius: 3, border: `1px solid ${C_AMBER}33`, opacity: 0.7 }}>
                  {k} {v < 1 ? v.toFixed(2) : v}
                </span>
              ))}
            </div>
          )}
        </div>
      ) : <EmptyPane text="Ask something — the routing decision appears here." />}

      <div>
        <p className="hud-section-divider" style={{ borderTop: "none", marginTop: 0 }}>model routing</p>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {[...p.rungs].sort((a, b) => a.tier - b.tier).map(r => {
            const active = dec?.rung === r.id;
            const used = dist[r.id] ?? 0;
            return (
              <div key={r.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 8px",
                border: `1px solid ${active ? C_AMBER : "var(--c-line)"}`,
                background: active ? `${C_AMBER}14` : "transparent",
                opacity: r.available ? 1 : 0.4, boxShadow: active ? `0 0 12px ${C_AMBER}33` : "none" }}>
                <span style={{ width: 6, height: 6, borderRadius: "50%", flexShrink: 0,
                  background: r.available ? C_GREEN : "var(--c-muted)" }} />
                <span style={{ fontSize: 10, flex: 1 }}>{r.label}</span>
                <div style={{ width: 34 }}><MiniBar value={r.quality} color={C_AMBER} /></div>
                {used > 0 && <span style={{ fontSize: 8, color: C_AMBER, opacity: 0.7, width: 22, textAlign: "right" }}>{used}×</span>}
              </div>
            );
          })}
        </div>
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, opacity: 0.6,
        borderTop: `1px solid ${C_AMBER}22`, paddingTop: 8 }}>
        <span>{p.metrics?.decisions ?? 0} routed</span>
        <span>avg {p.metrics?.avg_latency_s != null ? `${p.metrics.avg_latency_s}s` : "—"}</span>
        <span>adapts per-machine</span>
      </div>
    </div>
  );
}

function RigPanel(p: {
  models: ModelsData | null;
  pulls: Record<string, { status: string; pct: number }>;
  bench: Record<string, { tok?: number; status: string }>;
  onPull: (t: string) => void;
  onBench: (n: string) => void;
  onUse: (n: string) => void;
  onDelete: (n: string) => void;
}) {
  const m = p.models;
  const pinned = m?.pinned ?? m?.active?.deep ?? "";
  if (!m) return <EmptyPane text="Profiling your rig…" />;
  if (!m.ollama) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <p style={{ fontSize: 11, opacity: 0.8, lineHeight: 1.5 }}>
          Ollama isn't running. Local models power the Governor's on-device rungs — private, offline, and battery-aware.
        </p>
        <p style={{ fontSize: 10, opacity: 0.55 }}>Install from ollama.com, then run <code>ollama serve</code>.</p>
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
      <PanelIntro text="Local models matched to your CPU, GPU, and RAM. Pin, benchmark, download, or delete." />
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, opacity: 0.65 }}>
        <span>Ollama {m.version ? `v${m.version}` : "online"}</span>
        <span style={{ textTransform: "uppercase" }}>
          {m.budget
            ? `${m.budget.compute_label ?? "—"} compute · ${m.budget.budget_gb}GB RAM budget`
            : `tier · ${m.tier}`}
        </span>
      </div>
      {m.budget && (
        <div style={{ fontSize: 9, opacity: 0.62, lineHeight: 1.45 }}>
          {m.budget.cpu_cores ? `${m.budget.cpu_cores}c` : ""}
          {m.budget.cpu_ghz ? ` @ ${m.budget.cpu_ghz}GHz` : ""}
          {m.budget.gpu_name ? ` · ${m.budget.gpu_name}` : m.budget.gpu_accel ? " · GPU" : " · CPU inference"}
          {typeof m.budget.max_params_b === "number" ? ` · up to ~${m.budget.max_params_b}B params` : ""}
        </div>
      )}
      {m.budget && m.budget.local_viable === false && (
        <p style={{ fontSize: 10.5, color: C_AMBER, opacity: 0.9, lineHeight: 1.45,
          border: `1px solid ${C_AMBER}33`, padding: "8px 10px", borderRadius: 6 }}>
          Not enough RAM for local models (~{m.budget.budget_gb}GB budget). Add a Groq key in Settings for cloud routing.
        </p>
      )}
      {m.budget?.instant_tight && (
        <p style={{ fontSize: 9.5, color: C_AMBER, opacity: 0.85, lineHeight: 1.4 }}>
          RAM is tight ({m.budget.ram_available_gb}GB free) — close apps before pulling a large model.
        </p>
      )}
      {m.active?.enabled && (
        <div style={{ fontSize: 9.5, opacity: 0.7, lineHeight: 1.45, border: `1px solid ${C_AMBER}33`, padding: "6px 8px" }}>
          Active rungs — fast: <b>{shortModel(m.active.fast)}</b>
          {m.active.deep !== m.active.fast && <> · quality: <b>{shortModel(m.active.deep)}</b></>}
          {pinned && <> · pinned: <b style={{ color: C_AMBER }}>{shortModel(pinned)}</b></>}
        </div>
      )}
      <p className="hud-section-divider" style={{ borderTop: "none", marginTop: 0 }}>installed</p>
      {(m.installed ?? []).length === 0 ? (
        <EmptyPane text="No models yet — pull a recommended one below." />
      ) : (m.installed ?? []).map(mod => {
        const b = p.bench[mod.name];
        const isPinned = mod.name === pinned;
        const runnable = mod.runnable !== false;
        return (
          <div key={mod.name} style={{
            border: `1px solid ${isPinned ? `${C_AMBER}66` : !runnable ? `${C_RED}33` : "var(--c-line)"}`,
            background: isPinned ? `${C_AMBER}0a` : !runnable ? `${C_RED}06` : "transparent",
            padding: "6px 8px", display: "flex", flexDirection: "column", gap: 5,
            opacity: runnable ? 1 : 0.72,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 10, wordBreak: "break-all" }}>{mod.name}</span>
              <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                {isPinned && <span style={{ fontSize: 7.5, color: C_AMBER, border: `1px solid ${C_AMBER}55`, padding: "0 4px", borderRadius: 3, textTransform: "uppercase" }}>active</span>}
                {!runnable && <span style={{ fontSize: 7.5, color: C_RED, border: `1px solid ${C_RED}55`, padding: "0 4px", borderRadius: 3, textTransform: "uppercase" }}>blocked</span>}
                {mod.tools && runnable && <span style={{ fontSize: 7.5, color: C_GREEN, border: `1px solid ${C_GREEN}55`, padding: "0 4px", borderRadius: 3, textTransform: "uppercase" }}>tools</span>}
              </div>
            </div>
            {!runnable && mod.block_reason && (
              <span style={{ fontSize: 9, color: C_RED, opacity: 0.85, lineHeight: 1.35 }}>{mod.block_reason}</span>
            )}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 9, opacity: 0.6 }}>
              <span>{mod.params ?? ""}{mod.gb ? ` · ${mod.gb}GB` : ""}</span>
              <div style={{ display: "flex", gap: 4 }}>
                {runnable && mod.tools && !isPinned && (
                  <button onClick={() => p.onUse(mod.name)} style={benBtn} title="Use as quality model">use</button>
                )}
                {runnable && (
                  <button onClick={() => p.onBench(mod.name)} style={benBtn}>
                    {b?.status === "running" ? "…" : b?.tok ? `${b.tok} tok/s` : "benchmark"}
                  </button>
                )}
                <button onClick={() => p.onDelete(mod.name)} style={{ ...benBtn, borderColor: `${C_RED}44`, color: C_RED }} title="Delete from disk">
                  <Trash2 size={10} style={{ display: "inline", verticalAlign: "middle" }} />
                </button>
              </div>
            </div>
          </div>
        );
      })}
      <p className="hud-section-divider">available for your device</p>
      {(m.recommended ?? []).length === 0 ? (
        <EmptyPane text="No local models fit this hardware — use a Groq key for cloud AI." />
      ) : (m.recommended ?? []).map(rec => {
        const pull = p.pulls[rec.tag];
        const installing = pull && pull.status !== "success" && pull.status !== "done" && pull.pct < 100;
        return (
          <div key={rec.tag} style={{
            border: `1px solid ${rec.best ? `${C_AMBER}44` : "var(--c-line)"}`,
            background: rec.best ? `${C_AMBER}06` : "transparent",
            padding: "6px 8px", display: "flex", flexDirection: "column", gap: 4,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 10, fontWeight: rec.best ? 600 : 400 }}>{rec.tag}</span>
              <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                {rec.best && <span style={{ fontSize: 7.5, color: C_AMBER, textTransform: "uppercase", fontWeight: 700 }}>best</span>}
                {rec.tools && <span style={{ fontSize: 7.5, color: C_GREEN, textTransform: "uppercase" }}>tools</span>}
                {rec.installed
                  ? <span style={{ fontSize: 8, color: C_GREEN, textTransform: "uppercase" }}>installed</span>
                  : installing
                    ? <span style={{ fontSize: 8, color: C_AMBER }}>{pull.pct > 0 ? `${pull.pct}%` : "starting…"}</span>
                    : <button onClick={() => p.onPull(rec.tag)} style={benBtn}>pull</button>}
              </div>
            </div>
            <span style={{ fontSize: 9, opacity: 0.55 }}>
              {rec.params} · {rec.gb}GB disk{rec.needs_gb ? ` · ~${rec.needs_gb}GB RAM` : ""}{rec.ctx ? ` · ${rec.ctx}` : ""}
            </span>
            {rec.best_for && <span style={{ fontSize: 10, lineHeight: 1.4, opacity: 0.85 }}>{rec.best_for}</span>}
            {rec.limits && <span style={{ fontSize: 9, lineHeight: 1.35, opacity: 0.55 }}>Trade-off: {rec.limits}</span>}
            {pull && pull.pct > 0 && pull.pct < 100 && <MiniBar value={pull.pct / 100} color={C_AMBER} />}
          </div>
        );
      })}
    </div>
  );
}

function MemoryPanel(p: { items: MemItem[]; onForget: (id: number) => void; onSleep: () => void; sleepMsg: string | null; count: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <PanelIntro text="Facts JARVIS has saved about you. It learns more as you talk." />
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 10, opacity: 0.65 }}>{p.count} durable memories</span>
        <button onClick={p.onSleep} style={benBtn}>consolidate ⤓</button>
      </div>
      {p.sleepMsg && <p style={{ fontSize: 9.5, color: C_AMBER, opacity: 0.85 }}>● {p.sleepMsg}</p>}
      {p.items.length === 0 ? <EmptyPane text="Nothing remembered yet." /> :
        p.items.map(m => (
          <div key={m.id} style={{ border: "1px solid var(--c-line)", padding: "6px 8px", display: "flex", flexDirection: "column", gap: 3 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontSize: 8, textTransform: "uppercase", letterSpacing: "0.1em", color: C_AMBER, opacity: 0.7 }}>{m.category}</span>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ display: "flex", gap: 1 }}>
                  {Array.from({ length: 5 }).map((_, i) => (
                    <span key={i} style={{ width: 4, height: 4, borderRadius: "50%", background: i < Math.round(m.importance / 2) ? C_AMBER : `${C_AMBER}33` }} />
                  ))}
                </span>
                <button onClick={() => p.onForget(m.id)} title="Forget" style={{ background: "transparent", border: "none", cursor: "pointer", color: "var(--c-muted)", fontSize: 12, lineHeight: 1, padding: 0 }}>×</button>
              </div>
            </div>
            <span style={{ fontSize: 10, lineHeight: 1.4 }}>{m.content}</span>
          </div>
        ))}
    </div>
  );
}

function PanelIntro({ text }: { text: string }) {
  return <p className="hud-panel-intro">{text}</p>;
}

function HelpRow({ label, text }: { label: string; text: string }) {
  return (
    <div className="hud-help-row">
      <span className="hud-help-label">{label}</span>
      <span className="hud-help-text">{text}</span>
    </div>
  );
}

function HelpModal({ open, onClose, onOpenSettings }: { open: boolean; onClose: () => void; onOpenSettings: () => void }) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="hud-modal-overlay" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose}>
          <motion.div className="hud-modal hud-modal--wide"
            initial={{ opacity: 0, scale: 0.96, y: 8 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ duration: 0.2, ease: "easeOut" }} onClick={(e) => e.stopPropagation()}>
            <div className="hud-modal-header">
              <span>What is JARVIS?</span>
              <IconBtn onClick={onClose} title="Close"><X className="w-3 h-3" /></IconBtn>
            </div>
            <div className="hud-modal-body">
              <p className="hud-modal-intro" style={{ marginBottom: 10 }}>
                JARVIS is your personal AI. <b>Talk to it (mic) or type</b> in the bar at the bottom —
                ask questions, search the web, check your PC, remember things, or read the markets.
              </p>
              <p className="hud-modal-intro" style={{ marginBottom: 12 }}>
                <b>It picks the best AI for each request.</b> Easy things run on a fast local model on
                your PC; harder ones escalate to a stronger cloud model — automatically, factoring in
                your battery and load. <b style={{ color: "var(--c-amber)" }}>Add a free Groq key</b> in
                Settings for the full experience; without one it's local-only (and weaker).
              </p>
              <div className="hud-help-grid">
                <HelpRow label="Brain" text="Which AI model handled each request, and why." />
                <HelpRow label="Models" text="Local AI models — pin your quality brain, benchmark, download, or delete." />
                <HelpRow label="Memory" text="Facts JARVIS has saved about you." />
                <HelpRow label="Markets" text="Smart-money read on Indian indices. Analysis only." />
                <HelpRow label="Tasks" text="Things you've asked JARVIS to track." />
                <HelpRow label="Activity" text="Tools JARVIS just used, step by step." />
              </div>
            </div>
            <div className="hud-modal-footer">
              <button className="hud-modal-btn hud-modal-btn--ghost" onClick={() => { onClose(); onOpenSettings(); }}>Open Settings</button>
              <button className="hud-modal-btn hud-modal-btn--primary" onClick={onClose}>Got it</button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function CardHeader({ title, active }: { title: string; active?: boolean }) {
  return (
    <div className="hud-card-header">
      <span>{title}</span>
      {active !== undefined && <StatusDot tone={active ? "online" : "idle"} />}
    </div>
  );
}

function MoodChip({ emotion }: { emotion?: AgentStatus["emotion"] }) {
  if (!emotion || !emotion.enabled) return null;
  const AMBER = "oklch(0.68 0.22 38)";
  const intensity = Math.max(0, Math.min(1, emotion.intensity ?? 0));
  return (
    <div
      title={`${emotion.colour || emotion.emotion} · sarcasm: ${emotion.sarcasm}`}
      style={{
        display: "flex", alignItems: "center", gap: 6, padding: "3px 9px",
        borderRadius: 999, border: `1px solid ${AMBER}33`, background: `${AMBER}0d`,
        fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 10,
        letterSpacing: "0.06em", color: AMBER, whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          width: 7, height: 7, borderRadius: "50%", background: AMBER,
          boxShadow: `0 0 ${4 + intensity * 8}px ${AMBER}`, opacity: 0.55 + intensity * 0.45,
        }}
      />
      <span style={{ textTransform: "lowercase" }}>{emotion.emotion}</span>
    </div>
  );
}

function StatusPill({ tone, label }: { tone: Tone; label: string }) {
  return (
    <div className={`hud-pill hud-pill--${tone}`}>
      <StatusDot tone={tone} pulse />
      {label}
    </div>
  );
}

function StatusDot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return (
    <span
      className={`hud-dot hud-dot--${tone} ${pulse && tone === "online" ? "hud-dot--pulse" : ""}`}
    />
  );
}

function IconBtn({
  onClick, title, active = false, danger = false, children,
}: {
  onClick?: () => void; title?: string;
  active?: boolean; danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <motion.button
      className={`hud-icon-btn ${active ? "hud-icon-btn--active" : ""} ${danger ? "hud-icon-btn--danger" : ""}`}
      onClick={onClick}
      title={title}
      whileHover={{ scale: 1.08 }}
      whileTap={{ scale: 0.93 }}
    >
      {children}
    </motion.button>
  );
}

function AnimatedTask({ task, done = false }: { task: Task; done?: boolean }) {
  return (
    <motion.div
      className={`hud-task ${done ? "hud-task--done" : ""}`}
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.2 }}
    >
      {done
        ? <CheckCircle2 className="w-3 h-3 shrink-0 mt-0.5" />
        : <Clock className="w-3 h-3 shrink-0 mt-0.5" />
      }
      <div className="flex-1 min-w-0">
        <p className="hud-task-label">{task.t}</p>
        {task.eta && <p className="hud-task-meta">{task.eta}</p>}
      </div>
    </motion.div>
  );
}

function EmptyPane({ text }: { text: string }) {
  return (
    <div className="hud-empty">
      <span>{text}</span>
    </div>
  );
}
