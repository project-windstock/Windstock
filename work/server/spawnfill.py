"""
Filling empty ground with wild Pokemon -- shared by both radars.

The spawn table is filled by the GAME as phones ask for the map. Ground nobody
has asked about holds nothing, so a radar pointed at it shows an empty world.
This module does the asking: it works out the cells covering a circle and puts
each one to the very builder the client calls, so density, biome, the live event
and the 2016 rates all apply exactly as they would for a trainer standing there.
Nothing here decides what a spawn is; it only decides that the question is asked.

Ground already covered is not covered again. What a place holds is fixed for the
spawn window, so re-asking returns the same Pokemon -- a wasted call. Each filled
cell is remembered against its window and skipped until the window turns over,
which is also when the spawns themselves re-roll. The marks are shared, so the
Help Center radar and the World Manager never redo each other's work.
"""
import math
import threading
import time

_lock = threading.Lock()
_seeded = {}                     # cell id -> the spawn window it was filled for

# Ground covered per call, in level-15 cells (~290x200 m each). Each cell is its
# own call to the map builder at roughly a third of a second, and a 1 km circle
# is about fifty of them, so a caller that filled the lot in one go would hang
# for half a minute. Twelve keeps a call well under a second; the caller sweeps
# again for the rest.
CELLS_PER_CALL = 12


def cells_in(lat, lng, radius_m, step_m=100.0):
    """Level-15 cells covering a circle, nearest the centre first.

    Walks a grid finer than a cell (they are ~200 m across at their narrowest)
    and keeps the distinct cells it lands in, with the point in each closest to
    the centre -- that point is what the builder is asked about, so the cell is
    filled as if someone were standing in it.
    """
    import s2sphere
    coslat = max(0.05, math.cos(math.radians(lat)))
    best = {}
    n = max(1, int(radius_m / step_m))
    for iy in range(-n, n + 1):
        for ix in range(-n, n + 1):
            dy, dx = iy * step_m, ix * step_m
            d = math.hypot(dx, dy)
            if d > radius_m:
                continue
            la = lat + dy / 111_320.0
            ln = lng + dx / (111_320.0 * coslat)
            try:
                cid = s2sphere.CellId.from_lat_lng(
                    s2sphere.LatLng.from_degrees(la, ln)).parent(15).id()
            except Exception:
                continue
            if cid not in best or d < best[cid][0]:
                best[cid] = (d, la, ln)
    return [(c, v[1], v[2]) for c, v in sorted(best.items(), key=lambda kv: kv[1][0])]


def fill(lat, lng, radius_m, budget=CELLS_PER_CALL):
    """Fill up to `budget` unfilled cells of a circle. Returns (new, left)."""
    import world
    import protocol as P
    window = P._window(int(time.time() * 1000))[0]
    cells = cells_in(float(lat), float(lng), float(radius_m))
    with _lock:
        # Marks from older windows are dead weight: those cells have re-rolled
        # and are fair game again.
        for cid in [c for c, w in _seeded.items() if w != window]:
            _seeded.pop(cid, None)
        todo = [c for c in cells if _seeded.get(c[0]) != window]
        batch = todo[:max(0, int(budget))]
        for cid, _la, _ln in batch:
            _seeded[cid] = window            # claimed before the slow part
    with world._lock:
        before = set(world.SPAWNS)
    for cid, _cla, _cln in batch:
        # Asked WITHOUT a trainer fix, on purpose. The builder drops a knot of
        # `spawn_density` Pokemon within ~65 m of the trainer, which is right when
        # someone is standing there -- but filling ground this way made every cell
        # a "trainer", so the map came out as a row of tight clumps with empty
        # space between them. With no fix that cluster is skipped and the cell
        # yields its own spawn points, spread across it the way the world really
        # is. Fewer per cell, and in the right places.
        try:
            P.build_get_map_objects_response([cid], 0.0, 0.0)
        except Exception:
            with _lock:
                _seeded.pop(cid, None)       # didn't happen; let it be retried
    with world._lock:
        new = sum(1 for eid in world.SPAWNS if eid not in before)
    return new, len(todo) - len(batch)
