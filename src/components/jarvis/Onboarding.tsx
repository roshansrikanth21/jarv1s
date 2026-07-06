import { AnimatePresence, motion } from "framer-motion";
import { ArrowRight, Check, Cpu, Download, HardDrive, KeyRound, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArcReactor } from "@/components/jarvis/ArcReactor";

// First-run wizard, rendered at the route level before either deck. Three steps:
// name → pick a local brain (compute-aware advisor) → optional cloud key. Finishing
// sends set_name (which marks the backend "onboarded") and hands control to the app.

type ModelRec = {
  tag: string; params: string; gb: number; ctx?: string; tools?: boolean;
  best?: boolean; installed?: boolean; best_for?: string; limits?: string;
  needs_gb?: number;
};
type InstalledModel = { name: string; gb?: number | null; params?: string | null; tools?: boolean | null;
  runnable?: boolean; block_reason?: string | null };
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
  installed?: InstalledModel[]; recommended?: ModelRec[];
  active?: { fast: string; deep: string; enabled: boolean };
  budget?: ModelBudget;
};
type DeviceBrief = { ram_gb?: number; tier?: string; gpus?: { name: string; vendor?: string }[]; cpu?: { name?: string } };
type KeyStatus = { secure: boolean; groq: boolean; anthropic: boolean };
type Pull = { status: string; pct: number };

const AMBER = "var(--c-amber)";
const GREEN = "oklch(0.72 0.17 150)";

export function Onboarding({ onComplete }: { onComplete: (name: string) => void }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [models, setModels] = useState<ModelsData | null>(null);
  const [device, setDevice] = useState<DeviceBrief | null>(null);
  const [pulls, setPulls] = useState<Record<string, Pull>>({});
  const [groqKey, setGroqKey] = useState("");
  const [keyStatus, setKeyStatus] = useState<KeyStatus | null>(null);
  const [keyErr, setKeyErr] = useState("");
  const [pullErr, setPullErr] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const hasElectron = typeof window !== "undefined" && !!window.electronAPI?.setApiKeys;

  const fetchModels = useCallback(async () => {
    try { const r = await fetch("/api/models"); setModels(await r.json()); } catch { /* ignore */ }
  }, []);

  // Open a WS for pull progress + set_name; pull live model/device info.
  useEffect(() => {
    fetchModels();
    fetch("/api/device").then(r => r.json()).then(setDevice).catch(() => {});
    if (hasElectron) window.electronAPI!.getApiKeyStatus!().then(setKeyStatus).catch(() => {});

    const ws = new WebSocket(`${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === "model_pull") {
          const done = d.status === "success" || d.status === "done" || (Number(d.pct) || 0) >= 100;
          const err = d.status === "error" ? String(d.error ?? "Download blocked for this device.") : "";
          setPulls(p => ({ ...p, [String(d.model)]: { status: String(d.status ?? ""), pct: Number(d.pct) || 0 } }));
          if (err) setPullErr(p => ({ ...p, [String(d.model)]: err }));
          if (done) fetchModels();
        }
      } catch { /* ignore */ }
    };
    return () => ws.close();
  }, [fetchModels, hasElectron]);

  const pull = (tag: string) => {
    if (!(models?.recommended ?? []).some(r => r.tag === tag)) return;
    setPullErr(p => { const n = { ...p }; delete n[tag]; return n; });
    setPulls(p => ({ ...p, [tag]: { status: "starting", pct: 0 } }));
    wsRef.current?.send(JSON.stringify({ action: "pull_model", model: tag }));
  };

  const installedRunnable = (models?.installed ?? []).filter(m => m.tools && m.runnable !== false);
  const hasBrain = installedRunnable.length > 0 || Boolean(keyStatus?.groq) || Boolean(keyStatus?.anthropic);

  const finish = async () => {
    setSaving(true);
    if (hasElectron && groqKey.trim()) {
      try { setKeyStatus(await window.electronAPI!.setApiKeys!({ GROQ_API_KEY: groqKey.trim() })); }
      catch (e) { setKeyErr(e instanceof Error ? e.message : "Couldn't save key."); setSaving(false); return; }
    }

    // Refresh models so we pin whatever just finished downloading.
    let latest = models;
    try {
      const r = await fetch("/api/models");
      latest = await r.json();
      setModels(latest);
    } catch { /* use cached */ }

    const wsSend = (payload: object) => {
      const raw = JSON.stringify(payload);
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(raw);
      else wsRef.current?.addEventListener("open", () => wsRef.current?.send(raw), { once: true });
    };

    wsSend({ action: "set_name", name: name.trim() });
    try { localStorage.setItem("jarvis_user_name", name.trim()); } catch { /* ignore */ }

    const runnableLatest = (latest?.installed ?? []).filter(m => m.tools && m.runnable !== false);
    const bestRec = (latest?.recommended ?? []).find(r => r.best && r.installed)
      ?? (latest?.recommended ?? []).find(r => r.installed);
    const pinTag = bestRec?.tag
      ?? runnableLatest.sort((a, b) => (b.gb ?? 0) - (a.gb ?? 0))[0]?.name;
    const pinAllowed = pinTag && (
      (latest?.recommended ?? []).some(r => r.tag === pinTag)
      || runnableLatest.some(m => m.name === pinTag)
    );
    if (pinTag && pinAllowed) wsSend({ action: "set_local_model", model: pinTag });

    setTimeout(() => onComplete(name.trim()), 300);
  };

  const next = () => {
    if (step === 0) setStep(1);
    else if (step === 1) { if (hasElectron) setStep(2); else finish(); }
  };

  return (
    <div className="onb-root">
      <div className="onb-bg-grid" />
      <motion.div className="onb-card"
        initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, ease: "easeOut" }}>
        <div className="onb-stepper">
          {["You", "Local brain", hasElectron ? "Cloud key" : null].filter(Boolean).map((label, i) => (
            <div key={i} className={`onb-step ${i === step ? "is-active" : i < step ? "is-done" : ""}`}>
              <span className="onb-step-dot">{i < step ? <Check size={11} /> : i + 1}</span>
              <span className="onb-step-label">{label}</span>
            </div>
          ))}
        </div>

        <AnimatePresence mode="wait">
          {step === 0 && (
            <motion.div key="name" className="onb-pane"
              initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -12 }} transition={{ duration: 0.22 }}>
              <div className="onb-reactor"><ArcReactor active size="sm" /></div>
              <h1 className="onb-title">I'm JARVIS.</h1>
              <p className="onb-sub">
                Your personal AI — I run on your machine and scale the model to your hardware.
                Let's get set up. What should I call you?
              </p>
              <input
                className="onb-input" value={name} autoFocus spellCheck={false} maxLength={40}
                placeholder="e.g. Tony"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) next(); }}
              />
              <div className="onb-actions">
                <span />
                <button className="onb-btn onb-btn--primary" disabled={!name.trim()} onClick={next}>
                  Continue <ArrowRight size={14} />
                </button>
              </div>
            </motion.div>
          )}

          {step === 1 && (
            <motion.div key="models" className="onb-pane"
              initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -12 }} transition={{ duration: 0.22 }}>
              <h1 className="onb-title">Pick your local brain</h1>
              <p className="onb-sub">
                Matched to your CPU, GPU, and RAM — only models your machine can actually run well.
                The <b style={{ color: AMBER }}>BEST</b> pick balances quality and speed on your hardware.
              </p>

              <div className="onb-rig">
                <span><Cpu size={12} /> {device?.cpu?.name ? shortCpu(device.cpu.name) : "CPU"}
                  {models?.budget?.cpu_cores ? ` · ${models.budget.cpu_cores}c` : ""}
                  {models?.budget?.cpu_ghz ? ` @ ${models.budget.cpu_ghz}GHz` : ""}
                </span>
                <span><HardDrive size={12} /> {models?.budget
                  ? `${models.budget.ram_total_gb}GB RAM · ~${models.budget.budget_gb}GB for models`
                  : device?.ram_gb ? `${device.ram_gb}GB RAM` : "—"}</span>
                <span className="onb-rig-tier">
                  {models?.budget?.gpu_name
                    ? shortGpu(models.budget.gpu_name)
                    : models?.budget?.gpu_accel ? "GPU" : "CPU inference"}
                  {models?.budget?.compute_label ? ` · ${models.budget.compute_label} compute` : ""}
                </span>
              </div>
              {models?.budget?.instant_tight && (
                <div className="onb-note onb-note--warn">
                  RAM is tight right now ({models.budget.ram_available_gb}GB free) — close a few apps before downloading a large model.
                </div>
              )}

              {models && models.budget && !models.budget.local_viable && (
                <div className="onb-note onb-note--warn">
                  Not enough RAM for a local model on this PC (~{models.budget.budget_gb}GB budget after system reserve).
                  Skip this step and add a free <b style={{ color: AMBER }}>Groq</b> key next — JARVIS will run in the cloud.
                </div>
              )}

              {models && !models.ollama && (
                <div className="onb-note onb-note--warn">
                  Ollama isn't running. Install it from <button className="onb-link" onClick={() => openExt("https://ollama.com/download")}>ollama.com ↗</button> and
                  reopen JARVIS — or just add a cloud key on the next step.
                </div>
              )}

              {installedRunnable.length > 0 && (
                <div className="onb-note onb-note--ok">
                  <Check size={12} /> You already have {installedRunnable.map(m => m.name.split(":")[0]).join(", ")} — you're ready.
                </div>
              )}

              <div className="onb-models">
                {(models?.recommended ?? []).length === 0 && models?.budget?.local_viable === false ? null : (models?.recommended ?? []).map((rec) => {
                  const p = pulls[rec.tag];
                  const installing = p && p.status !== "success" && p.status !== "done" && p.pct < 100;
                  return (
                    <div key={rec.tag} className={`onb-model ${rec.best ? "is-best" : ""}`}>
                      <div className="onb-model-head">
                        <span className="onb-model-name">{rec.tag}</span>
                        <div className="onb-model-tags">
                          {rec.best && <span className="onb-tag onb-tag--best">best</span>}
                          {rec.tools && <span className="onb-tag onb-tag--tools">tools</span>}
                        </div>
                      </div>
                      <div className="onb-model-meta">
                        {rec.params} · {rec.gb}GB disk{rec.needs_gb ? ` · ~${rec.needs_gb}GB RAM` : ""}{rec.ctx ? ` · ${rec.ctx} ctx` : ""}
                      </div>
                      {rec.best_for && <div className="onb-model-for">{rec.best_for}</div>}
                      {rec.limits && <div className="onb-model-lim">Trade-off: {rec.limits}</div>}
                      <div className="onb-model-foot">
                        {rec.installed ? (
                          <span className="onb-installed"><Check size={12} /> Installed</span>
                        ) : installing ? (
                          <div className="onb-prog">
                            <div className="onb-prog-bar"><div className="onb-prog-fill" style={{ width: `${p.pct}%` }} /></div>
                            <span>{p.pct > 0 ? `${p.pct}%` : "starting…"}</span>
                          </div>
                        ) : p && (p.status === "error") ? (
                          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                            {pullErr[rec.tag] && <span style={{ fontSize: 10, color: "var(--c-warn)", lineHeight: 1.35 }}>{pullErr[rec.tag]}</span>}
                            <button className="onb-btn onb-btn--ghost" onClick={() => pull(rec.tag)}>retry</button>
                          </div>
                        ) : (
                          <button className="onb-btn onb-btn--ghost" disabled={!models?.ollama} onClick={() => pull(rec.tag)}>
                            <Download size={13} /> Download
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="onb-actions">
                <button className="onb-btn onb-btn--text" onClick={() => setStep(0)}>Back</button>
                <button className="onb-btn onb-btn--primary" onClick={next}>
                  {hasBrain ? "Continue" : "Skip for now"} <ArrowRight size={14} />
                </button>
              </div>
            </motion.div>
          )}

          {step === 2 && (
            <motion.div key="key" className="onb-pane"
              initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -12 }} transition={{ duration: 0.22 }}>
              <div className="onb-reactor"><Sparkles size={26} color={AMBER} /></div>
              <h1 className="onb-title">Want faster, smarter answers?</h1>
              <p className="onb-sub">
                Add a free <b style={{ color: AMBER }}>Groq</b> key and I'll route hard questions to the cloud —
                replies in a second or two instead of tens of seconds. Stored encrypted in your OS keychain, never shared.
                Totally optional.
              </p>
              <div className="onb-key">
                <div className="onb-key-head">
                  <span><KeyRound size={12} /> Groq API key {keyStatus?.groq && <span style={{ color: GREEN }}>· set</span>}</span>
                  <button className="onb-link" onClick={() => openExt("https://console.groq.com/keys")}>Get a free key ↗</button>
                </div>
                <input
                  className="onb-input" type="password" autoComplete="off" spellCheck={false}
                  value={groqKey} placeholder={keyStatus?.groq ? "•••••••• (leave blank to keep)" : "gsk_…"}
                  onChange={(e) => setGroqKey(e.target.value)}
                />
                {keyErr && <div className="onb-note onb-note--warn">{keyErr}</div>}
              </div>
              <div className="onb-actions">
                <button className="onb-btn onb-btn--text" onClick={() => setStep(1)}>Back</button>
                <button className="onb-btn onb-btn--primary" disabled={saving} onClick={finish}>
                  {saving ? "Setting up…" : "Enter JARVIS"} <ArrowRight size={14} />
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );

  function openExt(url: string) {
    if (window.electronAPI?.openExternal) window.electronAPI.openExternal(url);
    else window.open(url, "_blank", "noopener,noreferrer");
  }
}

function shortCpu(name: string): string {
  const m = name.match(/(Intel|AMD|Apple)[^,]*/i);
  return (m ? m[0] : name).replace(/\(R\)|\(TM\)|CPU|Processor/gi, "").replace(/\s+/g, " ").trim().slice(0, 22);
}

function shortGpu(name: string): string {
  return name.replace(/\(R\)|\(TM\)|Graphics|GPU/gi, "").replace(/\s+/g, " ").trim().slice(0, 20);
}
