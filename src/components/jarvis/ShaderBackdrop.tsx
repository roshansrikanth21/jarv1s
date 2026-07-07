// ShaderBackdrop — a living amber backdrop behind the command deck (godui layer 1:
// "deep backdrop"). Pure CSS: a SOLID dark base with slow, low-opacity amber glows
// drifting on top. Deliberately NOT WebGL — a full-screen shader canvas showed white
// on some GPUs/Electron (uninitialized framebuffer), washing out the whole UI. CSS
// cannot fail that way: the base is always `--c-bg`, so the backdrop is always dark and
// readable; the glows only ADD warm light. Reacts to JARVIS's state via glow intensity.
//
// Reduced-motion freezes the drift to a static (still pleasant) frame.
import { useEffect, useState } from "react";

export type BackdropState = "idle" | "listening" | "speaking";

export function ShaderBackdrop({ state = "idle" }: { state?: BackdropState }) {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    setReduced(!!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
  }, []);

  // Warm glow lifts when JARVIS is listening/speaking.
  const lvl = state === "speaking" ? 1 : state === "listening" ? 0.6 : 0.32;

  return (
    <div aria-hidden className={`god-bd${reduced ? " god-bd--still" : ""}`}>
      <div className="god-bd-glow god-bd-glow1" style={{ opacity: 0.55 + 0.45 * lvl }} />
      <div className="god-bd-glow god-bd-glow2" style={{ opacity: 0.4 + 0.5 * lvl }} />
      <div className="god-bd-vignette" />
      <style>{`
        .god-bd {
          position: fixed; inset: 0; pointer-events: none; overflow: hidden;
          background: var(--c-bg);               /* solid dark floor — never washes out */
        }
        .god-bd-glow {
          position: absolute; border-radius: 50%; filter: blur(70px);
          mix-blend-mode: screen;                /* add light, never lighten past the glow */
          will-change: transform;
        }
        .god-bd-glow1 {
          width: 90vw; height: 70vh; left: -10vw; top: -25vh;
          background: radial-gradient(circle, oklch(0.60 0.20 42 / 0.42), transparent 62%);
          animation: godBdDrift1 26s ease-in-out infinite;
        }
        .god-bd-glow2 {
          width: 80vw; height: 70vh; right: -18vw; bottom: -28vh;
          background: radial-gradient(circle, oklch(0.55 0.17 34 / 0.34), transparent 64%);
          animation: godBdDrift2 34s ease-in-out infinite;
        }
        .god-bd-vignette {
          position: absolute; inset: 0;
          background: radial-gradient(125% 90% at 50% 42%, transparent 52%, oklch(0.05 0.01 28 / 0.65) 100%);
        }
        @keyframes godBdDrift1 {
          0%,100% { transform: translate(0,0) scale(1); }
          50%     { transform: translate(6vw, 5vh) scale(1.12); }
        }
        @keyframes godBdDrift2 {
          0%,100% { transform: translate(0,0) scale(1.05); }
          50%     { transform: translate(-5vw,-4vh) scale(1); }
        }
        .god-bd--still .god-bd-glow { animation: none; }
      `}</style>
    </div>
  );
}
