# JARVIS as Unified Memory Hub — Architecture

Goal: tell one model something once, and Claude / ChatGPT / Gemini all know it.
JARVIS owns the single source of truth; every model reads and writes the *same* store.

## Why not just use OpenMemory / mem0?

OpenMemory MCP is free (Apache-2.0, self-host via Docker: Postgres + Qdrant; or the
lighter `CaviraOSS/OpenMemory` local fork). But JARVIS already has the richer brain:

| Capability                         | JARVIS today            | OpenMemory |
| ---------------------------------- | ----------------------- | ---------- |
| Importance scoring                 | yes (`importance` 1-10) | partial    |
| Access-reinforced decay/forgetting | yes (`decay_and_prune`) | no         |
| Sleep-cycle consolidation          | yes (`consolidation.py`)| no         |
| Semantic (vector) recall           | **no** (substring)      | yes        |
| Cross-client access (MCP)          | **no**                  | yes        |

Decision: **do not adopt OpenMemory as the store.** It would be a second, weaker
brain fighting ours. Instead, expose the existing JARVIS store over MCP and add the
two things we lack (vector recall + provenance). Borrow OpenMemory's *ideas*
(source tagging, namespaces, "recall-first"), not its code.

## Topology

```
                       ┌──────────── Claude Desktop / Code  (native MCP)
   JARVIS memory  MCP  ├──────────── ChatGPT              (custom connector / GPT Action)
   store  ◄──────────► ├──────────── Gemini CLI           (native MCP)
   (jarvis_memory.json)└──────────── JARVIS itself        (in-process, already wired)
        ▲
        │ same file, same functions
        ▼
   consolidation "sleep" loop  +  decay  +  importance  (unchanged)
```

One store. Four front-ends. The MCP server is a thin adapter over the functions
`api.py` already calls — it does not own a second copy of the data.

## Components

### 1. Store (exists — minor schema add)
`memory/jarvis_memory.json`, list of memory dicts. Add two fields:
- `source_model`: `claude | chatgpt | gemini | jarvis` — provenance, so "who told me this".
- `namespace`: default `personal`; lets you scope (e.g. `ctf`, `work`) without leaking.

Backward compatible: readers default missing fields (`source_model="jarvis"`,
`namespace="personal"`).

### 2. Semantic recall (new — replaces substring match at api.py:678)
- Embed each memory's `content` on write; cache the vector alongside it.
- Recall = cosine top-k over embeddings, then re-rank by `importance` and recency
  (reuse the same strength formula as `decay_and_prune`), then bump `access_count`
  and `last_access` on the hits (feeds the existing decay reinforcement).
- Embedding model: **local**, via Ollama `nomic-embed-text` (keeps reflection on-device,
  matches the existing "prefer local model" principle in consolidation.py). Fallback to
  `sentence-transformers/all-MiniLM-L6-v2` if Ollama is down.
- Index: start with an in-memory numpy matrix (you have <1k memories — brute-force cosine
  is microseconds). Swap to sqlite-vec / FAISS only if it ever grows past ~50k.

### 3. MCP server (new — the bridge)
`memory_mcp.py`, built on FastMCP. Exposes exactly two tools plus one resource:
- `remember(content, category?, importance?, namespace?)` — calls the SAME write path
  as the in-process `remember` tool, tags `source_model` from the connection.
- `recall(query, k?, namespace?)` — semantic recall (component 2).
- resource `memory://recent` — last N memories, for clients that support resources.

Runs two ways (same code, chosen by launch flag):
- **stdio** — for Claude Desktop / Code and Gemini CLI (they spawn it as a subprocess).
- **HTTP/SSE** — for ChatGPT connectors and remote access, mounted on the existing
  FastAPI app at `/mcp` so it shares JARVIS's process and port.

Concurrency: the MCP server and JARVIS's own loop both touch `jarvis_memory.json`.
Guard writes with a single `asyncio.Lock` (or a file lock) and make `remember`
append-and-flush atomic. Reads are cheap and tolerate staleness.

### 4. Context-awareness layer (the "seamless" part)
Recall only helps if models *call* it. Three mechanisms, cheapest first:
- **Recall-first instruction** — each client's system prompt / custom instructions:
  "At the start of a task, call `recall` with the user's request before answering."
  (Claude: CLAUDE.md / project instructions; ChatGPT: Custom Instructions; Gemini:
  system prompt.) This is the 80% solution and costs nothing.
- **Auto-context injection** — for JARVIS's own turns, prepend top-k recalled memories
  to the prompt automatically (already partially done at api.py:1415, `memories[-20:]`);
  upgrade that slice to a semantic recall on the current user message.
- **Write-back on consolidation** — the sleep loop already distills episodic → semantic.
  Tag those with `source_model` of whichever client drove the conversation so provenance
  survives consolidation.

## Data flow (example)

1. You tell **ChatGPT**: "my CTF team is 0xdead, we play on CTFtime."
2. ChatGPT (recall-first didn't match) calls `remember("User's CTF team is 0xdead ...",
   category="personal", namespace="ctf")` → JARVIS store, `source_model="chatgpt"`.
3. Next day you ask **Claude**: "when's our next CTF?" → Claude calls
   `recall("CTF team schedule")` → semantic hit returns the 0xdead fact.
4. Overnight JARVIS's sleep loop merges duplicates and decays the stale ones. All three
   models see the cleaned-up result on their next `recall`.

## Security / privacy

- The HTTP/SSE endpoint must be auth'd (bearer token in `.env`) and bound to localhost
  unless you deliberately tunnel it — it exposes your entire personal memory.
- Namespaces are advisory, not a security boundary; don't put secrets in memory expecting
  isolation. A `security`-category memory should never be surfaced to a cloud model's
  recall unless you opt in (add a `private: true` flag that in-process JARVIS sees but the
  MCP `recall` filters out by default).

## Build phases

1. **Schema + semantic recall** (in-process only) — add fields, swap substring→vector,
   re-rank. Test with JARVIS alone. No external surface yet. *Lowest risk, immediate win.*
   **✅ DONE** — `mem_recall.py` (Ollama `nomic-embed-text`, cosine + decay re-rank,
   substring fallback); `remember`/`recall_memory` in api.py rewired; schema fields
   `source_model`/`namespace`/`private`/`importance` added. Verified: semantic queries
   with no shared keywords hit the right memories.
2. **MCP server, stdio** — wire `memory_mcp.py`, connect Claude Desktop, verify
   remember/recall round-trips. One external client.
   **✅ DONE** — hub endpoints `POST /api/memory/remember` + `GET /api/memory/recall`
   in api.py (token-auth'd via `JARVIS_MEMORY_TOKEN`, loopback-only when unset,
   private memories always filtered for external callers); `memory_mcp.py` stdio shim
   (stateless, forwards to HTTP, per-client provenance via `JARVIS_MEMORY_SOURCE`);
   registered in Claude Desktop's `claude_desktop_config.json`. Verified end-to-end:
   remember-as-chatgpt → semantic recall-as-gemini round-trip, offline guard works.
3. **HTTP/SSE mount + auth** — mount on FastAPI, add token, connect ChatGPT connector.
   **✅ DONE (local half)** — `memory_mcp.py --http [port]` serves the same tools as a
   streamable-HTTP MCP endpoint on 127.0.0.1:8765/mcp, bearer-token-guarded
   (`JARVIS_MEMORY_TOKEN`, generated into `.env`; refuses to start without it).
   Verified: 401 without token, full MCP initialize handshake with it.
   Remaining (user action): tunnel it (`cloudflared tunnel --url http://127.0.0.1:8765`)
   and add the https URL + `/mcp` as a ChatGPT custom connector with the token.
4. **Gemini CLI + recall-first instructions** on all three clients.
   **✅ Gemini registered** — `jarvis-memory` added to `~/.gemini/settings.json`
   (`JARVIS_MEMORY_SOURCE=gemini`). Recall-first instructions still to be added per
   client (CLAUDE.md / ChatGPT custom instructions / GEMINI.md).
5. **Provenance through consolidation + `private` filtering.**

Phase 1 stands alone and makes JARVIS better even if you stop there.
```
