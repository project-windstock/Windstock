"""
What is left of the Help Center (https://pokemongo.zendesk.com/hc).

The Help Center PAGE is gone (2026-10-07). These pieces outlived it because
other parts of the server use them:
  - sign-in sessions (open_session / session_player): the shop, /radar
  - the radar sweep (/hc/login, /hc/spot, /hc/radar, /hc/mon): the Hub's
    Radar tab (research.py) and the desktop radar (radarsite.py)
  - the old nominations (recent / resolve): the World Manager's list
  - _DEX, the species names: events.py
"""
import json
import os
import threading
import time

_lock = threading.Lock()


import datadir

FILE = datadir.path("nominations.json")


def _load():
    try:
        with open(FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return [n for n in d.get("nominations", []) if isinstance(n, dict)]
    except (OSError, ValueError):
        return []


def _save(rows):
    try:
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"nominations": rows}, fh, indent=1)
        os.replace(tmp, FILE)
    except OSError:
        pass


# ---------------------------------------------------------------- sessions
# Signing in here checks the same password the game uses. Every call afterwards
# carries the token and the server takes the trainer name FROM the token -- the
# page never gets to say who it is, so one player can't act in another's name by
# typing a different username.
SESSION_HOURS = 12
_sessions = {}                                   # token -> (player, expires_ms)


def _now_ms():
    return int(time.time() * 1000)


def open_session(player):
    import secrets
    token = secrets.token_urlsafe(24)
    with _lock:
        for t, (_p, exp) in list(_sessions.items()):          # drop stale ones
            if exp < _now_ms():
                _sessions.pop(t, None)
        _sessions[token] = (player, _now_ms() + SESSION_HOURS * 3600 * 1000)
    return token


def session_player(token):
    """The signed-in trainer for this token, or None."""
    with _lock:
        row = _sessions.get(token or "")
        if not row:
            return None
        player, expires = row
        if expires < _now_ms():
            _sessions.pop(token, None)
            return None
    return player


def recent(n=25):
    with _lock:
        return _load()[-n:]


def resolve(nom_id, status):
    """Mark a nomination approved/rejected. Returns the row so the caller can
    place the fort."""
    with _lock:
        rows = _load()
        for r in rows:
            if r["id"] == nom_id:
                r["status"] = status
                r["resolved"] = int(time.time() * 1000)
                _save(rows)
                return dict(r)
    return None


# ---------------------------------------------------------------- radar
# One scan per cooldown (20 s), per trainer, enforced HERE -- the page also counts down, but
# that is only so the button looks honest; a reloaded page must not buy a scan.
# Defaults; the live values come from settings (radar.cooldown_seconds /
# radar.range_m) so they can be changed without a restart. Kept as module
# constants too because other modules still read them.
RADAR_COOLDOWN_MS = 20 * 1000
RADAR_RANGE_M = 500.0


def radar_cooldown_ms():
    try:
        import settings
        return max(0, int(settings.get("radar", "cooldown_seconds", cast=int))) * 1000
    except Exception:
        return RADAR_COOLDOWN_MS


def radar_range_m():
    try:
        import settings
        return max(50.0, float(settings.get("radar", "range_m", cast=float)))
    except Exception:
        return RADAR_RANGE_M
_radar_last = {}                                 # player -> ms of their last scan


def radar_cooldown_left(player):
    with _lock:
        last = _radar_last.get(player, 0)
    left = last + radar_cooldown_ms() - _now_ms()
    return max(0, int(left))


def _radar_scan(player):
    """What `player` can see around them right now, or None if we don't know
    where they are yet (they have not opened the game since the server started).

    Ground nobody has asked the map about holds nothing, so the sweep fills its
    own circle first -- a patch at a time, skipping anything already done for
    this spawn window. Standing still and sweeping again works through the rest
    rather than turning up the same empty ground.
    """
    import world
    world.use(player)                            # is_despawned reads the current account
    loc = world.player_location(player)
    if loc is None:
        return None
    lat, lng = loc
    import spawnfill
    try:
        seeded, left = spawnfill.fill(lat, lng, radar_range_m())
    except Exception:
        seeded, left = 0, 0
    import shiny as _shiny
    rows = []
    for s in world.spawns_near(lat, lng, radar_range_m()):
        pid = int(s.get("pokemon_id") or 0)
        rows.append({
            "pokemon_id": pid,
            "name": _DEX[pid] if 1 <= pid < len(_DEX) else f"#{pid}",
            "lat": s["lat"], "lng": s["lng"],
            "distance_m": round(s["distance_m"]),
            "expires_ms": int(s.get("expires_ms") or 0),
            # Asking this here settles the spawn's shiny roll at today's rate if
            # nothing had asked yet -- which is the same answer the encounter
            # would have given, since shiny.is_shiny remembers its judgement.
            "shiny": bool(_shiny.is_shiny(s["eid"], pid)),
        })
    with _lock:
        _radar_last[player] = _now_ms()
    import monart
    return {"lat": lat, "lng": lng, "range_m": radar_range_m(), "rows": rows,
            "art": monart.sizes(), "seeded": seeded, "left": left}


_DEX = [""] + (
    "Bulbasaur Ivysaur Venusaur Charmander Charmeleon Charizard Squirtle Wartortle "
    "Blastoise Caterpie Metapod Butterfree Weedle Kakuna Beedrill Pidgey Pidgeotto Pidgeot "
    "Rattata Raticate Spearow Fearow Ekans Arbok Pikachu Raichu Sandshrew Sandslash NidoranF "
    "Nidorina Nidoqueen NidoranM Nidorino Nidoking Clefairy Clefable Vulpix Ninetales Jigglypuff "
    "Wigglytuff Zubat Golbat Oddish Gloom Vileplume Paras Parasect Venonat Venomoth Diglett "
    "Dugtrio Meowth Persian Psyduck Golduck Mankey Primeape Growlithe Arcanine Poliwag Poliwhirl "
    "Poliwrath Abra Kadabra Alakazam Machop Machoke Machamp Bellsprout Weepinbell Victreebel "
    "Tentacool Tentacruel Geodude Graveler Golem Ponyta Rapidash Slowpoke Slowbro Magnemite "
    "Magneton Farfetchd Doduo Dodrio Seel Dewgong Grimer Muk Shellder Cloyster Gastly Haunter "
    "Gengar Onix Drowzee Hypno Krabby Kingler Voltorb Electrode Exeggcute Exeggutor Cubone "
    "Marowak Hitmonlee Hitmonchan Lickitung Koffing Weezing Rhyhorn Rhydon Chansey Tangela "
    "Kangaskhan Horsea Seadra Goldeen Seaking Staryu Starmie MrMime Scyther Jynx Electabuzz "
    "Magmar Pinsir Tauros Magikarp Gyarados Lapras Ditto Eevee Vaporeon Jolteon Flareon Porygon "
    "Omanyte Omastar Kabuto Kabutops Aerodactyl Snorlax Articuno Zapdos Moltres Dratini Dragonair "
    "Dragonite Mewtwo Mew".split())


def _json(obj, code=200):
    return code, {"Content-Type": "application/json",
                  "Cache-Control": "no-store"}, json.dumps(obj).encode("utf-8")


def handle(method, path, query, headers, body, log):
    try:
        d = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}

    if path.startswith("/hc/mon/"):
        # Sprite for one species, by dex number: /hc/mon/25.png. Public on purpose
        # -- it is art, and the radar page asks for it before anything is signed in.
        import monart
        return monart.serve(os.path.splitext(os.path.basename(path))[0])

    if path == "/hc/login":
        import world
        who = str(d.get("player") or "").strip()
        ok, why, real = world.check_login(who, str(d.get("password") or ""))
        if not ok:
            log(f"[help] sign-in refused for {who!r} ({why})")
            return _json({"ok": False, "message": world.login_message(why)})
        if why == "claimed":
            log(f"[help] {real} claimed their account")
        log(f"[help] {real} signed in")
        return _json({"ok": True, "player": real, "token": open_session(real)})

    if path not in ("/hc/radar", "/hc/spot"):
        return 404, {"Content-Type": "text/plain"}, b"no"
    # Everything past here needs a signed-in trainer. The name comes from the
    # token, never from the request body.
    me = session_player(d.get("token"))
    if me is None:
        return _json({"ok": False, "signed_out": True,
                      "message": "Please sign in again."})

    if path == "/hc/radar":
        wait = radar_cooldown_left(me)
        if wait > 0:
            return _json({"ok": False, "wait_ms": wait,
                          "message": "The radar is still recharging."})
        scan = _radar_scan(me)
        if scan is None:
            return _json({"ok": False, "wait_ms": 0,
                          "message": "Open the game first so the radar can find you."})
        shinies = sum(1 for r in scan["rows"] if r["shiny"])
        log(f"[help] {me} scanned: {len(scan['rows'])} nearby"
            + (f", {shinies} shiny" if shinies else ""))
        scan["ok"] = True
        scan["wait_ms"] = radar_cooldown_left(me)
        return _json(scan)

    if path == "/hc/spot":
        # Where THIS trainer is, for centring the radar map before any sweep.
        import world
        loc = world.player_location(me)
        return _json({"ok": bool(loc),
                      "lat": loc[0] if loc else 0.0, "lng": loc[1] if loc else 0.0,
                      "range_m": radar_range_m()})

    return 404, {"Content-Type": "text/plain"}, b"no"
