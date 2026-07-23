#!/usr/bin/env python3
"""
record_template.py — copy this into your workdir and fill in the SCENES.

  cp <app-skills-dir>/feature-demo-recording/references/record_template.py \
     ~/.kirocrew/workspace/uploads/<feature>-video/record.py

Then run (after setup.sh + a fresh token in .tokenurl):

  cd ~/.kirocrew/workspace/uploads/<feature>-video
  # RECORD -> page@*.webm + events.json
  KC_DEMO_REFS="<app-skills-dir>/feature-demo-recording/references" \
  KC_URL="$(cat .tokenurl)" "$(cat ~/.kirocrew/workspace/.demo-recording-venv/PY_PATH)" record.py
  # POLISH -> auto-zoom + dead-air trim -> demo.mp4
  bash <app-skills-dir>/feature-demo-recording/references/render.sh . demo.mp4 --dead-air-speed 6

The harness writes ./MAIN_WEBM and ./events.json. render.sh reads both and produces demo.mp4.
You only write SCENES below — clicks auto-become punch-in zooms; captions stay full-speed.
"""
import os
import sys

# The template is copied AWAY from references/, so demo_harness cannot be
# found on the script's own directory alone. KC_DEMO_REFS points back at the
# skill's references/ directory (see the RECORD command above). A local copy
# of the modules next to this script also works and takes precedence.
_refs = os.environ.get("KC_DEMO_REFS", "")
if _refs:
    sys.path.insert(0, _refs)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from demo_harness import Demo, is_ascii, url_from_args  # noqa: F401, E402
except ImportError:
    sys.exit(
        "FATAL: demo_harness is not importable. Set KC_DEMO_REFS to the "
        "skill's references directory, e.g.\n"
        "  KC_DEMO_REFS=<app-skills-dir>/feature-demo-recording/references \\\n"
        "  KC_URL=... <venv-python> record.py\n"
        "or copy the references/*.py modules next to this script."
    )

URL = url_from_args()
OUT = os.path.dirname(os.path.abspath(__file__))

EXTRA_CSS = ""

SEED = {"kc-onboarded": "1"}

with Demo(URL, OUT, seed_localstorage=SEED, extra_init_css=EXTRA_CSS) as d:
    # =========================================================================
    # WRITE YOUR SCENES HERE. A scene = a caption + the actions it narrates.
    # Methods: d.caption / d.cap_hide / d.click / d.click_side / d.focus_composer
    #          d.type / d.press / d.wait / d.shot / d.goto_nav
    # See session_grid_scenes.py for a complete worked example.
    # =========================================================================

    # --- Intro ---
    d.caption("MyProduct", "<Feature Name>",
              "<One-sentence hook describing what this feature is.>", secs=4.5)

    # --- Scene 1 ---
    d.caption("01 - <Verb>", "<Scene title>",
              "<What the viewer is about to see, in English.>", secs=3)
    d.click(['button:has-text("<Button>")', '[aria-label="<label>"]'], label="scene1 action")
    d.wait(1500)
    d.shot("scene1")

    # --- Scene 2 ---
    # d.caption("02 - ...", "...", "...", secs=3)
    # ...

    # --- Outro ---
    d.caption("MyProduct - <Feature>", "<Tagline>",
              "<Closing line.>", secs=4.5)
