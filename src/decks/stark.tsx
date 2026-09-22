import { motion } from "framer-motion";
import { useEffect, useRef, useState, useMemo } from "react";
import { WindowControls } from "@/components/jarvis/WindowControls";
import { useJarvisSocket, type Role } from "@/hooks/useJarvisSocket";

const C = {
  bg: "#0a0e14",
  surface: "#0f1319",
  border: "#1c2430",
  line: "#283040",
  text: "#8a96a6",
  bright: "#c8d0dc",
  orb: "#e8dcc8",
  orbGlow: "#c4b090",
  dim: "#4a5568",
  mute: "#1c2430",
  bar: "#707a88",
  barBg: "#161c26",
  warm: "#d4c8b0",
  amber: "#c8a050",
  amberDim: "#a08030",
  btnBg: "#181e28",
  btnHi: "#2a2820",
  btnHiBorder: "#7a6a40",
};

const fade = (d = 0) => ({ hidden: { opacity: 0 }, visible: { opacity: 1, transition: { duration: 0.7, delay: d } } });
const scaleIn = (d = 0) => ({ hidden: { scale: 0, opacity: 0 }, visible: { scale: 1, opacity: 1, transition: { duration: 1.2, delay: d, ease: [0.16, 1, 0.3, 1] } } });
const slideL = (d = 0) => ({ hidden: { opacity: 0, x: -30 }, visible: { opacity: 1, x: 0, transition: { duration: 0.6, delay: d, ease: "easeOut" } } });
const slideR = (d = 0) => ({ hidden: { opacity: 0, x: 30 }, visible: { opacity: 1, x: 0, transition: { duration: 0.6, delay: d, ease: "easeOut" } } });
const drawH = (d = 0) => ({ hidden: { scaleX: 0, opacity: 0 }, visible: { scaleX: 1, opacity: 1, transition: { duration: 0.8, delay: d, ease: "easeOut" } } });

type Dev = { cpu: number; mem: number; bat: number };
type Amb = { city: string; temp: number; desc: string };

export default function StarkDeck() {
  const { connected, listening, speaking, lines, stream, level, send, toggleMic, sendAction, showReconnectHint } = useJarvisSocket("Online.");
  const [input, setInput] = useState("");
  const [dev, setDev] = useState<Dev>({ cpu: 0, mem: 0, bat: 0 });
  const [amb, setAmb] = useState<Amb>({ city: "—", temp: 0, desc: "" });
  const [clock, setClock] = useState("");
  const [activeTab, setActiveTab] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => { scrollRef.current?.scrollTo({ top: 9e6, behavior: "smooth" }); }, [lines.length, stream]);
  useEffect(() => { const t = setInterval(() => setClock(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })), 1000); return () => clearInterval(t); }, []);

  useEffect(() => {
    let alive = true;
    const pull = () => fetch("/api/device").then(r => r.json()).then(d => {
      if (!alive) return;
      setDev({ cpu: Math.round(d?.cpu_percent ?? 0), mem: Math.round(d?.ram_percent ?? 0), bat: Math.round(d?.battery?.percent ?? 0) });
    }).catch(() => {});
    pull(); const id = setInterval(pull, 4000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  useEffect(() => {
    fetch("/api/status").then(r => r.json()).then(d => {
      const a = d?.ambient;
      if (a) setAmb({ city: a.city ?? "—", temp: Math.round(a.temperature_c ?? a.temp ?? 0), desc: a.description ?? a.condition ?? "" });
    }).catch(() => {});
  }, []);

  const submit = () => { if (input.trim()) { send(input); setInput(""); } };
  const thinking = !speaking && !listening && stream !== "";
  const state = speaking ? "speaking" : listening ? "listening" : thinking ? "thinking" : "idle";
  const active = state !== "idle";

  const coreBars = useMemo(() => {
    const b = dev.cpu / 100;
    return Array.from({ length: 10 }, (_, i) => {
      const n = Math.sin(i * 1.8 + dev.cpu * 0.1) * 0.35;
      return Math.max(0.06, Math.min(1, b + n * b + 0.04));
    });
  }, [dev.cpu]);

  const tempF = Math.round(amb.temp * 9 / 5 + 32);

  const MODS = [
    { sv: "sun", l: "Light", c: "give me a full system status report", hi: true },
    { sv: "lamp", l: "Light", c: "what do you remember about me" },
    { sv: "shield", l: "Security", c: "scan for security issues" },
    { sv: "speaker", l: "Noss", c: "toggle voice feedback" },
    { sv: "gear", l: "Settings", c: "show me your current configuration" },
    { sv: "monitor", l: "Bookmarks", c: "what is on my screen right now" },
    { sv: "thermo", l: "Proner", c: "get me the latest tech news" },
    { sv: "dots", l: "More", c: "what tools do you have" },
  ];

  return (
    <div style={{ position: "fixed", inset: 0, background: C.bg, overflow: "hidden", fontFamily: "'JetBrains Mono','SF Mono',ui-monospace,monospace", color: C.text }}>
      <div aria-hidden className="sk-grid" />
      <div aria-hidden className="sk-cross" />

      {/* FRAME BRACKETS */}
      <motion.div variants={fade(0.1)} initial="hidden" animate="visible"><FrameOverlay /></motion.div>

      {/* TOP HORIZONTAL LINE */}
      <motion.div variants={drawH(0.3)} initial="hidden" animate="visible"
        style={{ position: "absolute", top: 46, left: "26%", right: "26%", height: 1, background: C.line, transformOrigin: "center", zIndex: 3 }}>
        <div style={{ position: "absolute", top: -3, left: "50%", transform: "translateX(-50%)", width: 6, height: 6, background: C.line, borderRadius: "50%" }} />
      </motion.div>
      {/* BOTTOM HORIZONTAL LINE */}
      <motion.div variants={drawH(0.3)} initial="hidden" animate="visible"
        style={{ position: "absolute", bottom: 46, left: "26%", right: "26%", height: 1, background: C.line, transformOrigin: "center", zIndex: 3 }}>
        <div style={{ position: "absolute", top: -3, left: "50%", transform: "translateX(-50%)", width: 6, height: 6, background: C.line, borderRadius: "50%" }} />
      </motion.div>

      {/* TOP DATA STRINGS */}
      <motion.div variants={fade(0.5)} initial="hidden" animate="visible"
        style={{ position: "absolute", top: 32, left: 0, right: 0, display: "flex", justifyContent: "center", gap: 220, fontSize: 7, color: C.dim, letterSpacing: "0.12em", zIndex: 4 }}>
        <span>095350888</span><span>890899808</span>
      </motion.div>

      {/* HEADER (window controls) */}
      <div className="drag" style={{ position: "absolute", top: 8, right: 30, zIndex: 20 }}>
        <div className="no-drag"><WindowControls accent={C.orb} /></div>
      </div>

      {/* ═══ 3-COLUMN LAYOUT ═══ */}
      <div style={{ position: "absolute", top: 54, bottom: 84, left: 20, right: 20, display: "grid", gridTemplateColumns: "minmax(220px, 270px) 1fr minmax(230px, 270px)", gap: 10, zIndex: 5 }}>

        {/* ═══ LEFT COLUMN ═══ */}
        <div style={{ display: "flex", flexDirection: "column", gap: 5, paddingTop: 8, overflow: "hidden" }}>

          {/* CPU HISTOGRAM */}
          <motion.div variants={slideL(0.8)} initial="hidden" animate="visible">
            <div style={{ padding: "6px 0" }}>
              <div style={{ display: "flex", gap: 2, alignItems: "flex-end", height: 68 }}>
                {coreBars.map((v, i) => (
                  <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center" }}>
                    <div style={{ width: "100%", background: `linear-gradient(to top, ${C.amberDim}, ${C.amber})`, transition: "height 0.5s", height: `${v * 68}px` }} />
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", gap: 2, marginTop: 2, fontSize: 5, color: C.dim }}>
                {coreBars.map((v, i) => <span key={i} style={{ flex: 1, textAlign: "center" }}>{Math.round(v * 100)}%</span>)}
              </div>
              <div style={{ marginTop: 5, height: 2, background: C.barBg, position: "relative" }}>
                <div style={{ height: "100%", width: `${dev.cpu}%`, background: C.bar, transition: "width 0.5s" }} />
                <div style={{ position: "absolute", right: 0, top: -2, width: 5, height: 5, background: C.dim, borderRadius: "50%" }} />
              </div>
            </div>
          </motion.div>

          {/* SYSTEM BARS */}
          <motion.div variants={slideL(1.0)} initial="hidden" animate="visible">
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <SysBar label="CPU" value={dev.cpu} />
              <SysBar label="GPU" value={dev.cpu > 0 ? Math.min(100, dev.cpu + 8) : 0} />
              <SysBar label="MEMORY" value={dev.mem} />
              <SysBar label="REMOTE" value={Math.min(100, Math.round(dev.cpu * 0.6))} />
              <SysBar label="RESOURCE" value={dev.bat} />
            </div>
          </motion.div>

          {/* AMBIENT WEATHER */}
          <motion.div variants={slideL(1.3)} initial="hidden" animate="visible">
            <div style={{ border: `1px solid ${C.border}`, padding: "8px 10px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 6, color: C.dim, marginBottom: 1 }}>
                <div><span style={{ letterSpacing: "0.2em", fontSize: 7 }}>AMBIENT</span><br />{amb.city}</div>
                <div style={{ textAlign: "right" }}>06:00:080<br />Wed, {clock}</div>
              </div>
              <div style={{ fontSize: 16, fontWeight: 300, color: C.bright, margin: "4px 0 6px" }}>Weather</div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                {["rain", "cloud", "cloud", "partly", "snow"].map((w, i) => (
                  <div key={i} style={{ textAlign: "center" }}>
                    <WxIcon type={w} />
                    <div style={{ fontSize: 6, color: C.dim, marginTop: 2 }}>-{i + 1}°</div>
                  </div>
                ))}
              </div>
            </div>
          </motion.div>

          {/* TEMP + LOCAL TIME SPLIT */}
          <motion.div variants={slideL(1.5)} initial="hidden" animate="visible">
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", border: `1px solid ${C.border}` }}>
              <div style={{ padding: "8px 10px", borderRight: `1px solid ${C.border}`, display: "flex", alignItems: "center", gap: 8 }}>
                <WxIcon type="partly" size={28} />
                <div>
                  <div style={{ fontSize: 16, fontWeight: 200, color: C.bright }}>{tempF} °F</div>
                  <div style={{ fontSize: 6, color: C.dim }}>01001 080</div>
                </div>
              </div>
              <div style={{ padding: "8px 10px" }}>
                <div style={{ fontSize: 7, color: C.dim }}>Local Time</div>
                <div style={{ fontSize: 20, fontWeight: 200, color: C.bright, lineHeight: 1.1 }}>14°</div>
                <div style={{ fontSize: 6, color: C.dim }}>Monday &nbsp; °F</div>
                <div style={{ fontSize: 6, color: C.dim }}>06:36</div>
              </div>
            </div>
          </motion.div>

          {/* FORECAST */}
          <motion.div variants={slideL(1.7)} initial="hidden" animate="visible">
            <div style={{ border: `1px solid ${C.border}`, padding: "8px 10px", background: C.surface }}>
              <div style={{ fontSize: 7, color: C.dim, letterSpacing: "0.15em", marginBottom: 3 }}>Local Time</div>
              <div style={{ fontSize: 20, fontWeight: 200, color: C.bright, lineHeight: 1.1 }}>18° FF</div>
              <div style={{ display: "flex", marginTop: 6, fontSize: 6 }}>
                <span style={{ width: 60, color: C.text }}>Saturday</span>
                {["MON", "THU", "FRI", "BBR", "SAT"].map(d => <span key={d} style={{ width: 34, color: C.dim }}>{d}</span>)}
              </div>
              <div style={{ display: "flex", fontSize: 6, color: C.dim }}>
                <span style={{ width: 60 }}>Saturday</span>
                {["36°", "34°", "29°", "28°", "26°"].map((t, i) => <span key={i} style={{ width: 34 }}>{t}</span>)}
              </div>
            </div>
          </motion.div>

          {/* VOICE WAVEFORM */}
          <motion.div variants={slideL(1.9)} initial="hidden" animate="visible">
            <div style={{ border: `1px solid ${C.border}`, padding: "8px 10px", background: C.surface }}>
              <div style={{ fontSize: 7, color: C.dim, letterSpacing: "0.15em", marginBottom: 6 }}>Local Time</div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <button onClick={toggleMic} style={{ background: "transparent", border: "none", cursor: "pointer", color: C.text, fontSize: 18, padding: 0, lineHeight: 1 }}>▶</button>
                <WaveformBars level={level} active={speaking || listening} />
              </div>
              <div style={{ display: "flex", marginTop: 4, fontSize: 5, color: C.dim }}>
                {["MON", "TUE", "FRI", "NOON", "WODN", "MOON"].map(d => <span key={d} style={{ flex: 1 }}>{d}</span>)}
              </div>
            </div>
          </motion.div>
        </div>

        {/* ═══ CENTER: ORB ═══ */}
        <motion.div variants={scaleIn(0.4)} initial="hidden" animate="visible"
          style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", position: "relative" }}>
          <CentralOrb state={state} level={level} onClick={toggleMic} />
          <motion.div variants={fade(2.0)} initial="hidden" animate="visible"
            style={{ fontSize: 7, letterSpacing: "0.4em", color: C.dim, marginTop: 6, textAlign: "center" }}>
            {state === "speaking" ? "◂ TRANSMITTING ▸" : state === "listening" ? "◂ RECEIVING ▸" : state === "thinking" ? "◂ PROCESSING ▸" : "◂ STANDBY ▸"}
          </motion.div>
        </motion.div>

        {/* ═══ RIGHT COLUMN ═══ */}
        <div style={{ display: "flex", flexDirection: "column", gap: 5, paddingTop: 8, overflow: "hidden" }}>

          {/* TAB BAR */}
          <motion.div variants={slideR(0.9)} initial="hidden" animate="visible">
            <div style={{ display: "flex", border: `1px solid ${C.border}` }}>
              {["SMARTON", "REDITO", "MODE"].map((t, i) => (
                <button key={t} onClick={() => setActiveTab(i)} style={{
                  flex: 1, padding: "6px 0", background: i === activeTab ? C.surface : "transparent",
                  border: "none", borderRight: i < 2 ? `1px solid ${C.border}` : "none",
                  color: i === activeTab ? C.bright : C.dim, fontSize: 7, letterSpacing: "0.12em",
                  cursor: "pointer", fontFamily: "inherit",
                }}>{t}</button>
              ))}
            </div>
          </motion.div>

          {/* TOGGLE SWITCHES */}
          <motion.div variants={slideR(1.1)} initial="hidden" animate="visible">
            <div style={{ padding: "4px 0" }}>
              <ToggleRow icon="⌂" label="Aunation" sub="Barometer" />
              <div style={{ height: 6 }} />
              <ToggleRow label="Bento Chams" />
            </div>
          </motion.div>

          {/* ICON GRID 2x4 */}
          <motion.div variants={slideR(1.3)} initial="hidden" animate="visible">
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 4 }}>
              {MODS.map((m, i) => (
                <button key={i} className={m.hi ? "sk-btn sk-btn-hi" : "sk-btn"}
                  onClick={() => sendAction("command", { text: m.c })}>
                  <GridIcon type={m.sv} active={!!m.hi} />
                  <span style={{ fontSize: 5, letterSpacing: "0.04em", marginTop: 2 }}>{m.l}</span>
                </button>
              ))}
            </div>
          </motion.div>

          {/* SEARCH BAR */}
          <motion.div variants={slideR(1.5)} initial="hidden" animate="visible">
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 7, color: C.dim, letterSpacing: "0.12em", background: C.surface, border: `1px solid ${C.border}`, padding: "4px 8px" }}>BOFIR</span>
              <div style={{ flex: 1, height: 1, background: C.border }} />
              <span style={{ fontSize: 11, color: C.dim, cursor: "pointer", userSelect: "none" }}>−</span>
              <span style={{ fontSize: 11, color: C.dim, cursor: "pointer", userSelect: "none" }}>+</span>
            </div>
          </motion.div>

          {/* CODE / TRANSCRIPT VIEWER */}
          <motion.div variants={slideR(1.7)} initial="hidden" animate="visible" style={{ flex: 1, minHeight: 0 }}>
            <div style={{ border: `1px solid ${C.border}`, height: "100%", display: "flex", flexDirection: "column" }}>
              <div style={{ display: "flex", borderBottom: `1px solid ${C.border}`, fontSize: 7 }}>
                <span style={{ padding: "5px 10px", color: C.text, letterSpacing: "0.1em", borderBottom: `1px solid ${C.warm}` }}>FEATURES</span>
                <span style={{ padding: "5px 10px", color: C.dim, letterSpacing: "0.1em" }}>HOME</span>
                <div style={{ flex: 1 }} />
                <span style={{ padding: "5px 10px", color: C.dim, cursor: "pointer" }}>⌕</span>
              </div>
              <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "6px 10px", display: "flex", flexDirection: "column", gap: 4 }}>
                {lines.length > 0 || stream ? (
                  <>
                    {lines.slice(-40).map(l => <Msg key={l.id} role={l.role} text={l.text} />)}
                    {stream && <Msg role="agent" text={stream} streaming />}
                  </>
                ) : <CodePlaceholder />}
              </div>
            </div>
          </motion.div>
        </div>
      </div>

      {/* ═══ BOTTOM: input ═══ */}
      <motion.div variants={fade(2.2)} initial="hidden" animate="visible" style={{
        position: "absolute", bottom: 48, left: 0, right: 0, zIndex: 10, padding: "0 60px 0",
      }}>
        {!connected && showReconnectHint && (
          <div style={{ fontSize: 7, color: C.dim, textAlign: "center", marginBottom: 4 }}>re-establishing uplink…</div>
        )}
        <div style={{ display: "flex", alignItems: "center", gap: 12, maxWidth: 540, margin: "0 auto", borderBottom: `1px solid ${C.border}`, paddingBottom: 8 }}>
          <span style={{ fontSize: 12, color: C.dim }}>▸</span>
          <input value={input} onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") submit(); }}
            placeholder="speak or type…" autoFocus
            style={{ flex: 1, background: "transparent", border: "none", outline: "none", color: C.bright, fontFamily: "inherit", fontSize: 12, caretColor: C.orb }} />
          <button onClick={toggleMic} style={{ background: "transparent", border: "none", cursor: "pointer", padding: 4, opacity: listening ? 1 : 0.3, color: listening ? C.orb : C.dim, transition: "opacity 0.3s, color 0.3s" }}>
            <MicGlyph />
          </button>
        </div>
      </motion.div>

      {/* RADIAL MENU OVERLAY */}
      <RadialMenu sendAction={sendAction} />

      {/* BOTTOM DATA STRINGS */}
      <motion.div variants={fade(2.4)} initial="hidden" animate="visible" style={{
        position: "absolute", bottom: 28, left: 0, right: 0, zIndex: 3,
        display: "flex", justifyContent: "center", gap: 200, fontSize: 7, color: C.mute, letterSpacing: "0.12em",
      }}>
        <span>008909080</span><span>008080008</span>
      </motion.div>

      {/* 4-POINT STAR */}
      <motion.div variants={scaleIn(0.6)} initial="hidden" animate="visible" style={{
        position: "absolute", bottom: 64, right: 50, zIndex: 5,
      }}>
        <svg width="42" height="42" viewBox="0 0 24 24" opacity="0.45">
          <path d="M12 0L14 10L24 12L14 14L12 24L10 14L0 12L10 10Z" fill={C.text} />
        </svg>
      </motion.div>

      {/* LEFT EDGE TICK */}
      <motion.div variants={fade(0.7)} initial="hidden" animate="visible"
        style={{ position: "absolute", left: 18, top: "50%", transform: "translateY(-50%)", width: 1, height: 22, background: C.line, zIndex: 3 }} />
      {/* RIGHT EDGE TICK */}
      <motion.div variants={fade(0.7)} initial="hidden" animate="visible"
        style={{ position: "absolute", right: 18, top: "50%", transform: "translateY(-50%)", width: 1, height: 22, background: C.line, zIndex: 3 }} />

      <style>{`
        .sk-grid { position:absolute;inset:0;pointer-events:none;background-image:linear-gradient(${C.mute}30 1px,transparent 1px),linear-gradient(90deg,${C.mute}30 1px,transparent 1px);background-size:40px 40px; }
        .sk-cross { position:absolute;inset:0;pointer-events:none;background-image:radial-gradient(circle,${C.dim}18 1.2px,transparent 1.2px);background-size:40px 40px; }
        .sk-btn { background:${C.btnBg};border:1px solid ${C.border};display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:8px 2px;cursor:pointer;color:${C.text};font-family:inherit;border-radius:6px;transition:border-color 0.2s,color 0.2s; }
        .sk-btn:hover { border-color:${C.bar};color:${C.bright}; }
        .sk-btn-hi { background:${C.btnHi};border-color:${C.btnHiBorder};color:${C.bright}; }
        @keyframes sk-blink { 0%,100%{opacity:0.6} 50%{opacity:0} }
        @keyframes sk-energy { 0%,100%{opacity:0.3;} 50%{opacity:0.6;} }
        @keyframes sk-orbit1 { from{transform:rotate(0deg) translateX(170px) rotate(0deg)} to{transform:rotate(360deg) translateX(170px) rotate(-360deg)} }
        @keyframes sk-orbit2 { from{transform:rotate(90deg) translateX(145px) rotate(-90deg)} to{transform:rotate(450deg) translateX(145px) rotate(-450deg)} }
        @keyframes sk-orbit3 { from{transform:rotate(200deg) translateX(120px) rotate(-200deg)} to{transform:rotate(560deg) translateX(120px) rotate(-560deg)} }
        @keyframes sk-wv { 0%,100%{transform:scaleY(0.3)} 50%{transform:scaleY(1)} }
        ::-webkit-scrollbar{width:2px} ::-webkit-scrollbar-track{background:transparent} ::-webkit-scrollbar-thumb{background:${C.mute};border-radius:2px}
      `}</style>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════ */

function CentralOrb({ state, level, onClick }: { state: string; level: number; onClick: () => void }) {
  const active = state !== "idle";
  const speaking = state === "speaking";
  const rings = [180, 162, 144, 126, 108, 90, 70];

  return (
    <button onClick={onClick} aria-label="Toggle mic" style={{ background: "transparent", border: "none", cursor: "pointer", position: "relative", width: "min(48vh, 380px)", aspectRatio: "1/1" }}>
      {/* Energy lines */}
      <div style={{ position: "absolute", top: "calc(50% - 0.5px)", right: "50%", height: 1, width: active ? "180%" : "110%", background: `linear-gradient(to left, ${C.orb}40, transparent)`, transformOrigin: "right", transition: "width 1s", animation: active ? "sk-energy 3s ease-in-out infinite" : "none" }} />
      <div style={{ position: "absolute", top: "calc(50% - 0.5px)", left: "50%", height: 1, width: active ? "180%" : "110%", background: `linear-gradient(to right, ${C.orb}40, transparent)`, transformOrigin: "left", transition: "width 1s", animation: active ? "sk-energy 3s ease-in-out infinite" : "none" }} />

      {/* Orbiting elements */}
      <div style={{ position: "absolute", inset: 0, animation: "sk-orbit1 25s linear infinite", pointerEvents: "none" }}>
        <div style={{ position: "absolute", top: "50%", left: "50%", width: 5, height: 5, borderRadius: "50%", background: C.dim, marginLeft: -2.5, marginTop: -2.5 }} />
      </div>
      <div style={{ position: "absolute", inset: 0, animation: "sk-orbit2 18s linear infinite", pointerEvents: "none" }}>
        <div style={{ position: "absolute", top: "50%", left: "50%", width: 4, height: 4, borderRadius: "50%", background: C.dim, marginLeft: -2, marginTop: -2, opacity: 0.6 }} />
      </div>
      <div style={{ position: "absolute", inset: 0, animation: "sk-orbit3 30s linear infinite", pointerEvents: "none" }}>
        <div style={{ position: "absolute", top: "50%", left: "50%", width: 3, height: 3, borderRadius: "50%", background: C.warm, marginLeft: -1.5, marginTop: -1.5, opacity: 0.5 }} />
      </div>

      <svg viewBox="0 0 400 400" style={{ width: "100%", height: "100%" }}>
        <defs>
          <radialGradient id="sk-glow">
            <stop offset="0%" stopColor={C.orb} stopOpacity={speaking ? 0.6 : active ? 0.35 : 0.2} />
            <stop offset="60%" stopColor={C.orbGlow} stopOpacity={speaking ? 0.2 : active ? 0.1 : 0.06} />
            <stop offset="100%" stopColor={C.orb} stopOpacity="0" />
          </radialGradient>
          <radialGradient id="sk-fill">
            <stop offset="0%" stopColor={C.orb} stopOpacity="0.95" />
            <stop offset="70%" stopColor={C.orbGlow} stopOpacity="0.3" />
            <stop offset="100%" stopColor={C.orbGlow} stopOpacity="0" />
          </radialGradient>
        </defs>
        <circle cx="200" cy="200" r="190" fill="url(#sk-glow)" style={{ transition: "opacity 0.8s" }} />
        {rings.map((r, i) => (
          <circle key={i} cx="200" cy="200" r={r} fill="none" stroke={C.orb}
            strokeWidth={i < 2 ? 0.4 : i < 4 ? 0.6 : 0.8}
            opacity={active ? 0.25 + i * 0.05 : 0.15 + i * 0.04}
            style={{ transition: "opacity 0.6s" }} />
        ))}
        <circle cx="200" cy="200" r="48" fill="url(#sk-fill)"
          style={{ filter: `drop-shadow(0 0 ${active ? 36 : 22}px ${C.orbGlow})`, transition: "filter 0.8s" }} />
        <circle cx="20" cy="200" r="2.5" fill={C.text} opacity="0.25" />
        <circle cx="380" cy="200" r="2.5" fill={C.text} opacity="0.25" />
      </svg>
    </button>
  );
}

function FrameOverlay() {
  const arm = 60;
  const inset = 18;
  const bc = C.line;
  return (
    <div style={{ position: "absolute", inset, pointerEvents: "none", zIndex: 2 }}>
      {[
        { top: 0, left: 0, borderTop: `1px solid ${bc}`, borderLeft: `1px solid ${bc}` },
        { top: 0, right: 0, borderTop: `1px solid ${bc}`, borderRight: `1px solid ${bc}` },
        { bottom: 0, left: 0, borderBottom: `1px solid ${bc}`, borderLeft: `1px solid ${bc}` },
        { bottom: 0, right: 0, borderBottom: `1px solid ${bc}`, borderRight: `1px solid ${bc}` },
      ].map((s, i) => <div key={i} style={{ position: "absolute", width: arm, height: arm, ...s }} />)}
      {[
        { top: -3, left: "50%", transform: "translateX(-50%)" },
        { bottom: -3, left: "50%", transform: "translateX(-50%)" },
        { left: -3, top: "50%", transform: "translateY(-50%)" },
        { right: -3, top: "50%", transform: "translateY(-50%)" },
      ].map((s, i) => <div key={i} style={{ position: "absolute", width: 6, height: 6, background: bc, borderRadius: "50%", ...s }} />)}
    </div>
  );
}

function SysBar({ label, value }: { label: string; value: number }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ fontSize: 7, width: 54, color: C.dim, letterSpacing: "0.08em" }}>{label}</span>
      <div style={{ flex: 1, height: 3, background: C.barBg }}>
        <div style={{ height: "100%", width: `${v}%`, background: C.bar, transition: "width 0.5s ease" }} />
      </div>
    </div>
  );
}

function Msg({ role, text, streaming }: { role: Role; text: string; streaming?: boolean }) {
  if (role === "system") return <div style={{ fontSize: 7, color: C.dim, padding: "1px 0" }}>{text}</div>;
  const me = role === "user";
  return (
    <div style={{ fontSize: 8, lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word", color: me ? C.warm : C.text, borderLeft: me ? "none" : `1px solid ${C.border}`, paddingLeft: me ? 0 : 8, textAlign: me ? "right" : "left" }}>
      {!me && <span style={{ fontSize: 6, color: C.dim, letterSpacing: "0.12em" }}>JARVIS </span>}
      {text}
      {streaming && <span style={{ animation: "sk-blink 1s steps(1) infinite" }}>▋</span>}
    </div>
  );
}

function ToggleRow({ icon, label, sub }: { icon?: string; label: string; sub?: string }) {
  const [on, setOn] = useState(false);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "2px 4px" }}>
      {icon && <span style={{ fontSize: 16, color: C.dim }}>{icon}</span>}
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 10, color: C.bright }}>{label}</div>
        {sub && <div style={{ fontSize: 7, color: C.dim }}>{sub}</div>}
      </div>
      <button onClick={() => setOn(!on)} style={{
        width: 36, height: 18, borderRadius: 9, border: `1px solid ${C.border}`,
        background: on ? C.amber : C.barBg, cursor: "pointer", position: "relative", padding: 0, transition: "background 0.3s",
      }}>
        <div style={{ position: "absolute", top: 2, left: on ? 18 : 2, width: 12, height: 12, borderRadius: "50%", background: C.bright, transition: "left 0.3s" }} />
      </button>
    </div>
  );
}

function WxIcon({ type, size = 20 }: { type: string; size?: number }) {
  const s = size;
  const h = s * 0.8;
  const sc = C.text;
  if (type === "rain") return (
    <svg width={s} height={h} viewBox="0 0 24 20" fill="none" stroke={sc} strokeWidth="1.2" strokeLinecap="round">
      <path d="M7 12h9a4.5 4.5 0 10-3.5-7A5.5 5.5 0 002 9a3.2 3.2 0 005 3z" />
      <path d="M8 15l-1 3M12 15l-1 3M16 15l-1 3" />
    </svg>
  );
  if (type === "snow") return (
    <svg width={s} height={h} viewBox="0 0 24 20" fill="none" stroke={sc} strokeWidth="1.2" strokeLinecap="round">
      <path d="M7 12h9a4.5 4.5 0 10-3.5-7A5.5 5.5 0 002 9a3.2 3.2 0 005 3z" />
      <circle cx="8" cy="16" r="0.8" fill={sc} /><circle cx="12" cy="17" r="0.8" fill={sc} /><circle cx="16" cy="16" r="0.8" fill={sc} />
    </svg>
  );
  if (type === "partly") return (
    <svg width={s} height={h} viewBox="0 0 24 18" fill="none" stroke={sc} strokeWidth="1.2">
      <circle cx="8" cy="7" r="3.5" /><path d="M4.5 7H2M14 7h-1.5M8 3.5V2M8 11v1M5 4L4 3M11 4l1-1M5 10l-1 1M11 10l1 1" strokeLinecap="round" />
      <path d="M12 14h6a3 3 0 10-2.5-5A4 4 0 009 12a2.2 2.2 0 003 2z" />
    </svg>
  );
  return (
    <svg width={s} height={h} viewBox="0 0 24 14" fill="none" stroke={sc} strokeWidth="1.2">
      <path d="M7 12h9a4.5 4.5 0 10-3.5-7A5.5 5.5 0 002 9a3.2 3.2 0 005 3z" />
    </svg>
  );
}

function GridIcon({ type, active }: { type: string; active?: boolean }) {
  const sc = active ? C.bright : C.text;
  const w = 1.3;
  const s = 20;
  const icons: Record<string, JSX.Element> = {
    sun: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={sc} strokeWidth={w} strokeLinecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/></svg>,
    lamp: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={sc} strokeWidth={w} strokeLinecap="round"><path d="M9 18h6M10 22h4"/><path d="M12 2a7 7 0 00-4 12.7V17h8v-2.3A7 7 0 0012 2z"/></svg>,
    shield: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={sc} strokeWidth={w} strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>,
    speaker: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={sc} strokeWidth={w} strokeLinecap="round"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.08"/></svg>,
    gear: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={sc} strokeWidth={w}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>,
    monitor: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={sc} strokeWidth={w} strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><path d="M8 21h8M12 17v4"/></svg>,
    thermo: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={sc} strokeWidth={w} strokeLinecap="round"><path d="M14 14.76V3.5a2.5 2.5 0 00-5 0v11.26a4.5 4.5 0 105 0z"/></svg>,
    dots: <svg width={s} height={s} viewBox="0 0 24 24" fill={sc}><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>,
  };
  return icons[type] || <span style={{ fontSize: 14 }}>?</span>;
}

function WaveformBars({ level, active }: { level: number; active: boolean }) {
  const bars = 22;
  return (
    <div style={{ flex: 1, display: "flex", gap: 2, alignItems: "center", height: 32 }}>
      {Array.from({ length: bars }, (_, i) => {
        const base = Math.sin(i * 0.8) * 0.3 + 0.4;
        const h = active ? Math.max(0.15, base * (0.5 + level * 0.5) + Math.sin(i * 1.3 + Date.now() * 0.002) * 0.15) : base * 0.4;
        return (
          <div key={i} style={{
            flex: 1, height: `${Math.min(1, h) * 100}%`,
            background: C.amber, borderRadius: 1, transition: "height 0.15s",
            animation: active ? `sk-wv ${0.4 + i * 0.05}s ease-in-out infinite` : "none",
            animationDelay: `${i * 0.03}s`,
          }} />
        );
      })}
    </div>
  );
}

function CodePlaceholder() {
  const code = `static int erm_shore_provision() {
    Route ServiceClient;
    coordinator_in random fileAddress/destination, 5000;
    coordinator.int gordonplace: 0;
    coordinator.int gothamplace: 0;
    coordinator.int gothamblack: 0;
    coordinator.int source_eventId: 0;
}
section {
    onListener EventLocator, EventAction, 6009;
}
while (running channel coordinator) {
    dataLog = connectionRef;
    for each {
        readable(factoryLocationData(dataLocation001));
        terCommand(66,functionKey,functionFile());
        submission factoryData();
        result: encode_insertedCdataDetails, fileOut);
    }
    receive;
        const text_md_and_coordinatorSubRef;
        route.decorator_applicationLocation(decoreCode.file);
        react_router target log;
    }
}`;
  return (
    <pre style={{ fontSize: 7.5, lineHeight: 1.6, color: C.dim, fontFamily: "inherit", margin: 0, whiteSpace: "pre-wrap" }}>{code}</pre>
  );
}

function RadialMenu({ sendAction }: { sendAction: (type: string, data: any) => void }) {
  const s = 240;
  const cx = s / 2, cy = s / 2;
  const ri = 68, ro = 92;
  const gap = 2;

  const segs = [
    { a1: -110, a2: -65, color: "#3d2820" },
    { a1: -63, a2: -20, color: "#5c3a2a" },
    { a1: -18, a2: 30, color: "#4a2e20" },
    { a1: 32, a2: 80, color: "#6b4832" },
    { a1: 82, a2: 125, color: "#8a6242" },
    { a1: 127, a2: 170, color: "#5c3a28" },
    { a1: 172, a2: 215, color: "#3d2218" },
    { a1: 217, a2: 248, color: "#7a583a" },
  ];

  const icons: { angle: number; label: string; svg: JSX.Element }[] = [
    { angle: -90, label: "Config", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg> },
    { angle: -40, label: "Mail", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3" strokeLinecap="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M22 4L12 13 2 4"/></svg> },
    { angle: 10, label: "Settings", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12" strokeLinecap="round"/></svg> },
    { angle: 55, label: "Target", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg> },
    { angle: 105, label: "User", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3" strokeLinecap="round"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg> },
    { angle: 150, label: "Lock", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3" strokeLinecap="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg> },
    { angle: 195, label: "Play", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4" strokeLinecap="round"/></svg> },
    { angle: 235, label: "Grid", svg: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth="1.3"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> },
  ];

  const r2d = Math.PI / 180;
  const arc = (a1: number, a2: number, rin: number, rout: number) => {
    const x1o = cx + rout * Math.cos(a1 * r2d), y1o = cy + rout * Math.sin(a1 * r2d);
    const x2o = cx + rout * Math.cos(a2 * r2d), y2o = cy + rout * Math.sin(a2 * r2d);
    const x2i = cx + rin * Math.cos(a2 * r2d), y2i = cy + rin * Math.sin(a2 * r2d);
    const x1i = cx + rin * Math.cos(a1 * r2d), y1i = cy + rin * Math.sin(a1 * r2d);
    const lg = a2 - a1 > 180 ? 1 : 0;
    return `M ${x1o} ${y1o} A ${rout} ${rout} 0 ${lg} 1 ${x2o} ${y2o} L ${x2i} ${y2i} A ${rin} ${rin} 0 ${lg} 0 ${x1i} ${y1i} Z`;
  };

  return (
    <motion.div variants={scaleIn(2.2)} initial="hidden" animate="visible"
      style={{ position: "absolute", top: "46%", left: 170, transform: "translate(-50%, -50%)", width: s, height: s, zIndex: 8, pointerEvents: "none" }}>
      <svg width={s} height={s} viewBox={`0 0 ${s} ${s}`}>
        {segs.map((seg, i) => (
          <path key={i} d={arc(seg.a1, seg.a2, ri, ro)} fill={seg.color} opacity={0.85} />
        ))}
        <circle cx={cx} cy={cy} r={ri - 2} fill="none" stroke={C.line} strokeWidth="0.5" opacity={0.5} />
        <circle cx={cx} cy={cy} r={ro + 2} fill="none" stroke={C.line} strokeWidth="0.5" opacity={0.5} />
      </svg>
      {icons.map((ic, i) => {
        const iconR = ro + 22;
        const x = cx + iconR * Math.cos(ic.angle * r2d) - 10;
        const y = cy + iconR * Math.sin(ic.angle * r2d) - 10;
        return (
          <button key={i} onClick={() => sendAction("command", { text: ic.label })}
            style={{ position: "absolute", left: x, top: y, width: 20, height: 20, background: "transparent", border: "none", cursor: "pointer", pointerEvents: "auto", padding: 0, opacity: 0.85, display: "flex", alignItems: "center", justifyContent: "center" }}
            title={ic.label}>
            {ic.svg}
          </button>
        );
      })}
      {[0, 120, 240].map((a, i) => (
        <div key={i} style={{
          position: "absolute",
          left: cx + (ro + 4) * Math.cos(a * r2d) - 2.5,
          top: cy + (ro + 4) * Math.sin(a * r2d) - 2.5,
          width: 5, height: 5, borderRadius: "50%",
          background: C.warm, opacity: 0.6,
          animation: `sk-orbit${i + 1} ${20 + i * 5}s linear infinite`,
        }} />
      ))}
    </motion.div>
  );
}

function Dot({ on }: { on: boolean }) {
  return (
    <motion.span animate={{ opacity: on ? 1 : [0.3, 1, 0.3] }} transition={on ? {} : { duration: 1.5, repeat: Infinity }}
      style={{ width: 5, height: 5, borderRadius: "50%", display: "inline-block", background: on ? C.orb : C.warm, boxShadow: on ? `0 0 6px ${C.orbGlow}` : "none" }} />
  );
}

function MicGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v1a7 7 0 0014 0v-1M12 18v4" />
    </svg>
  );
}
