---
name: jarvis
description: Use when the user wants something done on their Windows PC or laptop (open/close apps, windows, settings, volume, files, browser, reminders, screen) or asks what JARVIS remembers about them. Hands the request to JARVIS, their personal assistant running on that PC, through the jarvis MCP tools.
---

# JARVIS — the user's PC assistant

JARVIS is a personal assistant running on the user's Windows PC. It controls that machine
and keeps its own long-term memory of the user. You reach it through four MCP tools from
the `jarvis` server.

## When to use it

- The user wants something done **on their PC/laptop**: "open notepad", "turn the volume
  down", "close Chrome", "open my downloads folder", "uninstall Zoom", "remind me at 6pm to
  call Roshan", "what's on my screen", "take a webcam picture".
- The user mentions JARVIS by name: "tell jarvis…", "ask jarvis…".
- You need what JARVIS knows about the user (preferences, projects, people) — use
  `jarvis_recall` first; it is cheaper than a full `jarvis_ask`.

Do NOT use it for things you can answer yourself without the PC (general knowledge,
writing, math) — that just adds latency.

## Tools

- `jarvis_ask(message, speak)` — runs a full JARVIS turn on the PC and returns its reply.
  Pass the user's request in plain words. JARVIS picks its own tools.
  - `speak=false` when the user is away from the PC (they are messaging you from a phone
    or another channel) so JARVIS replies silently. Use `speak=true` only if the user is
    at the PC or explicitly wants JARVIS to say something out loud.
- `jarvis_status()` — is JARVIS running, which brain, how many memories. Call it first if
  a previous call said JARVIS is offline.
- `jarvis_recall(query, k)` — semantic search over JARVIS's memory of the user.
- `jarvis_remember(content, category, importance)` — store one durable fact about the
  user (one standalone sentence; identity, preferences, projects, people, decisions).

## Rules

- Relay JARVIS's reply faithfully. If it says it did something, that is what happened on
  the PC; do not embellish or claim actions it did not report.
- Destructive requests (uninstall, delete, sending messages from the PC) — JARVIS asks for
  confirmation itself. Pass the user's yes/no back as a follow-up `jarvis_ask`.
- If a tool says JARVIS is not running, tell the user it needs to be started on the PC;
  do not retry in a loop.
- Shell commands on the PC may need approval in JARVIS's on-screen banner; if a reply says
  it is waiting for approval, tell the user to approve it at the PC.
