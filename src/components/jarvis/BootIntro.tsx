// BootIntro — full-screen cinematic launch video (the JARVIS "systems online" boot
// sequence) that plays once per app launch, then cross-fades into the live UI.
//
// Robust by design: if the video asset is missing, errors, or the user has reduced
// motion enabled, it calls onDone immediately so the app never gets stuck behind it.
// A Skip control is always available. Gated by sessionStorage so it plays on launch,
// not on every hot-reload during development.
import { useEffect, useRef, useState } from "react";

const SRC = "./intro.mp4";                 // drop the rendered clip at public/intro.mp4
const SEEN_KEY = "jarvis_intro_seen";

export function BootIntro({ onDone }: { onDone: () => void }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [fading, setFading] = useState(false);
  const doneRef = useRef(false);

  const finish = () => {
    if (doneRef.current) return;
    doneRef.current = true;
    setFading(true);
    try { sessionStorage.setItem(SEEN_KEY, "1"); } catch { /* ignore */ }
    // let the fade play before unmounting
    setTimeout(onDone, 650);
  };

  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    let seen = false;
    try { seen = sessionStorage.getItem(SEEN_KEY) === "1"; } catch { /* ignore */ }
    if (reduce || seen) { onDone(); return; }

    const v = videoRef.current;
    if (!v) { onDone(); return; }
    // If the asset can't start playing within a beat, don't hold the app hostage.
    const guard = setTimeout(() => { if (v.readyState < 2) finish(); }, 1600);
    v.play?.().catch(() => finish());
    return () => clearTimeout(guard);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      style={{
        position: "fixed", inset: 0, zIndex: 100000, background: "#0a0705",
        display: "flex", alignItems: "center", justifyContent: "center",
        opacity: fading ? 0 : 1, transition: "opacity 0.6s ease",
        pointerEvents: fading ? "none" : "auto",
      }}
    >
      <video
        ref={videoRef}
        src={SRC}
        muted
        autoPlay
        playsInline
        onEnded={finish}
        onError={finish}
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
      />
      <button
        onClick={finish}
        className="no-drag"
        style={{
          position: "absolute", bottom: 26, right: 28,
          background: "rgba(10,7,5,0.6)", border: "1px solid oklch(0.68 0.22 38 / 0.5)",
          color: "oklch(0.68 0.22 38)", borderRadius: 999, padding: "6px 16px",
          fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 11,
          letterSpacing: "0.18em", textTransform: "uppercase", cursor: "pointer",
          backdropFilter: "blur(6px)",
        }}
      >
        Skip →
      </button>
    </div>
  );
}
