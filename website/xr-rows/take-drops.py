"""D140 D: of the take's clicks whose down and up the build read on the form's tray, how many left it at rest.

Reads `taken-desk.mjs`'s `<name>-take-tries.json` (each try: the click as the pinch route read it, and where the tray
stood each of the six frames after it). A click on the tray that never makes it `taken` is a dropped press.
usage: python3 take-drops.py <tries.json> [...]   prints PASS or FAIL a file and ends 1 on a FAIL
"""
import json
import sys


def main(files):
    bad = 0
    for f in files:
        tries = json.load(open(f))
        clicks = [t for t in tries if (t.get("last") or {}).get("kind") == "click" and str((t.get("last") or {}).get("down", "")).startswith("dash:") and str((t.get("last") or {}).get("up", "")).startswith("dash:") and "track" in t]
        dropped = [t for t in clicks if "taken" not in t["track"]]
        ok = bool(clicks) and not dropped
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {f}: {len(clicks)} clicks with down and up on the tray, {len(dropped)} left it at rest")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) if sys.argv[1:] else "usage: python3 take-drops.py <tries.json> [...]")
