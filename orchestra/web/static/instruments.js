/* The instruments the robots do not have -- at their real size.
 *
 * Every sound in this show comes out of a speaker and every movement is air
 * playing, so the one thing an audience cannot see is the one thing it asks
 * about: which of the nine is the marimba. This file answers with the
 * instrument itself, one per seat, played in time from the same data the
 * robots play.
 *
 * WHAT DRIVES WHAT
 *
 *   anchors   the strike instants `song build` compiled. Every instrument
 *             animates off these, and the export has checked that for the
 *             pitched parts they agree with the score's note times exactly.
 *   notes     pitch per strike, where the score has one. Decides *which bar*.
 *   envelope  loudness of this robot's own stem, 50 Hz, normalised. The only
 *             thing here that knows the vibraphone keeps ringing after the
 *             mallet has gone, or that the shaker is a roll and not a hit.
 *
 * Each instrument looks ahead through its anchors: the mallet rises during the
 * quarter second before a hit and lands on the instant, instead of flinching
 * after it.
 *
 * SCALE. Metres, and the real ones: a five-octave marimba is 2.6 m long with
 * resonators that nearly reach the floor, a glockenspiel is a 70 cm case on a
 * stand, a shaker is something you hold. The pitched instruments are built at
 * their full standard range and the played bars light within it -- a marimba
 * with twenty bars is a toy, and the one thing this view must not look like
 * is a toy.
 *
 * `build()` is the only entry point, so any one of these can be replaced by a
 * modelled mesh without the stage knowing.
 */

import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

/* -- timing, seconds ------------------------------------------------------- */
const WINDUP = 0.26;     // the mallet rises through this, landing on the anchor
const REBOUND = 0.14;    // and settles over this
const FLASH = 0.32;      // a struck bar's glow, when nothing sustains it
const SPREAD = 0.11;     // metres a strike's light carries along the keyboard

/* -- materials, shared --------------------------------------------------- */
const grain = woodGrain();
const std = (color, roughness, metalness, extra = {}) =>
  new THREE.MeshStandardMaterial({ color, roughness, metalness, ...extra });

export const M = {
  rosewood: std(0x4a2411, 0.42, 0.05, { map: grain }),
  padauk: std(0x6e2f14, 0.45, 0.05, { map: grain }),
  maple: std(0xb08a5a, 0.5, 0.03, { map: grain }),
  hickory: std(0xc9a574, 0.55, 0.0),
  frame: std(0x15181f, 0.55, 0.35),
  carbon: std(0x0c0e14, 0.35, 0.5),
  /* Piano black, but not a mirror: at roughness 0.12 every flat panel aimed
   * near the key light -- the celesta's music desk, the glockenspiel's case --
   * came back as a blown white slab. */
  lacquer: new THREE.MeshPhysicalMaterial({
    color: 0x07080c, roughness: 0.3, metalness: 0.0, clearcoat: 1.0, clearcoatRoughness: 0.22,
  }),
  ivory: std(0xf4f0e6, 0.38, 0.0),
  ebony: std(0x0e0f12, 0.3, 0.1),
  aluminium: std(0xd9dee8, 0.26, 0.95),
  steel: std(0xc8ccd4, 0.2, 1.0),
  chrome: std(0xffffff, 0.1, 1.0),
  brass: std(0xe3b65c, 0.2, 0.95),
  darkBrass: std(0x8a6b2a, 0.35, 0.9),
  skin: std(0xe9ddc6, 0.75, 0.0),
  yarn: std(0x2a4fb0, 0.9, 0.0),
  cordHead: std(0x3a3f4a, 0.85, 0.0),
  plastic: std(0xf2f2f0, 0.3, 0.05),
  rubber: std(0x111216, 0.9, 0.0),
  leather: std(0x1a1512, 0.7, 0.0),
  foam: std(0x1b2740, 0.95, 0.0),
};

const ACCIDENTAL = new Set([1, 3, 6, 8, 10]);
const isAccidental = (pitch) => ACCIDENTAL.has(((pitch % 12) + 12) % 12);

/* -------------------------------------------------------------------------- */
/* Base                                                                       */
/* -------------------------------------------------------------------------- */

class Instrument {
  constructor(entry) {
    this.root = new THREE.Group();
    this.id = entry.id;
    /* The strikes this instrument plays: the build's anchors, and before
     * them the onsets `export.py` recovered from the stem for the
     * introduction -- the anchors do not cover it, and an instrument that
     * only knows the anchors stands still while its robot names itself.
     * Onsets carry no pitch, so the pitch list is padded to keep the two
     * aligned by index. */
    const onsets = entry.onsets || [];
    const anchors = entry.anchors || [];
    this.anchors = [...onsets, ...anchors];
    const pitched = entry.pitched && entry.notes && entry.notes.length === anchors.length;
    this.pitches = pitched ? [...onsets.map(() => null), ...entry.notes.map((n) => n[1])] : null;
    this.lo = entry.pitch_range ? entry.pitch_range[0] : 60;
    this.hi = entry.pitch_range ? entry.pitch_range[1] : 60;
    this.ptr = 0;
    this.level = 0;
    this.accent = new THREE.Color(0x40c8ff);
    this.glowMats = [];          // every material whose emissive follows the accent
    /* footprint, for the stage's layout and the placard */
    this.width = 1.0;
    this.depth = 0.6;
    this.topHeight = 1.2;
    /* the top of the instrument itself: where its leader line points. The
     * placard goes well above this, in the empty sky -- see `stage.js`. */
    /* the last struck pitch, and when, for the placard's note readout */
    this.lastPitch = null;
    this.lastAt = -1e9;
    this._fl = new Map();        // key -> { at, real }: see `flash`
  }

  /** How bright the strike on `key` still is, 1 down to 0 over `decay`.
   *
   * Aged in REAL seconds from the frame that first saw the strike, not in
   * program-time distance from its anchor -- the same reason the halo under
   * the seat is. Those agree only while frames are frequent. At a few frames
   * a second the next frame is already 0.8 s past, so a 0.3 s flash computed
   * in program time is found expired and never drawn once: measured on the
   * software renderer, the glockenspiel's whole six-note flourish passed
   * between two frames and not one bar ever lit. From the firing instant
   * every strike gets its full time on screen whatever the frame rate.
   */
  /** Real seconds since the strike at `at` on `key` was first seen, or
   *  Infinity if there has not been one yet. */
  since(key, at) {
    if (at === null || at === undefined) return Infinity;
    let e = this._fl.get(key);
    if (!e || e.at !== at) { e = { at, real: performance.now() }; this._fl.set(key, e); }
    return (performance.now() - e.real) / 1000;
  }

  /** The same, as a flash: 1 at the strike, 0 after `decay`. */
  flash(key, at, decay) {
    const age = this.since(key, at);
    return age < decay ? 1 - age / decay : 0;
  }

  next(t) {
    const a = this.anchors;
    while (this.ptr < a.length && a[this.ptr] < t) this.ptr++;
    return this.ptr;
  }

  rewind() { this.ptr = 0; this.lastPitch = null; this.lastAt = -1e9; this._fl.clear(); }

  /** The note to show on the placard, or null. */
  sounding(t) {
    if (t === null || this.lastPitch === null) return null;
    return (t - this.lastAt) < 0.5 ? this.lastPitch : null;
  }

  setAccent(c) {
    this.accent.copy(c);
    for (const m of this.glowMats) m.emissive.copy(c);
  }

  /** `t` program seconds or null, `env` 0..1, `beat` { beat, inBar, bpb, total }. */
  update(t, env, beat) {
    this.level = env;
    if (t !== null && this.pitches) {
      const n = this.next(t);
      if (n > 0 && this.pitches[n - 1] !== null) {
        this.lastPitch = this.pitches[n - 1]; this.lastAt = this.anchors[n - 1];
      }
    }
  }

  /** Marks every mesh under `root` as a shadow caster. */
  finish() {
    this.root.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    return this;
  }

  _glow(mat) {
    const m = mat.clone();
    m.emissive = this.accent.clone();
    m.emissiveIntensity = 0;
    this.glowMats.push(m);
    return m;
  }
}

function strokeHeight(t, last, next, lift) {
  let h = 0;
  if (next !== null) {
    const u = (t - (next - WINDUP)) / WINDUP;
    if (u > 0 && u < 1) h = Math.max(h, lift * Math.sin(Math.PI * u));
  }
  if (last !== null) {
    const a = t - last;
    if (a >= 0 && a < REBOUND) h = Math.max(h, lift * 0.35 * Math.sin(Math.PI * a / REBOUND));
  }
  return h;
}

const smooth = (u) => (u <= 0 ? 0 : u >= 1 ? 1 : u * u * (3 - 2 * u));

/* -------------------------------------------------------------------------- */
/* Pieces. y up, x left-right, +z toward the audience.                        */
/* -------------------------------------------------------------------------- */

function rbox(w, h, d, mat, r = 0.004) {
  return new THREE.Mesh(new RoundedBoxGeometry(w, h, d, 2, Math.min(r, w / 2, h / 2, d / 2)), mat);
}

function cylY(rTop, rBot, h, mat, seg = 24) {
  return new THREE.Mesh(new THREE.CylinderGeometry(rTop, rBot, h, seg), mat);
}

function cylZ(r, len, mat, seg = 16) {
  const g = new THREE.CylinderGeometry(r, r, len, seg);
  g.rotateX(Math.PI / 2);
  return new THREE.Mesh(g, mat);
}

function cylX(r, len, mat, seg = 16) {
  const g = new THREE.CylinderGeometry(r, r, len, seg);
  g.rotateZ(Math.PI / 2);
  return new THREE.Mesh(g, mat);
}

/** A chrome tripod stand of the kind every percussionist owns. */
function tripod(group, height, r = 0.012) {
  const pole = cylY(r, r, height, M.chrome);
  pole.position.y = height / 2;
  group.add(pole);
  const collar = cylY(r * 1.8, r * 1.8, 0.03, M.carbon);
  collar.position.y = 0.26;
  group.add(collar);
  for (let i = 0; i < 3; i++) {
    const a = (i / 3) * Math.PI * 2 + Math.PI / 6;
    const leg = cylY(r * 0.8, r * 0.8, 0.42, M.chrome);
    leg.position.set(Math.cos(a) * 0.16, 0.13, Math.sin(a) * 0.16);
    leg.rotation.set(Math.sin(a) * 0.75, 0, -Math.cos(a) * 0.75);
    group.add(leg);
    const foot = cylY(0.018, 0.022, 0.02, M.rubber);
    foot.position.set(Math.cos(a) * 0.3, 0.01, Math.sin(a) * 0.3);
    group.add(foot);
  }
}

/** A wheeled frame leg: tube, caster, wheel. */
function casterLeg(group, x, z, height) {
  const leg = cylY(0.016, 0.016, height - 0.08, M.chrome);
  leg.position.set(x, 0.08 + (height - 0.08) / 2, z);
  group.add(leg);
  const fork = rbox(0.03, 0.06, 0.03, M.carbon);
  fork.position.set(x, 0.07, z);
  group.add(fork);
  const wheel = cylX(0.04, 0.025, M.rubber);
  wheel.position.set(x, 0.04, z + 0.02);
  group.add(wheel);
}

/** A mallet: shaft along -z (toward the player), head at the origin. */
function mallet(headMat, len, headR, shaftMat = M.hickory) {
  const g = new THREE.Group();
  const shaft = cylZ(0.0045, len, shaftMat);
  shaft.position.z = -len / 2;
  g.add(shaft);
  const head = new THREE.Mesh(new THREE.SphereGeometry(headR, 18, 12), headMat);
  g.add(head);
  return g;
}

/* -------------------------------------------------------------------------- */
/* Keyed percussion: marimba, vibraphone, glockenspiel                        */
/* -------------------------------------------------------------------------- */

/** Two rows of bars laid out like a keyboard -- naturals in front, accidentals
 *  raised and set back toward the player -- built across the instrument's full
 *  standard range so it has the size of the real thing. */
class Keyed extends Instrument {
  constructor(entry, o) {
    super(entry);
    this.o = o;
    this.bars = [];
    this.byPitch = new Map();
    this.hitAt = new Map();
    this.malletX = [0, 0];

    const pitches = [];
    for (let p = o.rangeLo; p <= o.rangeHi; p++) pitches.push(p);
    const naturals = pitches.filter((p) => !isAccidental(p));
    const nN = naturals.length;
    /* slot width tapers low to high, like the bars do */
    const slots = [];
    let x = 0;
    naturals.forEach((p, i) => {
      const u = i / Math.max(1, nN - 1);
      const w = o.widthLo + (o.widthHi - o.widthLo) * u;
      slots.push({ p, x: x + w / 2, w, u });
      x += w + o.gap;
    });
    const span = x - o.gap;
    for (const s of slots) s.x -= span / 2;

    slots.forEach((s) => this._bar(s.p, s.x, s.w, 0, s.u));
    for (const p of pitches) {
      if (!isAccidental(p)) continue;
      const left = naturals.filter((n) => n < p).length - 1;   // natural to the left
      const a = slots[left], b = slots[Math.min(nN - 1, left + 1)];
      const u = (p - o.rangeLo) / Math.max(1, o.rangeHi - o.rangeLo);
      const w = o.widthLo + (o.widthHi - o.widthLo) * u;
      this._bar(p, (a.x + b.x) / 2, w * 0.96, 1, u);
    }

    this._frame(span + 0.16, o);
    this.width = span + 0.3;
    this.depth = o.lenLo + 0.3;
    this.topHeight = o.deck + 0.3;   // bars, plus a raised mallet

    this.mallets = [
      mallet(o.malletMat, o.malletLen, o.malletR),
      mallet(o.malletMat, o.malletLen, o.malletR),
    ];
    for (const m of this.mallets) { m.position.set(0, o.deck + 0.1, -0.1); this.root.add(m); }
    this.malletX = [-span / 4, span / 4];
  }

  _bar(pitch, x, w, row, u) {
    const o = this.o;
    const len = o.lenLo + (o.lenHi - o.lenLo) * u;
    const mat = this._glow(o.barMat);
    const y = o.deck + (row ? 0.028 : 0);
    const z = row ? -len * 0.28 - 0.03 : 0;
    const mesh = rbox(w, o.thick, len, mat, o.thick * 0.35);
    mesh.position.set(x, y, z);
    this.root.add(mesh);
    if (o.tubes) {
      const tl = Math.min(o.deck - 0.07, o.tubeLo + (o.tubeHi - o.tubeLo) * u);
      const tube = cylY(o.tubeR * (1 - u * 0.35), o.tubeR * (1 - u * 0.35), tl, o.tubeMat);
      tube.position.set(x, y - 0.025 - tl / 2, z);
      this.root.add(tube);
      if (o.fans) {
        const fan = cylY(o.tubeR * 0.8, o.tubeR * 0.8, 0.004, M.carbon);
        fan.position.set(x, y - 0.03, z);
        fan.rotation.x = 0.4;
        this.root.add(fan);
      }
    }
    const bar = { mesh, mat, pitch, x, y, z, len };
    this.bars.push(bar);
    this.byPitch.set(pitch, bar);
  }

  _frame(width, o) {
    const root = this.root;
    const rail = (z) => {
      const r = rbox(width, 0.04, 0.05, o.frameMat, 0.01);
      r.position.set(0, o.deck - 0.035, z);
      root.add(r);
    };
    if (o.caseStyle === 'case') {
      /* a glockenspiel lives in a box, on a stand */
      const c = rbox(width, 0.09, o.lenLo + 0.14, o.frameMat, 0.012);
      c.position.set(0, o.deck - 0.06, -0.03);
      root.add(c);
      const lid = rbox(width, 0.012, o.lenLo + 0.14, o.frameMat, 0.004);
      lid.position.set(0, o.deck + 0.2, -(o.lenLo + 0.14) / 2 - 0.1);
      lid.rotation.x = -1.25;
      root.add(lid);
      const stand = new THREE.Group();
      tripod(stand, o.deck - 0.11, 0.014);
      root.add(stand);
      return;
    }
    rail(o.lenLo * 0.5 + 0.06);
    rail(-o.lenLo * 0.5 - 0.06);
    /* end pieces, wood, and the brackets under them */
    for (const sx of [-1, 1]) {
      const end = rbox(0.07, 0.26, o.lenLo + 0.3, o.endMat || M.rosewood, 0.012);
      end.position.set(sx * (width / 2 - 0.02), o.deck - 0.1, -0.02);
      root.add(end);
      const bracket = rbox(0.05, 0.03, o.lenLo + 0.25, o.frameMat, 0.006);
      bracket.position.set(sx * (width / 2 - 0.02), o.deck - 0.23, -0.02);
      root.add(bracket);
    }
    /* the resonator rack's own rail, under the tubes */
    const rack = rbox(width - 0.12, 0.025, 0.03, o.frameMat, 0.006);
    rack.position.set(0, o.deck - 0.07 - (o.tubeHi * 0.5), 0);
    root.add(rack);
    for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
      casterLeg(root, sx * (width / 2 - 0.05), sz * (o.lenLo * 0.5 + 0.05) - 0.02, o.deck - 0.2);
    }
    if (o.motor) {
      const box = rbox(0.22, 0.16, 0.18, M.carbon, 0.01);
      box.position.set(-(width / 2 - 0.17), o.deck - 0.45, -0.1);
      root.add(box);
      const pedal = rbox(0.09, 0.015, 0.22, M.chrome, 0.004);
      pedal.position.set(0.2, 0.02, o.lenLo * 0.5 + 0.2);
      pedal.rotation.x = -0.25;
      root.add(pedal);
      const damper = rbox(width - 0.2, 0.015, 0.03, M.rubber, 0.004);
      damper.position.set(0, o.deck - 0.012, 0.0);
      root.add(damper);
    }
  }

  update(t, env, beat) {
    super.update(t, env, beat);
    const o = this.o;
    if (t === null) {
      for (const b of this.bars) b.mat.emissiveIntensity = 0;
      this.mallets.forEach((m, k) => m.position.set(this.malletX[k], o.deck + 0.1, -0.08));
      return;
    }
    const n = this.next(t);
    const a = this.anchors;

    for (let k = 0; k < 2; k++) {
      let last = null, next = null;
      for (let i = n - 1; i >= 0 && i >= n - 6; i--) if (i % 2 === k) { last = i; break; }
      for (let i = n; i < a.length && i < n + 6; i++) if (i % 2 === k) { next = i; break; }
      const lastT = last === null ? null : a[last];
      const nextT = next === null ? null : a[next];

      const target = next !== null ? this._barOf(next) : (last !== null ? this._barOf(last) : null);
      const from = last !== null ? this._barOf(last) : target;
      let x = this.malletX[k], y = o.deck + 0.05, z = -0.05;
      if (target) {
        const u = nextT === null ? 1 : smooth((t - (nextT - WINDUP)) / WINDUP);
        const f = from || target;
        x = f.x + (target.x - f.x) * u;
        z = f.z + (target.z - f.z) * u + 0.02;
        y = f.y + (target.y - f.y) * u + o.thick / 2 + o.malletR;
        this.malletX[k] = x;
      }
      const h = strokeHeight(t, lastT, nextT, o.lift);
      const m = this.mallets[k];
      m.position.set(x, y + h, z);
      m.rotation.x = h * 2.2;      // shaft tips up as the head rises
    }

    for (let i = Math.max(0, n - 14); i < n; i++) {
      const bar = this._barOf(i);
      if (bar) this.hitAt.set(bar.pitch, a[i]);
    }

    /* The light a strike makes, spread over the bars either side of it.
     *
     * A marimba bar is four centimetres wide and the row is seen from
     * seventeen metres: one bar lighting on its own is three pixels, which
     * is not a performance however bright it is. So a strike lights its own
     * bar fully and its neighbours by how far away they are, and the eye
     * reads a hand's width of the keyboard coming up -- which is what
     * striking a marimba actually looks like from the hall. */
    for (const b of this.bars) b.glow = 0;
    for (const [pitch, at] of this.hitAt) {
      if (t < at) continue;
      const src = this.byPitch.get(pitch);
      if (!src) continue;
      let core = this.flash(pitch, at, FLASH);
      if (o.sustain && t - at < 3.0) core = Math.max(core, env * 0.85);
      if (core <= 0) continue;
      for (const b of this.bars) {
        const d = (b.x - src.x) / SPREAD;
        const g = core * Math.exp(-d * d);
        if (g > b.glow) b.glow = g;
      }
    }
    for (const b of this.bars) b.mat.emissiveIntensity = b.glow * 1.5;
  }

  _barOf(i) {
    const n = this.bars.length;
    const p = this.pitches ? this.pitches[i] : null;
    if (p === null || p === undefined) {
      /* no pitch (the introduction's onsets, or an unpitched part): a bar
       * from the middle of the range, varied so a flourish is not one bar
       * struck five times */
      return this.bars[Math.floor(n * (0.38 + 0.24 * ((i * 7) % 5) / 4))];
    }
    return this.byPitch.get(p) || null;
  }
}

/* -------------------------------------------------------------------------- */
/* Celesta: an upright, black lacquer, four octaves of keys                   */
/* -------------------------------------------------------------------------- */

class Celesta extends Instrument {
  constructor(entry) {
    super(entry);
    const lo = 48, hi = 96;                  // C3..C7
    const kw = 0.0235, gap = 0.0012, slot = kw + gap;
    const naturals = [];
    for (let p = lo; p <= hi; p++) if (!isAccidental(p)) naturals.push(p);
    const span = naturals.length * slot - gap;
    const keyY = 0.77;
    this.keys = new Map();

    const W = span + 0.18, D = 0.48, H = 1.02;
    const body = rbox(W, H - 0.1, D, M.lacquer, 0.012);
    body.position.set(0, 0.1 + (H - 0.1) / 2, -0.1);
    this.root.add(body);
    /* a fabric grille on the front, where the sound comes out */
    const grille = rbox(W - 0.2, 0.3, 0.01, M.foam, 0.004);
    grille.position.set(0, 0.42, D / 2 - 0.1 + 0.002);
    this.root.add(grille);
    /* key bed and cheeks */
    const bed = rbox(W, 0.05, 0.3, M.lacquer, 0.008);
    bed.position.set(0, keyY - 0.04, D / 2 - 0.1 + 0.1);
    this.root.add(bed);
    for (const sx of [-1, 1]) {
      const cheek = rbox(0.07, 0.12, 0.3, M.lacquer, 0.008);
      cheek.position.set(sx * (W / 2 - 0.035), keyY + 0.02, D / 2 - 0.1 + 0.1);
      this.root.add(cheek);
    }
    const desk = rbox(0.5, 0.25, 0.01, M.lacquer, 0.004);
    desk.position.set(0, H + 0.02, D / 2 - 0.1 - 0.06);
    desk.rotation.x = -0.35;
    this.root.add(desk);
    const pedal = rbox(0.06, 0.012, 0.14, M.brass, 0.003);
    pedal.position.set(0.1, 0.03, D / 2 - 0.1 + 0.12);
    pedal.rotation.x = -0.3;
    this.root.add(pedal);
    for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
      const foot = rbox(0.06, 0.1, 0.06, M.lacquer, 0.006);
      foot.position.set(sx * (W / 2 - 0.06), 0.05, -0.1 + sz * (D / 2 - 0.06));
      this.root.add(foot);
    }

    const z0 = D / 2 - 0.1 + 0.1;             // front of the key bed
    naturals.forEach((p, i) => this._key(p, i * slot - span / 2 + kw / 2, 0, kw, 0.15, keyY, z0, M.ivory));
    for (let p = lo; p <= hi; p++) {
      if (!isAccidental(p)) continue;
      const left = naturals.filter((n) => n < p).length - 1;
      this._key(p, (left + 1) * slot - span / 2 - gap / 2, 1, kw * 0.55, 0.095, keyY + 0.012, z0, M.ebony);
    }
    this.width = W + 0.1;
    this.depth = D + 0.3;
    this.topHeight = H + 0.26;        // the music desk
  }

  _key(pitch, x, row, w, d, y, z0, baseMat) {
    const mat = this._glow(baseMat);
    const g = new THREE.Group();              // pivot at the back edge of the key
    g.position.set(x, y, z0 - 0.15);
    const mesh = rbox(w, row ? 0.014 : 0.012, d, mat, 0.002);
    mesh.position.set(0, 0, d / 2);
    g.add(mesh);
    this.root.add(g);
    this.keys.set(pitch, { g, mat });
  }

  update(t, env, beat) {
    super.update(t, env, beat);
    if (t === null) { for (const k of this.keys.values()) { k.g.rotation.x = 0; k.mat.emissiveIntensity = 0; } return; }
    const n = this.next(t);
    const a = this.anchors;
    const pressed = new Map();
    for (let i = Math.max(0, n - 12); i < n; i++) {
      let p = this.pitches ? this.pitches[i] : null;
      /* an unpitched strike (the introduction) presses a key mid-keyboard */
      if (p === null || p === undefined) p = 60 + ((i * 5) % 7);
      pressed.set(p, a[i]);
    }
    for (const k of this.keys.values()) { k.g.rotation.x = 0; k.glow = 0; }
    for (const [pitch, at] of pressed) {
      const src = this.keys.get(pitch);
      if (!src) continue;
      const d = this.flash(pitch, at, 0.26);
      if (d <= 0) continue;
      src.g.rotation.x = d * 0.06;       // front edge dips
      /* and the light carries to the keys either side: one 23 mm key lighting
       * alone is two pixels from out front, which no one reads as a note */
      for (const k of this.keys.values()) {
        const dx = (k.g.position.x - src.g.position.x) / SPREAD;
        const g = d * Math.exp(-dx * dx);
        if (g > k.glow) k.glow = g;
      }
    }
    for (const k of this.keys.values()) k.mat.emissiveIntensity = k.glow * 1.1;
  }
}

/* -------------------------------------------------------------------------- */
/* Woodblock                                                                  */
/* -------------------------------------------------------------------------- */

class Woodblock extends Instrument {
  constructor(entry) {
    super(entry);
    const h = 0.95;
    const stand = new THREE.Group();
    tripod(stand, h);
    this.root.add(stand);
    const arm = cylZ(0.008, 0.16, M.chrome);
    arm.position.set(0, h, 0.06);
    this.root.add(arm);

    this.mat = this._glow(M.maple);
    const block = rbox(0.24, 0.075, 0.075, this.mat, 0.008);
    block.position.set(0, h + 0.05, 0.1);
    this.root.add(block);
    const slot = rbox(0.2, 0.014, 0.03, M.carbon, 0.003);
    slot.position.set(0, h + 0.072, 0.13);
    this.root.add(slot);

    this.stick = new THREE.Group();           // pivot at the hand, above and behind
    this.stick.position.set(0.06, h + 0.3, -0.18);
    const shaft = cylZ(0.007, 0.4, M.hickory);
    shaft.position.z = 0.2;
    this.stick.add(shaft);
    const tip = new THREE.Mesh(new THREE.SphereGeometry(0.009, 10, 8), M.hickory);
    tip.position.z = 0.4;
    this.stick.add(tip);
    this.root.add(this.stick);
    this.width = 0.9;
    this.depth = 0.6;
    this.topHeight = h + 0.4;         // the raised stick
  }

  update(t, env, beat) {
    super.update(t, env, beat);
    if (t === null) { this.stick.rotation.x = 0.55; this.mat.emissiveIntensity = 0; return; }
    const n = this.next(t);
    const a = this.anchors;
    const last = n > 0 ? a[n - 1] : null;
    const next = n < a.length ? a[n] : null;
    const h = strokeHeight(t, last, next, 1.0);
    this.stick.rotation.x = 0.38 + h * 0.55;   // tip up at rest, further for the windup
    const age = this.since('hit', last);
    this.mat.emissiveIntensity = age < FLASH ? (1 - age / FLASH) * 1.1 : 0;
  }
}

/* -------------------------------------------------------------------------- */
/* Tambourine                                                                 */
/* -------------------------------------------------------------------------- */

class Tambourine extends Instrument {
  constructor(entry) {
    super(entry);
    const h = 1.0;
    const stand = new THREE.Group();
    tripod(stand, h);
    this.root.add(stand);
    const clamp = rbox(0.05, 0.04, 0.06, M.carbon, 0.006);
    clamp.position.set(0, h + 0.02, 0.02);
    this.root.add(clamp);

    this.ring = new THREE.Group();
    this.ring.position.set(0, h + 0.17, 0.05);
    this.ring.rotation.x = 0.55;              // tilted toward the audience
    const R = 0.125;
    const hoop = new THREE.Mesh(new THREE.TorusGeometry(R, 0.016, 12, 48), M.maple);
    this.ring.add(hoop);
    const head = new THREE.Mesh(new THREE.CircleGeometry(R - 0.004, 48), M.skin);
    head.position.z = 0.006;
    this.ring.add(head);
    this.jingleMat = this._glow(M.brass);
    this.jingles = [];
    for (let i = 0; i < 16; i++) {
      const ang = (i / 16) * Math.PI * 2;
      for (const dz of [-0.006, 0.006]) {
        const j = new THREE.Mesh(new THREE.CylinderGeometry(0.021, 0.021, 0.0012, 20), this.jingleMat);
        j.rotation.x = Math.PI / 2;
        j.position.set(Math.cos(ang) * R, Math.sin(ang) * R, dz);
        this.ring.add(j);
        this.jingles.push({ mesh: j, phase: i * 1.7 + dz * 100, base: dz });
      }
    }
    this.root.add(this.ring);
    this.width = 0.9;
    this.depth = 0.5;
    this.topHeight = h + 0.32;        // the top of the hoop
  }

  update(t, env, beat) {
    super.update(t, env, beat);
    if (t === null) { this.ring.rotation.set(0.55, 0, 0); this.jingleMat.emissiveIntensity = 0; return; }
    const n = this.next(t);
    const a = this.anchors;
    const last = n > 0 ? a[n - 1] : null;
    const age = this.since('hit', last);
    /* Brightest at the strike, then out -- not a shape that rises into it.
     * A rising curve is zero at the moment it fires, and the frame that
     * fires it is the only one that may see it: measured, the tambourine
     * was the one instrument that never lit during its own introduction. */
    const kick = age < 0.22 ? (1 - age / 0.22) : 0;
    this.ring.rotation.x = 0.55 + kick * 0.3 + Math.sin(t * 31) * env * 0.04;
    this.ring.rotation.z = Math.sin(t * 23) * env * 0.05;
    for (const j of this.jingles) j.mesh.position.z = j.base + Math.sin(t * 40 + j.phase) * env * 0.004;
    this.jingleMat.emissiveIntensity = kick * 1.1 + env * 0.3;
  }
}

/* -------------------------------------------------------------------------- */
/* Shaker                                                                     */
/* -------------------------------------------------------------------------- */

class Shaker extends Instrument {
  constructor(entry) {
    super(entry);
    const h = 0.9;
    const stand = new THREE.Group();
    tripod(stand, h);
    this.root.add(stand);
    const cradle = rbox(0.12, 0.03, 0.06, M.carbon, 0.006);
    cradle.position.set(0, h + 0.015, 0);
    this.root.add(cradle);

    this.arm = new THREE.Group();             // pivot in the cradle
    this.arm.position.set(0, h + 0.05, 0);
    /* Brushed steel, not chrome: a white mirror with an emissive on top was
     * a blown white bar, and this part strikes 504 times -- more than any
     * other -- so it was lit essentially from end to end of the piece. */
    this.mat = this._glow(M.steel);
    const tube = cylX(0.03, 0.26, this.mat, 24);
    this.arm.add(tube);
    for (const sx of [-1, 1]) {
      const cap = new THREE.Mesh(new THREE.SphereGeometry(0.03, 20, 14), M.carbon);
      cap.position.x = sx * 0.13;
      this.arm.add(cap);
    }
    this.root.add(this.arm);
    /* an egg shaker left on the base plate, because there is always one */
    const egg = new THREE.Mesh(new THREE.SphereGeometry(0.028, 16, 12), M.plastic);
    egg.scale.set(1, 1.3, 1);
    egg.position.set(0.22, 0.03, 0.2);
    egg.rotation.z = 1.2;
    this.root.add(egg);
    this.width = 0.9;
    this.depth = 0.5;
    this.topHeight = h + 0.1;
  }

  update(t, env, beat) {
    super.update(t, env, beat);
    if (t === null) { this.arm.rotation.z = 0; this.mat.emissiveIntensity = 0; return; }
    const n = this.next(t);
    const a = this.anchors;
    const last = n > 0 ? a[n - 1] : null;
    const next = n < a.length ? a[n] : null;
    const side = (n % 2 === 0) ? 1 : -1;
    let u = 1;
    if (last !== null && next !== null) u = smooth((t - last) / Math.max(0.05, next - last));
    const amp = 0.22 + env * 0.4;
    this.arm.rotation.z = (-side + 2 * side * u) * amp * (next === null ? 0.3 : 1);
    this.arm.position.x = Math.sin(this.arm.rotation.z) * -0.05;
    const age = this.since('hit', last);
    /* barely a glint. The swing is what shows it playing. */
    this.mat.emissiveIntensity = (age < 0.1 ? (1 - age / 0.1) : 0) * 0.22;
  }
}

/* -------------------------------------------------------------------------- */
/* Handbells                                                                  */
/* -------------------------------------------------------------------------- */

class Bells extends Instrument {
  constructor(entry) {
    super(entry);
    const h = 0.82;
    const W = 1.0, D = 0.45;
    const top = rbox(W, 0.03, D, M.frame, 0.006);
    top.position.y = h - 0.05;
    this.root.add(top);
    const pad = rbox(W - 0.04, 0.05, D - 0.04, M.foam, 0.012);
    pad.position.y = h - 0.01;
    this.root.add(pad);
    for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
      const leg = cylY(0.014, 0.014, h - 0.06, M.chrome);
      leg.position.set(sx * (W / 2 - 0.06), (h - 0.06) / 2, sz * (D / 2 - 0.06));
      this.root.add(leg);
    }

    this.bells = [];
    const n = 4;
    for (let i = 0; i < n; i++) {
      const g = new THREE.Group();            // pivot at the handle's top
      const x = (i - (n - 1) / 2) * 0.22;
      const r = 0.075 - i * 0.008;
      g.position.set(x, h + 0.19, 0);
      const mat = this._glow(M.brass);
      const cup = new THREE.Mesh(bellProfile(r), mat);
      cup.position.y = -0.19;
      g.add(cup);
      const handle = rbox(0.022, 0.1, 0.012, M.leather, 0.004);
      handle.position.y = -0.05;
      g.add(handle);
      const grip = new THREE.Mesh(new THREE.SphereGeometry(0.014, 12, 10), M.leather);
      g.add(grip);
      this.root.add(g);
      this.bells.push({ g, mat });
    }
    this.width = W + 0.2;
    this.depth = D + 0.3;
    this.topHeight = h + 0.25;
  }

  update(t, env, beat) {
    super.update(t, env, beat);
    if (t === null) { for (const b of this.bells) { b.g.rotation.x = 0; b.mat.emissiveIntensity = 0; } return; }
    const n = this.next(t);
    const a = this.anchors;
    const ring = this.bells.map(() => null);
    for (let i = Math.max(0, n - 8); i < n; i++) ring[i % this.bells.length] = a[i];
    this.bells.forEach((b, k) => {
      const at = ring[k];
      const age = this.since(k, at);
      b.g.rotation.x = age < 0.9 ? Math.sin(age * 20) * Math.exp(-age * 4) * 0.45 : 0;
      b.mat.emissiveIntensity = age < 0.7 ? Math.max(0, 1 - age / 0.7) * 1.0 : 0;
    });
  }
}

/* -------------------------------------------------------------------------- */
/* Conductor: a podium, a stand, and a baton that leaves light behind it      */
/* -------------------------------------------------------------------------- */

class Conductor extends Instrument {
  constructor(entry) {
    super(entry);
    const podium = rbox(1.1, 0.22, 1.1, M.carbon, 0.02);
    podium.position.y = 0.11;
    this.root.add(podium);
    /* a lit strip around the top edge: the one near-future flourish the
     * conductor gets, because a podium is otherwise a box */
    this.stripMat = this._glow(M.plastic);
    this.stripMat.emissiveIntensity = 0.9;
    const strip = new THREE.Mesh(new THREE.TorusGeometry(0.72, 0.008, 6, 4), this.stripMat);
    strip.rotation.x = Math.PI / 2;
    strip.rotation.z = Math.PI / 4;
    strip.position.y = 0.225;
    this.root.add(strip);

    const stand = new THREE.Group();
    stand.position.set(0, 0.22, -0.3);
    tripod(stand, 1.1, 0.01);
    const desk = rbox(0.5, 0.36, 0.012, M.carbon, 0.004);
    desk.position.set(0, 1.26, 0.04);
    desk.rotation.x = -0.5;
    stand.add(desk);
    /* the score is a tablet: dark glass with a faint light in it. A white
     * sheet of paper was the brightest thing on the stage and, mirrored in
     * the floor, a glaring bar standing on the podium. */
    this.tabletMat = this._glow(M.lacquer);
    this.tabletMat.emissiveIntensity = 0.35;
    const sheet = rbox(0.38, 0.26, 0.006, this.tabletMat, 0.004);
    sheet.position.set(0, 1.27, 0.05);
    sheet.rotation.x = -0.5;
    stand.add(sheet);
    this.root.add(stand);

    this.hand = new THREE.Group();
    this.hand.position.set(0.28, 1.55, 0.1);
    const baton = cylZ(0.004, 0.42, M.plastic);
    baton.position.z = 0.21;
    this.hand.add(baton);
    const cork = cylZ(0.012, 0.08, M.hickory);
    this.hand.add(cork);
    this.tipMat = this._glow(M.plastic);
    const tip = new THREE.Mesh(new THREE.SphereGeometry(0.012, 12, 10), this.tipMat);
    tip.position.z = 0.42;
    this.hand.add(tip);
    this.tip = tip;
    this.root.add(this.hand);

    /* the trail: the last N tip positions as a fading line */
    this.trailN = 28;
    this.trailPos = new Float32Array(this.trailN * 3);
    const tg = new THREE.BufferGeometry();
    tg.setAttribute('position', new THREE.BufferAttribute(this.trailPos, 3));
    this.trailMat = new THREE.LineBasicMaterial({
      color: this.accent, transparent: true, opacity: 0.9,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    this.trail = new THREE.Line(tg, this.trailMat);
    this.trail.frustumCulled = false;
    this.trail.visible = false;              // until the baton has moved
    this.root.add(this.trail);
    this._tipWorld = new THREE.Vector3();
    this._trailFilled = 0;

    this.width = 1.3;
    this.depth = 1.3;
    this.topHeight = 0.22;            // the podium; its placard hangs below
    this.labelBelow = true;
    /* The conductor stands downstage of everyone and its baton reaches higher
     * than any instrument. A placard above it would cross the whole back row;
     * under the podium there is nothing but floor. */
  }

  setAccent(c) { super.setAccent(c); this.trailMat.color.copy(c); }

  update(t, env, beat) {
    super.update(t, env, beat);
    const inPiece = t !== null && beat && beat.beat >= 0 && beat.beat <= beat.total;
    if (!inPiece) {
      this.hand.rotation.set(-0.2, 0, 0);
      this.tipMat.emissiveIntensity = 0.3;
      this.trailMat.opacity = Math.max(0, this.trailMat.opacity - 0.03);
      return;
    }
    /* a four-beat pattern: down, in, out, up -- with the ictus, the bounce
     * off the bottom, on every beat */
    const b = beat.inBar;
    const i = Math.floor(b), f = b - i;
    const pts = [[0, -1], [-0.7, -0.45], [0.7, -0.35], [0, 0.6]];
    const from = pts[i % pts.length], to = pts[(i + 1) % pts.length];
    const bounce = Math.sin(Math.PI * Math.min(1, f * 1.8)) * 0.35;
    const x = from[0] + (to[0] - from[0]) * smooth(f);
    const y = from[1] + (to[1] - from[1]) * smooth(f) + bounce;
    this.hand.rotation.set(-0.2 + y * 0.55, -x * 0.6, 0);
    this.tipMat.emissiveIntensity = (f < 0.15 ? (1 - f / 0.15) * 2.5 : 0) + 0.6;
    /* the conductor has no column on the wall; its podium edge is its meter */
    this.stripMat.emissiveIntensity = 0.5 + env * 1.4;

    this.tip.getWorldPosition(this._tipWorld);
    this.root.worldToLocal(this._tipWorld);
    const p = this.trailPos;
    p.copyWithin(3, 0, (this.trailN - 1) * 3);
    p[0] = this._tipWorld.x; p[1] = this._tipWorld.y; p[2] = this._tipWorld.z;
    if (this._trailFilled < this.trailN) {
      this._trailFilled++;
      for (let k = this._trailFilled; k < this.trailN; k++) {
        p[k * 3] = p[0]; p[k * 3 + 1] = p[1]; p[k * 3 + 2] = p[2];
      }
    }
    this.trail.geometry.attributes.position.needsUpdate = true;
    this.trail.visible = true;
    this.trailMat.opacity = 0.9;
  }
}

/* -------------------------------------------------------------------------- */

export function build(entry) {
  const kind = entry.instrument || entry.role;
  let inst;
  switch (kind) {
    case 'marimba':
      inst = new Keyed(entry, {
        rangeLo: 36, rangeHi: 96,                 // five octaves, C2..C7
        barMat: M.padauk, widthLo: 0.062, widthHi: 0.040, gap: 0.009,
        lenLo: 0.56, lenHi: 0.20, thick: 0.024, deck: 0.92,
        tubes: true, tubeMat: M.darkBrass, tubeR: 0.034, tubeLo: 0.72, tubeHi: 0.1,
        frameMat: M.frame, endMat: M.rosewood,
        malletMat: M.yarn, malletLen: 0.42, malletR: 0.03, lift: 0.26,
      });
      break;
    case 'vibraphone':
      inst = new Keyed(entry, {
        rangeLo: 53, rangeHi: 89,                 // three octaves, F3..F6
        barMat: M.aluminium, widthLo: 0.057, widthHi: 0.038, gap: 0.009,
        lenLo: 0.38, lenHi: 0.17, thick: 0.013, deck: 0.9,
        tubes: true, tubeMat: M.steel, tubeR: 0.028, tubeLo: 0.46, tubeHi: 0.08, fans: true,
        frameMat: M.frame, endMat: M.frame, motor: true, sustain: true,
        malletMat: M.cordHead, malletLen: 0.4, malletR: 0.026, lift: 0.22,
      });
      break;
    case 'glockenspiel':
      inst = new Keyed(entry, {
        rangeLo: 55, rangeHi: 84,                 // G3..C6 (sounds two octaves up)
        barMat: M.steel, widthLo: 0.03, widthHi: 0.022, gap: 0.006,
        lenLo: 0.16, lenHi: 0.08, thick: 0.008, deck: 0.98,
        tubes: false, frameMat: M.lacquer, caseStyle: 'case',
        malletMat: M.plastic, malletLen: 0.3, malletR: 0.012, lift: 0.14,
      });
      break;
    case 'celesta': inst = new Celesta(entry); break;
    case 'woodblock': inst = new Woodblock(entry); break;
    case 'tambourine': inst = new Tambourine(entry); break;
    case 'shaker': inst = new Shaker(entry); break;
    case 'bells': inst = new Bells(entry); break;
    case 'conductor': inst = new Conductor(entry); break;
    default: {
      inst = new Instrument(entry);
      const p = rbox(0.6, 0.9, 0.6, M.carbon, 0.02);
      p.position.y = 0.45;
      inst.root.add(p);
    }
  }
  return inst.finish();
}

/* -------------------------------------------------------------------------- */

/** A handbell, turned on a lathe from a real bell's profile. */
function bellProfile(r) {
  const pts = [];
  const prof = [[0.0, 1.0], [0.18, 0.98], [0.3, 0.9], [0.4, 0.74], [0.48, 0.55],
                [0.56, 0.36], [0.7, 0.2], [0.9, 0.08], [1.0, 0.0]];
  for (const [x, y] of prof) pts.push(new THREE.Vector2(x * r, y * r * 1.5));
  return new THREE.LatheGeometry(pts, 32);
}

/** A grain texture that reads as wood from two metres: long streaks with a
 *  little wander. Grey, so each material's own colour tints it. */
function woodGrain() {
  const w = 256, h = 256;
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d');
  g.fillStyle = '#b4b4b4';
  g.fillRect(0, 0, w, h);
  let seed = 7;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  for (let i = 0; i < 160; i++) {
    const x = rnd() * w, amp = 2 + rnd() * 6, ph = rnd() * 6, v = 70 + rnd() * 110;
    g.strokeStyle = `rgba(${v},${v},${v},${0.25 + rnd() * 0.35})`;
    g.lineWidth = 0.6 + rnd() * 1.6;
    g.beginPath();
    for (let y = 0; y <= h; y += 8) g.lineTo(x + Math.sin(y / 40 + ph) * amp, y);
    g.stroke();
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(1, 3);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}
