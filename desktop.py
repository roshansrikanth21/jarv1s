"""desktop.py — thin Windows-OS control surface for the `desktop` tool.

Wraps native Windows CLIs / URIs — nothing exotic, no UI automation:
  - Explorer:      explorer.exe <path>
  - Settings:      start ms-settings:<page>
  - Control Panel: control <applet.cpl>
  - Registry:      regedit (optionally pre-navigated to a key)
  - Components:    taskmgr, devmgmt.msc, services.msc, …
  - Packages:      winget list / winget uninstall

Design goals:
  - No shell=True anywhere; every subprocess is an argv list, so nothing model-supplied
    can be interpreted as a shell metacharacter.
  - Destructive actions (uninstall) require an explicit confirm=True; without it the tool
    returns a dry-run showing exactly what would happen. Prevents "oops the model set it".
  - Windows-only. On any other OS every action returns a clear "not supported" message —
    never raises, never partially executes.
  - Zero pip deps. Uses stdlib only (subprocess, os, sys, shutil, pathlib).
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")

# Recognized ms-settings URIs. Not exhaustive — Windows adds pages every release —
# so unknown pages fall through as-is if they look safe (alphanumeric + dash).
SETTINGS_PAGES: dict[str, str] = {
    "apps":             "ms-settings:appsfeatures",
    "appsfeatures":     "ms-settings:appsfeatures",
    "installed_apps":   "ms-settings:appsfeatures",
    "display":          "ms-settings:display",
    "sound":            "ms-settings:sound",
    "network":          "ms-settings:network",
    "wifi":             "ms-settings:network-wifi",
    "bluetooth":        "ms-settings:bluetooth",
    "personalization":  "ms-settings:personalization",
    "background":       "ms-settings:personalization-background",
    "themes":           "ms-settings:themes",
    "privacy":          "ms-settings:privacy",
    "notifications":    "ms-settings:notifications",
    "startup":          "ms-settings:startupapps",
    "startupapps":      "ms-settings:startupapps",
    "defaultapps":      "ms-settings:defaultapps",
    "power":            "ms-settings:powersleep",
    "powersleep":       "ms-settings:powersleep",
    "storage":          "ms-settings:storagesense",
    "taskbar":          "ms-settings:taskbar",
    "updates":          "ms-settings:windowsupdate",
    "windowsupdate":    "ms-settings:windowsupdate",
    "backup":           "ms-settings:backup",
    "recovery":         "ms-settings:recovery",
    "region":           "ms-settings:regionformatting",
    "language":         "ms-settings:regionlanguage",
    "keyboard":         "ms-settings:keyboard",
    "mouse":            "ms-settings:mousetouchpad",
    "gaming":           "ms-settings:gaming-gamebar",
    "family":           "ms-settings:family-group",
    "activation":       "ms-settings:activation",
}

# Recognized control panel applets. `control <name>` works for .cpl and named applets.
CONTROL_APPLETS: dict[str, str] = {
    "programs":         "appwiz.cpl",       # Programs and Features (uninstall/change)
    "uninstall":        "appwiz.cpl",
    "network":          "ncpa.cpl",         # Network Connections
    "sound":            "mmsys.cpl",
    "display":          "desk.cpl",
    "mouse":            "main.cpl",
    "keyboard":         "main.cpl,,1",
    "regional":         "intl.cpl",
    "power":            "powercfg.cpl",
    "firewall":         "firewall.cpl",
    "datetime":         "timedate.cpl",
    "system":           "sysdm.cpl",
    "fonts":            "fonts",
    "userpasswords":    "netplwiz",
}

# Recognized Windows components. Values are argv lists — no shell parsing.
COMPONENTS: dict[str, list[str]] = {
    "taskmgr":           ["taskmgr.exe"],
    "task_manager":      ["taskmgr.exe"],
    "devmgmt":           ["mmc.exe", "devmgmt.msc"],
    "device_manager":    ["mmc.exe", "devmgmt.msc"],
    "services":          ["mmc.exe", "services.msc"],
    "eventvwr":          ["mmc.exe", "eventvwr.msc"],
    "event_viewer":      ["mmc.exe", "eventvwr.msc"],
    "diskmgmt":          ["mmc.exe", "diskmgmt.msc"],
    "disk_management":   ["mmc.exe", "diskmgmt.msc"],
    "compmgmt":          ["mmc.exe", "compmgmt.msc"],
    "computer_management": ["mmc.exe", "compmgmt.msc"],
    "msconfig":          ["msconfig.exe"],
    "resmon":            ["resmon.exe"],
    "resource_monitor":  ["resmon.exe"],
    "perfmon":           ["perfmon.exe"],
    "cmd":               ["cmd.exe"],
    "powershell":        ["powershell.exe"],
    "gpedit":            ["mmc.exe", "gpedit.msc"],
    "group_policy":      ["mmc.exe", "gpedit.msc"],
    "secpol":            ["mmc.exe", "secpol.msc"],
    "notepad":           ["notepad.exe"],
    "calc":              ["calc.exe"],
    "calculator":        ["calc.exe"],
    "snip":              ["explorer.exe", "ms-screenclip:"],
    "screenshot":        ["explorer.exe", "ms-screenclip:"],
}

# Registry hives that can be pre-navigated. Anything else is refused.
_VALID_HIVES = {"HKCU", "HKLM", "HKCR", "HKU", "HKCC",
                "HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE", "HKEY_CLASSES_ROOT",
                "HKEY_USERS", "HKEY_CURRENT_CONFIG"}

# ms-settings: page names Windows accepts even when we don't know them — keep flexible.
_MS_SETTINGS_PAGE_RE = re.compile(r"^[a-z][a-z0-9\-]{0,60}$")

_NOT_WINDOWS = ("desktop control is Windows-only right now — this JARVIS host reports "
                f"platform={sys.platform!r}.")


def _find_winget() -> str | None:
    """winget ships as an App-Installer AppExecutionAlias. It's on PATH in a normal
    cmd/PowerShell session but Git Bash / minimal environments may miss it — resolve the
    concrete exe under LOCALAPPDATA\\Microsoft\\WindowsApps as the fallback."""
    on_path = shutil.which("winget")
    if on_path:
        return on_path
    p = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe"
    return str(p) if p.exists() else None


def _run(argv: list[str], *, capture: bool = False, timeout: int = 30) -> tuple[int, str, str]:
    """Bounded subprocess. argv (never shell=True). Returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(argv, capture_output=capture, text=capture,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout}s"
    except Exception as exc:
        return 1, "", f"{argv[0]}: {exc}"
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def _spawn(argv: list[str]) -> tuple[bool, str]:
    """Fire-and-forget — for opening windows we don't care about the exit of. Never blocks."""
    try:
        subprocess.Popen(argv, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, ""
    except FileNotFoundError:
        return False, f"{argv[0]}: not found on PATH"
    except Exception as exc:
        return False, f"{argv[0]}: {exc}"


# ── actions ─────────────────────────────────────────────────────────────────────
def open_path(path: str) -> str:
    """Open a file or folder in Explorer (or its default handler)."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    p = (path or "").strip()
    if not p:
        return "open_path: needs a 'path'."
    try:
        expanded = os.path.expandvars(os.path.expanduser(p))
    except Exception:
        expanded = p
    if not os.path.exists(expanded):
        return f"open_path: no such path: {expanded}"
    ok, err = _spawn(["explorer.exe", expanded])
    return f"Opened Explorer at: {expanded}" if ok else f"open_path: {err}"


def open_settings(page: str) -> str:
    """Open a Windows Settings page via the ms-settings: URI scheme."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    p = (page or "").strip().lower()
    if not p:
        return "open_settings: needs a 'page' (e.g. apps, display, network)."
    uri = SETTINGS_PAGES.get(p)
    if not uri:
        # Allow flexible pass-through for pages we don't have mapped, but keep it safe.
        if not _MS_SETTINGS_PAGE_RE.match(p):
            known = ", ".join(sorted(set(SETTINGS_PAGES)))
            return (f"open_settings: unknown page {page!r}. Known: {known}. "
                    f"Or supply a valid ms-settings page slug (letters/digits/dashes).")
        uri = f"ms-settings:{p}"
    # `start` handles URIs; requires shell=True but we control the whole argv.
    # Safer alternative: explorer.exe <uri> — Explorer resolves ms-settings: URIs too.
    ok, err = _spawn(["explorer.exe", uri])
    return f"Opened Settings: {uri}" if ok else f"open_settings: {err}"


def open_control_panel(applet: str) -> str:
    """Open a Control Panel applet (programs, network, sound, …)."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    a = (applet or "").strip().lower()
    if not a:
        return ("open_control_panel: needs an 'applet' (e.g. programs, network, sound).")
    resolved = CONTROL_APPLETS.get(a)
    if not resolved:
        known = ", ".join(sorted(set(CONTROL_APPLETS)))
        return f"open_control_panel: unknown applet {applet!r}. Known: {known}."
    # control <applet> — supports .cpl paths (with optional ",,tab" suffix) and named applets.
    argv = ["control.exe"] + resolved.split()
    ok, err = _spawn(argv)
    return f"Opened Control Panel: {resolved}" if ok else f"open_control_panel: {err}"


def open_component(name: str) -> str:
    """Open a Windows system component (taskmgr, device manager, services, etc.)."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    n = (name or "").strip().lower().replace(" ", "_")
    if not n:
        return "open_component: needs a 'component' (e.g. task_manager, device_manager, services)."
    argv = COMPONENTS.get(n)
    if not argv:
        known = ", ".join(sorted(set(COMPONENTS)))
        return f"open_component: unknown component {name!r}. Known: {known}."
    ok, err = _spawn(argv)
    return f"Opened {n} ({' '.join(argv)})" if ok else f"open_component: {err}"


def _valid_registry_key(key: str) -> str | None:
    """Validate a registry key path. Returns the normalized key or None."""
    k = (key or "").strip()
    if not k:
        return None
    head = k.split("\\", 1)[0].upper()
    if head not in _VALID_HIVES:
        return None
    # Reject characters that don't belong in a registry path.
    if re.search(r'[<>|"*?]', k):
        return None
    return k


def open_registry(key: str | None = None) -> str:
    """Open regedit. If `key` given, pre-navigate to it by writing regedit's LastKey."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    if key:
        norm = _valid_registry_key(key)
        if not norm:
            return (f"open_registry: refused invalid key {key!r} — must start with a valid "
                    "hive (HKCU / HKLM / HKCR / HKU / HKCC) and contain no <>|\"*? chars.")
        # regedit reads its target on launch from this per-user setting.
        rc, out, err = _run(["reg.exe", "add",
                             r"HKCU\Software\Microsoft\Windows\CurrentVersion\Applets\Regedit",
                             "/v", "LastKey", "/t", "REG_SZ", "/d", norm, "/f"],
                            capture=True, timeout=10)
        if rc != 0:
            return f"open_registry: failed to set LastKey: {(err or out).strip()[:200]}"
    ok, err = _spawn(["regedit.exe"])
    if not ok:
        return f"open_registry: {err}"
    return f"Opened regedit{' at ' + key if key else ''}."


# ── package management via winget ───────────────────────────────────────────────
def _parse_winget_list(text: str) -> list[dict]:
    """Parse `winget list` fixed-width table output into [{name, id, version, source}]."""
    rows: list[dict] = []
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    # Find the header line ("Name ... Id ... Version ... Available? ... Source")
    header_idx = None
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low.startswith("name") and " id" in low and "version" in low:
            header_idx = i
            break
    if header_idx is None:
        return rows
    header = lines[header_idx]
    # Column starts by finding each column-name position.
    def col(name: str) -> int:
        idx = header.lower().find(name)
        return idx if idx >= 0 else -1
    c_name, c_id, c_ver, c_src = col("name"), col("id"), col("version"), col("source")
    for ln in lines[header_idx + 2:]:  # skip header + separator '-----'
        if not ln.strip() or set(ln.strip()) <= {"-", " "}:
            continue
        def slice_at(a: int, b: int) -> str:
            if a < 0:
                return ""
            return ln[a:b].strip() if b > 0 else ln[a:].strip()
        rows.append({
            "name":    slice_at(c_name, c_id if c_id > 0 else -1),
            "id":      slice_at(c_id,   c_ver if c_ver > 0 else -1),
            "version": slice_at(c_ver,  c_src if c_src > 0 else -1),
            "source":  slice_at(c_src, -1),
        })
    return rows


def list_apps(filter_text: str = "") -> str:
    """List installed packages via `winget list`. Optional case-insensitive name filter."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    wg = _find_winget()
    if not wg:
        return ("list_apps: winget not found. Install 'App Installer' from the Microsoft "
                "Store, or open Settings > Apps manually.")
    argv = [wg, "list", "--accept-source-agreements", "--disable-interactivity"]
    if filter_text and filter_text.strip():
        argv += ["--name", filter_text.strip()]
    rc, out, err = _run(argv, capture=True, timeout=60)
    if rc != 0 and not out.strip():
        return f"list_apps: winget failed: {(err or '').strip()[:400]}"
    rows = _parse_winget_list(out)
    if not rows:
        return "list_apps: no packages returned (winget may be indexing; try again)."
    shown = rows[:40]
    lines = [f"Installed packages ({len(rows)} total, showing {len(shown)}):"]
    for r in shown:
        lines.append(f"  - {r.get('name',''):40s}  id={r.get('id','')}  v={r.get('version','')}")
    if len(rows) > len(shown):
        lines.append(f"  … and {len(rows) - len(shown)} more. Narrow with the `filter` arg.")
    return "\n".join(lines)


def uninstall_app(app: str, confirm: bool = False) -> str:
    """Uninstall a package via winget. Requires confirm=True; without it, returns a dry-run
    listing the matches so the model can present them to the user for a decision."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    a = (app or "").strip()
    if not a:
        return "uninstall_app: needs an 'app' (name or exact id)."
    wg = _find_winget()
    if not wg:
        return ("uninstall_app: winget not found. Fallback: opening Settings > Apps for "
                "manual uninstall. (Also try `open_control_panel programs`.)\n"
                + open_settings("appsfeatures"))
    # Resolve matches by name (fuzzy) so we can disambiguate before running.
    rc, out, err = _run([wg, "list", "--name", a, "--accept-source-agreements",
                         "--disable-interactivity"], capture=True, timeout=45)
    rows = _parse_winget_list(out) if rc == 0 else []
    # If the user passed an exact id and nothing came back by name, try by id.
    if not rows:
        rc2, out2, err2 = _run([wg, "list", "--id", a, "--accept-source-agreements",
                                "--disable-interactivity"], capture=True, timeout=45)
        if rc2 == 0:
            rows = _parse_winget_list(out2)
    if not rows:
        return (f"uninstall_app: no installed package matched {app!r}. Try `list_apps` "
                "first, or supply the exact winget id.")
    if len(rows) > 1:
        lines = [f"uninstall_app: {len(rows)} packages matched {app!r} — be more specific:"]
        for r in rows[:12]:
            lines.append(f"  - name={r.get('name')!r}  id={r.get('id')!r}  v={r.get('version','')}")
        lines.append("Re-call `uninstall_app` with the EXACT id (or a more precise name).")
        return "\n".join(lines)
    match = rows[0]
    exact_id = match.get("id", "") or ""
    if not confirm:
        return (f"uninstall_app (dry run): would uninstall\n"
                f"  name: {match.get('name', '')}\n"
                f"  id:   {exact_id}\n"
                f"  ver:  {match.get('version', '')}\n"
                f"Ask the user to confirm, then call again with confirm=true.")
    argv = [wg, "uninstall", "--id", exact_id, "--silent",
            "--accept-source-agreements", "--disable-interactivity"]
    rc, out, err = _run(argv, capture=True, timeout=300)
    tail = (out or err or "").strip().splitlines()[-8:]
    summary = "\n".join(tail)[:1500]
    if rc == 0:
        return f"Uninstalled {match.get('name')} ({exact_id}).\n{summary}"
    return f"uninstall_app failed (rc={rc}) for {exact_id}.\n{summary}"


# ── one entry point the tool dispatch calls ─────────────────────────────────────
_ACTIONS = {"open_path", "open_settings", "open_control_panel", "open_registry",
            "open_component", "list_apps", "uninstall_app"}


def run(action: str, args: dict) -> str:
    """Dispatch a `desktop` tool call. Never raises — every failure returns a string."""
    act = (action or "").strip().lower()
    if act not in _ACTIONS:
        return f"desktop: unknown action {action!r}. Available: {', '.join(sorted(_ACTIONS))}."
    if act == "open_path":
        return open_path(str(args.get("path") or ""))
    if act == "open_settings":
        return open_settings(str(args.get("page") or ""))
    if act == "open_control_panel":
        return open_control_panel(str(args.get("applet") or ""))
    if act == "open_registry":
        key = args.get("key")
        return open_registry(str(key) if key else None)
    if act == "open_component":
        return open_component(str(args.get("component") or args.get("name") or ""))
    if act == "list_apps":
        return list_apps(str(args.get("filter") or ""))
    if act == "uninstall_app":
        return uninstall_app(str(args.get("app") or ""), bool(args.get("confirm")))
    return f"desktop: action {act!r} was in _ACTIONS but had no handler — this is a bug."
