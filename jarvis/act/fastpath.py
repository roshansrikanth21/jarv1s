"""fastpath.py — deterministic intent router for simple OS commands.

The reasoning model is the wrong tool for "open Notepad". It adds a 1–5 s round-trip and,
worse, it can *say* it opened Notepad without any tool actually running. This module turns
a known class of plain commands into a concrete, machine-checkable action BEFORE the LLM is
ever consulted:

    "open notepad"            -> OPEN_APP      app=notepad
    "close chrome"            -> CLOSE_APP     app=chrome
    "volume up" / "louder"    -> VOLUME        direction=up
    "set volume to 30"        -> VOLUME        direction=set level=30
    "brightness down"         -> BRIGHTNESS    direction=down
    "mute"                    -> VOLUME        direction=mute
    "play" / "pause"          -> MEDIA         key=playpause
    "next track"              -> MEDIA         key=nexttrack
    "take a screenshot"       -> SCREENSHOT
    "lock my pc"              -> LOCK
    "turn wifi off"           -> WIFI          state=off

It is intentionally conservative: it matches only unambiguous, fully-specified commands and
returns None for everything else (which then goes to the full agent). A match is a *promise*
that api.py can execute and then VERIFY — it carries no side effects of its own, so it stays
pure and unit-testable with no imports from api.py.

Matching is purely lexical and contains no app allowlist — api.py owns which apps are
launchable/closable and reports honestly when one isn't. That keeps the capability check
(and the "Photoshop isn't installed" answer) in one place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class FastIntent:
    kind: str                       # OPEN_APP | CLOSE_APP | VOLUME | BRIGHTNESS | MEDIA | WIFI | LOCK | SCREENSHOT
    params: dict = field(default_factory=dict)
    label: str = ""                 # short human label for the debug trace

    def __post_init__(self):
        if not self.label:
            extra = " ".join(str(v) for v in self.params.values())
            self.label = f"{self.kind.lower()}{(' ' + extra) if extra else ''}".strip()


# Spoken aliases → the short app key api.py's allowlist understands. Only common ones; an
# unknown app still routes as OPEN_APP/CLOSE_APP and api.py decides if it can honour it.
_APP_ALIASES = {
    "vs code": "code", "vscode": "code", "visual studio code": "code", "the editor": "code",
    "google chrome": "chrome", "chrome browser": "chrome",
    "microsoft edge": "edge", "ms edge": "edge",
    "command prompt": "terminal", "cmd": "terminal", "windows terminal": "terminal",
    "powershell": "powershell",
    "file explorer": "explorer", "files": "explorer", "my files": "explorer",
    "calculator": "calc", "the calculator": "calc",
    "note pad": "notepad",
    "task manager": "taskmgr", "spotify": "spotify", "discord": "discord",
}

# Noise words trimmed from either end of a captured app name.
_APP_STRIP = re.compile(
    r"^(?:the|my|up|a|an)\s+|\s+(?:please|now|for\s+me|app|application|window|program|thanks|thank\s+you)$",
    re.I,
)


def _clean_app(raw: str) -> str:
    name = (raw or "").strip().strip(".!?,").lower()
    prev = None
    while name != prev:                       # peel leading/trailing noise repeatedly
        prev = name
        name = _APP_STRIP.sub("", name).strip()
    return _APP_ALIASES.get(name, name)


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = t.strip(".!?,;: ")
    t = re.sub(r"\s+", " ", t)
    t = t.replace("wi-fi", "wifi").replace("wi fi", "wifi")
    return t


# ── open / close ────────────────────────────────────────────────────────────────
_OPEN = re.compile(r"^(?:please\s+)?(?:open|launch|start|run|fire\s+up|boot\s+up|bring\s+up|pull\s+up|load)\s+(.+)$", re.I)
_CLOSE = re.compile(r"^(?:please\s+)?(?:close|quit|kill|exit|terminate)\s+(.+)$", re.I)

# ── volume ──────────────────────────────────────────────────────────────────────
_VOL_UPDOWN = re.compile(
    r"^(?:(?:turn|crank|bump|pump|push|put|take)\s+)?(?:the\s+)?(?:volume|sound|audio|it)\s+(up|down)$", re.I)
_VOL_WORD = re.compile(r"^(louder|quieter|softer)$", re.I)
_VOL_SET = re.compile(r"^(?:set|put|change)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:to|at)\s+(\d{1,3})\s*(?:%|percent)?$", re.I)
_VOL_MUTE = re.compile(r"^(mute|unmute)(?:\s+(?:the\s+)?(?:volume|sound|audio|everything|it))?$", re.I)

# ── brightness ────────────────────────────────────────────────────────────────────
_BRI_UPDOWN = re.compile(r"^(?:set\s+|turn\s+)?(?:the\s+)?brightness\s+(up|down)$", re.I)
_BRI_SET = re.compile(r"^(?:set|put|change)\s+(?:the\s+)?brightness\s+(?:to|at)\s+(\d{1,3})\s*(?:%|percent)?$", re.I)
_BRI_WORD = re.compile(r"^(dim|brighten)\s+(?:the\s+)?(?:screen|display|monitor|brightness)$", re.I)

# ── media keys ────────────────────────────────────────────────────────────────────
_MEDIA = [
    (re.compile(r"^(?:play|pause|resume|play\s*/?\s*pause)$", re.I), "playpause"),
    (re.compile(r"^(?:next|skip)(?:\s+(?:track|song|this))?$", re.I), "nexttrack"),
    (re.compile(r"^(?:previous|prev|last)(?:\s+(?:track|song))?$", re.I), "prevtrack"),
]

# ── reminders & goals (deterministic creation) ─────────────────────────────────────
# A clock like "7pm", "7:30 am", "19:00", normalised to 24h "HH:MM" for reminder.parse_when
# (whose HH:MM branch rolls to the next day if the time is already past).
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_DOW_WORDS = {"monday": "MON", "tuesday": "TUE", "wednesday": "WED", "thursday": "THU",
              "friday": "FRI", "saturday": "SAT", "sunday": "SUN",
              "mon": "MON", "tue": "TUE", "tues": "TUE", "wed": "WED", "thu": "THU",
              "thur": "THU", "thurs": "THU", "fri": "FRI", "sat": "SAT", "sun": "SUN"}


def _clock_to_hhmm(hh: str, mm: str | None, ampm: str | None) -> str | None:
    try:
        hour = int(hh)
    except (TypeError, ValueError):
        return None
    minute = int(mm) if mm else 0
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return f"{hour:02d}:{minute:02d}"


# Time phrases we can lift out of a sentence, longest/most-specific first.
_WHEN_PATTERNS = [
    re.compile(r"\bin\s+\d+\s*(?:min\w*|hour\w*|hr\w*|day\w*|sec\w*)\b", re.I),
    re.compile(r"\btomorrow(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?\b", re.I),
    re.compile(r"\btoday(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?\b", re.I),
    re.compile(r"\btonight\b", re.I),
    re.compile(r"\b(?:at|by|around|@)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", re.I),
    re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", re.I),
    re.compile(r"\b\d{1,2}\s*(?:am|pm)\b", re.I),
]
_REMIND_TRIGGER = re.compile(r"\b(?:please\s+)?remind\s+me(?:\s+(?:to|that|about))?\b", re.I)
_EVERY_RE = re.compile(r"\bevery\s+(day|morning|night|evening|week|"
                       r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|"
                       r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", re.I)


def _extract_when(low: str) -> tuple[str | None, tuple[int, int] | None]:
    """Find a time phrase in `low`; return (normalised when-string, (start,end) span) or
    (None, None). The when-string is something reminder.parse_when understands."""
    for rx in _WHEN_PATTERNS:
        m = rx.search(low)
        if not m:
            continue
        phrase = m.group(0)
        span = m.span()
        if phrase.startswith(("tomorrow", "today", "in ", "tonight")):
            if phrase == "tonight":
                return "today 20:00", span
            # normalise "tomorrow at 9am" → "tomorrow 9am" (parse_when accepts that)
            return phrase.replace(" at ", " "), span
        # a clock, possibly prefixed with at/by/around/@
        cm = _CLOCK.search(phrase)
        if cm:
            hhmm = _clock_to_hhmm(cm.group(1), cm.group(2), cm.group(3))
            if hhmm:
                return hhmm, span
    return None, None


def parse_reminder(text: str):
    """Deterministically pull a reminder out of a sentence. Returns {message, when, recurrence}
    or None. Handles 'remind me to X at 7pm', 'remind me at 7pm to X', 'tomorrow at 9am remind
    me to call mom', 'every monday remind me to update the tracker'."""
    raw = (text or "").strip().strip(".!?")
    low = raw.lower()
    has_remind = bool(_REMIND_TRIGGER.search(low))
    ev = _EVERY_RE.search(low)
    if not has_remind and not (ev and "remind" in low):
        return None
    if not has_remind:
        return None

    recurrence = None
    if ev:
        word = ev.group(1).lower()
        if word in ("day", "morning", "night", "evening"):
            recurrence = "daily"
        elif word == "week":
            recurrence = "weekly"
        else:
            dow = _DOW_WORDS.get(word)
            recurrence = f"weekly:{dow}" if dow else None

    when, _span = _extract_when(low)
    if when is None and recurrence is None:
        return None   # a reminder with no time is just a note — let the agent handle it

    # Build the message from the original text: drop the trigger, the every-phrase, the time
    # phrase, and connective leftovers — keeping original casing for the content.
    msg = _REMIND_TRIGGER.sub(" ", raw)
    msg = _EVERY_RE.sub(" ", msg)
    for rx in _WHEN_PATTERNS:
        msg = rx.sub(" ", msg)
    msg = re.sub(r"\b(?:to|that|about|at|by|on)\b\s*$", " ", msg, flags=re.I)
    msg = re.sub(r"^\s*(?:to|that|about)\b", " ", msg, flags=re.I)
    msg = re.sub(r"\s+", " ", msg).strip(" ,.:;-")
    return {"message": msg or "reminder", "when": when, "recurrence": recurrence}


# Goal: a future intention with a deadline.
_GOAL_TRIGGER = re.compile(
    r"^(?:i\s+(?:need|have|want|'ve\s+got|got)\s+to|i\s+must|remind me that i (?:need|have) to|"
    r"remember\s+(?:that\s+)?(?:my|i)|my)\b", re.I)
_GOAL_BY = re.compile(r"\b(?:by|before|due|deadline(?:\s+is)?|on)\b", re.I)
_DEADLINE_PATTERNS = [
    re.compile(r"\b(?:by|before|due(?:\s+on)?|deadline(?:\s+is)?(?:\s+on)?|on)\s+(.+)$", re.I),
]


def parse_goal(text: str):
    """Pull a goal + deadline out of 'I need to finish X by Friday', 'my report deadline is
    Oct 20', 'I have to submit the form by tomorrow'. Returns {title, deadline_text} or None.
    Only fires when there's a clear deadline cue — otherwise it's just conversation."""
    raw = (text or "").strip().strip(".!?")
    low = raw.lower()
    if not _GOAL_TRIGGER.search(low) or not _GOAL_BY.search(low):
        return None
    # Split on the deadline cue; everything before is the goal, after is the deadline.
    m = _DEADLINE_PATTERNS[0].search(raw)
    if not m:
        return None
    deadline_text = m.group(1).strip(" .")
    title = raw[:m.start()].strip()
    # Trim leading intent words so the title reads as the goal itself.
    title = re.sub(r"^(?:i\s+(?:need|have|want|'ve\s+got|got)\s+to|i\s+must|"
                   r"remind me that i (?:need|have) to|remember\s+(?:that\s+)?(?:my|i\s+need\s+to|i)|my)\s+",
                   "", title, flags=re.I).strip(" ,.:;-")
    title = re.sub(r"\s+(?:is|are|will be)$", "", title, flags=re.I).strip(" ,.:;-")
    if not title or not deadline_text:
        return None
    return {"title": title, "deadline_text": deadline_text}


# ── filesystem (verified, deterministic) ───────────────────────────────────────────
# "create a folder called X [in my documents]", "make a file notes.txt [in X] [with ...]",
# and the compound "folder X and a file Y in it". Deterministic so it works even when the
# cloud model is rate-limited — the exact case where trusting the LLM to call a tool is fragile.
_FS_LOC = r"(?:\s+(?:in|on|under|inside|into)\s+(?:my\s+|the\s+)?(?P<loc>[^\"']+?))?"
_FS_FOLDER = re.compile(
    rf"^(?:create|make|new|add)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:folder|directory|dir)\s+"
    rf"(?:called\s+|named\s+|titled\s+)?[\"']?(?P<name>[^\"']+?)[\"']?{_FS_LOC}$", re.I)
_FS_FILE = re.compile(
    rf"^(?:create|make|new|add)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:file|text\s*file|document|note)\s+"
    rf"(?:called\s+|named\s+|titled\s+)?[\"']?(?P<name>[^\"']+?)[\"']?{_FS_LOC}"
    rf"(?:\s+(?:with|containing|that\s+says|saying|with\s+the\s+text)\s+[\"']?(?P<content>.+?)[\"']?)?$", re.I)
# Compound: a folder AND a file (usually "inside it"). The file's content is optional.
_FS_COMPOUND = re.compile(
    rf"^(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called\s+|named\s+)?[\"']?(?P<folder>[^\"']+?)[\"']?{_FS_LOC}"
    rf"\s*(?:,|and|then|&)\s*(?:create|make|add|put|drop|place)?\s*(?:a\s+)?(?:new\s+)?(?:file|text\s*file|document|note)\s+"
    rf"(?:called\s+|named\s+)?[\"']?(?P<file>[^\"']+?)[\"']?(?:\s+(?:in\s+it|inside|inside\s+it|there|within))?"
    rf"(?:\s+(?:with|containing|that\s+says|saying)\s+[\"']?(?P<content>.+?)[\"']?)?$", re.I)
_FS_OPEN = re.compile(
    r"^open\s+(?:my\s+|the\s+)?(documents|desktop|downloads|pictures|music|videos|home\s+folder|downloads\s+folder)$", re.I)

# ── task queue ────────────────────────────────────────────────────────────────────
# Explicit "add a task ..." only — unambiguous queue intent. NOT "remind me to ..." (that's
# a scheduled reminder, a different tool). Fast-pathing this guarantees the task is really
# created instead of the model sometimes just replying "done".
_TASK_ADD = re.compile(r"^(?:add|create|queue|new)\s+(?:a\s+|an\s+)?task\s*(?:to|for|that|:|-|called)?\s*(.+)$", re.I)
_TASK_DONE = re.compile(r"^(?:complete|finish|close|mark)\s+task\s+#?(\d+)(?:\s+(?:as\s+)?(?:done|complete|completed|finished))?$", re.I)
_TASK_CANCEL = re.compile(r"^cancel\s+task\s+#?(\d+)$", re.I)

# ── wifi / lock / screenshot ──────────────────────────────────────────────────────
_WIFI = re.compile(r"^(?:turn\s+)?(?:wifi|wireless)\s+(on|off)$|^(?:turn\s+)?(on|off)\s+(?:the\s+)?wifi$", re.I)
_LOCK = re.compile(r"^lock\s+(?:my\s+|the\s+)?(?:pc|computer|screen|laptop|machine|desktop|workstation|session)$", re.I)
_SHOT = re.compile(r"^(?:take|grab|capture|snap|get|do)?\s*(?:a\s+)?screen\s?shot$", re.I)


def match(text: str) -> FastIntent | None:
    """Classify `text` into a FastIntent, or None if it isn't an unambiguous simple command."""
    t = _norm(text)
    if not t:
        return None

    m = _VOL_MUTE.match(t)
    if m:
        return FastIntent("VOLUME", {"direction": m.group(1).lower()})
    m = _VOL_UPDOWN.match(t)
    if m:
        return FastIntent("VOLUME", {"direction": m.group(1).lower()})
    m = _VOL_WORD.match(t)
    if m:
        return FastIntent("VOLUME", {"direction": "up" if m.group(1).lower() == "louder" else "down"})
    m = _VOL_SET.match(t)
    if m:
        return FastIntent("VOLUME", {"direction": "set", "level": int(m.group(1))})

    m = _BRI_UPDOWN.match(t)
    if m:
        return FastIntent("BRIGHTNESS", {"direction": m.group(1).lower()})
    m = _BRI_SET.match(t)
    if m:
        return FastIntent("BRIGHTNESS", {"direction": "set", "level": int(m.group(1))})
    m = _BRI_WORD.match(t)
    if m:
        return FastIntent("BRIGHTNESS", {"direction": "up" if m.group(1).lower() == "brighten" else "down"})

    for rx, key in _MEDIA:
        if rx.match(t):
            return FastIntent("MEDIA", {"key": key})

    # Reminders & goals — deterministic creation (checked before filesystem/open so a
    # "remind me to open X" isn't misread as an app launch).
    rem = parse_reminder(text)
    if rem is not None:
        return FastIntent("REMINDER", rem)
    goal = parse_goal(text)
    if goal is not None:
        return FastIntent("GOAL", goal)

    # Filesystem — match against the ORIGINAL text to keep names/content casing. Compound first.
    raw0 = (text or "").strip().strip(".!?")
    m = _FS_COMPOUND.match(raw0)
    if m:
        return FastIntent("FS_FOLDER_FILE", {
            "folder": m.group("folder").strip(), "location": (m.group("loc") or "").strip(),
            "file": m.group("file").strip(), "content": (m.group("content") or "").strip()})
    m = _FS_FILE.match(raw0)
    if m:
        return FastIntent("FS_FILE", {"name": m.group("name").strip(),
                                      "location": (m.group("loc") or "").strip(),
                                      "content": (m.group("content") or "").strip()})
    m = _FS_FOLDER.match(raw0)
    if m:
        return FastIntent("FS_FOLDER", {"name": m.group("name").strip(),
                                        "location": (m.group("loc") or "").strip()})
    m = _FS_OPEN.match(t)
    if m:
        return FastIntent("FS_OPEN", {"location": m.group(1).split()[0]})

    m = _TASK_DONE.match(t)
    if m:
        return FastIntent("TASK_DONE", {"n": int(m.group(1))})
    m = _TASK_CANCEL.match(t)
    if m:
        return FastIntent("TASK_CANCEL", {"n": int(m.group(1))})
    # Match the ADD description against the ORIGINAL text so the task keeps the user's casing
    # (e.g. "review the PR", not "review the pr").
    raw = (text or "").strip().strip(".!?,;: ")
    m = _TASK_ADD.match(raw)
    if m:
        desc = m.group(1).strip(" .")
        if desc and len(desc) >= 2:
            return FastIntent("TASK_ADD", {"text": desc})

    m = _WIFI.match(t)
    if m:
        return FastIntent("WIFI", {"state": (m.group(1) or m.group(2)).lower()})
    if _LOCK.match(t):
        return FastIntent("LOCK", {})
    if _SHOT.match(t):
        return FastIntent("SCREENSHOT", {})

    # Open/close LAST: their captures are greedy, so specific patterns above win first
    # (e.g. "turn it up" must not be read as open-an-app "it up").
    m = _CLOSE.match(t)
    if m:
        app = _clean_app(m.group(1))
        if app and _looks_like_app(app):
            return FastIntent("CLOSE_APP", {"app": app})
    m = _OPEN.match(t)
    if m:
        app = _clean_app(m.group(1))
        if app and _looks_like_app(app):
            return FastIntent("OPEN_APP", {"app": app})
    return None


# An app token is a short name/phrase, not a sentence. "open the pod bay doors" or
# "open a ticket about the bug" shouldn't be read as app launches — if the remainder after
# the verb is long or clause-like, let the full agent handle it.
_CLAUSE = re.compile(r"\b(?:and|then|so|because|about|to|for|with|that|which|if|when|but)\b", re.I)


def _looks_like_app(app: str) -> bool:
    words = app.split()
    if not (1 <= len(words) <= 4):
        return False
    if _CLAUSE.search(app):
        return False
    if len(app) > 40:
        return False
    return True
