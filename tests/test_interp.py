"""Our interpolation must match the SDK's exactly.

This is not a nice-to-have. A clip previewed through ``goto_target`` (SDK maths)
and the same clip played by the agent (our maths) have to produce identical
motion, or tuning by eye on one path means nothing on the other.
"""

import numpy as np
import pytest

from conftest import needs_sdk
from orchestra.motion.interp import (
    CARTOON,
    EASE_IN_OUT,
    INTERPOLATIONS,
    LINEAR,
    MIN_JERK,
    ease,
    head_pose_matrices,
    head_pose_matrix,
    interp_track,
    matrices_to_xyzrpy,
)


@needs_sdk
@pytest.mark.parametrize("name", INTERPOLATIONS)
def test_easing_matches_sdk_exactly(name):
    from reachy_mini.utils.interpolation import (
        InterpolationTechnique,
        time_trajectory,
    )

    tech = InterpolationTechnique(name)
    ts = np.linspace(0.0, 1.0, 257)
    mine = ease(ts, name)
    theirs = np.array([time_trajectory(float(t), tech) for t in ts])
    assert np.allclose(mine, theirs, atol=0, rtol=0), (
        f"{name} diverges from the SDK by up to {np.abs(mine - theirs).max():.3e}"
    )


@needs_sdk
def test_head_pose_matches_sdk():
    from reachy_mini.utils import create_head_pose

    rng = np.random.default_rng(7)
    for _ in range(50):
        x, y, z = rng.uniform(-0.03, 0.03, 3)
        r, p, yw = rng.uniform(-0.5, 0.5, 3)
        mine = head_pose_matrix(x, y, z, r, p, yw)
        theirs = create_head_pose(x, y, z, r, p, yw, mm=False, degrees=False)
        assert np.allclose(mine, theirs, atol=1e-12)


def test_minjerk_boundary_conditions():
    """Min-jerk must have zero velocity AND acceleration at both ends -- that
    is the whole reason it is the default for a move that starts from rest.

    Checked analytically rather than by finite differences: the numerical second
    derivative at a boundary carries O(dt * y''') error, which for this curve is
    large enough to swamp the property being tested.
    """
    t = np.linspace(0, 1, 2001)
    y = ease(t, MIN_JERK)
    assert y[0] == pytest.approx(0.0)
    assert y[-1] == pytest.approx(1.0)
    assert np.all(np.diff(y) >= -1e-12), "min-jerk must be monotonic"

    #  y' = 30t^2 - 60t^3 + 30t^4 ;  y'' = 60t - 180t^2 + 120t^3
    for tt in (0.0, 1.0):
        assert 30 * tt**2 - 60 * tt**3 + 30 * tt**4 == pytest.approx(0.0, abs=1e-12)
        assert 60 * tt - 180 * tt**2 + 120 * tt**3 == pytest.approx(0.0, abs=1e-12)

    # And the numerical velocity really is near zero at the ends compared with
    # its own peak, which is the property that matters in practice.
    v = np.gradient(y, t)
    assert abs(v[0]) < 0.01 * v.max() and abs(v[-1]) < 0.01 * v.max()


def test_cartoon_overshoots_on_purpose():
    """Cartoon is a back-ease: it dips below 0 and above 1. The validator
    forbids it near a strike anchor precisely because of this."""
    y = ease(np.linspace(0, 1, 501), CARTOON)
    assert y.min() < -0.01, "cartoon should anticipate below 0"
    assert y.max() > 1.01, "cartoon should overshoot above 1"


def test_linear_and_ease_stay_in_range():
    for name in (LINEAR, EASE_IN_OUT, MIN_JERK):
        y = ease(np.linspace(0, 1, 501), name)
        assert y.min() >= -1e-9 and y.max() <= 1 + 1e-9


def test_ease_rejects_unknown():
    with pytest.raises(ValueError, match="unknown interpolation"):
        ease(0.5, "bouncy")


def test_interp_track_holds_outside_range():
    beats = np.array([1.0, 2.0])
    vals = np.array([[0.0], [10.0]])
    out = interp_track(np.array([0.0, 1.0, 1.5, 2.0, 3.0]), beats, vals,
                       [LINEAR, LINEAR])
    assert out[0, 0] == pytest.approx(0.0)   # before first key: hold
    assert out[2, 0] == pytest.approx(5.0)   # midpoint
    assert out[4, 0] == pytest.approx(10.0)  # after last key: hold


def test_interp_track_uses_left_keyframe_curve():
    """A keyframe owns the curve to its RIGHT -- the animation convention."""
    beats = np.array([0.0, 1.0, 2.0])
    vals = np.array([[0.0], [1.0], [2.0]])
    out = interp_track(np.array([0.5, 1.5]), beats, vals, [LINEAR, MIN_JERK, LINEAR])
    assert out[0, 0] == pytest.approx(0.5)                    # linear segment
    assert out[1, 0] == pytest.approx(1.0 + ease(0.5, MIN_JERK))  # minjerk segment


def test_xyzrpy_roundtrip():
    rng = np.random.default_rng(3)
    v = np.column_stack([
        rng.uniform(-0.02, 0.02, (40, 3)).reshape(40, 3),
        rng.uniform(-0.4, 0.4, (40, 3)).reshape(40, 3),
    ])
    back = matrices_to_xyzrpy(head_pose_matrices(v))
    assert np.allclose(v, back, atol=1e-9)
