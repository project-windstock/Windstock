"""
Shiny models for the 2016 client (which has no shinies of its own).

How it plays:
  * a wild Pokemon looks normal on the map
  * tap it: if it is shiny, a client-side AGENT loads the shiny model
    (pm####_s, built with the Asset Editor) at the encounter
  * catch it: it stays shiny, and the agent shows the shiny model on the
    details screen too

WHAT THIS MODULE IS NOT
-----------------------
It does not, and cannot, make the 0.29/0.35 client load a variant on its own.
The spawn protos carry no model reference at all: MapPokemon, WildPokemon and
PokemonData have a `pokemon_id` and nothing that names a bundle (see
protocol.build_map_pokemon / build_wild_pokemon / build_pokemon_data). The
client resolves a species to a model through its own item templates, so the
only asset a spawn can select is the one its template already names.

That is why this is OFF by default and why nothing here changes a single byte
on the wire. This module is the server half of a two-part feature:

  * SERVER (here): judge each spawn, and publish the verdict as JSON on
    GET /shiny/state for the agent to poll.
  * CLIENT (the agent, `work/mods/android-035` in project-windstock/windstock's
    bracky-dev branch, gitignored and NOT in that repo): at encounter time,
    swap pm#### for pm####_s.

The bundles themselves need no help from us: they are ordinary variant
bundles registered in the assets' extra_digest.json and served through the
normal asset digest. See protocol._extra_digest / _rebuild_digest.

Ported from bracky-dev's work/server/shiny.py, which does the same thing for
a 0.35 APK plus that agent. Kept deliberately SMALLER than the original: the
Shiny Charm, Shiny Incense, gym-battle and map shiny states are all still
absent, because every one of them needs a game-master item template or a
client feature this branch does not have. They are reported as null/0 so an
agent written against the bracky-dev response still parses this one.
"""
import json
import re
import threading
import time
import zlib

from windstock.config import settings as _cfg

_lock = threading.Lock()

# username -> {eid, pokemon_id, shiny, t}
_ENCOUNTER = {}
ENCOUNTER_TTL_S = 180      # an encounter nobody finished stops counting after this

# encounter_id -> (rate, when). The rate is remembered with the roll so a spawn
# met while the rate was raised cannot quietly change its mind when the rate
# goes back down -- exactly the rerolling this module promises never happens.
#
# Evicted by AGE, not by count: a spawn only lives minutes, so an hour is far
# longer than any judgement has to survive, and an eviction that dropped a LIVE
# spawn would reroll it.
_JUDGED = {}
_MAX_JUDGED = 65536


def enabled():
    """Is the shiny feature on? Everything below is a no-op when it isn't."""
    return bool(_cfg.get("shiny", "enabled", cast=bool))


def species_with_models():
    """Pokemon numbers that have a pm####_s bundle, on either platform.

    A phone only ever downloads its own set (assets_ios/extra_digest.json,
    assets/extra_digest.json), so a species counts if EITHER has one -- same
    rule bracky-dev used, and the same list follows whatever the Asset Editor
    has built, with no list to keep in sync by hand.
    """
    names = []
    try:
        from windstock.game import protocol as P
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
    """The shiny rate right now: the plain base rate, clamped to 0..1."""
    return max(0.0, min(1.0, _cfg.get("shiny", "rate", cast=float)))


def is_shiny(encounter_id, pokemon_id):
    """Fixed per spawn: the same encounter id always gives the same answer.

    Backing out and tapping again, letting it flee and finding it again,
    restarting the server -- the answer is always the same, so there is no
    rerolling.
    """
    if not enabled():
        return False
    try:
        pid = int(pokemon_id)
        eid = int(encounter_id)
    except (TypeError, ValueError):
        return False
    if _cfg.get("shiny", "require_model", cast=bool) and pid not in species_with_models():
        # No pm####_s on disk: the agent would be told to load a bundle that
        # does not exist, so this species simply cannot be shiny.
        return False
    ttl = max(60, _cfg.get("shiny", "judged_ttl_s", cast=int))
    now = time.time()
    with _lock:
        hit = _JUDGED.get(eid)
        if hit is None:
            if len(_JUDGED) >= _MAX_JUDGED:
                stale = [k for k, (_r, t) in _JUDGED.items() if now - t > ttl]
                for k in (stale or
                          sorted(_JUDGED, key=lambda k: _JUDGED[k][1])[:_MAX_JUDGED // 4]):
                    _JUDGED.pop(k, None)
            hit = _JUDGED[eid] = (effective_rate(), now)
        rate = hit[0]
    roll = zlib.crc32(f"shiny:{eid}".encode()) / 0xFFFFFFFF
    return roll < rate


def note_encounter(username, encounter_id, pokemon_id):
    """Record what this trainer is looking at, and answer 'is it shiny?'."""
    try:
        shiny = is_shiny(encounter_id, pokemon_id)
        with _lock:
            _ENCOUNTER[username] = {"eid": str(encounter_id),
                                    "pokemon_id": int(pokemon_id),
                                    "shiny": bool(shiny), "t": time.time()}
        return shiny
    except Exception:
        return False


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


def handle(method, path, query, headers, body, log=None, ip=None):
    """GET /shiny/state -- what the agent polls.

    Response shape is the bracky-dev one so an agent written against it keeps
    working. Keys this branch has no equivalent for (charm, incense, gym, map,
    shiny_uids) are reported empty/null rather than omitted, so the agent's
    parsing does not have to special-case a short response.
    """
    out = {
        "species": sorted(species_with_models()),
        "encounter": None,
        # Nothing here records a shiny on a CAUGHT Pokemon: the Shinydex and the
        # custom medals that needed it are not on this branch.
        "shiny_uids": [],
        "charm": False,
        "incense_ms": 0,
        "rate": effective_rate(),
        "gym": None,
        "map": None,
    }
    if enabled():
        # This server identifies a trainer by the username on the RPC envelope
        # (rpc._last_user), not by phone IP as bracky-dev did.
        from windstock.game import rpc
        user = rpc._last_user[0]
        if user:
            enc = current_encounter(user)
            if enc:
                out["encounter"] = {k: enc.get(k) for k in ("eid", "pokemon_id", "shiny")}
    return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
        json.dumps(out).encode("utf-8")
