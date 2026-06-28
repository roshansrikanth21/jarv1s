# JARVIS — an embodied, compute-elastic personal agent

> A voice-first desktop assistant that treats the machine it runs on as its *body* —
> sensing available compute and scaling its own intelligence (model size, agent
> topology, even its demeanour) to match. The long game is a system that feels less
> like a chatbot and more like something continuous and alive.

This repo is the working foundation for that idea. Today it's a fully functional
Iron-Man-style assistant; the **north star** (and the research-worthy core) is the
**Governor** — a resource- and difficulty-aware controller that, per request, picks
the *minimum* cognition that clears a quality bar within the current latency/energy
budget, escalating up a lattice only when the task is hard or the device has energy
to spare. See [Roadmap](#roadmap).

---

## What it does today

- **The Governor (compute-elastic routing)** — every request is sized up (a cheap
  difficulty estimate), then routed to the *minimum* rung of an escalation lattice that
  clears the bar within the current energy/latency budget:
  `local·fast → cloud·fast / local·deep → cloud·deep → council`. On battery or under
  thermal load it conserves; plugged in and idle it spends freely and escalates. A
  **LinUCB contextual bandit** adapts the policy to *your* machine from observed latency
  + whether you had to re-ask. Modes: **Auto · Eco · Local (private) · Cloud**. Live in
  the Governor panel. (`governor.py`)
- **Embodiment / homeostasis** — the machine is the agent's body: battery, thermal, load
  and free RAM produce an "energy" state that dims the arc reactor, paces the TTS, trims
  verbosity, and biases routing. Idle + charging triggers a **"sleep"** cycle that
  consolidates the day's conversation into durable memory. (`device.py`, `consolidation.py`)
- **Model Advisor** — profiles your hardware tier and shows installed Ollama models (with
  tool-capability), live tokens/sec **benchmarks**, and tier-matched **recommendations**
  you can pull in one click (Rig panel). (`models_advisor.py`)
- **Voice loop** — mic → VAD → Groq Whisper STT → wake-word gate → agent → Edge neural
  TTS, with barge-in, an echo guard, and hallucination filtering.
- **Agentic brain** — a tool-calling loop behind one unified `Brain` contract spanning
  Groq, Claude, and any local Ollama model, with multi-turn memory and fillers.
- **Affect & persona — a PAD emotion engine** — a live mood in Pleasure-Arousal-
  Dominance space over a sharp/dry/dominant baseline, nudged by how the conversation
  goes and relaxed back home by a half-life **decay timer**. It tints JARVIS's register
  (and a touch of his TTS) every reply. Sarcasm dial: `playful · sharp · savage`; kill
  switch `JARVIS_EMOTION=0`. (`persona.py`)
- **Perceptual intelligence** — reads the user's affect/intent from the transcript
  (frustration, stress, gratitude, banter, insults, a "good morning" sent at 1 AM…)
  plus a mic-loudness cue, and steers the mood. Genuine distress always **suppresses the
  comedy** — playful never tips into offensive. (`perception.py`)
- **Ambient awareness** — time of day, IP-based location, and live weather (both keyless)
  ground his answers and his wit, and back a new `get_weather` tool. (`ambient.py`)
- **Tools** — memory, web search, system stats, app launch, tasks, screen capture +
  vision, shell, ICT market scan, and **open the c0mr4des trading terminal**.
- **Council (Mixture-of-Agents)** — *"deliberate …"* convenes a panel of models that
  debate; a chair reconciles one decision. It's the top rung of the lattice.
- **Markets / ICT scanner** — Smart-Money reads for Indian markets: structure (BOS/CHoCH),
  FVGs, order blocks, liquidity, premium/discount equilibrium, a 0–100 confluence score,
  **higher-timeframe confluence**, **session status**, and a draft **trade plan**
  (entry/SL/TP/RR). Plus a TradingView chart and a signal watcher. *Analysis only.*
- **Inspectable self-model** — durable memories with importance + decay, viewable and
  forgettable in the Memory panel.
- **Secure & persistent** — API keys encrypted via the OS keychain (Electron
  `safeStorage`); memories, history, tasks, and the learned policy survive restarts.

## Architecture

```
Electron shell ──spawns──► Python FastAPI backend (api.py)  ◄──WebSocket──►  React HUD
   (electron/)                  brain · tools · voice · affect · markets         (TanStack Start, src/)
```

- **Backend** — `api.py` (FastAPI + WebSocket). One file, deliberately.
- **Frontend** — TanStack Start + React 19 + Tailwind v4, hand-rolled HUD in
  `src/routes/index.tsx` (+ `src/components/jarvis/ArcReactor.tsx`).
- **Desktop** — `electron/main.js` boots the Python backend and loads the UI.

## Setup

**1. Python backend** (needs Python 3.10+):

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt      # Windows
# venv/bin/pip install -r requirements.txt         # macOS/Linux
```

**2. Keys** — copy `.env.example` to `.env` and set at least one brain:

```
GROQ_API_KEY=gsk_...        # free at console.groq.com — primary brain + Whisper STT
ANTHROPIC_API_KEY=sk-ant-...# optional fallback
# JARVIS_USER=Navneet       # who JARVIS serves (used in its system prompt)
```

> Voice input (Whisper STT) and screen vision both run through Groq, so a
> `GROQ_API_KEY` is required for the full experience.

**3. Frontend deps:**

```bash
npm install
```

## Run

| Command | What it does |
|---|---|
| `npm run desktop:dev` | Dev: Vite UI (:8080) + Electron, backend auto-spawned. Best for iterating. |
| `npm run desktop` | Production: builds the static SPA + launches Electron (the backend serves the UI from `dist/client`). |
| `python api.py` | Backend only on :8000 (serves the built SPA if present). |
| `npm run dev` | Frontend only (proxies `/api` + `/ws` to :8000). |
| `npm run build` / `npx tsc --noEmit` | Build / typecheck. |

The Governor's local rungs need **Ollama** running with at least one tool-capable model
(e.g. `ollama pull qwen2.5:7b`); cloud rungs need the matching key. With neither, JARVIS
tells you what to add.

## Security notes

- The agent can run shell commands and launch apps. The WebSocket therefore
  **rejects non-local Origins** (any website could otherwise open a socket to
  `localhost` and drive it) — override with `JARVIS_WS_ALLOW_ALL=1` only if you
  know why. An in-UI approval gate + audit log for shell actions is planned.
- Treat `capture_screen` / `search_web` output as untrusted: it flows into a model
  that can call tools (indirect prompt-injection surface). Hardening is on the roadmap.

## Roadmap

- **Phase 0 — Foundation (done):** unified `Brain` contract, persisted state,
  `/api/device`, WS origin lockdown, secure key storage, cleanup.
- **Phase 1 — Device Profiler + Model Advisor (done):** hardware tiering, on-device
  tok/s benchmarks, tier-matched recommendations, one-click pulls.
- **Phase 2 — The Governor (done):** difficulty- and resource-aware routing over the
  escalation lattice, with a per-machine LinUCB bandit and explainable decisions.
- **Phase 3 — Embodiment (done):** device-grounded homeostasis (reactor/voice/verbosity
  scale with the body) + idle-time "sleep" memory consolidation + inspectable self-model.
- **Phase 4 — Online learning + evaluation (in progress):** the bandit already adapts
  per machine; next is a metrics study of quality vs latency vs energy across tiers.

## Configuration (env)

Common overrides: `GROQ_MODEL`, `GROQ_REASONING_EFFORT`, `CLAUDE_MODEL`,
`JARVIS_TTS_VOICE`, `JARVIS_WAKE_WORDS`, `JARVIS_WAKE_REQUIRED`, `JARVIS_WATCHLIST`,
`JARVIS_WATCH_INTERVAL_MIN`, `JARVIS_USER`, `JARVIS_WS_ALLOW_ALL`, `JARVIS_EMOTION`, `JARVIS_SARCASM`, `JARVIS_HOME_CITY`. See `AFFECT.md` and the top of
`api.py` for the full list and defaults.
