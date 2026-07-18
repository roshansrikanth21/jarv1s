#!/usr/bin/env python3
"""Generate the JARVIS desktop icon — a single bold geometric "J" (JARVIS's own
initial), monochrome-first, with one precise accent triangle at its top corner.

This replaced two prior attempts, both rejected:
  1. A literal arc-reactor (concentric rings + glowing core) — the single most
     cloned "AI assistant" visual cliche; also a lore error (the arc reactor is
     Iron Man's SUIT power source, not JARVIS the software itself).
  2. A "1" monogram inside camera-viewfinder corner brackets, in amber/copper —
     rejected as "generic and tacky... color scheme is ass." Corner-bracket/
     reticle framing turned out to be its own well-documented AI/scanning-app
     cliche (QR/barcode SDKs use the exact same device), and that saturated
     amber/copper reads as hazard-signage, not premium software.

Design research (Vercel, Notion, Linear, 1Password, Figma's own icon-design
process) converges on: ONE strong silhouette, grid-discipline (every stroke
width/gap a multiple of one unit, never eyeballed), monochrome or near-
monochrome with a single restrained accent, and — critically — verify small-
size legibility by actually rendering and looking at 16x16px, not asserting it.
Several other candidate constructions (an abstract two-mass "stepped" shape;
a negative-space cut) were built and rejected at this stage: the stepped
shape blurred into an amorphous blob at 16px, and the negative-space cut
didn't read as intended at any size. This J does neither — it holds as one
fully-connected, legible silhouette from 16px up, and the accent triangle
(the single controlled deviation in an otherwise strict rectilinear shape)
reads as a nib/pen-tip catching light — apt for a system that's always
present and ready to respond, without literalizing that into a cliche icon.

No radial/concentric symmetry (rules out arc reactors), no frame/bracket
device (rules out reticles), not a bare numeral (rules out attempt 2's other
failure), no circuit/node/neural/brain/eye imagery.

Each output resolution is rendered independently at 8x supersample then
LANCZOS-downsampled (never upscaled from a smaller master).

Run: venv\\Scripts\\python scripts/make_icon.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "public"
OUT.mkdir(exist_ok=True)

BG = (10, 7, 5, 255)          # existing app near-black — kept, isolates the mark cleanly
MARK = (237, 233, 227, 255)   # warm off-white — shares the bg's warm undertone rather
                               # than a clinical cool white, so the mark reads "cut from
                               # the same material" as its background, not stamped on top
ACCENT = (120, 150, 220, 255) # one desaturated cool blue, reserved ONLY for the corner
                               # accent — deliberately not amber (attempt 2's failure),
                               # a quiet, precise "signal" touch, not a decoration


def draw_mark(size: int) -> Image.Image:
    """Render at `size`, supersampled 8x then downsampled for clean edges."""
    S = size * 8
    u = S / 16  # everything below is a multiple of this one grid unit
    img = Image.new("RGBA", (S, S), BG)
    d = ImageDraw.Draw(img)

    stroke_w = 3.2 * u
    stem_l, stem_r = 8.0 * u, 8.0 * u + stroke_w
    top_y, bot_y = 2.4 * u, 13.4 * u
    foot_l = 4.4 * u
    foot_top = bot_y - stroke_w   # foot is exactly one stroke_w tall — same unit as the stem
    cut = stroke_w * 0.85         # size of the one corner accent

    # The full J silhouette: a vertical stem that kicks left into a foot at the
    # bottom — one unbroken polygon, no separate parts to misalign or fragment
    # at small sizes.
    body = [
        (stem_l, top_y),
        (stem_r, top_y),
        (stem_r, bot_y),
        (foot_l, bot_y),
        (foot_l, foot_top),
        (stem_l, foot_top),
    ]
    d.polygon(body, fill=MARK)

    # The one controlled accent: a small triangle at the top-left corner, in
    # the single reserved accent color — reads as a nib/tip catching light.
    # (Kept as a filled color swap, not a cut-away notch, so the silhouette
    # stays a full rectangle at a glance and never thins out at 16px.)
    accent = [(stem_l, top_y), (stem_l + cut, top_y), (stem_l, top_y + cut)]
    d.polygon(accent, fill=ACCENT)

    try:
        _lanczos = Image.Resampling.LANCZOS
    except AttributeError:
        _lanczos = Image.LANCZOS  # type: ignore[attr-defined]
    return img.resize((size, size), _lanczos)


def main():
    ico_sizes = [16, 32, 48, 64, 128, 256]
    imgs = [draw_mark(s) for s in ico_sizes]

    draw_mark(256).save(OUT / "favicon.png")
    imgs[-1].save(OUT / "favicon.ico", sizes=[(s, s) for s in ico_sizes],
                  append_images=imgs[:-1])
    print(f"wrote {OUT/'favicon.ico'} and favicon.png")


if __name__ == "__main__":
    main()
