import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import ClassicDeck from "@/decks/classic";
import OverhaulDeck from "@/decks/overhaul";
import { Onboarding } from "@/components/jarvis/Onboarding";
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

// Two full UI designs, both wired to the same backend — pick one in the corner switcher.
const PRESETS = [
  { id: "classic",  label: "Command Deck" },
  { id: "overhaul", label: "Overhaul" },
];

function Page() {
  const [preset, setPreset] = useState<string>(() => {
    try { return localStorage.getItem("jarvis_ui_preset") || "overhaul"; } catch { return "overhaul"; }
  });
  useEffect(() => {
    try { localStorage.setItem("jarvis_ui_preset", preset); } catch { /* ignore */ }
  }, [preset]);

  // First-run gate: does the backend already know the operator's name?
  const [phase, setPhase] = useState<"loading" | "onboarding" | "ready">("loading");
  useEffect(() => {
    let cancelled = false;
    fetch("/api/agent/status")
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return;
        const onboarded = Boolean(d?.user?.onboarded);
        if (onboarded) {
          try { localStorage.setItem("jarvis_user_name", String(d?.user?.name ?? "")); } catch { /* ignore */ }
        }
        setPhase(onboarded ? "ready" : "onboarding");
      })
      .catch(() => {
        if (cancelled) return;
        // Backend briefly down — don't trap returning users in onboarding.
        try {
          const cached = localStorage.getItem("jarvis_user_name");
          setPhase(cached?.trim() ? "ready" : "onboarding");
        } catch {
          setPhase("onboarding");
        }
      });
    return () => { cancelled = true; };
  }, []);

  if (phase === "loading") return <BootScreen />;
  if (phase === "onboarding") return <Onboarding onComplete={() => setPhase("ready")} />;

  const Deck = preset === "classic" ? ClassicDeck : OverhaulDeck;
  return (
    <>
      {/* key forces a clean remount on switch — no stale state bleeds across presets */}
      <Deck key={preset} />
      <PresetSwitcher value={preset} onChange={setPreset} />
    </>
  );
}

function BootScreen() {
  return (
    <div style={{
      position: "fixed", inset: 0, display: "flex", flexDirection: "column",
      alignItems: "center", justifyContent: "center", gap: 18,
      background: "var(--c-bg, #0a0705)", color: "var(--c-amber, oklch(0.68 0.22 38))",
      fontFamily: "JetBrains Mono, ui-monospace, monospace",
    }}>
      <ArcReactor active size="sm" />
      <span style={{ fontSize: 11, letterSpacing: "0.3em", textTransform: "uppercase", opacity: 0.6 }}>
        Booting JARVIS…
      </span>
    </div>
  );
}

function PresetSwitcher({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const AMBER = "oklch(0.68 0.22 38)";
  return (
    <div
      style={{
        position: "fixed", bottom: 10, left: "50%", transform: "translateX(-50%)",
        zIndex: 99999, display: "flex", alignItems: "center", gap: 2,
        background: "rgba(10,7,5,0.9)", border: `1px solid ${AMBER}40`,
        borderRadius: 999, padding: 3, backdropFilter: "blur(6px)",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        boxShadow: `0 2px 18px ${AMBER}22`,
      }}
    >
      <span style={{ fontSize: 8, opacity: 0.45, letterSpacing: "0.18em", padding: "0 6px 0 4px" }}>UI</span>
      {PRESETS.map((p) => {
        const active = p.id === value;
        return (
          <button
            key={p.id}
            onClick={() => onChange(p.id)}
            style={{
              border: "none", cursor: "pointer", borderRadius: 999,
              padding: "4px 12px", fontFamily: "inherit", fontSize: 10, letterSpacing: "0.04em",
              background: active ? AMBER : "transparent",
              color: active ? "#0a0705" : `${AMBER}aa`,
              fontWeight: active ? 700 : 500, transition: "all 0.15s",
            }}
          >
            {p.label}
          </button>
        );
      })}
    </div>
  );
}
