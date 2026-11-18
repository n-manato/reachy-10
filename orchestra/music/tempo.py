"""Tempo map: the bidirectional beat <-> seconds mapping.

Lives on its own, imported by both the motion compiler and the music layer, so
neither has to depend on the other.

Piecewise-constant tempo, which is exactly what MIDI gives you: a list of
``set_tempo`` events, each in force until the next. Supporting this properly
(rather than assuming a single BPM) is what lets a song ritardando at the end
without the choreography sliding out of alignment.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TempoChange:
    beat: float
    bpm: float


class TempoMap:
    """Maps musical beats to wall seconds and back.

    Beat 0 is time 0 by definition. Tempo is constant between changes.
    """

    def __init__(self, changes: list[TempoChange] | None = None):
        changes = list(changes or [TempoChange(0.0, 120.0)])
        changes.sort(key=lambda c: c.beat)
        if changes[0].beat > 0.0:
            changes.insert(0, TempoChange(0.0, changes[0].bpm))
        for c in changes:
            if c.bpm <= 0:
                raise ValueError(f"tempo at beat {c.beat} must be > 0, got {c.bpm}")

        # Deduplicate identical consecutive tempi -- keeps the segment search
        # short and makes the map's repr readable.
        deduped = [changes[0]]
        for c in changes[1:]:
            if abs(c.bpm - deduped[-1].bpm) > 1e-9:
                deduped.append(c)
        self.changes = deduped

        self._beats = np.array([c.beat for c in self.changes], dtype=np.float64)
        self._bpm = np.array([c.bpm for c in self.changes], dtype=np.float64)
        self._spb = 60.0 / self._bpm  # seconds per beat within each segment

        # Cumulative seconds at each tempo change.
        seg_beats = np.diff(self._beats)
        self._times = np.concatenate([[0.0], np.cumsum(seg_beats * self._spb[:-1])])

    # -- construction ----------------------------------------------------
    @staticmethod
    def constant(bpm: float) -> "TempoMap":
        return TempoMap([TempoChange(0.0, float(bpm))])

    # -- queries ---------------------------------------------------------
    @property
    def is_constant(self) -> bool:
        return len(self.changes) == 1

    @property
    def initial_bpm(self) -> float:
        return float(self._bpm[0])

    def bpm_at_beat(self, beat: float | np.ndarray) -> np.ndarray:
        i = np.clip(np.searchsorted(self._beats, beat, side="right") - 1, 0, None)
        return self._bpm[i]

    def beats_to_seconds(self, beats: float | np.ndarray) -> np.ndarray:
        b = np.asarray(beats, dtype=np.float64)
        i = np.clip(np.searchsorted(self._beats, b, side="right") - 1, 0, len(self._beats) - 1)
        return self._times[i] + (b - self._beats[i]) * self._spb[i]

    def seconds_to_beats(self, seconds: float | np.ndarray) -> np.ndarray:
        s = np.asarray(seconds, dtype=np.float64)
        i = np.clip(np.searchsorted(self._times, s, side="right") - 1, 0, len(self._times) - 1)
        return self._beats[i] + (s - self._times[i]) / self._spb[i]

    def seconds_per_beat_at(self, beat: float) -> float:
        return float(60.0 / self.bpm_at_beat(beat))

    def __repr__(self) -> str:
        if self.is_constant:
            return f"TempoMap(constant {self.initial_bpm:g} BPM)"
        parts = ", ".join(f"b{c.beat:g}:{c.bpm:g}" for c in self.changes[:4])
        more = "..." if len(self.changes) > 4 else ""
        return f"TempoMap({len(self.changes)} changes: {parts}{more})"


@dataclass(frozen=True)
class Meter:
    """Time signature. `beats_per_bar` is in the tempo map's beat unit."""

    numerator: int = 4
    denominator: int = 4

    @property
    def beats_per_bar(self) -> float:
        # MIDI tempo is quarter-note based, so a 6/8 bar is 3 quarter-note beats.
        return self.numerator * 4.0 / self.denominator

    def bar_of_beat(self, beat: float) -> int:
        return int(beat // self.beats_per_bar)

    def beat_in_bar(self, beat: float) -> float:
        return float(beat % self.beats_per_bar)

    def __str__(self) -> str:
        return f"{self.numerator}/{self.denominator}"
