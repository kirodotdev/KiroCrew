"""Locate the ``mise`` binary for :func:`kiro_crew.env.activate_mise`.

:mod:`kiro_crew.env` re-exports :func:`_mise_bin`, so ``env._mise_bin`` stays
the name callers and tests patch.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _mise_bin() -> str | None:
    """Locate the ``mise`` binary in a non-login (daemon) context.

    A systemd / launchd gateway does not source the user's shell rc, so
    ``~/.local/bin`` (mise's default install dir) is often absent from the
    inherited ``$PATH``. Try ``$PATH`` first, then the default install dir,
    then macOS Homebrew locations. Discovery must work before mise activation
    adds the user's toolchain directories to the gateway environment.

    A candidate whose probe raises ``OSError`` is skipped like a missing one:
    ``Path.is_file`` swallows only not-found errors, so a ``stat`` refused with
    EACCES (a restricted ``/usr/local/bin`` or a sandboxed gateway) would
    otherwise abort gateway boot from a lookup that is meant to be optional.
    """
    found = shutil.which("mise")
    if found:
        return found
    candidates = [Path.home() / ".local" / "bin" / "mise"]
    if sys.platform == "darwin":
        candidates.extend([Path("/opt/homebrew/bin/mise"), Path("/usr/local/bin/mise")])
    for candidate in candidates:
        try:
            usable = candidate.is_file() and os.access(candidate, os.X_OK)
        except OSError as exc:
            logger.debug("mise candidate %s skipped: %s", candidate, type(exc).__name__)
            continue
        if usable:
            return str(candidate)
    return None
