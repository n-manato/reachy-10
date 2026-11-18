#!/usr/bin/env python3
"""ロボット上で走る自己診断。カメラ・スピーカー・マイク・モータを実際に動かす。

`orchestra fleet selftest` が各機に配って実行し、JSON を1行返させる。

なぜ必要か: 「メディアバックエンドが初期化できた」と「実際に音が出た/映像が
取れた」は別物。初期化はエラー無く通るのに、スピーカーが鳴っていない・カメラが
黒フレームしか返さない、という状態はいくらでもあり得る。ここでは実際に

  * カメラから1フレーム取得して、輝度分散が0でないこと（真っ黒/固定値でない）
  * スピーカーからトーンを鳴らし、それを**自分のマイクで録って**検出できること
  * モータのトルクを入れて微小に動かし、指令に追従すること

まで確かめる。スピーカー・マイクを1つの試験で同時に検証できるのが折り返し
試験の利点で、人が聴いて確認する必要がない。

出力は最後の1行に JSON。前段の SDK やライブラリのログは無視してよい。
"""

from __future__ import annotations

import json
import math
import struct
import sys
import time
import wave
from pathlib import Path

RESULT: dict = {"ok": False, "checks": {}}
TONE_HZ = 440.0


def check(name: str, fn) -> None:
    t0 = time.monotonic()
    try:
        detail = fn()
        RESULT["checks"][name] = {"ok": True, **(detail or {})}
    except Exception as e:
        RESULT["checks"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    RESULT["checks"][name]["ms"] = round((time.monotonic() - t0) * 1000)


def make_tone(path: Path, freq: float = 440.0, seconds: float = 6.0, sr: int = 44100):
    n = int(sr * seconds)
    frames = bytearray()
    for i in range(n):
        # 端をフェードしてクリックノイズを避ける
        env = min(1.0, i / (sr * 0.05), (n - i) / (sr * 0.05))
        v = int(22000 * env * math.sin(2 * math.pi * freq * i / sr))
        frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def main() -> int:
    import numpy as np

    from reachy_mini import ReachyMini

    global TONE_HZ
    TONE_HZ = float(sys.argv[1]) if len(sys.argv) > 1 else 440.0
    tone = Path("/tmp/orchestra_selftest_tone.wav")
    make_tone(tone, freq=TONE_HZ)

    reachy = None
    try:
        # エージェントと同じ接続の仕方をする。ここで差が出ると診断の意味がない。
        for backend in ("local", "gstreamer_no_video", "no_media"):
            try:
                reachy = ReachyMini(
                    connection_mode="localhost_only",
                    media_backend=backend,
                    automatic_body_yaw=True,
                )
                RESULT["media_backend"] = backend
                break
            except Exception:
                reachy = None
        if reachy is None:
            RESULT["error"] = "could not connect on any media backend"
            print(json.dumps(RESULT))
            return 1

        # ---- カメラ: 実際に1フレーム取る ----
        def cam():
            frame = None
            for _ in range(25):           # 起動直後は None が返ることがある
                frame = reachy.media.get_frame()
                if frame is not None:
                    break
                time.sleep(0.2)
            if frame is None:
                raise RuntimeError("no frame after 25 attempts")
            a = np.asarray(frame)
            std = float(a.std())
            return {
                "shape": list(a.shape),
                "std": round(std, 2),
                # 分散ゼロ = 真っ黒か固定値。センサは居るが絵が来ていない状態。
                "live": std > 1.0,
            }

        check("camera", cam)

        # ---- スピーカー + マイク: 折り返し ----
        def audio_loopback():
            sr_in = reachy.media.get_input_audio_samplerate()
            reachy.media.start_recording()
            time.sleep(0.4)

            def capture(seconds: float):
                buf = []
                t0 = time.monotonic()
                while time.monotonic() - t0 < seconds:
                    s = reachy.media.get_audio_sample()
                    if s is not None and len(s):
                        buf.append(np.asarray(s, dtype=np.float64).ravel())
                    time.sleep(0.01)
                return np.concatenate(buf) if buf else None

            def tone_energy(x, sr, freq, half_bw=25.0):
                """そのトーンの周波数だけのエネルギーを、全帯域比で返す。

                広帯域 RMS だと、同じ部屋の別のロボットの音・空調・サーボ音まで
                拾ってしまい判定にならない。狙った周波数の成分が全体に占める割合
                なら、周囲がうるさくても自分の音だけを見られる。
                """
                if x is None or len(x) < sr // 8:
                    return None
                w = x * np.hanning(len(x))
                spec = np.abs(np.fft.rfft(w)) ** 2
                fr = np.fft.rfftfreq(len(w), 1.0 / sr)
                band = (fr > freq - half_bw) & (fr < freq + half_bw)
                total = float(spec.sum()) + 1e-20
                return float(spec[band].sum() / total)

            quiet = tone_energy(capture(1.0), sr_in, TONE_HZ)
            if quiet is None:
                raise RuntimeError("no samples from the microphone (the capture path is dead)")

            # GStreamer playbin は Pi 上で立ち上がりに 0.3-1.5 秒かかることがある。
            # 固定の短い窓で測ると、鳴っているのに取り逃がす（実際に一度そうなった）。
            # 長めのトーンを鳴らし、検出できるまで窓をずらして追いかける。
            reachy.media.play_sound(str(tone))
            best, best_at, windows = 0.0, None, []
            t_start = time.monotonic()
            while time.monotonic() - t_start < 5.5:
                e = tone_energy(capture(0.5), sr_in, TONE_HZ)
                if e is None:
                    continue
                windows.append(round(e, 5))
                if e > best:
                    best, best_at = e, round(time.monotonic() - t_start, 2)
                if e > 0.02 and e > quiet * 4.0:
                    break
            time.sleep(0.3)

            ratio = (best + 1e-9) / (quiet + 1e-9)
            return {
                "samplerate": sr_in,
                "tone_hz": TONE_HZ,
                "band_frac_quiet": round(quiet, 5),
                "band_frac_peak": round(best, 5),
                "peak_at_s": best_at,
                "windows": windows[:12],
                "ratio": round(ratio, 1),
                # 自分のトーンの帯域だけが明確に立ち上がったか。判定は比率主体:
                # 絶対値(帯域占有率)は環境の広帯域ノイズ量で上下するので、単独の
                # しきい値にすると静かな部屋と騒がしい部屋で結論が変わってしまう。
                "heard": (ratio > 4.0 and best > 1e-3),
            }

        check("audio_loopback", audio_loopback)

        # ---- モータ: 実際に動かして追従を見る ----
        def motors():
            import numpy as np

            reachy.enable_motors()
            time.sleep(0.4)
            head, ant = reachy.get_current_joint_positions()
            before = float(np.asarray(ant)[0])
            # アンテナだけを小さく動かす。頭は動かさないので机上でも安全。
            reachy.goto_target(antennas=[-0.6, 0.6], body_yaw=None, duration=0.8)
            time.sleep(0.3)
            _, ant2 = reachy.get_current_joint_positions()
            moved = float(np.asarray(ant2)[0])
            reachy.goto_target(antennas=[-0.1745, 0.1745], body_yaw=None, duration=0.8)
            time.sleep(0.3)
            reachy.disable_motors()
            delta = abs(moved - before)
            return {
                "antenna_before_deg": round(math.degrees(before), 1),
                "antenna_moved_deg": round(math.degrees(moved), 1),
                "delta_deg": round(math.degrees(delta), 1),
                "responded": delta > 0.15,   # ~9度以上動いたか
            }

        check("motors", motors)

        # ---- IMU（Lite には無い） ----
        def imu():
            v = reachy.imu
            if v is None:
                return {"present": False, "note": "this unit has no IMU (Lite)"}
            return {"present": True, "keys": sorted(v.keys())}

        check("imu", imu)

        RESULT["ok"] = all(
            c.get("ok") for c in RESULT["checks"].values()
        ) and RESULT["checks"]["camera"].get("live", False) \
          and RESULT["checks"]["audio_loopback"].get("heard", False) \
          and RESULT["checks"]["motors"].get("responded", False)

    finally:
        if reachy is not None:
            try:
                reachy.disable_motors()
            except Exception:
                pass
            try:
                if reachy.media_released:
                    reachy.acquire_media()
            except Exception:
                pass
            for fn in (lambda: reachy.media.close(),
                       lambda: reachy.client.disconnect()):
                try:
                    fn()
                except Exception:
                    pass

    print(json.dumps(RESULT))
    return 0 if RESULT["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
