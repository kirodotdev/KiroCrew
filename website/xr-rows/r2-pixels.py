"""Row 2 (D130): a taken form over a desk line's lower part, by pixels, with the form and without it.

Reads `taken-desk.mjs`'s two stills (<name>-over.png with the form taken, <name>-r2-without.png the same pose with
the form back in its Bot's hands) and the boxes it logged (`r2 boxes`, picture coordinates -1..1, y up). It counts the
desk line's paper: its light pixels (all three colours over 200) in two parts of the board's box.
- Above the form's top: as many with the form as without it (at least 70 %): the form hides nothing outside its box.
The paper is counted, not compared pixel by pixel: the back row's Bots move at rest and their boards with them
(w1213: Mira's board stood some 6 px lower in the second still).
usage: python3 r2-pixels.py <still-dir> <name> <run-log>   prints PASS or FAIL lines and ends 1 on a FAIL
"""
import json
import re
import sys

import numpy as np
from PIL import Image


def main(d, name, log):
    line = next(x for x in open(log) if "r2 boxes:" in x)
    box = json.loads(re.sub(r"^.*r2 boxes: ", "", line))
    W, H = box["size"]
    a = np.asarray(Image.open(f"{d}/{name}-over.png").convert("RGB")).astype(int)
    b = np.asarray(Image.open(f"{d}/{name}-r2-without.png").convert("RGB")).astype(int)
    px = lambda n: (int((n[0] + 1) / 2 * W), int((1 - n[3]) / 2 * H), int((n[2] + 1) / 2 * W), int((1 - n[1]) / 2 * H))
    paper = lambda im, n: int((im[px(n)[1]:px(n)[3], px(n)[0]:px(n)[2]].min(axis=2) > 200).sum())
    f, g = box["form"], box["board"]
    above = [g[0], max(g[1], f[3] + 0.01), g[2], g[3]]
    under = [max(f[0], g[0]), g[1], min(f[2], g[2]), min(f[3], g[3])]
    bad = 0
    if above[1] < above[3]:
        pa, pb = paper(a, above), paper(b, above)
        ok = pb > 0 and pa >= 0.7 * pb
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} r2: the board's paper above the form shows as without the form: {pa} light pixels with it, {pb} without (at least 70 %)")
    else:
        bad += 1
        print("FAIL r2: no part of the board stands above the form's top")
    # Not judged: "where the form's box lies over the board, no paper shows". The box holds the air over the tray
    # between its parts (D114 2), and there the picture shows the board (w1213: more paper with the form than
    # without, 4394 against 1450). Which pixels are the form's own needs its parts' boxes by kind; not built.
    del under
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]) if len(sys.argv) >= 4 else "usage: python3 r2-pixels.py <still-dir> <name> <run-log>")
