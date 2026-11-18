"""Interpolation curves and pose construction.

Deliberately dependency-free (numpy only) so that clip compilation works without
the Reachy Mini SDK installed, and so the on-robot agent stays light.

The four easing curves reproduce the SDK's own
``reachy_mini.utils.interpolation.time_trajectory`` exactly -- see
``tests/test_interp_matches_sdk.py``, which asserts equality against the SDK
whenever it is importable. Do not "improve" the maths here: matching the SDK is
the point, so that a clip previewed via ``goto_target`` and the same clip played
by the agent produce identical motion.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

# Curve names, matching reachy_mini.utils.interpolation.InterpolationTechnique
LINEAR = "linear"
MIN_JERK = "minjerk"
EASE_IN_OUT = "ease_in_out"
CARTOON = "cartoon"

INTERPOLATIONS = (LINEAR, MIN_JERK, EASE_IN_OUT, CARTOON)

# Back-ease overshoot constants -- utils/interpolation.py:116-117
_C1 = 1.70158
_C2 = _C1 * 1.525


def ease(t: npt.ArrayLike, method: str = MIN_JERK) -> np.ndarray:
    """Map normalised time t in [0,1] through an easing curve.

    Returns the eased parameter, NOT necessarily in [0,1]: ``cartoon`` is a
    back-ease that dips below 0 (anticipation) and overshoots above 1. That is
    intentional and matches the SDK, but it means a cartoon segment commands
    poses *beyond* its endpoints -- which is why validate.py forbids cartoon
    near a strike anchor.
    """
    t = np.asarray(t, dtype=np.float64)

    if method == LINEAR:
        return t
    if method == MIN_JERK:
        # Standard quintic smoothstep: zero velocity AND acceleration at both
        # ends. utils/interpolation.py:106
        return 10.0 * t**3 - 15.0 * t**4 + 6.0 * t**5
    if method == EASE_IN_OUT:
        # Quadratic in/out. Note: acceleration is discontinuous at t=0.5.
        return np.where(t < 0.5, 2.0 * t * t, 1.0 - ((-2.0 * t + 2.0) ** 2) / 2.0)
    if method == CARTOON:
        lo = ((2.0 * t) ** 2 * ((_C2 + 1.0) * 2.0 * t - _C2)) / 2.0
        hi = (((2.0 * t - 2.0) ** 2 * ((_C2 + 1.0) * (2.0 * t - 2.0) + _C2)) + 2.0) / 2.0
        return np.where(t < 0.5, lo, hi)

    raise ValueError(
        f"unknown interpolation {method!r}; expected one of {list(INTERPOLATIONS)}"
    )


def interp_track(
    sample_beats: np.ndarray,
    key_beats: np.ndarray,
    key_values: np.ndarray,
    key_interps: list[str],
) -> np.ndarray:
    """Sample a keyframed track.

    Args:
        sample_beats: (N,) times to sample, in beats.
        key_beats:    (K,) strictly increasing keyframe times, in beats.
        key_values:   (K, D) keyframe values.
        key_interps:  length K; ``key_interps[i]`` eases the segment LEAVING
                      keyframe i. This is the animation convention -- a
                      keyframe owns the curve to its right.

    Returns:
        (N, D) sampled values. Before the first key and after the last, the
        endpoint value is held (no extrapolation).
    """
    key_values = np.atleast_2d(np.asarray(key_values, dtype=np.float64))
    if key_values.shape[0] != len(key_beats):
        key_values = key_values.T

    n_keys = len(key_beats)
    out = np.empty((len(sample_beats), key_values.shape[1]), dtype=np.float64)

    if n_keys == 1:
        out[:] = key_values[0]
        return out

    # Segment index for each sample: i such that key_beats[i] <= t < key_beats[i+1]
    idx = np.searchsorted(key_beats, sample_beats, side="right") - 1
    idx = np.clip(idx, 0, n_keys - 2)

    span = key_beats[idx + 1] - key_beats[idx]
    # Guard against duplicate keyframe times producing a divide-by-zero.
    span = np.where(span <= 0.0, 1.0, span)
    u = (sample_beats - key_beats[idx]) / span
    u = np.clip(u, 0.0, 1.0)  # hold outside the authored range

    # Ease each segment with its own curve. Grouping by method keeps this
    # vectorised instead of looping per sample.
    eased = np.empty_like(u)
    for method in INTERPOLATIONS:
        seg_mask = np.array([key_interps[i] == method for i in range(n_keys - 1)])
        if not seg_mask.any():
            continue
        sample_mask = seg_mask[idx]
        if sample_mask.any():
            eased[sample_mask] = ease(u[sample_mask], method)

    a = key_values[idx]
    b = key_values[idx + 1]
    out = a + (b - a) * eased[:, None]
    return out


def head_pose_matrix(
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    roll: float = 0.0,
    pitch: float = 0.0,
    yaw: float = 0.0,
) -> np.ndarray:
    """Build a 4x4 head pose from XYZRPY. Metres and radians.

    Matches ``reachy_mini.utils.create_head_pose(..., mm=False, degrees=False)``,
    which uses ``scipy Rotation.from_euler("xyz", ...)``. Lowercase "xyz" is
    *extrinsic*, i.e. rotations about the fixed world axes applied X then Y then
    Z, so the composed matrix is ``Rz @ Ry @ Rx``.
    """
    return head_pose_matrices(
        np.array([[x, y, z, roll, pitch, yaw]], dtype=np.float64)
    )[0]


def head_pose_matrices(xyzrpy: np.ndarray) -> np.ndarray:
    """Vectorised XYZRPY -> 4x4. Input (N,6) metres/radians, output (N,4,4).

    Vectorised because the compiler builds tens of thousands of these per song
    and the agent rebuilds them at load time on a Raspberry Pi.
    """
    xyzrpy = np.asarray(xyzrpy, dtype=np.float64).reshape(-1, 6)
    n = xyzrpy.shape[0]

    roll, pitch, yaw = xyzrpy[:, 3], xyzrpy[:, 4], xyzrpy[:, 5]
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)

    out = np.zeros((n, 4, 4), dtype=np.float64)

    # Rz(yaw) @ Ry(pitch) @ Rx(roll), expanded.
    out[:, 0, 0] = cy * cp
    out[:, 0, 1] = cy * sp * sr - sy * cr
    out[:, 0, 2] = cy * sp * cr + sy * sr
    out[:, 1, 0] = sy * cp
    out[:, 1, 1] = sy * sp * sr + cy * cr
    out[:, 1, 2] = sy * sp * cr - cy * sr
    out[:, 2, 0] = -sp
    out[:, 2, 1] = cp * sr
    out[:, 2, 2] = cp * cr

    out[:, :3, 3] = xyzrpy[:, :3]
    out[:, 3, 3] = 1.0
    return out


def matrices_to_xyzrpy(poses: np.ndarray) -> np.ndarray:
    """Inverse of :func:`head_pose_matrices`. Input (N,4,4) -> output (N,6).

    Used by the clip recorder to turn captured 4x4 poses back into editable
    XYZRPY keyframes. Gimbal lock (pitch = +/-90 deg) is far outside the +/-35 deg
    head cone, so the simple branch is safe here; it is still handled.
    """
    poses = np.asarray(poses, dtype=np.float64).reshape(-1, 4, 4)
    out = np.zeros((poses.shape[0], 6), dtype=np.float64)
    out[:, :3] = poses[:, :3, 3]

    r20 = np.clip(-poses[:, 2, 0], -1.0, 1.0)
    pitch = np.arcsin(r20)
    cp = np.cos(pitch)

    locked = np.abs(cp) < 1e-8
    roll = np.where(locked, 0.0, np.arctan2(poses[:, 2, 1], poses[:, 2, 2]))
    yaw = np.where(locked, np.arctan2(-poses[:, 0, 1], poses[:, 1, 1]),
                   np.arctan2(poses[:, 1, 0], poses[:, 0, 0]))

    out[:, 3] = roll
    out[:, 4] = pitch
    out[:, 5] = yaw
    return out


def smoothstep(t: npt.ArrayLike) -> np.ndarray:
    """3t^2 - 2t^3, clamped. Used for secondary-motion gain ramps."""
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)
