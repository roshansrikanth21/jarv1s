"""Static + policy self-test for WS reconnect reliability (non-security audit).

Asserts the shared hook no longer hard-stops after N attempts, and that
visibility/online kick + status.ok gates are present in source.

Usage:
    python scripts/selftest_reconnect_policy.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
HOOK = _REPO / "src" / "hooks" / "useJarvisSocket.ts"
BOUNDARY = _REPO / "src" / "components" / "jarvis" / "DeckErrorBoundary.tsx"
PKG = _REPO / "package.json"


def main() -> int:
    fails: list[str] = []
    src = HOOK.read_text(encoding="utf-8")

    if re.search(r"attempt\s*>=\s*12", src):
        fails.append("H5: hard stop `attempt >= 12` still present")

    if "visibilitychange" not in src or 'addEventListener("online"' not in src:
        fails.append("H5: missing visibility/online reconnect kick")

    if "Math.min(attempt, 5)" not in src:
        fails.append("H5: expected capped exponential backoff (Math.min(attempt, 5))")

    if "if (!r.ok)" not in src:
        fails.append("H6: /api/agent/status fetch lacks response.ok guard")

    if not BOUNDARY.is_file():
        fails.append("H7: DeckErrorBoundary.tsx missing")
    else:
        prime = (_REPO / "src" / "decks" / "prime.tsx").read_text(encoding="utf-8")
        if "DeckErrorBoundary" not in prime:
            fails.append("H7: prime.tsx does not wrap orb in DeckErrorBoundary")

    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    name = pkg.get("name", "")
    if name == "tanstack_start_ts":
        fails.append("DX: package.json name still tanstack_start_ts")

    scripts = pkg.get("scripts") or {}
    if "typecheck" not in scripts:
        fails.append("DX: missing npm run typecheck script")
    if "test:selftest" not in scripts and "selftest" not in scripts:
        fails.append("DX: missing npm selftest script")

    if fails:
        print("FAIL:")
        for f in fails:
            print(" ", f)
        return 1
    print("ALL reconnect/reliability/DX policy checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
