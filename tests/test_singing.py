"""歌詞の割り当てと、歌声の組み立て。

ここで守りたいのは「聞いて良いかどうか」ではなく、**壊れていたら必ず落ちる**
性質のほう。歌は耳でしか最終判断できないが、以下は耳を使わずに分かるし、
どれも実際に踏んだ失敗に対応している:

  * 音節と音符の対応がずれる  -> 歌詞が1つずつずれて言葉にならない
  * 休符を跨いで音程が均される -> 音符の端の f0 が落ちる（27Hz まで落ちた）
  * 包絡がちょうど 0 になる    -> WORLD が対数を取って NaN、ステム全滅
  * 一様に引き伸ばす          -> 子音まで伸びて「ssss」になる
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from orchestra.audio import lyrics as L
from orchestra.audio.singer import (
    SingStyle, Frames, pitch_line, runs, split_word, time_map, warp_formants,
)
from orchestra.music.tempo import TempoMap
from orchestra.music.timeline import NoteEvent


# ---------------------------------------------------------------------------
# 歌詞そのもの
# ---------------------------------------------------------------------------


def test_every_line_word_count_matches_its_text():
    """行の文字列と音節の分割が食い違っていないか。

    合成器は文字列のほうを喋るので、ここがずれると「どの音がどの単語か」の
    対応が最初から壊れる。
    """
    for line in L.all_lines():
        line.check()      # 食い違えば ValueError
        spoken = line.text.replace(",", "").replace(".", "").split()
        assert len(spoken) == len(line.words), line.text


def test_section_syllable_counts_match_the_score():
    """区間の音節数は、この曲の音符数と一致していなければならない。

    実測で Verse 39音 / Chorus 26音。ここが合わないなら採譜か音節の切り方が
    間違っているので、合わないまま先へ進んではいけない。
    """
    assert L.VERSE_SYLLABLES == 39
    assert L.CHORUS_SYLLABLES == 26


def _events(beats, pitch=69, dur=0.5):
    return [NoteEvent(beat=b, pitch=pitch, velocity=90, duration_beats=dur,
                      track="Melody") for b in beats]


def test_one_syllable_per_note_when_counts_match():
    evs = _events([i * 0.5 for i in range(L.VERSE_SYLLABLES)])
    plans = L.plan_melody(evs, beats_per_bar=4.0)
    assert sum(len(p.targets) for p in plans) == len(evs)
    assert [t.text for p in plans for t in p.targets] == [
        s for ln in L.VERSE_LINES for s in ln.syllables]


def test_targets_are_ordered_and_non_overlapping():
    evs = _events([i * 0.5 for i in range(L.VERSE_SYLLABLES)])
    for p in L.plan_melody(evs, beats_per_bar=4.0):
        for t in p.targets:
            assert t.end_beat > t.start_beat
        for a, b in zip(p.targets, p.targets[1:]):
            assert b.start_beat >= a.end_beat - 1e-9


def test_fewer_notes_than_syllables_still_sings_every_syllable():
    """対旋律は主旋律より音符が少ない（32 対 39）。音節を落とさずに詰める。"""
    line = L.VERSE_LINES[0]                      # 10音節
    evs = _events([i * 1.0 for i in range(6)])   # 6音
    plan = L._fit(line, evs)
    assert len(plan.targets) == line.n_syllables
    assert plan.start_beat == pytest.approx(evs[0].beat)
    assert plan.end_beat <= evs[-1].beat + evs[-1].duration_beats + 1e-9


def test_counterpoint_borrows_the_verse_under_the_chorus():
    """この曲の看板 ―― Chorus の裏で Verse を歌う。"""
    bpb = 4.0
    mel = _events([i * 0.5 for i in range(L.VERSE_SYLLABLES)])
    mel += _events([8 * bpb + i * 0.5 for i in range(L.CHORUS_SYLLABLES)])
    mplans = L.plan_melody(mel, bpb)

    cnt = _events([8 * bpb + i * 0.6 for i in range(16)])
    cplans = L.plan_counterpoint(cnt, mplans, beat_shift=8 * bpb)

    assert cplans, "対旋律に歌詞が付かなかった"
    sung = {p.line.text for p in cplans}
    assert sung <= {ln.text for ln in L.VERSE_LINES}, \
        f"Chorus の裏で歌うのは Verse のはず: {sung}"


def test_short_leftover_gets_a_tag_not_a_truncated_line():
    """曲末に2音だけ残るような場合、行を途中で切らず締めの言葉を当てる。"""
    plans = L._chunk(_events([0.0, 0.5]), L.VERSE_LINES)
    assert len(plans) == 1
    assert len(plans[0].targets) == 2
    assert plans[0].line.text == L.TAGS[2].text


# ---------------------------------------------------------------------------
# 時間の張り替え
# ---------------------------------------------------------------------------


def _frames(voiced_mask, power=None):
    n = len(voiced_mask)
    sp = np.ones((n, 8)) * (1.0 if power is None else 0.0)
    if power is not None:
        sp += np.asarray(power)[:, None]
    return Frames(f0=np.where(voiced_mask, 300.0, 0.0), sp=sp,
                  ap=np.full((n, 8), 0.1), rate=24000)


def test_time_map_hits_the_requested_length_exactly():
    fr = _frames(np.ones(40, dtype=bool))
    for m in (10, 40, 137):
        idx = time_map(fr, 0, 40, m)
        assert len(idx) == m
        assert np.all(np.diff(idx) >= -1e-9), "写像が単調でない"
        assert 0 <= idx.min() and idx.max() < 40


def test_stretching_lands_on_the_vowel_not_the_consonant():
    """一様に伸ばすと子音まで伸びて「ssss」になる。伸びるのは母音だけ。"""
    voiced = np.zeros(40, dtype=bool)
    voiced[10:30] = True                      # 母音20 / 子音20 フレーム
    fr = _frames(voiced)
    idx = time_map(fr, 0, 40, 120)            # 全体で3倍に伸ばす

    from_vowel = int(((idx >= 10) & (idx < 30)).sum())
    from_cons = 120 - from_vowel

    # 子音は自然な速さのまま通る（入力20フレーム -> 出力も20フレーム前後）。
    # 余った80フレームは全部母音が吸う。一様に伸ばすなら 60/60 になるので、
    # そこと明確に離れていることを見る。
    assert from_cons == pytest.approx(20, abs=3), (
        f"子音が {from_cons} フレームに伸びている（入力は20）")
    assert from_vowel >= 95, f"母音が {from_vowel} フレームしか伸びていない"


def test_time_map_survives_a_fully_unvoiced_syllable():
    fr = _frames(np.zeros(20, dtype=bool))
    idx = time_map(fr, 0, 20, 60)
    assert len(idx) == 60 and np.all(np.isfinite(idx))


# ---------------------------------------------------------------------------
# 音程の線
# ---------------------------------------------------------------------------


_FLAT = SingStyle(vibrato_cents=0.0, scoop_cents=0.0, drift_cents=0.0,
                  detune_cents=0.0)


def test_rest_does_not_drag_the_note_pitch_down():
    """休符を跨いで均すと音符の端が 0 側へ落ちる（実際に 27Hz まで落ちた）。"""
    note = np.zeros(300)
    note[:100] = 400.0
    note[200:] = 500.0                        # 100..200 は休符
    voiced = note > 0
    out = pitch_line(note, voiced, _FLAT, 0.005, np.random.default_rng(0))

    sung = out[voiced]
    assert sung.min() > 350.0, f"音符の端が {sung.min():.0f}Hz まで落ちている"
    assert sung.max() < 560.0


def test_pitch_line_tracks_the_written_pitch():
    note = np.full(200, 440.0)
    out = pitch_line(note, note > 0, _FLAT, 0.005, np.random.default_rng(0))
    cents = 1200 * np.log2(np.median(out) / 440.0)
    assert abs(cents) < 5.0, f"{cents:+.1f} cent ずれている"


def test_vibrato_only_on_notes_long_enough_to_hold():
    st = SingStyle(vibrato_cents=40.0, vibrato_onset_s=0.28, vibrato_ramp_s=0.1,
                   scoop_cents=0.0, drift_cents=0.0)
    rng = np.random.default_rng(0)
    short = pitch_line(np.full(30, 440.0), np.ones(30, bool), st, 0.005, rng)
    long_ = pitch_line(np.full(300, 440.0), np.ones(300, bool), st, 0.005, rng)
    assert short.std() < 0.5, "短い音符にビブラートが掛かっている"
    assert long_.std() > 1.0, "伸ばす音符にビブラートが掛かっていない"


def test_runs_finds_contiguous_regions():
    m = np.array([0, 1, 1, 0, 0, 1, 0], dtype=bool)
    assert runs(m) == [(1, 3), (5, 6)]


# ---------------------------------------------------------------------------
# 細部
# ---------------------------------------------------------------------------


def test_split_word_cuts_at_the_energy_valley():
    sr = 24000
    t = np.arange(int(0.4 * sr)) / sr
    x = np.sin(2 * np.pi * 300 * t)
    x[int(0.18 * sr):int(0.22 * sr)] *= 0.02       # 0.20s あたりに谷
    (a0, a1), (b0, b1) = split_word(x, sr, 0.0, 0.4, 2)
    assert a1 == pytest.approx(b0)
    assert 0.16 < a1 < 0.24, f"谷でない場所 {a1:.3f}s で切っている"


def test_formant_warp_moves_the_peak_up():
    sp = np.zeros((3, 200))
    sp[:, 40] = 1.0
    out = warp_formants(sp, 1.25)
    assert int(np.argmax(out[0])) == pytest.approx(50, abs=1)
    assert warp_formants(sp, 1.0) is sp        # 等倍なら触らない


# ---------------------------------------------------------------------------
# 通し
# ---------------------------------------------------------------------------


def _fake_utterance(rate=24000):
    """母音らしい合成音。2単語ぶん鳴らして、間に無音を置く。"""
    from orchestra.audio.tts import Utterance, WordMark

    n = int(0.6 * rate)
    t = np.arange(n) / rate
    x = np.zeros(n)
    for k in range(1, 12):                     # 250Hz の倍音列 = 有声音
        x += np.sin(2 * np.pi * 250 * k * t) / k
    x *= np.exp(-((t - 0.3) ** 2) / 0.05)      # 山なりの包絡
    x[int(0.27 * rate):int(0.30 * rate)] *= 0.05   # 単語の切れ目
    x /= np.abs(x).max()
    return Utterance(text="Ah oh.", voice="test", audio=x.astype(np.float32),
                     rate=rate,
                     words=[WordMark("Ah", 0.02, 0.25), WordMark("oh", 0.30, 0.28)])


def test_sing_line_end_to_end():
    """通しで動くこと。長さがぴったりで、非有限値が1つも出ないこと。

    包絡がちょうど 0 になると WORLD が対数を取って NaN になり、ステムが
    まるごと無音になる。実際にそうなったので、ここで押さえる。
    """
    pytest.importorskip("pyworld")
    from orchestra.audio.singer import sing_line

    line = L._L("Ah oh.", "Ah", "oh")
    plan = L.LinePlan(line, [
        L.SyllableTarget("Ah", 0.0, 1.0, 69),
        L.SyllableTarget("oh", 1.0, 2.0, 72),
    ])
    sung = sing_line(_fake_utterance(), plan, TempoMap.constant(60.0), _FLAT)

    assert sung.n_bad == 0, f"非有限値が {sung.n_bad} サンプル出ている"
    assert np.all(np.isfinite(sung.audio))
    assert len(sung.audio) / sung.rate == pytest.approx(2.0, abs=0.02)
    assert np.abs(sung.audio).max() > 1e-3, "無音になっている"
    assert sung.start_s == pytest.approx(0.0)


def test_word_count_mismatch_is_refused_not_guessed():
    """合成器の単語数と歌詞が食い違ったら止める。

    ずれたまま進むと以降の音節が全部1つずつずれて、言葉として崩れる。
    黙って続けるより落ちたほうがよい。
    """
    from orchestra.audio.singer import syllable_spans

    with pytest.raises(ValueError, match="word count"):
        syllable_spans(_fake_utterance(), L._L("Ah oh ee.", "Ah", "oh", "ee"))


def test_variance_keeps_each_stage_with_its_own_files():
    """工程ごとに同名のファイルを持つバンクで、別工程のものを掴まない。

    Tiger は `linguistic.onnx` を dsdur/ と dspitch/ の両方に持ち、音素表も
    3語違う。名前だけで探すと音長の予測に音程用のモデルが渡り、`ph_dur` が
    足りずに**1行も歌わない**（例外は行ごとに握られるので止まらない）。
    """
    pytest.importorskip("onnxruntime")
    root = Path(__file__).resolve().parent.parent / "models" / "diffsinger" / "tiger"
    if not root.exists():
        pytest.skip("voicebank not fetched")
    from orchestra.audio.svs import Variance, Voicebank

    vb = Voicebank.load(root)
    var = Variance.load(vb)

    assert "word_dur" in {i.name for i in var.ling_dur.get_inputs()}
    assert "ph_dur" in {i.name for i in var.ling_pitch.get_inputs()}
    assert len(var.token) == 116 and len(var.token_pitch) == 113
    assert not np.array_equal(var.emb_dur["tiger_fresh"], vb.speakers["tiger_fresh"])
