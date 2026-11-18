#!/usr/bin/env python3
"""ステムを1本に混ぜて、9台に配る前に耳で確かめる。

    python tools/preview_song.py songs/lamban
    python tools/preview_song.py songs/lamban --only balafon kora

9台へ配備して走らせるのは1回あたり2分近く掛かるうえ、部屋の音響が混ざる。
編曲や歌の出来を判断するだけなら、手元で混ぜたほうが速くて正確に分かる。

**これは本番の音ではない。** 実際は9台が別々の場所から鳴らすので、定位も
残響も違う。ここで分かるのは「素材と編曲が意図どおりか」まで。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from orchestra.audio.render import OUTPUT_RATE, read_wav_f32, write_wav_f32  # noqa: E402
from orchestra.music.score import load_score  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("song", nargs="?", default="songs/lamban")
    ap.add_argument("--out", default=None, help="defaults to <song>/preview.wav")
    ap.add_argument("--only", nargs="*", default=None, help="mix only these parts")
    ap.add_argument("--seconds", type=float, default=0.0, help="write only this many seconds from the start")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    song_dir = (root / args.song) if not Path(args.song).is_absolute() else Path(args.song)
    score = load_score(song_dir)

    parts = {n: p for n, p in score.parts.items() if p.audio}
    if args.only:
        missing = [n for n in args.only if n not in parts]
        if missing:
            print(f"unknown part(s): {', '.join(missing)}\n"
                  f"  available: {', '.join(parts)}", file=sys.stderr)
            return 1
        parts = {n: parts[n] for n in args.only}

    mix = None
    print(f"{'part':<15}{'peak':>8}{'rms':>9}  file")
    for name, part in parts.items():
        p = song_dir / "audio" / part.audio
        if not p.exists():
            print(f"{name:<15}{'--':>8}{'--':>9}  {part.audio} is missing")
            continue
        x, sr = read_wav_f32(p)
        if sr != OUTPUT_RATE:
            print(f"{name}: wrong sample rate {sr}", file=sys.stderr)
            return 1
        if mix is None:
            mix = np.zeros_like(x)
        if len(x) > len(mix):
            mix = np.pad(mix, ((0, len(x) - len(mix)), (0, 0)))
        mix[:len(x)] += x
        print(f"{name:<15}{np.abs(x).max():>8.3f}{np.sqrt((x ** 2).mean()):>9.4f}  {part.audio}")

    if mix is None:
        print("nothing to mix", file=sys.stderr)
        return 1
    if args.seconds > 0:
        mix = mix[:int(args.seconds * OUTPUT_RATE)]

    peak = float(np.abs(mix).max())
    # 合算しただけだと 1.0 を超える。**割るだけ**にして、音の形は変えない
    # （リミッタを掛けると、混ぜたときの粗が隠れて判断できなくなる）。
    gain = min(1.0, 0.98 / peak) if peak > 0 else 1.0
    out = Path(args.out) if args.out else (song_dir / "preview.wav")
    write_wav_f32(out, mix, gain=gain)
    print(f"\n{len(parts)} stems, summed peak {peak:.3f} -> x{gain:.3f} / "
          f"{len(mix)/OUTPUT_RATE:.1f}s\n{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
