"""
/radar -- the radar, on a computer.

The Help Center already has one, but it is drawn for a phone inside the game's
web view. This is the same thing sized for a desktop browser: sign in with your
trainer name and password, and it sweeps around wherever your phone last told
the server you were (world.player_location), so it works while the game is
closed.

Deliberately thin. Login, the sweep, the cooldown (radar.cooldown_seconds,
0 by default = none), the sprites and the shiny roll all belong to
helpcenter.py, and this page calls the very same
endpoints a phone would:

    POST /hc/login  {player, password}  -> {ok, player, token}
    POST /hc/spot   {token}             -> {ok, lat, lng, range_m}
    POST /hc/radar  {token}             -> {ok, rows[], wait_ms, ...}
    GET  /hc/mon/<id>.png               -> sprite

No CDN: one file, no external scripts or fonts, because the server this runs on
is often a laptop on a home network with no route to anywhere.

Reaching it: a server started by run.py binds 0.0.0.0, so any computer on the
same network can open http://<that machine>:<port>/radar. The copy inside the
iOS launcher binds 127.0.0.1 and is NOT reachable from another machine, by
design -- it has no authentication in front of the World Manager.
"""
import json

PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Windstock Radar</title>
<style>
  :root{
    --bg:#0c1117; --panel:#131b24; --line:#1f2b38; --ink:#e6edf3;
    --dim:#8b9aab; --accent:#2fd39b; --warn:#f2a03d; --bad:#ff8a8a;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{display:flex;align-items:baseline;gap:12px;padding:18px 24px;
         border-bottom:1px solid var(--line)}
  header h1{font-size:18px;margin:0;letter-spacing:.3px}
  header .who{color:var(--dim);font-size:13px}
  header button{margin-left:auto}
  button{background:var(--accent);color:#04231a;border:0;border-radius:8px;
         padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer}
  button.ghost{background:transparent;color:var(--dim);
               border:1px solid var(--line);font-weight:500}
  button:disabled{opacity:.5;cursor:default}
  .wrap{max-width:1100px;margin:0 auto;padding:24px}
  .grid{display:grid;grid-template-columns:minmax(320px,480px) 1fr;gap:24px;
        align-items:start}
  @media (max-width:820px){.grid{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;
         padding:18px}
  .panel h2{margin:0 0 4px;font-size:15px}
  .panel p.sub{margin:0 0 14px;color:var(--dim);font-size:13px}
  label{display:block;font-size:13px;color:var(--dim);margin:12px 0 5px}
  input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--line);
        background:#0b1219;color:var(--ink);font-size:15px}
  .msg{margin-top:12px;font-size:13px;min-height:1.2em}
  .msg.bad{color:var(--bad)} .msg.warn{color:var(--warn)}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;font-weight:600;color:var(--dim);font-size:12px;
     text-transform:uppercase;letter-spacing:.5px;padding:0 8px 8px}
  td{padding:7px 8px;border-top:1px solid var(--line);vertical-align:middle}
  td img{width:34px;height:34px;image-rendering:auto;vertical-align:middle}
  tr.gone{opacity:.4}
  .shiny{color:var(--warn)}
  .foot{color:var(--dim);font-size:12px;margin-top:14px}
  #radar{width:100%;height:auto;display:block}
  .blip{cursor:default}
</style>

<header>
  <h1>Windstock Radar</h1>
  <span class="who" id="who"></span>
  <button class="ghost" id="out" style="display:none">Sign out</button>
</header>

<div class="wrap">
  <!-- login -->
  <div class="panel" id="login" style="max-width:420px;margin:40px auto">
    <h2>Sign in</h2>
    <p class="sub">Your trainer name and password — the same ones you use in the
      game. The radar sweeps wherever your phone last was.</p>
    <label for="u">Trainer name</label>
    <input id="u" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" type="password" autocomplete="current-password">
    <div class="msg" id="lmsg"></div>
    <button id="go" style="margin-top:14px;width:100%">Sign in</button>
  </div>

  <!-- radar -->
  <div class="grid" id="app" style="display:none">
    <div class="panel">
      <svg id="radar" viewBox="0 0 400 400" role="img" aria-label="Radar"></svg>
      <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
        <button id="scan">Scan</button>
        <span class="who" id="pos"></span>
      </div>
      <div class="msg" id="rmsg"></div>
    </div>
    <div class="panel">
      <h2>Nearby</h2>
      <p class="sub" id="count">—</p>
      <table>
        <thead><tr><th></th><th>Pokemon</th><th>Distance</th><th>Leaves in</th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="foot" id="foot"></div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let token = localStorage.getItem("ws_token") || "";
let me = localStorage.getItem("ws_player") || "";
let scan = null, timer = null, cooldownUntil = 0, lastSweep = 0;
// With the cooldown set to 0 the page would re-sweep the instant the last one
// finished -- a request a second, forever. Auto-refresh is its own, slower
// clock; the Scan button is still as fast as the server allows.
const AUTO_REFRESH_MS = 30000;

async function api(path, extra) {
  const body = Object.assign({token}, extra || {});
  const r = await fetch(path, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return r.json();
}

function showLogin(message) {
  $("login").style.display = ""; $("app").style.display = "none";
  $("out").style.display = "none"; $("who").textContent = "";
  $("lmsg").textContent = message || ""; $("lmsg").className = "msg bad";
  token = ""; localStorage.removeItem("ws_token");
}

function showApp() {
  $("login").style.display = "none"; $("app").style.display = "";
  $("out").style.display = ""; $("who").textContent = "signed in as " + me;
}

$("go").onclick = async () => {
  const player = $("u").value.trim(), password = $("p").value;
  if (!player) { $("lmsg").textContent = "Enter your trainer name."; return; }
  $("go").disabled = true; $("lmsg").textContent = "";
  try {
    const r = await fetch("/hc/login", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({player, password})});
    const d = await r.json();
    if (!d.ok) { $("lmsg").textContent = d.message || "Could not sign in."; return; }
    token = d.token; me = d.player;
    localStorage.setItem("ws_token", token); localStorage.setItem("ws_player", me);
    showApp(); sweep();
  } catch (e) {
    $("lmsg").textContent = "Could not reach the server.";
  } finally { $("go").disabled = false; }
};
$("p").addEventListener("keydown", e => { if (e.key === "Enter") $("go").click(); });
$("out").onclick = () => { localStorage.removeItem("ws_player"); showLogin(""); };
$("scan").onclick = () => sweep();

async function sweep() {
  $("rmsg").textContent = ""; $("rmsg").className = "msg";
  $("scan").disabled = true;
  let d;
  try { d = await api("/hc/radar"); }
  catch (e) { $("rmsg").textContent = "Could not reach the server."; $("scan").disabled = false; return; }
  if (d.signed_out) { showLogin("Your session expired — sign in again."); return; }
  if (!d.ok) {
    // Either the cooldown, or the server has never heard where this trainer is.
    $("rmsg").textContent = d.message || "The radar is not ready.";
    $("rmsg").className = "msg warn";
    cooldownUntil = Date.now() + (d.wait_ms || 0);
    if (!d.wait_ms) $("scan").disabled = false;
    return;
  }
  scan = d;
  lastSweep = Date.now();
  cooldownUntil = Date.now() + (d.wait_ms || 0);
  $("pos").textContent = d.lat.toFixed(5) + ", " + d.lng.toFixed(5)
                       + " · " + Math.round(d.range_m) + " m";
  draw();
}

function metres(row) {
  // Offsets in metres from the player, north-up.
  const dy = (row.lat - scan.lat) * 111320;
  const dx = (row.lng - scan.lng) * 111320 * Math.cos(scan.lat * Math.PI / 180);
  return {dx, dy};
}

function draw() {
  const R = 180, cx = 200, cy = 200, range = scan.range_m || 500;
  let svg = "";
  for (const frac of [0.25, 0.5, 0.75, 1]) {
    svg += `<circle cx="${cx}" cy="${cy}" r="${R * frac}" fill="none"
             stroke="#1f2b38" stroke-width="1"/>`;
    svg += `<text x="${cx + 4}" y="${cy - R * frac + 13}" fill="#55677c"
             font-size="10">${Math.round(range * frac)} m</text>`;
  }
  svg += `<line x1="${cx - R}" y1="${cy}" x2="${cx + R}" y2="${cy}" stroke="#1f2b38"/>`;
  svg += `<line x1="${cx}" y1="${cy - R}" x2="${cx}" y2="${cy + R}" stroke="#1f2b38"/>`;
  svg += `<text x="${cx}" y="${cy - R - 6}" fill="#55677c" font-size="11"
           text-anchor="middle">N</text>`;
  svg += `<circle cx="${cx}" cy="${cy}" r="5" fill="#2fd39b"/>`;

  const now = Date.now();
  for (const row of scan.rows) {
    const {dx, dy} = metres(row);
    const d = Math.hypot(dx, dy);
    const k = Math.min(1, d / range);            // clamp anything on the rim
    const ang = Math.atan2(dx, dy);              // 0 = north, clockwise
    const x = cx + Math.sin(ang) * R * k, y = cy - Math.cos(ang) * R * k;
    const left = row.expires_ms ? row.expires_ms - now : 0;
    const dying = left > 0 && left < 60000;
    svg += `<g class="blip"><title>${esc(row.name)} · ${row.distance_m} m</title>`
         + `<circle cx="${x}" cy="${y}" r="${row.shiny ? 7 : 5}"
              fill="${row.shiny ? "#f2a03d" : (dying ? "#8b9aab" : "#79d9ff")}"
              stroke="#0c1117" stroke-width="1.5"/></g>`;
  }
  $("radar").innerHTML = svg;

  const rows = scan.rows.slice().sort((a, b) => a.distance_m - b.distance_m);
  $("count").textContent = rows.length
    ? rows.length + " nearby" + (rows.filter(r => r.shiny).length
        ? " · " + rows.filter(r => r.shiny).length + " shiny" : "")
    : "Nothing in range right now.";
  $("rows").innerHTML = rows.map(r => {
    const left = r.expires_ms ? r.expires_ms - Date.now() : 0;
    return `<tr data-exp="${r.expires_ms || 0}">
      <td><img src="/hc/mon/${r.pokemon_id}.png" alt="" loading="lazy"></td>
      <td>${esc(r.name)}${r.shiny ? ' <span class="shiny">★</span>' : ""}</td>
      <td>${r.distance_m} m</td>
      <td class="left">${clock(left)}</td></tr>`;
  }).join("");
  tick();
}

function clock(ms) {
  if (ms <= 0) return "gone";
  const s = Math.round(ms / 1000);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
}

function tick() {
  // One timer for the countdowns AND the cooldown, so the page settles into a
  // rescan the moment the server will allow one.
  clearTimeout(timer);
  const now = Date.now();
  for (const tr of document.querySelectorAll("#rows tr")) {
    const left = Number(tr.dataset.exp) - now;
    tr.querySelector(".left").textContent = clock(left);
    tr.classList.toggle("gone", left <= 0);
  }
  const wait = cooldownUntil - now;
  $("scan").disabled = wait > 0;
  $("scan").textContent = wait > 0 ? "Scan in " + Math.ceil(wait / 1000) + "s" : "Scan";
  $("foot").textContent = scan
    ? "Swept from where your phone last reported. Open the game to move it."
    : "";
  // auto-refresh once allowed, but no faster than AUTO_REFRESH_MS
  if (wait <= 0 && scan && now - lastSweep >= AUTO_REFRESH_MS) { sweep(); return; }
  timer = setTimeout(tick, 1000);
}

if (token && me) { showApp(); sweep(); } else { showLogin(""); }
</script>
</html>
"""


def handle(method, path, query, headers, body, log):
    if path.rstrip("/") in ("/radar", ""):
        return (200, {"Content-Type": "text/html; charset=utf-8",
                      "Cache-Control": "no-store"}, PAGE.encode("utf-8"))
    return 404, {"Content-Type": "text/plain"}, b"no such page"
