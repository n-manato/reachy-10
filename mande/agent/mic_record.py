"""ロボットのマイクで録音し、**ロボット自身の時計に紐づけて**保存する。

なぜこれがあるか
----------------
このシステムのログは全部スピーカーの手前までしか見ていない。start_err も
フレーム数も push_errors も、「指令が揃っていた」ことしか言わない。実際に
9台の音が空気中で揃っているかは、マイクを通さないと分からない。

外付けマイクを1本立てる代わりに、**指揮者ロボット自身のマイク**を使う。
実測で確かめたこと（2026-08-07、r02が鳴らし r01が聞いた）:

  * wireless CM4 の XMOS 経路は **他機の音を消さない**。r02 が 1kHz を
    鳴らした瞬間、r01 のマイクで 1kHz 成分が 0.012 → 0.38、RMS が 8倍。
    AEC は自機の再生を参照信号にするので、他機には効かない。
  * 再生と録音は同じパイプラインの別の枝なので**同時に動く**。指揮者は
    自分のカウントインを鳴らしながら録れる。

そして 9台が**それぞれ違うステム**を鳴らしているので、1本の録音から
台ごとに相関で分離できる。同じ音を9台で鳴らしていたら不可能だった。

時刻について
------------
`get_audio_sample()` にタイムスタンプは付かない。なので**最初のチャンクを
受け取った monotonic 時刻**を記録し、そこからサンプル数で数える。この
起点には取り込み側の遅れ（マイク→ALSA→appsink）が定数として乗るが、
**1本の録音の中で台を比較するぶんには完全に相殺する**。絶対時刻が要る
用途には使えない、というのはここに書いておく。
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("orchestra.agent.mic")


class MicRecorder:
    """1本ぶんの録音。開始・停止・WAV書き出しだけ。"""

    def __init__(self, reachy, out_path: Path, max_seconds: float = 300.0):
        self.reachy = reachy
        self.out_path = Path(out_path)
        self.max_seconds = float(max_seconds)
        self.media = getattr(reachy, "media", None)
        self.rate = 16000
        self.channels = 2
        self._chunks: list[np.ndarray] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.first_chunk_monotonic: float | None = None
        self.started_at_monotonic: float | None = None
        self.stats: dict = {}

    @property
    def available(self) -> bool:
        return self.media is not None and hasattr(self.media, "get_audio_sample")

    def start(self) -> dict:
        if not self.available:
            raise RuntimeError("cannot reach the microphone (connected as no_media?)")
        if self._thread is not None:
            raise RuntimeError("already recording")

        self.rate = int(self.media.get_input_audio_samplerate() or 16000)
        self.channels = int(self.media.get_input_channels() or 2)
        self._chunks.clear()
        self._stop.clear()
        self.first_chunk_monotonic = None
        self.media.start_recording()
        self.started_at_monotonic = time.monotonic()
        self._thread = threading.Thread(target=self._pump, name="mic", daemon=True)
        self._thread.start()
        return {"recording": True, "rate": self.rate, "channels": self.channels}

    def _pump(self) -> None:
        limit = int(self.max_seconds * self.rate)
        got = 0
        while not self._stop.is_set() and got < limit:
            try:
                s = self.media.get_audio_sample()
            except Exception as e:  # noqa: BLE001
                log.warning("get_audio_sample failed: %s", e)
                break
            if s is None or not len(s):
                # appsink は drop=True なので、待ちすぎると取りこぼす。
                # 実測のチャンク間隔は 64ms。5ms 刻みで十分速い。
                time.sleep(0.005)
                continue
            if self.first_chunk_monotonic is None:
                self.first_chunk_monotonic = time.monotonic()
            self._chunks.append(np.asarray(s, dtype=np.float32))
            got += len(s)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        try:
            self.media.stop_recording()
        except Exception as e:  # noqa: BLE001
            log.warning("stop_recording failed: %s", e)

        if not self._chunks:
            self.stats = {"ok": False, "error": "not a single chunk was captured"}
            return self.stats

        x = np.concatenate(self._chunks, axis=0)
        self._chunks.clear()
        mono = x.mean(axis=1)
        peak = float(np.abs(mono).max())

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        # 16bit で書く。マイク入力は小さい（実測の暗騒音 -55 dBFS）ので、
        # そのまま量子化すると下位ビットしか使わない。相関を取るだけなので
        # 正規化してよいが、**倍率を記録しておく**こと。後から dBFS を
        # 読み違えないため。
        norm = 0.95 / peak if peak > 1e-6 else 1.0
        pcm = np.clip(x * norm, -1.0, 1.0)
        with wave.open(str(self.out_path), "wb") as w:
            w.setnchannels(x.shape[1])
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes((pcm * 32767).astype(np.int16).tobytes())

        self.stats = {
            "ok": True,
            "path": str(self.out_path),
            "rate": self.rate,
            "channels": int(x.shape[1]),
            "frames": int(x.shape[0]),
            "seconds": round(x.shape[0] / self.rate, 3),
            "peak": round(peak, 6),
            "rms": round(float(np.sqrt(np.mean(mono**2))), 6),
            "dbfs": round(20 * np.log10(max(float(np.sqrt(np.mean(mono**2))), 1e-9)), 1),
            "normalised_by": round(float(norm), 3),
            # ★これが無いと録音は時刻を持たない紙屑になる★
            "first_chunk_monotonic": self.first_chunk_monotonic,
            "start_monotonic": self.started_at_monotonic,
        }
        if peak > 0.98:
            self.stats["warning"] = (
                "the microphone is clipping -- too close, or the volume is "
                "too high. That blunts the correlation peak; turn it down "
                "and record again"
            )
        return self.stats
