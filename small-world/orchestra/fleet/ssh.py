"""SSH and SCP, used for exactly what SSH is good at.

SSH is the right tool for provisioning, file deployment, launching the agent and
recovering a wedged daemon. It is emphatically **not** used for motion: it adds
TCP setup, encryption, a PTY and a process spawn per command, is unbatched, and
offers no timing guarantee. Motion goes over the agent's own channel, which is
already running when the show starts.

Uses the OpenSSH binaries that ship with Windows 10/11 rather than paramiko --
one less dependency, and `ssh`/`scp` are already what you would type by hand
when debugging.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..config import FleetConfig, RobotConfig
from .health import parallel


# `text=True` は Python の**ロケール**エンコーディングで復号する。日本語版
# Windows ではそれが cp932 になり、ロボット側が UTF-8 で書いたログを読んだ
# 瞬間に UnicodeDecodeError でスレッドごと落ちる ―― しかも落ちるのは
# subprocess の読み取りスレッドなので、こちらには「出力が空」としか見えず、
# 原因が分からないまま「ログが取れない」ことになる。実際そうなった。
# ロボット側は Linux で常に UTF-8 なので、明示的に固定する。errors="replace"
# は、壊れたバイトのせいで復旧作業そのものが止まらないようにするため。
_TEXT = {"encoding": "utf-8", "errors": "replace", "text": True}


class SSHUnavailable(RuntimeError):
    pass


@dataclass
class SSHResult:
    host: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> "SSHResult":
        if not self.ok:
            raise RuntimeError(
                f"ssh {self.host} failed ({self.returncode}): "
                f"{self.stderr.strip() or self.stdout.strip()}"
            )
        return self


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SSHUnavailable(
            f"{tool} not found on PATH. On Windows install the OpenSSH Client "
            "optional feature (Settings > Apps > Optional features), or add "
            "Git's usr/bin to PATH."
        )
    return path


class SSHClient:
    """Non-interactive SSH to one robot."""

    def __init__(self, cfg: FleetConfig, host: str):
        self.cfg = cfg
        self.host = host
        self.user = cfg.ssh_user
        self.key = cfg.ssh_key
        self.timeout = int(cfg.ssh.get("connect_timeout", 10))

    def _base_opts(self) -> list[str]:
        opts = [
            "-o", "BatchMode=yes",              # never hang on a password prompt
            "-o", f"ConnectTimeout={self.timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR",
        ]
        if self.key:
            opts += ["-i", str(self.key), "-o", "IdentitiesOnly=yes"]
        return opts

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def run(self, command: str, timeout: float = 60.0) -> SSHResult:
        cmd = [_require("ssh"), *self._base_opts(), self.target, command]
        try:
            p = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False, **_TEXT
            )
        except subprocess.TimeoutExpired:
            return SSHResult(self.host, 124, "", f"timed out after {timeout}s")
        return SSHResult(self.host, p.returncode, p.stdout, p.stderr)

    def run_detached(self, command: str, log: str = "/dev/null") -> SSHResult:
        """Start a long-lived process and return immediately.

        `setsid` + full redirection is what keeps the agent alive after the SSH
        channel closes; without it the agent dies with its parent shell the
        moment we disconnect.
        """
        wrapped = f"setsid nohup sh -c {_shq(command)} > {log} 2>&1 < /dev/null & echo $!"
        return self.run(wrapped, timeout=20.0)

    def push(self, local: str | Path, remote: str, timeout: float = 300.0) -> SSHResult:
        local = Path(local)
        if not local.exists():
            raise FileNotFoundError(local)
        cmd = [
            _require("scp"), *self._base_opts(), "-p",
            str(local), f"{self.target}:{remote}",
        ]
        try:
            p = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False, **_TEXT
            )
        except subprocess.TimeoutExpired:
            return SSHResult(self.host, 124, "", f"scp timed out after {timeout}s")
        return SSHResult(self.host, p.returncode, p.stdout, p.stderr)

    def pull(self, remote: str, local: str | Path, timeout: float = 300.0) -> SSHResult:
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            _require("scp"), *self._base_opts(), "-p",
            f"{self.target}:{remote}", str(local),
        ]
        p = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False, **_TEXT
        )
        return SSHResult(self.host, p.returncode, p.stdout, p.stderr)

    # -- higher level ----------------------------------------------------
    def mkdir(self, path: str) -> SSHResult:
        return self.run(f"mkdir -p {_shq(path)}")

    def find_python(self, preferred: str) -> str | None:
        """Locate an interpreter that can import reachy_mini.

        The agent must run against the robot's *own* SDK: that is what removes
        any PC-side version mismatch and what gives it the local media backend.
        """
        candidates = [
            preferred,
            "/venvs/mini_daemon/bin/python",
            "/venvs/mini_apps/bin/python",
            "/usr/bin/python3",
            "python3",
        ]
        probe = " ; ".join(
            f'{c} -c "import reachy_mini,sys;print(\'FOUND\',sys.executable,reachy_mini.__version__)" 2>/dev/null'
            for c in candidates
        )
        res = self.run(probe, timeout=60.0)
        for line in res.stdout.splitlines():
            if line.startswith("FOUND "):
                return line.split()[1]
        return None

    def restart_daemon(self) -> SSHResult:
        """The recovery path that works.

        LEARNINGS.md is unambiguous: `systemctl restart` is the reliable route,
        whereas the API stop/start pair corrupts WebRTC signalling state. Even
        though our architecture avoids WebRTC, this is still the one to use.
        """
        return self.run(
            "sudo systemctl restart reachy-mini-daemon.service", timeout=60.0
        )

    def daemon_journal(self, lines: int = 60) -> SSHResult:
        return self.run(
            f"journalctl -u reachy-mini-daemon.service -n {int(lines)} --no-pager",
            timeout=30.0,
        )


def _shq(s: str) -> str:
    """Single-quote for POSIX sh. The robots are Debian; the PC is not."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def fan_out(
    cfg: FleetConfig,
    robots: Sequence[RobotConfig],
    fn,
    workers: int | None = None,
) -> dict[str, tuple[object, Exception | None]]:
    """Run `fn(SSHClient, RobotConfig)` across robots concurrently."""
    def call(r: RobotConfig):
        return fn(SSHClient(cfg, r.ip), r)

    out: dict[str, tuple[object, Exception | None]] = {}
    for r, res, err in parallel(list(robots), call, workers=workers):
        out[r.id] = (res, err)
    return out


def install_pubkey(cfg: FleetConfig, host: str, password: str, pubkey: Path) -> SSHResult:
    """Bootstrap key auth using password auth once.

    Windows' OpenSSH has no `sshpass`, and BatchMode forbids prompting, so this
    shells out to `ssh` with the password on stdin only when the platform
    supports it. In practice the reliable route on Windows is to run
    `ssh-copy-id` from Git Bash, or paste the key manually -- `orchestra fleet
    provision` prints the exact command when it cannot do it itself.
    """
    raise SSHUnavailable(
        "automatic key installation is not supported here. Run this once per "
        "robot from Git Bash:\n"
        f"  ssh-copy-id -i {pubkey} {cfg.ssh_user}@{host}\n"
        "or append the key by hand:\n"
        f"  cat {pubkey} | ssh {cfg.ssh_user}@{host} "
        "'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'"
    )
