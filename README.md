# Reachy Mini Orchestra

A synchronized robot orchestra: **1 conductor + 8 instrumentalists**, driven from
one Windows PC over an isolated Wi-Fi network, on a shared musical timeline. The
show runs 144 seconds: the conductor introduces each player, the ensemble plays
*It's a Small World*, and all nine bow and put their faces away.

**No physical instruments.** Every sound comes out of the robots' own speakers;
the motion is air-instrument acting. That inversion matters more than it sounds:
when a beater hit a real chime bar, motion sync *was* musical sync. Now audio
alignment is the music and motion only has to look right. Audio timing is
therefore the first-class concern, not a follow-on.

Everything here is built against the *actual* Reachy Mini SDK and the *actual*
behaviour of these nine robots, both of which were inspected before a line was
written. Where the code makes a claim about the hardware, the source file and
line are cited.

---

## The one thing to understand first

**The Reachy Mini stack enforces no motion limits at all.** There is no velocity
limit, no acceleration limit and no workspace clamp anywhere in the Python stack;
the URDF's `velocity="8"` is metadata only. The single failure mode is IK
returning NaN, and the daemon catches that exception, throttle-logs it and
carries on:

```python
# daemon/backend/robot/backend.py:240-243
except ValueError as e:
    log_throttling.by_time(self.logger, interval=0.5).warning(f"IK error: {e}")
```

So an unreachable or too-fast pose produces a **silent freeze at the last valid
target** — not an error you can detect from the client. On top of that, this
firmware exposes **no motor temperature, current or load** anywhere in its API
(`GET /api/motors/status` returns exactly `{"mode": "..."}`), so there is no
runtime thermal watchdog to fall back on either.

Every safety guarantee in this project therefore lives in
[`orchestra/motion/validate.py`](orchestra/motion/validate.py), which runs on the
PC, at the song's real tempo, before anything reaches a robot. Nothing is
deployed unvalidated — `song build` refuses to write a failing build.

---

## Why an on-robot agent

The stock daemon has **no scheduled-execution primitive**. There is no "start at
absolute time T" in the protocol, the daemon, or the Rust motor layer; every
`timestamp` field on the wire is written by the client and never read by the
server. That leaves three options, and only one of them holds a beat across nine
robots:

| Approach | Skew | Status |
|---|---|---|
| PC streams targets at 50 Hz | 10 ms – 500 ms+ | The sibling `ethics-podcast` project measured ping going from ~15 ms to **500–1400 ms with timeouts** while streaming to just *three* robots. Nine is 450 msg/s on one channel. Rejected. |
| `PlayUploadedMoveCmd` fan-out | **18 ms measured** | Works, no SSH needed (`orchestra show play`). Kept as the fallback. At the edge of audible for percussion. |
| **On-robot agent + clock sync** | **0.999 ms measured** | ✅ The production path (`orchestra show run`). |

Both lower rows are **measured on this fleet**, not estimated — 9 robots,
full 24.3 s performance. See [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md).

So the whole performance is preloaded, the PC and each robot agree on a shared
timebase up front, and START carries an absolute instant a few seconds in the
future. Each robot then runs its own 100 Hz loop against its own monotonic clock.
Cross-robot skew collapses to clock-sync error, and Wi-Fi traffic during the show
drops to a 1 Hz heartbeat.

It also handles audio locally: each robot plays its own stem from local disk
through its own speaker, on the same clock as its own motion, never touching the
WebRTC signalling that nine identically named `reachy-mini` hosts would corrupt.

⚠ **`connection_mode="localhost_only"` is not sufficient on its own.** The SDK
picks the local backend only when localhost_only **and**
`is_local_camera_available()` ([`reachy_mini.py:338`]); otherwise it silently
falls back to WebRTC, which fails with `Connection refused`. That took out two of
nine robots on the first live run. The agent therefore requests backends
explicitly — `local` → `gstreamer_no_video` → `no_media` — and degrades to silent
motion rather than failing.

```
                    Windows PC
   song.mid + song.yaml ──► music/timeline ──┐
   clips/*.clip.json ─────► motion/compile ──┤
                                             ▼
                                  motion/validate  ◄── SAFETY GATE
                                             ▼
                                    show/build ──► per-robot program + stem
                                             │
                    ══ DEPLOY (before the show) ══
                                             ▼
   ┌─────────────────────────────────────────────────────────┐
   │ each robot: agent/orchestra_agent.py                    │
   │   • localhost WebSocket (sub-ms, no Wi-Fi in the loop)  │
   │   • clock sync with the PC (Cristian's algorithm)       │
   │   • sleep_until(T0 − offset), then a local 100 Hz loop  │
   │   • local WAV playback at T0 + audio_lead_ms            │
   └─────────────────────────────────────────────────────────┘
                      ▲  ~1 msg/s control channel
                      │  ARM / SYNC / START(T0) / ABORT / heartbeat
```

---

## Quick start

```bash
# Windows
python -m venv .venv && .venv\Scripts\activate

# Linux (the RTX3090 box). 3.11 explicitly -- the system `python3` is 3.8.
# `reachy-mini` pulls in PyGObject on Linux (it takes gstreamer-bundle on
# Windows instead), and PyGObject has no wheel: without these headers the
# install dies building pycairo.
sudo apt-get install -y libcairo2-dev libgirepository1.0-dev libglib2.0-dev
python3.11 -m venv .venv && source .venv/bin/activate

pip install -e ".[plot,dev,voice]"

orchestra fleet status                 # is everyone alive?
orchestra fleet discover --write       # reconcile IPs by hardware_id
orchestra clip list                    # what motions exist
orchestra clip check --bpm 112         # run the safety gate on the library

python tools/extract_samples.py        # pull instrument samples out of gm.dls
python tools/make_stems.py             # render per-robot audio
python tools/preview_song.py           # mix it down and listen before deploying
orchestra song build small_world       # compile + validate 9 programs

orchestra fleet provision              # install the agent on every robot
orchestra fleet agent start
orchestra show deploy small_world
orchestra show run small_world         # arm, sync, start, monitor, park
orchestra web small_world              # ...or drive it from a browser
```

Two commands worth remembering under pressure:

```bash
orchestra show stop      # graceful: agents ramp to neutral, then cut torque
orchestra show estop     # HARD: disable motors over HTTP, bypassing the agents
```

## While it is playing

A run is 148 seconds during which nothing else can be typed, so the useful
controls are keys:

| key | |
|---|---|
| ↑ / ↓ | volume, ±3 across all nine |
| → / ← | volume, ±1 — for settling on a level |
| `s` | finish the current take, then stop (`--loop` only) |
| ctrl-C | abort now: agents ramp to neutral, then park |

**Arrow keys need a real console.** Whether they arrive is up to the terminal,
not to us, and finding out 148 seconds into a performance is the wrong moment:

```bash
orchestra show keys      # ten seconds, no robots involved
```

A standalone PowerShell or Windows Terminal window gives a real console. Piped
or redirected stdin gives line input only — `s` still works with Enter, arrow
keys cannot, and the banner says so at startup rather than failing silently.

Volume is not sent through the daemon's `/api/volume/set`: **that route plays
`impatient1.wav` on every call**, which mid-show is one robot making a foreign
noise. It goes through the agent instead, over the SDK session that is already
open, which reaches the same mixer and makes no sound. Keypresses are coalesced
to at most ten fan-outs a second — a held arrow repeats about thirty times a
second, and nine robots times thirty is RPC traffic that would start delaying
the status polling that decides whether a robot has dropped out.

The level is saved to `config/calibration/<robot>.yaml` on exit, but only if you
actually moved it, and restored on the next run.

`estop` deliberately does not go through the agents, so it still works when one
is hung.

---

## The browser console

`orchestra show run` needs someone at a terminal for 148 seconds. An exhibition
needs a button anyone can press. Same lifecycle, different front end:

```bash
orchestra web small_world              # http://localhost:8080
orchestra web small_world --rehearse   # the screen, with no robots at all
```

| control | what it does |
|---|---|
| ▶ / ■ | one take: arm → sync → START → monitor. Stop fades the music and lowers the head over `agent.fade_out_s`, through the agents |
| ⟳ | repeat, with the same 5 s gap `show run --loop` uses |
| slider | **the robots' volume**, through the same `VolumeController` |
| E-STOP | `hard_stop()` — motors off over HTTP, agents bypassed |

Space, `L`, `T` (instruments / robot twin), arrow keys (±3 and ±1, matching
the terminal) and `F` all work. **The camera is fixed** — no drift, no push-in
on the choruses: a screen standing beside nine robots is a window onto them,
and a window does not move. **The page is in English**: the robot ids and
instrument names on it are the config's own strings, and a test keeps any
other language out of the three static files.

**The sound is the robots' own speakers, and the browser is not in the audio
path.** Nine stems, each played by the robot playing that part, on that robot's
own clock — the same arrangement [the rest of the system](#how-the-sound-works)
is built on. So the slider says `9 ROBOTS` on it, and moves those nine
mixers through the agent rather than the daemon's `/api/volume/set`. The page
itself is silent: the only `<audio>` element on it has no `src` until rehearsal
mode attaches one, and `/api/audio/preview.wav` returns **403 outside
`--rehearse`**. Putting the PC mix in the room during a performance would add a
tenth voice a sound card's latency away from nine that are aligned to 0.999 ms.
The console can therefore run on a muted machine, or a machine with no sound
card, and nothing about the show changes.

**There is no pause, and that is the hardware's shape rather than a
simplification of it.** The agent plays its program against its own monotonic
clock and the only ways out of that loop are *finish* and *abort to neutral*;
there is no suspend. A pause button would have to be a freeze at the last
target — the exact condition that stalls a servo against its own travel — or a
silent restart. So **stop returns to neutral and play always starts from the
top.**

Stop answers in under 50 ms. `show run` asks for a stop at the end of the
current take, which is right for a performance someone chose to run and wrong
for a button on a wall, so `Show.monitor` takes a `should_stop` callback and
slices its waits finely while still polling the robots at the measured-safe
2 Hz.

### What is on the screen

**The instruments.** The robots have none — the sound is nine speakers and
the motion is acting — so the one thing an audience cannot see is which seat
is the marimba, and the real robots are standing next to the screen already.
The screen shows nine instruments in the robots' seating, each with a placard
(`MARIMBA` over `R05`, both straight from `robots.yaml` so the screen cannot
drift from the config), and plays them from the same data the robots play:

| | from | shows |
|---|---|---|
| strikes | the build's `anchors`, plus onsets recovered from the stem for the introduction, which the anchors do not cover at all | the mallet rising a quarter second early and landing on the instant |
| pitch | the score's note events, which agree with the anchors to 0.0 ms | *which bar* the mallet lands on, which key sinks |
| level | that robot's **own stem**, RMS at 50 Hz | the vibraphone ringing after the mallet has gone, the shaker's roll, the glow under each placard |

**At their real size.** A five-octave marimba is 2.6 m long with resonators
that nearly reach the floor; a glockenspiel is a 70 cm case on a stand; a
shaker is something you hold. The pitched instruments are built across their
full standard range (the marimba C2–C7, 61 bars) and the played bars light
within it, because a marimba with twenty bars is a toy. Bars are laid out like
a keyboard (naturals in front, accidentals raised and set back) so a scale runs
left to right; the rest get the thing itself — a block and a stick on a chrome
tripod, a hoop with sixteen pairs of jingles, a tube shaker in a cradle, four
handbells turned from a bell's profile, a podium with a lit edge, a stand with
a tablet score and a baton that leaves a trail of light. Bevelled geometry,
wood grain, clearcoat lacquer, brushed aluminium. `instruments.js` has one
`build()` entry point per kind, so any of them can be swapped for a modelled
mesh.

**The look** is the three things a model needs that a toy does not: an
environment for the metal to reflect (a generated studio, via PMREM), a floor
that reflects the instruments (a `Reflector` under a dark glaze and a shadow
catcher), and a bloom pass so that what glows, glows (`UnrealBloomPass`). Over
each seat, a cone of light that brightens with that robot's own stem. The
three.js addons for all of this are vendored under `static/vendor/addons/` and
resolved by an import map, so the page still needs no network.
`?quality=low` turns the reflector, shadows and bloom off for a weak GPU.
Everything under `/static/` is served `Cache-Control: no-cache`: aiohttp's
static handler sends only `Last-Modified`, and a browser given only that
guesses a freshness of a tenth of the file's age — so an edited scene file can
go on being served from cache for minutes, with nothing anywhere to say why
the screen stopped reacting.

**The robot twin** (`T` toggles it) is the second view: nine Reachy Minis
playing **the frames the nine robots are playing** —
`build/<song>/<id>.program.json.gz` resampled to 50 Hz and nothing else. No
easing, no beat detection, no second choreography engine that could drift from
the first. It is for rehearsing a build without robots and for checking motion
— the one placement in it that is not a guess is the conductor's: `song.yaml`
requires it installed rotated 90°, so it plays at body yaw −90° facing the
ensemble and turns 180° to +90° to bow at the audience. Get that wrong and the
screen shows it bowing to the back wall.

**The introduction had nothing to show.** Not one anchor falls inside the
33-second naming sequence — `song build` writes them for the music only — so
the one stretch whose whole job is to say which robot plays what was silent on
screen. `export.py` now recovers its strikes from each stem's own level
(`onsets`), stopping short of the first anchor so the first note is not struck
twice; 28 of them fire during the introduction, in the build's own naming
order. During the music the anchors are still the only source, because a
vibraphone still ringing would produce onsets the robot is not playing.

Those onsets go into **the instruments too**, not just the halos — merged into
each instrument's own strike list at construction, with a null pitch padded in
to keep the list index-aligned with the note list. Without that the mallets,
keys and sticks stood still through the entire naming sequence while the
lights around them moved: the screen said a robot was playing and showed it
not playing.

**The strike halo had to change shape for this camera.** It began as a thin
ring of flat geometry on the floor, and it fired correctly — measured peak
opacity 0.75–0.80 on every robot through the introduction — but two metres up
and seventeen back is a six-degree view of the floor, at which a 20 cm band
projects to a hairline, and the instrument's own frame covers what is left of
it. It is a broad soft halo on a textured quad now, starting about as wide as
the instrument and growing half again over 0.6 s: the same read as the pools
of light already on the floor, which are visible at that angle precisely
because they are broad.

**And then it still did not appear, for two reasons that only bite when
frames are slow.** Measuring the live frame loop rather than a synthetic one
showed `update()` running about 1.2 times a second under software rendering,
with every halo painting 0.000:

* The halo was aged in *program* time from its anchor, and drawn *after* the
  strike was consumed. At 60 fps that is a 16 ms delay; at 1 fps the hit is
  registered at the end of one frame and the next frame is already past the
  0.6 s window, so the halo is computed, found expired, and never drawn once.
  It is aged in **real** seconds from the firing instant now, and strikes are
  consumed *before* the frame that draws them.
* `syncClock` hard-reset the page clock whenever the derived T0 moved by more
  than half a second — which sounds like a clock jump and is not. A snapshot
  is handled on the main thread, so a frame that takes most of a second makes
  every message that late, and the lateness lands in the estimate as a leap.
  Six spurious resets in one take, each rewinding the strike pointers and
  re-firing every halo at once. Lateness only ever makes the estimate later
  and easing averages it out, so the reset now fires only on real
  discontinuities: the first snapshot, a new take, or the clock running
  backwards. Measured after: **1 rewind, 28 intro strikes, 0 duplicates, every
  ring reaching 0.900.**

A burst of anchors due in a single frame is still treated as a seek rather
than a performance — keyed on the size of the burst, not on how old the
anchors are, because at a few frames a second an honest strike is already
old.

Behind either view, **one Heisei head-unit display, scaled up.** Drawn from a
photograph of a Kenwood DPX-660MD, and measured off it rather than guessed:
the columns radiate from a point below the stage — upright in the middle,
leaning further out toward the edges, their bases on an arc — so it reads as a
stadium bowl seen from the rim, and the gradient runs *along* each column,
green at the base through white-cyan to blue and violet at the tips (sampled
hues 150 → 184 → 208 → 280, the lit cores blown out white).

**One field, one waveform.** The whole background is a single display driven by
a single sound — `preview.wav`, the mix of all nine stems (`export.py`,
`mix_bands`: 20 log-spaced bands, 80 Hz–7.8 kHz, 2048-sample Hann window) —
so it rises and falls as one body. Peak-hold caps sit above each column,
hanging half a second before they fall: that is the thing that makes a
segmented analyser read as an analyser rather than as a bar chart — the bars
jump and drop, the caps linger and trace the shape of what just happened.

**It is calibrated per band, and that is what makes it move.** Measured, the
first version did not: bands 0–1 (40–68 Hz) had a mean level of 0.2 and 0.1 of
100 because the piece's lowest note is F2 at 87 Hz, and the mapping put those
two dead bands on the longest, outermost columns — the four most prominent
columns on screen never lit. Worse, normalising every band against one global
peak left bands 4–6 sitting at a near-constant 57–64 (std 8–15): the loudest
part of the display was the most static part of it, which is the opposite of
what a spectrum analyser is for. Each band is now scaled between two
percentiles of its own level (15th and 98th, chosen by sweeping them), so
every column uses its whole travel. Measured after: no band with a standard
deviation under 10, the music resting near half scale with room to peak, and
**2.5× the frame-to-frame movement** — 18.2 of 100 per band per 200 ms,
sampled off the live page. Silence is still silence; it sits far below any
band's quiet percentile. The per-stem spectra still drive the
instruments; nothing behind them is per-instrument any more. The bands are
mirrored out from the centre, and **the bass is at the edges**: the long outer
columns are the ones with room to show it, and the photograph's silhouette is
tall at the sides.

**The bowl is geometry, not data.** In the photograph the middle columns are
short and the outer ones reach the top. That is perspective — the far side of
a bowl is small — so each column's full length and cell size grow with its
angle, and the music modulates how much of that length is lit. Reading the
silhouette as data would have put the bass in the wrong place. The rim arc
dips by cos(θ), so it is placed to clear the tallest instrument at *every*
angle, not just in the middle; otherwise the bright base of the field sits on
the players.

**Everything there is light.** There is no panel for the cells to sit in: a
surface catches the stage lights, becomes a grey slab with a grid of dark
windows on it, and the backdrop stops being illumination and starts being an
office block at night. The cells are additively blended, so an unlit one
contributes exactly nothing.

`--rehearse` runs the whole console on a clock with no robots touched, which is
how the screen gets built and demonstrated. There `PC preview` plays
`preview.wav` through the browser so the screen can be checked against the mix,
and the slider relabels itself from `9 ROBOTS` to `PC PREVIEW` to say that is
what it is now moving.

**The screen does not sleep while the page is open** — Screen Wake Lock,
re-acquired on every `visibilitychange`, with a 685-byte looping video as the
fallback where the API is missing.

**It binds to loopback.** There is no authentication and no honest way to add
one: anything that can reach this port can start nine robots moving. `--host`
opts out and says so every time.

### Running it resident

An exhibition cannot depend on someone keeping a terminal open. The console
installs as a systemd **user** service:

```bash
deploy/install_service.sh              # install, enable, start
deploy/install_service.sh uninstall    # stop, disable, remove
journalctl --user -u orchestra-web -f  # logs
systemctl --user restart orchestra-web
```

`loginctl enable-linger` is what makes a *user* service come up at boot rather
than at login; the installer does it, and says so if it could not.

**Leaving it running does not arm anything.** The first thing that speaks to
the nine agents is `FleetBackend.open()`, reached only from `ShowRunner._takes()`
once the play event is set — so starting the service, restarting it and
rebooting the machine all leave the fleet exactly as they found it. Checked
after a boot: every robot `running`, `motors disabled`. The console sits in
`offline`, which is the honest name for *nothing has tried yet*. Stopping does
the opposite of arming: aiohttp's `run_app` handles SIGTERM and SIGINT alike
and runs `on_cleanup` either way, which is `runner.close()` parking them.
`TimeoutStopSec` is two minutes because a console that has opened the fleet
releases the port at once but takes 30–60 s to finish tearing down nine links,
and SIGKILL in the middle of that is the one way to leave a robot powered and
holding a pose.

**The resident service binds `0.0.0.0`, and that is a deliberate exception to
the paragraph above.** The tunnel in front of it could not reach
`127.0.0.1:8080` while the sibling exhibit on `0.0.0.0:8010` was reachable, so
whatever fronts these ports does not share this machine's loopback. Every
interface answers now, the docker bridge gateway included.

Nothing in this repo makes that safe, because nothing in it can: there is no
authentication on the port, so **anyone who can route to this host on 8080 can
start nine robots moving.** The control has to come from outside the process —
Cloudflare Access on the public URL, and a firewall rule if the LAN itself
should not have it:

```bash
sudo ufw allow from <tunnel host>/32 to any port 8080 proto tcp
sudo ufw deny 8080/tcp
```

---

## How the sound works

Every instrument you hear is a **real recording**, not synthesis. They come out of
`C:\Windows\System32\drivers\gm.dls` — the Roland-authored General MIDI sample
set Windows already ships. It is plain RIFF, so `tools/extract_samples.py` reads
it with nothing but the standard library. No download, no new dependency, and the
isolated network stays isolated.

The chain, PC-side and offline:

```
gm.dls  →  extract_samples.py  →  sampler (loop-aware, auto-tuned)
        →  render.py (22050 Hz)  →  resample to 16000  →  ensemble balance
        →  songs/small_world/audio/<part>.wav      one stem per robot
```

Two details that are easy to get wrong:

- **One stem per robot, never a shared mix.** Nine robots playing the same audio
  a few milliseconds apart comb-filters into something thin. Nine *different*
  parts turn the residual skew into "nine players", and the sound then comes from
  the same place as the motion.
- **16 kHz stereo F32 is not a choice** — the robot's audio path is hard-coded to
  it (`media/audio_base.py:59-60`). Audible bandwidth stops at 8 kHz, which
  matters for percussion: the tambourine sample had 69% of its energy above
  8 kHz. `sampler.suggest_pitch_shift` pulls those down into the band.

On the robot, playback is **explicit-PTS PCM streaming**, not `play_sound()`.
`play_sound` builds a fresh playbin and reopens the ALSA device on every call, so
all of that variable cost lands on the critical path. Instead the pipeline starts
once and stays up, and `push_audio_sample` timestamps **only the first buffer** —
everything after it is placed by byte count (`media/audio_base.py:88-104`). Call
jitter therefore only exists at the anchor; the rest of the piece is sample-exact.

Measured, nine robots, `orchestra show run small_world`:

| | |
|---|---|
| completed | 9/9, zero dropped |
| length | 104 s (32 bars at 80 BPM, plus lead-in and tail) |
| motion start error | 0.365 – 1.002 ms |
| frames | 10400/10401 on every robot, 0 late ticks |
| clock-sync uncertainty | 1.95 ms worst case |
| audio | 1041 chunks / 1,664,001 frames per robot, 0 push errors |
| clock-ratio spread | 237 ppm → **0.64 ms** across the fleet at the anchor |

---

## How the singing works — *retired, kept as a record*

> **This is no longer used.** Three synthesis routes were built and all three
> were rejected by ear: per-syllable SAPI + LPC, whole-line neural TTS + WORLD,
> and DiffSinger. The piece is now nine instruments (see *The cast*), and the
> voice modules under `orchestra/audio/` are unused by `songs/small_world`.
>
> The section is kept because the measurements in it were expensive to get and
> are still true — in particular that **every objective metric was green for a
> version that sounded terrible**, which is the reason the motion work later
> insisted on a person looking at the robots rather than reading a report.

The two singers sing **the actual words**. Nothing in `gm.dls` can do that — its
Choir Aahs is a static looped vowel — so the voice comes from somewhere else.

The chain: speak each line naturally with a neural TTS **child** voice, then
replace only its pitch and timing with the score's.

```
lyrics.py (lines + syllable splits)
  → tts.py      edge-tts, per-word timings, cached to samples/voice/
  → singer.py   WORLD: f0 / spectral envelope / aperiodicity
                ├ time-warp anchored on syllable boundaries
                ├ f0 ← the melody (portamento, vibrato, scoop)
                └ resynthesize, envelope untouched
  → voice_lead.wav · voice_harmony.wav
```

**The envelope is never touched**, and the envelope is what makes a vowel that
vowel. Only the excitation moves. That is the whole trick.

### The first attempt, and why it failed

It was built once with per-syllable Windows SAPI plus LPC resynthesis, and the
result was unusable. Four causes, each sufficient on its own:

| | |
|---|---|
| syllables synthesized **separately** | no coarticulation — people don't talk like that, so it fails as *language* no matter how good the pitch is |
| SAPI Zira is an **adult** voice, f0 ≈ 190 Hz | the melody is 294–784 Hz, so it needed an octave-plus of shift, and faking a child with a ×1.2 formant warp made it worse |
| 22nd-order all-pole fit | too coarse an envelope; detail is lost |
| pulse-train excitation, **binary** voiced/unvoiced | classic vocoder buzz, and fricatives break |

The replacement removes all four rather than tuning any of them:

| | |
|---|---|
| speak **whole lines** | there are no seams to hide |
| `en-US-AnaNeural` / `en-GB-MaisieNeural` are **children**, measured f0 median 295 / 297 Hz | overlaps the melody already; the shift is semitones, and no formant faking is needed at all |
| WORLD CheapTrick | not an all-pole approximation |
| WORLD D4C aperiodicity | a per-band ratio, not a binary flag, so fricatives and breath survive |

### Details that had to be right

- **Stretch the vowel, not the syllable.** Uniform stretching turns "s" into
  "ssss". Frames that are voiced *and* strong absorb the surplus; consonants pass
  through at 1:1. A 3× stretch of a 20-vowel/20-consonant syllable puts 100 of the
  120 output frames on the vowel.
- **Unvoiced source frames stay unvoiced.** Forcing f0 onto them turns "s" and
  "th" into vowels and destroys the word. The first version did exactly that.
- **Fill rests before smoothing the pitch line.** Rests are stored as 0 Hz, so
  smoothing across one drags the neighbouring note down — it reached 27 Hz.
- **Never let the envelope hit exactly 0.** WORLD takes its log during synthesis;
  the fade-in at a line's edge produced `-inf` → NaN, and every voice stem came
  out silent with no error. It is floored at 1e-14 and non-finite output is now
  counted and reported rather than quietly zeroed.
- **Two children, not one child twice.** Same voice for both parts fuses into one.
  They differ in source voice, vibrato rate, and detune.
- **Refuse on a word-count mismatch.** If the synthesizer returns a different
  number of words than the lyric expects, every later syllable is off by one.
  Better to stop.

Measured, `voice_lead`, first six lines:

| | |
|---|---|
| pitch error | median −0.1 cent, \|error\| median 4.9 cent |
| line duration | exact to the sample (0.0 ms over 6 lines) |
| f0 span | 286–797 Hz (melody D4–G5 = 294–784) |
| syllables vs notes | 132 / 132 lead, 78 syllables over 64 counterpoint notes |

### The one thing this can't tell you

Whether the words are actually *intelligible* is not measurable here, and the
numbers above were all green for the version that sounded terrible. Listen to it:

```bash
python tools/preview_song.py songs/small_world --only voice_lead voice_harmony
```

### Network

This is the only part of the build that reaches the internet, and it does so
**on the PC, once**. Lines are cached under `samples/voice/`; the show plays
baked WAVs. The robot network stays isolated. Rebuild with
`python tools/make_voice_bank.py`, or pass `--offline` to fail loudly instead of
fetching.

Like the `gm.dls` samples, the cached voice audio is Microsoft's, so
`samples/voice/` is gitignored and only the script is committed.

---

## Layout

```
config/
  robots.yaml          fleet registry, keyed on hardware_id (never on IP)
  fleet.yaml           network, deployment and every safety limit
  calibration/         per-robot measured values (audio lead)
orchestra/
  cli.py               the `orchestra` command
  config.py            config loading, SafetyLimits, RobotRegistry
  motion/              schema · compile · validate · layers · program · record
  motion/persona.py    per-instrument stance, beat-locked turn, intro & bow
  music/               tempo · timeline · midi · score
  conductor/           procedural conducting patterns
  fleet/               discovery · health · ssh · link (agent control channel)
  clock/               Cristian's algorithm + drift tracking
  show/                build · deploy · run
  web/                 the browser console: export · runner · server · static/
  web/static/          app · stage (seating, post, twin) · instruments · environment · palette
  preview/             single-robot playback · plots
orchestra/audio/
  sampler.py · render.py   instruments: real recordings, not synthesis
  lyrics.py · tts.py · singer.py · svs.py
                           the retired singing chain — unused by this song
agent/
  orchestra_agent.py   runs ON each robot
  audio_stream.py      explicit-PTS PCM streaming into the robot's speaker
clips/                 authored motion, beat-relative keyframes
  _retired/            the physical-instrument clips, kept with a note on why
samples/generated/     extracted from gm.dls — gitignored, see the licence note
samples/voice/         spoken lyrics — gitignored for the same reason
songs/small_world/     song.yaml · MIDI · per-robot audio stems
tools/                 extract_samples · make_stems · preview_song · skew
deploy/                the console as a resident systemd user service
tests/                 232 tests
docs/EXPERIMENTS.md    what still has to be measured on hardware
```

---

## The motion format

Two layers, deliberately separated. **Authoring** is beat-relative keyframes that
a person edits; the **compiled program** is a dense uniform frame table that a
robot plays.

```jsonc
{
  "name": "woodblock_strike",
  "instrument": "woodblock",
  "units": { "translation": "mm", "rotation": "deg" },
  "placement": "trigger",
  "anchors": [0.3],          // ← the instant SOUND IS PRODUCED
  "tracks": {
    "head": [
      { "beat": 0.00, "pose": { "z": 0,    "pitch": -5 }, "interp": "minjerk" },
      { "beat": 0.14, "pose": { "z": 3,    "pitch": -7 }, "interp": "linear"  },
      { "beat": 0.30, "pose": { "z": -7.5, "pitch": 10 }, "interp": "linear"  },
      { "beat": 0.42, "pose": { "z": -1,   "pitch": 3  }, "interp": "minjerk" },
      { "beat": 0.80, "pose": { "z": 0,    "pitch": 0  }, "interp": "minjerk" }
    ],
    "antennas": [ /* ... */ ]
  }
}
```

Four decisions worth explaining:

- **Time in beats.** A clip is reusable at any tempo and binds directly to the
  MIDI tempo map. Seconds are derived at compile time.
- **`anchors` mark the sound, not clip start.** The compiler places the clip at
  `note_beat − anchor`, so the windup happens *before* the beat, exactly as a
  player's does. This is the single thing that makes strikes land musically
  rather than late.
- **`anchors` are the one record of when sound happens**, and everything hangs
  off them: the anchor alignment above, the secondary-motion mute window, the
  gesture-overlap check, the duty-cycle heat estimate, and the note timing in
  `make_stems.py`. One source of truth for both the sound and the movement — write
  the audio timing separately and it will drift apart eventually.

  There used to be a second, narrower set (`contact`) marking anchors that
  physically struck something, which selected a laxer safety model. Removing the
  instruments removed the meaning of that flag, but **not the load on the servos**
  — an air-instrument gesture works the same motors just as hard. Splitting the
  duty-cycle check on it would have silently disabled the only heat protection
  there is, so the distinction is gone and every anchor counts.
- **XYZRPY in mm/deg, per-channel tracks.** Nobody hand-edits a rotation matrix,
  and head, body-yaw and antennas genuinely have independent rhythms.

Interpolation reproduces the SDK's four curves *exactly*
(`tests/test_interp.py` asserts bit-identical agreement), so a clip previewed
through `goto_target` and the same clip played by the agent are the same motion.

---

## The cast

Reachy Mini has **9 motors and no arms**: 6 Stewart actuators driving the head,
1 body yaw, 2 antennas. With nothing mounted, the whole job of the motion is to
make it *legible* — you should be able to tell who is playing what from across
the room, without hearing a note.

**The first version of this failed.** Every clip was authored around the strike —
a small up-down on each note — and from five metres away all nine looked the
same. Measuring why was the turning point:

| axis | operating limit | largest value in any clip | used |
|---|---|---|---|
| head z | ±22 mm | 5.0 mm | 23 % |
| head y | ±25 mm | 5.0 mm | 20 % |
| tilt cone | 25° | 7.0° | 28 % |
| **body yaw** | **150°** | **2.5°** | **1.7 %** |

Position was never the constraint. **Dynamics** were: joint velocity 4.0 rad/s
and a 10-second RMS duty ceiling. Since velocity is amplitude ÷ time, a fast
per-note gesture *must* be small — but a slow or sustained one can be enormous.
The identity had been put in the one place it could not grow.

So it moved. `orchestra/motion/persona.py` gives each instrument three layers,
and the per-note strike is now only the third of them:

1. **Stance** — a held posture. Zero velocity, so it costs nothing in the duty
   budget, and it is visible even while that robot is resting.
2. **Beat-locked body rotation** — the body swings around *forward* on the beat,
   with a different amplitude, period and phase per instrument. Coupled roll
   trails it by a quarter cycle.
3. **Per-note strike** — the existing clips, now carrying timing rather than
   identity.

| Robot | Part | Swing | Period | How you recognise it |
|---|---|---|---|---|
| r01 | conductor | — | — | Beat patterns with prep, ictus and cutoff. Turns its body to face sections. Procedural, not a clip. |
| r02 | celesta (lead) | ±8° | 4 beats | Nearly front-on, looking down at a keyboard, traversing sideways over four bars. |
| r03 | vibraphone (counter) | ±15° | **3 beats** | The only one that keeps ringing after the hit — the shimmer stays. |
| r04 | glockenspiel | ±6° | **1 beat** | **The only chin-up robot**, ears held high, small circle. |
| r05 | marimba | **±16°** | 2 beats | Swings hardest, looks down deepest, ears folded. |
| r06 | wood block | ±17° | 4 beats | Sharp single hits that stop dead. **One ear up, one down.** |
| r07 | tambourine | ±13° | 2 beats, **offbeat** | **Head permanently cocked** — the only one. |
| r08 | shaker | ±7° | 1 beat, **antiphase** | Never stops. Tilts the opposite way to r07. |
| r09 | jingle bells | ±14° | 2 beats, **offbeat** | **Ears in a class of their own** (±95°). The head barely moves. |

**Body yaw is centred on forward for all nine.** An earlier version parked each
robot at a fixed angle (marimba −55°, wood block +55°) so they faced permanently
off to one side, which just read as *robots pointing the wrong way*. Identity
comes from **how they swing**, not which way they face.

Measured after the change — `body_yaw_peak_deg` from `song build --report`,
against the 2.5° that every robot shared before:

```
r05 marimba 26.9    r06 wood block 19.0    r03 vibraphone 16.9    r09 bells 15.6
r07 tambourine 14.7 r02 celesta    10.5    r08 shaker      8.5    r04 glock  8.2
```

Up to **10.8×**. The spread matters more than the magnitude: if all nine landed
on the same number the original problem would be back.

Underneath all nine runs a **groove layer**: everyone rides the beat lightly at
all times, including while resting. Whether that layer is on is the difference
between nine robots doing things near each other and an ensemble.

### The stance is not free

The obvious assumption — a constant offset has zero velocity, so it costs
nothing — is wrong, and the safety gate proved it:

```
r04 glockenspiel   joint_vel_peak  3.078 -> 3.763   (+22 %, ceiling 4.0)
r02 celesta        joint_vel_peak  3.508 -> 3.801   (+8 %)
r05 marimba        joint_vel_peak  2.710 -> 2.747   (+1 %)
```

The offset itself carries no velocity, but it **moves the Stewart platform to a
different operating point, so the Jacobian changes** and the existing per-note
motion costs more joint travel there. How much depends entirely on the pose
(+1 % to +22 %), so a budget table is not enough — `song build --report` has to
be read after every change. On r04 the driver turned out to be the chin-up
*pitch*, not the yaw.

⚠ **Antennas are xc330-class at ±0.8 N·m — a quarter of the Stewart servos.**
The easiest motor on the robot to destroy — and for a while the duty-cycle gate
was not watching them at all, which left r09 (the one part designed around them)
completely unprotected. Both the head and the antennas now carry their own
sustained-work limit. See `docs/EXPERIMENTS.md`.

Hard limits, all from the SDK/URDF: head tilt cone **±35°**, head yaw relative to
body **±55°** (Placo) / ±65° (analytical), body yaw **±160°**, Stewart joints
asymmetric between −80° and +80°. Our operating limits sit inside all of them.

⚠ **The head pose is in world frame.** `validate.py` computes
`rel_yaw = head_yaw − body_yaw` against a 45° limit, so turning the body without
turning the head spends the entire turn out of that budget. Every persona turns
both together.

---

## The shape of a show

A program is **144.4 s**. The music is the middle 99 s of it.

```
  0.0 –   1.0   wake into the waiting pose
  0.0 –   4.5   the conductor turns 180° to the audience, bows and turns back
  4.5 –  27.7   eight introductions, 2.9 s each:
                  0.7 s  the conductor cues that robot
                  1.9 s  that robot sounds its instrument, moving big
                  0.3 s  gap
                everyone else holds one identical pose, completely still
 27.7 –  30.7   the conductor's last cue: all nine lift their faces together
 33.2           T0. the piece begins
127.7 – 132.2   final chord, held 6 beats; sound and motion decay together
132.5           ears go up, faces level
133.4 – 138.1   bow, held (ears fold with the head)
139.9 – 142.9   stow: the head sinks 44 mm, ears fold back
```

Three things make this hold together, and each was a bug first.

**The introductions are generated from the persona, not authored.** A robot's
showcase is its own performance stance, exaggerated, and the last frame of the
showcase *is* the first frame of the stance hold. Eight hand-written clips would
have drifted from the personas the first time anyone retuned a number. The
*audio* comes from the same place — the first few notes of that part's own MIDI
— so the sound and the motion cannot disagree about which instrument it is.

**Everyone else stops.** Only the conductor and the robot being introduced move;
the other seven hold `WAIT_POSE` (ears down, looking slightly down) at
**0.0000 /s** of measured motion. The first version only waited *until* its own
turn, so introduced robots carried on moving and by the end seven of them were
in motion at once — nobody could tell who was being introduced.

**The ending is a sequence of absolute poses, not offsets.** Bowing is "go to
this pose", not "add a bow to whatever you are holding". Added as offsets it
stacked on each robot's stance and blew straight through the envelope — antennas
to −225°, tilt to 37.2°. The secondary layers have to stop too: breathing
(−5 mm) and groove (−2.2 mm) on top of the stow's −44 mm reached −51 mm. A
sleeping robot does not breathe.

### Turning all the way round

The conductor bows to the audience, which means turning 180° from where it
conducts. Body yaw only reaches ±150°, so from a mechanical zero facing front
that is impossible.

The fix is placement, not software: **the conductor stands rotated 90° on the
table**, and its home is `body_yaw = −90°` (facing the ensemble). The bow is
`+90°`. The difference is exactly **180°**, and both ends sit inside the range.
Measured: −89.6° during the piece, +90.0° at the bow, **179.6° apart**, using
119° of the available 150°.

One consequence worth knowing: because the head pose is world-frame, the stow's
−21 mm x offset has to be **rotated with the body**, or the mechanism is asked
for a sideways displacement it cannot reach (`stewart_1` at −0.965 against a
−0.726 limit).

---

## Development workflow

```bash
orchestra clip record --robot r07 --out clips/shaker/mine.clip.json --bpm 112
       # gravity-comp hand puppeteering → 100 Hz capture → simplified keyframes
orchestra clip check --clip shaker/mine --bpm 112 --bpm 140   # gate, per tempo
orchestra clip plot  --clip shaker/mine --bpm 140             # see the margin
orchestra clip preview --clip shaker/mine --robot r07         # one robot
orchestra song build small_world --report                     # all nine
```

`clip check` takes the tempo because **a clip safe at 90 BPM can be unsafe at
140** — beat-relative keyframes compress in real time. Check at the fastest
tempo you will actually play.

`clip record` exists because hand-puppeteering under gravity compensation is
dramatically faster than typing angles for anything that has to look alive.

---

## Verification

```bash
pytest -q                              # 113 tests
python tools/sync_selftest.py --agents 9   # protocol, sync, abort, deadman
python tools/measure_skew.py rec.wav --bpm 112   # the measurement that counts
```

The self-test spawns nine agents in dry-run and drives the real show sequence.
It proves the logic; it does **not** prove timing — one machine, one clock,
loopback. The real number comes from a microphone, which is what
`measure_skew.py` is for. Percussion attacks read as simultaneous below roughly
10–20 ms; that is the bar.

**Measuring with the robots' own microphones.** No external mic is needed. The
wireless units' XMOS front end removes *their own* playback (echo cancellation
uses the local speaker as its reference) but leaves everyone else's alone —
verified: when one robot emitted a 1 kHz tone, the listener's 1 kHz share went
0.012 → 0.38 and its RMS rose 8×. So the conductor can record the performance it
is conducting:

```bash
orchestra show run small_world --record r01     # records, pulls the WAV back
orchestra calibrate skew calib --recording calib-r01.wav calib-r06.wav --mode waveform
```

Two things this taught, both worth keeping:

**The song cannot be the test signal.** Correlating stems against a recording of
the piece produced no peak at all (best margin 1.07) because the music repeats —
16 bars twice, with repeated figures inside that. A periodic signal has no unique
correlation lag. `tools/make_calibration.py` therefore emits a per-robot
band-limited noise burst with a different seed each, prints their measured mutual
correlation (0.084 — orthogonal enough), and each robot fires three times so the
agreement *between* its own repeats becomes the error bar. Well-heard robots
scatter 0.1–0.9 ms; a robot hearing only reflections scatters 40 ms and is
rejected rather than reported.

**One microphone position cannot calibrate anything.** Recording the same signal
from two different robots moved r04 by 29.7 ms and r07 by 14.7 ms, while r09 and
r08 barely shifted. What a single mic measures is overwhelmingly distance and
reflection, not robot timing. Feeding those numbers into `audio_lead_ms` would
deliberately mis-time the fleet for every listener except the one that recorded —
worse than not calibrating at all. Passing two or more recordings splits the
per-robot figure into a positional part and a part that survives every vantage
point; only the latter is a candidate for `audio_lead_ms`, and so far nothing has
cleared that bar. Every unit's value is still **0.0**, on purpose.

The fleet's own timing is verified separately and does hold up: start error
0.3–1.5 ms, clock-sync uncertainty 1.95 ms, 10400/10401 frames on all nine with
zero late ticks.

### Being wrong on purpose is cheaper than being wrong by accident

Several conclusions in `docs/EXPERIMENTS.md` are corrections of earlier
measurements that looked clean and were not: an autocorrelation that found a
loop length instead of a pitch, a "silent" baseline recorded while the other
robots were audible, a 1-second clock window reporting ±852 ppm of pure noise, a
duty-cycle metric that watched the head while the part under test moved only its
ears, a melody confidently recalled and simply wrong. Each is written down with
the wrong number next to the right one, because the failure mode of this
hardware is *silence* — the daemon swallows an IK failure and holds the last
pose — so a measurement that quietly lies is the most expensive thing here.

The sharpest example is the conductor. Its downbeat gesture tore open by 45% in
a single frame at every bar line, and the safety gate said PASS for months —
because at 112 BPM a beat is 53.57 frames, so the tear landed at a different
sub-frame offset each bar and got averaged under the ceiling. Changing the tempo
to 80 BPM, where a beat is exactly 75 frames, made it 6.02 rad/s against a 4.00
limit. A limit that only holds while the tempo does not divide evenly into the
frame rate is not a limit; `tests/test_conductor.py` now checks both kinds of
tempo deliberately.

---

## Scaling to 10+ robots

Nothing in the code knows a robot count. Add an entry to `robots.yaml` and a line
to the song's `cast`. More parts than robots reports which are unassigned; more
robots than parts gives the spares an idle program so they still breathe.

Per-robot traffic during a show is ~1 msg/s of heartbeat, so Wi-Fi scales well
past ten.

---

## Known state of the fleet

- All nine reachable, firmware **1.9.0**, steady-state RTT ~6–7 ms.
- **r04 (192.168.3.133)** was in daemon state `error` with its motor backend
  down. `orchestra fleet recover --robot r04` restarts it over SSH.
- **r02 (192.168.3.120)** briefly reported as a **Lite** variant. That was a
  side effect of a missing camera, not the variant: with no `/dev/video0` the
  daemon downgrades its own report. After the camera was fitted it reports
  `imx708` / `wireless` like the rest.
- **r04** had both antenna motors missing (`Missing motors: ["right_antenna",
  "left_antenna"]`), which survived a reboot — a real hardware fault, since
  repaired.
- The daemon binds `0.0.0.0` with CORS `*` and **no authentication**. Anyone on
  the SSID can command motion. Fine on an isolated network; worth knowing.
- First packet after idle spikes to 17–90 ms (Wi-Fi power-save). The 1 Hz
  heartbeat exists to keep that out of the START path.

See [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) for what still needs measuring
on hardware, and which parts of the design each answer would change.

---

## Note on the music

*It's a Small World* (Sherman Brothers, 1963) is under copyright. Private and
internal use is normally unproblematic; public performance or streaming is a
separate question. The pipeline is song-agnostic — point it at any MIDI file.

The score in the repo is a **real transcription**: 32 bars, verse and chorus,
80 BPM, 652 notes across six tracks. It was found as a freely-downloadable MIDI
(a `ROL2MID` conversion mirrored on several free MIDI archives) and imported with
`tools/import_midi.py`, which renames source tracks to the names `song.yaml`
binds parts to.

It replaced a melody written from memory — which turned out to be **wrong**. The
real chorus climbs a whole step on each repetition:

| | |
|---|---|
| It's a small world after all | C5 C5 \| **E5** C5 D5 |
| It's a small world after all | D5 D5 \| **F5** D5 E5 |
| It's a small world after all | E5 E5 \| **G5** E5 F5 |
| It's a small, small world | F5 E5 D5 G4 \| B4 C5 |

The from-memory version kept it level. Two independent sources agree on the
rising form: the downloaded MIDI, and a free piano tutorial that spells the same
three steps. `tools/make_small_world.py` still generates the wrong one — it is
kept as a fallback for songs with no MIDI, and now refuses to overwrite the real
score without `--force`.

Which track goes where matters more than usual, because this song's signature is
a **counterpoint**: the verse melody plays *over* the chorus.

| Track | Part | |
|---|---|---|
| Melody | `lead` (celesta) | the tune |
| Counter | `counter` (vibraphone) | ⭐ the verse played against the chorus |
| Harmony | `glocken` | offbeat comping, present throughout |
| Bass | `marimba` | bass line, which suits the low lateral gesture |

### The transcription fades out; it does not end

The MIDI is an amusement-park loop, so it has no cadence — it just gets quieter
and stops mid-phrase. Measured melody velocities over the last few bars:

```
94 -> 86 -> 72 -> 56 -> 11 -> 8      then two stray notes at 34 (the next repeat)
```

That is why it sounded like the piece was never finished: it wasn't. The
`finale:` block in `song.yaml` fixes it in three steps — `unfade_from_bar`
restores the velocities, `trim_after_bar` drops the stray pickup, and `at_bar`
lays a tonic chord under the melody's held note so the ensemble lands together.

The chord is appended **to the timeline**, not to the stems, so motion and audio
both pick it up from one source. Where a track already sounds on that beat the
chord **replaces** that note rather than adding one — two placements on the same
beat sum their amplitude and pushed the marimba to 5.54 rad/s against the 4.0
ceiling.

That assignment was inverted at first — `Counter` given to the glockenspiel and
`Harmony` to the second melodic voice. The names invite it: "Harmony" sounds like
the supporting part and "Counter" like a filler one. Previewing the tracks showed
the opposite. `Harmony` is offbeat comping; `Counter` is the real counterpoint,
and it is the one line in this song that has to be carried by a voice the ear can
follow, because the whole effect is hearing two melodies at once.

Swapping in a different score:

```bash
python tools/import_midi.py yours.mid --list          # see what's in it
python tools/import_midi.py yours.mid --preview 1     # is track 1 really the tune?
python tools/import_midi.py yours.mid --song songs/small_world     --map 1=Melody 3=Harmony 4=Counter 5=Bass
orchestra song build small_world
python tools/make_stems.py songs/small_world
orchestra show deploy small_world
```

Track numbers are never guessed for you. A wrong guess plays a different line at
full volume, which is far harder to notice than silence. The sampler was checked
from C2 to C7 and stays audible across that span, so an unfamiliar key will not
silently render nothing either.

The samples are extracted from **`gm.dls`, which is Microsoft-distributed and
Roland-authored**. Playing them on your own machine is one thing; committing or
redistributing the extracted WAVs is a separate decision that nobody here has
made. So the repository holds **the extraction script only** — the output lands
in `samples/generated/`, which is gitignored. Anyone cloning this runs
`tools/extract_samples.py` once against their own Windows install.
