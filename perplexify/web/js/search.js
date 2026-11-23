'use strict';
/* Manual search: the same meaning + keyword engine the chat uses, with no model involved at all. */

/* A normal search is: meaning + keywords (Both), in everything (All), in every ticked library. That is where it starts every time the
   app opens. Filters are deliberately not remembered between sessions, so yesterday's "Code only" can never silently hide today's results.
   Anything you change stays while you use the app, and "reset to defaults" brings the normal search back with one click. */
MR.SEARCH_DEFAULTS = { mode: 'hybrid', scope: 'all', all: false, path: '' };
MR.SS = Object.assign({}, MR.SEARCH_DEFAULTS, { langs: new Set(), q: '', only: null, seq: 0, offset: 0, hits: [], topScore: 0, hasMore: false, data: null, timer: 0, stale: false });

MR.searchIsDefault = function () {
  const s = MR.SS, d = MR.SEARCH_DEFAULTS;
  return s.mode === d.mode && s.scope === d.scope && s.all === d.all && !s.path.trim() && !s.langs.size && !s.only;
};

/* make every control show what MR.SS says (after a reset, or any change) */
MR.syncSearchControls = function () {
  const SS = MR.SS;
  ['mode', 'scope'].forEach((k) => MR.$$('#sv-' + k + ' button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === SS[k]))));
  const all = MR.$('#sv-all'), path = MR.$('#sv-path'), reset = MR.$('#sv-reset');
  if (all) all.checked = SS.all;
  if (path && path.value !== SS.path) path.value = SS.path;
  MR.updateSearchLangs();
  if (reset) reset.hidden = MR.searchIsDefault();
};

MR.resetSearchOptions = function () {
  Object.assign(MR.SS, MR.SEARCH_DEFAULTS, { langs: new Set(), only: null });
  MR.syncSearchControls();
  if (MR.SS.q.trim()) { clearTimeout(MR.SS.timer); MR.runSearch(); }
};

/* ------------------------------------------------------------------ the two views: Ask (a model answers) and Search (you read the passages) */
MR.renderViewTabs = function () {
  const v = MR.S.view, chat = MR.S.chat;
  const hint = { ask: 'A model answers from the passages it finds, with citations',
                 search: 'No model needed: meaning and keyword search of your libraries',
                 bookmarks: 'Sources you saved, in folders of your own',
                 history: 'Past chats and searches stay on this computer' }[v];
  // while a conversation is on screen the right-hand side names it and offers a fresh one
  const right = v === 'ask' && chat.turns
    ? `<span class="hintline chat-title" title="${MR.esc(chat.title)}">${MR.esc(chat.title || 'New chat')}</span><button class="btn small quiet" id="vt-new" type="button" title="Start a new conversation (the old one stays in History)">New chat</button>`
    : `<span class="hintline">${hint}</span>`;
  MR.$('#viewtabs').innerHTML = `
    <button type="button" role="tab" data-view="ask" aria-selected="${v === 'ask'}">Ask</button>
    <button type="button" role="tab" data-view="search" aria-selected="${v === 'search'}">Search</button>
    <button type="button" role="tab" data-view="bookmarks" aria-selected="${v === 'bookmarks'}">Bookmarks</button>
    <button type="button" role="tab" data-view="history" aria-selected="${v === 'history'}">History</button>
    <span class="spacer"></span>${right}`;
  MR.$$('[data-view]', MR.$('#viewtabs')).forEach((b) => b.addEventListener('click', () => MR.setView(b.dataset.view)));
  const fresh = MR.$('#vt-new');
  if (fresh) fresh.addEventListener('click', () => { MR.newChat(); const p = MR.$('#prompt'); if (p) p.focus(); });
};

MR.setView = function (v, opts = {}) {
  v = ['search', 'bookmarks', 'history'].includes(v) ? v : 'ask';
  MR.S.view = v;
  MR.store.set('view', v);
  MR.$('#transcript').hidden = MR.$('#composer').hidden = v !== 'ask';
  MR.$('#searchview').hidden = v !== 'search';
  MR.$('#bookmarksview').hidden = v !== 'bookmarks';
  MR.$('#historyview').hidden = v !== 'history';
  if (v === 'history') {
    MR.$('#banner').hidden = true;
    MR.$('#viewer').hidden = true;
    MR.history.show();
  } else if (v === 'bookmarks') {
    MR.$('#banner').hidden = true;
    MR.bookmarks.show();
  } else if (v === 'search') {
    MR.$('#banner').hidden = true;
    MR.updateSearchScope();
    if (MR.SS.stale && MR.SS.q.trim()) MR.runSearch();   // libraries were ticked or changed while you were in Ask
    MR.SS.stale = false;
  } else MR.updateBanner();
  MR.renderViewTabs();
  if (!opts.quiet) setTimeout(() => { const el = v === 'search' ? MR.$('#sv-q') : v === 'history' ? MR.$('#hv-q') : v === 'bookmarks' ? MR.$('#bm-q') : MR.$('#prompt'); if (el) el.focus(); }, 0);
};

/* ------------------------------------------------------------------ building the view */
MR.renderSearchView = function () {
  const SS = MR.SS;
  const seg = (id, opts, cur) => `<div class="seg" id="${id}" role="group">${opts.map(([v, l, t]) =>
    `<button type="button" data-v="${v}" aria-pressed="${v === cur}" title="${MR.esc(t)}">${l}</button>`).join('')}</div>`;
  MR.$('#searchview').innerHTML = `
    <div class="sv-bar"><div class="column">
      <div class="sv-input">
        <input id="sv-q" type="search" placeholder="Search your libraries..." aria-label="Search" autocomplete="off" spellcheck="false">
        <button class="btn primary" id="sv-go" type="button">Search</button></div>
      <div class="sv-opts">
        <div class="grp"><span class="label">Find</span>${seg('sv-mode', [
          ['hybrid', 'Both', 'Meaning and keywords together (best for most searches)'],
          ['semantic', 'Meaning', 'Related passages even when they share no words with your query'],
          ['keyword', 'Keywords', 'Exact words and names, ranked by how well they match']], SS.mode)}</div>
        <div class="grp"><span class="label">In</span>${seg('sv-scope', [
          ['all', 'All', 'Everything'], ['code', 'Code', 'Source code only'], ['docs', 'Docs', 'Documentation only']], SS.scope)}</div>
        <label class="check" title="Every word you type must appear in the passage"><input type="checkbox" id="sv-all" ${SS.all ? 'checked' : ''}> all words</label>
        <div class="grp"><span class="label">Path</span><input class="input small" id="sv-path" placeholder="contains..." value="${MR.esc(SS.path)}" aria-label="Only paths containing" spellcheck="false"></div>
        <a href="#" class="reset" id="sv-reset" title="Meaning + keywords, everything, every ticked library" hidden>reset to defaults</a>
      </div>
      <div class="sv-opts" id="sv-langs" style="margin-top:8px" hidden></div>
      <div class="sv-status" id="sv-status"></div>
      <div class="sv-related" id="sv-related" hidden></div>
    </div></div>
    <div class="sv-results" id="sv-results"><div class="column" id="sv-list"></div></div>`;

  const q = MR.$('#sv-q');
  q.value = SS.q;
  q.addEventListener('input', () => { SS.q = q.value; MR.searchSoon(); });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(SS.timer); SS.committed = true; MR.runSearch(); }
    else if (e.key === 'Escape') { q.value = ''; SS.q = ''; MR.runSearch(); }
  });
  MR.$('#sv-go').addEventListener('click', () => { clearTimeout(SS.timer); SS.committed = true; MR.runSearch(); });
  const bindSeg = (id, key) => MR.$$('#' + id + ' button').forEach((b) => b.addEventListener('click', () => {
    SS[key] = b.dataset.v;
    MR.syncSearchControls(); MR.runSearch();
  }));
  bindSeg('sv-mode', 'mode');
  bindSeg('sv-scope', 'scope');
  MR.$('#sv-all').addEventListener('change', (e) => { SS.all = e.target.checked; MR.syncSearchControls(); MR.runSearch(); });
  MR.$('#sv-path').addEventListener('input', (e) => { SS.path = e.target.value; MR.syncSearchControls(); MR.searchSoon(); });
  MR.$('#sv-reset').addEventListener('click', (e) => { e.preventDefault(); MR.resetSearchOptions(); });
  MR.$('#sv-list').addEventListener('click', MR.hitClick);
  MR.$('#sv-status').addEventListener('click', (e) => {          // the links the status line offers ("Tick all", "Add a source")
    const a = e.target.closest('[data-sv]');
    if (!a) return;
    e.preventDefault();
    if (a.dataset.sv === 'all') { MR.S.selected = new Set(MR.S.libs.map((l) => l.id)); MR.selectionChanged(); }
    else if (a.dataset.sv === 'add') MR.openAdd('web', MR.S.focus);
  });
  MR.syncSearchControls();
  MR.renderIntro();
};

MR.searchSoon = function () {
  clearTimeout(MR.SS.timer);
  MR.SS.timer = setTimeout(() => MR.runSearch(), 280);
};

/* programming-language chips, from what the ticked libraries actually contain */
MR.updateSearchLangs = function () {
  const box = MR.$('#sv-langs');
  if (!box) return;
  const counts = {};
  MR.S.libs.filter((l) => MR.S.selected.has(l.id)).forEach((l) => Object.entries(l.langs || {}).forEach(([k, v]) => { counts[k] = (counts[k] || 0) + v; }));
  const langs = Object.keys(counts).sort((a, b) => counts[b] - counts[a]).slice(0, 10);
  [...MR.SS.langs].forEach((l) => { if (!langs.includes(l)) MR.SS.langs.delete(l); });   // a language that is no longer in the ticked libraries cannot filter anything
  if (!langs.length || MR.SS.scope === 'docs') { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '<span class="label">Language</span><div class="facets">' + langs.map((l) =>
    `<button type="button" class="chip" data-lang="${MR.esc(l)}" aria-pressed="${MR.SS.langs.has(l)}">${MR.esc(l)}<span class="n">${MR.fmt.n(counts[l])}</span></button>`).join('') + '</div>';
  MR.$$('[data-lang]', box).forEach((b) => b.addEventListener('click', () => {
    const l = b.dataset.lang;
    MR.SS.langs.has(l) ? MR.SS.langs.delete(l) : MR.SS.langs.add(l);
    b.setAttribute('aria-pressed', String(MR.SS.langs.has(l)));
    const reset = MR.$('#sv-reset');
    if (reset) reset.hidden = MR.searchIsDefault();
    MR.runSearch();
  }));
};

MR.searchLibs = () => (MR.SS.only ? [MR.SS.only] : Array.from(MR.S.selected));

MR.updateSearchScope = function () {
  MR.updateSearchLangs();
  const el = MR.$('#sv-status');
  if (el && !MR.SS.q.trim()) { el.innerHTML = MR.scopeText(); MR.renderIntro(); }
};
MR.scopeText = function () {
  if (!MR.S.libs.length) return '<span class="warn">Nothing to search yet. <a href="#" data-sv="add">Add a source</a></span>';
  const t = MR.scopeLabel();
  return t ? 'Searching ' + MR.esc(t) : '<span class="warn">No library is ticked. <a href="#" data-sv="all">Tick all</a>, or tick the ones you want in the sidebar.</span>';
};

MR.renderIntro = function () {
  const list = MR.$('#sv-list');
  if (!list) return;
  if (!MR.S.libs.length) {
    list.innerHTML = '<div class="sv-empty"><b>Nothing to search yet.</b><br>Add a documentation website, a code repository or some documents from the sidebar, then search them here.</div>';
    return;
  }
  list.innerHTML = `<div class="sv-empty"><b>Search without a model.</b>
    <ul style="margin:10px 0 0;padding-left:1.2em;color:var(--muted);line-height:1.7">
      <li>Type words, a question, or a name such as <span class="mono">processBlock</span>. Names are matched exactly and by their parts.</li>
      <li>Put <span class="mono">"quotes"</span> around an exact phrase.</li>
      <li><b>Both</b> combines meaning and keywords. <b>Meaning</b> finds related passages that share no words with your query; <b>Keywords</b> is exact and instant.</li>
      <li>Tick one or several libraries in the sidebar to search them together, or use <b>only</b> to search just one.</li>
      <li>Open a result to read the exact lines, or send it to <b>Ask</b> to have the model explain it.</li>
    </ul><div class="sv-recent" id="sv-recent"></div></div>`;
  if (MR.history) MR.history.fillRecentSearches();      // "Recent searches", when there are any
};

/* ------------------------------------------------------------------ highlighting */
MR.hl = function (text, terms) {
  if (!terms || !terms.length) return MR.esc(text);
  const rx = new RegExp('(' + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
  let out = '', last = 0, m;
  while ((m = rx.exec(text))) {
    if (!m[0]) { rx.lastIndex++; continue; }
    out += MR.esc(text.slice(last, m.index)) + '<mark>' + MR.esc(m[0]) + '</mark>';
    last = m.index + m[0].length;
  }
  return out + MR.esc(text.slice(last));
};

/* ------------------------------------------------------------------ running a search */
MR.runSearch = async function (more) {
  const SS = MR.SS, S = MR.S;
  const status = MR.$('#sv-status'), list = MR.$('#sv-list');
  if (!status) return;
  const q = (MR.$('#sv-q').value || '').trim();
  SS.q = MR.$('#sv-q').value;
  if (!q) { SS.hits = []; SS.data = null; status.innerHTML = MR.scopeText(); MR.renderIntro(); MR.history.afterSearch(null); return; }
  const libs = MR.searchLibs();
  if (!libs.length) { SS.seq++; SS.data = null; status.innerHTML = MR.scopeText(); MR.renderIntro(); return; }
  const seq = ++SS.seq;
  if (!more) SS.offset = 0;
  status.innerHTML = '<span class="spin"></span> searching...';
  try {
    const d = await MR.api('/api/search', { method: 'POST', body: {
      query: q, libraries: libs, mode: SS.mode, match: SS.all ? 'all' : 'any', scope: SS.scope, langs: Array.from(SS.langs),
      path: SS.path.trim(), limit: 20, offset: SS.offset } });
    if (seq !== SS.seq) return;  // a newer search has already replaced this one
    SS.data = d;
    SS.hits = more ? SS.hits.concat(d.hits) : d.hits;
    if (!more) SS.topScore = d.hits.length ? d.hits[0].score : 0;
    SS.offset = d.shown;
    SS.hasMore = d.has_more;
    MR.renderHits();
    if (!more) MR.history.afterSearch(q, d.hits.length);       // remembers the search and shows related ones from the history
  } catch (e) {
    if (seq !== SS.seq) return;
    status.innerHTML = '<span class="err">' + MR.esc(e.message) + '</span>';
  }
};

MR.renderHits = function () {
  const SS = MR.SS, d = SS.data;
  const status = MR.$('#sv-status'), list = MR.$('#sv-list');
  const t = d.debug.timing_ms || {};
  const modeText = { hybrid: 'meaning + keywords', semantic: 'meaning', keyword: 'keywords' }[d.mode];
  const libsFacet = d.facets.libraries.length > 1 || SS.only ? d.facets.libraries : [];
  const chips = libsFacet.length ? '<div class="facets">' + libsFacet.map((f) =>
    `<button type="button" class="chip" data-only="${MR.esc(f.id)}" aria-pressed="${SS.only === f.id}" title="Search only ${MR.esc(f.name)}">${MR.esc(f.name)}<span class="n">${f.count}</span></button>`).join('') + '</div>' : '';
  const warns = (d.debug.warnings || []).map((w) => `<span class="warn">${MR.esc(w)}</span>`).join('');
  status.innerHTML = `<span>${SS.hits.length ? SS.hits.length + (SS.hasMore ? '+' : '') + ' results' : 'No results'}</span>
    <span>${MR.fmt.secs(t.total || 0)}</span><span>${modeText}${d.debug.reranked ? ', reranked' : ''}</span>
    <span>${MR.scopeText()}</span>${chips}${warns}`;
  MR.$$('[data-only]', status).forEach((b) => b.addEventListener('click', () => {
    SS.only = SS.only === b.dataset.only ? null : b.dataset.only;
    MR.syncSearchControls();
    MR.runSearch();
  }));
  if (!SS.hits.length) {
    const tips = [];
    if (SS.all) tips.push('turn off "all words"');
    if (SS.path.trim()) tips.push('clear the path filter');
    if (SS.langs.size || SS.scope !== 'all') tips.push('widen the language / scope filters');
    if (SS.mode === 'keyword') tips.push('try Both, which also finds related wording');
    if (d.debug.pending) tips.push(MR.fmt.n(d.debug.pending) + ' passages are still being embedded, so search by meaning is incomplete');
    list.innerHTML = `<div class="sv-empty"><b>Nothing matched.</b>${tips.length ? '<br>You could ' + tips.join(', or ') + '.' : ''}</div>`;
    return;
  }
  const top = SS.topScore || 1;
  list.innerHTML = SS.hits.map((h, i) => MR.hitHTML(h, i, top, d.terms)).join('') +
    (SS.hasMore ? '<div class="sv-more"><button class="btn" id="sv-more" type="button">Show more results</button></div>' : '');
  const more = MR.$('#sv-more');
  if (more) more.addEventListener('click', () => { more.disabled = true; MR.runSearch(true); });
};

MR.hitHTML = function (h, i, top, terms) {
  const code = h.kind === 'code', r = MR.rangeOf(h.anchor);
  const title = code ? h.title + (r ? ':' + (r[0] === r[1] ? r[0] : r[0] + '-' + r[1]) : '') : h.title;
  const crumb = (h.heading.startsWith(h.title + ' > ') ? h.heading.slice(h.title.length + 3) : h.heading) || h.library;
  const viewable = code || ['markdown', 'text', 'rst'].includes(h.kind);
  const href = h.online_url || h.local_url;
  const gh = h.online_url && /github\.com/.test(h.online_url);
  const rel = Math.max(4, Math.round(100 * h.score / top));
  const why = [h.vec_rank ? `<span class="tag" title="Rank by meaning">meaning #${h.vec_rank}</span>` : '',
               h.kw_rank ? `<span class="tag" title="Rank by keywords">keywords #${h.kw_rank}</span>` : ''].join('');
  return `<article class="hit" data-i="${i}">
    <div class="h-head"><span class="h-n">${h.n}</span><span class="h-title" title="${MR.esc(title)}">${MR.esc(title)}</span>
      ${h.lang ? `<span class="tag code">${MR.esc(h.lang)}</span>` : ''}
      <span class="h-rel" title="Relevance relative to the best result"><span class="relbar"><i style="width:${rel}%"></i></span>${rel}%</span></div>
    <div class="h-path" title="${MR.esc(crumb)}">${MR.esc(crumb)}</div>
    <div class="h-body${code ? ' code' : ''}" data-body>${MR.hl(h.excerpt || h.snippet, terms)}</div>
    <div class="h-foot"><span>${MR.esc(h.library)}</span><span class="why">${why}</span>${h.cosine != null ? `<span title="Cosine similarity of meaning">sim ${h.cosine}</span>` : ''}
      <button type="button" data-act="more">show passage</button>
      ${viewable ? '<button type="button" data-act="view">view source</button>' : ''}
      ${href ? `<a href="${MR.esc(href)}" target="_blank" rel="noopener noreferrer">${gh ? 'GitHub' : h.online_url ? 'open page' : 'open copy'} &#8599;</a>` : ''}
      ${MR.bookmarks.btn(h, 'data-act="bookmark"')}
      <button type="button" data-act="ask">ask about this</button></div>
  </article>`;
};

MR.hitClick = function (e) {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const card = btn.closest('.hit');
  const h = MR.SS.hits[+card.dataset.i];
  const body = MR.$('[data-body]', card);
  if (btn.dataset.act === 'bookmark') {
    MR.bookmarks.toggle(h, btn);
  } else if (btn.dataset.act === 'more') {
    const open = body.classList.toggle('full');
    body.innerHTML = MR.hl(open ? h.text : (h.excerpt || h.snippet), MR.SS.data.terms);
    btn.textContent = open ? 'show less' : 'show passage';
  } else if (btn.dataset.act === 'view') {
    MR.openViewer(h);
  } else if (btn.dataset.act === 'ask') {
    const r = MR.rangeOf(h.anchor);
    const crumb = h.heading.startsWith(h.title + ' > ') ? h.heading.slice(h.title.length + 3) : '';
    // the innermost heading or member name, without the "[2/2]" / "(part 3/4)" markers that split long passages
    const sym = crumb.split(' > ').pop().replace(/\s*(\[\d+\/\d+\]|\(part \d+\/\d+\))\s*$/, '').trim();
    MR.setView('ask');
    const ta = MR.$('#prompt');
    ta.value = h.kind === 'code'
      ? `Explain ${sym || 'this code'} in ${h.title}${r ? ` (lines ${r[0]}-${r[1]})` : ''}.`
      : sym ? `Explain ${sym} from "${h.title}".` : `Explain "${h.title}".`;
    MR.autosize();
    ta.focus();
  }
};

/* `/search words` typed in the Ask box, a suggestion, a related search or an entry of the history jumps here.
   `filters` (from the history) restores the options the search was made with; without them the normal search starts. */
MR.searchFor = function (text, filters) {
  const SS = MR.SS;
  if (filters) {
    Object.assign(SS, MR.SEARCH_DEFAULTS, { langs: new Set(filters.langs || []), only: null });
    SS.mode = filters.mode || SS.mode;
    SS.scope = filters.scope || SS.scope;
    SS.all = filters.match === 'all';
    SS.path = filters.path || '';
    MR.syncSearchControls();
  }
  SS.q = text;
  MR.setView('search', { quiet: !!filters });
  MR.$('#sv-q').value = text;
  clearTimeout(SS.timer);
  SS.committed = true;
  MR.runSearch();
};

/* the options of this search that differ from the normal one: what the history remembers it by */
MR.searchFilters = function () {
  const SS = MR.SS, d = MR.SEARCH_DEFAULTS, f = {};
  if (SS.mode !== d.mode) f.mode = SS.mode;
  if (SS.scope !== d.scope) f.scope = SS.scope;
  if (SS.all) f.match = 'all';
  if (SS.langs.size) f.langs = Array.from(SS.langs).sort();
  if (SS.path.trim()) f.path = SS.path.trim();
  return f;
};
