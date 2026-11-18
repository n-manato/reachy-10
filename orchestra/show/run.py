"""Running a performance: arm, sync, start, monitor, stop.

The sequence exists to make the moment of truth boring. By the time START is
sent, every robot already holds its whole program, is already at its start pose,
and already knows the offset between its clock and the PC's. START carries an
absolute instant a few seconds in the future, so its own delivery jitter never
reaches the music.

Two abort paths, because one is not enough:

* **Graceful** goes through the agents. Each ramps back to neutral over ~0.5 s
  and then cuts torque. It never freezes mid-strike -- a servo parked against an
  instrument is the exact condition that stalls and heats it.
* **Hard** goes straight to each daemon over HTTP and disables motors. It works
  even when an agent is wedged, because it does not involve the agent at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..clock.sync import ClockOffset
from ..config import Context, RobotConfig
from ..fleet.health import DaemonClient, emergency_stop, fleet_health, parallel
from ..fleet.link import AgentLink, Fleet, Heartbeat


@dataclass
class PreflightReport:
    ok: bool = True
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))
        if not passed:
            self.ok = False

    def summary(self) -> str:
        lines = []
        for name, passed, detail in self.checks:
            mark = "ok  " if passed else "FAIL"
            lines.append(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))
        lines.append(f"  => {'READY' if self.ok else 'NOT READY'}")
        return "\n".join(lines)


def sleep_unless(
    seconds: float,
    should_stop: Callable[[], bool] | None,
    slice_s: float = 0.05,
) -> bool:
    """Sleep, but answer `should_stop` quickly. True means it fired."""
    if should_stop is None:
        time.sleep(seconds)
        return False
    end = time.monotonic() + seconds
    while True:
        if should_stop():
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(slice_s, left))


def preflight(ctx: Context, robots: list[RobotConfig]) -> PreflightReport:
    """Everything that must be true before arming. Read-only."""
    rep = PreflightReport()
    ips = [r.ip for r in robots]
    health = fleet_health(ips, ctx.fleet.daemon_port)

    min_hz = ctx.fleet.safety.min_control_loop_hz
    for r in robots:
        h = health[r.ip]
        if not h.reachable:
            rep.add(f"{r.id} daemon reachable", False, h.error or "no response")
            continue
        if h.state != "running":
            rep.add(
                f"{r.id} daemon running", False,
                f"state={h.state}. Recover with `orchestra fleet recover --robot {r.id}`",
            )
            continue
        rep.add(f"{r.id} daemon running", True, f"v{h.version}")

        if h.control_loop_hz is not None and h.control_loop_hz < min_hz:
            rep.add(
                f"{r.id} control loop", False,
                f"{h.control_loop_hz:.1f} Hz is below the {min_hz:g} Hz floor "
                "-- this robot is struggling and will drift",
            )
        if h.app_lock and h.app_lock not in ("free", ""):
            rep.add(
                f"{r.id} app lock", False,
                f"held by {h.app_lock}. Stop the running app first "
                "(`orchestra fleet stop-app`), or it will fight us for control.",
            )

    from .deploy import agent_running

    for r in robots:
        running = agent_running(ctx, r)
        rep.add(
            f"{r.id} agent listening", running,
            "" if running else f"start it with `orchestra fleet agent start --robot {r.id}`",
        )
    return rep


def make_fleet(ctx: Context, robots: list[RobotConfig]) -> Fleet:
    return Fleet(
        [
            AgentLink(r.id, r.ip, ctx.fleet.agent_port, timeout=10.0)
            for r in robots
        ]
    )


@dataclass
class SyncSummary:
    offsets: dict[str, ClockOffset] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.offsets) and all(o.ok for o in self.offsets.values())

    @property
    def worst_uncertainty_ms(self) -> float:
        vals = [o.uncertainty_ms for o in self.offsets.values() if o.ok]
        return max(vals) if vals else float("inf")

    def summary(self) -> str:
        lines = [f"clock sync across {len(self.offsets)} robot(s):"]
        for rid in sorted(self.offsets):
            lines.append("  " + self.offsets[rid].summary())
        if self.ok:
            lines.append(
                f"  worst-case uncertainty {self.worst_uncertainty_ms:.2f} ms "
                "(percussion reads as simultaneous below ~10-20 ms)"
            )
        return "\n".join(lines)


def sync_clocks(ctx: Context, fleet: Fleet) -> SyncSummary:
    cs = ctx.fleet.clock_sync
    offsets = fleet.sync_clocks(
        probes=int(cs.get("probes", 60)),
        keep_best=int(cs.get("keep_best", 8)),
        probe_interval=float(cs.get("probe_interval_s", 0.02)),
        max_acceptable_rtt=float(cs.get("max_acceptable_rtt_ms", 60.0)) / 1000.0,
    )
    return SyncSummary(offsets=offsets)


@dataclass
class RunResult:
    started_at: float = 0.0
    duration: float = 0.0
    completed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    stats: dict[str, dict] = field(default_factory=dict)
    aborted: bool = False

    def summary(self) -> str:
        lines = [
            f"run {'ABORTED' if self.aborted else 'finished'} "
            f"after {self.duration:.1f}s",
            f"  completed: {len(self.completed)}  "
            f"dropped: {len(self.dropped)}  failed: {len(self.failed)}",
        ]
        for rid in sorted(self.stats):
            s = self.stats[rid]
            lines.append(
                f"    {rid:<5} start_err {s.get('start_error_ms', '?'):>8} ms  "
                f"frames {s.get('frames_sent', '?')}/{s.get('frames_expected', '?')}  "
                f"late {s.get('late_ticks', '?')} (worst {s.get('worst_late_ms', '?')} ms)"
            )
        for rid, err in self.failed.items():
            lines.append(f"    {rid}: FAILED -- {err}")
        for rid in self.dropped:
            lines.append(
                f"    {rid}: dropped mid-show (its part went silent; the rest "
                "continued)"
            )
        return "\n".join(lines)


class Show:
    """Owns a live performance from arm through teardown."""

    def __init__(
        self,
        ctx: Context,
        robots: list[RobotConfig],
        program_name: str = "{robot_id}.program.json.gz",
    ):
        self.ctx = ctx
        self.robots = robots
        self.program_name = program_name
        """Template resolved per robot, e.g. "{robot_id}.program.json.gz"."""
        self.fleet = make_fleet(ctx, robots)
        self.heartbeat: Heartbeat | None = None
        self.sync: SyncSummary | None = None
        self._by_id = {r.id: r for r in robots}

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        for rid, (_, err) in self.fleet.connect().items():
            if err:
                errors[rid] = str(err)
        # Start the heartbeat as soon as we are connected: it keeps Wi-Fi out of
        # power-save, which is what stops START paying the measured 17-90 ms
        # wake penalty.
        self.heartbeat = Heartbeat(
            self.fleet, float(self.ctx.fleet.show.get("heartbeat_interval_s", 1.0))
        )
        self.heartbeat.start()
        return errors

    def program_for(self, robot_id: str) -> str:
        return self.program_name.format(robot_id=robot_id)

    def load(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        for rid, (_, err) in self.fleet.load(self.program_for).items():
            if err:
                errors[rid] = str(err)
        return errors

    def arm(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        for rid, (_, err) in self.fleet.arm().items():
            if err:
                errors[rid] = str(err)
        return errors

    def sync_clocks(self) -> SyncSummary:
        self.sync = sync_clocks(self.ctx, self.fleet)
        return self.sync

    def start(self, lead_s: float | None = None) -> tuple[float, dict[str, str]]:
        if self.sync is None or not self.sync.ok:
            raise RuntimeError(
                "refusing to start without a successful clock sync on every "
                "robot -- an unsynced robot would begin at an arbitrary offset. "
                "Run `orchestra show sync` and check the report."
            )
        lead = lead_s if lead_s is not None else float(
            self.ctx.fleet.show.get("start_lead_s", 3.0)
        )
        t0, results = self.fleet.start_together(lead_s=lead)
        errors = {rid: str(err) for rid, (_, err) in results.items() if err}
        return t0, errors

    def monitor(
        self,
        duration: float,
        poll: float = 0.5,
        should_stop: "Callable[[], bool] | None" = None,
    ) -> RunResult:
        """Watch until every robot finishes or the wall clock runs out.

        A robot that stops answering is recorded and otherwise ignored. Each
        agent is autonomous after START, so one dropping costs exactly one
        instrument part.

        `should_stop` lets an operator interrupt without waiting for the end of
        the piece -- a stop button has to answer in well under a second, while
        asking nine agents for status twice a second is the rate that was
        measured not to interfere with the show. So the two are decoupled: the
        wait between polls is sliced finely and only the *answer* is fast. The
        caller still has to actually stop the robots; returning here only hands
        control back.
        """
        res = RunResult(started_at=time.monotonic(), duration=0.0)
        deadline = time.monotonic() + duration + 5.0
        pending = {l.robot_id for l in self.fleet.links}

        while pending and time.monotonic() < deadline:
            if sleep_unless(poll, should_stop):
                res.aborted = True
                res.duration = time.monotonic() - res.started_at
                return res
            for link in list(self.fleet.links):
                if link.robot_id not in pending:
                    continue
                # With a heartbeat running, read what it heard rather than ask.
                # Asking a robot that has gone waits out the link timeout, and
                # for those seconds `should_stop` is not looked at.
                st = None
                if self.heartbeat:
                    st = self.heartbeat.status_since(link.robot_id, res.started_at)
                else:
                    try:
                        st = link.status()
                    except Exception:
                        pass
                if st is None:
                    if self.heartbeat and self.heartbeat.failures.get(link.robot_id, 0) >= 3:
                        res.dropped.append(link.robot_id)
                        pending.discard(link.robot_id)
                    continue

                state = st.get("state")
                if state in ("done", "aborted", "error"):
                    pending.discard(link.robot_id)
                    res.stats[link.robot_id] = st.get("stats", {})
                    if state == "done":
                        res.completed.append(link.robot_id)
                    elif state == "error":
                        res.failed[link.robot_id] = st.get("error", "")
                    else:
                        res.aborted = True

        for rid in pending:
            res.dropped.append(rid)
        res.duration = time.monotonic() - res.started_at
        return res

    # -- stopping --------------------------------------------------------
    def stop(self, fade_s: float = 0.0) -> dict[str, str]:
        """Graceful: agents ramp to neutral, or fade out over `fade_s`, then
        disable motors."""
        errors = {}
        for rid, (_, err) in self.fleet.abort(fade_s).items():
            if err:
                errors[rid] = str(err)
        return errors

    def hard_stop(self) -> dict[str, bool]:
        """The panic button.

        Straight to each daemon over HTTP -- deliberately NOT through the agents,
        so it still works when an agent is hung, deadlocked or dead.
        """
        return emergency_stop([r.ip for r in self.robots], self.ctx.fleet.daemon_port)

    def teardown(self, sleep: bool = False) -> None:
        """Cut torque. Never rely on the SDK's context manager -- its `__exit__`
        leaves motors energised at the last target (reachy_mini.py:181-186).

        ★`sleep` の既定を False に変えた（2026-08-08）★
        顔をしまうのは**演奏プログラムの中**でやるようになった。そちらのほうが
        良い理由が4つある:

          * 9台がフレーム同期で同時に寝る。`goto_sleep()` は HTTP を並列に
            叩くだけで、各台が 1秒 + 2秒 の goto を勝手に走らせるので揃わない
          * `goto_sleep()` は go_sleep.wav を鳴らす（reachy_mini.py:673）。
            9台がバラバラのタイミングで「ぷしゅー」と鳴る
          * プログラムに入っていれば `song build` の安全ゲート、`clip plot`、
            `show run --record` のすべてに乗る。HTTP 経由だと何にも映らない
          * ここは既に `disarm()` の後なので、トルクを切ってから寝かせる動作を
            投げることになっていた

        曲に `outro` が無いときや、走らせずに片付けたいときは
        `orchestra show park --sleep` を使う。
        """
        try:
            self.fleet.disarm()
        except Exception:
            pass

        def park(r: RobotConfig) -> None:
            c = DaemonClient(r.ip, self.ctx.fleet.daemon_port, timeout=20.0)
            try:
                if sleep:
                    c.goto_sleep()
                c.disable_motors()
            finally:
                c.close()

        parallel(self.robots, park)

        if self.heartbeat:
            self.heartbeat.stop()
        self.fleet.close()

    def __enter__(self) -> "Show":
        self.connect()
        return self

    def __exit__(self, exc_type, *_) -> None:
        if exc_type is not None:
            # Any exception escaping a live show is a reason to make the robots
            # safe first and ask questions afterwards.
            try:
                self.hard_stop()
            except Exception:
                pass
        if self.heartbeat:
            self.heartbeat.stop()
        self.fleet.close()


LOOP_GAP_S = 5.0
"""ループ再生で1回と次の回のあいだに置く間。

就寝姿勢から次の回の開始姿勢へ戻る `goto`（arm 内、2秒）が入るので、ここを 0 に
すると礼の直後に頭が持ち上がって不自然に見える。観客が「終わった」と分かる間を
取るための値で、安全上の意味は無い。

端末（`orchestra show run`）と画面（`orchestra web`）の両方が使う。同じ公演が
起動方法によって違う間合いになってはいけないので、定義はここ1か所。"""
