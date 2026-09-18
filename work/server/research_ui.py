"""
The World Manager's Research page -- the research giver. Hand Field Research tasks to
trainers, edit the daily task pool and the Research Breakthrough, and write Special and
Timed Research storylines (steps -> tasks -> rewards). Backend: research.py. On the
phone it all shows behind the binoculars button above Nearby.
"""
import json

import webui

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research</title>
<style>__CSS__
 .rs-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 16px}
 .rs-tab{padding:9px 16px;border-radius:999px;border:1px solid var(--line);background:var(--card);
   cursor:pointer;font-weight:700;color:var(--ink2)}
 .rs-tab.on{background:var(--brand);color:#fff;border-color:transparent}
 .rs-card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
   margin:0 0 12px;box-shadow:var(--shadow)}
 .rs-card h3{margin:0 0 8px;font-size:15px}
 .rs-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
 .rs-row label{font-size:12px;font-weight:700;color:var(--ink3);text-transform:uppercase;letter-spacing:.06em}
 .rs-task{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;
   padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--soft);margin:6px 0}
 .rs-task .txt{font-weight:700}
 .rs-task .sub{font-size:12.5px;color:var(--ink3)}
 .rs-ed{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
 .rs-ed select,.rs-ed input{padding:7px 9px;font-size:13px}
 .rs-ed input[type=number]{width:74px}
 .rs-step{border:1px dashed #cdd7ea;border-radius:12px;padding:10px 12px;margin:10px 0;background:#fbfcff}
 .rs-step h4{margin:0 0 6px;font-size:13.5px;display:flex;gap:8px;align-items:center}
 .rs-step h4 .spacer{flex:1}
 .rs-mini{font-size:12px;padding:5px 10px}
 .rs-prog{height:8px;border-radius:99px;background:#e7ecf4;overflow:hidden;width:120px;display:inline-block;vertical-align:middle}
 .rs-prog i{display:block;height:100%;background:linear-gradient(90deg,#ffbe5c,#ff9434)}
 .rs-tr{display:grid;grid-template-columns:160px minmax(0,1fr) auto;gap:12px;padding:10px 0;border-top:1px solid var(--line)}
 .rs-tr:first-child{border-top:0}
 .rs-tr .who{font-weight:800}
 .rs-tr .ln{font-size:13px;color:var(--ink2)}
 .rs-preview{font-size:13px;color:var(--teal-dk);font-weight:700}
 .rs-hide{display:none}
 textarea{width:100%;min-height:54px;font:inherit;padding:8px;border-radius:10px;border:1px solid var(--line)}
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
  <nav class="topnav"><a href="/">Manager</a><a href="/downloads">World Data</a><a href="/soundpacks">Sound Packs</a><a class="active" href="/research">Research</a></nav>
  <div class="spacer"></div>
  <div class="meta">Trainers <b id="ntr">&hellip;</b></div>
</header>
<main class="wrap">
<h2>Research</h2>
<div class="hint">Hand out research and write storylines. On the phone, tap the <b>binoculars</b>
above Nearby: <b>Today</b> holds the day's Field Research and the Research Breakthrough,
<b>Special</b> holds storylines, and a <b>Timed</b> tab appears while timed research is running.
Progress counts real catches, throws, spins, hatches, evolutions, transfers, power-ups,
berries, gym battles and walking.</div>
<div class="rs-tabs" id="tabs"></div>
<div id="msg" class="hint"></div>
<div id="view"></div>
</main>
<script>
const DEX=__DEX__;
const $=s=>document.querySelector(s);
let D=null, TAB=localStorage.getItem('rs-tab')||'give', CFG=null, DIRTY=false;
const TABS=[['give','Give research'],['trainers','Trainers'],['field','Field Research pool'],
  ['special','Special Research'],['timed','Timed Research']];
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function say(t,bad){const m=$('#msg');m.textContent=t||'';m.style.color=bad?'#c0392b':'#127a45';}
async function api(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body||{})});return r.json();}
async function load(){D=await api('/api/research');CFG=JSON.parse(JSON.stringify(D.config));DIRTY=false;
  $('#ntr').textContent=D.trainers.length;paint();}
function tabs(){$('#tabs').innerHTML=TABS.map(([k,l])=>'<div class="rs-tab'+(k===TAB?' on':'')+'" data-k="'+k+'">'+l+'</div>').join('');
  document.querySelectorAll('.rs-tab').forEach(e=>e.onclick=()=>{if(DIRTY&&!confirm('Discard unsaved changes?'))return;
    if(DIRTY){CFG=JSON.parse(JSON.stringify(D.config));DIRTY=false;}
    TAB=e.dataset.k;localStorage.setItem('rs-tab',TAB);paint();});}
function dirty(){DIRTY=true;const b=$('#savebtn');if(b)b.classList.add('on');}

// ---------- wording (mirrors research.task_text)
function name(i){return DEX[i]||('#'+i);}
function taskText(t){if(t.text)return t.text;const n=Math.max(1,+t.count||1),sp=+t.species||0,k=t.type;
  const a=n===1;
  if(k==='catch')return 'Catch '+(a?'a':n)+' '+(sp?name(sp):(t.ptype?t.ptype+'-type Pokémon':'Pokémon'));
  if(k==='throw'){const q=t.throw||'Nice';return a?'Make '+(q==='Excellent'?'an':'a')+' '+q+' Throw':'Make '+n+' '+q+' Throws';}
  if(k==='curveball')return a?'Make a Curveball Throw':'Make '+n+' Curveball Throws';
  if(k==='spin')return a?'Spin a PokéStop':'Spin '+n+' PokéStops';
  if(k==='hatch')return a?'Hatch an Egg':'Hatch '+n+' Eggs';
  if(k==='evolve')return 'Evolve '+(a?'a':n)+' '+(sp?name(sp):'Pokémon');
  if(k==='transfer')return a?'Transfer a Pokémon':'Transfer '+n+' Pokémon';
  if(k==='power_up')return a?'Power up a Pokémon':'Power up Pokémon '+n+' times';
  if(k==='berry')return a?'Use a Razz Berry to help catch a Pokémon':'Use '+n+' Razz Berries to help catch Pokémon';
  if(k==='battle')return a?'Battle in a Gym':'Battle in a Gym '+n+' times';
  if(k==='walk')return 'Walk '+n+' km';return k;}
function rewardText(r){if(!r)return '';const n=+r.count||1;
  if(r.kind==='item')return n+'× '+(D.items[r.item]||('item '+r.item));
  if(r.kind==='stardust')return n+' Stardust';if(r.kind==='xp')return n+' XP';
  if(r.kind==='candy')return n+'× '+name(r.species)+' Candy';
  if(r.kind==='encounter')return (r.species||[]).map(name).join(' / ')+' encounter (CP '+(r.cp||[300,900]).join('–')+')';
  return r.kind;}

// ---------- editors
function opt(v,l,sel){return '<option value="'+esc(v)+'"'+(String(v)===String(sel)?' selected':'')+'>'+esc(l)+'</option>';}
function dexOpts(sel,none){let h=none?opt(0,none,sel):'';for(let i=1;i<DEX.length;i++)h+=opt(i,i+' '+DEX[i],sel);return h;}
function taskEditor(t,onChange){
  const el=document.createElement('div');el.className='rs-ed';
  const draw=()=>{
    let h='<select data-f="type">'+Object.entries(D.task_types).map(([k,l])=>opt(k,l,t.type)).join('')+'</select>'+
      '<input type="number" min="1" data-f="count" value="'+esc(t.count||1)+'" title="How many">';
    if(t.type==='catch'||t.type==='evolve')h+='<select data-f="species">'+dexOpts(t.species||0,'Any Pokémon')+'</select>';
    if(t.type==='catch')h+='<select data-f="ptype">'+opt('','Any type',t.ptype||'')+D.types.map(x=>opt(x,x+' type',t.ptype||'')).join('')+'</select>';
    if(t.type==='throw')h+='<select data-f="throw">'+['Nice','Great','Excellent'].map(x=>opt(x,x,t.throw||'Nice')).join('')+'</select>';
    h+='<input data-f="text" placeholder="Custom wording (optional)" value="'+esc(t.text||'')+'" size="22">';
    h+='<span class="rs-preview">'+esc(taskText(t))+'</span>';
    el.innerHTML=h;
    el.querySelectorAll('[data-f]').forEach(inp=>inp.onchange=inp.oninput=()=>{
      const f=inp.dataset.f;let v=inp.value;
      if(f==='count'||f==='species')v=+v||0;
      if(v===''||v===0&&f==='species'){delete t[f];}else t[f]=v;
      if(f==='type'){delete t.species;delete t.ptype;delete t.throw;draw();}
      else el.querySelector('.rs-preview').textContent=taskText(t);
      onChange&&onChange();});
  };draw();return el;}
function rewardEditor(r,onChange){
  const el=document.createElement('div');el.className='rs-ed';
  const draw=()=>{
    let h='<select data-f="kind">'+[['item','Item'],['stardust','Stardust'],['xp','XP'],['candy','Candy'],['encounter','Pokémon encounter']]
      .map(([k,l])=>opt(k,l,r.kind)).join('')+'</select>';
    if(r.kind==='item')h+='<select data-f="item">'+Object.entries(D.items).map(([k,l])=>opt(k,l,r.item||1)).join('')+'</select>';
    if(r.kind==='candy')h+='<select data-f="species">'+dexOpts(r.species||1)+'</select>';
    if(r.kind!=='encounter')h+='<input type="number" min="1" data-f="count" value="'+esc(r.count||1)+'">';
    else{
      h+='<input data-f="species" size="16" title="Pokédex numbers, comma separated -- one is picked at random" value="'+esc((r.species||[]).join(', '))+'" placeholder="e.g. 25 or 1, 4, 7">'+
        '<label>CP</label><input type="number" data-f="cp0" value="'+esc((r.cp||[300,900])[0])+'"><input type="number" data-f="cp1" value="'+esc((r.cp||[300,900])[1])+'">';
    }
    h+='<span class="rs-preview">'+esc(rewardText(r))+'</span>';
    el.innerHTML=h;
    el.querySelectorAll('[data-f]').forEach(inp=>inp.onchange=()=>{
      const f=inp.dataset.f,v=inp.value;
      if(f==='kind'){for(const k of Object.keys(r))delete r[k];r.kind=v;
        if(v==='item'){r.item=1;r.count=5;}else if(v==='encounter'){r.species=[25];r.cp=[300,900];}
        else if(v==='candy'){r.species=1;r.count=3;}else r.count=v==='xp'?1000:500;draw();}
      else if(f==='species'&&r.kind==='encounter')r.species=v.split(/[^0-9]+/).map(Number).filter(x=>x>0&&x<DEX.length);
      else if(f==='cp0'||f==='cp1'){r.cp=r.cp||[300,900];r.cp[f==='cp0'?0:1]=+v||0;}
      else r[f]=+v||0;
      el.querySelector('.rs-preview').textContent=rewardText(r);onChange&&onChange();});
  };draw();return el;}
function btn(label,cls,fn){const b=document.createElement('button');b.textContent=label;if(cls)b.className=cls;b.onclick=fn;return b;}
function card(title){const c=document.createElement('div');c.className='rs-card';if(title)c.innerHTML='<h3>'+esc(title)+'</h3>';return c;}
function row(){const r=document.createElement('div');r.className='rs-row';return r;}
function trainerSelect(multi){const s=document.createElement('select');
  s.innerHTML=opt('*','Every trainer','*')+D.trainers.map(t=>opt(t.username,t.username,'')).join('');return s;}
async function saveCfg(){const r=await api('/api/research/save',{config:CFG});if(r.ok){say('Saved. Phones pick it up the next time Research is opened.');await load();}else say(r.message||'Save failed',true);}
function saveBar(){const r=row();const b=btn('Save changes','',saveCfg);b.id='savebtn';r.appendChild(b);
  r.appendChild(btn('Discard','',()=>{CFG=JSON.parse(JSON.stringify(D.config));DIRTY=false;paint();}));return r;}

// ---------- tabs
function paint(){tabs();const v=$('#view');v.innerHTML='';({give:pGive,trainers:pTrainers,field:pField,special:()=>pStories('special'),timed:()=>pStories('timed')})[TAB](v);}

function pGive(v){
  const c=card('Give a Field Research task');
  c.insertAdjacentHTML('beforeend','<div class="hint">It shows at the top of the trainer\'s <b>Today</b> tab straight away and stays until they claim it.</div>');
  const t={type:'catch',count:5,reward:{kind:'item',item:1,count:10}};
  let r1=row();r1.innerHTML='<label>Task</label>';r1.appendChild(taskEditor(t));c.appendChild(r1);
  let r2=row();r2.innerHTML='<label>Reward</label>';r2.appendChild(rewardEditor(t.reward));c.appendChild(r2);
  let r3=row();r3.innerHTML='<label>To</label>';const who=trainerSelect();r3.appendChild(who);
  r3.appendChild(btn('Give task','on',async()=>{const r=await api('/api/research/give',{to:who.value,task:t});
    say(r.ok?'Given to '+r.count+' trainer(s).':(r.message||'Failed'),!r.ok);load();}));
  r3.appendChild(btn('Add to the daily pool instead','',async()=>{CFG.field.pool.push(JSON.parse(JSON.stringify(t)));await saveCfg();}));
  c.appendChild(r3);v.appendChild(c);

  const s=card('Start a storyline for a trainer');
  const all=[...CFG.special.map(x=>['special',x]),...CFG.timed.map(x=>['timed',x])];
  if(!all.length){s.insertAdjacentHTML('beforeend','<div class="hint">No storylines yet &mdash; write one on the Special or Timed Research tab.</div>');}
  else{const r=row();const sel=document.createElement('select');sel.innerHTML=all.map(([g,x],i)=>opt(i,(g==='timed'?'[Timed] ':'')+x.title,0)).join('');
    const who2=trainerSelect();r.append(sel,who2,btn('Give storyline','on',async()=>{const [g,x]=all[+sel.value];
      const res=await api('/api/research/assign',{group:g,id:x.id,to:who2.value});say(res.ok?'Storyline given.':(res.message||'Failed'),!res.ok);load();}));
    s.appendChild(r);s.insertAdjacentHTML('beforeend','<div class="hint">A storyline with no trainers chosen is for everyone.</div>');}
  v.appendChild(s);
}

function pTrainers(v){
  const c=card('Trainers');
  if(!D.trainers.length)c.insertAdjacentHTML('beforeend','<div class="hint">No trainers yet.</div>');
  D.trainers.forEach(tr=>{const r=document.createElement('div');r.className='rs-tr';
    const today=tr.today.map(t=>'<div class="ln">'+(t.claimed?'✓ ':'')+esc(t.text)+' <span class="rs-prog"><i style="width:'+(100*t.progress/t.count)+'%"></i></span> '+t.progress+'/'+t.count+'</div>').join('')||'<div class="ln">No tasks today</div>';
    const st=tr.stories.map(s=>'<div class="ln"><b>'+esc(s.title)+'</b> &mdash; '+(s.done?'complete':'step '+s.step+' of '+s.steps)+'</div>').join('');
    r.innerHTML='<div class="who">'+esc(tr.username)+'<div class="ln">Stamps '+tr.stamps+' / '+CFG.breakthrough.stamps+'</div></div><div>'+today+st+'</div>';
    const a=document.createElement('div');a.style.cssText='display:flex;flex-direction:column;gap:6px';
    a.appendChild(btn('Fill stamps','rs-mini',async()=>{await api('/api/research/stamps',{user:tr.username,n:CFG.breakthrough.stamps});say('Stamps filled for '+tr.username);load();}));
    a.appendChild(btn('New tasks today','rs-mini',async()=>{await api('/api/research/reset',{user:tr.username,what:'today'});say('Today\'s tasks re-rolled for '+tr.username);load();}));
    a.appendChild(btn('Reset all research','rs-mini danger',async()=>{if(!confirm('Reset all research progress for '+tr.username+'?'))return;
      await api('/api/research/reset',{user:tr.username,what:'all'});say('Research reset for '+tr.username);load();}));
    r.appendChild(a);c.appendChild(r);});
  v.appendChild(c);
}

function pField(v){
  const f=CFG.field;
  const c=card('Daily Field Research');
  const r=row();r.innerHTML='<label><input type="checkbox" '+(f.enabled?'checked':'')+' id="fen"> Hand out tasks every day</label>'+
    '<label style="margin-left:14px">Tasks per day</label><input type="number" min="1" max="10" id="fpd" value="'+(f.per_day||3)+'">';
  c.appendChild(r);
  c.insertAdjacentHTML('beforeend','<div class="hint">Each trainer gets this many tasks picked from the pool below every day. The first task claimed each day earns a stamp.</div>');
  v.appendChild(c);
  $('#fen').onchange=e=>{f.enabled=e.target.checked;dirty();};$('#fpd').oninput=e=>{f.per_day=+e.target.value||3;dirty();};
  const p=card('Task pool ('+f.pool.length+')');
  f.pool.forEach((t,i)=>{const w=document.createElement('div');w.className='rs-task';
    const left=document.createElement('div');left.appendChild(taskEditor(t,dirty));
    const rr=row();rr.innerHTML='<label>Reward</label>';rr.appendChild(rewardEditor(t.reward=t.reward||{kind:'item',item:1,count:5},dirty));left.appendChild(rr);
    w.appendChild(left);w.appendChild(btn('Remove','rs-mini danger',()=>{f.pool.splice(i,1);dirty();paint();}));p.appendChild(w);});
  p.appendChild(btn('+ Add task','',()=>{f.pool.push({type:'catch',count:5,reward:{kind:'item',item:1,count:5}});dirty();paint();}));
  v.appendChild(p);
  const b=CFG.breakthrough;const bc=card('Research Breakthrough');
  const br=row();br.innerHTML='<label><input type="checkbox" '+(b.enabled?'checked':'')+' id="ben"> On</label><label style="margin-left:14px">Stamps needed</label><input type="number" min="1" max="14" id="bst" value="'+(b.stamps||7)+'">';
  bc.appendChild(br);
  (b.rewards||[]).forEach((rw,i)=>{const x=row();x.appendChild(rewardEditor(rw,dirty));x.appendChild(btn('Remove','rs-mini danger',()=>{b.rewards.splice(i,1);dirty();paint();}));bc.appendChild(x);});
  bc.appendChild(btn('+ Add reward','',()=>{(b.rewards=b.rewards||[]).push({kind:'item',item:1,count:10});dirty();paint();}));
  v.appendChild(bc);
  $('#ben').onchange=e=>{b.enabled=e.target.checked;dirty();};$('#bst').oninput=e=>{b.stamps=+e.target.value||7;dirty();};
  v.appendChild(saveBar());
}

function toLocal(ms){if(!ms)return '';const d=new Date(ms-new Date().getTimezoneOffset()*60000);return d.toISOString().slice(0,16);}
function pStories(group){
  const v=$('#view');const list=CFG[group];
  const intro=card(group==='timed'?'Timed Research':'Special Research');
  intro.insertAdjacentHTML('beforeend','<div class="hint">'+(group==='timed'?
    'Timed Research gets its own tab on the phone while it runs, with a countdown, and disappears when it ends.':
    'Special Research storylines never expire. Trainers work through the steps in order: finish every task, claim each one, then claim the step\'s rewards.')+'</div>');
  intro.appendChild(btn('+ New storyline','on',()=>{const now=Date.now();
    const s={id:group+'-'+now,title:group==='timed'?'Timed Research':'New Special Research',description:'',trainers:[],
      steps:[{tasks:[{type:'catch',count:5,reward:{kind:'item',item:1,count:5}}],rewards:[{kind:'xp',count:500}]}]};
    if(group==='timed'){s.starts_ms=now;s.ends_ms=now+7*86400000;}
    list.push(s);dirty();paint();}));
  v.appendChild(intro);
  list.forEach((s,si)=>{
    const c=card('');
    const top=row();
    top.innerHTML='<input style="flex:1;font-weight:800;font-size:15px" value="'+esc(s.title)+'" data-k="title">';
    top.appendChild(btn('Delete storyline','rs-mini danger',()=>{if(confirm('Delete "'+s.title+'"?')){list.splice(si,1);dirty();paint();}}));
    c.appendChild(top);
    top.querySelector('[data-k=title]').oninput=e=>{s.title=e.target.value;dirty();};
    const ta=document.createElement('textarea');ta.placeholder='What Professor Willow says about it';ta.value=s.description||'';ta.oninput=()=>{s.description=ta.value;dirty();};c.appendChild(ta);
    const who=row();who.innerHTML='<label>Trainers</label><span class="hint" style="margin:0">'+(s.trainers.length?esc(s.trainers.join(', ')):'everyone')+'</span>';
    if(s.trainers.length)who.appendChild(btn('Make it for everyone','rs-mini',()=>{s.trainers=[];dirty();paint();}));
    c.appendChild(who);
    if(group==='timed'){const t=row();t.innerHTML='<label>Starts</label><input type="datetime-local" value="'+toLocal(s.starts_ms)+'" data-k="s"><label>Ends</label><input type="datetime-local" value="'+toLocal(s.ends_ms)+'" data-k="e">';
      t.querySelector('[data-k=s]').onchange=e=>{s.starts_ms=new Date(e.target.value).getTime()||0;dirty();};
      t.querySelector('[data-k=e]').onchange=e=>{s.ends_ms=new Date(e.target.value).getTime()||0;dirty();};c.appendChild(t);}
    s.steps.forEach((st,i)=>{const box=document.createElement('div');box.className='rs-step';
      box.innerHTML='<h4>Step '+(i+1)+' of '+s.steps.length+'<span class="spacer"></span></h4>';
      const h=box.querySelector('h4');
      if(i>0)h.appendChild(btn('↑','rs-mini',()=>{[s.steps[i-1],s.steps[i]]=[s.steps[i],s.steps[i-1]];dirty();paint();}));
      h.appendChild(btn('Remove step','rs-mini danger',()=>{s.steps.splice(i,1);dirty();paint();}));
      (st.tasks=st.tasks||[]).forEach((t,ti)=>{const w=document.createElement('div');w.className='rs-task';const l=document.createElement('div');
        l.appendChild(taskEditor(t,dirty));const rr=row();rr.innerHTML='<label>Reward</label>';rr.appendChild(rewardEditor(t.reward=t.reward||{kind:'item',item:1,count:5},dirty));l.appendChild(rr);
        w.appendChild(l);w.appendChild(btn('Remove','rs-mini danger',()=>{st.tasks.splice(ti,1);dirty();paint();}));box.appendChild(w);});
      if(st.tasks.length<3)box.appendChild(btn('+ Task','rs-mini',()=>{st.tasks.push({type:'spin',count:3,reward:{kind:'item',item:1,count:5}});dirty();paint();}));
      box.insertAdjacentHTML('beforeend','<div class="rs-row"><label>Step rewards</label></div>');
      (st.rewards=st.rewards||[]).forEach((rw,ri)=>{const x=row();x.appendChild(rewardEditor(rw,dirty));x.appendChild(btn('Remove','rs-mini danger',()=>{st.rewards.splice(ri,1);dirty();paint();}));box.appendChild(x);});
      if(st.rewards.length<3)box.appendChild(btn('+ Reward','rs-mini',()=>{st.rewards.push({kind:'stardust',count:500});dirty();paint();}));
      c.appendChild(box);});
    c.appendChild(btn('+ Add step','',()=>{s.steps.push({tasks:[{type:'catch',count:5,reward:{kind:'item',item:1,count:5}}],rewards:[{kind:'xp',count:500}]});dirty();paint();}));
    v.appendChild(c);
  });
  v.appendChild(saveBar());
}
window.onbeforeunload=()=>DIRTY?true:undefined;
load();
</script>
</body></html>"""


def page():
    import admin
    return webui.render(PAGE).replace("__DEX__", json.dumps(admin.DEX))


def api(path, d):
    """POST /api/research... -> JSON-able dict."""
    import research as R
    if path == "/api/research":
        return R.overview()
    if path == "/api/research/save":
        cfg = d.get("config")
        if not isinstance(cfg, dict):
            return {"ok": False, "message": "no config"}
        R.save_config(cfg)
        return {"ok": True}
    if path == "/api/research/give":
        task = d.get("task") or {}
        if task.get("type") not in R.TASK_TYPES:
            return {"ok": False, "message": "pick a task"}
        import world
        to = d.get("to") or "*"
        users = world.account_names() if to == "*" else [to]
        return {"ok": True, "count": R.give_field(users, task)}
    if path == "/api/research/assign":
        cfg = R.load_config()
        group = d.get("group") if d.get("group") in ("special", "timed") else "special"
        s = next((x for x in cfg[group] if x["id"] == d.get("id")), None)
        if not s:
            return {"ok": False, "message": "storyline not found"}
        to = d.get("to") or "*"
        if to == "*":
            s["trainers"] = []
        elif not s["trainers"]:
            return {"ok": True, "message": "already for everyone"}
        elif to not in s["trainers"]:
            s["trainers"].append(to)
        R.save_config(cfg)
        return {"ok": True}
    if path == "/api/research/reset":
        R.reset(d.get("user") or "", d.get("what") or "all")
        return {"ok": True}
    if path == "/api/research/stamps":
        R.set_stamps(d.get("user") or "", int(d.get("n") or 0))
        return {"ok": True}
    return None
