#!/usr/bin/env python3
"""Measure real cross-robot synchronisation from an audio recording.

    python tools/measure_skew.py rehearsal.wav --bpm 112
    python tools/measure_skew.py rehearsal.wav --program build/small_world/r08.program.json.gz

WHY THIS TOOL EXISTS
Every sync number the software can report is a number about the software. The
agents can agree on a timebase to the microsecond and still be audibly ragged if
a clock offset was mis-measured, a beater is mis-calibrated, or one robot's
control loop is running slow. The only honest measurement of "are they together"
is a microphone in the room.

HOW TO RUN THE TEST
1. Build a program where every robot strikes on the same beat (the simplest is
   a one-bar pattern of quarter notes given to all of them).
2. Put ONE recorder roughly equidistant from the robots. A phone is fine; the
   analysis needs onset timing, not fidelity. Sound travels ~34 cm/ms, so keep
   the distances within ~30 cm of each other or geometry alone will show up as
   ~1 ms of apparent skew.
3. Run the show, record, export a mono or stereo WAV, and point this at it.

WHAT YOU GET
Per beat, the spread of detected onsets in that cluster -- that spread IS the
audible skew. Percussion attacks read as simultaneous below roughly 10-20 ms, so
that is the bar to clear.

Uses only numpy and the stdlib `wave` module.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(n)

    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        raise ValueError(f"unsupported sample width {width * 8} bit")
    data = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    if width == 1:
        data = (data - 128.0) / 128.0
    else:
        data /= float(np.iinfo(dtype).max)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def onset_envelope(x: np.ndarray, sr: int, hop: int = 128, win: int = 512):
    """Spectral flux: the sum of positive frame-to-frame magnitude changes.

    Percussive attacks are broadband, so summing only the *increases* across the
    spectrum tracks them far more cleanly than raw amplitude, which is dominated
    by whatever is still ringing from the previous hit.
    """
    n_frames = max(1, (len(x) - win) // hop)
    window = np.hanning(win)
    prev = np.zeros(win // 2 + 1)
    flux = np.zeros(n_frames)
    for i in range(n_frames):
        seg = x[i * hop : i * hop + win] * window
        mag = np.abs(np.fft.rfft(seg))
        flux[i] = np.sum(np.maximum(mag - prev, 0.0))
        prev = mag
    times = np.arange(n_frames) * hop / sr
    if flux.max() > 0:
        flux /= flux.max()
    return times, flux


def pick_onsets(times, flux, threshold=0.12, min_gap_s=0.05):
    """Local maxima above `threshold`, with parabolic sub-frame refinement.

    Sub-frame matters: at hop=128 and 44.1 kHz a frame is 2.9 ms, which is the
    same order as the skew being measured. Interpolating the peak recovers
    roughly a tenth of that.
    """
    out = []
    dt = times[1] - times[0] if len(times) > 1 else 0.0
    last = -1e9
    for i in range(1, len(flux) - 1):
        if flux[i] < threshold or flux[i] < flux[i - 1] or flux[i] < flux[i + 1]:
            continue
        if times[i] - last < min_gap_s:
            continue
        a, b, c = flux[i - 1], flux[i], flux[i + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        out.append(times[i] + np.clip(shift, -1, 1) * dt)
        last = times[i]
    return np.array(out)


def cluster(onsets: np.ndarray, expected: np.ndarray, window_s: float):
    """Group detected onsets around each expected beat."""
    groups = []
    for e in expected:
        members = onsets[np.abs(onsets - e) <= window_s]
        groups.append((e, members))
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", type=Path)
    ap.add_argument("--bpm", type=float, help="expected tempo, if using a beat grid")
    ap.add_argument("--program", type=Path,
                    help="a built .program.json.gz -- use its anchors as the grid")
    ap.add_argument("--offset", type=float, default=None,
                    help="seconds from the start of the recording to the first "
                         "beat (auto-detected if omitted)")
    ap.add_argument("--threshold", type=float, default=0.12)
    ap.add_argument("--window-ms", type=float, default=60.0,
                    help="how far from an expected beat an onset may be and "
                         "still count as belonging to it")
    args = ap.parse_args()

    x, sr = read_wav(args.wav)
    print(f"{args.wav.name}: {len(x) / sr:.2f}s at {sr} Hz")

    times, flux = onset_envelope(x, sr)
    onsets = pick_onsets(times, flux, args.threshold)
    print(f"detected {len(onsets)} onset(s) above threshold {args.threshold}")
    if len(onsets) < 2:
        print("\nToo few onsets. Try lowering --threshold, or check that the "
              "recording actually contains the performance.")
        return 1

    # -- build the expected grid ------------------------------------------
    if args.program:
        from orchestra.motion.program import Program

        p = Program.load(args.program)
        grid = p.contact_anchors if len(p.contact_anchors) else p.anchors
        if not len(grid):
            print("that program has no anchors to compare against")
            return 1
        grid = np.asarray(grid)
        label = f"anchors from {args.program.name}"
    elif args.bpm:
        spb = 60.0 / args.bpm
        span = onsets[-1] - onsets[0]
        grid = np.arange(0.0, span + spb, spb)
        label = f"{args.bpm:g} BPM grid"
    else:
        print("give either --bpm or --program so there is something to compare to")
        return 1

    # Align the grid to the first detected onset unless told otherwise.
    offset = args.offset if args.offset is not None else onsets[0] - grid[0]
    grid = grid + offset
    print(f"comparing against {label}, offset {offset:.3f}s\n")

    window = args.window_ms / 1000.0
    groups = cluster(onsets, grid, window)

    print(f"{'beat':>6}  {'expected':>9}  {'n':>3}  {'spread ms':>10}  {'mean err ms':>12}")
    print("-" * 52)

    spreads, errors, hit = [], [], 0
    for i, (e, members) in enumerate(groups):
        if len(members) == 0:
            print(f"{i:>6}  {e:>9.3f}  {'-':>3}  {'(no onset)':>10}")
            continue
        hit += 1
        spread = (members.max() - members.min()) * 1000 if len(members) > 1 else 0.0
        err = (members.mean() - e) * 1000
        spreads.append(spread)
        errors.append(err)
        flag = "  <-- WIDE" if spread > 20 else ""
        print(f"{i:>6}  {e:>9.3f}  {len(members):>3}  {spread:>10.2f}  {err:>12.2f}{flag}")

    if not spreads:
        print("\nno onsets matched the grid -- check --offset and --window-ms")
        return 1

    spreads = np.array(spreads)
    errors = np.array(errors)

    print("\n" + "=" * 52)
    print(f"beats matched:        {hit}/{len(grid)}")
    print(f"onset spread:         mean {spreads.mean():.2f} ms, "
          f"median {np.median(spreads):.2f} ms, worst {spreads.max():.2f} ms")
    print(f"timing error vs grid: mean {errors.mean():+.2f} ms, "
          f"sd {errors.std():.2f} ms, worst {np.abs(errors).max():.2f} ms")

    worst = spreads.max()
    print()
    if worst < 10:
        print(f"VERDICT: tight. {worst:.1f} ms worst-case spread is below the "
              "~10 ms threshold at which percussion attacks stop being "
              "separable by ear.")
    elif worst < 20:
        print(f"VERDICT: acceptable. {worst:.1f} ms is at the edge of audible "
              "for percussion. Worth chasing if the piece is exposed.")
    else:
        print(f"VERDICT: audibly loose ({worst:.1f} ms). Things to check, in "
              "order of how often they are the cause:")
        print("  1. Clock sync uncertainty -- `orchestra show sync` reports it "
              "per robot. Anything above a few ms means a congested link.")
        print("  2. Per-robot audio lead, if stems are involved "
              "(`orchestra calibrate audio`).")
        print("  3. Strike depth calibration -- a beater that travels further "
              "arrives later, and that is mechanical, not a timing bug.")
        print("  4. Control-loop health: `orchestra fleet status` shows the "
              "loop rate; anything below ~49 Hz means that robot is struggling.")
        print("  5. Microphone geometry -- 34 cm of extra distance is 1 ms.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
