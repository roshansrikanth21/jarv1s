"""Offline self-test for `desktop.py`.

Verifies routing, argument validation, disambiguation, the uninstall confirm gate,
and the injection-safety property (nothing model-supplied ever reaches shell=True).
Every subprocess call is intercepted so no Explorer window opens, no regedit runs,
no winget is invoked. The tests still exercise the real code paths.

Usage:
    python scripts/selftest_desktop.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if callable(_reconfigure):
    try:
        _reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import desktop   # noqa: E402


_checks: list[tuple[str, bool, str]] = []


def ok(label: str, cond: bool, hint: str = "") -> None:
    _checks.append((label, bool(cond), hint))
    mark = "ok  " if cond else "FAIL"
    print(f"  {mark} {label}" + (f"   -> {hint}" if not cond and hint else ""))


def section(name: str) -> None:
    print(name)


# ── Test harness: intercept every subprocess call so we can assert on argv ─────
_spawn_calls: list[list[str]] = []
_run_calls: list[list[str]] = []
_run_responses: dict[str, tuple[int, str, str]] = {}
_startfile_calls: list[str] = []


def _install_stubs() -> None:
    def fake_spawn(argv):
        _spawn_calls.append(list(argv))
        return True, ""

    def fake_run(argv, *, capture=False, timeout=30):
        _run_calls.append(list(argv))
        # Match by the first non-exe token that identifies the winget subcommand.
        key = ""
        if len(argv) >= 2 and "winget" in argv[0].lower():
            key = f"winget:{argv[1]}"
        return _run_responses.get(key, (0, "", ""))

    def fake_startfile(path, *args, **kwargs):
        _startfile_calls.append(str(path))

    desktop._spawn = fake_spawn
    desktop._run = fake_run
    # Force IS_WINDOWS true so we exercise the real branches even if running on non-Windows.
    desktop.IS_WINDOWS = True
    os.startfile = fake_startfile  # type: ignore[attr-defined]


def _reset() -> None:
    _spawn_calls.clear()
    _run_calls.clear()
    _run_responses.clear()
    _startfile_calls.clear()


def _last_spawn() -> list[str]:
    return _spawn_calls[-1] if _spawn_calls else []


def run() -> int:
    _install_stubs()

    section("dispatch — unknown action")
    _reset()
    out = desktop.run("nope", {})
    ok("unknown action returns clear error", "unknown action" in out and "Available" in out, out)
    ok("unknown action never spawned anything", not _spawn_calls and not _run_calls)

    section("open_path — startfile / Explorer")
    _reset()
    # A path that definitely exists on any Windows host (or wherever this runs).
    real = str(Path(_REPO).resolve())
    out = desktop.run("open_path", {"path": real})
    ok(
        "open_path uses os.startfile with the resolved path",
        _startfile_calls[:1] == [real] and not _spawn_calls,
        str(_startfile_calls),
    )
    ok("open_path reports what it opened", out.startswith("Opened:"), out)

    _reset()
    out = desktop.run("open_path", {"path": ""})
    ok("open_path with empty path errors, does not spawn", "needs" in out and not _spawn_calls)

    _reset()
    out = desktop.run("open_path", {"path": r"C:\definitely\not\there\zzz42"})
    ok("open_path with nonexistent path errors, does not spawn",
       "no such path" in out and not _spawn_calls)

    section("open_settings — ms-settings URIs")
    _reset()
    out = desktop.run("open_settings", {"page": "apps"})
    ok("open_settings apps → ms-settings:appsfeatures",
       "ms-settings:appsfeatures" in _last_spawn(), str(_last_spawn()))
    _reset()
    out = desktop.run("open_settings", {"page": "SoMe-NeW-PaGe"})
    ok("open_settings accepts new pages that pass the URI-safe regex",
       any("ms-settings:some-new-page" in a for a in _last_spawn()), str(_last_spawn()))
    _reset()
    out = desktop.run("open_settings", {"page": "has spaces & bad!"})
    ok("open_settings rejects unsafe page strings",
       "unknown page" in out and not _spawn_calls, out)

    section("open_control_panel — control.exe applets")
    _reset()
    out = desktop.run("open_control_panel", {"applet": "programs"})
    ok("open_control_panel programs → control.exe appwiz.cpl",
       _last_spawn() == ["control.exe", "appwiz.cpl"], str(_last_spawn()))
    _reset()
    out = desktop.run("open_control_panel", {"applet": "made-up"})
    ok("open_control_panel unknown applet errors + lists knowns, does not spawn",
       "unknown applet" in out and "programs" in out and not _spawn_calls)

    section("open_registry — regedit, hive-validated when key is passed")
    _reset()
    out = desktop.run("open_registry", {})
    ok("open_registry with no key just spawns regedit",
       _last_spawn() == ["regedit.exe"] and not _run_calls)
    _reset()
    out = desktop.run("open_registry", {"key": r"HKCU\Software\Microsoft"})
    ok("open_registry with a valid key first writes LastKey via reg.exe",
       _run_calls and _run_calls[0][:2] == ["reg.exe", "add"])
    ok("open_registry then spawns regedit.exe",
       _last_spawn() == ["regedit.exe"])
    _reset()
    out = desktop.run("open_registry", {"key": r"BOGUS_HIVE\foo"})
    ok("open_registry refuses invalid hive, does not touch registry or spawn regedit",
       "refused invalid key" in out and not _run_calls and not _spawn_calls)
    _reset()
    out = desktop.run("open_registry", {"key": r'HKCU\Software\<bad>chars'})
    ok("open_registry refuses forbidden chars in key",
       "refused invalid key" in out and not _run_calls and not _spawn_calls)

    section("open_component — system components")
    _reset()
    out = desktop.run("open_component", {"component": "task_manager"})
    ok("open_component task_manager → taskmgr.exe", _last_spawn() == ["taskmgr.exe"])
    _reset()
    out = desktop.run("open_component", {"component": "device_manager"})
    ok("open_component device_manager → mmc.exe devmgmt.msc",
       _last_spawn() == ["mmc.exe", "devmgmt.msc"], str(_last_spawn()))
    _reset()
    out = desktop.run("open_component", {"component": "made up thing"})
    ok("open_component unknown errors + lists knowns",
       "unknown component" in out and "task_manager" in out and not _spawn_calls)

    section("list_apps — winget list parsing")
    # Give _find_winget a plausible path so we exercise the argv it constructs.
    desktop._find_winget = lambda: "winget"
    _reset()
    _run_responses["winget:list"] = (0, (
        "Name                     Id                     Version    Available    Source\n"
        "-----------------------  ---------------------  ---------  -----------  ------\n"
        "Zoom Workplace           Zoom.Zoom              6.0.10                  winget\n"
        "Visual Studio Code       Microsoft.VisualStud…  1.90.0                  winget\n"
    ), "")
    out = desktop.run("list_apps", {})
    ok("list_apps returns a header line + parsed rows",
       "Installed packages" in out and "Zoom Workplace" in out and "Visual Studio Code" in out, out[:200])
    ok("list_apps used `winget list` with expected flags",
       _run_calls and _run_calls[-1][:2] == ["winget", "list"]
       and "--accept-source-agreements" in _run_calls[-1])

    _reset()
    _run_responses["winget:list"] = (1, "", "winget failed hard")
    out = desktop.run("list_apps", {})
    ok("list_apps surfaces winget failures cleanly", "winget failed" in out, out)

    section("uninstall_app — dry-run + confirm gate + disambiguation")
    desktop._find_winget = lambda: "winget"

    # (a) no matches
    _reset()
    _run_responses["winget:list"] = (0, "no matches whatsoever\n", "")
    out = desktop.run("uninstall_app", {"app": "definitely-not-installed"})
    ok("no matches → clear error, no uninstall attempted",
       "no installed package matched" in out
       and not any(c[:2] == ["winget", "uninstall"] for c in _run_calls), out)

    # (b) exactly one match, confirm=False → dry-run only
    _reset()
    _run_responses["winget:list"] = (0, (
        "Name             Id         Version    Available    Source\n"
        "---------------  ---------  ---------  -----------  ------\n"
        "Zoom Workplace   Zoom.Zoom  6.0.10                  winget\n"
    ), "")
    out = desktop.run("uninstall_app", {"app": "zoom"})
    ok("one match without confirm → dry-run",
       "dry run" in out and "confirm=true" in out, out)
    ok("dry-run did NOT invoke `winget uninstall`",
       not any(c[:2] == ["winget", "uninstall"] for c in _run_calls))

    # (c) exactly one match, confirm=True → actually uninstalls
    _reset()
    _run_responses["winget:list"] = (0, (
        "Name             Id         Version    Available    Source\n"
        "---------------  ---------  ---------  -----------  ------\n"
        "Zoom Workplace   Zoom.Zoom  6.0.10                  winget\n"
    ), "")
    _run_responses["winget:uninstall"] = (0, "Successfully uninstalled\n", "")
    out = desktop.run("uninstall_app", {"app": "zoom", "confirm": True})
    ok("one match with confirm=true → runs `winget uninstall --id Zoom.Zoom`",
       any(c[:2] == ["winget", "uninstall"] and "Zoom.Zoom" in c for c in _run_calls),
       str(_run_calls))
    ok("confirmed uninstall reports the package name",
       "Uninstalled Zoom Workplace" in out, out)

    # (d) multiple matches → disambiguation, refuses to run
    _reset()
    _run_responses["winget:list"] = (0, (
        "Name              Id             Version    Available    Source\n"
        "----------------  -------------  ---------  -----------  ------\n"
        "Zoom Workplace    Zoom.Zoom      6.0.10                  winget\n"
        "Zoom Rooms        Zoom.Zoom-Rooms 5.16.0                 winget\n"
    ), "")
    out = desktop.run("uninstall_app", {"app": "zoom", "confirm": True})
    ok("multiple matches with confirm=true → refuses, asks for exact id",
       ("packages matched" in out or "2 packages" in out) and "EXACT id" in out,
       out)
    ok("multiple-match branch did NOT invoke `winget uninstall`",
       not any(c[:2] == ["winget", "uninstall"] for c in _run_calls))

    # (e) winget not on the machine → helpful fallback
    _reset()
    desktop._find_winget = lambda: None
    out = desktop.run("uninstall_app", {"app": "zoom"})
    ok("no winget → falls back with a helpful message and opens Settings > Apps",
       "winget not found" in out and any("ms-settings:appsfeatures" in " ".join(a) for a in _spawn_calls),
       out[:200])

    section("safety — no shell=True path is ever taken (argv only)")
    # The functions call _spawn/_run with argv lists only. Our stubs record every call.
    # An injection attempt in an arg should just be a string ELEMENT — never split by a shell.
    desktop._find_winget = lambda: "winget"
    _reset()
    _run_responses["winget:list"] = (0, (
        "Name         Id             Version    Available    Source\n"
        "-----------  -------------  ---------  -----------  ------\n"
        "EvilApp      x; rm -rf /    1.0                     winget\n"
    ), "")
    out = desktop.run("uninstall_app", {"app": "evil", "confirm": True})
    # The id "x; rm -rf /" gets passed as a single argv element; not interpreted by any shell.
    ran = [c for c in _run_calls if c[:2] == ["winget", "uninstall"]]
    ok("injection-y id is passed as a single argv element (no shell splitting)",
       any("x; rm -rf /" in c for c in ran), str(ran))

    # ── Mark-XLVIII parity: system control + mouse/keyboard + notify + webcam ──
    section("system_volume — up/down/mute/set + validation")
    _reset()
    out = desktop.run("system_volume", {"action": "mute"})
    ok("mute → powershell SendKeys VK_VOLUME_MUTE (173)",
       _run_calls and "powershell.exe" in _run_calls[-1] and "[char]173" in _run_calls[-1][-1], out[:80])
    _reset()
    out = desktop.run("system_volume", {"action": "set", "level": 50})
    ok("set 50% emits ~25 up-presses on top of the floor",
       _run_calls[-1][-1].count("[char]175") == 25, out[:80])
    _reset()
    out = desktop.run("system_volume", {"action": "bogus"})
    ok("unknown volume action errors, does not shell out",
       "must be up|down|mute|set" in out and not _run_calls, out)
    out = desktop.run("system_volume", {"action": "set"})
    ok("set without level errors clearly", "requires level" in out, out)

    section("brightness — up/down/set + WMI call shape")
    _reset()
    _run_responses[""] = (0, "", "")   # any powershell call → success stub
    def _brightness_run(argv, capture=False, timeout=30):
        _run_calls.append(list(argv))
        # First call is the read (returns "60"); second is the write.
        if "CurrentBrightness" in " ".join(argv):
            return (0, "60", "")
        return (0, "", "")
    desktop._run = _brightness_run
    out = desktop.run("brightness", {"action": "up"})
    ok("up reads current then writes current+10 via WmiSetBrightness",
       any("WmiSetBrightness(1, 70)" in " ".join(c) for c in _run_calls), out)
    # Restore the standard stub for later sections
    _install_stubs(); desktop._find_winget = lambda: "winget"
    _reset()
    out = desktop.run("brightness", {"action": "set", "level": 30})
    ok("set 30 goes straight to WmiSetBrightness(1, 30)",
       any("WmiSetBrightness(1, 30)" in " ".join(c) for c in _run_calls), out)
    out = desktop.run("brightness", {"action": "bogus"})
    ok("unknown brightness action errors", "must be up|down|set" in out)

    section("toggle_wifi — netsh enable/disable")
    _reset()
    out = desktop.run("toggle_wifi", {"state": "off"})
    ok("off → netsh interface set interface Wi-Fi disable",
       _run_calls and _run_calls[-1][:4] == ["netsh.exe", "interface", "set", "interface"]
       and "disable" in _run_calls[-1], str(_run_calls[-1] if _run_calls else []))
    _reset()
    out = desktop.run("toggle_wifi", {"state": "on", "adapter": "Ethernet"})
    ok("custom adapter name is passed to netsh",
       _run_calls and "Ethernet" in _run_calls[-1] and "enable" in _run_calls[-1])
    out = desktop.run("toggle_wifi", {"state": "bogus"})
    ok("unknown state errors, no netsh call", "state must be on|off" in out)

    section("mouse + keyboard — allowlisted keys, off-screen guard, type_text confirm")
    class _FakePA:
        def __init__(self):
            self.calls = []; self.FAILSAFE = True
        def size(self): return (1920, 1080)
        def click(self, x, y, clicks=1, button="left"): self.calls.append(("click", x, y, clicks, button))
        def moveTo(self, x, y, duration=0): self.calls.append(("moveTo", x, y, duration))
        def scroll(self, n): self.calls.append(("scroll", n))
        def typewrite(self, s, interval=0): self.calls.append(("typewrite", s, interval))
        def press(self, k): self.calls.append(("press", k))
        def hotkey(self, *keys): self.calls.append(("hotkey", keys))
    fake_pa = _FakePA()
    desktop._pyautogui = lambda: fake_pa

    out = desktop.run("mouse_click", {"x": 100, "y": 200, "button": "left"})
    ok("mouse_click at valid coords records the call",
       ("click", 100, 200, 1, "left") in fake_pa.calls, out)
    out = desktop.run("mouse_click", {"x": 5000, "y": 5000})
    ok("off-screen click is refused",
       "off-screen" in out and ("click", 5000, 5000, 1, "left") not in fake_pa.calls)
    out = desktop.run("mouse_click", {"x": 100, "y": 100, "button": "elbow"})
    ok("unknown mouse button errors", "button must be" in out)

    out = desktop.run("mouse_move", {"x": 200, "y": 300, "duration": 0.5})
    ok("mouse_move records the call",
       ("moveTo", 200, 300, 0.5) in fake_pa.calls)
    out = desktop.run("mouse_scroll", {"clicks": -3})
    ok("mouse_scroll records the call", ("scroll", -3) in fake_pa.calls)
    out = desktop.run("mouse_scroll", {"clicks": 999})
    ok("mouse_scroll clamps at ±20", ("scroll", 20) in fake_pa.calls)

    out = desktop.run("type_text", {"text": "hello"})
    ok("short type_text just types",
       any(c[0] == "typewrite" and c[1] == "hello" for c in fake_pa.calls))
    out = desktop.run("type_text", {"text": "x" * 100})
    ok("type_text >60 chars refuses without confirm",
       "call again with confirm=true" in out
       and not any(c[0] == "typewrite" and c[1] == "x" * 100 for c in fake_pa.calls))
    out = desktop.run("type_text", {"text": "x" * 100, "confirm": True})
    ok("type_text >60 chars proceeds with confirm=true",
       any(c[0] == "typewrite" and c[1] == "x" * 100 for c in fake_pa.calls))
    out = desktop.run("type_text", {"text": "line1\nline2"})
    ok("type_text with newline refuses without confirm", "newlines" in out)

    out = desktop.run("key_press", {"keys": "enter"})
    ok("single key press", ("press", "enter") in fake_pa.calls)
    out = desktop.run("key_press", {"keys": "ctrl+shift+p"})
    ok("hotkey combo", ("hotkey", ("ctrl", "shift", "p")) in fake_pa.calls)
    out = desktop.run("key_press", {"keys": "ctrl+launch_missile"})
    ok("out-of-allowlist key refused", "unknown key" in out)
    out = desktop.run("key_press", {"keys": "F5"})
    ok("F-keys accepted (case-insensitive)", ("press", "f5") in fake_pa.calls)

    section("notify + capture_webcam — degrade gracefully when the underlying lib is missing")
    # Force `from plyer import notification` to raise so we hit the fallback branch.
    import builtins
    _real_import = builtins.__import__
    def _blocking_import(name, *a, **k):
        if name == "plyer" or name.startswith("plyer."):
            raise ImportError("simulated plyer-missing")
        if name == "cv2":
            raise ImportError("simulated opencv-missing")
        return _real_import(name, *a, **k)
    builtins.__import__ = _blocking_import
    try:
        out = desktop.run("notify", {"title": "hi", "message": "there"})
        ok("notify without plyer returns a helpful install hint",
           "plyer not installed" in out and "pip install plyer" in out, out)
        out = desktop.run("capture_webcam", {})
        ok("capture_webcam without opencv returns a clear error",
           "opencv not available" in out, out)
    finally:
        builtins.__import__ = _real_import

    section("dispatch — new action names are all recognized")
    for new_act in ("system_volume", "brightness", "toggle_wifi", "mouse_click",
                    "mouse_move", "mouse_scroll", "type_text", "key_press",
                    "notify", "capture_webcam",
                    # p2 additions
                    "window_focus", "window_minimize", "window_maximize",
                    "window_restore", "window_close", "window_list", "remind"):
        _reset()
        out = desktop.run(new_act, {})
        ok(f"{new_act:15s} is dispatched (not 'unknown action')",
           "unknown action" not in out, out[:80])

    # ── p2: media keys allowlist ──
    section("media keys are in the key_press allowlist")
    for k in ("playpause", "nexttrack", "prevtrack", "stop",
              "volumeup", "volumedown", "volumemute"):
        _reset()
        # fake_pa was installed earlier; re-install a quick stub so keypress records.
        class _MK:
            def __init__(self): self.calls = []; self.FAILSAFE = True
            def press(self, k): self.calls.append(k)
            def hotkey(self, *_): pass
        _mk = _MK()
        desktop._pyautogui = lambda mk=_mk: mk
        out = desktop.run("key_press", {"keys": k})
        ok(f"{k:12s} accepted by key_press", k in _mk.calls, out[:80])

    # ── p2: reminder time parser ──
    section("reminder time parsing (offline, deterministic)")
    import reminder
    from datetime import datetime as _dt, timedelta as _td
    now = _dt(2026, 7, 15, 12, 0, 0)
    ok("'in 5 minutes' → +5m",
       reminder.parse_when("in 5 minutes", now=now) == now + _td(minutes=5))
    ok("'in 2 hours' → +2h",
       reminder.parse_when("in 2 hours", now=now) == now + _td(hours=2))
    ok("'tomorrow 9am' → next day 09:00",
       reminder.parse_when("tomorrow 9am", now=now)
       == _dt(2026, 7, 16, 9, 0))
    ok("'today 15:00' → today 15:00",
       reminder.parse_when("today 15:00", now=now) == _dt(2026, 7, 15, 15, 0))
    ok("bare '15:30' → today 15:30 (future)",
       reminder.parse_when("15:30", now=now) == _dt(2026, 7, 15, 15, 30))
    ok("bare '09:00' when past → tomorrow 09:00 (rollover)",
       reminder.parse_when("09:00", now=now) == _dt(2026, 7, 16, 9, 0))
    ok("ISO datetime parses",
       reminder.parse_when("2026-08-01T10:30", now=now) == _dt(2026, 8, 1, 10, 30))
    ok("garbage returns None", reminder.parse_when("nonsense", now=now) is None)
    ok("empty returns None", reminder.parse_when("", now=now) is None)

    # ── p2: reminder schedule refuses past + missing message ──
    section("reminder schedule: input validation")
    r = reminder.schedule(when="in 5 minutes", message="")
    ok("empty message refused", r.get("ok") is False and "message" in r.get("error", ""), str(r))
    r = reminder.schedule(when="garbage", message="do X")
    ok("unparseable when refused",
       r.get("ok") is False and "could not parse" in r.get("error", ""), str(r))
    r = reminder.schedule(when="2020-01-01T09:00", message="do X")
    ok("past when refused", r.get("ok") is False and "past" in r.get("error", ""), str(r))

    # ── p2: window_list uses pyautogui.getAllWindows ──
    section("window_focus disambiguation")
    class _Win:
        def __init__(self, title): self.title = title; self.actions = []
        def activate(self): self.actions.append("activate")
        def minimize(self): self.actions.append("minimize")
        def maximize(self): self.actions.append("maximize")
        def restore(self): self.actions.append("restore")
        def close(self): self.actions.append("close")
    class _FakePAW:
        FAILSAFE = True
        def __init__(self, wins): self.wins = wins
        def getAllWindows(self): return self.wins
        def size(self): return (1920, 1080)
    _fake_pa_2 = _FakePAW([_Win("Notepad"), _Win("VS Code"), _Win("Chrome - GitHub")])
    desktop._pyautogui = lambda pa=_fake_pa_2: pa
    out = desktop.run("window_focus", {"title": "Notepad"})
    ok("single match → activate", _fake_pa_2.wins[0].actions == ["activate"], out)
    out = desktop.run("window_focus", {"title": "notepad"})
    ok("substring case-insensitive match works",
       _fake_pa_2.wins[0].actions == ["activate", "activate"], out)
    out = desktop.run("window_focus", {"title": "nonexistent"})
    ok("no match errors, does not activate", "no window title matched" in out)
    _fake_pa_3 = _FakePAW([_Win("Chrome — GitHub"), _Win("Chrome — Notion")])
    desktop._pyautogui = lambda pa=_fake_pa_3: pa
    out = desktop.run("window_focus", {"title": "chrome"})
    ok("multi-match refuses and lists matches",
       "2 windows matched" in out and "Chrome" in out
       and _fake_pa_3.wins[0].actions == [] and _fake_pa_3.wins[1].actions == [])

    passed = sum(1 for _, c, _ in _checks if c)
    total = len(_checks)
    print()
    if passed == total:
        print(f"ALL {total} CHECKS PASSED")
        return 0
    for label, cond, hint in _checks:
        if not cond:
            print(f"  FAIL: {label}   ({hint})")
    print(f"\n{passed}/{total} passed")
    return 1


if __name__ == "__main__":
    sys.exit(run())
