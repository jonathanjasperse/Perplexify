'use strict';
/* Local models (llama.cpp): choosing, downloading and connecting a model that runs on this computer.

   One panel serves two places: the "Local model" tile of Connect a model (mode 'connect': pick, then "Use this model") and the
   Local model group of Settings > Models (mode 'settings': every change is saved at once). Everything it shows comes from
   /api/local/... (perplexify/local_api.py): the engine, the model files, the curated list, memory, the running model. */

MR.local = (() => {
  const L = {};
  const api = (path, opts) => MR.api('/api/local' + path, opts);
  const set = (el, html) => { if (el && el._h !== html) { el._h = html; el.innerHTML = html; } };   // only touch the page when something changed

  /* sizes like the Hugging Face page shows them (decimal), so a file can be compared with its download page */
  const sz = (n) => (n >= 1e9 ? (n / 1e9).toFixed(n >= 1e10 ? 0 : 2) + ' GB' : Math.round(n / 1e6) + ' MB');
  const gb = (n) => (n / 1073741824).toFixed(n >= 10 * 1073741824 ? 0 : 1) + ' GB';
  const FIT = {
    too_big: 'Probably too big for this computer.',
    tight: 'Memory is short right now: close other programs first.',
  };

  /* ------------------------------------------------------------------ pieces of the panel (pure: data in, HTML out) */
  L.html = {
    engine(s) {
      if (!s || (s.engine && s.engine.found)) return '';
      const e = s.engine || {};
      const job = s.engine_job;
      const bar = job ? progress(job, 'Downloading the engine') : '';
      return `<div class="callout error">${MR.esc(e.message || 'The local model engine (llama.cpp) is not installed.')}
        ${e.can_download === false ? '' : `<div class="row" style="margin-top:8px"><button class="btn small" type="button" data-a="engine" ${job ? 'disabled' : ''}>Download engine</button>
          <span class="hint">about ${sz((e.pinned && e.pinned.size) || 0)}, from the llama.cpp project on GitHub</span></div>`}${bar}</div>`;
    },

    installed(m, sel, active) {
      const list = (m && m.installed) || [];
      if (!list.length) return '<div class="hint">No model yet. Download one below, or choose a .gguf file you already have.</div>';
      return list.map((x) => {
        const usable = x.exists && x.valid;
        const bits = [x.file, x.exists ? sz(x.size) : 'file not found', x.architecture, x.linked ? 'chosen file, used where it is' : ''].filter(Boolean);
        const warn = !usable ? `<br><span class="err">${MR.esc(x.error || 'This file cannot be used.')}</span>`
          : x.fit && x.fit.level !== 'ok' ? `<br><span class="warn">${MR.esc(x.fit.message || FIT[x.fit.level])}</span>` : '';
        return `<div class="pline"><label class="check grow"><input type="radio" name="lc-model" value="${MR.esc(x.id)}" ${x.id === sel ? 'checked' : ''} ${usable ? '' : 'disabled'}>
          <span><b>${MR.esc(x.title)}</b>${x.id === active ? ' <span class="tag">in use</span>' : ''}<br><span class="hint">${MR.esc(bits.join(' · '))}</span>${warn}</span></label>
          <button class="btn small quiet" type="button" data-a="remove" data-id="${MR.esc(x.id)}" title="${x.linked ? 'Forget this file (the file itself is not deleted)' : 'Delete the file from this computer'}">${x.linked ? 'Remove' : 'Delete'}</button></div>`;
      }).join('');
    },

    catalog(m) {
      return ((m && m.catalog) || []).map((c) => {
        const tags = (c.tags || []).map((t) => ` <span class="tag">${MR.esc(t)}</span>`).join('');
        const warn = c.fit && c.fit.level !== 'ok' && c.state !== 'installed' ? `<br><span class="warn">${MR.esc(c.fit.message || FIT[c.fit.level])}</span>` : '';
        let act;
        if (c.state === 'installed') act = '<span class="tag">downloaded</span>';
        else if (c.state === 'downloading' && c.job) act = progress(c.job, '');
        else act = `<button class="btn small" type="button" data-a="download" data-id="${MR.esc(c.id)}">${c.state === 'partial' ? 'Resume ' + Math.round((100 * c.bytes) / c.size) + '%' : 'Download'}</button>`;
        return `<div class="pline"><span class="grow"><b>${MR.esc(c.name)}</b>${tags}<br><span class="hint">${MR.esc(c.summary)}</span><br>
          <span class="faint">${sz(c.size)} · needs about ${c.ram_gb} GB of memory · ${MR.esc(c.license)}</span>${warn}</span><span class="lc-act">${act}</span></div>`;
      }).join('');
    },

    options(s, m, sel, ctx, gpu) {
      const entry = ((m && m.installed) || []).find((x) => x.id === sel);
      const auto = entry && entry.auto_context ? ' (' + MR.fmt.int(entry.auto_context) + ')' : '';
      const sizes = [['', 'Automatic' + auto]].concat((s && s.context_choices || [4096, 8192, 16384, 32768]).map((n) => [String(n), MR.fmt.int(n)]));
      const hw = (s && s.hardware) || {};
      const train = entry && entry.context_length ? ` This model was made for up to ${MR.fmt.int(entry.context_length)}.` : '';
      const memory = hw.ram_total ? ` This computer has ${gb(hw.ram_total)} of memory, ${gb(hw.ram_available || 0)} free.` : '';
      return `<div class="opts two">
        <div class="field"><span class="label">Context size (tokens)</span>
          <select class="input" data-lc="ctx">${sizes.map((o) => `<option value="${o[0]}" ${o[0] === String(ctx || '') ? 'selected' : ''}>${o[1]}</option>`).join('')}</select>
          <div class="hint">How much text the model reads per question. Bigger fits more passages but needs more memory and starts each answer later.${train}${memory}</div></div>
        <div class="field"><span class="label">GPU use</span>
          <select class="input" data-lc="gpu"><option value="auto" ${gpu !== 'off' ? 'selected' : ''}>Automatic</option><option value="off" ${gpu === 'off' ? 'selected' : ''}>Off (CPU only)</option></select>
          <div class="hint">${MR.esc((hw.gpu && hw.gpu.note) || '')}</div></div></div>`;
    },

    state(s) {
      if (!s) return '<div class="hint">Looking at this computer...</div>';
      const r = s.running || {}, e = s.engine || {};
      let line = '';
      if (!e.found) line = '';
      else if (r.state === 'loading') line = `<span class="spin"></span> Loading ${MR.esc(r.model || 'the model')}... ${r.loading_seconds || 0} s. A large model can take up to a minute.`;
      else if (r.state === 'ready') {
        const g = r.gpu || {};
        const where = g.fell_back ? 'The graphics driver failed, so it runs on the CPU.'
          : g.active ? `Graphics card: ${MR.esc(g.device || 'in use')}, ${g.layers} of ${g.layers_total} layers.`
          : g.mode === 'off' ? 'Graphics card off: running on the CPU.' : 'Running on the CPU.';
        line = `<span class="ok">Ready.</span> ${MR.esc(r.model)}, ${MR.fmt.int(r.n_ctx)} tokens of context, loaded in ${r.load_seconds} s. ${where}`;
      } else if (r.state === 'error') line = `<div class="callout error" style="margin:0"><b>The model did not load.</b> ${MR.esc(r.error || '')}</div>`;
      else if (s.profile) line = 'Not loaded. It loads when you ask your first question (or press Load now).';
      else line = 'Choose or download a model to start.';
      const where = { installed: 'comes with the app', data: 'downloaded', environment: 'from PERPLEXIFY_LLAMA_DIR', override: 'your own program' }[e.source];
      const v = e.found && e.version ? `<div class="faint" style="margin-top:6px">Engine: llama.cpp ${MR.esc(e.version)}${where ? ', ' + where : ''}</div>` : '';
      return (line ? `<div class="lc-line">${line}</div>` : '') + v;
    },
  };

  /* a download (or any job) in progress: a bar, the figures, a Stop link */
  function progress(job, title) {
    const done = job.total ? Math.min(100, (100 * job.current) / job.total) : 0;
    const verifying = job.phase === 'verify';
    const figures = verifying ? 'Checking the file...' : job.phase === 'unpack' ? 'Unpacking...'
      : job.total ? `${sz(job.current)} of ${sz(job.total)}` + (job.message ? ' · ' + MR.esc(job.message) : '') : MR.esc(job.message || 'Starting...');
    return `<div class="lc-prog">${title ? `<div class="hint">${MR.esc(title)}</div>` : ''}<div class="bar ${job.total ? '' : 'indeterminate'}"><i style="width:${done}%"></i></div>
      <div class="hint">${figures} <a href="#" data-a="stopjob" data-job="${MR.esc(job.id)}">stop</a></div></div>`;
  }

  /* ------------------------------------------------------------------ the panel itself */
  L.mount = function (host, opts = {}) {
    const P = {
      host, mode: opts.mode || 'connect', status: null, models: null, sel: '', ctx: '', gpu: 'auto', started: false, alive: true,
      timer: 0, tick: 0, onSelect: opts.onSelect || (() => {}), onSaved: opts.onSaved || (() => {}),
    };
    host.innerHTML = `<div class="lc">
      <div data-lc="engine"></div>
      <div class="group"><h3>Models on this computer</h3><div data-lc="installed"></div>
        <div class="row lc-pick"><input class="input" data-lc="path" placeholder="or paste the path of a .gguf file and press Enter" spellcheck="false" autocomplete="off">
          <button class="btn" type="button" data-a="browse">Choose a .gguf file...</button></div>
        <div class="hint" style="margin-top:6px">A file you choose stays where it is: it is used from there and never copied.</div></div>
      <div class="group"><h3>Download a model</h3><div data-lc="catalog"></div>
        <div class="hint" style="margin-top:8px">Downloads come from Hugging Face, are checked against their published checksum and can be stopped and resumed.</div></div>
      <div data-lc="options"></div>
      ${P.mode === 'settings' ? `<div class="row wrap lc-tools"><button class="btn small" type="button" data-a="start">Load now</button><button class="btn small" type="button" data-a="test">Test</button>
        <button class="btn small" type="button" data-a="stop">Unload</button><span class="hint grow" data-lc="msg"></span></div>` : '<div class="hint" data-lc="msg"></div>'}
      <div data-lc="state" style="margin-top:10px"></div></div>`;
    const q = (n) => host.querySelector(`[data-lc="${n}"]`);

    const selected = () => ((P.models && P.models.installed) || []).find((x) => x.id === P.sel);
    const say = (text, cls = '') => {                               // the small line beside the buttons: "Saved.", an error, a test result
      const m = q('msg');
      if (m) { m.className = ['hint', m.classList.contains('grow') ? 'grow' : '', cls].filter(Boolean).join(' '); m.textContent = text; }
    };

    const draw = () => {
      const active = P.status && P.status.profile && P.status.profile.active ? P.status.profile.model : null;
      set(q('engine'), L.html.engine(P.status));
      set(q('installed'), L.html.installed(P.models, P.sel, active));
      set(q('catalog'), L.html.catalog(P.models));
      set(q('options'), L.html.options(P.status, P.models, P.sel, P.ctx, P.gpu));
      set(q('state'), L.html.state(P.status));
      const run = (P.status && P.status.running && P.status.running.state) || 'stopped';
      const found = P.status && P.status.engine && P.status.engine.found;
      const has = !!(P.status && P.status.profile && P.status.profile.model_found);
      const btn = (a) => host.querySelector(`[data-a="${a}"]`);
      if (btn('start')) btn('start').disabled = !(found && has) || run === 'loading' || run === 'ready';
      if (btn('test')) btn('test').disabled = !(found && has) || run === 'loading';
      if (btn('stop')) btn('stop').disabled = !(run === 'loading' || run === 'ready');
    };

    const fast = () => {
      const s = P.status, m = P.models;
      return !!(s && ((s.running && s.running.state === 'loading') || s.engine_job)) || !!(m && m.catalog.some((c) => c.state === 'downloading'));
    };

    const refresh = async () => {
      try {
        const [s, m] = await Promise.all([api('/status'), api('/models')]);
        const before = new Set(((P.models && P.models.catalog) || []).filter((c) => c.state === 'downloading').map((c) => c.id));
        P.status = s; P.models = m;
        if (!P.started) {                                             // first look: take over what the saved profile says
          P.started = true;
          const p = s.profile;
          if (p) { P.ctx = p.context_tokens ? String(p.context_tokens) : ''; P.gpu = p.gpu || 'auto'; }
          const names = m.installed.filter((x) => x.exists && x.valid);
          const mine = p && names.find((x) => x.file === p.model);
          P.sel = mine ? mine.id : names.length === 1 ? names[0].id : '';
          P.onSelect(P.sel);
        }
        // a download that just finished: choose it when nothing is chosen yet
        m.catalog.filter((c) => before.has(c.id) && c.state === 'installed').forEach((c) => {
          const got = m.installed.find((x) => x.catalog_id === c.id);
          if (got && !P.sel) { P.sel = got.id; P.onSelect(P.sel); }
        });
        if (P.sel && !m.installed.some((x) => x.id === P.sel && x.exists && x.valid)) { P.sel = ''; P.onSelect(''); }
        draw();
      } catch (e) { say(e.message, 'err'); }
    };

    const loop = async () => {
      if (!P.alive) return;
      if (!host.isConnected) { P.alive = false; return; }            // the dialog was closed
      if (fast() || P.tick % 4 === 0) await refresh();
      P.tick += 1;
      if (P.alive) P.timer = setTimeout(loop, 1000);
    };

    /* saving the choice: the model, the window and the graphics-card setting become the Local model profile */
    const save = async () => {
      if (!P.sel) throw new Error('Choose a model first.');
      const r = await api('/connect', { method: 'POST', body: { model: P.sel, context_tokens: P.ctx ? +P.ctx : null, gpu: P.gpu } });
      await MR.reloadSettings();
      await refresh();
      return r;
    };

    const act = async (a, el) => {
      try {
        if (a === 'download') {
          el.disabled = true;
          await api('/models/download', { method: 'POST', body: { id: el.dataset.id } });
          await MR.pollJobs(); await refresh();
        } else if (a === 'stopjob') {
          await MR.api('/api/jobs/' + encodeURIComponent(el.dataset.job) + '/cancel', { method: 'POST' });
          await MR.pollJobs(); setTimeout(refresh, 600);
        } else if (a === 'remove') {
          const x = P.models.installed.find((i) => i.id === el.dataset.id);
          const ok = x.linked || await MR.confirm('Delete model', `Delete ${x.file} (${sz(x.size)}) from this computer? You can download it again later.`, 'Delete');
          if (!ok) return;
          await api('/models/delete', { method: 'POST', body: { id: x.id } });
          if (P.sel === x.id) { P.sel = ''; P.onSelect(''); }
          await MR.reloadSettings(); await refresh();
        } else if (a === 'engine') {
          el.disabled = true;
          await api('/engine/download', { method: 'POST' });
          await MR.pollJobs(); await refresh();
        } else if (a === 'browse') {
          say('Choose the file in the window that opened...');
          const r = await api('/models/link', { method: 'POST', body: {} });
          say('');
          if (!r.cancelled) { await refresh(); P.sel = r.model.id; P.onSelect(P.sel); draw(); if (P.mode === 'settings') await apply(); }
        } else if (a === 'start') {
          await api('/start', { method: 'POST' }); await refresh();
        } else if (a === 'stop') {
          await api('/stop', { method: 'POST' }); await refresh(); say('Unloaded: its memory is free again.', 'ok');
        } else if (a === 'test') {
          el.disabled = true; say('Loading the model and asking it a tiny question...');
          const r = await api('/test', { method: 'POST' });
          say(`It answered "${r.reply}" in ${r.answer_seconds} s${r.load_seconds > 1 ? ` (loading took ${r.load_seconds} s)` : ''}.`, 'ok');
          await refresh();
        }
      } catch (e) { say(e.message, 'err'); if (el) el.disabled = false; refresh(); }
    };

    const apply = async () => {                                       // settings mode: a change is saved at once
      if (P.mode !== 'settings' || !P.sel) return;
      try { await save(); say('Saved.', 'ok'); P.onSaved(); } catch (e) { say(e.message, 'err'); }
    };

    host.addEventListener('click', (e) => {
      const el = e.target.closest('[data-a]');
      if (!el || !host.contains(el) || el.disabled) return;
      e.preventDefault();
      act(el.dataset.a, el);
    });
    host.addEventListener('change', (e) => {
      const t = e.target;
      if (t.name === 'lc-model') { P.sel = t.value; P.onSelect(P.sel); draw(); apply(); }
      else if (t.dataset.lc === 'ctx') { P.ctx = t.value; apply(); }
      else if (t.dataset.lc === 'gpu') { P.gpu = t.value; apply(); }
    });
    q('path').addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const path = e.target.value.trim();
      if (!path) return;
      try {
        const r = await api('/models/link', { method: 'POST', body: { path } });
        e.target.value = '';
        await refresh(); P.sel = r.model.id; P.onSelect(P.sel); draw();
        if (P.mode === 'settings') await apply();
      } catch (err) { say(err.message, 'err'); }
    });

    refresh().then(() => { P.timer = setTimeout(loop, 1000); });
    return {
      refresh, selected, save, say,
      get choice() { return P.sel; },
      destroy() { P.alive = false; clearTimeout(P.timer); },
    };
  };

  /* ------------------------------------------------------------------ in Connect a model */
  /* The tile was chosen: draw the panel under the tiles and turn the dialog's main button into "Use this model".
     Returns a function that stops the panel (called when another tile is chosen). */
  L.connectPane = function (m, onConnected) {
    const btn = m.$('#cx-go');
    const ctl = L.mount(m.$('#cx-local'), {
      mode: 'connect',
      onSelect: (id) => { btn.disabled = !id; },
    });
    btn.textContent = 'Use this model';
    btn.disabled = true;
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = 'Connecting...';
      try {
        const r = await ctl.save();
        onConnected();
        const info = r.info || {};
        m.$('#cx-local').insertAdjacentHTML('afterbegin', `<div class="callout" style="border-left-color:var(--ok)"><b>Connected to the local model ${MR.esc(r.profile.model)}.</b>
          You can start asking questions: the model loads with the first one${info.context_window ? ` and reads up to ${MR.fmt.int(info.context_window)} tokens at a time` : ''}.
          ${r.embeddings_note ? '<br>' + MR.esc(r.embeddings_note) : ''}${r.fit && r.fit.message ? `<br><span class="warn">${MR.esc(r.fit.message)}</span>` : ''}</div>`);
        m.$('#cx-local').scrollIntoView({ block: 'start' });
        btn.disabled = false; btn.textContent = 'Done'; btn.onclick = () => m.close();
        const cancel = m.$('[data-close2]');
        if (cancel) cancel.hidden = true;
      } catch (e) {
        ctl.say(e.message, 'err');
        btn.disabled = !ctl.choice; btn.textContent = 'Use this model';
      }
    };
    return () => ctl.destroy();
  };

  /* ------------------------------------------------------------------ in Settings > Models */
  L.settingsGroup = function (p, active) {
    return `<div class="group" data-local-prof="${MR.esc(p.id)}"><h3>${MR.esc(p.name)}${active ? ' &middot; active' : ''}</h3>
      <div data-lc-host></div>
      <div class="row" style="margin-top:12px"><button class="btn small" type="button" data-lact="activate" ${active ? 'disabled' : ''}>${active ? 'Active' : 'Use this one'}</button>
        <button class="btn small danger" type="button" data-lact="remove">Disconnect</button></div></div>`;
  };

  L.wireSettings = function (g, redraw) {
    const id = g.dataset.localProf;
    const ctl = L.mount(g.querySelector('[data-lc-host]'), { mode: 'settings', onSaved: () => { /* the top bar already follows reloadSettings */ } });
    g.querySelector('[data-lact="activate"]').addEventListener('click', async () => {
      await MR.api('/api/profiles/' + encodeURIComponent(id) + '/activate', { method: 'POST' });
      ctl.destroy(); await MR.reloadSettings(); redraw();
    });
    g.querySelector('[data-lact="remove"]').addEventListener('click', async () => {
      if (!(await MR.confirm('Disconnect', 'Forget the Local model connection? The model files stay on this computer.', 'Disconnect'))) return;
      ctl.destroy();
      try { await api('/stop', { method: 'POST' }); } catch (e) { /* it was not running */ }
      await MR.api('/api/profiles/' + encodeURIComponent(id), { method: 'DELETE' });
      await MR.reloadSettings(); redraw();
    });
    return ctl;
  };

  return L;
})();
