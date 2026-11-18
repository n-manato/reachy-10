"""喋った1行を、音程と長さだけ差し替えて歌にする。

前の方式（音節ごとの LPC 再合成）を捨てた理由
---------------------------------------------
最初は音節ごとに合成し、LPC で声道と音源に分け、音源をパルス列に差し替えて
歌わせた。理屈は正しいが、出来上がったものは聞くに堪えなかった。原因は4つ
あって、**どれも別々に効いていた**:

1. 音節を単独で合成して並べたので、**渡り**（前後の音で口の形が変わること）が
   無い。人はそう喋らないので、音程が完璧でも言葉に聞こえない
2. 素材が大人の声（f0 中央 190Hz）だったので、旋律域（294〜784Hz）まで
   1オクターブ以上持ち上げる必要があった
3. 22次の全極フィルタでは声道の形が粗く、細部が落ちる
4. パルス列の音源はいかにもボコーダの音になる。しかも有声/無声を2値で
   決めるので、摩擦音や息が壊れる

今の方式はこの4つを全部外す:

1. **行をまるごと自然に喋らせる。** 継ぎ目そのものが無い
2. **素材が子供の声**（`en-US-AnaNeural`、実測 f0 中央 295Hz）。旋律域と
   ほぼ重なるので、動かす量は1オクターブから数半音になる
3. **WORLD** の CheapTrick は全極近似ではないので、包絡の細部が残る
4. WORLD は**非周期性**を帯域ごとの比率として持つ。2値ではないので、
   摩擦音も息も連続量のまま保たれる

やること
--------
1. 喋った行を WORLD で f0 / スペクトル包絡 / 非周期性 に分ける
2. 時間軸を**音節の境界で留めて**張り替える。伸ばすのは母音の芯だけで、
   子音は自然な速さのまま（一様に伸ばすと「ssss」になる）
3. f0 を楽譜の音程に差し替える（ポルタメント・ビブラート・しゃくり付き）
4. 元の包絡と非周期性のまま再合成する

**包絡は触らない**ので母音は母音のまま残る。LPC でやりたかったことと同じで、
精度だけが違う。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .lyrics import Line, LinePlan
from .tts import TTS_RATE, VOICE_ANA, VOICE_MAISIE, Utterance, fetch

log = logging.getLogger("orchestra.audio.singer")

FRAME_PERIOD = 5.0        # ms。WORLD の解析間隔
F0_FLOOR = 100.0
F0_CEIL = 900.0           # 子供の声と旋律の上端(G5=784Hz)を含む


# ---------------------------------------------------------------------------
# 歌い方
# ---------------------------------------------------------------------------


@dataclass
class SingStyle:
    """1人ぶんの歌い方。2人が同じだと、2人に聞こえない。"""

    voice: str = VOICE_ANA
    speak_rate: str = "-20%"     # 喋りを遅くしておくと、伸ばす量が減る

    formant: float = 1.0         # 声道の長さ。素材が既に子供なので既定は等倍
    vibrato_hz: float = 5.5
    vibrato_cents: float = 22.0
    vibrato_onset_s: float = 0.28   # これより短い音符には掛けない
    vibrato_ramp_s: float = 0.25
    portamento_s: float = 0.055     # 音符の変わり目を繋ぐ幅
    scoop_cents: float = 55.0       # 音の入りのしゃくり
    scoop_s: float = 0.07
    drift_cents: float = 9.0        # ゆっくりした音程のゆらぎ
    detune_cents: float = 0.0
    breath_db: float = 0.0          # 非周期性を持ち上げる = 息っぽくする
    attack_s: float = 0.020
    release_s: float = 0.060
    seed: int = 1


VOICE_STYLES: dict[str, SingStyle] = {
    # 主旋律。米国の子供の声。
    "voice_lead": SingStyle(voice=VOICE_ANA, speak_rate="-20%",
                            vibrato_hz=5.6, vibrato_cents=24.0,
                            scoop_cents=60.0, detune_cents=0.0, seed=1),
    # ハモリ。英国の子供の声にして、揺れ方も変える。同じ声で2声を作ると
    # 融けて1人に聞こえるので、素材から変える。
    "voice_harmony": SingStyle(voice=VOICE_MAISIE, speak_rate="-18%",
                               vibrato_hz=4.8, vibrato_cents=16.0,
                               scoop_cents=35.0, detune_cents=-4.0,
                               breath_db=1.5, seed=2),
}


# ---------------------------------------------------------------------------
# WORLD による分解
# ---------------------------------------------------------------------------


@dataclass
class Frames:
    """喋った1行を分解したもの。"""

    f0: np.ndarray      # (T,)    0 は無声
    sp: np.ndarray      # (T, F)  スペクトル包絡（パワー）
    ap: np.ndarray      # (T, F)  非周期性 0..1
    rate: int
    period: float = FRAME_PERIOD

    @property
    def n(self) -> int:
        return len(self.f0)

    def time_to_frame(self, t: float) -> float:
        return t * 1000.0 / self.period


def analyse(x: np.ndarray, rate: int) -> Frames:
    import pyworld as pw

    d = np.ascontiguousarray(x, dtype=np.float64)
    f0, t = pw.harvest(d, rate, f0_floor=F0_FLOOR, f0_ceil=F0_CEIL,
                       frame_period=FRAME_PERIOD)
    f0 = pw.stonemask(d, f0, t, rate)
    sp = pw.cheaptrick(d, f0, t, rate)
    ap = pw.d4c(d, f0, t, rate)
    return Frames(f0=f0, sp=sp, ap=ap, rate=rate)


def warp_formants(sp: np.ndarray, ratio: float) -> np.ndarray:
    """周波数軸を伸縮して、声道の長さを変える。

    `ratio > 1` で共振が上に寄る = 声道が短い = 幼い。素材が既に子供なので
    普段は使わないが、2声を離したいときの逃げ道として残してある。
    """
    if abs(ratio - 1.0) < 1e-3:
        return sp
    f = np.arange(sp.shape[1], dtype=np.float64)
    src = np.clip(f / ratio, 0, sp.shape[1] - 1)
    out = np.empty_like(sp)
    for i in range(sp.shape[0]):
        out[i] = np.interp(src, f, sp[i])
    return out


# ---------------------------------------------------------------------------
# 音節の境界
# ---------------------------------------------------------------------------


def _energy(x: np.ndarray, hop: int) -> np.ndarray:
    n = max(1, len(x) // hop)
    e = np.empty(n)
    for i in range(n):
        seg = x[i * hop:(i + 1) * hop]
        e[i] = np.sqrt((seg ** 2).mean()) if len(seg) else 0.0
    return e


def split_word(x: np.ndarray, rate: int, t0: float, t1: float,
               k: int) -> list[tuple[float, float]]:
    """1単語を k 個の音節に切る。エネルギーの谷を探す。

    切る対象は "laugh-ter" / "af-ter" / "a-ware" のような2音節語だけ。多少
    ずれても、音符の変わり目が単語の中で少し早い/遅いだけで済む ―― 包絡は
    触らないので、母音そのものは壊れない。
    """
    if k <= 1:
        return [(t0, t1)]
    a, b = max(0, int(t0 * rate)), min(len(x), int(t1 * rate))
    seg = x[a:b]
    step = (t1 - t0) / k
    if len(seg) < 64:
        return [(t0 + i * step, t0 + (i + 1) * step) for i in range(k)]
    hop = max(1, int(rate * 0.005))
    e = _energy(seg, hop)
    if len(e) >= 5:
        e = np.convolve(e, np.ones(5) / 5, mode="same")
    cuts = [0.0]
    for i in range(1, k):
        c = i / k
        lo, hi = max(1, int(len(e) * (c - 0.18))), min(len(e) - 1, int(len(e) * (c + 0.18)))
        j = (lo + int(np.argmin(e[lo:hi]))) if hi > lo else int(len(e) * c)
        cuts.append(j * hop / rate)
    cuts.append(t1 - t0)
    cuts = sorted(cuts)
    return [(t0 + cuts[i], t0 + cuts[i + 1]) for i in range(k)]


def syllable_spans(utt: Utterance, line: Line) -> list[tuple[float, float]]:
    """喋った音声のどこがどの音節か。

    合成器が返す単語の時刻が土台。単語の数が歌詞側と食い違ったら**止める** ――
    ずれたまま歌わせると、以降の音節が全部1つずつずれて言葉にならない。
    """
    words = utt.word_spans()
    if len(words) != len(line.words):
        raise ValueError(
            f"word count disagrees: synthesiser {len(words)} / lyrics "
            f"{len(line.words)}\n"
            f"  {line.text!r}\n"
            f"  synthesiser: {[w.text for w in utt.words]}")
    out: list[tuple[float, float]] = []
    for (t0, t1), syls in zip(words, line.words):
        out.extend(split_word(utt.audio, utt.rate, t0, t1, len(syls)))
    return out


# ---------------------------------------------------------------------------
# 時間の張り替え
# ---------------------------------------------------------------------------


def _stretchable(fr: Frames, a: int, b: int) -> np.ndarray:
    """伸ばしてよいフレームはどれか（＝母音の芯）。

    一様に伸ばすと子音まで伸びて「ssss」になる。有声で、かつその音節の中で
    十分な強さがあるフレームだけを伸ばす。
    """
    voiced = fr.f0[a:b] > 0
    p = fr.sp[a:b].sum(axis=1)
    if not len(p):
        return voiced
    return voiced & (p > p.max() * 0.10)


def time_map(fr: Frames, a: int, b: int, m: int) -> np.ndarray:
    """入力フレーム [a,b) を出力 m フレームへ割り当てる写像。

    返すのは出力フレームごとの**入力フレーム位置（小数）**。小数のまま返して
    あとで補間するのは、大きく伸ばしたときに同じフレームが固まって「ビリつく」
    のを避けるため。
    """
    n = max(1, b - a)
    if m <= 0:
        return np.zeros(0)
    w = _stretchable(fr, a, b)
    n_s = int(w.sum())
    if n_s == 0:
        length = np.full(n, m / n, dtype=np.float64)
    else:
        q = float(np.clip((m - (n - n_s)) / n_s, 0.4, 12.0))
        length = np.where(w, q, 1.0).astype(np.float64)
        length *= m / length.sum()      # 端数を吸って、ぴったり m にする
    edges = np.concatenate([[0.0], np.cumsum(length)])
    pos = np.interp(np.arange(m, dtype=np.float64), edges, np.arange(n + 1))
    return a + np.clip(pos, 0, n - 1e-6)


def _gather(fr: Frames, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """小数フレーム位置で包絡と非周期性を補間する。

    包絡は**対数領域**で混ぜる。線形で混ぜると山と谷が均されて母音がこもる。
    """
    i0 = np.floor(idx).astype(int)
    i1 = np.minimum(i0 + 1, fr.n - 1)
    t = (idx - i0)[:, None]
    sp = np.exp((1 - t) * np.log(fr.sp[i0] + 1e-16) + t * np.log(fr.sp[i1] + 1e-16))
    ap = (1 - t) * fr.ap[i0] + t * fr.ap[i1]
    return sp, np.clip(ap, 1e-6, 1.0 - 1e-6)


# ---------------------------------------------------------------------------
# 音程の線
# ---------------------------------------------------------------------------


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or len(x) < 3:
        return x
    pad = n // 2
    y = np.concatenate([np.full(pad, x[0]), x, np.full(pad, x[-1])])
    return np.convolve(y, np.ones(n) / n, mode="same")[pad:pad + len(x)]


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """True が連続している区間を [開始, 終了) で返す。"""
    out: list[tuple[int, int]] = []
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def pitch_line(note_hz: np.ndarray, voiced: np.ndarray, st: SingStyle,
               period_s: float, rng: np.random.Generator) -> np.ndarray:
    """音符の並びから、実際に歌う音程の線を作る。

    階段状に切り替えると「機械が音程を切り替えた音」になる。人は (a) 音符の
    変わり目を繋ぎ、(b) 伸ばすところで揺らし、(c) 音の入りを下から取る。
    この3つを足す。
    """
    n = len(note_hz)
    if n == 0:
        return note_hz

    # ★休符を先に埋めてから均す★ 休符は 0 で入っているので、そのまま均すと
    # 音符の端が 0 側へ引きずられる。実際そうなって、ハモリの下限が 27Hz まで
    # 落ちた。休符のフレームは最後に無声化して捨てるので、前後の音符の値で
    # 埋めておけば害が無い。
    nz = np.flatnonzero(note_hz > 0)
    if not len(nz):
        return note_hz
    filled = np.interp(np.arange(n, dtype=float), nz.astype(float), note_hz[nz])

    # (a) 変わり目を繋ぐ。対数領域で均す = 音程として自然な補間になる
    lg = _smooth(np.log2(filled), max(1, int(round(st.portamento_s / period_s))))

    cents = np.zeros(n)
    t = np.arange(n) * period_s
    for a, b in runs(voiced):
        loc = t[a:b] - t[a]
        if (b - a) * period_s >= st.vibrato_onset_s:
            # (b) ビブラート。遅れて立ち上がる
            ramp = np.clip((loc - st.vibrato_onset_s) / max(st.vibrato_ramp_s, 1e-3), 0, 1)
            cents[a:b] += st.vibrato_cents * ramp * np.sin(
                2 * np.pi * st.vibrato_hz * loc + rng.uniform(0, 2 * np.pi))
        # (c) しゃくり。区間の頭だけ下から
        cents[a:b] -= st.scoop_cents * np.clip(1.0 - loc / max(st.scoop_s, 1e-3), 0, 1)

    # ゆっくりしたゆらぎ。完全に一定だと合成らしさが残る
    if st.drift_cents > 0 and n > 8:
        d = _smooth(rng.standard_normal(n), max(3, int(round(0.35 / period_s))))
        if d.std() > 1e-9:
            cents += st.drift_cents * d / d.std()

    return np.exp2(lg + (cents + st.detune_cents) / 1200.0)


# ---------------------------------------------------------------------------
# 1行を歌う
# ---------------------------------------------------------------------------


@dataclass
class SungLine:
    audio: np.ndarray
    rate: int
    start_s: float
    f0_lo: float = 0.0
    f0_hi: float = 0.0
    stretch_lo: float = 0.0
    stretch_hi: float = 0.0
    n_bad: int = 0


def sing_line(utt: Utterance, plan: LinePlan, tempo, st: SingStyle,
              transpose: int = 0) -> SungLine:
    """喋った1行を、その行の歌唱計画どおりに歌わせる。"""
    import pyworld as pw

    fr = analyse(utt.audio, utt.rate)
    if st.formant != 1.0:
        fr.sp = warp_formants(fr.sp, st.formant)

    spans = syllable_spans(utt, plan.line)
    if len(spans) != len(plan.targets):
        raise ValueError(f"{len(spans)} syllables against {len(plan.targets)} targets")

    period_s = fr.period / 1000.0
    beats = np.array([[t.start_beat, t.end_beat] for t in plan.targets])
    secs = np.asarray(tempo.beats_to_seconds(beats.ravel()), dtype=float).reshape(beats.shape)
    t_start, t_end = float(secs[0, 0]), float(secs[-1, 1])
    total = max(1, int(round((t_end - t_start) / period_s)))

    # 無音で埋めてから、音節のあるところだけ書き込む。行の中の休符は無音のまま。
    fft = fr.sp.shape[1]
    sp_out = np.full((total, fft), 1e-14)
    ap_out = np.ones((total, fft))
    note_hz = np.zeros(total)
    voiced = np.zeros(total, dtype=bool)
    src_voiced = np.zeros(total, dtype=bool)
    stretch: list[float] = []

    for (s0, s1), tgt, (o0, o1) in zip(spans, plan.targets, secs):
        a = int(np.clip(round(fr.time_to_frame(s0)), 0, fr.n - 1))
        b = int(np.clip(round(fr.time_to_frame(s1)), a + 1, fr.n))
        j0 = max(0, min(int(round((o0 - t_start) / period_s)), total - 1))
        j1 = max(j0 + 1, min(int(round((o1 - t_start) / period_s)), total))
        m = j1 - j0
        idx = time_map(fr, a, b, m)
        sp, ap = _gather(fr, idx)
        sp_out[j0:j1] = sp
        ap_out[j0:j1] = ap
        note_hz[j0:j1] = 440.0 * 2.0 ** ((tgt.pitch + transpose - 69) / 12.0)
        voiced[j0:j1] = True
        src_voiced[j0:j1] = fr.f0[np.floor(idx).astype(int)] > 0
        stretch.append(m / max(1, b - a))

    rng = np.random.default_rng(st.seed + int(round(plan.start_beat * 16)))
    f0_out = pitch_line(note_hz, voiced, st, period_s, rng)
    # ★元が無声だったところは無声のまま★ ここを有声にすると "s" や "th" が
    # 母音になり、言葉が壊れる。前の方式で実際にそうなった。
    f0_out[~src_voiced] = 0.0

    if st.breath_db:
        ap_out = np.clip(ap_out * 10.0 ** (st.breath_db / 20.0), 1e-6, 1 - 1e-6)

    # 行の端の当たりを丸める。包絡はパワーなので振幅の2乗を掛ける。
    env = np.ones(total)
    na = min(total, max(1, int(st.attack_s / period_s)))
    nr = min(total, max(1, int(st.release_s / period_s)))
    env[:na] = np.linspace(0, 1, na)
    env[total - nr:] = np.minimum(env[total - nr:], np.linspace(1, 0, nr))
    # ★0 で下限を切る★ WORLD は合成の途中で包絡の対数を取るので、要素が
    # ちょうど 0 だと -inf から NaN が出て、ステム全体が NaN になる。
    # フェードの端がまさに 0 になって、実際にそうなった。
    sp_out = np.maximum(sp_out * (env ** 2)[:, None], 1e-14)

    y = np.asarray(pw.synthesize(f0_out, np.ascontiguousarray(sp_out),
                                 np.ascontiguousarray(ap_out), fr.rate, fr.period),
                   dtype=np.float64)
    n_bad = int((~np.isfinite(y)).sum())
    if n_bad:
        # 黙って潰さない。1つでも出たら包絡か f0 の作り方が壊れている合図。
        log.warning("%d non-finite sample(s) in the synthesis: %r", n_bad, plan.line.text)
        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    v = f0_out[f0_out > 0]
    return SungLine(audio=y.astype(np.float32), rate=fr.rate, start_s=t_start,
                    n_bad=n_bad,
                    f0_lo=float(v.min()) if len(v) else 0.0,
                    f0_hi=float(v.max()) if len(v) else 0.0,
                    stretch_lo=float(min(stretch)) if stretch else 0.0,
                    stretch_hi=float(max(stretch)) if stretch else 0.0)


# ---------------------------------------------------------------------------
# 素材置き場
# ---------------------------------------------------------------------------


class PhraseBank:
    """歌う行の音声を貯めておく。取得は1回きりで、以降はディスクから読む。"""

    def __init__(self, directory: str | Path, style: SingStyle,
                 allow_network: bool = True):
        self.dir = Path(directory)
        self.style = style
        self.allow_network = allow_network
        self._cache: dict[str, Utterance] = {}

    def get(self, line: Line) -> Utterance:
        u = self._cache.get(line.text)
        if u is None:
            u = fetch(line.text, self.style.voice, self.dir,
                      rate=self.style.speak_rate, allow_network=self.allow_network)
            self._cache[line.text] = u
        return u


# ---------------------------------------------------------------------------
# ステム
# ---------------------------------------------------------------------------


@dataclass
class SungStem:
    audio: np.ndarray
    rate: int
    n_lines: int = 0
    n_syllables: int = 0
    f0_lo: float = 0.0
    f0_hi: float = 0.0
    stretch_lo: float = 0.0
    stretch_hi: float = 0.0
    warnings: list[str] = field(default_factory=list)


def render_sung_stem(plans: list[LinePlan], bank: PhraseBank, tempo, *,
                     duration_s: float, lead_in_s: float = 0.0,
                     tail_s: float = 0.0, gain: float = 1.0,
                     transpose: int = 0) -> SungStem:
    """歌唱計画をまとめて1本のステムにする。"""
    rate = TTS_RATE
    total = int(round((lead_in_s + duration_s + tail_s) * rate))
    out = np.zeros(total, dtype=np.float32)
    warn: list[str] = []
    f0_lo, f0_hi = 1e9, 0.0
    s_lo, s_hi = 1e9, 0.0
    n_syl = n_ok = 0

    for plan in plans:
        try:
            sung = sing_line(bank.get(plan.line), plan, tempo, bank.style,
                             transpose=transpose)
        except Exception as e:  # noqa: BLE001 - 1行の失敗で全体を落とさない
            warn.append(f"{plan.line.text!r}: {type(e).__name__}: {e}")
            continue
        if sung.rate != rate:
            rate = sung.rate
            total = int(round((lead_in_s + duration_s + tail_s) * rate))
            out = np.zeros(total, dtype=np.float32)
        i = int(round((lead_in_s + sung.start_s) * rate))
        seg = sung.audio
        if i < 0:
            seg, i = seg[-i:], 0
        j = min(total, i + len(seg))
        if j > i:
            out[i:j] += seg[:j - i] * gain
        if sung.n_bad:
            warn.append(f"{plan.line.text!r}: {sung.n_bad} non-finite sample(s)")
        n_ok += 1
        n_syl += len(plan.targets)
        f0_lo, f0_hi = min(f0_lo, sung.f0_lo), max(f0_hi, sung.f0_hi)
        s_lo, s_hi = min(s_lo, sung.stretch_lo), max(s_hi, sung.stretch_hi)

    if not n_ok:
        warn.append("not a single line was sung")
    return SungStem(audio=out, rate=rate, n_lines=n_ok, n_syllables=n_syl,
                    f0_lo=0.0 if f0_lo > 1e8 else f0_lo, f0_hi=f0_hi,
                    stretch_lo=0.0 if s_lo > 1e8 else s_lo, stretch_hi=s_hi,
                    warnings=warn)
