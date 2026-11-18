"""Turn a hand-puppeteered recording into editable keyframes.

The SDK's ``start_recording``/``stop_recording`` capture ~100 Hz of absolute
poses while the robot is under gravity compensation, i.e. while you move its
head by hand. That is a far faster way to author expressive motion than typing
angles -- but a thousand raw samples is not something anyone can edit.

So we simplify. Ramer-Douglas-Peucker keeps the points that carry the shape and
drops the ones that lie on a line between their neighbours, which for hand
motion is most of them. A 3-second capture typically reduces to 8-15 keyframes
that a person can then nudge.

Recorded frames carry ``time`` *inside* each dict as absolute ``time.time()``
(reachy_mini.py:547-556), which is a different layout from the ``RecordedMove``
file format's parallel ``time`` array. This module handles the transposition and
rebases to zero.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from ..music.tempo import TempoMap
from .interp import MIN_JERK, matrices_to_xyzrpy
from .schema import HEAD_AXES, PATTERN, TRIGGER, Clip, Track


def _rdp_mask(points: np.ndarray, epsilon: np.ndarray) -> np.ndarray:
    """Ramer-Douglas-Peucker over an N-dimensional polyline.

    `epsilon` is per-dimension so that millimetres and radians can be given
    sensible, independent tolerances -- a single scalar would make whichever
    unit is numerically larger dominate the result.
    """
    n = len(points)
    keep = np.zeros(n, dtype=bool)
    if n == 0:
        return keep
    keep[0] = keep[-1] = True
    if n <= 2:
        return keep

    scale = np.where(epsilon > 0, 1.0 / epsilon, 0.0)
    pts = points * scale

    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        a, b = pts[i0], pts[i1]
        ab = b - a
        denom = float(ab @ ab)
        seg = pts[i0 + 1 : i1]
        if denom < 1e-18:
            dist = np.linalg.norm(seg - a, axis=1)
        else:
            tproj = ((seg - a) @ ab) / denom
            proj = a + np.outer(np.clip(tproj, 0.0, 1.0), ab)
            dist = np.linalg.norm(seg - proj, axis=1)
        k = int(np.argmax(dist))
        if dist[k] > 1.0:  # already normalised by epsilon
            idx = i0 + 1 + k
            keep[idx] = True
            stack.append((i0, idx))
            stack.append((idx, i1))
    return keep


def parse_recording(raw: Sequence[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    """SDK record list -> (times rebased to 0, (N,9) SI frames)."""
    from .program import (
        CH_ANT_L,
        CH_ANT_R,
        CH_BODY_YAW,
        CH_X,
        CH_YAW,
        N_CHANNELS,
    )

    rows, times = [], []
    for entry in raw:
        if "head" not in entry:
            continue
        times.append(float(entry.get("time", 0.0)))
        head = np.asarray(entry["head"], dtype=np.float64).reshape(4, 4)
        xyzrpy = matrices_to_xyzrpy(head[None])[0]
        ant = entry.get("antennas") or [0.0, 0.0]
        row = np.zeros(N_CHANNELS)
        row[CH_X : CH_YAW + 1] = xyzrpy
        row[CH_BODY_YAW] = float(entry.get("body_yaw", 0.0) or 0.0)
        row[CH_ANT_R] = float(ant[0])
        row[CH_ANT_L] = float(ant[1])
        rows.append(row)

    if not rows:
        raise ValueError("recording contains no head poses")

    t = np.asarray(times, dtype=np.float64)
    t -= t[0]
    return t, np.asarray(rows, dtype=np.float64)


def to_clip(
    raw: Sequence[dict[str, Any]],
    name: str,
    bpm: float,
    instrument: str = "",
    placement: str = PATTERN,
    loop_beats: float | None = None,
    trim_start: float = 0.0,
    trim_end: float = 0.0,
    pos_epsilon_mm: float = 0.6,
    rot_epsilon_deg: float = 0.8,
    ant_epsilon_deg: float = 2.0,
    quantize: float = 0.0,
    max_keys: int = 40,
) -> Clip:
    """Convert a capture into an authored :class:`Clip`.

    Args:
        bpm: the tempo you were moving at. Keyframe times are stored in beats,
            so this is what makes the clip tempo-portable afterwards.
        quantize: snap keyframe beats to this grid (e.g. 0.25 for sixteenths).
            0 disables. Snapping usually helps -- hand timing is close to the
            grid but rarely on it, and a strike two frames early reads as sloppy.
        max_keys: if simplification leaves more than this, tolerances are
            loosened until it fits. An 80-keyframe clip is not editable.
    """
    from .program import CH_ANT_L, CH_ANT_R, CH_BODY_YAW, CH_X, CH_YAW

    t, frames = parse_recording(raw)

    if trim_start or trim_end:
        m = (t >= trim_start) & (t <= (t[-1] - trim_end))
        if m.sum() < 2:
            raise ValueError("trim removed almost everything -- check the values")
        t, frames = t[m] - t[m][0], frames[m]

    spb = 60.0 / bpm
    beats = t / spb

    eps = np.array(
        [
            pos_epsilon_mm / 1000.0, pos_epsilon_mm / 1000.0, pos_epsilon_mm / 1000.0,
            math.radians(rot_epsilon_deg), math.radians(rot_epsilon_deg),
            math.radians(rot_epsilon_deg),
            math.radians(rot_epsilon_deg),        # body_yaw
            math.radians(ant_epsilon_deg), math.radians(ant_epsilon_deg),
        ]
    )

    keep = _rdp_mask(frames, eps)
    loosen = 1.0
    while keep.sum() > max_keys and loosen < 64:
        loosen *= 1.5
        keep = _rdp_mask(frames, eps * loosen)

    idx = np.flatnonzero(keep)
    kb = beats[idx]
    if quantize > 0:
        kb = np.round(kb / quantize) * quantize
        # Quantisation can collide two keys; keep the first of each group.
        _, first = np.unique(kb, return_index=True)
        idx = idx[np.sort(first)]
        kb = np.unique(kb)

    kv = frames[idx]
    n = len(idx)
    interps = [MIN_JERK] * n

    tracks: dict[str, Track] = {
        "head": Track("head", kb.copy(), kv[:, CH_X : CH_YAW + 1].copy(), list(interps)),
        "antennas": Track(
            "antennas", kb.copy(),
            np.column_stack([kv[:, CH_ANT_R], kv[:, CH_ANT_L]]), list(interps),
        ),
    }
    if np.abs(kv[:, CH_BODY_YAW]).max() > math.radians(0.5):
        tracks["body_yaw"] = Track(
            "body_yaw", kb.copy(), kv[:, CH_BODY_YAW : CH_BODY_YAW + 1].copy(),
            list(interps),
        )

    span = float(kb[-1]) if len(kb) else 1.0
    if loop_beats is None:
        # Round to a musically sensible loop rather than the exact capture
        # length, which is never a round number of beats.
        loop_beats = max(1.0, round(span * 2) / 2)

    if placement == PATTERN:
        # A looping clip whose ends differ jumps once per bar; make them match.
        for tr in tracks.values():
            if abs(float(tr.beats[-1]) - loop_beats) < 0.5:
                tr.beats[-1] = loop_beats
                tr.values[-1] = tr.values[0]

    return Clip(
        name=name,
        instrument=instrument or None,
        description=(
            f"Captured by hand under gravity compensation at {bpm:g} BPM, "
            f"simplified from {len(frames)} samples to {n} keyframes."
        ),
        placement=placement,
        loop_beats=float(loop_beats),
        anchors=[],
        tracks=tracks,
        raw_units={"translation": "mm", "rotation": "deg"},
    )


def suggest_anchors(
    raw: Sequence[dict[str, Any]], bpm: float, min_gap_beats: float = 0.2
) -> list[float]:
    """Guess where a captured motion produced sound.

    Uses the deepest point of each downward excursion in head z -- for a struck
    instrument that is where the beater is lowest, which is where contact is.
    A suggestion only: check it against what you actually heard, because the
    anchor is what the whole musical alignment hangs off.
    """
    from .program import CH_Z

    t, frames = parse_recording(raw)
    z = frames[:, CH_Z]
    if len(z) < 5:
        return []

    spb = 60.0 / bpm
    minima = []
    for i in range(2, len(z) - 2):
        if z[i] < z[i - 1] and z[i] <= z[i + 1] and z[i] < z.mean() - 0.5 * z.std():
            minima.append(i)

    out: list[float] = []
    for i in minima:
        b = t[i] / spb
        if not out or b - out[-1] >= min_gap_beats:
            out.append(round(b, 3))
    return out
