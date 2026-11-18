#!/usr/bin/env python3
"""合成した音源を1本の WAV にまとめて、耳で確かめられるようにする。

    python tools/audition_samples.py
    python tools/audition_samples.py --only djembe balafon --out /tmp/x.wav

合成音の説得力はこの企画で一番の不確実要素なので、`make_mande_samples.py` を
いじったら必ずここを通して聴くこと。数値（音程誤差・帯域）が合っていても
「らしく聞こえない」ことは普通にある。

出力は本番と同じ 16kHz ステレオ ―― ロボットのスピーカーから出るときに何が
失われるかを含めて聴くため。22050Hz で聴くと高域があるので判断を誤る。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from orchestra.audio.render import OUTPUT_RATE, _resample_to_output, write_wav_f32  # noqa: E402
from orchestra.audio.sampler import SampleLibrary, play_note  # noqa: E402
from orchestra.audio import mande  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", default="samples/mande")
    ap.add_argument("--out", default="samples/audition.wav")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--gap", type=float, default=0.22, help="音と音の間 [s]")
    ap.add_argument("--rest", type=float, default=0.9, help="楽器と楽器の間 [s]")
    ap.add_argument("--dur", type=float, default=0.55, help="1音の長さ [s]")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    lib = SampleLibrary(root / args.samples)
    names = args.only or [n for n in mande.BUILDERS if n in lib.manifest["melodic"]]

    sr = mande.SR
    chunks: list[np.ndarray] = []
    print(f"{'instrument':<12}{'zones':>6}{'seconds':>9}")
    for name in names:
        entry = lib.manifest["melodic"][name]
        inst = lib.melodic(name, entry.get("kind", "struck"))
        piece: list[np.ndarray] = []
        for z in inst.zones:
            x, _ = play_note(inst, z.unity_note, args.dur, velocity=1.0,
                             samplerate=sr, tail_s=0.35)
            piece.append(x)
            piece.append(np.zeros(int(args.gap * sr)))
        piece.append(np.zeros(int(args.rest * sr)))
        block = np.concatenate(piece)
        chunks.append(block)
        print(f"{name:<12}{len(inst.zones):>6}{len(block)/sr:>8.1f}s")

    work = np.concatenate(chunks)
    audio = _resample_to_output(work, sr, OUTPUT_RATE)
    peak = float(np.abs(audio).max())
    if peak > 0:
        audio = audio * (0.9 / peak)
    stereo = np.column_stack([audio, audio]).astype(np.float32)

    out = (root / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    p = write_wav_f32(out, stereo)
    print(f"\n{len(work)/sr:.1f}s  {OUTPUT_RATE}Hz stereo  ->  {p}")
    print("本番と同じ 16kHz。8kHz より上は最初から無い前提で聴くこと。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
