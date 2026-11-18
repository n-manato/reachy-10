/* The backdrop: one Heisei head-unit display, scaled up behind the stage.
 *
 * Drawn from a photograph of a Kenwood DPX-660MD, and the things that make it
 * that and not a row of meters:
 *
 *   ONE FIELD, ONE WAVEFORM. The whole background is a single display driven
 *   by a single sound -- `preview.wav`, the mix of all nine stems -- so it
 *   rises and falls as one body. The per-stem spectra still drive the
 *   instruments; nothing behind them is per-instrument any more.
 *
 *   COLUMNS OF EQUAL SEGMENTS. Upright bars, side by side, every segment the
 *   same size. This replaced a radial fan whose columns grew toward the
 *   edges: at the rim its cells were nearly four times the size of the ones
 *   in the middle, and big bright slabs scattered around an arc read as
 *   floating panels, not as a meter. A head unit's display is a grid, and a
 *   grid is what makes the level legible -- you count segments.
 *
 *   ON A GENTLE ARC. The feet stand on a wide curve whose ends fall away
 *   from the camera, and each column turns with it. That is the whole of the
 *   perspective: a flat wall behind an orchestra is a flat wall, and a hard
 *   fan was a fairground. Twenty-five degrees at the ends is enough to read
 *   as depth.
 *
 *   MIRRORED. Twenty bands out from the centre, drawn to both sides: bass in
 *   the middle moving the whole body of it, highs rippling at the ends.
 *
 *   PEAK HOLD. A cap per column, parked at the highest level that column has
 *   reached lately, hanging half a second before it falls. It is the thing
 *   that makes a segmented analyser read as an analyser rather than as a bar
 *   chart: the bars jump and drop, the caps linger and trace the shape.
 *
 *   THE GRADIENT runs up each column, not across the field. Sampled off the
 *   photo: green at the base, through white-cyan, to blue, to violet at the
 *   tips, with the lit cores blown out white.
 */

import * as THREE from 'three';

const AXIS_Y = new THREE.Vector3(0, 1, 0);

export const PLATFORM = { w: 28, d: 26, z: -1.0, r: 2.0 };   // metres; the stage floats on this

export const WALL = {
  z: -7.0,            // well behind the back row (the marimba reaches -1.8)
  baseY: 0.55,        // the feet, just clear of the floor
  /* The arc the feet stand on. Radius and span together decide how far the
   * ends fall back: 55 m over 19.5 either way turns the end columns by 20
   * degrees and sets them 3.4 m further away than the middle. The span is
   * what it is because the field has to run off both sides of the frame --
   * a wall that stops inside it is an object on the stage, not a backdrop,
   * and the camera's horizontal half-angle here is 35 degrees. */
  curveR: 55.0,
  halfSpan: 19.5,
  bands: 20,          // half the wall; mirrored to both sides
  /* Thirteen segments reaching 4.45 m, not eighteen reaching 6.
   * At eighteen a loud chorus lit every column to the top of the frame and
   * the backdrop stopped being a meter: measured, 91% of the frame was lit
   * and the mean brightness was half of white. A display is mostly dark. */
  cells: 13,          // segments per column
  cellH: 0.30,        // every segment the same: it is a grid, not a fan
  cellW: 0.60,        // of a 1.0 m column pitch, so the gaps stay dark
  gapY: 0.40,         // of each slot left dark, vertically
};
WALL.cols = WALL.bands * 2;
WALL.pitch = (WALL.halfSpan * 2) / (WALL.cols - 1);
/* the tallest point of the field, for the placards to clear */
WALL.top = WALL.baseY + WALL.cells * WALL.cellH;

const RELEASE = 1.5;          // full scale to nothing in two thirds of a second
const FLOOR_CELLS = 1;        // the bottom segment never quite goes out
const PEAK_HOLD = 0.5;        // seconds a peak cap hangs before it starts to fall
const PEAK_FALL = 0.9;        // and how fast it falls once it does

/** The gradient along a column, base to tip, as sampled from the photograph:
 *  green, white-cyan, blue, violet. Returned as HSL so the lit core can be
 *  pushed toward white by value alone. */
function gradient(f, out) {
  let h, s, l;
  if (f < 0.22) { const u = f / 0.22; h = 150 - 6 * u; s = 0.85; l = 0.34 + 0.16 * u; }
  else if (f < 0.42) { const u = (f - 0.22) / 0.20; h = 144 + 40 * u; s = 0.85 - 0.5 * u; l = 0.50 + 0.22 * u; }
  else if (f < 0.72) { const u = (f - 0.42) / 0.30; h = 184 + 24 * u; s = 0.35 + 0.5 * u; l = 0.72 - 0.24 * u; }
  else { const u = (f - 0.72) / 0.28; h = 208 + 74 * u; s = 0.85; l = 0.48 - 0.10 * u; }
  return out.setHSL(((h % 360) + 360) % 360 / 360, s, l);
}

export class Environment {
  constructor(scene, seats, duration) {
    this.scene = scene;
    this.duration = duration || 1;
    this.accent = new THREE.Color(0x40c8ff);
    this.group = new THREE.Group();
    scene.add(this.group);
    this.accentMats = [];
    this._c = new THREE.Color();
    this._m = new THREE.Matrix4();
    this._q = new THREE.Quaternion();
    this._v = new THREE.Vector3();
    this._s = new THREE.Vector3();

    this.shown = new Float32Array(WALL.bands);
    this.peak = new Float32Array(WALL.bands);
    this.peakAt = new Float32Array(WALL.bands);

    this._rim();
    this._wall();
    this._cones(seats);
  }

  /* ---------------------------------------------------------- the platform */

  _rim() {
    const shape = roundedRect(PLATFORM.w, PLATFORM.d, PLATFORM.r);
    const pts = shape.getPoints(96).map((p) => new THREE.Vector3(p.x, 0.012, -p.y));
    const g = new THREE.BufferGeometry().setFromPoints(pts);
    const mat = new THREE.LineBasicMaterial({ color: this.accent, toneMapped: false, transparent: true, opacity: 0.9 });
    this.accentMats.push(mat);
    const loop = new THREE.LineLoop(g, mat);
    loop.position.z = PLATFORM.z;
    this.group.add(loop);
    const slab = new THREE.Mesh(
      new THREE.ExtrudeGeometry(shape, { depth: 0.5, bevelEnabled: true, bevelSize: 0.08, bevelThickness: 0.08, bevelSegments: 3 }),
      new THREE.MeshStandardMaterial({ color: 0x0a0d16, roughness: 0.5, metalness: 0.5 }));
    slab.rotation.x = Math.PI / 2;
    /* the bevel grows 8 cm *up* from the extrusion's origin; sat at -0.02 it
     * put a lit, metallic slab top over the mirror and the floor went grey */
    slab.position.set(0, -0.1, PLATFORM.z);
    this.group.add(slab);
  }

  /* ------------------------------------------------------------- the wall */

  /** Where column `c` stands: its place on the arc, and the way it faces.
   *  Shared by the cells, the caps and the rim so the three cannot drift. */
  static _at(c, v, q) {
    const W = WALL;
    const lateral = -W.halfSpan + c * W.pitch;
    const a = lateral / W.curveR;                 // the angle it has turned through
    if (q) q.setFromAxisAngle(AXIS_Y, a);
    return v.set(W.curveR * Math.sin(a),
                 0,
                 W.z - W.curveR * (1 - Math.cos(a)));
  }

  _wall() {
    const W = WALL;
    /* one cell geometry, a unit square; every instance is the same size --
     * that is the point of a grid -- so only the position changes */
    const geo = new THREE.PlaneGeometry(1, 1);
    const mat = new THREE.MeshBasicMaterial({
      toneMapped: false, transparent: true, blending: THREE.AdditiveBlending,
      depthWrite: false, side: THREE.DoubleSide,
    });
    this.cols = [];
    this.cells = new THREE.InstancedMesh(geo, mat, W.cols * W.cells);
    this.cells.instanceMatrix.setUsage(THREE.StaticDrawUsage);   // laid out once
    this.cells.frustumCulled = false;

    const m = this._m, q = this._q, v = this._v, sc = this._s;
    sc.set(W.cellW, W.cellH * (1 - W.gapY), 1);
    let i = 0;
    for (let c = 0; c < W.cols; c++) {
      /* band 0 at the centre on both sides, band 19 at the ends: the bass
       * swells the body of it, the highs ripple off the tips */
      const band = Math.abs(c - (W.cols - 1) / 2) / (W.cols / 2) * W.bands | 0;
      Environment._at(c, v, q);
      const foot = v.clone();
      for (let k = 0; k < W.cells; k++) {
        v.set(foot.x, W.baseY + (k + 0.5) * W.cellH, foot.z);
        m.compose(v, q, sc);
        this.cells.setMatrixAt(i, m);
        this.cells.setColorAt(i, this._c.setScalar(0));
        i++;
      }
      this.cols.push({ band: Math.min(W.bands - 1, band), foot, rot: q.clone() });
    }
    this.cells.instanceMatrix.needsUpdate = true;
    this.cells.instanceColor.needsUpdate = true;
    this.group.add(this.cells);

    /* The peak-hold caps: one per column, see the note at the top. */
    this.caps = new THREE.InstancedMesh(geo, mat.clone(), W.cols);
    this.caps.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.caps.frustumCulled = false;
    this.group.add(this.caps);

    /* the rim: the line every column stands on, the one line in the photo */
    const arc = [];
    for (let c = 0; c < W.cols; c++) {
      Environment._at(c, v, null);
      arc.push(new THREE.Vector3(v.x, W.baseY - W.cellH * 0.6, v.z));
    }
    const rimMat = new THREE.LineBasicMaterial({
      color: this.accent, toneMapped: false, transparent: true, opacity: 0.5,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    this.accentMats.push(rimMat);
    this.group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(arc), rimMat));

    /* the glow the whole field throws, brightening with the mix */
    const glowMat = new THREE.MeshBasicMaterial({
      map: fanGlowTexture(), color: 0x6fa8ff, transparent: true, opacity: 0,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    this.fanGlow = new THREE.Mesh(new THREE.PlaneGeometry(30, 10), glowMat);
    this.fanGlow.position.set(0, W.baseY + 2.4, W.z - 0.6);
    this.group.add(this.fanGlow);
  }

  /* ---------------------------------------------------------------- cones */

  _cones(seats) {
    this.cones = new Map();
    const coneGeo = new THREE.ConeGeometry(1.0, 4.6, 32, 1, true);
    coneGeo.translate(0, -2.3, 0);
    const discGeo = new THREE.CircleGeometry(1.0, 40);
    discGeo.rotateX(-Math.PI / 2);
    for (const s of seats) {
      const coneMat = new THREE.MeshBasicMaterial({
        color: this.accent, transparent: true, opacity: 0,
        blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, fog: false,
      });
      const discMat = new THREE.MeshBasicMaterial({
        color: this.accent, transparent: true, opacity: 0,
        blending: THREE.AdditiveBlending, depthWrite: false,
      });
      this.accentMats.push(coneMat, discMat);
      const cone = new THREE.Mesh(coneGeo, coneMat);
      cone.position.set(s.x, 4.8, s.z);
      cone.scale.set(0.3 + s.width * 0.14, 1, 0.3 + s.width * 0.14);
      const disc = new THREE.Mesh(discGeo, discMat);
      disc.position.set(s.x, 0.012, s.z);
      disc.scale.setScalar(0.5 + s.width * 0.55);
      this.group.add(cone, disc);
      this.cones.set(s.id, { cone, disc, coneMat, discMat, gain: s.conductor ? 0.45 : 1.0 });
    }
  }

  /* --------------------------------------------------------------- update */

  /** `mix` is this frame's row of `mix_bands` (0..100), or null when nothing
   *  is playing. The per-seat `levels` still drive the stage lights. */
  update(t, hue, now, levels, mix, dt) {
    this.accent.setHSL(hue / 360, 0.9, 0.62);
    for (const m of this.accentMats) m.color.copy(this.accent);

    const W = WALL;
    const lit = this._c;
    let sum = 0;
    for (let b = 0; b < W.bands; b++) {
      const target = mix ? mix[b] / 100 : 0;
      this.shown[b] = target >= this.shown[b] ? target : Math.max(target, this.shown[b] - RELEASE * dt);
      sum += this.shown[b];
    }
    this.room = sum / W.bands;

    let i = 0;
    for (const col of this.cols) {
      const level = this.shown[col.band];
      const count = Math.max(FLOOR_CELLS, Math.round(level * W.cells));
      for (let k = 0; k < W.cells; k++) {
        if (k < count) {
          const f = k / (W.cells - 1);
          gradient(f, lit);
          /* the leading cells blown toward white, the way the photo's are */
          const lead = Math.max(0, 1 - (count - 1 - k) / 2.5);
          const dim = level < 0.02 ? 0.3 : 1.0;
          lit.lerp(WHITE, lead * 0.55).multiplyScalar(dim);
        } else {
          lit.setScalar(0);
        }
        this.cells.setColorAt(i, lit);
        i++;
      }
    }
    this.cells.instanceColor.needsUpdate = true;

    /* the caps, after the bars: a cap rides one segment above the lit stack */
    const m = this._m, v = this._v, sc = this._s;
    sc.set(W.cellW, W.cellH * 0.3, 1);
    let c = 0;
    for (const col of this.cols) {
      const b = col.band;
      const level = this.shown[b];
      if (level >= this.peak[b]) { this.peak[b] = level; this.peakAt[b] = now; }
      else if (now - this.peakAt[b] > PEAK_HOLD) {
        this.peak[b] = Math.max(level, this.peak[b] - PEAK_FALL * dt);
      }
      const pc = this.peak[b] * W.cells;
      if (pc > 0.6) {
        v.set(col.foot.x, W.baseY + (pc + 0.6) * W.cellH, col.foot.z);
        m.compose(v, col.rot, sc);
        gradient(Math.min(1, pc / W.cells), lit);
        lit.lerp(WHITE, 0.45);
      } else {
        m.makeScale(0, 0, 0);
        lit.setScalar(0);
      }
      this.caps.setMatrixAt(c, m);
      this.caps.setColorAt(c, lit);
      c++;
    }
    this.caps.instanceMatrix.needsUpdate = true;
    this.caps.instanceColor.needsUpdate = true;

    /* Kept low on purpose: the dark between the segments is what makes the
     * field read as a display. Measured, a wash that lifted the gaps to a
     * fifth of white turned forty crisp columns into one sheet of light. */
    this.fanGlow.material.opacity = Math.min(0.08, this.room * 0.18);

    for (const [id, c] of this.cones) {
      const l = levels.get(id) || 0;
      const soft = (l * l * 0.8 + l * 0.2) * c.gain;
      c.coneMat.opacity = soft * 0.085;
      c.discMat.opacity = soft * 0.34;
    }
  }
}

const WHITE = new THREE.Color(0xffffff);

/* -------------------------------------------------------------------------- */

export function roundedRect(w, d, r) {
  const s = new THREE.Shape();
  const x = -w / 2, y = -d / 2;
  s.moveTo(x + r, y);
  s.lineTo(x + w - r, y);
  s.quadraticCurveTo(x + w, y, x + w, y + r);
  s.lineTo(x + w, y + d - r);
  s.quadraticCurveTo(x + w, y + d, x + w - r, y + d);
  s.lineTo(x + r, y + d);
  s.quadraticCurveTo(x, y + d, x, y + d - r);
  s.lineTo(x, y + r);
  s.quadraticCurveTo(x, y, x + r, y);
  return s;
}

/** A broad soft bloom centred low: what the whole field throws back. */
function fanGlowTexture() {
  const n = 128, c = document.createElement('canvas');
  c.width = c.height = n;
  const g = c.getContext('2d');
  const img = g.createImageData(n, n);
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      const dx = ((x / (n - 1)) * 2 - 1) * 0.75;
      const dy = (y / (n - 1)) * 2 - 1;
      const d = Math.sqrt(dx * dx + dy * dy);
      const o = (y * n + x) * 4;
      img.data[o] = img.data[o + 1] = img.data[o + 2] = 255;
      img.data[o + 3] = Math.round(255 * Math.max(0, 1 - d) ** 2);
    }
  }
  g.putImageData(img, 0, 0);
  return new THREE.CanvasTexture(c);
}
