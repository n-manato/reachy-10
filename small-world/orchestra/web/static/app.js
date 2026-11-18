/* The console: commands out, state in, one clock for everything on screen.
 *
 * The server owns the show; this file owns the *appearance* of the show, and
 * the two are kept apart on purpose. Every button is a POST that returns the
 * new state, every change in state arrives on one event stream, and nothing
 * here guesses -- a pressed button changes nothing on screen until the server
 * says it did.
 *
 * The clock is the one interesting part. Snapshots arrive about once a second
 * and the screen draws sixty times in between, so the page runs its own clock
 * and uses the snapshots only to correct it. T0 is kept in `performance.now()`
 * milliseconds, which is monotonic and immune to the wall clock being set, and
 * corrected by a twelfth of the error per snapshot -- enough to hold a 144
 * second piece, gentle enough that no correction is ever visible as a jump.
 * A disagreement larger than half a second is a new take rather than drift, so
 * that snaps.
 */

import { Stage } from './stage.js';
import { noteName } from './palette.js';

const el = (id) => document.getElementById(id);

const VOLUME_STEP = { ArrowUp: 3, ArrowDown: -3, ArrowRight: 1, ArrowLeft: -1 };
/* Matching `VOLUME_STEP` in orchestra/cli.py: coarse on up/down, fine on
 * left/right. The same hands use both front ends. */

const BUSY = new Set(['opening', 'arming', 'syncing', 'countdown', 'playing',
                      'gap', 'stopping']);
/* Must match `State.BUSY` in runner.py -- it decides whether the big button
 * means play or stop. */

const IDLE_HIDE_MS = 4000;

const ui = {
  title: el('title'), subtitle: el('subtitle'),
  play: el('btn-play'), loop: el('btn-loop'), estop: el('btn-estop'),
  full: el('btn-full'), audio: el('btn-audio'),
  volume: el('volume'), volumeOut: el('volume-out'),
  volumeGroup: el('volume-group'), volumeTarget: el('volume-target'),
  progress: el('progress'), fill: el('progress-fill'), head: el('progress-head'),
  sections: el('progress-sections'),
  elapsed: el('elapsed'), total: el('total'),
  fleet: el('fleet'), stateLine: el('state-line'), banner: el('banner'),
  badgeWake: el('badge-wake'), badgeMode: el('badge-mode'),
  labels: el('labels'), preview: el('preview'), keepawake: el('keepawake'),
};

const clock = { t0: null, lastT: null, take: undefined };
let snap = null;
let stageData = null;
let stage = null;
let audioOn = false;
let volumeLocal = null;     // held while a finger is on the slider
let lastIdle = performance.now();
let chipEls = new Map();
let labelEls = new Map();
let hitAt = new Map();      // robot id -> page time of its last strike

/* ------------------------------------------------------------------ clock */

function programTime() {
  if (clock.t0 === null) return null;
  return (performance.now() - clock.t0) / 1000;
}

/* Correct the page's clock against a snapshot.
 *
 * The hard reset is the delicate part. It used to fire whenever the derived
 * T0 moved by more than half a second, which sounds like a clock jump and is
 * not: a snapshot is handled on the main thread, so when a frame takes most
 * of a second -- a weak GPU, a software renderer, a big scene -- the message
 * is read that much after it was sent, and the lateness lands in the estimate
 * as if the clock had leapt. Six spurious resets in one take, each of them
 * rewinding the strike pointers and re-firing every halo at once.
 *
 * Lateness only ever makes the estimate *later*, and easing averages it out.
 * So reset only on the things that really are discontinuities -- the first
 * snapshot, a new take, or the clock running backwards -- and let everything
 * else ease. */
function syncClock(t, take) {
  if (t === null || t === undefined) {
    clock.t0 = null; clock.lastT = null; clock.take = take;
    return;
  }
  const target = performance.now() - t * 1000;
  const newTake = take !== undefined && take !== clock.take;
  const wentBack = clock.lastT !== null && t < clock.lastT - 0.25;
  if (clock.t0 === null || newTake || wentBack) {
    clock.t0 = target;
    resetHits();
  } else {
    clock.t0 += (target - clock.t0) / 12;
  }
  clock.lastT = t;
  clock.take = take;
}

function resetHits() {
  hitAt.clear();
  if (stage) stage.rewind();
}

/* --------------------------------------------------------------- commands */

async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : null,
    });
    if (!res.ok) { showBanner(`${path}: ${res.status} ${await res.text()}`); return; }
    apply(await res.json());
  } catch (e) {
    showBanner(`cannot reach the server: ${e.message}`);
  }
}

const playOrStop = () => post(snap && BUSY.has(snap.state) ? '/api/stop' : '/api/play');
const toggleLoop = () => post('/api/loop', { on: !(snap && snap.loop) });

function nudgeVolume(delta) {
  const base = volumeLocal !== null ? volumeLocal : (snap ? snap.volume : 50);
  setVolume(Math.max(0, Math.min(100, base + delta)));
}

function setVolume(value) {
  volumeLocal = value;
  paintVolume(value);
  if (audioOn) applyAudioGain(value);
  post('/api/volume', { value });
}

/* The slider is the one control a person drags, so it is also the one that can
 * fire thirty events a second. The server coalesces (VolumeController holds a
 * 100 ms floor), but there is no reason to make it: one request per frame at
 * most, and the last value always wins. */
let volumePending = null;
let volumeTimer = 0;
function queueVolume(value) {
  volumeLocal = value;
  paintVolume(value);
  if (audioOn) applyAudioGain(value);
  volumePending = value;
  if (volumeTimer) return;
  volumeTimer = setTimeout(() => {
    volumeTimer = 0;
    if (volumePending !== null) { post('/api/volume', { value: volumePending }); }
    volumePending = null;
  }, 60);
}

/* ------------------------------------------------------------------ state */

function apply(next) {
  const prev = snap;
  snap = next;
  syncClock(next.t, next.take);

  document.body.classList.toggle('running', BUSY.has(next.state));
  ui.loop.setAttribute('aria-pressed', String(!!next.loop));
  ui.play.querySelector('.label').textContent = BUSY.has(next.state) ? 'Stop' : 'Play';
  ui.play.setAttribute('aria-label', BUSY.has(next.state)
    ? 'Stop (the next play starts from the top)' : 'Play');
  ui.progress.classList.toggle('live', next.t !== null);

  if (volumeLocal === null) paintVolume(next.volume);
  if (canPreview(next)) {
    ui.badgeMode.hidden = false;
    ui.badgeMode.className = 'badge rehearsal';
    ui.badgeMode.textContent = 'REHEARSAL — NO ROBOTS';
    ui.audio.hidden = false;
  } else {
    ui.badgeMode.hidden = false;
    ui.badgeMode.className = 'badge';
    ui.badgeMode.textContent =
      `${next.robots.filter((r) => r.live).length} / ${next.robots.length} ROBOTS`;
    /* Not merely hidden: a fleet-mode page stops being able to play anything,
     * whatever state the button was left in when the mode changed. */
    ui.audio.hidden = true;
    if (audioOn) stopPreview();
  }
  paintVolumeTarget(next);

  paintState(next);
  paintFleet(next);
  syncAudio(next, prev);
}

/* The slider means two different things in the two modes, and on a wall
 * console nobody is going to guess which. So it says so. */
function paintVolumeTarget(s) {
  if (canPreview(s)) {
    /* No robots exist in this mode, so the tag must not claim any. The only
     * thing the slider can reach here is the browser's own playback. */
    ui.volumeTarget.textContent = 'PC PREVIEW';
    ui.volumeTarget.classList.toggle('preview', audioOn);
    ui.volumeGroup.title = audioOn
      ? 'Volume of the preview playing in this browser'
      : 'Rehearsal. Turn on PC preview to hear it at this level';
    return;
  }
  const n = s.robots.filter((r) => r.live).length || s.robots.length;
  ui.volumeTarget.textContent = `${n} ROBOTS`;
  ui.volumeTarget.classList.remove('preview');
  ui.volumeGroup.title =
    `Volume of ${n} robots' own speakers — not this browser's`;
}

function paintVolume(value) {
  ui.volume.value = String(value);
  ui.volume.style.setProperty('--pct', `${value}%`);
  ui.volumeOut.textContent = String(value);
}

const STATE_TEXT = {
  offline: 'idle — nothing connected yet',
  opening: 'connecting to the agents…',
  arming: 'arming — torque on, moving to the start pose',
  syncing: 'syncing clocks…',
  countdown: 'starting',
  playing: 'playing',
  gap: 'next take in',
  stopping: 'stopping',
  ready: 'ready',
  error: 'error',
};

function paintState(s) {
  const bits = [];
  bits.push(STATE_TEXT[s.state] || s.state);
  if (s.detail) bits.push(s.detail);
  if (s.state === 'gap' && s.next_in !== null) bits.push(`${s.next_in.toFixed(0)}s`);
  if (s.takes_done) {
    bits.push(`${s.takes_done} take${s.takes_done > 1 ? 's' : ''} played`);
  }
  if (s.volume_source && s.volume_source !== 'default') {
    bits.push(`volume from ${s.volume_source}`);
  }
  const last = s.last_take;
  if (last && last.worst_start_error_ms !== null && last.worst_start_error_ms !== undefined) {
    bits.push(`worst start error ${last.worst_start_error_ms} ms`);
  }
  ui.stateLine.innerHTML = bits.join(' · ') +
    (s.error ? ` <span class="err">— ${escape_(s.error)}</span>` : '');

  if (s.message || s.error) {
    showBanner(s.message || s.error, !s.error);
  } else {
    ui.banner.hidden = true;
  }
}

function paintFleet(s) {
  if (!s.robots.length) return;
  if (chipEls.size !== s.robots.length) {
    ui.fleet.innerHTML = '';
    chipEls = new Map();
    for (const r of s.robots) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = `<i class="dot"></i><b>${escape_(r.id)}</b> ${escape_(r.instrument)}`;
      ui.fleet.append(chip);
      chipEls.set(r.id, chip);
    }
  }
  for (const r of s.robots) {
    const chip = chipEls.get(r.id);
    if (!chip) continue;
    chip.classList.toggle('live', r.live);
    chip.classList.toggle('bad', !!r.error || (s.mode === 'fleet' && !r.loaded));
    chip.title = r.error || (r.part ? `${r.part} / ${r.audio || 'no stem'}` : '');
  }
}

function showBanner(text, notice = false) {
  ui.banner.hidden = false;
  ui.banner.className = notice ? 'notice' : '';
  ui.banner.textContent = text;
}

const escape_ = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ------------------------------------------------------- rehearsal  audio */

/* THE SHOW COMES OUT OF THE ROBOTS' OWN SPEAKERS. This page makes no sound
 * during a performance, and the volume slider is the robots' volume, not the
 * browser's -- it goes to `VolumeController`, which rides the SDK session each
 * agent already holds.
 *
 * The one exception is rehearsal mode, where there are no robots to hear and
 * the browser plays the mix `song build` rendered so the twin can be checked
 * against it. That is gated on the mode the server reports rather than on
 * whether a button happens to be hidden, and the server refuses to serve the
 * file at all outside rehearsal -- a page that cannot fetch it cannot play it.
 */
const canPreview = (s) => !!s && s.mode === 'rehearsal';

function stopPreview() {
  audioOn = false;
  ui.audio.setAttribute('aria-pressed', 'false');
  if (!ui.preview.paused) ui.preview.pause();
  ui.preview.currentTime = 0;
}

function applyAudioGain(value) {
  /* Only ever touches the rehearsal element. In fleet mode the slider's value
   * has already gone to nine robots and has nothing to do with this page. */
  ui.preview.volume = Math.max(0, Math.min(1, value / 100));
}

function syncAudio(s, prev) {
  if (!audioOn || !canPreview(s)) return;
  const t = s.t;
  if (s.state === 'playing' && t !== null && t >= 0) {
    if (ui.preview.paused) {
      ui.preview.currentTime = Math.max(0, t);
      ui.preview.play().catch(() => {});
    } else if (Math.abs(ui.preview.currentTime - t) > 0.25) {
      ui.preview.currentTime = Math.max(0, t);
    }
  } else if (!ui.preview.paused) {
    ui.preview.pause();
    ui.preview.currentTime = 0;
  }
}

/* -------------------------------------------------------------- wake lock */

let wakeLock = null;

async function acquireWakeLock() {
  if (document.visibilityState !== 'visible') return;
  if (!('wakeLock' in navigator)) { videoFallback(); return; }
  if (wakeLock && !wakeLock.released) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    setWakeBadge(true, 'Screen Wake Lock');
    wakeLock.addEventListener('release', () => setWakeBadge(false, 'released'));
  } catch (e) {
    /* Thrown when the document is not active, or the platform refuses. The
     * video below is the same trick a video site uses, and it works wherever
     * video playback does. */
    videoFallback(e.message);
  }
}

function videoFallback(why) {
  ui.keepawake.play().then(
    () => setWakeBadge(true, `video fallback${why ? ` (${why})` : ''}`),
    () => setWakeBadge(false, 'autoplay refused — click the page'),
  );
}

function setWakeBadge(on, why) {
  ui.badgeWake.classList.toggle('on', on);
  ui.badgeWake.textContent = on ? '● SCREEN AWAKE' : '○ SCREEN MAY SLEEP';
  ui.badgeWake.title = why || '';
}

/* ------------------------------------------------------------ the picture */

function frame() {
  const t = programTime();
  const dur = snap ? snap.duration : (stageData ? stageData.duration : 0);

  /* Strikes first, so the frame that consumes a hit is the frame that draws
   * it. Drawn second, every halo was a frame late -- invisible whenever a
   * frame lasts longer than the halo does. Each robot's anchors are sorted,
   * so this is a pointer walk rather than a search. */
  if (stage && t !== null && t >= 0) {
    for (const hit of stage.consumeHits(t)) hitAt.set(hit.id, performance.now());
  }

  if (stage) {
    stage.update(t);
    /* the console takes its tint from the same hue the scene is lit with */
    document.documentElement.style.setProperty('--hue', stage.hue.toFixed(1));
  }

  paintProgress(t, dur);
  paintLabels(t);
  maybeHideChrome(t);
  requestAnimationFrame(frame);
}

function paintProgress(t, dur) {
  const pct = (t !== null && dur > 0) ? Math.max(0, Math.min(1, t / dur)) : 0;
  ui.fill.style.width = `${pct * 100}%`;
  ui.head.style.left = `${pct * 100}%`;
  ui.progress.setAttribute('aria-valuenow', String(Math.round(pct * 100)));
  ui.elapsed.textContent = mmss(t === null ? 0 : Math.max(0, t));
  if (t !== null && t < 0) ui.elapsed.textContent = `−${mmss(-t)}`;

  if (stageData) {
    const section = sectionAt(t);
    const label = section ? SECTION_TEXT[section.name] || section.name : null;
    const bar = musicalPosition(t);
    ui.subtitle.textContent = [label, bar].filter(Boolean).join('   ') ||
      `${stageData.robots.length} robots · ${mmss(stageData.duration)} · `
      + `${stageData.music.bpm} BPM`;
  }
}

const SECTION_TEXT = {
  intro: 'introductions', verse: 'verse', chorus: 'chorus',
  finale: 'finale', outro: 'bow',
};

function sectionAt(t) {
  if (t === null || !stageData) return null;
  for (const s of stageData.sections) if (t >= s.start && t < s.end) return s;
  return null;
}

function musicalPosition(t) {
  if (t === null || !stageData || !stageData.music.bpm) return '';
  const { bpm, meter, t0_s, beats } = stageData.music;
  const beat = (t - t0_s) * bpm / 60;
  if (beat < 0 || beat > beats) return '';
  const bpb = meter[0] * 4 / meter[1];
  return `bar ${Math.floor(beat / bpb) + 1} · beat ${Math.floor(beat % bpb) + 1}`;
}

const LABEL_GAP = 7;   // px between neighbouring placards

/* The placards hang in one row in the sky, each on a leader line down to its
 * own instrument. Two things have to be settled here rather than in the scene:
 * how wide each one came out once the browser laid the text out, and what to
 * do when two land on top of each other.
 *
 * They are pushed apart sideways, not stacked. Perspective bunches the seats
 * at the ends of the row together, so a seat's own x is a suggestion; one tidy
 * row with a few leaders leaning is far quieter than a staircase, and a leaning
 * leader is what a leader line is for. */
function paintLabels(t) {
  if (!stage) return;
  const now = performance.now();
  const rows = stage.labelPositions();
  rows.sort((a, b) => a.x - b.x);
  const W = window.innerWidth;

  /* measure, then relax: a few passes of pushing overlapping neighbours apart
   * settles a row of nine without any solving */
  for (const p of rows) {
    const node = labelEls.get(p.id);
    p.halfW = ((node && node._w) || 110) / 2;
    p.lx = p.x;
  }
  const band = rows.filter((p) => p.anchorY !== null && !p.below);
  for (let pass = 0; pass < 4; pass++) {
    for (let i = 1; i < band.length; i++) {
      const a = band[i - 1], b = band[i];
      const need = a.halfW + b.halfW + LABEL_GAP;
      const push = (need - (b.lx - a.lx)) / 2;
      if (push > 0) { a.lx -= push; b.lx += push; }
    }
    for (const p of band) p.lx = Math.max(p.halfW + 8, Math.min(W - p.halfW - 8, p.lx));
  }

  for (const p of rows) {
    let node = labelEls.get(p.id);
    if (!node) {
      /* The placard: the instrument's name is the whole point of this view,
       * so it is the big line; the robot id is how the eye finds the same
       * seat on the table. */
      node = document.createElement('div');
      node.className = 'tag';
      /* The instrument's own name from the config, uppercased -- no lookup
       * table, so the screen cannot drift from robots.yaml. */
      node.innerHTML = `<span class="name">${escape_(p.instrument.toUpperCase())}</span>`
        + `<span class="rid">${escape_(p.id.toUpperCase())}</span>`
        + `<span class="note"></span>`;
      node._note = node.querySelector('.note');
      ui.labels.append(node);
      labelEls.set(p.id, node);
    }
    /* measured once the browser has laid it out; the text never changes */
    if (!node._w && node.offsetWidth) { node._w = node.offsetWidth; node._h = node.offsetHeight; }
    const tall = node._h || 46;
    const lx = p.lx === undefined ? p.x : p.lx;

    node.style.transform = `translate(${lx}px, ${p.y}px) translate(-50%, 0)`;
    node.style.opacity = p.visible ? '1' : '0';
    /* the leader: from the bottom of the placard to the top of the instrument,
     * leaning by however far the row had to push this one sideways */
    if (p.anchorY === null || p.below) {
      node.style.setProperty('--pin', '0px');
    } else {
      const dx = p.anchorX - lx, dy = p.anchorY - p.y - tall;
      node.style.setProperty('--pin', `${Math.max(0, Math.hypot(dx, dy)).toFixed(0)}px`);
      node.style.setProperty('--pin-a', `${(Math.atan2(-dx, Math.max(1, dy)) * 180 / Math.PI).toFixed(2)}deg`);
    }
    /* the glow under the name is this robot's own stem, right now */
    node.style.setProperty('--lvl', (p.level || 0).toFixed(3));
    /* the note just struck, for the pitched four; blank for the rest */
    const nn = p.note === null || p.note === undefined ? '' : noteName(p.note);
    if (node._note.textContent !== nn) node._note.textContent = nn;
    node.classList.toggle('sounding', nn !== '');
    const hot = now - (hitAt.get(p.id) || -1e9) < 170;
    node.classList.toggle('hit', hot);
    const chip = chipEls.get(p.id);
    if (chip) chip.classList.toggle('hit', hot);
  }
}

function mmss(s) {
  const whole = Math.floor(s);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

/* Hide the chrome while the show runs and nobody is touching anything. The
 * screen is next to the robots, and a control bar is not what anyone came to
 * look at. Any input brings it straight back. */
function maybeHideChrome(t) {
  const playing = snap && (snap.state === 'playing' || snap.state === 'countdown');
  const idleFor = performance.now() - lastIdle;
  const hide = playing && idleFor > IDLE_HIDE_MS && !document.activeElement?.closest('#console');
  document.body.classList.toggle('immersive', !!hide);
  document.body.classList.toggle('idle', !!hide);
}

const wake = () => { lastIdle = performance.now(); };

/* -------------------------------------------------------------- the stream */

function openStream() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    try { apply(JSON.parse(ev.data)); } catch (e) { /* a torn frame; the next
      one is a complete description of the present, so nothing is lost */ }
  };
  es.onerror = () => {
    /* EventSource reconnects by itself. Saying so beats a silent dead page. */
    ui.badgeMode.textContent = 'DISCONNECTED — RECONNECTING';
    ui.badgeMode.hidden = false;
  };
}

/* ------------------------------------------------------------------- boot */

/* The page is opened on a laptop or a wall display whose developer console
 * nobody here can see, so a boot that fails has to say so twice: on the
 * screen, and in the server's log, where it can be read. A failure used to
 * leave "Loading…" up and nothing anywhere else. */
let bootStep = 'loading the page';
let reports = 0;

function report(kind, detail) {
  if (reports++ >= 5) return;
  try {
    navigator.sendBeacon('/api/client-log', JSON.stringify({
      kind, step: bootStep, detail: String(detail ?? '').slice(0, 1500),
      ua: navigator.userAgent,
    }));
  } catch (e) { /* nowhere left to say it */ }
}

window.addEventListener('error', (e) => report('error', `${e.message}\n${e.error?.stack || ''}`));

/* Why WebGL would not start, in the browser's own words. Firefox's stack
 * leaves the message out, and the reason a context failed is only ever given
 * in this event, so a report without it says nothing about what to fix. */
function glProbe() {
  return ['webgl2', 'webgl'].map((kind) => {
    const c = document.createElement('canvas');
    let why = '';
    c.addEventListener('webglcontextcreationerror', (e) => { why = e.statusMessage || ''; });
    let ok = false;
    try { ok = !!c.getContext(kind); } catch (e) { why = why || String(e); }
    return `${kind} ${ok ? 'ok' : `failed${why ? `: ${why}` : ''}`}`;
  }).join('; ');
}
window.addEventListener('unhandledrejection', (e) => report('rejection', e.reason?.stack || e.reason));

setTimeout(() => {
  if (bootStep === 'done') return;
  showBanner(`Still ${bootStep}… Reloading the page usually clears this.`, true);
  report('slow', `not done after 15 s`);
}, 15000);

async function boot() {
  ui.total.textContent = '…';
  bootStep = 'fetching the stage data';
  const res = await fetch('/api/stage');
  if (!res.ok) { showBanner(`cannot load stage data: ${res.status}`); return; }
  stageData = await res.json();

  ui.title.textContent = stageData.title;
  ui.total.textContent = mmss(stageData.duration);
  for (const s of stageData.sections) {
    const span = document.createElement('span');
    span.className = s.name;
    span.style.flex = `${(s.end - s.start) / stageData.duration}`;
    span.title = `${SECTION_TEXT[s.name] || s.name}  ${mmss(s.start)}`;
    ui.sections.append(span);
  }

  /* The 3D view is the one part that depends on the viewer's GPU. Without it
   * the buttons, the clock and the state line still work, and a console that
   * cannot stop the robots because WebGL failed would be the worse outcome. */
  bootStep = 'building the 3D stage';
  try {
    stage = new Stage(el('stage'), stageData);
    window.__stage = stage;    // for poking at the scene from the console
    await stage.ready();
  } catch (e) {
    stage = null;
    const why = `${e?.message || e} (${glProbe()})`;
    showBanner(`3D view unavailable: ${why}. The controls still work.`, true);
    report('stage', `${why}\n${e?.stack || ''}`);
  }
  el('stage').addEventListener('webglcontextlost', () => {
    showBanner('3D view lost its GPU context. Reload the page to bring it back.', true);
    report('context-lost', 'webglcontextlost');
  });

  bootStep = 'connecting to the console';
  openStream();
  requestAnimationFrame(frame);
  acquireWakeLock();
}

/* -- wiring ---------------------------------------------------------------- */

ui.play.addEventListener('click', playOrStop);
ui.loop.addEventListener('click', toggleLoop);
ui.estop.addEventListener('click', () => {
  if (confirm('EMERGENCY STOP: cut torque on every robot now?')) post('/api/estop');
});
ui.volume.addEventListener('input', (e) => queueVolume(Number(e.target.value)));
ui.volume.addEventListener('change', () => { volumeLocal = null; });
ui.full.addEventListener('click', () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen?.().catch(() => {});
});
ui.audio.addEventListener('click', () => {
  if (!canPreview(snap)) { stopPreview(); return; }
  if (audioOn) { stopPreview(); paintVolumeTarget(snap); return; }
  /* The src is attached here and nowhere else, so a performance page never
   * even requests the file. */
  if (!ui.preview.getAttribute('src')) {
    ui.preview.setAttribute('src', '/api/audio/preview.wav');
  }
  audioOn = true;
  ui.audio.setAttribute('aria-pressed', 'true');
  applyAudioGain(snap.volume);
  syncAudio(snap, null);
  paintVolumeTarget(snap);
});

document.addEventListener('keydown', (e) => {
  wake();
  if (e.target instanceof HTMLInputElement && e.target.type !== 'range') return;
  if (e.code === 'Space') { e.preventDefault(); playOrStop(); return; }
  if (e.key === 'Escape' && snap && BUSY.has(snap.state)) { post('/api/stop'); return; }
  if (e.key === 'l' || e.key === 'L') { toggleLoop(); return; }
  if (e.key === 'f' || e.key === 'F') { ui.full.click(); return; }
  if (e.key === 't' || e.key === 'T') {
    /* the robot twin, for rehearsing a build without robots */
    const view = stage ? stage.toggleView() : null;
    document.body.classList.toggle('twin', view === 'twin');
    return;
  }
  if (e.key in VOLUME_STEP) { e.preventDefault(); nudgeVolume(VOLUME_STEP[e.key]); }
});

for (const ev of ['pointermove', 'pointerdown', 'wheel', 'touchstart']) {
  window.addEventListener(ev, wake, { passive: true });
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') acquireWakeLock();
});
window.addEventListener('click', () => acquireWakeLock(), { once: true });

window.__booted = true;      // the watchdog in index.html looks for this
boot().then(() => { bootStep = 'done'; }, (e) => {
  showBanner(`The page could not start: ${e?.message || e}`);
  report('boot', e?.stack || e);
});
