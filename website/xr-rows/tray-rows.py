"""DASH-16 A (D192 1, by pixels): where a resting tray's rows stand in each crop of a `traypress SAMPLE=2` run.

The tray is its own dark grey in the crop (about 29,34,46): pixels with a luminance between LO and HI, above the
room's near-black and below the Bot's light body behind it (w1323: a light-pixel count read the body's edge, which
moves on its own, not the tray). Its rows are the crop's rows holding at least MIN of them. Each crop's top row,
bottom row and the grey pixels' middle row are printed, and how far each moved over the run, in pixels. The crops of
a run share one clip.
usage: python3 tray-rows.py <run>-pixels.json   (the crops lie beside it)
"""
import json
import os
import sys

import numpy as np
from PIL import Image

LO = 20
HI = 60
MIN = 20


def rows_of(path):
    a = np.asarray(Image.open(path).convert("RGB")).astype(float)
    lum = a @ np.array([0.299, 0.587, 0.114])
    grey = (lum > LO) & (lum < HI)
    on = grey.sum(axis=1) >= MIN
    ys = np.nonzero(on)[0]
    if len(ys) == 0:
        return None
    # The middle is weighed over the tray's own rows only: grey pixels outside them are the body's soft edge.
    w = np.where(on, grey.sum(axis=1), 0).astype(float)
    return {"top": int(ys[0]), "bottom": int(ys[-1]), "middle": round(float((np.arange(len(w)) * w).sum() / w.sum()), 2)}


def main(p):
    d = json.load(open(p))
    here = os.path.dirname(p)
    out = {"file": p, "pose": d["pose"], "onAtStart": d["onAtStart"]}
    for run in ("on", "off"):
        rs = [rows_of(os.path.join(here, r["file"])) for r in d[run]["rows"]]
        got = [r for r in rs if r]
        spread = lambda k: (max(r[k] for r in got) - min(r[k] for r in got)) if got else None
        out[run] = {"crops": len(rs), "found": len(got), "top_moved_px": spread("top"), "bottom_moved_px": spread("bottom"),
                    "middle_moved_px": round(spread("middle"), 2) if got else None, "leaves": d[run]["leaves"],
                    "dot_on": sorted(set(str(r["on"]) for r in d[run]["rows"])), "clip": d[run]["clip"]}
    print(json.dumps(out))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        main(p)
