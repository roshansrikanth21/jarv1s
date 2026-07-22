// Stark — the cinematic movie-JARVIS HUD preset: a cyan holographic command surface
// built around a schematic Iron Man wireframe, arc-reactor gauges, a radial module
// menu, and live telemetry side panels. Fully functional: wired to useJarvisSocket for
// mic, streaming conversation, and live device stats — this is a real deck, not a mockup.
//
// Everything is SVG + CSS/framer-motion animation (no WebGL) so it can't white-screen on
// a bad GPU. Palette is arc-reactor cyan against near-black blue.
import { motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import { WindowControls } from "@/components/jarvis/WindowControls";
import { useJarvisSocket, type Role } from "@/hooks/useJarvisSocket";

const CYAN = "#5fdcff";
const CYAN_DIM = "#2aa6d8";
const BLUE = "#1c8cff";
const BG = "#03080f";

type Stat = { cpu: number; ram: number; disk: number };

export default function StarkDeck() {
  const {
    connected,
    listening,
    speaking,
    lines,
    stream,
    level,
    send,
    toggleMic,
    sendAction,
    showReconnectHint,
  } = useJarvisSocket("J.A.R.V.I.S. online. Good to see you, sir.");
  const [input, setInput] = useState("");
  const [stat, setStat] = useState<Stat>({ cpu: 0, ram: 0, disk: 0 });
  const [clock, setClock] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 9e6, behavior: "smooth" });
  }, [lines.length, stream]);

  // live clock
  useEffect(() => {
    const t = setInterval(
      () =>
        setClock(
          new Date().toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          }),
        ),
      1000,
    );
    return () => clearInterval(t);
  }, []);

  // live device vitals
  useEffect(() => {
    let alive = true;
    const pull = () =>
      fetch("/api/device")
        .then((r) => r.json())
        .then((d) => {
          if (!alive) return;
          setStat({
            cpu: Math.round(d?.cpu_percent ?? 0),
            ram: Math.round(
              (((d?.ram_total_gb ?? 0) - (d?.ram_available_gb ?? 0)) / (d?.ram_total_gb || 1)) *
                100,
            ),
            disk: Math.round(d?.disk_percent ?? 0),
          });
        })
        .catch(() => {});
    pull();
    const id = setInterval(pull, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const submit = () => {
    if (input.trim()) {
      send(input);
      setInput("");
    }
  };
  const orbState = speaking ? "speaking" : listening ? "listening" : "idle";

  const MODULES = [
    { key: "status", label: "STATUS", cmd: "give me a full system status report" },
    { key: "memory", label: "MEMORY", cmd: "what do you remember about me" },
    { key: "tools", label: "TOOLS", cmd: "what can you do" },
    { key: "recon", label: "RECON", cmd: "get me the latest tech and security news" },
    { key: "vision", label: "VISION", cmd: "what is on my screen right now" },
  ];

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: BG,
        color: CYAN,
        overflow: "hidden",
        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      }}
    >
      {/* ── ambient backdrop ── */}
      <div
        aria-hidden
        style={{
          position: "absolute",
          inset: 0,
          pointerEvents: "none",
          background:
            `radial-gradient(60% 50% at 50% 42%, ${CYAN}14, transparent 70%),` +
            `radial-gradient(120% 90% at 50% 120%, ${BLUE}10, transparent 60%), ${BG}`,
        }}
      />
      <div aria-hidden className="stark-grid" />
      <div aria-hidden className="stark-scan" />

      {/* ── header ── */}
      <header
        className="drag"
        style={{
          position: "relative",
          zIndex: 5,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "10px 16px",
        }}
      >
        <div className="no-drag" style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <StatusRing on={connected} />
          <div>
            <div
              style={{
                fontSize: 13,
                letterSpacing: "0.5em",
                fontWeight: 700,
                textShadow: `0 0 12px ${CYAN}`,
              }}
            >
              J.A.R.V.I.S.
            </div>
            <div style={{ fontSize: 7.5, letterSpacing: "0.3em", color: CYAN_DIM }}>
              STARK INDUSTRIES · MK LXXXV
            </div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{ fontSize: 18, letterSpacing: "0.15em", textShadow: `0 0 10px ${CYAN}` }}>
            {clock}
          </div>
          <div className="no-drag">
            <WindowControls accent={CYAN} />
          </div>
        </div>
      </header>

      {/* ── main grid ── */}
      <div
        style={{
          position: "relative",
          zIndex: 4,
          display: "grid",
          gridTemplateColumns: "220px 1fr 220px",
          gap: 12,
          padding: "4px 16px 12px",
          height: "calc(100vh - 60px)",
        }}
      >
        {/* LEFT — module rail + conversation */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: 0 }}>
          <Panel title="MODULES">
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              {MODULES.map((m, i) => (
                <motion.button
                  key={m.key}
                  onClick={() => sendAction("command", { text: m.cmd })}
                  initial={{ opacity: 0, x: -12 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 0.05 * i }}
                  className="stark-module"
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      background: CYAN,
                      boxShadow: `0 0 6px ${CYAN}`,
                      clipPath: "polygon(50% 0,100% 50%,50% 100%,0 50%)",
                    }}
                  />
                  {m.label}
                </motion.button>
              ))}
            </div>
          </Panel>
          <Panel title="TRANSCRIPT" grow>
            <div
              ref={scrollRef}
              style={{
                height: "100%",
                overflowY: "auto",
                display: "flex",
                flexDirection: "column",
                gap: 8,
                paddingRight: 4,
              }}
            >
              {lines.slice(-40).map((l) => (
                <Msg key={l.id} role={l.role} text={l.text} />
              ))}
              {stream && <Msg role="agent" text={stream} streaming />}
            </div>
          </Panel>
        </div>

        {/* CENTER — Iron Man wireframe + reactor */}
        <div
          style={{
            position: "relative",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            minHeight: 0,
          }}
        >
          <CornerFrame />
          <motion.div
            animate={{ y: [0, -8, 0] }}
            transition={{ duration: 6, repeat: Infinity, ease: "easeInOut" }}
            style={{
              position: "relative",
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              width: "100%",
            }}
          >
            <IronMan speaking={speaking} listening={listening} />
          </motion.div>
          <ArcReactor state={orbState} level={level} onClick={toggleMic} />
          <div
            style={{
              fontSize: 8,
              letterSpacing: "0.32em",
              color: CYAN_DIM,
              marginTop: 8,
              textTransform: "uppercase",
            }}
          >
            {orbState === "speaking"
              ? "◄ RESPONDING ►"
              : orbState === "listening"
                ? "◄ LISTENING ►"
                : "TAP REACTOR TO SPEAK"}
          </div>
        </div>

        {/* RIGHT — telemetry */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: 0 }}>
          <Panel title="VITALS">
            <Gauge label="CPU" value={stat.cpu} />
            <Gauge label="MEM" value={stat.ram} />
            <Gauge label="DSK" value={stat.disk} />
          </Panel>
          <Panel title="ARMOR" grow>
            <div style={{ display: "flex", justifyContent: "center", padding: "6px 0" }}>
              <MiniArmor />
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "4px 10px",
                fontSize: 8.5,
                color: CYAN_DIM,
                marginTop: 6,
              }}
            >
              {[
                ["POWER", "98%"],
                ["THRUST", "NOMINAL"],
                ["INTEG", "100%"],
                ["FLIGHT", "READY"],
                ["SHIELD", "ONLINE"],
                ["COMMS", connected ? "LINKED" : "----"],
              ].map(([k, v]) => (
                <div key={k} style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>{k}</span>
                  <span style={{ color: CYAN }}>{v}</span>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>

      {/* ── footer input ── */}
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          right: 0,
          zIndex: 6,
          padding: "0 16px 14px",
        }}
      >
        {!connected && showReconnectHint && (
          <div
            style={{
              fontSize: 9,
              color: CYAN_DIM,
              textAlign: "center",
              marginBottom: 6,
              letterSpacing: "0.1em",
            }}
          >
            re-establishing uplink…
          </div>
        )}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            maxWidth: 760,
            margin: "0 auto",
            padding: "8px 8px 8px 16px",
            background: "rgba(6,16,28,0.7)",
            border: `1px solid ${CYAN}44`,
            borderRadius: 8,
            backdropFilter: "blur(8px)",
            boxShadow: `0 0 24px ${CYAN}18, inset 0 0 20px ${CYAN}08`,
          }}
        >
          <span style={{ fontSize: 11, color: CYAN }}>❯</span>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
            placeholder="Address JARVIS…"
            autoFocus
            style={{
              flex: 1,
              background: "transparent",
              border: "none",
              outline: "none",
              color: "#dffaff",
              fontFamily: "inherit",
              fontSize: 13,
              letterSpacing: "0.03em",
            }}
          />
          <button
            onClick={toggleMic}
            title="Voice"
            style={{
              background: listening ? `${CYAN}22` : "transparent",
              border: `1px solid ${CYAN}55`,
              color: CYAN,
              borderRadius: 6,
              padding: "6px 9px",
              cursor: "pointer",
              display: "flex",
            }}
          >
            <MicIcon />
          </button>
          <button
            onClick={submit}
            style={{
              background: CYAN,
              border: "none",
              color: BG,
              borderRadius: 6,
              padding: "7px 16px",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.1em",
            }}
          >
            SEND
          </button>
        </div>
      </div>

      <style>{`
        .stark-grid { position:absolute; inset:0; pointer-events:none; opacity:0.5;
          background-image:linear-gradient(${CYAN}0a 1px,transparent 1px),linear-gradient(90deg,${CYAN}0a 1px,transparent 1px);
          background-size:44px 44px; animation:starkGrid 120s linear infinite; }
        @keyframes starkGrid { to { background-position:44px 44px; } }
        .stark-scan { position:absolute; left:0; right:0; height:180px; pointer-events:none;
          background:linear-gradient(${CYAN}00,${CYAN}10,${CYAN}00); animation:starkScan 7s linear infinite; }
        @keyframes starkScan { 0%{ transform:translateY(-200px);} 100%{ transform:translateY(100vh);} }
        .stark-module { display:flex; align-items:center; gap:9px; width:100%; text-align:left; cursor:pointer;
          background:transparent; border:1px solid ${CYAN}22; color:${CYAN_DIM}; border-radius:5px;
          padding:8px 11px; font-family:inherit; font-size:10.5px; letter-spacing:0.18em; transition:all .15s; }
        .stark-module:hover { border-color:${CYAN}; color:${CYAN}; background:${CYAN}10; box-shadow:0 0 14px ${CYAN}33; transform:translateX(3px); }
        @keyframes starkDraw { from { stroke-dashoffset:1400; } to { stroke-dashoffset:0; } }
        @keyframes starkPulse { 0%,100%{ opacity:.85; } 50%{ opacity:.35; } }
        @media (prefers-reduced-motion: reduce) { .stark-grid,.stark-scan { animation:none; } }
      `}</style>
    </div>
  );
}

/* ── panels ── */
function Panel({
  title,
  children,
  grow,
}: {
  title: string;
  children: React.ReactNode;
  grow?: boolean;
}) {
  return (
    <div
      style={{
        position: "relative",
        flex: grow ? 1 : "none",
        minHeight: grow ? 0 : undefined,
        background: "rgba(6,16,28,0.55)",
        border: `1px solid ${CYAN}2a`,
        borderRadius: 7,
        padding: "9px 11px",
        display: "flex",
        flexDirection: "column",
        boxShadow: `inset 0 0 24px ${CYAN}0a`,
      }}
    >
      <div
        style={{
          position: "absolute",
          top: -1,
          left: 10,
          right: 10,
          height: 1,
          background: `linear-gradient(90deg,transparent,${CYAN}88,transparent)`,
        }}
      />
      <div
        style={{
          fontSize: 8.5,
          letterSpacing: "0.32em",
          color: CYAN,
          marginBottom: 8,
          opacity: 0.85,
        }}
      >
        {title}
      </div>
      <div style={{ flex: grow ? 1 : "none", minHeight: grow ? 0 : undefined }}>{children}</div>
    </div>
  );
}

function Msg({ role, text, streaming }: { role: Role; text: string; streaming?: boolean }) {
  if (role === "system")
    return (
      <div style={{ fontSize: 9.5, color: CYAN_DIM, opacity: 0.7, letterSpacing: "0.05em" }}>
        {text}
      </div>
    );
  const me = role === "user";
  return (
    <div
      style={{
        fontSize: 11,
        lineHeight: 1.5,
        color: me ? "#bfe9ff" : "#e6fbff",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      <span
        style={{ color: me ? BLUE : CYAN, fontSize: 8, letterSpacing: "0.2em", marginRight: 6 }}
      >
        {me ? "YOU" : "JARVIS"}
      </span>
      {text}
      {streaming && <span style={{ opacity: 0.5 }}>▋</span>}
    </div>
  );
}

/* ── vitals gauge ── */
function Gauge({ label, value }: { label: string; value: number }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 7 }}>
      <span style={{ fontSize: 8.5, color: CYAN_DIM, width: 26 }}>{label}</span>
      <div
        style={{
          flex: 1,
          height: 6,
          background: `${CYAN}14`,
          borderRadius: 3,
          overflow: "hidden",
          position: "relative",
        }}
      >
        <motion.div
          animate={{ width: `${v}%` }}
          transition={{ duration: 0.6 }}
          style={{
            height: "100%",
            background: `linear-gradient(90deg,${CYAN_DIM},${CYAN})`,
            boxShadow: `0 0 8px ${CYAN}`,
          }}
        />
      </div>
      <span style={{ fontSize: 9, color: CYAN, width: 30, textAlign: "right" }}>{v}%</span>
    </div>
  );
}

/* ── status ring ── */
function StatusRing({ on }: { on: boolean }) {
  return (
    <svg width="30" height="30" viewBox="0 0 30 30">
      <circle
        cx="15"
        cy="15"
        r="12"
        fill="none"
        stroke={on ? CYAN : "#33505f"}
        strokeWidth="1"
        opacity="0.4"
      />
      <motion.circle
        cx="15"
        cy="15"
        r="12"
        fill="none"
        stroke={CYAN}
        strokeWidth="1.5"
        strokeDasharray="18 60"
        strokeLinecap="round"
        animate={{ rotate: 360 }}
        transition={{ duration: 3, repeat: Infinity, ease: "linear" }}
        style={{
          transformOrigin: "center",
          filter: `drop-shadow(0 0 4px ${CYAN})`,
          opacity: on ? 1 : 0.3,
        }}
      />
      <circle
        cx="15"
        cy="15"
        r="4"
        fill={on ? CYAN : "#33505f"}
        style={{ filter: on ? `drop-shadow(0 0 5px ${CYAN})` : "none" }}
      />
    </svg>
  );
}

/* ── corner frame ── */
function CornerFrame() {
  const c = (s: React.CSSProperties) => (
    <div style={{ position: "absolute", width: 22, height: 22, borderColor: `${CYAN}66`, ...s }} />
  );
  return (
    <>
      {c({ top: 0, left: 0, borderTop: "1px solid", borderLeft: "1px solid" })}
      {c({ top: 0, right: 0, borderTop: "1px solid", borderRight: "1px solid" })}
      {c({ bottom: 0, left: 0, borderBottom: "1px solid", borderLeft: "1px solid" })}
      {c({ bottom: 0, right: 0, borderBottom: "1px solid", borderRight: "1px solid" })}
    </>
  );
}

/* ── the Iron Man wireframe (schematic front view) ── */
function IronMan({ speaking, listening }: { speaking: boolean; listening: boolean }) {
  const active = speaking || listening;
  return (
    <svg
      viewBox="0 0 200 340"
      style={{
        height: "min(62vh, 520px)",
        maxWidth: "100%",
        filter: `drop-shadow(0 0 14px ${CYAN}44)`,
      }}
    >
      <defs>
        <linearGradient id="ironStroke" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={CYAN} />
          <stop offset="1" stopColor={CYAN_DIM} />
        </linearGradient>
      </defs>
      <g
        fill="none"
        stroke="url(#ironStroke)"
        strokeWidth="1.2"
        strokeLinejoin="round"
        style={{ strokeDasharray: 1400, animation: "starkDraw 3s ease-out forwards" }}
      >
        {/* helmet */}
        <path d="M100 8 C126 8 138 30 138 54 C138 72 132 84 128 92 L124 104 C122 112 110 116 100 116 C90 116 78 112 76 104 L72 92 C68 84 62 72 62 54 C62 30 74 8 100 8 Z" />
        <path d="M78 58 L94 66 L94 74 L74 70 Z" fill={`${CYAN}22`} />
        <path d="M122 58 L106 66 L106 74 L126 70 Z" fill={`${CYAN}22`} />
        <path d="M88 92 L112 92 M84 78 L116 78" strokeWidth="0.8" opacity="0.5" />
        {/* neck + shoulders */}
        <path d="M86 116 L86 126 M114 116 L114 126" />
        <path d="M70 132 C82 124 118 124 130 132 L150 150 L146 168 M50 150 L54 168 L70 132" />
        {/* torso */}
        <path d="M66 138 L74 210 L100 224 L126 210 L134 138" />
        <path d="M66 168 L134 168 M72 190 L128 190" strokeWidth="0.7" opacity="0.5" />
        {/* arms */}
        <path d="M52 150 L44 210 L52 250 L64 248 L60 206 L70 158" />
        <path d="M148 150 L156 210 L148 250 L136 248 L140 206 L130 158" />
        {/* forearms/hands */}
        <path d="M46 212 L40 258 L52 270 L60 258 L56 214" />
        <path d="M154 212 L160 258 L148 270 L140 258 L144 214" />
        {/* pelvis + legs */}
        <path d="M78 216 L74 240 L86 250 L100 246 L114 250 L126 240 L122 216" />
        <path d="M82 250 L78 316 L88 332 L98 316 L96 252" />
        <path d="M118 250 L122 316 L112 332 L102 316 L104 252" />
        <path d="M78 300 L98 300 M122 300 L102 300" strokeWidth="0.7" opacity="0.5" />
      </g>
      {/* chest arc reactor */}
      <g style={{ transformOrigin: "100px 158px" }}>
        <motion.circle
          cx="100"
          cy="158"
          r="15"
          fill="none"
          stroke={CYAN}
          strokeWidth="1"
          strokeDasharray="6 5"
          animate={{ rotate: 360 }}
          transition={{ duration: 10, repeat: Infinity, ease: "linear" }}
          style={{ transformOrigin: "100px 158px" }}
        />
        <circle cx="100" cy="158" r="9" fill={`${CYAN}22`} stroke={CYAN} strokeWidth="1.2" />
        <motion.circle
          cx="100"
          cy="158"
          r="4.5"
          fill={CYAN}
          animate={{ opacity: active ? [1, 0.5, 1] : [0.85, 0.6, 0.85] }}
          transition={{ duration: active ? 0.8 : 2.4, repeat: Infinity, ease: "easeInOut" }}
          style={{ filter: `drop-shadow(0 0 8px ${CYAN})` }}
        />
      </g>
      {/* eye glow */}
      <motion.g
        animate={{ opacity: active ? [1, 0.6, 1] : 0.8 }}
        transition={{ duration: 1.4, repeat: Infinity }}
      >
        <ellipse
          cx="84"
          cy="68"
          rx="5"
          ry="2.4"
          fill={CYAN}
          style={{ filter: `drop-shadow(0 0 5px ${CYAN})` }}
        />
        <ellipse
          cx="116"
          cy="68"
          rx="5"
          ry="2.4"
          fill={CYAN}
          style={{ filter: `drop-shadow(0 0 5px ${CYAN})` }}
        />
      </motion.g>
      {/* scan line over figure */}
      <motion.line
        x1="40"
        x2="160"
        stroke={CYAN}
        strokeWidth="1"
        opacity="0.5"
        animate={{ y1: [8, 332, 8], y2: [8, 332, 8] }}
        transition={{ duration: 5, repeat: Infinity, ease: "linear" }}
      />
    </svg>
  );
}

/* ── central arc-reactor control (mic orb) ── */
function ArcReactor({
  state,
  level,
  onClick,
}: {
  state: string;
  level: number;
  onClick: () => void;
}) {
  const scale = 1 + (state === "listening" ? Math.min(level / 32000, 1) * 0.25 : 0);
  return (
    <button
      onClick={onClick}
      title="Speak"
      style={{ background: "transparent", border: "none", cursor: "pointer", padding: 0 }}
    >
      <div
        style={{
          position: "relative",
          width: 92,
          height: 92,
          transform: `scale(${scale})`,
          transition: "transform .12s",
        }}
      >
        <motion.svg
          viewBox="0 0 100 100"
          style={{ position: "absolute", inset: 0 }}
          animate={{ rotate: 360 }}
          transition={{ duration: 18, repeat: Infinity, ease: "linear" }}
        >
          <circle
            cx="50"
            cy="50"
            r="46"
            fill="none"
            stroke={CYAN}
            strokeWidth="1"
            strokeDasharray="3 6"
            opacity="0.5"
          />
        </motion.svg>
        <motion.svg
          viewBox="0 0 100 100"
          style={{ position: "absolute", inset: 0 }}
          animate={{ rotate: -360 }}
          transition={{ duration: 26, repeat: Infinity, ease: "linear" }}
        >
          <circle
            cx="50"
            cy="50"
            r="38"
            fill="none"
            stroke={CYAN}
            strokeWidth="1.5"
            strokeDasharray="14 8"
            opacity="0.7"
            style={{ filter: `drop-shadow(0 0 4px ${CYAN})` }}
          />
        </motion.svg>
        <div
          style={{
            position: "absolute",
            inset: 26,
            borderRadius: "50%",
            border: `2px solid ${CYAN}`,
            display: "grid",
            placeItems: "center",
            boxShadow:
              state === "speaking"
                ? `0 0 30px ${CYAN}, inset 0 0 18px ${CYAN}88`
                : state === "listening"
                  ? `0 0 20px ${CYAN}aa, inset 0 0 12px ${CYAN}66`
                  : `0 0 12px ${CYAN}55`,
          }}
        >
          <motion.div
            animate={{
              opacity: state === "speaking" ? [1, 0.4, 1] : state === "idle" ? 0.7 : [1, 0.6, 1],
            }}
            transition={{ duration: state === "speaking" ? 0.6 : 2, repeat: Infinity }}
            style={{
              width: 20,
              height: 20,
              borderRadius: "50%",
              background: CYAN,
              filter: `drop-shadow(0 0 10px ${CYAN})`,
            }}
          />
        </div>
      </div>
    </button>
  );
}

/* ── mini rotating armor schematic (right panel) ── */
function MiniArmor() {
  return (
    <motion.svg
      width="86"
      height="120"
      viewBox="0 0 100 140"
      animate={{ rotateY: [0, 360] }}
      transition={{ duration: 14, repeat: Infinity, ease: "linear" }}
      style={{ filter: `drop-shadow(0 0 6px ${CYAN}55)` }}
    >
      <g fill="none" stroke={CYAN} strokeWidth="1" opacity="0.85">
        <circle cx="50" cy="20" r="12" />
        <path d="M38 34 L34 70 L50 80 L66 70 L62 34 Z" />
        <circle cx="50" cy="52" r="6" fill={`${CYAN}22`} />
        <path d="M34 40 L24 78 M66 40 L76 78" />
        <path d="M42 80 L40 120 L48 130 M58 80 L60 120 L52 130" />
      </g>
    </motion.svg>
  );
}

function MicIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
    >
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10v1a7 7 0 0014 0v-1M12 18v4" />
    </svg>
  );
}
