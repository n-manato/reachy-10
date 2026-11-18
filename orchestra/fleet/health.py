"""HTTP client for the Reachy Mini daemon REST API.

Every endpoint used here was confirmed against the live fleet's own
``/openapi.json`` (FastAPI/uvicorn on port 8000). Nothing is guessed.

This is the *out-of-band* control plane: arming, health polling, thermal
watchdog and -- importantly -- the hard emergency stop. It deliberately does not
carry motion, because the daemon's REST layer offers no timing guarantees.
Motion goes through the on-robot agent.

Note there is no authentication anywhere on this API (the daemon binds 0.0.0.0
with CORS ``*``), so these calls need no credentials. That is fine on an isolated
SSID and worth knowing generally.

**MEASURED NOTE -- no thermal telemetry.** On daemon 1.9.0 this fleet's
``GET /api/motors/status`` returns exactly ``{"mode": "disabled"}``. There is no
temperature, current or load field anywhere in the API. ``_extract_temperatures``
is kept because it costs nothing and would light up on a firmware that adds it,
but *do not design around it*: motor protection comes from the compile-time
duty-cycle and velocity limits in :mod:`orchestra.motion.validate`, not from a
runtime thermal watchdog.

What ``GET /api/daemon/status`` *does* give, and what we use instead:
``backend_status.motor_control_mode`` (survives the 503 that
``/api/motors/status`` returns while the backend is down) and
``backend_status.control_loop_stats`` with ``mean_control_loop_frequency``,
``max_control_loop_interval`` and ``nb_error`` -- a genuine health signal, since
a loop falling below ~49 Hz or accumulating errors means the robot is struggling.
"""

from __future__ import annotations

import concurrent.futures as cf
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

DEFAULT_TIMEOUT = 5.0


class DaemonError(RuntimeError):
    pass


@dataclass
class RobotHealth:
    """A snapshot of one robot, as reported by its own daemon."""

    ip: str
    reachable: bool = False
    state: str = "unknown"
    """not_initialized | starting | running | stopping | stopped | error"""
    robot_name: str = ""
    hardware_id: str = ""
    version: str = ""
    wireless: bool | None = None
    motors_mode: str = "unknown"
    motors_ok: bool = False
    backend_ready: bool = False
    control_loop_hz: float | None = None
    max_loop_interval: float | None = None
    loop_errors: int = 0
    app_lock: str = ""
    temperatures: dict[str, float] = field(default_factory=dict)
    """Empty on this firmware -- see MEASURED NOTE in the module docstring."""
    error: str = ""
    raw_status: dict[str, Any] = field(default_factory=dict)
    raw_motors: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.reachable and self.state == "running"

    @property
    def armed(self) -> bool:
        return self.healthy and self.motors_mode == "enabled"

    @property
    def max_temp(self) -> float | None:
        return max(self.temperatures.values()) if self.temperatures else None

    def summary(self) -> str:
        if not self.reachable:
            return f"UNREACHABLE ({self.error})"
        bits = [self.state]
        if self.motors_mode != "unknown":
            bits.append(f"motors={self.motors_mode}")
        if self.control_loop_hz:
            bits.append(f"{self.control_loop_hz:.1f}Hz")
        if (t := self.max_temp) is not None:
            bits.append(f"{t:.0f}C")
        return " ".join(bits)


class DaemonClient:
    """Thin, explicit wrapper around one robot's daemon."""

    def __init__(self, ip: str, port: int = 8000, timeout: float = DEFAULT_TIMEOUT):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.base = f"http://{ip}:{port}"
        self._session = requests.Session()
        # The robots are on the same LAN. On Windows `requests` picks the
        # WinINET proxy out of the registry, and a campus proxy cannot route to
        # 192.168.3.0/24 -- every daemon then reports `unknown` as if it were
        # down. Nothing on this session should ever go through a proxy.
        self._session.trust_env = False

    # -- primitives ------------------------------------------------------
    def _get(self, path: str, timeout: float | None = None) -> Any:
        r = self._session.get(self.base + path, timeout=timeout or self.timeout)
        r.raise_for_status()
        return r.json() if r.content else None

    def _post(self, path: str, timeout: float | None = None, **kw) -> Any:
        r = self._session.post(self.base + path, timeout=timeout or self.timeout, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return self._get("/api/daemon/status")

    def hardware_id(self) -> str:
        v = self._get("/api/daemon/hardware-id")
        if isinstance(v, dict):
            for k in ("hardware_id", "id", "uid"):
                if k in v:
                    return str(v[k])
            return str(next(iter(v.values()), ""))
        return str(v)

    def motors_status(self) -> dict[str, Any]:
        return self._get("/api/motors/status")

    def app_lock_status(self) -> dict[str, Any]:
        return self._get("/api/daemon/robot-app-lock-status")

    def state_full(self) -> dict[str, Any]:
        return self._get("/api/state/full")

    def health(self) -> RobotHealth:
        """One consolidated snapshot. Never raises -- failures land in `error`."""
        h = RobotHealth(ip=self.ip)
        try:
            st = self.status()
        except Exception as e:
            h.error = type(e).__name__
            return h

        h.reachable = True
        h.raw_status = st
        h.state = str(st.get("state", st.get("status", "unknown")))
        h.robot_name = str(st.get("robot_name", ""))
        h.version = str(st.get("version", st.get("daemon_version", "")))
        wv = st.get("wireless_version")
        h.wireless = bool(wv) if wv is not None else None
        for key in ("hardware_id", "hardwareId", "unit_id"):
            if st.get(key):
                h.hardware_id = str(st[key])
                break
        if not h.hardware_id:
            try:
                h.hardware_id = self.hardware_id()
            except Exception:
                pass

        # Prefer /api/daemon/status for motor mode and loop stats: it stays
        # available when /api/motors/status returns 503 (which it does while the
        # backend is rebuilding, and permanently on a robot in `error` state).
        backend = st.get("backend_status") or {}
        if isinstance(backend, dict):
            h.backend_ready = bool(backend.get("ready", False))
            if backend.get("motor_control_mode"):
                h.motors_mode = str(backend["motor_control_mode"])
            stats = backend.get("control_loop_stats") or {}
            if isinstance(stats, dict):
                f = stats.get("mean_control_loop_frequency")
                if isinstance(f, (int, float)):
                    h.control_loop_hz = float(f)
                mi = stats.get("max_control_loop_interval")
                if isinstance(mi, (int, float)):
                    h.max_loop_interval = float(mi)
                h.loop_errors = int(stats.get("nb_error", 0) or 0)

        try:
            m = self.motors_status()
            h.raw_motors = m if isinstance(m, dict) else {"value": m}
            h.motors_ok = True
            mode = _extract_motor_mode(h.raw_motors)
            if mode != "unknown":
                h.motors_mode = mode
            h.temperatures = _extract_temperatures(h.raw_motors)
        except Exception:
            # 503 while the backend rebuilds is normal right after a restart, and
            # permanent on a robot whose motor backend failed to come up. The
            # mode from daemon status above is still valid, so do not clobber it.
            h.motors_ok = False

        try:
            lock = self.app_lock_status()
            h.app_lock = str(lock.get("state", "")) if isinstance(lock, dict) else str(lock)
        except Exception:
            pass

        return h

    # -- control ---------------------------------------------------------
    def set_motor_mode(self, mode: str) -> Any:
        """mode in {enabled, disabled, gravity_compensation}."""
        if mode not in ("enabled", "disabled", "gravity_compensation"):
            raise ValueError(f"invalid motor mode {mode!r}")
        return self._post(f"/api/motors/set_mode/{mode}")

    def enable_motors(self) -> Any:
        return self.set_motor_mode("enabled")

    def disable_motors(self) -> Any:
        """The hard stop. Works even if our agent is wedged, because it goes to
        the daemon rather than through the agent."""
        return self.set_motor_mode("disabled")

    def wake_up(self) -> Any:
        return self._post("/api/move/play/wake_up", timeout=20.0)

    def goto_sleep(self) -> Any:
        return self._post("/api/move/play/goto_sleep", timeout=20.0)

    def restart_daemon(self) -> Any:
        """Prefer `systemctl restart` over SSH for recovery.

        The project LEARNINGS.md is explicit that the API stop/start pair
        corrupts WebRTC signalling state when driving robots remotely. We do not
        use WebRTC (the agent uses the local media backend), but `restart` is
        still the gentler of the API options and stop/start is avoided entirely.
        """
        return self._post("/api/daemon/restart", timeout=30.0)

    def stop_current_app(self) -> Any:
        return self._post("/api/apps/stop-current-app", timeout=30.0)

    # -- media -----------------------------------------------------------
    def list_sounds(self) -> list[str]:
        v = self._get("/api/media/sounds")
        if isinstance(v, dict):
            v = v.get("sounds", v.get("files", []))
        return [str(x) for x in (v or [])]

    def upload_sound(self, path: str | Path) -> Any:
        """Push a WAV to the robot's own sound library.

        A supported alternative to SCP for audio stems, and it works without SSH
        credentials.
        """
        path = Path(path)
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "audio/wav")}
            return self._post("/api/media/sounds/upload", timeout=120.0, files=files)

    def delete_sound(self, filename: str) -> Any:
        r = self._session.delete(
            f"{self.base}/api/media/sounds/{filename}", timeout=self.timeout
        )
        r.raise_for_status()
        return r.json() if r.content else None

    def get_volume(self) -> int:
        """Read the speaker volume, 0-100. Silent -- safe at any time."""
        return int(self._get("/api/volume/current").get("volume"))

    def set_volume(self, value: int) -> Any:
        """Set the speaker volume, 0-100.

        ⚠ **This plays `impatient1.wav` every single time.** The daemon route
        is "set the volume and play a test sound"; there is no way to ask for
        one without the other. Fine when you are deliberately checking a
        speaker by ear, wrong during a performance -- use `AgentLink.volume`
        there, which rides the SDK session and makes no sound.

        (The path was `/api/volume/` until 2026-08-10, which is not a route
        this daemon serves. Nothing called it, so the 404 never surfaced.)
        """
        return self._post("/api/volume/set", json={"volume": int(value)})

    def close(self) -> None:
        self._session.close()


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------
#
# The exact shape of /api/motors/status is experiment N-6 -- it is read here
# defensively so a schema difference degrades to "no temperature reading"
# rather than crashing the show.


def _walk(obj: Any, want: Callable[[str], bool]) -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and want(k):
                yield k, v
            yield from _walk(v, want)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, want)


def _extract_motor_mode(raw: dict[str, Any]) -> str:
    for key in ("mode", "control_mode", "motor_mode", "state"):
        v = raw.get(key)
        if isinstance(v, str):
            return v
    for _, v in _walk(raw, lambda k: k in ("mode", "control_mode", "motor_mode")):
        if isinstance(v, str):
            return v
    if "torque_enabled" in raw:
        return "enabled" if raw["torque_enabled"] else "disabled"
    return "unknown"


def _extract_loop_hz(raw: dict[str, Any]) -> float | None:
    for _, v in _walk(raw, lambda k: "freq" in k.lower() or k.lower().endswith("hz")):
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _extract_temperatures(raw: dict[str, Any]) -> dict[str, float]:
    """Best-effort temperature harvest, keyed by motor name where possible."""
    out: dict[str, float] = {}

    # Shape A: {"motors": {"stewart_1": {"temperature": 42}, ...}}
    motors = raw.get("motors")
    if isinstance(motors, dict):
        for name, m in motors.items():
            if isinstance(m, dict):
                for k in ("temperature", "temp", "temperature_c"):
                    if isinstance(m.get(k), (int, float)):
                        out[str(name)] = float(m[k])
                        break
    # Shape B: [{"name": "stewart_1", "temperature": 42}, ...]
    elif isinstance(motors, list):
        for m in motors:
            if isinstance(m, dict):
                name = str(m.get("name", m.get("id", len(out))))
                for k in ("temperature", "temp", "temperature_c"):
                    if isinstance(m.get(k), (int, float)):
                        out[name] = float(m[k])
                        break

    if not out:
        # Shape C: anything with "temp" in the key, anywhere.
        for i, (k, v) in enumerate(_walk(raw, lambda k: "temp" in k.lower())):
            if isinstance(v, (int, float)):
                out[f"{k}[{i}]"] = float(v)
            elif isinstance(v, list) and all(isinstance(x, (int, float)) for x in v):
                for j, x in enumerate(v):
                    out[f"{k}[{j}]"] = float(x)
    return out


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def parallel(
    items: Sequence[Any], fn: Callable[[Any], Any], workers: int | None = None
) -> list[tuple[Any, Any, Exception | None]]:
    """Run `fn` over `items` concurrently, preserving input order.

    Returns (item, result, error) triples -- never raises, because one bad robot
    must not take down a fleet-wide operation.
    """
    if not items:
        return []
    workers = workers or min(16, len(items))
    out: list[tuple[Any, Any, Exception | None]] = [(i, None, None) for i in items]
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, it): idx for idx, it in enumerate(items)}
        for fut in cf.as_completed(futures):
            idx = futures[fut]
            try:
                out[idx] = (items[idx], fut.result(), None)
            except Exception as e:  # noqa: BLE001 - deliberately swallowed
                out[idx] = (items[idx], None, e)
    return out


def fleet_health(
    ips: Sequence[str], port: int = 8000, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, RobotHealth]:
    def probe(ip: str) -> RobotHealth:
        c = DaemonClient(ip, port, timeout)
        try:
            return c.health()
        finally:
            c.close()

    results = parallel(list(ips), probe)
    out: dict[str, RobotHealth] = {}
    for ip, res, err in results:
        out[ip] = res if res is not None else RobotHealth(ip=ip, error=str(err))
    return out


def emergency_stop(ips: Sequence[str], port: int = 8000) -> dict[str, bool]:
    """Disable motors on every robot, in parallel. The hard abort.

    Uses a short timeout and reports per-robot success: during an abort you want
    to know immediately which robot did *not* respond, not wait on it.
    """
    def kill(ip: str) -> bool:
        c = DaemonClient(ip, port, timeout=3.0)
        try:
            c.disable_motors()
            return True
        finally:
            c.close()

    return {ip: (err is None and res is True) for ip, res, err in parallel(list(ips), kill)}
