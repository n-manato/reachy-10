"""A robot dying mid-show must not wedge the PC.

This is a regression test for a self-deadlock that froze an entire
performance. `AgentLink.call()` drops the link from inside its own
`with self._lock` block; `_lock` is a plain, non-reentrant `Lock`, so routing
that through `close()` -- which takes the same lock -- made the thread wait
forever for a lock it already held.

Nothing broke it out, because a lock acquire has no timeout. The observed
failure: the main loop stopped inside `Show.monitor()`, the heartbeat thread
stopped with it, all nine agents then hit their deadman and parked, and the PC
sat holding every socket in CLOSE_WAIT until it was killed by hand.

The trigger is the single most likely hardware event -- one robot losing
power -- so this path has to stay non-blocking.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from orchestra.fleet.link import AgentLink, AgentUnreachable, Heartbeat
from orchestra.show.run import Show
from orchestra.web.runner import FleetBackend

TIMEOUT_S = 5.0


class _VanishedPeer:
    """The robot lost power: the socket errors instead of answering."""

    def __init__(self, errno: int = 10053):
        self.errno = errno
        self.closed = False

    def sendall(self, data):
        raise OSError(self.errno, "connection aborted")

    def readline(self):
        raise OSError(self.errno, "connection aborted")

    def close(self):
        self.closed = True


def _call_in_thread(link: AgentLink, op: str = "status"):
    """Run link.call() with a hard deadline. Returns the exception it raised."""
    box: dict = {}

    def go():
        try:
            link.call(op)
        except BaseException as e:  # noqa: BLE001 - the point is what escapes
            box["exc"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout=TIMEOUT_S)
    if t.is_alive():
        pytest.fail(
            f"call({op!r}) never returned within {TIMEOUT_S}s -- the "
            "non-reentrant _lock is being taken twice again"
        )
    return box.get("exc")


def test_call_on_a_dead_peer_raises_instead_of_deadlocking():
    link = AgentLink("r01", "192.168.3.105")
    link._sock = _VanishedPeer()
    link._rfile = _VanishedPeer()

    exc = _call_in_thread(link)

    assert isinstance(exc, AgentUnreachable), f"got {exc!r}"
    assert "r01" in str(exc)


def test_the_link_is_left_usable_after_the_failure():
    """The lock must be released and the socket dropped, or the next take hangs."""
    link = AgentLink("r01", "192.168.3.105")
    link._sock = _VanishedPeer()
    link._rfile = _VanishedPeer()

    _call_in_thread(link)

    assert link._lock.acquire(blocking=False), "_lock was never released"
    link._lock.release()
    assert link._sock is None
    assert link._rfile is None
    assert not link.connected


def test_close_is_still_safe_on_its_own():
    """close() is called from outside the lock too -- that path must keep working."""
    link = AgentLink("r02", "192.168.3.120")
    sock, rfile = _VanishedPeer(), _VanishedPeer()
    link._sock, link._rfile = sock, rfile

    link.close()

    assert sock.closed and rfile.closed
    assert link._sock is None and link._rfile is None
    link.close()  # idempotent


def test_repeated_failures_do_not_accumulate_lock_state():
    """monitor() polls every 500 ms; a wedged robot gets hit many times."""
    link = AgentLink("r03", "192.168.3.123")
    for _ in range(5):
        link._sock = _VanishedPeer()
        link._rfile = _VanishedPeer()
        exc = _call_in_thread(link)
        assert isinstance(exc, AgentUnreachable)
    assert link._lock.acquire(blocking=False)
    link._lock.release()


# ---------------------------------------------------------------------------
# One robot lost must cost one instrument
# ---------------------------------------------------------------------------


class _Stalled:
    """The robot lost power mid-reply: the call just hangs until the timeout."""

    robot_id = "r01"

    def __init__(self):
        self.calls = 0
        self.release = threading.Event()

    def status(self):
        self.calls += 1
        self.release.wait(TIMEOUT_S)
        raise AgentUnreachable("r01: timed out")


class _Fine:
    def __init__(self, robot_id):
        self.robot_id = robot_id
        self.calls = 0

    def status(self):
        self.calls += 1
        return {"state": "playing"}


def _settle(check, timeout=TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.005)
    return False


def test_a_stalled_robot_does_not_starve_the_others():
    """The agents park after 5 s without contact, and a stalled call lasts
    longer than that. r01 going quiet once parked the other eight."""
    stalled, fine = _Stalled(), _Fine("r02")
    hb = Heartbeat(SimpleNamespace(links=[stalled, fine]), interval=0.01)
    hb.start()
    try:
        assert _settle(lambda: fine.calls >= 10), "r02 stopped being fed"
    finally:
        hb.stop()
        stalled.release.set()


def test_a_stalled_robot_does_not_collect_a_pile_of_threads():
    stalled = _Stalled()
    hb = Heartbeat(SimpleNamespace(links=[stalled]), interval=0.01)
    hb.start()
    try:
        assert _settle(lambda: stalled.calls >= 1)
        time.sleep(0.1)
        assert stalled.calls == 1, "a new call stacked on the one still waiting"
    finally:
        hb.stop()
        stalled.release.set()
    assert _settle(lambda: hb.failures.get("r01") == 1)


def test_heartbeat_only_reports_what_it_heard_after_the_time_asked():
    fine = _Fine("r02")
    hb = Heartbeat(SimpleNamespace(links=[fine]), interval=0.01)
    hb.start()
    try:
        assert _settle(lambda: "r02" in hb.seen)
    finally:
        hb.stop()
    seen = hb.seen["r02"]
    assert hb.status_since("r02", seen) == {"state": "playing"}
    assert hb.status_since("r02", seen + 1.0) is None, "an old answer is not news"
    assert hb.status_since("r09", 0.0) is None


def _show(links, heartbeat):
    show = Show.__new__(Show)
    show.fleet = SimpleNamespace(links=links)
    show.heartbeat = heartbeat
    return show


def test_monitor_answers_stop_while_a_robot_is_gone():
    """A stop button has to answer in well under a second."""
    stalled = _Stalled()
    hb = Heartbeat(SimpleNamespace(links=[stalled]), interval=0.01)
    hb.start()
    try:
        show = _show([stalled], hb)
        t0 = time.monotonic()
        res = show.monitor(60.0, poll=0.01, should_stop=lambda: time.monotonic() - t0 > 0.1)
        assert res.aborted
        assert time.monotonic() - t0 < 1.0
    finally:
        hb.stop()
        stalled.release.set()


def test_monitor_drops_a_robot_the_heartbeat_has_given_up_on():
    gone, fine = _Fine("r01"), _Fine("r02")
    hb = Heartbeat(SimpleNamespace(links=[]), interval=0.01)
    hb.failures["r01"] = 3
    started = threading.Timer(0.05, lambda: (
        hb.last_status.__setitem__("r02", {"state": "done", "stats": {"late": 0}}),
        hb.seen.__setitem__("r02", time.monotonic())))
    started.start()

    res = _show([gone, fine], hb).monitor(5.0, poll=0.01)

    assert res.dropped == ["r01"]
    assert res.completed == ["r02"]
    assert gone.calls == fine.calls == 0, "monitor must not ask them itself"


def test_monitor_ignores_a_status_left_over_from_the_last_take():
    """どの機体も前のテイクの `done` を覚えている。真に受けると即終了してしまう。"""
    fine = _Fine("r02")
    hb = Heartbeat(SimpleNamespace(links=[]), interval=0.01)
    hb.last_status["r02"] = {"state": "done", "stats": {}}
    hb.seen["r02"] = time.monotonic() - 10.0

    t0 = time.monotonic()
    res = _show([fine], hb).monitor(
        60.0, poll=0.01, should_stop=lambda: time.monotonic() - t0 > 0.1)
    assert res.aborted and res.completed == []


def test_monitor_without_a_heartbeat_still_asks_the_robot():
    fine = _Fine("r02")
    fine.status = lambda: {"state": "done", "stats": {}}
    res = _show([fine], None).monitor(5.0, poll=0.01)
    assert res.completed == ["r02"]


def test_emergency_stop_does_not_wait_for_a_robot_that_is_gone():
    release = threading.Event()
    called = []

    def hard_stop():
        called.append(time.monotonic())
        return {"r02": True}

    backend = FleetBackend.__new__(FleetBackend)
    backend.show = SimpleNamespace(
        fleet=SimpleNamespace(abort=lambda: release.wait(TIMEOUT_S)),
        hard_stop=hard_stop)

    t0 = time.monotonic()
    assert backend.hard_stop() == {"r02": True}
    assert called[0] - t0 < 0.5, "the motors waited for the stalled agent"
    release.set()
