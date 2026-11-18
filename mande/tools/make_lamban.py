#!/usr/bin/env python3
"""Lamban（Djelidon / Djelifoli / Lamba）の採譜を MIDI として書き出す。

    python tools/make_lamban.py

Lamban は**ジェリ（グリオ）のためだけに踊られる曲**。マリ起源で、バラフォン上で
Kouyaté 家によって作られたと伝わる。伝統的な楽器編成は balafon・djeli dundun・
tama で、ギニアとセネガルではそこに djembe と dunun が加わる。旋律楽器と打楽器の
混成が「後付けのアレンジ」ではなく伝承そのものである数少ない曲。

★ノート番号の読み替え★
--------------------
この曲の MIDI ノート番号は**音高を意味しない**。`orchestra/audio/mande.py` が
合成する各楽器のゾーン番号で:

    djembe / lead   60=bass(gun)  62=tone(go)  64=slap(pa)
    dundunba/sangban 60=開放  61=ミュート  72=kenken 鈴
    tama            55..67 = 13段の音程  68=上行ベンド  69=下行ベンド
    balafon         60+k  = k 枚目の棒（等間隔七音、1ステップ 171.43 cent）
    kora            48+k  = k 番目の弦（Sauta 調弦）
    ngoni           48+k  = k 番目の弦

音律は合成時に焼き込んであるので、12平均律を経由しない。`note_name()` の表示は
この曲では意味を成さない。

★出典の区別★
------------
定数表の各行に [DOC] か [RECON] を付けてある。[DOC] は出典のある採譜、
[RECON] は「出典のある制約を満たす私の再構成」。**それらしく捏造した8本より、
出典のある4本と正直な空白のほうが価値がある**という方針で書いている。

主な出典:
  * Paul Nas / WAP Pages, "African Rhythms" 採譜集 (PDF) pp.64-71 (Lamban),
    pp.97-100 (Griot)  https://djembefoley.ca/wp-content/uploads/2012/03/african-rhythms.pdf
  * Joe L. Williams Jr., "Transmitting the Mande Balafon", PhD diss.,
    Univ. of Maryland 2006, pp.113-120（Lamban のバラフォン伴奏形5種）
  * Malcolm Smith / Nansady Keita 伝, Vitae Drum Circle（110 BPM, 4/4）
  * kora-music.com「Scales and Tunings」（Sauta ほか4調弦の cent 値）

★拍子について★
--------------
**Lamban は 2分割（4/4）**。12/8 で書かれた採譜も存在するが、資料を突き合わせると
それは同じ音楽をスウィングして読んだものであって別の地方版ではない。Charry は
4/4 を所与として「なぜジェンベ奏者は 4/4 の伴奏形を選んだのか（三連のノリで
弾かれることもあるのに）」と問うている。Nas は拍子を変える代わりに
**「2番目と10番目のパルスがわずかに遅れる」**という微視的な指示を書いている。

そして Williams のバラフォン採譜は 4/4 の中が**三連符で埋まっている**。
つまり同じ小節の中で dunun/djembe が4分割、balafon が3分割を刻む ―― 
これが「二分割と三分割は矛盾ではなく相補である」という Williams の主張の実体で、
この曲の一番面白いところ。ここではそのまま実装している
（balafon は step 1/3 拍、それ以外は 1/4 拍）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestra.music.midi import write_midi  # noqa: E402
from orchestra.music.tempo import Meter, TempoChange, TempoMap  # noqa: E402
from orchestra.music.timeline import NoteEvent, Timeline  # noqa: E402

BPB = 4.0            # 拍/小節
ACC, NRM, SFT = 110, 85, 62

# ---------------------------------------------------------------------------
# 打楽器のパターン（16分 = 1小節16ステップ。位置は1始まり）
# ---------------------------------------------------------------------------
# [DOC] Ponda O'Bryan（Nas p.65）。dunun はそれぞれ自分の鈴(kenken)を持つ。
#
#                 1 e & a | 2 e & a | 3 e & a | 4 e & a
#   KENKENI  bell x . x . | x . x . | x . x . | x . x .
#            drum K . . . | K . K . | K . . . | K . K .
#   SANGBAN  bell x . x x | . x x . | x . x x | . x x .
#            drum S . . S | . . S . | S . . S | . . S .   <- 3+3+2 | 3+3+2
#   DUNUNBA  bell x . x x | . x x . | x . x . | x . x .
#            drum . . . D | . . D . | . . . . | D . D .
#
# サンバンの 3+3+2 がこの曲の署名。Nas の Lamban 項と Griot 項の両方に同じ形で
# 出てくるので、1つだけ実装するならこれ。
SANGBAN_DRUM = [1, 4, 7, 9, 12, 15]                            # [DOC]
SANGBAN_BELL = [1, 3, 4, 6, 7, 9, 11, 12, 14, 15]              # [DOC]
DUNUNBA_DRUM = [4, 7, 13, 15]                                  # [DOC]
DUNUNBA_BELL = [1, 3, 4, 6, 7, 9, 11, 13, 15]                  # [DOC]
KENKENI_BELL = [1, 3, 5, 7, 9, 11, 13, 15]                     # [DOC] 8分の等間隔

# [DOC] Malcolm Smith / Nansady Keita 伝、8分格子、110 BPM。
#   djembe 1   B . O O . . S S      ("ride your bicycle")
# 16分に直すと 1=B, 5=O, 7=O, 13=S, 15=S。
#
# Serge Blanc の 16分版（T T S S | . . S S | T T S S | B . S S）も採譜として
# あるが、16分が4つ続く上に「2番目と10番目を遅らせる」スウィングが掛かるため、
# 打点間隔が 110ms を割って安全ゲートに落ちる。**8分格子のほうを採る** ――
# こちらも同格の採譜で、しかも実機で鳴らせる。
DJEMBE_ACC = [(1, "bass"), (5, "tone"), (7, "tone"), (13, "slap"), (15, "slap")]  # [DOC]

# [DOC] Ponda O'Bryan の合図（Nas p.65）。T T S S | T S T T | S . . . | . . . .
CALL = [(1, "tone"), (2, "tone"), (3, "slap"), (4, "slap"),
        (5, "tone"), (6, "slap"), (7, "tone"), (8, "tone"), (9, "slap")]         # [DOC]

# [DOC] Griot 項（Nas p.98）のブレイク。F . T T | . T . T | T . S S | S . . .
BREAK = [(1, "slap"), (3, "tone"), (4, "tone"), (6, "tone"), (8, "tone"),
         (9, "tone"), (11, "slap"), (12, "slap"), (13, "slap")]                  # [DOC]

# [DOC] Griot 項（Nas p.98）の échauffement（加熱部）。
#   djembe   T T S S | . S S S | . S S S | . S S S   (1小節目)
#            . S S S | . S S S | . S S S | . S S S   (以降くり返し)
#   dununba  D . D D | . D D D | . D D D | . D D D
ECH_DJEMBE_FIRST = [(1, "tone"), (2, "tone"), (3, "slap"), (4, "slap")] + \
                   [(p, "slap") for p in (6, 7, 8, 10, 11, 12, 14, 15, 16)]      # [DOC]
ECH_DJEMBE = [(p, "slap") for p in (2, 3, 4, 6, 7, 8, 10, 11, 12, 14, 15, 16)]   # [DOC]
ECH_DUNUNBA = [1, 3, 4, 6, 7, 8, 10, 11, 12, 14, 15, 16]                         # [DOC]

# [DOC] Griot 項の終止。dununba: x D D D | . x D . | x . x . | D D . D
END_DUNUNBA = [2, 3, 4, 7, 13, 14, 16]                                           # [DOC]
END_DJEMBE = [(2, "slap"), (3, "slap"), (4, "slap"), (7, "slap"),
              (11, "tone"), (12, "tone")]                                        # [DOC]

# ---------------------------------------------------------------------------
# バラフォン（三連 = 1小節12パルス。位置は1始まり）
# ---------------------------------------------------------------------------
# [DOC] リズム。Williams 2006 Fig.7（Siriman Kouyate, Acc.1）を実際に読んだもの。
# 拍1と拍3は1打、拍2と拍4は三連の1つ目と3つ目に1打ずつ:
#
#                拍1      拍2      拍3      拍4
#   pulse        1 2 3  | 4 5 6  | 7 8 9  | 10 11 12
#   onset        x . .  | x . x  | x . .  |  x  .  x
#
# Williams の本文の記述と一致する ――「伴奏形2は拍2と拍4の1番目と3番目の
# パルスに同じ音の反復を含む」。これで採譜（図）と本文が独立に裏を取っている。
BALAFON_PULSES = [1, 4, 6, 7, 10, 12]                                            # [DOC]

# [RECON] 音そのもの。Williams の図は走査画像で、音符の高さまでは確度をもって
# 読めなかった（中途半端な自動採譜は「もっともらしく間違う」ので採らなかった）。
# 以下は本文で明示されている制約だけを満たす再構成:
#   * 2小節周期
#   * 片手が最低音の反復（Charry「一方の手で同じ音を刻み続けるのがバラらしさ」）
#   * 拍2と拍4は同じ音の反復
#   * 和声は3拍目で変わる
#   * 下行する走句が即興の語彙
# 数字は棒の番号（0 起点、等間隔七音）。主音を 7（＝下から2オクターブ目）に置く。
BALA_TONIC = 7
BALAFON_CYCLE = [                                                                # [RECON]
    # (小節, パルス, 棒番号)
    (0, 1, 7), (0, 4, 14), (0, 6, 14), (0, 7, 7), (0, 10, 14), (0, 12, 13),
    (1, 1, 7), (1, 4, 13), (1, 6, 13), (1, 7, 11), (1, 10, 12), (1, 12, 11),
]
# 下行する走句（birimintingo に相当する語彙）。バラフォンのソロ区間で使う。
BALAFON_RUN = [18, 17, 16, 15, 14, 13, 12, 11]                                   # [RECON]

# ---------------------------------------------------------------------------
# コラ / ンゴニ（16分格子）
# ---------------------------------------------------------------------------
# [RECON] Lamban の kumbengo（循環定型）の採譜は見つからなかった。
# 出典があるのは奏法の原理だけ:
#   * 親指が低音で周期を刻み、人差し指が上の弦で旋律を運ぶ
#   * kumbengo は「すべての変奏が戻ってくる点」（Racanelli 2012）
#   * birimintingo は下行する走句で、フレーズの切れ目に置き、必ず kumbengo に戻る
# ここでは低音をサンバンの 3+3+2 に合わせ、上声を裏の16分に置いた。
# 密度も出典に合わせている。kumbengo は「2小節周期の定型」であって
# 毎小節を埋めるものではない ―― 小節Aで低音が周期を示し、小節Bで上声が答える。
KORA_BASS_A = [(1, 7), (4, 11), (7, 7), (9, 14), (12, 11), (15, 7)]              # [RECON]
KORA_BASS_B = [(1, 7), (7, 11), (12, 7)]                                         # [RECON]
KORA_TREBLE = [(3, 18), (6, 17), (11, 18), (14, 16)]                             # [RECON] 小節Bのみ
KORA_RUN = [20, 19, 18, 17, 16, 15, 14, 13]                                      # [RECON]

# [RECON] ンゴニは1小節の短い周期で、低音域を刻む。役割の水準でしか出典が無い。
NGONI_RIFF = [(1, 0), (7, 4), (11, 0), (14, 2)]                                  # [RECON]

# [RECON] タマ。Lamban のタマ譜は見つからなかった。出典があるのは
# 「ジェリの三大楽器の1つ」「紐を締めて音程を連続的に変える」という役割だけ。
# 周期性の最も低いパートとして、2小節周期の終わりに短い応答を置く。
TAMA_ANSWER = [(11, 59), (12, 61), (13, 63), (15, 68)]                           # [RECON]
TAMA_ECH = [(1, 60), (3, 64), (5, 60), (7, 65), (9, 60), (11, 66), (13, 61), (15, 69)]

DJEMBE_ZONE = {"bass": 60, "tone": 62, "slap": 64}
DUNUN_OPEN, DUNUN_MUTE, DUNUN_BELL = 60, 61, 72

# ---------------------------------------------------------------------------
# 構成
# ---------------------------------------------------------------------------
# [RECON] 区分と長さ。仕組みは全部出典がある（合図で始まる／サンバンが
# échauffement を始めサンバンの一撃で終える／dununba は加熱部で連打になる／
# 「マリの曲は歌と共に遅く始まり踊りと共に速くなる」）が、小節数の割り振りは私。
#
# テンポの上限 120 BPM は安全ゲートから来ている ―― 発音する動作の間隔は
# 110ms 以上でなければならず、16分音符は 120 BPM で 125ms。ここが天井。
SECTIONS = [
    ("balafon alone",  4, 96),    # バラフォン独奏。調律と2小節周期を示す
    ("strings in",     4, 96),    # コラとンゴニが入る
    ("karignan in",    2, 96),    # 刻みが入って周期が固まる
    ("call",           1, 96),    # ソロ djembe の合図。dunun は最後の拍で入る
    ("groove slow",   12, 100),   # 遅い「歌」の部分
    ("tama answers",   8, 104),   # タマの応答（sataro に相当）
    ("balafon runs",   8, 108),   # バラフォンの下行走句
    ("break",          1, 108),   # ブレイク（Griot 項）
    ("groove fast",   12, 112),   # 速い部分。djembe のソロ語彙
    ("echauffement",  12, 116),   # 加熱部。dununba 連打
    ("ending",         2, 120),   # 終止（Griot 項）
]


def bar_of(name: str) -> int:
    b = 0
    for n, nb, _ in SECTIONS:
        if n == name:
            return b
        b += nb
    raise KeyError(name)


def n_bars(name: str) -> int:
    return dict((n, nb) for n, nb, _ in SECTIONS)[name]


TOTAL_BARS = sum(nb for _, nb, _ in SECTIONS)


def p16(bar: int, pos: int) -> float:
    """小節と16分位置(1始まり)から拍へ。"""
    return bar * BPB + (pos - 1) * 0.25


def p12(bar: int, pulse: int) -> float:
    """小節と三連パルス(1始まり)から拍へ。"""
    return bar * BPB + (pulse - 1) / 3.0


def build() -> Timeline:
    ev: list[NoteEvent] = []

    # 音符の長さは**次の打が来うる間隔より短く**保つ。同じ音高が時間的に重なると
    # MIDI の note_on/note_off の対応が壊れて音が消える（加熱部のドゥンドゥンバが
    # 12打中8打になっていた）。鳴り続けは `render.NOTE_TAIL_S` の余韻が持つ。
    MAX_DUR = 0.2      # 拍。16分刻み(0.25拍)より短い

    def add(track, beat, pitch, vel=NRM, dur=0.2):
        dur = min(dur, MAX_DUR)
        ev.append(NoteEvent(beat=beat, pitch=int(pitch), velocity=int(vel),
                            duration_beats=dur, track=track))

    # -- バラフォン: 全編 -----------------------------------------------------
    runs_from = bar_of("balafon runs")
    runs_to = runs_from + n_bars("balafon runs")
    for bar in range(TOTAL_BARS):
        # ソロ区間では2小節に1度、下行走句が定型を**置き換える**（差し込むのではなく）。
        # 重ねると同じ拍に2打が乗って打点間隔が 0 になり、安全ゲートに落ちる。
        run_bar = runs_from <= bar < runs_to and bar % 2 == 1
        if run_bar:
            # 三連1パルス刻み。1/6 拍だと 108 BPM で 93ms しか空かず 110ms を割る。
            for i, k in enumerate(BALAFON_RUN[:6]):
                add("Balafon", p12(bar, 7) + i / 3.0, 60 + k, ACC if i == 0 else NRM,
                    dur=0.28)
            # 走句の前半（拍1-2）は定型のまま
            for cb, pulse, k in BALAFON_CYCLE:
                if bar % 2 == cb and pulse <= 6:
                    add("Balafon", p12(bar, pulse), 60 + k,
                        ACC if pulse == 1 else NRM, dur=0.30)
            continue
        for cb, pulse, k in BALAFON_CYCLE:
            if bar % 2 != cb:
                continue
            add("Balafon", p12(bar, pulse), 60 + k, ACC if pulse == 1 else NRM, dur=0.30)

    # -- コラ / ンゴニ --------------------------------------------------------
    br = bar_of("break")
    run_bar = br - 1        # birimintingo は「フレーズの切れ目」―― ブレイク直前
    for bar in range(bar_of("strings in"), TOTAL_BARS):
        # 走句の小節は後半(拍3-4)を走句が置き換える。重ねない。
        limit = 8 if bar == run_bar else 99
        second = bar % 2 == 1
        for pos, k in (KORA_BASS_B if second else KORA_BASS_A):
            if pos <= limit:
                add("Kora", p16(bar, pos), 48 + k, ACC if pos == 1 else NRM, dur=0.5)
        if second:
            for pos, k in KORA_TREBLE:
                if pos <= limit:
                    add("Kora", p16(bar, pos), 48 + k, SFT, dur=0.3)
        for pos, k in NGONI_RIFF:
            add("Ngoni", p16(bar, pos), 48 + k, ACC if pos == 1 else NRM, dur=0.4)
    for i, k in enumerate(KORA_RUN):
        add("Kora", p16(run_bar, 9) + i * 0.25, 48 + k, ACC if i == 0 else NRM, dur=0.2)

    # -- 合図（r01 djembefola） ----------------------------------------------
    cb = bar_of("call")
    for pos, stroke in CALL:
        add("Lead", p16(cb, pos), DJEMBE_ZONE[stroke], ACC if pos <= 2 else NRM, dur=0.2)
    for pos, stroke in BREAK:
        add("Lead", p16(br, pos), DJEMBE_ZONE[stroke], ACC if pos == 1 else NRM, dur=0.2)
    # サンバンが échauffement を告げる合図（Nas: 「サンバンが加熱部を始める」）
    eb = bar_of("echauffement")
    add("Sangban", p16(eb - 1, 15), DUNUN_OPEN, ACC, dur=0.3)
    add("Sangban", p16(eb - 1, 16), DUNUN_OPEN, ACC, dur=0.3)

    # -- dunun と djembe: 合図の直後から ------------------------------------
    groove_from = cb + 1
    for bar in range(groove_from, TOTAL_BARS):
        in_ech = eb <= bar < eb + n_bars("echauffement")
        in_end = bar >= bar_of("ending")
        if in_end:
            for pos in END_DUNUNBA:
                add("Dundunba", p16(bar, pos), DUNUN_OPEN, ACC, dur=0.25)
            for pos, stroke in END_DJEMBE:
                add("Djembe", p16(bar, pos), DJEMBE_ZONE[stroke], ACC, dur=0.2)
            for pos in (1, 9):
                add("Sangban", p16(bar, pos), DUNUN_OPEN, ACC, dur=0.3)
            continue
        # サンバン: 太鼓と鈴。3+3+2 がこの曲の署名。
        # 加熱部を告げる合図の小節だけは、最後の打を合図が置き換える。
        skip = {15} if bar == eb - 1 else set()
        for pos in SANGBAN_DRUM:
            if pos in skip:
                continue
            add("Sangban", p16(bar, pos), DUNUN_OPEN, ACC if pos == 1 else NRM, dur=0.3)
        for pos in SANGBAN_BELL:
            if pos not in SANGBAN_DRUM:
                add("Sangban", p16(bar, pos), DUNUN_BELL, SFT, dur=0.2)
        # ドゥンドゥンバ: 加熱部では連打に変わる
        drum = ECH_DUNUNBA if in_ech else DUNUNBA_DRUM
        for pos in drum:
            add("Dundunba", p16(bar, pos), DUNUN_OPEN, ACC if pos in (1, 4) else NRM, dur=0.35)
        if not in_ech:
            for pos in DUNUNBA_BELL:
                if pos not in DUNUNBA_DRUM:
                    add("Dundunba", p16(bar, pos), DUNUN_BELL, SFT, dur=0.2)
        # ジェンベ
        if in_ech:
            pat = ECH_DJEMBE_FIRST if bar == eb else ECH_DJEMBE
        else:
            pat = DJEMBE_ACC
        for pos, stroke in pat:
            add("Djembe", p16(bar, pos), DJEMBE_ZONE[stroke],
                ACC if pos == 1 else NRM, dur=0.2)

    # -- タマ -----------------------------------------------------------------
    ta = bar_of("tama answers")
    for bar in range(ta, TOTAL_BARS):
        if bar >= bar_of("ending"):
            break
        if eb <= bar:
            for pos, z in TAMA_ECH:
                add("Tama", p16(bar, pos), z, NRM, dur=0.2)
        elif bar % 2 == 1:
            for pos, z in TAMA_ANSWER:
                add("Tama", p16(bar, pos), z, ACC if pos == 15 else NRM, dur=0.22)

    # -- テンポ ---------------------------------------------------------------
    changes, b = [], 0
    for _, nb, bpm in SECTIONS:
        if not changes or changes[-1].bpm != bpm:
            changes.append(TempoChange(beat=b * BPB, bpm=float(bpm)))
        b += nb
    tl = Timeline.from_events(ev, meter=Meter(4, 4),
                              total_beats=TOTAL_BARS * BPB, name="lamban")
    tl.tempo = TempoMap(changes)      # from_events は定数テンポしか作らない
    return tl


def check(tl) -> list[str]:
    """生成した時点で打点間隔を検算する。

    安全ゲート（`gesture_min_interval_s`, 既定 110ms）はコンパイル時に効くが、
    そこまで持ち越すと「どのパターンが悪いのか」が分からなくなる。**同じ拍に
    2打**と**110ms を割る間隔**は、採譜を書いた場所で捕まえるのが速い。
    """
    problems = []
    # 同じ音高の音符が時間的に重なっていないか。重なると MIDI の往復で音が消える。
    for name in tl.tracks():
        by_pitch = {}
        for e in tl.events_for(track=name):
            by_pitch.setdefault(e.pitch, []).append((e.beat, e.beat + e.duration_beats))
        for pitch, spans in by_pitch.items():
            spans.sort()
            for (a0, a1), (b0, _) in zip(spans, spans[1:]):
                if a1 > b0 + 1e-9:
                    problems.append(
                        f"{name}: 音高 {pitch} が拍 {b0:.3f} で重なっている"
                        f"（前の音が {a1:.3f} まで伸びている）。MIDI の往復で音が消える")
                    break

    for name in tl.tracks():
        beats = sorted(e.beat for e in tl.events_for(track=name))
        worst, at = 1e9, 0.0
        for a, b in zip(beats, beats[1:]):
            if b - a < 1e-9:
                problems.append(f"{name}: 拍 {a:.3f} に打点が重なっている")
                continue
            dt = tl.tempo.beats_to_seconds(b) - tl.tempo.beats_to_seconds(a)
            if dt < worst:
                worst, at = dt, a
        if worst < 0.110:
            problems.append(
                f"{name}: 最小の打点間隔が {worst*1000:.0f}ms（拍 {at:.2f}）。"
                "安全ゲートは 110ms 以上を要求する ―― テンポか細分を見直すこと")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="songs/lamban/lamban.mid")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    tl = build()
    problems = check(tl)
    if problems:
        print("打点間隔の検算に失敗:", file=sys.stderr)
        for pr in problems:
            print("  " + pr, file=sys.stderr)
        return 1
    path = write_midi(tl, root / args.out)

    print(f"Lamban  {TOTAL_BARS} bars / {tl.duration_s:.1f}s / {len(tl.events)} notes")
    print(f"\n{'section':<16}{'bars':>6}{'bpm':>6}")
    b = 0
    for name, nb, bpm in SECTIONS:
        print(f"{name:<16}{f'{b}-{b+nb-1}':>6}{bpm:>6}")
        b += nb
    print(f"\n{'track':<12}{'notes':>7}  zones")
    for name in tl.tracks():
        evs = tl.events_for(track=name)
        zs = sorted({e.pitch for e in evs})
        span = f"{zs[0]}-{zs[-1]}" if len(zs) > 3 else ",".join(map(str, zs))
        print(f"{name:<12}{len(evs):>7}  {span}")
    print(f"\n-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
