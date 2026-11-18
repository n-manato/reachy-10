"""実サンプルを鳴らすサンプラー。

gm.dls から抽出した波形（`tools/extract_samples.py`）を、音程を変えて鳴らす。

**ループが必須**という点だけは押さえておくこと。gm.dls の波形は極端に短い ――
グロッケンシュピールは 445 フレーム（20ms）しかない。ループ点を無視して
そのまま再生すると 20ms のクリック音にしかならず、「サンプルが壊れている」と
誤診する。実際には 326 フレーム目からの 118 フレームを繰り返して持続音を作る、
という古典的なサンプラーの作りになっている。

同じ理由で、抽出時に `wsmp` の `unity_note`（その録音が本来鳴らす音程）を
必ず持ち回っている。これが無いと何半音ずらせばよいか決まらない。

音程変更は読み出しポインタの進み方を変える方式（ratio = 目標Hz / unity Hz）。
これで音程とループを一度に扱える。線形補間なのでピッチを上げると折り返し歪みが
出るが、最終段で 16kHz へ落とすときのローパスが処理してくれる。
"""

from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np


def midi_to_hz(note: float) -> float:
    return 440.0 * 2.0 ** ((note - 69.0) / 12.0)


def _peak_near(x: np.ndarray, sr: int, expect_hz: float,
               tol_cents: float = 350.0) -> float:
    """期待値の近傍でスペクトルピークを探す。

    自己相関ではなくこれを使う理由: マレット系は倍音が強く、しかもループを
    繰り返すと自己相関は**ループ長そのもの**を周期として拾ってしまう
    （グロッケンのループは118フレーム＝187Hz。実際に一度これで誤診した）。
    おおよその期待値が分かっている場面では、窓を絞ったスペクトルピークのほうが
    はるかに頑健。
    """
    x = np.asarray(x, dtype=np.float64)
    n = min(len(x), sr)
    if n < 512:
        return 0.0
    w = x[:n] * np.hanning(n)
    sp = np.abs(np.fft.rfft(w, n * 4))      # ゼロ詰めで周波数分解能を上げる
    fr = np.fft.rfftfreq(n * 4, 1.0 / sr)
    lo = expect_hz * 2.0 ** (-tol_cents / 1200.0)
    hi = expect_hz * 2.0 ** (tol_cents / 1200.0)
    m = (fr >= lo) & (fr <= hi)
    if not m.any():
        return 0.0
    idx = int(np.flatnonzero(m)[np.argmax(sp[m])])
    if 0 < idx < len(sp) - 1:               # 放物線補間でサブビン精度に
        a, b, c = sp[idx - 1], sp[idx], sp[idx + 1]
        d = a - 2 * b + c
        if abs(d) > 1e-20:
            return float(fr[idx] + 0.5 * (a - c) / d * (fr[1] - fr[0]))
    return float(fr[idx])


@dataclass
class Zone:
    """1つの波形と、その再生に必要な情報。"""

    data: np.ndarray            # モノラル float64
    samplerate: int
    unity_note: int             # この波形が本来鳴らす MIDI ノート
    fine_tune_cents: float = 0.0
    loop_start: int = 0
    loop_length: int = 0
    name: str = ""

    @property
    def has_loop(self) -> bool:
        return self.loop_length > 1

    @property
    def unity_hz(self) -> float:
        """この波形が本来鳴らす周波数。

        **`fine_tune_cents` は意図的に使っていない。** gm.dls の `wsmp/sFineTune`
        を音程計算に足すと、実測で誤差がちょうど倍になる（8楽器中7つで確認）。
        この値は「これから補正すべき量」ではなく「サンプルに既に焼き込まれている
        調律」を記述しているらしい。仕様の解釈で悩むより実測に従う。
        残りの定数ずれは `Instrument.auto_tune()` が楽器ごとに吸収する。
        """
        return midi_to_hz(self.unity_note)


@dataclass
class Instrument:
    """複数ゾーンをまとめたマルチサンプル楽器。"""

    name: str
    zones: list[Zone]
    # 音の性格。エンベロープの既定値を決めるのに使う。
    kind: str = "mallet"        # mallet | sustain | percussion
    gain: float = 1.0
    pitch_shift: float = 0.0    # 半音。16kHz に収めるため下げたい場合に使う

    tuned: bool = False

    def zone_for(self, note: float) -> Zone:
        """目標音程に最も近い unity note のゾーンを選ぶ。

        マルチサンプルの意味は「音域ごとに別録りがある」なので、遠いゾーンを
        大きくピッチシフトするより近いゾーンを選ぶほうが自然に鳴る。
        """
        return min(self.zones, key=lambda z: abs(z.unity_note - note))

    def auto_tune(self, ref_notes: tuple[int, ...] = (60, 64, 67, 72)) -> float:
        """実際に鳴らして測り、楽器ごとの定数ずれを補正する。

        gm.dls の `unity_note` は概ね正しいが、楽器によって 30〜90 cent ほど
        系統的にずれている（グロッケン +38、Voice Oohs -86 など）。音程差は
        正確なので（実測でばらつき 0〜12 cent）、定数を1つ引けば直る。

        マジックナンバーを表に書くより、読み込み時に測って合わせるほうが
        サンプルを差し替えても壊れない。冪等。
        """
        if self.tuned or self.kind == "percussion":
            self.tuned = True
            return self.pitch_shift

        saved = self.pitch_shift
        self.pitch_shift = 0.0
        errs = []
        for note in ref_notes:
            x, sr = play_note(self, note, 0.7, tail_s=0.15)
            got = _peak_near(x[int(0.03 * sr):], sr, midi_to_hz(note))
            if got > 0:
                errs.append(1200.0 * np.log2(got / midi_to_hz(note)))
        # 中央値。倍音を拾った外れ値に引きずられないため。
        offset_cents = float(np.median(errs)) if errs else 0.0
        self.pitch_shift = saved - offset_cents / 100.0
        self.tuned = True
        return self.pitch_shift


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sw)
    if dtype is None:
        raise ValueError(f"{path}: unsupported bit depth {sw*8}")
    x = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    x = (x - 128.0) / 128.0 if sw == 1 else x / float(np.iinfo(dtype).max)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


class SampleLibrary:
    """`samples/generated/manifest.json` を読んで楽器を組み立てる。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        man_path = self.root / "manifest.json"
        if not man_path.exists():
            raise FileNotFoundError(
                f"{man_path} is missing. Run `python tools/extract_samples.py` first."
            )
        self.manifest = json.loads(man_path.read_text(encoding="utf-8"))

    @lru_cache(maxsize=64)
    def melodic(self, name: str, kind: str = "mallet", auto_tune: bool = True) -> Instrument:
        entry = self.manifest["melodic"].get(name)
        if entry is None:
            avail = ", ".join(sorted(self.manifest["melodic"]))
            raise KeyError(f"no melodic instrument {name!r}. Available: {avail}")
        zones = []
        for z in entry["zones"]:
            data, sr = _read_wav_mono(self.root / z["file"])
            if len(data) < 8:
                continue
            zones.append(Zone(
                data=data, samplerate=sr,
                unity_note=int(z["unity_note"]),
                fine_tune_cents=float(z.get("fine_tune_cents", 0.0)),
                loop_start=int(z["loop"]["start"]) if z.get("loop") else 0,
                loop_length=int(z["loop"]["length"]) if z.get("loop") else 0,
                name=z.get("wave_name", ""),
            ))
        if not zones:
            raise ValueError(f"{name}: not a single usable zone")
        inst = Instrument(name=name, zones=zones, kind=kind)
        if auto_tune:
            inst.auto_tune()
        return inst

    @lru_cache(maxsize=64)
    def percussion(self, name: str, fit_16k: bool = True) -> Instrument:
        entry = self.manifest["percussion"].get(name)
        if entry is None:
            avail = ", ".join(sorted(self.manifest["percussion"]))
            raise KeyError(f"no percussion instrument {name!r}. Available: {avail}")
        data, sr = _read_wav_mono(self.root / entry["file"])
        z = Zone(data=data, samplerate=sr,
                 unity_note=int(entry.get("unity_note", 60)),
                 name=entry.get("wave_name", ""))
        inst = Instrument(name=name, zones=[z], kind="percussion", tuned=True)
        if fit_16k:
            # 出力パイプラインは 16kHz 固定なので 8kHz より上は丸ごと消える。
            # タンバリンはエネルギーの 69%、カバサは 91% がそこにあり、
            # そのまま入れると別の楽器に聞こえる。音程ごと下げれば
            # アタックの質感は保たれる（実制作でも使われる手）。
            inst.pitch_shift = suggest_pitch_shift(data, sr)
        return inst


# ---------------------------------------------------------------------------
# 発音
# ---------------------------------------------------------------------------


def _envelope(n: int, sr: int, kind: str, sustain_s: float,
              attack_s: float = 0.004) -> np.ndarray:
    """振幅エンベロープ。

    ループはそのままだと永遠に鳴り続けるので、エンベロープで終わらせる。
    マレット系は指数減衰、歌やパッドは持続してから解放、打楽器は素通し。
    """
    t = np.arange(n) / sr
    a = int(max(1, attack_s * sr))
    env = np.ones(n)
    env[:a] = np.linspace(0.0, 1.0, a)      # クリック防止

    if kind == "mallet":
        # 叩いた音。持続時間に応じて減衰の速さを変える（短い音符ほど速く消える）
        tau = max(0.12, sustain_s * 0.9)
        env *= np.exp(-t / tau)
    elif kind == "sustain":
        # 歌・パッド。音符の長さだけ保持してから解放する
        rel = 0.18
        hold = max(0.0, sustain_s - rel)
        env *= np.clip((sustain_s + rel - t) / rel, 0.0, 1.0)
        # 立ち上がりを少し柔らかく（人の声・弦は打楽器のようには始まらない）
        soft = int(min(n, 0.05 * sr))
        if soft > a:
            env[:soft] *= np.linspace(0.0, 1.0, soft) ** 0.7
    # percussion は素通し（元の減衰をそのまま使う）

    # 末尾を必ず 0 にする。途中で切ると全ロボットのステムでプチノイズが出る。
    fade = int(min(n, 0.008 * sr))
    if fade > 1:
        env[-fade:] *= np.linspace(1.0, 0.0, fade)
    return env


def play_note(
    inst: Instrument,
    note: float,
    duration_s: float,
    velocity: float = 1.0,
    samplerate: int | None = None,
    tail_s: float = 0.0,
) -> tuple[np.ndarray, int]:
    """1音を鳴らして (波形, サンプルレート) を返す。

    Args:
        note: MIDI ノート番号。打楽器では無視される。
        duration_s: 音符の長さ。エンベロープの持続部に使う。
        tail_s: 音符の後に残す余韻。マレットの減衰を切らないため。
    """
    z = inst.zone_for(note)
    sr = samplerate or z.samplerate

    if inst.kind == "percussion":
        ratio = 2.0 ** (inst.pitch_shift / 12.0)
    else:
        target = midi_to_hz(note + inst.pitch_shift)
        ratio = target / z.unity_hz
    ratio *= z.samplerate / sr      # 出力レートが違う場合の補正

    total = int(max(1, (duration_s + tail_s) * sr))
    out = np.zeros(total)

    src = z.data
    n_src = len(src)
    loop_end = z.loop_start + z.loop_length

    if not z.has_loop or loop_end > n_src:
        # ループ無し（打楽器など）。読み切ったら終わり。
        idx = np.arange(total) * ratio
        valid = idx < n_src - 1
        i0 = idx[valid].astype(np.int64)
        frac = idx[valid] - i0
        out[valid] = src[i0] * (1 - frac) + src[i0 + 1] * frac
    else:
        # ループあり。アタック部を通過したあと loop 区間を巻き戻し続ける。
        # 読み出し位置を「実数の連続位置」として計算し、ループ内に折り返す。
        pos = np.arange(total, dtype=np.float64) * ratio
        in_attack = pos < z.loop_start
        looped = np.where(
            in_attack,
            pos,
            z.loop_start + np.mod(pos - z.loop_start, z.loop_length),
        )
        i0 = np.clip(looped.astype(np.int64), 0, n_src - 2)
        frac = looped - i0
        out = src[i0] * (1 - frac) + src[i0 + 1] * frac

    env = _envelope(total, sr, inst.kind, duration_s)
    return out * env * velocity * inst.gain, sr


def hf_energy_fraction(x: np.ndarray, sr: int, cutoff: float = 8000.0) -> float:
    """cutoff より上に何割のエネルギーがあるか。

    16kHz のパイプラインでは 8kHz より上が丸ごと失われる。タンバリンのように
    そこに 7 割の情報がある音は、そのまま入れると別の楽器に聞こえる。
    どれだけ下げれば救えるかを判断するために使う。
    """
    if len(x) < 64:
        return 0.0
    w = x * np.hanning(len(x))
    sp = np.abs(np.fft.rfft(w)) ** 2
    fr = np.fft.rfftfreq(len(w), 1.0 / sr)
    total = float(sp.sum())
    return float(sp[fr > cutoff].sum() / total) if total > 0 else 0.0


def suggest_pitch_shift(x: np.ndarray, sr: int, cutoff: float = 8000.0,
                        target_fraction: float = 0.15,
                        max_semitones: float = 14.0) -> float:
    """8kHz より上の成分を許容範囲まで落とすのに必要な半音数を求める。

    ブリックウォールで切ると芯だけ残って痩せるが、音程ごと下げれば
    アタックの質感は保たれる。タンバリンを1オクターブ下げても
    「大きめのタンバリン」として通用する ―― 実制作でも使われる手。
    """
    frac = hf_energy_fraction(x, sr, cutoff)
    if frac <= target_fraction:
        return 0.0
    # 半音ずつ下げて、必要量を探す（スペクトルを実際に動かすのではなく
    # cutoff を上げることで等価に評価する）
    for semis in np.arange(1.0, max_semitones + 0.5, 1.0):
        shifted_cutoff = cutoff * 2.0 ** (semis / 12.0)
        if hf_energy_fraction(x, sr, shifted_cutoff) <= target_fraction:
            return -float(semis)
    return -max_semitones
