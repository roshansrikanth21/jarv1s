#!/usr/bin/env python3
"""
JARVIS — Perceptual intelligence (read the human, not just the words).

The STT stage (Groq Whisper) gives us *what* was said. This stage estimates *how*
it was said and what the user is feeling/wanting, from cheap, transparent text
signals (and an optional loudness hint carried over from the mic). It is the seam
between hearing and feeling: its output is a PAD *nudge* for persona.py plus a
one-line read on the user that the system prompt can act on.

The single most important job here is the safety valve: when the user is genuinely
frustrated, stressed, or low, we flag `suppress_sarcasm` so JARVIS drops the comedy
and just helps. Sarcasm is for good moods, never for someone having a bad time.

Pure-Python, no deps, no network. Designed to be fast and forgiving — it never
raises into the request path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ── Lexicons ─────────────────────────────────────────────────────────────────────
_POSITIVE = {
    "good", "great", "nice", "awesome", "amazing", "love", "loved", "excellent",
    "perfect", "thanks", "thank", "thankyou", "appreciate", "appreciated", "brilliant",
    "cool", "legend", "genius", "beautiful", "wonderful", "yay", "glad", "happy",
    "fantastic", "superb", "clean", "slick", "nailed", "works", "working", "fixed",
    "lifesaver", "goat", "based",
}
_NEGATIVE = {
    "bad", "terrible", "awful", "hate", "worst", "broken", "useless", "stupid",
    "dumb", "wrong", "fail", "failed", "failing", "error", "annoying", "ugh",
    "crap", "sucks", "suck", "garbage", "trash", "idiot", "nonsense", "ridiculous",
    "frustrated", "frustrating", "angry", "mad", "pathetic", "lazy", "slow",
    "rubbish", "pointless", "messed", "borked",
}
# Frustration: things aren't working / repetition / exasperation.
_FRUSTRATION = [
    "again", "still not", "still doesn't", "still won't", "doesn't work", "does not work",
    "not working", "won't work", "wont work", "didn't work", "didnt work", "come on",
    "seriously", "for the third time", "how many times", "i already", "i told you",
    "that's wrong", "thats wrong", "not what i", "try again", "keeps", "keep failing",
    "why won't", "why wont", "why isn't", "why isnt", "this is broken",
]
_URGENCY = [
    "asap", "urgent", "urgently", "hurry", "quick", "quickly", "right now", "immediately",
    "deadline", "emergency", "need this now", "no time", "fast", "right away",
]
_DOWN = [
    "depressed", "depression", "exhausted", "burnt out", "burned out", "overwhelmed",
    "rough day", "terrible day", "bad day", "stressed out", "so stressed", "anxious",
    "can't sleep", "cant sleep", "lonely", "miserable", "hopeless", "i'm sad", "im sad",
    "feeling down", "feel awful", "wiped out", "drained", "no energy", "burning out",
]
_PLAYFUL = [
    "lol", "lmao", "lmfao", "rofl", "haha", "haha", "hehe", "heh", "jk", "just kidding",
    "kidding", "gotcha", "roast me", "no cap", "fr fr", "lowkey", "ngl", "bruh",
    "you wish", "smartass", "smart ass", "cheeky",
]
_PLAYFUL_EMOJI = ("😂", "🤣", "😄", "😅", "😆", "😏", "😜", "😝", "😬", "🙃", "😉", ";)", ":)", ":p", ":d", "xd")
_PROFANITY = {"damn", "hell", "crap", "wtf", "fuck", "fucking", "shit", "bullshit",
              "ass", "bastard", "bloody", "freaking", "frickin", "goddamn"}
# Direct shots at JARVIS.
_INSULT_PHRASES = [
    "shut up", "be quiet", "you're useless", "youre useless", "you are useless",
    "you're stupid", "youre stupid", "you suck", "you're dumb", "youre dumb",
    "hate you", "you're an idiot", "youre an idiot", "you idiot", "you're wrong",
    "stop talking", "you're terrible", "you're useless", "worst assistant",
]
_GRATITUDE = ("thank", "thanks", "cheers", "appreciate", "ty ", "tysm", "well done",
              "good job", "nice work", "good work", "you're the best", "youre the best",
              "lifesaver", "good boy", "good lad")
_COMMAND_VERBS = (
    "open", "run", "launch", "search", "scan", "show", "find", "play", "close", "kill",
    "start", "stop", "set", "make", "create", "write", "build", "fix", "check", "list",
    "remember", "delete", "remove", "add", "send", "pull", "push", "deploy", "calculate",
    "convert", "translate", "summarize", "summarise", "capture", "screenshot", "analyze",
    "analyse", "watch", "read", "tell me", "give me", "get me", "look up",
)

_GREETINGS = {
    "good morning": (4, 11, "morning"),
    "mornin": (4, 11, "morning"),
    "good afternoon": (12, 16, "afternoon"),
    "good evening": (17, 21, "evening"),
    "good night": (21, 4, "night"),
    "goodnight": (21, 4, "night"),
    "night night": (21, 4, "night"),
}
_TOKEN_RE = re.compile(r"[a-z']+")


@dataclass
class Read:
    """A read on the user this turn."""
    user_state: str = "neutral"          # frustrated|stressed|down|irritated|grateful|playful|excited|mismatch|commanding|curious|neutral
    valence: float = 0.0                 # -1..1  (negative..positive sentiment)
    arousal: float = 0.0                 # 0..1   (calm..fired-up)
    politeness: float = 0.0              # -1..1  (rude..polite)
    flags: list = field(default_factory=list)
    pad_nudge: dict = field(default_factory=lambda: {"p": 0.0, "a": 0.0, "d": 0.0})
    guidance: str = ""                   # one-line steer for the system prompt
    suppress_sarcasm: bool = False       # True => no jokes this turn

    def summary(self) -> dict:
        return {"user_state": self.user_state, "valence": round(self.valence, 2),
                "arousal": round(self.arousal, 2), "politeness": round(self.politeness, 2),
                "flags": self.flags, "suppress_sarcasm": self.suppress_sarcasm,
                "guidance": self.guidance}


def _greeting_mismatch(text: str, hour: Optional[int]):
    """Return (phrase, expected_label, actual_label) if the user greeted with the
    wrong time of day, else None. Hour is the user's *local* hour (0-23)."""
    if hour is None:
        return None
    low = text.lower()
    for phrase, (lo, hi, label) in _GREETINGS.items():
        if phrase not in low:
            continue
        # Window may wrap past midnight (e.g. night = 21..04).
        if lo <= hi:
            ok = lo <= hour <= hi
        else:
            ok = hour >= lo or hour <= hi
        if not ok:
            return (phrase, label, _tod_label(hour))
    return None


def _tod_label(hour: int) -> str:
    if hour < 5:   return "the middle of the night"
    if hour < 7:   return "very early morning"
    if hour < 12:  return "morning"
    if hour < 17:  return "afternoon"
    if hour < 21:  return "evening"
    return "night"


def analyze(text: str, hour: Optional[int] = None,
            acoustic_arousal: Optional[float] = None, reask: bool = False) -> Read:
    """Read the user's affect/intent from one utterance.

    hour             — user's local hour (0-23), for greeting/time-of-day mismatch.
    acoustic_arousal — optional 0..1 loudness hint carried from the mic (None if typed).
    reask            — True if this looks like a repeat/correction of the last request.
    """
    r = Read()
    raw = (text or "").strip()
    if not raw:
        return r
    low = raw.lower()
    tokens = _TOKEN_RE.findall(low)
    tokenset = set(tokens)

    # -- raw signal counts -------------------------------------------------------
    pos = len(tokenset & _POSITIVE)
    neg = len(tokenset & _NEGATIVE)
    prof = len(tokenset & _PROFANITY)
    letters = [c for c in raw if c.isalpha()]
    caps_ratio = (sum(c.isupper() for c in letters) / len(letters)) if len(letters) >= 4 else 0.0
    excls = raw.count("!")
    has_frustration = any(p in low for p in _FRUSTRATION) or reask
    has_urgency = any(p in low for p in _URGENCY)
    has_down = any(p in low for p in _DOWN)
    has_playful = any(p in low for p in _PLAYFUL) or any(e in low for e in _PLAYFUL_EMOJI)
    is_insult = any(p in low for p in _INSULT_PHRASES) or (prof and ("you" in tokenset or "jarvis" in low))
    is_gratitude = any(g in low for g in _GRATITUDE)
    is_question = raw.endswith("?") or low.startswith((
        "what", "why", "how", "when", "where", "who", "which", "can you", "could you",
        "do you", "is it", "are you", "should i", "would you"))
    starts_command = low.split()[0] in _COMMAND_VERBS if tokens else False
    polite = any(w in low for w in ("please", "could you", "would you", "thanks", "thank you", "kindly", "if you can"))
    blunt = starts_command and not polite and not is_question

    mismatch = _greeting_mismatch(low, hour)

    # -- valence / arousal / politeness ------------------------------------------
    r.valence = max(-1.0, min(1.0, (pos - neg - 1.2 * is_insult) / 3.0))
    text_arousal = min(1.0, 0.25 * excls + 0.6 * caps_ratio + 0.25 * prof
                       + 0.3 * has_urgency + 0.2 * has_frustration)
    r.arousal = max(text_arousal, acoustic_arousal or 0.0)
    r.politeness = max(-1.0, min(1.0, 0.5 * polite - 0.6 * is_insult - 0.2 * blunt))

    # -- resolve the dominant state (distress outranks everything) ---------------
    # Order matters: never crack a joke over genuine distress.
    if has_down:
        r.user_state = "down"
        r.flags.append("low_mood")
        r.pad_nudge = {"p": -0.04, "a": -0.06, "d": 0.04}
        r.guidance = "they sound low or worn out — be kind, steady and brief; no jokes, no fixing-by-lecture"
        r.suppress_sarcasm = True
    elif is_insult:
        r.user_state = "irritated"
        r.flags.append("insult_at_jarvis")
        r.pad_nudge = {"p": -0.12, "a": 0.14, "d": 0.16}
        r.guidance = ("they're taking a shot at you — stay unbothered and dry, don't grovel "
                      "and don't escalate, just do the task well")
        r.suppress_sarcasm = False   # an unbothered, dry line is fine; cruelty is not
    elif has_frustration or (neg and (excls or prof) and not pos):
        r.user_state = "frustrated"
        r.flags.append("frustration")
        if reask:
            r.flags.append("reask")
        r.pad_nudge = {"p": -0.08, "a": 0.18, "d": 0.10}
        r.guidance = "they're frustrated — cut the comedy, get crisp and actually fix it"
        r.suppress_sarcasm = True
    elif has_urgency:
        r.user_state = "stressed"
        r.flags.append("urgency")
        r.pad_nudge = {"p": -0.03, "a": 0.22, "d": 0.08}
        r.guidance = "they're in a hurry — answer first, trim everything, no preamble"
        r.suppress_sarcasm = True
    elif is_gratitude and r.valence >= 0:
        r.user_state = "grateful"
        r.flags.append("gratitude")
        r.pad_nudge = {"p": 0.22, "a": 0.04, "d": 0.06}
        r.guidance = "they're pleased with you — a short, smug 'you're welcome' is well earned"
    elif mismatch:
        phrase, expected, actual = mismatch
        r.user_state = "mismatch"
        r.flags.append("greeting_time_mismatch")
        r.pad_nudge = {"p": 0.12, "a": 0.06, "d": 0.10}
        r.guidance = (f"they said '{phrase}' but it's {actual} — a gentle, funny sanity-check "
                      f"about the time is exactly right here, then answer normally")
    elif has_playful or (caps_ratio > 0.5 and r.valence > 0):
        r.user_state = "playful"
        r.flags.append("banter")
        r.pad_nudge = {"p": 0.18, "a": 0.16, "d": 0.03}
        r.guidance = "they're being playful — match it, keep the volley quick and witty"
    elif excls >= 1 and r.valence > 0.2:
        r.user_state = "excited"
        r.flags.append("excited")
        r.pad_nudge = {"p": 0.15, "a": 0.20, "d": 0.0}
        r.guidance = "they're hyped — ride the energy but stay crisp"
    elif starts_command:
        r.user_state = "commanding"
        r.flags.append("command")
        r.pad_nudge = {"p": 0.0, "a": 0.06, "d": 0.04}
        r.guidance = ""   # normal work; persona's baseline handles tone
    elif is_question:
        r.user_state = "curious"
        r.flags.append("question")
        r.pad_nudge = {"p": 0.02, "a": 0.02, "d": 0.0}
        r.guidance = ""
    else:
        r.user_state = "neutral"
        # mild valence tracking so a kind word still warms him slightly
        r.pad_nudge = {"p": round(0.06 * r.valence, 3), "a": 0.0, "d": 0.0}
        r.guidance = ""

    if polite and not r.suppress_sarcasm:
        r.flags.append("polite")
    return r


if __name__ == "__main__":
    samples = [
        ("good morning jarvis", 1, None, False),
        ("this STILL doesn't work, again??", 14, None, True),
        ("thanks man, you're a lifesaver", 14, None, False),
        ("lol you're such a smartass", 20, None, False),
        ("ugh i'm so exhausted and overwhelmed today", 23, None, False),
        ("open chrome", 10, None, False),
        ("shut up jarvis", 15, None, False),
        ("what's the weather like", 9, None, False),
    ]
    for t, h, a, rk in samples:
        r = analyze(t, hour=h, acoustic_arousal=a, reask=rk)
        print(f"{t!r:48} -> {r.user_state:11} sup={int(r.suppress_sarcasm)} "
              f"nudge={r.pad_nudge}  | {r.guidance[:60]}")
