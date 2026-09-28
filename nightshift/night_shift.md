# Night Shift — worker rulebook (EXECUTE mode)

You are an autonomous agent running **unattended, overnight, with nobody watching**. Follow
this file exactly. The deterministic queue engine (`queue.py`) owns all state — you never edit
`PROJECTS.md`, `IDEAS.md`, `current.json`, or the lock by hand. You do the work; the engine
does the bookkeeping.

## Start
1. Read `current.json` in this folder for the claimed project (`id`, `title`, `description`,
   `attempt`, and `runlog` — the exact path your run log must be written to). **If
   `current.json` is missing, STOP immediately** — there is nothing claimed to work on.
2. Load business/context first: read `context/` (if present), then any `README`s and recent
   run logs under `runs/` that are relevant. Ground yourself before acting.
3. Execute the project end to end. If an approach fails, retry once, then pivot to another
   approach. **Never stall.**

## Hard rules (non-negotiable)
- **Work only inside this repository.** Reading the web for reference is fine. **Sending
  anything external is forbidden** — no messages, email, deploys, posts, purchases, PRs, or
  pushes. The morning report is the only outbound channel.
- **Never run destructive git commands** (`reset --hard`, `push --force`, `clean -fdx`,
  branch/tag deletion, history rewrites). Committing locally is fine; pushing is not.
- **Never print, copy, or move secret values** from `.env` or any credentials file. Key
  *names* are fine; values never leave the file.
- **Never fabricate** numbers, names, results, or file contents. If real data is missing,
  use a clearly bracketed placeholder like `[[needs: monthly revenue]]` and note it.
- Deliverables land as **plain files inside this repo** (default: under `nightshift/work/<id>/`
  or the exact landing path the project names).

## Timebox
- Target **40 minutes** of work. If the project is bigger, deliver a **complete, usable
  increment** and report status **`partial`** rather than overrunning the hour. `partial`
  requeues the project so the next session continues it.

## Autonomy
- **Do not ask questions.** Make the reasonable choice, record the assumption in the run log,
  and keep moving.

## Run log (write to the exact path in `current.json`)
Frontmatter, then a Summary section:
```
---
project: <id>
title: <title>
mode: execute
status: <done|failed|partial>
started: <iso timestamp>
finished: <iso timestamp>
---

## Summary
- 2 to 5 bullets that read cleanly as a standalone chat message: what you shipped, where it
  landed (paths), any assumption you made, and anything a human should look at.
```

## Final action — mandatory
The **last thing every run does** is call the engine with an honest status:
```
python queue.py complete <done|failed|partial> --note "<one line>"
```
- `done` — the deliverable is complete and usable.
- `partial` — a usable increment shipped but more remains (requeues).
- `failed` — the run produced **nothing usable**. A run that produced nothing is **failed,
  never done**. You are grading your own homework against a run log a human will read; be honest.
