"""
Windstock Regions: tick a country (or US state) and it goes live on the VPS.

    python3 region_app.py          (opens http://127.0.0.1:8766 in your browser)

For each region it: downloads the Geofabrik .osm.pbf (8 parallel ranges, md5
checked), pulls the real PokeStops/Gyms out of it (fetch_pois_pbf), votes its
terrain into biome cells by area (fetch_area_biomes), packs both into one
cell-indexed SQLite file (build_world_db), and copies that pack to the VPS as
/opt/pogo/data/world/<region>.sqlite.

The server scans data/world/*.sqlite on every map request, so a new pack is
live without a restart (worlddb.paths). It goes up under a dot-name first and
is renamed into place -- worlddb opens packs immutable, so one is never
rewritten while the server might be reading it. Packs are also kept locally in
tools/_packs, so a removed region can be pushed again without re-downloading.

The VPS is root@2.29.24.78 by default (WINDSTOCK_VPS=user@host to change).
"""
import datetime
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(WORK, "server"),
                os.path.join(WORK, "..", "ios-launcher", "tools")]


def _load(name):
    """tools/<name>.py by path: these modules push server/ onto the front of
    sys.path, and server/ keeps an older fetch_pois_pbf.py of its own."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fetch_pois_pbf = _load("fetch_pois_pbf")
fetch_area_biomes = _load("fetch_area_biomes")

PORT = 8766
VPS = os.environ.get("WINDSTOCK_VPS", "root@2.29.24.78")
REMOTE_WORLD = "/opt/pogo/data/world"
REMOTE_LEDGER = "/opt/pogo/data/populated_regions.json"
CACHE = os.path.join(HERE, "_osm_cache")
PACKS = os.path.join(HERE, "_packs")
GEOFABRIK = "https://download.geofabrik.de/{}-latest.osm.pbf"
UA = {"User-Agent": "windstock-regions/1.0"}
PARTS = 8
BIG = 1_000_000_000          # above this, a country is done by sub-region (_parts)
SSH = ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]


def _regions():
    """[(group, name, slug)] -- the World Manager's list, US by state."""
    import poidownload as pd
    out = []
    for name, slug in pd.COUNTRIES.items():
        if slug == "north-america/us":
            continue
        group = {"europe": "Europe", "north-america": "North America",
                 "south-america": "South America", "asia": "Asia",
                 "australia-oceania": "Oceania", "africa": "Africa"}[slug.split("/")[0]]
        out.append((group, name, slug))
    for name, slug in pd.US_STATES.items():
        out.append(("United States", name, slug))
    return out


REGIONS = _regions()
NAMES = {slug: name for _g, name, slug in REGIONS}


def pack_name(slug):
    return slug.replace("/", "_") + ".sqlite"


# ---------------------------------------------------------------- state

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}            # slug -> {"step", "pct", "error", "done"}
        self.queue = queue.Queue()
        self.log = []
        self.server = {"packs": {}, "ledger": [], "checked": 0, "error": ""}

    def say(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        with self.lock:
            self.log.append(line)
            del self.log[:-400]

    def set(self, slug, **kw):
        with self.lock:
            self.jobs.setdefault(slug, {}).update(kw)


S = State()


def ssh(cmd, timeout=120):
    r = subprocess.run(SSH + [VPS, cmd], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"ssh exit {r.returncode}")
    return r.stdout


def refresh_server():
    """What the VPS has: its region packs and the World Manager ledger."""
    try:
        out = ssh(f"cd {REMOTE_WORLD} && for f in *.sqlite; do "
                  f"[ -f \"$f\" ] && stat -c '%n %s %Y' \"$f\"; done; "
                  f"echo ---; cat {REMOTE_LEDGER} 2>/dev/null", timeout=30)
        top, _, ledger = out.partition("---\n")
        packs = {}
        for line in top.splitlines():
            parts = line.split()
            if len(parts) == 3:
                packs[parts[0]] = {"size": int(parts[1]), "mtime": int(parts[2])}
        try:
            done = json.loads(ledger).get("done", [])
        except ValueError:
            done = []
        with S.lock:
            S.server = {"packs": packs, "ledger": done, "checked": time.time(), "error": ""}
    except Exception as e:
        with S.lock:
            S.server["error"] = str(e)
            S.server["checked"] = time.time()


# ---------------------------------------------------------------- pipeline

def _head(slug):
    """(dated url, size) of a region's extract: -latest redirects to a dated file."""
    req = urllib.request.Request(GEOFABRIK.format(slug), headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.geturl(), int(r.headers["Content-Length"])


_index = None


def _children(slug):
    """Geofabrik's sub-regions of `slug` (Poland -> its 16 voivodeships), or []."""
    global _index
    if _index is None:
        with urllib.request.urlopen(urllib.request.Request(
                "https://download.geofabrik.de/index-v1-nogeom.json", headers=UA),
                timeout=120) as r:
            _index = [f["properties"] for f in json.load(r)["features"]]
    me = next((f for f in _index
               if f["urls"]["pbf"].endswith(f"/{slug}-latest.osm.pbf")), None)
    if me is None:
        return []
    pre = "https://download.geofabrik.de/"
    return [f["urls"]["pbf"][len(pre):-len("-latest.osm.pbf")]
            for f in _index if f.get("parent") == me["id"]]


def _parts(slug):
    """The extracts to process for `slug`. Osmium keeps every node location in
    RAM; past ~1.5 GB of .pbf that no longer fits this Mac (8 GB) and it thrashes
    for hours, so a big country is done one Geofabrik sub-region at a time."""
    _url, size = _head(slug)
    kids = _children(slug) if size >= BIG else []
    if not kids:
        return [slug]
    out = []
    for k in kids:
        out.extend(_parts(k))
    return out


def _download(slug, job, label):
    """The extract, fetched in PARTS ranges at once (Geofabrik throttles each
    connection) and checked against its published md5. Returns the path."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, slug.replace("/", "_") + ".osm.pbf")
    url, size = _head(slug)
    with urllib.request.urlopen(urllib.request.Request(url + ".md5", headers=UA),
                                timeout=60) as r:
        want = r.read().decode().split()[0]
    if os.path.exists(path) and os.path.getsize(path) == size:
        S.set(job, step=label + "Checking download", pct=None, detail="")
        if _md5(path) == want:
            S.say(f"{slug}: reusing cached extract")
            return path
    S.say(f"{slug}: downloading {size / 1e6:,.0f} MB from {url.rsplit('/', 1)[1]}")
    got = [0] * PARTS
    errors = []
    step = size // PARTS + 1
    tmp = path + ".part"

    with open(tmp, "wb") as fh:
        fh.truncate(size)

    def grab(i):
        a, b = i * step, min(size, (i + 1) * step) - 1
        for attempt in range(6):
            try:
                start = a + got[i]
                if start > b:
                    return
                rq = urllib.request.Request(url, headers=dict(UA, Range=f"bytes={start}-{b}"))
                with urllib.request.urlopen(rq, timeout=120) as r, open(tmp, "r+b") as out:
                    out.seek(start)
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        got[i] += len(chunk)
                if a + got[i] > b:
                    return
            except Exception as e:
                if attempt == 5:
                    errors.append(e)
                time.sleep(3)

    threads = [threading.Thread(target=grab, args=(i,), daemon=True) for i in range(PARTS)]
    for t in threads:
        t.start()
    t0 = time.time()
    while any(t.is_alive() for t in threads):
        time.sleep(1)
        done = sum(got)
        rate = done / max(1, time.time() - t0)
        S.set(job, step=label + "Downloading", pct=round(100 * done / size, 1),
              detail=f"{done / 1e6:,.0f} / {size / 1e6:,.0f} MB  ({rate / 1e6:.1f} MB/s)")
    if errors or sum(got) != size:
        raise RuntimeError(f"download incomplete ({sum(got):,} of {size:,} bytes): {errors[:1]}")
    S.set(job, step=label + "Checking download", pct=None, detail="")
    if _md5(tmp) != want:
        os.remove(tmp)
        raise RuntimeError("download corrupt (md5 mismatch) -- try again")
    os.replace(tmp, path)
    return path


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _build(slug, keep_pbf=False):
    """Download + extract every part of `slug`, merge, and pack it into
    tools/_packs/<pack>. Returns build_world_db's summary."""
    import build_world_db

    os.makedirs(PACKS, exist_ok=True)
    S.set(slug, step="Looking up the download", pct=None, detail="")
    parts = _parts(slug)
    if len(parts) > 1:
        S.say(f"{slug}: big extract -- doing it in {len(parts)} parts")
    forts, seen, cells = [], set(), {}
    for i, part in enumerate(parts, 1):
        label = f"{i}/{len(parts)} {part.rsplit('/', 1)[-1]}: " if len(parts) > 1 else ""
        pbf = _download(part, slug, label)
        S.set(slug, step=label + "Finding PokeStops & Gyms", pct=None, detail="")
        got, scanned = fetch_pois_pbf._extract(pbf)
        new = [f for f in got if f["id"] not in seen]   # parts overlap at borders
        seen.update(f["id"] for f in new)
        forts.extend(new)
        del got
        S.say(f"{part}: {len(new):,} forts from {scanned:,} features")
        S.set(slug, step=label + "Mapping biomes", detail="")
        c = fetch_area_biomes.compute(pbf, log=lambda m: S.set(slug, detail=m.strip()))
        cells.update(c)
        S.say(f"{part}: {len(c):,} biome cells")
        if not keep_pbf:
            os.remove(pbf)

    S.set(slug, step="Packing", detail="")
    biomes = os.path.join(CACHE, slug.replace("/", "_") + ".biomes.json")
    with open(biomes, "w", encoding="utf-8") as fh:
        json.dump({"cell_level": fetch_area_biomes.CELL_LEVEL, "cells": cells, "points": []}, fh)
    out = os.path.join(PACKS, pack_name(slug))
    info = build_world_db.build(out, forts=forts, biomes_path=biomes,
                                name=NAMES.get(slug, slug), region=slug)
    os.remove(biomes)
    return info


def _upload(slug):
    """Copy the pack up under a dot-name (not *.sqlite, so the server ignores
    it), then rename it into place and record the region in the ledger."""
    local = os.path.join(PACKS, pack_name(slug))
    name = pack_name(slug)
    tmp = f"{REMOTE_WORLD}/.{name}.upload"
    S.set(slug, step="Uploading to the server",
          detail=f"{os.path.getsize(local) / 1e6:,.0f} MB")
    r = subprocess.run(["rsync", "-z", "--partial", "-e", " ".join(SSH),
                        local, f"{VPS}:{tmp}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("upload failed: " + (r.stderr or r.stdout).strip())
    ledger_py = ("import json,sys;p=sys.argv[1];"
                 "d=json.load(open(p)) if __import__('os').path.exists(p) else {'done':[]};"
                 "s=set(d.get('done',[]));s.add(sys.argv[2]);"
                 "json.dump({'done':sorted(s)},open(p,'w'),indent=1)")
    ssh(f"set -e; chown pogo:pogo {tmp}; chmod 644 {tmp}; mv -f {tmp} {REMOTE_WORLD}/{name}; "
        f"python3 -c \"{ledger_py}\" {REMOTE_LEDGER} {slug}; chown pogo:pogo {REMOTE_LEDGER}")


def run_job(slug, keep_pbf=False):
    t0 = time.time()
    S.set(slug, step="Starting", pct=None, error="", done=False, detail="")
    S.say(f"{slug}: started")
    local = os.path.join(PACKS, pack_name(slug))
    if os.path.exists(local):
        S.say(f"{slug}: using the pack built {datetime.datetime.fromtimestamp(os.path.getmtime(local)):%Y-%m-%d %H:%M}"
              " (delete tools/_packs/" + pack_name(slug) + " to rebuild)")
    else:
        info = _build(slug, keep_pbf)
        S.say(f"{slug}: pack {info['forts']:,} forts, {info['biome_cells']:,} biome cells")
    _upload(slug)
    refresh_server()
    S.set(slug, step="Live on the server", pct=None, done=True,
          detail=f"took {(time.time() - t0) / 60:.0f} min")
    S.say(f"{slug}: LIVE on {VPS.split('@')[-1]}")


def remove(slug):
    name = pack_name(slug)
    ledger_py = ("import json,sys;p=sys.argv[1];d=json.load(open(p));"
                 "d['done']=[x for x in d.get('done',[]) if x!=sys.argv[2]];"
                 "json.dump(d,open(p,'w'),indent=1)")
    ssh(f"set -e; rm -f {REMOTE_WORLD}/{name}; "
        f"[ -f {REMOTE_LEDGER} ] && python3 -c \"{ledger_py}\" {REMOTE_LEDGER} {slug} || true")
    S.set(slug, step="", done=False, error="", detail="")
    S.say(f"{slug}: removed from the server (local pack kept in tools/_packs)")
    refresh_server()


def worker():
    while True:
        slug = S.queue.get()
        try:
            run_job(slug)
        except Exception as e:
            S.set(slug, step="Failed", error=str(e), pct=None)
            S.say(f"{slug}: FAILED -- {e}")
            traceback.print_exc()


# ---------------------------------------------------------------- web page

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Windstock Regions</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#6b7280;--line:#e3e6eb;--acc:#2563eb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#e8eaee;--mute:#9aa1ab;--line:#2b3037;--acc:#60a5fa;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{padding:20px 16px 8px;max-width:980px;margin:auto}h1{margin:0 0 4px;font-size:22px}
.sub{color:var(--mute)}main{max-width:980px;margin:auto;padding:0 16px 40px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
input[type=search]{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:14px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin:16px 0 6px}
.row{display:flex;align-items:center;gap:10px;padding:7px 2px;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.nm{flex:1;min-width:0}.st{font-size:12px;color:var(--mute)}
.st.ok{color:var(--ok)}.st.bad{color:var(--bad)}.st.busy{color:var(--warn)}
button{border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:7px;padding:5px 11px;font-size:13px;cursor:pointer}
button.go{background:var(--acc);border-color:var(--acc);color:#fff}button:disabled{opacity:.45;cursor:default}
.bar{height:4px;background:var(--line);border-radius:2px;margin-top:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
pre{margin:0;max-height:260px;overflow:auto;font:12px/1.4 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:var(--mute)}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.top .sp{flex:1}
</style></head><body>
<header><h1>Windstock Regions</h1>
<div class="sub">Pick a country or US state. It downloads, gets its PokéStops, Gyms and biomes, and goes live on <b id="vps"></b> without a restart.</div></header>
<main>
<div class="card"><div class="top"><span class="sp" id="srv">Checking the server…</span>
<button onclick="post('/api/refresh')">Refresh</button><button onclick="if(confirm('Close Windstock Regions? Running jobs stop.'))post('/api/quit').then(()=>document.body.innerHTML='<p style=padding:20px>Closed.</p>')">Quit</button></div></div>
<div class="card"><input type="search" id="q" placeholder="Search countries and states…" oninput="draw()"><div id="list"></div></div>
<div class="card"><pre id="log"></pre></div>
</main>
<script>
let S=null,lastHTML='';
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b||{})});const j=await r.json();if(j.error)alert(j.error);await tick();return j}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function status(r){
  const j=S.jobs[r.slug]||{};
  if(j.step&&!j.done&&!j.error)return {cls:'busy',txt:esc(j.step)+(j.pct!=null?' '+j.pct+'%':'')+(j.detail?' — '+esc(j.detail):''),busy:true,pct:j.pct};
  if(j.error)return {cls:'bad',txt:'Failed: '+esc(j.error)};
  if(r.pack)return {cls:'ok',txt:'Live on the server · '+(r.pack.size/1e6).toFixed(0)+' MB'+(j.detail?' · '+esc(j.detail):''),live:true};
  if(r.main)return {cls:'ok',txt:'In the main world pack',main:true};
  if(S.queued.includes(r.slug))return {cls:'busy',txt:'Queued',busy:true};
  return {cls:'',txt:r.built?'Built locally, not on the server':''};
}
function draw(){
  if(!S)return;const q=document.getElementById('q').value.trim().toLowerCase();
  let h='',g='';
  for(const r of S.regions){
    if(q&&!r.name.toLowerCase().includes(q)&&!r.group.toLowerCase().includes(q))continue;
    if(r.group!==g){g=r.group;h+='<h2>'+esc(g)+'</h2>'}
    const s=status(r);
    let btn=s.busy?'<button disabled>Working…</button>':s.live?`<button onclick="if(confirm('Remove ${esc(r.name)} from the server? Its PokéStops and Gyms disappear for players.'))post('/api/remove',{slug:'${r.slug}'})">Remove</button>`:s.main?'':`<button class="go" onclick="post('/api/add',{slug:'${r.slug}'})">Add to server</button>`;
    h+=`<div class="row"><div class="nm">${esc(r.name)}<div class="st ${s.cls}">${s.txt}</div>${s.busy&&s.pct!=null?`<div class="bar"><i style="width:${s.pct}%"></i></div>`:''}</div>${btn}</div>`;
  }
  h=h||'<p class="sub">No match.</p>';
  if(h!==lastHTML){lastHTML=h;document.getElementById('list').innerHTML=h}
}
async function tick(){
  try{S=await (await fetch('/api/state')).json()}catch(e){document.getElementById('srv').textContent='Windstock Regions is not running.';return}
  document.getElementById('vps').textContent=S.vps;
  const n=Object.keys(S.server.packs).length;
  document.getElementById('srv').innerHTML=S.server.error?'<span class="st bad">Can\'t reach the server: '+esc(S.server.error)+'</span>':(S.server.checked?'Server packs: '+n:'Checking the server…');
  const lg=document.getElementById('log'),stick=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-4;
  lg.textContent=S.log.join('\n')||'Nothing yet.';if(stick)lg.scrollTop=lg.scrollHeight;
  draw();
}
tick();setInterval(tick,1500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            with S.lock:
                srv = dict(S.server)
                jobs = json.loads(json.dumps(S.jobs))
                log = list(S.log)
                queued = list(S.queue.queue)
            regions = [{"group": g, "name": n, "slug": s,
                        "pack": srv["packs"].get(pack_name(s)),
                        "main": pack_name(s) not in srv["packs"] and (
                            s in srv["ledger"] or (s.startswith("north-america/us/")
                                                   and "north-america/us" in srv["ledger"])),
                        "built": os.path.exists(os.path.join(PACKS, pack_name(s)))}
                       for g, n, s in REGIONS]
            self._json({"vps": VPS.split("@")[-1], "server": srv, "jobs": jobs,
                        "log": log, "queued": queued, "regions": regions})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        # Only this page may drive it: a cross-site form post can't set JSON.
        if self.headers.get("Content-Type", "") != "application/json":
            return self._json({"error": "bad request"}, 400)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        slug = body.get("slug")
        if self.path in ("/api/add", "/api/remove") and slug not in NAMES:
            return self._json({"error": "unknown region"}, 400)
        if self.path == "/api/add":
            with S.lock:
                j = S.jobs.get(slug, {})
                busy = (j.get("step") and not j.get("done") and not j.get("error")) \
                    or slug in S.queue.queue
            if busy:
                return self._json({"error": "already queued"})
            S.set(slug, step="", error="", done=False, detail="")
            S.queue.put(slug)
            S.say(f"{slug}: queued")
            self._json({"ok": True})
        elif self.path == "/api/remove":
            try:
                remove(slug)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})
        elif self.path == "/api/refresh":
            refresh_server()
            self._json({"ok": True})
        elif self.path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0))).start()
        else:
            self._json({"error": "not found"}, 404)


def main():
    url = f"http://127.0.0.1:{PORT}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(url)           # already running: just show it
        return
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=refresh_server, daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    print(f"Windstock Regions on {url}  (VPS {VPS})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
