"""
memory_mcp.py — stdio MCP shim exposing JARVIS's memory to external models.

Claude Desktop / Claude Code / Gemini CLI spawn this as a subprocess; it speaks MCP
on stdin/stdout and forwards every call to the JARVIS backend's HTTP hub endpoints
(/api/memory/remember, /api/memory/recall). It is deliberately stateless and never
touches jarvis_memory.json itself — the JARVIS process is the single writer, so all
decay bookkeeping, category normalisation, and the private-memory filter live in one
place. If the backend is down, tools return "JARVIS is offline" rather than reading
stale data.

Config (Claude Desktop example, claude_desktop_config.json):
    "jarvis-memory": {
      "command": "C:\\Users\\rosha\\venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\rosha\\jarvis\\memory_mcp.py"],
      "env": { "JARVIS_MEMORY_SOURCE": "claude" }
    }
Gemini CLI (~/.gemini/settings.json) uses the same command/args shape with
JARVIS_MEMORY_SOURCE=gemini, so provenance survives per client.

ChatGPT mode:  python memory_mcp.py --http [port]
Runs the same two tools as a streamable-HTTP MCP server on 127.0.0.1:<port>/mcp
(default 8765), guarded by JARVIS_MEMORY_TOKEN as a bearer token. ChatGPT can't
reach localhost, so tunnel it when you want it connected, e.g.:
    cloudflared tunnel --url http://127.0.0.1:8765
then add the printed https URL + /mcp as a ChatGPT custom connector with the token.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Pick up JARVIS_MEMORY_TOKEN etc. from the repo .env (same loader behaviour as api.py:
# existing environment wins, .env fills the gaps).
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

JARVIS_URL = os.environ.get("JARVIS_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("JARVIS_MEMORY_TOKEN", "")
SOURCE = os.environ.get("JARVIS_MEMORY_SOURCE", "external")  # claude | chatgpt | gemini

mcp = FastMCP("jarvis-memory")

OFFLINE = ("JARVIS is offline — memory unavailable. Ask the user to start JARVIS "
           "if they want this remembered/recalled.")


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict | None:
    url = f"{JARVIS_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


@mcp.tool()
def remember(content: str, category: str = "fact", importance: int = 5,
             namespace: str = "personal") -> str:
    """Save a durable fact about the user to their unified memory (shared across
    Claude, ChatGPT, and Gemini). Use for identity, preferences, projects,
    relationships, decisions — not chit-chat. One standalone sentence per call."""
    r = _request("POST", "/api/memory/remember", body={
        "content": content, "category": category, "importance": importance,
        "namespace": namespace, "source_model": SOURCE,
    })
    if r is None:
        return OFFLINE
    return r.get("result") or r.get("error") or "Stored."


@mcp.tool()
def recall(query: str, k: int = 6, namespace: str | None = None) -> str:
    """Search the user's unified memory (shared across Claude, ChatGPT, and Gemini)
    by meaning, not keywords. Call this FIRST when a task might benefit from what
    the user has said before — preferences, projects, past decisions."""
    r = _request("GET", "/api/memory/recall",
                 params={"q": query, "k": k, "namespace": namespace})
    if r is None:
        return OFFLINE
    mems = r.get("memories") or []
    if not mems:
        return "No memories matching that query."
    return "\n".join(
        f"[{m.get('category', 'fact')}|{m.get('source_model', 'jarvis')}] {m.get('content', '')}"
        for m in mems
    )


class _TokenGuard:
    """ASGI wrapper: rejects any request without the bearer token. Sits in front of
    the whole MCP app so unauthenticated traffic never reaches protocol code."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if headers.get("authorization") != f"Bearer {TOKEN}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def _run_http(port: int) -> None:
    if not TOKEN:
        raise SystemExit("Refusing to serve HTTP without JARVIS_MEMORY_TOKEN set — "
                         "this endpoint exposes personal memory. Add it to .env.")
    import uvicorn
    global SOURCE
    SOURCE = os.environ.get("JARVIS_MEMORY_SOURCE", "chatgpt")
    mcp.settings.streamable_http_path = "/mcp"
    uvicorn.run(_TokenGuard(mcp.streamable_http_app()),
                host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    import sys
    if "--http" in sys.argv:
        idx = sys.argv.index("--http")
        port = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 and sys.argv[idx + 1].isdigit() else 8765
        _run_http(port)
    else:
        mcp.run()  # stdio transport
