"""`orchestra` -- the command line for the whole system.

Grouped to match the workflow rather than the code layout:

    orchestra fleet   ...   find, inspect, provision and recover robots
    orchestra clip    ...   author and verify ONE motion on ONE robot
    orchestra song    ...   compile a score into per-robot programs
    orchestra show    ...   deploy, arm, sync, start, stop
    orchestra calibrate ... measure the per-robot numbers that matter

The single rule worth knowing: **nothing is deployed unvalidated.** `song build`
runs the full safety gate at the song's real tempo and refuses to write a build
that fails, because on this hardware an over-speed or unreachable pose does not
raise -- it silently freezes the robot or stalls a servo against an instrument.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import Calibration, Context, RobotConfig


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return "  (nothing to show)"
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    out = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")


def _ctx(args) -> Context:
    try:
        return Context.load(getattr(args, "root", None))
    except FileNotFoundError as e:
        die(str(e))


def _robots(ctx: Context, args) -> list[RobotConfig]:
    sel = getattr(args, "robot", None)
    try:
        robots = ctx.robots.select(sel)
    except KeyError as e:
        die(str(e))
    if not robots:
        die("no robots selected")
    return robots


# ---------------------------------------------------------------------------
# fleet
# ---------------------------------------------------------------------------


def cmd_fleet_status(args) -> int:
    from .fleet.health import fleet_health

    ctx = _ctx(args)
    robots = _robots(ctx, args)
    health = fleet_health([r.ip for r in robots], ctx.fleet.daemon_port, timeout=6.0)

    from .show.deploy import agent_running

    rows = []
    unhealthy = 0
    for r in robots:
        h = health[r.ip]
        agent = "yes" if agent_running(ctx, r) else "-"
        if not h.healthy:
            unhealthy += 1
        rows.append([
            r.id, r.ip, r.role, r.instrument or "-", h.state,
            h.motors_mode,
            f"{h.control_loop_hz:.1f}" if h.control_loop_hz else "-",
            h.version or "-", h.app_lock or "-", agent,
        ])
    print(table(rows, ["id", "ip", "role", "instrument", "state", "motors",
                       "loop Hz", "fw", "lock", "agent"]))
    print()
    if unhealthy:
        print(f"  {unhealthy} robot(s) not in `running` state. "
              "Recover with: orchestra fleet recover --robot <id>")
    return 1 if unhealthy else 0


def cmd_fleet_discover(args) -> int:
    from .fleet.discovery import apply_reconciliation, discover, reconcile

    ctx = _ctx(args)
    subnet = args.subnet or ctx.fleet.network.get("subnet", "192.168.3.0/24")
    print(f"sweeping {subnet} for daemons on port {ctx.fleet.daemon_port} ...")
    found = discover(subnet, ctx.fleet.daemon_port)
    print(f"found {len(found)} robot(s)\n")

    rows = [[f.ip, f.hardware_id or "?", f.robot_name or "?", f.version or "?",
             f.state or "?", f.variant] for f in found]
    print(table(rows, ["ip", "hardware_id", "name", "fw", "state", "variant"]))

    rec = reconcile(ctx.robots, found)
    print("\nagainst robots.yaml:")
    print(rec.report())

    if rec.has_changes:
        if args.write:
            n = apply_reconciliation(ctx.robots, rec)
            path = ctx.robots.save()
            print(f"\nupdated {n} address(es) in {path}")
        else:
            print("\n(re-run with --write to update robots.yaml)")
    return 0


def cmd_fleet_recover(args) -> int:
    from .fleet.health import DaemonClient
    from .fleet.ssh import SSHClient

    ctx = _ctx(args)
    for r in _robots(ctx, args):
        print(f"{r.id} ({r.ip}):")
        # systemctl over SSH, not the API stop/start pair. The project's own
        # LEARNINGS.md documents the API route corrupting WebRTC signalling
        # state; systemctl is the route that reliably works.
        try:
            res = SSHClient(ctx.fleet, r.ip).restart_daemon()
            if res.ok:
                ok("daemon restarted via systemctl")
            else:
                bad(f"systemctl restart: {res.stderr.strip() or res.stdout.strip()}")
                if args.api_fallback:
                    DaemonClient(r.ip, ctx.fleet.daemon_port).restart_daemon()
                    ok("daemon restarted via API (fallback)")
        except Exception as e:
            bad(f"{type(e).__name__}: {e}")
            continue

        print("  waiting for the backend to come back ...")
        c = DaemonClient(r.ip, ctx.fleet.daemon_port, timeout=5.0)
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(3)
            h = c.health()
            if h.healthy:
                ok(f"running (fw {h.version}, motors {h.motors_mode})")
                break
            print(f"    state={h.state} ...")
        else:
            bad("did not reach `running` within 90s")
        c.close()
    return 0


def cmd_fleet_provision(args) -> int:
    from .show.deploy import provision_fleet

    ctx = _ctx(args)
    robots = _robots(ctx, args)
    print(f"provisioning {len(robots)} robot(s) ...\n")
    results = provision_fleet(ctx, robots)
    failed = 0
    for rid in sorted(results):
        res = results[rid]
        print(f"{rid}:")
        for s in res.steps:
            ok(s)
        if not res.ok:
            failed += 1
            bad(res.error)
    if failed:
        print(
            f"\n{failed} robot(s) failed. If SSH is refusing, install a key once "
            f"per robot from Git Bash:\n"
            f"  ssh-copy-id -i {ctx.fleet.ssh.get('key')}.pub "
            f"{ctx.fleet.ssh_user}@<ip>"
        )
    return 1 if failed else 0


def cmd_fleet_agent(args) -> int:
    from .show.deploy import agent_log, start_fleet_agents, stop_fleet_agents

    ctx = _ctx(args)
    robots = _robots(ctx, args)

    if args.action == "start":
        results = start_fleet_agents(ctx, robots, dry_run=args.dry_run)
    elif args.action == "stop":
        results = stop_fleet_agents(ctx, robots)
    else:
        for r in robots:
            print(f"=== {r.id} ({r.ip}) ===")
            print(agent_log(ctx, r, args.lines))
        return 0

    failed = 0
    for rid in sorted(results):
        res = results[rid]
        if res.ok:
            ok(f"{rid}: {'; '.join(res.steps) or args.action}")
        else:
            failed += 1
            bad(f"{rid}: {res.error}")
    return 1 if failed else 0


def cmd_fleet_selftest(args) -> int:
    """各機で実機自己診断を走らせる（カメラ・スピーカー・マイク・モータ）。

    「初期化できた」ではなく「実際に動いた」を見る。音声はロボット自身の
    マイクで自分のスピーカーを録る折り返し試験なので、人が聴かなくても
    出力経路と入力経路の両方を同時に検証できる。
    """
    import json

    from .fleet.health import parallel
    from .fleet.ssh import SSHClient

    ctx = _ctx(args)
    robots = _robots(ctx, args)
    base = ctx.fleet.remote_dir
    src = ctx.root / "agent" / "selftest.py"

    # 機体ごとに違う周波数を割り当てる。全機が同じ部屋にいるので、同じトーンだと
    # 並列実行時に互いの音を拾ってしまい判定にならない（実際に一度そうなった）。
    # 検出は周波数選択式なので、周波数さえずらせば同時に鳴らしても混ざらない。
    if args.tone:
        # 全機同じ周波数。直列実行と併せると周波数依存を切り分けられる。
        tones = [args.tone] * len(robots)
    else:
        # 機体ごとに違う周波数。全機が同じ部屋にいるので、同じトーンだと並列実行時に
        # 互いの音を拾ってしまう。倍音での混信を避けるため等比ではなく素数比で散らす。
        base = [443.0, 587.0, 719.0, 853.0, 967.0, 1103.0, 1237.0, 1381.0, 1523.0]
        tones = [base[i % len(base)] for i in range(len(robots))]

    def run(item) -> dict:
        r, tone_hz = item
        ssh = SSHClient(ctx.fleet, r.ip)
        ssh.run(f"mkdir -p {base}")
        up = ssh.push(src, f"{base}/selftest.py")
        if not up.ok:
            return {"ok": False, "error": f"upload: {up.stderr.strip()}"}
        python = r.extra.get("python") or ctx.fleet.remote_python
        res = ssh.run(
            f"{python} -u {base}/selftest.py {tone_hz} 2>/dev/null", timeout=180
        )
        for line in reversed(res.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        return {"ok": False, "error": (res.stderr or res.stdout or "no output")[-200:]}

    items = list(zip(robots, tones))
    workers = 1 if args.serial else len(robots)
    print(f"running self-test on {len(robots)} robot(s) "
          f"({'one at a time' if args.serial else 'in parallel, a distinct tone per robot'}, "
          f"20-40s each) ...\n")
    results: dict[str, dict] = {}
    for item, res, err in parallel(items, run, workers=workers):
        rid = item[0].id
        results[rid] = res if res is not None else {"ok": False, "error": str(err)}

    rows = []
    for r in robots:
        d = results[r.id]
        c = d.get("checks", {})

        def mark(name, key):
            x = c.get(name, {})
            if not x.get("ok"):
                return "FAIL"
            return "ok" if x.get(key, True) else "no response"

        cam = c.get("camera", {})
        aud = c.get("audio_loopback", {})
        mot = c.get("motors", {})
        rows.append([
            r.id,
            d.get("media_backend", "-"),
            f"{mark('camera','live')}" + (f" {cam.get('shape','')}" if cam.get("ok") else ""),
            f"{mark('audio_loopback','heard')}" + (f" x{aud.get('ratio','?')}" if aud.get("ok") else ""),
            f"{mark('motors','responded')}" + (f" {mot.get('delta_deg','?')}deg" if mot.get("ok") else ""),
            "yes" if c.get("imu", {}).get("present") else "no",
            "PASS" if d.get("ok") else "FAIL",
        ])
    print(table(rows, ["robot", "media", "camera", "audio(loopback)", "motors", "IMU", "overall"]))

    failed = [r.id for r in robots if not results[r.id].get("ok")]
    if failed:
        print()
        for rid in failed:
            d = results[rid]
            if d.get("error"):
                bad(f"{rid}: {d['error'][:160]}")
            for name, c in d.get("checks", {}).items():
                if not c.get("ok"):
                    bad(f"{rid}/{name}: {c.get('error','')[:140]}")
                elif name == "audio_loopback" and not c.get("heard"):
                    bad(f"{rid}/audio: no energy appears at {c.get('tone_hz')}Hz "
                        f"(silent {c.get('band_frac_quiet')} -> peak "
                        f"{c.get('band_frac_peak')} @{c.get('peak_at_s')}s, "
                        f"x{c.get('ratio')}; per-window={c.get('windows')}) "
                        "-- either the speaker or the microphone is dead")
                elif name == "camera" and not c.get("live"):
                    bad(f"{rid}/camera: frames arrive but variance is {c.get('std')} "
                        "-- all black or a frozen value (lens cap, or sensor not started)")
    if args.json:
        print("\n" + json.dumps(results, indent=1, ensure_ascii=False))
    return 1 if failed else 0


def cmd_fleet_speaker(args) -> int:
    """1台ずつ順にテスト音を鳴らす。耳で確認するためのコマンド。

    なぜ自動判定できないか: このハードには AEC（エコーキャンセラ）が入っており
    (`media/audio_base.py:38` の webrtcdsp + webrtcechoprobe)、ロボット自身の
    スピーカー音をマイク入力から積極的に打ち消す。折り返し録音による自動検証は
    原理的に成立しないので、ここは人の耳が唯一の判定手段になる。
    """
    import time

    from .fleet.health import DaemonClient

    ctx = _ctx(args)
    robots = _robots(ctx, args)

    print(f"sounding {len(robots)} speaker(s), one at a time.")
    print("note down which robots you heard -- and which you did not.\n")

    failed = []
    for r in robots:
        c = DaemonClient(r.ip, ctx.fleet.daemon_port, timeout=20.0)
        try:
            if args.volume is not None:
                # This route plays a sound of its own -- harmless here, since
                # the whole point of this command is to make a noise. Never
                # during a performance; that is what `AgentLink.volume` is for.
                c.set_volume(args.volume)
            vol = c.get_volume()
            print(f"  {r.id} ({r.ip})  volume {vol} ... ", end="", flush=True)
            c._post("/api/volume/test-sound")
            print("played")
        except Exception as e:
            failed.append(r.id)
            print(f"FAILED {type(e).__name__}: {e}")
        finally:
            c.close()
        time.sleep(args.gap)

    print("\nif a robot stayed silent, re-check it alone with `--robot <id>`.")
    if failed:
        for rid in failed:
            bad(f"{rid}: the play command itself failed")
        return 1
    return 0


def cmd_fleet_stop_app(args) -> int:
    from .fleet.health import DaemonClient

    ctx = _ctx(args)
    for r in _robots(ctx, args):
        c = DaemonClient(r.ip, ctx.fleet.daemon_port, timeout=30.0)
        try:
            c.stop_current_app()
            ok(f"{r.id}: stopped current app")
        except Exception as e:
            bad(f"{r.id}: {type(e).__name__}: {e}")
        finally:
            c.close()
    return 0


# ---------------------------------------------------------------------------
# clip
# ---------------------------------------------------------------------------


def cmd_clip_list(args) -> int:
    from .motion.schema import ClipLibrary

    ctx = _ctx(args)
    lib = ClipLibrary(ctx.clips_dir)
    rows = []
    for key in lib.keys():
        c = lib.get(key)
        rows.append([
            key, c.instrument or "-", c.placement,
            f"{c.loop_beats:g}",
            ",".join(f"{a:g}" for a in c.anchors) or "-",
            "+".join(sorted(c.tracks)),
        ])
    print(table(rows, ["clip", "instrument", "placement", "loop", "anchors", "tracks"]))
    if lib.errors:
        print("\nclips that failed to load:")
        for k, e in lib.errors.items():
            bad(f"{k}: {e}")
        return 1
    return 0


def _compile_clip(ctx: Context, ref: str, bpm: float, loops: int, robot_id: str = ""):
    from .motion.compile import compile_clip_preview
    from .motion.schema import ClipError, ClipLibrary

    lib = ClipLibrary(ctx.clips_dir)
    try:
        clip = lib.get(ref)
    except ClipError as e:
        die(str(e))
    res = compile_clip_preview(
        clip, bpm=bpm, loops=loops, fps=ctx.fleet.fps,
        neutral_antennas=ctx.fleet.neutral_antennas, robot_id=robot_id,
    )
    return clip, res


def cmd_clip_check(args) -> int:
    from .motion.schema import ClipLibrary
    from .motion.validate import validate_clip, validate_program

    ctx = _ctx(args)
    lib = ClipLibrary(ctx.clips_dir)
    refs = [args.clip] if args.clip else lib.keys()
    tempos = args.bpm or [112.0]

    worst = 0
    for ref in refs:
        for bpm in tempos:
            clip, res = _compile_clip(ctx, ref, bpm, args.loops)
            prep = validate_program(res.program, ctx.fleet.safety, bpm=bpm, label=ref)
            crep = validate_clip(clip, ctx.fleet.safety, bpm)
            prep.violations.extend(crep.violations)
            prep.ok = prep.ok and crep.ok
            print(prep.summary(verbose=args.verbose))
            print()
            if not prep.ok:
                worst = 1
    if worst:
        print("Validation failed. These limits are enforced here because the SDK")
        print("enforces none of them: an over-speed or unreachable pose is caught")
        print("by the daemon, throttle-logged and ignored, leaving the robot")
        print("frozen at its last valid target with no error you could detect.")
    return worst


def cmd_clip_fit(args) -> int:
    """クリップを安全な振幅まで自動で追い込む。

    関節速度は「振幅 ÷ 時間」で決まるので、形を保ったまま安全域に入れるには
    振幅を縮めるのが一番副作用が少ない。テンポを落とすと曲が変わり、
    キーフレームを個別に触ると軌道の形が崩れる。

    IK が非線形なので一発では決まらない。実測ピークから比を出して縮め、
    測り直す、を数回繰り返す。手で当てずっぽうに調整するより速くて確実。
    """
    from .motion.compile import compile_clip_preview
    from .motion.schema import ClipError, ClipLibrary, save_clip
    from .motion.validate import validate_program

    ctx = _ctx(args)
    lib = ClipLibrary(ctx.clips_dir)
    refs = [args.clip] if args.clip else lib.keys()
    tempos = args.bpm or [112.0]
    lim = ctx.fleet.safety

    changed = 0
    for ref in refs:
        try:
            clip = lib.get(ref)
        except ClipError as e:
            bad(str(e)); continue

        head_k, ant_k = 1.0, 1.0
        report = None
        for attempt in range(args.max_iter):
            trial = clip.scaled(head_k, ant_k) if (head_k, ant_k) != (1.0, 1.0) else clip
            worst_head, worst_ant = 0.0, 0.0
            duty_head, duty_ant = 0.0, 0.0
            ok_all = True
            for bpm in tempos:
                res = compile_clip_preview(
                    trial, bpm=bpm, loops=args.loops, fps=ctx.fleet.fps,
                    neutral_antennas=ctx.fleet.neutral_antennas)
                report = validate_program(res.program, lim, bpm=bpm, label=ref)
                st = report.stats
                worst_head = max(worst_head, st.get("joint_vel_peak", 0.0))
                worst_ant = max(worst_ant, st.get("antenna_r_peak_vel", 0.0),
                                st.get("antenna_l_peak_vel", 0.0))
                duty_head = max(duty_head, st.get("gesture_duty_rms", 0.0))
                duty_ant = max(duty_ant, st.get("antenna_duty_rms", 0.0))
                ok_all = ok_all and report.ok
            if ok_all:
                break
            # 目標の 90% を狙う。ぴったりだと次のテンポで落ちる。
            # ピーク速度と持続的な仕事量(duty)は別々に効くので両方見る。
            # 鈴のようにピークは余裕でも duty だけ超えるクリップが実在する。
            if worst_head > lim.joint_velocity_max:
                head_k *= (lim.joint_velocity_max * 0.90) / worst_head
            if duty_head > lim.gesture_duty_max_rms:
                head_k *= (lim.gesture_duty_max_rms * 0.90) / duty_head
            if worst_ant > lim.antenna_velocity_max:
                ant_k *= (lim.antenna_velocity_max * 0.90) / worst_ant
            if duty_ant > lim.antenna_duty_max_rms:
                ant_k *= (lim.antenna_duty_max_rms * 0.90) / duty_ant
            if head_k < 0.15 and ant_k < 0.15:
                break

        status = "PASS" if (report and report.ok) else "FAIL"
        note = ""
        if (head_k, ant_k) != (1.0, 1.0):
            note = f"  head x{head_k:.2f}"
            if ant_k != 1.0:
                note += f" / antennas x{ant_k:.2f}"
        print(f"  [{status}] {ref:<20} peak {report.stats.get('joint_vel_peak', 0):.2f} rad/s"
              f"{note}")
        if report and not report.ok:
            for v in report.worst(2):
                bad(f"    {v.kind} on {v.channel}: {v.detail[:90]}")

        if args.write and (head_k, ant_k) != (1.0, 1.0) and report and report.ok:
            fitted = clip.scaled(head_k, ant_k)
            fitted.description = (
                (clip.description or "")
                + f"\n\n[clip fit] scaled down to stay inside the envelope at "
                  f"{', '.join(f'{b:g}' for b in tempos)} BPM: head x{head_k:.3f}"
                + (f" / antennas x{ant_k:.3f}" if ant_k != 1.0 else "")
                + ". Timing and the shape of the path are unchanged."
            )
            p = save_clip(fitted, clip.source_path)
            changed += 1
            ok(f"    written back: {p.name}")

    if changed:
        print(f"\n{changed} clip(s) written back. Re-check with `clip check`.")
    elif not args.write:
        print("\n(pass --write to save these)")
    return 0


def cmd_clip_plot(args) -> int:
    from .preview.plot import plot_gesture_detail, plot_program

    ctx = _ctx(args)
    bpm = (args.bpm or [112.0])[0]
    clip, res = _compile_clip(ctx, args.clip, bpm, args.loops)
    out = Path(args.out or (ctx.build_dir / "plots" / f"{clip.name}_{bpm:g}bpm.png"))
    plot_program(res.program, ctx.fleet.safety, out=out,
                 title=f"{args.clip} @ {bpm:g} BPM", show=args.show)
    print(f"  wrote {out}")
    if len(res.program.anchors):
        d = out.with_name(out.stem + "_gesture.png")
        plot_gesture_detail(res.program, ctx.fleet.safety, out=d, show=args.show)
        print(f"  wrote {d}")
    return 0


def cmd_clip_preview(args) -> int:
    from .motion.validate import validate_program
    from .preview.player import play_program

    ctx = _ctx(args)
    robot = ctx.robots.get(args.robot)
    bpm = (args.bpm or [112.0])[0]
    clip, res = _compile_clip(ctx, args.clip, bpm, args.loops, robot.id)

    rep = validate_program(res.program, ctx.fleet.safety, bpm=bpm, label=args.clip)
    if not rep.ok and not args.force:
        print(rep.summary())
        die("clip failed validation -- refusing to send it to hardware. "
            "Fix it, or pass --force if you are certain (and watching).")
    if not rep.ok:
        print("WARNING: sending a clip that FAILED validation because --force was given.")
        print(rep.summary())

    print(f"previewing {args.clip} @ {bpm:g} BPM x{args.loops} on {robot.id} ({robot.ip})")

    if args.transport == "upload":
        # アップロード経路はメディアに一切触らない。SDK 経路は
        # media_backend="no_media" で接続するため、副作用として
        # /api/media/release が走りカメラ IPC ソケットが消える
        # (reachy_mini.py の _setup_media)。単発の動作確認でそれをやりたくない
        # 場合はこちら。
        import time

        from .fleet.upload import UploadSession

        with UploadSession(robot.id, robot.ip, port=ctx.fleet.daemon_port) as s:
            s.enable_motors()
            time.sleep(0.3)
            up = s.upload(res.program)
            if not up.ok:
                die(up.error)
            time.sleep(0.5)
            s.play(up.upload_id, play_frequency=ctx.fleet.fps,
                   initial_goto_duration=args.goto)
            total = res.program.duration + args.goto + 1.0
            time.sleep(total)
            if not args.hold:
                s.disable_motors()
        print(f"  done: {up.encoded_bytes / 1024:.0f}KB / {up.n_chunks} chunks, "
              f"{total:.1f}s{'  (torque held)' if args.hold else ''}")
        return 0

    stats = play_program(robot.ip, res.program, port=ctx.fleet.daemon_port)
    print(f"  done: {stats}")
    return 0


def cmd_clip_record(args) -> int:
    from .motion.record import suggest_anchors, to_clip
    from .motion.schema import save_clip
    from .preview.player import record_clip

    ctx = _ctx(args)
    robot = ctx.robots.get(args.robot)
    print(f"recording on {robot.id} ({robot.ip}) under gravity compensation")
    print("  the robot will go limp -- support the head, then move it by hand")
    raw = record_clip(robot.ip, args.seconds, ctx.fleet.daemon_port, args.countdown)
    if not raw:
        die("captured nothing")

    clip = to_clip(
        raw, name=Path(args.out).stem.replace(".clip", ""), bpm=args.bpm,
        instrument=args.instrument or "", placement=args.placement,
        quantize=args.quantize, trim_start=args.trim_start, trim_end=args.trim_end,
    )
    out = save_clip(clip, args.out)
    n = len(next(iter(clip.tracks.values())).beats)
    print(f"  wrote {out} ({n} keyframes from {len(raw)} samples)")

    if args.placement == "trigger":
        guesses = suggest_anchors(raw, args.bpm)
        if guesses:
            print(f"  suggested anchors (deepest points): {guesses}")
            print("  add them to the clip's `anchors` -- check against what you HEARD,")
            print("  since the anchor is what the musical alignment hangs off.")
    print(f"  now run: orchestra clip check --clip {out.stem.replace('.clip','')} --bpm {args.bpm:g}")
    return 0


# ---------------------------------------------------------------------------
# song
# ---------------------------------------------------------------------------


def cmd_song_info(args) -> int:
    from .music.midi import describe
    from .music.score import load_score

    ctx = _ctx(args)
    score = load_score(_song_path(ctx, args.song))
    print(f"{score.title}  ({score.name})")
    print(describe(score.timeline))
    print(f"\n  lead-in {score.lead_in_s}s, tail {score.tail_s}s")
    print("\nparts:")
    rows = []
    for name, p in score.parts.items():
        rows.append([
            name, p.kind, p.instrument or "-", p.clip or "-",
            score.cast.get(name, "UNASSIGNED"), p.audio or "-",
        ])
    print(table(rows, ["part", "kind", "instrument", "clip", "robot", "audio"]))

    problems = score.check_cast({r.id for r in ctx.robots})
    if problems:
        print("\ncast problems:")
        for p in problems:
            bad(p)
        return 1
    return 0


def _song_path(ctx: Context, song: str) -> Path:
    p = Path(song)
    if p.exists():
        return p
    cand = ctx.songs_dir / song
    if cand.exists():
        return cand
    die(f"no song {song!r} (looked in {ctx.songs_dir})")


def cmd_song_build(args) -> int:
    from .music.score import load_score
    from .show.build import build_show, write_build

    ctx = _ctx(args)
    score = load_score(_song_path(ctx, args.song))
    print(f"building {score.title} ...")
    build = build_show(ctx, score, validate=not args.no_validate)
    print(build.summary())

    for bp in build.parts:
        for w in bp.warnings:
            print(f"  warning [{bp.robot.id}]: {w}")

    if args.report:
        for bp in build.parts:
            if bp.report and (not bp.report.ok or args.verbose):
                print()
                print(bp.report.summary(verbose=args.verbose))

    if not build.ok:
        print("\nbuild FAILED validation -- nothing written.")
        print("Run with --report to see exactly which joint, which frame and why.")
        return 1

    out = ctx.build_dir / score.name
    written = write_build(build, out)
    print(f"\nwrote {len(written)} program(s) to {out}")
    return 0


def cmd_song_fit(args) -> int:
    """曲の文脈でクリップを安全域に追い込む。

    `clip fit` はクリップ単体で見るが、実際の曲では
      * 同じクリップが音符ごとに何度も置かれて**寄与が加算**され、
      * さらに常時ノリ層などの副次モーションが上に乗る
    ので、単体で通っても曲では超えることがある。ここでは実際にビルドした
    プログラムを測って、落ちたパートのクリップだけを縮める。
    """
    from .motion.schema import ClipLibrary, save_clip
    from .music.score import load_score
    from .show.build import build_show

    ctx = _ctx(args)
    score = load_score(_song_path(ctx, args.song))
    lim = ctx.fleet.safety
    # ヘッドとアンテナは別のモーター（sts3215 / xc330）なので別々に縮める。
    # 鈴のようにアンテナだけが超えるパートで頭の演技まで小さくしてしまうと、
    # 「誰が何を担当しているか見て分かる」という設計が壊れる。
    scales: dict[str, list[float]] = {}   # clip ref -> [head, antennas]

    for attempt in range(args.max_iter):
        lib = ClipLibrary(ctx.clips_dir)
        # 前回までに決めた縮小率を適用したライブラリを作る
        for ref, (kh, ka) in scales.items():
            lib._by_path[ref] = lib.get(ref).scaled(head=kh, antennas=ka)

        build = build_show(ctx, score, lib=lib, validate=True)
        failing = [bp for bp in build.parts if not bp.ok]
        if not failing:
            print(f"  every part passes (after {attempt} pass(es) of scaling)")
            break

        for bp in failing:
            if not bp.part or not bp.part.clip:
                bad(f"{bp.robot.id}: failing for a reason other than clip amplitude")
                continue
            ref = bp.part.clip
            st = bp.report.stats
            peak = st.get("joint_vel_peak", 0.0)
            rms = st.get("gesture_duty_rms", 0.0)
            ant_rms = st.get("antenna_duty_rms", 0.0)
            ant_peak = max(st.get("antenna_r_peak_vel", 0.0),
                           st.get("antenna_l_peak_vel", 0.0))

            kh = ka = 1.0
            if peak > lim.joint_velocity_max:
                kh = min(kh, (lim.joint_velocity_max * 0.88) / peak)
            if rms > lim.gesture_duty_max_rms:
                kh = min(kh, (lim.gesture_duty_max_rms * 0.88) / rms)
            if ant_peak > lim.antenna_velocity_max:
                ka = min(ka, (lim.antenna_velocity_max * 0.88) / ant_peak)
            if ant_rms > lim.antenna_duty_max_rms:
                ka = min(ka, (lim.antenna_duty_max_rms * 0.88) / ant_rms)

            if kh >= 1.0 and ka >= 1.0:
                bad(f"{bp.robot.id}/{ref}: not a velocity problem. {bp.report.worst(1)[0]}")
                continue
            cur = scales.setdefault(ref, [1.0, 1.0])
            cur[0] *= kh
            cur[1] *= ka
            print(f"  {bp.robot.id:<5} {ref:<18} head peak {peak:.2f} rms {rms:.2f}"
                  f" / ears peak {ant_peak:.2f} rms {ant_rms:.2f}"
                  f"  ->  head x{cur[0]:.3f} ears x{cur[1]:.3f}")
    else:
        bad("did not converge")

    if args.write and scales:
        lib = ClipLibrary(ctx.clips_dir)
        for ref, (kh, ka) in scales.items():
            clip = lib.get(ref)
            fitted = clip.scaled(head=kh, antennas=ka)
            fitted.description = (clip.description or "") + (
                f"\n\n[song fit] scaled down to stay inside the envelope in the "
                f"context of '{score.name}' (per-note overlap and the "
                f"secondary layers included): head x{kh:.3f} / "
                f"antennas x{ka:.3f}."
            )
            save_clip(fitted, clip.source_path)
            ok(f"written back: {ref} head x{kh:.3f} ears x{ka:.3f}")
    elif scales:
        print("\n(pass --write to save these)")
    return 0


def cmd_song_new(args) -> int:
    from .music.score import save_score_template

    ctx = _ctx(args)
    d = ctx.songs_dir / args.name
    if d.exists() and not args.force:
        die(f"{d} already exists (use --force to overwrite song.yaml)")
    (d / "audio").mkdir(parents=True, exist_ok=True)
    p = save_score_template(d / "song.yaml", [r.id for r in ctx.robots])
    print(f"  created {p}")
    print(f"  edit it, then: orchestra song build {args.name}")
    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def cmd_show_preflight(args) -> int:
    from .show.run import preflight

    ctx = _ctx(args)
    rep = preflight(ctx, _robots(ctx, args))
    print(rep.summary())
    return 0 if rep.ok else 1


def cmd_show_deploy(args) -> int:
    from .music.score import load_score
    from .show.deploy import deploy_show

    ctx = _ctx(args)
    score = load_score(_song_path(ctx, args.song))
    build_dir = ctx.build_dir / score.name
    if not build_dir.exists():
        die(f"nothing built for {score.name}. Run: orchestra song build {args.song}")

    audio_for = {
        rid: score.parts[pn].audio
        for pn, rid in score.cast.items()
        if pn in score.parts and score.parts[pn].audio
    }
    robots = _robots(ctx, args)

    # ★配る前に、ビルド済みプログラムが今の cast と合っているか確かめる★
    #
    # `song build` は検証に落ちたビルドを書かない（正しい）。だが `build/` には
    # **前回成功したビルドが残る**。cast を書き換えてビルドが落ちた直後に
    # deploy すると、**古い担当のモーションと新しい担当の音声**が同じ機体へ
    # 配られる ―― 指揮のプログラムでバラフォンの音を鳴らす、という状態になる。
    # エラーは出ないので、実機で見るまで気付かない。実際 2026-08-28 にやった。
    #
    # 音とモーションの対応は `anchors` で構造的に保証されている、というのが
    # この設計の柱なので、その前提が崩れる配布は止める。
    stale = []
    for r in robots:
        part = score.part_for_robot(r.id)
        prog = build_dir / f"{r.id}.program.json.gz"
        if part is None or not prog.exists():
            continue
        try:
            import gzip as _gzip
            import json as _json
            built = _json.loads(_gzip.open(prog, "rt", encoding="utf-8").read()).get("part", "")
        except Exception:
            continue
        # 指揮者のプログラムだけは `part` に "conductor" が入る
        # （`conductor.patterns.build_conductor_program` が手続き生成するため、
        #  song.yaml 上の名前 ―― この曲では "lead" ―― を持っていない）。
        # ここを素朴に名前比較すると、正しいビルドを止めてしまう。実際やった。
        from .music.score import KIND_CONDUCTOR
        expected = "conductor" if part.kind == KIND_CONDUCTOR else part.name
        if built and built != expected:
            stale.append(f"{r.id}: built for {built!r} but the cast now says "
                         f"{part.name!r} ({expected!r})")
    if stale:
        die("the build in {} does not match the current cast:\n  {}\n"
            "Run `orchestra song build {}` first -- and read its output: if the "
            "build FAILED validation, nothing was rewritten and what is on disk "
            "is the previous cast.".format(build_dir, "\n  ".join(stale), args.song))

    print(f"deploying {score.name} to {len(robots)} robot(s) ...")
    results = deploy_show(
        ctx, robots, build_dir,
        audio_dir=(score.source_dir / "audio") if score.source_dir else None,
        audio_for=audio_for,
    )
    failed = 0
    for rid in sorted(results):
        res = results[rid]
        if res.ok:
            ok(f"{rid}: {'; '.join(res.steps)}")
        else:
            failed += 1
            bad(f"{rid}: {res.error}")
    return 1 if failed else 0


def cmd_show_play(args) -> int:
    """SSH不要の代替経路で演奏する（upload -> play_uploaded_move）。

    エージェント経路が使えない間の実用解。実測ズレは約18ms で、打楽器が「同時」
    に聞こえる境界(10-20ms)のぎりぎり内側。より高精度が要るならエージェントを
    配備すること（`orchestra fleet provision`、SSH鍵が必要）。
    """
    from .fleet.health import fleet_health
    from .fleet.upload import ShowTarget, upload_and_play
    from .motion.program import Program
    from .music.score import load_score

    ctx = _ctx(args)
    score = load_score(_song_path(ctx, args.song))
    build_dir = ctx.build_dir / score.name
    if not build_dir.exists():
        die(f"{score.name} has not been built. Run: orchestra song build {args.song}")

    robots = _robots(ctx, args)

    # 不健全な機体は自動的に外す。1台落ちてもショーは続く、が設計方針。
    health = fleet_health([r.ip for r in robots], ctx.fleet.daemon_port)
    usable, skipped = [], []
    for r in robots:
        h = health[r.ip]
        if h.healthy:
            usable.append(r)
        else:
            skipped.append((r, h))

    # 除外は理由と「音楽的に何が欠けるか」まで出す。単に台数が減ったのではなく、
    # 特定の音が消えるので、聴いて「何かおかしい」となる前に知らせる。
    lost_pitches: list[str] = []
    for r, h in skipped:
        err = (h.raw_status or {}).get("error") or h.error or h.state
        part = score.part_for_robot(r.id)
        what = ""
        if part:
            from .music.midi import note_name

            if part.pitches:
                names = ", ".join(note_name(p) for p in part.pitches)
                lost_pitches.extend(note_name(p) for p in part.pitches)
                what = f" -- part '{part.name}' ({names}) will be missing"
            else:
                what = f" -- part '{part.name}' ({part.instrument or '?'}) will be missing"
        bad(f"skipping {r.id}: {err}{what}")

    if lost_pitches:
        print(f"  -> playing without {', '.join(sorted(set(lost_pitches)))} in the melody")
    if not usable:
        die("no usable robots")

    audio_for = {
        rid: score.parts[pn].audio
        for pn, rid in score.cast.items()
        if pn in score.parts and score.parts[pn].audio
    }

    targets = []
    for r in usable:
        p = build_dir / f"{r.id}.program.json.gz"
        if not p.exists():
            bad(f"{r.id}: no program ({p.name})")
            continue
        prog = Program.load(p)
        wav = None
        if not args.no_audio and (name := audio_for.get(r.id)) and score.source_dir:
            cand = score.source_dir / "audio" / name
            if cand.exists():
                wav = cand
        targets.append(ShowTarget(r.id, r.ip, prog, wav))

    if not targets:
        die("no programs to play")

    n_audio = sum(1 for t in targets if t.audio)
    print(f"{score.title}: {len(targets)} robot(s), "
          f"{max(t.program.duration for t in targets):.1f}s, "
          f"audio on {n_audio}")
    print("uploading (nothing is transferred once playing) ...")

    report = upload_and_play(
        targets,
        play_frequency=ctx.fleet.fps,
        initial_goto_duration=args.goto,
        disable_after=not args.hold,
        progress=lambda m: print(m),
    )

    for rid, err in report.get("errors", {}).items():
        bad(f"{rid}: {err}")

    for rid in sorted(report["uploads"]):
        u = report["uploads"][rid]
        extra = ""
        for e in u.events:
            extra = f"  + audio {e['audio']} ({e['kb']}KB, {e['chunks']} chunks)"
        ok(f"{rid}: {u.encoded_bytes / 1024:.0f}KB / {u.n_chunks} chunks{extra}")

    if (sp := report.get("fanout_spread_ms")) is not None:
        print(f"\nsend-side fan-out spread: {sp:.2f} ms")
        print("(measured acoustic spread is about 18 ms; "
              "measure it with tools/measure_skew.py)")
    print(f"played {report.get('duration_s', 0):.1f}s. "
          f"{'Torque held.' if args.hold else 'Parked.'}")
    return 1 if report.get("errors") else 0


LOOP_GAP_S = 5.0
"""ループ再生で1回と次の回のあいだに置く間。

就寝姿勢から次の回の開始姿勢へ戻る `goto`（arm 内、2秒）が入るので、ここを 0 に
すると礼の直後に頭が持ち上がって不自然に見える。観客が「終わった」と分かる間を
取るための値で、安全上の意味は無い。"""


ARROWS = ("up", "down", "left", "right")

_WIN_ARROW = {"H": "up", "P": "down", "K": "left", "M": "right"}
"""Windows コンソールが `\\x00` / `\\xe0` の次に返すコード。"""

_VT_ARROW = {"A": "up", "B": "down", "C": "right", "D": "left"}
"""ANSI エスケープ `\\x1b[` の次に来る文字。左右が Windows と逆順なので注意。"""


def _win_decode(ch: str | None, getch) -> str | None:
    """Windows コンソールの1文字を、必要なら2バイト目まで読んで解釈する。

    矢印とファンクションキーは `\\x00` か `\\xe0` の前置きで2バイト来る。
    **2バイト目は必ず読む** ―― 読み捨てないと次の1文字と誤認する。以前は
    読んだうえで捨てていたので、矢印がどこにも届かなかった。
    """
    if ch in ("\x00", "\xe0"):
        return _WIN_ARROW.get(getch())
    return ch


def _vt_decode(ch: str | None, getch) -> str | None:
    """ANSI エスケープを解釈する。矢印は `ESC [ A` の3バイト。

    ESC 単独（Esc キー）と区別が付かないので、続きは**短い待ち**で取りに行く
    ことを前提にしている。来なければ Esc として捨てる。
    """
    if ch != "\x1b":
        return ch
    if getch() != "[":
        return None
    return _VT_ARROW.get(getch() or "")


class KeyWatch:
    """演奏を止めずにキー入力を拾う。

    `input()` は使えない ―― 演奏の監視ループを丸ごとブロックしてしまう。
    別スレッドで拾い、呼び出し側は好きなときに見る。

    拾ったものは2種類に分けて扱う。

    **`s`（掛け金）** は `pressed` に記録するだけで、実際に止める判断は
    呼び出し側が曲の切れ目で行う。演奏の途中で切ると、ロボットは最後の姿勢の
    まま固まる（`show stop` を経由しないと中立へ戻らない）。

    **矢印（流れ）** は `on_arrow` に流す。音量調整に使うので、1回で見張りを
    降りては困る ―― 以前は最初の1致でスレッドごと終了していた。

    端末が無いとき（パイプ、cron、コンソールの取れない IDE）は黙って無効化
    する。キー入力が拾えないことは、演奏を止める理由にはならない。
    """

    def __init__(self, keys: str = "s") -> None:
        self.keys = set(keys.lower())
        self.pressed: str | None = None
        self.available = False
        """読み手を用意できたか。**一度決まったら変わらない。**

        起動時のバナーがこれを見て操作方法を出す。途中で端末が消えても倒さない
        ―― 「使えると案内したのに使えなかった」ではなく「使えていたが端末が
        消えた」であって、別の事実（`alive`）。"""
        self.alive = False
        """まだ読めているか。EOF や端末の消滅で False になる。"""
        self.needs_enter = False
        self.arrows = False
        """矢印が拾えるか。**行入力の端末では原理的に無理。**

        Enter を押すまでバイトが届かず、しかも端末の行編集がカーソル移動として
        食ってしまう。ここが False なら、音量操作は諦めるしかない。"""
        self.on_press = None       # 押された瞬間に呼ぶ。即座に手応えを返すため
        self.on_arrow = None       # "up" / "down" / "left" / "right"
        self._stop = False
        self._thread = None
        self._restore = None

    def __enter__(self) -> "KeyWatch":
        import threading

        # 上から順に、反応が速い方式を試す。最後の stdin はどこでも動くが
        # Enter が要る。
        reader = self._win_reader() or self._posix_reader()
        self.arrows = reader is not None
        if reader is None:
            reader = self._stdin_reader()
            self.needs_enter = reader is not None
        if reader is None:
            return self
        self.available = self.alive = True
        self._thread = threading.Thread(
            target=self._loop, args=(reader,), daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop = True
        self.alive = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._restore is not None:
            self._restore()

    def _loop(self, reader) -> None:
        while not self._stop:
            try:
                ch = reader()
            except Exception:
                # 端末が途中で消えた（リダイレクト、セッション切断、EOF）。
                # キーが拾えないだけなので、演奏には触らずに見張りだけ降りる。
                self.alive = False
                return
            if not ch:
                continue
            if ch in ARROWS:
                # 矢印は流し続ける。ここで return すると2回目以降が効かない。
                self._fire(self.on_arrow, ch)
            elif ch.lower() in self.keys:
                self.pressed = ch.lower()
                self._fire(self.on_press, self.pressed)

    @staticmethod
    def _fire(cb, arg) -> None:
        """コールバックの失敗で見張りを殺さない。

        描画で1回転んだせいで、以降のキーが全部効かなくなるほうが困る。"""
        if cb is None:
            return
        try:
            cb(arg)
        except Exception:
            pass

    # -- platform back ends ----------------------------------------------
    def _win_reader(self):
        """Win32 コンソールから1文字。Enter が要らない代わりに、**本物の
        コンソールが要る。**

        `msvcrt` はコンソールが無くても import でき、`kbhit()` は例外を出さずに
        0 を返し続ける ―― キーは stdin のパイプへ行くので永遠に拾えない。
        VS Code の統合ターミナル、ssh、パイプ経由が全部これ。`import` の成否で
        判定すると**黙って効かない**ので、コンソールの実在を Win32 に訊く。
        """
        try:
            import ctypes
            import msvcrt

            k = ctypes.windll.kernel32
            mode = ctypes.c_uint()
            if not k.GetConsoleMode(k.GetStdHandle(-10), ctypes.byref(mode)):
                return None
        except Exception:
            return None

        def read() -> str | None:
            if not msvcrt.kbhit():
                time.sleep(0.1)
                return None
            return _win_decode(msvcrt.getwch(), msvcrt.getwch)

        return read

    def _posix_reader(self):
        try:
            import os
            import select
            import termios
            import tty
        except ImportError:
            return None
        if not sys.stdin.isatty():
            return None

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        self._restore = lambda: termios.tcsetattr(
            fd, termios.TCSADRAIN, saved)

        def getch(timeout: float) -> str | None:
            """記述子から**直接** 1 バイト読む。

            `sys.stdin.read(1)` ではいけない。あれは緩衝された TextIOWrapper で、
            1文字返すために記述子からは**その時点で来ている分を全部**吸い上げる。
            矢印は `ESC [ A` の3バイトが一度に届くので、最初の `read(1)` が3バイト
            とも吸って `ESC` だけを返し、残る `[ A` は Python の緩衝に隠れる。次の
            `select` は記述子を見るから「何も来ていない」と答え、`_vt_decode` は
            続きを取れずに矢印を捨てる ―― `arrows=True` と案内しておきながら
            **黙って効かない**、Windows 側で一度やらかしたのと同じ壊れ方をする。
            記述子と `select` の見ているものを一致させるのが要点。
            """
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return None
            b = os.read(fd, 1)
            # 対象は制御キーと ASCII。多バイト文字は割れるが、`replace` で
            # 見張りを落とさない ―― キー1つの取りこぼしは演奏を止める理由に
            # ならない。
            return b.decode("utf-8", "replace") if b else None

        def read() -> str | None:
            return _vt_decode(getch(0.1), lambda: getch(0.05))

        return read

    def _stdin_reader(self):
        """行として読む。**どの端末でも動く代わりに Enter が要る。**

        コンソールが取れない環境（VS Code の統合ターミナル、ssh、パイプ）で
        使う最後の手段。`readline` は塞ぐが、このスレッドは daemon なので
        プロセスの終了は妨げない。
        """
        if sys.stdin is None or sys.stdin.closed:
            return None

        def read() -> str | None:
            line = sys.stdin.readline()
            if line == "":
                # EOF。読み続けると空文字で回り続けるので、見張りを降ろす。
                raise EOFError("stdin closed")
            line = line.strip().lower()
            return line[:1] if line else None

        return read


def _finish_recording(ctx, link, song_name: str) -> None:
    """録音を止め、WAV を PC に引き上げる。

    録音は WAV でロボット上に残す。エージェントの JSON チャンネルで
    100秒×16kHz×2ch を運ぶのは筋が悪いし、途中で切れたときに何も残らない。
    """
    from .fleet.ssh import SSHClient

    try:
        stats = link.call("record_stop")
    except Exception as e:
        bad(f"{link.robot_id}: cannot stop the recording: {e}")
        return
    if not stats.get("ok"):
        bad(f"{link.robot_id}: {stats.get('error', 'recording failed')}")
        return

    ok(f"{link.robot_id}: recorded {stats['seconds']}s "
       f"(peak {stats['peak']:.3f}, {stats['dbfs']} dBFS)")
    if stats.get("warning"):
        bad(f"  {stats['warning']}")

    robot = ctx.robots.get(link.robot_id)
    out = ctx.root / "recordings" / f"{song_name}-{link.robot_id}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = SSHClient(ctx.fleet, robot.ip).pull(stats["path"], out)
    if r.ok:
        ok(f"  pulled: {out}")
        print(f"  analyse with: orchestra calibrate skew {song_name} "
              f"--recording {out.name}")
    else:
        bad(f"  could not pull the file: {r.stderr.strip()[:160]}")


VOLUME_STEP = {"up": 3, "down": -3, "right": 1, "left": -1}
"""矢印1押しあたりの音量。↑↓ が普通の刻み、←→ が仕上げ用の細かい刻み。

0〜100 のパーセント。3 は「数回押せば体感で変わり、1回では行き過ぎない」幅。"""


def _volume_seed(ctx, robots) -> tuple[int, str]:
    """開始音量を決める。config が最優先、無ければ実機から読む。

    実機から読むのは `GET /api/volume/current` で、**これは音を鳴らさない。**
    鳴るのは POST /set のほうだけ。
    """
    from .show.volume import median, read_current

    saved = [c.volume for c in (Calibration.load(r.id, ctx.root / "config"
                                                 / "calibration")
                                for r in robots) if c.volume is not None]
    if saved:
        return int(median(saved)), "config"
    live = read_current(ctx, robots)
    if live:
        # 9台が食い違っていても、ここでは揃えにいかない。最初のキー操作で
        # 揃う ―― 起動時に黙って変えるより、押した瞬間に変わるほうが分かる。
        return int(median(live.values())), "robots"
    return 50, "default"


def _volume_save(ctx, robots, value: int) -> None:
    """音量を各機の校正ファイルへ書き戻す。

    `load` してから `save` するので `audio_lead_ms` などは保たれる。
    実際に変えたときしか呼ばない ―― 触っていない設定を書き換えない。
    """
    root = ctx.root / "config" / "calibration"
    for r in robots:
        cal = Calibration.load(r.id, root)
        cal.volume = int(value)
        cal.save(root)
    ok(f"volume {value} saved to {len(robots)} calibration file(s)")


def cmd_show_run(args) -> int:
    """Full performance: preflight, load, arm, sync, start, monitor, park."""
    from .music.score import load_score
    from .show.run import Show, preflight

    ctx = _ctx(args)
    score = load_score(_song_path(ctx, args.song))
    robots = _robots(ctx, args)

    if not args.skip_preflight:
        print("preflight:")
        rep = preflight(ctx, robots)
        print(rep.summary())
        if not rep.ok and not args.force:
            die("preflight failed -- fix the above, or pass --force")

    show = Show(ctx, robots)
    duration = 0.0

    try:
        print("\nconnecting to agents ...")
        errs = show.connect()
        for rid, e in errs.items():
            bad(f"{rid}: {e}")
        live = [l for l in show.fleet.links if l.connected]
        if not live:
            die("no agents reachable")
        show.fleet = type(show.fleet)(live)
        print(f"  {len(live)} agent(s) connected")

        # ★9台まとめて★ どちらも Fleet 側に並列版がある。逐次に回すと
        # 起動が台数に比例して延びる ―― arm は1台あたり 2秒の goto を含むので、
        # 9台順番だと 20秒近く待つことになる。表示は結果を受けてから出す。
        print("\nloading programs ...")
        for rid, (info, err) in sorted(show.fleet.load(show.program_for).items()):
            if err:
                bad(f"{rid}: {err}")
                continue
            duration = max(duration, float(info.get("duration", 0.0)))
            ok(f"{rid}: {info['n_frames']} frames, "
               f"{info['duration']}s, audio={info.get('audio') or '-'}")

        # --- 1回ぶんの演奏 ------------------------------------------------
        # arm から monitor までがループの単位。connect と load はプログラムが
        # 変わらないかぎり繰り返す必要がないので、外に置いてある。
        def one_take(take: int) -> "object":
            print("\narming (motors on, moving to start pose) ...")
            armed: list = []
            by_id = {l.robot_id: l for l in show.fleet.links}
            for rid, (info, err) in sorted(show.fleet.arm().items()):
                if err:
                    bad(f"{rid}: {err}")
                    continue
                armed.append(by_id[rid])
                note = ""
                if info.get("silent"):
                    note = "  NOTE: running silent (media failed to initialise)"
                elif info.get("media_backend") not in ("", "no_media"):
                    note = f"  media={info['media_backend']}"
                ok(f"{rid}: armed{note}")

            if not armed:
                die("no robot could be armed")
            if len(armed) < len(show.fleet.links):
                # arm できなかった機体に start を撃つと必ず失敗し、ログが二重に
                # 汚れるだけ。ここで対象から外す。
                dropped = [l.robot_id for l in show.fleet.links if l not in armed]
                print(f"  -> continuing without {', '.join(dropped)}")
                show.fleet = type(show.fleet)(armed)
            time.sleep(2.5)

            # 毎回取り直す。1回 148 秒なので、ループを回すほど時計はずれていく。
            # 同期は数秒で終わるうえ、ずれたまま走らせると打楽器がばらける。
            print("\nsyncing clocks ...")
            summary = show.sync_clocks()
            print(summary.summary())
            if not summary.ok and not args.force:
                die("clock sync failed on at least one robot -- refusing to start")

            # 指揮者のマイクで演奏そのものを録る。ここまでの指標（start_err,
            # frames, push_errors）はすべて**スピーカーの手前**までしか見ていない。
            # 実際に空気中で揃っているかは、この経路でしか分からない。
            rec_link = None
            rec_name = score.name if take == 1 else f"{score.name}-take{take}"
            if getattr(args, "record", None):
                rec_link = next((l for l in show.fleet.links
                                 if l.robot_id == args.record), None)
                if rec_link is None:
                    bad(f"{args.record} is not in this performance, so it cannot record")
                else:
                    try:
                        info = rec_link.call("record_start", name=rec_name,
                                             max_seconds=duration + 30.0)
                        ok(f"{args.record}: recording started "
                           f"{info.get('rate')}Hz/{info.get('channels')}ch")
                    except Exception as e:
                        bad(f"{args.record}: cannot start recording: {e}")
                        rec_link = None

            lead = args.lead if args.lead is not None else None
            print(f"\nSTART in {lead or ctx.fleet.show.get('start_lead_s', 3.0)}s ...")
            print("  (ctrl-C aborts gracefully; "
                  "`orchestra show estop` is the hard kill)")
            _t0, errs = show.start(lead_s=lead)
            for rid, e in errs.items():
                bad(f"{rid}: {e}")

            res = show.monitor(
                duration + float(ctx.fleet.show.get("start_lead_s", 3.0)))
            print()
            print(res.summary())

            if rec_link is not None:
                print()
                _finish_recording(ctx, rec_link, rec_name)
            return res

        # --- 操作卓 ----------------------------------------------------------
        # 単発でもループでも同じ。「プログラムが走っているあいだいつでも」なので、
        # --loop の有無で操作できたりできなかったりしてはいけない。
        import contextlib

        from .show.volume import VolumeController, bar

        loop = getattr(args, "loop", None)
        limit = loop if (loop or 0) > 0 else None
        # 参加している機体だけを相手にする。connect で落ちた台を含めると、
        # 音量を読むのに到達しない相手のタイムアウトを待つことになる。
        here = [r for r in robots
                if r.id in {l.robot_id for l in show.fleet.links}]

        with contextlib.ExitStack() as stack:
            keys = stack.enter_context(KeyWatch("sq"))
            # 押した瞬間に手応えを返す。次の回の頭まで無反応だと、効いたのか
            # 効いていないのか分からない ―― 1回 148 秒あるので致命的。
            keys.on_press = lambda ch: print(
                f"\n  [{ch}] got it -- stopping at the end of this take\n",
                flush=True)

            # ★音量の適用はキー入力の有無と無関係★
            #
            # ここは長らく `if keys.arrows:` の中にあった。矢印キーを出す端末で
            # しか音量を送らないという意味になっていて、非対話シェル・ssh -T・
            # CI から走らせると **音量が一度も送られない**。ロボットは直前に
            # 誰かが置いていった音量のまま鳴る。
            #
            # 2026-08-28、それで実際に「9台が完璧に同期して動いているのに音が
            # 一切出ない」公演が起きた。ログは 3268855 フレーム全部を push して
            # err 0 と報告し、WAV も ALSA ミキサーも正常で、原因の切り分けに
            # 分光解析まで要った。**保存した音量を適用しないなら、保存する
            # 意味が無い。** ライブで上下できるかどうかは別の話。
            seed, src = _volume_seed(ctx, here)

            def _draw(v: int) -> None:
                print(f"\r  volume {v:3d}  [{bar(v)}]      ",
                      end="", flush=True)

            # config から来た値は復元する（保存した理由がそれ）。実機から
            # 読んだ値は既にその状態なので送り返さない。
            vol = stack.enter_context(VolumeController(
                lambda: show.fleet, seed,
                apply_initial=(src == "config"),
                on_change=_draw if keys.arrows else None))
            if keys.arrows:
                keys.on_arrow = lambda a: vol.nudge(VOLUME_STEP.get(a, 0))
                tail = ("" if src == "config"
                        else "   -- the first press aligns all of them")
                print(f"\nvolume {seed} (from {src})   "
                      f"up/down +-{VOLUME_STEP['up']}   "
                      f"left/right +-{VOLUME_STEP['right']}{tail}")
            else:
                applied = "applied" if src == "config" else "already set on the robots"
                print(f"\nvolume {seed} (from {src}) -- {applied}. "
                      "this terminal has no arrow keys, so it cannot be nudged live")

            if keys.available and keys.needs_enter:
                print("press **s then Enter** to stop after the current take "
                      "(ctrl-C aborts immediately, mid-performance)")
            elif keys.available:
                print("press **s** to stop after the current take "
                      "(ctrl-C aborts immediately, mid-performance)")
            else:
                print("\nthis terminal gives no key input at all, "
                      "so stop it with ctrl-C")
            if limit:
                print(f"  stopping automatically after {limit} take(s)")

            take, rc = 0, 0
            while limit is None or take < limit:
                take += 1
                if loop is not None:
                    head = f" take {take}" + (f"/{limit}" if limit else "")
                    print(f"\n{'=' * 60}\n{head}\n{'=' * 60}")
                res = one_take(take)
                if res.failed:
                    rc = 1
                if loop is None:
                    break
                if keys.pressed:
                    print(f"\n[{keys.pressed}] stop requested -- "
                          f"finishing at take {take}")
                    break
                if limit is None or take < limit:
                    print(f"\n{LOOP_GAP_S:.0f}s until the next take "
                          f"(s to stop, ctrl-C to abort)")
                    time.sleep(LOOP_GAP_S)
                    if keys.pressed:
                        print(f"[{keys.pressed}] stop requested. "
                              f"Finishing at take {take}")
                        break
            if loop is not None:
                print(f"\nplayed {take} take(s)")
            if vol is not None and vol.touched:
                print()
                _volume_save(ctx, here, vol.value)
                for rid, e in sorted(vol.errors.items()):
                    bad(f"{rid}: volume: {e}")
            return rc

    except KeyboardInterrupt:
        print("\ninterrupted -- graceful abort (agents ramp to neutral, then park)")
        show.stop()
        time.sleep(1.5)
        return 130
    finally:
        if not args.no_park:
            print("\nparking ...")
            show.teardown(sleep=args.sleep)
        else:
            show.fleet.close()


def cmd_show_keys(args) -> int:
    """Report what this terminal can deliver, and echo keys so you can see it.

    Whether arrow keys arrive at all depends on the terminal, not on us, and
    finding out 148 seconds into a performance is the wrong time. This answers
    it in ten seconds with no robots involved.
    """
    print("checking what this terminal delivers ...\n")
    with KeyWatch("sq") as keys:
        if not keys.available:
            bad("no key input at all (piped or redirected stdin)")
            print("  live volume: NO      stop key: NO (use ctrl-C)")
            return 1
        if keys.arrows:
            ok("a real console: single keys arrive with no Enter")
            print("  live volume: YES     stop key: YES")
        else:
            bad("line input only: this terminal needs Enter after every key")
            print("  live volume: NO      stop key: YES (s then Enter)")
            print("\n  Arrow keys cannot work here -- the bytes do not arrive "
                  "until you press\n  Enter, and the terminal's own line "
                  "editing eats them as cursor moves.\n  A standalone "
                  "PowerShell or Windows Terminal window gives a real console.")

        seen: list[str] = []
        keys.on_arrow = lambda a: (seen.append(a),
                                   print(f"  arrow: {a}", flush=True))
        keys.on_press = lambda c: print(f"  key:   {c}", flush=True)
        secs = float(getattr(args, "seconds", 10.0) or 10.0)
        print(f"\npress the arrow keys for {secs:.0f}s ...")
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline and keys.alive:
            time.sleep(0.05)

        print()
        if keys.arrows and not seen:
            bad("no arrow key arrived. The console is there but the keys "
                "were not pressed, or something upstream is eating them")
            return 1
        if seen:
            ok(f"{len(seen)} arrow key(s) arrived: "
               f"{', '.join(sorted(set(seen)))}")
    return 0


def cmd_show_stop(args) -> int:
    from .show.run import make_fleet

    ctx = _ctx(args)
    robots = _robots(ctx, args)
    fleet = make_fleet(ctx, robots)
    fleet.connect()
    for rid, (_, err) in fleet.abort().items():
        (bad if err else ok)(f"{rid}: {err or 'aborting'}")
    fleet.close()
    return 0


def cmd_show_estop(args) -> int:
    """The panic button.

    Straight to each daemon over HTTP. Deliberately does NOT go through the
    agents, so it still works when an agent is hung or dead.
    """
    from .fleet.health import emergency_stop

    ctx = _ctx(args)
    robots = _robots(ctx, args)
    print("EMERGENCY STOP -- disabling motors on all selected robots")
    results = emergency_stop([r.ip for r in robots], ctx.fleet.daemon_port)
    failed = 0
    for r in robots:
        if results.get(r.ip):
            ok(f"{r.id} motors disabled")
        else:
            failed += 1
            bad(f"{r.id} DID NOT RESPOND -- cut power to this robot by hand")
    return 1 if failed else 0


def cmd_show_park(args) -> int:
    from .fleet.health import DaemonClient, parallel

    ctx = _ctx(args)
    robots = _robots(ctx, args)

    def park(r: RobotConfig) -> str:
        c = DaemonClient(r.ip, ctx.fleet.daemon_port, timeout=25.0)
        try:
            if args.sleep:
                c.goto_sleep()
            c.disable_motors()
            return "parked"
        finally:
            c.close()

    for r, res, err in parallel(robots, park):
        (bad if err else ok)(f"{r.id}: {err or res}")
    return 0


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------


def cmd_calibrate_audio(args) -> int:
    """Record the GStreamer->ALSA latency for one robot's local audio path."""
    ctx = _ctx(args)
    robot = ctx.robots.get(args.robot)
    cal = Calibration.load(robot.id, ctx.root / "config" / "calibration")
    cal.audio_lead_ms = args.lead_ms
    path = cal.save(ctx.root / "config" / "calibration")
    print(f"  {robot.id}: audio_lead_ms = {cal.audio_lead_ms}")
    print(f"  saved {path}")
    print("  Measure this by recording the robot with a phone: play a clip whose")
    print("  motion and sound should coincide, then read the offset off the")
    print("  waveform. Positive lead means audio starts EARLIER than motion.")
    return 0


def cmd_calibrate_skew(args) -> int:
    """録音から、9台の音が実際にどれだけ揃っていたかを出す。

    ここまでの指標（start_err、frames、push_errors）はすべて**スピーカーの
    手前**までしか見ていない。指令が揃っていたことしか言わない。この経路だけが
    「空気中で揃っていたか」を見る。
    """
    from .calibrate.skew import measure_skew, report
    from .music.score import load_score

    ctx = _ctx(args)
    song_dir = _song_path(ctx, args.song)
    score = load_score(song_dir)

    recs: list[Path] = []
    for name in args.recording:
        rec = Path(name)
        if not rec.is_absolute():
            cand = [ctx.root / "recordings" / name, ctx.root / name]
            rec = next((c for c in cand if c.exists()), cand[0])
        if not rec.exists():
            die(f"no recording at {rec}\n"
                f"  run this first: orchestra show run {args.song} --record r01")
        recs.append(rec)

    stems: dict[str, tuple[str, Path]] = {}
    for part_name, rid in score.cast.items():
        part = score.parts.get(part_name)
        if part is None or not part.audio:
            continue
        if rid == args.exclude:
            continue
        stems[rid] = (part_name, song_dir / "audio" / part.audio)

    distances = {}
    for item in (args.distance or []):
        if "=" not in item:
            die(f"--distance takes the form r0X=1.2, got: {item!r}")
        k, v = item.split("=", 1)
        distances[k] = float(v)

    print(f"reference stems: {len(stems)}")
    results = {}
    for rec in recs:
        # ファイル名の末尾がどの台で録ったかを示す（show run --record が付ける）
        who = rec.stem.rsplit("-", 1)[-1]
        print(f"\n=== {rec.name} ===")
        r = measure_skew(rec, stems, distances_m=distances or None, mode=args.mode)
        print(report(r, distances_used=bool(distances)))
        results[who] = r

    if len(results) > 1:
        from .calibrate.skew import compare

        print("\n=== cross-position comparison ===")
        print(compare(results))
    return 0 if any(r.usable for r in results.values()) else 1


def cmd_calibrate_show(args) -> int:
    ctx = _ctx(args)
    rows = []
    for r in ctx.robots:
        c = Calibration.load(r.id, ctx.root / "config" / "calibration")
        rows.append([r.id, r.instrument or "-", f"{c.audio_lead_ms:g}", c.notes or "-"])
    print(table(rows, ["robot", "instrument", "audio_lead_ms", "notes"]))
    print("\n  measure audio_lead_ms per robot and write it in. With no "
          "physical instruments, audio timing IS the performance.")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orchestra",
        description="Synchronized Reachy Mini orchestra",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--root", help="project root (defaults to the package's parent)")
    sub = p.add_subparsers(dest="group", required=True)

    def robot_arg(sp, help_="robot id(s), comma separated, or 'all'"):
        sp.add_argument("--robot", "-r", action="append", help=help_)

    # -- fleet ---------------------------------------------------------
    f = sub.add_parser("fleet", help="find, inspect and provision robots")
    fs = f.add_subparsers(dest="cmd", required=True)

    sp = fs.add_parser("status", help="health of every robot")
    robot_arg(sp)
    sp.set_defaults(func=cmd_fleet_status)

    sp = fs.add_parser("discover", help="sweep the subnet and reconcile IPs")
    sp.add_argument("--subnet")
    sp.add_argument("--write", action="store_true", help="update robots.yaml in place")
    sp.set_defaults(func=cmd_fleet_discover)

    sp = fs.add_parser("recover", help="restart a wedged daemon over SSH")
    robot_arg(sp)
    sp.add_argument("--api-fallback", action="store_true",
                    help="if systemctl fails, try the daemon's own restart endpoint")
    sp.set_defaults(func=cmd_fleet_recover)

    sp = fs.add_parser("provision", help="install the agent onto robots")
    robot_arg(sp)
    sp.set_defaults(func=cmd_fleet_provision)

    sp = fs.add_parser("agent", help="start/stop/inspect the on-robot agent")
    sp.add_argument("action", choices=["start", "stop", "log"])
    robot_arg(sp)
    sp.add_argument("--dry-run", action="store_true",
                    help="agent runs the protocol without touching the robot")
    sp.add_argument("--lines", type=int, default=50)
    sp.set_defaults(func=cmd_fleet_agent)

    sp = fs.add_parser(
        "selftest",
        help="self-test on real hardware: camera, speaker, microphone, motors",
    )
    robot_arg(sp)
    sp.add_argument("--json", action="store_true", help="also print the raw result")
    sp.add_argument("--serial", action="store_true",
                    help="one robot at a time (for rooms where distinct tones still bleed)")
    sp.add_argument("--tone", type=float,
                    help="same tone on every robot (combine with --serial to isolate)")
    sp.set_defaults(func=cmd_fleet_selftest)

    sp = fs.add_parser(
        "speaker", help="sound a test tone on each robot in turn, to check by ear")
    robot_arg(sp)
    sp.add_argument("--gap", type=float, default=3.0, help="gap between robots, in seconds")
    sp.add_argument("--volume", type=int, help="set the volume before sounding")
    sp.set_defaults(func=cmd_fleet_speaker)

    sp = fs.add_parser("stop-app", help="stop whatever app holds the robot lock")
    robot_arg(sp)
    sp.set_defaults(func=cmd_fleet_stop_app)

    # -- clip ----------------------------------------------------------
    c = sub.add_parser("clip", help="author and verify one motion")
    cs = c.add_subparsers(dest="cmd", required=True)

    sp = cs.add_parser("list", help="list the clip library")
    sp.set_defaults(func=cmd_clip_list)

    sp = cs.add_parser("check", help="run the safety gate at a given tempo")
    sp.add_argument("--clip", help="clip ref; omit to check the whole library")
    sp.add_argument("--bpm", type=float, action="append",
                    help="tempo to validate at (repeatable -- check the fastest "
                         "tempo you will actually play)")
    sp.add_argument("--loops", type=int, default=4)
    sp.add_argument("--verbose", "-v", action="store_true")
    sp.set_defaults(func=cmd_clip_check)

    sp = cs.add_parser(
        "fit", help="scale down to a safe amplitude, keeping shape and timing")
    sp.add_argument("--clip", help="omit to fit every clip")
    sp.add_argument("--bpm", type=float, action="append",
                    help="target tempo(s); the fastest one has to pass")
    sp.add_argument("--loops", type=int, default=4)
    sp.add_argument("--max-iter", type=int, default=8)
    sp.add_argument("--write", action="store_true", help="write the result back into the clip")
    sp.set_defaults(func=cmd_clip_fit)

    sp = cs.add_parser("plot", help="joint traces and velocity headroom")
    sp.add_argument("--clip", required=True)
    sp.add_argument("--bpm", type=float, action="append")
    sp.add_argument("--loops", type=int, default=4)
    sp.add_argument("--out")
    sp.add_argument("--show", action="store_true")
    sp.set_defaults(func=cmd_clip_plot)

    sp = cs.add_parser("preview", help="play a clip on ONE robot")
    sp.add_argument("--clip", required=True)
    sp.add_argument("--robot", "-r", required=True)
    sp.add_argument("--bpm", type=float, action="append")
    sp.add_argument("--loops", type=int, default=4)
    sp.add_argument("--force", action="store_true",
                    help="send even if validation failed (you had better be watching)")
    sp.add_argument("--transport", choices=["sdk", "upload"], default="upload",
                    help="upload: leaves media alone (default). sdk: uses the SDK's "
                         "play_move, which releases media as a side effect")
    sp.add_argument("--goto", type=float, default=2.0,
                    help="seconds to move into the start pose (upload path only)")
    sp.add_argument("--hold", action="store_true",
                    help="keep torque on after finishing")
    sp.set_defaults(func=cmd_clip_preview)

    sp = cs.add_parser("record", help="capture hand-puppeteered motion")
    sp.add_argument("--robot", "-r", required=True)
    sp.add_argument("--out", required=True, help="path to write, e.g. clips/shaker/mine.clip.json")
    sp.add_argument("--seconds", type=float, default=6.0)
    sp.add_argument("--bpm", type=float, default=112.0,
                    help="the tempo you moved at; keyframes are stored in beats")
    sp.add_argument("--instrument", default="")
    sp.add_argument("--placement", choices=["pattern", "trigger"], default="pattern")
    sp.add_argument("--quantize", type=float, default=0.0,
                    help="snap keyframes to this beat grid, e.g. 0.25")
    sp.add_argument("--trim-start", type=float, default=0.0)
    sp.add_argument("--trim-end", type=float, default=0.0)
    sp.add_argument("--countdown", type=int, default=3)
    sp.set_defaults(func=cmd_clip_record)

    # -- song ----------------------------------------------------------
    s = sub.add_parser("song", help="compile a score into per-robot programs")
    ss = s.add_subparsers(dest="cmd", required=True)

    sp = ss.add_parser("info", help="describe a song and check its cast")
    sp.add_argument("song")
    sp.set_defaults(func=cmd_song_info)

    sp = ss.add_parser("build", help="compile and validate the whole show")
    sp.add_argument("song")
    sp.add_argument("--report", action="store_true", help="print validation detail")
    sp.add_argument("--verbose", "-v", action="store_true")
    sp.add_argument("--no-validate", action="store_true",
                    help="skip the safety gate (never do this for hardware)")
    sp.set_defaults(func=cmd_song_build)

    sp = ss.add_parser(
        "fit", help="fit clips inside the envelope in song context "
                            "(overlap and secondary layers included)")
    sp.add_argument("song")
    sp.add_argument("--max-iter", type=int, default=6)
    sp.add_argument("--write", action="store_true")
    sp.set_defaults(func=cmd_song_fit)

    sp = ss.add_parser("new", help="create a song skeleton")
    sp.add_argument("name")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_song_new)

    # -- show ----------------------------------------------------------
    sh = sub.add_parser("show", help="deploy and run a performance")
    shs = sh.add_subparsers(dest="cmd", required=True)

    sp = shs.add_parser("preflight", help="check everything before arming")
    robot_arg(sp)
    sp.set_defaults(func=cmd_show_preflight)

    sp = shs.add_parser("deploy", help="push programs and audio stems")
    sp.add_argument("song")
    robot_arg(sp)
    sp.set_defaults(func=cmd_show_deploy)

    sp = shs.add_parser(
        "play",
        help="play without SSH (upload -> play_uploaded_move, measured spread ~18ms)",
    )
    sp.add_argument("song")
    robot_arg(sp)
    sp.add_argument("--goto", type=float, default=2.5,
                    help="seconds to move into the start pose")
    sp.add_argument("--no-audio", action="store_true")
    sp.add_argument("--hold", action="store_true",
                    help="keep torque on after finishing (default is to park)")
    sp.set_defaults(func=cmd_show_play)

    sp = shs.add_parser("run", help="arm, sync, start, monitor, park (needs the agent)")
    sp.add_argument("song")
    robot_arg(sp)
    sp.add_argument("--lead", type=float, help="seconds until T0")
    sp.add_argument("--skip-preflight", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-park", action="store_true")
    sp.add_argument("--sleep", action="store_true", help="goto_sleep when parking")
    sp.add_argument("--loop", nargs="?", type=int, const=0, metavar="N",
                    help="repeat the performance; omit N for endless. Press s to stop "
                         "after the current take (ctrl-C aborts immediately)")
    sp.add_argument("--record", metavar="ROBOT",
                    help="record the performance on this robot's microphone (e.g. r01). "
                         "The WAV is pulled afterwards for `calibrate skew`")
    sp.set_defaults(func=cmd_show_run)

    sp = shs.add_parser(
        "keys", help="does this terminal deliver arrow keys? (no robots needed)")
    sp.add_argument("--seconds", type=float, default=10.0,
                    help="how long to listen for keys")
    sp.set_defaults(func=cmd_show_keys)

    sp = shs.add_parser("stop", help="graceful abort via the agents")
    robot_arg(sp)
    sp.set_defaults(func=cmd_show_stop)

    sp = shs.add_parser("estop", help="HARD stop: disable motors over HTTP")
    robot_arg(sp)
    sp.set_defaults(func=cmd_show_estop)

    sp = shs.add_parser("park", help="return to a safe pose and cut torque")
    robot_arg(sp)
    sp.add_argument("--sleep", action="store_true")
    sp.set_defaults(func=cmd_show_park)

    # -- calibrate -----------------------------------------------------
    cal = sub.add_parser("calibrate", help="measure per-robot values")
    cals = cal.add_subparsers(dest="cmd", required=True)

    sp = cals.add_parser("audio", help="set the measured audio lead")
    sp.add_argument("--robot", "-r", required=True)
    sp.add_argument("--lead-ms", type=float, required=True)
    sp.set_defaults(func=cmd_calibrate_audio)

    sp = cals.add_parser(
        "skew", help="measure arrival-time spread across the fleet from a "
                             "recording (the conductor's mic will do)")
    sp.add_argument("song")
    sp.add_argument("--recording", required=True, nargs="+",
                    help="WAV(s) pulled by show run --record. Pass two or more to "
                         "separate position effects from per-robot effects")
    sp.add_argument("--exclude", default=None,
                    help="exclude the recording robot itself (its own sound swamps the mic)")
    sp.add_argument("--distance", nargs="*", metavar="r0X=M",
                    help="distance to the microphone in metres; subtracts the flight time")
    sp.add_argument("--mode", choices=["envelope", "waveform"], default="envelope",
                    help="envelope: for music / waveform: for calibration tones (matched filter)")
    sp.set_defaults(func=cmd_calibrate_skew)

    sp = cals.add_parser("show", help="print all calibration values")
    sp.set_defaults(func=cmd_calibrate_show)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
