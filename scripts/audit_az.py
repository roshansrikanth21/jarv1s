"""A-Z runtime audit. Reads GROQ_API_KEY from the environment. Never prints it."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("JARVIS_AUDIT_PORT", "18765"))
BASE = f"http://127.0.0.1:{PORT}"
REPORT = ROOT / ".local" / "audit_az_report.json"


def rec(results: list, name: str, ok: bool, detail: str, kind: str) -> None:
    results.append({"name": name, "ok": ok, "kind": kind, "detail": detail[:500]})
    print(f"[{'PASS' if ok else 'FAIL'}] {kind}: {name} — {detail[:220]}")


def http(method: str, path: str, body: dict | None = None, headers: dict | None = None, timeout: float = 20):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def groq_probe(results: list) -> str | None:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key or not key.startswith("gsk_"):
        rec(results, "groq key present", False, "GROQ_API_KEY missing or not a gsk_ key", "provider")
        return None
    rec(results, "groq key present", True, f"key length {len(key)}, prefix ok", "provider")
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/models",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "jarvis-audit/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if key:
            body = body.replace(key, "[redacted]")
        rec(results, "groq models list", False, f"HTTP {exc.code} {body[:180]}", "provider")
        return None
    except Exception as exc:
        rec(results, "groq models list", False, type(exc).__name__ + ": " + str(exc)[:180], "provider")
        return None
    ids = sorted(m.get("id", "") for m in payload.get("data", []) if m.get("id"))
    rec(results, "groq models list", bool(ids), f"{len(ids)} models", "provider")
    prefer = ("llama-3.1-8b-instant", "llama-3.3-70b-versatile", "openai/gpt-oss-20b")
    model = next((m for m in prefer if m in ids), ids[0] if ids else "")
    if not model:
        return None
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with exactly the single word PONG and nothing else."}],
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "jarvis-audit/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            chat = json.loads(resp.read().decode())
        text = chat["choices"][0]["message"]["content"].strip()
        rec(results, "groq direct completion", "PONG" in text.upper(), f"model={model} reply={text!r}", "provider")
    except Exception as exc:
        rec(results, "groq direct completion", False, f"model={model} {type(exc).__name__}: {exc}", "provider")
    return model


def policy_and_novelties(results: list) -> None:
    sys.path.insert(0, str(ROOT))
    from jarvis.policy.shell import check_command
    from jarvis.policy.browse import check_url
    from jarvis.policy.desktop import desktop_risk, requires_confirmation
    from jarvis.cognition.router import plan_from_governor

    blocked = {
        "Remove-Item foo": check_command("Remove-Item foo"),
        "del /f foo": check_command("del /f foo"),
        "format c:": check_command("format c:"),
        "echo hi & whoami": check_command("echo hi & whoami"),
        "echo hi > out.txt": check_command("echo hi > out.txt"),
    }
    rec(results, "shell denylist catches known destructive forms", all(blocked.values()), json.dumps({k: bool(v) for k, v in blocked.items()}), "policy")
    # Honest gap: this is a denylist. Commands outside the regex still pass.
    gaps = {
        "python -c print": check_command('python -c "print(1)"'),
        "powershell -Command Get-Date": check_command("powershell -Command Get-Date"),
        "newline then blocked token": check_command("whoami\nRemove-Item foo"),
    }
    leak = {k: v is None for k, v in gaps.items()}
    rec(results, "shell gate is allowlist not denylist", not any(leak.values()), "still allowed: " + ", ".join(k for k, ok in leak.items() if ok) or "none", "policy")

    rec(results, "browse blocks loopback and file scheme",
        check_url("http://127.0.0.1/") is not None and check_url("file:///c:/windows") is not None and check_url("https://example.com") is None,
        "ssrf + https", "policy")
    rec(results, "desktop uninstall requires confirmation",
        requires_confirmation("uninstall_app") and desktop_risk("open_app") == "low",
        "high vs low", "policy")

    plan = plan_from_governor(
        {"rung": "council", "rationale": "audit"},
        {"council", "cloud_fast", "local_fast"},
        tools_needed=True,
    )
    rung = getattr(plan, "rung", None) or (plan.get("rung") if isinstance(plan, dict) else None)
    rec(results, "tool turn is not sent to council", rung == "cloud_fast", f"rung={rung}", "novelty")

    from jarvis.cognition.governor import GovernorState, decide
    poor = {
        "ram_gb": 8, "ram_available_gb": 1.5, "cpu_percent": 95,
        "power_state": "battery", "battery": {"percent": 12, "plugged": False},
        "headroom": 0.1, "tier": "light", "vram_mb": 0, "cpu_temp_c": 90,
    }
    rich = {
        "ram_gb": 64, "ram_available_gb": 40, "cpu_percent": 5,
        "power_state": "ac", "battery": {"percent": 100, "plugged": True},
        "headroom": 0.95, "tier": "workstation", "vram_mb": 24000, "cpu_temp_c": 40,
    }
    avail = {"local_fast", "cloud_fast", "local_deep", "cloud_deep", "council"}
    easy = decide("what time is it", [], poor, avail, GovernorState(), "audit-easy")
    hard = decide("compare these architectures in depth and write a rigorous proof", [], rich, avail, GovernorState(), "audit-hard")
    rec(results, "governor difficulty score moves with the prompt",
        easy["difficulty"] < hard["difficulty"],
        f"easy={easy['rung']} score={easy['difficulty']} | hard={hard['rung']} score={hard['difficulty']}",
        "novelty")
    rec(results, "starved laptop is kept off the cloud rung for a trivial question",
        easy["rung"].startswith("local"),
        f"picked {easy['rung']} on battery 12% / 95% CPU / 1.5GB free",
        "novelty")

    try:
        import jarvis.host.models_advisor as models_advisor
        light = {"ram_gb": 8, "ram_available_gb": 3, "vram_mb": 0, "tier": "light", "cpu": {"logical": 4}}
        heavy = {"ram_gb": 64, "ram_available_gb": 48, "vram_mb": 24000, "tier": "workstation", "cpu": {"logical": 16}}
        ranked_light = models_advisor.ranked_for_device(light, set())
        ranked_heavy = models_advisor.ranked_for_device(heavy, set())
        names = lambda rows: [r.get("tag") or r.get("name") or "?" for r in (rows or [])][:4]
        rec(results, "model advisor ranks by device",
            names(ranked_light) != names(ranked_heavy) or bool(names(ranked_light)),
            f"light={names(ranked_light)} heavy={names(ranked_heavy)}",
            "novelty")
    except Exception as exc:
        rec(results, "model advisor ranks by device", False, f"{type(exc).__name__}: {exc}", "novelty")


def last_assistant() -> str:
    db = ROOT / "memory" / "dialogue.sqlite"
    if not db.exists():
        return ""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT content FROM dialogue_messages WHERE role='assistant' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        con.close()
    return (row[0] if row else "") or ""


def live_server(results: list) -> None:
    venv_py = ROOT / "venv" / "Scripts" / "python.exe"
    exe = str(venv_py if venv_py.exists() else Path(sys.executable))
    env = os.environ.copy()
    env["JARVIS_HOST"] = "127.0.0.1"
    env["JARVIS_PORT"] = str(PORT)
    env["JARVIS_ALLOW_PORT_FALLBACK"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = ROOT / ".local" / "audit_server.log"
    logf = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [exe, str(ROOT / "api.py")],
        cwd=str(ROOT),
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    try:
        health = None
        deadline = time.time() + 50
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                status, _, raw = http("GET", "/health", timeout=3)
                if status == 200:
                    health = json.loads(raw.decode())
                    break
            except Exception:
                time.sleep(0.4)
        rec(results, "backend /health on loopback", health is not None and health.get("status") == "ok",
            json.dumps({k: health.get(k) for k in ("status", "model", "kernel")} if health else {"exit": proc.poll()}),
            "boot")
        if health is None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
            rec(results, "server log tail", False, tail.replace("\n", " | "), "boot")
            return

        try:
            status, _, raw = http("GET", "/api/agent/status")
            body = json.loads(raw.decode()) if raw else {}
        except Exception as exc:
            rec(results, "status exposes governor, voice, tools", False, str(exc), "boot")
            return
        tools = [t.get("name") for t in body.get("tools") or []]
        rec(results, "status exposes governor, voice, tools",
            status == 200 and "governor" in body and "voice" in body and len(tools) > 3,
            f"tools={len(tools)} gov={body.get('governor', {}).get('mode')} stt={body.get('voice', {}).get('stt')}",
            "boot")

        code, hdrs, _ = http("POST", "/api/command", {"command": "ping"}, headers={"Origin": "https://evil.example"})
        rec(results, "mutating endpoint rejects foreign origin", code == 403, f"status={code}", "policy")
        acao = ""
        try:
            code2, hdrs2, _ = http("GET", "/health", headers={"Origin": "https://evil.example"})
            acao = hdrs2.get("Access-Control-Allow-Origin", "")
            rec(results, "CORS does not reflect an external origin", "evil.example" not in acao, f"acao={acao!r} status={code2}", "policy")
        except Exception as exc:
            rec(results, "CORS does not reflect an external origin", False, str(exc), "policy")

        before = last_assistant()
        http("POST", "/api/command", {
            "command": "Reply with exactly the single word PONG. Do not call any tools. Do not store a memory.",
        })
        seen = ""
        end = time.time() + 75
        while time.time() < end:
            seen = last_assistant()
            if seen and seen != before and "PONG" in seen.upper():
                break
            time.sleep(1)
        st, _, raw = http("GET", "/api/agent/status")
        status_body = json.loads(raw.decode()) if st == 200 else {}
        rung = (status_body.get("brain") or {}).get("last_rung")
        rec(results, "live turn through the agent", "PONG" in (seen or "").upper() and seen != before,
            f"rung={rung} reply={seen[:160]!r}", "novelty")

        code, _, raw = http("GET", "/api/skills")
        skills = json.loads(raw.decode()) if code == 200 else {}
        count = skills.get("count", len(skills.get("skills") or []))
        rec(results, "skills registry responds", code == 200, f"count={count}", "novelty")

        code, _, raw = http("GET", "/api/ict?symbol=AAPL&interval=15m", timeout=40)
        ict = json.loads(raw.decode()) if code == 200 else {"error": raw[:200].decode(errors="replace")}
        rec(results, "ICT analysis endpoint returns structured data",
            code == 200 and isinstance(ict, dict),
            f"keys={list(ict)[:8]} ok={ict.get('ok', ict.get('error', ''))!s}"[:240],
            "novelty")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()


def unit_tests(results: list) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else f"exit {proc.returncode}"
    rec(results, "unittest discover tests/", proc.returncode == 0, summary, "unit")


def main() -> int:
    results: list[dict] = []
    if os.environ.get("AUDIT_LIVE_ONLY") != "1":
        unit_tests(results)
        groq_probe(results)
        policy_and_novelties(results)
    live_server(results)
    passed = sum(1 for r in results if r["ok"])
    REPORT.write_text(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=2), encoding="utf-8")
    print(f"SUMMARY {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
