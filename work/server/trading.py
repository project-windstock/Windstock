"""
Pokemon trades between two trainers.

The 0.35 client has no trade screen, so trades are made in Discord: Windbot
(windbot/windbot.py) knows which Discord user is which trainer (the website
links them) and asks here, over loopback only:

    GET  /windstock/trade/box?login=L      L's tradeable Pokemon, stardust, trades left
    POST /windstock/trade/quote  {a, b, a_uid, b_uid}   would this trade go through, and its cost
    POST /windstock/trade/do     {a, b, a_uid, b_uid}   make it

`a`/`b` are game logins (an email profile's handle), `a_uid` is the Pokemon `a`
gives and `b_uid` the one `b` gives. uids travel as strings: they are 64-bit.

A traded Pokemon keeps its uid, so its IVs, moves and size (all rolled from the
uid, see protocol.build_pokemon_data) come along unchanged. The giver's save
lists it in DELETED, so their game drops it on the next inventory poll, and the
receiver's game picks it up the same way. The real game's rules: no favourites,
no gym defenders, and a Pokemon that was traded once can't be traded again.

Settings: the `trading` section (settings.py). Daily counts and a log of the
last trades are kept in data/trades.json.
"""
import json
import os
import threading
import time

import datadir
import settings as _cfg
import world

TRADES_FILE = os.path.join(datadir.ensure(), "trades.json")
LOG_MAX = 500
LEGENDARY = {144, 145, 146, 150, 151}

_lock = threading.Lock()
_state = None                     # {"days": {login_key: {"day", "n"}}, "log": [...]}


def _load():
    global _state
    if _state is None:
        try:
            with open(TRADES_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            _state = {"days": dict(d.get("days") or {}), "log": list(d.get("log") or [])}
        except (OSError, ValueError, AttributeError):
            _state = {"days": {}, "log": []}
    return _state


def _save():
    tmp = TRADES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_state, fh, indent=1)
    os.replace(tmp, TRADES_FILE)


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _setting(key, cast):
    return _cfg.get("trading", key, cast=cast)


def trades_today(login):
    with _lock:
        d = _load()["days"].get(login.lower()) or {}
    return int(d.get("n") or 0) if d.get("day") == _today() else 0


def _player(login):
    """The trainer's Player, or None when there is no such save. Never makes a
    save (world.use would), and leaves this thread's current trainer alone."""
    login = str(login or "").strip()
    if not login or len(login) > 64:
        return None
    path = os.path.join(world.SAVES_DIR, world._safe_name(login) + ".json")
    if not os.path.exists(path):
        return None
    prev = getattr(world._current, "player", None)
    try:
        return world.use(login)
    finally:
        world._current.player = prev


def _name(p):
    return p.CODENAME or p.username


def species(pid):
    try:
        from admin import DEX
        return DEX[pid]
    except (ImportError, IndexError):
        return f"#{pid}"


def _blocked(p, c):
    """Why this Pokemon can't be traded, or None."""
    if world.is_deployed(c["uid"]):
        return "it's defending a gym"
    if c.get("favorite"):
        return "it's a favourite (unfavourite it in the game first)"
    if c.get("traded"):
        return "it was traded before (a Pokemon can only be traded once)"
    return None


def _row(p, c):
    return {"uid": str(c["uid"]), "id": int(c["pokemon_id"]), "name": species(int(c["pokemon_id"])),
            "nickname": str(c.get("nickname") or ""), "cp": int(c.get("cp") or 0),
            "shiny": bool(c.get("shiny")), "blocked": _blocked(p, c)}


def box(login):
    p = _player(login)
    if p is None:
        return {"ok": False, "error": "That trainer hasn't played on Windstock yet."}
    with world._lock:
        mons = [_row(p, c) for c in p.CAUGHT]
        stardust, level = int(p.STARDUST), world.level_for_xp(p.XP)
    mons.sort(key=lambda m: (m["blocked"] is not None, -m["cp"]))
    limit = _setting("daily_limit", int)
    return {"ok": True, "name": _name(p), "level": level, "stardust": stardust,
            "trades_today": trades_today(login), "daily_limit": limit, "pokemon": mons}


def _find(p, uid):
    for c in p.CAUGHT:
        if c["uid"] == uid:
            return c
    return None


def cost_for(*mons):
    special = any(c.get("shiny") or int(c["pokemon_id"]) in LEGENDARY for c in mons)
    return _setting("special_cost" if special else "cost", int)


def _check(a, b, a_uid, b_uid):
    """(error, details). details: the two players, the two Pokemon, the cost.
    Call with world._lock held."""
    if not _setting("enabled", bool):
        return "Trading is switched off on this server.", None
    try:
        a_uid, b_uid = int(a_uid), int(b_uid)
    except (TypeError, ValueError):
        return "Pick the Pokémon from the list.", None
    pa, pb = _player(a), _player(b)
    if pa is None or pb is None:
        return "Both trainers need to have played on Windstock first.", None
    if pa is pb:
        return "You can't trade with yourself.", None
    ca, cb = _find(pa, a_uid), _find(pb, b_uid)
    if ca is None:
        return f"{_name(pa)} doesn't have that Pokémon any more.", None
    if cb is None:
        return f"{_name(pb)} doesn't have that Pokémon any more.", None
    for p, c in ((pa, ca), (pb, cb)):
        why = _blocked(p, c)
        if why:
            return f"{_name(p)}'s {species(int(c['pokemon_id']))} can't be traded: {why}.", None
    need = _setting("min_level", int)
    for p in (pa, pb):
        if world.level_for_xp(p.XP) < need:
            return f"{_name(p)} needs to reach level {need} to trade.", None
    limit = _setting("daily_limit", int)
    for login, p in ((a, pa), (b, pb)):
        if trades_today(login) >= limit:
            return f"{_name(p)} has used all {limit} trades for today. More tomorrow (UTC).", None
    cost = cost_for(ca, cb)
    for p in (pa, pb):
        if p.STARDUST < cost:
            return f"{_name(p)} needs {cost:,} stardust for this trade.", None
    return None, (pa, pb, ca, cb, cost)


def _summary(pa, pb, ca, cb, cost):
    return {"a_name": _name(pa), "b_name": _name(pb), "a_mon": _row(pa, ca), "b_mon": _row(pb, cb),
            "cost": cost, "candy": _setting("candy", int)}


def quote(a, b, a_uid, b_uid):
    with world._lock:
        err, d = _check(a, b, a_uid, b_uid)
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, **_summary(*d)}


def _receive(p, c, giver):
    """`c` (already off the giver's list) joins `p`'s box as a traded Pokemon."""
    p.DELETED.pop(int(c["uid"]), None)
    c = dict(c, traded=True, traded_from=_name(giver), traded_ms=int(time.time() * 1000))
    c.pop("favorite", None)
    p.CAUGHT.append(c)
    pid = int(c["pokemon_id"])
    e = p.POKEDEX.setdefault(pid, [0, 0])
    e[1] += 1
    e[0] = max(e[0], e[1])
    if c.get("shiny"):
        p.SHINYDEX.add(pid)
    p.STATS["unique_pokedex_entries"] = len({x["pokemon_id"] for x in p.CAUGHT})


def do(a, b, a_uid, b_uid):
    """Make the trade, all or nothing: everything is checked again under the
    world lock, so a second click on Accept (or a Pokemon moved meanwhile)
    just fails."""
    import protocol
    with world._lock:
        err, d = _check(a, b, a_uid, b_uid)
        if err:
            return {"ok": False, "error": err}
        pa, pb, ca, cb, cost = d
        out = _summary(*d)
        if _find(pb, ca["uid"]) or _find(pa, cb["uid"]):
            return {"ok": False, "error": "Those two Pokémon can't swap places."}
        now = int(time.time() * 1000)
        candy = _setting("candy", int)
        for giver, taker, c in ((pa, pb, ca), (pb, pa, cb)):
            giver.CAUGHT.remove(c)
            giver.DELETED[int(c["uid"])] = now
            giver.STARDUST -= cost
            if candy > 0:
                fam = protocol.pokemon_family(int(c["pokemon_id"]))
                giver.CANDY[fam] = giver.CANDY.get(fam, 0) + candy
            _receive(taker, c, giver)
    pa.save()
    pb.save()
    with _lock:
        st = _load()
        day = _today()
        for login in (a, b):
            k = login.lower()
            cur = st["days"].get(k) or {}
            st["days"][k] = {"day": day, "n": (int(cur.get("n") or 0) if cur.get("day") == day else 0) + 1}
        st["days"] = {k: v for k, v in st["days"].items() if v.get("day") == day}
        st["log"].append({"t": now, "a": a, "b": b, "a_gave": out["a_mon"]["name"], "b_gave": out["b_mon"]["name"],
                          "a_uid": out["a_mon"]["uid"], "b_uid": out["b_mon"]["uid"], "cost": cost})
        del st["log"][:-LOG_MAX]
        try:
            _save()
        except OSError:
            pass
    return {"ok": True, **out}


def _reply(code, payload):
    return code, {"Content-Type": "application/json", "Cache-Control": "no-store"}, json.dumps(payload).encode()


def handle(method, path, query, body, local):
    """/windstock/trade/*. Loopback only: Windbot on this machine is the only
    caller, and it has already checked who is who."""
    if not local:
        return _reply(404, {"ok": False})
    from urllib.parse import parse_qs
    try:
        if method == "GET" and path == "/windstock/trade/box":
            login = (parse_qs(query).get("login") or [""])[0]
            return _reply(200, box(login))
        if method == "POST" and path in ("/windstock/trade/quote", "/windstock/trade/do"):
            d = json.loads(body or b"{}")
            if not isinstance(d, dict):
                return _reply(400, {"ok": False, "error": "Bad request."})
            fn = quote if path.endswith("/quote") else do
            return _reply(200, fn(str(d.get("a") or ""), str(d.get("b") or ""), d.get("a_uid"), d.get("b_uid")))
    except ValueError:
        return _reply(400, {"ok": False, "error": "Bad request."})
    return _reply(404, {"ok": False})
