'use strict';
/* Dialogs: connect a model, add a source, settings, library actions. */

/* ------------------------------------------------------------------ modal helper */
MR.modal = function ({ title, body, foot = '', narrow = false, onClose, closable = true }) {
  const root = MR.$('#modal-root');
  const ov = MR.el(`<div class="overlay"><div class="modal ${narrow ? 'narrow' : ''}" role="dialog" aria-modal="true" aria-label="${MR.esc(title)}">
      <div class="m-head"><h2>${MR.esc(title)}</h2>${closable ? '<button class="btn small quiet" type="button" data-close aria-label="Close">close</button>' : ''}</div>
      <div class="m-body">${body}</div>${foot ? `<div class="m-foot">${foot}</div>` : ''}</div></div>`);
  const ctl = {
    el: ov, $: (s) => MR.$(s, ov), $$: (s) => MR.$$(s, ov),
    close() { ov.remove(); document.removeEventListener('keydown', esc); if (onClose) onClose(); },
  };
  const esc = (e) => { if (e.key === 'Escape' && closable && root.lastElementChild === ov) ctl.close(); };
  document.addEventListener('keydown', esc);
  if (closable) {
    ov.addEventListener('mousedown', (e) => { if (e.target === ov) ctl.close(); });
    MR.$('[data-close]', ov).addEventListener('click', () => ctl.close());
  }
  root.appendChild(ov);
  const first = MR.$('input:not([type=hidden]), select, textarea', ov);
  if (first) first.focus();
  return ctl;
};

MR.confirm = (title, message, okLabel = 'Delete', danger = true) => new Promise((resolve) => {
  const m = MR.modal({ title, narrow: true, body: `<p style="margin:0">${MR.esc(message)}</p>`,
    foot: `<button class="btn" data-no type="button">Cancel</button><button class="btn ${danger ? 'danger' : 'primary'}" data-yes type="button">${MR.esc(okLabel)}</button>`,
    onClose: () => resolve(false) });
  m.$('[data-no]').addEventListener('click', () => m.close());
  m.$('[data-yes]').addEventListener('click', () => { m.el.remove(); resolve(true); });
});

MR.ask1 = (title, label, value = '') => new Promise((resolve) => {
  const m = MR.modal({ title, narrow: true, body: `<div class="field"><span class="label">${MR.esc(label)}</span><input class="input" id="ask1" value="${MR.esc(value)}"></div>`,
    foot: '<button class="btn" data-no type="button">Cancel</button><button class="btn primary" data-yes type="button">Save</button>', onClose: () => resolve(null) });
  const done = () => { const v = m.$('#ask1').value.trim(); m.el.remove(); resolve(v || null); };
  m.$('[data-no]').addEventListener('click', () => m.close());
  m.$('[data-yes]').addEventListener('click', done);
  m.$('#ask1').addEventListener('keydown', (e) => { if (e.key === 'Enter') done(); });
});

/* ------------------------------------------------------------------ boot data and settings reload */
MR.applyBoot = function (b) {
  const S = MR.S;
  S.boot = b; S.version = b.version; S.platform = b.platform; S.shell = b.shell || 'browser';
  S.catalog = b.providers; S.embedders = b.embedders; S.presets = b.presets;
  MR.applySettings(b.settings);
  S.libs = b.libraries;
  S.busy = new Set(b.busy || []);
  S.selectionReady = false;
  MR.reconcileTicks();    // every library ticked the first time; later, your choices plus any library you have not seen yet
  const focus = MR.store.get('focus', null);
  S.focus = S.knownLibs.has(focus) ? focus : null;
  S.view = MR.store.get('view', 'ask') === 'search' ? 'search' : 'ask';
};
MR.applySettings = function (s) {
  const S = MR.S;
  S.settings = s; S.profiles = s.profiles;
  S.profile = s.profiles.find((p) => p.id === s.active_profile && p.has_key) || s.profiles.find((p) => p.has_key) || null;
};
MR.reloadSettings = async function () {
  const [s, e] = await Promise.all([MR.api('/api/settings'), MR.api('/api/embedders')]);
  MR.applySettings(s);
  MR.S.embedders = e.options;
  MR.renderTop(); MR.updateBanner();
  MR.refreshModelInfo();
  const tr = MR.$('#transcript');
  if (tr && !tr.querySelector('.turn')) { tr.innerHTML = ''; MR.renderWelcomeIfEmpty(); }
};

/* ------------------------------------------------------------------ connect a model */
MR.openConnect = function (preselect) {
  const S = MR.S;
  let sel = preselect || (S.catalog.find((p) => p.env_var) || S.catalog[0] || {}).id || 'anthropic';
  let useEnv = false;
  const connected = new Set(S.profiles.filter((p) => p.has_key).map((p) => p.provider));
  const m = MR.modal({ title: 'Connect a model', closable: true,
    body: '<div id="cx"></div>',
    foot: '<span class="grow" id="cx-note"></span><button class="btn" data-close2 type="button">Cancel</button><button class="btn primary" id="cx-go" type="button">Connect</button>' });
  m.$('[data-close2]').addEventListener('click', () => m.close());
  let stopLocal = null;     // the Local model panel (js/local.js) while its tile is chosen

  const draw = () => {
    if (stopLocal) { stopLocal(); stopLocal = null; }
    const P = S.catalog.find((p) => p.id === sel);
    const grid = S.catalog.map((p) => `<button type="button" class="prov" data-p="${p.id}" aria-pressed="${p.id === sel}">
      ${connected.has(p.id) ? '<span class="tick">ok</span>' : ''}<b>${MR.esc(p.name)}</b><span>${MR.esc(p.company)}</span></button>`).join('');
    const custom = P.id === 'custom';
    const isLocal = !!(P.local && MR.local);     // a model on this computer: no key, its own panel
    m.$('#cx').innerHTML = isLocal ? `<div class="provgrid">${grid}</div><div id="cx-local"></div>` : `
      <div class="provgrid">${grid}</div>
      ${P.env_var ? `<div class="callout">Found <b class="mono">${MR.esc(P.env_var)}</b> in your environment.
        <button class="btn small" id="cx-env" type="button" style="margin-left:10px">${useEnv ? 'Using it' : 'Use it'}</button></div>` : ''}
      ${custom ? `<div class="field"><span class="label">Service address (base URL)</span><input class="input" id="cx-url" placeholder="https://api.example.com/v1" spellcheck="false">
        <div class="row wrap" style="margin-top:4px">${(P.quick_fill || []).map((q) => `<button class="btn small" type="button" data-fill="${MR.esc(q.base_url)}">${MR.esc(q.name)}</button>`).join('')}</div></div>
        <div class="field"><span class="label">Model name (optional)</span><input class="input" id="cx-model" placeholder="chosen from the service's list when it has one" spellcheck="false"></div>` : ''}
      <div class="field" ${useEnv ? 'hidden' : ''} id="cx-keywrap"><span class="label">API key</span>
        <div class="row"><input class="input" id="cx-key" type="password" autocomplete="off" spellcheck="false" placeholder="${MR.esc(P.key_hint)}">
          <button class="btn small" id="cx-show" type="button">show</button></div>
        <div class="hint" id="cx-detect"></div>
        ${P.key_url ? `<div class="hint">Don't have one? <a href="${MR.esc(P.key_url)}" target="_blank" rel="noopener noreferrer">Create a key at ${MR.esc(P.company)} &#8599;</a>. It is stored encrypted on this computer and only sent to ${MR.esc(custom ? 'that service' : P.company)}.</div>` : ''}</div>
      <div id="cx-status"></div>
      <div class="hint">${MR.esc(P.model_note || '')}</div>`;
    m.$$('.prov').forEach((b) => b.addEventListener('click', () => { sel = b.dataset.p; useEnv = false; draw(); }));
    if (isLocal) { stopLocal = MR.local.connectPane(m, () => connected.add('local')); return; }
    const env = m.$('#cx-env');
    if (env) env.addEventListener('click', () => { useEnv = !useEnv; draw(); });
    m.$$('[data-fill]').forEach((b) => b.addEventListener('click', () => { m.$('#cx-url').value = b.dataset.fill; }));
    const key = m.$('#cx-key');
    if (key) {
      m.$('#cx-show').addEventListener('click', (e) => { key.type = key.type === 'password' ? 'text' : 'password'; e.target.textContent = key.type === 'password' ? 'show' : 'hide'; });
      key.addEventListener('input', async () => {
        const v = key.value.trim();
        if (v.length < 12) { m.$('#cx-detect').textContent = ''; return; }
        try {
          const r = await MR.api('/api/detect-provider', { method: 'POST', body: { key: v } });
          if (r.provider && r.provider !== sel) { sel = r.provider; draw(); const k2 = m.$('#cx-key'); k2.value = v; k2.focus(); m.$('#cx-detect').textContent = 'That looks like a ' + S.catalog.find((p) => p.id === sel).name + ' key.'; }
        } catch (e) { /* detection is only a convenience */ }
      });
      key.addEventListener('keydown', (e) => { if (e.key === 'Enter') m.$('#cx-go').click(); });
    }
    m.$('#cx-go').textContent = 'Connect'; m.$('#cx-go').disabled = false;
    m.$('#cx-go').onclick = go;
  };

  const go = async () => {
    const P = S.catalog.find((p) => p.id === sel);
    const key = m.$('#cx-key') ? m.$('#cx-key').value.trim() : '';
    const st = m.$('#cx-status');
    if (!key && !useEnv) { st.innerHTML = '<div class="callout error">Paste your API key first.</div>'; return; }
    const btn = m.$('#cx-go'); btn.disabled = true; btn.textContent = 'Checking...';
    st.innerHTML = '<div class="callout">Checking the key with ' + MR.esc(P.name) + '...</div>';
    try {
      const r = await MR.api('/api/connect', { method: 'POST', body: {
        provider: sel, api_key: useEnv ? '' : key, base_url: m.$('#cx-url') ? m.$('#cx-url').value.trim() : '',
        model: m.$('#cx-model') ? m.$('#cx-model').value.trim() : '' } });
      await MR.reloadSettings();
      connected.add(sel);
      const opts = r.models.map((x) => `<option value="${MR.esc(x.id)}" ${x.id === r.model ? 'selected' : ''}>${MR.esc(x.name && x.name !== x.id ? x.name + '  (' + x.id + ')' : x.id)}</option>`).join('');
      m.$('#cx').innerHTML = `<div class="callout" style="border-left-color:var(--ok)"><b>Connected to ${MR.esc(P.name)}.</b> You can start asking questions.</div>
        <div class="field"><span class="label">Model</span>
          <select class="input" id="cx-pick">${opts || `<option>${MR.esc(r.model)}</option>`}</select>
          <div class="hint">${MR.esc(P.model_note || '')}</div></div>
        ${r.info && r.info.context_window ? `<div class="hint">Context window: ${MR.fmt.int(r.info.context_window)} tokens.</div>` : ''}
        ${r.embeddings_note ? `<div class="callout">${MR.esc(r.embeddings_note)}</div>` : ''}`;
      m.$('#cx-pick').addEventListener('change', async (e) => {
        await MR.api('/api/profiles/' + encodeURIComponent(r.profile.id), { method: 'PATCH', body: { model: e.target.value, activate: true } });
        await MR.reloadSettings();
      });
      btn.disabled = false; btn.textContent = 'Done'; btn.onclick = () => m.close();
      m.$('[data-close2]').hidden = true;
    } catch (e) {
      st.innerHTML = '<div class="callout error">' + MR.esc(e.message) + '</div>';
      btn.disabled = false; btn.textContent = 'Connect';
    }
  };
  draw();
};

/* ------------------------------------------------------------------ add a source */
MR.openAdd = function (tab = 'web', libId = null) {
  const S = MR.S;
  const hasLibs = S.libs.length > 0;
  /* opened from a focused library (or its menu): the source goes into that library unless you say otherwise */
  let dest = libId && S.libs.some((l) => l.id === libId) ? 'existing' : 'new';
  let destId = dest === 'existing' ? libId : (S.libs[0] ? S.libs[0].id : '');
  let files = [];
  const m = MR.modal({ title: 'Add a source', body: '', foot: '<span class="grow" id="ad-note"></span><button class="btn" data-close2 type="button">Cancel</button><button class="btn primary" id="ad-go" type="button">Add</button>' });
  m.$('.m-body').style.padding = '0';
  m.$('[data-close2]').addEventListener('click', () => m.close());
  const embedderOpts = (disabled) => S.embedders.map((o) => `<option value="${o.id}" ${o.available ? '' : 'disabled'} ${o.id === 'builtin' ? 'selected' : ''}>${MR.esc(o.name)}${o.available ? '' : '  (needs a key)'}</option>`).join('');
  const defaultName = () => {
    if (tab === 'web') { try { return new URL(m.$('#ad-url').value.trim()).hostname.replace(/^www\./, '').replace(/^docs\./, ''); } catch (e) { return ''; } }
    if (tab === 'code') { const v = m.$('#ad-src').value.trim().replace(/[\\/]+$/, '').replace(/\.git$/, ''); return v.split(/[\\/:]/).pop() || ''; }
    return '';
  };

  const draw = () => {
    m.$('.m-body').innerHTML = `
      <div class="tabs pill" role="tablist">
        <button role="tab" data-tab="web" aria-selected="${tab === 'web'}">Website</button>
        <button role="tab" data-tab="code" aria-selected="${tab === 'code'}">Code repository</button>
        <button role="tab" data-tab="docs" aria-selected="${tab === 'docs'}">Documents</button></div>
      <div style="padding:18px">
        <div id="ad-pane">${pane()}</div>
        <div class="group" style="margin-top:6px">
          <h3>Where it goes</h3>
          <div class="row wrap">
            <label class="check"><input type="radio" name="ad-target" value="new" ${dest === 'new' ? 'checked' : ''}> New library</label>
            ${hasLibs ? `<label class="check"><input type="radio" name="ad-target" value="existing" ${dest === 'existing' ? 'checked' : ''}> Add to</label><select class="input" id="ad-existing" style="width:auto;min-width:160px">` + S.libs.map((l) => `<option value="${MR.esc(l.id)}" ${l.id === destId ? 'selected' : ''}>${MR.esc(l.name)}</option>`).join('') + '</select>' : ''}
          </div>
          <div class="field" id="ad-newwrap" style="margin:10px 0 0"><span class="label">Library name</span><input class="input" id="ad-name" placeholder="for example JUCE docs"></div>
          <details style="margin-top:12px"><summary class="hint" style="cursor:pointer">Search index options</summary>
            <div class="field" style="margin-top:10px"><span class="label">Embedding model</span>
              <select class="input" id="ad-emb">${embedderOpts()}</select>
              <div class="hint" id="ad-embhint">Built in: free, private and works offline. Very large libraries index much faster with a cloud embedding model when one is available.</div></div>
          </details>
        </div>
        <div id="ad-status"></div></div>`;
    m.$$('[data-tab]').forEach((b) => b.addEventListener('click', () => { tab = b.dataset.tab; draw(); }));
    wire();
    m.$('#ad-go').onclick = go;
    const embSel = m.$('#ad-emb');
    const sync = () => {
      const existing = m.$('input[name=ad-target]:checked').value === 'existing';
      m.$('#ad-newwrap').hidden = existing; embSel.disabled = existing;
      if (existing) m.$('#ad-embhint').textContent = 'An existing library keeps the embedding model it was built with.';
    };
    m.$$('input[name=ad-target]').forEach((r) => r.addEventListener('change', () => { dest = r.value; sync(); }));
    const exSel = m.$('#ad-existing');
    if (exSel) exSel.addEventListener('change', () => { destId = exSel.value; m.$('input[name=ad-target][value=existing]').checked = true; dest = 'existing'; sync(); });
    sync();
  };

  const pane = () => {
    if (tab === 'web') {
      return `<div class="field"><span class="label">Documentation website</span><input class="input" id="ad-url" placeholder="https://docs.example.com/manual/" spellcheck="false">
        <div class="hint">The site is downloaded politely (robots.txt respected) so it can be searched offline.</div></div>
        ${S.presets.length ? '<div class="row wrap" style="margin-bottom:14px"><span class="label">Quick add</span>' + S.presets.map((p) => `<button class="btn small" type="button" data-preset="${p.id}" title="${MR.esc(p.description)}">${MR.esc(p.name)}</button>`).join('') + '</div>' : ''}
        <details><summary class="hint" style="cursor:pointer">Download options</summary>
          <div class="opts" style="margin-top:10px">
            <div class="field"><span class="label">Page limit</span><input class="input" id="ad-max" type="number" value="20000" min="1"></div>
            <div class="field"><span class="label" aria-hidden="true">&nbsp;</span><label class="check ctl"><input type="checkbox" id="ad-pdf" checked> Include PDFs it links to</label>
              <label class="check"><input type="checkbox" id="ad-assets" checked> Keep styles and images (offline viewing)</label></div></div></details>`;
    }
    if (tab === 'code') {
      return `<div class="field"><span class="label">GitHub address or a folder on this computer</span>
          <div class="row"><input class="input" id="ad-src" placeholder="https://github.com/owner/repo   or   C:\\code\\project" spellcheck="false">
          <button class="btn" id="ad-browse" type="button">Browse...</button></div>
          <div class="hint">GitHub repositories are downloaded as a snapshot, no git needed. Other git hosts work if git is installed.</div></div>
        <div class="opts"><div class="field"><span class="label">Branch or tag (optional)</span><input class="input" id="ad-ref" placeholder="default branch"></div>
          <div class="field" title="Only needed for private GitHub repositories. It is used for this download and never saved."><span class="label">GitHub token (private repos)</span><input class="input" id="ad-token" type="password" autocomplete="off" placeholder="github_pat_..."></div></div>
        <div class="callout privacy">When you ask a question, the most relevant excerpts of this code are sent to your chosen provider${S.profile ? ' (' + MR.esc(S.profile.name) + ')' : ''}.
          Files that look like secrets (<span class="mono">.env</span>, keys) are never indexed and credentials in code are replaced with <span class="mono">[REDACTED]</span>.
          Only index code you are comfortable sharing with that provider.</div>
        <details><summary class="hint" style="cursor:pointer">Filters</summary><div class="opts" style="margin-top:10px">
          <div class="field"><span class="label">Only these paths</span><textarea class="input" id="ad-inc" rows="3" placeholder="src/&#10;include/"></textarea></div>
          <div class="field"><span class="label">Skip these paths</span><textarea class="input" id="ad-exc" rows="3" placeholder="tests/&#10;third_party/"></textarea></div></div>
          <div class="hint" style="margin:-4px 0 10px">One pattern per line. Leave both empty to take everything that is not skipped automatically.</div>
          <label class="check"><input type="checkbox" id="ad-gi" checked> Respect .gitignore</label>
          <label class="check" style="margin-top:6px"><input type="checkbox" id="ad-docs" checked> Include README files and docs</label>
          <div class="hint" style="margin-top:6px">Already skipped: node_modules, build folders, vendored code, lock files, minified and generated files, binaries.</div></details>`;
    }
    return `<div class="dropzone" id="ad-drop">Drop PDF, HTML, Markdown, text or zip files here
        <div class="row" style="justify-content:center;margin-top:12px"><button class="btn" id="ad-pick" type="button">Choose files...</button>
          <button class="btn" id="ad-folder" type="button">Choose a folder...</button></div>
        <input type="file" id="ad-file" multiple accept=".pdf,.html,.htm,.md,.markdown,.txt,.rst,.zip" hidden></div>
        <div id="ad-files" class="hint" style="margin-top:10px"></div>`;
  };

  const showFiles = () => { const el = m.$('#ad-files'); if (el) el.innerHTML = files.length ? files.map((f) => `<div class="mono">${MR.esc(f.name)} <span class="faint">${MR.fmt.bytes(f.size)}</span></div>`).join('') : ''; };
  const wire = () => {
    const url = m.$('#ad-url'), name = m.$('#ad-name');
    const fillName = () => { if (!name.dataset.touched) name.value = defaultName(); };
    name.addEventListener('input', () => { name.dataset.touched = '1'; });
    if (url) { url.addEventListener('input', fillName); m.$$('[data-preset]').forEach((b) => b.addEventListener('click', () => { const p = S.presets.find((x) => x.id === b.dataset.preset); url.value = p.url; name.value = p.library_name; name.dataset.touched = '1'; })); }
    const src = m.$('#ad-src');
    if (src) {
      src.addEventListener('input', fillName);
      m.$('#ad-browse').addEventListener('click', async () => { try { const r = await MR.api('/api/pick-folder', { method: 'POST' }); if (r.path) { src.value = r.path; fillName(); } } catch (e) { MR.toast(e.message, 'err'); } });
    }
    const drop = m.$('#ad-drop');
    if (drop) {
      const add = (list) => { files = files.concat(Array.from(list)); showFiles(); };
      ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
      ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
      drop.addEventListener('drop', (e) => add(e.dataTransfer.files));
      m.$('#ad-pick').addEventListener('click', () => m.$('#ad-file').click());
      m.$('#ad-file').addEventListener('change', (e) => add(e.target.files));
      m.$('#ad-folder').addEventListener('click', async () => {
        try { const r = await MR.api('/api/pick-folder', { method: 'POST' }); if (r.path) { files = []; drop.dataset.folder = r.path; m.$('#ad-files').innerHTML = '<div class="mono">' + MR.esc(r.path) + '</div>'; if (!name.dataset.touched) name.value = r.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop(); } }
        catch (e) { MR.toast(e.message, 'err'); }
      });
    }
  };

  const go = async () => {
    const st = m.$('#ad-status'), btn = m.$('#ad-go');
    const target = m.$('input[name=ad-target]:checked').value;
    const lines = (id) => (m.$(id) ? m.$(id).value.split('\n').map((x) => x.trim()).filter(Boolean) : []);
    const fail = (msg) => { st.innerHTML = '<div class="callout error">' + MR.esc(msg) + '</div>'; btn.disabled = false; btn.textContent = 'Add'; };
    try {
      let what;
      if (tab === 'web') { const u = m.$('#ad-url').value.trim(); if (!/^https?:\/\//i.test(u)) return fail('Enter the website address, starting with https://'); what = { url: u }; }
      else if (tab === 'code') { const v = m.$('#ad-src').value.trim(); if (!v) return fail('Enter a GitHub address or choose a folder.'); what = { src: v }; }
      else { const folder = m.$('#ad-drop').dataset.folder; if (!files.length && !folder) return fail('Choose some files or a folder first.'); what = { folder }; }
      btn.disabled = true; btn.textContent = 'Starting...';
      let libId;
      if (target === 'existing') libId = m.$('#ad-existing').value;
      else {
        const name = m.$('#ad-name').value.trim() || defaultName() || 'My library';
        const opt = S.embedders.find((o) => o.id === m.$('#ad-emb').value) || S.embedders[0];
        const spec = { provider: opt.provider, model: opt.model };
        if (opt.dimensions) spec.dimensions = opt.dimensions;
        if (tab === 'code' && opt.provider === 'gemini') spec.flavor = 'code';
        const lib = await MR.api('/api/libraries', { method: 'POST', body: { name, embedder: spec } });
        libId = lib.id;
      }
      let job;
      const base = '/api/libraries/' + encodeURIComponent(libId);
      if (tab === 'web') {
        job = await MR.api(base + '/ingest/crawl', { method: 'POST', body: { url: what.url, max_pages: +m.$('#ad-max').value || 20000,
          include_pdfs: m.$('#ad-pdf').checked, download_assets: m.$('#ad-assets').checked } });
      } else if (tab === 'code') {
        const isPath = /^([a-zA-Z]:[\\/]|[\\/]{1,2}|~)/.test(what.src);
        job = await MR.api(base + '/ingest/repo', { method: 'POST', body: { [isPath ? 'path' : 'url']: what.src, ref: m.$('#ad-ref').value.trim(),
          token: m.$('#ad-token').value.trim(), include: lines('#ad-inc'), exclude: lines('#ad-exc'),
          use_gitignore: m.$('#ad-gi').checked, include_docs: m.$('#ad-docs').checked } });
      } else if (what.folder) {
        job = await MR.api(base + '/ingest/folder', { method: 'POST', body: { path: what.folder } });
      } else {
        for (let i = 0; i < files.length; i++) {
          btn.textContent = `Uploading ${i + 1}/${files.length}...`;
          await MR.api(base + '/upload?name=' + encodeURIComponent(files[i].name), { method: 'PUT', body: files[i], raw: true });
        }
        job = await MR.api(base + '/ingest/uploads', { method: 'POST' });
      }
      m.close();
      MR.S.selected.add(libId); MR.S.knownLibs.add(libId); MR.persistTicks();
      MR.toast('Started: ' + job.title, 'ok');
      await MR.refreshLibs(); await MR.pollJobs();
    } catch (e) { fail(e.message); }
  };
  draw();
};

MR.addPreset = async function (p) {
  try {
    const r = await MR.api('/api/presets/' + encodeURIComponent(p.id) + '/add', { method: 'POST' });
    MR.S.selected.add(r.library.id); MR.S.knownLibs.add(r.library.id); MR.persistTicks();
    MR.toast('Downloading ' + p.name + '. You can ask questions as soon as passages appear.', 'ok', 6000);
    await MR.refreshLibs(); await MR.pollJobs();
  } catch (e) { MR.toast(e.message, 'err', 8000); }
};

/* ------------------------------------------------------------------ settings */
MR.openSettings = function (tab = 'models') {
  const S = MR.S;
  const m = MR.modal({ title: 'Settings', body: '', foot: '<button class="btn" data-close2 type="button">Close</button>' });
  m.$('.m-body').style.padding = '0';
  m.$('[data-close2]').addEventListener('click', () => m.close());
  const tabs = [['models', 'Models'], ['search', 'Search'], ['embeddings', 'Embeddings'], ['appearance', 'Appearance'], ['privacy', 'Privacy & data']];
  const draw = async () => {
    m.$('.m-body').innerHTML = `<div class="tabs" role="tablist">${tabs.map((t) => `<button role="tab" data-tab="${t[0]}" aria-selected="${t[0] === tab}">${t[1]}</button>`).join('')}</div><div style="padding:18px" id="st-pane"></div>`;
    m.$$('[data-tab]').forEach((b) => b.addEventListener('click', () => { tab = b.dataset.tab; draw(); }));
    await ({ models: models, search, embeddings, appearance, privacy }[tab] || models)();
  };

  const appearance = async () => {
    const seg = (id, cur, opts) => `<div class="seg" id="${id}" role="group">${opts.map(([v, l]) => `<button type="button" data-v="${v}" aria-pressed="${v === cur}">${l}</button>`).join('')}</div>`;
    m.$('#st-pane').innerHTML = `
      <div class="field"><span class="label">Theme</span>
        ${seg('ap-theme', MR.theme(), [['light', 'Light: warm paper'], ['dark', 'Dark: minimal (default)']])}
        <div class="hint">Dark is what you get until you choose otherwise. The switch in the top bar does the same thing.</div></div>
      <div class="field"><span class="label">Font for answers and titles</span>
        ${seg('ap-read', MR.read(), [['sans', 'Sans (default)'], ['serif', 'Serif']])}
        <div class="hint">Code and file names always use a monospace font.</div></div>`;
    const bind = (id, fn) => MR.$$('#' + id + ' button', m.$('#st-pane')).forEach((b) => b.addEventListener('click', () => {
      fn(b.dataset.v);
      MR.$$('#' + id + ' button', m.$('#st-pane')).forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    }));
    bind('ap-theme', MR.setTheme);
    bind('ap-read', MR.setRead);
  };

  const models = async () => {
    const pane = m.$('#st-pane');
    const rows = S.profiles.filter((p) => p.has_key).map((p) => {
      if (p.provider === 'local' && MR.local) return MR.local.settingsGroup(p, !!(S.profile && p.id === S.profile.id));   // a model on this computer (js/local.js)
      const anth = p.kind === 'anthropic';
      return `<div class="group" data-prof="${MR.esc(p.id)}">
        <h3>${MR.esc(p.name)}${S.profile && p.id === S.profile.id ? ' &middot; active' : ''}</h3>
        <div class="opts">
          <div class="field"><span class="label">Model</span><select class="input" data-f="model"><option value="${MR.esc(p.model)}">${MR.esc(p.model || 'loading...')}</option></select></div>
          ${anth ? `<div class="field"><span class="label">Effort</span><select class="input" data-f="effort">
              ${[['', 'model default'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['xhigh', 'xhigh'], ['max', 'max']].map((o) => `<option value="${o[0]}" ${(p.effort || '') === o[0] ? 'selected' : ''}>${o[1]}</option>`).join('')}</select></div>`
            : `<div class="field"><span class="label">Reasoning</span><select class="input" data-f="reasoning">
              ${[['', 'model default'], ['off', 'off (where supported)'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['max', 'max (where supported)']].map((o) => `<option value="${o[0]}" ${(p.reasoning || '') === o[0] ? 'selected' : ''}>${o[1]}</option>`).join('')}</select></div>`}
          <div class="field" title="Leave empty to use the size the provider reports. Enter a number of tokens to override it."><span class="label">Context window (tokens)</span><input class="input" data-f="context_tokens" type="number" min="1024" placeholder="automatic" value="${p.context_tokens || ''}"></div>
          ${anth ? `<div class="field"><span class="label" aria-hidden="true">&nbsp;</span><label class="check ctl"><input type="checkbox" data-f="show_thinking" ${p.show_thinking ? 'checked' : ''}> Show a summary of the model's reasoning</label></div>` : ''}
        </div>
        <div class="row" style="margin-top:4px"><button class="btn small" data-act="activate" type="button">${S.profile && p.id === S.profile.id ? 'Active' : 'Use this one'}</button>
          <button class="btn small danger" data-act="remove" type="button">Disconnect</button><span class="hint grow" data-msg></span></div></div>`;
    }).join('');
    pane.innerHTML = (rows || '<div class="callout">No model is connected yet.</div>') + '<button class="btn" id="st-add" type="button">+ Connect a provider</button>';
    m.$('#st-add').addEventListener('click', () => { m.close(); MR.openConnect(); });
    MR.$$('[data-local-prof]', pane).forEach((g) => MR.local.wireSettings(g, draw));
    MR.$$('[data-prof]', pane).forEach(async (g) => {
      const id = g.dataset.prof, msg = MR.$('[data-msg]', g);
      try {
        const r = await MR.api('/api/profiles/' + encodeURIComponent(id) + '/models');
        const cur = S.profiles.find((p) => p.id === id).model;
        MR.$('[data-f=model]', g).innerHTML = r.models.map((x) => `<option value="${MR.esc(x.id)}" ${x.id === cur ? 'selected' : ''}>${MR.esc(x.name && x.name !== x.id ? x.name + '  (' + x.id + ')' : x.id)}</option>`).join('') ||
          `<option>${MR.esc(cur)}</option>`;
      } catch (e) { msg.textContent = e.message; msg.className = 'hint grow err'; }
      MR.$$('[data-f]', g).forEach((inp) => inp.addEventListener('change', async () => {
        const f = inp.dataset.f;
        const v = inp.type === 'checkbox' ? inp.checked : inp.type === 'number' ? (inp.value ? +inp.value : null) : inp.value || null;
        try { await MR.api('/api/profiles/' + encodeURIComponent(id), { method: 'PATCH', body: { [f]: v } }); msg.textContent = 'saved'; msg.className = 'hint grow ok'; await MR.reloadSettings(); }
        catch (e) { msg.textContent = e.message; msg.className = 'hint grow err'; }
      }));
      MR.$('[data-act=activate]', g).addEventListener('click', async () => { await MR.api('/api/profiles/' + encodeURIComponent(id) + '/activate', { method: 'POST' }); await MR.reloadSettings(); draw(); });
      MR.$('[data-act=remove]', g).addEventListener('click', async () => {
        if (await MR.confirm('Disconnect', 'Remove the saved key for this provider from this computer?', 'Disconnect')) { await MR.api('/api/profiles/' + encodeURIComponent(id), { method: 'DELETE' }); await MR.reloadSettings(); draw(); }
      });
    });
  };

  const search = async () => {
    const r = S.settings.retrieval;
    const num = (k, label, min, max, hint) => `<div class="field"><span class="label">${label}</span><input class="input" data-r="${k}" type="number" min="${min}" max="${max}" value="${r[k]}"><div class="hint">${hint}</div></div>`;
    const chk = (k, label, hint) => `<div class="field"><label class="check"><input type="checkbox" data-r="${k}" ${r[k] ? 'checked' : ''}> ${label}</label><div class="hint">${hint}</div></div>`;
    m.$('#st-pane').innerHTML = `<div class="opts two">
      ${num('top_k', 'Passages per question', 1, 40, 'How many excerpts are handed to the model.')}
      ${num('max_per_doc', 'At most per page or file', 1, 10, 'Keeps one long page from crowding out the rest.')}
      ${num('candidates', 'Candidates per method', 10, 200, 'Meaning search and keyword search each contribute this many.')}
      ${num('max_excerpt_tokens', 'Excerpt budget (tokens)', 1000, 400000, 'Upper limit; also capped by the model\'s window.')}</div>
      ${chk('query_rewrite', 'Rewrite follow-up questions', 'Turns "and its parameters?" into a standalone search (one short extra model call).')}
      ${chk('multi_query', 'Search with extra phrasings', 'Asks the model for alternative wordings. Better recall, one more call.')}
      ${chk('rerank', 'Rerank with a cross-encoder', 'More accurate ordering; the small model it needs ships with Perplexify.')}
      <div class="row" style="margin-bottom:18px"><button class="btn primary" id="st-save" type="button">Save</button><span class="hint" id="st-msg"></span></div>
      <div class="group"><h3>Web research (the Research switch beside the question box)</h3>
        <p class="hint" style="margin:0 0 10px">The connected model decides what to search for; Perplexify runs the searches, reads the best pages and hands them to the model to cite.
          A search service key makes this dependable. Without one, DuckDuckGo is tried, which often refuses automated searches.</p>
        ${[['tavily', 'Tavily', 'tvly-...', 'https://app.tavily.com/home', 'built for AI research, 1,000 free searches a month'],
           ['brave', 'Brave Search', 'BSA...', 'https://api-dashboard.search.brave.com/app/keys', 'independent index, free tier']].map(([id, name, ph, url, note]) => {
          const saved = S.settings.embedding_keys && S.settings.embedding_keys[id];
          return `<div class="field"><span class="label">${name} key ${saved ? '<span class="ok">saved</span>' : ''}</span>
            <div class="row"><input class="input" data-wk="${id}" type="password" autocomplete="off" placeholder="${saved ? 'saved. Paste a new key to replace it' : ph}">
              <button class="btn" data-wsave="${id}" type="button">Save</button>${saved ? `<button class="btn quiet" data-wdel="${id}" type="button">Remove</button>` : ''}</div>
            <div class="hint"><a href="${url}" target="_blank" rel="noopener noreferrer">Get a key &#8599;</a> &middot; ${note}</div></div>`;
        }).join('')}
        <div class="row"><button class="btn" id="st-wtest" type="button">Test web search</button><span class="hint" id="st-wmsg"></span></div></div>`;
    const wsay = (t, c) => { m.$('#st-wmsg').textContent = t; m.$('#st-wmsg').className = 'hint ' + c; };
    const wkey = async (id, key) => {
      try { await MR.api('/api/search-keys', { method: 'POST', body: { provider: id, key } }); await MR.reloadSettings(); await search(); wsay(key ? 'Key saved.' : 'Key removed.', 'ok'); }
      catch (x) { wsay(x.message, 'err'); }
    };
    MR.$$('[data-wsave]', m.$('#st-pane')).forEach((b) => b.addEventListener('click', () => {
      const v = m.$(`[data-wk="${b.dataset.wsave}"]`).value.trim();
      if (!v) { wsay('Paste the key first.', 'err'); return; }
      wkey(b.dataset.wsave, v);
    }));
    MR.$$('[data-wdel]', m.$('#st-pane')).forEach((b) => b.addEventListener('click', () => wkey(b.dataset.wdel, '')));
    m.$('#st-wtest').addEventListener('click', async () => {
      wsay('Searching...', 'muted');
      try { const r = await MR.api('/api/search-keys/test', { method: 'POST' }); wsay(`Works: ${r.results} ${r.results === 1 ? 'result' : 'results'} from ${r.engine}.`, 'ok'); }
      catch (x) { wsay(x.message, 'err'); }
    });
    m.$('#st-save').addEventListener('click', async () => {
      const body = {};
      MR.$$('[data-r]', m.$('#st-pane')).forEach((i) => { body[i.dataset.r] = i.type === 'checkbox' ? i.checked : +i.value; });
      try { S.settings = await MR.api('/api/settings', { method: 'PUT', body: { retrieval: body } }); m.$('#st-msg').textContent = 'Saved.'; m.$('#st-msg').className = 'hint ok'; }
      catch (e) { m.$('#st-msg').textContent = e.message; m.$('#st-msg').className = 'hint err'; }
    });
  };

  const embeddings = async () => {
    const e = await MR.api('/api/embedders');
    S.embedders = e.options;
    const cur = e.default;
    const isCur = (o) => o.provider === cur.provider && o.model === cur.model;
    m.$('#st-pane').innerHTML = `<div class="callout">Embeddings turn text into numbers so search can match meaning. The default runs inside this app: no key, no cost, works offline.
      Claude and DeepSeek have no embeddings service of their own. This choice applies to <b>new</b> libraries; existing ones keep theirs (change it from the library menu).</div>
      <div class="group"><h3>Default for new libraries</h3>${e.options.map((o) => `<div class="pline"><label class="check grow"><input type="radio" name="emb" value="${o.id}" ${isCur(o) ? 'checked' : ''} ${o.available ? '' : 'disabled'}>
        <span><b>${MR.esc(o.name)}</b><br><span class="hint">${MR.esc(o.note)}${o.available ? '' : ' (not available until a key is added)'}</span></span></label></div>`).join('')}</div>
      <div class="group"><h3>Voyage AI key (for Claude users who want cloud embeddings)</h3>
        <div class="row"><input class="input" id="st-voyage" type="password" autocomplete="off" placeholder="${S.settings.embedding_keys && S.settings.embedding_keys.voyage ? 'saved. Paste a new key to replace it' : 'pa-...'}">
          <button class="btn" id="st-vsave" type="button">Save key</button></div>
        <div class="hint" style="margin-top:6px"><a href="https://dashboard.voyageai.com/organization/api-keys" target="_blank" rel="noopener noreferrer">Create a key &#8599;</a> &middot; Gemini and OpenAI embeddings reuse the Gemini or ChatGPT key you connected above.</div></div>
      <div class="row"><button class="btn primary" id="st-save" type="button">Save default</button><button class="btn" id="st-test" type="button">Test selected</button><span class="hint" id="st-msg"></span></div>`;
    const picked = () => e.options.find((o) => o.id === (m.$('input[name=emb]:checked') || {}).value);
    const specOf = (o) => { const s = { provider: o.provider, model: o.model }; if (o.dimensions) s.dimensions = o.dimensions; return s; };
    const say = (t, c) => { m.$('#st-msg').textContent = t; m.$('#st-msg').className = 'hint ' + c; };
    m.$('#st-save').addEventListener('click', async () => { const o = picked(); if (!o) return say('Pick one first.', 'err'); try { await MR.api('/api/settings', { method: 'PUT', body: { embedder: specOf(o) } }); say('Saved.', 'ok'); } catch (x) { say(x.message, 'err'); } });
    m.$('#st-test').addEventListener('click', async () => { const o = picked(); if (!o) return; say('Testing...', 'muted'); try { const r = await MR.api('/api/embedders/test', { method: 'POST', body: { embedder: specOf(o) } }); say('Works. Vectors have ' + r.dim + ' dimensions.', 'ok'); } catch (x) { say(x.message, 'err'); } });
    m.$('#st-vsave').addEventListener('click', async () => { try { await MR.api('/api/embedding-keys', { method: 'POST', body: { provider: 'voyage', key: m.$('#st-voyage').value.trim() } }); say('Voyage key saved.', 'ok'); await embeddings(); } catch (x) { say(x.message, 'err'); } });
  };

  const privacy = async () => {
    m.$('#st-pane').innerHTML = `<div class="group"><h3>What leaves this computer</h3>
        <p style="margin:0 0 8px">Your libraries, the downloaded sites and the search index stay on this computer. When you ask a question, three things are sent to the model provider you connected:
        your question, the few most relevant excerpts (documentation or source code), and the recent conversation. Nothing else is uploaded.
        With a local model (llama.cpp) even those stay here: the model runs on this computer and nothing is sent anywhere.</p>
        <p style="margin:0 0 8px">Your chat history, saved searches and bookmarks are stored only in this computer's data folder. They are never sent to a provider; you can clear history in the History tab.</p>
        <p style="margin:0">With a cloud embedding model selected for a library, its passages are also sent to that embedding provider while it is indexed. The built-in model sends nothing.</p></div>
      <div class="group"><h3>Keys</h3><p style="margin:0">API keys are encrypted with ${S.platform === 'windows' ? 'Windows data protection (bound to your Windows account)' : S.platform === 'mac' ? 'the macOS Keychain' : 'a local file readable only by you'} and are never shown again in this window.</p></div>
      <div class="group"><h3>This app</h3><div class="row wrap">
        <button class="btn" id="st-data" type="button">Open data folder</button>
        <button class="btn danger" id="st-quit" type="button">Quit Perplexify</button></div>
        <div class="hint" style="margin-top:8px">Version ${MR.esc(S.version)}. ${S.shell === 'window' ? 'Closing the window quits Perplexify.'
          : `Closing this browser tab does not stop the app; it keeps running in the ${S.platform === 'mac' ? 'menu bar' : 'tray'}.`}</div></div>`;
    m.$('#st-data').addEventListener('click', () => MR.api('/api/open-data-folder', { method: 'POST' }).catch((e) => MR.toast(e.message, 'err')));
    m.$('#st-quit').addEventListener('click', async () => {
      if (!(await MR.confirm('Quit Perplexify', 'Stop the app? Running imports will be cancelled.', 'Quit'))) return;
      try {
        await MR.api('/api/shutdown', { method: 'POST' });
        if (S.shell !== 'window') document.body.innerHTML = '<p style="padding:40px;font-family:var(--mono)">Perplexify has stopped. You can close this tab.</p>';  // in its own window the window just closes
      } catch (e) { MR.toast(e.message, 'err'); }
    });
  };
  draw();
};

MR.openHelp = function () {
  MR.modal({ title: 'Help', narrow: true, body: `<p style="margin-top:0">Type a question and press <span class="kbd">Enter</span>. Citations like <span class="mono">[1]</span> open the matching source card;
    <b>view source</b> shows the exact lines.</p>
    <dl class="kv"><dt>/new</dt><dd>start a new conversation</dd><dt>/add</dt><dd>add a website, repository or documents</dd><dt>/model</dt><dd>change the model</dd>
    <dt>/theme</dt><dd>switch light and dark</dd><dt>Esc</dt><dd>stop a running answer</dd><dt>Up arrow</dt><dd>bring back your last question</dd></dl>
    <p class="hint">Tip: type an identifier such as <span class="mono">processBlock</span> to jump straight to where it is defined.</p>` });
};

/* ------------------------------------------------------------------ library menu */
MR.libMenu = function (id, anchor) {
  MR.closePopover();
  const lib = MR.S.libs.find((l) => l.id === id);
  if (!lib) return;
  const busy = MR.S.busy.has(id);
  const items = [
    ['add', 'Add a source to this library...', !busy],
    ['refresh', 'Refresh from sources', !busy && lib.sources && lib.sources.length],
    ['resume', 'Finish embedding', !busy && lib.pending > 0],
    ['embedder', 'Change embedding model...', !busy && lib.chunks > 0],
    ['details', 'Details', true], ['rename', 'Rename...', true], ['---'], ['delete', 'Delete...', true],
  ].filter((i) => i[0] === '---' || i[2]);
  const menu = MR.el('<div class="menu">' + items.map((i) => (i[0] === '---' ? '<hr>' : `<button type="button" data-a="${i[0]}">${i[1]}</button>`)).join('') + '</div>');
  document.body.appendChild(menu);
  MR.placeNear(menu, anchor);
  menu.addEventListener('click', (e) => { const a = e.target.dataset.a; if (a) { MR.closePopover(); MR.libAction(id, a); } });
  document.addEventListener('mousedown', MR._outside, true);
};

MR.libAction = async function (id, action) {
  const lib = MR.S.libs.find((l) => l.id === id);
  const base = '/api/libraries/' + encodeURIComponent(id);
  try {
    if (action === 'add') {
      MR.openAdd('web', id);
    } else if (action === 'refresh' || action === 'resume') {
      await MR.api(base + '/' + action, { method: 'POST' });
      MR.toast(action === 'refresh' ? 'Refreshing ' + lib.name : 'Resuming embedding', 'ok');
      await MR.refreshLibs(); await MR.pollJobs();
    } else if (action === 'rename') {
      const v = await MR.ask1('Rename library', 'Name', lib.name);
      if (v) { await MR.api(base, { method: 'PATCH', body: { name: v } }); await MR.refreshLibs(); }
    } else if (action === 'delete') {
      const busy = MR.S.busy.has(id);       // still downloading or indexing: that is stopped first
      const text = busy
        ? `"${lib.name}" is still being downloaded or indexed. Stop that and delete the library, with everything downloaded for it? This cannot be undone.`
        : `Delete "${lib.name}" and everything downloaded for it? This cannot be undone.`;
      if (await MR.confirm('Delete library', text, 'Delete')) {
        await MR.api(base + (busy ? '?stop_jobs=true' : ''), { method: 'DELETE' });
        MR.S.selected.delete(id);
        await MR.refreshLibs(); await MR.pollJobs();
        MR.toast('Deleted ' + lib.name + '.', 'ok');
      }
    } else if (action === 'embedder') {
      const opts = MR.S.embedders.map((o) => `<option value="${o.id}" ${o.available ? '' : 'disabled'}>${MR.esc(o.name)}${o.available ? '' : ' (needs a key)'}</option>`).join('');
      const m = MR.modal({ title: 'Change embedding model', narrow: true, body: `<p style="margin-top:0">Currently <b>${MR.esc(lib.embedder.model)}</b>. All ${MR.fmt.int(lib.chunks)} passages are embedded again with the new model; nothing is downloaded again.</p>
        <div class="field"><span class="label">New model</span><select class="input" id="ce">${opts}</select></div><div id="ce-st"></div>`,
        foot: '<button class="btn" data-no type="button">Cancel</button><button class="btn primary" id="ce-go" type="button">Re-embed</button>' });
      m.$('[data-no]').addEventListener('click', () => m.close());
      m.$('#ce-go').addEventListener('click', async () => {
        const o = MR.S.embedders.find((x) => x.id === m.$('#ce').value);
        const spec = { provider: o.provider, model: o.model }; if (o.dimensions) spec.dimensions = o.dimensions;
        try { await MR.api(base + '/reembed', { method: 'POST', body: { embedder: spec } }); m.close(); await MR.refreshLibs(); await MR.pollJobs(); }
        catch (e) { m.$('#ce-st').innerHTML = '<div class="callout error">' + MR.esc(e.message) + '</div>'; }
      });
    } else if (action === 'details') {
      const d = await MR.api(base);
      const src = (d.sources || []).map((s) => `<div class="mono">${MR.esc(s.type === 'crawl' ? 'website ' + s.url : s.type === 'repo' ? 'repository ' + (s.url || s.path) : (s.type + ' ' + (s.path || '')))} <span class="faint">${MR.fmt.ago(s.last_run)}</span></div>`).join('') || '<span class="faint">none recorded</span>';
      MR.modal({ title: d.name, body: `<dl class="kv">
        <dt>documents</dt><dd>${MR.fmt.int(d.documents)}</dd><dt>passages</dt><dd>${MR.fmt.int(d.chunks)} (${MR.fmt.int(d.embedded)} embedded)</dd>
        <dt>kinds</dt><dd>${MR.esc(Object.entries(d.kinds || {}).map(([k, v]) => k + ' ' + v).join(', ') || '-')}</dd>
        <dt>embedding</dt><dd>${MR.esc(d.embedder.provider + ': ' + d.embedder.model)}${d.embedder.dim ? ' (' + d.embedder.dim + ' dims)' : ''}</dd>
        <dt>approx. tokens</dt><dd>~${MR.fmt.int(d.tokens)} <span class="faint">(all passages together)</span></dd>
        <dt>index size</dt><dd>${MR.fmt.bytes(d.db_bytes)}</dd><dt>downloaded files</dt><dd>${MR.fmt.bytes(d.source_bytes)}</dd>
        <dt>last indexed</dt><dd>${MR.fmt.ago(d.last_indexed)}</dd><dt>sources</dt><dd>${src}</dd></dl>` });
    }
  } catch (e) { MR.toast(e.message, 'err', 7000); }
};
