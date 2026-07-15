#!/usr/bin/env python3
"""Night Shift — the deterministic queue + state engine.

This script is the ONLY thing that ever edits queue state. The overnight AI does the
thinking; this script does the bookkeeping, so the queue files can never end up
half-written or corrupted by a hallucinated status line at 2 AM.

Two markdown files are the whole interface:
  PROJECTS.md   the committed work queue   ("## NS-001 | status | Title" + description)
  IDEAS.md      the raw idea backlog       ("## IDEA-001 | status | Title" + description)

Project statuses: pending, in_progress, done, failed, stalled, partial(→requeued pending)
Idea statuses:    raw, promoted, parked, rejected

The claim command decides the hour's MODE (execute-first) and signals it via exit code:
  0  EXECUTE  a pending project   (handoff written to current.json, task_type "execute")
  4  CURATE   raw ideas exist, queue empty        (task_type "curate")
  5  IDEATE   queue AND backlog empty              (task_type "ideate")
  3  SKIP     a fresh lock already exists (a run is still in progress)
  1  ERROR    unexpected failure
  2  (reserved / argparse usage errors)

Everything is safe to rerun, reads/writes UTF-8 explicitly, and never leaves a queue
file half-written (write-to-temp + atomic replace).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECTS_FILE = ROOT / "PROJECTS.md"
IDEAS_FILE = ROOT / "IDEAS.md"
CURRENT_FILE = ROOT / "current.json"
LOCK_FILE = ROOT / ".lock"
RUNS_DIR = ROOT / "runs"

LOCK_STALE_HOURS = float(os.environ.get("NS_LOCK_STALE_HOURS", "3"))

PROJECT_STATUSES = ("pending", "in_progress", "done", "failed", "stalled", "partial")
IDEA_STATUSES = ("raw", "promoted", "parked", "rejected")

# Exit codes — the runner branches on these.
EXIT_EXECUTE, EXIT_ERROR, EXIT_USAGE, EXIT_SKIP, EXIT_CURATE, EXIT_IDEATE = 0, 1, 2, 3, 4, 5

# "## NS-001 | pending | Title"  — id + status + title. Bare "## Title" is adopted on the
# next pass and rewritten into the proper form with the next free id.
_HDR_FULL = re.compile(r"^##\s+(?P<id>(?:NS|IDEA)-\d+)\s*\|\s*(?P<status>\w+)\s*\|\s*(?P<title>.+?)\s*$")
_HDR_BARE = re.compile(r"^##\s+(?P<title>.+?)\s*$")


# ── low-level IO ─────────────────────────────────────────────────────────────────
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same dir, then atomically replace — a crash mid-write
    can never leave the queue file truncated."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── parsing / rendering ──────────────────────────────────────────────────────────
class Entry:
    """One project or idea: header (id/status/title) plus the free-text body below it."""
    def __init__(self, kind: str, id_: str | None, status: str, title: str, body: str):
        self.kind = kind            # "project" | "idea"
        self.id = id_               # NS-001 / IDEA-001, or None until adopted
        self.status = status
        self.title = title
        self.body = body.rstrip("\n")

    def render(self) -> str:
        head = f"## {self.id} | {self.status} | {self.title}"
        return head + ("\n" + self.body if self.body.strip() else "")


def _parse(path: Path, prefix: str) -> list[Entry]:
    """Parse PROJECTS.md / IDEAS.md into Entry objects. A bare '## Title' with no id/status
    is a hand-added item — captured here with id=None so `_reindex` can adopt it."""
    kind = "project" if prefix == "NS" else "idea"
    default_status = "pending" if prefix == "NS" else "raw"
    text = _read(path)
    entries: list[Entry] = []
    cur: Entry | None = None
    body_lines: list[str] = []
    for line in text.splitlines():
        m = _HDR_FULL.match(line)
        if m and m.group("id").startswith(prefix):
            if cur is not None:
                cur.body = "\n".join(body_lines).strip("\n")
                entries.append(cur)
            cur = Entry(kind, m.group("id"), m.group("status"), m.group("title"), "")
            body_lines = []
            continue
        mb = _HDR_BARE.match(line)
        if mb and not line.startswith("###") and not _HDR_FULL.match(line):
            # A new bare header starts a new (unadopted) entry.
            if cur is not None:
                cur.body = "\n".join(body_lines).strip("\n")
                entries.append(cur)
            cur = Entry(kind, None, default_status, mb.group("title"), "")
            body_lines = []
            continue
        if cur is not None:
            body_lines.append(line)
    if cur is not None:
        cur.body = "\n".join(body_lines).strip("\n")
        entries.append(cur)
    return entries


def _next_id(entries: list[Entry], prefix: str) -> str:
    nums = [int(e.id.split("-")[1]) for e in entries if e.id and e.id.startswith(prefix)]
    return f"{prefix}-{(max(nums) + 1 if nums else 1):03d}"


def _reindex(entries: list[Entry], prefix: str) -> bool:
    """Adopt any bare (id=None) entries by assigning the next free id. Returns True if any
    change was made."""
    changed = False
    for e in entries:
        if e.id is None:
            e.id = _next_id(entries, prefix)
            changed = True
    return changed


def _write_entries(path: Path, entries: list[Entry], header: str) -> None:
    parts = [header.rstrip() + "\n"]
    for e in entries:
        parts.append("\n" + e.render() + "\n")
    _atomic_write(path, "".join(parts).rstrip() + "\n")


PROJECTS_HEADER = (
    "# PROJECTS.md — Night Shift work queue\n"
    "#\n"
    "# Add work by typing a heading:  `## Some title`  then a description below it.\n"
    "# The engine adopts it into `## NS-00X | pending | Some title` on its next pass.\n"
    "# Order = priority (top runs first). Never hand-edit the status column; the engine owns it.\n"
)
IDEAS_HEADER = (
    "# IDEAS.md — Night Shift idea backlog\n"
    "#\n"
    "# The curator promotes raw ideas into PROJECTS.md; the ideator appends new ones here.\n"
    "# Statuses: raw, promoted, parked, rejected. The engine owns this column.\n"
)


def _load_projects() -> list[Entry]:
    return _parse(PROJECTS_FILE, "NS")


def _load_ideas() -> list[Entry]:
    return _parse(IDEAS_FILE, "IDEA")


def _save_projects(entries: list[Entry]) -> None:
    _write_entries(PROJECTS_FILE, entries, PROJECTS_HEADER)


def _save_ideas(entries: list[Entry]) -> None:
    _write_entries(IDEAS_FILE, entries, IDEAS_HEADER)


def _append_trail(entry: Entry, note: str) -> None:
    """Append a timestamped status-trail line to an entry's body so the history is visible
    in the queue file itself."""
    line = f"- _[{_now_iso()}] {note}_"
    entry.body = (entry.body + "\n" + line).strip("\n")


# ── lock ─────────────────────────────────────────────────────────────────────────
def _lock_is_fresh() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        ts = datetime.fromisoformat(_read(LOCK_FILE).strip())
    except Exception:
        return False   # unparseable lock = stale, safe to take over
    return datetime.now() - ts < timedelta(hours=LOCK_STALE_HOURS)


def _take_lock() -> None:
    _atomic_write(LOCK_FILE, _now_iso())


def _clear_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ── commands ─────────────────────────────────────────────────────────────────────
def cmd_init(_args) -> int:
    if not PROJECTS_FILE.exists():
        _save_projects([])
    if not IDEAS_FILE.exists():
        _save_ideas([])
    RUNS_DIR.mkdir(exist_ok=True)
    print("initialized nightshift queue at", ROOT)
    return EXIT_EXECUTE


def cmd_claim(args) -> int:
    """Decide the hour's mode (execute-first) and write the handoff. Adopts bare headers,
    honors the lock, and supports a --mode override for the trust test."""
    if _lock_is_fresh() and not args.force:
        print("SKIP: a fresh lock exists (a run is still in progress)")
        return EXIT_SKIP

    projects = _load_projects()
    ideas = _load_ideas()
    # Adopt any hand-added bare headers before deciding.
    if _reindex(projects, "NS"):
        _save_projects(projects)
    if _reindex(ideas, "IDEA"):
        _save_ideas(ideas)

    mode = args.mode  # optional override: execute | curate | ideate
    target: Entry | None = None
    if mode in (None, "execute"):
        target = next((p for p in projects if p.status == "pending"), None)
        if target:
            mode = "execute"
    if target is None and mode in (None, "curate"):
        if any(i.status == "raw" for i in ideas) or mode == "curate":
            mode = "curate"
    if mode is None:
        mode = "ideate"

    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")

    if mode == "execute":
        if target is None:
            print("no pending project to execute")
            return EXIT_ERROR
        attempt = _attempt_of(target) + 1
        _set_attempt(target, attempt)
        target.status = "in_progress"
        _append_trail(target, f"claimed (attempt {attempt})")
        _save_projects(projects)
        runlog = f"runs/{stamp}_{target.id}.md"
        _write_current({
            "task_type": "execute", "id": target.id, "title": target.title,
            "description": target.body, "attempt": attempt,
            "timestamp": _now_iso(), "runlog": runlog,
        })
        _take_lock()
        print(f"EXECUTE {target.id} (attempt {attempt}) -> {runlog}")
        return EXIT_EXECUTE

    if mode == "curate":
        runlog = f"runs/{stamp}_curate.md"
        _write_current({
            "task_type": "curate", "id": "curate", "title": "Curate raw ideas",
            "description": "", "attempt": 1, "timestamp": _now_iso(), "runlog": runlog,
        })
        _take_lock()
        print(f"CURATE -> {runlog}")
        return EXIT_CURATE

    # ideate
    runlog = f"runs/{stamp}_ideate.md"
    _write_current({
        "task_type": "ideate", "id": "ideate", "title": "Generate new project ideas",
        "description": "", "attempt": 1, "timestamp": _now_iso(), "runlog": runlog,
    })
    _take_lock()
    print(f"IDEATE -> {runlog}")
    return EXIT_IDEATE


def cmd_complete(args) -> int:
    """The AI's mandatory final action. status ∈ done|failed|partial (execute) or done (curate/
    ideate). partial requeues the project as pending so the next hour continues it."""
    cur = _read_current()
    if not cur:
        print("no current.json — nothing to complete")
        return EXIT_ERROR
    status = args.status
    note = args.note or ""
    task_type = cur.get("task_type", "execute")

    if task_type in ("curate", "ideate"):
        # No project row to update — just record and release.
        print(f"{task_type} complete ({status})")
        CURRENT_FILE.unlink(missing_ok=True)
        _clear_lock()
        return EXIT_EXECUTE

    projects = _load_projects()
    target = next((p for p in projects if p.id == cur.get("id")), None)
    if target is None:
        print(f"project {cur.get('id')} not found")
        CURRENT_FILE.unlink(missing_ok=True)
        _clear_lock()
        return EXIT_ERROR

    if status == "partial":
        target.status = "pending"   # requeue to continue next hour
        _append_trail(target, f"partial — requeued. {note}".strip())
    elif status in ("done", "failed"):
        target.status = status      # final
        _append_trail(target, f"{status}. {note}".strip())
    else:
        print(f"invalid status: {status}")
        return EXIT_USAGE
    _save_projects(projects)
    print(f"{target.id} -> {target.status}")
    CURRENT_FILE.unlink(missing_ok=True)
    _clear_lock()
    return EXIT_EXECUTE


def cmd_reap(args) -> int:
    """Crash handler — ALWAYS run after every AI session, pass it the agent's exit code.
    If current.json still exists the session died/stalled without reporting: requeue on the
    first failed attempt, mark failed on the second, mark stalled if it exited cleanly but
    never called complete. Writes a stub run log and ALWAYS clears the lock."""
    cur = _read_current()
    if not cur:
        _clear_lock()   # nothing in flight — just make sure the next hour isn't blocked
        print("reap: clean (no in-flight work)")
        return EXIT_EXECUTE

    agent_exit = args.agent_exit
    task_type = cur.get("task_type", "execute")
    stub = ROOT / cur.get("runlog", f"runs/{datetime.now():%Y-%m-%d_%H%M}_crash.md")
    stub.parent.mkdir(parents=True, exist_ok=True)

    if task_type in ("curate", "ideate"):
        _write_stub(stub, cur, "failed", f"{task_type} session ended without calling complete (exit {agent_exit})")
        print(f"reap: {task_type} released (exit {agent_exit})")
        CURRENT_FILE.unlink(missing_ok=True)
        _clear_lock()
        return EXIT_EXECUTE

    projects = _load_projects()
    target = next((p for p in projects if p.id == cur.get("id")), None)
    if target is not None:
        attempt = _attempt_of(target)
        # exit 0 but never completed → it exited cleanly yet didn't report = stalled.
        if agent_exit == 0:
            target.status = "stalled"
            _append_trail(target, "stalled — session exited without calling complete")
            outcome = "stalled"
        elif attempt <= 1:
            target.status = "pending"   # first crash: give it one more hour
            _append_trail(target, f"crashed (exit {agent_exit}) — requeued for retry")
            outcome = "requeued"
        else:
            target.status = "failed"    # second crash: retire it
            _append_trail(target, f"crashed again (exit {agent_exit}) — retired as failed")
            outcome = "failed"
        _save_projects(projects)
        _write_stub(stub, cur, outcome, f"session crashed/stalled (exit {agent_exit})")
        print(f"reap: {target.id} -> {outcome}")
    else:
        print("reap: in-flight project not found; releasing lock")

    CURRENT_FILE.unlink(missing_ok=True)
    _clear_lock()
    return EXIT_EXECUTE


def cmd_list(_args) -> int:
    projects = _load_projects()
    ideas = _load_ideas()
    from collections import Counter
    pc, ic = Counter(p.status for p in projects), Counter(i.status for i in ideas)
    print("PROJECTS:", dict(pc) or "(none)")
    for p in projects:
        print(f"  {p.id} | {p.status} | {p.title}")
    print("IDEAS:", dict(ic) or "(none)")
    for i in ideas:
        print(f"  {i.id} | {i.status} | {i.title}")
    return EXIT_EXECUTE


def cmd_add(args) -> int:
    projects = _load_projects()
    _reindex(projects, "NS")
    new = Entry("project", _next_id(projects, "NS"), "pending", args.title,
                (args.desc or "").strip())
    if args.desc:
        new.body = args.desc.strip()
    _append_trail(new, f"added (priority {args.priority})")
    if args.priority == "high":
        # high priority goes to the top of the pending run order
        first_pending = next((idx for idx, p in enumerate(projects) if p.status == "pending"), len(projects))
        projects.insert(first_pending, new)
    else:
        projects.append(new)
    _save_projects(projects)
    print(f"added {new.id} | {new.title}")
    return EXIT_EXECUTE


def cmd_idea_add(args) -> int:
    ideas = _load_ideas()
    _reindex(ideas, "IDEA")
    new = Entry("idea", _next_id(ideas, "IDEA"), "raw", args.title, (args.desc or "").strip())
    if args.domain:
        new.body = (new.body + f"\n- domain: {args.domain}").strip("\n")
    ideas.append(new)
    _save_ideas(ideas)
    print(f"added {new.id} | {new.title}")
    return EXIT_EXECUTE


def cmd_promote(args) -> int:
    """Curator action: turn a raw idea into a pending project, mark the idea promoted with a
    pointer to the new project id."""
    ideas = _load_ideas()
    idea = next((i for i in ideas if i.id == args.idea_id), None)
    if idea is None:
        print(f"idea {args.idea_id} not found")
        return EXIT_ERROR
    projects = _load_projects()
    _reindex(projects, "NS")
    new_id = _next_id(projects, "NS")
    proj = Entry("project", new_id, "pending", idea.title, idea.body)
    _append_trail(proj, f"promoted from {idea.id}")
    projects.append(proj)
    _save_projects(projects)
    idea.status = "promoted"
    _append_trail(idea, f"promoted -> {new_id}")
    _save_ideas(ideas)
    print(f"promoted {idea.id} -> {new_id}")
    return EXIT_EXECUTE


def cmd_idea_status(args) -> int:
    """Curator action: park or reject a raw idea with a one-line note."""
    ideas = _load_ideas()
    idea = next((i for i in ideas if i.id == args.idea_id), None)
    if idea is None:
        print(f"idea {args.idea_id} not found")
        return EXIT_ERROR
    if args.status not in ("parked", "rejected"):
        print("status must be parked or rejected")
        return EXIT_USAGE
    idea.status = args.status
    _append_trail(idea, f"{args.status}. {args.note or ''}".strip())
    _save_ideas(ideas)
    print(f"{idea.id} -> {args.status}")
    return EXIT_EXECUTE


# ── attempt counter + current.json + stub helpers ────────────────────────────────
_ATTEMPT_RE = re.compile(r"<!--attempt:(\d+)-->")


def _attempt_of(entry: Entry) -> int:
    m = _ATTEMPT_RE.search(entry.body)
    return int(m.group(1)) if m else 0


def _set_attempt(entry: Entry, n: int) -> None:
    if _ATTEMPT_RE.search(entry.body):
        entry.body = _ATTEMPT_RE.sub(f"<!--attempt:{n}-->", entry.body)
    else:
        entry.body = (entry.body + f"\n<!--attempt:{n}-->").strip("\n")


def _write_current(data: dict) -> None:
    _atomic_write(CURRENT_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def _read_current() -> dict | None:
    if not CURRENT_FILE.exists():
        return None
    try:
        return json.loads(_read(CURRENT_FILE))
    except Exception:
        return None


def _write_stub(path: Path, cur: dict, status: str, reason: str) -> None:
    """A run log the morning report can show, even for a crashed session — so a broken night
    looks broken."""
    fm = (
        "---\n"
        f"project: {cur.get('id', '?')}\n"
        f"title: {cur.get('title', '?')}\n"
        f"mode: {cur.get('task_type', 'execute')}\n"
        f"status: {status}\n"
        f"started: {cur.get('timestamp', '?')}\n"
        f"finished: {_now_iso()}\n"
        "---\n\n"
        "## Summary\n"
        f"- {reason}\n"
    )
    _atomic_write(path, fm)


# ── CLI ──────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Night Shift queue + state engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create empty queue files").set_defaults(func=cmd_init)

    c = sub.add_parser("claim", help="decide the hour's mode + write the handoff")
    c.add_argument("--mode", choices=["execute", "curate", "ideate"], help="force a mode (trust test)")
    c.add_argument("--force", action="store_true", help="ignore a fresh lock")
    c.set_defaults(func=cmd_claim)

    c = sub.add_parser("complete", help="AI's mandatory final action")
    c.add_argument("status", choices=["done", "failed", "partial"])
    c.add_argument("--note", default="")
    c.set_defaults(func=cmd_complete)

    c = sub.add_parser("reap", help="crash handler — run after every session")
    c.add_argument("--agent-exit", type=int, default=0, help="exit code of the AI session")
    c.set_defaults(func=cmd_reap)

    sub.add_parser("list", help="show queue + backlog state").set_defaults(func=cmd_list)

    c = sub.add_parser("add", help="add a project")
    c.add_argument("title")
    c.add_argument("--desc", default="")
    c.add_argument("--priority", choices=["high", "medium", "low"], default="medium")
    c.set_defaults(func=cmd_add)

    c = sub.add_parser("idea-add", help="add a raw idea")
    c.add_argument("title")
    c.add_argument("--desc", default="")
    c.add_argument("--domain", default="")
    c.set_defaults(func=cmd_idea_add)

    c = sub.add_parser("promote", help="promote a raw idea into a pending project")
    c.add_argument("idea_id")
    c.set_defaults(func=cmd_promote)

    c = sub.add_parser("idea-status", help="park/reject a raw idea")
    c.add_argument("idea_id")
    c.add_argument("status", choices=["parked", "rejected"])
    c.add_argument("--note", default="")
    c.set_defaults(func=cmd_idea_status)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:   # never leave the caller without a clear exit code
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
