// SettingsPanel — the ONE global settings surface, identical across every deck.
// It is intentionally self-contained: it talks to the backend over plain HTTP
// (/api/settings) and to Electron safeStorage for API keys, so it needs no deck
// socket and can be mounted once, above all decks, in the root route. Only the
// `accent` prop changes per deck — everything else is byte-identical everywhere.
//
// Secrets never touch this code's persistence: keys go to Electron's OS-encrypted
// store (DPAPI/Keychain) via setApiKeys; the renderer only ever sees booleans.
import { useCallback, useEffect, useRef, useState, type CSSProperties, type Ref } from "react";
import type { ApiKeyStatus } from "@/types/electron";

type VoiceOption = { id: string; label: string };
type Settings = {
  user_name: string;
  voice: string;
  voice_options: VoiceOption[];
  mode: string;
  modes: string[];
  always_listen: boolean;
  store_overheard: boolean;
  stt: boolean;
};

const MODE_HINT: Record<string, string> = {
  auto: "Balance speed, quality & battery automatically",
  eco: "Conserve battery — prefer the cheapest capable model",
  local: "Private & offline — on-device models only",
  cloud: "Max quality — always use the best cloud model",
};

export function SettingsPanel({
  open,
  accent,
  onClose,
}: {
  open: boolean;
  accent: string;
  onClose: () => void;
}) {
  const hasElectron = typeof window !== "undefined" && !!window.electronAPI?.setApiKeys;
  const [s, setS] = useState<Settings | null>(null);
  const [keyStatus, setKeyStatus] = useState<ApiKeyStatus | null>(null);
  const [groqKey, setGroqKey] = useState("");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [mem0Key, setMem0Key] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const firstFieldRef = useRef<HTMLInputElement | null>(null);

  // Load current settings + key status each time the panel opens.
  useEffect(() => {
    if (!open) return;
    setErr("");
    setGroqKey("");
    setAnthropicKey("");
    setMem0Key("");
    fetch("/api/settings")
      .then(async (r) => {
        if (!r.ok) throw new Error(`Settings load failed (${r.status})`);
        return r.json();
      })
      .then(setS)
      .catch((e) => {
        setS(null);
        setErr(e instanceof Error ? e.message : "Couldn't load settings from the backend.");
      });
    if (hasElectron)
      window.electronAPI!.getApiKeyStatus!()
        .then(setKeyStatus)
        .catch(() => {});
    const t = setTimeout(() => firstFieldRef.current?.focus(), 60);
    return () => clearTimeout(t);
  }, [open, hasElectron]);

  // Esc closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const patch = (k: keyof Settings, v: unknown) => setS((p) => (p ? { ...p, [k]: v } : p));

  const openLink = (url: string) => {
    if (window.electronAPI?.openExternal) window.electronAPI.openExternal(url);
    else window.open(url, "_blank", "noopener,noreferrer");
  };

  const save = useCallback(async () => {
    if (!s) {
      setErr("Settings haven't loaded — is the backend running? Close and reopen Settings.");
      return;
    }
    setSaving(true);
    setErr("");
    try {
      // 1) non-secret prefs → backend (broadcasts so every deck stays in sync)
      await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: s.user_name,
          voice: s.voice,
          mode: s.mode,
          always_listen: s.always_listen,
          store_overheard: s.store_overheard,
        }),
      }).then(async (r) => {
        if (!r.ok) {
          const body = await r.text().catch(() => "");
          throw new Error(body || `Settings save failed (${r.status})`);
        }
      });
      // 2) API keys → Electron OS-encrypted store (restarts the backend to pick them up)
      if (hasElectron) {
        const keys: Record<string, string> = {};
        if (groqKey.trim()) keys.GROQ_API_KEY = groqKey.trim();
        if (anthropicKey.trim()) keys.ANTHROPIC_API_KEY = anthropicKey.trim();
        if (mem0Key.trim()) keys.MEM0_API_KEY = mem0Key.trim();
        if (Object.keys(keys).length) {
          setKeyStatus(await window.electronAPI!.setApiKeys!(keys));
        }
      }
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Couldn't save settings.");
    } finally {
      setSaving(false);
    }
  }, [s, groqKey, anthropicKey, mem0Key, hasElectron, onClose]);

  if (!open) return null;

  const c = palette(accent);

  return (
    <div style={c.overlay} className="no-drag" onMouseDown={onClose}>
      <div
        style={c.card}
        onMouseDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
      >
        <div style={c.header}>
          <span style={c.title}>Settings</span>
          <button style={c.close} onClick={onClose} title="Close (Esc)" aria-label="Close">
            ✕
          </button>
        </div>

        <div style={c.body}>
          {/* ── API Keys — the headline: make JARVIS work ── */}
          <div style={c.sectionLabel}>API Keys</div>
          {!hasElectron ? (
            <p style={c.note}>
              Secure key storage runs in the desktop app. In the browser/dev, set keys in your{" "}
              <code>.env</code>.
            </p>
          ) : keyStatus && !keyStatus.secure ? (
            <p style={{ ...c.note, color: "#f0a35e" }}>
              OS secure storage is unavailable here — keys can’t be saved safely.
            </p>
          ) : (
            <p style={c.note}>
              Encrypted with your OS keychain (Windows DPAPI). Never written in plaintext.
            </p>
          )}

          <KeyRow
            c={c}
            label="Groq"
            hint="free & fast — recommended"
            configured={Boolean(keyStatus?.groq)}
            disabled={!hasElectron}
            value={groqKey}
            onChange={setGroqKey}
            placeholder="gsk_…"
            onGet={() => openLink("https://console.groq.com/keys")}
            inputRef={firstFieldRef}
          />
          <KeyRow
            c={c}
            label="Anthropic"
            hint="optional — Claude for the hardest asks"
            configured={Boolean(keyStatus?.anthropic)}
            disabled={!hasElectron}
            value={anthropicKey}
            onChange={setAnthropicKey}
            placeholder="sk-ant-…"
            onGet={() => openLink("https://console.anthropic.com/settings/keys")}
          />
          <KeyRow
            c={c}
            label="Mem0"
            hint="optional — sync memory across devices"
            configured={Boolean(keyStatus?.mem0)}
            disabled={!hasElectron}
            value={mem0Key}
            onChange={setMem0Key}
            placeholder="m0-…"
            onGet={() => openLink("https://app.mem0.ai/dashboard/api-keys")}
          />

          {s && (
            <>
              {/* ── Identity ── */}
              <div style={c.sectionLabel}>Identity</div>
              <input
                style={c.input}
                value={s.user_name}
                onChange={(e) => patch("user_name", e.target.value)}
                placeholder="What should JARVIS call you?"
                maxLength={40}
                spellCheck={false}
              />

              {/* ── Compute mode ── */}
              <div style={c.sectionLabel}>Compute mode</div>
              <div style={c.segRow}>
                {s.modes.map((m) => (
                  <button
                    key={m}
                    onClick={() => patch("mode", m)}
                    style={{ ...c.seg, ...(s.mode === m ? c.segOn : null) }}
                  >
                    {m}
                  </button>
                ))}
              </div>
              <p style={c.note}>{MODE_HINT[s.mode] ?? ""}</p>

              {/* ── Voice ── */}
              <div style={c.sectionLabel}>Voice</div>
              <select
                style={c.input}
                value={s.voice}
                onChange={(e) => patch("voice", e.target.value)}
              >
                {s.voice_options.map((v) => (
                  <option key={v.id} value={v.id} style={{ background: "#141414", color: "#eee" }}>
                    {v.label}
                  </option>
                ))}
              </select>

              {/* ── Privacy ── */}
              <div style={c.sectionLabel}>Privacy</div>
              <Toggle
                c={c}
                label="Always-on listening"
                sub="Auto-start the mic on launch (still only acts on “Jarvis …”)."
                on={s.always_listen}
                onToggle={() => patch("always_listen", !s.always_listen)}
              />
              <Toggle
                c={c}
                label="Remember overheard speech"
                sub="Transcribe & keep ambient audio near the mic. Off = only your commands."
                on={s.store_overheard}
                onToggle={() => patch("store_overheard", !s.store_overheard)}
              />
            </>
          )}

          {err && <p style={{ ...c.note, color: "#f07070" }}>{err}</p>}
        </div>

        <div style={c.footer}>
          <button style={c.ghostBtn} onClick={onClose}>
            Cancel
          </button>
          <button style={c.primaryBtn} onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── small building blocks ──────────────────────────────────────────────────────
type Pal = ReturnType<typeof palette>;

function KeyRow({
  c,
  label,
  hint,
  configured,
  disabled,
  value,
  onChange,
  placeholder,
  onGet,
  inputRef,
}: {
  c: Pal;
  label: string;
  hint: string;
  configured: boolean;
  disabled?: boolean;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  onGet: () => void;
  inputRef?: Ref<HTMLInputElement>;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 5 }}>
        <span style={c.keyLabel}>{label}</span>
        <span style={{ fontSize: 11, color: "rgba(230,235,240,0.4)" }}>{hint}</span>
        <span style={{ flex: 1 }} />
        {configured ? (
          <span style={c.pillOn}>configured ✓</span>
        ) : (
          <span style={c.pillOff}>not set</span>
        )}
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <input
          ref={inputRef}
          style={{ ...c.input, marginBottom: 0, opacity: disabled ? 0.5 : 1 }}
          type="password"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          placeholder={configured ? "•••••••••  (enter a new key to replace)" : placeholder}
          spellCheck={false}
          autoComplete="off"
        />
        <button style={c.getBtn} onClick={onGet} title={`Get a ${label} key`}>
          Get key ↗
        </button>
      </div>
    </div>
  );
}

function Toggle({
  c,
  label,
  sub,
  on,
  onToggle,
}: {
  c: Pal;
  label: string;
  sub: string;
  on: boolean;
  onToggle: () => void;
}) {
  return (
    <div style={c.toggleRow} onClick={onToggle}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, color: "rgba(236,240,244,0.92)" }}>{label}</div>
        <div style={{ fontSize: 11, color: "rgba(230,235,240,0.42)", marginTop: 2 }}>{sub}</div>
      </div>
      <div style={{ ...c.switch, ...(on ? c.switchOn : null) }}>
        <div style={{ ...c.knob, transform: on ? "translateX(16px)" : "translateX(0)" }} />
      </div>
    </div>
  );
}

// ── theme: everything neutral-dark, `accent` is the only deck-specific color ─────
function palette(accent: string) {
  const input: CSSProperties = {
    width: "100%",
    boxSizing: "border-box",
    marginBottom: 4,
    background: "rgba(255,255,255,0.04)",
    border: "1px solid rgba(255,255,255,0.12)",
    borderRadius: 9,
    padding: "9px 11px",
    color: "#eef2f5",
    fontFamily: "inherit",
    fontSize: 13,
    outline: "none",
  };
  return {
    accent,
    overlay: {
      position: "fixed",
      inset: 0,
      zIndex: 100000,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      background: "rgba(4,6,9,0.62)",
      backdropFilter: "blur(6px)",
      fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    } as CSSProperties,
    card: {
      width: "min(460px, 92vw)",
      maxHeight: "88vh",
      display: "flex",
      flexDirection: "column",
      background: "linear-gradient(180deg, #16181d 0%, #101216 100%)",
      border: "1px solid rgba(255,255,255,0.1)",
      borderRadius: 16,
      boxShadow: `0 24px 80px rgba(0,0,0,0.6), 0 0 0 1px ${hexA(accent, 0.14)}`,
      overflow: "hidden",
    } as CSSProperties,
    header: {
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "15px 18px",
      borderBottom: "1px solid rgba(255,255,255,0.07)",
    } as CSSProperties,
    title: {
      fontSize: 15,
      fontWeight: 600,
      letterSpacing: "0.02em",
      color: "#f2f5f8",
    } as CSSProperties,
    close: {
      background: "transparent",
      border: "none",
      color: "rgba(236,240,244,0.55)",
      fontSize: 15,
      cursor: "pointer",
      padding: 4,
      lineHeight: 1,
    } as CSSProperties,
    body: { padding: "16px 18px", overflowY: "auto" } as CSSProperties,
    sectionLabel: {
      fontSize: 10.5,
      fontWeight: 700,
      letterSpacing: "0.12em",
      textTransform: "uppercase",
      color: hexA(accent, 0.85),
      margin: "16px 0 9px",
    } as CSSProperties,
    note: {
      fontSize: 11.5,
      color: "rgba(230,235,240,0.46)",
      margin: "0 0 8px",
      lineHeight: 1.5,
    } as CSSProperties,
    input,
    keyLabel: { fontSize: 13, fontWeight: 600, color: "#eef2f5" } as CSSProperties,
    pillOn: {
      fontSize: 10.5,
      fontWeight: 600,
      color: accent,
      background: hexA(accent, 0.14),
      border: `1px solid ${hexA(accent, 0.4)}`,
      borderRadius: 999,
      padding: "2px 8px",
    } as CSSProperties,
    pillOff: {
      fontSize: 10.5,
      color: "rgba(230,235,240,0.5)",
      background: "rgba(255,255,255,0.05)",
      border: "1px solid rgba(255,255,255,0.12)",
      borderRadius: 999,
      padding: "2px 8px",
    } as CSSProperties,
    getBtn: {
      flexShrink: 0,
      background: "rgba(255,255,255,0.05)",
      border: "1px solid rgba(255,255,255,0.12)",
      borderRadius: 9,
      color: "rgba(236,240,244,0.8)",
      fontFamily: "inherit",
      fontSize: 12,
      padding: "0 12px",
      cursor: "pointer",
      whiteSpace: "nowrap",
    } as CSSProperties,
    segRow: { display: "flex", gap: 5, marginBottom: 4 } as CSSProperties,
    seg: {
      flex: 1,
      textTransform: "capitalize",
      background: "rgba(255,255,255,0.04)",
      border: "1px solid rgba(255,255,255,0.1)",
      borderRadius: 9,
      padding: "8px 0",
      color: "rgba(236,240,244,0.72)",
      fontFamily: "inherit",
      fontSize: 12.5,
      cursor: "pointer",
    } as CSSProperties,
    segOn: {
      background: hexA(accent, 0.16),
      border: `1px solid ${hexA(accent, 0.55)}`,
      color: "#fff",
      fontWeight: 600,
    } as CSSProperties,
    toggleRow: {
      display: "flex",
      alignItems: "center",
      gap: 12,
      padding: "10px 0",
      cursor: "pointer",
      borderBottom: "1px solid rgba(255,255,255,0.05)",
    } as CSSProperties,
    switch: {
      flexShrink: 0,
      width: 34,
      height: 18,
      borderRadius: 999,
      padding: 2,
      background: "rgba(255,255,255,0.14)",
      transition: "background 0.15s",
    } as CSSProperties,
    switchOn: { background: accent } as CSSProperties,
    knob: {
      width: 14,
      height: 14,
      borderRadius: 999,
      background: "#fff",
      transition: "transform 0.15s ease",
      boxShadow: "0 1px 3px rgba(0,0,0,0.4)",
    } as CSSProperties,
    footer: {
      display: "flex",
      justifyContent: "flex-end",
      gap: 8,
      padding: "13px 18px",
      borderTop: "1px solid rgba(255,255,255,0.07)",
    } as CSSProperties,
    ghostBtn: {
      background: "transparent",
      border: "1px solid rgba(255,255,255,0.14)",
      borderRadius: 9,
      color: "rgba(236,240,244,0.8)",
      fontFamily: "inherit",
      fontSize: 13,
      padding: "8px 16px",
      cursor: "pointer",
    } as CSSProperties,
    primaryBtn: {
      background: accent,
      border: "none",
      borderRadius: 9,
      color: "#0a0c10",
      fontFamily: "inherit",
      fontSize: 13,
      fontWeight: 700,
      padding: "8px 20px",
      cursor: "pointer",
    } as CSSProperties,
  };
}

// accent hex → rgba string. Accepts #rgb / #rrggbb; falls back to the raw value.
function hexA(hex: string, a: number): string {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  let h = m[1];
  if (h.length === 3)
    h = h
      .split("")
      .map((x) => x + x)
      .join("");
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}
