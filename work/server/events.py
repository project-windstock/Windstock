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
}

# One-click events for the menu.
PRESETS = {
    "Normal":           {"event_name": "Normal", "spawn_density": 5, "species_mode": "all",
                         "min_cp": 100, "max_cp": 1200, "shiny_rate": 0.0},
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
}

_lock = threading.Lock()
_cache = {"mtime": None, "cfg": None}


def _read_file():
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


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
    merged.update(_read_file())
    merged.update(cfg or {})
    merged = _sanitize(merged)
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
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


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


def active_scheduled(now=None):
    """The scheduled row running right now, or None. Later rows win, so a special
    one-off can sit on top of a weekly slot."""
    import time as _time
    lt = _time.localtime(now or _time.time())
    minute = lt.tm_hour * 60 + lt.tm_min
    today = _time.strftime("%Y-%m-%d", lt)
    hit = None
    for row in schedule():
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
    return c
