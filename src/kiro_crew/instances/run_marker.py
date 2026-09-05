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
    ``logout`` / ``stop``, via ``port_resolution.resolve_client_port``) find a gateway
    on a non-default port with zero configuration. That path reads only the
    filename, never the recorded launcher path.

    A marker is NOT proof a gateway is there: :func:`clear_marker` runs only on
    graceful shutdown, so a crash or SIGKILL leaves the file behind and an
    unrelated process may since have bound that port. Because client commands
    send the local secret (``X-Local-Secret``) to whatever is listening, the
    consumer MUST verify the listener before trusting a discovered port, using
    the pid sidecar this module writes beside the marker (:func:`read_pid`) — see
    ``port_resolution._gateway_owns_port``. This module deliberately does not offer a
    bare "is something listening" helper, so no caller can mistake reachability
    for identity.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "gateway-"
_MARKER_SUFFIX = ".bin"
_PID_SUFFIX = ".pid"
_START_SUFFIX = ".start"
_SECRET_SUFFIX = ".secret"
_MAX_PID_BYTES = 64
#: Cap for the whole pid file: the digits plus surrounding whitespace and a
#: trailing newline, with room to spare so a legitimate record is never
#: truncated into a malformed one.
_MAX_PID_FILE_BYTES = _MAX_PID_BYTES + 8
#: READ-side cap for the start-identity sidecar. The token is opaque and
#: platform-shaped (a Linux clock-tick count, a macOS ``sec.usec`` pair), so the
#: bound is generous while still refusing a file that is not a sidecar. It bounds
#: what this module will PARSE off disk; it does not shape what the producer
#: emits, which is :func:`kiro_crew.platform_compat.get_process_start_id`.
_MAX_START_TOKEN_BYTES = 128

#: Directory containing gateway sidecars, relative to a data home. Public so a
#: control plane can inspect a pod's isolated home without resolving its own.
RUN_DIR_NAME = "run"


def pid_file_name(port: int) -> str:
    """File name of the PID sidecar written for a gateway serving *port*."""
    return f"{_MARKER_PREFIX}{int(port)}{_PID_SUFFIX}"


def _start_file_name(port: int) -> str:
    """File name of the start-identity sidecar for a gateway serving *port*.

    Mirrors :func:`pid_file_name`. Private because the token is only ever read
    through :func:`read_pid_record_path`, which derives this name from the pid
    path it was handed -- so no consumer outside this module needs to spell it.
    """
    return f"{_MARKER_PREFIX}{int(port)}{_START_SUFFIX}"


def _start_path_for(path: Path) -> Path:
    """Start-identity sidecar sitting beside the pid sidecar at *path*.

    One derivation rule shared by the writer and the reader, so the two cannot
    drift apart on where the token lives.
    """
    return path.with_suffix(_START_SUFFIX)


def pid_start_token(pid: int) -> str:
    """Canonical start-time identity for *pid*, or ``""`` when unknown.

    The single producer of both the value the sidecar records and the value a
    reader compares it against, so the two can never drift in how they spell it.

    It chains two platform helpers because neither covers every host, and using
    either alone costs a platform:

    - :func:`kiro_crew.platform_compat.get_process_start_id` is preferred. It is
      in-process on every platform it implements (no subprocess) and on macOS
      reads ``libproc`` at MICROSECOND resolution, where ``process_start_time``'s
      ``ps -o lstart=`` spelling is only 1-second granular — a pid recycled
      inside the same second would reproduce an identical token there and the
      guard would silently pass.
    - :func:`kiro_crew.platform_compat.process_start_time` is the fallback, and
      it is what keeps **Windows** working: it reads the process creation
      ``FILETIME`` (100-ns units) through a query-only handle, while
      ``get_process_start_id`` implements Linux and macOS only and answers
      ``None`` everywhere else. Without this leg the token is empty on every
      Windows host, so a pod there could never prove ownership at all — an
      unsatisfiable requirement rather than a strict one.

    The fallback's value is whitespace-collapsed because the macOS ``ps``
    spelling is space-padded and the reader requires a single token; Windows
    returns a bare integer, so the collapse is a no-op there.

    ``""`` means "this host would not say", which is the same answer an absent
    sidecar gives. Every caller must read it as *unproven* rather than as a
    match.
    """
    try:
        primary = platform_compat.get_process_start_id(int(pid))
    except Exception:  # best-effort identity probe -- never break a caller
        primary = None
    if primary:
        return str(primary)
    try:
        fallback = platform_compat.process_start_time(int(pid))
    except Exception:
        return ""
    if not fallback:
        return ""
    return "-".join(str(fallback).split())


def _pid_record(pid: int) -> str:
    """Sidecar body for *pid*: exactly the pid and a newline, nothing else.

    Byte-identical to the format this module has always written, which is what
    keeps the reader SHIPPED in older clients working -- it takes the WHOLE file,
    strips it and requires ``isdigit()``. Any second line, a start-time token
    included, makes that reader answer ``None``, and a client sharing this data
    home would then deny a gateway that genuinely is ours. The start identity
    therefore lives in its own ``.start`` sidecar (:func:`_start_path_for`)
    rather than in this file.
    """
    return f"{pid}\n"


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


def _read_sidecar(port: int, suffix: str) -> str:
    """Stripped contents of ``run/gateway-<port><suffix>``, or ``""``.

    Read-only on purpose: it composes the path itself instead of calling
    :func:`marker_path` / :func:`pid_path` / :func:`secret_path`, because those
    go through :func:`_run_dir` and would MATERIALISE ``run/`` — a client command
    that merely looks for a gateway must not create the run dir. (``config_dir()``
    still resolves, and creates, the data home itself.) An unreadable or absent
    file is indistinguishable from an empty one here; each reader decides what
    "" means for its own value.
    """
    try:
        return (
            (config_dir() / "run" / f"{_MARKER_PREFIX}{int(port)}{suffix}")
            .read_text(encoding="utf-8")
            .strip()
        )
    except (OSError, ValueError):
        return ""


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
    return _run_dir() / pid_file_name(port)


def secret_path(port: int) -> Path:
    """Path of the internal-API credential for the gateway serving *port*.

    The credential is a property of ONE gateway generation: it is generated at
    startup and held in memory as the value the auth middleware compares
    against. Keying its file by port keeps it paired with the listener a client
    actually dials, which the single shared ``.local_secret`` cannot do -- that
    file is last-writer-wins per data home, so a second gateway starting in the
    same home silently replaces the credential of the process that owns the
    port, and every internal call then fails 403 until something restarts.

    Lives beside the marker and the pid sidecar, inside the ``0700`` ``run/``
    dir on the ``is_sensitive_path`` floor, and is written ``0600``.
    """
    return _run_dir() / f"{_MARKER_PREFIX}{int(port)}{_SECRET_SUFFIX}"


def read_secret(port: int) -> str:
    """Internal-API credential recorded for *port*, or ``""`` when absent.

    Read-only: never creates ``run/`` (mirrors :func:`read_pid`, so a client
    merely looking for a gateway materialises no state). An empty return means
    "no per-port credential here" -- the caller falls back to the shared
    ``.local_secret`` for gateways predating the per-port file. Presence is NOT
    proof the recorded gateway still owns the port; that remains
    ``port_resolution._gateway_owns_port``'s job.
    """
    return _read_sidecar(port, _SECRET_SUFFIX)


def _read_start_token(pid_path: Path) -> str:
    """Start identity recorded beside the pid sidecar at *pid_path*, or ``""``.

    Bounded, ASCII, single-token defensive parsing of an on-disk file -- an
    absent, oversized, undecodable or whitespace-bearing file all read as ``""``
    = unproven, never as a match. Single-token is the producer's own contract
    (``get_process_start_id`` is documented colon-free and single-token), not a
    normalisation this module performs. The read never creates the file or any
    parent directory. The token is compared VERBATIM against a freshly probed
    one, so a malformed file cannot accidentally agree with anything.
    """
    try:
        with _start_path_for(pid_path).open("rb") as stream:
            blob = stream.read(_MAX_START_TOKEN_BYTES + 1)
    except (OSError, ValueError):
        return ""
    if len(blob) > _MAX_START_TOKEN_BYTES:
        return ""
    try:
        token = blob.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    # Single-token by contract (``get_process_start_id`` is documented colon-free
    # and single-token), so anything carrying inner whitespace or a control
    # character is not a sidecar this module wrote.
    if not token or not token.isprintable() or token.split() != [token]:
        return ""
    return token


def read_pid_record_path(path: Path) -> tuple[int, str] | None:
    """``(pid, start_token)`` recorded at *path*, or ``None`` when there is no
    positive ASCII-decimal pid to read.

    The pid comes from *path* itself, whose body is the bare pid and nothing
    else (:func:`_pid_record`); the start identity comes from the ``.start``
    sidecar beside it (:func:`_start_path_for`). Splitting them is what lets the
    pid file stay byte-identical to the format every shipped reader expects.
    Only the FIRST line of *path* is parsed, so a two-line record left behind by
    an intermediate build still yields its pid -- which fails closed exactly the
    same way (its token is not where a reader looks) while producing the
    accurate "record present, no start identity" diagnostic rather than "no
    record at all".

    Both reads are capped and neither creates a file or any parent directory.

    ``start_token`` is ``""`` when no sidecar is readable -- written by a gateway
    that predates the start-identity binding, or by one on a host that could not
    read its own. It is NOT a wildcard: a caller that needs to know the pid
    still names the same process must treat an empty token as unproven, because
    the whole point of the token is that a recycled pid cannot reproduce it.
    """
    try:
        with path.open("rb") as stream:
            blob = stream.read(_MAX_PID_FILE_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(blob) > _MAX_PID_FILE_BYTES:
        return None
    raw = blob.split(b"\n")[0].strip()
    if not raw or len(raw) > _MAX_PID_BYTES or not raw.isascii() or not raw.isdigit():
        return None
    pid = int(raw)
    if pid <= 0:
        return None
    return pid, _read_start_token(path)


def _read_pid_path(path: Path) -> int | None:
    """Positive PID recorded at *path*, ignoring its start identity, or ``None``.

    Reachability-style accessor for the one caller that only needs the number.
    Private on purpose: :func:`read_pid_record_path` is what a caller reaches for
    when the answer must also prove the pid still names the process it was
    recorded for, and an equally public "just the pid" helper invites skipping
    that proof.
    """
    record = read_pid_record_path(path)
    return record[0] if record is not None else None


def read_pid(port: int) -> int | None:
    """Pid recorded by the gateway serving *port*, or ``None``.

    The identity claim a client uses before trusting a discovered port. It is
    trustworthy because the sidecar is written ``0600`` inside the ``0700``
    ``run/`` dir (which is on the ``is_sensitive_path`` floor, so agent file
    tools cannot write it either): another local user cannot point it at a
    process of theirs. It is NOT proof of liveness — a crashed gateway leaves
    its pid behind — so the caller must also confirm that pid currently holds
    the port. See ``port_resolution._gateway_owns_port``.

    Read-only: never creates ``run/``.
    """
    path = config_dir() / RUN_DIR_NAME / pid_file_name(port)
    return _read_pid_path(path)


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
    return _read_sidecar(port, _MARKER_SUFFIX) or None


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

    Writes three files, then prunes markers left by earlier runs on other ports:

    * ``gateway-<port>.bin`` — the launcher path for the SSH token mint, or
      **empty** when no console script sits beside ``sys.executable`` (a
      source-tree ``python -m kiro_crew`` launch). Mint's shell clause requires
      a non-empty executable path (``[ -n "$__kb" ] && [ -x "$__kb" ]``), so an
      empty marker is inert there — exactly as an absent one was. Writing it
      anyway matters because the *filename* is what client port discovery reads,
      and skipping it denied discovery to precisely the source/dev launches that
      most often run on a non-default port.
    * ``gateway-<port>.start`` — this process's start-time identity
      (:func:`pid_start_token`), or **empty** on a host that will not report one.
      It is what makes the pid record's FRESHNESS provable: a pid alone cannot be
      told apart from the same number recycled onto an unrelated process after a
      crash left the sidecar behind, and a reader that must not hand a credential
      to that process needs to know the difference. Written even when empty, so a
      token this generation cannot produce can never be a PREDECESSOR's token
      left in place.
    * ``gateway-<port>.pid`` — this process's pid and nothing else, the identity
      a client checks before trusting the port (see :func:`read_pid`). It stays a
      bare pid so the whole-file reader shipped in older clients keeps parsing it
      (:func:`_pid_record`), which is why the start identity is a separate file
      rather than a second line.

    The start file is written FIRST. Both orders fail closed — a pid without a
    token reads as unproven, and a token without a pid is never consulted — but
    this one narrows the window in which a published pid has no token to none of
    the gateway's own making.

    All three go through :func:`kiro_crew.atomic_write.atomic_write`, which uses a
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
    pid = os.getpid()
    try:
        atomic_write(marker_path(port), (launcher + "\n") if launcher else "", mode=0o600)
        token = pid_start_token(pid)
        atomic_write(_start_path_for(pid_path(port)), (token + "\n") if token else "", mode=0o600)
        atomic_write(pid_path(port), _pid_record(pid), mode=0o600)
        logger.info("Wrote gateway run-marker for port %s -> %s", port, launcher or "(port only)")
    except Exception as e:  # best-effort — never break startup on a marker write
        logger.warning("Could not write gateway run-marker for port %s: %s", port, e)
    prune_markers(keep_port=port)


def prune_markers(*, keep_port: int) -> None:
    """Remove run-markers for every port except *keep_port* and any LIVE sibling.

    Stale markers accumulate one per port ever used, and each one costs a client
    command a listener lookup, so the live gateway reaps them: it knows which port
    is current, and it is the only writer.

    What it must NOT reap is a sibling that is still serving. ``gateway.lock``
    makes a gateway a singleton per data home only when every start goes through
    it; in practice one machine runs several -- a second gateway launched by hand,
    one started from another checkout that inherits the default data home, a
    cutover overlapping its predecessor -- and those really do share a home. A
    blanket prune then deletes a LIVE gateway's marker and pid sidecar, which
    makes it undiscoverable to `token` / `status` / `stop` and removes the very
    evidence the credential writer uses to avoid clobbering that gateway's
    credential. So each candidate is checked against the same ownership proof its
    readers use (recorded pid, holds the port, same uid, argv looks like a
    gateway) and skipped when it passes.

    Best-effort and never raises: a marker we fail to remove only costs a future
    lookup, and the ownership check still rejects it.

    It removes the marker, the pid sidecar and that pid's start identity, but
    NEVER the credential, because ``_gateway_owns_port`` cannot tell a dead
    gateway from an unprovable one. It
    fails closed by RETURNING FALSE -- non-POSIX returns False outright, and a
    missing or throwing listener-lookup tool is folded into False as well -- so
    False means "ownership not proven", not "process gone". Deleting on False
    would therefore delete a LIVE incumbent's credential on every Windows host
    and on any host without listener tooling; its clients would fall back to the
    shared ``.local_secret`` that a newcomer may have replaced, and the prune
    would CAUSE the 403 the per-port credential exists to prevent.

    A credential left behind is the safe residue: it is only reachable for a port
    whose gateway is gone, which is already unreachable. Deletion belongs to
    :func:`clear_marker`, where the gateway is authoritatively done with the port.
    """
    # Function-local: port_resolution imports this module, so a module-level
    # import would be circular.
    from kiro_crew import port_resolution

    try:
        stale = [p for p in marker_ports() if p != int(keep_port)]
    except Exception:
        return
    for port in stale:
        try:
            if port_resolution._gateway_owns_port(int(port)):
                logger.info(
                    "Keeping gateway run-marker for port %s: that gateway is still live",
                    port,
                )
                continue
        except Exception:
            logger.debug("Ownership check failed for port %s", port, exc_info=True)
        for path in (marker_path(port), pid_path(port), _start_path_for(pid_path(port))):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        logger.info("Pruned stale gateway run-marker for port %s", port)


def clear_marker(port: int) -> None:
    """Best-effort removal of the run-marker, pid sidecar, start identity and credential.

    The start identity goes with the pid it attests: on its own it names nothing,
    and leaving it beside a pid file a later gateway rewrites is exactly the
    stale-token pairing the freshness check exists to refuse.

    The credential goes too: it names a generation that no longer owns the port,
    so leaving it behind would let a client authenticate with a value the next
    owner never had. A crash still leaves all four (nothing runs), which is why
    every consumer verifies ownership rather than trusting presence.
    """
    for path in (
        marker_path(port),
        pid_path(port),
        _start_path_for(pid_path(port)),
        secret_path(port),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
