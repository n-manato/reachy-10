#!/usr/bin/env python3
"""Reachy Mini Orchestra -- on-robot agent.

Runs ON the robot, against the robot's own Python and its own reachy_mini SDK.
Deliberately a single file depending only on the stdlib plus reachy_mini/numpy,
so deployment is one `scp` and there is no packaging step, no pip install and no
version skew with the PC.

WHY THIS EXISTS
---------------
The stock daemon has no scheduled-execution primitive. There is no "start at
absolute time T" anywhere in the protocol, the daemon or the Rust motor layer;
every `timestamp` field in the wire protocol is written by the client and never
read by the server. And streaming targets from the PC does not survive nine
robots on one Wi-Fi channel -- with only three robots the sibling ethics-podcast
project measured ICMP going from ~15 ms to 500-1400 ms with timeouts while
streaming.

So the agent inverts the problem. The whole performance is preloaded, the PC and
robot agree on a shared timebase up front, and the PC then sends one small START
message seconds in advance. Each robot runs its own 100 Hz loop against its own
monotonic clock. Cross-robot skew collapses to clock-sync error (~1-5 ms) instead
of per-frame network jitter, and Wi-Fi traffic during the show drops to a 1 Hz
heartbeat.

It also handles audio locally: the robot plays its own stem from local disk
through its own speaker, on the same clock as its own motion.

CAVEAT, learned the hard way on the first live run: `connection_mode=
"localhost_only"` is NOT by itself enough to get a local media backend. The SDK
picks LOCAL only when localhost_only **and** `is_local_camera_available()`
(reachy_mini.py:338); otherwise it silently falls back to WebRTC, which then
fails with `Connection refused` on any robot not running a signalling server.
That killed two of nine robots at arm time. So `connect()` asks for backends
explicitly and degrades to `no_media` rather than failing -- a robot that moves
in silence beats a robot that does nothing.

PROTOCOL
--------
Newline-delimited JSON over TCP. One request per line, one response per line.
Small, synchronous and easy to drive from a shell for debugging:

    printf '{"op":"status"}\\n' | nc 192.168.3.105 8770
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# 同じディレクトリに置いた audio_stream.py を読めるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent))

AGENT_VERSION = "0.1.0"
DEFAULT_PORT = 8770

# Frame channel layout. MUST match orchestra/motion/program.py -- the round-trip
# is asserted by tests/test_program_and_agent.py.
CH_X, CH_Y, CH_Z = 0, 1, 2
CH_ROLL, CH_PITCH, CH_YAW = 3, 4, 5
CH_BODY_YAW = 6
CH_ANT_R, CH_ANT_L = 7, 8
N_CHANNELS = 9

STATE_IDLE = "idle"
STATE_LOADED = "loaded"
STATE_ARMED = "armed"
STATE_SCHEDULED = "scheduled"
STATE_PLAYING = "playing"
STATE_DONE = "done"
STATE_ABORTED = "aborted"
STATE_ERROR = "error"

log = logging.getLogger("orchestra.agent")


# ---------------------------------------------------------------------------
# Program (mirror of orchestra/motion/program.py, kept minimal on purpose)
# ---------------------------------------------------------------------------


@dataclass
class Program:
    robot_id: str = ""
    song: str = ""
    part: str = ""
    instrument: str = ""
    fps: float = 100.0
    frames: np.ndarray = field(default_factory=lambda: np.zeros((0, N_CHANNELS)))
    anchors: np.ndarray = field(default_factory=lambda: np.zeros(0))
    audio_file: str = ""
    audio_lead_ms: float = 0.0
    heads: np.ndarray | None = None
    """(N,4,4) head matrices, precomputed at load so the hot loop never does
    trigonometry. ~2.3 MB for a 3-minute song -- trivial on a Pi, and it keeps
    per-frame work down to an array slice and a JSON dump."""

    @property
    def n_frames(self) -> int:
        return int(self.frames.shape[0])

    @property
    def duration(self) -> float:
        return max(0.0, (self.n_frames - 1) / self.fps)

    @staticmethod
    def load(path: str | Path) -> "Program":
        path = Path(path)
        raw = path.read_bytes()
        if path.suffix == ".gz" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        d = json.loads(raw.decode("utf-8"))
        if d.get("format") != "reachy-orchestra-program":
            raise ValueError(f"{path} is not a reachy-orchestra-program")

        frames = np.asarray(d.get("frames", []), dtype=np.float64)
        if frames.size == 0:
            frames = np.zeros((0, N_CHANNELS), dtype=np.float64)
        if frames.shape[1] != N_CHANNELS:
            raise ValueError(f"expected {N_CHANNELS} channels, got {frames.shape[1]}")

        audio = d.get("audio") or {}
        p = Program(
            robot_id=str(d.get("robot_id", "")),
            song=str(d.get("song", "")),
            part=str(d.get("part", "")),
            instrument=str(d.get("instrument") or ""),
            fps=float(d.get("fps", 100.0)),
            frames=frames,
            anchors=np.asarray(d.get("anchors", []), dtype=np.float64),
            audio_file=str(audio.get("file", "")),
            audio_lead_ms=float(audio.get("lead_ms", 0.0)),
        )
        p.heads = head_matrices(frames[:, CH_X : CH_YAW + 1])
        return p

    def frame_index_at(self, t: float) -> int:
        if self.n_frames == 0:
            return 0
        return max(0, min(self.n_frames - 1, int(round(t * self.fps))))


def head_matrices(xyzrpy: np.ndarray) -> np.ndarray:
    """XYZRPY -> 4x4, matching reachy_mini.utils.create_head_pose.

    scipy's ``from_euler("xyz", ...)`` is lowercase = extrinsic, so the composed
    matrix is Rz @ Ry @ Rx. Written out longhand to avoid a scipy import in the
    hot path.
    """
    xyzrpy = np.asarray(xyzrpy, dtype=np.float64).reshape(-1, 6)
    n = xyzrpy.shape[0]
    roll, pitch, yaw = xyzrpy[:, 3], xyzrpy[:, 4], xyzrpy[:, 5]
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)

    m = np.zeros((n, 4, 4), dtype=np.float64)
    m[:, 0, 0] = cy * cp
    m[:, 0, 1] = cy * sp * sr - sy * cr
    m[:, 0, 2] = cy * sp * cr + sy * sr
    m[:, 1, 0] = sy * cp
    m[:, 1, 1] = sy * sp * sr + cy * cr
    m[:, 1, 2] = sy * sp * cr - cy * sr
    m[:, 2, 0] = -sp
    m[:, 2, 1] = cp * sr
    m[:, 2, 2] = cp * cr
    m[:, :3, 3] = xyzrpy[:, :3]
    m[:, 3, 3] = 1.0
    return m


def minjerk(u: float) -> float:
    """10u^3 - 15u^4 + 6u^5, matching the SDK's MIN_JERK."""
    u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)
    return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5


def sleep_until(target: float, stop: threading.Event | None = None) -> bool:
    """Block until ``time.monotonic() >= target``.

    Coarse sleep down to ~2 ms, then a short spin. The spin is what buys the
    last millisecond: ``time.sleep`` on Linux typically overshoots by 50-100 us
    but can be far worse under load, and that error would land directly in the
    cross-robot skew budget. Returns False if interrupted.
    """
    while True:
        remaining = target - time.monotonic()
        if remaining <= 0:
            return True
        if stop is not None and stop.is_set():
            return False
        if remaining > 0.020:
            time.sleep(min(remaining - 0.010, 0.25))
        elif remaining > 0.002:
            time.sleep(remaining - 0.0015)
        else:
            while time.monotonic() < target:
                if stop is not None and stop.is_set():
                    return False
            return True


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class Agent:
    def __init__(
        self,
        base_dir: Path,
        daemon_port: int = 8000,
        fps: float | None = None,
        abort_ramp_s: float = 0.5,
        deadman_timeout_s: float = 5.0,
        dry_run: bool = False,
        use_net_clock: bool = False,
    ):
        self.base_dir = Path(base_dir)
        self.daemon_port = daemon_port
        self.fps_override = fps
        self.abort_ramp_s = abort_ramp_s
        self.deadman_timeout_s = deadman_timeout_s
        self.dry_run = dry_run
        self.use_net_clock = use_net_clock

        self.lock = threading.RLock()
        self.state = STATE_IDLE
        self.error = ""
        self.program: Program | None = None
        self.program_name = ""

        self.reachy = None
        self._media_on = False
        self.media_backend = ""
        self.audio_stream = None   # AudioStream。arm 時に用意する
        self.volume: int | None = None
        """`op_volume` で最後に設定した音量。まだ触っていなければ None。

        読み返しはしない。デーモンの `GET /api/volume/current` は PC からでも
        引けるし、演奏中に増やしたい往復ではない。"""

        self._t0_local: float | None = None
        self.recorder = None    # MicRecorder | None
        self._play_thread: threading.Thread | None = None
        self._stop = threading.Event()
        """Stops the CURRENT playback. Cleared again by every `start`."""

        self._abort = threading.Event()
        self._fade_s = 0.0
        self._cut = threading.Event()
        """Ends a fade-out now. The emergency stop must not wait for one."""

        self._shutdown = threading.Event()
        """Process is going away. Distinct from `_stop` on purpose: the deadman
        watchdog must survive an abort. An earlier version had it watching
        `_stop`, which meant the watchdog exited permanently after the first
        abort and every subsequent run was unprotected -- silently, since a
        watchdog that never fires looks identical to one that never needed to."""

        self.last_contact = time.monotonic()
        self.stats: dict[str, Any] = {}
        self.started_at_mono: float | None = None

    # -- state helpers ---------------------------------------------------
    def _set_state(self, s: str, err: str = "") -> None:
        with self.lock:
            self.state = s
            self.error = err
        log.info("state -> %s%s", s, f" ({err})" if err else "")

    def touch(self) -> None:
        self.last_contact = time.monotonic()

    # -- robot connection ------------------------------------------------
    def connect(self, want_media: bool) -> None:
        """Connect to the local daemon, degrading audio rather than failing.

        `localhost_only` keeps the control WebSocket on loopback -- sub-
        millisecond, no Wi-Fi anywhere in the motion path. That part is not
        negotiable and always works.

        Audio is a different story. The SDK's "default" backend does NOT simply
        mean local: it picks LOCAL only when `connection_mode == "localhost_only"`
        **and** `is_local_camera_available()` (reachy_mini.py:338). On a unit
        without a reachable camera IPC endpoint -- the Lite variant, or any robot
        whose daemon holds the camera -- it silently falls back to **WebRTC**,
        which then dies with `Connection refused` because nothing is serving the
        signalling port. That took out two robots on the first live run.

        So we ask for backends explicitly, in descending order of capability, and
        end at `no_media`. A robot that moves in silence is enormously better
        than a robot that does nothing: the motion is the performance, the stem
        merely reinforces it.
        """
        if self.reachy is not None:
            return
        if self.dry_run:
            log.warning("dry-run: not connecting to the robot")
            return

        from reachy_mini import ReachyMini

        # "local"              -> IPC to the daemon, coexists with it
        # "gstreamer_no_video" -> direct GStreamer, audio only, no camera needed
        # "no_media"           -> motion only; always available
        candidates = ["local", "gstreamer_no_video", "no_media"] if want_media else ["no_media"]

        last_err: Exception | None = None
        for backend in candidates:
            try:
                self.reachy = ReachyMini(
                    connection_mode="localhost_only",
                    port=self.daemon_port,
                    media_backend=backend,
                    automatic_body_yaw=True,
                )
                self._media_on = backend != "no_media"
                self.media_backend = backend
                if backend != candidates[0]:
                    log.warning(
                        "media backend %r failed; fell back to %r%s",
                        candidates[0], backend,
                        " -- THIS ROBOT WILL BE SILENT" if backend == "no_media" else "",
                    )
                log.info("connected to local daemon (media=%s)", backend)
                return
            except Exception as e:
                last_err = e
                log.warning("media backend %r unusable: %s", backend, e)
                # Half-built ReachyMini objects hold a socket; drop it before retrying.
                self.disconnect()

        raise RuntimeError(
            f"could not connect to the local daemon on any media backend: {last_err}"
        )

    def disconnect(self) -> None:
        """Close the robot connection, giving back anything we took.

        The re-acquire is the subtle part. Asking for `no_media` makes the SDK
        call `release_media()` on our behalf, which hands the audio+camera
        hardware away from the daemon -- and the daemon's media server is what
        creates `/tmp/reachymini_camera_socket`. Leave it released and the robot
        looks, to every later caller, like a unit with no camera.

        The SDK's own `__exit__` re-acquires for exactly this reason
        (reachy_mini.py:181-186). We do not use the context manager here, so we
        have to replicate it explicitly -- an earlier version did not, and left
        three robots with their media released after every single show.
        """
        r, self.reachy = self.reachy, None
        if r is None:
            return
        for fn in (
            lambda: r.acquire_media() if r.media_released else None,
            lambda: r.media.close(),
            lambda: r.client.disconnect(),
        ):
            try:
                fn()
            except Exception as e:
                log.debug("disconnect step failed (continuing): %s", e)

    # -- commands --------------------------------------------------------
    def op_hello(self, _req: dict) -> dict:
        import platform

        try:
            import reachy_mini

            sdk = reachy_mini.__version__
        except Exception:
            sdk = "?"
        return {
            "agent_version": AGENT_VERSION,
            "sdk_version": sdk,
            "python": sys.executable,
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "base_dir": str(self.base_dir),
        }

    def op_ping(self, req: dict) -> dict:
        """Clock-sync probe. Timestamps are this robot's ``time.monotonic()``.

        The PC pairs t_recv/t_send with its own send/receive instants and runs
        Cristian's algorithm. Handled inline on the connection thread and kept
        free of any locking, because anything that delays this reply asymmetrically
        biases the offset estimate.
        """
        t_recv = time.monotonic()
        return {
            "seq": req.get("seq"),
            "t_pc": req.get("t_pc"),
            "t_recv": t_recv,
            "t_send": time.monotonic(),
        }

    def op_load(self, req: dict) -> dict:
        name = str(req.get("program", "")).strip()
        if not name:
            raise ValueError("load requires 'program'")
        path = self.base_dir / "programs" / name
        if not path.exists():
            for cand in (
                path.with_suffix(".json.gz"),
                self.base_dir / "programs" / f"{name}.program.json.gz",
            ):
                if cand.exists():
                    path = cand
                    break
        if not path.exists():
            avail = sorted(
                p.name for p in (self.base_dir / "programs").glob("*") if p.is_file()
            )
            raise FileNotFoundError(f"no program {name!r}; available: {avail}")

        p = Program.load(path)
        if self.fps_override:
            p.fps = self.fps_override
        with self.lock:
            self.program = p
            self.program_name = path.name
        self._set_state(STATE_LOADED)

        audio_ok = True
        if p.audio_file:
            audio_ok = (self.base_dir / "audio" / p.audio_file).exists()

        return {
            "program": path.name,
            "n_frames": p.n_frames,
            "duration": round(p.duration, 3),
            "fps": p.fps,
            "anchors": len(p.anchors),
            "audio": p.audio_file,
            "audio_present": audio_ok,
            "audio_lead_ms": p.audio_lead_ms,
        }

    def op_arm(self, _req: dict) -> dict:
        p = self.program
        if p is None:
            raise RuntimeError("nothing loaded -- call load first")

        self._cut.clear()
        want_media = bool(p.audio_file)
        self.connect(want_media)
        if self.dry_run:
            self._set_state(STATE_ARMED)
            return {"armed": True, "dry_run": True}

        r = self.reachy
        # ORDER MATTERS on SDK 1.9.0. enable_motors() pins every target to the
        # *present* pose before flipping torque on, so `set_target(X);
        # enable_motors()` no longer drives to X (reachy_mini.py:539-542).
        # Arm first, then command the start pose.
        r.enable_motors()
        time.sleep(0.2)

        # 音声ストリーマを用意する。パイプラインの起動とレイテンシ測定は
        # ここで一度だけ済ませ、START の瞬間には何もしないで済むようにする。
        self.audio_stream = None
        if p.audio_file and self._media_on:
            audio_path = self.base_dir / "audio" / p.audio_file
            if not audio_path.exists():
                log.error("no audio stem at %s -- this robot will be silent", audio_path)
            else:
                try:
                    from audio_stream import AudioStream, read_wav_f32

                    pcm, sr = read_wav_f32(audio_path)
                    st = AudioStream(
                        r, pcm, sr,
                        lead_ms=p.audio_lead_ms,
                        use_net_clock=self.use_net_clock,
                    )
                    info = st.prepare()
                    if sr != info["rate"]:
                        log.error(
                            "stem is %dHz but the pipeline is %dHz. A rate mismatch "
                            "breaks both pitch and speed -- re-render it on "
                            "the PC", sr, info["rate"])
                    else:
                        self.audio_stream = st
                        log.info("audio ready: %s", info)
                except Exception as e:
                    log.exception("audio setup failed; continuing silent: %s", e)

        start = p.frames[0] if p.n_frames else np.zeros(N_CHANNELS)
        head = head_matrices(start[None, CH_X : CH_YAW + 1])[0]
        r.goto_target(
            head=head,
            antennas=[float(start[CH_ANT_R]), float(start[CH_ANT_L])],
            body_yaw=float(start[CH_BODY_YAW]),
            duration=2.0,
        )
        self._set_state(STATE_ARMED)
        return {
            "armed": True,
            "media": self._media_on,
            "media_backend": self.media_backend,
            "silent": bool(want_media and not self._media_on),
            "audio_ready": self.audio_stream is not None,
            "audio": self.audio_stream.stats if self.audio_stream else None,
        }

    def op_disarm(self, _req: dict) -> dict:
        """Cut torque AND drop the robot connection.

        Disconnecting matters as much as disabling torque. While a ReachyMini is
        alive with a media backend, this process holds the ALSA device -- and a
        held ALSA device makes the daemon's own media server fail to start with
        `Could not open audio device for recording. Device is being used by
        another application`, which in turn means no camera IPC socket. In other
        words, an agent that stays connected can silently break the daemon's
        media on the next restart. Observed on this fleet: agents still holding
        /dev/snd hours after a show finished.

        Re-arming reconnects, so nothing is lost by letting go here.
        """
        self._cut.set()
        self._stop.set()
        self._stop_audio()
        self.audio_stream = None
        if self.recorder is not None:
            # 録音スレッドを残したまま切ると ALSA を掴んだままになる。
            # このファームでは、それがデーモンのメディア再起動を丸ごと殺す。
            try:
                self.recorder.stop()
            except Exception as e:
                log.warning("could not stop the recording: %s", e)
            self.recorder = None
        if self.reachy is not None and not self.dry_run:
            try:
                self.reachy.disable_motors()
            except Exception as e:
                log.warning("disable_motors failed: %s", e)
        self.disconnect()
        self._set_state(STATE_IDLE)
        return {"disarmed": True, "released": True}

    def op_volume(self, req: dict) -> dict:
        """Set the speaker volume (0-100), safely, **during a performance.**

        There are three ways to change the volume on this hardware and only one
        of them is usable while the robot is playing:

        - `POST /api/volume/set` on the daemon **plays `impatient1.wav` every
          time** (routers/volume.py). Mid-show that is a foreign noise coming
          out of one robot. There is no parameter to suppress it.
        - Opening a second `ws://<ip>:8000/ws/sdk` session would avoid the
          sound, but a second session competing with the one carrying the
          performance is not a risk worth taking.
        - **This.** The agent already holds an SDK session from `arm`, so the
          command rides the connection that is already there. It reaches the
          same `VolumeControl` singleton as the REST route, plays nothing, and
          never touches the GStreamer pipeline.

        `send_command` returns immediately. The daemon's own mixer write can
        take up to 2 s on units without pulsectl (it shells out to `amixer`),
        but that happens on the far side and does not block this reply.
        """
        if self.reachy is None:
            raise RuntimeError("not connected -- arm first")
        # Imported here, not at module scope. If a robot's own SDK turns out to
        # lack this command, a top-level import would stop the agent from
        # starting at all -- trading one missing feature for nine dead robots.
        from reachy_mini.io.protocol import SetVolumeCmd

        v = max(0, min(100, int(req.get("value", 0))))
        if not self.dry_run:
            self.reachy.client.send_command(SetVolumeCmd(volume=v))
        self.volume = v
        return {"volume": v}

    def op_start(self, req: dict) -> dict:
        """Schedule playback at an absolute instant on THIS robot's clock.

        The PC has already converted its own T0 into our timebase using the
        measured offset, so all the agent has to do is wait. Because START is
        sent seconds early, its own transit jitter is irrelevant -- which is the
        entire point of decoupling *when the message arrives* from *when the
        motion begins*.
        """
        p = self.program
        if p is None:
            raise RuntimeError("nothing loaded")
        if self.state not in (STATE_ARMED, STATE_DONE, STATE_ABORTED):
            raise RuntimeError(f"cannot start from state {self.state!r}; arm first")

        t0_local = req.get("t0_local")
        if t0_local is None:
            raise ValueError("start requires 't0_local' (this robot's monotonic clock)")
        t0_local = float(t0_local)

        lead = float(req.get("lead_s", 0.0))
        now = time.monotonic()
        if t0_local < now:
            raise ValueError(
                f"t0_local is {now - t0_local:.3f}s in the past -- clock sync is "
                "stale or the PC scheduled too aggressively"
            )

        self._stop.clear()
        self._abort.clear()
        self._cut.clear()
        self._fade_s = 0.0
        self._t0_local = t0_local
        self.touch()

        self._play_thread = threading.Thread(
            target=self._play, args=(p, t0_local), name="playback", daemon=True
        )
        self._set_state(STATE_SCHEDULED)
        self._play_thread.start()

        return {
            "scheduled": True,
            "t0_local": t0_local,
            "in_s": round(t0_local - now, 4),
            "duration": round(p.duration, 3),
            "lead_s": lead,
        }

    # -- microphone ------------------------------------------------------
    # 指揮者のマイクで演奏を録る。9台がそれぞれ違うステムを鳴らしているので、
    # 1本の録音から台ごとに相関で分離できる（同じ音を鳴らしていたら不可能）。
    # ここまでの全指標はスピーカーの手前までしか見ていない ―― これが唯一、
    # 実際に空気中で揃っているかを見る経路。

    def op_record_start(self, req: dict) -> dict:
        from mic_record import MicRecorder

        if self.reachy is None:
            raise RuntimeError("not connected -- arm first")
        if self.recorder is not None:
            raise RuntimeError("already recording")
        name = str(req.get("name") or "capture") + ".wav"
        out = self.base_dir / "recordings" / name
        rec = MicRecorder(self.reachy, out,
                          max_seconds=float(req.get("max_seconds", 300.0)))
        if not rec.available:
            raise RuntimeError("no microphone on this connection (connected as no_media)")
        info = rec.start()
        self.recorder = rec
        log.info("recording started: %s", out)
        return info

    def op_record_stop(self, _req: dict) -> dict:
        if self.recorder is None:
            raise RuntimeError("not recording")
        stats = self.recorder.stop()
        self.recorder = None
        log.info("recording stopped: %s", stats)
        return stats

    def op_stop(self, _req: dict) -> dict:
        self._stop.set()
        return {"stopping": True}

    def op_abort(self, req: dict) -> dict:
        """End the take: fade out over `fade_s`, or ramp to neutral without it.

        An abort with no `fade_s` also cuts short a fade already running, which
        is how the emergency stop gets through.
        """
        self._fade_s = fade_s = max(0.0, float(req.get("fade_s", 0.0)))
        if not fade_s:
            self._cut.set()
        self._abort.set()
        self._stop.set()

        # Armed but not started: no playback thread is there to see the flags,
        # and the robot would otherwise stand holding the start pose.
        p = self.program
        started = self._play_thread is not None and self._play_thread.is_alive()
        if fade_s and p is not None and self.state == STATE_ARMED and not started:
            self._set_state(STATE_ABORTED)
            threading.Thread(target=self._fade_out, args=(p, None, fade_s),
                             name="fade-out", daemon=True).start()
        return {"aborting": True}

    def op_status(self, _req: dict) -> dict:
        p = self.program
        now = time.monotonic()
        elapsed = None
        if self.state == STATE_PLAYING and self._t0_local is not None:
            elapsed = round(now - self._t0_local, 4)
        return {
            "state": self.state,
            "error": self.error,
            "program": self.program_name,
            "duration": round(p.duration, 3) if p else 0.0,
            "elapsed": elapsed,
            "connected": self.reachy is not None,
            "media_backend": self.media_backend,
            "volume": self.volume,
            "mono": now,
            "since_contact_s": round(now - self.last_contact, 2),
            "stats": self.stats,
        }

    def op_shutdown(self, _req: dict) -> dict:
        self._shutdown.set()
        self._stop.set()
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"shutdown": True}

    # -- playback --------------------------------------------------------
    def _send_frame(self, p: Program, idx: int) -> None:
        if self.dry_run or self.reachy is None:
            return
        from reachy_mini.io.protocol import SetFullTargetCmd

        row = p.frames[idx]
        # SetFullTargetCmd, not set_target(): set_target unconditionally sends a
        # second AppendRecordCmd message on every call even when not recording
        # (reachy_mini.py:547-557). One message per frame instead of two.
        self.reachy.client.send_command(
            SetFullTargetCmd(
                head=p.heads[idx].flatten().tolist(),
                antennas=[float(row[CH_ANT_R]), float(row[CH_ANT_L])],
                body_yaw=float(row[CH_BODY_YAW]),
            )
        )

    def _play(self, p: Program, t0: float) -> None:
        try:
            # 音声は明示PTSでスケジュールする。play_sound() 方式は呼ぶたびに
            # ALSA を開き直して台間で ±20-50ms ばらついたので使わない
            # （agent/audio_stream.py の冒頭に経緯を書いてある）。
            if self.audio_stream is not None and not self.dry_run:
                try:
                    info = self.audio_stream.schedule(t0)
                    log.info("audio scheduled: %s", info)
                except Exception as e:
                    log.error("could not schedule audio: %s", e)

            if not sleep_until(t0, self._stop):
                self._finish_early(p)
                return

            self._set_state(STATE_PLAYING)
            self.started_at_mono = time.monotonic()
            start_err = self.started_at_mono - t0

            n = p.n_frames
            period = 1.0 / p.fps
            sent = 0
            late = 0
            worst_late = 0.0
            last_idx = -1

            while not self._stop.is_set():
                now = time.monotonic()
                t = now - t0
                if t >= p.duration:
                    break

                # Absolute-time indexing, recomputed every tick from the clock.
                # Never an incrementing counter: a single slow tick would
                # otherwise shift the rest of the performance permanently.
                idx = p.frame_index_at(t)
                if idx != last_idx:
                    self._send_frame(p, idx)
                    sent += 1
                    last_idx = idx

                nxt = t0 + (idx + 1) * period
                behind = time.monotonic() - nxt
                if behind > 0:
                    late += 1
                    worst_late = max(worst_late, behind)
                    # Already past the next slot -- do not sleep, just re-derive
                    # the index from the clock on the next pass.
                    continue
                if not sleep_until(nxt, self._stop):
                    break

            if self._abort.is_set():
                if self._fade_s > 0:
                    self._fade_out(p, t0, self._fade_s)
                else:
                    self._ramp_to_neutral(p)
                self._set_state(STATE_ABORTED)
            else:
                if n:
                    self._send_frame(p, n - 1)
                self._stop_audio()
                self._set_state(STATE_DONE)

            self.stats = {
                "audio": (self.audio_stream.stats if self.audio_stream else None),
                "frames_sent": sent,
                "frames_expected": n,
                "start_error_ms": round(start_err * 1000, 3),
                "late_ticks": late,
                "worst_late_ms": round(worst_late * 1000, 2),
            }
            log.info("playback finished: %s", self.stats)

        except Exception:
            self._set_state(STATE_ERROR, traceback.format_exc(limit=4))
            log.exception("playback failed")
            try:
                self._ramp_to_neutral(p)
            except Exception:
                pass

    def _stop_audio(self) -> None:
        """鳴っている音を止める。

        中断したのにロボットが鳴り続けるのは、演奏としても診断としても最悪。
        `stop_playing()` ではなくフラッシュで止める（デバイスを閉じると
        次の演奏で開き直しのばらつきが戻ってくる）。
        """
        if self.audio_stream is not None:
            try:
                self.audio_stream.stop()
            except Exception as e:
                log.warning("could not stop the audio: %s", e)

    def _send_pose(self, row: np.ndarray) -> None:
        """Command a pose computed on the fly rather than a precomputed frame."""
        from reachy_mini.io.protocol import SetFullTargetCmd

        head = head_matrices(row[None, CH_X : CH_YAW + 1])[0]
        self.reachy.client.send_command(
            SetFullTargetCmd(
                head=head.flatten().tolist(),
                antennas=[float(row[CH_ANT_R]), float(row[CH_ANT_L])],
                body_yaw=float(row[CH_BODY_YAW]),
            )
        )

    def _fade_out(self, p: Program, t0: float | None, fade_s: float) -> None:
        """Stop button: let the take die away instead of cutting it off.

        The piece keeps playing underneath and is mixed into its own closing
        pose, the `frames*(1-w) + target*w` the ending is built with, so the
        motion shrinks as the head goes down. The target is the last frame and
        not the bow because that is the park pose: the one place the head can
        be left down with the torque off, already through the `song build`
        safety gate.

        `t0` of None means the take never started, so the head goes down from
        the start pose with nothing playing underneath.

        `_cut` ends this at once, so the emergency stop never waits for a fade.
        """
        begin = time.monotonic()
        end = begin + fade_s
        if self.audio_stream is not None and not self.dry_run:
            try:
                log.info("audio fading: %s", self.audio_stream.fade_out(end))
            except Exception as e:
                log.warning("could not fade the audio: %s", e)

        live = not self.dry_run and self.reachy is not None and p.n_frames > 0
        try:
            while not (self._cut.is_set() or self._shutdown.is_set()):
                now = time.monotonic()
                if now >= end:
                    break
                if live:
                    row = p.frames[0 if t0 is None else p.frame_index_at(now - t0)]
                    w = minjerk((now - begin) / fade_s)
                    self._send_pose(row + (p.frames[-1] - row) * w)
                self._cut.wait(1.0 / p.fps)
            if live and not self._cut.is_set():
                self._send_pose(p.frames[-1])
        except Exception as e:
            log.warning("fade-out failed: %s", e)

        self._stop_audio()
        if live:
            try:
                self.reachy.disable_motors()
            except Exception:
                pass

    def _ramp_to_neutral(self, p: Program | None) -> None:
        """Graceful abort: ease back to the start pose, then cut torque.

        Never a hard stop mid-motion. On a struck instrument, freezing at the
        contact point is the exact condition that stalls and heats a servo, so
        the abort path retracts first and disables second.
        """
        self._stop_audio()      # 動きより先に音を止める。無音のほうが安全側。
        if self.dry_run or self.reachy is None or p is None or p.n_frames == 0:
            return
        try:
            cur_idx = p.frame_index_at(
                (time.monotonic() - (self._t0_local or time.monotonic()))
            )
            a = p.frames[cur_idx].copy()
            b = p.frames[0].copy()
            steps = max(2, int(self.abort_ramp_s * p.fps))
            for i in range(steps + 1):
                w = minjerk(i / steps)
                self._send_pose(a + (b - a) * w)
                time.sleep(1.0 / p.fps)
        except Exception as e:
            log.warning("abort ramp failed: %s", e)
        try:
            self.reachy.disable_motors()
        except Exception:
            pass

    def _finish_early(self, p: Program) -> None:
        if self._abort.is_set() and self._fade_s > 0:
            # Stopped in the countdown. Nothing has sounded, so there is
            # nothing to fade, and what is scheduled must not start.
            self._stop_audio()
            self._fade_out(p, None, self._fade_s)
        self._set_state(STATE_ABORTED if self._abort.is_set() else STATE_IDLE)

    # -- deadman ---------------------------------------------------------
    def deadman_loop(self) -> None:
        """Park safely if the PC goes away mid-show.

        Only fires while actually playing: a silent control link between runs is
        normal and must not disturb an idle robot. When it does fire it lets the
        current phrase finish rather than stopping instantly -- a robot frozen
        mid-strike is both louder and more damaging than one that plays four
        more beats and then rests.
        """
        while not self._shutdown.is_set():
            time.sleep(0.5)
            if self.state != STATE_PLAYING:
                continue
            silent = time.monotonic() - self.last_contact
            if silent > self.deadman_timeout_s:
                log.error(
                    "deadman: no PC contact for %.1fs -- parking after this phrase",
                    silent,
                )
                self._abort.set()
                self._stop.set()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Handler(socketserver.StreamRequestHandler):
    timeout = 300

    def handle(self) -> None:
        agent: Agent = self.server.agent  # type: ignore[attr-defined]
        peer = self.client_address[0]
        log.debug("client connected: %s", peer)
        try:
            for raw in self.rfile:
                line = raw.strip()
                if not line:
                    continue
                agent.touch()
                try:
                    req = json.loads(line.decode("utf-8"))
                except Exception as e:
                    self._reply({"ok": False, "error": f"bad json: {e}"})
                    continue

                op = str(req.get("op", ""))
                fn = getattr(agent, f"op_{op}", None)
                if fn is None:
                    self._reply({"ok": False, "error": f"unknown op {op!r}"})
                    continue
                try:
                    result = fn(req)
                    self._reply({"ok": True, "op": op, **(result or {})})
                except Exception as e:
                    log.warning("op %s failed: %s", op, e)
                    self._reply(
                        {"ok": False, "op": op, "error": f"{type(e).__name__}: {e}"}
                    )
        except (ConnectionResetError, BrokenPipeError, socket.timeout):
            pass
        finally:
            log.debug("client disconnected: %s", peer)

    def _reply(self, payload: dict) -> None:
        self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # Deliberately NOT allow_reuse_address: a bind failure is how we detect a
    # second agent already running on this robot. Two agents would both write
    # targets and silently fight -- the SDK's app lock does not cover SDK
    # clients (robot_app_lock.py:1-14), so this bind is the only mutual
    # exclusion available.
    allow_reuse_address = False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reachy Mini Orchestra on-robot agent")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--daemon-port", type=int, default=8000)
    ap.add_argument(
        "--base-dir",
        default=str(Path(__file__).resolve().parent),
        help="directory holding programs/ and audio/",
    )
    ap.add_argument("--fps", type=float, default=None, help="override program fps")
    ap.add_argument("--abort-ramp", type=float, default=0.5)
    ap.add_argument("--deadman", type=float, default=5.0)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="run the protocol and timing without touching the robot",
    )
    ap.add_argument(
        "--net-clock", action="store_true",
        help="use a shared network clock for audio (needs ORCHESTRA_CLOCK_HOST). "
             "Measured drift is only -16 to -27 ppm, so this is rarely needed",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    agent = Agent(
        base_dir=Path(args.base_dir),
        daemon_port=args.daemon_port,
        fps=args.fps,
        abort_ramp_s=args.abort_ramp,
        deadman_timeout_s=args.deadman,
        dry_run=args.dry_run,
        use_net_clock=args.net_clock,
    )

    try:
        server = Server(("0.0.0.0", args.port), Handler)
    except OSError as e:
        print(
            f"cannot bind port {args.port}: {e}\n"
            "Another orchestra agent is probably already running on this robot. "
            "Two agents would fight over the same targets -- stop the other one "
            "first (orchestra fleet agent stop).",
            file=sys.stderr,
        )
        return 1
    server.agent = agent  # type: ignore[attr-defined]

    threading.Thread(target=agent.deadman_loop, daemon=True).start()

    def shutdown(_sig, _frm):
        log.info("signal received, shutting down")
        agent._shutdown.set()
        agent._stop.set()
        try:
            if agent.reachy is not None and not agent.dry_run:
                agent.reachy.disable_motors()
        except Exception:
            pass
        agent.disconnect()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        "orchestra agent %s listening on 0.0.0.0:%d (base=%s, dry_run=%s)",
        AGENT_VERSION, args.port, args.base_dir, args.dry_run,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        agent.disconnect()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
