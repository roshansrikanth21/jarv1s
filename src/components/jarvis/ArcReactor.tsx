import { motion } from "framer-motion";

interface Props {
  active?: boolean;
  speaking?: boolean;
  size?: "sm" | "md";
  energy?: number;   // 0..1 homeostatic energy — dims the reactor when the body is low
}

const AMBER = "oklch(0.68 0.22 38)";
const AMBER_DIM = "oklch(0.52 0.16 38)";

export function ArcReactor({ active = false, speaking = false, size = "md", energy = 1 }: Props) {
  const px   = size === "sm" ? 128 : 192;
  const core = size === "sm" ?  56 :  80;

  return (
    <div style={{ position: "relative", width: px, height: px, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                  opacity: 0.6 + 0.4 * Math.max(0, Math.min(1, energy)), transition: "opacity 1.2s ease" }}>

      {/* Ambient glow */}
      <motion.div
        style={{ position: "absolute", inset: 0, borderRadius: "50%" }}
        animate={{
          boxShadow: active
            ? speaking
              ? [`0 0 30px ${AMBER}80`, `0 0 60px ${AMBER}CC`, `0 0 30px ${AMBER}80`]
              : [`0 0 18px ${AMBER}40`, `0 0 32px ${AMBER}65`, `0 0 18px ${AMBER}40`]
            : `0 0 0px ${AMBER}00`,
        }}
        transition={{ duration: speaking ? 0.9 : 2.5, repeat: Infinity, ease: "easeInOut" }}
      />

      {/* Outer slow-spin ring */}
      <motion.svg
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
        viewBox="0 0 100 100"
        animate={{ rotate: 360 }}
        transition={{ duration: 24, repeat: Infinity, ease: "linear" }}
      >
        <circle cx="50" cy="50" r="47" fill="none" stroke={AMBER} strokeWidth="0.15" strokeDasharray="1 3" opacity="0.3" />
        <path d="M50 3 A47 47 0 0 1 97 50" fill="none" stroke={AMBER} strokeWidth="1" strokeDasharray="5 9" opacity="0.55" strokeLinecap="round" />
        <path d="M50 97 A47 47 0 0 1 3 50" fill="none" stroke={AMBER} strokeWidth="1" strokeDasharray="5 9" opacity="0.55" strokeLinecap="round" />
      </motion.svg>

      {/* Counter-spin inner ring */}
      <motion.svg
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
        viewBox="0 0 100 100"
        animate={{ rotate: -360 }}
        transition={{ duration: 36, repeat: Infinity, ease: "linear" }}
      >
        <circle cx="50" cy="50" r="40" fill="none" stroke={AMBER} strokeWidth="0.25" strokeDasharray="2 6" opacity="0.25" />
        <path d="M50 10 A40 40 0 0 1 90 50" fill="none" stroke={AMBER} strokeWidth="0.6" strokeDasharray="3 8" opacity="0.4" strokeLinecap="round" />
      </motion.svg>

      {/* Static rings */}
      <svg style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }} viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="43.5" fill="none" stroke={AMBER} strokeWidth="0.35" opacity="0.10" />
        <circle cx="50" cy="50" r="33"   fill="none" stroke={AMBER} strokeWidth="0.12" strokeDasharray="0.4 2.2" opacity="0.25" />
      </svg>

      {/* Slow text orbit */}
      <motion.svg
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
        viewBox="0 0 100 100"
        animate={{ rotate: -360 }}
        transition={{ duration: 50, repeat: Infinity, ease: "linear" }}
      >
        <defs>
          <path id="arc-text-path" d="M50 50 m-29,0 a29,29 0 1,1 58,0 a29,29 0 1,1 -58,0" />
        </defs>
        <text fill={AMBER} fontSize="3.6" fontWeight="600" letterSpacing="2.8" opacity="0.5">
          <textPath href="#arc-text-path">J.A.R.V.I.S · MARK LXXXV · </textPath>
        </text>
      </motion.svg>

      {/* Pulse rings */}
      {active && [0, 1.4].map((delay, i) => (
        <motion.div
          key={i}
          style={{
            position: "absolute", borderRadius: "50%",
            width: "52%", height: "52%",
            border: `1px solid ${AMBER}`,
          }}
          animate={{ scale: [1, 1.75], opacity: [0.45, 0] }}
          transition={{ duration: 2.8, repeat: Infinity, ease: "easeOut", delay }}
        />
      ))}

      {/* Core */}
      <motion.div
        style={{
          position: "relative", width: core, height: core, borderRadius: "50%",
          border: "1px solid",
          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
        }}
        animate={{
          borderColor: active
            ? speaking ? `${AMBER}CC` : `${AMBER}70`
            : `${AMBER_DIM}55`,
          backgroundColor: active ? `${AMBER}0D` : "transparent",
        }}
        transition={{ duration: 0.4 }}
      >
        <div
          style={{
            position: "absolute", inset: 4, borderRadius: "50%",
            border: `1px solid ${AMBER}18`,
          }}
        />

        {/* Waveform bars */}
        <div style={{ display: "flex", gap: 2, alignItems: "flex-end", height: 16, marginBottom: 4 }}>
          {[0, 1, 2, 3, 4].map((i) => (
            <motion.div
              key={i}
              style={{ width: 2.5, background: AMBER, borderRadius: 2 }}
              animate={{
                height: speaking
                  ? ["20%", `${38 + Math.sin(i * 1.4) * 48}%`, "20%"]
                  : active ? "28%" : "14%",
                opacity: active ? 0.88 : 0.22,
              }}
              transition={{
                duration: speaking ? 0.38 + i * 0.07 : 0.5,
                repeat: speaking ? Infinity : 0,
                ease: "easeInOut",
                delay: i * 0.05,
              }}
            />
          ))}
        </div>

        <motion.span
          style={{ fontSize: 7, fontWeight: 700, letterSpacing: "0.22em", lineHeight: 1 }}
          animate={{ color: active ? AMBER : "oklch(0.52 0.04 55)" }}
        >
          {active ? (speaking ? "TX" : "RX") : "STDBY"}
        </motion.span>
        <span style={{ fontSize: 5, letterSpacing: "0.1em", color: "oklch(0.52 0.04 55)", marginTop: 2, opacity: 0.5 }}>
          MK LXXXV
        </span>
      </motion.div>

      {/* Corner brackets */}
      {[
        { top: 8,    left: 8,    borderTop: `1px solid ${AMBER}`, borderLeft: `1px solid ${AMBER}` },
        { top: 8,    right: 8,   borderTop: `1px solid ${AMBER}`, borderRight: `1px solid ${AMBER}` },
        { bottom: 8, left: 8,    borderBottom: `1px solid ${AMBER}`, borderLeft: `1px solid ${AMBER}` },
        { bottom: 8, right: 8,   borderBottom: `1px solid ${AMBER}`, borderRight: `1px solid ${AMBER}` },
      ].map((s, i) => (
        <motion.div
          key={i}
          style={{ position: "absolute", width: 12, height: 12, ...s }}
          animate={{ opacity: active ? 1 : 0.35 }}
          transition={{ duration: 0.3 }}
        />
      ))}
    </div>
  );
}
