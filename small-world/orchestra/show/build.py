"""Score + clips + cast -> one validated Program per robot.

This is where the musical timeline and the motion library meet. The important
step -- and the one that makes strikes land musically rather than late -- is
**anchor alignment**: a clip is placed so that its `anchor_beat` coincides with
the note, not so that its start does. The windup therefore happens *before* the
beat, exactly as a player's does.

Nothing leaves this module unvalidated. `build_show` runs the full safety gate on
every program at the song's real tempo and refuses to emit anything that fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import radians
from pathlib import Path

import numpy as np

from ..conductor.patterns import ConductorStyle, build_conductor_program
from ..config import Calibration, Context, RobotConfig
from ..motion import layers, persona
from ..motion.compile import Placement, compile_placements
from ..motion.program import AudioSpec, Program
from ..motion.schema import PATTERN, TRIGGER, Clip, ClipLibrary
from ..motion.validate import ValidationReport, validate_clip, validate_program
from ..music.score import (
    KIND_CONDUCTOR, KIND_IDLE, KIND_MIDI, KIND_VOICE, Part, Score, ScoreError,
)


def _prologue_epilogue(score: Score, part: Part | None, times: np.ndarray,
                       who: persona.Persona, is_conductor: bool,
                       music_end_s: float) -> np.ndarray:
    """紹介と、礼＋就寝。音楽の外側なので**秒**で組む。

    紹介の振り付けは書かない ―― `persona.showcase` が**本番の構えを誇張した
    ものを生成する**ので、紹介と演奏中の姿が食い違いようがない。最後は本番の
    構えちょうどに収まる。
    """
    out = np.zeros((len(times), persona.N_CHANNELS))
    intro, outro = score.intro, score.outro

    if intro:
        lead = float(intro.get("lead_s", 1.0))
        greet = float(intro.get("greet_s", 3.5))
        slot = float(intro.get("slot_s", 2.0))
        gap = float(intro.get("gap_s", 0.3))
        cue_s = float(intro.get("cue_s", 0.7))
        order = list(intro.get("order") or [])
        first = lead + greet
        if is_conductor:
            out += persona.greeting(times, lead, greet, scan_to=0.0,
                                    turn_deg=float(intro.get("greet_turn_deg", 0.0)),
                                    turn_s=float(intro.get("greet_turn_s", 1.6)))
            # 台ごとに合図を出す。「指示 -> 応答」が見えるようにする。
            for i in range(len(order)):
                toward = (2.0 * i / max(len(order) - 1, 1)) - 1.0
                out += persona.cue(times, first + i * (slot + gap), cue_s, toward)
            # 紹介の締め ―― 全員に向かって大きく一度。これを受けて9台が
            # 一斉に顔を上げる（`wait_blend` の解除をこの打点に合わせてある）。
            settle_at = score.intro_s - float(intro.get("settle_s", 3.0))
            # scale は 1.7 まで上げたら指揮者の関節上限を割った（stewart_1 が
            # 1.551、範囲 1.285）。-90° に構えているぶんヤコビアンが効く。
            out += persona.cue(times, settle_at, float(intro.get("ready_cue_s", 1.2)),
                               toward=0.0, scale=1.25)
        elif part is not None and part.name in order:
            i = order.index(part.name)
            # ★合図を受けてから鳴る★ 枠の頭は指揮者のもの。
            at = first + i * (slot + gap) + cue_s
            out += persona.showcase(who, times, at, max(slot - cue_s, 0.4))

    return out


def _intro_slot_s(score: Score, part: Part | None) -> float | None:
    """このパートが名乗る時刻（プログラム先頭からの秒）。無ければ None。"""
    intro = score.intro
    order = list(intro.get("order") or []) if intro else []
    if not order or part is None or part.name not in order:
        return None
    slot = float(intro.get("slot_s", 2.0))
    gap = float(intro.get("gap_s", 0.3))
    return (float(intro.get("lead_s", 1.0)) + float(intro.get("greet_s", 3.5))
            + order.index(part.name) * (slot + gap))


def _stance_ramp_beat(score: Score, part: Part | None, tl) -> float | None:
    """構えを立ち上げ始める拍。名乗りがあるならその枠の頭、無ければ既定。"""
    if not score.intro:
        return None
    slot = _intro_slot_s(score, part)
    span = float(score.intro.get("slot_s", 2.0))
    # 名乗りが無い役（指揮者）は、全員が構え終える settle の頭で。
    at_s = (slot + span * 0.25) if slot is not None else (
        score.intro_s - float(score.intro.get("settle_s", 3.0)))
    return float(tl.tempo.seconds_to_beats(at_s - score.t0_s))


def _persona_for(part: Part | None, robot: RobotConfig) -> persona.Persona:
    """このロボットの「持ち姿」を決める。

    明示の `persona:` が最優先。無ければ音源プリセット、楽器名、最後に
    レジストリの楽器の順で引く。**指揮者は part.kind から引く** ―― 楽器名を
    持たないので。知らない名前なら構えなし（落とさない）。
    """
    if part is None:
        return persona.DEFAULT
    for name in (part.persona, part.voice_preset, part.instrument,
                 part.kind, robot.instrument):
        p = persona.get(name)
        if p is not persona.DEFAULT:
            return p
    return persona.DEFAULT


@dataclass
class BuiltPart:
    robot: RobotConfig
    part: Part | None
    program: Program
    report: ValidationReport | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report is None or self.report.ok


@dataclass
class ShowBuild:
    score: Score
    parts: list[BuiltPart] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and all(p.ok for p in self.parts)

    def summary(self) -> str:
        lines = [
            f"{self.score.title} ({self.score.name})",
            f"  {len(self.parts)} robot(s), "
            f"{self.parts[0].program.duration:.1f}s" if self.parts else "  (no parts)",
        ]
        for bp in self.parts:
            status = "ok  " if bp.ok else "FAIL"
            what = bp.part.name if bp.part else "idle"
            n_err = len(bp.report.errors) if bp.report else 0
            extra = f"  {n_err} error(s)" if n_err else ""
            lines.append(
                f"    [{status}] {bp.robot.id:<5} {what:<14} "
                f"{bp.program.n_frames:>6} frames  "
                f"{len(bp.program.anchors):>4} strikes{extra}"
            )
        for p in self.problems:
            lines.append(f"  PROBLEM: {p}")
        return "\n".join(lines)


def _placements_for_part(
    part: Part, score: Score, lib: ClipLibrary
) -> tuple[list[Placement], list[str]]:
    """Turn one part's events into scheduled clip placements."""
    warnings: list[str] = []
    if not part.clip:
        raise ScoreError(f"part {part.name!r} has no `clip`")

    clip = lib.get(part.clip)
    accent = lib.get(part.accent_clip) if part.accent_clip else None
    tl = score.timeline
    assert tl is not None

    placements: list[Placement] = []

    if clip.placement == PATTERN and part.kind not in (KIND_MIDI, KIND_VOICE):
        # A looping groove is driven by the clip's own internal rhythm, so the
        # part needs no note list at all -- and asking for one would force
        # authors to write a `pattern` string that nothing reads.
        # A groove clip loops continuously over the part's span rather than
        # being retriggered; its own anchors mark where it makes sound.
        bpb = tl.meter.beats_per_bar
        start = part.start_bar * bpb
        end = (part.end_bar * bpb) if part.end_bar is not None else tl.total_beats
        placements.append(
            Placement(
                clip=clip,
                start_beat=start,
                end_beat=end,
                gain=part.gain,
                layer=10,
                suppress_below=bool(clip.anchors),
                label=f"{part.name}:{clip.name}",
            )
        )
        return placements, warnings

    # Trigger placement: one instance per note, aligned on the anchor.
    events = score.events_for_part(part)
    for ev in events:
        chosen = accent if (accent is not None and ev.velocity >= 100) else clip
        # THE alignment step. The clip's anchor lands on the note; everything
        # before the anchor (the windup) therefore happens before the beat.
        start_beat = ev.beat - chosen.primary_anchor
        if start_beat < 0:
            # Negative is fine and expected -- the windup extends backwards into
            # the lead-in, which is what the lead-in is for. Only complain if
            # there is not enough of it.
            spb = 60.0 / tl.tempo.initial_bpm
            needed = -start_beat * spb
            if needed > score.lead_in_s:
                warnings.append(
                    f"{part.name}: the note at beat {ev.beat:g} needs "
                    f"{needed:.2f}s of windup but lead_in_s is only "
                    f"{score.lead_in_s:g}s. Raise lead_in_s in song.yaml, or use "
                    "a clip with an earlier anchor."
                )
        placements.append(
            Placement(
                clip=chosen,
                start_beat=start_beat,
                gain=part.gain * (0.75 + 0.35 * ev.dynamic),
                layer=10,
                suppress_below=bool(chosen.anchors),
                label=f"{part.name}@{ev.beat:g}",
            )
        )
    return placements, warnings


def _secondary_for(
    program: Program,
    score: Score,
    robot: RobotConfig,
    conductor_azimuth: float = 0.0,
) -> list[np.ndarray]:
    """Assemble this robot's expressive layers.

    Every layer returned here is subsequently muted around strike anchors by
    :func:`layers.apply_secondary`, so none of it can perturb an instrument hit.
    """
    cfg = score.secondary or {}
    if not cfg:
        return []

    tl = score.timeline
    assert tl is not None
    t = program.times()
    ctx = layers.SecondaryContext(
        times=t,
        anchors=program.anchors,
        phrase_env=tl.phrase_envelope(t, lead_in_s=score.lead_in_s),
        # Per-robot phase from the hardware id: nine robots breathing in
        # lockstep reads as a glitch, not an ensemble.
        seed=layers.phase_seed(robot.hardware_id or robot.id),
        conductor_azimuth_rad=conductor_azimuth,
    )

    out: list[np.ndarray] = []

    # 常時ノリ層。全機・常時。自分の出番が無い間もリズムに乗り続けることで
    # 9台が「合奏している集団」に見える。アンカー付近では既存の消音機構が
    # 自動で弱めるので、楽器のジェスチャを邪魔しない。
    if cfg.get("groove", True):
        beats = tl.tempo.seconds_to_beats(t - score.t0_s)
        out.append(layers.beat_groove(
            ctx, beats, beats_per_bar=tl.meter.beats_per_bar))

    if cfg.get("breathing", True):
        gate = layers.idleness(ctx)
        out.append(layers.breathing(ctx, gate=gate))
    if cfg.get("phrase_sway", True):
        out.append(layers.phrase_sway(ctx))
    if cfg.get("attention", True) and not robot.is_conductor:
        out.append(
            layers.attention(ctx, events_s=tl.phrase_starts_s(score.t0_s))
        )
    return out


def build_show(
    ctx: Context,
    score: Score,
    lib: ClipLibrary | None = None,
    fps: float | None = None,
    validate: bool = True,
    include_idle: bool = True,
) -> ShowBuild:
    """Compile the whole performance.

    Every robot in the cast gets a program. Robots not cast get an idle program
    when `include_idle`, so a spare unit still breathes rather than sitting dead
    on stage.
    """
    lib = lib or ClipLibrary(ctx.clips_dir)
    fps = fps or ctx.fleet.fps
    tl = score.timeline
    if tl is None:
        raise ScoreError("score has no timeline")

    build = ShowBuild(score=score)
    build.problems.extend(score.check_cast({r.id for r in ctx.robots}))
    if lib.errors:
        for key, err in lib.errors.items():
            build.problems.append(f"clip {key} failed to load: {err}")

    # 紹介は `lead_in_s` を伸ばして代用しない。あれは「振りかぶりが収まる
    # 余裕」という別の意味を持っていて、下の警告(:126-132)がそれと比較して
    # いる。30秒に伸ばすと警告が永久に鳴らなくなる。T0 だけを後ろへずらす。
    duration_s = tl.duration_s + score.tail_total_s
    neutral = ctx.fleet.neutral_antennas

    assigned: dict[str, Part] = {}
    for part_name, rid in score.cast.items():
        part = score.parts.get(part_name)
        if part is not None:
            assigned[rid] = part

    targets = [r for r in ctx.robots.enabled if r.id in assigned]
    if include_idle:
        targets += [r for r in ctx.robots.enabled if r.id not in assigned]

    for robot in targets:
        part = assigned.get(robot.id)
        warnings: list[str] = []

        audio = None
        if part and part.audio and score.source_dir:
            audio_path = score.source_dir / "audio" / part.audio
            # The measured GStreamer->ALSA latency for THIS robot, so its sound
            # reaches the speaker as its actuator moves.
            cal = Calibration.load(robot.id, ctx.root / "config" / "calibration")
            audio = AudioSpec(file=part.audio, lead_ms=cal.audio_lead_ms)
            if not audio_path.exists():
                warnings.append(
                    f"audio stem {audio_path.name} not found in {audio_path.parent} "
                    "-- this robot will play motion without sound"
                )

        is_conductor = bool(part and part.kind == KIND_CONDUCTOR)
        # ★指揮者だけ台の上で 90° 回して置く★ 機構の可動域は ±150° なので、
        # 正面を機構ゼロに置くと礼のときに 180° 回れない。演奏中の向きを
        # -90° に、礼の向きを +90° に取れば差はちょうど 180° になり、
        # どちらも可動域の内側。**設置の向きと対になる設定**なので、
        # ここを変えるときは実機の置き方も変えること。
        home_yaw = (radians(float(score.outro.get("conductor_home_yaw_deg", 0.0)))
                    if (is_conductor and score.outro) else 0.0)

        if part is not None and part.kind == KIND_CONDUCTOR:
            program = build_conductor_program(
                tl,
                fps=fps,
                lead_in_s=score.t0_s,
                neutral_antennas=neutral,
                style=ConductorStyle(),
                robot_id=robot.id,
                song=score.name,
                # ★tail_total_s★ 指揮者は自分でプログラム長を計算するので、
                # ここを tail_s のままにすると指揮者だけ短くなる。9台の長さが
                # 揃わないと共有 T0 が意味を失う（実際 12991 対 14091 になった）。
                tail_s=score.tail_total_s,
            )
            program.audio = audio

        elif part is None or part.kind == KIND_IDLE:
            res = compile_placements(
                [], tempo=tl.tempo, duration_s=duration_s, fps=fps,
                neutral_antennas=neutral, robot_id=robot.id, song=score.name,
                part="idle", lead_in_s=score.t0_s,
            )
            program = res.program
            # 動かない役でも、宣言された音は鳴らす。ここを落とすと
            # 「song.yaml に audio: と書いたのに無音、警告も無し」になる。
            # 校正用の曲（チャープを鳴らすだけで動かない）がまさにこの形。
            program.audio = audio

        else:
            placements, warn = _placements_for_part(part, score, lib)
            warnings.extend(warn)
            res = compile_placements(
                placements, tempo=tl.tempo, duration_s=duration_s, fps=fps,
                neutral_antennas=neutral, robot_id=robot.id, song=score.name,
                part=part.name, instrument=part.instrument or robot.instrument,
                audio=audio, lead_in_s=score.t0_s,
            )
            program = res.program
            warnings.extend(res.warnings)

        # Expression goes on last, and is muted around every strike.
        deltas = _secondary_for(program, score, robot)
        # 礼と就寝の間は副次モーションも止める。呼吸(z-5mm)とグルーヴ(z-2.2mm)が
        # 就寝姿勢(z-44mm)に乗って -51.0mm になり、可動域を割った。寝ている
        # ロボットは呼吸で頭を上下させない。
        if deltas and score.outro:
            _t = program.times()
            _end = score.t0_s + tl.duration_s
            _from = _end - 2.0
            if score.finale:
                _fb = (float(score.finale.get("at_bar", tl.n_bars))
                       * tl.meter.beats_per_bar)
                _from = score.t0_s + float(tl.tempo.beats_to_seconds(_fb))
            g = persona.release_gate(_t, _from, max(_end - _from, 0.5))
            deltas = [d * g[:, None] for d in deltas]
        if deltas:
            sm = ctx.fleet.secondary_motion
            program = layers.apply_secondary(
                program,
                deltas,
                mute_window_s=float(sm.get("anchor_mute_window_s", 0.08)),
                ramp_s=float(sm.get("anchor_mute_ramp_s", 0.12)),
            )

        # 持ち姿とフレーズの弧。**ダッキングの後に、通さずに足す。**
        # `apply_secondary` に入れると打点ごとに姿勢が抜けるうえ、大きな一定量を
        # 80ms でゼロにすると、そこで関節速度の上限に当たる。
        # 誰が何を演奏しているかを見せているのはここ（persona.py の冒頭を参照）。
        if home_yaw:
            # 設置の向きぶんを全フレームに載せる。頭も一緒に回すので
            # rel_yaw は変わらない（頭の姿勢は world 座標）。
            program.frames[:, persona.CH_BODY_YAW] += home_yaw
            program.frames[:, persona.CH_YAW] += home_yaw

        who = _persona_for(part, robot)
        t = program.times()
        music_end_s = score.t0_s + tl.duration_s
        # ★音と動きを一緒に収める★ 終止和音が鳴った瞬間から構えを解きはじめ、
        # 和音の余韻が消えるのと同時に動きも止まる。以前は礼の直前まで
        # 全振幅で振り続けていたので、音が消えたあとも一人で踊っている台が
        # いるように見えた。
        gate = None
        if score.outro:
            fade_from = music_end_s - 2.0
            if score.finale:
                fin_beat = (float(score.finale.get("at_bar", tl.n_bars))
                            * tl.meter.beats_per_bar)
                fade_from = score.t0_s + float(tl.tempo.beats_to_seconds(fin_beat))
            gate = persona.release_gate(t, fade_from,
                                        max(music_end_s - fade_from, 0.5))
        if who is not persona.DEFAULT:
            beats = tl.tempo.seconds_to_beats(t - score.t0_s)
            program = persona.apply_posture(
                program, who, beats, tl.meter.beats_per_bar,
                phase=layers.phase_seed(robot.hardware_id or robot.id),
                # 紹介がある曲では、**自分の名乗りの枠で**構えを立ち上げる。
                # 全機いっせいに構えるより「紹介された台から順に構えていく」
                # ほうが読める。名乗りの誇張ぶんはこの上に乗る。
                ramp_from_beat=_stance_ramp_beat(score, part, tl),
                gate=gate,
            )

        # 紹介の間、自分の番が来るまでは全員同じ姿勢で止まる。動いているのが
        # 指揮者と紹介中の1台だけになるので、目がそこへ行く。
        if score.intro and not is_conductor:
            slot = _intro_slot_s(score, part)
            if slot is not None:
                settle = float(score.intro.get("settle_s", 3.0))
                # ★指揮者の打点に合わせて一斉に顔を上げる★ 解除の時刻を
                # 合図の「打つ」瞬間と**同じ式から**出す。別々に書くと
                # 合図より先に顔が上がる。
                settle_at = score.intro_s - settle
                lift_at = persona.cue_ictus(
                    settle_at, float(score.intro.get("ready_cue_s", 1.2)))
                tgt, w_wait = persona.wait_blend(
                    t, float(score.intro.get("lead_s", 1.0)),
                    t_until=lift_at,
                    slot_at=slot,
                    slot_span=float(score.intro.get("slot_s", 2.6)),
                    ramp=0.45)
                program.frames = (program.frames * (1.0 - w_wait)[:, None]
                                  + tgt * w_wait[:, None])

        # 紹介は差分で足す（構えの上に「大きく見せる」を重ねるので）。
        if score.intro:
            program.frames = program.frames + _prologue_epilogue(
                score, part, t, who,
                is_conductor=bool(part and part.kind == KIND_CONDUCTOR),
                music_end_s=music_end_s,
            )

        # 礼と就寝は**絶対姿勢へ寄せる**。差分で足すと、その台が保っている
        # 姿勢に積み上がって可動域を割る（persona.bow_and_stow を参照）。
        if score.outro:
            o = score.outro
            target, w = persona.bow_and_stow(
                t, music_end_s + score.tail_s,
                ready_s=float(o.get("ready_s", 1.5)),
                bow_s=float(o.get("bow_s", 3.5)), hold_s=float(o.get("hold_s", 1.2)),
                rise_s=float(o.get("rise_s", 1.8)), stow_s=float(o.get("stow_s", 3.0)),
                # 指揮者だけ客席のほうへ向き直って礼をする
                turn_deg=(float(o.get("conductor_turn_deg", 0.0))
                          if is_conductor else 0.0),
                home_yaw_deg=(float(o.get("conductor_home_yaw_deg", 0.0))
                              if is_conductor else 0.0))
            # w はチャンネルごと（指揮者の向き直りだけ別の重みで動くため）
            program.frames = program.frames * (1.0 - w) + target * w

        report = None
        if validate:
            report = validate_program(
                program,
                ctx.fleet.safety,
                bpm=tl.tempo.initial_bpm,
                label=f"{robot.id}/{part.name if part else 'idle'}",
            )
            if part and part.clip:
                try:
                    clip_rep = validate_clip(
                        lib.get(part.clip), ctx.fleet.safety, tl.tempo.initial_bpm
                    )
                    report.violations.extend(clip_rep.violations)
                    report.ok = report.ok and clip_rep.ok
                except Exception as e:
                    warnings.append(f"clip validation skipped: {e}")
            program.validation = report.to_dict()

        build.parts.append(
            BuiltPart(robot=robot, part=part, program=program, report=report,
                      warnings=warnings)
        )

    return build


def write_build(build: ShowBuild, out_dir: str | Path) -> dict[str, Path]:
    """Write every program to disk, ready for deployment.

    Refuses to write a failing build: an unvalidated program on a robot is
    exactly the silent-freeze / stalled-servo situation the gate exists to
    prevent.
    """
    if not build.ok:
        failing = [bp.robot.id for bp in build.parts if not bp.ok]
        raise RuntimeError(
            "refusing to write a build that failed validation "
            f"(robots: {failing or 'n/a'}; problems: {build.problems}). "
            "Run `orchestra song build --report` to see why."
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for bp in build.parts:
        p = out_dir / f"{bp.robot.id}.program.json.gz"
        bp.program.save(p)
        written[bp.robot.id] = p
    return written
