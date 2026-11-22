'use strict';
/* The frame around the conversation: top bar with the context gauge, the library list, the jobs panel. */

/* ------------------------------------------------------------------ top bar and context gauge */
MR.renderTop = function () {
  const S = MR.S, p = S.profile;
  const theme = MR.theme();
  const chip = p
    ? `<button class="model-chip" id="model-chip" type="button" aria-label="Context window and model details">
         <svg class="ring" id="ring" viewBox="0 0 32 32" aria-hidden="true">
           <circle class="track" cx="16" cy="16" r="13"/>
           <circle class="arc" id="ring-arc" cx="16" cy="16" r="13" transform="rotate(-90 16 16)"/>
         </svg>
         <span class="names"><span class="prov">${MR.esc(p.name)}</span><span class="model" id="chip-model">${MR.esc(p.model || 'choose a model')}</span></span>
         <span class="figures"><span id="fig-used"></span><span id="fig-pct"></span></span>
       </button>`
    : `<button class="model-chip empty" id="model-chip" type="button">No model connected. Connect one</button>`;
  MR.$('#top').innerHTML = `
    <button class="icon-btn" id="btn-libs" type="button" aria-label="Libraries" style="display:none">
      <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button>
    <div class="brand">
      <svg class="mark" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.7" stroke-linejoin="miter" aria-hidden="true">
        <path d="M12 6 3 5v13l9 1 9-1V5z"/><path d="M12 6v13"/><path d="M5.5 22h5"/></svg>
      <span>Perplexify</span><span class="ver">v${MR.esc(S.version)}</span>
    </div>
    <div class="spacer"></div>
    ${chip}
    <div class="seg" role="group" aria-label="Colour theme">
      <button type="button" data-theme-btn="light" aria-pressed="${theme === 'light'}">light</button>
      <button type="button" data-theme-btn="dark" aria-pressed="${theme === 'dark'}">dark</button>
    </div>
    <button class="icon-btn" id="btn-settings" type="button" title="Settings" aria-label="Settings">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>
    </button>`;
  MR.$$('[data-theme-btn]').forEach((b) => b.addEventListener('click', () => MR.setTheme(b.dataset.themeBtn)));
  MR.$('#btn-settings').addEventListener('click', () => MR.openSettings());
  MR.$('#model-chip').addEventListener('click', (e) => (p ? MR.showGauge(e.currentTarget) : MR.openConnect()));
  const libsBtn = MR.$('#btn-libs');
  libsBtn.addEventListener('click', () => MR.$('#side').classList.toggle('open'));
  libsBtn.style.display = window.matchMedia('(max-width: 860px)').matches ? '' : 'none';
  MR.updateGauge();
};

MR.updateGauge = function () {
  const arc = MR.$('#ring-arc');
  if (!arc) return;
  const c = MR.S.ctx, g = MR.gauge();
  arc.setAttribute('stroke-dasharray', String(MR.RING_C));
  arc.setAttribute('stroke-dashoffset', String(MR.RING_C * (1 - g.shown)));
  const ring = MR.$('#ring');
  ring.setAttribute('class', 'ring ' + (c.loading ? 'loading' : g.level));
  MR.$('#fig-used').innerHTML = c.window ? `<b>${MR.fmt.n(c.used)}</b> / ${MR.fmt.n(c.window)}` : 'context';
  MR.$('#fig-pct').textContent = c.window ? MR.fmt.pct(g.pct) + ' used' : '';
  const pop = MR.$('#gauge-pop');
  if (pop) MR.fillGauge(pop);
};

MR.fillGauge = function (pop) {
  const c = MR.S.ctx, p = MR.S.profile, g = MR.gauge(), b = c.breakdown;
  const src = { api: 'reported by the provider', setting: 'set in Settings', default: 'catalog default' }[c.source] || c.source || '-';
  pop.innerHTML = `
    <h4>Context window</h4>
    <dl class="kv" style="grid-template-columns:118px 1fr">
      <dt>model</dt><dd>${MR.esc(p ? p.model : '-')}</dd>
      <dt>window</dt><dd>${MR.fmt.int(c.window)} tokens <span class="faint">(${MR.esc(src)})</span></dd>
      <dt>${c.actual ? 'last request' : 'this request'}</dt><dd>${MR.fmt.int(Math.round(c.used))} tokens, ${MR.fmt.pct(g.pct)}</dd>
      ${c.maxOutput ? `<dt>max output</dt><dd>${MR.fmt.int(c.maxOutput)} tokens</dd>` : ''}
    </dl>
    ${b ? `<h4 style="margin-top:12px">Prompt breakdown (estimated)</h4>
    <dl class="kv" style="grid-template-columns:118px 1fr">
      <dt>instructions</dt><dd>${MR.fmt.int(b.system)}</dd>
      <dt>conversation</dt><dd>${MR.fmt.int(b.history)}</dd>
      <dt>excerpts</dt><dd>${MR.fmt.int(b.excerpts)}</dd>
      <dt>question</dt><dd>${MR.fmt.int(b.question)}</dd>
    </dl>` : '<p class="faint" style="margin:10px 0 0">Ask a question to see how the window is used.</p>'}
    <div class="row" style="margin-top:12px"><button class="btn small" type="button" id="gauge-change">Change model</button></div>`;
  const ch = MR.$('#gauge-change', pop);
  if (ch) ch.addEventListener('click', () => { MR.closePopover(); MR.openSettings('models'); });
};

MR.closePopover = function () {
  MR.$$('#gauge-pop, .menu').forEach((e) => e.remove());
  document.removeEventListener('mousedown', MR._outside, true);
};
MR._outside = (e) => { if (!e.target.closest('#gauge-pop, .menu, #model-chip, .menu-btn')) MR.closePopover(); };
MR.placeNear = function (el, anchor, align = 'right') {
  const r = anchor.getBoundingClientRect();
  el.style.top = Math.min(r.bottom + 6, window.innerHeight - el.offsetHeight - 8) + 'px';
  const left = align === 'right' ? r.right - el.offsetWidth : r.left;
  el.style.left = Math.max(8, Math.min(left, window.innerWidth - el.offsetWidth - 8)) + 'px';
};
MR.showGauge = function (anchor) {
  if (MR.$('#gauge-pop')) { MR.closePopover(); return; }
  MR.closePopover();
  const pop = MR.el('<div class="popover" id="gauge-pop"></div>');
  document.body.appendChild(pop);
  MR.fillGauge(pop);
  MR.placeNear(pop, anchor);
  document.addEventListener('mousedown', MR._outside, true);
  MR.refreshModelInfo();
};

MR.refreshModelInfo = async function () {
  const p = MR.S.profile;
  if (!p) { MR.S.ctx.window = 0; MR.updateGauge(); return; }
  try {
    const info = await MR.api('/api/llm/info?profile_id=' + encodeURIComponent(p.id));
    if (info.context_window) {
      const c = MR.S.ctx;
      c.window = info.context_window; c.source = info.context_source || ''; c.maxContext = info.max_context; c.maxOutput = info.max_output;
    }
  } catch (e) { /* the gauge keeps its last known window */ }
  MR.updateGauge();
};

/* ------------------------------------------------------------------ libraries */
MR.libKinds = function (l) {
  const k = l.kinds || {};
  const code = k.code || 0;
  const docs = Object.keys(k).filter((x) => x !== 'code').reduce((a, x) => a + k[x], 0);
  return { code, docs };
};

MR.renderSide = function () {
  const S = MR.S;
  const side = MR.$('#side');
  const sel = S.libs.filter((l) => S.selected.has(l.id)).length;
  const items = S.libs.map((l) => {
    const { code, docs } = MR.libKinds(l);
    const busy = S.busy.has(l.id);
    let state = '';
    if (busy) state = '<div class="state"><span class="warn">working...</span></div>';
    else if (l.chunks === 0) state = '<div class="state faint">empty. Use Add source</div>';
    else if (l.pending > 0) state = `<div class="state warn">${MR.fmt.n(l.embedded)} / ${MR.fmt.n(l.chunks)} embedded. <a href="#" data-resume="${MR.esc(l.id)}">finish</a></div>`;
    return `<div class="lib${S.focus === l.id ? ' focus' : ''}" data-id="${MR.esc(l.id)}">
      <label class="check"><input type="checkbox" ${S.selected.has(l.id) ? 'checked' : ''} aria-label="Search ${MR.esc(l.name)}"></label>
      <div class="name" title="${MR.esc(l.name)}. Click to focus it: new sources are added here">${MR.esc(l.name)}</div>
      <div class="tools"><button class="only" type="button" title="Search only this library">only</button>
        <button class="menu-btn" type="button" aria-label="Actions for ${MR.esc(l.name)}">&middot;&middot;&middot;</button></div>
      <div class="meta">
        ${code ? '<span class="tag code">code</span>' : ''}${docs ? '<span class="tag">docs</span>' : ''}
        <span>${MR.fmt.n(l.documents)} ${code >= docs ? 'files' : 'pages'}</span><span>${MR.fmt.n(l.chunks)} passages</span>
      </div>${state}
    </div>`;
  }).join('');
  const jobs = S.jobs.filter((j) => !S.dismissed.has(j.id)).slice(0, 5).map((j) => {
    const run = j.status === 'queued' || j.status === 'running';
    const pct = j.total ? Math.min(100, (100 * j.current) / j.total) : 0;
    const color = j.status === 'error' ? 'err' : j.status === 'done' ? 'ok' : j.status === 'cancelled' ? 'faint' : '';
    return `<div class="job" data-job="${j.id}">
      <div class="j-title"><span title="${MR.esc(j.title)}">${MR.esc(j.title)}</span>
        ${run ? `<a href="#" data-cancel="${j.id}">stop</a>` : `<a href="#" data-dismiss="${j.id}">dismiss</a>`}</div>
      ${run ? `<div class="bar ${j.total ? '' : 'indeterminate'}"><i style="width:${pct}%"></i></div>` : ''}
      <div class="j-msg ${color}">${run ? MR.esc((j.phase || 'working') + (j.total ? ` ${MR.fmt.n(j.current)}/${MR.fmt.n(j.total)}` : '')) + ' ' : j.status + ': '}${MR.esc(j.error || j.message || '')}</div>
    </div>`;
  }).join('');
  const focused = S.libs.find((l) => l.id === S.focus);
  const addLabel = focused ? '+ Add to ' + (focused.name.length > 20 ? focused.name.slice(0, 19) + '...' : focused.name) : '+ Add source';
  side.innerHTML = `
    <div class="side-head"><span class="label">Libraries <span class="faint">${sel}/${S.libs.length}</span></span>
      ${S.libs.length > 1 ? '<span class="links"><a href="#" data-sel="all">all</a><a href="#" data-sel="none">none</a></span>' : ''}</div>
    <div class="libs">${items || '<div class="side-empty">Nothing here yet. Add a manual, a documentation website or a code repository.</div>'}</div>
    ${jobs ? `<div class="jobs">${jobs}</div>` : ''}
    <div class="side-foot"><button class="btn primary" id="btn-add" type="button" title="${focused ? 'Add a website, repository or documents to ' + MR.esc(focused.name) : 'Add a website, repository or documents'}">${MR.esc(addLabel)}</button></div>`;
  MR.$('#btn-add').addEventListener('click', () => MR.openAdd('web', S.focus));
  MR.$$('[data-sel]', side).forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    S.selected = a.dataset.sel === 'all' ? new Set(S.libs.map((l) => l.id)) : new Set();
    MR.selectionChanged();
  }));
  MR.$$('.lib', side).forEach((row) => {
    const id = row.dataset.id;
    MR.$('input', row).addEventListener('change', (e) => {
      e.target.checked ? S.selected.add(id) : S.selected.delete(id);
      MR.selectionChanged();
    });
    MR.$('.name', row).addEventListener('click', () => {
      S.focus = S.focus === id ? null : id;
      MR.store.set('focus', S.focus);
      MR.renderSide();
    });
    MR.$('.only', row).addEventListener('click', () => { S.selected = new Set([id]); MR.selectionChanged(); });
    MR.$('.menu-btn', row).addEventListener('click', (e) => MR.libMenu(id, e.currentTarget));
  });
  MR.$$('[data-resume]', side).forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); MR.libAction(a.dataset.resume, 'resume'); }));
  MR.$$('[data-cancel]', side).forEach((a) => a.addEventListener('click', async (e) => { e.preventDefault(); await MR.api('/api/jobs/' + a.dataset.cancel + '/cancel', { method: 'POST' }); MR.pollJobs(); }));
  MR.$$('[data-dismiss]', side).forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); S.dismissed.add(a.dataset.dismiss); MR.renderSide(); }));
};

/* Which libraries are ticked. A library this browser has not seen before starts ticked, so a manual added from the command line
   (or in another window) is searchable at once; a library you unticked stays unticked, also the next time the app opens. */
MR.reconcileTicks = function () {
  const S = MR.S;
  let base, seen;
  if (S.selectionReady) { base = S.selected; seen = S.knownLibs; }                  // while the app is open, memory is the truth
  else {
    const stored = MR.store.get('selected', null);
    base = new Set(stored || []);
    seen = new Set(MR.store.get('seen', stored ? S.libs.map((l) => l.id) : []));     // older saves had no such list: whatever was there counts as seen
  }
  S.selected = new Set(S.libs.filter((l) => base.has(l.id) || !seen.has(l.id)).map((l) => l.id));
  S.knownLibs = new Set(S.libs.map((l) => l.id));
  S.selectionReady = true;
  MR.persistTicks();
};
MR.persistTicks = function () {
  MR.store.set('selected', Array.from(MR.S.selected));
  MR.store.set('seen', Array.from(MR.S.knownLibs));
};

/* the ticks decide which libraries every question and search looks in: one, several or all */
MR.selectionChanged = function () {
  MR.persistTicks();
  MR.renderSide();
  MR.updateScope();
  MR.updateSearchScope();
  if (MR.S.view === 'search') MR.searchSoon(); else MR.SS.stale = true;
};

/* ------------------------------------------------------------------ polling */
MR.refreshLibs = async function () {
  const r = await MR.api('/api/libraries');
  const S = MR.S;
  S.libs = r.libraries;
  S.busy = new Set(r.libraries.filter((l) => l.busy).map((l) => l.id));
  const known = new Set(S.libs.map((l) => l.id));
  const before = Array.from(S.selected).sort().join();
  MR.reconcileTicks();   // libraries that appeared since last time start ticked; deleted ones drop out
  if (before !== Array.from(S.selected).sort().join()) MR.SS.stale = true;
  if (S.focus && !known.has(S.focus)) S.focus = null;
  MR.renderSide();
  MR.renderWelcomeIfEmpty();
  MR.updateSearchScope();   // language chips follow what the libraries now contain
};

MR.pollJobs = async function () {
  const S = MR.S;
  try {
    const r = await MR.api('/api/jobs');
    const wasActive = S.jobs.some((j) => j.status === 'queued' || j.status === 'running');
    const prev = new Map(S.jobs.map((j) => [j.id, j.status]));
    S.jobs = r.jobs;
    const nowActive = S.jobs.some((j) => j.status === 'queued' || j.status === 'running');
    S.jobs.forEach((j) => {
      const before = prev.get(j.id);
      if (before && (before === 'running' || before === 'queued') && j.status === 'done') MR.toast(j.title + ' finished.', 'ok');
      if (before && (before === 'running' || before === 'queued') && j.status === 'error') MR.toast(j.title + ': ' + j.error, 'err', 8000);
      if (!before && j.status === 'done' && wasActive === false && j.finished && Date.now() / 1000 - j.finished < 3) MR.toast(j.title + ' finished.', 'ok');
    });
    if (wasActive || nowActive) await MR.refreshLibs(); else MR.renderSide();
    // new libraries created by a job should be searchable right away
    S.libs.forEach((l) => { if (!S.knownLibs.has(l.id)) { S.knownLibs.add(l.id); S.selected.add(l.id); } });
  } catch (e) { /* the app may be restarting; try again on the next tick */ }
  clearTimeout(MR._pollTimer);
  const active = MR.S.jobs.some((j) => j.status === 'queued' || j.status === 'running');
  MR._pollTimer = setTimeout(MR.pollJobs, active ? 1000 : 4000);
};
