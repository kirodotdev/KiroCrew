"""M115 (D137): the dot lay on a DRAWN form, by pixels.

For each `m115 pixels` line of a `dash4-taken.mjs` run (ONLY=m115): the still with Atlas's form taken and the dot on
it, and the same head pose once the form is back in its Bot's hands. The patch round the lens's middle (where the
dot is) must change in most of its pixels: the form was drawn there, not only its box.
usage: python3 m115-pixels.py <still-dir> <run-log>   prints PASS or FAIL lines and ends 1 on a FAIL
"""
import json
import re
import sys

import numpy as np
from PIL import Image

HALF = 10
OUT = 20


def main(d, log):
    bad = 0
    for line in open(log):
        if "m115 pixels:" not in line:
            continue
        x = json.loads(re.sub(r"^.*m115 pixels: ", "", line))
        a = np.asarray(Image.open(f"{d}/{x['with']}").convert("RGB")).astype(int)
        b = np.asarray(Image.open(f"{d}/{x['without']}").convert("RGB")).astype(int)
        h, w = a.shape[:2]
        cy, cx = h // 2, w // 2
        # A ring round the dot, not the dot: the dot is drawn over everything, the same in both stills (w1207).
        yy, xx = np.mgrid[-OUT:OUT + 1, -OUT:OUT + 1]
        ring = (np.hypot(yy, xx) > HALF) & (np.hypot(yy, xx) <= OUT)
        pa, pb = a[cy - OUT:cy + OUT + 1, cx - OUT:cx + OUT + 1], b[cy - OUT:cy + OUT + 1, cx - OUT:cx + OUT + 1]
        share = float((np.abs(pa - pb).max(axis=2) > 30)[ring].mean())
        # The dot's place at full size, with the form and without it, side by side (D141: a crop settles what a still
        # shows at the dot).
        C = 120
        crop = np.concatenate([a[cy - C:cy + C, cx - C:cx + C], np.full((2 * C, 6, 3), 255), b[cy - C:cy + C, cx - C:cx + C]], axis=1).astype(np.uint8)
        Image.fromarray(crop).save(f"{d}/{x['with'].replace('.png', '')}-dot-crop.png")
        ok = share > 0.5
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} m115 pixels ({x['kind']}): the ring {HALF} to {OUT} px round the dot changes when the form goes back: {share:.2f} of it (> 0.5), {x['with']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]) if len(sys.argv) >= 3 else "usage: python3 m115-pixels.py <still-dir> <run-log>")
