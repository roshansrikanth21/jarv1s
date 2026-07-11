# cortex — JARVIS's cognitive memory

> **Metaphor:** past / present / future — three tables that mirror how humans hold
> time. Every model call flows through this before it reaches the LLM.

Where the old memory was a flat JSON list of durable facts plus a rolling history
window, cortex is a proper cognitive substrate:

```
   mic / text ──► _build_system_prompt(query)
                     │
                     ▼
             cortex.build_system_prompt(query, hooks)
             ├─ persona (from persona.py, unchanged)
             ├─ emotion (PAD mirror + attachment, from cortex.store)
             ├─ facts    ← vectors.search_facts(query, k=8)   ─┐
             └─ episodes ← vectors.search_episodes(query, k=3) ─┴─ semantic recall
                                                                    (Ollama nomic-embed-text
                                                                     → WordHashEmbedder)

   user↔assistant exchange ─► cortex.record_turn(user, assistant)
                                    │
                                    ├─ SQLite: episodes ← the raw turn
                                    ├─ Chroma: episodes index
                                    └─ router("extraction", …)  ── fire-and-forget
                                              │
                                              ├─ facts ─── SQLite + Chroma
                                              ├─ prospective ── SQLite
                                              └─ emotion_signals ── nudge emotion_state

   ~3 AM cron ─► python -m cortex.dreaming
                    │
                    └─ router("consolidation", …) — long-ctx pass over the day →
                       new episode source='dreaming-summary'; raw turns get their
                       `summary` field pointed at it. New facts + prospective too.
```

## Storage

SQLite (WAL) at `memory/cortex.sqlite`. Chroma (persistent) at `memory/chroma/`.
SQL is authoritative; Chroma is the index.

| table          | maps to  | rows                                                                                                                                                              |
| -------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `episodes`     | past     | `(id, timestamp, raw_text, summary, source)` — `source ∈ {chat, dreaming-summary, voice, whatsapp, …}`; `summary` is nullable and points at a dreaming episode id |
| `facts`        | present  | `(id, text, category, confidence 0..1, created_at, last_confirmed_at, source_episode_id, namespace, source_model, private, importance, access_count, last_access)` — categories: preference / situation / person / identity / skill |
| `prospective`  | future   | `(id, description, due_at, recurrence, status, created_at, source_episode_id)` — `status ∈ {pending, done, snoozed, cancelled}`                                    |
| `emotion_state`| mood     | single-row PAD + attachment vector, all in [-1, 1]                                                                                                                 |

All timestamps ISO-8601 UTC. Foreign keys from `facts` / `prospective` back to the
episode that spawned them.

## Read path — every LLM call flows through this

`cortex.build_system_prompt(query, hooks)` assembles the system message in a fixed
order, capped at ~2000 tokens by default:

1. **Persona** (`base_prompt`, from `_BASE_PROMPT` in `api.py`)
2. **Ambient** (`hooks.ambient_fragment` — time/place/weather)
3. **Persona style block** (`hooks.persona_block` — live PAD + how to read this turn)
4. **Homeostasis** (`hooks.homeostasis_line` — low-energy nudge, if any)
5. **[Emotion]** paragraph (from `cortex.store.emotion_state`)
6. **What you know about the user** — `vectors.search_facts(query, k=8)`
7. **Recent context** — `vectors.search_episodes(query, k=3)`
8. **Overheard nearby** — `hooks.overheard` (unchanged from the current buffer)

Under budget pressure, episodes drop first (lower priority), then lower-confidence
facts. **Persona, ambient, homeostasis, and [Emotion] never trim** — they're what
keep JARVIS *himself* even under a tight cap.

## Write path — after every user↔assistant exchange

`cortex.record_turn(user, assistant)`:

1. Persist the raw turn as an `episodes` row (`source='chat'`).
2. Kick `cortex.extract.schedule(...)` — one router call, `task_type='extraction'`,
   JSON output — fire-and-forget so it never blocks the reply.
3. Extraction returns three tracks:
   - `facts` → `store.add_fact(...)` (dedup + confidence bump on exact-match)
   - `prospective_items` → `store.add_prospective(...)`
   - `emotion_signals` → `store.nudge_emotion(...)` (each axis clamped to [-0.2, 0.2])

If any provider is missing or the model returns junk, extraction silently skips —
the reply is already out.

## The router

`cortex.router.route(task_type, prompt, ...)` — one entry point for every LLM call
the cortex itself makes. Chat turns still flow through JARVIS's Governor + brain
layer (that path already uses `cortex.build_system_prompt`).

| task_type       | picks                                                                                    | why                                          |
| --------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------- |
| `extraction`    | local fast (`qwen2.5:7b` via Ollama) → Groq gpt-oss-20b → Claude Haiku                    | cheap, quick, JSON out                       |
| `consolidation` | Groq gpt-oss-120b → Claude → local deep                                                  | long-context: the day's raw turns in one go |
| `reflection`    | local fast → Groq → Claude                                                               | short generative                             |

Never raises. Returns `""` on a total miss.

## Nightly consolidation — "dreaming"

Cron/Task-Scheduler runs `python -m cortex.dreaming` at ~3 AM:

1. Pull every `chat` episode whose `summary` is NULL for the target day.
2. Send the batch through `router('consolidation', ...)`.
3. Model returns `{ summary, facts, prospective_items }`.
4. Insert one new `episodes` row `source='dreaming-summary'` holding the narrative.
5. Mark raw turns as consolidated — their `summary` field points at the new episode.
6. Insert newly-noticed facts + prospective back-linked to the summary episode.

Old raw turns stay in the DB, still searchable, but the daily summary is what
usually surfaces during retrieval — the day compresses cleanly.

**Schedule it (Windows Task Scheduler):**
```
schtasks /Create /SC DAILY /ST 03:00 /TN "JARVIS Dreaming" ^
  /TR "C:\Users\jyoth\Documents\Claude\Projects\Jarv1s\venv\Scripts\python.exe -m cortex.dreaming"
```

**Schedule it (cron):**
```
0 3 * * * cd /path/to/jarv1s && ./venv/bin/python -m cortex.dreaming
```

## Non-negotiables

- **Every LLM call routes through the prompt-builder** — the three chat brain
  functions in `api.py` (`_brain_groq`, `_brain_claude`, `_brain_ollama`) all call
  `_build_system_prompt(text)`, which now delegates to `cortex.build_system_prompt`.
- **Fail-soft** — if Chroma is down, cortex falls back to an in-RAM cosine index.
  If Ollama's embed model is missing, embeddings degrade to WordHash. If the
  router has no provider, extraction and dreaming silently skip. **The reply
  still goes out** — persona + ambient alone.
- **Token-budget aware** — memory is trimmed to fit `token_cap`; never truncated
  silently. See `cortex.prompt.build`.
- **Async writes** — extraction runs after the reply is sent; the user never waits.

## Self-test

```
python scripts/selftest_memory.py
```

Runs 37 offline checks under `JARVIS_FORCE_WORDHASH=1` in a temp dir, so nothing
touches your real memory/. Verifies schema, dedup, semantic recall, prompt-builder
under budget pressure, migration idempotence, and the extract/recall/forget
round-trip. No network, no keys, no Ollama required.

## Migration

On first boot, if the cortex tables are empty and `memory/jarvis_memory.json` /
`memory/jarvis_history.json` exist, `cortex.migrate.run_if_needed()` imports
every entry as a fact / episode (categories mapped to the new closed set). The
legacy JSON files are left in place; nothing is destroyed.

## Env knobs

See `.env.example` → *Cortex* block. Highlights:
- `JARVIS_MEMORY_DB` · `JARVIS_CHROMA_DIR` — file locations
- `JARVIS_EMBED_MODEL` — Ollama embed model (default `nomic-embed-text`)
- `JARVIS_FORCE_WORDHASH=1` — offline embedder, for tests
- `JARVIS_ROUTER_EXTRACT` · `JARVIS_ROUTER_CONSOL` — per-task model overrides
