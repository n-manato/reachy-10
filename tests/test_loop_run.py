"""ループ再生の停止手段が、演奏を巻き込まずに効くこと。

`--loop` は 148 秒の演奏を繰り返す。停止のキー入力は**演奏の監視ループを
ブロックしてはいけない** ―― `input()` を使うと 148 秒間ロボットの状態を誰も
見ないことになる。ここで守るのは「押されたことを記録するだけ」という性質と、
端末が無い環境（CI、パイプ、cron）で黙って無効化されることの2点。
"""

import io
import time

import pytest

from orchestra.cli import LOOP_GAP_S, KeyWatch


def test_key_watch_survives_a_terminal_it_cannot_read():
    """端末が無くても例外を投げない。

    キーが拾えないことは演奏を止める理由にならない ―― ctrl-C が残っている。
    """
    kw = KeyWatch("s")
    kw._win_reader = lambda: None
    kw._posix_reader = lambda: None
    kw._stdin_reader = lambda: None
    with kw as k:
        assert k.available is False
        assert k.alive is False
        assert k.pressed is None


def test_falls_back_to_stdin_when_there_is_no_console(monkeypatch):
    """実際に踏んだバグ。

    `msvcrt` はコンソールが無くても import でき、`kbhit()` は例外を出さずに
    0 を返し続ける ―― キーは stdin へ行くので永遠に拾えない。import の成否で
    判定していたため、**黙って効かない**状態になっていた。コンソールが取れない
    なら stdin へ降りること。
    """
    monkeypatch.setattr("sys.stdin", io.StringIO("s\n"))
    kw = KeyWatch("s")
    kw._win_reader = lambda: None       # コンソールが取れない環境を模す
    kw._posix_reader = lambda: None
    with kw as k:
        assert k.available is True, "stdin があるのに見張りを諦めている"
        assert k.needs_enter is True
        deadline = time.monotonic() + 2.0
        while k.pressed is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert k.pressed == "s"


def test_stdin_at_eof_does_not_spin():
    """EOF（`< nul`、パイプ）で空文字を読み続けて回り続けないこと。"""
    kw = KeyWatch("s")
    kw._win_reader = lambda: None
    kw._posix_reader = lambda: None
    reader = kw._stdin_reader()
    assert reader is not None
    with pytest.raises(EOFError):
        import sys as _sys
        saved, _sys.stdin = _sys.stdin, io.StringIO("")
        try:
            reader()
        finally:
            _sys.stdin = saved


def test_press_is_reported_immediately(monkeypatch):
    """次の回の頭まで無反応だと、効いたのか分からない（1回 148 秒ある）。"""
    monkeypatch.setattr("sys.stdin", io.StringIO("s\n"))
    seen = []
    kw = KeyWatch("s")
    kw._win_reader = lambda: None
    kw._posix_reader = lambda: None
    kw.on_press = seen.append
    with kw:
        deadline = time.monotonic() + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
    assert seen == ["s"]


def test_key_watch_records_the_key_without_blocking():
    kw = KeyWatch("sq")
    keys = iter(["x", None, "S"])   # 大文字でも拾う。関係ないキーは無視

    kw._win_reader = lambda: (lambda: next(keys, None))
    with kw as k:
        assert k.available is True
        deadline = time.monotonic() + 2.0
        while k.pressed is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert k.pressed == "s"


def test_key_watch_ignores_keys_it_was_not_asked_for():
    kw = KeyWatch("s")
    kw._win_reader = lambda: (lambda: "q")
    with kw as k:
        time.sleep(0.2)
        assert k.pressed is None


def test_a_reader_that_breaks_mid_run_does_not_raise():
    """端末が途中で消えても、見張りが降りるだけで演奏は続く。

    `available`（読み手を用意できた）は倒さない。倒すのは `alive` のほう
    ―― バナーの案内が嘘だったのではなく、途中で端末が消えたという別の事実。
    """

    def broken():
        raise OSError("console went away")

    kw = KeyWatch("s")
    kw._win_reader = lambda: broken
    with kw as k:
        time.sleep(0.2)
        assert k.pressed is None
        assert k.available is True
        assert k.alive is False


def test_arrows_keep_arriving_instead_of_latching_once():
    """音量は押すたびに効かないといけない。

    以前は最初の1致でスレッドごと `return` していた（`s` は1回きりで足りた
    ため）。矢印をそこに乗せると2回目から無反応になる。
    """
    seen = []
    keys = iter(["up", "up", "down", "right", "left", "up"])
    kw = KeyWatch("s")
    kw._win_reader = lambda: (lambda: next(keys, None))
    kw.on_arrow = seen.append
    with kw:
        deadline = time.monotonic() + 2.0
        while len(seen) < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert seen == ["up", "up", "down", "right", "left", "up"]


def test_s_still_latches_while_arrows_stream():
    """矢印を流しても `s` の掛け金は壊れない。"""
    arrows = []
    keys = iter(["up", "s", "down"])
    kw = KeyWatch("s")
    kw._win_reader = lambda: (lambda: next(keys, None))
    kw.on_arrow = arrows.append
    with kw:
        deadline = time.monotonic() + 2.0
        while len(arrows) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert kw.pressed == "s"
    assert arrows == ["up", "down"], "s のあとも矢印が届くこと"


def test_arrow_tables_cover_all_four_on_both_platforms():
    from orchestra.cli import _VT_ARROW, _WIN_ARROW, ARROWS

    assert set(_WIN_ARROW.values()) == set(ARROWS)
    assert set(_VT_ARROW.values()) == set(ARROWS)
    # 左右が逆順。Windows は K/M、ANSI は D/C -- 取り違えやすいので固定する。
    assert _WIN_ARROW["K"] == _VT_ARROW["D"] == "left"
    assert _WIN_ARROW["M"] == _VT_ARROW["C"] == "right"


def _feed(seq):
    """1文字ずつ返す `getch`。エスケープ解釈のテスト用。"""
    it = iter(seq)
    return lambda: next(it, None)


def test_windows_two_byte_arrows_are_decoded_not_discarded():
    """実際に踏んでいたバグ。

    2バイト目を読んだうえで**捨てて**いたので、矢印がどこにも届かなかった。
    読み捨てをやめるだけでなく、名前に直るところまで見る。
    """
    from orchestra.cli import _win_decode

    assert _win_decode("\xe0", _feed("H")) == "up"
    assert _win_decode("\x00", _feed("P")) == "down"
    assert _win_decode("\xe0", _feed("K")) == "left"
    assert _win_decode("\xe0", _feed("M")) == "right"


def test_windows_plain_characters_pass_straight_through():
    from orchestra.cli import _win_decode

    assert _win_decode("s", _feed("")) == "s"
    assert _win_decode("q", _feed("")) == "q"


def test_windows_function_keys_are_swallowed_not_mistaken_for_letters():
    """F1 は `\\x00` + `;`。2バイト目を読まないと `;` が次の入力に見える。"""
    from orchestra.cli import _win_decode

    getch = _feed(";s")
    assert _win_decode("\x00", getch) is None
    assert getch() == "s", "2バイト目を食い残している"


def test_ansi_escape_sequences_are_assembled_from_three_reads():
    from orchestra.cli import _vt_decode

    assert _vt_decode("\x1b", _feed("[A")) == "up"
    assert _vt_decode("\x1b", _feed("[B")) == "down"
    assert _vt_decode("\x1b", _feed("[C")) == "right"
    assert _vt_decode("\x1b", _feed("[D")) == "left"
    assert _vt_decode("s", _feed("")) == "s"


def test_posix_arrows_survive_arriving_as_one_write():
    """実際に踏んだバグ。端末は矢印の `ESC [ A` を**一度の write で**寄越す。

    `_vt_decode` の単体テストは1文字ずつ手渡すので、ここは通らないと分からない。
    `sys.stdin.read(1)` は緩衝された TextIOWrapper なので、1文字返すために記述子
    から3バイトとも吸い上げ `ESC` だけを返す。残る `[ A` は Python の緩衝に隠れ、
    記述子を見る `select` は「何も来ていない」と答える ―― 矢印が丸ごと消えるのに
    `arrows=True` と案内してしまう。Windows では `msvcrt` 側を通るので露呈せず、
    Linux に移して初めて出た。
    """
    pty = pytest.importorskip("pty", reason="POSIX 端末のみ")
    import os
    import sys

    master, slave = pty.openpty()
    kw = KeyWatch("s")
    stdin = os.fdopen(slave, "r")      # 本番の `sys.stdin` と同じ緩衝の付き方
    saved = sys.stdin
    sys.stdin = stdin
    try:
        reader = kw._posix_reader()
        assert reader is not None, "pty を tty と認識できていない"
        for seq, want in ((b"\x1b[A", "up"), (b"\x1b[B", "down"),
                          (b"\x1b[C", "right"), (b"\x1b[D", "left")):
            os.write(master, seq)      # 端末と同じく1回で3バイト
            assert reader() == want
        os.write(master, b"s")
        assert reader() == "s", "普通のキーまで壊していないこと"
    finally:
        sys.stdin = saved
        if kw._restore is not None:
            kw._restore()
        stdin.close()
        os.close(master)


def test_a_bare_escape_is_dropped_rather_than_blocking():
    """Esc 単独は矢印と区別が付かない。続きが来なければ捨てる。"""
    from orchestra.cli import _vt_decode

    assert _vt_decode("\x1b", _feed([None])) is None
    assert _vt_decode("\x1b", _feed("[")) is None      # ESC [ で途切れた
    assert _vt_decode("\x1b", _feed("OP")) is None     # F1 (ESC O P)


def test_a_line_terminal_reports_that_arrows_are_unavailable(monkeypatch):
    """行入力では矢印は原理的に届かない。使えるふりをしない。

    Enter を押すまでバイトが来ず、端末の行編集がカーソル移動として食う。
    ここで `arrows` が True になると、演奏中に押しても無反応な機能を
    「使える」と案内することになる。
    """
    monkeypatch.setattr("sys.stdin", io.StringIO("s\n"))
    kw = KeyWatch("s")
    kw._win_reader = lambda: None
    kw._posix_reader = lambda: None
    with kw as k:
        assert k.available is True
        assert k.arrows is False


def test_the_gap_between_takes_covers_the_re_arm_goto():
    """arm の `goto`（2秒）より短いと、礼の直後に頭が持ち上がって見える。"""
    assert LOOP_GAP_S >= 2.0


def test_loop_flag_defaults_to_a_single_run():
    """`--loop` を付けなければ従来どおり1回で終わる（既定を変えない）。"""
    from orchestra.cli import build_parser

    p = build_parser()
    assert p.parse_args(["show", "run", "small_world"]).loop is None
    assert p.parse_args(["show", "run", "small_world", "--loop"]).loop == 0
    assert p.parse_args(["show", "run", "small_world", "--loop", "3"]).loop == 3
