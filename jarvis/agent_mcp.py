"""
agent_mcp.py — MCP server that makes JARVIS reachable from OpenClaw (or any MCP client).

OpenClaw spawns this over stdio (or connects over streamable HTTP) and gets four tools:

    jarvis_ask       run a full JARVIS turn on this PC and get the reply back
    jarvis_status    is JARVIS up, which brain, how many memories
    jarvis_recall    semantic search over JARVIS's long-term memory
    jarvis_remember  store a durable fact in JARVIS's memory

Like memory_mcp.py this is a stateless shim: every call forwards to the running JARVIS
backend over loopback HTTP (/api/ask, /health, /api/memory/*). The backend stays the only
process that touches memory, tools, or the desktop. If JARVIS isn't running, tools say so
instead of failing.

Kept separate from memory_mcp.py on purpose: that server is memory-only and is wired into
Claude / ChatGPT / Gemini. jarvis_ask can drive the whole machine (apps, shell, browser),
so it only reaches clients you register it with explicitly.

stdio — OpenClaw on this machine (scripts/openclaw_setup.py fills in the paths):
    openclaw mcp add jarvis --command <repo>/venv/Scripts/python.exe \
        --arg -m --arg jarvis.agent_mcp --cwd <repo> --timeout 180

HTTP — OpenClaw on another machine or inside WSL:
    python -m jarvis.agent_mcp --http [port] [--host 0.0.0.0]
Serves streamable HTTP at http://<host>:<port>/mcp (default 127.0.0.1:8766). Refuses to
start without JARVIS_MCP_TOKEN; clients must send "Authorization: Bearer <token>".

Env (read from the repo .env too; real environment wins):
    JARVIS_URL           backend base URL   (default http://127.0.0.1:$JARVIS_PORT or :8000)
    JARVIS_AGENT_TOKEN   bearer for /api/ask, if the backend requires one
    JARVIS_MEMORY_TOKEN  bearer for /api/memory/*, if the backend requires one
    JARVIS_MCP_TOKEN     bearer this server demands in --http mode
    JARVIS_ASK_TIMEOUT   seconds to wait for a reply (default 120)
"""
from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

from jarvis.paths import ROOT


def _load_env() -> None:
    """Fill gaps from <repo>/.env (same rules as api.py: real env wins, quotes stripped,
    trailing ' # comment' on unquoted values ignored)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            for marker in (" #", "\t#"):
                if marker in val:
                    val = val[:val.index(marker)].rstrip()
        os.environ.setdefault(key.strip(), val)


_load_env()

JARVIS_URL = (os.environ.get("JARVIS_URL")
              or f"http://127.0.0.1:{os.environ.get('JARVIS_PORT', '8000')}").rstrip("/")
AGENT_TOKEN = os.environ.get("JARVIS_AGENT_TOKEN", "")
MEMORY_TOKEN = os.environ.get("JARVIS_MEMORY_TOKEN", "")
MCP_TOKEN = os.environ.get("JARVIS_MCP_TOKEN", "")
ASK_TIMEOUT = float(os.environ.get("JARVIS_ASK_TIMEOUT", "120") or 120)
SOURCE = "openclaw"

mcp = FastMCP("jarvis")

OFFLINE = ("JARVIS is not running on the PC. Ask the user to start it "
           "(npm run desktop:dev, or python api.py) and try again.")


def _call(method: str, path: str, *, token: str = "", params: dict | None = None,
          body: dict | None = None, timeout: float = 15.0) -> tuple[int, dict | None]:
    """HTTP to the JARVIS backend. Returns (status, json). status 0 means unreachable."""
    url = f"{JARVIS_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "ignore") or "{}")
        except Exception:
            return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return 0, None


def _refused(code: int, token_var: str) -> str | None:
    if code == 0:
        return OFFLINE
    if code in (401, 403):
        return (f"JARVIS refused the request ({code}). Set {token_var} to the same value "
                "in JARVIS's .env and in this bridge's environment.")
    return None


@mcp.tool()
def jarvis_ask(message: str, speak: bool = True) -> str:
    """Ask JARVIS — the user's personal assistant running on their Windows PC — to do or
    answer something ON THAT PC. JARVIS runs the request with its own tools: open or close
    apps and windows, Windows settings, volume / brightness / Wi-Fi, files and folders,
    browser automation, OS reminders, web search, screenshots and webcam, and its
    long-term memory of the user. Returns JARVIS's reply.

    Pass the user's request in plain words, e.g. "open notepad", "what's on my screen",
    "remind me at 6pm to call Roshan". Set speak=false when the user is away from the PC
    (e.g. messaging from their phone) so JARVIS answers silently instead of out loud."""
    code, r = _call("POST", "/api/ask", token=AGENT_TOKEN,
                    body={"message": message, "speak": bool(speak),
                          "timeout_s": ASK_TIMEOUT},
                    timeout=ASK_TIMEOUT + 15)
    refused = _refused(code, "JARVIS_AGENT_TOKEN")
    if refused:
        return refused
    if code != 200 or not isinstance(r, dict):
        return f"JARVIS returned an error ({code}): {(r or {}).get('error', 'unknown')}"
    reply = r.get("reply") or "(JARVIS finished without saying anything.)"
    notes = []
    if r.get("tools"):
        notes.append("tools used: " + ", ".join(r["tools"]))
    if r.get("status") == "timeout":
        notes.append(f"still working after {ASK_TIMEOUT:.0f}s — reply may be partial")
    elif r.get("status") == "interrupted":
        notes.append("interrupted — a newer command on the PC took over")
    return reply + (f"\n\n[{'; '.join(notes)}]" if notes else "")


@mcp.tool()
def jarvis_status() -> str:
    """Check whether JARVIS is running on the user's PC, which brain it is using, and how
    many long-term memories it holds. Cheap — call it before jarvis_ask if unsure."""
    code, r = _call("GET", "/health")
    if code == 0 or not isinstance(r, dict):
        return OFFLINE
    return (f"JARVIS is online · brain: {r.get('model', '?')} · "
            f"memories: {r.get('memories', '?')} · as of {r.get('time', '?')}")


@mcp.tool()
def jarvis_recall(query: str, k: int = 6) -> str:
    """Search JARVIS's long-term memory of the user by meaning (preferences, projects,
    people, past decisions). Faster and cheaper than jarvis_ask for "what does JARVIS know
    about X". Private memories are never returned."""
    code, r = _call("GET", "/api/memory/recall", token=MEMORY_TOKEN,
                    params={"q": query, "k": max(1, min(int(k), 20))})
    refused = _refused(code, "JARVIS_MEMORY_TOKEN")
    if refused:
        return refused
    mems = (r or {}).get("memories") or []
    if not mems:
        return "JARVIS has no memories matching that."
    return "\n".join(f"[{m.get('category', 'fact')}] {m.get('content', '')}" for m in mems)


@mcp.tool()
def jarvis_remember(content: str, category: str = "fact", importance: int = 5) -> str:
    """Store one durable fact about the user in JARVIS's long-term memory, so JARVIS knows
    it next time (identity, preferences, projects, people, decisions — not chit-chat).
    One standalone sentence per call."""
    code, r = _call("POST", "/api/memory/remember", token=MEMORY_TOKEN,
                    body={"content": content, "category": category,
                          "importance": importance, "source_model": SOURCE})
    refused = _refused(code, "JARVIS_MEMORY_TOKEN")
    if refused:
        return refused
    return (r or {}).get("result") or (r or {}).get("error") or "Stored."


class _BearerGuard:
    """ASGI wrapper: rejects any HTTP request without the bearer token before it reaches
    MCP protocol code."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def _run_http(host: str, port: int) -> None:
    if not MCP_TOKEN:
        raise SystemExit("Refusing to serve HTTP without JARVIS_MCP_TOKEN — jarvis_ask can "
                         "drive this PC. Add JARVIS_MCP_TOKEN=<long random string> to .env.")
    import uvicorn
    mcp.settings.streamable_http_path = "/mcp"
    uvicorn.run(_BearerGuard(mcp.streamable_http_app(), MCP_TOKEN),
                host=host, port=port, log_level="warning")


def main(argv: list[str]) -> None:
    if "--http" in argv:
        i = argv.index("--http")
        port = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 8766
        host = os.environ.get("JARVIS_MCP_HOST", "127.0.0.1")
        if "--host" in argv and len(argv) > argv.index("--host") + 1:
            host = argv[argv.index("--host") + 1]
        _run_http(host, port)
    else:
        mcp.run()   # stdio


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
