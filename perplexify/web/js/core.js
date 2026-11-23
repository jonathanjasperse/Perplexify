'use strict';
/* Shared state and helpers. Everything the other files use hangs off the MR object. */
const MR = {
  S: {
    boot: null, platform: 'other', version: '',
    libs: [], selected: new Set(), busy: new Set(),
    shell: 'browser',       // where this page is shown: 'window' (the app's own window), 'browser' or 'server' (told by /api/bootstrap)
    focus: null,            // the library new sources are added to (click a library name in the sidebar)
    view: 'ask',            // 'ask' (a model answers) or 'search' (read the passages yourself)
    profiles: [], profile: null, catalog: [], embedders: [], presets: [], settings: null,
    jobs: [], dismissed: new Set(),
    history: [],            // [{role, content}] sent with each question so follow-ups work
    chat: { id: null, title: '', turns: 0 },    // the saved conversation on screen (see history.js); id is null until its first answer is saved
    streaming: false, abort: null,
    ctx: { window: 0, used: 0, breakdown: null, source: '', maxContext: null, maxOutput: null, actual: false, loading: false },
  },
};

/* ------------------------------------------------------------------ small helpers */
MR.$ = (sel, root = document) => root.querySelector(sel);
MR.$$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
MR.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
MR.el = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
MR.sleep = (ms) => new Promise((r) => setTimeout(r, ms));

MR.fmt = {
  n(n) {
    if (n == null || isNaN(n)) return '-';
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'k';
    return String(Math.round(n));
  },
  int(n) { return (n == null ? 0 : n).toLocaleString(); },
  pct(x) { return x <= 0 ? '0%' : x < 0.1 ? '<0.1%' : x < 10 ? x.toFixed(1) + '%' : Math.round(x) + '%'; },
  bytes(b) {
    if (!b) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return b.toFixed(b >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
  },
  secs(ms) { return ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + ' s'; },
  ago(ts) {
    if (!ts) return 'never';
    const d = Date.now() / 1000 - ts;
    if (d < 90) return 'just now';
    if (d < 5400) return Math.round(d / 60) + ' min ago';
    if (d < 129600) return Math.round(d / 3600) + ' h ago';
    return Math.round(d / 86400) + ' days ago';
  },
};

MR.store = {
  get(k, d) { try { const v = localStorage.getItem('perplexify.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('perplexify.' + k, JSON.stringify(v)); } catch (e) { /* private window: carry on without */ } },
};

MR.api = async function (path, opts = {}) {
  const { method = 'GET', body, signal, raw } = opts;
  const init = { method, signal, headers: {} };
  if (body !== undefined) {
    if (raw) { init.body = body; } else { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  }
  let res;
  try { res = await fetch(path, init); } catch (e) { throw new Error('Could not reach the Perplexify app. Is it still running?'); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const d = data && (data.detail || data.error);
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : 'Request failed (' + res.status + ')');
  }
  return data;
};

MR.toast = function (msg, kind = '', ms = 4500) {
  const t = MR.el(`<div class="toast ${kind}">${MR.esc(msg)}</div>`);
  MR.$('#toast-root').appendChild(t);
  setTimeout(() => t.remove(), ms);
};

MR.copy = async function (text, label = 'Copied') {
  try { await navigator.clipboard.writeText(text); MR.toast(label, 'ok', 1500); }
  catch (e) { MR.toast('Could not copy. Select the text and press Ctrl+C.', 'err'); }
};

/* ------------------------------------------------------------------ theme */
MR.setTheme = function (t) {
  if (t !== 'light' && t !== 'dark') return;
  document.documentElement.dataset.theme = t;
  MR.store.set('theme', t);
  try { localStorage.setItem('perplexify.theme', t); } catch (e) { /* see store */ }
  MR.$$('[data-theme-btn]').forEach((b) => b.setAttribute('aria-pressed', b.dataset.themeBtn === t ? 'true' : 'false'));
  MR.syncWindowTheme();
};
MR.theme = () => document.documentElement.dataset.theme;

/* In the app's own window the title bar follows the theme; the window cannot see the page's theme, so tell it. */
MR.syncWindowTheme = function () {
  if (MR.S.shell !== 'window') return;
  fetch('/api/window/theme', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ theme: MR.theme() }) }).catch(() => { /* the title bar keeps its colour */ });
};

/* answer and title font: a plain sans by default (like ChatGPT and Claude); a serif is available as a choice */
MR.setRead = function (r) {
  r = r === 'serif' ? 'serif' : 'sans';
  document.documentElement.dataset.read = r;
  try { localStorage.setItem('perplexify.read', r); } catch (e) { /* private window: carry on without */ }
};
MR.read = () => document.documentElement.dataset.read || 'sans';

/* ------------------------------------------------------------------ context gauge maths */
MR.RING_C = 2 * Math.PI * 13;
MR.gauge = function () {
  const c = MR.S.ctx;
  const frac = c.window > 0 ? Math.min(1, c.used / c.window) : 0;
  const shown = c.used > 0 ? Math.max(frac, 0.014) : 0;  // a sliver is always visible once anything is used
  return { frac, shown, pct: frac * 100, level: frac >= 0.9 ? 'err' : frac >= 0.75 ? 'warn' : '' };
};

/* ------------------------------------------------------------------ markdown (safe: everything is escaped first) */
MR.md = function (src) {
  const blocks = [];
  let s = String(src || '').replace(/\r\n?/g, '\n');
  // fenced code first, so nothing inside is interpreted; an unterminated fence (while streaming) runs to the end
  s = s.replace(/```([^\n`]*)\n([\s\S]*?)(?:```|$)/g, (m, info, code) => {
    blocks.push({ lang: (info || '').trim().split(/\s+/)[0] || '', code: code.replace(/\n$/, '') });
    return '\n\u0000B' + (blocks.length - 1) + '\u0000\n';
  });
  s = MR.esc(s);
  const lines = s.split('\n');
  const out = [];

  const inline = (t) => {
    const codes = [], links = [];
    t = t.replace(/`([^`\n]+)`/g, (m, c) => { codes.push(c); return '\u0001' + (codes.length - 1) + '\u0001'; });
    t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, txt, url) => { links.push({ txt, url }); return '\u0002' + (links.length - 1) + '\u0002'; });
    t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
         .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>')
         .replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
    t = t.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, (m, nums) =>
      nums.split(/\s*,\s*/).map((n) => '<button class="cite" data-n="' + n + '" type="button">' + n + '</button>').join(''));
    t = t.replace(/\u0002(\d+)\u0002/g, (m, k) => '<a href="' + links[k].url + '" target="_blank" rel="noopener noreferrer">' + links[k].txt + '</a>');
    t = t.replace(/\u0001(\d+)\u0001/g, (m, k) => '<code>' + codes[k] + '</code>');
    return t;
  };

  const codeBlock = (b) => '<div class="code"><div class="code-h"><span>' + MR.esc(b.lang || 'text') +
    '</span><button type="button" data-copy>copy</button></div><pre><code>' + MR.esc(b.code) + '</code></pre></div>';

  const isBlockStart = (l) => /^\u0000B\d+\u0000$/.test(l) || /^#{1,4}\s/.test(l) || /^\s*([-*+]|\d+[.)])\s+/.test(l) ||
    /^&gt;\s?/.test(l) || /^\s*([-*_])(\s*\1){2,}\s*$/.test(l);

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = line.match(/^\u0000B(\d+)\u0000$/))) { out.push(codeBlock(blocks[+m[1]])); i++; continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { out.push('<h' + m[1].length + '>' + inline(m[2]) + '</h' + m[1].length + '>'); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
    if (/^&gt;\s?/.test(line)) {
      const q = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) { q.push(lines[i].replace(/^&gt;\s?/, '')); i++; }
      out.push('<blockquote>' + inline(q.join(' ')) + '</blockquote>');
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const items = [];
      while (i < lines.length) {
        const mm = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (mm) { items.push({ indent: mm[1].length, ordered: /\d/.test(mm[2]), text: mm[3] }); i++; continue; }
        if (items.length && /^\s{2,}\S/.test(lines[i]) && !/^\u0000B/.test(lines[i].trim())) { items[items.length - 1].text += ' ' + lines[i].trim(); i++; continue; }
        break;
      }
      let html = ''; const stack = [];
      for (const it of items) {
        const tag = it.ordered ? 'ol' : 'ul';
        while (stack.length && it.indent < stack[stack.length - 1].indent) html += '</li></' + stack.pop().tag + '>';
        if (!stack.length || it.indent > stack[stack.length - 1].indent) { html += '<' + tag + '>'; stack.push({ indent: it.indent, tag }); }
        else html += '</li>';
        html += '<li>' + inline(it.text);
      }
      while (stack.length) html += '</li></' + stack.pop().tag + '>';
      out.push(html);
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
      let html = '<table><thead><tr>' + cells(line).map((c) => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>';
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { html += '<tr>' + cells(lines[i]).map((c) => '<td>' + c + '</td>').join('') + '</tr>'; i++; }
      out.push(html + '</tbody></table>');
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !(para.length && isBlockStart(lines[i]))) { para.push(lines[i].trim()); i++; }
    out.push('<p>' + inline(para.join(' ')) + '</p>');
  }
  return out.join('\n');
};
