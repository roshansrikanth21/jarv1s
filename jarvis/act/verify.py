"""verify.py — did the OS action ACTUALLY happen?

JARVIS must never say "Done" on faith. Launching a process only proves a process was
*spawned*, not that it's alive a beat later (a bad path, a crash-on-start, an installer
stub that exits immediately all "succeed" at Popen). Closing a window by title proves
nothing about whether the program is gone. These helpers answer the only question that
matters for an honest reply: is the process actually running now, or actually gone now?

All process matching is by executable basename, case-insensitive (e.g. "notepad.exe"),
because that's what the launch allowlist speaks and what survives per-user install paths.
Everything here degrades to a conservative "unverified" rather than raising — a missing
psutil or a permission error must never turn a real success into a crash, nor a real
failure into a fabricated success.
"""
from __future__ import annotations

import os
import time

# A verification result that reads like an answer, not a status code. `verified` is the
# honest bit: True only when we positively observed the expected state; False when we
# positively observed the opposite; None when we genuinely couldn't tell (no psutil, access
# denied) — the caller must then hedge, never claim success.
class VerifyResult:
    __slots__ = ("ok", "verified", "detail")

    def __init__(self, ok: bool, verified: bool | None, detail: str = ""):
        self.ok = ok                # did the thing we wanted end up true?
        self.verified = verified    # True observed / False observed-opposite / None unknown
        self.detail = detail

    def __bool__(self) -> bool:
        return bool(self.ok)

    def __repr__(self) -> str:
        return f"VerifyResult(ok={self.ok}, verified={self.verified}, detail={self.detail!r})"


def _basename(exe: str) -> str:
    """'C:\\...\\Notepad.exe' | 'notepad' | 'code' -> comparable lowercase basename."""
    b = os.path.basename((exe or "").strip().strip('"')).lower()
    return b


def _candidates(exe: str) -> set[str]:
    """Names a process might report for this target. We match the bare stem too, since some
    apps report 'Code.exe' while the allowlist says 'code', and Store aliases vary in case."""
    b = _basename(exe)
    out = {b}
    if b.endswith(".exe"):
        out.add(b[:-4])
    else:
        out.add(b + ".exe")
    return {c for c in out if c}


def _iter_proc_names():
    """Yield lowercase process names, or raise if psutil is unavailable."""
    import psutil  # local import: a missing psutil degrades to None, never a hard crash
    for p in psutil.process_iter(["name"]):
        try:
            nm = (p.info.get("name") or "").lower()
        except Exception:
            continue
        if nm:
            yield nm


def running_pids(exe: str):
    """PIDs whose process name matches `exe` (by basename). Empty list = none found.
    Returns None if we can't inspect processes at all (no psutil / access denied)."""
    try:
        import psutil
    except Exception:
        return None
    want = _candidates(exe)
    pids: list[int] = []
    try:
        for p in psutil.process_iter(["name", "pid"]):
            try:
                nm = (p.info.get("name") or "").lower()
            except Exception:
                continue
            if nm in want:
                pids.append(int(p.info.get("pid") or 0))
    except Exception:
        return None
    return pids


def is_running(exe: str) -> bool | None:
    """True / False if we can tell, None if we can't inspect processes."""
    pids = running_pids(exe)
    if pids is None:
        return None
    return len(pids) > 0


def wait_until_running(exe: str, timeout: float = 2.5, poll: float = 0.1) -> VerifyResult:
    """Poll until `exe` appears, up to `timeout` seconds. Used right after a launch so we only
    claim success once the process is genuinely visible. If we can't inspect processes at all,
    returns ok=True/verified=None (the launch itself didn't error — we just can't confirm)."""
    deadline = time.monotonic() + max(0.0, timeout)
    unknown_seen = False
    while time.monotonic() < deadline:
        r = is_running(exe)
        if r is True:
            return VerifyResult(True, True, f"{_basename(exe)} is running")
        if r is None:
            unknown_seen = True
            break
        time.sleep(poll)
    if unknown_seen:
        return VerifyResult(True, None, "could not inspect running processes")
    return VerifyResult(False, False, f"{_basename(exe)} did not appear after {timeout:.1f}s")


def wait_until_gone(exe: str, timeout: float = 4.0, poll: float = 0.1) -> VerifyResult:
    """Poll until `exe` is no longer running, up to `timeout` seconds. Used after a close/kill
    so we only claim it closed once the process is genuinely gone."""
    deadline = time.monotonic() + max(0.0, timeout)
    last: bool | None = None
    while time.monotonic() < deadline:
        r = is_running(exe)
        if r is False:
            return VerifyResult(True, True, f"{_basename(exe)} is closed")
        if r is None:
            return VerifyResult(True, None, "could not inspect running processes")
        last = r
        time.sleep(poll)
    if last is True:
        return VerifyResult(False, False, f"{_basename(exe)} is still running after {timeout:.1f}s")
    return VerifyResult(True, None, "could not confirm")


def terminate(exe: str, timeout: float = 4.0) -> VerifyResult:
    """Gracefully close every process matching `exe`, then verify it's gone. Tries terminate()
    first (lets the app close cleanly / flush unsaved state to its own prompt), escalating to
    kill() only for stragglers. Returns an honest VerifyResult — ok=False if anything survives,
    verified=None only when we truly can't inspect processes."""
    try:
        import psutil
    except Exception:
        return VerifyResult(False, None, "psutil unavailable — cannot close by process")
    pids = running_pids(exe)
    if pids is None:
        return VerifyResult(False, None, "could not inspect running processes")
    if not pids:
        return VerifyResult(True, True, f"{_basename(exe)} was not running")
    procs: list = []
    for pid in pids:
        try:
            procs.append(psutil.Process(pid))
        except Exception:
            pass
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=max(0.5, timeout * 0.6))
    for p in alive:
        try:
            p.kill()   # straggler — force it
        except Exception:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=max(0.5, timeout * 0.4))
    return wait_until_gone(exe, timeout=1.0)
