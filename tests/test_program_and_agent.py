"""The agent carries its own copy of the Program format so it can be a single
deployable file. That duplication is only safe if the two stay in agreement --
which is what these tests enforce.
"""

import numpy as np
import pytest

from orchestra.motion.program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_X,
    CH_YAW,
    N_CHANNELS,
    AudioSpec,
    Program,
    concat,
)


def _program(n=250, fps=100.0) -> Program:
    rng = np.random.default_rng(11)
    frames = np.zeros((n, N_CHANNELS))
    t = np.arange(n) / fps
    frames[:, 2] = 0.006 * np.sin(2 * np.pi * 1.5 * t)
    frames[:, 4] = 0.10 * np.sin(2 * np.pi * 0.8 * t)
    frames[:, CH_BODY_YAW] = 0.05 * np.sin(2 * np.pi * 0.4 * t)
    frames[:, CH_ANT_R] = -0.1745
    frames[:, CH_ANT_L] = 0.1745
    anchors = np.array([0.5, 1.0, 1.5])
    return Program(
        robot_id="r07", song="s", part="p", instrument="shaker", fps=fps,
        frames=frames, anchors=anchors,
        audio=AudioSpec("x.wav", lead_ms=42.0),
    )


def test_roundtrip_preserves_everything(tmp_path):
    p = _program()
    f = p.save(tmp_path / "a.program.json.gz")
    back = Program.load(f)
    assert back.robot_id == p.robot_id
    assert back.fps == p.fps
    assert back.audio.lead_ms == 42.0
    assert np.allclose(back.frames, p.frames, atol=1e-6)
    assert np.allclose(back.anchors, p.anchors)


def test_gzip_is_actually_smaller(tmp_path):
    p = _program(n=3000)
    gz = p.save(tmp_path / "a.program.json.gz")
    raw = p.save(tmp_path / "a.program.json")
    assert gz.stat().st_size < raw.stat().st_size * 0.5


def test_agent_loader_agrees_with_pc(tmp_path):
    """The load-bearing duplication check."""
    import orchestra_agent

    p = _program()
    f = p.save(tmp_path / "a.program.json.gz")
    ag = orchestra_agent.Program.load(f)

    assert ag.n_frames == p.n_frames
    assert ag.fps == p.fps
    assert ag.audio_lead_ms == 42.0
    assert ag.duration == pytest.approx(p.duration)
    assert np.allclose(ag.frames, p.frames, atol=1e-6)


def test_agent_head_matrices_match_pc(tmp_path):
    """Two separate assertions, because they can fail for different reasons.

    First that the inlined maths is identical given identical input -- that is
    the duplication risk. Second that the file round trip (frames are rounded to
    6 dp on save) stays far below anything the hardware could resolve.
    """
    import orchestra_agent

    from orchestra.motion.interp import head_pose_matrices

    p = _program()
    f = p.save(tmp_path / "a.program.json.gz")
    ag = orchestra_agent.Program.load(f)

    same_input = head_pose_matrices(ag.frames[:, CH_X : CH_YAW + 1])
    assert np.allclose(ag.heads, same_input, atol=0, rtol=0), (
        "the agent's inlined pose maths has drifted from the PC's"
    )

    # 1e-6 rad is ~0.2 millidegree; 1e-6 m is a micron. Both are orders of
    # magnitude below the servos' resolution.
    assert np.allclose(ag.heads, p.head_matrices(), atol=1e-5)


def test_agent_channel_layout_matches():
    import orchestra_agent

    from orchestra.motion import program as pc

    for name in ("CH_X", "CH_Y", "CH_Z", "CH_ROLL", "CH_PITCH", "CH_YAW",
                 "CH_BODY_YAW", "CH_ANT_R", "CH_ANT_L", "N_CHANNELS"):
        assert getattr(orchestra_agent, name) == getattr(pc, name), (
            f"{name} differs between the agent and the PC -- frames would be "
            "interpreted with the wrong channel order"
        )


def test_agent_minjerk_matches_pc():
    import orchestra_agent

    from orchestra.motion.interp import MIN_JERK, ease

    for u in np.linspace(-0.2, 1.2, 41):
        assert orchestra_agent.minjerk(float(u)) == pytest.approx(
            float(ease(np.clip(u, 0, 1), MIN_JERK))
        )


def test_frame_index_is_clamped_and_absolute():
    p = _program(n=100, fps=100.0)
    assert p.frame_index_at(-5.0) == 0
    assert p.frame_index_at(1e6) == 99
    assert p.frame_index_at(0.50) == 50


def test_recorded_move_export_shape():
    """The fallback path feeds this straight into the SDK's RecordedMove."""
    p = _program(n=120)
    rm = p.to_recorded_move()
    assert set(rm) == {"description", "time", "set_target_data"}
    assert len(rm["time"]) == len(rm["set_target_data"])
    assert len(rm["time"]) > p.n_frames, "trailing pad frames should be present"
    assert np.all(np.diff(rm["time"]) > 0), "timestamps must be strictly increasing"
    frame = rm["set_target_data"][0]
    assert np.array(frame["head"]).shape == (4, 4)
    assert len(frame["antennas"]) == 2


def test_concat_offsets_anchors():
    a, b = _program(n=100), _program(n=100)
    c = concat([a, b])
    assert c.n_frames == 200
    assert len(c.anchors) == len(a.anchors) + len(b.anchors)
    assert c.anchors.max() > a.anchors.max()


def test_rejects_wrong_channel_count(tmp_path):
    d = _program().to_dict()
    d["frames"] = [[0.0] * 5 for _ in range(10)]
    with pytest.raises(ValueError, match="channels"):
        Program.from_dict(d)


def test_rejects_newer_format(tmp_path):
    d = _program().to_dict()
    d["version"] = 99
    with pytest.raises(ValueError, match="newer"):
        Program.from_dict(d)
