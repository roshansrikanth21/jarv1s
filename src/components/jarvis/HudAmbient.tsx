// HudAmbient — a reactive amber particle field rendered behind the command-deck
// HUD. Depth-parallax motes drift through a volumetric haze; a central bloom and
// the mote energy respond to JARVIS's live state (idle → listening → speaking) and
// mood intensity, so the whole backdrop breathes with the assistant. Canvas 2D
// (no WebGL dependency), DPR-aware, self-cleaning, and honours prefers-reduced-motion.
//
// Pointer-events: none and absolutely positioned — purely decorative, never
// intercepts interaction. Mounted inside overhaul's `.hud-bg` layer.
import { useEffect, useRef } from "react";

export type AmbientState = "idle" | "listening" | "speaking";

type Props = {
  state?: AmbientState;
  /** 0..1 mood intensity — scales bloom + drift energy. */
  intensity?: number;
  /** Base accent as [r,g,b]; defaults to JARVIS copper-amber. */
  rgb?: [number, number, number];
};

type Mote = { x: number; y: number; z: number; vx: number; vy: number; r: number; tw: number };
type Streak = { x: number; y: number; vx: number; vy: number; life: number; max: number };

const AMBER: [number, number, number] = [232, 160, 80];

export function HudAmbient({ state = "idle", intensity = 0.5, rgb = AMBER }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // Live values the animation loop reads without restarting on every prop change.
  const stateRef = useRef(state);
  const intensityRef = useRef(intensity);
  stateRef.current = state;
  intensityRef.current = intensity;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    const [r, g, b] = rgb;
    const rgba = (a: number) => `rgba(${r},${g},${b},${a})`;

    // Sized to the viewport, not the parent box: `.hud-bg` is a full-screen fixed
    // backdrop whose own content-box collapses to 0 (all its children are absolutely
    // positioned), so measuring against it yields nothing. innerWidth/innerHeight is
    // both correct here and immune to that layout quirk.
    let w = 0, h = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
    let motes: Mote[] = [];
    const streaks: Streak[] = [];

    const seed = () => {
      const count = Math.round(Math.min(110, Math.max(46, (w * h) / 22000)));
      motes = Array.from({ length: count }, () => {
        const z = Math.random();                     // depth 0 (far) .. 1 (near)
        return {
          x: Math.random() * w,
          y: Math.random() * h,
          z,
          vx: (Math.random() - 0.5) * (0.06 + z * 0.16),
          vy: (Math.random() - 0.5) * (0.06 + z * 0.16) - 0.03, // faint upward bias
          r: 0.4 + z * 1.7,
          tw: Math.random() * Math.PI * 2,           // twinkle phase
        };
      });
    };

    const resize = () => {
      w = window.innerWidth; h = window.innerHeight;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(w * dpr));
      canvas.height = Math.max(1, Math.floor(h * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      seed();
    };
    resize();

    window.addEventListener("resize", resize);

    // Energy eases toward the target implied by state — no jarring jumps on change.
    let energy = 0.35;
    let bloom = 0;
    let raf = 0;
    let last = performance.now();
    let streakTimer = 0;

    const targetFor = (s: AmbientState) => (s === "speaking" ? 1 : s === "listening" ? 0.62 : 0.32);

    const frame = (now: number) => {
      const dt = Math.min(48, now - last) / 16.67; // ~frames elapsed, clamped
      last = now;

      // Self-heal: if the viewport wasn't measurable at mount (0×0) or has since
      // changed, re-measure. Guarantees the field fills the window once it exists,
      // without depending on a resize event ever firing.
      const vw = window.innerWidth, vh = window.innerHeight;
      if (vw > 0 && vh > 0 && (vw !== w || vh !== h)) resize();
      if (w === 0 || h === 0) { raf = requestAnimationFrame(frame); return; }

      const s = stateRef.current;
      const moodBoost = 0.75 + 0.5 * Math.max(0, Math.min(1, intensityRef.current));
      const target = targetFor(s) * moodBoost;
      energy += (target - energy) * 0.05 * dt;
      const speakPulse = s === "speaking" ? 0.5 + 0.5 * Math.sin(now / 140) : 0;
      bloom += ((s === "idle" ? 0.12 : 0.4 + 0.6 * speakPulse) * moodBoost - bloom) * 0.06 * dt;

      ctx.clearRect(0, 0, w, h);

      // Central volumetric bloom — the "core is powered" glow.
      const cx = w / 2, cy = h * 0.46;
      const rad = Math.max(w, h) * (0.28 + 0.06 * energy);
      const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, rad);
      grad.addColorStop(0, rgba(0.05 + 0.10 * bloom));
      grad.addColorStop(0.5, rgba(0.02 + 0.04 * bloom));
      grad.addColorStop(1, rgba(0));
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, w, h);

      // Motes.
      ctx.globalCompositeOperation = "lighter";
      for (const m of motes) {
        if (!reduced) {
          const spd = 0.5 + energy * 1.6;
          m.x += m.vx * spd * dt;
          m.y += m.vy * spd * dt;
          m.tw += 0.02 * dt;
          if (m.x < -4) m.x = w + 4; else if (m.x > w + 4) m.x = -4;
          if (m.y < -4) m.y = h + 4; else if (m.y > h + 4) m.y = -4;
        }
        const twinkle = 0.6 + 0.4 * Math.sin(m.tw);
        const a = (0.05 + m.z * 0.32) * twinkle * (0.5 + 0.9 * energy);
        ctx.beginPath();
        ctx.arc(m.x, m.y, m.r, 0, Math.PI * 2);
        ctx.fillStyle = rgba(a);
        ctx.fill();
      }

      // Occasional data streak — a fast telemetry mote crossing the field.
      streakTimer -= dt;
      if (!reduced && streakTimer <= 0 && streaks.length < 3) {
        streakTimer = 180 + Math.random() * 260 - energy * 120;
        const edge = Math.random() * h;
        streaks.push({ x: -20, y: edge, vx: 6 + Math.random() * 6, vy: (Math.random() - 0.5) * 1.2, life: 0, max: 60 });
      }
      for (let i = streaks.length - 1; i >= 0; i--) {
        const st = streaks[i];
        st.x += st.vx * dt; st.y += st.vy * dt; st.life += dt;
        const p = st.life / st.max;
        const a = Math.sin(p * Math.PI) * 0.5 * (0.5 + energy);
        ctx.beginPath();
        ctx.moveTo(st.x, st.y);
        ctx.lineTo(st.x - st.vx * 4, st.y - st.vy * 4);
        ctx.strokeStyle = rgba(a);
        ctx.lineWidth = 1.1;
        ctx.stroke();
        if (st.x > w + 30 || st.life > st.max) streaks.splice(i, 1);
      }
      ctx.globalCompositeOperation = "source-over";

      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, [rgb]);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      style={{ position: "fixed", inset: 0, width: "100vw", height: "100vh", pointerEvents: "none" }}
    />
  );
}
