"""DASH-7 (D118 4): where a carried Bot's head is against its feet, as pixels, in the stills of `hem-row.mjs lean`.

The background is the stills before the lift (the Bot then stands at its place, away from where it is carried). A Bot
pixel differs from it by more than DIFF and is near the Bot's own colour (read at its middle in the first still). The
head is the middle across of the top 15 % of the Bot's rows; it is said in body heights (feet to the top of the body
on the screen) to the right (+) or left (-) of the feet on the screen, less its mean in the stills before the lift.
usage: python3 lean-measure.py <still-dir> [...]   writes <still-dir>/lean.json and prints a line a still and a sum
"""
import json
import sys

import numpy as np
from PIL import Image

DIFF, TINT = 60, 70


def measure(d):
    lines = json.load(open(f"{d}/lines.json"))
    frames = [f for f in lines["frames"] if f.get("feetPx")]
    img = {f["name"]: np.asarray(Image.open(f"{d}/{f['name']}.png").convert("RGB")).astype(int) for f in frames}
    first = frames[0]
    # The background where the Bot is carried: the stills before the lift (it stands at its place then).
    bg = np.median(np.stack([img[f["name"]] for f in frames if f["phase"] == "before"]), axis=0)
    mx, my = int(first["feetPx"]["x"]), int((first["feetPx"]["y"] + first["topPx"]["y"]) / 2)
    tint = np.median(img[first["name"]][my - 4:my + 5, mx - 4:mx + 5].reshape(-1, 3), axis=0)
    h, w = bg.shape[:2]
    rows = []
    for f in frames:
        fx, fy, ty = f["feetPx"]["x"], f["feetPx"]["y"], f["topPx"]["y"]
        H = fy - ty
        x0, x1 = int(max(0, fx - 0.9 * H)), int(min(w, fx + 0.9 * H))
        y0, y1 = int(max(0, ty - 0.15 * H)), int(min(h, fy))
        part = img[f["name"]][y0:y1, x0:x1]
        near = np.abs(part - tint).max(axis=2) < TINT
        # Before the lift the Bot stands on its own background: its colour alone finds it.
        mask = near if f["phase"] == "before" else near & (np.abs(part - bg[y0:y1, x0:x1]).sum(axis=2) > DIFF)
        filled = np.nonzero(mask.sum(axis=1) >= 3)[0]
        if len(filled) < 5:
            rows.append({"name": f["name"], "ms": f["ms"], "phase": f["phase"], "found": False})
            continue
        top, bot = filled[0], filled[-1]
        band = mask[top:top + max(1, int(0.15 * (bot - top))) + 1]
        head = np.nonzero(band)[1].mean() + x0
        rows.append({"name": f["name"], "ms": f["ms"], "phase": f["phase"], "found": True, "bodyPx": int(bot - top), "head": float((head - fx) / H), "pose": f["pose"]})
    base = np.mean([r["head"] for r in rows if r["found"] and r["phase"] == "before"] or [0.0])
    for r in rows:
        if r["found"]:
            r["headRight"] = round(r["head"] - base, 3)
    json.dump({"dir": d, "dist": lines.get("dist"), "rows": rows}, open(f"{d}/lean.json", "w"), indent=1)
    return rows


for d in sys.argv[1:]:
    print(f"== {d}")
    rows = measure(d)
    for r in rows:
        if r["found"]:
            print(f"{r['name']} {r['ms']:5} {r['phase']:13} body {r['bodyPx']:4}px head {r['headRight']:+.3f}")
        else:
            print(f"{r['name']} {r['ms']:5} {r['phase']:13} not found")
    for ph in ("carried-left", "held-left", "carried-right", "held-right"):
        v = [r["headRight"] for r in rows if r["found"] and r["phase"] == ph]
        if v:
            print(f"sum {ph:13} head right of the feet: mean {np.mean(v):+.3f}, from {min(v):+.3f} to {max(v):+.3f} ({len(v)} stills)")
