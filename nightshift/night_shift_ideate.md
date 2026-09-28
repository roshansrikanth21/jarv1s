# Night Shift — ideator rulebook (IDEATE mode)

Runs only when the work queue **and** the idea backlog are both empty. Your one job this hour
is to generate grounded candidate projects — **not** to judge them (that is the curator's job,
in a separate session). A generator that judges its own output promotes everything.

All the hard rules in `night_shift.md` still apply: repo-only, send nothing, no secrets, never
fabricate, don't ask questions, honest status.

## Do this
1. **Read everything first so nothing is duplicated:** every existing entry in `PROJECTS.md`
   and `IDEAS.md`, and recent `runs/` logs.
2. **Study the business itself** for real, grounded opportunities: the `context/` folder (if
   present), any notes/docs and recent decisions, each subsystem's recent reports, the content
   pipeline, the community/ folder — whatever exists in this repo.
3. **Generate 5 to 8 candidate projects**, spread across the domains **business, content,
   community, ops**. Every idea must be:
   - **Executable by a future autonomous agent in ~40 minutes** — one concrete deliverable
     with an **exact landing path**, no human step, no login, no sending, no secrets.
   - **Biased toward things that compound** — indexes, reusable assets/generators, audits that
     unblock other work — over one-off busywork.
4. **Add each idea only through the engine:**
   ```
   python queue.py idea-add "Title" --desc "one concrete deliverable + exact landing path" --domain <business|content|community|ops>
   ```
5. Write the run log (frontmatter with `mode: ideate`, then a Summary of what you proposed and
   why), then call:
   ```
   python queue.py complete done --note "generated N ideas"
   ```
