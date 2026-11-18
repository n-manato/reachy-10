"""The compiled per-robot program: a dense, timed frame table.

This is what actually ships to a robot. It is deliberately compact -- 9 floats
per frame in SI units rather than a 4x4 matrix (22 numbers) -- because a
3-minute song at 100 Hz is 18,000 frames per robot and the agent rebuilds the
matrices locally at load time anyway.

The frame grid is strictly uniform. That matters: the SDK's own ``RecordedMove``
computes ``duration = len(traj) * mean_dt`` and then *raises* when evaluated at
``t >= timestamps[-1]`` (motion/recorded_move.py:84,110), and the daemon-side
playback loop has no guard against it (backend/abstract.py:625). A uniform grid
plus a padded trailing frame makes that failure mode unreachable.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .interp import head_pose_matrices

# Frame column layout. Keep this ordering stable -- the agent depends on it.
CH_X, CH_Y, CH_Z = 0, 1, 2
CH_ROLL, CH_PITCH, CH_YAW = 3, 4, 5
CH_BODY_YAW = 6
CH_ANT_R, CH_ANT_L = 7, 8
N_CHANNELS = 9

CHANNEL_NAMES = (
    "x", "y", "z", "roll", "pitch", "yaw", "body_yaw", "antenna_r", "antenna_l",
)

FORMAT = "reachy-orchestra-program"
FORMAT_VERSION = 1

# Frames of held final pose appended by `to_recorded_move`. See its docstring --
# this protects the daemon-side fallback player, which lacks the SDK player's
# end-of-clip clamp.
_TAIL_PAD_FRAMES = 5


@dataclass
class AudioSpec:
    """Per-robot audio stem.

    `lead_ms` compensates the constant GStreamer->ALSA latency on that specific
    robot so the sound reaches the speaker at the same instant the actuator
    moves. Same concept as the SDK's own ``audio_lead_s``
    (backend/abstract.py:588-619). Measured per robot; see
    ``orchestra calibrate audio``.
    """

    file: str
    lead_ms: float = 0.0
    gain: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "lead_ms": self.lead_ms, "gain": self.gain}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AudioSpec":
        return AudioSpec(
            file=str(d["file"]),
            lead_ms=float(d.get("lead_ms", 0.0)),
            gain=float(d.get("gain", 1.0)),
        )


@dataclass
class Program:
    """A fully compiled, robot-ready performance."""

    robot_id: str
    song: str = ""
    part: str = ""
    instrument: str | None = None
    fps: float = 100.0
    frames: np.ndarray = field(default_factory=lambda: np.zeros((0, N_CHANNELS)))
    anchors: np.ndarray = field(default_factory=lambda: np.zeros(0))
    """音が鳴る瞬間（秒）。

    物理楽器が無い構成では、これは「スピーカーから音が出る時刻」であり、
    ジェスチャがそこに合っているかの基準になる。安全検査（重なり・デューティ比）
    もここを見る ―― 楽器が無くてもサーボの負荷は変わらないため。
    """

    audio: AudioSpec | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- derived ---------------------------------------------------------
    @property
    def n_frames(self) -> int:
        return int(self.frames.shape[0])

    @property
    def duration(self) -> float:
        """Seconds. The last frame is at ``(n-1)/fps``."""
        return max(0.0, (self.n_frames - 1) / self.fps)

    def times(self) -> np.ndarray:
        return np.arange(self.n_frames, dtype=np.float64) / self.fps

    def head_matrices(self) -> np.ndarray:
        """(N,4,4) head poses. Built on demand; the agent caches the result."""
        return head_pose_matrices(self.frames[:, CH_X : CH_YAW + 1])

    def frame_index_at(self, t: float) -> int:
        """Nearest frame index for elapsed time t, clamped to the program.

        Absolute-time indexing, deliberately: the agent recomputes this from
        ``monotonic() - t0`` every tick rather than incrementing a counter, so a
        slow tick cannot drift the rest of the show.
        """
        if self.n_frames == 0:
            return 0
        idx = int(round(t * self.fps))
        return max(0, min(self.n_frames - 1, idx))

    # -- IO --------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "robot_id": self.robot_id,
            "song": self.song,
            "part": self.part,
            "instrument": self.instrument,
            "fps": self.fps,
            "n_frames": self.n_frames,
            "duration": round(self.duration, 6),
            "encoding": "xyzrpy_bodyyaw_antennas / SI (m, rad)",
            "channels": list(CHANNEL_NAMES),
            "audio": self.audio.to_dict() if self.audio else None,
            "anchors": [round(float(a), 6) for a in self.anchors],
            "validation": self.validation,
            "meta": self.meta,
            # Rounded to 6 dp: ~1 micron / ~0.2 millidegree, far below anything
            # the hardware can resolve, and it roughly halves the file size.
            "frames": [[round(float(v), 6) for v in row] for row in self.frames],
        }

    def save(self, path: str | Path) -> Path:
        """Write gzipped JSON. Roughly 3x smaller on the wire than plain JSON."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8")
        if path.suffix == ".gz":
            path.write_bytes(gzip.compress(payload, compresslevel=6))
        else:
            path.write_bytes(payload)
        return path

    @staticmethod
    def load(path: str | Path) -> "Program":
        path = Path(path)
        raw = path.read_bytes()
        if path.suffix == ".gz" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return Program.from_dict(json.loads(raw.decode("utf-8")))

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Program":
        if d.get("format") != FORMAT:
            raise ValueError(f"not a {FORMAT} file (got format={d.get('format')!r})")
        if int(d.get("version", 0)) > FORMAT_VERSION:
            raise ValueError(
                f"program format version {d['version']} is newer than this build "
                f"supports ({FORMAT_VERSION}); update the agent"
            )
        frames = np.asarray(d.get("frames", []), dtype=np.float64)
        if frames.size == 0:
            frames = np.zeros((0, N_CHANNELS), dtype=np.float64)
        elif frames.shape[1] != N_CHANNELS:
            raise ValueError(
                f"expected {N_CHANNELS} channels per frame, got {frames.shape[1]}"
            )
        audio = d.get("audio")
        return Program(
            robot_id=str(d.get("robot_id", "")),
            song=str(d.get("song", "")),
            part=str(d.get("part", "")),
            instrument=d.get("instrument"),
            fps=float(d.get("fps", 100.0)),
            frames=frames,
            anchors=np.asarray(d.get("anchors", []), dtype=np.float64),
            audio=AudioSpec.from_dict(audio) if audio else None,
            validation=dict(d.get("validation", {})),
            meta=dict(d.get("meta", {})),
        )

    # -- interop ---------------------------------------------------------
    def to_recorded_move(self) -> dict[str, Any]:
        """Convert to the SDK's native ``RecordedMove`` schema.

        Used by the fallback sync path (``UploadMoveStartCmd`` ->
        ``PlayUploadedMoveCmd``), and by ``reachy.play_move()`` during
        single-robot preview. Schema per motion/recorded_move.py:78-80.

        **On the trailing pad.** ``RecordedMove`` computes
        ``dt = (time[-1] - time[0]) / len(time)`` and
        ``duration = len(trajectory) * dt`` (recorded_move.py:84-91), so for any
        clip starting at t=0 the duration is *identically* ``time[-1]`` -- no
        amount of padding changes that. Meanwhile ``evaluate()`` raises at
        ``t >= time[-1]`` (recorded_move.py:110). The SDK-side player clamps with
        ``min(t, duration - 1e-2)`` (reachy_mini.py:1078) and is therefore safe,
        but the daemon-side loop used by ``play_uploaded_move`` does not
        (abstract.py:625) and can read a ``t`` that has advanced past the loop
        condition between the check and the call.

        So the pad is not there to change the arithmetic. It is there so the last
        ``_TAIL_PAD_FRAMES`` of the clip hold the final pose unchanged: if the
        daemon does evaluate in that region the robot is already at its end pose,
        and if it raises instead, the motion has visibly completed. This is
        belt-and-braces for the fallback path only -- the on-robot agent uses
        :class:`Program` directly with its own clamped indexing.
        """
        heads = self.head_matrices()
        times = list(self.times())
        data = [
            {
                "head": heads[i].tolist(),
                "antennas": [
                    float(self.frames[i, CH_ANT_R]),
                    float(self.frames[i, CH_ANT_L]),
                ],
                "body_yaw": float(self.frames[i, CH_BODY_YAW]),
            }
            for i in range(self.n_frames)
        ]
        for _ in range(_TAIL_PAD_FRAMES if data else 0):
            times.append(times[-1] + 1.0 / self.fps)
            data.append(dict(data[-1]))
        return {
            "description": f"{self.song}/{self.robot_id}/{self.instrument or self.part}",
            "time": times,
            "set_target_data": data,
        }


def concat(programs: list[Program]) -> Program:
    """Concatenate programs end to end, preserving anchors. Same robot assumed."""
    if not programs:
        raise ValueError("nothing to concatenate")
    base = programs[0]
    frames = np.concatenate([p.frames for p in programs], axis=0)
    anchors: list[float] = []
    offset = 0.0
    for p in programs:
        anchors.extend((p.anchors + offset).tolist())
        offset += p.n_frames / p.fps
    out = Program(
        robot_id=base.robot_id,
        song=base.song,
        part=base.part,
        instrument=base.instrument,
        fps=base.fps,
        frames=frames,
        anchors=np.asarray(sorted(anchors), dtype=np.float64),
        audio=base.audio,
        meta=dict(base.meta),
    )
    return out
