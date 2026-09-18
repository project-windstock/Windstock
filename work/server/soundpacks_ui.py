"""
The World Manager's Sound Packs page: build packs that replace the game's music and
sounds, one clip at a time, then pull them onto the phone from the tweak's sound pack
menu ("Download packs from server"). Backend: soundpacks.py. Same Bracky dashboard skin
as the other World Manager pages (webui.CSS via __CSS__).
"""
import json

import soundpacks as SP
import webui

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sound Packs</title>
<style>__CSS__
 .sp-top{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:6px 0 14px}
 .sp-packs{display:flex;gap:8px;flex-wrap:wrap}
 .sp-pack{padding:9px 14px;border-radius:999px;border:1px solid var(--line);background:var(--card);
   cursor:pointer;font-weight:700;color:var(--ink2)}
 .sp-pack.on{background:var(--brand,#2358d8);color:#fff;border-color:transparent}
 .sp-pack small{font-weight:600;opacity:.75;margin-left:6px}
 .sp-group{margin:18px 0 8px;display:flex;align-items:baseline;gap:10px}
 .sp-group h3{margin:0;font-size:15px}
 .sp-group .ct{font-size:12.5px;color:var(--ink3)}
 .sp-group button{margin-left:auto}
 .sp-rows{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:var(--card)}
 .sp-row{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) auto;gap:10px;
   align-items:center;padding:9px 14px;border-top:1px solid var(--line)}
 .sp-row:first-child{border-top:0}
 .sp-row .nm{font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .sp-row .clip{font-family:var(--mono);font-size:11.5px;color:var(--ink3)}
 .sp-row .st{font-size:12.5px;color:var(--ink3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .sp-row .st.have{color:#1c8a5a;font-weight:700}
 .sp-row .act{display:flex;gap:6px;align-items:center}
 .sp-row audio{height:30px;width:170px}
 .sp-row.drag{background:var(--soft)}
 .sp-empty{padding:40px;text-align:center;color:var(--ink3)}
 .sp-filter{min-width:220px}
 @media(max-width:700px){.sp-row{grid-template-columns:1fr}.sp-row audio{width:100%}}
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
  <nav class="topnav"><a href="/">Manager</a><a href="/downloads">World Data</a><a class="active" href="/soundpacks">Sound Packs</a><a href="/research">Research</a></nav>
  <div class="spacer"></div>
  <div class="meta">Packs <b id="npacks">&hellip;</b></div>
</header>
<main class="wrap">
<h2>Sound Packs</h2>
<div class="hint">Replace the game's music and sounds. Pick a pack (or make one), then upload a
file next to any sound &mdash; or drop a file straight onto its row. On the phone, open
<b>Settings &rarr; About</b> and tap <b>Download packs from server</b>, then choose the pack.</div>

<div class="sp-top">
  <input id="newname" placeholder="New pack name" size="18">
  <button class="on" onclick="createPack()">Create pack</button>
  <span class="spacer"></span>
  <input id="filter" class="sp-filter" placeholder="Search sounds (e.g. ball, walk, Pikachu)" oninput="paint()">
</div>
<div class="sp-packs" id="packs"></div>
<div class="bar" id="packbar" style="display:none;margin-top:10px">
  <b id="packtitle"></b><span class="inline-label" id="packinfo"></span>
  <span class="spacer"></span>
  <button onclick="renamePack()">Rename</button>
  <button class="danger" onclick="deletePack()">Delete pack</button>
</div>
<div id="msg" class="hint"></div>
<div id="groups"></div>
</main>
<script>
const CAT=__CATALOGUE__;
const $=s=>document.querySelector(s);
let PACKS=[], CUR=localStorage.getItem('sp-pack')||'';
const OPEN=JSON.parse(localStorage.getItem('sp-open')||'{"music":true,"catch":true}');
function say(t,bad){const m=$('#msg');m.textContent=t||'';m.style.color=bad?'#c0392b':'';}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function kb(n){return n>1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.round(n/1024))+' KB';}
async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  return r.json();
}
async function load(){
  const r=await fetch('/api/sp/list').then(r=>r.json());
  PACKS=r.packs||[];
  if(!PACKS.find(p=>p.name===CUR)) CUR=PACKS.length?PACKS[0].name:'';
  paint();
}
function cur(){return PACKS.find(p=>p.name===CUR);}
function paint(){
  $('#npacks').textContent=PACKS.length;
  $('#packs').innerHTML=PACKS.map(p=>'<div class="sp-pack'+(p.name===CUR?' on':'')+'" data-n="'+esc(p.name)+'">'+
    esc(p.name)+'<small>'+Object.keys(p.clips).length+'</small></div>').join('')||
    '<span class="hint">No packs yet &mdash; name one above and press Create pack.</span>';
  document.querySelectorAll('.sp-pack').forEach(el=>el.onclick=()=>{CUR=el.dataset.n;
    localStorage.setItem('sp-pack',CUR);paint();});
  const p=cur();
  $('#packbar').style.display=p?'':'none';
  if(!p){$('#groups').innerHTML='';return;}
  $('#packtitle').textContent=p.name;
  $('#packinfo').textContent=Object.keys(p.clips).length+' sound(s) replaced · '+kb(p.bytes);
  const q=$('#filter').value.trim().toLowerCase();
  let html='';
  CAT.groups.forEach(g=>{
    const rows=g.clips.filter(c=>!q||c.label.toLowerCase().includes(q)||c.clip.toLowerCase().includes(q));
    if(!rows.length)return;
    const have=g.clips.filter(c=>p.clips[c.clip]).length;
    const open=q||OPEN[g.id];
    html+='<div class="sp-group"><h3>'+esc(g.name)+'</h3><span class="ct">'+have+' of '+g.clips.length+
      ' replaced</span><button onclick="tog(\''+g.id+'\')">'+(open?'Hide':'Show')+'</button></div>';
    if(!open)return;
    html+='<div class="sp-rows">'+rows.map(c=>{
      const f=p.clips[c.clip];
      const url='/api/sp/file/'+encodeURIComponent(p.name)+'/'+encodeURIComponent(f?f.file:'')+'?v='+(f?f.mtime:0);
      return '<div class="sp-row" data-clip="'+c.clip+'"><div><div class="nm">'+esc(c.label)+
        '</div><div class="clip">'+c.clip+'</div></div>'+
        (f?'<audio controls preload="none" src="'+url+'"></audio>':
           '<div class="st">Game default</div>')+
        '<div class="act"><button'+(f?'':' class="on"')+' onclick="this.nextElementSibling.click()">'+(f?'Replace':'Upload')+'</button>'+
        '<input type="file" accept="audio/*,.mp3,.m4a,.wav,.caf,.aac,.aif,.aiff" hidden onchange="up(this,\''+c.clip+'\')">'+
        (f?'<button class="danger" onclick="rmClip(\''+c.clip+'\')">Remove</button>':'')+'</div></div>';
    }).join('')+'</div>';
  });
  $('#groups').innerHTML=html||'<div class="sp-empty">No sounds match that search.</div>';
  document.querySelectorAll('.sp-row').forEach(row=>{
    row.ondragover=e=>{e.preventDefault();row.classList.add('drag');};
    row.ondragleave=()=>row.classList.remove('drag');
    row.ondrop=e=>{e.preventDefault();row.classList.remove('drag');
      if(e.dataTransfer.files[0]) send(row.dataset.clip,e.dataTransfer.files[0]);};
  });
}
function tog(id){OPEN[id]=!OPEN[id];localStorage.setItem('sp-open',JSON.stringify(OPEN));paint();}
function up(input,clip){if(input.files[0]) send(clip,input.files[0]);}
async function send(clip,file){
  say('Uploading '+file.name+'…');
  const r=await fetch('/api/sp/upload?pack='+encodeURIComponent(CUR)+'&clip='+encodeURIComponent(clip)+
    '&name='+encodeURIComponent(file.name),{method:'POST',body:file}).then(r=>r.json());
  say(r.message,!r.ok); load();
}
async function rmClip(clip){const r=await api('/api/sp/remove',{pack:CUR,clip});say(r.message,!r.ok);load();}
async function createPack(){
  const n=$('#newname').value.trim(); const r=await api('/api/sp/create',{pack:n});
  say(r.message,!r.ok); if(r.ok){CUR=r.name;localStorage.setItem('sp-pack',CUR);$('#newname').value='';} load();
}
async function renamePack(){
  const n=prompt('New name for '+CUR+':',CUR); if(!n||n===CUR)return;
  const r=await api('/api/sp/rename',{pack:CUR,name:n}); say(r.message,!r.ok);
  if(r.ok){CUR=r.name;localStorage.setItem('sp-pack',CUR);} load();
}
async function deletePack(){
  if(!confirm('Delete the pack "'+CUR+'" and all its sounds?'))return;
  const r=await api('/api/sp/delete',{pack:CUR}); say(r.message,!r.ok); load();
}
load();
</script>
</body></html>"""


def page():
    return webui.render(PAGE).replace("__CATALOGUE__", json.dumps(SP.catalogue()))
