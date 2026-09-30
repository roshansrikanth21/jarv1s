"""Shell command policy — allow only simple, non-destructive single commands.

Returns None when allowed, otherwise a human-readable denial reason.
Centralized so browse/desktop/pentest policies can grow beside this without
living inside api.py.
"""
from __future__ import annotations

import re

_CMD_BLOCK_RE = re.compile(
    r"(?:^|\s)(?:rm\s+-rf|del(?:ete)?\s+|erase\s+|remove-item\b|"
    r"format\s+|shutdown|reboot|mkfs|diskpart|"
    r"reg\s+delete|curl\s+.+\|\s*(?:ba)?sh|(?:powershell|pwsh)\s+-(?:e|enc|encodedcommand)\b|"
    r"invoke-expression|iex\s|wget\s+.+\|\s*sh|"
    r"(?:python|python3|py)\s+-c\b|node\s+-e\b)",
    re.I,
)
_CMD_META_RE = re.compile(r"[;&|`>\n\r]|(?:\$\()")

MAX_COMMAND_LEN = 500


def check_command(cmd: str) -> str | None:
    """Return None if ``cmd`` may run; else a denial string for the tool result."""
    if cmd is None:
        return "run_command needs a non-empty command string."
    c = str(cmd).strip()
    if not c:
        return "run_command needs a non-empty command string."
    if _CMD_BLOCK_RE.search(c):
        return "Command blocked for safety."
    if _CMD_META_RE.search(c):
        return "Shell chaining and redirection are blocked — one simple command only."
    if len(c) > MAX_COMMAND_LEN:
        return f"Command too long ({MAX_COMMAND_LEN} char max)."
    return None
