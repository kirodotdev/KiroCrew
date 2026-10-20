"""Probe and size the per-spawn systemd cgroup scope.

Whether a transient user scope can be created, the agents slice it nests under, and the
limits read from config. One of the owners ``kiro_crew.sandbox`` is carved into: every
name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import socket
import sys
import threading
import time
import urllib.parse

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir

# The single source of the agents-slice name is sandbox_tiers (beside
# _IN_SANDBOX_MARKER, in the lowest sandbox layer); re-exported below so this
# module's public _CGROUP_AGENTS_SLICE name and every reader of it stay unchanged.
from kiro_crew.sandbox_tiers import _CGROUP_AGENTS_SLICE  # noqa: F401

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


# ── cgroup v2 scope enforcement (fork bomb + memory DoS) ──
# The RLIMIT preexec (resource_limit_preexec) caps a SINGLE process's FDs, but
# RLIMIT is the wrong tool for the headline threats: RLIMIT_NPROC is
# per-real-UID (not per-spawn-subtree) and RLIMIT_AS caps virtual not resident
# memory. cgroup v2 pids.max / memory.max are the correct per-cgroup ceilings —
# they bound the agent + all its MCP-server/tool descendants as one unit, and
# the kernel enforces at fork()/alloc time (no reaper race). We place each
# agent-influenced spawn in a transient systemd --user --scope, which works
# UNPRIVILEGED when the user session has cgroup v2 delegation (pids + memory
# controllers). See docs/architecture/resource-protection.md.

# Default cgroup ceilings (per agent scope). Overridable via the same
# ``resource_limits`` config block used by apply_resource_limits.
_CGROUP_DEFAULT_MAX_PROCESSES = 8192  # pids.max counts TASKS (threads), not processes;
# 1024 starved legitimate JVM build trees (Gradle + parallel test workers need
# thousands of threads -> pthread_create EAGAIN / 'unable to create native thread'
# while the host is idle); 8192 still bounds fork bombs which spawn tens of
# thousands of tasks near-instantly. Override via resource_limits.max_processes.

# CPUWeight — proportional CPU share for agent scopes (systemd default is 100).
# Setting 50 makes agent scopes yield to interactive work under CPU contention
# while still using 100% of idle CPU — proportional share, never a hard throttle.
# Both grok-build and OpenClaw ship no default CPU quota; fair-share weight is
# the correct default for agent workloads that include legitimate builds.
_CGROUP_DEFAULT_CPU_WEIGHT = 50

# The memory.max default is HOST-PROPORTIONAL, not a flat cap: the agent
# subprocess tree may occupy up to this fraction of physical RAM before the
# kernel OOM-kills the scope. This is a PER-SCOPE ceiling (each spawn gets its
# own transient scope), so 65% bounds a single runaway tree to a share that
# leaves headroom for the OS + gateway — it is NOT an aggregate host guarantee
# across many concurrent scopes. It gives the agent real headroom on the 16–32
# GB machines this targets (16 GB → ~10.6 GB, 32 GB → ~21.3 GB) — where a flat
# 8 GB cap was both too tight on big boxes and too loose on small ones. There
# is deliberately NO floor: a floor could push a tiny box above 65%, and 65% is
# the ceiling on our take.
_CGROUP_MEMORY_FRACTION = 0.65
# Fallback memory.max (MB) used only when physical RAM can't be read (sysconf
# missing/unknown). The cgroup path is Linux-only, where SC_PHYS_PAGES exists,
# so this is a belt-and-suspenders default, not the normal path.
_CGROUP_FALLBACK_MAX_MEMORY_MB = 8192


def _instance_slice_token() -> str:
    """A short, systemd-safe token identifying THIS instance's data home.

    Hex only, and specifically NO dashes: systemd reads a dash in a slice name
    as a hierarchy separator, so a token containing one would silently insert
    another slice level (and two tokens could collide on a shared prefix).

    Keyed on the resolved data home because that is already the ownership key
    the rest of the codebase uses — ``cleanup_orphaned_session_roots`` reaps
    from a per-data-home account book, which is exactly why it cannot reach a
    co-resident instance's children. Deriving the slice from the same key keeps
    one notion of "whose process is this" instead of adding a second.
    """
    return hashlib.sha256(str(config_dir()).encode("utf-8")).hexdigest()[:12]


def _agents_slice_name() -> str:
    """The slice new agent scopes are placed in: one child per instance.

    Every gateway on a host shared ONE slice, because the name was a module
    constant with no per-instance component. Nothing downstream could then tell
    one instance's scopes from another's, since a scope's identity is its slice
    plus a timestamp and neither names an owner. That is a correctness floor
    for anything operating on scopes as a population: a host routinely runs
    several gateways at once — ``kirocrew pod up`` creates one per pod by
    design — so "every scope in the agents slice" is not "my scopes", and a
    sweep that assumes it is reaches into a live co-resident session.

    Returns a dash-nested CHILD of :data:`_CGROUP_AGENTS_SLICE`, so the parent
    stays the aggregate enforcement boundary: cgroup v2 bounds a descendant by
    the minimum effective limit along its ancestor chain, so the MemoryHigh /
    MemoryMax on the parent keep applying to every instance's scopes exactly as
    before. Callers that match on the parent's name in a cgroup path (it is a
    path component of every scope either way) are likewise unaffected.

    Degrades to the bare parent slice if the data home cannot be resolved: a
    missing ownership component must not fail the spawn, matching how an
    unavailable cgroup backend is handled.
    """
    try:
        token = _instance_slice_token()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "could not derive a per-instance agent slice (%s); falling back to "
            "the shared %s — scopes from this instance will not be "
            "distinguishable from a co-resident gateway's",
            exc,
            _CGROUP_AGENTS_SLICE,
        )
        return _CGROUP_AGENTS_SLICE
    return f"{_CGROUP_AGENTS_SLICE[: -len('.slice')]}-{token}.slice"


def _default_max_memory_mb() -> int:
    """Return the default cgroup ``memory.max`` in MB: a fixed fraction
    (:data:`_CGROUP_MEMORY_FRACTION`) of physical RAM, so the ceiling scales
    with the machine instead of being a flat cap. Falls back to
    :data:`_CGROUP_FALLBACK_MAX_MEMORY_MB` if host RAM can't be determined.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    # Windows has no ``os.sysconf``, so the probe above raises AttributeError and
    # would leave a FLAT cap that ignores the machine entirely. That is not a
    # cosmetic gap now that ``apply_windows_resource_ceiling`` consumes this
    # value: on an 8 GB host the fallback EQUALS physical RAM and on a smaller
    # one it exceeds it, so the Job object's memory limit could never engage
    # before the host was exhausted — the ceiling would exist and enforce
    # nothing. Ask the kernel instead. ``system_memory()`` returns None off
    # Windows, so POSIX still reaches the fallback below unchanged.
    mem = platform_compat.system_memory()
    if mem is not None:
        mb = int(mem[0] * _CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    return _CGROUP_FALLBACK_MAX_MEMORY_MB


# Cached (available, reason) probe result. Half of what it depends on is NOT
# process-stable: the systemd user manager, its bus and the delegated user slice
# belong to the user's LOGIN, and logind tears all three down at the last logout
# on a host without lingering while ``XDG_RUNTIME_DIR`` stays set in our
# environment. So the cache is keyed on a cheap fingerprint of that session state
# (:func:`_cgroup_scope_session_fingerprint`) and re-validated on a TTL; see
# :func:`_probe_cgroup_scope`.
_CGROUP_SCOPE_PROBE: tuple[bool, str] | None = None
# The fingerprint and monotonic timestamp the cached probe was computed against.
_CGROUP_SCOPE_PROBE_SESSION: tuple[object, ...] | None = None
_CGROUP_SCOPE_PROBE_AT = 0.0
# Upper bound on how long a cached probe is trusted without recomputing it. The
# fingerprint catches the normal logout (logind removes the runtime directory);
# this catches a manager that died and left its socket file behind, which no
# stat can tell apart from a live one. A recompute is a handful of small file
# reads and one non-blocking connect(), so once a minute costs nothing.
_CGROUP_SCOPE_PROBE_TTL_SECONDS = 60.0
_CGROUP_SCOPE_PROBE_LOCK = threading.Lock()
_CGROUP_WARNED = False
# Appended to every "no reachable user manager" reason, so both the one-time
# warning and the mid-run transition warning name the fix an operator can apply.
_CGROUP_LINGER_REMEDY = (
    "the systemd user manager is not running for this user, which logind does "
    "when the last login session ends on a host without lingering; "
    "`loginctl enable-linger $USER` keeps it running (it needs sudo on a managed "
    "host), and the ceiling returns on its own once the "
    "manager is back"
)


def _warn_cgroup_unavailable(reason: str) -> None:
    """Emit the one-time SECURITY warning for a host without cgroup enforcement.

    Shared by the per-spawn wrapper and the slice-limit application so a host
    where delegation is missing produces exactly ONE warning, no matter which
    site notices first — both react to the same host condition.
    """
    global _CGROUP_WARNED
    if _CGROUP_WARNED:
        return
    _CGROUP_WARNED = True
    logger.warning(
        "SECURITY: cgroup v2 scope enforcement unavailable (%s); agent "
        "subprocess fork-bomb / memory-DoS ceilings are NOT enforced on "
        "this host. RLIMIT_NOFILE still applies. See "
        "docs/architecture/resource-protection.md.",
        reason,
    )


def _user_slice_controllers_path() -> str:
    """``cgroup.controllers`` of this uid's logind user slice."""
    return f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/cgroup.controllers"


def _user_bus_socket_paths() -> tuple[str, ...]:
    """Filesystem sockets ``systemd-run --user`` may dial, most specific first.

    ``DBUS_SESSION_BUS_ADDRESS``'s ``unix:path=`` entries, then the two sockets
    systemd derives from ``XDG_RUNTIME_DIR``: the user manager's private socket
    (``systemd-run --scope`` tries it first) and the session bus. Abstract and
    non-unix transports are skipped -- they have no path to stat or dial here.

    A D-Bus address value may percent-escape any byte (the spec's escaping is
    ``%XX`` over the raw bytes), so it is decoded before use; probing the
    escaped spelling literally would miss a live bus and drop the ceiling. A
    decoded value carrying a NUL cannot name any filesystem socket and would
    make ``os.stat`` raise ``ValueError`` on the spawn path, so it is skipped.
    """
    paths: list[str] = []
    for entry in os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").split(";"):
        transport, _, params = entry.partition(":")
        if transport != "unix":
            continue
        for param in params.split(","):
            key, _, value = param.partition("=")
            if key != "path" or not value:
                continue
            decoded = urllib.parse.unquote_to_bytes(value)
            if decoded and b"\0" not in decoded:
                paths.append(os.fsdecode(decoded))
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime_dir:
        paths.append(os.path.join(runtime_dir, "systemd", "private"))
        paths.append(os.path.join(runtime_dir, "bus"))
    return tuple(dict.fromkeys(paths))


def _stat_identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _cgroup_scope_session_fingerprint() -> tuple[object, ...]:
    """Cheap per-spawn identity of the login-scoped state the probe depends on.

    Stats only -- no reads, no connects -- so it is safe on the event loop at
    every spawn. Changes when logind removes or recreates the runtime directory,
    when the user manager re-creates its sockets (new inode), when the user
    slice comes or goes, or when the gateway's bus locators change. Off Linux it
    is constant: nothing there is session-scoped.
    """
    if sys.platform != "linux":
        return ()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    sockets = _user_bus_socket_paths()
    return (
        runtime_dir,
        _stat_identity(runtime_dir) if runtime_dir else None,
        sockets,
        tuple(_stat_identity(path) for path in sockets),
        _stat_identity(_user_slice_controllers_path()),
    )


def _user_bus_reachable() -> tuple[bool, str]:
    """True when some :func:`_user_bus_socket_paths` socket accepts a connection.

    A socket FILE is not a bus: a manager that died leaves it behind (refused),
    and a seccomp or LSM policy can refuse ``connect()`` outright (EPERM) --
    either way ``systemd-run`` dies with ``Failed to connect to bus`` before it
    execs the command it wraps. The connect is NON-BLOCKING, so a listener with
    a full backlog answers EAGAIN instead of stalling the caller; that still
    proves something is listening and counts as reachable. The connection is
    closed at once, before any D-Bus authentication, which the bus treats as an
    ordinary client hang-up.
    """
    paths = _user_bus_socket_paths()
    if not paths:
        return (False, "no XDG_RUNTIME_DIR (no systemd user session)")
    failures: list[str] = []
    for path in paths:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.setblocking(False)
                err = sock.connect_ex(path)
        except OSError as exc:
            err = exc.errno or errno.EIO
        if err in (0, errno.EAGAIN, errno.EINPROGRESS):
            return (True, "ok")
        failures.append(f"{path}: {os.strerror(err)}")
    return (False, f"user bus unreachable ({'; '.join(failures)}): {_CGROUP_LINGER_REMEDY}")


def _probe_cgroup_scope() -> tuple[bool, str]:
    """Return (available, reason) for unprivileged cgroup-v2 scope enforcement.

    Requires, on Linux: a pure cgroup-v2 mount, the ``pids`` and ``memory``
    controllers delegated to our user slice, a ``systemd-run`` binary, and a
    REACHABLE user bus. Any missing piece → not available.

    The answer follows the user's login rather than the process: it is
    recomputed whenever :func:`_cgroup_scope_session_fingerprint` changes and at
    most every :data:`_CGROUP_SCOPE_PROBE_TTL_SECONDS`. A gateway started inside
    an SSH session therefore stops prepending ``systemd-run`` once logind has
    stopped the user manager -- which it would otherwise keep doing, failing
    every spawn with ``Failed to connect to bus`` -- and takes the same no-scope
    path a gateway started after the logout takes; and it bounds spawns again
    when the manager comes back. Either flip is logged (see
    :func:`_log_cgroup_scope_transition`), so the ceiling is never lost quietly.
    """
    global _CGROUP_SCOPE_PROBE, _CGROUP_SCOPE_PROBE_SESSION, _CGROUP_SCOPE_PROBE_AT
    global _CPU_DELEGATED
    session = _cgroup_scope_session_fingerprint()
    now = time.monotonic()
    cached = _CGROUP_SCOPE_PROBE
    if (
        cached is not None
        and session == _CGROUP_SCOPE_PROBE_SESSION
        and now - _CGROUP_SCOPE_PROBE_AT < _CGROUP_SCOPE_PROBE_TTL_SECONDS
    ):
        return cached
    with _CGROUP_SCOPE_PROBE_LOCK:
        cached = _CGROUP_SCOPE_PROBE
        if (
            cached is not None
            and session == _CGROUP_SCOPE_PROBE_SESSION
            and now - _CGROUP_SCOPE_PROBE_AT < _CGROUP_SCOPE_PROBE_TTL_SECONDS
        ):
            return cached
        result = _compute_cgroup_scope_probe()
        _CGROUP_SCOPE_PROBE = result
        _CGROUP_SCOPE_PROBE_SESSION = session
        _CGROUP_SCOPE_PROBE_AT = now
        # The cpu controller lives in the same user slice; re-read it with the rest.
        _CPU_DELEGATED = None
    if cached is not None and cached[0] != result[0]:
        _log_cgroup_scope_transition(result)
    return result


def _log_cgroup_scope_transition(result: tuple[bool, str]) -> None:
    """Report a mid-run flip of scope availability, once per flip.

    Losing the ceiling is a SECURITY warning even when the one-time startup
    warning already fired for an earlier outage: the operator must be able to
    see from the log WHEN new spawns stopped being bounded. It also marks the
    one-time warning as spent, so ``cgroup_scope_argv`` does not repeat it.
    """
    global _CGROUP_WARNED
    available, reason = result
    if available:
        logger.info(
            "cgroup v2 scope enforcement is available again; new agent subprocesses "
            "are bounded by the fork-bomb / memory-DoS ceilings once more."
        )
        return
    _CGROUP_WARNED = True
    logger.warning(
        "SECURITY: cgroup v2 scope enforcement became unavailable while the gateway "
        "was running (%s). New agent subprocesses start WITHOUT the fork-bomb / "
        "memory-DoS ceilings (RLIMIT_NOFILE still applies) instead of failing with "
        "'Failed to connect to bus'. See docs/architecture/resource-protection.md.",
        reason,
    )


def _compute_cgroup_scope_probe() -> tuple[bool, str]:
    """Uncached capability check backing :func:`_probe_cgroup_scope`."""
    if sys.platform != "linux":
        return (False, "not Linux")
    # The same fixed-directory lookup ``cgroup_scope_argv`` pins its wrapper
    # with, minus its PATH-walking miss diagnostic: this runs on the event loop
    # each time the probe refreshes, so it must stay a handful of stats and
    # never touch a PATH entry that may sit on a stalled mount. The miss is
    # reported through the reason below instead.
    if platform_compat.trusted_system_bin_quiet("systemd-run") is None:
        return (False, "systemd-run not found in a trusted system directory")
    # A user session bus is required for `systemd-run --user`. The variable
    # alone proves nothing: a login shell sets it from $UID by formula, so it
    # stays set after logind removed the directory it names.
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return (False, "no XDG_RUNTIME_DIR (no systemd user session)")
    bus_ok, bus_reason = _user_bus_reachable()
    if not bus_ok:
        return (False, bus_reason)
    # Pure cgroup v2 unified hierarchy.
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            # v2 is a single line beginning "0::".
            if not any(line.startswith("0::") for line in fh):
                return (False, "not a cgroup v2 unified hierarchy")
    except OSError as exc:
        return (False, f"cannot read /proc/self/cgroup: {exc}")
    # The pids + memory controllers must be delegated to our user slice, else
    # systemd-run --scope can set the knobs but the kernel won't enforce them.
    try:
        with open(_user_slice_controllers_path(), encoding="utf-8") as fh:
            controllers = set(fh.read().split())
        missing = {"pids", "memory"} - controllers
        if missing:
            return (False, f"controllers not delegated: {sorted(missing)}")
    except OSError as exc:
        return (False, f"cannot read delegated controllers: {exc}")
    return (True, "ok")


_CPU_DELEGATED: bool | None = None


def _cpu_controller_delegated() -> bool:
    """Return True when the ``cpu`` controller is delegated to our user slice.

    CPUWeight / CPUQuota on a ``systemd-run --user`` scope are only enforced
    when the cpu controller is delegated; emitting them without delegation is
    a silent no-op at best and a warning at worst, so callers gate the CPU
    properties on this check. Cached alongside the main probe, and dropped
    whenever :func:`_probe_cgroup_scope` recomputes, because the user slice it
    reads belongs to the login session. Failure to read → False (skip CPU
    properties, keep pids/memory enforcement).
    """
    global _CPU_DELEGATED
    if _CPU_DELEGATED is None:
        try:
            with open(_user_slice_controllers_path(), encoding="utf-8") as fh:
                _CPU_DELEGATED = "cpu" in fh.read().split()
        except OSError:
            _CPU_DELEGATED = False
    return _CPU_DELEGATED


def _cgroup_limits_from_config() -> tuple[int, int, int, int]:
    """Return ``(max_processes, max_memory_mb, cpu_weight, max_cpu_percent)``
    for the cgroup scope.

    Reads the same ``resource_limits`` config block as apply_resource_limits;
    falls back to the module defaults. ``0`` (or junk) means "use default" for
    the cgroup ceiling — unlike the RLIMIT path, we never leave the cgroup DoS
    ceiling unset by default (that is the whole point of this control). The
    memory default is host-proportional (see :func:`_default_max_memory_mb`).

    ``max_cpu_percent`` is the OPT-IN hard CPU quota (``CPUQuota``): ``0``
    (the default) means "no quota property emitted at all" — hard CPU caps
    slow legitimate builds, so unlike the other ceilings this one is off
    unless an operator explicitly sets ``resource_limits.max_cpu_percent``.
    """
    max_procs = _CGROUP_DEFAULT_MAX_PROCESSES
    max_mem_mb = _default_max_memory_mb()
    cpu_weight = _CGROUP_DEFAULT_CPU_WEIGHT
    max_cpu_percent = 0  # opt-in: 0 = emit no CPUQuota
    try:
        # circular import: sandbox is a low-level module imported by
        # config/security consumers — importing kiro_crew.config.loader at
        # module load would create an import cycle, so it stays function-level
        # (same pattern as resource_limit_preexec below).
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        # One validated read for the whole block. ResourceLimitsConfig.from_raw
        # is the only place these keys are coerced, and it is what refuses a
        # fraction, a NaN/Infinity from json.loads, and a non-number before
        # ``int()`` can raise on them and abort the remaining fields.
        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
        # ``>= 1``, so 0 lands on the default with everything else out of domain:
        # TasksMax=0 / MemoryMax=0M are rejected by systemd and the scope would
        # never start, so this ceiling is never left unset. The SAME two keys
        # mean "leave inherited" when 0 reaches the rlimit path in
        # security.apply_resource_limits — ResourceLimitsConfig carries both
        # domains so neither side can be tightened without seeing the other.
        if rl.max_processes is not None and rl.max_processes >= 1:
            max_procs = rl.max_processes
        if rl.max_memory_mb is not None and rl.max_memory_mb >= 1:
            max_mem_mb = rl.max_memory_mb
        # Range-checked at the parse site (1..10000), so any value that arrives
        # here is usable as-is.
        if rl.cpu_weight is not None:
            cpu_weight = rl.cpu_weight
        # Opt-in: 0 keeps the "emit no CPUQuota" default rather than capping.
        if rl.max_cpu_percent is not None and rl.max_cpu_percent > 0:
            max_cpu_percent = rl.max_cpu_percent
    except Exception:
        logger.debug("cgroup limits: config unavailable, using defaults")
    return max_procs, max_mem_mb, cpu_weight, max_cpu_percent
