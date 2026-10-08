"""
A trainer's bag and Pokemon storage, for the website's admin page.

Saves live here, on the game server; the admin page (admin.html on the
Windstock website) lives on the website. The website asks here over loopback,
with its GAME_KEY (this server's SITE_GAME_KEY), after checking that the person
asking is a website admin:

    GET  /windstock/admin/trainer?login=L     bag, eggs, candy, every Pokemon
    GET  /windstock/admin/catalog             items, species and movesets for the forms
    GET  /windstock/admin/art/<dex>.png       a Pokemon sprite
    POST /windstock/admin/item      {login, item_id, count, mode: add|take|set}
    POST /windstock/admin/give      {login, pokemon_id, level, cp?, ivs?, quick?, charged?,
                                     shiny?, nickname?}
    POST /windstock/admin/take      {login, uid}

`login` is the save name: an email profile's handle or a name login. uids
travel as strings (64-bit). Nothing here makes a save: an unknown login is an
error, never a new trainer.

A Pokemon's IVs, moves and size are all rolled from its uid
(protocol.build_pokemon_data), so giving one with chosen IVs means drawing
uids until one rolls them. Moves come out of the same roll, so chosen moves
that roll doesn't give are kept as a pick (protocol.pick_moves): any IVs go
with any of the species' moves. Given Pokemon don't count as catches: the
leaderboard's "caught" stays honest, but they do fill in the Pokedex.
"""
import hmac
import json
import os
import threading
import time
import urllib.parse

import world

LEGENDARY = {144, 145, 146, 150, 151}
MAX_LEVEL = 40
_ROLL_SECONDS = 2.0                  # longest we spend looking for a uid

# Bag items by id. 901/902 are incubators: they live in Player.INCUBATORS, not the bag.
ITEM_NAMES = {1: "Poké Ball", 2: "Great Ball", 3: "Ultra Ball", 4: "Master Ball",
              101: "Potion", 102: "Super Potion", 103: "Hyper Potion", 104: "Max Potion",
              201: "Revive", 202: "Max Revive", 301: "Lucky Egg", 401: "Incense",
              402: "Shiny Incense", 501: "Lure Module",
              701: "Razz Berry", 901: "Unlimited Incubator", 902: "Egg Incubator"}
GIVEABLE_ITEMS = [1, 2, 3, 4, 101, 102, 103, 104, 201, 202, 701, 401, 301, 501, 902, 402]
BALL_NAMES = {1: "Poké Ball", 2: "Great Ball", 3: "Ultra Ball", 4: "Master Ball"}

# HoloPokemonType
TYPE_NAMES = ["", "Normal", "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost",
              "Steel", "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon",
              "Dark", "Fairy"]

# PokemonMove (POGOProtos), the moves gamedata's movesets use. "_FAST" dropped.
MOVE_NAMES = {13: 'Wrap', 14: 'Hyper Beam', 16: 'Dark Pulse', 18: 'Sludge',
              20: 'Vice Grip', 21: 'Flame Wheel', 22: 'Megahorn', 24: 'Flamethrower',
              26: 'Dig', 28: 'Cross Chop', 30: 'Psybeam', 31: 'Earthquake',
              32: 'Stone Edge', 33: 'Ice Punch', 35: 'Discharge', 36: 'Flash Cannon',
              38: 'Drill Peck', 39: 'Ice Beam', 40: 'Blizzard', 42: 'Heat Wave',
              45: 'Aerial Ace', 46: 'Drill Run', 47: 'Petal Blizzard', 49: 'Bug Buzz',
              50: 'Poison Fang', 51: 'Night Slash', 53: 'Bubble Beam', 54: 'Submission',
              56: 'Low Sweep', 57: 'Aqua Jet', 58: 'Aqua Tail', 59: 'Seed Bomb',
              60: 'Psyshock', 62: 'Ancient Power', 63: 'Rock Tomb', 64: 'Rock Slide',
              65: 'Power Gem', 69: 'Ominous Wind', 70: 'Shadow Ball', 72: 'Magnet Bomb',
              74: 'Iron Head', 77: 'Thunder Punch', 78: 'Thunder', 79: 'Thunderbolt',
              80: 'Twister', 82: 'Dragon Pulse', 83: 'Dragon Claw', 84: 'Disarming Voice',
              85: 'Draining Kiss', 86: 'Dazzling Gleam', 87: 'Moonblast', 88: 'Play Rough',
              89: 'Cross Poison', 90: 'Sludge Bomb', 91: 'Sludge Wave', 92: 'Gunk Shot',
              94: 'Bone Club', 95: 'Bulldoze', 96: 'Mud Bomb', 99: 'Signal Beam',
              100: 'X-Scissor', 101: 'Flame Charge', 102: 'Flame Burst', 103: 'Fire Blast',
              104: 'Brine', 105: 'Water Pulse', 106: 'Scald', 107: 'Hydro Pump',
              108: 'Psychic', 109: 'Psystrike', 111: 'Icy Wind', 114: 'Giga Drain',
              115: 'Fire Punch', 116: 'Solar Beam', 117: 'Leaf Blade', 118: 'Power Whip',
              121: 'Air Cutter', 122: 'Hurricane', 123: 'Brick Break', 125: 'Swift',
              126: 'Horn Attack', 127: 'Stomp', 129: 'Hyper Fang', 131: 'Body Slam',
              132: 'Rest', 133: 'Struggle', 134: 'Scald Blastoise', 135: 'Hydro Pump Blastoise',
              136: 'Wrap Green', 137: 'Wrap Pink', 200: 'Fury Cutter', 201: 'Bug Bite',
              202: 'Bite', 203: 'Sucker Punch', 204: 'Dragon Breath', 205: 'Thunder Shock',
              206: 'Spark', 207: 'Low Kick', 208: 'Karate Chop', 209: 'Ember',
              210: 'Wing Attack', 211: 'Peck', 212: 'Lick', 213: 'Shadow Claw',
              214: 'Vine Whip', 215: 'Razor Leaf', 216: 'Mud Shot', 217: 'Ice Shard',
              218: 'Frost Breath', 219: 'Quick Attack', 220: 'Scratch', 221: 'Tackle',
              222: 'Pound', 223: 'Cut', 224: 'Poison Jab', 225: 'Acid', 226: 'Psycho Cut',
              227: 'Rock Throw', 228: 'Metal Claw', 229: 'Bullet Punch', 230: 'Water Gun',
              231: 'Splash', 232: 'Water Gun Blastoise', 233: 'Mud Slap', 234: 'Zen Headbutt',
              235: 'Confusion', 236: 'Poison Sting', 237: 'Bubble', 238: 'Feint Attack',
              239: 'Steel Wing', 240: 'Fire Fang', 241: 'Rock Smash'}

_lock = threading.Lock()           # one admin change at a time


def _log(msg):
    print(f"[admin] {msg}", flush=True)


def _species(pid):
    try:
        from admin import DEX
        return DEX[pid] or f"#{pid}"
    except (ImportError, IndexError):
        return f"#{pid}"


def _move(mid):
    import gamedata
    t = (getattr(gamedata, "MOVE_TYPES", {}) or {}).get(mid, 0)
    return {"id": int(mid), "name": MOVE_NAMES.get(mid, f"Move {mid}"),
            "type": TYPE_NAMES[t] if 0 <= t < len(TYPE_NAMES) else ""}


def _authorised(headers):
    import siteaccounts
    conf = siteaccounts._conf()
    if not conf:
        return False
    given = (headers.get("X-Game-Key") or "") if headers is not None else ""
    return hmac.compare_digest(given.encode(), conf[1].encode())


def _player(login):
    """The trainer's Player, or None when there is no such save. Never makes a
    save, and leaves this thread's current trainer alone."""
    login = str(login or "").strip()
    if not login or len(login) > 64:
        return None
    if not os.path.exists(os.path.join(world.SAVES_DIR, world._safe_name(login) + ".json")):
        return None
    prev = getattr(world._current, "player", None)
    try:
        return world.use(login)
    finally:
        world._current.player = prev


class _As:
    """Run world.* calls as trainer `p` on this thread."""
    def __init__(self, p):
        self.p = p

    def __enter__(self):
        self.prev = getattr(world._current, "player", None)
        world._current.player = self.p
        return self.p

    def __exit__(self, *exc):
        world._current.player = self.prev


def _level_of(pid, cp, ivs):
    """The level (1-40) a CP works out to, for show. Above 40: 40."""
    import gamedata
    import protocol
    cpm = protocol.cpm_for(pid, cp, *ivs)
    table = gamedata.CPM or [0.5]
    best = min(range(len(table)), key=lambda i: abs(table[i] - cpm))
    return best + 1


def _mon(p, c):
    import protocol
    pid, uid = int(c["pokemon_id"]), int(c["uid"])
    cp = int(c.get("cp") or 10)
    ivs = protocol._ivs(uid)
    quick, charged = protocol.moves_for(pid, uid)
    height, weight = protocol.pokemon_size(pid, uid)
    hp_max = protocol.max_hp(pid, uid, cp)
    types = (protocol._poke_meta().get(pid) or {}).get("types") or ()
    with _As(p):
        fort = world.deployed_fort(uid)
    return {"uid": str(uid), "id": pid, "name": _species(pid),
            "nickname": str(c.get("nickname") or ""), "cp": cp,
            "hp": protocol.current_hp(c), "maxHp": hp_max,
            "ivs": list(ivs), "ivPct": round(sum(ivs) * 100 / 45),
            "level": _level_of(pid, cp, ivs),
            "types": [TYPE_NAMES[t] for t in types if 0 < t < len(TYPE_NAMES)],
            "quick": _move(quick), "charged": _move(charged),
            "height": round(height, 2), "weight": round(weight, 2),
            "shiny": bool(c.get("shiny")), "favorite": bool(c.get("favorite")),
            "legendary": pid in LEGENDARY,
            "caughtMs": int(c.get("caught_ms") or 0),
            "ball": BALL_NAMES.get(int(c.get("pokeball") or 0), ""),
            "upgrades": int(c.get("num_upgrades") or 0),
            "gym": bool(fort),
            "origin": ("given by an admin" if c.get("gift") == "admin"
                       else "research reward" if c.get("reward")
                       else f"traded from {c['traded_from']}" if c.get("traded_from")
                       else "hatched" if c.get("hatched") else "caught")}


def trainer(login):
    p = _player(login)
    if p is None:
        return {"ok": False, "error": "No game save for this trainer yet (they haven't played online)."}
    import protocol
    with world._lock:
        caught = [dict(c) for c in p.CAUGHT]
        bag = sorted((int(i), int(n)) for i, n in p.BAG.items() if n > 0)
        incs = [dict(i) for i in p.INCUBATORS]
        eggs = [dict(e) for e in p.EGGS]
        candy = sorted((int(f), int(n)) for f, n in p.CANDY.items() if n > 0)
        head = {"login": p.username, "trainer": p.CODENAME or p.username,
                "level": world.level_for_xp(p.XP), "xp": int(p.XP), "team": int(p.TEAM or 0),
                "stardust": int(p.STARDUST), "coins": int(p.COINS),
                "maxPokemon": int(p.MAX_POKEMON), "maxItems": int(p.MAX_ITEMS),
                "kmWalked": round(float(p.STATS.get("km_walked", 0.0) or 0.0), 2)}
    items = [{"id": i, "name": ITEM_NAMES.get(i, f"Item {i}"), "count": n} for i, n in bag]
    extra = [i for i in incs if i.get("item") == 902]
    if extra:
        items.append({"id": 902, "name": ITEM_NAMES[902], "count": len(extra),
                      "note": "uses left: " + ", ".join(str(i.get("uses")) for i in extra)})
    km = head["kmWalked"]
    egg_rows = []
    for e in eggs:
        inc = e.get("incubator")
        left = (max(0.0, e.get("target_km", 0.0) - (km - e.get("start_km", 0.0)))
                if inc else float(e.get("target_km", 0.0)))
        egg_rows.append({"km": float(e.get("target_km", 0.0)), "incubating": bool(inc),
                         "kmLeft": round(left, 2)})
    mons = [_mon(p, c) for c in caught]
    mons.sort(key=lambda m: -m["caughtMs"])
    return {"ok": True, **head,
            "itemsUsed": sum(n for _, n in bag), "bag": items, "eggs": egg_rows,
            "candy": [{"family": f, "name": _species(f), "count": n} for f, n in candy],
            "pokemon": mons}


def catalog():
    import gamedata
    species = []
    for pid in range(1, 152):
        species.append({"id": pid, "name": _species(pid),
                        "quick": [_move(m) for m in gamedata.QUICK.get(pid, [])],
                        "charged": [_move(m) for m in gamedata.CHARGED.get(pid, [])]})
    return {"ok": True, "maxLevel": MAX_LEVEL,
            "items": [{"id": i, "name": ITEM_NAMES[i]} for i in GIVEABLE_ITEMS],
            "species": species}


def _int(v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def item(login, item_id, count, mode):
    p = _player(login)
    if p is None:
        return {"ok": False, "error": "No game save for this trainer."}
    iid = _int(item_id, 1, 100000)
    if iid is None or iid not in ITEM_NAMES or iid == 901:
        return {"ok": False, "error": "Pick an item."}
    n = _int(count, 0, 9999)
    if n is None or (mode != "set" and n < 1):
        return {"ok": False, "error": "Pick a number from 1 to 9999."}
    if mode not in ("add", "take", "set"):
        return {"ok": False, "error": "Unknown change."}
    name = ITEM_NAMES[iid]
    with _lock, _As(p):
        with world._lock:
            if iid == 902:
                spare = [i for i in p.INCUBATORS if i.get("item") == 902]
                have = len(spare)
            else:
                have = int(p.BAG.get(iid, 0))
            want = have + n if mode == "add" else max(0, have - n) if mode == "take" else n
            if iid == 902 and want < have:
                # Only idle incubators can go: one with an egg in it stays.
                idle = [i for i in spare if not i.get("egg")]
                drop = idle[:have - want]
                for inc in drop:
                    p.INCUBATORS.remove(inc)
                want = have - len(drop)
            elif iid != 902:
                if want:
                    p.BAG[iid] = want
                else:
                    p.BAG.pop(iid, None)
        if iid == 902 and want > have:
            world.add_item(902, want - have)
        p.save()
    _log(f"{p.username}: {name} {have} -> {want}")
    return {"ok": True, "item": name, "before": have, "after": want}


def reward(login, d):
    """A website reward (referrals, accounts.js): {"coins": n, "items": {item_id: n}, "why"}.
    Added on top of what the trainer has. 404-style error when there is no save yet --
    the website keeps the reward and sends it again once the trainer has played."""
    p = _player(login)
    if p is None:
        return {"ok": False, "error": "No save for that trainer yet.", "nosave": True}
    try:
        coins = max(0, min(100000, int(d.get("coins") or 0)))
        items = {int(k): max(0, min(999, int(v))) for k, v in (d.get("items") or {}).items()}
    except (TypeError, ValueError, AttributeError):
        return {"ok": False, "error": "Bad reward."}
    if any(k not in ITEM_NAMES or k == 902 for k in items):
        return {"ok": False, "error": "Unknown item."}
    with _lock, _As(p):
        with world._lock:
            p.COINS = int(p.COINS) + coins
            for iid, n in items.items():
                if n:
                    p.BAG[iid] = int(p.BAG.get(iid, 0)) + n
        p.save()
    why = str(d.get("why") or "reward")[:60]
    _log(f"{p.username}: {why}: +{coins} coins"
         + "".join(f", +{n} {ITEM_NAMES[i]}" for i, n in items.items() if n))
    return {"ok": True, "coins": int(p.COINS)}


def _roll_uid(p, ivs):
    """A fresh uid that rolls these IVs (None = any). None when it took too
    long (1 in 4096 uids rolls a given spread: well inside the time)."""
    import protocol
    with world._lock:
        used = {int(c.get("uid", 0)) for c in p.CAUGHT} | {int(u) for u in p.DELETED}
    end = time.monotonic() + _ROLL_SECONDS
    want_ivs = tuple(ivs) if ivs else None
    while time.monotonic() < end:
        for _ in range(2000):
            uid = world._fresh_uid(used)
            if want_ivs and protocol._ivs(uid) != want_ivs:
                continue
            return uid
    return None


def give(login, d):
    import gamedata
    import protocol
    p = _player(login)
    if p is None:
        return {"ok": False, "error": "No game save for this trainer."}
    pid = _int(d.get("pokemon_id"), 1, 151)
    if pid is None:
        return {"ok": False, "error": "Pick a Pokémon."}
    level = _int(d.get("level") or 20, 1, MAX_LEVEL)
    if level is None:
        return {"ok": False, "error": f"Level is 1 to {MAX_LEVEL}."}
    cp = None
    if d.get("cp") not in (None, "", 0, "0"):
        cp = _int(d.get("cp"), 10, 9999)
        if cp is None:
            return {"ok": False, "error": "CP is 10 to 9999."}
    ivs = None
    if d.get("ivs") not in (None, "", "random"):
        raw = d.get("ivs")
        ivs = [_int(x, 0, 15) for x in raw] if isinstance(raw, list) and len(raw) == 3 else [None]
        if None in ivs:
            return {"ok": False, "error": "IVs are three numbers from 0 to 15."}
    quick = _int(d.get("quick"), 1, 1000) if d.get("quick") else None
    charged = _int(d.get("charged"), 1, 1000) if d.get("charged") else None
    if quick and quick not in gamedata.QUICK.get(pid, []):
        return {"ok": False, "error": f"{_species(pid)} can't know that fast move."}
    if charged and charged not in gamedata.CHARGED.get(pid, []):
        return {"ok": False, "error": f"{_species(pid)} can't know that charged move."}
    nickname = str(d.get("nickname") or "").replace("\n", " ").strip()[:12]
    shiny = bool(d.get("shiny"))
    with _lock:
        uid = _roll_uid(p, ivs)
        if uid is None:
            return {"ok": False, "error": "Couldn't roll those IVs in time. Try again."}
        if quick or charged:
            # Moves the uid's own roll doesn't give are kept as a pick.
            rq, rc = protocol.moves_for(pid, uid)
            if (quick or rq) != rq or (charged or rc) != rc:
                protocol.pick_moves(pid, uid, quick or rq, charged or rc)
        if cp is None:
            cp = protocol.cp_at_level(pid, uid, gamedata.CPM[level - 1])
        c = {"uid": uid, "pokemon_id": pid, "cp": cp, "caught_ms": int(time.time() * 1000),
             "pokeball": 1, "gift": "admin"}
        if shiny:
            c["shiny"] = True
        if nickname:
            c["nickname"] = nickname
        with world._lock:
            p.DELETED.pop(uid, None)
            p.CAUGHT.append(c)
            e = p.POKEDEX.setdefault(pid, [0, 0])
            e[1] += 1
            e[0] = max(e[0], e[1])
            if shiny:
                p.SHINYDEX.add(pid)
            p.STATS["unique_pokedex_entries"] = len({x["pokemon_id"] for x in p.CAUGHT})
            full = len(p.CAUGHT) > p.MAX_POKEMON
        p.save()
    mon = _mon(p, c)
    _log(f"{p.username}: given {'shiny ' if shiny else ''}{mon['name']} CP{cp} "
         f"{'/'.join(map(str, mon['ivs']))} uid {uid}")
    out = {"ok": True, "pokemon": mon}
    if full:
        out["warning"] = "Their storage is over its limit now."
    return out


def take(login, uid):
    p = _player(login)
    if p is None:
        return {"ok": False, "error": "No game save for this trainer."}
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Pick a Pokémon."}
    with _lock, _As(p):
        member = world.deployed_member(uid)
        if member:
            world.recall(member[0], uid)    # off the gym first, as this trainer
        with world._lock:
            for i, c in enumerate(p.CAUGHT):
                if int(c["uid"]) == uid:
                    gone = p.CAUGHT.pop(i)
                    break
            else:
                return {"ok": False, "error": "They don't have that Pokémon any more."}
            # Listed as deleted, so the phone drops it on its next inventory poll.
            p.DELETED[uid] = int(time.time() * 1000)
        p.save()
    import protocol
    if uid in protocol._picks():
        protocol.pick_moves(0, uid, None, None)
    name = _species(int(gone["pokemon_id"]))
    _log(f"{p.username}: took {name} CP{gone.get('cp')} uid {uid}"
         + (" (was defending a gym)" if member else ""))
    return {"ok": True, "name": name, "cp": int(gone.get("cp") or 0), "fromGym": bool(member)}


def _reply(code, payload):
    return code, {"Content-Type": "application/json", "Cache-Control": "no-store"}, json.dumps(payload).encode()


def handle(method, path, query, body, headers, local):
    """/windstock/admin/*: loopback and the website's key, or a plain 404."""
    if not local or not _authorised(headers):
        return _reply(404, {"ok": False})
    q = urllib.parse.parse_qs(query or "")
    try:
        if method == "GET" and path == "/windstock/admin/trainer":
            return _reply(200, trainer((q.get("login") or [""])[0]))
        if method == "GET" and path == "/windstock/admin/catalog":
            return _reply(200, catalog())
        if method == "GET" and path.startswith("/windstock/admin/art/"):
            import monart
            return monart.serve(os.path.splitext(os.path.basename(path))[0])
        if method == "POST":
            d = json.loads(body or b"{}")
            if not isinstance(d, dict):
                return _reply(400, {"ok": False, "error": "Bad request."})
            login = str(d.get("login") or "")
            if path == "/windstock/admin/item":
                return _reply(200, item(login, d.get("item_id"), d.get("count"), str(d.get("mode") or "")))
            if path == "/windstock/admin/give":
                return _reply(200, give(login, d))
            if path == "/windstock/admin/unspoof":
                import anticheat
                if _player(login) is None:
                    return _reply(200, {"ok": False, "error": "No save for that trainer."})
                return _reply(200, {"ok": True, "had": anticheat.clear(login)})
            if path == "/windstock/admin/reward":
                return _reply(200, reward(login, d))
            if path == "/windstock/admin/take":
                return _reply(200, take(login, d.get("uid")))
    except ValueError:
        return _reply(400, {"ok": False, "error": "Bad request."})
    return _reply(404, {"ok": False})
