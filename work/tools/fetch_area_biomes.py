"""
Real-terrain biomes for a non-US region, voted by AREA instead of by feature.

fetch_us_biomes gives every terrain feature one vote per level-12 cell it touches.
That collapses European cities to "grassland": thousands of tiny landuse=grass
verges and pitches each out-vote the one big residential polygon. Here every
polygon (closed ways AND multipolygon relations) is rasterised onto a ~200 m
point grid instead, so each cell goes to whatever terrain actually COVERS most of
it. Where polygons overlap (a park inside a residential area) the smaller one
wins the grid point -- it's the more specific tag.

Merges into data/osm_biomes.json (existing cells elsewhere are kept; cells inside
this region are replaced). biomes.py hot-reloads the JSON; on the VPS the JSON is
packed into world.sqlite by deploy/upload.sh.

Usage:
  python3 fetch_area_biomes.py some-region.osm.pbf [--grid-m 200] [--dry-run [--dump cells.json]]
"""
import json
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_osm_biomes as fob   # noqa: E402  (RULES + _biome classifier)
import osmium                    # noqa: E402
import s2sphere                  # noqa: E402
from osmium.filter import KeyFilter  # noqa: E402

OUT = os.path.join(HERE, "..", "server", "data", "osm_biomes.json")
CELL_LEVEL = 12
BIOMES = ["water", "wetland", "forest", "mountain", "desert", "city", "grassland"]
MINOR_WATERWAY_SKIP = True   # waterway=* lines aren't areas; riverbanks come as natural=water


def _bbox(path):
    """The extract's header bbox (Geofabrik sets it), else a full scan of nodes."""
    box = osmium.io.Reader(path, osmium.osm.osm_entity_bits.NOTHING).header().box()
    if box.valid():
        return box.bottom_left.lat, box.bottom_left.lon, box.top_right.lat, box.top_right.lon
    raise SystemExit("extract has no header bbox")


def compute(path, grid_m=200.0, log=print, storage="flex_mem"):
    """{level-12 cell id (str): biome} for every cell of the extract with terrain."""
    s, w, n, e = _bbox(path)
    dlat = grid_m / 111_320.0
    dlng = grid_m / (111_320.0 * math.cos(math.radians((s + n) / 2)))
    rows = int((n - s) / dlat) + 1
    cols = int((e - w) / dlng) + 1
    log(f"  bbox {s:.3f},{w:.3f} .. {n:.3f},{e:.3f}  grid {rows}x{cols} "
        f"({rows * cols / 1e6:.1f}M points at {grid_m:.0f} m)")
    biome = np.full((rows, cols), -1, dtype=np.int8)
    owner_area = np.full((rows, cols), np.inf, dtype=np.float32)

    keys = sorted({k for k, _v, _b in fob.RULES if k != "waterway"})
    fp = (osmium.FileProcessor(path)
          .with_locations(storage)
          .with_areas(KeyFilter(*keys))
          .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA)))
    t0 = time.time()
    seen = painted = 0
    for a in fp:
        seen += 1
        if seen % 100_000 == 0:
            log(f"    {seen:,} areas  painted {painted:,}  {time.time() - t0:.0f}s")
        b = fob._biome(dict(a.tags))
        if b is None or b not in BIOMES:
            continue
        # Every ring (outer + inner) as edges; even-odd fill handles the holes.
        xs, ys = [], []
        try:
            for outer in a.outer_rings():
                rings = [outer] + list(a.inner_rings(outer))
                for ring in rings:
                    pts = [(nd.lat, nd.lon) for nd in ring if nd.location.valid()]
                    if len(pts) < 3:
                        continue
                    ys.append(np.array([p[0] for p in pts]))
                    xs.append(np.array([p[1] for p in pts]))
        except Exception:
            continue
        if not ys:
            continue
        y1 = np.concatenate([r for r in ys]); x1 = np.concatenate([r for r in xs])
        y2 = np.concatenate([np.roll(r, -1) for r in ys])
        x2 = np.concatenate([np.roll(r, -1) for r in xs])
        r0 = max(0, int(math.ceil((y1.min() - s) / dlat)))
        r1 = min(rows - 1, int(math.floor((y1.max() - s) / dlat)))
        if r1 < r0:
            continue                       # thinner than one grid row
        # Polygon area in grid-point units (shoelace on the grid projection).
        area = abs(np.sum((x1 / dlng) * (y2 / dlat) - (x2 / dlng) * (y1 / dlat))) / 2
        bi = BIOMES.index(b)
        ok = y1 != y2
        y1, y2, x1, x2 = y1[ok], y2[ok], x1[ok], x2[ok]
        lo, hi = np.minimum(y1, y2), np.maximum(y1, y2)
        for c0 in range(r0, r1 + 1, 256):                 # chunk rows: rows x edges
            ry = s + np.arange(c0, min(r1, c0 + 255) + 1) * dlat
            hit = (ry[:, None] >= lo[None, :]) & (ry[:, None] < hi[None, :])
            for i, row_y in enumerate(ry):
                m = hit[i]
                if not m.any():
                    continue
                xi = x1[m] + (row_y - y1[m]) * (x2[m] - x1[m]) / (y2[m] - y1[m])
                xi.sort()
                r = c0 + i
                for k in range(0, len(xi) - 1, 2):
                    a0 = max(0, int(math.ceil((xi[k] - w) / dlng)))
                    a1 = min(cols - 1, int(math.floor((xi[k + 1] - w) / dlng)))
                    if a1 < a0:
                        continue
                    seg = owner_area[r, a0:a1 + 1] > area
                    if seg.any():
                        biome[r, a0:a1 + 1][seg] = bi
                        owner_area[r, a0:a1 + 1][seg] = area
                        painted += 1
    log(f"  {seen:,} areas read in {time.time() - t0:.0f}s")

    # Vote the grid into level-12 cells.
    votes = {}
    rr, cc = np.nonzero(biome >= 0)
    lats = s + rr * dlat
    lngs = w + cc * dlng
    bs = biome[rr, cc]
    log(f"  {len(rr):,} grid points have terrain; voting into cells ...")
    for la, ln, b in zip(lats.tolist(), lngs.tolist(), bs.tolist()):
        cid = s2sphere.CellId.from_lat_lng(
            s2sphere.LatLng.from_degrees(la, ln)).parent(CELL_LEVEL).id()
        d = votes.get(cid)
        if d is None:
            votes[cid] = d = [0] * len(BIOMES)
        d[b] += 1
    cells = {str(c): BIOMES[max(range(len(BIOMES)), key=d.__getitem__)]
             for c, d in votes.items()}
    tally = {}
    for v in cells.values():
        tally[v] = tally.get(v, 0) + 1
    log(f"  {len(cells):,} cells: " + ", ".join(f"{k} {v:,}" for k, v in
                                                sorted(tally.items(), key=lambda kv: -kv[1])))
    return cells


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return
    grid_m = 200.0
    if "--grid-m" in sys.argv:
        grid_m = float(sys.argv[sys.argv.index("--grid-m") + 1])
    cells = compute(args[0], grid_m, log=lambda m: print(m, flush=True))
    if "--dry-run" in sys.argv:
        if "--dump" in sys.argv:
            with open(sys.argv[sys.argv.index("--dump") + 1], "w") as fh:
                json.dump(cells, fh)
        return
    with open(OUT, encoding="utf-8") as fh:
        data = json.load(fh)
    before = len(data["cells"])
    data["cells"].update(cells)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    print(f"wrote {OUT}: {before:,} -> {len(data['cells']):,} cells", flush=True)


if __name__ == "__main__":
    main()
