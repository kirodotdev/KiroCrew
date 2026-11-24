"""DASH-8 (a): the feel of a moved Bot, from `hem-measure.py`'s pixels of one `hem-row.mjs` run.

Moving stills are those whose feet moved on the screen since the still before. Of those: the hem's most trail and the
head's mean lean (both in body heights, along the way it went). After the last move, 15 stills: how far the hem swings
past its place and how often it crosses it (a sign kept only past 0.01). Over the whole run: the blinks, as runs of
stills whose dark eye pixels fall under 40 % of the run's middle. Bars (D118 5): trail <= 0.3, swing <= 0.08, crossings <= 2.
usage: python3 feel-judge.py <still-dir> [...]   prints one line a dir and writes <still-dir>/feel.json
"""
import json
import sys

import numpy as np

# The most stills a blink lasts: 3 at the rows' 100 ms a still. A lid is down about 100 to 300 ms (DASH-15 A2).
BLINK_MOST = 3


def judge(d):
    m = json.load(open(f"{d}/measure.json"))
    frames = {f["name"]: f for f in json.load(open(f"{d}/lines.json"))["frames"]}
    rows = [dict(r) for r in m["rows"] if r["found"]]
    # One still alone that jumps (another thing of the Bot's colour in its box for one still: both builds show such
    # stills, w1170, w1171) is not the cloth: each still's hem is the middle of it and its two neighbours.
    raw = [r["hemAhead"] for r in rows]
    for k, r in enumerate(rows):
        r["hemRaw"] = raw[k]
        r["hemAhead"] = float(np.median(raw[max(0, k - 1):k + 2]))
    fx = lambda r: frames[r["name"]]["feetPx"]["x"]
    moving = [r for k, r in enumerate(rows) if k > 0 and abs(fx(r) - fx(rows[k - 1])) > 0.5]
    if not moving:
        return {"dir": d, "error": "no still moved"}
    last = rows.index(moving[-1])
    # Not the run's last two stills: the background there is the median of the last six, and the last still jumps on
    # both builds (w1170, w1171), cloth or no cloth.
    after = rows[last + 1:min(last + 16, len(rows) - 2)]
    trail = max(-r["hemAhead"] for r in moving)
    lean = float(np.mean([r["headAhead"] for r in moving]))
    swing = max([r["hemAhead"] for r in after] or [0.0])
    signs = [1 if r["hemAhead"] > 0.01 else -1 if r["hemAhead"] < -0.01 else 0 for r in after]
    s = [x for x in signs if x]
    crossings = sum(1 for a, b in zip(s, s[1:]) if a != b)
    # DASH-15 A2: a blink is a short run of stills whose eyes read shut, on a body that is seen, with open eyes in the
    # still before and the one after. A still whose body is mostly hidden (its own patch under 60 % of the run's middle
    # fill of its box) is not seen: a Bot or a board in front is no blink. A longer run is a face not seen (a Bot seen
    # from behind, w1284 walk-15: Atlas walks away from him), and a run whose seen eyes are under 8 px at the middle has
    # no eyes to judge: `blinks` is then None. Before this, every still counted, and the walk rows' "blinks" were the
    # stretches in which the Bot showed its back.
    tall = lambda r: max(1.0, frames[r["name"]]["feetPx"]["y"] - frames[r["name"]]["topPx"]["y"])
    fill = [r.get("bodyArea", 0) / tall(r) ** 2 for r in rows]
    fill_mid = float(np.median(fill)) if fill else 0.0
    seen = [x >= 0.6 * fill_mid for x in fill]
    eyes = [r.get("eyesPx", 0) for r in rows]
    seen_eyes = [e for e, s in zip(eyes, seen) if s]
    mid = float(np.median(seen_eyes)) if seen_eyes else 0.0
    low = [s and e < 0.4 * mid for e, s in zip(eyes, seen)]
    runs, k = [], 0
    while k < len(low):
        if not low[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(low) and low[j + 1]:
            j += 1
        runs.append((k, j))
        k = j + 1
    opened = lambda i: 0 <= i < len(rows) and seen[i] and not low[i]
    blink_runs = [(a, b) for a, b in runs if b - a + 1 <= BLINK_MOST and opened(a - 1) and opened(b + 1)]
    eyes_seen = mid >= 8
    blinks = len(blink_runs) if eyes_seen else None
    # Each blink's stills: its time and what the hand did then (held: lifted and still; walk: carried).
    shut_stills = [f"{rows[i]['ms']} ms {rows[i]['phase']}" for a, b in blink_runs for i in range(a, b + 1)] if eyes_seen else []
    out = {"dir": d, "moving_stills": len(moving), "trail": round(trail, 3), "lean_head_ahead": round(lean, 3),
           "swing_past": round(swing, 3), "crossings": crossings, "blinks": blinks, "shut_stills": shut_stills, "stills": len(rows), "eyes_px_middle": mid,
           "unseen_stills": sum(1 for s in seen if not s), "face_away_runs": sum(1 for a, b in runs if (a, b) not in blink_runs),
           "bars": "PASS" if trail <= 0.3 and swing <= 0.08 and crossings <= 2 else "FAIL"}
    json.dump(out, open(f"{d}/feel.json", "w"), indent=1)
    return out


def main(dirs):
    # Ends non-zero when any run is over D118 5's bars or could not be judged (M111), with one PASS or FAIL line a run.
    worst = 0
    for d in dirs:
        out = judge(d)
        print(json.dumps(out))
        ok = out.get("bars") == "PASS"
        why = out.get("error") or f"trail {out['trail']} (<= 0.3), swing {out['swing_past']} (<= 0.08), crossings {out['crossings']} (<= 2)"
        print(f"{'PASS' if ok else 'FAIL'} {d}: {why}")
        worst = worst if ok else 1
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) if sys.argv[1:] else "usage: python3 feel-judge.py <still-dir> [...]")
