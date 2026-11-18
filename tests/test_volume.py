"""演奏中に音量を変える経路。

守りたいのは3つ。**間引き**（キーリピートで9台に毎秒270発を撃たない）、
**重ねない**（詰まった呼び出しが `status()` を遅らせ、演奏中の台を「脱落」と
誤判定させる）、**クランプ**（デーモンは 0-100 しか受け付けない）。
"""

import time

from orchestra.cli import VOLUME_STEP
from orchestra.show.volume import MIN_INTERVAL_S, VolumeController, bar, median


class FakeFleet:
    """`Fleet.volume` の形だけ真似る。呼ばれた値と時刻を控える。"""

    def __init__(self, delay: float = 0.0, fail: str | None = None) -> None:
        self.links = [object()]
        self.calls: list[tuple[float, int]] = []
        self.spans: list[tuple[float, float]] = []   # 重なり検査用
        self.delay = delay
        self.fail = fail

    def volume(self, value: int):
        t0 = time.monotonic()
        self.calls.append((t0, int(value)))
        if self.delay:
            time.sleep(self.delay)
        self.spans.append((t0, time.monotonic()))
        if self.fail:
            return {"r01": (None, RuntimeError(self.fail))}
        return {"r01": ({"volume": value}, None)}


def _settle(pred, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        time.sleep(0.01)


# -- 値そのもの ---------------------------------------------------------------

def test_value_is_clamped_to_what_the_daemon_accepts():
    """`SetVolumeCmd` は pydantic で ge=0 le=100。外れた値は送る前に潰す。"""
    f = FakeFleet()
    with VolumeController(lambda: f, 95) as v:
        assert v.nudge(+20) == 100
        assert v.nudge(+20) == 100
        assert v.set(-5) == 0
        assert v.nudge(-20) == 0


def test_arrow_steps_are_coarse_up_down_and_fine_left_right():
    assert VOLUME_STEP["up"] == -VOLUME_STEP["down"] > 0
    assert VOLUME_STEP["right"] == -VOLUME_STEP["left"] > 0
    assert VOLUME_STEP["up"] > VOLUME_STEP["right"], "↑↓ のほうが粗いこと"


def test_a_value_read_off_the_robots_is_not_pushed_back_at_them():
    """起動しただけで9台の音量を書き換えない。

    実機から読んだ値は既にその状態なので、送り返す意味が無い。9台が食い違って
    いても起動時に黙って揃えない ―― 揃えるのは最初のキー操作の瞬間で、人が
    押したから変わった、と分かる形にする。
    """
    f = FakeFleet()
    with VolumeController(lambda: f, 60, apply_initial=False):
        time.sleep(MIN_INTERVAL_S * 3)
    assert f.calls == []


def test_a_value_restored_from_config_is_pushed_once():
    """保存した値は復元する。それが保存した理由なので。"""
    f = FakeFleet()
    with VolumeController(lambda: f, 62, apply_initial=True) as v:
        _settle(lambda: f.calls)
        time.sleep(MIN_INTERVAL_S * 2)
        assert v.touched is False, "復元しただけで config を書き直さない"
    assert [c[1] for c in f.calls] == [62]


# -- 間引き -------------------------------------------------------------------

def test_a_burst_of_keypresses_collapses_into_few_calls():
    f = FakeFleet()
    with VolumeController(lambda: f, 50) as v:
        for _ in range(30):            # キーリピート相当
            v.nudge(+1)
            time.sleep(0.002)
        _settle(lambda: f.calls and f.calls[-1][1] == v.value)

    assert len(f.calls) <= 4, f"30回の入力で {len(f.calls)} 回送っている"
    assert f.calls[-1][1] == 80, "最後は必ず最新の値であること"


def test_calls_are_spaced_by_at_least_the_throttle():
    f = FakeFleet()
    with VolumeController(lambda: f, 0) as v:
        for i in range(1, 6):
            v.set(i * 10)
            time.sleep(MIN_INTERVAL_S * 1.2)
        _settle(lambda: f.calls and f.calls[-1][1] == 50)

    gaps = [b[0] - a[0] for a, b in zip(f.calls, f.calls[1:])]
    assert all(g >= MIN_INTERVAL_S * 0.9 for g in gaps), gaps


def test_the_same_value_is_never_sent_twice():
    f = FakeFleet()
    with VolumeController(lambda: f, 50) as v:
        v.set(70)
        _settle(lambda: f.calls)
        for _ in range(5):
            v.set(70)
            time.sleep(MIN_INTERVAL_S * 1.5)
    assert [c[1] for c in f.calls] == [70]


def test_a_slow_robot_does_not_pile_up_calls():
    """1台が amixer で2秒かかっても、次を重ねて撃たない。

    `AgentLink` はリンクごとにロックを持つので、重ねると同じリンクの
    `status()` が待たされる。`Show.monitor` は連続失敗を脱落と見なすため、
    ここで詰まらせると**演奏中の台を落ちたことにしてしまう。**
    """
    f = FakeFleet(delay=0.3)
    with VolumeController(lambda: f, 50) as v:
        v.set(60)
        for _ in range(10):
            v.nudge(+1)
            time.sleep(0.01)
        _settle(lambda: f.calls and f.calls[-1][1] == v.value, timeout=3.0)

    # 1回に 0.3 秒かかるのに 10 回押した。回数ではなく**重なっていないこと**を見る
    # ―― 回数は速さ次第で変わるが、重ならないことは構造として保証されるべき。
    assert len(f.spans) >= 2, "遅くても2回は送っているはず"
    for (_, end), (start, _) in zip(f.spans, f.spans[1:]):
        assert start >= end - 1e-6, f"送信が重なっている: {f.spans}"


# -- 失敗の扱い ---------------------------------------------------------------

def test_a_robot_that_refuses_is_recorded_but_does_not_stop_anything():
    f = FakeFleet(fail="no volume control")
    with VolumeController(lambda: f, 50) as v:
        v.set(60)
        _settle(lambda: v.errors)
    assert "r01" in v.errors
    assert "no volume control" in v.errors["r01"]


def test_no_fleet_yet_is_not_an_error():
    """connect の前や、全機脱落した後に押されても落ちない。

    押した事実（`touched`）は残る ―― 届く相手が居なかっただけで、その人が
    その音量を選んだことは変わらない。
    """
    with VolumeController(lambda: None, 50) as v:
        v.set(70)
        time.sleep(MIN_INTERVAL_S * 3)
        assert v.value == 70
        assert v.errors == {}, "相手が居ないのはエラーではない"


def test_the_fleet_is_read_at_call_time_not_captured():
    """`show.fleet` は connect と arm で作り直される。

    掴んだままだと、外したはずの台に送り続けることになる。
    """
    first, second = FakeFleet(), FakeFleet()
    box = [first]
    with VolumeController(lambda: box[0], 50) as v:
        v.set(60)
        _settle(lambda: first.calls)
        box[0] = second
        v.set(70)
        _settle(lambda: second.calls)
    assert [c[1] for c in first.calls] == [60]
    assert [c[1] for c in second.calls] == [70]


# -- 小物 ---------------------------------------------------------------------

def test_touched_only_after_something_was_sent():
    """保存するかどうかの判定。触っていない設定は書き換えない。"""
    f = FakeFleet()
    with VolumeController(lambda: f, 50) as v:
        assert v.touched is False
        v.set(55)
        _settle(lambda: f.calls)
        assert v.touched is True


def test_median_ignores_one_outlier():
    """1台だけ 0 や 100 で残っていても、開始点を引きずられない。"""
    assert median([60, 62, 61, 0, 61]) == 61
    assert median([]) is None


def test_bar_spans_the_full_range():
    assert bar(0).strip("-") == ""
    assert set(bar(100)) == {"#"}
    assert len(bar(37)) == len(bar(0)) == 20
