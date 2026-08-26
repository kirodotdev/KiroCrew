#!/usr/bin/env python3
"""STEP 2 -- record the real UI, PACED BY THE NARRATION. Copy and adapt this.

This is the only file you write per video. Everything else in the skill is generic.

    python3 record.py            # after narrate.py has produced narr.json

What it guarantees, and why each part exists:

* Beats are hit on ABSOLUTE targets read out of narr.json, not by sleeping for a
  duration. Reload and typing overhead then gets absorbed instead of accumulating:
  duration-driven pacing drifted ~3s by the last beat, absolute targets hold it
  under a few hundred ms.

* `preroll_s` is written into events.json. Video recording starts when the page is
  created, but the beat clock starts later, after navigation and any first-run
  modals. The compositor must offset the clip by this much or voice and picture sit
  seconds apart while every other check looks fine.

* Each primary mark carries `beat: <index>`, pairing it with the narration line of
  the same index. A beat may add EXTRA marks (a click, then a focus on what the
  click revealed); those are left untagged so verify_align.py does not read them
  as drift.

* The cursor is only shown for real pointer actions. Teleporting it onto a
  keyboard-driven beat draws a dot that claims a click nobody made.

Selector note: production bundles often strip `data-testid`, so prefer roles and
accessible names (`[role="dialog"][aria-label="..."]`). Probe the real page once and
write down what exists rather than trusting the source.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import subprocess
import time

from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent
W, H = 1920, 1080

# --- ADAPT: how to reach the URL under test -------------------------------------
# If the target needs a credential, the OPERATOR hands this script the finished
# URL in KC_VIDEO_TARGET_URL. This script does NOT mint one, on purpose: minting a
# dashboard credential is denied to the agent at the shell, so doing it from a
# child process would be routing around that control rather than satisfying it.
# The bare pod URL below is the fallback, and it is only enough when the target
# does not ask for a credential.
POD = "my-pod"


def _write_nofollow(path: pathlib.Path, text: str) -> None:
    """Publish an artifact without ever writing through a link at its name.

    This recorder deliberately does NOT import the shared path gate: it runs under
    the interpreter that has playwright, which on a normal host cannot import the
    product package. So this is the standard-library equivalent for the two files
    it publishes -- refuse a symlink at the name, create the stage through
    O_NOFOLLOW, and rename it into place, so an interrupted run leaves no partial
    events.json for the next step to read.
    """
    if path.is_symlink():
        raise SystemExit(f"refusing to write through a symlink: {path}")
    # O_EXCL, and a name nobody can predict: O_NOFOLLOW refuses a SYMLINK, but a
    # HARD link is the same inode with nothing to follow, so O_TRUNC on a
    # pre-planted stage name would truncate whatever it points at. Creating the
    # stage exclusively means an existing entry is an error, never a target.
    stage = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(stage, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(stage, path)
    except BaseException:
        stage.unlink(missing_ok=True)
        raise


def target_url() -> str:
    url = os.environ.get("KC_VIDEO_TARGET_URL", "").strip()
    if url:
        return url
    out = subprocess.run(
        ["kirocrew", "pod", "url", POD], capture_output=True, text=True, check=True
    )
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    base = lines[-1] if lines else ""
    if not base:
        raise SystemExit("could not read the pod URL -- set KC_VIDEO_TARGET_URL instead")
    return base


# Seed whatever suppresses first-run modals; some gates are ALSO server-side, in
# which case set the matching config and re-mint, because changing config reloads.
# Seed the product's DEFAULT colour theme so the capture and the brand frames share
# one palette. A capture on some other theme puts a different accent on screen from
# the slides around it, which is what makes a film look assembled from parts.
SEED = {
    "mc-onboarded": "1",
    "mc-import-onboarded": "1",
    "mc-privacy-acked": "1",
    "mc-color-theme": "kiro",
    "mc-theme": "dark",
}

CURSOR_JS = r"""
(()=>{const c=document.createElement('div');c.id='__cur';
c.style.cssText='position:fixed;z-index:2147483647;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;background:rgba(0,212,146,.9);box-shadow:0 0 18px rgba(0,212,146,.65);pointer-events:none;left:-99px;top:-99px;transition:left .05s linear,top .05s linear;';
const add=()=>{if(document.body&&!document.getElementById('__cur'))document.body.appendChild(c);};
if(document.readyState!=='loading')add();else document.addEventListener('DOMContentLoaded',add);
window.__moveCur=(x,y)=>{c.style.left=x+'px';c.style.top=y+'px';};})();
"""


def beat_targets(narr_path="assets/audio/narr.json"):
    """Absolute beat times on the footage clock, plus the tail after the last beat."""
    narr = json.loads((BASE / narr_path).read_text())
    foot = [line for line in narr["lines"] if line["role"] == "footage"]
    outro = [line for line in narr["lines"] if line["role"] == "outro"][0]
    t0 = foot[0]["start"]
    return (
        [round(line["start"] - t0, 3) for line in foot],
        round(outro["start"] - foot[-1]["start"], 3),
    )


def main() -> None:
    url = target_url()
    targets, tail = beat_targets()
    print(f"beat targets={targets} tail={tail}")

    events: list[dict] = []
    clock = {"t0": 0.0, "t_video": 0.0}

    def now() -> float:
        return time.monotonic() - clock["t0"]

    def hold_until(t: float, page) -> None:
        remain = t - now()
        if remain < -0.25:
            print(f"    ! overran beat target by {-remain:.2f}s at t={now():.2f}s")
        if remain > 0:
            page.wait_for_timeout(int(remain * 1000))

    def mark(page, label, box, kind="click", beat=None):
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        if kind == "click":
            page.evaluate("([x,y])=>window.__moveCur&&window.__moveCur(x,y)", [cx, cy])
        else:
            page.evaluate("()=>window.__moveCur&&window.__moveCur(-99,-99)")
        e = {
            "t_ms": int(now() * 1000),
            "kind": kind,
            "label": label,
            "focal": {"x": round(cx, 1), "y": round(cy, 1)},
            "bbox": {"w": round(box["width"], 1), "h": round(box["height"], 1)},
            "viewport": {"width": W, "height": H},
        }
        if beat is not None:
            e["beat"] = beat
        events.append(e)
        print(f"  [{e['t_ms']/1000:7.3f}s] {kind:5s} beat={beat} {label}")

    def box_of(page, sel, timeout=8000):
        loc = page.locator(sel).first
        loc.wait_for(state="visible", timeout=timeout)
        return loc.bounding_box()

    def sub_box(box, x_frac=0.0, w_frac=1.0, h_frac=1.0):
        """A sub-region of an element, to frame part of a list instead of all of it."""
        out = dict(box)
        out["x"] = box["x"] + box["width"] * x_frac
        out["width"] = box["width"] * w_frac
        out["height"] = box["height"] * h_frac
        return out

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
        ctx = b.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(BASE),
            record_video_size={"width": W, "height": H},
            device_scale_factor=1,
        )
        for k, v in SEED.items():
            ctx.add_init_script(f"try{{localStorage.setItem({k!r},{v!r})}}catch(e){{}}")
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        # monotonic, not wall clock: an NTP step or a manual clock change during a
        # take would move every beat relative to the narration, which is exactly
        # the misalignment this pipeline exists to prevent.
        clock["t_video"] = time.monotonic()  # recording begins with the page

        # --- pre-roll: navigate and reach a known starting state. NOT narrated. ---
        # The URL may carry a credential, and a Playwright navigation error puts the
        # whole URL in its message -- which lands in agent-visible stderr. Report the
        # failure without it.
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            raise SystemExit(
                "navigation to the target failed "
                f"({type(exc).__name__}) -- the URL is withheld because it may "
                "carry a credential; check KC_VIDEO_TARGET_URL and that the target "
                "is reachable"
            ) from None
        page.wait_for_selector("main, textarea", timeout=30000)
        page.wait_for_timeout(2500)
        for _ in range(2):
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

        clock["t0"] = time.monotonic()  # the footage clock starts HERE

        # --- ADAPT: one block per narration footage line, in order ----------------
        # Pattern: hold_until(target - lead) -> do the work -> hold_until(target) -> mark
        # If the work physically cannot fit the lead (a page reload in a short cut),
        # do the work and mark when the result is on screen; say so in a comment.

        # BEAT 0
        el = box_of(page, "text=/Search for anything/i")
        mark(page, "starting state", el, kind="focus", beat=0)

        # BEAT 1
        hold_until(targets[1] - 1.0, page)
        page.keyboard.press("Control+k")
        dialog = box_of(page, '[role="dialog"]')
        hold_until(targets[1], page)
        mark(page, "dialog open", dialog, kind="focus", beat=1)

        # BEAT 2 -- a real click, so the cursor and a ripple are honest here
        hold_until(targets[2] - 1.2, page)
        row = page.locator('[role="option"]').first
        row.wait_for(state="visible", timeout=8000)
        hold_until(targets[2], page)
        mark(page, "row", row.bounding_box(), kind="click", beat=2)
        row.click()
        page.wait_for_timeout(700)
        # an extra, deliberately untagged mark on what the click revealed
        mark(page, "result of the click", box_of(page, '[role="dialog"]'), kind="focus")

        # ... one block per remaining beat ...

        hold_until(targets[-1] + tail, page)
        # Hold THIS page's video handle before the context goes away: its path is
        # the exact file Playwright wrote for this run. Scanning the directory --
        # even filtered by a start timestamp -- cannot tell two overlapping
        # recordings apart, and would happily pair one run's video with another
        # run's events.
        video = page.video
        if video is None:
            raise SystemExit("this page recorded no video -- check record_video_dir on the context")
        # close() finalizes the file, and path() needs a LIVE transport -- asking
        # after the sync_playwright block exits raises on a stopped connection, so
        # the order is: close the context, resolve the path, then leave the block.
        ctx.close()
        video_path = video.path()
        b.close()

    if not video_path or not pathlib.Path(video_path).exists():
        raise SystemExit(
            "the recording was not finalized -- refusing to publish; check that "
            "the browser context closed cleanly"
        )
    main_webm = str(video_path)
    _write_nofollow(BASE / "MAIN_WEBM", main_webm or "")
    _write_nofollow(
        BASE / "events.json",
        json.dumps(
            {
                "viewport": {"width": W, "height": H},
                "preroll_s": round(clock["t0"] - clock["t_video"], 3),
                "events": events,
            },
            indent=2,
        ),
    )
    print(f"\nMAIN_WEBM: {main_webm}")
    print(f"preroll={round(clock['t0'] - clock['t_video'], 3)}s events={len(events)}")
    print("next: transcode the webm to assets/footage.mp4, then compose.py")


if __name__ == "__main__":
    main()
