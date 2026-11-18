"""Offline inspection: joint traces, velocity headroom, strike anatomy.

The point of these plots is to make the *margin* visible. A clip that passes
validation with 2% headroom is one tempo bump away from failing, and a number in
a report does not convey that the way a trace against a limit line does.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..config import JOINT_NAMES, SafetyLimits
from ..motion.program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_PITCH,
    CH_ROLL,
    CH_X,
    CH_Y,
    CH_YAW,
    CH_Z,
    Program,
)
from ..motion.validate import solve_joints


def plot_program(
    program: Program,
    limits: SafetyLimits,
    out: str | Path | None = None,
    title: str = "",
    show: bool = False,
):
    """Four stacked panels: pose, joints, joint velocity, antennas."""
    try:
        import matplotlib

        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "plotting needs matplotlib (pip install 'reachy-orchestra[plot]')"
        ) from e

    t = program.times()
    f = program.frames
    joints, bad = solve_joints(f)
    dt = 1.0 / program.fps
    vel = np.gradient(joints, dt, axis=0) if program.n_frames > 2 else np.zeros_like(joints)

    fig, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=True)
    fig.suptitle(title or f"{program.robot_id} / {program.part} / {program.instrument or ''}")

    # -- 1. commanded pose ---------------------------------------------
    ax = axes[0]
    ax.plot(t, f[:, CH_X] * 1000, label="x (mm)")
    ax.plot(t, f[:, CH_Y] * 1000, label="y (mm)")
    ax.plot(t, f[:, CH_Z] * 1000, label="z (mm)")
    ax.plot(t, np.degrees(f[:, CH_ROLL]), label="roll (deg)", ls="--")
    ax.plot(t, np.degrees(f[:, CH_PITCH]), label="pitch (deg)", ls="--")
    ax.plot(t, np.degrees(f[:, CH_BODY_YAW]), label="body_yaw (deg)", ls=":")
    ax.set_ylabel("commanded pose")
    ax.legend(ncol=6, fontsize=8)
    ax.grid(alpha=0.3)

    # -- 2. joint positions vs limits -----------------------------------
    ax = axes[1]
    for j, name in enumerate(JOINT_NAMES):
        ax.plot(t, np.degrees(joints[:, j]), lw=1, label=name)
        lo, hi = limits.joint_range(name)
        ax.axhline(np.degrees(lo), color="r", lw=0.4, alpha=0.25)
        ax.axhline(np.degrees(hi), color="r", lw=0.4, alpha=0.25)
    if bad.any():
        # Regions where IK has no solution. On hardware these would be silently
        # swallowed by the daemon and freeze the robot at its last valid target.
        ax.fill_between(t, -180, 180, where=bad, color="red", alpha=0.25,
                        label="IK UNREACHABLE")
    ax.set_ylabel("joint angle (deg)")
    ax.legend(ncol=8, fontsize=7)
    ax.grid(alpha=0.3)

    # -- 3. joint velocity vs the ceiling -------------------------------
    ax = axes[2]
    for j, name in enumerate(JOINT_NAMES):
        ax.plot(t, vel[:, j], lw=1, label=name)
    lim = limits.joint_velocity_max
    ax.axhline(lim, color="r", ls="--", lw=1, label=f"limit {lim:.1f} rad/s")
    ax.axhline(-lim, color="r", ls="--", lw=1)
    peak = float(np.abs(vel).max()) if vel.size else 0.0
    ax.set_ylabel("joint velocity (rad/s)")
    ax.set_title(
        f"peak {peak:.2f} rad/s -- {100 * peak / lim:.0f}% of ceiling "
        f"(headroom {lim - peak:+.2f})",
        fontsize=9,
    )
    ax.legend(ncol=8, fontsize=7)
    ax.grid(alpha=0.3)

    # -- 4. antennas + strike anchors -----------------------------------
    ax = axes[3]
    ax.plot(t, np.degrees(f[:, CH_ANT_R]), label="antenna R (deg)")
    ax.plot(t, np.degrees(f[:, CH_ANT_L]), label="antenna L (deg)")
    for i, a in enumerate(program.anchors):
        ax.axvline(a, color="k", lw=0.7, alpha=0.5,
                   label="strike anchor" if i == 0 else None)
    ax.set_ylabel("antennas (deg)")
    ax.set_xlabel("time (s)")
    ax.legend(ncol=3, fontsize=8)
    ax.grid(alpha=0.3)

    fig.tight_layout()
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=110)
    if show:
        plt.show()
    else:
        plt.close(fig)
    return out


def plot_gesture_detail(
    program: Program,
    limits: SafetyLimits,
    anchor_index: int = 0,
    window_s: float = 0.6,
    out: str | Path | None = None,
    show: bool = False,
):
    """音が鳴る瞬間まわりを拡大する: 軌道と速度。

    物理楽器が無いので「接触点で止まる＝ストール」という危険は消えたが、
    ジェスチャが音の瞬間に合っているかを目で確かめるのには依然として要る。
    アンカー（音が出る瞬間）に軌道の見せ場が来ているかを見る。
    """
    try:
        import matplotlib

        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("plotting needs matplotlib") from e

    if not len(program.anchors):
        raise ValueError("this program has no anchors (no sounding moments)")

    a = float(program.anchors[min(anchor_index, len(program.anchors) - 1)])
    t = program.times()
    m = (t >= a - window_s / 2) & (t <= a + window_s / 2)
    if not m.any():
        raise ValueError("anchor lies outside the program")

    joints, _ = solve_joints(program.frames)
    vel = np.gradient(joints, 1.0 / program.fps, axis=0)
    speed = np.linalg.norm(vel, axis=1)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    fig.suptitle(f"strike #{anchor_index} @ t={a:.3f}s -- {program.robot_id}")

    ax1.plot(t[m], program.frames[m, CH_Z] * 1000, label="head z (mm)")
    ax1.plot(t[m], np.degrees(program.frames[m, CH_PITCH]), label="pitch (deg)")
    ax1.axvline(a, color="k", ls="--", label="strike (anchor)")
    ax1.set_ylabel("depth")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)

    ax2.plot(t[m], speed[m], color="tab:red", label="joint speed |v| (rad/s)")
    ax2.axvline(a, color="k", ls="--")
    at_anchor = float(speed[program.frame_index_at(a)])
    ax2.set_title(f"joint velocity at the strike: {at_anchor:.3f} rad/s", fontsize=9)
    ax2.set_ylabel("speed")
    ax2.set_xlabel("time (s)")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=110)
    if show:
        plt.show()
    else:
        plt.close(fig)
    return out
