#!/usr/bin/env python3
"""End-to-end synchronisation self-test, with no robots involved.

Run:  python tools/sync_selftest.py --agents 9

Spawns N agents in ``--dry-run`` (they speak the full protocol and run the real
playback loop, they just never touch a robot), then drives them through exactly
the sequence a show uses: connect, load, arm, clock-sync, scheduled start,
monitor. Finally it reports the spread of actual start instants.

What this DOES prove: the protocol, the clock-sync estimator, the scheduling
maths, the fan-out and the abort paths all work for N robots.

What this does NOT prove: real timing. Every agent here shares one machine's
clock and a loopback network, so the measured skew is a floor, not a prediction.
The real number has to come from a microphone -- see `tools/measure_skew.py` and
experiment N-4. Treat a good result here as "no logic errors", nothing more.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestra.config import FleetConfig  # noqa: E402
from orchestra.fleet.link import AgentLink, Fleet, Heartbeat  # noqa: E402
from orchestra.motion.compile import compile_clip_preview  # noqa: E402
from orchestra.motion.schema import ClipLibrary  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=9)
    ap.add_argument("--base-port", type=int, default=8900)
    ap.add_argument("--bpm", type=float, default=112.0)
    ap.add_argument("--loops", type=int, default=6)
    ap.add_argument("--clip", default="shaker/groove")
    ap.add_argument("--lead", type=float, default=3.0)
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    fleet_cfg = FleetConfig.load(root / "config" / "fleet.yaml")
    lib = ClipLibrary(root / "clips")
    clip = lib.get(args.clip)

    tmp = Path(tempfile.mkdtemp(prefix="orchestra_selftest_"))
    procs: list[subprocess.Popen] = []
    links: list[AgentLink] = []

    print(f"self-test with {args.agents} agent(s) in {tmp}\n")

    try:
        # -- spawn ---------------------------------------------------------
        for i in range(args.agents):
            rid = f"r{i + 1:02d}"
            base = tmp / rid
            (base / "programs").mkdir(parents=True, exist_ok=True)
            (base / "audio").mkdir(parents=True, exist_ok=True)

            res = compile_clip_preview(
                clip, bpm=args.bpm, loops=args.loops, fps=fleet_cfg.fps,
                neutral_antennas=fleet_cfg.neutral_antennas, robot_id=rid,
            )
            res.program.save(base / "programs" / f"{rid}.program.json.gz")

            port = args.base_port + i
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(root / "agent" / "orchestra_agent.py"),
                     "--dry-run", "--port", str(port), "--base-dir", str(base),
                     "--log-level", "WARNING"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            )
            links.append(AgentLink(rid, "127.0.0.1", port, timeout=10.0))

        time.sleep(2.0)
        fleet = Fleet(links)

        # -- connect / load / arm ------------------------------------------
        for rid, (_, err) in fleet.connect().items():
            if err:
                print(f"  FAIL connect {rid}: {err}")
                return 1
        print(f"connected: {len(fleet)} agent(s)")

        duration = 0.0
        for link in fleet.links:
            info = link.load(f"{link.robot_id}.program.json.gz")
            duration = max(duration, float(info["duration"]))
        print(f"loaded: {duration:.2f}s programs")
        fleet.arm()
        print("armed")

        # -- clock sync ----------------------------------------------------
        print("\nclock sync:")
        offsets = fleet.sync_clocks(probes=40, keep_best=8, probe_interval=0.005)
        for rid in sorted(offsets):
            print("  " + offsets[rid].summary())
        worst_unc = max(o.uncertainty_ms for o in offsets.values() if o.ok)
        print(f"  worst uncertainty: {worst_unc:.3f} ms")

        # -- scheduled start -----------------------------------------------
        # The heartbeat is not optional decoration. It keeps each agent's
        # deadman satisfied, and on real hardware it also keeps the Wi-Fi link
        # out of power-save so START never pays the measured 17-90 ms wake
        # penalty. Without it, agents correctly abort mid-piece on the
        # assumption the PC has gone away.
        heartbeat = Heartbeat(fleet, interval=1.0)
        heartbeat.start()

        print(f"\nscheduling T0 in {args.lead}s ...")
        t0, results = fleet.start_together(lead_s=args.lead)
        errs = {r: e for r, (_, e) in results.items() if e}
        if errs:
            print(f"  start errors: {errs}")
            return 1

        time.sleep(args.lead + duration + 1.5)
        heartbeat.stop()

        # -- collect -------------------------------------------------------
        print("\nresults:")
        start_errors = []
        for link in fleet.links:
            st = link.status()
            s = st.get("stats", {})
            se = s.get("start_error_ms")
            if se is not None:
                start_errors.append(float(se))
            print(
                f"  {link.robot_id}  {st['state']:<8} "
                f"start_err {se:>7} ms  "
                f"frames {s.get('frames_sent')}/{s.get('frames_expected')}  "
                f"late {s.get('late_ticks')} (worst {s.get('worst_late_ms')} ms)"
            )

        if start_errors:
            spread = max(start_errors) - min(start_errors)
            print(f"\n  START SPREAD ACROSS {len(start_errors)} AGENTS: "
                  f"{spread:.3f} ms")
            print(f"  (min {min(start_errors):+.3f}, max {max(start_errors):+.3f})")
            print("\n  Reminder: one machine, one clock, loopback. This is a floor,")
            print("  not a prediction. Measure the real number with a microphone.")

        n_done = sum(1 for l in fleet.links if l.status()["state"] == "done")
        print(f"\n  {n_done}/{len(fleet)} completed normally")

        # -- abort path ----------------------------------------------------
        print("\ntesting explicit abort ...")
        fleet.arm()
        hb = Heartbeat(fleet, interval=1.0)
        hb.start()
        fleet.start_together(lead_s=0.8)
        time.sleep(1.4)
        fleet.abort()
        time.sleep(1.5)
        hb.stop()
        states = {l.robot_id: l.status()["state"] for l in fleet.links}
        n_aborted = sum(1 for s in states.values() if s == "aborted")
        print(f"  {n_aborted}/{len(states)} reached `aborted`")

        # -- deadman -------------------------------------------------------
        # Pull the PC away mid-piece and confirm each agent parks itself rather
        # than freezing wherever it happened to be. On a struck instrument,
        # freezing at the contact point is exactly the sustained-stall condition
        # that heats a servo -- so "park" and "freeze" are not interchangeable.
        deadman_s = 5.0
        if duration <= deadman_s + 0.5:
            print("\nskipping deadman test: this program is only "
                  f"{duration:.1f}s, shorter than the {deadman_s:g}s deadman, so "
                  "it would finish before the timer could fire. Re-run with a "
                  "larger --loops to exercise it.")
            n_parked = len(fleet)
        else:
            print("\ntesting deadman (no heartbeat during playback) ...")
            fleet.arm()
            fleet.start_together(lead_s=0.5)
            time.sleep(0.5 + deadman_s + 2.0)
            states = {l.robot_id: l.status()["state"] for l in fleet.links}
            n_parked = sum(1 for s in states.values() if s == "aborted")
            print(f"  {n_parked}/{len(states)} parked after {deadman_s:g}s of "
                  f"silence (states: {sorted(set(states.values()))})")
            print("  -- they abort rather than freeze: a robot stopped at a "
                  "strike's contact point is a stalled servo.")

        ok = (n_done == len(fleet) and n_aborted == len(states)
              and n_parked == len(fleet))
        print(f"\n{'SELF-TEST PASSED' if ok else 'SELF-TEST FAILED'}")
        return 0 if ok else 1

    finally:
        for link in links:
            try:
                link.shutdown()
            except Exception:
                pass
            link.close()
        time.sleep(0.5)
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())
