# J.A.R.V.I.S — Just A Rather Very Intelligent System

A voice-first AI command deck for the desktop — an Iron-Man-style HUD that talks back,
remembers the conversation, reasons with a panel of models, sees your screen, and reads
the Indian markets with ICT / smart-money analysis. Runs on **free cloud models** (Groq),
so it works fine on a laptop with **no GPU**.

> Built by [@roshansrikanth21](https://github.com/roshansrikanth21). Personal assistant +
> security/CTF sidekick + trading analyst.

---

## Features

- **Cloud brain (free, no GPU)** — defaults to `openai/gpt-oss-120b` on Groq, a true
  reasoning model that keeps its chain-of-thought hidden and answers concisely.
- **Real conversation** — multi-turn memory, natural spoken style, follow-ups
  (“and the RAM?”, “spell it backwards”). Say *“new conversation”* to reset.
- **Wake word + barge-in** — say **“JARVIS …”** to issue a command; it ignores everything
  else. Talk over it (or say “JARVIS stop”) to interrupt mid-sentence.
- **Pick your voice** — 9 Microsoft Edge neural voices (British/US/Australian, m/f),
  switchable live from the dropdown by the mic.
- **Mixture-of-Agents council** — say *“deliberate: …”* and three different models debate
  a question independently, then a chair reconciles them into one decision.
- **Screen vision** — *“what’s on my screen?”* uses cloud vision (Llama 4), no local model.
- **Markets (Indian) + ICT scanner** — Nifty 50, Sensex, Bank Nifty, any NSE stock.
  Detects market structure (BOS/CHoCH), fair-value gaps, order blocks, liquidity sweeps,
  buy/sell-side liquidity, higher-timeframe confluence, session timing, and drafts an
  entry/SL/TP plan. Live TradingView chart in the **Markets** tab.
- **Scheduled watcher** — scans a watchlist every N minutes and alerts on fresh signals.
- **Tools** — memory, web search, system info, app launch, task queue, screen capture,
  shell, and the ICT scanner — orchestrated by the agent.

> ⚠️ **Trading is analysis-only.** JARVIS reads markets, flags setups, and drafts levels.
> It will **never place an order or move money** — you execute trades yourself.

---

## Stack

| Layer | Tech |
|-------|------|
| Desktop shell | Electron (frameless HUD) |
| UI | React + Vite + Tailwind + Framer Motion |
| Backend | FastAPI (Python), WebSocket streaming |
| Brain / vision / STT | Groq (OpenAI-compatible API) |
| TTS | Microsoft Edge neural voices (`edge-tts`) |
| Market data | `yfinance` + `pandas` |
| Local fallback | Ollama (optional) |

The Python backend does the LLM/voice/market work; Electron renders the HUD and spawns
the backend automatically.

---

## Setup

**Prerequisites:** Python 3.11+, Node 18+, and a free Groq API key
([console.groq.com](https://console.groq.com) — no credit card).

```bash
git clone https://github.com/roshansrikanth21/jarv1s.git
cd jarv1s

# 1. Python backend
python -m venv venv
venv\Scripts\pip install -r requirements.txt      # Windows
# venv/bin/pip install -r requirements.txt         # macOS/Linux

# 2. Secrets
copy .env.example .env                              # Windows  (cp on *nix)
#   then edit .env and set GROQ_API_KEY=gsk_...

# 3. Frontend deps
npm install

# 4. Run the desktop app (starts Vite + Electron + backend)
npm run desktop:dev
```

Electron looks for the Python venv at `./venv` or `../venv`, or set `JARVIS_PYTHON` to a
specific interpreter.

For voice input you also need a working microphone; transcription uses Groq Whisper
(no extra install). Screen vision and the brain are fully cloud — **no GPU required**.

---

## Usage

- **Type** in the command bar, or click the **mic** and speak (prefix with *“JARVIS”*).
- **Deliberate:** *“deliberate: rewrite my toolkit in Rust or stay on Python?”*
- **Markets:** *“scan Nifty for ICT setups on the 15 minute”*, or open the **Markets** tab,
  pick Nifty/Sensex/Bank Nifty (or type an NSE ticker), and hit **Start watcher**.
- **Screen:** *“what’s on my screen?”* / **Scan Screen** button.
- **Memory:** *“remember that …”*, *“what do you know about me?”*

---

## Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `GROQ_API_KEY` | — | **Required.** Free Groq key. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Primary brain. |
| `GROQ_REASONING_EFFORT` | `low` | `low`/`medium`/`high` (gpt-oss). |
| `GROQ_VISION_MODEL` | `meta-llama/llama-4-scout-17b-16e-instruct` | Screen vision. |
| `GROQ_STT_MODEL` | `whisper-large-v3-turbo` | Speech-to-text. |
| `JARVIS_TTS_VOICE` | `en-GB-RyanNeural` | Default voice. |
| `JARVIS_TTS_RATE` / `JARVIS_TTS_PITCH` | `+8%` / `+0Hz` | Voice tuning. |
| `JARVIS_WAKE_WORDS` | `jarvis,…` | Wake words (comma-sep). |
| `JARVIS_WAKE_REQUIRED` | `1` | `0` to disable wake-word gating. |
| `JARVIS_MOA_PROPOSERS` | llama-3.3-70b, qwen3-32b, llama-4-scout | Council panel. |
| `JARVIS_MOA_AGGREGATOR` | `openai/gpt-oss-120b` | Council chair. |
| `JARVIS_WATCHLIST` | `nifty,sensex` | ICT watcher symbols. |
| `JARVIS_WATCH_INTERVAL_MIN` | `5` | Watcher scan interval. |
| `JARVIS_WATCH_TF` | `15m` | Watcher timeframe. |
| `JARVIS_WATCH_SPEAK` | `1` | Speak watcher alerts aloud. |
| `ANTHROPIC_API_KEY` | — | Optional Claude fallback. |

---

## Project layout

```
api.py                 FastAPI backend — brain, voice, vision, tools, ICT engine, watcher
requirements.txt       Python deps
electron/              Electron main + preload (window, backend spawn)
src/routes/index.tsx   The HUD (command deck, panels, Markets, Council)
src/components/         UI components (ArcReactor, …)
.env.example           Config template
```

---

## Notes

- Markets data is delayed/last-session outside NSE hours (09:15–15:30 IST, Mon–Fri); the
  watcher only fires during the session.
- Your `.env` (API keys) is gitignored — never commit it.
- Not financial advice. Not affiliated with Marvel.
