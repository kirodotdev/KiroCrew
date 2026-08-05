#!/usr/bin/env python3
"""
camera.py — turn the harness event log into auto-zoom camera keyframes.

Ported from preston176/screen-demo-skill's camera.py, adapted to our pipeline:
  - We log EXACT click focal points (cx,cy in px) — more accurate than back-inferring
    a focal point from a bbox — plus the clicked element's size for the zoom level.
  - Each click -> a "punch-in" keyframe (zoom to the click point); between clicks the
    camera springs back to a wide shot in the post-processor.

Zoom level (from screen-demo-skill): smaller targets zoom in more, capped at max_zoom.
    target_frac = max(bbox_w/vw, bbox_h/vh)
    zoom        = min(max_zoom, max(1.0, 0.30 / target_frac))   # target fills ~30% of frame

Output camera.json:
    {
      "viewport": {"width", "height"},
      "default_zoom": 1.0,
      "max_zoom": 1.6,
      "keyframes": [ {"t_ms", "zoom", "focal": {"x","y"} (normalized 0..1), "label"} ]
    }

Usage:  python camera.py --events events.json --out camera.json [--max-zoom 1.6]
"""
import argparse
import json

DEFAULT_MAX_ZOOM = 2.0
TARGET_FILL = 0.34


def keyframe_for(ev, max_zoom):
    vw = ev["viewport"]["width"]
    vh = ev["viewport"]["height"]
    fx = min(1.0, max(0.0, ev["focal"]["x"] / vw))
    fy = min(1.0, max(0.0, ev["focal"]["y"] / vh))
    w = ev.get("bbox", {}).get("w", 0.0)
    h = ev.get("bbox", {}).get("h", 0.0)
    target_frac = max(w / vw if vw else 0.0, h / vh if vh else 0.0)
    if target_frac <= 0:
        zoom = min(max_zoom, 1.4)
    else:
        zoom = min(max_zoom, max(1.0, TARGET_FILL / target_frac))
    return {
        "t_ms": ev["t_ms"],
        "zoom": round(zoom, 3),
        "focal": {"x": round(fx, 4), "y": round(fy, 4)},
        "label": ev.get("label") or ev.get("kind") or "moment",
    }


def build(events_doc, max_zoom=DEFAULT_MAX_ZOOM):
    vp = events_doc.get("viewport", {"width": 1600, "height": 1000})
    evs = events_doc.get("events", [])
    kfs = [keyframe_for(ev, max_zoom) for ev in evs if ev.get("kind") == "click"]
    spans = [{"start_ms": ev["t_ms"], "end_ms": ev["t_ms"] + ev.get("dur_ms", 3000),
              "label": ev.get("label", "")}
             for ev in evs if ev.get("kind") == "caption"]
    return {"viewport": vp, "default_zoom": 1.0, "max_zoom": max_zoom,
            "keyframes": kfs, "live_spans": spans}


def main():
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _pathcheck import read_json_input, safe_open_output  # noqa: E402

    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-zoom", type=float, default=DEFAULT_MAX_ZOOM)
    ap.add_argument("--workdir", default=os.getcwd(),
                    help="Output confinement directory (default: CWD)")
    a = ap.parse_args()
    doc = read_json_input(a.events)
    cfg = build(doc, a.max_zoom)
    with safe_open_output(a.out, workdir=a.workdir) as f:
        json.dump(cfg, f, indent=2)
    print(f"camera: {len(cfg['keyframes'])} keyframes -> {a.out}")


if __name__ == "__main__":
    main()
