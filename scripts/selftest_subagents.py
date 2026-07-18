"""Offline self-test for subagents.py.

Uses mocked brain + execute_tool so nothing hits a real LLM. Verifies:
  - Spec validation (empty list, missing prompt, cap at MAX_AGENTS, name de-dup)
  - Tool filter (destructive tools stripped; spawn_agents itself stripped → no forkbomb)
  - Parallel execution (all agents run; total time ≈ max, not sum)
  - Per-agent iteration cap (runaway agent gets stopped cleanly)
  - Wall-clock timeout (long agent gets cancelled; other results still returned)
  - Malformed brain replies handled without crashing
  - format_results renders the block cleanly + surfaces __error__

Usage:
    python scripts/selftest_subagents.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if callable(_reconfigure):
    try:
        _reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import subagents   # noqa: E402


_checks: list[tuple[str, bool, str]] = []


def ok(label: str, cond: bool, hint: str = "") -> None:
    _checks.append((label, bool(cond), hint))
    mark = "ok  " if cond else "FAIL"
    print(f"  {mark} {label}" + (f"   -> {hint}" if not cond and hint else ""))


def section(name: str) -> None:
    print(name)


# ── Mock harness ────────────────────────────────────────────────────────────────
def make_brain(
    script_by_agent: dict[str, list[dict]],
    call_log: list | None = None,
):
    """Returns an async brain_call that replays scripted replies per user prompt.
    Each entry in the per-agent list is a reply {"content": ..., "tool_calls": [...]}.
    """
    if call_log is None:
        call_log = []

    async def brain(messages, tools, max_tokens):
        # Identify which agent this call is for by the user prompt in the first turn.
        user_prompt = ""
        for m in messages:
            if m.get("role") == "user":
                user_prompt = m.get("content", "")
                break
        call_log.append({"prompt": user_prompt, "tools": [t["function"]["name"] for t in tools]})
        script = script_by_agent.get(user_prompt) or []
        idx = sum(1 for m in messages if m.get("role") == "assistant")
        if idx >= len(script):
            # No more scripted replies → return a no-tool-call finalizer so the loop ends.
            return {"content": "(script exhausted)", "tool_calls": []}
        return script[idx]
    return brain, call_log


def make_execute_tool(
    responses: dict | None = None,
    sleep_by_name: dict | None = None,
):
    responses = responses or {}
    sleep_by_name = sleep_by_name or {}

    def execute(name, args):
        if name in sleep_by_name:
            time.sleep(sleep_by_name[name])
        return responses.get(name, f"stub({name})")
    return execute


# All tools JARVIS exposes. We include a mix so we can prove the filter works.
FULL_TOOLS = [
    {"type": "function", "function": {"name": "search_web", "description": "", "parameters": {}}},
    {"type": "function", "function": {"name": "browse", "description": "", "parameters": {}}},
    {"type": "function", "function": {"name": "recall_memory", "description": "", "parameters": {}}},
    {"type": "function", "function": {"name": "remember", "description": "", "parameters": {}}},
    {"type": "function", "function": {"name": "run_command", "description": "", "parameters": {}}},
    {"type": "function", "function": {"name": "desktop", "description": "", "parameters": {}}},
    {"type": "function", "function": {"name": "spawn_agents", "description": "", "parameters": {}}},
    {"type": "function", "function": {"name": "get_weather", "description": "", "parameters": {}}},
]


def run() -> int:
    section("filter_tools — only read-only tools survive; spawn_agents stripped (no forkbomb)")
    filtered = subagents.filter_tools(FULL_TOOLS)
    names = {t["function"]["name"] for t in filtered}
    ok("search_web survives filter", "search_web" in names)
    ok("recall_memory survives filter", "recall_memory" in names)
    ok("remember (write) is stripped", "remember" not in names)
    ok("run_command (shell) is stripped", "run_command" not in names)
    ok("desktop (writes) is stripped", "desktop" not in names)
    ok("spawn_agents itself is stripped — no nested recursion",
       "spawn_agents" not in names)

    section("spec validation")
    async def _noop_brain(*_args):
        return {"content": "", "tool_calls": []}

    async def _val(specs):
        return await subagents.run_all(specs, [], _noop_brain, lambda *_: "")

    r = asyncio.run(_val([]))
    ok("empty list returns __error__", r.get("__error__", "").startswith("spawn_agents"))
    r = asyncio.run(_val([{"prompt": ""}]))
    ok("missing prompt returns __error__", "'prompt'" in r.get("__error__", ""))
    r = asyncio.run(_val([{"prompt": "x"}] * (subagents.MAX_AGENTS + 1)))
    ok("above MAX_AGENTS returns __error__", "at most" in r.get("__error__", ""))
    r = asyncio.run(_val("not a list"))
    ok("non-list returns __error__", "non-empty list" in r.get("__error__", ""))
    r = asyncio.run(_val([{"prompt": "hi"}, "not a dict"]))
    ok("non-object in list returns __error__", "not an object" in r.get("__error__", ""))

    section("name de-duplication")
    # When two specs share a name (or none is given), we should still get distinct keys.
    async def _dup():
        brain, _ = make_brain({"a": [{"content": "done", "tool_calls": []}],
                               "b": [{"content": "done", "tool_calls": []}]})
        return await subagents.run_all(
            [{"name": "x", "prompt": "a"}, {"name": "x", "prompt": "b"}],
            FULL_TOOLS, brain, make_execute_tool())
    r = asyncio.run(_dup())
    ok("two agents with the same name get distinct keys", len(r) == 2)

    section("basic — one agent, no tool calls, immediate reply")
    async def _basic():
        brain, log = make_brain({"weather": [{"content": "It's sunny.", "tool_calls": []}]})
        r = await subagents.run_all(
            [{"name": "weather", "prompt": "weather"}],
            FULL_TOOLS, brain, make_execute_tool())
        return r, log
    r, log = asyncio.run(_basic())
    ok("single-agent returns its content", r.get("weather") == "It's sunny.")
    ok("brain was called exactly once for a no-tool reply", len(log) == 1)
    ok("brain saw only the filtered tool set",
       "run_command" not in log[0]["tools"] and "search_web" in log[0]["tools"])

    section("tool loop — one tool call, then reply")
    async def _loop():
        brain, log = make_brain({"q": [
            {"content": "", "tool_calls": [
                {"id": "c1", "name": "search_web", "arguments": json.dumps({"query": "cats"})}]},
            {"content": "Cats are mammals.", "tool_calls": []},
        ]})
        exec_calls = []
        def exe(name, args):
            exec_calls.append((name, args))
            return "results: 1) domestic cats are mammals ..."
        r = await subagents.run_all(
            [{"name": "q", "prompt": "q"}], FULL_TOOLS, brain, exe)
        return r, log, exec_calls
    r, log, exec_calls = asyncio.run(_loop())
    ok("tool-loop agent returns the final content",
       r.get("q") == "Cats are mammals.", str(r))
    ok("brain was called twice (once tool, once final)", len(log) == 2)
    ok("execute_tool got the tool call with parsed args",
       exec_calls == [("search_web", {"query": "cats"})], str(exec_calls))

    section("iteration cap — runaway agent gets stopped cleanly")
    async def _runaway():
        # Return a tool call forever.
        forever = [{"content": "", "tool_calls":
                    [{"id": f"c{i}", "name": "search_web", "arguments": "{}"}]}
                   for i in range(20)]
        brain, log = make_brain({"forever": forever})
        r = await subagents.run_all(
            [{"name": "forever", "prompt": "forever"}], FULL_TOOLS, brain,
            make_execute_tool())
        return r, log
    r, log = asyncio.run(_runaway())
    ok("runaway agent surfaces the iteration cap message",
       "iteration cap" in r.get("forever", ""), r.get("forever", "")[:120])
    ok("brain was called at most MAX_ITERS_PER_AGENT times",
       len(log) <= subagents.MAX_ITERS_PER_AGENT, f"len(log)={len(log)}")

    section("parallelism — 3 agents that each sleep 0.4s finish in ~0.4s, not ~1.2s")
    async def _par():
        script = [{"content": "done", "tool_calls": []}]
        brain, _ = make_brain({"a": script, "b": script, "c": script})
        # Slow the brain calls themselves so the parallelism is provable.
        async def slow_brain(messages, tools, max_tokens):
            reply = await brain(messages, tools, max_tokens)
            await asyncio.sleep(0.4)
            return reply
        t0 = time.perf_counter()
        r = await subagents.run_all(
            [{"name": "a", "prompt": "a"},
             {"name": "b", "prompt": "b"},
             {"name": "c", "prompt": "c"}],
            FULL_TOOLS, slow_brain, make_execute_tool())
        elapsed = time.perf_counter() - t0
        return r, elapsed
    r, elapsed = asyncio.run(_par())
    ok("all three agents returned", set(r.keys()) == {"a", "b", "c"}, str(r))
    ok("elapsed < 0.9s (parallel), not >= 1.2s (serial)",
       elapsed < 0.9, f"elapsed={elapsed:.2f}s")

    section("wall-clock timeout — slow agent cancelled, fast one still returned")
    async def _timeout():
        async def brain(messages, tools, max_tokens):
            user = next((m["content"] for m in messages if m["role"] == "user"), "")
            if user == "slow":
                await asyncio.sleep(3.0)
                return {"content": "slow-done", "tool_calls": []}
            return {"content": "fast-done", "tool_calls": []}
        r = await subagents.run_all(
            [{"name": "slow", "prompt": "slow"},
             {"name": "fast", "prompt": "fast"}],
            FULL_TOOLS, brain, make_execute_tool(),
            wall_clock_sec=1)  # force timeout
        return r
    r = asyncio.run(_timeout())
    ok("fast agent still returned its result", r.get("fast") == "fast-done", str(r))
    ok("slow agent got a cancellation marker",
       "cancelled" in r.get("slow", "").lower() or "wall-clock" in r.get("slow", ""),
       r.get("slow", ""))

    section("brain crash — one agent's exception doesn't kill the batch")
    async def _crash():
        async def brain(messages, tools, max_tokens):
            user = next((m["content"] for m in messages if m["role"] == "user"), "")
            if user == "boom":
                raise RuntimeError("nope")
            return {"content": "ok", "tool_calls": []}
        r = await subagents.run_all(
            [{"name": "boom", "prompt": "boom"}, {"name": "good", "prompt": "hi"}],
            FULL_TOOLS, brain, make_execute_tool())
        return r
    r = asyncio.run(_crash())
    ok("crashing agent surfaces error string, no exception",
       "error" in r.get("boom", "").lower() or "nope" in r.get("boom", ""),
       r.get("boom", ""))
    ok("sibling agent still returned normally", r.get("good") == "ok")

    section("format_results — clean text block")
    txt = subagents.format_results({"one": "answer 1", "two": "answer 2"})
    ok("format includes both labeled blocks",
       "[one]\nanswer 1" in txt and "[two]\nanswer 2" in txt, txt)
    err = subagents.format_results({"__error__": "some error"})
    ok("format surfaces __error__ directly", err == "some error")
    empty = subagents.format_results({})
    ok("empty results render a hint", "no sub-agent" in empty.lower())

    passed = sum(1 for _, c, _ in _checks if c)
    total = len(_checks)
    print()
    if passed == total:
        print(f"ALL {total} CHECKS PASSED")
        return 0
    for label, cond, hint in _checks:
        if not cond:
            print(f"  FAIL: {label}   ({hint})")
    print(f"\n{passed}/{total} passed")
    return 1


if __name__ == "__main__":
    sys.exit(run())
