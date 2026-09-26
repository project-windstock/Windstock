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
                       "rate": float,
                       "gym": {"id", "shiny_pids": [..], "battle": {"atk_pid", "atk_shiny",
                               "def_pid", "def_shiny"} | null} | null,
                       "map": {"guards": {"<fort id>": pid}} | null}
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


def set_rate(encounter_id, rate):
    """Pin one spawn's shiny chance (a Shiny Incense's own Pokemon: incense_spawn_rate),
    instead of the account-wide effective_rate(). Same fixed roll as every other spawn."""
    try:
        eid = int(encounter_id)
    except (TypeError, ValueError):
        return
    with _lock:
        _JUDGED[eid] = (max(0.0, min(1.0, float(rate))), time.time())


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


def member_shiny(m):
    """Is this gym defender shiny? Your own Pokemon keep the shine they were caught
    with (copied onto the member when deployed; looked up for older members). A
    rival (NPC) defender gets ONE fixed roll from its uid at the base rate, like a
    wild spawn -- the same gym shows the same shiny every time."""
    if m.get("shiny") is not None:
        return bool(m["shiny"])
    if m.get("npc") or m.get("raid"):
        uid = int(m.get("uid") or 0)
        with _lock:
            if uid not in _JUDGED:
                _JUDGED[uid] = (max(0.0, min(1.0, _cfg.get("shiny", "rate", cast=float))),
                                time.time())
        return is_shiny(uid, m.get("pokemon_id"))
    try:
        import world
        return bool((world.get_caught(m["uid"]) or {}).get("shiny"))
    except Exception:
        return False


def gym_state(user):
    """What the tweak needs to draw a gym's shinies: which defender species at the
    gym this trainer has open are shiny, and -- mid-battle -- whether the two
    Pokemon fighting are."""
    import rpc
    import world
    gid = rpc.gym_for_user(user)
    if not gid:
        return None
    out = {"id": gid, "shiny_pids": sorted({int(m["pokemon_id"]) for m in world.gym_members(gid)
                                             if member_shiny(m)}), "battle": None}
    live = [b for b in world.BATTLES.values()
            if b.get("gym") == gid and b.get("player") == user and not b.get("finished")]
    if live:
        b = max(live, key=lambda b: b.get("start", 0))
        dm = next((m for m in world.gym_members(gid) if m["uid"] == b["defender"]), None)
        out["battle"] = {"atk_pid": b["atk_pid"],
                         "atk_shiny": bool((world.get_caught(b["attacker"]) or {}).get("shiny")),
                         "def_pid": b["def_pid"],
                         "def_shiny": bool(dm and member_shiny(dm))}
    return out


def map_state(user):
    """Shinies to draw on the MAP: for every gym the map was sent recently, the
    species on top when that guard is shiny. (Wild Pokemon stay normal on the map
    by design -- a shiny only shows once you tap it.)"""
    import protocol as P
    import world
    guards = {}
    now = time.time()
    for fid, t in list(P._SEEN_GYMS.items()):
        if now - t > 900:
            P._SEEN_GYMS.pop(fid, None)
            continue
        ms = world.gym_members(fid)
        if ms:
            best = max(ms, key=lambda m: m.get("cp", 0))    # the one on top (world.gym_guard)
            if member_shiny(best):
                guards[fid] = int(best["pokemon_id"])
    return {"guards": guards}


def handle(method, path, query, headers, body, log, ip=None):
    import rpc
    import world
    user = rpc.user_for_ip(ip)
    out = {"species": sorted(species_with_models()), "encounter": None, "shiny_uids": []}
    if user:
        enc = current_encounter(user)
        if enc:
            out["encounter"] = {k: enc.get(k) for k in ("eid", "pokemon_id", "shiny")}
        world.use(user)
        out["shiny_uids"] = [str(c["uid"]) for c in world.caught() if c.get("shiny")]
        out["charm"] = world.has_shiny_charm()
        out["incense_ms"] = world.shiny_incense_ms_left()
        out["rate"] = effective_rate()
        try:
            out["gym"] = gym_state(user)
        except Exception:
            out["gym"] = None
        try:
            out["map"] = map_state(user)
        except Exception:
            out["map"] = None
    return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
        json.dumps(out).encode("utf-8")
