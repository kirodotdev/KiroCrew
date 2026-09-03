"""Startup guard that WARNS when the served SPA bundle is stale.

The gateway serves the dashboard from a gitignored, build-copied ``dist/``
(``handlers/core.py``). Nothing verifies that the served bundle was built from
the same tree as the running backend, so a restart that did NOT rebuild/copy
the frontend keeps serving an OLD bundle — the dashboard renders (assets are
*present*), so the gap is silent until a behavioral test fails.

This guard is the FRESHNESS counterpart to ``stale_asset_watchdog.py``'s
PRESENCE check, and it deliberately does NOT share that watchdog's response:

  * The vanish watchdog fires when assets are *gone* — the process can serve
    nothing useful, so it shuts down and lets a supervisor restart a fresh one.
  * This guard fires when assets are *present but stale* — the dashboard still
    works, and a restart alone would serve the *same* stale ``dist`` (a restart
    does not rebuild the frontend). Shutting down would loop forever. So it
    only logs a WARNING with rebuild guidance.

Identity comes from a build-id stamped into ``dist/build-id.json`` at
``vite build`` time (see ``website/vite.config.ts`` ``buildIdPlugin``), reusing
the exact ``${version}-${sha}`` scheme ``swVersionPlugin`` already computes. The
backend's own build commit comes from the baked ``_build_info.py`` in a packaged
install (``beacon.baked_commit()``), falling back to ``git rev-parse HEAD`` in a
source checkout.

The check is best-effort and conservative: any unknown side of the comparison
(no ``build-id.json``, a dist that predates this feature, an unknown backend
commit) means it SKIPS silently rather than false-warning. The only path that
warns is a confident mismatch between two known commits.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from kiro_crew import beacon, platform_compat
from kiro_crew.dashboard.handlers.core import _DIST_DIR

logger = logging.getLogger(__name__)

# Build-id stamp emitted into dist/ by website/vite.config.ts:buildIdPlugin and
# staged into the package by the same `cp -R website/dist ...` every packaging
# path runs.
_BUILD_ID_PATH = _DIST_DIR / "build-id.json"


def _read_dist_commit() -> tuple[str, str] | None:
    """Return ``(commit, build_id)`` from ``dist/build-id.json``, or None.

    None on any of: the file is absent (dist predates this feature, or a dev
    build that skipped the stamp), unreadable, not valid JSON, or carries no
    non-empty ``commit`` (git was unavailable when the frontend was built). Each
    of these is a "cannot verify" case, not a staleness signal.
    """
    try:
        raw = _BUILD_ID_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("Bundle freshness: build-id.json is not valid JSON — skipping.")
        return None
    if not isinstance(data, dict):
        return None
    commit = str(data.get("commit") or "").strip()
    build_id = str(data.get("buildId") or "").strip()
    if not commit:
        return None
    return commit, build_id


def _backend_commit() -> str:
    """Return the running backend's build commit, or "".

    Prefers the baked ``_build_info.COMMIT`` (authoritative in a packaged
    install; a running copy cannot change it). Falls back to ``git rev-parse
    HEAD`` for a source/dev checkout where ``_build_info.py`` is absent — git
    resolved through ``trusted_system_bin`` (fixed system dirs, never ``PATH``,
    which can lead with agent-writable directories where a planted shim would
    run with the gateway's environment). Returns "" if none of these is
    available, which makes the caller skip.
    """
    baked = beacon.baked_commit()
    if baked:
        return baked
    git_bin = platform_compat.trusted_system_bin("git")
    if git_bin is None:
        return ""
    try:
        result = subprocess.run(
            [git_bin, "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def check_bundle_freshness() -> None:
    """Warn (once, at startup) if the served SPA bundle looks stale.

    Best-effort and conservative — never raises, never shuts down. Warns only
    on a confident commit mismatch between the dist stamp and the backend; every
    "cannot verify" case (missing stamp, unknown backend commit) skips silently
    so a transitional dist or a git-less packaging path never false-alarms.
    """
    try:
        dist = _read_dist_commit()
        if dist is None:
            logger.debug("Bundle freshness: no dist/build-id.json commit to compare — skipping.")
            return
        dist_commit, dist_build_id = dist

        backend_commit = _backend_commit()
        if not backend_commit:
            logger.debug("Bundle freshness: backend build commit unknown — skipping.")
            return

        if dist_commit != backend_commit:
            logger.warning(
                "Dashboard SPA bundle is STALE: the served frontend is build "
                "%s (commit %s) but this backend is running commit %s. A restart "
                "will not fix this — it re-serves the same dist. Rebuild and "
                "restage the frontend: `cd website && npm run build`, copy "
                "website/dist into src/kiro_crew/static/dist, then restart.",
                dist_build_id or dist_commit[:7],
                dist_commit[:7],
                backend_commit[:7],
            )
    except Exception:
        # A freshness advisory must never break startup. Swallow everything and
        # note it at debug level for diagnosis.
        logger.debug("Bundle freshness check failed — ignoring.", exc_info=True)
