'use strict';
/* Start-up. */
(async function init() {
  // A file dropped anywhere but on the "Add source" drop zone must not replace the page: a browser tab would open the
  // file, and the app's own window has no Back button to return from it.
  // (Only files: the app's own drags, such as a bookmark onto a folder, are not files.)
  const hasFiles = (e) => !!(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files'));
  const inDropZone = (e) => !!(e.target && e.target.closest && e.target.closest('#ad-drop'));
  window.addEventListener('dragover', (e) => { if (hasFiles(e) && !inDropZone(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'none'; } });
  window.addEventListener('drop', (e) => { if (hasFiles(e) && !inDropZone(e)) e.preventDefault(); });
  MR.renderComposer();
  MR.renderSearchView();
  MR.renderViewTabs();
  MR.history.init();      // the History view and the suggestions that come from it
  MR.bookmarks.init();    // the Bookmarks view (a tree of folders and saved sources)
  MR.wireTranscript();
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); const p = MR.$(MR.S.view === 'search' ? '#sv-q' : MR.S.view === 'history' ? '#hv-q' : MR.S.view === 'bookmarks' ? '#bm-q' : '#prompt'); if (p) { p.focus(); if (p.select) p.select(); } }
  });
  window.addEventListener('resize', () => {
    const b = MR.$('#btn-libs'); if (b) b.style.display = window.matchMedia('(max-width: 860px)').matches ? '' : 'none';
    if (MR.$('#prompt')) MR.autosize();                    // the wrapping of a long question changes with the width
  });
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
