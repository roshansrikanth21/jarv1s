import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import OverhaulDeck from "@/decks/overhaul";
import FocusDeck from "@/decks/focus";
import TerminalDeck from "@/decks/terminal";
import PrimeDeck from "@/decks/prime";
import ChatDeck from "@/decks/chat";
import { Onboarding } from "@/components/jarvis/Onboarding";
import { ArcReactor } from "@/components/jarvis/ArcReactor";
import { BootIntro } from "@/components/jarvis/BootIntro";
import { LiveOps } from "@/components/jarvis/LiveOps";
import { SettingsPanel } from "@/components/jarvis/SettingsPanel";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [{ title: "JARVIS" }, { name: "description", content: "JARVIS Command Deck" }],
    links: [
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "anonymous" },
      {
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&display=swap",
      },
      {
        // Prime deck voices: B612/B612 Mono (Airbus cockpit telemetry face),
        // Michroma (micro-labels only), Instrument Serif (JARVIS's spoken text).
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=B612:wght@400;700&family=B612+Mono:wght@400;700&family=Michroma&family=Instrument+Serif:ital@0;1&display=swap",
      },
    ],
  }),
  component: Page,
});

// UI designs, all wired to the same backend — pick one in the corner switcher.
// One canonical amber HUD ("Command Deck" = the fine-tuned overhaul deck). The old
// duplicate `classic` deck was retired; saved prefs pointing at it migrate below.
const PRESETS = [
  { id: "prime", label: "Prime" },
  { id: "overhaul", label: "Command Deck" },
  { id: "focus", label: "Focus" },
  { id: "terminal", label: "Terminal" },
  { id: "chat", label: "Chat" },
];
const DECKS = {
  prime: PrimeDeck,
  overhaul: OverhaulDeck,
  focus: FocusDeck,
  terminal: TerminalDeck,
  chat: ChatDeck,
} as const;

function Page() {
  const [preset, setPreset] = useState<string>(() => {
    try {
      const saved = localStorage.getItem("jarvis_ui_preset");
      // `classic` was folded into the single "Command Deck" (overhaul).
      if (saved === "classic") return "overhaul";
      if (saved && saved in DECKS) return saved;
      return "prime";
    } catch {
      return "prime";
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("jarvis_ui_preset", preset);
    } catch {
      /* ignore */
    }
  }, [preset]);

  // Launch-intro gate — the boot video plays over everything until it finishes.
  const [introDone, setIntroDone] = useState(false);

  // Global Settings — ONE panel, identical on every deck. Opened by the gear in the
  // preset switcher, by Ctrl+, anywhere, or by a `jarvis:open-settings` event any deck
  // (e.g. Prime/Command Deck) fires so their in-chrome gear opens this same panel.
  const [settingsOpen, setSettingsOpen] = useState(false);
  useEffect(() => {
    const openIt = () => setSettingsOpen(true);
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === ",") {
        e.preventDefault();
        setSettingsOpen((v) => !v);
      }
    };
    window.addEventListener("jarvis:open-settings", openIt);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("jarvis:open-settings", openIt);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

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
          try {
            localStorage.setItem("jarvis_user_name", String(d?.user?.name ?? ""));
          } catch {
            /* ignore */
          }
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
    return () => {
      cancelled = true;
    };
  }, []);

  const Deck = DECKS[preset as keyof typeof DECKS] ?? PrimeDeck;
  return (
    <>
      {/* Cinematic launch video overlays everything, then fades to reveal the app.
          Self-skips if the asset is missing / reduced-motion / already seen this launch. */}
      {!introDone && <BootIntro onDone={() => setIntroDone(true)} />}
      {phase === "loading" ? (
        <BootScreen />
      ) : phase === "onboarding" ? (
        <Onboarding onComplete={() => setPhase("ready")} />
      ) : (
        <>
          {/* key forces a clean remount on switch — no stale state bleeds across presets */}
          <Deck key={preset} />
          <PresetSwitcher
            value={preset}
            onChange={setPreset}
            onSettings={() => setSettingsOpen(true)}
          />
          {/* The one global settings surface — same on every deck, themed to its accent. */}
          <SettingsPanel
            open={settingsOpen}
            accent={PRESET_ACCENT[preset] ?? "#c4a5ff"}
            onClose={() => setSettingsOpen(false)}
          />
          {/* Ground-truth feed of real tool executions — proves what JARVIS actually ran. */}
          <LiveOps />
        </>
      )}
    </>
  );
}

function BootScreen() {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 18,
        background: "var(--c-bg, #0a0705)",
        color: "var(--c-amber, oklch(0.68 0.22 38))",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
      }}
    >
      <ArcReactor active size="sm" />
      <span
        style={{ fontSize: 11, letterSpacing: "0.3em", textTransform: "uppercase", opacity: 0.6 }}
      >
        Booting JARVIS…
      </span>
    </div>
  );
}

const PRESET_ACCENT: Record<string, string> = {
  prime: "#c4a5ff",
  overhaul: "#f0b060",
  focus: "#5ec8e8",
  terminal: "#41ff6e",
  chat: "#10a37f",
};

function PresetSwitcher({
  value,
  onChange,
  onSettings,
}: {
  value: string;
  onChange: (v: string) => void;
  onSettings?: () => void;
}) {
  return (
    <div
      className="no-drag"
      style={{
        position: "fixed",
        // Always docked to the very bottom, on every deck — each deck reserves a matching
        // bottom band (paddingBottom) so this sits cleanly BELOW the input, never over it.
        bottom: 10,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 99999,
        display: "flex",
        alignItems: "center",
        gap: 2,
        background: "rgba(8, 10, 14, 0.94)",
        border: "1px solid rgba(255, 255, 255, 0.14)",
        borderRadius: 999,
        padding: 3,
        backdropFilter: "blur(8px)",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        boxShadow: "0 4px 24px rgba(0, 0, 0, 0.45)",
      }}
    >
      <span
        style={{
          fontSize: 8,
          color: "rgba(232, 236, 240, 0.5)",
          letterSpacing: "0.18em",
          padding: "0 6px 0 4px",
        }}
      >
        UI
      </span>
      {PRESETS.map((p) => {
        const active = p.id === value;
        const accent = PRESET_ACCENT[p.id] ?? "#e8a045";
        return (
          <button
            key={p.id}
            onClick={() => onChange(p.id)}
            style={{
              border: "none",
              cursor: "pointer",
              borderRadius: 999,
              padding: "4px 12px",
              fontFamily: "inherit",
              fontSize: 10,
              letterSpacing: "0.04em",
              background: active ? accent : "transparent",
              color: active ? "#07090c" : "rgba(232, 236, 240, 0.78)",
              fontWeight: active ? 700 : 500,
              transition: "color 0.15s, background 0.15s",
            }}
            onMouseEnter={(e) => {
              if (!active) e.currentTarget.style.color = accent;
            }}
            onMouseLeave={(e) => {
              if (!active) e.currentTarget.style.color = "rgba(232, 236, 240, 0.78)";
            }}
          >
            {p.label}
          </button>
        );
      })}
      {/* Global settings entry — same gear on every deck, opens the one shared panel. */}
      {onSettings && (
        <>
          <span
            style={{
              width: 1,
              alignSelf: "stretch",
              background: "rgba(255,255,255,0.12)",
              margin: "3px 3px",
            }}
          />
          <button
            onClick={onSettings}
            title="Settings (Ctrl+,)"
            aria-label="Settings"
            style={{
              border: "none",
              cursor: "pointer",
              borderRadius: 999,
              padding: "4px 9px",
              fontSize: 12,
              lineHeight: 1,
              background: "transparent",
              color: "rgba(232, 236, 240, 0.7)",
              transition: "color 0.15s",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.color = "#fff";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.color = "rgba(232, 236, 240, 0.7)";
            }}
          >
            ⚙
          </button>
        </>
      )}
    </div>
  );
}
