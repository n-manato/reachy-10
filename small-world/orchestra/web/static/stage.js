/* The stage: nine seats, and on each the thing that robot is playing.
 *
 * TWO VIEWS, ONE SEATING ORDER
 *
 * The default view is the instruments (`instruments.js`), at their real size
 * on a black-lacquer platform in front of one head-unit display: a fan of
 * segmented columns driven by the whole mix as a single waveform
 * (`environment.js`).
 * The robots have no instruments -- the sound is nine speakers and the motion
 * is acting -- so the one thing an audience cannot see is which seat is the
 * marimba, and the real robots are standing right next to the screen already.
 * Seats run left to right in the robots' own order, so a glance from screen to
 * table answers the question.
 *
 * The twin is the second view, toggled with T: nine Reachy Minis playing the
 * frames the nine robots are playing. It is kept for what it is good at --
 * rehearsing with the fleet switched off, and checking a build's motion before
 * a robot does it. It lives in its own compact arc with its own camera.
 *
 * THE LOOK is three things the first version did not have, and they are what
 * separates a model from a toy: an environment for the metal to reflect, a
 * floor that reflects the instruments, and a bloom pass so that what glows,
 * glows. Shadows on the floor ground everything. All of it is in `_build`,
 * and all of it switches off with `?quality=low`.
 */

import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { Reflector } from 'three/addons/objects/Reflector.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { build as buildInstrument } from './instruments.js';
import { Environment, PLATFORM, WALL, roundedRect } from './environment.js';
import { hueFor, stepHue, SECTION_HUE } from './palette.js';

/* -- channel order: export.py ships `channels` and this must match it ----- */
const CH = { x: 0, y: 1, z: 2, roll: 3, pitch: 4, yaw: 5, body: 6, antR: 7, antL: 8 };
const NCH = 9;

/* -- twin proportions, metres (off the URDF's bounding boxes) -------------- */
const FOOT_R = 0.058, FOOT_H = 0.013;
const BODY_R_BOT = 0.054, BODY_R_TOP = 0.046, BODY_H = 0.098;
const HEAD_R = 0.048;
const BODY_TOP = FOOT_H + BODY_H;
const HEAD_Z = BODY_TOP + 0.060;
const ANT_LEN = 0.060, ANT_R = 0.0016;
const CONDUCTOR_SEAT_YAW = Math.PI;
/* Faces upstage at body yaw -90 and the audience at +90: `song.yaml` requires
 * the conductor installed a quarter turn round, and its program leans on it. */

const SEAT_GAP = 0.55;     // metres between neighbouring instruments
const RING_S = 0.6;        // seconds a strike halo lives
/* The camera looks *down* on the row from about sixteen degrees.
 *
 * It used to sit at 2.0 and aim at 1.35, which is a two-degree view: the
 * floor was edge-on, so a 2.5 m halo projected to eight pixels of smear and
 * could not be read however bright it was, and the instruments were seen as
 * flat cut-outs. From here the same halo projects to 0.7 m, the tops of the
 * bars and keys are in view -- which is where they are struck -- and the row
 * reads as objects standing on a floor. Still fixed: raised, not drifting. */
const CAM_Y = 5.2;
const CAM_LOOK_Y = 0.95;
const SEEK_BURST = 6;      // more anchors than this in one frame means a seek

export class Stage {
  constructor(canvas, data) {
    this.canvas = canvas;
    this.data = data;
    this.fps = data.fps;
    this.nFrames = Math.max(...data.robots.map((r) => r.frames.length));
    this.t = null;
    this.view = 'instruments';
    this.hue = SECTION_HUE.intro;
    this.accent = new THREE.Color();
    this.hitPtr = data.robots.map(() => 0);
    this._hits = [];
    this._last = performance.now();
    this.quality = new URLSearchParams(location.search).get('quality') || 'high';

    this.tracks = data.robots.map((r) => {
      const a = new Float32Array(r.frames.length * NCH);
      for (let i = 0; i < r.frames.length; i++) {
        const row = r.frames[i];
        for (let c = 0; c < NCH; c++) a[i * NCH + c] = row[c];
      }
      return { n: r.frames.length, a };
    });

    this._build();
    this._resize();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(canvas);
  }

  async ready() {}

  /* ------------------------------------------------------------ the scene */

  _build() {
    const high = this.quality !== 'low';
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: !high, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, high ? 1.5 : 1));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.shadowMap.enabled = high;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x020309);
    this.camera = new THREE.PerspectiveCamera(32, 1, 0.1, 4000);   // the planet is 900 m out

    /* something for the metal to reflect: a neutral studio, generated */
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.5;
    pmrem.dispose();

    /* lights: a warm key with the one shadow map, and two coloured rims that
     * draw the edges of the metal out of the dark */
    this.scene.add(new THREE.HemisphereLight(0x8fa6e6, 0x0e1c38, 0.22));   // sky, and a little earthshine from below
    const key = new THREE.DirectionalLight(0xfff1e0, 1.7);
    key.position.set(5, 12, 9);
    key.castShadow = high;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.left = -9; key.shadow.camera.right = 9;
    key.shadow.camera.top = 9; key.shadow.camera.bottom = -9;
    key.shadow.camera.near = 2; key.shadow.camera.far = 40;
    key.shadow.bias = -0.0006;
    key.shadow.normalBias = 0.02;
    this.scene.add(key);
    this.rimL = new THREE.SpotLight(0x40c8ff, 900, 0, 0.55, 0.7, 2);
    this.rimL.position.set(-11, 8, -6);
    this.rimR = new THREE.SpotLight(0xff4fd8, 900, 0, 0.55, 0.7, 2);
    this.rimR.position.set(11, 8, -6);
    for (const l of [this.rimL, this.rimR]) { l.target.position.set(0, 1, 0); this.scene.add(l, l.target); }

    this._floor(high);

    this.robots = this.data.robots.map((r, i) => this._seat(r, i));
    this._layout();
    for (const r of this.robots) {
      this.scene.add(r.seat);
      this.twinRoot.add(r.twinSeat);
    }

    this.env = new Environment(
      this.scene,
      this.robots.map((r) => ({
        id: r.id, x: r.seat.position.x, z: r.seat.position.z, width: r.inst.width,
        conductor: r.role === 'conductor',
      })),
      this.data.duration,
      this._nominalCamZ(),
    );

    /* Post: bloom is what makes an emissive surface a light.
     *
     * The composer needs its own multisampled buffers. Everything is drawn
     * into them rather than into the canvas, so the context's `antialias`
     * never applies -- without `samples` here, high quality had no
     * antialiasing at all and every edge in the scene was a staircase. */
    const size = new THREE.Vector2();
    this.renderer.getDrawingBufferSize(size);
    const target = new THREE.WebGLRenderTarget(size.x, size.y, {
      type: THREE.HalfFloatType, samples: high ? 4 : 0,
    });
    this.composer = new EffectComposer(this.renderer, target);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    if (high) {
      /* threshold high enough that only the things meant to be lights bloom:
       * below 0.8 the brushed metal caught it too and the stage went milky */
      this.bloom = new UnrealBloomPass(new THREE.Vector2(800, 450), 0.42, 0.5, 0.92);
      this.composer.addPass(this.bloom);
    }
    this.composer.addPass(new OutputPass());

    this._v = new THREE.Vector3();
    this._a = new THREE.Vector3();
    this.levels = new Map();
    this.mixBands = this.data.mix_bands && this.data.mix_bands.length ? this.data.mix_bands : null;
  }

  _floor(high) {
    /* The floor is a platform in orbit: a rounded slab the stage floats on,
     * with the planet visible past its edge. Its top is a mirror under a dark
     * glaze, with a shadow catcher on top of that. */
    /* The shape stays in its own XY plane and the *mesh* is laid flat: the
     * Reflector takes its mirror plane from the mesh's rotation, so a
     * geometry rotated flat under an unrotated mesh mirrors across a wall. */
    const top = () => new THREE.ShapeGeometry(roundedRect(PLATFORM.w, PLATFORM.d, PLATFORM.r), 24);
    const lay = (mesh, y) => {
      mesh.rotation.x = -Math.PI / 2;
      mesh.position.set(0, y, PLATFORM.z);
      this.scene.add(mesh);
      return mesh;
    };
    if (high) {
      this.reflector = lay(new Reflector(top(), {
        clipBias: 0.003, textureWidth: 1024, textureHeight: 1024, color: 0x343a48,
      }), 0);
    } else {
      lay(new THREE.Mesh(top(),
        new THREE.MeshStandardMaterial({ color: 0x0b0e16, roughness: 0.3, metalness: 0.6 })), 0);
    }
    /* lit, so the coloured spots pool on the stage; near-black, so it adds
     * almost nothing of its own and the mirror shows through */
    lay(new THREE.Mesh(top(),
      new THREE.MeshStandardMaterial({ color: 0x04060d, transparent: true, opacity: 0.62, roughness: 1 })), 0.002);
    const shadows = lay(new THREE.Mesh(top(), new THREE.ShadowMaterial({ opacity: 0.55 })), 0.004);
    shadows.receiveShadow = true;

    this.twinRoot = new THREE.Group();
    this.twinRoot.rotation.x = -Math.PI / 2;     // the twin is built z-up
    this.twinRoot.visible = false;
    this.scene.add(this.twinRoot);
  }

  _seat(entry, index) {
    /* the instrument, y-up, in the scene */
    const seat = new THREE.Group();
    const inst = buildInstrument(entry);
    seat.add(inst.root);

    /* The strike ring, on the floor under the instrument.
     *
     * A thin annulus of flat geometry was the wrong shape for this camera:
     * two metres up and seventeen back is a six-degree view of the floor, at
     * which a 20 cm band projects to a hairline and the instrument's own
     * frame covers what is left. It fired correctly and could not be seen.
     * So it is a wide soft halo on a textured quad instead -- the same read
     * as the pools of light already on the floor, which are visible at this
     * angle because they are broad. */
    const ringMat = new THREE.MeshBasicMaterial({
      map: Stage._ringTexture(), color: 0xffffff, transparent: true, opacity: 0,
      depthWrite: false, blending: THREE.AdditiveBlending,
    });
    const ring = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), ringMat);
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.02;          // clear of the cone discs at 0.012
    seat.add(ring);

    /* The wall of the ripple.
     *
     * Anything lying flat on this floor is seen at six degrees and projects
     * to a smear a couple of pixels deep -- measured, the flat halo did light
     * the floor (54 against 31 for an unstruck seat) and still could not be
     * read as a ring. A third of a metre of height, by contrast, projects to
     * about thirty pixels. So the ripple has a side: an open cylinder that
     * grows with the halo and fades with it, which is what actually carries
     * the strike to the eye from out front. */
    const wallMat = new THREE.MeshBasicMaterial({
      map: Stage._wallTexture(), color: 0xffffff, transparent: true, opacity: 0,
      depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
    });
    const wallGeo = new THREE.CylinderGeometry(0.5, 0.5, 1, 48, 1, true);
    wallGeo.translate(0, 0.5, 0);           // stands on the floor
    const wall = new THREE.Mesh(wallGeo, wallMat);
    seat.add(wall);

    /* the twin, z-up, in its own compact arc */
    const twinSeat = new THREE.Group();
    const pose = this._twinPose(entry, index, this.data.robots.length);
    twinSeat.position.set(pose.x, pose.y, 0);
    twinSeat.rotation.z = pose.yaw;
    const parts = buildRobot(entry.role === 'conductor');
    twinSeat.add(parts.root);

    return {
      id: entry.id,
      instrument: entry.instrument || entry.role,
      role: entry.role,
      /* What the view walks for strikes: the build's anchors, plus the
       * onsets recovered from the stem for the introduction, which the
       * anchors do not cover at all. */
      anchors: [...(entry.onsets || []), ...entry.anchors],
      envelope: entry.envelope || null,
      bandsAll: entry.bands || null,
      seat, inst, ring, wall, twinSeat,
      ...parts,
      hit: -1e9, hitReal: -1e9, hitStrength: 0, level: 0,
    };
  }

  /** Left to right in the robots' order, each as wide as it really is, on a
   *  shallow arc; the conductor on its podium downstage, centre. */
  _layout() {
    const performers = this.robots.filter((r) => r.role !== 'conductor');
    const total = performers.reduce((s, r) => s + r.inst.width, 0) + SEAT_GAP * (performers.length - 1);
    let x = -total / 2;
    performers.forEach((r, i) => {
      const u = performers.length > 1 ? i / (performers.length - 1) - 0.5 : 0;
      const cx = x + r.inst.width / 2;
      r.seat.position.set(cx, 0, -0.6 + Math.abs(u) * 1.6);
      r.seat.rotation.y = -u * 0.22;         // the ends turn in toward centre
      x += r.inst.width + SEAT_GAP;
    });
    for (const r of this.robots) if (r.role === 'conductor') r.seat.position.set(0, 0, 2.4);
    this.stageWidth = total;
    /* One height for every placard, clear of the tallest instrument: a row of
     * readouts hanging in the sky reads as a row; nine different heights read
     * as clutter. */
    this.labelBand = Math.max(...performers.map((r) => r.inst.topHeight)) + 1.45;
    /* the fan is far enough back that the placards clear it in projection */
  }

  _twinPose(entry, index, total) {
    if (entry.role === 'conductor') return { x: 0, y: -0.28, yaw: CONDUCTOR_SEAT_YAW };
    const performers = total - 1;
    const k = index > 0 ? index - 1 : 0;
    const u = performers > 1 ? k / (performers - 1) - 0.5 : 0;
    const a = u * 2.05;
    return { x: Math.sin(a) * 0.52 * 1.66, y: 0.08 + Math.cos(a) * 0.52 * 0.30, yaw: -Math.PI / 2 - u * 0.32 };
  }

  toggleView() {
    this.view = this.view === 'twin' ? 'instruments' : 'twin';
    const twin = this.view === 'twin';
    this.twinRoot.visible = twin;
    for (const r of this.robots) r.seat.visible = !twin;
    /* nine near-white shells a hand's width from the lens, under a stage's
     * worth of light, bloom into blobs: expose the twin on its own terms */
    this.renderer.toneMappingExposure = twin ? 0.55 : 1.0;
    if (this.bloom) this.bloom.strength = twin ? 0.12 : 0.42;
    return this.view;
  }

  _beat(t) {
    const m = this.data.music;
    if (t === null || !m || !m.bpm) return null;
    const beat = (t - m.t0_s) * m.bpm / 60;
    const bpb = m.meter ? m.meter[0] * 4 / m.meter[1] : 4;
    return { beat, inBar: ((beat % bpb) + bpb) % bpb, bpb, total: m.beats };
  }

  /* ----------------------------------------------------------- the frame */

  update(t) {
    const now = performance.now();
    const dt = Math.min(0.1, (now - this._last) / 1000);
    this._last = now;
    this.t = t;
    const show = t !== null && t >= 0;
    const twin = this.view === 'twin';
    const beat = show ? this._beat(t) : null;
    const fi = show ? Math.min(this.nFrames - 1, Math.round(t * this.fps)) : -1;

    this.hue = stepHue(this.hue, hueFor(t, this.data.sections), dt);
    this.accent.setHSL(this.hue / 360, 0.9, 0.62);

    for (let i = 0; i < this.robots.length; i++) {
      const r = this.robots[i];
      r.level = (fi >= 0 && r.envelope) ? (r.envelope[fi] || 0) : 0;
      this.levels.set(r.id, r.level);
      /* the per-stem spectrum is for the instruments; the backdrop takes
       * the mix instead, as one waveform */
      if (twin) {
        const tr = this.tracks[i];
        const f = show ? sample(tr, t * this.fps) : sample(tr, 0);
        r.body.rotation.z = f[CH.body];
        r.head.position.set(f[CH.x], f[CH.y], HEAD_Z + f[CH.z]);
        r.head.rotation.set(f[CH.roll], f[CH.pitch], f[CH.yaw]);
        r.antR.rotation.x = f[CH.antR];
        r.antL.rotation.x = f[CH.antL];
      } else {
        r.inst.setAccent(this.accent);
        r.inst.update(show ? t : null, r.level, beat);
      }
      this._paintHit(r);
    }

    const mix = (show && this.mixBands && fi >= 0) ? this.mixBands[fi] : null;
    this.env.update(t, this.hue, now / 1000, this.levels, mix, dt);
    this._camera(t, now / 1000);
    this.composer.render();
  }

  /** The halo under one seat.
   *
   * Aged in **real** seconds from the moment the strike was consumed, not in
   * program-time distance from its anchor. Those are the same thing only
   * while frames are frequent: at a few frames a second -- a weak GPU, a
   * software renderer, a backgrounded tab -- a hit is registered at the end
   * of one frame and the next frame is already past the window, so the halo
   * is computed, found expired, and never drawn once. Measured from the
   * firing instant it always gets its full time on screen.
   */
  _paintHit(r) {
    const age = (performance.now() - r.hitReal) / 1000;
    if (age < 0 || age > RING_S) {
      if (r.ring.material.opacity !== 0) {
        r.ring.material.opacity = 0; r.ring.scale.setScalar(1);
        r.wall.material.opacity = 0;
      }
      return;
    }
    const u = age / RING_S;
    r.ring.material.color.copy(this.accent);
    r.wall.material.color.copy(this.accent);
    /* bright at once, then out; the quad is sized in metres across, so the
     * halo starts about as wide as the instrument and grows past it */
    r.ring.material.opacity = Math.min(0.9, (1 - u) * (1 - u) * 0.95 * Math.min(1.4, r.hitStrength));
    /* grows by about half again: far enough to read as a spreading ring,
     * not so far that a marimba's halo reaches the woodblock */
    const w = (1.1 + r.inst.width * 0.8) * (0.62 + u * 0.8);
    r.ring.scale.set(w, w, 1);
    /* the wall rides the same radius, shrinking in height as it spreads */
    r.wall.scale.set(w, 0.42 * (1 - u * 0.65), w);
    r.wall.material.opacity = (1 - u) * (1 - u) * 0.8 * Math.min(1.4, r.hitStrength);
  }

  /** The ripple's side: bright at the floor, gone at the top. */
  static _wallTexture() {
    if (Stage.__wall) return Stage.__wall;
    const n = 64, c = document.createElement('canvas');
    c.width = 4; c.height = n;
    const g = c.getContext('2d');
    const img = g.createImageData(4, n);
    for (let y = 0; y < n; y++) {
      /* row 0 is the top of a cylinder's UV, so fade the other way */
      const up = 1 - y / (n - 1);
      const a = Math.round(255 * Math.pow(1 - up, 1.6));
      for (let x = 0; x < 4; x++) {
        const o = (y * 4 + x) * 4;
        img.data[o] = img.data[o + 1] = img.data[o + 2] = 255;
        img.data[o + 3] = a;
      }
    }
    g.putImageData(img, 0, 0);
    Stage.__wall = new THREE.CanvasTexture(c);
    return Stage.__wall;
  }

  /** A soft annulus: nothing in the middle, brightest at 55% of the radius,
   *  gone by the edge. Shared by all nine. */
  static _ringTexture() {
    if (Stage.__ring) return Stage.__ring;
    const n = 256, c = document.createElement('canvas');
    c.width = c.height = n;
    const g = c.getContext('2d');
    const img = g.createImageData(n, n);
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        const dx = (x / (n - 1)) * 2 - 1, dy = (y / (n - 1)) * 2 - 1;
        const d = Math.sqrt(dx * dx + dy * dy);
        /* a band around d = 0.55, falling off both ways, cut off at the edge */
        let v = Math.exp(-((d - 0.5) ** 2) / 0.075) * Math.max(0, 1 - d);
        const o = (y * n + x) * 4;
        img.data[o] = img.data[o + 1] = img.data[o + 2] = 255;
        img.data[o + 3] = Math.round(255 * Math.min(1, v));
      }
    }
    g.putImageData(img, 0, 0);
    Stage.__ring = new THREE.CanvasTexture(c);
    return Stage.__ring;
  }

  /** Where the camera stands: far enough back that the whole row fits.
   *
   * Solved as a *range*, not a depth, because the camera looks down from
   * CAM_Y. Taking the fitting distance as z and then raising the camera
   * pushes the real range out and shrinks the row; this takes the range the
   * fit needs and lays it back down onto the floor. */
  _nominalCamZ() {
    /* fit the whole row at this aspect ratio, with a seat's worth of margin */
    const half = this.stageWidth / 2 + 1.2;
    const fov = this.camera.fov * Math.PI / 180;
    /* from the canvas, not the camera: this runs inside `_build`, before
     * `_resize` has given the camera its real aspect (it is 1 until then) */
    const aspect = (this.canvas.clientWidth || window.innerWidth) /
                   Math.max(1, this.canvas.clientHeight || window.innerHeight);
    const range = (half / (Math.tan(fov / 2) * aspect)) * 1.12;
    const drop = CAM_Y - CAM_LOOK_Y;
    return Math.sqrt(Math.max(4, range * range - drop * drop)) + 2.0;
  }

  /** Fixed. No sway, no push-in on the choruses: a screen beside nine
   *  robots is a window onto them, and a window does not drift. */
  _camera(t, now) {
    if (this.view === 'twin') {
      this.camera.position.set(0, 0.27, 1.5);
      this.camera.lookAt(0, 0.07, -0.02);
      return;
    }
    this.camera.position.set(0, CAM_Y, this._nominalCamZ());
    this.camera.lookAt(0, CAM_LOOK_Y, -0.3);
  }

  _sectionName(t) {
    if (t === null) return null;
    for (const s of this.data.sections) if (t >= s.start && t < s.end) return s.name;
    return null;
  }

  /* ------------------------------------------------------------- strikes */

  consumeHits(t) {
    this._hits.length = 0;
    for (let i = 0; i < this.robots.length; i++) {
      const r = this.robots[i];
      const anchors = r.anchors;
      let p = this.hitPtr[i];
      /* How many are due. A handful is a slow frame -- fire them all, a weak
       * GPU should not lose notes. A flood is a seek: the clock was corrected
       * or the view rewound, and replaying the whole piece so far in one
       * frame would set every seat alight at once. Then only the last counts.
       * Keyed on the size of the burst, not on how old the anchors are,
       * because at a few frames a second an honest strike is already old. */
      let due = p;
      while (due < anchors.length && anchors[due] <= t) due++;
      const n = due - p;
      if (n > SEEK_BURST) p = due - 1;
      while (p < due) {
        const strength = this._strength(i, anchors[p]);
        r.hit = anchors[p];
        r.hitReal = performance.now();
        r.hitStrength = strength;
        this._hits.push({ id: r.id, strength, at: anchors[p] });
        p++;
      }
      this.hitPtr[i] = p;
    }
    return this._hits;
  }

  /** How hard it looked, from how fast the head was moving at the anchor: the
   *  gesture amplitude is scaled by note velocity in the build, so the
   *  derivative at the strike is the dynamic, recovered rather than invented. */
  _strength(i, at) {
    const tr = this.tracks[i];
    const idx = Math.min(tr.n - 1, Math.max(1, Math.round(at * this.fps)));
    const o = idx * NCH, p = (idx - 1) * NCH;
    const v = (Math.abs(tr.a[o + CH.pitch] - tr.a[p + CH.pitch]) * 9
      + Math.abs(tr.a[o + CH.z] - tr.a[p + CH.z]) * 120
      + Math.abs(tr.a[o + CH.antR] - tr.a[p + CH.antR]) * 6) * this.fps;
    return Math.max(0.35, Math.min(1.6, 0.35 + v * 0.55));
  }

  rewind() {
    this.hitPtr.fill(0);
    for (const r of this.robots) {
      r.inst.rewind(); r.hit = -1e9; r.hitReal = -1e9;
      r.ring.material.opacity = 0; r.wall.material.opacity = 0;
    }
  }

  /* -------------------------------------------------------------- labels */

  labelPositions() {
    const out = [];
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    const twin = this.view === 'twin';
    for (const r of this.robots) {
      if (twin) {
        r.head.getWorldPosition(this._v);
        this._v.y += r.role === 'conductor' ? -0.165 : 0.115;
        this._v.project(this.camera);
        out.push({
          id: r.id, instrument: r.instrument, level: r.level || 0, note: null,
          x: (this._v.x * 0.5 + 0.5) * w, y: (-this._v.y * 0.5 + 0.5) * h,
          anchorX: null, anchorY: null, below: false, visible: this._v.z < 1,
        });
        continue;
      }
      /* two points: where the placard hangs, and the top of the instrument it
       * belongs to, so the page can draw a leader between them */
      const below = !!r.inst.labelBelow;
      r.seat.getWorldPosition(this._a);
      this._a.y += r.inst.topHeight;
      this._a.project(this.camera);
      r.seat.getWorldPosition(this._v);
      this._v.y += below ? -0.45 : this.labelBand;
      this._v.project(this.camera);
      out.push({
        id: r.id,
        instrument: r.instrument,
        level: r.level || 0,
        note: r.inst.sounding(this.t),
        x: (this._v.x * 0.5 + 0.5) * w,
        y: (-this._v.y * 0.5 + 0.5) * h,
        anchorX: (this._a.x * 0.5 + 0.5) * w,
        anchorY: (-this._a.y * 0.5 + 0.5) * h,
        below,
        visible: this._v.z < 1,
      });
    }
    return out;
  }

  /* -------------------------------------------------------------- resize */

  _resize() {
    const w = this.canvas.clientWidth || window.innerWidth;
    const h = this.canvas.clientHeight || window.innerHeight;
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    if (this.bloom) this.bloom.setSize(w / 2, h / 2);
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }
}

/* -------------------------------------------------------------------------- */
/* The twin: one robot, from primitives. Forward is +x, up is +z.             */
/* -------------------------------------------------------------------------- */

const COL = { shell: 0xf2ece1, shellDark: 0xd8d0c2, visor: 0x14161f, trim: 0x2a3344, foot: 0x1b2130, glow: 0xffd79a };

function buildRobot(isConductor) {
  const root = new THREE.Group();
  const shell = new THREE.MeshStandardMaterial({ color: COL.shell, roughness: 0.62, metalness: 0.04 });
  const shellDark = new THREE.MeshStandardMaterial({ color: COL.shellDark, roughness: 0.7, metalness: 0.03 });
  const dark = new THREE.MeshStandardMaterial({ color: COL.visor, roughness: 0.34, metalness: 0.22 });
  const trim = new THREE.MeshStandardMaterial({ color: isConductor ? 0x5b4a8a : COL.trim, roughness: 0.5, metalness: 0.2 });
  const glowMat = new THREE.MeshStandardMaterial({ color: COL.shell, emissive: COL.glow, emissiveIntensity: 0, roughness: 0.4, metalness: 0.1 });

  const foot = cyl(FOOT_R, FOOT_R * 0.96, FOOT_H, new THREE.MeshStandardMaterial({ color: COL.foot, roughness: 0.8, metalness: 0.1 }));
  foot.position.z = FOOT_H / 2;
  root.add(foot);

  const body = new THREE.Group();
  body.position.z = FOOT_H;
  root.add(body);
  const barrel = cyl(BODY_R_TOP, BODY_R_BOT, BODY_H, shell);
  barrel.position.z = BODY_H / 2;
  body.add(barrel);
  const collar = cyl(BODY_R_TOP * 0.97, BODY_R_TOP * 1.02, 0.007, trim);
  collar.position.z = BODY_H - 0.0035;
  body.add(collar);
  const badgeZ = BODY_H * 0.42;
  const badgeR = BODY_R_BOT + (BODY_R_TOP - BODY_R_BOT) * (badgeZ / BODY_H);
  const badge = new THREE.Mesh(new THREE.CircleGeometry(0.0062, 20), trim);
  badge.position.set(badgeR + 0.0007, 0, badgeZ);
  badge.rotation.y = Math.PI / 2;
  body.add(badge);
  const neck = cyl(0.016, 0.019, HEAD_Z - BODY_H - FOOT_H + 0.012, new THREE.MeshStandardMaterial({ color: 0x3a4154, roughness: 0.45, metalness: 0.35, transparent: true, opacity: 0.85 }));
  neck.position.z = BODY_H + (HEAD_Z - BODY_H - FOOT_H) / 2;
  body.add(neck);

  /* the head hangs off the base, not the body: the pose the build ships is
   * already absolute, and `body_yaw` is a separate joint */
  const head = new THREE.Group();
  head.rotation.order = 'ZYX';     // scipy extrinsic "xyz" = Rz Ry Rx
  root.add(head);
  const skull = new THREE.Mesh(new THREE.SphereGeometry(HEAD_R, 44, 30), shell);
  head.add(skull);
  const visor = new THREE.Mesh(new THREE.SphereGeometry(HEAD_R * 1.004, 44, 26, 0, Math.PI * 2, 0, 1.02), dark);
  visor.rotation.z = -Math.PI / 2;
  head.add(visor);
  const lens = cyl(0.0072, 0.0072, 0.005, new THREE.MeshStandardMaterial({ color: 0x0a1626, roughness: 0.1, metalness: 0.65 }));
  lens.rotation.y = Math.PI / 2;
  lens.position.set(HEAD_R * 1.005, 0, 0.004);
  head.add(lens);
  const seam = new THREE.Mesh(new THREE.TorusGeometry(HEAD_R * Math.sin(1.02), 0.0012, 8, 56), shellDark);
  seam.rotation.y = Math.PI / 2;
  seam.position.x = HEAD_R * Math.cos(1.02);
  head.add(seam);
  const antR = antenna(-1, trim, glowMat);
  const antL = antenna(+1, trim, glowMat);
  head.add(antR, antL);
  if (isConductor) {
    const baton = cyl(0.0012, 0.0018, 0.075, shellDark);
    baton.position.set(HEAD_R * 0.5, -0.03, -HEAD_R * 0.5);
    baton.rotation.set(0.5, 0, -0.5);
    head.add(baton);
  }
  return { root, body, head, antR, antL, glowMat };
}

function antenna(side, trim, glowMat) {
  const g = new THREE.Group();
  g.position.set(-HEAD_R * 0.18, side * HEAD_R * 0.46, HEAD_R * 0.80);
  const arm = new THREE.Group();
  arm.rotation.y = -0.22;
  g.add(arm);
  const rod = cyl(ANT_R, ANT_R * 1.5, ANT_LEN, trim);
  rod.position.set(0, side * ANT_LEN * 0.18, ANT_LEN * 0.46);
  rod.rotation.x = -side * 0.36;
  arm.add(rod);
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.0052, 16, 12), glowMat);
  tip.position.set(0, side * ANT_LEN * 0.36, ANT_LEN * 0.9);
  arm.add(tip);
  return g;
}

function cyl(rTop, rBot, h, mat) {
  const g = new THREE.CylinderGeometry(rTop, rBot, h, 32);
  g.rotateX(Math.PI / 2);
  return new THREE.Mesh(g, mat);
}

const _f = new Float64Array(NCH);

function sample(track, fi) {
  const n = track.n;
  if (fi <= 0) { for (let c = 0; c < NCH; c++) _f[c] = track.a[c]; return _f; }
  if (fi >= n - 1) {
    const o = (n - 1) * NCH;
    for (let c = 0; c < NCH; c++) _f[c] = track.a[o + c];
    return _f;
  }
  const i = fi | 0, u = fi - i, o = i * NCH, p = o + NCH;
  for (let c = 0; c < NCH; c++) { const a = track.a[o + c]; _f[c] = a + (track.a[p + c] - a) * u; }
  return _f;
}
