"""
Who is asking: signed login tokens, and the brakes on guessing passwords.

The login tokens used to be `PTC.<base64 username>.<random>` -- nothing tied
them to this server, so anyone could write one for any trainer and the RPC
believed it. Every token and auth ticket is now signed (HMAC-SHA256) with a
key that never leaves data/secret.key, carries its own expiry, and is checked
on every request.

    token  = auth.issue("a", "Bracky68", 30 * 86400)
    name   = auth.verify(token, "a")          # None if forged, expired, wrong kind

Kinds: "t" = the short-lived service ticket the login hands out (`ST-...`),
       "a" = the access token the game then carries in auth_info.
"""
import base64
import hashlib
import hmac
import os
import threading
import time

import datadir

TICKET_TTL = 5 * 60                 # login -> token exchange happens at once
ACCESS_TTL = 30 * 24 * 3600         # the game is told 2 h; honour a stored one longer

_lock = threading.Lock()
_key_bytes = None


def _key():
    """This server's signing key: 32 random bytes in data/secret.key (0600)."""
    global _key_bytes
    if _key_bytes:
        return _key_bytes
    with _lock:
        if _key_bytes:
            return _key_bytes
        path = datadir.path("secret.key")
        try:
            with open(path, "rb") as fh:
                key = bytes.fromhex(fh.read().decode("ascii").strip())
        except (OSError, ValueError):
            key = b""
        if len(key) < 32:
            key = os.urandom(32)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    fh.write(key.hex())
            except OSError as e:
                # Still safe, just not durable: everyone signs in again after a restart.
                print(f"[auth] could not save secret.key ({e}); logins won't "
                      f"survive a restart", flush=True)
        _key_bytes = key
        return key


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mac(data):
    """HMAC-SHA256 of `data` under this server's key (32 raw bytes)."""
    return hmac.new(_key(), data, hashlib.sha256).digest()


def issue(kind, username, ttl):
    body = "PTC.%s.%s.%d.%s" % (kind, _b64(username.encode("utf-8")),
                                int(time.time()) + int(ttl), os.urandom(8).hex())
    return body + "." + _b64(mac(body.encode("ascii")))


def _parse(token, kind):
    """(username, expiry, nonce) for a token we signed that is still valid."""
    try:
        if not token or len(token) > 512:
            return None
        body, _, sig = token.rpartition(".")
        if not hmac.compare_digest(_b64(mac(body.encode("ascii"))), sig):
            return None
        magic, k, name, exp, nonce = body.split(".")
        if magic != "PTC" or k != kind or int(exp) < time.time():
            return None
        return _unb64(name).decode("utf-8"), int(exp), nonce
    except (ValueError, UnicodeError):
        return None


def verify(token, kind):
    """The trainer a token was issued to, or None."""
    got = _parse(token, kind)
    return got[0] if got else None


# ---------------------------------------------------------------- throttling
# Failed sign-ins are counted per address and per trainer name. The address
# limit is what stops a guesser; the (looser) name limit covers a guesser who
# keeps changing address, without letting one stranger lock a trainer out for long.
WINDOW = 10 * 60
MAX_FAILS_IP = 10
MAX_FAILS_NAME = 30
MAX_CLAIMS_IP = 5                   # brand-new accounts per address ...
CLAIM_WINDOW = 24 * 3600            # ... per day

_events = {}                        # key -> [timestamps]


def client_ip():
    """The address the request being handled came from (set by server.py)."""
    import sys
    rpc = sys.modules.get("rpc")
    return getattr(getattr(rpc, "_req_ip", None), "value", None) if rpc else None


def is_local(ip):
    return not ip or ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _count(key, window):
    now = time.time()
    with _lock:
        hits = [t for t in _events.get(key, ()) if now - t < window]
        if hits:
            _events[key] = hits
        else:
            _events.pop(key, None)
        return len(hits)


def _note(key):
    with _lock:
        if len(_events) > 50000:            # a flood of addresses can't eat the RAM
            _events.clear()
        _events.setdefault(key, []).append(time.time())


def login_blocked(username, ip):
    """True when this address or this name has failed too often lately."""
    if not is_local(ip) and _count("ip:" + ip, WINDOW) >= MAX_FAILS_IP:
        return True
    return _count("name:" + (username or "").lower(), WINDOW) >= MAX_FAILS_NAME


def login_failed(username, ip):
    if not is_local(ip):
        _note("ip:" + ip)
    _note("name:" + (username or "").lower())


def claim_allowed(ip):
    return is_local(ip) or _count("claim:" + ip, CLAIM_WINDOW) < MAX_CLAIMS_IP


def claimed(ip):
    if not is_local(ip):
        _note("claim:" + ip)
