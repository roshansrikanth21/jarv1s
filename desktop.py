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


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows: suppress console flash


def _run(argv: list[str], *, capture: bool = False, timeout: int = 30) -> tuple[int, str, str]:
    """Bounded subprocess. argv (never shell=True). CREATE_NO_WINDOW on Windows so calls
    to powershell/netsh/reg/winget never flash a cmd window. Returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(argv, capture_output=capture, text=capture,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=_NO_WINDOW)
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
    """Open a file/folder/app the way a user means "open X":
      • executable (.exe/.com/.bat/.cmd) → LAUNCH it (so "open notepad/chrome" actually
        starts the app, instead of revealing it in Explorer);
      • folder → open it in Explorer;
      • document → open in its default handler.
    os.startfile does exactly this per-type; explorer.exe <exe> would only reveal the file."""
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
    is_exe = os.path.isfile(expanded) and os.path.splitext(expanded)[1].lower() in (
        ".exe", ".com", ".bat", ".cmd")
    try:
        os.startfile(expanded)   # launches exes, opens folders in Explorer, docs in their app
        return (f"Launched: {expanded}" if is_exe else f"Opened: {expanded}")
    except Exception as exc:
        ok, err = _spawn(["explorer.exe", expanded])   # fallback: reveal in Explorer
        return f"Opened Explorer at: {expanded}" if ok else f"open_path: {exc or err}"


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


# ── system control (volume / brightness / wifi) ─────────────────────────────────
# All Windows-only. Zero pip deps — uses PowerShell + WMI + netsh, which ship with Windows.

_VK = {"volume_mute": 173, "volume_down": 174, "volume_up": 175}


def system_volume(action: str, level: int | None = None) -> str:
    """System volume: up | down | mute | set (with level 0-100).
    `set` is approximate — issued as N up/down presses (~2% per press) rather than a
    Core Audio API call. Exact level requires pycaw; kept dep-free on purpose."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    act = (action or "").strip().lower()
    if act not in ("up", "down", "mute", "set"):
        return "system_volume: action must be up|down|mute|set."
    if act == "set":
        if level is None:
            return "system_volume: set requires level (0-100)."
        try:
            lvl = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return "system_volume: level must be an integer 0-100."
        # Ensure the floor (50 downs is more than enough for any preset), then N ups.
        cmds = (["(New-Object -ComObject WScript.Shell).SendKeys([char]174)"] * 50
                + ["(New-Object -ComObject WScript.Shell).SendKeys([char]175)"] * (lvl // 2))
        ps = "; ".join(cmds)
    else:
        vk = _VK[f"volume_{act}"]
        ps = f"(New-Object -ComObject WScript.Shell).SendKeys([char]{vk})"
    rc, _, err = _run(["powershell.exe", "-NoProfile", "-Command", ps],
                      capture=True, timeout=10)
    if rc == 0:
        return f"volume: {act}" + (f" (~{level}%)" if act == "set" else "")
    return f"system_volume failed: {(err or '').strip()[:160]}"


def brightness(action: str, level: int | None = None) -> str:
    """Screen brightness: up | down | set (level 0-100). Uses WMI — works on laptop
    internal displays that support DDC. External monitors and desktops usually can't be
    controlled this way (that's a hardware limit, not a bug)."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    act = (action or "").strip().lower()
    if act not in ("up", "down", "set"):
        return "brightness: action must be up|down|set."
    if act == "set":
        if level is None:
            return "brightness: set requires level (0-100)."
        try:
            target = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return "brightness: level must be an integer 0-100."
    else:
        query = ("$b = (Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness "
                 "-ErrorAction SilentlyContinue).CurrentBrightness; "
                 "if ($b -eq $null) { 50 } else { $b }")
        rc, out, _ = _run(["powershell.exe", "-NoProfile", "-Command", query],
                          capture=True, timeout=8)
        try:
            current = int((out or "50").strip().splitlines()[-1])
        except Exception:
            current = 50
        target = min(100, current + 10) if act == "up" else max(0, current - 10)
    ps = (f"(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods "
          f"-ErrorAction Stop).WmiSetBrightness(1, {target}) | Out-Null")
    rc, _, err = _run(["powershell.exe", "-NoProfile", "-Command", ps],
                      capture=True, timeout=8)
    if rc == 0:
        return f"brightness: {target}%"
    return (f"brightness failed (WMI brightness only works on laptop displays with DDC "
            f"support): {(err or '').strip()[:160]}")


def toggle_wifi(state: str, adapter: str = "Wi-Fi") -> str:
    """Enable / disable the named network adapter (default 'Wi-Fi'). Usually needs admin;
    the underlying netsh call will return the error verbatim if it doesn't have rights."""
    if not IS_WINDOWS:
        return _NOT_WINDOWS
    s = (state or "").strip().lower()
    if s in ("on", "enable"):
        op = "enable"
    elif s in ("off", "disable"):
        op = "disable"
    else:
        return "toggle_wifi: state must be on|off."
    rc, out, err = _run(["netsh.exe", "interface", "set", "interface",
                         adapter, op], capture=True, timeout=10)
    if rc == 0:
        return f"Wi-Fi ({adapter}): {op}d"
    msg = (err or out or "").strip()[:220]
    return (f"toggle_wifi failed: {msg}. Usually this needs admin — right-click PowerShell "
            "> Run as administrator, or toggle it in Settings > Network.")


# ── mouse + keyboard control (via pyautogui — lazy import) ─────────────────────
def _pyautogui():
    """Lazy import so hosts without pyautogui installed can still use every other
    desktop action. Callers get a clear string error instead of a hard ImportError."""
    try:
        import pyautogui
        # Failsafe: pyautogui raises when the mouse hits (0,0), a physical panic-abort.
        # Leave it enabled — it's a real safety on a machine driven by an LLM.
        pyautogui.FAILSAFE = True
        return pyautogui
    except Exception as exc:
        raise RuntimeError(
            f"pyautogui not installed ({exc}). Run `pip install pyautogui` "
            "in the JARVIS venv.")


def _screen_bounds() -> tuple[int, int] | None:
    try:
        pa = _pyautogui()
        return pa.size()
    except Exception:
        return None


def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Click at absolute screen coords. Left/right/middle. clicks capped at 3."""
    try:
        pa = _pyautogui()
    except RuntimeError as exc:
        return str(exc)
    if button not in ("left", "right", "middle"):
        return "mouse_click: button must be left|right|middle."
    try:
        xi, yi = int(x), int(y)
        n = max(1, min(3, int(clicks)))
    except (TypeError, ValueError):
        return "mouse_click: x/y/clicks must be integers."
    size = _screen_bounds()
    if size and (xi < 0 or yi < 0 or xi >= size[0] or yi >= size[1]):
        return f"mouse_click: ({xi},{yi}) is off-screen (screen is {size[0]}x{size[1]})."
    try:
        pa.click(xi, yi, clicks=n, button=button)
        return f"clicked ({xi},{yi}) {button} x{n}"
    except Exception as exc:
        return f"mouse_click failed: {exc}"


def mouse_move(x: int, y: int, duration: float = 0.0) -> str:
    """Move the pointer to absolute screen coords. duration in seconds (0..3)."""
    try:
        pa = _pyautogui()
    except RuntimeError as exc:
        return str(exc)
    try:
        xi, yi = int(x), int(y)
        d = max(0.0, min(3.0, float(duration)))
    except (TypeError, ValueError):
        return "mouse_move: x/y must be int; duration must be number."
    try:
        pa.moveTo(xi, yi, duration=d)
        return f"mouse at ({xi},{yi})"
    except Exception as exc:
        return f"mouse_move failed: {exc}"


def mouse_scroll(clicks: int) -> str:
    """Scroll wheel — positive is up, negative is down."""
    try:
        pa = _pyautogui()
    except RuntimeError as exc:
        return str(exc)
    try:
        n = max(-20, min(20, int(clicks)))
    except (TypeError, ValueError):
        return "mouse_scroll: clicks must be an integer (-20..20)."
    try:
        pa.scroll(n)
        return f"scrolled {n:+d}"
    except Exception as exc:
        return f"mouse_scroll failed: {exc}"


def type_text(text: str, confirm: bool = False) -> str:
    """Type text into whatever window has focus. Requires confirm=true when the payload
    is > 60 chars OR contains newlines/tabs — otherwise a hallucinated string could dump
    a whole paragraph into a chat / code editor / terminal. The model must preview it to
    the user and get a yes first."""
    try:
        pa = _pyautogui()
    except RuntimeError as exc:
        return str(exc)
    t = "" if text is None else str(text)
    if not t:
        return "type_text: text is empty."
    if not confirm and (len(t) > 60 or "\n" in t or "\t" in t):
        preview = (t[:60] + "…") if len(t) > 60 else t
        return (f"type_text: payload is {len(t)} chars and/or has newlines/tabs — "
                f"preview {preview!r} to the user, then call again with confirm=true.")
    try:
        pa.typewrite(t, interval=0.005)   # tiny per-char delay = better key registration
        return f"typed {len(t)} char(s)"
    except Exception as exc:
        return f"type_text failed: {exc}"


_ALLOWED_KEYS = frozenset({
    *"abcdefghijklmnopqrstuvwxyz0123456789",
    "enter", "return", "esc", "escape", "tab", "backspace", "delete", "space",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown", "insert",
    "capslock", "printscreen", "scrolllock", "pause",
    *(f"f{i}" for i in range(1, 25)),
    "ctrl", "control", "alt", "shift", "cmd", "command", "win", "winleft", "winright",
    # Media keys — control YouTube/Spotify/Netflix/system audio without opening the app.
    "playpause", "nexttrack", "prevtrack", "stop",
    "volumemute", "volumeup", "volumedown",
})


def key_press(keys: str) -> str:
    """Press a single key or a hotkey combo. Single: 'enter' | 'esc' | 'f5' | 'a'.
    Combo: 'ctrl+c' | 'cmd+shift+p' | 'alt+tab'. Keys are allowlisted — arbitrary
    unicode strings would be a `type_text` call, not this one."""
    try:
        pa = _pyautogui()
    except RuntimeError as exc:
        return str(exc)
    seq = str(keys or "").strip().lower()
    if not seq:
        return "key_press: needs 'keys'."
    parts = [p.strip() for p in seq.split("+") if p.strip()]
    for p in parts:
        if p not in _ALLOWED_KEYS:
            return (f"key_press: unknown key {p!r}. Allowed: a-z, 0-9, named keys "
                    "(enter/esc/tab/space/backspace/delete/arrows/home/end/pageup/pagedown/"
                    "f1-f24), modifiers (ctrl/alt/shift/cmd/win).")
    try:
        if len(parts) == 1:
            pa.press(parts[0])
        else:
            pa.hotkey(*parts)
        return f"pressed: {seq}"
    except Exception as exc:
        return f"key_press failed: {exc}"


# ── OS toast notifications ──────────────────────────────────────────────────────
def notify(title: str, message: str, timeout: int = 5) -> str:
    """Fire a native OS toast notification via plyer (cross-platform).
    On Windows this uses the Action Center toast; on macOS AppleScript notifications;
    on Linux libnotify. Not persistent — fire-and-forget."""
    try:
        from plyer import notification
    except Exception as exc:
        return (f"notify: plyer not installed ({exc}). "
                "Run `pip install plyer` in the JARVIS venv.")
    try:
        notification.notify(
            title=(title or "").strip()[:60] or "JARVIS",
            message=(message or "").strip()[:200] or "",
            timeout=max(2, min(30, int(timeout))),
            app_name="JARVIS",
        )
        return f"notified: {title!r}"
    except Exception as exc:
        return f"notify failed: {exc}"


# ── webcam capture ──────────────────────────────────────────────────────────────
def capture_webcam(save_path: str | None = None) -> str:
    """Grab one frame from the default webcam. Returns the file path so the model can
    hand it to `analyze_image` for vision reasoning. opencv is already a JARVIS dep."""
    try:
        import cv2
    except Exception as exc:
        return f"capture_webcam: opencv not available ({exc})."
    # DirectShow backend is much friendlier on Windows than the default.
    backend = cv2.CAP_DSHOW if IS_WINDOWS else 0
    cap = cv2.VideoCapture(0, backend)
    if not cap.isOpened():
        return "capture_webcam: no camera available (or in use by another app)."
    try:
        # First read is often black on some drivers — do a couple of warm-up frames.
        frame = None
        for _ in range(4):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
        if frame is None:
            return "capture_webcam: camera opened but returned no frame."
        import time
        from pathlib import Path as _P
        if save_path:
            out = _P(save_path)
        else:
            out_dir = _P(__file__).resolve().parent / "memory" / "webcam"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"webcam_{int(time.time())}.jpg"
        cv2.imwrite(str(out), frame)
        return f"captured: {out}"
    finally:
        cap.release()


# ── window management (via pywin32 if installed, pyautogui.getWindowsWithTitle otherwise) ──
def _match_windows(title_substr: str) -> list:
    """Return list of window objects (backend-agnostic) whose title contains
    `title_substr` (case-insensitive). Empty on no match."""
    if not title_substr:
        return []
    want = title_substr.lower()
    # Try pyautogui first (falls back through pygetwindow — cross-platform-ish).
    try:
        pa = _pyautogui()
        try:
            wins = pa.getAllWindows()
        except Exception:
            wins = pa.getWindowsWithTitle(title_substr) or []
        return [w for w in wins if want in (getattr(w, "title", "") or "").lower()]
    except Exception:
        return []


def _window_action(title: str, op: str) -> str:
    """One entry point for focus/min/max/close by title substring. Uses the same
    disambiguation pattern as uninstall_app: 0 matches → error; 2+ → refuse and
    list; 1 → perform."""
    if not title:
        return f"window_{op}: needs 'title' (substring, case-insensitive)."
    matches = _match_windows(title)
    if not matches:
        return f"window_{op}: no window title matched {title!r}."
    if len(matches) > 1:
        titles = ", ".join(f"{(getattr(w, 'title', '') or '')!r}" for w in matches[:8])
        return (f"window_{op}: {len(matches)} windows matched {title!r}: {titles}. "
                "Be more specific.")
    w = matches[0]
    try:
        if op == "focus":
            w.activate()
        elif op == "minimize":
            w.minimize()
        elif op == "maximize":
            w.maximize()
        elif op == "close":
            w.close()
        elif op == "restore":
            w.restore()
        else:
            return f"window_{op}: unknown op."
        return f"window {op}d: {getattr(w, 'title', '')!r}"
    except Exception as exc:
        return f"window_{op} failed: {exc}"


def window_focus(title: str) -> str:      return _window_action(title, "focus")
def window_minimize(title: str) -> str:   return _window_action(title, "minimize")
def window_maximize(title: str) -> str:   return _window_action(title, "maximize")
def window_restore(title: str) -> str:    return _window_action(title, "restore")
def window_close(title: str) -> str:      return _window_action(title, "close")


def window_list() -> str:
    """List currently visible windows — for the model to pick a target."""
    try:
        pa = _pyautogui()
    except RuntimeError as exc:
        return str(exc)
    try:
        wins = pa.getAllWindows()
    except Exception as exc:
        return f"window_list failed: {exc}"
    titles = [(getattr(w, "title", "") or "").strip() for w in wins]
    titles = [t for t in titles if t]
    if not titles:
        return "window_list: no visible windows."
    shown = titles[:25]
    body = "\n".join(f"  - {t}" for t in shown)
    tail = f"\n  … and {len(titles) - len(shown)} more" if len(titles) > len(shown) else ""
    return f"Visible windows ({len(titles)}):\n{body}{tail}"


# ── OS-native scheduled reminders (wraps reminder.py) ───────────────────────────
def remind(sub_action: str, args: dict) -> str:
    """schedule / list / cancel. All calls delegated to reminder.py."""
    try:
        import reminder
    except Exception as exc:
        return f"remind: reminder module unavailable ({exc})."
    sub = (sub_action or "").strip().lower()
    if sub in ("", "schedule", "add", "create"):
        r = reminder.schedule(
            when=str(args.get("when") or ""),
            message=str(args.get("message") or args.get("body") or ""),
            title=str(args.get("title") or "JARVIS"),
        )
        if not r.get("ok"):
            return f"remind: {r.get('error', 'unknown error')}"
        return (f"Reminder scheduled — id={r['id']}, when={r['when']}, "
                f"title={r['title']!r}, message={r['message']!r}")
    if sub == "list":
        items = reminder.list_all()
        if not items:
            return "remind list: no reminders scheduled."
        lines = [f"Scheduled reminders ({len(items)}):"]
        for it in items[:20]:
            lines.append(f"  - id={it['id']}  next={it.get('next_run', '?')}  "
                         f"status={it.get('status', '?')}")
        return "\n".join(lines)
    if sub in ("cancel", "delete", "remove"):
        rid = str(args.get("id") or args.get("reminder_id") or "").strip()
        if not rid:
            return "remind cancel: needs 'id' (from schedule / list)."
        r = reminder.cancel(rid)
        return f"remind cancel: {r}"
    return f"remind: unknown sub_action {sub_action!r}. Use schedule | list | cancel."


# ── one entry point the tool dispatch calls ─────────────────────────────────────
_ACTIONS = {
    # existing
    "open_path", "open_settings", "open_control_panel", "open_registry",
    "open_component", "list_apps", "uninstall_app",
    # Mark-XLVIII parity (p1)
    "system_volume", "brightness", "toggle_wifi",
    "mouse_click", "mouse_move", "mouse_scroll",
    "type_text", "key_press",
    "notify", "capture_webcam",
    # Mark-XLVIII parity (p2): windows + native reminders
    "window_focus", "window_minimize", "window_maximize", "window_restore",
    "window_close", "window_list",
    "remind",
}


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
    # new dispatch (Mark-XLVIII parity)
    if act == "system_volume":
        return system_volume(str(args.get("action") or args.get("volume_action") or ""),
                             args.get("level"))
    if act == "brightness":
        return brightness(str(args.get("action") or args.get("brightness_action") or ""),
                          args.get("level"))
    if act == "toggle_wifi":
        return toggle_wifi(str(args.get("state") or ""),
                           str(args.get("adapter") or "Wi-Fi"))
    if act == "mouse_click":
        return mouse_click(args.get("x", 0), args.get("y", 0),
                           str(args.get("button") or "left"),
                           int(args.get("clicks") or 1))
    if act == "mouse_move":
        return mouse_move(args.get("x", 0), args.get("y", 0),
                          args.get("duration") or 0.0)
    if act == "mouse_scroll":
        return mouse_scroll(int(args.get("clicks") or 0))
    if act == "type_text":
        return type_text(str(args.get("text") or ""), bool(args.get("confirm")))
    if act == "key_press":
        return key_press(str(args.get("keys") or ""))
    if act == "notify":
        return notify(str(args.get("title") or ""),
                      str(args.get("message") or args.get("body") or ""),
                      int(args.get("timeout") or 5))
    if act == "capture_webcam":
        return capture_webcam(args.get("path"))
    # p2 dispatch — windows + reminders
    if act == "window_focus":
        return window_focus(str(args.get("title") or ""))
    if act == "window_minimize":
        return window_minimize(str(args.get("title") or ""))
    if act == "window_maximize":
        return window_maximize(str(args.get("title") or ""))
    if act == "window_restore":
        return window_restore(str(args.get("title") or ""))
    if act == "window_close":
        return window_close(str(args.get("title") or ""))
    if act == "window_list":
        return window_list()
    if act == "remind":
        return remind(str(args.get("sub_action") or args.get("op") or "schedule"), args)
    return f"desktop: action {act!r} was in _ACTIONS but had no handler — this is a bug."
