"""
The launcher's side of Windstock Online: an encrypted tunnel to a server on
the internet.

The game cannot be taught to trust a certificate, and half of it (the login)
only speaks plain HTTP. So in Online mode the launcher runs THIS on the phone
instead of the game server, on the same two loopback ports:

    game  --plain or local TLS-->  127.0.0.1  --TLS 1.2+, pinned-->  ip:port

To the game it is the server-on-this-phone, which is the path that already
works. Everything that leaves the phone is inside TLS and is only sent to a
server whose certificate chains to the CA pinned for that address:

  * a CA listed in online_pins.py (shipped with the launcher), else
  * the CA this phone was given the FIRST time it connected to that address
    (kept in data/online_pins.json). A different one later is refused until
    the player chooses "Forget saved certificate" -- the operator re-made the
    server's certificate, or somebody is in the middle.

Every request also gets an X-WS-Device header: a random id made on this phone
and never shown to anyone but the server, inside TLS. A verified game login
ties it to a trainer, which is how the tweak's screens know who is asking
without going by IP address (two phones behind one router share that).

    tunnel.serve("203.0.113.7:8888")         # ws_boot does this; blocks
"""
import http.client
import json
import os
import secrets
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler

import datadir
import netbase

# The name the server's certificate is made out to (gen_certs.py). We dial an
# IP address, so this is what the certificate is checked against.
SERVER_NAME = "pgorelease.nianticlabs.com"
# This launcher's build, sent as X-WS-Launcher on every request. The server can
# require a newer one (settings launcher.min_build): the game's login then says
# "update", and the launcher's Play button opens the downloads page instead.
# Bump it whenever a release must replace the one before on the public server.
LAUNCHER_BUILD = 2
CONNECT_TIMEOUT = 12
READ_TIMEOUT = 60

STATE = {"target": None, "ctx": None, "error": None, "source": None}
_lock = threading.Lock()
_last_complaint = [0.0, ""]


def _log(msg):
    print(f"[tunnel] {msg}", flush=True)


def _complain(msg):
    """Log a failure, but not on every one of the game's several requests a second."""
    now = time.time()
    if msg != _last_complaint[1] or now - _last_complaint[0] > 30:
        _last_complaint[0], _last_complaint[1] = now, msg
        _log(msg)
    STATE["error"] = msg


def parse_target(text):
    """'1.2.3.4:8888' -> ('1.2.3.4', 8888); None unless an IPv4 literal and a port."""
    try:
        host, _, port = (text or "").strip().partition(":")
        socket.inet_aton(host)
        if host.count(".") != 3 or not 0 < int(port) < 65536:
            return None
        return host, int(port)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------- pinning
def _pins_file():
    return datadir.path("online_pins.json")


def _saved_pins():
    try:
        with open(_pins_file(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pin(key, pem):
    pins = _saved_pins()
    pins[key] = pem
    tmp = _pins_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pins, fh, indent=1)
    os.replace(tmp, _pins_file())


def _fetch_ca(host, port):
    """Ask the server for its CA. Over TLS, but UNVERIFIED -- there is nothing
    to verify it against yet; this is the trust-on-first-use moment, and the
    only one. What comes back is not believed, just kept: every real
    connection afterwards must present a certificate signed by it."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT, context=ctx)
    try:
        conn.request("GET", "/windstock/ca.crt", headers={"Host": SERVER_NAME})
        r = conn.getresponse()
        pem = r.read(32768).decode("ascii", "replace")
    finally:
        conn.close()
    if r.status != 200 or pem.count("BEGIN CERTIFICATE") != 1 or not netbase.fingerprint(pem):
        raise ValueError("that address did not answer with a certificate")
    return pem


def pinned_ca(host, port):
    """(pem, where it came from) for this address; fetches and saves on first use."""
    key = f"{host}:{port}"
    try:
        import online_pins
        pem = online_pins.BUILTIN.get(key)
        if pem:
            return pem, "built into the launcher"
    except ImportError:
        pass
    pem = _saved_pins().get(key)
    if pem:
        return pem, "saved on this phone"
    pem = _fetch_ca(host, port)
    _save_pin(key, pem)
    return pem, "seen for the first time and saved"


def forget(target=None):
    """Drop the saved certificate for `target` (default: the current one), so
    the next connection learns it afresh. Built-in pins are not affected."""
    key = target or (STATE["target"] and "%s:%d" % STATE["target"])
    pins = _saved_pins()
    had = pins.pop(key, None) is not None
    if had:
        tmp = _pins_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(pins, fh, indent=1)
        os.replace(tmp, _pins_file())
    with _lock:
        STATE["ctx"] = None
    _log(f"forgot the saved certificate for {key}" if had
         else f"no saved certificate for {key}")
    return had


def _context():
    """The TLS context for the current target, or None (reason in STATE['error'])."""
    with _lock:
        if STATE["ctx"] is not None:
            return STATE["ctx"]
        host, port = STATE["target"]
        try:
            pem, source = pinned_ca(host, port)
        except Exception as e:
            _complain(f"cannot get {host}:{port}'s certificate: {e}")
            return None
        ctx = ssl.create_default_context(cadata=pem)      # this CA and nothing else
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # Python 3.13 (what the phones run) turned on RFC 5280 strictness, which
        # rejects a leaf with no Authority Key Identifier -- and gen_certs.py's
        # leaves have none. The chain, the name and the dates are still checked.
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        STATE["ctx"], STATE["source"] = ctx, source
        _log(f"{host}:{port} certificate {source}: SHA-256 {netbase.fingerprint(pem)}")
        return ctx


def device_id():
    """This phone's id for the server: random, made once, kept in data/."""
    path = datadir.path("device.id")
    try:
        with open(path, encoding="ascii") as fh:
            value = fh.read().strip()
        if 22 <= len(value) <= 64:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(value)
    except OSError as e:
        _log(f"could not save device.id ({e}); using one for this run only")
    return value


# --------------------------------------------------------------------- relay
class _Upstream(http.client.HTTPConnection):
    """A TLS connection to the target's IP, verified as SERVER_NAME against the
    pinned CA."""

    def __init__(self, host, port, ctx):
        super().__init__(host, port, timeout=CONNECT_TIMEOUT)
        self._ctx = ctx

    def connect(self):
        sock = socket.create_connection((self.host, self.port), CONNECT_TIMEOUT)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Resume the last TLS session with this server: an abbreviated
            # handshake, one round trip fewer for every new connection.
            sess = _session(self._ctx, self.host, self.port)
            try:
                self.sock = self._ctx.wrap_socket(sock, server_hostname=SERVER_NAME,
                                                  session=sess)
            except ValueError:          # session not usable with this context
                self.sock = self._ctx.wrap_socket(sock, server_hostname=SERVER_NAME)
            self.sock.settimeout(READ_TIMEOUT)
        except BaseException:
            sock.close()
            raise

    def remember_session(self):
        """Keep this connection's TLS session for the next one to resume. Called
        after a response: under TLS 1.3 the ticket only arrives after the
        handshake, with the first data."""
        try:
            sess = self.sock.session if self.sock is not None else None
        except (AttributeError, ssl.SSLError):
            sess = None
        if sess is not None:
            with _pool_lock:
                _sessions[(self.host, self.port)] = (self._ctx, sess)


# Idle upstream connections, kept for the game's next local connection. The game
# opens new connections to 127.0.0.1 all the time (each screen, each tweak poll),
# and every one used to cost a fresh TCP + TLS handshake to the server -- two or
# three round trips, ~0.5 s from far away -- before its request even left.
_pool = []                       # (conn, ctx, idle_since)
_sessions = {}                   # (host, port) -> (ctx, ssl.SSLSession)
_pool_lock = threading.Lock()
POOL_MAX = 6
POOL_IDLE = 50                   # seconds; the server drops idle ones at 120


def _session(ctx, host, port):
    with _pool_lock:
        row = _sessions.get((host, port))
    return row[1] if row and row[0] is ctx else None


def _pool_take(ctx, host, port):
    """A kept-alive connection to host:port made with ctx, or None."""
    now = time.monotonic()
    stale = []
    got = None
    with _pool_lock:
        while _pool:
            conn, c, since = _pool.pop()
            if (c is ctx and conn.host == host and conn.port == port
                    and now - since < POOL_IDLE):
                got = conn
                break
            stale.append(conn)
    for conn in stale:
        try:
            conn.close()
        except OSError:
            pass
    return got


def _pool_give(conn, ctx):
    with _pool_lock:
        if len(_pool) < POOL_MAX:
            _pool.append((conn, ctx, time.monotonic()))
            return
    try:
        conn.close()
    except OSError:
        pass


# Headers that describe one hop, not the request; each side makes its own.
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
        "content-length", "expect", "accept-encoding",
        "x-ws-device", "x-ws-forwarded-for", "x-ws-launcher"}


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = netbase.IDLE_TIMEOUT
    device = ""

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        self._up = None
        self._up_ok = False
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass

    def finish(self):
        # A healthy upstream (its last answer read in full, not closing) goes back
        # to the pool for the next local connection; anything else is dropped.
        if self._up is not None and self._up_ok and self._up.sock is not None:
            _pool_give(self._up, self._up._ctx)
            self._up = None
        self._drop()
        super().finish()

    def _drop(self):
        if self._up is not None:
            try:
                self._up.close()
            except OSError:
                pass
            self._up = None

    def _answer(self, status, body=b"", headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        # One write, headers and body together (see server.Handler._reply).
        self._headers_buffer.append(b"\r\n")
        if self.command != "HEAD":
            self._headers_buffer.append(body)
        self.wfile.write(b"".join(self._headers_buffer))
        self._headers_buffer = []

    def _relay(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > netbase.MAX_BODY:
            self.close_connection = True
            return self._answer(413, b"too large", [("Connection", "close")])
        body = self.rfile.read(n) if n else None
        if self.path == "/windstock/tunnel":
            return self._status()

        ctx = _context()
        if ctx is None:
            return self._answer(502, (STATE["error"] or "no certificate").encode())
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
        headers["X-WS-Device"] = self.device
        headers["X-WS-Launcher"] = str(LAUNCHER_BUILD)
        host, port = STATE["target"]

        # One upstream connection per local one, reused: a TLS handshake per
        # request would add a round trip or two to every tap in the game.
        if self._up is not None and self._up._ctx is not ctx:
            self._drop()                # the pin changed since this one was made
        self._up_ok = False
        for attempt in (0, 1):
            if self._up is None and attempt == 0:
                self._up = _pool_take(ctx, host, port)
            fresh = self._up is None
            if fresh:
                self._up = _Upstream(host, port, ctx)
            try:
                self._up.request(self.command, self.path, body=body, headers=headers)
                r = self._up.getresponse()
                data = r.read()
                if fresh:
                    self._up.remember_session()
                break
            except ssl.SSLCertVerificationError as e:
                self._drop()
                _complain(f"REFUSED {host}:{port}: its certificate is not the one "
                          f"{STATE['source'] or 'pinned'} for this address "
                          f"({e.verify_message}). If the server's owner re-made it, "
                          f"use \"Forget saved certificate\" in the launcher; "
                          f"otherwise someone may be intercepting the connection.")
                return self._answer(502, b"certificate mismatch")
            except (OSError, http.client.HTTPException) as e:
                self._drop()
                # A kept-alive connection the server has since closed fails on
                # its first use; that request never ran, so once more on a new
                # one. A timeout is different -- the request may have run.
                stale = isinstance(e, (ConnectionError, http.client.RemoteDisconnected,
                                       http.client.CannotSendRequest,
                                       ssl.SSLEOFError, ssl.SSLZeroReturnError))
                if fresh or attempt or not stale:
                    _complain(f"cannot reach {host}:{port}: {e}")
                    return self._answer(502, b"server unreachable")
        STATE["error"] = None
        out = [(k, v) for k, v in r.getheaders() if k.lower() not in _HOP]
        if r.will_close:
            self._drop()
        else:
            self._up_ok = True
        self._answer(r.status, data, out)

    do_GET = do_POST = do_HEAD = _relay

    def _status(self):
        """The launcher's "Test connection": answered HERE, not by the server.
        Makes one real request down the tunnel, so "ok" means the address
        answered as a Windstock server AND its certificate matched the pin."""
        host, port = STATE["target"]
        out = {"tunnel": True, "target": f"{host}:{port}", "ok": False,
               "message": "", "pinned": "", "fingerprint": ""}
        ctx = _context()
        if ctx is None:
            out["message"] = STATE["error"] or "no certificate for this address"
        else:
            out["pinned"] = STATE["source"] or ""
            try:
                pem, _ = pinned_ca(host, port)
                out["fingerprint"] = netbase.fingerprint(pem)
            except Exception:
                pass
            up = _Upstream(host, port, ctx)
            try:
                up.request("GET", "/windstock/online", headers={
                    "Host": SERVER_NAME, "X-WS-Launcher": str(LAUNCHER_BUILD)})
                r = up.getresponse()
                data = r.read()
                try:
                    is_ws = r.status == 200 and json.loads(data).get("server") == "windstock"
                except ValueError:
                    is_ws = False
                out["ok"] = is_ws
                out["launcherBuild"] = LAUNCHER_BUILD
                try:
                    info = json.loads(data) if is_ws else {}
                except ValueError:
                    info = {}
                need = int(info.get("minBuild") or 0)
                out["minBuild"] = need
                out["updateURL"] = str(info.get("updateURL") or "")
                out["updateRequired"] = need > LAUNCHER_BUILD
                out["message"] = ("" if is_ws else
                                  "Something answered, but it is not a Windstock server")
            except ssl.SSLCertVerificationError:
                out["message"] = ("The server's certificate is not the one "
                                  + (STATE["source"] or "pinned") + " for this address")
            except ssl.SSLError as e:
                out["message"] = f"No encrypted connection: {e.reason or e}"
            except OSError as e:
                out["message"] = f"Cannot reach the server: {e.strerror or e}"
            finally:
                up.close()
        self._answer(200, json.dumps(out).encode(),
                     [("Content-Type", "application/json"), ("Cache-Control", "no-store")])


def serve(target, bind="127.0.0.1", port=8443, plain_port=8080, cert=None, key=None):
    """Run the tunnel to `target` ("ip:port"). Blocks in the TLS listener's
    serve_forever, like server.main(), so ws_boot drives both the same way."""
    parsed = parse_target(target)
    if not parsed:
        raise ValueError(f"not an IP:port address: {target!r}")
    with _lock:
        STATE.update(target=parsed, ctx=None, error=None, source=None)
    Proxy.device = device_id()

    plain = netbase.ThreadingHTTPServer((bind, plain_port), Proxy)
    threading.Thread(target=plain.serve_forever, daemon=True,
                     name="windstock-tunnel-http").start()

    # The game's own TLS connections end HERE, on loopback, with the
    # certificate it already trusts for the server-on-this-phone.
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        main = netbase.TLSServer((bind, port), Proxy)
        main.socket = netbase.tls_context(cert, key, legacy=True).wrap_socket(
            main.socket, server_side=True, do_handshake_on_connect=False)
    else:
        main = netbase.ThreadingHTTPServer((bind, port), Proxy)
    _log(f"{bind}:{port} (TLS) and :{plain_port} (plain)  ->  TLS to {target}")
    threading.Thread(target=_context, daemon=True, name="windstock-tunnel-pin").start()
    main.serve_forever()
