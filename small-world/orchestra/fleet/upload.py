"""SSH不要の代替経路: デーモンへ動作を丸ごとアップロードして再生させる。

`UploadMoveStartCmd` → `UploadMoveChunkCmd` × N → `UploadMoveFinishCmd` →
`PlayUploadedMoveCmd` を `ws://<ip>:8000/ws/sdk` に直接送る。SDK の `ReachyMini`
はこれらをラップしていないため `client.send_command()` を使う。

これは**フォールバック経路**であって本命ではない。protocol.py:382-388 の設計
意図どおり、フレームごとの往復がなくなるのでストリーミングよりはるかに安定する
が、`PlayUploadedMoveCmd` には開始時刻フィールドが存在しない。各ロボットの t0 は
自分の asyncio タスクがコマンドを受け取った瞬間 (abstract.py:599) に決まるため、
ズレ = Wi-Fi のファンアウト幅（10〜40ms 程度）になり、事後に測れても事前には
補正できない。

それでも価値があるのは:
  * SSH 鍵の設置が要らない（オンロボットエージェントは要る）
  * 事前ロードなので演奏中の Wi-Fi 負荷がほぼゼロ
  * 音声も同じ upload_id で添付でき、モーションと同じクロックで走る

本命のエージェント経路（1〜5ms）が使えない間の実用解として、また N-2 が失敗した
場合の保険として用意する。
"""

from __future__ import annotations

import base64
import gzip
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..motion.program import Program

# protocol.py の実際の上限。超えるとサーバ側でスロットが黙って捨てられる。
MAX_CHUNK = 16 * 1024
MAX_MOVE_CHUNKS = 4096
MAX_AUDIO_CHUNKS = 16384
SLOT_TTL_S = 300.0


@dataclass
class UploadResult:
    robot_id: str
    upload_id: str = ""
    ok: bool = False
    n_chunks: int = 0
    payload_bytes: int = 0
    encoded_bytes: int = 0
    elapsed_s: float = 0.0
    error: str = ""
    events: list[dict] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return (self.encoded_bytes / self.payload_bytes) if self.payload_bytes else 1.0


def _chunk(s: str, size: int = MAX_CHUNK) -> list[str]:
    return [s[i : i + size] for i in range(0, len(s), size)]


class UploadSession:
    """1台のロボットに対する upload → play の一連。

    `ReachyMini` を経由せず生の WSClient を張る。`ReachyMini.__init__` はメディア
    初期化まで走らせるが、この経路では不要で、余計な `/api/media/release` を
    叩きたくないため。
    """

    def __init__(self, robot_id: str, host: str, port: int = 8000, timeout: float = 10.0):
        self.robot_id = robot_id
        self.host = host
        self.port = port
        self.timeout = timeout
        self._client = None

    def connect(self):
        from reachy_mini.io.ws_client import WSClient

        c = WSClient(self.host, self.port)
        c.wait_for_connection(timeout=self.timeout)
        self._client = c
        return c

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> "UploadSession":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- モータ制御 -------------------------------------------------------
    def enable_motors(self) -> None:
        from reachy_mini.io.protocol import SetTorqueCmd

        self._client.send_command(SetTorqueCmd(on=True))

    def disable_motors(self) -> None:
        from reachy_mini.io.protocol import SetTorqueCmd

        self._client.send_command(SetTorqueCmd(on=False))

    # -- アップロード -----------------------------------------------------
    def upload(
        self,
        program: Program,
        upload_id: str | None = None,
        gzip_payload: bool = True,
    ) -> UploadResult:
        """Program を RecordedMove 形式に変換してアップロードする。"""
        from reachy_mini.io.protocol import (
            UploadMoveChunkCmd,
            UploadMoveFinishCmd,
            UploadMoveStartCmd,
        )

        res = UploadResult(robot_id=self.robot_id, upload_id=upload_id or uuid.uuid4().hex)
        t0 = time.monotonic()

        move = program.to_recorded_move()
        raw = json.dumps(move, separators=(",", ":"))
        res.payload_bytes = len(raw)

        if gzip_payload:
            # docstring 曰く「数秒を超える move には推奨」。実測でおよそ 1/10。
            body = base64.b64encode(gzip.compress(raw.encode("utf-8"), 6)).decode("ascii")
            encoding = "gzip+base64"
        else:
            body = raw
            encoding = "json"
        res.encoded_bytes = len(body)

        chunks = _chunk(body)
        res.n_chunks = len(chunks)
        if not (1 <= len(chunks) <= MAX_MOVE_CHUNKS):
            res.error = (
                f"{len(chunks)} chunks exceeds the {MAX_MOVE_CHUNKS} limit "
                "(enable gzip, or split the move)"
            )
            return res

        self._client.send_command(
            UploadMoveStartCmd(
                upload_id=res.upload_id,
                total_chunks=len(chunks),
                description=f"{program.song}/{program.robot_id}/{program.part}",
                estimated_duration_s=program.duration,
                encoding=encoding,
            )
        )
        # 順序が命。1つでも順番が狂うとサーバはスロットを黙って破棄する
        # (protocol.py の UploadMoveChunkCmd docstring)。
        for i, ch in enumerate(chunks):
            self._client.send_command(
                UploadMoveChunkCmd(upload_id=res.upload_id, chunk_index=i, chunk=ch)
            )
        self._client.send_command(UploadMoveFinishCmd(upload_id=res.upload_id))

        res.elapsed_s = time.monotonic() - t0
        res.ok = True
        return res

    def upload_audio(self, wav: str | Path, upload_id: str) -> UploadResult:
        """同じ upload_id に WAV を紐付ける。モーションと同じクロックで鳴る。"""
        from reachy_mini.io.protocol import (
            UploadAudioChunkCmd,
            UploadAudioFinishCmd,
            UploadAudioStartCmd,
        )

        res = UploadResult(robot_id=self.robot_id, upload_id=upload_id)
        t0 = time.monotonic()
        data = Path(wav).read_bytes()
        res.payload_bytes = len(data)
        body = base64.b64encode(data).decode("ascii")
        res.encoded_bytes = len(body)

        chunks = _chunk(body)
        res.n_chunks = len(chunks)
        if not (1 <= len(chunks) <= MAX_AUDIO_CHUNKS):
            res.error = f"{len(chunks)} chunks exceeds the {MAX_AUDIO_CHUNKS} limit"
            return res

        self._client.send_command(
            UploadAudioStartCmd(
                upload_id=upload_id, total_chunks=len(chunks), encoding="wav-base64"
            )
        )
        for i, ch in enumerate(chunks):
            self._client.send_command(
                UploadAudioChunkCmd(upload_id=upload_id, chunk_index=i, chunk=ch)
            )
        self._client.send_command(UploadAudioFinishCmd(upload_id=upload_id))

        res.elapsed_s = time.monotonic() - t0
        res.ok = True
        return res

    # -- 再生 -------------------------------------------------------------
    def play(
        self,
        upload_id: str,
        play_frequency: float = 100.0,
        initial_goto_duration: float = 1.5,
        audio_lead_ms: float = 0.0,
    ) -> float:
        """再生を開始し、送信した瞬間の PC monotonic を返す。

        開始時刻を指定する手段は無い。t0 はこのメッセージがロボットに届いた
        瞬間に決まる。戻り値はズレを事後に見積もるための基準。
        """
        from reachy_mini.io.protocol import PlayUploadedMoveCmd

        sent = time.monotonic()
        self._client.send_command(
            PlayUploadedMoveCmd(
                upload_id=upload_id,
                play_frequency=play_frequency,
                initial_goto_duration=initial_goto_duration,
                audio_lead_ms=audio_lead_ms,
            )
        )
        return sent

    def cancel(self, upload_id: str) -> None:
        from reachy_mini.io.protocol import CancelMoveCmd

        self._client.send_command(CancelMoveCmd(upload_id=upload_id))


# ---------------------------------------------------------------------------
# 複数台
# ---------------------------------------------------------------------------


@dataclass
class ShowTarget:
    robot_id: str
    ip: str
    program: Program
    audio: Path | None = None


def upload_and_play(
    targets: list[ShowTarget],
    play_frequency: float = 100.0,
    initial_goto_duration: float = 2.5,
    settle_s: float = 1.0,
    disable_after: bool = True,
    wait: bool = True,
    progress=None,
) -> dict:
    """全台に事前ロードしてから一斉再生する。

    肝は「先に全台のアップロードを終わらせてから play を撃つ」こと。演奏開始の
    瞬間に転送は一切走らない。

    実測値（このフリートで8台、非周期波形を相互相関）:
        送信側ファンアウト幅  1.3 ms
        実位相スプレッド     18 ms  (計測ノイズ下限 ~20ms と同程度)

    打楽器のアタックが「同時」に聞こえる境界がおよそ 10-20 ms なので、これは
    ぎりぎり実用域。それ以上を求めるならオンロボットエージェント経路
    （1-5 ms、要 SSH 鍵）に移ること。

    注意: スロットは再生完了で破棄される。同じ upload_id は2度使えない。
    """
    import concurrent.futures as cf

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    sessions: dict[str, UploadSession] = {}
    uploads: dict[str, UploadResult] = {}
    report: dict = {"uploads": uploads, "play_sent": {}, "errors": {}}

    try:
        # --- 接続 + モーション（+音声）アップロード。並列。 ---
        def prepare(t: ShowTarget):
            s = UploadSession(t.robot_id, t.ip, timeout=15.0)
            s.connect()
            sessions[t.robot_id] = s
            s.enable_motors()
            up = s.upload(t.program)
            if up.ok and t.audio and Path(t.audio).exists():
                # 同じ upload_id に紐付けると、モーションと音声がデーモンの
                # 同一クロックで走る（protocol.py の設計意図どおり）。
                a = s.upload_audio(t.audio, up.upload_id)
                up.events.append(
                    {"audio": Path(t.audio).name, "ok": a.ok,
                     "chunks": a.n_chunks, "kb": round(a.encoded_bytes / 1024)}
                )
            return up

        with cf.ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            futs = {pool.submit(prepare, t): t.robot_id for t in targets}
            for f in cf.as_completed(futs):
                rid = futs[f]
                try:
                    uploads[rid] = f.result()
                    say(f"  uploaded {rid}")
                except Exception as e:
                    report["errors"][rid] = f"{type(e).__name__}: {e}"
                    say(f"  FAILED   {rid}: {e}")

        ready = sorted(rid for rid, u in uploads.items() if u.ok)
        if not ready:
            return report

        # デーモンがスロットを組み立て、トルクが乗るのを待つ。
        time.sleep(settle_s)

        # --- 一斉再生。ここだけは可能な限り同時に。 ---
        def fire(rid: str) -> float:
            return sessions[rid].play(
                uploads[rid].upload_id,
                play_frequency=play_frequency,
                initial_goto_duration=initial_goto_duration,
            )

        with cf.ThreadPoolExecutor(max_workers=len(ready)) as pool:
            futs = {pool.submit(fire, rid): rid for rid in ready}
            for f in cf.as_completed(futs):
                rid = futs[f]
                try:
                    report["play_sent"][rid] = f.result()
                except Exception as e:
                    report["errors"][rid] = f"{type(e).__name__}: {e}"

        sent = list(report["play_sent"].values())
        if len(sent) > 1:
            report["fanout_spread_ms"] = (max(sent) - min(sent)) * 1000.0

        longest = max((t.program.duration for t in targets), default=0.0)
        report["duration_s"] = longest + initial_goto_duration
        if wait:
            time.sleep(report["duration_s"] + 1.5)

        return report

    finally:
        for rid, s in sessions.items():
            try:
                if disable_after:
                    s.disable_motors()
            except Exception:
                pass
            s.close()


def abort_all(robots: list[tuple[str, str]]) -> dict[str, bool]:
    """全台のトルクを落とす。upload 経路用の緊急停止。

    `orchestra show estop` (HTTP 経由) と役割は同じで、こちらは既に張ってある
    WebSocket を使う。どちらでも止まるが、estop のほうが依存が少ないので
    本当に困ったときはそちらを使うこと。
    """
    out: dict[str, bool] = {}
    for rid, ip in robots:
        try:
            with UploadSession(rid, ip, timeout=4.0) as s:
                s.disable_motors()
            out[rid] = True
        except Exception:
            out[rid] = False
    return out
