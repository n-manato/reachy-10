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

import pytest

from orchestra.fleet.link import AgentLink, AgentUnreachable

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
