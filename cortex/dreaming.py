"""cortex.dreaming — the nightly consolidation job.

Cron / Task Scheduler runs `python -m cortex.dreaming` at ~3 AM. This module:
  1. Pulls yesterday's raw episodes (source='chat', summary IS NULL).
  2. Sends the batch to router(task_type='consolidation') — long-ctx by preference.
  3. Model returns { summary, facts, prospective_items }.
  4. Insert one new episode source='dreaming-summary' holding the narrative.
  5. Mark raw turns as consolidated (their `summary` field points at the new one).
  6. Insert newly-noticed facts and prospective, back-linked to the summary episode.

Old raw turns stay in the DB, still searchable, but the daily summary is what
usually surfaces during retrieval — the day compresses cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import router, store, vectors

log = logging.getLogger("jarvis.cortex.dreaming")

_CONSOL_PROMPT = """You are the memory consolidator for a personal AI assistant, running
overnight while the user sleeps. You will be given every raw user↔assistant turn from
today. Write a compact narrative summary of the day, and identify durable facts and
prospective items that should carry forward.

TODAY'S RAW TURNS ({n_turns} total, oldest first):
{turns}

Rules:
- The `summary` is one short paragraph (2-4 sentences) in past tense: what the user did,
  what mattered, any emotional through-line. It should read like a memory, not a log.
- Facts: standalone third-person sentences ("User …"), only DURABLE knowledge worth
  keeping (identity, preferences, ongoing projects, relationships, decisions). Skip
  chit-chat, greetings, transient states.
  categories: preference | situation | person | identity | skill
  confidence 0.0-1.0 (higher = more sure).
- Prospective: reminders/deadlines/recurring intents the user mentioned. due_at is
  ISO-8601 UTC if a specific time was said, else null.
- Return NOTHING except JSON. Empty arrays are fine.

Return exactly:
{{"summary": "...",
  "facts": [{{"text": "...", "category": "...", "confidence": 0.0}}],
  "prospective_items": [{{"description": "...", "due_at": "...|null", "recurrence": "...|null"}}]}}"""


def _window_utc(day: Optional[str] = None) -> tuple[str, str]:
    """Return (start_iso, end_iso) for the day to consolidate. `day` = YYYY-MM-DD in UTC.
    Default = yesterday UTC, so a 3 AM cron picks up the day that just ended."""
    if day:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        d = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


async def run_once(day: Optional[str] = None, min_turns: int = 4) -> dict:
    """One consolidation cycle. Returns a summary dict; never raises."""
    start, end = _window_utc(day)
    raw = store.episodes_between(start, end, source="chat")
    # Only unsummarized turns (dreaming already ran → summary field set).
    raw = [ep for ep in raw if not ep.get("summary")]
    if len(raw) < min_turns:
        return {"status": "skipped", "reason": f"only {len(raw)} unsummarized turn(s)",
                "window": [start, end]}

    turns_text = "\n".join(
        f"- ({ep['timestamp'][11:16]}) {ep['raw_text'][:400]}" for ep in raw
    )
    prompt = _CONSOL_PROMPT.format(n_turns=len(raw), turns=turns_text)
    reply = await router.route("consolidation", prompt, json_mode=True)
    parsed = router.parse_json(reply) or {}

    summary_text = (parsed.get("summary") or "").strip()
    if not summary_text:
        return {"status": "empty", "window": [start, end], "raw_turns": len(raw)}

    # Persist the dreaming summary as a new episode; back-link raw turns; add facts + prospective.
    summary_id = store.add_episode(summary_text, source="dreaming-summary")
    store.mark_episodes_consolidated([ep["id"] for ep in raw], summary_id)
    ep_obj = {"id": summary_id, "raw_text": summary_text,
              "source": "dreaming-summary", "timestamp": store.utcnow()}
    vectors.index_episode(ep_obj)

    added_facts, added_prospective = 0, 0
    for f in parsed.get("facts") or []:
        text = (f.get("text") or "").strip()
        if not text:
            continue
        cat = (f.get("category") or "preference").strip().lower()
        conf = float(f.get("confidence") or 0.6)
        fid = store.add_fact(text, category=cat, confidence=conf,
                             source_episode_id=summary_id,
                             namespace="personal", source_model="jarvis")
        if fid:
            vectors.index_fact(store.get_fact(fid) or {})
            added_facts += 1

    for p in parsed.get("prospective_items") or []:
        desc = (p.get("description") or "").strip()
        if not desc:
            continue
        pid = store.add_prospective(
            desc, due_at=p.get("due_at") or None,
            recurrence=p.get("recurrence") or None,
            source_episode_id=summary_id,
        )
        if pid:
            added_prospective += 1

    return {
        "status": "ok",
        "window": [start, end],
        "raw_turns": len(raw),
        "summary": summary_text,
        "facts_added": added_facts,
        "prospective_added": added_prospective,
        "summary_episode_id": summary_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="JARVIS nightly consolidation.")
    parser.add_argument("--day", help="YYYY-MM-DD (UTC) to consolidate. Default: yesterday.")
    parser.add_argument("--min-turns", type=int, default=4,
                        help="Skip if fewer unsummarized turns than this.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = asyncio.run(run_once(day=args.day, min_turns=args.min_turns))
    log.info("dreaming: %s", result)


if __name__ == "__main__":
    main()
