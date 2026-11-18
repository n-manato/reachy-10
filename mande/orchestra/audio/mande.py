"""マンデ（マリ／ギニア）の楽器を数値合成する。

**なぜ録音ではなく合成なのか。** gm.dls にも一般的な GM 音源にも
kora / ngoni / balafon / djembe / dunun / tama / karignan は入っていない。
コンガやハープで代用すると単に別の楽器に聞こえるうえ、この編成に要る
2つのものが原理的に表現できない:

1. **打法** ―― djembe の bass / tone / slap、dunun の開放 / ミュート、
   それぞれの鈴。GM の打楽器は1楽器1波形なので打ち分けられない。
2. **音律** ―― マンデのバラフォンはほぼ等間隔の七音音階（1ステップ約 171 cent、
   7平均律に近い）で、12平均律の鍵盤楽器では出せない音程が並ぶ。

出力は既存サンプラーが読む形式そのまま（WAV + `manifest.json`）なので
`SampleLibrary` 側は無改造で動く。将来実録音が手に入ったら manifest の
エントリを差し替えるだけで乗り換えられる。

**帯域の制約**: ロボットの音声経路は 16kHz 固定で、8kHz より上は丸ごと消える。
参考元ではタンバリン（エネルギーの69%が 8kHz 超）が別の楽器に聞こえる事故が
起きている。ここでは最初から 7kHz でローパスして帯域内に情報を収める ――
後から音程を下げて救うより、作る側で収めるほうが素直。

すべて決定的（楽器・打法ごとに固定シード）。同じスクリプトを2回走らせれば
同じ波形が出る。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

SR = 22050              # `render.WORK_RATE` と一致させる（サンプラー内の再変換を避ける）
BAND_LIMIT_HZ = 7000.0  # 16kHz 出力で失われる 8kHz より下に収める


# ---------------------------------------------------------------------------
# 素材
# ---------------------------------------------------------------------------


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _onepole_lp(x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    """1次ローパス。素材を軽く丸めるだけの用途。"""
    if cutoff >= sr * 0.5:
        return x
    a = np.exp(-2.0 * np.pi * cutoff / sr)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc = (1.0 - a) * v + a * acc
        y[i] = acc
    return y


def _butter(x: np.ndarray, sr: int, cutoff: float, kind: str = "low",
            order: int = 4) -> np.ndarray:
    """バターワース。scipy があれば使い、無ければ1次で妥協する。"""
    try:
        from scipy.signal import butter, sosfilt
        wn = np.clip(cutoff / (sr * 0.5), 1e-4, 0.999)
        sos = butter(order, wn, btype=kind, output="sos")
        return sosfilt(sos, x)
    except ImportError:
        if kind == "low":
            return _onepole_lp(x, sr, cutoff)
        return x - _onepole_lp(x, sr, cutoff)


def _bandpass(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    try:
        from scipy.signal import butter, sosfilt
        wn = [np.clip(lo / (sr * 0.5), 1e-4, 0.98),
              np.clip(hi / (sr * 0.5), 1e-3, 0.99)]
        sos = butter(2, wn, btype="band", output="sos")
        return sosfilt(sos, x)
    except ImportError:
        return _onepole_lp(x - _onepole_lp(x, sr, lo), sr, hi)


def _strike(n: int, sr: int, seed: int, lo: float, hi: float,
            length_s: float) -> np.ndarray:
    """打撃の励起。帯域を絞った短いノイズバースト。

    打法の差はほとんどここで決まる。掌で押す bass は低い帯域を長めに、
    指先で弾く slap は高い帯域を極短に。膜の共鳴（下の `_resonate`）は
    同じでも、入力が違えば出てくる音は別物になる。
    """
    m = max(4, int(length_s * sr))
    x = np.zeros(n)
    burst = _rng(seed).standard_normal(m)
    burst *= np.exp(-np.arange(m) / (m * 0.35))     # バーストの中でも減衰
    x[:m] = burst
    return _bandpass(x, sr, lo, hi)


def _resonate(exciter: np.ndarray, sr: int, freqs, amps, taus) -> np.ndarray:
    """2極共振器のバンクで励起を鳴らす。

    加算合成でも似た音は作れるが、共振器に通すほうがアタックの結合が自然に出る
    ―― 打撃ノイズの成分がモードに「吸い込まれて」立ち上がるので。
    """
    try:
        from scipy.signal import lfilter
    except ImportError:
        lfilter = None
    out = np.zeros(len(exciter))
    for f, a, tau in zip(freqs, amps, taus):
        if f <= 0.0 or f >= sr * 0.45:
            continue
        r = float(np.exp(-1.0 / max(tau * sr, 1.0)))
        w = 2.0 * np.pi * f / sr
        b = [a * (1.0 - r)]                          # 共振点のゲインを揃える
        aa = [1.0, -2.0 * r * np.cos(w), r * r]
        if lfilter is not None:
            out += lfilter(b, aa, exciter)
        else:
            y = np.zeros(len(exciter)); y1 = y2 = 0.0
            for i, v in enumerate(exciter):
                cur = b[0] * v - aa[1] * y1 - aa[2] * y2
                y[i] = cur; y2 = y1; y1 = cur
            out += y
    return out


def _additive(n: int, sr: int, f0: float, ratios, amps, taus, seed: int,
              glide_semitones: float = 0.0, glide_s: float = 0.06) -> np.ndarray:
    """加算合成。**音程が時間で動く場合はこちら。**

    共振器は係数が固定なのでグライドを掛けられない。タマ（トーキングドラム）の
    「喋る」感じは音程が動くことそのものなので、あれだけは加算で作る。
    """
    t = np.arange(n) / sr
    bend = 2.0 ** (glide_semitones * (1.0 - np.exp(-t / max(glide_s, 1e-4))) / 12.0)
    phase_base = 2.0 * np.pi * np.cumsum(f0 * bend) / sr
    rng = _rng(seed)
    out = np.zeros(n)
    for ratio, a, tau in zip(ratios, amps, taus):
        if f0 * ratio >= sr * 0.45:
            continue
        out += a * np.exp(-t / tau) * np.sin(ratio * phase_base + rng.uniform(0, 2 * np.pi))
    return out


def _pluck(n: int, sr: int, f0: float, decay_s: float, brightness: float,
           seed: int, buzz: float = 0.0) -> np.ndarray:
    """拡張 Karplus-Strong。撥弦楽器（kora / ngoni）用。

    **分数遅延が要る。** 素朴な KS は遅延線長が整数サンプルに丸まるので、
    出る音程は sr/round(sr/f0) に量子化される。22050Hz では低音弦で数 cent、
    高音弦では十数 cent ずれる。バラフォンと同じ音階を鳴らす楽器がそれだけ
    ぶら下がると合奏でうなる。遅延線の読み出しを線形補間して端数を吸収する。

    ループフィルタは2点平均（FIR、群遅延ちょうど `damp`）。これも遅延の一部
    なので、目標周期から差し引いてから遅延線長を決める。
    """
    period = sr / f0
    damp = 0.5 - 0.35 * brightness          # ループフィルタの重み＝その群遅延
    L = int(np.ceil(period - damp))
    L = max(2, L)
    frac = L - (period - damp)              # 0 <= frac < 1。前へ読んで縮める分
    frac = float(np.clip(frac, 0.0, 0.999))

    rng = _rng(seed)
    line = rng.uniform(-1.0, 1.0, L)
    line = _onepole_lp(line, sr, f0 * (2.0 + 8.0 * brightness))   # 撥弦位置の代わり
    line -= line.mean()

    # ループ1周あたりの減衰量。decay_s で -60dB になるように。
    loops = max(1.0, decay_s * sr / period)
    g = 10.0 ** (-3.0 / loops)

    out = np.zeros(n)
    prev = 0.0
    ptr = 0
    for i in range(n):
        a = line[ptr]
        b = line[(ptr + 1) % L]
        cur = a + frac * (b - a)            # 分数遅延
        filt = (1.0 - damp) * cur + damp * prev
        prev = cur
        line[ptr] = filt * g
        out[i] = cur
        ptr = (ptr + 1) % L

    if buzz > 0.0:
        # ンゴニの金属片（nyenyemo）、コラの駒の当たり。振幅が大きいところだけ
        # ビリつく ―― 一定量のノイズを足すと「常に汚れている」音になる。
        env = _onepole_lp(np.abs(out), sr, 60.0)
        gate = np.clip((env - 0.06) / 0.2, 0.0, 1.0)
        rat = _bandpass(_rng(seed + 1).standard_normal(n), sr, 1800.0, 5500.0)
        out = out + buzz * gate * rat
    return out


def _pluck_tuned(n: int, sr: int, f_target: float, decay_s: float,
                 brightness: float, seed: int, buzz: float = 0.0,
                 iters: int = 2) -> np.ndarray:
    """鳴らして測り、狙った周波数に合わせてから返す。

    分数遅延を入れても、ループフィルタの群遅延は周波数依存なので端数が残る。
    表に補正値を書くより、参考元の `Instrument.auto_tune()` と同じ発想で
    **測って合わせる**ほうがパラメータを触っても壊れない。冪等。
    """
    drive = f_target
    y = _pluck(n, sr, drive, decay_s, brightness, seed, buzz=0.0)
    for _ in range(iters):
        got = _peak_near_local(y[int(0.005 * sr):], sr, f_target)
        if got <= 0.0:
            break
        err = f_target / got
        if abs(1200.0 * np.log2(err)) < 0.5:      # 0.5 cent 以内なら十分
            break
        drive *= err
        y = _pluck(n, sr, drive, decay_s, brightness, seed, buzz=0.0)
    if buzz > 0.0:
        env = _onepole_lp(np.abs(y), sr, 60.0)
        gate = np.clip((env - 0.06) / 0.2, 0.0, 1.0)
        rat = _bandpass(_rng(seed + 1).standard_normal(len(y)), sr, 1800.0, 5500.0)
        y = y + buzz * gate * rat
    return y


def _peak_near_local(x: np.ndarray, sr: int, expect_hz: float,
                     tol_cents: float = 250.0) -> float:
    """期待値近傍のスペクトルピーク（サンプラー側と同じ考え方の簡易版）。"""
    nn = min(len(x), sr)
    if nn < 512:
        return 0.0
    w = x[:nn] * np.hanning(nn)
    sp = np.abs(np.fft.rfft(w, nn * 4))
    fr = np.fft.rfftfreq(nn * 4, 1.0 / sr)
    lo = expect_hz * 2.0 ** (-tol_cents / 1200.0)
    hi = expect_hz * 2.0 ** (tol_cents / 1200.0)
    m = (fr >= lo) & (fr <= hi)
    if not m.any():
        return 0.0
    idx = int(np.flatnonzero(m)[np.argmax(sp[m])])
    if 0 < idx < len(sp) - 1:
        a, b, c = sp[idx - 1], sp[idx], sp[idx + 1]
        d = a - 2 * b + c
        if abs(d) > 1e-20:
            return float(fr[idx] + 0.5 * (a - c) / d * (fr[1] - fr[0]))
    return float(fr[idx])


def _tail(x: np.ndarray, sr: int, fade_s: float = 0.006) -> np.ndarray:
    """末尾を必ず 0 にする。切りっぱなしだとステム全体でプチノイズが出る。"""
    k = min(len(x), max(2, int(fade_s * sr)))
    x = x.copy()
    x[-k:] *= np.linspace(1.0, 0.0, k)
    return x


def _finish(x: np.ndarray, sr: int, peak: float = 0.86) -> np.ndarray:
    """帯域制限 → 正規化 → 末尾処理。全ストロークの共通の仕上げ。"""
    x = _butter(x, sr, BAND_LIMIT_HZ, "low", order=6)
    m = float(np.abs(x).max())
    if m > 1e-9:
        x = x * (peak / m)
    return _tail(x, sr)


# ---------------------------------------------------------------------------
# 音律
# ---------------------------------------------------------------------------

# マンデのバラフォンは**ほぼ等間隔の七音音階**で、1ステップが平均 171 cent
# （＝1200/7）と報告されている。ギニア・ニアガソラの Sosso-Bala（ユネスコ
# 無形文化遺産、Dökala 家が28世代守っている）がその基準器。
# 12平均律の全音(200)と半音(100)の中間にある音程が並ぶので、鍵盤楽器では出せない。
STEP_CENTS = 1200.0 / 7.0       # = 171.4286


def heptatonic(f_low: float, n: int) -> list[float]:
    """等間隔七音音階を下から n 音。"""
    return [f_low * 2.0 ** (k * STEP_CENTS / 1200.0) for k in range(n)]


# コラの伝統調弦。**バラフォンと違い、コラは等間隔ではない。**
# kora-music.com が4種の調弦を cent で載せている（基準 F）:
#     Silaba/Tomora ba  F 200 G 185 A 115 B 200 C 200 D 185 E 115 f
#     Tomora mesengo    F 230 G  95 A 175 B 200 C 230 D  95 E 175 f
#     Hardino           F 185 G 220 A  95 B 200 C 185 D 220 E  95 f
#     Sauta             F 185 G 220 A 200 B  95 C 185 D 220 E  95 f
#
# Lamban にどれを使うかは資料が割れている ―― Amadu Jobarteh は Sauta、
# Jaliology は Hardino、ガンビアでは "Lambang Silaba" という曲名でも通っている。
# ここでは**名前の付いた奏者の帰属がある Sauta** を採る。第4度が半音上がる調弦で、
# バラフォンの等間隔七音に対して独特の色が出る。
SAUTA_CENTS = (0.0, 185.0, 405.0, 605.0, 700.0, 885.0, 1105.0)


def kora_scale(f_low: float, n: int, cents=SAUTA_CENTS) -> list[float]:
    """調弦表から n 弦ぶんの周波数。7弦で1オクターブ上がる。"""
    out = []
    for k in range(n):
        octave, deg = divmod(k, len(cents))
        out.append(f_low * 2.0 ** (octave + cents[deg] / 1200.0))
    return out


# ---------------------------------------------------------------------------
# 楽器
# ---------------------------------------------------------------------------


@dataclass
class Stroke:
    """1ゾーン分の波形と、それが何であるかの記述。"""

    zone: int                  # manifest の unity_note。**音高ではなく通し番号**
    label: str                 # 人が読む名前（"bass" / "bar_07" など）
    audio: np.ndarray
    hz: float = 0.0            # 音程がある場合の実周波数（記録用）


@dataclass
class Instrument:
    name: str
    kind: str                  # render.INSTRUMENT_PRESETS の種別と揃える
    strokes: list[Stroke] = field(default_factory=list)
    note: str = ""


# --- 膜鳴 -------------------------------------------------------------------

# 理想円形膜のモード比（ベッセル関数の零点比）。実際の太鼓は空気負荷と皮の
# 剛性で低次側が下に寄り、倍音列に近づく。`squash` でその寄りを表現する。
MEMBRANE = np.array([1.000, 1.594, 2.136, 2.296, 2.653, 2.918, 3.156, 3.501])


def _membrane_modes(squash: float) -> np.ndarray:
    """低次モードを倍音列側へ寄せる。squash=0 で理想膜、1 で完全な倍音列。"""
    harm = np.arange(1, len(MEMBRANE) + 1, dtype=float)
    return MEMBRANE * (1.0 - squash) + harm * squash


def _drum(f0: float, tau0: float, squash: float, seed: int,
          strike_lo: float, strike_hi: float, strike_len: float,
          dur: float, mode_tilt: float = 1.0, click: float = 0.0,
          click_hz: float = 2500.0, sr: int = SR) -> np.ndarray:
    """太鼓の1打。モード共振＋打撃ノイズ＋（あれば）バチの当たり音。"""
    n = int(dur * sr)
    ratios = _membrane_modes(squash)
    amps = mode_tilt ** np.arange(len(ratios))
    taus = tau0 * (0.75 ** np.arange(len(ratios)))      # 高次ほど速く消える
    ex = _strike(n, sr, seed, strike_lo, strike_hi, strike_len)
    y = _resonate(ex, sr, ratios * f0, amps, taus)
    if click > 0.0:
        # 木のバチが皮に当たる音。太鼓の「胴」ではなく「当たり」の成分で、
        # これが無いと dunun がシンセのキックのように聞こえる。
        c = _strike(n, sr, seed + 7, click_hz * 0.5, click_hz * 2.2, 0.0016)
        y = y + click * c
    return y


def build_djembe() -> Instrument:
    """ジェンベ。山羊皮を高く張った片面太鼓。素手で bass / tone / slap を打ち分ける。

    ゾーン割当（`songs/lamban/song.yaml` と `tools/make_lamban.py` が使う）:
        60 = bass (gun/dun)  掌で中央
        62 = tone (go/do)    指を揃えて縁の内側
        64 = slap (pa/ta)    指先で縁 ―― 弾けるような高い音
    """
    st = [
        Stroke(60, "bass", _finish(_drum(f0=78.0, tau0=0.42, squash=0.55, seed=101,
                                         strike_lo=40.0, strike_hi=420.0,
                                         strike_len=0.010, dur=0.75,
                                         mode_tilt=0.42), SR), 78.0),
        Stroke(62, "tone", _finish(_drum(f0=196.0, tau0=0.20, squash=0.35, seed=102,
                                         strike_lo=180.0, strike_hi=2600.0,
                                         strike_len=0.004, dur=0.45,
                                         mode_tilt=0.72), SR), 196.0),
        Stroke(64, "slap", _finish(_drum(f0=340.0, tau0=0.075, squash=0.20, seed=103,
                                         strike_lo=900.0, strike_hi=6200.0,
                                         strike_len=0.0022, dur=0.30,
                                         mode_tilt=0.95, click=0.55,
                                         click_hz=3400.0), SR), 340.0),
    ]
    return Instrument("djembe", "struck", st,
                      note="bass/tone/slap をゾーンで打ち分ける")


def _dunun(name: str, f0: float, tau_open: float, bell_hz: float,
           seed: int) -> Instrument:
    """両面太鼓（dunun）＋ その奏者が足で／手で鳴らす鉄鈴 kenken。

    ゾーン割当:
        60 = 開放（バチを離す）
        61 = ミュート（バチを皮に置いたまま）
        72 = 鈴 kenken
    dunun は太鼓と鈴が**同じ奏者の1つのパート**なので、1楽器にまとめている。
    """
    open_ = _drum(f0=f0, tau0=tau_open, squash=0.62, seed=seed,
                  strike_lo=45.0, strike_hi=900.0, strike_len=0.0035,
                  dur=1.10, mode_tilt=0.38, click=0.30, click_hz=2100.0)
    mute = _drum(f0=f0 * 1.06, tau0=0.075, squash=0.62, seed=seed + 1,
                 strike_lo=60.0, strike_hi=1400.0, strike_len=0.0030,
                 dur=0.30, mode_tilt=0.55, click=0.42, click_hz=2400.0)
    # 鉄鈴。非調和なモードを重ね、減衰は長め。
    n = int(0.55 * SR)
    ex = _strike(n, SR, seed + 2, 900.0, 6500.0, 0.0012)
    bell = _resonate(ex, SR,
                     bell_hz * np.array([1.0, 2.71, 4.13, 5.42, 6.81]),
                     [1.0, 0.70, 0.48, 0.34, 0.22],
                     [0.30, 0.22, 0.17, 0.13, 0.10])
    st = [
        Stroke(60, "open", _finish(open_, SR), f0),
        Stroke(61, "muted", _finish(mute, SR, peak=0.62), f0 * 1.06),
        Stroke(72, "bell", _finish(bell, SR, peak=0.70), bell_hz),
    ]
    return Instrument(name, "struck", st, note="開放 / ミュート / kenken 鈴")


def build_dundunba() -> Instrument:
    """ドゥンドゥンバ ―― dunun 三兄弟の最大。最低音で対リズムを刻む。"""
    return _dunun("dundunba", f0=62.0, tau_open=0.60, bell_hz=690.0, seed=201)


def build_sangban() -> Instrument:
    """サンバン ―― 中型。「アンサンブルの心臓」と呼ばれ、曲の骨格を出す。

    鈴の音程を dundunba よりわずかに上げてある。同じ高さにすると2台の鈴が
    混ざって1つに聞こえ、どちらが何を叩いているのか分からなくなる。
    """
    return _dunun("sangban", f0=104.0, tau_open=0.44, bell_hz=845.0, seed=211)


def build_tama() -> Instrument:
    """タマ（タマニ）―― 砂時計型の締太鼓。脇に挟んで紐を締め、音程を変える。

    **この楽器だけ加算合成**。「喋る」ように聞こえるのは音程が動くからで、
    係数固定の共振器ではグライドが掛けられない。

    ゾーン 55-67 に13段の定常音程（約1オクターブ）、
    ゾーン 68 / 69 に上行 / 下行のベンドを1打ずつ持たせている。
    """
    st: list[Stroke] = []
    lo = 200.0
    for k in range(13):
        f = lo * 2.0 ** (k / 12.0)
        n = int(0.34 * SR)
        y = _additive(n, SR, f,
                      ratios=_membrane_modes(0.45)[:6],
                      amps=[1.0, 0.62, 0.40, 0.27, 0.18, 0.12],
                      taus=[0.13, 0.10, 0.085, 0.07, 0.055, 0.045],
                      seed=300 + k)
        y = y + 0.45 * _strike(n, SR, 320 + k, 1600.0, 5800.0, 0.0018)
        st.append(Stroke(55 + k, f"step_{k:02d}", _finish(y, SR), f))

    for zone, semis, tag in ((68, 5.0, "bend_up"), (69, -5.0, "bend_down")):
        n = int(0.42 * SR)
        base = lo * 2.0 ** (6 / 12.0)
        y = _additive(n, SR, base,
                      ratios=_membrane_modes(0.45)[:6],
                      amps=[1.0, 0.62, 0.40, 0.27, 0.18, 0.12],
                      taus=[0.20, 0.15, 0.12, 0.10, 0.08, 0.065],
                      seed=340 + zone, glide_semitones=semis, glide_s=0.10)
        y = y + 0.40 * _strike(n, SR, 360 + zone, 1600.0, 5800.0, 0.0018)
        st.append(Stroke(zone, tag, _finish(y, SR), base))
    return Instrument("tama", "struck", st, note="13段の音程 + 上行/下行ベンド")


# --- 体鳴 -------------------------------------------------------------------


def build_balafon() -> Instrument:
    """バラフォン ―― 瓢箪共鳴の木琴。Lamban はこの楽器の上で生まれた。

    3つの要素でできている:
      * **棒** 自由端棒のモードは 1 : 2.756 : 5.404 だが、実物は裏を削って
        （アンダーカット）2倍音を 3倍、3倍音を 6倍付近へ寄せてある。
      * **瓢箪** 基音を強く持ち上げ、減衰をわずかに速める共鳴体。
      * **ビリつき** 瓢箪に開けた穴に薄膜（蜘蛛の卵嚢や紙）を張ってある。
        振幅が大きいときだけジリッと鳴る ―― **これがバラフォンの決定的な音色**で、
        マリンバとの差はほぼここにある。一定量のノイズを足すのではなく、
        包絡線で開くゲートを通すのが肝。

    音律は等間隔七音（1ステップ 171.43 cent）。21枚 = 3オクターブ。
    ゾーン 60 + k が k 枚目の棒。**MIDI 番号は音高ではなく棒の番号**。
    """
    st: list[Stroke] = []
    pitches = heptatonic(174.6, 21)      # 最低音 ≒ F3 相当
    for k, f in enumerate(pitches):
        dur = float(np.interp(f, [170.0, 1400.0], [0.90, 0.34]))
        n = int(dur * SR)
        tau = float(np.interp(f, [170.0, 1400.0], [0.34, 0.11]))
        y = _additive(n, SR, f,
                      ratios=[1.0, 3.0, 6.0, 10.1],
                      amps=[1.0, 0.30, 0.13, 0.05],
                      taus=[tau, tau * 0.42, tau * 0.26, tau * 0.16],
                      seed=400 + k)
        # マレットが棒に当たる音
        y = y + 0.30 * _strike(n, SR, 440 + k, 1200.0, 5200.0, 0.0014)
        # 瓢箪のビリつき
        env = _onepole_lp(np.abs(y), SR, 45.0)
        gate = np.clip((env - 0.10) / 0.22, 0.0, 1.0)
        rat = _bandpass(_rng(480 + k).standard_normal(n), SR, 1500.0, 5000.0)
        y = y + 0.26 * gate * rat
        st.append(Stroke(60 + k, f"bar_{k:02d}", _finish(y, SR), f))
    return Instrument("balafon", "struck", st,
                      note="等間隔七音 21枚。ゾーン=棒番号")


def build_karignan() -> Instrument:
    """カリニャン（ネゲ）―― 溝を刻んだ金属管を金属棒で擦る。ジェリムソの楽器。

    音は「1打」ではなく**溝を次々に弾く密なインパルス列**。往復で溝を渡る
    速さも本数も違うので、往(60)と復(61)を別ゾーンにしてある。交互に鳴らすと
    人が擦っているむらが出る。
    """
    st: list[Stroke] = []
    for zone, (teeth, span, res, seed) in enumerate(
            [(19, 0.072, 3100.0, 501), (15, 0.061, 3550.0, 502)]):
        n = int(0.26 * SR)
        ex = np.zeros(n)
        rng = _rng(seed)
        for i in range(teeth):
            # 等間隔だと機械的な唸りになるので、位置と強さを少し散らす
            pos = (i / teeth) * span * (1.0 + rng.uniform(-0.10, 0.10))
            j = int(pos * SR)
            if 0 <= j < n:
                ex[j] += rng.uniform(0.55, 1.0) * (1.0 - 0.4 * i / teeth)
        ex = _bandpass(ex, SR, 1400.0, 6800.0)
        y = _resonate(ex, SR,
                      res * np.array([1.0, 1.87, 2.63, 3.41]),
                      [1.0, 0.62, 0.40, 0.26],
                      [0.055, 0.042, 0.033, 0.026])
        st.append(Stroke(60 + zone, ["scrape_out", "scrape_in"][zone],
                         _finish(y, SR, peak=0.78), res))
    return Instrument("karignan", "struck", st, note="往 / 復 の2ゾーン")


# --- 撥弦 -------------------------------------------------------------------


def build_kora() -> Instrument:
    """コラ ―― 21弦のハープリュート。瓢箪の胴に牛皮を張り、釣糸の弦を親指で弾く。

    調弦は **Sauta**（`SAUTA_CENTS`、上の注記を参照）。バラフォンの等間隔七音とは
    意図的にずらしてある ―― マンデの合奏でコラとバラフォンの音律が完全には
    一致しないのは実際そうで、そのわずかな差が響きの厚みになる。

    ゾーン 48 + k が k 番目の弦（低い順）。**MIDI 番号は音高ではなく弦の番号**。
    """
    st: list[Stroke] = []
    pitches = kora_scale(87.3, 21)       # 最低音 ≒ F2 相当。3オクターブ
    for k, f in enumerate(pitches):
        decay = float(np.interp(f, [87.0, 700.0], [2.0, 0.85]))
        dur = min(2.4, decay * 1.15)
        n = int(dur * SR)
        y = _pluck_tuned(n, SR, f, decay_s=decay,
                         brightness=float(np.interp(f, [87.0, 700.0], [0.30, 0.62])),
                         seed=600 + k, buzz=0.05)
        # 瓢箪＋皮の胴鳴り。低い共鳴を1つ足すだけで「箱」の感じが出る。
        body = _resonate(y, SR, [138.0, 262.0], [0.22, 0.12], [0.09, 0.06])
        st.append(Stroke(48 + k, f"string_{k:02d}", _finish(y + body, SR), f))
    return Instrument("kora", "struck", st,
                      note="Sauta 調弦 21弦。ゾーン=弦番号")


def build_ngoni() -> Instrument:
    """ンゴニ ―― 皮を張った小さな胴に弦を渡したリュート。バンジョーの祖先筋。

    コラより減衰が速く、金属片（nyenyemo）のビリつきが常に付いて回る。
    低音域でリフを刻む役。7弦。ゾーン 48 + k。
    """
    st: list[Stroke] = []
    pitches = heptatonic(87.3, 7) + heptatonic(87.3 * 2.0, 7)
    for k, f in enumerate(pitches):
        decay = float(np.interp(f, [87.0, 350.0], [0.85, 0.45]))
        n = int(min(1.4, decay * 1.4) * SR)
        y = _pluck_tuned(n, SR, f, decay_s=decay,
                         brightness=float(np.interp(f, [87.0, 350.0], [0.45, 0.70])),
                         seed=700 + k, buzz=0.22)
        st.append(Stroke(48 + k, f"string_{k:02d}", _finish(y, SR), f))
    return Instrument("ngoni", "struck", st, note="14弦（2オクターブ）。ゾーン=弦番号")


BUILDERS = {
    "balafon": build_balafon,
    "kora": build_kora,
    "ngoni": build_ngoni,
    "tama": build_tama,
    "djembe": build_djembe,
    "dundunba": build_dundunba,
    "sangban": build_sangban,
    "karignan": build_karignan,
}


def build_all() -> list[Instrument]:
    return [BUILDERS[k]() for k in BUILDERS]
