"""1本の録音から、9台の音が実際にどれだけ揃っていたかを出す。

原理
----
9台は**それぞれ違うステム**を鳴らしている。だから1本の録音の中で、各台の
ステムとの相互相関を取れば、その台の音がいつ到着したかが分かる。同じ音を
9台で鳴らしていたらピークが重なって分離できない ―― ステムを分けた設計上の
判断が、そのまま測定を可能にしている。

何が含まれるか（ここを誤解すると数字を読み違える）
--------------------------------------------------
台 i について求まる遅れ τ_i の中身は:

    τ_i = 音声パイプラインの遅れ(台i)
        + 空気中の伝搬時間(台i → マイク)
        + 取り込みの遅れ(マイク側、全台共通)
        + クロック同期の誤差(台i)

3項目は**全台に共通なので、台どうしの差を見るときは完全に消える**。
残る問題は2項目 ―― 音速 343 m/s は 2.9 ms/m なので、机の上で 1.5 m 散って
いれば ±4 ms 程度は幾何だけで生じる。**これは誤差ではなく物理**で、聴く人に
とっても本当にその通り遅れて届く。

つまりここで出る数字は「**指揮者の位置で聴いたときのずれ**」。電気系だけの
値が欲しければ `--distances` に各台までの距離を渡せば伝搬ぶんを引く。
渡さなければ引かない（勝手に補正して、補正したことを忘れるほうが危険）。

相関のかけ方
------------
生波形どうしの相関は位相に敏感すぎて、部屋の反射で簡単に崩れる。ここでは
**振幅包絡（エネルギーの立ち上がり）**で相関を取る。打楽器のアタックは
包絡に鋭く出るので、位相が乱れても山は残る。
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SPEED_OF_SOUND = 343.0      # m/s、20℃


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """WAV を (N,) モノラル float32 と サンプルレートで読む。"""
    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sw)
    if dtype is None:
        raise ValueError(f"{path}: unsupported bit depth {sw * 8}")
    x = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    x = (x - 128.0) / 128.0 if sw == 1 else x / np.float32(np.iinfo(dtype).max)
    return x.reshape(-1, ch).mean(axis=1), sr


def envelope(x: np.ndarray, sr: int, hop: int = 64, smooth_ms: float = 6.0) -> np.ndarray:
    """振幅包絡。エネルギーを間引いて、軽くならす。

    生波形ではなく包絡で相関を取るのは、部屋の反射で位相が崩れても
    「音が立ち上がった瞬間」は残るから。
    """
    n = (len(x) // hop) * hop
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    e = np.sqrt((x[:n].reshape(-1, hop) ** 2).mean(axis=1))
    w = max(1, int(smooth_ms * 1e-3 * sr / hop))
    if w > 1:
        k = np.hanning(w * 2 + 1)
        k /= k.sum()
        e = np.convolve(e, k, mode="same")
    # 立ち上がりだけ残す。定常音は相関の山をなだらかにするだけで役に立たない。
    d = np.diff(e, prepend=e[0])
    return np.maximum(d, 0.0).astype(np.float32)


@dataclass
class Arrival:
    robot_id: str
    part: str
    lag_s: float
    peak: float            # 相関ピークの高さ（正規化済み）
    margin: float          # 2番目に高い山との比。低いと当てにならない
    ok: bool = True
    note: str = ""
    # 校正音のときだけ埋まる。同じ台を何度も鳴らして、その一致度を誤差とする。
    # 1発しか測らないと、出た数字がどれくらい信用できるか分からない。
    repeats: list[float] = field(default_factory=list)
    scatter_ms: float = 0.0


@dataclass
class SkewResult:
    arrivals: list[Arrival] = field(default_factory=list)
    reference: str = ""
    sr: int = 0
    hop: int = 64
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> list[Arrival]:
        return [a for a in self.arrivals if a.ok]

    def spread_ms(self) -> float:
        lags = [a.lag_s for a in self.usable]
        return (max(lags) - min(lags)) * 1000.0 if len(lags) > 1 else 0.0


def _corr_lag(rec_env: np.ndarray, stem_env: np.ndarray, max_lag: int) -> tuple[int, float, float]:
    """stem が rec のどこにあるかを相関で探す。(lag, ピーク, 二番手との比)。"""
    if len(stem_env) < 8 or len(rec_env) < 8:
        return 0, 0.0, 0.0
    a = rec_env - rec_env.mean()
    b = stem_env - stem_env.mean()
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    A = np.fft.rfft(a, n)
    B = np.fft.rfft(b, n)
    c = np.fft.irfft(A * np.conj(B), n)[: max_lag + 1]
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom <= 0:
        return 0, 0.0, 0.0
    c = c / denom
    k = int(np.argmax(c))
    peak = float(c[k])
    # 二番手は、ピークの周り（±30ms相当）を除いた最大値。ここと差が無ければ
    # 「どこでも同じくらい合う」ということで、その台の測定は信用できない。
    guard = max(4, len(c) // 200)
    masked = c.copy()
    masked[max(0, k - guard): k + guard + 1] = -np.inf
    second = float(np.max(masked)) if np.isfinite(masked).any() else 0.0
    margin = peak / second if second > 1e-9 else float("inf")
    return k, peak, margin


def _burst_starts(x: np.ndarray, sr: int, frac: float = 0.25,
                  min_gap_s: float = 0.5) -> list[int]:
    """校正用ステムの中で、バーストが始まるサンプル位置。"""
    thr = float(np.abs(x).max()) * frac
    if thr <= 0:
        return []
    out: list[int] = []
    i, gap = 0, int(min_gap_s * sr)
    idx = np.flatnonzero(np.abs(x) > thr)
    for k in idx:
        if not out or k - out[-1] > gap:
            out.append(int(k))
    return out


def measure_bursts(
    rec: np.ndarray,
    stem: np.ndarray,
    sr: int,
    burst_s: float = 0.25,
    search_ms: float = 250.0,
    coarse_lag_s: float = 5.0,
) -> tuple[float, float, list[float], float]:
    """バーストを1発ずつ合わせ、(中央値, ばらつき, 各推定, 相関) を返す。

    **1発だけ測ってはいけない。** 部屋の反射や一時的な雑音で1回くらいは
    簡単にずれる。同じ台が同じ音を何度も出しているのだから、そのばらつきを
    そのまま誤差として出す。一致しなければ、その台の数字は捨てる。
    """
    starts = _burst_starts(stem, sr)
    if not starts:
        return 0.0, 0.0, [], 0.0

    # まず全体で粗く合わせ、録音の中でのステムの開始位置を決める。
    k0, peak, _ = _corr_lag(rec, stem, int(coarse_lag_s * sr))

    half = int(search_ms * 1e-3 * sr)
    nb = int(burst_s * sr)
    ests: list[float] = []
    for st in starts:
        ref = stem[st: st + nb]
        if len(ref) < nb // 2:
            continue
        c0 = k0 + st - half
        seg = rec[max(0, c0): c0 + nb + 2 * half]
        if len(seg) < nb + half:
            continue
        a = seg - seg.mean()
        b = ref - ref.mean()
        n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
        c = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)[: len(a)]
        d = np.linalg.norm(a) * np.linalg.norm(b)
        if d <= 0:
            continue
        k = int(np.argmax(c / d))
        # 窓の端に張り付いた推定は、実際にはもっと外にある可能性がある。
        # 「窓の端」を答えとして返すと、外れ値が上限に化けて紛れ込む。
        if k <= 1 or k >= len(a) - nb - 1:
            continue
        ests.append((k - half) / sr)

    if not ests:
        return 0.0, 0.0, [], peak
    med = float(np.median(ests))
    scatter = float(np.median(np.abs(np.asarray(ests) - med))) * 1000.0
    return k0 / sr + med, scatter, [e * 1000 for e in ests], peak


def measure_skew(
    recording: str | Path,
    stems: dict[str, tuple[str, Path]],
    max_lag_s: float = 2.0,
    min_peak: float = 0.02,
    min_margin: float = 1.15,
    distances_m: dict[str, float] | None = None,
    mode: str = "envelope",
    max_scatter_ms: float = 6.0,
) -> SkewResult:
    """録音と各台のステムから到着時刻を出す。

    Args:
        stems: robot_id -> (part名, ステムのパス)
        distances_m: robot_id -> マイクまでの距離[m]。渡せば伝搬ぶんを引く。
            渡さなければ引かない ―― 勝手に補正して、補正したことを忘れる
            ほうが危険なので。
        mode: "envelope" は音楽向け（位相に頼らない）。"waveform" は整合フィルタで、
            校正用のノイズバーストのように広帯域で非周期な信号に使う。
            実測での差は大きい ―― 同じ録音でピーク比 1.08 対 6.24。
    """
    rec, sr = read_wav(recording)
    raw = mode == "waveform"
    hop = 1 if raw else 64
    res = SkewResult(sr=sr, hop=hop)
    rec_ref = rec if raw else envelope(rec, sr, hop=hop)
    max_lag = int(max_lag_s * sr / hop)

    for rid, (part, path) in sorted(stems.items()):
        path = Path(path)
        if not path.exists():
            res.arrivals.append(Arrival(rid, part, 0.0, 0.0, 0.0, ok=False,
                                        note=f"no stem: {path.name}"))
            continue
        stem, ssr = read_wav(path)
        if ssr != sr:
            res.warnings.append(
                f"{rid}: stem is {ssr}Hz, recording is {sr}Hz -- not comparable")
            res.arrivals.append(Arrival(rid, part, 0.0, 0.0, 0.0, ok=False,
                                        note="sample-rate mismatch"))
            continue
        if raw:
            # 校正音: バーストを1発ずつ合わせ、一致度を誤差にする。
            lag, scatter, ests, peak = measure_bursts(rec, stem, sr)
            _, _, margin = _corr_lag(rec, stem, max_lag)
            if distances_m and rid in distances_m:
                lag -= distances_m[rid] / SPEED_OF_SOUND
            a = Arrival(rid, part, lag, peak, margin,
                        repeats=ests, scatter_ms=scatter)
            if len(ests) < 2:
                a.ok = False
                a.note = ("fewer than two usable bursts. With one you cannot "
                          "tell how much to trust the number")
            elif scatter > max_scatter_ms:
                a.ok = False
                a.note = (f"repeats disagree (scatter {scatter:.1f} ms > "
                          f"{max_scatter_ms:.0f}) -- either reflections, or "
                          "this robot is buried in the mix")
            res.arrivals.append(a)
            continue

        stem_ref = envelope(stem, sr, hop=hop)
        k, peak, margin = _corr_lag(rec_ref, stem_ref, max_lag)
        lag = k * hop / sr
        if distances_m and rid in distances_m:
            lag -= distances_m[rid] / SPEED_OF_SOUND

        a = Arrival(rid, part, lag, peak, margin)
        if peak < min_peak:
            a.ok = False
            a.note = (f"weak correlation ({peak:.3f} < {min_peak}) -- this robot "
                      "is not reaching the mic, or is buried in the mix")
        elif margin < min_margin:
            a.ok = False
            a.note = (f"no clear peak (runner-up ratio {margin:.2f}). A repetitive "
                      "part correlates almost anywhere")
        res.arrivals.append(a)

    usable = res.usable
    if usable:
        ref = min(usable, key=lambda a: a.lag_s)
        res.reference = ref.robot_id
    if len(usable) < 2:
        res.warnings.append(
            "fewer than two usable robots. Skew only means anything as a "
            "difference between robots, so this recording says nothing")
    return res


def report(res: SkewResult, distances_used: bool = False) -> str:
    lines = []
    if not res.usable:
        lines.append("  no usable measurements")
    else:
        base = min(a.lag_s for a in res.usable)
        lines.append(f"  {'robot':<6}{'part':<15}{'arrival[ms]':>12}"
                     f"{'scatter':>10}{'n':>4}{'corr':>8}")
        lines.append("  " + "-" * 56)
        for a in sorted(res.arrivals, key=lambda x: (not x.ok, x.lag_s)):
            if not a.ok:
                lines.append(f"  {a.robot_id:<6}{a.part:<15}{'--':>12}"
                             f"{'':>10}{len(a.repeats):>4}{a.peak:>8.3f}"
                             f"  {a.note}")
                continue
            lines.append(f"  {a.robot_id:<6}{a.part:<15}"
                         f"{(a.lag_s - base) * 1000:>12.1f}"
                         f"{a.scatter_ms:>10.1f}{len(a.repeats):>4}"
                         f"{a.peak:>8.3f}")
        lines.append("")
        lines.append(f"  spread {res.spread_ms():.1f} ms "
                     f"({len(res.usable)} robots)")
        lines.append("  percussion attacks read as simultaneous below "
                     "roughly 10-20 ms")
    for w in res.warnings:
        lines.append(f"  ! {w}")
    lines.append("")
    if distances_used:
        lines.append("  distances were given, so flight time has been subtracted.")
    else:
        lines.append(
            "  NOTE: these numbers include flight time through air "
            "(343 m/s = 2.9 ms/m).\n"
            "    Robots spread over 1.5 m of table account for ~4 ms on "
            "their own.\n"
            "    So this is the skew *as heard from the conductor's seat*, "
            "not the\n"
            "    electrical skew. Pass --distance r0X=1.2 to separate them.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 複数地点からの測定を突き合わせる
# ---------------------------------------------------------------------------


def compare(results: dict[str, SkewResult]) -> str:
    """別々の台のマイクで測った結果を並べ、何が動いて何が動かないかを見る。

    **1地点の測定だけでは `audio_lead_ms` を決めてはいけない。** そこで測れる
    のは「その位置で聴いたときのずれ」で、その大半は音の通り道で決まる。
    実測（2026-08-07、同じ校正音を r01 と r06 のマイクで録った）:

        robot   r01が聞いた   r06が聞いた
        r04      36.1 ms       6.3 ms     <- 30 ms 動いた
        r07      23.5 ms       8.8 ms     <- 15 ms 動いた
        r03      30.1 ms      38.0 ms

    聞く位置を変えただけで順序が入れ替わる。つまり見えているのは主に距離と
    反射で、ロボット側のタイミングではない。これを補正値として入れると、
    録った1点に合わせて**他の全員に対してわざと狂わせる**ことになる。

    切り分け方は単純で、**どの地点から聴いても遅い台だけが本当に遅い**。
    位置で変わるぶんは空気の話なので、機体に焼き付けてはいけない。
    """
    ids = sorted({a.robot_id for r in results.values() for a in r.usable})
    if len(results) < 2:
        return ("  only one listening position -- position effects and "
                "per-robot effects cannot be separated.")

    lines = [f"  {'robot':<7}" + "".join(f"{'heard by ' + k:>16}" for k in results)
             + f"{'min':>10}{'swing':>10}"]
    lines.append("  " + "-" * (7 + 16 * len(results) + 20))

    common: dict[str, float] = {}
    for rid in ids:
        vals = {}
        for k, r in results.items():
            hit = next((a for a in r.usable if a.robot_id == rid), None)
            if hit is None:
                continue
            base = min(a.lag_s for a in r.usable)
            vals[k] = (hit.lag_s - base) * 1000.0
        if len(vals) < 2:
            continue
        lo, hi = min(vals.values()), max(vals.values())
        common[rid] = lo
        cells = "".join(f"{vals.get(k, float('nan')):>16.1f}" for k in results)
        lines.append(f"  {rid:<7}{cells}{lo:>10.1f}{hi - lo:>10.1f}")

    if not common:
        return "\n".join(lines + ["", "  no robot was measurable from both positions"])

    worst_move = max(
        (max(v for k, r in results.items()
             for a in r.usable if a.robot_id == rid for v in [(a.lag_s - min(x.lag_s for x in r.usable)) * 1000])
         - common[rid]) for rid in common)
    lines += [
        "",
        f"  left over from every position (suspect the robot): "
        f"{max(common.values()) - min(common.values()):.1f} ms",
        f"  largest change with listening position (that is air): {worst_move:.1f} ms",
        "",
        "  A robot with a large 'swing' must NOT have that number written "
        "into audio_lead_ms.",
        "    That value is distance and reflection; it is only true at the "
        "one spot you recorded from.",
        "    Only the 'min' column is a candidate to bake into a robot.",
    ]
    return "\n".join(lines)
