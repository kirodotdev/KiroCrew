#!/usr/bin/env python3
"""Measure the rendered stroke width of the Crew Members mark against a Lucide glyph.

`capture-member-nav-glyph.mjs` writes `glyph-strip.png` at deviceScaleFactor 8:
one image holding the Lucide `MessageSquare` glyph (Sessions row) and the new
masked two-ghost pair mark (Crew Members row), both nominally `size={16}`.

At 8x a 1.33px stroke is ~10.7 device px, so it can be MEASURED rather than
eyeballed. For each glyph this scans every pixel row, finds runs of "inked"
pixels, and reports the modal run width -- the vertical strokes dominate, so the
mode is the stroke thickness. If the two glyphs agree, the new mark carries the
same optical weight as its neighbours, which is the claim under review.

Usage: python3 measure-glyph-stroke.py /abs/path/glyph-strip.png
"""
import sys
from collections import Counter

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment probe
    sys.exit("Pillow not available; install it or run under an interpreter that has it")


def runs_in_row(row, threshold):
    """Widths of consecutive inked pixels in one scanline."""
    out, run = [], 0
    for value in row:
        if value >= threshold:
            run += 1
        else:
            if run:
                out.append(run)
            run = 0
    if run:
        out.append(run)
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    img = Image.open(path).convert("RGBA")
    w, h = img.size

    # The rail is dark, the glyphs are light: use luminance distance from the
    # background (sampled at a corner) so this does not hardcode a theme colour.
    px = img.load()
    bg = px[0, 0]

    def ink(p):
        return max(abs(p[0] - bg[0]), abs(p[1] - bg[1]), abs(p[2] - bg[2]))

    ink_map = [[ink(px[x, y]) for x in range(w)] for y in range(h)]
    peak = max(max(r) for r in ink_map)

    # Two passes, with a LOCAL threshold per glyph. A single global threshold keyed
    # to the brightest glyph silently drops a dimmer one (the first run of this
    # script found only 1 of 2 bands, because an active rail row paints in accent
    # and an idle one in muted -- the idle glyph never cleared half the accent
    # peak). Pass 1 finds bands at a low threshold; pass 2 measures each band
    # against its OWN peak, so contrast between glyphs cannot bias the widths.
    detect = peak * 0.15
    inked_rows = [y for y in range(h) if any(v >= detect for v in ink_map[y])]
    if not inked_rows:
        sys.exit("no inked pixels found -- the glyphs did not render in this frame")
    bands, start, prev = [], inked_rows[0], inked_rows[0]
    for y in inked_rows[1:]:
        if y - prev > 3:
            bands.append((start, prev))
            start = y
        prev = y
    bands.append((start, prev))

    print(f"{path}  ({w}x{h}, peak ink {peak})")
    print(f"{len(bands)} glyph band(s) found\n")

    results = []
    for i, (top, bot) in enumerate(bands, 1):
        local_peak = max(max(ink_map[y]) for y in range(top, bot + 1))
        # Half local peak: above the antialiased skirt, below the solid core.
        threshold = local_peak * 0.5
        counter = Counter()
        # An earlier draft of the Crew Members mark carried TWO weights (a bubble
        # at the root's 2 with an inner ghost at 1.5); the shipped pair is a single
        # weight, and this tally is kept so a second weight creeping back is
        # visible here as well as in the unit test. A single modal
        # width would just report whichever perimeter is longer and hide the
        # relationship, so outer and interior runs are tallied separately. On a
        # scanline crossing the ghost the runs read
        # [bubble-left, ghost..., bubble-right], so first/last are the OUTLINE and
        # anything between them is INTERIOR detail.
        outer, inner = Counter(), Counter()
        for y in range(top, bot + 1):
            runs = runs_in_row(ink_map[y], threshold)
            for r in runs:
                counter[r] += 1
            if len(runs) >= 3:
                outer[runs[0]] += 1
                outer[runs[-1]] += 1
                for r in runs[1:-1]:
                    inner[r] += 1
        if not counter:
            continue
        # The modal run is the stroke; ignore 1-2px specks from corner antialiasing.
        modal = Counter({k: v for k, v in counter.items() if k > 2})
        if not modal:
            continue
        stroke, count = modal.most_common(1)[0]
        results.append(stroke)
        print(f"glyph {i}: rows {top}-{bot} (h={bot - top + 1}px)  "
              f"local peak {local_peak}, threshold {threshold:.0f}  "
              f"modal stroke = {stroke}px  (seen {count}x)")

        def modal_of(c):
            trimmed = Counter({k: v for k, v in c.items() if k > 2})
            return trimmed.most_common(1)[0] if trimmed else None

        om, im = modal_of(outer), modal_of(inner)
        if om and im and om[0] != im[0]:
            print(f"           outline stroke = {om[0]}px (seen {om[1]}x), "
                  f"interior stroke = {im[0]}px (seen {im[1]}x)  "
                  f"-> interior is {im[0] / om[0]:.2f}x the outline")
        elif om and im:
            print(f"           outline and interior both {om[0]}px (single weight)")

    if len(results) >= 2:
        spread = max(results) - min(results)
        print(f"\nstroke widths: {results}   spread = {spread}px")
        # One device pixel at 8x is 0.125 CSS px; anything inside that is
        # antialiasing, not a weight difference.
        verdict = "MATCH" if spread <= 1 else "MISMATCH"
        print(f"verdict: {verdict} (>=2px spread would be a visible weight difference)")
        return 0 if spread <= 1 else 1
    print("\nonly one glyph band -- nothing to compare against")
    return 1


if __name__ == "__main__":
    sys.exit(main())
