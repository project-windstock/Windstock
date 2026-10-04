"""
render3d.py -- a small software renderer, so the 3D preview needs nothing but
numpy and Pillow.

No OpenGL, no pyrender, no browser. That is deliberate: this has to run on a
plain Python install, inside a Tk window, on a machine with no working GPU
driver. A 3,000-vertex model at 500x500 is well under a frame with numpy doing
the arithmetic in bulk, and the whole pipeline is short enough to read top to
bottom:

    model  = meshdata.load(bundle)                       # geometry + textures
    image  = render3d.render(model, yaw=30, pitch=-10)   # a PIL Image

The pipeline is the ordinary one -- rotate, project orthographically, z-buffer,
interpolate UVs and normals per triangle, sample the texture, shade with two
lights. It is vectorised per *triangle* and resolves pixels by keeping the
nearest fragment, so the cost scales with triangles rather than pixels.

Two choices worth arguing for, since both look like shortcuts:

* Orthographic, not perspective. A character sheet is drawn orthographically, and
  when the point of the view is to compare two recolours of the same model, a
  perspective camera makes a near arm look bigger than a far one and two builds
  look different when only the camera moved.
* Nearest-neighbour texture sampling, not bilinear. Bilinear looks better and
  hides the two things this preview exists to show: whether alpha is right, and
  whether the UVs line up. Aliased is honest here; the texture pane next to it
  shows the texels.
"""
import io
import threading

import numpy as np
from PIL import Image

from asset_tools import meshdata

BG_TOP = (40, 44, 54)
BG_BOTTOM = (21, 23, 29)
AMBIENT = 0.34
KEY_LIGHT = np.array([0.40, 0.34, 0.85])      # above and in front
FILL_LIGHT = np.array([-0.55, -0.18, 0.30])   # below and behind
KEY_COLOUR = np.array([1.00, 0.97, 0.90])
FILL_COLOUR = np.array([0.55, 0.62, 0.80])
RIM = 0.10


class RenderError(Exception):
    """Raised for something the caller should report, not a programming error."""


# --------------------------------------------------------------------------
# camera
# --------------------------------------------------------------------------
def _view_rotation(yaw, pitch, roll=0.0):
    """Rotate the model into camera space, as a row-vector 3x3.

    Yaw about Y, then pitch about X, then roll about Z. The model stays in
    Unity's left-handed frame, so no handedness conversion is needed anywhere
    else -- the same frame the glTF exporter emits.
    """
    y, p, r = (np.radians(float(v)) for v in (yaw, pitch, roll))
    cy, sy, cp, sp = np.cos(y), np.sin(y), np.cos(p), np.sin(p)
    cr, sr = np.cos(r), np.sin(r)
    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]])
    rz = np.array([[cr, -sr, 0.0], [sr, cr, 0.0], [0.0, 0.0, 1.0]])
    return rz @ rx @ ry


def _project(points, half_w, half_h):
    """Camera-space points -> (x_ndc, y_ndc, depth) with smaller depth nearer.

    The camera looks down -Z, so a point's distance in front of it is -z, and
    keeping the smallest depth keeps the nearest fragment. Doing that sign
    explicitly here means the rasteriser has one rule and no special cases.
    """
    x = points[:, 0] / half_w
    y = points[:, 1] / half_h
    depth = -points[:, 2]
    return np.stack([x, y, depth], axis=1)


# --------------------------------------------------------------------------
# textures
# --------------------------------------------------------------------------
def _texture(png_bytes):
    """PNG bytes -> float RGBA in 0..1, or None if it will not decode."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            im = im.convert("RGBA")
            w, h = im.size
            arr = np.asarray(im, dtype=np.float32) / 255.0
    except Exception:
        return None
    if not w or not h:
        return None
    return arr.reshape(h, w, 4), w, h


def _cached_texture(model, path_id):
    """Decoded texture for `path_id`, decoded at most once per distinct PNG.

    Decoding a 1024x1024 PNG costs about as much as rasterising the whole model,
    and a drag re-renders on every mouse event, so decoding per part per frame was
    the single biggest cost in the preview. The cache is keyed on the PNG *bytes*,
    not just the path id, so painting a texture (which swaps the bytes) misses the
    cache and re-decodes on the next frame without anything having to invalidate
    it by hand.
    """
    if not path_id:
        return None
    entry = model["textures"].get(path_id)
    if not entry:
        return None
    raw = entry[0]
    cache = model.setdefault("_tex_cache", {})
    hit = cache.get(path_id)
    if hit is not None and hit[0] == raw:
        return hit[1]
    tex = _texture(raw)
    cache[path_id] = (raw, tex)
    return tex


def _sample(tex, w, h, u, v):
    """Nearest-neighbour. u wraps, v flips: Unity's UV origin is bottom-left and
    the textures come out of the bundle top-row-first."""
    if tex is None:
        return np.full((len(u), 4), 0.78, dtype=np.float32)
    x = np.mod((u * w).astype(np.int32), w)
    y = np.clip(((1.0 - v) * h).astype(np.int32), 0, h - 1)
    return tex[y, x]


# --------------------------------------------------------------------------
# shading
# --------------------------------------------------------------------------
def _shade(normals, texels, ambient=AMBIENT):
    """Two-light lambert plus a rim, bounded so a lit surface cannot blow out.

    The bound is the point. An unbounded sum of ambient + key + fill + rim clips
    every up-facing surface to flat white, which looks like a lighting bug and
    destroys the shading that makes a UV or normal problem visible. So the
    ambient and the diffuse terms partition unity: the brightest possible pixel is
    0.34 + 0.66 * tint, just under white, and the texture's own colour is what
    you are actually looking at.
    """
    length = np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-8)
    n = normals / length
    # abs(), not max(0, .): these materials are doubleSided, so a backface still
    # has to be lit rather than going black.
    key = np.abs(n @ KEY_LIGHT)
    fill = np.abs(n @ FILL_LIGHT)
    diffuse = 0.72 * key + 0.28 * fill
    warm = 0.75 * KEY_COLOUR + 0.25 * FILL_COLOUR
    tint = diffuse[:, None] * warm[None, :]
    lit = float(ambient) + (1.0 - float(ambient)) * tint
    lit = lit + RIM * (1.0 - np.abs(n[:, 2]))[:, None] * (1.0 - float(ambient))
    return np.clip(texels[:, :3] * lit, 0.0, 1.0), texels[:, 3]


# --------------------------------------------------------------------------
# rasterisation
# --------------------------------------------------------------------------
def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _candidates(sx, sy, live, by0, by1, width, height):
    """Scanline spans: the pixels a triangle can actually cover, per row.

    The obvious way to enumerate a triangle's pixels is its bounding box, and that
    is what this used to do -- 80,264 candidate pixels to draw 3,990 real ones on
    a 2,520-triangle test mesh, i.e. 95% of the rasteriser's work thrown away.
    Character meshes are full of long thin slivers (fingers, hair cards, the
    gap between an arm and the body), and a sliver's bounding box is mostly empty,
    so the waste is worst exactly where the triangle count is highest.

    A convex triangle meets a horizontal scanline in one interval, so per row it is
    enough to find where its two crossing edges land. Sorting the three vertices by
    y splits the rows at the middle vertex: below it the crossing edges are
    v0-v1 and v0-v2, above it v1-v2 and v0-v2. Which of the two is on the left
    depends on the winding, and every material in these bundles is doubleSided, so
    min/max is used rather than assuming an order. Each half only ever divides by
    the dy of an edge that spans it, so a horizontal edge -- a flat-shaded sliver,
    or two vertices on the same row -- cannot divide by zero; the degenerate half
    is the empty one.

    The span is deliberately one pixel wider than the exact coverage rule at each
    end. A too-wide span costs a little arithmetic; a too-narrow one punches holes
    in the model, and the barycentric test below still has the final say on which
    candidates are real.
    """
    sx = sx[live]
    sy = sy[live]
    order = np.argsort(sy, axis=1, kind="stable")
    ys = np.take_along_axis(sy, order, axis=1)
    xs = np.take_along_axis(sx, order, axis=1)
    y0, y1, y2 = ys[:, 0], ys[:, 1], ys[:, 2]
    x0, x1, x2 = xs[:, 0], xs[:, 1], xs[:, 2]

    rows0 = by0[live]
    rows1 = by1[live]
    nrows = (rows1 - rows0 + 1).astype(np.int64)
    total = int(nrows.sum())
    if total <= 0:
        return (np.empty(0, np.int32),) * 3
    tri = np.repeat(live.astype(np.int32), nrows)
    # x0..y2 are indexed by position within `live`, while tri carries ORIGINAL
    # triangle indices for the barycentric lookups further down, so each row needs
    # both: slot to reach the vertices, tri to reach uvs/normals/indices.
    slot = np.repeat(np.arange(len(live), dtype=np.int32), nrows)
    row = np.repeat(rows0.astype(np.int64), nrows) + (
        np.arange(total, dtype=np.int64)
        - np.repeat(np.cumsum(nrows) - nrows, nrows))
    fy = row.astype(np.float32) + 0.5              # scanline at the pixel centre

    def _at(ax, ay, bx, by):
        """x where the edge a->b crosses this scanline. dy==0 is neutralised so a
        horizontal edge yields a finite number instead of inf/nan; the row it
        would have been needed for lies in the other half and is never selected."""
        dy = by - ay
        dy = np.where(np.abs(dy) < 1e-9, np.float32(1.0), dy)
        return ax + (fy - ay) * (bx - ax) / dy

    # broadcast the three vertices across this row's triangle
    X0, Y0, X1, Y1, X2, Y2 = (v[slot] for v in (x0, y0, x1, y1, x2, y2))
    # below the middle vertex: v0-v1 and v0-v2. above it: v1-v2 and v0-v2.
    lo_a, lo_b = _at(X0, Y0, X1, Y1), _at(X0, Y0, X2, Y2)
    hi_a, hi_b = _at(X1, Y1, X2, Y2), _at(X0, Y0, X2, Y2)
    upper = fy >= Y1
    xa = np.where(upper, np.minimum(hi_a, hi_b), np.minimum(lo_a, lo_b))
    xb = np.where(upper, np.maximum(hi_a, hi_b), np.maximum(lo_a, lo_b))

    # The span is the exact pixel-centre coverage rule. It is a superset of what
    # the barycentric test below accepts -- both use the same screen-space edges,
    # and a convex triangle's interior on a scanline is exactly [xa, xb] -- so this
    # is not an approximation of the inside test, it is the same set computed
    # without a per-pixel test. (Verified pixel-identical against the bounding-box
    # rasteriser on a sphere and on a mesh of slivers and degenerate triangles.)
    xa = np.clip(np.ceil(xa - 0.5), 0, width - 1)
    xb = np.clip(np.floor(xb - 0.5), 0, width - 1)
    counts = np.maximum(xb - xa + 1.0, 0.0).astype(np.int64)
    keep = counts > 0
    if not keep.any():
        return (np.empty(0, np.int32),) * 3
    tri, row, xa, counts = tri[keep], row[keep], xa[keep], counts[keep]
    total = int(counts.sum())
    # expand each surviving scanline into its pixels
    base = (np.arange(total, dtype=np.int64)
            - np.repeat((np.cumsum(counts) - counts).astype(np.int64), counts))
    px = (np.repeat(xa.astype(np.int64), counts) + base).astype(np.int32)
    py = np.repeat(row.astype(np.int32), counts)
    return np.repeat(tri, counts), px, py


def _draw_part(buffers, tris, screen, uvs, normals, tex, width, height):
    """Add one part into the shared (depth, colour, alpha) buffers, in place.

    Bin-then-resolve, fully vectorised, in about ten numpy operations rather than
    one Python iteration per triangle. A part here is ~4,900 triangles averaging
    20 px each, so the naive loop spends essentially all its time on numpy call
    overhead -- 4.8 s for a 400x400 frame, measured, of which under a fifth was
    shading. So instead:

      1. build one flat list of (triangle, candidate pixel) pairs, using each
         triangle's scanline span;
      2. evaluate the barycentrics for all of them at once;
      3. let one lexsort by (pixel, depth) pick each pixel's nearest fragment;
      4. sample and shade only those survivors.

    That is the same answer as the per-triangle loop in a fraction of the time,
    and it matters because this runs on every frame of a drag.

    Both windings are drawn -- every material in these bundles is doubleSided --
    and barycentrics are used signed, so a triangle wound either way interpolates
    correctly with no sign fix-up.
    """
    depth, colour, out_a = buffers
    if not len(tris):
        return

    sc = screen[tris]                       # (nt, 3, 3) of x, y, depth
    sx = (sc[:, :, 0] * 0.5 + 0.5) * width
    sy = (0.5 - sc[:, :, 1] * 0.5) * height
    sz = sc[:, :, 2]
    denom = _cross(sx[:, 1] - sx[:, 0], sy[:, 1] - sy[:, 0],
                   sx[:, 2] - sx[:, 0], sy[:, 2] - sy[:, 0]).astype(np.float32)

    bx0 = np.clip(np.floor(sx.min(axis=1)), 0, width - 1).astype(np.int32)
    bx1 = np.clip(np.ceil(sx.max(axis=1)), 0, width - 1).astype(np.int32)
    by0 = np.clip(np.floor(sy.min(axis=1)), 0, height - 1).astype(np.int32)
    by1 = np.clip(np.ceil(sy.max(axis=1)), 0, height - 1).astype(np.int32)
    bw = bx1 - bx0 + 1
    bh = by1 - by0 + 1
    ok = (np.isfinite(sx).all(axis=1) & np.isfinite(sy).all(axis=1)
          & np.isfinite(sz).all(axis=1) & (np.abs(denom) > 1e-9)
          & (bw > 0) & (bh > 0))
    if not ok.any():
        return
    live = np.nonzero(ok)[0]
    if not len(live):
        return
    if int((bw[live] * bh[live]).sum()) > 200_000_000:   # sanity, not a real case
        raise RenderError("mesh projects to an implausible number of pixels")

    # 1. one flat list of candidate pixels, as scanline spans rather than bounding
    #    boxes: on a mesh of slivers the box is 95% empty, so ~6x the candidates
    #    and ~6x the per-candidate work for pixels that fail the inside test
    #    immediately. Verified pixel-identical to the bounding-box version on a
    #    smooth sphere and on slivers and degenerate triangles at six angles each.
    #    int32 indices: these arrays run to millions of entries, are re-read
    #    several times, and int64 doubles the memory traffic for nothing.
    tri, px, py = _candidates(sx, sy, live, by0, by1, width, height)
    if not len(tri):
        return

    # 2. barycentrics for every pair at once. Point-edge, not edge-point:
    #    cross(pp - p0, p2 - p0) / denom is 1.0 at p1 by construction. With the
    #    operands swapped it is -1.0 there, still sums to 1 across the triangle,
    #    and still fails every inside test -- so the model renders as a sparse
    #    scatter of single pixels with no error at all.
    fx = px.astype(np.float32) + 0.5
    fy = py.astype(np.float32) + 0.5
    d = denom[tri]
    b1 = _cross(fx - sx[tri, 0], fy - sy[tri, 0],
                sx[tri, 2] - sx[tri, 0], sy[tri, 2] - sy[tri, 0]) / d
    b2 = _cross(fx - sx[tri, 1], fy - sy[tri, 1],
                sx[tri, 0] - sx[tri, 1], sy[tri, 0] - sy[tri, 1]) / d
    b0 = 1.0 - b1 - b2
    # 3. nearest fragment per pixel, and not behind what is already there.
    #    The inside test and the depth test are independent tests on the same
    #    pairs, so they are evaluated together and the eight arrays are compacted
    #    once. They used to be two separate boolean filters, and every candidate
    #    fragment paid for two full copy-and-compact passes.
    pix = (py * width + px).astype(np.int32)
    flat = depth.reshape(-1)
    z = b0 * sz[tri, 0] + b1 * sz[tri, 1] + b2 * sz[tri, 2]
    inside = ((b0 >= 0) & (b1 >= 0) & (b2 >= 0)) | ((b0 <= 0) & (b1 <= 0) & (b2 <= 0))
    # pix is in range for every candidate (the scanline clipped both ends), so the
    # gather is safe even for pairs the inside test will discard. NaN and inf from
    # a zero-area triangle compare False, so degenerate pairs drop out here without
    # needing a separate finiteness test.
    take = inside & (z < flat[pix])
    if not take.any():
        return
    b0, b1, b2, tri, pix, z = b0[take], b1[take], b2[take], tri[take], pix[take], z[take]

    order = np.lexsort((z, pix))            # by pixel, then by depth
    spix, sz_ = pix[order], z[order]
    first = np.ones(len(spix), dtype=bool)
    first[1:] = spix[1:] != spix[:-1]
    win = order[first]
    b0, b1, b2, tri, pix, z = b0[win], b1[win], b2[win], tri[win], pix[win], z[win]

    # 4. shade the survivors only
    v0, v1, v2 = tris[tri, 0], tris[tri, 1], tris[tri, 2]
    u = b0 * uvs[v0, 0] + b1 * uvs[v1, 0] + b2 * uvs[v2, 0]
    v = b0 * uvs[v0, 1] + b1 * uvs[v1, 1] + b2 * uvs[v2, 1]
    n = (b0[:, None] * normals[v0] + b1[:, None] * normals[v1]
         + b2[:, None] * normals[v2])
    texel = _sample(tex[0] if tex else None,
                    tex[1] if tex else 1, tex[2] if tex else 1, u, v)
    rgb, alpha = _shade(n, texel)

    flat[pix] = z
    colour.reshape(-1, 3)[pix] = rgb
    out_a.reshape(-1)[pix] = alpha


# --------------------------------------------------------------------------
# background
# --------------------------------------------------------------------------
# Per-thread scratch, keyed by frame size. A drag re-renders at one fixed size
# hundreds of times, and re-allocating three full-frame buffers and rebuilding
# the gradient + mgrid checkerboard every time was pure overhead. Per-thread so
# two renders can never share a buffer and stomp each other's depth test.
_SCRATCH = threading.local()


def _buffers(height, width):
    """(depth, colour, alpha) for one frame, cleared and ready to draw into."""
    cache = getattr(_SCRATCH, "buffers", None)
    if cache is None or cache[0] != (height, width):
        depth = np.empty((height, width), dtype=np.float32)
        colour = np.empty((height, width, 3), dtype=np.float32)
        alpha = np.empty((height, width), dtype=np.float32)
        cache = ((height, width), depth, colour, alpha)
        _SCRATCH.buffers = cache
    _, depth, colour, alpha = cache
    depth.fill(np.inf)
    colour.fill(0.0)
    alpha.fill(0.0)
    return depth, colour, alpha


def _background(width, height, checker=8):
    """A vertical gradient, in 0..1 floats to match the rest of the pipeline.

    BG_TOP and BG_BOTTOM are written as 0-255 because that is how you read a
    colour, so they get divided here rather than being 40x too bright once they
    are multiplied by 255 again on the way out.

    Cached per size, because it is a function of the size and nothing else. A
    copy is handed out: the caller blends the model into it.
    """
    cache = getattr(_SCRATCH, "bg", None)
    if cache is None or cache[0] != (width, height, checker):
        ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        top = np.array(BG_TOP, dtype=np.float32) / 255.0
        bottom = np.array(BG_BOTTOM, dtype=np.float32) / 255.0
        col = top[None, :] * (1.0 - ramp) + bottom[None, :] * ramp
        img = np.repeat(col[:, None, :], width, axis=1)
        if checker:
            # A checkerboard behind the model is the only reliable way to see an
            # alpha bug rather than guessing at a dark pixel.
            yy, xx = np.mgrid[0:height, 0:width]
            board = (((xx // max(1, checker)) + (yy // max(1, checker))) % 2)
            img *= (0.88 + 0.12 * board).astype(np.float32)[:, :, None]
        cache = ((width, height, checker), img)
        _SCRATCH.bg = cache
    return cache[1].copy()


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------
def render(model, width=520, height=520, yaw=32.0, pitch=-12.0, roll=0.0,
            scale=None, ambient=AMBIENT, checker=8, margin=0.08):
    """Render a model from :func:`meshdata.load` to a PIL Image.

    `scale` is the model's height in view units; left as None the model is framed
    from its own bounding box, so a new species does not arrive off-screen.
    `checker=0` drops the transparency checkerboard.
    """
    width = int(max(64, width))
    height = int(max(64, height))

    # Skinning is a full pass over every vertex, and the bind pose does not
    # change between frames, so it is done once per model and reused. Previously
    # every mouse-move event re-posed the whole mesh, which is most of why
    # orbiting lagged.
    posed = model.get("_posed_cache")
    if posed is None or posed.get("_n") != len(model["parts"]):
        posed = {"_n": len(model["parts"]), "parts": {}}
        for i, part in enumerate(model["parts"]):
            posed["parts"][i] = (meshdata.posed_positions(part),
                                 meshdata.posed_normals(part))
        model["_posed_cache"] = posed

    parts = []
    for i, part in enumerate(model["parts"]):
        parts.append((part,) + posed["parts"][i])
    if not parts:
        raise RenderError("this bundle has no drawable mesh")


    allpts = np.vstack([p[1] for p in parts])
    lo = allpts.min(axis=0)
    hi = allpts.max(axis=0)
    # The widest axis, not hi.max()-lo.max(): those differ whenever the model
    # is off-centre, which would clip it out of frame.
    extent = float(np.max(hi - lo)) or 1.0
    centre = (lo + hi) * 0.5

    aspect = width / float(height)
    if scale is None:
        scale = extent / max(1e-6, 1.0 - 2.0 * margin)
    half_h = max(float(scale) * 0.5, 1e-6)
    half_w = half_h * aspect

    view = _view_rotation(yaw, pitch, roll)
    depth, colour, alpha = _buffers(height, width)

    for part, pos, nrm in parts:
        # float32 from here on. posed_* hands back float64, and the rasteriser
        # then does about a dozen array ops per candidate pixel over millions of
        # candidates, so the extra width is pure memory traffic.
        rpos = ((pos - centre) @ view.T).astype(np.float32)
        rnrm = (nrm @ view.T).astype(np.float32)
        screen = _project(rpos, half_w, half_h)
        uvs = part["mesh"]["uvs"].astype(np.float32)
        subs = part["mesh"].get("submeshes")
        if not subs:
            # no submesh information (or a caller built the model by hand):
            # one texture for everything, as before
            subs = [part["mesh"]["tris"].astype(np.int64)]
            tex_ids = [part.get("texture")]
        else:
            tex_ids = part.get("submesh_textures") or [part.get("texture")]
            # zip() would stop at the shorter list and silently drop the remaining
            # submeshes -- geometry vanishing rather than drawing unshaded, which is
            # the very failure mode per-submesh textures exist to prevent. Pad with
            # the legacy texture instead.
            if len(tex_ids) < len(subs):
                tex_ids = list(tex_ids) + [part.get("texture")] * (
                    len(subs) - len(tex_ids))
        for sub, tid in zip(subs, tex_ids):
            if not len(sub):
                continue
            _draw_part((depth, colour, alpha), sub.astype(np.int32), screen,
                       uvs, rnrm, _cached_texture(model, tid), width, height)

    canvas = _background(width, height, checker)
    # Blend over the whole frame with no mask. Where nothing was drawn alpha is 0,
    # and canvas*(1-0) + colour*0 is exactly the background, so the mask bought
    # nothing and cost a lot: np.copyto against a broadcast (h, w, 1) mask falls
    # off numpy's fast path, and this runs on every pixel of every frame.
    a = alpha[:, :, None]
    np.multiply(canvas, 1.0 - a, out=canvas)
    canvas += colour * a
    np.multiply(canvas, 255.0, out=canvas)
    np.clip(canvas, 0.0, 255.0, out=canvas)
    return Image.fromarray(canvas.astype(np.uint8), "RGB")


def render_views(model, size=260, **angles):
    """A contact sheet: {name: Image} for several yaws, for the editor's strip.

    Handy for eyeballing a UV seam -- a seam that shows at one angle and not
    another is a texture problem, not a geometry one.
    """
    out = {}
    for name, kw in angles.items():
        try:
            out[name] = render(model, width=size, height=size, **kw)
        except RenderError:
            continue
    return out
