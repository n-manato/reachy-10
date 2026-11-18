#!/usr/bin/env python3
"""マンデ楽器の音源を合成して `samples/mande/` に書き出す。

    python tools/make_mande_samples.py

参考元（DEV/Reachy_10）は Windows の `gm.dls` から実録音を抜いていたが、
GM には kora も ngoni も balafon も djembe も dunun も無い。ここでは
`orchestra/audio/mande.py` が数値合成する（理由はそちらの docstring に）。

出力形式は**参考元の `samples/generated/manifest.json` と同じスキーマ**なので、
`SampleLibrary` は無改造で読める。全楽器を `melodic` 側に置いているのは、
`percussion` 側が 1楽器 1波形・音程固定で、打法の打ち分けができないため。

各エントリに `"tuned": true` を立てている。合成側で音律を焼き込んでいるので、
`Instrument.auto_tune()`（12平均律からのずれを「誤差」とみなして補正する）を
通してはいけない。
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from orchestra.audio import mande  # noqa: E402


def write_wav16(path: Path, x: np.ndarray, sr: int) -> int:
    """モノラル 16bit PCM。`sampler._read_wav_mono` が読む形式。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(x, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return len(pcm)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="samples/mande")
    ap.add_argument("--only", nargs="*", default=None,
                    help="この楽器だけ作り直す（既定は全部）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    out = (root / args.out) if not Path(args.out).is_absolute() else Path(args.out)

    names = args.only or list(mande.BUILDERS)
    unknown = [n for n in names if n not in mande.BUILDERS]
    if unknown:
        print(f"error: unknown instrument(s) {unknown}. "
              f"Available: {', '.join(sorted(mande.BUILDERS))}", file=sys.stderr)
        return 1

    man_path = out / "manifest.json"
    manifest = (json.loads(man_path.read_text(encoding="utf-8"))
                if man_path.exists() else
                {"source": "synthesised by orchestra/audio/mande.py",
                 "samplerate": mande.SR, "melodic": {}, "percussion": {}})

    print(f"synthesising {len(names)} instrument(s) at {mande.SR} Hz -> {out}\n")
    print(f"{'instrument':<12}{'zones':>6}{'range (Hz)':>18}{'total':>10}  note")

    for name in names:
        inst = mande.BUILDERS[name]()
        zones = []
        total = 0
        for s in inst.strokes:
            rel = f"melodic/{name}_{s.zone}_{s.label}.wav"
            frames = write_wav16(out / rel, s.audio, mande.SR)
            total += frames
            zones.append({
                "file": rel,
                "unity_note": int(s.zone),
                "fine_tune_cents": 0.0,
                "samplerate": mande.SR,
                "channels": 1,
                "bits": 16,
                "frames": frames,
                "duration": round(frames / mande.SR, 4),
                # ループは張らない。合成側で減衰しきるまで書き出しているので、
                # サンプラーはループ無し経路（読み切ったら終わり）で正しく鳴る。
                "loop": None,
                "wave_name": s.label,
                "hz": round(float(s.hz), 3),
            })
        manifest["melodic"][name] = {
            "synth": f"orchestra.audio.mande.build_{name}",
            # ★ auto_tune を通さないための申告 ★ 音律も打法も合成時に焼いてある。
            "tuned": True,
            "kind": inst.kind,
            "note": inst.note,
            "zones": zones,
        }
        hzs = [s.hz for s in inst.strokes if s.hz > 0]
        span = f"{min(hzs):.0f}-{max(hzs):.0f}" if hzs else "-"
        print(f"{name:<12}{len(zones):>6}{span:>18}{total/mande.SR:>9.1f}s  {inst.note}")

    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    n_wav = sum(len(v["zones"]) for v in manifest["melodic"].values())
    size = sum(p.stat().st_size for p in out.rglob("*.wav")) / 1024 / 1024
    print(f"\n{len(manifest['melodic'])} instruments / {n_wav} wav / {size:.1f}MB")
    print(f"manifest: {man_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
