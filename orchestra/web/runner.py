"""The performance, driven by four buttons instead of a terminal.

`orchestra show run` is the same lifecycle with a person watching the log. Two
things change when the operator is a web page, and both are structural rather
than cosmetic.

**There is no pause, by design.** A Reachy Mini has no way to hold a phrase and
resume it: the agent plays its program against its own monotonic clock, and the
only ways out of that loop are *finish* and *abort to neutral*. A pause button
would therefore have to be a lie -- either a freeze at the last target, which
is the condition that stalls a servo against its own travel limit, or a silent
restart. So STOP returns to neutral and PLAY always starts from the top, which
is the honest shape of the hardware rather than a simplification of it.

**Stop has to answer immediately.** The terminal front end asks for a stop at
the end of the current take, which is right for a performance someone chose to
run and wrong for a button on a wall. So STOP aborts mid-take, through the
agents, which fade the music and lower the head over `agent.fade_out_s`, and
never freeze mid-strike.

The state machine lives in one thread because the thing it drives is one thing:
nine robots on one timeline. Commands arrive from HTTP handlers on the event
loop and are handed over as events, never as direct calls into the fleet -- an
`AgentLink` holds a per-link lock across send-and-read, so a handler that
called into it directly could block the whole event loop behind a robot that
had stopped answering.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..fleet.link import AgentLink
from ..show.run import LOOP_GAP_S, Show, make_fleet, sleep_unless

ARM_SETTLE_S = 0.5
"""Arming returns once the head has reached the start pose, and the clock sync
after this adds about 2 s more. Any longer is nine robots standing motionless
in front of the audience."""
"""Pause after arming, before the clock sync.

`op_arm` ends with a 2.0 s `goto_target` to the start pose and returns without
waiting for it. Syncing clocks through an agent that is still running a goto
measures the goto, not the network.
"""


class State:
    """Where the show is. Strings, because they go straight out as JSON."""

    OFFLINE = "offline"
    """Nothing connected. Either the agents are not up or nothing tried yet."""
    OPENING = "opening"
    ARMING = "arming"
    SYNCING = "syncing"
    COUNTDOWN = "countdown"
    PLAYING = "playing"
    GAP = "gap"
    STOPPING = "stopping"
    READY = "ready"
    ERROR = "error"

    BUSY = (OPENING, ARMING, SYNCING, COUNTDOWN, PLAYING, GAP, STOPPING)
    """States in which PLAY does nothing. Anything else can start a take."""

    FADES = (ARMING, SYNCING, COUNTDOWN, PLAYING)
    """States in which STOP has a head to bring down. In the gap the last take
    has already ended in the closing pose, so there is nothing left to lower."""


class BackendError(RuntimeError):
    """The fleet could not be brought to the point of playing."""


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
# The runner never touches `Show` directly. Two reasons, in order of weight:
# the whole console -- every state transition, the loop, the stop path, the
# progress clock -- is then testable without robots; and the same page becomes
# usable as a rehearsal tool with the fleet switched off, which is when most of
# the looking at it happens.


@dataclass
class TakeResult:
    """One take, reduced to what a screen can show."""

    completed: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    aborted: bool = False
    duration: float = 0.0
    worst_start_error_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed": list(self.completed),
            "dropped": list(self.dropped),
            "failed": dict(self.failed),
            "aborted": self.aborted,
            "duration": round(self.duration, 2),
            "worst_start_error_ms": self.worst_start_error_ms,
        }


class FleetBackend:
    """Nine real robots, via the on-robot agents."""

    mode = "fleet"

    def __init__(self, ctx, robots: list):
        self.ctx = ctx
        self.robots = robots
        self.show: Show | None = None
        self.duration = 0.0
        self.load_info: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self._volume = None
        self.volume_source = "default"

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        """Connect, load, and start the heartbeat. Leaves nothing behind if it
        fails -- a half-open fleet means a second heartbeat thread on the next
        attempt, all of them talking to the same nine robots."""
        from ..show.volume import VolumeController, seed_volume

        show = Show(self.ctx, self.robots)
        self.errors = {}
        try:
            self.errors.update(show.connect())
            live = [l for l in show.fleet.links if l.connected]
            if not live:
                raise BackendError(
                    "no agent answered. Start them with "
                    "`orchestra fleet agent start`, or run the console with "
                    "--rehearse to drive the screen without robots."
                )
            # Drop the unreachable ones rather than carrying them: every later
            # fan-out would wait out their timeout. `refresh` lets them back in
            # before a later take.
            show.fleet = type(show.fleet)(live)
            if show.heartbeat is not None:
                show.heartbeat.fleet = show.fleet

            for rid, (info, err) in show.fleet.load(show.program_for).items():
                if err:
                    self.errors[rid] = str(err)
                    continue
                self.load_info[rid] = info
                self.duration = max(self.duration, float(info.get("duration", 0.0)))
            if not self.load_info:
                raise BackendError("no robot could load its program")
        except Exception:
            if show.heartbeat is not None:
                show.heartbeat.stop()
            show.fleet.close()
            raise

        self.show = show
        self._health()
        here = [r for r in self.robots if r.id in self.load_info]
        seed, self.volume_source = seed_volume(self.ctx, here)
        self._volume = VolumeController(
            lambda: self.show.fleet if self.show else None,
            seed,
            apply_initial=(self.volume_source == "config"),
        )
        self._volume.__enter__()

    def _use(self, links: list) -> None:
        """Replace the fleet, and point the heartbeat at the same links.

        `Heartbeat` re-reads `fleet.links` every second, so rebinding is enough
        -- but forgetting it leaves a 1 Hz poke going at robots that are no
        longer in the show, and leaves robots that just joined getting none.
        The poke is what keeps Wi-Fi out of power-save, which is what stops
        START paying the measured 17-90 ms wake penalty.
        """
        assert self.show is not None
        self.show.fleet = type(self.show.fleet)(links)
        if self.show.heartbeat is not None:
            self.show.heartbeat.fleet = self.show.fleet

    def refresh(self) -> None:
        """Give robots that dropped out of an earlier take another chance.

        `arm` removes whatever failed, because sending START to a robot that
        did not arm fails every time and reports the same fault twice. That is
        right for one take and wrong for a console that stays up all day: one
        transient failure would otherwise cost that instrument until somebody
        restarted the server.
        """
        if self.show is None:
            return
        present = {l.robot_id for l in self.show.fleet.links}
        missing = [r for r in self.robots
                   if r.id not in present and r.id in self.load_info]
        if not missing:
            return

        links = list(self.show.fleet.links)
        for r in missing:
            link = AgentLink(r.id, r.ip, self.ctx.fleet.agent_port, timeout=10.0)
            try:
                link.connect()
                self.load_info[r.id] = link.load(self.show.program_for(r.id))
            except Exception as e:  # noqa: BLE001
                self.errors[r.id] = f"still away: {e}"
                link.close()
                continue
            self.errors.pop(r.id, None)
            links.append(link)
        self._use(links)

    def _health(self) -> None:
        """Ask each daemon how it is, and put the answer where the page can
        show it.

        `show run` prints a full preflight and refuses to continue; a wall
        console cannot refuse, because the person in front of it has nowhere
        else to go. So the checks still run and still name the fix, but they
        arrive as text next to a robot's chip rather than as an exit code. The
        agent check is left out -- we are already talking to every agent, which
        is a better answer than asking SSH about it.
        """
        from ..fleet.health import fleet_health

        try:
            health = fleet_health([r.ip for r in self.robots],
                                  self.ctx.fleet.daemon_port, timeout=5.0)
        except Exception:  # noqa: BLE001
            return
        floor = self.ctx.fleet.safety.min_control_loop_hz
        for r in self.robots:
            h = health.get(r.ip)
            if h is None or not h.reachable:
                self.errors.setdefault(r.id, "daemon not answering")
            elif h.state != "running":
                self.errors.setdefault(
                    r.id, f"daemon is {h.state} -- "
                          f"`orchestra fleet recover --robot {r.id}`")
            elif h.app_lock and h.app_lock not in ("free", ""):
                self.errors.setdefault(
                    r.id, f"app lock held by {h.app_lock} -- "
                          "`orchestra fleet stop-app`")
            elif h.control_loop_hz is not None and h.control_loop_hz < floor:
                self.errors.setdefault(
                    r.id, f"control loop {h.control_loop_hz:.1f} Hz, below the "
                          f"{floor:g} Hz floor -- this one will drift")

    def close(self) -> None:
        if self._volume is not None:
            self._volume.__exit__()
            self._volume = None
        if self.show is not None:
            try:
                self.show.teardown()
            except Exception:
                pass
            self.show = None

    # -- a take ----------------------------------------------------------
    def arm(self) -> dict[str, str]:
        assert self.show is not None
        errors: dict[str, str] = {}
        armed = []
        by_id = {l.robot_id: l for l in self.show.fleet.links}
        for rid, (info, err) in self.show.fleet.arm().items():
            if err:
                errors[rid] = str(err)
                continue
            armed.append(by_id[rid])
            if info.get("silent"):
                errors[rid] = "armed but silent (media failed to initialise)"
        if not armed:
            raise BackendError("no robot could be armed")
        # START to a robot that failed to arm always fails. Taking it out here
        # keeps one failure from being reported twice; `refresh` tries it again
        # before the next take.
        self._use(armed)
        return errors

    def sync(self) -> tuple[bool, str]:
        assert self.show is not None
        summary = self.show.sync_clocks()
        return summary.ok, summary.summary()

    def start(self) -> tuple[float, dict[str, str]]:
        assert self.show is not None
        return self.show.start()

    def monitor(self, should_stop: Callable[[], bool]) -> TakeResult:
        assert self.show is not None
        lead = float(self.ctx.fleet.show.get("start_lead_s", 3.0))
        res = self.show.monitor(self.duration + lead, should_stop=should_stop)
        starts = [
            s["start_error_ms"]
            for s in res.stats.values()
            if isinstance(s.get("start_error_ms"), (int, float))
        ]
        return TakeResult(
            completed=res.completed,
            dropped=res.dropped,
            failed=res.failed,
            aborted=res.aborted,
            duration=res.duration,
            worst_start_error_ms=max(starts, key=abs) if starts else None,
        )

    # -- stopping --------------------------------------------------------
    @property
    def fade_s(self) -> float:
        return float(self.ctx.fleet.agent.get("fade_out_s", 5.0))

    def abort(self, fade_s: float = 0.0) -> dict[str, str]:
        if self.show is None:
            return {}
        return self.show.stop(fade_s)

    def disarm(self) -> None:
        if self.show is None:
            return
        try:
            self.show.fleet.disarm()
        except Exception:
            pass

    def hard_stop(self) -> dict[str, bool]:
        if self.show is None:
            return {}
        # Tell the agents too, best-effort. `hard_stop` deliberately does not
        # involve them so that it works when one is wedged, but an agent that
        # is fine should know the show is over rather than keep pushing frames
        # at a motor controller with the torque off. On its own thread: a robot
        # that has gone would hold this for the link timeout, and the motors
        # must not wait for that.
        fleet = self.show.fleet

        def tell_agents() -> None:
            try:
                fleet.abort()
            except Exception:
                pass

        threading.Thread(target=tell_agents, name="estop-agents", daemon=True).start()
        return self.show.hard_stop()

    def reopen(self) -> None:
        """Rebuild the fleet after a hard stop or a lost agent."""
        assert self.show is not None
        self.show.fleet.close()
        self._use(make_fleet(self.ctx, self.robots).links)
        for rid, (_, err) in self.show.fleet.connect().items():
            if err:
                self.errors[rid] = str(err)
        live = [l for l in self.show.fleet.links if l.connected]
        if not live:
            raise BackendError("no agent answered on reconnect")
        self._use(live)
        for rid, (info, err) in self.show.fleet.load(self.show.program_for).items():
            if err:
                self.errors[rid] = str(err)
            else:
                self.load_info[rid] = info

    # -- volume ----------------------------------------------------------
    @property
    def volume(self) -> int:
        return self._volume.value if self._volume else 50

    @property
    def volume_touched(self) -> bool:
        return bool(self._volume and self._volume.touched)

    def set_volume(self, value: int) -> int:
        if self._volume is None:
            return 50
        return self._volume.set(value)

    def robot_rows(self) -> list[dict[str, Any]]:
        live = set()
        if self.show is not None:
            live = {l.robot_id for l in self.show.fleet.links}
        rows = []
        for r in self.robots:
            info = self.load_info.get(r.id, {})
            rows.append({
                "id": r.id,
                "role": r.role,
                "instrument": r.instrument or r.role,
                "part": info.get("part", ""),
                "loaded": r.id in self.load_info,
                "live": r.id in live,
                "audio": info.get("audio") or "",
                "error": self.errors.get(r.id, ""),
            })
        return rows


class RehearsalBackend:
    """No robots. A clock, and the same state machine around it.

    This is not a mock for tests that happens to be shipped -- it is how the
    screen gets built, demonstrated and checked when the fleet is packed away,
    and it is what makes every transition in `ShowRunner` testable. It reports
    the same duration the build says, so the twin on screen runs at exactly the
    speed the robots would.
    """

    mode = "rehearsal"

    def __init__(self, duration: float, robots: list | None = None):
        self.duration = float(duration)
        self.robots = list(robots or [])
        self.errors: dict[str, str] = {}
        self.volume_source = "default"
        self._volume = 50
        self.arm_s = 0.0
        """Pretend arming takes no time. Overridden in tests that time it."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def arm(self) -> dict[str, str]:
        if self.arm_s:
            time.sleep(self.arm_s)
        return {}

    def sync(self) -> tuple[bool, str]:
        return True, "rehearsal: no clocks to sync"

    def start(self) -> tuple[float, dict[str, str]]:
        return time.monotonic() + 1.0, {}

    def monitor(self, should_stop: Callable[[], bool]) -> TakeResult:
        t0 = time.monotonic()
        aborted = sleep_unless(self.duration, should_stop)
        return TakeResult(
            completed=[r.id for r in self.robots] if not aborted else [],
            aborted=aborted,
            duration=time.monotonic() - t0,
        )

    fade_s = 0.0

    def abort(self, fade_s: float = 0.0) -> dict[str, str]:
        return {}

    def disarm(self) -> None:
        pass

    def hard_stop(self) -> dict[str, bool]:
        return {}

    def reopen(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def volume_touched(self) -> bool:
        return False

    def set_volume(self, value: int) -> int:
        self._volume = max(0, min(100, int(value)))
        return self._volume

    def robot_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.id,
                "role": r.role,
                "instrument": r.instrument or r.role,
                "part": "",
                "loaded": True,
                "live": False,
                "audio": "",
                "error": "",
            }
            for r in self.robots
        ]


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class ShowRunner:
    """One show, one worker thread, four commands.

    Every public method is safe to call from the event loop and returns at
    once. Nothing here blocks on a robot.
    """

    def __init__(self, backend, duration: float, lead_s: float = 3.0):
        self.backend = backend
        self.duration = float(duration)
        self.lead_s = float(lead_s)

        self._lock = threading.Lock()
        self._state = State.OFFLINE
        self._detail = ""
        self._message = ""
        self._error = ""
        self._loop = False
        self._take = 0
        self._takes_done = 0
        self._t0: float | None = None
        self._gap_until: float | None = None
        self._last: TakeResult | None = None
        self._sync_note = ""
        self._opened = False
        self._panic = False
        """Latched by the emergency stop.

        Without it, the worker thread -- which is sitting inside `monitor` when
        the button is pressed -- would run its normal stop path and leave the
        screen reading `ready` seconds after an emergency stop. The one state
        nobody may be shown is a calm one that is not true."""

        self._play = threading.Event()
        self._stop = threading.Event()
        self._quit = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers: list[Callable[[dict], None]] = []

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._worker, name="show-runner", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 20.0) -> None:
        self._quit.set()
        self._stop.set()
        self._play.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.backend.close()

    # -- commands --------------------------------------------------------
    def play(self) -> dict[str, Any]:
        with self._lock:
            busy = self._state in State.BUSY
        if busy:
            return self.snapshot()
        with self._lock:
            self._panic = False
        self._stop.clear()
        self._play.set()
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._play.clear()
        return self.snapshot()

    def set_loop(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self._loop = bool(on)
        # Deliberately not "stop now if turned off". Loop is a question about
        # what happens *after* this take, and answering it mid-take by cutting
        # the music would be a different button.
        self._publish()
        return self.snapshot()

    def set_volume(self, value: int) -> dict[str, Any]:
        self.backend.set_volume(int(value))
        self._publish()
        return self.snapshot()

    def estop(self) -> dict[str, Any]:
        """The panic button: motors off over HTTP, agents bypassed."""
        with self._lock:
            self._panic = True
        self._stop.set()
        self._play.clear()
        result = {}
        try:
            result = self.backend.hard_stop()
        except Exception as e:  # noqa: BLE001
            self._set(State.ERROR, error=f"hard stop failed: {e}")
            return self.snapshot()
        down = [rid for rid, ok_ in result.items() if not ok_]
        self._set(
            State.ERROR,
            message="EMERGENCY STOP -- motors disabled. "
                    "Check the robots, then press PLAY to re-arm.",
            error="; ".join(f"{rid}: hard stop failed" for rid in down),
        )
        return self.snapshot()

    # -- state -----------------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn) if fn in self._subscribers else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            t = None
            if self._t0 is not None:
                t = round(time.monotonic() - self._t0, 4)
            gap = None
            if self._gap_until is not None:
                gap = round(max(0.0, self._gap_until - time.monotonic()), 2)
            return {
                "mode": self.backend.mode,
                "state": self._state,
                "detail": self._detail,
                "message": self._message,
                "error": self._error,
                "loop": self._loop,
                "take": self._take,
                "takes_done": self._takes_done,
                "t": t,
                "duration": round(self.duration, 3),
                "lead_s": self.lead_s,
                "next_in": gap,
                "volume": self.backend.volume,
                "volume_source": self.backend.volume_source,
                "sync": self._sync_note,
                "last_take": self._last.as_dict() if self._last else None,
                "robots": self.backend.robot_rows(),
                "server_time": round(time.time(), 3),
            }

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set(
        self,
        state: str | None = None,
        detail: str | None = None,
        message: str | None = None,
        error: str | None = None,
        t0: float | None = None,
        clear_t0: bool = False,
        gap_until: float | None = None,
    ) -> None:
        with self._lock:
            if state is not None:
                self._state = state
            if detail is not None:
                self._detail = detail
            if message is not None:
                self._message = message
            if error is not None:
                self._error = error
            if t0 is not None:
                self._t0 = t0
            if clear_t0:
                self._t0 = None
            self._gap_until = gap_until
        self._publish()

    def _publish(self) -> None:
        snap = self.snapshot()
        for fn in list(self._subscribers):
            try:
                fn(snap)
            except Exception:
                # A subscriber that cannot be fed is a disconnected browser.
                # It is not a reason to disturb a performance.
                pass

    # -- the worker ------------------------------------------------------
    def _worker(self) -> None:
        while not self._quit.is_set():
            if not self._play.wait(0.25):
                continue
            self._play.clear()
            if self._quit.is_set():
                return
            try:
                self._takes()
            except BackendError as e:
                self._set(State.ERROR, error=str(e), clear_t0=True)
            except Exception as e:  # noqa: BLE001
                self._set(State.ERROR, error=f"{type(e).__name__}: {e}",
                          clear_t0=True)

    def _ensure_open(self) -> None:
        if self._opened and self.state != State.ERROR:
            return
        self._set(State.OPENING, detail="connecting to the agents", error="",
                  message="")
        if self._opened:
            # Already holding a fleet and a heartbeat. Rebuilding from scratch
            # here would start a second heartbeat thread against the same nine
            # robots; `reopen` reconnects the links the error dropped.
            self.backend.reopen()
        else:
            self.backend.open()
            self._opened = True
        if self.backend.duration > 0:
            self.duration = float(self.backend.duration)

    def _takes(self) -> None:
        self._ensure_open()
        interrupted = False
        try:
            while not self._quit.is_set():
                with self._lock:
                    self._take += 1
                    take = self._take
                    label = f"take {take}" if self._loop or take > 1 else ""

                self.backend.refresh()
                self._set(State.ARMING, detail=label or "arming",
                          message="", error="", clear_t0=True)
                arm_errors = self.backend.arm()
                if arm_errors:
                    self._set(error="; ".join(
                        f"{rid}: {msg}" for rid, msg in sorted(arm_errors.items())))
                if sleep_unless(ARM_SETTLE_S, self._stop.is_set):
                    interrupted = True
                    break

                self._set(State.SYNCING, detail=label or "syncing clocks")
                ok_, note = self.backend.sync()
                with self._lock:
                    self._sync_note = note
                if not ok_:
                    raise BackendError(
                        "clock sync failed on at least one robot -- refusing to "
                        "start. An unsynced robot would begin at an arbitrary "
                        "offset.")
                if self._stop.is_set():
                    interrupted = True
                    break

                t0, start_errors = self.backend.start()
                self._set(State.COUNTDOWN, detail=label, t0=t0,
                          error="; ".join(f"{rid}: {msg}"
                                          for rid, msg in sorted(start_errors.items())))
                if sleep_unless(max(0.0, t0 - time.monotonic()), self._stop.is_set):
                    interrupted = True
                    break

                self._set(State.PLAYING, detail=label)
                res = self.backend.monitor(self._stop.is_set)
                with self._lock:
                    self._last = res
                    # Only finished takes count. The screen shows this as
                    # "played N times", and a take somebody stopped eight bars
                    # in was not a performance.
                    if not res.aborted:
                        self._takes_done += 1
                if res.aborted and self._stop.is_set():
                    interrupted = True
                    break

                with self._lock:
                    looping = self._loop
                if not looping:
                    break
                until = time.monotonic() + LOOP_GAP_S
                self._set(State.GAP, detail=f"next take in {LOOP_GAP_S:g}s",
                          clear_t0=True, gap_until=until)
                if sleep_unless(LOOP_GAP_S, self._stop.is_set):
                    interrupted = True
                    break
        finally:
            self._finish(interrupted)

    def _after_panic(self) -> None:
        # The emergency stop has already said what state this is in, and the
        # way out must not replace that with a calmer one -- not even
        # `stopping`. Motors are off; all that is left is to let go.
        self.backend.disarm()
        self._stop.clear()
        with self._lock:
            self._take = 0
        self._set(detail="motors disabled")

    def _finish(self, interrupted: bool) -> None:
        with self._lock:
            panic = self._panic
            fade_s = self.backend.fade_s if self._state in State.FADES else 0.0
        if panic:
            self._after_panic()
            return

        leaving = "fading out" if fade_s else "returning to neutral"
        self._set(State.STOPPING, detail=leaving if interrupted else "parking",
                  clear_t0=True, gap_until=None)
        if interrupted:
            # Through the agents. Each ends in a pose it is safe to let go of,
            # and only then cuts torque, so nothing is left parked against its
            # own travel limit. Disarming early would cut the fade off.
            errs = self.backend.abort(fade_s)
            if errs:
                self._set(error="; ".join(
                    f"{rid}: {msg}" for rid, msg in sorted(errs.items())))
            if sleep_unless(fade_s + 1.0, lambda: self._panic):
                self._after_panic()
                return
        # Either way, let go of the motors and the robots' audio device. An
        # agent that stays connected keeps /dev/snd, and a held ALSA device
        # stops the daemon's own media server from starting next time.
        self.backend.disarm()
        self._stop.clear()
        with self._lock:
            self._take = 0
        self._set(
            State.READY,
            detail="",
            message="stopped -- the next play starts from the top"
                    if interrupted else "")
