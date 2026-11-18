"""歌詞のフレーズを、ニューラル音声合成に喋らせて手元に貯める。

なぜ SAPI をやめたか
--------------------
最初は Windows 内蔵の SAPI（Microsoft Zira）を使った。理由は「このPCに既に
ある物だけで済む」。だが出来上がった歌が聞くに堪えなかった。原因は3つあって、
どれも SAPI 側にある:

1. **大人の声だった。** Zira の基本周波数は中央値 190Hz 前後。旋律は D4-G5
   (294-784Hz) なので、1オクターブ以上持ち上げる必要がある。そこまで動かすと
   何をしても無理が出る。
2. **連結合成なので、単独の音節を並べると継ぎ目だらけになる。** 「イッツ」
   「ア」「ワールド」を別々に喋らせて繋ぐと、渡り（coarticulation）が無い。
   人はそう喋らないので、音程が完璧でも言葉に聞こえない。
3. 22kHz の古い素材なので、加工の余地が少ない。

Edge のニューラル音声には **`en-US-AnaNeural` と `en-GB-MaisieNeural` という
子供の声**がある。実測した基本周波数の中央値は 295Hz / 297Hz ―― 旋律域と
ほぼ重なる。**持ち上げる量が1オクターブから数半音になる**ので、1 が消える。
そして行まるごと喋らせれば 2 も消える。

ネットワークについて
--------------------
ここだけは外部（Microsoft の音声合成）に繋ぐ。ただし **取得は1回きりで、
結果はディスクに貯まる**。演奏時に鳴るのは焼き上げた WAV なので、本番の
経路には何も足さない ―― ロボット側の閉じたネットワークはそのまま。

キャッシュは `wave`（標準ライブラリ）で書く。取得のときだけ mp3 を解く
必要があって `soundfile` を使うが、**2回目以降は標準ライブラリだけで読める**。

単語の時刻について
------------------
`boundary="WordBoundary"` を渡すと、合成器が**どの単語をいつ喋ったか**を
返してくる。これが無いと、出来上がった音声のどこが "world" なのか分からず、
音符に載せられない。既定は `SentenceBoundary` で単語が来ないので、明示的に
指定すること（ここで一度踏んだ）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("orchestra.audio.tts")

# 合成器が返す音声のレート。Edge は 24kHz mono mp3 で返す。
TTS_RATE = 24000

# 子供の声。実測 f0 中央値 295Hz / 297Hz。
VOICE_ANA = "en-US-AnaNeural"        # 米。Cartoon / "Cute"
VOICE_MAISIE = "en-GB-MaisieNeural"  # 英。別人に聞こえるので2声目に使う


class TTSUnavailable(RuntimeError):
    """キャッシュに無く、取りにも行けない。"""


@dataclass(frozen=True)
class WordMark:
    """合成器が報告した、1単語の位置。"""

    text: str
    start: float   # 秒
    dur: float


@dataclass
class Utterance:
    """喋らせた1フレーズ。"""

    text: str
    voice: str
    audio: np.ndarray          # mono float32
    rate: int
    words: list[WordMark]

    @property
    def duration(self) -> float:
        return len(self.audio) / self.rate

    def word_spans(self) -> list[tuple[float, float]]:
        """単語ごとの [開始, 終了) を、隙間なく単調に並べ直す。

        合成器の `duration` をそのまま終端に使うと、単語の間に無音の隙間が
        できる。歌に載せるときは**時間軸を連続に保ちたい**（隙間で切ると、
        語尾の子音や渡りがそこで落ちる）ので、次の単語の開始までを前の単語の
        持ち分にする。読点の休符も、そのぶん前の単語に含まれる ―― 伸縮の
        重み付けが無音を勝手に潰すので、これで困らない。
        """
        if not self.words:
            return []
        out: list[tuple[float, float]] = []
        for i, w in enumerate(self.words):
            end = self.words[i + 1].start if i + 1 < len(self.words) else w.start + w.dur
            out.append((w.start, max(end, w.start + 1e-3)))
        return out


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------


def _key(text: str, voice: str, rate: str, pitch: str) -> str:
    h = hashlib.sha1(f"{text}\x00{voice}\x00{rate}\x00{pitch}".encode()).hexdigest()
    return h[:12]


def _write_cache(d: Path, key: str, u: Utterance) -> None:
    d.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(u.audio, -1.0, 1.0)
    with wave.open(str(d / f"{key}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(u.rate)
        w.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    (d / f"{key}.json").write_text(json.dumps({
        "text": u.text, "voice": u.voice, "rate": u.rate,
        "words": [{"text": w.text, "start": w.start, "dur": w.dur} for w in u.words],
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_cache(d: Path, key: str) -> Utterance | None:
    wav, meta = d / f"{key}.wav", d / f"{key}.json"
    if not (wav.exists() and meta.exists()):
        return None
    m = json.loads(meta.read_text(encoding="utf-8"))
    with wave.open(str(wav), "rb") as w:
        n, sr = w.getnframes(), w.getframerate()
        x = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
    return Utterance(text=m["text"], voice=m["voice"], audio=x, rate=sr,
                     words=[WordMark(k["text"], k["start"], k["dur"]) for k in m["words"]])


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------


def _synthesise(text: str, voice: str, rate: str, pitch: str) -> Utterance:
    """外部の合成器から音声と単語の時刻を取る。ここだけネットワークを使う。"""
    import asyncio

    import edge_tts

    async def run():
        c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                 boundary="WordBoundary")  # ★既定では単語が来ない
        buf = bytearray()
        marks: list[WordMark] = []
        async for ch in c.stream():
            if ch["type"] == "audio":
                buf.extend(ch["data"])
            elif ch["type"] == "WordBoundary":
                marks.append(WordMark(ch["text"], ch["offset"] / 1e7, ch["duration"] / 1e7))
        return bytes(buf), marks

    mp3, marks = asyncio.run(run())
    if not mp3:
        raise TTSUnavailable(f"no audio came back for {text!r}")

    import io

    import soundfile as sf
    x, sr = sf.read(io.BytesIO(mp3), dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return Utterance(text=text, voice=voice, audio=np.ascontiguousarray(x), rate=int(sr),
                     words=marks)


def fetch(text: str, voice: str, cache_dir: str | Path, *,
          rate: str = "-20%", pitch: str = "+0Hz",
          allow_network: bool = True) -> Utterance:
    """フレーズを1つ手に入れる。キャッシュにあればネットワークを使わない。

    `rate` を遅めにしてあるのは、**母音を長く取っておきたい**から。歌では
    1音節が 0.375〜0.75 秒になるが、普通の速さの発話は 0.2 秒前後しかない。
    引き伸ばす量が少ないほど素材のまま残る。
    """
    d = Path(cache_dir)
    key = _key(text, voice, rate, pitch)
    u = _read_cache(d, key)
    if u is not None:
        return u
    if not allow_network:
        raise TTSUnavailable(
            f"not in the cache: {text!r} ({voice})\n"
            f"  Fetching it needs the outside network. Run "
            f"`python tools/make_voice_bank.py` once while connected to the "
            f"internet; after that everything is read from {d}")
    log.info("synthesising: %s (%s)", text, voice)
    u = _synthesise(text, voice, rate, pitch)
    _write_cache(d, key, u)
    return u
