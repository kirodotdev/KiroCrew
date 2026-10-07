#!/usr/bin/env bash
# Shoot every project-report screenshot a pull request attaches, in one command.
#
# The point is reproducibility. These images are a pull request's evidence, and an
# image nobody can regenerate is a claim rather than a reading -- so every shot below
# comes from a fixture in the repository through the renderer in the repository, with
# the clock pinned by the fixture so two runs on different days agree.
#
# The images themselves are NOT committed: `.gitignore` sends review evidence to a
# GitHub attachment (`gh pr create|edit --attach`), and `$OUT` below is an ignored
# directory so a capture run never dirties `git status`. This script is the committed
# half -- the recipe, which is the part a reader needs to check a picture.
#
# Run from the repository root. Needs the interpreter that carries playwright, which
# is not the repo venv:
#   PY=<playwright python> bash scripts/shoot_dashboard_evidence.sh
set -euo pipefail

PY="${PY:-python3}"
R=scripts/render_dashboard_builtin.py
T=src/kiro_crew/dashboard_templates/builtin/project-report
F=test/fixtures/dashboard_templates
OUT=.github/screenshots/dyndash

shoot() { local name="$1"; shift; PYTHONPATH=src "$PY" "$R" "$T" "$@" "$OUT/$name"; }

# ---- the page as a reader meets it, at both widths in both themes --------------
shoot dyndash-report-430-dark.png   "$F/sample_workstreams.json" --width 430  --theme dark
shoot dyndash-report-430-light.png  "$F/sample_workstreams.json" --width 430  --theme light
shoot dyndash-report-1200-dark.png  "$F/sample_workstreams.json" --width 1200 --theme dark
shoot dyndash-report-1200-light.png "$F/sample_workstreams.json" --width 1200 --theme light

# ---- R1 the verdict line: live, and downgraded to its honest silence ----------
shoot dyndash-report-verdict-live-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --crop ".pr-vd"
shoot dyndash-report-verdict-live-light.png \
  "$F/sample_workstreams.json" --width 1200 --theme light --crop ".pr-vd"
shoot dyndash-report-verdict-no-word-dark.png \
  "$F/sample_workstreams-stale.json" --width 1200 --theme dark --crop ".pr-vd"
shoot dyndash-report-verdict-no-word-light.png \
  "$F/sample_workstreams-stale.json" --width 1200 --theme light --crop ".pr-vd"

# ---- R2 the red-lane strip, with its owner tags and its did-not-run row -------
shoot dyndash-report-red-lanes-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --crop ".pr-lanes"
shoot dyndash-report-red-lanes-light.png \
  "$F/sample_workstreams.json" --width 1200 --theme light --crop ".pr-lanes"

# ---- R3 idle time on the rows, and the band when the fold stops advancing -----
shoot dyndash-report-fold-quiet-dark.png \
  "$F/sample_workstreams-stale.json" --width 1200 --theme dark --crop ".pr-stale"
shoot dyndash-report-fold-quiet-light.png \
  "$F/sample_workstreams-stale.json" --width 1200 --theme light --crop ".pr-stale"
shoot dyndash-report-fold-quiet-page-dark.png \
  "$F/sample_workstreams-stale.json" --width 1200 --theme dark
shoot dyndash-report-fold-quiet-page-430-dark.png \
  "$F/sample_workstreams-stale.json" --width 430 --theme dark

# ---- R4 the task drawer: a task with a record, and one with almost none -------
shoot dyndash-report-drawer-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --open 0 --crop ".pr-dr"
shoot dyndash-report-drawer-light.png \
  "$F/sample_workstreams.json" --width 1200 --theme light --open 0 --crop ".pr-dr"
shoot dyndash-report-drawer-empty-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --open 3 --crop ".pr-dr"
shoot dyndash-report-drawer-page-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --open 0
shoot dyndash-report-drawer-430-dark.png \
  "$F/sample_workstreams.json" --width 430 --theme dark --open 0

# ---- R5 the per-epic pipeline: worker -> verdict -> the lead accepts -----------
shoot dyndash-report-pipeline-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark \
  --view "Dynamic dashboard round 4" --crop ".pr-card:has(.pr-pipe)"
shoot dyndash-report-pipeline-light.png \
  "$F/sample_workstreams.json" --width 1200 --theme light \
  --view "Dynamic dashboard round 4" --crop ".pr-card:has(.pr-pipe)"
shoot dyndash-report-pipeline-430-dark.png \
  "$F/sample_workstreams.json" --width 430 --theme dark \
  --view "Dynamic dashboard round 4" --crop ".pr-card:has(.pr-pipe)"

# ---- the live indicator, in both of its two readings --------------------------
shoot dyndash-report-live-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --crop ".pr-head"
shoot dyndash-report-live-quiet-dark.png \
  "$F/sample_workstreams-stale.json" --width 1200 --theme dark --crop ".pr-head"

# ---- the tree and the needs-you card, which the new bands sit above ------------
shoot dyndash-report-epic-tree.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --crop ".pr-card:has(.pr-tree)"
shoot dyndash-report-all-workstreams.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark
shoot dyndash-report-needs-you-wide-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --crop ".pr-card:has(.pr-todo)"
shoot dyndash-report-needs-you-wide-light.png \
  "$F/sample_workstreams.json" --width 1200 --theme light --crop ".pr-card:has(.pr-todo)"
shoot dyndash-report-needs-you-narrow-dark.png \
  "$F/sample_workstreams.json" --width 430 --theme dark --crop ".pr-card:has(.pr-todo)"
shoot dyndash-report-needs-you-narrow-light.png \
  "$F/sample_workstreams.json" --width 430 --theme light --crop ".pr-card:has(.pr-todo)"

# ---- one workstream on its own, reached by pressing its own pill ---------------
shoot dyndash-report-one-workstream-dark.png \
  "$F/sample_workstreams.json" --width 1200 --theme dark --view "Dynamic dashboard round 4"
shoot dyndash-report-one-workstream-light.png \
  "$F/sample_workstreams.json" --width 1200 --theme light --view "Dynamic dashboard round 4"

# ---- the empty page, which is what a new crewmate actually sees first ----------
shoot dyndash-report-empty-wide.png   "$F/sample_workstreams-empty.json" --width 1200 --theme dark
shoot dyndash-report-empty-narrow.png "$F/sample_workstreams-empty.json" --width 430  --theme dark

echo "all shots written to $OUT"
