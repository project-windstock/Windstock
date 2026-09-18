"""
Shiny Pokemon for the 2016 client (which has no shinies of its own).

How it plays:
  * a wild Pokemon looks normal on the map
  * tap it: if it's shiny, the encounter loads the shiny model (pm####_s, built by
    tools/make_shiny_bundle.py) and the windstock tweak throws sparkles
  * catch it: it stays shiny -- its details screen shows the shiny model + sparkles

Only species that have a shiny bundle in the iOS assets can be shiny.

ONE ROLL PER SPAWN: whether a spawn is shiny comes from a hash of its encounter id,
never from a random draw at encounter time. Backing out and tapping again, letting it
flee and finding it again, restarting the server -- the answer is always the same, so
there's no rerolling.

Tweak API (game host, trainer recognised by phone IP like the raid/weather screens):
  GET /shiny/state -> {"species": [25], "encounter": {"eid","pokemon_id","shiny"} | null,
                       "shiny_uids": ["123", ...]}
"""
import json
import re
import threading
import time
import zlib

import settings as _cfg

_lock = threading.Lock()
_ENCOUNTER = {}          # username -> {"eid", "pokemon_id", "shiny", "t"}
ENCOUNTER_TTL_S = 180    # an encounter nobody finished stops counting after this


def species_with_models():
    """Pokemon numbers that have a shiny bundle available for iPhone."""
    try:
        import protocol as P
        names = P._extra_digest("ios").keys()
    except Exception:
        return set()
    out = set()
    for n in names:
        m = re.fullmatch(r"pm(\d{4})_s", n)
        if m:
            out.add(int(m.group(1)))
    return out


def is_shiny(encounter_id, pokemon_id):
    """Fixed per spawn: the same encounter id always gives the same answer."""
    if not _cfg.get("shiny", "enabled", cast=bool):
        return False
    try:
        pid = int(pokemon_id)
        eid = int(encounter_id)
    except (TypeError, ValueError):
        return False
    if pid not in species_with_models():
        return False
    rate = max(0.0, min(1.0, _cfg.get("shiny", "rate", cast=float)))
    roll = zlib.crc32(f"shiny:{eid}".encode()) / 0xFFFFFFFF
    return roll < rate


def note_encounter(username, encounter_id, pokemon_id):
    shiny = is_shiny(encounter_id, pokemon_id)
    with _lock:
        _ENCOUNTER[username] = {"eid": str(encounter_id), "pokemon_id": int(pokemon_id),
                                "shiny": bool(shiny), "t": time.time()}
    return shiny


def end_encounter(username):
    with _lock:
        _ENCOUNTER.pop(username, None)


def current_encounter(username):
    with _lock:
        e = _ENCOUNTER.get(username)
        if e and time.time() - e["t"] > ENCOUNTER_TTL_S:
            _ENCOUNTER.pop(username, None)
            e = None
        return dict(e) if e else None


def handle(method, path, query, headers, body, log, ip=None):
    import rpc
    import world
    user = rpc.user_for_ip(ip)
    out = {"species": sorted(species_with_models()), "encounter": None, "shiny_uids": []}
    if user:
        enc = current_encounter(user)
        if enc:
            out["encounter"] = {k: enc[k] for k in ("eid", "pokemon_id", "shiny")}
        world.use(user)
        out["shiny_uids"] = [str(c["uid"]) for c in world.caught() if c.get("shiny")]
    return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
        json.dumps(out).encode("utf-8")
