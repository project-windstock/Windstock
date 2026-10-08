"""
Windstock anti-cheat: a speed lock, the way the 2016 game had one.

Above a set speed, three things stop working until the trainer slows down:

    spinning PokeStops    anticheat.spin_max_mph   (default 35)  -> "try again later"
    catching Pokemon      anticheat.catch_max_mph  (default 35)  -> the ball breaks free
    walking distance      anticheat.walk_max_mph   (default 15)  -> eggs and km don't count

Speed is measured from the positions the game reports with every request:
distance over time across the last minute (observe(), called from rpc.handle),
so GPS jitter between two fixes a second apart cannot trip it, and a jump
across town keeps the lock on for about a minute afterwards.

Spoofers are NOT speed-locked: a trainer whose Windstock GPS panel
(work/mods/ios-gps, android-035 lib/gps.js) says spoofing is on plays
normally. They are MARKED instead -- the panel reports itself to /shiny/gps,
the save gets a "spoof" mark, the leaderboard shows it beside their name, and
their GPS tab turns into the Windstock W on blue. The mark stays until an
admin clears it.

    save field  "spoof": {"since": ms, "last": ms, "how": "gps panel", "n": count}
"""
import math
import os
import threading
import time
from collections import deque

_lock = threading.Lock()
_track = {}           # trainer -> deque of (seconds, lat, lng), the last WINDOW seconds
_panel = {}           # trainer -> seconds when the GPS panel last said "spoofing on"

WINDOW = 60           # seconds of positions the speed is measured over
MIN_SPAN = 10         # ...but never over less than this (two fixes 1 s apart jitter)
PANEL_FRESH = 150     # the panel repeats every 60 s; this long without one = off
MPH = 1.609344        # km/h per mph

DEFAULT_MPH = {"spin": 35.0, "catch": 35.0, "walk": 15.0}


def _setting(key, default):
    try:
        import settings
        v = settings.get("anticheat", key)
        return default if v is None else v
    except Exception:
        return default


def enabled():
    # Not on the server built into a phone launcher: that one is the player's own.
    return bool(_setting("enabled", True)) and not os.environ.get("WS_SERVER_DIR")


def _metres(a, b):
    dlat = (b[0] - a[0]) * 111_320.0
    dlng = (b[1] - a[1]) * 111_320.0 * math.cos(math.radians((a[0] + b[0]) / 2.0))
    return math.hypot(dlat, dlng)


def observe(username, lat, lng, now=None):
    """One reported position (every request carries one)."""
    if not username:
        return
    now = time.time() if now is None else now
    with _lock:
        q = _track.get(username)
        if q is None:
            q = _track[username] = deque()
        if q and abs(q[-1][1] - lat) < 1e-7 and abs(q[-1][2] - lng) < 1e-7:
            return                                      # the same fix again
        q.append((now, lat, lng))
        while len(q) > 2 and now - q[1][0] > WINDOW:
            q.popleft()


def speed_kmh(username, now=None):
    """How fast this trainer is going, km/h (0 when unknown or standing still)."""
    now = time.time() if now is None else now
    with _lock:
        q = list(_track.get(username) or ())
    if len(q) < 2:
        return 0.0
    last = q[-1]
    if now - last[0] > WINDOW:
        return 0.0                                      # not moved for a minute
    # the oldest fix in the window that is at least MIN_SPAN before the newest
    first = next((p for p in q if last[0] - p[0] >= MIN_SPAN), q[-2])
    dt = max(1.0, last[0] - first[0])
    return _metres((first[1], first[2]), (last[1], last[2])) / dt * 3.6


def limit_mph(kind):
    try:
        return float(_setting(f"{kind}_max_mph", DEFAULT_MPH[kind]))
    except (TypeError, ValueError):
        return DEFAULT_MPH[kind]


def too_fast(username, kind):
    """True when `kind` ("spin", "catch", "walk") is locked for this trainer right
    now. Never for a trainer spoofing with the Windstock GPS panel."""
    if not username or not enabled():
        return False
    lim = limit_mph(kind)
    if lim <= 0 or spoofing_now(username):
        return False
    return speed_kmh(username) > lim * MPH


def report_gps(username, on):
    """The GPS panel's own report (/shiny/gps). on=True marks the trainer."""
    if not username:
        return False
    with _lock:
        if on:
            _panel[username] = time.time()
        else:
            _panel.pop(username, None)
    if on:
        mark(username, "gps panel")
    return bool(on)


def spoofing_now(username):
    """True while this trainer's GPS panel says spoofing is on."""
    with _lock:
        t = _panel.get(username)
    return bool(t and time.time() - t < PANEL_FRESH)


def mark(username, how):
    """Put the spoofer mark on a save (or refresh its "last" time)."""
    import world
    now = int(time.time() * 1000)
    try:
        with world.acting_as(username) as p:
            first = not p.SPOOF
            s = dict(p.SPOOF or {})
            s.setdefault("since", now)
            s["last"] = now
            s["n"] = int(s.get("n") or 0) + 1
            s["how"] = how
            p.SPOOF = s
            p.save()
        if first:
            print(f"[anticheat] {username} marked as a spoofer ({how})", flush=True)
        return True
    except Exception as e:
        print(f"[anticheat] could not mark {username}: {e}", flush=True)
        return False


def clear(username):
    """Admin: take the mark off."""
    import world
    with _lock:
        _panel.pop(username, None)
    try:
        with world.acting_as(username) as p:
            had = bool(p.SPOOF)
            p.SPOOF = {}
            p.save()
        if had:
            print(f"[anticheat] {username}: spoofer mark cleared", flush=True)
        return had
    except Exception:
        return False
