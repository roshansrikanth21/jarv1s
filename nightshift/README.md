# Night Shift

An AI employee that works 10 PM–6 AM: every hour a **fresh** headless Claude Code session
claims one job, works it under a rulebook, logs the result, and the deterministic engine
updates the queue. When the queue runs dry the machine studies the repo, proposes its own
projects, judges them critically, and keeps working. At 6:45 AM one trustworthy report tells
you exactly what shipped while you slept.

Based on Doby Lanete's "The Night Shift" blueprint, adapted for Windows + this repo.

## Why it's built this way (the six rules)
1. **The AI never touches state.** Every status change goes through `queue.py`. The model
   thinks; the script does the bookkeeping. Highest-leverage decision in the whole build.
2. **Fresh session, single job, hard timebox.** One hour, one project, one rulebook. `partial`
   is the release valve — big projects ship as increments instead of overrunning.
3. **Structurally no API billing.** `run_hour.ps1` deletes every API credential before launch,
   so the only possible auth is your Claude **subscription** login. Worst case is a failed run
   and a requeue, never a surprise bill.
4. **Repo-only, send-nothing.** The overnight agent cannot message, deploy, or spend. Its whole
   blast radius is files in this folder, all reviewable in the morning.
5. **Honest statuses, deterministic reporting.** The agent self-reports from a fixed vocabulary,
   the reaper records what actually happened, and `report.py` has no model in it.
6. **Execute-first, curate hard.** Real work always wins the hour. Ideas only get generated when
   there's nothing to ship, and a skeptical second pass promotes few and parks the rest.

## The interface — two markdown files
- **`PROJECTS.md`** — the work queue. Add a project by typing a heading:
  ```
  ## Audit my config keys for anything silently broken
  Read every *.env.example and settings file, cross-check against what the code reads,
  and write findings to nightshift/work/<id>/config-audit.md.
  ```
  The engine adopts `## Some title` into `## NS-00X | pending | Some title` on its next pass.
  **Order = priority** (top runs first).
- **`IDEAS.md`** — the raw idea backlog the curator/ideator manage. You rarely touch this.

## Requirements
- The `claude` CLI, logged in via your **subscription** (not an API key). Auto-detected at
  `~/.local/bin/claude.exe` or npm-global; override with `NS_CLAUDE`.
- Python at `C:\Users\rosha\venv\Scripts\python.exe` (override with `NS_PYTHON`).
- A machine that stays on (or is allowed to wake) overnight.

## ⚠️ Autonomy / permission mode — decide this before the first night
An unattended agent can't answer approval prompts at 2 AM, so it runs in a relaxed permission
mode (`NS_PERM_MODE`):
- **`acceptEdits`** (default) — auto-accepts file edits, but in headless mode it **denies
  un-allowlisted Bash**, so the worker can't call `python queue.py complete` and every run gets
  reaped as *stalled*. Safe, but not functional on its own — use only if you add an
  `--allowedTools` allowlist that includes `Bash`.
- **`bypassPermissions`** — **no approval gate at all**. Fully autonomous (this is what the
  blueprint uses and what makes the queue actually complete), but the agent can edit or run
  anything **inside the repo** without asking. The only guardrails are the rulebook (repo-only,
  send-nothing, no destructive git, no secret values) and the runner clearing your API creds
  (so a run can fail but never bill you).

The runner ships defaulting to the **safer** `acceptEdits`. To get the real autonomous behavior,
set `NS_PERM_MODE=bypassPermissions` **only after** you've read `night_shift.md`, run the trust
test below, and are comfortable with an unattended agent editing files in this repo overnight.
Nothing runs on a schedule until *you* install the tasks and choose the mode.

## The engine (`queue.py`)
```
python queue.py init                         # create empty queue files
python queue.py add "Title" --desc "…" --priority high|medium|low
python queue.py list                         # show queue + backlog
python queue.py claim [--mode m] [--force]   # decide the hour's mode + write the handoff
python queue.py complete done|failed|partial --note "…"
python queue.py reap --agent-exit N          # crash handler (always run after a session)
python queue.py idea-add "Title" --desc "…" --domain ops
python queue.py promote IDEA-001             # curator: idea -> pending project
python queue.py idea-status IDEA-001 parked|rejected --note "…"
```
`claim` signals the hour's mode by **exit code**: `0` execute · `4` curate · `5` ideate ·
`3` skip (a run is still in progress) · `1` error.

## Install the schedule
From an **elevated** PowerShell:
```
powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1
```
Creates `JarvisNightShift` (hourly 10 PM–6 AM, wakes the machine) and `JarvisNightShiftReport`
(6:45 AM). Remove with `install_tasks.ps1 -Remove`.

Env overrides: `NS_REPO`, `NS_PYTHON`, `NS_CLAUDE` (path to the `claude` CLI),
`NS_LOCK_STALE_HOURS`.

## Trust test — do this before the first real night
Prove the state machine survives you trying to break it (Prompt 7 of the blueprint):
```
python queue.py add "Throwaway test" --desc "prove the state machine" --priority high
python queue.py claim                     # EXECUTE, exit 0
# now kill the agent mid-run on purpose (or just:)
python queue.py reap --agent-exit 1       # -> requeued to pending, stub log written
python queue.py claim                      # attempt 2
python queue.py reap --agent-exit 1       # -> failed (two-strikes), NOT pending
python queue.py claim --mode curate --force   # force a curate pass, read the run log
python queue.py claim --mode ideate --force   # force an ideate pass, read the run log
python report.py --dry-run                # read the digest end to end
```
Only after that, let the 10 PM task fire for real — and read the nightly log
(`logs/night_YYYY-MM-DD.log`) the next morning before trusting anything else.

## Files
| file | what it is |
|---|---|
| `queue.py` | deterministic state engine — the only thing that edits queue state |
| `night_shift.md` | worker rulebook (execute mode) |
| `night_shift_curate.md` / `night_shift_ideate.md` | curator / ideator rulebooks |
| `run_hour.ps1` | hourly runner — clears API keys, claims, launches `claude`, always reaps |
| `report.py` | deterministic 6:45 AM report + Windows toast (no LLM in it) |
| `install_tasks.ps1` | registers/removes the two scheduled tasks |
| `PROJECTS.md` / `IDEAS.md` | the queue + idea backlog (committed) |
| `runs/` `reports/` `logs/` `work/` | run logs, dated reports, nightly logs, deliverables (gitignored) |
