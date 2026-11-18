import math

import numpy as np
import pytest

from orchestra.motion.compile import (
    DEFAULT_NEUTRAL_ANTENNAS,
    Placement,
    compile_clip_preview,
    compile_placements,
)
from orchestra.motion.program import CH_ANT_L, CH_ANT_R, CH_BODY_YAW, CH_Z
from orchestra.motion.schema import ClipError, clip_from_dict
from orchestra.music.tempo import TempoMap

BASIC = {
    "name": "t",
    "units": {"translation": "mm", "rotation": "deg"},
    "placement": "pattern",
    "loop_beats": 1.0,
    "tracks": {
        "head": [
            {"beat": 0.0, "pose": {"z": 0}, "interp": "minjerk"},
            {"beat": 0.5, "pose": {"z": -10}, "interp": "minjerk"},
            {"beat": 1.0, "pose": {"z": 0}, "interp": "minjerk"},
        ]
    },
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_units_normalise_to_si():
    clip = clip_from_dict(BASIC)
    head = clip.track("head")
    assert head.values[1, 2] == pytest.approx(-0.010)  # -10 mm -> -0.01 m


def test_rotation_units_normalise():
    # `trigger` rather than `pattern`: a looping clip whose ends differ is
    # rejected (correctly) by the discontinuity check, which is not what this
    # test is about.
    d = {**BASIC, "placement": "trigger", "tracks": {"head": [
        {"beat": 0.0, "pose": {"pitch": 0}},
        {"beat": 1.0, "pose": {"pitch": 90}},
    ]}}
    clip = clip_from_dict(d)
    assert clip.track("head").values[1, 4] == pytest.approx(math.pi / 2)


def test_scalar_antenna_value_is_symmetric_flare():
    d = {**BASIC, "tracks": {"antennas": [{"beat": 0.0, "value": 20}]}}
    clip = clip_from_dict(d)
    v = clip.track("antennas").values[0]
    assert v[0] == pytest.approx(-math.radians(20))
    assert v[1] == pytest.approx(math.radians(20))


def test_omitted_axes_are_neutral():
    clip = clip_from_dict(BASIC)
    head = clip.track("head")
    assert np.allclose(head.values[:, [0, 1, 3, 4, 5]], 0.0)


def test_unknown_axis_rejected():
    d = {**BASIC, "tracks": {"head": [{"beat": 0, "pose": {"wiggle": 1}}]}}
    with pytest.raises(ClipError, match="unknown axis"):
        clip_from_dict(d)


def test_unknown_track_rejected():
    d = {**BASIC, "tracks": {"tail": [{"beat": 0, "value": 1}]}}
    with pytest.raises(ClipError, match="unknown track"):
        clip_from_dict(d)


def test_duplicate_keyframe_time_rejected():
    d = {**BASIC, "tracks": {"head": [
        {"beat": 0.0, "pose": {"z": 0}},
        {"beat": 0.0, "pose": {"z": -5}},
    ]}}
    with pytest.raises(ClipError, match="duplicate keyframe"):
        clip_from_dict(d)


def test_unknown_interp_rejected():
    d = {**BASIC, "tracks": {"head": [{"beat": 0, "pose": {"z": 0}, "interp": "boing"}]}}
    with pytest.raises(ClipError, match="unknown interp"):
        clip_from_dict(d)


def test_discontinuous_loop_rejected():
    """A looping clip whose ends differ jumps once per bar. Better to fail at
    author time than to glitch audibly for the whole song."""
    d = {**BASIC, "tracks": {"head": [
        {"beat": 0.0, "pose": {"z": 0}},
        {"beat": 1.0, "pose": {"z": -8}},
    ]}}
    with pytest.raises(ClipError, match="jump at every loop boundary"):
        clip_from_dict(d)


def test_scaled_shrinks_amplitude_not_timing():
    """`scaled` は `clip fit` / `song fit` が安全域に追い込むときの唯一の手。

    タイミングを触らないことが重要で、テンポを落とすと曲が変わるし、
    キーフレームを個別に触ると動きの形が崩れる。
    """
    clip = clip_from_dict({**BASIC, "placement": "trigger", "anchors": [0.3]})
    small = clip.scaled(head=0.5)
    assert small.anchors == clip.anchors
    assert small.loop_beats == clip.loop_beats
    assert np.allclose(small.track("head").beats, clip.track("head").beats)
    assert np.allclose(small.track("head").values, clip.track("head").values * 0.5)
    assert np.allclose(clip.track("head").values, clip_from_dict(
        {**BASIC, "placement": "trigger", "anchors": [0.3]}
    ).track("head").values), "元のクリップを破壊してはいけない"


def test_scaled_treats_antennas_as_absolute_angles():
    """アンテナは中立からの**差分**を縮める。

    絶対角なので素朴に全体へ掛けると、振り幅ではなく耳の開き具合そのものが
    変わってしまう ―― 静かにさせたつもりが、ずっと閉じた顔になる。
    """
    clip = clip_from_dict({**BASIC, "tracks": {"antennas": [
        {"beat": 0.0, "value": 20, "interp": "minjerk"},
        {"beat": 0.5, "value": 40, "interp": "minjerk"},
        {"beat": 1.0, "value": 20, "interp": "minjerk"},
    ]}})
    # 左右で符号が逆なので、必ず耳ごと（列ごと）に見ること。
    before = clip.track("antennas").values
    after = clip.scaled(antennas=0.5).track("antennas").values
    assert after.shape == before.shape
    assert np.allclose(after.mean(axis=0), before.mean(axis=0)), "中心位置は動かさない"
    assert np.allclose(np.ptp(after, axis=0), np.ptp(before, axis=0) * 0.5), \
        "振り幅だけ半分に"


def test_scaled_head_and_antennas_are_independent():
    """xc330 は sts3215 の 1/4 のトルクしかないので、頭は余裕でもアンテナだけ
    超える（逆も）ことが普通に起きる。鈴のパートが実際にそれだった。"""
    clip = clip_from_dict({**BASIC, "tracks": {
        "head": [{"beat": 0.0, "pose": {"z": 0}}, {"beat": 0.5, "pose": {"z": -8}},
                 {"beat": 1.0, "pose": {"z": 0}}],
        "antennas": [{"beat": 0.0, "value": 10}, {"beat": 0.5, "value": 50},
                     {"beat": 1.0, "value": 10}],
    }})
    out = clip.scaled(head=1.0, antennas=0.5)
    assert np.allclose(out.track("head").values, clip.track("head").values)
    assert np.ptp(out.track("antennas").values) < np.ptp(clip.track("antennas").values)


def test_roundtrip_save_load(tmp_path):
    from orchestra.motion.schema import load_clip, save_clip

    clip = clip_from_dict({**BASIC, "placement": "trigger", "anchors": [0.5]})
    p = save_clip(clip, tmp_path / "x.clip.json")
    back = load_clip(p)
    assert back.name == clip.name
    assert back.anchors == clip.anchors
    assert back.placement == clip.placement
    assert np.allclose(back.track("head").values, clip.track("head").values, atol=1e-6)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def test_neutral_antennas_are_the_baseline():
    """A clip with no antenna track must leave antennas at the SDK's neutral,
    which is NOT zero (-10/+10 deg reduces shake at vertical)."""
    clip = clip_from_dict(BASIC)
    res = compile_clip_preview(clip, bpm=120, loops=1, fps=100)
    assert res.program.frames[0, CH_ANT_R] == pytest.approx(DEFAULT_NEUTRAL_ANTENNAS[0])
    assert res.program.frames[0, CH_ANT_L] == pytest.approx(DEFAULT_NEUTRAL_ANTENNAS[1])


def test_uniform_time_grid():
    res = compile_clip_preview(clip_from_dict(BASIC), bpm=112, loops=3, fps=100)
    t = res.program.times()
    assert np.allclose(np.diff(t), 0.01)


def test_tempo_scales_duration_not_shape():
    """Same clip, different tempo: peak displacement identical, duration halved."""
    clip = clip_from_dict(BASIC)
    slow = compile_clip_preview(clip, bpm=60, loops=2, fps=100).program
    fast = compile_clip_preview(clip, bpm=120, loops=2, fps=100).program
    assert fast.duration < slow.duration
    assert np.abs(slow.frames[:, CH_Z]).max() == pytest.approx(
        np.abs(fast.frames[:, CH_Z]).max(), rel=0.02
    )


def test_placements_are_additive():
    clip = clip_from_dict(BASIC)
    tempo = TempoMap.constant(120)
    one = compile_placements(
        [Placement(clip, 0.0, end_beat=4.0)], tempo, 2.0, fps=100
    ).program
    two = compile_placements(
        [Placement(clip, 0.0, end_beat=4.0), Placement(clip, 0.0, end_beat=4.0)],
        tempo, 2.0, fps=100,
    ).program
    depth1 = np.abs(one.frames[:, CH_Z]).max()
    depth2 = np.abs(two.frames[:, CH_Z]).max()
    assert depth2 == pytest.approx(2 * depth1, rel=0.02)


def test_trigger_anchor_aligns_to_the_note():
    """The whole point of `anchors`: the clip is placed so its CONTACT lands on
    the beat, meaning the windup happens before it."""
    clip = clip_from_dict({**BASIC, "placement": "trigger", "anchors": [0.5]})
    tempo = TempoMap.constant(60)  # 1 beat == 1 second
    res = compile_placements(
        [Placement(clip, start_beat=2.0 - clip.primary_anchor)],
        tempo, duration_s=6.0, fps=100,
    )
    assert len(res.program.anchors) == 1
    assert res.program.anchors[0] == pytest.approx(2.0, abs=0.02)


def test_lead_in_allows_negative_beats():
    """A note on beat 0 needs its windup BEFORE beat 0. The lead-in is what
    makes that representable; clamping there would truncate every downbeat."""
    clip = clip_from_dict({**BASIC, "placement": "trigger", "anchors": [0.5]})
    tempo = TempoMap.constant(60)
    res = compile_placements(
        [Placement(clip, start_beat=-0.5)],
        tempo, duration_s=3.0, fps=100, lead_in_s=1.0,
    )
    # The strike should land exactly at lead_in (t=1.0s), not be pushed later.
    assert res.program.anchors[0] == pytest.approx(1.0, abs=0.02)
    # And motion must exist during the lead-in -- that is the windup.
    assert np.abs(res.program.frames[:100, CH_Z]).max() > 1e-4


def test_pattern_loops_phase_locked_to_its_start():
    clip = clip_from_dict(BASIC)
    tempo = TempoMap.constant(60)
    res = compile_placements(
        [Placement(clip, start_beat=1.0, end_beat=5.0)], tempo, 6.0, fps=100
    )
    z = res.program.frames[:, CH_Z]
    # Nothing before the placement starts.
    assert np.abs(z[:95]).max() < 1e-9
    # Deepest point of the first loop is half a beat after the start.
    assert np.argmin(z[100:200]) == pytest.approx(50, abs=4)


def test_every_anchor_reaches_the_program():
    """アンカーは「音が出る瞬間」の唯一の記録で、下流の全部がこれに乗っている。

    副次モーションの消音窓、発熱保護の duty、ジェスチャ重なり判定、そして
    音側の `make_stems` まで同じ配列から出ている。物理楽器があった頃は
    「接触したアンカー」だけを別に持って一部の検査をそちらに掛けていたが、
    楽器を外してもサーボの発熱は変わらないので、**分けると保護が黙って
    消える**。だから区別を廃して1本にした。ここではループ型・単発型の
    両方の音が漏れなく出てくることを見る。
    """
    groove = clip_from_dict({**BASIC, "anchors": [0.0, 0.5]})
    strike = clip_from_dict({**BASIC, "placement": "trigger", "anchors": [0.5]})
    tempo = TempoMap.constant(60)
    res = compile_placements(
        [Placement(groove, 0.0, end_beat=4.0), Placement(strike, 1.0)],
        tempo, 6.0, fps=100,
    )
    anchors = res.program.anchors
    # ループ側: 4拍 / 60BPM を 0.5拍間隔 -> 0.0,0.5,1.0 ... 3.5s の8点
    # 単発側: 拍1 のアンカー 0.5拍 -> 1.5s
    assert len(anchors) == 9
    assert np.isclose(anchors, 1.5).sum() == 2, (
        "同じ時刻に重なる音も、別々の発音として1つずつ残ること"
    )
    assert (np.diff(anchors) >= 0).all(), "時刻順に並んでいること"
