"""
PoGO 0.29 private server — single HTTPS listener that serves both the fake PTC
SSO (sso.pokemon.com) and the game RPC (pgorelease.nianticlabs.com), routed by
the Host header. Both hostnames resolve to this machine via your DNS redirect,
and one multi-SAN cert (see gen_certs.py) covers them all.

Run:  py server.py            # listens on 0.0.0.0:443
      PORT=8443 py server.py  # custom port (for local testing without root)
"""
import datetime
import os
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler

import netbase
from netbase import (HANDSHAKE_TIMEOUT, IDLE_TIMEOUT, MAX_BODY,      # noqa: F401
                     ThreadingHTTPServer, TLSServer)                 # (ws_boot uses these names)

import helpcenter
import bracky_site
import rpc
import shop
import sso

HERE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.environ.get("CERT_DIR", os.path.join(HERE, "certs"))
CERT = os.path.join(CERT_DIR, "server.crt")
KEY = os.path.join(CERT_DIR, "server.key")
PORT = int(os.environ.get("PORT", "443"))
BIND = os.environ.get("BIND", "0.0.0.0")


_QUIET = {"/research/ping", "/research/state", "/shiny/state", "/shiny/gps", "/windstock/players", "/windstock/events"}


def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    for line in str(msg).splitlines() or [""]:
        print(f"{ts}  {line}", flush=True)


_loopback = netbase.loopback


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT

    def log_message(self, *a):       # silence default noisy logging
        pass

    def setup(self):
        super().setup()
        # Small request/answer pairs on a long keep-alive connection: send each
        # packet the moment it's ready instead of batching (Nagle's algorithm).
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass

    def _read_body(self):
        """The request body. ValueError if its length is missing sense or over
        MAX_BODY -- checked before a single byte is read."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n < 0 or n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def _client_ip(self):
        """Who is really asking. sso_bridge.py relays logins from loopback and
        names the phone in X-WS-Forwarded-For; that header is believed ONLY
        from loopback, so nobody outside can lend themselves another address."""
        ip = self.client_address[0]
        if _loopback(ip):
            fwd = (self.headers.get("X-WS-Forwarded-For") or "").strip()
            if fwd and len(fwd) <= 64:
                return fwd
        return ip

    def _dispatch(self, method):
        host = (self.headers.get("Host", "") or "").split(":")[0].lower()
        path, _, query = self.path.partition("?")
        try:
            body = self._read_body()
        except ValueError:
            self.close_connection = True       # the unread body is still in the pipe
            return self._reply(413, {"Content-Type": "text/plain",
                                     "Connection": "close"}, b"too large")
        client_ip = self._client_ip()
        rpc._req_ip.value = client_ip
        rpc._req_dev.value = rpc.clean_device(self.headers.get("X-WS-Device"))
        _old = False
        if getattr(self.server, "online_door", False):
            import online
            _old = online.outdated(self.headers, local=_loopback(self.client_address[0]))
            if not _old and online.up_to_date(self.headers):
                online.clear_outdated(client_ip)
        rpc._req_old.value = _old
        if (getattr(self.server, "online_door", False)
                and not isinstance(self.connection, ssl.SSLSocket)
                and not _loopback(self.client_address[0])):
            # Plain HTTP on the public door: a launcher from before the tunnel,
            # or a stranger. Nothing is served in the clear over the network.
            self.close_connection = True
            return self._reply(426, {"Content-Type": "text/plain", "Connection": "close",
                                     "Upgrade": "TLS/1.2"},
                               b"This server only accepts the encrypted connection. "
                               b"Update your Windstock launcher.")
        try:
            # Log every non-RPC request. When a login "just fails", this is the
            # difference between knowing the client reached us and guessing.
            # (the tweak's background polls are left out -- they arrive every few seconds)
            if (not path.startswith(("/plfe", "/windstock/admin/art/"))
                    and path not in _QUIET):
                log(f"[http] {method} https://{host}{path}"
                    + (f"?{query}" if query else ""))
            if bracky_site.owns(path):
                # The public Bracky site. Checked first: it is matched
                # by exact path, so it cannot shadow the game's own routes.
                status, headers, out = bracky_site.handle(method, path, query,
                                                            self.headers, body, log)
            elif path.startswith("/windstock/trade/"):
                # Trades, asked by Windbot over loopback (trading.py).
                import trading
                status, headers, out = trading.handle(
                    method, path, query, body, local=_loopback(self.client_address[0]))
            elif path.startswith("/windstock/admin/"):
                # The website's admin page: a trainer's bag and Pokemon
                # (trainer_admin.py). Loopback + the website's key only.
                import trainer_admin
                status, headers, out = trainer_admin.handle(
                    method, path, query, body, self.headers,
                    local=_loopback(self.client_address[0]))
            elif path.startswith("/windstock/"):
                # The online door's handshake (online.py): "is this a Windstock
                # server?" and its CA, asked by the launchers over plain HTTP.
                import online
                status, headers, out = online.handle(
                    method, path, local=_loopback(self.client_address[0]))
            elif path.startswith("/webassets/"):
                # Leaflet, shipped with the server. The in-game map page loads
                # it from THIS origin (the World Manager is on another port).
                import webassets
                status, headers, out = webassets.handle(path)
            elif path.startswith("/radar"):
                # The desktop radar. By PATH, before the host checks: people
                # open it by IP from a laptop, so the Host header is whatever
                # they typed, not one of the game's hostnames.
                import radarsite
                status, headers, out = radarsite.handle(method, path, query,
                                                        self.headers, body, log)
            elif path.startswith("/shop"):
                # Checked before the host, so the shop is reachable on whichever
                # of our hostnames the phone happens to resolve.
                status, headers, out = shop.handle(method, path, query,
                                                   self.headers, body, log)
            elif "zendesk" in host or path.startswith("/hc"):
                # The radar's sign-in/sweep API (helpcenter.py). The Help Center
                # page itself is gone, so the in-game support button gets a 404.
                status, headers, out = helpcenter.handle(method, path, query,
                                                         self.headers, body, log)
            elif path.startswith("/sso"):
                # The PTC login, by PATH rather than by host. The patched client
                # asks pgorelease.nianticlabs.com for /sso/... instead of
                # sso.pokemon.com, because that hostname is the one the phone's
                # network keeps resolving to the real Niantic servers. Both still
                # work -- an unpatched APK arrives here via the host check below.
                status, headers, out = sso.handle(method, path, query,
                                                  self.headers, body, log)
            elif "pokemon.com" in host:
                status, headers, out = sso.handle(method, path, query,
                                                  self.headers, body, log)
            elif path.startswith("/glm/"):
                # Google Mobile Maps tiles -- the map's roads. Matched by PATH
                # because the host is mobilemaps.clients.google.com, which none
                # of the checks below would claim.
                import gmm
                status, headers, out = gmm.handle(method, path, query,
                                                  self.headers, body, log)
            elif path.startswith("/shiny/"):
                # The windstock tweak asking whether what's on screen is shiny.
                import shiny
                status, headers, out = shiny.handle(method, path, query, self.headers,
                                                    body, log, client_ip)
            elif path.startswith("/research"):
                # The windstock tweak's Research screens (binoculars button on the map).
                import research
                status, headers, out = research.handle(method, path, query, self.headers,
                                                       body, log, client_ip)
            elif path.startswith("/soundpacks/"):
                # The windstock tweak's "Download packs from server".
                import soundpacks
                status, headers, out = soundpacks.handle(method, path, query, self.headers,
                                                         body, log)
            elif path.startswith("/weather"):
                # The windstock tweak's map weather (effects + icon).
                import weather
                status, headers, out = weather.handle(method, path, query, self.headers,
                                                      body, log, client_ip)
            elif path.startswith("/raid"):
                # The windstock tweak's raid screens (lobby / HUD / results).
                import raidlobby
                status, headers, out = raidlobby.handle(method, path, query, self.headers,
                                                        body, log, client_ip)
            elif "niantic" in host or path.startswith("/plfe"):
                status, headers, out = rpc.handle(method, path, query,
                                                  self.headers, body, log)
            else:
                log(f"[?] {method} host={host!r} {path} (unrouted)")
                status, headers, out = 404, {"Content-Type": "text/plain"}, b"?"
        except Exception as e:
            import traceback
            log(f"[!] handler error: {e}\n{traceback.format_exc()}")
            status, headers, out = 500, {"Content-Type": "text/plain"}, b"err"
        self._reply(status, headers, out)

    def _reply(self, status, headers, out):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        # Uploaded photos and the like are served as exactly the type we say.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(out)))
        # Headers and body leave in ONE write. end_headers() used to flush the
        # headers on their own and the body followed in a second send; Nagle held
        # that second packet until the phone ACKed the first, so every answer cost
        # an extra round trip -- ~160 ms per request from the far side of the world.
        self._headers_buffer.append(b"\r\n")
        self._headers_buffer.append(out or b"")
        self.wfile.write(b"".join(self._headers_buffer))
        self._headers_buffer = []

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def main():
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        log("Missing certs. Run:  py gen_certs.py")
        sys.exit(1)
    # 0.29-era Mono/BoringSSL clients negotiate older suites; be permissive.
    ctx = netbase.tls_context(CERT, KEY, legacy=True)

    class LoggingHTTPSServer(TLSServer):
        log = staticmethod(log)

    httpd = LoggingHTTPSServer((BIND, PORT), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True,
                                   do_handshake_on_connect=False)
    log(f"PoGO private server listening on https://{BIND}:{PORT}")
    log("Routes: *.pokemon.com -> PTC SSO   |   *.nianticlabs.com -> RPC")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
