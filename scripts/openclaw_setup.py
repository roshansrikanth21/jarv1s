"""Register JARVIS with a local OpenClaw install.

    python scripts/openclaw_setup.py            # register the MCP server + install the skill
    python scripts/openclaw_setup.py --dry-run  # only print the commands

Does three things with THIS machine's paths filled in:
  1. openclaw mcp add jarvis   — stdio server: <venv python> -m jarvis.agent_mcp
     (OpenClaw probes it before saving, so a bad path fails here, not mid-chat)
  2. openclaw skills install   — integrations/openclaw/jarvis, so OpenClaw's agent
     knows when to hand requests to JARVIS
  3. openclaw mcp reload       — the running gateway picks it up on the next turn

Re-running is safe: an existing `jarvis` server is replaced and the skill overwritten.
Undo with:  openclaw mcp unset jarvis
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "integrations" / "openclaw" / "jarvis"
SERVER = "jarvis"
TIMEOUT_S = 180   # a JARVIS turn with tools can take a while; OpenClaw must wait for it


def _python() -> str:
    """The interpreter that has JARVIS's deps (mcp etc.)."""
    if os.environ.get("JARVIS_PYTHON"):
        return os.environ["JARVIS_PYTHON"]
    for rel in ("venv/Scripts/python.exe", "venv/bin/python"):
        p = ROOT / rel
        if p.exists():
            return str(p)
    return sys.executable


def _run(cmd: list[str], dry: bool, *, check: bool = True) -> int:
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if dry:
        return 0
    rc = subprocess.run(cmd).returncode
    if check and rc != 0:
        raise SystemExit(f"command failed (exit {rc})")
    return rc


def main() -> None:
    dry = "--dry-run" in sys.argv
    oc = shutil.which("openclaw")
    if not oc:
        raise SystemExit("openclaw CLI not found on PATH. Install OpenClaw first: "
                         "npm install -g openclaw")
    if not (SKILL_DIR / "SKILL.md").exists():
        raise SystemExit(f"skill missing: {SKILL_DIR / 'SKILL.md'}")

    py = _python()
    # Replace any previous registration so re-runs pick up moved paths.
    if not dry:
        shown = subprocess.run([oc, "mcp", "show", SERVER],
                               capture_output=True, text=True)
        if shown.returncode == 0 and SERVER in (shown.stdout or ""):
            _run([oc, "mcp", "unset", SERVER], dry, check=False)

    _run([oc, "mcp", "add", SERVER,
          "--command", py,
          "--arg", "-m", "--arg", "jarvis.agent_mcp",
          "--cwd", str(ROOT),
          "--timeout", str(TIMEOUT_S)], dry)
    _run([oc, "skills", "install", str(SKILL_DIR), "--global", "--force"], dry)
    _run([oc, "mcp", "reload"], dry, check=False)

    print("\nDone. JARVIS must be running on this PC (npm run desktop:dev) for the tools "
          "to answer.\nCheck from OpenClaw with:  openclaw mcp probe jarvis")


if __name__ == "__main__":
    main()
