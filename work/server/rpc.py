"""
RPC endpoint — pgorelease.nianticlabs.com/plfe/rpc

Milestone 1: get the player logged in and onto the map.
  - Decode + log the RequestEnvelope (so we can verify the real client's exact
    field layout against protocol.py's constants).
  - First call to /plfe/rpc  -> status 53 REDIRECT to an api_url on the same
    host (the documented bootstrap handshake).
  - Calls to the redirected url -> status 2 OK, with an auth_ticket and one
    return per request. GET_PLAYER gets a real PlayerData (nickname=username,
    tutorial complete). Every other request gets an empty response for now;
    that is enough to clear the login/handshake. We flesh them out next.

api_url stays on pgorelease.nianticlabs.com so the client keeps hitting a host
our DNS redirect + TLS cert already cover.
"""
import struct
import os
import time
import pb
import protocol as P

API_HOST = "pgorelease.nianticlabs.com"
API_PATH = "/plfe/0/rpc"          # any non-bootstrap path works
# Where GET_DOWNLOAD_URLS points the client for the ~65 MB of model bundles.
# Default: the Niantic host (resolves to this server via our DNS). Override with
# ASSET_BASE_URL to send downloads over a FAST public path (e.g. a playit tunnel)
# so remote players don't pull assets through the slow Tailscale relay.
ASSET_BASE_URL = os.environ.get("ASSET_BASE_URL", f"https://{API_HOST}/asset")
# Set LOG_VERBOSE=1 (or DEBUG=1) to include the raw protobuf dumps and the
# blow-by-blow battle diagnostics. OFF by default so the log reads as a plain
# account of what the game asked for and what we answered, one batch at a time.
VERBOSE = bool(os.environ.get("LOG_VERBOSE") or os.environ.get("DEBUG"))
_dump_budget = [8]                 # (verbose only) dump the first N raw envelopes
# The last location / trainer ANYONE reported. Only the World Manager (the
# operator, on localhost) reads these; nothing a player can reach does, because
# on a shared server "the last request" is somebody else.
_last_loc = [0.0, 0.0]
_last_user = [None]
_logged_loc = {}                  # username -> last location we PRINTED (de-spam)
# Which trainer each phone (by IP) last played as. The tweak's raid screens call
# /raid/... without the game's login, so this is how raidlobby.py knows who's asking.
import threading as _threading
_req_ip = _threading.local()      # set by server.py for the request being handled
_ip_user = {}
# The tweak's screens (Research, the Radar map, weather, raids) know who you are only by
# the IP your game's requests come from. Kept on disk too: a server restart used to
# forget everyone, and a Radar opened before the game's next request then answered
# "unknown trainer -- open the game first".
_IP_USERS_FILE = None
# An address stops meaning a trainer this long after their game last spoke from
# it, so a recycled or shared address doesn't keep someone else's identity.
IP_USER_TTL = 60 * 60
_ip_seen = {}                     # ip -> time.time() of that trainer's last game request
_ip_saved = [0.0]
# username -> time.time() of their last verified game request. The game asks
# for the map every few seconds while it is open, so "recent" means "playing".
_user_seen = {}
ONLINE_WINDOW = 5 * 60
# The launcher's tunnel adds X-WS-Device (a random secret it made up, sent only
# inside TLS) to everything from one phone. device -> [trainer, last game request].
_req_dev = _threading.local()     # set by server.py for the request being handled
_req_old = _threading.local()      # this request came from a launcher too old for here (online.outdated)
_dev_user = {}
DEVICE_TTL = 30 * 24 * 3600
MAX_DEVICES = 20000
_DEVICE_OK = None


def clean_device(value):
    """The header's value if it looks like a device id, else None."""
    global _DEVICE_OK
    if _DEVICE_OK is None:
        import re
        _DEVICE_OK = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
    value = (value or "").strip()
    return value if _DEVICE_OK.match(value) else None


def _ip_users_file():
    global _IP_USERS_FILE
    if _IP_USERS_FILE is None:
        import datadir
        _IP_USERS_FILE = os.path.join(datadir.ensure(), "ip_users.json")
    return _IP_USERS_FILE


def _load_ip_users():
    import json
    try:
        with open(_ip_users_file(), encoding="utf-8") as fh:
            d = json.load(fh)
        for ip, row in (d.items() if isinstance(d, dict) else ()):
            # [username, last_seen]; the older file had the bare username, which
            # carries no age and so is not trusted.
            if isinstance(row, list) and len(row) == 2 and row[0]:
                _ip_user[str(ip)] = str(row[0])
                _ip_seen[str(ip)] = float(row[1])
    except (OSError, ValueError, TypeError):
        pass
    try:
        with open(_devices_file(), encoding="utf-8") as fh:
            d = json.load(fh)
        for dev, row in (d.items() if isinstance(d, dict) else ()):
            if clean_device(dev) and isinstance(row, list) and len(row) == 2 and row[0]:
                _dev_user[dev] = [str(row[0]), float(row[1])]
    except (OSError, ValueError, TypeError):
        pass


def _devices_file():
    return os.path.join(os.path.dirname(_ip_users_file()), "devices.json")


def _remember_ip(ip, username):
    """Called only for a request whose login this server has verified."""
    now = time.time()
    dev = getattr(_req_dev, "value", None)
    dev_changed = False
    if dev:
        row = _dev_user.get(dev)
        dev_changed = not row or row[0] != username
        _dev_user[dev] = [username, now]
    changed = _ip_user.get(ip) != username or dev_changed
    _ip_user[ip] = username
    _ip_seen[ip] = now
    _user_seen[username] = now
    if not changed and now - _ip_saved[0] < 300:
        return
    _ip_saved[0] = now
    import json
    try:
        for old in [k for k, t in _ip_seen.items() if now - t > IP_USER_TTL]:
            _ip_seen.pop(old, None)
            _ip_user.pop(old, None)
        f = _ip_users_file()
        with open(f + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({k: [v, _ip_seen.get(k, 0)] for k, v in _ip_user.items()}, fh)
        os.replace(f + ".tmp", f)
        stale = [k for k, row in _dev_user.items() if now - row[1] > DEVICE_TTL]
        if len(_dev_user) - len(stale) > MAX_DEVICES:      # keep the newest
            stale += sorted((k for k in _dev_user if k not in stale),
                            key=lambda k: _dev_user[k][1])[:len(_dev_user) - len(stale) - MAX_DEVICES]
        for k in stale:
            _dev_user.pop(k, None)
        f = _devices_file()
        fd = os.open(f + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_dev_user, fh)
        os.replace(f + ".tmp", f)
    except OSError:
        pass


_load_ip_users()



# username -> "ios" / "android", from the client's config-version check at login. The
# in-game shop lists the Shiny Charm / Shiny Incense only on iOS, whose game files carry
# their store art (tools/patch_badges.py); on Android they'd be blank tiles.
USER_PLATFORM = {}


import re as _re
_HOST_OK = _re.compile(r"^[A-Za-z0-9.\-]{1,253}(:\d{1,5})?$")


def _wobbles(resp):
    """How many times the client will rock the ball before a break-out: its
    Pokeball.GetNumShakes mapping of miss_percent (protocol._escape_miss_percent)."""
    raw = pb.get(resp, 2, pb.WT_64)
    miss = struct.unpack("<d", struct.pack("<Q", raw))[0] if raw else 0.0
    return 3 if miss < 0.15 else 2 if miss < 0.5 else 1 if miss < 0.75 else 0

def _ip_identifies(ip):
    """May an address alone say who is asking? On loopback (the server on the
    phone itself) and private networks (home Wi-Fi, a tailnet) every phone has
    its own address. Over the internet a whole household -- or a whole mobile
    carrier -- shares one, so there it never counts: the launcher's device id
    is the only thing that does."""
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        return (a.is_loopback or a.is_private or a.is_link_local
                or a in ipaddress.ip_network("100.64.0.0/10"))
    except ValueError:
        return False


def online_count(window=ONLINE_WINDOW):
    """How many trainers have the game open right now (a count, no names)."""
    now = time.time()
    for old in [u for u, t in list(_user_seen.items()) if now - t > IP_USER_TTL]:
        _user_seen.pop(old, None)
    return sum(1 for t in list(_user_seen.values()) if now - t <= window)


def ip_for_user(username):
    """The address this trainer most recently played from, or None."""
    now = time.time()
    seen = [(_ip_seen.get(ip, 0), ip) for ip, name in list(_ip_user.items()) if name == username]
    seen = [row for row in seen if now - row[0] <= IP_USER_TTL]
    return max(seen)[1] if seen else None


def user_for_device():
    """The trainer last logged in to the game from the phone making this
    request (its launcher tunnel's X-WS-Device), or None. Never by address."""
    dev = getattr(_req_dev, "value", None)
    row = _dev_user.get(dev) if dev else None
    return row[0] if row and time.time() - row[1] <= DEVICE_TTL else None


def user_for_ip(ip):
    """Who the tweak's screens (Research, raids, weather, shiny) are talking to.

    A launcher's tunnel marks every request with its own secret device id, and
    a verified game login on that device says whose it is -- so two phones
    behind one router are two trainers. Without an id it falls back to the
    address, where that is safe (see _ip_identifies)."""
    now = time.time()
    dev = getattr(_req_dev, "value", None)
    if dev:
        row = _dev_user.get(dev)
        if row and now - row[1] <= DEVICE_TTL:
            return row[0]
    if not ip or now - _ip_seen.get(ip, 0) > IP_USER_TTL or not _ip_identifies(ip):
        return None
    return _ip_user.get(ip)


_incense_logged = {}     # trainer -> the last incense encounter id written to the log


def _loc(username):
    """Where THIS trainer last was, (0, 0) if never seen."""
    try:
        import world
        return world.player_location(username) or (0.0, 0.0)
    except Exception:
        return (0.0, 0.0)


# The gym each trainer last opened (GET_GYM_DETAILS / START_GYM_BATTLE). The raid screens
# ask for "my current gym" instead of reading the fort id out of game memory.
_user_gym = {}


def gym_for_user(user):
    return _user_gym.get(user) if user else None


def _moved_far(a, b, metres=15.0):
    """Roughly true if two lat/lngs are more than `metres` apart -- enough to be
    worth another [loc] line. ~1e-5 degrees is about a metre, so this is a cheap
    box test, no trig needed."""
    return (abs(a[0] - b[0]) + abs(a[1] - b[1])) > (metres / 111_000.0)


def _envelope_latlng(fields):
    la = pb.get(fields, P.RE_LATITUDE, pb.WT_64)
    lo = pb.get(fields, P.RE_LONGITUDE, pb.WT_64)
    if la is None or lo is None:
        return None
    return (struct.unpack("<d", struct.pack("<Q", la))[0],
            struct.unpack("<d", struct.pack("<Q", lo))[0])


def _research(username, kind, n=1, **ctx):
    """Count something toward Field / Special / Timed Research (research.py)."""
    try:
        import research
        research.event(username, kind, n, **ctx)
    except Exception as e:
        print(f"   [research] event {kind} failed: {e}", flush=True)


def _mon(uid):
    """'#25 CP312' for one of the current trainer's Pokemon (for the log)."""
    try:
        import world
        c = world.get_caught(uid)
        return f"#{c['pokemon_id']} CP{c['cp']}" if c else ""
    except Exception:
        return ""


def _build_returns(reqs, username, log):
    # Everything below reads/writes THIS account's state (saves/<name>.json).
    # Must happen before any world.* call in the request.
    import world
    world.use(username)
    here = _loc(username)       # this trainer's own position, never "whoever was last"
    # Bring home any Gym defenders that have served their time and pay the coins.
    # Checked on every batch so it happens while you play, not only on a restart.
    try:
        for fid, pid, coins in world.collect_gym_returns():
            log(f"   [gym] defender #{pid} came home from {fid[:12]}... "
                f"after {world._defender_minutes():g} min -> +{coins} PokeCoins "
                f"(total {world.current().COINS})")
    except Exception as e:
        log(f"   [gym] return check failed: {e}")
    returns = []
    for rtype, msg in reqs:
        if rtype == P.RT.GET_PLAYER:
            returns.append(P.build_get_player_response(username))
            log(f"      -> GET_PLAYER answered as {username!r}")
        elif rtype == P.RT.SET_CONTACT_SETTINGS:
            returns.append(P.build_set_contact_settings_response(username))
            log("      -> SET_CONTACT_SETTINGS -> SUCCESS (nothing to email offline)")
        elif rtype == P.RT.GET_MAP_OBJECTS:
            cells, lat, lng = P.parse_get_map_objects(msg)
            if not lat and not lng:                 # map msg had no fix; use the
                lat, lng = here                     # player's last known location
            returns.append(P.build_get_map_objects_response(cells, lat, lng))
            log(f"      -> GET_MAP_OBJECTS: {len(cells)} cells @ ({lat:.5f},{lng:.5f}) + spawns")
        elif rtype == P.RT.GET_INVENTORY:
            since = P.parse_get_inventory(msg)
            returns.append(P.build_get_inventory_response(since))
            log("      -> GET_INVENTORY answered " +
                (f"as a delta since {since}" if since else "in full (cold start)"))
        elif rtype == P.RT.DOWNLOAD_REMOTE_CONFIG_VERSION:
            plat = P.parse_platform(msg)
            USER_PLATFORM[username] = plat          # the in-game shop depends on it
            returns.append(P.build_download_remote_config_version_response(plat))
            ver = P.parse_client_version(msg)
            log(f"      -> config version check [{plat}] client v{ver // 100 / 10:.2f}"
                + ("\n" + pb.pretty(msg) if VERBOSE else ""))
        elif rtype == P.RT.CHECK_CHALLENGE:
            returns.append(P.build_check_challenge_response())
            log("      -> CHECK_CHALLENGE answered (no captcha)")
        elif rtype == P.RT.DOWNLOAD_SETTINGS:
            returns.append(P.build_download_settings_response())
            log("      -> DOWNLOAD_SETTINGS answered")
        elif rtype == P.RT.GET_ASSET_DIGEST:
            plat = P.parse_platform(msg)
            r = P.build_get_asset_digest_response(plat)
            returns.append(r)
            log(f"      -> asset digest [{plat}]: {len(P._our_bundles(plat))} bundles"
                + ("\n" + pb.pretty(msg) if VERBOSE else ""))
        elif rtype == P.RT.GET_DOWNLOAD_URLS:
            ids = P.parse_get_download_urls(msg)
            returns.append(P.build_get_download_urls_response(ids, ASSET_BASE_URL))
            log(f"      -> GET_DOWNLOAD_URLS for {ids} -> {ASSET_BASE_URL}/...")
        elif rtype == P.RT.DOWNLOAD_ITEM_TEMPLATES:
            r = P.build_download_item_templates_response()
            returns.append(r)
            log(f"      -> game master sent ({len(r):,} bytes)"
                + ("\n" + pb.pretty(msg) if VERBOSE else ""))
        elif rtype == P.RT.FORT_DETAILS:
            fid, flat, flng = P.parse_fort_request(msg)
            returns.append(P.build_fort_details_response(fid, flat, flng))
            log(f"      -> FORT_DETAILS {fid!r}")
        elif rtype == P.RT.GET_GYM_DETAILS:
            fid, glat, glng = P.parse_gym_details(msg)
            if fid:
                _user_gym[username] = fid
            import world
            returns.append(P.build_gym_details_response(
                fid, glat or here[0], glng or here[1], int(time.time() * 1000)))
            log(f"      -> GET_GYM_DETAILS {fid!r} ({len(world.gym_members(fid))} defenders)")
        elif rtype == P.RT.FORT_DEPLOY_POKEMON:
            fid, uid = P.parse_deploy(msg)
            returns.append(P.build_fort_deploy_response(
                fid, uid, here[0], here[1], int(time.time() * 1000)))
            import world
            log(f"      -> FORT_DEPLOY_POKEMON {fid!r} pokemon={uid} "
                f"-> {len(world.gym_members(fid))} at gym")
        elif rtype == P.RT.START_GYM_BATTLE:
            gid, atk_ids, def_id = P.parse_start_gym_battle(msg)
            if gid:
                _user_gym[username] = gid
                # Rivals guarding an unclaimed gym are worked out on the fly for
                # the map; make them real now so the battle, the prestige and
                # taking the gym all run through the normal path.
                import world as _wg
                _wg.ensure_npc_defenders(gid)
            r = P.build_start_gym_battle_response(gid, atk_ids, def_id,
                                                  int(time.time() * 1000))
            returns.append(r)
            if VERBOSE:
                # Full request+response dump, for spotting a 0.35 proto drift.
                try:
                    log("      [gymdbg] START req:" + chr(10) + pb.pretty(msg))
                    log("      [gymdbg] START resp:" + chr(10) + pb.pretty(r))
                except Exception as _e:
                    log(f"      [gymdbg] dump failed: {_e}")
            d = pb.decode(r); res = pb.get(d, 1, pb.WT_VARINT)
            bid = pb.get(d, 4, pb.WT_LEN)
            if res == 1 and bid:
                _research(username, "battle")
            import world
            b = world.BATTLES.get(bid.decode()) if bid else None
            log("      -> gym battle START: " +
                (f"begun ({len(atk_ids)} attacker(s))"
                 + (f", your #{b['atk_pid']} (CP {b['atk_cp']}) vs their "
                    f"#{b['def_pid']} (CP {b['def_cp']})" if b else "")
                 if res == 1 and bid else
                 {5: "the gym is empty",
                  8: "no Pokemon fit to fight (all fainted, or your only Pokemon is "
                     "the one guarding this gym)"}.get(res, f"couldn't start (code {res})")))
        elif rtype == P.RT.ATTACK_GYM:
            gid, bid, actions, last_seen = P.parse_attack_gym(msg)
            if VERBOSE:
                try:
                    log("      [gymdbg] ATTACK req:" + chr(10) + pb.pretty(msg))
                except Exception:
                    pass
            r = P.build_attack_gym_response(gid, bid, actions,
                                            int(time.time() * 1000), last_seen)
            returns.append(r)
            d = pb.decode(r)
            lg = pb.decode(pb.get(d, 2, pb.WT_LEN) or b"")
            state = pb.get(lg, 1, pb.WT_VARINT)
            rid = pb.get(d, 3, pb.WT_LEN)
            import world
            now = int(time.time() * 1000)
            b = world.BATTLES.get(bid)
            outcome = {1: "trading blows", 2: "VICTORY -- gym taken!",
                       3: "all your Pokemon fainted",
                       4: "you left the battle"}.get(state, f"state {state}")
            if state == 2 and b and b.get("next_defender"):
                outcome = "defender beaten -- next one up"
            hp = (f" (your HP {max(0, b['atk_hp'])}/{b['atk_max']}, "
                  f"their HP {max(0, b['def_hp'])}/{b['def_max']})" if b else "")
            prestige = ""
            if b and b.get("gym_result"):
                gr = b["gym_result"]
                prestige = (f"  gym prestige now {gr[0]:,} (level {gr[1]})"
                            + (f", {gr[2]} sent home" if gr[2] else ""))
            log(f"      -> gym battle: {len(actions)} tap(s) -> {outcome}{hp}{prestige}")
            if VERBOSE:
                out = [pb.decode(a) for a in pb.get_all(lg, 4) if isinstance(a, bytes)]
                offs = [(pb.get(a, 2, pb.WT_VARINT) or 0) - now for a in out]
                log(f"         [gymdbg] known={b is not None} "
                    f"match={bool(rid) and rid.decode() == bid} "
                    f"out={len(out)} actions offset_ms={offs}")
        elif rtype == P.RT.COLLECT_DAILY_DEFENDER_BONUS:
            r = P.build_collect_daily_defender_bonus_response()
            returns.append(r)
            d = pb.decode(r)
            res = pb.get(d, 1, pb.WT_VARINT)
            got = pb.get_all(d, 3)
            gyms = pb.get(d, 4, pb.WT_VARINT) or 0
            log("      -> COLLECT_DAILY_DEFENDER_BONUS -> "
                + {1: f"paid {got} for {gyms} gym(s)", 3: "too soon (once a day)",
                   4: "no gyms being defended"}.get(res, f"result {res}"))
        elif rtype == P.RT.USE_ITEM_XP_BOOST:
            iid = P.parse_use_item_xp_boost(msg)
            r = P.build_use_item_xp_boost_response(iid)
            returns.append(r)
            res = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            log("      -> USE_ITEM_XP_BOOST -> " +
                {1: "Lucky Egg active: double XP", 3: "one is already running",
                 4: "you have none"}.get(res, f"result {res}"))
        elif rtype == P.RT.USE_INCENSE:
            r = P.build_use_incense_response(P.parse_use_incense(msg))
            returns.append(r)
            res = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            log("      -> USE_INCENSE -> " +
                {1: "Incense burning: more Pokemon nearby",
                 2: "one is already burning",
                 3: "you have none"}.get(res, f"result {res}"))
        elif rtype == P.RT.GET_INCENSE_POKEMON:
            r = P.build_get_incense_pokemon_response()
            returns.append(r)
            d = pb.decode(r)
            if pb.get(d, 1, pb.WT_VARINT) == 1:       # only when one is there: this rides
                _ie = pb.get(d, 6, pb.WT_64)          # on every request while it burns
                if _incense_logged.get(username) != _ie:
                    _incense_logged[username] = _ie
                    log(f"      -> GET_INCENSE_POKEMON -> #{pb.get(d, 2, pb.WT_VARINT)} "
                        f"came to the Incense (encounter {_ie})")
        elif rtype == P.RT.ADD_FORT_MODIFIER:
            iid, fid, mlat, mlng = P.parse_add_fort_modifier(msg)
            r = P.build_add_fort_modifier_response(
                iid, fid, int(time.time() * 1000),
                mlat or here[0], mlng or here[1])
            returns.append(r)
            res = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            log(f"      -> ADD_FORT_MODIFIER {fid[:12]}... -> " +
                {1: "Lure attached", 2: "that stop already has one",
                 4: "you have no Lure Modules"}.get(res, f"result {res}"))
        elif rtype == P.RT.USE_ITEM_CAPTURE:
            iid, eid = P.parse_use_item_capture(msg)
            r = P.build_use_item_capture_response(iid, eid)
            returns.append(r)
            ok = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            if ok:
                _research(username, "berry")
            log(f"      -> USE_ITEM_CAPTURE item={iid} encounter={eid} -> " +
                ("Razz Berry used, next ball is much likelier to hold"
                 if ok else "couldn't use that item"))
        elif rtype == P.RT.SET_AVATAR:
            look = P.parse_set_avatar(msg)
            returns.append(P.build_set_avatar_response(look, username))
            log("      -> SET_AVATAR " + (", ".join(f"{k}={v}" for k, v in look.items())
                                          if look else "(nothing sent)"))
        elif rtype == P.RT.MARK_TUTORIAL_COMPLETE:
            done = P.parse_mark_tutorial(msg)         # handles packed steps
            import world
            world.mark_tutorial(done)                 # persist before we reply
            returns.append(P.build_mark_tutorial_complete_response(username))
            log(f"      -> MARK_TUTORIAL_COMPLETE {done or 'all'} "
                f"(done now: {world.tutorial_steps()})")
        elif rtype == P.RT.ENCOUNTER_TUTORIAL_COMPLETE:
            pid = P.parse_encounter_tutorial_complete(msg)
            returns.append(P.build_encounter_tutorial_complete_response(pid))
            log(f"      -> ENCOUNTER_TUTORIAL_COMPLETE starter #{pid} caught")
        elif rtype == P.RT.CLAIM_CODENAME:
            name = P.parse_claim_codename(msg)
            r = P.build_claim_codename_response(name, username)
            returns.append(r)
            st = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            log(f"      -> CLAIM_CODENAME {name!r} -> " +
                {1: "claimed", 2: "already taken", 3: "not valid"}.get(st, str(st)))
        elif rtype == P.RT.GET_SUGGESTED_CODENAMES:
            returns.append(P.build_suggested_codenames_response(username))
            log("      -> GET_SUGGESTED_CODENAMES answered")
        elif rtype == P.RT.CHECK_CODENAME_AVAILABLE:
            name = P.parse_claim_codename(msg)
            returns.append(P.build_check_codename_available_response(name))
            log(f"      -> CHECK_CODENAME_AVAILABLE {name!r}")
        elif rtype == P.RT.SET_PLAYER_TEAM:
            team = P.parse_set_player_team(msg)
            r = P.build_set_player_team_response(team, username)
            returns.append(r)
            st = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            names = {1: "Mystic (blue)", 2: "Valor (red)", 3: "Instinct (yellow)"}
            log(f"      -> SET_PLAYER_TEAM -> " +
                (f"joined {names.get(team, team)}" if st == 1 else
                 "team was already chosen" if st == 2 else "failed"))
        elif rtype == P.RT.GET_HATCHED_EGGS:
            import world
            for h in world.check_hatches(P.hatch_species):
                _research(username, "hatch", pokemon_id=h["pokemon_id"])
                log(f"   [egg] a {h['km']:g} km egg hatched into #{h['pokemon_id']} "
                    f"CP{h['cp']} (+{h['xp']} XP, +{h['candy']} candy, "
                    f"+{h['stardust']} stardust)")
            returns.append(P.build_get_hatched_eggs_response())
        elif rtype == P.RT.USE_ITEM_EGG_INCUBATOR:
            inc_id, egg_uid = P.parse_use_item_egg_incubator(msg)
            r = P.build_use_item_egg_incubator_response(inc_id, egg_uid)
            returns.append(r)
            res = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            log(f"      -> USE_ITEM_EGG_INCUBATOR {inc_id!r} egg={egg_uid} -> " +
                {1: "incubating", 2: "no such incubator", 3: "no such egg",
                 4: "that isn't an egg", 5: "incubator already in use",
                 6: "egg already incubating",
                 7: "incubator has no uses left"}.get(res, f"result {res}"))
        elif rtype in (P.RT.USE_ITEM_POTION, P.RT.USE_ITEM_REVIVE):
            iid, uid = P.parse_use_item(msg)
            revive = rtype == P.RT.USE_ITEM_REVIVE
            r = (P.build_use_item_revive_response(iid, uid) if revive
                 else P.build_use_item_potion_response(iid, uid))
            returns.append(r)
            d = pb.decode(r)
            res = pb.get(d, 1, pb.WT_VARINT)
            hp = pb.get(d, 2, pb.WT_VARINT)
            log(f"      -> {'USE_ITEM_REVIVE' if revive else 'USE_ITEM_POTION'} "
                f"item={iid} pokemon={uid} -> " +
                (f"healed to {hp} HP" if res == 1 else
                 {2: "no such Pokemon", 3: "cannot use it on that Pokemon",
                  4: "it's defending a gym"}.get(res, f"result {res}")))
        elif rtype == P.RT.RELEASE_POKEMON:
            uid = P.parse_pokemon_id(msg)
            was = _mon(uid)                  # read first: it's gone afterwards
            r = P.build_release_response(uid)
            returns.append(r)
            res = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            if res == 1:
                _research(username, "transfer")
            log(f"      -> RELEASE_POKEMON {uid} {was} -> " +
                {1: "transferred (+1 candy)", 2: "REFUSED: it's at a gym",
                 3: "FAILED"}.get(res, str(res)))
        elif rtype == P.RT.UPGRADE_POKEMON:
            uid = P.parse_pokemon_id(msg)
            r = P.build_upgrade_response(uid)
            returns.append(r)
            res = pb.get(pb.decode(r), 1, pb.WT_VARINT)
            if res == 1:
                _research(username, "power_up")
            log(f"      -> UPGRADE_POKEMON {uid} {_mon(uid)} -> " +
                {1: "powered up", 2: "not found", 3: "not enough candy/stardust",
                 5: "it's at a gym"}.get(res, str(res)))
        elif rtype == P.RT.EVOLVE_POKEMON:
            uid = P.parse_pokemon_id(msg)
            was = _mon(uid)
            r = P.build_evolve_response(uid)
            returns.append(r)
            d = pb.decode(r); res = pb.get(d, 1, pb.WT_VARINT)
            if res == 1:
                try:
                    _research(username, "evolve", pokemon_id=int(was.split()[0][1:]) if was else 0)
                except ValueError:
                    _research(username, "evolve")
            log(f"      -> EVOLVE_POKEMON {uid} {was} -> " +
                {1: f"evolved! +{pb.get(d,3,pb.WT_VARINT)} xp", 2: "missing",
                 3: "not enough candy", 4: "cannot evolve",
                 5: "it's at a gym"}.get(res, str(res)))
        elif rtype == P.RT.NICKNAME_POKEMON:
            f = pb.decode(msg)
            uid = pb.get(f, 1, pb.WT_64) or 0
            nick = pb.get(f, 2, pb.WT_LEN) or b""
            returns.append(P.build_nickname_response(uid, nick.decode("utf-8", "replace")))
            log(f"      -> NICKNAME_POKEMON {uid} {_mon(uid)} = "
                f"{nick.decode('utf-8','replace')!r}")
        elif rtype == P.RT.SET_FAVORITE_POKEMON:
            f = pb.decode(msg)
            uid = pb.get(f, 1, pb.WT_64) or pb.get(f, 1, pb.WT_VARINT) or 0
            fav = bool(pb.get(f, 2, pb.WT_VARINT))
            returns.append(P.build_favorite_response(uid, fav))
            log(f"      -> SET_FAVORITE_POKEMON {uid} {_mon(uid)} fav={fav}")
        elif rtype == P.RT.RECYCLE_INVENTORY_ITEM:
            iid, cnt = P.parse_recycle(msg)
            returns.append(P.build_recycle_response(iid, cnt))
            log(f"      -> RECYCLE_INVENTORY_ITEM item={iid} x{cnt} (dropped from bag)")
        elif rtype == P.RT.GET_PLAYER_PROFILE:
            r = P.build_player_profile_response(int(time.time() * 1000))
            returns.append(r)
            got = P.badge_progress()
            earned = [f"{P.badge_name(bt)} {'I' * rank}"
                      for bt, rank, _lo, _hi, _c in got if rank]
            log(f"      -> GET_PLAYER_PROFILE: {len(got)} medals"
                + (f", earned: {', '.join(earned)}" if earned else ", none earned yet"))
        elif rtype == P.RT.CHECK_AWARDED_BADGES:
            r = P.build_check_awarded_badges_response()
            returns.append(r)
            d = pb.decode(r)
            new = pb.get(d, 2, pb.WT_LEN)
            if new:
                log("      -> CHECK_AWARDED_BADGES: new medal(s) awarded")
        elif rtype == P.RT.LEVEL_UP_REWARDS:
            import world
            lvl = P.parse_level_up_rewards(msg) or world.stats()[0]
            already = world.level_claimed(lvl)
            returns.append(P.build_level_up_rewards_response(lvl))
            log(f"      -> LEVEL_UP_REWARDS level {lvl}: "
                + ("AWARDED_ALREADY (no popup)" if already else "SUCCESS, items granted"))
        elif rtype == P.RT.FORT_SEARCH:
            fid, _, _ = P.parse_fort_request(msg)
            import anticheat
            if anticheat.too_fast(username, "spin"):
                # FortSearchResult 3 = IN_COOLDOWN_PERIOD: "try again later", no items
                returns.append(pb.Writer().uint(1, 3).to_bytes())
                log(f"      -> FORT_SEARCH {fid!r} SPEED LOCK "
                    f"({anticheat.speed_kmh(username) / anticheat.MPH:.0f} mph)")
                continue
            r = P.build_fort_search_response(fid, int(time.time() * 1000))
            returns.append(r)
            d = pb.decode(r)
            got = []
            for a in pb.get_all(d, 2):                 # ItemAward{item_id, count}
                if isinstance(a, bytes):
                    ad = pb.decode(a)
                    got.append(f"item{pb.get(ad, 1, pb.WT_VARINT)}"
                               f"x{pb.get(ad, 2, pb.WT_VARINT) or 1}")
            res = pb.get(d, 1, pb.WT_VARINT)
            if res == 1:
                _research(username, "spin")
            log(f"      -> FORT_SEARCH {fid!r} "
                + (f"got [{' '.join(got)}] +{pb.get(d, 5, pb.WT_VARINT) or 0}xp"
                   if res == 1 else "BAG FULL" if res == 4 else f"result {res}"))
        elif rtype == P.RT.ENCOUNTER:
            eid = P.parse_encounter(msg)
            returns.append(P.build_encounter_response(eid, int(time.time() * 1000)))
            import world
            s = world.get_spawn(eid)
            if s:
                import shiny as _shiny
                if _shiny.note_encounter(username, eid, s["pokemon_id"]):
                    log(f"      *** SHINY #{s['pokemon_id']} (encounter {eid}) ***")
            log(f"      -> ENCOUNTER {eid} -> " +
                (f"pokemon #{s['pokemon_id']} cp{s['cp']} (catch screen)"
                 if s else "NOT_FOUND (unknown spawn)"))
        elif rtype == P.RT.CATCH_POKEMON:
            eid, ball, hit, reticle, spin, hitpos = P.parse_catch(msg)
            import anticheat
            if anticheat.too_fast(username, "catch"):
                # CatchStatus 2 = ESCAPE: the ball breaks free; the encounter goes on
                returns.append(pb.Writer().uint(1, 2).double(2, 0.0).to_bytes())
                log(f"      -> CATCH_POKEMON {eid} SPEED LOCK "
                    f"({anticheat.speed_kmh(username) / anticheat.MPH:.0f} mph)")
                continue
            # Read BEFORE building the response -- the response consumes it.
            import world as _w
            berry = _w.berry_mult(eid)
            _sp = _w.get_spawn(eid)
            returns.append(P.build_catch_pokemon_response(
                eid, ball, hit, int(time.time() * 1000), reticle, spin, hitpos))
            try:
                _st = pb.get(pb.decode(returns[-1]), 1, pb.WT_VARINT)
                if _st in (1, 3):                   # caught or fled: the encounter is over
                    import shiny as _shiny
                    _shiny.end_encounter(username)
            except Exception:
                pass
            b = P.throw_bonus(reticle, spin, hitpos)
            _cr = pb.decode(returns[-1])
            st = pb.get(_cr, 1, pb.WT_VARINT)
            # The Journal lists catches and Pokemon that ran away (not break-outs
            # or misses, which the encounter simply continues after).
            if _sp and st == 1:
                _pid = int(_sp["pokemon_id"])
                if P.is_ditto(eid, _pid):
                    _pid = P.DITTO                   # it was a Ditto all along
                    log(f"   [ditto] that #{_sp['pokemon_id']} was a Ditto!")
                _research(username, "catch", pokemon_id=_pid)
            if _sp and hit and st in (1, 2, 3):          # the game counts throws that land
                if b:
                    _research(username, "throw", throw=b[1])
                try:
                    if spin and spin >= P._threshold("spin_bonus_threshold", P.ENC_SPIN_BONUS, 0.5):
                        _research(username, "curveball")
                except Exception:
                    pass
            if _sp and st in (1, 3):
                _uid = int(pb.get(_cr, 3, pb.WT_64) or pb.get(_cr, 3, pb.WT_VARINT) or 0)
                # What was actually caught -- a disguised Ditto is logged as Ditto.
                _got = (_w.get_caught(_uid) if st == 1 and _uid else None) or _sp
                _w.log_action({"kind": "catch", "result": 1 if st == 1 else 2,
                               "pokemon_id": int(_got["pokemon_id"]),
                               "cp": int(_got.get("cp", 0)),
                               "uid": _uid})
            log(f"      -> CATCH_POKEMON {eid} ball={ball} hit={hit} "
                f"reticle={reticle:.2f} hitpos={hitpos:.3f} spin={spin:.2f}"
                + (f" berry=x{berry:.1f}" if berry and berry > 1.0 else "")
                + " -> "
                + {1: "CAUGHT", 2: "broke out", 3: "fled",
                   4: "missed"}.get(st, f"status {st}")
                + (f" ({b[1]} throw! +{b[2]} XP)" if b and hit and st == 1 else "")
                + (f" after {_wobbles(_cr)} wobble(s)" if st in (2, 3) else ""))
        elif rtype == P.RT.FORT_RECALL_POKEMON:
            fid, uid, rlat, rlng = P.parse_fort_recall(msg)
            r = P.build_fort_recall_response(fid, uid, rlat or here[0],
                                             rlng or here[1])
            returns.append(r)
            ok = pb.get(pb.decode(r), 1, pb.WT_VARINT) == 1
            log(f"      -> FORT_RECALL_POKEMON {fid[:12]}... pokemon={uid} -> "
                + ("back in your box" if ok else "it wasn't at that gym"))
        elif rtype == P.RT.USE_ITEM_GYM:
            _iid, gid = P.parse_use_item_gym(msg)
            returns.append(P.build_use_item_gym_response(gid))
            log(f"      -> USE_ITEM_GYM {gid[:12]}... -> SUCCESS")
        elif rtype == P.RT.COLLECT_DAILY_BONUS:
            returns.append(P.build_collect_daily_bonus_response())
            log("      -> COLLECT_DAILY_BONUS -> SUCCESS")
        elif rtype in (P.RT.INCENSE_ENCOUNTER, P.RT.DISK_ENCOUNTER):
            eid = P.parse_special_encounter(msg)
            r = P.build_special_encounter_response(eid)
            returns.append(r)
            ok = pb.get(pb.decode(r), 1, pb.WT_VARINT) == 1
            if ok:
                import world
                import shiny as _shiny
                s = world.get_spawn(eid)
                if s and _shiny.note_encounter(username, eid, s["pokemon_id"]):
                    log(f"      *** SHINY #{s['pokemon_id']} (encounter {eid}) ***")
            log(f"      -> {P.rt_name(rtype)} {eid} -> "
                + ("catch screen" if ok else "NOT_FOUND (unknown spawn)"))
        elif rtype == P.RT.EQUIP_BADGE:
            returns.append(P.build_equip_badge_response(msg))
            log("      -> EQUIP_BADGE -> SUCCESS")
        elif rtype == P.RT.SFIDA_ACTION_LOG:
            returns.append(P.build_action_log_response())
            import world as _wj
            log(f"      -> JOURNAL: {len(_wj.action_log())} entries")
        elif rtype == P.RT.ECHO:
            returns.append(P.build_echo_response())
            log("      -> ECHO")
        elif rtype == P.RT.DEBUG_UPDATE_INVENTORY:
            # A cheat request any client can send. Off unless the operator
            # starts the server with ALLOW_DEBUG_RPC=1 (items come from the
            # World Manager otherwise).
            if os.environ.get("ALLOW_DEBUG_RPC") == "1":
                returns.append(P.build_debug_update_inventory_response(msg))
                log("      -> DEBUG_UPDATE_INVENTORY -> items granted")
            else:
                returns.append(pb.Writer().bool_(1, False).to_bytes())
                log("      -> DEBUG_UPDATE_INVENTORY -> REFUSED (debug requests are off)")
        elif rtype == P.RT.DEBUG_DELETE_PLAYER:
            returns.append(P.build_debug_delete_player_response())
            log("      -> DEBUG_DELETE_PLAYER -> REFUSED (saves are never wiped)")
        elif rtype == P.RT.PLAYER_UPDATE:
            ulat, ulng = P.parse_player_update(msg)
            if ulat or ulng:
                _last_loc[0], _last_loc[1] = ulat, ulng
                here = (ulat, ulng)
                import world as _wl
                _wl.set_player_location(ulat, ulng, username)
            # Nearby Pokemon/forts already come from GET_MAP_OBJECTS; an empty
            # PlayerUpdateOutProto means "nothing extra".
            returns.append(b"")
            log(f"      -> PLAYER_UPDATE @ ({ulat:.5f},{ulng:.5f})")
        else:
            returns.append(b"")            # placeholder; client tolerates empties
            log(f"      -> {P.rt_name(rtype)} (#{rtype}) empty response")
    return returns


def handle(method, path, query, headers, body, log):
    # Asset bundle download: the client GETs the URL we returned from
    # GET_DOWNLOAD_URLS (https://pgorelease.nianticlabs.com/asset/pm0001).
    if method == "GET" and path.startswith("/asset/"):
        asset_id = path[len("/asset/"):]
        fp = P.bundle_path(asset_id)
        if fp:
            data = open(fp, "rb").read()
            log(f"[asset] SERVING {asset_id} ({len(data)} bytes) to client")
            return 200, {"Content-Type": "application/octet-stream"}, data
        log(f"[asset] 404 unknown asset {asset_id!r}")
        return 404, {"Content-Type": "text/plain"}, b"no such asset"

    # PokeStop / Gym photos: files the user dropped in photos/ next to the server,
    # referenced from FortDetailsResponse.image_urls.
    if method == "GET" and path.startswith("/fortimg/"):
        name = os.path.basename(path[len("/fortimg/"):])
        if name == P.DEFAULT_FORT_IMAGE:            # built-in placeholder photo
            data = P.default_fort_png()
            return 200, {"Content-Type": P.image_content_type(data)}, data
        fp = os.path.join(P.PHOTO_DIR, name)
        if name and os.path.isfile(fp):
            data = open(fp, "rb").read()
            ext = os.path.splitext(name)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")
            log(f"[photo] SERVING {name} ({len(data)} bytes)")
            return 200, {"Content-Type": ctype}, data
        log(f"[photo] 404 {name!r} (put it in the photos/ folder)")
        return 404, {"Content-Type": "text/plain"}, b"no such photo"

    if method != "POST" or not body:
        return 405, {"Content-Type": "text/plain"}, b"method not allowed"

    try:
        request_id, reqs, fields = P.parse_request_envelope(body)
    except Exception:
        return 400, {"Content-Type": "text/plain"}, b"bad request"
    username = P.resolve_username(fields)
    if username and getattr(_req_old, "value", False):
        # A launcher older than the public server's launcher.min_build (online.py):
        # back to the login, which tells them to update.
        log(f"[rpc] REFUSED: {username!r} is on an old launcher -> update needed")
        try:
            import online
            online.note_outdated(username, getattr(_req_ip, "value", None))
        except Exception:
            pass
        return 200, {"Content-Type": "application/binary"}, P.build_response_envelope(
            status_code=P.STATUS_INVALID_AUTH, request_id=request_id)
    if not username:
        # No token or ticket that THIS server signed: not signed in. 102 sends
        # the client back through the login (which is where the password is
        # checked) instead of letting it play as whatever name it claims.
        log("[rpc] REFUSED: no valid login on this request -> client must sign in again")
        return 200, {"Content-Type": "application/binary"}, P.build_response_envelope(
            status_code=P.STATUS_INVALID_AUTH, request_id=request_id)
    try:
        import siteaccounts as _sa
        _gone = _sa.refused(username)
    except Exception:
        _gone = None
    if _gone:
        # Banned on the website, or a reset/deleted save: the token the game
        # kept is no good any more -- back to the login, where the website says no.
        log(f"[rpc] REFUSED: {username!r} is {_gone} on the website")
        return 200, {"Content-Type": "application/binary"}, P.build_response_envelope(
            status_code=P.STATUS_INVALID_AUTH, request_id=request_id)
    try:
        import world as _w
        _why = _w.phone_refused(username)
    except Exception:
        _why = None
    if _why:
        # A one-phone (beta) account, and this isn't its phone.
        log(f"[rpc] REFUSED: {username!r} is locked to one phone ({_why})")
        return 200, {"Content-Type": "application/binary"}, P.build_response_envelope(
            status_code=P.STATUS_INVALID_AUTH, request_id=request_id)
    _last_user[0] = username
    _ip = getattr(_req_ip, "value", None)
    if _ip:
        _remember_ip(_ip, username)

    # A blank line + this header frame each request batch, so the log reads as a
    # sequence of "the game asked for X, we answered Y" rather than one endless
    # stream. The per-request answers below are indented under it.
    log("")
    if reqs:
        log(f"[{username}]  " + ", ".join(P.rt_name(t) for t, _ in reqs))

    # track the player's reported location (envelope #7/#8); remember last good
    ll = _envelope_latlng(fields)
    if ll and (abs(ll[0]) > 1e-6 or abs(ll[1]) > 1e-6):
        _last_loc[0], _last_loc[1] = ll
        try:
            import world as _w
            _w.use(username)
            _w.set_player_location(ll[0], ll[1], username)
            import anticheat
            anticheat.observe(username, ll[0], ll[1])
            if anticheat.too_fast(username, "walk"):
                _w.current().LAST_POS = (ll[0], ll[1])     # too fast: this stretch doesn't count
            else:
                _w.add_distance(ll[0], ll[1])
        except Exception:
            pass
        # The phone reports its position on EVERY request (a few a second), so
        # only note it when it has actually moved -- otherwise it drowns the log.
        if _moved_far(ll, _logged_loc.get(username, (0.0, 0.0))):
            _logged_loc[username] = ll
            log(f"   [loc] now at {ll[0]:.5f},{ll[1]:.5f}")

    # (verbose) the raw envelope, for the first few batches only.
    if VERBOSE and reqs and _dump_budget[0] > 0:
        _dump_budget[0] -= 1
        log("   raw envelope:\n" + pb.pretty(body))

    # Echo the host the client actually reached us on, so the bootstrap redirect
    # and every api_url keep the client on THAT host. Essential for patched, no-VPN
    # clients (baked to e.g. bracky.playit.plus): redirecting them to pgorelease
    # would strand them. Unchanged for DNS/VPN clients -- they arrive as pgorelease
    # and get pgorelease back.
    api_host = (headers.get("Host") or "").strip()
    if not _HOST_OK.match(api_host):            # never echo junk into api_url
        api_host = API_HOST

    # Bootstrap handshake: tell the client which host to use from now on.
    # A patched client's RPC base is padded to fill the fixed 32-byte Unity slot
    # (e.g. "/plfe000000000"), so its first call is "/plfe000000000/rpc", not the
    # bare "/plfe/rpc". Accept any /plfe*/rpc that isn't the post-redirect api path.
    if path.startswith("/plfe") and path.endswith("/rpc") and "/0/rpc" not in path:
        env = P.build_response_envelope(
            status_code=P.STATUS_REDIRECT,
            request_id=request_id,
            api_url=f"{api_host}{API_PATH}",
        )
        log(f"   -> handshake redirect to {API_HOST}{API_PATH}")
        return 200, {"Content-Type": "application/binary"}, env

    # In-game Shop PURCHASE: platform request type 2 carrying the item_id we
    # listed. Charge coins / grant the item server-side, then ack.
    if not reqs:
        _buy_id = P.buy_item_id(fields)
        if _buy_id:
            try:
                import shop as _shop
                import world as _w
                _w.use(username)
                ok, msg = _shop.purchase(_buy_id)
                env = P.build_response_envelope(
                    status_code=P.STATUS_OK,
                    request_id=request_id,
                    unknown6=_shop.build_buy_response(ok),
                    api_url=f"{api_host}{API_PATH}",
                    auth_ticket=P.build_auth_ticket(username),
                )
                log(f"[shop] BUY {_buy_id} -> {'OK' if ok else 'NO'}: {msg} "
                    f"({_w.current().COINS} coins)")
                return 200, {"Content-Type": "application/binary"}, env
            except Exception as e:
                log(f"[shop] buy failed: {type(e).__name__}: {e}")

    # In-game Shop screen: an empty-requests envelope carrying platform request
    # type 5. Answer with the shop item list in the response's field-6 platform
    # response (NOT a normal RPC return). Items + live currencies come from shop.py.
    if not reqs and P.wants_shop(fields):
        try:
            import shop as _shop
            import world as _w
            _w.use(username)
            p = _w.current()
            u6 = _shop.build_platform_shop(p.COINS, p.STARDUST,
                                           USER_PLATFORM.get(username, "android"))
            env = P.build_response_envelope(
                status_code=P.STATUS_OK,
                request_id=request_id,
                unknown6=u6,
                api_url=f"{api_host}{API_PATH}",
                auth_ticket=P.build_auth_ticket(username),
            )
            log(f"[shop] -> in-game shop ({len(_shop.CATALOGUE)} items, "
                f"{p.COINS} coins)")
            return 200, {"Content-Type": "application/binary"}, env
        except Exception as e:
            log(f"[shop] in-game shop failed: {type(e).__name__}: {e}")

    # Normal handling.
    _t0 = time.perf_counter()
    returns = _build_returns(reqs, username, log)
    env = P.build_response_envelope(
        status_code=P.STATUS_OK,
        request_id=request_id,
        returns=returns,
        api_url=f"{api_host}{API_PATH}",
        auth_ticket=P.build_auth_ticket(username),
    )
    log(f"   ({len(returns)} answered in {(time.perf_counter() - _t0) * 1000:.0f} ms)")
    return 200, {"Content-Type": "application/binary"}, env
