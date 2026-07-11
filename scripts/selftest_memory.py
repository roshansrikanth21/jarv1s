"""Offline self-test for the cortex memory layer.

Runs entirely without a network — forces the WordHash embedder and (if chromadb
is installed) uses an ephemeral Chroma dir under a temp path so nothing touches
the user's real memory/.

Usage:
    python scripts/selftest_memory.py

Passes with either Chroma installed (real vector index) or without (in-RAM cosine
fallback). Prints every check as it runs; exits 1 on the first failure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Isolate the test run from real data.
_TMP = Path(tempfile.mkdtemp(prefix="jarvis_cortex_test_"))
os.environ["JARVIS_MEMORY_DB"] = str(_TMP / "cortex.sqlite")
os.environ["JARVIS_CHROMA_DIR"] = str(_TMP / "chroma")
os.environ["JARVIS_FORCE_WORDHASH"] = "1"     # deterministic embedder

# Add repo root to path so `import cortex` works from either the repo or scripts/.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import cortex                                                # noqa: E402
from cortex import embeddings, extract, prompt, router, store, vectors    # noqa: E402


_checks: list[tuple[str, bool, str]] = []


def ok(label: str, cond: bool, hint: str = "") -> None:
    _checks.append((label, bool(cond), hint))
    mark = "ok " if cond else "FAIL"
    print(f"  {mark}  {label}" + (f"   -> {hint}" if not cond and hint else ""))


def section(name: str) -> None:
    print(f"{name}")


def run() -> int:
    section("cortex.store — schema + CRUD")
    cortex.init()
    s0 = cortex.stats()
    ok("boot returns stats dict", isinstance(s0, dict) and "facts" in s0)
    ok("emotion_state seeded", store.get_emotion().get("dominance") is not None)

    eid = store.add_episode("user: hi\nassistant: hi back", source="chat")
    ok("add_episode returns id", bool(eid))
    fid = store.add_fact("User prefers dark mode.", category="preference",
                         confidence=0.9, source_episode_id=eid)
    ok("add_fact returns id", bool(fid))
    got = store.get_fact(fid)
    ok("get_fact roundtrip", got and got["text"] == "User prefers dark mode.")

    # Dedup on exact-text within namespace bumps confidence, doesn't insert twice.
    fid2 = store.add_fact("User prefers dark mode.", category="preference",
                          confidence=0.7)
    ok("dedup returns same id", fid == fid2)
    facts_now = store.all_facts()
    ok("dedup did not double-insert", len(facts_now) == 1)

    section("cortex.store — prospective + emotion nudge + reinforcement")
    pid = store.add_prospective("Buy groceries Saturday",
                                due_at="2026-07-18T10:00:00+00:00")
    ok("add_prospective returns id", bool(pid))
    pending = store.pending_prospective()
    ok("pending_prospective lists it", len(pending) == 1)

    before = store.get_emotion()
    store.nudge_emotion(pleasure=0.1, attachment=0.05)
    after = store.get_emotion()
    ok("nudge_emotion moves pleasure",
       abs(after["pleasure"] - (before["pleasure"] + 0.1)) < 1e-6)
    ok("attachment axis persists",
       abs(after["attachment"] - (before["attachment"] + 0.05)) < 1e-6)

    ok("clamping caps at 1.0",
       store.set_emotion(pleasure=5.0)["pleasure"] == 1.0)
    ok("clamping floors at -1.0",
       store.set_emotion(pleasure=-5.0)["pleasure"] == -1.0)

    # reinforce_facts must bump access_count and set last_access.
    store.reinforce_facts([fid])
    row = store.get_fact(fid)
    ok("reinforce_facts bumps access_count", row["access_count"] >= 1)
    ok("reinforce_facts stamps last_access", bool(row["last_access"]))

    section("cortex.embeddings — locked backend + deterministic WordHash")
    embeddings.reset_for_tests()
    v1 = embeddings.embed("the quick brown fox")
    v2 = embeddings.embed("the quick brown fox")
    ok("embedder locked (backend name is 'wordhash')",
       embeddings.backend() == "wordhash", embeddings.backend())
    ok("embeddings are deterministic across calls", v1 == v2)
    ok("dim is 128 (wordhash)", embeddings.dim() == 128, str(embeddings.dim()))

    v3 = embeddings.embed("wholly unrelated banana train")
    def _cos(a, b):
        import math
        d = sum(x*y for x, y in zip(a, b))
        na = math.sqrt(sum(x*x for x in a)) or 1
        nb = math.sqrt(sum(y*y for y in b)) or 1
        return d/(na*nb)
    ok("similar texts have higher cos than dissimilar",
       _cos(v1, embeddings.embed("quick brown fox jumps")) > _cos(v1, v3))

    section("cortex.vectors — semantic recall over facts")
    # Populate a few facts spanning categories, so search can discriminate.
    store.add_fact("User is a data scientist at Anthropic.", category="identity", confidence=0.95)
    store.add_fact("User's dog is named Basil.", category="person", confidence=0.9)
    store.add_fact("User loves matcha lattes without sugar.", category="preference", confidence=0.9)
    # Re-index anything we bypassed via direct store calls.
    for f in store.all_facts():
        vectors.index_fact(f)

    hits_dark = vectors.search_facts("what theme should I use", k=5)
    ok("recall finds dark-mode fact",
       any("dark mode" in (h["document"] or "").lower() for h in hits_dark),
       f"got {[h['document'] for h in hits_dark]}")

    hits_dog = vectors.search_facts("what is the user's pet's name", k=5)
    ok("recall finds pet fact",
       any("basil" in (h["document"] or "").lower() for h in hits_dog))

    hits_private = vectors.search_facts("dark mode", k=5, include_private=False)
    ok("public-only filter still returns non-private facts", len(hits_private) >= 1)

    section("cortex.prompt — build_system_prompt + token cap")
    hooks = prompt.PromptHooks(
        base_prompt="You are JARVIS.",
        user_name="Jyothir",
        ambient_fragment="[Surroundings] Late night, Bangalore, clear.",
        persona_block="[Affect] mood: focused; user: neutral.",
    )
    sp = prompt.build_system_prompt("what theme do I prefer?", hooks)
    ok("prompt starts with base", sp.startswith("You are JARVIS."))
    ok("prompt contains ambient fragment", "Surroundings" in sp)
    ok("prompt contains persona block", "Affect" in sp)
    ok("prompt contains recalled dark-mode fact", "dark mode" in sp.lower())

    # Token-cap trim: force a tiny cap; episodes drop first, persona/emotion survive.
    tiny = prompt.build_system_prompt("what theme", hooks, token_cap=80)
    ok("tiny-cap trim keeps base prompt", "You are JARVIS." in tiny)
    ok("tiny-cap trim is shorter", len(tiny) < len(sp))

    section("cortex.router — parse_json robustness (no network calls)")
    parsed = router.parse_json('```json\n{"facts": [], "prospective_items": []}\n```')
    ok("parse_json strips code fences", isinstance(parsed, dict) and "facts" in parsed)
    ok("parse_json returns None on garbage", router.parse_json("no braces here") is None)

    section("cortex.extract — schedule is fire-and-forget (no LLM available)")
    # With no GROQ / Claude / Ollama in the test env, router returns "" and extraction
    # silently no-ops. We only assert the scheduler doesn't crash the loop.
    async def _drive():
        t = extract.schedule("hello", "hi", source_episode_id=eid)
        if t is not None:
            await t
    asyncio.run(_drive())
    ok("extract.schedule survived a no-LLM environment", True)

    section("cortex.migrate — idempotent guard")
    from cortex import migrate as mig
    result = mig.run_if_needed()
    ok("second-run migrate is a no-op", result.get("skipped") is True)

    section("cortex.recall + forget — public API round-trip")
    fid_new = cortex.remember("User's favourite album is In Rainbows.",
                              category="preference", confidence=0.95)
    ok("cortex.remember returns id", bool(fid_new))
    hits = cortex.recall("what music does the user like", k=5)
    ok("cortex.recall finds the new fact",
       any("In Rainbows" in (h.get("text") or "") for h in hits))
    ok("cortex.forget removes it", cortex.forget(fid_new))
    hits2 = cortex.recall("in rainbows", k=5)
    ok("recall no longer returns forgotten fact",
       not any("In Rainbows" in (h.get("text") or "") for h in hits2))

    section("cortex.dreaming — window math (no LLM call)")
    from cortex import dreaming
    start, end = dreaming._window_utc("2026-07-10")
    ok("window is a 24h span", start.startswith("2026-07-10") and end.startswith("2026-07-11"))

    # ── report ─────────────────────────────────────────────────────────────────
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
