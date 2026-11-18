"""■ を押したときのフェードアウト。

音楽は絞り、モーションは小さくしながら頭を下げて終わる。ここで守るのは
「きれいに終わるか」ではなく、壊れると実機で困る性質のほう:

  * どのタイミングで押しても、曲の最終フレームの姿勢で終わる
    -> トルクを切ってよい姿勢で終わる
  * 始まる前に押したら音を鳴らさない
  * 緊急停止はフェードを待たない
  * フェードしない経路（デッドマンなど）は今までどおり素早く中立へ戻る
  * 曲間は頭がもう下がっているので待たせない
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from orchestra.motion.program import CH_ANT_L, CH_ANT_R, N_CHANNELS, Program
from orchestra.web.runner import RehearsalBackend, ShowRunner, State

TIMEOUT_S = 5.0


def settle(check, timeout=TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.005)
    return False


# ---------------------------------------------------------------------------
# 音
# ---------------------------------------------------------------------------


def test_faded_goes_from_full_to_silent_without_a_step():
    from audio_stream import faded

    pcm = np.ones((48000, 2), dtype=np.float32)
    out = faded(pcm, 0, 8000, 32000)

    assert out.dtype == np.float32
    assert np.all(out[:8000] == 1.0), "始まるまでは元の音量のまま"
    assert np.all(out[40000:] == 0.0), "絞りきったら無音"
    assert np.all(np.diff(out[:, 0]) <= 0.0), "途中で音量が戻らない"
    assert np.abs(np.diff(out[:, 0])).max() < 1e-3, "継ぎ目が聞こえない"


def test_faded_continues_across_chunks():
    """音は 0.1 秒ずつ押し込むので、ブロックの切れ目に段差が出てはいけない。"""
    from audio_stream import faded

    pcm = np.ones((48000, 2), dtype=np.float32)
    whole = faded(pcm, 0, 8000, 32000)
    parts = np.vstack([faded(pcm[i:i + 1600], i, 8000, 32000)
                       for i in range(0, 48000, 1600)])
    assert np.allclose(parts, whole)


# ---------------------------------------------------------------------------
# エージェント
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self, owner):
        self.owner = owner

    def send_command(self, cmd):
        self.owner.heads.append(np.array(cmd.head).reshape(4, 4))
        self.owner.antennas.append(list(cmd.antennas))


class _Reachy:
    """送られた姿勢を覚えるだけの偽物。"""

    def __init__(self):
        self.heads: list[np.ndarray] = []
        self.antennas: list[list[float]] = []
        self.disabled = 0
        self.client = _Client(self)

    def disable_motors(self):
        self.disabled += 1

    def enable_motors(self):
        pass

    def goto_target(self, **_kw):
        pass


class _Audio:
    """予約と停止だけ覚える偽物。"""

    stats: dict = {}

    def __init__(self):
        self.stopped = 0

    def schedule(self, _t0):
        return {}

    def fade_out(self, _until):
        return {}

    def stop(self):
        self.stopped += 1


def _armed(tmp_path, seconds=20.0, fps=100.0):
    import orchestra_agent as A

    n = int(seconds * fps)
    t = np.arange(n) / fps
    frames = np.zeros((n, N_CHANNELS))
    frames[:, 2] = 0.006 * np.sin(2 * np.pi * 1.5 * t)
    frames[:, 4] = 0.10 * np.sin(2 * np.pi * 0.8 * t)
    frames[:, CH_ANT_R] = -0.3
    frames[:, CH_ANT_L] = 0.3
    # 曲の終わりは本物と同じく、頭を下げて耳を畳んだ姿勢
    frames[-1, 2] = -0.044
    frames[-1, 4] = 0.367
    frames[-1, CH_ANT_R] = -2.618
    frames[-1, CH_ANT_L] = 2.618
    f = Program(robot_id="r07", song="s", part="p", instrument="shaker", fps=fps,
                frames=frames, anchors=np.array([0.5])).save(tmp_path / "a.program.json.gz")

    agent = A.Agent(base_dir=tmp_path)
    agent.program = A.Program.load(f)
    agent.reachy = _Reachy()
    agent._set_state(A.STATE_ARMED)
    return agent, agent.reachy, A


def _agent(tmp_path):
    agent, robot, A = _armed(tmp_path)
    agent.op_start({"t0_local": time.monotonic() + 0.02})
    assert settle(lambda: agent.state == A.STATE_PLAYING)
    assert settle(lambda: len(robot.heads) > 3)
    return agent, robot, A


def test_fade_out_ends_in_the_closing_pose_and_cuts_torque(tmp_path):
    agent, robot, A = _agent(tmp_path)
    before = len(robot.heads)

    agent.op_abort({"fade_s": 0.3})
    assert settle(lambda: agent.state == A.STATE_ABORTED)

    last = agent.program.heads[-1].reshape(4, 4)
    assert np.allclose(robot.heads[-1], last, atol=1e-6), "頭を下げた姿勢で終わる"
    assert robot.antennas[-1] == pytest.approx([-2.618, 2.618], abs=1e-3)
    assert robot.disabled == 1
    assert len(robot.heads) - before > 5, "飛ばさず、少しずつ下げる"


def test_fade_out_moves_smoothly_towards_the_pose(tmp_path):
    agent, robot, A = _agent(tmp_path)
    before = len(robot.heads)

    agent.op_abort({"fade_s": 0.4})
    assert settle(lambda: agent.state == A.STATE_ABORTED)

    ant = np.array([a[0] for a in robot.antennas[before:]])
    assert np.all(np.diff(ant) <= 1e-9), "耳は畳む方向にだけ動く"
    assert np.abs(np.diff(ant)).max() < 0.5, "一気に飛ばない"


def test_stopped_while_armed_the_head_still_goes_down(tmp_path):
    """再生準備と時計合わせの間は、再生スレッドがまだ無い。"""
    agent, robot, A = _armed(tmp_path)

    agent.op_abort({"fade_s": 0.3})
    assert settle(lambda: robot.disabled == 1)

    assert agent.state == A.STATE_ABORTED
    assert np.allclose(robot.heads[-1], agent.program.heads[-1].reshape(4, 4), atol=1e-6)
    assert len(robot.heads) > 5, "飛ばさず、少しずつ下げる"


def test_stopped_in_the_countdown_it_goes_down_without_sounding(tmp_path):
    agent, robot, A = _armed(tmp_path)
    agent.audio_stream = audio = _Audio()
    agent.op_start({"t0_local": time.monotonic() + 10.0})
    assert settle(lambda: agent._play_thread.is_alive())

    agent.op_abort({"fade_s": 0.3})
    assert settle(lambda: agent.state == A.STATE_ABORTED)

    assert audio.stopped >= 1, "予約した音が鳴り出してはいけない"
    assert robot.disabled == 1
    assert np.allclose(robot.heads[-1], agent.program.heads[-1].reshape(4, 4), atol=1e-6)


def test_emergency_stop_cuts_a_fade_from_the_armed_pose(tmp_path):
    agent, robot, A = _armed(tmp_path)

    agent.op_abort({"fade_s": 30.0})
    assert settle(lambda: len(robot.heads) > 20)
    agent.op_abort({})
    assert settle(lambda: robot.disabled == 1, timeout=1.0)


def test_a_leftover_cut_does_not_stop_the_next_fade(tmp_path):
    """disarm と緊急停止は `_cut` を立てたまま去る。次の準備中の ■ が
    それを踏むと、頭を下げずに開始姿勢のままトルクが切れる。"""
    agent, robot, A = _armed(tmp_path)
    agent._cut.set()
    agent.op_arm({})

    agent.op_abort({"fade_s": 0.3})
    assert settle(lambda: robot.disabled == 1)
    assert np.allclose(robot.heads[-1], agent.program.heads[-1].reshape(4, 4), atol=1e-6)


def test_abort_without_a_fade_cuts_a_running_fade_short(tmp_path):
    """緊急停止はここを通る。フェードの終わりを待ってはいけない。"""
    agent, robot, A = _agent(tmp_path)

    agent.op_abort({"fade_s": 30.0})
    assert settle(lambda: len(robot.heads) > 20)
    t0 = time.monotonic()
    agent.op_abort({})
    assert settle(lambda: agent.state == A.STATE_ABORTED, timeout=1.0)
    assert time.monotonic() - t0 < 0.5

    last = agent.program.heads[-1].reshape(4, 4)
    assert not np.allclose(robot.heads[-1], last, atol=1e-6), \
        "切られたフェードが最後の姿勢まで走りきってはいけない"


def test_disarm_cuts_a_running_fade_short(tmp_path):
    agent, robot, A = _agent(tmp_path)

    agent.op_abort({"fade_s": 30.0})
    assert settle(lambda: len(robot.heads) > 20)
    agent.op_disarm({})
    assert settle(lambda: agent.state == A.STATE_ABORTED, timeout=1.0)


def test_abort_without_a_fade_still_ramps_to_the_start_pose(tmp_path):
    """デッドマンと CLI の停止はこちら。素早く戻す挙動を変えない。"""
    agent, robot, A = _agent(tmp_path)

    agent.op_abort({})
    assert settle(lambda: agent.state == A.STATE_ABORTED)
    assert np.allclose(robot.heads[-1], agent.program.heads[0].reshape(4, 4), atol=1e-6)


def test_a_new_take_is_not_cut_by_the_last_ones_abort(tmp_path):
    agent, robot, A = _agent(tmp_path)

    agent.op_abort({})
    assert settle(lambda: agent.state == A.STATE_ABORTED)
    agent.reachy = _Reachy()
    agent.op_start({"t0_local": time.monotonic() + 0.02})
    assert settle(lambda: agent.state == A.STATE_PLAYING)

    assert not agent._cut.is_set()
    assert agent._fade_s == 0.0
    agent.op_abort({"fade_s": 0.2})
    assert settle(lambda: agent.state == A.STATE_ABORTED)


# ---------------------------------------------------------------------------
# コンソール
# ---------------------------------------------------------------------------


class _Fading(RehearsalBackend):
    fade_s = 0.4

    def __init__(self):
        super().__init__(30.0)
        self.aborts: list[float] = []

    def abort(self, fade_s: float = 0.0) -> dict[str, str]:
        self.aborts.append(fade_s)
        return {}


@pytest.fixture
def fading():
    backend = _Fading()
    r = ShowRunner(backend, 30.0, lead_s=0.05)
    r.start()
    yield r, backend
    r.close()


def test_stop_while_playing_waits_out_the_fade(fading):
    runner, backend = fading
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)

    t0 = time.monotonic()
    runner.stop()
    assert settle(lambda: runner.state == State.STOPPING)
    assert runner.snapshot()["detail"] == "fading out"
    assert settle(lambda: runner.state == State.READY)

    assert backend.aborts == [0.4]
    assert time.monotonic() - t0 >= 0.4, "フェードの途中で disarm してはいけない"


def test_stop_while_arming_fades_too(fading):
    runner, backend = fading
    runner.play()
    assert settle(lambda: runner.state == State.ARMING)

    runner.stop()
    assert settle(lambda: runner.state == State.STOPPING)
    assert runner.snapshot()["detail"] == "fading out"
    assert settle(lambda: runner.state == State.READY)
    assert backend.aborts == [0.4]


def test_stop_in_the_countdown_fades_too(fading):
    runner, backend = fading
    runner.play()
    assert settle(lambda: runner.state == State.COUNTDOWN)

    runner.stop()
    assert settle(lambda: runner.state == State.READY)
    assert backend.aborts == [0.4]


def test_stop_in_the_gap_does_not_fade(fading):
    runner, backend = fading
    runner.set_loop(True)
    backend.duration = 0.05
    runner.play()
    assert settle(lambda: runner.state == State.GAP)

    runner.stop()
    assert settle(lambda: runner.state == State.READY)
    assert backend.aborts == [0.0]


def test_emergency_stop_beats_a_fade(fading):
    runner, backend = fading
    backend.fade_s = 30.0
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)
    runner.stop()
    assert settle(lambda: runner.state == State.STOPPING)

    runner.estop()
    assert settle(lambda: runner.snapshot()["detail"] == "motors disabled", timeout=1.0)
    assert runner.state == State.ERROR, "緊急停止の表示を穏やかな状態で上書きしない"
