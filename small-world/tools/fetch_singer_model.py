#!/usr/bin/env python3
"""歌声合成のモデルを取ってきて展開する。

    python tools/fetch_singer_model.py

**モデルはリポジトリに入れない。** ボイスバンクは CC BY-NC-ND 4.0 +
Commons Clause、ボコーダは CC BY-NC-SA 4.0 で、どちらも**再配布が禁止**
されている。`gm.dls` の抽出物と同じ扱いで、置くのは取得スクリプトだけ。
各自が自分で取得する。

  * 非営利・個人利用は無料。大学の内部デモはここに入る
  * 営利利用は別途ライセンスが要る
  * 公開配信や公開演奏は、曲の著作権とは**別に**判断が要る

取得は1回きりで、あとはオフラインで動く。演奏そのものはここで作った WAV を
鳴らすだけなので、ロボット側の閉じたネットワークには何も足さない。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "models" / "diffsinger"

SOURCES = {
    "voicebank": {
        "url": "https://github.com/spicytigermeat/tiger_diffsinger/releases/"
               "download/v106/TIGER_DS_v106_PACK.zip",
        "zip": "tiger_v106.zip",
        "into": "tiger",
        "mb": 530,
        "licence": "CC BY-NC-ND 4.0 + Commons Clause "
                   "(non-commercial only, no redistribution)",
    },
    "vocoder": {
        "url": "https://github.com/openvpi/vocoders/releases/"
               "download/nsf-hifigan-v1/nsf_hifigan_onnx_20221211.zip",
        "zip": "nsf_hifigan_onnx.zip",
        "into": "vocoder",
        "mb": 50,
        "licence": "CC BY-NC-SA 4.0（OpenVPI Team）",
    },
}


def _download(url: str, path: Path) -> None:
    if path.exists():
        print(f"  already here: {path.name} ({path.stat().st_size / 1e6:.0f} MB)")
        return
    print(f"  fetching: {url}")
    tmp = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {done/1e6:6.0f} / {total/1e6:.0f} MB", end="", flush=True)
        print()
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default=str(DEST))
    ap.add_argument("--keep-zip", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    print("fetching the singing-synthesis models -- about 580 MB in total.\n")
    for name, s in SOURCES.items():
        print(f"[{name}] {s['mb']} MB  {s['licence']}")
        out = dest / s["into"]
        if out.exists() and not args.force:
            print(f"  already extracted: {out}")
            continue
        z = dest / s["zip"]
        try:
            _download(s["url"], z)
        except Exception as e:  # noqa: BLE001
            print(f"  failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        if out.exists():
            shutil.rmtree(out)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(out)
        print(f"  extracted: {out}")
        if not args.keep_zip:
            z.unlink(missing_ok=True)

    print(f"\n{dest}\n"
          "NOTE: these models may not be redistributed, so they stay out of "
          "the repository (models/ is gitignored).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
