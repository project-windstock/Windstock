"""
Logins that live on the Windstock website.

On the public server a player registers on the website (email + password) and
signs in to the game with the same two. This module is the game server's side
of that: check() asks the website whether an email and password are right, and
a background thread sends each such trainer's stats to the website's
leaderboard, so nobody has to switch reporting on.

Off unless both are set in the environment (the VPS unit sets them):
  SITE_ACCOUNTS_URL   the website as seen from this machine, e.g. http://127.0.0.1:8787
  SITE_GAME_KEY       the website's GAME_KEY (16+ characters)
  SITE_PUBLIC_URL     optional: the address players type, for the "register
                      first" message (defaults to no address in the message)
  SITE_LOGINS         optional: 0 = leaderboard only. Logins stay as they are
                      (by name, checked here) and every trainer's stats still go
                      to the website, which gives name-login trainers a board
                      row of their own.

The phones' embedded server never sets these, so nothing changes there.

A website trainer's save is named by the handle the website gives out
("ws" + 8 hex digits), never by the email, and its password field holds PW_MARK:
no password matches it and it is not empty, so the save can't be signed in to
or claimed by name -- only through the website's check.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

PW_MARK = "site$-"
REPORT_EVERY = 20           # seconds between sweeps of the saves
_TIMEOUT = 5
GONE_DAYS = 31              # longer than a game token lives (auth.ACCESS_TTL)

# The game keeps its sign-in token (good for 30 days) and signs back in with it
# without asking for the password, so a ban or a deleted save has to be checked
# on every game request too (rpc.py -> refused()). _banned comes from the
# website on every sweep; _gone is every save deleted for the website, kept on
# disk for GONE_DAYS so an old token can't bring that trainer back.
_banned = set()
_gone = None                # save name -> when it was deleted (epoch s)
_gone_lock = threading.Lock()


def _key(name):
    import world
    return world._safe_name(name)


def _gone_file():
    import world
    return os.path.join(world.HERE, "site_gone.json")


def _load_gone():
    global _gone
    if _gone is None:
        try:
            with open(_gone_file(), encoding="utf-8") as fh:
                _gone = {str(k): float(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError, AttributeError):
            _gone = {}
    return _gone


def _mark_gone(name):
    with _gone_lock:
        gone = _load_gone()
        now = time.time()
        gone[_key(name)] = now
        for k in [k for k, t in gone.items() if now - t > GONE_DAYS * 86400]:
            del gone[k]
        try:
            tmp = _gone_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(gone, fh)
            os.replace(tmp, _gone_file())
        except OSError:
            pass


def refused(name):
    """"banned" / "deleted" when trainer `name` may no longer play here, else None."""
    if not enabled() or not name:
        return None
    key = _key(name)
    if key in _banned:
        return "banned"
    with _gone_lock:
        t = _load_gone().get(key)
    if t is not None and time.time() - t < GONE_DAYS * 86400:
        return "deleted"
    return None


def _conf():
    url = (os.environ.get("SITE_ACCOUNTS_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SITE_GAME_KEY") or "").strip()
    return (url, key) if url and len(key) >= 16 else None


def enabled():
    return _conf() is not None


def logins():
    """True when the website checks the logins (not just takes the stats)."""
    return enabled() and (os.environ.get("SITE_LOGINS") or "").strip() != "0"


def register_hint():
    """Where to register, for messages: ' at http://...' or ''."""
    site = (os.environ.get("SITE_PUBLIC_URL") or "").strip().rstrip("/")
    return f" at {site}/register.html" if site else " on the Windstock website"


def _post(path, payload):
    """(status, json) from the website; status 0 = could not reach it."""
    return _call(path, payload)


def _call(path, payload=None):
    """POST `payload` (or GET when None) to the website."""
    url, key = _conf()
    req = urllib.request.Request(
        url + path, method="GET" if payload is None else "POST",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Game-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return 0, {}


def check(email, password, ip):
    """(handle, None) when the website knows this email and password, else
    (None, why) with why one of world.login_message()'s reasons."""
    status, body = _post("/api/game/login",
                         {"email": email, "password": password, "ip": ip or ""})
    handle = str(body.get("handle") or "")
    if status == 200 and handle:
        return handle, None
    if status == 404:
        return None, "not registered"
    if status == 401:
        return None, "wrong password"
    if status == 429:
        return None, "too many attempts"
    if status == 403 and body.get("banned"):
        return None, "banned"
    return None, "site down"


# ---------------------------------------------------------------- leaderboard

def _last_ip(name):
    """Where this trainer last played from, or "". The website uses it to show a
    phone signed in to a profile there the rank of the trainer played on it."""
    try:
        import rpc
        return rpc.ip_for_user(name) or ""
    except Exception:
        return ""


def _stats(path):
    """What the leaderboard wants from one save, or None if there is nothing to
    send (unreadable, never signed in to, or nothing caught or spun yet).
    A website trainer goes by its handle, any other by its login name."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    stats = d.get("stats") or {}
    name = str(d.get("username") or "")
    if not name:
        return None
    try:
        out = {"trainer": str(d.get("codename") or "") or "Trainer",
               "team": max(0, min(3, int(d.get("team") or 0))),
               "level": max(1, min(50, int(d.get("level") or 1))),
               "caught": int(stats.get("pokemons_captured") or 0),
               "spun": int(stats.get("poke_stop_visits") or 0),
               "walked": round(float(stats.get("km_walked") or 0.0), 2),
               # anticheat.py: the website tags spoofers on the leaderboard
               "spoofer": bool(d.get("spoof"))}
    except (TypeError, ValueError):
        return None
    if d.get("pw") == PW_MARK:
        out["handle"] = name
    else:
        # No password: never signed in to over the network (a test save, or one
        # made on the host's own machine) -- not a player of this server.
        if not d.get("pw") or (not out["caught"] and not out["spun"]):
            return None
        out["login"] = name
        if not d.get("codename"):
            out["trainer"] = name           # the game shows the login name too
        ip = _last_ip(name)
        if ip:
            out["ip"] = ip
    return out


def _sweep(saves_dir, sent):
    """Report every trainer whose save changed since the last sweep.
    `sent` maps file name -> the mtime that was reported."""
    try:
        names = [f for f in os.listdir(saves_dir) if f.endswith(".json")]
    except OSError:
        return
    for fn in names:
        path = os.path.join(saves_dir, fn)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if sent.get(fn) == mtime:
            continue
        payload = _stats(path)
        if payload is None:
            sent[fn] = mtime            # nothing to send; look again when it changes
            continue
        status, _ = _post("/api/game/report", payload)
        # 404: the profile was deleted on the website; 400: a number it won't
        # take. Neither gets better by retrying before the save changes.
        if status in (200, 400, 404):
            sent[fn] = mtime


def _wipes(log):
    """Delete the saves the website has queued: profiles reset or deleted there
    (by their owner or an admin). Then tell it which are done."""
    global _banned
    status, body = _call("/api/game/wipes")
    if status != 200:
        return
    import world
    if isinstance(body.get("banned"), list):
        _banned = {_key(n) for n in body["banned"] if n}
    done = []
    for w in body.get("wipes") or []:
        name = str(w.get("name") or "").strip()
        if not name:
            continue
        _mark_gone(name)
        had = world.wipe_account(name)
        done.append(w.get("id"))
        log(f"[site] {w.get('why') or 'wipe'}: {name} " + ("save deleted" if had else "had no save"))
    if done:
        _post("/api/game/wipes/done", {"ids": done})


def start_reporter(saves_dir, log=print):
    """Start the leaderboard thread (does nothing when the feature is off)."""
    if not enabled():
        return False

    def loop():
        sent = {}
        while True:
            try:
                _wipes(log)
                _sweep(saves_dir, sent)
            except Exception as e:                      # never let it die
                log(f"[site] leaderboard report failed: {e}")
            time.sleep(REPORT_EVERY)

    threading.Thread(target=loop, daemon=True).start()
    return True


def standing(name, saves_dir):
    """The leaderboard as trainer `name` (whoever is logged in to the game on the
    asking phone) sees it: their place and the top of each board, the same
    shape as the website's /api/app/standing. None when the feature is off, the
    website can't be reached, or the trainer isn't on the board yet."""
    if not enabled() or not name:
        return None
    kind = "login"
    try:
        import world
        with open(os.path.join(saves_dir, world._safe_name(name) + ".json"), encoding="utf-8") as fh:
            if json.load(fh).get("pw") == PW_MARK:
                kind = "handle"
    except (OSError, ValueError, AttributeError):
        pass
    import urllib.parse
    status, body = _call("/api/game/standing?" + urllib.parse.urlencode({"login": name, "kind": kind}))
    return body if status == 200 and body.get("ok") else None
