#!/usr/bin/env python3
"""`pingtest` の録音から、台ごとの音の到着時刻を測る。

    python tools/make_pingtest.py                       # 測定音を作る
    orchestra song build pingtest && orchestra show deploy pingtest
    orchestra show run pingtest --record r02 --skip-preflight   # 位置を変えて数回
    python tools/measure_ping.py

なぜ `orchestra calibrate skew` ではなくこれか
--------------------------------------------
`calibrate skew` は**波形の相互相関**で測る。PC 上では正しい方法だが、
実機のマイクを通すと効かない ―― Reachy のマイクは音声用の前段 DSP
（エコー除去・雑音抑圧・AGC）を通るので、振幅の時間変化は残っても
**波形の位相は保たれない**。7箇所から録って corr 0.010-0.020（=無相関）だった。

ここでは:
  * 測定音を**トーン**にする（雑音抑圧に消されない。`make_pingtest.py` 参照）
  * 台ごとに違う周波数を割り当て、**狭帯域フィルタでその台だけを抜き出す**。
    遠い台が床ノイズに埋もれる問題がこれで大きく改善する（SNR x43-x3697）
  * 波形ではなく**包絡線の立ち上がり**（ピークの50%を最初に超える点）で測る
  * 8回繰り返し、中央値と**四分位幅**を出す。四分位幅がその位置での再現性

★出た数字をそのまま `audio_lead_ms` に書いてはいけない★
------------------------------------------------------
到着時刻には距離（343 m/s = 2.9 ms/m）と反射が乗っている。**1箇所の測定は
その1点でしか正しくない。** 複数の位置から測って、位置を変えても動かない
成分だけが機体固有の候補。実測ではその「位置差」が 7-62 ms あり、
測ろうとしている量より大きい ―― だから `audio_lead_ms` は 0.0 のままにしてある。
"""

from __future__ import annotations

import argparse
import glob
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

# 定数は測定音を作った側から読む。二重管理にすると必ずずれる。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_pingtest import FIRST, FREQS, GAP, ROUNDS  # noqa: E402

NROB = len(FREQS)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    w = wave.open(str(path))
    n, sr, ch = w.getnframes(), w.getframerate(), w.getnchannels()
    x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(float) / 32768
    return (x.reshape(-1, ch).mean(axis=1) if ch > 1 else x), sr


def narrow(x: np.ndarray, sr: int, f: float, bw: float = 90.0) -> np.ndarray:
    """f の周りだけ残して包絡線にする。ここが遠い台を拾う鍵。"""
    from scipy.signal import butter, sosfiltfilt
    sos = butter(4, [(f - bw) / (sr / 2), (f + bw) / (sr / 2)],
                 btype="band", output="sos")
    e = np.abs(sosfiltfilt(sos, x))
    k = max(1, int(sr * 0.003))
    return np.convolve(e, np.ones(k) / k, "same")


def align(total: np.ndarray, sr: int) -> tuple[int, float]:
    """期待するインパルス列との相互相関で録音の頭出しをする。"""
    dur = FIRST + GAP * NROB * ROUNDS + 1.0
    exp = np.zeros(int(dur * sr))
    for k in range(NROB):
        for r in range(ROUNDS):
            i = int((FIRST + GAP * (k + NROB * r)) * sr)
            exp[i:i + int(0.04 * sr)] = 1.0
    a, b = total - total.mean(), exp - exp.mean()
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    lim = len(a) - len(b)
    if lim <= 0:
        return 0, 0.0
    k = int(np.argmax(c[:lim]))
    denom = np.linalg.norm(a[k:k + len(b)]) * np.linalg.norm(b) + 1e-12
    return k, float(c[k] / denom)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recordings", default="recordings/pingtest-r*.wav")
    ap.add_argument("--max-iqr", type=float, default=8.0,
                    help="この四分位幅[ms]を超えたら、その位置の測定は採らない")
    ap.add_argument("--min-snr", type=float, default=8.0)
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    paths = sorted(glob.glob(str(root / args.recordings)))
    if not paths:
        print(f"no recordings matched {args.recordings}", file=sys.stderr)
        return 1

    rows: dict[str, dict[str, float]] = {}
    for p in paths:
        name = Path(p).stem.replace("pingtest-", "")
        x, sr = read_wav(Path(p))
        envs = [narrow(x, sr, f) for f in FREQS]
        off, q = align(np.sum(envs, axis=0), sr)
        print(f"\n--- {name} のマイク  (頭出し {off/sr:.3f}s, 一致度 {q:.3f}) ---")
        print("  台   周波数   到着ずれの中央値  四分位幅   n   SNR      判定")
        for k, f in enumerate(FREQS):
            e = envs[k]
            floor = float(np.median(e))
            devs, snrs = [], []
            for r in range(ROUNDS):
                c = off + int((FIRST + GAP * (k + NROB * r)) * sr)
                lo, hi = c - int(0.06 * sr), c + int(0.06 * sr)
                if lo < 0 or hi >= len(e):
                    continue
                seg = e[lo:hi]
                pk = float(seg.max())
                if pk < floor * 5:
                    continue
                j = int(np.argmax(seg >= pk * 0.5))     # 立ち上がり
                devs.append((j - int(0.06 * sr)) / sr * 1000)
                snrs.append(pk / max(floor, 1e-12))
            rid = f"r{k+1:02d}"
            if len(devs) < 4:
                print(f"  {rid}  {f:>5}Hz   検出 {len(devs)}/{ROUNDS} 回 ―― SNR不足")
                continue
            med = float(np.median(devs))
            iqr = float(np.percentile(devs, 75) - np.percentile(devs, 25))
            sn = float(np.median(snrs))
            ok = iqr <= args.max_iqr and sn >= args.min_snr and len(devs) >= 6
            if ok:
                rows.setdefault(rid, {})[name] = med
            print(f"  {rid}  {f:>5}Hz      {med:+8.1f} ms   {iqr:6.1f}  "
                  f"{len(devs):2d}  x{sn:7.1f}  {'OK' if ok else '--'}")

    print("\n=== 位置をまたいだ比較（OK のみ）===")
    names = sorted({n for v in rows.values() for n in v})
    print("  台  " + "".join(f"{n:>10}" for n in names) + "      最小   位置差")
    swings = []
    for rid in sorted(rows):
        vals = [rows[rid].get(n) for n in names]
        good = [v for v in vals if v is not None]
        line = "".join(f"{(f'{v:+.1f}' if v is not None else '-'):>10}" for v in vals)
        if len(good) >= 2:
            sw = max(good) - min(good)
            swings.append(sw)
            print(f"  {rid} {line} {min(good, key=abs):+8.1f} {sw:8.1f}")
        else:
            print(f"  {rid} {line}   （1箇所のみ）")
    if swings:
        print(f"\n位置差の最大 {max(swings):.1f} ms / 最小 {min(swings):.1f} ms。")
        print("空気の伝搬は 1.5m の卓で約 4.4 ms。**位置差がそれを大きく超えるなら、"
              "測っているのは部屋であって機体ではない。**")
        print("この列の数字を audio_lead_ms に書かないこと ―― 録音した1点でしか正しくない。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
