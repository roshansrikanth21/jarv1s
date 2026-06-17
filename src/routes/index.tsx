import { createFileRoute } from "@tanstack/react-router";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import {
  Activity,
  Brain,
  CheckCircle2,
  ChevronRight,
  Clock,
  CopyCheck,
  Database,
  Eye,
  Lock,
  Mic,
  MicOff,
  Minus,
  Radio,
  Send,
  ShieldCheck,
  Terminal,
  Trash2,
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

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "JARVIS" },
      { name: "description", content: "JARVIS Command Deck" },
    ],
    links: [
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "anonymous" },
      {
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&display=swap",
      },
    ],
  }),
  component: Page,
});

// ── Types ─────────────────────────────────────────────────
type Tone = "online" | "warn" | "idle";
type LineRole = "user" | "agent" | "system" | "tool";
type Line = { id: string; role: LineRole; text: string; at: string };
type Task = { id: number; t: string; eta?: string; status: "queued" | "active" | "done"; at?: string };
type ToolInfo = { name: string; description: string };
type AgentTrace = { step: number; action: string; args: Record<string, unknown>; observation: string };
type AgentStatus = {
  brain?: { primary_llm: string; local_model: string; max_agent_steps: number };
  memory?: { available: boolean; count: number };
  tools?: ToolInfo[];
  tasks?: Task[];
  trace?: AgentTrace[];
};

declare global {
  interface Window {
    electronAPI?: {
      minimizeWindow?: () => void;
      closeWindow?: () => void;
      restartBackend?: () => Promise<void>;
    };
  }
}

// ── Constants ─────────────────────────────────────────────
const QUICK = [
  { label: "Scan Screen", cmd: "what is on my screen right now",                  icon: Eye       },
  { label: "Fix It",      cmd: "look at my screen and tell me what to fix",       icon: CopyCheck },
  { label: "CVE News",    cmd: "get me the latest cybersecurity news",             icon: Radio     },
  { label: "Recall",      cmd: "what do you remember about me",                   icon: Database  },
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
function Page() {
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
    mkLine("system", "Neural interface initialised. Awaiting directive."),
  ]);
  const [tasks, setTasks]           = useState<Task[]>([]);
  const [agentStatus, setAgentStatus] = useState<AgentStatus>({});
  const [sysStats, setSysStats]     = useState({ cpu: 0, ram: 0, disk: 0 });
  const [cmdHistory, setCmdHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx]       = useState(-1);
  const [rightTab, setRightTab]     = useState<"tasks" | "trace" | "tools">("tasks");
  const [reactorFlash, setReactorFlash] = useState(false);

  const wsRef       = useRef<WebSocket | null>(null);
  const speakTmr    = useRef<number | null>(null);
  const audioRef    = useRef<HTMLAudioElement | null>(null);
  const scrollRef   = useRef<HTMLDivElement | null>(null);
  const inputRef    = useRef<HTMLInputElement | null>(null);

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
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    setConnecting(true);
    // Always go through Vite proxy (or same-host in prod) — avoids cross-origin WS issues
    const url = `ws://${window.location.host}/ws`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true); setConnecting(false); setError(null);
      addLineRef.current("system", "WebSocket uplink established.");
      refreshRef.current();
    };
    ws.onclose = () => {
      setConnected(false); setListening(false); setSpeaking(false); setConnecting(false);
    };
    ws.onerror = () => {
      setError("Backend unreachable — retrying in 5 s");
      setConnecting(false);
      setTimeout(() => connectWsRef.current(), 5000);
    };
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        const txt: string = d.text ?? d.message ?? "";
        if (d.type === "state" || d.type === "status") {
          setSpeaking(d.status === "speaking");
          if (txt) addLineRef.current("system", txt);
        }
        if (d.type === "transcription" || d.type === "transcript") addLineRef.current("user", txt);
        if (d.type === "llm_response" || d.type === "response") {
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
          audio.onended = () => { setSpeaking(false); URL.revokeObjectURL(url); audioRef.current = null; };
          audio.onerror = () => { setSpeaking(false); URL.revokeObjectURL(url); audioRef.current = null; };
          audio.play().catch(() => setSpeaking(false));
        }
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
      wsRef.current?.close();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6, behavior: reduced ? "auto" : "smooth" });
  }, [lines.length, reduced]);

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
      const d = await r.json();
      addLine("agent", d.response ?? "Done.");
      refreshRef.current();
    } catch { setError("No backend connection."); }
  }, [addLine, input]);

  const toggleListen = () => {
    if (!connected) { connectWs(); return; }
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
  const activeTsk = tasks.find(t => t.status === "active");
  const qTasks    = tasks.filter(t => t.status === "queued");
  const doneTasks = tasks.filter(t => t.status === "done").slice(-4).reverse();

  const connTone: Tone = connected ? "online" : connecting ? "warn" : "idle";
  const connLabel = connected
    ? listening ? "listening" : "online"
    : connecting ? "linking…" : "offline";

  return (
    <div className="hud-root">
      {/* Background layers */}
      <div className="hud-bg" aria-hidden>
        <div className="hud-bg-grid" />
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
            <p className="hud-logo-sub">command deck · mk lxxxv</p>
          </div>
        </div>

        <div className="hud-ticker-wrap">
          <div className="hud-ticker">
            {Array.from({ length: 2 }, (_, rep) =>
              [
                `status: ${connLabel}`,
                `model: ${agentStatus.brain?.local_model ?? "llama3.1:8b"}`,
                `memory: ${memOk ? `${memCount} items` : "offline"}`,
                `tools: ${tools.length} active`,
                `safety: bounded / 8 steps`,
                `backend: ${connected ? "ws open" : "standby"}`,
                `trace: ${trace.length} steps`,
              ].map((s, i) => <span key={`${rep}-${i}`}>{s}</span>)
            )}
          </div>
        </div>

        <div className="hud-header-controls no-drag">
          <StatusPill tone={connTone} label={connLabel} />
          <IconBtn onClick={refreshStatus} title="Refresh"><Activity className="w-3.5 h-3.5" /></IconBtn>
          <IconBtn onClick={() => window?.electronAPI?.restartBackend?.()} title="Restart backend"><Zap className="w-3.5 h-3.5" /></IconBtn>
          <div className="hud-sep" />
          <IconBtn onClick={() => window?.electronAPI?.minimizeWindow?.()} title="Minimize"><Minus className="w-3.5 h-3.5" /></IconBtn>
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
              <ArcReactor active={connected} speaking={speaking || listening} size="sm" />
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
            <CardHeader title="Agent Core" active={connected} />
            <div className="hud-agent-rows">
              <AgentRow icon={Brain}    label="Brain"   val={agentStatus.brain?.local_model ?? "claude-sonnet-4-6"} />
              <AgentRow icon={Database} label="Memory"  val={memOk ? `${memCount} records` : "offline"} />
              <AgentRow icon={Wrench}   label="Tools"   val={`${tools.length} registered`} />
              <AgentRow icon={Lock}     label="Safety"  val={`max ${agentStatus.brain?.max_agent_steps ?? 8} steps`} />
            </div>
          </motion.div>

          {/* Safety badges */}
          <motion.div className="hud-card" variants={ITEM_VARIANTS}>
            <CardHeader title="Safety Kernel" />
            <div className="hud-safety-grid">
              <SafeBadge icon={ShieldCheck}  label="Bounded"    on />
              <SafeBadge icon={Lock}         label="No AutoPwr" on />
              <SafeBadge icon={CheckCircle2} label="JSON Tools" on />
              <SafeBadge icon={Eye}          label="Traceable"  on={trace.length > 0} />
            </div>
          </motion.div>
        </motion.aside>

        {/* ── CENTER: TERMINAL ── */}
        <section className="hud-center">

          {/* Stream header */}
          <div className="hud-stream-header">
            <div className="flex items-center gap-2">
              <span className="text-amber text-[10px]">›</span>
              <span className="hud-label">Neural Stream</span>
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
            <p className="hud-hint">Enter · send &nbsp;|&nbsp; ↑↓ · history &nbsp;|&nbsp; Alt+Space · focus</p>
          </div>
        </section>

        {/* ── RIGHT: TABBED PANEL ── */}
        <aside className="hud-right">
          {/* Tab bar */}
          <div className="hud-tabs">
            {(
              [
                { id: "tasks", icon: ListTodo,  label: "Tasks",  badge: qTasks.length > 0 ? String(qTasks.length) : undefined },
                { id: "trace", icon: GitBranch, label: "Trace",  badge: trace.length > 0  ? String(trace.length)  : undefined },
                { id: "tools", icon: Boxes,     label: "Tools",  badge: tools.length > 0  ? String(tools.length)  : undefined },
              ] as const
            ).map((tab) => (
              <button
                key={tab.id}
                className={`hud-tab ${rightTab === tab.id ? "hud-tab--active" : ""}`}
                onClick={() => setRightTab(tab.id)}
              >
                <tab.icon className="w-3.5 h-3.5" />
                <span>{tab.label}</span>
                {tab.badge && <span className="hud-tab-badge">{tab.badge}</span>}
                {rightTab === tab.id && (
                  <motion.div className="hud-tab-indicator" layoutId="tab-indicator" />
                )}
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
                  {trace.length === 0
                    ? <EmptyPane text="Tool calls appear here." />
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

              {rightTab === "tools" && (
                <motion.div
                  key="tools"
                  className="hud-tab-panel"
                  initial={{ opacity: 0, x: 12 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -12 }}
                  transition={{ duration: 0.2 }}
                >
                  {tools.length === 0
                    ? <EmptyPane text="Connect to backend to load tools." />
                    : tools.map((t, i) => (
                      <motion.div
                        key={t.name}
                        className="hud-tool-row"
                        initial={{ opacity: 0, y: 8 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ delay: i * 0.04 }}
                      >
                        <div className="hud-tool-icon"><Wrench className="w-3 h-3" /></div>
                        <div className="min-w-0">
                          <p className="hud-tool-name">{t.name}</p>
                          <p className="hud-tool-desc">{t.description}</p>
                        </div>
                      </motion.div>
                    ))
                  }
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </aside>
      </div>
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

function SafeBadge({ icon: Icon, label, on }: { icon: React.ElementType; label: string; on: boolean }) {
  return (
    <motion.div
      className={`hud-safe-badge ${on ? "hud-safe-badge--on" : ""}`}
      animate={{ opacity: on ? 1 : 0.45 }}
      whileHover={{ scale: 1.04 }}
    >
      <Icon className="w-3.5 h-3.5" />
      <span>{label}</span>
    </motion.div>
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
