"""The safety gate. Nothing reaches a robot without passing through here.

Why this module carries so much weight: **the Reachy Mini stack enforces
nothing**. There is no velocity limit, no acceleration limit, no jerk limit and
no workspace clamp anywhere in the Python stack -- the URDF's ``velocity="8"`` is
metadata only. The single failure mode is IK returning NaN, and the daemon
catches that exception, throttle-logs it and carries on::

    # daemon/backend/robot/backend.py:240-243
    except ValueError as e:
        log_throttling.by_time(self.logger, interval=0.5).warning(f"IK error: {e}")

So an unreachable or too-fast pose produces a **silent freeze at the last valid
target**, not an error you can detect from the client. This module converts that
into a loud, early, local failure on the PC.

Validation is always run at the *target tempo*, because beat-relative keyframes
compress in real time: a clip that is safe at 90 BPM can exceed the velocity
ceiling at 140 BPM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..config import JOINT_NAMES, SafetyLimits
from .interp import CARTOON, head_pose_matrices
from .program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_X,
    CH_Y,
    CH_YAW,
    CH_Z,
    Program,
)
from .schema import Clip

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warning"


@dataclass
class Violation:
    kind: str
    severity: str
    channel: str
    frame: int
    time: float
    value: float
    limit: float
    detail: str

    def __str__(self) -> str:
        return (
            f"[{self.severity}] {self.kind} on {self.channel} "
            f"@ frame {self.frame} (t={self.time:.3f}s): {self.detail}"
        )


@dataclass
class ValidationReport:
    ok: bool = True
    violations: list[Violation] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)
    n_frames: int = 0
    fps: float = 100.0
    bpm: float | None = None
    label: str = ""

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == SEVERITY_WARN]

    def add(self, v: Violation) -> None:
        self.violations.append(v)
        if v.severity == SEVERITY_ERROR:
            self.ok = False

    def worst(self, n: int = 10) -> list[Violation]:
        """Most severe first, then by how far over the limit they are."""
        def key(v: Violation) -> tuple:
            over = abs(v.value) / v.limit if v.limit else 0.0
            return (0 if v.severity == SEVERITY_ERROR else 1, -over)

        return sorted(self.violations, key=key)[:n]

    def summary(self, verbose: bool = False) -> str:
        head = f"{'PASS' if self.ok else 'FAIL'}"
        if self.label:
            head += f"  {self.label}"
        if self.bpm:
            head += f"  @ {self.bpm:g} BPM"
        head += f"  ({self.n_frames} frames, {self.n_frames / self.fps:.2f}s)"

        lines = [head]
        if self.violations:
            n_err, n_warn = len(self.errors), len(self.warnings)
            lines.append(f"  {n_err} error(s), {n_warn} warning(s)")
            shown = self.violations if verbose else self.worst(10)
            for v in shown:
                lines.append(f"    {v}")
            if not verbose and len(self.violations) > len(shown):
                lines.append(
                    f"    ... and {len(self.violations) - len(shown)} more "
                    "(use --verbose)"
                )

        if self.stats:
            lines.append("  margins:")
            for k in sorted(self.stats):
                lines.append(f"    {k:<28} {self.stats[k]:.4g}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "n_errors": len(self.errors),
            "n_warnings": len(self.warnings),
            "stats": self.stats,
            "bpm": self.bpm,
            "violations": [
                {
                    "kind": v.kind,
                    "severity": v.severity,
                    "channel": v.channel,
                    "frame": v.frame,
                    "time": round(v.time, 4),
                    "value": round(v.value, 6),
                    "limit": round(v.limit, 6),
                    "detail": v.detail,
                }
                for v in self.worst(50)
            ],
        }


# ---------------------------------------------------------------------------
# Kinematics access
# ---------------------------------------------------------------------------

_KIN_CACHE: dict[bool, object] = {}


def get_kinematics(automatic_body_yaw: bool = True):
    """The SDK's analytical IK engine, cached (construction reads asset JSON).

    ``automatic_body_yaw=True`` matches the robot's own default
    (``ReachyMini(automatic_body_yaw=True)``), so the joints we validate are the
    joints the robot will actually produce -- including any clamping it applies.
    """
    if automatic_body_yaw not in _KIN_CACHE:
        try:
            from reachy_mini.kinematics.analytical_kinematics import AnalyticalKinematics
        except ImportError as e:  # pragma: no cover - depends on install
            raise RuntimeError(
                "the Reachy Mini SDK is required for validation "
                "(pip install reachy-mini). Clip compilation works without it, "
                "but nothing may be deployed unvalidated."
            ) from e
        _KIN_CACHE[automatic_body_yaw] = AnalyticalKinematics(
            automatic_body_yaw=automatic_body_yaw
        )
    return _KIN_CACHE[automatic_body_yaw]


def solve_joints(
    frames: np.ndarray, automatic_body_yaw: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """Run IK over every frame.

    Returns ``(joints, bad)`` where joints is (N,7) = [body_yaw, stewart_1..6]
    and bad is an (N,) bool mask of frames whose IK failed or returned NaN --
    exactly the frames that would silently freeze the robot.
    """
    kin = get_kinematics(automatic_body_yaw)
    poses = head_pose_matrices(frames[:, CH_X : CH_YAW + 1])
    n = frames.shape[0]
    joints = np.full((n, 7), np.nan, dtype=np.float64)
    bad = np.zeros(n, dtype=bool)

    for i in range(n):
        try:
            j = kin.ik(poses[i], float(frames[i, CH_BODY_YAW]))
        except Exception:
            bad[i] = True
            continue
        j = np.asarray(j, dtype=np.float64)
        if j.shape != (7,) or not np.all(np.isfinite(j)):
            bad[i] = True
            continue
        joints[i] = j

    return joints, bad


# ---------------------------------------------------------------------------
# Program validation
# ---------------------------------------------------------------------------


def _first_runs(mask: np.ndarray, limit: int = 6) -> list[tuple[int, int]]:
    """Contiguous True runs as (start, length). Keeps reports readable when a
    single authoring mistake trips thousands of consecutive frames."""
    if not mask.any():
        return []
    idx = np.flatnonzero(mask)
    splits = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate([[idx[0]], idx[splits + 1]])
    ends = np.concatenate([idx[splits], [idx[-1]]])
    return [(int(s), int(e - s + 1)) for s, e in zip(starts, ends)][:limit]


def validate_program(
    program: Program,
    limits: SafetyLimits,
    bpm: float | None = None,
    label: str = "",
    check_strikes: bool = True,
) -> ValidationReport:
    """Full frame-level validation. This is the gate."""
    rep = ValidationReport(
        n_frames=program.n_frames, fps=program.fps, bpm=bpm, label=label or program.robot_id
    )
    if program.n_frames == 0:
        rep.add(
            Violation("empty", SEVERITY_ERROR, "program", 0, 0.0, 0, 0,
                      "program has no frames")
        )
        return rep

    f = program.frames
    t = program.times()
    dt = 1.0 / program.fps

    def report_runs(mask, kind, channel, values, limit, fmt):
        for start, length in _first_runs(mask):
            seg = slice(start, start + length)
            k = start + int(np.argmax(np.abs(values[seg])))
            rep.add(
                Violation(
                    kind, SEVERITY_ERROR, channel, k, float(t[k]),
                    float(values[k]), float(limit),
                    fmt(values[k], limit, length),
                )
            )

    # -- 1. Translation box ------------------------------------------------
    for ch, name in ((CH_X, "x"), (CH_Y, "y"), (CH_Z, "z")):
        lo, hi = limits.translation_m[name]
        vals = f[:, ch]
        bad = (vals < lo) | (vals > hi)
        report_runs(
            bad, "translation_out_of_box", f"head.{name}", vals, max(abs(lo), abs(hi)),
            lambda v, l, n: (
                f"head {name}={v * 1000:.1f}mm outside "
                f"[{lo * 1000:.0f}, {hi * 1000:.0f}]mm for {n} frame(s)"
            ),
        )
    rep.stats["head_x_peak_mm"] = float(np.abs(f[:, CH_X]).max() * 1000)
    rep.stats["head_y_peak_mm"] = float(np.abs(f[:, CH_Y]).max() * 1000)
    rep.stats["head_z_peak_mm"] = float(np.abs(f[:, CH_Z]).max() * 1000)

    # -- 2. Tilt cone ------------------------------------------------------
    # Angle between the head's own Z axis and world Z. Computed from the matrix
    # rather than sqrt(roll^2+pitch^2) so it stays exact at large angles.
    poses = head_pose_matrices(f[:, CH_X : CH_YAW + 1])
    cos_tilt = np.clip(poses[:, 2, 2], -1.0, 1.0)
    tilt = np.arccos(cos_tilt)
    bad = tilt > limits.tilt_cone_rad
    report_runs(
        bad, "tilt_cone", "head.tilt", tilt, limits.tilt_cone_rad,
        lambda v, l, n: (
            f"head tilt {np.degrees(v):.1f}deg exceeds operating limit "
            f"{np.degrees(l):.1f}deg (hardware hard limit "
            f"{np.degrees(limits.tilt_cone_hard_rad):.0f}deg) for {n} frame(s)"
        ),
    )
    rep.stats["tilt_peak_deg"] = float(np.degrees(tilt.max()))

    # -- 3. Body yaw -------------------------------------------------------
    by = f[:, CH_BODY_YAW]
    bad = np.abs(by) > limits.body_yaw_rad
    report_runs(
        bad, "body_yaw", "body_yaw", by, limits.body_yaw_rad,
        lambda v, l, n: (
            f"body_yaw {np.degrees(v):.1f}deg exceeds {np.degrees(l):.0f}deg "
            f"for {n} frame(s)"
        ),
    )
    rep.stats["body_yaw_peak_deg"] = float(np.degrees(np.abs(by).max()))

    # -- 4. Antennas -------------------------------------------------------
    for ch, name in ((CH_ANT_R, "antenna_r"), (CH_ANT_L, "antenna_l")):
        vals = f[:, ch]
        bad = np.abs(vals) > limits.antenna_rad
        report_runs(
            bad, "antenna_range", name, vals, limits.antenna_rad,
            lambda v, l, n: (
                f"{name} {np.degrees(v):.1f}deg exceeds {np.degrees(l):.0f}deg "
                f"for {n} frame(s)"
            ),
        )

    ant_vel = np.zeros((program.n_frames, 2))
    if program.n_frames > 1:
        for k, (ch, name) in enumerate(((CH_ANT_R, "antenna_r"), (CH_ANT_L, "antenna_l"))):
            vel = np.gradient(f[:, ch], dt)
            ant_vel[:, k] = vel
            bad = np.abs(vel) > limits.antenna_velocity_max
            report_runs(
                bad, "antenna_velocity", name, vel, limits.antenna_velocity_max,
                lambda v, l, n: (
                    f"{name} velocity {abs(v):.1f} rad/s exceeds {l:.1f} rad/s "
                    f"for {n} frame(s). Antennas are xc330-class (+/-0.8 N.m), a "
                    "quarter of the Stewart servos -- slow this down or reduce "
                    "mounted mass."
                ),
            )
            rep.stats[f"{name}_peak_vel"] = float(np.abs(vel).max())

    # -- 5. Inverse kinematics --------------------------------------------
    joints, bad_ik = solve_joints(f)

    if bad_ik.any():
        for start, length in _first_runs(bad_ik):
            rep.add(
                Violation(
                    "ik_unreachable", SEVERITY_ERROR, "head", start, float(t[start]),
                    float(length), 0.0,
                    f"IK has no solution for {length} frame(s) starting here. "
                    "On hardware the daemon would swallow this and hold the last "
                    "valid target -- a silent freeze. Reduce the pose amplitude.",
                )
            )
        # Everything below needs joints; interpolate across the gaps so one bad
        # region does not mask every other problem in the clip.
        good = ~bad_ik
        if good.any():
            for j in range(7):
                joints[bad_ik, j] = np.interp(
                    np.flatnonzero(bad_ik), np.flatnonzero(good), joints[good, j]
                )
        else:
            return rep

    # -- 6. Silent body-yaw clamping --------------------------------------
    # With automatic_body_yaw (the robot's default) the solver silently rewrites
    # body yaw to respect max_body_yaw / max_relative_yaw. If it did, the robot
    # will not do what the clip says.
    clamp_err = np.abs(joints[:, 0] - by)
    bad = clamp_err > np.radians(1.0)
    report_runs(
        bad, "body_yaw_clamped", "body_yaw", clamp_err, np.radians(1.0),
        lambda v, l, n: (
            f"IK rewrote body_yaw by {np.degrees(v):.1f}deg for {n} frame(s). "
            "The robot will not reproduce the authored yaw -- reduce head yaw or "
            "body yaw so the safe-IK clamp never engages."
        ),
    )

    # -- 7. Head yaw relative to body -------------------------------------
    rel = np.arctan2(
        np.sin(f[:, CH_YAW] - joints[:, 0]), np.cos(f[:, CH_YAW] - joints[:, 0])
    )
    bad = np.abs(rel) > limits.rel_yaw_rad
    report_runs(
        bad, "relative_yaw", "head.yaw", rel, limits.rel_yaw_rad,
        lambda v, l, n: (
            f"head yaw relative to body {np.degrees(v):.1f}deg exceeds "
            f"{np.degrees(l):.0f}deg (engines disagree on the hard limit: "
            "analytical 65deg, placo 55deg) for {n} frame(s)".replace("{n}", str(n))
        ),
    )
    rep.stats["rel_yaw_peak_deg"] = float(np.degrees(np.abs(rel).max()))

    # -- 8. Per-joint position limits --------------------------------------
    for j, name in enumerate(JOINT_NAMES):
        lo, hi = limits.joint_range(name)
        vals = joints[:, j]
        bad = (vals < lo) | (vals > hi)
        report_runs(
            bad, "joint_limit", name, vals, max(abs(lo), abs(hi)),
            lambda v, l, n, _lo=lo, _hi=hi, _nm=name: (
                f"{_nm}={np.degrees(v):.1f}deg outside inset range "
                f"[{np.degrees(_lo):.1f}, {np.degrees(_hi):.1f}]deg for {n} frame(s)"
            ),
        )

    # -- 9. 関節の速度と加速度 --------------------------------------------
    #
    # 物理楽器を廃止したので「打鍵の衝撃」による速度免除は無くなった。
    # ビーターも接触相手も無い以上、上限を一時的に上げる根拠が無い。
    # そのぶん 4.0 rad/s が全区間で効く唯一の速度ゲートになる。
    #
    # 加速度だけは「音が鳴る瞬間」の周りで緩める。エアー演奏では overshoot
    # （cartoon 補間）が表現として欲しく、そこが最も加速度が立つため。
    # 速度は緩めない ―― 持続的な過駆動がサーボを焼くのはそのままなので。
    if program.n_frames > 2:
        vel = np.gradient(joints, dt, axis=0)
        acc = np.gradient(vel, dt, axis=0)

        near_sound = _sound_mask(t, program.anchors, limits.gesture_window_s)
        accel_warn_ceiling = np.where(
            near_sound, limits.joint_accel_max * limits.gesture_accel_scale,
            limits.joint_accel_max,
        )
        accel_abort_ceiling = np.where(
            near_sound, limits.joint_accel_abort * limits.gesture_accel_scale,
            limits.joint_accel_abort,
        )

        for j, name in enumerate(JOINT_NAMES):
            v = vel[:, j]
            bad = np.abs(v) > limits.joint_velocity_max
            report_runs(
                bad, "joint_velocity", name, v, limits.joint_velocity_max,
                lambda val, l, n, _nm=name: (
                    f"{_nm} reaches {abs(val):.2f} rad/s, over the {l:.2f} rad/s limit "
                    f"for {n} frame(s). The SDK enforces nothing here, so the "
                    "servo just falls silently behind -- but sustained "
                    "over-driving means sustained peak current. Reduce the "
                    "amplitude or slow the tempo."
                ),
            )

            a = acc[:, j]
            over_warn = np.abs(a) > accel_warn_ceiling
            over_err = np.abs(a) > accel_abort_ceiling

            for start, length in _first_runs(over_err):
                k = start + int(np.argmax(np.abs(a[start : start + length])))
                rep.add(
                    Violation(
                        "joint_acceleration", SEVERITY_ERROR, name, k, float(t[k]),
                        float(a[k]), float(accel_abort_ceiling[k]),
                        f"{name} reaches {abs(a[k]):.0f} rad/s^2, over the abort ceiling "
                        f"of {accel_abort_ceiling[k]:.0f} for {length} frame(s). "
                        "Soften the turnaround.",
                    )
                )
            for start, length in _first_runs(over_warn & ~over_err):
                k = start + int(np.argmax(np.abs(a[start : start + length])))
                rep.add(
                    Violation(
                        "joint_acceleration", SEVERITY_WARN, name, k, float(t[k]),
                        float(a[k]), float(accel_warn_ceiling[k]),
                        f"{name} reaches {abs(a[k]):.0f} rad/s^2, over "
                        f"{accel_warn_ceiling[k]:.0f} rad/s^2 for {length} frame(s). "
                        "That ceiling comes from servo torque over inertia "
                        "(3.23 N.m / 0.027 kg.m^2); past it the servo lags "
                        "instead of following. Brief is fine, sustained is not.",
                    )
                )

        rep.stats["joint_vel_peak"] = float(np.abs(vel).max())
        rep.stats["joint_vel_headroom"] = float(
            limits.joint_velocity_max - np.abs(vel).max()
        )
        rep.stats["joint_acc_peak"] = float(np.abs(acc).max())

        # -- 10. ジェスチャ保護 --------------------------------------------
        # ★ contact ではなく anchors 基準。物理楽器が無くなっても
        #   サーボの発熱は変わらないので、接触の有無で分岐させると
        #   発熱保護が無言で消える。
        if check_strikes and len(program.anchors):
            _check_gestures(rep, program, joints, vel, ant_vel, limits, t)

    return rep


def _sound_mask(t: np.ndarray, anchors: np.ndarray, window_s: float) -> np.ndarray:
    """音が鳴る瞬間の周辺フレーム。

    物理楽器があった頃は「打鍵の衝撃」を表していたが、今は「見せ場のキメ」を
    表す。加速度が立つのは同じ場所なので、窓の使い方だけが変わった。
    """
    mask = np.zeros_like(t, dtype=bool)
    if window_s <= 0:
        return mask
    for a in np.atleast_1d(anchors):
        mask |= np.abs(t - a) <= window_s
    return mask


def _check_gestures(
    rep: ValidationReport,
    program: Program,
    joints: np.ndarray,
    vel: np.ndarray,
    ant_vel: np.ndarray,
    limits: SafetyLimits,
    t: np.ndarray,
) -> None:
    """音を出す瞬間まわりの検査。

    物理楽器を廃止したので **dwell（接触点で止まり続けてストールする）検査は
    削除した** ―― 押し付ける相手が無いのでストールし得ないし、「キメで一瞬
    止まる」という演技を誤検出してしまう。

    残したのは2つ。どちらも楽器の有無と無関係にサーボへ効く。
    """
    anchors = np.asarray(program.anchors)

    # 1) 連続するジェスチャが重なっていないか。
    #    クリップの寄与は加算される（compile.py の acc += delta）ので、前の動きが
    #    終わる前に次が始まると振幅が合算され、並進ボックスを突き抜ける。
    if len(anchors) > 1:
        gaps = np.diff(anchors)
        bad = np.flatnonzero(gaps < limits.gesture_min_interval_s)
        for k in bad[:4]:
            rep.add(
                Violation(
                    "gesture_overlap", SEVERITY_ERROR, "head",
                    program.frame_index_at(anchors[k]), float(anchors[k]),
                    float(gaps[k]), limits.gesture_min_interval_s,
                    f"only {gaps[k] * 1000:.0f} ms between strikes, under the "
                    f"{limits.gesture_min_interval_s * 1000:.0f} ms minimum. "
                    "Clip contributions are summed, so overlapping strikes "
                    "add up and leave the workspace. Use a shorter clip.",
                )
            )
        rep.stats["min_gesture_gap_ms"] = float(gaps.min() * 1000)

    # 2) 持続的な仕事量。★これが唯一の発熱保護★
    #    このファームは温度・電流・負荷を一切報告しない（実測確認済み）ので
    #    実行時の監視は不可能。楽器を外してもサーボの負荷は変わらない。
    #
    #    指標は**関節速度の実効値(RMS)**。当初はアンカー数/秒で数えていたが、
    #    それは打鍵という離散的な衝撃を想定した指標で、シェイカーのように
    #    小さく連続的に動く動作を正しく評価できない ―― 打点は多いが1回あたりの
    #    仕事は小さく、逆にウッドブロックは打点が少なくても1回が大きい。
    #    発熱は動いた量で決まるので RMS が正しい。
    #    ★ヘッドとアンテナは別々に見る★ 混ぜて1つの数にすると意味が読めない。
    #    sts3215 (±3.23 N.m) と xc330 (±0.8 N.m) は別クラスのモーターで、
    #    同じ rad/s でも熱的な意味が違う。しかもアンテナ主体のパート（鈴）は
    #    ヘッドがほぼ静止するので、ヘッドだけ見ていると **最も弱いモーターを
    #    最も酷使するパートが、保護対象から丸ごと外れる**。
    win = limits.gesture_duty_window_s
    if win <= 0 or len(t) < 5:
        return

    fps = 1.0 / (t[1] - t[0])
    w = max(2, int(win * fps))

    def worst_rms(speed: np.ndarray) -> tuple[float, float]:
        """`win` 秒の移動窓で最も重い区間の実効値と、その時刻。"""
        if len(speed) <= w:
            return float(np.sqrt(np.mean(speed**2))), float(t[0])
        # 移動平均を累積和で。長い曲でも一瞬で済む。
        csum = np.concatenate([[0.0], np.cumsum(speed**2)])
        windows = (csum[w:] - csum[:-w]) / w
        k = int(np.argmax(windows))
        return float(np.sqrt(windows[k])), float(t[k])

    for stat, channel, speeds, ceiling, why in (
        ("gesture_duty_rms", "head", vel, limits.gesture_duty_max_rms,
         "6x sts3215 plus body_yaw"),
        ("antenna_duty_rms", "antennas", ant_vel, limits.antenna_duty_max_rms,
         "2x xc330 -- a quarter of the head's torque, the "
          "easiest axis on this robot to destroy"),
    ):
        if ceiling <= 0 or not speeds.size:
            continue
        rms, worst_at = worst_rms(np.linalg.norm(speeds, axis=1))
        rep.stats[stat] = rms
        if rms > ceiling:
            rep.add(
                Violation(
                    "gesture_duty_cycle", SEVERITY_ERROR, channel,
                    program.frame_index_at(worst_at), worst_at, rms, ceiling,
                    f"RMS joint velocity for {channel} over a {win:g}s window is "
                    f"{rms:.2f} rad/s, over the {ceiling:.2f} ceiling ({why}). "
                    "This firmware reports no motor temperature, so sustained "
                    "work is the only thermal protection there is. Reduce the "
                    "amplitude, or thin out the motion.",
                )
            )


# ---------------------------------------------------------------------------
# Clip-level validation
# ---------------------------------------------------------------------------


def validate_clip(clip: Clip, limits: SafetyLimits, bpm: float) -> ValidationReport:
    """Checks that need the authored keyframes, not the sampled frames.

    Run alongside :func:`validate_program`; ``orchestra clip check`` does both.
    """
    rep = ValidationReport(label=f"clip {clip.name}", bpm=bpm)
    spb = 60.0 / bpm

    # cartoon は端点を越えて外挿する（utils/interpolation.py:115）。
    # 物理楽器があった頃はビーターが楽器を突き抜けるので無条件禁止だったが、
    # エアー演奏では**むしろ欲しい誇張**なので既定では禁止しない。
    # 突き抜ける先が無いぶん、守るのは並進ボックスと速度上限の仕事になった。
    # `forbid_cartoon_near_anchor_s > 0` にすれば従来どおり止められる。
    if clip.anchors and limits.forbid_cartoon_near_anchor_s > 0:
        for tr in clip.tracks.values():
            for i, interp in enumerate(tr.interps[:-1]):
                if interp != CARTOON:
                    continue
                seg_start_s = float(tr.beats[i]) * spb
                seg_end_s = float(tr.beats[i + 1]) * spb
                for a in clip.anchors:
                    a_s = a * spb
                    near = (
                        seg_start_s - limits.forbid_cartoon_near_anchor_s
                        <= a_s
                        <= seg_end_s + limits.forbid_cartoon_near_anchor_s
                    )
                    if near:
                        rep.add(
                            Violation(
                                "cartoon_near_anchor", SEVERITY_ERROR, tr.name, i,
                                seg_start_s, 0.0, 0.0,
                                f"track {tr.name!r} uses cartoon interpolation over beat "
                                f"{tr.beats[i]:g}, crossing the strike at beat {a:g}. "
                                "The configuration forbids this.",
                            )
                        )
                        break

    # An anchor with no keyframe near it usually means the author moved the
    # motion but forgot the anchor -- the strike would then be scheduled against
    # a moment where nothing happens.
    for a in clip.anchors:
        head = clip.track("head")
        if head is None:
            continue
        if np.min(np.abs(head.beats - a)) > 0.08:
            rep.add(
                Violation(
                    "anchor_without_keyframe", SEVERITY_WARN, "head", 0, a * spb,
                    float(a), 0.0,
                    f"anchor at beat {a:g} has no head keyframe within 0.08 beats. "
                    "Anchors mark the instant of contact -- put a keyframe there so "
                    "the contact pose is explicit.",
                )
            )

    return rep
