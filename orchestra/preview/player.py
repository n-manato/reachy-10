"""Play a Program on ONE robot from the PC, for iteration.

This is the Phase-1 workhorse: author a clip, see it move, adjust, repeat. It
deliberately uses the SDK's own player (``ReachyMini.play_move``) rather than the
agent, for two reasons. It needs no deployment step, so the edit-run loop stays
tight; and it exercises the SDK's real interpolation path, so what you see is
what the SDK would do.

It is **not** how a show runs. Streaming from the PC is fine for one robot and
falls apart at nine -- that is what the on-robot agent exists for. Use
``orchestra show`` for anything involving more than one robot.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ..motion.program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_X,
    CH_YAW,
    Program,
)


def _move_class():
    """Build the adapter lazily so this module imports without the SDK."""
    from reachy_mini.motion.move import Move

    class ProgramMove(Move):
        """Adapts a compiled Program to the SDK's ``Move`` interface.

        The SDK calls ``evaluate(t)`` at ``play_frequency`` with t guaranteed in
        [0, duration]. We index the frame table rather than re-interpolating:
        the frames are already dense at 100 Hz, and re-interpolating would mean
        the preview and the agent could disagree about what a clip looks like.
        """

        def __init__(self, program: Program, sound: Path | None = None):
            self._p = program
            self._heads = program.head_matrices()
            self._sound = sound

        @property
        def duration(self) -> float:
            # Shaved slightly so the SDK's final tick can never index past the
            # table -- the same end-of-clip hazard that RecordedMove has.
            return max(self._p.duration - 1e-3, 1e-3)

        @property
        def sound_path(self) -> Path | None:
            return self._sound

        def evaluate(self, t: float):
            i = self._p.frame_index_at(t)
            row = self._p.frames[i]
            return (
                self._heads[i],
                np.array([row[CH_ANT_R], row[CH_ANT_L]]),
                float(row[CH_BODY_YAW]),
            )

    return ProgramMove


def connect(host: str, port: int = 8000, media: bool = False, timeout: float = 10.0):
    """Open a network connection to one robot.

    ``connection_mode="network"`` is mandatory, not stylistic: the SDK's default
    ``"auto"`` tries **localhost first** (reachy_mini.py:439-444) and would
    silently bind to a local daemon if one happened to be running on this PC,
    leaving you tuning a clip against the wrong machine.
    """
    from reachy_mini import ReachyMini

    return ReachyMini(
        host=host,
        port=port,
        connection_mode="network",
        media_backend="default" if media else "no_media",
        timeout=timeout,
    )


def play_program(
    host: str,
    program: Program,
    port: int = 8000,
    fps: float | None = None,
    goto_start_duration: float = 1.5,
    sound: Path | None = None,
    park_after: bool = True,
    verbose: bool = True,
) -> dict:
    """Arm, move to the start pose, play, then park.

    Always parks explicitly. The SDK's context manager does *not* disable motors
    on exit -- it only closes media and drops the socket (reachy_mini.py:181-186)
    -- so a robot left after a naive `with` block sits energised at its last
    commanded pose.
    """
    ProgramMove = _move_class()
    reachy = connect(host, port, media=sound is not None)
    stats: dict = {}
    try:
        # Arm BEFORE commanding a pose. On SDK 1.9.0 `enable_motors()` pins all
        # targets to the present pose before enabling torque, so the older
        # `set_target(X); enable_motors()` idiom no longer drives to X.
        reachy.enable_motors()
        time.sleep(0.2)

        row = program.frames[0]
        head0 = program.head_matrices()[0]
        reachy.goto_target(
            head=head0,
            antennas=[float(row[CH_ANT_R]), float(row[CH_ANT_L])],
            body_yaw=float(row[CH_BODY_YAW]),
            duration=goto_start_duration,
        )

        move = ProgramMove(program, sound=sound)
        t0 = time.monotonic()
        if verbose:
            print(f"  playing {program.duration:.2f}s on {host} ...")
        reachy.play_move(
            move, play_frequency=fps or program.fps, sound=sound is not None
        )
        stats["wall_s"] = round(time.monotonic() - t0, 3)
        stats["expected_s"] = round(program.duration, 3)
        stats["overrun_ms"] = round(
            (stats["wall_s"] - stats["expected_s"]) * 1000, 1
        )
        return stats
    finally:
        try:
            if park_after:
                reachy.goto_target(
                    head=np.eye(4),
                    antennas=[float(row[CH_ANT_R]), float(row[CH_ANT_L])],
                    body_yaw=0.0,
                    duration=1.0,
                )
                reachy.disable_motors()
        except Exception:
            pass
        for fn in (lambda: reachy.media.close(), lambda: reachy.client.disconnect()):
            try:
                fn()
            except Exception:
                pass


def record_clip(
    host: str,
    seconds: float,
    port: int = 8000,
    countdown: int = 3,
) -> list[dict]:
    """Capture hand-puppeteered motion under gravity compensation.

    By far the fastest way to author expressive motion: switch the robot limp,
    move the head by hand, and keep the result. Typing joint angles cannot
    compete for anything that has to look alive.

    Returns the SDK's raw record list; :func:`orchestra.motion.record.to_clip`
    turns it into editable keyframes.
    """
    reachy = connect(host, port, media=False)
    try:
        reachy.enable_gravity_compensation()
        for i in range(countdown, 0, -1):
            print(f"  recording in {i}...")
            time.sleep(1.0)
        print(f"  RECORDING for {seconds:.1f}s -- move the head by hand now")
        reachy.start_recording()
        time.sleep(seconds)
        data = reachy.stop_recording()
        print(f"  captured {len(data or [])} samples")
        return data or []
    finally:
        try:
            reachy.disable_gravity_compensation()
            reachy.disable_motors()
        except Exception:
            pass
        try:
            reachy.client.disconnect()
        except Exception:
            pass
