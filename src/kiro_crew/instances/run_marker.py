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

import contextlib
import logging
import os
import sys
from collections.abc import Iterator
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


def secret_file_name(port: int) -> str:
    """File name of the credential sidecar written for a gateway serving *port*.

    Public for the same reason as :data:`RUN_DIR_NAME`: a control plane reads a
    pod's credential out of the pod's own isolated home, which :func:`secret_path`
    cannot name because it resolves against the CALLING process's data home. The
    name is produced here so the reader and the writer share one spelling.

    Names a PORT, which is a set of listeners rather than one: several addresses
    can carry the same port number, so a reader that must know WHICH listener it
    reached wants :func:`listener_secret_file_name` instead.
    """
    return f"{_MARKER_PREFIX}{int(port)}{_SECRET_SUFFIX}"


def encode_bind_address(host: str) -> str:
    """Filename-safe spelling of the bind address *host*.

    ``:`` is legal in an IPv6 literal and illegal in a Windows filename, so it is
    the one character that has to change. The mapping is injective over IP
    literals, which draw on hex digits, ``.`` and ``:`` alone, so two different
    addresses can never collide on one file name -- which is the whole property
    the caller is buying.
    """
    return host.replace(":", "_")


def listener_secret_file_name(port: int, host: str) -> str:
    """File name of the credential for the listener at *host* on *port*.

    A listener is an address AND a port. One port number can carry several
    listeners at once -- ``KIROCREW_BIND=::1`` binds the v6 loopback and leaves
    IPv4 ``127.0.0.1:<port>`` free for anything else to take -- so a name keyed
    by port alone names a SET, and a reader resolving it can be handed the
    credential of a listener it never spoke to. Keying the name by both makes
    that unrepresentable: the reader asks for the address it dialled and either
    gets that listener's credential or nothing.
    """
    return f"{_MARKER_PREFIX}{int(port)}-{encode_bind_address(host)}{_SECRET_SUFFIX}"


def _start_path_for(path: Path) -> Path:
    """Start-identity sidecar sitting beside the pid sidecar at *path*.

    The one derivation rule in this module, shared by the writer and every
    reader, so the two cannot drift apart on where the token lives.

    Keyed on the pid PATH rather than on a port because the pid sidecar is not
    always inside this process's ``run/``: a pod keeps its own in an isolated
    data home (``pod.runtime._pod_pid_record_path``), which a bare port cannot
    name. ``test_pod_api.py`` re-spells the ``.start`` suffix literally, on
    purpose, as a tripwire for renaming it here without updating its readers.
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
    """Return ``<config_dir>/run`` (created owner-only); mirrors sandbox._ensure_run_dir.

    ``restrict_dir_to_owner``, not ``os.chmod(0o700)``: on POSIX the two are the
    same call, but a mode is meaningless on Windows, where a bare chmod leaves
    the directory on the inherited DACL. This directory holds the gateway's
    credential, its pid — the identity a client checks before trusting a port —
    and the marker naming an executable mint will exec, so it must be owner-only
    on every platform. The directory helper is also what makes the sidecars
    safe: its Windows grants carry ``(OI)(CI)``, so files created inside inherit
    owner-only, which ``atomic_write(mode=0o600)`` cannot deliver there.

    Best-effort by contract, unlike the fail-loud helper it calls: ``_run_dir``
    is reached through :func:`secret_path` on the dashboard's startup path, and
    a directory that cannot be tightened (owned by another uid, an unresolvable
    SID) must not take the gateway down.
    """
    d = config_dir() / "run"
    d.mkdir(parents=True, exist_ok=True)
    try:
        # exist_ok does not re-apply the mode/DACL to an existing dir, so tighten
        # unconditionally rather than only on the create.
        platform_compat.restrict_dir_to_owner(d)
    except OSError:
        logger.debug("could not restrict run dir %s to its owner", d, exc_info=True)
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
    return _run_dir() / secret_file_name(port)


def listener_secret_path(port: int, host: str) -> Path:
    """Path of the credential for the listener at *host* on *port*.

    Sits beside :func:`secret_path` in the same owner-only ``run/`` dir and is
    written ``0600`` the same way. The two hold the same value for the same
    gateway generation and differ only in what their names identify: this one
    names ONE listener, which is what a client that dialled a specific address
    needs in order to know the credential belongs to the party it reached.
    """
    return _run_dir() / listener_secret_file_name(port, host)


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


#: How long a marker-lock acquire waits before giving up.
#:
#: Deliberately the same number as the shutdown's own wait for a stalled marker
#: write (``_MARKER_WRITE_WAIT_SECS`` in the gateway), because that is the
#: timescale this lock sits inside rather than a new one: shutdown already treats
#: 5 seconds as the point at which a boot write is presumed stalled, so a lock
#: that waited longer could turn a write which WOULD have landed inside that
#: window into one that misses it, and a lock that waited less could refuse a
#: peer that shutdown still considers live.
#:
#: A legitimate hold is three ``atomic_write`` calls or a handful of unlinks --
#: single-digit milliseconds even on a slow filesystem -- so this is ~three orders
#: of magnitude of headroom and never refuses a real contender. The ceiling
#: matters because BOTH holders are latency-sensitive: the graceful path is
#: flushing live state when it runs, and waiting on a wedged peer there costs the
#: flush. ``platform_compat._LOCK_TIMEOUT_SECS`` (300s) is the ceiling for long
#: critical sections and would be that hang.
_MARKER_LOCK_TIMEOUT_SECS = 5.0


def marker_lock_path(port: int) -> Path:
    """Path of the per-port lock serialising marker reads against marker writes.

    One lock per PORT, because the port is what two generations contend for: the
    outgoing gateway's cleanup and the incoming one's publication are the two
    sides, and they are never the same process.
    """
    return _run_dir() / f"{_MARKER_PREFIX}{int(port)}.lock"


@contextlib.contextmanager
def _marker_lock(port: int) -> Iterator[bool]:
    """Hold the per-port marker lock for the block; yield whether it was taken.

    This exists because a CHECK and the WRITE it authorises must not be separable.
    Reading the pid record, deciding the record names someone else, and then
    writing is three steps, and a successor can publish between the first and the
    third -- at which point the decision was true when it was made and false when
    it was acted on. Holding the lock across all three is what makes the decision
    still true at the moment it is used.

    Yields False rather than raising when the lock cannot be taken, and every
    caller treats False as "do nothing". That is the fail-closed direction for
    both callers, and they get there from opposite starting points:

    * A write that does not happen costs this generation its discovery marker,
      which is worth nothing to a process that does not hold the port.
    * A delete that does not happen leaves a stale entry, which costs one refused
      round trip -- the credential is scoped to a generation, so it cannot
      authenticate against a later listener. Deleting WITHOUT the lock is the
      unsafe direction: that is precisely how a shutdown eats its successor's
      record.

    Never raises. A data home on a filesystem with no working locks (some network
    mounts) yields False forever, which degrades to the two costs above rather
    than to an unserialised write.
    """
    fd: int | None = None
    guard = None
    try:
        # O_RDWR | O_CREAT, never O_TRUNC: truncating before the lock is held lets
        # a contender observe the file empty mid-section (GH-9248), which is why
        # platform_compat.open_lock_file exists and why this mirrors it.
        fd = os.open(os.fspath(marker_lock_path(port)), os.O_RDWR | os.O_CREAT, 0o600)
        guard = platform_compat.file_lock(fd, exclusive=True, timeout=_MARKER_LOCK_TIMEOUT_SECS)
        guard.__enter__()
    except (OSError, ValueError, RuntimeError):
        if guard is not None:
            guard = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
        logger.debug("Could not take the marker lock for port %s; doing nothing.", port)
        yield False
        return
    try:
        yield True
    finally:
        try:
            guard.__exit__(None, None, None)
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _record_names_another_live_gateway(port: int, pid: int) -> bool:
    """True when *port*'s pid record names a LIVE process that is not *pid*.

    The proof is the start identity, not the pid number: a pid alone cannot be
    told apart from the same number recycled onto an unrelated process after a
    crash left the sidecar behind. So the recorded token must still match what
    :func:`pid_start_token` reports for that pid, and an EMPTY token counts as
    unproven rather than as a wildcard -- that is the same rule
    :func:`clear_late_marker_write` applies, for the same reason.

    False on every unprovable reading, which is the direction that fails safe
    HERE: this gates a gateway's own boot-time write, and a boot that refuses to
    record its marker on a doubt loses port discovery for the rest of its life.
    The case being excluded is narrow and specific -- a successor already holds
    the port and has published -- so only a positive proof of it declines.
    """
    record = read_pid_record_path(pid_path(port))
    if record is None:
        return False
    recorded_pid, recorded_token = record
    if recorded_pid == pid or not recorded_token:
        return False
    return pid_start_token(recorded_pid) == recorded_token


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
    cannot redirect the write to truncate another file. Never raises — a
    failed write just leaves mint on the candidate search and discovery on the
    default port.
    """
    launcher = gateway_launcher_path()
    if not launcher:
        logger.debug(
            "No venv kirocrew launcher next to %s — writing port-only run-marker", sys.executable
        )
    pid = os.getpid()
    with _marker_lock(port) as locked:
        if not locked:
            # No lock, no write. The check below would be unserialised, which is
            # exactly the race the lock exists to close -- and an unwritten marker
            # costs this generation only its own discovery entry.
            logger.info(
                "Not writing the run-marker for port %s: the marker lock is unavailable.",
                port,
            )
            return
        if _record_names_another_live_gateway(port, pid):
            # A boot-path write that stalled (a slow fs, a suspended VM) can land
            # after this generation gave up the port and a SUCCESSOR bound it and
            # published. Writing then replaces that successor's pid record with
            # this process's own, and every client that checks the record before
            # trusting the port is told the wrong owner. Declining costs this
            # generation its discovery marker, which is worth nothing to a process
            # that does not hold the port.
            #
            # The check and the writes below are ONE critical section. Separated,
            # a successor can publish between them, so the decision would be true
            # when made and false when acted on -- and the write would then be the
            # very overwrite this check exists to prevent.
            logger.info(
                "Not writing the run-marker for port %s: the pid record names another live gateway.",
                port,
            )
            return
        try:
            atomic_write(marker_path(port), (launcher + "\n") if launcher else "", mode=0o600)
            token = pid_start_token(pid)
            atomic_write(
                _start_path_for(pid_path(port)), (token + "\n") if token else "", mode=0o600
            )
            atomic_write(pid_path(port), _pid_record(pid), mode=0o600)
            logger.info(
                "Wrote gateway run-marker for port %s -> %s", port, launcher or "(port only)"
            )
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
    gateway from an unprovable one. That check
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


def listener_secret_paths(port: int) -> list[Path]:
    """Every listener-keyed credential recorded for *port*, whatever the address.

    The address is part of the name, so a reader that knows which address it
    dialled can name its own entry directly. A caller that must act on ALL of
    them -- cleanup, which knows a generation is gone but not which addresses it
    bound -- cannot, and enumerating is the only honest answer. Read-only: never
    creates ``run/``.
    """
    try:
        d = config_dir() / RUN_DIR_NAME
        if not d.is_dir():
            return []
        prefix = f"{_MARKER_PREFIX}{int(port)}-"
        return sorted(p for p in d.glob(f"{prefix}*{_SECRET_SUFFIX}") if p.is_file())
    except OSError:
        return []


#: Listener addresses THIS process published a credential for, per port. Written
#: by :func:`note_published_listener` at publication time and read only by
#: :func:`clear_marker`, which must delete its own entries and no one else's.
#: In-process rather than on disk on purpose: the only caller of ``clear_marker``
#: is the owning gateway's own graceful shutdown, so the knowledge is already
#: here, and a file recording it would itself need an ownership proof.
_PUBLISHED_LISTENERS: dict[int, set[str]] = {}


def note_published_listener(port: int, address: str) -> None:
    """Record that this process published a listener credential for *address*.

    Called after the write succeeds, so a failed publication leaves nothing to
    delete. Idempotent, and quietly ignores an empty address -- the caller
    suppresses the listener-keyed write in that case too.
    """
    if not address:
        return
    _PUBLISHED_LISTENERS.setdefault(int(port), set()).add(address)


def published_listeners(port: int) -> frozenset[str]:
    """Addresses this process published a listener credential for on *port*."""
    return frozenset(_PUBLISHED_LISTENERS.get(int(port), ()))


def clear_marker(port: int) -> None:
    """Best-effort removal of the run-marker, pid sidecar, start identity and credentials.

    The start identity goes with the pid it attests: on its own it names nothing,
    and leaving it beside a pid file a later gateway rewrites is exactly the
    stale-token pairing the freshness check exists to refuse.

    The credentials go too -- the port-keyed one, and the listener-keyed entries
    THIS PROCESS published (:func:`note_published_listener`). Each names a
    generation that does not own the port, so leaving one behind would let a
    client authenticate with a value the next owner never had.

    Scoped to this process's own entries rather than every entry naming the port,
    because a port and a listener are not the same thing: two gateways in one data
    home can hold the same port on different addresses, and enumerating by port
    would delete the OTHER one's credential while it is still serving -- answering
    403 to every client that had already read it. What cannot be proven is not
    deleted.

    An entry this process did not publish is therefore left in place, and that is
    safe rather than merely conservative: a client reading a stale entry is
    refused by the gateway and moves on to the next candidate for that family
    (see the walk in ``listenerSecretsFor``), so the cost is one wasted round trip
    -- while deleting a live sibling's entry costs that sibling every client it
    had. A crash leaves everything (nothing runs), which is why every consumer
    verifies ownership rather than trusting presence.
    """
    for path in (
        marker_path(port),
        pid_path(port),
        _start_path_for(pid_path(port)),
        secret_path(port),
        *(listener_secret_path(port, address) for address in published_listeners(port)),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _PUBLISHED_LISTENERS.pop(int(port), None)


def clear_late_marker_write(port: int) -> bool:
    """Undo a :func:`write_marker` that landed after shutdown had already cleared.

    Answers True when files were removed, False when the pid record names another
    process and nothing was touched.

    What separates this from :func:`clear_marker` is WHEN each one runs.
    ``clear_marker`` runs while the shutting-down gateway still holds its
    listener, so no other process can have published under these names yet, and
    clearing by location is therefore clearing its own. This runs from a detached
    thread at an arbitrary later point, which may be after the listener is free
    and a replacement gateway has bound the port and published its own state. At
    that point a location does not identify an owner, so the action is scoped
    twice, and the two scopes cover different things:

    * Never a credential, whatever the pid record says. This is the
      unconditional half, and it is the one that matters: the late write this
      exists to undo creates no credential, so deleting one reaches past the
      mistake being corrected, and deleting a successor's would answer 403 to
      every client that had already read it.
    * Only while the pid record names THIS process, which declines the marker,
      pid and start identity of a gateway that is not this one. Its reach is
      narrower than it looks, and the limit is worth stating: a late
      :func:`write_marker` rewrites that record with this process's own pid, so
      after a write that landed the answer is True by construction. What the
      check covers is every path where the write did NOT land -- it is
      best-effort and swallows its own failures -- plus any caller that reaches
      here without writing first. It cannot undo a clobber that already
      happened, and a pid read taken BEFORE the write cannot substitute for it:
      the successor appears after that read, so the value it returns is this
      port's previous occupant, not its next one. Keeping a successor's marker
      intact means guarding the WRITE, which is a change on the gateway boot
      path rather than here.

    An unreadable pid record answers False for the same reason: an owner that
    cannot be established is treated as somebody else's.
    """
    with _marker_lock(port) as locked:
        if not locked:
            # No lock, no delete. This is the fail-closed direction HERE: a
            # surviving marker costs one refused round trip, while deleting
            # unserialised is how this function eats a successor's record -- the
            # very outcome it exists to avoid.
            return False
        try:
            if read_pid(port) != os.getpid():
                return False
        except Exception:
            return False
        # Inside the lock with the check, so no successor can publish between
        # reading the record and acting on it.
        for path in (marker_path(port), pid_path(port), _start_path_for(pid_path(port))):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return True
