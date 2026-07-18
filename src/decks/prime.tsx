// PRIME — the flagship JARVIS preset. Rendered by src/routes/index.tsx.
// All protocol I/O goes through the shared useJarvisSocket hook (send/toggleMic/
// sendAction/subscribe) + the REST endpoints; this file is view-only.
//
// Design doctrine (distilled from the five-angle research pass):
//   presence is one honest mark · glow is an event · decisions are visible ·
//   every gauge reads a real number · serif voice for JARVIS, mono for machines.
import { useCallback, useEffect, useMemo, useRef, useState, lazy, Suspense } from "react";
import { Mic, MicOff, Send, X, Moon, Trash2, Volume2 } from "lucide-react";
import { ContentPanel, type ContentPanelData } from "@/components/jarvis/ContentPanel";
import { WindowControls } from "@/components/jarvis/WindowControls";
import { useJarvisSocket } from "@/hooks/useJarvisSocket";
import "./prime.css";

const CoreOrb3D = lazy(() =>
  import("@/components/jarvis/CoreOrb3D").then((m) => ({ default: m.CoreOrb3D })),
);

/* ── types (mirrors api.py payloads) ─────────────────────── */
type Task = {
  id: number;
  t: string;
  eta?: string;
  status: "queued" | "active" | "done";
  at?: string;
};
type ToolInfo = { name: string; description: string };
type AgentTrace = {
  step: number;
  action: string;
  args: Record<string, unknown>;
  observation: string;
};
type Homeostasis = {
  energy: number;
  mood: string;
  label: string;
  on_ac: boolean;
  tts_rate?: string;
};
type GovDecision = {
  id: string;
  rung: string;
  label: string;
  kind: string;
  difficulty: number;
  factors?: Record<string, number>;
  lambda_eff: number;
  rationale: string;
  candidates?: { id: string; util: number }[];
};
type Rung = {
  id: string;
  label: string;
  kind: string;
  tier: number;
  quality: number;
  energy: number;
  latency: number;
  available: boolean;
};
type AgentStatus = {
  brain?: { primary_llm: string; local_model: string; reasoning?: string; max_agent_steps: number };
  conversation?: { turns: number };
  voice?: {
    current: string;
    options: { id: string; label: string }[];
    tts?: boolean;
    stt?: boolean;
    stt_hint?: string | null;
  };
  user?: { name: string; onboarded: boolean };
  watch?: { watching: boolean; watchlist: string[]; interval_min: number; tf: string };
  memory?: { available: boolean; count: number };
  governor?: {
    mode: string;
    available: string[];
    metrics: {
      distribution: Record<string, number>;
      avg_latency_s: number | null;
      decisions: number;
    };
  };
  homeostasis?: Homeostasis | null;
  device_tier?: string;
  local?: { enabled: boolean; fast: string; deep: string };
  tools?: ToolInfo[];
  tasks?: Task[];
  trace?: AgentTrace[];
  sys?: { cpu: number; ram: number; disk: number };
};
type InstalledModel = {
  name: string;
  gb: number | null;
  params?: string;
  quant?: string;
  tools?: boolean;
  runnable?: boolean;
  block_reason?: string | null;
};
type ModelRec = {
  tag: string;
  params: string;
  gb: number;
  tools?: boolean;
  best?: boolean;
  installed?: boolean;
  best_for?: string;
  limits?: string;
  needs_gb?: number;
  rank?: number;
  score?: number;
  fit_note?: string;
  runnable_now?: boolean;
  block_reason_now?: string | null;
  tok_per_sec?: number | null;
};
type ModelsData = {
  ollama: boolean;
  version?: string;
  tier?: string;
  installed?: InstalledModel[];
  running?: { name: string; gb: number | null; on_gpu: boolean }[];
  recommended?: ModelRec[];
  ranked?: ModelRec[];
  allowed_count?: number;
  active?: { fast: string; deep: string; enabled: boolean };
  pinned?: string | null;
  budget?: {
    ram_total_gb: number;
    ram_available_gb: number;
    budget_gb: number;
    live_gb?: number;
    disk_free_gb?: number | null;
    vram_gb: number;
    gpu_accel: boolean;
    instant_tight?: boolean;
    headroom?: number;
    compute_label?: string;
    compute_score?: number;
    max_params_b?: number;
    gpu_name?: string | null;
    cpu_cores?: number;
    cpu_threads?: number;
    cpu_ghz?: number | null;
  };
};
type MemItem = {
  id: number;
  content: string;
  category: string;
  importance: number;
  source: string;
};
type IctRead = {
  ok: boolean;
  error?: string;
  symbol?: string;
  interval?: string;
  last?: number;
  bias?: "bullish" | "bearish" | "neutral";
  structure?: string;
  bos?: string;
  sweep?: string;
  order_block?: string;
  read?: string;
  equilibrium?: number;
  zone?: string;
  score?: number;
  htf_bias?: string;
  confluence?: string;
  plan?: { side: string; entry?: number; sl?: number; tp?: number; rr?: number; text: string };
  session?: { open: boolean; note: string; ist: string };
  buyside?: number[];
  sellside?: number[];
};

type Feed =
  | { id: string; at: string; kind: "user" | "agent" | "system"; text: string }
  | {
      id: string;
      at: string;
      kind: "tool";
      action: string;
      args: Record<string, unknown>;
      observation: string;
      step: number;
    }
  | { id: string; at: string; kind: "route"; d: GovDecision }
  | { id: string; at: string; kind: "alert"; symbol: string; text: string };

// Distributive omit — plain Omit collapses the Feed union into never-matching shapes.
type FeedInput = Feed extends infer F ? (F extends Feed ? Omit<F, "id" | "at"> : never) : never;

const now = () =>
  new Date().toLocaleTimeString("en-GB", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
const fid = () => `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
const short = (m?: string) => (m || "").split("/").pop()?.replace("-instruct", "") || "—";
const pct = (v: number) => `${Math.round(v * 100)}%`;

const CHIPS: { label: string; cmd: string; action?: string }[] = [
  { label: "morning briefing", cmd: "", action: "trigger_briefing" },
  { label: "what's on my screen", cmd: "what is on my screen right now" },
  { label: "latest news", cmd: "search the web for latest world news in news mode" },
  { label: "what do you remember", cmd: "what do you remember about me" },
  { label: "system check", cmd: "how is my system doing" },
];
const MODES = ["auto", "eco", "local", "cloud"] as const;
const SYMBOLS = ["nifty", "banknifty", "sensex"] as const;
const TABS = ["governor", "rig", "memory", "ops", "markets"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABELS: Record<Tab, string> = {
  governor: "Brain",
  rig: "Models",
  memory: "Memory",
  ops: "Tasks",
  markets: "Markets",
};
const BACKEND_HINT: Record<string, string> = {
  cloud_fast: "Add Groq API key in Settings",
  cloud_deep: "Add Anthropic API key",
  council: "Needs Groq API key",
  local_fast: "Start Ollama",
  local_deep: "Start Ollama + free RAM",
};

// window.electronAPI is declared globally in decks/overhaul.tsx.

/* ═══════════════════════════════════════════════════════════ */
export default function PrimeDeck() {
  const {
    connected,
    listening,
    speaking,
    stream,
    level,
    send,
    toggleMic,
    sendAction,
    subscribe,
    mood,
    showReconnectHint,
  } = useJarvisSocket("Ready. Speak or type below.");

  const [feed, setFeed] = useState<Feed[]>([
    { id: fid(), at: now(), kind: "system", text: "Ready. Speak or type below." },
  ]);
  const [input, setInput] = useState("");
  const [busyText, setBusyText] = useState<string | null>(null);
  const [status, setStatus] = useState<AgentStatus>({});
  const [homeo, setHomeo] = useState<Homeostasis | null>(null);
  const [lastRoute, setLastRoute] = useState<GovDecision | null>(null);
  const [govMode, setGovMode] = useState("auto");
  const [rungs, setRungs] = useState<Rung[]>([]);
  const [models, setModels] = useState<ModelsData | null>(null);
  const [pulls, setPulls] = useState<Record<string, { status: string; pct: number }>>({});
  const [bench, setBench] = useState<Record<string, { tok?: number; status: string }>>({});
  const [customModel, setCustomModel] = useState("");
  const [mems, setMems] = useState<MemItem[]>([]);
  const [sleepMsg, setSleepMsg] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("governor");
  const [mktSym, setMktSym] = useState<string>("nifty");
  const [mkt, setMkt] = useState<IctRead | null>(null);
  const [mktLoading, setMktLoading] = useState(false);
  const [watching, setWatching] = useState(false);
  const [pulseKey, setPulseKey] = useState(0);
  // Settings is now ONE global panel (see routes/index.tsx) so it's identical on every
  // deck. Every in-deck "open settings" request routes there; the old in-deck SettingsModal
  // below is retired — it only ever renders with open=false, so it's inert.
  const settingsOpen = false;
  const setSettingsOpen = (v: boolean) => {
    if (v) window.dispatchEvent(new CustomEvent("jarvis:open-settings"));
  };
  const [sendErr, setSendErr] = useState<string | null>(null);
  const [contentPanel, setContentPanel] = useState<ContentPanelData | null>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const fetchModelsRef = useRef<() => void>(() => {});
  const fetchLoadedRef = useRef<() => void>(() => {});
  const fetchMemsRef = useRef<() => void>(() => {});
  const [loadedTs, setLoadedTs] = useState<number | null>(null);

  const push = useCallback((entry: FeedInput) => {
    setFeed((p) => [...p.slice(-180), { id: fid(), at: now(), ...entry }]);
  }, []);

  /* ── data fetchers ─────────────────────────────────────── */
  const fetchStatus = useCallback(async () => {
    try {
      const r = await fetch("/api/agent/status");
      if (!r.ok) return;
      const d: AgentStatus = await r.json();
      setStatus(d);
      if (d.homeostasis) setHomeo(d.homeostasis);
      if (d.governor?.mode) setGovMode(d.governor.mode);
      if (d.watch) setWatching(Boolean(d.watch.watching));
    } catch {
      /* backend briefly down — keep last snapshot */
    }
  }, []);

  const fetchGovernor = useCallback(async () => {
    try {
      const r = await fetch("/api/governor");
      if (!r.ok) return;
      const d = await r.json();
      if (Array.isArray(d.rungs)) setRungs(d.rungs);
      if (d.mode) setGovMode(d.mode);
      if (d.homeostasis) setHomeo(d.homeostasis);
    } catch {
      /* silent */
    }
  }, []);

  const fetchModels = useCallback(async () => {
    try {
      const r = await fetch("/api/models");
      if (r.ok) setModels(await r.json());
    } catch {
      /* silent */
    }
  }, []);
  fetchModelsRef.current = fetchModels;

  const fetchLoaded = useCallback(async () => {
    try {
      const r = await fetch("/api/models/loaded");
      if (!r.ok) return;
      const d = await r.json();
      if (Array.isArray(d.running)) {
        setModels((m) => (m ? { ...m, running: d.running } : m));
        if (typeof d.ts === "number") setLoadedTs(d.ts);
      }
    } catch {
      /* silent */
    }
  }, []);
  fetchLoadedRef.current = fetchLoaded;

  const fetchMems = useCallback(async () => {
    try {
      const r = await fetch("/api/memory");
      if (!r.ok) return;
      const d = await r.json();
      setMems(Array.isArray(d.memories) ? d.memories : []);
    } catch {
      /* silent */
    }
  }, []);
  fetchMemsRef.current = fetchMems;

  const fetchMkt = useCallback(async (sym: string) => {
    setMktLoading(true);
    try {
      const r = await fetch(`/api/ict?symbol=${encodeURIComponent(sym)}&interval=15m`);
      setMkt(await r.json());
    } catch {
      setMkt({ ok: false, error: "Market service unreachable." });
    } finally {
      setMktLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 20000);
    return () => clearInterval(id);
  }, [fetchStatus]);

  useEffect(() => {
    if (tab === "governor") fetchGovernor();
  }, [tab, fetchGovernor]);
  useEffect(() => {
    if (tab !== "rig") return;
    fetchModels();
    fetchLoaded();
    const slow = setInterval(fetchModels, 30000);
    const fast = setInterval(fetchLoaded, 5000);
    return () => {
      clearInterval(slow);
      clearInterval(fast);
    };
  }, [tab, fetchModels, fetchLoaded]);
  useEffect(() => {
    const t = customModel.trim();
    if (!t) return;
    const p = pulls[t];
    if (p && (p.status === "done" || p.pct >= 100)) setCustomModel("");
  }, [pulls, customModel]);
  useEffect(() => {
    if (tab === "memory") fetchMems();
  }, [tab, fetchMems]);
  useEffect(() => {
    if (tab !== "markets") return;
    fetchMkt(mktSym);
    const id = setInterval(() => fetchMkt(mktSym), 60000);
    return () => clearInterval(id);
  }, [tab, mktSym, fetchMkt]);

  /* ── protocol taps beyond the hook's core routing ──────── */
  useEffect(
    () =>
      subscribe((d) => {
        const txt = String(d.text ?? d.message ?? "");
        switch (d.type) {
          case "transcription":
          case "transcript":
            if (txt.trim()) push({ kind: "user", text: txt });
            break;
          case "llm_response":
          case "response":
            if (txt.trim()) push({ kind: "agent", text: txt });
            setBusyText(null);
            setPulseKey((k) => k + 1);
            break;
          case "state":
          case "status":
            if (d.status === "thinking") setBusyText(txt || "thinking");
            else if (d.status === "idle") setBusyText(null);
            break;
          case "agent_tool": {
            const s = d.step as AgentTrace | undefined;
            if (s?.action)
              push({
                kind: "tool",
                action: s.action,
                args: s.args ?? {},
                observation: s.observation ?? "",
                step: s.step ?? 0,
              });
            break;
          }
          case "governor_decision":
            if (d.decision) {
              const dec = d.decision as GovDecision;
              setLastRoute(dec);
              push({ kind: "route", d: dec });
            }
            if (d.homeostasis) setHomeo(d.homeostasis as Homeostasis);
            break;
          case "governor_mode":
            if (d.mode) setGovMode(String(d.mode));
            break;
          case "ict_alert":
            push({ kind: "alert", symbol: String(d.symbol ?? "?"), text: txt });
            break;
          case "watch_state":
            setWatching(Boolean(d.watching));
            break;
          case "system":
            if (txt.trim()) push({ kind: "system", text: txt });
            break;
          case "tts_error":
            if (txt.trim()) push({ kind: "system", text: txt });
            break;
          case "tasks":
            if (Array.isArray(d.tasks)) setStatus((p) => ({ ...p, tasks: d.tasks as Task[] }));
            break;
          case "model_pull": {
            const m = String(d.model ?? "");
            setPulls((p) => ({
              ...p,
              [m]: { status: String(d.status ?? ""), pct: Number(d.pct) || 0 },
            }));
            if (d.status === "error" && d.error) push({ kind: "system", text: String(d.error) });
            if (d.status === "done" || (Number(d.pct) || 0) >= 100) {
              fetchModelsRef.current();
              fetchLoadedRef.current();
            }
            break;
          }
          case "model_bench":
            if (d.model)
              setBench((b) => ({
                ...b,
                [String(d.model)]: {
                  tok: typeof d.tok_per_sec === "number" ? d.tok_per_sec : undefined,
                  status: String(d.status ?? ""),
                },
              }));
            break;
          case "model_delete":
          case "local_model_set":
            if (d.ok === false && d.error) push({ kind: "system", text: String(d.error) });
            fetchModelsRef.current();
            fetchLoadedRef.current();
            break;
          case "models_loaded":
            if (Array.isArray(d.running)) {
              setModels((m) => (m ? { ...m, running: d.running as ModelsData["running"] } : m));
              if (typeof d.ts === "number") setLoadedTs(d.ts as number);
            }
            break;
          case "sleep":
            setSleepMsg(d.state === "start" ? "consolidating memory…" : txt || "rested");
            if (d.state === "done") {
              fetchMemsRef.current();
              window.setTimeout(() => setSleepMsg(null), 6000);
            }
            break;
          case "memory_update":
            fetchMemsRef.current();
            break;
          case "name_changed":
            if (d.name)
              setStatus((p) => ({ ...p, user: { name: String(d.name), onboarded: true } }));
            break;
          case "content_panel":
            if (d.title && d.body) {
              setContentPanel({
                title: String(d.title),
                body: String(d.body),
                ts: d.ts ? String(d.ts) : undefined,
              });
            }
            break;
          // system_alert -> a native OS notification (useJarvisSocket), not an
          // in-page banner — nothing to do with it here.
          case "briefing":
            if (d.phase === "start") push({ kind: "system", text: "Morning briefing starting…" });
            if (d.phase === "done") push({ kind: "system", text: "Briefing complete." });
            break;
          case "open_trading":
            window.electronAPI
              ?.openTrading?.()
              .then((r) => {
                if (r?.ok === false && r.error) push({ kind: "system", text: r.error });
                else push({ kind: "system", text: "Opening trading terminal…" });
              })
              .catch(() => push({ kind: "system", text: "Could not open the trading terminal." }));
            break;
        }
      }),
    [subscribe, push],
  );

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6, behavior: "smooth" });
  }, [feed.length, stream, busyText]);

  /* ── actions ───────────────────────────────────────────── */
  const submit = useCallback(() => {
    const t = input.trim();
    if (!t) return;
    if (!connected) {
      setSendErr("Just a moment — waking up");
      window.setTimeout(() => setSendErr(null), 4000);
    }
    push({ kind: "user", text: t });
    setBusyText("thinking");
    send(t);
    setInput("");
    inputRef.current?.focus();
  }, [input, connected, push, send]);

  const runChip = useCallback(
    (chip: (typeof CHIPS)[number]) => {
      if (chip.action) {
        sendAction(chip.action);
        return;
      }
      if (!chip.cmd.trim()) return;
      push({ kind: "user", text: chip.cmd });
      setBusyText("thinking");
      send(chip.cmd);
    },
    [push, send, sendAction],
  );

  const setMode = useCallback(
    (m: string) => {
      setGovMode(m);
      sendAction("set_mode", { mode: m });
    },
    [sendAction],
  );

  const onForget = useCallback(
    (id: number) => {
      sendAction("forget_memory", { id });
      setMems((p) => p.filter((m) => m.id !== id));
    },
    [sendAction],
  );

  const onDeleteModel = useCallback(
    (name: string) => {
      if (!window.confirm(`Delete ${name} from disk?`)) return;
      sendAction("delete_model", { model: name });
    },
    [sendAction],
  );

  const onPullModel = useCallback(
    (tag: string) => {
      const t = tag.trim();
      if (!t) return;
      sendAction("pull_model", { model: t });
    },
    [sendAction],
  );

  /* ── derived ───────────────────────────────────────────── */
  const energy = homeo?.energy ?? 1;
  const jstate = speaking
    ? "speaking"
    : listening
      ? "listening"
      : busyText !== null
        ? "thinking"
        : energy < 0.33
          ? "low"
          : "idle";
  const stateWord = speaking
    ? "speaking"
    : listening
      ? "listening"
      : busyText !== null
        ? "thinking"
        : !connected
          ? showReconnectHint
            ? "waking up"
            : "ready"
          : energy < 0.33
            ? "low power"
            : "ready";

  const sys = status.sys ?? { cpu: 0, ram: 0, disk: 0 };
  const tasks = status.tasks ?? [];
  const activeTasks = tasks.filter((t) => t.status === "active");
  const queuedTasks = tasks.filter((t) => t.status === "queued");
  const doneTasks = tasks
    .filter((t) => t.status === "done")
    .slice(-5)
    .reverse();
  const tools = status.tools ?? [];
  const trace = (status.trace ?? []).slice(-14).reverse();
  const metrics = status.governor?.metrics;
  const userName = status.user?.name || "operator";
  const cloudOn = (status.governor?.available ?? []).some(
    (r) => r.startsWith("cloud") || r === "council",
  );
  const localOn = Boolean(status.local?.enabled);
  const brainLine = !connected
    ? showReconnectHint
      ? "waking up"
      : "…"
    : cloudOn && localOn
      ? "cloud + local"
      : cloudOn
        ? "cloud"
        : localOn
          ? `local · ${short(status.local?.fast)}`
          : "no engine configured";
  const micLevel = Math.min(level / 32767, 1);

  const rankByTag = useMemo(() => {
    const m = new Map<string, ModelRec>();
    for (const r of models?.ranked ?? []) m.set(r.tag, r);
    return m;
  }, [models?.ranked]);

  const rankForInstalled = useCallback(
    (name: string) => {
      if (rankByTag.has(name)) return rankByTag.get(name);
      const base = name.split("/").pop() ?? name;
      const [n, v] = base.split(":");
      const stem = v ? `${n}:${v.split("-")[0]}` : n;
      for (const [tag, rec] of rankByTag) {
        if (tag === stem || name.startsWith(tag)) return rec;
      }
      return undefined;
    },
    [rankByTag],
  );

  const findInstalled = useCallback(
    (tag: string) => {
      for (const m of models?.installed ?? []) {
        const rec = rankForInstalled(m.name);
        if (rec?.tag === tag || m.name === tag || m.name.startsWith(`${tag}-`)) return m;
      }
      return undefined;
    },
    [models?.installed, rankForInstalled],
  );

  const rankedList = useMemo(() => {
    const raw = models?.ranked?.length
      ? models.ranked
      : (models?.recommended ?? []).map((r, i) => ({ ...r, rank: r.rank ?? i + 1 }));
    return raw.map((r, i) => ({ ...r, rank: r.rank ?? i + 1 }));
  }, [models?.ranked, models?.recommended]);

  const orphanInstalled = useMemo(() => {
    const rankedTags = new Set(rankedList.map((r) => r.tag));
    return (models?.installed ?? []).filter((m) => {
      const rec = rankForInstalled(m.name);
      return !rec?.tag || !rankedTags.has(rec.tag);
    });
  }, [models?.installed, rankedList, rankForInstalled]);

  const lastAgent = [...feed].reverse().find((e) => e.kind === "agent");
  const agentCaption = stream || (lastAgent && lastAgent.kind === "agent" ? lastAgent.text : "");

  return (
    <div className="pr-root" data-jstate={jstate} style={{ paddingBottom: 48 }}>
      {/* ═══ header ═══ */}
      <header className="pr-header drag">
        <div className="no-drag" style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <span className="pr-wordmark">
            JARV<em>1</em>S
          </span>
          <span className="pr-header-sub">prime</span>
        </div>
        <div className="pr-statusline no-drag">
          <span>{stateWord}</span>
          <span style={{ opacity: 0.35 }}>·</span>
          <span>
            engine <b>{brainLine}</b>
          </span>
          <span style={{ opacity: 0.35 }}>·</span>
          <span>
            mode <b>{govMode}</b>
          </span>
          {homeo && (
            <>
              <span style={{ opacity: 0.35 }}>·</span>
              <span>
                energy <b className="pr-num">{pct(energy)}</b> {homeo.label}
              </span>
            </>
          )}
          {mood?.enabled && (
            <>
              <span style={{ opacity: 0.35 }}>·</span>
              <span title={mood.colour ?? mood.emotion}>
                mood <b>{mood.emotion}</b>
              </span>
            </>
          )}
        </div>
        <div className="pr-trust no-drag" title="Connection, mic, and speaker status">
          <span className="pr-trust-item">
            <i className={`pr-trust-dot ${connected ? "pr-trust-dot--on" : ""}`} />
            <span className="pr-lab">conn</span>
          </span>
          <span className="pr-trust-item">
            <i className={`pr-trust-dot ${listening ? "pr-trust-dot--hot" : ""}`} />
            <span className="pr-lab">mic</span>
          </span>
          <span className="pr-trust-item">
            <i className={`pr-trust-dot ${speaking ? "pr-trust-dot--tx" : ""}`} />
            <span className="pr-lab">speak</span>
          </span>
        </div>
        <WindowControls variant="prime" onSettings={() => setSettingsOpen(true)} />
      </header>

      <div className="pr-body">
        {/* ═══ left wing — telemetry + tools ═══ */}
        <aside className="pr-wing pr-wing--left">
          <section className="pr-card">
            <div className="pr-card-head">
              <span className="pr-lab">vitals</span>
              <span className="pr-lab">{status.device_tier ?? "node"}</span>
            </div>
            <div className="pr-tele">
              {(
                [
                  ["cpu", sys.cpu],
                  ["ram", sys.ram],
                  ["disk", sys.disk],
                ] as const
              ).map(([k, v]) => (
                <div className="pr-tele-row" key={k}>
                  <div className="pr-tele-head">
                    <span className="pr-lab">{k}</span>
                    <span className={`pr-num ${v > 90 ? "is-bad" : v > 75 ? "is-warn" : ""}`}>
                      {v}%
                    </span>
                  </div>
                  <div className="pr-meter">
                    <i
                      className={v > 90 ? "is-bad" : v > 75 ? "is-warn" : ""}
                      style={{ width: `${v}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="pr-card">
            <div className="pr-card-head">
              <span className="pr-lab">mode</span>
              <span className="pr-num">{govMode}</span>
            </div>
            <div className="pr-modes">
              {MODES.map((m) => (
                <button
                  key={m}
                  className={`pr-mode ${govMode === m ? "pr-mode--on" : ""}`}
                  onClick={() => setMode(m)}
                >
                  {m}
                </button>
              ))}
            </div>
          </section>

          <section className="pr-card pr-toolstack">
            <div className="pr-card-head">
              <span className="pr-lab">quick actions</span>
            </div>
            <div className="pr-chips pr-chips--stack">
              {CHIPS.map((c) => (
                <button key={c.label} className="pr-chip" onClick={() => runChip(c)}>
                  {c.label}
                </button>
              ))}
            </div>
          </section>
        </aside>

        {/* ═══ center — 3D core + live coordinates ═══ */}
        <main className="pr-arena">
          <button
            type="button"
            className="pr-orb-stage no-drag"
            onClick={toggleMic}
            title={listening ? "Stop listening" : "Tap core — speak"}
          >
            {/* Reticle lives INSIDE the stage so it's always concentric with the orb — as a
                sibling it centered on the column and drifted above the orb (the caption below
                pushed the orb up). pointer-events:none keeps the whole stage clickable. */}
            <div className="pr-reticle" aria-hidden />
            {pulseKey > 0 && <span key={pulseKey} className="pr-orb-flash" aria-hidden />}
            <Suspense
              fallback={<div className="pr-orb-canvas pr-orb-canvas--loading" aria-hidden />}
            >
              <CoreOrb3D state={jstate} audioLevel={micLevel} />
            </Suspense>
          </button>

          <div className="pr-arena-foot">
            <ContentPanel data={contentPanel} onDismiss={() => setContentPanel(null)} />
            <span className="pr-lab">core · {stateWord}</span>
            {busyText && !agentCaption && (
              <p className="pr-arena-thinking">
                <span className="pr-thinking-dots" aria-hidden />
                {busyText}
              </p>
            )}
            {agentCaption && (
              <p className="pr-arena-voice">
                {agentCaption.slice(0, 220)}
                {agentCaption.length > 220 ? "…" : ""}
                {stream && <span className="pr-caret" />}
              </p>
            )}
            {!agentCaption && !busyText && (
              <p className="pr-arena-idle">Tap the core to speak · type in the activity panel</p>
            )}
          </div>
        </main>

        {/* ═══ right wing — stream + system panels ═══ */}
        <aside className="pr-wing pr-wing--right">
          <div className="pr-stream-col">
            <div className="pr-stream-head">
              <span className="pr-lab">activity</span>
              <span className="pr-num">{feed.length} items</span>
            </div>
            <div className="pr-feed pr-feed--compact" ref={scrollRef}>
              {feed.map((e) => (
                <FeedRow key={e.id} e={e} />
              ))}
              {busyText !== null && !stream && (
                <div className="pr-thinking">
                  <i />
                  <i />
                  <i />
                  <span>{busyText}</span>
                </div>
              )}
            </div>
            <div className="pr-composer pr-composer--compact">
              <div className="pr-inputbar">
                <input
                  ref={inputRef}
                  className="pr-input"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") submit();
                  }}
                  placeholder={`Talk to JARVIS, ${userName}…`}
                />
                <button
                  className={`pr-mic ${listening ? "pr-mic--live" : ""}`}
                  onClick={toggleMic}
                  title={listening ? "Stop" : "Mic"}
                >
                  {listening ? <Mic size={15} /> : <MicOff size={15} />}
                </button>
                <button className="pr-send" onClick={submit} disabled={!input.trim()}>
                  <Send size={12} />
                </button>
              </div>
              <div className="pr-composer-hint">
                <span className={sendErr ? "is-bad" : ""}>
                  {sendErr ?? (connected ? "Connected" : showReconnectHint ? "Waking up…" : "")}
                </span>
              </div>
            </div>
          </div>

          <div className="pr-tools-col">
            <nav className="pr-tabs">
              {TABS.map((t) => (
                <button
                  key={t}
                  className={`pr-tab ${tab === t ? "pr-tab--on" : ""}`}
                  onClick={() => setTab(t)}
                >
                  {TAB_LABELS[t]}
                </button>
              ))}
            </nav>

            {tab === "governor" && (
              <div className="pr-pane">
                {lastRoute ? (
                  <div className="pr-card pr-brain-summary">
                    <p className="pr-brain-last">
                      Last reply: <strong>{lastRoute.label}</strong>
                    </p>
                    {metrics?.avg_latency_s != null && metrics.decisions > 0 && (
                      <p className="pr-brain-meta">
                        Avg {metrics.avg_latency_s.toFixed(0)}s over {metrics.decisions} message
                        {metrics.decisions === 1 ? "" : "s"}
                      </p>
                    )}
                  </div>
                ) : (
                  <div className="pr-empty">Send a message to see which backend answers.</div>
                )}

                <div className="pr-sec">
                  <span className="pr-lab">backends</span>
                </div>
                {rungs.length === 0 && <div className="pr-empty">Loading…</div>}
                <ul className="pr-backend-list">
                  {rungs.map((r) => (
                    <li key={r.id} className={r.available ? "is-on" : "is-off"}>
                      <span className="pr-backend-name">{r.label}</span>
                      <span className="pr-backend-state">
                        {r.available ? "ready" : (BACKEND_HINT[r.id] ?? "offline")}
                      </span>
                    </li>
                  ))}
                </ul>

                {!cloudOn && localOn && (
                  <p className="pr-pane-hint">
                    Only local models are active. Add a Groq key in Settings (Ctrl+,) for much
                    faster replies.
                  </p>
                )}
              </div>
            )}

            {tab === "rig" && (
              <div className="pr-pane">
                {!models && <div className="pr-empty">Loading models…</div>}
                {models && (
                  <>
                    {models.budget && (
                      <div className="pr-card pr-budget pr-budget--profile">
                        <div className="pr-budget-title">Your machine</div>
                        {models.budget.instant_tight && (
                          <p className="pr-budget-warn">
                            RAM is tight ({models.budget.ram_available_gb?.toFixed(1)}GB free) —
                            close apps before downloading or switching models.
                          </p>
                        )}
                        <div className="pr-kv">
                          <span className="pr-lab">system</span>
                          <span className="pr-num">
                            {models.budget.compute_label ?? "—"}
                            {models.budget.headroom != null
                              ? ` · ${Math.round(models.budget.headroom * 100)}% headroom`
                              : ""}
                          </span>
                        </div>
                        <div className="pr-kv">
                          <span className="pr-lab">cpu</span>
                          <span className="pr-num">
                            {models.budget.cpu_cores ?? "—"} cores
                            {models.budget.cpu_ghz ? ` @ ${models.budget.cpu_ghz}GHz` : ""}
                          </span>
                        </div>
                        <div className="pr-kv">
                          <span className="pr-lab">gpu</span>
                          <span className="pr-num">
                            {models.budget.gpu_accel && models.budget.gpu_name
                              ? models.budget.gpu_name
                              : "cpu only"}
                          </span>
                        </div>
                        <div className="pr-kv">
                          <span className="pr-lab">ram free</span>
                          <span className="pr-num">
                            {models.budget.ram_available_gb?.toFixed(1)} /{" "}
                            {models.budget.ram_total_gb?.toFixed(0)} GB
                          </span>
                        </div>
                        <div className="pr-kv">
                          <span className="pr-lab">live for models</span>
                          <span className="pr-num">
                            {models.budget.live_gb?.toFixed(1) ?? "—"} GB
                          </span>
                        </div>
                        <div className="pr-kv">
                          <span className="pr-lab">capacity</span>
                          <span className="pr-num">{models.budget.budget_gb?.toFixed(1)} GB</span>
                        </div>
                        {models.budget.disk_free_gb != null && (
                          <div className="pr-kv">
                            <span className="pr-lab">disk free</span>
                            <span className="pr-num">
                              {models.budget.disk_free_gb.toFixed(1)} GB
                            </span>
                          </div>
                        )}
                        {models.budget.gpu_accel && models.budget.vram_gb > 0 && (
                          <div className="pr-kv">
                            <span className="pr-lab">vram</span>
                            <span className="pr-num">{models.budget.vram_gb?.toFixed(0)} GB</span>
                          </div>
                        )}
                        {models.allowed_count != null && (
                          <p className="pr-budget-note">
                            {models.allowed_count} catalog model
                            {models.allowed_count === 1 ? "" : "s"} fit this hardware
                          </p>
                        )}
                      </div>
                    )}
                    {!models.ollama && (
                      <div className="pr-card pr-empty">
                        Ollama isn't running — local models are offline. Cloud rungs still work.
                      </div>
                    )}
                    {models.active?.enabled && (
                      <div className="pr-card" style={{ padding: "8px 10px" }}>
                        <div className="pr-kv">
                          <span className="pr-lab">fast lane</span>
                          <span className="pr-num">{short(models.active.fast)}</span>
                        </div>
                        <div className="pr-kv" style={{ marginTop: 5 }}>
                          <span className="pr-lab">deep lane</span>
                          <span className="pr-num">{short(models.active.deep)}</span>
                        </div>
                      </div>
                    )}
                    {models.ollama && (
                      <>
                        <div className="pr-sec pr-sec--live">
                          <span className="pr-lab">loaded in memory</span>
                          <span className="pr-live-dot" title="Updates every 2s" />
                        </div>
                        <div className="pr-card pr-loaded">
                          {(models.running?.length ?? 0) === 0 ? (
                            <p className="pr-loaded-empty">
                              Nothing in RAM — Ollama unloads idle models after a few minutes.
                            </p>
                          ) : (
                            models.running!.map((r) => (
                              <div className="pr-kv pr-loaded-row" key={r.name}>
                                <span className="pr-num pr-loaded-name">{r.name}</span>
                                <span className="pr-num">
                                  {r.gb != null ? `${r.gb.toFixed(1)} GB` : "—"} ·{" "}
                                  {r.on_gpu ? "gpu" : "cpu"}
                                </span>
                              </div>
                            ))
                          )}
                          {loadedTs != null && (
                            <p className="pr-loaded-ts">
                              live · {new Date(loadedTs * 1000).toLocaleTimeString()}
                            </p>
                          )}
                        </div>
                      </>
                    )}
                    {rankedList.length > 0 && (
                      <>
                        <div className="pr-sec">
                          <span className="pr-lab">ranked for this machine</span>
                        </div>
                        {rankedList.map((r) => {
                          const inst = findInstalled(r.tag);
                          const isInstalled = Boolean(r.installed || inst);
                          const isActive =
                            inst &&
                            (inst.name === models.active?.fast ||
                              inst.name === models.active?.deep);
                          const pull = pulls[r.tag];
                          const pulling =
                            pull &&
                            pull.status !== "done" &&
                            pull.status !== "error" &&
                            pull.pct < 100;
                          const b = inst ? bench[inst.name] : undefined;
                          return (
                            <div
                              key={r.tag}
                              className={`pr-card pr-model pr-model--ranked ${r.best ? "pr-model--best" : ""} ${isActive ? "pr-model--active" : ""}`}
                            >
                              <div className="pr-rank-col" aria-hidden>
                                <span className="pr-rank-badge">#{r.rank}</span>
                              </div>
                              <div className="pr-model-body">
                                <div className="pr-model-head">
                                  <span className="pr-model-name">{r.tag}</span>
                                  <span className="pr-model-meta">
                                    {r.gb} GB · {r.params}
                                    {r.needs_gb != null ? ` · ~${r.needs_gb}GB` : ""}
                                  </span>
                                </div>
                                {r.fit_note && <div className="pr-model-fit">{r.fit_note}</div>}
                                {r.best_for && <div className="pr-model-desc">{r.best_for}</div>}
                                <div className="pr-model-badges">
                                  {r.best && (
                                    <span className="pr-badge pr-badge--accent">top pick</span>
                                  )}
                                  {isInstalled && (
                                    <span className="pr-badge pr-badge--good">installed</span>
                                  )}
                                  {r.tok_per_sec != null && (
                                    <span className="pr-badge pr-badge--good">
                                      {r.tok_per_sec.toFixed(1)} tok/s
                                    </span>
                                  )}
                                  {r.runnable_now === false && !isInstalled && (
                                    <span
                                      className="pr-badge pr-badge--warn"
                                      title={r.block_reason_now ?? ""}
                                    >
                                      close apps first
                                    </span>
                                  )}
                                  {inst?.runnable === false && (
                                    <span
                                      className="pr-badge pr-badge--warn"
                                      title={inst.block_reason ?? ""}
                                    >
                                      ram tight
                                    </span>
                                  )}
                                  {r.tools && (
                                    <span className="pr-badge pr-badge--good">tools</span>
                                  )}
                                  {isActive && (
                                    <span className="pr-badge pr-badge--accent">active</span>
                                  )}
                                  {b?.status === "running" && (
                                    <span className="pr-badge">benching…</span>
                                  )}
                                  {b?.tok != null && (
                                    <span className="pr-badge pr-badge--good">
                                      {b.tok.toFixed(1)} tok/s
                                    </span>
                                  )}
                                </div>
                                {pulling ? (
                                  <div className="pr-pull">
                                    <span className="pr-meter">
                                      <i style={{ width: `${pull.pct}%` }} />
                                    </span>
                                    <span className="pr-num">{Math.round(pull.pct)}%</span>
                                  </div>
                                ) : isInstalled && inst ? (
                                  <div className="pr-model-actions">
                                    <button
                                      className="pr-btn pr-btn--accent"
                                      disabled={inst.runnable === false || Boolean(isActive)}
                                      onClick={() =>
                                        sendAction("set_local_model", { model: inst.name })
                                      }
                                    >
                                      use
                                    </button>
                                    <button
                                      className="pr-btn"
                                      onClick={() =>
                                        sendAction("benchmark_model", { model: inst.name })
                                      }
                                    >
                                      bench
                                    </button>
                                    <button
                                      className="pr-btn pr-btn--danger"
                                      onClick={() => onDeleteModel(inst.name)}
                                    >
                                      delete
                                    </button>
                                  </div>
                                ) : (
                                  <div className="pr-model-actions">
                                    <button
                                      className="pr-btn pr-btn--accent"
                                      disabled={r.runnable_now === false}
                                      title={
                                        r.runnable_now === false
                                          ? (r.block_reason_now ?? "Not enough free RAM")
                                          : undefined
                                      }
                                      onClick={() => onPullModel(r.tag)}
                                    >
                                      download
                                    </button>
                                  </div>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </>
                    )}
                    {orphanInstalled.length > 0 && (
                      <>
                        <div className="pr-sec">
                          <span className="pr-lab">other installed</span>
                        </div>
                        {orphanInstalled.map((m) => {
                          const isActive =
                            m.name === models.active?.fast || m.name === models.active?.deep;
                          const b = bench[m.name];
                          return (
                            <div
                              key={m.name}
                              className={`pr-card pr-model ${isActive ? "pr-model--active" : ""}`}
                            >
                              <div className="pr-model-head">
                                <span className="pr-model-name">{m.name}</span>
                                <span className="pr-model-meta">
                                  {m.gb != null ? `${m.gb.toFixed(1)} GB` : ""}
                                  {m.params ? ` · ${m.params}` : ""}
                                </span>
                              </div>
                              <div className="pr-model-badges">
                                {m.tools && <span className="pr-badge pr-badge--good">tools</span>}
                                {m.runnable === false && (
                                  <span
                                    className="pr-badge pr-badge--warn"
                                    title={m.block_reason ?? ""}
                                  >
                                    ram tight
                                  </span>
                                )}
                                {b?.tok != null && (
                                  <span className="pr-badge pr-badge--good">
                                    {b.tok.toFixed(1)} tok/s
                                  </span>
                                )}
                              </div>
                              <div className="pr-model-actions">
                                <button
                                  className="pr-btn pr-btn--accent"
                                  disabled={m.runnable === false || isActive}
                                  onClick={() => sendAction("set_local_model", { model: m.name })}
                                >
                                  use
                                </button>
                                <button
                                  className="pr-btn"
                                  onClick={() => sendAction("benchmark_model", { model: m.name })}
                                >
                                  bench
                                </button>
                                <button
                                  className="pr-btn pr-btn--danger"
                                  onClick={() => onDeleteModel(m.name)}
                                >
                                  delete
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      </>
                    )}
                    {models.ollama && (
                      <>
                        <div className="pr-sec">
                          <span className="pr-lab">add any ollama model</span>
                        </div>
                        <div className="pr-card pr-add-model">
                          <p className="pr-add-model-hint">
                            Pull any Ollama tag. Blocked if disk or free RAM is too low.
                          </p>
                          <div className="pr-add-model-row">
                            <input
                              className="pr-add-model-input"
                              type="text"
                              placeholder="name:tag"
                              value={customModel}
                              onChange={(e) => setCustomModel(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" && customModel.trim()) {
                                  onPullModel(customModel.trim());
                                }
                              }}
                            />
                            <button
                              className="pr-btn pr-btn--accent"
                              disabled={!customModel.trim()}
                              onClick={() => onPullModel(customModel.trim())}
                            >
                              pull
                            </button>
                          </div>
                          {customModel.trim() && pulls[customModel.trim()] && (
                            <div className="pr-pull" style={{ marginTop: 6 }}>
                              <span className="pr-meter">
                                <i
                                  style={{
                                    width: `${pulls[customModel.trim()].pct}%`,
                                  }}
                                />
                              </span>
                              <span className="pr-num">
                                {Math.round(pulls[customModel.trim()].pct)}%
                              </span>
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </>
                )}
              </div>
            )}

            {tab === "memory" && (
              <div className="pr-pane">
                <div
                  className="pr-card"
                  style={{
                    padding: "9px 10px",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <span className="pr-lab">{mems.length} durable memories</span>
                  <button
                    className="pr-btn pr-btn--accent"
                    onClick={() => sendAction("trigger_sleep")}
                  >
                    <Moon size={11} style={{ verticalAlign: -2, marginRight: 5 }} />
                    sleep cycle
                  </button>
                </div>
                {sleepMsg && (
                  <p className="pr-pane-note" style={{ color: "var(--p-accent-text)" }}>
                    {sleepMsg}
                  </p>
                )}
                {mems.length === 0 && (
                  <div className="pr-empty">
                    Nothing remembered yet. Say "remember that…" and it lands here — inspectable,
                    deletable, yours.
                  </div>
                )}
                {mems.map((m) => (
                  <div key={m.id} className="pr-card pr-mem">
                    <span
                      className="pr-mem-imp"
                      title={`importance ${m.importance}/10`}
                      aria-hidden
                    >
                      {Array.from({ length: 5 }, (_, i) => (
                        <i key={i} className={i < Math.round(m.importance / 2) ? "on" : ""} />
                      ))}
                    </span>
                    <div className="pr-mem-text">
                      {m.content}
                      <div className="pr-mem-cat">
                        {m.category} · {m.source}
                      </div>
                    </div>
                    <button className="pr-mem-x" title="Forget this" onClick={() => onForget(m.id)}>
                      <Trash2 size={12} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {tab === "ops" && (
              <div className="pr-pane">
                <div className="pr-sec">
                  <span className="pr-lab">tasks</span>
                </div>
                {tasks.length === 0 && (
                  <div className="pr-empty">
                    No tasks queued. Ask JARVIS to "remind me to…" or "queue a task".
                  </div>
                )}
                {[...activeTasks, ...queuedTasks].map((t) => (
                  <div
                    key={t.id}
                    className={`pr-card pr-task ${t.status === "active" ? "pr-task--active" : ""}`}
                  >
                    <span className="pr-task-dot" />
                    <div>
                      <div className="pr-task-label">{t.t}</div>
                      <div className="pr-task-meta">
                        {t.status}
                        {t.eta ? ` · ${t.eta}` : ""}
                      </div>
                    </div>
                  </div>
                ))}
                {doneTasks.map((t) => (
                  <div key={t.id} className="pr-card pr-task pr-task--done">
                    <span className="pr-task-dot" />
                    <div>
                      <div className="pr-task-label">{t.t}</div>
                    </div>
                  </div>
                ))}
                <div className="pr-sec">
                  <span className="pr-lab">recent tool activity</span>
                </div>
                {trace.length === 0 && (
                  <div className="pr-empty">No tool runs yet this session.</div>
                )}
                {trace.map((s, i) => (
                  <div key={`${s.step}-${i}`} className="pr-card pr-trace">
                    <div className="pr-trace-head">
                      <span className="pr-trace-step">#{s.step}</span>
                      <span className="pr-trace-action">{s.action}</span>
                    </div>
                    {s.observation && (
                      <div className="pr-trace-obs">{s.observation.slice(0, 220)}</div>
                    )}
                  </div>
                ))}
                <div className="pr-sec">
                  <span className="pr-lab">{tools.length} capabilities</span>
                </div>
                <div className="pr-caps">
                  {tools.map((t) => (
                    <span key={t.name} className="pr-cap" title={t.description}>
                      {t.name}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {tab === "markets" && (
              <div className="pr-pane">
                <div className="pr-mkt-syms">
                  {SYMBOLS.map((s) => (
                    <button
                      key={s}
                      className={`pr-mode ${mktSym === s ? "pr-mode--on" : ""}`}
                      onClick={() => setMktSym(s)}
                    >
                      {s}
                    </button>
                  ))}
                </div>
                <button
                  className={`pr-btn ${watching ? "pr-btn--accent" : ""}`}
                  style={{ alignSelf: "stretch" }}
                  onClick={() => sendAction(watching ? "stop_watch" : "start_watch")}
                >
                  {watching
                    ? `watcher live · ${status.watch?.interval_min ?? "?"}m ${status.watch?.tf ?? ""}`
                    : "start ict watcher"}
                </button>
                {mktLoading && !mkt && <div className="pr-empty">reading price…</div>}
                {mkt && !mkt.ok && (
                  <div className="pr-card pr-empty">{mkt.error ?? "Market read failed."}</div>
                )}
                {mkt?.ok && (
                  <>
                    <div className="pr-card">
                      <div className="pr-mkt-bias">
                        <span className="pr-num">{mkt.last?.toLocaleString("en-IN") ?? "—"}</span>
                        <span className={`pr-bias-word pr-bias-word--${mkt.bias ?? "neutral"}`}>
                          {mkt.bias ?? "neutral"}
                          {typeof mkt.score === "number" ? ` · ${mkt.score}` : ""}
                        </span>
                      </div>
                      {mkt.read && <div className="pr-mkt-read">{mkt.read}</div>}
                      <div className="pr-mkt-rows">
                        {mkt.structure && (
                          <div className="pr-kv">
                            <span className="pr-lab">structure</span>
                            <span className="pr-num">{mkt.structure}</span>
                          </div>
                        )}
                        {mkt.zone && (
                          <div className="pr-kv">
                            <span className="pr-lab">zone</span>
                            <span className="pr-num">
                              {mkt.zone}
                              {mkt.equilibrium
                                ? ` · eq ${mkt.equilibrium.toLocaleString("en-IN")}`
                                : ""}
                            </span>
                          </div>
                        )}
                        {mkt.htf_bias && (
                          <div className="pr-kv">
                            <span className="pr-lab">htf bias</span>
                            <span className="pr-num">{mkt.htf_bias}</span>
                          </div>
                        )}
                        {mkt.sweep && (
                          <div className="pr-kv">
                            <span className="pr-lab">sweep</span>
                            <span className="pr-num">{mkt.sweep}</span>
                          </div>
                        )}
                        {mkt.order_block && (
                          <div className="pr-kv">
                            <span className="pr-lab">order block</span>
                            <span className="pr-num">{mkt.order_block}</span>
                          </div>
                        )}
                        {mkt.session && (
                          <div className="pr-kv">
                            <span className="pr-lab">session</span>
                            <span className="pr-num">
                              {mkt.session.open ? "open" : "closed"} · {mkt.session.ist}
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                    {mkt.plan && (
                      <div className="pr-card" style={{ padding: "9px 10px" }}>
                        <div className="pr-kv">
                          <span className="pr-lab">plan · {mkt.plan.side}</span>
                          <span className="pr-num">
                            {mkt.plan.entry ? `in ${mkt.plan.entry}` : ""}
                            {mkt.plan.sl ? ` · sl ${mkt.plan.sl}` : ""}
                            {mkt.plan.tp ? ` · tp ${mkt.plan.tp}` : ""}
                            {mkt.plan.rr ? ` · rr ${mkt.plan.rr}` : ""}
                          </span>
                        </div>
                        {mkt.plan.text && (
                          <p className="pr-pane-note" style={{ marginTop: 6 }}>
                            {mkt.plan.text}
                          </p>
                        )}
                      </div>
                    )}
                  </>
                )}
                {feed
                  .filter((e): e is Extract<Feed, { kind: "alert" }> => e.kind === "alert")
                  .slice(-4)
                  .reverse()
                  .map((e) => (
                    <div key={e.id} className="pr-card pr-alert">
                      <div className="pr-alert-head">
                        <span className="pr-alert-sym">⚑ {e.symbol}</span>
                        <span className="pr-alert-at">{e.at}</span>
                      </div>
                      <div className="pr-alert-text">{e.text}</div>
                    </div>
                  ))}
              </div>
            )}
          </div>
        </aside>
      </div>

      {settingsOpen && (
        <SettingsModal
          name={status.user?.name ?? ""}
          voice={status.voice?.current ?? ""}
          options={status.voice?.options ?? []}
          sttHint={status.voice?.stt_hint ?? null}
          onClose={() => setSettingsOpen(false)}
          onSave={(name, voice) => {
            if (name.trim()) sendAction("set_name", { name: name.trim() });
            if (voice) sendAction("set_voice", { voice });
            setSettingsOpen(false);
          }}
        />
      )}
    </div>
  );
}

/* ── feed row ────────────────────────────────────────────── */
function FeedRow({ e }: { e: Feed }) {
  if (e.kind === "user" || e.kind === "agent" || e.kind === "system") {
    return (
      <div className={`pr-entry pr-entry--${e.kind}`}>
        <span className="pr-entry-at">{e.at}</span>
        <div className="pr-entry-body">
          <div className="pr-entry-who">
            {e.kind === "agent" ? "jarvis" : e.kind === "user" ? "you" : "sys"}
          </div>
          <div className="pr-entry-text">{e.text}</div>
        </div>
      </div>
    );
  }
  if (e.kind === "tool") {
    const argStr = Object.entries(e.args ?? {})
      .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
      .join("  ");
    return (
      <div className="pr-entry">
        <span className="pr-entry-at">{e.at}</span>
        <details>
          <summary>
            <span className="pr-mrow">
              <span className="pr-mrow-tag pr-mrow-tag--tool">tool</span>
              <span className="pr-mrow-text">
                <b style={{ color: "var(--p-mid)", fontWeight: 400 }}>{e.action}</b> —{" "}
                {e.observation.slice(0, 110)}
                {e.observation.length > 110 ? "…" : ""}
              </span>
            </span>
          </summary>
          <div className="pr-mrow-detail">
            {argStr && (
              <>
                <b>args</b> {argStr}
                <br />
              </>
            )}
            <b>result</b> {e.observation}
          </div>
        </details>
      </div>
    );
  }
  if (e.kind === "route") {
    return (
      <div className="pr-entry">
        <span className="pr-entry-at">{e.at}</span>
        <span className="pr-mrow">
          <span className="pr-mrow-tag pr-mrow-tag--route">route</span>
          <span className="pr-mrow-text">Used {e.d.label}</span>
        </span>
      </div>
    );
  }
  if (e.kind === "alert") {
    return (
      <div className="pr-entry">
        <span className="pr-entry-at">{e.at}</span>
        <span className="pr-mrow">
          <span className="pr-mrow-tag pr-mrow-tag--alert">{e.symbol}</span>
          <span className="pr-mrow-text">{e.text}</span>
        </span>
      </div>
    );
  }
  return null;
}

function SettingsModal({
  name,
  voice,
  options,
  sttHint,
  onClose,
  onSave,
}: {
  name: string;
  voice: string;
  options: { id: string; label: string }[];
  sttHint?: string | null;
  onClose: () => void;
  onSave: (name: string, voice: string) => void;
}) {
  const [nm, setNm] = useState(name);
  const [vc, setVc] = useState(voice);
  return (
    <div className="pr-overlay" onClick={onClose}>
      <div
        className="pr-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Settings"
      >
        <div className="pr-modal-head">
          <span className="pr-lab" style={{ fontSize: 9, color: "var(--p-accent-text)" }}>
            settings
          </span>
          <button className="pr-iconbtn" onClick={onClose} title="Close">
            <X size={13} />
          </button>
        </div>
        <div className="pr-modal-body">
          <div className="pr-field">
            <label className="pr-lab" htmlFor="pr-name">
              what should jarvis call you
            </label>
            <input
              id="pr-name"
              value={nm}
              onChange={(e) => setNm(e.target.value)}
              placeholder="Your name"
              maxLength={40}
            />
          </div>
          {options.length > 0 && (
            <div className="pr-field">
              <label className="pr-lab">voice (speak out)</label>
              <p className="pr-modal-hint">
                Uses free Edge TTS — no API key. Save to hear a test phrase.
              </p>
              {sttHint && <p className="pr-modal-hint pr-modal-hint--warn">{sttHint}</p>}
              <div style={{ display: "flex", flexDirection: "column", gap: 5, marginTop: 5 }}>
                {options.map((o) => (
                  <button
                    key={o.id}
                    className={`pr-voice-opt ${vc === o.id ? "pr-voice-opt--on" : ""}`}
                    onClick={() => setVc(o.id)}
                  >
                    <Volume2 size={13} />
                    {o.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
        <div className="pr-modal-foot">
          <button className="pr-btn" onClick={onClose}>
            cancel
          </button>
          <button className="pr-btn pr-btn--accent" onClick={() => onSave(nm, vc)}>
            save
          </button>
        </div>
      </div>
    </div>
  );
}
