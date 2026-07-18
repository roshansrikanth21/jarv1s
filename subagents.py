"""subagents.py — parallel specialist agents fired from the main JARVIS turn.

The `spawn_agents` tool takes a list of {name, prompt} specs and runs them in
parallel. Each sub-agent gets a silent tool loop with a bounded read-only tool
set. When they all return (or the wall-clock hits), the parent gets the combined
results as one text block — no WebSocket noise, no TTS — and can reason over them.

Design (bounded on purpose so this can't become a forkbomb or a runaway bill):
  - Max 5 sub-agents per call.
  - Max 6 tool iterations per sub-agent.
  - Wall-clock cap: 90s for the whole batch.
  - Tool filter: read-only subset only. Sub-agents CANNOT invoke `spawn_agents`
    themselves (no recursion), `run_command` / `desktop` (no writes),
    `remember` (no writes to memory), etc.
  - Provider preference (Groq → Ollama) is injected via `brain_call`.

This module is pure: it takes `brain_call` and `execute_tool` as callables so it
can be tested with mocks. api.py wires the real providers in.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, cast

log = logging.getLogger("jarvis.subagents")

MAX_AGENTS = 5
MAX_ITERS_PER_AGENT = 6
WALL_CLOCK_SEC = 90
MAX_TOKENS_PER_STEP = 800
PROMPT_CAP_CHARS = 4000
NAME_CAP_CHARS = 40
OBS_CAP_CHARS = 2000

# Tool names sub-agents ARE allowed to use. Anything not in this set is stripped
# from the tools list before we hand it to the sub-agent's brain call. Additions
# should be *read-only* — anything that writes memory, files, or process state
# must be forbidden here (no `remember`, no `run_command`, no `desktop`, no
# `spawn_agents` itself).
READ_ONLY_TOOL_NAMES = frozenset({
    "search_web",
    "browse",              # browse is bounded (structured actions, no code); reads are fine
    "recall_memory",
    "get_system_info",
    "get_weather",
    "analyze_image",
    "capture_screen",
    "watch_video",
    "calculate",
    "recon",               # passive recon only
    "report",              # writes a report from memory — no external side effects
    "ict_scan",
})

_SUBAGENT_SYSTEM = (
    "You are a JARVIS sub-agent working on ONE focused question in parallel with "
    "other sub-agents. Do the work, then answer directly — no preamble, no "
    "self-reference, no headers, no markdown. If tools help, call them (you have "
    "a read-only subset). Keep the final answer under a few sentences unless the "
    "task genuinely needs more. If you can't get a good answer, say what's "
    "missing in one line."
)


BrainCall = Callable[[list[dict], list[dict], int], Awaitable[dict]]
# BrainCall contract:
#   (messages, tools, max_tokens) -> {"content": str, "tool_calls": list[dict]}
# where each tool_call is {"id": str, "name": str, "arguments": str-or-dict}.

ExecuteTool = Callable[[str, dict], Any]
# ExecuteTool contract: (name, args) -> str  (sync; wrapped via asyncio.to_thread)


def filter_tools(all_tools: list[dict]) -> list[dict]:
    """Return only the read-only subset, in the same schema api.py uses (openai-shaped)."""
    out = []
    for t in all_tools or []:
        try:
            name = t["function"]["name"]
        except (KeyError, TypeError):
            continue
        if name in READ_ONLY_TOOL_NAMES:
            out.append(t)
    return out


def _validate_specs(specs: Any) -> tuple[list[dict] | None, str | None]:
    """Coerce and cap the model-supplied specs. Returns (valid_specs, error_or_None)."""
    if not isinstance(specs, list) or not specs:
        return None, "spawn_agents: 'agents' must be a non-empty list."
    if len(specs) > MAX_AGENTS:
        return None, f"spawn_agents: at most {MAX_AGENTS} agents per call (got {len(specs)})."
    valid: list[dict] = []
    seen_names: set[str] = set()
    for i, s in enumerate(specs):
        if not isinstance(s, dict):
            return None, f"spawn_agents: agent #{i} is not an object."
        prompt = (s.get("prompt") or "").strip()
        if not prompt:
            return None, f"spawn_agents: agent #{i} is missing 'prompt'."
        raw_name = (s.get("name") or f"agent_{i+1}").strip()[:NAME_CAP_CHARS]
        # De-duplicate names so the results dict doesn't collide.
        name = raw_name
        suffix = 2
        while name in seen_names:
            name = f"{raw_name}_{suffix}"
            suffix += 1
        seen_names.add(name)
        valid.append({"name": name, "prompt": prompt[:PROMPT_CAP_CHARS]})
    return valid, None


async def _run_one(
    spec: dict,
    tools: list[dict],
    brain_call: BrainCall,
    execute_tool: ExecuteTool,
    max_iters: int = MAX_ITERS_PER_AGENT,
) -> tuple[str, str]:
    """Run one sub-agent to completion (or the iteration cap). Returns (name, result_text).
    Never raises — any exception becomes a string result so gather() sees a clean value."""
    name = spec["name"]
    messages: list[dict] = [
        {"role": "system", "content": _SUBAGENT_SYSTEM},
        {"role": "user", "content": spec["prompt"]},
    ]
    try:
        for _ in range(max_iters):
            reply = await brain_call(messages, tools, MAX_TOKENS_PER_STEP)
            content = (reply.get("content") or "").strip()
            tool_calls = reply.get("tool_calls") or []
            if not tool_calls:
                return name, content or "(sub-agent returned nothing)"
            # Append the assistant turn (with the tool_calls) so the model has the
            # right context on the next iteration.
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {"id": tc.get("id") or f"call_{i}", "type": "function",
                     "function": {"name": tc["name"], "arguments": tc.get("arguments") or "{}"}}
                    for i, tc in enumerate(tool_calls)
                ],
            })
            # Run each tool call (sync; wrap so we don't block the event loop).
            for i, tc in enumerate(tool_calls):
                raw_args = tc.get("arguments") or {}
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                try:
                    obs = await asyncio.to_thread(execute_tool, tc["name"], args)
                except Exception as exc:
                    obs = f"tool {tc['name']!r} raised: {exc}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{i}",
                    "content": str(obs)[:OBS_CAP_CHARS],
                })
        return name, f"(sub-agent {name!r} hit the {max_iters}-iteration cap without finishing)"
    except Exception as exc:
        log.info("subagent %s crashed: %s", name, exc)
        return name, f"(sub-agent {name!r} error: {exc})"


async def run_all(
    specs: Any,
    all_tools: list[dict],
    brain_call: BrainCall,
    execute_tool: ExecuteTool,
    *,
    wall_clock_sec: int = WALL_CLOCK_SEC,
) -> dict:
    """Fire the specs in parallel. Returns a dict:
        success  → {agent_name: result_text, ...}
        failure  → {"__error__": "...why..."}
    Callers should check for '__error__' first."""
    valid, err = _validate_specs(specs)
    if err or valid is None:
        return {"__error__": err or "spawn_agents: invalid agent specs."}
    specs_ok: list[dict] = valid
    tools = filter_tools(all_tools)
    tasks = [asyncio.create_task(_run_one(s, tools, brain_call, execute_tool))
             for s in specs_ok]
    try:
        pairs = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), wall_clock_sec)
    except asyncio.TimeoutError:
        for t in tasks:
            if not t.done():
                t.cancel()
        # Collect any that finished before the timeout so nothing is silently lost.
        partial: dict[str, str] = {}
        for t, s in zip(tasks, specs_ok):
            if t.done() and not t.cancelled():
                try:
                    n, r = t.result()
                    partial[n] = r
                except Exception:
                    pass
            else:
                partial[s["name"]] = f"(cancelled: hit {wall_clock_sec}s wall-clock)"
        return partial
    results: dict[str, str] = {}
    for item, s in zip(pairs, specs_ok):
        if isinstance(item, BaseException):
            results[s["name"]] = f"(sub-agent {s['name']!r} error: {item})"
            continue
        name, text = cast(tuple[str, str], item)
        results[name] = text
    return results


def format_results(results: dict) -> str:
    """Render the results dict as a text block for the parent agent to read."""
    if not results:
        return "(no sub-agent results)"
    if "__error__" in results:
        return results["__error__"]
    parts: list[str] = []
    for name, result in results.items():
        parts.append(f"[{name}]\n{str(result).strip()}")
    return "\n\n".join(parts)
