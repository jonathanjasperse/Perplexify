'use strict';
/* The conversation: transcript, composer, streaming, source cards, in-app source viewer. */

const SPIN = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
MR._spinI = 0;
MR._spinTimer = null;
MR.startSpin = function () {
  if (MR._spinTimer) return;
  MR._spinTimer = setInterval(() => {
    MR._spinI = (MR._spinI + 1) % SPIN.length;
    /* the spinner itself is drawn in CSS; this timer is kept only so callers do not change */
  }, 90);
};
MR.stopSpin = function () { clearInterval(MR._spinTimer); MR._spinTimer = null; };

/* ------------------------------------------------------------------ welcome / empty state */
MR.renderWelcomeIfEmpty = function () {
  const tr = MR.$('#transcript');
  if (!tr || tr.querySelector('.turn')) return;
  const S = MR.S;
  const connected = !!S.profile, hasLibs = S.libs.length > 0;
  const preset = (S.presets || [])[0];
  const step = (n, done, title, sub, btn) => `<div class="step"><span class="n">${done ? '<span class="ok">ok</span>' : n}</span>
      <span class="t"><b>${title}</b><span>${sub}</span></span>${btn || ''}</div>`;
  const questions = hasLibs ? [
    'How do I get started?', 'Which function should I call to...', 'Explain how ... works, with an example',
    'Where is ... implemented?'] : [];
  tr.innerHTML = `<div class="column welcome">
    <h1>Ask your manuals and your code.</h1>
    <p>Perplexify indexes documentation and source code on this computer, finds the passages that matter for each question,
       and asks the cloud model you choose to answer from them, with citations you can open. The material can be far larger than
       any model's context window.</p>
    <div class="steps">
      ${step(1, connected, connected ? 'Model connected' : 'Connect a model', connected ? `${MR.esc(S.profile.name)}, ${MR.esc(S.profile.model)}` : 'ChatGPT, Claude, Gemini or DeepSeek. Paste a key and you are done.',
        connected ? '' : '<button class="btn primary" id="w-connect" type="button">Connect</button>')}
      ${step(2, hasLibs, hasLibs ? `${S.libs.length} ${S.libs.length === 1 ? 'library' : 'libraries'} ready` : 'Add something to search',
        hasLibs ? 'Add more any time from the sidebar.' : 'A documentation website, a GitHub repository, a folder of PDFs or Markdown.',
        '<button class="btn' + (hasLibs ? '' : ' primary') + '" id="w-add" type="button">Add source</button>')}
      ${preset && !hasLibs ? step('', false, preset.name, MR.esc(preset.description), '<button class="btn" id="w-preset" type="button">Add this</button>') : ''}
    </div>
    ${questions.length ? '<div class="label">Try asking</div><div class="suggestions">' + questions.map((q) => `<button type="button" data-q="${MR.esc(q)}">${MR.esc(q)}</button>`).join('') + '</div>' : ''}
    ${hasLibs ? '<p class="hint" style="margin-top:18px">Rather read the passages yourself? <a href="#" id="w-search">Search without a model</a>: meaning and keyword search of the ticked libraries.</p>' : ''}
  </div>`;
  const on = (id, fn) => { const b = MR.$(id, tr); if (b) b.addEventListener('click', fn); };
  on('#w-search', (e) => { e.preventDefault(); MR.setView('search'); });
  on('#w-connect', () => MR.openConnect());
  on('#w-add', () => MR.openAdd());
  on('#w-preset', () => MR.addPreset(preset));
  MR.$$('[data-q]', tr).forEach((b) => b.addEventListener('click', () => { const t = MR.$('#prompt'); t.value = b.dataset.q; t.focus(); MR.autosize(); }));
  MR.updateBanner();
};

MR.updateBanner = function () {
  const b = MR.$('#banner');
  if (!b) return;
  if (MR.S.profile) { b.hidden = true; return; }
  b.hidden = false;
  b.innerHTML = '<span>No model is connected, so questions cannot be answered yet.</span><button class="btn small primary" id="banner-connect" type="button">Connect a model</button>';
  MR.$('#banner-connect').addEventListener('click', () => MR.openConnect());
};

/* ------------------------------------------------------------------ composer */
const COMMANDS = [
  ['/new', 'start a new conversation'], ['/search', 'search the libraries without a model: /search words'], ['/add', 'add a website, repository or documents'],
  ['/model', 'change the model'], ['/theme', 'switch light / dark'], ['/settings', 'open settings'], ['/help', 'what can I type here?'],
];
MR.renderComposer = function () {
  MR.$('#composer').innerHTML = `<div class="column">
    <div class="slash" id="slash" hidden></div>
    <div class="promptbox">
      <textarea id="prompt" rows="1" placeholder="Ask about your libraries..." aria-label="Question" spellcheck="true"></textarea>
      <button class="btn primary small" id="btn-send" type="button">send</button></div>
    <div class="hintbar"><div class="left">
      <span><span class="kbd">Enter</span> send</span><span><span class="kbd">Shift</span>+<span class="kbd">Enter</span> new line</span>
      <span><span class="kbd">Esc</span> stop</span><span><span class="kbd">/</span> commands</span></div>
      <div class="right"><span id="scope"></span></div></div></div>`;
  const ta = MR.$('#prompt');
  ta.addEventListener('input', () => { MR.autosize(); MR.slashUpdate(); });
  ta.addEventListener('keydown', MR.promptKey);
  MR.$('#btn-send').addEventListener('click', () => (MR.S.streaming ? MR.stop() : MR.submit()));
  MR.updateScope();
  ta.focus();
};
MR.autosize = function () { const t = MR.$('#prompt'); t.style.height = 'auto'; t.style.height = Math.min(200, t.scrollHeight) + 'px'; };
/* which libraries the ticks in the sidebar point at, in words: "JUCE docs", "JUCE docs, JUCE source", "all 4 libraries" */
MR.scopeLabel = function () {
  const S = MR.S, sel = S.libs.filter((l) => S.selected.has(l.id));
  if (!sel.length) return '';
  if (sel.length <= 2) return sel.map((l) => l.name).join(', ');
  return sel.length === S.libs.length ? `all ${sel.length} libraries` : `${sel.length} libraries`;
};
MR.updateScope = function () {
  const el = MR.$('#scope');
  if (el) {
    const t = MR.scopeLabel();
    el.textContent = t ? 'searching ' + t : 'no library selected';
    el.title = MR.S.libs.filter((l) => MR.S.selected.has(l.id)).map((l) => l.name).join(', ');
  }
  const b = MR.$('#btn-send');
  if (b) b.textContent = MR.S.streaming ? 'stop' : 'send';
};

MR._slashIdx = 0;
MR.slashUpdate = function () {
  const ta = MR.$('#prompt'), box = MR.$('#slash');
  const v = ta.value;
  if (!v.startsWith('/') || /\s/.test(v)) { box.hidden = true; return; }
  const list = COMMANDS.filter((c) => c[0].startsWith(v.toLowerCase()));
  if (!list.length) { box.hidden = true; return; }
  MR._slashIdx = Math.min(MR._slashIdx, list.length - 1);
  box.hidden = false;
  box.innerHTML = list.map((c, i) => `<button type="button" data-cmd="${c[0]}" class="${i === MR._slashIdx ? 'on' : ''}"><span>${c[0]}</span><span>${c[1]}</span></button>`).join('');
  MR.$$('button', box).forEach((b) => b.addEventListener('mousedown', (e) => { e.preventDefault(); MR.runCommand(b.dataset.cmd); }));
};
MR.runCommand = function (cmd) {
  const ta = MR.$('#prompt');
  ta.value = ''; MR.autosize(); MR.$('#slash').hidden = true;
  ({ '/new': MR.newChat, '/search': () => MR.setView('search'), '/add': () => MR.openAdd('web', MR.S.focus), '/model': () => MR.openSettings('models'),
     '/theme': () => MR.setTheme(MR.theme() === 'dark' ? 'light' : 'dark'), '/settings': () => MR.openSettings(),
     '/help': () => MR.openHelp() }[cmd] || (() => {}))();
};
MR.promptKey = function (e) {
  const ta = e.target, box = MR.$('#slash');
  if (!box.hidden) {
    const btns = MR.$$('button', box);
    if (e.key === 'ArrowDown') { e.preventDefault(); MR._slashIdx = (MR._slashIdx + 1) % btns.length; MR.slashUpdate(); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); MR._slashIdx = (MR._slashIdx - 1 + btns.length) % btns.length; MR.slashUpdate(); return; }
    if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); MR.runCommand(btns[MR._slashIdx].dataset.cmd); return; }
    if (e.key === 'Escape') { box.hidden = true; return; }
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); MR.submit(); }
  else if (e.key === 'Escape') { if (MR.S.streaming) MR.stop(); }
  else if (e.key === 'ArrowUp' && !ta.value && MR._lastQ) { e.preventDefault(); ta.value = MR._lastQ; MR.autosize(); }
};

MR.submit = function () {
  const ta = MR.$('#prompt');
  const q = ta.value.trim();
  if (!q) return;
  if (q.startsWith('/') && COMMANDS.some((c) => c[0] === q.toLowerCase())) { MR.runCommand(q.toLowerCase()); return; }
  const found = /^\/search\s+(.+)$/i.exec(q);   // "/search words": run it in the Search view, no model involved
  if (found) { ta.value = ''; MR.autosize(); MR.$('#slash').hidden = true; MR.searchFor(found[1].trim()); return; }
  ta.value = ''; MR.autosize();
  MR.ask(q);
};
MR.stop = function () { if (MR.S.abort) MR.S.abort.abort(); };
MR.newChat = function () {
  if (MR.S.streaming) MR.stop();
  MR.S.history = [];
  MR.S.ctx.used = 0; MR.S.ctx.breakdown = null; MR.S.ctx.actual = false;
  MR.$('#transcript').innerHTML = '';
  MR.$('#viewer').hidden = true;
  MR.renderWelcomeIfEmpty();
  MR.updateGauge();
};

/* ------------------------------------------------------------------ streaming */
MR.stream = async function (body, onEvent, signal) {
  const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
  if (!res.ok || !res.body) throw new Error('The app did not accept the question (' + res.status + ').');
  const reader = res.body.getReader(), dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data: ')) {
          let ev = null;
          try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; /* a malformed line */ }
          try { onEvent(ev); } catch (e) { console.error('Could not handle the "' + ev.type + '" event', e); }
        }
      }
    }
  }
};

MR.stickScroll = function (fn) {
  const tr = MR.$('#transcript');
  const stick = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 80;
  fn();
  if (stick) tr.scrollTop = tr.scrollHeight;
};

MR.ask = async function (q) {
  const S = MR.S;
  if (S.streaming) return;
  if (!S.profile) { MR.openConnect(); return; }
  const libs = Array.from(S.selected);
  if (!libs.length) { MR.toast('Select at least one library in the sidebar first.', 'err'); return; }
  MR._lastQ = q;
  const tr = MR.$('#transcript');
  const welcome = MR.$('.welcome', tr);
  if (welcome) tr.innerHTML = '';
  tr.appendChild(MR.el(`<article class="turn user"><div class="column"><div class="prompt-line"><span class="caret">&gt;</span><div class="utext">${MR.esc(q)}</div></div></div></article>`));
  const turn = MR.el(`<article class="turn bot"><div class="column">
      <div class="trace"></div><details class="thinking" hidden><summary>reasoning</summary><div class="t-body"></div></details>
      <div class="answer"></div><div class="notices"></div><div class="sources" hidden></div><div class="foot"></div></div></article>`);
  tr.appendChild(turn);
  tr.scrollTop = tr.scrollHeight;
  const R = { trace: MR.$('.trace', turn), answer: MR.$('.answer', turn), think: MR.$('.thinking', turn), notices: MR.$('.notices', turn),
              sources: MR.$('.sources', turn), foot: MR.$('.foot', turn), text: '', thinking: '', srcs: [], est: 0, chars: 0, raf: 0,
              t0: performance.now(), usage: null, errored: false, lines: {} };
  turn._R = R;
  const line = (key, html, cls = '') => {
    let el = R.lines[key];
    if (!el) { el = R.lines[key] = MR.el('<div class="ln"><span class="dot"></span><span class="body"></span></div>'); R.trace.appendChild(el); }
    // `cls` is the row's state: spin (busy), done or err. Only the glyph span carries the `spin` class,
    // so nothing that targets spinners can ever overwrite the row itself.
    el.className = 'ln' + (cls.includes('err') ? ' err' : cls.includes('done') ? ' done' : '');
    MR.$('.dot', el).innerHTML = cls.includes('spin') ? '<span class="spin"></span>' : cls.includes('done') ? '✓' : cls.includes('err') ? '!' : '●';
    MR.$('.body', el).innerHTML = html;
    return el;
  };
  const sub = (html) => R.trace.appendChild(MR.el('<div class="ln"><span class="dot"></span><span class="sub">' + html + '</span></div>'));
  const render = (final) => {
    R.answer.innerHTML = MR.md(R.text) + (final ? '' : '<span class="cursor"></span>');
  };
  const schedule = () => { if (!R.raf) R.raf = requestAnimationFrame(() => { R.raf = 0; MR.stickScroll(() => render(false)); }); };

  S.streaming = true; S.ctx.loading = true; MR.updateGauge(); MR.updateScope(); MR.startSpin();
  S.abort = new AbortController();
  line('search', '<b>Search</b> ' + MR.esc(q.length > 90 ? q.slice(0, 90) + '...' : q), 'spin');

  const onEvent = (ev) => {
    switch (ev.type) {
      case 'status':
        if (ev.text === 'generating') line('gen', '<b>' + MR.esc(S.profile.model) + '</b> writing the answer', 'spin');
        break;
      case 'rewrite': sub('as a standalone question: <b>' + MR.esc(ev.query) + '</b>'); break;
      case 'expand': sub('also searching: ' + ev.queries.map((x) => MR.esc(x)).join(' &middot; ')); break;
      case 'sources': {
        R.srcs = ev.sources;
        const libsN = new Set(ev.sources.map((s) => s.library_id)).size;
        const d = ev.debug || {};
        line('search', '<b>Search</b> ' + MR.esc(q.length > 90 ? q.slice(0, 90) + '...' : q), 'done');
        sub(`${ev.sources.length} passages from ${libsN} ${libsN === 1 ? 'library' : 'libraries'} &middot; vector ${d.vector_hits ?? '-'} + keyword ${d.keyword_hits ?? '-'} candidates &middot; ${d.timing_ms ? MR.fmt.secs(d.timing_ms.total) : ''}${d.reranked ? ' &middot; reranked' : ''}`);
        if (d.pending) sub('<span class="warn">' + MR.fmt.n(d.pending) + ' passages are still being embedded, so semantic matching is partial.</span>');
        MR.renderSources(R, turn);
        break;
      }
      case 'context': {
        const c = S.ctx;
        c.window = ev.window || c.window; c.maxContext = ev.max_context; c.breakdown = ev.breakdown; c.used = ev.estimated_prompt; c.actual = false;
        R.est = ev.estimated_prompt;
        sub(`prompt about ${MR.fmt.n(ev.estimated_prompt)} tokens of ${MR.fmt.n(c.window)} &middot; ${MR.fmt.n(ev.breakdown.excerpts)} of it excerpts`);
        MR.updateGauge();
        break;
      }
      case 'thinking':
        R.thinking += ev.text; R.think.hidden = false; MR.$('.t-body', R.think).textContent = R.thinking; break;
      case 'token':
        if (S.ctx.loading) { S.ctx.loading = false; MR.updateGauge(); }
        R.text += ev.text; R.chars += ev.text.length;
        if (R.est) { S.ctx.used = R.est + R.chars / 3.6; MR.updateGauge(); }
        schedule();
        break;
      case 'usage':
        R.usage = ev;
        if (ev.prompt_tokens != null) { S.ctx.used = ev.prompt_tokens + (ev.completion_tokens || 0); S.ctx.actual = true; MR.updateGauge(); }
        break;
      case 'notice': R.notices.appendChild(MR.el('<div class="notice">' + MR.esc(ev.text) + '</div>')); break;
      case 'error':
        R.errored = true;
        line('gen', MR.esc(ev.message), 'err');
        if (ev.code === 'no_model') MR.openConnect();
        break;
      case 'done': R.done = ev; break;
    }
  };

  try {
    await MR.stream({ question: q, history: S.history.slice(-8), libraries: libs, profile_id: S.profile.id }, onEvent, S.abort.signal);
  } catch (e) {
    if (e.name === 'AbortError') { R.stopped = true; }
    else { R.errored = true; line('gen', MR.esc(e.message), 'err'); }
  }
  cancelAnimationFrame(R.raf); R.raf = 0;
  S.streaming = false; S.ctx.loading = false; S.abort = null;
  MR.stopSpin();
  MR.$$('.spin', turn).forEach((s) => { s.parentElement.innerHTML = R.stopped ? '■' : '✓'; });
  MR.stickScroll(() => render(true));
  if (R.text.trim()) {
    S.history.push({ role: 'user', content: q }, { role: 'assistant', content: R.text });
    const d = R.done || {}, u = R.usage || {};
    const bits = [];
    if (R.stopped) bits.push('stopped');
    if (d.elapsed_ms) bits.push(MR.fmt.secs(d.elapsed_ms));
    if (d.tokens_per_s) bits.push(d.tokens_per_s + ' tok/s');
    if (u.prompt_tokens != null) bits.push(MR.fmt.n(u.prompt_tokens) + ' in / ' + MR.fmt.n(u.completion_tokens || 0) + ' out');
    R.foot.innerHTML = bits.map((b) => '<span>' + MR.esc(b) + '</span>').join('') + '<button type="button" data-copy-answer>copy answer</button>';
  } else if (R.stopped) {
    R.foot.innerHTML = '<span>stopped</span>';
  }
  MR.updateGauge(); MR.updateScope();
  MR.refreshModelInfo();
};

/* ------------------------------------------------------------------ sources */
MR.rangeOf = function (anchor) {
  const m = /^L(\d+)(?:-L(\d+))?$/.exec(anchor || '');
  return m ? [+m[1], +(m[2] || m[1])] : null;
};
MR.renderSources = function (R, turn) {
  if (!R.srcs.length) return;
  R.sources.hidden = false;
  R.sources.innerHTML = '<div class="label">Sources</div><div class="cards">' + R.srcs.map((s) => {
    const code = s.kind === 'code', r = MR.rangeOf(s.anchor);
    const title = code ? s.title + (r ? ':' + (r[0] === r[1] ? r[0] : r[0] + '-' + r[1]) : '') : s.title;
    const crumb = (s.heading.startsWith(s.title + ' > ') ? s.heading.slice(s.title.length + 3) : s.heading) || s.library;
    const viewable = code || ['markdown', 'text', 'rst'].includes(s.kind);
    const href = s.online_url || s.local_url;
    const gh = s.online_url && /github\.com/.test(s.online_url);
    return `<div class="card" data-n="${s.n}">
      <div class="c-head"><span class="c-n">[${s.n}]</span><span class="c-title" title="${MR.esc(title)}">${MR.esc(title)}</span>${s.lang ? `<span class="tag code">${MR.esc(s.lang)}</span>` : ''}</div>
      <div class="c-path" title="${MR.esc(crumb)}">${MR.esc(crumb)}</div>
      <div class="c-snip ${code ? 'code' : ''}">${MR.esc(s.snippet)}</div>
      <div class="c-foot"><span>${MR.esc(s.library)}</span>
        ${viewable ? '<button type="button" data-view>view source</button>' : ''}
        ${href ? `<a href="${MR.esc(href)}" target="_blank" rel="noopener noreferrer">${gh ? 'GitHub' : s.online_url ? 'open page' : 'open copy'} &#8599;</a>` : ''}
        <span title="fusion score${s.cosine != null ? ', cosine ' + s.cosine : ''}">${s.score}</span></div></div>`;
  }).join('') + '</div>';
};

MR.openViewer = async function (s) {
  const v = MR.$('#viewer');
  v.hidden = false;
  v.innerHTML = '<div class="v-head"><div class="v-title">Loading...</div></div>';
  try {
    const d = await MR.api(`/api/libraries/${encodeURIComponent(s.library_id)}/source?doc=${s.doc_id}`);
    const r = MR.rangeOf(s.anchor);
    const head = (extra) => `<div class="v-head"><div class="v-title">${MR.esc(d.title)}<div class="v-sub">${MR.esc([d.lang, r ? 'lines ' + r[0] + (r[1] !== r[0] ? '-' + r[1] : '') : '', s.library].filter(Boolean).join(' · '))}</div></div>
      <button class="btn small quiet" id="v-copy" type="button">copy path</button>${d.online_url ? `<a class="btn small quiet" href="${MR.esc(d.online_url)}${s.anchor ? '#' + MR.esc(s.anchor) : ''}" target="_blank" rel="noopener noreferrer">open &#8599;</a>` : ''}
      <button class="btn small quiet" id="v-close" type="button" aria-label="Close viewer">close</button></div>${extra}`;
    if (d.text == null) {
      v.innerHTML = head(`<div class="v-body" style="padding:16px;font-family:var(--sans)">This file is not plain text. ${d.file_url ? `<a href="${MR.esc(d.file_url)}" target="_blank" rel="noopener">Open it</a>.` : ''}</div>`);
    } else {
      const lines = d.text.split('\n');
      let from = 1, to = lines.length, pre = '', post = '';
      if (lines.length > 4000) {
        const mid = r ? r[0] : 1;
        from = Math.max(1, mid - 400); to = Math.min(lines.length, mid + 800);
        pre = from > 1 ? `<div class="vgap">lines 1-${from - 1} not shown</div>` : '';
        post = to < lines.length ? `<div class="vgap">lines ${to + 1}-${lines.length} not shown</div>` : '';
      }
      let html = '';
      for (let n = from; n <= to; n++) {
        const hit = r && n >= r[0] && n <= r[1];
        html += `<div class="vline${hit ? ' hit-line' : ''}" id="vl-${n}"><span class="no">${n}</span><span class="tx">${MR.esc(lines[n - 1]) || ' '}</span></div>`;
      }
      v.innerHTML = head(`<div class="v-body" id="v-body">${pre}${html}${post}</div>`);
      if (r) {
        const target = MR.$('#vl-' + r[0]), body = MR.$('#v-body');
        if (target) body.scrollTop = Math.max(0, target.offsetTop - body.clientHeight / 3);
      }
    }
    MR.$('#v-close').addEventListener('click', () => { v.hidden = true; });
    MR.$('#v-copy').addEventListener('click', () => MR.copy(d.title, 'Path copied'));
  } catch (e) {
    v.innerHTML = `<div class="v-head"><div class="v-title err">${MR.esc(e.message)}</div><button class="btn small quiet" id="v-close" type="button">close</button></div>`;
    MR.$('#v-close').addEventListener('click', () => { v.hidden = true; });
  }
};

/* delegated clicks inside the transcript */
MR.wireTranscript = function () {
  MR.$('#transcript').addEventListener('click', (e) => {
    const t = e.target;
    const turn = t.closest('.turn.bot');
    const copyBtn = t.closest('[data-copy]');
    if (copyBtn) { const code = copyBtn.closest('.code').querySelector('code').textContent; MR.copy(code, 'Code copied'); return; }
    if (t.closest('[data-copy-answer]') && turn) { MR.copy(turn._R.text, 'Answer copied'); return; }
    const cite = t.closest('.cite');
    if (cite && turn) {
      const card = MR.$(`.card[data-n="${cite.dataset.n}"]`, turn);
      if (card) { card.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); card.classList.add('flash'); setTimeout(() => card.classList.remove('flash'), 1400); }
      return;
    }
    const view = t.closest('[data-view]');
    if (view && turn) {
      const n = +view.closest('.card').dataset.n;
      const s = turn._R.srcs.find((x) => x.n === n);
      if (s) MR.openViewer(s);
    }
  });
};
