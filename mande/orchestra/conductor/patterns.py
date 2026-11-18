"""The conductor: real conducting patterns, generated from the timeline.

Not a sine wave. A sine wave has no readable beat -- the eye cannot tell which
instant is "now", because a sinusoid's extremum is its slowest moment. What
musicians actually read is the **ictus**: the instant the gesture arrives, made
legible by *accelerating into it* and rebounding immediately out.

So each beat is built as a bounce:

    ictus(k) --[rebound: fast out, decelerating to an apex]--> apex
    apex     --[fall: accelerating]-------------------------> ictus(k+1)

The rebound uses ``1-(1-v)^2`` (leaves fast, settles at the top) and the fall
uses ``v^p`` with p>1 (starts lazy, arrives fast). That asymmetry is the entire
trick: peak speed lands exactly on the beat instead of between beats.

Everything else layers on top of that skeleton:

* **Beat patterns** are the real conducting figures -- 4/4 goes down, left,
  right, up; 3/4 down, out, up; 2/4 down, up. Lateral motion uses head y *and*
  body yaw together so the gesture reads from across a room.
* **The downbeat is bigger.** That is how a bar's position is communicated.
* **Phrase shaping** scales the whole gesture by the phrase envelope, so the
  conductor opens through a phrase and settles at the cadence.
* **A prep beat** lifts on the last beat before T0. Players need a breath, and
  it makes the start cue unambiguous.
* **A cutoff** closes the piece with a circular gesture into a held freeze.
* **Antennas carry dynamics** -- wide and raised for forte, low and narrow for
  piano.

The conductor is generated rather than authored so it works for any song at any
tempo and meter without anyone re-drawing keyframes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..motion.program import (
    CH_ANT_L,
    CH_ANT_R,
    CH_BODY_YAW,
    CH_PITCH,
    CH_ROLL,
    CH_X,
    CH_Y,
    CH_YAW,
    CH_Z,
    N_CHANNELS,
    Program,
)
from ..music.timeline import Timeline

# Normalised ictus positions per meter: (lateral, vertical) in -1..1.
# Lateral is +y = the conductor's own left. These are the standard figures.
BEAT_PATTERNS: dict[int, list[tuple[float, float]]] = {
    1: [(0.0, -1.0)],
    2: [(0.0, -1.0), (-0.15, 0.65)],
    3: [(0.0, -1.0), (-0.95, -0.25), (-0.35, 0.7)],
    4: [(0.0, -1.0), (0.95, -0.45), (-0.95, -0.45), (-0.3, 0.6)],
    6: [(0.0, -1.0), (0.35, -0.8), (0.85, -0.5),
        (-0.85, -0.5), (-0.5, -0.1), (-0.25, 0.65)],
}


@dataclass
class ConductorStyle:
    """Gesture sizing. All within Reachy Mini's real workspace."""

    # These amplitudes are sized so the peak joint velocity of the ictus stays
    # inside the sustained ceiling (4 rad/s) at typical tempos. That is not
    # merely a compliance exercise: unlike a strike, the conductor beats every
    # single beat for the whole piece, so its load really is sustained. And a
    # gesture the servo cannot track would *lag*, smearing the exact instant the
    # ictus exists to communicate -- so sizing it to the hardware makes it
    # crisper, not weaker. Check with `orchestra song build --report` after
    # changing any of them.
    z_amp_m: float = 0.0045
    """Vertical travel of the ictus. The primary beat signal.

    2026-08-28: 0.006 -> 0.0053。参考元は 112 BPM・24秒の曲で決めた値だが、
    Lamban は 120 BPM まで上がり 150秒続く。指揮者は**全拍を振り続ける**ので
    負荷は本当に持続的で、10秒窓の実効値が上限の 93% まで来ていた。
    目標は 85% 以下。"""

    y_amp_m: float = 0.0049
    """Lateral travel, for the beat-pattern figure。上の z と同じ理由で縮めた。"""

    body_yaw_amp_rad: float = float(np.radians(10.0))
    """Body yaw follows the lateral gesture, which makes it legible at distance.

    7° 相当だった。上限 150° に対して実測ピーク 6.8° しか使っておらず、
    遠目には胴が回っているように見えなかった。指揮の左右は**胴で示す**もの
    なので上げた。head yaw は 0 のままなので、この値がそのまま rel_yaw
    （上限 45°）に乗ることに注意 ―― 下の `section_turn_rad` と足して
    使い切らないようにしてある。"""

    section_turn_rad: float = float(np.radians(26.0))
    """フレーズごとに、体ごとアンサンブルの別の側へ向き直る量。

    拍の図形（上の `body_yaw_amp_rad`）は速くて小さい。**遅くて大きい回転**を
    別に持たせると、指揮者が「いま誰に向かって振っているか」が見える。
    実際の指揮者がやっていることでもある。

    これは head yaw も一緒に回す（体で向き直るので顔も付いていく）。
    そうしないと rel_yaw を食う ―― 頭の姿勢は world 座標なので、胴だけ
    回すと相対 yaw がそのぶん開く。"""

    section_turn_bars: float = 4.0
    """向き直る周期（小節）。フレーズ長に合わせるのが自然。"""

    section_turn_min_s: float = 8.0
    """ただし**これより速くは回らない**。

    小節で指定すると速いテンポでそのぶん速く回ってしまう。人が体ごと向き直る
    のに掛かる時間はテンポと関係ないし、150 BPM では 4小節が 6.4秒まで縮んで
    実効値の上限に当たった（`tests/test_conductor.py` が検出）。秒で下限を
    切っておく。"""

    pitch_amp_rad: float = float(np.radians(8.0))
    """A nod into each ictus. Reads as 'here' more strongly than translation
    alone, and is cheaper in joint velocity than the equivalent in z."""

    lean_x_m: float = 0.004
    """Forward lean through a phrase."""

    downbeat_scale: float = 1.45
    rebound_frac: float = 0.42
    """Share of the beat spent rebounding upward before the fall."""

    fall_power: float = 1.85
    """>1 accelerates into the ictus. This is what makes the beat readable."""

    rebound_power: float = 2.0

    antenna_base_rad: float = float(np.radians(14.0))
    antenna_dynamic_rad: float = float(np.radians(26.0))

    prep_beats: float = 1.0
    """Length of the breath-in lift before the first downbeat."""

    cutoff_beats: float = 2.0


def _pattern_for(beats_per_bar: float) -> list[tuple[float, float]]:
    n = int(round(beats_per_bar))
    if n in BEAT_PATTERNS:
        return BEAT_PATTERNS[n]
    # Unusual meter: fall back to a generic figure that still puts a clear
    # down on beat 1 and a clear up on the last beat.
    n = max(1, n)
    out = [(0.0, -1.0)]
    for i in range(1, n):
        side = 1.0 if i % 2 else -1.0
        rise = -0.5 + 1.1 * (i / max(n - 1, 1))
        out.append((side * 0.9 * (1.0 - i / n), rise))
    return out


def _bounce(u: np.ndarray, style: ConductorStyle) -> tuple[np.ndarray, np.ndarray]:
    """Split beat-phase u in [0,1) into (rebound weight, fall weight).

    Returns two arrays that are mutually exclusive: within the rebound the fall
    weight is 0 and vice versa, so the caller can blend three control points
    (previous ictus, apex, next ictus) without branching.
    """
    r = np.clip(style.rebound_frac, 0.05, 0.9)
    in_rebound = u < r

    w_rise = np.zeros_like(u)
    v = np.clip(u / r, 0.0, 1.0)
    # Leaves the ictus fast, decelerates to the apex.
    w_rise[in_rebound] = (1.0 - (1.0 - v) ** style.rebound_power)[in_rebound]

    w_fall = np.zeros_like(u)
    v2 = np.clip((u - r) / (1.0 - r), 0.0, 1.0)
    # Starts lazy at the apex, arrives fast on the beat.
    w_fall[~in_rebound] = (v2**style.fall_power)[~in_rebound]

    return w_rise, w_fall


def build_conductor_program(
    timeline: Timeline,
    fps: float = 100.0,
    lead_in_s: float = 2.0,
    neutral_antennas: tuple[float, float] = (-0.1745, 0.1745),
    style: ConductorStyle | None = None,
    robot_id: str = "",
    song: str = "",
    tail_s: float = 2.0,
) -> Program:
    """Generate the conductor's full performance.

    `lead_in_s` must be long enough to contain the prep beat; the first downbeat
    lands exactly at ``lead_in_s``, which is the same T0 every performer uses.
    """
    style = style or ConductorStyle()
    tempo = timeline.tempo
    bpb = timeline.meter.beats_per_bar
    pattern = _pattern_for(bpb)
    n_beats_pattern = len(pattern)

    total_s = lead_in_s + timeline.duration_s + tail_s
    n = int(round(total_s * fps)) + 1
    t = np.arange(n, dtype=np.float64) / fps

    frames = np.zeros((n, N_CHANNELS), dtype=np.float64)
    frames[:, CH_ANT_R] = neutral_antennas[0]
    frames[:, CH_ANT_L] = neutral_antennas[1]

    music_t = t - lead_in_s
    # Beats run continuously across the WHOLE program, including negative values
    # through the lead-in and past the end. This is what makes the gesture
    # continuous by construction: the prep beat is simply beat -1 of the same
    # pattern (the "up" position falling into the downbeat), which is precisely
    # what a prep beat is musically. An earlier version special-cased the prep
    # with its own formula and produced a step discontinuity at T0 -- the
    # validator caught it as a 21 rad/s joint velocity spike at exactly
    # t = lead_in_s.
    beats = tempo.seconds_to_beats(music_t)

    spb0 = tempo.seconds_per_beat_at(0.0)
    prep = (music_t < 0.0) & (music_t >= -style.prep_beats * spb0)
    tail = music_t > timeline.duration_s

    env = timeline.phrase_envelope(t, lead_in_s=lead_in_s)
    # Never let the gesture die completely mid-piece; a conductor at zero
    # amplitude has stopped conducting.
    env = 0.45 + 0.55 * env

    # One amplitude envelope over the whole program: silent before the prep,
    # ramping in through it, full during the piece, decaying through the tail.
    amp = np.zeros_like(t)
    amp[prep] = np.clip((music_t[prep] + style.prep_beats * spb0) / (style.prep_beats * spb0), 0, 1) ** 1.5
    playing = (music_t >= 0.0) & (music_t <= timeline.duration_s)
    amp[playing] = 1.0
    if tail.any():
        cutoff_s = max(style.cutoff_beats * tempo.seconds_per_beat_at(timeline.total_beats), 1e-6)
        amp[tail] = np.clip(1.0 - (music_t[tail] - timeline.duration_s) / cutoff_s, 0.0, 1.0) ** 1.2

    # ---------------- the beat pattern ----------------
    k = np.floor(beats).astype(int)
    u = beats - k
    idx_cur = np.mod(k, n_beats_pattern)
    idx_nxt = np.mod(k + 1, n_beats_pattern)

    pat = np.asarray(pattern, dtype=np.float64)

    # Beat 1 of every bar is larger: that is how bar position is communicated.
    #
    # The emphasis is baked into the CONTROL POINTS, not multiplied onto the
    # blended result. That distinction is not cosmetic. `pos` is continuous
    # across a beat boundary because the falling curve arrives exactly at
    # `p_nxt`, which is the next beat's `p_cur` -- but a per-beat multiplier
    # applied afterwards jumps 1.0 -> 1.45 between two adjacent frames, and the
    # composite gesture tears open at every downbeat.
    #
    # This hid for a long time. At 112 BPM one beat is 53.57 frames at 100 fps,
    # so beat boundaries land between samples at a different sub-frame offset
    # each bar and the step got smeared under the velocity ceiling. At 80 BPM a
    # beat is exactly 75 frames, every boundary lands ON a frame, and the same
    # step showed up undiminished: 6.02 rad/s against a 4.00 ceiling, at every
    # downbeat. A limit that only holds when the tempo does not divide evenly
    # into the frame rate is not a limit.
    emph = np.where(np.arange(n_beats_pattern) == 0, style.downbeat_scale, 1.0)
    pat_e = pat * emph[:, None]

    p_cur = pat_e[idx_cur]
    p_nxt = pat_e[idx_nxt]

    # Apex sits above and between the two ictus points -- where a hand actually
    # goes on the rebound.
    apex = np.column_stack(
        [(p_cur[:, 0] + p_nxt[:, 0]) * 0.5, np.maximum(p_cur[:, 1], p_nxt[:, 1]) + 0.55]
    )

    w_rise, w_fall = _bounce(u, style)
    in_rebound = (u < np.clip(style.rebound_frac, 0.05, 0.9))[:, None]
    rising = p_cur + (apex - p_cur) * w_rise[:, None]
    falling = apex + (p_nxt - apex) * w_fall[:, None]
    pos = np.where(in_rebound, rising, falling)

    scale = env * amp

    lat, vert = pos[:, 0], pos[:, 1]

    frames[:, CH_Y] = lat * style.y_amp_m * scale
    frames[:, CH_Z] = vert * style.z_amp_m * scale
    frames[:, CH_BODY_YAW] = lat * style.body_yaw_amp_rad * scale
    # Nod into the ictus: pitch dips as the gesture descends.
    frames[:, CH_PITCH] = -vert * style.pitch_amp_rad * scale
    frames[:, CH_X] = style.lean_x_m * (env - 0.45) * amp

    # ---------------- cutoff flourish ----------------
    # A small circle laid ON TOP of the (already decaying) beat gesture, so it
    # closes rather than cuts. Circular because a cutoff must be distinguishable
    # from just another beat. Starts at zero amplitude, so it adds no
    # discontinuity where it begins.
    if tail.any():
        span = max(
            style.cutoff_beats * tempo.seconds_per_beat_at(timeline.total_beats), 1e-6
        )
        v = np.clip((music_t[tail] - timeline.duration_s) / span, 0.0, 1.0)
        ring = np.sin(np.pi * v) ** 1.5   # 0 at both ends: no seam either side
        ang = 2.0 * np.pi * v
        frames[tail, CH_Y] += style.y_amp_m * 0.7 * np.sin(ang) * ring
        frames[tail, CH_Z] += style.z_amp_m * 0.5 * (np.cos(ang) - 1.0) * 0.5 * ring
        frames[tail, CH_BODY_YAW] += style.body_yaw_amp_rad * 0.5 * np.sin(ang) * ring

    # ---------------- dynamics on the antennas ----------------
    # Antennas carry dynamics continuously across the whole program: wide and
    # raised for forte, low and narrow for piano. During the tail they settle
    # open and hold -- the visual full stop.
    dyn = _dynamics_curve(timeline, t, lead_in_s)
    if tail.any():
        v = np.clip((music_t[tail] - timeline.duration_s) / max(
            style.cutoff_beats * tempo.seconds_per_beat_at(timeline.total_beats), 1e-6), 0, 1)
        dyn[tail] = dyn[tail] * (1 - v) + 0.55 * v
    spread = style.antenna_base_rad + style.antenna_dynamic_rad * dyn
    frames[:, CH_ANT_R] = -spread
    frames[:, CH_ANT_L] = spread

    # ---------------- 体ごとの向き直り ----------------
    # 拍の図形は速くて小さい。そこへ**遅くて大きい回転**を重ねると、
    # 「いま誰に向かって振っているか」が遠目にも見える。フレーズ周期で
    # ゆっくり左右を向く。遅い（4小節=12秒）ので速度はほとんど食わない。
    #
    # ★head yaw も一緒に回す★ 頭の姿勢は world 座標なので、胴だけ回すと
    # rel_yaw（上限45°）がそのぶん開く。体で向き直るなら顔も付いていくのが
    # 自然でもある。
    frames[:, CH_ROLL] = 0.0
    frames[:, CH_YAW] = 0.0
    if style.section_turn_rad:
        period_beats = max(style.section_turn_bars * timeline.meter.beats_per_bar, 1e-6)
        # 速いテンポでは秒の下限のほうが効く（体ごと向き直る時間はテンポと
        # 無関係）。150 BPM で 4小節=6.4秒まで縮み、実効値の上限に当たった。
        floor_beats = style.section_turn_min_s / max(
            tempo.seconds_per_beat_at(0.0), 1e-9)
        period = max(period_beats, floor_beats)
        turn = style.section_turn_rad * np.sin(2.0 * np.pi * beats / period) * amp
        frames[:, CH_BODY_YAW] += turn
        frames[:, CH_YAW] += turn

    # Anchors are the ictus instants -- the moments the conductor "means".
    # Performers' strikes are validated against these, and the skew tool uses
    # them as the conductor's reference grid.
    ictus_beats = np.arange(0.0, timeline.total_beats + 1e-9, 1.0)
    anchors = tempo.beats_to_seconds(ictus_beats) + lead_in_s

    return Program(
        robot_id=robot_id,
        song=song,
        part="conductor",
        instrument="conductor",
        fps=fps,
        frames=frames,
        anchors=anchors,
        meta={
            "lead_in_s": lead_in_s,
            "meter": str(timeline.meter),
            "beats": timeline.total_beats,
            "generated": "conductor.patterns.build_conductor_program",
        },
    )


def _dynamics_curve(timeline: Timeline, t: np.ndarray, lead_in_s: float) -> np.ndarray:
    """0..1 loudness over time, from note velocities.

    Falls back to the phrase envelope when the arrangement has no meaningful
    velocity variation -- which is common in hand-entered MIDI, where everything
    lands on 100.
    """
    evs = timeline.events
    if not evs:
        return timeline.phrase_envelope(t, lead_in_s=lead_in_s)

    vels = np.array([e.dynamic for e in evs])
    if float(vels.max() - vels.min()) < 0.05:
        return timeline.phrase_envelope(t, lead_in_s=lead_in_s)

    ev_t = timeline.tempo.beats_to_seconds(np.array([e.beat for e in evs])) + lead_in_s
    order = np.argsort(ev_t)
    ev_t, vels = ev_t[order], vels[order]

    curve = np.interp(t, ev_t, vels, left=vels[0], right=vels[-1])
    # Smooth over roughly a beat so the antennas breathe rather than twitch.
    win = max(3, int(len(t) / max(len(evs), 1)))
    kernel = np.ones(win) / win
    return np.clip(np.convolve(curve, kernel, mode="same"), 0.0, 1.0)
