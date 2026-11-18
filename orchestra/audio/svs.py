"""歌声合成（DiffSinger）で歌わせる。

なぜ喋りの加工をやめたか
------------------------
ここまで2回、**喋った音声を歌に変換する**方向で作った。

  1. 音節ごとの SAPI + LPC 再合成 → 論外
  2. 行まるごとのニューラル TTS（子供の声）+ WORLD → 数値は全部緑、でも駄目

2番目は音程誤差 中央 −0.1 cent、長さ誤差 0.0 ms、素材は実測 f0 中央 295Hz の
子供の声、と測れるものは全部良かった。それでも歌に聞こえない。**喋りと歌の
差は音程と長さではない**からで、そこを合わせても足りない。歌には歌の発声が
あり、それは録音された発話の中に存在しない。

だから**最初から歌うモデル**に変える。DiffSinger は歌唱データで学習した
音響モデルで、音素列と音程を与えると歌のメルスペクトログラムを出す。喋りを
曲げるのではないので、上の壁が構造的に無い。

必要なもの
----------
`tools/fetch_singer_model.py` が取得する。**再配布不可なのでリポジトリには
入らない**（`gm.dls` と同じ扱い）。

    models/diffsinger/tiger/lib/
      dsacoustic/acoustic.onnx      音素+音程 -> メル
      dsacoustic/phonemes.txt       116種
      dsacoustic/*.emb              話者埋め込み（声色7種）
      dsvocoder/tgm_hifigan.onnx    メル -> 波形（44.1kHz）
      dsdur/dsdict-en.yaml          英語辞書 10075語

音長と音程の予測器（`dsdur` / `dspitch`）は**使わない**。どちらも「楽譜から
歌い方を推測する」ためのもので、こちらは既に楽譜から正確な音価と音程を
知っている。推測させると、揃えたはずのタイミングが崩れる。

推論の契約（モデルから直接読んだもの）
--------------------------------------
    acoustic: tokens[1,N] durations[1,N] f0[1,T] gender[1,T] velocity[1,T]
              spk_embed[1,T,256] depth[] steps[]  ->  mel[1,T,128]
    vocoder : mel[1,T,128] f0[1,T]  ->  waveform[1,S]

`T = sum(durations)`。1フレームは hop 512 / 44100 Hz = 11.61 ms。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("orchestra.audio.svs")

DEFAULT_ROOT = Path("models/diffsinger/nessie")

# --- 音素の分類 ------------------------------------------------------------
# ARPAbet は言語学の側の決まりで、バンクごとに変わるものではない。バンク同梱の
# 記号表を読むより、こちらで持つほうが移植性が高い ―― 実際、Tiger は記号種別を
# YAML に持ち、Nessie は持っていない。
ARPA_VOWELS = frozenset(
    "aa ae ah ao aw ax ay eh er ey ih iy ow oy uh uw".split())

# 子音の標準的な長さ（秒）。種別ごとに変える ―― 破裂音は短く、摩擦音は長い。
# 一律にすると "s" が足りず "t" が余る。
CONSONANT_S: dict[str, float] = {}
for _phs, _d in (("b d g k p t dx q", 0.055),        # 破裂
                 ("ch jh", 0.090),                    # 破擦
                 ("f v th dh s z sh zh hh", 0.085),   # 摩擦
                 ("m n ng", 0.065),                   # 鼻音
                 ("l r", 0.055),                      # 流音
                 ("w y", 0.050)):                     # 半母音
    CONSONANT_S.update({p: _d for p in _phs.split()})
CONSONANT_DEFAULT = 0.060

# 発音辞書の綴りの揺れ。CMUdict は使わないが、バンク同梱辞書を使うときに要る
# ―― Tiger の英語辞書は 10075 語のうち6語が `h` を使うが、`h` は音響モデルの
# 音素表にも辞書自身の記号定義にも無い（`hh` の綴り間違い）。
PHONEME_ALIASES: dict[str, str] = {"h": "hh"}


class Lexicon:
    """単語を ARPAbet の音素列にする。

    **バンク同梱の辞書は使わない。** 理由が2つある:

      * 入っていないバンクがある。Nessie の `dictionary-en.txt` は42行しかなく、
        単語辞書ではなく記号表。単語→音素は OpenUtau のプラグイン側にある
      * 入っていても穴と誤りがある。Tiger の辞書は 10075 語だが活用形に弱く
        （laughter / tears / hopes / fears が引けない）、6語が存在しない音素を
        指していた

    代わりに **CMUdict**（カーネギーメロン大の発音辞書、パブリックドメイン、
    13万語）を使う。バンクに依存しないので、モデルを差し替えても同じ発音に
    なる。実測で歌詞21語すべて、ARPAbet 39音素すべてが載る。
    """

    def __init__(self, extra: dict[str, tuple[str, ...]] | None = None):
        import cmudict

        self._d = cmudict.dict()
        self._extra = {k.lower(): tuple(v) for k, v in (extra or {}).items()}

    @staticmethod
    def _bare(p: str) -> str:
        """CMUdict の強勢記号を落とす（`EY1` -> `ey`）。"""
        return p.rstrip("0123456789").lower()

    def phonemes(self, word: str) -> tuple[str, ...]:
        w = word.lower().strip(".,!?;:\"'")
        if w in self._extra:
            return self._extra[w]
        v = self._d.get(w)
        if not v:
            raise KeyError(
                f"no pronunciation for {word!r}\n"
                f"  Not in CMUdict. Supply ARPAbet via `Lexicon(extra=...)`.")
        return tuple(self._bare(p) for p in v[0])


class VoicebankMissing(FileNotFoundError):
    """モデルが置かれていない。"""


class QuietOutput(RuntimeError):
    """合成は成功したが、実質無音だった。"""


# ---------------------------------------------------------------------------
# ボイスバンク
# ---------------------------------------------------------------------------


@dataclass
class Voicebank:
    """1つの歌声モデル。**形式が2系統あるので、そこを吸収する。**

    Tiger (v106) と LUNAI (v170) は同じ DiffSinger でも中身の置き方が違う:

    |            | Tiger                | Nessie / LUNAI             |
    |------------|----------------------|----------------------------|
    | 設定       | ルートの dsconfig    | `configs/dsconfig.yaml`    |
    | 音素表     | `phonemes.txt`       | `phonemes.json`（言語別）  |
    | 埋め込み   | 256次元              | 384次元                    |
    | mel の底   | 10                   | e                          |
    | 追加入力   | なし                 | `languages`, `tension`     |

    形の違いはここで吸収し、**推論側はモデルが宣言した入力名から組み立てる**
    （`Engine.render`）。そうしておけば、次のバンクで入力が1つ増えても
    ここだけで済む。
    """

    root: Path
    name: str
    phonemes: list[str]
    token: dict[str, int]
    speakers: dict[str, np.ndarray]
    acoustic: Path
    vocoder: Path
    emb_dim: int = 256
    mel_gain: float = 1.0        # メルの底が音響とボコーダで違うときの補正
    lang: str = "en"
    lang_id: int | None = None
    sample_rate: int = 44100
    hop_size: int = 512
    num_mel_bins: int = 128
    max_depth: float = 0.6

    @property
    def frame_s(self) -> float:
        return self.hop_size / self.sample_rate

    @staticmethod
    def is_vowel(ph: str) -> bool:
        return ph in ARPA_VOWELS

    def resolve(self, ph: str) -> int:
        """ARPAbet の裸名を、このモデルの音素番号にする。

        新しい形式は音素を言語で名前空間に分ける（`en/ae`）。ただし言語に
        依らないものは裸のまま入っている（`hh` `cl` `SP` `AP`）。両方試す。
        """
        for cand in (f"{self.lang}/{ph}", ph):
            if cand in self.token:
                return self.token[cand]
        raise KeyError(
            f"phoneme {ph!r} is unknown to the model ({self.name}). "
            f"The dictionary and the phoneme table disagree")

    def knows(self, ph: str) -> bool:
        return f"{self.lang}/{ph}" in self.token or ph in self.token

    # -- 読み込み ------------------------------------------------------------

    @staticmethod
    def load(root: str | Path = DEFAULT_ROOT, lang: str = "en",
             vocoder: str | Path | None = None) -> "Voicebank":
        root = Path(root)
        if not root.exists():
            raise VoicebankMissing(
                f"no singing model at {root}\n"
                "  Fetch it with `python tools/fetch_singer_model.py`. It is "
                "not in the repository because it may not be redistributed.")
        import yaml

        cfg_path = next((p for p in (root / "dsconfig.yaml",
                                     root / "configs" / "dsconfig.yaml")
                         if p.exists()), None)
        if cfg_path is None:
            found = list(root.rglob("dsconfig.yaml"))
            cfg_path = min(found, key=lambda p: len(p.parts)) if found else None
        if cfg_path is None:
            raise VoicebankMissing(f"no dsconfig.yaml under {root}")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        base = cfg_path.parent            # 設定内の相対パスはここが起点

        ph_path = (base / cfg["phonemes"]).resolve()
        if ph_path.suffix == ".json":
            raw = json.loads(ph_path.read_text(encoding="utf-8"))
            # dict なら {名前: 番号}、list なら並び順が番号
            token = ({k: int(v) for k, v in raw.items()} if isinstance(raw, dict)
                     else {p: i for i, p in enumerate(raw)})
            phonemes = [k for k, _ in sorted(token.items(), key=lambda kv: kv[1])]
        else:
            phonemes = ph_path.read_text(encoding="utf-8").split()
            token = {p: i for i, p in enumerate(phonemes)}

        lang_id = None
        if cfg.get("languages"):
            langs = json.loads((base / cfg["languages"]).resolve()
                               .read_text(encoding="utf-8"))
            lang_id = int(langs[lang]) if lang in langs else None

        # ボコーダ。`vocoder=` を渡せばバンク同梱のものを差し替えられる ――
        # メルから波形を作る最後の一段で、「シュー」という雑音の出どころは
        # ほぼここ。バンクによって出来が違うので、取り替えられるようにする。
        voc_dir = (Path(vocoder) if vocoder
                   else (base / cfg.get("vocoder", "dsvocoder")).resolve())
        if voc_dir.is_file():                      # onnx を直接指された
            voc, voc_model = {}, voc_dir
        else:
            voc = yaml.safe_load((voc_dir / "vocoder.yaml").read_text(encoding="utf-8"))
            voc_model = (voc_dir / voc["model"]).resolve()
            if not voc_model.exists():             # 別ディレクトリに置く形もある
                hit = next(iter(voc_dir.parent.rglob(Path(voc["model"]).name)),
                           None) or next(iter(root.rglob(Path(voc["model"]).name)), None)
                if hit is None:
                    raise VoicebankMissing(f"vocoder not found: {voc['model']}")
                voc_model = hit
        # ★メルの底が食い違うと、破綻せずに濁る★ log10 と自然対数で ln10=2.303
        # 倍ずれる。この生態系で実際に起きている問題なので、宣言を突き合わせる。
        acoustic_base = str(cfg.get("mel_base", "e")).lower()
        voc_base = str(voc.get("mel_base", acoustic_base)).lower()
        mel_gain = 1.0
        if acoustic_base != voc_base:
            mel_gain = (np.log(10.0) if acoustic_base in ("10", "log10")
                        else 1.0 / np.log(10.0))
            log.warning("mel log base differs: acoustic %s / vocoder %s -> x%.3f",
                        acoustic_base, voc_base, mel_gain)

        speakers: dict[str, np.ndarray] = {}
        for rel in cfg.get("speakers", []):
            p = (base / f"{rel}.emb").resolve()
            if p.exists():
                speakers[Path(rel).name] = np.fromfile(p, dtype=np.float32)
        if not speakers:
            raise VoicebankMissing(f"no speaker embeddings at all under {root}")
        dims = {len(e) for e in speakers.values()}
        if len(dims) != 1:
            raise VoicebankMissing(f"speaker embeddings have mismatched sizes: {dims}")

        vb = Voicebank(
            root=root, name=root.name, phonemes=phonemes, token=token,
            speakers=speakers, acoustic=(base / cfg["acoustic"]).resolve(),
            vocoder=voc_model, mel_gain=float(mel_gain),
            emb_dim=dims.pop(), lang=lang, lang_id=lang_id,
            sample_rate=int(voc.get("sample_rate", cfg.get("sample_rate", 44100))),
            hop_size=int(voc.get("hop_size", cfg.get("hop_size", 512))),
            num_mel_bins=int(voc.get("num_mel_bins", cfg.get("num_mel_bins", 128))),
            max_depth=float(cfg.get("max_depth", 0.6)),
        )
        missing = [p for p in sorted(ARPA_VOWELS | set(CONSONANT_S)) if not vb.knows(p)]
        if missing:
            log.warning("%s: ARPAbet phonemes the model does not have: %s", vb.name, missing)
        return vb

    # -- 音素 ----------------------------------------------------------------

    def syllabify(self, phones: tuple[str, ...], n: int) -> list[list[str]]:
        """音素列を n 音節に割る。

        母音の位置で切る。母音と母音の間の子音は**後ろの音節の頭に寄せる**
        （最大頭子音則）。2つあるときは1つずつ分ける ―― これで
        laughter は [l ae f][t er]、aware は [ax][w eh r] になる。
        """
        vpos = [i for i, p in enumerate(phones) if self.is_vowel(p)]
        if len(vpos) != n:
            raise ValueError(
                f"{len(vpos)} vowel(s) but {n} syllable(s): {' '.join(phones)}")
        if n == 1:
            return [list(phones)]
        out: list[list[str]] = []
        start = 0
        for k in range(n - 1):
            gap = phones[vpos[k] + 1:vpos[k + 1]]        # 母音間の子音
            coda = len(gap) // 2                          # 半分は前の音節の尻へ
            cut = vpos[k] + 1 + coda
            out.append(list(phones[start:cut]))
            start = cut
        out.append(list(phones[start:]))
        return out

    def line_syllables(self, line, lex: "Lexicon") -> list[list[str]]:
        """歌詞の1行を、音節ごとの音素列にする。

        `Line.words` の音節の切り方（表記上のもの）を、音素の側でも同じ数に
        揃える。ここがずれると音節と音符の対応が崩れる。
        """
        words = line.text.replace(",", " ").replace(".", " ").split()
        if len(words) != len(line.words):
            raise ValueError(f"word count does not match: {line.text!r}")
        out: list[list[str]] = []
        for w, syls in zip(words, line.words):
            out.extend(self.syllabify(lex.phonemes(w), len(syls)))
        return out


# ---------------------------------------------------------------------------
# 歌い方
# ---------------------------------------------------------------------------


@dataclass
class SvsStyle:
    """1人ぶんの設定。2人が同じだと2人に聞こえない。"""

    speaker: str = "tiger_fresh"
    speaker_mix: tuple[str, float] | None = None   # 別の声色を混ぜる

    # ★gender は 0 から動かさないこと★ 「フォルマントを上げて幼くする」
    # つまみのつもりで使ったが、このバンクでは**出力が崩壊する**。実測:
    #     0.00 -> rms 0.131      -0.20 -> 0.047
    #    -0.45 -> 0.001（無音）  -1.00 -> 0.0003
    # 例外も警告も出ないまま、ただ静かになる。学習時の augmentation の
    # 効き方がこの軸だけ違うのだと思われる。声を作り分けるなら speaker/
    # speaker_mix を使う。
    gender: float = 0.0
    velocity: float = 1.0
    # 以下はバンクによって受け付けたり受け付けなかったりする。宣言された
    # ものだけが実際に渡る（`Engine.render` を参照）。
    tension: float = 0.0
    energy: float = 0.0
    breathiness: float = 0.0
    voicing: float = 0.0
    steps: int = 20            # reflow の反復回数。多いほど良く、遅い
    depth: float = 0.6
    gain: float = 1.0

    # 歌い回しをバンクの予測器に任せる。切ると下の自前パラメータで作るが、
    # **規則的すぎて機械に聞こえる**（そのために付いているモデルなので、
    # 切るのは比較のときだけにすること）。
    use_variance: bool = True
    pitch_steps: int = 20
    expressiveness: float = 1.0    # 予測された抑揚をどれだけ効かせるか

    # ★tension の予測値は使わない★ 音長と音程の予測は効いたが、tension だけは
    # 逆効果だった。変数モデルが返す値は実測 -9.27 .. +4.91 で、**67% の
    # フレームが [-1,1] の外**にいる。この種の制御量は正規化されている前提の
    # はずで、学習時に見ていない範囲を音響モデルに入れると、破綻はしないが
    # 音が濁る。聴き比べでも定数 0 が最良だった。
    #   'predicted' … 変数モデルの出力をそのまま（濁る）
    #   'clip'      … [-1,1] に丸める
    #   'off'       … 定数 `tension`（既定。いちばん澄む）
    tension_source: str = "off"

    # 音程の付け方（`singer.pitch_line` にそのまま渡す）
    vibrato_hz: float = 5.4
    vibrato_cents: float = 30.0
    vibrato_onset_s: float = 0.30
    vibrato_ramp_s: float = 0.25
    portamento_s: float = 0.045
    scoop_cents: float = 45.0
    scoop_s: float = 0.06
    drift_cents: float = 7.0
    detune_cents: float = 0.0
    seed: int = 1

    def embedding(self, vb: Voicebank) -> np.ndarray:
        if self.speaker not in vb.speakers:
            raise KeyError(f"unknown voice {self.speaker!r}. "
                           f"Available: {', '.join(sorted(vb.speakers))}")
        e = vb.speakers[self.speaker].astype(np.float32)
        if self.speaker_mix:
            other, w = self.speaker_mix
            if other not in vb.speakers:
                raise KeyError(f"unknown voice {other!r}")
            e = (1 - w) * e + w * vb.speakers[other].astype(np.float32)
        return e


# 声色の名前はバンクごとに違う。既定は Nessie（cute / soft / standard）。
#
# 声色の選択は好みではなく**実測で決めた**。持続母音の 4-8kHz（「シュー」の
# 帯域）を測ると、同じ行でもはっきり差が出る:
#     standard 0.435%   cute 0.764%   soft 1.335%
# `soft` は名前に反していちばん粗い。主旋律は言葉を運ぶので、いちばん静かな
# `standard` を当てる。
VOICE_STYLES: dict[str, SvsStyle] = {
    "voice_lead": SvsStyle(speaker="standard", seed=1, steps=50, pitch_steps=30,
                           vibrato_hz=5.6, vibrato_cents=32.0, scoop_cents=50.0),
    # ハモリ。別の声色にして音程も少しずらす。同じ設定の2声は融けて
    # 1人に聞こえる（gender が使えないぶん、ここで離すしかない）。
    "voice_harmony": SvsStyle(speaker="cute", seed=2, detune_cents=-4.0,
                              steps=50, pitch_steps=30,
                              vibrato_hz=4.7, vibrato_cents=20.0, scoop_cents=30.0),
}

# Tiger を使うとき用。`--voicebank` で切り替えたら合わせて差し替える。
TIGER_STYLES: dict[str, SvsStyle] = {
    "voice_lead": SvsStyle(speaker="tiger_fresh", seed=1),
    "voice_harmony": SvsStyle(speaker="tiger_mystic",
                              speaker_mix=("tiger_royal", 0.35),
                              seed=2, detune_cents=-4.0),
}


def styles_for(vb: Voicebank) -> dict[str, SvsStyle]:
    """そのバンクに実在する声色だけを使うプリセットを返す。"""
    for table in (VOICE_STYLES, TIGER_STYLES):
        if all(s.speaker in vb.speakers for s in table.values()):
            return table
    names = sorted(vb.speakers)
    log.warning("%s: no default voice, using %s", vb.name, names[:2])
    return {"voice_lead": SvsStyle(speaker=names[0], seed=1),
            "voice_harmony": SvsStyle(speaker=names[min(1, len(names) - 1)],
                                      seed=2, detune_cents=-4.0)}

# これを下回ったら「鳴っていない」とみなす。gender の件のように、モデルは
# 静かな出力を**エラー無しで**返してくる。黙って無音を出荷しないための番人。
QUIET_RMS = 0.005


# ---------------------------------------------------------------------------
# 音素の並べ方
# ---------------------------------------------------------------------------


@dataclass
class Layout:
    """1行ぶんの音素列と、そのフレーム数。"""

    phones: list[str]
    frames: np.ndarray            # 各音素のフレーム数 (int64)
    t0: float                     # 出力先頭に対応する曲中の時刻
    vowel_frame: list[int]        # 音節ごとの母音開始フレーム
    note_hz: np.ndarray           # フレームごとの目標音程（0 は休符）

    @property
    def total(self) -> int:
        return int(self.frames.sum())


def layout_line(vb: Voicebank, syllables: list[list[str]], starts: np.ndarray,
                ends: np.ndarray, pitches: np.ndarray, lead_s: float = 0.30,
                tail_s: float = 0.30) -> Layout:
    """音節ごとの目標時刻から、音素の境界を決める。

    歌の約束は「**母音が拍に乗る**」こと。子音は拍の前に置く ―― 人はそう
    歌うし、そうしないと「タン」が「ンタ」に聞こえる。だから各音節の母音を
    目標時刻ちょうどに置き、頭の子音はその手前から取る。

    子音に持っていかれて母音が痩せないよう、子音の塊は使える時間の半分
    までに抑える。速い音符では子音のほうが縮む ―― 実際の歌でもそうなる。
    """
    n = len(syllables)
    if n == 0 or len(starts) != n or len(ends) != n or len(pitches) != n:
        raise ValueError("syllable count does not match the target count")

    heads, cores, tails = [], [], []
    for syl in syllables:
        vi = next((i for i, p in enumerate(syl) if vb.is_vowel(p)), None)
        if vi is None:
            raise ValueError(f"syllable with no vowel: {' '.join(syl)}")
        heads.append(syl[:vi])
        cores.append(syl[vi])
        tails.append(syl[vi + 1:])

    def nominal(p: str) -> float:
        return CONSONANT_S.get(p, CONSONANT_DEFAULT)

    def block(phs: list[str]) -> float:
        return sum(nominal(p) for p in phs)

    # (音素, 終了時刻, 音程Hz) の並び。音程 0 は休符。
    seg: list[tuple[str, float, float]] = []

    def spread(phs: list[str], t_from: float, t_to: float, hz: float) -> None:
        """子音の塊を [t_from, t_to) に、標準の長さの比で割り振る。"""
        if not phs:
            return
        w = np.array([nominal(p) for p in phs], dtype=float)
        w = w / w.sum() * max(t_to - t_from, 0.0)
        t = t_from
        for p, d in zip(phs, w):
            t += d
            seg.append((p, t, hz))
        seg[-1] = (seg[-1][0], t_to, hz)

    def hz_of(m: float) -> float:
        return 440.0 * 2.0 ** ((m - 69) / 12.0)

    t_begin = float(starts[0]) - lead_s
    # 行頭: 無音 -> 最初の頭子音 -> 最初の母音
    h0 = min(block(heads[0]), lead_s * 0.6)
    seg.append(("SP", float(starts[0]) - h0, 0.0))
    spread(heads[0], float(starts[0]) - h0, float(starts[0]), hz_of(pitches[0]))

    for i in range(n):
        hz = hz_of(pitches[i])
        v_start = float(starts[i])
        rest = (i + 1 < n) and (float(starts[i + 1]) - float(ends[i]) > 0.02)

        if i + 1 < n and not rest:
            # レガート: 母音 -> [尻子音 + 次の頭子音] -> 次の母音
            blk = tails[i] + heads[i + 1]
            want = min(block(blk), (float(starts[i + 1]) - v_start) * 0.5)
            v_end = float(starts[i + 1]) - want
            seg.append((cores[i], v_end, hz))
            spread(tails[i], v_end, v_end + want * _share(tails[i], blk, nominal), hz)
            spread(heads[i + 1], v_end + want * _share(tails[i], blk, nominal),
                   float(starts[i + 1]), hz_of(pitches[i + 1]))
        else:
            # 音符の終わりで切る。尻子音はその中に収める
            want = min(block(tails[i]), (float(ends[i]) - v_start) * 0.5)
            v_end = float(ends[i]) - want
            seg.append((cores[i], v_end, hz))
            spread(tails[i], v_end, float(ends[i]), hz)
            if rest:
                nh = min(block(heads[i + 1]),
                         (float(starts[i + 1]) - float(ends[i])) * 0.8)
                seg.append(("SP", float(starts[i + 1]) - nh, 0.0))
                spread(heads[i + 1], float(starts[i + 1]) - nh,
                       float(starts[i + 1]), hz_of(pitches[i + 1]))

    seg.append(("SP", float(ends[-1]) + tail_s, 0.0))

    # 時刻をフレームに量子化する。**累積してから差を取る**ので丸め誤差が
    # 溜まらず、合計は必ずフレーム境界にちょうど一致する。
    t_end = np.array([t for _, t, _ in seg], dtype=np.float64)
    acc = np.round((t_end - t_begin) / vb.frame_s).astype(np.int64)
    acc = np.maximum.accumulate(np.maximum(acc, 0))
    frames = np.diff(acc, prepend=0)

    # 長さ0になった音素は落とす。速い音符で子音が詰まると出る。
    keep = frames > 0
    phones = [p for (p, _, _), k in zip(seg, keep) if k]
    hz_seq = np.array([h for (_, _, h), k in zip(seg, keep) if k])
    frames = frames[keep]
    if not len(phones):
        raise ValueError("no phonemes survived -- the note values are too short")

    starts_f = np.concatenate([[0], np.cumsum(frames)])[:-1]
    note_hz = np.repeat(hz_seq, frames)
    vowel_frame = [int(starts_f[i]) for i, p in enumerate(phones)
                   if vb.is_vowel(p) and p != "SP"]
    return Layout(phones=phones, frames=frames, t0=t_begin,
                  vowel_frame=vowel_frame, note_hz=note_hz)


def _share(part: list[str], whole: list[str], nominal) -> float:
    """塊の中で `part` が占める割合。"""
    tot = sum(nominal(p) for p in whole)
    return (sum(nominal(p) for p in part) / tot) if tot > 0 else 0.0


# ---------------------------------------------------------------------------
# 音長・音程・tension の予測器
# ---------------------------------------------------------------------------


@dataclass
class Variance:
    """バンク同梱の「歌い方」の予測器。

    **最初はこれを使わなかった。それが機械音声の主因だった。**

    「楽譜から歌い方を推測させるとタイミングが崩れる」と考えて、音長は
    種別ごとの固定値、f0 は正弦のビブラートで自分で作っていた。音程誤差は
    中央 −0.1 cent、長さ誤差 0 ms まで追い込めたが、**規則的すぎる音長と
    数学的に綺麗すぎる f0 こそが機械に聞こえる原因**だった。人の歌は
    そうなっていない。

    しかも心配は要らなかった。これらは**楽譜で拘束して使える**設計になって
    いる:

      * `dur` は `word_dur`（単語ごとの長さ）を受け取り、**その中での**
        音素の配分だけを決める
      * `pitch` は `note_midi` / `note_dur`（楽譜そのもの）を受け取り、
        **書かれた音符に沿った** f0 を返す

    つまり音符の位置は動かず、中の歌い回しだけが人らしくなる。

    もう1つ、`tension`（毎フレームの声の張り）も本来ここが出す。音響モデルは
    これを要求するが、私は**定数 0.0 を流していた** ―― 例外は出ないので
    気付かない。

    実測で分かった規約（推測すると黙って壊れるので、実物で確かめた）:

      * `ph_dur_pred` は**フレーム単位**。ただし `word_dur` の合計を超えて
        返る（65+65 に対し 152.5）ので、**単語ごとに正規化して嵌める**
      * `pitch_pred` は **MIDI半音**。Hz ではない
    """

    ling_dur: object
    dur: object
    ling_pitch: object
    pitch: object
    ling_var: object | None
    variance: object | None
    token: dict[str, int]
    token_pitch: dict[str, int]
    emb_dur: dict[str, np.ndarray]
    emb_pitch: dict[str, np.ndarray]
    emb_var: dict[str, np.ndarray]
    lang_id: int
    lang: str = "en"

    @staticmethod
    def load(vb: Voicebank, threads: int = 0) -> "Variance | None":
        """在れば読む。無いバンクもあるので、無くても止めない。"""
        import onnxruntime as ort

        def find(*names) -> Path | None:
            for n in names:
                hit = next((p for p in vb.root.rglob(n)), None)
                if hit:
                    return hit
            return None

        def beside(model: Path | None, *names) -> Path | None:
            # Tiger は linguistic.onnx と phonemes.txt を工程ごとに同名で持つ。
            # 名前だけで探すと別工程のものを掴むので、相手のモデルの隣を見る
            if model is None:
                return None
            return next((model.with_name(n) for n in names
                         if model.with_name(n).exists()), None)

        dur, pitch = find("dur.onnx"), find("pitch.onnx")
        need = {
            "ling_dur": (beside(dur, "linguistic-dur.onnx", "linguistic.onnx")
                         or find("linguistic-dur.onnx", "linguistic.onnx")),
            "dur": dur,
            "ling_pitch": (beside(pitch, "linguistic-pitch.onnx", "linguistic.onnx")
                           or find("linguistic-pitch.onnx", "linguistic.onnx")),
            "pitch": pitch,
        }
        if any(v is None for v in need.values()):
            log.warning("%s: duration/pitch predictors incomplete %s -- falling back to rules",
                        vb.name, [k for k, v in need.items() if v is None])
            return None

        def table(model: Path) -> dict[str, int] | None:
            ph = (find("phonemes-variance.json") or find("phonemes.json")
                  or beside(model, "phonemes.txt") or find("phonemes.txt"))
            if ph is None:
                return None
            if ph.suffix == ".json":
                raw = json.loads(ph.read_text(encoding="utf-8"))
                return ({k: int(v) for k, v in raw.items()} if isinstance(raw, dict)
                        else {p: i for i, p in enumerate(raw)})
            return {p: i for i, p in enumerate(ph.read_text(encoding="utf-8").split())}

        token, token_pitch = table(dur), table(pitch)
        if token is None or token_pitch is None:
            return None

        def embeds(sub: str) -> dict[str, np.ndarray]:
            d = next((p for name in ("embeds", "files")
                      for p in vb.root.rglob(f"{sub}/{name}")
                      if p.is_dir() and any(p.glob("*.emb"))), None)
            if d is None:
                return dict(vb.speakers)
            return {p.stem: np.fromfile(p, dtype=np.float32) for p in d.glob("*.emb")}

        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads

        def s(p):
            return ort.InferenceSession(str(p), opts, providers=["CPUExecutionProvider"])

        ling_var, var = find("linguistic-variance.onnx"), find("variance.onnx")
        return Variance(
            ling_dur=s(need["ling_dur"]), dur=s(need["dur"]),
            ling_pitch=s(need["ling_pitch"]), pitch=s(need["pitch"]),
            ling_var=s(ling_var) if ling_var else None,
            variance=s(var) if var else None,
            token=token, token_pitch=token_pitch,
            emb_dur=embeds("dsdur"), emb_pitch=embeds("dspitch"),
            emb_var=embeds("dsvariance"), lang_id=vb.lang_id or 0, lang=vb.lang)

    # -- 補助 ----------------------------------------------------------------

    def _tok(self, ph: str, token: dict[str, int] | None = None) -> int:
        table = self.token if token is None else token
        for cand in (f"{self.lang}/{ph}", ph):
            if cand in table:
                return table[cand]
        raise KeyError(f"the variance model does not know phoneme {ph!r}")

    def _emb(self, table: dict[str, np.ndarray], name: str, n: int) -> np.ndarray:
        # `table.get(name) or ...` は配列に対して真偽が取れず落ちる
        e = table.get(name)
        if e is None:
            e = next(iter(table.values()))
        return np.tile(e.astype(np.float32), (1, n, 1))

    def _run(self, sess, outs, feed):
        want = {i.name for i in sess.get_inputs()}
        return sess.run(outs, {k: v for k, v in feed.items() if k in want})

    # -- 本体 ----------------------------------------------------------------

    def phoneme_frames(self, words: list[list[str]], word_frames: np.ndarray,
                       word_midi: np.ndarray, speaker: str) -> np.ndarray:
        """単語ごとの持ち時間の**中で**、音素の長さを決めさせる。

        返り値は単語ごとの合計が `word_frames` にぴったり一致する。
        楽譜のタイミングは動かない。
        """
        flat = [p for w in words for p in w]
        n = len(flat)
        tokens = np.array([[self._tok(p) for p in flat]], dtype=np.int64)
        langs = np.full((1, n), self.lang_id, dtype=np.int64)
        div = np.array([[len(w) for w in words]], dtype=np.int64)
        wdur = word_frames[None].astype(np.int64)
        ph_midi = np.array([[int(round(m)) for w, m in zip(words, word_midi)
                             for _ in w]], dtype=np.int64)

        enc, masks = self._run(self.ling_dur, ["encoder_out", "x_masks"], {
            "tokens": tokens, "languages": langs, "word_div": div, "word_dur": wdur})
        pred = self._run(self.dur, ["ph_dur_pred"], {
            "encoder_out": enc, "x_masks": masks, "ph_midi": ph_midi,
            "spk_embed": self._emb(self.emb_dur, speaker, n)})[0][0]

        # ★単語ごとに正規化する★ 予測はフレーム単位だが合計は保証されない
        # （実測: 65+65 フレームの枠に対して 152.5 が返る）。ここで嵌めないと
        # 曲が伸びる。
        out = np.zeros(n, dtype=np.int64)
        i = 0
        for w, target in zip(words, word_frames.astype(np.int64)):
            k = len(w)
            seg = np.maximum(pred[i:i + k].astype(np.float64), 1e-3)
            if target < k:                       # 音素の数すら入らない速い音符
                seg = np.ones(k)
                target = k
            acc = np.round(np.cumsum(seg) / seg.sum() * target).astype(np.int64)
            acc = np.maximum.accumulate(np.clip(acc, 0, target))
            d = np.diff(acc, prepend=0)
            # 長さ0の音素が出ないよう、長いものから1フレームずつ融通する
            while (d < 1).any():
                take = int(np.argmax(d))
                give = int(np.argmin(d))
                if d[take] <= 1:
                    break
                d[take] -= 1
                d[give] += 1
            out[i:i + k] = d
            i += k
        return out

    def f0_line(self, phones: list[str], ph_frames: np.ndarray,
                note_midi: np.ndarray, note_frames: np.ndarray,
                base_midi: np.ndarray, speaker: str,
                steps: int = 20, expr: float = 1.0) -> np.ndarray:
        """書かれた音符に沿った、人らしい f0 を作らせる。返りは Hz。"""
        n, t = len(phones), int(ph_frames.sum())
        tokens = np.array([[self._tok(p, self.token_pitch) for p in phones]], dtype=np.int64)
        enc, _ = self._run(self.ling_pitch, ["encoder_out", "x_masks"], {
            "tokens": tokens,
            "languages": np.full((1, n), self.lang_id, dtype=np.int64),
            "ph_dur": ph_frames[None].astype(np.int64)})
        pred = self._run(self.pitch, ["pitch_pred"], {
            "encoder_out": enc, "ph_dur": ph_frames[None].astype(np.int64),
            "note_midi": note_midi[None].astype(np.float32),
            "note_rest": (note_midi < 1)[None],
            "note_dur": note_frames[None].astype(np.int64),
            "pitch": base_midi[None].astype(np.float32),
            "expr": np.full((1, t), expr, np.float32),
            "retake": np.ones((1, t), bool),
            "spk_embed": self._emb(self.emb_pitch, speaker, t),
            "steps": np.array(steps, np.int64)})[0][0]
        return 440.0 * 2.0 ** ((pred - 69.0) / 12.0)      # ★MIDI半音で返る★

    def tension_line(self, phones: list[str], ph_frames: np.ndarray,
                     f0_hz: np.ndarray, speaker: str,
                     steps: int = 20) -> np.ndarray | None:
        if self.variance is None or self.ling_var is None:
            return None
        n, t = len(phones), int(ph_frames.sum())
        enc, _ = self._run(self.ling_var, ["encoder_out", "x_masks"], {
            "tokens": np.array([[self._tok(p) for p in phones]], dtype=np.int64),
            "languages": np.full((1, n), self.lang_id, dtype=np.int64),
            "ph_dur": ph_frames[None].astype(np.int64)})
        midi = 69.0 + 12.0 * np.log2(np.maximum(f0_hz, 1.0) / 440.0)
        return self._run(self.variance, ["tension_pred"], {
            "encoder_out": enc, "ph_dur": ph_frames[None].astype(np.int64),
            "pitch": midi[None].astype(np.float32),
            "tension": np.zeros((1, t), np.float32),
            "retake": np.ones((1, t, 1), bool),
            "spk_embed": self._emb(self.emb_var, speaker, t),
            "steps": np.array(steps, np.int64)})[0][0]


# ---------------------------------------------------------------------------
# 推論
# ---------------------------------------------------------------------------


class Engine:
    """ONNX セッションを持ち回す。モデルが重い（音響 312MB）ので1回だけ開く。"""

    def __init__(self, vb: Voicebank, threads: int = 0,
                 lex: "Lexicon | None" = None):
        import onnxruntime as ort

        self.vb = vb
        self.lex = lex or Lexicon()
        self.variance = Variance.load(vb, threads=threads)
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self._ort = ort
        log.info("loading the acoustic model: %s", vb.acoustic)
        self.acoustic = ort.InferenceSession(str(vb.acoustic), opts,
                                             providers=["CPUExecutionProvider"])
        self.vocoder = ort.InferenceSession(str(vb.vocoder), opts,
                                            providers=["CPUExecutionProvider"])

    def render(self, layout: Layout, f0: np.ndarray, st: SvsStyle,
               tension: np.ndarray | None = None) -> np.ndarray:
        """メルを出して波形にする。

        **入力はモデルが宣言したものだけを渡す。** バンクによって要る入力が
        違うため（Tiger は8つ、Nessie は `languages` と `tension` が増えて
        10）、こちらで決め打ちにすると次のバンクで落ちる。名前で引き当て、
        知らない名前が来たら黙って流さずに止める。
        """
        vb = self.vb
        t = layout.total
        if len(f0) != t:
            raise ValueError(f"f0 has {len(f0)} frames but the phonemes total {t}")
        n_tok = len(layout.phones)
        tokens = np.array([[vb.resolve(p) for p in layout.phones]], dtype=np.int64)
        emb = st.embedding(vb)

        pool = {
            "tokens": lambda: tokens,
            "durations": lambda: layout.frames[None].astype(np.int64),
            "languages": lambda: np.full((1, n_tok), vb.lang_id or 0, dtype=np.int64),
            "f0": lambda: f0[None].astype(np.float32),
            "gender": lambda: np.full((1, t), st.gender, np.float32),
            "velocity": lambda: np.full((1, t), st.velocity, np.float32),
            # ★予測された曲線があればそれを使う★ ここに定数を流していたのが
            # 機械音声の一因だった（例外は出ないので気付かない）。
            "tension": lambda: (tension[None].astype(np.float32)
                                if tension is not None and len(tension) == t
                                else np.full((1, t), st.tension, np.float32)),
            "energy": lambda: np.full((1, t), st.energy, np.float32),
            "breathiness": lambda: np.full((1, t), st.breathiness, np.float32),
            "voicing": lambda: np.full((1, t), st.voicing, np.float32),
            "spk_embed": lambda: np.tile(emb, (1, t, 1)).astype(np.float32),
            "depth": lambda: np.array(min(st.depth, vb.max_depth), np.float32),
            "steps": lambda: np.array(st.steps, np.int64),
        }
        feed = {}
        for spec in self.acoustic.get_inputs():
            make = pool.get(spec.name)
            if make is None:
                raise KeyError(
                    f"{vb.name}: unknown model input {spec.name!r} ({spec.shape}). "
                    f"Running inference without knowing what to feed it "
                    f"produces wrong audio silently. Add it to the pool in "
                    f"`svs.Engine.render`.")
            feed[spec.name] = make()

        mel = self.acoustic.run(["mel"], feed)[0]
        if vb.mel_gain != 1.0:
            mel = mel * vb.mel_gain
        wav = self.vocoder.run(["waveform"], {"mel": mel, "f0": feed["f0"]})[0]
        return np.asarray(wav[0], dtype=np.float32)


# ---------------------------------------------------------------------------
# 楽譜から歌へ
# ---------------------------------------------------------------------------


@dataclass
class SungLine:
    audio: np.ndarray
    rate: int
    start_s: float
    n_phones: int = 0
    rms: float = 0.0
    f0_lo: float = 0.0
    f0_hi: float = 0.0


def plan_words(vb: Voicebank, syls: list[list[str]], starts: np.ndarray,
               ends: np.ndarray, pitches: np.ndarray, lead_s: float = 0.30,
               tail_s: float = 0.30) -> tuple[list[list[str]], np.ndarray,
                                              np.ndarray, float]:
    """音節を「単語」に仕立て、それぞれの持ちフレーム数を楽譜から決める。

    予測器に渡すのはこの枠だけで、**枠の中の配分は向こうに任せる**。枠が
    楽譜どおりなので、音符の位置は動かない。

    音節の頭は、母音が拍に乗るよう**拍より手前**から始める（歌の約束。
    そうしないと「タン」が「ンタ」になる）。手前にどれだけ取るかは子音の
    標準の長さから決め、直前の空きの半分を超えないようにする。
    """
    n = len(syls)
    heads = [[p for p in s[:next(i for i, q in enumerate(s) if vb.is_vowel(q))]]
             for s in syls]

    def lead(k: int) -> float:
        want = sum(CONSONANT_S.get(p, CONSONANT_DEFAULT) for p in heads[k])
        room = (starts[k] - (ends[k - 1] if k else starts[0] - lead_s))
        return float(min(want, max(room, 0.0) * 0.5 + (lead_s * 0.5 if k == 0 else 0)))

    begin = np.array([starts[k] - lead(k) for k in range(n)])
    begin = np.maximum.accumulate(begin)          # 単調にしておく
    t0 = float(min(begin[0], starts[0] - lead_s))

    words: list[list[str]] = []
    edges: list[float] = []
    midi: list[float] = []
    if begin[0] - t0 > 1e-3:
        words.append(["SP"])
        edges.append(begin[0])
        midi.append(float(pitches[0]))
    for k in range(n):
        words.append(list(syls[k]))
        nxt = begin[k + 1] if k + 1 < n else ends[-1]
        edges.append(float(max(nxt, begin[k] + 0.03)))
        midi.append(float(pitches[k]))
        # 休符: 音符が終わってから次の音節が始まるまでが空いていれば無音
        if k + 1 < n and begin[k + 1] - ends[k] > 0.05:
            words[-1] = list(syls[k])
            edges[-1] = float(ends[k])
            words.append(["SP"])
            edges.append(float(begin[k + 1]))
            midi.append(float(pitches[k]))
    words.append(["SP"])
    edges.append(float(ends[-1] + tail_s))
    midi.append(float(pitches[-1]))

    acc = np.round((np.array(edges) - t0) / vb.frame_s).astype(np.int64)
    acc = np.maximum.accumulate(np.maximum(acc, 1))
    frames = np.diff(acc, prepend=0)
    keep = frames > 0
    return ([w for w, k in zip(words, keep) if k], frames[keep],
            np.array(midi)[keep], t0)


def sing_line(engine: Engine, plan, tempo, st: SvsStyle,
              transpose: int = 0) -> SungLine:
    """歌唱計画の1行を歌わせる。"""
    from .singer import pitch_line

    vb = engine.vb
    syls = vb.line_syllables(plan.line, engine.lex)
    if len(syls) != len(plan.targets):
        raise ValueError(
            f"{len(syls)} phoneme syllables against {len(plan.targets)} targets: "
            f"{plan.line.text!r}")

    beats = np.array([[t.start_beat, t.end_beat] for t in plan.targets])
    secs = np.asarray(tempo.beats_to_seconds(beats.ravel()), dtype=float).reshape(-1, 2)
    pitches = np.array([t.pitch + transpose for t in plan.targets], dtype=float)

    var = engine.variance if st.use_variance else None
    if var is not None:
        words, wframes, wmidi, t0 = plan_words(vb, syls, secs[:, 0], secs[:, 1], pitches)
        phones = [p for w in words for p in w]
        pframes = var.phoneme_frames(words, wframes, wmidi, st.speaker)
        base = np.repeat(wmidi, wframes)
        f0 = var.f0_line(phones, pframes, wmidi, wframes, base, st.speaker,
                         steps=st.pitch_steps, expr=st.expressiveness)
        lay = Layout(phones=phones, frames=pframes, t0=t0,
                     vowel_frame=[], note_hz=np.repeat(
                         440.0 * 2.0 ** ((wmidi - 69) / 12.0), wframes))
        ten = None
        if st.tension_source in ("predicted", "clip"):
            ten = var.tension_line(phones, pframes, f0, st.speaker,
                                   steps=st.pitch_steps)
            if ten is not None and st.tension_source == "clip":
                ten = np.clip(ten, -1.0, 1.0)
        wav = engine.render(lay, f0, st, tension=ten)
    else:
        lay = layout_line(vb, syls, secs[:, 0], secs[:, 1], pitches)
        rng = np.random.default_rng(st.seed + int(round(plan.start_beat * 16)))
        f0 = pitch_line(lay.note_hz, lay.note_hz > 0, st, vb.frame_s, rng)
        wav = engine.render(lay, f0, st)
    rms = float(np.sqrt((wav ** 2).mean())) if len(wav) else 0.0
    if rms < QUIET_RMS:
        raise QuietOutput(
            f"almost silent (rms {rms:.5f}): {plan.line.text!r}\n"
            f"  The model does not raise here; it just returns quiet audio.\n"
            f"  If gender={st.gender} is not 0, suspect that first.")
    v = f0[lay.note_hz > 0]
    return SungLine(audio=wav * st.gain, rate=vb.sample_rate, start_s=lay.t0,
                    n_phones=len(lay.phones), rms=rms,
                    f0_lo=float(v.min()) if len(v) else 0.0,
                    f0_hi=float(v.max()) if len(v) else 0.0)


@dataclass
class SungStem:
    audio: np.ndarray
    rate: int
    n_lines: int = 0
    n_phones: int = 0
    f0_lo: float = 0.0
    f0_hi: float = 0.0
    warnings: list[str] = field(default_factory=list)


def render_sung_stem(plans, engine: Engine, tempo, *, duration_s: float,
                     lead_in_s: float = 0.0, tail_s: float = 0.0,
                     gain: float = 1.0, transpose: int = 0,
                     style: SvsStyle | None = None,
                     progress=None) -> SungStem:
    """歌唱計画をまとめて1本のステムにする。"""
    st = style or SvsStyle()
    rate = engine.vb.sample_rate
    total = int(round((lead_in_s + duration_s + tail_s) * rate))
    out = np.zeros(total, dtype=np.float32)
    warn: list[str] = []
    f0_lo, f0_hi = 1e9, 0.0
    n_ok = n_ph = 0

    for k, plan in enumerate(plans):
        try:
            sung = sing_line(engine, plan, tempo, st, transpose=transpose)
        except Exception as e:  # noqa: BLE001 - 1行の失敗で全体を落とさない
            warn.append(f"{plan.line.text!r}: {type(e).__name__}: {e}")
            continue
        i = int(round((lead_in_s + sung.start_s) * rate))
        seg = sung.audio
        if i < 0:
            seg, i = seg[-i:], 0
        j = min(total, i + len(seg))
        if j > i:
            out[i:j] += seg[:j - i] * gain
        n_ok += 1
        n_ph += sung.n_phones
        f0_lo, f0_hi = min(f0_lo, sung.f0_lo), max(f0_hi, sung.f0_hi)
        if progress:
            progress(k + 1, len(plans), sung)

    if not n_ok:
        warn.append("not a single line was sung")
    return SungStem(audio=out, rate=rate, n_lines=n_ok, n_phones=n_ph,
                    f0_lo=0.0 if f0_lo > 1e8 else f0_lo, f0_hi=f0_hi,
                    warnings=warn)
