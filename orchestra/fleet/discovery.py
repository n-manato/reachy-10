"""Finding robots on the network and reconciling them against the registry.

Two mechanisms, in order of reliability for *this* fleet:

1. **Subnet sweep + hardware-id.** Probe every host on the subnet for an open
   daemon port, then ask each one for its ``hardware_id``. This is the primary
   method because hardware_id is the only stable identity we have.

2. **mDNS** (``_reachy-mini._tcp.local.``, utils/discovery.py:22). Kept as a
   convenience, but note that all nine units ship with the identical hostname
   ``reachy-mini`` and robot name ``reachy_mini``. The SDK anticipates the
   collision -- its resolver prefers the TXT ``address`` field over DNS
   precisely because of same-hostname Pis (discovery.py:345) -- but with nine
   units the results are still not something to key a show on.

The sweep is what `orchestra fleet discover` runs, and it updates the cached IPs
in robots.yaml in place, matching on hardware_id.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field

from ..config import RobotConfig, RobotRegistry
from .health import DaemonClient, parallel


@dataclass
class Found:
    ip: str
    hardware_id: str = ""
    robot_name: str = ""
    version: str = ""
    state: str = ""
    wireless: bool | None = None
    via: str = "sweep"

    @property
    def variant(self) -> str:
        return "wireless" if self.wireless else "lite"


def port_open(ip: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def sweep_subnet(
    subnet: str, port: int = 8000, timeout: float = 0.35, workers: int = 128
) -> list[str]:
    """Return the IPs in `subnet` with an open daemon port.

    Two passes: a cheap TCP connect over the whole range, then identity queries
    only against the hits. A /24 sweep takes a couple of seconds.
    """
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(h) for h in net.hosts()]
    results = parallel(hosts, lambda ip: port_open(ip, port, timeout), workers=workers)
    return [ip for ip, ok, err in results if err is None and ok]


def identify(ips: list[str], port: int = 8000, timeout: float = 4.0) -> list[Found]:
    """Ask each candidate who it is."""

    def probe(ip: str) -> Found:
        c = DaemonClient(ip, port, timeout=timeout)
        try:
            st = c.status()
            f = Found(
                ip=ip,
                robot_name=str(st.get("robot_name", "")),
                version=str(st.get("version", st.get("daemon_version", ""))),
                state=str(st.get("state", st.get("status", ""))),
            )
            wv = st.get("wireless_version")
            f.wireless = bool(wv) if wv is not None else None
            for key in ("hardware_id", "hardwareId", "unit_id"):
                if st.get(key):
                    f.hardware_id = str(st[key])
                    break
            if not f.hardware_id:
                # Separate endpoint on daemons that omit it from status.
                f.hardware_id = c.hardware_id()
            return f
        finally:
            c.close()

    out: list[Found] = []
    for ip, res, err in parallel(ips, probe):
        if res is not None:
            out.append(res)
        else:
            out.append(Found(ip=ip, state=f"error: {type(err).__name__}"))
    return out


def discover(subnet: str, port: int = 8000) -> list[Found]:
    return identify(sweep_subnet(subnet, port), port)


def discover_mdns(timeout: float = 5.0) -> list[Found]:
    """mDNS discovery via the SDK. Best-effort; see the module docstring."""
    try:
        from reachy_mini.utils.discovery import find_robots
    except ImportError:
        return []
    try:
        robots = find_robots(timeout=timeout)
    except Exception:
        return []

    out: list[Found] = []
    for r in robots:
        props = getattr(r, "properties", {}) or {}
        # Prefer the resolved address over the hostname: nine identically-named
        # Pis cannot all be reachy-mini.local.
        addrs = getattr(r, "addresses", None) or []
        ip = str(addrs[0]) if addrs else str(props.get("address", getattr(r, "host", "")))
        out.append(
            Found(
                ip=ip,
                hardware_id=str(props.get("unit_id", "")),
                robot_name=str(props.get("robot_name", getattr(r, "name", ""))),
                version=str(props.get("version", "")),
                wireless=(str(props.get("model", "")).endswith("Wireless") or None),
                via="mdns",
            )
        )
    return out


@dataclass
class Reconciliation:
    """What a discovery run means for the registry."""

    matched: list[tuple[RobotConfig, Found]] = field(default_factory=list)
    moved: list[tuple[RobotConfig, Found]] = field(default_factory=list)
    missing: list[RobotConfig] = field(default_factory=list)
    unknown: list[Found] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.moved)

    def report(self) -> str:
        lines = []
        for r, f in self.moved:
            lines.append(f"  MOVED    {r.id}: {r.ip} -> {f.ip}")
        for r in self.missing:
            lines.append(f"  MISSING  {r.id} (was {r.ip}) -- powered off, or on another subnet")
        for f in self.unknown:
            hw = f.hardware_id or "?"
            lines.append(f"  UNKNOWN  {f.ip} hardware_id={hw} -- not in robots.yaml")
        if not lines:
            lines.append(f"  all {len(self.matched)} robot(s) at their expected addresses")
        return "\n".join(lines)


def reconcile(registry: RobotRegistry, found: list[Found]) -> Reconciliation:
    """Match discovery results to the registry on hardware_id.

    Deliberately does *not* match on IP: matching on IP is what makes a fleet
    silently address the wrong robot after a DHCP shuffle.
    """
    rec = Reconciliation()
    by_hw = {f.hardware_id: f for f in found if f.hardware_id}
    used: set[str] = set()

    for robot in registry:
        f = by_hw.get(robot.hardware_id)
        if f is None:
            rec.missing.append(robot)
            continue
        used.add(f.hardware_id)
        if f.ip != robot.ip:
            rec.moved.append((robot, f))
        else:
            rec.matched.append((robot, f))

    for f in found:
        if f.hardware_id and f.hardware_id not in used:
            rec.unknown.append(f)
        elif not f.hardware_id:
            rec.unknown.append(f)
    return rec


def apply_reconciliation(registry: RobotRegistry, rec: Reconciliation) -> int:
    """Write discovered IPs back into the registry. Returns the change count."""
    for robot, f in rec.moved:
        robot.ip = f.ip
        if f.wireless is not None:
            robot.variant = f.variant
    return len(rec.moved)
