'use strict';
/* Speech to text: the microphone button in every search box (Ask, Search, Bookmarks, History).

   The app listens and recognises on this computer (perplexify/dictation.py: a Whisper model, no key, no internet). The page
   records the microphone as plain 16 kHz samples, stops by itself after a pause (or when the button is pressed again),
   sends them to /api/dictation/transcribe and puts the words in the box you were typing in. The box reacts as if you had
   typed them, so suggestions and live search keep working.

   The browser's own speech recognition is only a fallback for copies without the speech engine: in the app's window
   (WebView2) it cannot reach the cloud service Chromium uses, which is why it reported "needs an internet connection". */

MR.voice = (() => {
  const V = { rec: null, input: null, btn: null, base: '', finals: '', want: false, failing: false, on: false, busy: false, mode: '', st: null };

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && AudioCtx);
  /* the browser's own speech recognition, under either of its two names (the fallback) */
  const engine = window.SpeechRecognition || window.webkitSpeechRecognition;
  V.supported = canRecord || !!engine;

  const RATE = 16000;
  const SILENCE_MS = 1800;        // stop this long after the last words
  const NO_SPEECH_MS = 12000;     // ... or when nothing was said at all for this long
  const MIN_SPEECH_RMS = 0.012;   // the quietest level that counts as speech (a noise floor is measured too)

  const ICON_MIC = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5a2.75 2.75 0 0 1 2.75 2.75v5.5a2.75 2.75 0 0 1-5.5 0v-5.5A2.75 2.75 0 0 1 12 3.5z"/>'
    + '<path d="M5.75 11.5a6.25 6.25 0 0 0 12.5 0"/><path d="M12 17.75V20.5"/><path d="M8.75 20.5h6.5"/></svg>';

  /* words that do not end with a space keep one when the next words follow them */
  const glue = (a, b) => !a ? b : !b ? a : (/\s$/.test(a) || /^\s/.test(b) ? a + b : a + ' ' + b);

  const inWindow = () => MR.S.shell === 'window';          // WebView2 has no address bar or padlock

  /* ------------------------------------------------------------------ the button */
  /* Puts a quiet round microphone straight after the box's input, before any send or other button. */
  V.attach = function (input) {
    if (!input || input.dataset.voice) return null;
    input.dataset.voice = '1';
    const btn = MR.el(`<button type="button" class="mic-btn${V.supported ? '' : ' unsupported'}" aria-pressed="false" aria-label="Speak instead of typing" title="${V.supported ? 'Speak instead of typing' : 'Speech recognition is not available here'}">${ICON_MIC}</button>`);
    btn.addEventListener('click', () => V.toggle(input, btn));
    input.insertAdjacentElement('afterend', btn);
    return btn;
  };

  /* one button for four boxes: pressing it again stops; starting it elsewhere moves it there */
  V.toggle = async function (input, btn) {
    if (!V.supported) { MR.toast('Speech recognition is not available in this window.', 'err'); return; }
    if (V.busy) return;                                      // still turning the last recording into words
    if (V.on && V.input === input) { V.stop(); return; }
    if (V.on) V.stop();
    if (await V.localAvailable()) { V.startLocal(input, btn); return; }
    if (engine) { V.startBrowser(input, btn); return; }
    MR.toast('Speech recognition is not installed in this copy of Perplexify.', 'err');
  };

  /* ------------------------------------------------------------------ on this computer */
  V.localAvailable = async function () {
    if (!canRecord) return false;
    if (!V.st) {
      try { V.st = await MR.api('/api/dictation'); } catch (e) { V.st = { available: false }; }
    }
    return !!V.st.available;
  };

  /* the speech model is shipped with the installer; from the source it is downloaded once, as a job in the sidebar */
  V.ensureModel = async function () {
    if (V.st && V.st.model_ready) return true;
    try { V.st = await MR.api('/api/dictation'); } catch (e) { /* keep what we know */ }
    if (V.st && V.st.model_ready) return true;
    try {
      const r = await MR.api('/api/dictation/prepare', { method: 'POST' });
      if (r.ready) { V.st.model_ready = true; return true; }
      MR.toast(`Downloading the speech model (about ${V.st.model_mb || 145} MB, only once). Press the microphone again when it has finished.`, 'ok', 9000);
      MR.pollJobs();
    } catch (e) { MR.toast(e.message, 'err', 7000); }
    return false;
  };

  V.startLocal = async function (input, btn) {
    if (!(await V.ensureModel())) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      MR.toast(e && (e.name === 'NotFoundError' || e.name === 'OverconstrainedError')
        ? 'No microphone was found. Plug one in (or choose a different one) and try again.'
        : inWindow()
          ? 'The microphone was refused. Check Windows settings (Privacy > Microphone: desktop apps), then press the microphone again.'
          : 'The microphone is blocked for this page. Allow it (the padlock beside the address), then press the microphone again.', 'err', 7000);
      return;
    }
    let ctx;
    try { ctx = new AudioCtx({ sampleRate: RATE }); } catch (e) { ctx = new AudioCtx(); }   // otherwise the samples are resampled below
    try { await ctx.resume(); } catch (e) { /* it starts running with the microphone */ }
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const mute = ctx.createGain(); mute.gain.value = 0;      // keeps the graph running without playing the microphone back
    const r = { stream, ctx, src, proc, mute, chunks: [], samples: 0, spoke: false, floor: null, blocks: 0, started: performance.now(), lastVoice: 0 };

    proc.onaudioprocess = (e) => {
      if (V.rec !== r) return;
      const x = e.inputBuffer.getChannelData(0);
      r.chunks.push(new Float32Array(x)); r.samples += x.length;
      let sum = 0; for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
      const rms = Math.sqrt(sum / x.length), now = performance.now();
      if (r.blocks < 4) r.floor = r.floor == null ? rms : Math.min(r.floor, rms);   // the room's own noise: the quietest of the first blocks
      r.blocks++;
      // speech is clearly louder than the room, but the limit never goes above 0.03, so starting to talk at once still counts
      if (rms > Math.max(MIN_SPEECH_RMS, Math.min((r.floor || 0) * 3, 0.03))) { r.spoke = true; r.lastVoice = now; }
      const seconds = r.samples / ctx.sampleRate, max = (V.st && V.st.max_seconds) || 120;
      if ((r.spoke && now - r.lastVoice > SILENCE_MS) || (!r.spoke && now - r.started > NO_SPEECH_MS) || seconds > max - 2) V.stop();
    };
    src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);

    V.rec = r; V.mode = 'local'; V.input = input; V.btn = btn;
    V.base = input.value ? input.value.replace(/\s+$/, '') + ' ' : '';   // dictate after whatever was typed already
    V.finals = ''; V.on = true;
    btn.classList.add('listening'); btn.setAttribute('aria-pressed', 'true');
    const box = input.closest('.promptbox, .sv-input');
    if (box) box.classList.add('listening');
  };

  /* stop listening, then turn the recording into words */
  V.finishLocal = async function () {
    const r = V.rec, input = V.input, btn = V.btn;
    V.rec = null; V.on = false;
    try { r.proc.onaudioprocess = null; r.proc.disconnect(); r.src.disconnect(); r.mute.disconnect(); } catch (e) { /* already apart */ }
    r.stream.getTracks().forEach((t) => t.stop());
    const rate = r.ctx.sampleRate;
    try { r.ctx.close(); } catch (e) { /* closed already */ }
    if (btn) { btn.classList.remove('listening'); btn.classList.add('busy'); }
    const box = input && input.closest('.promptbox, .sv-input');
    if (box) box.classList.remove('listening');

    if (!r.spoke || r.samples < rate * 0.3) { V.reset(); MR.toast('No speech was heard.', 'ok', 2500); return; }
    V.busy = true;
    try {
      const pcm = V.to16k(r.chunks, r.samples, rate);
      const q = '?language=' + encodeURIComponent((navigator.language || '').slice(0, 2));
      const out = await MR.api('/api/dictation/transcribe' + q, { method: 'POST', body: pcm.buffer, raw: true });
      if (input && input.isConnected && out.text) {
        V.input = input; V.finals = out.text;
        V.paint('');
        input.focus();
      } else if (!out.text) {
        MR.toast('No words were recognised. Try again a little closer to the microphone.', 'ok', 3500);
      }
    } catch (e) {
      MR.toast(e.message, 'err', 7000);
    } finally {
      V.reset();
    }
  };

  /* the samples of all blocks as one array at 16 kHz (a straight copy when the browser gave us 16 kHz already) */
  V.to16k = function (chunks, total, rate) {
    const all = new Float32Array(total);
    let at = 0; chunks.forEach((c) => { all.set(c, at); at += c.length; });
    if (Math.abs(rate - RATE) < 1) return all;
    const n = Math.floor(total * RATE / rate), out = new Float32Array(n), k = rate / RATE;
    for (let i = 0; i < n; i++) {
      const p = i * k, j = Math.floor(p), f = p - j;
      out[i] = all[j] * (1 - f) + (all[Math.min(j + 1, total - 1)] * f);
    }
    return out;
  };

  /* ------------------------------------------------------------------ the browser's own recognition (fallback) */
  V.startBrowser = function (input, btn) {
    let rec;
    try { rec = new engine(); }
    catch (e) { MR.toast('Voice input is not available here: ' + e.message, 'err'); return; }

    V.rec = rec; V.mode = 'browser'; V.input = input; V.btn = btn;
    V.base = input.value ? input.value.replace(/\s+$/, '') + ' ' : '';
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
      const say = {
        'not-allowed': inWindow()
          ? 'The microphone was refused. Check Windows settings (Privacy > Microphone: desktop apps), then press the microphone again.'
          : 'The microphone is blocked for this page. Allow it (the padlock beside the address), then press the microphone again.',
        'service-not-allowed': 'The microphone was refused. Allow it in the browser settings, then try again.',
        'audio-capture': 'No microphone was found. Plug one in (or choose a different one) and try again.',
        'network': 'This browser sends speech to an online service and could not reach it.',
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
    if (V.mode === 'local' && V.rec) { V.finishLocal(); return; }   // listening ends now; the words arrive a moment later
    V.want = false;
    if (V.rec) { try { V.rec.stop(); } catch (e) { /* already ending */ } }
    V.reset();                             // at once, so the button does not pulse on for another moment
  };

  V.reset = function () {
    if (V.btn) { V.btn.classList.remove('listening', 'busy'); V.btn.setAttribute('aria-pressed', 'false'); }
    if (V.input) { const box = V.input.closest('.promptbox, .sv-input'); if (box) box.classList.remove('listening'); }
    V.rec = null; V.input = null; V.btn = null; V.mode = '';
    V.base = ''; V.finals = '';
    V.on = false; V.busy = false; V.want = false; V.failing = false;
  };

  /* ------------------------------------------------------------------ start up */
  /* The four search bars of the interface. Each view is drawn once at startup and never re-drawn,
     so one attach per box lasts the whole session. */
  V.init = function () {
    ['#prompt', '#sv-q', '#bm-q', '#hv-q'].forEach((sel) => V.attach(MR.$(sel)));
    V.localAvailable();                    // asks the app once, so the first press does not wait for it
  };

  return V;
})();
