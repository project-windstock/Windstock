"""
The online door: one public port that players reach by IP, with no DNS and no
certificate to install.

The Windstock launchers (iOS and Android) have a "Windstock Online" mode that
takes nothing but an address like 203.0.113.7:8888. The launcher runs a small
tunnel on the phone (tunnel.py): the game talks to 127.0.0.1 exactly as it
does to the server-on-this-phone, and the tunnel carries every request here
over TLS, checked against THIS server's own CA. That CA is what
/windstock/ca.crt hands out; a launcher keeps the one it saw first for an
address and refuses a different one later.

The port is TLS only (1.2+). The listener still looks at the first byte of
each connection, but only so that something speaking plain HTTP -- a launcher
from before the tunnel -- gets a readable "update your launcher" instead of a
dropped connection. Plain HTTP is served to loopback alone (a health check on
the same machine).

    import online; online.start(8888)        # run.py does this

The World Manager is NOT reachable here: it stays on 127.0.0.1.
"""
import json
import os
import threading

import netbase
import server

STATE = {"port": 0, "plain": 0, "tls": 0}


def _log(msg):
    print(f"[online] {msg}", flush=True)


class DualServer(netbase.DualServer):
    online_door = True               # server.Handler refuses plain HTTP from the network here
    counts = STATE
    log = staticmethod(_log)


def ca_pem():
    """The CA the server certificate chains to, as PEM text, or None."""
    path = os.path.join(server.CERT_DIR, "ca.crt")
    try:
        with open(path, "r", encoding="ascii") as f:
            return f.read()
    except OSError:
        return None


def min_build():
    """The launcher build the public server needs (settings launcher.min_build)."""
    try:
        import settings
        return max(0, int(settings.get("launcher", "min_build") or 0))
    except Exception:
        return 0


def update_url():
    try:
        import settings
        return str(settings.get("launcher", "update_url") or "")
    except Exception:
        return "https://windstock.org/downloads.html"


def outdated(headers, local=False):
    """True when the launcher asking (X-WS-Launcher, sent by tunnel.py; none =
    build 0) is older than min_build. Never for loopback (the phone's own
    server, health checks, Windbot)."""
    need = min_build()
    if local or not need:
        return False
    try:
        have = int((headers.get("X-WS-Launcher") or "0").strip() or 0)
    except (TypeError, ValueError, AttributeError):
        have = 0
    return have < need


def up_to_date(headers):
    """True when the request names a launcher build that meets min_build."""
    try:
        have = int((headers.get("X-WS-Launcher") or "0").strip() or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(have) and have >= min_build()


# Trainers / addresses whose game requests were just refused as out of date. The
# login may reach the server by another path (the port-80 sign-in bridge on a
# PC-hosted server carries no launcher build), so without this it would succeed
# and the game would loop back to the login forever instead of saying "update".
# Short-lived, and cleared as soon as that address shows an up-to-date launcher.
_refused = {}
REFUSED_TTL = 120


def note_outdated(username, ip):
    import time
    now = time.time()
    for k in (("u", (username or "").lower()), ("ip", ip or "")):
        if k[1]:
            _refused[k] = now


def recently_outdated(username, ip):
    import time
    now = time.time()
    for k in (("u", (username or "").lower()), ("ip", ip or "")):
        t = _refused.get(k)
        if k[1] and t and now - t < REFUSED_TTL:
            return True
    return False


def clear_outdated(ip):
    """That address now shows an up-to-date launcher (names expire on their own)."""
    _refused.pop(("ip", ip or ""), None)


UPDATE_TEXT = ("This Windstock is out of date. Get the new one at "
               "windstock.org/downloads to keep playing.")


def handle(method, path, local=False):
    """/windstock/* -- what a launcher asks before it trusts this address."""
    if path == "/windstock/players" and local:
        # How many trainers are playing. Loopback only: Windbot (windbot/) on
        # this machine asks; a count, never names.
        import rpc
        body = json.dumps({"ok": True, "players": rpc.online_count()}).encode()
        return 200, {"Content-Type": "application/json",
                     "Cache-Control": "no-store"}, body
    if path == "/windstock/events" and local:
        # What is on and what is coming, for Windbot's /events. Loopback only.
        import events
        body = json.dumps({"ok": True, **events.summary()}).encode()
        return 200, {"Content-Type": "application/json",
                     "Cache-Control": "no-store"}, body
    if path == "/windstock/standing":
        # The launcher's leaderboard, in Windstock Online mode only: the tunnel
        # marks the request with the phone's device id, and the trainer last
        # logged in to the game on that phone is who it is about -- no separate
        # sign-in. Off (404) wherever the website isn't wired up, which
        # includes every phone's own server.
        import rpc
        import siteaccounts
        import world
        name = rpc.user_for_device()
        if not siteaccounts.enabled():
            return 404, {"Content-Type": "application/json"}, b'{"ok":false,"error":"no leaderboard here"}'
        board = siteaccounts.standing(name, world.SAVES_DIR) if name else None
        if board is None:
            body = json.dumps({"ok": False, "signedIn": bool(name),
                               "error": "Not on the leaderboard yet." if name
                               else "Log in to the game first."}).encode()
            return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, body
        board["login"] = name
        return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, json.dumps(board).encode()
    if path == "/windstock/online":
        # "Test connection" in the launchers. Plain JSON, no player data.
        body = json.dumps({
            "ok": True,
            "server": "windstock",
            "plain": False,
            "tls": ca_pem() is not None,
            "port": STATE["port"],
            # The launcher build this server needs; the tunnel's "Test connection"
            # turns it into updateRequired, and Play into an Update button.
            "minBuild": min_build(),
            "updateURL": update_url(),
        }).encode()
        return 200, {"Content-Type": "application/json",
                     "Cache-Control": "no-store"}, body
    if path == "/windstock/ca.crt":
        pem = ca_pem()
        if pem is None:
            return 404, {"Content-Type": "text/plain"}, b"no CA on this server"
        return 200, {"Content-Type": "application/x-pem-file"}, pem.encode("ascii")
    return 404, {"Content-Type": "text/plain"}, b"?"


def start(port, bind="0.0.0.0"):
    """Serve server.Handler on `port` over TLS. Returns the httpd (None without a certificate)."""
    if not (os.path.exists(server.CERT) and os.path.exists(server.KEY)):
        # No certificate, no door: there is no unencrypted way in.
        _log("no TLS certificate (run gen_certs.py) -- the online port stays closed")
        return None
    httpd = DualServer((bind, port), server.Handler)
    # Only the launchers' tunnel dials this port, and it is modern: no TLS 1.0
    # here, unlike the game's own port.
    httpd.tls_ctx = netbase.tls_context(server.CERT, server.KEY, legacy=False)
    STATE["port"] = port
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="windstock-online").start()
    _log(f"listening on {bind}:{port} (TLS 1.2+ only)")
    fp = netbase.fingerprint(ca_pem() or "")
    if fp:
        # What a player's launcher log shows when it first pins this server.
        _log(f"certificate fingerprint (SHA-256): {fp}")
    return httpd
