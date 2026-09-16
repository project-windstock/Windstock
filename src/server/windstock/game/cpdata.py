"""
cpdata.json -- the DEFAULT wild-spawn table.

``cpdata.json`` (shipped beside the server) records, for every Kanto Pokemon:
its release rarity (``spawn_percent_chance``), the CP ceiling it could reach in
2016 (``max_cp_2016``) and the handful of CP values it most often appeared at in
the wild (``most_common_wild_cps``).

This module turns that file into the two decisions the map builder actually
needs:

* :func:`pick_species` -- which species spawns, weighted exactly by the file's
  ``spawn_percent_chance`` (a Pidgey really is ~16%, a Dragonite ~0.001%).
* :func:`pick_cp` -- how strong it is. 60% of the time a wild spawn sits on one
  of that species' most common wild CP values, within +/-30 CP (a Bulbasaur with
  a common CP of 600 lands in 570..630). The remaining 40% return ``None`` so the
  caller keeps its normal range-based roll, which preserves events and the
  ``spawns.cap_cp_to_species`` behaviour.

Species with ``spawn_percent_chance`` of 0 (Ditto, the legendaries) never appear
naturally, exactly as in 2016.

The file is hot-reloaded by mtime, like the rest of the server's config, so
editing it takes effect on the next map refresh with no restart.
"""
import json
import os
import threading

from windstock.config import paths

#: Chance a wild spawn uses one of the species' most common wild CP values.
COMMON_CP_CHANCE = 0.60
#: +/- window (CP) around that most common value.
COMMON_CP_TOLERANCE = 30

#: Written by the /default command when the user wants the vanilla table back.
SPAWN_MODE = "cpdata"
CLASSIC_MODE = "classic"
#: DEFAULT: cpdata.json's rarity AND the OpenStreetMap biomes from biomes.json --
#: "which species" stays vanilla, "how likely here" is flavoured by the terrain.
BIOME_MODE = "biomes"
DEFAULT_MODE = BIOME_MODE
MODES = (BIOME_MODE, SPAWN_MODE, CLASSIC_MODE)

_lock = threading.RLock()
_cache = {"mtime": None, "records": None, "by_id": None}


def _normalize(raw):
    """One record from cpdata.json -> the small dict the spawner works with."""
    try:
        pid = int(raw.get("pokedex_id"))
    except (TypeError, ValueError):
        return None
    if not 1 <= pid <= 151:
        return None
    try:
        chance = float(raw.get("spawn_percent_chance") or 0.0)
    except (TypeError, ValueError):
        chance = 0.0
    if chance < 0:
        chance = 0.0
    try:
        ceiling = int(raw.get("max_cp_2016") or 0)
    except (TypeError, ValueError):
        ceiling = 0
    cps = []
    for c in raw.get("most_common_wild_cps") or []:
        try:
            ci = int(c)
        except (TypeError, ValueError):
            continue
        if ci > 0:
            cps.append(ci)
    return {
        "pokedex_id": pid,
        "name": str(raw.get("name") or ""),
        "chance": chance,
        "max_cp": ceiling,
        "common_cps": cps,
    }


def _load_file():
    try:
        with open(paths.CPDATA_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            rec = _normalize(entry)
            if rec:
                out.append(rec)
    return out


def records():
    """Every usable record, reloaded only when cpdata.json changes."""
    with _lock:
        try:
            m = os.path.getmtime(paths.CPDATA_FILE)
        except OSError:
            m = None
        if _cache["records"] is None or m != _cache["mtime"]:
            recs = _load_file()
            _cache["records"] = recs
            _cache["by_id"] = {r["pokedex_id"]: r for r in recs}
            _cache["mtime"] = m
        return _cache["records"]


def by_id():
    records()
    return _cache["by_id"] or {}


def record(pokemon_id):
    """The cpdata record for a Pokedex number, or None."""
    try:
        pid = int(pokemon_id)
    except (TypeError, ValueError):
        return None
    return by_id().get(pid)


def available():
    """True when cpdata.json loaded and offers at least one spawning species."""
    return any(r["chance"] > 0 for r in records())


def _weighted():
    """(population, weights) for the naturally spawning species, or None."""
    pop, wts = [], []
    for r in records():
        if r["chance"] > 0:
            pop.append(r["pokedex_id"])
            wts.append(r["chance"])
    if not pop:
        return None
    return pop, wts


def pick_species(rnd, multiplier=None):
    """A wild species weighted by spawn_percent_chance, or None if cpdata is
    empty/unavailable (the caller then falls back to its built-in tiers).

    `multiplier`, when given, is called with a Pokedex number and returns how
    much the local biome favours it (1.0 = not favoured). That is how the default
    "biomes" mode combines the two tables: cpdata.json keeps every species at its
    vanilla rarity, and the terrain only shifts WHICH of them dominate here.
    """
    pair = _weighted()
    if not pair:
        return None
    pop, wts = pair
    if multiplier is None:
        return int(rnd.choices(pop, weights=wts, k=1)[0])
    boosted = []
    for pid, base in zip(pop, wts):
        try:
            m = float(multiplier(pid))
        except Exception:
            m = 1.0
        boosted.append(base * m if m > 0 else 0.0)
    if not any(boosted):
        boosted = wts
    return int(rnd.choices(pop, weights=boosted, k=1)[0])


def pick_cp(rnd, pokemon_id, common_chance=COMMON_CP_CHANCE,
            tolerance=COMMON_CP_TOLERANCE):
    """A CP guided by the species' most common wild CP values.

    Returns an int (capped at the species' 2016 ceiling) for the ~60% of spawns
    that sit on the common value, or ``None`` for the rest so the caller can use
    its normal range roll.
    """
    rec = record(pokemon_id)
    if not rec or not rec["common_cps"]:
        return None
    if rnd.random() >= common_chance:
        return None
    base = rnd.choice(rec["common_cps"])
    cp = base + rnd.randint(-tolerance, tolerance)
    cp = max(1, cp)
    if rec["max_cp"] > 0:
        cp = min(cp, rec["max_cp"])
    return int(cp)
