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
