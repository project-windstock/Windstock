"""
World + player state for the PoGO private server, saved to disk.

MULTI-ACCOUNT: each username gets its own file in saves/<name>.json (bag, Pokemon,
XP, candy, storage...). The GYMS are deliberately SHARED in gyms.json, so two
accounts play in the same world and can see -- and battle -- each other's
defenders, the way the real game worked.

rpc.py calls use(username) at the top of every request. ThreadingHTTPServer gives
each request its own thread, so the "current player" is a thread-local and two
people playing at once never step on each other. Module-level names (world.BAG,
world.CANDY, ...) still work: a module __getattr__ forwards them to the player
whose request is being handled, so the rest of the server didn't have to change.
"""
import contextlib
import hashlib
import hmac
import json
import math
import os
import threading
import time

import random as _random
import sys as _sys

import settings as _cfg


import datadir

HERE = datadir.ensure()                           # <exe folder>/data
SAVE_FILE = os.path.join(HERE, "save.json")       # legacy single-player save
SAVES_DIR = os.path.join(HERE, "saves")           # one file per account
GYMS_FILE = os.path.join(HERE, "gyms.json")       # SHARED between accounts
LURES_FILE = os.path.join(HERE, "lures.json")     # SHARED between accounts

_lock = threading.RLock()
_current = threading.local()
_players = {}                                     # username -> Player

# Batched saves (Player.save): players changed since the last write. Not on a
# phone (ws_boot sets WS_SERVER_DIR): one trainer, nothing to batch, and iOS
# can kill the launcher outright with no chance to flush.
SAVE_DELAY = 0.0 if os.environ.get("WS_SERVER_DIR") else 1.0
_dirty = set()
_dirty_lock = threading.Lock()
_write_lock = threading.Lock()
_saver = []


def flush_saves():
    """Write every pending save now (shutdown, or anyone who needs the disk current)."""
    with _dirty_lock:
        todo = list(_dirty)
        _dirty.clear()
    for p in todo:
        p.save_now()


def _start_saver():
    if _saver:
        return
    with _dirty_lock:
        if _saver:
            return

        def loop():
            while True:
                time.sleep(SAVE_DELAY)
                try:
                    flush_saves()
                except Exception as e:      # never let the save thread die
                    print(f"[save] flush failed: {e}", flush=True)
        t = threading.Thread(target=loop, daemon=True, name="windstock-saves")
        _saver.append(t)
        t.start()


import atexit as _atexit
_atexit.register(flush_saves)

# TutorialCompletion steps 0..7 (LEGAL_SCREEN..FIRST_TIME_EXPERIENCE_COMPLETE).
# A trainer who has done all of these skips onboarding; an empty list runs it.
_TUTORIAL_COMPLETE = [0, 1, 2, 3, 4, 5, 6, 7]

# --- items -------------------------------------------------------------------
ITEM_POKE_BALL = 1
ITEM_GREAT_BALL = 2
ITEM_POTION = 101
ITEM_REVIVE = 201
ITEM_RAZZ_BERRY = 701

_STARTING_BAG = {ITEM_POKE_BALL: 50, ITEM_GREAT_BALL: 20,
                 ITEM_POTION: 20, ITEM_REVIVE: 10, ITEM_RAZZ_BERRY: 20}

# Real 2016 XP thresholds (PlayerLevelSettings.required_experience). These are
# the genuine 40 and are never generated -- levels 1-40 must cost exactly what
# they cost in the real game.
_LEVEL_XP_2016 = [0, 1000, 3000, 6000, 10000, 15000, 21000, 28000, 36000, 45000,
                  55000, 65000, 75000, 85000, 100000, 120000, 140000, 160000, 185000,
                  210000, 260000, 335000, 435000, 560000, 710000, 900000, 1100000,
                  1350000, 1650000, 2000000, 2500000, 3000000, 3750000, 4750000,
                  6000000, 7500000, 9500000, 12000000, 15000000, 20000000]

# PlayerStats.experience is a varint, but whether THIS client reads it as int32
# or int64 is unverified (POGOProtos disagrees with 0.29 often enough not to bet
# on it). Staying under the int32 ceiling is correct either way; past it the
# level ring can wrap into nonsense.
_XP_CEILING = 2_147_483_647

_LEVEL_XP_CACHE = None
_LEVEL_XP_KEY = None


def _build_level_xp():
    """The XP table, extended past 40 when max_level says so. Rebuilt only when
    the relevant settings change, since this is read on every XP award."""
    global _LEVEL_XP_CACHE, _LEVEL_XP_KEY
    try:
        cap = int(_cfg.get("progression", "max_level", cast=int))
        step = int(_cfg.get("progression", "level_xp_step", cast=int))
        accel = int(_cfg.get("progression", "level_xp_accel", cast=int))
    except Exception:
        cap, step, accel = 40, 2000000, 5000
    cap = max(1, min(500, cap))
    key = (cap, step, accel)
    if _LEVEL_XP_CACHE is not None and _LEVEL_XP_KEY == key:
        return _LEVEL_XP_CACHE
    table = list(_LEVEL_XP_2016[:cap])
    base = _LEVEL_XP_2016[-1]
    for lvl in range(len(_LEVEL_XP_2016) + 1, cap + 1):
        d = lvl - len(_LEVEL_XP_2016)
        table.append(min(_XP_CEILING, base + step * d + accel * d * d))
    _LEVEL_XP_CACHE, _LEVEL_XP_KEY = table, key
    return table


def max_level():
    return len(_build_level_xp())


class _LevelXP:
    """LEVEL_XP stayed a plain list for years and other code indexes/len()s it,
    so keep that shape while making it follow the configured cap."""

    def _t(self):
        return _build_level_xp()

    def __getitem__(self, i):
        return self._t()[i]

    def __len__(self):
        return len(self._t())

    def __iter__(self):
        return iter(self._t())


LEVEL_XP = _LevelXP()


def level_for_xp(xp):
    table = _build_level_xp()
    lvl = 1
    for i, need in enumerate(table):
        if xp >= need:
            lvl = i + 1
    return min(len(table), lvl)


def xp_for_level(level):
    """The XP floor of a level -- the inverse of level_for_xp, so setting a
    trainer's XP to this puts them exactly at the start of that level."""
    table = _build_level_xp()
    idx = max(1, min(len(table), int(level))) - 1
    return table[idx]


def level_bounds(xp):
    table = _build_level_xp()
    lvl = level_for_xp(xp)
    prev = table[lvl - 1] if lvl - 1 < len(table) else table[-1]
    nxt = table[lvl] if lvl < len(table) else table[-1]
    return prev, nxt


def _safe_name(username):
    keep = "".join(c for c in (username or "player") if c.isalnum() or c in "-_")
    return (keep or "player")[:32].lower()


# ================================================================== the player
class Player:
    def __init__(self, username):
        self.username = username
        self.file = os.path.join(SAVES_DIR, _safe_name(username) + ".json")
        self.gone = False
        self.BAG = dict(_STARTING_BAG)
        self.CAUGHT = []
        self.CANDY = {}
        self.STARDUST = 5000
        self.XP = 0
        self.LEVEL = 1
        self.COINS = 0
        self.MAX_POKEMON = 250
        self.MAX_ITEMS = 350
        self.CLAIMED_LEVELS = []
        # uid -> ms it was removed. A GET_INVENTORY delta is ADDITIVE, so simply
        # dropping a Pokemon from CAUGHT does not delete it on the client -- the
        # client predicts the deletion locally and ROLLS IT BACK when the server
        # never confirms, which is why transferred Pokemon reappeared. We have to
        # keep reporting them as deleted for a while.
        self.DELETED = {}
        # pokemon_id -> [times_encountered, times_captured]. Without this the
        # Pokedex screen is simply empty, however much you catch.
        self.POKEDEX = {}
        # Eggs live apart from CAUGHT so nothing else (battles, transfers, the
        # box count) has to learn what an egg is. Each is
        # {uid, target_km, start_km, incubator}.
        self.EGGS = []
        # One unlimited incubator, exactly like the real game gave you.
        self.INCUBATORS = [{"id": "incubator-unlimited", "item": 901,
                            "uses": -1, "egg": 0, "start_km": 0.0,
                            "target_km": 0.0}]
        self.HATCHED = []        # hatched, not yet reported to the client
        # The in-game Journal: newest last, {"t": ms, "kind": "catch"|"fort", ...}.
        self.ACTION_LOG = []
        # "Start date" on the profile and PlayerData.creation_timestamp_ms. Set once
        # when the trainer is created and saved, so it never moves again.
        self.CREATED_MS = int(time.time() * 1000)
        self.LAST_POS = None     # (lat, lng) for the walked-distance tally
        # Where this trainer last was, as [lat, lng, ms] -- SAVED, unlike
        # PLAYER_LOC, which is in memory and empty until they open the game
        # again after a restart. The desktop radar (radarsite.py) sweeps from
        # here, so it still works while the phone is in a pocket.
        self.LAST_SEEN = None
        self.TEAM = 0            # 0 = not chosen yet; set in game at level 5
        # Onboarding: which TutorialCompletion steps this trainer has finished.
        # DEFAULT COMPLETE so every account skips onboarding unless it is a brand
        # new one AND progression.run_tutorial is on (set in use()). An existing
        # save with no "tutorial" key is also treated as complete (see load_from),
        # so turning the tutorial on never drags a current player back through it.
        self.TUTORIAL = list(_TUTORIAL_COMPLETE)
        # The codename the trainer picked in the NAME_SELECTION step. Shown in game
        # (PlayerData.username) in preference to the login name when set.
        self.CODENAME = ""
        # Whether this trainer has used their FREE "make a PokeStop here" yet. The
        # first one costs nothing; after that each costs coins (see buy_stop).
        self.FREE_STOP_USED = False
        # ms of the last time the Shop's defender bonus (coins+stardust for gyms
        # you're holding) was collected -- gates the once-a-day cooldown.
        self.LAST_DEFENDER_BONUS = 0
        # Wild Pokemon this trainer has caught or that fled FROM THEM, so it
        # vanishes from THEIR map only -- other phones still see the shared spawn.
        # {encounter_id: expiry_ms}, transient (not saved; re-rolls with the world).
        self.DESPAWNED = {}
        # Daily catch/spin streaks: {"catch_day": "2026-09-15", "catch_days": 3, ...}.
        # A day with at least one catch (or spin) keeps that streak alive; missing a day
        # starts it over, exactly like the real game's daily bonus.
        self.STREAK = {"catch_day": "", "catch_days": 0, "spin_day": "", "spin_days": 0}
        self.BERRIES = {}        # encounter_id -> capture multiplier in effect
        self.PW = ""             # "salt$hash"; empty until the account is claimed
        # A one-phone account (beta testers): the first launcher that signs in
        # becomes DEVICE (a hash of its X-WS-Device id) and no other phone gets in.
        self.LOCK = False
        self.DEVICE = ""
        # anticheat.py: {"since", "last", "how", "n"} once marked as a spoofer; {} = not.
        self.SPOOF = {}
        self.APPLIED = []        # active Lucky Egg / Incense: {item, applied_ms, expires_ms}
        self.SHINY_CHARM = False # bought once in the shop; boosts the shiny rate forever
        # Everything the Medals screen is scored on. The client draws each medal
        # from the badge list we return in GET_PLAYER_PROFILE, and that list is
        # computed from these counters against the game master's rank targets --
        # so a counter that is never bumped is a medal that never moves.
        self.STATS = {"pokemons_encountered": 0, "pokemons_captured": 0,
                      "poke_stop_visits": 0, "pokeballs_thrown": 0,
                      "unique_pokedex_entries": 0, "km_walked": 0.0,
                      "evolutions": 0, "eggs_hatched": 0,
                      "big_magikarp": 0, "small_rattata": 0, "pikachu_caught": 0,
                      "battle_attack_won": 0, "battle_attack_total": 0,
                      "battle_training_won": 0, "battle_training_total": 0,
                      "battle_defended_won": 0, "pokemon_deployed": 0,
                      # Windstock's own medals (protocol.CUSTOM_BADGES)
                      "shiny_caught": 0, "night_caught": 0}
        # HoloPokemonType -> how many of that type you've caught (a Pokemon with
        # two types counts for both, as it did in 2016).
        self.CAUGHT_BY_TYPE = {}
        # badge_type -> highest rank we've already shown the award popup for.
        self.BADGES = {}
        # Pokemon numbers ever caught shiny (the Shinydex medal). Kept apart from
        # CAUGHT so transferring a shiny doesn't take it off the medal.
        self.SHINYDEX = set()
        # Look: {avatar, skin, hair, shirt, pants, hat, shoes, eyes, backpack}.
        # Empty = use the defaults from settings.json.
        self.AVATAR = {}
        # "Let me pick in the game": holds AVATAR_SELECTION back from the
        # tutorial list so the client runs its own dress-up screen next launch.
        self.AVATAR_ASK = False

    def snapshot(self):
        return {"username": self.username,
                "bag": {str(k): v for k, v in self.BAG.items()},
                "caught": self.CAUGHT,
                "candy": {str(k): v for k, v in self.CANDY.items()},
                "stardust": self.STARDUST, "xp": self.XP, "level": self.LEVEL,
                "coins": self.COINS, "stats": self.STATS,
                "claimed_levels": self.CLAIMED_LEVELS,
                "deleted": {str(k): v for k, v in self.DELETED.items()},
                "pokedex": {str(k): list(v) for k, v in self.POKEDEX.items()},
                "team": self.TEAM, "pw": self.PW, "applied": self.APPLIED,
                "lock": self.LOCK, "device": self.DEVICE,
                "spoof": self.SPOOF,
                "shiny_charm": self.SHINY_CHARM,
                "tutorial": list(self.TUTORIAL), "codename": self.CODENAME,
                "free_stop_used": self.FREE_STOP_USED,
                "last_seen": list(self.LAST_SEEN) if self.LAST_SEEN else None,
                "last_defender_bonus": self.LAST_DEFENDER_BONUS,
                "streak": self.STREAK,
                "eggs": self.EGGS, "incubators": self.INCUBATORS,
                "hatched": self.HATCHED, "action_log": self.ACTION_LOG,
                "created_ms": self.CREATED_MS,
                "caught_by_type": {str(k): v for k, v in self.CAUGHT_BY_TYPE.items()},
                "badges": {str(k): v for k, v in self.BADGES.items()},
                "shinydex": sorted(self.SHINYDEX),
                "avatar": self.AVATAR, "avatar_ask": self.AVATAR_ASK,
                "max_pokemon": self.MAX_POKEMON, "max_items": self.MAX_ITEMS}

    def save(self):
        """Persist soon. One catch or spin used to rewrite the whole save several
        times over, synchronously; now changes are batched and written by the
        save thread within SAVE_DELAY seconds (flush_saves() on shutdown). A
        trainer with no file yet is written at once, so "does this account
        exist" checks on disk stay true from the first moment."""
        if self.gone:
            return                         # wiped (wipe_account): never write it back
        if not SAVE_DELAY or not os.path.exists(self.file):
            self.save_now()
            return
        with _dirty_lock:
            _dirty.add(self)
        _start_saver()

    def save_now(self):
        with _lock:                        # a consistent picture, not one mid-change
            if self.gone:
                return
            text = json.dumps(self.snapshot(), indent=1)
        with _write_lock:
            try:
                os.makedirs(SAVES_DIR, exist_ok=True)
                tmp = self.file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, self.file)  # atomic; never a half-written save
            except OSError:
                pass                       # a failed save must never break play

    def load_from(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return False
        self.BAG = {}
        for k, v in (d.get("bag") or {}).items():
            try:
                self.BAG[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        if not self.BAG:
            self.BAG = dict(_STARTING_BAG)
        self.CAUGHT = [c for c in (d.get("caught") or []) if isinstance(c, dict)]
        # Repair saves written while ids came from the spawn point: several
        # Pokemon could share one uid, and the client (which keys by uid) then
        # showed only one of them. Give the collisions fresh ids.
        seen = set()
        for c in self.CAUGHT:
            u = int(c.get("uid", 0) or 0)
            if not u or u in seen:
                u = _fresh_uid(seen)
                c["uid"] = u
            seen.add(u)
        self.CANDY = {}
        for k, v in (d.get("candy") or {}).items():
            try:
                self.CANDY[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        self.DELETED = {}
        for k, v in (d.get("deleted") or {}).items():
            try:
                self.DELETED[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        self.POKEDEX = {}
        for k, v in (d.get("pokedex") or {}).items():
            try:
                self.POKEDEX[int(k)] = [int(v[0]), int(v[1])]
            except (TypeError, ValueError, IndexError):
                pass
        # Backfill from the collection so an existing save doesn't show an empty
        # Pokedex for Pokemon that were caught before it was recorded.
        for c in self.CAUGHT:
            pid = int(c.get("pokemon_id", 0) or 0)
            if pid and pid not in self.POKEDEX:
                self.POKEDEX[pid] = [1, 1]
        self.TEAM = int(d.get("team", 0) or 0)
        # Old saves (written before onboarding existed) have no "tutorial" key --
        # treat them as fully done so a current player is never sent through it.
        _seen = d.get("last_seen")
        if isinstance(_seen, (list, tuple)) and len(_seen) >= 2:
            try:
                self.LAST_SEEN = [float(_seen[0]), float(_seen[1]),
                                  int(_seen[2]) if len(_seen) > 2 else 0]
            except (TypeError, ValueError):
                self.LAST_SEEN = None
        self.TUTORIAL = sorted({int(x) for x in d.get("tutorial", _TUTORIAL_COMPLETE)
                                if str(x).lstrip("-").isdigit()})
        self.CODENAME = str(d.get("codename", "") or "")
        self.FREE_STOP_USED = bool(d.get("free_stop_used", False))
        self.LAST_DEFENDER_BONUS = int(d.get("last_defender_bonus", 0) or 0)
        self.PW = str(d.get("pw", "") or "")
        self.LOCK = bool(d.get("lock", False))
        self.DEVICE = str(d.get("device", "") or "")
        self.SPOOF = d.get("spoof") if isinstance(d.get("spoof"), dict) else {}
        self.APPLIED = [a for a in (d.get("applied") or []) if isinstance(a, dict)]
        self.SHINY_CHARM = False
        self.EGGS = [e for e in (d.get("eggs") or []) if isinstance(e, dict)]
        inc = [i for i in (d.get("incubators") or []) if isinstance(i, dict)]
        if inc:
            self.INCUBATORS = inc
        # Incubators used to land in the BAG as plain items (shop 902s, level-up
        # 901s), where the client can't use them. Turn them into real ones. The
        # 901s were level rewards meant to be basic, so they become basic too --
        # every trainer keeps exactly one unlimited incubator.
        loose = sum(int(self.BAG.pop(k, 0) or 0) for k in INCUBATOR_ITEMS)
        if loose > 0:
            _new_incubators(self, 902, loose)
        self.HATCHED = [h for h in (d.get("hatched") or []) if isinstance(h, dict)]
        self.ACTION_LOG = [a for a in (d.get("action_log") or [])
                           if isinstance(a, dict)][-ACTION_LOG_MAX:]
        # Saves from before created_ms existed: the oldest thing we can date is
        # the earliest caught Pokemon / Journal entry -- a far better "trainer
        # since" than now. It's written back on the next save and then stays put.
        try:
            created = int(d.get("created_ms") or 0)
        except (TypeError, ValueError):
            created = 0
        if created <= 0:
            stamps = [int(c.get("caught_ms") or 0) for c in (d.get("caught") or [])
                      if isinstance(c, dict)]
            stamps += [int(a.get("t") or 0) for a in self.ACTION_LOG]
            stamps = [s for s in stamps if s > 1_400_000_000_000]   # sane ms only
            created = min(stamps) if stamps else self.CREATED_MS
        self.CREATED_MS = created
        self.STARDUST = int(d.get("stardust", self.STARDUST) or self.STARDUST)
        self.XP = int(d.get("xp", 0) or 0)
        self.COINS = int(d.get("coins", 0) or 0)
        self.MAX_POKEMON = int(d.get("max_pokemon", 250) or 250)
        self.MAX_ITEMS = int(d.get("max_items", 350) or 350)
        for k, v in (d.get("stats") or {}).items():
            if k in self.STATS:
                self.STATS[k] = v
        for k, v in (d.get("caught_by_type") or {}).items():
            try:
                self.CAUGHT_BY_TYPE[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        for k, v in (d.get("badges") or {}).items():
            try:
                self.BADGES[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        for n in d.get("shinydex") or []:
            try:
                self.SHINYDEX.add(int(n))
            except (TypeError, ValueError):
                pass
        # Shinies caught before the shiny medals existed still count: backfill the
        # total and the Shinydex from the ones still in the collection.
        if "shinydex" not in d:
            owned = [c for c in self.CAUGHT if c.get("shiny")]
            self.SHINYDEX.update(int(c.get("pokemon_id", 0) or 0) for c in owned)
            self.SHINYDEX.discard(0)
            self.STATS["shiny_caught"] = max(self.STATS["shiny_caught"], len(owned))
        # Backfill the type tally from the collection, so a save made before
        # medals existed doesn't show every type medal at zero.
        if not self.CAUGHT_BY_TYPE:
            import protocol
            for c in self.CAUGHT:
                for t in protocol.pokemon_types(int(c.get("pokemon_id", 0) or 0)):
                    self.CAUGHT_BY_TYPE[t] = self.CAUGHT_BY_TYPE.get(t, 0) + 1
        self.AVATAR_ASK = bool(d.get("avatar_ask"))
        av = d.get("avatar")
        if isinstance(av, dict):
            self.AVATAR = {str(k): int(v) for k, v in av.items()
                           if str(v).lstrip("-").isdigit()}
        st = d.get("streak")
        if isinstance(st, dict):
            self.STREAK.update({k: st.get(k, self.STREAK[k]) for k in self.STREAK})
        self.CLAIMED_LEVELS = [int(x) for x in (d.get("claimed_levels") or [])
                               if str(x).lstrip("-").isdigit()]
        self.LEVEL = level_for_xp(self.XP)
        if not self.CLAIMED_LEVELS:      # pre-existing save: don't replay old popups
            self.CLAIMED_LEVELS = list(range(1, self.LEVEL + 1))
        # The Shiny Charm was removed (2026-10-05): take it back and refund it, once.
        if d.get("shiny_charm") or self.BAG.get(SHINY_CHARM_ITEM, 0) > 0:
            self.BAG.pop(SHINY_CHARM_ITEM, None)
            self.COINS += SHINY_CHARM_REFUND
            self._refunded_charm = True
            print(f"[shop] {self.username}: Shiny Charm removed, +{SHINY_CHARM_REFUND} coins back", flush=True)
        return True


_uid_rng = _random.Random()


def _fresh_uid(used):
    """An unused 63-bit Pokemon id."""
    while True:
        u = _uid_rng.getrandbits(62) | 1
        if u not in used:
            return u


def new_uid(seed=0):
    """A UNIQUE id for a newly caught Pokemon.

    This used to be `encounter_id ^ 0xC0FFEE`, which is derived from the spawn
    point and therefore repeats: catching at the same place twice produced the
    SAME id. The client keys Pokemon by id, so the second catch silently replaced
    the first instead of showing up, and transferring one removed only one of the
    duplicates while the rest kept it on screen.
    """
    p = current()
    # The trainer goes into the id too: two trainers catching the same shared
    # spawn used to get the SAME id, and one deploying it to a gym made the other's
    # copy read as deployed (and stuck) as well.
    who = int.from_bytes(hashlib.sha1(p.username.encode("utf-8")).digest()[:4], "big")
    with _lock:
        used = {int(c.get("uid", 0)) for c in p.CAUGHT} | set(p.DELETED)
        base = (int(seed) ^ 0xC0FFEE ^ (who << 28)) & 0x3FFFFFFFFFFFFFFF
        return base if base and base not in used else _fresh_uid(used)


# (username, encounter_id) -> the uid that encounter's Pokemon will keep. Chosen
# ONCE, at the encounter, so the catch reply names the same id the catch screen
# showed; otherwise a taken id got replaced at catch time and the post-catch
# summary never opened.
ENCOUNTER_UIDS = {}


def encounter_uid(encounter_id, forget=False):
    key = (current().username, int(encounter_id))
    with _lock:
        uid = ENCOUNTER_UIDS.get(key)
        if uid is None:
            uid = new_uid(encounter_id)
            if len(ENCOUNTER_UIDS) > 5000:
                ENCOUNTER_UIDS.clear()
            ENCOUNTER_UIDS[key] = uid
        if forget:
            ENCOUNTER_UIDS.pop(key, None)
        return uid


def use(username):
    """Make `username` the account for this request (called by rpc.py)."""
    name = username or "player"
    with _lock:
        p = _players.get(name)
        if p is None:
            # Saves are stored under a lowercased file name, so "Bracky68" and
            # "bracky68" are the SAME account. Without this they got two Player
            # objects over one file, and whichever saved last wiped the other's
            # progress -- exactly what happens when the World Manager gives an
            # item to a trainer who is playing right now.
            key = _safe_name(name)
            for existing, obj in _players.items():
                if _safe_name(existing) == key:
                    p = obj
                    break
        if p is not None and not os.path.exists(p.file):
            # The save was deleted while the server was running (a reset for
            # testing). Every save goes through os.replace, so the file never
            # vanishes by itself -- drop the in-memory copy, or its next save()
            # would quietly write all the old progress straight back.
            p.gone = True                  # nor may a pending batched save
            _players.pop(name, None)
            p = None
        if p is None:
            p = Player(name)
            path = os.path.join(SAVES_DIR, _safe_name(name) + ".json")
            if os.path.exists(path):
                p.load_from(path)
                if getattr(p, "_refunded_charm", False):
                    p.save()                # the charm refund, written so it happens once
            else:
                # No save for this account yet. If the old single-player save.json
                # is still around and nobody has claimed it, adopt it so an
                # existing collection carries over instead of starting from zero.
                have_any = os.path.isdir(SAVES_DIR) and any(
                    f.endswith(".json") for f in os.listdir(SAVES_DIR))
                if not have_any and os.path.exists(SAVE_FILE) and p.load_from(SAVE_FILE):
                    p.username = name
                else:
                    # A genuinely new trainer: run onboarding if it's switched on.
                    # (Adopted legacy saves keep their complete state above.)
                    try:
                        if _cfg.get("progression", "run_tutorial", cast=bool):
                            # Optionally pre-complete POKEMON_CAPTURE (step 3) so the
                            # slow scripted starter catch is skipped and the first
                            # catch is a normal fast wild encounter -- the rest of
                            # onboarding (legal/avatar/name/team) still runs.
                            if _cfg.get("progression", "tutorial_skip_starter", cast=bool):
                                p.TUTORIAL = [3]
                            else:
                                p.TUTORIAL = []
                    except Exception:
                        pass
                p.save()
            _players[name] = p
    _current.player = p
    return p


def wipe_account(name):
    """Delete trainer `name`'s save and research progress (a profile reset or
    deleted on the Windstock website). Anyone playing it right now keeps the
    in-memory copy only for the request in flight: it is dropped here and never
    saved again, so their next request starts from nothing. True when there
    was something to delete."""
    key = _safe_name(name)
    found = False
    with _lock:
        for n in [n for n in _players if _safe_name(n) == key]:
            _players.pop(n).gone = True
            found = True
        try:
            os.remove(os.path.join(SAVES_DIR, key + ".json"))
            found = True
        except OSError:
            pass
    try:
        import research
        want = research._safe(name).lower()
        for fn in os.listdir(research.PROGRESS_DIR):
            if fn.endswith(".json") and fn[:-5].lower() == want:
                os.remove(os.path.join(research.PROGRESS_DIR, fn))
    except (OSError, ImportError, AttributeError):
        pass
    return found


def current():
    p = getattr(_current, "player", None)
    if p is None:
        p = use("player")
    return p


def accounts():
    """Summary of every save on disk, for the World Manager."""
    out = []
    try:
        for fn in sorted(os.listdir(SAVES_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SAVES_DIR, fn), encoding="utf-8") as fh:
                    d = json.load(fh)
                out.append({"username": d.get("username", fn[:-5]),
                            "level": level_for_xp(int(d.get("xp", 0) or 0)),
                            "xp": int(d.get("xp", 0) or 0),
                            "caught": len(d.get("caught") or []),
                            "coins": int(d.get("coins", 0) or 0)})
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


# module-level names (world.BAG, world.CANDY, ...) forward to the current player
_FORWARD = {"BAG", "CAUGHT", "CANDY", "STARDUST", "XP", "LEVEL", "COINS", "DELETED", "POKEDEX",
            "EGGS", "INCUBATORS", "HATCHED", "TEAM", "BERRIES", "APPLIED",
            "MAX_POKEMON", "MAX_ITEMS", "CLAIMED_LEVELS", "STATS", "SHINY_CHARM",
            "CAUGHT_BY_TYPE", "BADGES", "SHINYDEX", "AVATAR", "AVATAR_ASK"}


def __getattr__(name):
    if name in _FORWARD:
        return getattr(current(), name)
    raise AttributeError(name)


def save():
    current().save()


# ================================================= shared world (all accounts)
GYMS = {}                          # fort_id -> [{uid, pokemon_id, cp, trainer, team}]
# 2016 gym prestige -> level (1..10). A gym holds `level` defenders; attacking an
# enemy gym drains its prestige (dropping levels and ejecting the lowest defender,
# neutral at 0), training a friendly one raises it. fort_id -> prestige points.
# Shared across accounts, saved alongside GYMS in gyms.json.
PRESTIGE = {}
GYM_LEVELS = (0, 2000, 4000, 8000, 12000, 16000, 20000, 30000, 40000, 50000)
GYM_PRESTIGE_CAP = 52000
BATTLES = {}                       # battle_id -> live battle state
SPAWNS = {}                        # transient
DESPAWNED = {}                     # encounter_id -> expiry_ms
# Lures are attached to a FORT and are visible to everyone, so they live in the
# shared world rather than on one player. fort_id -> {item, expires_ms, by}
FORT_MODIFIERS = {}
# Spun stops are PER PLAYER: (username, fort_id) -> cooldown_complete_ms. The map
# has to keep reporting the cooldown, or the next map refresh repaints the stop blue.
SPIN_COOLDOWNS = {}


def set_spin_cooldown(fort_id, until_ms):
    with _lock:
        SPIN_COOLDOWNS[(current().username, fort_id)] = int(until_ms)


def spin_cooldown(fort_id):
    now = int(time.time() * 1000)
    with _lock:
        key = (current().username, fort_id)
        until = SPIN_COOLDOWNS.get(key, 0)
        if until and until <= now:
            SPIN_COOLDOWNS.pop(key, None)
            return 0
        return until
# "Raid" mode: one boss standing in EVERY gym, shared by all accounts. 0.29 has
# no raid support at all, so this fakes it with the pieces the client does have --
# a gym defender under the trainer name "raid" that becomes a catchable wild
# Pokemon at your feet the moment you knock it out.
RAID = {"on": False, "pokemon_id": 150, "cp": 3000, "trainer": "raid"}
RAID_FILE = os.path.join(HERE, "raid.json")
BONUS_SPAWNS = {}                  # username -> [ {eid,pid,cp,lat,lng,expires_ms} ]
# Multiplayer raids: ONE shared HP pool per gym's boss, hit by every trainer.
# fort_id -> {key, hp, max, down_until, damage: {username: total}}
RAID_BOSSES = {}
PLAYER_LOC = {}                    # username -> (lat, lng), last reported position
_MAX_SPAWNS = 4000


def save_gyms():
    try:
        tmp = GYMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"gyms": GYMS, "prestige": PRESTIGE,
                       "npc_cleared": NPC_CLEARED, "returns": RETURNS}, fh, indent=1)
        os.replace(tmp, GYMS_FILE)
    except OSError:
        pass


def _backfill_prestige():
    """A gym that has defenders but no prestige entry is one from before prestige
    existed -- give it enough to match its current defender count so it isn't
    instantly over capacity."""
    for fid, members in GYMS.items():
        if members and fid not in PRESTIGE:
            PRESTIGE[fid] = GYM_LEVELS[min(10, max(1, len(members))) - 1]


def load_gyms():
    try:
        with open(GYMS_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        GYMS.clear()
        GYMS.update({k: v for k, v in (d.get("gyms") or {}).items()
                     if isinstance(v, list)})
        PRESTIGE.clear()
        PRESTIGE.update({k: int(v) for k, v in (d.get("prestige") or {}).items()
                         if isinstance(v, (int, float))})
        NPC_CLEARED.clear()
        NPC_CLEARED.update({k: int(v) for k, v in (d.get("npc_cleared") or {}).items()
                            if isinstance(v, (int, float))})
        RETURNS.clear()
        RETURNS.update({k: [r for r in v if isinstance(r, list) and len(r) == 5]
                        for k, v in (d.get("returns") or {}).items()
                        if isinstance(v, list)})
        _backfill_prestige()
        return True
    except (OSError, ValueError):
        pass
    try:                       # migrate gyms out of the old single-player save
        with open(SAVE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        GYMS.update({k: v for k, v in (d.get("gyms") or {}).items()
                     if isinstance(v, list)})
        if GYMS:
            _backfill_prestige()
            save_gyms()
    except (OSError, ValueError):
        pass
    return False


# --- spawns ------------------------------------------------------------------
def remember_spawn(encounter_id, pokemon_id, lat, lng, cp, spawn_id, expires_ms):
    with _lock:
        if len(SPAWNS) >= _MAX_SPAWNS:
            for k in sorted(SPAWNS, key=lambda k: SPAWNS[k]["expires_ms"])[:_MAX_SPAWNS // 2]:
                SPAWNS.pop(k, None)
        SPAWNS[encounter_id] = {"pokemon_id": pokemon_id, "lat": lat, "lng": lng,
                                "cp": cp, "spawn_id": spawn_id, "expires_ms": expires_ms}


def get_spawn(encounter_id):
    with _lock:
        # If THIS trainer already caught it (or it fled from them) it's gone for
        # them, even though the shared spawn still exists for other phones.
        if is_despawned(encounter_id):
            return None
        s = SPAWNS.get(encounter_id)
        return dict(s) if s else None


def remove_spawn(encounter_id):
    # PER-ACCOUNT: a catch/flee removes the Pokemon for THIS trainer only. It used
    # to pop the shared SPAWNS dict, which made it vanish on every other phone too.
    mark_despawned(encounter_id, int(time.time() * 1000) + 20 * 60 * 1000)


def mark_despawned(encounter_id, until_ms):
    p = current()
    with _lock:
        p.DESPAWNED[encounter_id] = until_ms
        if len(p.DESPAWNED) > 5000:
            now = int(time.time() * 1000)
            for k in [k for k, v in p.DESPAWNED.items() if v < now]:
                p.DESPAWNED.pop(k, None)


def is_despawned(encounter_id):
    p = current()
    with _lock:
        exp = p.DESPAWNED.get(encounter_id)
        if exp is None:
            return False
        if exp < int(time.time() * 1000):
            p.DESPAWNED.pop(encounter_id, None)
            return False
        return True


def live_spawns(limit=2000):
    """The whole wild spawn table, minus anything already expired.

    No trainer, no range, no per-trainer filtering -- the World Manager radar
    wants to see what EXISTS. The player-facing radar uses spawns_near instead,
    which is scoped to one trainer and hides what they already took.
    """
    now = int(time.time() * 1000)
    with _lock:
        items = list(SPAWNS.items())
    out = []
    for eid, s in items:
        exp = int(s.get("expires_ms") or 0)
        if exp and exp < now:
            continue
        row = dict(s)
        row["eid"] = eid
        out.append(row)
    out.sort(key=lambda r: r.get("expires_ms") or 0)     # soonest to vanish first
    return out[:limit]


def spawns_near(lat, lng, metres=500.0, limit=60):
    """Live wild spawns within `metres` of a point, nearest first.

    For the Help Center radar. Anything already expired is skipped, and so is
    anything the CURRENT trainer has caught or let flee, so the radar lists what
    that trainer's phone would actually still see on the map.

    Each row is a copy -- the caller never gets a handle on the shared spawn.
    """
    if not (abs(lat) > 1e-6 or abs(lng) > 1e-6):
        return []
    now = int(time.time() * 1000)
    deg = metres / 111_000.0
    shrink = max(0.05, math.cos(math.radians(lat)))   # a degree of longitude is shorter up north
    out = []
    with _lock:
        items = list(SPAWNS.items())
    for eid, s in items:
        exp = int(s.get("expires_ms") or 0)
        if exp and exp < now:
            continue
        dlat, dlng = s["lat"] - lat, (s["lng"] - lng) * shrink
        if abs(dlat) > deg or abs(dlng) > deg:        # cheap box test before the hypot
            continue
        d = math.hypot(dlat, dlng) * 111_000.0
        if d > metres:
            continue
        if is_despawned(eid):
            continue
        row = dict(s)
        row["eid"] = eid
        row["distance_m"] = d
        out.append(row)
    out.sort(key=lambda r: r["distance_m"])
    return out[:limit]


# --- bag ---------------------------------------------------------------------
def created_ms():
    """When the current trainer started (ms) -- stored in the save, never moves."""
    return int(current().CREATED_MS)


ACTION_LOG_MAX = 100            # Journal entries kept per trainer


def log_action(entry):
    """Add one Journal entry ({"kind": "catch"|"fort", ...}) for the current
    trainer, stamped now; the oldest fall off past ACTION_LOG_MAX."""
    p = current()
    e = dict(entry)
    e.setdefault("t", int(time.time() * 1000))
    with _lock:
        p.ACTION_LOG.append(e)
        del p.ACTION_LOG[:-ACTION_LOG_MAX]
    p.save()


def action_log():
    with _lock:
        return [dict(a) for a in current().ACTION_LOG]


INCUBATOR_ITEMS = (901, 902)    # ITEM_INCUBATOR_BASIC_UNLIMITED, ITEM_INCUBATOR_BASIC
BASIC_INCUBATOR_USES = 3        # a 2016 basic incubator hatches three eggs


def _new_incubators(p, item_id, count):
    """Turn incubator items into real EggIncubator entries (caller holds _lock).
    The client can only put an egg into something on the egg_incubators list --
    a 902 sitting in the bag as an ordinary item is unusable."""
    base = int(time.time() * 1000)
    taken = {i["id"] for i in p.INCUBATORS}
    for k in range(int(count)):
        n = 0
        while f"incubator-{base}-{k}-{n}" in taken:
            n += 1
        iid = f"incubator-{base}-{k}-{n}"
        taken.add(iid)
        p.INCUBATORS.append({"id": iid, "item": int(item_id),
                             "uses": -1 if item_id == 901 else BASIC_INCUBATOR_USES,
                             "egg": 0, "start_km": 0.0, "target_km": 0.0})
    return sum(1 for i in p.INCUBATORS if i.get("item") == item_id)


def add_item(item_id, count):
    p = current()
    with _lock:
        if item_id in INCUBATOR_ITEMS:
            n = _new_incubators(p, item_id, count)
        else:
            p.BAG[item_id] = p.BAG.get(item_id, 0) + count
            n = p.BAG[item_id]
    p.save()
    return n


def take_item(item_id, count=1):
    p = current()
    with _lock:
        if p.BAG.get(item_id, 0) < count:
            return False
        p.BAG[item_id] -= count
    p.save()
    return True


def bag_items(include_empty=False):
    p = current()
    with _lock:
        return [(i, c) for i, c in sorted(p.BAG.items())
                if c > 0 or (include_empty and i not in INCUBATOR_ITEMS)]


def bag_count():
    with _lock:
        return sum(current().BAG.values())


# --- caught pokemon ----------------------------------------------------------
def add_caught(uid, pokemon_id, cp, **extra):
    p = current()
    with _lock:
        p.DELETED.pop(int(uid), None)     # never report a live Pokemon as deleted
        p.CAUGHT.append({"uid": uid, "pokemon_id": pokemon_id, "cp": cp,
                         "caught_ms": int(time.time() * 1000), **extra})
        p.STATS["pokemons_captured"] += 1
        p.STATS["pokeballs_thrown"] += 1
        p.STATS["unique_pokedex_entries"] = len({c["pokemon_id"] for c in p.CAUGHT})
        n = len(p.CAUGHT)
    p.save()
    return n


def caught():
    with _lock:
        return list(current().CAUGHT)


def get_caught(uid):
    with _lock:
        for c in current().CAUGHT:
            if c["uid"] == uid:
                return dict(c)
    return None


def update_caught(uid, **fields):
    p = current()
    with _lock:
        for c in p.CAUGHT:
            if c["uid"] == uid:
                c.update(fields)
                out = dict(c)
                break
        else:
            return None
    p.save()
    return out


def release(uid):
    if is_deployed(uid):
        return False, "deployed"
    p = current()
    with _lock:
        for i, c in enumerate(p.CAUGHT):
            if c["uid"] == uid:
                p.CAUGHT.pop(i)
                break
        else:
            return False, "not found"
        p.DELETED[int(uid)] = int(time.time() * 1000)
    p.save()
    return True, "ok"


def recent_deletions(max_age_ms=1800000):
    """Pokemon removed recently, so the inventory delta can keep confirming the
    deletion until the client has certainly seen it. Old entries are dropped so
    the list cannot grow without bound."""
    p = current()
    cutoff = int(time.time() * 1000) - max_age_ms
    with _lock:
        stale = [u for u, ts in p.DELETED.items() if ts < cutoff]
        for u in stale:
            p.DELETED.pop(u, None)
        return sorted(p.DELETED.items())


def stats():
    p = current()
    with _lock:
        return p.LEVEL, p.XP


def add_xp(n):
    p = current()
    n = int(int(n) * xp_multiplier() + 0.5)   # Lucky Egg / event bonus (1.5x rounds half up)
    with _lock:
        p.XP += n
        p.LEVEL = level_for_xp(p.XP)
    p.save()
    return p.XP


def bump(counter, n=1):
    p = current()
    with _lock:
        if counter in p.STATS:
            p.STATS[counter] += n
    p.save()


def bump_type(pokemon_id):
    """Count a catch towards the type medals (Bug Catcher, Fisherman, ...).
    Dual-type Pokemon count for both types."""
    import protocol
    p = current()
    with _lock:
        for t in protocol.pokemon_types(pokemon_id):
            p.CAUGHT_BY_TYPE[t] = p.CAUGHT_BY_TYPE.get(t, 0) + 1
    p.save()


def record_shiny_catch(pokemon_id):
    """A shiny was caught: Shiny Hunter +1, and its species joins the Shinydex."""
    p = current()
    with _lock:
        p.STATS["shiny_caught"] += 1
        p.SHINYDEX.add(int(pokemon_id))
    p.save()


def shinydex_count():
    with _lock:
        return len(current().SHINYDEX)


def caught_by_type():
    with _lock:
        return dict(current().CAUGHT_BY_TYPE)


def badge_rank(badge_type):
    """Highest rank the player has already been shown the popup for."""
    with _lock:
        return int(current().BADGES.get(int(badge_type), 0))


def claim_badge(badge_type, rank):
    p = current()
    with _lock:
        p.BADGES[int(badge_type)] = int(rank)
    p.save()


def avatar():
    with _lock:
        return dict(current().AVATAR)


def owner_avatar(owner):
    """Another trainer's saved look (by login name): live if they are loaded, else
    read from their save. {} = never customised (the default look)."""
    if not owner:
        return {}
    with _lock:
        p = _players.get(owner)
        if p is not None:
            return dict(p.AVATAR)
    try:
        path = os.path.join(SAVES_DIR, _safe_name(owner) + ".json")
        with open(path, encoding="utf-8") as fh:
            av = json.load(fh).get("avatar")
        return {str(k): int(v) for k, v in av.items()
                if str(v).lstrip("-").isdigit()} if isinstance(av, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def avatar_ask(on=None):
    """Whether to let the client's own dress-up screen run next launch."""
    p = current()
    with _lock:
        if on is not None:
            p.AVATAR_ASK = bool(on)
    if on is not None:
        p.save()
    return p.AVATAR_ASK


def set_avatar(look):
    """Remember the look the client sent from the customisation screen."""
    p = current()
    clean = {}
    for k, v in (look or {}).items():
        try:
            clean[str(k)] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    with _lock:
        p.AVATAR.update(clean)
        p.AVATAR_ASK = False           # they've chosen; stop asking
    p.save()
    return dict(p.AVATAR)


def add_coins(n):
    p = current()
    with _lock:
        p.COINS = max(0, p.COINS + n)
    p.save()
    return p.COINS


def level_claimed(level):
    with _lock:
        return int(level) in current().CLAIMED_LEVELS


def claim_level(level):
    """Mark `level` paid. Returns True only for the call that actually claimed
    it -- the client fires LEVEL_UP_REWARDS twice at once on a mid-play level-up,
    so check-then-claim must be one step or both requests pay out."""
    p = current()
    with _lock:
        if int(level) in p.CLAIMED_LEVELS:
            return False
        p.CLAIMED_LEVELS.append(int(level))
    p.save()
    return True


# --- candy / stardust --------------------------------------------------------
def add_candy(family, n):
    p = current()
    with _lock:
        p.CANDY[family] = p.CANDY.get(family, 0) + n
        out = p.CANDY[family]
    p.save()
    return out


PW_ITERATIONS = 200_000


def _hash_pw(password, salt=None, iterations=PW_ITERATIONS):
    """PBKDF2 -- passwords are never stored in the clear, not even on a server
    that only your family can reach. Stored as "pbkdf2$<iterations>$<salt>$<hash>"."""
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                            salt.encode("ascii"), iterations).hex()
    return f"pbkdf2${iterations}${salt}${h}"


def _pw_matches(stored, password):
    """(matches, needs_rehash). Understands the older "salt$hash" (60,000
    rounds) so nobody has to reset; those are upgraded on the next good login."""
    try:
        if stored.startswith("pbkdf2$"):
            _, iters, salt, _h = stored.split("$")
            iters = int(iters)
            return (hmac.compare_digest(stored, _hash_pw(password, salt, iters)),
                    iters < PW_ITERATIONS)
        salt, want = stored.split("$", 1)
        got = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                  salt.encode("ascii"), 60000).hex()
        return hmac.compare_digest(want, got), True
    except (ValueError, UnicodeError):
        return False, False


MIN_PASSWORD = 6            # for accounts made over the network
MAX_PASSWORD = 256


def _name_ok(name):
    """A NEW trainer name: letters, digits, spaces and . _ - @ + (an email
    works). Keeps markup and control characters out of every page and log that
    shows a name."""
    return (0 < len(name) <= 48 and any(c.isalnum() for c in name)
            and all(c.isalnum() or c in " ._-@+" for c in name))


def _server_full():
    """Has the server reached settings.server.max_accounts? Every account is a
    save file and signing up is free, so this is what bounds the disk."""
    try:
        cap = int(_cfg.get("server", "max_accounts"))
        if cap <= 0:
            return False
        return sum(1 for f in os.listdir(SAVES_DIR) if f.endswith(".json")) >= cap
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _invite_only(ip):
    """settings.server.open_signups is off (or OPEN_SIGNUPS=0) and this request
    came over the network: no new trainers, and no claiming a save that never
    had a password -- only accounts the operator made get in."""
    import auth
    if auth.is_local(ip):
        return False
    try:
        return not _cfg.get("server", "open_signups", env="OPEN_SIGNUPS",
                            cast=lambda v: str(v).strip().lower()
                            not in ("0", "false", "no", "off", "closed"))
    except Exception:
        return False


def login_message(why, taken=False):
    """What to tell the person, for check_login()'s reason."""
    return {
        "wrong password": ("That trainer name is already taken and the password "
                           "doesn't match." if taken else "Wrong password."),
        "too many attempts": "Too many attempts. Wait a few minutes and try again.",
        "bad name": "Use letters and numbers for a new trainer name.",
        "weak password": f"Pick a password of at least {MIN_PASSWORD} characters.",
        "too many accounts": "Too many new trainers from this network today.",
        "server full": "This server is not taking new trainers right now.",
        "no username": "Enter your trainer name.",
        "email": "Email sign-in is only for the Windstock server. Use a trainer "
                 "name and password here.",
        "invite only": "This server is invite-only right now. Ask the host for "
                       "a tester account.",
        "other phone": "This trainer is locked to another phone.",
        "no phone": "This trainer can only sign in from the Windstock launcher "
                    "(Windstock Online).",
        "not registered": "You're not registered yet. Tap Register to sign up" + _register_hint()
                          + ", pick a game username on your profile there, then sign in here with it.",
        "site down": "Can't check your login right now. Try again in a minute.",
        "banned": "This trainer has been banned from Windstock.",
    }.get(why, "Unable to log in with the details provided.")


def _register_hint():
    import siteaccounts
    return siteaccounts.register_hint()


def _site_login(email, password, ip):
    """check_login() for a server whose logins live on the Windstock website
    (siteaccounts.py): the website checks the email and password and names the
    save. The save carries no password of its own."""
    import auth
    import siteaccounts
    handle, why = siteaccounts.check(email, password, ip)
    if not handle:
        if why == "wrong password":
            auth.login_failed(email, ip)
        return False, why, None
    path = os.path.join(SAVES_DIR, _safe_name(handle) + ".json")
    new = not os.path.exists(path)
    if new and _server_full():
        return False, "server full", None
    prev = getattr(_current, "player", None)
    try:
        p = use(handle)
        if p.PW != siteaccounts.PW_MARK:
            p.PW = siteaccounts.PW_MARK
            p.save()
        why = _phone_gate(p)
        if why:
            return False, why, handle
        return True, "claimed" if new else "ok", handle
    finally:
        _current.player = prev


def check_login(username, password):
    """(ok, reason, name). The FIRST login for a name claims it and sets the
    password; after that the password has to match. An unknown name is claimed
    rather than refused, so a new trainer can just sign in and start playing.

    Where the Windstock website holds the logins (siteaccounts.py, the public
    server), an email -- or the game username a profile picked there -- is
    checked there instead and nothing new can be claimed here. Trainers made
    before that keep signing in with their name and password.

    Guessing is throttled per address and per name, and claiming a new name
    over the network needs a real password and is rationed per address (auth.py)."""
    import auth
    name = (username or "").strip()
    password = password or ""
    if not name:
        return False, "no username", None
    if len(name) > 64 or len(password) > MAX_PASSWORD:
        return False, "bad name" if len(name) > 64 else "wrong password", None
    ip = auth.client_ip()
    if auth.login_blocked(name, ip):
        return False, "too many attempts", None
    import siteaccounts
    # A server hosted on a PC (localhost): no emails and no sign-up rules --
    # type a trainer name and password and that is the account, made on first
    # use. Emails are Windstock website accounts. WS_SERVER_DIR = the
    # launcher's built-in server on a phone, which keeps its own rules.
    pc = not siteaccounts.logins() and not os.environ.get("WS_SERVER_DIR")
    if pc and "@" in name:
        return False, "email", None
    site = siteaccounts.logins() and not auth.is_local(ip)
    if site and "@" in name:
        return _site_login(name, password, ip)
    known = next((n for n in account_names() if n.lower() == name.lower()), None)
    real = known or name
    weak = not password or (len(password) < MIN_PASSWORD and not auth.is_local(ip))
    if known is None and not os.path.exists(
            os.path.join(SAVES_DIR, _safe_name(name) + ".json")):
        # Nothing on disk under this name: this login would CREATE an account.
        # Refuse before use() so a turned-away name leaves nothing behind.
        if site:
            # sign-ups are on the website; there a profile picks a game username
            # (letters and numbers -- the game's login box won't type "@" or ".")
            return _site_login(name, password, ip)
        if not _name_ok(name):
            return False, "bad name", None
        if not pc:
            if _invite_only(ip):
                return False, "invite only", None
            if weak:
                return False, "weak password", None
            if not auth.claim_allowed(ip):
                return False, "too many accounts", None
            if _server_full():
                return False, "server full", None
    prev = getattr(_current, "player", None)
    try:
        p = use(real)
        if not p.PW:
            if site:
                return False, "not registered", real
            if not pc and _invite_only(ip):
                return False, "invite only", real
            if not pc and weak:
                return False, "weak password", real
            p.PW = _hash_pw(password)
            p.save()
            if not pc:
                auth.claimed(ip)
            return True, "claimed", real
        ok, stale = _pw_matches(p.PW, password)
        if ok:
            why = _phone_gate(p)
            if why:
                return False, why, real
            if stale:
                p.PW = _hash_pw(password)
                p.save()
            return True, "ok", real
        auth.login_failed(name, ip)
        return False, "wrong password", real
    finally:
        _current.player = prev


def _this_phone():
    """The launcher device id on the request being handled (set by server.py)."""
    import sys
    rpc = sys.modules.get("rpc")
    return getattr(getattr(rpc, "_req_dev", None), "value", None) if rpc else None


def _phone_gate(p):
    """None if this request may use account `p`, else why not. A one-phone
    account takes the first launcher that shows up with the right login and
    refuses every other one -- so handing the password on gets nobody in."""
    if not p.LOCK:
        return None
    dev = _this_phone()
    if not dev:
        return "no phone"
    mark = hashlib.sha256(dev.encode("ascii")).hexdigest()
    with _lock:
        if not p.DEVICE:
            p.DEVICE = mark
            bound = True
        else:
            bound = False
            if not hmac.compare_digest(p.DEVICE, mark):
                return "other phone"
    if bound:
        p.save()
    return None


def phone_refused(username):
    """rpc.py, on every game request: is this signed-in trainer a one-phone
    account being played from a phone that isn't the one? (A token copied off
    the right phone is no better than the password.)"""
    with _lock:
        p = _players.get(username)
    if p is None:
        prev = getattr(_current, "player", None)
        try:
            p = use(username)
        finally:
            _current.player = prev
    return _phone_gate(p)


def beta_password():
    """A password to hand a tester: 12 characters nobody mixes up, in groups."""
    import secrets
    abc = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(abc) for _ in range(4)) for _ in range(3))


def beta_account(username, action):
    """World Manager: one-phone (beta tester) accounts. (ok, message).

      new    -- create the trainer with a fresh password, locked to the first phone
      lock   -- make an existing trainer one-phone (the next sign-in picks the phone)
      forget -- keep it one-phone but let the next sign-in pick a new phone
      unlock -- back to an ordinary account
    """
    name = (username or "").strip()
    real = next((n for n in account_names() if n.lower() == name.lower()), None)
    prev = getattr(_current, "player", None)
    try:
        if action == "new":
            if real or os.path.exists(os.path.join(SAVES_DIR, _safe_name(name) + ".json")):
                return False, f"{real or name!r} already exists -- use Lock to one phone"
            if not _name_ok(name):
                return False, login_message("bad name")
            if _server_full():
                return False, login_message("server full")
            pw = beta_password()
            p = use(name)
            p.PW, p.LOCK, p.DEVICE = _hash_pw(pw), True, ""
            p.save()
            return True, (f"Beta trainer {name!r} created. Password: {pw}  -- shown "
                          f"once. It locks to the first phone that signs in.")
        if not real:
            return False, f"no account called {name!r}"
        p = use(real)
        if action == "lock":
            p.LOCK, p.DEVICE = True, ""
            msg = f"{real} is now a one-phone account; the next sign-in picks the phone"
        elif action == "forget":
            if not p.LOCK:
                return False, f"{real} is not a one-phone account"
            p.DEVICE = ""
            msg = f"{real}'s phone forgotten; the next sign-in picks the new one"
        elif action == "unlock":
            p.LOCK, p.DEVICE = False, ""
            msg = f"{real} can sign in from any phone again"
        else:
            return False, "unknown action"
        p.save()
        return True, msg
    finally:
        _current.player = prev


def set_password(username, password):
    """Used by the World Manager to reset a forgotten password."""
    real = next((n for n in account_names() if n.lower() == (username or "").lower()),
                None)
    if not real:
        return False
    prev = getattr(_current, "player", None)
    try:
        p = use(real)
        p.PW = _hash_pw(password)
        p.save()
        return True
    finally:
        _current.player = prev


def has_password(username):
    real = next((n for n in account_names() if n.lower() == (username or "").lower()),
                None)
    if not real:
        return False
    prev = getattr(_current, "player", None)
    try:
        return bool(use(real).PW)
    finally:
        _current.player = prev


def account_names():
    """Every account we know of, once each, spelled the way it is played.

    Loaded players are keyed by the name the client sends ("Bracky68"); saves
    are files named by _safe_name(), which lowercases ("bracky68"). Both used to
    come back, so the World Manager's account list showed every trainer twice
    and its error messages listed both spellings.
    """
    best = {}                      # _safe_name -> the nicest spelling we have
    with _lock:
        for name in _players:
            best[_safe_name(name)] = name
    try:
        for fn in os.listdir(SAVES_DIR):
            if fn.endswith(".json") and not fn.startswith("_"):
                stem = fn[:-len(".json")]
                if stem in best:
                    continue       # a loaded player already covers this save
                # The save knows its own capitalisation; the file name lost it.
                shown = stem
                try:
                    with open(os.path.join(SAVES_DIR, fn), encoding="utf-8") as fh:
                        shown = str(json.load(fh).get("username") or stem)
                except (OSError, ValueError, TypeError):
                    pass
                best[stem] = shown
    except OSError:
        pass
    return sorted(best.values())


def resolve_account(username):
    """The account a typed name refers to, as the rest of the server spells it,
    or None if there is no such save.

    Case-insensitive on purpose: account_names() is built from save FILE names,
    which _safe_name() lowercases, so a trainer called "Bracky68" is stored as
    "bracky68". Comparing exactly is why the World Manager used to answer "no
    account called 'Bracky68'" for the very trainer you were playing as.
    """
    name = (username or "").strip()
    if not name:
        return None
    known = account_names()
    if name in known:
        return name
    lowered = name.lower()
    # Prefer a loaded player, whose key is spelled the way the game uses it.
    with _lock:
        for loaded in _players:
            if loaded.lower() == lowered:
                return loaded
    for other in known:
        if other.lower() == lowered:
            return other
    return None


def last_active_account():
    """The trainer most recently saved -- what "me" means in the World Manager
    when no name is typed. On a phone there is usually exactly one."""
    best, best_mtime = None, -1.0
    for name in account_names():
        try:
            mtime = os.path.getmtime(
                os.path.join(SAVES_DIR, _safe_name(name) + ".json"))
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = name, mtime
    return resolve_account(best) if best else None


@contextlib.contextmanager
def acting_as(username):
    """Run a block as another account, on THIS thread only.

    The admin site runs on its own thread, so switching the thread-local player
    here cannot disturb an in-flight game request. Restores the previous player
    afterwards either way. Raises KeyError if the account has never been seen --
    we do NOT want a typo silently creating a new save.
    """
    name = resolve_account(username)
    if name is None:
        raise KeyError((username or "").strip())
    prev = getattr(_current, "player", None)
    try:
        yield use(name)
    finally:
        _current.player = prev


# ------------------------------------------------------------------ EGGS
def km_walked():
    return float(current().STATS.get("km_walked", 0.0) or 0.0)


def add_walked_km(km):
    """Credit distance directly (the World Manager's test walk). Returns the total."""
    p = current()
    with _lock:
        p.STATS["km_walked"] = km_walked() + float(km)
        total = p.STATS["km_walked"]
    p.save()
    return total


def add_distance(lat, lng):
    """Accumulate real walked distance from successive GPS fixes.

    Eggs only make sense if the distance is EARNED, so this is deliberately
    strict: a jump bigger than `max_step_m` is treated as a teleport (or GPS
    glitch) and contributes nothing, and sub-metre jitter is ignored so a
    stationary phone can't hatch an egg by trembling.
    """
    p = current()
    prev = p.LAST_POS
    p.LAST_POS = (lat, lng)
    if not prev:
        return 0.0
    dlat = (lat - prev[0]) * 111320.0
    dlng = ((lng - prev[1]) * 111320.0
            * math.cos(math.radians((lat + prev[0]) / 2.0)))
    metres = math.hypot(dlat, dlng)
    lo = _cfg.get("eggs", "min_step_m", cast=float)
    hi = _cfg.get("eggs", "max_step_m", cast=float)
    if metres < lo or metres > hi:
        return 0.0
    with _lock:
        p.STATS["km_walked"] = km_walked() + metres / 1000.0
    return metres


def eggs():
    with _lock:
        return [dict(e) for e in current().EGGS]


def incubators():
    with _lock:
        return [dict(i) for i in current().INCUBATORS]


def give_egg(target_km):
    """A new egg from a PokeStop. Returns None if the egg bag is full."""
    p = current()
    with _lock:
        if len(p.EGGS) >= _cfg.get("eggs", "max_eggs", cast=int):
            return None
        uid = _fresh_uid({c["uid"] for c in p.CAUGHT}
                         | {e["uid"] for e in p.EGGS} | set(p.DELETED))
        egg = {"uid": uid, "target_km": float(target_km),
               "start_km": 0.0, "incubator": ""}
        p.EGGS.append(egg)
    p.save()
    return dict(egg)


def use_incubator(incubator_id, egg_uid):
    """Put an egg in an incubator. Returns a UseItemEggIncubator result code:
    1=SUCCESS 2=NO_INCUBATOR 3=NO_EGG 4=NOT_AN_EGG 5=INCUBATOR_BUSY
    6=EGG_ALREADY_INCUBATING 7=NO_USES_LEFT."""
    p = current()
    with _lock:
        inc = next((i for i in p.INCUBATORS if i["id"] == incubator_id), None)
        if inc is None:
            return 2, None
        egg = next((e for e in p.EGGS if e["uid"] == egg_uid), None)
        if egg is None:
            return (4, None) if any(c["uid"] == egg_uid for c in p.CAUGHT) else (3, None)
        if inc.get("egg"):
            return 5, None
        if egg.get("incubator"):
            return 6, None
        if inc.get("uses", -1) == 0:
            return 7, None
        start = km_walked()
        egg["start_km"] = start
        egg["incubator"] = incubator_id
        inc.update(egg=egg_uid, start_km=start,
                   target_km=start + egg["target_km"])
    p.save()
    return 1, dict(inc)


def _hatch_uid(used):
    """A fresh uid whose IVs clear eggs.iv_floor. A Pokemon's IVs are derived from its
    uid (protocol._ivs) everywhere -- bag, gyms, battles, other accounts -- so picking
    the uid is what gives a hatchling the 10/10/10 floor of the real game instead of
    the 0-15 roll a wild catch gets. ~5% of uids pass a floor of 10, so this is a few
    dozen draws at most."""
    try:
        floor = max(0, min(15, int(_cfg.get("eggs", "iv_floor", cast=int))))
    except Exception:
        floor = 10
    return _floored_uid(used, floor)


def _floored_uid(used, floor):
    """A fresh unused uid whose three IVs are each at least `floor`."""
    import protocol
    for _ in range(5000):
        uid = _fresh_uid(used)
        if min(protocol._ivs(uid)) >= floor:
            return uid
    return uid


def research_uid():
    """A fresh uid for a research reward Pokemon: IVs clear pokemon.research_iv_floor
    (10/10/10 by default), like eggs do, instead of a wild catch's 0-15 roll."""
    try:
        floor = max(0, min(15, int(_cfg.get("pokemon", "research_iv_floor", cast=int))))
    except Exception:
        floor = 10
    p = current()
    with _lock:
        used = {int(c.get("uid", 0)) for c in p.CAUGHT} | set(p.DELETED)
    return _floored_uid(used, floor)


def reserve_encounter_uid(encounter_id, uid):
    """Fix the uid an encounter will show and be caught as (research rewards)."""
    with _lock:
        if len(ENCOUNTER_UIDS) > 5000:
            ENCOUNTER_UIDS.clear()
        ENCOUNTER_UIDS[(current().username, int(encounter_id))] = uid


def check_hatches(pick_species):
    """Hatch any egg that has covered its distance. `pick_species(target_km)`
    supplies the species so the rarity logic can live in protocol.py."""
    p = current()
    done = []
    with _lock:
        walked = km_walked()
        for egg in list(p.EGGS):
            if not egg.get("incubator"):
                continue
            if walked - egg["start_km"] < egg["target_km"]:
                continue
            tier = egg["target_km"]
            pid, cp = pick_species(tier)
            uid = _hatch_uid({c["uid"] for c in p.CAUGHT}
                             | {e["uid"] for e in p.EGGS} | set(p.DELETED))
            p.CAUGHT.append({"uid": uid, "pokemon_id": pid, "cp": cp,
                             "caught_ms": int(time.time() * 1000)})
            p.EGGS.remove(egg)
            # Inventory deltas are additive: without a deletion the client keeps the
            # hatched egg in its list, still "in" the incubator that is now free.
            p.DELETED[int(egg["uid"])] = int(time.time() * 1000)
            for i in p.INCUBATORS:
                if i["id"] == egg["incubator"]:
                    i.update(egg=0, start_km=0.0, target_km=0.0)
                    if i.get("uses", -1) > 0:
                        i["uses"] -= 1
            # A basic incubator on its last use breaks, as in the real game.
            p.INCUBATORS = [i for i in p.INCUBATORS if i.get("uses", -1) != 0]
            xp = _cfg.get("eggs", "hatch_xp", cast=int)
            candy = 2 + int(tier)
            dust = int(tier) * 100
            rec = {"uid": uid, "pokemon_id": pid, "cp": cp, "km": tier,
                   "xp": xp, "candy": candy, "stardust": dust,
                   "egg_uid": egg["uid"]}
            p.HATCHED.append(rec)
            done.append(rec)
            p.STATS["eggs_hatched"] = p.STATS.get("eggs_hatched", 0) + 1
    if done:
        p.save()
    # A hatchling counts towards the type and species medals exactly like a
    # catch does. Done outside the lock -- bump_type takes it itself.
    for rec in done:
        import protocol
        protocol._score_medals(rec["pokemon_id"], rec["uid"])
    return done


def drain_hatched():
    """Hand the client the eggs that hatched since it last asked."""
    p = current()
    with _lock:
        out, p.HATCHED = list(p.HATCHED), []
    if out:
        p.save()
    return out


def pokedex_saw(pokemon_id):
    p = current()
    with _lock:
        e = p.POKEDEX.setdefault(int(pokemon_id), [0, 0])
        e[0] += 1
    p.save()


def pokedex_caught(pokemon_id):
    p = current()
    with _lock:
        e = p.POKEDEX.setdefault(int(pokemon_id), [0, 0])
        e[1] += 1
        if e[0] < e[1]:
            e[0] = e[1]          # caught implies seen
    p.save()


def pokedex_is_new(pokemon_id):
    """True until this species has been caught (or hatched) once."""
    with _lock:
        e = current().POKEDEX.get(int(pokemon_id))
    return not e or e[1] <= 0


def pokedex():
    with _lock:
        return sorted((pid, v[0], v[1]) for pid, v in current().POKEDEX.items())


def apply_item(item_id, minutes):
    """Start a Lucky Egg / Incense. (result_code, entry): 1=SUCCESS,
    2=ALREADY_ACTIVE, 3=NONE_IN_INVENTORY."""
    p = current()
    now = int(time.time() * 1000)
    with _lock:
        p.APPLIED = [a for a in p.APPLIED if a.get("expires_ms", 0) > now]
        if any(a["item"] == int(item_id) for a in p.APPLIED):
            return 2, None
    if not take_item(int(item_id), 1):
        return 3, None
    entry = {"item": int(item_id), "applied_ms": now,
             "expires_ms": now + int(minutes * 60000)}
    with _lock:
        p.APPLIED.append(entry)
    p.save()
    return 1, dict(entry)


def applied_items():
    p = current()
    now = int(time.time() * 1000)
    with _lock:
        p.APPLIED = [a for a in p.APPLIED if a.get("expires_ms", 0) > now]
        return [dict(a) for a in p.APPLIED]


def item_active(item_id):
    return any(a["item"] == int(item_id) for a in applied_items())


def xp_multiplier():
    """A Lucky Egg doubles everything you earn while it burns; an event XP bonus
    (events.json xp_bonus) multiplies on top."""
    try:
        import events
        bonus = events.xp_bonus()[0]
    except Exception:
        bonus = 1
    return (2 if item_active(301) else 1) * bonus


# The shiny pair are BAG items (2026-09-25): two Items the 2016 game never used,
# renamed and given their icons in the patched client (tools/patch_badges.py).
#   Shiny Charm   = ITEM_X_MIRACLE (604): works while it's in the bag, like a key item.
#   Shiny Incense = ITEM_INCENSE_SPICY (402): used from the bag like Incense; it then
#                   burns in APPLIED as 402, so the client shows it as an active incense.
# SHINY_INCENSE_ITEM (9401) is the old server-only burn, still honoured for saves
# that bought one before this.
SHINY_CHARM_ITEM = 604
SHINY_INCENSE_BAG_ITEM = 402
SHINY_INCENSE_ITEM = 9401


# The Shiny Charm is gone (2026-10-05). A save that still has one (in the bag, or
# the older SHINY_CHARM flag) loses it on load and gets its price back.
SHINY_CHARM_REFUND = 1000


def has_shiny_charm():
    """Always False: the Shiny Charm was removed."""
    return False


def grant_shiny_charm():
    return False, "The Shiny Charm isn't sold any more."


def grant_shiny_incense():
    """(ok, message). A Shiny Incense into the bag -- use it from there."""
    if room_in_bag() < 1:
        return False, "Your bag is full."
    n = add_item(SHINY_INCENSE_BAG_ITEM, 1)
    return True, f"Shiny Incense added to your bag (you have {n})."


def start_shiny_incense(minutes):
    """(ok, message). Like apply_item(), but nothing is taken from the bag --
    the shop grants the burn directly."""
    p = current()
    now = int(time.time() * 1000)
    with _lock:
        p.APPLIED = [a for a in p.APPLIED if a.get("expires_ms", 0) > now]
        if any(a["item"] == SHINY_INCENSE_ITEM for a in p.APPLIED):
            return False, "A Shiny Incense is already burning."
        p.APPLIED.append({"item": SHINY_INCENSE_ITEM, "applied_ms": now,
                          "expires_ms": now + int(float(minutes) * 60000)})
    p.save()
    return True, f"Shiny Incense lit for {int(float(minutes))} minutes."


def client_applied_items():
    """applied_items() minus our own private boosts. The 2016 client draws an
    active-buff icon from its bundled art, so an id it has never heard of must
    never reach it -- SHINY_INCENSE_ITEM is server-side only."""
    return [a for a in applied_items() if int(a.get("item", 0)) < 9000]


def shiny_incense_ms_left():
    now = int(time.time() * 1000)
    return max(0, max([a["expires_ms"] for a in applied_items()
                       if a["item"] in (SHINY_INCENSE_ITEM, SHINY_INCENSE_BAG_ITEM)] or [0]) - now)


def save_lures():
    """Lures live in the shared world, so -- like gyms -- they need their own
    file. Without this a Lure the player SPENT an item on vanished the moment the
    server restarted, while Lucky Egg/Incense (stored in the per-player save)
    survived. Same 30 minutes, two different outcomes."""
    try:
        tmp = LURES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"lures": FORT_MODIFIERS}, fh, indent=1)
        os.replace(tmp, LURES_FILE)
    except OSError:
        pass


def load_lures():
    now = int(time.time() * 1000)
    try:
        with open(LURES_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return False
    FORT_MODIFIERS.clear()
    # Drop anything that burned out while the server was down -- a lure's timer
    # is wall-clock, so it does not pause for a restart.
    FORT_MODIFIERS.update({k: v for k, v in (d.get("lures") or {}).items()
                           if isinstance(v, dict) and v.get("expires_ms", 0) > now})
    return True


def add_fort_modifier(fort_id, item_id, minutes, by, pos=None):
    """Attach a Lure to a PokeStop. 1=SUCCESS 2=ALREADY_HAS_ONE 4=NO_ITEM.
    pos = the stop's (lat, lng) when known: kept with the lure, so its Pokemon can be
    placed even when the map request doesn't carry that stop (and after a restart)."""
    now = int(time.time() * 1000)
    with _lock:
        cur = FORT_MODIFIERS.get(fort_id)
        if (cur and cur.get("expires_ms", 0) > now
                and _cfg.get("boosts", "lure_stacks", cast=bool)):
            # Lure party: add this lure's time to what's already burning.
            cap = now + int(_cfg.get("boosts", "lure_max_minutes", cast=float) * 60000)
            cur["expires_ms"] = min(cap, int(cur["expires_ms"]) + int(minutes * 60000))
            if pos and "lat" not in cur:
                cur["lat"], cur["lng"] = float(pos[0]), float(pos[1])
            FORT_MODIFIERS[fort_id] = cur
            save_lures()
            return 1, dict(cur)
        if cur and cur.get("expires_ms", 0) > now:
            return 2, None
    if not take_item(int(item_id), 1):
        return 4, None
    mod = {"item": int(item_id), "expires_ms": now + int(minutes * 60000), "by": by}
    if pos:
        mod["lat"], mod["lng"] = float(pos[0]), float(pos[1])
    with _lock:
        FORT_MODIFIERS[fort_id] = mod
    save_lures()
    return 1, dict(mod)


def fort_modifier(fort_id):
    now = int(time.time() * 1000)
    with _lock:
        m = FORT_MODIFIERS.get(fort_id)
        if not m:
            return None
        if m.get("expires_ms", 0) <= now:
            FORT_MODIFIERS.pop(fort_id, None)
            expired = True
        else:
            expired = False
            out = dict(m)
    if expired:
        save_lures()
        return None
    return out


def lured_forts():
    now = int(time.time() * 1000)
    with _lock:
        dead = [k for k, m in FORT_MODIFIERS.items()
                if m.get("expires_ms", 0) <= now]
        for fid in dead:
            FORT_MODIFIERS.pop(fid, None)
        out = {k: dict(v) for k, v in FORT_MODIFIERS.items()}
    # Only touch the disk when a lure actually burned out; this runs on every
    # map refresh.
    if dead:
        save_lures()
    return out


def add_stardust(n):
    """Stardust had no adder at all -- it could only ever be SPENT (spend()), so
    the total drifted down from its starting value and never up."""
    p = current()
    with _lock:
        p.STARDUST += int(n)
        out = p.STARDUST
    p.save()
    return out


def candy(family):
    with _lock:
        return current().CANDY.get(family, 0)


def spend(family=None, candy_n=0, dust_n=0):
    p = current()
    with _lock:
        if candy_n and p.CANDY.get(family, 0) < candy_n:
            return False
        if dust_n and p.STARDUST < dust_n:
            return False
        if candy_n:
            p.CANDY[family] = p.CANDY.get(family, 0) - candy_n
        if dust_n:
            p.STARDUST -= dust_n
    p.save()
    return True


# --- storage -----------------------------------------------------------------
def spend_coins(n):
    """Take PokeCoins for a shop purchase. False if there aren't enough."""
    p = current()
    with _lock:
        if p.COINS < int(n):
            return False
        p.COINS -= int(n)
    p.save()
    return True


def add_coins(n):
    p = current()
    with _lock:
        p.COINS += int(n)
        out = p.COINS
    p.save()
    return out


def buy_storage(kind):
    p = current()
    step = _cfg.get("storage", "pokemon_upgrade_step" if kind == "pokemon"
                    else "items_upgrade_step", cast=int)
    cost = _cfg.get("storage", "pokemon_upgrade_cost" if kind == "pokemon"
                    else "items_upgrade_cost", cast=int)
    cap = _cfg.get("storage", "max_pokemon_limit" if kind == "pokemon"
                   else "max_items_limit", cast=int)
    with _lock:
        cur = p.MAX_POKEMON if kind == "pokemon" else p.MAX_ITEMS
        if cur + step > cap:
            return False, f"already at the maximum ({cap})", cur
        if p.COINS < cost:
            return False, f"need {cost} PokeCoins, you have {p.COINS}", cur
        p.COINS -= cost
        if kind == "pokemon":
            p.MAX_POKEMON = cur + step
            new = p.MAX_POKEMON
        else:
            p.MAX_ITEMS = cur + step
            new = p.MAX_ITEMS
    p.save()
    return True, f"+{step} space for {cost} coins", new


def storage():
    p = current()
    with _lock:
        return {"max_pokemon": p.MAX_POKEMON, "max_items": p.MAX_ITEMS,
                "pokemon_used": len(p.CAUGHT), "items_used": sum(p.BAG.values()),
                "coins": p.COINS, "stardust": p.STARDUST}


# --- gyms (shared between accounts) ------------------------------------------
def _defender_minutes():
    return _cfg.get("gyms", "defender_minutes", env="DEFENDER_MINUTES", cast=float)


def _defender_coins():
    return _cfg.get("gyms", "defender_coins", env="DEFENDER_COINS", cast=int)


def _max_defenders():
    return _cfg.get("gyms", "max_defenders", cast=int)


def my_team():
    """The team this trainer picked in game. Falls back to the settings value for
    accounts created before teams could be chosen."""
    t = current().TEAM
    return t if t else _cfg.get("gyms", "team", env="TEAM", cast=int)


def tutorial_steps():
    """The TutorialCompletion steps this trainer has finished. Empty = run the
    whole new-trainer onboarding; [0..7] = skip straight to the map."""
    return sorted(set(current().TUTORIAL))


def mark_tutorial(steps):
    """Record onboarding steps the client just reported complete."""
    p = current()
    add = [int(s) for s in (steps or []) if str(s).lstrip("-").isdigit()]
    if not add:
        return
    with _lock:
        p.TUTORIAL = sorted(set(p.TUTORIAL) | set(add))
    p.save()


def codename():
    return current().CODENAME or ""


def owner_codename(owner):
    """The in-game name of a loaded trainer (by login name), or "" if unknown --
    so a defender shows the name its trainer goes by NOW, and old defenders saved
    under the login handle get their real name back."""
    with _lock:
        p = _players.get(owner) if owner else None
    return (getattr(p, "CODENAME", "") or "") if p is not None else ""


def owner_level(owner):
    """The trainer level of a loaded trainer (by login name), or 0 if unknown."""
    with _lock:
        p = _players.get(owner) if owner else None
    return int(getattr(p, "LEVEL", 0) or 0) if p is not None else 0


def set_codename(name):
    """The trainer's chosen in-game name (NAME_SELECTION step)."""
    p = current()
    with _lock:
        p.CODENAME = str(name or "")[:60]     # room for [colour] tags
    p.save()
    return p.CODENAME


def buy_stop(cost):
    """Pay for a 'make a PokeStop here'. The FIRST one is free; after that it costs
    `cost` PokeCoins. Returns (ok, charged, reason)."""
    p = current()
    with _lock:
        if not p.FREE_STOP_USED:
            p.FREE_STOP_USED = True
            p.save()
            return True, 0, "first one free"
    if spend_coins(cost):
        return True, cost, "ok"
    return False, cost, f"need {cost} PokeCoins, you have {p.COINS}"


def set_team(team):
    """SetPlayerTeam. Returns (status, team): 1=SUCCESS, 2=TEAM_ALREADY_SET."""
    p = current()
    with _lock:
        if p.TEAM:
            return 2, p.TEAM
        p.TEAM = max(1, min(3, int(team)))
        out = p.TEAM
    p.save()
    return 1, out


def use_berry(encounter_id, mult):
    """Remember that a berry is in effect for this encounter."""
    current().BERRIES[int(encounter_id)] = float(mult)


def berry_mult(encounter_id, consume=False):
    p = current()
    m = p.BERRIES.get(int(encounter_id), 1.0)
    if consume:
        p.BERRIES.pop(int(encounter_id), None)
    return m


def pokemon_full():
    p = current()
    return len(p.CAUGHT) + len(p.EGGS) >= p.MAX_POKEMON


def bag_count():
    return sum(current().BAG.values())


def bag_full():
    return bag_count() >= current().MAX_ITEMS


def room_in_bag():
    return max(0, current().MAX_ITEMS - bag_count())


def _raid_member(fort_id):
    """The boss that stands in every gym while raid mode is on. Its uid is derived
    from the fort so it is stable per gym and never collides with a real Pokemon."""
    uid = (abs(hash(("raid", fort_id))) & 0x3FFFFFFFFFFFFFFF) | 1
    return {"uid": uid, "pokemon_id": int(RAID["pokemon_id"]),
            "cp": int(RAID["cp"]), "trainer": RAID.get("trainer", "raid"),
            "team": 0, "raid": True, "deployed_ms": int(time.time() * 1000)}


def is_raid_uid(fort_id, uid):
    return RAID["on"] and _raid_member(fort_id)["uid"] == uid


# Gyms nobody holds used to be EMPTY, and an empty gym cannot be fought:
# START_GYM_BATTLE answers GYM_EMPTY and the client only offers "deploy". So
# every gym on a fresh server was decoration. These are rival defenders for
# unclaimed gyms -- worked out from the fort id, so a gym looks the same every
# time you pass it, and nothing is written to disk until you actually fight.
NPC_CLEARED = {}                      # fort_id -> ms it was beaten
# Defenders that came home while their owner was offline:
# owner -> [[fort_id, pokemon_id, coins, owner, uid], ...], paid on their next request.
RETURNS = {}
_NPC_TRAINER = "Rival"


def _npc_count():
    return max(0, _cfg.get("gyms", "npc_defenders", cast=int))


def _npc_members(fort_id):
    """Who is guarding an unclaimed gym, or [] when the feature is off or the
    gym was beaten recently."""
    n = _npc_count()
    if not n:
        return []
    mins = max(0, _cfg.get("gyms", "npc_respawn_minutes", cast=int))
    beaten = int(NPC_CLEARED.get(fort_id, 0))
    if beaten and (time.time() * 1000 - beaten) < mins * 60_000:
        return []
    lo = max(10, _cfg.get("gyms", "npc_min_cp", cast=int))
    hi = max(lo, _cfg.get("gyms", "npc_max_cp", cast=int))
    rnd = _random.Random(f"npc:{fort_id}")
    # A team that is not the player's, so the client offers a fight rather
    # than a deploy slot.
    mine = my_team() or 1
    team = rnd.choice([t for t in (1, 2, 3) if t != mine])
    out = []
    for i in range(n):
        pid = rnd.randint(1, 151)
        while pid in _NPC_SKIP:
            pid = rnd.randint(1, 151)
        out.append({"uid": (abs(hash(("npc", fort_id, i))) & 0x3FFFFFFFFFFFFFFF) | 1,
                    "pokemon_id": pid, "cp": rnd.randint(lo, hi),
                    "trainer": _NPC_TRAINER, "team": team, "npc": True,
                    "deployed_ms": int(time.time() * 1000)})
    out.sort(key=lambda m: -m["cp"])
    return out


_NPC_SKIP = {144, 145, 146, 150, 151}      # no legendaries guarding a street gym


def ensure_npc_defenders(fort_id):
    """Make the rivals real, just before a battle.

    Kept virtual until here on purpose: gym_guard() runs for every gym in every
    map response, and writing one of these per gym seen would fill gyms.json
    with thousands of entries. Once they are in GYMS the existing battle,
    prestige and eject logic treats them like any other defender.
    """
    with _lock:
        if GYMS.get(fort_id):
            return False
        members = _npc_members(fort_id)
        if not members:
            return False
        GYMS[fort_id] = members
        PRESTIGE.setdefault(fort_id, 500 * len(members))
    save_gyms()
    return True


def gym_members(fort_id):
    with _lock:
        if RAID["on"]:
            # The boss REPLACES whatever was defending. Real defenders were sent
            # home when raid mode was switched on, so nothing is lost.
            return [_raid_member(fort_id)]
        real = GYMS.get(fort_id)
        if real:
            return list(real)
    return _npc_members(fort_id)      # outside the lock: reads settings


def load_raid():
    global RAID
    try:
        with open(RAID_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        RAID.update({"on": bool(d.get("on", False)),
                     "pokemon_id": int(d.get("pokemon_id", 150) or 150),
                     "cp": int(d.get("cp", 3000) or 3000),
                     "trainer": str(d.get("trainer", "raid") or "raid")})
    except (OSError, ValueError, TypeError):
        pass
    return dict(RAID)


def save_raid():
    try:
        tmp = RAID_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(RAID, fh, indent=1)
        os.replace(tmp, RAID_FILE)
    except OSError:
        pass


def set_raid(on=None, pokemon_id=None, cp=None, trainer=None):
    """Turn raid mode on/off. Switching it ON sends every deployed Pokemon home
    first, so nobody loses a defender to the boss taking its place."""
    sent_home = 0
    with _lock:
        if on is not None:
            RAID["on"] = bool(on)
        if pokemon_id is not None:
            RAID["pokemon_id"] = max(1, min(151, int(pokemon_id)))
        if cp is not None:
            RAID["cp"] = max(10, min(9999, int(cp)))
        if trainer is not None:
            RAID["trainer"] = str(trainer)[:16] or "raid"
        RAID_BOSSES.clear()            # any change starts every boss fresh at full HP
        if RAID["on"]:
            sent_home = sum(len(v) for v in GYMS.values())
            GYMS.clear()
            PRESTIGE.clear()
    save_gyms()
    save_raid()
    return dict(RAID), sent_home


def raid():
    with _lock:
        return dict(RAID)


def raid_boss_state(fort_id, max_hp, now_ms):
    """The shared HP of one gym's raid boss -- every trainer fights the same pool.
    Returns {hp, max, down_until}. A knocked-out boss comes back at full HP once its
    respawn time has passed, or straight away if the boss/CP was changed."""
    key = (int(RAID["pokemon_id"]), int(RAID["cp"]), int(max_hp))
    with _lock:
        s = RAID_BOSSES.get(fort_id)
        if s is None or s["key"] != key or (s["down_until"] and now_ms >= s["down_until"]):
            s = {"key": key, "hp": int(max_hp), "max": int(max_hp),
                 "down_until": 0, "damage": {}}
            RAID_BOSSES[fort_id] = s
        return {"hp": s["hp"], "max": s["max"], "down_until": s["down_until"]}


def raid_hit(fort_id, username, damage, now_ms, respawn_ms):
    """Apply one trainer's damage to a gym's shared boss. Returns
    (hp_left, felled_now, {username: damage}). felled_now is True only for the
    request that lands the final blow, so the group is rewarded exactly once."""
    with _lock:
        s = RAID_BOSSES.get(fort_id)
        if s is None:
            return 0, False, {}
        if s["hp"] <= 0:
            return 0, False, dict(s["damage"])
        # Only the HP the boss actually had left counts -- an overkill finishing blow
        # mustn't outscore the trainers who did the real work.
        d = min(max(0, int(damage)), s["hp"])
        if d:
            s["damage"][username] = s["damage"].get(username, 0) + d
            s["hp"] = max(0, s["hp"] - d)
        if s["hp"] == 0:
            s["down_until"] = int(now_ms) + int(respawn_ms)
            return 0, True, dict(s["damage"])
        return s["hp"], False, dict(s["damage"])


def raid_bosses():
    """Live boss HP per gym, for the World Manager."""
    with _lock:
        return {f: {"hp": s["hp"], "max": s["max"], "down_until": s["down_until"],
                    "trainers": len(s["damage"])} for f, s in RAID_BOSSES.items()}


def set_player_location(lat, lng, username=None):
    """Remember where each trainer is, so a raid drop lands at THEIR feet (the old
    single last-location was whoever happened to send the latest request)."""
    if not (abs(lat) > 1e-6 or abs(lng) > 1e-6):
        return
    who = username or current().username
    with _lock:
        PLAYER_LOC[who] = (float(lat), float(lng))

    # Also keep it in the save, so it survives a restart. This is called on
    # EVERY rpc -- several times a minute -- so it only writes when the trainer
    # has actually moved or the stored fix has gone stale.
    now = int(time.time() * 1000)
    try:
        p = current()
        if p.username != who:
            return
        old = p.LAST_SEEN
        if old:
            moved = math.hypot((lat - old[0]) * 111_320.0,
                               (lng - old[1]) * 111_320.0
                               * max(0.05, math.cos(math.radians(lat))))
            if moved < 25.0 and now - int(old[2] or 0) < 120_000:
                return
        p.LAST_SEEN = [float(lat), float(lng), now]
        p.save()
    except Exception:
        pass


def player_location(username):
    """Where this trainer is. The live position when they have played since the
    server started, otherwise the one saved from last time -- which is what
    lets the desktop radar work with the game closed."""
    with _lock:
        live = PLAYER_LOC.get(username)
    if live:
        return live
    # Read the save directly rather than use(): use() switches the ACTIVE
    # trainer, and this is called from request handlers serving someone else.
    try:
        path = os.path.join(SAVES_DIR, _safe_name(username) + ".json")
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh).get("last_seen")
        return (float(saved[0]), float(saved[1])) if saved else None
    except (OSError, ValueError, TypeError, IndexError):
        return None


def _day_key(offset=0):
    """Today's date where the SERVER is, as YYYY-MM-DD (offset in days)."""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + offset * 86400))


def daily_streak(kind):
    """First catch ('catch') or stop spin ('spin') of the day.

    Returns (xp, dust, items, streak_days, seventh). Items are added to the bag here;
    the XP and stardust are RETURNED rather than credited, so the caller can put them on
    the catch/spin screen and bank them once. Later calls the same day return zeros.
    """
    if not _cfg.get("daily", "enabled", cast=bool):
        return 0, 0, [], 0, False
    p = current()
    today, yesterday = _day_key(), _day_key(-1)
    day_key, count_key = kind + "_day", kind + "_days"
    with _lock:
        st = p.STREAK
        if st.get(day_key) == today:
            return 0, 0, [], int(st.get(count_key, 0)), False
        st[count_key] = int(st.get(count_key, 0)) + 1 if st.get(day_key) == yesterday else 1
        st[day_key] = today
        days = int(st[count_key])
    seventh = (days % 7 == 0)
    suffix = "7" if seventh else ""
    xp = int(_cfg.get("daily", f"{kind}{suffix}_xp", cast=int))
    dust = int(_cfg.get("daily", f"{kind}{suffix}_dust", cast=int))
    items = []
    if seventh:
        for iid, cnt in (_cfg.get("daily", f"{kind}7_items") or {}).items():
            try:
                items.append((int(iid), int(cnt)))
            except (TypeError, ValueError):
                pass
    for iid, cnt in items:
        add_item(iid, cnt)
    p.save()
    return xp, dust, items, days, seventh


def streaks():
    """Both streaks, for the World Manager."""
    p = current()
    with _lock:
        return dict(p.STREAK)


def add_bonus_spawn(username, eid, pid, cp, lat, lng, expires_ms):
    """A one-off wild Pokemon placed for ONE trainer -- the defeated raid boss
    dropping at your feet. Kept per account so it doesn't appear for everyone."""
    with _lock:
        lst = BONUS_SPAWNS.setdefault(username, [])
        lst.append({"eid": eid, "pid": pid, "cp": cp, "lat": lat, "lng": lng,
                    "expires_ms": expires_ms})
        del lst[:-10]


def bonus_spawns(username):
    now = int(time.time() * 1000)
    with _lock:
        lst = [b for b in BONUS_SPAWNS.get(username, []) if b["expires_ms"] > now]
        BONUS_SPAWNS[username] = lst
        return [dict(b) for b in lst]


def drop_bonus_spawn(username, eid):
    with _lock:
        lst = BONUS_SPAWNS.get(username, [])
        BONUS_SPAWNS[username] = [b for b in lst if b["eid"] != eid]


def gym_team(fort_id):
    """Which team holds this gym (0 = neutral/white)."""
    ms = gym_members(fort_id)
    return ms[0].get("team", 0) if ms else 0


def _level_for(prestige):
    lvl = 1
    for i, thr in enumerate(GYM_LEVELS):
        if prestige >= thr:
            lvl = i + 1
    return max(1, min(10, lvl))


def gym_prestige(fort_id):
    with _lock:
        if fort_id in PRESTIGE:
            return int(PRESTIGE[fort_id])
        held = bool(GYMS.get(fort_id))
    if held:
        return 0
    # A gym still held by virtual rivals has no PRESTIGE entry yet, and the
    # client reads the gym's LEVEL off this -- reporting 0 drew a level-less
    # gym. Same value ensure_npc_defenders() will write when you fight it.
    return 500 * len(_npc_members(fort_id))


def gym_level(fort_id):
    """A gym's level, 1..10, derived from its prestige (also its defender slots)."""
    return _level_for(gym_prestige(fort_id))


def gym_capacity(fort_id):
    return gym_level(fort_id)


def prestige_for_defeat(atk_cp, def_cp):
    """Prestige swing for beating one defender. Scales with the CP you were up
    against, so toppling a stronger Pokemon is worth more (2016 behaviour)."""
    ratio = (int(def_cp or 0) or 1) / max(1, int(atk_cp or 0) or 1)
    return int(max(400, min(2500, 800 * (0.4 + ratio))))


def add_prestige(fort_id, delta):
    """Apply the prestige change from a finished battle. Positive = training a
    friendly gym up; negative = an attack draining it. Returns
    (new_prestige, new_level, ejected_members); at <=0 the gym goes NEUTRAL (every
    defender sent home) so the winner can claim it with a fresh deploy."""
    ejected = []
    with _lock:
        # 2016 gyms topped out at 52,000 prestige (level 10 starts at 50,000).
        new = min(GYM_PRESTIGE_CAP, int(PRESTIGE.get(fort_id, 0)) + int(delta))
        if new <= 0:
            ejected = list(GYMS.get(fort_id, []))
            GYMS.pop(fort_id, None)
            PRESTIGE.pop(fort_id, None)
            level, new = 1, 0
            # Rivals stay beaten for a while; otherwise the gym you just took
            # would have a fresh set guarding it on the next map refresh.
            if any(m.get("npc") for m in ejected):
                NPC_CLEARED[fort_id] = int(time.time() * 1000)
        else:
            PRESTIGE[fort_id] = new
            level = _level_for(new)
            members = GYMS.get(fort_id, [])
            over = len(members) - level
            if over > 0:                      # a level-down kicks out the weakest
                lowest = sorted(members, key=lambda m: m.get("cp", 0))[:over]
                drop = {m["uid"] for m in lowest}
                GYMS[fort_id] = [m for m in members if m["uid"] not in drop]
                ejected = lowest
    save_gyms()
    return new, level, ejected


def _is_mine(m, me):
    return m.get("owner") in (None, me)


def is_deployed(uid):
    """Is THIS trainer's Pokemon `uid` defending a gym? Ids are only unique inside
    one trainer's box (two trainers who catch the same shared spawn can hold the
    same id), so someone else's defender must not lock up your copy."""
    me = current().username
    with _lock:
        return any(m["uid"] == uid and _is_mine(m, me)
                   for ms in GYMS.values() for m in ms)


def deployed_fort(uid):
    """The fort_id this Pokemon is defending, or None. The client counts your
    defenders by which Pokemon in the inventory carry a deployed_fort_id, and it
    greys the Shop's defender-bonus shield (never even sends the collect request)
    when that count is zero -- so this is what makes the shield collectable."""
    member = deployed_member(uid)
    return member[0] if member else None


def deployed_member(uid):
    """(fort_id, a copy of the gym member) for THIS trainer's Pokemon `uid` if it
    is defending a gym, or None."""
    me = current().username
    with _lock:
        for fid, ms in GYMS.items():
            for m in ms:
                if m["uid"] == uid and _is_mine(m, me):
                    return fid, dict(m)
    return None


def deploy(fort_id, uid, trainer=None, team=None):
    p = current()
    # The trainer's chosen in-game name -- p.username is the login handle (a
    # website account's is "ws" + 8 hex digits), which isn't meant to be seen.
    trainer = trainer or p.CODENAME or p.username
    team = team if team is not None else my_team()
    level = stats()[0]
    c = get_caught(uid)
    if not c:
        return False, "unknown pokemon"
    if not team:
        return False, "no team"
    # A gym still held by (virtual) rivals, or standing in for a raid boss, is not
    # an empty slot: claiming it would have made the rivals vanish under you.
    if RAID["on"]:
        return False, "held by another team"
    rivals = not GYMS.get(fort_id) and _npc_members(fort_id)
    if rivals:
        return False, "held by another team"
    with _lock:
        # One gym at a time per Pokemon -- anywhere, not just this gym. (And never
        # two defenders with the same id in one gym: the client keys them by id.)
        if any(m["uid"] == uid and (_is_mine(m, p.username) or fid == fort_id)
               for fid, ms in GYMS.items() for m in ms):
            return False, "already deployed"
        members = GYMS.setdefault(fort_id, [])
        if members and members[0].get("team", team) != team:
            return False, "held by another team"
        # Slots scale with the gym's level (its prestige), not a flat cap -- a fresh
        # gym holds one, and it must be TRAINED up before more can join. Computed
        # inline because we already hold _lock here.
        if len(members) >= _level_for(int(PRESTIGE.get(fort_id, 0))):
            return False, "gym full"
        members.append({"uid": uid, "pokemon_id": c["pokemon_id"], "cp": c["cp"],
                        "trainer": trainer, "team": team, "owner": p.username,
                        "level": level,     # shown on the gym screen when offline
                        "avatar": dict(p.AVATAR),   # their look, not the viewer's
                        "shiny": bool(c.get("shiny")),      # drawn shiny at the gym
                        # A defender walks in with the health it has, and keeps
                        # whatever an attacker knocks off it (see set_gym_hp).
                        "stamina": c.get("stamina"),
                        "deployed_ms": int(time.time() * 1000)})
    save_gyms()
    return True, "ok"


def set_gym_hp(fort_id, uid, hp):
    """Remember how battered a defender is.

    Gym HP used to live only inside one battle, so backing out and attacking again
    faced defenders restored to full -- a gym could not be worn down over several
    fights. The damage is stored on the member and saved with the gym (it heals
    back over gyms.defender_heal_minutes, see protocol._defender_hp), and the
    owner's own copy of the Pokemon is kept in step so the Pokemon list draws the
    same bar. Unknown forts and raid bosses -- which are generated fresh and are
    not members -- are a silent no-op."""
    hp = max(0, int(hp))
    owner = None
    with _lock:
        for m in GYMS.get(fort_id, []):
            if m["uid"] == uid:
                if m.get("stamina") == hp:
                    return False
                m["stamina"] = hp
                m["hurt_ms"] = int(time.time() * 1000)
                owner = m.get("owner")
                break
        else:
            return False
    save_gyms()
    if owner and owner == current().username:
        update_caught(uid, stamina=hp)
    return True


def reset_gym_hp(fort_id, uid):
    """A defender that was knocked out but still holds its slot (the gym kept enough
    prestige) stands back up at FULL health for the next challenger, as in 2016. Storing
    the 0 instead left it on 1 HP -- _defender_hp never reports 0 -- so the next fight
    was over in one tap."""
    with _lock:
        for m in GYMS.get(fort_id, []):
            if m["uid"] == uid:
                if m.get("stamina") is None and "hurt_ms" not in m:
                    return False
                m["stamina"] = None
                m.pop("hurt_ms", None)
                break
        else:
            return False
    save_gyms()
    return True


def gym_hp(fort_id, uid):
    """The defender's stored health, or None if it has never been hurt. This is the
    RAW stored value -- protocol._defender_hp() applies the heal-back."""
    for m in gym_members(fort_id):
        if m["uid"] == uid:
            v = m.get("stamina")
            return None if v is None else max(0, int(v))
    return None


def recall(fort_id, uid):
    """Take YOUR defender back off a gym. Someone else's Pokemon is not yours to
    recall (this used to remove any uid it was handed)."""
    me = current().username
    with _lock:
        members = GYMS.get(fort_id, [])
        keep = [m for m in members
                if not (m["uid"] == uid and m.get("owner") in (None, me))]
        changed = len(keep) != len(members)
        if not changed:
            return False
        if keep:
            GYMS[fort_id] = keep
        else:
            GYMS.pop(fort_id, None)
    save_gyms()
    return True


def clear_gym(fort_id):
    """Every defender is knocked out -- the gym goes neutral."""
    with _lock:
        GYMS.pop(fort_id, None)
        PRESTIGE.pop(fort_id, None)
    save_gyms()


def gym_guard(fort_id):
    ms = gym_members(fort_id)
    if not ms:
        return None
    best = max(ms, key=lambda m: m.get("cp", 0))
    return best["pokemon_id"], best["cp"], 500 * len(ms)


def collect_gym_returns():
    """Bring home any Pokemon that has served its shift and pay its coins. Only
    the account that OWNS a defender gets paid for it."""
    now = int(time.time() * 1000)
    cutoff = _defender_minutes() * 60_000
    coins = _defender_coins()
    faint = _cfg.get("pokemon", "faint_after_gym", cast=bool)
    returned = []
    me = current()
    with _lock:
        changed = False
        for fid in list(GYMS):
            keep = []
            for m in GYMS.get(fid, []):
                dep = m.get("deployed_ms")
                if dep is None:
                    m["deployed_ms"] = now
                    keep.append(m)
                elif now - dep >= cutoff:
                    changed = True
                    returned.append((fid, m["pokemon_id"], coins,
                                     m.get("owner"), m["uid"]))
                else:
                    keep.append(m)
            if keep:
                GYMS[fid] = keep
            else:
                # Shifts are short (gyms.defender_minutes), so the gym keeps its
                # prestige -- the training isn't lost every time it empties.
                GYMS.pop(fid, None)
        # Someone else's defender came home during THIS trainer's request. It is
        # gone from the gym now, so its owner can't find it on their next check --
        # hold the payout for them (saved with the gyms) instead of dropping it.
        for r in returned:
            if r[3] and r[3] != me.username:
                RETURNS.setdefault(r[3], []).append(list(r))
        returned = [r for r in returned if not r[3] or r[3] == me.username]
        held = RETURNS.pop(me.username, [])
        changed = changed or bool(held)
        returned += [tuple(r) for r in held]
    mine = []
    if changed:
        save_gyms()
    if returned:
        for fid, pid, c, owner, uid in returned:
            with _lock:
                me.COINS += c
                if faint:
                    for pk in me.CAUGHT:
                        if pk["uid"] == uid:
                            pk["stamina"] = 0
                            break
            mine.append((fid, pid, c))
        if mine:
            me.save()
    return mine


def time_left(fort_id, uid):
    for m in gym_members(fort_id):
        if m["uid"] == uid:
            dep = m.get("deployed_ms") or 0
            return max(0, int(_defender_minutes() * 60 - (time.time() - dep / 1000)))
    return 0


def my_defended_gyms():
    """How many DISTINCT gyms this account currently has a Pokemon defending.
    The defender bonus pays per gym, not per Pokemon, exactly as it did in 2016."""
    me = current().username
    with _lock:
        return sum(1 for ms in GYMS.values()
                   if any(m.get("owner") == me for m in ms))


def defender_bonus_next_ms():
    """The timestamp (ms) at which this account may next collect the Shop defender
    bonus. In the past (or now) => collectable right now; the client greys the
    shield until this moment. A never-collected account (LAST_DEFENDER_BONUS = 0)
    lands in 1970, so the shield is live from the first login."""
    cooldown_ms = _cfg.get("gyms", "defender_bonus_cooldown_hours", cast=float) * 3_600_000
    return int(current().LAST_DEFENDER_BONUS or 0) + int(cooldown_ms)


def collect_defender_bonus():
    """The Shop's shield: coins + stardust for every gym you're holding, once a
    day. Returns (result, coins, stardust, gyms):
      result 1=SUCCESS 3=TOO_SOON 4=NO_DEFENDERS   (matches the client's enum).
    On SUCCESS the coins/stardust are already banked and the cooldown is armed."""
    coin_each = _cfg.get("gyms", "defender_bonus_coins", cast=int)
    dust_each = _cfg.get("gyms", "defender_bonus_stardust", cast=int)
    cap = _cfg.get("gyms", "defender_bonus_max_gyms", cast=int)
    cooldown_ms = _cfg.get("gyms", "defender_bonus_cooldown_hours", cast=float) * 3_600_000
    now = int(time.time() * 1000)
    p = current()
    gyms = min(my_defended_gyms(), max(1, cap))
    if gyms <= 0:
        return 4, 0, 0, 0                              # NO_DEFENDERS
    if now - int(p.LAST_DEFENDER_BONUS or 0) < cooldown_ms:
        return 3, 0, 0, gyms                           # TOO_SOON
    coins, dust = coin_each * gyms, dust_each * gyms
    with _lock:
        p.COINS += coins
        p.STARDUST += dust
        p.LAST_DEFENDER_BONUS = now
    p.save()
    return 1, coins, dust, gyms


def repair_duplicate_uids():
    """Give every Pokemon an id no OTHER trainer's Pokemon has.

    Until 2026-10-03 two trainers who caught the same shared spawn got the SAME id.
    Gym battles cannot tell two Pokemon with one id apart: once one copy was
    defending, the other trainer's copy counted as "the defender" and could not
    fight it -- the next battle of the run answered "no Pokemon fit to fight" and
    the game threw you out. Runs once at start-up, before any trainer is loaded.
    The copy defending a gym keeps its id (else the earliest catch does); every
    other copy gets a fresh one. Its CP is unchanged, but its IVs, moves and size
    are re-rolled, as they come from the id. Returns how many were changed."""
    if _players:
        return 0                            # trainers already loaded: their saves are live
    try:
        names = [f for f in os.listdir(SAVES_DIR) if f.endswith(".json")]
    except OSError:
        return 0
    saves, where = {}, {}
    for f in names:
        path = os.path.join(SAVES_DIR, f)
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not isinstance(d.get("caught"), list):
            continue
        saves[path] = d
        for i, c in enumerate(d["caught"]):
            if isinstance(c, dict) and c.get("uid"):
                where.setdefault(int(c["uid"]), []).append((path, i))
    defending = {(m.get("owner"), int(m["uid"])) for ms in GYMS.values() for m in ms}
    every = set(where)
    touched, fixed = set(), 0
    for uid, spots in where.items():
        if len({p for p, _ in spots}) < 2:
            continue

        def _rank(spot):
            d = saves[spot[0]]
            return (0 if (d.get("username"), uid) in defending else 1,
                    int(d["caught"][spot[1]].get("caught_ms") or 0))
        spots = sorted(spots, key=_rank)
        seen = {spots[0][0]}
        for path, i in spots[1:]:
            if path in seen:
                continue                    # the same trainer twice: left alone
            seen.add(path)
            d = saves[path]
            new = _fresh_uid(every | {int(k) for k in (d.get("deleted") or {})})
            every.add(new)
            d["caught"][i]["uid"] = new
            # Tell the client the old id is gone, or it keeps a ghost of it.
            d.setdefault("deleted", {})[str(uid)] = int(time.time() * 1000)
            for ms in GYMS.values():
                for m in ms:
                    if int(m["uid"]) == uid and m.get("owner") == d.get("username"):
                        m["uid"] = new
            touched.add(path)
            fixed += 1
    for path in touched:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(saves[path], fh, indent=1)
            os.replace(tmp, path)
        except OSError:
            pass
    if fixed:
        save_gyms()
        print(f"[world] gave {fixed} Pokemon a new id (it was shared with another "
              f"trainer's Pokemon)", flush=True)
    return fixed


load_gyms()
load_raid()
load_lures()
try:
    repair_duplicate_uids()
except Exception as _e:                     # never stop the server over a repair
    print(f"[world] duplicate-id repair skipped: {_e}", flush=True)
