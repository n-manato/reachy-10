# Hardware experiments

What is measured, what is still assumed, and what each answer would change.

The software is complete and tested. Some of it stands on assumptions that only
hardware can settle. This file tracks them honestly: **an assumption written down
is a risk; an assumption not written down is a bug waiting to be blamed on
something else.**

Status legend: ✅ measured · ⚠ assumed, needs measuring · ⬜ not yet attempted

---

## ✅ 音量を変える3経路のうち、演奏中に使えるのは1つだけ（2026-08-10）

演奏中に音量を変えたい。「どれでもいいから設定できればいい」と思って調べたら、
3経路のうち2つは**演奏を壊す**ものだった。

| 経路 | テスト音 | セッション | 判定 |
|---|---|---|---|
| PC → `POST /api/volume/set` | **鳴る** | 無関係 | ✗ |
| PC → 新しい `ws://ip:8000/ws/sdk` | 鳴らない | **新規** | ✗ |
| **エージェント → 既存の SDK 接続** | 鳴らない | 既存を再利用 | ✓ |

**1つめが罠。** デーモンのこのルートは docstring からして "Set the output
volume level **and play a test sound**" で、呼ぶたびに `impatient1.wav` を
鳴らす（`daemon/app/routers/volume.py:104-110`）。抑制するパラメータは無い。
演奏中に叩けば、その台だけ関係ない音を出す。

コード上には「デバイスが埋まっていれば鳴らないかもしれない」逃げ道もある
（`-9985` を捕まえて `status: "busy"` を返す）が、**`set_volume` はその例外を
握り潰す**ので、鳴るか鳴らないかはファームとタイミング次第になる。演奏の最中に
「たぶん鳴らない」に賭ける理由が無い。

2つめは音は出ないが、演奏を運んでいるセッションの隣に別セッションを作ることに
なる。ロックの取り合いで演奏が落ちる可能性と、音量が変えられる利便性は釣り合わない。

3つめが答え。エージェントは arm の時点で `ReachyMini` を持っていて、その
`client`（`WSClient`）から `SetVolumeCmd` を送れる。REST と**同じ
`VolumeControl` シングルトン**に届き（`daemon/backend/abstract.py:1477-1512`）、
音は鳴らず、GStreamer にも触らない。

**教訓**: 「設定できるか」ではなく「**設定する副作用は何か**」を先に読む。
3つとも「音量を設定する API」で、機能としては同じに見えた。

### ついでに見つかった実バグ

`DaemonClient.set_volume` は `POST /api/volume/` を叩いていた。**そんなルートは
無い**（あるのは `/current` `/set` `/test-sound`）。どこからも呼ばれていない
死んだコードだったので、404 が誰の目にも触れていなかった。正しいルートに直し、
テスト音が鳴る副作用を docstring に書いた。

---

## ✅ 矢印キーは「コンソードがあるか」で決まる（2026-08-10）

`s` で停止する機能で二度踏んだ問題の続き。`msvcrt` はコンソールが無くても
import でき、`kbhit()` は例外も出さずに 0 を返し続ける ―― キーは stdin の
パイプへ行くので永遠に届かない。前回はコンソールの実在を
`GetConsoleMode(GetStdHandle(-10))` で確かめるように直した。

矢印はさらに条件が厳しい。**行入力の端末では原理的に届かない。** Enter を
押すまでバイトが来ず、しかも端末の行編集がカーソル移動として食ってしまう。
`s` は Enter 付きで代用できたが、矢印には代用が無い。

拾える端末での解釈:

| | Windows コンソール | ANSI (ConPTY / POSIX) |
|---|---|---|
| ↑ | `\xe0` `H` | `\x1b` `[` `A` |
| ↓ | `\xe0` `P` | `\x1b` `[` `B` |
| → | `\xe0` `M` | `\x1b` `[` `C` |
| ← | `\xe0` `K` | `\x1b` `[` `D` |

**左右が逆順**（Windows は K/M、ANSI は D/C）なので取り違えやすい。テストで
固定してある。

もともとのコードは2バイト目を**読んだうえで捨てていた**。読み捨て自体は
正しい（読まないと次の1文字と誤認する）が、そこに矢印が入っていた。

**148秒待たずに判定できるようにした** ―― `orchestra show keys` はロボットを
一切使わず、この端末が何を渡してくるかを10秒で答える。同じ失敗を三度踏まない
ための道具で、機能そのものより先に作った。

---

## ✅ 見分けが付かなかったのは、可動域を使っていなかったから（2026-08-09）

音・同期・安全ゲートは実機で成立していたのに、README が最初に掲げた要件
――「音を聞かずに、部屋の向こうから誰が何を演奏しているか分かること」――
だけが満たせていなかった。原因を推測せずに、全クリップのピークを測った。

| 軸 | 運用上限 | 全クリップの最大 | 使用率 |
|---|---|---|---|
| head z | ±22 mm | 5.0 mm | 23 % |
| head y | ±25 mm | 5.0 mm | 20 % |
| tilt cone | 25° | 7.0° | 28 % |
| **body_yaw** | **150°** | **2.5°** | **1.7 %** |
| antenna | 150° | 30° | 20 % |

**可動域の 98 % を使っていなかった。** 安全側に倒しすぎたのではなく、識別情報を
置く**場所**を間違えていた。効いている制約は位置ではなく動力学（関節速度
4.0 rad/s、10 秒窓の実効値ヘッド 2.5 / アンテナ 3.0 rad/s）で、速度 = 振幅 ÷
時間である以上、

> **速い動きが小さいのは物理的に避けられない。だが遅い動きと静止した構えは
> 桁違いに大きくできる。**

旧設計は識別情報を「1 音ごとの小さな上下」に置いていた。そこは物理的に大きく
できない唯一の場所だった。だから遠目には 9 台が同じに見えた。

対策は、識別情報を大きくできる場所へ移すこと。`orchestra/motion/persona.py` の
3 層 ―― 常時の**構え**（速度ゼロ）、**拍に乗った胴体の回転**（遅いので大きく
できる）、**1 音ごとの打点**（従来どおり小さい）。ビルド後の実測ピーク:

```
              旧      新
r05 marimba   2.5°   26.9°     r03 counter   2.5°   16.9°
r06 accent    2.5°   19.0°     r09 colour    2.5°   15.6°
r07 ride      2.5°   14.7°     r02 lead      2.5°   10.5°
r04 glocken   2.5°    8.2°     r08 pulse     2.5°    8.5°
```

演奏者で最大 **10.8 倍**。値がばらついているのが要点で、揃っていたら元の問題
（全員同じに見える）が戻る。指揮者は別枠で 119.4°。

**教訓**: 「安全のために小さくしてある」と「そもそも大きくしていない」は違う。
測るまで区別が付かない。

---

## ✅ 静止した構えはタダではない ―― ヤコビアン（2026-08-09、自分の仮説が外れた記録）

設計時にこう書いた ――「一定オフセットは速度・加速度・実効値に何も足さない。
だから静止姿勢はいくら大きくても無料」。**微分の話としては正しく、機構の話
としては間違っていた。**

構えを入れる前後で `joint_vel_peak` を比較すると:

```
r04 glockenspiel   3.078 -> 3.763   (+22 %、上限 4.0)
r02 celesta        3.508 -> 3.801   (+8 %)
r05 marimba        2.710 -> 2.747   (+1 %)
```

構え自体の速度はたしかにゼロ。だが構えは Stewart 機構の**動作点**を動かし、
そこでのヤコビアンが変わる。同じ head 速度に要る関節速度が変わるので、**上に
乗っている 1 音ごとの動きのコストが変わる**。増分が +1 % から +22 % までばらつく
のはそのため ―― 姿勢によって機構の効率が違う。

余裕がいちばん薄いのは r04（グロッケン、上限まで 0.24 rad/s）。ここが構えを
これ以上大きくできない実際の壁で、位置のボックスではない。

**教訓**: 直列機構なら本当に無料だった。並列機構では「一定オフセット」が
下流の全部の係数を書き換える。数式ではなく、ゲートを通して測る。

---

## ✅ 並進ボックスの z が上下逆に狭かった（2026-08-09、実バグ）

エンディングで顔をしまうのに z −44 mm が要る。`config/fleet.yaml` の
`head_translation_m.z` は `[-0.022, 0.022]` だったので、まず**箱が正しいのかを
測った**（N-5 が未実施のままだったため）。IK と関節範囲検査を各方向へ掃引:

| 目標 z | 最も厳しい関節 | 結果 |
|---|---|---|
| **+22 mm** | `stewart_2` | **2.92° 超過 ―― 箱を通るのに関節検査が落ちる** |
| −22 mm | — | 余裕あり |
| **−44 mm** | `stewart_2` | **+12.80° 余裕。到達可能** |

**上は広すぎ、下は倍の余地があった。** 箱と関節検査が矛盾していたのに露見して
いなかったのは、誰も +22 mm まで使っていなかったから（上の表のとおり最大
5.0 mm）。`[-0.046, 0.021]` に直した。上を締めるのは安全側、下を開けるのは実測
に基づく。SDK のスリープ姿勢が演奏プログラムの中で扱えるようになった。

---

## ✅ `attention` レイヤの本体が死んでいた（2026-08-09、実バグ）

`layers.attention` は「フレーズの頭で指揮者のほうを向く」層で、有効になっていて
テストも通っていた。実際には**首が一度も動いていなかった。**

```python
out[:, CH_YAW] = amount * np.sin(ctx.conductor_azimuth_rad) * profile
```

`conductor_azimuth_rad` は `_secondary_for` の既定値 0.0 のままで、呼び出し側
（`build.py:389`）は一度も渡していない。`sin(0) = 0` なので yaw 成分は恒等的に
ゼロ。生き残っていたのは `cos(0)` 側の **4 mm の前傾だけ**だった。

`config/robots.yaml` に方位のデータが無く、実際の卓上配置も測っていない以上、
正しい値を入れようがない。**予算表には 8° として計上されていた**ので、そこは
実質空いている ―― 構えの rel_yaw 予算はその 8° を含んだまま組んである
（保守側の誤り）。

**教訓**: 既定値 0 のパラメータは、渡し忘れても例外を出さずに層を無音にする。
「有効になっている」と「効いている」は別で、テストが後者を見ていなかった。

---

## ✅ 採譜そのものがフェードアウトで終わっていた（2026-08-09）

「最後が途中で終わったように聞こえる」という指摘に対し、モーション側を疑う前に
MIDI の末尾のベロシティを出した。主旋律、最後の数小節:

```
94 -> 86 -> 72 -> 56 -> 11 -> 8      そのあと 34 の2音（次の繰り返しの頭）
```

**終止が無い。** 遊園地のループ音源なので、終わるのではなく小さくなって切れる。
実装の問題ではなく素材の問題だった（ユーザの見立て「データをそもそも作って
ない」が当たっていた）。

`song.yaml` の `finale:` で 3 手当て ―― `unfade_from_bar` でベロシティを戻し、
`trim_after_bar` で余分な 2 音を落とし、`at_bar` で主音の和音を伸ばした音の下に
敷く。和音は**ステムではなくタイムラインに**足すので、音とモーションが同じ
1 か所から生える。

その和音でバグを 1 つ踏んだ: 既に音のある拍に重ねると、加算合成（クランプ無し）
なので振幅が足し算になり、マリンバが **5.54 rad/s**（上限 4.0）に飛んだ。
±0.26 拍以内に既存音があれば**足さずに置き換える**ようにした。

---

## ✅ 公式ダンスライブラリを読んだ（2026-08-09）

Pollen Robotics の `reachy_mini_dances` を読んで、自分の設計と突き合わせた。
借りたのは 1 つだけ:

- **胴体の回転を主役にしている。** 頭の並進ではなく yaw で振り付けを作っている。
  上の使用率の表と同じ結論に、向こうは最初から立っていた。
- **ロールを yaw の 1/4 周期ずらして結合している。** 回転にロールを 90° 遅れで
  足すと、機械的なスイベルではなく「乗っている」動きに見える。`Sweep` の
  `beat_roll_deg` はこれ。
- 借りなかったもの: 振り付けが実時間の正弦で書かれていて拍に乗っていない。
  こちらは 9 台が同じタイムラインで合奏するので、拍で書かないと揃わない。

---

## ✅ 指揮者の 180° 反転は、ソフトではなく配置で解いた（2026-08-09）

最後に指揮者が客席へ礼をする。指揮の向きから 180° 反転する必要があるが、
body_yaw は ±150° なので、機械原点が正面だと物理的に届かない。

**卓上で指揮者を 90° 回して置き、ホームを `body_yaw = −90°`（客席ではなく
合奏を向く）に定義した。** 礼は `+90°`。差はちょうど 180° で、両端とも範囲内。

実測 ―― 演奏中 **−89.6°**、礼で **+90.0°**、**179.6° 離れている**。使用量は
150° 中 119°。

副作用を 1 つ踏んだ: 頭の姿勢は world 座標なので、就寝姿勢の x −21 mm を体と
一緒に回さないと、機構に横方向の変位を要求することになる（`stewart_1` が
−0.965、上限 −0.726）。姿勢の並進を回転角で回してから渡すようにした。

---

## ✅ オープニングとエンディングで踏んだ罠（2026-08-09）

どれも安全ゲートが実機の前に止めた。ゲートが無ければ全部そのまま出ていた。

| 症状 | 数値 | 原因 |
|---|---|---|
| 礼で腕が飛ぶ | アンテナ −225°、tilt 37.2° | 礼を**差分**で足していた。各台の構えの上に積まれる。絶対姿勢に変えた |
| 就寝が箱を割る | z −51.0 mm | 副次レイヤ（呼吸 −5 mm、グルーヴ −2.2 mm）が就寝中も動いていた。**寝ているロボットは呼吸しない** |
| 全員が構えの 2 倍 | ちょうど 2.000 倍 | 名乗りが構えを**再度**出力し、`_ease` が 1 にクランプしたまま戻らない。名乗りは誇張分だけ出す |
| アンテナが 1 フレームで 209 rad/s | 上限 4.0 の 52 倍 | ブレンド係数を重みの比で作っていて 0→1 に跳ねた。時間ベースに変えた |
| 合図で指揮者が関節上限 | `stewart_1` 1.551（上限 1.285） | 合図の誇張 1.7 倍が過大。1.25 に |

**教訓**: 5 件のうち 4 件は「ある文脈で正しいものを、別の文脈へそのまま持ち込んだ」
種類の誤り（差分と絶対、演奏中と就寝中、名乗りと本番、重み比と時間比）。実機を
1 度も傷めずに全部捕まえられたのは、ゲートが**曲の実テンポで通しを検査する**
から。

---

## ✅ 歌声 ―― 数値が全部緑でも駄目なことがある（2026-08-07、方式やり直し）

歌を**音節ごとの SAPI 合成 + LPC 再合成**で作った。検算はすべて通っていた ――
音節と音符の対応 39/39・26/26、音程誤差 2〜8 cent、長さは誤差ゼロ、
フォルマント比 実測 ×1.18〜1.19（狙い 1.20）。それでも**聞くに堪えなかった**。

この失敗の教訓は「測れるものを全部測っても、測っていない軸で落ちる」こと。
言葉として聞き取れるかは、ここでは測れない。

原因は4つあって、**どれも単独で致命的**だった:

| 原因 | なぜ効くか |
|---|---|
| 音節を**単独で**合成して並べた | 渡り（coarticulation）が無い。人はそう喋らないので、音程が完璧でも言語として成立しない |
| Zira は**大人**の声（f0 中央 190Hz） | 旋律は 294〜784Hz。1オクターブ以上の移動が要り、子供らしさは ×1.20 のフォルマント歪みで偽装していた |
| 22次の全極フィルタ | 包絡が粗い |
| パルス列音源 + 有声/無声の**2値**判定 | ボコーダ臭。摩擦音が壊れる |

**4つとも外す**方式に置き換えた。個別に調整しても届かないと判断した。

| | 旧 | 新 |
|---|---|---|
| 単位 | 音節 | **行まるごと** |
| 声 | SAPI Zira（大人 190Hz） | `en-US-AnaNeural` / `en-GB-MaisieNeural`（**子供**、実測 f0 中央 295 / 297Hz） |
| 分解 | LPC 22次 | **WORLD** CheapTrick |
| 音源 | パルス列、有声/無声2値 | WORLD D4C（帯域ごとの**非周期性の比率**） |
| フォルマント | ×1.20 で子供に偽装 | **等倍**。素材が既に子供なので触らない |

子供の声の f0 中央 295Hz は旋律域（294〜784Hz）と重なる。**移動量が
1オクターブから数半音に落ちる**のが最大の効果で、旧方式の主因がこれで消える。

新方式で踏んだ実バグ3つ（いずれも黙って壊れる型）:

| バグ | 症状 |
|---|---|
| 休符（0Hz）を跨いで音程を均した | 音符の端が引きずられ、f0 下限が **27Hz** まで落ちた |
| 包絡がフェード端でちょうど 0 | WORLD が合成中に対数を取り `-inf` → **NaN**。歌ステム全体が無音になるが、エラーは出ない |
| ―― | 以後、非有限値は数えて報告する。黙って `nan_to_num` しない |

実測（`voice_lead`、先頭6行）: 音程誤差 中央 −0.1 cent / |誤差| 中央 4.9 cent、
行の長さ誤差 0.0 ms、f0 286〜797Hz、音節132/音符132。

**それでも「聞き取れるか」はここでは判定していない。**
`python tools/preview_song.py songs/small_world --only voice_lead voice_harmony`

---

## ✅ 指揮者ロボットのマイクで測れる（2026-08-07）

外付けマイクを立てなくても、**ロボット自身のマイクで演奏を録れる**。

実機で確かめたこと:

| 問い | 結果 |
|---|---|
| wireless CM4 で録音できるか | できる。16 kHz / 2ch、64 ms 刻みで届く |
| 再生しながら録れるか | 録れる。同じパイプラインの別の枝 |
| XMOS の AEC は他機の音も消すか | **消さない**。r02 が 1kHz を鳴らした瞬間、r01 のマイクで 1kHz 成分 0.012 → 0.38、RMS 8倍 |
| 自機の音は | 消える。録音した本人だけは毎回 reject される（想定どおり） |

AEC は自機の再生を参照信号にするので、他機には効かない。これで
`show run --record r01` が成立する。

---

## ⚠ 曲では測れない ―― 反復の罠を3回踏んだ（2026-08-07）

**1回目**: シェイカーの動きの周期を自己相関で測ろうとして、ループ長を拾った。

**2回目**: 演奏の録音と各台のステムを相関させたら、ピーク比が 1.00〜1.07 で
どこにも山が立たなかった。*It's a Small World* は 16小節がそのまま2回繰り返され、
その中も同じ音型でできている。**周期的な信号は相関で位置を特定できない。**

**3回目**: だから測定用にチャープを作った ―― のに、全機に**同じ**チャープを
1.5 秒の等間隔で鳴らさせた。信号全体が周期 1.5 秒で周期的になり、相関が隣の台の
チャープに合った（lag が 1.5 秒違いの2群に割れた）。「繰り返すものは測れない」と
書いた直後に、繰り返しで測定信号を作っていた。

**決着**: 台ごとに seed の違う帯域制限ノイズバースト（0.25秒 / 300〜6000 Hz）。
生成時に相互相関を実測して表示する（実測 0.084 ―― 十分に直交）。
解析は生波形の整合フィルタ。同じ録音で **ピーク比 1.08（包絡）対 6.24（整合フィルタ）**。

さらに **1発で測ってはいけない**。同じ台に3回鳴らさせ、その一致度をそのまま
誤差として出す。一致しない台は捨てる。r01 が自分の音を測ったときのばらつきは
39.9 ms、よく聞こえた台は 0.1〜0.9 ms だった。この差が、値を採用してよいかの
判定そのものになる。

---

## ✅ 音の到着差は**ほぼ全部が空気の話**だった（2026-08-07）

同じ校正音を、r01 のマイクと r06 のマイクで別々に録って突き合わせた。

| robot | r01が聞いた | r06が聞いた | 最小 | 変動 |
|---|---|---|---|---|
| r09 | 0.0 ms | 1.2 ms | 0.0 | 1.2 |
| r08 | 3.1 ms | 5.5 ms | 3.1 | 2.4 |
| r07 | 23.5 ms | 8.8 ms | 8.8 | **14.7** |
| r03 | 30.1 ms | 38.0 ms | 30.1 | 7.9 |
| r04 | **36.1 ms** | **6.3 ms** | 6.3 | **29.7** |

**r04 は聞く位置を変えただけで 30 ms 動く。** 機体側のタイミングが原因なら
どの地点から聴いても遅いはずで、そうなっていない。見えているのは距離と反射で、
ロボットの時計ではない。

**だからこの数字を `audio_lead_ms` に入れてはいけない。** 入れると、録音した
1点に合わせて**他の全員に対してわざと狂わせる**ことになる。1地点の測定で
補正値を決めるのは、測定していないより悪い。

`orchestra calibrate skew <song> --recording A.wav B.wav` は2地点以上を渡すと
この分離を自動で出す。「変動」が小さく「最小」が大きい台だけが、機体側の
遅れを疑ってよい候補になる。

**電気系の同期そのものは別経路で確認済み** ―― start_err 0.3〜1.5 ms、
クロック同期の不確かさ 1.95 ms、9台とも frames 10400/10401 で late tick 0。

---

## ✅ 実譜面での通し演奏 — 9/9完走・104秒（2026-08-07）

記憶で書いた仮の旋律を捨て、実際の採譜（32小節・Verse+Chorus・80 BPM・
652音／6トラック）に差し替えた後の実測。

| 指標 | 実測 |
|---|---|
| 完走 | 9/9（脱落0・失敗0） |
| 長さ | 104.0 秒 |
| モーション start_err | 0.365 〜 1.002 ms |
| フレーム | 10400/10401（全機同一）、late tick 0 |
| クロック同期の不確かさ | 最悪 1.95 ms |
| 音声 | 1041チャンク / 1,664,001フレーム、push_errors 0、全機 complete |
| クロック比のばらつき | 237 ppm → アンカー時点で 0.64 ms |

24秒版（下記）から4倍の長さになっても、同期・脱落・late tick のいずれも
悪化していない。

---

## ✅ 指揮者に小節線の不連続があった（2026-08-07、実バグ）

**症状:** 80 BPM で指揮者だけ検証に落ちる。stewart_1 が 6.02 rad/s（上限4.00）、
しかも小節線の直前に規則的に。112 BPM では通っていた。

**原因:** 拍頭を1.45倍にする `emphasis` を、**位置を補間したあとに**掛けていた。
位置そのものは拍をまたいで連続している（落下カーブの終点が次の拍の始点と同じ）
のに、そこへ拍ごとの倍率を後から掛けると、隣り合う2フレームで振幅が
1.0 → 1.45 に跳ぶ。

**なぜ長く見逃されたか** ―― ここが肝心で、**テンポとフレームレートの噛み合わせで
症状が消えていた**。100 fps に対して

- 112 BPM は 1拍 = 53.57 フレーム。拍の境界が毎回違う小数位置に落ちるので、
  段差が複数フレームに散らされて上限の下に収まっていた
- 80 BPM は 1拍 = ちょうど 75 フレーム。境界が必ずフレームに乗るので、
  段差がそのまま1フレームの跳ねとして出る

つまり **「テンポがフレームレートを割り切らないとき**だけ**成り立っていた上限」**
だった。それは上限ではない。

**修正:** 倍率を制御点の側に掛けてから補間する。こうすると落下カーブの終点が
次の拍の始点と同じ値のままなので、合成後も連続する。

修正前後（80 BPM、関節速度ピーク）: **6.02 → 2.07 rad/s**。

修正後、60〜150 BPM で速度ピークがテンポに対して単調になった（1.56 → 3.83 rad/s）。
`tests/test_conductor.py` に、フレーム整数倍のテンポ（60/80/100/120/150）と
そうでないテンポの両方、および段差そのものの検査を入れてある。指揮者は
手続き生成でクリップと違い人の目で見直されないので、ここが唯一の歯止め。

---

## ✅ 楽器なし編成での通し演奏 — 9/9完走（2026-08-07）

物理楽器を廃止し、全楽器音を各機のスピーカーから出す構成での実測。
`orchestra show run small_world`、指揮者1 + 歌2 + 楽器6。

| 指標 | 実測 |
|---|---|
| 完走 | 9/9（脱落0・失敗0） |
| モーション start_err | 0.386 〜 0.978 ms |
| フレーム | 2429/2430（全機同一） |
| late tick | 0（worst 0.0 ms） |
| クロック同期の不確かさ | 最悪 2.07 ms |
| 音声 | 243チャンク / 388,573フレーム、push_errors 0、全機 complete |

音声はPCMの明示PTSストリーミング。**最初のバッファにだけPTSを打ち、
以降はバイト数で連続配置される**ので、呼び出しジッタは曲の頭の1回にしか
効かない。

---

## ✅ パイプラインクロック比 — 2点法は使えない（2026-08-07、自分の測定ミスの記録）

**測り直しの結果:** 台間のドリフト幅 **−32 〜 +68 ppm（幅100 ppm）**。
スケジュール余裕 2.7 秒ぶんに換算して **台間ずれ 0.27 ms**。

最初は2点の差で比を出していて、9台で **−56 〜 +1112 ppm（幅1168 ppm）** という
値が出た。これは水晶のばらつきではなく **ほぼ全部が量子化ノイズ**だった。

GStreamer の `AudioClock` は ALSA の周期（ここでは 16 ms）でしか進まない
階段状の時計で、1回の読みは最大 16 ms 古い。両端で独立にずれ得るので、
2.5 秒窓の2点差は原理的に ±6400 ppm まで暴れる。窓全体に50点取って直線を
当てはめると踏み面が平均化されて消え、同じ 2.5 秒で上記の値になった。
残差は約 120 ppm で、これが量子化ノイズの実体（`drift_resid_ppm` として
毎回記録している）。

**併せて訂正:** 1168 ppm から「24.3秒の曲で28 msずれる」と書いたのは誤り。
この比が効くのは monotonic → パイプラインクロックの換算、つまり
**スケジュール余裕ぶんだけ**で、曲の長さには比例しない。演奏開始後の
バッファはバイト数で 16 kHz ちょうどに連続配置されるため。曲中に効くのは
ALSA ハードウェア側のドリフトで、それは別の量（未測定）。

---

## ✅ 唯一の発熱保護がアンテナを見ていなかった（2026-08-07、実バグ）

duty cycle 検査（このファームは温度・電流・負荷を一切報告しない ―― N-6 ―― ので
構造的な唯一の発熱保護）が、**IK で解いたヘッド7関節しか見ていなかった**。
アンテナは別経路で範囲と速度だけ検査され、持続的な仕事量は誰も見ていなかった。

そのため **アンテナ主体で設計したパート（鈴 / r09）だけが、保護対象から丸ごと
外れていた**。しかもアンテナは xc330（±0.8 N.m）で、sts3215（±3.23 N.m）の
1/4のトルクしかない、この機体で最も壊しやすい軸。最も弱いモーターを最も
酷使するパートが、最も無防備だったことになる。

実測（`build/small_world`、112 BPM、10秒窓の最悪値、rad/s）:

| | r01 | r08 | r03 | r07 | r02 | r06 | r05 | r04 | r09 |
|---|---|---|---|---|---|---|---|---|---|
| アンテナ | 0.23 | 0.49 | 0.83 | 1.49 | 1.54 | 1.79 | 1.83 | 2.19 | **3.64** |

上限は `antenna_duty_max_rms: 3.0` とした。**これは温度実測ではない**（測れない）。
根拠は「ベンダ自身が連続稼働させている量」で、SDK 由来の idle breather が
0.26 rad @ 0.5 Hz、実効値 0.82 rad/s。3.0 はその約3.7倍まで許し、ピーク上限
12 rad/s の半分近くを延々と出し続ける状態は拒否する、という設定。
r09 は `song fit` がアンテナ振幅のみ x0.725 に縮めて通した（頭の演技は x1.000 の
まま ―― 縮めると「誰が何を担当しているか見て分かる」設計が壊れるため）。

---

## ✅ N-1 — SDK / firmware version parity

**Result:** `reachy-mini==1.9.0` installs on the PC and matches the fleet's
firmware exactly. IK verified working: `ik(eye4, 0.0)` → `[0, ±0.6265 …]`.

The on-robot agent runs against the robot's *own* SDK regardless, so this matters
less than it would in a PC-streaming design — but parity means the preview path
and the show path use identical maths.

---

## ✅ N-6 — What `/api/motors/status` actually reports

**Result, and it is a significant one:** on daemon 1.9.0 the endpoint returns
exactly

```json
{"mode": "disabled"}
```

No temperature. No current. No load. Nothing. **A runtime thermal watchdog is
impossible on this firmware.**

**What this changed in the design:** motor protection became *structural* instead
of reactive — compile-time duty-cycle limiting
(`safety.strike_duty_max_hz`, currently 4 strikes/s averaged over 10 s) plus the
velocity and dwell checks. `_extract_temperatures` is retained because it costs
nothing and would light up on a firmware that adds the fields, but nothing is
designed around it.

What *is* available and is used instead, from `/api/daemon/status`:

- `backend_status.motor_control_mode` — survives the 503 that
  `/api/motors/status` returns while the backend is down (and permanently on a
  robot in `error` state).
- `backend_status.control_loop_stats.mean_control_loop_frequency` — a genuine
  health signal. Nominal 50 Hz; below ~49 means that robot is struggling.
- `nb_error`, `max_control_loop_interval`.

---

## ✅ Fleet identity

All nine `hardware_id` values read live and confirmed against `robots.yaml`.
Steady-state RTT 6–7 ms, jitter 1–2 ms; first packet after idle spikes to
17–90 ms (Wi-Fi power-save), which is why the 1 Hz heartbeat exists.

---

## ✅ N-12 — Upload + play on 1.9.0 — **確認済み、実測あり**

`UploadMoveStartCmd` → `Chunk` → `Finish` → `PlayUploadedMoveCmd` は
`ws://<ip>:8000/ws/sdk` で正常に動作する。実装は
[`orchestra/fleet/upload.py`](../orchestra/fleet/upload.py)、CLI は
`orchestra show play`。

**実測（8台、非周期波形を指令波形と相互相関）**

| 項目 | 実測値 |
|---|---|
| gzip 圧縮率 | 66KB → 6.5KB (0.099) |
| 1台のアップロード | 1 チャンク / 6-25 ms |
| 送信側ファンアウト幅 | **1.2 - 1.3 ms** |
| **実位相スプレッド** | **18.0 ms** (sd 5.7 ms) |
| 共通遅延 | +158〜176 ms（全機共通、同期には無関係） |

計測ノイズ下限が ~20-25ms（50Hz 状態ストリーム）なので、真のズレは 18ms 以下。
事前予測の「10-40ms」と整合する。打楽器のアタックが同時に聞こえる境界
(10-20ms) のぎりぎり内側なので、**実用はできるが余裕はない**。

追従精度も測れた: 指令 ±7mm に対し実測 p2p 11.3mm（約81%）、roll は指令 8°
p2p に対し 8.4°（ほぼ100%）。並進のほうが追従が悪いのは Stewart 機構として
妥当。振動周期は指令 0.600s に対し実測 0.600s（100.1 BPM）で**テンポは完全一致**。

24.3秒の全曲を8台+音声5台で通し、演奏後もループエラー0・制御ループ
49.5-49.7Hz と劣化なし。

**計測でハマった2点**（同じ罠を踏まないよう記録）:
1. スロットは**再生完了で破棄される**。同じ `upload_id` を2度再生できず、
   2回目は無言で何も起きない。計測ごとに新しいスロットを上げること。
2. 周期波形の相互相関は**周期の整数倍だけ曖昧**。位相計測には非周期波形
   （静止→単発の振り→静止）を使うこと。
3. `initial_goto_duration` の「垂れた頭を戻す動作」を掴むと、各機の垂れ方が
   違うので無意味な数百 ms のばらつきが出る。先に姿勢を整えてから測ること。

---

## ✅ カメラ / メディア — 全9台正常（2026-08-07 修理後）

全機 **imx708** が libcamera に登録され、カメラ IPC ソケットも存在する。
`camera_specs` は全機 `wireless`。

### `is_local_camera_available()` が実際に見ているもの

Linux ではこの1行だけ（`daemon/utils.py:52-69`）:

```python
return os.path.exists("/tmp/reachymini_camera_socket")
```

**カメラの動作可否は一切判定していない。** ソケットファイルの有無だけ。そして
そのソケットは**デーモンのメディアサーバが正常起動したときにだけ**作られる。
つまりこの関数は実質「メディアサーバが健全か」を見ている。

「カメラIPCが無い個体」という言い方は誤り。固定的な機体の性質ではなく、
**実行時の状態**であり、しかも次の2つの経路で簡単に消える。

### 消える理由1 — `no_media` で接続すると自分で消す

`media_backend="no_media"` を指定すると SDK は `release_media()` を自動で呼び、
デーモンにメディアを手放させる（`reachy_mini.py` の `_setup_media`）。
`clip preview` の旧既定がこれで、動作確認するたびにソケットを消していた。
そのため `clip preview` の既定を `--transport upload`（メディアに触らない）に
変更した。

### 消える理由2 — ALSA を誰かが掴んでいるとメディアサーバが起動に失敗する

```
ERROR - Could not open audio device for recording.
Device is being used by another application. (4) ../ext/alsa/gstalsasrc
```

これが出るとメディアサーバが立ち上がりきらず、カメラソケットも作られない。
実際に踏んだ2ケース:

1. **pipewire との起動順競合** — カメラ交換後の再起動時、pipewire が先に
   ALSA を掴んでいた。落ち着いてからもう一度 `systemctl restart` で正常取得。
2. **私のエージェントが掴みっぱなし** — `op_disarm` がトルクを切るだけで
   `ReachyMini` を切断していなかったため、演奏終了後も何時間も /dev/snd を
   保持していた。この状態でデーモンを再起動すると上のエラーになる。
   `op_disarm` で `disconnect()` するよう修正済み。

**教訓**: このスタックでは「カメラが映らない」の原因が音声側にあることがある。
両者はメディアサーバという1つのプロセスにぶら下がっており、片方の失敗が
もう片方を道連れにする。

### mDNS ホスト名は全機 `reachy-mini` のまま

`hostname` は9台とも `reachy-mini`。avahi が衝突回避で
`reachy-mini-7.local` のような連番を勝手に付けているだけで、実体は同名。
LEARNINGS.md が警告する衝突そのもの。今の構成は WebRTC を使わないので実害は
無いが、将来 WebRTC を使うなら先に改名が要る。

---

## ⛔ r04 — アンテナモータ欠損（ハードウェア故障、2026-08-07 修理済み）

デーモンの `error` はソフトの問題ではなかった:

```json
"error": "Missing motors: [\"right_antenna\", \"left_antenna\"]!"
```

モータID 17 / 18 がシリアルバス上で応答していない。**デーモン再起動では直らない。**
2個が同時に落ちているので、個別のサーボ故障よりは**コネクタ/ケーブル外れ**の
可能性が高い（アンテナはバスの終端側にぶら下がる）。

物理確認の手順:
1. アンテナのコネクタを抜き差しする
2. `orchestra fleet status` で `error` が消えるか見る
3. 消えなければ、正常機のアンテナと入れ替えて切り分ける（サーボ側かバス側か）

`orchestra show play` は不健全な機体を理由付きで自動除外するので、r04 が
直るまでは 8台編成として動く。ただし F4 のチャイムバー担当なので、その音は
欠ける。

---

## ⛔ SSH — 鍵が未設置（エージェント配備をブロック中）

```
pollen@192.168.3.105: Permission denied (publickey,password)
```

Windows の OpenSSH は `BatchMode=yes` でパスワードプロンプトを出さないため、
非対話では入れない。**本命のエージェント経路（1-5ms）はこれが解けるまで使えない。**

1台につき1回、Git Bash から:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/reachy_orchestra -N ""
ssh-copy-id -i ~/.ssh/reachy_orchestra.pub pollen@192.168.3.105
```
その後 `orchestra fleet provision` → `orchestra fleet agent start`。

---

## ✅ N-2 / N-3 / N-4 — エージェント経路、実機で成立 **（本命）**

SSH鍵を9台に設置（`tools/install_ssh_keys.py`）した上で配備し、全9台で
「It's a Small World」24.3秒を完走。

**実測（9台、エージェント自己申告の start_error）**

| 項目 | 予測 | 実測 |
|---|---|---|
| start_error | 1-5 ms | **0.397 - 1.396 ms** |
| **9台のスプレッド** | 1-5 ms | **0.999 ms** |
| クロック同期 RTT | 6-7 ms | **3.47 - 3.81 ms** |
| 同期不確かさ | ±1-3 ms | **≤1.90 ms** |
| フレーム | — | 2429/2430（全機）、late tick **0** |
| 演奏後ループエラー | — | **0**（制御ループ 49.3-49.7 Hz） |

**アップロード経路（18ms）比で18倍改善。** 打楽器の可聴閾値 10-20ms に対して
一桁の余裕ができた。

- **N-2 成立**: `/venvs/mini_daemon/bin/python` で起動したエージェントが、
  デーモン稼働中に `localhost:8000/ws/sdk` へ並行接続してモーションを出せる。
- **N-3 成立**: 音声6台すべて `media=local` で、各機のスピーカーからステムが
  鳴る。WebRTC は一切経由しない。
- **N-4 成立**: Wi-Fi 上の Cristian で ±1.9ms。RTT が事前調査(6-7ms)より速い
  3.5-3.8ms だったのは、常時接続＋TCP_NODELAY のハートビートで電力管理の
  スリープに入らないため。

### ⚠ 最初の走行で2台落ちた — `localhost_only` だけでは足りない

初回は r01/r02 が arm に失敗した:

```
Connection mode selected: localhost_only
No local IPC endpoint. Using WebRTC backend for streaming.
op arm failed: [Errno 111] Connection refused
```

**設計時の私の想定が甘かった点。** SDK は
`connection_mode == "localhost_only"` **かつ** `is_local_camera_available()`
の両方が真のときだけ LOCAL を選び、駄目なら**黙って WebRTC に落ちる**
(reachy_mini.py:338)。カメラ IPC が無い個体（Lite 変種や、デーモンが
カメラを掴んでいる機体）では WebRTC になり、シグナリングサーバが居ないので
`Connection refused` で死ぬ。

修正: `connect()` はバックエンドを**明示指定**し、
`local` → `gstreamer_no_video` → `no_media` と段階的に降りる。
**音声の失敗でモーションを止めない** ―― 無音でも動くロボットのほうが、
何もしないロボットよりはるかにマシだから。修正後は9台とも `local` を取得。

CLI 側も、arm に失敗した機体を start 対象から外すようにした（失敗確定の
コマンドを撃ってログを二重に汚さない）。

---

## ⚠ N-2 — Can a second process own the robot? **(旧: GATING — 上記で解決済み)**

**Assumed:** yes. A plain Python script on the robot can connect to
`localhost:8000/ws/sdk` alongside the running daemon and command motion.

**Why it should hold:** the SDK's own app runtime does exactly this — a
`ReachyMiniApp` is spawned as a separate OS process and connects as an ordinary
SDK client (`apps/app.py:134-143`). SDK clients bypass the robot app lock
entirely (`robot_app_lock.py:1-14`), and the WebSocket server accepts any
connection with no exclusivity (`ws_server.py:115-137`).

**Test it:**
```bash
orchestra fleet provision --robot r01     # finds an interpreter with reachy_mini
orchestra fleet agent start --robot r01
orchestra clip preview --clip shaker/groove --robot r01   # via the agent path
```

**If it fails:** fall back to `UploadMove*` + `PlayUploadedMoveCmd` fan-out.
Confirmed present in the `AnyCommand` union (`protocol.py:677-685`) and routed by
`ws_server.py:156-171`. Costs ~10–40 ms of uncorrectable skew instead of 1–5 ms.

⚠ **Exactly one writer per robot.** Two SDK clients both succeed and then fight
over the same target attributes, silently. The agent's non-reusable port bind is
the only mutual exclusion available — do not defeat it.

---

## ⚠ N-3 — Local audio from the agent process **(GATING for robot-speaker audio)**

**Assumed:** with `connection_mode="localhost_only"` the SDK selects
`MediaBackend.LOCAL` (verified in source at `reachy_mini.py:338`) and
`media.play_sound(path)` plays through the robot's own speaker without WebRTC.

**Unknown:** whether media acquire/release contends with the daemon, and what the
constant GStreamer→ALSA latency is.

**Test it:**
```bash
orchestra show deploy small_world --robot r01
orchestra show run small_world --robot r01
# record with a phone; read the motion-vs-sound offset off the waveform
orchestra calibrate audio --robot r01 --lead-ms <measured>
```

**If local audio does not work from a second process:** either move audio into a
`ReachyMiniApp` (which takes the robot lock), or drop robot audio and let the
physical instruments carry the performance.

---

## ⚠ N-4 — Real clock-sync accuracy **(GATING for the sync claim)**

**Assumed:** ±1–3 ms offset accuracy over Wi-Fi, from a 6–7 ms RTT floor with
1–2 ms jitter, using min-RTT sample selection.

**Measured so far:** on loopback, offset error 3.8 µs and start spread 0.003 ms
across 9 agents (`tools/sync_selftest.py`). That validates the *algorithm*, not
the *link*.

**Test it:**
```bash
orchestra show sync            # reports per-robot offset and uncertainty
# leave it running 10+ minutes and watch drift_ppm stabilise
```

Then the measurement that actually matters:
```bash
python tools/measure_skew.py rehearsal.wav --bpm 112
```
All robots strike beat 1 in unison, one microphone, cross-correlate the onsets.
**Target: <10 ms worst-case spread.** Keep mic distances within ~30 cm of each
other — sound travels 34 cm/ms, so geometry alone shows up as skew.

---

## ⚠ N-5 — Real head workspace — **z は実測済み、x/y は未測定（2026-08-09）**

z は上の「並進ボックスの z が上下逆に狭かった」で掃引し、`[-0.046, 0.021]` に
置き換えた。**x/y の ±25 mm はいまも仮の値。** 全クリップの実測ピークが 5.0 mm
（使用率 20 %）なので、いま困ってはいない ―― 困る前に測る価値が出るのは、構えを
これ以上大きくしたくなったときだが、その壁はボックスではなくヤコビアン
（r04 が上限まで 0.24 rad/s）のほうが先に来る。

**Assumed:** ±25 mm in x/y (`safety.head_translation_m`), chosen conservatively.

Remember `np.eye(4)` is the *neutral* head, not the mechanism origin — IK applies
`head_z_offset = 0.177 m` internally (`analytical_kinematics.py:76`).

**Test it:** sweep each axis at neutral orientation until IK starts failing, and
replace the box in `config/fleet.yaml` with the measured envelope. Larger
amplitudes mean more audible shakers and more visible conducting.

---

## ⬜ N-7 — Strike mechanics

Unknown: the minimum pitch/z amplitude and duration that produce a clean audible
strike, and the minimum usable inter-strike interval.

Current placeholders: `strike_min_interval_s: 0.11`, and clip depths of 6–7.5 mm.

**Test it:**
```bash
orchestra calibrate strike --robot r08     # descend 1 mm at a time to contact
orchestra clip preview --clip woodblock/strike --robot r08 --bpm 112
```

The result sets the maximum note rate the whole arrangement can use.

---

## ⬜ N-8 — Thermal behaviour and duty cycle

Since there is **no temperature telemetry** (N-6), this has to be established by
observation: run three minutes of continuous eighth-note strikes and feel the
motors, watching `mean_control_loop_frequency` for degradation.

Current limit: `strike_duty_max_hz: 4.0` over a 10 s window. For reference, the
busiest robot in *Small World* (G4) averages **1.35 strikes/s** — comfortable
margin, which is a deliberate consequence of splitting the melody across five
robots rather than giving one robot everything.

---

## ⬜ N-9 — Shaker audibility

Is a head-mounted shaker loud enough against nine robots' servo noise plus their
speakers, at audience distance?

**If not:** shift weight from Tier 1 (contactless) toward Tier 2 (struck), which
has far more attack. The cast is a `song.yaml` edit.

---

## ⚠ N-10 — 100 Hz vs 50 Hz on the loopback loop — **100 Hz のまま据え置き（2026-08-09）**

The daemon consumes at a hard 50 Hz (`robot/backend.py:71`), so 100 Hz is 2×
oversampled. If 50 looks identical, it halves the agent's CPU and message rate
for free. Set `agent.fps` in `config/fleet.yaml`.

**現状の判断（未測定のまま据え置き）**: ファームウェアの
`control_loop_frequency = 50.0` はハードコードで上げられない。こちらが 100 Hz
で送っても消費されるのは 50 Hz なので、**ロボットの最大レートでは動いていない
――「最大」が 50 Hz だから、上限に対しては 2 倍余っている。**

それでも 100 Hz を落とさない理由: 148 秒の通しで **late tick 0 / dropped 0**。
CPU に困っていない以上、50 Hz に落とす利得は無い。逆に 100 Hz は消費側の位相と
無関係に「直前のサンプル」が常に 10 ms 以内であることを保証する（50 Hz 同士だと
位相がずれたとき最大 20 ms 古いサンプルを読む）。**測るべきは CPU ではなく、
50 Hz に落としたときのエイリアシングの有無**で、それは実機の目でしか判定できない。

---

## ⬜ N-11 — Recover r04

`192.168.3.133` had daemon state `error`, motor backend down, port 8443 closed.

```bash
orchestra fleet recover --robot r04
```

Uses `systemctl restart` over SSH, **not** the API stop/start pair — the sibling
project's `LEARNINGS.md` documents the API route corrupting WebRTC signalling
state.

---

## ⬜ N-12 — Upload/play fallback path on 1.9.0

Worth confirming even though it is not the primary path, because it is the
fallback if N-2 fails. The commands are present in the 1.8.0 `AnyCommand` union
and routed by the WebSocket server; 1.9.0 needs a quick check.

---

## Physical build (parallel with software, not a blocker)

- **Mounts.** Velcro or 3D-printed clips for head-crown instruments. Antenna
  bells must be *grams*, not tens of grams.
- **Compliant beaters.** Silicone or foam tip on a slightly springy shaft. This
  is what absorbs strike overshoot so the servo never fights a rigid stop — it is
  a safety component, not a finishing touch.
- **Fixed stands.** A stand that drifts 2 mm turns a calibrated strike into
  either a miss or a stall. Tape or weight them, and re-run
  `orchestra calibrate strike` after any physical change.
- **Spacing.** Beyond head-swing radius plus instrument extent.

---

## Recommended, low risk, high value

**DHCP reservations on the Speedefy router**, using the MACs already in
`robots.yaml`. It is a router-side change that touches zero robots and makes IPs
stable across reboots — the sibling project notes IPs drifting after reboots with
only four robots.

**Unique hostnames are optional here.** They would be mandatory with WebRTC (the
mDNS collision permanently corrupts signalling for the second identically-named
robot to join), but the local-media architecture never triggers it. Worth doing
eventually for SSH sanity; not blocking.

---

## 2026-08-28 — Lamban を実機9台で通した（マンデ編成）

✅ **通った。** `orchestra show run lamban --record r01`、207.8 秒、
completed 9 / dropped 0 / failed 0。start_err 0.472–1.330 ms、全機 20430/20431
フレーム、late 0、クロック同期の最悪不確かさ 1.80 ms。参照元の小曲
（10401 フレーム、112 BPM 一定）に対して **8.5倍長く、96→120 BPM で動く曲**だが、
数字は同じ水準を保った。

環境: `reachy-mini` 1.10.0（PC側）/ デーモン 1.9.0（実機）。バージョン差の影響は
観測されなかった ―― PC 側で要るのは検証用の IK だけで、実機側は自分の SDK を
使うため。`tests/test_interp.py`（SDK と bit-identical の検証）も 1.10.0 で通る。

### ✅ 実機バグ: 保存した音量が、端末によっては一度も送られない

**最初の通し公演は完全に無音だった。** 9台が 0.5 ms 以内で同期して動き、
エージェントは 3268855 フレーム全部を push して err 0 / complete と報告し、
WAV も ALSA ミキサー（PCM 100%, on）も正常。それでも音が出ない。

原因は `cli.py` の1行:

```python
vol = None
if keys.arrows:                       # ← ここ
    seed, src = _volume_seed(ctx, here)
    vol = VolumeController(..., apply_initial=(src == "config"))
```

**矢印キーを出す端末でしか音量を送っていなかった。** 非対話シェル・`ssh -T`・
CI から走らせると `_volume_seed` が呼ばれず、較正ファイルの `volume: 100` は
読まれもしない。ロボットは前回誰かが置いていった音量のまま鳴る。

ライブで上下できるかどうかと、保存した音量を復元するかどうかは**別の話**。
後者はキー入力と無関係に常に行うべきで、そう直した。
**保存した値を適用しないなら、保存する意味が無い。**

### ⚠ 訂正: 「サーボ音しか録れていない」は私の誤診だった

録音の分光で 320–640 Hz に 84.7% 集中しているのを見て「サーボの動作音であって
音楽ではない」と結論した。**間違い。** それはバラフォンだった ―― この曲の主役
（mix_db 0.0）で、エネルギーの約60%が 300–600 Hz にある。

誤りの理由は比較対象。`preview.wav`（9本の合計＝当然広帯域）と比べたが、
**1台のマイクが拾うのは近くの1〜2台に偏る**ので、比べるべきは「その位置で
支配的になる台のステム」だった。静止状態で録り直したら、録音の帯域分布
（300–600 Hz 69.6% / 600–1200 Hz 24.0%）はバラフォンのステム本体
（59.8% / 29.7%）とほぼ一致した。

この repo の原則どおり、**間違った数字を正しい数字の隣に残す**。分光は
「何が鳴っているか」を答えるが、「何と比べるか」を間違えると自信を持って
逆の結論を出す。

### ✅ 内蔵マイクでも到着時刻は測れる ―― ただし測定音を変えれば

⛔ **まず失敗した。** `orchestra calibrate skew` を**7箇所**（r01/r02/r04/r05/
r06/r07/r09 のマイク）から録って回したが、波形相関は corr **0.010-0.020**
＝実質無相関。出てくる値は 1485.9 / 3018.8 ms のように**バースト間隔 1.5 秒の
倍数**で、隣のスロットに誤ロックしているだけだった。包絡線に切り替えても
3回の再現が 100-280 ms ばらついて使えない。

⚠ **原因は「測定音がノイズだったこと」。** `make_calibration.py` の測定音は
帯域制限ノイズ（300-6000 Hz, 250 ms）で、台ごとに直交シードを与えてある ――
PC 上の相関としては理想的な設計。だが **Reachy のマイクは音声用の前段 DSP
（エコー除去・雑音抑圧・AGC）を通る。雑音抑圧は定常的なノイズを消すための
機能で、帯域制限ノイズはまさにその標的**。波形の位相も包絡線の立ち上がりも
残らない。

✅ **測定音をトーンにしたら通った。** `tools/make_pingtest.py` を新設:

  * 35 ms の**減衰正弦**、立ち上がり 1 ms ―― 雑音抑圧に消されず、到着時刻が一意
  * 台ごとに違う周波数（1600-3730 Hz）―― **狭帯域フィルタでその台だけ抜ける**。
    遠い台が床に埋もれる問題がこれで解ける
  * 8回繰り返し ―― 中央値と四分位幅（＝その位置での再現性）が出せる

結果（`tools/measure_ping.py`、4箇所から）:

```
  台      r02のマイク  r04のマイク  r07のマイク  r09のマイク    最小   位置差
  r01        +33.0      +35.6      +12.8      +12.2      +12.2     23.3
  r02            -       +7.6      +15.6      +11.8       +7.6      8.0
  r03         +1.9       +2.8       +7.7      +13.7       +1.9     11.8
  r04        +31.5          -      -22.1       +8.4       +8.4     53.7
  r05         -0.7      +34.3       +7.0      -21.4       -0.7     55.7
  r06         +5.8       +4.6      -25.6      -22.2       +4.6     31.4
  r07         +1.6      +38.2          -      -23.7       +1.6     61.8
  r08        +29.0       +7.1          -       +5.2       +5.2     23.8
  r09         -1.9       +4.8          -          -       -1.9      6.7

  SNR x43-x3697 / 8回中8回検出 / 位置ごとの四分位幅 1.2-7.2 ms
```

**測定系は動くようになった。だが答えは変わらない。** 位置差が **6.7-61.8 ms**
あり、1.5 m の卓を空気が渡る 4.4 ms を大きく超える。つまりこの数字が測って
いるのは**部屋（反射）であって機体ではない**。位置を変えても動かない成分だけが
`audio_lead_ms` の候補で、それは位置差に埋もれて取り出せない。

→ **`audio_lead_ms` は全機 0.0 のまま。** 今回はそれが「未実施」ではなく
**測った上での判断**になった。数字を入れれば、録音した1点以外のすべての
聴取位置で意図的にずらすことになる。

なお電気的な同期のほうは別途 0.5 ms 以内で取れており（上）、
「打楽器が同時に聞こえる」境界の 10-20 ms に対して十分な余裕がある。

### ⚠ 低域: 2台は小型スピーカーでほぼ鳴らない

ステムの 300 Hz 未満のエネルギー比:

```
  balafon  0.1%   karignan 0.0%   dundunba 1.7%   sangban 6.9%   kora  9.5%
  tama    33.7%   ngoni   47.0%   lead    80.1%   djembe 90.6%
```

djembe の bass は f0 78 Hz、tone 196 Hz。28cm の機体の小型スピーカーはこの帯域を
ほとんど再生できない。参照元の編成（チェレスタ・グロッケン・ヴィブラフォン・
マリンバ）は全部 500 Hz–5 kHz の明るい楽器で、偶然この機体に最適だった。

**設計時に 16 kHz / 8 kHz の上限だけを見て、下限を見ていなかった。**
`sampler.suggest_pitch_shift` は高すぎる打楽器を下げる仕組みを持つが、
低すぎる楽器を上げる仕組みは無い。民族音楽学的な音高の絶対値を守るか、
実機で聞こえることを守るかのトレードオフになる（音律の関係とリズムは
どちらでも保たれる）。⬜ 未着手。

### ⚠ r01 が公演中に Wi-Fi から落ちた

2回目の通しで r01 が `dropped mid-show`（その台だけ無音になり、残り8台は続行 ――
設計どおりの縮退）。以後 ping も SSH も無応答になり、`orchestra fleet recover` も
90秒で失敗。**物理的な電源再投入で復帰した。** 再投入後はエージェントが落ちて
いるので `orchestra fleet agent start --robot r01` が要る。原因は未特定。⬜

⬜ **発熱は未測定のまま。** この曲は 204 秒で、参照元の 24 秒の 8.5 倍。
`gesture_duty_max_rms` / `antenna_duty_max_rms` は今も実測の裏付けが無い値で、
今回のビルドの最悪値は上限の 84%。この日は通しを4回走らせたが触っての確認は
していない。連続で3回以上まわして実際に触って確かめること（N-8）。

---

## 2026-08-28 — 公演中に台が落ちる原因: Wi-Fi のパワーセーブ

✅ **原因を特定して直した。**

### 症状

公演中に単独の台が `dropped mid-show` になる（その台だけ無音になり、残りは続行 ――
設計どおりの縮退だが、公演としては1台欠ける）。エージェントのログ:

```
r09  19:50:07  deadman: no PC contact for 5.0s -- parking after this phrase
r04  19:59:58  deadman: no PC contact for 5.2s -- parking after this phrase
r08  20:04:58  deadman: no PC contact for 5.0s -- parking after this phrase
```

**別々の台が別々の時刻**。PC 側の一斉停止ではない。1台は完全にネットワークから
消え、ping も SSH も通らなくなって物理的な電源再投入が要った。

### 潰した仮説

| 仮説 | 実測 | 判定 |
|---|---|---|
| 電源不足 / サーマルスロットル | `vcgencmd get_throttled` = **0x0**（15.7日間クリーン） | ✗ |
| 発熱 | 44.3–45.2 °C | ✗ |
| メモリ枯渇 | 812–942 MB / 3796 MB | ✗ |
| CPU 過負荷 | load 0.86–1.42 | ✗ |
| 電波が弱い | −52〜−55 dBm、リンク品質 55–58/70 | ✗ |
| ロボットの再起動 | uptime 15.7日（電源を切った1台を除く） | ✗ |

### 原因

**Wi-Fi のパワーセーブが子機側で有効だった**（`brcmf_cfg80211_set_power_mgmt:
power save enabled`、全9台）。9台へ 5Hz で3分間 ping した実測:

```
                 損失         RTT 最小    平均      最大
  power save 有効  0.45-2.57%    3 ms   100-118 ms  610-690 ms
  power save 無効  0.00-0.67%    3 ms    68- 73 ms  590-706 ms
```

**平均が最小の 30 倍ある。** 子機が寝ているあいだ AP がパケットを溜め、DTIM
ビーコンでまとめて渡すため。ショー中に流れるのは 1Hz のハートビートだけなので、
ここで数百 ms が積もると 5 秒のデッドマンに届く。

### 直し方

`orchestra fleet provision` に組み込んだ（`orchestra/show/deploy.py`）:

```bash
nmcli connection modify <wlan0 の接続> wifi.powersave 2   # 再起動後も効く
iw dev wlan0 set power_save off                           # 即時反映
```

パスワード無しの sudo が要る。無い環境では警告だけ出して provision 自体は成功
させる ―― 「気付かないまま公演で落ちる」より「毎回警告が出る」ほうがよい。

### ⬜ 残り: PC 側のドングルも同じ状態

ロボット側を切っても平均 RTT は 68–73 ms 止まりで、最小の 3 ms には戻らない。
残りは **PC 側の USB ドングル（rtl8821au、`wlx984827b2551a`）**:

```
Power save: on
Link Quality=30/100  Signal level=39/100
RX dropped 1114 packets
dmesg: usb 1-3: reset high-speed USB device number 3
       usb 1-3: USB disconnect, device number 3
```

**USB バス上でリセットと切断が起きた記録がある。** これは1台が完全にネット
ワークから消えた件の説明になりうる。こちらは root が要るので未実施:

```bash
sudo iw dev wlx984827b2551a set power_save off
# 恒久化するなら
sudo nmcli connection modify robot-wifi 802-11-wireless.powersave 2
```

USB ドングルのリセットが繰り返すようなら、別のポート（できれば別の USB
コントローラ）に挿し替えるか、有線でブリッジするほうが確実。

### 耐久試験（lamban x3 連続、パワーセーブ off 後）

```
  take 1/3   207.7s   completed 9  dropped 0
  take 2/3   213.6s   completed 6  dropped 1   <- r01 が脱落
  take 3/3   207.3s   completed 8  dropped 0
```

**r01 以外の8台は3回とも完走。** start_err は 0.356-1.368 ms、遅延フレーム 0。
パワーセーブの修正は効いている（修正前は1回の走行で1台落ちる頻度だった）。

✅ **発熱は問題なかった（N-8 の回答）。** 204秒 x 3回 = 10分強の連続演奏の前後で:

```
  開始前  44.3 - 46.2 'C
  終了後  43.8 - 45.2 'C      throttled=0x0（全機）
```

**上がっていない。** デューティ上限（ヘッド 2.5 / アンテナ 3.0 rad/s 実効値）は
今も実測の裏付けが無い数字だが、少なくともこの曲・この編成・この長さでは
熱的な余裕が十分にある。より長い曲や連続公演では改めて測ること。

### ⛔ r01 だけは別の故障

**r01 はパワーセーブを切っても落ちる。しかも落ち方が違う。**

  * 他の8台の脱落: ハートビートが5秒途切れる → デッドマンで自動退避。
    ネットワーク自体は生きていて、次の走行では普通に参加する。
  * r01 の脱落: **ネットワークから完全に消える。** ping も SSH も通らず、
    `orchestra fleet recover` も届かない。**物理的な電源再投入以外に戻す手が無い。**
    この日2回起きた。

つまり r01 の症状はハートビートのタイムアウトではなく、Wi-Fi インタフェース
（あるいは機体そのもの）が固まっている。brcmfmac の SDIO ファームウェアが
トラップすると、こうなってリブートまで復帰しない。

⚠ **訂正: 「r01 だけカメラが死んでいる」は誤りだった。**
最初、r01 の dmesg にある

```
  imx708 10-001a: failed to read chip id 708, with error -5
  imx708 10-001a: probe with driver imx708 failed with error -5
```

を「この個体だけのハードウェア異常」として挙げた。**r02 と r07 を同じ条件で
調べたら、まったく同じ2行が出ていた。** `/dev/video*` の並びも同一。
全機に出る無害なプローブメッセージであって、r01 の差ではない。
1台しか見ずに「これが違いだ」と言ったのが誤り。**差だと言う前に、
差でない台を確かめること。**

r01 と健全な台（r02 / r07）を並べて比較した結果、**ソフトウェア・設定の差は
何ひとつ見つからなかった**:

| 項目 | r01 | r02 / r07 |
|---|---|---|
| カメラのエラー | imx708 x2 | **同じ** |
| /dev/video* | 16個 | **同じ** |
| ALSA capture を掴むプロセス | daemon の python | **同じ** |
| 動作サービス | daemon / bluetooth / gpio-shutdown | **同じ** |
| brcmfmac のメッセージ | txcap_blob のみ | **同じ** |
| メディアバックエンド | local (IPC) | **同じ** |
| エージェント側の分岐 | 無し（役割を知らない） | **同じ** |

配布物も同等（全機 20431 フレーム、r01 のアンカーは 265 でむしろ少ない）。

⬜ **次にやること**: r01 を電源再投入したら、まず永続ジャーナルを有効にして
（`sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`）
次に固まったときの直前のログを残せるようにする。今は再起動でカーネルログが
消えてしまい、死因が読めない。
それでも再発するなら、**この個体を公演から外す**か交換するのが早い ――
9台のうち1台だけが、他と同じ条件で、他と違う壊れ方をしている。

### ✅ deploy が古いビルドを黙って配る穴を塞いだ

cast 入替実験の準備中に踏んだ。手順はこう:

1. `song.yaml` の cast を書き換える（r01 を指揮者から演奏者へ）
2. `orchestra song build lamban` → **検証に落ちて何も書かれない**（正しい挙動）
3. `orchestra show deploy lamban` → **成功する**

このとき配られるのは **`build/` に残っていた前回の（古い cast の）プログラム**と、
**今の cast に従って選ばれた音声**。つまり r01 へ「指揮者のモーション」と
「バラフォンの音声」が同時に届く。エラーは1つも出ない。

音とモーションの対応は `anchors` から両方を出すことで構造的に保証している、
というのがこの設計の柱なのに、配布の段でその前提が崩れる。しかも**実機で
見るまで気付かない**種類の壊れ方。

`cmd_show_deploy` に、配る前の照合を入れた ―― ビルド済みプログラムの `part` が
今の cast と食い違っていたら止める:

```
error: the build in build/lamban does not match the current cast:
  r01: built for 'balafon' but the cast now says 'lead'
  r02: built for 'conductor' but the cast now says 'balafon'
```

**「ビルドが落ちたら何も書かない」だけでは足りない。落ちたあと、古いものが
配られないところまでが対策。**

### ⚠ cast 入替実験 — 結果は「判定不能」

r02 を指揮者、r01 をバラフォン奏者にして 3回連続:

```
  take 1/3  207.5s  completed 9  dropped 0
  take 2/3  208.3s  completed 9  dropped 0
  take 3/3  207.7s  completed 9  dropped 0
```

**一度も落ちなかった。だからといって何も証明されていない。**

r01 の脱落頻度を数えると、パワーセーブ修正後の実績は **6回中1回**（前回の
soak の take 2）。この頻度なら、3回連続で無事故になる確率は
(5/6)^3 = **58%** ―― コイン投げと変わらない。**「3回通ったから直った」とは
言えない。**

言えるのは弱い示唆だけ:

  * r02 を指揮者にしても落ちなかった → 「指揮者という役割」が原因である線は
    やや弱まった（ただし3回では足りない）
  * r01 を演奏者にしても落ちなかった → 機体説も否定されていない（同上）

⬜ **必要なのは回数。** 判定するには片側の cast で最低6-9回は回す必要がある
（1回あたり 3.5分なので 20-30分）。それまでは「r01 に固有の間欠故障の疑いが
残っている」以上のことを言わない。

なお take 2 で r06 の start_err が **3.181 ms** と、これまでの最大値
（他は全て 1.6 ms 以下）を記録した。「打楽器が同時に聞こえる」境界の
10-20 ms には十分収まっているが、外れ値として記録しておく。


### ⛔ 「胴体を90度ねじったまま保持することが原因」―― 測ったら違った

r01 だけが持つ物理条件を数えたら、確かに際立っていた:

```
  robot  part        body_yaw 振れ幅   総回転量   |yaw|>60° の時間
  r01    conductor       208.4°       3285°       193.7s   <-
  r02-r09 （最大）         69.5°       8206°         0.0s
```

**総回転量はむしろ r01 が少ない**（よく動くのは r06/r09 で 8200°台）。
差は「動く量」ではなく **動作点** ―― r01 だけが 204秒の公演のうち 193.7秒を
|body_yaw| > 60° で過ごす。他8台は全員 0.0秒。arm した瞬間から −90° で、
終幕で +90° へ反転する。`conductor_home_yaw_deg: -90`（指揮者は台の上で
90度回して置いてある）が理由で、これを 0 にすると終幕の180度旋回が
可動限界 150° を超えてビルドが落ちる（実際に確認した）。

配線やアンテナが回転部を通っているなら、ねじれたまま保持されることで
接触が間欠的に切れる ―― 「完全にネットワークから消えて電源再投入以外で
戻らない」という r01 固有の壊れ方とよく整合する筋書きだった。

**そこで、本番構成（r01=指揮者）で公演を2回まわしながら、9台全部に 5Hz で
ping を並走させた。** r01 だけがねじれている7分間、その link だけが劣化するか:

```
  robot            loss   rtt最小    平均     最大    最大無応答
  r01(ねじれ)      0.00%    2.86    8.8 ms   609ms     0.80s
  r02             0.00%    2.93   10.3      625       0.82s
  r03             0.00%    2.82   10.2      630       0.82s
  ...（以下同様）
  r08             0.00%    2.60    9.2      610       0.80s

  損失     r01 0.00%  / 他の中央値 0.00%
  平均RTT  r01 8.83ms / 他の中央値 10.18ms  → 比 0.87倍（r01のほうが良い）
  最大無応答 r01 0.80s / 他の中央値 0.82s   → 比 0.98倍
```

**r01 の link はまったく劣化していない。むしろ全機で最良の部類。**
ねじれが RF に効いているなら、7分間の保持でここに出るはずだった。出ない。
→ **この仮説は落とす。** 2回とも 9/9 完走、脱落ゼロ。

### ⚠ ただし「パワーセーブ修正で直った」とも、まだ言えない

上の測定で、もう1つ自分の誤りが見えた。**待機中に測ったレイテンシは、
公演中の実態を表していない。**

```
                              損失          平均RTT    条件
  ① 何もしていない            0.45-2.57%    100-118ms  待機中
  ② ロボット側だけ off        0.00-0.67%     68- 73ms  待機中
  ③ 現在                      0.00%          8.8-10.3ms 公演中
```

③ が ② より桁違いに良いのは、**公演中は常に双方向の通信がある**ので
どちらの側も深く眠らないから。5Hz の ping だけでは足りず、ハートビートと
エージェントの TCP が加わって初めてリンクが起きたままになる。

つまり**「パワーセーブ由来のレイテンシが積もって5秒のデッドマンに届いた」
という私の説明は、公演中の条件では成り立ちにくい。** 修正前の公演中の
link を測っていないので、**何が直したのかは確定できていない。**

事実として言えるのはここまで:

  * 修正前: 脱落が繰り返し起きた（r01 が2回、pingtest でも2台が未完走）
  * 修正後: **5回連続で 9/9 完走**（入替 cast 3回 + 本番 cast 2回）
  * ただし旧来の脱落頻度 1/6 なら、5回無事故は確率 (5/6)^5 = **40%**。
    運で説明できてしまう範囲
  * 公演中の link は現在きわめて良好（損失 0%、平均 10ms、1秒超の途切れ 0回）

⬜ **PC 側のドングルは未対処のまま。** `Power save: on`、NM 設定も `default`、
RX dropped は今日の走行で **1114 → 1591** に増えた（+477）。dmesg には USB
リセットと切断の記録もある。残る最有力の未対処要因はここ。


### ✅ PC 側も切って 6回連続 — 脱落ゼロ、発熱も変化なし

PC のドングル（rtl8821au）のパワーセーブを切ったあと、本番構成で6回連続:

```
  take 1-6/6   各 207.5-208.4s   completed 9  dropped 0  failed 0
  温度  開始前 44.3-45.7'C  ->  終了後 43.8-45.7'C   throttled=0x0（全機）
```

**21分の連続演奏で温度が上がらない。** N-8（発熱の未測定）はこれで閉じてよい
――少なくともこの曲・この編成では。

待機中の link は劇的に良くなった（同条件比較）:

```
                          損失          平均RTT     最大RTT
  ロボット側だけ off      0.00-0.67%     68-73 ms   590-706 ms
  PC 側も off             0%             7.8-8.2ms   17-24 ms
```

**最悪値が 30倍良くなった。** 数百ミリ秒のスパイクが消えた。

⚠ **ただし 21分の連続運転では、60秒の待機測定ほど良くない:**

```
  21分の公演中（5Hz ping 並走）
    損失 0.44-1.19%  平均RTT 92-98ms  最大RTT 1065-1464ms
    最大の途切れ 1.66s / 1秒超 70回 / 3秒超 0回 / 5秒超 0回
```

短い待機測定（平均 8ms）と長時間の公演中（平均 97ms）でこれだけ違う。
**測定は本番と同じ長さ・同じ負荷でやらないと数字を信用できない。**
ドングルの RX dropped が今日だけで 1114 -> 1591 と増え続けているので、
持続負荷で劣化している可能性が高い。

それでも**デッドマンの閾値 5秒に対して、実測の最大途切れは 1.66秒 ―― 3倍の余裕**。
3秒を超えた途切れは9台・21分で1回も無い。ここが、落ちなくなった直接の説明。

### 現時点の集計

```
  ロボット側 power save off 後   14テイク中 1回脱落（r01、PC側修正の前）
  PC 側も off 後                  6テイク中 0回
```

旧来の頻度（1/6）が続いていたなら6回無事故の確率は (5/6)^6 = 33%。
**回数だけでは決定的でない。** 決定的なのは上の link 実測 ―― デッドマンが
発火する条件（5秒の途切れ）が、実測レンジの外へ出た。

⬜ **PC 側の設定は再起動で戻る。** `iw` で切っただけで NetworkManager の
プロファイルは `default` のまま。恒久化するには
`sudo nmcli connection modify robot-wifi 802-11-wireless.powersave 2`。
⬜ ドングルの RX dropped が増え続ける件は未解決。長時間の公演を組むなら
別の USB コントローラへ挿し替えるか、有線を引くのが確実。
