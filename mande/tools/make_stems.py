#!/usr/bin/env python3
"""実サンプルから、ロボット1台ごとの音声ステムを作る。

    python tools/make_stems.py songs/lamban

物理楽器を廃止したので、**ここが出す音がそのまま観客に聞こえる音**になる。
合成音ではなく `gm.dls` から抽出した実録音（Roland 音源）を使う
（`tools/extract_samples.py` が先に必要）。

**台ごとに違うステム**を出すのが重要。9台が同じ音を数ミリ秒ずれて鳴らすと
コムフィルタになって痩せるが、別々のパートなら残ったズレは「9人が
演奏している」ように聞こえ、しかも音の出る場所と動く場所が一致する。

出力は 16kHz stereo ―― ロボットの音声パイプラインが固定でこの形式を要求する。
可聴帯域は 8kHz までなので、高域が命の打楽器（タンバリンは 8kHz 超に
エネルギーの69%）は自動で音程を下げて収めている（`sampler.suggest_pitch_shift`）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from orchestra.audio.render import (  # noqa: E402
    OUTPUT_RATE,
    RenderResult,
    Voice,
    balance,
    make_voice,
    render_stem,
    write_wav_f32,
)
from orchestra.audio.sampler import SampleLibrary  # noqa: E402
from orchestra.config import Context  # noqa: E402
from orchestra.music.midi import note_name  # noqa: E402
from orchestra.motion.compile import Placement  # noqa: E402
from orchestra.motion.schema import PATTERN, ClipLibrary  # noqa: E402
from orchestra.music.score import KIND_CONDUCTOR, KIND_MIDI, load_score  # noqa: E402
from orchestra.music.timeline import NoteEvent  # noqa: E402


def events_from_clip_anchors(part, score, lib: ClipLibrary) -> list[NoteEvent]:
    """ループ型クリップのアンカーから、音を鳴らすタイミングを取り出す。

    シェイカーやタンバリンのように「クリップ内蔵のリズムで鳴り続ける」パートは
    `pattern` 文字列を持たない ―― リズムはクリップの `anchors` にある。

    ここで **モーション側とまったく同じ `Placement.anchor_beats()` を使う**のが
    肝。音のタイミングを別に書き起こすと、いつか必ずズレる。1つの真実から
    音と動きの両方を出す。
    """
    tl = score.timeline
    clip = lib.get(part.clip)
    bpb = tl.meter.beats_per_bar
    start = part.start_bar * bpb
    end = (part.end_bar * bpb) if part.end_bar is not None else tl.total_beats
    pl = Placement(clip=clip, start_beat=start, end_beat=end)
    step = clip.loop_beats / max(1, len(clip.anchors))
    # `zone_cycle` があれば、アンカーごとにゾーン（＝打法）を巡回させる。
    # カリニャンは往と復で音が違うので、全部を同じ音で鳴らすと機械的になる。
    cycle = part.zone_cycle or [60]
    return [
        NoteEvent(beat=b, pitch=cycle[i % len(cycle)],
                  velocity=100 if (b % bpb) < 1e-6 else 82,
                  duration_beats=step, track=part.name)
        for i, b in enumerate(pl.anchor_beats())
    ]


def showcase_events(part, score, base_events: list[NoteEvent]) -> list[NoteEvent]:
    """オープニングの名乗りで鳴らす音。

    **自前で音型を書かない。** そのパートが本編で実際に弾く音の先頭を使う。
    そうすれば「これが私の音」が本番と同じ音色・同じ音域になるし、採譜を
    差し替えたら名乗りも自動で付いてくる。

    モーション側（`persona.showcase`）とは**同じ時刻の式**を使う。ここを
    別々に書き起こすと、いつか必ずズレる ―― `events_from_clip_anchors` の
    注記と同じ理由。
    """
    intro = score.intro
    order = list(intro.get("order") or [])
    if not intro or part.name not in order:
        return []

    tl = score.timeline
    slot = float(intro.get("slot_s", 2.0))
    gap = float(intro.get("gap_s", 0.3))
    # ★合図のぶん遅らせる★ 枠の頭は指揮者が「出て」を出す時間。モーション側
    # （`build._prologue_epilogue`）と同じ式にしておくこと ―― ここを別々に
    # 書くと、指揮者の合図と音がずれる。
    cue_s = float(intro.get("cue_s", 0.7))
    t_slot = (float(intro.get("lead_s", 1.0)) + float(intro.get("greet_s", 3.5))
              + order.index(part.name) * (slot + gap) + cue_s)
    slot = max(slot - cue_s, 0.4)

    # 本編の先頭から拾う。無ければ（ループ型の打楽器など）中央 C を叩く。
    src = sorted(base_events, key=lambda e: e.beat)[:8]
    pitches = [e.pitch for e in src] or [60]
    n = max(2, min(6, len(pitches)))
    step = slot * 0.62 / n            # 枠の前半だけで鳴らし、後半は構えに使う

    out: list[NoteEvent] = []
    for k in range(n):
        t = t_slot + slot * 0.22 + k * step
        beat = float(tl.tempo.seconds_to_beats(t - score.t0_s))
        out.append(NoteEvent(beat=beat, pitch=pitches[k % len(pitches)],
                             velocity=104 if k == 0 else 88,
                             duration_beats=max(step, 0.1) * tl.tempo.initial_bpm / 60.0,
                             track=f"{part.name}:intro"))
    return out


def lead_calls(score, lib: SampleLibrary) -> tuple[list[NoteEvent], Voice]:
    """r01（指揮者 = djembefola）が鳴らす音。

    参考元の指揮者は warm_pad の和音ベッドを敷いていたが、マンデの合奏に
    持続和音は無い。ここでは実際のアンサンブルどおり、**ソロ djembe の
    合図(call)とブレイク**を鳴らす。実アンサンブルでもソロ djembe は伴奏
    djembe とは別の奏者・別の役で、曲の切り替えを全部この人が出す。

    音型は自前で書かず、`tools/make_lamban.py` が MIDI の "Lead" トラックに
    書いたものを読む ―― 指揮の合図と鳴る音を2箇所に書くと、いつか必ずずれる。

    カウントインだけはここで足す。負の拍に置くので本編の小節を侵さない。
    """
    tl = score.timeline
    evs = [NoteEvent(beat=e.beat, pitch=e.pitch, velocity=e.velocity,
                     duration_beats=e.duration_beats, track=e.track)
           for e in tl.events_for(track="Lead")]

    # カウントイン4つ。最後が最も強い。ジェンベの tone(62) で軽く入れる。
    for k in range(4):
        evs.append(NoteEvent(beat=-(4 - k), pitch=62,
                             velocity=104 if k == 3 else 64,
                             duration_beats=0.2, track="count"))
    return evs, make_voice(lib, "djembe")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("song", nargs="?", default="songs/lamban")
    ap.add_argument("--samples", default="samples/mande")
    ap.add_argument("--no-balance", action="store_true",
                    help="skip the ensemble-level balancing pass")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    ctx = Context.load(root)
    song_dir = (root / args.song) if not Path(args.song).is_absolute() else Path(args.song)
    score = load_score(song_dir)
    tl = score.timeline

    try:
        lib = SampleLibrary(root / args.samples)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    clips = ClipLibrary(root / "clips")

    print(f"{score.title}  {tl.n_bars} bars / {tl.duration_s:.1f}s / {tl.tempo!r}")
    print(f"samples: {root / args.samples}\n")

    duration = tl.duration_s
    results: dict[str, RenderResult] = {}
    files: dict[str, str] = {}
    infos: dict[str, str] = {}

    for name, part in score.parts.items():
        if not part.audio:
            continue

        if part.kind == KIND_CONDUCTOR:
            evs, voice = lead_calls(score, lib)
        else:
            preset = part.voice_preset or part.instrument
            try:
                voice = make_voice(lib, preset)
            except KeyError as e:
                print(f"  --   {name:<15} {e}")
                continue
            voice.transpose = part.transpose
            if part.kind == KIND_MIDI or part.pattern:
                evs = score.events_for_part(part)
            else:
                # ループ型クリップ: リズムはクリップの anchors にある
                evs = events_from_clip_anchors(part, score, clips)
            # オープニングで名乗る音。本編の音から拾うので、音色も音域も本番と同じ。
            evs = list(evs) + showcase_events(part, score, evs)

        voice.gain *= part.gain
        r = render_stem(evs, voice, tl.tempo, duration_s=duration,
                        lead_in_s=score.t0_s, tail_s=score.tail_total_s)
        results[name] = r
        files[name] = part.audio

        pitches = sorted({e.pitch + part.transpose for e in evs}) if evs else []
        span = (f"{note_name(pitches[0])}-{note_name(pitches[-1])}"
                if pitches else "-")
        infos[name] = f"{len(evs):>3} notes {span:<9}"
        print(f"  ok   {name:<15} {infos[name]} peak {r.peak:.3f}"
              + ("  " + "; ".join(r.warnings) if r.warnings else ""))

    if not results:
        print("no stems could be rendered", file=sys.stderr)
        return 1

    mix_db = {k: score.parts[k].mix_db for k in results if k in score.parts}
    gains = ({k: 1.0 for k in results} if args.no_balance
             else balance(results, mix_db=mix_db))

    out_dir = song_dir / "audio"
    print(f"\n{'part':<15}{'gain':<8}{'peak':<10}{'file'}")
    total = 0
    for name, r in results.items():
        g = gains[name]
        p = write_wav_f32(out_dir / files[name], r.audio, gain=g)
        total += p.stat().st_size
        print(f"{name:<15}{g:<8.2f}{r.peak * g:<10.3f}{p.name}")

    peaks = [results[k].peak * gains[k] for k in results]
    print(f"\n{len(results)} stems / {total/1024/1024:.1f}MB / {OUTPUT_RATE}Hz stereo")
    print(f"peak spread after balancing: {min(peaks):.3f} .. {max(peaks):.3f}"
          "  (the tighter this is, the less one robot jumps out of the nine)")
    print("\nEvery robot plays a different stem. Nine robots playing the same "
          "audio comb-filter each other and the sound goes thin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
