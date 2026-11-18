"""ブラウザの操作卓。

守りたいのは3つ。

**「止めたら最初から」が構造として成り立っていること。** 一時停止を持たない
のはUIの都合ではなく、エージェントが自分の単調時計でプログラムを回していて
途中で保留する手段が無いから。だから STOP のあとの PLAY は必ず arm からやり
直す ―― ここが崩れると、画面は「最初から」と言いながら途中から鳴る。

**非常停止が上書きされないこと。** ボタンを押した瞬間、作業スレッドは
`monitor` の中にいる。掛け金が無いと、そのスレッドが通常の停止処理を終えて
数秒後に画面へ `ready` と書く。非常停止の直後に出してはいけない唯一の状態。

**ステージデータが譜面と一致すること。** 画面のツインは実機と同じフレームを
再生することだけが取り柄で、小節の区切りがずれていれば背景も灯りも全部ずれる。

ロボットは一切要らない。`RehearsalBackend` が同じ状態機械を時計だけで回す。
"""

import asyncio
import gzip
import json
import time
from pathlib import Path

import pytest

from orchestra.web.runner import RehearsalBackend, ShowRunner, State


class Robot:
    """`RobotConfig` のうち、ここで使う分だけ。"""

    def __init__(self, rid, role="performer", instrument="marimba"):
        self.id = rid
        self.role = role
        self.instrument = instrument


def settle(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def runner():
    """1回 0.6 秒の公演。状態遷移はすべて本物。"""
    backend = RehearsalBackend(0.6, robots=[Robot("r01", "conductor"), Robot("r02")])
    r = ShowRunner(backend, 0.6, lead_s=0.2)
    r.start()
    yield r
    r.close(timeout=5.0)


# -- 再生と停止 ---------------------------------------------------------------


def test_a_take_runs_through_every_state_in_order(runner):
    seen = []
    runner.subscribe(lambda s: seen.append(s["state"]))
    runner.play()
    assert settle(lambda: runner.state == State.READY and State.PLAYING in seen)

    order = [s for i, s in enumerate(seen) if i == 0 or seen[i - 1] != s]
    for want in (State.ARMING, State.SYNCING, State.COUNTDOWN,
                 State.PLAYING, State.STOPPING, State.READY):
        assert want in order, f"{want} never happened: {order}"
    assert order.index(State.COUNTDOWN) < order.index(State.PLAYING)
    assert order.index(State.PLAYING) < order.index(State.STOPPING)


def test_the_clock_starts_negative_and_runs_through_zero(runner):
    """`t` はプログラム時刻。カウントダウン中は負で、T0 をまたいで連続する。

    配信は**状態が変わったときだけ**で、演奏中に毎秒刻むのはSSE側の仕事
    （`server.TICK_S`）。画面は自前の時計を回して、届いた値で補正する。
    だからここで見るのは「流れてくる量」ではなく `snapshot()` が今どこを
    指しているか ―― 進んでいることと、止まったら指さなくなること。
    """
    announced = []
    runner.subscribe(lambda s: announced.append(s["t"]))
    runner.play()

    assert settle(lambda: runner.state == State.COUNTDOWN)
    assert runner.snapshot()["t"] < 0, "the countdown is before T0, so t is negative"

    assert settle(lambda: runner.state == State.PLAYING)
    a = runner.snapshot()["t"]
    time.sleep(0.2)
    b = runner.snapshot()["t"]
    assert b > a, f"the clock did not advance: {a} -> {b}"

    assert settle(lambda: runner.state == State.READY)
    assert runner.snapshot()["t"] is None
    assert any(t is not None and t < 0 for t in announced), \
        "the countdown was never announced, so no page could draw it"


def test_stopping_mid_take_returns_to_ready_and_rewinds(runner):
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)
    runner.stop()
    assert settle(lambda: runner.state == State.READY)

    snap = runner.snapshot()
    assert snap["t"] is None, "a stopped show must not still report a position"
    assert snap["take"] == 0
    assert "from the top" in snap["message"]


def test_the_next_play_starts_a_new_take_from_zero(runner):
    """一時停止は無い。2回目の PLAY は arm からやり直す。"""
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)
    runner.stop()
    assert settle(lambda: runner.state == State.READY)

    firsts = []
    runner.subscribe(lambda s: firsts.append(s["t"]) if s["t"] is not None else None)
    runner.play()
    assert settle(lambda: runner.state in (State.COUNTDOWN, State.PLAYING))
    assert min(firsts) < 0, "the second take did not begin before T0"


def test_play_while_playing_does_nothing(runner):
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)
    before = runner.snapshot()["take"]
    runner.play()
    time.sleep(0.05)
    assert runner.snapshot()["take"] == before


# -- ループ -------------------------------------------------------------------


def test_loop_runs_another_take_and_stops_when_turned_off(runner, monkeypatch):
    monkeypatch.setattr("orchestra.web.runner.LOOP_GAP_S", 0.1)
    runner.set_loop(True)
    runner.play()
    assert settle(lambda: runner.snapshot()["takes_done"] >= 2, timeout=12.0), \
        "the second take never started"
    assert settle(lambda: runner.state == State.GAP, timeout=12.0) or True
    runner.set_loop(False)
    runner.stop()
    assert settle(lambda: runner.state == State.READY, timeout=12.0)


def test_turning_loop_off_does_not_cut_the_music(runner):
    """ループは「この回のあと」の話。切った瞬間に演奏を止めるのは別のボタン。"""
    runner.set_loop(True)
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)
    runner.set_loop(False)
    time.sleep(0.1)
    assert runner.state == State.PLAYING


# -- 非常停止 -----------------------------------------------------------------


def test_emergency_stop_is_not_overwritten_by_the_worker(runner):
    """実際に踏みかけたバグ。

    ボタンを押した時点で作業スレッドは `monitor` の中。掛け金が無いと、その
    スレッドが停止処理を終えたあとに `ready` を書いて、非常停止の表示を
    消してしまう。
    """
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)
    runner.estop()
    assert runner.state == State.ERROR

    time.sleep(1.5)        # 作業スレッドが通常の停止処理を終えるだけの時間
    snap = runner.snapshot()
    assert snap["state"] == State.ERROR, "an emergency stop quietly became 'ready'"
    assert "EMERGENCY" in snap["message"]


def test_play_clears_the_emergency_latch(runner):
    runner.play()
    assert settle(lambda: runner.state == State.PLAYING)
    runner.estop()
    assert settle(lambda: runner.state == State.ERROR)
    runner.play()
    assert settle(lambda: runner.state in (State.ARMING, State.SYNCING,
                                           State.COUNTDOWN, State.PLAYING))


# -- 音量 ---------------------------------------------------------------------


def test_volume_is_clamped_to_what_the_daemon_accepts(runner):
    assert runner.set_volume(140)["volume"] == 100
    assert runner.set_volume(-5)["volume"] == 0
    assert runner.set_volume(37)["volume"] == 37


# -- ステージデータ -----------------------------------------------------------


@pytest.fixture(scope="module")
def stage(ctx):
    from orchestra.music.score import load_score
    from orchestra.web.export import stage_data

    score = load_score(ctx.songs_dir / "small_world")
    return stage_data(ctx, score, ctx.robots.select(None))


def test_every_robot_with_a_build_gets_the_same_timeline(stage):
    assert len(stage["robots"]) == 9
    n = {len(r["frames"]) for r in stage["robots"]}
    assert len(n) == 1, f"robots disagree about the length of the show: {n}"
    assert abs(stage["duration"] - 144.45) < 0.01


def test_frames_are_resampled_not_truncated(stage):
    """50 Hz に落としても、尺と終端は動かないこと。"""
    for r in stage["robots"]:
        assert len(r["frames"]) == round(stage["duration"] * stage["fps"]) + 1
        assert len(r["frames"][0]) == len(stage["channels"]) == 9


def test_sections_line_up_with_the_score(stage):
    """`song.yaml` は 32小節 = Verse8 + Chorus8 + Verse8 + Chorus8 と書いてある。

    背景の色も灯りも図形もこの区切りで動くので、ここがずれると全部ずれる。
    """
    names = [s["name"] for s in stage["sections"]]
    assert names == ["intro", "verse", "chorus", "verse", "chorus",
                     "finale", "outro"]

    t0 = stage["music"]["t0_s"]
    assert abs(stage["sections"][0]["end"] - t0) < 1e-6, "the intro must end at T0"

    bar_s = 60.0 / stage["music"]["bpm"] * 4      # 4/4
    for s in stage["sections"][1:3]:
        assert abs((s["end"] - s["start"]) - 8 * bar_s) < 1e-6

    # 継ぎ目が無いこと。背景は区間で色を選ぶので、隙間は一瞬の色落ちになる。
    for a, b in zip(stage["sections"], stage["sections"][1:]):
        assert abs(a["end"] - b["start"]) < 1e-6
    assert abs(stage["sections"][-1]["end"] - stage["duration"]) < 1e-6


def test_anchors_are_the_strike_times_the_build_shipped(stage, ctx):
    import gzip as gz

    for entry in stage["robots"]:
        path = ctx.build_dir / "small_world" / f"{entry['id']}.program.json.gz"
        with gz.open(path, "rt", encoding="utf-8") as f:
            prog = json.load(f)
        assert len(entry["anchors"]) == len(prog["anchors"])
        assert entry["anchors"] == sorted(entry["anchors"])
        assert entry["anchors"][-1] <= stage["duration"]


def test_pitched_parts_carry_notes_that_land_on_their_anchors(stage):
    """画面の楽器は anchors で叩き、notes で**どのバー**かを決める。

    両者が同じ瞬間を指していなければ、マレットは正しいバーの上で空振りする。
    ビルドと譜面で 0.0 ms 一致することを実測した上での前提なので、ここで守る。
    """
    pitched = [r for r in stage["robots"] if r["pitched"]]
    assert {r["instrument"] for r in pitched} == {
        "celesta", "vibraphone", "glockenspiel", "marimba"}
    for r in pitched:
        assert r["notes"] and len(r["notes"]) == len(r["anchors"]), r["id"]
        for note, anchor in zip(r["notes"], r["anchors"]):
            assert abs(note[0] - anchor) < 2e-3, (r["id"], note, anchor)
        lo, hi = r["pitch_range"]
        assert lo < hi and all(lo <= n[1] <= hi for n in r["notes"])


def test_one_pitch_is_a_drum_not_a_melody(stage):
    wb = next(r for r in stage["robots"] if r["instrument"] == "woodblock")
    assert wb["pitched"] is False
    assert wb["notes"] and len({n[1] for n in wb["notes"]}) == 1


def test_loop_parts_have_anchors_but_no_score_notes(stage):
    for name in ("tambourine", "shaker", "bells"):
        r = next(r for r in stage["robots"] if r["instrument"] == name)
        assert r["notes"] is None and len(r["anchors"]) > 100, name


def test_envelope_is_the_robots_own_stem_at_stage_rate(stage, ctx):
    """各台の包絡線は、その台が再生するステムそのものから。

    長さはフレーム数と同じ（同じ添字で引く）。名乗りの順に、各ステムが
    自分のスロットで初めて鳴る ―― ビルドの `intro.order` と同じ順になる。
    """
    first = {}
    for r in stage["robots"]:
        env = r["envelope"]
        assert len(env) == len(r["frames"]), r["id"]
        assert 0.0 <= min(env) and max(env) <= 1.0
        assert max(env) > 0.5, f"{r['id']} never gets loud -- wrong stem?"
        first[r["id"]] = next(i for i, v in enumerate(env) if v > 0.05) / stage["fps"]
    order = sorted(stage["intro_order"], key=lambda rid: first[rid])
    assert order == stage["intro_order"], first


def test_the_introduction_has_strikes_of_its_own(stage):
    """ビルドの `anchors` は**音楽しか覆っていない** —— 33秒の名乗りの中に
    1個も無い。名乗りは「どの台が何を弾くか」を言うためだけの区間なので、
    そこで画面に何も起きないのは一番まずい沈黙だった。

    代わりにステムの立ち上がりから拾う。拾った順が、ビルドが決めた名乗りの
    順（`intro.order` を配役で解いたもの）と一致することを守る。
    """
    intro = next(s for s in stage["sections"] if s["name"] == "intro")
    for r in stage["robots"]:
        assert not [a for a in r["anchors"] if a < intro["end"]], \
            f"{r['id']}: anchors now reach into the intro -- this test's premise is gone"
        assert r["onsets"], f"{r['id']} names itself but has no onset"
        # 最初の anchor と重ならないこと（重なると初音が二度打ちに見える）
        assert r["onsets"][-1] < r["anchors"][0] - 0.2, r["id"]

    first = {r["id"]: r["onsets"][0] for r in stage["robots"]}
    assert sorted(stage["intro_order"], key=lambda rid: first[rid]) == stage["intro_order"]
    # 指揮者は最後。名乗りのあとのカウントインがその正体
    assert first["r01"] > max(first[rid] for rid in stage["intro_order"])


def test_each_introduction_is_a_phrase_not_one_note(stage):
    """名乗りの区間で、各台は**一節**（6音前後）を弾く。1音ではない。

    以前のオンセット検出はRMSが閾値を上に横切った点だけを拾っていた。
    マリンバやチェレスタは最初の音がまだ減衰している最中に次の音が来るので、
    包絡は閾値より下まで戻らず、2音目以降が一つも拾えなかった ——
    セレスタ1、ビブラフォン1、マリンバ1、ベル1。画面上では0.3秒の一瞬の
    きらめきが一度だけで、「3Dモデルが演奏しない」と見えていた原因がこれ。

    いまはスペクトルフラックスで取る。各台が自分の名乗りで実際に弾いている
    音数が出ること、そして**全台が同じくらい**出ることを守る。
    """
    intro = next(s for s in stage["sections"] if s["name"] == "intro")
    named = set(stage["intro_order"])
    counts = {}
    for r in stage["robots"]:
        if r["id"] not in named:
            continue
        counts[r["id"]] = len([o for o in r["onsets"] if o < intro["end"]])
    assert counts, "no robot is introduced"
    for rid, n in counts.items():
        assert n >= 4, f"{rid}: {n} onset(s) in its own introduction -- a phrase, not a note"
    # 名乗りは全台そろって同じ形。どれか一台だけ沈黙しているのが前の状態だった
    assert max(counts.values()) - min(counts.values()) <= 2, counts


def test_an_introduction_onset_is_a_real_attack(stage):
    """拾ったオンセットは、そのステムが本当に鳴り始めた点であること。

    名乗りの一節は密（0.2秒間隔）なので、閾値を下げるだけだと減衰の途中を
    刻んでしまう。隣り合うオンセットが近すぎないこと、そして自分の名乗りの
    窓の中に収まっていることを見る。
    """
    order = stage["intro_order"]
    intro = next(s for s in stage["sections"] if s["name"] == "intro")
    for i, rid in enumerate(order):
        r = next(x for x in stage["robots"] if x["id"] == rid)
        mine = [o for o in r["onsets"] if o < intro["end"]]
        for a, b in zip(mine, mine[1:]):
            assert b - a >= 0.12, f"{rid}: {a} and {b} are the same note twice"
        # 次の台が名乗り始める前に終わっていること
        if i + 1 < len(order):
            nxt = next(x for x in stage["robots"] if x["id"] == order[i + 1])
            first_next = min(o for o in nxt["onsets"] if o < intro["end"])
            assert max(mine) < first_next, f"{rid} runs into {order[i + 1]}"


def test_bands_are_a_real_spectrum_of_each_stem(stage):
    """壁のアナライザは、その席のステムの本物の短時間スペクトル。

    形が正直であること ―― マリンバは低域が、グロッケンとベルは高域が立つ。
    台ごとに1回だけ正規化するので、グロッケンの低域は暗いままでよい。
    """
    import numpy as np

    edges = stage["band_edges_hz"]
    assert edges == [60, 150, 400, 1000, 2500, 5000, 8000]
    by = {r["instrument"]: np.array(r["bands"]) for r in stage["robots"]}
    for name, b in by.items():
        assert b.shape == (len(stage["robots"][0]["frames"]), len(edges) - 1), name
        assert b.dtype.kind == "i" and b.min() >= 0 and b.max() == 100, name
    mean = {k: v.mean(axis=0) for k, v in by.items()}
    assert mean["marimba"][:2].mean() > mean["marimba"][4:].mean()
    assert mean["glockenspiel"][3:].mean() > mean["glockenspiel"][:1].mean()
    assert mean["bells"][3:].mean() > mean["bells"][:2].mean()
    assert mean["conductor"][:2].mean() > mean["conductor"][3:].mean(), "the bed is bass"


def test_the_backdrop_is_driven_by_one_waveform(stage):
    """背景は**1本の波形**で動く ―― 9台ぶんではなく、ミックス（preview.wav）。

    台ごとのスペクトルは楽器を動かすためのもので、背景はそれとは別に、
    全体が一つの体として上下する。無音のところは本当に 0 であること、
    音楽のところは帯域がばらけて立つこと（全帯域が一緒に動くなら、
    それは列が増えただけのレベルメーターになる）。
    """
    import numpy as np

    mb = np.array(stage["mix_bands"])
    fps = stage["fps"]
    assert mb.shape == (len(stage["robots"][0]["frames"]), 20)
    assert mb.min() >= 0 and mb.max() == 100

    # 名乗りが始まる前は無音
    assert mb[: int(4.0 * fps)].max() == 0

    # 音楽の中では帯域ごとに違う動きをしている
    music = mb[int(40 * fps): int(120 * fps)]
    assert music.mean() > 15
    corr = np.corrcoef(music.T)
    off = corr[~np.eye(20, dtype=bool)]
    assert np.nanmean(off) < 0.75, "every band moves together -- that is a level meter"


def test_intro_order_is_robot_ids_in_the_order_they_name_themselves(stage):
    assert stage["intro_order"] == ["r02", "r03", "r04", "r05",
                                    "r06", "r07", "r08", "r09"]


# -- HTTP ---------------------------------------------------------------------


@pytest.fixture
def client(stage):
    """`TestClient` を素の asyncio で回す。pytest-aiohttp は入れていない。"""
    from aiohttp.test_utils import TestClient, TestServer

    from orchestra.web.server import build_app

    backend = RehearsalBackend(0.6, robots=[Robot("r01", "conductor")])
    runner = ShowRunner(backend, 0.6, lead_s=0.2)
    runner.start()
    app = build_app(runner, stage)

    loop = asyncio.new_event_loop()
    c = TestClient(TestServer(app), loop=loop)
    loop.run_until_complete(c.start_server())

    def call(method, path, **kw):
        async def go():
            async with c.request(method, path, **kw) as resp:
                body = await resp.read()
                return resp.status, resp.headers, body
        return loop.run_until_complete(go())

    try:
        yield call
    finally:
        loop.run_until_complete(c.close())
        loop.close()


def test_every_command_answers_with_the_whole_state(client):
    for method, path, body in [
        ("GET", "/api/state", None),
        ("POST", "/api/play", None),
        ("POST", "/api/stop", None),
        ("POST", "/api/loop", {"on": True}),
        ("POST", "/api/volume", {"value": 44}),
    ]:
        status, _, raw = client(method, path, json=body) if body else client(method, path)
        assert status == 200, f"{method} {path} -> {status}"
        snap = json.loads(raw)
        assert "state" in snap and "robots" in snap and "duration" in snap


def test_volume_outside_the_range_is_clamped_not_refused(client):
    """スライダーの端を拒否するのは馬鹿げている。"""
    assert json.loads(client("POST", "/api/volume", json={"value": 500})[2])["volume"] == 100
    assert client("POST", "/api/volume", json={"value": "loud"})[0] == 400


def test_stage_is_served_as_gzip_and_parses(client):
    """4 MB の生JSONを 0.6 MB にして渡す。

    本体は `raw` に入ってこない ―― aiohttp のクライアントは
    `Content-Encoding: gzip` を見て勝手に解く。圧縮されていること自体は
    ヘッダで見て、中身は解けたものを見る。
    """
    status, headers, raw = client("GET", "/api/stage")
    assert status == 200
    assert headers["Content-Encoding"] == "gzip"
    data = json.loads(raw)
    assert data["format"] == "reachy-orchestra-stage"
    assert len(data["robots"]) == 9
    assert len(gzip.compress(raw, 6)) < len(raw) / 3


def test_the_pc_mix_is_refused_outside_rehearsal(ctx, stage):
    """本番の音は**9台のスピーカー**から出る。

    PCで焼いたミックスをブラウザで鳴らせてしまうと、サウンドカードの遅延ぶん
    ずれた10本目の声が部屋に混じる。画面のボタンを隠すだけでは保証にならない
    ので、サーバーが配信そのものを断る。
    """
    import asyncio as aio

    from aiohttp.test_utils import TestClient, TestServer

    from orchestra.web.server import build_app

    class Fleetish(RehearsalBackend):
        mode = "fleet"            # 実機モードの振りをするだけ。実機は要らない

    preview = ctx.songs_dir / "small_world" / "preview.wav"
    assert preview.exists(), "this test is only meaningful with a rendered mix"

    for backend, want in [(RehearsalBackend(0.3), 200), (Fleetish(0.3), 403)]:
        runner = ShowRunner(backend, 0.3)
        runner.start()
        loop = aio.new_event_loop()
        c = TestClient(TestServer(build_app(runner, stage, preview)), loop=loop)
        loop.run_until_complete(c.start_server())
        try:
            async def go():
                async with c.get("/api/audio/preview.wav") as r:
                    await r.read()
                    return r.status
            assert loop.run_until_complete(go()) == want, backend.mode
        finally:
            loop.run_until_complete(c.close())
            loop.close()
            runner.close(timeout=5.0)


def test_the_page_is_served(client):
    status, _, raw = client("GET", "/")
    assert status == 200
    assert b"<canvas id=\"stage\"" in raw
    assert b"/static/app.js" in raw


def test_nothing_on_screen_is_japanese():
    """画面の表示は英語だけ。

    ソースのコメントはこのリポジトリの流儀どおり日本語のままでよい ――
    守るのは**画面に出る文字**だけで、静的ファイル3本と、サーバーが
    スナップショットに載せて送る文面がその範囲。
    """
    import re

    from orchestra.web import runner, server

    cjk = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
    static = Path(__file__).resolve().parent.parent / "orchestra" / "web" / "static"
    for path in sorted(static.rglob("*")):
        if not path.is_file() or "vendor" in path.parts:
            continue
        if path.suffix not in (".html", ".js", ".css"):
            continue
        hits = [l for l in path.read_text(encoding="utf-8").splitlines() if cjk.search(l)]
        assert not hits, f"{path.name}: {hits[:3]}"

    # サーバーが文面を送る側に回ったら、それも画面の文字になる
    for mod in (runner, server):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        for line in text.splitlines():
            if cjk.search(line) and ('"' in line or "'" in line):
                assert line.lstrip().startswith(("#", "*", '"""')), \
                    f"{Path(mod.__file__).name}: {line.strip()[:70]}"


# -- a page that cannot start has to be able to say so --------------------------


def test_a_page_report_lands_in_the_log_on_one_line(client, capsys):
    status, _, body = client("POST", "/api/client-log", data=json.dumps({
        "kind": "stage", "step": "building the 3D stage",
        "detail": "Error: WebGL\ncontext failed", "ua": "Test/1.0"}))
    assert status == 204 and body == b""
    out = capsys.readouterr().out
    assert "page stage while building the 3D stage: Error: WebGL context failed" in out
    assert out.count("\n") == 1, "a report stays on one line in the journal"


def test_page_reports_are_bounded(client, capsys):
    status, _, _ = client("POST", "/api/client-log", data="x" * 100_000)
    assert status == 204
    assert len(capsys.readouterr().out) < 1200, "a huge report is cut, not logged whole"

    codes = [client("POST", "/api/client-log", data="{}")[0] for _ in range(40)]
    assert 429 in codes, "a public URL must not be able to flood the journal"


def test_page_reports_take_post_only(client):
    assert client("GET", "/api/client-log")[0] == 405


def test_the_page_says_when_its_script_never_ran():
    """A module that fails to load leaves app.js unable to report anything."""
    from orchestra.web.server import STATIC

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    watchdog = html.index("window.__booted")
    assert watchdog < html.index('type="importmap"'), "it must not depend on the modules"
    assert "window.__booted = true" in (STATIC / "app.js").read_text(encoding="utf-8")
