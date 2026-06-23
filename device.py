"""
device.py — the agent's "body": hardware capability + live power/thermal state.

Windows-first, minimal deps (stdlib + psutil). GPU VRAM uses nvidia-smi when an
NVIDIA card is present, else the 64-bit registry value `qwMemorySize` (the modern
`Win32_VideoController.AdapterRAM` is a uint32 that caps at ~4 GB and `wmic` is
gone on Win11). Every probe is wrapped so one missing tool never breaks profiling.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

import psutil

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_static_cache: dict | None = None


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=_NO_WINDOW)
        return out.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _nvidia_gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform == "win32":
        cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "nvidia-smi.exe")
        exe = cand if os.path.exists(cand) else None
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            try:
                gpus.append({"name": parts[0], "vram_mb": int(float(parts[1])), "vendor": "NVIDIA"})
            except ValueError:
                gpus.append({"name": parts[0], "vram_mb": None, "vendor": "NVIDIA"})
    return gpus


def _windows_gpus_registry() -> list[dict]:
    if sys.platform != "win32":
        return []
    import winreg
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    gpus = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        if not sub.isdigit():
            continue
        try:
            k = winreg.OpenKey(root, sub)
        except OSError:
            continue
        name = vram = None
        try:
            name, _ = winreg.QueryValueEx(k, "DriverDesc")
        except OSError:
            pass
        try:
            vram, _ = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")
        except OSError:
            vram = None
        winreg.CloseKey(k)
        if name:
            gpus.append({
                "name": name,
                "vram_mb": int(vram) // (1024 ** 2) if vram else None,
                "vendor": "integrated" if (not vram or int(vram) == 0) else "discrete",
            })
    winreg.CloseKey(root)
    return gpus


def _gpus() -> list[dict]:
    nv = _nvidia_gpus()
    if nv:
        return nv
    if sys.platform == "win32":
        return _windows_gpus_registry()
    if sys.platform == "darwin" and platform.machine() == "arm64":
        # Apple Silicon: unified memory; the GPU shares system RAM.
        total = round(psutil.virtual_memory().total / 1024 ** 3, 1)
        return [{"name": _mac_chip() or "Apple GPU", "vram_mb": int(total * 1024), "vendor": "apple"}]
    return []


def _mac_chip() -> str | None:
    out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    return out.strip() or None


def _cpu_name() -> str:
    if sys.platform == "darwin":
        return _mac_chip() or platform.processor() or "unknown"
    return platform.processor() or platform.uname().processor or "unknown"


def cpu_temp_c() -> float | None:
    """Best-effort CPU temperature. Linux returns real °C; Windows returns None
    (no unprivileged API) — the Governor degrades gracefully without it."""
    fn = getattr(psutil, "sensors_temperatures", None)
    if fn is None:
        return None
    try:
        temps = fn()
    except Exception:
        return None
    if not temps:
        return None
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
        if temps.get(key):
            return round(temps[key][0].current, 1)
    first = next(iter(temps.values()), None)
    return round(first[0].current, 1) if first else None


def _battery() -> dict | None:
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    if b is None:
        return None
    return {"percent": round(b.percent), "plugged": bool(b.power_plugged)}


def _tier(ram_gb: float, vram_mb: int) -> str:
    if vram_mb >= 16000 or ram_gb >= 48:
        return "workstation"
    if vram_mb >= 8000 or ram_gb >= 24:
        return "heavy"
    if vram_mb >= 4000 or ram_gb >= 12:
        return "balanced"
    if ram_gb >= 6:
        return "light"
    return "potato"


def profile() -> dict:
    """Full device profile: static specs (cached) + live power/load/thermal + a
    coarse capability tier and a 0..1 'headroom' estimate of free compute."""
    global _static_cache
    if _static_cache is None:
        vm = psutil.virtual_memory()
        try:
            freq = psutil.cpu_freq()
            freq_mhz = round(freq.max or freq.current) if freq else None
        except Exception:
            freq_mhz = None
        _static_cache = {
            "os": f"{platform.system()} {platform.release()}",
            "cpu": {"name": _cpu_name(),
                    "physical": psutil.cpu_count(logical=False),
                    "logical": psutil.cpu_count(logical=True),
                    "freq_mhz": freq_mhz},
            "ram_gb": round(vm.total / 1024 ** 3, 1),
            "gpus": _gpus(),
        }

    prof = {**_static_cache, "gpus": list(_static_cache["gpus"])}
    vm = psutil.virtual_memory()
    bat = _battery()
    temp = cpu_temp_c()
    prof["ram_available_gb"] = round(vm.available / 1024 ** 3, 1)
    prof["ram_percent"] = vm.percent
    prof["cpu_percent"] = psutil.cpu_percent(interval=0)
    prof["cpu_temp_c"] = temp
    prof["battery"] = bat
    prof["power_state"] = "battery" if (bat and not bat["plugged"]) else "ac"

    vram = max((g.get("vram_mb") or 0 for g in prof["gpus"]), default=0)
    prof["vram_mb"] = vram
    prof["tier"] = _tier(prof["ram_gb"], vram)

    # Headroom: how much compute is free to spend right now (1 = wide open).
    head = 1.0
    head *= 1.0 - 0.6 * (prof["cpu_percent"] / 100.0)
    head *= 0.5 + 0.5 * min(1.0, prof["ram_available_gb"] / 8.0)
    if prof["power_state"] == "battery":
        head *= 0.4 + 0.4 * ((bat["percent"] / 100.0) if bat else 1.0)
    if isinstance(temp, (int, float)) and temp > 80:
        head *= 0.6
    prof["headroom"] = round(max(0.0, min(1.0, head)), 2)
    return prof
