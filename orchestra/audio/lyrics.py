"""歌詞を行として持ち、その音節を旋律の音符へ割り当てる。

なぜ「行」なのか
----------------
最初は音節ごとに合成して並べた。これは失敗だった。単独で喋らせた
「イッツ」「ア」「ワールド」を繋いでも、**渡り**（前後の音の影響で口の形が
変わること）が無いので言葉に聞こえない。人はそういう喋り方をしない。

なので**行をまるごと自然に喋らせ、あとから音程と長さだけを差し替える**。
子音も渡りも息継ぎも、喋ったときのまま残る。ここで持つのは「行の文字列」と
「単語ごとの音節の切り方」の2つ。

音節と音符の対応
----------------
この曲では区間の合計がぴったり合う（実測）:

    Verse (bar 0-7)   旋律 39音  /  歌詞 39音節
    Chorus(bar 8-15)  旋律 26音  /  歌詞 26音節

**行の切れ目は小節線と一致しない** ―― アウフタクトがあるので、2小節ごとに
数えると 9/10/12/8 のようにずれる。区間で数えて、区間の中を音節数で
区切るのが正しい。

合成器が返す単語と、ここの `words` は**数も順序も一致していなければならない**。
一致しないと、どの音がどの単語か分からなくなる。実行時に検算する。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    """歌詞の1行。合成に渡す文字列と、単語ごとの音節の切り方。"""

    text: str
    words: tuple[tuple[str, ...], ...]

    @property
    def syllables(self) -> tuple[str, ...]:
        return tuple(s for w in self.words for s in w)

    @property
    def n_syllables(self) -> int:
        return sum(len(w) for w in self.words)

    def check(self) -> None:
        """行の文字列と、単語の分割が食い違っていないか。"""
        spoken = self.text.replace(",", "").replace(".", "").split()
        joined = ["".join(w) for w in self.words]
        if len(spoken) != len(joined):
            raise ValueError(
                f"word count does not match: sentence {len(spoken)} / split "
                f"{len(joined)}\n"
                f"  {self.text!r}\n  {joined}")


def _L(text: str, *words: str) -> Line:
    """`_L("It's a world", "It's", "a", "world")` ―― `-` で音節を切る。"""
    line = Line(text, tuple(tuple(w.split("-")) for w in words))
    line.check()
    return line


# --- Verse: 39音節 ---------------------------------------------------------
VERSE_LINES: tuple[Line, ...] = (
    _L("It's a world of laughter, a world of tears.",
       "It's", "a", "world", "of", "laugh-ter", "a", "world", "of", "tears"),          # 10
    _L("It's a world of hopes and a world of fears.",
       "It's", "a", "world", "of", "hopes", "and", "a", "world", "of", "fears"),       # 10
    _L("There's so much that we share, that it's time we're aware.",
       "There's", "so", "much", "that", "we", "share",
       "that", "it's", "time", "we're", "a-ware"),                                     # 12
    _L("It's a small world after all.",
       "It's", "a", "small", "world", "af-ter", "all"),                                # 7
)

# --- Chorus: 26音節 --------------------------------------------------------
_HOOK = _L("It's a small world after all.",
           "It's", "a", "small", "world", "af-ter", "all")                             # 7
CHORUS_LINES: tuple[Line, ...] = (_HOOK, _HOOK, _HOOK,
    _L("It's a small, small world.",
       "It's", "a", "small", "small", "world"),                                        # 5
)

# 余った数音のための締め。曲の最後に2音だけ残る、といったときに使う。
TAGS: dict[int, Line] = {
    1: _L("World.", "world"),
    2: _L("Small world.", "small", "world"),
    3: _L("Small, small world.", "small", "small", "world"),
    4: _L("A small, small world.", "a", "small", "small", "world"),
    5: CHORUS_LINES[3],
}

VERSE_SYLLABLES = sum(x.n_syllables for x in VERSE_LINES)     # 39
CHORUS_SYLLABLES = sum(x.n_syllables for x in CHORUS_LINES)   # 26


def all_lines() -> list[Line]:
    """合成しておくべき行の集合（同じ文は1度でよい）。"""
    seen: dict[str, Line] = {}
    for ln in (*VERSE_LINES, *CHORUS_LINES, *TAGS.values()):
        seen.setdefault(ln.text, ln)
    return list(seen.values())


# ---------------------------------------------------------------------------
# 音符への割り当て
# ---------------------------------------------------------------------------


@dataclass
class SyllableTarget:
    """1音節を「いつ・どの高さで」歌うか。"""

    text: str
    start_beat: float
    end_beat: float
    pitch: int


@dataclass
class LinePlan:
    """1行ぶんの歌唱計画。`targets` は行の音節と1対1で並ぶ。"""

    line: Line
    targets: list[SyllableTarget]

    @property
    def start_beat(self) -> float:
        return self.targets[0].start_beat

    @property
    def end_beat(self) -> float:
        return self.targets[-1].end_beat


def _spans(events, legato: float = 1.6) -> list[tuple[float, float]]:
    """各音符の [開始, 終了) 拍。

    歌はレガートが基本なので、**次の音符まで伸ばす**。ただし休符を跨いで
    伸ばすのは違うので、間隔が音価の `legato` 倍を超えたら音価で切る。
    """
    out = []
    for i, e in enumerate(events):
        if i + 1 < len(events):
            gap = events[i + 1].beat - e.beat
            end = (events[i + 1].beat if gap <= e.duration_beats * legato
                   else e.beat + e.duration_beats)
        else:
            end = e.beat + e.duration_beats
        out.append((e.beat, max(end, e.beat + 1e-3)))
    return out


def _fit(line: Line, events) -> LinePlan:
    """行の音節を、渡された音符に載せる。

    音節数と音符数が同じならそのまま1対1。音節のほうが多いときは、1つの
    音符に複数の音節を詰めてその音価を分ける ―― 対旋律のように、同じ歌詞を
    少ない音符で歌う場合がこれ。
    """
    syls = line.syllables
    n, k = len(syls), len(events)
    if k == 0:
        raise ValueError(f"no notes for: {line.text!r}")
    spans = _spans(events)
    # 音節 j -> 音符 idx。連続した音節が同じ音符に固まるように割る。
    owner = [min(int(j * k / n), k - 1) for j in range(n)]
    targets: list[SyllableTarget] = []
    for j, s in enumerate(syls):
        i = owner[j]
        share = [q for q in range(n) if owner[q] == i]
        pos, m = share.index(j), len(share)
        b0, b1 = spans[i]
        step = (b1 - b0) / m
        targets.append(SyllableTarget(s, b0 + pos * step, b0 + (pos + 1) * step,
                                      events[i].pitch))
    return LinePlan(line, targets)


def _chunk(events, lines: tuple[Line, ...]) -> list[LinePlan]:
    """区間の音符を、行ごとの音節数で順に切り分ける。"""
    plans: list[LinePlan] = []
    i = 0
    for ln in lines:
        if i >= len(events):
            break
        take = min(ln.n_syllables, len(events) - i)
        if take < ln.n_syllables:
            # 行の途中で音符が尽きた。中途半端に歌わせるより、その数に
            # 合った締めの言葉を当てる。
            tag = TAGS.get(take)
            if tag is None:
                break
            plans.append(_fit(tag, events[i:i + take]))
            i += take
            break
        plans.append(_fit(ln, events[i:i + take]))
        i += take
    if i < len(events):
        rest = len(events) - i
        tag = TAGS.get(rest)
        if tag is not None:
            plans.append(_fit(tag, events[i:]))
    return plans


def plan_melody(events, beats_per_bar: float, verse_bars: int = 8,
                chorus_bars: int = 8) -> list[LinePlan]:
    """主旋律に Verse / Chorus を割り当てる。

    区間の判定は**小節**で行う（通し番号で数えると、音符が1つ増減しただけで
    以降の歌詞が全部ずれる）。区間の中は音節数で切る。
    """
    period = verse_bars + chorus_bars
    groups: list[tuple[bool, list]] = []
    for e in sorted(events, key=lambda x: x.beat):
        is_verse = (int(e.beat // beats_per_bar) % period) < verse_bars
        if not groups or groups[-1][0] != is_verse:
            groups.append((is_verse, []))
        groups[-1][1].append(e)

    plans: list[LinePlan] = []
    for is_verse, evs in groups:
        plans.extend(_chunk(evs, VERSE_LINES if is_verse else CHORUS_LINES))
    return plans


def plan_counterpoint(events, melody_plans: list[LinePlan],
                      beat_shift: float) -> list[LinePlan]:
    """対旋律に、主旋律の同じ位置の歌詞を歌わせる。

    この曲の看板は「Chorus の裏で Verse を歌う」対位法。対旋律は主旋律の
    Verse を簡略化したもので**音符の数が一致しない**（32 対 39）ので、順番に
    詰めると歌詞が後ろへずれる。だから `beat_shift` だけ戻した位置にある
    主旋律の行を引き当て、その行を対旋律の音符数で歌う。数が足りないぶんは
    1音符に複数音節が乗る ―― 対旋律が実際にやっていることと同じ。
    """
    if not melody_plans:
        return []
    bounds = [(p.start_beat, p.end_beat, p) for p in melody_plans]
    buckets: dict[int, list] = {}
    for e in sorted(events, key=lambda x: x.beat):
        want = e.beat - beat_shift
        i = min(range(len(bounds)),
                key=lambda k: abs((bounds[k][0] + bounds[k][1]) / 2 - want))
        buckets.setdefault(i, []).append(e)
    return [_fit(bounds[i][2].line, evs) for i, evs in sorted(buckets.items()) if evs]
