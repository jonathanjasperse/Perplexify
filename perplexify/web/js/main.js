'use strict';
/* Start-up. */
(async function init() {
  // A file dropped anywhere but on the "Add source" drop zone must not replace the page: a browser tab would open the
  // file, and the app's own window has no Back button to return from it.
  const inDropZone = (e) => !!(e.target && e.target.closest && e.target.closest('#ad-drop'));
  window.addEventListener('dragover', (e) => { if (!inDropZone(e)) { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = 'none'; } });
  window.addEventListener('drop', (e) => { if (!inDropZone(e)) e.preventDefault(); });
  MR.renderComposer();
  MR.renderSearchView();
  MR.renderViewTabs();
  MR.wireTranscript();
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); const p = MR.$(MR.S.view === 'search' ? '#sv-q' : '#prompt'); if (p) { p.focus(); if (p.select) p.select(); } }
  });
  window.addEventListener('resize', () => { const b = MR.$('#btn-libs'); if (b) b.style.display = window.matchMedia('(max-width: 860px)').matches ? '' : 'none'; });
  try {
    MR.applyBoot(await MR.api('/api/bootstrap'));
  } catch (e) {
    const fromFile = location.protocol === 'file:';
    MR.$('#transcript').innerHTML = `<div class="column welcome"><h1>Can't reach the app</h1>
      <p>${fromFile ? 'This page was opened straight from a file, so it cannot talk to the app behind it. Start Perplexify (from the Start menu, or <span class="mono">python -m perplexify app</span>) and use the address it opens in your browser.' : MR.esc(e.message)}</p></div>`;
    return;
  }
  MR.syncWindowTheme();   // the window's title bar takes the page's theme
  MR.renderTop();
  MR.renderSide();
  MR.renderWelcomeIfEmpty();
  MR.setView(MR.S.view, { quiet: true });
  MR.updateSearchScope();   // the Search view was drawn before the libraries were known
  MR.updateScope();
  MR.refreshModelInfo();
  // first run: go straight to the one thing that is needed
  if (!MR.S.profile) setTimeout(() => MR.openConnect(), 250);
  MR.pollJobs();
  setInterval(MR.updateScope, 1500);
})();
