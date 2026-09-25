"""
Biomes -- geography-flavoured wild spawns for the PoGO private server.

The real 2016 game grouped wild spawns into "biomes" tied to the terrain around
you (the Silph Road community mapped this out: water Pokemon by rivers/coasts,
rock/ground on hills, city trash downtown, ...). This module knows the TWO things
that needs:

* WHICH TYPES a biome favours -- from ``biomes.json``, the shipped data file
  beside ``cpdata.json`` (hot-reloaded, so editing it takes effect on the next
  spawn). Each entry is ``{"types": {type_name: multiplier}, "osm": [[key, value],
  ...]}``; a species takes the strongest boost among its one-or-two types.
* WHICH BIOME A LOCATION IS IN -- from real OpenStreetMap terrain. The server
  asks the free Overpass API about an area the first time someone plays there,
  caches the classified terrain in ``data/osm_biomes.json`` (the same file the
  fetch_osm_biomes/fetch_us_biomes tools write), and then answers every spawn in
  that area from the cache. Where OSM has nothing -- open countryside, or a
  machine with no internet -- a coarse S2 cell (a few km across) hashes to one
  biome instead, so a neighbourhood always has the same flavour and the world
  stays varied everywhere.

Type values are HoloPokemonType (POGOProtos), which is what convert_gm.py wrote
into game_master.bin and what protocol.pokemon_types() reads back -- so the
numbers here line up with the served data. If a value were ever off the only cost
is a less thematic biome (the base pool still contains every species), never a
broken spawn.

This module is deliberately data-only + pure functions plus the OSM cache: it
knows the biomes and which one a location is in. protocol.py owns the actual
species tables (rarity tiers and the game master's per-species types) and just
asks us for the type boosts to apply.
"""

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request

import s2sphere

from windstock.config import paths

# HoloPokemonType
NORMAL, FIGHTING, FLYING, POISON, GROUND, ROCK, BUG, GHOST, STEEL = range(1, 10)
FIRE, WATER, GRASS, ELECTRIC, PSYCHIC, ICE, DRAGON, DARK, FAIRY = range(10, 19)

#: HoloPokemonType value for a type name, and back again. biomes.json spells the
#: types out ({"water": 9}) because that is what a human edits.
TYPE_IDS = {
    "normal": NORMAL, "fighting": FIGHTING, "flying": FLYING, "poison": POISON,
    "ground": GROUND, "rock": ROCK, "bug": BUG, "ghost": GHOST, "steel": STEEL,
    "fire": FIRE, "water": WATER, "grass": GRASS, "electric": ELECTRIC,
    "psychic": PSYCHIC, "ice": ICE, "dragon": DRAGON, "dark": DARK, "fairy": FAIRY,
}
TYPE_NAMES = {v: k for k, v in TYPE_IDS.items()}

# Fallback used only when biomes.json is missing/unreadable -- the game still
# spawns sensibly rather than losing its biomes entirely.
BIOMES = {
    "water":      {WATER: 9, ICE: 5, FLYING: 2},
    "grassland":  {GRASS: 7, NORMAL: 4, BUG: 3, FAIRY: 3},
    "forest":     {BUG: 8, GRASS: 5, POISON: 4, FAIRY: 2},
    "mountain":   {ROCK: 8, GROUND: 6, FIGHTING: 4, FIRE: 3},
    "city":       {NORMAL: 5, ROCK: 5, ELECTRIC: 6, POISON: 5, STEEL: 4, GHOST: 4},
    "wetland":    {WATER: 6, POISON: 5, GROUND: 4, GRASS: 4, BUG: 3},
    "desert":     {GROUND: 8, ROCK: 6, FIRE: 5},
    "suburban":   {NORMAL: 6, FAIRY: 4, ELECTRIC: 3, PSYCHIC: 3},
}
BIOME_NAMES = list(BIOMES)

# Fallback OSM classification, ordered: (key, value_substring, biome). Only used
# if biomes.json can't be read; otherwise the rules come from the file.
OSM_RULES = [
    ("natural", "water", "water"), ("natural", "wetland", "wetland"),
    ("natural", "bay", "water"), ("natural", "spring", "water"),
    ("waterway", "", "water"), ("landuse", "reservoir", "water"),
    ("landuse", "basin", "water"),
    ("natural", "wood", "forest"), ("landuse", "forest", "forest"),
    ("natural", "scrub", "forest"), ("natural", "heath", "forest"),
    ("natural", "peak", "mountain"), ("natural", "cliff", "mountain"),
    ("natural", "ridge", "mountain"), ("natural", "rock", "mountain"),
    ("natural", "bare_rock", "mountain"), ("natural", "scree", "mountain"),
    ("natural", "sand", "desert"), ("natural", "dune", "desert"),
    ("natural", "beach", "desert"), ("landuse", "quarry", "mountain"),
    ("landuse", "residential", "city"), ("landuse", "commercial", "city"),
    ("landuse", "industrial", "city"), ("landuse", "retail", "city"),
    ("leisure", "park", "grassland"), ("leisure", "garden", "grassland"),
    ("leisure", "golf_course", "grassland"), ("leisure", "pitch", "grassland"),
    ("leisure", "recreation_ground", "grassland"),
    ("landuse", "grass", "grassland"), ("landuse", "meadow", "grassland"),
    ("landuse", "village_green", "grassland"), ("landuse", "cemetery", "grassland"),
    ("landuse", "farmland", "grassland"), ("landuse", "farmyard", "grassland"),
    ("landuse", "orchard", "grassland"), ("natural", "grassland", "grassland"),
    ("natural", "fell", "grassland"),
    ("place", "suburb", "suburban"), ("place", "village", "suburban"),
]

# What the World Manager may ask the free OSM Overpass API (the map data behind
# openstreetmap.org). Several public mirrors; we try them in turn (the main one
# 504s a lot).
_OSM_DEFAULTS = {
    "enabled": True,
    "query_radius_m": 4000,
    "retry_minutes": 360,
    "min_interval_s": 8,
    "max_points": 200000,
    "timeout_s": 60,
    "user_agent": "pogo-private-server/1.0 (personal use)",
    "api": ["https://overpass.kumi.systems/api/interpreter",
            "https://overpass-api.de/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"],
}

_BIOMES_FILE = paths.BIOMES_FILE
_MAX_M = 1500.0                  # how far a terrain point still describes a cell
_cell_level = 12

# ---- REAL terrain from OSM (data/osm_biomes.json) ---------------------------
# Written by the fetch tools AND by the live lookups below. Two shapes:
#   {"points": [{lat,lng,biome}, ...]}          -- a region someone has played in
#   {"cells": {"<level-12 cell id>": biome}}    -- national map (fetch_us_biomes)
_TERRAIN_FILE = os.path.join(paths.ensure(), "osm_biomes.json")

_tlock = threading.RLock()
_terrain = {"mtime": None, "pts": []}
_cells = {"mtime": None, "map": {}, "level": 12}
_cache = {}                      # region-cell id -> biome name (memoised lookups)

# definitions from biomes.json, hot-reloaded by mtime
_INFO = {"loaded": False, "mtime": None, "gen": 0, "biomes": {}, "names": [],
         "rules": [], "osm": {}, "max_m": _MAX_M, "cell_level": _cell_level}

# live OSM lookups: one (throttled) background query per area, cached in the file
_fetch_lock = threading.RLock()
_online = {"tried": {}, "busy": set(), "last_call": 0.0, "fail_streak": 0,
           "areas": 0, "errors": 0, "points": 0}


def _num(mapping, key, default):
    try:
        return float(mapping.get(key, default))
    except (TypeError, ValueError, AttributeError):
        return float(default)


def _load_defs():
    """(Re)read biomes.json when it changes. Returns the parsed definitions."""
    try:
        m = os.path.getmtime(_BIOMES_FILE)
    except OSError:
        m = None
    with _tlock:
        if _INFO["loaded"] and _INFO["mtime"] == m:
            return _INFO
        table, names, rules = {}, [], []
        osm = {}
        max_m, level = _MAX_M, _cell_level
        if m is not None:
            try:
                with open(_BIOMES_FILE, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                doc = None
            if isinstance(doc, dict) and isinstance(doc.get("biomes"), dict):
                for name, blob in doc["biomes"].items():
                    blob = blob if isinstance(blob, dict) else {}
                    boosts = {}
                    for tname, mult in (blob.get("types") or {}).items():
                        tv = TYPE_IDS.get(str(tname).strip().lower())
                        if tv is None:
                            continue
                        try:
                            mult = float(mult)
                        except (TypeError, ValueError):
                            continue
                        if mult > 1:
                            boosts[tv] = mult
                    table[str(name)] = boosts
                    names.append(str(name))
                    for rule in (blob.get("osm") or []):
                        try:
                            key = str(rule[0])
                            val = "" if len(rule) < 2 else str(rule[1])
                        except (TypeError, IndexError, KeyError):
                            continue
                        rules.append((key, val, str(name)))
                if isinstance(doc.get("osm"), dict):
                    osm = doc["osm"]
                max_m = _num(doc, "max_distance_m", _MAX_M)
                level = int(_num(doc, "cell_level", _cell_level))
        _INFO.update(biomes=table, names=names, rules=rules, osm=osm,
                     max_m=max_m, cell_level=level)
        _INFO["loaded"], _INFO["mtime"] = True, m
        _INFO["gen"] += 1
        _cache.clear()           # the biome table changed -> re-resolve everything
        return _INFO


def generation():
    """Bumped whenever biomes.json is re-read, so callers can drop their caches."""
    return _load_defs()["gen"]


def biome_names():
    """The biome names, in biomes.json order."""
    return list(_load_defs()["names"]) or list(BIOME_NAMES)


def type_boosts(biome):
    """{type_value: multiplier} for a biome. Empty for an unknown biome, which
    leaves the base rarity pool untouched (a perfectly good 'no biome' fallback)."""
    d = _load_defs()
    if d["biomes"]:
        return d["biomes"].get(biome, {})
    return BIOMES.get(biome, {})


def osm_rules():
    """The ordered (key, value_substring, biome) OSM classification rules."""
    rules = _load_defs()["rules"]
    return list(rules) if rules else list(OSM_RULES)


def osm_keys():
    """Every OSM key the rules look at (what an Overpass query should ask for)."""
    return sorted({k for k, _v, _b in osm_rules()})


def classify_tags(tags):
    """The game biome for an OSM element's tags, or None. The first matching rule
    wins, in biomes.json order, so specific terrain is listed before city/grass."""
    for key, val, biome in osm_rules():
        got = tags.get(key)
        if got is not None and (val == "" or val in str(got)):
            return biome
    return None


def _mix(n):
    """A stable 64-bit hash (Python's hash() is salted per process, so a cell
    would land in a different biome every restart -- the world has to be the same
    place tomorrow that it was today)."""
    n &= (1 << 64) - 1
    n ^= n >> 33
    n = (n * 0xFF51AFD7ED558CCD) & ((1 << 64) - 1)
    n ^= n >> 33
    n = (n * 0xC4CEB9FE1A85EC53) & ((1 << 64) - 1)
    n ^= n >> 33
    return n


def _load_cells():
    try:
        m = os.path.getmtime(_TERRAIN_FILE)
    except OSError:
        m = None
    with _tlock:
        if _cells["mtime"] != m:
            cmap, lvl = {}, 12
            if m is not None:
                try:
                    with open(_TERRAIN_FILE, "r", encoding="utf-8") as fh:
                        d = json.load(fh)
                    lvl = int(d.get("cell_level", 12))
                    for k, v in (d.get("cells") or {}).items():
                        cmap[int(k)] = str(v)
                except (OSError, ValueError, KeyError, TypeError):
                    cmap = {}
            _cells["mtime"], _cells["map"], _cells["level"] = m, cmap, lvl
            _cache.clear()
        return _cells["map"], _cells["level"]


def _load_terrain():
    try:
        m = os.path.getmtime(_TERRAIN_FILE)
    except OSError:
        m = None
    with _tlock:
        if _terrain["mtime"] != m:
            pts = []
            if m is not None:
                try:
                    with open(_TERRAIN_FILE, "r", encoding="utf-8") as fh:
                        for p in (json.load(fh).get("points") or []):
                            pts.append((float(p["lat"]), float(p["lng"]),
                                        str(p["biome"])))
                except (OSError, ValueError, KeyError, TypeError):
                    pts = []
            _terrain["mtime"], _terrain["pts"] = m, pts
            _cache.clear()       # data changed -> drop memoised biomes
        return _terrain["pts"]


def _terrain_biome(lat, lng, max_m=None):
    pts = _load_terrain()
    if not pts:
        return None
    cosl = max(0.2, math.cos(math.radians(lat)))
    best, bestd = None, float(max_m or _load_defs()["max_m"] or _MAX_M)
    for plat, plng, b in pts:
        d = math.hypot((plat - lat) * 111320.0, (plng - lng) * 111320.0 * cosl)
        if d < bestd:
            best, bestd = b, d
    return best


# ---- live OSM lookups -------------------------------------------------------
def _osm_cfg():
    """The OSM settings: biomes.json's "osm" block over the built-in defaults."""
    cfg = dict(_OSM_DEFAULTS)
    block = _load_defs()["osm"]
    if isinstance(block, dict):
        for k in cfg:
            if k not in block:
                continue
            v = block[k]
            if k == "api":
                if isinstance(v, list) and v:
                    cfg[k] = [str(u) for u in v]
            elif k == "enabled":
                cfg[k] = bool(v)
            elif k == "user_agent":
                cfg[k] = str(v)
            else:
                cfg[k] = _num(block, k, cfg[k])
    return cfg


def osm_enabled():
    """True when the server may ask the OSM API for terrain it hasn't cached.

    OSM_BIOMES_ONLINE=0 forces it off (and =1 forces it on); otherwise the
    settings.json toggle (spawns.osm_online) wins, then biomes.json's
    osm.enabled, and finally it is on.
    """
    env = os.environ.get("OSM_BIOMES_ONLINE")
    if env not in (None, ""):
        return env.strip().lower() not in ("0", "false", "no", "off")
    try:
        from windstock.config import settings as _cfg
        return bool(_cfg.get("spawns", "osm_online", cast=bool))
    except Exception:
        pass
    block = _load_defs()["osm"]
    if isinstance(block, dict) and "enabled" in block:
        return bool(block["enabled"])
    return True


def fetch_osm_points(lat, lng, radius_m=None):
    """Ask the OSM Overpass API for the terrain around a point and return
    [{"lat", "lng", "biome"}, ...]. Raises if every mirror fails."""
    cfg = _osm_cfg()
    radius = int(radius_m or cfg["query_radius_m"])
    keys = osm_keys()
    parts = "".join(
        f'nwr(around:{radius},{float(lat)},{float(lng)})["{k}"];' for k in keys)
    query = f"[out:json][timeout:180];({parts});out center tags;"
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    data = None
    for url in cfg["api"]:
        try:
            req = urllib.request.Request(
                url, data=body, headers={"User-Agent": cfg["user_agent"]})
            with urllib.request.urlopen(req, timeout=int(cfg["timeout_s"])) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except Exception as e:                       # mirror down / no internet
            last = e
            data = None
    if data is None:
        raise RuntimeError(f"OSM Overpass unreachable ({last})")
    out = []
    for el in data.get("elements", []):
        biome = classify_tags(el.get("tags") or {})
        if not biome:
            continue
        if el.get("type") == "node":
            plat, plng = el.get("lat"), el.get("lon")
        else:                                        # way/relation -> centre
            c = el.get("center") or {}
            plat, plng = c.get("lat"), c.get("lon")
        if plat is None or plng is None:
            continue
        out.append({"lat": round(float(plat), 6), "lng": round(float(plng), 6),
                    "biome": biome})
    return out


def _merge_points(pts):
    """Add freshly fetched terrain points to data/osm_biomes.json, keeping the
    cells map and any points already there. Returns how many were new."""
    if not pts:
        return 0
    with _fetch_lock:
        try:
            with open(_TERRAIN_FILE, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        old, seen = [], set()
        for p in (doc.get("points") or []):
            try:
                key = (round(float(p["lat"]), 6), round(float(p["lng"]), 6),
                       str(p["biome"]))
            except (TypeError, ValueError, KeyError):
                continue
            old.append({"lat": round(float(p["lat"]), 6),
                        "lng": round(float(p["lng"]), 6), "biome": str(p["biome"])})
            seen.add(key)
        out, added = old, 0
        for p in pts:
            key = (p["lat"], p["lng"], p["biome"])
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
            added += 1
        cap = int(_osm_cfg()["max_points"])
        if cap > 0 and len(out) > cap:
            out = out[-cap:]                 # keep the most recently seen areas
        doc["points"] = out
        try:
            os.makedirs(os.path.dirname(_TERRAIN_FILE), exist_ok=True)
            tmp = _TERRAIN_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, _TERRAIN_FILE)
        except OSError:
            return 0
        # Fresh terrain for this area: drop the memoised biomes so the very next
        # spawn resolves against the real OSM features instead of the hash guess.
        if added:
            with _tlock:
                _cache.clear()
        return added


def refresh_area(lat, lng, radius_m=None):
    """Fetch an area's real terrain from OSM NOW and cache it in
    data/osm_biomes.json. Returns how many new points were added. Used by the
    fetch tools and by tests; the server itself uses request_area()."""
    return _merge_points(fetch_osm_points(lat, lng, radius_m))


def _fetch_worker(key, lat, lng, radius_m):
    try:
        added = _merge_points(fetch_osm_points(lat, lng, radius_m))
        with _fetch_lock:
            _online["points"] += added
            _online["areas"] += 1
            _online["fail_streak"] = 0
        print(f"[biomes] OSM terrain around ({lat:.4f},{lng:.4f}): "
              f"+{added} points", flush=True)
    except Exception as e:
        with _fetch_lock:
            _online["errors"] += 1
            _online["fail_streak"] = min(_online["fail_streak"] + 1, 6)
        print(f"[biomes] OSM lookup failed: {e}", flush=True)
    finally:
        with _fetch_lock:
            _online["busy"].discard(key)


def request_area(lat, lng, radius_m=None):
    """Ask OSM for this area's terrain in the BACKGROUND, if we haven't recently.

    Returns True when a lookup was started. It never blocks: the spawn path can't
    wait on the internet, so a region uses the hash-chosen biome until the fetched
    terrain lands, then switches over (hot-reloaded by the next spawn)."""
    if not osm_enabled():
        return False
    cfg = _osm_cfg()
    try:
        key = s2sphere.CellId.from_lat_lng(
            s2sphere.LatLng.from_degrees(lat, lng)).parent(11).id()
    except Exception:
        key = (round(lat, 2), round(lng, 2))
    now = time.time()
    with _fetch_lock:
        if key in _online["busy"] or len(_online["busy"]) >= 2:
            return False
        last = _online["tried"].get(key)
        if last is not None and now - last < max(60.0, cfg["retry_minutes"] * 60.0):
            return False
        # Space lookups out, and back off further when OSM is unreachable.
        gap = max(0.0, cfg["min_interval_s"]) * (2 ** _online["fail_streak"])
        if now - _online["last_call"] < gap:
            return False
        _online["last_call"] = now
        _online["tried"][key] = now
        _online["busy"].add(key)
    threading.Thread(target=_fetch_worker,
                     args=(key, float(lat), float(lng), radius_m),
                     daemon=True).start()
    return True


def forced_biome():
    """A biome forced for testing (the World Manager's "Force biome" dropdown),
    or "" when the real terrain should be used.

    Honoured by biome_for(), so a forced biome is what you actually get -- in the
    spawns, the map readout and the World Manager header alike. An unknown name is
    ignored (it would only strip the boosts anyway) and falls back to auto.
    """
    try:
        from windstock.config import events
        name = str(events.get().get("force_biome") or "").strip().lower()
    except Exception:
        return ""
    return name if name and name in biome_names() else ""


def status():
    """What the World Manager shows about the live terrain data."""
    _load_terrain()
    _load_cells()
    with _tlock:
        points, cells = len(_terrain["pts"]), len(_cells["map"])
    with _fetch_lock:
        busy, areas, errors = len(_online["busy"]), _online["areas"], _online["errors"]
    return {"points": points, "cells": cells, "online": osm_enabled(),
            "lookups": areas, "errors": errors, "busy": busy,
            "biomes": biome_names(), "forced": forced_biome(),
            "source": _TERRAIN_FILE}


# ---- NESTS + day/night ------------------------------------------------------
# 2016 nests: a patch of the map spawns mostly ONE species, and which species
# rotates on a cycle. We make ~1 in 8 regions a nest, picking from the classic
# nestable species, and rotate them every `rotation_days`.
NEST_SPECIES = [1, 4, 7, 25, 133, 147, 66, 63, 129, 92, 41, 74, 60, 72, 98, 116,
                43, 46, 48, 27, 21, 16, 19, 39, 37, 58, 88, 100, 104, 109]


def nest_species(lat, lng, now_ms, level=12, rotation_days=14):
    """The nest species for this region right now, or 0 if the region isn't a
    nest. Stable for the whole `rotation_days` window, then it rotates."""
    try:
        cid = s2sphere.CellId.from_lat_lng(
            s2sphere.LatLng.from_degrees(lat, lng)).parent(int(level)).id()
    except Exception:
        return 0
    period = int(now_ms // (max(1, int(rotation_days)) * 86_400_000))
    if _mix(cid ^ 0x4E455354) % 8 != 0:            # only ~1 region in 8 is a nest
        return 0
    idx = _mix(cid ^ (period * 0x9E3779B97F4A7C15)) % len(NEST_SPECIES)
    return NEST_SPECIES[idx]


# Species that come out mostly after dark / mostly in daylight (2016 behaviour).
NIGHT_SPECIES = {41, 42, 92, 93, 94, 43, 44, 45, 96, 97, 104, 105, 37, 38, 52, 53,
                 35, 36, 39, 40, 46, 47, 48, 49, 66, 24}
DAY_SPECIES = {10, 11, 12, 13, 14, 15, 16, 17, 19, 21, 25, 46, 48, 63, 69, 60, 1,
               4, 7, 123, 127, 128}


def is_night(now_ms, lng=0.0):
    """Rough local night at the player's longitude (15 deg per hour), so it works
    regardless of the server's own timezone. Night = before 6am or after 8pm."""
    t = time.gmtime(now_ms / 1000.0)
    local_h = (t.tm_hour + t.tm_min / 60.0 + float(lng) / 15.0) % 24.0
    return local_h < 6.0 or local_h >= 20.0


def biome_for(lat, lng, level=12, salt=0):
    """The biome at a location.

    Uses the real terrain of the nearest OSM feature when we have data for the
    area; asks OSM for it in the background the first time someone plays there;
    and otherwise falls back to a deterministic hash so a whole level-`level` S2
    cell (~3 km at 12) shares one biome. Memoised per region cell.

    A biome forced for testing (see forced_biome) wins over all of that."""
    forced = forced_biome()
    if forced:
        return forced
    try:
        cid = s2sphere.CellId.from_lat_lng(
            s2sphere.LatLng.from_degrees(lat, lng)).parent(int(level)).id()
    except Exception:
        return biome_names()[0]
    with _tlock:
        hit = _cache.get(cid)
    if hit is not None:
        return hit
    # Classify at the region cell's CENTRE so the whole cell is one biome.
    ctr = s2sphere.CellId(cid).to_lat_lng()
    clat, clng = ctr.lat().degrees, ctr.lng().degrees
    # 1) nearest real terrain point (from a fetch tool or an earlier live lookup)
    b = _terrain_biome(clat, clng)
    if b is None:
        # 2) the national cell map (fetch_us_biomes) -- direct lookup when this cell
        #    is at the map's level, else re-key the centre to that level.
        cmap, lvl = _load_cells()
        if cmap:
            key = cid if lvl == int(level) else s2sphere.CellId.from_lat_lng(ctr).parent(lvl).id()
            b = cmap.get(key)
    if b is None and osm_enabled():
        # 3) nothing cached for here yet -- ask the OSM API in the background and
        #    use the hash choice until it answers (the fetched terrain is picked up
        #    by the very next lookup, which reloads the file by mtime).
        try:
            request_area(clat, clng)
        except Exception:
            pass
    if b is None:
        # 4) deterministic hash, so rural gaps are still varied and consistent
        names = biome_names()
        b = names[_mix(cid ^ (int(salt) * 0x9E3779B97F4A7C15)) % len(names)]
    with _tlock:
        _cache[cid] = b
    return b
