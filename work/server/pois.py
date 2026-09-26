"""
OSM-sourced PokeStops / Gyms.

Loads data/osm_forts.json (produced by tools/fetch_osm_pois.py from OpenStreetMap)
and hands the map builder real businesses/parks/churches/etc. as forts. Because
they sit on actual buildings they are off the road and close enough to spin --
unlike the procedural cell-centre forts, which can land in the middle of a road.

Same shape as places.forts: {id, lat, lng, kind: "stop"|"gym", name, image},
plus "park": True on the ones that are parks (see is_park).

Which forts are gyms is decided HERE, per level-15 cell, not by the importer:
each cell gets one gym per gyms.stops_per_gym stops, parks first -- the real game
puts its gyms in parks. Doing it at read time means the existing osm_forts.json
and the phone's world packs follow the setting without being rebuilt.
Hot-reloaded by mtime, so re-running the fetch tool takes effect on the next map
refresh with no restart.
"""
import hashlib
import json
import os
import re
import threading

import datadir

POIS_FILE = os.path.join(datadir.ensure(), "osm_forts.json")

_lock = threading.RLock()
_cache = {"mtime": None, "forts": [], "by_cell": None, "view": None}

# ---- parks -------------------------------------------------------------------
# The fort files keep a name but not the OSM tags, so a park is recognised by its
# name. Tuned on the 1.1M-fort US file: ~20k parks, campgrounds, trails and
# nature areas, without catching "Park Ave Dental", "Olive Garden" or
# "The Park at Riverside" apartments.
_PARK_END = re.compile(
    r"\b(park|parque|parc|playground|arboretum|preserve|reserve|green|commons?|"
    r"meadows?|woods|forest|greenway|trailhead|trail|campground|"
    r"recreation (?:area|ground)|picnic area|wildlife area|nature (?:center|centre)|"
    r"(?:community|botanical|public|rose|memorial|japanese|sculpture) gardens?)\s*$",
    re.I)
_PARK_ANY = re.compile(
    r"\b(?:state|county|city|national|regional|memorial|community|neighbou?rhood|"
    r"municipal|dog|skate|water|linear) park\b|"
    r"\b(?:park|playground)\b.*\b(?:entrance|pavilion|shelter|playground|pond|lake|"
    r"trailhead|picnic|boat (?:launch|ramp)|overlook|trail|loop|fields?|courts?|"
    r"splash pad|bandstand|gazebo|campground)\b", re.I)
_NOT_PARK = re.compile(
    r"\b(?:apartments?|apts|apartment|dental|dentist|pharmacy|clinic|hotel|motel|inn|"
    r"bank|salon|school|elementary|academy|market|grocery|liquor|auto|motors|plaza|"
    r"mall|office|offices|realty|insurance|condos?|villas?|townhomes|senior|"
    r"hospital|medical|restaurant|cafe|bar|pizza|grill|diner|deli|shop|store|tire|"
    r"garage|storage|homes|estates|lodge|suites|mobile|r\.?v\.?|golf|cemetery|"
    r"funeral|industrial|business|corporate|tech|research|trailer|residences?|"
    r"living|the park at|on the park)\b", re.I)


def is_park(name):
    """True if a fort's name reads as a park / green space."""
    if not name or _NOT_PARK.search(name):
        return False
    return bool(_PARK_END.search(name) or _PARK_ANY.search(name))


def _h(s):
    return int(hashlib.md5(str(s).encode()).hexdigest()[:12], 16)


def _stops_per_gym():
    try:
        import settings as _cfg
        return max(1, int(_cfg.get("gyms", "stops_per_gym", cast=int)))
    except Exception:
        return 5


def rebalance(cell_id, bucket, stops_per_gym=None):
    """The forts of ONE level-15 cell as copies with "kind" and "park" set.

    One fort in every (stops_per_gym + 1) is a gym; the fractional remainder is
    a stable per-cell coin flip, so a 3-fort cell has a gym half the time and the
    whole map lands on the ratio. Parks get the gym first, then forts the
    importer already made gyms (landmarks), then a stable hash -- so the same
    places stay gyms from one day to the next."""
    if not bucket:
        return bucket
    per = stops_per_gym or _stops_per_gym()
    out = []
    for f in sorted(bucket, key=lambda x: x["id"]):
        g = dict(f)
        g["park"] = is_park(g.get("name", ""))
        out.append(g)
    whole, rem = divmod(len(out), per + 1)
    want = whole + (1 if (_h(("gymcell", cell_id)) % (per + 1)) < rem else 0)
    rank = sorted(out, key=lambda g: (not g["park"], g.get("kind") != "gym",
                                      _h(("gym", g["id"]))))
    gyms = {g["id"] for g in rank[:want]}
    for g in out:
        g["kind"] = "gym" if g["id"] in gyms else "stop"
    return out


class _Balanced:
    """A {cell_id: [forts]} view that rebalances each cell the first time it is
    asked for. Doing all 1.1M forts up front cost ~15 s; a map request only
    ever touches a handful of cells."""

    def __init__(self, inner, per):
        self._inner, self._per, self._memo = inner, per, {}

    def get(self, cell_id, default=None):
        hit = self._memo.get(cell_id)
        if hit is None:
            hit = rebalance(cell_id, self._inner.get(cell_id) or [], self._per)
            if len(self._memo) > 20000:
                self._memo.clear()
            self._memo[cell_id] = hit
        return hit if hit else default

    def __bool__(self):
        return bool(self._inner)


_db_view = {"key": None, "view": None}


def _use_db():
    """The JSON file wins when it exists (the desktop: poidownload writes it and
    this hot-reloads it). Without it -- the phone -- fall back to the cell-indexed
    world.sqlite, which answers per cell instead of holding 1.1M forts in RAM."""
    if os.path.exists(POIS_FILE):
        return False
    import worlddb
    return worlddb.path() is not None


def forts():
    """Every OSM fort, or [] if there's no file yet. Cheap: only re-reads the file
    when its mtime changes.

    With only world.sqlite this materialises the whole table -- ~1 GB -- so
    nothing on a hot path may call it. Use forts_by_cell() or near()."""
    if _use_db():
        import worlddb
        return worlddb.all_forts()
    with _lock:
        try:
            m = os.path.getmtime(POIS_FILE)
        except OSError:
            _cache["mtime"], _cache["forts"] = None, []
            return []
        if _cache["mtime"] != m:
            out = []
            try:
                with open(POIS_FILE, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                for f in (d.get("forts") or []):
                    try:
                        out.append({
                            "id": str(f["id"]),
                            "lat": float(f["lat"]), "lng": float(f["lng"]),
                            "kind": "gym" if f.get("kind") == "gym" else "stop",
                            "name": str(f.get("name", ""))[:40],
                            "image": str(f.get("image", ""))[:300],
                            # level-15 S2 cell, precomputed by the grab tools; "" if an
                            # older file didn't store it (forts_by_cell fills it in then).
                            "cell": str(f.get("cell", "")),
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
            except (OSError, ValueError):
                out = []
            _cache["mtime"], _cache["forts"], _cache["by_cell"] = m, out, None
        return list(_cache["forts"])


def forts_by_cell():
    """Every OSM fort grouped by its level-15 S2 cell id (int), built once per file
    change and cached. Lets the map builder pull just the cells it needs with a dict
    lookup instead of walking every fort on each request -- the difference between
    a few thousand and several million forts staying instant.

    Uses the "cell" precomputed by the grab tools; for an older file without it, the
    ids are computed here once and cached (still O(N) once, not per request).

    With world.sqlite instead of the JSON, returns a lazy stand-in whose .get()
    is one indexed query -- the only way callers use this."""
    if _use_db():
        import worlddb
        key = (tuple(worlddb.paths()), _stops_per_gym())
        with _lock:
            if _db_view["key"] != key:
                _db_view["key"] = key
                _db_view["view"] = _Balanced(worlddb.CellIndex(), key[1])
            return _db_view["view"]
    with _lock:
        forts()                                     # refresh cache if the file changed
        per = _stops_per_gym()
        if _cache["by_cell"] is None:
            idx = {}
            need_s2 = [f for f in _cache["forts"] if not f.get("cell")]
            if need_s2:
                try:
                    import s2sphere
                    for f in need_s2:
                        try:
                            f["cell"] = str(s2sphere.CellId.from_lat_lng(
                                s2sphere.LatLng.from_degrees(
                                    f["lat"], f["lng"])).parent(15).id())
                        except Exception:
                            f["cell"] = ""
                except ImportError:
                    pass
            for f in _cache["forts"]:
                c = f.get("cell")
                if not c:
                    continue
                try:
                    idx.setdefault(int(c), []).append(f)
                except ValueError:
                    continue
            _cache["by_cell"] = idx
        if _cache["view"] is None or _cache["view"]._inner is not _cache["by_cell"] \
                or _cache["view"]._per != per:
            _cache["view"] = _Balanced(_cache["by_cell"], per)
        return _cache["view"]


def near(lat, lng):
    """Forts in the level-15 cell containing (lat, lng) and its eight
    neighbours -- a ~1.5 km square, plenty for "the closest place within
    250 m". Replaces walking every fort in the file, which was a 1.1M-item
    linear scan on the desktop and a 1 GB load on the phone."""
    try:
        import s2sphere
        cell = s2sphere.CellId.from_lat_lng(
            s2sphere.LatLng.from_degrees(lat, lng)).parent(15)
        ids = [cell.id()] + [n.id() for n in cell.get_all_neighbors(15)]
    except Exception:
        return []
    idx = forts_by_cell()
    out = []
    for cid in ids:
        out.extend(idx.get(cid) or [])
    return out
