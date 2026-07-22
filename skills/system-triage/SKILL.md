---
name: System Triage
description: Diagnose why the machine is slow, hot, or draining battery, and suggest fixes.
---

# System Triage

Use this when the user says their computer is slow, laggy, hot, fan-loud, or the
battery is dying fast.

## Steps

1. **Snapshot the machine.** Call `get_system_info` to read CPU, memory, disk, battery,
   and temperature.
2. **Find the pressure.** Identify which resource is actually constrained:
   - CPU pegged high → a runaway process or too many background tasks.
   - Memory near full → swapping, which makes everything feel slow.
   - Battery low + high energy draw → suggest power-saving steps.
   - High temperature → thermal throttling; airflow/dust or heavy load.
3. **Explain in one line** what's causing the symptom the user reported.
4. **Recommend concrete fixes**, ordered by impact and ease — close specific heavy apps,
   restart a stuck service, plug in, etc. Only suggest a `run_command` action if the user
   asks you to actually do something, and respect the approval flow.

## Rules

- Report the numbers that matter, not a full dump.
- Tie every recommendation back to a specific reading, so it's justified, not generic.
- Don't run destructive commands. Suggest, and let the user confirm.
