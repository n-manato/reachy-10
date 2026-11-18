#!/usr/bin/env python3
"""ロボット上で音声パイプラインの素性と**スケジュール精度**を測る。

`orchestra fleet audio-probe` が各機に配って実行する。

一番知りたいのは「明示PTSで指定した時刻に、実際にその時刻に鳴るか」。
SDK には `sync=True` の appsink が既にぶら下がっていて（head wobbler 用）、
音がレンダリングされた瞬間に `feed(pcm, time.monotonic_ns())` が呼ばれる
(`media/audio_gstreamer.py:283-296`)。ここに自前のオブジェクトを差し込めば、
**自分が指定した時刻と実際にレンダリングされた時刻の差**が測れる。

注意: この appsink は audiosink のリングバッファより手前の tee 分岐なので、
スピーカーから出る瞬間より一定時間だけ早い。相対的なスケジュール精度と
台間のばらつきを見る用途には正確で、絶対的な「口から出る時刻」は
マイク実測（`orchestra calibrate audio`）で別途取る。

出力は最後の1行に JSON。
"""

from __future__ import annotations

import json
import sys
import time

RESULT: dict = {"ok": False}


class _Tap:
    """appsink のレンダリング時刻を捕まえるためのダミー wobbler。

    SDK は `_head_wobbler` に対して feed/start/stop/reset を呼ぶだけなので、
    同じ形をしたオブジェクトを置けば横取りできる。
    """

    def __init__(self):
        self.events: list[tuple[float, float]] = []   # (monotonic_ns, rms)

    def feed(self, pcm, play_at_monotonic_ns):
        try:
            import numpy as np
            a = np.asarray(pcm, dtype=np.float64).ravel()
            rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
        except Exception:
            rms = 0.0
        self.events.append((float(play_at_monotonic_ns), rms))

    def start(self): pass
    def stop(self): pass
    def reset(self): pass


def main() -> int:
    import numpy as np

    from reachy_mini import ReachyMini

    lead_s = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0

    reachy = None
    try:
        for backend in ("local", "gstreamer_no_video", "no_media"):
            try:
                reachy = ReachyMini(connection_mode="localhost_only",
                                    media_backend=backend)
                RESULT["media_backend"] = backend
                break
            except Exception as e:
                RESULT.setdefault("backend_errors", {})[backend] = str(e)[:120]
                reachy = None
        if reachy is None:
            RESULT["error"] = "cannot connect"
            print(json.dumps(RESULT)); return 1

        audio = getattr(reachy.media, "audio", None)
        pipe = getattr(audio, "_pipeline", None)
        appsrc = getattr(audio, "_appsrc", None)
        if audio is None or pipe is None or appsrc is None:
            RESULT["error"] = "cannot reach the pipeline / appsrc"
            print(json.dumps(RESULT)); return 1

        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        RESULT["gst_version"] = Gst.version_string()
        RESULT["sample_rate"] = rate = int(getattr(audio, "SAMPLE_RATE", 16000))
        RESULT["channels"] = ch = int(getattr(audio, "CHANNELS", 2))

        tap = _Tap()
        audio._head_wobbler = tap          # ← レンダリング時刻を横取りする

        reachy.media.start_playing()
        time.sleep(1.5)

        clock = pipe.get_clock()
        base = pipe.get_base_time()
        RESULT["clock"] = type(clock).__name__ if clock else None
        RESULT["base_time_ns"] = int(base)

        # クロックの進み方（monotonic 比）。1.0 から離れる分だけ、
        # monotonic 駆動のモーションと音がずれていく。
        # AudioClock は ALSA の周期(16ms)で階段状に進むので、1秒窓だと
        # ±800ppm も暴れて意味がない。窓を長く取る。
        drift_window = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
        t0, m0 = clock.get_time(), time.monotonic_ns()
        time.sleep(drift_window)
        t1, m1 = clock.get_time(), time.monotonic_ns()
        ratio = (t1 - t0) / (m1 - m0)
        RESULT["drift_window_s"] = drift_window
        RESULT["clock_vs_monotonic_ratio"] = round(ratio, 9)
        RESULT["drift_ppm"] = round((ratio - 1.0) * 1e6, 1)
        RESULT["drift_ms_per_min"] = round((ratio - 1.0) * 60_000, 2)

        q = Gst.Query.new_latency()
        if pipe.query(q):
            live, minl, maxl = q.parse_latency()
            RESULT["latency_min_ms"] = (round(minl / 1e6, 2)
                                        if minl != Gst.CLOCK_TIME_NONE else None)

        # --- スケジュール精度の実測 -------------------------------------
        # 無音を少し流してからトーンを置く。無音があるとレンダリングが連続し、
        # 「トーンが来た瞬間」を RMS の跳ね上がりで検出できる。
        n_sil = int(rate * lead_s * 1.5)
        n_tone = int(rate * 0.20)
        t = np.arange(n_tone) / rate
        env = np.minimum(1.0, np.minimum(t / 0.005, (n_tone / rate - t) / 0.005))
        tone = (0.30 * env * np.sin(2 * np.pi * 880.0 * t)).astype(np.float32)

        silence = np.zeros((n_sil, ch), dtype=np.float32)
        tone_pcm = np.column_stack([tone] * ch).astype(np.float32)

        now_running = int(clock.get_time() - pipe.get_base_time())
        start_pts = now_running + int(lead_s * Gst.SECOND)
        RESULT["scheduled_pts_ns"] = start_pts
        # 指定時刻を monotonic に写しておく（後で実測と比べるため）
        m_ref = time.monotonic_ns()
        c_ref = int(clock.get_time())
        scheduled_monotonic = m_ref + int((start_pts + pipe.get_base_time() - c_ref) / ratio)
        RESULT["scheduled_monotonic_ns"] = scheduled_monotonic

        def push(pcm, pts):
            buf = Gst.Buffer.new_wrapped(pcm.tobytes())
            buf.pts = int(pts)
            buf.dts = int(pts)
            buf.duration = int(len(pcm) * Gst.SECOND // rate)
            return appsrc.push_buffer(buf)

        r1 = push(silence, start_pts - int(n_sil * Gst.SECOND // rate))
        r2 = push(tone_pcm, start_pts)
        RESULT["push_returns"] = [str(r1), str(r2)]

        tap.events.clear()
        deadline = time.monotonic() + lead_s + 2.5
        while time.monotonic() < deadline:
            time.sleep(0.02)

        # RMS が跳ねた最初の瞬間 = トーンが実際にレンダリングされた時刻
        evs = tap.events
        RESULT["tap_events"] = len(evs)
        if evs:
            rmss = [e[1] for e in evs]
            peak = max(rmss)
            RESULT["tap_peak_rms"] = round(peak, 5)
            thresh = max(peak * 0.35, 1e-4)
            hit = next((e for e in evs if e[1] >= thresh), None)
            if hit and peak > 1e-4:
                actual_ns = hit[0]
                raw_ms = (actual_ns - scheduled_monotonic) / 1e6
                # パイプラインは申告レイテンシぶん遅らせてレンダリングする。
                # これは誤差ではなく仕様なので、引いてから精度を見る。
                lat = RESULT.get("latency_min_ms") or 0.0
                RESULT["render_raw_offset_ms"] = round(raw_ms, 2)
                RESULT["render_error_ms"] = round(raw_ms - lat, 2)
                # レイテンシを引いた残差が小さければスケジュールは効いている
                RESULT["scheduling_works"] = bool(abs(raw_ms - lat) < 40)
            else:
                RESULT["render_error_ms"] = None
                RESULT["scheduling_works"] = False

        RESULT["ok"] = bool(RESULT.get("scheduling_works"))
    except Exception as e:
        import traceback
        RESULT["error"] = f"{type(e).__name__}: {e}"
        RESULT["trace"] = traceback.format_exc(limit=3)
    finally:
        if reachy is not None:
            for fn in (lambda: reachy.media.close(),
                       lambda: reachy.client.disconnect()):
                try:
                    fn()
                except Exception:
                    pass

    print(json.dumps(RESULT))
    return 0 if RESULT.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
