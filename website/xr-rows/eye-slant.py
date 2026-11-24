"""M112 (D131 3): does a carried Bot tip the way it is carried, by the slant of the line through its two eyes?

Reads `hem-measure.py`'s `measure.json` (each still's `eyePair`) of one `hem-row.mjs carry1` run (FLIP=1: a mirrored
Bot). A still's tip is the eye line's slant, + when its top leans the way the feet went on the screen, less the
middle of the slant once it stood upright again where the carry ended. Moving stills are those whose feet moved
since the still before; the bar reads their second half: their middle tip is at least MIN_DEG, and the eyes are
found in most of them. Writes
<dir>/slant.json and <dir>/slant.png (the most tipped moving still with the eye line drawn); prints PASS or FAIL
and ends 1 on FAIL.
usage: python3 eye-slant.py <still-dir> [...]
"""
import json
import sys

import numpy as np
from PIL import Image, ImageDraw

MIN_DEG = 2.0


def judge(d):
    m = json.load(open(f"{d}/measure.json"))
    frames = {f["name"]: f for f in json.load(open(f"{d}/lines.json"))["frames"]}
    rows = [r for r in m["rows"] if r["found"]]
    way = m["way"]
    tip = lambda r: way * r["eyePair"]["slantDeg"] if r.get("eyePair") else None
    fx = lambda r: frames[r["name"]]["feetPx"]["x"]
    moving = [r for k, r in enumerate(rows) if k > 0 and abs(fx(r) - fx(rows[k - 1])) > 0.5]
    # The upright slant is read where the carry ended, once it stood upright again (from 500 ms after the last move,
    # not the run's last two stills): the same place on the screen as the carry's last stills, so the lens's own
    # slant of a far, off-middle Bot is the same in both and drops out. The art's own slant drops out with it.
    last = rows.index(moving[-1]) if moving else len(rows)
    settled = [tip(r) for r in rows[last + 5:len(rows) - 2] if tip(r) is not None]
    base = float(np.median(settled)) if settled else None
    held = [tip(r) for r in rows if r["phase"] == "held" and tip(r) is not None]
    # The carry's second half: the tip has had 300 ms to come, and the screen place is near where it ended.
    seen = [r for r in moving[len(moving) // 2:] if tip(r) is not None]
    moving = moving[len(moving) // 2:]
    out = {"dir": d, "way": way, "baseDeg": base, "heldDeg": round(float(np.median(held)) - (base or 0.0), 2) if held else None, "settled": len(settled), "moving": len(moving), "eyesFound": len(seen), "minDeg": MIN_DEG,
           "tips": [{"ms": r["ms"], "phase": r["phase"], "tipDeg": round(tip(r) - (base or 0.0), 2)} for r in seen]}
    if base is None or not moving or len(seen) * 2 < len(moving):
        out["bars"] = "FAIL"
        out["why"] = "the eyes were not found once it stood upright after the carry, or in most of the carry's second half"
    else:
        mid = float(np.median([t["tipDeg"] for t in out["tips"]]))
        out["middleTipDeg"] = round(mid, 2)
        out["bars"] = "PASS" if mid >= MIN_DEG else "FAIL"
        out["why"] = f"the middle tip while carried is {mid:+.2f} degrees the way it went (>= {MIN_DEG})"
        most = max(seen, key=lambda r: tip(r))
        im = Image.open(f"{d}/{most['name']}.png").convert("RGB")
        p = most["eyePair"]
        ImageDraw.Draw(im).line([tuple(p["left"]), tuple(p["right"])], fill=(255, 0, 0), width=2)
        im.save(f"{d}/slant.png")
        out["still"] = {"name": most["name"], "ms": most["ms"], "eyePair": p}
    json.dump(out, open(f"{d}/slant.json", "w"), indent=1)
    return out


def main(dirs):
    worst = 0
    for d in dirs:
        out = judge(d)
        print(f"{'PASS' if out['bars'] == 'PASS' else 'FAIL'} {d}: {out['why']}; upright after {out['baseDeg']} ({out['settled']} stills), lifted before the carry {out['heldDeg']}, eyes in {out['eyesFound']} of {out['moving']} stills of the carry's second half")
        worst = worst if out["bars"] == "PASS" else 1
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) if sys.argv[1:] else "usage: python3 eye-slant.py <still-dir> [...]")
