"""Runtime checks for mutating-endpoint + browse SSRF + ICT validation gates.

Usage:
    python scripts/selftest_security_gates.py
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
os.environ.setdefault("JARVIS_LOG_LEVEL", "WARNING")
os.environ.setdefault("JARVIS_FORCE_WORDHASH", "1")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402


async def _noop_dispatch(_text: str) -> None:
    return None


def main() -> int:
    fails: list[str] = []
    api.dispatch_command = _noop_dispatch  # type: ignore[assignment]

    with TestClient(api.app) as client:
        r = client.post(
            "/api/command",
            json={"command": "ping"},
            headers={"Origin": "http://localhost:9999"},
        )
        if r.status_code != 403:
            fails.append(f"H1 expected 403 for :9999 Origin, got {r.status_code}")

        r2 = client.post(
            "/api/command",
            json={"command": "status check only"},
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        if r2.status_code != 200 or r2.json().get("status") != "processing":
            fails.append(f"H1b expected 200 processing for :8080, got {r2.status_code} {r2.text}")

        r3 = client.post("/api/command", json={"command": "noop"})
        if r3.status_code != 200:
            fails.append(f"H1c expected 200 without Origin on loopback, got {r3.status_code}")

        r4 = client.post(
            "/api/settings",
            json={},
            headers={"Origin": "file:///C:/jarvis/index.html"},
        )
        if r4.status_code != 200:
            fails.append(f"H3 expected 200 for file:// Origin, got {r4.status_code}")

        bad = client.get("/api/ict", params={"symbol": "../../etc/passwd"})
        body = bad.json() if bad.status_code == 200 else {}
        if bad.status_code != 200 or body.get("ok") is not False:
            fails.append(f"ICT invalid symbol should return ok:false, got {bad.text[:160]}")

    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "evil.rebinding.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))]
        return real(host, *a, **k)

    socket.getaddrinfo = fake  # type: ignore[assignment]
    try:
        err = api._browse_allowed("http://evil.rebinding.test/")
        if not err or ("private" not in err.lower() and "loopback" not in err.lower()):
            fails.append(f"H2 expected private/loopback block, got {err!r}")
    finally:
        socket.getaddrinfo = real  # type: ignore[assignment]

    out = api.execute_tool("launch_app", {"app": "not-a-real-app-xyz"})
    if "Unknown app" not in str(out):
        fails.append(f"H4 expected Unknown app rejection, got {out!r}")

    if fails:
        print("FAIL:")
        for f in fails:
            print(" ", f)
        return 1
    print("ALL security gate checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
