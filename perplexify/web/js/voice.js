'use strict';
/* Speech to text: the microphone button in every search box (Ask, Search, Bookmarks, History).
   The Web Speech API does the listening; the words land in the box you were typing in and the box
   reacts as if you had typed them, so searching while you talk, suggestions and everything else keep working.
   Chromium does the recognising: it is in the app's own window (WebView2) and in Edge and Chrome.
   Firefox has no speech recognition yet, so there the button explains itself instead of pretending. */

MR.voice = (() => {
  const V = { rec: null, input: null, btn: null, base: '', finals: '', want: false, failing: false };

  /* the browser's own speech recognition, under either of its two names */
  const engine = window.SpeechRecognition || window.webkitSpeechRecognition;
  V.supported = !!engine;

  const ICON_MIC = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5a2.75 2.75 0 0 1 2.75 2.75v5.5a2.75 2.75 0 0 1-5.5 0v-5.5A2.75 2.75 0 0 1 12 3.5z"/>'
    + '<path d="M5.75 11.5a6.25 6.25 0 0 0 12.5 0"/><path d="M12 17.75V20.5"/><path d="M8.75 20.5h6.5"/></svg>';

  /* words that do not end with a space keep one when the next words follow them */
  const glue = (a, b) => !a ? b : !b ? a : (/\s$/.test(a) || /^\s/.test(b) ? a + b : a + ' ' + b);

  /* ------------------------------------------------------------------ the button */
  /* Puts a quiet round microphone straight after the box's input, before any send or other button. */
  V.attach = function (input) {
    if (!input || input.dataset.voice) return null;
    input.dataset.voice = '1';
    const btn = MR.el(`<button type="button" class="mic-btn${V.supported ? '' : ' unsupported'}" aria-pressed="false" aria-label="Speak instead of typing" title="${V.supported ? 'Speak instead of typing' : 'Speech recognition is not available in this browser (the app window, Edge or Chrome have it)'}">${ICON_MIC}</button>`);
    btn.addEventListener('click', () => V.toggle(input, btn));
    input.insertAdjacentElement('afterend', btn);
    return btn;
  };

  /* one button for four boxes: pressing it again stops; starting it elsewhere moves it there */
  V.toggle = function (input, btn) {
    if (!V.supported) { MR.toast('Speech recognition needs Chromium: the app window, Microsoft Edge or Google Chrome.', 'err'); return; }
    if (V.on && V.input === input) { V.stop(); return; }
    V.start(input, btn);
  };

  /* ------------------------------------------------------------------ listening */
  V.start = function (input, btn) {
    if (V.rec) V.stop();
    let rec;
    try { rec = new engine(); }
    catch (e) { MR.toast('Voice input is not available here: ' + e.message, 'err'); return; }

    V.rec = rec; V.input = input; V.btn = btn;
    V.base = input.value ? input.value.replace(/\s+$/, '') + ' ' : '';   // dictate after whatever was typed already
    V.finals = '';
    V.want = true; V.failing = false;

    rec.lang = navigator.language || 'en-US';
    rec.continuous = true;               // carry on through the little pauses between words
    rec.interimResults = true;           // show the words while they are still being guessed
    rec.maxAlternatives = 1;

    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) V.finals = glue(V.finals, r[0].transcript);
        else interim = glue(interim, r[0].transcript);
      }
      V.paint(interim);
    };

    rec.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return;    // a pause: listening picks up again on its own
      V.failing = true; V.want = false;
      const inWindow = MR.S.shell === 'window';                        // WebView2 has no address bar or padlock
      const say = {
        'not-allowed': inWindow
          ? 'The microphone was refused. Check Windows settings (Privacy > Microphone: desktop apps), then press the microphone again.'
          : 'The microphone is blocked for this page. Allow it (the padlock beside the address), then press the microphone again.',
        'service-not-allowed': inWindow
          ? 'The microphone was refused by the window. Open Perplexify in Edge or Chrome instead and allow it there.'
          : 'The microphone was refused. Allow it in the browser settings, then try again.',
        'audio-capture': 'No microphone was found. Plug one in (or choose a different one) and try again.',
        'network': 'Speech recognition needs an internet connection.',
      }[e.error] || 'Voice input failed (' + (e.error || 'unknown reason') + ').';
      MR.toast(say, 'err');
    };

    /* Chromium stops of its own accord after a silence: while the user still wants to dictate, listen again */
    rec.onend = () => {
      if (V.rec !== rec) return;           // stopped or replaced in the meantime
      if (V.want && !V.failing) {
        try { rec.start(); return; }
        catch (e) { /* started twice: carry on to the next end */ }
      }
      V.reset();
    };

    try { rec.start(); }
    catch (e) { V.reset(); MR.toast('Voice input could not start: ' + e.message, 'err'); return; }

    V.on = true;
    btn.classList.add('listening');
    btn.setAttribute('aria-pressed', 'true');
    const box = input.closest('.promptbox, .sv-input');
    if (box) box.classList.add('listening');
  };

  /* the words, into the box; the box answers as if they had been typed */
  V.paint = function (interim) {
    const input = V.input;
    if (!input) return;
    input.value = glue(glue(V.base, V.finals), interim);
    if (typeof MR.autosize === 'function') MR.autosize();
    input.dispatchEvent(new Event('input', { bubbles: true }));
  };

  V.stop = function () {
    V.want = false;
    if (V.rec) { try { V.rec.stop(); } catch (e) { /* already ending */ } }
    V.reset();                             // at once, so the button does not pulse on for another moment
  };

  V.reset = function () {
    if (V.btn) { V.btn.classList.remove('listening'); V.btn.setAttribute('aria-pressed', 'false'); }
    if (V.input) { const box = V.input.closest('.promptbox, .sv-input'); if (box) box.classList.remove('listening'); }
    V.rec = null; V.input = null; V.btn = null;
    V.base = ''; V.finals = '';
    V.on = false; V.want = false; V.failing = false;
  };

  /* ------------------------------------------------------------------ start up */
  /* The four search bars of the interface. Each view is drawn once at startup and never re-drawn,
     so one attach per box lasts the whole session. */
  V.init = function () {
    ['#prompt', '#sv-q', '#bm-q', '#hv-q'].forEach((sel) => V.attach(MR.$(sel)));
  };

  return V;
})();
