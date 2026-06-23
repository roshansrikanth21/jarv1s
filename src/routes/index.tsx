import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import ClassicDeck from "@/decks/classic";
import OverhaulDeck from "@/decks/overhaul";

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
    try { return localStorage.getItem("jarvis_ui_preset") || "classic"; } catch { return "classic"; }
  });
  useEffect(() => {
    try { localStorage.setItem("jarvis_ui_preset", preset); } catch { /* ignore */ }
  }, [preset]);

  const Deck = preset === "overhaul" ? OverhaulDeck : ClassicDeck;

  return (
    <>
      <Deck />
      <PresetSwitcher value={preset} onChange={setPreset} />
    </>
  );
}

function PresetSwitcher({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const AMBER = "oklch(0.68 0.22 38)";
  return (
    <div
      title="Switch UI preset"
      style={{
        position: "fixed", bottom: 8, left: 8, zIndex: 99999,
        display: "flex", alignItems: "center", gap: 6,
        background: "rgba(10,7,5,0.82)", border: `1px solid ${AMBER}33`,
        borderRadius: 6, padding: "3px 7px", backdropFilter: "blur(4px)",
        fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 10, color: AMBER,
      }}
    >
      <span style={{ opacity: 0.55, letterSpacing: "0.12em" }}>UI</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{
          background: "transparent", color: AMBER, border: "none", outline: "none",
          fontFamily: "inherit", fontSize: 10, cursor: "pointer",
        }}
      >
        {PRESETS.map((p) => (
          <option key={p.id} value={p.id} style={{ background: "#120a06", color: "#eee" }}>{p.label}</option>
        ))}
      </select>
    </div>
  );
}
