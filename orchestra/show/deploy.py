"""Getting the agent, the programs and the audio stems onto the robots.

Everything here happens *before* the show. Nothing in this module runs while the
performance is playing -- that is the point of preloading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Context, RobotConfig
from ..fleet.health import DaemonClient, parallel
from ..fleet.ssh import SSHClient, SSHResult

AGENT_FILE = "orchestra_agent.py"
# エージェントと一緒に置く補助モジュール。単体で完結させたいので数は絞る。
AGENT_EXTRA_FILES = ("audio_stream.py", "mic_record.py", "selftest.py",
                     "audio_probe.py")


@dataclass
class DeployResult:
    robot_id: str
    ok: bool = False
    steps: list[str] = field(default_factory=list)
    error: str = ""

    def note(self, msg: str) -> None:
        self.steps.append(msg)


def provision(ctx: Context, robot: RobotConfig) -> DeployResult:
    """Create the remote layout and install the agent.

    Also locates a Python that can import reachy_mini. That matters: the agent
    must run against the robot's *own* SDK, which is what removes any PC-side
    version skew and what gives it the local (non-WebRTC) media backend.
    """
    res = DeployResult(robot_id=robot.id)
    ssh = SSHClient(ctx.fleet, robot.ip)
    base = ctx.fleet.remote_dir

    try:
        r = ssh.run(f"mkdir -p {base}/programs {base}/audio {base}/logs")
        if not r.ok:
            res.error = f"mkdir failed: {r.stderr.strip()}"
            return res
        res.note(f"created {base}")

        python = ssh.find_python(ctx.fleet.remote_python)
        if not python:
            res.error = (
                "no interpreter on this robot can import reachy_mini. Tried the "
                f"configured {ctx.fleet.remote_python} and the usual venvs. Check "
                "`agent.python` in config/fleet.yaml."
            )
            return res
        res.note(f"interpreter: {python}")
        robot.extra["python"] = python

        for fname in (AGENT_FILE, *AGENT_EXTRA_FILES):
            src = ctx.root / "agent" / fname
            if not src.exists():
                continue
            r = ssh.push(src, f"{base}/{fname}")
            if not r.ok:
                res.error = f"{fname} upload failed: {r.stderr.strip()}"
                return res
        res.note(f"uploaded {AGENT_FILE} + {len(AGENT_EXTRA_FILES)} modules")

        r = ssh.run(f"{python} -c \"import numpy, json; print('deps ok')\"")
        if not r.ok or "deps ok" not in r.stdout:
            res.error = f"agent dependency check failed: {r.stderr.strip()}"
            return res
        res.note("dependencies ok")

        res.ok = True
        return res
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res


def deploy_programs(
    ctx: Context,
    robot: RobotConfig,
    program_path: Path,
    audio_path: Path | None = None,
    use_http_for_audio: bool = False,
) -> DeployResult:
    """Push one robot's program and (optionally) its audio stem."""
    res = DeployResult(robot_id=robot.id)
    ssh = SSHClient(ctx.fleet, robot.ip)
    base = ctx.fleet.remote_dir

    try:
        r = ssh.push(program_path, f"{base}/programs/{program_path.name}")
        if not r.ok:
            res.error = f"program upload failed: {r.stderr.strip()}"
            return res
        res.note(f"program {program_path.name} ({program_path.stat().st_size} B)")

        if audio_path and audio_path.exists():
            if use_http_for_audio:
                # The daemon's own sound-library endpoint. Needs no SSH
                # credentials, but puts the file in the daemon's sound dir
                # rather than ours, so the agent has to be told where to look.
                c = DaemonClient(robot.ip, ctx.fleet.daemon_port)
                try:
                    c.upload_sound(audio_path)
                    res.note(f"audio {audio_path.name} via daemon API")
                finally:
                    c.close()
            else:
                r = ssh.push(audio_path, f"{base}/audio/{audio_path.name}")
                if not r.ok:
                    res.error = f"audio upload failed: {r.stderr.strip()}"
                    return res
                res.note(f"audio {audio_path.name} ({audio_path.stat().st_size} B)")

        res.ok = True
        return res
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res


def agent_start(ctx: Context, robot: RobotConfig, dry_run: bool = False) -> DeployResult:
    """Launch the agent, detached, so it survives the SSH channel closing."""
    res = DeployResult(robot_id=robot.id)
    ssh = SSHClient(ctx.fleet, robot.ip)
    base = ctx.fleet.remote_dir
    python = robot.extra.get("python") or ctx.fleet.remote_python

    try:
        if agent_running(ctx, robot):
            res.note("already running")
            res.ok = True
            return res

        cmd = (
            f"{python} -u {base}/{AGENT_FILE} "
            f"--port {ctx.fleet.agent_port} "
            f"--daemon-port {ctx.fleet.daemon_port} "
            f"--base-dir {base} "
            f"--fps {ctx.fleet.fps} "
            f"--abort-ramp {ctx.fleet.agent.get('abort_ramp_s', 0.5)} "
            f"--deadman {ctx.fleet.agent.get('deadman_timeout_s', 5.0)}"
        )
        if dry_run:
            cmd += " --dry-run"

        r = ssh.run_detached(cmd, log=f"{base}/logs/agent.log")
        if not r.ok:
            res.error = f"launch failed: {r.stderr.strip()}"
            return res
        res.note(f"launched (pid {r.stdout.strip()})")
        res.ok = True
        return res
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res


def agent_stop(ctx: Context, robot: RobotConfig) -> DeployResult:
    """Stop the agent. Tries a clean shutdown first, then pkill."""
    res = DeployResult(robot_id=robot.id)
    from ..fleet.link import AgentLink

    try:
        link = AgentLink(robot.id, robot.ip, ctx.fleet.agent_port, timeout=3.0)
        link.connect()
        link.shutdown()
        link.close()
        res.note("shut down cleanly")
        res.ok = True
        return res
    except Exception:
        pass

    try:
        ssh = SSHClient(ctx.fleet, robot.ip)
        r = ssh.run(f"pkill -f {AGENT_FILE} || true")
        res.note("pkill issued" if r.ok else f"pkill failed: {r.stderr.strip()}")
        res.ok = r.ok
        return res
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res


def agent_running(ctx: Context, robot: RobotConfig) -> bool:
    import socket

    try:
        with socket.create_connection((robot.ip, ctx.fleet.agent_port), timeout=2.0):
            return True
    except OSError:
        return False


def agent_log(ctx: Context, robot: RobotConfig, lines: int = 50) -> str:
    ssh = SSHClient(ctx.fleet, robot.ip)
    r = ssh.run(f"tail -n {int(lines)} {ctx.fleet.remote_dir}/logs/agent.log")
    return r.stdout or r.stderr


# ---------------------------------------------------------------------------
# Fleet-wide
# ---------------------------------------------------------------------------


def deploy_show(
    ctx: Context,
    robots: list[RobotConfig],
    build_dir: Path,
    audio_dir: Path | None = None,
    audio_for: dict[str, str] | None = None,
) -> dict[str, DeployResult]:
    """Push every robot's program and stem concurrently."""
    audio_for = audio_for or {}

    def one(robot: RobotConfig) -> DeployResult:
        prog = build_dir / f"{robot.id}.program.json.gz"
        if not prog.exists():
            r = DeployResult(robot_id=robot.id)
            r.error = f"no program built for {robot.id} ({prog} missing)"
            return r
        audio = None
        name = audio_for.get(robot.id)
        if name and audio_dir:
            cand = audio_dir / name
            if cand.exists():
                audio = cand
        return deploy_programs(ctx, robot, prog, audio)

    return {
        r.id: (res if res is not None else _failed(r.id, err))
        for r, res, err in parallel(robots, one)
    }


def _failed(robot_id: str, err: Exception | None) -> DeployResult:
    d = DeployResult(robot_id=robot_id)
    d.error = str(err) if err else "unknown failure"
    return d


def provision_fleet(ctx: Context, robots: list[RobotConfig]) -> dict[str, DeployResult]:
    return {
        r.id: (res if res is not None else _failed(r.id, err))
        for r, res, err in parallel(robots, lambda rb: provision(ctx, rb))
    }


def start_fleet_agents(
    ctx: Context, robots: list[RobotConfig], dry_run: bool = False
) -> dict[str, DeployResult]:
    return {
        r.id: (res if res is not None else _failed(r.id, err))
        for r, res, err in parallel(robots, lambda rb: agent_start(ctx, rb, dry_run))
    }


def stop_fleet_agents(ctx: Context, robots: list[RobotConfig]) -> dict[str, DeployResult]:
    return {
        r.id: (res if res is not None else _failed(r.id, err))
        for r, res, err in parallel(robots, lambda rb: agent_stop(ctx, rb))
    }
