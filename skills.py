"""Hermes-style skills — procedural memory as on-disk skill folders.

Modelled on the Hermes / agentskills.io standard: a *skill* is a directory under the
skills root holding a ``SKILL.md`` with YAML frontmatter (``name``, ``description``)
and a markdown body of step-by-step instructions.

The point is progressive disclosure. The agent's system prompt carries only the skill
NAMES + one-line descriptions (cheap); the full instruction body is pulled on demand
via the ``use_skill`` tool the moment a request matches one. Skills can also be authored
at runtime with :func:`create` — that is how JARVIS grows its own procedural memory
after solving something non-trivial.

No server state lives here; discovery is filesystem-driven and cached with an mtime
signature so edits (or newly created skills) show up within a couple of seconds.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

# Repo-local ./skills by default; override with JARVIS_SKILLS_DIR (e.g. ~/.hermes/skills).
SKILLS_ROOT = Path(os.environ.get("JARVIS_SKILLS_DIR") or (Path(__file__).parent / "skills"))

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "sig": None, "skills": {}}
_TTL = 2.0  # seconds — plus a dir-signature check, so external edits are picked up fast


@dataclass
class Skill:
    name: str
    slug: str
    description: str
    body: str
    path: Path


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _parse(md: str) -> tuple[dict, str]:
    """Split leading YAML frontmatter (--- ... ---) from the markdown body."""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(md[3:end].strip()) or {}
            except Exception:
                meta = {}
            body = md[end + 4:].lstrip("\n")
            if isinstance(meta, dict):
                return meta, body
    return {}, md


def _dir_signature(root: Path) -> tuple:
    if not root.exists():
        return ()
    sig = []
    for p in sorted(root.glob("*/SKILL.md")):
        try:
            sig.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            pass
    return tuple(sig)


def _load_all() -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if not SKILLS_ROOT.exists():
        return out
    for md_path in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        try:
            meta, body = _parse(md_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = str(meta.get("name") or md_path.parent.name).strip()
        slug = _slug(name) or _slug(md_path.parent.name)
        if not slug:
            continue
        out[slug] = Skill(
            name=name,
            slug=slug,
            description=str(meta.get("description") or "").strip(),
            body=body.strip(),
            path=md_path,
        )
    return out


def _refresh() -> dict[str, Skill]:
    now = time.time()
    with _lock:
        sig = _dir_signature(SKILLS_ROOT)
        if _cache["sig"] == sig and (now - _cache["at"]) < _TTL and _cache["skills"] is not None:
            return _cache["skills"]
        skills = _load_all()
        _cache.update(at=now, sig=sig, skills=skills)
        return skills


def all_skills() -> list[Skill]:
    return sorted(_refresh().values(), key=lambda s: s.name.lower())


def get(name: str) -> Skill | None:
    return _refresh().get(_slug(name))


def load(name: str) -> str | None:
    """Full instruction body for a skill, or ``None`` if the name is unknown."""
    s = get(name)
    return s.body if s else None


def catalog(max_skills: int = 50) -> str:
    """Compact ``[SKILLS]`` block for the system prompt — names + descriptions only."""
    skills = all_skills()[:max_skills]
    if not skills:
        return ""
    lines = [
        "[SKILLS] Procedural playbooks available to you. When a request clearly matches one,",
        "call the use_skill tool with its name FIRST to load the full step-by-step instructions,",
        "then follow them — don't guess a skill's steps. Skills you have:",
    ]
    lines += [f"  - {s.slug}: {s.description}" for s in skills]
    return "\n".join(lines)


def create(name: str, description: str, instructions: str) -> Skill:
    """Author (or overwrite) a skill on disk and return it. This is procedural memory:
    call it after working out a repeatable procedure so it can be reused later."""
    slug = _slug(name)
    if not slug:
        raise ValueError("skill name must contain letters or digits")
    if not (instructions or "").strip():
        raise ValueError("skill instructions must not be empty")
    d = SKILLS_ROOT / slug
    d.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump({"name": name.strip(), "description": description.strip()},
                        sort_keys=False).strip()
    (d / "SKILL.md").write_text(
        f"---\n{fm}\n---\n\n{instructions.strip()}\n", encoding="utf-8"
    )
    with _lock:
        _cache["sig"] = None  # force a reload on next access
    s = get(name)
    if s is None:  # pragma: no cover — should always resolve right after writing
        raise RuntimeError("skill was written but could not be reloaded")
    return s
