"""Regression guard for the production bundle's V8 heap ceiling.

Background
----------
``website``'s production build (``tsc -b && vite build``) peaks around 2.8 GB of
RSS, and rollup's chunk-rendering phase is where it spikes. Node picks a default
old-space ceiling from total system memory, so the limit differs per runner: the
16 GB ubuntu-22.04 runner gets ~4 GB and finishes, while the 7 GB macos-14 arm64
runner gets less and dies with ``FATAL ERROR: Ineffective mark-compacts near heap
limit`` in ``rendering chunks...``. The failure is platform-split and looks like a
flake, which is how it survived several rounds of "re-run the job".

Every packaging path (``make website``, ``make desktop`` via
``packaging/build-desktop.sh``, build-wheel, build, ci, pages and docker-smoke
workflows) shells out to ``npm run build``, so the ceiling belongs in that one
script rather than in each caller's environment.

Why the flag and not ``NODE_OPTIONS``
------------------------------------
An inline ``NODE_OPTIONS=...`` prefix inside an npm script is not portable to
Windows ``cmd`` without a helper dependency, and exporting it in CI would leave
local and packaged builds on the old default. Invoking vite's own bin through
``node`` with the flag works identically on every platform. The ceiling is a cap,
not a reservation, so a build that does not need the headroom does not pay for it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = REPO_ROOT / "website" / "package.json"

#: The observed peak is ~2.8 GB; this leaves headroom for growth while staying
#: under the smallest runner's physical memory (macos-14 has 7 GB).
MIN_HEAP_MB = 6144


def _build_script() -> str:
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    return str(data["scripts"]["build"])


def test_build_script_raises_the_heap_ceiling_explicitly():
    script = _build_script()
    match = re.search(r"--max-old-space-size=(\d+)", script)
    assert match, (
        "website's build script no longer pins a V8 heap ceiling; the macos-14 "
        "runner will OOM in rollup's chunk-rendering phase"
    )
    assert int(match.group(1)) >= MIN_HEAP_MB, (
        f"heap ceiling {match.group(1)} MB is below the {MIN_HEAP_MB} MB the bundle needs"
    )


def test_build_script_invokes_vite_through_node():
    """The flag has to reach V8. `vite build` alone would ignore it, and an inline
    NODE_OPTIONS prefix does not survive Windows cmd."""
    script = _build_script()
    assert "node --max-old-space-size" in script, (
        "the heap flag is not passed to a node invocation"
    )
    assert "vite/bin/vite.js" in script, (
        "the build no longer runs vite's bin directly, so the flag reaches nothing"
    )
    assert "NODE_OPTIONS" not in script, (
        "an inline NODE_OPTIONS prefix is not portable to Windows cmd"
    )


def test_type_check_still_runs_before_the_bundle():
    """`tsc -b` is the only thing that type-checks the app (the root tsconfig is
    references-only), so it must stay ahead of the bundle step."""
    script = _build_script()
    assert script.index("tsc -b") < script.index("vite"), (
        "the bundle is built before types are checked"
    )
