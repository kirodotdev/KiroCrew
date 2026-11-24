"""DASH-7 (D115, row b): where a Bot's hem and head are against its feet, as pixels, in the stills of hem-row.mjs.

The background of each pixel is taken from stills in which the Bot stands elsewhere: before it moves for the pixels
nearer where it ends, after it stopped for the pixels nearer where it started. A Bot pixel differs from that by more
than DIFF. In a box round the feet, the head is the top 15 % of the Bot's rows and the hem its lowest 12 %; each
place is the middle of its Bot pixels across. Both are said in body heights (the feet to the top of the body, on
the screen), ahead (+) or behind (-) the feet along the way the feet went on the screen, less the same at rest
after the stop (the art and the lens put both a little off the feet even at rest).
usage: python3 hem-measure.py <still-dir> [<still-dir> ...]   writes <still-dir>/measure.json and prints a table
"""
import json
import sys

import numpy as np
from PIL import Image

DIFF = 60
TINT = 70


def patches_of(dark, mask):
    """The 4-joined patches of `dark` ((x, y) pixels) that the body closes round: of the pixels just outside a patch,
    at least 60 % are the Bot's own colour (`mask`). A gap of the room between the body and a flick is not."""
    h, w = mask.shape
    left, out = set(dark), []
    while left:
        seed = left.pop()
        patch, todo = [seed], [seed]
        while todo:
            x, y = todo.pop()
            for q in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if q in left:
                    left.discard(q)
                    patch.append(q)
                    todo.append(q)
        # Closed round: from the patch's middle, the Bot's colour lies to its left, right, above and below it (the
        # eye's own soft rim is neither dark nor the Bot's colour, so the pixels next to it cannot be asked).
        cx, cy = (int(round(v)) for v in np.mean(patch, axis=0))
        row, col = mask[cy], mask[:, cx]
        if row[:cx].any() and row[cx + 1:].any() and col[:cy].any() and col[cy + 1:].any():
            out.append(patch)
    return out


def body_of(mask, cx, cy):
    """The 4-joined patch of `mask` that holds the Bot's middle (`cx`, `cy`, box pixels), or the mask pixel nearest it.
    Its eyes are not in it (they are dark); the eye count reads each row from its leftmost to its rightmost pixel."""
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    out = np.zeros_like(mask)
    if len(xs) == 0:
        return out
    k = int(np.argmin((xs - cx) ** 2 + (ys - cy) ** 2))
    todo = [(int(xs[k]), int(ys[k]))]
    out[ys[k], xs[k]] = True
    while todo:
        x, y = todo.pop()
        for qx, qy in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= qx < w and 0 <= qy < h and mask[qy, qx] and not out[qy, qx]:
                out[qy, qx] = True
                todo.append((qx, qy))
    return out


def measure(d):
    lines = json.load(open(f"{d}/lines.json"))
    frames = [f for f in lines["frames"] if f.get("feetPx")]
    img = {f["name"]: np.asarray(Image.open(f"{d}/{f['name']}.png").convert("RGB")).astype(int) for f in frames}
    moved = [f for f in frames if f["phase"] not in ("before", "held")]
    first, last = frames[0], frames[-1]
    before = np.median(np.stack([img[f["name"]] for f in frames if f["phase"] == "before"]), axis=0)
    after = np.median(np.stack([img[f["name"]] for f in frames[-6:]]), axis=0)
    h, w = before.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    a, b = first["feetPx"], last["feetPx"]
    near_start = (xx - a["x"]) ** 2 + (yy - a["y"]) ** 2 < (xx - b["x"]) ** 2 + (yy - b["y"]) ** 2
    bg = np.where(near_start[..., None], after, before)
    way = np.sign(b["x"] - a["x"]) or 1.0
    # The Bot's own colour: the middle of its body in the first still. Only pixels near it are the Bot, so another
    # Bot of another colour that comes near, the ring at its feet and the sign over it are not taken for it.
    mx, my = int(first["feetPx"]["x"]), int((first["feetPx"]["y"] + first["topPx"]["y"]) / 2)
    tint = np.median(img[first["name"]][my - 4:my + 5, mx - 4:mx + 5].reshape(-1, 3), axis=0)
    rows = []
    for f in frames:
        fx, fy, ty = f["feetPx"]["x"], f["feetPx"]["y"], f["topPx"]["y"]
        H = fy - ty
        x0, x1 = int(max(0, fx - 0.9 * H)), int(min(w, fx + 0.9 * H))
        y0, y1 = int(max(0, ty - 0.15 * H)), int(min(h, fy))
        part = img[f["name"]][y0:y1, x0:x1]
        mask = (np.abs(part - bg[y0:y1, x0:x1]).sum(axis=2) > DIFF) & (np.abs(part - tint).max(axis=2) < TINT)
        filled = np.nonzero(mask.sum(axis=1) >= 3)[0]
        if len(filled) < 5:
            rows.append({"name": f["name"], "ms": f["ms"], "phase": f["phase"], "found": False})
            continue
        top, bot = filled[0], filled[-1]
        tall = bot - top
        # The eyes: dark pixels inside the body's own outline in its upper 45 % (between its leftmost and rightmost
        # pixel on each row). A shut eye is a thin line: far fewer of them.
        # DASH-15 A2: the outline is the Bot's own body, the one joined patch of its colour at the box's middle, not
        # every pixel of its colour in the box. Read across the whole box's colour, a row ran from the Bot to another
        # thing of a near colour (a Bot beside it, a board's frame) and counted the dark room between (w1284 walk-15
        # f003: 1458 "eye" pixels, most of them that room).
        body = body_of(mask, int(fx) - x0, int((fy + ty) / 2) - y0)
        eyes = 0
        dark = []
        lum = part.astype(float) @ np.array([0.299, 0.587, 0.114])
        for yy_ in range(top, top + max(1, int(0.45 * tall))):
            cols = np.nonzero(body[yy_])[0]
            if len(cols) >= 2:
                xs = np.nonzero(lum[yy_, cols[0]:cols[-1] + 1] < 45)[0] + cols[0]
                eyes += len(xs)
                dark += [(int(x), yy_) for x in xs]
        # M112: the two eyes are the two largest patches of those dark pixels that the body closes round: a patch that
        # reaches the box's edge is the room behind it. Each eye's middle, in the still's own pixels, and the slant of
        # the line through them (degrees, + when the right eye is lower).
        pair = None
        patches = [p for p in patches_of(set(dark), mask) if len(p) >= 3]
        patches.sort(key=len, reverse=True)
        if len(patches) >= 2 and len(patches[0]) <= 3 * len(patches[1]):
            a_, b_ = (np.array(p, dtype=float).mean(axis=0) for p in patches[:2])
            lc, rc = (a_, b_) if a_[0] < b_[0] else (b_, a_)
            pair = {"left": [round(lc[0] + x0, 1), round(lc[1] + y0, 1)], "right": [round(rc[0] + x0, 1), round(rc[1] + y0, 1)],
                    "slantDeg": round(float(np.degrees(np.arctan2(rc[1] - lc[1], rc[0] - lc[0]))), 2), "gapPx": round(float(rc[0] - lc[0]), 1),
                    "eyePx": [len(patches[0]), len(patches[1])]}
        band = lambda lo, hi: np.nonzero(mask[lo:hi + 1])[1].mean() + x0 if mask[lo:hi + 1].any() else np.nan
        head = band(top, top + int(0.15 * tall))
        hem = band(bot - int(0.12 * tall), bot)
        rows.append({"name": f["name"], "ms": f["ms"], "phase": f["phase"], "found": True, "bodyPx": int(tall), "feetX": round(fx, 1), "eyesPx": eyes, "eyePair": pair, "bodyArea": int(body.sum()),
                     "head": float((head - fx) * way / H), "hem": float((hem - fx) * way / H), "pose": f["pose"]})
    rest = [r for r in rows[-6:] if r["found"]]
    base_head = np.mean([r["head"] for r in rest]) if rest else 0.0
    base_hem = np.mean([r["hem"] for r in rest]) if rest else 0.0
    for r in rows:
        if r["found"]:
            r["headAhead"] = round(r["head"] - base_head, 3)
            r["hemAhead"] = round(r["hem"] - base_hem, 3)
            r["head"] = round(r["head"], 3)
            r["hem"] = round(r["hem"], 3)
    json.dump({"dir": d, "dist": lines.get("dist"), "mode": lines.get("mode"), "way": way, "rows": rows}, open(f"{d}/measure.json", "w"), indent=1)
    return rows


for d in sys.argv[1:]:
    print(f"== {d}")
    for r in measure(d):
        if not r["found"]:
            print(f"{r['name']} {r['ms']:5} {r['phase']:11} not found")
            continue
        p = r["pose"]
        line = f"| pose head {p['head']} yaw {p['turn']['yawDeg']} pitch {p['turn']['pitchDeg']} fov {p['view']['fovDeg']}" if "turn" in p else f"| pose eye {[round(v, 3) for v in p['eye']]} yaw {p['yawDeg']:.1f} pitch {p['pitchDeg']:.1f} lens {p['lensDeg']} (session)"
        print(f"{r['name']} {r['ms']:5} {r['phase']:11} body {r['bodyPx']:4}px feet x {r['feetX']:6} | head {r['headAhead']:+.3f} hem {r['hemAhead']:+.3f} (head-hem {r['headAhead'] - r['hemAhead']:+.3f}) {line}")
