"""
The listeners every door of the server is built from, with the limits that
keep one client from taking the whole thing down.

Kept free of game imports on purpose: the launchers' TLS tunnel (tunnel.py)
uses these too, and must not drag protocol.py and friends into memory.

    ThreadingHTTPServer   a ceiling on open connections, in total and per address
    TLSServer             TLS on every connection, handshake off the accept thread
    DualServer            plain HTTP and TLS on one port, decided per connection
"""
import hashlib
import socket
import ssl
import sys
import threading
from http.server import ThreadingHTTPServer as _ThreadingHTTPServer

# The biggest request we will read. The largest honest one is a Help Center
# nomination with its photo (~4 MB of base64); everything the game sends is a
# few KB. Without a cap, one request with a huge Content-Length fills the RAM.
MAX_BODY = 8 * 1024 * 1024
# A connection that says nothing for this long is dropped, so idle or
# deliberately slow clients can't each hold a thread forever.
IDLE_TIMEOUT = 120
HANDSHAKE_TIMEOUT = 15
TLS_HANDSHAKE = 0x16            # first byte of a TLS handshake record


def loopback(ip):
    return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def tls_context(cert, key, legacy=True):
    """A server-side TLS context for `cert`/`key`.

    legacy=True is for the game's OWN connections: its 2016-era Mono/BoringSSL
    stacks negotiate TLS 1.0 and old suites, and refusing those refuses the
    game. legacy=False (TLS 1.2+, modern suites) is for doors only the
    launchers' tunnel dials."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    try:
        if legacy:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        else:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (ssl.SSLError, ValueError):
        pass
    return ctx


def fingerprint(pem):
    """SHA-256 of a PEM certificate, as 'AB:CD:...' -- what an operator reads
    out so a player can check they pinned the right server. '' if unreadable."""
    try:
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.strip())).hexdigest().upper()
    except (ValueError, TypeError, AttributeError):
        return ""
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """ThreadingHTTPServer with a ceiling on open connections, in total and per
    address -- every connection is a thread, and nothing else bounds them.
    (ws_boot.py builds its plain listener from server.ThreadingHTTPServer,
    which is this class.)"""
    daemon_threads = True
    MAX_CONNS = 600
    MAX_PER_IP = 64

    def _slots(self):
        if "_conn_lock" not in self.__dict__:
            self.__dict__.setdefault("_conn_count", {})
            self.__dict__.setdefault("_conn_lock", threading.Lock())
        return self._conn_lock, self._conn_count

    def process_request(self, request, client_address):
        ip = client_address[0]
        lock, count = self._slots()
        with lock:
            full = (sum(count.values()) >= self.MAX_CONNS
                    or (count.get(ip, 0) >= self.MAX_PER_IP and not loopback(ip)))
            if not full:
                count[ip] = count.get(ip, 0) + 1
        if full:
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            lock, count = self._slots()
            with lock:
                left = count.get(client_address[0], 1) - 1
                if left > 0:
                    count[client_address[0]] = left
                else:
                    count.pop(client_address[0], None)

    def handle_error(self, request, client_address):
        # A phone dropping its keep-alive connection (app backgrounded, network
        # switch) or going quiet isn't a server error; no traceback for it.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError,
                                          socket.timeout, ssl.SSLError)):
            return
        super().handle_error(request, client_address)

    def shutdown_request(self, request):
        # An SSLSocket and the raw one it wraps are the same fd; closing either is fine.
        try:
            super().shutdown_request(request)
        except OSError:
            pass


class TLSServer(ThreadingHTTPServer):
    """Every connection is TLS. Wrap the LISTENING socket with
    `ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)`.

    The handshake runs here, on the connection's own thread and under a
    timeout. It used to run inside accept(), where one client that connected
    and then said nothing stalled the listener for everybody. Failures are
    logged so we can see WHY a client (e.g. native RPC) is rejected -- 'bad
    certificate' => client distrusts our CA; 'unsupported protocol' / 'no shared
    cipher' => TLS-version/cipher mismatch."""
    log = staticmethod(lambda msg: print(msg, flush=True))

    def finish_request(self, request, client_address):
        if isinstance(request, ssl.SSLSocket):
            try:
                request.settimeout(HANDSHAKE_TIMEOUT)
                request.do_handshake()
            except ssl.SSLError as e:
                self.log(f"[tls] handshake FAILED: {e!r}")
                return
            except OSError:
                return
        super().finish_request(request, client_address)


def sniff(sock, timeout=HANDSHAKE_TIMEOUT):
    """'tls', 'plain', or None if the client sent nothing. Leaves the byte unread."""
    sock.settimeout(timeout)
    try:
        first = sock.recv(1, socket.MSG_PEEK)
    finally:
        sock.settimeout(None)
    if not first:
        return None
    return "tls" if first[0] == TLS_HANDSHAKE else "plain"


class DualServer(ThreadingHTTPServer):
    """Plain HTTP and TLS on ONE port, decided per connection by its first
    byte (0x16 opens a TLS handshake).

    The decision runs in finish_request, which ThreadingMixIn calls on the
    connection's own thread -- a slow or silent client never holds up accept().
    Set `tls_ctx` after building it; without one, TLS connections are dropped.
    """
    tls_ctx = None
    counts = None                    # optional {"plain": n, "tls": n}
    log = staticmethod(lambda msg: print(msg, flush=True))

    def finish_request(self, request, client_address):
        try:
            kind = sniff(request)
        except OSError:
            return
        if kind is None:
            return
        if kind == "tls":
            if self.tls_ctx is None:
                return
            try:
                # Under a timeout: a client that opens a handshake and stalls
                # must not hold this thread forever.
                request.settimeout(HANDSHAKE_TIMEOUT)
                request = self.tls_ctx.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError) as e:
                self.log(f"TLS handshake from {client_address[0]} failed: {e!r}")
                return
        if self.counts is not None:
            self.counts[kind] = self.counts.get(kind, 0) + 1
        self.RequestHandlerClass(request, client_address, self)
