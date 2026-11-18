"""アップロード経路のうち、ハードウェア無しで検証できる部分.

ネットワーク部分は実機が要るので、ここではペイロード生成とチャンク分割の
不変条件だけを固める。protocol.py の上限を1つでも超えるとサーバ側でスロットが
**無言で破棄される**ので、事前に弾けることに意味がある。
"""

import gzip
import json
import base64

import numpy as np
import pytest

from orchestra.fleet.upload import (
    MAX_AUDIO_CHUNKS,
    MAX_CHUNK,
    MAX_MOVE_CHUNKS,
    _chunk,
)
from orchestra.motion.program import N_CHANNELS, Program


def _program(seconds=24.3, fps=100.0) -> Program:
    n = int(seconds * fps) + 1
    t = np.arange(n) / fps
    f = np.zeros((n, N_CHANNELS))
    f[:, 1] = 0.007 * np.sin(2 * np.pi * 1.7 * t)
    f[:, 3] = np.radians(4) * np.sin(2 * np.pi * 1.7 * t)
    f[:, 7], f[:, 8] = -0.1745, 0.1745
    return Program(robot_id="r01", song="s", part="p", fps=fps, frames=f)


def test_chunks_never_exceed_the_wire_limit():
    s = "x" * (MAX_CHUNK * 3 + 17)
    chunks = _chunk(s)
    assert all(len(c) <= MAX_CHUNK for c in chunks)
    assert "".join(chunks) == s


def test_full_length_song_fits_in_the_chunk_budget():
    """24秒の曲が gzip 後に何チャンクになるか。上限4096を大きく下回ること。"""
    prog = _program()
    raw = json.dumps(prog.to_recorded_move(), separators=(",", ":"))
    body = base64.b64encode(gzip.compress(raw.encode(), 6)).decode("ascii")
    n = len(_chunk(body))
    assert n <= MAX_MOVE_CHUNKS
    # 実測で 24秒 -> 一桁チャンク。桁が変わったら何かがおかしい。
    assert n < 64, f"{n} チャンクは想定より多い（圧縮が効いていない？）"


def test_gzip_is_worth_it():
    """protocol.py は「数秒を超える move には gzip 推奨」と言う。裏を取る。"""
    prog = _program()
    raw = json.dumps(prog.to_recorded_move(), separators=(",", ":"))
    gz = base64.b64encode(gzip.compress(raw.encode(), 6)).decode("ascii")
    assert len(gz) < len(raw) * 0.25, (
        f"base64込みでも 1/4 未満に落ちるはず（実測 {len(gz)/len(raw):.3f}）"
    )


def test_audio_chunk_budget():
    """2.8MB の WAV が音声チャンク上限に収まること。"""
    fake = b"\x00" * (2_800_000)
    body = base64.b64encode(fake).decode("ascii")
    n = len(_chunk(body))
    assert n <= MAX_AUDIO_CHUNKS
    assert n < 300, f"{n} チャンク"


def test_recorded_move_payload_is_json_serialisable():
    """アップロードするのは JSON。numpy 型が混ざっていると送信時に落ちる。"""
    prog = _program(seconds=2.0)
    move = prog.to_recorded_move()
    s = json.dumps(move)          # 例外が出ないこと
    back = json.loads(s)
    assert set(back) == {"description", "time", "set_target_data"}
    assert len(back["time"]) == len(back["set_target_data"])
    assert all(isinstance(x, float) for x in back["time"])


def test_show_target_defaults():
    from orchestra.fleet.upload import ShowTarget

    t = ShowTarget("r01", "192.168.3.105", _program(seconds=1.0))
    assert t.audio is None, "音声は任意。無くても再生できること"
