"""
Nests and nest migration -- the part players can SEE.

biomes.park_nest_species already makes parks spawn mostly one species and
rotates it every `spawns.nest_rotation_days`. This module answers the questions
a 2016 player asked the Silph Road: where are the nests near me, what is in
them, and when do they migrate?

A nest is every park stop in one level-14 S2 cell (~600 m), because that is the
key park_nest_species uses -- so one big park is one nest, and the species
listed here is exactly the one that spawns there.

Rotation periods count from the Unix epoch, which was a Thursday, so with the
default 14 days (or 7) migrations land on Thursdays at 00:00 UTC -- the same
day the real game migrated its nests.
"""

import math
import time

import biomes
import settings as _cfg

DAY_MS = 86_400_000


def _rotation_days():
    try:
        return max(1, int(_cfg.get("spawns", "nest_rotation_days", cast=int)))
    except Exception:
        return 14


def enabled():
    try:
        return bool(_cfg.get("spawns", "nests", cast=bool))
    except Exception:
        return False


def period(now_ms=None):
    """Which rotation we are in. Changes exactly when the nests migrate."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return int(now_ms // (_rotation_days() * DAY_MS))


def migration(now_ms=None):
    """{"period", "last_ms", "next_ms", "rotation_days"} for the current cycle."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    days = _rotation_days()
    p = int(now_ms // (days * DAY_MS))
    return {"period": p, "last_ms": p * days * DAY_MS,
            "next_ms": (p + 1) * days * DAY_MS, "rotation_days": days}


def _dist_m(lat1, lng1, lat2, lng2):
    cosl = max(0.2, math.cos(math.radians((lat1 + lat2) / 2)))
    return math.hypot((lat2 - lat1) * 111320.0, (lng2 - lng1) * 111320.0 * cosl)


def _park_forts(lat, lng, radius_m):
    """Park forts within radius_m, via the level-15 cell index (never a scan of
    the whole fort list, which is ~1 GB on the phone)."""
    import s2sphere
    import pois
    # a square cap is plenty: we filter by real distance afterwards
    dlat = radius_m / 111320.0
    dlng = radius_m / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    rect = s2sphere.LatLngRect.from_point_pair(
        s2sphere.LatLng.from_degrees(lat - dlat, lng - dlng),
        s2sphere.LatLng.from_degrees(lat + dlat, lng + dlng))
    cov = s2sphere.RegionCoverer()
    cov.min_level = cov.max_level = 15
    cov.max_cells = 2000
    idx = pois.forts_by_cell()
    out = []
    for c in cov.get_covering(rect):
        for f in idx.get(c.id()) or []:
            if f.get("park") and _dist_m(lat, lng, f["lat"], f["lng"]) <= radius_m:
                out.append(f)
    return out


def _best_name(names):
    """The park's name: the one most of its stops share, else the shortest."""
    counts = {}
    for n in names:
        if n:
            counts[n] = counts.get(n, 0) + 1
    if not counts:
        return "Park"
    return sorted(counts, key=lambda n: (-counts[n], len(n)))[0]


def near(lat, lng, radius_m=3000.0, limit=20, now_ms=None):
    """The nests around (lat, lng), nearest first:
    [{"species", "name", "lat", "lng", "distance_m", "stops"}]."""
    if not enabled():
        return []
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    days = _rotation_days()
    try:
        share = float(_cfg.get("spawns", "park_nest_share", cast=float))
    except Exception:
        share = 0.67
    try:
        import s2sphere
        forts = _park_forts(lat, lng, radius_m)
    except Exception:
        return []
    groups = {}
    for f in forts:
        cid = s2sphere.CellId.from_lat_lng(
            s2sphere.LatLng.from_degrees(f["lat"], f["lng"])).parent(14).id()
        groups.setdefault(cid, []).append(f)
    out = []
    for cid, fs in groups.items():
        sp = biomes.park_nest_species(fs[0]["lat"], fs[0]["lng"], now_ms, days, share)
        if not sp:
            continue
        clat = sum(f["lat"] for f in fs) / len(fs)
        clng = sum(f["lng"] for f in fs) / len(fs)
        # distance to the NEAREST stop of the park, not its middle: that's
        # how far you actually have to walk
        d = min(_dist_m(lat, lng, f["lat"], f["lng"]) for f in fs)
        out.append({"species": int(sp), "name": _best_name(f.get("name", "") for f in fs),
                    "lat": round(clat, 6), "lng": round(clng, 6),
                    "distance_m": int(d), "stops": len(fs)})
    out.sort(key=lambda n: n["distance_m"])
    return out[:max(1, int(limit))]
