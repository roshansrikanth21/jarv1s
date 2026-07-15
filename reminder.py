"""reminder.py — OS-native scheduled reminders that fire even when JARVIS is closed.

Mark-XLVIII pattern: use the OS's own scheduler + a small "wake up, show a toast"
helper. Windows Task Scheduler / macOS launchd / Linux systemd. Cross-platform
signature; Windows implementation is complete, macOS/Linux fall back to a
"schedule at OS level yourself" error so we never lie about having scheduled it.

Public API (all sync, callable from desktop.py's `remind` action):
    schedule(when: str, message: str, title: str = "JARVIS") -> dict
    list_all() -> list[dict]
    cancel(reminder_id: str) -> dict

Windows implementation:
    - Uses `schtasks /Create` to register a one-shot task under \\JARVIS\\.
    - The task's action is `powershell.exe -Command <toast>` — a minimal
      BurntToast-style call that shows a native Action Center notification.
    - No pip deps (BurntToast module isn't required — we use the built-in
      Windows Runtime API via ToastNotificationManager if it's there, else a
      MessageBox fallback).

Time parsing:
    ISO 8601 ("2026-07-15T15:00"), "in 5 minutes", "in 2 hours", "tomorrow 9am",
    "today 15:00", or a bare "HH:MM" (interpreted as next occurrence). Kept
    small on purpose — the model can format the ISO if needed.
"""
from __future__ import annotations

import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TASK_PREFIX = "JARVIS_Reminder_"


# ── time parsing ────────────────────────────────────────────────────────────────
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?:\s*(am|pm))?$", re.I)
_REL_RE = re.compile(r"^in\s+(\d+)\s*(min|mins|minute|minutes|hr|hrs|hour|hours|"
                     r"sec|secs|second|seconds|day|days)$", re.I)
_TOMORROW_RE = re.compile(r"^tomorrow(?:\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?$", re.I)
_TODAY_RE = re.compile(r"^today(?:\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?$", re.I)


def parse_when(s: str, *, now: datetime | None = None) -> datetime | None:
    """Parse a human 'when' into a concrete datetime. Returns None on failure."""
    if not s:
        return None
    now = now or datetime.now()
    text = s.strip()

    # Try ISO first — the format we'd prefer the model to emit.
    try:
        dt = datetime.fromisoformat(text)
        return dt
    except ValueError:
        pass

    m = _REL_RE.match(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("sec"):
            delta = timedelta(seconds=n)
        elif unit.startswith("min"):
            delta = timedelta(minutes=n)
        elif unit.startswith(("hr", "hour")):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        return now + delta

    m = _TOMORROW_RE.match(text)
    if m:
        base = (now + timedelta(days=1)).replace(second=0, microsecond=0)
        return _apply_hh_mm(base, m.group(1), m.group(2), m.group(3), default_hour=9)

    m = _TODAY_RE.match(text)
    if m:
        base = now.replace(second=0, microsecond=0)
        return _apply_hh_mm(base, m.group(1), m.group(2), m.group(3), default_hour=now.hour + 1)

    m = _HHMM_RE.match(text)
    if m:
        base = now.replace(second=0, microsecond=0)
        target = _apply_hh_mm(base, m.group(1), m.group(2), m.group(3), default_hour=0)
        if target and target <= now:
            target = target + timedelta(days=1)   # next occurrence
        return target

    return None


def _apply_hh_mm(base: datetime, hh: str | None, mm: str | None,
                 ampm: str | None, *, default_hour: int) -> datetime | None:
    """Apply optional hh/mm/am-pm to a base datetime; None on invalid values."""
    try:
        hour = int(hh) if hh else default_hour
        minute = int(mm) if mm else 0
    except ValueError:
        return None
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return base.replace(hour=hour, minute=minute)


# ── the toast payload the scheduler runs at fire time ──────────────────────────
def _toast_powershell(title: str, message: str) -> str:
    """Return a PowerShell snippet that shows a Windows Action Center toast.
    Uses the WinRT ToastNotificationManager (present on Win10+). Falls back to a
    plain MsgBox if the WinRT path fails so the reminder is never invisible."""
    # Escape single-quotes for PowerShell string literals.
    t = (title or "JARVIS").replace("'", "''")
    m = (message or "").replace("'", "''")
    return (
        "try {"
        "  [Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime] > $null;"
        "  [Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,"
        "ContentType=WindowsRuntime] > $null;"
        f"  $xml = '<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{t}</text><text>{m}</text></binding></visual></toast>';"
        "  $doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "  $doc.LoadXml($xml);"
        "  $notif = [Windows.UI.Notifications.ToastNotification]::new($doc);"
        "  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('JARVIS')"
        ".Show($notif)"
        "} catch {"
        "  Add-Type -AssemblyName PresentationFramework;"
        f"  [System.Windows.MessageBox]::Show('{m}','{t}') > $null"
        "}"
    )


# ── Windows: schtasks-backed schedule / list / cancel ──────────────────────────
def _win_schedule(when: datetime, title: str, message: str) -> dict:
    rid = uuid.uuid4().hex[:12]
    task_name = f"{TASK_PREFIX}{rid}"
    ps_payload = _toast_powershell(title, message)
    # schtasks wants /SC ONCE + /SD MM/DD/YYYY + /ST HH:MM (24h). /RL LIMITED so no admin.
    sd = when.strftime("%m/%d/%Y")
    st = when.strftime("%H:%M")
    argv = [
        "schtasks.exe", "/Create",
        "/TN", task_name,
        "/SC", "ONCE",
        "/SD", sd,
        "/ST", st,
        "/TR", f'powershell.exe -NoProfile -WindowStyle Hidden -Command "{ps_payload}"',
        "/RL", "LIMITED",
        "/F",   # overwrite if exists
    ]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15,
                           creationflags=_NO_WINDOW)
    except Exception as exc:
        return {"ok": False, "error": f"schtasks call failed: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout or "").strip()[:300]}
    return {"ok": True, "id": rid, "task_name": task_name,
            "when": when.isoformat(timespec="minutes"),
            "title": title, "message": message}


def _win_list() -> list[dict]:
    """Enumerate JARVIS_Reminder_* scheduled tasks. Returns [{id, when, next_run, status}]."""
    argv = ["schtasks.exe", "/Query", "/FO", "CSV", "/V"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                           creationflags=_NO_WINDOW)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    lines = [ln for ln in (r.stdout or "").splitlines() if TASK_PREFIX in ln]
    out: list[dict] = []
    for ln in lines:
        # CSV columns can contain quoted commas; use csv module for safety.
        import csv, io
        row = next(csv.reader(io.StringIO(ln)), None)
        if not row:
            continue
        name = row[1] if len(row) > 1 else ""
        next_run = row[2] if len(row) > 2 else ""
        status = row[3] if len(row) > 3 else ""
        name = name.strip().lstrip("\\")
        rid = name.replace(TASK_PREFIX, "", 1) if name.startswith(TASK_PREFIX) else name
        out.append({"id": rid, "task_name": name,
                    "next_run": next_run, "status": status})
    return out


def _win_cancel(rid: str) -> dict:
    task_name = rid if rid.startswith(TASK_PREFIX) else f"{TASK_PREFIX}{rid}"
    argv = ["schtasks.exe", "/Delete", "/TN", task_name, "/F"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                           creationflags=_NO_WINDOW)
    except Exception as exc:
        return {"ok": False, "error": f"schtasks call failed: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout or "").strip()[:300]}
    return {"ok": True, "id": rid.replace(TASK_PREFIX, "", 1),
            "task_name": task_name, "deleted": True}


# ── public API ──────────────────────────────────────────────────────────────────
_NOT_WINDOWS_ERR = ("reminder: OS-native scheduling is Windows-only for now. On "
                    "macOS/Linux, ask the user to run a one-shot `at` or "
                    "`systemd-run` command themselves.")


def schedule(when: str, message: str, title: str = "JARVIS") -> dict:
    """Schedule a reminder. `when` accepts ISO, 'in 5 minutes', 'tomorrow 9am',
    'today 15:00', 'HH:MM'. Returns {ok, id, when, ...} on success."""
    if not IS_WINDOWS:
        return {"ok": False, "error": _NOT_WINDOWS_ERR}
    if not message:
        return {"ok": False, "error": "reminder: 'message' is required."}
    when_dt = parse_when(when)
    if not when_dt:
        return {"ok": False,
                "error": f"reminder: could not parse when={when!r}. Try ISO "
                         "(2026-07-15T15:00), 'in 5 minutes', 'tomorrow 9am', "
                         "'today 15:00', or 'HH:MM'."}
    if when_dt <= datetime.now():
        return {"ok": False,
                "error": f"reminder: {when_dt.isoformat(timespec='minutes')} is in the "
                         "past. Pick a future time."}
    return _win_schedule(when_dt, title, message)


def list_all() -> list[dict]:
    """Return currently-scheduled JARVIS reminders. Empty list if none / non-Windows."""
    if not IS_WINDOWS:
        return []
    return _win_list()


def cancel(reminder_id: str) -> dict:
    """Cancel by short id (returned from schedule) or full task name."""
    if not IS_WINDOWS:
        return {"ok": False, "error": _NOT_WINDOWS_ERR}
    rid = (reminder_id or "").strip()
    if not rid:
        return {"ok": False, "error": "reminder: id is required."}
    return _win_cancel(rid)
