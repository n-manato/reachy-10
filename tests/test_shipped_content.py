"""The clips and song that actually ship must pass their own gate.

This is the regression net: if someone tunes a clip amplitude or nudges a safety
limit, this is what notices that the shipped library no longer validates.
"""

import pytest

from conftest import needs_sdk
from orchestra.motion.compile import compile_clip_preview
from orchestra.motion.validate import validate_clip, validate_program

TARGET_BPM = 112.0


def test_library_loads_without_errors(lib):
    assert len(lib) > 0, "no clips found"
    assert not lib.errors, f"clips failed to load: {lib.errors}"


def test_every_clip_declares_an_instrument(lib):
    for key in lib.keys():
        assert lib.get(key).instrument, f"{key} has no instrument"


def test_sounding_clips_have_anchors(lib):
    """アンカーが「音が出る瞬間」の唯一の記録。

    副次モーションの消音、発熱保護の duty、ジェスチャ重なり判定、そして
    `make_stems` の発音タイミングまで全部これに乗っている。単発クリップで
    ここが空だと、**音とモーションが別々の真実を持つ**ことになり、いつか
    必ずズレる。
    """
    for key in lib.keys():
        c = lib.get(key)
        # `expression` は音を伴わない演技（頷き・耳の反応など）。アンカーが
        # 無いのが正しい ―― 鳴らす瞬間そのものが無い。
        if c.placement == "trigger" and c.instrument != "expression":
            assert c.anchors, (
                f"{key} は音符ごとに発火する単発クリップなのにアンカーが無い。"
                "どの瞬間に音が出るかが決まらず、アンカー整列も効かない"
            )


@needs_sdk
@pytest.mark.parametrize("bpm", [90.0, TARGET_BPM, 130.0])
def test_all_clips_pass_at_performance_tempos(lib, fleet_cfg, bpm):
    failures = []
    for key in lib.keys():
        clip = lib.get(key)
        res = compile_clip_preview(clip, bpm=bpm, loops=4, fps=fleet_cfg.fps,
                                   neutral_antennas=fleet_cfg.neutral_antennas)
        rep = validate_program(res.program, fleet_cfg.safety, bpm=bpm, label=key)
        crep = validate_clip(clip, fleet_cfg.safety, bpm)
        if not (rep.ok and crep.ok):
            failures.append(f"{key} @ {bpm}: {[str(v) for v in rep.worst(3)]}")
    assert not failures, "\n".join(failures)


@needs_sdk
def test_clips_have_real_velocity_headroom(lib, fleet_cfg):
    """Passing with 2% margin is one tempo bump from failing. Report the
    margin so it is visible rather than implicit."""
    tight = []
    for key in lib.keys():
        res = compile_clip_preview(lib.get(key), bpm=TARGET_BPM, loops=4,
                                   fps=fleet_cfg.fps,
                                   neutral_antennas=fleet_cfg.neutral_antennas)
        rep = validate_program(res.program, fleet_cfg.safety, bpm=TARGET_BPM)
        head = rep.stats.get("joint_vel_headroom", 0.0)
        if head < 0.2:
            tight.append(f"{key}: only {head:.2f} rad/s of headroom")
    assert not tight, "\n".join(tight)


@needs_sdk
def test_small_world_builds_and_validates(ctx):
    from orchestra.music.score import load_score
    from orchestra.show.build import build_show

    score = load_score(ctx.songs_dir / "small_world")
    build = build_show(ctx, score, validate=True)

    assert not build.problems, build.problems
    for bp in build.parts:
        assert bp.ok, f"{bp.robot.id} failed:\n{bp.report.summary()}"


def test_every_cast_robot_has_a_stem(ctx):
    """音は全部ロボットのスピーカーから出る。ステムが無い＝その台は無音。

    物理楽器があった頃は「1台＝1音程のチャイムバー」だったので、旋律の音程を
    全部どれかの台に割り当てられているかを見ていた。楽器を外した今は
    1台が旋律を丸ごと唄えるので、その検査は意味を失った。代わりに効くのは
    **配役された全台に鳴らすものがあるか**で、これを落とすとその台は
    動くだけの置物になる。
    """
    from orchestra.music.score import load_score

    score = load_score(ctx.songs_dir / "small_world")
    silent = [name for name in score.cast if not score.parts[name].audio]
    assert not silent, f"配役されているのにステムが無い: {silent}"


def test_small_world_stems_exist_on_disk(ctx):
    """`tools/make_stems.py` を流し忘れると、ビルドは通るのに全台が無音で踊る。"""
    from orchestra.music.score import load_score

    song_dir = ctx.songs_dir / "small_world"
    score = load_score(song_dir)
    missing = [
        p.audio for p in score.parts.values()
        if p.audio and not (song_dir / "audio" / p.audio).exists()
    ]
    assert not missing, (
        f"ステムが無い: {missing}\n"
        "python tools/make_stems.py songs/small_world で生成する"
    )


def test_small_world_cast_is_valid(ctx):
    from orchestra.music.score import load_score

    score = load_score(ctx.songs_dir / "small_world")
    assert not score.check_cast({r.id for r in ctx.robots})


def test_registry_identities_are_unique(ctx):
    hwids = [r.hardware_id for r in ctx.robots if r.hardware_id]
    assert len(hwids) == len(set(hwids)), "duplicate hardware_id in robots.yaml"
    ips = [r.ip for r in ctx.robots if r.ip]
    assert len(ips) == len(set(ips)), "duplicate ip in robots.yaml"


def test_exactly_one_conductor(ctx):
    conductors = [r for r in ctx.robots if r.is_conductor]
    assert len(conductors) == 1, f"expected 1 conductor, found {len(conductors)}"


def test_registry_select_resolves_by_any_key(ctx):
    r = ctx.robots.robots[0]
    assert ctx.robots.get(r.id) is r
    assert ctx.robots.get(r.hardware_id) is r
    assert ctx.robots.get(r.ip) is r
    assert len(ctx.robots.select(None)) == len(ctx.robots.enabled)
