"""Offline self-test for the browse-tool action compiler in api.py.

Verifies that every op in `_bh_build_script`:
  1. Produces a well-formed browser-harness script string.
  2. Rejects malformed inputs with a clear error (no silent failure).
  3. Safely quotes model-supplied values — an injection string like '"; alert(1);//'
     is embedded as a JS/JSON string literal and cannot escape into executable code.

No Chrome, no network, no keys required.

Usage:
    python scripts/selftest_browse.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# api.py touches modules that need a working env; force safe defaults.
os.environ.setdefault("JARVIS_LOG_LEVEL", "WARNING")
os.environ.setdefault("JARVIS_FORCE_WORDHASH", "1")

from api import _bh_build_script, _WEB_APPS   # noqa: E402


_checks: list[tuple[str, bool, str]] = []


def ok(label: str, cond: bool, hint: str = "") -> None:
    _checks.append((label, bool(cond), hint))
    mark = "ok  " if cond else "FAIL"
    print(f"  {mark} {label}" + (f"   -> {hint}" if not cond and hint else ""))


def section(name: str) -> None:
    print(name)


def compile_ok(actions: list) -> str:
    """Compile and assert success; return the script text."""
    script, err = _bh_build_script(actions)
    assert err is None, f"expected success, got error: {err}"
    assert script is not None
    return script


def compile_err(actions: list) -> str:
    """Compile and assert error; return the error string."""
    script, err = _bh_build_script(actions)
    assert script is None, f"expected error, got script: {script[:120]}"
    return err or ""


def run() -> int:
    section("backward-compat — existing ops still compile")
    s = compile_ok([{"op": "navigate", "url": "https://example.com"},
                    {"op": "read", "selector": "h1"}])
    ok("navigate + read produce a harness snippet", "goto_url" not in s and "new_tab" in s)
    ok("wait_for_load emitted after navigate", "wait_for_load()" in s)
    ok("import time appears in header (needed for wait_ms)", "import time" in s)

    section("open_app — smart URL shortcuts")
    s = compile_ok([{"op": "open_app", "app": "whatsapp"}])
    ok("whatsapp resolves to web.whatsapp.com URL",
       "web.whatsapp.com" in s, s[:200])
    s = compile_ok([{"op": "open_app", "app": "spotify"}])
    ok("spotify resolves to open.spotify.com", "open.spotify.com" in s)
    err = compile_err([{"op": "open_app", "app": "nonexistent-app-xyz"}])
    ok("unknown app returns a helpful error listing supported apps",
       "unknown app" in err and "whatsapp" in err, err)
    ok("open_app dict covers the 5 apps we advertise",
       all(a in _WEB_APPS for a in ("whatsapp", "slack", "spotify", "gmail", "youtube")))

    section("find_text — click/type/read by visible label")
    s = compile_ok([{"op": "find_text", "action": "click", "text": "Send"}])
    ok("click compiles", "find_text" in s and "clicked" in s)
    ok("find_text prints a header line before the JS", '"[find_text #0]' in s)
    s = compile_ok([{"op": "find_text", "action": "type",
                     "text": "Type a message", "text2": "hey"}])
    ok("type flavour compiles", "typed" in s)
    err = compile_err([{"op": "find_text", "action": "type", "text": "box"}])
    ok("type without text2/value errors clearly",
       "text2" in err or "value" in err, err)
    err = compile_err([{"op": "find_text", "action": "hover", "text": "x"}])
    ok("unknown action inside find_text errors clearly",
       "click|type|read" in err, err)
    err = compile_err([{"op": "find_text", "action": "click"}])
    ok("missing text errors clearly", "'text'" in err, err)

    section("find_text — placeholders in the JS template are ALL substituted")
    s = compile_ok([{"op": "find_text", "action": "click", "text": "Send"}])
    for placeholder in ("__TARGET__", "__ACTION__", "__TYPE_TEXT__", "__ROLE__"):
        ok(f"{placeholder} substituted (not leaked into script)", placeholder not in s,
           f"still in script — template leak")

    section("wait_for_text + wait_ms — timing primitives")
    s = compile_ok([{"op": "wait_for_text", "text": "Chats", "timeout_ms": 3000}])
    ok("wait_for_text emits a poll loop", "for _ in range" in s and "time.sleep" in s)
    ok("wait_for_text hits 'found' branch", "if _hit == 'found': break" in s)
    err = compile_err([{"op": "wait_for_text", "text": "x", "timeout_ms": "soon"}])
    ok("non-integer timeout errors", "integer" in err, err)
    s = compile_ok([{"op": "wait_ms", "ms": 500}])
    ok("wait_ms emits time.sleep(0.5)", "time.sleep(0.5)" in s)
    s = compile_ok([{"op": "wait_ms", "ms": 999999}])
    ok("wait_ms clamps big values to 15s ceiling", "time.sleep(15.0)" in s)

    section("safety — model-supplied strings can't escape their JS/JSON quoting")
    injection = '"; window.__pwn = 1; //'
    s = compile_ok([{"op": "find_text", "action": "click", "text": injection}])
    # The injection string must appear only inside a JSON-encoded string literal —
    # every " inside the payload should be escaped as \".
    escaped = injection.replace("\\", "\\\\").replace('"', '\\"')
    ok("injection payload is JSON-escaped inside the script",
       escaped in s, "raw payload leaked unquoted")
    ok("no literal JS assignment 'window.__pwn = 1' outside a quoted string",
       "window.__pwn = 1" not in s.replace(escaped, ""),
       "payload escaped its quoting")

    section("guardrails — allowlist / SSRF gates still apply through open_app")
    err = compile_err([{"op": "open_app", "app": "", "url": "http://localhost:8000"}])
    ok("localhost URL still blocked via open_app path", "localhost" in err.lower(), err)
    err = compile_err([{"op": "open_app", "app": "", "url": "file:///etc/passwd"}])
    ok("file:// scheme still blocked via open_app path", "scheme" in err.lower(), err)

    section("meta — length + shape checks unchanged")
    err = compile_err([])
    ok("empty actions still errors", "non-empty" in err, err)
    err = compile_err([{"op": "wait_ms", "ms": 1}] * 21)
    ok("more than 20 actions still errors", "20 max" in err, err)
    err = compile_err([{"op": "totally_made_up"}])
    ok("unknown op still errors clearly", "unknown op" in err, err)

    passed = sum(1 for _, c, _ in _checks if c)
    total = len(_checks)
    print()
    if passed == total:
        print(f"ALL {total} CHECKS PASSED")
        return 0
    for label, cond, hint in _checks:
        if not cond:
            print(f"  FAIL: {label}   ({hint})")
    print(f"\n{passed}/{total} passed")
    return 1


if __name__ == "__main__":
    sys.exit(run())
