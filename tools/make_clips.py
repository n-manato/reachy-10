#!/usr/bin/env python3
"""マンデ編成の演奏クリップ8本を書き出す（初回のオーサリング用）。

    python tools/make_clips.py --force

**普段は使わない。** クリップは `orchestra clip fit --write` や
`orchestra song fit --write` が振幅を書き換えていくので、リポジトリにある
`clips/**/*.clip.json` のほうが真実になる。ここを走らせると調整が消える。
参考元の `tools/make_small_world.py` と同じ扱い（`--force` が要る）。

動きの形は参考元が確立したものを土台にしている:

    振りかぶり → 打点(anchor) → 跳ね返り(overshoot) → 復帰

`anchors` は**音が出る瞬間**であって動作の開始ではない。叩く楽器は振りかぶりが
拍より前に来る必要があるので、コンパイル時に `start_beat = 音符の拍 - anchor`
として前へずらされる。その余地が `song.yaml` の `lead_in_s`。

★鉄則: すべてのトラックは中立で始まり中立で終わる★

コンパイルは**中立からの差分の足し算**なので、クリップが中立以外の値で
終わると、その差分が「常時かかるオフセット」として残る。16分音符で発火する
パートは1打あたり4つ前後のクリップが重なるため、わずかな居残りでも4倍に
積み上がり、アンテナが可動端(150°)に張り付く。実際そうなった ――
コラのアンテナが -150°〜-2° を振り回し、`song fit` で振幅を 1/34 にしても
実効値が全く下がらなかった（振幅ではなくオフセットの重なりだったので）。

**構え（持続する姿勢）は persona の Stance が持つ。クリップは打点の前後だけ
動いて、必ず中立へ帰る。** これが分担。

振幅は控えめに置いてある。`orchestra clip check --bpm 96 --bpm 120` →
`orchestra clip fit --write` で安全域に詰めるのが前提。
"""


from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 中立。`config/fleet.yaml` poses.neutral_antennas_rad と同じ値。
NEUTRAL_ANT = (-10.0, 10.0)

UNITS = {"translation": "mm", "rotation": "deg"}


def head(*rows):
    return [{"beat": b, "pose": p, "interp": i} for b, p, i in rows]


def scalar(*rows):
    return [{"beat": b, "value": v, "interp": i} for b, v, i in rows]


def ant(*rows):
    return [{"beat": b, "value": list(v), "interp": i} for b, v, i in rows]


CLIPS = {}
N = NEUTRAL_ANT

# --- バラフォン --------------------------------------------------------------
CLIPS["balafon/strike"] = dict(
    name="balafon_strike", instrument="balafon", placement="trigger",
    loop_beats=1.0, anchors=[0.2],
    description=(
        "バラフォンを双バチで叩く（エアー）。\n\n"
        "見分けの要点: **横に一番広く渡る**。3オクターブ21枚の木琴を左右に移動しながら"
        "叩く楽器なので、y の移動と体の向きが打点ごとに入る。うつむいて棒を見ている姿勢は"
        "ペルソナの構えが持っていて、ここは打点の前後だけを担当する。\n\n"
        "ンゴニ・コラも同じ音階を鳴らすが、あちらは弾く楽器で体が動かない。"
        "「大きく横に渡りながら上から落とす」のはこれだけ。"),
    tracks=dict(
        head=head((0.0, {"y": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk"),
                  (0.08, {"y": -4.0, "z": 2.5, "pitch": -3.0}, "minjerk"),
                  (0.2, {"y": 4.0, "z": -3.5, "pitch": 4.0}, "linear"),
                  (0.28, {"y": 5.5, "z": -4.2, "pitch": 4.8}, "minjerk"),
                  (0.62, {"y": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.08, -2.2, "minjerk"),
                        (0.2, 2.2, "minjerk"), (0.62, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.2, (-22.0, 22.0), "minjerk"),
                     (0.62, N, "minjerk"))))

# --- コラ --------------------------------------------------------------------
CLIPS["kora/pluck"] = dict(
    name="kora_pluck", instrument="kora", placement="trigger",
    loop_beats=1.0, anchors=[0.12],
    description=(
        "コラを弾く（エアー）。\n\n"
        "見分けの要点: **頭がほとんど動かず、耳だけが弾く**。21弦を両手の親指と"
        "人差し指で弾く楽器で、奏者の体幹は静止したまま指だけが走る。その指を"
        "アンテナに担わせた。耳が大きく開いた構えはペルソナ側（速度ゼロ）にあり、"
        "ここはその上で指を弾くぶんだけ動く。\n\n"
        "16分で弾き続けるパートなので、**打点の前後 0.45 拍で必ず中立へ帰る**。"
        "居残ると隣の音のぶんと足し算になって可動端に張り付く。\n\n"
        "ンゴニも撥弦だが、あちらは深く前傾して胴を覗き込む。こちらは顔が正面。"),
    tracks=dict(
        head=head((0.0, {"z": 0.0, "pitch": 0.0}, "minjerk"),
                  (0.05, {"z": 0.9, "pitch": -0.8}, "minjerk"),
                  (0.12, {"z": -1.1, "pitch": 1.2}, "linear"),
                  (0.18, {"z": -1.4, "pitch": 1.5}, "minjerk"),
                  (0.45, {"z": 0.0, "pitch": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.12, 1.0, "minjerk"),
                        (0.45, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.12, (-20.5, 20.5), "minjerk"),
                     (0.45, N, "minjerk"))))

# --- ンゴニ ------------------------------------------------------------------
CLIPS["ngoni/pluck"] = dict(
    name="ngoni_pluck", instrument="ngoni", placement="trigger",
    loop_beats=1.0, anchors=[0.15],
    description=(
        "ンゴニを弾く（エアー）。\n\n"
        "見分けの要点: **最も深く前傾し、最も短い周期で刻む**。小さな皮張りの胴を"
        "抱え込んで覗き込む姿勢はペルソナの構えが持つ。減衰が速い楽器なので"
        "打点の動きも短く切れる。\n\n"
        "コラと同じ撥弦だが、あちらは顔が正面で耳が主役。こちらは頭が深く落ちる。"),
    tracks=dict(
        head=head((0.0, {"z": 0.0, "pitch": 0.0}, "minjerk"),
                  (0.06, {"z": 1.6, "pitch": -2.0}, "minjerk"),
                  (0.15, {"z": -2.0, "pitch": 2.5}, "linear"),
                  (0.21, {"z": -2.4, "pitch": 3.0}, "minjerk"),
                  (0.5, {"z": 0.0, "pitch": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.06, -1.5, "minjerk"),
                        (0.15, 1.5, "minjerk"), (0.5, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.15, (-18.0, 18.0), "minjerk"),
                     (0.5, N, "minjerk"))))

# --- タマ --------------------------------------------------------------------
CLIPS["tama/strike"] = dict(
    name="tama_strike", instrument="tama", placement="trigger",
    loop_beats=1.0, anchors=[0.22],
    description=(
        "タマ（トーキングドラム）を曲がったバチで打つ（エアー）。\n\n"
        "見分けの要点: **傾きが主役で、耳が左右非対称**。ただし非対称も傾きも"
        "**構えの側**で作っている ―― 定常オフセットは速度を持たないので、"
        "動きで見せるより遥かに大きく取れる。ここは打点で roll が反対側へ返る"
        "ぶんだけを担当する（紐を締めて放す動作に対応）。\n\n"
        "カリニャンも傾くが、あちらは往復し続けて止まらない。こちらは打点で止まる。"),
    tracks=dict(
        head=head((0.0, {"roll": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk"),
                  (0.09, {"roll": -2.7, "z": 1.8, "pitch": -1.35}, "minjerk"),
                  (0.22, {"roll": 3.15, "z": -2.25, "pitch": 1.8}, "linear"),
                  (0.31, {"roll": 3.8, "z": -2.7, "pitch": 2.15}, "minjerk"),
                  (0.7, {"roll": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.09, -2.0, "minjerk"),
                        (0.22, 2.0, "minjerk"), (0.7, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.22, (-20.0, 20.0), "minjerk"),
                     (0.7, N, "minjerk"))))

# --- ジェンベ ----------------------------------------------------------------
CLIPS["djembe/hit"] = dict(
    name="djembe_hit", instrument="djembe", placement="trigger",
    loop_beats=1.0, anchors=[0.25],
    description=(
        "ジェンベを素手で打つ（エアー）。\n\n"
        "見分けの要点: **上下の振幅が最大**。バチではなく掌と指で打つ楽器なので、"
        "打点が最も深く落ち、跳ね返りも大きい。速度 = 振幅 ÷ 時間 なので、"
        "「一番深く落ちる」個性を残すために打点を後ろへ置いて時間を稼いでいる。\n\n"
        "dunun 2台もバチで打つが、あちらは遅く重い弧を描く。こちらは速く落ちて速く戻る。"),
    tracks=dict(
        head=head((0.0, {"z": 0.0, "pitch": 0.0}, "minjerk"),
                  (0.1, {"z": 4.0, "pitch": -4.0}, "minjerk"),
                  (0.25, {"z": -5.0, "pitch": 5.0}, "linear"),
                  (0.35, {"z": -6.0, "pitch": 5.8}, "minjerk"),
                  (0.8, {"z": 0.0, "pitch": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.1, -2.5, "minjerk"),
                        (0.25, 2.5, "minjerk"), (0.8, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.25, (-23.0, 23.0), "minjerk"),
                     (0.8, N, "minjerk"))))

# --- ドゥンドゥンバ ----------------------------------------------------------
CLIPS["dundunba/beat"] = dict(
    name="dundunba_beat", instrument="dundunba", placement="trigger",
    loop_beats=1.0, anchors=[0.25],
    description=(
        "ドゥンドゥンバ（最大の dunun）をバチで打つ（エアー）。\n\n"
        "見分けの要点: **最も遅く、最も低く、最も大きい弧**。両面の大太鼓を"
        "横向きに置いてバチで打つ楽器で、上体の重さが見えるほど大きく沈む。"
        "耳はほとんど動かさない ―― 動きの主役は胴体。\n\n"
        "サンバンと形は同じだが、向きも位相も逆で、周期は倍（8拍対4拍）。"
        "2台が向かい合って掛け合っているように見える。"),
    tracks=dict(
        head=head((0.0, {"y": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk"),
                  (0.09, {"y": -3.0, "z": 5.0, "pitch": -3.5}, "minjerk"),
                  (0.25, {"y": 3.0, "z": -6.0, "pitch": 4.0}, "linear"),
                  (0.34, {"y": 4.0, "z": -7.0, "pitch": 4.6}, "minjerk"),
                  (0.72, {"y": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.09, -3.0, "minjerk"),
                        (0.25, 3.0, "minjerk"), (0.72, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.25, (-15.0, 15.0), "minjerk"),
                     (0.72, N, "minjerk"))))

# --- サンバン ----------------------------------------------------------------
CLIPS["sangban/beat"] = dict(
    name="sangban_beat", instrument="sangban", placement="trigger",
    loop_beats=1.0, anchors=[0.2],
    description=(
        "サンバン（中型の dunun）をバチで打つ（エアー）。\n\n"
        "見分けの要点: **ドゥンドゥンバの鏡像**。同じ「バチで打つ」形だが、"
        "横移動の向きが逆で、周期は半分。アンサンブルの心臓と呼ばれるパートなので、"
        "ドゥンドゥンバより一段軽く、一段速い。\n\n"
        "3+3+2 という最も密な定型を叩くパートなので、打点の前後 0.55 拍で"
        "中立へ帰す。居残ると隣の打のぶんと足し算になる。"),
    tracks=dict(
        head=head((0.0, {"y": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk"),
                  (0.07, {"y": 2.6, "z": 3.6, "pitch": -3.0}, "minjerk"),
                  (0.2, {"y": -2.6, "z": -4.4, "pitch": 3.6}, "linear"),
                  (0.28, {"y": -3.4, "z": -5.0, "pitch": 4.1}, "minjerk"),
                  (0.55, {"y": 0.0, "z": 0.0, "pitch": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.07, 2.6, "minjerk"),
                        (0.2, -2.6, "minjerk"), (0.55, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.2, (-19.0, 19.0), "minjerk"),
                     (0.55, N, "minjerk"))))

# --- カリニャン --------------------------------------------------------------
CLIPS["karignan/scrape"] = dict(
    name="karignan_scrape", instrument="karignan", placement="pattern",
    loop_beats=1.0, anchors=[0.0, 0.5],
    description=(
        "カリニャン（溝を刻んだ金属管）を金属棒で擦る（エアー）。\n\n"
        "見分けの要点: **9台で唯一、止まる瞬間が無い**。擦ることそのものが発音なので"
        "打点という概念が無く、左右への往復が途切れない。**1往復＝2打**（往と復）"
        "なので、8分で鳴らしても動きは4分周期で済む ―― 速い刻みを安全に見せられる"
        "唯一のパートでもある。\n\n"
        "アンカーは y が 0 を通過する瞬間、つまり**最も速く動いている瞬間**に置いて"
        "ある。端で止まる瞬間ではない ―― 擦る音が出るのは動いている最中だから。\n\n"
        "ループ型なので周回の継ぎ目が中立で揃っている必要がある（先頭と末尾が同じ値）。"),
    tracks=dict(
        head=head((0.0, {"y": 0.0, "roll": 0.0, "z": 0.0}, "minjerk"),
                  (0.25, {"y": 4.4, "roll": 2.6, "z": -0.9}, "minjerk"),
                  (0.5, {"y": 0.0, "roll": 0.0, "z": 0.0}, "minjerk"),
                  (0.75, {"y": -4.4, "roll": -2.6, "z": -0.9}, "minjerk"),
                  (1.0, {"y": 0.0, "roll": 0.0, "z": 0.0}, "minjerk")),
        body_yaw=scalar((0.0, 0.0, "minjerk"), (0.25, 2.4, "minjerk"),
                        (0.75, -2.4, "minjerk"), (1.0, 0.0, "minjerk")),
        antennas=ant((0.0, N, "minjerk"), (0.25, (-16.0, 16.0), "minjerk"),
                     (0.5, N, "minjerk"), (0.75, (-16.0, 16.0), "minjerk"),
                     (1.0, N, "minjerk"))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="既存のクリップを上書きする（clip fit の調整が消える）")
    ap.add_argument("--out", default="clips")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    out = root / args.out
    existing = [k for k in CLIPS if (out / f"{k}.clip.json").exists()]
    if existing and not args.force:
        print("これらは既にある: " + ", ".join(existing), file=sys.stderr)
        print("clip fit / song fit の調整を消してよければ --force を付けて。",
              file=sys.stderr)
        return 1

    for key, spec in CLIPS.items():
        doc = {"name": spec["name"], "version": 1, "instrument": spec["instrument"],
               "description": spec["description"], "units": UNITS,
               "placement": spec["placement"], "loop_beats": spec["loop_beats"],
               "anchors": spec["anchors"], "tracks": spec["tracks"]}
        path = out / f"{key}.clip.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(f"  {key:<22} {spec['placement']:<8} anchors {spec['anchors']}")
    print(f"\n{len(CLIPS)} clips -> {out}")
    print("次: orchestra clip check --bpm 96 --bpm 120  →  orchestra clip fit --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
