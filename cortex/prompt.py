"""cortex.prompt — the read path.

Every LLM chat call in JARVIS routes its system message through here. The message
is assembled in a fixed order (persona → emotion → memory), then trimmed to a token
budget with a strict priority list: episodes drop first, then lower-importance facts,
then higher-importance facts. Persona + emotion are never trimmed — they're what
keep JARVIS *him*.

The public entry point is `build_system_prompt(user_prompt, hooks)`.  `hooks` carries
the run-time bits `api.py` already computes (user name, ambient fragment, persona
style block, homeostasis line, overheard text) so this module stays a leaf and
never imports api.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from . import emotion as emo_mod
from . import store, vectors

DEFAULT_TOKEN_CAP = 2000
CHARS_PER_TOKEN = 4  # heuristic — good enough for budget-and-trim

FACT_K = 8
EPISODE_K = 3


def _tok(text: str) -> int:
    return max(1, len(text or "") // CHARS_PER_TOKEN)


@dataclass
class PromptHooks:
    """Everything the caller assembles per-turn and hands to the builder."""
    base_prompt: str                            # the persona/system base text
    user_name: str = "the user"
    ambient_fragment: str = ""                  # ambient.prompt_fragment() text
    persona_block: str = ""                     # persona.style_block(...)
    homeostasis_line: str = ""                  # low-energy nudge line, if any
    overheard: list[dict] = field(default_factory=list)
    namespace: str = "personal"
    include_private: bool = True                # in-process JARVIS sees private; the HTTP hub does not


def _facts_block(user_name: str, facts: list[dict]) -> str:
    if not facts:
        return ""
    lines = [f"What you know about {user_name} (most relevant first):"]
    for f in facts:
        cat = f.get("category", "fact")
        conf = float(f.get("confidence", 0.7))
        marker = "•" if conf >= 0.8 else "◦"
        lines.append(f"  {marker} [{cat}] {f['text']}")
    return "\n".join(lines)


def _episodes_block(episodes: list[dict]) -> str:
    if not episodes:
        return ""
    lines = ["Recent context (things said before, most relevant first):"]
    for ep in episodes:
        ts = (ep.get("timestamp") or "")[:16].replace("T", " ")
        snip = (ep.get("raw_text") or "").strip().replace("\n", " ")
        if len(snip) > 260:
            snip = snip[:260] + "…"
        lines.append(f"  – ({ts}) {snip}")
    return "\n".join(lines)


def _overheard_block(overheard: list[dict]) -> str:
    if not overheard:
        return ""
    lines = ["Recently overheard nearby (ambient — relate to it only if relevant):"]
    for o in overheard[-8:]:
        lines.append(f"  - {o.get('text', '')}")
    return "\n".join(lines)


def retrieve(query: str, hooks: PromptHooks,
             fact_k: int = FACT_K, episode_k: int = EPISODE_K) -> tuple[list[dict], list[dict]]:
    """Do the two vector searches and hydrate facts from SQLite (source of truth)."""
    query = (query or "").strip()
    if not query:
        return [], []
    fact_hits = vectors.search_facts(query, k=fact_k, namespace=hooks.namespace,
                                     include_private=hooks.include_private)
    fact_ids = [h["id"] for h in fact_hits]
    facts = store.get_facts(fact_ids) if fact_ids else []
    if fact_ids:
        store.reinforce_facts(fact_ids)

    ep_hits = vectors.search_episodes(query, k=episode_k, include_dreaming=True)
    ep_ids = [h["id"] for h in ep_hits]
    episodes = []
    if ep_ids:
        # Fetch by-id: newest chronological is not what we want — order = relevance.
        marks = ",".join("?" * len(ep_ids))
        with store.connect() as conn:
            rows = conn.execute(
                f"SELECT id, timestamp, raw_text, source FROM episodes "
                f"WHERE id IN ({marks})", tuple(ep_ids),
            ).fetchall()
        by_id = {r["id"]: dict(r) for r in rows}
        episodes = [by_id[i] for i in ep_ids if i in by_id]

    return facts, episodes


def build(user_prompt: str, hooks: PromptHooks,
          token_cap: int = DEFAULT_TOKEN_CAP,
          retrieve_fn: Optional[Callable] = None) -> str:
    """Assemble the system message. Trims memory to fit the cap; persona/emotion float.

    Order in the final prompt:
        1. base_prompt (persona core)
        2. ambient fragment
        3. persona style block (live mood + how to read user this turn)
        4. homeostasis / low-energy nudge
        5. emotion fragment (attachment axis; persona covers the other three)
        6. facts block (semantic recall)
        7. episodes block (semantic recall)
        8. overheard block
    """
    parts: list[str] = [hooks.base_prompt.strip()]
    if hooks.ambient_fragment:
        parts.append(hooks.ambient_fragment.strip())
    if hooks.persona_block:
        parts.append(hooks.persona_block.strip())
    if hooks.homeostasis_line:
        parts.append(hooks.homeostasis_line.strip())

    emo_line = emo_mod.fragment()
    if emo_line:
        parts.append("[Emotion]\n" + emo_line)

    retrieve_fn = retrieve_fn or retrieve
    facts, episodes = retrieve_fn(user_prompt, hooks)

    fixed = "\n\n".join(parts).strip()
    fixed_tokens = _tok(fixed)
    # Fixed sections (persona/ambient/emotion) are non-negotiable; memory competes for
    # whatever budget is left. Under a tiny cap that goes to zero — persona survives,
    # memory drops out. Under a normal cap (~2000) there's plenty for both.
    remaining = max(0, token_cap - fixed_tokens)

    # Priority list: episodes cheapest, then trim facts from lowest confidence/importance.
    episodes_block = _episodes_block(episodes)
    while episodes and _tok(episodes_block) + _tok(_facts_block(hooks.user_name, facts)) > remaining:
        episodes.pop()
        episodes_block = _episodes_block(episodes)

    if facts:
        facts_sorted = sorted(
            facts,
            key=lambda f: (float(f.get("confidence", 0.5)) + 0.1 * int(f.get("importance", 5))),
            reverse=True,
        )
        while _tok(_facts_block(hooks.user_name, facts_sorted)) + _tok(episodes_block) > remaining:
            if not facts_sorted:
                break
            facts_sorted.pop()
        facts = facts_sorted

    facts_block = _facts_block(hooks.user_name, facts)
    over_block = _overheard_block(hooks.overheard)

    final_parts = [fixed]
    for block in (facts_block, episodes_block, over_block):
        if block:
            final_parts.append(block)
    return "\n\n".join(final_parts).strip()


def build_system_prompt(user_prompt: str, hooks: PromptHooks,
                        token_cap: int = DEFAULT_TOKEN_CAP) -> str:
    """Public entry point. See `build`."""
    return build(user_prompt, hooks, token_cap=token_cap)
