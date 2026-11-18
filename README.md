# Reachy Mini Orchestra — Mande ensemble

A synchronized robot orchestra: **1 lead + 8 instrumentalists**, driven from one
PC over an isolated Wi-Fi network, on a shared musical timeline. The show runs
about 200 seconds: the lead introduces each player, the ensemble plays
**Lamban**, and all nine bow and put their faces away.

**Lamban** (also *Djelidon*, *Djelifoli*, *Lamba*) is the Maninka/Malinké piece
danced for the **jeli** — the griot. It was born on the balafon, in the Kouyaté
lineage, and its traditional instrumentation is balafon, *djeli dundun* and
*tama*, with kora alongside; in Guinea and Senegal a djembe-and-dunun battery
joins in. That matters here: this is one of the few pieces where **melodic and
percussion instruments together are the tradition itself**, not a modern
arrangement — so a mixed eight-piece robot ensemble is a faithful reading rather
than a compromise. There is no singing.

The eight parts, one per robot: **balafon · kora · ngoni · tama · djembe ·
dundunba · sangban · karignan**. Every rhythm and figure in the score is marked
`[DOC]` (a transcription with a citation) or `[RECON]` (my reconstruction from
sourced constraints) in `tools/make_lamban.py`. See *Note on the music* at the
end.

**No physical instruments.** Every sound comes out of the robots' own speakers;
the motion is air-instrument acting. That inversion matters more than it sounds:
when a beater hit a real chime bar, motion sync *was* musical sync. Now audio
alignment is the music and motion only has to look right. Audio timing is
therefore the first-class concern, not a follow-on.

Everything here is built against the *actual* Reachy Mini SDK and the *actual*
behaviour of these nine robots, both of which were inspected before a line was
written. Where the code makes a claim about the hardware, the source file and
line are cited.

---

## The one thing to understand first

**The Reachy Mini stack enforces no motion limits at all.** There is no velocity
limit, no acceleration limit and no workspace clamp anywhere in the Python stack;
the URDF's `velocity="8"` is metadata only. The single failure mode is IK
returning NaN, and the daemon catches that exception, throttle-logs it and
carries on:

```python
# daemon/backend/robot/backend.py:240-243
except ValueError as e:
    log_throttling.by_time(self.logger, interval=0.5).warning(f"IK error: {e}")
```

So an unreachable or too-fast pose produces a **silent freeze at the last valid
target** — not an error you can detect from the client. On top of that, this
firmware exposes **no motor temperature, current or load** anywhere in its API
(`GET /api/motors/status` returns exactly `{"mode": "..."}`), so there is no
runtime thermal watchdog to fall back on either.

Every safety guarantee in this project therefore lives in
[`orchestra/motion/validate.py`](orchestra/motion/validate.py), which runs on the
PC, at the song's real tempo, before anything reaches a robot. Nothing is
deployed unvalidated — `song build` refuses to write a failing build.

---

## Why an on-robot agent

The stock daemon has **no scheduled-execution primitive**. There is no "start at
absolute time T" in the protocol, the daemon, or the Rust motor layer; every
`timestamp` field on the wire is written by the client and never read by the
server. That leaves three options, and only one of them holds a beat across nine
robots:

| Approach | Skew | Status |
|---|---|---|
| PC streams targets at 50 Hz | 10 ms – 500 ms+ | The sibling `ethics-podcast` project measured ping going from ~15 ms to **500–1400 ms with timeouts** while streaming to just *three* robots. Nine is 450 msg/s on one channel. Rejected. |
| `PlayUploadedMoveCmd` fan-out | **18 ms measured** | Works, no SSH needed (`orchestra show play`). Kept as the fallback. At the edge of audible for percussion. |
| **On-robot agent + clock sync** | **0.999 ms measured** | ✅ The production path (`orchestra show run`). |

Both lower rows are **measured on this fleet**, not estimated — 9 robots,
full 24.3 s performance. See [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md).

So the whole performance is preloaded, the PC and each robot agree on a shared
timebase up front, and START carries an absolute instant a few seconds in the
future. Each robot then runs its own 100 Hz loop against its own monotonic clock.
Cross-robot skew collapses to clock-sync error, and Wi-Fi traffic during the show
drops to a 1 Hz heartbeat.

It also handles audio locally: each robot plays its own stem from local disk
through its own speaker, on the same clock as its own motion, never touching the
WebRTC signalling that nine identically named `reachy-mini` hosts would corrupt.

⚠ **`connection_mode="localhost_only"` is not sufficient on its own.** The SDK
picks the local backend only when localhost_only **and**
`is_local_camera_available()` ([`reachy_mini.py:338`]); otherwise it silently
falls back to WebRTC, which fails with `Connection refused`. That took out two of
nine robots on the first live run. The agent therefore requests backends
explicitly — `local` → `gstreamer_no_video` → `no_media` — and degrades to silent
motion rather than failing.

```
                    Windows PC
   song.mid + song.yaml ──► music/timeline ──┐
   clips/*.clip.json ─────► motion/compile ──┤
                                             ▼
                                  motion/validate  ◄── SAFETY GATE
                                             ▼
                                    show/build ──► per-robot program + stem
                                             │
                    ══ DEPLOY (before the show) ══
                                             ▼
   ┌─────────────────────────────────────────────────────────┐
   │ each robot: agent/orchestra_agent.py                    │
   │   • localhost WebSocket (sub-ms, no Wi-Fi in the loop)  │
   │   • clock sync with the PC (Cristian's algorithm)       │
   │   • sleep_until(T0 − offset), then a local 100 Hz loop  │
   │   • local WAV playback at T0 + audio_lead_ms            │
   └─────────────────────────────────────────────────────────┘
                      ▲  ~1 msg/s control channel
                      │  ARM / SYNC / START(T0) / ABORT / heartbeat
```

---

## Quick start

```bash
# Linux。3.11 を明示する（システムの python3 は 3.8）。
# `reachy-mini` は Linux で PyGObject を引き、PyGObject には wheel が無い。
# 下のヘッダが無いと pycairo のビルドで死ぬ。
sudo apt-get install -y libcairo2-dev libgirepository1.0-dev libglib2.0-dev
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[plot,dev]"

python tools/make_mande_samples.py     # 8楽器を数値合成 -> samples/mande/
python tools/audition_samples.py       # 合成音を1本にまとめて聴く（最重要）
python tools/make_lamban.py            # 採譜 -> songs/lamban/lamban.mid
python tools/make_stems.py songs/lamban   # ロボット1台ごとの音声ステム
python tools/preview_song.py songs/lamban # 全部を足して試聴

orchestra clip list                    # どんな動きがあるか
orchestra clip check --bpm 96 --bpm 120   # クリップ単体の安全ゲート
orchestra song build lamban --report   # 9台ぶんコンパイル + 検証

orchestra fleet status                 # 全機生きているか
orchestra fleet discover --write       # hardware_id で IP を突き合わせる
orchestra fleet provision              # 各ロボットにエージェントを入れる
orchestra fleet agent start
orchestra show deploy lamban
orchestra show run lamban              # arm, sync, start, monitor, park
```

追い込まれたときに思い出す2つ:

```bash
orchestra show stop      # 穏当: エージェントが中立へ寄せてからトルクを切る
orchestra show estop     # 強制: HTTP で直接モーターを落とす（エージェントを迂回）
## While it is playing

A run is 204 seconds during which nothing else can be typed, so the useful
controls are keys:

| key | |
|---|---|
| ↑ / ↓ | volume, ±3 across all nine |
| → / ← | volume, ±1 — for settling on a level |
| `s` | finish the current take, then stop (`--loop` only) |
| ctrl-C | abort now: agents ramp to neutral, then park |

**Arrow keys need a real console.** Whether they arrive is up to the terminal,
not to us, and finding out 148 seconds into a performance is the wrong moment:

```bash
orchestra show keys      # ten seconds, no robots involved
```

A standalone PowerShell or Windows Terminal window gives a real console. Piped
or redirected stdin gives line input only — `s` still works with Enter, arrow
keys cannot, and the banner says so at startup rather than failing silently.

Volume is not sent through the daemon's `/api/volume/set`: **that route plays
`impatient1.wav` on every call**, which mid-show is one robot making a foreign
noise. It goes through the agent instead, over the SDK session that is already
open, which reaches the same mixer and makes no sound. Keypresses are coalesced
to at most ten fan-outs a second — a held arrow repeats about thirty times a
second, and nine robots times thirty is RPC traffic that would start delaying
the status polling that decides whether a robot has dropped out.

The level is saved to `config/calibration/<robot>.yaml` on exit, but only if you
actually moved it, and restored on the next run.

`estop` deliberately does not go through the agents, so it still works when one
is hung.

---

## How the sound works

**すべて数値合成**。参考にした構成は Windows の `gm.dls`（Roland の GM 音源）から
実録音を抜いていたが、GM には kora も ngoni も balafon も djembe も dunun も
入っていない。コンガやハープで代用すると単に別の楽器に聞こえるうえ、この編成に
要る2つのものが原理的に表現できない:

1. **打法** — djembe の bass / tone / slap、dunun の開放 / ミュート / 鈴。
   GM の打楽器は1楽器1波形なので打ち分けられない。
2. **音律** — マンデのバラフォンはほぼ等間隔の七音音階（1ステップ約 **171 cent**
   = 1200/7）で、12平均律の鍵盤では出せない音程が並ぶ。コラはさらに別で、
   ここでは **Sauta** 調弦（F 185 G 220 A 200 B 95 C 185 D 220 E 95 f）を使う。

`orchestra/audio/mande.py` が全部を合成する:

| 系統 | 方式 |
|---|---|
| djembe / dunun / tama | 円形膜のモード（ベッセル零点比）を2極共振器のバンクにして、帯域を絞った打撃ノイズで励起。打法の差は**励起の帯域と長さ**で作る |
| tama のベンド | 加算合成。共振器は係数固定でグライドが掛けられないので、あれだけ別方式 |
| balafon | 自由端棒のモードをアンダーカットに倣って 1:3:6 へ寄せ、**瓢箪のビリつき**を包絡線で開くゲート付きノイズで足す。マリンバとの差はほぼここ |
| kora / ngoni | 拡張 Karplus-Strong。分数遅延で音程の量子化誤差を消し、さらに**鳴らして測って合わせる**（実測誤差 0.5 cent 以内） |
| karignan | 溝を弾く密なインパルス列を金属共鳴へ。往と復で密度を変える |

出力は既存サンプラーが読む形式そのまま（WAV + `manifest.json`）なので
`SampleLibrary` は無改造。将来実録音が手に入ったら manifest のエントリを
差し替えるだけで乗り換えられる。

```
mande.py  →  make_mande_samples.py  →  samples/mande/*.wav + manifest.json
          →  sampler（ゾーン = 打法 / 棒 / 弦）
          →  render.py (22050 Hz)  →  16000 Hz へ  →  アンサンブル均し
          →  songs/lamban/audio/<part>.wav      1台につき1本
```

間違えやすい点が2つ:

**ゾーン番号は音高ではない。** マニフェストの `unity_note` に「その波形が本来
鳴らす音」を書くと、`play_note` の音程比が 1.0 になって**合成した周波数がそのまま
鳴る**。だから 12平均律を経由せずに任意の音律が出せる。代わりに MIDI ノート番号は
「何番目の棒か」「どの打法か」を指す記号になる。マニフェストには `"tuned": true`
を立ててあり、`Instrument.auto_tune()`（12平均律からのずれを誤差として補正する）を
通さない。通すと焼き込んだ音律が壊れる。

**16 kHz は選択ではない。** ロボットの音声経路が固定でこの形式を要求する
（`media/audio_base.py:59-60`）。可聴帯域は 8 kHz まで。参考元はタンバリンの
エネルギーの69%が 8 kHz 超にあって別の楽器に聞こえる事故を起こしていた。
ここでは合成の段で 7 kHz にローパスして最初から帯域内に収めている ――
後から音程を下げて救うより素直。実測で全楽器 8 kHz 超のエネルギーは 0.0%。

**1台につき1本のステム。共有ミックスにはしない。** 9台が同じ音を数ミリ秒ずれて
鳴らすとコムフィルタになって痩せる。別々のパートなら残ったズレは「9人が演奏して
いる」ように聞こえ、しかも音の出る場所と動く場所が一致する。

## Layout

```
config/
  robots.yaml          fleet registry, keyed on hardware_id (never on IP)
  fleet.yaml           network, deployment and every safety limit
  calibration/         per-robot measured values (audio lead)
orchestra/
  cli.py               the `orchestra` command
  config.py            config loading, SafetyLimits, RobotRegistry
  motion/              schema · compile · validate · layers · program · record
  motion/persona.py    per-instrument stance, beat-locked turn, intro & bow
  music/               tempo · timeline · midi · score
  conductor/           procedural conducting patterns
  fleet/               discovery · health · ssh · link (agent control channel)
  clock/               Cristian's algorithm + drift tracking
  show/                build · deploy · run
  preview/             single-robot playback · plots
orchestra/audio/
  mande.py             8つのマンデ楽器の数値合成（モーダル / Karplus-Strong）
  sampler.py           ゾーン = 打法・棒・弦。音律は焼き込み済み（tuned）
  render.py            プリセット表とステムのレンダリング・均し
agent/
  orchestra_agent.py   runs ON each robot
  audio_stream.py      explicit-PTS PCM streaming into the robot's speaker
clips/                 authored motion, beat-relative keyframes
                       すべて中立で始まり中立で終わる（下の注記を参照）
samples/mande/         合成した音源 — gitignored、いつでも再生成できる
songs/lamban/          song.yaml · MIDI · per-robot audio stems
tools/
  make_mande_samples   音源合成      audition_samples  合成音の試聴
  make_lamban          採譜 -> MIDI  make_clips        クリップの初回オーサリング
  make_stems           ステム        preview_song      全部を足して試聴
  measure_skew · sync_selftest · import_midi · install_ssh_keys
tests/                 185 tests
docs/EXPERIMENTS.md    what still has to be measured on hardware
```

---

## The motion format

Two layers, deliberately separated. **Authoring** is beat-relative keyframes that
a person edits; the **compiled program** is a dense uniform frame table that a
robot plays.

```jsonc
{
  "name": "sangban_beat",
  "instrument": "sangban",
  "units": { "translation": "mm", "rotation": "deg" },
  "placement": "trigger",
  "anchors": [0.3],          // ← the instant SOUND IS PRODUCED
  "tracks": {
    "head": [
      { "beat": 0.00, "pose": { "z": 0,    "pitch": -5 }, "interp": "minjerk" },
      { "beat": 0.14, "pose": { "z": 3,    "pitch": -7 }, "interp": "linear"  },
      { "beat": 0.30, "pose": { "z": -7.5, "pitch": 10 }, "interp": "linear"  },
      { "beat": 0.42, "pose": { "z": -1,   "pitch": 3  }, "interp": "minjerk" },
      { "beat": 0.80, "pose": { "z": 0,    "pitch": 0  }, "interp": "minjerk" }
    ],
    "antennas": [ /* ... */ ]
  }
}
```

Four decisions worth explaining:

- **Time in beats.** A clip is reusable at any tempo and binds directly to the
  MIDI tempo map. Seconds are derived at compile time.
- **`anchors` mark the sound, not clip start.** The compiler places the clip at
  `note_beat − anchor`, so the windup happens *before* the beat, exactly as a
  player's does. This is the single thing that makes strikes land musically
  rather than late.
- **`anchors` are the one record of when sound happens**, and everything hangs
  off them: the anchor alignment above, the secondary-motion mute window, the
  gesture-overlap check, the duty-cycle heat estimate, and the note timing in
  `make_stems.py`. One source of truth for both the sound and the movement — write
  the audio timing separately and it will drift apart eventually.

  There used to be a second, narrower set (`contact`) marking anchors that
  physically struck something, which selected a laxer safety model. Removing the
  instruments removed the meaning of that flag, but **not the load on the servos**
  — an air-instrument gesture works the same motors just as hard. Splitting the
  duty-cycle check on it would have silently disabled the only heat protection
  there is, so the distinction is gone and every anchor counts.
- **XYZRPY in mm/deg, per-channel tracks.** Nobody hand-edits a rotation matrix,
  and head, body-yaw and antennas genuinely have independent rhythms.

Interpolation reproduces the SDK's four curves *exactly*
(`tests/test_interp.py` asserts bit-identical agreement), so a clip previewed
through `goto_target` and the same clip played by the agent are the same motion.

---

### クリップは中立で始まり中立で終わる

この編成で一番手こずったのはここ。コンパイルは**中立からの差分の足し算**なので、
クリップが中立以外の値で終わると、その差分が「常時かかるオフセット」として残る。
16分音符で発火するパートは1打あたり4つ前後のクリップが重なるため、わずかな
居残りが4倍に積み上がる。

実際に起きたこと ―― コラのクリップはアンテナを 18° から始めて 18° で終えていた
（中立は 10°）。差分 +8° が常時、しかも重なって積算され、**アンテナが可動端の
150° に張り付いた**。厄介なのは自動フィットが効かなかったこと: 振幅を 1/34 まで
縮めても実効値が全く下がらない。**振幅ではなくオフセットの重なりだった**ので、
振幅をいくら縮めても中立からの居残りは消えなかった。

```
症状: antenna_r が -150°〜-2° を振り回す。song fit が「収束しない」で終わる。
     ears x0.536 -> x0.313 -> x0.178 -> x0.099 -> x0.053 -> x0.029  実効値 4.93 のまま
原因: クリップの静止値が中立と違う。placement は加算合成。
直し: 全トラックを中立始まり・中立終わりに書き直す。実効値 4.93 -> 2.28
```

分担はこう考えるのが正しい:

- **持続する姿勢は `persona.Stance` が持つ。** 定常オフセットなので速度ゼロ、
  発熱制限に一切効かない。だから大きく取れる。
- **クリップは打点の前後だけ動いて、必ず中立へ帰る。** タイミングを伝えるのが
  仕事で、個性を伝えるのは仕事ではない。

`tools/make_clips.py` はこの規則で書いてある。

## The cast

Reachy Mini has **9 motors and no arms**: 6 Stewart actuators driving the head,
1 body yaw, 2 antennas. With nothing mounted, the whole job of the motion is to
make it *legible* — you should be able to tell who is playing what from across
the room, without hearing a note.

**The first version of this failed.** Every clip was authored around the strike —
a small up-down on each note — and from five metres away all nine looked the
same. Measuring why was the turning point:

| axis | operating limit | largest value in any clip | used |
|---|---|---|---|
| head z | ±22 mm | 5.0 mm | 23 % |
| head y | ±25 mm | 5.0 mm | 20 % |
| tilt cone | 25° | 7.0° | 28 % |
| **body yaw** | **150°** | **2.5°** | **1.7 %** |

Position was never the constraint. **Dynamics** were: joint velocity 4.0 rad/s
and a 10-second RMS duty ceiling. Since velocity is amplitude ÷ time, a fast
per-note gesture *must* be small — but a slow or sustained one can be enormous.
The identity had been put in the one place it could not grow.

So it moved. `orchestra/motion/persona.py` gives each instrument three layers,
and the per-note strike is now only the third of them:

1. **Stance** — a held posture. Zero velocity, so it costs nothing in the duty
   budget, and it is visible even while that robot is resting.
2. **Beat-locked body rotation** — the body swings around *forward* on the beat,
   with a different amplitude, period and phase per instrument. Coupled roll
   trails it by a quarter cycle.
3. **Per-note strike** — the existing clips, now carrying timing rather than
   identity.

| Robot | Part | Swing | Period | How you recognise it |
|---|---|---|---|---|
| r01 | lead (djembefola) | — | — | Faces the audience. Beat patterns with prep, ictus and cutoff; calls and breaks. Procedural, not a clip. |
| r02 | balafon | ±13° | 4 beats | **Travels furthest sideways** — three octaves of bars, crossed over four bars. Looks down at them. |
| r03 | kora | ±4° | 4 beats | **The stillest robot; the ears do the playing.** 21 strings plucked by thumbs and index fingers — the body never moves. |
| r04 | ngoni | ±7° | **1 beat** | **Leans in deepest** and rides every single beat. Fastest period on the floor. |
| r05 | tama | ±10° | 2 beats, **offbeat** | **Permanently cocked to one side** — the drum is held under an arm. **The only asymmetric ears.** |
| r06 | djembe | ±15° | 2 beats | **The deepest vertical strike** — struck with bare hands, so it falls furthest and rebounds hardest. |
| r07 | dundunba | ±15° | **8 beats** | **Lowest, slowest, largest arc.** The ears stay out of it; the weight of the upper body is the whole point. |
| r08 | sangban | ±13° | 4 beats, **antiphase** | Mirrors r07 — **opposite facing, opposite phase, half the period.** The two dununs read as a conversation. |
| r09 | karignan | ±7° | 1 beat, **antiphase** | **Never stops** — scraping *is* the sound, so there is no instant of rest. Tilts the opposite way to r05. |

**構えは互いに違う軸で立てる。** 全員が同じ軸で大小を競うと「なんとなく違う」に
しかならない。体の向き（dundunba +14° / sangban −14°）、高さ（kora が最も高く
dundunba が最も低い）、傾き（tama だけが傾ぎ続ける）、耳（kora だけ別格に開き、
tama だけ左右非対称）、止まらない（karignan だけ）、拍の周期（1 → 2 → 4 → 8 拍に
散らす）。

**Body yaw is centred on forward for all nine.** An earlier version parked each
robot at a fixed angle (marimba −55°, wood block +55°) so they faced permanently
off to one side, which just read as *robots pointing the wrong way*. Identity
comes from **how they swing**, not which way they face.

Measured after the change — `body_yaw_peak_deg` from `song build --report`,
against the 2.5° that every robot shared before:

```
r08 sangban 49.2   r07 dundunba 42.1   r05 tama     35.3   r02 balafon 23.8
r06 djembe  17.2   r04 ngoni     9.4   r09 karignan  9.3   r03 kora     6.3
```

Up to **10.8×**. The spread matters more than the magnitude: if all nine landed
on the same number the original problem would be back.

Underneath all nine runs a **groove layer**: everyone rides the beat lightly at
all times, including while resting. Whether that layer is on is the difference
between nine robots doing things near each other and an ensemble.

### The stance is not free

The obvious assumption — a constant offset has zero velocity, so it costs
nothing — is wrong, and the safety gate proved it:

```
r04 glockenspiel   joint_vel_peak  3.078 -> 3.763   (+22 %, ceiling 4.0)
r02 celesta        joint_vel_peak  3.508 -> 3.801   (+8 %)
（上は参考元の編成での実測。この編成でも同じことが起きるので、構えを触るたび
 `orchestra song build lamban --report` を読むこと。現状の最悪値は上限の 84 %）
r05 marimba        joint_vel_peak  2.710 -> 2.747   (+1 %)
```

The offset itself carries no velocity, but it **moves the Stewart platform to a
different operating point, so the Jacobian changes** and the existing per-note
motion costs more joint travel there. How much depends entirely on the pose
(+1 % to +22 %), so a budget table is not enough — `song build --report` has to
be read after every change. On r04 the driver turned out to be the chin-up
*pitch*, not the yaw.

⚠ **Antennas are xc330-class at ±0.8 N·m — a quarter of the Stewart servos.**
The easiest motor on the robot to destroy — and for a while the duty-cycle gate
was not watching them at all, which left r09 (the one part designed around them)
completely unprotected. Both the head and the antennas now carry their own
sustained-work limit. See `docs/EXPERIMENTS.md`.

Hard limits, all from the SDK/URDF: head tilt cone **±35°**, head yaw relative to
body **±55°** (Placo) / ±65° (analytical), body yaw **±160°**, Stewart joints
asymmetric between −80° and +80°. Our operating limits sit inside all of them.

⚠ **The head pose is in world frame.** `validate.py` computes
`rel_yaw = head_yaw − body_yaw` against a 45° limit, so turning the body without
turning the head spends the entire turn out of that budget. Every persona turns
both together.

---

## The shape of a show

A program is **204.3 s**. The music is the middle 149 s of it.

```
  0.0 –   1.0   wake into the waiting pose
  1.0 –   5.2   the lead greets the audience
  5.2 –  33.6   eight introductions, 3.55 s each:
                  1.0 s  the lead cues that robot
                  2.2 s  that robot sounds its instrument, moving big
                  0.35s  gap
                everyone else holds one identical pose, completely still
 33.6 –  36.6   the last cue: all nine lift their faces together
 39.1           T0. the piece begins at 96 BPM
                  bars  0- 3   balafon alone — the tuning and the 2-bar cycle
                  bars  4- 7   kora and ngoni enter
                  bars  8- 9   karignan locks the cycle
                  bar  10      the lead's call; the dununs enter on its last beat
                  bars 11-22   slow "song" section        100 BPM
                  bars 23-30   the tama answers           104 BPM
                  bars 31-38   balafon descending runs    108 BPM
                  bar  39      break
                  bars 40-51   fast section               112 BPM
                  bars 52-63   échauffement — the sangban signals it,
                               the dundunba goes near-continuous   116 BPM
                  bars 64-65   ending figure              120 BPM
188.4           the piece ends
190.6 – 195.8   bow, held (the lead turns its body 180° to face the audience)
199.4 – 202.8   stow: the head sinks 44 mm, ears fold back
```

**加速は構造であって演出ではない。** 「マリの曲は歌と共に遅く始まり、踊りと共に
速くなる」というのは Lamban について文献に書かれていることで、échauffement
（加熱部）はその形式化された名前が付いた区間。サンバンが始めを告げ、サンバンの
一撃で終える ―― これも記録されている手順。

**上限 120 BPM は音楽ではなく安全ゲートが決めている。** 発音する動作の間隔は
110 ms 以上でなければならず（`gesture_min_interval_s`）、16分音符は 120 BPM で
ちょうど 125 ms。ここが天井。実測の最小間隔は 125 ms（r03/r06/r07）。
文献にある Lamban の標準テンポは 110 BPM なので、この天井の内側に収まっている。

Three things make this hold together, and each was a bug first.

**The introductions are generated from the persona, not authored.** A robot's
showcase is its own performance stance, exaggerated, and the last frame of the
showcase *is* the first frame of the stance hold. Eight hand-written clips would
have drifted from the personas the first time anyone retuned a number. The
*audio* comes from the same place — the first few notes of that part's own MIDI
— so the sound and the motion cannot disagree about which instrument it is.

**Everyone else stops.** Only the conductor and the robot being introduced move;
the other seven hold `WAIT_POSE` (ears down, looking slightly down) at
**0.0000 /s** of measured motion. The first version only waited *until* its own
turn, so introduced robots carried on moving and by the end seven of them were
in motion at once — nobody could tell who was being introduced.

**The ending is a sequence of absolute poses, not offsets.** Bowing is "go to
this pose", not "add a bow to whatever you are holding". Added as offsets it
stacked on each robot's stance and blew straight through the envelope — antennas
to −225°, tilt to 37.2°. The secondary layers have to stop too: breathing
(−5 mm) and groove (−2.2 mm) on top of the stow's −44 mm reached −51 mm. A
sleeping robot does not breathe.

### Turning all the way round

The conductor bows to the audience, which means turning 180° from where it
conducts. Body yaw only reaches ±150°, so from a mechanical zero facing front
that is impossible.

The fix is placement, not software: **the conductor stands rotated 90° on the
table**, and its home is `body_yaw = −90°` (facing the ensemble). The bow is
`+90°`. The difference is exactly **180°**, and both ends sit inside the range.
Measured: −89.6° during the piece, +90.0° at the bow, **179.6° apart**, using
119° of the available 150°.

One consequence worth knowing: because the head pose is world-frame, the stow's
−21 mm x offset has to be **rotated with the body**, or the mechanism is asked
for a sideways displacement it cannot reach (`stewart_1` at −0.965 against a
−0.726 limit).

---

## Development workflow

```bash
orchestra clip record --robot r07 --out clips/dundunba/mine.clip.json --bpm 110
       # gravity-comp hand puppeteering → 100 Hz capture → simplified keyframes
orchestra clip check --clip dundunba/mine --bpm 96 --bpm 120  # gate, per tempo
orchestra clip plot  --clip dundunba/mine --bpm 120           # see the margin
orchestra clip preview --clip dundunba/mine --robot r07       # one robot
orchestra song build lamban --report                          # all nine
```

`clip check` takes the tempo because **a clip safe at 90 BPM can be unsafe at
140** — beat-relative keyframes compress in real time. Check at the fastest
tempo you will actually play.

`clip record` exists because hand-puppeteering under gravity compensation is
dramatically faster than typing angles for anything that has to look alive.

---

## Verification

```bash
pytest -q                              # 185 tests
python tools/sync_selftest.py --agents 9   # protocol, sync, abort, deadman
python tools/measure_skew.py rec.wav --bpm 110   # the measurement that counts
```

The self-test spawns nine agents in dry-run and drives the real show sequence.
It proves the logic; it does **not** prove timing — one machine, one clock,
loopback. The real number comes from a microphone, which is what
`measure_skew.py` is for. Percussion attacks read as simultaneous below roughly
10–20 ms; that is the bar.

**Measuring with the robots' own microphones.** No external mic is needed. The
wireless units' XMOS front end removes *their own* playback (echo cancellation
uses the local speaker as its reference) but leaves everyone else's alone —
verified: when one robot emitted a 1 kHz tone, the listener's 1 kHz share went
0.012 → 0.38 and its RMS rose 8×. So the conductor can record the performance it
is conducting:

```bash
python tools/make_pingtest.py                              # トーン型の測定音
orchestra song build pingtest && orchestra show deploy pingtest
orchestra show run pingtest --record r02 --skip-preflight  # 位置を変えて3-4回
orchestra show run pingtest --record r07 --skip-preflight
python tools/measure_ping.py
```

⚠ **`calibrate skew` + `make_calibration.py`（帯域制限ノイズ）は実機のマイクでは
効かない。** 2026-08-28 に7箇所から録って確認した ―― 波形相関 corr 0.010-0.020
＝無相関で、出る値はバースト間隔 1.5 秒の倍数（隣のスロットへの誤ロック）。
理由は Reachy のマイクが音声用の前段 DSP を通ること。**雑音抑圧は定常ノイズを
消す機能で、帯域制限ノイズはその標的そのもの**。PC 上では理想的な設計が、
実機のマイクを通した瞬間に成立しなくなる。

`tools/make_pingtest.py` はトーン（35 ms の減衰正弦、台ごとに別周波数）を使い、
狭帯域フィルタでその台だけを抜く。SNR x43-x3697、8回中8回検出、位置ごとの
再現性 1.2-7.2 ms まで来た。詳細は `docs/EXPERIMENTS.md`。

Two things this taught, both worth keeping:

**The song cannot be the test signal.** Correlating stems against a recording of
the piece produced no peak at all (best margin 1.07) because the music repeats —
16 bars twice, with repeated figures inside that. A periodic signal has no unique
correlation lag. `tools/make_calibration.py` therefore emits a per-robot
band-limited noise burst with a different seed each, prints their measured mutual
correlation (0.084 — orthogonal enough), and each robot fires three times so the
agreement *between* its own repeats becomes the error bar. Well-heard robots
scatter 0.1–0.9 ms; a robot hearing only reflections scatters 40 ms and is
rejected rather than reported.

**One microphone position cannot calibrate anything.** Recording the same signal
from two different robots moved r04 by 29.7 ms and r07 by 14.7 ms, while r09 and
r08 barely shifted. What a single mic measures is overwhelmingly distance and
reflection, not robot timing. Feeding those numbers into `audio_lead_ms` would
deliberately mis-time the fleet for every listener except the one that recorded —
worse than not calibrating at all. Passing two or more recordings splits the
per-robot figure into a positional part and a part that survives every vantage
point; only the latter is a candidate for `audio_lead_ms`, and so far nothing has
cleared that bar. Every unit's value is still **0.0**, on purpose.

**2026-08-28、Lamban を実機9台で通した実測**（`orchestra show run lamban --record r01`、
207.8 秒、completed 9 / dropped 0 / failed 0）:

```
  robot   start_err     frames        late
  r01      0.843 ms   20430/20431      0
  r02      1.330 ms   20430/20431      0
  r03      0.509 ms   20430/20431      0
  r04      0.472 ms   20430/20431      0
  r05      0.567 ms   20430/20431      0
  r06      0.595 ms   20430/20431      0
  r07      0.894 ms   20430/20431      0
  r08      0.642 ms   20430/20431      0
  r09      0.795 ms   20430/20431      0
  clock-sync worst-case uncertainty 1.80 ms   rtt_min 3.32-3.61 ms
```

参考元（小曲、112 BPM、10401 フレーム）と同じ水準が、**8.5倍長い曲（20431 フレーム、
96→120 BPM の加速つき）でも保たれている**。遅延フレームはゼロ。

**この曲でも「曲は測定信号にできない」が再現した。** Lamban の録音を各ステムと
相関させると、二番手との比が 1.00〜1.14 でピークが立たない ―― 循環定型の曲なので
当然で、参考元が *It's a Small World* で踏んだのと同じ罠。専用の校正音
（`tools/make_calibration.py`）に切り替えるのが正しい。

**さらにこの卓上配置では、r01 の内蔵マイクが自機と隣の1台しか拾えなかった。**
校正音の録音を直接見ると、r01 と r02 のバーストだけが録れていて（録音 3.6-5.2 秒、
振幅 0.95）、r03-r09 の 40 秒ぶんはほぼ床ノイズに埋もれていた。相関値 0.007-0.017。
外部マイクをアンサンブルの正面に置いて録るのが要る。`orchestra calibrate skew` は
任意の WAV を受け付けるので、録音機さえあれば手順は変わらない。

`audio_lead_ms` が全機 **0.0 のままなのは意図的**（上の「単一マイクでは何も較正できない」
を参照）。値を入れると、録音した1点以外のすべての聴取位置で意図的にずらすことになる。

### Being wrong on purpose is cheaper than being wrong by accident

Several conclusions in `docs/EXPERIMENTS.md` are corrections of earlier
measurements that looked clean and were not: an autocorrelation that found a
loop length instead of a pitch, a "silent" baseline recorded while the other
robots were audible, a 1-second clock window reporting ±852 ppm of pure noise, a
duty-cycle metric that watched the head while the part under test moved only its
ears, a melody confidently recalled and simply wrong. Each is written down with
the wrong number next to the right one, because the failure mode of this
hardware is *silence* — the daemon swallows an IK failure and holds the last
pose — so a measurement that quietly lies is the most expensive thing here.

The sharpest example is the conductor. Its downbeat gesture tore open by 45% in
a single frame at every bar line, and the safety gate said PASS for months —
because at 112 BPM a beat is 53.57 frames, so the tear landed at a different
sub-frame offset each bar and got averaged under the ceiling. Changing the tempo
to 80 BPM, where a beat is exactly 75 frames, made it 6.02 rad/s against a 4.00
limit. A limit that only holds while the tempo does not divide evenly into the
frame rate is not a limit; `tests/test_conductor.py` now checks both kinds of
tempo deliberately.

---

## Scaling to 10+ robots

Nothing in the code knows a robot count. Add an entry to `robots.yaml` and a line
to the song's `cast`. More parts than robots reports which are unassigned; more
robots than parts gives the spares an idle program so they still breathe.

Per-robot traffic during a show is ~1 msg/s of heartbeat, so Wi-Fi scales well
past ten.

---

## Known state of the fleet

- All nine reachable, firmware **1.9.0**, steady-state RTT ~6–7 ms.
- **r04 (192.168.3.133)** was in daemon state `error` with its motor backend
  down. `orchestra fleet recover --robot r04` restarts it over SSH.
- **r02 (192.168.3.120)** briefly reported as a **Lite** variant. That was a
  side effect of a missing camera, not the variant: with no `/dev/video0` the
  daemon downgrades its own report. After the camera was fitted it reports
  `imx708` / `wireless` like the rest.
- **r04** had both antenna motors missing (`Missing motors: ["right_antenna",
  "left_antenna"]`), which survived a reboot — a real hardware fault, since
  repaired.
- The daemon binds `0.0.0.0` with CORS `*` and **no authentication**. Anyone on
  the SSID can command motion. Fine on an isolated network; worth knowing.
- First packet after idle spikes to 17–90 ms (Wi-Fi power-save). The 1 Hz
  heartbeat exists to keep that out of the START path.

See [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) for what still needs measuring
on hardware, and which parts of the design each answer would change.

---

## Note on the music

**Lamban** は伝統曲で、著作権者のいる作品ではない。演奏や配信に法的な障害は無い。
一方で「伝統である」ことは「何を鳴らしても伝統になる」ことを意味しないので、
**どこまでが出典のある採譜で、どこからが私の再構成か**を全部書き残してある。
`tools/make_lamban.py` の各定数に `[DOC]` か `[RECON]` が付いている。

### [DOC] — 出典のある採譜

| 何 | 中身 | 出典 |
|---|---|---|
| サンバンの太鼓 | 16分 1,4,7,9,12,15 = **3+3+2 \| 3+3+2**。この曲の署名 | Nas p.65（Ponda O'Bryan）。Griot 項 p.98 にも同形 |
| サンバンの鈴 | 16分 1,3,4,6,7,9,11,12,14,15 | 同上 |
| ドゥンドゥンバ | 太鼓 4,7,13,15 / 鈴 1,3,4,6,7,9,11,13,15 | 同上 |
| ケンケニの鈴 | 8分の等間隔 ―― **カリニャンに移した** | 同上（楽器の割当は私の判断） |
| ジェンベ伴奏 | 8分格子 `B . O O . . S S` | Malcolm Smith / Nansady Keita 伝、110 BPM |
| 合図(call) | `T T S S \| T S T T \| S . . .` | Nas p.65（Ponda O'Bryan） |
| ブレイク | `F . T T \| . T . T \| T . S S \| S . . .` | Nas p.98（Griot 項） |
| échauffement | djembe `. S S S` の連打 / dununba ほぼ連打 | Nas p.98（Griot 項） |
| 終止 | dununba `x D D D \| . x D . \| x . x . \| D D . D` | 同上 |
| バラフォンの**リズム** | 三連分割、2小節周期、拍2と拍4の1つ目と3つ目に打 | Williams 2006 Fig.7（Siriman Kouyate Acc.1）を実際に読んだもの |
| バラフォンの音律 | 等間隔七音、1ステップ 171.43 cent | Williams 2006 ch.6、Sosso-Bala（ユネスコ無形文化遺産） |
| コラの調弦 | **Sauta** F 185 G 220 A 200 B 95 C 185 D 220 E 95 f | kora-music.com「Scales and Tunings」。Lamban への帰属は Amadu Jobarteh |
| 加速構成 | 「遅く始まり速くなる」／サンバンが加熱部を始め、一撃で終える | Nas p.64, p.79 |

**拍子について。** Lamban は **2分割（4/4）**。12/8 で書かれた採譜も存在するが、
資料を突き合わせるとそれは同じ音楽をスウィングして読んだもので、別の地方版という
根拠は見つからなかった。Charry は 4/4 を所与として「なぜジェンベ奏者は 4/4 の
伴奏形を選んだのか（三連のノリで弾かれることもあるのに）」と問うている。
Nas は拍子を変える代わりに「2番目と10番目のパルスがわずかに遅れる」という
微視的な指示を書いている。

そして **Williams のバラフォン採譜は 4/4 の中が三連符で埋まっている**。
つまり同じ小節の中で dunun と djembe が4分割、balafon が3分割を刻む ――
これが「二分割と三分割は矛盾ではなく相補である」という Williams の主張の実体で、
この曲の一番面白いところ。ここではそのまま実装している（balafon は 1/3 拍刻み、
それ以外は 1/4 拍刻み）。

### [RECON] — 出典のある制約を満たす再構成

- **バラフォンの音そのもの。** Williams の図（Fig.6-9、3人の名のある奏者の伴奏形
  5種）は走査画像で、音符の**高さ**までは確度をもって読めなかった。自動採譜も
  試したが、中途半端な音符検出は「もっともらしく間違う」ので採らなかった。
  本文で明示されている制約 ―― 2小節周期・片手が最低音の反復・拍2と拍4は同じ音の
  反復・和声は3拍目で変わる・下行する走句が即興の語彙 ―― だけを満たしてある。
  **ここを埋めたい人へ**: Williams 2006 の物理 PDF 123-126ページを高解像度で
  レンダリングして読むのが最短。
- **コラの kumbengo とンゴニ。** Lamban の kumbengo の採譜は見つからなかった。
  出典があるのは奏法の原理だけ（親指が低音で周期を刻み、人差し指が旋律を運ぶ／
  kumbengo は「すべての変奏が戻ってくる点」／birimintingo は下行する走句で
  フレーズの切れ目に置き必ず kumbengo に戻る）。低音をサンバンの 3+3+2 に
  合わせてある。
- **タマ。** Lamban のタマ譜は無い。役割だけが文献にある（ジェリの三大楽器の
  1つ、紐を締めて音程を連続的に変える）。周期性の最も低いパートとして
  2小節周期の終わりに短い応答を置いた。
- **区分と小節数。** 仕組みは全部出典があるが、長さの割り振りは私。

### 知りつつ落としたもの

- **ケンケニと2台目のサンバン。** Nansady Keita 伝では Lamban は例外的に
  **dunun を4台**使う（kenkeni + サンバン2台 + dundunba）。「3人しかいなかったら
  どうするか」と訊かれて彼は「**その時は Lamban を演らない**」と答えている。
  8台に8種を割り当てる制約からこれを 2台に減らしたのは、承知の上の縮約。
  ケンケニの鈴（唯一の等間隔の層）はカリニャンが引き継いでいる。
- **スウィング（2番目と10番目のパルスの遅れ）。** 実装していない。遅らせると
  次の打点との間隔が 110 ms を割って安全ゲートに落ちるため。8分格子の
  ジェンベ伴奏を採ったのはこの制約も理由。

### 出典

- Paul Nas / WAP Pages, *African Rhythms*（PDF）pp.64-71（Lamban）, pp.97-100（Griot）, 記譜法 pp.6-7
  https://djembefoley.ca/wp-content/uploads/2012/03/african-rhythms.pdf
- Joe L. Williams Jr., *Transmitting the Mande Balafon*, PhD diss., Univ. of Maryland 2006, pp.113-120
  https://api.drum.lib.umd.edu/server/api/core/bitstreams/fc947982-218f-4e87-ab9e-f6f81e71be05/content
- Malcolm Smith, *Lamban*, Vitae Drum Circle — https://www.thesmith.org.uk/music/projects/vitae/rhythms/lamban.html
- Tontinkan, *Lamban* — https://tontinkan.net/wp/lamban/
- Eric Charry 編 mandebala.net — http://www.mandebala.net/references/lamban.php
- kora-music.com, *Scales and Tunings* — https://www.kora-music.com/e/skalen.htm
- D. Racanelli, *Formulaic Variation Procedures in Mande Griot (Jeli) Guitar Playing*, AAWM 2.1 (2012)
- Eric Charry, *Mande Music*（未参照。Lamban は pp.150-153、バラフォン伴奏形は pp.184-187）
