#!/usr/bin/env python3
# mypy: ignore-errors
"""
postprocess.py — turn the raw webm + event log into a polished, auto-zoomed mp4.

Pipeline (all local, no Node/Remotion, no cloud):
  webm frames --> spring-eased zoom/pan around each click's focal point --> h264 mp4

Camera model (faithful to preston176/screen-demo-skill's CameraComposition.tsx):
  - Each click keyframe defines a pose (zoom, focal). The camera springs WIDE->pose
    starting LEAD_MS before the click (punch-in), HOLDS, then springs pose->WIDE
    (punch-out). Overlapping windows: the later pose wins.
  - Between poses, the value is spring_interpolate(prev_pose -> cur_pose) over
    TRANSITION_FRAMES, then holds.
  - Zoom/pan is applied as a CROP: at zoom z, focal (fx,fy), the visible source rect
    is (W/z, H/z) centered on the focal point (clamped to bounds), resized to (W,H).

Usage:
  python postprocess.py --webm in.webm --camera camera.json --out demo.mp4 \
      [--speed 1.0] [--lead-ms 280] [--hold-ms 1400] [--transition-frames 18] [--max-zoom 1.6]
"""
import argparse

import imageio.v2 as imageio
import numpy as np
from PIL import Image
from spring import spring_interpolate

WIDE = (1.0, 0.5, 0.5)


def build_poses(keyframes, fps, lead_ms, hold_ms):
    poses = [(0, WIDE)]
    for kf in keyframes:
        t = kf["t_ms"]
        pose = (kf["zoom"], kf["focal"]["x"], kf["focal"]["y"])
        in_f = max(0, round((t - lead_ms) / 1000.0 * fps))
        out_f = round((t + hold_ms) / 1000.0 * fps)
        poses.append((in_f, pose))
        poses.append((out_f, WIDE))
    poses.sort(key=lambda p: p[0])
    merged = {}
    for f, p in poses:
        merged[f] = p
    return sorted(merged.items())


def pose_at(frame, poses, fps, transition_frames):
    idx = 0
    for i, (af, _) in enumerate(poses):
        if af <= frame:
            idx = i
        else:
            break
    cur_f, cur = poses[idx]
    prev = poses[idx - 1][1] if idx > 0 else cur
    local = frame - cur_f
    z = spring_interpolate(local, fps, prev[0], cur[0], transition_frames)
    fx = spring_interpolate(local, fps, prev[1], cur[1], transition_frames)
    fy = spring_interpolate(local, fps, prev[2], cur[2], transition_frames)
    return z, fx, fy


def live_windows(keyframes, fps, lead_ms, hold_ms, spans=None):
    wins = []
    for kf in keyframes:
        t = kf["t_ms"]
        wins.append((max(0, round((t - lead_ms) / 1000.0 * fps)),
                     round((t + hold_ms) / 1000.0 * fps)))
    for sp in (spans or []):
        wins.append((max(0, round(sp["start_ms"] / 1000.0 * fps)),
                     round(sp["end_ms"] / 1000.0 * fps)))
    wins.sort()
    merged = []
    for w in wins:
        if merged and w[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], w[1]))
        else:
            merged.append(list(w))
    return [tuple(w) for w in merged]


def is_live(frame, windows):
    return any(a <= frame <= b for a, b in windows)


def apply_zoom(img, zoom, fx, fy):
    if zoom <= 1.0001:
        return img
    W, H = img.size
    cw, ch = W / zoom, H / zoom
    cx, cy = fx * W, fy * H
    left = min(max(cx - cw / 2, 0), W - cw)
    top = min(max(cy - ch / 2, 0), H - ch)
    crop = img.crop((round(left), round(top), round(left + cw), round(top + ch)))
    return crop.resize((W, H), Image.LANCZOS)


def main():
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _pathcheck import (  # noqa: E402
        open_media_input,
        read_json_input,
        safe_open_output,
        safe_output_path,
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--webm", required=True)
    ap.add_argument("--camera", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--lead-ms", type=int, default=850)
    ap.add_argument("--hold-ms", type=int, default=1700)
    ap.add_argument("--transition-frames", type=int, default=26)
    ap.add_argument("--out-fps", type=int, default=30)
    ap.add_argument("--dead-air-speed", type=float, default=6.0)
    ap.add_argument("--workdir", default=os.getcwd(),
                    help="Output confinement directory (default: CWD)")
    a = ap.parse_args()

    cam = read_json_input(a.camera)
    out_path = safe_output_path(a.out, workdir=a.workdir)

    # Render into a private 0700 temp dir so ffmpeg never re-opens the
    # user-influenced destination pathname; the finished file is then
    # copied to the destination through the no-follow descriptor path.
    import shutil
    import tempfile
    render_dir = tempfile.mkdtemp(prefix=".render-", dir=a.workdir)
    render_path = os.path.join(render_dir, "render.mp4")

    webm_path, webm_cleanup = open_media_input(a.webm, workdir=a.workdir)
    reader = None
    writer = None
    written = 0
    dead = 0
    try:
        reader = imageio.get_reader(webm_path, format="ffmpeg")
        meta = reader.get_meta_data()
        src_fps = meta.get("fps", 25) or 25
        kfs = cam.get("keyframes", [])
        poses = build_poses(kfs, src_fps, a.lead_ms, a.hold_ms)
        windows = live_windows(kfs, src_fps, a.lead_ms, a.hold_ms, cam.get("live_spans"))
        print(f"src_fps={src_fps} keyframes={len(kfs)} poses={len(poses)} "
              f"live_windows={len(windows)} dead_air_speed={a.dead_air_speed}x -> {out_path}")

        writer = imageio.get_writer(render_path, format="ffmpeg", fps=a.out_fps, codec="libx264",
                                    quality=8, macro_block_size=None,
                                    ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"])
        base_stride = max(1, src_fps / a.out_fps * a.speed)
        next_emit = 0.0
        for i, frame in enumerate(reader):
            if i < next_emit:
                continue
            live = is_live(i, windows) or not windows
            next_emit += base_stride * (1.0 if live else a.dead_air_speed)
            if not live:
                dead += 1
            z, fx, fy = pose_at(i, poses, src_fps, a.transition_frames)
            img = Image.fromarray(frame[:, :, :3])
            img = apply_zoom(img, z, fx, fy)
            writer.append_data(np.asarray(img))
            written += 1
        if writer is not None:
            writer.close()
            writer = None
        with open(render_path, "rb") as src, \
                safe_open_output(a.out, workdir=a.workdir, mode="wb") as dst:
            shutil.copyfileobj(src, dst)
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        webm_cleanup()
        shutil.rmtree(render_dir, ignore_errors=True)
    print(f"wrote {written} frames ({dead} from compressed dead-air) -> {out_path}")


if __name__ == "__main__":
    main()
