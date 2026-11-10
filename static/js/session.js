/* The session screen: record, transcribe, summarise, explain, chat. */
const T = window.I18N;

const root = document.getElementById('session-root');
const sessionId = root.dataset.sessionId;
const params = new URLSearchParams(window.location.search);

let source = root.dataset.source === 'link' ? 'mic' : root.dataset.source;
const deviceId = params.get('device') || '';

const transcriptEl = document.getElementById('transcript');
const transcriptEmpty = document.getElementById('transcript-empty');
const statusEl = document.getElementById('capture-status');
const liveTag = document.getElementById('live-tag');

// ------------------------------------------------------------------ recording

const startBtn = document.getElementById('start-rec');
const stopBtn = document.getElementById('stop-rec');

if (startBtn) {
  const paint = makeWaveformPainter(document.getElementById('waveform'));
  let pending = 0;

  const capture = new Capture({
    onLevel: paint,
    onStatus: (state) => {
      const recording = state === 'recording';
      startBtn.disabled = recording;
      stopBtn.disabled = !recording;
      liveTag.style.display = recording ? '' : 'none';
      statusEl.textContent = recording ? T.listening : T.stopped;
    },
    onClip: async (blob) => {
      pending += 1;
      statusEl.textContent = `${T.transcribing} (${pending})`;
      const ext = (blob.type.split('/')[1] || 'webm').split(';')[0];
      const form = new FormData();
      form.append('file', blob, `clip.${ext}`);
      try {
        const data = await api(`/api/sessions/${sessionId}/transcribe`, {
          method: 'POST',
          body: form,
        });
        if (data.text) { appendSegment(data.text, data.id, data.audio); copilotNudge(); }
      } catch (err) {
        toast(err.message, 'bad');
      } finally {
        pending -= 1;
        if (pending === 0) statusEl.textContent = capture.isRecording ? T.listening : T.stopped;
      }
    },
  });

  startBtn.addEventListener('click', async () => {
    startBtn.disabled = true;
    try {
      await capture.start(source, deviceId);
    } catch (err) {
      startBtn.disabled = false;
      toast(err.message || T.micFailed, 'bad');
    }
  });

  stopBtn.addEventListener('click', () => capture.stop());

  // Switching source mid-session restarts capture with the new one.
  document.querySelectorAll('#source-switch .seg-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const next = btn.dataset.source;
      if (next === source) return;
      const wasRecording = capture.isRecording;
      capture.stop();
      source = next;
      document.querySelectorAll('#source-switch .seg-btn').forEach((b) => {
        b.setAttribute('aria-pressed', String(b.dataset.source === source));
      });
      document.getElementById('source-tag').textContent =
        T.sourceNames[source];
      if (wasRecording) {
        try {
          await capture.start(source, deviceId);
        } catch (err) {
          toast(err.message, 'bad');
        }
      }
    });
  });

  if (params.get('autostart') === '1') {
    startBtn.click();
  }
}

function appendSegment(text, id, audio) {
  if (transcriptEmpty) transcriptEmpty.remove();
  const line = document.createElement('div');
  line.className = 'seg-line is-new' + (audio && root.dataset.replay ? ' has-audio' : '');
  if (id) line.dataset.id = id;
  if (audio) {
    line.dataset.audio = audio;
    line.dataset.offset = '0';
    const hint = document.getElementById('replay-hint');
    if (hint) hint.hidden = false;
  }
  line.innerHTML = `<span class="seg-time">${timeNow()}</span><span class="seg-body"><span class="seg-text"></span><span class="seg-trans" hidden></span></span>`;
  line.querySelector('.seg-text').textContent = text;
  transcriptEl.appendChild(line);
  stickBottom();
  if (translateTarget && id) ensureTranslated();
}

// ---------------------------------------------------------------- playback

// Every line knows which recording it came from and where it starts in it.
// Clicking a line loads that file (if it is not already loaded) and seeks.
const audioEl = document.getElementById('audio');
const player = document.getElementById('player');
const playerToggle = document.getElementById('player-toggle');
const playerSeek = document.getElementById('player-seek');
const playerTime = document.getElementById('player-time');
const playerTotal = document.getElementById('player-total');
const iconPlay = document.getElementById('player-icon-play');
const iconPause = document.getElementById('player-icon-pause');
let playingLine = null;

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

function playLine(line) {
  const src = line.dataset.audio;
  if (!src || !root.dataset.replay) return;
  const offset = Number(line.dataset.offset || 0) / 1000;
  player.hidden = false;
  if (playingLine) playingLine.classList.remove('is-playing');
  playingLine = line;
  line.classList.add('is-playing');

  const seekAndPlay = () => {
    audioEl.currentTime = offset;
    audioEl.play().catch((err) => toast(err.message, 'bad'));
  };
  if (audioEl.dataset.src !== src) {
    audioEl.dataset.src = src;
    audioEl.src = src;
    audioEl.addEventListener('loadedmetadata', seekAndPlay, { once: true });
    audioEl.load();
  } else {
    seekAndPlay();
  }
}

transcriptEl.addEventListener('click', (event) => {
  const line = event.target.closest('.seg-line.has-audio');
  if (!line || window.getSelection().toString()) return;
  playLine(line);
});

if (audioEl) {
  playerToggle.addEventListener('click', () => {
    if (!audioEl.src) return;
    if (audioEl.paused) audioEl.play(); else audioEl.pause();
  });
  audioEl.addEventListener('play', () => { iconPlay.hidden = true; iconPause.hidden = false; });
  audioEl.addEventListener('pause', () => { iconPlay.hidden = false; iconPause.hidden = true; });
  audioEl.addEventListener('ended', () => { if (playingLine) playingLine.classList.remove('is-playing'); });
  audioEl.addEventListener('loadedmetadata', () => { playerTotal.textContent = fmtTime(audioEl.duration); });
  audioEl.addEventListener('timeupdate', () => {
    playerTime.textContent = fmtTime(audioEl.currentTime);
    if (audioEl.duration) playerSeek.value = Math.round((audioEl.currentTime / audioEl.duration) * 1000);
    // Move the highlight along as playback crosses into the next line of
    // the same recording.
    const now = audioEl.currentTime * 1000;
    let best = null;
    transcriptEl.querySelectorAll('.seg-line.has-audio').forEach((l) => {
      if (l.dataset.audio !== audioEl.dataset.src) return;
      const off = Number(l.dataset.offset || 0);
      if (off <= now + 250 && (!best || off > Number(best.dataset.offset || 0))) best = l;
    });
    if (best && best !== playingLine) {
      if (playingLine) playingLine.classList.remove('is-playing');
      playingLine = best;
      best.classList.add('is-playing');
    }
  });
  playerSeek.addEventListener('input', () => {
    if (audioEl.duration) audioEl.currentTime = (playerSeek.value / 1000) * audioEl.duration;
  });
}

// ------------------------------------------------------------- translation
//
// Two passes, so the translation is both instant and clean:
//
//   Pass 1 (live, per line): the moment a line arrives it is translated on its
//   own and shown under it, with no waiting for the sentence to finish, so the
//   translation never lags behind the lecture. Not saved on the server.
//
//   Pass 2 (sentence): once consecutive lines make a complete sentence (they end
//   in sentence punctuation, or enough lines have piled up) the whole sentence is
//   translated together and REPLACES the rough per-line pieces with one clean
//   line. For a sentence recorded live across several 6-second clips, the English
//   fragments are joined back into the one sentence that was spoken. This pass is
//   cached, so reopening the note is instant.
//
// Changing the language re-runs both passes for the whole transcript.

const translateSelect = document.getElementById('translate-select');
let translateTarget = '';
let lineQueue = [];             // single line els awaiting the instant pass
let unitQueue = [];             // arrays of line els that form a complete sentence
let translateBusy = false;
let translateRetry = null;
let translateSweepTimer = null;
const SENTENCE_END = /[.?!\u3002\u2026\uFF01\uFF1F][)"'\u201d\u2019\]]?\s*$/;
const UNITS_PER_REQUEST = 10;
const LINES_PER_REQUEST = 10;
// Merge/translate a run as one sentence after this many lines even without
// punctuation. The instant per-line pass already covers latency, so this can
// wait a little for real sentence ends instead of chopping every few lines.
const MAX_UNIT_LINES = 6;
const TRANSLATE_CONCURRENCY = 2;

try { translateTarget = localStorage.getItem('translateTarget') || ''; } catch { /* private mode */ }
if (translateSelect) {
  if (translateTarget && [...translateSelect.options].some((o) => o.value === translateTarget)) {
    translateSelect.value = translateTarget;
  } else {
    translateTarget = '';
  }
  translateSelect.addEventListener('change', () => {
    translateTarget = translateSelect.value;
    try { localStorage.setItem('translateTarget', translateTarget); } catch { /* ignore */ }
    lineQueue = []; unitQueue = [];
    applyTranslationMode();
    if (translateTarget) startTranslateSweep(); else stopTranslateSweep();
  });
  applyTranslationMode();
  if (translateTarget) startTranslateSweep();
}

// Keep the newest line pinned to the bottom while recording, but only when the
// reader is already at the bottom. If they scrolled up to re-read, leave them
// there instead of yanking the page down on every new line.
let autoFollow = true;
function pageScroller() { return document.scrollingElement || document.documentElement; }
function nearPageBottom() { const el = pageScroller(); return el.scrollHeight - (el.scrollTop + el.clientHeight) < 200; }
window.addEventListener('scroll', () => { autoFollow = nearPageBottom(); }, { passive: true });
function stickBottom() { if (autoFollow) window.scrollTo({ top: pageScroller().scrollHeight }); }

// Lines merged into a sentence are dropped from view (class u-gone) but kept in
// the DOM, so they never re-enter the passes below.
function transcriptLines() {
  return [...transcriptEl.querySelectorAll('.seg-line[data-id]:not(.u-gone)')];
}
function isRecording() {
  try { return !!(capture && capture.isRecording); } catch { return false; }
}
function segText(l) { return (l.querySelector('.seg-text').textContent || '').trim(); }

// Split the visible lines into complete sentence units and a trailing tail. A
// unit closes at sentence punctuation or at MAX_UNIT_LINES. The still-open tail
// is kept separate while recording (its lines get the instant pass); once
// recording stops, or for a static transcript, the tail is closed as a unit too.
function closedUnitsAndTail(lines, final) {
  const units = [];
  let cur = [];
  for (const l of lines) {
    cur.push(l);
    if (SENTENCE_END.test(segText(l)) || cur.length >= MAX_UNIT_LINES) { units.push(cur); cur = []; }
  }
  if (cur.length && final) { units.push(cur); cur = []; }
  return { units, tail: cur };
}

function applyTranslationMode() {
  if (!translateTarget) {
    lineQueue = []; unitQueue = [];
    transcriptLines().forEach((l) => { const t = l.querySelector('.seg-trans'); t.hidden = true; t.classList.remove('is-loading'); });
    return;
  }
  // A line already carrying a translation for this language (cached, or rendered
  // by the server) counts as done, so it is not queued again.
  transcriptLines().forEach((l) => {
    const t = l.querySelector('.seg-trans');
    if (t.dataset.lang === translateTarget) {
      t.hidden = !t.textContent;
      t.classList.remove('is-loading');
      if (!t.dataset.stage) t.dataset.stage = 'unit';
    }
  });
  ensureTranslated();
}

// Queue whatever is not yet translated into the current language. Idempotent:
// safe to call on each new clip and from the periodic sweep.
function ensureTranslated() {
  if (!translateTarget) return;
  const { units, tail } = closedUnitsAndTail(transcriptLines(), !isRecording());

  // Pass 2 - complete sentences.
  for (const unit of units) {
    const last = unit[unit.length - 1];
    const lt = last.querySelector('.seg-trans');
    if (lt.dataset.stage === 'unit' && lt.dataset.lang === translateTarget) continue;
    lt.hidden = false; lt.classList.add('is-loading');
    if (lt.dataset.lang !== translateTarget) lt.textContent = '\u2026';
    if (!unitQueue.some((u) => u[u.length - 1] === last)) unitQueue.push(unit);
  }

  // Pass 1 - the live tail, still forming a sentence: translate each line now.
  for (const l of tail) {
    const t = l.querySelector('.seg-trans');
    if (t.dataset.lang === translateTarget) continue; // already shown (line or sentence)
    t.hidden = false; t.classList.add('is-loading');
    if (t.dataset.lang !== translateTarget) t.textContent = '\u2026';
    if (!lineQueue.includes(l)) lineQueue.push(l);
  }

  drainTranslateQueue();
}

async function drainTranslateQueue() {
  if (translateBusy || !translateTarget) return;
  translateBusy = true;
  let hadError = false;
  try {
    while ((unitQueue.length || lineQueue.length) && translateTarget && !hadError) {
      const target = translateTarget;
      const reqs = [];
      for (let c = 0; c < TRANSLATE_CONCURRENCY && (unitQueue.length || lineQueue.length); c++) {
        if (unitQueue.length) reqs.push({ type: 'unit', batch: unitQueue.splice(0, UNITS_PER_REQUEST) });
        else reqs.push({ type: 'line', batch: lineQueue.splice(0, LINES_PER_REQUEST) });
      }
      await Promise.all(reqs.map(async (req) => {
        const groups = req.type === 'unit'
          ? req.batch.map((u) => u.map((l) => l.dataset.id).filter(Boolean))
          : req.batch.map((l) => [l.dataset.id]);
        let data;
        try {
          data = await withRetries(() => translateFetch(groups, target, req.type === 'line'), 2);
        } catch (err) {
          if (req.type === 'unit') unitQueue.unshift(...req.batch); else lineQueue.unshift(...req.batch);
          hadError = true;                 // never drop work
          if (!translateRetry) translateRetry = setTimeout(() => { translateRetry = null; drainTranslateQueue(); }, 4000);
          return;
        }
        if (target !== translateTarget) return;
        if (req.type === 'unit') req.batch.forEach((u) => applyUnit(u, data.translations, target));
        else req.batch.forEach((l) => applyLine(l, data.translations, target));
      }));
    }
  } finally {
    translateBusy = false;
  }
}

// Instant pass: show the rough translation under one line, unless the clean
// sentence translation has already landed there (never downgrade it).
function applyLine(line, translations, target) {
  const t = line.querySelector('.seg-trans');
  t.classList.remove('is-loading');
  if (t.dataset.stage === 'unit' && t.dataset.lang === target) return;
  const tr = translations[line.dataset.id];
  if (tr) { t.textContent = tr; t.dataset.lang = target; t.dataset.stage = 'line'; t.hidden = false; }
  else { t.hidden = true; }
  stickBottom();
}

// Sentence pass: show one clean translation. For a live sentence spoken across
// several lines, also join the English fragments back into the single sentence
// and fold the earlier lines away, keeping the first line's time and audio on the
// surviving line so click-to-replay still starts at the sentence.
function applyUnit(unit, translations, target) {
  const last = unit[unit.length - 1];
  const full = translations[last.dataset.id] || '';
  const mergeable = unit.length > 1 && unit.every((l) => l.classList.contains('is-new'));

  if (mergeable) {
    const first = unit[0];
    last.querySelector('.seg-text').textContent = unit.map(segText).join(' ').replace(/\s+/g, ' ').trim();
    last.querySelector('.seg-time').textContent = first.querySelector('.seg-time').textContent;
    if (first.dataset.audio) {
      last.dataset.audio = first.dataset.audio;
      last.dataset.offset = first.dataset.offset || '0';
      last.classList.add('has-audio');
    }
    for (let i = 0; i < unit.length - 1; i++) {
      unit[i].classList.add('u-gone');
      unit[i].classList.remove('has-audio');
    }
  } else {
    for (let i = 0; i < unit.length - 1; i++) {
      const t = unit[i].querySelector('.seg-trans');
      t.textContent = ''; t.dataset.lang = target; t.dataset.stage = 'unit'; t.hidden = true;
    }
  }

  const t = last.querySelector('.seg-trans');
  t.classList.remove('is-loading');
  t.dataset.lang = target; t.dataset.stage = 'unit';
  if (full) { t.textContent = full; t.hidden = false; }
  else { t.textContent = ''; t.hidden = true; }
  stickBottom();
}

function startTranslateSweep() {
  if (translateSweepTimer) return;
  translateSweepTimer = setInterval(() => { if (translateTarget) ensureTranslated(); }, 6000);
}
function stopTranslateSweep() {
  if (translateSweepTimer) { clearInterval(translateSweepTimer); translateSweepTimer = null; }
}

// Hard time limit: a server that hangs (no reply, no error) must not freeze the
// queue. On timeout it aborts and throws, which the retry/requeue path handles.
// `ephemeral` marks the instant per-line pass, which the server does not save.
async function translateFetch(groups, target, ephemeral = false, ms = 20000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    return await api(`/api/sessions/${sessionId}/translate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ groups, target, persist: !ephemeral }), signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}

async function withRetries(fn, tries) {
  for (let i = 0; ; i++) {
    try { return await fn(); }
    catch (err) { if (i >= tries) throw err; await new Promise((r) => setTimeout(r, 800 * (i + 1))); }
  }
}

// ------------------------------------------------------------------- importing

const importPanel = document.getElementById('import-panel');
if (importPanel) {
  const message = document.getElementById('import-message');
  const bar = document.getElementById('import-bar');
  const timer = setInterval(async () => {
    try {
      const job = await api(`/api/sessions/${sessionId}/progress`);
      if (job.state === 'idle' || job.state === 'done') {
        clearInterval(timer);
        importPanel.style.display = 'none';
        // Reload only if we are still on the importing screen; a finished note
        // already shows its transcript, so never reload it (that looped).
        if (job.state === 'done' && importPanel && importPanel.offsetParent !== null
            && !transcriptEl.querySelector('.seg-line')) {
          window.location.reload();
        }
        return;
      }
      importPanel.style.display = '';
      message.textContent = job.message || T.processing;
      if (job.total) bar.style.width = `${Math.round((job.done / job.total) * 100)}%`;
      if (job.state === 'error') {
        clearInterval(timer);
        importPanel.style.display = 'none';
        toast(job.message, 'bad');
      }
    } catch {
      clearInterval(timer);
    }
  }, 1500);
}

// --------------------------------------------------------------------- title

const titleInput = document.getElementById('session-title');
let titleTimer;
titleInput.addEventListener('input', () => {
  clearTimeout(titleTimer);
  titleTimer = setTimeout(() => {
    api(`/api/sessions/${sessionId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: titleInput.value.trim() || T.untitled }),
    }).catch(() => {});
  }, 700);
});

// ------------------------------------------------------------------- summary

const summaryCard = document.getElementById('summary-card');
const summaryBody = document.getElementById('summary-body');
const keywordsEl = document.getElementById('keywords');
const kwPanel = document.getElementById('kw-panel');
const summaryBtn = document.getElementById('make-summary');

if (summaryBody.textContent.trim()) {
  summaryBody.innerHTML = renderMarkdown(summaryBody.textContent);
}

// Two views of the same session: the full notes and the cram sheet.
const examBody = document.getElementById('exam-body');
const examTab = document.getElementById('exam-tab');
const notesHint = document.getElementById('notes-hint');
if (examBody.textContent.trim()) {
  examBody.innerHTML = renderMarkdown(examBody.textContent);
} else {
  examTab.hidden = true;
}
let showTab = function (which) {
  document.querySelectorAll('#notes-tabs .seg-btn').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tab === which)));
  summaryBody.hidden = which !== 'notes';
  examBody.hidden = which !== 'exam';
  notesHint.textContent = which === 'exam' ? T.examHint : T.notesHint;
};
document.getElementById('notes-tabs').addEventListener('click', (e) => {
  const b = e.target.closest('.seg-btn');
  if (b) showTab(b.dataset.tab);
});

summaryBtn.addEventListener('click', async () => {
  summaryBtn.disabled = true;
  const original = summaryBtn.textContent;
  summaryBtn.textContent = T.summarizing;
  try {
    const data = await api(`/api/sessions/${sessionId}/summary`, { method: 'POST' });
    summaryBody.innerHTML = renderMarkdown(data.summary);
    examBody.innerHTML = renderMarkdown(data.exam_sheet || '');
    rawNotes = { notes: data.summary || '', exam: data.exam_sheet || '' };
    examTab.hidden = !data.exam_sheet;
    showTab('notes');
    summaryCard.style.display = '';
    renderKeywords(data.keywords);
    if (data.title) titleInput.value = data.title;
    summaryCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    toast(T.summarized, 'ok');
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    summaryBtn.disabled = false;
    summaryBtn.textContent = original;
  }
});

function renderKeywords(list) {
  keywordsEl.innerHTML = '';
  (list || []).forEach((kw) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'kw';
    btn.dataset.kw = kw;
    btn.setAttribute('aria-expanded', 'false');
    btn.textContent = kw;
    keywordsEl.appendChild(btn);
  });
}

keywordsEl.addEventListener('click', async (event) => {
  const btn = event.target.closest('.kw');
  if (!btn) return;

  // Clicking the open keyword again closes the panel.
  if (btn.getAttribute('aria-expanded') === 'true') {
    btn.setAttribute('aria-expanded', 'false');
    kwPanel.style.display = 'none';
    return;
  }
  keywordsEl.querySelectorAll('.kw').forEach((b) => b.setAttribute('aria-expanded', 'false'));
  btn.setAttribute('aria-expanded', 'true');

  const kw = btn.dataset.kw;
  const notes = window.KEYWORD_NOTES || {};
  kwPanel.style.display = '';

  // Seen before — render the saved note without a round trip.
  if (notes[kw]) {
    kwPanel.innerHTML = renderMarkdown(notes[kw]);
    return;
  }

  kwPanel.textContent = T.loadingExplanation;
  try {
    const data = await api(`/api/sessions/${sessionId}/keyword?q=${encodeURIComponent(kw)}`);
    notes[kw] = data.explanation;
    kwPanel.innerHTML = renderMarkdown(data.explanation);
  } catch (err) {
    kwPanel.textContent = err.message;
  }
});

// -------------------------------------------------------------- notes editor

// The notes are the user's: the active tab opens as markdown, and saving
// replaces just that field. Regenerating later overwrites it, and says so.
const editBtn = document.getElementById('edit-notes');
const editor = document.getElementById('notes-editor');
const textarea = document.getElementById('notes-textarea');
let rawNotes = { notes: window.RAW_NOTES || '', exam: window.RAW_EXAM || '' };
let activeTab = 'notes';
const _showTab = showTab;
showTab = function (which) { activeTab = which; editor.hidden = true; summaryBody.hidden = false; _showTab(which); };

if (editBtn) {
  editBtn.addEventListener('click', () => {
    textarea.value = rawNotes[activeTab] || '';
    summaryBody.hidden = true;
    examBody.hidden = true;
    editor.hidden = false;
    textarea.style.height = Math.min(600, Math.max(240, textarea.scrollHeight)) + 'px';
    textarea.focus();
  });
  document.getElementById('edit-cancel').addEventListener('click', () => showTab(activeTab));
  document.getElementById('edit-save').addEventListener('click', async () => {
    const value = textarea.value;
    const field = activeTab === 'exam' ? 'exam_sheet' : 'summary';
    try {
      await api(`/api/sessions/${sessionId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: value }) });
      rawNotes[activeTab] = value;
      (activeTab === 'exam' ? examBody : summaryBody).innerHTML = renderMarkdown(value);
      showTab(activeTab);
      toast(T.saved, 'ok');
    } catch (err) { toast(err.message, 'bad'); }
  });
}

const notesLang = document.getElementById('notes-lang');
if (notesLang) notesLang.addEventListener('change', async () => {
  try {
    await api(`/api/sessions/${sessionId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes_lang: notesLang.value }) });
    toast(T.notesLangSaved, 'ok');
  } catch (err) { toast(err.message, 'bad'); }
});

// ---------------------------------------------------------------------- chat

const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');
const chatLog = document.getElementById('chat-log');
const chatSend = document.getElementById('chat-send');

chatForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = chatInput.value.trim();
  if (!question) return;

  const empty = document.getElementById('chat-empty');
  if (empty) empty.remove();

  addMessage('user', question);
  chatInput.value = '';
  chatSend.disabled = true;

  const thinking = addMessage('assistant', '…');
  try {
    const data = await api(`/api/sessions/${sessionId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question }),
    });
    thinking.querySelector('.msg-body').textContent = data.reply;
  } catch (err) {
    thinking.querySelector('.msg-body').textContent = err.message;
  } finally {
    chatSend.disabled = false;
    chatInput.focus();
  }
});

function addMessage(role, text) {
  const el = document.createElement('div');
  el.className = 'msg' + (role === 'user' ? ' msg-user' : '');
  el.innerHTML = `<span class="msg-role">${role === 'user' ? T.me : T.ai}</span><div class="msg-body"></div>`;
  el.querySelector('.msg-body').textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

// -------------------------------------------------------------------- export

function exportMarkdown() {
  const lines = [`# ${titleInput.value}`, ''];

  const summary = summaryBody.textContent.trim();
  if (summary) lines.push(`## ${T.exportSummary}`, '', summary, '');
  const exam = examBody.textContent.trim();
  if (exam) lines.push(`## ${T.exportExam}`, '', exam, '');

  const keywords = [...keywordsEl.querySelectorAll('.kw')].map((b) => b.textContent);
  if (keywords.length) lines.push(`## ${T.exportKeywords}`, '', keywords.join(', '), '');

  lines.push(`## ${T.exportTranscript}`, '');
  transcriptEl.querySelectorAll('.seg-line:not(.u-gone)').forEach((line) => {
    lines.push(`[${line.querySelector('.seg-time').textContent}] ${line.querySelector('.seg-text').textContent}`);
    const trans = line.querySelector('.seg-trans');
    if (trans && !trans.hidden && trans.textContent.trim() && trans.textContent !== '…') {
      lines.push(`    ${trans.textContent}`);
    }
  });

  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${titleInput.value.replace(/[^\w가-힣 -]/g, '') || 'session'}.md`;
  a.click();
  URL.revokeObjectURL(url);
}

// Export menu
const exportBtn = document.getElementById('export-btn');
const exportMenu = document.getElementById('export-menu');
if (exportBtn) {
  exportBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !exportMenu.hidden;
    exportMenu.hidden = open;
    exportBtn.setAttribute('aria-expanded', String(!open));
  });
  document.addEventListener('click', () => { exportMenu.hidden = true; exportBtn.setAttribute('aria-expanded', 'false'); });
  exportMenu.addEventListener('click', (e) => e.stopPropagation());
  document.getElementById('export-md').addEventListener('click', () => { exportMarkdown(); exportMenu.hidden = true; });
  document.getElementById('export-print').addEventListener('click', () => { exportMenu.hidden = true; window.print(); });
}

// ------------------------------------------------------------------ speakers

// Renaming a speaker is a per-session label, so it saves as you type.
document.querySelectorAll('.speaker-name').forEach((input) => {
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      api(`/api/sessions/${sessionId}/speakers`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: input.dataset.label, name: input.value.trim() }),
      }).catch((err) => toast(err.message, 'bad'));
    }, 700);
  });
});

// -------------------------------------------------------------------- folder

// Filing this session into a folder saves immediately; the folder page then
// includes it in cross-recording chat.
const folderSelect = document.getElementById('folder-select');
if (folderSelect) {
  folderSelect.addEventListener('change', async () => {
    try {
      await api(`/api/sessions/${sessionId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder_id: folderSelect.value || null }),
      });
      toast(folderSelect.value ? T.filed : T.unfiled, 'ok');
    } catch (err) {
      toast(err.message, 'bad');
    }
  });
}

// -------------------------------------------------------------------- slides

// A PDF of the slides sits beside the transcript, and its text goes into the
// notes the next time they are generated.
const slidesBtn = document.getElementById('slides-btn');
const slidesFile = document.getElementById('slides-file');
const slidesPanel = document.getElementById('slides-panel');
const slidesFrame = document.getElementById('slides-frame');
const slidesName = document.getElementById('slides-name');
const grid = document.getElementById('session-grid');

function openSlides() {
  if (slidesFrame.getAttribute('src') === 'about:blank') slidesFrame.src = slidesBtn.dataset.url + '#view=FitH';
  slidesPanel.hidden = false;
  grid.classList.add('has-slides');
}
function closeSlides() {
  slidesPanel.hidden = true;
  grid.classList.remove('has-slides');
}
if (slidesBtn) slidesBtn.addEventListener('click', () => {
  if (slidesBtn.dataset.has) { slidesPanel.hidden ? openSlides() : closeSlides(); }
  else slidesFile.click();
});
if (slidesBtn) document.getElementById('slides-replace').addEventListener('click', () => slidesFile.click());
if (slidesBtn) document.getElementById('slides-close').addEventListener('click', closeSlides);
if (slidesBtn) slidesFile.addEventListener('change', async () => {
  const file = slidesFile.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  slidesBtn.disabled = true;
  slidesBtn.textContent = T.uploading;
  try {
    const data = await api(`/api/sessions/${sessionId}/attachment`, { method: 'POST', body: form });
    slidesBtn.dataset.has = '1';
    slidesBtn.title = data.name;
    slidesName.textContent = data.name;
    slidesFrame.src = data.url + '?v=' + Date.now() + '#view=FitH';
    openSlides();
    toast(data.chars ? T.slidesAttached : T.slidesNoText, data.chars ? 'ok' : 'bad');
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    slidesBtn.disabled = false;
    slidesBtn.textContent = slidesBtn.dataset.has ? T.viewSlides : T.attachSlides;
    slidesFile.value = '';
  }
});
if (slidesBtn) document.getElementById('slides-remove').addEventListener('click', async () => {
  if (!confirm(T.slidesRemoveConfirm)) return;
  try {
    await api(`/api/sessions/${sessionId}/attachment`, { method: 'DELETE' });
    closeSlides();
    slidesBtn.dataset.has = '';
    slidesBtn.title = '';
    slidesBtn.textContent = T.attachSlides;
    slidesFrame.src = 'about:blank';
  } catch (err) { toast(err.message, 'bad'); }
});

// ------------------------------------------------------- meeting copilot

// In a live meeting, suggest what the user could say next. Runs only when the
// toggle is on; debounced so a burst of clips makes one call, not many.
const copilotToggle = document.getElementById('copilot-toggle');
const copilotCard = document.getElementById('copilot-card');
const copilotReply = document.getElementById('copilot-reply');
const copilotGloss = document.getElementById('copilot-gloss');
const copilotHint = document.getElementById('copilot-hint');
let copilotOn = false;
let copilotTimer = null;
let copilotBusy = false;

function copilotNudge() {
  if (!copilotOn) return;
  clearTimeout(copilotTimer);
  // Wait for a short lull in speech before asking, so we answer a finished
  // question rather than a half-sentence.
  copilotTimer = setTimeout(runCopilot, 2500);
}

async function runCopilot() {
  if (!copilotOn || copilotBusy) return;
  copilotBusy = true;
  try {
    const data = await api(`/api/sessions/${sessionId}/copilot`, { method: 'POST' });
    const reply = (data.reply || '').trim();
    if (!reply || reply === '(listening)') {
      copilotCard.hidden = true;
      if (copilotHint) { copilotHint.hidden = false; copilotHint.textContent = T.copilotListening; }
      return;
    }
    if (copilotHint) copilotHint.hidden = true;
    copilotCard.hidden = false;
    copilotReply.textContent = reply;
    copilotReply.classList.remove('is-wait');
    copilotGloss.textContent = data.gloss || '';
  } catch (err) {
    if (err.upgrade) { window.location.href = err.upgrade; return; }
    toast(err.message, 'bad');
  } finally {
    copilotBusy = false;
  }
}

if (copilotToggle) {
  copilotToggle.addEventListener('change', () => {
    copilotOn = copilotToggle.checked;
    if (copilotOn) {
      if (copilotHint) { copilotHint.hidden = false; copilotHint.textContent = T.copilotThinking; }
      runCopilot();
    } else {
      clearTimeout(copilotTimer);
      if (copilotHint) copilotHint.hidden = false;
    }
  });
  const copyBtn = document.getElementById('copilot-copy');
  const againBtn = document.getElementById('copilot-again');
  if (copyBtn) copyBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(copilotReply.textContent).then(() => toast(T.copilotCopied, 'ok')).catch(() => {});
  });
  if (againBtn) againBtn.addEventListener('click', runCopilot);
}
