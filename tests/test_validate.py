"""The safety gate must actually catch things.

Every test here corresponds to a failure mode that, on real hardware, produces
NO error: the daemon catches the exception, throttle-logs it and holds the last
valid target. If these tests do not fire, nothing else will.
"""

from dataclasses import replace

import numpy as np
import pytest

from conftest import needs_sdk
from orchestra.motion.compile import compile_clip_preview
from orchestra.motion.program import CH_ANT_L, CH_ANT_R, CH_Z, N_CHANNELS, Program
from orchestra.motion.schema import clip_from_dict
from orchestra.motion.validate import (
    SEVERITY_ERROR,
    SEVERITY_WARN,
    validate_clip,
    validate_program,
)

pytestmark = needs_sdk


def _kinds(rep, severity=None):
    return {
        v.kind for v in rep.violations
        if severity is None or v.severity == severity
    }


def _clip(**over):
    base = {
        "name": "t",
        "units": {"translation": "mm", "rotation": "deg"},
        "placement": "pattern",
        "loop_beats": 1.0,
        "tracks": {"head": [
            {"beat": 0.0, "pose": {"z": 0}, "interp": "minjerk"},
            {"beat": 0.5, "pose": {"z": -6}, "interp": "minjerk"},
            {"beat": 1.0, "pose": {"z": 0}, "interp": "minjerk"},
        ]},
    }
    base.update(over)
    return clip_from_dict(base)


def test_reasonable_clip_passes(fleet_cfg):
    res = compile_clip_preview(_clip(), bpm=112, loops=4, fps=100)
    rep = validate_program(res.program, fleet_cfg.safety, bpm=112)
    assert rep.ok, rep.summary()


def test_tempo_scaling_is_caught(fleet_cfg):
    """THE trap: a clip safe at 90 BPM can be unsafe at 200, because
    beat-relative keyframes compress in real time."""
    clip = _clip(tracks={"head": [
        {"beat": 0.0, "pose": {"z": 0, "y": 0}, "interp": "minjerk"},
        {"beat": 0.25, "pose": {"z": -14, "y": 14}, "interp": "linear"},
        {"beat": 0.5, "pose": {"z": 0, "y": 0}, "interp": "linear"},
        {"beat": 0.75, "pose": {"z": -14, "y": -14}, "interp": "linear"},
        {"beat": 1.0, "pose": {"z": 0, "y": 0}, "interp": "minjerk"},
    ]})
    slow = validate_program(
        compile_clip_preview(clip, bpm=60, loops=2, fps=100).program,
        fleet_cfg.safety, bpm=60,
    )
    fast = validate_program(
        compile_clip_preview(clip, bpm=280, loops=2, fps=100).program,
        fleet_cfg.safety, bpm=280,
    )
    assert slow.ok, "should be safe at a slow tempo"
    assert not fast.ok, "the same clip must FAIL when compressed into 280 BPM"
    assert "joint_velocity" in _kinds(fast, SEVERITY_ERROR)


def test_tilt_cone_enforced(fleet_cfg):
    clip = _clip(tracks={"head": [
        {"beat": 0.0, "pose": {"pitch": 0}, "interp": "minjerk"},
        {"beat": 0.5, "pose": {"pitch": 33}, "interp": "minjerk"},
        {"beat": 1.0, "pose": {"pitch": 0}, "interp": "minjerk"},
    ]})
    rep = validate_program(
        compile_clip_preview(clip, bpm=40, loops=1, fps=100).program,
        fleet_cfg.safety, bpm=40,
    )
    assert not rep.ok
    assert "tilt_cone" in _kinds(rep, SEVERITY_ERROR)


def test_translation_box_enforced(fleet_cfg):
    clip = _clip(tracks={"head": [
        {"beat": 0.0, "pose": {"z": 0}, "interp": "minjerk"},
        {"beat": 0.5, "pose": {"z": -60}, "interp": "minjerk"},
        {"beat": 1.0, "pose": {"z": 0}, "interp": "minjerk"},
    ]})
    rep = validate_program(
        compile_clip_preview(clip, bpm=30, loops=1, fps=100).program,
        fleet_cfg.safety, bpm=30,
    )
    assert not rep.ok
    assert "translation_out_of_box" in _kinds(rep, SEVERITY_ERROR)


def test_dwell_is_no_longer_an_error(fleet_cfg):
    """楽器を外したので dwell 検査は廃止した。

    押し付ける相手が無ければサーボはストールしない。逆に「決めポーズで一瞬
    止まる」はエアー演奏では欲しい表現なので、残しておくと誤検出になる。
    """
    clip = _clip(placement="trigger", anchors=[0.3],
                 tracks={"head": [
                     {"beat": 0.0, "pose": {"z": 0}, "interp": "minjerk"},
                     {"beat": 0.3, "pose": {"z": -6}, "interp": "linear"},
                     {"beat": 0.6, "pose": {"z": -6}, "interp": "linear"},  # 静止
                     {"beat": 1.0, "pose": {"z": 0}, "interp": "minjerk"},
                 ]})
    rep = validate_program(
        compile_clip_preview(clip, bpm=100, loops=2, fps=100).program,
        fleet_cfg.safety, bpm=100,
    )
    assert "strike_dwell" not in _kinds(rep)
    assert rep.ok, rep.summary()


def test_gesture_overlap_detected(fleet_cfg):
    """クリップの寄与は加算される。連続ジェスチャが重なると振幅が合算されて
    可動範囲を超えるので、アンカー間隔が最小値を割ったら止める。"""
    frames = np.zeros((400, N_CHANNELS))
    gap = fleet_cfg.safety.gesture_min_interval_s / 2.0
    p = Program(robot_id="x", frames=frames, fps=100,
                anchors=np.arange(0.5, 3.5, gap))
    rep = validate_program(p, fleet_cfg.safety)
    assert "gesture_overlap" in _kinds(rep, SEVERITY_ERROR)


def test_sound_window_relaxes_acceleration_only(fleet_cfg):
    """音が出る瞬間だけ加速度の警告閾値を緩める。

    以前は「打鍵の衝撃」を理由に *速度* を 1.6 倍まで許していたが、衝撃を
    吸収するビーターがもう無いので撤廃した。緩めるのは加速度だけ、しかも
    アンカー近傍だけ、しかも警告レベルだけ。中止閾値は動かさない。
    """
    # 加速度が通常上限(120)と緩和後(120*1.8=216)の**あいだ**に来るよう選んである。
    # 両方超えていたら差が出ないし、両方下回っていても差が出ない。
    tracks = {"head": [
        {"beat": 0.0, "pose": {"z": 0}, "interp": "minjerk"},
        {"beat": 0.5, "pose": {"z": -12}, "interp": "minjerk"},
        {"beat": 1.0, "pose": {"z": 0}, "interp": "minjerk"},
    ]}
    run = lambda anchors: validate_program(
        compile_clip_preview(
            _clip(placement="trigger", anchors=anchors, tracks=tracks),
            bpm=300, loops=3, fps=100,
        ).program,
        fleet_cfg.safety, bpm=300,
    )
    near = run([0.5])   # アンカーが加速度のピークに重なる
    away = run([0.0])   # 同じ動き、アンカーだけ離れている

    lo = fleet_cfg.safety.joint_accel_max
    hi = lo * fleet_cfg.safety.gesture_accel_scale
    assert lo < near.stats["joint_acc_peak"] < hi, "テストが意図した帯に入っていない"

    n_acc = lambda r: sum(v.kind == "joint_acceleration" for v in r.violations)
    assert n_acc(near) == 0 and n_acc(away) > 0, (
        "音の瞬間に重なった加速度は緩める。同じ動きでも音と無関係な位置なら緩めない"
    )
    # 速度側には免除が無いこと。まったく同じだけ出る。
    n_vel = lambda r: sum(v.kind == "joint_velocity" for v in r.violations)
    assert n_vel(near) == n_vel(away) > 0, "速度に免除を作ってはいけない"


def test_antenna_velocity_is_stricter(fleet_cfg):
    """Antennas are xc330-class at +/-0.8 N.m -- a quarter of the Stewart
    servos, and the easiest motor on the robot to destroy."""
    clip = _clip(tracks={"antennas": [
        {"beat": 0.0, "value": 5, "interp": "linear"},
        {"beat": 0.06, "value": 150, "interp": "linear"},
        {"beat": 0.5, "value": 5, "interp": "minjerk"},
        {"beat": 1.0, "value": 5, "interp": "minjerk"},
    ]})
    rep = validate_program(
        compile_clip_preview(clip, bpm=140, loops=2, fps=100).program,
        fleet_cfg.safety, bpm=140,
    )
    assert not rep.ok
    assert "antenna_velocity" in _kinds(rep, SEVERITY_ERROR)


def test_cartoon_near_anchor_is_allowed_by_default(fleet_cfg):
    """cartoon は端点を越えて外挿する（utils/interpolation.py:115）。

    物理楽器があった頃はビーターを楽器に突き刺す動きだったので無条件で禁止
    していたが、エアー演奏では overshoot こそ欲しい表現なので既定では通す。
    行き過ぎた分は並進ボックスと速度上限が受け止める。
    設定で締めたい現場のために閾値は残してある。
    """
    clip = _clip(placement="trigger", anchors=[0.5],
                 tracks={"head": [
                     {"beat": 0.0, "pose": {"z": 0}, "interp": "cartoon"},
                     {"beat": 0.5, "pose": {"z": -6}, "interp": "minjerk"},
                     {"beat": 1.0, "pose": {"z": 0}, "interp": "minjerk"},
                 ]})
    assert fleet_cfg.safety.forbid_cartoon_near_anchor_s == 0.0, (
        "fleet.yaml の既定は 0（=禁止しない）のはず"
    )
    rep = validate_clip(clip, fleet_cfg.safety, bpm=112)
    assert "cartoon_near_anchor" not in _kinds(rep, SEVERITY_ERROR)

    strict = replace(fleet_cfg.safety, forbid_cartoon_near_anchor_s=0.15)
    rep = validate_clip(clip, strict, bpm=112)
    assert "cartoon_near_anchor" in _kinds(rep, SEVERITY_ERROR), (
        "閾値を立てたら検出できること"
    )


def test_ik_unreachable_detected(fleet_cfg):
    """The most dangerous case, because on hardware it is completely silent:
    the daemon logs and holds the last valid target."""
    frames = np.zeros((60, N_CHANNELS))
    frames[:, CH_Z] = 0.35  # far outside any reachable workspace
    rep = validate_program(Program(robot_id="x", frames=frames, fps=100),
                           fleet_cfg.safety)
    assert not rep.ok
    assert {"ik_unreachable", "translation_out_of_box"} & _kinds(rep, SEVERITY_ERROR)


def _sustained(freq_hz: float, amp_rad: float, secs: float = 30.0, fps: float = 100.0):
    """アンテナだけを延々と振り続けるプログラム。

    アンテナは関節にそのまま対応するので、発熱指標を素直に作れる。
    """
    n = int(secs * fps)
    t = np.arange(n) / fps
    frames = np.zeros((n, N_CHANNELS))
    wave = amp_rad * np.sin(2 * np.pi * freq_hz * t)
    frames[:, CH_ANT_R] = wave
    frames[:, CH_ANT_L] = -wave
    return Program(robot_id="x", frames=frames, fps=fps,
                   anchors=np.arange(0.0, secs, 0.5))


def test_duty_cycle_is_reported_for_head_and_antennas(fleet_cfg):
    """この指標が**消えたら発熱保護がゼロになる**。

    このファームは温度・電流・負荷を一切報告しない（docs/EXPERIMENTS.md N-6）
    ので、構造的な仕事量の見積もりが唯一の防護になっている。統計が出ている
    こと自体をテストするのは、付け替えの過程で黙って落ちるのを防ぐため。

    両方の統計を要求するのが肝。当初ヘッドしか見ておらず、**アンテナ主体の
    パート（鈴）だけが保護対象から丸ごと外れていた**（2026-08-07 修正）。
    """
    rep = validate_program(_sustained(0.5, 0.2), fleet_cfg.safety)
    for stat in ("gesture_duty_rms", "antenna_duty_rms"):
        assert stat in rep.stats, (
            f"{stat} が消えている＝発熱保護がその軸で黙って無効化されている"
        )
    assert rep.stats["antenna_duty_rms"] < fleet_cfg.safety.antenna_duty_max_rms


def test_antenna_duty_cycle_limit(fleet_cfg):
    """アンテナを休みなく振り続ければ、1フレームずつは合法でも落とす。

    アンテナは xc330 (±0.8 N.m)、ヘッドの1/4のトルクしかない最弱の軸。
    ここが落ちなければ鈴のパートは無制限に酷使できてしまう。
    """
    rep = validate_program(_sustained(3.0, 0.5), fleet_cfg.safety)
    assert "gesture_duty_cycle" in _kinds(rep, SEVERITY_ERROR), rep.summary()
    assert any(v.channel == "antennas" for v in rep.errors)


def test_duty_cycle_counts_work_not_strike_rate(fleet_cfg):
    """指標を「アンカー数/秒」から「関節速度のRMS」に替えた理由。

    シェイカーは打点が少ないのに止まらず動き続け、ウッドブロックは打点が
    まばらでも1回が大きい。打数を数えると前者を見逃し後者を過剰に罰する。
    発熱は動いた総量で決まるので、打数が同じでも動きが激しいほうが重く出る
    ことを確認する（両者ともアンカーは 0.5s 間隔で同数）。
    """
    calm = validate_program(_sustained(0.5, 0.2), fleet_cfg.safety)
    busy = validate_program(_sustained(2.0, 0.4), fleet_cfg.safety)
    assert busy.stats["antenna_duty_rms"] > 3 * calm.stats["antenna_duty_rms"]


def test_empty_program_rejected(fleet_cfg):
    rep = validate_program(Program(robot_id="x", fps=100), fleet_cfg.safety)
    assert not rep.ok


def test_report_is_actionable(fleet_cfg):
    """A violation has to say which joint, which frame, and what to do."""
    clip = _clip(tracks={"head": [
        {"beat": 0.0, "pose": {"pitch": 0}, "interp": "minjerk"},
        {"beat": 0.5, "pose": {"pitch": 34}, "interp": "minjerk"},
        {"beat": 1.0, "pose": {"pitch": 0}, "interp": "minjerk"},
    ]})
    rep = validate_program(
        compile_clip_preview(clip, bpm=40, loops=1, fps=100).program,
        fleet_cfg.safety, bpm=40, label="demo",
    )
    v = rep.errors[0]
    assert v.channel and v.detail and v.frame >= 0
    assert "demo" in rep.summary()
