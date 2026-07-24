"""Run-marker so remote token-mint targets the *running* gateway's install.

The problem this solves:
    Token mint SSHes to the remote desktop and runs ``kirocrew token`` resolved
    from a fixed PATH candidate list (:data:`token_mint.REMOTE_BIN_CANDIDATES`,
    first entry ``$HOME/.local/bin/kirocrew``). When that launcher symlinks into
    an *uninstalled* git worktree (no ``.venv``), every mint fails with
    "KiroCrew venv not found" — even though the gateway itself is up and healthy,
    because it runs from a *different* venv. The user's sync -> rebuild -> restart
    of the gateway can't fix mint, since mint never consults the gateway's own
    install. The proactive/reactive token-refresh loop then re-mints on every
    poll and fails, which surfaces as the pane periodically disconnecting and
    reconnecting.

The fix:
    At startup the gateway records the absolute path to *its own* ``kirocrew``
    launcher, keyed by the port it serves, at
    ``<config_dir>/run/gateway-<port>.bin``. The mint shell snippet reads that
    marker first and, when it names an executable, ``exec``s it — guaranteeing
    mint uses the same built venv as the live gateway. An absent/stale marker
    (older remotes, or a gateway that isn't running) makes mint fall back to the
    candidate search, so nothing regresses.

Trust: the marker lives in the ``0700`` ``run/`` dir (owner-only) and is written
``0600`` by the gateway itself; the dir is on the ``is_sensitive_path`` floor
(``security._SENSITIVE_HOME_DIRS``) so agent file tools cannot write it. That
write boundary — not an ownership check on the mint side — is what bounds who can
plant a marker; the mint side additionally only ``exec``s the path when it is an
executable file (``-x``), the same boundary as the pre-existing
``~/.local/bin/kirocrew`` candidate.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)


def _run_dir() -> Path:
    """Return ``<config_dir>/run`` (created ``0700``); mirrors sandbox._ensure_run_dir."""
    d = config_dir() / "run"
    d.mkdir(parents=True, exist_ok=True)
    try:
        # exist_ok does not re-apply mode on an existing dir — enforce 0700 so the
        # marker (which names an executable mint will exec) isn't world-writable.
        # 0o700 (owner-only) is deliberate for a dir holding an exec'd path;
        # semgrep's 0o644 default is wrong for a private dir (mirrors sandbox.py).
        os.chmod(d, 0o700)  # nosemgrep
    except OSError:
        pass
    return d


def marker_path(port: int) -> Path:
    """Path of the run-marker for a gateway serving *port*."""
    return _run_dir() / f"gateway-{int(port)}.bin"


def gateway_launcher_path() -> str | None:
    """Absolute path to the running gateway's own ``kirocrew`` launcher.

    In an OSS venv install ``sys.executable`` is ``<venv>/bin/python`` and the
    console script ``pip install -e .`` creates is its sibling
    ``<venv>/bin/kirocrew`` (``kirocrew.exe`` on Windows). Returns ``None`` when
    that launcher is absent or not executable, in which case mint keeps using the
    PATH candidate search. ``sys.executable`` is deliberately *not* resolved
    through symlinks: the console script sits next to the (possibly symlinked)
    interpreter in the venv's ``bin/``, not next to the real interpreter.
    """
    exe = sys.executable
    if not exe:
        return None
    base = Path(exe).with_name("kirocrew.exe" if os.name == "nt" else "kirocrew")
    try:
        if base.is_file() and os.access(base, os.X_OK):
            return str(base)
    except OSError:
        return None
    return None


def write_marker(port: int) -> None:
    """Best-effort: record the running gateway's launcher path for *port*.

    Written via :func:`kiro_crew.atomic_write.atomic_write`, which uses a unique
    ``mkstemp`` (``O_EXCL``, mode ``0600``) temp then ``os.replace``. Using the
    shared helper — rather than a predictable ``<name>.tmp`` — closes a same-user
    symlink-TOCTOU: a pre-planted ``gateway-<port>.bin.tmp`` symlink can no longer
    redirect the write to truncate another file. Never raises — a failed write
    just leaves mint on the candidate search.
    """
    launcher = gateway_launcher_path()
    if not launcher:
        logger.debug("No venv kirocrew launcher next to %s — skipping run-marker", sys.executable)
        return
    try:
        atomic_write(marker_path(port), launcher + "\n", mode=0o600)
        logger.info("Wrote gateway run-marker for port %s -> %s", port, launcher)
    except Exception as e:  # best-effort — never break startup on a marker write
        logger.warning("Could not write gateway run-marker for port %s: %s", port, e)


def clear_marker(port: int) -> None:
    """Best-effort removal of the run-marker for *port* (on graceful shutdown)."""
    try:
        marker_path(port).unlink(missing_ok=True)
    except OSError:
        pass
