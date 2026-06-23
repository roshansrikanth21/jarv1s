#!/usr/bin/env python3
"""Generate the JARVIS desktop icon — an arc-reactor mark on a dark warm field.
Renders at 4x then downsamples for clean anti-aliasing, and writes a multi-size
.ico (Windows) plus a PNG. Run: venv\\Scripts\\python scripts/make_icon.py"""
import math
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "public"
OUT.mkdir(exist_ok=True)

S = 1024
BG = (10, 7, 5, 255)
AMBER = (232, 150, 58)
AMBER_HI = (255, 196, 120)
CORE = (255, 224, 180)

img = Image.new("RGBA", (S, S), BG)
d = ImageDraw.Draw(img)
c = S / 2


def ring(r, w, color, a=255):
    d.ellipse([c - r, c - r, c + r, c + r], outline=color + (a,), width=w)


# faint outer field glow
for i, r in enumerate(range(int(S * 0.47), int(S * 0.30), -6)):
    d.ellipse([c - r, c - r, c + r, c + r], outline=AMBER + (10,), width=2)

ring(S * 0.46, 6, AMBER, 90)
ring(S * 0.40, 10, AMBER, 230)
ring(S * 0.30, 5, AMBER, 120)

# coil segments — the arc-reactor ring of wedges
n = 9
r_in, r_out = S * 0.205, S * 0.285
for k in range(n):
    a0 = (k / n) * 2 * math.pi
    a1 = a0 + (2 * math.pi / n) * 0.62
    pts = [
        (c + r_in * math.cos(a0), c + r_in * math.sin(a0)),
        (c + r_out * math.cos(a0), c + r_out * math.sin(a0)),
        (c + r_out * math.cos(a1), c + r_out * math.sin(a1)),
        (c + r_in * math.cos(a1), c + r_in * math.sin(a1)),
    ]
    d.polygon(pts, fill=AMBER_HI + (235,))

# inner ring + glowing core
ring(S * 0.19, 6, AMBER_HI, 255)
for r, col, a in [(S * 0.155, AMBER, 200), (S * 0.11, AMBER_HI, 230), (S * 0.07, CORE, 255)]:
    d.ellipse([c - r, c - r, c + r, c + r], fill=col + (a,))

img = img.resize((256, 256), Image.LANCZOS)
img.save(OUT / "favicon.png")
img.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"wrote {OUT/'favicon.ico'} and favicon.png")
