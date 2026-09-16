"""
Real weather, 2017-style, on the 2016 client.

The 0.29/0.35 client has no weather at all, so the server brings it:
  * spawns   Pokemon whose type the weather favours appear more often
  * CP       weather-boosted wild Pokemon roll higher CP
  * stardust catching a boosted Pokemon pays extra stardust
  * the windstock tweak polls /weather to draw rain/snow/fog/wind on the map + an icon

Source: Open-Meteo (https://open-meteo.com) -- free, no account, no API key. This is
the ONE feature that talks to the outside world. Privacy: only a location ROUNDED to
0.1 degree (roughly 10 km) is ever sent, at most once per 30 minutes per area, from a
background thread so a map refresh never waits on the internet. No internet = no
weather (everything else keeps working). Turn it off with settings.json -> weather.enabled.

Conditions and boosted types follow the real 2017 game:
  sunny/clear   Grass, Ground, Fire        rain   Water, Electric, Bug
  partly cloudy Normal, Rock               snow   Ice, Steel
  cloudy        Fairy, Fighting, Poison    fog    Dark, Ghost
  windy         Dragon, Flying, Psychic
"""
import json
import threading
import time
import urllib.request

import settings as _cfg

# HoloPokemonType (same numbering as biomes.py / game_master.bin)
NORMAL, FIGHTING, FLYING, POISON, GROUND, ROCK, BUG, GHOST, STEEL = range(1, 10)
FIRE, WATER, GRASS, ELECTRIC, PSYCHIC, ICE, DRAGON, DARK, FAIRY = range(10, 19)
TYPE_NAMES = {NORMAL: "Normal", FIGHTING: "Fighting", FLYING: "Flying", POISON: "Poison",
              GROUND: "Ground", ROCK: "Rock", BUG: "Bug", GHOST: "Ghost", STEEL: "Steel",
              FIRE: "Fire", WATER: "Water", GRASS: "Grass", ELECTRIC: "Electric",
              PSYCHIC: "Psychic", ICE: "Ice", DRAGON: "Dragon", DARK: "Dark", FAIRY: "Fairy"}

# condition -> (label, icon, boosted types)
CONDITIONS = {
    "sunny":         ("Sunny",         "☀️", (GRASS, GROUND, FIRE)),
    "clear":         ("Clear",         "🌙", (GRASS, GROUND, FIRE)),     # sunny, at night
    "partly_cloudy": ("Partly cloudy", "⛅", (NORMAL, ROCK)),
    "cloudy":        ("Cloudy",        "☁️", (FAIRY, FIGHTING, POISON)),
    "rainy":         ("Rain",          "🌧️", (WATER, ELECTRIC, BUG)),
    "snow":          ("Snow",          "❄️", (ICE, STEEL)),
    "fog":           ("Fog",           "🌫️", (DARK, GHOST)),
    "windy":         ("Windy",         "💨", (DRAGON, FLYING, PSYCHIC)),
}

CACHE_S = 30 * 60          # refetch an area at most every 30 minutes
RETRY_S = 5 * 60           # ...or 5 minutes after a failed fetch

_lock = threading.Lock()
_CACHE = {}                # (lat 0.1deg, lng 0.1deg) -> {"at": epoch s, "data": dict|None}
_PENDING = set()
_last_error = [None]


def _key(lat, lng):
    return (round(float(lat), 1), round(float(lng), 1))


def classify(code, wind_kmh, is_day):
    """WMO weather code (+ wind) -> one of CONDITIONS."""
    code = int(code)
    if code in (45, 48):
        cond = "fog"
    elif 71 <= code <= 77 or code in (85, 86):
        cond = "snow"
    elif 51 <= code <= 67 or 80 <= code <= 82 or code >= 95:
        cond = "rainy"
    elif code == 3:
        cond = "cloudy"
    elif code == 2:
        cond = "partly_cloudy"
    else:                                       # 0 clear sky, 1 mainly clear
        cond = "sunny" if is_day else "clear"
    # Strong wind wins over dry skies, like the real game's "Windy".
    if cond in ("sunny", "clear", "partly_cloudy", "cloudy") and \
            float(wind_kmh or 0) >= _cfg.get("weather", "windy_kmh", cast=float):
        cond = "windy"
    return cond


def _fetch(key):
    lat, lng = key
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%.1f&longitude=%.1f"
           "&current=weather_code,temperature_2m,wind_speed_10m,is_day" % (lat, lng))
    data = None
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            cur = json.loads(r.read().decode("utf-8"))["current"]
        is_day = bool(cur.get("is_day", 1))
        data = {"condition": classify(cur["weather_code"], cur.get("wind_speed_10m"), is_day),
                "code": int(cur["weather_code"]),
                "temp_c": cur.get("temperature_2m"),
                "wind_kmh": cur.get("wind_speed_10m"),
                "is_day": is_day,
                "fetched": int(time.time())}
        _last_error[0] = None
    except Exception as e:                      # offline, DNS, SSL, bad JSON...
        _last_error[0] = f"{type(e).__name__}: {e}"
    with _lock:
        _CACHE[key] = {"at": time.time(), "data": data}
        _PENDING.discard(key)


def _view(d):
    label, icon, types = CONDITIONS[d["condition"]]
    return dict(d, label=label, icon=icon, boosted_types=list(types),
                boosted_names=[TYPE_NAMES[t] for t in types])


def current(lat, lng):
    """The weather at a location, or None (off, no location, or not fetched yet).
    Never blocks: an unknown area is fetched in the background."""
    if not _cfg.get("weather", "enabled", cast=bool) or lat is None or lng is None:
        return None
    if not (abs(lat) > 1e-6 or abs(lng) > 1e-6):
        return None
    forced = str(_cfg.get("weather", "force") or "").strip().lower()
    if forced in CONDITIONS:
        return _view({"condition": forced, "temp_c": None, "is_day": True, "forced": True})
    key = _key(lat, lng)
    now = time.time()
    with _lock:
        e = _CACHE.get(key)
        stale = e is None or now - e["at"] > (CACHE_S if e["data"] else RETRY_S)
        if stale and key not in _PENDING:
            _PENDING.add(key)
            threading.Thread(target=_fetch, args=(key,), daemon=True).start()
        data = e["data"] if e else None
    return _view(data) if data else None


def boosted_types(lat, lng):
    w = current(lat, lng)
    return set(w["boosted_types"]) if w else set()


def is_boosted(pokemon_id, lat, lng):
    b = boosted_types(lat, lng)
    if not b:
        return False
    import protocol as P
    return any(t in b for t in P.pokemon_types(int(pokemon_id)))


def handle(method, path, query, headers, body, log, ip=None):
    """GET /weather -- the weather where the asking phone's trainer is (for the tweak)."""
    import rpc
    import world
    user = rpc.user_for_ip(ip)
    loc = world.player_location(user) if user else None
    w = current(*loc) if loc else None
    out = {"known": bool(w)}
    if w:
        out.update(w)
    elif _cfg.get("weather", "enabled", cast=bool) and _last_error[0]:
        out["error"] = _last_error[0]
    return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
        json.dumps(out).encode("utf-8")
