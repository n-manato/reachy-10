#!/usr/bin/env python3
"""FALLBACK ONLY -- generates a placeholder melody from memory.

    python tools/make_small_world.py --force   # 本物の譜面を上書きする

**This is no longer what the show plays.** On 2026-08-07 the real transcription
was imported (32 bars, verse + chorus, with the counterpoint), so
`songs/small_world/small_world.mid` now holds an actual score and this script
would destroy it. It refuses to write without `--force`.

Kept because it is still the fastest way to get *some* timeline when you have no
MIDI at all -- a new song, a rehearsal without the score to hand, or a test that
needs a known-simple note list.

WHY IT IS ONLY A FALLBACK

The notes below were written from memory and are **wrong**. The real chorus
climbs a whole step on each repetition:

    It's a small world after all     C5 C5 | E5 C5 D5
    It's a small world after all     D5 D5 | F5 D5 E5      <- up a step
    It's a small world after all     E5 E5 | G5 E5 F5      <- up again
    It's a small, small world        F5 E5 D5 G4 | B4 C5

PHRASE_A/PHRASE_B below keep it level instead, which is simply a different tune.
That is what writing music from memory buys you. See `tools/import_midi.py` for
bringing in a real score.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestra.music.midi import note_number, write_midi  # noqa: E402
from orchestra.music.tempo import Meter, TempoMap  # noqa: E402
from orchestra.music.timeline import NoteEvent, Timeline  # noqa: E402

BPM = 112.0
METER = Meter(4, 4)

# (beat_within_phrase, note_name, duration_beats, velocity)
# Phrase A: "It's a small world af-ter all"
PHRASE_A = [
    (0.0, "G4", 0.5, 100),
    (0.5, "G4", 0.5, 84),
    (1.0, "G4", 0.5, 92),
    (1.5, "A4", 0.5, 88),
    (2.0, "G4", 0.5, 96),
    (2.5, "F4", 0.5, 84),
    (3.0, "E4", 1.0, 104),
]

# Phrase B: "It's a small, small world" -- the resolving fourth phrase
PHRASE_B = [
    (0.0, "F4", 0.5, 100),
    (0.5, "F4", 0.5, 84),
    (1.0, "F4", 0.5, 92),
    (1.5, "G4", 0.5, 88),
    (2.0, "F4", 0.5, 96),
    (2.5, "E4", 0.5, 84),
    (3.0, "D4", 1.0, 108),
]

# Four phrases per verse: A A A B. Two verses, plus a 1-bar tag.
STRUCTURE = [PHRASE_A, PHRASE_A, PHRASE_A, PHRASE_B] * 2
PHRASE_BEATS = 4.0


def build() -> Timeline:
    events: list[NoteEvent] = []

    for i, phrase in enumerate(STRUCTURE):
        base = i * PHRASE_BEATS
        for beat, name, dur, vel in phrase:
            events.append(
                NoteEvent(
                    beat=base + beat,
                    pitch=note_number(name),
                    velocity=vel,
                    duration_beats=dur,
                    track="Melody",
                )
            )

    total = len(STRUCTURE) * PHRASE_BEATS

    # A final tonic to land on, so the cutoff gesture has something to cut off.
    events.append(
        NoteEvent(beat=total, pitch=note_number("D4"), velocity=110,
                  duration_beats=2.0, track="Melody")
    )
    total += METER.beats_per_bar

    tl = Timeline(
        tempo=TempoMap.constant(BPM),
        meter=METER,
        events=sorted(events, key=lambda e: e.beat),
        total_beats=total,
        name="small_world",
    )
    tl.build_phrases(bars_per_phrase=1)  # one phrase per bar: A | A | A | B
    return tl


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="overwrite the real transcription with the from-memory placeholder")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tl = build()
    out = (Path(args.out) if args.out else
           Path(__file__).resolve().parent.parent / "songs" / "small_world" / "small_world.mid")
    if out.exists() and not args.force:
        print(
            f"error: {out} already exists.\n"
            "  It holds the real transcription (32 bars, verse + chorus, with "
            "the counterpoint).\n"
            "  This script writes a placeholder melody from memory, and the "
            "pitches are wrong.\n"
            "  Pass --force to replace it anyway, or --out to write elsewhere.",
            file=sys.stderr,
        )
        return 1
    write_midi(tl, out)

    pitches = sorted({e.pitch for e in tl.events})
    from orchestra.music.midi import note_name

    print(f"wrote {out}")
    print(f"  {len(tl.events)} notes, {tl.n_bars} bars, "
          f"{tl.duration_s:.1f}s at {BPM:g} BPM")
    print(f"  distinct pitches ({len(pitches)}): "
          f"{', '.join(note_name(p) for p in pitches)}")
    print(f"  -> the cast needs {len(pitches)} chime-bar robots, one per pitch")

    # Per-pitch strike counts: this is the per-robot workload, and the number
    # that determines duty cycle.
    print("\n  strikes per robot:")
    for p in pitches:
        n = sum(1 for e in tl.events if e.pitch == p)
        rate = n / tl.duration_s
        print(f"    {note_name(p):<4} {n:>3} strikes  ({rate:.2f}/s average)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
