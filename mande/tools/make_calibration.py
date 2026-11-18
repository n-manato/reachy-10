#!/usr/bin/env python3
"""台ごとの音の到着時刻を測るための、専用の校正音を作る。

    python tools/make_calibration.py

なぜ曲そのものでは測れないか
----------------------------
最初は演奏の録音と各台のステムを相関させようとして、失敗した。相関ピークが
どこにも立たない（二番手との比が 1.00〜1.07）。理由は曲が反復的だから ――
*It's a Small World* は 16小節がそのまま2回繰り返され、その中も同じ音型の
繰り返しでできている。周期的な信号の自己相関は周期ごとに同じ高さの山を作るので、
「どこで合っているか」が原理的に決まらない。

**この罠はこのプロジェクトで3回踏んだ。** 1回目はシェイカーの動きの周期を
自己相関で測ろうとしてループ長を拾った。2回目が上の曲。3回目は、この
測定信号そのものだった ―― 全機に**同じ**チャープを 1.5 秒の等間隔で
鳴らさせたので、信号全体が周期 1.5 秒で周期的になり、相関が隣の台の
チャープに合ってしまった（lag が 1.5 秒違いの2群に割れた）。
「繰り返すものは相関で位置を特定できない」と自分で書いた直後に、
繰り返しで測定信号を作っていた。

どうするか
----------
台ごとに**互いに相関しない**信号を鳴らす。乱数列を帯域制限したノイズバースト
（0.25秒、300〜6000 Hz）を、seed を変えて9本作る:

  * 自己相関のピーク幅はおよそ 1/帯域幅 = **0.18 ms**。測りたい 1〜10 ms に
    対して十分に鋭い。
  * seed が違う乱数列どうしはほとんど相関しない（生成時に実測して表示する）。
    だから隣の台の音には合わない。

さらに鳴らす時刻も台ごとにずらす。これは分離のためというより、反射や
マスキングの影響を減らすため。

解析は**生波形の相関（整合フィルタ）**で行う。包絡だとバーストの形しか
見ないので分解能が 0.25 秒ぶんに鈍る ―― 実測でピーク比 1.08 対 6.24 と
はっきり差が出た。

出るもの
--------
`songs/calib/` に曲と同じ形の一式。あとは通常の経路にそのまま乗る:

    orchestra song build calib
    orchestra show deploy calib
    orchestra show run calib --record r01
    orchestra calibrate skew calib --recording calib-r01.wav --mode waveform
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from orchestra.audio.render import OUTPUT_RATE, write_wav_f32  # noqa: E402
from orchestra.config import Context  # noqa: E402

BURST_S = 0.25
F_LO, F_HI = 300.0, 6000.0    # 16kHz なので可聴上限は 8kHz。余裕を見て 6k まで
SLOT_S = 1.5                  # 台と台の間隔。反射が収まるだけ空ける
ROUNDS = 3                    # 平均するために繰り返す
BPM = 100.0


def burst(seed: int, rate: int = OUTPUT_RATE) -> np.ndarray:
    """台ごとに**別の**帯域制限ノイズバースト。

    最初はチャープにしたが、それでは足りなかった。全機が同じ波形を 1.5 秒の
    等間隔で鳴らすと、**信号全体が周期 1.5 秒で周期的になる**。相関は隣の台の
    バーストにも同じ高さで合ってしまい、実際 lag が 1.5 秒違いの2群に割れた。
    自分で「音楽は繰り返すから測れない」と書いておきながら、測定用の信号を
    繰り返しで作っていた。

    乱数列は seed が違えば互いにほとんど相関しない。だから台ごとに別の seed を
    与えれば、隣の台のバーストには合わなくなる。自己相関は帯域幅の逆数まで
    鋭くなる（300〜6000 Hz なら ~0.18 ms）ので、分解能はチャープと同じ。

    帯域制限は周波数領域で切る。スピーカーが出せない超低域と、16 kHz で
    折り返す高域を入れても、部屋を暖めるだけで測定には効かない。
    """
    n = int(BURST_S * rate)
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / rate)
    X[(f < F_LO) | (f > F_HI)] = 0.0
    x = np.fft.irfft(X, n)
    x /= max(float(np.abs(x).max()), 1e-9)
    edge = int(0.01 * rate)
    w = np.ones(n)
    w[:edge] = np.hanning(edge * 2)[:edge]
    w[-edge:] = np.hanning(edge * 2)[edge:]
    return (x * w).astype(np.float32)


def orthogonality(sigs: list[np.ndarray]) -> float:
    """一番まずい組み合わせの相互相関。1に近いと分離できない。"""
    worst = 0.0
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            a, b = sigs[i], sigs[j]
            c = np.correlate(a, b, mode="full")
            c /= np.linalg.norm(a) * np.linalg.norm(b)
            worst = max(worst, float(np.abs(c).max()))
    return worst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="songs/calib")
    ap.add_argument("--gain", type=float, default=0.25,
                    help="lower this if the recording clips (0.6 did)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    ctx = Context.load(root)
    robots = list(ctx.robots.enabled)
    out_dir = (root / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    (out_dir / "audio").mkdir(parents=True, exist_ok=True)

    sigs = [burst(1000 + i) for i in range(len(robots))]
    rate = OUTPUT_RATE
    worst = orthogonality(sigs)
    total_s = SLOT_S * len(robots) * ROUNDS + 2.0
    n_total = int(total_s * rate)

    print(f"calibration tone: band-limited noise {F_LO:.0f}-{F_HI:.0f}Hz / "
          f"{BURST_S * 1000:.0f}ms, **a distinct waveform per robot**, "
          f"{len(robots)} robots x {ROUNDS} rounds, {total_s:.1f}s total")
    print(f"  expected autocorrelation peak width ~{1000.0 / (F_HI - F_LO):.2f} ms")
    print(f"  worst cross-correlation between robots {worst:.3f}  "
          + ("-- orthogonal enough" if worst < 0.2
             else "** they may not separate **") + "\n")

    for i, r in enumerate(robots):
        buf = np.zeros((n_total, 2), dtype=np.float32)
        c = sigs[i]
        for k in range(ROUNDS):
            # k 巡目の i 番目。台ごとに時刻がずれるので、録音の中で必ず孤立する。
            # 波形自体も台ごとに違うので、孤立していなくても分離できる。
            at = 1.0 + (k * len(robots) + i) * SLOT_S
            a = int(at * rate)
            b = min(n_total, a + len(c))
            buf[a:b, 0] = c[: b - a] * args.gain
            buf[a:b, 1] = c[: b - a] * args.gain
        p = write_wav_f32(out_dir / "audio" / f"{r.id}.wav", buf, samplerate=rate)
        firsts = [1.0 + (k * len(robots) + i) * SLOT_S for k in range(ROUNDS)]
        print(f"  {r.id}  {p.name:<12} sounds at {', '.join(f'{x:.1f}' for x in firsts)} s")

    # song.yaml。動きは最小限 ―― ここで測りたいのは音だけなので、頭が動いて
    # 風切り音を立てるほうが邪魔になる。
    parts = []
    cast = []
    for r in robots:
        parts.append(
            # 動かない役にする。測りたいのは音だけで、頭が動くと風切り音と
            # サーボの音がマイクに乗り、相関の邪魔になる。
            f"  {r.id}:\n"
            f"    kind: idle\n"
            f"    instrument: calibration\n"
            f"    audio: {r.id}.wav\n"
        )
        cast.append(f"  {r.id}: {r.id}")

    (out_dir / "song.yaml").write_text(
        "# generated by tools/make_calibration.py\n"
        "#\n"
        "# A 'song' that exists only to measure arrival times: chirps, not a\n"
        "# melody. The real piece cannot measure this -- music repeats, so the\n"
        "# correlation never produces a clean peak. Here each robot sounds at a\n"
        "# different moment, so it is always isolated in the recording.\n"
        "song:\n"
        "  name: calib\n"
        '  title: "Audio arrival-time calibration"\n'
        f"  bpm: {BPM:g}\n"
        "  meter: [4, 4]\n"
        f"  bars: {int(total_s * BPM / 60.0 / 4.0) + 1}\n"
        "  lead_in_s: 2.0\n"
        "  tail_s: 1.0\n"
        "\n"
        "parts:\n" + "".join(parts) + "\ncast:\n" + "\n".join(cast) + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote: {out_dir}")
    print("\n  orchestra song build calib")
    print("  orchestra show deploy calib")
    print("  orchestra show run calib --record r01")
    print("  orchestra calibrate skew calib --recording calib-r01.wav")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
