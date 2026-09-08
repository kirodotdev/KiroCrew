"""Agentic "real user" GUI tests: a pixel-only agent driving the dashboard in CI.

The packages here are NOT part of ``kiro_crew``. They are the CI-side harness
described in ``docs/build/gui-user-test.md``:

* ``x11``       -- screenshot + input backend (Pillow ImageGrab, xdotool) with the
                   pure coordinate / key / argv mapping kept import-safe so it can
                   be unit-tested without a display.
* ``scenarios`` -- the YAML scenario DSL loader and validator.
* ``harness``   -- the Bedrock Messages API loop with step / time / budget gates.
* ``report``    -- renders ``summary.json`` into the PR comment / issue body.
"""
