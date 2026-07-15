"""
system_monitor.py — Proactive hardware telemetry with cooldown-gated alerts.

Adapted from Mark-XLVII's pattern: background checks, streak guard for CPU,
5-minute cooldown per metric type. Returns speakable alert text for JARVIS TTS.
"""
from __future__ import annotations

import platform
import subprocess
import time

import psutil

_OS = platform.system()
_COOLDOWN_SEC = 300
_CPU_STREAK_NEEDED = 3

DEFAULT_THRESHOLDS = {
    "cpu": 90.0,
    "ram": 90.0,
    "temp": 85.0,
    "gpu": 95.0,
}


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _gpu_usage() -> float:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, creationflags=_NO_WINDOW,
        )
        if r.returncode == 0:
            vals = [float(v.strip()) for v in r.stdout.strip().split("\n") if v.strip()]
            if vals:
                return sum(vals) / len(vals)
    except Exception:
        pass
    return -1.0


def _cpu_temp() -> float:
    try:
        temps = psutil.sensors_temperatures()
        for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "cpu-thermal"):
            if name in temps and temps[name]:
                return float(temps[name][0].current)
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
    except Exception:
        pass
    if _OS == "Windows":
        try:
            r = subprocess.run(
                [
                    "powershell", "-Command",
                    "(Get-WmiObject MSAcpi_ThermalZoneTemperature "
                    "-Namespace root/wmi).CurrentTemperature",
                ],
                capture_output=True, text=True, timeout=3, creationflags=_NO_WINDOW,
            )
            if r.returncode == 0 and r.stdout.strip():
                raw = float(r.stdout.strip().split("\n")[0])
                return (raw / 10.0) - 273.15
        except Exception:
            pass
    return -1.0


def snapshot() -> dict:
    cpu = psutil.cpu_percent(interval=0.15)
    ram = psutil.virtual_memory()
    temp = _cpu_temp()
    gpu = _gpu_usage()
    return {
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(ram.percent, 1),
        "cpu_temp_c": round(temp, 1) if temp > 0 else None,
        "gpu_percent": round(gpu, 1) if gpu >= 0 else None,
    }


class SystemMonitor:
    """Stateful monitor — call check() every ~10s from the api lifespan loop."""

    def __init__(self, thresholds: dict | None = None):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN_SEC

    def _record(self, key: str) -> None:
        self._last_alert[key] = time.monotonic()

    def check(self) -> dict | None:
        """
        Returns None if healthy, else:
        {severity, metric, speak, detail}
        """
        try:
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            temp = _cpu_temp()
            gpu = _gpu_usage()
        except Exception:
            return None

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK_NEEDED and self._can_alert("cpu"):
                self._record("cpu")
                self._cpu_streak = 0
                return {
                    "severity": "warn",
                    "metric": "cpu",
                    "speak": f"Sir, CPU load is critically high at {cpu:.0f} percent. "
                             "Consider closing heavy applications.",
                    "detail": f"CPU {cpu:.0f}% sustained",
                }
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            self._record("ram")
            return {
                "severity": "warn",
                "metric": "ram",
                "speak": f"Sir, memory is at {ram:.0f} percent. You may want to free some RAM.",
                "detail": f"RAM {ram:.0f}%",
            }

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            self._record("temp")
            return {
                "severity": "critical",
                "metric": "temp",
                "speak": f"Sir, CPU temperature is {temp:.0f} degrees. "
                         "I'd reduce load or check cooling.",
                "detail": f"CPU temp {temp:.0f}°C",
            }

        if gpu >= 0 and gpu >= self.thresholds["gpu"] and self._can_alert("gpu"):
            self._record("gpu")
            return {
                "severity": "warn",
                "metric": "gpu",
                "speak": f"Sir, GPU utilization is at {gpu:.0f} percent.",
                "detail": f"GPU {gpu:.0f}%",
            }

        return None
