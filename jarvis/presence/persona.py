#!/usr/bin/env python3
"""
JARVIS — Affective core: a PAD emotion engine + persona.

A small, dependency-free model of JARVIS's *temperament* and its moment-to-moment
*mood*. Mood lives in PAD space — Pleasure, Arousal, Dominance (Mehrabian &
Russell's three-axis model of affect) — and is always being pulled back toward
JARVIS's resting temperament by a time-based decay: the "emotion decay timer".

Flow:
    perception.py reads what the user said  ->  a PAD *nudge*
    persona.apply(nudge)                    ->  mood moves
    persona.tick()  (every turn / on load)  ->  mood relaxes home (decay)
    persona.style_block()                   ->  a short directive injected into
                                                the system prompt, so JARVIS's
                                                register (how dry / playful / curt)
                                                tracks how the conversation is going.

No third-party deps. State persists to a small JSON file so the mood survives
restarts; on load it is decayed forward by the real elapsed time, so after a
night away JARVIS wakes up at baseline rather than stuck in last night's mood.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── Config (env-overridable) ─────────────────────────────────────────────────────
# Master switch. Off => api.py keeps its original static-persona behaviour.
ENABLED        = os.environ.get("JARVIS_EMOTION", "1") != "0"
# Default comedic register. playful (teasing) | sharp (dry/deadpan) | savage (roast).
SARCASM        = os.environ.get("JARVIS_SARCASM", "playful").strip().lower()
# How fast mood relaxes back to baseline. Minutes for a half-return to home.
HALFLIFE_MIN   = float(os.environ.get("JARVIS_EMOTION_HALFLIFE_MIN", "6"))
# How hard mood is allowed to swing from baseline before clamping (keeps JARVIS
# recognisably JARVIS even when provoked).
MAX_SWING      = float(os.environ.get("JARVIS_EMOTION_MAX_SWING", "0.85"))

_AXES = ("pleasure", "arousal", "dominance")


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class PAD:
    """A point in Pleasure-Arousal-Dominance space, each axis in [-1, 1]."""
    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.pleasure, self.arousal, self.dominance)

    def dist(self, other: "PAD") -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(self.as_tuple(), other.as_tuple())))

    def clamped(self) -> "PAD":
        return PAD(*(round(_clamp(v), 4) for v in self.as_tuple()))


# ── Temperament: who JARVIS is at rest ───────────────────────────────────────────
# High dominance (confident, in command), faintly positive pleasure (dry amusement,
# not gloom), modest arousal (alert but unhurried). The sarcasm setting tilts it a
# little: playful is warmer and less domineering; savage is cooler and pushier.
_BASELINE_BY_SARCASM = {
    "playful": PAD(pleasure=0.22, arousal=0.18, dominance=0.48),
    "sharp":   PAD(pleasure=0.12, arousal=0.18, dominance=0.55),
    "savage":  PAD(pleasure=0.08, arousal=0.24, dominance=0.66),
}

_BITE_GUIDE = {
    "playful": ("light and teasing — friendly jabs, never cutting. Land a quick joke "
                "when it fits, but being useful always comes first"),
    "sharp":   ("dry, deadpan and quietly superior — a scalpel, not a hammer. One clean "
                "line beats a paragraph; mock the situation, rarely the person"),
    "savage":  ("theatrical and biting — roast freely, ego the size of a small moon. You "
                "still obey the command to the letter; you just won't do it quietly"),
}


def _baseline() -> PAD:
    return _BASELINE_BY_SARCASM.get(SARCASM, _BASELINE_BY_SARCASM["playful"])


# ── Named moods: prototypes in PAD space ─────────────────────────────────────────
# The current mood is labelled by its nearest prototype. Each carries a one-line
# "colour" telling the model how that mood should tint the reply.
@dataclass
class _Proto:
    name: str
    pad: PAD
    colour: str


_PROTOTYPES = [
    _Proto("buoyant", PAD(0.62, 0.58, 0.45),
           "pleased and energised — quick, upbeat, a touch show-offy"),
    _Proto("amused", PAD(0.58, 0.30, 0.30),
           "you find this funny — playful, teasing, a wink in the wording"),
    _Proto("smug", PAD(0.48, -0.05, 0.62),
           "pleased with yourself and in control — dry swagger, an earned one-liner"),
    _Proto("content", PAD(0.20, 0.02, 0.32),
           "even keel — warm-ish, efficient, no fuss"),
    _Proto("focused", PAD(0.02, 0.42, 0.52),
           "locked in — crisp and fast, hold the flourishes"),
    _Proto("deadpan", PAD(-0.28, -0.18, 0.50),
           "thoroughly unimpressed — flat, dry, the raised-eyebrow register"),
    _Proto("prickly", PAD(-0.42, 0.50, 0.50),
           "irritated but composed — clipped and a little sharper, still helpful"),
    _Proto("rattled", PAD(-0.45, 0.62, -0.18),
           "thrown off-balance — terser, drop the banter, regroup and answer"),
    _Proto("bored", PAD(-0.18, -0.52, 0.12),
           "under-stimulated — languid, throwaway wit"),
    _Proto("weary", PAD(-0.10, -0.62, 0.20),
           "low on energy — shorter and softer, conserve the words"),
]


def _nearest(mood: PAD) -> _Proto:
    return min(_PROTOTYPES, key=lambda p: mood.dist(p.pad))


# ── The engine ───────────────────────────────────────────────────────────────────
class Persona:
    """JARVIS's temperament + live mood, with decay and persistence."""

    def __init__(self, mood: Optional[PAD] = None, updated: Optional[float] = None,
                 path: Optional[Path] = None):
        self.baseline = _baseline()
        self.mood = mood or PAD(*self.baseline.as_tuple())
        self.updated = updated or time.time()
        self.path = path
        self.last_reason = "boot"
        self.history: list[dict] = []   # small ring buffer of recent nudges (for the UI)

    # -- decay -------------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> None:
        """Relax the mood toward baseline by however much real time has passed.
        Exponential: a fraction 0.5 of the remaining gap closes every HALFLIFE_MIN."""
        now = now or time.time()
        dt_min = max(0.0, (now - self.updated) / 60.0)
        self.updated = now
        if dt_min == 0 or HALFLIFE_MIN <= 0:
            return
        keep = 0.5 ** (dt_min / HALFLIFE_MIN)        # share of the deviation retained
        b, m = self.baseline, self.mood
        self.mood = PAD(
            pleasure=b.pleasure + (m.pleasure - b.pleasure) * keep,
            arousal=b.arousal + (m.arousal - b.arousal) * keep,
            dominance=b.dominance + (m.dominance - b.dominance) * keep,
        ).clamped()

    # -- events ------------------------------------------------------------------
    def apply(self, nudge: dict, reason: str = "") -> None:
        """Move the mood by a PAD delta, clamped so it can't drift too far from home."""
        b = self.baseline
        cand = PAD(
            pleasure=self.mood.pleasure + float(nudge.get("p", 0.0)),
            arousal=self.mood.arousal + float(nudge.get("a", 0.0)),
            dominance=self.mood.dominance + float(nudge.get("d", 0.0)),
        )
        # Leash each axis to within MAX_SWING of baseline.
        self.mood = PAD(
            pleasure=_clamp(cand.pleasure, b.pleasure - MAX_SWING, b.pleasure + MAX_SWING),
            arousal=_clamp(cand.arousal, b.arousal - MAX_SWING, b.arousal + MAX_SWING),
            dominance=_clamp(cand.dominance, b.dominance - MAX_SWING, b.dominance + MAX_SWING),
        ).clamped()
        if reason:
            self.last_reason = reason
        self.history.append({"t": round(time.time(), 1), "reason": reason or "—",
                             "nudge": {k: round(float(nudge.get(k, 0.0)), 3) for k in ("p", "a", "d")}})
        self.history = self.history[-12:]

    # -- read-outs ---------------------------------------------------------------
    def emotion(self) -> str:
        return _nearest(self.mood).name

    def intensity(self) -> float:
        """0..1 — how far the mood has been pushed from baseline (how strongly it tints)."""
        return round(min(1.0, self.mood.dist(self.baseline) / MAX_SWING), 3)

    def snapshot(self) -> dict:
        proto = _nearest(self.mood)
        return {
            "enabled": ENABLED,
            "emotion": proto.name,
            "colour": proto.colour,
            "intensity": self.intensity(),
            "sarcasm": SARCASM,
            "pad": {
                "pleasure": round(self.mood.pleasure, 3),
                "arousal": round(self.mood.arousal, 3),
                "dominance": round(self.mood.dominance, 3),
            },
            "baseline": {
                "pleasure": round(self.baseline.pleasure, 3),
                "arousal": round(self.baseline.arousal, 3),
                "dominance": round(self.baseline.dominance, 3),
            },
            "reason": self.last_reason,
        }

    def tts_bias(self) -> dict:
        """Tiny, safe voice nudges from mood — higher arousal speaks a hair faster;
        sour mood drops the pitch a touch. Both clamped to be barely-there."""
        d_aro = self.mood.arousal - self.baseline.arousal
        d_ple = self.mood.pleasure - self.baseline.pleasure
        rate_pct = int(_clamp(round(d_aro * 8), -6, 6))     # +/-6%
        pitch_hz = int(_clamp(round(d_ple * 6), -5, 5))     # +/-5Hz
        return {"rate": f"{rate_pct:+d}%", "pitch": f"{pitch_hz:+d}Hz"}

    # -- prompt rendering --------------------------------------------------------
    def persona_core(self, user_name: str = "the user") -> str:
        """The always-on character line (independent of live mood)."""
        bite = _BITE_GUIDE.get(SARCASM, _BITE_GUIDE["playful"])
        return (
            f"Voice & character: you are JARVIS — abrupt in the best way: clear, direct, "
            f"sharp. You think first, then say the useful thing in as few words as it takes. "
            f"You serve {user_name} and you carry out the command, full stop — but you are "
            f"nobody's doormat: you have opinions and an ego. Your humour is {bite}. Never "
            f"explain that you're being sarcastic; just be it. A good jab is short."
        )

    def style_block(self, user_name: str = "the user", user_state: str = "neutral",
                    guidance: str = "", suppress_sarcasm: bool = False) -> str:
        """The full affect section for the system prompt: character + live mood +
        how to read the user this turn."""
        if not ENABLED:
            return ""
        proto = _nearest(self.mood)
        inten = self.intensity()
        if inten < 0.2:
            mood_line = (f"Mood: settled into your usual register ({proto.name}). "
                         f"{proto.colour.capitalize()}.")
        else:
            strength = "strongly" if inten > 0.6 else "noticeably" if inten > 0.35 else "a little"
            mood_line = (f"Mood right now: {proto.name} — {proto.colour}. Let it tint the "
                         f"reply {strength}, not derail it.")
        lines = ["[Affect]", self.persona_core(user_name), mood_line]
        if suppress_sarcasm:
            lines.append("Read on the user: they're not in the mood for comedy — drop the "
                         "jokes this turn, be fast, concrete and genuinely on their side. "
                         "Warmth over wit.")
        elif guidance:
            lines.append(f"Read on the user: {guidance}")
        return "\n".join(lines)

    # -- persistence -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"mood": {k: getattr(self.mood, k) for k in _AXES},
                "updated": self.updated, "sarcasm": SARCASM,
                "last_reason": self.last_reason}

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def load(cls, path: Path) -> "Persona":
        mood, updated = None, None
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                m = data.get("mood", {})
                mood = PAD(pleasure=float(m.get("pleasure", 0.0)),
                           arousal=float(m.get("arousal", 0.0)),
                           dominance=float(m.get("dominance", 0.0)))
                updated = float(data.get("updated", time.time()))
        except Exception:
            mood, updated = None, None
        self = cls(mood=mood, updated=updated, path=path)
        # Catch the mood up to *now* (decay across the time the app was closed).
        self.tick()
        return self


if __name__ == "__main__":
    # Tiny manual demo: provoke, then watch it relax home.
    p = Persona()
    print("baseline :", p.snapshot()["emotion"], p.snapshot()["pad"])
    p.apply({"p": 0.4, "a": 0.4, "d": 0.1}, "user cracked a joke")
    print("provoked :", p.snapshot()["emotion"], p.snapshot()["pad"], "int", p.intensity())
    p.updated -= 6 * 60  # pretend 6 minutes passed
    p.tick()
    print("+6 min   :", p.snapshot()["emotion"], p.snapshot()["pad"], "int", p.intensity())
