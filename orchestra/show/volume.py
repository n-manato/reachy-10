"""Live master volume for the whole fleet, adjustable mid-performance.

A show is 148 seconds long and `Show.monitor` spends all of it blocked. That
leaves no place to change the volume from -- which matters, because the right
level is not knowable until the robots are actually playing in the actual room.

This is the sending half. Key handling lives in `KeyWatch` (orchestra/cli.py);
all this needs is `set()` / `nudge()` called from somewhere.

Three constraints shape the design, and each one came from reading how the rest
of the system behaves rather than from taste:

1. **Never send through the daemon's REST route.** `POST /api/volume/set`
   plays `impatient1.wav` on every call. During a performance that is one robot
   emitting a foreign noise. The agent's `op_volume` rides the SDK session that
   is already open and plays nothing.

2. **Coalesce.** A held arrow key repeats ~30 times a second. Nine robots times
   thirty is 270 RPCs per second for a value the ear cannot follow anyway. Only
   the latest value matters, so send at most `MIN_INTERVAL_S` apart and only
   when it actually differs from what was last sent.

3. **Never overlap fan-outs.** `AgentLink` holds a per-link lock across
   send-and-read, so a slow volume call delays the next `status()` on that same
   link. `Show.monitor` treats three consecutive status failures as "this robot
   dropped out" -- so a pile-up here could report a robot as lost while it is
   playing perfectly well. One fan-out in flight at a time.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

MIN_INTERVAL_S = 0.1
"""送信の下限間隔。キーリピートを間引くためのもの。

100 ms なら人の操作としては即応に感じるし、9台×毎秒10回までに抑えられる。"""

TICK_S = 0.02
"""目標値を見に行く周期。送信間隔とは別。

短いのは反応のため ―― 押してから最大 20 ms で送信判断に入る。"""


class VolumeController:
    """Owns one master volume and pushes it to every robot.

    Nothing is sent until the value is actually changed. The nine robots may
    boot at different levels; aligning them unasked would change the balance of
    a show that is already running. The first keypress aligns them, and that is
    a deliberate, visible moment rather than a silent one at startup.
    """

    def __init__(
        self,
        fleet_of: Callable[[], object],
        initial: int,
        apply_initial: bool = False,
        on_change: Callable[[int], None] | None = None,
    ) -> None:
        """
        Args:
            fleet_of: returns the CURRENT fleet. Not the fleet itself --
                `cmd_show_run` rebuilds `show.fleet` when robots drop at
                connect and again at arm, so a captured object goes stale and
                would keep talking to robots that are no longer in the show.
            initial: starting value, from config or read off the robots.
            apply_initial: push `initial` once at startup. True when it came
                from config -- restoring a saved level is the whole point of
                having saved it. False when it was *read off* the robots,
                because then it is already their state and re-sending it would
                only align nine robots that nobody asked to align.
            on_change: called with the new value whenever it moves, for display.
        """
        self._fleet_of = fleet_of
        self._value = _clamp(initial)
        self._sent: int | None = None if apply_initial else self._value
        self._touched = False
        self._on_change = on_change
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: dict[str, str] = {}
        """Last failure per robot. A robot that will not take a volume change
        is not a reason to stop the show, but it should not vanish either."""

    # -- state -----------------------------------------------------------
    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    @property
    def touched(self) -> bool:
        """Did a person move this? Decides whether to write it to config.

        Deliberately not "was anything sent" -- restoring a value that config
        already holds is not a reason to rewrite config.
        """
        return self._touched

    def set(self, value: int) -> int:
        return self._move(lambda cur: value)

    def nudge(self, delta: int) -> int:
        return self._move(lambda cur: cur + delta)

    def _move(self, fn) -> int:
        with self._lock:
            self._value = _clamp(fn(self._value))
            self._touched = True
            v = self._value
        self._wake.set()
        if self._on_change is not None:
            self._on_change(v)
        return v

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "VolumeController":
        self._thread = threading.Thread(
            target=self._run, name="volume", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            # Long enough for one in-flight fan-out to land. A volume change
            # that never arrives is harmless; hanging the exit is not.
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        last_send = 0.0
        while not self._stop.is_set():
            self._wake.wait(TICK_S)
            self._wake.clear()
            with self._lock:
                want = self._value
            if want == self._sent:
                continue
            now = time.monotonic()
            if now - last_send < MIN_INTERVAL_S:
                continue
            last_send = now
            self._push(want)

    def _push(self, value: int) -> None:
        fleet = self._fleet_of()
        if fleet is None or not getattr(fleet, "links", None):
            return
        try:
            results = fleet.volume(value)
        except Exception as e:  # noqa: BLE001
            # The fan-out itself is not supposed to raise -- it collects
            # per-robot failures. If it does, the show still matters more.
            self.errors["*"] = str(e)
            return
        for rid, (_res, err) in results.items():
            if err is None:
                self.errors.pop(rid, None)
            else:
                self.errors[rid] = str(err)
        self._sent = value


def _clamp(v: int) -> int:
    return max(0, min(100, int(v)))


def read_current(ctx, robots) -> dict[str, int]:
    """Read each robot's current volume from the daemon.

    `GET /api/volume/current` is the read side and is silent. It is only the
    POST that plays a sound, so this is safe to call at startup.
    """
    from ..fleet.health import DaemonClient, parallel

    def one(r):
        c = DaemonClient(r.ip, ctx.fleet.daemon_port, timeout=4.0)
        return int(c._get("/api/volume/current").get("volume"))

    out: dict[str, int] = {}
    for robot, val, err in parallel(robots, one):
        if err is None and isinstance(val, int) and 0 <= val <= 100:
            out[robot.id] = val
    return out


def median(values) -> int | None:
    """Seed value when the nine robots disagree.

    The median rather than the mean: one robot left at 0 or 100 by an earlier
    session should not drag the starting point for the other eight.
    """
    vals = sorted(int(v) for v in values)
    if not vals:
        return None
    return vals[len(vals) // 2]


def bar(value: int, width: int = 20) -> str:
    filled = round(width * value / 100)
    return "#" * filled + "-" * (width - filled)


# ---------------------------------------------------------------------------
# Where the starting value comes from, and where it goes back to
# ---------------------------------------------------------------------------
# These live here rather than next to a front end because both front ends need
# them and they are about volume, not about terminals or HTTP. `orchestra show
# run` and `orchestra web` must agree on the starting level or the same room
# sounds different depending on how the show was launched.


def seed_volume(ctx, robots) -> tuple[int, str]:
    """The level to start at, and where it came from ("config"/"robots"/"default").

    Config wins -- saving a level is a statement about this room, and reading
    the robots would overwrite it with whatever the last session left behind.

    Reading the robots is safe to do at startup: it is `GET
    /api/volume/current`, and only the POST side plays `impatient1.wav`.
    """
    from ..config import Calibration

    root = ctx.root / "config" / "calibration"
    saved = [
        c.volume
        for c in (Calibration.load(r.id, root) for r in robots)
        if c.volume is not None
    ]
    if saved:
        return int(median(saved)), "config"
    live = read_current(ctx, robots)
    if live:
        # Nine robots that disagree are not aligned here. The first press
        # aligns them, which is a visible moment rather than a silent one at
        # startup.
        return int(median(live.values())), "robots"
    return 50, "default"


def save_volume(ctx, robots, value: int) -> int:
    """Write the level into each robot's calibration file. Returns the count.

    Load-then-save per robot, so `audio_lead_ms` and the rest survive. Only
    call this when a person actually moved it -- rewriting a setting nobody
    touched is how saved values quietly rot.
    """
    from ..config import Calibration

    root = ctx.root / "config" / "calibration"
    for r in robots:
        cal = Calibration.load(r.id, root)
        cal.volume = int(value)
        cal.save(root)
    return len(robots)
