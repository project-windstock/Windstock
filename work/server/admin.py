"""
"World Manager" -- local web UI for the PoGO private server.

Runs on http://127.0.0.1:<port> (localhost only, never exposed to the phone or the
network). Lets you click a map to place PokeStops, Gyms and Pokemon spawns at real
coordinates, and tune the global spawn settings. Everything is written to
places.json / events.json, which the game server hot-reloads on the next map
refresh -- no restart needed.

The map tiles come from OpenStreetMap, so this page needs internet; the placement
controls and the coordinate list still work fine offline.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))

import events as EV
import places as PL
import webui

# Items the "Give to a player" panel can hand out (id -> label). These are the
# ones the 2016 client actually knows how to display in the bag.
# NOTE: 301 is the Lucky Egg and 501 is the Lure Module (Troy Disk). This list
# previously had 501 labelled "Lucky Egg" and 602 labelled "Lure Module" -- 602 is
# actually X Attack, so both of those handed out the wrong item.
GIVEABLE = [(1, "Poke Ball"), (2, "Great Ball"), (3, "Ultra Ball"),
            (4, "Master Ball"),
            (101, "Potion"), (102, "Super Potion"), (103, "Hyper Potion"),
            (104, "Max Potion"), (201, "Revive"), (202, "Max Revive"),
            (701, "Razz Berry"), (401, "Incense"), (301, "Lucky Egg"),
            (501, "Lure Module"), (902, "Egg Incubator")]

# Kanto species names for the picker (index 0 unused)
DEX = [""] + """Bulbasaur Ivysaur Venusaur Charmander Charmeleon Charizard Squirtle Wartortle
Blastoise Caterpie Metapod Butterfree Weedle Kakuna Beedrill Pidgey Pidgeotto Pidgeot Rattata
Raticate Spearow Fearow Ekans Arbok Pikachu Raichu Sandshrew Sandslash NidoranF Nidorina
Nidoqueen NidoranM Nidorino Nidoking Clefairy Clefable Vulpix Ninetales Jigglypuff Wigglytuff
Zubat Golbat Oddish Gloom Vileplume Paras Parasect Venonat Venomoth Diglett Dugtrio Meowth
Persian Psyduck Golduck Mankey Primeape Growlithe Arcanine Poliwag Poliwhirl Poliwrath Abra
Kadabra Alakazam Machop Machoke Machamp Bellsprout Weepinbell Victreebel Tentacool Tentacruel
Geodude Graveler Golem Ponyta Rapidash Slowpoke Slowbro Magnemite Magneton Farfetchd Doduo
Dodrio Seel Dewgong Grimer Muk Shellder Cloyster Gastly Haunter Gengar Onix Drowzee Hypno
Krabby Kingler Voltorb Electrode Exeggcute Exeggutor Cubone Marowak Hitmonlee Hitmonchan
Lickitung Koffing Weezing Rhyhorn Rhydon Chansey Tangela Kangaskhan Horsea Seadra Goldeen
Seaking Staryu Starmie MrMime Scyther Jynx Electabuzz Magmar Pinsir Tauros Magikarp Gyarados
Lapras Ditto Eevee Vaporeon Jolteon Flareon Porygon Omanyte Omastar Kabuto Kabutops Aerodactyl
Snorlax Articuno Zapdos Moltres Dratini Dragonair Dragonite Mewtwo Mew""".split()

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bracky &mdash; World Manager</title>
<link rel="stylesheet" href="/webassets/leaflet/leaflet.css?v=1.9.4"/>
<style>__CSS__
 /* --- World Manager only ------------------------------------------- */
 #map{
   height:54vh;min-height:330px;width:100%;background:#dfeee8;
   border-top:1px solid var(--line);border-bottom:1px solid var(--line);
   box-shadow:inset 0 6px 14px rgba(20,60,80,.08);
 }
 .leaflet-container{font-family:var(--font)}
 .leaflet-popup-content-wrapper,.leaflet-bar a{
   border-radius:var(--r-sm);color:var(--ink);box-shadow:var(--shadow)
 }
 .leaflet-bar a:hover{background:var(--soft)}
 .leaflet-control-attribution{background:rgba(255,255,255,.82)!important}
 #rmap{
   height:46vh;min-height:300px;width:100%;background:#dfeee8;
   border:1px solid var(--line);border-radius:var(--r-sm);
 }
 /* A radar marker is the Pokemon STANDING ON a disc, not filling one: the disc
    is the ground it sits on (and what you aim at), the art rises clear above it.
    Sizing the sprite to the disc instead just reads as a coloured square.
    Named .rmon, not .mon -- the shared stylesheet already uses .mon for the green
    MON tag, and the marker inherited its background. */
 .rmon{width:68px;height:64px;position:relative}
 .rmon .disc{position:absolute;left:50%;bottom:0;width:36px;height:36px;margin-left:-18px;
   border-radius:50%;background:#fff;border:2px solid #fff;
   box-shadow:0 2px 6px rgba(20,60,80,.45)}
 .rmon.sh .disc{background:radial-gradient(circle at 50% 35%,#fff6de,#ffd98a);
   border-color:#f2a03d;box-shadow:0 0 0 3px #ffeab8,0 2px 6px rgba(20,60,80,.45)}
 /* Centred on the disc by transform and sized per species in JS: each sprite is
    cropped tight to the Pokemon and they are all different shapes, so one fixed
    box would leave a wide one a sliver and a tall one towering. */
 .rmon img{position:absolute;left:50%;bottom:13px;transform:translateX(-50%);
   filter:drop-shadow(0 2px 2px rgba(0,0,0,.35))}
 .rmon .star{position:absolute;right:4px;top:0;font-size:15px;color:#f2a03d;
   text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;line-height:1}
 .tag.shiny{background:#f2a03d;color:#fff}
 .row.shiny .t{color:#8a6410;font-weight:700}
 .rstat{align-self:center;font-size:12.5px;color:var(--ink2)}
 .rstat b{color:var(--ink)}
 .rstat .sh{color:#b8860b}
 .rtip{background:#22404c;color:#fff;border:0;border-radius:6px;font:700 11px var(--font);
   padding:2px 6px;box-shadow:none}
 .rtip.sh{background:#f2a03d}
 .rtip:before{display:none}
 /* labels sitting inline in a .bar row, next to the controls */
 .inline-label{align-self:center;font-size:12.5px;color:var(--ink2);font-weight:700}
 .coins{color:#b8860b;font-family:var(--mono)}
 .by{color:var(--ink3)}
</style></head><body>
<header>
  <div class="brand">
    <div class="mark"><svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M5 3v18" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
      <path d="M5 6.6 L20 9 L20 13 L5 15.4 Z" fill="#fff"/>
      <path d="M10 7.3v6.9M14.4 7.9v5.6" stroke="#2358d8" stroke-width="1.4" stroke-linecap="round"/>
    </svg></div>
    <div class="word"><h1>Bracky</h1><small>World Manager</small></div>
  </div>
  <nav class="topnav"><a class="active" href="/">Manager</a><a href="/downloads">World Data</a><a href="/soundpacks">Sound Packs</a><a href="/research">Research</a></nav>
  <div class="spacer"></div>
  <div class="pill" id="status">Running</div>
  <div class="meta">
    Placed <b id="cnt">0</b> &middot; <span id="counts"></span><br>
    Spawn mode <b id="mode">-</b>
  </div>
</header>
<main class="wrap">

<div id="warn" class="warn" style="display:none">
  <b>No Gyms in your world.</b> Random stops/gyms are OFF, so the only ones that exist are the
  ones you place. Choose <b>Gym</b> below and click the map (or use Build ring) &mdash; then you
  can tap it in game and station a Pokemon there.
</div>
<h2>World Manager</h2>
<div class="bar">
  <button id="b-stop" class="on" onclick="setMode('stop')">PokeStop</button>
  <button id="b-gym" onclick="setMode('gym')">Gym</button>
  <button id="b-mon" onclick="setMode('mon')">Pokemon</button>
  <select id="species"></select>
  <input id="pname" placeholder="Name (optional)" size="14">
  <input id="pimg" placeholder="Photo: file in photos/ or URL" size="20">
  <button onclick="togProc('forts')" id="b-pf">Random stops/gyms: OFF</button>
  <button onclick="togProc('spawns')" id="b-ps">Random Pokemon: ON</button>
  <button class="danger" onclick="clearAll()">Clear all</button>
</div>
<div id="map"></div>
<div class="hint">Click the map to place the selected object. Click a marker to remove it.
Changes apply live &mdash; walk around in game and they'll appear.</div>

<h2>Quick Build</h2>
<div class="bar">
  <span class="inline-label">Ring of stops around trainer:</span>
  <input id="ring-n" type="number" value="8" min="1" max="24" size="3" title="how many stops">
  <input id="ring-r" type="number" value="60" min="10" max="500" size="4" title="radius in metres">
  <button onclick="ring()">Build ring</button>
</div>

<h2>PokeStop Loot</h2>
<div class="list" id="loot"></div>
<div class="bar">
  <button onclick="lootAdd()">+ Add item</button>
  <select id="loot-mode" title="how chances are used">
    <option value="weighted">Weighted: each item picked by odds (real game)</option>
    <option value="chance">Chance: each row rolls on its own</option>
  </select>
  <label class="inline-label">Items per spin
    <input id="loot-min" type="number" min="0" max="999" size="3"> &ndash;
    <input id="loot-max" type="number" min="0" max="999" size="3"></label>
  <button onclick="lootSave()">Save loot</button>
  <button onclick="lootLoad()">Revert</button>
</div>
<div class="hint" id="loothint">Weighted: a spin gives "items per spin" items, each picked
using the chances as odds (count columns are ignored). Chance: 1 = row always drops, 0 =
never, and the first row tops a spin up to the minimum. Applies to the next spin &mdash; no
restart.</div>

<h2>Raid</h2>
<div class="bar">
  <button onclick="raidToggle()" id="b-raid">Raid: off</button>
  <select id="raid-mon"></select>
  <label class="inline-label">CP
    <input id="raid-cp" type="number" value="3000" min="10" max="9999" size="5"></label>
  <input id="raid-name" value="raid" size="8" title="trainer name shown at the gym">
  <button onclick="raidSave()">Apply</button>
</div>
<div class="hint" id="raidhint">Puts one boss in EVERY gym, replacing whatever is
defending (real defenders are sent home first, nothing is lost). Raids are multiplayer:
each gym's boss has one shared HP pool (settings.json &rarr; raids) that every trainer
hits together. When it falls, everyone who dealt enough damage gets it dropped at their
own feet to catch, and that gym's boss respawns a few minutes later.</div>

<h2>Radar</h2>
<div class="bar">
  <button onclick="sweep()" id="b-sweep">Sweep now</button>
  <button onclick="togLive()" id="b-live">Live: off</button>
  <button onclick="rFitNow()" title="Frame everything on the radar">Fit</button>
  <span class="inline-label">Trainer reach</span>
  <select id="r-range" onchange="sweep()">
    <option value="250">250 m</option>
    <option value="500">500 m</option>
    <option value="1000" selected>1 km</option>
    <option value="2000">2 km</option>
    <option value="5000">5 km</option>
  </select>
  <label class="inline-label"><input type="checkbox" id="r-onlysh" onchange="rPaint()">
    Shinies only</label>
  <span class="rstat" id="r-stat">Not swept yet.</span>
</div>
<div id="rmap"></div>
<div class="hint">Every wild Pokemon on the clock right now, straight out of the spawn
table &mdash; shinies in gold. Nothing is hidden: a spawn shows here whether or not
anyone is near it, so you can watch the world fill even with no one playing.
<b>Trainer reach</b> only decides which trainers count as being in range of a spawn (and
how big their ring is drawn) &mdash; it never drops one from the list. Trainers appear
once the game has reported their position. Players get the same sweep from the Help
Center radar, but scoped to themselves, once a minute, and without whatever they have
already caught.<br>
<b>With nobody playing</b>, a sweep also spawns Pokemon &mdash; across the whole
<b>Trainer reach</b> circle around the map centre, through the game's own spawner, so
density, biome and the live event all apply. It fills a patch of ground at a time
(about a dozen per sweep, once every 5 seconds) and never re-fills ground it has
already done: what a place holds is fixed until the spawn window turns over, so asking
again would only return the same Pokemon. Keep sweeping, or turn Live on, until it says
the area is filled. As soon as a trainer is on, the game spawns around them and the
sweep only looks.</div>

<h2>Nominations</h2>
<div class="hint">Added by players from the in-game Help Center
(Settings &rarr; support). These go straight into the world &mdash; one per player
per day. Remove one here if it shouldn't be there.</div>
<div class="list" id="noms"><div class="empty">No nominations waiting.</div></div>

<h2>Give to a player</h2>
<div class="bar">
  <span class="inline-label">Trainer</span>
  <input id="giveuser" list="accounts" placeholder="username" size="14"
         title="Leave blank for the trainer currently playing">
  <datalist id="accounts"></datalist>
  <span class="hint" style="align-self:center">Blank = whoever is playing now</span>
</div>
<div class="bar">
  <select id="giveitem"></select>
  <input id="giveqty" type="number" value="20" min="1" max="999" size="4">
  <button onclick="give()">Add items</button>
</div>
<div class="bar">
  <select id="givecandy"></select>
  <input id="givecandyqty" type="number" value="25" min="1" max="999" size="4">
  <button onclick="giveCandy()">Add candy</button>
</div>
<div class="bar">
  <input id="givedust" type="number" value="1000" min="1" max="999999" size="7">
  <button onclick="giveDust()">Add stardust</button>
</div>
<div class="bar">
  <input id="givexp" type="number" value="10000" min="1" max="99999999" size="8">
  <button onclick="giveXp()">Add XP</button>
  <input id="givelevel" type="number" value="5" min="1" max="500" size="4">
  <button onclick="setLevel()">Set level</button>
</div>
<div class="bar">
  <input id="newpw" placeholder="New password" size="14">
  <button onclick="resetPw()">Reset password</button>
</div>
<div class="hint" id="givehint">Candy goes to the whole evolution family, so Charmander
candy also powers up Charmeleon and Charizard. Everything shows up in game within a
few seconds.</div>

<h2>Events</h2>
<div class="bar" id="presets"></div>
<div class="bar">
  <input id="ev-name" placeholder="Event name" size="14">
  <label class="inline-label">Density
    <input id="ev-density" type="number" min="0" max="60" size="3"></label>
  <select id="ev-mode">
    <option value="all">All 151</option><option value="list">From list</option>
    <option value="single">One species</option>
  </select>
  <select id="ev-species" title="which species, when the mode is One species"></select>
  <input id="ev-list" placeholder="1,4,7,25" size="12" title="species list">
  <label class="inline-label">CP
    <input id="ev-min" type="number" min="10" max="5000" size="4"> &ndash;
    <input id="ev-max" type="number" min="10" max="5000" size="4"></label>
  <button onclick="saveEv()">Apply event</button>
</div>
<div class="hint">Density = wild Pokemon around you (0&ndash;60). "One species" + a
Pokemon makes a themed event, e.g. a Pikachu festival.</div>

<h2>Scheduled events</h2>
<div class="bar">
  <input id="sc-name" placeholder="Community Day" size="14">
  <select id="sc-preset"></select>
  <label class="inline-label">from <input id="sc-start" value="11:00" size="4"></label>
  <label class="inline-label">to <input id="sc-end" value="14:00" size="4"></label>
  <input id="sc-days" placeholder="days e.g. 5,6" size="9" title="Mon=0 ... Sun=6; blank = every day">
  <button onclick="scAdd()">Add</button>
</div>
<div class="hint" id="sc-now">Runs itself: while a row's time window is on, its event is
live and everything goes back to normal afterwards. Mon=0 &hellip; Sun=6.</div>
<div class="list" id="sc-list"></div>

<div class="list" id="list"></div>
</main>

<script src="/webassets/leaflet/leaflet.js?v=1.9.4"></script>
<script>
// Served from this machine, not a CDN: the phone runs the whole server
// offline, and when unpkg was unreachable `L` was undefined, the first
// L.map() threw, and every control BELOW it on the page stopped working too.
if (window.L) L.Icon.Default.prototype.options.imagePath = "/webassets/leaflet/";
</script>
<script>
let mode='stop', map, layer, data={forts:[],spawns:[]};
const $=i=>document.getElementById(i);
const DEX=__DEX__;

function setMode(m){mode=m;['stop','gym','mon'].forEach(k=>$('b-'+k).className=(k===m?'on':''));}
function icon(color,txt){return L.divIcon({className:'',html:
 `<div style="background:${color};border:2px solid #fff;border-radius:50%;width:26px;height:26px;
   display:flex;align-items:center;justify-content:center;font:700 11px monospace;color:#fff;
   box-shadow:0 1px 4px rgba(0,0,0,.5)">${txt}</div>`, iconSize:[26,26], iconAnchor:[13,13]});}

// --- radar -----------------------------------------------------------------
// Its own map, deliberately: the map above is for PLACING things, and a click
// there drops a stop. Nothing on the radar is clickable into the world.
let rmap=null, rlayer=null, rdata=null, rlive=null, rFit=true;
function rFitNow(){ rFit=true; rPaint(); }
// {dex: [w,h]} for the species we have art for; anything else keeps the numbered
// dot, so a half-filled art folder is still a usable radar.
let MONART = {};
// Scale by AREA, not by fitting a box: every sprite then carries about the same
// visual weight whatever its shape, which is what a fixed square box gets wrong.
// The cap stops a big wide one from swamping its neighbours.
function monBox(wh){
  const TARGET=40, CAP=58;
  const s=TARGET/Math.sqrt(wh[0]*wh[1]);
  let w=wh[0]*s, h=wh[1]*s;
  const k=Math.min(1, CAP/Math.max(w,h));
  return [Math.round(w*k), Math.round(h*k)];
}
function rIcon(r){
  const wh=MONART[r.pokemon_id];
  if(wh){
    const [w,h]=monBox(wh);
    return L.divIcon({className:'',
      html:`<div class="rmon${r.shiny?' sh':''}"><span class="disc"></span>`
          +`<img src="/monart/${r.pokemon_id}.png" alt="" width="${w}" height="${h}">`
          +`${r.shiny?'<span class="star">★</span>':''}</div>`,
      iconSize:[68,64], iconAnchor:[34,62]});
  }
  const txt=r.shiny?'★':String(r.pokemon_id), sh=r.shiny;   // no art: numbered dot
  return L.divIcon({className:'',html:
  `<div style="background:${sh?'#f2a03d':'#6f8f9e'};border:2px solid #fff;border-radius:50%;
    width:${sh?30:24}px;height:${sh?30:24}px;display:flex;align-items:center;
    justify-content:center;font:700 ${sh?13:10}px monospace;color:#fff;
    box-shadow:0 1px 4px rgba(0,0,0,.45)${sh?';outline:3px solid #ffeab8':''}">${txt}</div>`,
  iconSize:[sh?30:24,sh?30:24], iconAnchor:[sh?15:12,sh?15:12]});}
function rDist(m){return m>=1000 ? (m/1000).toFixed(1)+' km' : m+' m';}
// Who is near it -- everyone in reach, or failing that the closest trainer there
// is, so a far-off spawn still says something useful instead of nothing.
function rWho(r){
  if(r.near.length) return r.near.map(n=>`${n.who} ${rDist(n.m)}`).join(', ');
  if(r.nearest) return `nearest ${r.nearest.who} ${rDist(r.nearest.m)}`;
  return 'no trainer about';
}
function goneIn(ms){
  const left=ms-Date.now();
  if(!ms||left<=0) return 'going now';
  return left<60000 ? 'under 1 min left' : Math.round(left/60000)+' min left';
}
async function sweep(){
  $('b-sweep').disabled=true;
  const label=$('b-sweep').textContent; $('b-sweep').textContent='Sweeping…';
  // Where we are looking: the radar map once it exists, otherwise the placement
  // map above. With nobody playing, that is where the server puts the spawns.
  const src = rmap || map, c = src && src.getCenter();
  let u = '/api/radar?range='+encodeURIComponent($('r-range').value);
  if(c) u += '&lat='+c.lat+'&lng='+c.lng;
  try{
    rdata=await (await fetch(u)).json();
  }finally{ $('b-sweep').disabled=false; $('b-sweep').textContent=label; }
  rPaint();
}
function togLive(){
  if(rlive){ clearInterval(rlive); rlive=null; $('b-live').className=''; $('b-live').textContent='Live: off'; return; }
  rlive=setInterval(sweep,5000); $('b-live').className='on'; $('b-live').textContent='Live: on';
  sweep();
}
function rPaint(){
  if(!rdata) return;
  MONART = rdata.art||{};
  const onlySh=$('r-onlysh').checked;
  const rows=rdata.rows.filter(r=>!onlySh||r.shiny);
  if(!rmap && window.L){
    const t=rdata.trainers[0];
    try{
    rmap=L.map('rmap').setView([t?t.lat:39.19,t?t.lng:-96.58], t?16:4);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
      attribution:'&copy; OpenStreetMap'}).addTo(rmap);
    rlayer=L.layerGroup().addTo(rmap);
    }catch(err){ rmap=null; console.error('raid map failed',err); }
  }
  // Offline (no map library): skip everything that draws onto it, but let the
  // counts and the shiny list below still render.
  if(rmap && rlayer){
  rlayer.clearLayers();
  const pts=[];
  rdata.trainers.forEach(t=>{
    pts.push([t.lat,t.lng]);
    L.circle([t.lat,t.lng],{radius:rdata.range_m,color:'#2b6cf6',weight:1,
      fillColor:'#2b6cf6',fillOpacity:.04}).addTo(rlayer);
    L.circleMarker([t.lat,t.lng],{radius:7,color:'#fff',weight:3,
      fillColor:'#2b6cf6',fillOpacity:1}).addTo(rlayer)
      .bindTooltip(t.name+' — '+t.count+' in reach',{permanent:true,direction:'top',
                   className:'rtip',offset:[0,-6]});
  });
  // Shiny labels ride along on the map, but only while there are few enough to
  // read -- on a busy event they stack into a wall, so past a handful they go
  // back to hover-only and you get them by pointing at one.
  const label = rows.filter(r=>r.shiny).length <= 6;
  rows.forEach(r=>{
    pts.push([r.lat,r.lng]);
    L.marker([r.lat,r.lng],{icon:rIcon(r),zIndexOffset:r.shiny?1000:0})
      .addTo(rlayer)
      .bindTooltip((r.shiny?'★ ':'')+r.name+(r.cp?' CP '+r.cp:'')
                   +' — '+rWho(r)+' · '+goneIn(r.expires_ms),
                   {permanent:!!r.shiny&&label,direction:'top',
                    className:'rtip'+(r.shiny?' sh':''),
                    offset:[0,MONART[r.pokemon_id]?-60:-10]});
  });
  // Frame everything the FIRST time only. Refitting on every sweep threw away
  // wherever you had panned or zoomed to -- and with Live on it did that every
  // five seconds. Use Fit to get the overview back on demand.
  if(pts.length && rFit){ rFit=false; rmap.fitBounds(L.latLngBounds(pts).pad(0.08)); }
  setTimeout(()=>rmap.invalidateSize(),80);
  }
  // The spawn count stands on its own -- with nobody playing there is still a
  // world to look at, so this never falls back to "no trainers" and stops there.
  $('r-stat').innerHTML = `<b>${rdata.rows.length}</b> wild`
    + (rdata.shiny?` · <b class="sh">${rdata.shiny} shiny</b>`:'')
    + (rdata.trainers.length
        ? ` · <b>${rdata.in_range}</b> in reach of `
          + `<b>${rdata.trainers.length}</b> trainer${rdata.trainers.length===1?'':'s'}`
        : ' · no trainer playing'
          + (rdata.seeded?` · <b class="sh">+${rdata.seeded} spawned</b>`:'')
          // How much of the circle still has no spawns. Sweep again (or leave
          // Live on) and it works through the rest, a patch at a time.
          + (rdata.seed_left>0?` · ${rdata.seed_left} patches left to fill`
             : (rdata.can_seed&&rdata.seed_left===0?' · area filled':''))
          + (rdata.seed_radius_m<rdata.range_m
             ? ` · filling the inner ${(rdata.seed_radius_m/1000)} km`:''))
    + ` · ${new Date(rdata.ts).toLocaleTimeString()}`;
}

let player={lat:0,lng:0};
async function ring(){
  if(!player.lat){alert('No trainer position yet - open the game first, or click the map to place manually.');return;}
  await post('/api/ring',{lat:player.lat,lng:player.lng,count:+$('ring-n').value,
    radius_m:+$('ring-r').value,gym:true});
  load();
}
async function makeStop(){
  if(!player.lat){alert('No trainer position yet - open the game first so it reports where you are.');return;}
  const r = await post('/api/makestop',{lat:player.lat,lng:player.lng,player:$('giveuser').value});
  const h=$('stophint'); h.textContent=(r.ok?'✓ ':'✗ ')+r.message;
  h.style.color = r.ok ? '#7fd1a6' : '#ff9a9a';
  load();
}
async function saveEv(){
  await post('/api/save',{event_name:$('ev-name').value,spawn_density:+$('ev-density').value,
    species_mode:$('ev-mode').value,
    species_list:$('ev-list').value.split(',').map(x=>parseInt(x.trim())).filter(x=>x>=1&&x<=151),
    single_species:+$('ev-species').value,
    min_cp:+$('ev-min').value,max_cp:+$('ev-max').value});
  load();
}
async function preset(n){await post('/api/preset',{name:n});load();}
let SCHED=[];
function scPaint(r){
  SCHED=r.rows||[];
  const box=$('sc-list'); box.innerHTML='';
  $('sc-now').textContent = r.active ? ('Running now: '+r.active) :
    'Nothing scheduled right now. Mon=0 ... Sun=6; blank days = every day.';
  SCHED.forEach((row,i)=>{
    const d=document.createElement('div'); d.className='bar';
    d.textContent=`${row.name}  ${row.start}-${row.end}  ${row.preset||'custom'}`
      +(row.days&&row.days.length?('  days '+row.days.join(',')):'  every day');
    const del=document.createElement('button'); del.textContent='Remove';
    del.onclick=async()=>{SCHED.splice(i,1);scPaint(await post('/api/schedule',{rows:SCHED}));};
    d.appendChild(del); box.appendChild(d);
  });
}
async function scAdd(){
  const days=$('sc-days').value.split(',').map(x=>parseInt(x.trim())).filter(x=>x>=0&&x<=6);
  SCHED.push({name:$('sc-name').value||'Event',preset:$('sc-preset').value,
    start:$('sc-start').value,end:$('sc-end').value,days:days,enabled:true});
  scPaint(await post('/api/schedule',{rows:SCHED}));
}
let LOOT_ITEMS=[];
function lootRow(item,chance,min,max){
  const row=document.createElement('div'); row.className='bar loot-row';
  const sel=document.createElement('select');
  LOOT_ITEMS.forEach(n=>{const o=document.createElement('option');o.value=n;
    o.textContent=n.replace(/_/g,' ');sel.appendChild(o);});
  sel.value=item;
  const num=(v,step,mx,t)=>{const i=document.createElement('input');i.type='number';
    i.min=0;i.max=mx;i.step=step;i.value=v;i.size=4;i.title=t;return i;};
  const lbl=t=>{const s=document.createElement('span');s.className='inline-label';s.textContent=t;return s;};
  const up=document.createElement('button');up.textContent='↑';
  up.onclick=()=>{if(row.previousElementSibling)row.parentNode.insertBefore(row,row.previousElementSibling);};
  const rm=document.createElement('button');rm.textContent='Remove';rm.className='danger';
  rm.onclick=()=>row.remove();
  row.append(sel,lbl('chance'),num(chance,0.05,1,'chance'),lbl('count'),
    num(min,1,999,'min'),lbl('–'),num(max,1,999,'max'),up,rm);
  $('loot').appendChild(row);
}
async function lootLoad(){
  const r=await post('/api/loot',{}); LOOT_ITEMS=r.items; $('loot').innerHTML='';
  Object.entries(r.loot||{}).forEach(([n,s])=>lootRow(n,s.chance??1,s.min??1,s.max??1));
  $('loot-min').value=r.min_items; $('loot-max').value=r.max_items;
  $('loot-mode').value=r.mode==='weighted'?'weighted':'chance';
}
function lootAdd(){lootRow(LOOT_ITEMS[0],0.5,1,1);}
async function lootSave(){
  const rows=[...document.querySelectorAll('.loot-row')].map(row=>{
    const i=row.querySelectorAll('input');
    return {item:row.querySelector('select').value,chance:+i[0].value,min:+i[1].value,max:+i[2].value};});
  const names=rows.map(r=>r.item);
  if(new Set(names).size!==names.length){$('loothint').textContent='✗ Each item can only appear once.';
    $('loothint').style.color='#ff9a9a';return;}
  await post('/api/loot',{loot:rows,mode:$('loot-mode').value,min_items:+$('loot-min').value,max_items:+$('loot-max').value});
  await lootLoad();
  $('loothint').textContent='✓ Saved — applies to the next spin.'; $('loothint').style.color='#7fd1a6';
}
function raidPaint(r){
  $('b-raid').textContent = 'Raid: ' + (r.on ? 'ON' : 'off');
  $('b-raid').style.background = r.on ? '#8a2b2b' : '';
  if(r.pokemon_id) $('raid-mon').value = r.pokemon_id;
  if(r.cp) $('raid-cp').value = r.cp;
  if(r.trainer) $('raid-name').value = r.trainer;
}
async function raidToggle(){
  const r = await post('/api/raid', {on: $('b-raid').textContent.indexOf('ON') < 0,
    pokemon_id:+$('raid-mon').value, cp:+$('raid-cp').value, trainer:$('raid-name').value});
  raidPaint(r); $('raidhint').textContent = r.message; load();
}
async function raidSave(){
  const r = await post('/api/raid', {pokemon_id:+$('raid-mon').value,
    cp:+$('raid-cp').value, trainer:$('raid-name').value});
  raidPaint(r); $('raidhint').textContent = r.message; load();
}
async function loadNoms(){
  const r = await post('/api/noms', {});
  const box = $('noms');
  if (!r.rows || !r.rows.length){
    box.innerHTML = '<div class="empty">Nothing added yet.</div>'; return;
  }
  box.innerHTML = '';
  r.rows.forEach(function(n){
    const d = document.createElement('div'); d.className='row';
    d.innerHTML = '<span class="tag ' + (n.kind==='gym'?'gym':'stop') + '">'
      + (n.kind==='gym'?'GYM':'STOP') + '</span>'
      + '<span class="t"><b>' + n.name + '</b> &mdash; ' + n.lat.toFixed(5) + ', '
      + n.lng.toFixed(5) + '<br><small style="color:#8892a0">by ' + n.player
      + (n.note ? ' &middot; ' + n.note : '') + '</small></span>';
    const no = document.createElement('span');
    no.className='x'; no.textContent='remove';
    no.onclick = function(){ resolveNom(n.id,'rejected'); };
    d.appendChild(no); box.appendChild(d);
  });
}
async function resolveNom(id, status){
  const r = await post('/api/noms/resolve', {id: id, status: status});
  loadNoms(); load();
}
function giveResult(r){
  $('givehint').textContent = (r.ok?'\u2713 ':'\u2717 ') + r.message;
  $('givehint').style.color = r.ok ? '#7fd1a6' : '#ff9a9a';
  load();
}
async function give(){
  giveResult(await post('/api/give',{player:$('giveuser').value,
    kind:'item', item_id:+$('giveitem').value, count:+$('giveqty').value}));
}
async function giveCandy(){
  giveResult(await post('/api/give',{player:$('giveuser').value,
    kind:'candy', pokemon_id:+$('givecandy').value, count:+$('givecandyqty').value}));
}
async function resetPw(){
  const who = $('giveuser').value.trim();
  if(!who) return giveResult({ok:false, message:'Type which trainer first'});
  giveResult(await post('/api/setpw', {player: who, password: $('newpw').value}));
  $('newpw').value='';
}
async function giveDust(){
  giveResult(await post('/api/give',{player:$('giveuser').value,
    kind:'stardust', count:+$('givedust').value}));
}
async function giveXp(){
  giveResult(await post('/api/give',{player:$('giveuser').value,
    kind:'xp', count:+$('givexp').value}));
}
async function setLevel(){
  giveResult(await post('/api/give',{player:$('giveuser').value,
    kind:'level', level:+$('givelevel').value}));
}

async function load(){
  const j=await (await fetch('/api/world')).json();
  data=j.places; player=j.player; $('cnt').textContent=data.forts.length+data.spawns.length;
  const ngym=data.forts.filter(f=>f.kind==='gym').length;
  const nstop=data.forts.length-ngym;
  $('warn').style.display=(ngym===0&&!data.procedural_forts)?'block':'none';
  $('counts').textContent=nstop+' PokeStops / '+ngym+' Gyms / '+data.spawns.length+' spawn points';
  $('mode').textContent=j.config.event_name+' / density '+j.config.spawn_density;
  $('b-pf').textContent='Random stops/gyms: '+(data.procedural_forts?'ON':'OFF');
  $('b-ps').textContent='Random Pokemon: '+(data.procedural_spawns?'ON':'OFF');
  $('b-pf').className=data.procedural_forts?'on':'';
  $('b-ps').className=data.procedural_spawns?'on':'';
  const c=j.config;
  $('ev-name').value=c.event_name; $('ev-density').value=c.spawn_density;
  $('ev-mode').value=c.species_mode; $('ev-list').value=(c.species_list||[]).join(',');
  $('ev-species').value=c.single_species||25;
  $('ev-min').value=c.min_cp; $('ev-max').value=c.max_cp;
  if(!$('presets').dataset.done){
    (j.presets||[]).forEach(n=>{const b=document.createElement('button');
      b.textContent=n;b.onclick=()=>preset(n);$('presets').appendChild(b);});
    (j.presets||[]).forEach(n=>{const o=document.createElement('option');
      o.value=n;o.textContent=n;$('sc-preset').appendChild(o);});
    $('presets').dataset.done='1';
  }
  if(!map && window.L){
    // Never let the map take the page with it. The controls below (give, loot,
    // raids, accounts) are the point of this page on a phone, and they used to
    // die with a "L is not defined" when the map library could not be reached.
    try{
      map=L.map('map').setView([j.player.lat||39.19,j.player.lng||-96.58], j.player.lat?18:16);
      const tiles=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
        attribution:'&copy; OpenStreetMap'}).addTo(map);
      // Tiles are the ONE thing here that needs the internet. Without them the
      // map still works -- you can pan, tap to place, and see what is already
      // there -- so say so once instead of leaving a blank grey box.
      tiles.once('tileerror',()=>{ if(!$('offline-note')){
        const n=document.createElement('div'); n.id='offline-note'; n.className='hint';
        n.textContent='No map pictures (this server is offline) — tap the empty '
                    + 'map to place things, or use "here" to drop one at the trainer.';
        $('map').insertAdjacentElement('afterend',n); } });
      layer=L.layerGroup().addTo(map);
      map.on('click',e=>place(e.latlng.lat,e.latlng.lng));
      if(j.player.lat) L.circleMarker([j.player.lat,j.player.lng],{radius:7,color:'#ffd34d',
        fillColor:'#ffd34d',fillOpacity:1}).addTo(map).bindTooltip('Trainer');
    }catch(err){ map=null; console.error('map failed',err); }
  }
  if(!map && !$('offline-note')){
    const n=document.createElement('div'); n.id='offline-note'; n.className='hint';
    n.textContent='The map could not start, so placing by tapping is unavailable. '
                + 'Everything else on this page still works.';
    $('map').insertAdjacentElement('afterend',n);
  }
  draw();
}
function draw(){
  if(!map||!layer) return;          // offline: nothing to draw onto
  layer.clearLayers();
  data.forts.forEach(f=>L.marker([f.lat,f.lng],{icon:icon(f.kind==='gym'?'#c0392b':'#2b6cf6',
    f.kind==='gym'?'G':'S')}).addTo(layer).bindTooltip(f.name).on('click',()=>del(f.id)));
  data.spawns.forEach(s=>L.marker([s.lat,s.lng],{icon:icon(s.pokemon_id?'#1f9d55':'#7a5cc4',s.pokemon_id?String(s.pokemon_id):'?')})
    .addTo(layer).bindTooltip(s.pokemon_id?(DEX[s.pokemon_id]||('#'+s.pokemon_id)):'Random').on('click',()=>del(s.id)));
  const rows=[...data.forts.map(f=>({id:f.id,cls:f.kind==='gym'?'gym':'stop',
      tag:f.kind==='gym'?'GYM':'STOP',
      t:`${f.name}${f.image?' [photo]':''} — ${f.lat.toFixed(5)}, ${f.lng.toFixed(5)}`})),
    ...data.spawns.map(s=>({id:s.id,cls:'mon',tag:s.pokemon_id?'MON':'RANDOM',
      t:`${s.pokemon_id?(DEX[s.pokemon_id]||('#'+s.pokemon_id)):'Random Pokemon'} — ${s.lat.toFixed(5)}, ${s.lng.toFixed(5)}`}))];
  $('list').innerHTML = rows.length ? rows.map(r=>
    `<div class="row"><span class="tag ${r.cls}">${r.tag}</span><span class="t">${r.t}</span>
     <span class="x" onclick="del('${r.id}')">remove</span></div>`).join('')
    : '<div class="empty">Nothing placed yet — click the map above.</div>';
}
async function post(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(b||{})})).json();}
async function place(lat,lng){
  const name=$('pname').value;
  if(mode==='mon') await post('/api/spawn',{lat,lng,pokemon_id:+$('species').value,name});
  else await post('/api/fort',{lat,lng,kind:mode,name,image:$('pimg').value});
  load();
}
async function del(id){await post('/api/remove',{id});load();}
async function clearAll(){if(confirm('Remove every placed object?')){await post('/api/clear',{});load();}}
async function togProc(what){
  const cur = what==='forts' ? data.procedural_forts : data.procedural_spawns;
  await post('/api/procedural',{on:!cur, what}); load();
}
(__GIVEABLE__).forEach(([id,label])=>{const o=document.createElement('option');
  o.value=id;o.textContent=label;$('giveitem').appendChild(o);});
DEX.forEach((n,i)=>{if(i){const o=document.createElement('option');o.value=i;
  o.textContent=n+' candy';$('givecandy').appendChild(o);}});
$('givecandy').value=25;
DEX.forEach((n,i)=>{if(i){const o=document.createElement('option');o.value=i;
  o.textContent=i+' '+n;$('ev-species').appendChild(o);}});
$('ev-species').value=25;
DEX.forEach((n,i)=>{if(i){const o=document.createElement('option');o.value=i;
  o.textContent=i+' '+n;$('raid-mon').appendChild(o);}});
$('raid-mon').value=150;
post('/api/raid',{}).then(raidPaint);
post('/api/schedule',{}).then(scPaint);
lootLoad();
post('/api/accounts',{}).then(r=>{(r.accounts||[]).forEach(n=>{
  const o=document.createElement('option');o.value=n;$('accounts').appendChild(o);});});
{const o=document.createElement('option');o.value=0;
 o.textContent='Random Pokemon';$('species').appendChild(o);}
DEX.forEach((n,i)=>{if(i){const o=document.createElement('option');o.value=i;
  o.textContent=i+' '+n;$('species').appendChild(o);}});
$('species').value=0;
load(); loadNoms(); setInterval(load, 15000); setInterval(loadNoms, 20000);
</script></body></html>"""


# A sweep with nobody playing seeds the world itself. 5s apart at most, which is
# also the Live interval -- leave Live on over an empty server and the map keeps
# filling the way it would if someone were walking there.
RADAR_SEED_COOLDOWN_MS = 5000
# Ground covered per sweep, in level-15 cells (~290x200 m each). A sweep fills
# the whole range eventually, but not in one go: each cell is its own call to the
# map builder at roughly a third of a second, and a 1 km circle is about fifty of
# them. Twelve keeps a sweep inside its own 5s cooldown, so Live never falls
# behind itself, and the area fills over a handful of sweeps instead of hanging
# the page for half a minute.
RADAR_CELLS_PER_SWEEP = 12
# How wide a circle a sweep will FILL, however wide a reach you are looking at.
# A 1 km circle already makes ~540 spawns and world.SPAWNS holds 4000 before it
# starts evicting, so filling a 5 km one would quietly throw away what it made
# earlier in the same sweep. Reading is not capped -- the radar still lists every
# spawn out to whatever reach you pick.
RADAR_SEED_MAX_M = 2000.0
_radar_seeded = [0]


def seed_spawns(lat, lng, radius_m=1000.0):
    """Fill the area around a point with wild Pokemon. Returns (new, left).

    The work (and the record of which ground is already done) lives in
    spawnfill, shared with the Help Center radar so the two never redo each
    other's cells. This adds the manager's own rate limit on top.
    """
    import time
    import spawnfill
    if time.time() * 1000 - _radar_seeded[0] < RADAR_SEED_COOLDOWN_MS:
        return 0, -1                                   # -1: didn't look
    try:
        return spawnfill.fill(lat, lng, min(float(radius_m), RADAR_SEED_MAX_M),
                              RADAR_CELLS_PER_SWEEP)
    finally:
        # Stamped AFTER the work, not before: the first sweep is slow (it loads
        # the OSM and biome tables, the same cost a trainer's first map request
        # pays), and a window opened before that would have elapsed by the time
        # it ended, letting the next sweep straight through.
        _radar_seeded[0] = time.time() * 1000


def radar(range_m=1000.0, seed_at=None):
    """The live wild spawn table, plus every trainer whose position we know.

    EVERY spawn is listed, whether or not a trainer is standing near it -- the
    manager is here to see the world, so an empty room still shows what is out
    there. `range_m` only decides which trainers count as "in reach" of a spawn
    (and how big their ring is drawn); it never hides one.

    Shinies are settled by shiny.is_shiny, so what shows here is exactly what the
    phone will get when it taps that spawn -- looking never rerolls it.
    """
    import math
    import time
    import world
    import shiny as _shiny
    with world._lock:
        locs = dict(world.PLAYER_LOC)
    if not locs:
        # Nobody has been placed by name yet (an old client, or the very first
        # request) -- fall back to the single last-seen position.
        try:
            import rpc
            if abs(rpc._last_loc[0]) > 1e-6 or abs(rpc._last_loc[1]) > 1e-6:
                locs = {rpc._last_user[0] or "Trainer": tuple(rpc._last_loc)}
        except Exception:
            pass
    # Nobody playing: the sweep fills the world where the manager is looking, so
    # the radar has something to show. With a trainer on, the game is already
    # spawning around them and a second source would just crowd their map.
    seeded, left = 0, 0
    seed_r = min(range_m, RADAR_SEED_MAX_M)
    if not locs and seed_at:
        seeded, left = seed_spawns(seed_at[0], seed_at[1], range_m)
    who = sorted(locs.items())
    rows = []
    for sp in world.live_spawns():
        pid = int(sp.get("pokemon_id") or 0)
        near, nearest = [], None
        for name, (plat, plng) in who:
            d = math.hypot(sp["lat"] - plat,
                           (sp["lng"] - plng) * max(0.05, math.cos(math.radians(plat)))
                           ) * 111_000.0
            hit = {"who": name, "m": round(d)}
            if d <= range_m:
                near.append(hit)
            if nearest is None or d < nearest["m"]:
                nearest = hit
        near.sort(key=lambda n: n["m"])
        rows.append({
            "eid": str(sp["eid"]), "pokemon_id": pid,
            "name": DEX[pid] if 1 <= pid < len(DEX) else f"#{pid}",
            "lat": sp["lat"], "lng": sp["lng"],
            "cp": int(sp.get("cp") or 0),
            "expires_ms": int(sp.get("expires_ms") or 0),
            "shiny": bool(_shiny.is_shiny(sp["eid"], pid)),
            "near": near, "nearest": nearest,
        })
    # In reach first (closest of all), then everything else by how soon it goes.
    rows.sort(key=lambda r: (0, r["near"][0]["m"]) if r["near"]
              else (1, r["expires_ms"] or 0))
    trainers = [{"name": n, "lat": la, "lng": ln,
                 "count": sum(1 for r in rows
                              if any(x["who"] == n for x in r["near"]))}
                for n, (la, ln) in who]
    import monart
    return {"trainers": trainers, "rows": rows, "range_m": range_m,
            "shiny": sum(1 for r in rows if r["shiny"]),
            "in_range": sum(1 for r in rows if r["near"]),
            "art": monart.sizes(),
            "seeded": seeded, "seed_left": left, "can_seed": not locs,
            "seed_radius_m": seed_r,
            "ts": int(time.time() * 1000)}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj))

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/webassets/"):
            import webassets
            code, hdrs, body = webassets.handle(p)
            self.send_response(code)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if p == "/":
            return self._send(200, "text/html; charset=utf-8",
                              webui.render(PAGE)
                                  .replace("__DEX__", json.dumps(DEX))
                                  .replace("__GIVEABLE__", json.dumps(GIVEABLE)))

        if p == "/research":
            import research_ui
            return self._send(200, "text/html; charset=utf-8", research_ui.page())
        if p == "/soundpacks":
            import soundpacks_ui
            return self._send(200, "text/html; charset=utf-8", soundpacks_ui.page())
        if p == "/api/sp/list":
            import soundpacks as SP
            return self._json({"packs": SP.packs()})
        if p.startswith("/api/sp/file/"):
            import soundpacks as SP, urllib.parse as _up
            pack, _, fname = _up.unquote(p[len("/api/sp/file/"):]).partition("/")
            f = SP.file_path(pack, fname)
            if not f:
                return self._send(404, "text/plain", "not found")
            with open(f, "rb") as fh:
                return self._send(200, SP.content_type(f), fh.read())
        if p == "/downloads":
            import downloads_ui
            return self._send(200, "text/html; charset=utf-8", downloads_ui.world())
        if p == "/downloads/us":
            import downloads_ui
            return self._send(200, "text/html; charset=utf-8", downloads_ui.us())
        if p == "/api/downloads/status":
            import poidownload
            return self._json(poidownload.status())
        if p.startswith("/monart/"):
            # Sprite for one species, by dex number: /monart/25.png
            import monart          # os is imported at module scope; importing
                                   # os.path here made `os` a LOCAL for all of
                                   # do_GET, so any earlier use raised
                                   # UnboundLocalError
            code, hdrs, body = monart.serve(
                os.path.splitext(os.path.basename(p))[0])
            return self._send(code, hdrs["Content-Type"], body)

        if p == "/api/radar":
            import urllib.parse as _up
            q = dict(_up.parse_qsl(self.path.partition("?")[2]))
            try:
                rng = max(50.0, min(5000.0, float(q.get("range") or 1000)))
            except (TypeError, ValueError):
                rng = 1000.0
            try:
                at = (float(q["lat"]), float(q["lng"]))
            except (KeyError, TypeError, ValueError):
                at = None
            return self._json(radar(rng, at))

        if p == "/api/world":
            try:
                import rpc
                lat, lng = rpc._last_loc[0], rpc._last_loc[1]
            except Exception:
                lat, lng = 0.0, 0.0
            import world, settings as CFG
            return self._json({"places": PL.get(), "config": EV.get(),
                               "presets": list(EV.PRESETS),
                               "storage": world.storage(),
                               "prices": {
                                   "pokemon_step": CFG.get("storage", "pokemon_upgrade_step"),
                                   "pokemon_cost": CFG.get("storage", "pokemon_upgrade_cost"),
                                   "items_step": CFG.get("storage", "items_upgrade_step"),
                                   "items_cost": CFG.get("storage", "items_upgrade_cost")},
                               "player": {"lat": lat, "lng": lng}})
        self._send(404, "text/plain", "not found")

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if p == "/api/sp/upload":
            # a raw audio file (not JSON): /api/sp/upload?pack=..&clip=..&name=file.mp3
            import soundpacks as SP, urllib.parse as _up
            q = dict(_up.parse_qsl(self.path.partition("?")[2]))
            ok, msg = SP.put_clip(q.get("pack"), q.get("clip"), q.get("name"), raw)
            return self._json({"ok": ok, "message": msg})
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            d = {}
        try:
            if p.startswith("/api/research"):
                import research_ui
                out = research_ui.api(p, d)
                if out is not None:
                    return self._json(out)
            if p in ("/api/sp/create", "/api/sp/delete", "/api/sp/rename", "/api/sp/remove"):
                import soundpacks as SP
                if p == "/api/sp/create":
                    ok, msg = SP.create(d.get("pack"))
                    return self._json({"ok": ok, "message": msg, "name": SP._safe_pack(d.get("pack"))})
                if p == "/api/sp/delete":
                    ok, msg = SP.delete(d.get("pack"))
                    return self._json({"ok": ok, "message": msg})
                if p == "/api/sp/rename":
                    ok, msg = SP.rename(d.get("pack"), d.get("name"))
                    return self._json({"ok": ok, "message": msg, "name": SP._safe_pack(d.get("name"))})
                ok, msg = SP.remove_clip(d.get("pack"), d.get("clip"))
                return self._json({"ok": ok, "message": msg})
            if p == "/downloads/start":
                import poidownload
                region = (d.get("region") or "").strip()
                name = (d.get("name") or region).strip()
                started = poidownload.start(name, region) if region else False
                return self._json({"ok": bool(region), "started": started})
            if p == "/downloads/remove":
                import poidownload
                region = (d.get("region") or "").strip()
                name = (d.get("name") or region).strip()
                started = poidownload.remove(name, region) if region else False
                return self._json({"ok": bool(region), "started": started})
            if p == "/api/fort":
                return self._json(PL.add_fort(d.get("lat"), d.get("lng"),
                                              d.get("kind", "stop"), d.get("name", ""),
                                              d.get("image", "")))
            if p == "/api/spawn":
                return self._json(PL.add_spawn(d.get("lat"), d.get("lng"),
                                               d.get("pokemon_id", 1), d.get("name", "")))
            if p == "/api/remove":
                return self._json({"removed": PL.remove(d.get("id", ""))})
            if p == "/api/clear":
                return self._json(PL.clear(d.get("what", "all")))
            if p == "/api/give":
                import world
                import contextlib as _ctx
                who = (d.get("player") or "").strip()
                # Blank means "me": the trainer whose save changed last. The
                # World Manager's own thread has no player of its own, so this
                # used to fall through to a phantom "player" account.
                if not who:
                    who = world.last_active_account() or ""
                kind = d.get("kind", "item")
                # Blank means "whoever is playing right now", which keeps the old
                # one-click behaviour. A name is validated against existing saves
                # so a typo can't quietly create an empty account.
                try:
                    ctx = world.acting_as(who) if who else _ctx.nullcontext()
                except KeyError:
                    known = ", ".join(world.account_names()) or "none yet"
                    return self._json({"ok": False,
                                       "message": f"No trainer called {who!r}. "
                                                  f"Accounts on this server: {known}."})
                try:
                    with ctx:
                        target = who or world.current().username
                        if kind == "xp":
                            cnt = max(1, min(99_999_999, int(d.get("count", 1))))
                            total = world.add_xp(cnt)
                            msg = (f"gave {target} {cnt} XP (now {total}, "
                                   f"level {world.current().LEVEL})")
                        elif kind == "level":
                            # Set the level outright by moving XP to the floor of
                            # the requested level. Going DOWN is allowed on purpose
                            # -- it is the only way to retest a level gate.
                            lvl = max(1, min(500, int(d.get("level", 1))))
                            floor = world.xp_for_level(lvl)
                            p_ = world.current()
                            p_.XP = floor
                            p_.LEVEL = world.level_for_xp(floor)
                            p_.save()
                            msg = f"{target} is now level {p_.LEVEL} ({floor} XP)"
                        elif kind == "stardust":
                            cnt = max(1, min(999999, int(d.get("count", 1))))
                            total = world.add_stardust(cnt)
                            msg = f"gave {target} {cnt} stardust (now {total})"
                        elif kind == "candy":
                            import protocol as P
                            pid = int(d.get("pokemon_id", 0))
                            cnt = max(1, min(999, int(d.get("count", 1))))
                            if not 1 <= pid <= 151:
                                return self._json({"ok": False,
                                                   "message": "unknown Pokemon"})
                            fam = P.pokemon_family(pid)
                            total = world.add_candy(fam, cnt)
                            label = DEX[pid] if pid < len(DEX) else str(pid)
                            fam_label = DEX[fam] if fam < len(DEX) else str(fam)
                            msg = (f"gave {target} {cnt} {fam_label} candy "
                                   f"(now {total})")
                            if fam != pid:
                                msg += f" — {label}'s family"
                        else:
                            iid = int(d.get("item_id", 0))
                            cnt = max(1, min(999, int(d.get("count", 1))))
                            names = dict(GIVEABLE)
                            if iid not in names:
                                return self._json({"ok": False,
                                                   "message": "unknown item"})
                            total = world.add_item(iid, cnt)
                            msg = (f"gave {target} {cnt} x {names[iid]} "
                                   f"(now {total})")
                except KeyError:
                    known = ", ".join(world.account_names()) or "none yet"
                    return self._json({"ok": False,
                                       "message": f"No trainer called {who!r}. "
                                                  f"Accounts on this server: {known}."})
                return self._json({"ok": True, "message": msg})
            if p == "/api/noms":
                import helpcenter
                return self._json({"rows": helpcenter.recent()})
            if p == "/api/noms/resolve":
                import helpcenter
                row = helpcenter.resolve(d.get("id", ""), d.get("status", "rejected"))
                if not row:
                    return self._json({"ok": False, "message": "unknown nomination"})
                # Places are added straight away now, so "resolve" only ever
                # means taking one back out again.
                for f in list(PL.get()["forts"]):
                    if (abs(f["lat"] - row["lat"]) < 1e-6
                            and abs(f["lng"] - row["lng"]) < 1e-6):
                        PL.remove(f["id"])
                return self._json({"ok": True,
                                   "message": f"removed {row['name']!r}"})
            if p == "/api/setpw":
                import world
                who = (d.get("player") or "").strip()
                # Blank means "me": the trainer whose save changed last. The
                # World Manager's own thread has no player of its own, so this
                # used to fall through to a phantom "player" account.
                if not who:
                    who = world.last_active_account() or ""
                pw = d.get("password") or ""
                if not pw:
                    return self._json({"ok": False, "message": "pick a password"})
                if world.set_password(who, pw):
                    return self._json({"ok": True,
                                       "message": f"{who}'s password has been reset"})
                return self._json({"ok": False,
                                   "message": f"no account called {who!r}"})
            if p == "/api/makestop":
                # "Spawn a PokeStop in my area" -- first one free, then it costs
                # coins. Drops a real, spinnable stop at the trainer's location
                # (their home/business), which is what a rural player with no
                # nearby OSM stops needs. Names it after the nearest OSM place if
                # there is one within ~250 m, else a plain Bracky stop.
                import world, contextlib as _ctx, rpc as _rpc, math as _math
                who = (d.get("player") or "").strip()
                # Blank means "me": the trainer whose save changed last. The
                # World Manager's own thread has no player of its own, so this
                # used to fall through to a phantom "player" account.
                if not who:
                    who = world.last_active_account() or ""
                cost = int(d.get("cost", 1000))
                try:
                    lat = float(d.get("lat")) if d.get("lat") is not None else _rpc._last_loc[0]
                    lng = float(d.get("lng")) if d.get("lng") is not None else _rpc._last_loc[1]
                except (TypeError, ValueError):
                    lat, lng = _rpc._last_loc[0], _rpc._last_loc[1]
                if abs(lat) < 1e-6 and abs(lng) < 1e-6:
                    return self._json({"ok": False, "message":
                                       "no player location yet -- open the game so it "
                                       "reports where you are, then try again"})
                try:
                    ctx = world.acting_as(who) if who else _ctx.nullcontext()
                    ctx.__enter__()
                except KeyError:
                    known = ", ".join(world.account_names()) or "none yet"
                    return self._json({"ok": False,
                                       "message": f"No trainer called {who!r}. "
                                                  f"Accounts on this server: {known}."})
                try:
                    target = who or world.current().username
                    ok, charged, reason = world.buy_stop(cost)
                    if not ok:
                        return self._json({"ok": False, "message": reason,
                                           "coins": world.COINS})
                    # Name it after the closest OSM place within 250 m, if any.
                    name = (d.get("name") or "").strip()
                    if not name:
                        try:
                            import pois
                            best, bestd = None, 250.0
                            for f in pois.near(lat, lng):
                                dy = (f["lat"] - lat) * 111320.0
                                dx = (f["lng"] - lng) * 111320.0 * max(0.2, _math.cos(_math.radians(lat)))
                                dm = _math.hypot(dx, dy)
                                if dm < bestd:
                                    best, bestd = f["name"], dm
                            name = best or "Bracky Stop"
                        except Exception:
                            name = "Bracky Stop"
                    fort = PL.add_fort(lat, lng, "stop", name[:40])
                    price = "free" if charged == 0 else f"{charged}c"
                    return self._json({"ok": True, "coins": world.COINS,
                                       "message": f"placed {name!r} at your location "
                                                  f"({price}) for {target}", "fort": fort})
                finally:
                    ctx.__exit__(None, None, None)
            if p == "/api/raid":
                import world
                if not d:                      # plain read
                    return self._json(world.raid())
                cfg, sent = world.set_raid(d.get("on"), d.get("pokemon_id"),
                                           d.get("cp"), d.get("trainer"))
                who = DEX[cfg["pokemon_id"]] if cfg["pokemon_id"] < len(DEX) else "?"
                msg = (f"Raid ON — {who} CP{cfg['cp']} is now defending every gym"
                       + (f"; {sent} defender(s) sent home" if sent else "")
                       if cfg["on"] else
                       "Raid off — gyms are back to normal and empty")
                return self._json(dict(cfg, message=msg))
            if p == "/api/loot":
                import settings as CFG, protocol as P
                if "loot" in d:                # save
                    loot = {}
                    for row in d.get("loot") or []:
                        name = str(row.get("item", "")).strip().lower()
                        if name not in P.LOOT_ITEM_IDS or name in loot:
                            continue
                        lo = max(0, min(999, int(row.get("min", 1))))
                        loot[name] = {
                            "chance": max(0.0, min(1.0, float(row.get("chance", 1)))),
                            "min": lo,
                            "max": max(lo, min(999, int(row.get("max", lo))))}
                    lo = max(0, min(999, int(d.get("min_items", 0))))
                    CFG.set_values("pokestops", {
                        "loot": loot, "min_items_per_spin": lo,
                        "loot_mode": "weighted" if d.get("mode") == "weighted" else "chance",
                        "max_items_per_spin": max(lo, min(999, int(d.get("max_items", lo))))})
                return self._json({
                    "loot": CFG.get("pokestops", "loot"),
                    "mode": CFG.get("pokestops", "loot_mode"),
                    "min_items": CFG.get("pokestops", "min_items_per_spin"),
                    "max_items": CFG.get("pokestops", "max_items_per_spin"),
                    "items": list(P.LOOT_ITEM_IDS)})
            if p == "/api/accounts":
                import world
                return self._json({"accounts": world.account_names()})
            if p == "/api/procedural":
                return self._json(PL.set_procedural(d.get("on", True), d.get("what", "both")))
            if p == "/api/save":
                return self._json(EV.save(d))
            if p == "/api/schedule":
                if d.get("rows") is not None:
                    EV.set_schedule(d.get("rows"))
                row = EV.active_scheduled()
                return self._json({"rows": EV.schedule(),
                                   "active": (row or {}).get("name", "")})
            if p == "/api/preset":
                m = EV.apply_preset(d.get("name", ""))
                return self._json(m if m else {"error": "unknown preset"},
                                  200 if m else 400)
            if p == "/api/ring":
                # furnish a whole neighbourhood in one click: a ring of PokeStops
                # (plus an optional Gym) around a centre point
                import math
                lat = float(d.get("lat", 0.0)); lng = float(d.get("lng", 0.0))
                n = max(1, min(24, int(d.get("count", 8))))
                r_m = max(10.0, min(500.0, float(d.get("radius_m", 60))))
                gym = bool(d.get("gym", True))
                made = []
                for i in range(n):
                    a = 2 * math.pi * i / n
                    dlat = (r_m * math.cos(a)) / 111320.0
                    dlng = (r_m * math.sin(a)) / (111320.0 * max(0.2, math.cos(math.radians(lat))))
                    made.append(PL.add_fort(lat + dlat, lng + dlng, "stop", f"Ring Stop {i+1}"))
                if gym:
                    made.append(PL.add_fort(lat, lng, "gym", "Home Gym"))
                return self._json({"placed": len(made)})
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        self._send(404, "text/plain", "not found")


def serve(port=8080, host="127.0.0.1"):
    ThreadingHTTPServer((host, port), _Handler).serve_forever()


def start(port=8080, host="127.0.0.1"):
    t = threading.Thread(target=serve, kwargs={"port": port, "host": host}, daemon=True)
    t.start()
    return t
