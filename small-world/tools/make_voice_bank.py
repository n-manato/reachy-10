#!/usr/bin/env python3
"""歌詞の各行を、ニューラル音声合成に喋らせて手元に貯める。

    python tools/make_voice_bank.py

**ここだけ外部ネットワークを使う。** Microsoft Edge の音声合成に行を喋らせて、
音声と単語ごとの時刻をディスクに落とす。取得は1回きりで、以降はキャッシュ
から読む。演奏時に鳴るのは焼き上げた WAV なので、ロボット側の閉じた
ネットワークには何も足さない。

なぜ SAPI をやめたか
--------------------
最初は Windows 内蔵の SAPI（Zira）で音節ごとに合成した。歌にならなかった。
実測すると理由がはっきりしていて、

  * Zira は**大人の声**（f0 中央 190Hz）。旋律は 294〜784Hz なので1オクターブ
    以上持ち上げる必要があり、そこまで動かすと何をしても壊れる
  * 音節を単独で喋らせて並べたので、**渡り**が無い。人はそう喋らない

Edge には `en-US-AnaNeural` / `en-GB-MaisieNeural` という**子供の声**がある。
実測 f0 中央 295Hz / 297Hz で旋律域とほぼ重なる。しかも**行をまるごと**
喋らせるので渡りが残る。この2つで前の失敗の主因が両方消える。

喋る速さを落としてある理由
--------------------------
歌の1音節は 0.375〜0.75 秒だが、普通の速さの発話は 0.2 秒前後しかない。
引き伸ばす量が少ないほど素材のまま残るので、あらかじめゆっくり喋らせておく。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from orchestra.audio.lyrics import all_lines  # noqa: E402
from orchestra.audio.singer import VOICE_STYLES, analyse, syllable_spans  # noqa: E402
from orchestra.audio.tts import TTSUnavailable, fetch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="samples/voice")
    ap.add_argument("--offline", action="store_true",
                    help="do not fetch; verify against what is already cached")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    out = (root / args.out) if not Path(args.out).is_absolute() else Path(args.out)

    lines = all_lines()
    print(f"{len(lines)} lines of lyrics x {len(VOICE_STYLES)} voices -> {out}\n")

    bad: list[str] = []
    for preset, st in VOICE_STYLES.items():
        print(f"=== {preset}  {st.voice}  rate={st.speak_rate}")
        print(f"{'len s':>7}{'words':>6}{'syll':>5}{'f0 med':>8}{'voiced%':>8}  text")
        for line in lines:
            try:
                u = fetch(line.text, st.voice, out, rate=st.speak_rate,
                          allow_network=not args.offline)
            except TTSUnavailable as e:
                bad.append(f"{preset} {line.text!r}: {e}")
                print(f"{'--':>7}{'':>4}{'':>5}{'':>8}{'':>7}  {line.text}")
                continue
            except Exception as e:  # noqa: BLE001
                bad.append(f"{preset} {line.text!r}: {type(e).__name__}: {e}")
                continue

            # 聞かずに分かることだけを見る ―― 無音でないか、単語の数が
            # 歌詞と合っているか、音節に切れるか。ここが崩れていると
            # 歌わせても言葉にならない。
            note = ""
            try:
                spans = syllable_spans(u, line)
                if len(spans) != line.n_syllables:
                    note = f"  ** syllables {len(spans)} != {line.n_syllables}"
            except ValueError as e:
                spans = []
                note = "  ★" + str(e).splitlines()[0]
            if note:
                bad.append(f"{preset} {line.text!r}:{note.strip()}")

            fr = analyse(u.audio, u.rate)
            v = fr.f0[fr.f0 > 0]
            if float(np.abs(u.audio).max()) < 1e-3:
                bad.append(f"{preset} {line.text!r}: silent")
            print(f"{u.duration:>7.2f}{len(u.words):>4}{len(spans):>5}"
                  f"{(np.median(v) if len(v) else 0):>8.0f}"
                  f"{(len(v) / max(1, fr.n) * 100):>7.0f}  {line.text}{note}")
        print()

    if bad:
        print("problems:")
        for b in bad:
            print(f"  {b}")
        return 1

    print("NOTE: this only shows the material is not broken. Whether it is\n"
          "  intelligible as singing can only be judged by listening.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
