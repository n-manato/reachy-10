"""MIDI ingestion.

MIDI is a good master representation for this project, and worth saying why
rather than just asserting it:

* It carries a **tempo map**, not a single BPM, so a ritardando at the end is
  data rather than a special case.
* It is **track- and channel-structured**, which maps cleanly onto "one part per
  robot".
* Note velocity gives a free **dynamics** channel -- louder note, bigger gesture.
* Every notation and DAW tool exports it, so the arrangement can be edited in
  real music software instead of in JSON.

What it does *not* carry is phrasing, which is why :mod:`timeline` infers or is
told that separately.

Only note-on/note-off, set_tempo and time_signature are consumed. Everything
else (program changes, controllers, pitch bend) is ignored by design: a robot
striking a wood block has no use for a filter sweep.
"""

from __future__ import annotations

from pathlib import Path

from .tempo import Meter, TempoChange, TempoMap
from .timeline import NoteEvent, Timeline


class MidiError(RuntimeError):
    pass


def load_midi(
    path: str | Path,
    default_bpm: float = 120.0,
    merge_channels: bool = False,
) -> Timeline:
    """Read a MIDI file into a :class:`Timeline`.

    Track names come from the ``track_name`` meta message where present, falling
    back to ``track_<n>`` -- those names are what ``song.yaml`` binds parts to,
    so name your tracks in the DAW and the binding reads well.
    """
    try:
        import mido
    except ImportError as e:  # pragma: no cover
        raise MidiError("mido is required to read MIDI files (pip install mido)") from e

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    mid = mido.MidiFile(str(path))
    tpb = mid.ticks_per_beat or 480

    # -- tempo map and meter, gathered across all tracks in absolute ticks --
    tempo_changes: list[TempoChange] = []
    meter = Meter()
    meter_found = False

    for track in mid.tracks:
        t = 0
        for msg in track:
            t += msg.time
            if msg.type == "set_tempo":
                bpm = 60_000_000.0 / msg.tempo
                tempo_changes.append(TempoChange(beat=t / tpb, bpm=bpm))
            elif msg.type == "time_signature" and not meter_found:
                meter = Meter(msg.numerator, msg.denominator)
                meter_found = True

    tempo = (
        TempoMap(sorted(tempo_changes, key=lambda c: c.beat))
        if tempo_changes
        else TempoMap.constant(default_bpm)
    )

    # -- notes -------------------------------------------------------------
    events: list[NoteEvent] = []
    for i, track in enumerate(mid.tracks):
        name = ""
        for msg in track:
            if msg.type == "track_name":
                name = str(msg.name).strip()
                break
        name = name or f"track_{i}"

        t = 0
        # (channel, pitch) -> (start_beat, velocity). Handles overlapping notes
        # of the same pitch by last-on-wins, which is what a sane file implies.
        open_notes: dict[tuple[int, int], tuple[float, int]] = {}

        for msg in track:
            t += msg.time
            beat = t / tpb
            if msg.type == "note_on" and msg.velocity > 0:
                open_notes[(msg.channel, msg.note)] = (beat, msg.velocity)
            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                key = (msg.channel, msg.note)
                if key not in open_notes:
                    continue
                start, vel = open_notes.pop(key)
                events.append(
                    NoteEvent(
                        beat=start,
                        pitch=msg.note,
                        velocity=vel,
                        duration_beats=max(beat - start, 1e-3),
                        track=name,
                        channel=0 if merge_channels else msg.channel,
                    )
                )

        # A note left hanging at end-of-track still happened; give it a nominal
        # length rather than dropping it silently.
        for (ch, pitch), (start, vel) in open_notes.items():
            events.append(
                NoteEvent(
                    beat=start, pitch=pitch, velocity=vel, duration_beats=0.25,
                    track=name, channel=0 if merge_channels else ch,
                )
            )

    events.sort(key=lambda e: (e.beat, e.track, e.pitch))
    last = max((e.beat + e.duration_beats for e in events), default=0.0)
    bpb = meter.beats_per_bar
    total = float(int(last / bpb + 0.999) * bpb) if bpb else last

    tl = Timeline(
        tempo=tempo,
        meter=meter,
        events=events,
        total_beats=max(total, last),
        name=path.stem,
    )
    tl.build_phrases()
    return tl


def describe(tl: Timeline) -> str:
    """A short human summary -- what `orchestra song info` prints."""
    lines = [
        f"{tl.name or '(unnamed)'}: {tl.n_bars} bars, {tl.total_beats:g} beats, "
        f"{tl.duration_s:.1f}s",
        f"  meter {tl.meter}, {tl.tempo!r}",
        f"  {len(tl.events)} note events across {len(tl.tracks())} track(s)",
    ]
    for name in tl.tracks():
        evs = tl.events_for(track=name)
        if not evs:
            continue
        pitches = sorted({e.pitch for e in evs})
        shown = ", ".join(note_name(p) for p in pitches[:10])
        more = f" (+{len(pitches) - 10} more)" if len(pitches) > 10 else ""
        lines.append(f"    {name:<24} {len(evs):>4} notes  pitches: {shown}{more}")
    lines.append(f"  {len(tl.phrases)} phrase(s)")
    return "\n".join(lines)


_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def note_name(pitch: int) -> str:
    """MIDI number -> scientific pitch notation. 60 -> C4."""
    return f"{_NAMES[pitch % 12]}{pitch // 12 - 1}"


def note_number(name: str) -> int:
    """Inverse of :func:`note_name`. Accepts 'C5', 'F#4', 'Bb3'."""
    s = name.strip()
    if not s:
        raise ValueError("empty note name")
    letter = s[0].upper()
    if letter not in "ABCDEFG":
        raise ValueError(f"bad note name {name!r}")
    i = 1
    semitone = _NAMES.index(letter)
    while i < len(s) and s[i] in "#b♯♭":
        semitone += 1 if s[i] in "#♯" else -1
        i += 1
    try:
        octave = int(s[i:])
    except ValueError as e:
        raise ValueError(f"bad octave in note name {name!r}") from e
    return (octave + 1) * 12 + semitone


def write_midi(tl: Timeline, path: str | Path, ticks_per_beat: int = 480) -> Path:
    """Write a Timeline back out as a MIDI file, one track per part.

    Round-tripping matters for the workflow: the arrangement can be generated
    here, opened in a DAW, edited by ear, and read back.
    """
    import mido

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)

    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("track_name", name=tl.name or "orchestra", time=0))
    meta.append(
        mido.MetaMessage(
            "time_signature",
            numerator=tl.meter.numerator,
            denominator=tl.meter.denominator,
            time=0,
        )
    )
    last_tick = 0
    for change in tl.tempo.changes:
        tick = int(round(change.beat * ticks_per_beat))
        meta.append(
            mido.MetaMessage(
                "set_tempo",
                tempo=int(round(60_000_000.0 / change.bpm)),
                time=tick - last_tick,
            )
        )
        last_tick = tick
    mid.tracks.append(meta)

    for name in tl.tracks():
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        pending: list[tuple[int, str, int, int]] = []
        for e in tl.events_for(track=name):
            on = int(round(e.beat * ticks_per_beat))
            off = int(round((e.beat + e.duration_beats) * ticks_per_beat))
            pending.append((on, "note_on", e.pitch, e.velocity))
            pending.append((max(off, on + 1), "note_off", e.pitch, 0))
        pending.sort(key=lambda x: (x[0], x[1] == "note_on"))
        prev = 0
        for tick, kind, pitch, vel in pending:
            track.append(
                mido.Message(kind, note=pitch, velocity=vel, time=tick - prev)
            )
            prev = tick
        mid.tracks.append(track)

    mid.save(str(path))
    return path
