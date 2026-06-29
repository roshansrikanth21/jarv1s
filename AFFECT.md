# JARVIS — Affect, Perception & Ambient awareness

Three small, dependency-free modules that give JARVIS a *temperament*, the ability
to *read the room*, and a sense of its *surroundings*. They feed one thing: the
system prompt that wraps every brain call — so JARVIS's register (how dry, how
playful, how curt) and its grounding (time, place, weather) shift with the moment
instead of being a frozen string.

```
   mic / text ──► perception.analyze() ──► PAD nudge ──► persona.apply()
                        (reads the user)                      │
   ambient.snapshot() ─────────────────────────────┐         ▼  persona.tick()  (decay)
   (time · place · weather)                         │   _build_system_prompt()
                                                     └──────────►  one live prompt  ──► Brain
```

Everything is **off-by-default-safe**: set `JARVIS_EMOTION=0` and JARVIS reverts to
the original static persona. Nothing here can raise into the request path, and no
network call blocks a reply (ambient runs on a background refresher + cache).

---

## 1. `persona.py` — the PAD emotion engine

JARVIS's mood is a point in **PAD space** (Pleasure · Arousal · Dominance — the
Mehrabian/Russell model of affect), each axis in `[-1, 1]`.

- **Temperament (baseline).** Where he sits at rest: high *dominance* (confident, in
  command), faintly positive *pleasure* (dry amusement, not gloom), modest *arousal*
  (alert, unhurried). The `JARVIS_SARCASM` setting tilts the baseline — `playful` is
  warmer and less domineering, `savage` is cooler and pushier.
- **Mood.** Starts at baseline and gets **nudged** by events (a PAD delta from
  perception). Each nudge is leashed so he can't be flung more than `MAX_SWING` from
  baseline — he stays recognisably JARVIS even when provoked.
- **The decay timer.** `tick()` relaxes the mood back toward baseline by however much
  real time has passed — exponential, with a configurable **half-life** (default
  6 min). Provoke him and he cools off; leave him and he returns home. On restart the
  saved mood is decayed forward by the elapsed time, so after a night away he wakes at
  baseline rather than stuck in last night's mood.
- **Naming.** The current mood is labelled by its nearest of ten prototypes
  (`buoyant, amused, smug, content, focused, deadpan, prickly, rattled, bored, weary`),
  each carrying a one-line "colour" that tints the reply. **Intensity** = how far the
  mood has been pushed from baseline → how strongly it colours.
- **Output.** `style_block()` renders the `[Affect]` section of the system prompt:
  character line + live mood + the per-turn read on the user. `tts_bias()` adds a
  barely-there voice nudge (higher arousal speaks a hair faster; a sour mood drops
  pitch a touch).

State persists to `memory/jarvis_persona.json`.

## 2. `perception.py` — perceptual intelligence

STT (Groq Whisper) tells us *what* was said; this tells us *how*, and what the user
wants emotionally. Cheap, transparent, pure-Python signals over the transcript
(plus an optional **loudness hint** carried from the mic for voice turns):

frustration · stress/urgency · low mood · insult-at-JARVIS · gratitude/praise ·
playful banter · excitement · greeting-vs-time mismatch · plain command/question.

It returns a `user_state`, rough valence/arousal/politeness, a **PAD nudge** for the
persona, and a one-line **guidance** the prompt acts on.

**The safety valve.** Genuine distress (frustrated / stressed / low) **always
outranks** comedy and sets `suppress_sarcasm` → JARVIS drops the jokes and just
helps. Sarcasm is for good moods, never for someone having a bad time. This is what
keeps "playful" from ever landing as "offensive".

## 3. `ambient.py` — situational awareness

Three keyless signals, cached with TTLs, short timeouts, graceful offline fallback
to "time only":

- **Time of day** from the user's *local* clock (resolved via their timezone).
- **Location** — city/region/country via IP geolocation (`ip-api.com`). Pin a city
  with `JARVIS_HOME_CITY` to skip the lookup.
- **Weather** — current conditions via **Open-Meteo** (WMO code → plain English).

Surfaces as the `[Surroundings]` prompt line and powers the new **`get_weather`**
tool. The local hour also feeds perception's greeting-mismatch check — which is how
"good morning" at 1 AM earns a sanity-check instead of a straight reply.

---

## Worked example

> **You (1:14 AM):** "good morning jarvis"
> *perception:* `mismatch`, not distress → jokes allowed.
> *persona:* pleasure + dominance nudged up → mood reads **smug**.
> *ambient:* it's 1:14 AM.
> **JARVIS:** "Morning? It's quarter past one. Either your clock's broken or your
> sleep schedule is — what do you need?"

> **You (later, tired):** "ugh this still isn't working, again"
> *perception:* `frustrated` → `suppress_sarcasm=True`.
> **JARVIS:** drops the bit, gets crisp, fixes the thing.

## Knobs

See `.env.example` → *Affect* and *Ambient* blocks. Highlights:
`JARVIS_EMOTION` (master switch) · `JARVIS_SARCASM=playful|sharp|savage` ·
`JARVIS_EMOTION_HALFLIFE_MIN` · `JARVIS_HOME_CITY`.

## Tests

`python scripts/selftest_affect.py` — 27 offline assertions over decay, nudges,
clamping, persistence, every perception class, the distress-beats-jokes guarantee,
and ambient rendering. No network, no keys.
