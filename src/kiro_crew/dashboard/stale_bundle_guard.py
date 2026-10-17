"""Startup guard that WARNS when the served SPA bundle is stale.

The gateway serves the dashboard from a gitignored, build-copied ``dist/``
(``handlers/core.py``). Nothing verifies that the served bundle was built from
the same frontend source as the running backend, so a restart that did NOT
rebuild/copy the frontend keeps serving an OLD bundle — the dashboard renders
(assets are *present*), so the gap is silent until a behavioral test fails.

This guard is the FRESHNESS counterpart to ``stale_asset_watchdog.py``'s
PRESENCE check, and it deliberately does NOT share that watchdog's response:

  * The vanish watchdog fires when assets are *gone* — the process can serve
    nothing useful, so it shuts down and lets a supervisor restart a fresh one.
  * This guard fires when assets are *present but stale* — the dashboard still
    works, and a restart alone would serve the *same* stale ``dist`` (a restart
    does not rebuild the frontend). Shutting down would loop forever. So it
    only logs a WARNING with rebuild guidance.

Identity is the git tree id of ``website/`` (``git rev-parse HEAD:website``), the
same identity ``frontend._write_build_source_fingerprint`` already records in
``kirocrew-build-source.txt`` for Dev Fleet's rebuild skip. It changes iff a
tracked frontend file changes, so a backend-only or docs-only commit after a
build does NOT make a correct bundle look stale. The dist side reads the
``websiteTree`` stamped into ``dist/build-id.json`` by ``website/vite.config.ts``
``buildIdPlugin`` (rewritten by every ``vite build``, hand-run ones included),
and falls back to that fingerprint only for a dist with no ``build-id.json``.
Both writers stamp only when ``website/``
is clean, because a dirty build compiled content ``HEAD:website`` does not
describe. The backend side is the baked ``_build_info.WEBSITE_TREE`` in a
packaged install (``beacon.baked_website_tree()``), falling back to
``git rev-parse HEAD:website`` in a source checkout.

The check is best-effort and conservative: any unknown side of the comparison
(no stamp, a dist that predates this feature or was built dirty, an unknown
backend tree) means it SKIPS silently rather than false-warning. The only path
that warns is a confident mismatch between two known tree ids.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from kiro_crew import beacon
from kiro_crew.code_fingerprint import hardened_git_env
from kiro_crew.dashboard.handlers.core import _DIST_DIR
from kiro_crew.platform_compat import trusted_git_bin
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

# Main's build-source fingerprint (frontend._write_build_source_fingerprint):
# the HEAD:website tree id, written beside the staged dist by build_and_stage.
_BUILD_SOURCE_PATH = _DIST_DIR / "kirocrew-build-source.txt"

# Build-id stamp emitted into dist/ by website/vite.config.ts:buildIdPlugin on
# every `vite build` and staged into the package by the same
# `cp -R website/dist ...` every packaging path runs.
_BUILD_ID_PATH = _DIST_DIR / "build-id.json"

# The kiro_crew package directory: the `-C` root of the source-checkout probe.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _read_dist_tree() -> tuple[str, str] | None:
    """Return ``(website_tree, build_id)`` the served dist was built from, or None.

    ``build-id.json`` wins whenever it parses: every ``vite build`` rewrites
    it, while ``kirocrew-build-source.txt`` is written only by
    ``build_and_stage`` and can survive a hand-run build copied over the old
    dist. So the fingerprint is read only when there is no ``build-id.json``
    (a dist that predates the plugin). None when the chosen stamp carries no
    tree id (absent, unreadable, or a build of a dirty/git-less tree). Each of
    these is a "cannot verify" case, not a staleness signal.
    """
    try:
        data = json.loads(_BUILD_ID_PATH.read_text(encoding="utf-8"))
    except OSError:
        data = None
    except (ValueError, TypeError):
        logger.debug("Bundle freshness: build-id.json is not valid JSON — skipping.")
        return None
    if isinstance(data, dict):
        build_id = str(data.get("buildId") or "").strip()
        tree = str(data.get("websiteTree") or "").strip()
        return (tree, build_id) if tree else None
    try:
        tree = _BUILD_SOURCE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return (tree, "") if tree else None


def _backend_website_tree() -> str:
    """Return the ``website/`` tree id of the running backend's source, or "".

    Prefers the baked ``_build_info.WEBSITE_TREE`` (authoritative in a packaged
    install; a running copy cannot change it). Falls back to ``git rev-parse
    HEAD:website`` for a source checkout, through the same trusted git path as
    ``code_fingerprint``: git resolved by ``trusted_git_bin`` (fixed system
    dirs, never ``PATH``) and run under ``hardened_git_env`` (no planted
    config). Returns "" if neither is available, which makes the caller skip.
    """
    baked = beacon.baked_website_tree()
    if baked:
        return baked
    git = trusted_git_bin()
    if git is None:
        return ""
    try:
        result = subprocess.run(
            [git, "-C", str(_PACKAGE_ROOT), "-c", "core.pager=cat", "rev-parse", "HEAD:website"],
            capture_output=True,
            timeout=5,
            check=False,
            env=hardened_git_env(),
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def check_bundle_freshness() -> None:
    """Warn (once, at startup) if the served SPA bundle looks stale.

    Best-effort and conservative — never raises, never shuts down. Warns only
    on a confident ``website/`` tree mismatch between the dist stamp and the
    backend; every "cannot verify" case skips silently so a transitional dist
    or a git-less packaging path never false-alarms.
    """
    try:
        dist = _read_dist_tree()
        if dist is None:
            logger.debug("Bundle freshness: no website/ tree stamp in dist — skipping.")
            return
        dist_tree, dist_build_id = dist

        backend_tree = _backend_website_tree()
        if not backend_tree:
            logger.debug("Bundle freshness: backend website/ tree unknown — skipping.")
            return

        if dist_tree != backend_tree:
            logger.warning(
                "Dashboard SPA bundle is STALE: the served frontend%s was built "
                "from website/ tree %s but this backend's website/ tree is %s. A "
                "restart will not fix this — it re-serves the same dist. Rebuild "
                "and restage the frontend: `cd website && npm run build`, copy "
                "website/dist into src/kiro_crew/static/dist, then restart.",
                f" (build {dist_build_id})" if dist_build_id else "",
                dist_tree[:7],
                backend_tree[:7],
            )
    except Exception:
        # A freshness advisory must never break startup. Swallow everything and
        # note it at debug level for diagnosis.
        logger.debug("Bundle freshness check failed — ignoring.", exc_info=True)
