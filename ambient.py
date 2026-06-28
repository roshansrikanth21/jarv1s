#!/usr/bin/env python3
"""
JARVIS — Ambient / situational awareness (where, when, what's it like out).

Gives JARVIS a sense of its surroundings so its answers can be grounded ("it's
gone midnight", "it's raining — take a coat") and its wit can land ("good morning?
it's 1am"). Three cheap, keyless signals:

  * Time of day  — from the user's *local* clock (resolved via their timezone).
  * Location     — city/region/country via IP geolocation (ip-api.com, no key).
  * Weather      — current conditions via Open-Meteo (no key).

Everything is cached with a TTL and every network call is wrapped + short-timeout,
so a flaky connection just means JARVIS falls back to "time only" — it never hangs
or raises into the request path. Network work should be driven from a background
refresh; `snapshot()` itself is non-blocking and always returns at least the clock.

Override: set JARVIS_HOME_CITY to pin a city (skips the IP lookup); weather then
comes from geocoding that name. Otherwise location is detected automatically.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _HAS_TZ = True
except Exception:                       # pragma: no cover
    _HAS_TZ = False

HOME_CITY   = os.environ.get("JARVIS_HOME_CITY", "").strip()
GEO_TTL     = int(os.environ.get("JARVIS_GEO_TTL_SEC", str(6 * 3600)))    # 6 h
WX_TTL      = int(os.environ.get("JARVIS_WEATHER_TTL_SEC", "1200"))       # 20 min
HTTP_TIMEOUT = float(os.environ.get("JARVIS_AMBIENT_TIMEOUT", "4"))

_LOCK = threading.Lock()
_CACHE: dict = {"geo": None, "geo_at": 0.0, "wx": None, "wx_at": 0.0}

# WMO weather codes -> (short label, casual descriptor)
_WMO = {
    0: ("clear", "clear skies"), 1: ("mostly clear", "mostly clear"),
    2: ("partly cloudy", "partly cloudy"), 3: ("overcast", "grey and overcast"),
    45: ("fog", "foggy"), 48: ("freezing fog", "freezing fog"),
    51: ("light drizzle", "spitting a little"), 53: ("drizzle", "drizzly"),
    55: ("heavy drizzle", "heavy drizzle"), 56: ("freezing drizzle", "freezing drizzle"),
    57: ("freezing drizzle", "freezing drizzle"),
    61: ("light rain", "a bit of rain"), 63: ("rain", "raining"),
    65: ("heavy rain", "chucking it down"), 66: ("freezing rain", "freezing rain"),
    67: ("freezing rain", "freezing rain"),
    71: ("light snow", "light snow"), 73: ("snow", "snowing"),
    75: ("heavy snow", "heavy snow"), 77: ("snow grains", "snow grains"),
    80: ("rain showers", "passing showers"), 81: ("rain showers", "showery"),
    82: ("violent showers", "torrential showers"),
    85: ("snow showers", "snow showers"), 86: ("snow showers", "heavy snow showers"),
    95: ("thunderstorm", "thundery"), 96: ("thunderstorm w/ hail", "thunderstorms and hail"),
    99: ("thunderstorm w/ hail", "violent thunderstorms and hail"),
}


def _http_json(url: str, timeout: float = HTTP_TIMEOUT):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0 (ambient)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None


# ── Location ─────────────────────────────────────────────────────────────────────
def _geocode_city(name: str) -> Optional[dict]:
    """Resolve a city name -> location dict via Open-Meteo geocoding (keyless)."""
    q = urllib.parse.quote(name)
    data = _http_json(f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json")
    res = (data or {}).get("results") or []
    if not res:
        return None
    g = res[0]
    return {"city": g.get("name"), "region": g.get("admin1"), "country": g.get("country"),
            "country_code": g.get("country_code"), "lat": g.get("latitude"),
            "lon": g.get("longitude"), "tz": g.get("timezone"), "source": "config"}


def _ip_locate() -> Optional[dict]:
    """Detect location from the public IP (ip-api.com, keyless, http)."""
    data = _http_json("http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,lat,lon,timezone")
    if not data or data.get("status") != "success":
        return None
    return {"city": data.get("city"), "region": data.get("regionName"),
            "country": data.get("country"), "country_code": data.get("countryCode"),
            "lat": data.get("lat"), "lon": data.get("lon"),
            "tz": data.get("timezone"), "source": "ip"}


def geolocate(force: bool = False) -> Optional[dict]:
    """Cached location. HOME_CITY (if set) wins; otherwise auto IP geolocation."""
    now = time.time()
    with _LOCK:
        if not force and _CACHE["geo"] and (now - _CACHE["geo_at"] < GEO_TTL):
            return _CACHE["geo"]
    geo = _geocode_city(HOME_CITY) if HOME_CITY else _ip_locate()
    if geo and geo.get("lat") is not None:
        with _LOCK:
            _CACHE["geo"], _CACHE["geo_at"] = geo, now
        return geo
    with _LOCK:                          # serve stale rather than nothing
        return _CACHE["geo"]


# ── Weather ──────────────────────────────────────────────────────────────────────
def weather(lat: float, lon: float, force: bool = False) -> Optional[dict]:
    now = time.time()
    with _LOCK:
        if not force and _CACHE["wx"] and (now - _CACHE["wx_at"] < WX_TTL):
            return _CACHE["wx"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
           "weather_code,wind_speed_10m,is_day&timezone=auto")
    data = _http_json(url)
    cur = (data or {}).get("current")
    if not cur:
        with _LOCK:
            return _CACHE["wx"]
    code = int(cur.get("weather_code", -1))
    label, casual = _WMO.get(code, ("unsettled", "hard to say"))
    wx = {
        "temp_c": cur.get("temperature_2m"),
        "feels_c": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "is_day": bool(cur.get("is_day", 1)),
        "code": code, "label": label, "casual": casual,
        "tz": (data or {}).get("timezone"),
    }
    with _LOCK:
        _CACHE["wx"], _CACHE["wx_at"] = wx, now
    return wx


# ── Time of day ──────────────────────────────────────────────────────────────────
def _tz_name() -> Optional[str]:
    geo = _CACHE.get("geo")
    if geo and geo.get("tz"):
        return geo["tz"]
    wx = _CACHE.get("wx")
    if wx and wx.get("tz"):
        return wx["tz"]
    return None


def local_now() -> tuple[datetime, str]:
    """User's local datetime (via their timezone if known) + the tz label."""
    tz = _tz_name()
    if tz and _HAS_TZ:
        try:
            return datetime.now(ZoneInfo(tz)), tz
        except Exception:
            pass
    return datetime.now(), "system"


def time_of_day(hour: int) -> tuple[str, str]:
    """(bucket, friendly label) for an hour 0-23."""
    if hour < 5:   return "late_night", "the dead of night"
    if hour < 7:   return "early_morning", "early morning"
    if hour < 12:  return "morning", "morning"
    if hour < 17:  return "afternoon", "afternoon"
    if hour < 21:  return "evening", "evening"
    return "night", "night"


# ── Snapshot + rendering ─────────────────────────────────────────────────────────
def snapshot() -> dict:
    """Non-blocking: always-fresh local clock + whatever location/weather is cached."""
    dt, tz = local_now()
    hour = dt.hour
    bucket, tod_label = time_of_day(hour)
    with _LOCK:
        geo = _CACHE["geo"]
        wx = _CACHE["wx"]
    return {
        "hour": hour,
        "time_str": dt.strftime("%I:%M %p").lstrip("0"),
        "date_str": dt.strftime("%A, %B %d %Y"),
        "tod_bucket": bucket,
        "tod_label": tod_label,
        "tz": tz,
        "location": geo,
        "weather": wx,
    }


def refresh() -> dict:
    """Blocking: refresh location then weather (call from a background thread)."""
    geo = geolocate()
    if geo and geo.get("lat") is not None:
        weather(geo["lat"], geo["lon"])
    return snapshot()


def prompt_fragment(snap: Optional[dict] = None) -> str:
    """A compact ambient line for the system prompt."""
    s = snap or snapshot()
    loc = s.get("location") or {}
    wx = s.get("weather") or {}
    where = ""
    if loc.get("city"):
        where = f" in {loc['city']}"
        if loc.get("country_code"):
            where += f", {loc['country_code']}"
    wxbit = ""
    if wx.get("temp_c") is not None:
        wxbit = f" — {wx.get('casual', wx.get('label',''))}, {round(wx['temp_c'])}°C"
        if wx.get("feels_c") is not None and abs((wx["feels_c"] or 0) - (wx["temp_c"] or 0)) >= 3:
            wxbit += f" (feels {round(wx['feels_c'])}°)"
    return (f"[Surroundings] It's {s['time_str']} ({s['tod_label']}) on "
            f"{s['date_str']}{where}{wxbit}. Use this only when it actually helps the "
            f"answer or makes for a fair aside — don't recite it unprompted.")


def weather_report(city: str = "") -> str:
    """Spoken-friendly weather string for the get_weather tool."""
    if city:
        geo = _geocode_city(city)
        if not geo:
            return f"I couldn't find a place called {city}."
    else:
        geo = geolocate()
        if not geo:
            return ("I can't tell where you are right now — no location fix. "
                    "Tell me a city and I'll pull its weather.")
    wx = weather(geo["lat"], geo["lon"], force=True)
    if not wx or wx.get("temp_c") is None:
        return "I couldn't reach the weather service just now."
    place = geo.get("city") or "your area"
    parts = [f"In {place} it's {round(wx['temp_c'])} degrees and {wx.get('casual', wx['label'])}"]
    if wx.get("feels_c") is not None and abs(wx["feels_c"] - wx["temp_c"]) >= 3:
        parts.append(f"feels like {round(wx['feels_c'])}")
    if wx.get("humidity") is not None:
        parts.append(f"humidity {round(wx['humidity'])} percent")
    if wx.get("wind_kmh"):
        parts.append(f"wind {round(wx['wind_kmh'])} kilometres an hour")
    return ", ".join(parts) + "."


if __name__ == "__main__":
    # Offline-safe: parsing is exercised with injected data in tests; here we just
    # show the clock path + try a live refresh (prints whatever it can reach).
    print("clock-only snapshot:", json.dumps(snapshot(), default=str)[:200])
    print("live refresh ...")
    s = refresh()
    print("fragment:", prompt_fragment(s))
