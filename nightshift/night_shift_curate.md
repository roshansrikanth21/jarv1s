# Night Shift — curator rulebook (CURATE mode)

Runs when the work queue is empty but **raw ideas exist**. You are the quality gate of the
whole pipeline. Executing weak projects is **worse** than executing none — it fills the night
with plausible-looking busywork and erodes trust in the morning report. Promote few; park the
rest with reasons.

All the hard rules in `night_shift.md` still apply: repo-only, send nothing, no secrets, never
fabricate, don't ask questions, honest status.

## Do this
1. Read every **raw** idea in `IDEAS.md` and every existing project in `PROJECTS.md` (so you
   don't promote a duplicate).
2. **Score every raw idea on four axes:**
   - **Leverage** — how much it moves the business; compounding beats one-off.
   - **Effort** — genuinely finishable in ~40 minutes.
   - **Fit** — matches current priorities.
   - **Autonomy feasibility** — repo-only, no secrets, no external step. **Failing this one
     disqualifies the idea outright.**
3. **Promote at most 3 winners, best first** (queue order = priority order):
   ```
   python queue.py promote <IDEA-id>
   ```
4. **Park** the good-but-not-now and **reject** the weak/duplicate, each with a one-line note:
   ```
   python queue.py idea-status <IDEA-id> parked   --note "why later"
   python queue.py idea-status <IDEA-id> rejected --note "why not"
   ```
   **Leave nothing un-triaged.** Every raw idea ends this hour as promoted, parked, or rejected.
5. Write the run log (frontmatter with `mode: curate`, then a Summary: how many promoted /
   parked / rejected and the reasoning), then call:
   ```
   python queue.py complete done --note "promoted X, parked Y, rejected Z"
   ```
