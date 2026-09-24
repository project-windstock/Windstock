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

Shiny Charm / Shiny Incense (ported from Kanto's shop) multiply the rate: the charm
permanently, the incense while it burns. Both are bought in the shop, and neither is a
bag item -- see world.SHINY_INCENSE_ITEM for why.

Tweak API (game host, trainer recognised by phone IP like the raid/weather screens):
  GET /shiny/state -> {"species": [25], "encounter": {"eid","pokemon_id","shiny"} | null,
                       "shiny_uids": ["123", ...], "charm": bool, "incense_ms": int,
                       "rate": float}
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

# encounter_id -> (rate, when). The Shiny Charm and Shiny Incense change the rate,
# and without this a spawn met while an incense burned would quietly stop being
# shiny once it ran out -- exactly the rerolling this module promises never
# happens. Judged once, remembered.
#
# Evicted by AGE, not by count: a spawn only lives minutes, so an hour is far
# longer than any judgement has to survive, and an eviction that dropped a LIVE
# spawn would reroll it. (An early version capped the count instead, and a busy
# hour silently rerolled the oldest spawns still on the map.)
_JUDGED = {}
_JUDGED_TTL_S = 3600
_MAX_JUDGED = 65536


def species_with_models():
    """Pokemon numbers that have a shiny bundle -- on either platform, since a phone only
    ever downloads its own (assets_ios/extra_digest.json, assets/extra_digest.json)."""
    names = []
    try:
        import protocol as P
        for platform in ("ios", "android"):
            names += list(P._extra_digest(platform).keys())
    except Exception:
        return set()
    out = set()
    for n in names:
        m = re.fullmatch(r"pm(\d{4})_s", n)
        if m:
            out.add(int(m.group(1)))
    return out


def effective_rate():
    """The shiny rate for the CURRENT world account, right now.

    base x Shiny Charm (permanent, bought once) x Shiny Incense (while it burns).
    Ported from Kanto, which sells the same pair; the numbers are ours and live
    in settings.
    """
    rate = max(0.0, min(1.0, _cfg.get("shiny", "rate", cast=float)))
    try:
        import world
        if world.has_shiny_charm():
            rate *= max(1.0, _cfg.get("shiny", "charm_multiplier", cast=float))
        if world.shiny_incense_ms_left() > 0:
            rate *= max(1.0, _cfg.get("shiny", "incense_multiplier", cast=float))
    except Exception:
        pass
    return max(0.0, min(1.0, rate))


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
    now = time.time()
    with _lock:
        hit = _JUDGED.get(eid)
        if hit is None:
            if len(_JUDGED) >= _MAX_JUDGED:
                stale = [k for k, (_r, t) in _JUDGED.items() if now - t > _JUDGED_TTL_S]
                for k in stale or sorted(_JUDGED, key=lambda k: _JUDGED[k][1])[:_MAX_JUDGED // 4]:
                    _JUDGED.pop(k, None)
            hit = _JUDGED[eid] = (effective_rate(), now)
        rate = hit[0]
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
        out["charm"] = world.has_shiny_charm()
        out["incense_ms"] = world.shiny_incense_ms_left()
        out["rate"] = effective_rate()
    return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
        json.dumps(out).encode("utf-8")
