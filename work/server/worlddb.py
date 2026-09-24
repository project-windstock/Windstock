"""
World packs -- the OSM world data, indexed by S2 cell, one SQLite file per
region, for places that cannot afford to hold it in memory.

pois.py and biomes.py were written for a desktop: each json.load()s its whole
file up front. Measured on the US data that is 1,134,423 forts at 1.18 GB peak
RSS, plus 968,715 biome cells at another 477 MB. On a phone running the game in
the same process, that is how you get killed by jetsam. Every consumer only
ever asks "what is in THIS cell", so the data is packed on the Mac into
read-only SQLite files keyed by cell (ios-launcher/tools/build_world_packs.py),
and a request costs one indexed lookup per cell per pack.

Packs are found in two places, and ALL of them are used together:

  * <data dir>/world/*.sqlite   -- downloaded by the launcher's World Data
                                   screen, one per country the player picked;
  * world/*.sqlite beside this  -- shipped inside the app bundle, if any.

Several can cover the same ground -- Geofabrik extracts overlap at borders,
and a downloaded country can repeat a bundled one -- so fort results are
de-duplicated by fort id, which is deterministic from the OSM object.

Only used when pois/biomes have no JSON file; the desktop keeps its JSON,
which poidownload writes and pois hot-reloads.

Opened `immutable=1`: the bundle is read-only, and without it SQLite tries to
create lock and journal files beside the database and fails. That also means
a pack must never be rewritten in place -- the launcher downloads to a temp
name and renames, and a new file is a new pack.
"""
import glob
import json
import os
import sqlite3
import threading
from urllib.request import pathname2url

import datadir

_HERE = os.path.dirname(os.path.abspath(__file__))
_local = threading.local()
_meta_lock = threading.Lock()
_meta_cache = {}                 # (path, mtime) -> {key: value}


def _dirs():
    return [os.path.join(datadir.ensure(), "world"),
            os.path.join(_HERE, "world")]


def paths():
    """Every installed pack, downloaded ones first. Re-scanned on each call --
    a listdir of a small directory -- so a pack the launcher just installed is
    live on the very next map request, with no restart."""
    out, seen = [], set()
    for d in _dirs():
        for p in sorted(glob.glob(os.path.join(d, "*.sqlite"))):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                out.append(p)
    return out


def path():
    """Kept for callers that only need to know whether ANY pack exists."""
    ps = paths()
    return ps[0] if ps else None


def _conn(p):
    """One connection per pack per thread: the server is a ThreadingHTTPServer
    and sqlite3 connections are not safe to share across threads."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    key = (p, _mtime(p))
    c = conns.get(key)
    if c is None:
        # a replaced or deleted pack leaves a stale connection behind; drop it
        for k in [k for k in conns if k[0] == p]:
            try:
                conns.pop(k).close()
            except sqlite3.Error:
                pass
        c = sqlite3.connect(f"file:{pathname2url(p)}?mode=ro&immutable=1",
                            uri=True, check_same_thread=False)
        conns[key] = c
    return c


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def s64(cell_id):
    """S2 cell ids are unsigned 64-bit; SQLite integers are signed. Faces 4 and
    5 set the top bit, so store them two's-complement."""
    cell_id = int(cell_id)
    return cell_id - (1 << 64) if cell_id >= (1 << 63) else cell_id


def pack_meta(p):
    key = (p, _mtime(p))
    with _meta_lock:
        hit = _meta_cache.get(key)
        if hit is not None:
            return hit
    vals = {}
    try:
        for k, v in _conn(p).execute("SELECT key, value FROM meta"):
            vals[k] = v
    except sqlite3.Error:
        vals = {}
    with _meta_lock:
        _meta_cache[key] = vals
    return vals


def packs():
    """[{path, name, region, forts, biome_cells, built}] for the World Data UI
    and the log."""
    out = []
    for p in paths():
        m = pack_meta(p)
        out.append({"path": p, "name": m.get("name") or os.path.basename(p),
                    "region": m.get("region", ""),
                    "forts": int(m.get("fort_count", 0) or 0),
                    "biome_cells": int(m.get("biome_count", 0) or 0),
                    "built": m.get("built", "")})
    return out


def has_forts():
    return any(int(pack_meta(p).get("fort_count", 0) or 0) > 0 for p in paths())


def _fort(r, cell):
    return {"id": r[0], "lat": r[1], "lng": r[2],
            "kind": "gym" if r[3] else "stop",
            "name": r[4] or "", "image": r[5] or "", "cell": cell}


def forts_in_cell(cell_id):
    out, seen = [], set()
    for p in paths():
        try:
            rows = _conn(p).execute(
                "SELECT id, lat, lng, kind, name, image FROM forts WHERE cell = ?",
                (s64(cell_id),)).fetchall()
        except sqlite3.Error:
            continue                       # a broken pack must not break the map
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                out.append(_fort(r, str(cell_id)))
    return out


def all_forts():
    """Every fort in every pack. Deliberately NOT on any hot path -- this is the
    1 GB load the packs exist to avoid."""
    out, seen = [], set()
    for p in paths():
        try:
            for r in _conn(p).execute(
                    "SELECT id, lat, lng, kind, name, image FROM forts"):
                if r[0] not in seen:
                    seen.add(r[0])
                    out.append(_fort(r, ""))
        except sqlite3.Error:
            continue
    return out


def biome_level():
    for p in paths():
        m = pack_meta(p)
        if int(m.get("biome_count", 0) or 0) > 0:
            return int(m.get("biome_level", 12) or 12)
    return 12


def has_biomes():
    return any(int(pack_meta(p).get("biome_count", 0) or 0) > 0 for p in paths())


def biome_cell(cell_id):
    for p in paths():
        try:
            row = _conn(p).execute("SELECT biome FROM biome_cells WHERE cell = ?",
                                   (s64(cell_id),)).fetchone()
        except sqlite3.Error:
            continue
        if row:
            return row[0]
    return None


def terrain_points():
    pts = []
    for p in paths():
        raw = pack_meta(p).get("terrain_points")
        try:
            pts.extend(tuple(x) for x in (json.loads(raw) if raw else []))
        except ValueError:
            continue
    return pts


class CellIndex:
    """Stands in for the {cell_id: [forts]} dict pois.forts_by_cell() returns.
    Callers only use .get() and truthiness, so that is all this implements."""

    def get(self, cell_id, default=None):
        hit = forts_in_cell(cell_id)
        return hit if hit else default

    def __bool__(self):
        return has_forts()


class BiomeCells:
    """Stands in for the {cell_id: biome} dict biomes._load_cells() returns."""

    def get(self, cell_id, default=None):
        b = biome_cell(cell_id)
        return b if b is not None else default

    def __bool__(self):
        return has_biomes()
