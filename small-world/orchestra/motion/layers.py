"""Expressive secondary motion, and the rule that keeps it subordinate.

Robots that only do their instrument motion read as machines. Small sway,
breathing and phrase-following read as musicians. But **instrument accuracy
outranks expression**, and that is enforced mechanically here rather than left to
authoring discipline: every secondary layer is multiplied by a gain envelope that
goes to zero inside a window around each strike anchor. A sway can never perturb
a strike, no matter what amplitudes someone tunes.

Everything is additive in XYZRPY -- the same model the compiler uses -- so layers
compose without knowing about each other.

The SDK offers ``compose_world_offset`` (utils/interpolation.py:187) for layering
in SE(3), and the ethics-podcast project uses it that way. We stay in XYZRPY
because within a +/-35 deg head cone the two agree to well under the hardware's
resolution, and per-axis addition is far easier to reason about while tuning by
eye. :func:`compose_world_offset_frames` is provided for the cases where exact
SE(3) layering is wanted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .interp import head_pose_matrices, matrices_to_xyzrpy, smoothstep
from .program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_PITCH,
    CH_X,
    CH_Y,
    CH_YAW,
    CH_Z,
    N_CHANNELS,
    Program,
)


def phase_seed(key: str) -> float:
    """Stable per-robot phase offset in [0, 2pi).

    Nine robots breathing in perfect unison reads as a glitch, not an ensemble.
    Derived from the hardware_id so it is deterministic across runs.
    """
    h = hashlib.sha256(key.encode("utf-8")).digest()
    return (int.from_bytes(h[:4], "big") / 0xFFFFFFFF) * 2.0 * np.pi


@dataclass
class SecondaryContext:
    """Everything a secondary layer may look at."""

    times: np.ndarray
    """Program time in seconds, one entry per frame."""

    anchors: np.ndarray
    """Sound-producing instants in seconds. Used for muting and for idleness."""

    phrase_env: np.ndarray | None = None
    """0..1 musical intensity per frame. None => flat 1.0."""

    seed: float = 0.0
    conductor_azimuth_rad: float = 0.0
    """Where the conductor is, from this robot's point of view. Used by
    :func:`attention` so performers glance at the right place."""

    @property
    def n(self) -> int:
        return len(self.times)

    def envelope(self) -> np.ndarray:
        if self.phrase_env is None:
            return np.ones(self.n)
        return np.clip(self.phrase_env, 0.0, 1.0)


def _empty(ctx: SecondaryContext) -> np.ndarray:
    return np.zeros((ctx.n, N_CHANNELS), dtype=np.float64)


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


def idleness(ctx: SecondaryContext, delay_s: float = 0.3, fade_s: float = 0.8) -> np.ndarray:
    """0 while playing, ramping to 1 once the robot has been silent a while.

    Gates breathing so a robot looks alive during rests without wobbling through
    its own part.
    """
    if len(ctx.anchors) == 0:
        return np.ones(ctx.n)
    # Time since the most recent anchor at or before each frame.
    idx = np.searchsorted(ctx.anchors, ctx.times, side="right") - 1
    since = np.where(idx >= 0, ctx.times - ctx.anchors[np.clip(idx, 0, None)], np.inf)
    # Also suppress just *before* the next anchor -- anticipation, not surprise.
    nxt = np.searchsorted(ctx.anchors, ctx.times, side="left")
    until = np.where(
        nxt < len(ctx.anchors), ctx.anchors[np.clip(nxt, 0, len(ctx.anchors) - 1)] - ctx.times, np.inf
    )
    quiet = np.minimum(since, until)
    return smoothstep((quiet - delay_s) / max(fade_s, 1e-9))


def breathing(
    ctx: SecondaryContext,
    z_amp_m: float = 0.005,
    freq_hz: float = 0.1,
    antenna_amp_rad: float = 0.26,
    antenna_freq_hz: float = 0.5,
    gate: np.ndarray | None = None,
) -> np.ndarray:
    """Slow vertical breath plus antenna drift.

    Amplitudes are the values proven in the ethics-podcast project
    (robot_worker.py:64-94): 5 mm of Z at 0.1 Hz, ~15 deg of antenna at 0.5 Hz.
    Reused rather than re-derived because they are known to look right on this
    hardware.
    """
    out = _empty(ctx)
    g = np.ones(ctx.n) if gate is None else gate

    out[:, CH_Z] = z_amp_m * np.sin(2 * np.pi * freq_hz * ctx.times + ctx.seed)
    ant = antenna_amp_rad * np.sin(
        2 * np.pi * antenna_freq_hz * ctx.times + ctx.seed * 0.5
    )
    # Antennas move in opposition, which reads as breathing rather than drifting.
    out[:, CH_ANT_R] = -ant * 0.5
    out[:, CH_ANT_L] = ant * 0.5
    return out * g[:, None]


def beat_groove(
    ctx: SecondaryContext,
    beats: np.ndarray,
    z_amp_m: float = 0.0022,
    pitch_deg: float = 1.4,
    body_yaw_deg: float = 1.0,
    sway_bars: float = 2.0,
    beats_per_bar: float = 4.0,
) -> np.ndarray:
    """全機が常に軽くリズムに乗る層。

    自分の出番が無い間も含めて**常に**かかる。これが入るかどうかで、9台が
    「合奏している集団」に見えるか「順番に動く機械」に見えるかが分かれる。

    形は拍に位相同期した小さな上下動。拍の頭で沈み、裏で戻る ―― 人が
    音楽に合わせて体を揺らすときと同じ位相。振幅は意図的にごく小さくしてある
    （2.2mm / 1.4度）。楽器のジェスチャと張り合うと、どちらが本題か分から
    なくなるため。

    アンカー付近では既存の消音機構が自動でこれを弱めるので、自分の演奏中は
    自然に引っ込む。機体ごとの位相は hardware_id から散らしてあり、9台が
    完全に揃って上下すると生き物ではなく機械に見えてしまうのを避けている。

    Args:
        beats: 各フレームの拍位置（負値＝リードイン中）。
    """
    out = _empty(ctx)
    if len(beats) != ctx.n:
        raise ValueError("len(beats) does not match the frame count")

    # 拍内の位相。拍頭で 0、裏で 0.5。
    phase = 2.0 * np.pi * (beats % 1.0)
    # 機体ごとにごく小さな位相差をつける（±3%拍程度）。揃いすぎを崩すだけ。
    jitter = 0.06 * np.pi * np.sin(ctx.seed)

    # 拍頭で沈む: -cos なので phase=0 で最小
    bob = -np.cos(phase + jitter)
    out[:, CH_Z] = z_amp_m * bob
    out[:, CH_PITCH] = np.radians(pitch_deg) * bob * 0.6

    # 小節をまたぐゆっくりした左右の揺れ。拍の刻みだけだと硬く見える。
    slow = np.sin(2.0 * np.pi * beats / (sway_bars * beats_per_bar) + ctx.seed)
    out[:, CH_BODY_YAW] = np.radians(body_yaw_deg) * slow

    # 演奏が始まる前（リードイン中）は徐々に立ち上げる。いきなり全機が
    # 揺れ出すと、指揮者の予備拍が埋もれる。
    ramp = smoothstep((beats + 2.0) / 2.0)
    return out * ramp[:, None]


def phrase_sway(
    ctx: SecondaryContext,
    yaw_deg: float = 1.5,
    lean_mm: float = 3.0,
    freq_hz: float = 0.18,
) -> np.ndarray:
    """A slow lean that follows the musical phrase.

    Scaled by the phrase envelope, so the robot opens up through a phrase and
    settles at the cadence. This is the layer that most makes an ensemble look
    like it is listening to itself.
    """
    out = _empty(ctx)
    env = ctx.envelope()

    # Sway lives in the body and in lateral lean only. It deliberately leaves
    # head yaw alone: head yaw is how a performer aims at its instrument, and
    # nudging it would move the beater off target between strikes.
    out[:, CH_BODY_YAW] = (
        np.radians(yaw_deg) * np.sin(2 * np.pi * freq_hz * ctx.times + ctx.seed) * env
    )
    out[:, CH_Y] = (
        (lean_mm / 1000.0)
        * np.sin(2 * np.pi * freq_hz * 0.5 * ctx.times + ctx.seed * 0.7)
        * env
    )
    return out


def attention(
    ctx: SecondaryContext,
    events_s: np.ndarray,
    amount_deg: float = 8.0,
    hold_s: float = 0.7,
    travel_s: float = 0.35,
) -> np.ndarray:
    """Turn briefly toward the conductor at the given moments.

    Fired on phrase boundaries only -- a robot that looks at the conductor on
    every beat reads as nervous, not attentive.
    """
    out = _empty(ctx)
    if len(events_s) == 0:
        return out

    amount = np.radians(amount_deg)
    profile = np.zeros(ctx.n)
    for e in np.atleast_1d(events_s):
        d = ctx.times - e
        rise = smoothstep(d / max(travel_s, 1e-9))
        fall = 1.0 - smoothstep((d - hold_s) / max(travel_s, 1e-9))
        profile = np.maximum(profile, np.clip(rise * fall, 0.0, 1.0))

    # Yaw toward the conductor's bearing, with a small forward lean so the
    # gesture reads as "leaning in to look" rather than a flat swivel.
    out[:, CH_YAW] = amount * np.sin(ctx.conductor_azimuth_rad) * profile
    out[:, CH_X] = 0.004 * np.cos(ctx.conductor_azimuth_rad) * profile
    return out


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def anchor_gain(
    times: np.ndarray,
    anchors: np.ndarray,
    mute_window_s: float = 0.08,
    ramp_s: float = 0.12,
) -> np.ndarray:
    """1 between strikes, 0 within `mute_window_s` of any strike.

    This single function *is* the "instrument accuracy beats expression" rule.
    """
    g = np.ones_like(times)
    if len(anchors) == 0:
        return g
    for a in np.atleast_1d(anchors):
        d = np.abs(times - a)
        g = np.minimum(g, smoothstep((d - mute_window_s) / max(ramp_s, 1e-9)))
    return g


def apply_secondary(
    program: Program,
    deltas: list[np.ndarray],
    mute_window_s: float = 0.08,
    ramp_s: float = 0.12,
) -> Program:
    """Add secondary layers to a program, muted around every strike.

    Returns a new Program; the input is not modified, so a caller can validate
    both and compare.
    """
    if not deltas:
        return program

    t = program.times()
    gain = anchor_gain(t, program.anchors, mute_window_s, ramp_s)

    total = np.zeros_like(program.frames)
    for d in deltas:
        if d.shape != program.frames.shape:
            raise ValueError(
                f"secondary layer has shape {d.shape}, expected {program.frames.shape}"
            )
        total += d

    out = Program(
        robot_id=program.robot_id,
        song=program.song,
        part=program.part,
        instrument=program.instrument,
        fps=program.fps,
        frames=program.frames + total * gain[:, None],
        anchors=program.anchors.copy(),
        audio=program.audio,
        validation=dict(program.validation),
        meta={**program.meta, "secondary_layers": len(deltas)},
    )
    return out


def compose_world_offset_frames(
    frames: np.ndarray, offset_xyzrpy: np.ndarray
) -> np.ndarray:
    """Exact SE(3) layering, mirroring the SDK's ``compose_world_offset``.

    ``R_final = R_off @ R_abs``, ``t_final = t_abs + t_off``
    (utils/interpolation.py:187-227), with the rotation re-orthonormalised via
    SVD to stop drift accumulating over a long program.

    The additive XYZRPY path is the default because it is easier to tune; use
    this when a layer applies large rotations where the two would diverge.
    """
    base = head_pose_matrices(frames[:, CH_X : CH_YAW + 1])
    off = head_pose_matrices(offset_xyzrpy)

    r = off[:, :3, :3] @ base[:, :3, :3]
    u, _, vt = np.linalg.svd(r)
    r = u @ vt

    out = np.zeros_like(base)
    out[:, :3, :3] = r
    out[:, :3, 3] = base[:, :3, 3] + off[:, :3, 3]
    out[:, 3, 3] = 1.0

    new = frames.copy()
    new[:, CH_X : CH_YAW + 1] = matrices_to_xyzrpy(out)
    return new
