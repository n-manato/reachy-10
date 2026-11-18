"""PCM を明示PTSでロボットの音声パイプラインへ流し込む。

`orchestra_agent.py` から使う。エージェントと同じく単体で完結させ、
ロボット側の Python（reachy_mini + numpy）だけで動くようにしてある。

なぜ `play_sound()` を使わないか
--------------------------------
SDK の `play_sound()` は呼ぶたびに playbin と audiosink を新規生成し、
**ALSA デバイスを開き直して**から非同期で PLAYING にする（戻り値も待たない、
`media/audio_gstreamer.py:519-554`）。可変コストが全部クリティカルパスに乗るので、
台間で ±20〜50ms ばらつく。物理楽器が無く音声が演奏そのものになった構成では、
これは打楽器のアタックがフラムとして聞こえる水準で使えない。

代わりに appsrc へ**明示PTS付き**でバッファを押し込む。実機実測（3台）:

    スケジュール残差   +2.46 / -0.33 / -0.69 ms
    申告レイテンシ     286.0 ms（3台とも同一）
    クロックドリフト   -16〜-27 ppm ＝ -1〜-1.6 ms/分

24秒の曲ならドリフトは 0.65ms。共有ネットワーククロック（GstNet）を使わなくても
要件を満たすので、既定では使わない。`use_net_clock` で有効にできる。

押さえるべき3点
---------------
1. **申告レイテンシを引く。** PTS=P のバッファは「P に相当する時刻 + レイテンシ」に
   鳴る。286ms は誤差ではなく仕様。
2. **`stop_playing()` / `clear_player()` を演奏中に呼ばない。** どちらも
   NULL/PAUSED を経由してデバイスを開き直すか、アンカーを打ち直す。
3. **`set_max_output_buffers()` を使わない。** `leaky-type=2`（古いバッファを捨てる）
   なので音楽が欠ける。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger("orchestra.agent.audio")


def read_wav_f32(path) -> tuple[np.ndarray, int]:
    """WAV を (N, ch) float32 と サンプルレート で読む。

    PC 側 `orchestra/audio/render.py` の同名関数と対になっている。
    エージェントを1ファイル+1モジュールに保つため、ここにも置いてある。
    """
    import wave

    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sw)
    if dtype is None:
        raise ValueError(f"{path}: unsupported bit depth {sw * 8}")
    x = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    x = (x - 128.0) / 128.0 if sw == 1 else x / np.float32(np.iinfo(dtype).max)
    return np.ascontiguousarray(x.reshape(-1, ch), dtype=np.float32), sr

CHUNK_S = 0.10          # 1回に押し込む長さ
LOOKAHEAD_S = 1.5       # 再生位置よりどれだけ先まで積むか


class AudioStream:
    """1台ぶんの音声を、指定した monotonic 時刻に鳴らす。"""

    def __init__(self, reachy, pcm: np.ndarray, rate: int,
                 lead_ms: float = 0.0, gain: float = 1.0,
                 use_net_clock: bool = False):
        """
        Args:
            pcm: (N, ch) float32。ステムのレート＝パイプラインのレートであること。
            lead_ms: 機体ごとの実測補正。正なら音を早める（スピーカーまでの遅れを詰める）。
        """
        self.reachy = reachy
        self.rate = int(rate)
        self.lead_ms = float(lead_ms)
        self.gain = float(gain)
        self.use_net_clock = use_net_clock

        self.audio = getattr(reachy.media, "audio", None)
        self.pipe = getattr(self.audio, "_pipeline", None) if self.audio else None
        self.appsrc = getattr(self.audio, "_appsrc", None) if self.audio else None

        self.pcm = self._conform(pcm)
        self.latency_ns = 0
        self.ratio = 1.0            # パイプラインクロック / monotonic
        self.ratio_resid_ppm = 0.0  # 上の推定の残差。大きいほど信用できない
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started_at_monotonic: float | None = None
        self.stats: dict = {}

    # -- 準備 ------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.pipe is not None and self.appsrc is not None

    def _conform(self, pcm: np.ndarray) -> np.ndarray:
        """パイプラインが要求する形（float32, インターリーブ, ch数一致）に整える。"""
        ch = int(getattr(self.audio, "CHANNELS", 2)) if self.audio else 2
        x = np.asarray(pcm, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] < ch:
            x = np.repeat(x[:, :1], ch, axis=1)
        elif x.shape[1] > ch:
            x = x[:, :ch]
        if self.gain != 1.0:
            x = x * np.float32(self.gain)
        # クリップさせない。パイプラインの先で歪むと原因が追いにくい。
        peak = float(np.abs(x).max()) if x.size else 0.0
        if peak > 0.999:
            x = x / peak * np.float32(0.999)
            log.warning("the stem was clipping, so it was scaled by %.2f", 0.999 / peak)
        return np.ascontiguousarray(x, dtype=np.float32)

    def prepare(self) -> dict:
        """パイプラインを起動し、レイテンシとクロック比を測る。

        1回だけ呼ぶ。演奏ごとに呼び直す必要はないし、呼ぶとデバイスを
        開き直して余計なばらつきを生む。
        """
        if not self.available:
            raise RuntimeError("cannot reach the audio pipeline (connected as no_media?)")

        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        self.reachy.media.start_playing()
        time.sleep(1.0)                    # クロックが動き出すのを待つ

        if self.use_net_clock:
            self._attach_net_clock()

        q = Gst.Query.new_latency()
        if self.pipe.query(q):
            _live, minl, _maxl = q.parse_latency()
            if minl != Gst.CLOCK_TIME_NONE:
                self.latency_ns = int(minl)

        clock = self.pipe.get_clock()
        if clock is None:
            raise RuntimeError("cannot get the pipeline clock")
        self.ratio, self.ratio_resid_ppm = self._measure_ratio(clock)

        self.stats = {
            "latency_ms": round(self.latency_ns / 1e6, 2),
            "clock_ratio": round(self.ratio, 9),
            "drift_ppm": round((self.ratio - 1.0) * 1e6, 1),
            "drift_resid_ppm": round(self.ratio_resid_ppm, 1),
            "rate": self.rate,
            "frames": int(self.pcm.shape[0]),
            "duration_s": round(self.pcm.shape[0] / self.rate, 3),
            "net_clock": self.use_net_clock,
        }
        return self.stats

    def _measure_ratio(self, clock, window_s: float = 2.5,
                       n: int = 50) -> tuple[float, float]:
        """パイプラインクロックと monotonic の進み方の比を測る。

        **2点で割ってはいけない。** GStreamer の AudioClock は ALSA の周期
        （ここでは 16 ms）でしか進まない階段状の時計なので、1回の読みは最大
        16 ms 古い。両端で独立に最大 16 ms ずれ得るから、2.5 秒窓の2点差では
        ±6400 ppm まで暴れる。実際、2点法で9台を測ったら −56〜+1112 ppm と
        散らばった（2026-08-07）。あれは水晶のばらつきではなく、ほぼ全部が
        この量子化ノイズだった。

        窓全体に等間隔で標本を取り、直線を当てはめると階段の踏み面が平均化
        されて消える。同じ 2.5 秒で桁違いに素直な値が出る。

        返り値の2つ目は残差（ppm換算）で、**推定がどれくらい信用できるか**を
        そのまま表す。これが大きいときは値を信じずに 1.0 として扱ったほうが安全。
        """
        ts, ms = [], []
        step = window_s / n
        for _ in range(n):
            ts.append(clock.get_time())
            ms.append(time.monotonic_ns())
            time.sleep(step)
        x = np.asarray(ms, dtype=np.float64)
        y = np.asarray(ts, dtype=np.float64)
        x -= x[0]
        y -= y[0]
        slope, intercept = np.polyfit(x, y, 1)
        resid = y - (slope * x + intercept)
        # 残差を「窓全体でどれだけの ppm に相当するか」に直す
        span = x[-1] - x[0]
        resid_ppm = float(resid.std() / span * 1e6) if span > 0 else float("inf")

        # 明らかに壊れた推定（クロックが止まっている等）は 1.0 に倒す。
        # 比を間違えるより「補正しない」ほうが害が小さい ―― 効くのは
        # スケジュール余裕ぶんの換算だけで、曲中の timing はバイト数で
        # 決まるので影響しない。
        if not np.isfinite(slope) or abs(slope - 1.0) > 0.01:
            log.warning("clock-ratio estimate is implausible (%.6f); using 1.0", slope)
            return 1.0, resid_ppm
        return float(slope), resid_ppm

    def _attach_net_clock(self) -> None:
        """PC を時刻源にした共有クロックへ切り替える（任意）。

        実測ではドリフトが -16〜-27ppm しかなく、24秒の曲では 0.65ms にしか
        ならないので既定では使わない。3分を超える曲や、より厳密に揃えたい場合の
        upgrade path として置いてある。PC 側 GStreamer 1.28.5 とロボット側 1.26.2 の
        相互接続は未検証なので、有効にするなら先に確認すること。
        """
        import os
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstNet", "1.0")
        from gi.repository import Gst, GstNet

        host = os.environ.get("ORCHESTRA_CLOCK_HOST")
        port = int(os.environ.get("ORCHESTRA_CLOCK_PORT", "8555"))
        if not host:
            log.warning("ORCHESTRA_CLOCK_HOST is not set; not using a shared clock")
            self.use_net_clock = False
            return
        clock = GstNet.NetClientClock.new("orchestra", host, port, 0)
        if not clock.wait_for_sync(5 * Gst.SECOND):
            log.warning("shared clock did not sync; continuing on the local clock")
            self.use_net_clock = False
            return
        self.pipe.use_clock(clock)
        self.pipe.set_start_time(Gst.CLOCK_TIME_NONE)
        self.pipe.set_base_time(0)
        log.info("switched to the shared clock (%s:%d)", host, port)

    # -- 時刻変換 --------------------------------------------------------
    def running_time_at(self, monotonic_s: float) -> int:
        """monotonic の時刻を、パイプラインの running time (ns) に写す。

        実測で base_time は 0 なので running time はクロック時刻そのもの。
        それでも `get_base_time()` を引いておく（将来 0 でなくなっても壊れない）。
        """
        clock = self.pipe.get_clock()
        now_c = int(clock.get_time() - self.pipe.get_base_time())
        now_m = time.monotonic_ns()
        delta_ns = (monotonic_s * 1e9) - now_m
        return int(now_c + delta_ns * self.ratio)

    # -- 再生 ------------------------------------------------------------
    def schedule(self, t0_monotonic: float) -> dict:
        """`t0_monotonic` に音の先頭が鳴るようスケジュールする。

        レイテンシと機体ごとの補正を引いた PTS を計算し、供給スレッドを起こす。
        """
        if not self.available:
            raise RuntimeError("cannot reach the audio pipeline")

        target = t0_monotonic - self.lead_ms / 1000.0
        pts0 = self.running_time_at(target) - self.latency_ns
        now_running = self.running_time_at(time.monotonic())
        margin_ms = (pts0 - now_running) / 1e6
        if margin_ms < 0:
            log.warning(
                "audio start time is %.0fms in the past; it has to be further "
                "ahead than the latency (%.0fms)",
                -margin_ms, self.latency_ns / 1e6)

        self._stop.clear()
        self.started_at_monotonic = t0_monotonic
        self._thread = threading.Thread(
            target=self._feed, args=(pts0,), name="audio-feed", daemon=True)
        self._thread.start()
        return {"pts0_ns": pts0, "margin_ms": round(margin_ms, 2),
                "latency_ms": round(self.latency_ns / 1e6, 2),
                "lead_ms": self.lead_ms}

    def _feed(self, pts0: int) -> None:
        """再生位置の先を追いかけながらチャンクを押し込む。

        appsrc にブロッキングも backpressure も無く、`current-level-bytes` も
        実測では常に 0 で当てにならなかった。なので壁時計でペーシングする。
        PTS は毎回サンプル数から厳密に計算するので、押し込むタイミングが多少
        ずれても音の位置は動かない。
        """
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        rate = self.rate
        n_total = int(self.pcm.shape[0])
        chunk = max(1, int(CHUNK_S * rate))
        pushed = 0
        sent = 0
        errors = 0

        while pushed < n_total and not self._stop.is_set():
            # このチャンクが鳴るべき時刻が、今からどれだけ先か
            pts = pts0 + (pushed * Gst.SECOND) // rate
            now_running = self.running_time_at(time.monotonic())
            ahead_s = (pts - now_running) / 1e9
            if ahead_s > LOOKAHEAD_S:
                time.sleep(min(0.05, ahead_s - LOOKAHEAD_S))
                continue

            end = min(n_total, pushed + chunk)
            block = self.pcm[pushed:end]
            buf = Gst.Buffer.new_wrapped(block.tobytes())
            buf.pts = int(pts)
            buf.dts = int(pts)
            buf.duration = int(len(block) * Gst.SECOND // rate)
            ret = self.appsrc.push_buffer(buf)
            if ret != Gst.FlowReturn.OK:
                errors += 1
                log.warning("push_buffer returned %s", ret)
                if errors > 20:
                    break
            pushed = end
            sent += 1

        self.stats.update({
            "chunks_sent": sent, "frames_pushed": pushed,
            "push_errors": errors,
            "complete": pushed >= n_total,
        })
        log.info("audio feed finished: %d chunks / %d frames (err %d)",
                 sent, pushed, errors)

    def stop(self) -> None:
        """供給を止め、まだ鳴っていない音を捨てる。

        中断時に音だけ鳴り続けるのを防ぐ。`stop_playing()` は使わない
        （デバイスを閉じてしまい、次の演奏で開き直しのばらつきを招く）。
        フラッシュだけで十分。
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            # appsrc から下流へフラッシュを流し、キューに残った音を捨てる
            self.appsrc.send_event(Gst.Event.new_flush_start())
            self.appsrc.send_event(Gst.Event.new_flush_stop(True))
        except Exception as e:
            log.warning("could not flush the audio: %s", e)
