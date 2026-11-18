"""The show, reshaped for a browser.

The page draws a digital twin of the nine robots. It recomputes nothing:
`song build` already wrote every frame of every robot to
`build/<song>/<id>.program.json.gz`, and that is the same data the robots
themselves play. This module reshapes exactly that, and nothing else, into one
file the page fetches once.

Three decisions worth the words.

**Nothing is re-derived from the score.** The twin has one job -- to match the
robots -- and the only thing guaranteed to match the robots is the build they
are running. Regenerating choreography on the PC for display would be a second
implementation of the same intent, free to drift from the first, and it would
drift silently and be wrong exactly when it mattered: right after a rebuild.

**Nothing is served straight from the program files.** Each is 14446 frames x 9
channels plus a validation report; nine of them is 1.3 M numbers delivered to a
page that consumes 60 frames a second. Resampling to 50 Hz and rounding to 4
decimals costs nothing visible and cuts it by five.

**50 Hz, not 30.** The fastest thing on screen is a strike -- roughly 100 ms of
downstroke. 50 Hz puts five samples on it and a browser interpolating to 60 fps
keeps its shape. At 30 Hz the attack visibly softens, which is the one thing a
percussion twin must not do. Four decimals is 0.1 mm and 0.006 deg, well below
what the servos it portrays can resolve.
"""

from __future__ import annotations

import gzip
import json
import wave
from pathlib import Path
from typing import Any

import numpy as np

FORMAT = "reachy-orchestra-stage"
VERSION = 5

STAGE_FPS = 50.0
"""Resample rate for the browser. See the module docstring."""

DECIMALS = 4
"""Rounding for frame values. SI units, so 1e-4 is 0.1 mm / 0.006 deg."""

BLOCK_BARS = 8.0
"""Bars per verse/chorus block when the song does not say otherwise.

`songs/small_world/song.yaml` documents its own structure as 32 bars =
Verse8 + Chorus8 + Verse8 + Chorus8, so eight is right here, but it is a
convention rather than a fact about the file -- a song that wants different
blocks can declare `sections:` and this is skipped entirely.
"""


class ExportError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


def load_program(path: Path) -> dict[str, Any]:
    """One compiled program, as `song build` wrote it."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ExportError(f"cannot read {path}: {e}") from e
    if data.get("format") != "reachy-orchestra-program":
        raise ExportError(f"{path} is not a compiled program")
    return data


def resample(frames: list[list[float]], src_fps: float, dst_fps: float) -> np.ndarray:
    """Linear resample along time, per channel.

    Linear and not something smoother on purpose. The source is already the
    final motion at 100 Hz -- any smoothing here would show the viewer a motion
    the robots are not making, and the one thing this view is for is believing
    what it shows.

    Every channel is an angle or a displacement in SI units, none of them wrap,
    so there is no shortest-arc problem to get wrong.
    """
    a = np.asarray(frames, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] == 0:
        raise ExportError("program has no frames")
    if dst_fps >= src_fps:
        return a
    n_out = int(round((a.shape[0] - 1) * dst_fps / src_fps)) + 1
    src_t = np.arange(a.shape[0]) / src_fps
    dst_t = np.arange(n_out) / dst_fps
    out = np.empty((n_out, a.shape[1]), dtype=np.float64)
    for c in range(a.shape[1]):
        out[:, c] = np.interp(dst_t, src_t, a[:, c])
    return out


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def sections_for(score, duration: float) -> list[dict[str, Any]]:
    """Named spans of the program, in seconds from its start.

    The page uses these for the backdrop: the intro lights the lanterns one by
    one as each robot names itself, each verse/chorus block moves the colour
    somewhere else in the world, and the finale blooms. So they have to line up
    with the music to the beat, which means deriving them from the same tempo
    map the build used rather than from guessed timings.

    An explicit `sections:` block in the song wins. Nothing here is clever
    enough to be trusted over someone writing it down.
    """
    tl = getattr(score, "timeline", None)
    if tl is None:
        return [{"name": "program", "start": 0.0, "end": round(duration, 3)}]

    t0 = float(score.t0_s)
    bpb = tl.meter.beats_per_bar
    total_bars = tl.total_beats / bpb
    music_end = t0 + float(tl.tempo.beats_to_seconds(tl.total_beats))

    def at_bar(bar: float) -> float:
        return t0 + float(tl.tempo.beats_to_seconds(bar * bpb))

    out: list[dict[str, Any]] = []
    if t0 > 0:
        out.append({"name": "intro", "start": 0.0, "end": round(t0, 3)})

    declared = (score.raw.get("sections") if getattr(score, "raw", None) else None)
    if declared:
        for s in declared:
            bar = float(s.get("bar", 0.0))
            end_bar = float(s.get("end_bar", total_bars))
            out.append({
                "name": str(s.get("name", "section")),
                "start": round(at_bar(bar), 3),
                "end": round(at_bar(end_bar), 3),
            })
    else:
        finale_bar = None
        if getattr(score, "finale", None):
            raw_bar = score.finale.get("at_bar")
            if raw_bar is not None:
                finale_bar = float(raw_bar)
        body_end = finale_bar if finale_bar is not None else total_bars

        bar, i = 0.0, 0
        while bar < body_end - 1e-6:
            end = min(bar + BLOCK_BARS, body_end)
            out.append({
                "name": "verse" if i % 2 == 0 else "chorus",
                "index": i // 2 + 1,
                "start": round(at_bar(bar), 3),
                "end": round(at_bar(end), 3),
            })
            bar, i = end, i + 1
        if finale_bar is not None:
            out.append({
                "name": "finale",
                "start": round(at_bar(finale_bar), 3),
                "end": round(music_end, 3),
            })

    if duration > music_end + 1e-3:
        # Everything after the last note: the decay tail plus the bow and the
        # face being put away. One span, because the backdrop treats it as one
        # gesture -- it settles.
        out.append({
            "name": "outro",
            "start": round(music_end, 3),
            "end": round(duration, 3),
        })
    return out


# ---------------------------------------------------------------------------
# What each robot is playing
# ---------------------------------------------------------------------------
# The robots have no instruments. The sound is nine speakers and the motion is
# acting, so the one question the audience actually asks -- "which one is the
# marimba?" -- is one the robots cannot answer. The page answers it with the
# instrument itself, and for that it needs two things the motion does not
# carry: *which note* (for the four pitched parts) and *how loud, right now*
# (for all nine, including the vibraphone's ring after the strike).


def note_events(score, part) -> list[list[float]] | None:
    """`[t, pitch, velocity, duration_s]` per note, in program seconds.

    None for a part the score cannot resolve into notes: the conductor, and
    the loop-type parts whose rhythm lives inside the clip rather than in the
    score (`ride`, `pulse`, `colour`). Those still have anchors.

    Checked against the build: for every pitched part the note instants and
    the program's anchors agree to the millisecond, so the page can trust
    either as the strike and use this only for what it adds.
    """
    from ..music.score import ScoreError

    if part is None or part.kind == "conductor":
        return None
    tl = score.timeline
    if tl is None:
        return None
    try:
        evs = score.events_for_part(part)
    except ScoreError:
        return None
    t0 = float(score.t0_s)
    out = []
    for e in evs:
        start = t0 + float(tl.tempo.beats_to_seconds(e.beat))
        end = t0 + float(tl.tempo.beats_to_seconds(e.beat + e.duration_beats))
        out.append([round(start, 3), int(e.pitch), int(e.velocity),
                    round(max(0.0, end - start), 3)])
    out.sort()
    return out


ONSET_WIN = 1024
ONSET_HOP = 256          # about 6 ms at 44.1 kHz: fine enough to place a note
ONSET_GAMMA = 20.0       # log compression, so a quiet note counts like a loud one
ONSET_MEDIAN_S = 0.20    # the window the adaptive threshold is taken over
ONSET_DELTA = 0.08       # a floor, so silence does not trigger on its own noise
ONSET_LAMBDA = 1.6       # how far above the local median a peak must stand
ONSET_REFRACTORY = 0.12  # one note is one onset


def onsets(path: Path | None, fps: float, before: float) -> list[float]:
    """Times this stem starts a note, up to `before`.

    The build's `anchors` cover the music and nothing else: not one of them
    falls inside the 33 s introduction, which is the one stretch whose whole
    job is to say which robot plays what. So the introduction's strikes are
    recovered from the stem instead.

    By SPECTRAL FLUX, not by the level crossing a threshold. Each instrument
    plays a six-note flourish when it is named, and on a level meter that is
    one event: a marimba's first note is still decaying when the second is
    struck, so the envelope never falls back under the threshold and the
    other five are invisible. Measured on these stems, the level crossing
    found one onset for the celesta, one for the vibraphone, one for the
    marimba and one for the bells -- a single flash where there is a phrase,
    which is why nothing on screen looked like it was playing. The rise in
    *spectral* energy, summed over the bins and compared against its own
    local median, finds all six for every part.

    Deliberately not used during the music. There the anchors are the build's
    own answer, exact to the millisecond, and a vibraphone still ringing would
    produce onsets the robot is not playing.
    """
    if path is None or before <= 0:
        return []
    a, sr = _mono(path)
    if a is None:
        return []
    # only the stretch asked for, plus a window's grace
    a = a[:max(0, int(round((before + 0.25) * sr)))]
    if len(a) < ONSET_WIN * 2:
        return []

    win, hop = ONSET_WIN, ONSET_HOP
    n = 1 + (len(a) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    mag = np.abs(np.fft.rfft(a[idx] * np.hanning(win).astype(np.float32), axis=1))
    # log compression first: otherwise the flux is the loud notes only
    d = np.diff(np.log1p(ONSET_GAMMA * mag), axis=0)
    flux = np.maximum(0.0, d).sum(axis=1)       # half-wave rectified: rises only
    if flux.max() <= 0:
        return []
    flux /= flux.max()

    # adaptive threshold: a peak has to stand out from its own neighbourhood,
    # which is what lets a note inside another note's decay still count
    fr = sr / hop
    half = max(1, int(round(ONSET_MEDIAN_S * fr)))
    pad = np.pad(flux, half, mode="edge")
    med = np.median(np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1), axis=-1)
    thr = ONSET_DELTA + ONSET_LAMBDA * med

    out: list[float] = []
    peak = (flux[1:-1] > thr[1:-1]) & (flux[1:-1] >= flux[:-2]) & (flux[1:-1] > flux[2:])
    for i in np.flatnonzero(peak) + 1:
        t = (i + 1) * hop / sr + win / (2 * sr)
        if t > before:
            break
        if not out or t - out[-1] >= ONSET_REFRACTORY:
            out.append(round(float(t), 3))
    return out


BAND_EDGES_HZ = [60, 150, 400, 1000, 2500, 5000, 8000]
"""Six bands, roughly a fifth of a decade apart, up to the stems' Nyquist.

Chosen so the parts land in different places: the marimba's bass line (F2 is
87 Hz) sits in the bottom two, the glockenspiel's bars (2-5 kHz) in the top
two, the celesta and vibraphone across the middle. A spectrum analyser whose
bands all moved together would be a level meter with extra columns.
"""

BAND_RANGE_DB = 48.0
"""Display range below each stem's own peak. Forty-eight dB is what the
segmented meters on the head units this is drawn from showed, and it keeps
the quiet bands visible without pinning the loud ones."""


def band_levels(path: Path, fps: float, n_frames: int,
                edges_hz: list[float] = BAND_EDGES_HZ) -> list[list[int]]:
    """Per-frame band energies of one stem, 0..100, aligned with `envelope`.

    A short-time Fourier transform at the stage frame rate: a 1024-sample
    window (64 ms at 16 kHz) centred on each frame, Hann, power summed per
    band, in dB against the stem's own peak over all bands and frames. One
    normalisation per stem rather than per band, so the *shape* of the
    spectrum is honest -- a glockenspiel's bass band stays dark, because it
    is.

    This is the stem the robot plays, not the PC mix: the analyser behind a
    seat shows that seat's sound.
    """
    try:
        with wave.open(str(path), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error):
        return [[0] * (len(edges_hz) - 1)] * n_frames
    if sw != 2:
        return [[0] * (len(edges_hz) - 1)] * n_frames
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)

    win = 1024
    hop = sr / fps
    half = win // 2
    padded = np.concatenate([np.zeros(half, np.float32), a, np.zeros(win, np.float32)])
    starts = np.clip(np.round(np.arange(n_frames) * hop).astype(np.int64), 0, len(padded) - win)
    frames = np.lib.stride_tricks.sliding_window_view(padded, win)[starts]
    window = np.hanning(win).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)

    nb = len(edges_hz) - 1
    power = np.empty((n_frames, nb), dtype=np.float64)
    for b in range(nb):
        lo, hi = edges_hz[b], edges_hz[b + 1]
        sel = (freqs >= lo) & (freqs < hi)
        power[:, b] = spec[:, sel].sum(axis=1) if sel.any() else 0.0
    db = 10.0 * np.log10(power + 1e-12)
    peak = float(db.max())
    if peak <= -100.0:
        return [[0] * nb] * n_frames
    level = np.clip((db - (peak - BAND_RANGE_DB)) / BAND_RANGE_DB, 0.0, 1.0)
    return np.round(level * 100).astype(int).tolist()


MIX_BANDS = 20
"""Bands across the backdrop's half-fan, mirrored to both sides.

The reference display is one continuous field, not a row of separate meters,
and it is symmetric: twenty bands out from the centre, drawn twice. Low
frequencies in the middle, so the bass moves the whole body of it and the
highs ripple at the edges.
"""

MIX_LO_HZ, MIX_HI_HZ = 80.0, 7800.0
"""The band the music actually occupies.

It started at 40 Hz, and the lowest two bands came out at a mean level of 0.2
and 0.1 of 100 -- the piece's lowest note is F2 at 87 Hz, so there is simply
nothing down there. On the fan those two bands sit on the longest, outermost
columns, so the four most prominent columns on screen never lit at all.
"""

MIX_PCT_LO, MIX_PCT_HI = 15.0, 98.0
"""Each band is scaled between two percentiles of its own level.

One global reference for all twenty bands left the sustained middle of the
spectrum pegged near two thirds and barely moving: the loudest part of the
display was the most static part of it, which is the opposite of what a
spectrum analyser is for. A single per-band reference fixed the dead bands but
put every mean up at 55-87, so the field sat near full instead of dancing.

Two percentiles set both ends: this band's quiet is the bottom of its column
and its loud is the top, so every column uses its whole travel and an ordinary
bar of music lands in the middle of the scale. Consumer analysers are
calibrated so that ordinary music fills the display; this is that calibration,
measured from the piece rather than guessed.

Silence is still silence: it sits far below any band's quiet percentile.

15 and 98 were chosen by sweeping them: the music then rests near half scale
with room to peak, the field moves about two and a half times as much from
frame to frame as a single global reference gave, and the introduction -- one
instrument at a time, far quieter than the tutti that sets the percentiles --
still registers instead of clipping to nothing.
"""


def mix_bands(path: Path, fps: float, n_frames: int,
              n_bands: int = MIX_BANDS) -> list[list[int]]:
    """A log-spaced spectrum of the whole mix, 0..100, one row per frame.

    `preview.wav` is the render of all nine stems together -- the one
    waveform the backdrop answers to. Per-stem spectra drive the instruments;
    this drives the field behind them, so it rises and falls as a single
    thing rather than as nine things that happen to be next to each other.
    """
    try:
        with wave.open(str(path), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error):
        return [[0] * n_bands] * n_frames
    if sw != 2:
        return [[0] * n_bands] * n_frames
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)

    win, hop, half = 2048, sr / fps, 1024
    padded = np.concatenate([np.zeros(half, np.float32), a, np.zeros(win, np.float32)])
    starts = np.clip(np.round(np.arange(n_frames) * hop).astype(np.int64), 0, len(padded) - win)
    frames = np.lib.stride_tricks.sliding_window_view(padded, win)[starts]
    spec = np.abs(np.fft.rfft(frames * np.hanning(win).astype(np.float32), axis=1)) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)

    edges = np.geomspace(MIX_LO_HZ, min(MIX_HI_HZ, sr / 2), n_bands + 1)
    power = np.empty((n_frames, n_bands), dtype=np.float64)
    for b in range(n_bands):
        sel = (freqs >= edges[b]) & (freqs < edges[b + 1])
        # a narrow low band can fall between two bins; take the nearest one
        power[:, b] = spec[:, sel].mean(axis=1) if sel.any() else \
            spec[:, np.argmin(np.abs(freqs - (edges[b] + edges[b + 1]) / 2))]
    db = 10.0 * np.log10(power + 1e-12)
    if float(db.max()) <= -100.0:
        return [[0] * n_bands] * n_frames

    # Percentiles are taken over the frames where anything is sounding at
    # all; computed over the silence before the music they would sit at the
    # noise floor and every band would read full.
    loud = db.max(axis=1) > db.max() - 60.0
    live = db[loud] if loud.any() else db
    lo = np.percentile(live, MIX_PCT_LO, axis=0)
    hi = np.percentile(live, MIX_PCT_HI, axis=0)
    span = np.maximum(hi - lo, 6.0)        # a band that never varies still gets a scale
    level = np.clip((db - lo) / span, 0.0, 1.0)
    return np.round(level * 100).astype(int).tolist()


def _mono(path: Path) -> tuple[np.ndarray | None, int]:
    """One stem as mono float samples, or (None, 0) if it cannot be read.

    A stem that cannot be read never raises: the show does not depend on the
    screen, and a missing file should cost a glow, not a performance.
    """
    try:
        with wave.open(str(path), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error):
        return None, 0
    if sw != 2:
        return None, 0
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def envelope(path: Path, fps: float, n_frames: int) -> list[float]:
    """Loudness of one stem, one value per stage frame, 0..1.

    RMS over each frame's worth of samples, normalised to the stem's own peak.
    Per stem rather than against the mix on purpose: the question on screen is
    "is *this* one sounding", not "how loud is it compared with the marimba".

    This is the file the robot plays. Not a model of it, not the PC mix -- the
    same bytes `show deploy` copies to the robot, so the glow on screen and the
    sound in the room come from one source. A stem that cannot be read gives a
    silent envelope rather than an error; the show does not depend on it.
    """
    try:
        with wave.open(str(path), "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error):
        return [0.0] * n_frames
    if sw != 2:
        return [0.0] * n_frames
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)

    hop = sr / fps
    edges = (np.arange(n_frames + 1) * hop).round().astype(np.int64)
    edges = np.clip(edges, 0, len(a))
    sq = np.concatenate([[0.0], np.cumsum(a.astype(np.float64) ** 2)])
    counts = np.maximum(1, edges[1:] - edges[:-1])
    rms = np.sqrt((sq[edges[1:]] - sq[edges[:-1]]) / counts)
    peak = float(rms.max())
    if peak <= 1e-6:
        return [0.0] * n_frames
    return np.round(rms / peak, 3).tolist()


# ---------------------------------------------------------------------------
# Stage data
# ---------------------------------------------------------------------------


def stage_data(
    ctx,
    score,
    robots: list,
    build_dir: Path | None = None,
    fps: float = STAGE_FPS,
) -> dict[str, Any]:
    """Everything the page needs about this song, in one dict.

    Robots with no compiled program are left out rather than faked. A seat on
    screen with nothing in it is a lie about the cast; an absent seat is a
    visible, checkable fact.
    """
    build = Path(build_dir) if build_dir else (ctx.build_dir / score.name)
    if not build.exists():
        raise ExportError(
            f"no build at {build}. Compile it first: "
            f"orchestra song build --song {score.name}"
        )

    entries: list[dict[str, Any]] = []
    duration = 0.0
    src_fps = 0.0
    channels: list[str] = []

    for r in robots:
        path = build / f"{r.id}.program.json.gz"
        if not path.exists():
            continue
        prog = load_program(path)
        frames = resample(prog["frames"], float(prog["fps"]), fps)
        duration = max(duration, float(prog["duration"]))
        src_fps = max(src_fps, float(prog["fps"]))
        channels = channels or list(prog["channels"])

        part = score.part_for_robot(r.id)
        anchors = [round(float(a), 3) for a in prog.get("anchors", [])]
        notes = note_events(score, part)
        pitches = [n[1] for n in notes] if notes else []
        stem_name = (prog.get("audio") or {}).get("file") or (part.audio if part else "")
        stem = Path(score.source_dir or build) / "audio" / stem_name if stem_name else None
        env = envelope(stem, fps, len(frames)) if stem else [0.0] * len(frames)
        entries.append({
            "id": r.id,
            "role": r.role,
            "part": prog.get("part") or "",
            "instrument": prog.get("instrument") or r.instrument or "",
            "voice": getattr(part, "voice_preset", "") if part else "",
            "duration": round(float(prog["duration"]), 3),
            "audio_lead_ms": float((prog.get("audio") or {}).get("lead_ms", 0.0)),
            "anchors": anchors,
            # stop short of the first anchor: an onset a frame before it is
            # the same note, and the pair would read as a double strike
            "onsets": onsets(stem, fps, (anchors[0] - 0.25) if anchors else 0.0),
            "notes": notes,
            # One pitch is a drum with a MIDI number, not a melody: the
            # woodblock's pattern is written on C4 and should not get a bar.
            "pitched": len(set(pitches)) > 1,
            "pitch_range": [min(pitches), max(pitches)] if pitches else None,
            "envelope": env,
            "bands": band_levels(stem, fps, len(frames)) if stem else [[0] * (len(BAND_EDGES_HZ) - 1)] * len(frames),
            "persona": (prog.get("meta") or {}).get("persona", {}),
            "frames": np.round(frames, DECIMALS).tolist(),
        })

    n_long = max(len(e["frames"]) for e in entries)
    mix_path = Path(score.source_dir) / "preview.wav" if score.source_dir else None
    if mix_path is not None and not mix_path.exists():
        mix_path = None

    if not entries:
        raise ExportError(
            f"{build} holds no program for any of the selected robots "
            f"({', '.join(r.id for r in robots)})"
        )

    tl = getattr(score, "timeline", None)
    return {
        "format": FORMAT,
        "version": VERSION,
        "song": score.name,
        "title": score.title or score.name,
        "duration": round(duration, 3),
        "fps": fps,
        "source_fps": src_fps,
        "channels": channels,
        "music": {
            "bpm": round(float(tl.tempo.initial_bpm), 3) if tl else None,
            "meter": [tl.meter.numerator, tl.meter.denominator] if tl else None,
            "beats": round(float(tl.total_beats), 3) if tl else None,
            "bars_per_phrase": int(score.bars_per_phrase),
            "t0_s": round(float(score.t0_s), 3),
        },
        "sections": sections_for(score, duration),
        "band_edges_hz": list(BAND_EDGES_HZ),
        "mix_bands": mix_bands(mix_path, fps, n_long) if mix_path else [],
        "intro_order": _intro_order(score),
        "robots": entries,
    }


def _intro_order(score) -> list[str]:
    """Robot ids in the order they name themselves during the intro.

    The backdrop lights one lantern per robot as it plays, so it needs the
    order the *build* used, which is part names in `intro.order` resolved
    through the cast -- not the order robots happen to appear in robots.yaml.
    """
    order = (score.intro or {}).get("order") or []
    out = []
    for part_name in order:
        rid = score.robot_for(part_name)
        if rid:
            out.append(rid)
    return out


def write_stage(path: Path, data: dict[str, Any]) -> Path:
    """Write stage data as gzipped JSON, which is also how it is served."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, separators=(",", ":")).encode("utf-8")
    path.write_bytes(gzip.compress(blob, 6))
    return path


def stage_bytes(data: dict[str, Any]) -> bytes:
    """Gzipped JSON, ready to hand to a client with Content-Encoding: gzip."""
    return gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 6)
