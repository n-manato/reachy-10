"""The authored clip format: beat-relative keyframes, per-channel tracks.

A clip is what a human edits. It is deliberately NOT the format sent to a robot
-- see :mod:`orchestra.motion.program` for that. Two properties make the
authoring format worth keeping separate:

* **Time is in beats, not seconds.** A clip is reusable at any tempo and binds
  directly to a MIDI tempo map. Seconds are derived at compile time.
* **`anchors` mark the instant sound is produced**, not the start of the motion.
  For a struck instrument the windup must *precede* the beat. Anchoring on
  contact is what makes strikes land musically instead of late.

Values are authored in whatever units are convenient (mm/deg by default) and
normalised to SI (m/rad) on load. The original units are preserved so a clip
round-trips through load/save unchanged.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .interp import INTERPOLATIONS, MIN_JERK

HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
TRANSLATION_AXES = ("x", "y", "z")

# Placement modes.
#   TRIGGER -- the clip is instantiated once per musical event, with anchors[0]
#              aligned to that event's time. Use for strikes and one-shots.
#   PATTERN -- the clip loops continuously on the beat grid, independent of
#              individual notes. Use for shakers, sways and conductor beats.
TRIGGER = "trigger"
PATTERN = "pattern"
PLACEMENTS = (TRIGGER, PATTERN)


class ClipError(ValueError):
    """A clip file is structurally invalid. Raised at load, never at play."""


@dataclass
class Track:
    """One channel of keyframes, already normalised to SI units.

    `beats` is strictly increasing, `values` is (K, D), and `interps[i]` eases
    the segment *leaving* keyframe i (the last entry is unused but kept so the
    arrays stay parallel).
    """

    name: str
    beats: np.ndarray
    values: np.ndarray
    interps: list[str]

    @property
    def dim(self) -> int:
        return int(self.values.shape[1])

    @property
    def span(self) -> tuple[float, float]:
        return float(self.beats[0]), float(self.beats[-1])


@dataclass
class Clip:
    """An authored motion clip."""

    name: str
    version: int = 1
    instrument: str | None = None
    description: str = ""
    placement: str = PATTERN
    loop_beats: float = 1.0
    anchors: list[float] = field(default_factory=list)
    """音が鳴る瞬間（拍単位）。

    物理楽器を廃止したので「打鍵の瞬間」ではなく「スピーカーから音が出る瞬間」。
    コンパイラはここを音符に合わせてクリップを配置するので、振りかぶりが
    音より先に来る。副次モーションの消音と、ずれ計測の基準も兼ねる。
    """
    tracks: dict[str, Track] = field(default_factory=dict)
    max_joint_vel_frac: float | None = None
    source_path: Path | None = None
    raw_units: dict[str, str] = field(
        default_factory=lambda: {"translation": "mm", "rotation": "deg"}
    )

    # -- convenience -----------------------------------------------------
    @property
    def makes_sound(self) -> bool:
        """音を出す瞬間が定義されているか。

        物理楽器があった頃は「打鍵するか」だったが、今は「音が鳴る瞬間が
        分かっているか」。アンカーの意味そのものが変わった。
        """
        return bool(self.anchors)

    @property
    def primary_anchor(self) -> float:
        """The anchor aligned to a musical event in TRIGGER placement."""
        return self.anchors[0] if self.anchors else 0.0

    def duration_beats(self) -> float:
        """Authored extent in beats.

        For a looping pattern this is `loop_beats`; for a trigger clip it is the
        last keyframe, since a one-shot may be shorter or longer than a beat.
        """
        if self.placement == PATTERN:
            return self.loop_beats
        last = 0.0
        for tr in self.tracks.values():
            last = max(last, float(tr.beats[-1]))
        return last

    def track(self, name: str) -> Track | None:
        return self.tracks.get(name)

    def scaled(self, head: float = 1.0, antennas: float = 1.0) -> "Clip":
        """振幅だけを一律に縮めた複製を返す。タイミングは変えない。

        関節速度は「振幅 ÷ 時間」で決まるので、形（＝タイミングと軌道の比率）を
        保ったまま安全な範囲に収めるには、これが一番副作用が少ない。
        テンポを落とすと曲が変わってしまい、キーフレームを個別に触ると
        形が崩れる。

        アンテナを別係数にしているのは、xc330 が sts3215 の 1/4 のトルクしか
        無く、速度上限も別に設けてあるため。頭が余裕でもアンテナだけ超える
        （逆も）ことが普通に起きる。
        """
        import copy

        out = copy.deepcopy(self)
        for name, tr in out.tracks.items():
            k = antennas if name == "antennas" else head
            if k == 1.0:
                continue
            if name == "antennas":
                # アンテナは絶対角なので、中立(±10度相当)からの差分を縮める。
                # 素朴に全体へ掛けると開き具合そのものが変わってしまう。
                base = tr.values.mean(axis=0, keepdims=True)
                tr.values = base + (tr.values - base) * k
            else:
                tr.values = tr.values * k
        return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _unit_scale(raw_units: dict[str, str]) -> tuple[float, float]:
    """Return (translation_scale_to_m, rotation_scale_to_rad)."""
    t = str(raw_units.get("translation", "mm")).lower()
    r = str(raw_units.get("rotation", "deg")).lower()
    if t in ("mm", "millimeter", "millimetre"):
        ts = 1e-3
    elif t in ("m", "meter", "metre"):
        ts = 1.0
    else:
        raise ClipError(f"units.translation must be 'mm' or 'm', got {t!r}")
    if r in ("deg", "degree", "degrees"):
        rs = math.pi / 180.0
    elif r in ("rad", "radian", "radians"):
        rs = 1.0
    else:
        raise ClipError(f"units.rotation must be 'deg' or 'rad', got {r!r}")
    return ts, rs


def _parse_keys(
    raw: Iterable[dict[str, Any]],
    name: str,
    extract,
    dim: int,
) -> Track:
    beats: list[float] = []
    values: list[list[float]] = []
    interps: list[str] = []

    for i, kf in enumerate(raw):
        if "beat" not in kf:
            raise ClipError(f"track {name!r} keyframe {i} is missing 'beat'")
        beat = float(kf["beat"])
        interp = str(kf.get("interp", MIN_JERK))
        if interp not in INTERPOLATIONS:
            raise ClipError(
                f"track {name!r} keyframe {i}: unknown interp {interp!r}; "
                f"expected one of {list(INTERPOLATIONS)}"
            )
        vals = extract(kf, i)
        if len(vals) != dim:
            raise ClipError(
                f"track {name!r} keyframe {i}: expected {dim} value(s), got {len(vals)}"
            )
        beats.append(beat)
        values.append(list(vals))
        interps.append(interp)

    if not beats:
        raise ClipError(f"track {name!r} has no keyframes")

    order = np.argsort(np.asarray(beats, dtype=np.float64), kind="stable")
    b = np.asarray(beats, dtype=np.float64)[order]
    v = np.asarray(values, dtype=np.float64)[order]
    ip = [interps[i] for i in order]

    if np.any(np.diff(b) < 0):
        raise ClipError(f"track {name!r}: keyframe beats must be sortable")
    dupes = np.where(np.diff(b) == 0.0)[0]
    if len(dupes):
        raise ClipError(
            f"track {name!r}: duplicate keyframe at beat {b[dupes[0]]:.4f}. "
            "Two keys at the same time are ambiguous -- separate them or delete one."
        )

    return Track(name=name, beats=b, values=v, interps=ip)


def load_clip(path: str | Path) -> Clip:
    """Load and structurally validate a `.clip.json` file.

    Raises :class:`ClipError` with an actionable message. Every failure mode here
    is a mistake in a hand-edited file, so the messages name the track and the
    keyframe index.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ClipError(f"{path}: invalid JSON -- {e}") from e

    return clip_from_dict(data, source_path=path)


def clip_from_dict(data: dict[str, Any], source_path: Path | None = None) -> Clip:
    where = str(source_path) if source_path else "<clip>"

    name = data.get("name") or (source_path.stem.replace(".clip", "") if source_path else "")
    if not name:
        raise ClipError(f"{where}: clip needs a 'name'")

    raw_units = dict(data.get("units", {"translation": "mm", "rotation": "deg"}))
    tscale, rscale = _unit_scale(raw_units)

    placement = str(data.get("placement", "")).lower()
    anchors_raw = data.get("anchors")
    if anchors_raw is None and "anchor_beat" in data:
        anchors_raw = [data["anchor_beat"]]
    anchors = [float(a) for a in (anchors_raw or [])]

    if not placement:
        # A clip with an anchor is a strike unless it says otherwise; anything
        # else loops on the grid.
        placement = TRIGGER if anchors else PATTERN
    if placement not in PLACEMENTS:
        raise ClipError(f"{where}: placement must be one of {list(PLACEMENTS)}")


    loop_beats = float(data.get("loop_beats", 1.0))
    if loop_beats <= 0:
        raise ClipError(f"{where}: loop_beats must be > 0, got {loop_beats}")

    raw_tracks = data.get("tracks")
    if not raw_tracks:
        raise ClipError(f"{where}: clip has no 'tracks'")

    tracks: dict[str, Track] = {}

    if "head" in raw_tracks:
        def head_extract(kf: dict[str, Any], i: int) -> list[float]:
            pose = kf.get("pose", {})
            if not isinstance(pose, dict):
                raise ClipError(f"head keyframe {i}: 'pose' must be an object")
            unknown = set(pose) - set(HEAD_AXES)
            if unknown:
                raise ClipError(
                    f"head keyframe {i}: unknown axis {sorted(unknown)}; "
                    f"valid axes are {list(HEAD_AXES)}"
                )
            # Omitted axes hold neutral (0). This is what keeps clips readable:
            # a clip states only what it actually moves.
            return [
                float(pose.get(ax, 0.0)) * (tscale if ax in TRANSLATION_AXES else rscale)
                for ax in HEAD_AXES
            ]

        tracks["head"] = _parse_keys(raw_tracks["head"], "head", head_extract, 6)

    if "body_yaw" in raw_tracks:
        def yaw_extract(kf: dict[str, Any], i: int) -> list[float]:
            if "value" not in kf:
                raise ClipError(f"body_yaw keyframe {i} is missing 'value'")
            return [float(kf["value"]) * rscale]

        tracks["body_yaw"] = _parse_keys(
            raw_tracks["body_yaw"], "body_yaw", yaw_extract, 1
        )

    if "antennas" in raw_tracks:
        def ant_extract(kf: dict[str, Any], i: int) -> list[float]:
            v = kf.get("value")
            if v is None:
                raise ClipError(f"antennas keyframe {i} is missing 'value'")
            if np.isscalar(v):
                # A scalar means a symmetric flare: [-v, +v]. Antennas point
                # outward in opposite directions, so this is the common case.
                return [-float(v) * rscale, float(v) * rscale]
            v = list(v)
            if len(v) != 2:
                raise ClipError(
                    f"antennas keyframe {i}: value must be [right, left] or a scalar"
                )
            return [float(v[0]) * rscale, float(v[1]) * rscale]

        tracks["antennas"] = _parse_keys(
            raw_tracks["antennas"], "antennas", ant_extract, 2
        )

    unknown_tracks = set(raw_tracks) - {"head", "body_yaw", "antennas"}
    if unknown_tracks:
        raise ClipError(
            f"{where}: unknown track(s) {sorted(unknown_tracks)}. "
            "Reachy Mini exposes exactly three command channels: "
            "head (6-DOF pose), body_yaw, antennas."
        )

    limits = data.get("limits", {})
    vel_frac = limits.get("max_joint_vel_frac")

    clip = Clip(
        name=str(name),
        version=int(data.get("version", 1)),
        instrument=data.get("instrument"),
        description=str(data.get("description", "")),
        placement=placement,
        loop_beats=loop_beats,
        anchors=anchors,
        tracks=tracks,
        max_joint_vel_frac=float(vel_frac) if vel_frac is not None else None,
        source_path=source_path,
        raw_units=raw_units,
    )

    _check_semantics(clip, where)
    return clip


def _check_semantics(clip: Clip, where: str) -> None:
    """Catch authoring mistakes that parse fine but will not behave."""
    for anchor in clip.anchors:
        if anchor < 0:
            raise ClipError(f"{where}: anchor {anchor} is negative")
        if clip.placement == PATTERN and anchor > clip.loop_beats:
            raise ClipError(
                f"{where}: anchor at beat {anchor} lies outside the "
                f"{clip.loop_beats}-beat loop"
            )

    if clip.placement == PATTERN:
        # A looping clip whose first and last keyframe differ will visibly jump
        # at the loop point. Warn loudly rather than silently producing a glitch
        # once per bar for the whole song.
        for tr in clip.tracks.values():
            if tr.beats[-1] < clip.loop_beats - 1e-9:
                continue
            if not np.allclose(tr.values[0], tr.values[-1], atol=1e-6):
                raise ClipError(
                    f"{where}: track {tr.name!r} is a PATTERN spanning the full "
                    f"{clip.loop_beats}-beat loop but its first and last keyframes "
                    "differ, which will jump at every loop boundary. Make them "
                    "equal, or shorten the last keyframe past the loop point."
                )


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def clip_to_dict(clip: Clip) -> dict[str, Any]:
    """Serialise back to the authored form, restoring the clip's own units."""
    tscale, rscale = _unit_scale(clip.raw_units)

    out: dict[str, Any] = {
        "name": clip.name,
        "version": clip.version,
    }
    if clip.instrument:
        out["instrument"] = clip.instrument
    if clip.description:
        out["description"] = clip.description
    out["units"] = dict(clip.raw_units)
    out["placement"] = clip.placement
    out["loop_beats"] = round(clip.loop_beats, 6)
    if clip.anchors:
        out["anchors"] = [round(a, 6) for a in clip.anchors]

    tracks: dict[str, Any] = {}
    if (tr := clip.track("head")) is not None:
        keys = []
        for i in range(len(tr.beats)):
            pose = {}
            for j, ax in enumerate(HEAD_AXES):
                v = tr.values[i, j]
                if abs(v) < 1e-12:
                    continue  # omitted == neutral; keeps files readable
                pose[ax] = round(
                    v / (tscale if ax in TRANSLATION_AXES else rscale), 4
                )
            keys.append(
                {"beat": round(float(tr.beats[i]), 6), "pose": pose, "interp": tr.interps[i]}
            )
        tracks["head"] = keys

    if (tr := clip.track("body_yaw")) is not None:
        tracks["body_yaw"] = [
            {
                "beat": round(float(tr.beats[i]), 6),
                "value": round(float(tr.values[i, 0]) / rscale, 4),
                "interp": tr.interps[i],
            }
            for i in range(len(tr.beats))
        ]

    if (tr := clip.track("antennas")) is not None:
        tracks["antennas"] = [
            {
                "beat": round(float(tr.beats[i]), 6),
                "value": [
                    round(float(tr.values[i, 0]) / rscale, 4),
                    round(float(tr.values[i, 1]) / rscale, 4),
                ],
                "interp": tr.interps[i],
            }
            for i in range(len(tr.beats))
        ]

    out["tracks"] = tracks
    if clip.max_joint_vel_frac is not None:
        out["limits"] = {"max_joint_vel_frac": clip.max_joint_vel_frac}
    return out


def save_clip(clip: Clip, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(clip_to_dict(clip), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


class ClipLibrary:
    """Loads `clips/**/*.clip.json` and resolves them by short name.

    A clip is addressable either by bare name (``groove``) or by its folder path
    (``shaker/groove``). Bare names must be unique across the library; the loader
    says so explicitly rather than silently picking one.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._by_path: dict[str, Clip] = {}
        self._by_name: dict[str, list[str]] = {}
        self.errors: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self._by_path.clear()
        self._by_name.clear()
        self.errors.clear()
        if not self.root.exists():
            return
        for f in sorted(self.root.rglob("*.clip.json")):
            key = f.relative_to(self.root).as_posix()[: -len(".clip.json")]
            try:
                clip = load_clip(f)
            except ClipError as e:
                self.errors[key] = str(e)
                continue
            self._by_path[key] = clip
            self._by_name.setdefault(Path(key).name, []).append(key)

    def __len__(self) -> int:
        return len(self._by_path)

    def keys(self) -> list[str]:
        return sorted(self._by_path)

    def all(self) -> dict[str, Clip]:
        return dict(self._by_path)

    def get(self, ref: str) -> Clip:
        ref = ref.strip().removesuffix(".clip.json")
        if ref in self._by_path:
            return self._by_path[ref]
        candidates = self._by_name.get(ref, [])
        if len(candidates) == 1:
            return self._by_path[candidates[0]]
        if len(candidates) > 1:
            raise ClipError(
                f"clip name {ref!r} is ambiguous: {candidates}. "
                "Use the full path, e.g. 'shaker/groove'."
            )
        known = ", ".join(self.keys()[:12]) or "(library is empty)"
        raise ClipError(f"no clip {ref!r} in {self.root}. Known clips: {known}")
