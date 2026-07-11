"""cortex.router — one entry point for every LLM call the cortex makes.

Chat turns still flow through JARVIS's Governor + brain layer (that path already uses
cortex.build_system_prompt). This router is for the *cortex-internal* calls:

  task_type = "extraction"     — small/fast, JSON output, tight ceiling
  task_type = "consolidation"  — long context; prefers the biggest available brain
  task_type = "reflection"     — short generative summary, mood-writing, fallbacks

Each task picks a provider by what's actually available (GROQ_API_KEY / ANTHROPIC_API_KEY
/ local Ollama), so a machine with just Groq keeps working, and a fully-offline machine
with Ollama also keeps working. Nothing hard-fails; on a total miss we return "".
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger("jarvis.cortex")

GROQ_MODEL_EXTRACT = os.environ.get("JARVIS_ROUTER_EXTRACT",
                                    os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"))
GROQ_MODEL_CONSOL = os.environ.get("JARVIS_ROUTER_CONSOL",
                                   os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
LOCAL_FAST = os.environ.get("JARVIS_LOCAL_FAST", "qwen2.5:7b")
LOCAL_DEEP = os.environ.get("JARVIS_LOCAL_DEEP", "")

_TASK_DEFAULTS = {
    "extraction":    {"max_tokens": 700,  "prefer": "fast"},
    "consolidation": {"max_tokens": 1500, "prefer": "deep"},
    "reflection":    {"max_tokens": 500,  "prefer": "fast"},
}


def _has_groq() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


def _has_claude() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _ollama_up() -> bool:
    try:
        import ollama
        ollama.list()
        return True
    except Exception:
        return False


async def _call_groq(model: str, prompt: str, max_tokens: int,
                     json_mode: bool) -> str:
    try:
        import openai
    except Exception:
        return ""
    try:
        client = openai.AsyncOpenAI(api_key=os.environ["GROQ_API_KEY"],
                                    base_url="https://api.groq.com/openai/v1")
        kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        r = await client.chat.completions.create(**kwargs)
        return (r.choices[0].message.content or "").strip()
    except Exception as exc:
        log.info("cortex.router: groq %s failed (%s)", model, exc)
        return ""


async def _call_claude(model: str, prompt: str, max_tokens: int) -> str:
    try:
        import anthropic
    except Exception:
        return ""
    try:
        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        r = await client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            b.text for b in r.content
            if getattr(b, "type", "") == "text"
        ).strip()
    except Exception as exc:
        log.info("cortex.router: claude %s failed (%s)", model, exc)
        return ""


async def _call_ollama(model: str, prompt: str, max_tokens: int,
                       json_mode: bool) -> str:
    if not model:
        return ""
    try:
        import ollama
    except Exception:
        return ""
    try:
        client = ollama.AsyncClient()
        opts = {"num_predict": max_tokens}
        kwargs: dict = {"model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "options": opts}
        if json_mode:
            kwargs["format"] = "json"
        r = await client.chat(**kwargs)
        return (r.message.content or "").strip()
    except Exception as exc:
        log.info("cortex.router: ollama %s failed (%s)", model, exc)
        return ""


async def route(task_type: str, prompt: str, *, json_mode: bool = False,
                max_tokens: Optional[int] = None) -> str:
    """Run one LLM call for a cortex task. Never raises; returns "" on total miss."""
    cfg = _TASK_DEFAULTS.get(task_type, _TASK_DEFAULTS["reflection"])
    mt = max_tokens or cfg["max_tokens"]
    prefer = cfg["prefer"]

    # Order tuned per task_type:
    if task_type == "consolidation":
        # long-ctx first: Groq gpt-oss-120b > Claude > local deep
        candidates = [
            ("groq", GROQ_MODEL_CONSOL) if _has_groq() else None,
            ("claude", CLAUDE_MODEL) if _has_claude() else None,
            ("ollama", LOCAL_DEEP or LOCAL_FAST) if _ollama_up() else None,
        ]
    else:
        # fast-first: local fast > groq small > claude haiku
        candidates = [
            ("ollama", LOCAL_FAST) if _ollama_up() else None,
            ("groq", GROQ_MODEL_EXTRACT) if _has_groq() else None,
            ("claude", CLAUDE_MODEL) if _has_claude() else None,
        ]
    candidates = [c for c in candidates if c]
    if not candidates:
        log.info("cortex.router: no provider available for %s", task_type)
        return ""

    for provider, model in candidates:
        if provider == "groq":
            out = await _call_groq(model, prompt, mt, json_mode)
        elif provider == "claude":
            out = await _call_claude(model, prompt, mt)
        else:
            out = await _call_ollama(model, prompt, mt, json_mode)
        if out:
            return out
    return ""


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(text: str) -> dict | None:
    """Lenient JSON extractor — pulls the first {...} block out of a mixed reply."""
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        try:
            return json.loads(text)
        except Exception:
            return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None
