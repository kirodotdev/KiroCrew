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

Second consumer — port discovery:
    The marker's *filename* also advertises which port a gateway is serving, so
    :func:`marker_ports` lets a local client command (``token`` / ``status`` /
    ``logout`` / ``stop``, via ``cli_server.resolve_client_port``) find a gateway
    on a non-default port with zero configuration. That path reads only the
    filename, never the recorded launcher path.

    A marker is NOT proof a gateway is there: :func:`clear_marker` runs only on
    graceful shutdown, so a crash or SIGKILL leaves the file behind and an
    unrelated process may since have bound that port. Because client commands
    send the local secret (``X-Local-Secret``) to whatever is listening, the
    consumer MUST verify the listener before trusting a discovered port, using
    the pid sidecar this module writes beside the marker (:func:`read_pid`) — see
    ``cli_server._gateway_owns_port``. This module deliberately does not offer a
    bare "is something listening" helper, so no caller can mistake reachability
    for identity.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "gateway-"
_MARKER_SUFFIX = ".bin"
_PID_SUFFIX = ".pid"


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
    return _run_dir() / f"{_MARKER_PREFIX}{int(port)}{_MARKER_SUFFIX}"


def pid_path(port: int) -> Path:
    """Path of the pid sidecar for a gateway serving *port*.

    Kept separate from the marker because the two have different readers: mint
    ``cat``s the marker and execs its contents, so the marker must hold exactly
    one path and nothing else. The pid therefore lives beside it in
    ``gateway-<port>.pid``.
    """
    return _run_dir() / f"{_MARKER_PREFIX}{int(port)}{_PID_SUFFIX}"


def read_pid(port: int) -> int | None:
    """Pid recorded by the gateway serving *port*, or ``None``.

    The identity claim a client uses before trusting a discovered port. It is
    trustworthy because the sidecar is written ``0600`` inside the ``0700``
    ``run/`` dir (which is on the ``is_sensitive_path`` floor, so agent file
    tools cannot write it either): another local user cannot point it at a
    process of theirs. It is NOT proof of liveness — a crashed gateway leaves
    its pid behind — so the caller must also confirm that pid currently holds
    the port. See ``cli_server._gateway_owns_port``.

    Read-only: never creates ``run/``.
    """
    try:
        raw = (config_dir() / "run" / f"{_MARKER_PREFIX}{int(port)}{_PID_SUFFIX}").read_text(
            encoding="utf-8"
        )
    except (OSError, ValueError):
        return None
    raw = raw.strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid if pid > 0 else None


def read_launcher(port: int) -> str | None:
    """Launcher path recorded by the gateway serving *port*, or ``None``.

    The marker's *contents* — the absolute path to the running gateway's own
    ``kirocrew`` launcher (see :func:`write_marker`), or ``None`` when the
    marker is absent, unreadable, or empty (a source-tree ``python -m
    kiro_crew`` launch records no launcher). Same trust argument as
    :func:`read_pid`: the file is written ``0600`` inside the keystone-fenced
    ``0700`` ``run/`` dir, so only the gateway itself can have planted it.
    Callers that exec the result must still validate it the way mint's shell
    clause does (an existing executable file).

    Read-only: never creates ``run/`` (unlike :func:`marker_path`, whose
    ``_run_dir`` materialises the directory).
    """
    try:
        raw = (config_dir() / "run" / f"{_MARKER_PREFIX}{int(port)}{_MARKER_SUFFIX}").read_text(
            encoding="utf-8"
        )
    except (OSError, ValueError):
        return None
    raw = raw.strip()
    return raw or None


def marker_ports() -> list[int]:
    """Ports named by the run-markers currently on disk, ascending.

    Read-only discovery: unlike :func:`marker_path` this never creates
    ``run/`` (a client command that merely *looks* for a gateway should not
    materialise state), and it ignores the marker *contents* entirely — only
    the port encoded in the filename is used, so a planted marker can at worst
    point a client at a port it must still authenticate against.

    A marker's presence does NOT mean the gateway is up: :func:`clear_marker`
    only runs on graceful shutdown, so a crash or SIGKILL leaves the file
    behind. Callers must verify the listener's identity themselves (see the
    module docstring) — reachability alone is not enough, because a client
    command hands the local secret to whatever answers on the port.
    """
    try:
        d = config_dir() / "run"
        if not d.is_dir():
            return []
        names = [p.name for p in d.glob(f"{_MARKER_PREFIX}*{_MARKER_SUFFIX}") if p.is_file()]
    except OSError:
        return []
    ports: set[int] = set()
    for name in names:
        stem = name[len(_MARKER_PREFIX) : -len(_MARKER_SUFFIX)]
        # Strict: digits only (no sign, no whitespace, no "6776.old") and a
        # usable TCP port. Anything else is not a marker this module wrote.
        if not stem.isdigit():
            continue
        port = int(stem)
        if 1 <= port <= 65535:
            ports.add(port)
    return sorted(ports)


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
    """Best-effort: record that this gateway serves *port*, plus its pid.

    Writes two files, then prunes markers left by earlier runs on other ports:

    * ``gateway-<port>.bin`` — the launcher path for the SSH token mint, or
      **empty** when no console script sits beside ``sys.executable`` (a
      source-tree ``python -m kiro_crew`` launch). Mint's shell clause requires
      a non-empty executable path (``[ -n "$__kb" ] && [ -x "$__kb" ]``), so an
      empty marker is inert there — exactly as an absent one was. Writing it
      anyway matters because the *filename* is what client port discovery reads,
      and skipping it denied discovery to precisely the source/dev launches that
      most often run on a non-default port.
    * ``gateway-<port>.pid`` — this process's pid, the identity a client checks
      before trusting the port (see :func:`read_pid`).

    Both go through :func:`kiro_crew.atomic_write.atomic_write`, which uses a
    unique ``mkstemp`` (``O_EXCL``, mode ``0600``) temp then ``os.replace``.
    Using the shared helper — rather than a predictable ``<name>.tmp`` — closes a
    same-user symlink-TOCTOU: a pre-planted ``gateway-<port>.bin.tmp`` symlink
    can no longer redirect the write to truncate another file. Never raises — a
    failed write just leaves mint on the candidate search and discovery on the
    default port.
    """
    launcher = gateway_launcher_path()
    if not launcher:
        logger.debug(
            "No venv kirocrew launcher next to %s — writing port-only run-marker", sys.executable
        )
    try:
        atomic_write(marker_path(port), (launcher + "\n") if launcher else "", mode=0o600)
        atomic_write(pid_path(port), f"{os.getpid()}\n", mode=0o600)
        logger.info("Wrote gateway run-marker for port %s -> %s", port, launcher or "(port only)")
    except Exception as e:  # best-effort — never break startup on a marker write
        logger.warning("Could not write gateway run-marker for port %s: %s", port, e)
    prune_markers(keep_port=port)


def prune_markers(*, keep_port: int) -> None:
    """Remove run-markers for every port except *keep_port*.

    A gateway is a singleton per data home (``gateway.lock``), so any marker
    naming a *different* port belongs to an earlier run that crashed before
    :func:`clear_marker` could fire. Left alone they accumulate one per port ever
    used, and each one costs a client command a listener lookup — making
    discovery slower the longer a dev box churns ports. The live gateway is the
    right place to reap them: it knows which port is current, and it is the only
    writer.

    Best-effort and never raises: a marker we fail to remove only costs a future
    lookup, and the ownership check still rejects it.
    """
    try:
        stale = [p for p in marker_ports() if p != int(keep_port)]
    except Exception:
        return
    for port in stale:
        for path in (marker_path(port), pid_path(port)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        logger.info("Pruned stale gateway run-marker for port %s", port)


def clear_marker(port: int) -> None:
    """Best-effort removal of the run-marker + pid sidecar (on graceful shutdown)."""
    for path in (marker_path(port), pid_path(port)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
