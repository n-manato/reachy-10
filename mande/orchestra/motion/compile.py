"""Compile authored clips + a placement schedule into a dense Program.

The composition model is **additive deltas over neutral**. Each placement
contributes ``clip_value - neutral``, and contributions sum. Two reasons:

* It composes naturally. A shaker pattern, a phrase sway and a breathing layer
  can all run at once without any of them knowing about the others.
* Within Reachy Mini's actual workspace (a +/-35 deg head cone) small-angle
  addition of XYZRPY deltas is well behaved and, crucially, *predictable* to
  the person editing the clip. Composing in SE(3) would be more "correct" and
  much harder to reason about while tuning by eye.

Layering priority is expressed through per-placement gain envelopes, not through
overwrite ordering -- see :mod:`orchestra.motion.layers`. A strike placement can
suppress everything below it in a window around its anchor, which is how the
"instrument accuracy always beats expression" rule is enforced mechanically
rather than by convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..music.tempo import TempoMap
from .program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_X,
    CH_YAW,
    N_CHANNELS,
    AudioSpec,
    Program,
)
from .schema import PATTERN, TRIGGER, Clip

# SDK neutral antenna position (reachy_mini.py:47). Not zero: a ~10 deg outward
# offset measurably reduces shake when the antennas sit vertical.
DEFAULT_NEUTRAL_ANTENNAS = (-0.1745, 0.1745)


def neutral_frame(antennas: Sequence[float] = DEFAULT_NEUTRAL_ANTENNAS) -> np.ndarray:
    f = np.zeros(N_CHANNELS, dtype=np.float64)
    f[CH_ANT_R] = float(antennas[0])
    f[CH_ANT_L] = float(antennas[1])
    return f


@dataclass
class Placement:
    """One scheduled instance of a clip on the song timeline."""

    clip: Clip
    start_beat: float
    end_beat: float | None = None
    """For PATTERN clips: loop until this beat. None => a single pass."""

    gain: float = 1.0
    layer: int = 0
    """Higher layers are composited later and may suppress lower ones."""

    suppress_below: bool = False
    """When True, this placement's anchors mute all lower layers nearby."""

    suppress_window_s: float = 0.08
    suppress_ramp_s: float = 0.12
    label: str = ""

    def anchor_beats(self) -> list[float]:
        """Absolute song-beat positions of every sound-producing instant."""
        clip = self.clip
        if not clip.anchors:
            return []
        if clip.placement == TRIGGER or self.end_beat is None:
            return [self.start_beat + a for a in clip.anchors]

        out: list[float] = []
        n = int(np.ceil((self.end_beat - self.start_beat) / clip.loop_beats))
        for i in range(max(0, n)):
            base = self.start_beat + i * clip.loop_beats
            for a in clip.anchors:
                t = base + a
                if self.start_beat - 1e-9 <= t < self.end_beat - 1e-9:
                    out.append(t)
        return out


@dataclass
class CompileResult:
    program: Program
    placements: list[Placement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _sample_placement(
    placement: Placement,
    song_beats: np.ndarray,
    neutral: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample one placement onto the song's frame grid.

    Returns ``(delta, active)`` where delta is (N, 9) offsets from neutral and
    active is an (N,) bool mask of frames the placement covers.
    """
    from .interp import interp_track  # local import keeps module import cheap

    clip = placement.clip
    n = len(song_beats)
    delta = np.zeros((n, N_CHANNELS), dtype=np.float64)

    local = song_beats - placement.start_beat

    if clip.placement == PATTERN and placement.end_beat is not None:
        active = (song_beats >= placement.start_beat - 1e-9) & (
            song_beats < placement.end_beat - 1e-9
        )
        # Wrap into the loop. Modulo on the *local* beat keeps the pattern
        # phase-locked to the placement start, not to the song origin, so a
        # pattern that begins mid-bar still loops correctly.
        local = np.mod(local, clip.loop_beats)
    else:
        extent = clip.duration_beats()
        active = (local >= -1e-9) & (local <= extent + 1e-9)

    if not active.any():
        return delta, active

    lb = local[active]

    if (tr := clip.track("head")) is not None:
        vals = interp_track(lb, tr.beats, tr.values, tr.interps)
        delta[active, CH_X : CH_YAW + 1] = vals

    if (tr := clip.track("body_yaw")) is not None:
        vals = interp_track(lb, tr.beats, tr.values, tr.interps)
        delta[active, CH_BODY_YAW] = vals[:, 0]

    if (tr := clip.track("antennas")) is not None:
        vals = interp_track(lb, tr.beats, tr.values, tr.interps)
        # Antenna keyframes are authored as absolute angles, so subtract neutral
        # to make them additive like every other channel.
        delta[active, CH_ANT_R] = vals[:, 0] - neutral[CH_ANT_R]
        delta[active, CH_ANT_L] = vals[:, 1] - neutral[CH_ANT_L]

    return delta * placement.gain, active


def _suppression_envelope(
    times: np.ndarray, anchors: np.ndarray, window_s: float, ramp_s: float
) -> np.ndarray:
    """1.0 everywhere except a smooth notch to 0.0 around each anchor.

    This is the mechanism behind "instrument accuracy outranks expression": a
    strike's anchors carve a hole in every lower layer, so nothing perturbs the
    head while it is producing sound.
    """
    from .interp import smoothstep

    env = np.ones_like(times)
    if len(anchors) == 0 or (window_s <= 0 and ramp_s <= 0):
        return env

    for a in anchors:
        d = np.abs(times - a)
        # 0 inside the window, ramping back to 1 across `ramp_s` outside it.
        g = smoothstep((d - window_s) / max(ramp_s, 1e-9))
        env = np.minimum(env, g)
    return env


def compile_placements(
    placements: Sequence[Placement],
    tempo: TempoMap,
    duration_s: float,
    fps: float = 100.0,
    neutral_antennas: Sequence[float] = DEFAULT_NEUTRAL_ANTENNAS,
    robot_id: str = "",
    song: str = "",
    part: str = "",
    instrument: str | None = None,
    audio: AudioSpec | None = None,
    lead_in_s: float = 0.0,
) -> CompileResult:
    """Render placements to a uniform frame table.

    Args:
        duration_s: total program length, excluding `lead_in_s`.
        lead_in_s: silent frames prepended at neutral. Gives the robot a moment
            to settle at the start pose after START fires and before the first
            musical event -- and gives the conductor room for a prep beat.
    """
    if fps <= 0:
        raise ValueError("fps must be > 0")

    neutral = neutral_frame(neutral_antennas)
    total_s = max(0.0, duration_s) + max(0.0, lead_in_s)
    n = int(round(total_s * fps)) + 1
    times = np.arange(n, dtype=np.float64) / fps

    # Musical time starts after the lead-in. Beats go NEGATIVE during the
    # lead-in, deliberately: a strike clip anchored on the very first note needs
    # its windup to happen before beat 0, and the lead-in is exactly the room
    # provided for that. Clamping here would silently truncate the anticipation
    # on every downbeat-starting part.
    song_seconds = times - lead_in_s
    song_beats = tempo.seconds_to_beats(song_seconds)

    warnings: list[str] = []
    ordered = sorted(placements, key=lambda p: (p.layer, p.start_beat))

    # Anchors of every suppressing placement, in program seconds.
    suppressors: list[tuple[Placement, np.ndarray]] = []
    for p in ordered:
        if not p.suppress_below:
            continue
        ab = p.anchor_beats()
        if ab:
            at = tempo.beats_to_seconds(np.asarray(ab)) + lead_in_s
            suppressors.append((p, at))

    acc = np.zeros((n, N_CHANNELS), dtype=np.float64)
    all_anchor_times: list[float] = []

    for p in ordered:
        delta, active = _sample_placement(p, song_beats, neutral)
        if not active.any():
            warnings.append(
                f"placement {p.label or p.clip.name!r} at beat {p.start_beat:g} "
                "covers no frames (starts past the end of the program?)"
            )
            continue

        # Every suppressor on a strictly higher layer carves into this one.
        for sp, anchor_times in suppressors:
            if sp.layer <= p.layer or sp is p:
                continue
            env = _suppression_envelope(
                times, anchor_times, sp.suppress_window_s, sp.suppress_ramp_s
            )
            delta *= env[:, None]

        acc += delta

        ab = p.anchor_beats()
        if ab:
            all_anchor_times.extend(
                (tempo.beats_to_seconds(np.asarray(ab)) + lead_in_s).tolist()
            )

    frames = acc + neutral[None, :]

    anchors = np.asarray(sorted(t for t in all_anchor_times if 0.0 <= t <= total_s))
    program = Program(
        robot_id=robot_id,
        song=song,
        part=part,
        instrument=instrument,
        fps=fps,
        frames=frames,
        anchors=anchors,
        audio=audio,
        meta={
            "lead_in_s": lead_in_s,
            "tempo": repr(tempo),
            "n_placements": len(ordered),
        },
    )
    return CompileResult(program=program, placements=list(ordered), warnings=warnings)


def compile_clip_preview(
    clip: Clip,
    bpm: float,
    loops: int = 4,
    fps: float = 100.0,
    neutral_antennas: Sequence[float] = DEFAULT_NEUTRAL_ANTENNAS,
    lead_in_s: float = 0.5,
    robot_id: str = "",
) -> CompileResult:
    """Compile a single clip on repeat -- the Phase-1 iteration workhorse.

    Used by ``orchestra clip preview``, ``clip check`` and ``clip plot`` so all
    three see byte-identical motion.
    """
    tempo = TempoMap.constant(bpm)
    span = clip.loop_beats if clip.placement == PATTERN else clip.duration_beats()
    span = max(span, 1e-6)
    total_beats = span * max(1, loops)

    if clip.placement == PATTERN:
        placements = [
            Placement(clip=clip, start_beat=0.0, end_beat=total_beats, label=clip.name)
        ]
    else:
        # A trigger clip is fired once per `span`, exactly as the song compiler
        # would fire it on successive notes.
        placements = [
            Placement(clip=clip, start_beat=i * span, label=f"{clip.name}#{i}")
            for i in range(max(1, loops))
        ]

    duration_s = float(tempo.beats_to_seconds(total_beats))
    return compile_placements(
        placements,
        tempo=tempo,
        duration_s=duration_s + 0.25,  # let the final recovery finish
        fps=fps,
        neutral_antennas=neutral_antennas,
        robot_id=robot_id,
        song="preview",
        part=clip.name,
        instrument=clip.instrument,
        lead_in_s=lead_in_s,
    )
