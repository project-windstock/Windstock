"""
Stress test page: logs in N fake trainers (loadbot001...) and has each one
walk around making the same map calls the game does, while it shows the
request rate, errors and response times.

    python3 stress_app.py      (opens http://127.0.0.1:8765 in your browser)

Point it at a LOCAL server first (PORT=8443 py server.py). Each bot is a real
account on the target, named loadbotNNN with the password "loadtest" -- or,
when Login is filled in (a server with email logins), every bot signs in as
that one account.
"""
import http.client
import json
import math
import queue
import random
import socket
import ssl
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

import s2sphere

import pb
import protocol as P

SSO = "sso.pokemon.com"
API = "pgorelease.nianticlabs.com"
CALLBACK = "https://sso.pokemon.com/sso/oauth2.0/callback"
PASSWORD = "loadtest"


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.logq = queue.Queue()       # lines for the log box
        self.reset()

    def reset(self):
        self.active = self.logins_ok = self.logins_fail = 0
        self.requests = self.errors = 0
        self.times = []                 # ms, last few thousand requests
        self.started = time.time()
        self.ended = None
        self.last_error = ""

    def log(self, msg):
        self.logq.put(time.strftime("%H:%M:%S  ") + msg)

    def ok(self, ms):
        with self.lock:
            self.requests += 1
            self.times.append(ms)
            if len(self.times) > 5000:
                del self.times[:1000]

    def fail(self, msg):
        with self.lock:
            self.errors += 1
            self.last_error = msg
        self.log("ERROR " + msg)


class Bot:
    def __init__(self, n, cfg, stats, stop):
        self.name = cfg.get("login") or f"loadbot{n:03d}"
        self.password = cfg.get("password") or PASSWORD
        self.n, self.cfg, self.stats, self.stop = n, cfg, stats, stop
        self.lat = cfg["lat"] + random.uniform(-0.01, 0.01)
        self.lng = cfg["lng"] + random.uniform(-0.01, 0.01)
        self.conns = {}

    # one keep-alive TLS connection per hostname, like the game
    def _conn(self, host):
        c = self.conns.get(host)
        if c is None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            c = http.client.HTTPSConnection(host, context=ctx, timeout=20)
            addr = (self.cfg["host"], self.cfg["port"])

            def connect(c=c, host=host):
                sock = socket.create_connection(addr, timeout=20)
                c.sock = ctx.wrap_socket(sock, server_hostname=host)
            c.connect = connect
            self.conns[host] = c
        return c

    def req(self, host, method, path, body=None, ctype=None):
        headers = {"Host": host}
        if ctype:
            headers["Content-Type"] = ctype
        c = self._conn(host)
        try:
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        except Exception:
            c.close()
            self.conns.pop(host, None)
            raise

    def login(self):
        svc = "/sso/login?service=" + urllib.parse.quote(CALLBACK)
        s, h, b = self.req(SSO, "GET", svc)
        page = json.loads(b)
        form = urllib.parse.urlencode({
            "lt": page["lt"], "execution": page["execution"], "_eventId": "submit",
            "username": self.name, "password": self.password})
        s, h, b = self.req(SSO, "POST", svc, body=form,
                           ctype="application/x-www-form-urlencoded")
        loc = h.get("Location", "")
        ticket = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("ticket", [""])[0]
        if not ticket:
            try:
                why = json.loads(b)["errors"][0]
            except Exception:
                why = f"HTTP {s}"
            raise RuntimeError(f"login refused: {why}")
        body = urllib.parse.urlencode({
            "client_id": "mobile-app_pokemon-go", "grant_type": "refresh_token",
            "code": ticket, "redirect_uri": "https://www.nianticlabs.com/pokemongo/error"})
        s, h, b = self.req(SSO, "POST", "/sso/oauth2.0/accessToken", body=body,
                           ctype="application/x-www-form-urlencoded")
        self.token = urllib.parse.parse_qs(b.decode()).get("access_token", [""])[0]
        if not self.token:
            raise RuntimeError("no access token")
        self.conns.pop(SSO).close()         # one open connection per bot, not two
        # handshake: bootstrap -> redirect -> api path
        f = self.rpc("/plfe/rpc", [(P.RT.GET_PLAYER, b"")])
        api_url = pb.get(f, P.RESP_API_URL)
        api_url = api_url.decode() if isinstance(api_url, bytes) else api_url
        self.path = "/" + api_url.split("/", 1)[1]
        self.rpc(self.path, [(P.RT.GET_PLAYER, b""), (P.RT.GET_INVENTORY, b"")])

    def rpc(self, path, requests):
        auth = (pb.Writer().string(P.AI_PROVIDER, "ptc")
                .message(P.AI_TOKEN, pb.Writer().string(P.AI_TOKEN_CONTENTS, self.token)
                         .uint(2, 59)).to_bytes())
        w = (pb.Writer().uint(P.RE_STATUS_CODE, 2)
             .uint(P.RE_REQUEST_ID, random.getrandbits(40)))
        for rtype, msg in requests:
            r = pb.Writer().uint(P.REQ_TYPE, rtype)
            if msg:
                r.bytes_(P.REQ_MESSAGE, msg)
            w.message(P.RE_REQUESTS, r.to_bytes())
        w.double(P.RE_LATITUDE, self.lat).double(P.RE_LONGITUDE, self.lng)
        w.double(P.RE_ACCURACY, 10.0).message(P.RE_AUTH_INFO, auth)
        t0 = time.perf_counter()
        s, h, b = self.req(API, "POST", path, body=w.to_bytes(), ctype="application/binary")
        ms = (time.perf_counter() - t0) * 1000
        if s != 200:
            raise RuntimeError(f"HTTP {s}")
        f = pb.decode(b)
        code = pb.get(f, P.RESP_STATUS_CODE)
        if code not in (P.STATUS_OK, P.STATUS_REDIRECT):
            raise RuntimeError(f"status {code}")
        self.stats.ok(ms)
        return f

    def map_request(self):
        ll = s2sphere.LatLng.from_degrees(self.lat, self.lng)
        cell = s2sphere.CellId.from_lat_lng(ll).parent(15)
        cells = [cell.id()] + [c.id() for c in cell.get_all_neighbors(15)]
        msg = (pb.Writer().packed_varints(1, cells)
               .packed_varints(2, [0] * len(cells))
               .double(3, self.lat).double(4, self.lng).to_bytes())
        upd = pb.Writer().double(1, self.lat).double(2, self.lng).to_bytes()
        return [(P.RT.GET_MAP_OBJECTS, msg), (P.RT.PLAYER_UPDATE, upd),
                (P.RT.GET_HATCHED_EGGS, b""), (P.RT.CHECK_AWARDED_BADGES, b"")]

    def run(self):
        try:
            self.login()
            with self.stats.lock:
                self.stats.logins_ok += 1
                self.stats.active += 1
            self.stats.log(f"bot {self.n} signed in as {self.name}")
        except Exception as e:
            with self.stats.lock:
                self.stats.logins_fail += 1
            self.stats.fail(f"bot {self.n} login: {e}")
            if "refused" in str(e):
                # a wrong password x30 would trip the server's guess limit
                # (10 fails per address) and lock this whole address out
                self.stats.log("login refused -- stopping every bot")
                self.stop.set()
            return
        heading = random.uniform(0, 2 * math.pi)
        try:
            while not self.stop.is_set():
                # walk ~5 m per tick, turning a little
                heading += random.uniform(-0.5, 0.5)
                self.lat += 0.000045 * math.cos(heading)
                self.lng += 0.000045 * math.sin(heading) / math.cos(math.radians(self.lat))
                try:
                    self.rpc(self.path, self.map_request())
                except Exception as e:
                    self.stats.fail(f"bot {self.n}: {type(e).__name__}: {e}")
                self.stop.wait(self.cfg["interval"] * random.uniform(0.8, 1.2))
        finally:
            with self.stats.lock:
                self.stats.active -= 1
            self.stats.log(f"bot {self.n} stopped")
            for c in self.conns.values():
                c.close()


FIELDS = [("Server IP", "host", "2.29.24.78"), ("Port", "port", "8888"),
          ("Bots", "bots", "30"), ("Seconds between calls", "interval", "5"),
          ("Bot logins per second", "ramp", "5"),
          ("Run for (seconds, 0 = forever)", "duration", "120"),
          ("Latitude", "lat", "40.0"), ("Longitude", "lng", "-75.0"),
          ("Login (blank = loadbotNNN)", "login", ""), ("Password", "password", "")]


class Runner:
    def __init__(self):
        self.stats = Stats()
        self.stop = threading.Event()
        self.running = False
        self.end_at = None
        self.next_summary = 0
        self.lines = []                 # the log, kept for the page
        threading.Thread(target=self.watch, daemon=True).start()

    def start(self, form):
        if self.running:
            return "already running"
        try:
            cfg = {k: (form.get(k) or "").strip() for _, k, _ in FIELDS}
            for k in ("port", "bots", "duration"):
                cfg[k] = int(cfg[k])
            for k in ("interval", "ramp", "lat", "lng"):
                cfg[k] = float(cfg[k])
        except ValueError:
            return "check the numbers"
        self.stats.reset()
        self.stop.clear()
        self.running = True
        self.end_at = time.time() + cfg["duration"] if cfg["duration"] else None
        self.next_summary = time.time() + 10
        self.stats.log(f"START {cfg['bots']} bots -> {cfg['host']}:{cfg['port']}, "
                       f"a call every {cfg['interval']:g}s, "
                       f"{'for %ds' % cfg['duration'] if cfg['duration'] else 'until stopped'}")
        threading.Thread(target=self.spawn, args=(cfg,), daemon=True).start()
        return "started"

    def spawn(self, cfg):
        for n in range(1, cfg["bots"] + 1):
            if self.stop.is_set():
                return
            threading.Thread(target=Bot(n, cfg, self.stats, self.stop).run, daemon=True).start()
            time.sleep(1 / max(cfg["ramp"], 0.1))

    def stop_run(self):
        if self.running:
            self.stats.ended = time.time()
            self.stats.log("STOP " + self.summary())
        self.stop.set()
        self.running = False

    def summary(self):
        s = self.stats
        with s.lock:
            t = sorted(s.times[-2000:])
            return (f"{s.active} bots online, {s.requests} requests, {s.errors} errors, "
                    f"p50 {t[len(t) // 2] if t else 0:.0f} ms, "
                    f"p95 {t[int(len(t) * 0.95)] if t else 0:.0f} ms")

    def watch(self):
        while True:
            if self.running and (self.stop.is_set() or
                                 (self.end_at and time.time() >= self.end_at)):
                self.stop_run()
            if self.running and time.time() >= self.next_summary:
                self.next_summary = time.time() + 10
                self.stats.log(self.summary())
            while not self.stats.logq.empty():
                self.lines.append(self.stats.logq.get())
            del self.lines[:-2000]
            time.sleep(0.25)

    def status(self, since):
        s = self.stats
        with s.lock:
            t = sorted(s.times[-2000:])
            el = max((s.ended or time.time()) - s.started, 1e-6)
            st = {
                "running": self.running, "seconds": round(el),
                "bots online": s.active,
                "logins": f"{s.logins_ok} ok / {s.logins_fail} failed",
                "requests": f"{s.requests} ({s.requests / el:.1f}/s)",
                "errors": s.errors,
                "response p50": f"{t[len(t) // 2] if t else 0:.0f} ms",
                "response p95": f"{t[int(len(t) * 0.95)] if t else 0:.0f} ms",
                "slowest": f"{t[-1] if t else 0:.0f} ms",
            }
        st["log"] = self.lines[since:] if since <= len(self.lines) else self.lines
        st["logsize"] = len(self.lines)
        return st


PAGE = """<!doctype html><title>Stress Test</title>
<h3>Stress Test</h3>
<form id=f onsubmit="return false">
%s
<button onclick="post('/start')">Start</button>
<button onclick="post('/stop')">Stop</button>
</form>
<p><b id=state>...</b></p>
<pre id=stats></pre>
<p>Log <button onclick="document.getElementById('log').textContent='';">Clear log</button></p>
<pre id=log style="height:300px;overflow:auto;border:1px solid"></pre>
<script>
var since = 0, spin = 0;
function post(path) {
  fetch(path, {method: 'POST', body: new URLSearchParams(new FormData(document.getElementById('f')))});
}
function poll() {
  fetch('/status?since=' + since).then(r => r.json()).then(s => {
    spin++;
    document.getElementById('state').textContent = s.running
      ? '|/-\\\\'[spin %% 4] + ' RUNNING ' + s.seconds + 's' : 'STOPPED';
    var t = '';
    for (var k of ['bots online','logins','requests','errors','response p50','response p95','slowest'])
      t += (k + '              ').slice(0, 14) + s[k] + '\\n';
    document.getElementById('stats').textContent = t;
    var log = document.getElementById('log');
    if (s.log.length) {
      log.textContent += s.log.join('\\n') + '\\n';
      log.scrollTop = log.scrollHeight;
    }
    since = s.logsize;
  }).catch(() => {
    document.getElementById('state').textContent = 'app closed';
  }).finally(() => setTimeout(poll, 300));
}
poll();
</script>
"""


def page():
    rows = "".join(
        f'<label>{label} <input name={key} value="{default}"'
        f'{" type=password" if key == "password" else ""}></label><br>\n'
        for label, key, default in FIELDS)
    return PAGE % rows


runner = Runner()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/status":
            q = urllib.parse.parse_qs(u.query)
            since = int((q.get("since") or ["0"])[0] or 0)
            self.send(json.dumps(runner.status(since)), "application/json")
        else:
            self.send(page(), "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(
            self.rfile.read(n).decode(), keep_blank_values=True).items()}
        if self.path == "/start":
            self.send(runner.start(form), "text/plain")
        else:
            runner.stop_run()
            self.send("stopped", "text/plain")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Stress test running at http://127.0.0.1:8765  (Ctrl+C to quit)")
    webbrowser.open("http://127.0.0.1:8765")
    srv.serve_forever()
