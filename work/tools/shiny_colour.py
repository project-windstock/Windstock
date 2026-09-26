"""
shiny_colour.py -- learn a normal->shiny colour mapping from an icon pair, apply it to a
model texture.

The official normal and shiny icons are renders of the same model in the same pose, so
their opaque pixels line up (alpha IoU >= 0.97 for 147/151 Gen-1 pairs; the rest are
flame/gas Pokemon, where only the overlap is used). Every overlapping pixel is a sample
"this normal colour became that shiny colour".

A single hue shift (make_shiny_bundle.py's original Pikachu mode) can't express most
shinies -- Charizard's orange goes black while its belly stays cream -- so the samples
are split into colour REGIONS (k-means in Lab, lightness down-weighted so shading of one
colour stays one region), and each region gets its own per-channel affine fit
shiny = gain * normal + offset. Fitting across the region's shading range keeps the fit
valid on the texture's unlit albedo, which is brighter than most icon pixels.

A texture pixel takes a soft blend of the regions it is close to (no seams where two
regions meet); a pixel far from every region the icon shows -- mouth interiors, UV
padding -- is left alone.
"""
import numpy as np
from PIL import Image

K = 10              # colour regions per species
L_WEIGHT = 0.35     # lightness matters less than hue/chroma when grouping colours
SIGMA = 9.0         # soft-assignment width, Lab units
FAR = 30.0          # beyond this distance from every region: leave the pixel unchanged
MIN_SAMPLES = 40    # a region with fewer pixels than this is dropped


# ------------------------------------------------------------------ colour space
def _srgb_to_lab(rgb):
    """rgb float array [...,3] in 0..1 -> Lab [...,3]"""
    c = np.where(rgb > 0.04045, ((rgb + 0.055) / 1.055) ** 2.4, rgb / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805],
                  [0.2126, 0.7152, 0.0722],
                  [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def _lab_to_srgb(lab):
    """Lab [...,3] -> rgb float 0..1 (clipped); inverse of _srgb_to_lab."""
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    def finv(f):
        return np.where(f ** 3 > 0.008856, f ** 3, (f - 16 / 116) / 7.787)
    xyz = np.stack([finv(fx), finv(fy), finv(fz)], -1) * np.array([0.95047, 1.0, 1.08883])
    m = np.array([[3.2406, -1.5372, -0.4986],
                  [-0.9689, 1.8758, 0.0415],
                  [0.0557, -0.2040, 1.0570]])
    c = xyz @ m.T
    c = np.clip(c, 0, 1)
    return np.where(c > 0.0031308, 1.055 * c ** (1 / 2.4) - 0.055, 12.92 * c)


def _shade_transfer(c, B, Bn):
    """Carry a SHADE of skin colour B over to the new skin Bn, in Lab: same lightness
    step from the skin, the new skin's hue (plus the shade's own small hue offset), and
    colourfulness scaled like the skin's. A per-channel gain can't do this: it turns a
    deeper orange on an orange->grey Pokemon olive instead of dark grey."""
    cl, bl, nl = _srgb_to_lab(c), _srgb_to_lab(B), _srgb_to_lab(Bn)
    cc, cb, cn = (np.hypot(x[..., 1], x[..., 2]) for x in (cl, bl, nl))
    hc, hb, hn = (np.arctan2(x[..., 2], x[..., 1]) for x in (cl, bl, nl))
    L = cl[..., 0] + (nl[..., 0] - bl[..., 0]) * np.clip(cl[..., 0] / np.maximum(bl[..., 0], 1), 0, 1)
    C = cn * cc / np.maximum(cb, 1.0)
    H = hn + (hc - hb)
    return _lab_to_srgb(np.stack([L, C * np.cos(H), C * np.sin(H)], -1))


def _features(rgb):
    lab = _srgb_to_lab(rgb)
    lab[..., 0] *= L_WEIGHT
    return lab


# ------------------------------------------------------------------ learning
def _kmeans(x, k, iters=25, seed=1):
    rng = np.random.default_rng(seed)
    # k-means++ seeding
    centres = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        d = np.min(((x[:, None, :] - np.array(centres)[None]) ** 2).sum(-1), axis=1)
        centres.append(x[rng.choice(len(x), p=d / d.sum())])
    c = np.array(centres)
    for _ in range(iters):
        lab = np.argmin(((x[:, None, :] - c[None]) ** 2).sum(-1), axis=1)
        new = np.array([x[lab == i].mean(0) if (lab == i).any() else c[i] for i in range(k)])
        if np.allclose(new, c):
            break
        c = new
    return c, lab


def _load_pairs(normal_icon, shiny_icon):
    a = np.asarray(Image.open(normal_icon).convert("RGBA"), dtype=np.float64) / 255
    b = np.asarray(Image.open(shiny_icon).convert("RGBA"), dtype=np.float64) / 255
    mask = (a[..., 3] > 0.8) & (b[..., 3] > 0.8)
    # erode one pixel: edge pixels mix in the transparent background
    m = mask.copy()
    m[1:, :] &= mask[:-1, :]; m[:-1, :] &= mask[1:, :]
    m[:, 1:] &= mask[:, :-1]; m[:, :-1] &= mask[:, 1:]
    return a[m][:, :3], b[m][:, :3]


class Mapping:
    def __init__(self, centres, affines, identity):
        self.centres = centres      # [k,3] feature space
        self.affines = affines      # [k,3,2] per channel (gain, offset)
        self.identity = identity    # [k] bool, region didn't change colour

    def describe(self):
        return f"{len(self.centres)} regions, {int((~self.identity).sum())} recoloured"

    def apply_rgb(self, rgb):
        """rgb float [N,3] 0..1 -> mapped rgb [N,3]"""
        f = _features(rgb)
        d2 = ((f[:, None, :] - self.centres[None]) ** 2).sum(-1)       # [N,k]
        w = np.exp(-(d2 - d2.min(1, keepdims=True)) / (2 * SIGMA ** 2))
        w /= w.sum(1, keepdims=True)
        g = self.affines[..., 0]                                         # [k,3]
        o = self.affines[..., 1]
        mapped = (w[:, :, None] * (rgb[:, None, :] * g[None] + o[None])).sum(1)
        # fade back to the original colour far from anything the icon shows
        near = np.sqrt(d2.min(1))
        keep = np.clip((near - FAR * 0.7) / (FAR * 0.3), 0, 1)[:, None]
        return np.clip(mapped * (1 - keep) + rgb * keep, 0, 1)


def learn(normal_icon, shiny_icon, k=K):
    n, s = _load_pairs(normal_icon, shiny_icon)
    if len(n) < 500:
        raise ValueError(f"only {len(n)} overlapping pixels")
    feats = _features(n)
    sub = np.random.default_rng(0).choice(len(n), min(len(n), 12000), replace=False)
    centres, _ = _kmeans(feats[sub], k)
    lab = np.argmin(((feats[:, None, :] - centres[None]) ** 2).sum(-1), axis=1)
    keep_c, affines, ident = [], [], []
    for i in range(k):
        sel = lab == i
        if sel.sum() < MIN_SAMPLES:
            continue
        ni, si = n[sel], s[sel]
        aff = np.zeros((3, 2))
        for ch in range(3):
            x, y = ni[:, ch], si[:, ch]
            # ridge toward "gain = mean ratio, no offset": a region with almost no
            # shading spread would otherwise get a wild slope
            A = np.stack([x, np.ones_like(x)], 1)
            lam = 0.02 * len(x)
            prior = np.array([y.mean() / max(x.mean(), 1e-3), 0.0])
            reg = np.diag([lam, lam * 4])
            aff[ch] = np.linalg.solve(A.T @ A + reg, A.T @ y + reg @ prior)
        keep_c.append(centres[i]); affines.append(aff)
        ident.append(np.abs(si - ni).mean() < 0.03)
    return Mapping(np.array(keep_c), np.array(affines), np.array(ident))


def self_check(mapping, normal_icon, shiny_icon):
    """mean abs error (0..255) of mapped-normal vs real shiny on the icon, and the
    error of doing nothing -- the second number is how different the shiny is at all."""
    n, s = _load_pairs(normal_icon, shiny_icon)
    m = mapping.apply_rgb(n)
    return float(np.abs(m - s).mean() * 255), float(np.abs(n - s).mean() * 255)


# ------------------------------------------------------------------ applying
def recolour_image(img, mapping, hidden_only=False):
    """Recolour a PIL image. Alpha is kept as-is (body textures use it as a mask).
    hidden_only=True touches only fully transparent pixels -- the face material is
    drawn opaque, so those 'hidden' RGB values show around the eyes."""
    arr = np.asarray(img.convert("RGBA"), dtype=np.float64) / 255
    flat = arr.reshape(-1, 4)
    sel = flat[:, 3] == 0 if hidden_only else np.ones(len(flat), bool)
    out = flat.copy()
    rgb = flat[sel, :3]
    for i in range(0, len(rgb), 65536):
        out[np.flatnonzero(sel)[i:i + 65536], :3] = mapping.apply_rgb(rgb[i:i + 65536])
    return Image.fromarray((out.reshape(arr.shape) * 255 + 0.5).astype(np.uint8), "RGBA")


# ------------------------------------------------------------------ edge clean-up
EDGE_RANGE = 0.07     # a pixel whose 3x3 neighbourhood spans more than this (per channel,
                      # summed, 0..3) sits on an edge between colours
EDGE_WIN = 3          # half-size of the window searched for the colours an edge pixel blends


def clean_edges(orig, recoloured):
    """Fix the fringes a per-colour recolour leaves where two colours meet.

    An anti-aliased edge pixel is a BLEND of the colours either side of it (fur and eye
    outline, blue body and cream belly). Recolouring it as a colour in its own right can send
    it somewhere neither neighbour went -- the pink rim on shiny Gyarados, specks around
    Electrode's eyes. Instead: find the two nearby 'pure' colours (and the window's most
    extreme pixel, for 1-2 px lines that have no pure pixels of their own) that best explain
    the edge pixel as a mix, and give it the same mix of THEIR new colours.

    orig, recoloured: PIL images (RGBA), same size. Returns a new RGBA image; alpha untouched.
    """
    o = np.asarray(orig.convert("RGBA"), dtype=np.float64) / 255
    r = np.asarray(recoloured.convert("RGBA"), dtype=np.float64) / 255
    rgb, delta = o[..., :3], r[..., :3] - o[..., :3]
    h, w = rgb.shape[:2]
    pad = np.pad(rgb, ((1, 1), (1, 1), (0, 0)), mode="edge")
    stack = np.stack([pad[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)])
    spread = (stack.max(0) - stack.min(0)).sum(-1)
    pure = spread < EDGE_RANGE
    out = delta.copy()
    ys, xs = np.nonzero(~pure)
    W = EDGE_WIN
    for y, x in zip(ys, xs):
        y0, y1, x0, x1 = max(0, y - W), min(h, y + W + 1), max(0, x - W), min(w, x + W + 1)
        win_c = rgb[y0:y1, x0:x1].reshape(-1, 3)
        win_d = delta[y0:y1, x0:x1].reshape(-1, 3)
        win_p = pure[y0:y1, x0:x1].reshape(-1)
        c = rgb[y, x]
        cand_c = win_c[win_p]
        cand_d = win_d[win_p]
        # the most extreme pixel in the window stands in for a thin line's own colour
        far = np.argmax(((win_c - win_c.mean(0)) ** 2).sum(-1))
        cand_c = np.vstack([cand_c, win_c[far]])
        cand_d = np.vstack([cand_d, win_d[far]])
        if len(cand_c) > 24:                           # thin out, keeping variety
            idx = np.linspace(0, len(cand_c) - 1, 24).astype(int)
            cand_c, cand_d = cand_c[idx], cand_d[idx]
        # best pair (A, B) with c ~ t*A + (1-t)*B
        A = cand_c[:, None, :]; B = cand_c[None, :, :]
        AB = A - B
        denom = (AB ** 2).sum(-1)
        t = np.where(denom > 1e-6, ((c - B) * AB).sum(-1) / np.maximum(denom, 1e-6), 1.0)
        t = np.clip(t, 0, 1)
        resid = ((t[..., None] * A + (1 - t[..., None]) * B - c) ** 2).sum(-1)
        i, j = np.unravel_index(np.argmin(resid), resid.shape)
        if resid[i, j] > 0.02:                          # nothing nearby explains it: leave it
            continue
        out[y, x] = t[i, j] * cand_d[i] + (1 - t[i, j]) * cand_d[j]
    res = o.copy()
    res[..., :3] = np.clip(rgb + out, 0, 1)
    return Image.fromarray((res * 255 + 0.5).astype(np.uint8), "RGBA")


def fade_by_alpha(orig, recoloured):
    """Face textures: keep opaque pixels (the eyes themselves) as they were, take the new
    colour on fully transparent ones, and blend in between by transparency."""
    o = np.asarray(orig.convert("RGBA"), dtype=np.float64) / 255
    r = np.asarray(recoloured.convert("RGBA"), dtype=np.float64) / 255
    a = o[..., 3:4]
    res = o.copy()
    res[..., :3] = r[..., :3] * (1 - a) + o[..., :3] * a
    return Image.fromarray((res * 255 + 0.5).astype(np.uint8), "RGBA")


# ------------------------------------------------------------------ face / eye sheets
SKIN_FLAT = 0.06      # 3x3 spread (summed channels) below which a pixel is flat skin
SKIN_LAB = 16.0       # Lab distance to a skin colour that still counts as that skin
SKIN_MIN = 0.04       # a flat colour covering less of the sheet than this isn't skin
SKIN_REACH = 6        # px: how far from skin an edge pixel can be and still blend with it
INK_WIN = 2           # half-size of the window searched for the line ("ink") colour
SHADE_CHROMA = 10.0   # a line with less colour than this (Lab chroma) is black/grey ink
SHADE_HUE = 30.0      # degrees: a coloured line this close in hue to the skin is a shade of it
INK_DARK_L = 30.0     # Lab lightness below which a line is an outline and keeps its colour
SHADE_RESID = 0.12    # colour left over after skin+line unmixing above which a pixel is its
                      # own shade of the skin (a shading patch), not an anti-aliased edge


def _shift(a, dy, dx):
    """a shifted by (dy, dx) with edge padding, same shape."""
    h, w = a.shape[:2]
    p = np.pad(a, ((2 * SKIN_REACH, 2 * SKIN_REACH), (2 * SKIN_REACH, 2 * SKIN_REACH))
               + ((0, 0),) * (a.ndim - 2), mode="edge")
    o = 2 * SKIN_REACH
    return p[o + dy:o + dy + h, o + dx:o + dx + w]


def recolour_ink_sheet(img, mapping, target=None):
    """Recolour a face / eye sheet: the Pokemon's skin changes, the drawing on it doesn't.

    These sheets are line art (eye outlines, lashes, mouths) painted over the Pokemon's
    normal skin colour, drawn opaque on the model. Mapping every pixel on its own (v1-v3)
    sent each anti-aliased edge pixel -- half outline, half skin -- somewhere neither side
    went: an orange ring round shiny Charmander's eyes, olive specks round Eevee's. Here:

      * skin = the big flat colour(s) of the sheet outside the eyeballs (alpha marks the
        eyeballs). Its colour change is learned ONCE per neighbourhood from the icon
        mapping (smoothed skin colour B -> B') and applied as a per-channel gain, so every
        shade of the skin -- shading, brow strokes -- becomes the same shade of the new skin
      * a pixel near skin is a MIX of that skin B and the nearby line colour I:
        p = t*B + (1-t)*I (+ a small residual). Its skin share becomes B'; its line share
        follows the skin if the line is black/grey or a shade of the skin, and stays as
        drawn if it's another colour (mouths, cheeks) -- a clean edge, no halo
      * the eyes themselves (alpha) are never touched.
    target: an already-recoloured version of this sheet whose flat skin is RIGHT (it came
    from the same recolour as the body), used for the new skin colour instead of the
    mapping, so the face can't drift from a hand-tuned body. Alpha is untouched."""
    arr = np.asarray(img.convert("RGBA"), dtype=np.float64) / 255
    rgb, alpha = arr[..., :3], arr[..., 3]
    h, w = alpha.shape
    stack = np.stack([_shift(rgb, dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
    spread = (stack.max(0) - stack.min(0)).sum(-1)
    lab = _srgb_to_lab(rgb)
    cand = (spread < SKIN_FLAT) & (alpha < 0.5)
    skin = np.zeros((h, w), bool)
    left = cand.copy()
    for _ in range(3):
        if left.sum() < SKIN_MIN * h * w:
            break
        q = np.round(lab[left] / 6).astype(int)
        keys, counts = np.unique(q, axis=0, return_counts=True)
        centre = keys[np.argmax(counts)] * 6.0
        near = cand & (np.sqrt(((lab - centre) ** 2).sum(-1)) < SKIN_LAB)
        if near.sum() < SKIN_MIN * h * w:
            break
        skin |= near
        left &= ~near
    if not skin.any():
        return recolour_image(img, mapping)            # no skin found: old behaviour
    # smoothed skin colour B everywhere near skin (5x5 average of skin pixels, then
    # spread outwards SKIN_REACH px)
    def spread_skin(src):
        """5x5 average of src over skin pixels, spread outwards SKIN_REACH px."""
        S = np.zeros_like(rgb); cnt = np.zeros((h, w))
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                k = _shift(skin, dy, dx)
                S += _shift(src, dy, dx) * k[..., None]; cnt += k
        kn = cnt > 0
        S[kn] /= cnt[kn, None]
        for _ in range(SKIN_REACH):
            sb, c2 = np.zeros_like(S), np.zeros((h, w))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    k = _shift(kn, dy, dx)
                    sb += _shift(S, dy, dx) * k[..., None]; c2 += k
            new = ~kn & (c2 > 0)
            S[new] = sb[new] / c2[new, None]
            kn |= new
        return S, kn
    B, known = spread_skin(rgb)
    if target is not None:
        tgt = np.asarray(target.convert("RGBA"), dtype=np.float64)[..., :3] / 255
        Bn, _k = spread_skin(tgt)
    else:
        Bn = B.copy()
        idx = np.flatnonzero(known)
        Bf, Bnf = B.reshape(-1, 3), Bn.reshape(-1, 3)
        for i in range(0, len(idx), 65536):
            sel = idx[i:i + 65536]
            Bnf[sel] = mapping.apply_rgb(Bf[sel])
    gain = np.clip(Bn / np.maximum(B, 0.02), 0, 4)
    out = rgb.copy()
    out[skin] = np.clip(rgb * gain, 0, 1)[skin]
    edge = known & ~skin & (alpha < 0.5)
    # ink I: the pixel in the window that is farthest from this pixel's skin colour
    best_d = np.full((h, w), -1.0)
    I = rgb.copy()
    for dy in range(-INK_WIN, INK_WIN + 1):
        for dx in range(-INK_WIN, INK_WIN + 1):
            c = _shift(rgb, dy, dx)
            d = ((c - B) ** 2).sum(-1)
            upd = d > best_d
            best_d[upd] = d[upd]
            I[upd] = c[upd]
    BI = B - I
    den = (BI ** 2).sum(-1)
    t = np.clip(((rgb - I) * BI).sum(-1) / np.maximum(den, 1e-6), 0, 1)
    t = np.where(den > 1e-4, t, 0.0)
    resid = rgb - (t[..., None] * B + (1 - t[..., None]) * I)
    Il, Bl = _srgb_to_lab(I), _srgb_to_lab(B)
    ci, cb = np.hypot(Il[..., 1], Il[..., 2]), np.hypot(Bl[..., 1], Bl[..., 2])
    cosang = (Il[..., 1] * Bl[..., 1] + Il[..., 2] * Bl[..., 2]) / np.maximum(ci * cb, 1e-6)
    # dark outlines are outlines whatever their tint; mid-tone lines that are grey or a
    # shade of the skin (brow strokes, fur marks) follow the skin
    follows = (Il[..., 0] > INK_DARK_L) & \
        ((ci < SHADE_CHROMA) | (cosang > np.cos(np.radians(SHADE_HUE))))
    In = np.where(follows[..., None], _shade_transfer(I, B, Bn), I)
    # The art's edges aren't exact linear mixes; keep only the leftover's BRIGHTNESS
    # (its colour, scaled by the skin's change, is what painted a yellow rim on Eevee)
    r_lum = resid.mean(-1, keepdims=True)
    r_chroma = np.sqrt(((resid - r_lum) ** 2).sum(-1))
    moved = np.clip(t[..., None] * Bn + (1 - t[..., None]) * In + r_lum, 0, 1)
    # A pixel that is itself a deeper/lighter SHADE of the skin (shading patches, the
    # orange bits round shiny Charizard's eye) and isn't just skin blending into a dark
    # outline takes the skin's change whole.
    pl = lab
    cp = np.hypot(pl[..., 1], pl[..., 2])
    cos_p = (pl[..., 1] * Bl[..., 1] + pl[..., 2] * Bl[..., 2]) / np.maximum(cp * cb, 1e-6)
    shade_px = (cp > SHADE_CHROMA) & (cos_p > np.cos(np.radians(SHADE_HUE))) & \
        (pl[..., 0] > INK_DARK_L) & (r_chroma > SHADE_RESID)
    moved = np.where(shade_px[..., None], _shade_transfer(rgb, B, Bn), moved)
    out[edge] = moved[edge]
    res = arr.copy()
    res[..., :3] = out
    return Image.fromarray((res * 255 + 0.5).astype(np.uint8), "RGBA")
