"""
Live event / spawn configuration for the PoGO private server.

A tiny JSON file (events.json, next to this module) holds the current "event"
settings — spawn density, which species appear, CP range, shiny rate, etc. It is
hot-reloaded on every read (by mtime) so changes made from the web control panel
(admin.py) take effect immediately, no server restart needed.

protocol.build_get_map_objects_response() calls get() on every map refresh.
"""
import json
import os
import time
import threading

import sys as _sys

import datadir

HERE = datadir.ensure()                           # <exe folder>/data
EVENTS_FILE = os.path.join(HERE, "events.json")

DEFAULTS = {
    "event_name": "Normal",
    "spawn_density": 5,         # wild Pokemon clustered around the player.
                            # Real PoGO shows a handful; 60 is the hard cap.
                            # (Cells around you each add ~1 more, spread out.)
    "species_mode": "all",      # "all" | "list" | "single"
    "species_list": [1, 4, 7, 25, 133, 143],
    "single_species": 25,       # Pikachu
    "min_cp": 100,
    "max_cp": 1200,
    "shiny_rate": 0.0,          # 0..1  (NOTE: the 0.29 client can't render shinies)
    # Normally wild CP is capped at what each species can naturally reach
    # (spawns.cap_cp_to_species). An event that hands out deliberately overpowered
    # Pokemon sets this True so its min/max_cp win over the natural ceiling.
    "allow_overcap": False,
    # XP multiplier for everyone while this event runs (stacks with a Lucky Egg). Above 1,
    # patched iOS clients show it as a buff on the map -- the Windstock "W" in the Lucky
    # Egg's spot, with xp_bonus_label (default "2x XP") where the timer would be.
    "xp_bonus": 1,
    "xp_bonus_label": "",
    # A bonus set by hand can end on its own: "YYYY-MM-DDTHH:MM" local time, "" = no end.
    # xp_bonus_from_ms is when it was switched on (set by save), where the ring starts.
    "xp_bonus_until": "",
    "xp_bonus_from_ms": 0,
    # More event bonuses, each shown as its own buff beside the XP one on patched iOS
    # clients (icon + label). candy_bonus multiplies the candy from catches, hatches and
    # transfers; shiny_bonus multiplies the shiny chance; boost_species get boost_mult x
    # their normal share of wild spawns (their buff reads boost_label).
    "candy_bonus": 1,
    "candy_bonus_label": "",
    "shiny_bonus": 1,
    "shiny_bonus_label": "",
    "boost_species": [],
    "boost_mult": 1,
    "boost_share": 0,               # 0..1: that share of ALL wild spawns is a boost species
    "boost_label": "",
    "boost_icon": "",               # "ghost" = the Halloween ghost; "" = the Windstock W
    # A look for the whole event on patched clients: "" = none, "halloween" = the Hub's
    # spooky banner and a purple night tint over the map.
    "theme": "",
    # false = the Hub banner and the theme's music at night, but no forced night, fog or
    # tint on the map (Halloween's opening days; the finale turns the full look on).
    "theme_look": True,
    # The built-in yearly events (SEASONAL, below) run on their own unless this is false.
    "seasonal": True,
    # ...but not on the server built into a phone launcher: Halloween belongs to the
    # online server. true = the phone's own server runs them too.
    "seasonal_on_phone": False,
    # Scheduled and seasonal events follow each PLAYER's clock, not the server's: a
    # "11:00-14:00" event runs 11-2 wherever they are, and Halloween starts at their
    # midnight. false = everyone gets the server's clock (one moment for all).
    "local_time": True,
}

# Halloween (Windstock: Oct 20 - Nov 1; 2016 ran Oct 26 - Nov 1), in two parts, with
# "Dark&Shadow" (Gastly, Zubat, Drowzee, Cubone and their evolutions everywhere) throughout:
#   the start  -- 1.5x XP and 1.5x candy, the Halloween music at night;
#   the finale -- Halloween weekend: 2x XP, 2x candy, 2x shiny, twice as many Pokemon,
#                 and the full look: night all day, so the Halloween music all day.
HALLOWEEN_SPECIES = [41, 42, 92, 93, 94, 96, 97, 104, 105]
HALLOWEEN_DENSITY = 10          # wild Pokemon around the trainer (normally 5)

# One-click events for the menu.
PRESETS = {
    "Normal":           {"event_name": "Normal", "spawn_density": 5, "species_mode": "all",
                         "min_cp": 100, "max_cp": 1200, "shiny_rate": 0.0, "xp_bonus": 1,
                         "candy_bonus": 1, "shiny_bonus": 1, "boost_mult": 1, "theme": "",
                         "theme_look": True},
    "Double XP":        {"event_name": "Double XP", "xp_bonus": 2, "xp_bonus_until": ""},
    "Swarm":      {"event_name": "Swarm", "spawn_density": 25, "species_mode": "all",
                         "min_cp": 100, "max_cp": 1500, "shiny_rate": 0.0},
    "Pikachu Festival": {"event_name": "Pikachu Festival", "spawn_density": 10, "species_mode": "single",
                         "single_species": 25, "min_cp": 300, "max_cp": 1000, "shiny_rate": 0.0},
    "Starter Party":    {"event_name": "Starter Party", "spawn_density": 8, "species_mode": "list",
                         "species_list": [1, 4, 7], "min_cp": 200, "max_cp": 1200, "shiny_rate": 0.0},
    "Legendary Hunt":   {"event_name": "Legendary Hunt", "spawn_density": 4, "species_mode": "list",
                         "species_list": [144, 145, 146, 150, 151], "min_cp": 2000, "max_cp": 3500,
                         "shiny_rate": 0.0, "allow_overcap": True},
    "High CP":          {"event_name": "High CP", "spawn_density": 5, "species_mode": "all",
                         "min_cp": 2500, "max_cp": 4000, "shiny_rate": 0.0, "allow_overcap": True},
    "Halloween":        {"event_name": "Halloween", "species_mode": "all",
                         "xp_bonus": 1.5, "xp_bonus_label": "1.5x XP",
                         "candy_bonus": 1.5, "candy_bonus_label": "1.5x Candy",
                         "boost_species": HALLOWEEN_SPECIES, "boost_mult": 4,
                         "boost_label": "Dark&Shadow", "boost_icon": "ghost",
                         "theme": "halloween", "theme_look": False},
    "Halloween Finale": {"event_name": "Halloween Finale", "species_mode": "all",
                         "spawn_density": HALLOWEEN_DENSITY,
                         "xp_bonus": 2, "xp_bonus_label": "2x XP",
                         "candy_bonus": 2, "candy_bonus_label": "2x Candy",
                         "shiny_bonus": 2, "shiny_bonus_label": "2x Shiny",
                         "boost_species": HALLOWEEN_SPECIES, "boost_mult": 4,
                         "boost_label": "Dark&Shadow", "boost_icon": "ghost",
                         "theme": "halloween", "theme_look": True},
}

# Built-in yearly events, on every server without anyone scheduling them (events.json
# "seasonal": false turns them off). Same shape as a scheduled row, with month-day
# "from"/"to"; a row saved in the World Manager's schedule wins over these.
SEASONAL = [
    {"name": "Halloween", "preset": "Halloween", "start": "00:00", "end": "24:00",
     "from": "10-20", "to": "10-29"},
    {"name": "Halloween Finale", "preset": "Halloween Finale", "start": "00:00", "end": "24:00",
     "from": "10-30", "to": "11-01"},
]

# The Windstock calendar: a weekly rotation that runs on the public server by itself, so
# there is always something on or coming (events.json "weekly": false turns it off; like
# SEASONAL it stays off the server inside a phone launcher). Times are each trainer's own
# clock (local_time). SEASONAL rows come after these and so win over them (Halloween).
#   Tuesday     18:00-19:00  Spotlight Hour  -- one species everywhere, 2x candy (rotates weekly)
#   Wednesday   18:00-19:00  Swarm Hour      -- four times the usual wild Pokemon
#   Thursday    18:00-19:00  Shiny Hour      -- 3x shiny odds
#   Sat + Sun   all day      Weekend Bonus   -- 1.5x XP
#   3rd Sunday  14:00-17:00  Community Day   -- one species, 3x candy, 4x shiny, 2x XP (rotates monthly)
SPOTLIGHT = [25, 133, 147, 10, 129, 92, 66, 60, 16, 43, 74, 81, 116, 27, 37, 58, 63, 111,
             19, 41, 50, 54, 77, 88, 96, 100, 104, 109, 120, 138, 140]
COMMUNITY_DAY = [1, 4, 7, 25, 147, 133, 129, 63, 66, 92, 74, 143]   # by month, Jan..Dec
WEEKLY_AHEAD_DAYS = 14

_NAMES = None


def _species_name(pid):
    global _NAMES
    if _NAMES is None:
        try:
            import helpcenter
            _NAMES = {i: n for i, n in enumerate(helpcenter._DEX) if n}
        except Exception:
            _NAMES = {}
    return _NAMES.get(int(pid)) or f"#{pid}"


def weekly(now=None):
    """The Windstock calendar (see above) as dated rows from yesterday to two weeks out."""
    import time as _time
    try:
        saved = _read_file()
        if not saved.get("weekly", True):
            return []
        if os.environ.get("WS_SERVER_DIR"):
            return []
    except Exception:
        pass
    now = now or _time.time()
    lt = local(now)
    base = day_start(lt.tm_year, lt.tm_mon, lt.tm_mday)
    out = []
    for i in range(-1, WEEKLY_AHEAD_DAYS + 1):
        t = _time.gmtime(base + i * 86400 + 12 * 3600)          # noon: safe from DST edges
        day = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        wd = t.tm_wday
        week = int(_time.strftime("%V", t))

        def row(name, start, end, cfg, to=None):
            out.append({"name": name, "start": start, "end": end, "from": day,
                        "to": to or day, "cfg": cfg, "weekly": True})

        if wd == 1:
            sp = SPOTLIGHT[week % len(SPOTLIGHT)]
            row(f"Spotlight Hour: {_species_name(sp)}", "18:00", "19:00",
                {"event_name": "Spotlight Hour", "boost_species": [sp], "boost_mult": 12,
                 "boost_share": 1, "spawn_density": 12,
                 "boost_label": "Spotlight", "candy_bonus": 2, "candy_bonus_label": "2x Candy"})
        elif wd == 2:
            row("Swarm Hour", "18:00", "19:00",
                {"event_name": "Swarm Hour", "spawn_density": 20})
        elif wd == 3:
            row("Shiny Hour", "18:00", "19:00",
                {"event_name": "Shiny Hour", "shiny_bonus": 3, "shiny_bonus_label": "3x Shiny"})
        elif wd == 5:
            nxt = _time.gmtime(base + (i + 1) * 86400 + 12 * 3600)
            row("Weekend Bonus", "00:00", "24:00",
                {"event_name": "Weekend Bonus", "xp_bonus": 1.5, "xp_bonus_label": "1.5x XP"},
                to=f"{nxt.tm_year:04d}-{nxt.tm_mon:02d}-{nxt.tm_mday:02d}")
        if wd == 6 and 15 <= t.tm_mday <= 21:                    # the third Sunday
            sp = COMMUNITY_DAY[(t.tm_mon - 1) % len(COMMUNITY_DAY)]
            row(f"Community Day: {_species_name(sp)}", "14:00", "17:00",
                {"event_name": "Community Day", "boost_species": [sp], "boost_mult": 15,
                 "boost_label": _species_name(sp)[:12], "spawn_density": 12, "boost_share": 1,
                 "candy_bonus": 3, "candy_bonus_label": "3x Candy",
                 "shiny_bonus": 4, "shiny_bonus_label": "4x Shiny",
                 "xp_bonus": 2, "xp_bonus_label": "2x XP"})
            out[-1].pop("weekly")                                # a big one: always listed
    return out


_lock = threading.Lock()
_cache = {"mtime": None, "cfg": None}


# ---------------------------------------------------------------- whose clock
# A public server sits in one timezone (the VPS: UTC) and its players in others. The
# phone reports its UTC offset (tweak v158+, "X-WS-TZ" on /shiny/state); without one
# the trainer's longitude gives it to the nearest hour, the way biomes.is_night does.
# The server inside the phone is unaffected -- its clock IS the player's.
_TZ = {}                        # username (lowercase) -> seconds east of UTC
_who = threading.local()        # .user: the trainer an HTTP request is for


def for_user(username):
    """Name the trainer this request is for (rpc requests need not: world.use() did)."""
    _who.user = username or None


def set_tz(username, seconds):
    """Remember a phone's own UTC offset, in seconds (None/garbage is ignored)."""
    try:
        sec = int(seconds)
    except (TypeError, ValueError):
        return
    if username and -14 * 3600 <= sec <= 14 * 3600:
        _TZ[str(username).lower()] = sec


def tz_offset():
    """Seconds east of UTC for the trainer being served, or None for the server's clock."""
    try:
        cfg = _cache["cfg"] if _cache["cfg"] is not None else _read_file()
        if not cfg.get("local_time", DEFAULTS["local_time"]):
            return None
        import world
        name = getattr(_who, "user", None)
        player = getattr(world._current, "player", None)
        if not name and player is not None:
            name = player.username
        if not name:
            return None
        if name.lower() in _TZ:
            return _TZ[name.lower()]
        loc = world.PLAYER_LOC.get(name)
        if not loc and player is not None and player.username == name:
            loc = getattr(player, "LAST_SEEN", None)
        if loc:
            return int(round(float(loc[1]) / 15.0)) * 3600
    except Exception:
        pass
    return None


def local(now=None):
    """struct_time of `now` on the clock events run by (see tz_offset)."""
    now = time.time() if now is None else now
    off = tz_offset()
    return time.localtime(now) if off is None else time.gmtime(now + off)


def day_start(year, month, day):
    """Epoch seconds of that date's midnight on the same clock."""
    off = tz_offset()
    if off is None:
        return time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
    import calendar
    return calendar.timegm((year, month, day, 0, 0, 0)) - off


def date_start(text):
    """'YYYY-MM-DD' -> epoch seconds of its midnight (ValueError if it is not a date)."""
    t = time.strptime(text, "%Y-%m-%d")
    return day_start(t.tm_year, t.tm_mon, t.tm_mday)


_raw = {"key": None, "data": {}}
_raw_lock = threading.Lock()


def _read_file():
    """events.json as saved (a fresh top-level copy the caller may change).
    Re-parsed only when the file changes: schedule() is asked on every get(),
    and one map refresh calls get() ~18 times -- 18 opens + parses of the file."""
    try:
        st = os.stat(EVENTS_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _raw_lock:
        if _raw["key"] != key:
            try:
                with open(EVENTS_FILE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                return {}                  # caught mid-write: try again next time
            _raw["key"], _raw["data"] = key, data if isinstance(data, dict) else {}
        return dict(_raw["data"])


def get():
    """Current config (DEFAULTS merged with events.json), hot-reloaded by mtime.
    A scheduled event (see set_schedule) overrides the saved one while it runs, so
    double-XP hours, spawn surges and community days need nobody at the keyboard."""
    with _lock:
        try:
            m = os.path.getmtime(EVENTS_FILE)
        except OSError:
            m = None
        if _cache["cfg"] is None or m != _cache["mtime"]:
            cfg = dict(DEFAULTS)
            cfg.update(_read_file())
            _cache["cfg"] = _sanitize(cfg)
            _cache["mtime"] = m
        cfg = dict(_cache["cfg"])
    row = active_scheduled()
    if row:
        over = dict(PRESETS.get(row.get("preset"), {}))
        over.update(row.get("cfg") or {})
        if over:
            cfg.update(over)
            cfg["event_name"] = row.get("name") or over.get("event_name", cfg.get("event_name"))
            cfg = _sanitize(cfg)
            cfg["scheduled"] = row.get("name", True)
    return cfg


def save(cfg):
    """Merge + validate + persist a (full or partial) config; returns the result."""
    merged = dict(DEFAULTS)
    old = _sanitize(dict(DEFAULTS, **_read_file()))
    merged.update(old)
    merged.update(cfg or {})
    merged = _sanitize(merged)
    # the ring starts when the bonus (or its end) last changed, not on every save
    if merged["xp_bonus"] > 1 and (merged["xp_bonus"] != old["xp_bonus"] or
                                   merged["xp_bonus_until"] != old["xp_bonus_until"] or
                                   not merged.get("xp_bonus_from_ms")):
        import time as _time
        merged["xp_bonus_from_ms"] = int(_time.time() * 1000)
    with _lock:
        with open(EVENTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
        _cache["cfg"] = None            # force reload on next get()
    return merged


def apply_preset(name):
    p = PRESETS.get(name)
    return save(p) if p else None


def _hhmm(v, default):
    """'HH:MM' -> minutes since midnight."""
    try:
        h, m = str(v).split(":")
        return max(0, min(24 * 60, int(h) * 60 + int(m)))
    except Exception:
        return default


def schedule():
    """The saved list of scheduled events (see set_schedule)."""
    cfg = _read_file() or {}
    rows = cfg.get("schedule")
    return ([dict(r) for r in rows if isinstance(r, dict) and not _old_halloween(r)]
            if isinstance(rows, list) else [])


def _old_halloween(row):
    """The one-part Halloween row older saves carry (Oct 20 - Nov 1, the "Halloween"
    preset all day). It would sit on top of the two-part SEASONAL event and run the
    opening bonuses to the end, so it is passed over and SEASONAL takes its place."""
    return (row.get("preset") == "Halloween" and not row.get("cfg") and not row.get("days")
            and str(row.get("from", ""))[5:] == "10-20" and str(row.get("to", ""))[5:] == "11-01"
            and _hhmm(row.get("start"), 0) == 0 and _hhmm(row.get("end"), 1440) >= 1439)


def set_schedule(rows):
    """Replace the schedule. Each row:
        {"name": "Community Day", "preset": "Pikachu Festival",
         "days": [5, 6],            # weekday numbers, Mon=0; [] or missing = every day
         "start": "11:00", "end": "14:00",
         "from": "2026-09-01", "to": "2026-12-31"}   # optional date window
    A row may carry "cfg": {...} instead of "preset" to set values directly."""
    clean = []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        row = {"name": str(r.get("name", "Event"))[:40],
               "start": str(r.get("start", "00:00"))[:5],
               "end": str(r.get("end", "23:59"))[:5],
               "days": [int(d) % 7 for d in (r.get("days") or []) if str(d).isdigit()],
               "enabled": bool(r.get("enabled", True))}
        if r.get("preset") in PRESETS:
            row["preset"] = r["preset"]
        if isinstance(r.get("cfg"), dict):
            row["cfg"] = r["cfg"]
        for k in ("from", "to"):
            if r.get(k):
                row[k] = str(r[k])[:10]
        clean.append(row)
    cfg = _read_file() or {}
    cfg["schedule"] = clean
    with _lock:
        with open(EVENTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
        _cache["mtime"] = None
    return clean


def secret_preset(name):
    """Kept out of the iPhone app's World Manager (admin.py shows it only on the Mac):
    presets that hand out XP, candy or shiny bonuses, or are a seasonal surprise."""
    p = PRESETS.get(name) or {}
    return (_mult(p.get("xp_bonus")) > 1 or _mult(p.get("candy_bonus")) > 1 or
            int(p.get("shiny_bonus", 1) or 1) > 1 or bool(p.get("theme")) or
            any(r.get("preset") == name for r in SEASONAL))


def seasonal(now=None):
    """The weekly calendar's rows, then SEASONAL as dated rows for this year (and next,
    so late-December lookups see January's) -- or [] when events.json turns them off."""
    import time as _time
    try:
        saved = _read_file()
        if not saved.get("seasonal", DEFAULTS["seasonal"]):
            return []
        # the server inside a phone launcher (ws_boot sets WS_SERVER_DIR)
        if os.environ.get("WS_SERVER_DIR") and not saved.get("seasonal_on_phone",
                                                             DEFAULTS["seasonal_on_phone"]):
            return []
    except Exception:
        pass
    year = local(now or _time.time()).tm_year
    out = weekly(now)
    for y in (year, year + 1):
        for r in SEASONAL:
            row = dict(r, seasonal=True)
            row["from"], row["to"] = f"{y}-{r['from']}", f"{y}-{r['to']}"
            out.append(row)
    return out


def active_scheduled(now=None):
    """The scheduled row running right now, or None. Later rows win, so a special
    one-off can sit on top of a weekly slot -- and anything scheduled by hand sits on
    top of the built-in seasonal events."""
    import time as _time
    lt = local(now or _time.time())
    minute = lt.tm_hour * 60 + lt.tm_min
    today = _time.strftime("%Y-%m-%d", lt)
    hit = None
    for row in seasonal(now) + schedule():
        if not row.get("enabled", True):
            continue
        if row.get("days") and lt.tm_wday not in row["days"]:
            continue
        if row.get("from") and today < row["from"]:
            continue
        if row.get("to") and today > row["to"]:
            continue
        start = _hhmm(row.get("start"), 0)
        end = _hhmm(row.get("end"), 24 * 60)
        inside = (start <= minute < end) if start <= end else (minute >= start or minute < end)
        if inside:
            hit = row
    return hit


def _clampi(v, lo, hi, d):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = d
    return max(lo, min(hi, v))


def _mult(v, hi=10):
    """A bonus multiplier, 1..hi: whole ones stay ints (2), the rest keep two decimals (1.5)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1
    v = round(max(1.0, min(float(hi), v)), 2) if v == v else 1
    return int(v) if v == int(v) else v


def _sanitize(c):
    c["spawn_density"] = _clampi(c.get("spawn_density"), 0, 60, 5)
    c["min_cp"] = _clampi(c.get("min_cp"), 10, 5000, 100)
    c["max_cp"] = _clampi(c.get("max_cp"), 10, 5000, 1200)
    if c["max_cp"] < c["min_cp"]:
        c["max_cp"] = c["min_cp"]
    if c.get("species_mode") not in ("all", "list", "single"):
        c["species_mode"] = "all"
    c["single_species"] = _clampi(c.get("single_species"), 1, 151, 25)
    clean = []
    for x in c.get("species_list") or []:
        try:
            xi = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= xi <= 151:
            clean.append(xi)
    c["species_list"] = clean or [25]
    try:
        c["shiny_rate"] = max(0.0, min(1.0, float(c.get("shiny_rate", 0.0))))
    except (TypeError, ValueError):
        c["shiny_rate"] = 0.0
    c["allow_overcap"] = bool(c.get("allow_overcap", False))
    c["event_name"] = str(c.get("event_name", "Event"))[:40]
    c["xp_bonus"] = _mult(c.get("xp_bonus"))
    c["xp_bonus_label"] = str(c.get("xp_bonus_label") or "")[:12]
    c["xp_bonus_until"] = str(c.get("xp_bonus_until") or "")[:16]
    if _until_ms(c["xp_bonus_until"]) is None:
        c["xp_bonus_until"] = ""
    try:
        c["xp_bonus_from_ms"] = max(0, int(c.get("xp_bonus_from_ms") or 0))
    except (TypeError, ValueError):
        c["xp_bonus_from_ms"] = 0
    c["candy_bonus"] = _mult(c.get("candy_bonus"))
    c["shiny_bonus"] = _clampi(c.get("shiny_bonus"), 1, 50, 1)
    c["boost_mult"] = _clampi(c.get("boost_mult"), 1, 50, 1)
    boost = []
    for x in c.get("boost_species") or []:
        try:
            xi = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= xi <= 151 and xi not in boost:
            boost.append(xi)
    c["boost_species"] = boost
    try:
        c["boost_share"] = max(0.0, min(1.0, float(c.get("boost_share") or 0)))
    except (TypeError, ValueError):
        c["boost_share"] = 0.0
    for k in ("candy_bonus_label", "shiny_bonus_label", "boost_label"):
        c[k] = str(c.get(k) or "")[:12]
    c["theme"] = str(c.get("theme") or "")[:20]
    c["theme_look"] = bool(c.get("theme_look", True))
    c["local_time"] = bool(c.get("local_time", True))
    c["seasonal_on_phone"] = bool(c.get("seasonal_on_phone", False))
    c["boost_icon"] = str(c.get("boost_icon") or "")[:20]
    return c


def _until_ms(v):
    """'YYYY-MM-DDTHH:MM' (local, what <input type=datetime-local> gives) -> epoch ms."""
    if not v:
        return None
    import time as _time
    try:
        return int(_time.mktime(_time.strptime(str(v)[:16], "%Y-%m-%dT%H:%M")) * 1000)
    except (ValueError, OverflowError):
        return None


def xp_bonus():
    """(multiplier, label) of the event-wide XP bonus; (1, "") when there is none."""
    try:
        c = get()
    except Exception:
        return 1, ""
    m = _mult(c.get("xp_bonus"))
    if m > 1 and not _scheduled_window():
        until = _until_ms(c.get("xp_bonus_until"))
        if until is not None and time.time() * 1000 >= until:
            m = 1                                        # set by hand, and its end has passed
    return m, (c.get("xp_bonus_label") or f"{m}x XP") if m > 1 else ""


def xp_bonus_window(now=None):
    """(start_ms, end_ms) of the running XP bonus -- the buff's ring on the phone runs
    down over it. A scheduled row's window, else a hand-set bonus's from/until; None when
    it has no end (the ring stays full)."""
    win = _scheduled_window(now)
    if win:
        return win
    c = get()
    until = _until_ms(c.get("xp_bonus_until"))
    if _mult(c.get("xp_bonus")) <= 1 or until is None or until <= time.time() * 1000:
        return None
    start = int(c.get("xp_bonus_from_ms") or 0) or until - 3600 * 1000
    return (min(start, until - 1000), until)


def _scheduled_window(now=None):
    """(start_ms, end_ms) when a scheduled row is what turned the XP bonus on, else None."""
    row = active_scheduled(now)
    if not row:
        return None
    over = dict(PRESETS.get(row.get("preset"), {}))
    over.update(row.get("cfg") or {})
    if _mult(over.get("xp_bonus")) <= 1:
        return None
    return _row_window(row, now)


def event_window(now=None):
    """(start_ms, end_ms) of the scheduled event running now, or None (set by hand: no end)."""
    row = active_scheduled(now)
    return _row_window(row, now) if row else None


def _row_window(row, now=None):
    """Today's run of a scheduled row. A row that runs all day on every day of a date
    range (Halloween: Oct 26 - Nov 1, 00:00-24:00) is one window over the whole range,
    so its buffs' rings run down over the week instead of starting again each midnight."""
    import time as _time
    now = now or _time.time()
    lt = local(now)
    midnight = day_start(lt.tm_year, lt.tm_mon, lt.tm_mday)
    minute = lt.tm_hour * 60 + lt.tm_min
    start = _hhmm(row.get("start"), 0)
    end = _hhmm(row.get("end"), 24 * 60)
    if start == 0 and end >= 23 * 60 + 59 and not row.get("days") and row.get("from") and row.get("to"):
        try:
            s = date_start(row["from"])
            e = date_start(row["to"]) + 86400
            if e > s:
                return int(s * 1000), int(e * 1000)
        except (ValueError, OverflowError):
            pass
    s_day = e_day = 0
    if start > end:                     # runs past midnight
        if minute >= start:
            e_day = 1
        else:
            s_day = -1
    s = midnight + s_day * 86400 + start * 60
    e = midnight + e_day * 86400 + end * 60
    return int(s * 1000), int(e * 1000)


def candy_mult():
    """Event candy multiplier (catches, hatches, transfers); 1 when there is none."""
    try:
        return _mult(get().get("candy_bonus"))
    except Exception:
        return 1


def shiny_mult():
    try:
        return int(get().get("shiny_bonus") or 1)
    except Exception:
        return 1


def spawn_boost():
    """({species}, multiplier) the event favours in the wild; (set(), 1) when none."""
    try:
        c = get()
    except Exception:
        return set(), 1
    m = int(c.get("boost_mult") or 1)
    sp = set(c.get("boost_species") or [])
    return (sp, m) if sp and m > 1 else (set(), 1)


def buffs():
    """Every event bonus running now, as the buffs patched iOS clients draw beside the
    Lucky Egg's: [{"id", "icon", "label", "start_ms", "end_ms"}]. icon names a sprite the
    client got from patch_badges.py (Store_Windstock_<icon>). No window = the ring stays full."""
    out = []
    try:
        c = get()
    except Exception:
        return out
    win = event_window()

    def add(bid, icon, label, w=win):
        b = {"id": bid, "icon": icon, "label": label}
        if w:
            b["start_ms"], b["end_ms"] = w
        out.append(b)

    m, label = xp_bonus()
    if m > 1:
        add("xp", "xpbonus", label, xp_bonus_window())
    m = _mult(c.get("candy_bonus"))
    if m > 1:
        add("candy", "candybonus", c.get("candy_bonus_label") or f"{m}x Candy")
    m = int(c.get("shiny_bonus") or 1)
    if m > 1:
        add("shiny", "shinybonus", c.get("shiny_bonus_label") or f"{m}x Shiny")
    if spawn_boost()[1] > 1:
        add("boost", "ghostbonus" if c.get("boost_icon") == "ghost" else "xpbonus",
            c.get("boost_label") or "Spawns up")
    return out


def _bonus_labels(c):
    """The bonuses a config hands out, worded the way its buffs are."""
    out = []
    m = _mult(c.get("xp_bonus"))
    if m > 1:
        out.append(c.get("xp_bonus_label") or f"{m}x XP")
    m = _mult(c.get("candy_bonus"))
    if m > 1:
        out.append(c.get("candy_bonus_label") or f"{m}x Candy")
    try:
        m = int(c.get("shiny_bonus") or 1)
    except (TypeError, ValueError):
        m = 1
    if m > 1:
        out.append(c.get("shiny_bonus_label") or f"{m}x Shiny")
    try:
        boosted = bool(c.get("boost_species")) and int(c.get("boost_mult") or 1) > 1
    except (TypeError, ValueError):
        boosted = False
    if boosted:
        out.append(c.get("boost_label") or "Spawns up")
    return out


SUMMARY_AHEAD = 90 * 86400          # how far ahead "coming up" looks


def summary(now=None):
    """What is on and what is coming, for a reader that is no trainer (Windbot):
        {"current": {...} | None, "upcoming": [...], "local_time": bool}
    Asked with no trainer in hand, so "now" is the server's clock; with local_time
    on, each player reaches the same dates by their own."""
    now = now or time.time()
    cfg = get()
    row = active_scheduled(now)
    labels = [b["label"] for b in buffs()]
    name = cfg.get("event_name") or "Normal"

    def dated(r, item):
        for k in ("from", "to", "start", "end"):
            if r.get(k):
                item[k] = r[k]
        if r.get("days"):
            item["days"] = list(r["days"])
        return item

    current = None
    if row or labels or name != "Normal":
        current = {"name": name if name != "Normal" else "Bonus", "bonuses": labels,
                   "scheduled": bool(row)}
        if row:
            dated(row, current)
        else:
            try:
                win = xp_bonus_window(now)
            except Exception:
                win = None
            if win:
                current["end_ms"] = win[1]

    today = time.strftime("%Y-%m-%d", local(now))
    upcoming = []
    for r in seasonal(now) + schedule():
        if not r.get("enabled", True) or r == row:
            continue
        if r.get("to") and today > r["to"]:
            continue
        if r.get("from") and today < r["from"]:
            try:
                if date_start(r["from"]) - now > SUMMARY_AHEAD:
                    continue
            except (ValueError, OverflowError):
                continue
        over = dict(PRESETS.get(r.get("preset"), {}))
        over.update(r.get("cfg") or {})
        item = dated(r, {"name": r.get("name") or over.get("event_name") or "Event",
                         "bonuses": _bonus_labels(over)})
        if r.get("weekly"):
            item["weekly"] = True
        upcoming.append(item)
    key = lambda e: (e.get("from") or today, e.get("start") or "")
    upcoming.sort(key=key)
    # the weekly calendar alone would fill the list; keep room for the big ones
    big = [e for e in upcoming if not e.get("weekly")][:3]
    upcoming = sorted(big + [e for e in upcoming if e.get("weekly")][:6 - len(big)], key=key)
    return {"current": current, "upcoming": upcoming,
            "local_time": bool(cfg.get("local_time", DEFAULTS["local_time"]))}
