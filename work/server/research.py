"""
Research -- Field Research (the TODAY tab, with the 7-day Research Breakthrough),
Special Research storylines (the SPECIAL tab) and Timed Research (its own tab, only
while one is running). The 0.35 client has none of this, so the windstock tweak draws
the screens (binoculars button above Nearby) and this module is their backend.

  config   data/research.json               -- edited on the World Manager's Research page
  progress data/research_progress/<user>.json

Progress comes from the game itself: rpc.py calls event() after a catch, spin, hatch,
evolve, transfer, power-up, berry or gym battle. Walking is read from the trainer's
km counter, measured from when the task was handed out.

Tweak API (game host, trainer recognised by phone IP, like the raid/shiny screens):
  GET  /research/state               -> the three tabs, ready to draw
  POST /research/claim {kind, ...}   -> grants the reward, returns the new state
  GET  /research/art/<file>.png      -> official research art (research_art/)
  GET  /research/pokemon/<n>.png     -> Pokemon icon (fetched once from PokeMiners, cached)
  GET  /research/font/<file>.ttf     -> the game's Lato, so the screens match the game
"""
import copy
import json
import os
import random
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import datadir
    DATA = datadir.DATA_DIR if hasattr(datadir, "DATA_DIR") else os.path.join(HERE, "data")
except Exception:
    DATA = os.path.join(HERE, "data")
CONFIG_FILE = os.path.join(DATA, "research.json")
PROGRESS_DIR = os.path.join(DATA, "research_progress")
ART_DIR = os.path.join(HERE, "research_art")
POKEMON_ICON_URL = ("https://raw.githubusercontent.com/PokeMiners/pogo_assets/master/"
                    "Images/Pokemon/pokemon_icon_{:03d}_00.png")

_lock = threading.RLock()
_notify = {}             # user -> {"seq", "tasks", "t"}: what the toast should say

# ------------------------------------------------------------------ vocabulary
TYPES = ["", "Normal", "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost",
         "Steel", "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon",
         "Dark", "Fairy"]

TASK_TYPES = {                       # type -> (label for the World Manager)
    "catch": "Catch Pokemon",
    "throw": "Make Nice / Great / Excellent throws",
    "curveball": "Make Curveball throws",
    "spin": "Spin PokeStops",
    "hatch": "Hatch Eggs",
    "evolve": "Evolve Pokemon",
    "transfer": "Transfer Pokemon",
    "power_up": "Power up Pokemon",
    "berry": "Use Razz Berries",
    "battle": "Battle in a Gym",
    "walk": "Walk (km)",
}

ITEMS = {  # item id -> (name, art file)
    1: ("Poké Ball", "pokeball_sprite.png"), 2: ("Great Ball", "greatball_sprite.png"),
    3: ("Ultra Ball", "ultraball_sprite.png"), 4: ("Master Ball", "masterball_sprite.png"),
    101: ("Potion", "Item_0101.png"), 102: ("Super Potion", "Item_0102.png"),
    103: ("Hyper Potion", "Item_0103.png"), 104: ("Max Potion", "Item_0104.png"),
    201: ("Revive", "Item_0201.png"), 202: ("Max Revive", "Item_0202.png"),
    301: ("Lucky Egg", "luckyegg.png"), 401: ("Incense", "Incense_0.png"),
    501: ("Lure Module", "../shopicons/lure.png"), 701: ("Razz Berry", "Item_0701.png"),
    901: ("Egg Incubator", "EggIncubatorUnlimited_Empty.png"),
    902: ("Egg Incubator", "EggIncubatorIAP_Empty.png"),
}


def _dex_name(pid):
    try:
        import admin
        dex = getattr(admin, "DEX", None)
        if dex and 0 < int(pid) < len(dex):
            return str(dex[int(pid)])
    except Exception:
        pass
    return f"#{int(pid)}"


def _plural(n, one, many):
    return one if n == 1 else many


def task_text(t):
    """The task line exactly the way the game words it."""
    if t.get("text"):
        return str(t["text"])
    n = max(1, int(t.get("count", 1) or 1))
    kind = t.get("type", "catch")
    sp = int(t.get("species", 0) or 0)
    ptype = str(t.get("ptype", "") or "")
    if kind == "catch":
        if sp:
            name = _dex_name(sp)
            return f"Catch {'a' if n == 1 else n} {name}"
        what = f"{ptype}-type Pokémon" if ptype else "Pokémon"
        return f"Catch {'a' if n == 1 else n} {what}"
    if kind == "throw":
        q = (t.get("throw") or "Nice").capitalize()
        if n == 1:
            return f"Make {'an' if q == 'Excellent' else 'a'} {q} Throw"
        return f"Make {n} {q} Throws"
    if kind == "curveball":
        return "Make a Curveball Throw" if n == 1 else f"Make {n} Curveball Throws"
    if kind == "spin":
        return "Spin a PokéStop" if n == 1 else f"Spin {n} PokéStops"
    if kind == "hatch":
        return "Hatch an Egg" if n == 1 else f"Hatch {n} Eggs"
    if kind == "evolve":
        if sp:
            return f"Evolve {'a' if n == 1 else n} {_dex_name(sp)}"
        return "Evolve a Pokémon" if n == 1 else f"Evolve {n} Pokémon"
    if kind == "transfer":
        return "Transfer a Pokémon" if n == 1 else f"Transfer {n} Pokémon"
    if kind == "power_up":
        return "Power up a Pokémon" if n == 1 else f"Power up Pokémon {n} times"
    if kind == "berry":
        return ("Use a Razz Berry to help catch a Pokémon" if n == 1 else
                f"Use {n} Razz Berries to help catch Pokémon")
    if kind == "battle":
        return "Battle in a Gym" if n == 1 else f"Battle in a Gym {n} times"
    if kind == "walk":
        return f"Walk {n} km"
    return f"{kind} ×{n}"


def reward_view(r):
    """What the phone draws for a reward: icon URL + a short label."""
    kind = r.get("kind", "item")
    n = int(r.get("count", 1) or 1)
    if kind == "item":
        iid = int(r.get("item", 1) or 1)
        name, art = ITEMS.get(iid, (f"Item {iid}", "QuestReward.png"))
        return {"kind": kind, "icon": "/research/art/" + art.replace("../shopicons/", "shop/"),
                "label": f"×{n}", "name": name, "count": n}
    if kind == "stardust":
        return {"kind": kind, "icon": "/research/art/stardust_painted.png", "label": f"{n}",
                "name": "Stardust", "count": n}
    if kind == "xp":
        return {"kind": kind, "icon": "/research/art/TodayView_Icon_XP.png", "label": f"{n}",
                "name": "XP", "count": n}
    if kind == "candy":
        sp = int(r.get("species", 1) or 1)
        return {"kind": kind, "icon": "/research/art/candy_rgb.png", "label": f"×{n}",
                "name": f"{_dex_name(sp)} Candy", "count": n}
    if kind == "encounter":
        pool = [int(x) for x in (r.get("species") or [1]) if int(x)] or [1]
        if len(pool) == 1:
            return {"kind": kind, "icon": f"/research/pokemon/{pool[0]}.png", "label": "",
                    "name": f"{_dex_name(pool[0])} encounter", "count": 1}
        return {"kind": kind, "icon": "/research/art/QuestPokemonReward.png", "label": "",
                "name": "Pokémon encounter", "count": 1}
    return {"kind": kind, "icon": "/research/art/QuestReward.png", "label": "", "name": kind,
            "count": n}


# ---------------------------------------------------------------- default config
def _t(kind, count, reward, **kw):
    d = {"type": kind, "count": count, "reward": reward}
    d.update(kw)
    return d


def _item(iid, n):
    return {"kind": "item", "item": iid, "count": n}


def _enc(*species, cp=(300, 900)):
    return {"kind": "encounter", "species": list(species), "cp": list(cp)}


DEFAULT_CONFIG = {
    "field": {
        "enabled": True,
        "per_day": 3,
        "pool": [
            _t("catch", 10, _enc(129)),
            _t("catch", 5, _item(1, 5)),
            _t("catch", 3, _enc(133), ptype="Normal"),
            _t("catch", 5, _item(701, 3), ptype="Water"),
            _t("throw", 3, _enc(95), throw="Great"),
            _t("throw", 5, _item(2, 5), throw="Nice"),
            _t("throw", 1, _enc(147), throw="Excellent"),
            _t("curveball", 3, _enc(114)),
            _t("spin", 5, _item(1, 10)),
            _t("spin", 3, {"kind": "stardust", "count": 500}),
            _t("hatch", 1, _enc(102)),
            _t("hatch", 2, _enc(113)),
            _t("evolve", 1, _enc(100)),
            _t("evolve", 3, _enc(138, 140)),
            _t("transfer", 3, _item(201, 2)),
            _t("power_up", 5, {"kind": "stardust", "count": 1000}),
            _t("berry", 5, _enc(125)),
            _t("battle", 1, _item(102, 3)),
            _t("walk", 2, _item(102, 3)),
        ],
    },
    "breakthrough": {
        "enabled": True,
        "stamps": 7,
        "rewards": [_enc(142, 131, 143, 137, cp=(1200, 2000)),
                    _item(1, 20), {"kind": "stardust", "count": 1500}, _item(301, 1)],
    },
    "special": [{
        "id": "kanto-survey",
        "title": "Professor Willow's Kanto Survey",
        "description": "Professor Willow needs your help cataloguing the Pokémon of the "
                       "Kanto region.",
        "trainers": [],
        "steps": [
            {"tasks": [_t("catch", 5, _item(1, 10)),
                       _t("spin", 3, _item(701, 3)),
                       _t("throw", 3, {"kind": "stardust", "count": 500}, throw="Nice")],
             "rewards": [{"kind": "xp", "count": 500}, _enc(1, 4, 7)]},
            {"tasks": [_t("catch", 10, _item(2, 5)),
                       _t("evolve", 2, {"kind": "xp", "count": 1000}),
                       _t("hatch", 1, _item(301, 1))],
             "rewards": [{"kind": "stardust", "count": 1500}, _enc(25)]},
            {"tasks": [_t("throw", 3, _item(3, 5), throw="Great"),
                       _t("curveball", 5, {"kind": "stardust", "count": 1000}),
                       _t("walk", 5, _item(901, 1))],
             "rewards": [{"kind": "xp", "count": 2000}, _enc(147, cp=(500, 800))]},
            {"tasks": [_t("throw", 1, {"kind": "xp", "count": 1500}, throw="Excellent"),
                       _t("battle", 3, _item(202, 3)),
                       _t("catch", 20, _item(3, 10))],
             "rewards": [{"kind": "stardust", "count": 3000}, _enc(151, cp=(1100, 1100))]},
        ],
    }],
    "timed": [],
}


def _fill_ids(cfg):
    """Every task/storyline gets a stable id (progress is stored against it)."""
    for i, t in enumerate(cfg["field"]["pool"]):
        t.setdefault("id", f"f{i}-{t.get('type')}-{t.get('count')}")
    for group in ("special", "timed"):
        for s in cfg[group]:
            s.setdefault("id", f"{group}-{int(time.time() * 1000)}")
            s.setdefault("trainers", [])
            s.setdefault("steps", [])
    return cfg


def load_config():
    with _lock:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            _write_json(CONFIG_FILE, _fill_ids(cfg))
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, copy.deepcopy(v))
        return _fill_ids(cfg)


def save_config(cfg):
    with _lock:
        for k in DEFAULT_CONFIG:
            if k not in cfg:
                cfg[k] = copy.deepcopy(DEFAULT_CONFIG[k])
        _write_json(CONFIG_FILE, _fill_ids(cfg))
    return load_config()


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ------------------------------------------------------------------- progress
def _safe(user):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(user))[:64] or "player"


def _load_progress(user):
    try:
        with open(os.path.join(PROGRESS_DIR, _safe(user) + ".json"), encoding="utf-8") as fh:
            p = json.load(fh)
    except (OSError, ValueError):
        p = {}
    p.setdefault("day", "")
    p.setdefault("field", [])            # today's tasks: {id, task, progress, claimed, km0}
    p.setdefault("given", [])            # handed out from the World Manager (kept until claimed)
    p.setdefault("stamps", {"count": 0, "last_day": ""})
    p.setdefault("stories", {})          # storyline id -> {step, progress[], claimed[], step_claimed, done, km0[]}
    return p


def _save_progress(user, p):
    _write_json(os.path.join(PROGRESS_DIR, _safe(user) + ".json"), p)


def _today():
    return time.strftime("%Y-%m-%d")


def _km(user):
    try:
        import world
        world.use(user)
        return world.km_walked()
    except Exception:
        return 0.0


def _level(user):
    """The trainer's level -- the phone hides Research and All Events below 5."""
    try:
        import world
        world.use(user)
        return int(world.stats()[0])
    except Exception:
        return 0


def _roll_today(user, p, cfg):
    """A fresh set of field tasks each day (same set if asked twice the same day)."""
    today = _today()
    if p["day"] == today:
        return
    p["day"] = today
    f = cfg["field"]
    pool = f.get("pool") or []
    tasks = []
    if f.get("enabled", True) and pool:
        rng = random.Random(f"{user}:{today}")
        picks = rng.sample(range(len(pool)), min(int(f.get("per_day", 3) or 3), len(pool)))
        km = _km(user)
        for i in picks:
            tasks.append({"id": f"{today}:{i}", "task": copy.deepcopy(pool[i]), "progress": 0,
                          "claimed": False, "km0": km})
    # keep yesterday's finished-but-unclaimed tasks, like the game does
    keep = [t for t in p["field"] if not t["claimed"] and t["progress"] >= _need(t["task"])]
    p["field"] = keep + tasks


def _need(task):
    return max(1, int(task.get("count", 1) or 1))


def _assigned(story, user):
    who = story.get("trainers") or []
    return not who or user in who


def _timed_live(story, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    start = int(story.get("starts_ms", 0) or 0)
    end = int(story.get("ends_ms", 0) or 0)
    return (not start or now_ms >= start) and (not end or now_ms < end)


def _story_state(p, story, user):
    st = p["stories"].get(story["id"])
    steps = story.get("steps") or []
    if st is None:
        st = p["stories"][story["id"]] = {"step": 0, "progress": [], "claimed": [],
                                          "step_claimed": False, "done": False, "km0": []}
    if not st["done"] and st["step"] < len(steps):
        n = len(steps[st["step"]].get("tasks") or [])
        if len(st["progress"]) != n:
            km = _km(user)
            st["progress"] = [0] * n
            st["claimed"] = [False] * n
            st["km0"] = [km] * n
            st["step_claimed"] = False
    return st


def _stories_for(cfg, user, now_ms=None):
    out = []
    for s in cfg["special"]:
        if _assigned(s, user) and s.get("steps"):
            out.append(("special", s))
    for s in cfg["timed"]:
        if _assigned(s, user) and s.get("steps") and _timed_live(s, now_ms):
            out.append(("timed", s))
    return out


def _matches(task, kind, ctx):
    tt = task.get("type")
    if tt != kind:
        return False
    if kind in ("catch", "evolve"):
        sp = int(task.get("species", 0) or 0)
        if sp and sp != int(ctx.get("pokemon_id", 0) or 0):
            return False
        ptype = str(task.get("ptype", "") or "")
        if ptype and ptype not in (ctx.get("types") or []):
            return False
    if kind == "throw":
        want = (task.get("throw") or "Nice").capitalize()
        got = ctx.get("throw")
        order = ["Nice", "Great", "Excellent"]
        # a Great throw counts for a "Nice" task too, like the game
        if got not in order or order.index(got) < order.index(want):
            return False
    return True


def event(user, kind, n=1, **ctx):
    """Something happened in the game that research may count."""
    if not user:
        return
    if "pokemon_id" in ctx and "types" not in ctx:
        try:
            import protocol as P
            ctx["types"] = [TYPES[t] for t in P.pokemon_types(ctx["pokemon_id"]) if 0 < t < len(TYPES)]
        except Exception:
            ctx["types"] = []
    with _lock:
        cfg = load_config()
        p = _load_progress(user)
        _roll_today(user, p, cfg)
        changed = []            # (key, text, progress, need) for each task that moved
        for t in p["field"] + p["given"]:
            if not t["claimed"] and t["progress"] < _need(t["task"]) and _matches(t["task"], kind, ctx):
                t["progress"] = min(_need(t["task"]), t["progress"] + n)
                changed.append(("field:" + t["id"], task_text(t["task"]),
                                t["progress"], _need(t["task"])))
        for _grp, s in _stories_for(cfg, user):
            st = _story_state(p, s, user)
            if st["done"]:
                continue
            tasks = s["steps"][st["step"]].get("tasks") or []
            for i, task in enumerate(tasks):
                if st["progress"][i] < _need(task) and _matches(task, kind, ctx):
                    st["progress"][i] = min(_need(task), st["progress"][i] + n)
                    changed.append((f"story:{s['id']}:{i}", task_text(task),
                                    st["progress"][i], _need(task)))
        if changed:
            _save_progress(user, p)
            _bump(user, changed)


def _bump(user, changed):
    """Tell the phone which tasks just moved (one catch can move several).

    Keyed by task so the same task moving twice in one action counts once: the
    phone shows "N research tasks updated" for several, and the task itself with
    its progress bar when only one moved.
    """
    now = time.time()
    with _lock:
        cur = _notify.get(user) or {"seq": 0, "tasks": {}, "t": 0}
        # events from the same action (a catch + its throw) arrive together: merge them
        tasks = dict(cur.get("tasks") or {}) if now - cur["t"] < 1.0 else {}
        for key, text, progress, need in changed:
            tasks[key] = {"text": text, "progress": progress, "count": need}
        _notify[user] = {"seq": cur["seq"] + 1, "tasks": tasks, "t": now}


def _apply_walk(user, p, cfg):
    km = None

    def walked(km0):
        nonlocal km
        if km is None:
            km = _km(user)
        return max(0, int(km - float(km0 or 0)))

    for t in p["field"] + p["given"]:
        if t["task"].get("type") == "walk" and not t["claimed"]:
            t["progress"] = min(_need(t["task"]), max(t["progress"], walked(t.get("km0"))))
    for _grp, s in _stories_for(cfg, user):
        st = _story_state(p, s, user)
        if st["done"]:
            continue
        for i, task in enumerate(s["steps"][st["step"]].get("tasks") or []):
            if task.get("type") == "walk":
                km0 = st["km0"][i] if i < len(st["km0"]) else 0
                st["progress"][i] = min(_need(task), max(st["progress"][i], walked(km0)))


# ---------------------------------------------------------------------- view
def _task_view(key, task, progress, claimed):
    need = _need(task)
    return {"key": key, "text": task_text(task), "progress": min(progress, need), "count": need,
            "done": progress >= need, "claimed": bool(claimed),
            "reward": reward_view(task.get("reward") or _item(1, 1))}


def state(user):
    with _lock:
        cfg = load_config()
        p = _load_progress(user)
        _roll_today(user, p, cfg)
        _apply_walk(user, p, cfg)
        now = int(time.time() * 1000)
        today = [_task_view("given:" + t["id"], t["task"], t["progress"], t["claimed"])
                 for t in p["given"] if not t["claimed"]]
        today += [_task_view("field:" + t["id"], t["task"], t["progress"], t["claimed"])
                  for t in p["field"]]
        bt = cfg["breakthrough"]
        need = int(bt.get("stamps", 7) or 7)
        stamps = {"enabled": bool(bt.get("enabled", True)), "have": min(p["stamps"]["count"], need),
                  "need": need, "stamped_today": p["stamps"]["last_day"] == _today(),
                  "can_claim": p["stamps"]["count"] >= need,
                  "rewards": [reward_view(r) for r in bt.get("rewards") or []]}
        tabs = {"special": [], "timed": []}
        for grp, s in _stories_for(cfg, user, now):
            st = _story_state(p, s, user)
            steps = s["steps"]
            view = {"id": s["id"], "title": s.get("title", "Special Research"),
                    "description": s.get("description", ""), "steps": len(steps),
                    "step": min(st["step"], len(steps) - 1) + (1 if not st["done"] else 1),
                    "done": st["done"]}
            if grp == "timed":
                view["ends_ms"] = int(s.get("ends_ms", 0) or 0)
            if not st["done"]:
                step = steps[st["step"]]
                view["step"] = st["step"] + 1
                view["step_title"] = step.get("title", "")
                view["tasks"] = [_task_view(f"story:{s['id']}:{i}", t, st["progress"][i],
                                            st["claimed"][i])
                                 for i, t in enumerate(step.get("tasks") or [])]
                view["rewards"] = [reward_view(r) for r in step.get("rewards") or []]
                view["can_claim_step"] = all(v["claimed"] for v in view["tasks"])
            else:
                view["step"] = len(steps)
                view["tasks"] = []
                view["rewards"] = []
                view["can_claim_step"] = False
            tabs[grp].append(view)
        _save_progress(user, p)
        claimable = (any(t["done"] and not t["claimed"] for t in today) or stamps["can_claim"] or
                     any(t["done"] and not t["claimed"] for g in tabs.values() for s in g
                         for t in s["tasks"]) or
                     any(s["can_claim_step"] for g in tabs.values() for s in g))
        return {"today": {"tasks": today, "breakthrough": stamps},
                "special": tabs["special"], "timed": tabs["timed"],
                "claimable": claimable, "level": _level(user), "server_ms": now}


# -------------------------------------------------------------------- claims
def _grant(user, r, log=None):
    """Put a reward into the trainer's save. Returns what the phone should show."""
    import world
    world.use(user)
    kind = r.get("kind", "item")
    n = int(r.get("count", 1) or 1)
    view = reward_view(r)
    if kind == "item":
        world.add_item(int(r.get("item", 1) or 1), n)
    elif kind == "stardust":
        world.add_stardust(n)
    elif kind == "xp":
        world.add_xp(n)
    elif kind == "candy":
        import protocol as P
        world.add_candy(P.pokemon_family(int(r.get("species", 1) or 1)), n)
    elif kind == "encounter":
        pool = [int(x) for x in (r.get("species") or [1]) if int(x)] or [1]
        pid = random.choice(pool)
        lo, hi = (list(r.get("cp") or [300, 900]) + [900])[:2]
        cp = random.randint(int(min(lo, hi)), int(max(lo, hi)))
        view = reward_view({"kind": "encounter", "species": [pid]})
        view["pokemon_id"] = pid
        view["cp"] = cp
        view["placed"] = _place_encounter(user, pid, cp)
    if log:
        log(f"[research] {user} got {view['name']} {view.get('label', '')}".rstrip())
    return view


def _place_encounter(user, pid, cp):
    """The reward Pokemon appears at the trainer's feet (only for them) for 30 minutes."""
    import world
    import protocol as P
    loc = world.player_location(user)
    if not loc:
        try:
            import rpc
            loc = (rpc._last_loc[0], rpc._last_loc[1])
        except Exception:
            loc = None
    if not loc or not (abs(loc[0]) > 1e-6 or abs(loc[1]) > 1e-6):
        return False
    now = int(time.time() * 1000)
    uh = 0
    for ch in user:
        uh = (uh * 31 + ord(ch)) & 0xFFFFFFFF
    eid = (now ^ (uh << 20) ^ (pid << 40) ^ 0x2E5EA2C4) & ((1 << 62) - 1)
    lat, lng = loc[0] + 0.00002, loc[1] + 0.00001
    expires = now + 30 * 60 * 1000
    sid = P._hex_id((eid, "research"), 11)
    world.remember_spawn(eid, pid, lat, lng, cp, sid, expires)
    world.add_bonus_spawn(user, eid, pid, cp, lat, lng, expires)
    return True


def claim(user, data, log=None):
    kind = data.get("kind")
    key = str(data.get("key", ""))
    with _lock:
        cfg = load_config()
        p = _load_progress(user)
        _roll_today(user, p, cfg)
        _apply_walk(user, p, cfg)
        granted = []
        err = None
        if kind == "task" and (key.startswith("field:") or key.startswith("given:")):
            lst = p["field"] if key.startswith("field:") else p["given"]
            tid = key.split(":", 1)[1]
            t = next((x for x in lst if x["id"] == tid), None)
            if not t or t["claimed"] or t["progress"] < _need(t["task"]):
                err = "not ready"
            else:
                t["claimed"] = True
                granted.append(_grant(user, t["task"].get("reward") or _item(1, 1), log))
                if key.startswith("given:"):
                    p["given"] = [x for x in p["given"] if x["id"] != tid]
                # the first field task of the day earns a breakthrough stamp
                bt = cfg["breakthrough"]
                if bt.get("enabled", True) and p["stamps"]["last_day"] != _today():
                    need = int(bt.get("stamps", 7) or 7)
                    if p["stamps"]["count"] < need:
                        p["stamps"]["count"] += 1
                        p["stamps"]["last_day"] = _today()
        elif kind == "task" and key.startswith("story:"):
            _, sid, idx = key.split(":", 2)
            s = next((x for _g, x in _stories_for(cfg, user) if x["id"] == sid), None)
            st = _story_state(p, s, user) if s else None
            i = int(idx) if idx.isdigit() else -1
            if not st or st["done"] or not (0 <= i < len(st["progress"])):
                err = "not found"
            else:
                task = s["steps"][st["step"]]["tasks"][i]
                if st["claimed"][i] or st["progress"][i] < _need(task):
                    err = "not ready"
                else:
                    st["claimed"][i] = True
                    granted.append(_grant(user, task.get("reward") or _item(1, 1), log))
        elif kind == "step":
            sid = str(data.get("id", ""))
            s = next((x for _g, x in _stories_for(cfg, user) if x["id"] == sid), None)
            st = _story_state(p, s, user) if s else None
            if not st or st["done"] or not all(st["claimed"]):
                err = "not ready"
            else:
                for r in s["steps"][st["step"]].get("rewards") or []:
                    granted.append(_grant(user, r, log))
                st["step"] += 1
                st["progress"], st["claimed"], st["km0"] = [], [], []
                if st["step"] >= len(s["steps"]):
                    st["done"] = True
                if log:
                    log(f"[research] {user} finished step {st['step']} of '{s.get('title')}'")
                _story_state(p, s, user)
        elif kind == "breakthrough":
            bt = cfg["breakthrough"]
            need = int(bt.get("stamps", 7) or 7)
            if p["stamps"]["count"] < need:
                err = "not ready"
            else:
                p["stamps"]["count"] = 0
                for r in bt.get("rewards") or []:
                    granted.append(_grant(user, r, log))
                if log:
                    log(f"[research] {user} claimed a Research Breakthrough")
        else:
            err = "unknown claim"
        _save_progress(user, p)
    out = state(user)
    out["granted"] = granted
    if err:
        out["error"] = err
    return out


# --------------------------------------------------------------- World Manager
def give_field(users, task):
    task = dict(task)
    task.setdefault("id", f"g{int(time.time() * 1000)}")
    with _lock:
        for u in users:
            p = _load_progress(u)
            p["given"].append({"id": f"{task['id']}-{len(p['given'])}", "task": task,
                               "progress": 0, "claimed": False, "km0": _km(u)})
            _save_progress(u, p)
    return len(users)


def reset(user, what="all"):
    with _lock:
        p = _load_progress(user)
        if what in ("all", "today"):
            p["day"] = ""
            p["field"] = []
            p["given"] = []
        if what in ("all", "stamps"):
            p["stamps"] = {"count": 0, "last_day": ""}
        if what == "all":
            p["stories"] = {}
        elif what.startswith("story:"):
            p["stories"].pop(what[6:], None)
        _save_progress(user, p)


def set_stamps(user, n):
    with _lock:
        p = _load_progress(user)
        p["stamps"]["count"] = max(0, int(n))
        _save_progress(user, p)


def overview():
    """Every trainer's research at a glance, for the World Manager."""
    import world
    cfg = load_config()
    out = []
    for name in world.account_names():
        try:
            s = state(name)
        except Exception:
            continue
        out.append({"username": name,
                    "today": [{"text": t["text"], "progress": t["progress"], "count": t["count"],
                               "claimed": t["claimed"]} for t in s["today"]["tasks"]],
                    "stamps": s["today"]["breakthrough"]["have"],
                    "stories": [{"id": x["id"], "title": x["title"], "step": x["step"],
                                 "steps": x["steps"], "done": x["done"], "group": g}
                                for g in ("special", "timed") for x in s[g]]})
    return {"config": cfg, "trainers": out, "task_types": TASK_TYPES, "types": TYPES[1:],
            "items": {str(k): v[0] for k, v in ITEMS.items()}}


# ------------------------------------------------------------------ all events
def _preset_bonuses(cfg):
    """Short "what's on" lines for an event card, from its preset/config."""
    out = []
    if not isinstance(cfg, dict):
        return out
    mode = cfg.get("species_mode")
    if mode and mode != "all":
        out.append(str(mode).replace("_", " ").title() + " spawns")
    if cfg.get("spawn_density", 0) and int(cfg["spawn_density"]) > 5:
        out.append("More Pokémon everywhere")
    if float(cfg.get("shiny_rate", 0) or 0) > 0:
        out.append("Increased Shiny chance")
    if cfg.get("max_cp", 0) and int(cfg["max_cp"]) > 1500:
        out.append("Higher CP")
    return out[:4]


def _event_windows(row, now=None, ahead_days=14):
    """When this scheduled row runs next (or is running), as (start_ms, end_ms)."""
    now = now or time.time()
    try:
        import events as EV
        sh, sm = [int(x) for x in str(row.get("start", "00:00")).split(":")[:2]]
        eh, em = [int(x) for x in str(row.get("end", "23:59")).split(":")[:2]]
    except Exception:
        return []
    out = []
    for day in range(-1, ahead_days):
        t = time.localtime(now + day * 86400)
        date = time.strftime("%Y-%m-%d", t)
        if row.get("days") and t.tm_wday not in row["days"]:
            continue
        if row.get("from") and date < row["from"]:
            continue
        if row.get("to") and date > row["to"]:
            continue
        base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        start = base + sh * 3600 + sm * 60
        end = base + eh * 3600 + em * 60
        if end <= start:
            end += 86400                     # a window that runs past midnight
        if end > now:
            out.append((int(start * 1000), int(end * 1000)))
        if len(out) >= 3:
            break
    return out


def events_view():
    """Everything the phone's All Events screen shows: what's on now, what's next, and
    any Timed Research that is running or scheduled."""
    now_ms = int(time.time() * 1000)
    rows = []
    try:
        import events as EV
        presets = EV.PRESETS
        for row in EV.schedule():
            if not row.get("enabled", True):
                continue
            cfg = row.get("cfg") if isinstance(row.get("cfg"), dict) else presets.get(row.get("preset"))
            for start, end in _event_windows(row):
                rows.append({"title": row.get("name", "Event"), "kind": "event",
                             "preset": row.get("preset", ""), "starts_ms": start, "ends_ms": end,
                             "bonuses": _preset_bonuses(cfg),
                             "active": start <= now_ms < end})
    except Exception:
        pass
    cfg = load_config()
    for s in cfg.get("timed") or []:
        if not s.get("steps"):
            continue
        rows.append({"title": s.get("title", "Timed Research"), "kind": "research",
                     "starts_ms": int(s.get("starts_ms", 0) or 0),
                     "ends_ms": int(s.get("ends_ms", 0) or 0),
                     "bonuses": ["Timed Research"],
                     "active": _timed_live(s, now_ms)})
    # The event set by hand in the World Manager (events.json) has no schedule
    # and no end -- it simply runs until someone changes it. It was missing from
    # this screen entirely, so a server could be deep in a Legendary Hunt while
    # the calendar said "nothing scheduled". ends_ms 0 means open-ended; the
    # phone shows that as "Running now".
    try:
        import events as EV
        live = EV.get()
        if str(live.get("event_name", "")) not in ("", "Normal"):
            rows.append({"title": live.get("event_name"), "kind": "event",
                         "preset": "", "starts_ms": 0, "ends_ms": 0,
                         "bonuses": _preset_bonuses(live), "active": True})
    except Exception:
        pass
    rows.sort(key=lambda r: (not r["active"], r["starts_ms"]))
    return {"server_ms": now_ms, "now": [r for r in rows if r["active"]],
            "soon": [r for r in rows if not r["active"]]}


# ------------------------------------------------------------------ radar
def radar_view(user, sweep, log=None):
    """The Help Center's radar, for the phone's All Events screen. Same 500 m scan
    and the same once-a-minute cooldown -- a sweep here counts there, and back."""
    import helpcenter as HC
    wait = HC.radar_cooldown_left(user)
    base = {"range_m": HC.radar_range_m(), "wait_ms": wait,
            "cooldown_ms": HC.radar_cooldown_ms()}
    # Where the trainer is, so a map can centre itself BEFORE the first sweep
    # (and while the cooldown is running). The list view never needed this.
    try:
        import world as _w
        loc = _w.player_location(user)
        if loc:
            base["lat"], base["lng"] = loc[0], loc[1]
    except Exception:
        pass
    if not sweep:
        return dict(base, ok=True)
    if wait > 0:
        return dict(base, ok=False, message="The radar is still recharging.")
    scan = HC._radar_scan(user)
    if scan is None:
        return dict(base, ok=False, message="Walk around a little so the radar can find you.")
    rows = sorted(scan["rows"], key=lambda r: (not r["shiny"], r["distance_m"]))
    if log:
        shinies = sum(1 for r in rows if r["shiny"])
        log(f"[research] {user} swept the radar: {len(rows)} nearby"
            + (f", {shinies} shiny" if shinies else ""))
    return dict(base, ok=True, swept=True, rows=rows, server_ms=int(time.time() * 1000),
                lat=scan["lat"], lng=scan["lng"],
                wait_ms=HC.radar_cooldown_left(user))


# ------------------------------------------------------------------- HTTP (phone)
def _json(obj, status=200):
    return status, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
        json.dumps(obj).encode("utf-8")


def _png(path):
    kind = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    with open(path, "rb") as fh:
        return 200, {"Content-Type": kind, "Cache-Control": "max-age=86400"}, fh.read()


def _pokemon_icon(n):
    d = os.path.join(ART_DIR, "pokemon")
    path = os.path.join(d, f"{n}.png")
    if not os.path.exists(path):
        os.makedirs(d, exist_ok=True)
        try:
            with urllib.request.urlopen(POKEMON_ICON_URL.format(n), timeout=8) as r:
                data = r.read()
            with open(path + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(path + ".tmp", path)
        except Exception:
            return os.path.join(ART_DIR, "QuestPokemonReward.png")
    return path


def handle(method, path, query, headers, body, log, ip=None):
    import rpc
    rest = path[len("/research"):].strip("/")
    if rest.startswith("art/"):
        name = os.path.basename(rest[4:])
        if rest[4:].startswith("shop/"):
            f = os.path.join(HERE, "shopicons", name)
        else:
            f = os.path.join(ART_DIR, name)
        return _png(f) if os.path.exists(f) else (404, {"Content-Type": "text/plain"}, b"?")
    if rest.startswith("pokemon/"):
        try:
            n = int(os.path.splitext(os.path.basename(rest))[0])
        except ValueError:
            return 404, {"Content-Type": "text/plain"}, b"?"
        return _png(_pokemon_icon(n))
    if rest.startswith("font/"):
        f = os.path.join(ART_DIR, "fonts", os.path.basename(rest[5:]))
        if not os.path.exists(f):
            return 404, {"Content-Type": "text/plain"}, b"?"
        with open(f, "rb") as fh:
            return 200, {"Content-Type": "font/ttf", "Cache-Control": "max-age=86400"}, fh.read()
    user = rpc.user_for_ip(ip)
    if not user:
        return _json({"error": "unknown trainer -- open the game first"}, 403)
    if rest == "ping":
        n = _notify.get(user) or {"seq": 0, "tasks": {}}
        tasks = list((n.get("tasks") or {}).values())
        out = {"seq": n["seq"], "count": len(tasks), "level": _level(user)}
        if len(tasks) == 1:                 # the phone draws it with its progress bar
            out["task"] = tasks[0]
        return _json(out)
    if rest == "events":
        return _json(events_view())
    if rest == "radar":
        return _json(radar_view(user, method == "POST", log))
    if rest == "map":
        # The radar as a MAP, for the Radar tab in the game. Served from this
        # origin so it needs no login: rpc.user_for_ip already knows who is
        # playing on this device.
        return (200, {"Content-Type": "text/html; charset=utf-8",
                      "Cache-Control": "no-store"}, MAP_PAGE.encode("utf-8"))
    if rest in ("", "state"):
        return _json(state(user))
    if rest == "claim" and method == "POST":
        try:
            data = json.loads((body or b"{}").decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        return _json(claim(user, data, log))
    return _json({"error": "not found"}, 404)


# ------------------------------------------------------------- the map page
# Shown inside the game, in the Research screen's Radar tab (a web view drawn
# over Unity). Same sweep, same 500 m, same one-a-minute cooldown as the Help
# Center's radar and the phone's list -- this is the map version of it.
#
# Leaflet comes from /webassets on this very origin, never a CDN: the server is
# on the phone and often has no internet. Without tiles the map still works --
# markers, distances and taps are all local -- so a tile failure only costs the
# picture, and says so once.
MAP_PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Radar</title>
<link rel="stylesheet" href="/webassets/leaflet/leaflet.css?v=1.9.4">
<style>
  :root{ --ink:#22323f; --dim:#6f8190; --teal:#2fa88c; --pink:#e06a90; }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:#eef3f5;
            font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
            color:var(--ink);-webkit-text-size-adjust:100%}
  #map{position:absolute;inset:0;background:#dde6ea}
  .bar{position:absolute;left:0;right:0;bottom:0;z-index:500;
       padding:10px 14px calc(10px + env(safe-area-inset-bottom));
       background:rgba(255,255,255,.94);backdrop-filter:blur(8px);
       border-top:1px solid #dde6ea;display:flex;align-items:center;gap:12px}
  #sweep{flex:0 0 auto;background:var(--teal);color:#fff;border:0;border-radius:11px;
         padding:11px 18px;font-size:15px;font-weight:700;min-width:124px}
  #sweep:disabled{background:#b9c8d0}
  #status{flex:1;font-size:13px;color:var(--dim)}
  #status b{color:var(--ink)}
  .note{position:absolute;top:10px;left:10px;right:10px;z-index:500;
        background:rgba(255,255,255,.95);border-radius:11px;padding:10px 12px;
        font-size:13px;color:var(--dim);box-shadow:0 2px 10px rgba(0,0,0,.12)}
  .pin{width:46px;height:46px;margin:-23px 0 0 -23px;position:relative}
  .pin img{width:46px;height:46px;filter:drop-shadow(0 2px 3px rgba(0,0,0,.35))}
  .pin.sh:after{content:"\2605";position:absolute;right:-1px;top:-3px;color:#f2a03d;
                font-size:17px;text-shadow:0 1px 2px rgba(0,0,0,.5)}
  .leaflet-popup-content{margin:10px 12px;font-size:14px}
  .leaflet-popup-content b{display:block;font-size:15px}
  .leaflet-popup-content span{color:var(--dim);font-size:13px}
</style>

<div id="map"></div>
<div class="bar">
  <button id="sweep">Sweep</button>
  <div id="status">Looking for you…</div>
</div>

<script src="/webassets/leaflet/leaflet.js?v=1.9.4"></script>
<script>
if (window.L) L.Icon.Default.prototype.options.imagePath = "/webassets/leaflet/";
const $ = id => document.getElementById(id);
let map = null, layer = null, me = null, rows = [], cooldownUntil = 0, ticker = null;

function note(text) {
  if ($("offline")) return;
  const n = document.createElement("div");
  n.className = "note"; n.id = "offline"; n.textContent = text;
  document.body.appendChild(n);
  setTimeout(() => n.remove(), 8000);
}

function ensureMap(lat, lng) {
  if (map || !window.L) return map;
  try {
    map = L.map("map", {zoomControl: false, attributionControl: false})
           .setView([lat, lng], 17);
    const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                              {maxZoom: 19}).addTo(map);
    // The only thing here that needs the internet. Markers still work without it.
    tiles.once("tileerror", () => note("No map pictures right now — the markers "
                                     + "below are still where things are."));
    layer = L.layerGroup().addTo(map);
  } catch (e) { map = null; }
  return map;
}

function drawMe(lat, lng) {
  if (!map) return;
  if (me) { me.setLatLng([lat, lng]); return; }
  me = L.circleMarker([lat, lng], {radius: 8, color: "#fff", weight: 3,
                                   fillColor: "#2b6cf6", fillOpacity: 1}).addTo(map);
  L.circle([lat, lng], {radius: 500, color: "#2b6cf6", weight: 1,
                        fillColor: "#2b6cf6", fillOpacity: .05}).addTo(map);
}

function icon(r) {
  return L.divIcon({className: "", iconSize: [46, 46],
    html: '<div class="pin' + (r.shiny ? " sh" : "") + '">'
        + '<img src="/research/pokemon/' + r.pokemon_id + '.png" alt=""></div>'});
}

function left(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s <= 0 ? "gone" : Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function draw() {
  if (!map || !layer) return;
  layer.clearLayers();
  const now = Date.now();
  for (const r of rows) {
    if (r.expires_ms && r.expires_ms < now) continue;
    L.marker([r.lat, r.lng], {icon: icon(r), zIndexOffset: r.shiny ? 1000 : 0})
     .addTo(layer)
     .bindPopup("<b>" + (r.shiny ? "★ " : "") + r.name + "</b>"
              + "<span>" + r.distance_m + " m away · leaves in "
              + left(r.expires_ms - now) + "</span>");
  }
}

async function call(method) {
  const r = await fetch("/research/radar", {method, headers: {"Content-Type": "application/json"}});
  return r.json();
}

async function sweep(auto) {
  $("sweep").disabled = true;
  let d;
  try { d = await call("POST"); }
  catch (e) { $("status").textContent = "Could not reach the server."; return tick(); }
  cooldownUntil = Date.now() + (d.wait_ms || 0);
  if (d.lat) { ensureMap(d.lat, d.lng); drawMe(d.lat, d.lng); }
  if (!d.ok) { $("status").textContent = d.message || "Not ready."; return tick(); }
  if (d.swept) {
    rows = d.rows || [];
    if (map) map.setView([d.lat, d.lng], map.getZoom() || 17);
    draw();
  }
  tick();
}

function tick() {
  clearTimeout(ticker);
  const wait = cooldownUntil - Date.now();
  $("sweep").disabled = wait > 0;
  $("sweep").textContent = wait > 0 ? "Sweep in " + Math.ceil(wait / 1000) + "s" : "Sweep";
  if (rows.length) {
    const live = rows.filter(r => !r.expires_ms || r.expires_ms > Date.now());
    const sh = live.filter(r => r.shiny).length;
    $("status").innerHTML = "<b>" + live.length + "</b> nearby"
                          + (sh ? " · <b>" + sh + " shiny</b>" : "");
    draw();
  }
  ticker = setTimeout(tick, 1000);
}

$("sweep").onclick = () => sweep(false);

(async () => {
  // Centre on the trainer before the first sweep, so the map is never blank.
  try {
    const d = await call("GET");
    cooldownUntil = Date.now() + (d.wait_ms || 0);
    if (d.lat) { ensureMap(d.lat, d.lng); drawMe(d.lat, d.lng); $("status").textContent = "Ready."; }
    else $("status").textContent = "Open the game and walk a little so the radar can find you.";
  } catch (e) { $("status").textContent = "Could not reach the server."; }
  if (!window.L) note("The map could not start.");
  tick();
  if (cooldownUntil <= Date.now()) sweep(true);   // first sweep on open
})();
</script>
</html>
"""
