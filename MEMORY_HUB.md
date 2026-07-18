# JARVIS as Unified Memory Hub — Architecture

Goal: tell one model something once, and Claude / ChatGPT / Gemini all know it.
JARVIS owns the single source of truth; every model reads and writes the *same* store.

> **Status (beta):** Cortex (`cortex/`) is the authoritative store today — SQLite facts /
> episodes / prospective + vector recall (Chroma or WordHash fallback). Optional Mem0
> mirroring and an MCP adapter (`memory_mcp.py`) extend reach across clients. This doc
> describes the hub architecture; see [`CORTEX.md`](CORTEX.md) for the cognitive layer.

## Why not just use OpenMemory / mem0 as the primary store?

OpenMemory MCP is free (Apache-2.0, self-host via Docker: Postgres + Qdrant; or the
lighter `CaviraOSS/OpenMemory` local fork). But JARVIS already has the richer brain:

| Capability                         | JARVIS today                         | OpenMemory |
| ---------------------------------- | ------------------------------------ | ---------- |
| Importance scoring                 | yes (`importance` 1–10)              | partial    |
| Access-reinforced decay/forgetting | yes (access counts + dreaming)       | no         |
| Sleep-cycle consolidation          | yes (`cortex.dreaming`)              | no         |
| Semantic (vector) recall           | yes (`cortex.vectors` + embeddings)  | yes        |
| Cross-client access (MCP)          | yes (`memory_mcp.py`, beta)          | yes        |
| Optional cloud mirror              | yes (`MEM0_API_KEY` → `sync_mem0`)   | n/a        |

Decision: **do not adopt OpenMemory as the store.** It would be a second brain fighting
ours. Instead, expose cortex over MCP and optionally mirror durable facts to Mem0.
Borrow OpenMemory's *ideas* (source tagging, namespaces, "recall-first"), not its schema.

## Topology

```
                       ┌──────────── Claude Desktop / Code  (native MCP)
   cortex store  MCP   ├──────────── ChatGPT              (custom connector / GPT Action)
   (SQLite+vectors)◄──►├──────────── Gemini CLI           (native MCP)
        ▲              └──────────── JARVIS itself        (in-process)
        │
        │ optional writethrough / dreaming batch
        ▼
   Mem0 cloud mirror (gated on MEM0_API_KEY)
```

One authoritative store (cortex). MCP is a thin adapter — it does not own a second copy.
Legacy `memory/jarvis_memory.json` remains a compatibility mirror for older UI counts.

## Components

1. **`cortex/`** — SQLite + vectors + extraction + dreaming (see `CORTEX.md`).
2. **`memory_mcp.py`** — MCP server surface over remember / recall / forget / stats.
3. **`cortex/sync_mem0.py`** — optional push/restore when Mem0 is configured.
4. **Electron Settings** — can store `MEM0_API_KEY` via OS `safeStorage`.

## Operational notes

- Private facts are visible to JARVIS in-process; external hub callers must not receive them
  unless explicitly authorized (HTTP hub filters private).
- Dreaming / sleep still runs from `api.py`'s idle consolidation loop — it writes cortex,
  not a separate consolidation module.
- This hub is **beta**: expect schema and tool names to evolve with cortex.

## Related docs

- [`CORTEX.md`](CORTEX.md) — cognitive memory design
- [`README.md`](README.md) — product overview
- [`.env.example`](.env.example) — `MEM0_*` / cortex path overrides
