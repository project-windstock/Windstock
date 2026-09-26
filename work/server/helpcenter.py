"""
Help Center -- https://pokemongo.zendesk.com/hc

The in-game Settings screen has a support button that opens that URL in the
phone's browser. We redirect the host and serve this instead, which makes it the
one place a PLAYER can reach from inside the game without being told a URL.

What it does: lets a trainer nominate a PokeStop or Gym where they're standing.
Nominations land in nominations.json and show up in the World Manager for
approval -- this is the player-facing half, deliberately separate from the
World Manager, which is the admin tool and stays on localhost.
"""
import json
import os
import sys
import threading
import time

import webui

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


ONE_A_DAY_MS = 24 * 60 * 60 * 1000

# ---------------------------------------------------------------- sessions
# Signing in here checks the same password the game uses. Every call afterwards
# carries the token and the server takes the trainer name FROM the token -- the
# page never gets to say who it is, so one player can't edit another's look or
# nominate in their name by typing a different username.
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


def _photo_dir():
    import protocol
    return protocol.PHOTO_DIR


def save_photo(data_url, nom_id):
    """Write an uploaded photo into photos/ and return its filename.

    The page shrinks the image before sending, so this stays small. Anything
    that isn't a plain base64 image is dropped rather than trusted.
    """
    import base64
    if not data_url or not data_url.startswith("data:image/"):
        return ""
    try:
        head, b64 = data_url.split(",", 1)
        ext = "jpg" if "jpeg" in head or "jpg" in head else "png"
        raw = base64.b64decode(b64)
        if len(raw) > 3_000_000:                     # 3 MB is already generous
            return ""
        d = _photo_dir()
        os.makedirs(d, exist_ok=True)
        fn = f"{nom_id}.{ext}"
        with open(os.path.join(d, fn), "wb") as fh:
            fh.write(raw)
        return fn
    except Exception:
        return ""


def last_nomination(player):
    p = (player or "").lower()
    with _lock:
        times = [r.get("when", 0) for r in _load()
                 if (r.get("player") or "").lower() == p]
    return max(times) if times else 0


def cooldown_left(player):
    """Milliseconds until this trainer may nominate again. One a day, so a walk
    round the block can't fill the map with junk."""
    last = last_nomination(player)
    if not last:
        return 0
    return max(0, ONE_A_DAY_MS - (int(time.time() * 1000) - last))


def add(player, kind, name, lat, lng, note, photo_data=""):
    """Record a nomination and place it straight away -- these are auto-accepted."""
    import places
    nom_id = "nom-%d" % int(time.time() * 1000)
    photo = save_photo(photo_data, nom_id)
    row = {"id": nom_id,
           "player": player, "kind": "gym" if kind == "gym" else "stop",
           "name": (name or "").strip()[:40] or "Unnamed",
           "lat": round(float(lat), 6), "lng": round(float(lng), 6),
           "note": (note or "").strip()[:200], "photo": photo,
           "status": "approved", "when": int(time.time() * 1000)}
    places.add_fort(row["lat"], row["lng"], row["kind"], row["name"], photo)
    with _lock:
        rows = _load()
        rows.append(row)
        _save(rows)
    return row


def recent(n=25):
    with _lock:
        return _load()[-n:]


def mine(player):
    with _lock:
        return [r for r in _load()
                if (r.get("player") or "").lower() == (player or "").lower()][-20:]


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


# ---------------------------------------------------------------- events
# The Events page shows the live event (events.json, always known) plus any
# upcoming one the manager scheduled. The manager mirrors its pending schedule
# into event_schedule.json next to the server so this page can read it -- the
# schedule itself is timed inside the manager, this is just the read-out.
# ---------------------------------------------------------------- radar
# One scan a minute, per trainer, enforced HERE -- the page also counts down, but
# that is only so the button looks honest; a reloaded page must not buy a scan.
# Defaults; the live values come from settings (radar.cooldown_seconds /
# radar.range_m) so they can be changed without a restart. Kept as module
# constants too because other modules still read them.
RADAR_COOLDOWN_MS = 60 * 1000
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


SCHED_FILE = datadir.path("event_schedule.json")

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


def _event_summary(cfg):
    """A one-line, player-friendly description of which Pokemon an event favours."""
    mode = cfg.get("species_mode", "all")
    if mode == "single":
        i = int(cfg.get("single_species", 25) or 25)
        who = _DEX[i] if 1 <= i < len(_DEX) else f"#{i}"
        return f"{who} everywhere"
    if mode == "list":
        names = [_DEX[i] for i in (cfg.get("species_list") or []) if 1 <= i < len(_DEX)]
        if not names:
            return "a themed line-up"
        return ", ".join(names[:6]) + (", and more" if len(names) > 6 else "")
    return "All Pokemon"


def _event_public(cfg):
    return {"name": str(cfg.get("event_name", "Normal"))[:40],
            "species": _event_summary(cfg),
            "cp_min": cfg.get("min_cp"), "cp_max": cfg.get("max_cp"),
            "density": cfg.get("spawn_density")}


def _read_schedule():
    """Upcoming scheduled events the manager wrote out. Anything whose start time
    has already passed (e.g. the manager was closed before it fired) is dropped so
    players never see a ghost event that will not happen."""
    try:
        with open(SCHED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    now = time.time()
    out = []
    for e in (data.get("upcoming") or []):
        try:
            st = float(e.get("start_ts") or 0)
        except (TypeError, ValueError):
            continue
        if st and st < now - 120:
            continue
        out.append({"name": str(e.get("event_name", "Event"))[:40],
                    "start_ts": st, "end_ts": e.get("end_ts"),
                    "detail": str(e.get("detail", ""))[:80]})
    out.sort(key=lambda e: e["start_ts"])
    return out


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Help Center</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>__CSS__
 /* ---------- help center only ---------- */
 body{color:var(--ink);background:#fff;min-height:100vh}
 header{background:#fff;color:var(--ink);padding:20px 18px 16px;
  border-bottom:1px solid #edf2f0}
 header h1{margin:0;font-size:22px;font-weight:800;letter-spacing:.02em}
 header p{margin:5px 0 0;font-size:13px;color:#8ba0ab}
 header a.site{display:inline-block;margin-top:9px;font-size:12.5px;font-weight:700;
  color:#22987c;text-decoration:none}
 .wrap{max-width:560px;margin:0 auto;padding:16px 14px 40px}
 .card{background:#fff;border:1px solid #ebf1ef;border-radius:14px;padding:16px;
  margin-bottom:14px;box-shadow:0 1px 3px rgba(20,60,80,.05)}
 .card h2{margin:0 0 4px;font-size:16px;font-weight:800;color:#22404c}
 .card p.sub{margin:0 0 12px;font-size:12.5px;color:#7d94a1;line-height:1.55}
 label{display:block;font-size:12px;font-weight:700;color:#5b7683;margin:12px 0 5px;
  text-transform:uppercase;letter-spacing:.04em}
 input[type=text],textarea{width:100%;font:inherit;font-size:16px;color:#22404c;
  background:#fff;border:2px solid #e4ecea;border-radius:11px;padding:12px}
 input:focus,textarea:focus{outline:none;border-color:#38a58c;background:#fff}
 textarea{min-height:70px;resize:vertical}
 .kinds{display:flex;gap:10px}
 .kinds button{flex:1;padding:13px 8px;border-radius:12px;border:2px solid #e4ecea;
  background:#fff;font:inherit;font-size:15px;font-weight:700;color:#5b7683;cursor:pointer}
 .kinds button.on{background:#e6f5ef;border-color:#38a58c;color:#1d6f5c}
 #map{height:260px;border-radius:12px;margin-top:6px;border:2px solid #e4ecea;
  background:#f6f9f8}
 .coords{font-size:12px;color:#7d94a1;margin-top:7px;text-align:center}
 .coords b{color:#22404c}
 .shot{display:flex;gap:12px;align-items:center;margin-top:6px}
 .shot .prev{width:78px;height:78px;border-radius:12px;flex:none;background:#f6f9f8
  center/cover no-repeat;border:2px dashed #cfdcd6;display:grid;place-items:center;
  color:#a8bcb4;font-size:11px;text-align:center;overflow:hidden}
 .pick{flex:1;border:2px solid #e4ecea;background:#fff;border-radius:11px;
  padding:13px;font:inherit;font-size:14px;font-weight:700;color:#5b7683;
  text-align:center;cursor:pointer}
 .pick:active{background:#e6f5ef;border-color:#38a58c}
 input[type=file]{display:none}
 .go{width:100%;margin-top:16px;border:0;border-radius:999px;padding:15px;font:inherit;
  font-weight:800;font-size:16px;color:#fff;cursor:pointer;
  background:linear-gradient(180deg,#3ec39f,#22987c);box-shadow:0 4px 0 #17705c}
 .go:active{transform:translateY(2px);box-shadow:0 2px 0 #17705c}
 .go[disabled]{background:#adbdc4;box-shadow:0 4px 0 #8b9aa1}
 .quota{margin-top:10px;text-align:center;font-size:12.5px;color:#8a6410;
  background:#fff5e0;border-radius:10px;padding:10px}
 .row{display:flex;align-items:center;gap:10px;padding:11px 0;
  border-bottom:1px solid #eef3f0;font-size:14px}
 .row:last-child{border-bottom:0}
 .row .t{flex:1}
 .row .t small{display:block;color:#8ba0ab;font-size:11.5px;margin-top:2px}
 .row .th{width:40px;height:40px;border-radius:9px;background:#f6f9f8 center/cover no-repeat;flex:none}
 /* outfit picker: one row per slot, big enough to hit with a thumb */
 .slot{display:flex;align-items:center;gap:10px;padding:7px 0}
 .slot .nm{flex:1;font-size:14px;font-weight:700;color:#5b7683;text-transform:capitalize}
 .slot button{width:46px;height:46px;border-radius:12px;border:2px solid #dde8e3;
  background:#f7fbf9;font-size:22px;font-weight:800;color:#3d5563;line-height:1}
 .slot button:active{background:#e6f5ef;border-color:#38a58c}
 .slot .v{min-width:34px;text-align:center;font-size:19px;font-weight:800;color:#22404c}
 input[type=password]{width:100%;font:inherit;font-size:16px;color:#22404c;
  background:#fff;border:2px solid #e4ecea;border-radius:11px;padding:12px}
 .mann{display:grid;place-items:center;padding:6px 0 2px}
 .mann svg{width:150px;height:210px;filter:drop-shadow(0 6px 10px rgba(0,0,0,.14))}
 .note2{margin-top:10px;font-size:11.5px;color:#8ba0ab;line-height:1.6;text-align:center}
 .pickers{display:flex;flex-direction:column;gap:10px}
 .pick2{display:block;width:100%;text-align:left;padding:16px;border-radius:14px;
  border:2px solid #e4ecea;background:#fff;font:inherit;cursor:pointer}
 .pick2 b{display:block;font-size:16px;font-weight:800;color:#22404c}
 .pick2 small{display:block;margin-top:3px;font-size:12.5px;color:#7d94a1}
 .pick2:active{background:#e6f5ef;border-color:#38a58c}
 .back{width:100%;margin-top:12px;border:0;background:none;font:inherit;
  font-size:13px;font-weight:700;color:#7d94a1;padding:10px;cursor:pointer}
 /* the round menu, in the shape of the game's own Poke Ball menu */
 .dial{display:flex;justify-content:center;gap:18px;flex-wrap:wrap;padding:6px 0 2px}
 .orb{width:104px;border:0;background:none;padding:0;cursor:pointer;
  display:flex;flex-direction:column;align-items:center;gap:8px}
 .orb .art{width:88px;height:88px;border-radius:50%;display:block;
  background:#fff center/52px 52px no-repeat;
  border:3px solid #e4ecea;box-shadow:0 5px 0 #dfe8e5,0 8px 16px rgba(20,60,80,.12);
  transition:transform .12s ease,box-shadow .12s ease}
 .orb:active .art{transform:translateY(4px);box-shadow:0 1px 0 #d6e2dc,0 4px 10px rgba(0,0,0,.14)}
 .orb .cap{font-size:13px;font-weight:800;color:#3d5563;letter-spacing:.01em}
 .orb .art.shop{background-image:url('/shop/icon/coinstack.png')}
 .orb .art.place{background-image:url('/shop/icon/lure.png')}
 /* no clothing art in the 2016 icon set, so draw one */
 .orb .art.look{background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><circle cx='32' cy='17' r='11' fill='%23f7c59f' stroke='%23c98b56' stroke-width='2'/><path d='M32 4a13 13 0 0 0-13 12c4 1 9-2 13-5 3 3 8 6 13 5A13 13 0 0 0 32 4z' fill='%235b7683'/><path d='M14 60c0-11 8-19 18-19s18 8 18 19z' fill='%2322987c' stroke='%2317705c' stroke-width='2'/><path d='M25 43l7 6 7-6' fill='none' stroke='%23fff' stroke-width='3' stroke-linecap='round'/></svg>")}
 .orb .art.events{background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect x='10' y='14' width='44' height='40' rx='6' fill='%23fff' stroke='%235b7683' stroke-width='3'/><rect x='10' y='14' width='44' height='12' fill='%2322987c'/><rect x='20' y='7' width='4' height='12' rx='2' fill='%235b7683'/><rect x='40' y='7' width='4' height='12' rx='2' fill='%235b7683'/><circle cx='24' cy='36' r='3' fill='%235b7683'/><circle cx='34' cy='36' r='3' fill='%235b7683'/><circle cx='44' cy='36' r='3' fill='%23f2a03d'/><circle cx='24' cy='46' r='3' fill='%235b7683'/><circle cx='34' cy='46' r='3' fill='%235b7683'/></svg>")}
 #v-events h3{margin:18px 0 8px;font-size:12px;font-weight:800;color:#5b7683;
  text-transform:uppercase;letter-spacing:.04em}
 .ev{display:flex;gap:12px;align-items:flex-start;padding:12px;border:1px solid #ebf1ef;
  border-radius:12px;margin-bottom:10px;background:#fbfdfc}
 .ev .tag{flex:none;min-width:60px;text-align:center;font-size:11px;font-weight:800;
  text-transform:uppercase;letter-spacing:.02em;color:#1d6f5c;background:#e6f5ef;
  border-radius:999px;padding:6px 8px}
 .ev .tag.now{color:#a4471d;background:#ffe9d9}
 .ev .b{flex:1}
 .ev .b b{display:block;font-size:15.5px;font-weight:800;color:#22404c}
 .ev .b small{display:block;color:#7d94a1;font-size:12.5px;margin-top:3px;line-height:1.5}
 .ev .rbtn{flex:none;align-self:center;display:flex;align-items:center;gap:5px;
  border:0;border-radius:999px;padding:7px 11px 7px 8px;background:#e6f5ef;color:#17705c;
  font:inherit;font-size:12px;font-weight:800;cursor:pointer}
 .ev .rbtn:active{transform:scale(.96)}
 .ev .rbtn .art{width:20px;height:20px;background-size:contain;background-repeat:no-repeat}
 .pill{font-size:11px;font-weight:800;padding:4px 9px;border-radius:999px;
  text-transform:uppercase;background:#dff3e6;color:#1d6f43}
 .empty{color:#9fb2bb;font-size:13px;text-align:center;padding:12px}
 /* ---------- radar ---------- */
 #rmap{height:300px;border-radius:12px;margin-top:6px;border:2px solid #e4ecea;
  background:#eaf3f0}
 .rhead{display:flex;align-items:center;gap:10px;margin-top:12px}
 .rhead .n{flex:1;font-size:12.5px;color:#7d94a1}
 .rhead .n b{color:#22404c}
 .find{display:flex;align-items:center;gap:10px;padding:10px 0;
  border-bottom:1px solid #eef3f0;font-size:14px}
 .find:last-child{border-bottom:0}
 .find .dot{width:34px;height:34px;border-radius:50%;flex:none;display:grid;
  place-items:center;font-size:15px;font-weight:800;color:#fff;background:#6f8f9e}
 .find.sh .dot{background:linear-gradient(160deg,#ffd34d,#f2a03d);
  box-shadow:0 0 0 3px #fff4d6}
 .find .t{flex:1}
 .find .t b{font-size:14.5px;font-weight:800;color:#22404c}
 .find .t small{display:block;color:#8ba0ab;font-size:11.5px;margin-top:2px}
 .find.sh .t b{color:#8a6410}
 .find .d{font-size:12px;font-weight:800;color:#5b7683;background:#f1f6f4;
  border-radius:999px;padding:5px 10px;flex:none}
 /* The Pokemon stands ON its disc rather than filling it -- sized to the disc it
    just reads as a coloured square. Species with no art keep the lettered dot. */
 .rmon{width:68px;height:64px;position:relative}
 .rmon .disc{position:absolute;left:50%;bottom:0;width:36px;height:36px;margin-left:-18px;
  border-radius:50%;background:#fff;border:2px solid #fff;
  box-shadow:0 2px 6px rgba(20,60,80,.45)}
 .rmon.sh .disc{background:radial-gradient(circle at 50% 35%,#fff6de,#ffd98a);
  border-color:#f2a03d;box-shadow:0 0 0 3px #ffeab8,0 2px 6px rgba(20,60,80,.45)}
 /* Centred on the disc by transform, sized per species in JS -- see monBox. */
 .rmon img{position:absolute;left:50%;bottom:13px;transform:translateX(-50%);
  filter:drop-shadow(0 2px 2px rgba(0,0,0,.35))}
 .rmon .star{position:absolute;right:4px;top:0;font-size:15px;color:#f2a03d;
  text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;line-height:1}
 .find .dot img{width:30px;height:30px;object-fit:contain;object-position:50% 100%}
 .find.sh .dot img{width:32px;height:32px}
 .rtip{background:#22404c;color:#fff;border:0;border-radius:8px;font-size:11px;
  font-weight:700;padding:3px 7px;box-shadow:none}
 .rtip.sh{background:#f2a03d}
 .rtip:before{display:none}
 .orb .art.radar,.ev .rbtn .art{background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><circle cx='32' cy='32' r='26' fill='%23e6f5ef' stroke='%2322987c' stroke-width='3'/><circle cx='32' cy='32' r='17' fill='none' stroke='%2322987c' stroke-width='2' opacity='.55'/><circle cx='32' cy='32' r='8' fill='none' stroke='%2322987c' stroke-width='2' opacity='.55'/><path d='M32 32L32 6A26 26 0 0 1 54 44z' fill='%2338a58c' opacity='.28'/><circle cx='32' cy='32' r='3.5' fill='%2317705c'/><circle cx='44' cy='22' r='4' fill='%23f2a03d'/></svg>")}
 #toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,14px);
  background:#22404c;color:#fff;padding:13px 20px;border-radius:13px;font-size:14px;
  font-weight:600;max-width:88vw;text-align:center;opacity:0;transition:.25s;
  pointer-events:none;z-index:2000}
 #toast.on{opacity:1;transform:translate(-50%,0)}
 #toast.bad{background:#8a2732}
</style></head><body>
<header>
  <h1>Help Center</h1>
  <p>Add a PokeStop or Gym near you</p>
  <a class="site" href="https://projectwindstock.site.je">Project Windstock website &rarr;</a>
</header>
<div class="wrap">

  <div class="card" id="v-login">
    <h2>Sign in</h2>
    <p class="sub">Same trainer name and password you use in the game. Your first
      login in the game is what set the password.</p>
    <label>Trainer name</label>
    <input type="text" id="who" placeholder="trainer name" autocapitalize="off"
           spellcheck="false">
    <label>Password</label>
    <input type="password" id="pass" placeholder="password">
    <button class="go" id="signin" onclick="signIn()">SIGN IN</button>
  </div>

  <div class="card" id="v-menu" style="display:none">
    <h2>Hi <span id="hello"></span></h2>
    <p class="sub">What would you like to do?</p>
    <div class="dial">
      <button class="orb" onclick="show('place')">
        <span class="art place"></span><span class="cap">Add a place</span></button>
      <button class="orb" onclick="show('events')">
        <span class="art events"></span><span class="cap">Events</span></button>
      <button class="orb" onclick="show('radar')">
        <span class="art radar"></span><span class="cap">Radar</span></button>
    </div>
    <button class="back" onclick="signOut()">Sign out</button>
  </div>

  <div class="card" id="v-place" style="display:none">
    <h2>Add a place</h2>
    <p class="sub">Drag the map so the pin sits on the spot, add a photo, and it
      goes into the game straight away. One a day.</p>

    <label>What is it</label>
    <div class="kinds">
      <button id="k-stop" class="on" onclick="setKind('stop')">PokeStop</button>
      <button id="k-gym" onclick="setKind('gym')">Gym</button>
    </div>

    <label>Where</label>
    <div id="map"></div>
    <div class="coords" id="coords">Finding you&hellip;</div>

    <label>Photo</label>
    <div class="shot">
      <div class="prev" id="prev">no photo</div>
      <div class="pick" onclick="document.getElementById('file').click()">
        Choose a photo</div>
      <input type="file" id="file" accept="image/*" onchange="pickPhoto(this)">
    </div>

    <label>Name</label>
    <input type="text" id="name" placeholder="e.g. The Old Oak Tree" maxlength="40">
    <label>About it</label>
    <textarea id="note" placeholder="A sentence about it" maxlength="200"></textarea>
    <button class="go" id="send" onclick="send()">ADD IT</button>
    <div class="quota" id="quota" style="display:none"></div>
    <button class="back" onclick="show('menu')">Back</button>
  </div>

  <div class="card" id="v-mine" style="display:none">
    <h2>Yours</h2>
    <p class="sub">Everything you've added.</p>
    <div id="list"><div class="empty">Nothing yet.</div></div>
  </div>

  <div class="card" id="v-radar" style="display:none">
    <h2>Radar</h2>
    <p class="sub">A sweep around where you are standing in the game. It picks up
      everything wild within 500 m &mdash; shinies included. One sweep a minute.</p>
    <div id="rmap"></div>
    <div class="rhead">
      <div class="n" id="r-when">Tap sweep to look around you.</div>
    </div>
    <button class="go" id="scan" onclick="scan()">SWEEP</button>
    <div id="r-list"></div>
    <button class="back" onclick="show('menu')">Back</button>
  </div>

  <div class="card" id="v-events" style="display:none">
    <h2>Events</h2>
    <p class="sub">What's out in the wild right now, and what's coming up.</p>
    <div id="ev-now"></div>
    <h3>Coming up</h3>
    <div id="ev-next"><div class="empty">Nothing scheduled.</div></div>
    <button class="back" onclick="show('menu')">Back</button>
  </div>
</div>
<div id="toast"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const $ = id => document.getElementById(id);
let kind = 'stop', pos = null, photo = '', map = null, marker = null;
function setKind(k){
  kind = k;
  $('k-stop').className = k === 'stop' ? 'on' : '';
  $('k-gym').className  = k === 'gym'  ? 'on' : '';
}
function toast(m, bad){
  const t = $('toast'); t.textContent = m; t.className = 'on' + (bad ? ' bad' : '');
  clearTimeout(t._t); t._t = setTimeout(function(){ t.className=''; }, 3000);
}
let TOKEN = '', ME = '';
function show(view){
  ['login','menu','place','mine','events','radar'].forEach(function(v){
    const el = $('v-' + v);
    if (el) el.style.display = (v === view || (v === 'mine' && view === 'place'))
                               ? '' : 'none';
  });
  if (view === 'place' && !mapReady) startMap();
  if (view === 'place') refresh();
  if (view === 'events') loadEvents();
  if (view === 'radar' && !radarReady) startRadar();   // async: fills itself in
}
async function signIn(){
  const who = $('who').value.trim();
  if (!who) return toast('Enter your trainer name', true);
  $('signin').disabled = true;
  const r = await api('/hc/login', {player: who, password: $('pass').value});
  $('signin').disabled = false;
  if (!r.ok) return toast(r.message, true);
  TOKEN = r.token; ME = r.player;
  $('pass').value = '';
  $('hello').textContent = ME;
  show('menu');
}
function signOut(){
  TOKEN = ''; ME = '';
  clearInterval(rTimer); rTimer = null; rLeft = 0;   // the next trainer gets their own sweep
  $('scan').disabled = false; $('scan').textContent = 'SWEEP';
  show('login');
}
async function api(path, body){
  const payload = Object.assign({}, body || {});
  if (TOKEN) payload.token = TOKEN;
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify(payload)});
  const out = await r.json();
  if (out && out.signed_out){        // token expired or server restarted
    TOKEN = ''; show('login'); toast('Please sign in again', true);
  }
  return out;
}
function showCoords(){
  $('coords').innerHTML = 'Pin at <b>' + pos.lat.toFixed(5) + ', ' + pos.lng.toFixed(5)
                        + '</b> &mdash; drag the map to move it';
}
function initMap(lat, lng){
  pos = {lat: lat, lng: lng};
  if (typeof L === 'undefined'){        // no internet for the tiles; coords still work
    $('map').style.display = 'none';
    $('coords').innerHTML = 'Using where you are: <b>' + lat.toFixed(5) + ', '
                          + lng.toFixed(5) + '</b>';
    return;
  }
  map = L.map('map', {zoomControl:true}).setView([lat, lng], 18);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
              {maxZoom:19, attribution:'&copy; OpenStreetMap'}).addTo(map);
  marker = L.marker([lat, lng], {draggable:true}).addTo(map);
  marker.on('dragend', function(){
    const p = marker.getLatLng(); pos = {lat:p.lat, lng:p.lng}; showCoords();
  });
  map.on('click', function(e){
    marker.setLatLng(e.latlng); pos = {lat:e.latlng.lat, lng:e.latlng.lng}; showCoords();
  });
  showCoords();
  setTimeout(function(){ map.invalidateSize(); }, 200);
}
// Shrink on the device before uploading -- a modern phone photo is several MB
// and none of that detail survives on a 40px marker anyway.
function pickPhoto(input){
  const f = input.files && input.files[0];
  if (!f) return;
  const img = new Image(), rd = new FileReader();
  rd.onload = function(){ img.src = rd.result; };
  img.onload = function(){
    const max = 640, sc = Math.min(1, max / Math.max(img.width, img.height));
    const c = document.createElement('canvas');
    c.width = Math.round(img.width * sc); c.height = Math.round(img.height * sc);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    photo = c.toDataURL('image/jpeg', 0.82);
    $('prev').style.backgroundImage = 'url(' + photo + ')';
    $('prev').textContent = '';
  };
  rd.readAsDataURL(f);
}
async function send(){
  if (!pos) return toast('Still finding where you are', true);
  if (!TOKEN) return toast('Sign in first', true);
  if (!photo) return toast('Add a photo first', true);
  $('send').disabled = true;
  const r = await api('/hc/nominate', {
    kind: kind, name: $('name').value,
    note: $('note').value, photo: photo, lat: pos.lat, lng: pos.lng});
  $('send').disabled = false;
  toast(r.message, !r.ok);
  if (r.ok){
    $('name').value=''; $('note').value=''; photo='';
    $('prev').style.backgroundImage=''; $('prev').textContent='no photo';
    refresh();
  }
  if (r.wait_ms) showQuota(r.wait_ms);
}
function showQuota(ms){
  if (!ms){ $('quota').style.display='none'; $('send').disabled=false; return; }
  const h = Math.floor(ms/3600000), m = Math.round((ms%3600000)/60000);
  $('quota').style.display='block';
  $('quota').textContent = 'You have used today\u2019s place. Next one in '
                         + (h ? h + 'h ' : '') + m + 'm.';
  $('send').disabled = true;
}
async function refresh(){
  if (!TOKEN) return;
  const r = await api('/hc/mine', {});
  showQuota(r.wait_ms || 0);
  const box = $('list');
  if (!r.rows || !r.rows.length){
    box.innerHTML = '<div class="empty">Nothing yet.</div>'; return;
  }
  box.innerHTML = '';
  r.rows.slice().reverse().forEach(function(n){
    const d = document.createElement('div'); d.className='row';
    d.innerHTML =
      '<div class="th"' + (n.photo ? ' style="background-image:url(/hc/photo/'
        + encodeURIComponent(n.photo) + ')"' : '') + '></div>'
      + '<div class="t">' + n.name
      + '<small>' + (n.kind === 'gym' ? 'Gym' : 'PokeStop') + ' &middot; '
      + n.lat.toFixed(4) + ', ' + n.lng.toFixed(4) + '</small></div>'
      + '<span class="pill">in game</span>';
    box.appendChild(d);
  });
}
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function fmtClock(ts){
  const d = new Date(ts * 1000);
  let h = d.getHours(); const m = d.getMinutes(), ap = h < 12 ? 'AM' : 'PM';
  h = h % 12 || 12;
  return h + ':' + (m < 10 ? '0' : '') + m + ' ' + ap;
}
function whenLabel(ts){
  const mins = Math.round((ts * 1000 - Date.now()) / 60000);
  if (mins <= 0) return 'starting now';
  if (mins < 60) return 'in ' + mins + ' min';
  if (mins < 1440) return 'at ' + fmtClock(ts);
  return 'tomorrow ' + fmtClock(ts);
}
function evRow(tag, tagCls, name, lines){
  return '<div class="ev"><div class="tag ' + tagCls + '">' + esc(tag) + '</div>'
    + '<div class="b"><b>' + esc(name) + '</b>'
    + lines.filter(Boolean).map(function(l){ return '<small>' + esc(l) + '</small>'; }).join('')
    + '</div>'
    + '<button class="rbtn" onclick="show(\'radar\')" title="Sweep for it on the radar">'
    + '<span class="art"></span>Radar</button></div>';
}
async function loadEvents(){
  const r = await api('/hc/events', {});
  const c = r.current;
  $('ev-now').innerHTML = c
    ? evRow('Now', 'now', c.name,
        [c.species, (c.cp_min && c.cp_max) ? ('CP ' + c.cp_min + '–' + c.cp_max) : ''])
    : '';
  const box = $('ev-next');
  if (r.upcoming && r.upcoming.length){
    box.innerHTML = r.upcoming.map(function(e){
      return evRow(whenLabel(e.start_ts), '', e.name, [e.detail]);
    }).join('');
  } else {
    box.innerHTML = '<div class="empty">Nothing scheduled. Check back later!</div>';
  }
}
// ---------------------------------------------------------------- radar
// The sweep is spent server-side, so the countdown here is only about keeping the
// button honest -- reloading the page does not hand you another one.
let rmap = null, rlayer = null, radarReady = false, rLeft = 0, rTimer = null;
let rFit = true;                     // frame the sweep once, then leave the view alone
async function startRadar(){
  radarReady = true;
  if (typeof L === 'undefined'){        // no tiles out here; the list still works
    $('rmap').style.display = 'none'; return;
  }
  rmap = L.map('rmap', {zoomControl:true}).setView([0, 0], 17);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
              {maxZoom:19, attribution:'&copy; OpenStreetMap'}).addTo(rmap);
  rlayer = L.layerGroup().addTo(rmap);
  // Open ON the trainer rather than in the middle of the ocean: the first sweep
  // may be a minute away, and a map of nowhere tells you nothing meanwhile.
  const r = await api('/hc/spot', {});
  if (r && r.ok){
    rmap.setView([r.lat, r.lng], 16);
    L.circle([r.lat, r.lng], {radius: r.range_m, color:'#38a58c', weight:1,
                              fillColor:'#38a58c', fillOpacity:.06}).addTo(rlayer);
    L.circleMarker([r.lat, r.lng], {radius:7, color:'#fff', weight:3,
                                    fillColor:'#2f6fd0', fillOpacity:1})
      .addTo(rlayer).bindTooltip('You', {permanent:true, direction:'top',
                                         className:'rtip', offset:[0,-6]});
    $('r-when').textContent = 'Standing by. Tap sweep to look around you.';
  } else {
    $('r-when').textContent = 'Open the game so the radar can find you.';
  }
  setTimeout(function(){ rmap.invalidateSize(); }, 200);
}
function mmss(ms){
  const s = Math.max(0, Math.ceil(ms / 1000));
  return Math.floor(s / 60) + ':' + (s % 60 < 10 ? '0' : '') + (s % 60);
}
function rCooldown(ms){
  clearInterval(rTimer); rLeft = ms || 0;
  const tick = function(){
    if (rLeft <= 0){
      clearInterval(rTimer); rTimer = null;
      $('scan').disabled = false; $('scan').textContent = 'SWEEP';
      return;
    }
    $('scan').disabled = true;
    $('scan').textContent = 'RECHARGING ' + mmss(rLeft);
    rLeft -= 1000;
  };
  tick();
  if (rLeft > 0) rTimer = setInterval(tick, 1000);
}
function goneIn(ms){
  const left = ms - Date.now();
  if (!ms || left <= 0) return 'about to go';
  return left < 60000 ? 'gone in under a minute'
                      : 'gone in ' + Math.round(left / 60000) + ' min';
}
let ART = {};              // {dex:[w,h]} for species with a sprite; rest keep the dot
// Scale by AREA so every sprite carries the same visual weight whatever its
// shape -- the art is cropped tight, and one fixed box turns a wide Pokemon into
// a sliver next to a tall one.
function monBox(wh){
  const TARGET = 40, CAP = 58;
  const s = TARGET / Math.sqrt(wh[0] * wh[1]);
  let w = wh[0] * s, h = wh[1] * s;
  const k = Math.min(1, CAP / Math.max(w, h));
  return [Math.round(w * k), Math.round(h * k)];
}
function drawRadar(r){
  ART = r.art || {};
  if (rmap){
    rlayer.clearLayers();
    const ring = L.circle([r.lat, r.lng], {radius: r.range_m, color:'#38a58c',
                            weight:1, fillColor:'#38a58c', fillOpacity:.06}).addTo(rlayer);
    // Frame the whole sweep on the FIRST look only: after that, whatever you
    // panned or zoomed to is yours to keep, and the next sweep leaves it alone.
    if (rFit){ rFit = false; rmap.fitBounds(ring.getBounds(), {padding:[12, 12]}); }
    L.circleMarker([r.lat, r.lng], {radius:7, color:'#fff', weight:3,
                                    fillColor:'#2f6fd0', fillOpacity:1})
      .addTo(rlayer).bindTooltip('You', {permanent:true, direction:'top',
                                         className:'rtip', offset:[0,-6]});
    r.rows.forEach(function(p){
      // A species with art gets the sprite; anything else keeps the plain dot.
      const wh = ART[p.pokemon_id], box = wh && monBox(wh);
      const m = box
        ? L.marker([p.lat, p.lng], {zIndexOffset: p.shiny ? 1000 : 0,
            icon: L.divIcon({className:'',
              html:'<div class="rmon' + (p.shiny ? ' sh' : '') + '">'
                 + '<span class="disc"></span>'
                 + '<img src="/hc/mon/' + p.pokemon_id + '.png" alt="" width="'
                 + box[0] + '" height="' + box[1] + '">'
                 + (p.shiny ? '<span class="star">★</span>' : '') + '</div>',
              iconSize:[68,64], iconAnchor:[34,62]})})
        : L.circleMarker([p.lat, p.lng], {
            radius: p.shiny ? 9 : 6, weight: p.shiny ? 3 : 2, color:'#fff',
            fillColor: p.shiny ? '#f2a03d' : '#6f8f9e', fillOpacity:1});
      m.addTo(rlayer)
       .bindTooltip((p.shiny ? '★ ' : '') + p.name,
                    {permanent: !!p.shiny, direction:'top',
                     className: 'rtip' + (p.shiny ? ' sh' : ''),
                     offset:[0, box ? -62 : -6]});
    });
    setTimeout(function(){ rmap.invalidateSize(); }, 100);
  }
  const shinies = r.rows.filter(function(p){ return p.shiny; }).length;
  const more = r.left > 0
    ? ' &middot; still looking at the edges &mdash; sweep again'
    : '';
  $('r-when').innerHTML = r.rows.length
    ? '<b>' + r.rows.length + '</b> nearby'
      + (shinies ? ' &middot; <b>' + shinies + ' shiny</b>' : '')
      + ' &middot; swept ' + fmtClock(Date.now() / 1000) + more
    : 'Nothing within 500 m right now.' + more;
  const box = $('r-list');
  box.innerHTML = '';
  r.rows.forEach(function(p){
    const d = document.createElement('div');
    d.className = 'find' + (p.shiny ? ' sh' : '');
    d.innerHTML = '<div class="dot">'
      + (ART[p.pokemon_id]
          ? '<img src="/hc/mon/' + p.pokemon_id + '.png" alt="">'
          : (p.shiny ? '★' : esc(p.name[0]))) + '</div>'
      + '<div class="t"><b>' + esc(p.name) + (p.shiny ? ' — shiny!' : '') + '</b>'
      + '<small>' + goneIn(p.expires_ms) + '</small></div>'
      + '<div class="d">' + p.distance_m + ' m</div>';
    box.appendChild(d);
  });
}
async function scan(){
  if (rLeft > 0) return;
  $('scan').disabled = true;
  const r = await api('/hc/radar', {});
  if (!r.ok){
    toast(r.message, true);
    rCooldown(r.wait_ms || 0);
    return;
  }
  drawRadar(r);
  if (r.rows.some(function(p){ return p.shiny; })) toast('Shiny on the radar!');
  rCooldown(r.wait_ms || 60000);
}
$('who').addEventListener('change', refresh);
(async function(){
  const r = await api('/hc/where', {});
  if (r.player && !$('who').value) $('who').value = r.player;
  if (navigator.geolocation){
    navigator.geolocation.getCurrentPosition(
      function(p){ initMap(p.coords.latitude, p.coords.longitude); refresh(); },
      function(){ initMap(r.lat || 0, r.lng || 0); refresh(); },
      {enableHighAccuracy:true, timeout:8000});
  } else {
    initMap(r.lat || 0, r.lng || 0); refresh();
  }
})();
</script></body></html>"""


def _json(obj, code=200):
    return code, {"Content-Type": "application/json",
                  "Cache-Control": "no-store"}, json.dumps(obj).encode("utf-8")


def handle(method, path, query, headers, body, log):
    if path in ("/", "/hc", "/hc/", "/hc/en-us", "/help"):
        return (200, {"Content-Type": "text/html; charset=utf-8",
                      "Cache-Control": "no-store"},
                webui.render_phone(PAGE).encode("utf-8"))
    try:
        d = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        d = {}

    if path.startswith("/hc/mon/"):
        # Sprite for one species, by dex number: /hc/mon/25.png. Public on purpose
        # -- it is art, and the radar page asks for it before anything is signed in.
        import monart
        return monart.serve(os.path.splitext(os.path.basename(path))[0])

    if path == "/hc/where":
        import rpc
        return _json({"lat": rpc._last_loc[0], "lng": rpc._last_loc[1],
                      "player": rpc._last_user[0]})

    if path == "/hc/events":
        # Public: anyone can see what's on, no sign-in needed.
        import events as EV
        return _json({"current": _event_public(EV.get()),
                      "upcoming": _read_schedule()})

    if path == "/hc/login":
        import world
        who = (d.get("player") or "").strip()
        ok, why, real = world.check_login(who, d.get("password") or "")
        if not ok:
            log(f"[help] sign-in refused for {who!r} ({why})")
            return _json({"ok": False,
                          "message": ("Wrong password." if why == "wrong password"
                                      else "Enter your trainer name.")})
        if why == "claimed":
            log(f"[help] {real} claimed their account from the Help Center")
        log(f"[help] {real} signed in")
        return _json({"ok": True, "player": real, "token": open_session(real)})

    # Everything past here needs a signed-in trainer. The name comes from the
    # token, never from the request body.
    me = session_player(d.get("token"))
    if me is None:
        return _json({"ok": False, "signed_out": True,
                      "message": "Please sign in again."})

    if path == "/hc/nominate":
        who = me
        if not who:
            return _json({"ok": False, "message": "Enter your trainer name."})
        wait = cooldown_left(who)
        if wait > 0:
            return _json({"ok": False, "wait_ms": wait,
                          "message": "You've already added a place today."})
        try:
            lat, lng = float(d.get("lat")), float(d.get("lng"))
        except (TypeError, ValueError):
            return _json({"ok": False, "message": "No location on that one."})
        if not (abs(lat) > 1e-6 or abs(lng) > 1e-6):
            return _json({"ok": False, "message": "No location on that one."})
        row = add(who, d.get("kind"), d.get("name"), lat, lng,
                  d.get("note"), d.get("photo"))
        log(f"[help] {row['player']} added a {row['kind']}: {row['name']!r} at "
            f"{row['lat']:.5f},{row['lng']:.5f}"
            + (f" (photo {row['photo']})" if row["photo"] else " (no photo)"))
        return _json({"ok": True, "wait_ms": cooldown_left(who),
                      "message": "Added. Look for it on the map."})

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

    if path == "/hc/mine":
        who = me
        return _json({"rows": mine(who), "wait_ms": cooldown_left(who)})

    if path.startswith("/hc/photo/"):
        import urllib.parse as _up
        name = os.path.basename(_up.unquote(path[len("/hc/photo/"):]))
        fp = os.path.join(_photo_dir(), name)
        if name and os.path.isfile(fp):
            ext = os.path.splitext(name)[1].lower()
            ct = "image/png" if ext == ".png" else "image/jpeg"
            with open(fp, "rb") as fh:
                return 200, {"Content-Type": ct,
                             "Cache-Control": "public, max-age=3600"}, fh.read()
        return 404, {"Content-Type": "text/plain"}, b"no photo"

    return 404, {"Content-Type": "text/plain"}, b"no"
