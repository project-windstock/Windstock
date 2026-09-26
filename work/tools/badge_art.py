"""
badge_art.py -- draw custom medals in the style of the game's own.

A stock medal (Badge_Pikachu_BRONZE/SILVER/GOLD_01, 256x256) is a ring plus a tinted
emblem with a darker outline, a diagonal light-to-dark gradient and a pale sheen; the
middle is transparent. We keep the ring exactly, remove the emblem, and draw a new
emblem shape in that level's colours (sampled from the old emblem), so a new medal
sits next to the stock ones without looking pasted in.

Emblems are drawn as masks at 4x and downsampled.
"""
import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

SS = 4                   # supersampling
SIZE = 256


# ------------------------------------------------------------------ template analysis
def _radius_grid(size=SIZE):
    y, x = np.mgrid[0:size, 0:size]
    c = (size - 1) / 2
    return np.hypot(x - c, y - c)


def split_template(img):
    """-> (ring RGBA image, emblem colours dict). The ring is everything outside the
    widest transparent gap between the emblem and the rim."""
    a = np.asarray(img.convert("RGBA"), dtype=np.float64)
    r = _radius_grid(a.shape[0])
    alpha = a[..., 3]
    prof = [alpha[(r >= k) & (r < k + 1)].mean() for k in range(0, 128)]
    # walk in from the rim until the ring ends, then take the start of the gap
    k = 127
    while k > 0 and prof[k] < 20:
        k -= 1
    while k > 0 and prof[k] >= 20:
        k -= 1
    gap_outer = k
    ring = a.copy()
    ring[r <= gap_outer - 1, 3] = 0
    emblem = (r < gap_outer - 3) & (alpha > 200)
    px = a[emblem][:, :3]
    lum = px @ np.array([0.299, 0.587, 0.114])
    # the outline is the emblem's darkest pixels; fill spans the rest
    order = np.argsort(lum)
    n = len(order)
    colours = {
        "outline": np.median(px[order[: n // 12]], 0),
        "dark": np.median(px[order[n // 5: n // 3]], 0),
        "light": np.median(px[order[int(n * 0.75): int(n * 0.92)]], 0),
        "sheen": np.median(px[order[int(n * 0.97):]], 0),
    }
    return Image.fromarray(ring.astype(np.uint8), "RGBA"), colours, gap_outer


# ------------------------------------------------------------------ emblem shapes
def _sparkle(draw, cx, cy, r, pinch=0.28, rot=0.0, fill=255):
    """Four-point shiny sparkle: an astroid-like star with concave sides."""
    pts = []
    for i in range(160):
        t = 2 * math.pi * i / 160
        # superellipse |x|^p + |y|^p = 1 with p < 1 gives concave sides
        c, s = math.cos(t), math.sin(t)
        p = pinch * 2
        x = math.copysign(abs(c) ** (2 / p), c)
        y = math.copysign(abs(s) ** (2 / p), s)
        xr = x * math.cos(rot) - y * math.sin(rot)
        yr = x * math.sin(rot) + y * math.cos(rot)
        pts.append((cx + xr * r, cy + yr * r))
    draw.polygon(pts, fill=fill)


def _star5(draw, cx, cy, r, inner=0.45):
    pts = []
    for i in range(10):
        t = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * inner
        pts.append((cx + rr * math.cos(t), cy + rr * math.sin(t)))
    draw.polygon(pts, fill=255)


def emblem_mask(kind, size=SIZE * SS):
    """White-on-black mask of the emblem, centred, about 60% of the medal."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    u = size / 256.0                                  # 1 unit = 1 px at 256
    if kind == "shiny":
        _sparkle(d, 118 * u, 136 * u, 70 * u)
        _sparkle(d, 178 * u, 80 * u, 26 * u)
        _sparkle(d, 176 * u, 176 * u, 17 * u)
    elif kind == "night":
        # crescent: a disc with an offset disc cut out
        d.ellipse([60 * u, 58 * u, 188 * u, 186 * u], fill=255)
        d.ellipse([98 * u, 36 * u, 214 * u, 152 * u], fill=0)
        _star5(d, 172 * u, 150 * u, 20 * u)
        _sparkle(d, 150 * u, 88 * u, 14 * u)
        _star5(d, 196 * u, 104 * u, 11 * u)
    elif kind == "shinydex":
        # Poke Ball with a sparkle on its shoulder
        cx, cy, R = 118 * u, 140 * u, 58 * u
        d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=255)
        d.rectangle([cx - R - 2 * u, cy - 7 * u, cx + R + 2 * u, cy + 7 * u], fill=0)
        d.ellipse([cx - 22 * u, cy - 22 * u, cx + 22 * u, cy + 22 * u], fill=0)
        d.ellipse([cx - 12 * u, cy - 12 * u, cx + 12 * u, cy + 12 * u], fill=255)
        # sparkle on the shoulder, with a clear gap cut so the outlines don't merge
        _sparkle(d, 176 * u, 82 * u, 40 * u, pinch=0.4, fill=0)
        _sparkle(d, 176 * u, 82 * u, 30 * u)
    else:
        raise ValueError(kind)
    return m


# ------------------------------------------------------------------ compositing
def _gradient(size, light, dark):
    """Diagonal light (top-left) to dark (bottom-right)."""
    y, x = np.mgrid[0:size, 0:size] / (size - 1)
    t = np.clip((x + y) / 2 * 1.2 - 0.1, 0, 1)[..., None]
    rgb = np.asarray(light) * (1 - t) + np.asarray(dark) * t
    return rgb


def render_medal(template, kind):
    """template: a stock 256x256 medal of the wanted level. Returns 256x256 RGBA."""
    ring, col, _gap = split_template(template)
    big = SIZE * SS
    mask = emblem_mask(kind, big)
    # outline: the mask grown by ~3px (at 256), in the outline colour, under the fill
    grown = mask.filter(ImageFilter.MaxFilter(2 * 3 * SS + 1))
    fill = _gradient(big, col["light"], col["dark"])
    # sheen: a soft pale band across the upper-left of the emblem
    y, x = np.mgrid[0:big, 0:big] / (big - 1)
    band = np.exp(-(((x + y) - 0.72) / 0.07) ** 2)[..., None] * 0.55
    fill = fill * (1 - band) + np.asarray(col["sheen"]) * band
    out = np.zeros((big, big, 4))
    g = np.asarray(grown, dtype=np.float64)[..., None] / 255
    f = np.asarray(mask.filter(ImageFilter.GaussianBlur(SS * 0.6)), dtype=np.float64)[..., None] / 255
    out[..., :3] = np.asarray(col["outline"]) * (1 - f) + fill * f
    out[..., 3:] = g * 255
    emblem = Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGBA").resize((SIZE, SIZE), Image.LANCZOS)
    medal = ring.copy()
    medal.alpha_composite(emblem)
    return medal
