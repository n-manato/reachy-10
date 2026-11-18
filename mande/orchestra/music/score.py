"""`song.yaml`: what the piece is, what the parts are, and who plays them.

The separation that makes this scale: a **part** is musical (a melody line, a
pulse, the conductor) and a **robot** is hardware. `cast` maps one to the other.
Going from nine robots to ten is a registry entry plus a cast line -- no code
touches a robot count anywhere.

Part sources, in order of how much they are used:

``midi``      notes from a MIDI track, filtered by track name / pitch / channel.
              Each note fires the part's clip with its anchor aligned to the
              note. This is how pitched instruments (chime bars) work.
``pattern``   a text rhythm (``"X..x|x..x"``), either looped as a groove or
              fired per hit. This is how unpitched percussion works, and it
              needs no MIDI file at all.
``conductor`` generated procedurally from the timeline by
              :mod:`orchestra.conductor.patterns`.
``idle``      no part; secondary motion only. Spare robots still look alive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .midi import load_midi, note_number
from .tempo import Meter, TempoMap
from .timeline import NoteEvent, Timeline, parse_pattern, pattern_length_beats

KIND_MIDI = "midi"
KIND_PATTERN = "pattern"
KIND_CONDUCTOR = "conductor"
KIND_IDLE = "idle"
"""歌のパート。音源の取り方は midi と同じだが、`transpose` で主旋律から
ハモリを機械的に作れる。物理楽器が無く歌がメロディを担う構成のために追加した。"""
KINDS = (KIND_MIDI, KIND_PATTERN, KIND_CONDUCTOR, KIND_IDLE)


class ScoreError(ValueError):
    pass


@dataclass
class Part:
    """One musical role."""

    name: str
    kind: str = KIND_PATTERN
    clip: str = ""
    accent_clip: str = ""
    instrument: str = ""
    audio: str = ""
    gain: float = 1.0

    # 合奏の中での位置づけ。`gain` は音源の作り方の補正、こちらは**役割**。
    # 全ステムを同じ RMS に揃えるだけだと、足したときにパート数の多いほうが
    # 勝つ（楽器7本 対 歌2本で、歌が 4.5 dB 下に潜っていた）。誰が主役かを
    # ここに書く。
    mix_db: float = 0.0

    # midi source
    track: str = ""
    pitches: list[int] = field(default_factory=list)
    channel: int | None = None

    # 音源側の指定（ステム生成に使う）
    voice_preset: str = ""
    """orchestra.audio.render の楽器プリセット名。空なら instrument を使う。"""
    persona: str = ""
    """orchestra.motion.persona の「持ち姿」名。空なら voice_preset -> instrument。

    誰が何を演奏しているかを見せるのは、1音ごとの打点ではなく**常時の構え**の
    仕事（`orchestra/motion/persona.py` を参照）。"""
    transpose: int = 0
    """半音。ハモリを主旋律から機械的に作るのに使う（-3 で3度下など）。"""

    # pattern source
    pattern: str = ""
    step_beats: float = 0.25
    zone_cycle: list[int] = field(default_factory=list)
    """ループ型クリップのアンカーに、この順でゾーン（＝ノート番号）を割り当てる。

    カリニャンのように**往と復で音が違う**楽器のためにある。クリップの
    `anchors` は鳴る時刻しか持たず、既定では全部が同じ音になってしまうので、
    `[60, 61]` と書けば往・復・往・復…と交互に鳴る。
    `kind: pattern` かつ `pattern` 文字列を持たないパートにだけ効く。
    """
    repeat: bool = True
    start_bar: int = 0
    end_bar: float | None = None
    """小節。**小数可** ―― 終止和音が小節の途中にあるとき、リズムをそこで
    止めるのに要る。整数しか書けないと1小節ぶん鳴り続けてしまう。"""

    def __str__(self) -> str:
        return f"{self.name} ({self.kind}, {self.instrument or 'unassigned'})"


@dataclass
class Score:
    name: str
    title: str = ""
    source_dir: Path | None = None
    timeline: Timeline | None = None
    parts: dict[str, Part] = field(default_factory=dict)
    cast: dict[str, str] = field(default_factory=dict)
    lead_in_s: float = 2.5
    tail_s: float = 2.0
    bars_per_phrase: int = 4
    secondary: dict[str, Any] = field(default_factory=dict)
    intro: dict[str, Any] = field(default_factory=dict)
    outro: dict[str, Any] = field(default_factory=dict)
    finale: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- 前後の演出 ----------------------------------------------------------
    # `lead_in_s` を伸ばして代用しない。あれは「打点クリップの振りかぶりが
    # 収まる余裕」という別の意味を持っていて、`build.py` の振りかぶり警告が
    # それと比較している。30秒に伸ばすとその警告が永久に鳴らなくなる。
    # 前後の演出は独立した尺として持ち、T0（1拍目）だけを後ろへずらす。

    @property
    def intro_s(self) -> float:
        """紹介（指揮者の挨拶 + 各台の名乗り）の長さ。"""
        i = self.intro
        if not i:
            return 0.0
        n = len(i.get("order") or [])
        return (float(i.get("lead_s", 1.0))
                + float(i.get("greet_s", 3.5))
                + n * (float(i.get("slot_s", 2.0)) + float(i.get("gap_s", 0.3)))
                + float(i.get("settle_s", 3.0)))

    @property
    def outro_s(self) -> float:
        """礼と就寝の長さ（`tail_s` の後ろに付く）。

        礼で下げる -> 保つ -> いったん起きる -> 顔をしまう -> 静止、の順。
        `build.py` の配置と同じ足し算にしておくこと（ずれると最後が切れる）。
        """
        o = self.outro
        if not o:
            return 0.0
        return (float(o.get("ready_s", 1.5)) + float(o.get("bow_s", 3.5))
                + float(o.get("hold_s", 1.2)) + float(o.get("rise_s", 1.8))
                + float(o.get("stow_s", 3.0)) + float(o.get("rest_s", 1.5)))

    @property
    def t0_s(self) -> float:
        """プログラム先頭から見た、音楽の1拍目の時刻。"""
        return self.intro_s + self.lead_in_s

    @property
    def tail_total_s(self) -> float:
        return self.tail_s + self.outro_s

    # -- cast ------------------------------------------------------------
    def robot_for(self, part_name: str) -> str | None:
        return self.cast.get(part_name)

    def part_for_robot(self, robot_id: str) -> Part | None:
        for part_name, rid in self.cast.items():
            if rid == robot_id:
                return self.parts.get(part_name)
        return None

    def unassigned_parts(self) -> list[str]:
        return [n for n in self.parts if n not in self.cast]

    def check_cast(self, known_robot_ids: set[str]) -> list[str]:
        """Problems with the cast, as human sentences. Empty means fine."""
        problems: list[str] = []
        for part_name, rid in self.cast.items():
            if part_name not in self.parts:
                problems.append(
                    f"cast assigns robot {rid!r} to part {part_name!r}, which is "
                    "not defined under `parts`"
                )
            if rid not in known_robot_ids:
                problems.append(
                    f"cast assigns part {part_name!r} to robot {rid!r}, which is "
                    "not in robots.yaml"
                )
        seen: dict[str, str] = {}
        for part_name, rid in self.cast.items():
            if rid in seen:
                problems.append(
                    f"robot {rid!r} is cast in two parts ({seen[rid]!r} and "
                    f"{part_name!r}) -- one robot can only play one part"
                )
            seen[rid] = part_name
        for n in self.unassigned_parts():
            problems.append(f"part {n!r} has no robot assigned -- it will be silent")
        return problems

    # -- events ----------------------------------------------------------
    def events_for_part(self, part: Part) -> list[NoteEvent]:
        """Resolve a part into concrete note events on the song timeline."""
        if self.timeline is None:
            raise ScoreError("score has no timeline")
        tl = self.timeline

        if part.kind == KIND_MIDI:
            pitches = set(part.pitches) if part.pitches else None
            evs = tl.events_for(
                track=part.track or None, channel=part.channel, pitches=pitches
            )
            if not evs:
                # Silent is a legitimate outcome, but almost always a typo, so
                # say what was actually available.
                raise ScoreError(
                    f"part {part.name!r} matched no notes "
                    f"(track={part.track!r}, pitches={part.pitches}, "
                    f"channel={part.channel}). Tracks in this MIDI: {tl.tracks()}"
                )
            return evs

        if part.kind == KIND_PATTERN:
            if not part.pattern:
                raise ScoreError(f"part {part.name!r} is kind=pattern but has no pattern")
            base = parse_pattern(
                part.pattern, step_beats=part.step_beats, track=part.name
            )
            span = pattern_length_beats(part.pattern, part.step_beats)
            if span <= 0:
                raise ScoreError(f"part {part.name!r} pattern is empty")

            bpb = tl.meter.beats_per_bar
            start = part.start_bar * bpb
            end = (part.end_bar * bpb) if part.end_bar is not None else tl.total_beats

            if not part.repeat:
                return [
                    NoteEvent(
                        beat=e.beat + start, pitch=e.pitch, velocity=e.velocity,
                        duration_beats=e.duration_beats, track=part.name,
                    )
                    for e in base
                    if start + e.beat < end
                ]

            out: list[NoteEvent] = []
            b = start
            while b < end - 1e-9:
                for e in base:
                    t = b + e.beat
                    if t < end - 1e-9:
                        out.append(
                            NoteEvent(
                                beat=t, pitch=e.pitch, velocity=e.velocity,
                                duration_beats=e.duration_beats, track=part.name,
                            )
                        )
                b += span
            return sorted(out, key=lambda e: e.beat)

        return []


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_pitches(raw: Any) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    out: list[int] = []
    for p in raw:
        if isinstance(p, int):
            out.append(p)
        else:
            out.append(note_number(str(p)))
    return out


def load_score(path: str | Path) -> Score:
    """Load a `song.yaml` and its MIDI (if any)."""
    path = Path(path)
    if path.is_dir():
        path = path / "song.yaml"
    if not path.exists():
        raise FileNotFoundError(path)

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    src = path.parent
    song = data.get("song") or {}

    name = str(song.get("name") or src.name)
    score = Score(
        name=name,
        title=str(song.get("title", name)),
        source_dir=src,
        lead_in_s=float(song.get("lead_in_s", 2.5)),
        tail_s=float(song.get("tail_s", 2.0)),
        bars_per_phrase=int(song.get("bars_per_phrase", 4)),
        secondary=data.get("secondary") or {},
        intro=data.get("intro") or {},
        outro=data.get("outro") or {},
        finale=data.get("finale") or {},
        raw=data,
    )

    # -- timeline ---------------------------------------------------------
    meter_raw = song.get("meter", [4, 4])
    meter = Meter(int(meter_raw[0]), int(meter_raw[1]))

    midi_name = song.get("midi")
    if midi_name:
        midi_path = src / str(midi_name)
        if not midi_path.exists():
            raise ScoreError(f"{path}: midi file not found: {midi_path}")
        tl = load_midi(midi_path, default_bpm=float(song.get("bpm", 120.0)))
        if song.get("bpm") and tl.tempo.is_constant:
            # An explicit bpm in song.yaml overrides the file's, which is how
            # you rehearse a piece slowly without re-exporting the MIDI.
            tl.tempo = TempoMap.constant(float(song["bpm"]))
        if "meter" in song:
            tl.meter = meter
    else:
        bars = int(song.get("bars", 8))
        tl = Timeline(
            tempo=TempoMap.constant(float(song.get("bpm", 120.0))),
            meter=meter,
            events=[],
            total_beats=bars * meter.beats_per_bar,
            name=name,
        )

    if song.get("bars"):
        tl.total_beats = float(song["bars"]) * tl.meter.beats_per_bar

    # -- 終止形 --------------------------------------------------------------
    # 採譜がそのまま終わると「途中で切れた」ように聞こえることがある。この曲が
    # まさにそれで、盛り上がりのあと低い8分音符が2つ鳴って終わっていた。
    #
    # 終わりの和音を**タイムラインそのものに足す**。ステム生成側だけに足すと
    # 音は鳴るのに動かない台が出るし、モーション側だけに足すと逆になる。
    # `events_for_part` はトラック名で拾うので、ここへ入れておけば音と動きの
    # 両方が自動で付いてくる ―― 1つの真実から両方を出す。
    if score.finale:
        fin = score.finale
        bpb = tl.meter.beats_per_bar
        at = float(fin.get("at_bar", tl.n_bars)) * bpb
        hold = float(fin.get("hold_beats", 4.0))
        vel = int(fin.get("velocity", 100))

        # ★フェードアウトを戻す★ この採譜は「終わる」のではなく**消えていく**。
        # 実測した velocity: 94 -> 86 -> 72 -> 56 -> 11 -> 8。遊園地の無限ループ
        # そのままで、終止形が最初から入っていない。そのまま鳴らすと
        # 「最後まで演奏していない」ように聞こえる（実際そのとおり）。
        # ここから先の velocity を、フェードが始まる前の水準へ戻す。
        unfade = fin.get("unfade_from_bar")
        if unfade is not None:
            b0 = float(unfade) * bpb
            import statistics
            from dataclasses import replace
            # トラックごとに「フェード前の代表値」へ引き上げる。NoteEvent は
            # frozen なので、書き換えではなく作り直す。
            target = {}
            for track in {e.track for e in tl.events}:
                ref = [e.velocity for e in tl.events
                       if e.track == track and e.beat < b0]
                if ref:
                    target[track] = int(min(127, statistics.median(ref)))
            tl.events = [
                (replace(e, velocity=target[e.track])
                 if (e.beat >= b0 and e.track in target
                     and e.velocity < target[e.track]) else e)
                for e in tl.events
            ]

        # ★フェードの残りを捨てる★ 終止のあとに次の繰り返しの頭が中途半端に
        # 残っている（vel 34 の2音）。切らないと終止和音の後ろで鳴る。
        trim = fin.get("trim_after_bar")
        if trim is not None:
            cut = float(trim) * bpb + 1e-6
            tl.events = [e for e in tl.events if e.beat <= cut]

        # ★同じ拍に既存の音があるなら、足さずに**差し替える**★
        # 足すとクリップの配置が2つ重なり、寄与が加算されて振幅が倍になる
        # （実測: マリンバの関節速度 5.54 rad/s、上限 4.0。`gesture_overlap`
        # も同時に出た）。終止和音はその音を「終わりの音にする」ものであって、
        # 別の音を重ねるものではない。
        for track, pitches in (fin.get("notes") or {}).items():
            track = str(track)
            for p in (pitches if isinstance(pitches, list) else [pitches]):
                hit = next((i for i, e in enumerate(tl.events)
                            if e.track == track and abs(e.beat - at) < 0.26), None)
                note = NoteEvent(beat=at, pitch=int(p), velocity=vel,
                                 duration_beats=hold, track=track)
                if hit is None:
                    tl.events.append(note)
                else:
                    tl.events[hit] = note
        tl.events.sort(key=lambda e: e.beat)
        tl.total_beats = max(
            (e.beat + e.duration_beats for e in tl.events), default=tl.total_beats)
        tl.total_beats = max(tl.total_beats, at + hold)

    tl.build_phrases(score.bars_per_phrase)
    score.timeline = tl

    # -- parts ------------------------------------------------------------
    for pname, praw in (data.get("parts") or {}).items():
        praw = praw or {}
        kind = str(praw.get("kind", KIND_PATTERN if praw.get("pattern") else KIND_MIDI))
        if kind not in KINDS:
            raise ScoreError(
                f"{path}: part {pname!r} has kind {kind!r}; expected one of {list(KINDS)}"
            )
        score.parts[str(pname)] = Part(
            name=str(pname),
            kind=kind,
            clip=str(praw.get("clip", "")),
            accent_clip=str(praw.get("accent_clip", "")),
            instrument=str(praw.get("instrument", "")),
            audio=str(praw.get("audio", "")),
            gain=float(praw.get("gain", 1.0)),
            mix_db=float(praw.get("mix_db", 0.0)),
            track=str(praw.get("track", "")),
            pitches=_parse_pitches(praw.get("pitches") or praw.get("pitch")),
            channel=praw.get("channel"),
            voice_preset=str(praw.get("voice_preset", "")),
            persona=str(praw.get("persona", "")),
            transpose=int(praw.get("transpose", 0)),
            pattern=str(praw.get("pattern", "")),
            step_beats=float(praw.get("step", praw.get("step_beats", 0.25))),
            zone_cycle=[int(v) for v in (praw.get("zone_cycle") or [])],
            repeat=bool(praw.get("repeat", True)),
            start_bar=int(praw.get("start_bar", 0)),
            end_bar=(None if praw.get("end_bar") is None
                     else float(praw["end_bar"])),
        )

    for pname, rid in (data.get("cast") or {}).items():
        score.cast[str(pname)] = str(rid)

    return score


def save_score_template(path: str | Path, robot_ids: list[str]) -> Path:
    """Write a commented starter `song.yaml`. Used by `orchestra song new`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cast_lines = "\n".join(
        f"  part_{i + 1}: {rid}" for i, rid in enumerate(robot_ids[1:], start=0)
    )
    path.write_text(
        f"""song:
  name: {path.parent.name}
  title: "Untitled"
  bpm: 112
  meter: [4, 4]
  bars: 16
  lead_in_s: 2.5      # T0 is at lead_in_s; the conductor's prep beat lives here
  tail_s: 2.0
  bars_per_phrase: 4
  # midi: song.mid    # optional -- omit to drive everything from patterns

parts:
  conductor:
    kind: conductor

  part_1:
    kind: pattern
    pattern: "X...x...X...x..."   # X accent, x hit, . rest, | bar line
    step: 0.25
    clip: shaker/groove
    instrument: shaker

cast:
  conductor: {robot_ids[0] if robot_ids else "r01"}
{cast_lines}

secondary:
  breathing: true
  phrase_sway: true
  attention: true
""",
        encoding="utf-8",
    )
    return path
