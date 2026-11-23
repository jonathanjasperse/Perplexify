'use strict';
/* Chat and search history: the History view, suggestions while typing (from your own history, like a search engine's
   autocomplete), related searches, and the recent chats on the welcome screen. The data lives in the app (history.db in the
   data folder); see perplexify/history.py and history_api.py. */

MR.history = (() => {
  const H = { kind: 'chats', q: '', seq: 0, keep: true, noteTimer: 0, lastNoted: '', relSeq: 0, list: [], recentSeq: 0, recentSearchSeq: 0 };
  const enc = encodeURIComponent;
  const api = (path, opts) => MR.api('/api/history' + path, opts);
  const norm = (t) => String(t || '').trim().replace(/\s+/g, ' ').toLowerCase();
  H.api = api;

  /* ------------------------------------------------------------------ small helpers */
  const dayLabel = (ts) => {
    const d = new Date(ts * 1000), now = new Date();
    const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((start(now) - start(d)) / 86400000);
    if (diff <= 0) return 'Today';
    if (diff === 1) return 'Yesterday';
    if (diff < 7) return 'Previous 7 days';
    if (diff < 30) return 'Previous 30 days';
    return d.toLocaleString([], { month: 'long', year: 'numeric' });
  };
  /* the matched words of a full-text hit are wrapped in control characters by the app; escape first, then turn them into <mark> */
  const marked = (snip) => MR.esc(snip).replace(/\u0001/g, '<mark>').replace(/\u0002/g, '</mark>');
  const styled = (text, typed) => {
    const t = String(typed || '').trim();
    if (t && text.toLowerCase().startsWith(t.toLowerCase())) return '<b>' + MR.esc(text.slice(0, t.length)) + '</b>' + MR.esc(text.slice(t.length));
    const words = t.split(/\s+/).filter(Boolean);
    return words.length ? MR.hl(text, words) : MR.esc(text);
  };
  const filterWords = (f) => {
    const out = [];
    const mode = { semantic: 'meaning', keyword: 'keywords' }[f.mode];
    if (mode) out.push(mode); else out.push('meaning + keywords');
    if (f.scope === 'code') out.push('code only'); else if (f.scope === 'docs') out.push('docs only');
    if (f.match === 'all') out.push('all words');
    if (f.langs && f.langs.length) out.push(f.langs.join(', '));
    if (f.path) out.push('path contains "' + f.path + '"');
    return out;
  };

  /* ------------------------------------------------------------------ suggestions while typing */
  /* Attach a dropdown of earlier questions and searches to a text box. Arrow keys move, Enter or Tab take the highlighted one,
     Escape closes it; with nothing highlighted Enter does what it always did. opts: up (open above the box), prefer ('chat' or
     'search': which kind comes first), min (characters before it opens), recent (open with recent items on click when empty),
     onPick(suggestion, input). */
  H.attachSuggest = function (input, opts) {
    if (!input) return;
    const host = input.closest('.column') || input.parentElement;
    const box = MR.el('<div class="suggest" role="listbox" hidden></div>');
    box.classList.add(opts.up ? 'up' : 'down');
    host.appendChild(box);
    let items = [], on = -1, seq = 0, timer = 0;
    const close = () => { box.hidden = true; items = []; on = -1; };
    const paint = () => {
      box.innerHTML = items.map((s, i) => `<button type="button" role="option" class="sg${i === on ? ' on' : ''}" data-i="${i}">
        <span class="sg-text">${styled(s.text, input.value)}</span><span class="tag" title="${s.kind === 'chat' ? 'You asked this' : 'You searched for this'}">${s.kind === 'chat' ? 'asked' : 'searched'}</span></button>`).join('');
    };
    const open = async (q) => {
      const mine = ++seq;
      let r;
      try { r = await api('/suggest?q=' + enc(q) + '&limit=' + (opts.limit || 6) + '&prefer=' + (opts.prefer || '')); } catch (e) { return; }
      if (mine !== seq || document.activeElement !== input) return;
      items = (r.suggestions || []).filter((s) => norm(s.text) !== norm(q));       // not what is already typed
      if (!items.length) { close(); return; }
      on = -1; paint();
      if (!opts.up) { const w = input.closest('.sv-input') || input; box.style.top = (w.offsetTop + w.offsetHeight + 4) + 'px'; }
      box.hidden = false;
    };
    const pick = (i) => { const s = items[i]; if (!s) return; close(); opts.onPick(s, input); };
    input.addEventListener('input', () => {
      clearTimeout(timer); seq++;
      const q = input.value;
      if (q.trim().length < (opts.min || 1) || q.startsWith('/')) { close(); return; }      // "/" starts a command in the Ask box
      timer = setTimeout(() => open(q), 110);
    });
    // in the capture phase, so the box's own Enter / arrow handling does not also run while a suggestion is highlighted
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { clearTimeout(timer); seq++; }                // sent or searched: no dropdown may open for what was just submitted
      if (box.hidden) {
        if (e.key === 'ArrowDown' && opts.recent && !input.value.trim()) { e.preventDefault(); open(''); }
        return;
      }
      if (e.key === 'ArrowDown') { e.preventDefault(); e.stopImmediatePropagation(); on = (on + 1) % items.length; paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopImmediatePropagation(); on = on <= 0 ? items.length - 1 : on - 1; paint(); }
      else if ((e.key === 'Enter' || e.key === 'Tab') && on >= 0) { e.preventDefault(); e.stopImmediatePropagation(); pick(on); }
      else if (e.key === 'Escape') { e.stopImmediatePropagation(); close(); }
      else if (e.key === 'Enter') close();
    }, true);
    input.addEventListener('blur', () => setTimeout(close, 120));
    if (opts.recent) input.addEventListener('click', () => { if (!input.value.trim()) open(''); });
    box.addEventListener('mousedown', (e) => {                                   // mousedown, so the box keeps focus until the click is handled
      e.preventDefault();
      const b = e.target.closest('.sg');
      if (b) pick(+b.dataset.i);
    });
  };

  /* ------------------------------------------------------------------ related searches under the results */
  H.related = async function (q) {
    const box = MR.$('#sv-related');
    if (!box) return;
    const mine = ++H.relSeq;
    let r;
    try { r = await api('/related?q=' + enc(q) + '&limit=6'); } catch (e) { return; }
    if (mine !== H.relSeq) return;
    const found = r.related || [];
    if (!found.length) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML = '<span class="label">Related in your history</span><div class="facets">' + found.map((s, i) =>
      `<button type="button" class="chip" data-i="${i}" title="${s.kind === 'chat' ? 'A question you asked' : 'A search you ran'}${s.count > 1 ? ', ' + s.count + ' times' : ''}">${MR.esc(s.text.length > 60 ? s.text.slice(0, 59) + '…' : s.text)}</button>`).join('') + '</div>';
    MR.$$('.chip', box).forEach((b) => b.addEventListener('click', () => MR.searchFor(found[+b.dataset.i].text)));
  };

  /* Called after every search: remembers it (at once when it was confirmed with Enter, a click or a suggestion; after a short
     pause when it was only typed) and shows related searches from the history. `q` is null when the box was emptied. */
  H.afterSearch = function (q, count) {
    clearTimeout(H.noteTimer);
    const SS = MR.SS, box = MR.$('#sv-related');
    const committed = SS.committed;
    SS.committed = false;
    if (!q) { H.relSeq++; if (box) { box.hidden = true; box.innerHTML = ''; } return; }
    const filters = MR.searchFilters();
    const key = JSON.stringify([norm(q), filters]);
    const note = () => {
      if (H.lastNoted === key || !H.keep) return;
      H.lastNoted = key;
      api('/searches', { method: 'POST', body: { query: q, filters, libraries: MR.searchLibs(), results: count || 0 } }).then(() => H.changed(true), () => {});
    };
    if (committed) note(); else if (count > 0) H.noteTimer = setTimeout(note, 1500);
    H.related(q);
  };

  /* ------------------------------------------------------------------ the History view */
  H.render = function () {
    const v = MR.$('#historyview');
    if (!v) return;
    v.innerHTML = `
      <div class="sv-bar"><div class="column">
        <div class="sv-input"><input id="hv-q" type="search" placeholder="Search your chats and searches..." aria-label="Search the history" autocomplete="off" spellcheck="false"></div>
        <div class="sv-opts">
          <div class="grp"><span class="label">Show</span><div class="seg" id="hv-kind" role="group">
            <button type="button" data-v="chats" aria-pressed="true">Chats</button><button type="button" data-v="searches" aria-pressed="false">Searches</button></div></div>
          <label class="check" title="Switch off to stop saving new chats and searches. What is saved stays until you delete it."><input type="checkbox" id="hv-keep" checked> remember chats and searches</label>
          <a href="#" class="reset" id="hv-clear" title="Delete everything in this list" hidden>clear all</a>
        </div>
        <div class="sv-status" id="hv-status"></div>
      </div></div>
      <div class="sv-results"><div class="column" id="hv-list"></div></div>`;
    const q = MR.$('#hv-q');
    let timer = 0;
    q.addEventListener('input', () => { H.q = q.value.trim(); clearTimeout(timer); timer = setTimeout(H.refresh, 200); });
    q.addEventListener('keydown', (e) => { if (e.key === 'Escape' && q.value) { q.value = ''; H.q = ''; H.refresh(); } });
    MR.$$('#hv-kind button').forEach((b) => b.addEventListener('click', () => {
      H.kind = b.dataset.v;
      MR.$$('#hv-kind button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      H.refresh();
    }));
    MR.$('#hv-keep').addEventListener('change', async (e) => {
      try { const st = await api('/config', { method: 'PUT', body: { enabled: e.target.checked } }); H.keep = st.enabled; }
      catch (err) { e.target.checked = H.keep; MR.toast(err.message, 'err'); }
    });
    MR.$('#hv-clear').addEventListener('click', async (e) => {
      e.preventDefault();
      const what = H.kind === 'chats' ? 'chats' : 'searches';
      if (!(await MR.confirm('Clear ' + what, `Delete every saved ${what === 'chats' ? 'chat, with its answers and sources' : 'search'}? This cannot be undone.`, 'Delete all'))) return;
      try { await api('/' + what, { method: 'DELETE' }); } catch (err) { MR.toast(err.message, 'err'); return; }
      if (what === 'chats') MR.S.chat.id = null;
      H.changed();
    });
    MR.$('#hv-list').addEventListener('click', (e) => {
      const item = e.target.closest('.hv-item');
      if (!item) return;
      const act = e.target.closest('[data-act]');
      if (act) { e.stopPropagation(); H.act(item, act.dataset.act); } else H.open(item);
    });
    MR.$('#hv-list').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.classList.contains('hv-item')) H.open(e.target);
    });
  };

  H.show = function () {
    H.refresh();
    api('').then((st) => { H.keep = st.enabled; const k = MR.$('#hv-keep'); if (k) k.checked = st.enabled; }, () => {});
  };

  H.refresh = async function () {
    const mine = ++H.seq;
    const list = MR.$('#hv-list'), status = MR.$('#hv-status'), clear = MR.$('#hv-clear');
    if (!list) return;
    let d;
    try { d = await api((H.kind === 'chats' ? '/chats?limit=150&q=' : '/searches?limit=200&q=') + enc(H.q)); }
    catch (e) { if (mine === H.seq) status.innerHTML = '<span class="err">' + MR.esc(e.message) + '</span>'; return; }
    if (mine !== H.seq) return;
    H.list = H.kind === 'chats' ? d.chats : d.searches;
    const what = H.kind === 'chats' ? 'chat' : 'search';
    status.innerHTML = H.q ? `<span>${d.total} ${what}${d.total === 1 ? '' : (what === 'chat' ? 's' : 'es')} match</span>`
      : `<span>${d.total} saved ${what}${d.total === 1 ? '' : (what === 'chat' ? 's' : 'es')}</span>` + (H.keep ? '' : '<span class="warn">history is off: new chats and searches are not saved</span>');
    clear.hidden = !!H.q || !d.total;
    if (!H.list.length) { list.innerHTML = H.emptyText(); return; }
    list.innerHTML = H.kind === 'chats' ? H.chatsHTML() : H.searchesHTML();
  };

  H.emptyText = function () {
    if (H.q) return `<div class="sv-empty"><b>Nothing in your history matches "${MR.esc(H.q)}".</b></div>`;
    return H.kind === 'chats'
      ? '<div class="sv-empty"><b>No chats saved yet.</b><br>Ask a question in the Ask tab. Every answer, together with the sources it cited, is kept here so you can open the conversation again and carry on.</div>'
      : '<div class="sv-empty"><b>No searches saved yet.</b><br>Searches you run in the Search tab are kept here, and suggested again when you start typing something similar.</div>';
  };

  H.chatsHTML = function () {
    let last = '', out = '';
    H.list.forEach((c, i) => {
      const g = H.q ? '' : dayLabel(c.updated);
      if (g && g !== last) { out += `<div class="hv-group">${MR.esc(g)}</div>`; last = g; }
      const libs = (c.libraries || []).map((l) => l.name);
      const libText = libs.length > 2 ? libs.slice(0, 2).join(', ') + ' +' + (libs.length - 2) : libs.join(', ');
      const model = c.profile && (c.profile.model || c.profile.name);
      const line = c.match ? marked(c.match) : MR.esc(c.preview || '');
      out += `<article class="hv-item" data-i="${i}" tabindex="0">
        <div class="hv-main"><div class="hv-title" title="${MR.esc(c.title)}">${MR.esc(c.title)}</div>
          ${line ? `<div class="hv-preview">${line}</div>` : ''}
          <div class="hv-meta"><span>${c.turns} ${c.turns === 1 ? 'question' : 'questions'}</span>${c.sources ? `<span>${c.sources} sources cited</span>` : ''}
            ${model ? `<span>${MR.esc(model)}</span>` : ''}${libText ? `<span title="${MR.esc(libs.join(', '))}">${MR.esc(libText)}</span>` : ''}<span>${MR.fmt.ago(c.updated)}</span></div></div>
        <div class="hv-tools"><button class="btn small quiet" type="button" data-act="rename">rename</button><button class="btn small quiet" type="button" data-act="delete">delete</button></div>
      </article>`;
    });
    return out;
  };

  H.searchesHTML = function () {
    let last = '', out = '';
    H.list.forEach((s, i) => {
      const g = H.q ? '' : dayLabel(s.last_run);
      if (g && g !== last) { out += `<div class="hv-group">${MR.esc(g)}</div>`; last = g; }
      const libs = (s.libraries || []).map((l) => l.name);
      out += `<article class="hv-item" data-i="${i}" tabindex="0">
        <div class="hv-main"><div class="hv-title" title="${MR.esc(s.query)}">${MR.esc(s.query)}</div>
          <div class="hv-meta">${filterWords(s.filters || {}).map((w) => `<span>${MR.esc(w)}</span>`).join('')}
            <span>${s.results} ${s.results === 1 ? 'result' : 'results'}</span>${s.runs > 1 ? `<span>searched ${s.runs} times</span>` : ''}
            ${libs.length ? `<span title="${MR.esc(libs.join(', '))}">${MR.esc(libs.length > 2 ? libs.slice(0, 2).join(', ') + ' +' + (libs.length - 2) : libs.join(', '))}</span>` : ''}<span>${MR.fmt.ago(s.last_run)}</span></div></div>
        <div class="hv-tools"><button class="btn small quiet" type="button" data-act="delete">delete</button></div>
      </article>`;
    });
    return out;
  };

  H.open = function (item) {
    const entry = H.list[+item.dataset.i];
    if (!entry) return;
    if (H.kind === 'chats') MR.openChat(entry.id); else MR.searchFor(entry.query, entry.filters || {});
  };

  H.act = async function (item, act) {
    const entry = H.list[+item.dataset.i];
    if (!entry) return;
    try {
      if (H.kind === 'chats' && act === 'rename') {
        const title = await MR.ask1('Rename chat', 'Title', entry.title);
        if (!title) return;
        await api('/chats/' + enc(entry.id), { method: 'PATCH', body: { title } });
        if (MR.S.chat.id === entry.id) { MR.S.chat.title = title; MR.renderViewTabs(); }
      } else if (act === 'delete') {
        const label = H.kind === 'chats' ? entry.title : entry.query;
        if (!(await MR.confirm(H.kind === 'chats' ? 'Delete chat' : 'Delete search', `Delete "${label.length > 80 ? label.slice(0, 79) + '…' : label}"? This cannot be undone.`))) return;
        await api(H.kind === 'chats' ? '/chats/' + enc(entry.id) : '/searches/' + entry.id, { method: 'DELETE' });
        if (H.kind === 'chats' && MR.S.chat.id === entry.id) MR.S.chat.id = null;       // the next question starts a new chat
      }
    } catch (e) { MR.toast(e.message, 'err'); return; }
    H.changed();
  };

  /* something was saved, renamed or deleted: redraw whatever shows the lists */
  H.changed = function (quiet) {
    if (MR.S.view === 'history') H.refresh();
    if (!quiet) H.fillRecent();
    H.fillRecentSearches();
  };

  /* ------------------------------------------------------------------ the welcome screen */
  H.fillRecent = async function () {
    const box = MR.$('#w-recent');
    if (!box) return;
    const mine = ++H.recentSeq;
    let d;
    try { d = await api('/chats?limit=4'); } catch (e) { return; }
    if (mine !== H.recentSeq || !box.isConnected) return;
    if (!d.chats.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<div class="label">Recent chats</div><div class="recent">' + d.chats.map((c) =>
      `<button type="button" data-chat="${MR.esc(c.id)}"><span class="r-title">${MR.esc(c.title)}</span><span class="r-meta">${MR.fmt.ago(c.updated)}</span></button>`).join('') +
      '</div><a href="#" class="hint" id="w-allhist">See all in History</a>';
    MR.$$('[data-chat]', box).forEach((b) => b.addEventListener('click', () => MR.openChat(b.dataset.chat)));
    MR.$('#w-allhist', box).addEventListener('click', (e) => { e.preventDefault(); MR.setView('history'); });
  };

  /* the Search view before anything is typed: the searches you ran last, one click to run them again with the same options */
  H.fillRecentSearches = async function () {
    const box = MR.$('#sv-recent');
    if (!box) return;
    const mine = ++H.recentSearchSeq;
    let d;
    try { d = await api('/searches?limit=8'); } catch (e) { return; }
    if (mine !== H.recentSearchSeq || !box.isConnected) return;
    const found = d.searches || [];
    if (!found.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<div class="label" style="margin:16px 0 8px">Recent searches</div><div class="facets">' + found.map((s, i) =>
      `<button type="button" class="chip" data-i="${i}" title="${MR.esc(filterWords(s.filters || {}).join(', '))}">${MR.esc(s.query.length > 50 ? s.query.slice(0, 49) + '…' : s.query)}</button>`).join('') + '</div>';
    MR.$$('.chip', box).forEach((b) => b.addEventListener('click', () => MR.searchFor(found[+b.dataset.i].query, found[+b.dataset.i].filters || {})));
  };

  /* ------------------------------------------------------------------ start up */
  H.init = function () {
    H.render();
    const ta = MR.$('#prompt');
    H.attachSuggest(ta, { up: true, prefer: 'chat', limit: 6, min: 2, onPick: (s) => { ta.value = s.text; MR.autosize(); ta.focus(); } });
    const sq = MR.$('#sv-q');
    H.attachSuggest(sq, { prefer: 'search', limit: 7, min: 1, recent: true, onPick: (s) => {
      sq.value = s.text; MR.SS.q = s.text; MR.SS.committed = true; clearTimeout(MR.SS.timer); MR.runSearch();
    } });
    H.attachSuggest(MR.$('#hv-q'), { prefer: 'chat', limit: 7, min: 1, onPick: (s, input) => { input.value = s.text; H.q = s.text; H.refresh(); } });
    api('').then((st) => { H.keep = st.enabled; }, () => {});
  };

  return H;
})();

/* Open a saved chat in the Ask view, exactly as it was (the trace, the answer, the cited sources), ready to carry on. */
MR.openChat = async function (id) {
  const S = MR.S;
  if (S.streaming) { MR.toast('An answer is still being written. Press Stop first.', 'err'); return; }
  let chat;
  try { chat = await MR.api('/api/history/chats/' + encodeURIComponent(id)); }
  catch (e) { MR.toast(e.message, 'err'); return; }
  MR.newChat();
  MR.setView('ask', { quiet: true });
  const tr = MR.$('#transcript');
  tr.innerHTML = '';
  chat.turns_list.forEach((t) => MR.replayTurn(t));
  S.chat = { id: chat.id, title: chat.title, turns: chat.turns_list.length };
  S.history = chat.turns_list.filter((t) => t.answer).flatMap((t) => [{ role: 'user', content: t.question }, { role: 'assistant', content: t.answer }]);
  MR.renderViewTabs();
  // the libraries this chat searched, when they are not the ticked ones now
  const ids = (chat.libraries || []).map((l) => l.id).filter((i) => S.libs.some((l) => l.id === i));
  const same = ids.length === S.selected.size && ids.every((i) => S.selected.has(i));
  if (ids.length && !same) {
    const names = chat.libraries.filter((l) => ids.includes(l.id)).map((l) => l.name).join(', ');
    const note = MR.el(`<div class="column"><div class="notice info">This chat searched ${MR.esc(names)}. <a href="#" data-tick>Tick ${ids.length === 1 ? 'it' : 'them'}</a> to carry on there.</div></div>`);
    tr.insertBefore(note, tr.firstChild);
    MR.$('[data-tick]', note).addEventListener('click', (e) => { e.preventDefault(); S.selected = new Set(ids); MR.selectionChanged(); note.remove(); });
  }
  tr.scrollTop = tr.scrollHeight;
  const p = MR.$('#prompt');
  if (p) p.focus();
};
