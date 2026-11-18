#!/usr/bin/env python3
"""gm.dls から実楽器サンプルを抽出する（標準ライブラリ + numpy のみ）。

    python tools/extract_samples.py --list          中身を一覧する
    python tools/extract_samples.py                 必要な音を抽出する

なぜこれが要るか
----------------
ユーザは合成音ではなく**実録音**を選んだ。そして `C:\\Windows\\System32\\drivers\\gm.dls`
に Roland 音源の General MIDI サンプル集（3.28MB / 235楽器 / 495波形）があり、
必要な楽器がすべて実録音で入っている。ダウンロードも新規依存も要らない。

DLS は素の RIFF で、`wvpl` チャンクの中身は標準的な RIFF-WAVE の塊が並んでいるだけ。
各波形には `wsmp` チャンクが付いていて、**その録音が本来何の音程なのか**（unity note）と
微調整（fine tune）、ループ点が書いてある。この unity note が無いと、抽出した波形を
何半音ずらせばよいか分からない ―― サンプラーにとって波形と同じくらい重要な情報。

ライセンスについて
------------------
gm.dls は Microsoft 配布・Roland 制作。手元で鳴らすのと、抽出物を配布するのは別問題。
そのため抽出先は `samples/generated/`（.gitignore 済み）に固定し、リポジトリには
この抽出スクリプトだけを残す。公開演奏や配布を想定するなら別途判断が要る。

参考: DLS Level 1/2 仕様 (MMA), RIFF/WAVE 仕様
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DLS = Path(r"C:\Windows\System32\drivers\gm.dls")


# ---------------------------------------------------------------------------
# RIFF 読み取り
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    ident: str
    start: int          # データ先頭（ヘッダ8バイトの直後）
    size: int
    list_type: str = ""  # LIST/RIFF のときだけ

    @property
    def end(self) -> int:
        return self.start + self.size


def iter_chunks(buf: bytes, start: int, end: int):
    """[start, end) の範囲を RIFF チャンクとして走査する。

    チャンクは偶数境界に揃う（奇数サイズの後にパディング1バイト）。これを忘れると
    2つ目以降が全部ずれるので、RIFF を手で読むときの定番の落とし穴。
    """
    pos = start
    while pos + 8 <= end:
        ident = buf[pos : pos + 4].decode("ascii", "replace")
        (size,) = struct.unpack_from("<I", buf, pos + 4)
        data = pos + 8
        if data + size > end:
            size = end - data          # 壊れたファイルでも落ちないように
        c = Chunk(ident, data, size)
        if ident in ("LIST", "RIFF"):
            c.list_type = buf[data : data + 4].decode("ascii", "replace")
        yield c
        pos = data + size + (size & 1)   # ← パディング


def find(buf: bytes, start: int, end: int, ident: str, list_type: str = "") -> Chunk | None:
    for c in iter_chunks(buf, start, end):
        if c.ident == ident and (not list_type or c.list_type == list_type):
            return c
    return None


# ---------------------------------------------------------------------------
# DLS 構造
# ---------------------------------------------------------------------------


@dataclass
class Wave:
    index: int
    name: str = ""
    samplerate: int = 22050
    channels: int = 1
    bits: int = 16
    n_frames: int = 0
    unity_note: int = 60        # この波形が本来鳴らす MIDI ノート番号
    fine_tune_cents: float = 0.0
    loop_start: int = 0
    loop_length: int = 0
    has_loop: bool = False
    pcm_offset: int = 0
    pcm_bytes: int = 0

    @property
    def duration(self) -> float:
        return self.n_frames / self.samplerate if self.samplerate else 0.0


@dataclass
class Instrument:
    bank: int
    program: int
    name: str = ""
    wave_indices: list[int] = field(default_factory=list)

    @property
    def is_drum(self) -> bool:
        # DLS では bank の最上位ビットがドラムキットを表す
        return bool(self.bank & 0x80000000)

    @property
    def gm_program(self) -> int:
        return self.program + 1     # GM は 1 始まりで語られることが多い


class DLS:
    def __init__(self, path: Path):
        self.path = path
        self.buf = path.read_bytes()
        if self.buf[:4] != b"RIFF" or self.buf[8:12] != b"DLS ":
            raise ValueError(f"{path} is not a DLS file")
        (riff_size,) = struct.unpack_from("<I", self.buf, 4)
        self.root_start, self.root_end = 12, min(len(self.buf), 8 + riff_size)

        self.waves: list[Wave] = []
        self.instruments: list[Instrument] = []
        self.cues: list[int] = []
        self._parse()

    # -- 解析 --------------------------------------------------------------
    def _parse(self) -> None:
        b = self.buf
        # ptbl: 波形プール内の各波形へのオフセット表
        ptbl = find(b, self.root_start, self.root_end, "ptbl")
        wvpl = find(b, self.root_start, self.root_end, "LIST", "wvpl")
        if ptbl is None or wvpl is None:
            raise ValueError("no ptbl / wvpl chunk found")

        cb_size, n_cues = struct.unpack_from("<II", b, ptbl.start)
        self.cues = list(
            struct.unpack_from(f"<{n_cues}I", b, ptbl.start + cb_size)
        )
        pool_base = wvpl.start + 4       # 'wvpl' の4バイトを飛ばす

        for i, cue in enumerate(self.cues):
            pos = pool_base + cue
            if pos + 12 > len(b):
                continue
            (size,) = struct.unpack_from("<I", b, pos + 4)
            self.waves.append(self._parse_wave(i, pos + 12, pos + 8 + size))

        lins = find(b, self.root_start, self.root_end, "LIST", "lins")
        if lins is not None:
            for c in iter_chunks(b, lins.start + 4, lins.end):
                if c.ident == "LIST" and c.list_type == "ins ":
                    inst = self._parse_instrument(c)
                    if inst:
                        self.instruments.append(inst)

    def _parse_wave(self, index: int, start: int, end: int) -> Wave:
        b = self.buf
        w = Wave(index=index)
        for c in iter_chunks(b, start, end):
            if c.ident == "fmt ":
                (_tag, ch, sr, _bps, _align, bits) = struct.unpack_from(
                    "<HHIIHH", b, c.start
                )
                w.channels, w.samplerate, w.bits = ch, sr, bits
            elif c.ident == "data":
                w.pcm_offset, w.pcm_bytes = c.start, c.size
            elif c.ident == "wsmp":
                # unity note が無いと何半音ずらせばよいか分からない
                (cb, unity, fine, _gain, _opts, n_loops) = struct.unpack_from(
                    "<IHhiII", b, c.start
                )
                w.unity_note = unity
                w.fine_tune_cents = fine / 65536.0 * 100.0 if abs(fine) > 100 else float(fine)
                if n_loops:
                    lp = c.start + cb
                    (_cbl, _typ, ls, ll) = struct.unpack_from("<IIII", b, lp)
                    w.loop_start, w.loop_length, w.has_loop = ls, ll, True
            elif c.ident == "LIST" and c.list_type == "INFO":
                nm = find(b, c.start + 4, c.end, "INAM")
                if nm:
                    w.name = _cstr(b, nm.start, nm.size)
        if w.bits and w.channels:
            w.n_frames = w.pcm_bytes // (w.bits // 8 * w.channels)
        return w

    def _parse_instrument(self, ins: Chunk) -> Instrument | None:
        b = self.buf
        insh = find(b, ins.start + 4, ins.end, "insh")
        if insh is None:
            return None
        (_n_regions, bank, program) = struct.unpack_from("<III", b, insh.start)
        inst = Instrument(bank=bank, program=program)

        info = find(b, ins.start + 4, ins.end, "LIST", "INFO")
        if info:
            nm = find(b, info.start + 4, info.end, "INAM")
            if nm:
                inst.name = _cstr(b, nm.start, nm.size)

        lrgn = find(b, ins.start + 4, ins.end, "LIST", "lrgn")
        if lrgn:
            for c in iter_chunks(b, lrgn.start + 4, lrgn.end):
                if c.ident == "LIST" and c.list_type in ("rgn ", "rgn2"):
                    wlnk = find(b, c.start + 4, c.end, "wlnk")
                    if wlnk:
                        (_opts, _phase, _ch, idx) = struct.unpack_from(
                            "<HHII", b, wlnk.start
                        )
                        if idx not in inst.wave_indices:
                            inst.wave_indices.append(idx)
        return inst

    # -- 出力 --------------------------------------------------------------
    def wave_pcm(self, w: Wave) -> bytes:
        return self.buf[w.pcm_offset : w.pcm_offset + w.pcm_bytes]

    def write_wav(self, w: Wave, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(w.channels)
            out.setsampwidth(max(1, w.bits // 8))
            out.setframerate(w.samplerate)
            out.writeframes(self.wave_pcm(w))
        return path


def _cstr(buf: bytes, start: int, size: int) -> str:
    raw = buf[start : start + size]
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


# ---------------------------------------------------------------------------
# 欲しい楽器
# ---------------------------------------------------------------------------
# GM のプログラム番号（0始まり）。名前一致より番号のほうが確実。
WANTED_MELODIC = {
    "glockenspiel": 9,
    "celesta": 8,
    "music_box": 10,
    "vibraphone": 11,
    "marimba": 12,
    "xylophone": 13,
    "tubular_bells": 14,
    "choir_aahs": 52,      # ← 歌パート用。存在するか要確認
    "voice_oohs": 53,
    "synth_voice": 54,
    "warm_pad": 89,
    "halo_pad": 94,
    "string_ensemble": 48,
}

# 打楽器は GM ドラムキットのノート番号で拾う
WANTED_PERCUSSION = {
    "woodblock": 76,       # High Wood Block
    "tambourine": 54,
    "jingle_bell": 83,
    "maracas": 70,
    "cabasa": 69,
    "triangle": 81,        # Open Triangle
    "claves": 75,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dls", type=Path, default=DEFAULT_DLS)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent.parent / "samples" / "generated")
    ap.add_argument("--list", action="store_true", help="just list the contents")
    ap.add_argument("--grep", help="filter the listing by name")
    args = ap.parse_args()

    if not args.dls.exists():
        print(f"not found: {args.dls}", file=sys.stderr)
        return 1

    dls = DLS(args.dls)
    print(f"{args.dls}  ({args.dls.stat().st_size/1024/1024:.2f} MB)")
    print(f"  {len(dls.instruments)} instruments / {len(dls.waves)} waves\n")

    melodic = sorted((i for i in dls.instruments if not i.is_drum),
                     key=lambda i: i.program)
    drums = [i for i in dls.instruments if i.is_drum]

    if args.list:
        q = (args.grep or "").lower()
        print("=== melodic instruments (GM program) ===")
        for i in melodic:
            if q and q not in i.name.lower():
                continue
            print(f"  {i.program:>3}  {i.name:<26} waves={len(i.wave_indices)}")
        print(f"\n=== {len(drums)} drum kit(s) ===")
        for d in drums[:2]:
            print(f"  bank=0x{d.bank:08x} program={d.program} "
                  f"{d.name!r} regions={len(d.wave_indices)}")
        if q:
            print(f"\n=== waves whose name contains {q!r} ===")
            for w in dls.waves:
                if q in w.name.lower():
                    print(f"  [{w.index:>3}] {w.name:<12} {w.samplerate}Hz "
                          f"{w.channels}ch {w.bits}bit {w.duration:.3f}s "
                          f"unity={w.unity_note} loop={w.has_loop}")
        return 0

    # -- 抽出 -------------------------------------------------------------
    manifest: dict = {"source": str(args.dls), "melodic": {}, "percussion": {}}
    by_program = {i.program: i for i in melodic}
    n = 0

    for label, prog in WANTED_MELODIC.items():
        inst = by_program.get(prog)
        if inst is None:
            print(f"  --   {label:<18} no GM program {prog}")
            continue
        entries = []
        for k, wi in enumerate(inst.wave_indices):
            if wi >= len(dls.waves):
                continue
            w = dls.waves[wi]
            p = args.out / "melodic" / f"{label}_{k}.wav"
            dls.write_wav(w, p)
            entries.append({
                "file": str(p.relative_to(args.out)).replace("\\", "/"),
                "unity_note": w.unity_note,
                "fine_tune_cents": round(w.fine_tune_cents, 2),
                "samplerate": w.samplerate,
                "channels": w.channels,
                "bits": w.bits,
                "frames": w.n_frames,
                "duration": round(w.duration, 4),
                "loop": ({"start": w.loop_start, "length": w.loop_length}
                         if w.has_loop else None),
                "wave_name": w.name,
            })
            n += 1
        manifest["melodic"][label] = {"gm_program": prog, "name": inst.name,
                                      "zones": entries}
        print(f"  ok   {label:<18} {inst.name:<24} {len(entries)} zone")

    # ドラムキットは1つのインストゥルメントの中でノート番号ごとに領域が分かれる。
    # 領域→ノートの対応には rgnh が要るので、そこだけ改めて読む。
    if drums:
        kit = drums[0]
        note_map = _drum_note_map(dls, kit)
        for label, note in WANTED_PERCUSSION.items():
            wi = note_map.get(note)
            if wi is None or wi >= len(dls.waves):
                print(f"  --   {label:<18} no note {note}")
                continue
            w = dls.waves[wi]
            p = args.out / "percussion" / f"{label}.wav"
            dls.write_wav(w, p)
            manifest["percussion"][label] = {
                "file": str(p.relative_to(args.out)).replace("\\", "/"),
                "gm_note": note,
                "unity_note": w.unity_note,
                "samplerate": w.samplerate,
                "channels": w.channels,
                "bits": w.bits,
                "duration": round(w.duration, 4),
                "wave_name": w.name,
            }
            n += 1
            print(f"  ok   {label:<18} note {note:<4} {w.name:<10} "
                  f"{w.duration:.3f}s {w.samplerate}Hz")

    args.out.mkdir(parents=True, exist_ok=True)
    mpath = args.out / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nextracted {n} wave(s) into {args.out}, manifest in {mpath.name}")
    print("(the extracted audio is gitignored; only this script is committed)")
    return 0


def _drum_note_map(dls: DLS, kit: Instrument) -> dict[int, int]:
    """ドラムキットの「MIDIノート番号 -> 波形インデックス」を作る。

    メロディ楽器と違い、ドラムは1つのインストゥルメント内で領域(rgn)ごとに
    別の音が割り当たっている。どの領域がどのノートかは `rgnh` の音域に書いてある。
    """
    b = dls.buf
    out: dict[int, int] = {}
    for ins in iter_chunks(b, dls.root_start, dls.root_end):
        pass  # ルート走査は使わない。下で kit を直接探す。

    lins = find(b, dls.root_start, dls.root_end, "LIST", "lins")
    if lins is None:
        return out
    for c in iter_chunks(b, lins.start + 4, lins.end):
        if not (c.ident == "LIST" and c.list_type == "ins "):
            continue
        insh = find(b, c.start + 4, c.end, "insh")
        if insh is None:
            continue
        (_n, bank, program) = struct.unpack_from("<III", b, insh.start)
        if bank != kit.bank or program != kit.program:
            continue
        lrgn = find(b, c.start + 4, c.end, "LIST", "lrgn")
        if lrgn is None:
            continue
        for r in iter_chunks(b, lrgn.start + 4, lrgn.end):
            if not (r.ident == "LIST" and r.list_type in ("rgn ", "rgn2")):
                continue
            rgnh = find(b, r.start + 4, r.end, "rgnh")
            wlnk = find(b, r.start + 4, r.end, "wlnk")
            if rgnh is None or wlnk is None:
                continue
            (lo, hi) = struct.unpack_from("<HH", b, rgnh.start)
            (_o, _p, _ch, idx) = struct.unpack_from("<HHII", b, wlnk.start)
            for note in range(lo, hi + 1):
                out.setdefault(note, idx)
        break
    return out


if __name__ == "__main__":
    raise SystemExit(main())
