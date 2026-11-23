'use strict';
/* Bookmarks: sources you saved, in folders you make, shown as a tree with arrows like a file system. A bookmark is saved
   from a source card in an answer, a search result or the source viewer (the bookmark button); in the Bookmarks tab it can be
   opened, renamed, given a note, moved between folders (menu or drag and drop) or removed. The data lives in the app
   (bookmarks.db in the data folder); see perplexify/bookmarks.py and bookmarks_api.py. */

MR.bookmarks = (() => {
  const B = { folders: [], items: [], byKey: new Map(), loaded: false, sort: 'name', q: '', drag: null, forceOpen: new Set() };
  const api = (path, opts) => MR.api('/api/bookmarks' + path, opts);
  const enc = encodeURIComponent;
  const esc = MR.esc;
  const keyOf = (s) => `${s.library_id}|${s.doc_id}|${s.anchor || ''}`;

  const ICON = {
    folder: '<svg class="bm-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    doc: '<svg class="bm-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
    mark: '<svg class="bm-mark" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h10a1 1 0 0 1 1 1v17l-6-4-6 4V4a1 1 0 0 1 1-1z"/></svg>',
    arrow: '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1l5 4-5 4z"/></svg>',
  };

  /* ------------------------------------------------------------------ the data */
  B.load = async function () {
    const d = await api('');
    B.folders = d.folders;
    B.items = d.items;
    B.byKey = new Map(d.items.map((i) => [i.key, i]));
    B.loaded = true;
    B.refreshButtons();
  };
  B.folder = (id) => B.folders.find((f) => f.id === id) || null;
  B.item = (id) => B.items.find((i) => i.id === id) || null;
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true });
  const byTitle = (a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: 'base', numeric: true });
  B.children = (parent) => B.folders.filter((f) => (f.parent_id || null) === (parent || null)).sort(byName);
  B.itemsIn = (parent) => B.items.filter((i) => (i.folder_id || null) === (parent || null)).sort(B.sort === 'new' ? (a, b) => b.created - a.created : byTitle);
  B.count = function (folderId) {                                  // bookmarks in a folder and in every folder below it
    let n = B.items.filter((i) => i.folder_id === folderId).length;
    B.children(folderId).forEach((f) => { n += B.count(f.id); });
    return n;
  };
  B.depthOf = function (folderId) {
    let d = 0, f = B.folder(folderId);
    while (f) { d += 1; f = f.parent_id ? B.folder(f.parent_id) : null; }
    return d;
  };
  B.descendants = function (folderId) {                            // a folder and every folder below it
    const out = [folderId];
    B.children(folderId).forEach((f) => out.push(...B.descendants(f.id)));
    return out;
  };
  B.merge = function (item) {
    const i = B.items.findIndex((x) => x.id === item.id);
    if (i >= 0) B.items[i] = item; else B.items.push(item);
    B.byKey = new Map(B.items.map((x) => [x.key, x]));
    B.refreshButtons();
    B.redraw();
  };
  B.mergeFolder = function (f) {
    const i = B.folders.findIndex((x) => x.id === f.id);
    if (i >= 0) B.folders[i] = f; else B.folders.push(f);
    B.redraw();
  };

  /* ------------------------------------------------------------------ the bookmark button on sources */
  B.titleOf = function (s) {                                       // how a source is named: a code passage with its lines
    const r = MR.rangeOf(s.anchor);
    return s.kind === 'code' && r ? s.title + ':' + (r[0] === r[1] ? r[0] : r[0] + '-' + r[1]) : s.title;
  };
  /* markup of the button for a source; `attrs` is for the page's own click handling (data-bm, data-act="bookmark") */
  B.btn = function (s, attrs = '', cls = '') {
    const on = B.byKey.has(keyOf(s));
    return `<button type="button" class="bm-btn ${cls}" ${attrs} data-bmkey="${esc(keyOf(s))}" aria-pressed="${on}" title="${on ? 'Remove this bookmark' : 'Bookmark this source'}">${ICON.mark}<span>${on ? 'bookmarked' : 'bookmark'}</span></button>`;
  };
  B.refreshButtons = function () {
    MR.$$('.bm-btn[data-bmkey]').forEach((b) => {
      const on = B.byKey.has(b.dataset.bmkey);
      b.setAttribute('aria-pressed', String(on));
      b.title = on ? 'Remove this bookmark' : 'Bookmark this source';
      const label = MR.$('span', b);
      if (label) label.textContent = on ? 'bookmarked' : 'bookmark';
    });
  };

  /* the button was pressed: remove the bookmark, or ask which folder to put it in and save it */
  B.toggle = async function (s, anchor) {
    const saved = B.byKey.get(keyOf(s));
    if (saved) { await B.removeItem(saved, true); return; }
    const choice = await B.pickFolder(anchor, { title: 'Save bookmark in' });
    if (!choice) return;
    try {
      const r = await api('/items', { method: 'POST', body: { library_id: s.library_id, doc_id: s.doc_id, anchor: s.anchor || null,
        title: B.titleOf(s), heading: s.heading || '', snippet: s.snippet || '', folder_id: choice.folderId } });
      B.merge(r.item);
      MR.toast('Bookmarked' + (choice.name ? ' in ' + choice.name : ''), 'ok', 2200);
    } catch (e) { MR.toast(e.message, 'err'); }
  };

  B.removeItem = async function (item, withUndo) {
    try { await api('/items/' + enc(item.id), { method: 'DELETE' }); } catch (e) { MR.toast(e.message, 'err'); return; }
    B.items = B.items.filter((x) => x.id !== item.id);
    B.byKey = new Map(B.items.map((x) => [x.key, x]));
    B.refreshButtons();
    B.redraw();
    if (!withUndo) return;
    const t = MR.el('<div class="toast">Bookmark removed <button type="button" class="btn small quiet" data-undo>Undo</button></div>');
    MR.$('#toast-root').appendChild(t);
    const timer = setTimeout(() => t.remove(), 6000);
    MR.$('[data-undo]', t).addEventListener('click', async () => {
      clearTimeout(timer); t.remove();
      try {
        const r = await api('/items', { method: 'POST', body: { library_id: item.library_id, doc_id: item.doc_id, anchor: item.anchor || null,
          title: item.title, heading: item.heading, snippet: item.snippet, folder_id: item.folder_id && B.folder(item.folder_id) ? item.folder_id : null } });
        B.merge(r.item);
      } catch (e) { MR.toast(e.message, 'err'); }
    });
  };

  /* ------------------------------------------------------------------ choosing a folder: a small menu */
  /* Resolves {folderId, name} (folderId null is the top) or null when closed without choosing. `exclude` are folders that
     cannot be chosen (a folder being moved, and what is inside it). */
  B.pickFolder = function (anchor, opts = {}) {
    return new Promise((resolve) => {
      MR.closePopover();
      const exclude = new Set(opts.exclude || []);
      const rows = [];
      const walk = (parent, depth) => B.children(parent).forEach((f) => { rows.push({ f, depth, off: exclude.has(f.id) }); walk(f.id, depth + 1); });
      walk(null, 0);
      const menu = MR.el(`<div class="menu bm-menu" role="menu"><div class="bm-menu-title">${esc(opts.title || 'Choose a folder')}</div>
        <button type="button" role="menuitem" data-id="">${ICON.folder}<span>Bookmarks <span class="faint">(no folder)</span></span></button>
        ${rows.map((r) => `<button type="button" role="menuitem" data-id="${esc(r.f.id)}" style="--d:${r.depth}" ${r.off ? 'disabled' : ''}>${ICON.folder}<span>${esc(r.f.name)}</span></button>`).join('')}
        <hr><button type="button" role="menuitem" data-new>+ New folder...</button></div>`);
      document.body.appendChild(menu);
      MR.placeNear(menu, anchor, 'left');
      let busy = false;
      const done = (v) => { menu.remove(); document.removeEventListener('mousedown', outside, true); document.removeEventListener('keydown', key, true); resolve(v); };
      const outside = (e) => { if (!busy && !menu.contains(e.target)) done(null); };
      const key = (e) => { if (!busy && e.key === 'Escape') { e.stopPropagation(); done(null); } };
      document.addEventListener('mousedown', outside, true);
      document.addEventListener('keydown', key, true);
      menu.addEventListener('click', async (e) => {
        const b = e.target.closest('button');
        if (!b || b.disabled) return;
        if (b.hasAttribute('data-new')) {
          busy = true;
          const name = await MR.ask1('New folder', 'Name');
          busy = false;
          if (!name) return;
          try { const f = await api('/folders', { method: 'POST', body: { name } }); B.mergeFolder(f); done({ folderId: f.id, name: f.name }); }
          catch (err) { MR.toast(err.message, 'err'); }
          return;
        }
        const id = b.dataset.id;
        done({ folderId: id || null, name: id ? B.folder(id).name : '' });
      });
      const first = MR.$('button:not([disabled])', menu);
      if (first) first.focus();
    });
  };

  /* ------------------------------------------------------------------ the Bookmarks tab */
  B.render = function () {
    const v = MR.$('#bookmarksview');
    if (!v) return;
    v.innerHTML = `
      <div class="sv-bar"><div class="column">
        <div class="sv-input"><input id="bm-q" type="search" placeholder="Filter your bookmarks..." aria-label="Filter bookmarks" autocomplete="off" spellcheck="false">
          <button class="btn primary" id="bm-new" type="button" title="Make a folder at the top of your bookmarks">+ New folder</button></div>
        <div class="sv-opts">
          <div class="grp"><span class="label">Sort</span><div class="seg" id="bm-sort" role="group">
            <button type="button" data-v="name" aria-pressed="true">Name</button><button type="button" data-v="new" aria-pressed="false" title="Bookmarks: newest first">Newest</button></div></div>
          <a href="#" id="bm-open-all">expand all</a><a href="#" id="bm-close-all">collapse all</a>
        </div>
        <div class="sv-status" id="bm-status"></div>
      </div></div>
      <div class="sv-results"><div class="column"><div class="bm-tree" id="bm-tree" role="tree" aria-label="Bookmarks"></div></div></div>`;
    const q = MR.$('#bm-q');
    q.addEventListener('input', () => { B.q = q.value; B.redraw(); });
    q.addEventListener('keydown', (e) => { if (e.key === 'Escape' && q.value) { q.value = ''; B.q = ''; B.redraw(); } });
    MR.$('#bm-new').addEventListener('click', () => B.newFolder(null));
    MR.$$('#bm-sort button').forEach((b) => b.addEventListener('click', () => {
      B.sort = b.dataset.v;
      MR.$$('#bm-sort button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      B.redraw();
    }));
    MR.$('#bm-open-all').addEventListener('click', (e) => { e.preventDefault(); B.openAll(true); });
    MR.$('#bm-close-all').addEventListener('click', (e) => { e.preventDefault(); B.openAll(false); });
    B.wireTree(MR.$('#bm-tree'));
  };

  B.show = async function () {
    try { await B.load(); } catch (e) { MR.toast(e.message, 'err'); }
    B.redraw();
  };

  /* what a query leaves visible: folders (with everything in them) whose name matches, and folders that hold a matching bookmark */
  B.filter = function () {
    const words = B.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return null;
    const hay = (i) => [i.title, i.heading, i.library, i.snippet, i.note].join(' ').toLowerCase();
    const items = new Set(), folders = new Set();
    const nameHit = (f) => words.every((w) => f.name.toLowerCase().includes(w));
    const visit = (f, inherited) => {                              // returns whether anything below (or the folder itself) is shown
      const own = inherited || nameHit(f);
      let any = own;
      B.itemsIn(f.id).forEach((i) => { if (own || words.every((w) => hay(i).includes(w))) { items.add(i.id); any = true; } });
      B.children(f.id).forEach((c) => { if (visit(c, own)) any = true; });
      if (any) folders.add(f.id);
      return any;
    };
    B.children(null).forEach((f) => visit(f, false));
    B.itemsIn(null).forEach((i) => { if (words.every((w) => hay(i).includes(w))) items.add(i.id); });
    return { items, folders };
  };

  B.redraw = function (focusId) {
    const tree = MR.$('#bm-tree'), status = MR.$('#bm-status');
    if (!tree) return;
    const at = document.activeElement;
    const keep = focusId || (at && at.closest && tree.contains(at) ? (at.closest('.bm-row') || {}).dataset?.fid || (at.closest('.bm-row') || {}).dataset?.iid : null);
    const shown = B.filter();
    const rowsHTML = (parent, depth) => {
      let out = '';
      B.children(parent).forEach((f) => {
        if (shown && !shown.folders.has(f.id)) return;
        const open = shown ? true : f.open;
        const inner = open ? rowsHTML(f.id, depth + 1) : '';
        out += `<div class="bm-row bm-folder${open ? ' open' : ''}" role="treeitem" aria-expanded="${open}" data-fid="${esc(f.id)}" draggable="true" style="--d:${depth}" tabindex="-1">
          <button type="button" class="bm-arrow" tabindex="-1" aria-label="${open ? 'Collapse' : 'Expand'} ${esc(f.name)}">${ICON.arrow}</button>${ICON.folder}
          <span class="bm-name" title="${esc(f.name)}">${esc(f.name)}</span><span class="bm-count" title="Bookmarks in this folder and the folders inside it">${B.count(f.id)}</span>
          <span class="bm-tools"><button type="button" class="btn small quiet" data-act="newfolder" title="Make a folder inside this one">+ folder</button><button type="button" class="btn small quiet" data-act="menu" aria-label="More for ${esc(f.name)}" title="Rename, move, delete">&middot;&middot;&middot;</button></span></div>`;
        if (open && inner) out += `<div class="bm-children" style="--cd:${depth}" role="group">${inner}</div>`;
        else if (open && !shown && !B.itemsIn(f.id).length && !B.children(f.id).length) out += `<div class="bm-empty" style="--d:${depth + 1}">Empty folder. Drag bookmarks here.</div>`;
      });
      B.itemsIn(parent).forEach((i) => { if (!shown || shown.items.has(i.id)) out += itemHTML(i, depth); });
      return out;
    };
    const html = rowsHTML(null, 0);
    tree.innerHTML = html || emptyHTML();
    const n = B.items.length, nf = B.folders.length;
    status.innerHTML = `<span>${n} ${n === 1 ? 'bookmark' : 'bookmarks'}</span><span>${nf} ${nf === 1 ? 'folder' : 'folders'}</span>` +
      (shown ? `<span>${shown.items.size} match${shown.items.size === 1 ? '' : 'es'}</span>` : '');
    const first = MR.$('.bm-row', tree);
    if (first) first.tabIndex = 0;
    if (keep) {                                                    // the row that had the keyboard focus keeps it across the redraw
      const again = MR.$(`[data-fid="${keep}"], [data-iid="${keep}"]`, tree);
      if (again) { again.focus({ preventScroll: !focusId }); if (focusId) again.scrollIntoView({ block: 'nearest' }); }
    }
  };

  const emptyHTML = () => B.q.trim()
    ? `<div class="sv-empty"><b>Nothing in your bookmarks matches "${esc(B.q.trim())}".</b></div>`
    : `<div class="sv-empty"><b>No bookmarks yet.</b><br>Press <b>bookmark</b> under a source in an answer, in a search result, or in the source viewer to save it here.
         Then keep them in folders of your own, with arrows to open and close each one.<div class="row" style="margin-top:14px"><button class="btn" type="button" data-act="newfolder-top">+ New folder</button></div></div>`;

  const itemHTML = (i, depth) => {
    const gone = !i.available;
    const crumb = (i.heading || '').split(' > ').slice(1).join(' > ') || i.heading || '';
    const lines = [crumb, i.library].filter(Boolean).join('  ·  ');
    return `<div class="bm-row bm-item${gone ? ' gone' : ''}" role="treeitem" data-iid="${esc(i.id)}" draggable="true" style="--d:${depth}" tabindex="-1">
      <span class="bm-arrow bm-spacer"></span>${ICON.doc}
      <span class="bm-main"><button type="button" class="bm-title" data-act="open" title="${esc(i.title)}">${esc(i.title)}</button>
        <span class="bm-meta" title="${esc(lines)}">${esc(lines)}${i.lang ? ` <span class="tag code">${esc(i.lang)}</span>` : ''}${gone ? ` <span class="tag" title="${i.library_exists ? 'The page or file is no longer in its library' : 'The library was deleted'}">${i.library_exists ? 'not in library' : 'library deleted'}</span>` : ''}${i.note ? ` <span class="bm-note" title="${esc(i.note)}">&#9998; ${esc(i.note.length > 60 ? i.note.slice(0, 59) + '…' : i.note)}</span>` : ''}</span></span>
      <span class="bm-tools">
        <button type="button" class="btn small quiet" data-act="snippet" title="Show the saved passage">passage</button>
        <button type="button" class="btn small quiet" data-act="ask" title="Ask the model about this source">ask</button>
        <button type="button" class="btn small quiet" data-act="menu" aria-label="More for ${esc(i.title)}" title="Note, rename, move, remove">&middot;&middot;&middot;</button></span></div>`;
  };

  /* the "..." menu of a row: what is less often needed than opening it */
  B.rowMenu = function (kind, id, anchor) {
    MR.closePopover();
    const it = kind === 'item' ? B.item(id) : null;
    const entries = kind === 'folder'
      ? [['rename', 'Rename'], ['move', 'Move to...'], ['delete', 'Delete folder']]
      : [...(it && it.online_url ? [['page', 'Open the original page']] : []), ['note', it && it.note ? 'Edit the note...' : 'Add a note...'],
         ['rename', 'Rename'], ['move', 'Move to...'], ['remove', 'Remove bookmark']];
    const menu = MR.el(`<div class="menu bm-menu" role="menu">${entries.map(([k, t]) => `<button type="button" role="menuitem" data-m="${k}">${esc(t)}</button>`).join('')}</div>`);
    document.body.appendChild(menu);
    MR.placeNear(menu, anchor, 'right');
    const close = () => { MR.closePopover(); document.removeEventListener('keydown', key, true); };
    const key = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    document.addEventListener('mousedown', MR._outside, true);
    document.addEventListener('keydown', key, true);
    menu.addEventListener('click', async (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      close();
      const m = b.dataset.m;
      if (kind === 'folder') {
        if (m === 'rename') B.renameFolder(id);
        else if (m === 'delete') B.deleteFolder(id);
        else if (m === 'move') { const c = await B.pickFolder(anchor, { title: 'Move folder into', exclude: B.descendants(id) }); if (c) B.moveFolder(id, c.folderId); }
      } else if (it) {
        if (m === 'page') window.open(it.online_url, '_blank', 'noopener');
        else if (m === 'note') B.editNote(it);
        else if (m === 'rename') B.renameItem(it);
        else if (m === 'remove') B.removeItem(it, true);
        else if (m === 'move') { const c = await B.pickFolder(anchor, { title: 'Move bookmark to' }); if (c) B.moveItem(it.id, c.folderId); }
      }
    });
  };

  /* ------------------------------------------------------------------ doing things */
  B.openAll = async function (open) {
    B.folders.forEach((f) => { f.open = open; });
    B.redraw();
    try { await api('/open-all', { method: 'POST', body: { open } }); } catch (e) { MR.toast(e.message, 'err'); }
  };

  B.toggleFolder = async function (id) {
    const f = B.folder(id);
    if (!f) return;
    f.open = !f.open;
    B.redraw();
    try { await api('/folders/' + enc(id), { method: 'PATCH', body: { open: f.open } }); } catch (e) { MR.toast(e.message, 'err'); }
  };

  B.newFolder = async function (parentId) {
    const name = await MR.ask1(parentId ? 'New folder inside ' + B.folder(parentId).name : 'New folder', 'Name');
    if (!name) return;
    try {
      const f = await api('/folders', { method: 'POST', body: { name, parent_id: parentId } });
      if (parentId) { const p = B.folder(parentId); if (p) p.open = true; }
      B.mergeFolder(f);
      B.redraw(f.id);                                              // the new folder is selected, so it is easy to see where it went
    } catch (e) { MR.toast(e.message, 'err'); }
  };

  B.renameFolder = async function (id) {
    const f = B.folder(id);
    const name = await MR.ask1('Rename folder', 'Name', f.name);
    if (!name || name === f.name) return;
    try { B.mergeFolder(await api('/folders/' + enc(id), { method: 'PATCH', body: { name } })); } catch (e) { MR.toast(e.message, 'err'); }
  };

  B.moveFolder = async function (id, parentId) {
    try { B.mergeFolder(await api('/folders/' + enc(id) + '/move', { method: 'POST', body: { parent_id: parentId } })); }
    catch (e) { MR.toast(e.message, 'err'); }
  };

  B.deleteFolder = async function (id) {
    const f = B.folder(id);
    const ids = B.descendants(id), items = B.items.filter((i) => ids.includes(i.folder_id)).length;
    const inside = items || ids.length > 1
      ? ` with ${ids.length > 1 ? (ids.length - 1) + (ids.length === 2 ? ' folder' : ' folders') + (items ? ' and ' : '') : ''}${items ? items + (items === 1 ? ' bookmark' : ' bookmarks') : ''} inside it` : '';
    if (!(await MR.confirm('Delete folder', `Delete the folder "${f.name}"${inside}? The sources themselves stay in your libraries.`))) return;
    try {
      await api('/folders/' + enc(id), { method: 'DELETE' });
      B.folders = B.folders.filter((x) => !ids.includes(x.id));
      B.items = B.items.filter((x) => !ids.includes(x.folder_id));
      B.byKey = new Map(B.items.map((x) => [x.key, x]));
      B.refreshButtons();
      B.redraw();
    } catch (e) { MR.toast(e.message, 'err'); }
  };

  B.moveItem = async function (id, folderId) {
    try { B.merge((await api('/items/' + enc(id) + '/move', { method: 'POST', body: { folder_id: folderId } }))); }
    catch (e) { MR.toast(e.message, 'err'); return; }
    if (folderId) { const f = B.folder(folderId); if (f) { f.open = true; B.redraw(); } }
  };

  B.renameItem = async function (item) {
    const title = await MR.ask1('Rename bookmark', 'Name', item.title);
    if (!title || title === item.title) return;
    try { B.merge(await api('/items/' + enc(item.id), { method: 'PATCH', body: { title } })); } catch (e) { MR.toast(e.message, 'err'); }
  };

  B.editNote = function (item) {
    const m = MR.modal({ title: 'Note on ' + (item.title.length > 50 ? item.title.slice(0, 49) + '…' : item.title), narrow: true,
      body: `<div class="field"><span class="label">Note</span><textarea class="input" id="bm-note" rows="5" maxlength="2000" placeholder="Why this source matters, what to look for...">${esc(item.note || '')}</textarea></div>`,
      foot: '<button class="btn" data-no type="button">Cancel</button><button class="btn primary" data-yes type="button">Save</button>' });
    m.$('[data-no]').addEventListener('click', () => m.close());
    m.$('[data-yes]').addEventListener('click', async () => {
      try { B.merge(await api('/items/' + enc(item.id), { method: 'PATCH', body: { note: m.$('#bm-note').value } })); m.close(); }
      catch (e) { MR.toast(e.message, 'err'); }
    });
  };

  B.openItem = function (item) {
    if (!item.available) { MR.toast(item.library_exists ? 'That page or file is no longer in its library.' : 'The library of this bookmark was deleted.', 'err'); return; }
    const viewable = item.kind === 'code' || ['markdown', 'text', 'rst'].includes(item.kind);
    if (viewable) { MR.openViewer(item); return; }
    const href = item.online_url || item.local_url;
    if (href) window.open(href, '_blank', 'noopener');
    else MR.toast('Nothing to open for this source.', 'err');
  };

  B.askAbout = function (item) {
    const sym = (item.heading || '').split(' > ').pop().replace(/\s*(\[\d+\/\d+\]|\(part \d+\/\d+\))\s*$/, '').trim();
    MR.setView('ask');
    const ta = MR.$('#prompt');
    ta.value = item.kind === 'code' ? `Explain ${sym || 'this code'} in ${item.title}.` : sym && sym !== item.title ? `Explain ${sym} from "${item.title}".` : `Explain "${item.title}".`;
    MR.autosize();
    ta.focus();
  };

  /* ------------------------------------------------------------------ clicks, keys and drag and drop on the tree */
  B.wireTree = function (tree) {
    tree.addEventListener('click', (e) => {
      const top = e.target.closest('[data-act="newfolder-top"]');
      if (top) { B.newFolder(null); return; }
      const row = e.target.closest('.bm-row');
      if (!row) return;
      const act = e.target.closest('[data-act]');
      if (row.dataset.fid) {
        const id = row.dataset.fid;
        if (!act) { if (!e.target.closest('.bm-name') || e.detail < 2) B.toggleFolder(id); return; }
        if (act.dataset.act === 'newfolder') B.newFolder(id);
        else if (act.dataset.act === 'menu') B.rowMenu('folder', id, act);
        return;
      }
      const item = B.item(row.dataset.iid);
      if (!item) return;
      if (!act) return;
      const what = act.dataset.act;
      if (what === 'open') B.openItem(item);
      else if (what === 'ask') B.askAbout(item);
      else if (what === 'menu') B.rowMenu('item', item.id, act);
      else if (what === 'snippet') {
        const next = row.nextElementSibling;
        if (next && next.classList.contains('bm-snip')) { next.remove(); return; }
        row.insertAdjacentHTML('afterend', `<div class="bm-snip" style="--d:${row.style.getPropertyValue('--d')}">${esc(item.snippet || '(no passage saved)')}</div>`);
      }
    });
    tree.addEventListener('dblclick', (e) => {
      const name = e.target.closest('.bm-folder .bm-name');
      if (name) { e.preventDefault(); B.renameFolder(name.closest('.bm-row').dataset.fid); }
    });
    tree.addEventListener('focusin', (e) => {
      const row = e.target.closest('.bm-row');
      if (!row) return;
      MR.$$('.bm-row', tree).forEach((r) => { r.tabIndex = r === row ? 0 : -1; });
    });
    tree.addEventListener('keydown', (e) => {
      const row = e.target.closest('.bm-row');
      if (!row || e.target !== row) return;
      const rows = MR.$$('.bm-row', tree), at = rows.indexOf(row);
      const go = (r) => { if (r) { e.preventDefault(); r.focus(); } };
      if (e.key === 'ArrowDown') go(rows[at + 1]);
      else if (e.key === 'ArrowUp') go(rows[at - 1]);
      else if (e.key === 'Home') go(rows[0]);
      else if (e.key === 'End') go(rows[rows.length - 1]);
      else if (row.dataset.fid) {
        const f = B.folder(row.dataset.fid);
        if (e.key === 'ArrowRight') { e.preventDefault(); if (!f.open) B.toggleFolder(f.id); else go(rows[at + 1]); }
        else if (e.key === 'ArrowLeft') {
          e.preventDefault();
          if (f.open && !B.q.trim()) B.toggleFolder(f.id); else if (f.parent_id) go(MR.$(`[data-fid="${f.parent_id}"]`, tree));
        }
        else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); B.toggleFolder(f.id); }
        else if (e.key === 'F2') { e.preventDefault(); B.renameFolder(f.id); }
        else if (e.key === 'Delete') { e.preventDefault(); B.deleteFolder(f.id); }
      } else {
        const item = B.item(row.dataset.iid);
        if (!item) return;
        if (e.key === 'ArrowLeft' && item.folder_id) go(MR.$(`[data-fid="${item.folder_id}"]`, tree));
        else if (e.key === 'Enter') { e.preventDefault(); B.openItem(item); }
        else if (e.key === 'F2') { e.preventDefault(); B.renameItem(item); }
        else if (e.key === 'Delete') { e.preventDefault(); B.removeItem(item, true); }
      }
    });

    /* drag a folder or a bookmark onto a folder to put it there, or onto the empty space to bring it to the top */
    const TYPE = 'application/x-perplexify-bookmark';
    let hoverTimer = 0;
    const clearDrop = () => { clearTimeout(hoverTimer); MR.$$('.drop', tree).forEach((r) => r.classList.remove('drop')); tree.classList.remove('drop-root'); };
    const allowed = (folderId) => {                                // can the thing being dragged go into this folder (null: the top)?
      const d = B.drag;
      if (!d) return false;
      if (d.kind === 'item') { const it = B.item(d.id); return !!it && (it.folder_id || null) !== folderId; }
      const f = B.folder(d.id);
      return !!f && (f.parent_id || null) !== folderId && folderId !== d.id && !(folderId && B.descendants(d.id).includes(folderId));
    };
    tree.addEventListener('dragstart', (e) => {
      const row = e.target.closest ? e.target.closest('.bm-row') : null;
      if (!row) return;
      B.drag = row.dataset.fid ? { kind: 'folder', id: row.dataset.fid } : { kind: 'item', id: row.dataset.iid };
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData(TYPE, JSON.stringify(B.drag));
      e.dataTransfer.setData('text/plain', row.querySelector('.bm-name, .bm-title').textContent);
      row.classList.add('dragging');
    });
    tree.addEventListener('dragend', () => { B.drag = null; clearDrop(); MR.$$('.dragging', tree).forEach((r) => r.classList.remove('dragging')); });
    tree.addEventListener('dragover', (e) => {
      if (!B.drag) return;
      const row = e.target.closest('.bm-row');
      const target = row ? (row.dataset.fid || null) : null;
      // over an item or the empty space: the top (a bookmark inside a folder belongs to that folder, so it is its folder that takes the drop)
      let into = target;
      if (row && row.dataset.iid) { const it = B.item(row.dataset.iid); into = it ? it.folder_id || null : null; }
      if (!allowed(into)) { clearDrop(); return; }
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      clearDrop();
      const folderRow = into ? MR.$(`.bm-folder[data-fid="${into}"]`, tree) : null;
      if (folderRow) {
        folderRow.classList.add('drop');
        const f = B.folder(into);
        if (f && !f.open && !B.q.trim()) hoverTimer = setTimeout(() => B.toggleFolder(into), 700);
      } else tree.classList.add('drop-root');
    });
    tree.addEventListener('dragleave', (e) => { if (!tree.contains(e.relatedTarget)) clearDrop(); });
    tree.addEventListener('drop', (e) => {
      const d = B.drag;
      if (!d) return;
      e.preventDefault();
      const row = e.target.closest('.bm-row');
      let into = row ? (row.dataset.fid || null) : null;
      if (row && row.dataset.iid) { const it = B.item(row.dataset.iid); into = it ? it.folder_id || null : null; }
      const ok = allowed(into);                                    // asked while B.drag is still set
      clearDrop();
      B.drag = null;
      if (!ok) return;
      if (d.kind === 'item') B.moveItem(d.id, into); else B.moveFolder(d.id, into);
    });
  };

  /* ------------------------------------------------------------------ start up */
  B.init = function () {
    B.render();
    B.load().then(() => B.redraw(), () => {});
  };

  return B;
})();
