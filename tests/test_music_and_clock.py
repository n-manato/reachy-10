import numpy as np
import pytest

from orchestra.clock.sync import DriftTracker, Sample, estimate
from orchestra.music.midi import note_name, note_number
from orchestra.music.tempo import Meter, TempoChange, TempoMap
from orchestra.music.timeline import NoteEvent, Timeline, parse_pattern


# ---------------------------------------------------------------------------
# Tempo
# ---------------------------------------------------------------------------


def test_constant_tempo_roundtrip():
    tm = TempoMap.constant(120)
    assert tm.beats_to_seconds(4.0) == pytest.approx(2.0)
    assert tm.seconds_to_beats(2.0) == pytest.approx(4.0)


def test_variable_tempo_is_piecewise():
    """A ritardando must be data, not a special case -- MIDI gives it to us as
    a list of set_tempo events."""
    tm = TempoMap([TempoChange(0.0, 120.0), TempoChange(4.0, 60.0)])
    assert tm.beats_to_seconds(4.0) == pytest.approx(2.0)   # 4 beats @120
    assert tm.beats_to_seconds(6.0) == pytest.approx(4.0)   # +2 beats @60
    assert tm.seconds_to_beats(4.0) == pytest.approx(6.0)


def test_tempo_roundtrip_random():
    tm = TempoMap([TempoChange(0, 100), TempoChange(3, 140), TempoChange(9, 80)])
    b = np.linspace(0, 20, 200)
    assert np.allclose(tm.seconds_to_beats(tm.beats_to_seconds(b)), b, atol=1e-9)


def test_negative_seconds_give_negative_beats():
    """The lead-in depends on this: a windup before beat 0 must be
    representable, not clamped."""
    tm = TempoMap.constant(120)
    assert tm.seconds_to_beats(-0.5) == pytest.approx(-1.0)


def test_zero_tempo_rejected():
    with pytest.raises(ValueError):
        TempoMap([TempoChange(0.0, 0.0)])


def test_meter_beats_per_bar():
    assert Meter(4, 4).beats_per_bar == 4.0
    assert Meter(3, 4).beats_per_bar == 3.0
    assert Meter(6, 8).beats_per_bar == 3.0   # quarter-note beats


# ---------------------------------------------------------------------------
# Patterns and timeline
# ---------------------------------------------------------------------------


def test_pattern_grammar():
    evs = parse_pattern("X..x", step_beats=0.25)
    assert [e.beat for e in evs] == [0.0, 0.75]
    assert evs[0].velocity > evs[1].velocity, "X is the accent"


def test_pattern_ignores_bar_lines():
    assert len(parse_pattern("x.x.|x.x.")) == len(parse_pattern("x.x.x.x."))


def test_pattern_rejects_junk():
    with pytest.raises(ValueError, match="unknown character"):
        parse_pattern("x.q.")


def test_phrase_envelope_shape():
    tl = Timeline.from_events(
        [NoteEvent(beat=float(i)) for i in range(16)], bpm=120, meter=Meter(4, 4)
    )
    tl.build_phrases(bars_per_phrase=2)
    t = np.linspace(0, tl.duration_s, 400)
    env = tl.phrase_envelope(t)
    assert env.min() >= 0.0 and env.max() <= 1.0
    assert env.max() > 0.9, "each phrase should reach full intensity"


def test_events_filtered_by_pitch():
    evs = [NoteEvent(beat=i, pitch=60 + (i % 3), track="M") for i in range(9)]
    tl = Timeline.from_events(evs, bpm=120)
    assert len(tl.events_for(track="M", pitches={60})) == 3


def test_note_name_roundtrip():
    for n in range(21, 108):
        assert note_number(note_name(n)) == n


def test_note_number_accepts_accidentals():
    assert note_number("C4") == 60
    assert note_number("F#4") == 66
    assert note_number("Bb3") == 58


# ---------------------------------------------------------------------------
# Clock sync
# ---------------------------------------------------------------------------


def test_estimate_recovers_a_known_offset():
    """Cristian's algorithm on a symmetric path should return the true offset."""
    true_offset = 1.2345
    samples = [
        Sample(seq=i, rtt=0.006 + 0.001 * (i % 5),
               offset=true_offset + 0.0002 * ((i % 7) - 3))
        for i in range(40)
    ]
    res = estimate(samples, keep_best=8, robot_id="r01")
    assert res.ok
    assert res.offset == pytest.approx(true_offset, abs=1e-3)
    assert res.n_kept == 8


def test_estimate_prefers_low_rtt_samples():
    """Offset error is bounded by path ASYMMETRY, and the fastest round trips
    are the least asymmetric. Averaging everything would be worse."""
    good = [Sample(i, rtt=0.006, offset=1.0) for i in range(10)]
    bad = [Sample(i + 10, rtt=0.400, offset=1.9) for i in range(30)]
    res = estimate(good + bad, keep_best=8)
    assert res.offset == pytest.approx(1.0, abs=1e-6), (
        "high-RTT outliers must not drag the estimate"
    )


def test_estimate_rejects_a_congested_link():
    samples = [Sample(i, rtt=0.5, offset=1.0) for i in range(20)]
    res = estimate(samples, max_acceptable_rtt=0.06)
    assert not res.ok
    assert "congested" in res.reason or "ceiling" in res.reason


def test_estimate_with_no_samples():
    res = estimate([], robot_id="r01")
    assert not res.ok
    assert "SYNC FAILED" in res.summary()


def test_uncertainty_never_beats_half_rtt():
    """Quoting better accuracy than half the minimum RTT would be dishonest:
    that is the irreducible error if the path is maximally asymmetric."""
    samples = [Sample(i, rtt=0.020, offset=1.0) for i in range(20)]
    res = estimate(samples)
    assert res.uncertainty_ms >= 10.0 - 1e-9


def test_to_local_applies_offset():
    res = estimate([Sample(0, rtt=0.006, offset=5.0)], keep_best=1)
    assert res.to_local(100.0) == pytest.approx(105.0)


def test_drift_tracker_smooths():
    d = DriftTracker()
    d.update(estimate([Sample(0, rtt=0.006, offset=1.000)], keep_best=1))
    d.update(estimate([Sample(0, rtt=0.006, offset=1.100)], keep_best=1))
    assert 1.0 < d.offset < 1.1, "a single noisy round should nudge, not replace"


def test_drift_tracker_replaces_an_estimate_left_from_days_ago():
    """The console stays up for days and its links keep their trackers.

    Blending in a ten-day-old offset put START 18 s in the past, and halved
    that per take, while the sync report showed a healthy measured offset.
    """
    old = estimate([Sample(0, rtt=0.006, offset=1.000)], keep_best=1)
    new = estimate([Sample(0, rtt=0.006, offset=19.000)], keep_best=1)
    old.measured_at, new.measured_at = 0.0, 10 * 86400.0

    d = DriftTracker()
    d.update(old)
    d.update(new)
    assert d.offset == pytest.approx(19.000)


def test_drift_ppm_needs_a_baseline():
    d = DriftTracker()
    d.update(estimate([Sample(0, rtt=0.006, offset=1.0)], keep_best=1))
    assert d.drift_ppm() is None, "one sample cannot establish drift"
