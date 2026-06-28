# JARVIS — Status Audit (2026-06-24)

Snapshot after merging the teammate's `dev` (compute layer + UI overhaul) into `main`.
Legend: ✅ done & verified · 🟡 done, not verified end-to-end · ⛔ not started.

---

## 0. The "both UIs look the same" symptom — diagnosed

**Not a case of identical UIs.** Evidence:
- `src/decks/overhaul.tsx` has **56** of the teammate's distinctive elements (Governor/device/tier/Settings/battery/API-key panels); `src/decks/classic.tsx` has **0**. They are genuinely different designs.
- The switcher is correctly wired: `routeTree.gen.ts` maps `/` → the switcher in `src/routes/index.tsx`; logic renders `OverhaulDeck` vs `ClassicDeck` off persisted state.

**So the likely causes (need a live look to confirm):**
1. The **bottom-left "UI" dropdown** defaults to *Command Deck* (classic) — you have to actually pick **Overhaul** to see the other one.
2. Both descend from the same "command deck" base, so at a glance the amber HUD layout reads similar; the differences are in panels/Settings/tabs.
3. Possible the tiny switcher widget is hard to spot or overlapped by a deck footer (z-index/placement).

**Action:** toggle it once. If both render identically *after* toggling → live render bug I'd need eyes-on (or a screenshot) to fix.

---

## 1. Merge / integration ✅
- `main` = `509e807`, pushed to `jarv1s`. Teammate's `dev` + your work unified; nothing lost.
- 4 UI files dev deleted (`Panel`, `SuitBlueprint`, `Waveform`, `lib/utils`) **restored** (classic deck needs them).
- Backend boots; `tsc` clean; both decks build.

## 2. Backend compute layer (teammate) 🟡
- `governor.py` (compute-elastic router), `device.py` (power/thermal), `consolidation.py` (memory sleep-cycle), `models_advisor.py` (local-model profiler) — **fully wired** in the request flow (`governor.decide`, `device.profile`, `consolidation.consolidate` all called), boots (`Governor rungs: ['cloud_fast','council'] | tier: balanced`).
- 🟡 **Not exercised live:** model pull/benchmark via `models_advisor`, the idle consolidation cycle, governor escalation under load. Needs a real session to confirm behavior.

## 3. Your features (carried through merge) 🟡
- gpt-oss-120b brain, conversation memory, wake word + barge-in, 9 voices, Mixture-of-Agents council, vision (screen), ICT scanner (Nifty/Sensex + HTF confluence/trade-plan/session) — present in the superset `api.py`, backend confirms council + voice config. 🟡 re-verify in the merged app (esp. inside the Overhaul deck).

## 4. Trading terminal (c0mr4des) 🟡
- Cloned to `../c0mr4des_terminal`; Python `.venv` + `node_modules` installed; backend **verified booting on :8100**. Launcher wired (Electron window + `open_trading` tool + header button).
- 🟡 **Not verified end-to-end:** actually opening the window from JARVIS (button/voice) and the c0mr4des UI rendering.
- ⛔ Its `vite.config` port edit is **local only** (not committed to the c0mr4des repo).
- ⛔ Broker (Angel One) creds + its Gemini key for AI chart analysis **not configured** → those features degrade.

## 5. Video & image understanding (claude-video) ⛔
- **Not started.** Plan stands: adapt claude-video's method (yt-dlp → ffmpeg frames → Whisper) to JARVIS's free Groq Whisper + Llama-4 vision. Needs `ffmpeg` + `yt-dlp`; route temp files to K:. Disk now freed (~72 GB), so unblocked.
- `analyze_image(path)` tool (trivial, reuses Groq vision) — also not added yet.

## 6. Reuse from K: folders (surveyed, not integrated) ⛔
- **E.D.I.T.H** cheap pure-Python tools (weather, YouTube, news, calculator) — easy wins, not lifted.
- PyAutoGUI desktop control, Piper offline TTS, Picovoice/clap wake — not adopted.
- Semantic memory: the teammate's `consolidation.py` now covers this idea, so the heavy torch/chromadb path is no longer needed.
- `word-vectorization-model` / `ai-market-analyser` — skip (heavy / redundant).

## 7. Housekeeping ⛔ / 🟡
- `requirements.txt`: backend booted on the existing venv, but the file should be reconciled for a clean fresh-clone (verify `ollama` etc. are listed). 🟡
- `README.md`: currently the teammate's version — may not document your trading/ICT/voice/**UI-preset** additions. ⛔
- `merge-dev` throwaway branch still exists (safe to delete). 🟡
- UI preset switcher is minimal (corner dropdown) — could move into a proper Settings panel. ⛔

---

## Recommended order
1. **Verify the UI switcher live** (toggle → Overhaul). Fix placement/visibility if needed.
2. **End-to-end test the trading launch** (open c0mr4des from JARVIS), commit the c0mr4des port edit.
3. **Video + image understanding** (now unblocked by disk).
4. **Lift E.D.I.T.H's cheap tools** (weather/news/youtube/calculator).
5. Reconcile `requirements.txt` + rewrite `README` for the unified project; delete `merge-dev`.

---

## 8. Affect / perception / ambient (2026-06-28) ✅
New, dependency-free layer giving JARVIS a temperament, a read on the user, and a
sense of its surroundings. All wired into `api.py` and guarded by `JARVIS_EMOTION`
(off => original static persona). No new pip deps (stdlib only).
- `persona.py` — **PAD emotion engine**: sharp/dry/dominant baseline, event nudges,
  exponential **decay timer** back to baseline, 10 named moods, sarcasm dial
  (`playful`/`sharp`/`savage`), JSON persistence, prompt `[Affect]` block + subtle TTS bias.
- `perception.py` — **perceptual intelligence**: classifies user affect/intent from the
  transcript (+ mic-loudness cue) → PAD nudge + per-turn guidance. **Distress always
  suppresses sarcasm** (the "never offensive" guarantee).
- `ambient.py` — time-of-day + IP geolocation (`ip-api.com`) + weather (Open-Meteo),
  both keyless, cached, offline-safe; new `get_weather` tool; `[Surroundings]` prompt line.
- Verified: `python -m py_compile` clean; `python scripts/selftest_affect.py` → **27/27**;
  live import of `api.py` exercised `_build_system_prompt`, `_update_affect`, `agent_status`,
  `_voice_params`, and the `get_weather` tool.
- STT verdict: the existing Groq Whisper large-v3-turbo + energy-VAD + wake-word/echo
  guard is **good — kept as-is**; perception is layered *on top* (text + loudness), not a
  replacement.
- ⛔ Not yet: a HUD readout for the mood (data is already exposed on `/api/agent/status`
  and the `emotion` WS event — UI wiring is a small follow-up).
