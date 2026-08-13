#!/usr/bin/env python3
"""Record a browser flow as webm (+ mp4/gif when ffmpeg is available).

Entry point for the browser-recording skill. Owns dependency probing,
argument validation, and post-processing; delegates the actual Playwright
session to the sibling ``driver.mjs`` (Playwright's recording API is only
exposed to its JS package, which is the copy frontend projects already have).

Usage:
    python3 record_browser.py --url URL [--scenario FILE.mjs]
        [--project DIR] [--size 1280x800] [--out DIR] [--name BASENAME]
        [--settle-ms 600] [--tail-ms 400]

Outputs (printed one per line at the end, machine-readable):
    WEBM <path>      always
    MP4 <path>       when ffmpeg is present
    GIF <path>       when ffmpeg is present

Design notes:
- Probe-first, fail-loud: node and project-local Playwright are required and
  reported with exact remediation commands; nothing is auto-installed.
- ffmpeg is optional: the webm is the evidence, mp4/gif are conveniences.
- The GIF uses a two-pass palette encode (fps=12, width<=800) — the settled
  recipe for crisp UI recordings at reasonable size.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

DRIVER = Path(__file__).with_name("driver.mjs")


def _fail(msg: str, code: int = 2) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _probe_node() -> str:
    node = shutil.which("node")
    if not node:
        _fail(
            "node not found on PATH. Install Node.js (https://nodejs.org) — the "
            "recording driver runs on the project's own Playwright package."
        )
    return node


def _probe_playwright(project: Path) -> None:
    """Require playwright resolvable from the project directory (fail loud)."""
    if not (project / "node_modules" / "playwright").is_dir() and not (
        project / "node_modules" / "playwright-core"
    ).is_dir():
        _fail(
            f"playwright is not installed under {project}/node_modules. "
            "Install it in the project first:\n"
            "  npm i -D playwright && npx playwright install chromium\n"
            "(this skill never installs packages on its own)"
        )


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _convert(ffmpeg: str, webm: Path, out_dir: Path, name: str) -> tuple[Path, Path]:
    mp4 = out_dir / f"{name}.mp4"
    gif = out_dir / f"{name}.gif"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(webm),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         str(mp4)],
        check=True,
    )
    # Two-pass palette GIF: sharp text, bounded size.
    with tempfile.TemporaryDirectory() as td:
        palette = Path(td) / "palette.png"
        filters = "fps=12,scale='min(800,iw)':-1:flags=lanczos"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(webm),
             "-vf", f"{filters},palettegen", str(palette)],
            check=True,
        )
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), "-i", str(palette),
             "-lavfi", f"{filters} [x]; [x][1:v] paletteuse", str(gif)],
            check=True,
        )
    return mp4, gif


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True, help="page to open (http/https)")
    ap.add_argument("--scenario", help="path to scenario .mjs (default export async (page)=>{})")
    ap.add_argument("--project", help="frontend project dir whose Playwright install to use "
                                      "(default: scenario's dir, else cwd)")
    ap.add_argument("--size", default="1280x800", help="viewport WxH (default 1280x800)")
    ap.add_argument("--out", help="output dir (default: a fresh temp dir)")
    ap.add_argument("--name", default="recording", help="output basename (default: recording)")
    ap.add_argument("--settle-ms", type=int, default=600, help="wait after load before scenario")
    ap.add_argument("--tail-ms", type=int, default=400, help="wait after scenario before close")
    args = ap.parse_args(argv)

    if not _URL_RE.match(args.url):
        _fail("--url must be http(s)://")
    m = _SIZE_RE.match(args.size)
    if not m:
        _fail("--size must look like 1280x800")
    width, height = int(m.group(1)), int(m.group(2))

    scenario: Path | None = None
    if args.scenario:
        scenario = Path(args.scenario).resolve()
        if not scenario.is_file():
            _fail(f"scenario not found: {scenario}")
        if scenario.suffix != ".mjs":
            _fail("scenario must be an .mjs ES module (default export async (page) => {})")

    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.name):
        _fail("--name must be a plain filename fragment (letters, digits, . _ -)")

    if args.project:
        project = Path(args.project).resolve()
    elif scenario:
        project = scenario.parent
    else:
        project = Path.cwd()
    node = _probe_node()
    _probe_playwright(project)

    out_dir = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix="browser-rec-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = {
        "url": args.url,
        "scenarioPath": str(scenario) if scenario else None,
        "outDir": str(out_dir),
        "width": width,
        "height": height,
        "settleMs": args.settle_ms,
        "tailMs": args.tail_ms,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg, f)
        cfg_path = f.name

    proc = subprocess.run(
        [node, str(DRIVER), cfg_path],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=600,
    )
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        _fail(f"driver failed (exit {proc.returncode})", proc.returncode)

    # Scenario code (and the page itself) can print to stdout, so the RECORDED
    # marker is spoofable. Two containments: take the LAST marker line (ours is
    # printed after the scenario finished), and refuse any path that does not
    # resolve inside out_dir — a forged line pointing elsewhere must never
    # become the source of a shutil.move.
    webm_line = next(
        (ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith("RECORDED ")),
        None,
    )
    if not webm_line:
        _fail("driver produced no RECORDED line — no video was flushed")
    webm_src = Path(webm_line.split(" ", 1)[1]).resolve()
    try:
        webm_src.relative_to(out_dir)
    except ValueError:
        _fail(f"driver reported a video outside the output dir (spoofed?): {webm_src}")
    if not webm_src.is_file():
        _fail(f"driver reported a video that does not exist: {webm_src}")

    # Playwright names the file with a random hash — give it the asked-for name.
    webm = out_dir / f"{args.name}.webm"
    if webm_src != webm:
        shutil.move(str(webm_src), str(webm))

    print(f"WEBM {webm}")
    ffmpeg = _ffmpeg()
    if ffmpeg:
        try:
            mp4, gif = _convert(ffmpeg, webm, out_dir, args.name)
            print(f"MP4 {mp4}")
            print(f"GIF {gif}")
        except subprocess.CalledProcessError as exc:
            print(f"note: ffmpeg conversion failed ({exc}); webm is still valid",
                  file=sys.stderr)
    else:
        print("note: ffmpeg not found — emitted webm only. Install ffmpeg for "
              "mp4/gif (macOS: brew install ffmpeg).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
