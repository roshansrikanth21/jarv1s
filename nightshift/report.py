#!/usr/bin/env python3
"""Night Shift — the morning report.

Fully deterministic: it scans run logs and reports ONLY what they say. No language model
anywhere in this file — no summarizing, no invented outcomes. The layer that tells you what
happened must be incapable of embellishing it. Because the reaper writes a stub log even for
crashed sessions, failures show with the same prominence as wins: a broken night looks broken.

Outputs:
  1. A durable dated report at reports/YYYY-MM-DD.md (run counts by mode + one section per run +
     current queue state).
  2. A compact digest surfaced to you — a Windows toast via JARVIS's desktop.notify, and the
     digest text printed to stdout. --dry-run prints without notifying.

Scheduled at 6:45 AM, after the final 6 AM hourly slot.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
REPORTS_DIR = ROOT / "reports"

STATUS_EMOJI = {"done": "✅", "partial": "🟡", "failed": "❌", "stalled": "⚠️",
                "requeued": "🔁", "in_progress": "⏳"}
MODE_TAG = {"execute": "[exec]", "curate": "[curate]", "ideate": "[ideate]"}


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _parse_run(path: Path) -> dict:
    """Pull frontmatter + the Summary bullets out of one run log."""
    text = _read(path)
    fm: dict[str, str] = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        body = m.group(2)
    bullets: list[str] = []
    in_summary = False
    for line in body.splitlines():
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            s = line.strip()
            if s.startswith(("-", "*", "•")):
                bullets.append(s.lstrip("-*• ").strip())
    return {"path": path, "fm": fm, "bullets": bullets}


def _started(run: dict) -> datetime | None:
    raw = run["fm"].get("started", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except Exception:
            continue
    # fall back to the run-log filename timestamp: YYYY-MM-DD_HHMM_*
    m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})", run["path"].name)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}{m.group(3)}", "%Y-%m-%d %H%M")
        except Exception:
            return None
    return None


def _collect(hours: float) -> list[dict]:
    cutoff = datetime.now() - timedelta(hours=hours)
    runs = []
    for p in sorted(RUNS_DIR.glob("*.md")):
        run = _parse_run(p)
        st = _started(run)
        if st and st >= cutoff:
            run["_started"] = st
            runs.append(run)
    runs.sort(key=lambda r: r["_started"])
    return runs


def _queue_state() -> tuple[dict, list[str]]:
    """Read live queue counts + the next few pending titles, via the engine's parser."""
    sys.path.insert(0, str(ROOT))
    try:
        import queue as _q  # the Night Shift engine (nightshift/queue.py)
        projects = _q._load_projects()
        ideas = _q._load_ideas()
    except Exception:
        return {}, []
    counts = {"projects": dict(Counter(p.status for p in projects)),
              "ideas": dict(Counter(i.status for i in ideas))}
    pending = [p.title for p in projects if p.status == "pending"][:5]
    return counts, pending


def build_report(runs: list[dict], counts: dict, pending: list[str]) -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    modes = Counter(r["fm"].get("mode", "execute") for r in runs)
    lines = [f"# Night Shift report — {day}", ""]
    if not runs:
        lines += ["No runs found in the last 12 hours.",
                  f"Check the nightly log at `nightshift/logs/night_{day}.log` for diagnosis.", ""]
    else:
        summary = ", ".join(f"{n} {m}" for m, n in modes.items())
        lines += [f"**{len(runs)} runs**: {summary}", ""]
        for r in runs:
            fm = r["fm"]
            status = fm.get("status", "?")
            emoji = STATUS_EMOJI.get(status, "•")
            tag = MODE_TAG.get(fm.get("mode", "execute"), "")
            title = fm.get("title", fm.get("project", r["path"].stem))
            lines.append(f"## {emoji} {tag} {fm.get('project', '')} — {title}  ({status})")
            lines.append(f"_{fm.get('started', '?')} → {fm.get('finished', '?')}  ·  `nightshift/{r['path'].relative_to(ROOT).as_posix()}`_")
            for b in r["bullets"]:
                lines.append(f"- {b}")
            lines.append("")
    lines += ["---", "## Queue state",
              f"- projects: {counts.get('projects') or '(none)'}",
              f"- ideas: {counts.get('ideas') or '(none)'}"]
    if pending:
        lines.append("- next up: " + " · ".join(pending))
    return "\n".join(lines).rstrip() + "\n"


def build_digest(runs: list[dict], counts: dict, pending: list[str], report_path: Path) -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    if not runs:
        return (f"Night Shift {day}: no runs in the last 12h. "
                f"See nightshift/logs/night_{day}.log.")
    modes = Counter(r["fm"].get("mode", "execute") for r in runs)
    head = f"Night Shift {day}: {len(runs)} runs (" + ", ".join(f"{n} {m}" for m, n in modes.items()) + ")"
    body = []
    for r in runs:
        fm = r["fm"]
        emoji = STATUS_EMOJI.get(fm.get("status", "?"), "•")
        tag = MODE_TAG.get(fm.get("mode", "execute"), "")
        title = fm.get("title", fm.get("project", r["path"].stem))
        body.append(f"{emoji} {tag} {title}")
        for b in r["bullets"][:2]:
            body.append(f"    · {b}")
    tail = f"queue: {counts.get('projects') or 'empty'}"
    if pending:
        tail += " | next: " + pending[0]
    tail += f"\nfull report: nightshift/{report_path.relative_to(ROOT).as_posix()}"
    return head + "\n" + "\n".join(body) + "\n" + tail


def _notify(title: str, message: str) -> None:
    """Fire a Windows toast via JARVIS's desktop layer (falls back to plyer, then silent)."""
    try:
        sys.path.insert(0, str(ROOT.parent))   # repo root, where desktop.py lives
        import desktop
        # desktop.notify(title, message) — same call the `desktop` tool uses.
        desktop.notify(title, message)         # type: ignore[attr-defined]
        return
    except Exception:
        pass
    try:
        from plyer import notification
        notification.notify(title=title, message=message[:250], timeout=15)
    except Exception:
        pass


def main(argv=None) -> int:
    # The digest contains status emoji; the default Windows console is cp1252 and would crash
    # on them (and so would the scheduled task's log redirect). Force UTF-8 output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")   # type: ignore[attr-defined]
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Night Shift morning report")
    ap.add_argument("--hours", type=float, default=12.0, help="look-back window")
    ap.add_argument("--dry-run", action="store_true", help="print the digest without notifying")
    args = ap.parse_args(argv)

    runs = _collect(args.hours)
    counts, pending = _queue_state()

    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{datetime.now():%Y-%m-%d}.md"
    report_path.write_text(build_report(runs, counts, pending), encoding="utf-8")

    digest = build_digest(runs, counts, pending, report_path)
    print(digest)
    print(f"\n[report written to nightshift/{report_path.relative_to(ROOT).as_posix()}]")

    if not args.dry_run:
        n_done = sum(1 for r in runs if r["fm"].get("status") == "done")
        _notify(f"Night Shift: {n_done} shipped, {len(runs)} runs", digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
