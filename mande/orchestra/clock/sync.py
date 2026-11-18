"""Clock synchronisation: Cristian's algorithm over the agent control channel.

The problem this solves: the daemon has no scheduled-execution primitive, so the
only way nine robots can begin together is for each to already know *when* to
begin, on its own clock. That needs a shared timebase, and there is no NTP server
on the isolated show network.

So we do it at the application layer, which turns out to be both simpler and
more accurate than depending on system time:

    PC   ---- t0 ---->                          (PC monotonic at send)
                        robot: t1 (recv), t2 (send)
    PC   <---- t3 ----                          (PC monotonic at receive)

    rtt    = (t3 - t0) - (t2 - t1)
    offset = ((t1 - t0) + (t2 - t3)) / 2        -> robot_mono - pc_mono

Two properties matter. First, both sides use ``time.monotonic()``, so differing
epochs are irrelevant and a wall-clock step (NTP, manual set, DST) cannot corrupt
a running show. Second, the estimator keeps only the lowest-RTT samples: offset
error is bounded by the *asymmetry* of the path, and the fastest round trips are
the ones with the least queuing in either direction. Averaging everything would
be strictly worse than averaging the best few.

With this fleet's measured ~6-7 ms RTT floor and 1-2 ms jitter, the expected
offset accuracy is +/-1-3 ms -- comfortably inside the ~10-20 ms window where
percussion attacks read as simultaneous.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field


@dataclass
class Sample:
    seq: int
    rtt: float
    offset: float
    """robot_monotonic - pc_monotonic, in seconds."""


@dataclass
class ClockOffset:
    """The result of one sync round."""

    robot_id: str = ""
    offset: float = 0.0
    rtt_min: float = 0.0
    rtt_median: float = 0.0
    spread: float = 0.0
    """Std-dev of the offsets among the kept samples: our uncertainty estimate."""
    n_samples: int = 0
    n_kept: int = 0
    measured_at: float = 0.0
    ok: bool = False
    reason: str = ""
    samples: list[Sample] = field(default_factory=list)

    @property
    def uncertainty_ms(self) -> float:
        """Conservative bound: the worse of sample spread and half the min RTT.

        Half the minimum RTT is the irreducible error if the path is maximally
        asymmetric, so quoting anything better than that would be optimistic.
        """
        return max(self.spread, self.rtt_min / 2.0) * 1000.0

    def to_local(self, pc_time: float) -> float:
        """Convert a PC monotonic instant into this robot's monotonic clock."""
        return pc_time + self.offset

    def summary(self) -> str:
        if not self.ok:
            return f"{self.robot_id}: SYNC FAILED ({self.reason})"
        return (
            f"{self.robot_id}: offset {self.offset * 1000:+9.3f} ms  "
            f"rtt_min {self.rtt_min * 1000:5.2f} ms  "
            f"+/-{self.uncertainty_ms:.2f} ms  ({self.n_kept}/{self.n_samples})"
        )


def estimate(
    samples: list[Sample],
    keep_best: int = 8,
    max_acceptable_rtt: float = 0.060,
    robot_id: str = "",
) -> ClockOffset:
    """Reduce raw probes to a single offset estimate."""
    res = ClockOffset(robot_id=robot_id, measured_at=time.monotonic(),
                      n_samples=len(samples), samples=samples)
    if not samples:
        res.reason = "no samples"
        return res

    ordered = sorted(samples, key=lambda s: s.rtt)
    res.rtt_min = ordered[0].rtt
    res.rtt_median = statistics.median(s.rtt for s in samples)

    if res.rtt_min > max_acceptable_rtt:
        res.reason = (
            f"best RTT {res.rtt_min * 1000:.1f} ms exceeds the "
            f"{max_acceptable_rtt * 1000:.0f} ms ceiling -- link too congested "
            "to trust this estimate"
        )
        return res

    kept = ordered[: max(1, min(keep_best, len(ordered)))]
    offs = [s.offset for s in kept]
    res.offset = statistics.fmean(offs)
    res.spread = statistics.pstdev(offs) if len(offs) > 1 else 0.0
    res.n_kept = len(kept)
    res.ok = True
    return res


class DriftTracker:
    """Tracks a robot's clock drift across successive sync rounds.

    A Raspberry Pi crystal typically runs a few tens of ppm off, which over a
    3-minute piece is a handful of milliseconds -- small, but the same order as
    the sync accuracy itself, so it is worth measuring rather than assuming.

    Also smooths the offset: a single noisy round should nudge the estimate, not
    replace it. The smoothing is deliberately light so a genuine step (agent
    restart, clock reset) is still tracked within a couple of rounds.
    """

    def __init__(self, alpha: float = 0.4):
        self.alpha = alpha
        self.offset: float | None = None
        self.history: list[ClockOffset] = []

    def update(self, obs: ClockOffset) -> float:
        if not obs.ok:
            return self.offset if self.offset is not None else 0.0
        self.history.append(obs)
        if self.offset is None:
            self.offset = obs.offset
        else:
            # Weight a confident round more heavily than a noisy one.
            conf = 1.0 / max(obs.uncertainty_ms, 0.1)
            prev_conf = 1.0 / max(self.drift_uncertainty_ms(), 0.1)
            a = min(0.9, max(self.alpha, conf / (conf + prev_conf)))
            self.offset = (1 - a) * self.offset + a * obs.offset
        return self.offset

    def drift_uncertainty_ms(self) -> float:
        return self.history[-1].uncertainty_ms if self.history else 10.0

    def drift_ppm(self) -> float | None:
        """Estimated clock drift in parts per million, once there is a baseline."""
        if len(self.history) < 2:
            return None
        a, b = self.history[0], self.history[-1]
        dt = b.measured_at - a.measured_at
        if dt < 30.0:
            # Too short a baseline: the answer would be dominated by sync noise.
            return None
        return (b.offset - a.offset) / dt * 1e6

    def projected_error_ms(self, horizon_s: float) -> float | None:
        """How far the clocks will have parted after `horizon_s` of playing."""
        ppm = self.drift_ppm()
        if ppm is None:
            return None
        return abs(ppm) * horizon_s / 1000.0
