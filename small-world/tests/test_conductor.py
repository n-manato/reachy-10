"""指揮者は手続きで生成されるので、クリップと違って人の目で見直されない。

ここが唯一の歯止め。実際、2026-08-07 に「小節線でジェスチャが1フレーム分だけ
裂ける」不連続がこの経路に潜んでいて、**テンポが100fpsを割り切るときだけ**
表に出るという性質のせいで長く見逃されていた。
"""

import numpy as np
import pytest

from conftest import needs_sdk
from orchestra.conductor.patterns import ConductorStyle, build_conductor_program
from orchestra.motion.program import CH_PITCH, CH_Y, CH_Z
from orchestra.motion.validate import SEVERITY_ERROR, validate_program
from orchestra.music.tempo import Meter, TempoMap
from orchestra.music.timeline import Timeline

pytestmark = needs_sdk

# 100 fps を割り切るテンポ（1拍がちょうど整数フレーム）と、割り切らないもの。
# 割り切るほうが危険側 -- 拍の境界が必ずフレームに乗るので、境界にある段差が
# 均されずそのまま出る。
FRAME_ALIGNED = [60.0, 80.0, 100.0, 120.0, 150.0]
FRAME_OFFSET = [72.0, 90.0, 112.0, 132.0]


def _program(bpm: float, fleet_cfg, bars: int = 16):
    tl = Timeline(tempo=TempoMap.constant(bpm), meter=Meter(4, 4), events=[],
                  total_beats=bars * 4.0, name="t")
    tl.build_phrases(bars_per_phrase=4)
    return build_conductor_program(
        tl, fps=fleet_cfg.fps, lead_in_s=2.5,
        neutral_antennas=fleet_cfg.neutral_antennas,
        style=ConductorStyle(), tail_s=2.5,
    )


@pytest.mark.parametrize("bpm", FRAME_ALIGNED + FRAME_OFFSET)
def test_conductor_passes_at_every_tempo(bpm, fleet_cfg):
    rep = validate_program(_program(bpm, fleet_cfg), fleet_cfg.safety, bpm=bpm)
    assert rep.ok, rep.summary()


def test_downbeat_emphasis_is_continuous(fleet_cfg):
    """拍頭を大きくする倍率は、**補間したあとに掛けてはいけない**。

    位置は拍をまたいで連続している（落下カーブの終点が次の拍の始点と同じ）が、
    そこへ拍ごとの倍率を後から掛けると、隣り合う2フレームで振幅が 1.0 -> 1.45 に
    跳ぶ。制御点の側に掛けてから補間すれば連続のまま。

    80 BPM は 1拍 = ちょうど75フレームなので、この段差が最も素直に出る条件。
    """
    p = _program(80.0, fleet_cfg)
    f = p.frames
    for ch, name, tol in ((CH_Z, "z", 1.5e-3), (CH_Y, "y", 1.5e-3),
                          (CH_PITCH, "pitch", np.radians(2.5))):
        step = np.abs(np.diff(f[:, ch]))
        k = int(np.argmax(step))
        assert step[k] < tol, (
            f"{name} が1フレームで {step[k]:.5f} 動いている（frame {k}, "
            f"t={k / p.fps:.3f}s）。小節線で段差になっている疑い"
        )


def test_conductor_velocity_tracks_tempo_monotonically(fleet_cfg):
    """速いほど速い、それだけ。どこかのテンポで突然跳ねるなら段差がある。

    テンポとフレームレートの噛み合わせで安全余裕が上下するようでは、
    「このテンポなら安全」という保証が成り立たない。
    """
    peaks = []
    for bpm in sorted(FRAME_ALIGNED + FRAME_OFFSET):
        rep = validate_program(_program(bpm, fleet_cfg), fleet_cfg.safety, bpm=bpm)
        peaks.append((bpm, rep.stats["joint_vel_peak"]))
    vals = [v for _, v in peaks]
    assert all(a < b for a, b in zip(vals, vals[1:])), (
        f"速度ピークがテンポに対して単調でない: {peaks}"
    )


def test_prep_beat_starts_from_rest(fleet_cfg):
    """予備拍は静止から立ち上がること。

    以前ここも別式で書かれていて T0 で 21.7 rad/s の跳ねを作っていた。
    予備拍は「同じ拍パターンの −1 拍目」であって、別の動きではない。
    """
    p = _program(112.0, fleet_cfg)
    head = p.frames[:, [CH_Y, CH_Z, CH_PITCH]]
    assert np.abs(head[0]).max() < 1e-6, "1フレーム目は中立から始まること"

    # チャンネルごとに単位が違う（y/z はメートル、pitch はラジアン）ので、
    # まとめて max を取っても意味を持たない。関節速度で見る。
    from orchestra.motion.validate import solve_joints

    joints, bad = solve_joints(p.frames)
    assert not bad.any(), "予備拍のあいだに IK が解けない姿勢がある"
    vel = np.abs(np.gradient(joints, 1.0 / p.fps, axis=0))
    lead_frames = int(2.5 * p.fps)
    peak = float(vel[:lead_frames].max())
    assert peak < fleet_cfg.safety.joint_velocity_max, (
        f"予備拍のあいだに {peak:.2f} rad/s 出ている"
        f"（上限 {fleet_cfg.safety.joint_velocity_max}）"
    )


def test_conductor_marks_every_beat(fleet_cfg):
    """アンカーは拍そのもの。数が合わなければ下流の消音も duty も狂う。"""
    bars = 16
    p = _program(112.0, fleet_cfg, bars=bars)
    # 予備拍ぶんを含むので、少なくとも小節数×拍数はあること
    assert len(p.anchors) >= bars * 4
    gaps = np.diff(p.anchors)
    assert gaps.min() > 0, "アンカーが時刻順でない"
    spb = 60.0 / 112.0
    assert np.allclose(gaps, spb, atol=0.02), "拍が等間隔でない"
