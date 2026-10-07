"""filesystem.py — verified file/folder operations for the `files` tool.

Multi-step requests like "create a folder called ProjectX, open it, and put a notes file
inside" must not be answered on faith. Every operation here does the thing and then VERIFIES
the real filesystem state before reporting — a create that didn't land, or a delete that
left the file behind, comes back as an honest failure, never a blanket "done". That lets the
agent run such a sequence one step at a time and stop the moment a step genuinely fails,
instead of charging ahead and fabricating success.

Windows-first but cross-platform (pure stdlib). Destructive delete is gated behind an
explicit confirm. `~` and environment vars in paths are expanded; relative paths resolve
against the user's home, not the app's working directory, so "make a folder called X" lands
somewhere the user expects rather than inside the install.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _resolve(path: str) -> Path:
    p = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
    pp = Path(p)
    if not pp.is_absolute():
        pp = Path.home() / pp
    return pp


def create_folder(path: str) -> str:
    if not (path or "").strip():
        return "create_folder: needs a path."
    p = _resolve(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return f"I couldn't create the folder {p} — {exc}"
    if p.is_dir():
        return f"Created folder {p}."
    return f"I tried to create {p} but it isn't there afterward."


def create_file(path: str, content: str = "") -> str:
    if not (path or "").strip():
        return "create_file: needs a path."
    p = _resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or "", encoding="utf-8")
    except Exception as exc:
        return f"I couldn't create the file {p} — {exc}"
    if p.is_file():
        extra = f" ({len(content)} chars)" if content else ""
        return f"Created file {p}{extra}."
    return f"I tried to create {p} but it isn't there afterward."


def open_path(path: str) -> str:
    """Open a file or folder in the OS. Verifies the target exists first (so we don't claim to
    have opened something that isn't there), then delegates to the desktop opener."""
    if not (path or "").strip():
        return "open: needs a path."
    p = _resolve(path)
    if not p.exists():
        return f"There's nothing at {p} to open."
    try:
        from jarvis.act import desktop
        return desktop.open_path(str(p))
    except Exception as exc:
        return f"I couldn't open {p} — {exc}"


def exists(path: str) -> str:
    p = _resolve(path)
    if p.is_dir():
        return f"Yes — {p} is a folder."
    if p.is_file():
        return f"Yes — {p} is a file ({p.stat().st_size} bytes)."
    return f"No — nothing exists at {p}."


def list_dir(path: str) -> str:
    p = _resolve(path or str(Path.home()))
    if not p.is_dir():
        return f"{p} isn't a folder."
    try:
        entries = sorted(os.listdir(p))
    except Exception as exc:
        return f"I couldn't read {p} — {exc}"
    if not entries:
        return f"{p} is empty."
    shown = entries[:50]
    body = "\n".join(f"  {'[dir] ' if (p / e).is_dir() else ''}{e}" for e in shown)
    tail = f"\n  … and {len(entries) - len(shown)} more" if len(entries) > len(shown) else ""
    return f"{p} ({len(entries)} items):\n{body}{tail}"


def delete(path: str, confirm: bool = False) -> str:
    """Delete a file or folder — but only with confirm=True. Without it, a dry-run that says
    exactly what would be removed. Verifies the target is gone afterward."""
    if not (path or "").strip():
        return "delete: needs a path."
    p = _resolve(path)
    if not p.exists():
        return f"Nothing to delete — {p} doesn't exist."
    kind = "folder (and everything in it)" if p.is_dir() else "file"
    if not confirm:
        return (f"This would permanently delete the {kind} at {p}. "
                f"Confirm to proceed (call again with confirm=true).")
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except Exception as exc:
        return f"I couldn't delete {p} — {exc}"
    if p.exists():
        return f"I tried to delete {p} but it's still there."
    return f"Deleted {p}."


def run(action: str, args: dict) -> str:
    """Dispatch a `files` tool call. Never raises — every failure returns a string."""
    act = (action or "").strip().lower()
    if act in ("create_folder", "make_folder", "mkdir"):
        return create_folder(str(args.get("path") or ""))
    if act in ("create_file", "write_file", "new_file"):
        return create_file(str(args.get("path") or ""), str(args.get("content") or ""))
    if act == "open":
        return open_path(str(args.get("path") or ""))
    if act == "exists":
        return exists(str(args.get("path") or ""))
    if act in ("list", "list_dir", "ls"):
        return list_dir(str(args.get("path") or ""))
    if act == "delete":
        return delete(str(args.get("path") or ""), bool(args.get("confirm")))
    return (f"files: unknown action {action!r}. Use create_folder | create_file | open | "
            "exists | list | delete.")
