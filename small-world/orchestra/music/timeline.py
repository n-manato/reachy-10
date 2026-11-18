"""The musical timeline: bars, beats, phrases and note events.

Everything the choreography engine needs to know about the *music*, separated
from anything about robots. A timeline can come from a MIDI file, from a text
rhythm pattern, or be built by hand -- the compiler downstream neither knows nor
cares which.

Phrases matter more than they might appear. They are what drives the conductor's
amplitude shaping and the performers' sway envelope, and they are the difference
between an ensemble that looks like it is following the music and one that looks
like it is following a metronome.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .tempo import Meter, TempoMap


@dataclass(frozen=True)
class NoteEvent:
    """One sound-producing musical event."""

    beat: float
    pitch: int = 60
    velocity: int = 100
    duration_beats: float = 0.25
    track: str = ""
    channel: int = 0

    @property
    def dynamic(self) -> float:
        """Velocity normalised to 0..1, for scaling gesture amplitude."""
        return max(0.0, min(1.0, self.velocity / 127.0))


@dataclass
class Phrase:
    index: int
    start_beat: float
    end_beat: float

    @property
    def length_beats(self) -> float:
        return self.end_beat - self.start_beat

    def position(self, beat: float) -> float:
        """0 at the phrase start, 1 at its end. Outside the phrase, clamped."""
        if self.length_beats <= 0:
            return 0.0
        return float(np.clip((beat - self.start_beat) / self.length_beats, 0.0, 1.0))


@dataclass
class Timeline:
    tempo: TempoMap
    meter: Meter = field(default_factory=Meter)
    events: list[NoteEvent] = field(default_factory=list)
    phrases: list[Phrase] = field(default_factory=list)
    total_beats: float = 0.0
    name: str = ""

    # -- basics ----------------------------------------------------------
    @property
    def duration_s(self) -> float:
        return float(self.tempo.beats_to_seconds(self.total_beats))

    @property
    def n_bars(self) -> int:
        return int(np.ceil(self.total_beats / self.meter.beats_per_bar))

    def tracks(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self.events:
            seen.setdefault(e.track, None)
        return list(seen)

    def events_for(
        self,
        track: str | None = None,
        channel: int | None = None,
        pitches: set[int] | None = None,
    ) -> list[NoteEvent]:
        out = []
        for e in self.events:
            if track is not None and e.track != track:
                continue
            if channel is not None and e.channel != channel:
                continue
            if pitches is not None and e.pitch not in pitches:
                continue
            out.append(e)
        return sorted(out, key=lambda e: e.beat)

    # -- grids -----------------------------------------------------------
    def beat_grid(self) -> np.ndarray:
        return np.arange(0.0, self.total_beats + 1e-9, 1.0)

    def bar_starts(self) -> np.ndarray:
        return np.arange(0.0, self.total_beats + 1e-9, self.meter.beats_per_bar)

    def downbeats_s(self) -> np.ndarray:
        return self.tempo.beats_to_seconds(self.bar_starts())

    # -- phrases ---------------------------------------------------------
    def build_phrases(self, bars_per_phrase: int = 4) -> list[Phrase]:
        """Regular phrasing. The right default for folk and children's tunes,
        which is exactly the repertoire here."""
        bpb = self.meter.beats_per_bar
        span = bars_per_phrase * bpb
        out: list[Phrase] = []
        i, b = 0, 0.0
        while b < self.total_beats - 1e-9:
            out.append(Phrase(i, b, min(b + span, self.total_beats)))
            b += span
            i += 1
        self.phrases = out
        return out

    def phrases_from_rests(self, min_gap_beats: float = 1.0) -> list[Phrase]:
        """Infer phrasing from where the music breathes.

        Useful when a MIDI arrangement does not divide into even blocks. Falls
        back to regular phrasing if the piece has no rests worth the name.
        """
        if not self.events:
            return self.build_phrases()
        beats = sorted(e.beat for e in self.events)
        breaks = [
            (beats[i] + beats[i + 1]) / 2
            for i in range(len(beats) - 1)
            if beats[i + 1] - beats[i] >= min_gap_beats
        ]
        if not breaks:
            return self.build_phrases()
        bounds = [0.0, *breaks, self.total_beats]
        self.phrases = [
            Phrase(i, bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
        ]
        return self.phrases

    def phrase_at(self, beat: float) -> Phrase | None:
        for p in self.phrases:
            if p.start_beat - 1e-9 <= beat < p.end_beat:
                return p
        return self.phrases[-1] if self.phrases else None

    def phrase_envelope(
        self, times_s: np.ndarray, lead_in_s: float = 0.0, shape: float = 0.6
    ) -> np.ndarray:
        """A 0..1 intensity curve that rises through each phrase and settles.

        Shape is a raised arch peaking around 60% of the way in, which is where
        a phrase's tension usually sits -- an arch peaking dead centre reads as
        mechanical, and one peaking at the end fights the cadence.
        """
        beats = self.tempo.seconds_to_beats(np.maximum(times_s - lead_in_s, 0.0))
        env = np.zeros_like(beats)
        if not self.phrases:
            self.build_phrases()
        for p in self.phrases:
            m = (beats >= p.start_beat) & (beats < p.end_beat)
            if not m.any():
                continue
            u = (beats[m] - p.start_beat) / max(p.length_beats, 1e-9)
            env[m] = np.sin(np.pi * np.clip(u, 0, 1) ** shape)
        return np.clip(env, 0.0, 1.0)

    def phrase_starts_s(self, lead_in_s: float = 0.0) -> np.ndarray:
        if not self.phrases:
            self.build_phrases()
        return (
            self.tempo.beats_to_seconds(np.array([p.start_beat for p in self.phrases]))
            + lead_in_s
        )

    # -- construction ----------------------------------------------------
    @staticmethod
    def from_events(
        events: list[NoteEvent],
        bpm: float = 120.0,
        meter: Meter | None = None,
        total_beats: float | None = None,
        name: str = "",
    ) -> "Timeline":
        m = meter or Meter()
        if total_beats is None:
            last = max((e.beat + e.duration_beats for e in events), default=0.0)
            # Round up to a whole bar so the piece ends where a musician expects.
            total_beats = float(np.ceil(last / m.beats_per_bar) * m.beats_per_bar)
        tl = Timeline(
            tempo=TempoMap.constant(bpm),
            meter=m,
            events=sorted(events, key=lambda e: e.beat),
            total_beats=total_beats,
            name=name,
        )
        tl.build_phrases()
        return tl


# ---------------------------------------------------------------------------
# Text rhythm patterns
# ---------------------------------------------------------------------------


def parse_pattern(
    pattern: str,
    step_beats: float = 0.25,
    track: str = "pattern",
    pitch: int = 60,
    accent_velocity: int = 110,
    normal_velocity: int = 80,
) -> list[NoteEvent]:
    """Parse a compact text rhythm into note events.

    Grammar, chosen so a drum part is legible at a glance and diffs cleanly::

        X   accented hit
        x   normal hit
        .   rest
        -   rest (alias, reads better for sustained gaps)
        |   bar line, ignored (purely visual)
        space  ignored

    ``parse_pattern("X..x|x..x", step_beats=0.25)`` is two beats of sixteenths
    with an accent on the downbeat.
    """
    events: list[NoteEvent] = []
    step = 0
    for ch in pattern:
        if ch in "| \t\n":
            continue
        if ch in ".-_":
            step += 1
            continue
        if ch in "Xx":
            events.append(
                NoteEvent(
                    beat=step * step_beats,
                    pitch=pitch,
                    velocity=accent_velocity if ch == "X" else normal_velocity,
                    duration_beats=step_beats,
                    track=track,
                )
            )
            step += 1
            continue
        raise ValueError(
            f"unknown character {ch!r} in rhythm pattern. "
            "Use X (accent), x (hit), . or - (rest), | (bar line)."
        )
    return events


def pattern_length_beats(pattern: str, step_beats: float = 0.25) -> float:
    steps = sum(1 for ch in pattern if ch not in "| \t\n")
    return steps * step_beats
