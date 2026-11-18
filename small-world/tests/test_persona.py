"""ペルソナ（持ち姿・フレーズの弧）。

守りたいのは2つだけ:

  1. **予算内に収まること。** 構えは可動域を大きく使うのが目的なので、
     副次モーションと打点クリップが上に乗るぶんを引いた残りに収まって
     いないと `song build` が落ちる。落ちてから気付くより、ここで落とす。
  2. **9役が互いに違うこと。** 全部が「なんとなく違う」だけだと、そもそも
     このモジュールを作った意味が無い。差が付いていることを数値で見る。

「見て分かるか」自体はここでは判定できない ―― 実機を部屋の向こうから
見るしかない。ここが守るのは、そこへ行く前に落ちないことだけ。
"""

from __future__ import annotations

from math import degrees, radians

import numpy as np
import pytest

from orchestra.motion import persona as P
from orchestra.motion.program import (
    CH_ANT_L, CH_ANT_R, CH_BODY_YAW, CH_PITCH, CH_ROLL, CH_X, CH_Y, CH_YAW, CH_Z,
)

# 運用上限（config/fleet.yaml safety:）
BOX_M = {CH_X: 0.025, CH_Y: 0.025, CH_Z: 0.022}
TILT_DEG = 25.0
BODY_YAW_DEG = 150.0
ANTENNA_DEG = 150.0
REL_YAW_DEG = 45.0

# 上に乗るものの取り分。ペルソナはこれを引いた残りしか使えない。
#   副次: groove z2.2 / breathing z5.0 / phrase_sway y3.0 / attention x4.0,yaw8°
#   打点: 最大 z2.8 y1.8 x1.0 pitch4.2° roll6.9° antenna30°
RESERVE_M = {CH_X: 0.005, CH_Y: 0.005, CH_Z: 0.010}
RESERVE_TILT_DEG = 12.0
RESERVE_BODY_YAW_DEG = 5.0
RESERVE_ANTENNA_DEG = 40.0
RESERVE_REL_YAW_DEG = 10.0


def test_every_role_has_a_persona():
    """song.yaml が使う9つの声色すべてに定義があること。"""
    used = ["conductor", "celesta", "vibraphone", "glockenspiel", "marimba",
            "woodblock", "tambourine", "shaker", "jingle_bells"]
    for name in used:
        assert P.get(name) is not P.DEFAULT, f"{name} のペルソナが無い"


def test_aliases_resolve():
    assert P.get("bells") is P.PERSONAS["jingle_bells"]
    assert P.get("GLOCKEN") is P.PERSONAS["glockenspiel"]
    assert P.get("nonsense") is P.DEFAULT      # 知らない名前でも落とさない


@pytest.mark.parametrize("name", sorted(P.PERSONAS))
def test_persona_fits_the_budget(name):
    """構え + 弧 が、他のものが乗る余地を残して収まっていること。"""
    p = P.PERSONAS[name]
    env = p.envelope()

    for ch, limit in BOX_M.items():
        room = limit - RESERVE_M[ch]
        assert env[ch] <= room + 1e-9, (
            f"{name}: 並進 ch{ch} が {env[ch]*1000:.1f}mm、"
            f"使えるのは {room*1000:.1f}mm まで（上限 {limit*1000:.0f}mm から"
            f"副次と打点のぶん {RESERVE_M[ch]*1000:.0f}mm を引いた残り）")

    # チルトは roll と pitch の合成で効く（validate は m[2,2] から出す）
    tilt = degrees(np.arccos(np.clip(
        np.cos(env[CH_ROLL]) * np.cos(env[CH_PITCH]), -1.0, 1.0)))
    assert tilt <= TILT_DEG - RESERVE_TILT_DEG + 1e-9, (
        f"{name}: 合成チルト {tilt:.1f}° が予算 "
        f"{TILT_DEG - RESERVE_TILT_DEG:.0f}° を超えている")

    assert degrees(env[CH_BODY_YAW]) <= BODY_YAW_DEG - RESERVE_BODY_YAW_DEG
    for ch in (CH_ANT_R, CH_ANT_L):
        absolute = abs(p.stance.antennas_deg[0 if ch == CH_ANT_R else 1])
        assert absolute <= ANTENNA_DEG - RESERVE_ANTENNA_DEG + 1e-9, (
            f"{name}: アンテナ {absolute:.0f}° が予算超過")


@pytest.mark.parametrize("name", sorted(P.PERSONAS))
def test_head_turns_with_the_body(name):
    """体を回すなら頭も回す。でないと rel_yaw(45°) を使い切る。

    頭の姿勢は world 座標なので、body_yaw だけ 40° 回して head yaw を 0 の
    ままにすると、相対 yaw が -40° になって上限にぶつかる。
    """
    p = P.PERSONAS[name]
    row = p.stance.row()
    rel = degrees(row[CH_YAW] - row[CH_BODY_YAW])
    assert abs(rel) <= REL_YAW_DEG - RESERVE_REL_YAW_DEG, (
        f"{name}: 頭と体の向きの差が {rel:.0f}°。"
        f"attention レイヤの 8° が乗るので {REL_YAW_DEG - RESERVE_REL_YAW_DEG:.0f}° まで")
    assert abs(rel) == pytest.approx(abs(p.stance.face_deg), abs=1e-6)


def test_the_nine_are_mutually_distinguishable():
    """どの2台も、少なくとも1つの軸ではっきり違うこと。

    全員が同じ軸で大小を競うと「なんとなく違う」にしかならない。ここでは
    「遠目に分かる差」の下限を軸ごとに決めて、全ペアがどれか1つは満たすと
    いう形で見る。
    """
    # 常時の向きは全役 0（正面）にした ―― 固定で横を向いていると
    # 「変な方向を向いている台」に見えるため。だから見分けは**動きのほう**が
    # 担う: 体を振る大きさ・その周期・拍に対する位相。
    salient = {
        "swing": (lambda p: p.sweep.beat_yaw_deg, 4.0),        # 体を振る大きさ
        "rate": (lambda p: p.sweep.beat_period, 0.9),          # 何拍で1往復するか
        "offbeat": (lambda p: p.sweep.beat_phase, 0.2),        # 表か裏か
        "pitch": (lambda p: p.stance.pitch_deg, 6.0),          # 顎の角度
        "ears": (lambda p: sum(abs(a) for a in p.stance.antennas_deg) / 2, 20.0),
    }
    names = sorted(P.PERSONAS)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pa, pb = P.PERSONAS[a], P.PERSONAS[b]
            diffs = {k: abs(f(pa) - f(pb)) for k, (f, _) in salient.items()}
            shapes_differ = pa.sweep.shape != pb.sweep.shape
            ok = shapes_differ or any(
                diffs[k] >= thr for k, (_, thr) in salient.items())
            assert ok, (
                f"{a} と {b} が見分けられない: 差は "
                + ", ".join(f"{k} {v:.0f}" for k, v in diffs.items())
                + f"、弧はどちらも {pa.sweep.shape}")


def test_ears_alone_separate_the_bell_part():
    """鈴は耳が主役。他のどれよりもはっきり開いていること。"""
    bells = P.PERSONAS["jingle_bells"].stance.antennas_deg
    spread = sum(abs(a) for a in bells) / 2
    for name, p in P.PERSONAS.items():
        if name == "jingle_bells":
            continue
        other = sum(abs(a) for a in p.stance.antennas_deg) / 2
        assert spread > other + 15.0, f"鈴({spread:.0f}°) が {name}({other:.0f}°) と近い"


# ---------------------------------------------------------------------------
# 動力学 —— ここが「大きくても無料」の根拠
# ---------------------------------------------------------------------------


def _beats(n=1200, fps=100.0, bpm=80.0, lead_in_s=2.5):
    t = np.arange(n) / fps
    return (t - lead_in_s) * bpm / 60.0


def test_a_pure_stance_costs_no_velocity():
    """弧も拍回転も持たない構えは、完全な静止であること。

    静止しているから発熱制限に効かない ―― それがこの設計の土台なので、
    本当に静止していることを確かめる。
    """
    p = P.Persona(stance=P.PERSONAS["woodblock"].stance, sweep=P.Sweep())
    beats = _beats()
    d = P.posture_delta(p, beats, beats_per_bar=4.0)
    settled = d[beats > 2.0]
    assert np.allclose(settled, settled[0], atol=1e-12), "構えが動いている"


def test_beat_rotation_stays_on_the_body_axes():
    """拍に乗った回転は body_yaw と head yaw にだけ出ること。

    頭の姿勢は world 座標なので、体だけ回すと rel_yaw(45°) を食う。
    必ず同じ量だけ頭も回っていること。
    """
    p = P.PERSONAS["marimba"]
    assert p.sweep.beat_yaw_deg > 0
    beats = _beats(n=2000)
    d = P.posture_delta(p, beats, beats_per_bar=4.0)
    s = d[beats > 2.0]
    assert np.allclose(s[:, CH_BODY_YAW], s[:, CH_YAW], atol=1e-9), \
        "体と頭の回転が食い違っている"


@pytest.mark.parametrize("name", sorted(P.PERSONAS))
def test_sustained_motion_stays_slow(name):
    """構えに乗る動きは、打点に比べて桁で遅いこと。

    2種類あって速さが違うので、別々に見る:

      * フレーズの弧 ―― 1〜4小節（3〜12秒）周期。ほぼ静止に近い
      * 拍に乗った胴体の回転 ―― 1〜4拍（0.75〜3秒）周期。こちらは速い

    上限（関節速度 4.0 rad/s）に対してどれだけ食うかが問題なので、
    姿勢チャンネルの変化率で代理する。**この数字だけでは足りない** ――
    構えは機構の動作点を移してヤコビアンを変えるので、最終判断は
    `song build` の margins で行うこと（モジュール冒頭の注記を参照）。
    """
    p = P.PERSONAS[name]
    beats = _beats(n=3000)
    d = P.posture_delta(p, beats, beats_per_bar=4.0)
    settled = d[beats > 2.0]
    if len(settled) < 3:
        pytest.skip("フレームが足りない")
    v = np.abs(np.gradient(settled, 1 / 100.0, axis=0)).max()
    # 拍回転を持つ役は当然速い。それでも上限の1/3は超えないこと。
    ceiling = 1.3 if p.sweep.beat_yaw_deg else 0.4
    assert v < ceiling, f"{name}: 持続的な動きが速すぎる（{v:.3f} /s）"


def test_ramp_starts_from_neutral():
    """プログラム先頭は中立に近いこと。

    `op_arm` は frames[0] へ 2秒かけて動く。そこが既に完成した構えでも
    構わないが、途中で急に構え始めるのは避けたい。
    """
    beats = _beats()
    d = P.posture_delta(P.PERSONAS["marimba"], beats, beats_per_bar=4.0)
    assert np.abs(d[0]).max() < 1e-6, "先頭が中立でない"
    assert np.abs(d[beats > 2.0][0]).max() > 1e-3, "構えに入っていない"


def test_apply_posture_leaves_the_original_alone():
    from orchestra.motion.program import N_CHANNELS, Program

    beats = _beats(n=300)
    prog = Program(robot_id="r05", frames=np.zeros((300, N_CHANNELS)), fps=100.0)
    out = P.apply_posture(prog, P.PERSONAS["marimba"], beats, 4.0)
    assert np.allclose(prog.frames, 0.0), "元のプログラムが書き換わっている"
    assert not np.allclose(out.frames, 0.0)
    assert "persona" in out.meta


def test_the_greeting_turns_to_the_audience_and_back_in_its_slot():
    """礼は客席に向かってする。戻りが遅れると最初の合図とぶつかる。"""
    t = np.arange(0.0, 6.0, 0.01)
    out = P.greeting(t, 1.0, 3.5, turn_deg=180.0, turn_s=1.6)

    assert degrees(out[0, CH_BODY_YAW]) == pytest.approx(0.0)
    assert np.allclose(out[:, CH_BODY_YAW], out[:, CH_YAW]), "頭も一緒に回す"

    bowing = np.abs(out[:, CH_PITCH]) > radians(1.0)
    assert bowing.any()
    assert np.allclose(np.degrees(out[bowing, CH_BODY_YAW]), 180.0, atol=1.0), \
        "回りきってから頭を下げ、頭を上げてから戻る"
    assert np.all(out[t >= 4.5] == 0.0), "1台目の合図までに戻っている"

    speed = np.abs(np.diff(out[:, CH_BODY_YAW])) / 0.01
    assert speed.max() < 4.0, "関節速度の上限"


def test_the_greeting_stays_put_without_a_turn():
    t = np.arange(0.0, 6.0, 0.01)
    out = P.greeting(t, 1.0, 3.5)
    assert np.all(out[:, CH_BODY_YAW] == 0.0)
    assert np.all(out[t < 1.0, CH_PITCH] == 0.0)
