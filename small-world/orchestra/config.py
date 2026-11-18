"""Loading of `config/fleet.yaml` and `config/robots.yaml`.

Everything downstream reads its limits and network settings from here, so that
changing a safety margin is a config edit rather than a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    """Repo root, overridable with ORCHESTRA_ROOT for tests and odd layouts."""
    env = os.environ.get("ORCHESTRA_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"missing config file {path}. Run `orchestra fleet init` to create one."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return data


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

# Joint order used everywhere: matches the SDK's 7-vector
# [body_yaw, stewart_1..stewart_6] (reachy_mini.py:798-806).
JOINT_NAMES = (
    "body_yaw",
    "stewart_1",
    "stewart_2",
    "stewart_3",
    "stewart_4",
    "stewart_5",
    "stewart_6",
)


@dataclass
class SafetyLimits:
    """Operating limits. Tighter than the hardware's, deliberately."""

    tilt_cone_rad: float
    tilt_cone_hard_rad: float
    rel_yaw_rad: float
    rel_yaw_hard_rad: float
    body_yaw_rad: float
    body_yaw_hard_rad: float
    antenna_rad: float
    translation_m: dict[str, tuple[float, float]]
    joint_limits_rad: dict[str, tuple[float, float]]
    joint_limit_inset: float
    joint_velocity_max: float
    joint_accel_max: float
    joint_accel_abort: float
    antenna_velocity_max: float
    gesture_min_interval_s: float
    gesture_duty_window_s: float
    gesture_duty_max_rms: float
    antenna_duty_max_rms: float
    gesture_window_s: float
    gesture_accel_scale: float
    forbid_cartoon_near_anchor_s: float
    motor_temp_warn_c: float
    motor_temp_abort_c: float
    min_control_loop_hz: float

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "SafetyLimits":
        import math

        def deg(section: str, key: str = "limit") -> float:
            return math.radians(float(d[section][key]))

        vel_frac = float(d.get("joint_velocity_frac", 0.5))
        vel_max = float(d.get("joint_velocity_max_rad_s", 8.0)) * vel_frac

        return SafetyLimits(
            tilt_cone_rad=deg("head_tilt_cone_deg"),
            tilt_cone_hard_rad=deg("head_tilt_cone_deg", "hard"),
            rel_yaw_rad=deg("head_rel_yaw_deg"),
            rel_yaw_hard_rad=deg("head_rel_yaw_deg", "hard"),
            body_yaw_rad=deg("body_yaw_deg"),
            body_yaw_hard_rad=deg("body_yaw_deg", "hard"),
            antenna_rad=deg("antenna_deg"),
            translation_m={
                k: (float(v[0]), float(v[1]))
                for k, v in d["head_translation_m"].items()
            },
            joint_limits_rad={
                k: (float(v[0]), float(v[1]))
                for k, v in d["joint_limits_rad"].items()
            },
            joint_limit_inset=float(d.get("joint_limit_inset", 0.05)),
            joint_velocity_max=vel_max,
            joint_accel_max=float(d.get("joint_accel_max_rad_s2", 120.0)),
            joint_accel_abort=float(d.get("joint_accel_abort_rad_s2", 600.0)),
            gesture_window_s=float(d.get("gesture_window_s", 0.10)),
            gesture_accel_scale=float(d.get("gesture_accel_scale", 1.8)),
            antenna_velocity_max=float(d.get("antenna_velocity_max_rad_s", 12.0)),
            gesture_min_interval_s=float(d.get("gesture_min_interval_s", 0.11)),
            gesture_duty_window_s=float(d.get("gesture_duty_window_s", 10.0)),
            gesture_duty_max_rms=float(d.get("gesture_duty_max_rms", 2.5)),
            antenna_duty_max_rms=float(d.get("antenna_duty_max_rms", 3.0)),
            forbid_cartoon_near_anchor_s=float(
                d.get("forbid_cartoon_near_anchor_s", 0.0)
            ),
            motor_temp_warn_c=float(d.get("motor_temp_warn_c", 55)),
            motor_temp_abort_c=float(d.get("motor_temp_abort_c", 65)),
            min_control_loop_hz=float(d.get("min_control_loop_hz", 45.0)),
        )

    def joint_range(self, name: str) -> tuple[float, float]:
        """Joint limits with the safety inset applied."""
        lo, hi = self.joint_limits_rad[name]
        span = hi - lo
        pad = span * self.joint_limit_inset
        return lo + pad, hi - pad


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------


@dataclass
class FleetConfig:
    network: dict[str, Any]
    ssh: dict[str, Any]
    agent: dict[str, Any]
    clock_sync: dict[str, Any]
    show: dict[str, Any]
    safety: SafetyLimits
    secondary_motion: dict[str, Any]
    poses: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def daemon_port(self) -> int:
        return int(self.network.get("daemon_port", 8000))

    @property
    def agent_port(self) -> int:
        return int(self.network.get("agent_port", 8770))

    @property
    def ssh_user(self) -> str:
        return str(self.ssh.get("user", "pollen"))

    @property
    def ssh_key(self) -> Path | None:
        k = self.ssh.get("key")
        if not k:
            return None
        p = Path(os.path.expanduser(str(k)))
        return p if p.exists() else None

    @property
    def remote_dir(self) -> str:
        return str(self.agent.get("remote_dir", "/home/pollen/orchestra"))

    @property
    def remote_python(self) -> str:
        return str(self.agent.get("python", "/venvs/mini_daemon/bin/python"))

    @property
    def fps(self) -> float:
        return float(self.agent.get("fps", 100.0))

    @property
    def neutral_antennas(self) -> tuple[float, float]:
        v = self.poses.get("neutral_antennas_rad", [-0.1745, 0.1745])
        return float(v[0]), float(v[1])

    @staticmethod
    def load(path: str | Path | None = None) -> "FleetConfig":
        p = Path(path) if path else project_root() / "config" / "fleet.yaml"
        d = _read_yaml(p)
        return FleetConfig(
            network=d.get("network", {}),
            ssh=d.get("ssh", {}),
            agent=d.get("agent", {}),
            clock_sync=d.get("clock_sync", {}),
            show=d.get("show", {}),
            safety=SafetyLimits.from_dict(d.get("safety", {})),
            secondary_motion=d.get("secondary_motion", {}),
            poses=d.get("poses", {}),
            raw=d,
        )


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------


@dataclass
class RobotConfig:
    """One robot's identity and default assignment.

    `hardware_id` is identity; `ip` is a cache. Never key anything on `ip`.
    """

    id: str
    hardware_id: str
    ip: str
    mac: str = ""
    variant: str = "wireless"
    role: str = "performer"
    instrument: str | None = None
    pitch: str | None = None
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_conductor(self) -> bool:
        return self.role == "conductor"

    @property
    def has_imu(self) -> bool:
        """Lite units return None from the SDK's `imu` property."""
        return self.variant != "lite"

    def __str__(self) -> str:
        what = self.instrument or self.role
        return f"{self.id} ({self.ip}, {what})"


@dataclass
class Calibration:
    """Per-robot measured values. Absent fields fall back to safe defaults."""

    robot_id: str
    audio_lead_ms: float = 0.0
    home_offset: dict[str, float] = field(default_factory=dict)
    volume: int | None = None
    notes: str = ""

    @staticmethod
    def load(robot_id: str, root: Path | None = None) -> "Calibration":
        root = root or (project_root() / "config" / "calibration")
        path = root / f"{robot_id}.yaml"
        if not path.exists():
            return Calibration(robot_id=robot_id)
        d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Calibration(
            robot_id=robot_id,
            audio_lead_ms=float(d.get("audio_lead_ms", 0.0)),
            home_offset=dict(d.get("home_offset", {})),
            volume=d.get("volume"),
            notes=str(d.get("notes", "")),
        )

    def save(self, root: Path | None = None) -> Path:
        root = root or (project_root() / "config" / "calibration")
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{self.robot_id}.yaml"
        payload = {
            "robot_id": self.robot_id,
            "audio_lead_ms": self.audio_lead_ms,
            "home_offset": self.home_offset,
            "volume": self.volume,
            "notes": self.notes,
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path


class RobotRegistry:
    """The fleet, addressable by id or hardware_id.

    Iteration order follows the file, which keeps CLI output stable.
    """

    def __init__(self, robots: list[RobotConfig], path: Path | None = None):
        self.robots = robots
        self.path = path
        self._by_id = {r.id: r for r in robots}
        self._by_hwid = {r.hardware_id: r for r in robots if r.hardware_id}
        dupes = [r.id for r in robots if list(self._by_id).count(r.id) > 1]
        if len(self._by_id) != len(robots):
            raise ValueError(f"duplicate robot id(s) in {path}: {sorted(set(dupes))}")

    def __iter__(self):
        return iter(self.robots)

    def __len__(self) -> int:
        return len(self.robots)

    @property
    def enabled(self) -> list[RobotConfig]:
        return [r for r in self.robots if r.enabled]

    @property
    def conductor(self) -> RobotConfig | None:
        return next((r for r in self.robots if r.is_conductor), None)

    @property
    def performers(self) -> list[RobotConfig]:
        return [r for r in self.robots if not r.is_conductor]

    def get(self, key: str) -> RobotConfig:
        """Resolve by id, hardware_id or IP -- whichever the user typed."""
        if key in self._by_id:
            return self._by_id[key]
        if key in self._by_hwid:
            return self._by_hwid[key]
        for r in self.robots:
            if r.ip == key:
                return r
        raise KeyError(
            f"unknown robot {key!r}. Known: {', '.join(sorted(self._by_id))}"
        )

    def select(self, keys: list[str] | None) -> list[RobotConfig]:
        """Resolve a --robot selection; None or ['all'] means the whole fleet."""
        if not keys or keys == ["all"]:
            return self.enabled
        out: list[RobotConfig] = []
        for k in keys:
            for part in str(k).split(","):
                part = part.strip()
                if part:
                    out.append(self.get(part))
        # De-duplicate, preserving order.
        seen: set[str] = set()
        return [r for r in out if not (r.id in seen or seen.add(r.id))]

    @staticmethod
    def load(path: str | Path | None = None) -> "RobotRegistry":
        p = Path(path) if path else project_root() / "config" / "robots.yaml"
        d = _read_yaml(p)
        entries = d.get("robots") or []
        robots: list[RobotConfig] = []
        known = {
            "id", "hardware_id", "ip", "mac", "variant",
            "role", "instrument", "pitch", "enabled",
        }
        for i, e in enumerate(entries):
            if "id" not in e:
                raise ValueError(f"{p}: robots[{i}] is missing 'id'")
            robots.append(
                RobotConfig(
                    id=str(e["id"]),
                    hardware_id=str(e.get("hardware_id", "")),
                    ip=str(e.get("ip", "")),
                    mac=str(e.get("mac", "")),
                    variant=str(e.get("variant", "wireless")),
                    role=str(e.get("role", "performer")),
                    instrument=e.get("instrument"),
                    pitch=e.get("pitch"),
                    enabled=bool(e.get("enabled", True)),
                    extra={k: v for k, v in e.items() if k not in known},
                )
            )
        return RobotRegistry(robots, path=p)

    def save(self, path: str | Path | None = None) -> Path:
        p = Path(path) if path else (self.path or project_root() / "config" / "robots.yaml")
        payload = {"robots": []}
        for r in self.robots:
            e: dict[str, Any] = {"id": r.id, "hardware_id": r.hardware_id}
            if r.mac:
                e["mac"] = r.mac
            e["ip"] = r.ip
            e["variant"] = r.variant
            e["role"] = r.role
            if r.instrument:
                e["instrument"] = r.instrument
            if r.pitch:
                e["pitch"] = r.pitch
            if not r.enabled:
                e["enabled"] = False
            e.update(r.extra)
            payload["robots"].append(e)
        p.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return p


@dataclass
class Context:
    """Everything the CLI needs, loaded once."""

    fleet: FleetConfig
    robots: RobotRegistry
    root: Path

    @staticmethod
    def load(root: str | Path | None = None) -> "Context":
        r = Path(root) if root else project_root()
        return Context(
            fleet=FleetConfig.load(r / "config" / "fleet.yaml"),
            robots=RobotRegistry.load(r / "config" / "robots.yaml"),
            root=r,
        )

    @property
    def clips_dir(self) -> Path:
        return self.root / "clips"

    @property
    def songs_dir(self) -> Path:
        return self.root / "songs"

    @property
    def build_dir(self) -> Path:
        return self.root / "build"
