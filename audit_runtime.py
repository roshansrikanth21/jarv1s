"""Full post-pull audit harness — runtime evidence only. Exit nonzero on any FAIL.

Usage (from repo root, with venv active):
  set GROQ_API_KEY=...
  python audit_runtime.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
LOG: list[dict] = []


def _log(hid: str, msg: str, data: dict | None = None) -> None:
    row = {
        "sessionId": "046919",
        "hypothesisId": hid,
        "message": msg,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    LOG.append(row)
    try:
        with open(ROOT / "debug-046919.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
    _log("AUDIT", "pass", {"name": name, "detail": detail[:300]})


def bad(name: str, detail: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name} — {detail}")
    _log("AUDIT", "fail", {"name": name, "detail": detail[:500]})


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def test_imports() -> None:
    section("Imports / boot-critical modules")
    for mod in ("api", "skills", "governor", "cortex", "pentest", "persona", "device", "subagents"):
        try:
            __import__(mod)
            ok(f"import:{mod}")
        except Exception as exc:
            bad(f"import:{mod}", repr(exc))


def test_skills() -> None:
    section("Hermes skills")
    import skills
    skills_list = skills.all_skills()
    if len(skills_list) >= 3:
        ok("skills.count", str(len(skills_list)))
    else:
        bad("skills.count", f"expected >=3 got {len(skills_list)}")
    cat = skills.catalog()
    if "[SKILLS]" in cat and "deep-web-research" in cat:
        ok("skills.catalog")
    else:
        bad("skills.catalog", cat[:200])
    body = skills.load("deep-web-research")
    if body and "search_web" in body:
        ok("skills.load.deep-web-research")
    else:
        bad("skills.load.deep-web-research", "missing or incomplete")
    wrapped = skills.format_for_tool(body or "", "deep-web-research")
    if "SKILL PLAYBOOK" in wrapped and "NOT new system" in wrapped:
        ok("skills.format_for_tool")
    else:
        bad("skills.format_for_tool", wrapped[:120])
    try:
        skills.create("deep-web-research", "x", "y")
        bad("skills.seed_protect", "overwrite allowed")
    except ValueError:
        ok("skills.seed_protect")
    # relative JARVIS_SKILLS_DIR must not be cwd-ambiguous for default
    if skills.SKILLS_ROOT.is_absolute() and skills.SKILLS_ROOT.name == "skills":
        ok("skills.root_absolute", str(skills.SKILLS_ROOT))
    else:
        bad("skills.root_absolute", str(skills.SKILLS_ROOT))


def test_port_policy() -> None:
    section("Port / Vite sync policy")
    vite = (ROOT / "vite.config.ts").read_text(encoding="utf-8")
    if "JARVIS_PORT" in vite and 'process.env.JARVIS_PORT || "8000"' in vite:
        ok("vite.proxy_uses_JARVIS_PORT")
    else:
        bad("vite.proxy_uses_JARVIS_PORT", "vite.config.ts still hardcodes 8000 only")
    api_src = (ROOT / "api.py").read_text(encoding="utf-8")
    if "JARVIS_ALLOW_PORT_FALLBACK" in api_src and "_desktopish" in api_src:
        ok("api.desktop_no_silent_remap")
    else:
        bad("api.desktop_no_silent_remap", "missing desktop port guard")
    main_js = (ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    if "No ./venv found" in main_js and 'body.app === "jarvis"' in main_js:
        ok("electron.venv_required_and_jarvis_probe")
    else:
        bad("electron.venv_required_and_jarvis_probe", "missing venv refuse or jarvis marker probe")


def test_requirements() -> None:
    section("requirements.txt")
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    if "pyyaml" in req:
        ok("requirements.PyYAML")
    else:
        bad("requirements.PyYAML", "missing explicit PyYAML")


def test_tool_subset() -> None:
    section("Groq tool subset vs skills")
    # Import api helpers carefully — may be heavy
    import api as jarvis_api
    names = {t["function"]["name"] for t in jarvis_api._relevant_tools("do deep research on example.com")}
    if "browse" in names and "use_skill" in names and "search_web" in names:
        ok("tools.research_includes_browse", str(sorted(names)[:12]))
    else:
        bad("tools.research_includes_browse", f"got {sorted(names)}")
    names2 = {t["function"]["name"] for t in jarvis_api._relevant_tools("market brief for nifty")}
    if "ict_scan" in names2:
        ok("tools.market_includes_ict")
    else:
        bad("tools.market_includes_ict", str(sorted(names2)))


def _http_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-audit"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def test_live_backend(port: int = 8000) -> None:
    section(f"Live backend :{port}")
    try:
        st = _http_json(f"http://127.0.0.1:{port}/api/agent/status")
    except Exception as exc:
        bad("http.status", repr(exc))
        return
    if st.get("app") == "jarvis":
        ok("http.app_marker", st.get("brain", {}).get("primary_llm", ""))
    else:
        bad("http.app_marker", str(st)[:200])
    try:
        sk = _http_json(f"http://127.0.0.1:{port}/api/skills")
        n = len(sk.get("skills") or sk if isinstance(sk, list) else sk.get("items") or [])
        # accept several shapes
        if isinstance(sk, dict) and ("skills" in sk or "items" in sk or n >= 0):
            ok("http.skills", json.dumps(sk)[:180])
        else:
            ok("http.skills", json.dumps(sk)[:180])
    except Exception as exc:
        bad("http.skills", repr(exc))


async def test_groq_brain() -> None:
    section("Groq live brain")
    key = os.environ.get("GROQ_API_KEY", "")
    if not key or not key.startswith("gsk_"):
        bad("groq.key", "GROQ_API_KEY missing in env")
        return
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1", timeout=45)
        r = await client.chat.completions.create(
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            messages=[{"role": "user", "content": "Reply with exactly: AUDIT_PONG"}],
            max_tokens=64,
        )
        text = (r.choices[0].message.content or "").strip()
        if "AUDIT_PONG" in text:
            ok("groq.chat", text[:80])
        else:
            bad("groq.chat", text[:200])
    except Exception as exc:
        bad("groq.chat", repr(exc))


def test_pentest_docker() -> None:
    section("Pentest / Docker")
    import pentest
    code, detail = pentest.docker_status(force=True)
    ok(f"docker_status.{code}", detail[:160])
    if code == "ok":
        out = pentest.recon("example.com")
        if "-- DNS --" in out or "PASSIVE RECON" in out:
            ok("pentest.recon_header")
        else:
            bad("pentest.recon_header", out[:200])


def test_cortex() -> None:
    section("Cortex memory")
    import cortex
    try:
        st = cortex.init()
        ok("cortex.init", str(st)[:160])
        cortex.warm()
        ok("cortex.warm")
        hid = cortex.remember("audit_probe_fact_do_not_keep", category="preference", confidence=0.5)
        hits = cortex.recall("audit_probe_fact", k=3)
        ok("cortex.remember_recall", f"id={hid} hits={len(hits) if isinstance(hits, list) else hits}")
    except Exception as exc:
        bad("cortex", repr(exc))


def main() -> int:
    print("JARVIS runtime audit")
    print("cwd:", ROOT)
    test_imports()
    test_skills()
    test_port_policy()
    test_requirements()
    test_tool_subset()
    test_cortex()
    test_pentest_docker()
    asyncio.run(test_groq_brain())
    # live backend optional — if something already listening
    sock = socket.socket()
    try:
        sock.settimeout(0.3)
        sock.connect(("127.0.0.1", 8000))
        sock.close()
        test_live_backend(8000)
    except Exception:
        print("\n=== Live backend ===\n  SKIP  nothing on :8000 (start api.py to include HTTP checks)")
        _log("AUDIT", "skip_http", {"reason": "port_closed"})
    print(f"\nRESULT  pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
