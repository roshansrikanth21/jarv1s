$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv-telegram\Scripts\python.exe"
$bridge = Join-Path $PSScriptRoot "telegram_bot.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Telegram environment is missing. From the repo root run: py -3 -m venv .venv-telegram"
}

$secureBotToken = Read-Host "Telegram bot token (input hidden)" -AsSecureString
$botToken = [System.Net.NetworkCredential]::new("", $secureBotToken).Password
if ([string]::IsNullOrWhiteSpace($botToken)) {
    throw "A Telegram bot token is required."
}

$allowedUserId = Read-Host "Allowed Telegram user ID (leave blank for first-run /id pairing)"
$secureAgentToken = Read-Host "JARVIS_AGENT_TOKEN if set in the running backend (otherwise blank)" -AsSecureString
$agentToken = [System.Net.NetworkCredential]::new("", $secureAgentToken).Password

try {
    $env:TELEGRAM_BOT_TOKEN = $botToken
    $env:TELEGRAM_ALLOWED_USER_IDS = $allowedUserId.Trim()
    $env:JARVIS_AGENT_TOKEN = $agentToken
    & $python $bridge
    if ($LASTEXITCODE -ne 0) {
        throw "Telegram bridge exited with code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:\TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:\TELEGRAM_ALLOWED_USER_IDS -ErrorAction SilentlyContinue
    Remove-Item Env:\JARVIS_AGENT_TOKEN -ErrorAction SilentlyContinue
    Remove-Variable botToken, agentToken, secureBotToken, secureAgentToken -ErrorAction SilentlyContinue
}
