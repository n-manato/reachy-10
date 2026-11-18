#!/usr/bin/env python3
"""外から持ってきた MIDI を、この曲が読める形に直して取り込む。

    python tools/import_midi.py 拾ってきた.mid --song songs/small_world \\
        --map 1=Melody 2=Harmony 4=Counter 5=Bass

`song.yaml` の各パートは **トラック名** でどの旋律を弾くかを決める
（`orchestra/music/midi.py` の `load_midi` がトラック名で束縛する）。
ところが世に出回っている MIDI はトラック名が無いか、"ROL2MID conversion"
のような変換器の署名が入っているだけのことが多い。ここで付け直す。

**トラック番号は自分で見て決めること。** 決め打ちにすると、別のファイルを
入れたときに黙って違う旋律が鳴る ―― 音が出ているだけに気づきにくい。
`--list` で中身を出してから決める:

    python tools/import_midi.py 拾ってきた.mid --list

`--dry-run` は書き込まずに、取り込んだ結果を小節単位で表示する。実際に
差し替える前に「本当にその旋律か」を目で確かめるために使う。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mido  # noqa: E402

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# General MIDI のプログラム番号 -> 名前。どのトラックが何かを推測する助けに
# なるだけで、判断そのものは人がやる。
GM = {
    0: "Acoustic Grand", 11: "Vibraphone", 12: "Marimba", 13: "Xylophone",
    14: "Tubular Bells", 32: "Acoustic Bass", 33: "Finger Bass",
    35: "Fretless Bass", 36: "Slap Bass 1", 48: "String Ensemble",
    52: "Choir Aahs", 53: "Voice Oohs", 54: "Synth Voice", 56: "Trumpet",
    65: "Alto Sax", 71: "Clarinet", 72: "Piccolo", 73: "Flute",
    80: "Lead Square", 81: "Lead Saw", 85: "Lead Voice", 86: "Lead Fifths",
    88: "Pad New Age", 89: "Pad Warm",
}


def note_name(p: int) -> str:
    return f"{NAMES[p % 12]}{p // 12 - 1}"


def scan(mid: mido.MidiFile) -> list[dict]:
    """各トラックの中身を要約する。"""
    tpb = mid.ticks_per_beat or 480
    out = []
    for i, track in enumerate(mid.tracks):
        name, prog, notes, chans = "", set(), [], set()
        t = 0
        tempos = []
        for msg in track:
            t += msg.time
            if msg.type == "track_name" and not name:
                name = str(msg.name).strip()
            elif msg.type == "program_change":
                prog.add(msg.program)
            elif msg.type == "set_tempo":
                tempos.append(60_000_000.0 / msg.tempo)
            elif msg.type == "note_on" and msg.velocity > 0:
                notes.append((t / tpb, msg.note))
                chans.add(msg.channel)
        out.append({
            "index": i, "name": name, "prog": sorted(prog), "notes": notes,
            "channels": sorted(chans), "tempos": tempos,
            "lo": min((p for _, p in notes), default=0),
            "hi": max((p for _, p in notes), default=0),
            "end_beat": max((b for b, _ in notes), default=0.0),
        })
    return out


def report(info: list[dict], tpb: int) -> None:
    print(f"  ticks_per_beat={tpb}")
    for tr in info:
        if not tr["notes"]:
            tempo = f"  tempo={[round(x, 1) for x in tr['tempos'][:4]]}" if tr["tempos"] else ""
            print(f"  [{tr['index']:>2}] {tr['name'][:24]:<24} (no notes){tempo}")
            continue
        inst = ", ".join(GM.get(p, f"prog{p}") for p in tr["prog"]) or "-"
        print(f"  [{tr['index']:>2}] {tr['name'][:24]:<24} {len(tr['notes']):>4} notes  "
              f"{note_name(tr['lo'])}-{note_name(tr['hi']):<4}  "
              f"~{tr['end_beat'] / 4:.0f} bars  {inst}")


def preview(mid: mido.MidiFile, index: int, bars: int = 8) -> None:
    """1トラックを小節ごとに書き出す。旋律が本物かを目で確かめるため。"""
    tpb = mid.ticks_per_beat or 480
    tr = scan(mid)[index]
    grouped: dict[int, list] = {}
    for beat, pitch in tr["notes"]:
        grouped.setdefault(int(beat // 4), []).append((beat % 4, pitch))
    for bar in sorted(grouped)[:bars]:
        items = " ".join(f"{b:g}:{note_name(p)}" for b, p in sorted(grouped[bar]))
        print(f"    bar {bar + 1:>3} | {items}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="the MIDI file to import")
    ap.add_argument("--song", default="songs/small_world")
    ap.add_argument("--map", nargs="*", default=[],
                    metavar="N=NAME", help="track number = new name")
    ap.add_argument("--list", action="store_true", help="just show the contents")
    ap.add_argument("--preview", type=int, metavar="N",
                    help="print that track bar by bar")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--drop-unmapped", action="store_true",
                    help="drop tracks you did not name")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"error: {src} does not exist", file=sys.stderr)
        return 1
    mid = mido.MidiFile(str(src))
    tpb = mid.ticks_per_beat or 480

    print(f"{src.name}: format={mid.type} tracks={len(mid.tracks)}")
    report(scan(mid), tpb)

    if args.preview is not None:
        print(f"\n  --- track {args.preview} ---")
        preview(mid, args.preview)
    if args.list:
        return 0

    if not args.map:
        print("\nName at least one track with --map (e.g. --map 1=Melody). "
              "song.yaml binds parts by track name.",
              file=sys.stderr)
        return 1

    mapping: dict[int, str] = {}
    for item in args.map:
        if "=" not in item:
            print(f"error: --map takes the form N=NAME, got {item!r}", file=sys.stderr)
            return 1
        n, name = item.split("=", 1)
        try:
            idx = int(n)
        except ValueError:
            print(f"error: track number is not a number: {n!r}", file=sys.stderr)
            return 1
        if not 0 <= idx < len(mid.tracks):
            print(f"error: no track {idx} (valid: 0..{len(mid.tracks) - 1})",
                  file=sys.stderr)
            return 1
        mapping[idx] = name

    out = mido.MidiFile(type=mid.type, ticks_per_beat=tpb)
    kept = []
    for i, track in enumerate(mid.tracks):
        has_notes = any(m.type == "note_on" and m.velocity > 0 for m in track)
        if args.drop_unmapped and has_notes and i not in mapping:
            continue
        new = mido.MidiTrack()
        named = i in mapping
        if named:
            new.append(mido.MetaMessage("track_name", name=mapping[i], time=0))
        for msg in track:
            # 元の名前は捨てる。2つ入っていると load_midi は最初の1つを採る。
            if msg.type == "track_name" and named:
                continue
            new.append(msg)
        out.tracks.append(new)
        if named:
            kept.append(f"{i} -> {mapping[i]!r}")

    print("\nrenamed tracks:")
    for k in kept:
        print(f"  {k}")

    dest = Path(args.song) / f"{Path(args.song).name}.mid"
    if args.dry_run:
        print(f"\n(--dry-run: nothing written. It would go to {dest})")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(dest))
    print(f"\nwrote: {dest}")

    # 書いたものを、実際に曲を読む経路でそのまま読み直して確認する。
    # 「書けた」と「この曲として読める」は別のこと。
    from orchestra.music.midi import load_midi

    tl = load_midi(dest)
    tracks: dict[str, int] = {}
    for e in tl.events:
        tracks[e.track] = tracks.get(e.track, 0) + 1
    print(f"re-read: {tl.n_bars} bars / {tl.duration_s:.1f}s / {tl.tempo!r}")
    for name, n in sorted(tracks.items(), key=lambda kv: -kv[1]):
        ps = [e.pitch for e in tl.events if e.track == name]
        print(f"  {name:<12} {n:>4} notes  {note_name(min(ps))}-{note_name(max(ps))}")
    print("\nThe `track:` field in song.yaml has to match these names. "
          "A part that does not match stays silent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
