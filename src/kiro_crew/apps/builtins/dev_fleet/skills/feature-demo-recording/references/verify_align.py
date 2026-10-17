#!/usr/bin/env python3
"""STEP 5 -- verify the cut numerically, before anyone watches it.

    python3 verify_align.py renders/out.mp4 --narr assets/audio/narr.json --events events.json

Four checks, each catching a failure this pipeline has actually shipped:

  drift    every beat's recorded timestamp vs the start of the narration line it
           belongs to. Duration-driven pacing accumulated ~3s here; absolute-target
           pacing holds it under a few hundred ms.

  audio    mean/max loudness. A composition whose <audio> element has no `id` is
           not discovered by the renderer and the file ships SILENT while every
           other check passes. A silent track reads about -91 dB.

  picture  mean luminance sampled in the slide windows and in the footage window.
           If the video element fails to seek under a screenshot-mode renderer the
           footage window comes out near-black while the slides look fine.

  streams  resolution, duration against the composition, and that an audio stream
           exists at all.

Exit code is non-zero if any check fails, so this can gate delivery.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pathcheck import open_media_input, read_json_input  # noqa: E402


def _home() -> pathlib.Path:
    """Resolved on call, never bound at import.

    A module-level Path.home() is a host-isolation hazard: any test that
    imports this module and reaches the binding writes the operator's real
    home, which is why the repo's isolation floor rejects it.
    """
    return pathlib.Path.home()


DRIFT_WARN = 1.0  # seconds; above this the cut looks out of sync
DRIFT_FAIL = 3.0
# Below this a window is effectively black. A designed dark slide still averages well
# above it, so the floor fires only on a genuinely black frame.
BLACK_MAX = 12.0
BRIGHT_MIN = 244.0  # above this it is blown out -- a white frame is as dead as a black one


def tool(name: str) -> str:
    found = shutil.which(name) or str(_home() / ".local/bin" / name)
    if pathlib.Path(found).exists():
        return found
    # The doctor accepts an imageio-provided ffmpeg, which is not on PATH. This
    # must accept the same one or a green doctor would still fail here.
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and pathlib.Path(exe).exists():
                return exe
        except Exception:
            pass
    raise SystemExit(f"{name} not found -- run references/deps.py --install")


def probe_streams(mp4: str) -> dict:
    out = subprocess.run(
        [
            tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            mp4,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def loudness(mp4: str) -> tuple[float | None, float | None]:
    out = subprocess.run(
        [tool("ffmpeg"), "-hide_banner", "-i", mp4, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    txt = out.stderr + out.stdout
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", txt)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", txt)
    return (float(mean.group(1)) if mean else None, float(peak.group(1)) if peak else None)


def luma(mp4: str, at: float) -> float | None:
    out = subprocess.run(
        [
            tool("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{at}",
            "-t",
            "0.4",
            "-i",
            mp4,
            "-vf",
            "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    m = re.search(r"YAVG=([\d.]+)", out.stderr + out.stdout)
    return float(m.group(1)) if m else None


def pair_beats(
    foot_rel: list[float], beat_rel: list[float], tags: list[int | None]
) -> tuple[list[int | None], str]:
    """Map each narration line to the beat that belongs to it.

    A beat may carry EXTRA marks -- a click plus a follow-up focus on what the click
    revealed -- so beats and lines are not 1:1 and pairing positionally reads those
    extras as multi-second drift. Prefer an explicit `beat` index written by the
    recorder; fall back to nearest-in-order matching and say the pairing was inferred.
    """
    if any(t is not None for t in tags):
        out: list[int | None] = [None] * len(foot_rel)
        for j, t in enumerate(tags):
            if t is not None and 0 <= t < len(out) and out[t] is None:
                out[t] = j
        return out, "explicit `beat` tags"

    out, used = [], 0
    for i, fr in enumerate(foot_rel):
        best, bestd = None, None
        for j in range(used, len(beat_rel)):
            if len(beat_rel) - j < len(foot_rel) - i:  # keep one beat per remaining line
                break
            d = abs(beat_rel[j] - fr)
            if bestd is None or d < bestd:
                best, bestd = j, d
        out.append(best)
        if best is not None:
            used = best + 1
    return out, "inferred (nearest-in-order); tag beats in the recorder to make it exact"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mp4")
    ap.add_argument("--narr", default="assets/audio/narr.json")
    ap.add_argument("--events", default="events.json")
    args = ap.parse_args()

    # Every input goes through the centralized gate, exactly as narrate.py and
    # compose.py do: this script is handed paths by an agent, and a verifier is
    # not a licence to read a protected file (or to hand one to ffmpeg).
    narr = read_json_input(args.narr)
    ev = read_json_input(args.events)
    # The media gate hands back a PRIVATE copy plus its cleanup, because ffprobe
    # and ffmpeg are subprocesses with their own descriptor tables and cannot use
    # a /proc/self/fd alias. Unpack both -- stringifying the pair silently feeds
    # ffprobe a tuple and every measurement comes back empty.
    mp4, _drop_copy = open_media_input(args.mp4)
    atexit.register(_drop_copy)
    mp4 = str(mp4)
    foot = [line for line in narr["lines"] if line["role"] == "footage"]
    intro = [line for line in narr["lines"] if line["role"] == "intro"]
    beats = sorted(ev["events"], key=lambda e: e["t_ms"])
    failures: list[str] = []

    print("== drift: beat timestamp vs its narration line ==")
    t0_line, t0_beat = foot[0]["start"], beats[0]["t_ms"] / 1000.0
    foot_rel = [line["start"] - t0_line for line in foot]
    beat_rel = [b["t_ms"] / 1000.0 - t0_beat for b in beats]
    mapping, how = pair_beats(foot_rel, beat_rel, [b.get("beat") for b in beats])
    print(f"  pairing: {how}")
    worst = 0.0
    for i, fr in enumerate(foot_rel):
        j = mapping[i] if i < len(mapping) else None
        if j is None:
            failures.append(f"no beat paired with narration line {i}")
            print(f"  beat {i}: line {fr:7.3f}s  -- unpaired  FAIL")
            continue
        d = beat_rel[j] - fr
        worst = max(worst, abs(d))
        # Anything past DRIFT_WARN is visible on screen, and the house budget is
        # a few hundred milliseconds -- a WARN that does not fail the gate lets a
        # visibly desynchronised cut exit 0, which is what the gate exists to stop.
        if abs(d) <= DRIFT_WARN:
            flag = "ok"
        elif abs(d) <= DRIFT_FAIL:
            flag = "FAIL"
        else:
            flag = "FAIL!"
        if flag != "ok":
            failures.append(f"beat {i} drifts {d:+.2f}s from its line")
        print(f"  beat {i}: line {fr:7.3f}s  beat {beat_rel[j]:7.3f}s  drift {d:+6.3f}s  {flag}")
    extras = len(beats) - sum(1 for m in mapping if m is not None)
    if extras > 0:
        print(f"  ({extras} extra mid-beat mark(s), not judged)")
    print(f"  worst drift {worst:.3f}s")

    silent = bool(narr.get("silent"))

    print("== audio ==")
    mean, peak = (None, None) if silent else loudness(mp4)
    if silent:
        print("  silent mode: no narration expected, check skipped")
    elif mean is None:
        failures.append("no audio measurement -- is there an audio stream?")
        print("  no measurement")
    else:
        silent = mean < -80
        if silent:
            failures.append(
                f"audio is effectively silent (mean {mean} dB) -- "
                "check the <audio> element has an id"
            )
        print(f"  mean {mean} dB, peak {peak} dB  {'SILENT' if silent else 'ok'}")

    print("== picture ==")

    # Sample INSIDE the line being judged: a fixed +1.5s offset leaves any line
    # shorter than that, so the probe lands on the next slide or the outro and a
    # black or broken segment is accepted.
    def _inside(line):
        dur = float(line.get("dur") or 0.0)
        return line["start"] + (min(1.5, dur / 2) if dur > 0 else 1.5)

    probes = []
    if intro:
        probes.append(("slide", _inside(intro[len(intro) // 2])))
    for i in (0, len(foot) // 2, len(foot) - 1):
        if 0 <= i < len(foot):
            probes.append((f"footage[{i}]", _inside(foot[i])))
    for label, at in probes:
        y = luma(mp4, at)
        if y is None:
            failures.append(f"could not sample luminance at {at:.1f}s")
            print(f"  {label:12s} t={at:7.2f}s  no data")
            continue
        dark = y < BLACK_MAX
        blown = y > BRIGHT_MIN
        bad = dark or blown
        if bad:
            how = "near-black" if dark else "blown out"
            failures.append(
                f"{label} at {at:.1f}s is {how} (YAVG {y:.1f}) -- "
                "did the video element fail to seek?"
            )
        if dark:
            verdict = "NEAR-BLACK"
        elif blown:
            verdict = "BLOWN-OUT"
        else:
            verdict = "ok"
        print(f"  {label:12s} t={at:7.2f}s  YAVG={y:7.2f}  {verdict}")

    print("== streams ==")
    info = probe_streams(mp4)
    dur = float(info.get("format", {}).get("duration", 0))
    kinds = {s["codec_type"]: s for s in info.get("streams", [])}
    if "audio" not in kinds and not silent:
        failures.append("no audio stream in the file")
    v = kinds.get("video", {})
    print(
        f"  video {v.get('codec_name')} {v.get('width')}x{v.get('height')}  "
        f"audio {kinds.get('audio', {}).get('codec_name')}  duration {dur:.2f}s"
    )
    expected = narr["total"] + 0.8
    if abs(dur - expected) > 1.5:
        failures.append(f"duration {dur:.2f}s does not match the composition ({expected:.2f}s)")
    # Printing the dimensions is not checking them: a render at the wrong size
    # still plays, and the mismatch only shows up as a letterboxed or cropped
    # film after someone has already published it.
    want = ev.get("viewport") or {}
    want_w, want_h = want.get("width"), want.get("height")
    got_w, got_h = v.get("width"), v.get("height")
    if want_w and want_h and (got_w, got_h) != (want_w, want_h):
        failures.append(
            f"video is {got_w}x{got_h} but the capture was {want_w}x{want_h} "
            "-- the render size does not match the recording"
        )

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("all checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
