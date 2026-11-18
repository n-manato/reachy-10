"""楽器ごとの「動きの人格」。**誰が何を演奏しているか**を見せる層。

なぜこれが要るか
----------------
README が最初に掲げた要件は「音を聞かずに、部屋の向こうから誰が何を演奏して
いるか分かること」だった。それが満たせていなかった。理由は測ると一目瞭然で、
**全クリップが可動範囲をほとんど使っていない**:

    head z    ±22mm 中  最大 5.0mm  (23%)
    head y    ±25mm 中  最大 5.0mm  (20%)
    tilt      25°   中  最大 7.0°   (28%)
    body_yaw  150°  中  最大 2.5°   ( 1.7% )   ← ほぼ使っていない

効いているのは位置ではなく**動力学**（関節速度 4.0 rad/s、10秒窓の実効値
ヘッド 2.5 / アンテナ 3.0 rad/s）。速度 = 振幅 ÷ 時間なので:

    速い動きが小さいのは物理的に仕方ない。
    だが**遅い動き・静止した構えは桁違いに大きくできる**。

★ただし「静止姿勢は無料」ではなかった★
------------------------------------
最初「一定量なので速度も実効値もゼロ、いくら大きくても無料」と考えた。
**間違いだった。** 実測（構えの有無で `song build` を2回まわして比較）:

    r04 グロッケン  joint_vel_peak  3.078 -> 3.763  (+22%, 上限 4.0)
    r02 チェレスタ  joint_vel_peak  3.508 -> 3.801  (+8%)
    r05 マリンバ    joint_vel_peak  2.710 -> 2.747  (+1%)

構え**そのもの**は確かに速度を持たない。だが構えは Stewart 機構の動作点を
移すので、**ヤコビアンが変わり、同じ直交座標の動きに必要な関節速度が変わる**。
つまり既存の打点クリップが、構えた姿勢では余計に関節を回すことになる。

効き方は姿勢によって大きく違う（r04 は +22%、r05 は +1%）ので、
**予算表だけでは足りず、曲の文脈で実測しないと分からない**。
`song build` の margins を必ず見ること。目安は上限の 85% 以内。

これまでの設計は識別情報を「1音ごとの小さな上下」に置いていた。そこは物理的に
大きくできない場所なので、遠目には全台が同じに見える。**識別情報を、大きく
できる場所へ移す** ―― それがこのモジュール。

3つの層で見せる
---------------
1. **持ち姿（`Stance`）** ―― 常時の構え。体の向き・頭の高さ・傾き・耳の角度。
   完全な静止なので速度も実効値もゼロ。出番が無い間も誰が何担当か見える。
2. **フレーズの弧（`Sweep`）** ―― 4小節かけて描く大きな軌跡。80 BPM なら
   12秒周期なので、y±18mm でも速度は 0.02 m/s 未満。桁違いに余裕がある。
3. **1音ごとの打点** ―― 既存のクリップのまま。タイミングを読ませる役に徹する。

ダッキングしてはいけない
------------------------
`layers.apply_secondary` は打点の ±80ms を 0 にする。ここへ構えを通すと
**打つたびに姿勢が抜ける**。しかも大きな一定量を 80ms でゼロにすると
0.1〜0.2 m/s の速度が立ち、`joint_velocity`（4.0 rad/s）に落ちる。
だからこの層は `apply_secondary` の**後**に、ダッキングを通さずに足す。

座標系の注意
------------
頭の姿勢は world 座標で、`rel_yaw = head_yaw − body_yaw` に 45° の上限がある
（`validate.py:369-382`）。**body_yaw を大きく振るなら head yaw も一緒に
回さないと rel_yaw を使い切る**。なので `Stance` は体の向き `body_yaw_deg` と、
それに対する小さな `face_deg` で持ち、`head_yaw = body_yaw + face_deg` を出す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import radians

import numpy as np

from .program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_PITCH,
    CH_ROLL,
    CH_X,
    CH_Y,
    CH_YAW,
    CH_Z,
    N_CHANNELS,
    Program,
)

# アンテナの中立（`config/fleet.yaml` poses.neutral_antennas_rad と同じ値）。
NEUTRAL_ANTENNAS_DEG = (-10.0, 10.0)

SHAPES = ("none", "sway", "arc", "circle", "tilt")


@dataclass(frozen=True)
class Stance:
    """常時の構え。**速度ゼロなので発熱制限に一切効かない** ―― ここが要点。

    `antennas_deg` は絶対角（クリップと同じ流儀）。中立は ∓10° で、絶対値が
    大きいほど開く。値の向きは既存クリップの使われ方から取ったので、実際の
    見えかたは実機で一度確かめること。
    """

    body_yaw_deg: float = 0.0
    face_deg: float = 0.0
    """体の向きに対する頭の向き。rel_yaw(45°上限) を食うので小さく保つ。"""
    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    antennas_deg: tuple[float, float] = NEUTRAL_ANTENNAS_DEG

    def row(self) -> np.ndarray:
        """9チャンネルの**差分**（中立からのオフセット）を SI で返す。"""
        r = np.zeros(N_CHANNELS)
        r[CH_X] = self.x_mm / 1000.0
        r[CH_Y] = self.y_mm / 1000.0
        r[CH_Z] = self.z_mm / 1000.0
        r[CH_ROLL] = radians(self.roll_deg)
        r[CH_PITCH] = radians(self.pitch_deg)
        r[CH_YAW] = radians(self.body_yaw_deg + self.face_deg)
        r[CH_BODY_YAW] = radians(self.body_yaw_deg)
        r[CH_ANT_R] = radians(self.antennas_deg[0] - NEUTRAL_ANTENNAS_DEG[0])
        r[CH_ANT_L] = radians(self.antennas_deg[1] - NEUTRAL_ANTENNAS_DEG[1])
        return r


@dataclass(frozen=True)
class Sweep:
    """フレーズ単位のゆっくりした弧。遅いので大きくできる。"""

    shape: str = "none"
    bars: float = 4.0
    y_mm: float = 0.0
    z_mm: float = 0.0
    roll_deg: float = 0.0
    body_yaw_deg: float = 0.0

    # ★拍に乗った胴体の回転★ 上の弧はフレーズ単位（1〜4小節）でゆっくり
    # 動くが、こちらは**拍に同期して体を振る**。9台が同じ拍で体を振ると
    # 「合奏している集団」に見えるし、周期を役ごとに変えれば見分けにもなる。
    # 遅い弧と違って速度を食うので、実測しながら決めること。
    beat_yaw_deg: float = 0.0
    beat_period: float = 2.0     # 何拍で1往復するか
    beat_phase: float = 0.0      # 拍に対する位相（0.25 で裏拍寄り）
    beat_roll_deg: float = 0.0
    """胴体の回転に連れて首も傾ぐ量。**回転より 1/4 周期遅らせる。**

    公式の dances library が `move_groovy_sway_and_roll` でやっている手口で、
    横移動と roll を `phase_offset + 0.25` ずらしている。同位相で動かすと
    機械が2軸を同時に回しているようにしか見えないが、1/4 ずらすと体の
    重心移動に首が遅れて付いてくる形になり、生き物の動きに見える。
    """

    def __post_init__(self) -> None:
        if self.shape not in SHAPES:
            raise ValueError(f"unknown sweep shape {self.shape!r}; valid: {SHAPES}")

    def peak(self) -> np.ndarray:
        """このスウィープが到達しうる最大の**差分**（SI、絶対値）。"""
        r = np.zeros(N_CHANNELS)
        if self.shape == "none":
            return r
        r[CH_Y] = abs(self.y_mm) / 1000.0
        r[CH_Z] = abs(self.z_mm) / 1000.0
        r[CH_ROLL] = radians(abs(self.roll_deg))
        r[CH_BODY_YAW] = radians(abs(self.body_yaw_deg) + abs(self.beat_yaw_deg))
        # 体が回れば頭も一緒に回す（rel_yaw を食わないため）
        r[CH_YAW] = r[CH_BODY_YAW]
        return r

    def sample(self, beats: np.ndarray, beats_per_bar: float,
               phase: float = 0.0) -> np.ndarray:
        """拍の配列から (N, 9) の差分を作る。"""
        n = len(beats)
        out = np.zeros((n, N_CHANNELS))

        # 拍に乗った胴体の回転。形（shape）とは独立に効く ―― 静止している役
        # （ウッドブロック）でも、拍で体は振れる。
        if self.beat_yaw_deg or self.beat_roll_deg:
            th = 2.0 * np.pi * (beats / max(self.beat_period, 1e-6) + self.beat_phase)
            b = radians(self.beat_yaw_deg) * np.sin(th + phase)
            out[:, CH_BODY_YAW] += b
            out[:, CH_YAW] += b
            if self.beat_roll_deg:
                # 1/4 周期遅らせる。同位相だと2軸が同時に回るだけに見える。
                out[:, CH_ROLL] += radians(self.beat_roll_deg) * np.sin(
                    th + phase - np.pi / 2)

        if self.shape == "none":
            return out
        period = max(self.bars * beats_per_bar, 1e-6)
        th = 2.0 * np.pi * beats / period + phase
        y, z = self.y_mm / 1000.0, self.z_mm / 1000.0
        if self.shape == "sway":
            out[:, CH_Y] = y * np.sin(th)
        elif self.shape == "arc":
            # 横に往復しながら、その両端で沈む。低い楽器を端まで弾きに行く形。
            out[:, CH_Y] = y * np.sin(th)
            out[:, CH_Z] = -abs(z) * np.abs(np.sin(th))
        elif self.shape == "circle":
            out[:, CH_Y] = y * np.sin(th)
            out[:, CH_Z] = z * np.cos(th)
        elif self.shape == "tilt":
            out[:, CH_ROLL] = radians(self.roll_deg) * np.sin(th)
        if self.body_yaw_deg:
            b = radians(self.body_yaw_deg) * np.sin(th)
            out[:, CH_BODY_YAW] += b
            out[:, CH_YAW] += b
        return out


@dataclass(frozen=True)
class Persona:
    """1つの役の見せかた。"""

    stance: Stance = field(default_factory=Stance)
    sweep: Sweep = field(default_factory=Sweep)
    showcase_hits: int = 4
    note: str = ""

    def envelope(self) -> np.ndarray:
        """構え + 弧 の最悪値（SI、絶対値）。予算の検算に使う。"""
        return np.abs(self.stance.row()) + self.sweep.peak()


# ---------------------------------------------------------------------------
# 9役
# ---------------------------------------------------------------------------
# 割り振りの考え方: **9台が互いに違う軸で目立つ**ようにする。全員が同じ軸で
# 大小を競うと、結局「なんとなく違う」にしかならない。
#
#   体の向き   マリンバ -40° / ウッドブロック +40° / グロッケン +25°
#   高さ       グロッケンが最も高く、マリンバが最も低い（±11mm で最大差 22mm）
#   傾き       タンバリンだけが首を傾げ続ける
#   耳         鈴が別格に開き、ウッドブロックだけ左右非対称
#   動き続ける シェイカーだけ止まらない / ウッドブロックだけ静止
#
# 予算（副次レイヤ +7.2mm z ・打点クリップ +2.8mm z などを足した上で）
#   z ±22mm / y ±25mm / x ±25mm / tilt 25° / rel_yaw 45° / antenna 150°
# tests/test_persona.py がこれを数値で守る。

# 2026-08-09 改訂 ―― 公式の dances library（pollen-robotics/reachy_mini_dances_
# library、20手）を読んで方針を修正した。あちらの20手が使う軸の頻度は
#     pitch 8 / roll 7 / y 4 / yaw 2 / z 1 / x 1
# で、**回転が圧倒的、並進はほとんど使わない**。こちらの実測（28cm の機体で
# 並進 ±16mm は遠目に見えない）とも一致する。
#
# 一方であちらは **body_yaw を1つも使っていない** ―― `MoveOffsets` にその
# チャンネルが無いから。だがこの機体で最も視認性が高い軸はそこなので、
# 使わない手は無い。「回転主体・body_yaw を広く取る」へ寄せた。
#
# 3つ目の学び: あちらは `subcycles_per_beat` で**拍に対する速さ**を手ごとに
# 変えている。同じ形でも速さが違えば別の役に見える。弧の周期を1〜4小節に
# 散らした。
PERSONAS: dict[str, Persona] = {
    # 指揮者。動きは手続き生成のままで、構えだけ足す。唯一こちらを向いている。
    "conductor": Persona(
        stance=Stance(z_mm=5.0, antennas_deg=(-35.0, 35.0)),
        sweep=Sweep(),
        showcase_hits=0,
        note="the only one facing you and watching everyone; turns its "
             "whole body every 8 seconds",
    ),
    # 主旋律。鍵盤なので正面寄り。見下ろして横へ渡る。
    "celesta": Persona(
        # 打点クリップ自体が速い（joint_vel 3.5 で全9役の最上位）ので、弧は
        # 控えめに。y14 だと 3.80/4.0 まで行った。
        # ★この役だけは足せない★ 打点が全9役で最も速く、素で 3.55/4.0。
        # 構えを盛ると 3.93 まで行った（ヤコビアンが動くため）。90% を目安に
        # 抑える。見分けは体の向きと顎の角度で足りる。
        stance=Stance(z_mm=6.0, pitch_deg=7.0,
                      antennas_deg=(-25.0, 25.0)),
        sweep=Sweep(shape="sway", bars=4.0, y_mm=8.0, beat_yaw_deg=8.0, beat_period=4.0, beat_roll_deg=2.0),
        showcase_hits=5,
        note="nearly front-on, looking down at a keyboard, traversing "
             "sideways over four bars",
    ),
    # 対旋律。内を向いて、打った後に揺れが残る（クリップ側の特徴）。
    "vibraphone": Persona(
        stance=Stance(z_mm=2.0, pitch_deg=5.0, roll_deg=-4.0,
                      antennas_deg=(-50.0, 50.0)),
        sweep=Sweep(shape="tilt", bars=3.0, roll_deg=5.0, beat_yaw_deg=15.0, beat_period=3.0, beat_roll_deg=5.0),
        showcase_hits=4,
        note="turned inward; the only one that keeps ringing after the "
             "strike. Three-bar cycle",
    ),
    # 小さく高い楽器。**唯一の顎上げ**。耳を立てている。
    "glockenspiel": Persona(
        # 高さは z ではなく **pitch（顎を上げる）** で出す。z は ±21mm しか
        # 無いうえ副次と打点で 10mm 取られ、そもそも遠目に見えない。
        # 「上を向いている台」は一目で分かる。
        # この役はヤコビアンの効きが最も強く、素の 3.39 が構えを盛ると 3.90
        # まで行った。body_yaw を 30->22 に落としてもほぼ動かず、効いていたのは
        # **顎上げ（pitch）** のほうだった ―― 台を傾けると同じ打点により多くの
        # 関節速度が要る。-10° -> -7° で 3.6 台に収まる。
        stance=Stance(z_mm=4.0, pitch_deg=-7.0,
                      antennas_deg=(-75.0, 75.0)),
        sweep=Sweep(shape="circle", bars=4.0, y_mm=4.0, z_mm=2.0, beat_yaw_deg=6.0, beat_period=1.0, beat_roll_deg=2.0),
        showcase_hits=6,
        note="the only chin-up robot; ears held high, small circle",
    ),
    # 大きく低い楽器。**9台で最も体を振り、最も深く見下ろす**。
    "marimba": Persona(
        stance=Stance(z_mm=-8.0, pitch_deg=11.0,
                      antennas_deg=(-15.0, 15.0)),
        sweep=Sweep(shape="arc", bars=4.0, y_mm=18.0, z_mm=3.0, body_yaw_deg=10.0, beat_yaw_deg=16.0, beat_period=2.0, beat_roll_deg=6.0),
        showcase_hits=4,
        note="swings hardest (-55 deg); low, wide, ears folded down",
    ),
    # アクセント。**唯一ほとんど静止していて、単発だけが鋭い**。
    "woodblock": Persona(
        stance=Stance(roll_deg=5.0,
                      antennas_deg=(-70.0, 5.0)),
        sweep=Sweep(beat_yaw_deg=17.0, beat_period=4.0, beat_roll_deg=4.0),
        showcase_hits=4,
        note="faces furthest out (+55 deg) and holds still; one ear up. "
             "Moves only at the strike",
    ),
    # リズム。**唯一ずっと首を傾げている**。
    "tambourine": Persona(
        # チルトは roll と pitch の**合成**で効く。構え 8° + 弧 4° = 12° で、
        # 打点クリップの roll 6.9° が乗る余地を残す。
        stance=Stance(z_mm=4.0, roll_deg=8.0,
                      antennas_deg=(-30.0, 30.0)),
        sweep=Sweep(shape="tilt", bars=2.0, roll_deg=4.0, beat_yaw_deg=13.0, beat_period=2.0, beat_phase=0.25, beat_roll_deg=7.0),
        showcase_hits=6,
        note="head permanently cocked, the tilt rocking on a two-bar cycle",
    ),
    # 刻み。**唯一止まる瞬間が無い**。逆側へ傾ぐ。
    "shaker": Persona(
        stance=Stance(roll_deg=-8.0,
                      antennas_deg=(-20.0, 20.0)),
        sweep=Sweep(shape="sway", bars=1.0, y_mm=12.0, roll_deg=4.0, beat_yaw_deg=7.0, beat_period=1.0, beat_phase=0.5, beat_roll_deg=6.0),
        showcase_hits=8,
        note="swings on every bar and never stops; tilts the opposite way",
    ),
    # 彩り。**耳が別格に大きい**。頭はほとんど動かない。
    "jingle_bells": Persona(
        stance=Stance(z_mm=3.0, pitch_deg=-8.0,
                      antennas_deg=(-95.0, 95.0)),
        sweep=Sweep(beat_yaw_deg=14.0, beat_period=2.0, beat_phase=0.25, beat_roll_deg=4.0),
        showcase_hits=6,
        note="ears in a class of their own; the head barely moves, the "
             "ears do the playing",
    ),
}

# song.yaml が `bells` のような別名で書いていても引けるように。
ALIASES = {"bells": "jingle_bells", "celeste": "celesta", "vibes": "vibraphone",
           "glocken": "glockenspiel", "voice": "celesta"}

DEFAULT = Persona(note="default -- no stance")


def get(name: str) -> Persona:
    """名前からペルソナを引く。無ければ構えなしを返す（落とさない）。"""
    key = (name or "").strip().lower()
    return PERSONAS.get(ALIASES.get(key, key), DEFAULT)


# ---------------------------------------------------------------------------
# 適用
# ---------------------------------------------------------------------------


def posture_delta(persona: Persona, beats: np.ndarray, beats_per_bar: float,
                  phase: float = 0.0, ramp_beats: float = 2.0,
                  ramp_from_beat: float | None = None) -> np.ndarray:
    """構え + 弧 を (N, 9) の差分にする。

    先頭は中立から始めて、`ramp_beats` かけて構えへ入る。`op_arm` は
    frames[0] へ2秒かけて動くので、そこが中立なら「起き上がる → 構える」の
    順になり、構えに入る動作そのものが見える。

    `ramp_from_beat` を渡すと、そこから構え始める。オープニングで
    「名乗ってから構える」を作るときに使う。既定はプログラムの先頭。
    """
    n = len(beats)
    out = np.zeros((n, N_CHANNELS))
    if n == 0:
        return out
    out += persona.stance.row()[None, :]
    out += persona.sweep.sample(beats, beats_per_bar, phase)
    if ramp_beats > 0:
        b0 = float(beats[0]) if ramp_from_beat is None else float(ramp_from_beat)
        u = np.clip((beats - b0) / max(ramp_beats, 1e-9), 0.0, 1.0)
        out *= (u * u * (3.0 - 2.0 * u))[:, None]
    return out


# ---------------------------------------------------------------------------
# 前後の演出（紹介・礼・就寝）
# ---------------------------------------------------------------------------
#
# ここは時間（秒）で組む。音楽の外側なので拍に縛る意味が無く、むしろテンポを
# 変えたときに挨拶の速さまで変わるのは困る。
#
# ★速度の上限から、動ける量が決まる★ minjerk の最大角速度は
#     peak_ω ≈ 1.875 * Δ / T
# なので、上限 4.0 rad/s に対して安全率 0.6 を見ると
#     Δ ≤ 0.6 * 4.0 * T / 1.875 = 1.28 * T [rad] ≈ 73° * T[秒]
# 2秒の枠なら 1脚 0.7秒として約 51°。名乗りの振幅はここから決める。

MAX_DEG_PER_S = 73.0

SHOWCASE_ANTENNA_CAP_DEG = 125.0
"""名乗りで耳を開ける上限（絶対角）。運用上限 150° に少し余裕を残す。"""


def _smoothstep(u: np.ndarray) -> np.ndarray:
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def _ease(t: np.ndarray, t0: float, t1: float) -> np.ndarray:
    """[t0, t1] で 0->1 へ滑らかに上がる重み。"""
    if t1 <= t0:
        return (t >= t1).astype(float)
    return _smoothstep((t - t0) / (t1 - t0))


def _segment(t: np.ndarray, t0: float, t1: float) -> np.ndarray:
    """[t0, t1] で 0->1->0 の山。区間外は 0。"""
    mid = 0.5 * (t0 + t1)
    up = _ease(t, t0, mid)
    down = 1.0 - _ease(t, mid, t1)
    return np.where((t >= t0) & (t <= t1), np.minimum(up, down), 0.0)


def showcase(persona: Persona, t: np.ndarray, t0: float, span: float,
             gain: float = 2.2) -> np.ndarray:
    """「これが私です」―― 自分の構えを誇張して見せる。

    本番の構えをそのまま大きくする。**別に振り付けを書かない**のが肝で、
    そうしないと紹介と演奏中の姿が食い違っていく。最後は本番の構えちょうどに
    収まるので、継ぎ目が構造的に存在しない。

    振幅は枠の長さから決める（上の `MAX_DEG_PER_S`）。枠が短ければ自動的に
    小さくなるので、`slot_s` を詰めても速度制限に落ちない。
    """
    out = np.zeros((len(t), N_CHANNELS))
    if span <= 0:
        return out
    base = persona.stance.row()

    # ★誇張するのは「向き」と「耳」だけ★ 最初は構え全体を倍にしたが、
    # z と pitch まで伸ばすと関節の位置上限を割った（stewart_4 が -90.2°、
    # 範囲 [-73.6, 41.6]）。遠目に「こっちを見て」と読めるのは体の向きと耳で、
    # 高さや前傾はそもそも読めない。効く軸だけ伸ばす。
    BIG = [CH_BODY_YAW, CH_YAW, CH_ANT_R, CH_ANT_L]
    leg = max(span * 0.35, 0.15)
    biggest = float(np.max(np.abs(base[[CH_BODY_YAW, CH_YAW]])))
    allowed = np.radians(MAX_DEG_PER_S * leg)
    g = gain if biggest <= 1e-6 else min(gain, allowed / biggest)
    g = max(g, 1.0)

    # ★ここが返すのは「誇張ぶん」だけ★ 素の構えは `apply_posture` の担当で、
    # 各台は自分の枠でそれを立ち上げる（build.py が `ramp_from_beat` を渡す）。
    # 最初ここでも素の構えを足していたら、`_ease` が 1 で張り付いたまま
    # 戻らないので**構えが二重に掛かり**、全機ちょうど2倍になった
    # （r09 のアンテナが 192.9°、r04 の body_yaw が 50°）。
    rise = _ease(t, t0, t0 + span * 0.30)
    fall = _ease(t, t0 + span * 0.72, t0 + span)
    extra = np.zeros(N_CHANNELS)
    extra[BIG] = base[BIG] * (g - 1.0)
    out += extra[None, :] * (rise * (1.0 - fall))[:, None]

    # 耳をひと振り。どの役も耳は使えるので、名乗りの共通の合図になる
    wag = _segment(t, t0 + span * 0.25, t0 + span * 0.75)
    out[:, CH_ANT_R] += np.radians(-22.0) * wag
    out[:, CH_ANT_L] += np.radians(22.0) * wag

    # ★アンテナは「構え込みの絶対角」で頭打ちにする★ 倍率を掛けると鈴
    # （構え ±95°）が 2.2倍 + ひと振りで上限 150° を大きく割る。
    # このとき同じ枠で構えも立ち上がっているので、**誇張ぶんだけ**を見て
    # 抑えても足りない（それで 177° が残った）。合計で抑える。
    cap = radians(SHOWCASE_ANTENNA_CAP_DEG)
    for ch, neutral in ((CH_ANT_R, radians(NEUTRAL_ANTENNAS_DEG[0])),
                        (CH_ANT_L, radians(NEUTRAL_ANTENNAS_DEG[1]))):
        held = base[ch] + neutral            # 構えを取り終えたときの実角度
        out[:, ch] = np.clip(out[:, ch] + held, -cap, cap) - held
    return out


def cue(t: np.ndarray, at: float, span: float, toward: float,
        scale: float = 1.0) -> np.ndarray:
    """指揮者の「出て」の合図。振りかぶって、切る。

    紹介では**指揮者が先に合図し、それを受けて台が鳴る**。ただ順番に鳴るより、
    指示 -> 応答の関係が見えるほうが合奏らしい。

    `toward` は -1..+1 で、アンサンブルのどのあたりを向くか。指揮者は
    `home_yaw` を中心に振るので、ここは中心からの相対で返す。
    """
    out = np.zeros((len(t), N_CHANNELS))
    if span <= 0:
        return out
    # 振りかぶり（上げる）-> 打つ（下げて止める）
    lift = _segment(t, at, at + span * 0.55)
    hit = _segment(t, at + span * 0.45, at + span)
    out[:, CH_Z] += (0.010 * lift - 0.012 * hit) * scale
    out[:, CH_PITCH] += (np.radians(-7.0) * lift + np.radians(12.0) * hit) * scale
    # 相手のほうを向く。合図の間だけ、終わったら中心へ戻る
    face = _segment(t, at, at + span)
    out[:, CH_BODY_YAW] += np.radians(22.0) * toward * face
    out[:, CH_YAW] += np.radians(22.0) * toward * face
    out[:, CH_ANT_R] += np.radians(-26.0) * hit * scale
    out[:, CH_ANT_L] += np.radians(26.0) * hit * scale
    return out


def cue_ictus(at: float, span: float) -> float:
    """`cue` の「打つ」瞬間。合図に動作を合わせるとき、ここへ揃える。

    合図の形と、それを受ける側の動きを**同じ式から**出す。別々に書くと
    「合図より先に顔が上がる」といったズレが必ず入る。
    """
    return at + span * 0.725


def wait_blend(t: np.ndarray, t_from: float, t_until: float,
               slot_at: float = 0.0, slot_span: float = 0.0,
               ramp: float = 0.6) -> tuple[np.ndarray, np.ndarray]:
    """紹介の間ずっと `WAIT_POSE` で止まる。**自分の枠だけ抜ける。**
    `(target, weight)`

    自分の番が終わったら**また待機に戻る**。最初は「自分の番が来るまで」
    だけ待たせていたので、名乗り終えた台がそのまま構えて動き続け、後半は
    紹介済みの台が全部動いている状態になっていた。それでは誰が紹介されて
    いるのか分からない。

    動いていてよいのは、いつでも**指揮者と、いま名乗っている1台だけ**。
    """
    n = len(t)
    # 紹介が始まってから、全員が構えに入る直前まで
    on = _ease(t, t_from, t_from + ramp) * (1.0 - _ease(t, t_until, t_until + ramp))
    if slot_span > 0:
        # 自分の枠のあいだだけ穴を空ける
        hole = (_ease(t, slot_at - ramp * 0.5, slot_at)
                * (1.0 - _ease(t, slot_at + slot_span,
                               slot_at + slot_span + ramp)))
        on = on * (1.0 - hole)
    return np.repeat(WAIT_POSE[None, :], n, axis=0), on


def greeting(t: np.ndarray, t0: float, span: float,
             scan_to: float = 0.0, turn_deg: float = 0.0,
             turn_s: float = 1.6) -> np.ndarray:
    """指揮者の挨拶。客席へ一礼して、そのあとアンサンブルを見渡す。

    指揮者はアンサンブルを向いて立っているので、そのまま礼をすると楽器の台に
    頭を下げることになる。`turn_deg` を渡すと客席へ向き直って礼をし、挨拶の枠の
    終わりまでに戻る。**曲の長さを変えないよう**、回り始めは `t0` を待たず
    プログラムの先頭からにする。頭も一緒に回すので rel_yaw は消費しない。
    """
    out = np.zeros((len(t), N_CHANNELS))
    if span <= 0:
        return out
    end = t0 + span
    bow_from, bow_to = t0, t0 + span * 0.6
    if turn_deg:
        bow_from, bow_to = turn_s, end - turn_s
        facing = (radians(turn_deg) * _ease(t, 0.0, turn_s)
                  * (1.0 - _ease(t, bow_to, end)))
        out[:, CH_BODY_YAW] += facing
        out[:, CH_YAW] += facing
    bow = _segment(t, bow_from, bow_to)
    out[:, CH_Z] += -0.010 * bow
    out[:, CH_PITCH] += np.radians(14.0) * bow
    out[:, CH_ANT_R] += np.radians(-30.0) * bow
    out[:, CH_ANT_L] += np.radians(30.0) * bow
    if scan_to > t0 + span:
        # 名乗りの間、紹介されている台のほうを向いていく
        u = np.clip((t - (t0 + span)) / max(scan_to - t0 - span, 1e-6), 0.0, 1.0)
        turn = np.radians(28.0) * np.sin(2.0 * np.pi * u)
        out[:, CH_BODY_YAW] += turn
        out[:, CH_YAW] += turn
    return out


def _absolute(z_m=0.0, x_m=0.0, pitch_deg=0.0, ant_deg=None) -> np.ndarray:
    """絶対姿勢を1フレーム分。アンテナは中立を含んだ実角度で入れる。"""
    r = np.zeros(N_CHANNELS)
    r[CH_X] = x_m
    r[CH_Z] = z_m
    r[CH_PITCH] = radians(pitch_deg)
    a = NEUTRAL_ANTENNAS_DEG if ant_deg is None else ant_deg
    r[CH_ANT_R] = radians(a[0])
    r[CH_ANT_L] = radians(a[1])
    return r


WAIT_POSE = _absolute(pitch_deg=8.0, ant_deg=(-3.0, 3.0))
"""紹介の間、自分の番を待っている姿勢。耳を下げ、少しうつむく。

**9台とも同じ姿勢で完全に止まる。** 動いているのは指揮者と、いま紹介されて
いる1台だけ ―― そうすると目がそこへ行く。全員が思い思いに動いていると、
誰が紹介されているのか分からない。

耳を下げるのは `READY_POSE`（耳を上げる）と対になっていて、
「待つ」と「応える」が形で対比される。
"""

READY_POSE = _absolute(ant_deg=(-80.0, 80.0))
"""「気をつけ」。**9台とも完全に同じ姿勢**にする。

演奏中はそれぞれ違う構えをしている（首を傾げていたり、顎を上げていたり）。
そのまま礼に入ると、傾いたまま頭を下げる台が出る。挨拶は揃っていないと
挨拶に見えないので、**礼の前に一度そろえる**。

耳を上げるのが合図になる ―― 頭の動きより先に、遠目にもはっきり分かる。
`_absolute` の既定で roll も pitch も 0 なので、顔はここで水平に戻る。
"""

BOW_POSE = _absolute(z_m=-0.016, pitch_deg=19.0, ant_deg=(-2.0, 2.0))
"""礼。**頭と一緒に耳も伏せる。**

`READY_POSE` で上げた耳（±80°）を、頭を下げるのに合わせて畳む。犬がお辞儀
するときの形で、動作として自然に見える。耳を上げたまま頭だけ下げると、
2つの動きが別々に見えてしまう。

耳を上げる -> 下げる、の落差そのものが礼の合図にもなる。
"""


# SDK の park 姿勢（reachy_mini.py:55-75）。実測で全関節に余裕あり
# （最小 +15.25°、stewart_5）。可動域の z を実測値へ直したので、これを
# **演奏プログラムの中で**扱える ―― 9台がフレーム同期で同時に寝る。
STOW_X_M = -0.021
STOW_Z_M = -0.044
STOW_PITCH_DEG = 21.0
"""SDK の park は 24.39°。チルト上限 25° に対して余裕 0.6° しか無く、
グルーヴの 1.4° が乗っただけで割る。見た目はほぼ同じなので 21° にして
4° の余裕を残す。z は -44mm のまま ―― 「頭が胴に沈む」のはこちらの効果。"""
STOW_ANTENNA_DEG = 150.0
"""SDK は 174.7° だが、運用上限が 150°。ここは上限を広げるより手前で止める。
150° でも耳は十分に後ろへ倒れている。"""


STOW_POSE = _absolute(x_m=STOW_X_M, z_m=STOW_Z_M, pitch_deg=STOW_PITCH_DEG,
                      ant_deg=(-STOW_ANTENNA_DEG, STOW_ANTENNA_DEG))


def bow_and_stow(t: np.ndarray, t0: float, bow_s: float, hold_s: float,
                 rise_s: float, stow_s: float, ready_s: float = 1.5,
                 turn_deg: float = 0.0,
                 home_yaw_deg: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """礼と就寝を **絶対姿勢への遷移** として返す。`(target(N,9), weight(N,))`

    ★差分で足してはいけない★ 最初そう作って全機が可動域を割った。礼は
    「構えたままお辞儀を足す」ではなく「**その姿勢へ行く**」動作で、就寝も
    メーカーの park という絶対的な姿勢。差分にすると、その台がたまたま
    保っている姿勢（指揮者なら開いたアンテナ、マリンバなら +10° の前傾）に
    積み上がる。実測でアンテナ -225°、チルト 37.2° まで行った。

    呼び出し側は `frames*(1-w) + target*w` で混ぜる。
    """
    n = len(t)
    target = np.zeros((n, N_CHANNELS))
    if bow_s <= 0:
        return target, np.zeros((n, N_CHANNELS))

    # 姿勢の並びを時刻で書く。区間ごとに if を積むより、こう並べたほうが
    # 「いつ何の姿勢にいるか」が読めるし、段を足すのも楽。
    #
    #   耳を上げて揃える -> 礼 -> 保つ -> 起きる -> 顔をしまう
    #
    # ★礼の前に一度そろえる★ 演奏中は9台が別々の構えをしている（首を
    # 傾げていたり顎を上げていたり）。そのまま頭を下げると、傾いたまま礼を
    # する台が出る。挨拶は揃っていないと挨拶に見えない。
    t_bow = t0 + ready_s
    t_hold = t_bow + bow_s
    t_rise = t_hold + hold_s
    t_stow = t_rise + rise_s
    keys = [
        (t0,              READY_POSE),   # 音が止まった瞬間、耳を上げて水平に
        (t_bow,           READY_POSE),
        (t_hold,          BOW_POSE),     # 礼
        (t_rise,          BOW_POSE),     # 保つ
        (t_stow,          READY_POSE),   # 起きる
        (t_stow + stow_s, STOW_POSE),    # 顔をしまう
    ]
    for (ta, pa), (tb, pb) in zip(keys, keys[1:]):
        seg = (t >= ta) & (t <= tb)
        if not seg.any():
            continue
        u = _ease(t[seg], ta, tb)[:, None]
        target[seg] = pa[None, :] * (1.0 - u) + pb[None, :] * u
    target[t > keys[-1][0]] = keys[-1][1]

    # 混ぜ具合。耳上げの間に構えから完全に乗り換え、以降は最後まで 1。
    w = np.repeat(_ease(t, t0, t0 + max(ready_s, 0.3))[:, None], N_CHANNELS, axis=1)

    # 指揮者は客席のほうへ向き直って礼をする。180° は機構的に無理なので
    # （body_yaw の運用上限 150°）限界の手前まで。**頭も一緒に回す** ――
    # 頭の姿勢は world 座標なので、胴だけ回すと相対 yaw（上限45°）を食う。
    #
    # 向き直りは礼より早く始まり、就寝までその向きのまま。だから重みは
    # **チャンネルごとに別**にする。全体の重みを持ち上げると、礼と就寝の
    # 間の「起き上がる」区間まで姿勢が固定されてしまう。
    if turn_deg or home_yaw_deg:
        # 回る -> 礼 -> 保つ -> **元の向きへ戻る** -> 就寝。
        # 戻さないと、横を向いたまま寝ることになる。戻りは「起き上がる」区間
        # （rise_s）と重ねるので、動作としては一続きに見える。
        #
        # ★真後ろを向くための置き方★ 機構の可動域は ±150° なので、正面を
        # 機構ゼロに置くと 180° は回れない。そこで**指揮者だけ台の上で 90°
        # 回して置き**、演奏中の向き（= 客席に背を向けてアンサンブルを見る）を
        # `home_yaw_deg = -90°`、礼の向きを `+90°` にする。差はちょうど 180° で、
        # どちらも可動域の内側に収まる。
        # 耳を上げている間に回り始め、起き上がるところで元の向きへ戻す。
        turn_w = (_ease(t, t0, t_bow + bow_s * 0.4)
                  * (1.0 - _ease(t, t_rise, t_stow)))
        ang = radians(home_yaw_deg) + radians(turn_deg) * turn_w
        target[:, CH_BODY_YAW] = ang
        target[:, CH_YAW] = ang
        for ch in (CH_BODY_YAW, CH_YAW):
            w[:, ch] = np.maximum(w[:, ch], turn_w)
        # ★並進も一緒に回す★ 頭の位置は world 座標なので、体が 140° 回った
        # あとに world の x=-21mm を指すと、機構から見れば真横への変位に
        # なって届かない（stewart_1 が -0.965、範囲 [-0.726, 1.285]）。
        # 姿勢は体に付いて回るものなので、x/y を回転させる。
        x, y = target[:, CH_X].copy(), target[:, CH_Y].copy()
        c, s = np.cos(ang), np.sin(ang)
        target[:, CH_X] = x * c - y * s
        target[:, CH_Y] = x * s + y * c
    return target, w


def release_gate(times: np.ndarray, at_s: float, span: float) -> np.ndarray:
    """`at_s` から `span` 秒かけて構えを解く重み（1 -> 0）。

    **礼と就寝は構えの「上に」乗せてはいけない。** 最初そう作って、アンテナが
    -225°（上限150）、チルトが 37.2°（上限25）になった。礼は「構えたまま
    お辞儀する」ではなく「構えを解いてお辞儀する」動作なので、先に構えを
    抜く。就寝も同じ ―― メーカーの park 姿勢は絶対的な姿勢であって、
    誰かの構えからの相対量ではない。
    """
    if span <= 0:
        return np.ones_like(times)
    return 1.0 - _smoothstep((times - at_s) / span)


def apply_posture(program: Program, persona: Persona, beats: np.ndarray,
                  beats_per_bar: float, phase: float = 0.0,
                  ramp_from_beat: float | None = None,
                  gate: np.ndarray | None = None) -> Program:
    """構えと弧をプログラムへ足す。**ダッキングを通さない。**

    `layers.apply_secondary` と違い、打点の周りで抜かない。構えは打っている
    間も保っていなければ意味が無いし、大きな一定量を 80ms でゼロにすると
    そこで速度制限に落ちる。
    """
    if program.n_frames == 0:
        return program
    delta = posture_delta(persona, beats, beats_per_bar, phase,
                          ramp_from_beat=ramp_from_beat)
    if gate is not None:
        delta = delta * np.asarray(gate)[:, None]
    if delta.shape != program.frames.shape:
        raise ValueError(
            f"posture {delta.shape} does not match program "
            f"{program.frames.shape}")
    out = Program(**{**program.__dict__, "frames": program.frames + delta})
    out.meta = dict(program.meta)
    out.meta["persona"] = {
        "stance": persona.stance.__dict__,
        "sweep": persona.sweep.__dict__,
        "note": persona.note,
    }
    return out
