#!/usr/bin/env python3
"""
session_grid_scenes.py — the COMPLETE, working Session Grid demo, as a reference.

This is the real 6-scene script (split -> fork -> 2x2 grid -> persist -> close -> live-sync),
ported to the demo_harness API. Read it to see real selectors, the fork-precondition fix,
and English caption phrasing. Adapt scene-by-scene for a new feature.

Run exactly like record_template.py:
  KC_URL="$(cat .tokenurl)" "$(cat ~/.kiro/crew/workspace/.demo-recording-venv/PY_PATH)" \
      session_grid_scenes.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from demo_harness import Demo, is_ascii, url_from_args  # noqa: E402

URL = url_from_args()
OUT = os.path.dirname(os.path.abspath(__file__))

EXTRA_CSS = (".sidebar-inner .overflow-y-auto{display:none !important}"
             " .sidebar-inner .scroll-shadow{display:none !important}")
SEED = {"kc-onboarded": "1"}

with Demo(URL, OUT, seed_localstorage=SEED, extra_init_css=EXTRA_CSS) as d:
    try:
        d.page.evaluate("()=>{try{localStorage.removeItem('kc-split-layouts');"
                        "localStorage.removeItem('kc-session-grid-tree')}catch(e){}}")
    except Exception:
        pass

    d.caption("KiroCrew", "Session Grid",
              "A native split-view chat workspace — one window, many live sessions.", secs=4.5)
    for i in range(2):
        if d.click(['button:has-text("New chat")', '[aria-label*="new chat" i]'],
                   label=f"New chat #{i+1}"):
            d.wait(1600)
    if d.focus_composer():
        d.type("Let's explore the Session Grid feature.", delay=34)
        d.wait(500)
        d.press("Enter")
        d.wait(3500)
        d.log("  seeded anchor with history")
    d.cap_hide()
    d.shot("02-anchorA")

    d.caption("01 - Split", "Split a chat",
              "Press Cmd+D, then pick another session to view them side-by-side.", secs=2.0)
    if not d.click(['[aria-label="Enter split view"]', '[title*="Split view" i]',
                    '[aria-label="Return to split view"]'], label="enter split"):
        d.press("Meta+d")
        d.wait(500)
        d.press("Control+d")
    d.wait(1500)
    if d.click(['input[placeholder="Search sessions..."]'], label="picker search"):
        d.type("untitled", delay=45)
        d.wait(900)
    d.shot("03-split-seeded")
    picked = False
    for cx, cy, loc in d.all_visible('button:has-text("msgs")'):
        try:
            if is_ascii((loc.inner_text() or "").strip()):
                d._glide(cx, cy)
                d.page.mouse.click(cx, cy)
                d.wait(600)
                d.log("  picked English session")
                picked = True
                break
        except Exception:
            pass
    if not picked:
        d.log("  no ASCII row — using New session (avoids non-English titles)")
        d.click(['button:has-text("New session")'], label="pick fallback New session")
    d.wait(1800)
    d.shot("04-two-panes")

    d.caption("02 - Fork", "Fork a session",
              "Forking clones a session's full history into a new child — branch a "
              "conversation without touching the original.", secs=3.0)
    left = d.all_visible('[aria-label="Split down"]')
    if left:
        left.sort(key=lambda t: t[0])
        fx, fy, _ = left[0]
        d.page.mouse.click(fx, fy - 300 if fy and fy > 320 else 300)
        d.wait(500)
    d.click_side(['[aria-label="Split down"]'], side="left", label="split down (left col)")
    d.wait(1500)
    fork = d.first_visible(['button:has-text("Fork"):not([disabled])', 'button:has-text("Fork")'], "Fork")
    fork_disabled = False
    if fork:
        try:
            fork_disabled = fork.is_disabled()
        except Exception:
            pass
    if fork and not fork_disabled:
        d.click(['button:has-text("Fork"):not([disabled])'], label="Fork -> fills bottom-left")
        d.wait(2600)
    else:
        d.log("  !! Fork disabled — fallback New session so the scene still shows a pane")
        d.click(['button:has-text("New session")'], label="fork fallback")
        d.wait(2000)
    try:
        d.log("  pickers_still_open =", d.page.locator('input[placeholder="Search sessions..."]').count(),
              "(0 = fork populated a pane)")
    except Exception:
        pass
    d.shot("05-forked")

    d.caption("03 - Grid", "A 2x2 grid",
              "Every pane splits on its own. Split the right column to complete a four-pane grid.",
              secs=3.0)
    d.click_side(['[aria-label="Split down"]'], side="right", label="split down (right col)")
    d.wait(1500)
    d.click(['button:has-text("New session")'], label="New session -> fills bottom-right")
    d.wait(2000)
    d.wait(800)
    d.shot("06-quad")

    d.caption("04 - Persist", "The layout persists",
              "Leave to another page and come back — your split is restored automatically.", secs=3.0)
    for nav in ("Schedule", "Artifacts", "Agents"):
        if d.goto_nav(nav):
            break
    d.wait(1800)
    d.shot("07-away")
    d.goto_nav("Chat")
    d.wait(2600)
    d.shot("08-returned-persist")

    d.caption("05 - Close", "Close a pane",
              "Close any pane from its header — the grid reflows around it.", secs=3.0)
    d.click_side(['[aria-label="Close pane"]', '[aria-label="Close cell"]'], side="right",
                 label="close a pane")
    d.wait(1700)
    d.shot("09-after-close")

    d.caption("06 - Live sync", "One session, two views",
              "A pane IS the live session. Type in the split, then collapse to its single "
              "view — the same message is right there.", secs=3.0)
    tas = d.all_visible('textarea')
    if tas:
        tas.sort(key=lambda t: (t[0], t[1]))
        cx, cy, _ = tas[0]
        d._glide(cx, cy)
        d.page.mouse.click(cx, cy)
        d.wait(400)
        d.type("Hello from the split pane — this message stays in sync.", delay=38)
        d.wait(900)
        d.shot("10-typed")
        d.press("Enter")
        d.wait(3500)
        d.shot("11-sent")
    for i in range(4):
        cands = d.all_visible('[aria-label="Close pane"]') + d.all_visible('[aria-label="Close cell"]')
        if len(cands) <= 1:
            break
        cands.sort(key=lambda t: (t[0], t[1]))
        cx, cy, _ = cands[-1]
        d._glide(cx, cy)
        d.page.mouse.click(cx, cy)
        d.wait(1600)
        d.log(f"  closed other pane #{i+1}")
    d.wait(1500)
    d.caption("06 - Live sync", "Same session — single view",
              "Back to one chat, and the message you typed in the split is still here.", secs=3.5)
    d.shot("12-single-sync")

    d.caption("KiroCrew - Session Grid", "Split - Fork - Grid - Persist - Sync",
              "All native in the chat surface — no separate app.", secs=4.5)
