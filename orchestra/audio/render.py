"""タイムラインから、ロボット1台分の音声ステムをレンダリングする。

物理楽器が無い構成では、ここが出す音がそのまま観客に聞こえる音になる。

出力は **16kHz stereo float32** —— ロボットの音声パイプラインが固定でこの形式を
要求するため（`media/audio_base.py:59-60`）。作業自体は素材の 22050Hz で行い、
最後にまとめて 16kHz へ落とす。音を1つずつ 16kHz に落とすと、そのたびに
折り返し歪みが混ざるため。

**なぜ台ごとに別のステムなのか**: 9台が同じ音を数ミリ秒ずれて鳴らすと
コムフィルタになって痩せる。台ごとに違うパートを鳴らせば、残ったズレは
「9人が弾いている」ように聞こえ、しかも音が出る場所と動く場所が一致する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..music.tempo import TempoMap
from ..music.timeline import NoteEvent
from .sampler import Instrument, SampleLibrary, play_note

OUTPUT_RATE = 16000        # パイプライン固定値。変更不可
WORK_RATE = 22050          # gm.dls の素材レート。ここで作業する


@dataclass
class Voice:
    """1つのステムを作るための「楽器 + 鳴らし方」の指定。"""

    instrument: Instrument
    gain: float = 1.0
    transpose: int = 0             # 半音
    note_tail_s: float = 0.35      # 音符の後に残す余韻（減衰を切らない）
    articulation: float = 1.0
    """音符を、記譜された長さの何割まで実際に伸ばすか。

    歌にはこれが要る。楽譜どおりに詰めて鳴らすと音符どうしが完全に繋がり、
    「アー」のサンプルが切れ目なく続くので、**人が唄っているのではなく
    パッドが鳴っている**ように聞こえる。実測で、主旋律の94%の音符が
    隙間ゼロだった。人は音節ごとに口を閉じるので、そこを模す。
    打楽器には無関係（1.0 のまま）。
    """
    pan: float = 0.0               # -1..+1。ロボットは1台1スピーカーなので通常0
    fixed_note: int | None = None  # 打楽器のように音程を持たない場合

    def render_note(self, ev: NoteEvent, dur_s: float, sr: int) -> np.ndarray:
        note = self.fixed_note if self.fixed_note is not None else ev.pitch + self.transpose
        # 音節の切れ目を作る。短くしすぎると言葉ではなく点になるので、
        # 最低 120ms は残す。
        if self.articulation < 1.0:
            dur_s = max(0.12, dur_s * self.articulation)
        x, _ = play_note(
            self.instrument, note, dur_s,
            velocity=0.35 + 0.65 * ev.dynamic,
            samplerate=sr, tail_s=self.note_tail_s,
        )
        return x * self.gain


@dataclass
class RenderResult:
    audio: np.ndarray               # (N, 2) float32, 16kHz
    samplerate: int = OUTPUT_RATE
    peak: float = 0.0
    rms: float = 0.0
    n_notes: int = 0
    clipped: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return len(self.audio) / self.samplerate


def _resample_to_output(x: np.ndarray, sr_in: int, sr_out: int = OUTPUT_RATE) -> np.ndarray:
    """折り返し防止フィルタ付きでレート変換する。

    16kHz へ落とすと 8kHz より上は失われる。単純な間引きだとその成分が
    折り返して可聴域に混ざるので、必ずローパスを通す resample_poly を使う。
    """
    if sr_in == sr_out:
        return x
    try:
        from scipy.signal import resample_poly
        from math import gcd

        g = gcd(int(sr_in), int(sr_out))
        return resample_poly(x, sr_out // g, sr_in // g)
    except ImportError:
        # scipy が無い環境向けの退避。品質は落ちるので推奨しない。
        n_out = int(round(len(x) * sr_out / sr_in))
        return np.interp(
            np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x
        )


def render_stem(
    events: list[NoteEvent],
    voice: Voice,
    tempo: TempoMap,
    duration_s: float,
    lead_in_s: float = 0.0,
    tail_s: float = 2.0,
    work_rate: int = WORK_RATE,
) -> RenderResult:
    """イベント列を1本のステムにする。"""
    total_s = lead_in_s + duration_s + tail_s
    n_work = int(total_s * work_rate) + 1
    buf = np.zeros(n_work, dtype=np.float64)
    warnings: list[str] = []

    for ev in sorted(events, key=lambda e: e.beat):
        t0 = float(tempo.beats_to_seconds(ev.beat)) + lead_in_s
        t1 = float(tempo.beats_to_seconds(ev.beat + ev.duration_beats)) + lead_in_s
        dur = max(0.05, t1 - t0)
        x = voice.render_note(ev, dur, work_rate)

        i = int(round(t0 * work_rate))
        if i < 0:
            x = x[-i:]
            i = 0
        j = min(n_work, i + len(x))
        if j > i:
            buf[i:j] += x[: j - i]

    audio = _resample_to_output(buf, work_rate, OUTPUT_RATE)

    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    if peak > 0.99:
        warnings.append(f"peak is {peak:.2f}, on the edge of clipping. Lower the gain")

    stereo = np.column_stack([audio, audio]).astype(np.float32)
    return RenderResult(
        audio=stereo,
        peak=peak,
        rms=float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0,
        n_notes=len(events),
        clipped=peak > 0.999,
        warnings=warnings,
    )


def balance(results: dict[str, RenderResult], headroom_db: float = -6.0,
            match_rms: bool = True,
            mix_db: dict[str, float] | None = None) -> dict[str, float]:
    """全ステムを1つのアンサンブルとして音量を揃える。

    9台が同時に鳴る以上、レベルは**ファイル単位ではなく合奏として**決めないと
    意味がない。各ステムを単体で正規化すると、休符だらけのパートが実音のときだけ
    突出して爆音になる。

    ただし「全部を同じ大きさに揃える」だけでは足りない。**足したときに
    パート数の多いほうが勝つ**からで、この曲では楽器7本に対して歌2本、
    実測で歌が伴奏より 4.5 dB 下に潜っていた。主旋律を歌が持っているのに
    伴奏の下にいるのは逆で、これは音源の作り方とは無関係に効く ―― 本物の
    歌の録音を持ってきても同じように埋もれる。

    そこで `mix_db` でパートごとに狙いをずらす。役割の指定であって、音量の
    微調整ではない（誰が主役かを書く）。

    Returns: part -> 適用すべきゲイン
    """
    if not results:
        return {}
    mix_db = mix_db or {}

    # 実際に鳴っている部分の RMS を基準にする（休符を含めた RMS だと
    # 出番の少ないパートほど過大評価される）
    def active_rms(r: RenderResult) -> float:
        mono = r.audio[:, 0]
        act = mono[np.abs(mono) > 1e-4]
        return float(np.sqrt(np.mean(act**2))) if len(act) else 0.0

    levels = {k: active_rms(v) for k, v in results.items()}
    ref = np.median([v for v in levels.values() if v > 0] or [1.0])
    target_peak = 10 ** (headroom_db / 20.0)

    gains: dict[str, float] = {}
    for k, r in results.items():
        want = ref * 10 ** (float(mix_db.get(k, 0.0)) / 20.0)
        g = (want / levels[k]) if (match_rms and levels[k] > 0) else 1.0
        # 揃えた結果クリップしないように上限をかける
        if r.peak * g > target_peak:
            g = target_peak / max(r.peak, 1e-9)
        gains[k] = float(np.clip(g, 0.05, 8.0))
    return gains


def write_wav_f32(path: str | Path, audio: np.ndarray, samplerate: int = OUTPUT_RATE,
                  gain: float = 1.0) -> Path:
    """16bit PCM WAV として書き出す。

    エージェントは PCM を直接ストリームするので、ここは中間表現でしかない。
    16bit にしておくと、他のツールでも中身を確認できる。
    """
    import wave

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(audio, dtype=np.float64) * gain
    peak = np.abs(x).max() if x.size else 0.0
    if peak > 0.999:
        x = x / peak * 0.999
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(pcm.shape[1] if pcm.ndim > 1 else 1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm.tobytes())
    return path


def read_wav_f32(path: str | Path) -> tuple[np.ndarray, int]:
    """WAV を (N,2) float32 / サンプルレート で読む。エージェント側でも使う形。"""
    import wave

    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[sw]
    x = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    x = (x - 128.0) / 128.0 if sw == 1 else x / float(np.iinfo(dtype).max)
    x = x.reshape(-1, ch)
    if ch == 1:
        x = np.column_stack([x[:, 0], x[:, 0]])
    elif ch > 2:
        x = x[:, :2]
    return np.ascontiguousarray(x, dtype=np.float32), sr


# ---------------------------------------------------------------------------
# 楽器の定義（この編成で使うもの）
# ---------------------------------------------------------------------------
#
# マンデ（マリ／ギニア）の Lamban 編成。8種すべて `tools/make_mande_samples.py`
# が数値合成で作る。gm.dls には kora も ngoni も balafon も djembe も無いので、
# 実録音の代用を探すより自分で鳴らすほうが近いところまで行ける。
#
# **すべて `melodic` 経路で引く。** `percussion` 経路は 1楽器 1波形・`fixed_note=60`
# 固定なので、djembe の bass/tone/slap や dunun の open/muted/鈴 を打ち分けられない。
# こちらはゾーン＝打法（打楽器）／ゾーン＝棒・弦（旋律楽器）で持たせている。
#
# 種別 `struck` は `_envelope()` に分岐を持たないので**素通し**になり、合成側で
# 作り込んだ減衰がそのまま生きる。`play_note()` 側では `percussion` 以外として
# 扱われるので、ノート番号によるゾーン選択が効く ―― この2つの性質の組み合わせが
# 欲しくてこの名前を使っている。

# name -> (ライブラリ上の名前, 種別, 既定ゲイン, 移調)
INSTRUMENT_PRESETS: dict[str, tuple[str, str, float, int]] = {
    # 旋律 —— ゾーン番号は「棒」「弦」の通し番号。音高ではない（音律は合成時に焼く）
    "balafon":  ("balafon", "struck", 0.55, 0),
    "kora":     ("kora", "struck", 0.80, 0),
    "ngoni":    ("ngoni", "struck", 0.80, 0),
    # 太鼓 —— ゾーン番号は打法
    "djembe":   ("djembe", "struck", 0.90, 0),
    "dundunba": ("dundunba", "struck", 0.90, 0),
    "sangban":  ("sangban", "struck", 0.85, 0),
    "tama":     ("tama", "struck", 0.80, 0),
    # 体鳴 —— ゾーン番号は往/復
    "karignan": ("karignan", "struck", 0.55, 0),
}

# 使っていない。`percussion` 経路は 1楽器 1波形・音程固定で、この編成では
# 打ち分けが要るため全楽器を `INSTRUMENT_PRESETS` 側に置いている。
PERCUSSION_PRESETS: dict[str, tuple[str, float]] = {}


# 音符の後に残す余韻[秒]。**音符の長さとは別物**であることが肝。
#
# 楽譜側の音符は短く書いてある（16分刻みのパートでは 0.2 拍）。同じ音高の
# 音符が時間的に重なると、MIDI の note_on/note_off の対応が壊れて音が脱落する
# ためで、実際に加熱部のドゥンドゥンバが12打中8打になっていた。
#
# だが太鼓も撥弦も、次の打が来ても前の音は鳴り続ける。その「鳴り続け」は
# 音符の長さではなく**こちら**が持つ。`render_stem` は加算合成なので、
# 余韻どうしが重なるのは正しい挙動（実際の楽器がそうであるように）。
NOTE_TAIL_S: dict[str, float] = {
    "balafon": 0.85,     # 瓢箪共鳴。低音の棒はよく伸びる
    "kora": 1.60,        # 21弦。低音弦は2秒近く鳴る
    "ngoni": 0.85,       # コラより速く減衰する
    "tama": 0.30,        # 小さな締太鼓。短い
    "djembe": 0.55,
    "dundunba": 0.95,    # 最大の胴。最も長く残る
    "sangban": 0.75,
    "karignan": 0.22,    # 擦る音。余韻は無い
}


def make_voice(lib: SampleLibrary, preset: str) -> Voice:
    """プリセット名から Voice を組み立てる。"""
    if preset in INSTRUMENT_PRESETS:
        sample_name, kind, gain, transpose = INSTRUMENT_PRESETS[preset]
        return Voice(instrument=lib.melodic(sample_name, kind),
                     gain=gain, transpose=transpose,
                     note_tail_s=NOTE_TAIL_S.get(
                         preset, 0.5 if kind == "sustain" else 0.35))
    if preset in PERCUSSION_PRESETS:
        sample_name, gain = PERCUSSION_PRESETS[preset]
        return Voice(instrument=lib.percussion(sample_name), gain=gain,
                     fixed_note=60, note_tail_s=0.25)
    known = ", ".join(sorted(set(INSTRUMENT_PRESETS) | set(PERCUSSION_PRESETS)))
    raise KeyError(f"no instrument preset {preset!r}. Available: {known}")
