#!/usr/bin/env python3
"""Offline self-test for JARVIS's affective layer (persona + perception + ambient).

Runs with no network and no API keys:  python scripts/selftest_affect.py
Exits non-zero on the first failed assertion. Safe to wire into CI.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import persona as persona_mod
import perception
import ambient

ok = 0
def check(cond, label):
    global ok
    assert cond, f"FAIL: {label}"
    ok += 1
    print(f"  ok  {label}")

print("persona — PAD engine")
p = persona_mod.Persona()
check(p.emotion() in {"content", "smug"}, "rests near baseline")
base_dom = p.mood.dominance
p.apply({"p": 0.45, "a": 0.45, "d": 0.1}, "joke")
check(p.snapshot()["pad"]["pleasure"] > 0.5, "a joke lifts pleasure")
check(p.intensity() > 0.4, "intensity rises when provoked")
dev_before = p.mood.dist(p.baseline)
p.updated -= persona_mod.HALFLIFE_MIN * 60          # one half-life passes
p.tick()
dev_after = p.mood.dist(p.baseline)
check(abs(dev_after - dev_before / 2) < 0.05, "decay halves the deviation over one half-life")
# clamp: hammering one axis can't fling JARVIS past MAX_SWING from baseline
for _ in range(50):
    p.apply({"p": -1, "a": 1, "d": 1}, "spam")
check(p.mood.dist(p.baseline) <= persona_mod.MAX_SWING * 1.8 + 0.01, "mood stays leashed to baseline")

print("persona — persistence")
import tempfile, pathlib
tf = pathlib.Path(tempfile.mkdtemp()) / "persona.json"
q = persona_mod.Persona(path=tf); q.apply({"p": 0.3, "a": 0.2, "d": 0.0}, "warmth"); q.save()
r = persona_mod.Persona.load(tf)
check(abs(r.mood.pleasure - q.mood.pleasure) < 0.2, "state survives a save/load round-trip")

print("perception — reading the user")
cases = {
    ("good morning jarvis", 1):        ("mismatch", False),
    ("this STILL doesnt work again!!", 14): ("frustrated", True),
    ("thanks man you're a lifesaver", 14):  ("grateful", False),
    ("lol you absolute smartass", 20):      ("playful", False),
    ("i'm exhausted and overwhelmed", 23):  ("down", True),
    ("open chrome", 10):                     ("commanding", False),
    ("shut up jarvis", 15):                  ("irritated", False),
    ("hurry up i need this asap", 9):        ("stressed", True),
}
for (text, hour), (state, suppress) in cases.items():
    r = perception.analyze(text, hour=hour, reask=("again" in text))
    check(r.user_state == state, f"{text!r:34} -> {state}")
    check(r.suppress_sarcasm == suppress, f"{text!r:34} -> suppress={suppress}")

print("perception — distress always outranks a joke (safety)")
r = perception.analyze("good morning, this is STILL broken again", hour=1, reask=True)
check(r.suppress_sarcasm is True, "frustration beats greeting-mismatch -> no jokes")

print("ambient — time / weather rendering (mocked data, no network)")
check(ambient.time_of_day(2)[0] == "late_night", "2am -> late_night")
check(ambient.time_of_day(14)[0] == "afternoon", "2pm -> afternoon")
check(ambient._WMO[65][0] == "heavy rain", "WMO 65 -> heavy rain")
ambient._CACHE.update(
    geo={"city": "Hyderabad", "country_code": "IN", "lat": 17.4, "lon": 78.5, "tz": "Asia/Kolkata"},
    geo_at=time.time(),
    wx={"temp_c": 31.0, "feels_c": 35.0, "humidity": 55, "wind_kmh": 9.0, "is_day": True,
        "code": 1, "label": "mostly clear", "casual": "mostly clear", "tz": "Asia/Kolkata"},
    wx_at=time.time())
frag = ambient.prompt_fragment()
check("Hyderabad" in frag and "31" in frag, "prompt fragment includes place + temperature")

print(f"\nALL {ok} CHECKS PASSED")
