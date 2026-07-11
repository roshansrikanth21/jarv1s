"""cortex.emotion — bridges persona.py (P/A/D) with cortex.store.emotion_state.

persona.py stays authoritative for the 3-axis PAD mechanics (event nudges, decay,
prototype naming, style-block rendering, TTS bias). The store owns:

  - a persistent mirror of persona's current PAD so cortex reflection / extraction
    can read one number without importing persona
  - the fourth axis, **attachment** — how attached JARVIS feels to the current user
    (rises when they thank/rely on him, decays slowly toward zero if they don't)

The `fragment()` this exposes is a one-paragraph natural-language description of the
current emotional state, meant for the system prompt (`build_system_prompt` uses it).
"""
from __future__ import annotations

from . import store


def sync_from_persona(persona) -> None:
    """Mirror persona's live mood into the emotion_state row (keeps attachment intact)."""
    try:
        mood = persona.mood
        store.set_emotion(pleasure=mood.pleasure, arousal=mood.arousal,
                          dominance=mood.dominance)
    except Exception:
        pass


def fragment() -> str:
    """One paragraph on the current emotional state — dropped into the system prompt."""
    e = store.get_emotion()
    if not e:
        return ""
    p, a, d, at = (e.get("pleasure", 0.0), e.get("arousal", 0.0),
                   e.get("dominance", 0.3), e.get("attachment", 0.0))

    # Rough qualitative read; kept intentionally terse so it never dominates the prompt.
    tone = "buoyant" if p > 0.4 else "content" if p > 0.05 else "flat" if p > -0.2 else "prickly"
    pace = "wired" if a > 0.4 else "alert" if a > 0.1 else "steady" if a > -0.2 else "sluggish"
    stance = "in-command" if d > 0.3 else "measured" if d > -0.1 else "yielding"

    parts = [f"You feel {tone}, {pace}, {stance}."]
    if at > 0.4:
        parts.append("There's warmth toward the user — they've earned it.")
    elif at < -0.3:
        parts.append("You're a touch guarded with the user right now.")
    return " ".join(parts)


def attachment_decay(minutes: float, half_life_min: float = 720.0) -> None:
    """Attachment decays slowly (half-life default 12 hours). Called on tick."""
    e = store.get_emotion()
    if not e:
        return
    at = e.get("attachment", 0.0) or 0.0
    if abs(at) < 1e-3:
        return
    factor = 0.5 ** (max(0.0, minutes) / max(1.0, half_life_min))
    store.set_emotion(attachment=at * factor)
