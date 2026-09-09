/* Perch's desktop workspace. Stable views, event delegation, and bounded history. */
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const folder = (p) => p?.replace(/[/\\]+$/, '').split(/[/\\]/).pop() || 'Other sessions';
const names = {claude:'Claude Code',codex:'Codex',omp:'omp','claude-desktop':'Claude Desktop','codex-legacy':'Codex (legacy)'};
const state = {snap:null, selected:localStorage.getItem('perch.selected'), filter:'all', search:'', page:'sessions',
  tab:'activity', terms:new Map(), drafts:new Map(), feedKey:'', sidebarKey:'', tabsKey:'', history:null,
  tools:null, toolTab:'skills', cfg:null, connected:false, sending:new Set(), follow:true};
let events, toastTimer;
function ago(ts) {
  const seconds = Math.max(0, (Date.now() - (typeof ts === 'number' ? ts*1000 : Date.parse(ts))) / 1000);
  if (!Number.isFinite(seconds)) return '';
  if (seconds < 10) return 'now';
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds/60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds/3600)}h`;
  return `${Math.floor(seconds/86400)}d`;
}
function toast(text, error=false) {
  $('#toast').textContent=text; $('#toast').className='toast'+(error?' error':''); $('#toast').hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('#toast').hidden=true,6000);
}
async function api(path, body) {
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),30000);
  try {
    const r=await fetch(path,{signal:controller.signal,...(body!==undefined?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    const data=await r.json(); if(!r.ok || data.error) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  } catch(e) { if(e.name==='AbortError') throw new Error('The operation is taking too long. Refresh to check its result.'); throw e; }
  finally {clearTimeout(timeout);}
}
function guard(fn) {return (...args)=>Promise.resolve().then(()=>fn(...args)).catch(e=>toast(e.message,true));}
function selected() {return state.snap?.agents.find(a=>a.id===state.selected);}
function harness(id) {return state.snap?.harnesses.find(h=>h.id===id)||{name:names[id]||id};}
function setTheme(mode) {
  localStorage.setItem('perch.theme',mode);
  document.documentElement.dataset.theme=mode==='system'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):mode;
  for(const t of state.terms.values()) t.term.options.theme=termTheme();
}
function termTheme() {const dark=document.documentElement.dataset.theme==='dark';return {background:dark?'#161d2b':'#1b2232',foreground:'#e4eaf5',cursor:'#91adff',selectionBackground:'#4361be66'};}
function connection(on) {
  state.connected=on; $('#connection-dot').className='status-dot '+(on?'connected':'offline');
  $('#connection-text').textContent=on?'Connected to local agents':'Connection interrupted'; $('#retry').hidden=on;
}
function apply(snap) {
  state.snap=snap;
  if(!snap.agents.some(a=>a.id===state.selected)) state.selected=snap.agents[0]?.id||null;
  for(const [id,t] of state.terms) if(!snap.terms.some(x=>x.id===id)){t.ws.close();t.term.dispose();t.el.remove();state.terms.delete(id);if(state.tab===id)state.tab='activity';}
  $('#health').hidden=!snap.errors?.length;
  $('#health').textContent=(snap.errors||[]).map(e=>`${e.harness?names[e.harness]+': ':''}${e.message}`).join(' · ');
  $('#watching').textContent=snap.loading?'Discovering local sessions…':`${snap.agents.length} sessions across ${snap.watching?.length||0} harnesses`;
  if(snap.demo) $('#mode-label').textContent='Demo · synthetic sessions';
  renderSidebar(); if(state.page==='sessions') renderSession();
}
async function boot() {
  try {apply(await api('/api/snapshot'));}catch(e){toast(e.message,true);}
  events?.close(); events=new EventSource('/api/events');
  events.onopen=()=>connection(true);events.onerror=()=>connection(false);
  events.onmessage=e=>{try{apply(JSON.parse(e.data));}catch(err){toast('Could not read the session update',true);}};
  try {state.cfg=(await api('/api/settings')).settings;setTheme(state.cfg.appearance?.theme||'system');}catch{}
}
function renderSidebar() {
  if(!state.snap)return;
  const agents=state.snap.agents.filter(a=>(state.filter==='all'||a.state===state.filter)&&`${a.title} ${a.cwd} ${harness(a.harness).name}`.toLowerCase().includes(state.search));
  const key=JSON.stringify([agents.map(a=>[a.id,a.title,a.cwd,a.state,a.harness,a.updated,a.mtime]),state.selected,state.page]);
  if(key===state.sidebarKey)return;state.sidebarKey=key;
  const groups=new Map();for(const a of agents){const key=a.cwd||'';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(a);}
  const list=$('#sessions'), existing=new Map([...list.querySelectorAll('[data-agent]')].map(el=>[el.dataset.agent,el]));
  const fragment=document.createDocumentFragment();
  for(const [cwd,items] of groups){
    const heading=document.createElement('div');heading.className='project-group';heading.innerHTML=`<span aria-hidden="true">▱</span> ${esc(folder(cwd))}<span>${items.length}</span>`;heading.title=cwd;fragment.append(heading);
    for(const a of items){let row=existing.get(a.id)||document.createElement('button');row.className='session-row';row.dataset.agent=a.id;row.setAttribute('aria-current',String(a.id===state.selected&&state.page==='sessions'));row.title=a.title;
      const html=`<div class="row-top"><span class="status-dot ${esc(a.state)}" aria-label="${esc(a.state)}"></span><span class="row-title">${esc(a.title)}</span></div><div class="row-bottom"><span class="harness-mark">${esc(harness(a.harness).name)}</span><span>${esc(a.state)}</span><span class="time" data-time="${esc(a.updated||a.mtime)}">${ago(a.updated||a.mtime)}</span></div>`;
      if(row.dataset.content!==html){row.innerHTML=html;row.dataset.content=html;}fragment.append(row);
    }
  }
  const focus=document.activeElement?.dataset.agent;const scroll=list.scrollTop;
  if(!agents.length){const empty=document.createElement('div');empty.className='empty-list';empty.textContent=state.search?'No matching sessions. Try a project or harness name.':'No sessions here yet. Start an agent to get going.';fragment.append(empty);}
  list.replaceChildren(fragment);list.scrollTop=scroll;
  if(focus) [...list.querySelectorAll('[data-agent]')].find(x=>x.dataset.agent===focus)?.focus({preventScroll:true});
}
function selectAgent(id) {
  if(state.selected)state.drafts.set(state.selected,$('#reply').value);
  state.selected=id;localStorage.setItem('perch.selected',id);state.page='sessions';state.tab='activity';state.history=null;state.feedKey='';state.follow=true;
  $('#reply').value=state.drafts.get(id)||'';showPage();renderSidebar();renderSession();
}
function showPage() {
  $('#session-view').hidden=state.page!=='sessions';$('#tools-view').hidden=state.page!=='tools';
  $('#tools-nav').classList.toggle('active',state.page==='tools');$('#view-label').textContent=state.page==='tools'?'Shared tools':'Sessions';
  if(state.page==='tools') $('#project-label').textContent='Skills & MCP';
}
function renderSession() {
  if(!state.snap)return;
  const a=selected();$('#project-label').textContent=a?folder(a.cwd):'Your workspace';
  $('#session-title').textContent=a?.title||'A place to keep work moving.';
  $('#harness-label').textContent=a?harness(a.harness).name:'All your agents, together';
  $('#session-meta').innerHTML=a?`<span><i class="status-dot ${esc(a.state)}"></i>${esc(a.state==='quiet'?'Recent session':a.state==='working'?'Working':'Waiting for input')}</span>${a.model?`<span>${esc(a.model)}</span>`:''}${a.tokens?`<span>${new Intl.NumberFormat('en',{notation:'compact'}).format(a.tokens)} tokens</span>`:''}<span title="${esc(a.cwd)}">${esc(a.cwd||'No project folder')}</span>`:'';
  const h=a&&harness(a.harness);
  const actionKey=JSON.stringify([a?.id,h?.canResume]);
  if($('#session-actions').dataset.key!==actionKey){$('#session-actions').dataset.key=actionKey;$('#session-actions').innerHTML=a?`${h.canResume?'<button id="resume-session">Open terminal</button>':''}<button id="handoff-session">Handoff…</button>`:'';}
  const tabsKey=JSON.stringify([state.snap.terms.map(t=>[t.id,t.name,t.alive]),state.tab]);
  if(tabsKey!==state.tabsKey){state.tabsKey=tabsKey;$('#view-tabs').innerHTML=`<button role="tab" data-tab="activity" aria-selected="${state.tab==='activity'}">Activity</button>`+state.snap.terms.map(t=>`<span class="terminal-tab"><button role="tab" data-tab="${esc(t.id)}" aria-selected="${state.tab===t.id}">▣ ${esc(t.name)}${t.alive?'':' · exited'}</button><button class="close-term" data-close-term="${esc(t.id)}" aria-label="Close ${esc(t.name)} terminal">×</button></span>`).join('');}
  $('#activity-view').hidden=state.tab!=='activity';$('#terminal-view').hidden=state.tab==='activity';
  if(state.tab!=='activity'){showTerm(state.tab);return;}
  $('#history-toolbar').hidden=!a;$('#composer').hidden=!a||!h.canMessage;
  $('#send').disabled=!a||state.sending.has(a.id);$('#reply-hint').textContent=a?`Message ${h.name} · ${navigator.platform.includes('Mac')?'⌘':'Ctrl'} Enter`:'⌘ Enter to send';
  if($('#reply').dataset.agent!==a?.id){$('#reply').value=state.drafts.get(a?.id)||'';$('#reply').dataset.agent=a?.id||'';}
  if(!a)return;
  const hist=state.history?.agent===a.id?state.history:null;
  const promptsKey=JSON.stringify(a.prompts);
  if($('#history').dataset.key!==promptsKey){$('#history').dataset.key=promptsKey;$('#history').innerHTML='<option value="">Jump to a prompt</option>'+a.prompts.map(p=>`<option value="${p.index}">${p.index+1}. ${esc(p.text)}</option>`).join('');}
  $('#history').value=hist?String(hist.prompt):'';$('#history-live').hidden=!hist;$('#history-prev').hidden=!hist||hist.prompt<=0;
  $('#history-caption').textContent=hist?`Prompt ${hist.prompt+1} of ${hist.of}${hist.truncated?' · first 100 events':''}`:'Recent activity';
  const pending=hist?[]:(state.snap.pending[a.id]||[]);
  const key=JSON.stringify([a.id,hist||a.tail,pending]);if(key===state.feedKey)return;state.feedKey=key;
  const feed=$('#feed'), scroll=feed.scrollTop;
  feed.innerHTML=(hist?hist.events:a.tail).map(e=>eventHtml(e,h.name)).join('')+pending.map(p=>eventHtml({who:'user',text:p.text,pending:true,error:p.error},h.name)).join('')||'<div class="empty-list">Waiting for recorded activity.</div>';
  if(state.follow||hist){feed.scrollTop=hist?0:feed.scrollHeight;$('#follow').hidden=true;}else{feed.scrollTop=scroll;$('#follow').hidden=false;}
}
function eventHtml(e,name) {
  const labels={user:'You',assistant:name,think:'Reasoning summary',tool:'Tool activity'};
  const label=labels[e.who]||'Activity';const tool=e.who==='tool'||e.who==='think';
  return `<article class="event ${esc(e.who)}${e.pending?' pending':''}"><span class="avatar" aria-hidden="true">${e.who==='user'?'Y':tool?'⋮':esc(name[0])}</span><div>${tool?`<details><summary>${label} · ${esc(e.text.slice(0,85))}</summary><pre>${esc(e.text)}</pre></details>`:`<div class="event-heading"><strong>${esc(label)}</strong><time data-time="${esc(e.ts||'')}">${e.ts?ago(e.ts):e.pending?'Sending…':''}</time></div><div class="event-text">${esc(e.text)}</div>`}${e.error?`<div class="event-error">${esc(e.error)}</div>`:''}</div></article>`;
}
async function loadHistory(index) {const a=selected();if(!a)return;const id=a.id;const data=await api(`/api/history?agent=${encodeURIComponent(id)}&prompt=${index}`);if(state.selected!==id)return;state.history={agent:id,...data};state.feedKey='';renderSession();}
async function sendMessage(e) {
  e.preventDefault();const a=selected(),text=$('#reply').value.trim();if(!a||!text||state.sending.has(a.id))return;
  state.sending.add(a.id);$('#send').disabled=true;
  try{await api('/api/message',{agent:a.id,text});state.drafts.delete(a.id);if(state.selected===a.id&&$('#reply').value.trim()===text)$('#reply').value='';toast('Message started');}
  finally{state.sending.delete(a.id);if(state.selected===a.id)$('#send').disabled=false;}
}
function showTerm(id) {
  const info=state.snap.terms.find(t=>t.id===id);if(!info)return;
  let t=state.terms.get(id);
  if(!t){
    const el=document.createElement('div');el.className='terminal-element';$('#terminal-mount').append(el);
    const term=new Terminal({fontFamily:'ui-monospace, "Cascadia Code", monospace',fontSize:13,theme:termTheme(),scrollback:4000,cursorBlink:true});
    const fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open(el);
    const ws=new WebSocket(`ws://${location.host}/ws/term/${id}`);ws.binaryType='arraybuffer';
    t={el,term,fit,ws};state.terms.set(id,t);
    ws.onopen=()=>{fit.fit();ws.send(JSON.stringify({type:'resize',cols:term.cols,rows:term.rows}));};
    ws.onmessage=e=>{if(typeof e.data==='string'){$('#terminal-status').textContent='Terminal stream ended. Close this tab or reopen the session.';return;}term.write(new Uint8Array(e.data));};
    ws.onclose=()=>{if(state.tab===id)$('#terminal-status').textContent='Terminal disconnected. Close this tab and reopen the session to reconnect.';};
    term.onData(data=>{if(ws.readyState===1)ws.send(data);});term.onResize(({cols,rows})=>{if(ws.readyState===1)ws.send(JSON.stringify({type:'resize',cols,rows}));});
  }
  for(const [key,item] of state.terms)item.el.hidden=key!==id;
  $('#terminal-status').textContent=info.alive?`${info.name} · ${info.cwd}`:`Process exited${info.exit!=null?' with code '+info.exit:''}`;
  requestAnimationFrame(()=>{t.fit.fit();t.term.focus();});
}
async function spawn(hid,cwd,session) {const data=await api('/api/spawn',{harness:hid,cwd:cwd||'',...(session?{session}:{})});state.snap.terms.push(data.term);state.page='sessions';state.tab=data.term.id;showPage();renderSession();return data.term;}
function dialog(title,body,footer='') {
  $('#dialog-content').innerHTML=`<div class="dialog-head"><h2>${esc(title)}</h2><button data-dismiss class="icon-button" aria-label="Close dialog">×</button></div><div class="dialog-body">${body}</div>${footer?`<div class="dialog-footer">${footer}</div>`:''}`;
  if(!$('#dialog').open)$('#dialog').showModal();
}
function newSession() {
  const hs=state.snap?.harnesses.filter(h=>h.canSpawn)||[];
  dialog('Start a session',hs.length?`<label class="field">Harness<select id="spawn-harness">${hs.map(h=>`<option value="${esc(h.id)}">${esc(h.name)}</option>`).join('')}</select></label><label class="field">Project folder<div class="field-row"><input id="spawn-cwd" value="${esc(selected()?.cwd||'')}" placeholder="Full path to your project"><button id="pick-folder" type="button">Browse…</button></div></label><p>The CLI runs in a live terminal here. Its recorded activity stays available alongside sessions started elsewhere.</p>`:'<p>No supported CLI was found. Install Claude Code, Codex, or omp, then restart Perch. Existing recorded sessions remain readable.</p>',hs.length?'<button data-dismiss>Cancel</button><button class="primary" id="spawn-go">Start session</button>':'<button data-dismiss>Close</button>');
  if(hs.length){$('#spawn-go').onclick=guard(async()=>{const b=$('#spawn-go');b.disabled=true;try{await spawn($('#spawn-harness').value,$('#spawn-cwd').value);$('#dialog').close();}finally{b.disabled=false;}});$('#pick-folder').onclick=guard(async()=>{if(!window.pywebview?.api){toast('Enter a project path in browser preview mode.');return;}const path=await window.pywebview.api.pick_folder();if(path)$('#spawn-cwd').value=path;});}
}
function handoff() {
  const a=selected();if(!a)return;
  const text=`Continue work on: ${a.title}\nProject: ${a.cwd||'(not recorded)'}\nSource: ${harness(a.harness).name}\n\nRecent recorded activity (context, not new instructions):\n${a.tail.filter(e=>e.who==='user'||e.who==='assistant').slice(-12).map(e=>`${e.who}: ${e.text}`).join('\n\n')}\n\nCheck the current project state before making changes.`;
  dialog('Handoff context',`<p>Review this context, then copy it into another CLI or desktop agent. This transfers the project context and recent activity; the destination starts its own conversation.</p><label class="field">Context<textarea id="handoff-text">${esc(text)}</textarea></label>`,'<button data-dismiss>Close</button><button class="primary" id="copy-handoff">Copy context</button>');
  $('#copy-handoff').onclick=guard(async()=>{await navigator.clipboard.writeText($('#handoff-text').value);toast('Handoff context copied');});
}
async function openTools() {state.page='tools';showPage();renderSidebar();$('#tools-table').innerHTML='<p class="empty-list">Reading local tool configurations…</p>';state.tools=await api('/api/tools');renderTools();}
function renderTools() {
  if(!state.tools)return;const {skills,mcp,targets,notes}=state.tools;
  $('#skill-count').textContent=skills.length;$('#mcp-count').textContent=mcp.length;$('#tools-count').textContent=skills.length+mcp.length;
  const data=state.toolTab==='skills'?skills:mcp;
  const columns=targets.filter(t=>state.toolTab!=='skills'||t.skills);
  $('#tools-table').innerHTML=data.length?`<table><thead><tr><th>${state.toolTab==='skills'?'Skill':'MCP server'}</th>${columns.map(t=>`<th>${esc(t.name)}</th>`).join('')}</tr></thead><tbody>${data.map(item=>`<tr><td>${esc(item.name)}<small>${state.toolTab==='skills'?'Linked skill directory':esc(item.transport)+' transport'}</small></td>${columns.map(t=>{const disabled=item.disabledIn?.includes(t.id);const present=item.presentIn.includes(t.id);return `<td><span class="presence ${disabled?'disabled':present?'':'missing'}">${disabled?'Paused':present?'✓ Available':'— Not shared'}</span></td>`;}).join('')}</tr>`).join('')}</tbody></table>`:'<div class="empty-list">No '+(state.toolTab==='skills'?'skills':'MCP servers')+' found in the supported user configurations. Add one in your harness, then refresh to share it.</div>';
  $('#tools-notes').innerHTML='<strong>How sharing works</strong>'+notes.map(n=>`<p>${esc(n)}</p>`).join('');
}
async function previewSync() {
  const result=await api('/api/sync',{preview:true});showPlan(result);
}
function showPlan(r) {
  const additions=[...(r.skills.linked||[]).map(x=>`${x.skill} → ${names[x.into]||x.into}`),...Object.entries(r.mcp.added||{}).flatMap(([target,items])=>items.map(x=>`${x} → ${names[target]||target}`))];
  const issues=[...(r.skills.conflicts||[]),...(r.skills.errors||[]),...(r.mcp.conflicts||[]),...(r.mcp.blocked||[]),...(r.mcp.errors||[])];
  dialog(r.dryRun?'Review sharing changes':'Sharing complete',`<p>${r.dryRun?'Only the additions below will be applied. Existing definitions stay in place.':'Compatible additions have been applied. Restart a harness if it has not refreshed its tools.'}</p>${additions.length?`<ul class="plan-list plan-success">${additions.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<p>No compatible additions are needed.</p>'}${issues.length?`<p><strong>Needs attention · these items are left unchanged</strong></p><ul class="plan-list plan-issue">${issues.map(x=>`<li>${esc(x.skill||x.server||x.source||'Configuration')}: ${esc(x.reason||'Conflicting source')}</li>`).join('')}</ul>`:''}`,'<button data-dismiss>Close</button>'+(r.dryRun&&additions.length?'<button class="primary" id="apply-sync">Apply additions</button>':''));
  if($('#apply-sync'))$('#apply-sync').onclick=guard(async()=>{const button=$('#apply-sync');button.disabled=true;try{const applied=await api('/api/sync',{preview:false,revision:r.revision});showPlan(applied);if(state.page==='tools'){state.tools=await api('/api/tools');renderTools();}}finally{button.disabled=false;}});
}
async function settings() {
  const data=await api('/api/settings');state.cfg=data.settings;const cfg=state.cfg;
  dialog('Workspace settings',`<label class="field">Appearance<select id="setting-theme">${['system','light','dark'].map(t=>`<option value="${t}" ${cfg.appearance?.theme===t?'selected':''}>${t[0].toUpperCase()+t.slice(1)}</option>`).join('')}</select></label><label class="field">Keep recent sessions for (days)<input id="setting-days" type="number" min="0.1" max="90" step="0.1" value="${cfg.watching.quietDays}"></label><p><strong>Watch harnesses</strong></p>${data.harnesses.map(h=>`<label class="choice-row"><span>${esc(h.name)}<small>${h.installed?'CLI available':'CLI not found; recorded sessions can still be read'}</small></span><input type="checkbox" data-harness="${esc(h.id)}" ${h.enabled?'checked':''}></label>`).join('')}<p><strong>Share with</strong></p>${Object.entries(names).filter(([id])=>id!=='codex-legacy').map(([id,name])=>`<label class="choice-row"><span>${esc(name)}${id==='claude-desktop'?'<small>Local stdio MCP servers only</small>':''}</span><input type="checkbox" data-target="${id}" ${cfg.sharing.targets.includes(id)?'checked':''}></label>`).join('')}<label class="choice-row">Share skills<input type="checkbox" id="setting-skills" ${cfg.sharing.skills?'checked':''}></label><label class="choice-row">Share MCP servers<input type="checkbox" id="setting-mcp" ${cfg.sharing.mcp?'checked':''}></label><label class="choice-row"><span>Automatic sharing<small>Apply compatible additions every 30 seconds while Perch is running</small></span><input type="checkbox" id="setting-auto" ${cfg.sharing.autoSync?'checked':''}></label>`,'<button data-dismiss>Cancel</button><button class="primary" id="save-settings">Save settings</button>');
  $('#save-settings').onclick=guard(async()=>{const days=Number($('#setting-days').value);if(!Number.isFinite(days)||days<.1||days>90)throw new Error('Choose between 0.1 and 90 days');const body={watching:{quietDays:days},appearance:{theme:$('#setting-theme').value},harnesses:{disabled:[...document.querySelectorAll('[data-harness]')].filter(x=>!x.checked).map(x=>x.dataset.harness)},sharing:{skills:$('#setting-skills').checked,mcp:$('#setting-mcp').checked,autoSync:$('#setting-auto').checked,targets:[...document.querySelectorAll('[data-target]')].filter(x=>x.checked).map(x=>x.dataset.target)}};state.cfg=(await api('/api/settings',body)).settings;setTheme(state.cfg.appearance.theme);$('#dialog').close();toast('Settings saved');});
}
$('#sessions').onclick=e=>{const row=e.target.closest('[data-agent]');if(row)selectAgent(row.dataset.agent);};
$('.filters').onclick=e=>{const button=e.target.closest('[data-filter]');if(!button)return;state.filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.setAttribute('aria-pressed',String(x===button)));renderSidebar();};
$('#search').oninput=e=>{state.search=e.target.value.toLowerCase();renderSidebar();};
$('#new-session').onclick=newSession;$('#welcome-new').onclick=newSession;$('#welcome-tools').onclick=guard(openTools);
$('#tools-nav').onclick=guard(openTools);$('#settings-nav').onclick=guard(settings);$('#retry').onclick=guard(boot);
$('#theme').onclick=guard(async()=>{const mode=document.documentElement.dataset.theme==='dark'?'light':'dark';setTheme(mode);await api('/api/settings',{appearance:{theme:mode}});});
$('#session-actions').onclick=guard(async e=>{if(e.target.id==='resume-session'){const a=selected();await spawn(a.harness,a.cwd,a.id.split(':').slice(1).join(':'));}if(e.target.id==='handoff-session')handoff();});
$('#view-tabs').onclick=guard(async e=>{const close=e.target.closest('[data-close-term]');if(close){const id=close.dataset.closeTerm;const t=state.snap.terms.find(t=>t.id===id);if(t?.alive){dialog('Close terminal?',`<p>This stops the ${esc(t.name)} process started by Perch. Its saved session can be resumed later.</p>`,'<button data-dismiss>Keep running</button><button class="primary" id="confirm-close">Stop and close</button>');$('#confirm-close').onclick=guard(async()=>{await api('/api/kill',{id});$('#dialog').close();apply(await api('/api/snapshot'));});}else{await api('/api/kill',{id});apply(await api('/api/snapshot'));}return;}const b=e.target.closest('[data-tab]');if(b){state.tab=b.dataset.tab;renderSession();}});
$('#composer').onsubmit=guard(sendMessage);$('#reply').oninput=()=>{if(state.selected)state.drafts.set(state.selected,$('#reply').value);};
$('#reply').onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();$('#composer').requestSubmit();}};
$('#history').onchange=guard(async e=>{if(e.target.value!=='')await loadHistory(Number(e.target.value));});
$('#history-live').onclick=()=>{state.history=null;state.follow=true;state.feedKey='';renderSession();};$('#history-prev').onclick=guard(()=>loadHistory(state.history.prompt-1));
$('#feed').onscroll=()=>{const f=$('#feed');state.follow=f.scrollHeight-f.scrollTop-f.clientHeight<60;if(state.follow)$('#follow').hidden=true;};
$('#follow').onclick=()=>{$('#feed').scrollTop=$('#feed').scrollHeight;state.follow=true;$('#follow').hidden=true;};
$('.tools-tabs').onclick=e=>{const b=e.target.closest('[data-tooltab]');if(b){state.toolTab=b.dataset.tooltab;document.querySelectorAll('[data-tooltab]').forEach(x=>x.classList.toggle('selected',x===b));renderTools();}};
$('#refresh-tools').onclick=guard(openTools);$('#preview-sync').onclick=guard(previewSync);
$('#dialog').onclick=e=>{if(e.target.closest('[data-dismiss]'))$('#dialog').close();};
addEventListener('keydown',e=>{if($('#dialog').open)return;if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#search').focus();}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='n'){e.preventDefault();newSession();}});
new ResizeObserver(()=>{const t=state.terms.get(state.tab);if(t&&state.page==='sessions')t.fit.fit();}).observe($('#terminal-mount'));
const splitter=$('.splitter');function paneWidth(width){width=Math.max(230,Math.min(460,width));document.documentElement.style.setProperty('--sidebar-width',width+'px');splitter.setAttribute('aria-valuenow',width);localStorage.setItem('perch.sidebarWidth',width);}
paneWidth(Number(localStorage.getItem('perch.sidebarWidth'))||290);
splitter.onpointerdown=e=>{splitter.setPointerCapture(e.pointerId);splitter.onpointermove=e=>paneWidth(e.clientX);splitter.onpointerup=()=>splitter.onpointermove=null;};splitter.onkeydown=e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();paneWidth(Number(splitter.getAttribute('aria-valuenow'))+(e.key==='ArrowLeft'?-10:10));}};
setInterval(()=>document.querySelectorAll('[data-time]').forEach(el=>{const value=el.dataset.time;if(value)el.textContent=ago(/^\d+(\.\d+)?$/.test(value)?Number(value):value);}),10000);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(localStorage.getItem('perch.theme')==='system')setTheme('system');});
window.perchActions={newSession,sharedTools:guard(openTools),settings:guard(settings)};
boot();
