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

# Force UTF-8 on stdout so unicode arrows in labels don't crash on the Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
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

    desktop._spawn = fake_spawn
    desktop._run = fake_run
    # Force IS_WINDOWS true so we exercise the real branches even if running on non-Windows.
    desktop.IS_WINDOWS = True


def _reset() -> None:
    _spawn_calls.clear()
    _run_calls.clear()
    _run_responses.clear()


def _last_spawn() -> list[str]:
    return _spawn_calls[-1] if _spawn_calls else []


def run() -> int:
    _install_stubs()

    section("dispatch — unknown action")
    _reset()
    out = desktop.run("nope", {})
    ok("unknown action returns clear error", "unknown action" in out and "Available" in out, out)
    ok("unknown action never spawned anything", not _spawn_calls and not _run_calls)

    section("open_path — Explorer")
    _reset()
    # A path that definitely exists on any Windows host (or wherever this runs).
    real = str(Path(_REPO).resolve())
    out = desktop.run("open_path", {"path": real})
    ok("open_path spawns explorer.exe with the resolved path",
       _last_spawn()[:1] == ["explorer.exe"] and _last_spawn()[-1] == real, str(_last_spawn()))
    ok("open_path reports what it opened", "Opened Explorer at:" in out)

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
