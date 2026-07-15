<#
  Night Shift - hourly runner (Windows / PowerShell).

  Task Scheduler calls this every hour, 10 PM..6 AM. It:
    1. Sets paths (repo, nightly log, the `claude` CLI).
    2. FORCE-CLEARS every API-key variable Claude Code could authenticate with, so the CLI can
       ONLY use your logged-in subscription. If that login ever expires the run fails fast and
       requeues - it is structurally impossible to silently bill metered tokens.
    3. Runs `queue.py claim` and branches on its exit code (0 execute / 4 curate / 5 ideate /
       3 skip / 1 error).
    4. On a real claim, launches a FRESH headless `claude` session with the matching rulebook.
    5. ALWAYS runs `queue.py reap` afterward with the agent's exit code, so a crashed or
       stalled session is recorded and released and the next hour starts clean.
    6. Appends everything to one nightly log.

  Nothing here is JARVIS-specific except the default paths - point $Repo elsewhere to run the
  Night Shift over any folder of plain files.
#>

$ErrorActionPreference = 'Continue'

# -- 1. paths ----------------------------------------------------------------------
$Repo    = if ($env:NS_REPO)  { $env:NS_REPO }  else { Split-Path -Parent $PSScriptRoot }  # jarvis repo root
$NsDir   = Join-Path $Repo 'nightshift'
$Python  = if ($env:NS_PYTHON) { $env:NS_PYTHON } else { 'C:\Users\rosha\venv\Scripts\python.exe' }
# Find the claude CLI: NS_CLAUDE override, then PATH, then the known install locations. Task
# Scheduler runs with a minimal PATH, so an explicit fallback is what makes this fire unattended.
$Claude = $env:NS_CLAUDE
if (-not $Claude) { $Claude = (Get-Command claude -ErrorAction SilentlyContinue).Source }
if (-not $Claude) {
  $Claude = @(
    "$env:USERPROFILE\.local\bin\claude.exe",
    "$env:APPDATA\npm\claude.cmd"
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
$PermMode = if ($env:NS_PERM_MODE) { $env:NS_PERM_MODE } else { 'acceptEdits' }  # see note at launch
$LogDir  = Join-Path $NsDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Nightly = Join-Path $LogDir ("night_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

function Log($msg) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg
  Add-Content -Path $Nightly -Value $line -Encoding utf8
  Write-Host $line
}

Log "-------- hourly run start --------"

if (-not $Claude) {
  Log "ERROR: 'claude' CLI not found on PATH and NS_CLAUDE not set. Aborting."
  exit 1
}

# -- 2. structurally no API billing -------------------------------------------------
# Clear every credential the CLI could use to authenticate via a metered API instead of the
# subscription login. A misconfigured session then has NO path to a surprise bill.
$killVars = @(
  'ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL',
  'CLAUDE_API_KEY','CLAUDE_CODE_OAUTH_TOKEN',
  'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','CLAUDE_CODE_USE_BEDROCK',
  'GOOGLE_APPLICATION_CREDENTIALS','CLAUDE_CODE_USE_VERTEX'
)
foreach ($v in $killVars) { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
Log "cleared API credentials - subscription-auth only"

# -- 3. claim ------------------------------------------------------------------------
Push-Location $NsDir
& $Python queue.py claim *>> $Nightly
$claimExit = $LASTEXITCODE
Pop-Location
Log "claim exit=$claimExit"

# map exit code -> mode + rulebook
$mode = $null; $rulebook = $null
switch ($claimExit) {
  0 { $mode = 'execute'; $rulebook = 'night_shift.md' }
  4 { $mode = 'curate';  $rulebook = 'night_shift_curate.md' }
  5 { $mode = 'ideate';  $rulebook = 'night_shift_ideate.md' }
  3 { Log "SKIP - a run is still in progress. Nothing to do."; Log "-------- hourly run end --------"; exit 0 }
  default { Log "claim returned $claimExit (error/no-op). Not launching agent."; Log "-------- hourly run end --------"; exit 0 }
}
Log "mode=$mode rulebook=$rulebook"

# -- 4. launch a fresh headless agent ------------------------------------------------
$prompt = @"
You are the overnight Night Shift worker. Read current.json in this folder for the claimed work,
then follow $rulebook in this folder EXACTLY. Complete the work fully autonomously. Write the run
log to the exact path given in current.json. Finish by calling the queue engine's complete command
with an honest status (a run that produced nothing usable is failed, never done).
Hard rules: work only inside this repository; never send anything external (no messages, email,
deploys, posts, PRs, or pushes); never run destructive git commands; never print, copy, or move
secret values from .env; never fabricate; do not ask questions - make the reasonable choice, record
the assumption in the run log, and keep moving.
"@

Push-Location $NsDir
Log "launching claude ($mode) [$PermMode]..."
# Headless print mode. Working dir = nightshift/ (blast radius = repo).
#
# PERMISSION MODE - read this before the first real night.
#   An unattended agent can't answer approval prompts at 2 AM, so it needs a relaxed mode:
#     bypassPermissions  - no approval gate at all. Fully autonomous (what the blueprint uses),
#                          but the agent can edit/run anything in the repo without asking. The
#                          rulebook (repo-only, send-nothing, no destructive git, no secrets) is
#                          the ONLY guardrail, plus the cleared API creds (no metered billing).
#     acceptEdits        - auto-accepts file edits but denies un-allowlisted Bash in headless
#                          mode - which means the worker CAN'T call `python queue.py complete`
#                          and every run gets reaped as stalled. Use only with an -allowedTools
#                          allowlist that includes Bash.
#   Default is acceptEdits (safer). Set NS_PERM_MODE=bypassPermissions to run truly autonomous,
#   AFTER you've read the rulebook and run the trust test and are comfortable with the tradeoff.
& $Claude -p $prompt --permission-mode $PermMode *>> $Nightly
$agentExit = $LASTEXITCODE
Pop-Location
Log "agent exit=$agentExit"

# -- 5. ALWAYS reap ------------------------------------------------------------------
Push-Location $NsDir
& $Python queue.py reap --agent-exit $agentExit *>> $Nightly
Pop-Location
Log "reaped. -------- hourly run end --------"
exit 0
