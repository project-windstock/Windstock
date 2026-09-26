"""
shiny_overrides.py -- hand-tuned colour rules for shinies the icon mapping gets wrong.

shiny_colour.py learns normal->shiny colours from the icon pair, which fails where a small or
shaded region carries the change (Magnemite's magnet tips), where the icon is too small to
learn from (Eevee), or where two parts share one colour but change differently (Venomoth's
pale body stays, its pale wings go blue). For those species a rule list runs AFTER the
mapping and replaces the pixels it matches:

    (texture suffix or None for all, hue range, sat range, val range, target rgb)

A matched pixel becomes the target colour scaled by its brightness relative to the median of
everything that rule matched in that texture, so the texture's own shading survives. Targets
come from clustering the official shiny icons (lit colours, a touch brighter than the
icon's average, since textures are unlit albedo).
"""
import numpy as np
from PIL import Image

ANY = (0.0, 1.0)
REDS = ((0.95, 1.0), (0.0, 0.08))        # hue wraps

MAGNET_TIP = (0.22, 0.22, 0.22)
OVERRIDES = {
    81: [   # Magnemite: steel body -> gold, red/blue magnet tips -> charcoal
        (None, REDS, (0.3, 1), ANY, MAGNET_TIP),
        (None, (0.5, 0.6), (0.45, 1), (0.3, 1), MAGNET_TIP),
        (None, (0.45, 0.65), (0.08, 0.45), (0.55, 1), (0.88, 0.82, 0.58)),
    ],
    82: [   # Magneton: as Magnemite, and its grey magnets go dark
        (None, REDS, (0.3, 1), ANY, MAGNET_TIP),
        (None, (0.5, 0.6), (0.45, 1), (0.3, 1), MAGNET_TIP),
        (None, (0.45, 0.65), (0.08, 0.45), (0.55, 1), (0.86, 0.80, 0.58)),
        (None, ANY, (0.0, 0.07), (0.5, 0.8), (0.31, 0.30, 0.28)),
    ],
    133: [  # Eevee: brown -> silver, cream -> bluish white, dark ear tips -> charcoal
        (None, (0.03, 0.14), (0.45, 1), (0.5, 1), (0.86, 0.85, 0.79)),
        (None, (0.05, 0.16), (0.1, 0.45), (0.65, 1), (0.91, 0.93, 0.97)),
        (None, (0.0, 0.14), (0.45, 1), (0.0, 0.5), (0.30, 0.30, 0.27)),
    ],
    49: [   # Venomoth: wings -> blue, body stays pale (a little cooler)
        ("BodyB1", ANY, ANY, (0.4, 1), (0.47, 0.64, 0.90)),        # the whole wing sheet
        ("BodyA1", (0.66, 0.74), (0.2, 1), (0.4, 1), (0.42, 0.58, 0.86)),
        ("BodyA1", (0.58, 0.64), (0.12, 1), (0.6, 1), (0.47, 0.66, 0.90)),
        ("BodyA1", (0.72, 0.9), (0.03, 0.2), (0.6, 1), (0.86, 0.85, 0.92)),
    ],
}


def _in(v, rng):
    if isinstance(rng[0], tuple):                 # union of ranges (hue wrap)
        return np.logical_or.reduce([_in(v, r) for r in rng])
    return (v >= rng[0]) & (v <= rng[1])


def apply(num, tex_name, original, mapped):
    """original, mapped: PIL RGBA images of the same texture. Returns mapped with this
    species' rules applied (or mapped unchanged if it has none)."""
    rules = OVERRIDES.get(num)
    if not rules:
        return mapped
    src = np.asarray(original.convert("RGBA"), dtype=np.float64) / 255
    out = np.asarray(mapped.convert("RGBA"), dtype=np.float64).copy() / 255
    rgb = src[..., :3]
    mx, mn = rgb.max(-1), rgb.min(-1)
    v = mx
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    h = _hue(rgb)
    taken = np.zeros(v.shape, bool)
    for suffix, hr, sr, vr, target in rules:
        if suffix and not tex_name.endswith(suffix):
            continue
        m = _in(h, hr) & _in(s, sr) & _in(v, vr) & ~taken
        if not m.any():
            continue
        ref = np.median(v[m])
        k = np.clip(v[m] / max(ref, 1e-3), 0.45, 1.35)[:, None]
        out[..., :3][m] = np.clip(np.asarray(target) * k, 0, 1)
        taken |= m
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")


def _hue(rgb):
    """Vectorised HSV hue (0..1)."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = rgb.max(-1), rgb.min(-1)
    d = np.where(mx - mn == 0, 1, mx - mn)
    h = np.where(mx == r, (g - b) / d % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4))
    return np.where(mx - mn == 0, 0, h / 6)
