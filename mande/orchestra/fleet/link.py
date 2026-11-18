"""The PC's control channel to one on-robot agent.

Newline-delimited JSON over a persistent TCP connection. Persistent for two
reasons beyond the obvious: it doubles as the Wi-Fi keep-alive (the survey
measured a 17-90 ms power-save wake spike on the first packet after idle, which
would otherwise land squarely in the START path), and it gives the agent's
deadman a signal to watch.

This channel carries roughly one message per second during a show. It is a
control plane, not a motion path -- that distinction is the whole architecture.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..clock.sync import ClockOffset, DriftTracker, Sample, estimate


class AgentError(RuntimeError):
    """The agent replied with ok=false."""


class AgentUnreachable(RuntimeError):
    """We could not talk to the agent at all."""


@dataclass
class AgentInfo:
    agent_version: str = ""
    sdk_version: str = ""
    python: str = ""
    hostname: str = ""
    platform: str = ""
    pid: int = 0
    base_dir: str = ""


class AgentLink:
    """One persistent connection to one robot's agent."""

    def __init__(self, robot_id: str, host: str, port: int = 8770, timeout: float = 8.0):
        self.robot_id = robot_id
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._rfile = None
        self._lock = threading.Lock()
        self.info: AgentInfo | None = None
        self.drift = DriftTracker()
        self.last_error = ""

    # -- connection ------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> AgentInfo:
        if self._sock is not None:
            return self.info or AgentInfo()
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise AgentUnreachable(
                f"{self.robot_id} ({self.host}:{self.port}): {e}. "
                "Is the agent running? `orchestra fleet agent start`"
            ) from e
        # Nagle would coalesce our tiny control messages and add tens of ms of
        # latency to a clock-sync probe -- exactly the wrong thing to do here.
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(self.timeout)
        self._sock = s
        self._rfile = s.makefile("rb")

        d = self.call("hello")
        self.info = AgentInfo(
            agent_version=str(d.get("agent_version", "")),
            sdk_version=str(d.get("sdk_version", "")),
            python=str(d.get("python", "")),
            hostname=str(d.get("hostname", "")),
            platform=str(d.get("platform", "")),
            pid=int(d.get("pid", 0)),
            base_dir=str(d.get("base_dir", "")),
        )
        return self.info

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Tear the socket down. **The caller must already hold `_lock`.**

        `call()` has to drop the link from inside its own `with self._lock`
        block, and `_lock` is a plain, non-reentrant `Lock`. Routing that
        through `close()` made the thread block on a lock it was already
        holding -- a permanent self-deadlock with no timeout to break it.

        It did not just lose one robot. The main loop stopped inside
        `monitor()`, the heartbeat stopped with it, every agent then hit its
        deadman and parked, and the PC sat there holding all nine sockets open
        in CLOSE_WAIT until it was killed. A robot dying mid-show is exactly
        the condition that triggers it, so the failure the fleet is most likely
        to hit was also the one that wedged the whole performance.
        """
        for obj in (self._rfile, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._rfile = None
        self._sock = None

    def __enter__(self) -> "AgentLink":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- rpc -------------------------------------------------------------
    def call(self, op: str, **kw) -> dict[str, Any]:
        if self._sock is None:
            self.connect()
        payload = json.dumps({"op": op, **kw}) + "\n"
        with self._lock:
            try:
                self._sock.sendall(payload.encode("utf-8"))
                line = self._rfile.readline()
            except OSError as e:
                # Already inside `with self._lock` -- must NOT go through
                # close(), which takes the same non-reentrant lock.
                self._close_locked()
                raise AgentUnreachable(f"{self.robot_id}: {op} failed: {e}") from e
        if not line:
            self.close()
            raise AgentUnreachable(f"{self.robot_id}: agent closed the connection")

        try:
            resp = json.loads(line.decode("utf-8"))
        except Exception as e:
            raise AgentError(f"{self.robot_id}: unparseable reply: {line!r}") from e

        if not resp.get("ok"):
            err = resp.get("error", "unknown error")
            self.last_error = err
            raise AgentError(f"{self.robot_id}: {op}: {err}")
        return resp

    # -- operations ------------------------------------------------------
    def load(self, program: str) -> dict[str, Any]:
        return self.call("load", program=program)

    def arm(self) -> dict[str, Any]:
        return self.call("arm")

    def disarm(self) -> dict[str, Any]:
        return self.call("disarm")

    def stop(self) -> dict[str, Any]:
        return self.call("stop")

    def abort(self) -> dict[str, Any]:
        return self.call("abort")

    def status(self) -> dict[str, Any]:
        return self.call("status")

    def shutdown(self) -> dict[str, Any]:
        return self.call("shutdown")

    def volume(self, value: int) -> dict[str, Any]:
        """Set the speaker volume, 0-100. Safe to call during a performance.

        Goes through the agent on purpose: the daemon's own `/api/volume/set`
        plays a test sound every time it is called. See `op_volume` in the
        agent for the full reasoning.
        """
        return self.call("volume", value=int(value))

    def heartbeat(self) -> bool:
        """Cheap liveness poke; also keeps Wi-Fi out of power-save."""
        try:
            self.status()
            return True
        except Exception:
            return False

    # -- clock -----------------------------------------------------------
    def sync_clock(
        self,
        probes: int = 60,
        keep_best: int = 8,
        probe_interval: float = 0.02,
        max_acceptable_rtt: float = 0.060,
    ) -> ClockOffset:
        """Run one Cristian round against this agent.

        The probe loop is intentionally plain and serial: any concurrency here
        would introduce queuing asymmetry, which is precisely the error term the
        estimator is trying to minimise.
        """
        samples: list[Sample] = []
        for i in range(probes):
            t0 = time.monotonic()
            try:
                r = self.call("ping", seq=i, t_pc=t0)
            except (AgentError, AgentUnreachable):
                break
            t3 = time.monotonic()

            t1 = float(r["t_recv"])
            t2 = float(r["t_send"])
            rtt = (t3 - t0) - (t2 - t1)
            if rtt < 0:
                # Only possible if a clock ticked backwards mid-probe; discard.
                continue
            offset = ((t1 - t0) + (t2 - t3)) / 2.0
            samples.append(Sample(seq=i, rtt=rtt, offset=offset))
            if probe_interval:
                time.sleep(probe_interval)

        res = estimate(
            samples,
            keep_best=keep_best,
            max_acceptable_rtt=max_acceptable_rtt,
            robot_id=self.robot_id,
        )
        if res.ok:
            self.drift.update(res)
        return res

    @property
    def offset(self) -> float:
        """Smoothed robot_mono - pc_mono, from every sync round so far."""
        return self.drift.offset if self.drift.offset is not None else 0.0

    def start_at(self, t0_pc: float) -> dict[str, Any]:
        """Schedule playback at a PC-monotonic instant.

        The PC converts into the robot's timebase here rather than sending a raw
        PC timestamp, so there is exactly one place where the offset is applied
        and the agent stays trivially simple.
        """
        if self.drift.offset is None:
            raise RuntimeError(
                f"{self.robot_id}: clock not synced -- call sync_clock() first. "
                "Starting without a measured offset would put this robot "
                "arbitrarily far from the rest."
            )
        t0_local = t0_pc + self.drift.offset
        return self.call("start", t0_local=t0_local, t0_pc=t0_pc)


class Fleet:
    """A collection of agent links, driven concurrently.

    Every fan-out method returns per-robot results and never raises on a single
    robot's failure: one robot dropping out must cost exactly one instrument
    part, not the show.
    """

    def __init__(self, links: list[AgentLink]):
        self.links = links

    @property
    def ids(self) -> list[str]:
        return [l.robot_id for l in self.links]

    def __iter__(self):
        return iter(self.links)

    def __len__(self) -> int:
        return len(self.links)

    def _fan(self, fn, workers: int | None = None) -> dict[str, tuple[Any, Exception | None]]:
        import concurrent.futures as cf

        out: dict[str, tuple[Any, Exception | None]] = {}
        if not self.links:
            return out
        with cf.ThreadPoolExecutor(max_workers=workers or len(self.links)) as pool:
            futs = {pool.submit(fn, l): l for l in self.links}
            for f in cf.as_completed(futs):
                link = futs[f]
                try:
                    out[link.robot_id] = (f.result(), None)
                except Exception as e:  # noqa: BLE001
                    out[link.robot_id] = (None, e)
        return out

    def connect(self):
        return self._fan(lambda l: l.connect())

    def load(self, program_for):
        """`program_for` maps robot_id -> program filename."""
        return self._fan(lambda l: l.load(program_for(l.robot_id)))

    def arm(self):
        return self._fan(lambda l: l.arm())

    def disarm(self):
        return self._fan(lambda l: l.disarm())

    def status(self):
        return self._fan(lambda l: l.status())

    def abort(self):
        return self._fan(lambda l: l.abort())

    def stop(self):
        return self._fan(lambda l: l.stop())

    def volume(self, value: int):
        """Set every robot to the same volume, concurrently.

        Concurrency is not an optimisation here. A unit without pulsectl shells
        out to `amixer`, which the daemon allows up to 2 s -- nine of those in
        series would freeze the caller for 18 s.
        """
        return self._fan(lambda l: l.volume(value))

    def sync_clocks(self, **kw) -> dict[str, ClockOffset]:
        res = self._fan(lambda l: l.sync_clock(**kw))
        out: dict[str, ClockOffset] = {}
        for rid, (val, err) in res.items():
            if val is not None:
                out[rid] = val
            else:
                out[rid] = ClockOffset(robot_id=rid, reason=str(err))
        return out

    def start_together(self, lead_s: float = 3.0) -> tuple[float, dict[str, tuple[Any, Exception | None]]]:
        """Schedule every robot for the same PC-monotonic instant.

        Fan-out order and transit jitter do not matter: each agent receives an
        absolute time in its own clock and waits for it. That is the entire
        reason START is sent seconds early rather than at the moment of truth.
        """
        t0 = time.monotonic() + lead_s
        return t0, self._fan(lambda l: l.start_at(t0))

    def close(self) -> None:
        for l in self.links:
            l.close()

    def __enter__(self) -> "Fleet":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Heartbeat:
    """Background 1 Hz poke at every agent.

    Two jobs: keep the Wi-Fi link awake so START never pays the power-save wake
    penalty, and feed the agents' deadman timers so they know the PC is alive.
    """

    def __init__(self, fleet: Fleet, interval: float = 1.0):
        self.fleet = fleet
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.failures: dict[str, int] = {}
        self.last_status: dict[str, dict] = {}

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            for link in self.fleet.links:
                try:
                    self.last_status[link.robot_id] = link.status()
                    self.failures[link.robot_id] = 0
                except Exception:
                    self.failures[link.robot_id] = self.failures.get(link.robot_id, 0) + 1
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "Heartbeat":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
