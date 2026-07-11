"""cortex.extract — the post-turn extraction pipeline.

After every user↔assistant exchange, one router call (task_type="extraction") reads
the turn and returns three tracks:

  facts             — durable knowledge → cortex.store.add_fact
  prospective_items — deadlines/reminders/intents → cortex.store.add_prospective
  emotion_signals   — a small PAD+attachment delta → cortex.store.nudge_emotion

All fire-and-forget: `schedule` returns immediately. If the model call fails or JSON
is malformed, extraction is silently skipped — the reply already went out.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from . import router, store, vectors

log = logging.getLogger("jarvis.cortex")

_EXTRACT_PROMPT = """You are the memory-extractor for a personal AI assistant.

You will read ONE user↔assistant exchange and return JSON identifying:
  1. any DURABLE FACTS about the user (preferences, situations, people they know, identity, skills)
  2. any PROSPECTIVE items (deadlines, reminders, recurring intents, "remind me to X")
  3. an EMOTION SIGNAL for the assistant — how the user's tone/topic should nudge its mood

RECENT EXCHANGE:
  user:      {user_text}
  assistant: {assistant_text}

Rules:
- Facts must be standalone third-person sentences ("User has …" / "User prefers …" / "User's …")
- categories: preference | situation | person | identity | skill  — pick the best fit
- confidence 0.0-1.0: how sure you are this is durable and true. Chit-chat and one-offs → skip.
- prospective due_at is ISO-8601 UTC if a date/time was said; otherwise null
- emotion_signals: each of pleasure/arousal/dominance/attachment in [-0.2, 0.2]. Zero if uncertain.
- Return NOTHING except JSON. Use empty arrays / zeros when nothing applies.

Return exactly:
{{"facts": [{{"text": "...", "category": "...", "confidence": 0.0}}],
  "prospective_items": [{{"description": "...", "due_at": "...|null", "recurrence": "...|null"}}],
  "emotion_signals": {{"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0, "attachment": 0.0}}}}"""


async def _extract_once(user_text: str, assistant_text: str,
                        source_episode_id: Optional[str],
                        namespace: str) -> dict:
    prompt = _EXTRACT_PROMPT.format(
        user_text=(user_text or "")[:1000],
        assistant_text=(assistant_text or "")[:1000],
    )
    raw = await router.route("extraction", prompt, json_mode=True)
    parsed = router.parse_json(raw) or {}
    summary = {"facts": 0, "prospective": 0, "emotion": False}

    for f in parsed.get("facts") or []:
        text = (f.get("text") or "").strip()
        if not text:
            continue
        cat = (f.get("category") or "preference").strip().lower()
        conf = float(f.get("confidence") or 0.5)
        if conf < 0.4:
            continue
        fid = store.add_fact(text, category=cat, confidence=conf,
                             source_episode_id=source_episode_id,
                             namespace=namespace, source_model="jarvis")
        if fid:
            vectors.index_fact(store.get_fact(fid) or {})
            summary["facts"] += 1

    for p in parsed.get("prospective_items") or []:
        desc = (p.get("description") or "").strip()
        if not desc:
            continue
        pid = store.add_prospective(
            desc, due_at=p.get("due_at") or None,
            recurrence=p.get("recurrence") or None,
            source_episode_id=source_episode_id,
        )
        if pid:
            summary["prospective"] += 1

    sig = parsed.get("emotion_signals") or {}
    if any(sig.get(k) for k in ("pleasure", "arousal", "dominance", "attachment")):
        clamp = lambda x: max(-0.2, min(0.2, float(x or 0)))
        store.nudge_emotion(
            pleasure=clamp(sig.get("pleasure")),
            arousal=clamp(sig.get("arousal")),
            dominance=clamp(sig.get("dominance")),
            attachment=clamp(sig.get("attachment")),
        )
        summary["emotion"] = True

    return summary


def schedule(user_text: str, assistant_text: str, *,
             source_episode_id: Optional[str] = None,
             namespace: str = "personal") -> asyncio.Task | None:
    """Fire-and-forget wrapper. Returns the task (mostly for tests); safe to ignore."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    async def _run():
        try:
            await _extract_once(user_text, assistant_text,
                                source_episode_id, namespace)
        except Exception as exc:
            log.info("cortex.extract: skipped (%s)", exc)

    return loop.create_task(_run())


async def run_sync(user_text: str, assistant_text: str, *,
                   source_episode_id: Optional[str] = None,
                   namespace: str = "personal") -> dict:
    """Synchronous variant (for CLI / self-test) — awaited, so callers see the summary."""
    try:
        return await _extract_once(user_text, assistant_text,
                                   source_episode_id, namespace)
    except Exception as exc:
        log.info("cortex.extract: skipped (%s)", exc)
        return {"facts": 0, "prospective": 0, "emotion": False}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
