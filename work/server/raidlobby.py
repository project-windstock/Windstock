"""
Raid lobby API -- the data behind the windstock tweak's raid screens (0.35 iOS).

The 2016 client has no raids, so the tweak draws its own Raid Lobby / HUD / Results
on top of the game and polls this module for everything it shows. The fight itself is
still the game's gym battle; the shared boss HP lives in world.RAID_BOSSES and is hit
by protocol.build_attack_gym_response.

Lobby flow for one gym:
  waiting   trainers join, pick a team, press Ready
  countdown someone pressed Start: everyone in the lobby gets the same countdown
  fighting  the raid timer runs; the battle is the normal gym battle
  done      boss down (won) or timer out (failed); results stay up for a minute

Routes (JSON, on the game host, so the phone already trusts the certificate):
  GET  /raid/status?gym=ID     everything the screens draw
  POST /raid/join   {gym}      enter the lobby
  POST /raid/leave  {gym}
  POST /raid/ready  {gym, ready}
  POST /raid/team   {gym, uids: ["123", ...]}   up to 6 Pokemon, in order
  POST /raid/start  {gym}      start the countdown for everyone in the lobby

The tweak can't send the game's login, so the trainer is recognised by the phone's
IP address: rpc.py remembers which trainer each IP last played as.
"""
import json
import threading
import time
from urllib.parse import parse_qs

import settings as _cfg
import world

_lock = threading.RLock()
LOBBIES = {}                      # fort_id -> lobby dict (see _fresh)
MEMBER_TIMEOUT_MS = 15000         # a lobby screen polls every second; gone after this
RESULTS_MS = 60000                # how long a finished raid's results stay up


def _now():
    return int(time.time() * 1000)


def _json(obj, status=200):
    return status, {"Content-Type": "application/json",
                    "Cache-Control": "no-store"}, json.dumps(obj).encode("utf-8")


def _name(pokemon_id):
    try:
        import admin
        dex = getattr(admin, "DEX", None)
        if dex and 0 <= int(pokemon_id) < len(dex):
            return str(dex[int(pokemon_id)])
    except Exception:
        pass
    return f"#{int(pokemon_id)}"


def _fresh():
    return {"state": "waiting", "members": {}, "start_at": 0, "ends_at": 0,
            "results": None}


def _boss(fort):
    """The raid boss in this gym with its SHARED HP, or None when there is no raid."""
    import protocol as P
    if not world.raid()["on"]:
        return None
    ms = world.gym_members(fort)
    if not ms or not ms[0].get("raid"):
        return None
    b = ms[0]
    # Same max-HP formula as protocol.build_start_gym_battle_response, so the lobby
    # bar and the battle agree on the pool.
    base = P._hp_for(b["cp"], b["pokemon_id"], b["uid"])
    dmax = max(1, int(base * _cfg.get("raids", "boss_hp_multiplier", cast=float)))
    st = world.raid_boss_state(fort, dmax, _now())
    return {"pokemon_id": b["pokemon_id"], "name": _name(b["pokemon_id"]),
            "cp": b["cp"], "hp": st["hp"], "max": st["max"],
            "down_until": st["down_until"]}


def _damage(fort):
    """[(trainer, damage)] for this gym's boss, biggest first."""
    with world._lock:
        s = world.RAID_BOSSES.get(fort)
        dmg = dict(s["damage"]) if s else {}
    return sorted(dmg.items(), key=lambda kv: -kv[1])


def _advance(fort, now):
    """Move the lobby along its states. Called on every request."""
    lb = LOBBIES.get(fort)
    if lb is None:
        lb = LOBBIES[fort] = _fresh()
    if lb["state"] in ("waiting", "countdown", "done"):
        for u, m in list(lb["members"].items()):
            if now - m["seen"] > MEMBER_TIMEOUT_MS:
                del lb["members"][u]
    if lb["state"] == "countdown":
        if not lb["members"]:
            LOBBIES[fort] = lb = _fresh()                  # everyone left
        elif now >= lb["start_at"]:
            lb["state"] = "fighting"
            lb["ends_at"] = lb["start_at"] + int(
                _cfg.get("raids", "raid_seconds", cast=float) * 1000)
    if lb["state"] == "fighting":
        boss = _boss(fort)
        won = boss is not None and boss["hp"] <= 0
        if boss is None or won or now >= lb["ends_at"]:
            lb["results"] = {"won": won, "finished": now,
                             "damage": [{"name": u, "damage": d} for u, d in _damage(fort)]}
            lb["state"] = "done"
            if not won:
                # Timed out: the boss heals, so the next group starts fresh.
                with world._lock:
                    world.RAID_BOSSES.pop(fort, None)
    if lb["state"] == "done" and now - lb["results"]["finished"] > RESULTS_MS:
        LOBBIES[fort] = lb = _fresh()
    return lb


def _my_pokemon(user):
    """The trainer's Pokemon that can fight, strongest first, for the team picker."""
    world.use(user)
    out = []
    for c in world.caught():
        if int(c.get("stamina", 1) if c.get("stamina") is not None else 1) <= 0:
            continue
        if world.is_deployed(c["uid"]):
            continue
        out.append({"uid": str(c["uid"]), "pokemon_id": int(c["pokemon_id"]),
                    "name": _name(c["pokemon_id"]), "cp": int(c.get("cp", 0))})
    out.sort(key=lambda p: -p["cp"])
    return out[:60]


def _view(fort, lb, user, now):
    dmg = dict(_damage(fort))
    members = [{"name": u, "ready": bool(m["ready"]), "damage": int(dmg.get(u, 0)),
                "me": u == user}
               for u, m in sorted(lb["members"].items(), key=lambda kv: kv[1]["joined"])]
    me = lb["members"].get(user)
    return {
        "on": True, "gym": fort, "server_ms": now,
        "boss": _boss(fort),
        "lobby": {
            "state": lb["state"],
            "countdown_ms": max(0, lb["start_at"] - now) if lb["state"] == "countdown" else 0,
            "time_left_ms": max(0, lb["ends_at"] - now) if lb["state"] == "fighting" else 0,
            "raid_seconds": _cfg.get("raids", "raid_seconds", cast=float),
            "members": members,
        },
        "me": {"name": user, "in_lobby": me is not None,
               "ready": bool(me and me["ready"]),
               "team": [str(u) for u in (me["team"] if me else [])]},
        "my_pokemon": _my_pokemon(user),
        "results": lb["results"],
    }


def handle(method, path, query, headers, body, log, ip=None):
    import rpc
    q = {k: v[0] for k, v in parse_qs(query or "").items()}
    data = {}
    if body:
        try:
            data = json.loads(body.decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            data = {}
    user = rpc.user_for_ip(ip)
    if not user:
        return _json({"error": "unknown trainer -- open the game first"}, 403)
    # No gym given = "the gym I have open": the one this trainer last asked the game about.
    fort = str(data.get("gym") or q.get("gym") or rpc.gym_for_user(user) or "")
    action = path[len("/raid"):].strip("/") or "status"
    if not fort:
        return _json({"error": "no gym"}, 400)
    now = _now()
    with _lock:
        if _boss(fort) is None:
            return _json({"on": False, "gym": fort})
        lb = _advance(fort, now)
        me = lb["members"].get(user)
        if action == "join":
            if me is None:
                me = lb["members"][user] = {"joined": now, "ready": False,
                                            "team": [], "seen": now}
                log(f"[raid] {user} joined the lobby at {fort} ({len(lb['members'])} in lobby)")
        elif action == "leave":
            if lb["members"].pop(user, None) is not None:
                log(f"[raid] {user} left the lobby at {fort}")
            me = None
        elif action == "ready":
            if me is not None:
                me["ready"] = bool(data.get("ready", True))
        elif action == "team":
            if me is not None:
                uids = []
                for u in (data.get("uids") or [])[:6]:
                    try:
                        uids.append(int(u))
                    except (TypeError, ValueError):
                        pass
                me["team"] = uids
        elif action == "start":
            boss = _boss(fort)
            if me is not None and lb["state"] == "waiting" and boss and boss["hp"] > 0:
                me["ready"] = True
                lb["state"] = "countdown"
                lb["start_at"] = now + int(
                    _cfg.get("raids", "countdown_seconds", cast=float) * 1000)
                log(f"[raid] {user} started the raid at {fort} "
                    f"({len(lb['members'])} trainer(s))")
        elif action != "status":
            return _json({"error": f"unknown action {action!r}"}, 404)
        if me is not None:
            me["seen"] = now
        lb = _advance(fort, now)
        return _json(_view(fort, lb, user, now))
