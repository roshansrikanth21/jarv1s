# Jarv1s Architecture V2 — AI Operating System Kernel

> **Status:** Engineering contract. Phases 0–5 kernel foundations are in-tree under `jarvis/` (dialogue, router plan+health, policies, events, session, telemetry). Full brain/tool extraction and voice rewrite remain strangler follow-ons.  
> **Grounded in:** `api.py`, `cortex/`, `governor.py`, `skills.py`, `electron/main.js`, `routers/rest.py` (Jul 2026).  
> **Companion canvas:** `jarv1s-ai-os-redesign.canvas.tsx` (executive map).  
> **Local audit:** `python .local/audit_runtime.py` (`.local/` is gitignored).

---

## 0. Challenge the framing

**You do not need eighteen parallel redesigns on day one.** You need a *kernel boundary* and a strangler migration that kills the three truths that currently lie about each other:

1. **Memory truth** — Cortex SQLite/Chroma *and* legacy `jarvis_memory.json` / `jarvis_history.json` / in-memory `memories` / `_history` still participate in the live path.
2. **Routing truth** — Governor LinUCB rungs, keyword difficulty, and a separate regex tool-intent layer all decide what the model sees.
3. **Capability truth** — Tools live as a giant `TOOLS` list + `execute_tool` in `api.py`; skills are a thin progressive-disclosure layer; approval is mostly `run_command`.

[Certain] The LLM is not the bottleneck. The monolith + dual writers + heuristic gating are.

Preserve what already points the right direction: Cortex’s past/present/future metaphor, Governor’s energy-aware lattice, PromptHooks leaf pattern, skills progressive disclosure, Electron+venv portability fixes. **Replace** everything that forces those good ideas to share process memory with a 5k-line god module.

---

## 1. Target kernel (one picture)

```
┌─────────────────────────────────────────────────────────────┐
│  Experience     decks · voice UX · notifications · proactive │
├─────────────────────────────────────────────────────────────┤
│  Orchestration  SessionState · Planner · EventBus · Policy   │
├─────────────────────────────────────────────────────────────┤
│  Cognition      PromptBuilder · Router · Providers · I/O     │
├─────────────────────────────────────────────────────────────┤
│  Memory Fabric  working · episodic · semantic · procedural   │
│                 preferences · project · task · scratchpad    │
├─────────────────────────────────────────────────────────────┤
│  Capabilities   tools · skills · plugins · sandbox           │
├─────────────────────────────────────────────────────────────┤
│  Platform       sqlite · vectors · cache · secrets · otel    │
└─────────────────────────────────────────────────────────────┘
```

**Design rules (non-negotiable)**

1. Explicit `SessionState` — never rely on the LLM to remember.
2. One memory authority — no dual-write.
3. `PromptBuilder` modules — no ad-hoc string paste past the builder.
4. Provider-agnostic `Router` — Groq/Claude/Ollama/OpenAI/… as adapters.
5. Tools as packages: schema, permissions, health, cache keys.
6. Event bus between voice, planner, memory, UI.
7. Policy engine: allowlist > denylist for shell/desktop/browse.
8. Every turn emits cost, latency, tokens, tool spans.

---

## 2. Proposed folder structure

```
jarvis/
  kernel/
    session/          # SessionState, turn machine
    events/           # EventBus + typed events
    policy/           # permissions, approval, sandbox
    planner/          # goal → graph → execute → verify
  cognition/
    prompt/           # PromptBuilder modules + budget
    router/           # Provider registry, health, fallback
    providers/        # groq, anthropic, openai, ollama, openrouter
  memory/
    fabric/           # stores, ranking, consolidation
    retrieval/        # hybrid search, scorers
    encryption/       # at-rest for private namespaces
  capabilities/
    tools/            # one package per tool family
    skills/           # SKILL.md runtime (existing, hardened)
    plugins/          # hot-reload SDK
  voice/
    wake/ vad/ stt/ tts/ bargein/
  platform/
    db/               # schemas, migrations, backups
    cache/            # l1 ram / l2 disk layers
    telemetry/        # metrics, traces, cost ledger
    secrets/          # keyring / DPAPI, never .env in logs
  adapters/
    fastapi/          # thin HTTP/WS surface (today’s api.py shrinks here)
    electron/         # process supervision only
```

Strangler rule: `api.py` becomes a façade that imports kernel packages; decks keep talking WebSocket.

---

# Subsystem redesigns

Each P0 section follows the 18-point contract. P1/P2 sections are condensed but complete enough to implement from.

---

## A. Memory Fabric (P0)

### 1. Current Problem
Cortex is the right *shape* (episodes, facts, prospective, emotion) but the process still maintains `_history`, `memories`, overheard buffers, and legacy JSON writers/readers. Consolidation (“sleep”) and extraction race with live turns. Token budget uses `len//4`.

### 2. Why It Happens
Migration was additive (migrate.py + dual paths) instead of cutover. `api.py` owned conversation history before Cortex existed and never fully surrendered it.

### 3. Industry Best Practice
Single source of truth + derived indexes. Working memory in RAM; durable episodic/semantic in DB; embeddings as *index only*; importance × recency × relevance scoring; scheduled consolidation offline.

### 4. Better Architecture
**Memory Fabric** with typed stores and one `MemoryService` façade:

| Store | Lifetime | Write path | Read path |
|-------|----------|------------|-----------|
| Working | turn | SessionState | PromptBuilder |
| Short-term dialogue | session | append turns | last-N + summary |
| Conversation summary | rolling | after N turns / sleep | PromptBuilder |
| Episodic | durable | record_turn | vector+time |
| Semantic facts | durable | extract + remember | hybrid recall |
| Procedural | durable | tool/skill outcomes | “how we do X” |
| Preferences | durable | explicit + extract | always high priority |
| Project / Task | scoped | planner + tools | namespace filter |
| Scratchpad / Reflection | ephemeral→durable | planner | active goal only |
| Tool memory | durable | tool results hashed | cache + few-shot |

**Interactions:** extractors only write semantic/procedural; planner owns task/project; PromptBuilder never reads raw JSON files.

### 5. Implementation Strategy
1. Freeze legacy JSON as import-only (already mostly true in migrate).
2. Move `_history` into `memory.dialogue` table + SessionState cache.
3. Delete dual writes to `jarvis_memory.json` after one release of read-compat.
4. Add ranker: `score = α·sim + β·recency + γ·importance + δ·namespace_match`.
5. Real tokenizer budget (tiktoken / provider tokenizer adapter).

### 6. Folder Structure
`jarvis/memory/fabric/{stores,rankers,consolidate,expire}.py`

### 7. Data Flow
`turn end → record_episode → enqueue extract → reinforce accessed facts → optional summarize → dream consolidate`

### 8. Sequence Diagram
```
User → Session → MemoryService.recall(query, budget)
MemoryService → Vectors.search + SQL hydrate → Ranker → top-K
Session → PromptBuilder.inject(memories)
Turn end → MemoryService.commit(episode) → Extractor(async)
```

### 9. Trade-offs
Strong consistency vs write latency: async extract (keep). Losing legacy JSON means one-way migration (acceptable).

### 10. Performance Impact
Fewer disk writes; predictable prompt size; parallel recall. Cold start: defer vector warm (already done) + lazy embed model.

### 11. Security Impact
Namespace + `private` flag + optional SQLCipher/DPAPI for `memory/*.db`. Hub mode must keep `include_private=False`.

### 12. Scalability Impact
SQLite fine for single-user desktop. Schema ready for later sync (CRDT or LWW per fact id).

### 13. Future Compatibility
Personal knowledge graph = facts + relations table; multi-device sync = export stream of fact/episode events.

### 14. Example Code
```python
@dataclass
class RecallQuery:
    text: str
    namespace: str | None
    project_id: str | None
    budget_tokens: int
    kinds: frozenset[str]  # {"semantic","episodic","pref","procedural"}

class MemoryService:
    def recall(self, q: RecallQuery) -> list[MemoryHit]:
        hits = self.hybrid.search(q)
        ranked = self.ranker.score(hits, q)
        return self.budget.trim(ranked, q.budget_tokens)

    def commit_turn(self, user: str, assistant: str, *, meta: TurnMeta) -> str:
        eid = self.episodic.add(user, assistant, meta)
        self.bus.emit(MemoryUpdated(eid))
        self.extract.schedule(eid)
        return eid
```

### 15. Migration Plan
Phase 1: dialogue table mirrors `_history`. Phase 2: switch readers. Phase 3: stop writing JSON. Phase 4: delete loaders.

### 16. Testing Strategy
Golden recalls; dual-read parity tests during migration; injection tests (prompt must not include forgotten facts); budget never exceeds cap.

### 17. Potential Risks
Extraction hallucinations writing bad facts — keep confidence + user confirm for identity/preference. Chroma/WordHash drift — SQL remains authoritative.

### 18. Beyond This
Conflict resolution UI; reflection memory after failed tools; encryption of overheard ambient buffer.

---

## B. Prompt Builder (P0)

### 1. Current Problem
`cortex.prompt.build_system_prompt` is a real builder, but `api.py` still concatenates skills ads, tool policy text, and large `_BASE_PROMPT` outside a unified budget. Char/4 token heuristic under/over estimates.

### 2. Why It Happens
PromptHooks were designed as a leaf to avoid circular imports; everything else stayed in the monolith.

### 3. Industry Best Practice
Modular sections with priority + hard token budgets; assembler chooses *relevant* modules; structured sections with clear delimiters; never dump full tool catalogs when intent is chat-only.

### 4. Better Architecture
```
PromptBuilder
  ├── SystemCoreModule        # identity, safety, style
  ├── GoalModule              # SessionState.current_goal
  ├── TaskModule              # active task/subtask
  ├── SummaryModule           # conversation summary
  ├── MemoryModule            # MemoryService.recall
  ├── RecentDialogueModule    # last N turns (compressed)
  ├── ToolResultModule        # this-turn tool outputs
  ├── KnowledgeModule         # RAG / browse excerpts
  ├── ConstraintsModule       # policy + user prefs
  ├── ScratchpadModule        # planner scratch
  └── DynamicInstrModule      # skill body when use_skill fired
```
Each module: `estimate_tokens()`, `render(ctx) -> Section | None`, `priority: int`.

### 5. Implementation Strategy
Promote cortex.prompt → cognition.prompt; move `_BASE_PROMPT` into SystemCore; skills advertisement becomes a low-priority module; tool schemas injected only when Router says `tools_enabled`.

### 6–8. Structure / Flow / Sequence
`SessionTurn → Router.decision → PromptBuilder.build(ctx, decision) → messages[]`

### 9. Trade-offs
More abstraction vs one string — pay it once; debugging needs a `prompt.dump` debug endpoint (redacted).

### 10–13. Impacts
Lower tokens → cost/latency win; less prompt injection surface; sections versionable for A/B.

### 14. Example Code
```python
class PromptBuilder:
    def build(self, ctx: TurnContext, decision: RouteDecision) -> list[Message]:
        sections: list[Section] = []
        budget = decision.context_budget
        for mod in sorted(self.modules, key=lambda m: m.priority):
            if not mod.applies(ctx, decision):
                continue
            sec = mod.render(ctx)
            if not sec:
                continue
            cost = sec.tokens
            if cost > budget and not mod.mandatory:
                continue
            sections.append(sec)
            if not mod.mandatory:
                budget -= cost
        return assemble(sections)
```

### 15–17. Migration / Tests / Risks
Shadow-build: log old vs new prompt hashes offline. Risk: persona tone regressions — keep mandatory persona+emotion untrimmed (already the rule).

### 18. Beyond
Prompt cache keys per provider; section-level analytics (which modules correlate with tool success).

---

## C. LLM Router (P0)

### 1. Current Problem
Governor is strong (LinUCB + device energy) but incomplete as a *provider* layer: no health probes, no cost ledger, no capability matching (vision / tools / JSON), OpenAI/OpenRouter not first-class, cortex.router is a second mini-router for extract/dream tasks.

### 2. Why It Happens
Governor chooses *rungs*; api.py hard-binds rung → `_brain_groq` / `_brain_claude` / `_brain_ollama`. Cortex grew its own callers for background jobs.

### 3. Industry Best Practice
Adapter pattern + circuit breaker + fallback chain + capability tags + timeout/retry with jitter + structured output mode negotiated per provider.

### 4. Better Architecture
```
Router
  ├── Registry(providers)
  ├── HealthMonitor (latency EMA, error rate)
  ├── CapabilityIndex (tools, vision, json, streaming, ctx_window)
  ├── CostModel (USD/1k in/out per model)
  ├── Governor (keep: difficulty × energy × LinUCB)
  └── Executor (timeouts, retries, fallbacks, stream)
```
Unify cortex background calls through the same Router with `task_type` priority lanes (interactive vs batch).

### 5. Implementation Strategy
Extract brains from api.py into `providers/*`. Governor returns `RouteDecision{rung, model, tools_enabled, max_tokens, timeout_ms}`. Executor runs fallbacks: `cloud_fast → local_fast` on 429/5xx.

### 9. Trade-offs
Extra hop vs direct SDK — negligible vs network. Bandit cold-start needs priors (already have).

### 10–13. Impacts
p95 latency via health routing; cost via cheaper rungs; offline via local-only mode; future models = new adapter file.

### 14. Example Code
```python
@dataclass
class RouteDecision:
    provider: str
    model: str
    tools_enabled: bool
    max_tokens: int
    timeout_ms: int
    fallbacks: list[tuple[str, str]]

async def complete(req: CompletionRequest) -> CompletionResult:
    decision = governor.decide(req) | capability.refine(req)
    for provider, model in [(decision.provider, decision.model), *decision.fallbacks]:
        try:
            return await registry[provider].complete(req.with_model(model), decision)
        except (Timeout, RateLimit, ProviderDown):
            health.mark_fail(provider)
            continue
    raise AllProvidersFailed()
```

### 15–18. Migration / Tests / Beyond
Parity tests: same prompt → same rung distribution offline. Chaos: kill Groq → must land Ollama if available. Beyond: speculative draft on local_fast while cloud_deep streams.

---

## D. Tool System + Policy (P0)

### 1. Current Problem
Monolithic `TOOLS` + `execute_tool`. `_relevant_tools` regex hides schemas (TPM workaround that also hides capability). Approval skewed to shell. Browse/desktop/pentest each invent their own safety checks.

### 2. Why It Happens
Tools grew organically inside the WebSocket handler for speed of hackathon shipping.

### 3. Industry Best Practice
Tool packages with JSON Schema, permission scopes, rate limits, result caching, health checks; central policy engine decides allow/deny/ask; MCP-compatible shapes for future.

### 4. Better Architecture
```
ToolPackage
  metadata, permissions[], input_schema, examples
  validate() → execute() → normalize()
  cache_key(), rate_limit(), health()
PolicyEngine.evaluate(tool, args, session) → Allow | Deny | AskUser
ToolRuntime.run → events ToolCalled / ToolCompleted / ToolFailed
```

### 5. Implementation Strategy
One directory per family (`browse`, `desktop`, `recon`, `shell`, `memory`, …). Keep OpenAI-compatible schema export for providers. Replace regex subset with: Governor `tools_enabled` + Policy + optional *semantic* tool retrieval (embed tool descriptions, top-M).

### 9. Trade-offs
More files vs one giant match — required for sandboxing and marketplace.

### 11. Security Impact
Allowlist for shell; SSRF centralization; pentest scope as first-class Policy predicate; no silent Origin-less WS trust for mutating tools (pair with session token).

### 14. Example Code
```python
@tool(name="run_command", permissions=["shell.exec"], risk="high")
async def run_command(args: RunCommandArgs, ctx: ToolContext) -> ToolResult:
    decision = ctx.policy.evaluate("run_command", args, ctx.session)
    if decision is AskUser:
        ok = await ctx.session.request_approval(args)
        if not ok:
            return ToolResult.denied()
    return await ctx.sandbox.shell(args.cmd, timeout=args.timeout)
```

### 15–18. Migration / Tests / Beyond
Move tools one family at a time behind `execute_tool` façade. Contract tests per schema. Beyond: plugin tools with capability negotiation.

---

## E. Task Planner (P0/P1)

### 1. Current Problem
Agent loop is “model ↔ tools” without an explicit plan graph. `spawn_agents` is parallelism without durable checkpoints. Cancellation via generation counter is clever but not a planner.

### 2. Why It Happens
Voice UX optimized for single-turn tool use; multi-step lives only in the LLM’s working context.

### 3. Industry Best Practice
Separate plan from act: Goal → DAG → execute with verify → reflect → replan; checkpoint after each node; cancel/timeout per node.

### 4. Better Architecture
`Planner` produces `TaskGraph`; `Executor` runs ready nodes (parallel where independent); `Verifier` checks artifacts; failures → `Replanner`. SessionState holds `current_goal`, `graph`, `node_status`.

### 5–18. Condensed plan
MVP: only for multi-tool / multi-step intents (difficulty > threshold). Persist graphs in SQLite. Events: `TaskCreated`, `TaskCompleted`. Tests: diamond DAG parallel, cancel mid-flight, resume after crash. Risk: over-planning greetings — gate hard. Beyond: multi-agent with shared blackboard.

---

## F. Application State + Event Bus (P1)

### 1. Current Problem
Dozens of globals in `api.py` (`_history`, `_listening`, `_tts_playing`, `_current_task`, …). Tight coupling between voice, brain, and UI push.

### 2. Better Architecture
```python
@dataclass
class SessionState:
    session_id: str
    current_goal: str | None
    current_project: str | None
    current_task: str | None
    subtask: str | None
    conversation: DialogueWindow
    tool_state: dict
    workflow: WorkflowPhase  # idle|listening|thinking|acting|speaking
    pending_approvals: list[Approval]
    interrupted: InterruptedAction | None
    queue: deque[QueuedTask]
```
**EventBus** (in-process, typed): `WakeWord`, `SpeechStarted/Ended`, `ToolCalled/Completed`, `TaskCreated/Completed`, `MemoryUpdated`, `LLMResponded`, `PluginLoaded`, `NotificationCreated`.

Voice writes events; Planner/Router subscribe; decks subscribe to projection events only.

### Trade-offs / Migration
Replace globals gradually with a single `AppContext` injected into handlers. Risk: event storms — coalesce UI updates (you already do render coalescing on the client).

---

## G. Voice Pipeline (P1)

### Current → Target
| Stage | Today | Target |
|-------|-------|--------|
| Wake | keywords / always-listen | configurable; push-to-talk profile |
| VAD | local | keep + calibrate per device |
| STT | faster-whisper tiny / Groq | streaming partials → early intent |
| TTS | Edge | provider adapters + offline voice |
| Barge-in | generation counters | first-class InterruptController |
| Echo | text guard + mute window | AEC + playback clock sync |

Latency budget: wake→first token &lt; 800ms on cloud_fast path (partial STT + speculative route).

---

## H. Persistence / Database (P1)

**Separate concerns (one desktop, multiple DBs or schemas):**

| Domain | Store |
|--------|-------|
| Settings / profiles | `settings.db` or JSON+schema |
| Memories | `cortex.db` (evolve schema) |
| Conversations | `dialogue` tables |
| Embeddings | Chroma / sqlite-vec |
| Caches | `cache/` disk + LRU RAM |
| Tasks / graphs | `tasks.db` |
| Logs / analytics | append-only + retention |
| Plugins | manifest + sandboxed data dirs |

Migrations: Alembic or custom versioned SQL (Cortex already has migrate pattern). Backups: file copy of WAL checkpoint on idle. Recovery: import event log.

---

## I. Caching (P1)

Layers: embedding cache (text hash → vector), tool cache (idempotent GETs), ICT cache (exists), response cache (only for pure Q&A with explicit TTL), prompt-prefix cache where provider supports it. Invalidation: memory write busts related recall cache; tool cache keyed by args+policy version.

---

## J. Observability (P1)

Emit per turn: `trace_id`, provider, model, tokens in/out, USD estimate, tool spans, memory hit counts, prompt module sizes, governor rung, device energy. Sink: structured JSON logs + optional OTLP. Dashboard later; **first** ship `/debug/metrics` locally (auth-bound).

---

## K. Security (P1)

| Area | Action |
|------|--------|
| Secrets | OS keyring; never log; rotate any key pasted in chat |
| Memory | private flag + encryption option |
| WS | require desktop session token; tighten Origin-less |
| Shell | allowlist + approval + sandbox working dir |
| Browse | central SSRF allow/deny |
| Plugins | permission manifest; no raw network by default |
| Prompt injection | tool results wrapped as untrusted data sections |

---

## L. Plugin System (P2)

Hot-reload watch on `plugins/*/plugin.json`. DI: receive `MemoryService`, `Bus`, `Router` handles — never import `api`. Version field + capability discovery. Marketplace readiness = signed manifests later; isolation = subprocess or RestrictedPython for untrusted.

Skills (`skills/*/SKILL.md`) stay as *instruction packs*; plugins are *code*.

---

## M. UX / Performance (P1)

- Streaming tokens + tool progress events (partially present).
- Cold start: already deferring cortex warm, whisper unload, governor numpy — continue extracting imports.
- Perceived latency: acknowledgements on slow tools (filler path exists — bind to ToolRuntime).
- Proactive: bus-driven, rate-limited, respect homeostasis.
- Cross-device: out of scope until Memory Fabric event export exists.

---

## N. Future features (compatibility map)

| Future | Depends on |
|--------|------------|
| Multi-agent | Planner + shared Memory namespaces + Policy |
| Cloud sync | Memory event log + encryption |
| Phone companion | thin client + same Router API |
| Smart home | Plugin permissions `iot.*` |
| Vision | Router capability + tool `see` |
| Offline AI | local rungs mandatory path |
| Self-improving memory | consolidate + user feedback loop |
| PKGs | facts + edges table |
| AI coding | project memory + sandboxed shell |

---

# 3. Migration roadmap (strangler)

| Phase | Weeks | Outcome | Exit criteria |
|-------|-------|---------|---------------|
| 0 Kernel extract | 2–3 | Packages exist; api.py re-exports | All tests green; no behavior change |
| 1 Memory Fabric | 2–3 | Single writer; PromptBuilder budget | JSON write disabled; recall parity |
| 2 Router + Tools | 2–4 | Adapters + policy + tool packages | Fallback chaos test passes |
| 3 State + Events | 1–2 | SessionState + bus; planner MVP | No new globals; barge-in via InterruptController |
| 4 Voice + Perf | 2–3 | Streaming STT; cache layers | Wake→token budget met on reference machine |
| 5 Observe + Secure | 2 | OTel/cost; sandbox; session token | Threat model checklist signed off |

**Do not** rewrite decks first. Decks are clients of the kernel.

---

# 4. What we keep vs kill

| Keep | Kill / replace |
|------|----------------|
| Cortex metaphor + SQL authority | Dual-write to legacy JSON |
| Governor lattice + LinUCB | Regex as sole tool gate |
| PromptHooks leaf idea | Char/4 as only tokenizer |
| Skills progressive disclosure | Giant unscoped tool dumps every turn |
| Electron venv + JARVIS_PORT discipline | Silent port remap in desktop mode |
| Generation-based barge-in idea | Ad-hoc globals as architecture |

---

# 5. First implementation slice (if you say go)

The highest leverage *sequence* is not “boil the ocean”:

1. **Phase 0:** extract `tools/`, `brains/` → packages behind stable façades.  
2. **Phase 1a:** dialogue store replaces `_history` dual path.  
3. **Phase 1b:** unify PromptBuilder budget (real tokenizer).  
4. **Phase 2a:** PolicyEngine for shell/desktop/browse.  
5. **Phase 2b:** Router adapters + health/fallback.

Anything else before that is theater.

---

*Document owner: architecture proposal for Jarv1s kernel. Update when Phase 0 lands.*
