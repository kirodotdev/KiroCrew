"""Bound the agents slice as a whole, and build a spawn's scope prefix.

The aggregate memory and task ceilings, their memory-pressure and throttling probes, the
Windows resource ceiling, and :func:`cgroup_scope_argv`. One of the owners
``kiro_crew.sandbox`` is carved into: every name here stays readable and patchable as
``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.sandbox_cgroup import (
    _CGROUP_AGENTS_SLICE,
    _agents_slice_name,
    _cgroup_limits_from_config,
    _cgroup_scope_epoch,
    _cpu_controller_delegated,
    _probe_cgroup_scope,
    _read_resource_limits,
    _scope_memory_high_mb,
    _warn_cgroup_unavailable,
)

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


# ── aggregate agent-slice soft ceiling (memory.high on kirocrew-agents.slice) ──
# Per-scope MemoryMax bounds ONE runaway spawn tree, but scopes are created per
# spawn: several concurrent agent trees, each legitimately under its own 65%
# cap, can still sum past physical RAM and livelock a swapless host. memory.high
# on the slice all agent scopes share throttles-and-reclaims the whole subtree
# once the SUM of agent memory crosses it, keeping the kernel and the gateway
# (which lives outside the slice) responsive — a soft layer BELOW the slice's
# hard aggregate memory.max (ensure_agents_slice_limits), which OOM-kills a
# scope only when throttling was not enough, while each scope's own memory.max
# still hard-kills an individual runaway. Same trust model as the scope
# ceilings: unprivileged, enforced by the user manager, requires the memory
# controller delegated to the user slice (the existing _probe_cgroup_scope
# check).

# Fraction of physical RAM used for the default slice memory.high. Higher than
# the per-scope 65% because it bounds the SUM of all agent scopes, and below
# the slice's hard 80% memory.max so throttling engages before the kernel
# OOM-kills, with OS + gateway headroom preserved even while the whole fleet
# is being throttled.
_SLICE_MEMORY_HIGH_FRACTION = 0.75
# Fallback slice memory.high (MB) used only when physical RAM can't be read.
# The slice path is Linux-only, where SC_PHYS_PAGES exists, so this is a
# belt-and-suspenders default, not the normal path.
_SLICE_FALLBACK_MEMORY_HIGH_MB = 12288

# Last MemoryHigh value applied to the slice by THIS process ("24576M" /
# "infinity"), or None before the first reconcile. The desired value is
# host-derived and constant for the process's life (no config input), so
# after the first successful apply every later reconcile in the same login
# session reduces to a string compare and no-ops.
_SLICE_MEMHIGH_APPLIED: str | None = None
# Kill switch: set after a failed apply so a broken systemctl is warned about
# ONCE per login session and never hammered on every subsequent spawn.
_SLICE_MEMHIGH_DISABLED = False
# The enforcement epoch (``_cgroup_scope_epoch``) the two values above were
# recorded in, or None when nothing was recorded by a reconcile. The applied
# value is a ``--runtime`` property that vanishes with the login session, and a
# failure seen while the user manager was going away says nothing about the
# next one, so state from an older epoch is discarded before the next
# reconcile. A kill switch set from outside a reconcile carries no stamp and
# stays set.
_SLICE_MEMHIGH_EPOCH: int | None = None


def _slice_memory_high_is_stale() -> bool:
    """Whether the recorded MemoryHigh state belongs to an earlier login session."""
    stamp = _SLICE_MEMHIGH_EPOCH
    return stamp is not None and stamp != _cgroup_scope_epoch()


def _forget_stale_slice_memory_high() -> None:
    """Drop MemoryHigh state recorded under an earlier epoch, re-arming the reconcile."""
    global _SLICE_MEMHIGH_APPLIED, _SLICE_MEMHIGH_DISABLED, _SLICE_MEMHIGH_EPOCH
    if not _slice_memory_high_is_stale():
        return
    _SLICE_MEMHIGH_APPLIED = None
    _SLICE_MEMHIGH_DISABLED = False
    _SLICE_MEMHIGH_EPOCH = None


def _default_slice_memory_high_mb() -> int:
    """Return the default slice ``memory.high`` in MB: a fixed fraction
    (:data:`_SLICE_MEMORY_HIGH_FRACTION`) of physical RAM, falling back to
    :data:`_SLICE_FALLBACK_MEMORY_HIGH_MB` if host RAM can't be determined.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _SLICE_MEMORY_HIGH_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    return _SLICE_FALLBACK_MEMORY_HIGH_MB


def _ensure_agent_slice_memory_high() -> None:
    """Reconcile ``MemoryHigh`` on :data:`_CGROUP_AGENTS_SLICE` with the host default.

    The slice is UID-GLOBAL: every gateway instance under this user (live,
    dev-backend, pods with delegation) parents scopes into the same slice, so
    the ceiling is deliberately NOT config-driven — a per-instance config key
    would let one instance (e.g. a dev gateway configured permissively) lift
    or lower the ceiling that protects the others. The value is always the
    host-derived default (:func:`_default_slice_memory_high_mb`).

    Runs ``systemctl --user set-property --runtime`` — unprivileged: the user
    manager owns the slice, and the memory controller is delegated wherever the
    caller's probe passed. ``--runtime`` is deliberate: the drop-in lives under
    ``$XDG_RUNTIME_DIR`` and vanishes with the login session, so no persistent
    unit files accumulate under ``~/.config`` and a stale ceiling can never
    outlive the login session that set it.

    Never raises: agent spawns must not fail because the ceiling could not be
    applied. On failure it logs one loud warning and disarms until the user
    manager's next login session (see :data:`_SLICE_MEMHIGH_EPOCH`).
    """
    global _SLICE_MEMHIGH_APPLIED, _SLICE_MEMHIGH_DISABLED, _SLICE_MEMHIGH_EPOCH
    _forget_stale_slice_memory_high()
    if _SLICE_MEMHIGH_DISABLED:
        return
    epoch = _cgroup_scope_epoch()
    desired = f"{_default_slice_memory_high_mb()}M"
    if desired == _SLICE_MEMHIGH_APPLIED:
        return
    try:
        systemctl = platform_compat.trusted_system_bin("systemctl")
        if systemctl is None:
            raise FileNotFoundError("systemctl not found in trusted system dirs")
        proc = subprocess.run(
            [
                systemctl,
                "--user",
                "set-property",
                "--runtime",
                _CGROUP_AGENTS_SLICE,
                f"MemoryHigh={desired}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "non-zero exit")
    except Exception as exc:
        _SLICE_MEMHIGH_DISABLED = True
        _SLICE_MEMHIGH_EPOCH = epoch
        logger.warning(
            "SECURITY: could not apply MemoryHigh=%s to %s (%s); the AGGREGATE "
            "agent memory ceiling is NOT enforced on this host — per-scope "
            "MemoryMax still applies. See "
            "docs/architecture/resource-protection.md.",
            desired,
            _CGROUP_AGENTS_SLICE,
            exc,
        )
        return
    _SLICE_MEMHIGH_APPLIED = desired
    _SLICE_MEMHIGH_EPOCH = epoch
    logger.info("agent slice %s: MemoryHigh=%s applied", _CGROUP_AGENTS_SLICE, desired)


# Last observed value of the slice's memory.events `high` counter, or None
# before the first successful read. The first read only baselines — the
# counter is monotonic for the slice cgroup's lifetime, so a nonzero value
# may predate this process — and climbs are judged against it.
_SLICE_MEMHIGH_EVENTS_SEEN: int | None = None
# True while inside a climbing episode that has already been warned about, so
# sustained throttling logs once per episode instead of on every spawn. Reset
# when an observation finds the counter stable (episode over) or lower (slice
# cgroup recreated).
_SLICE_MEMHIGH_CLIMB_WARNED = False


def _slice_own_high_events(slice_dir: Path) -> int | None:
    """The slice's OWN ``high`` event count, excluding its descendants.

    ``memory.events`` is hierarchical: it also counts ``high`` events of every
    cgroup below the slice. Each agent scope carries its own finite
    ``MemoryHigh`` (:func:`cgroup_scope_argv`), so one runtime over ITS ceiling
    would read as the whole slice being throttled. ``memory.events.local``
    counts only breaches of the slice's own ``memory.high``. When that file is
    missing, empty or unreadable (a kernel older than 5.2) this returns ``None``:
    no counter signal, so a scope's own events never read as slice throttling,
    and ``memory.current >= memory.high`` is the only throttling signal there.
    """
    return _read_cgroup_counters(slice_dir / "memory.events.local").get("high")


def _slice_memory_events_high() -> int | None:
    """Return the slice cgroup's own ``high`` counter (:func:`_slice_own_high_events`).

    The slice directory comes from :func:`_agents_slice_cgroup_dir`, which
    understands systemd's dash-hierarchy (``kirocrew-agents.slice`` nests
    under ``kirocrew.slice`` in the user manager's subtree). ``None`` when it
    cannot be read: not Linux, no cgroup v2, the slice cgroup not currently
    materialized (systemd releases an empty slice), or unparseable content.
    """
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return None
    return _slice_own_high_events(slice_dir)


def _check_slice_memory_pressure() -> None:
    """Warn when the slice's own ``high`` event counter climbs.

    Past ``memory.high`` the kernel throttles-and-reclaims the subtree
    SILENTLY — agents just slow down; nothing kills and nothing alerts, since
    per-scope ``MemoryMax`` never fired. The ``high`` counter climbing is the
    kernel's only signal that the aggregate ceiling is throttling, and
    surfacing it makes "agents mysteriously slow" diagnosable from the
    gateway log as ceiling throttling rather than a hang.

    Warned once per climbing episode: the first observed increase logs, later
    increases stay silent until an observation finds the counter stable,
    which closes the episode. A DECREASE means the slice cgroup was recreated
    (an empty slice is released and its counters reset) — re-baseline
    silently, never warn.
    """
    global _SLICE_MEMHIGH_EVENTS_SEEN, _SLICE_MEMHIGH_CLIMB_WARNED
    current = _slice_memory_events_high()
    if current is None:
        return
    previous = _SLICE_MEMHIGH_EVENTS_SEEN
    _SLICE_MEMHIGH_EVENTS_SEEN = current
    if previous is None or current <= previous:
        # First read, counter stable, or slice cgroup recreated: (re)baseline
        # and close any open episode.
        _SLICE_MEMHIGH_CLIMB_WARNED = False
        return
    if _SLICE_MEMHIGH_CLIMB_WARNED:
        return
    _SLICE_MEMHIGH_CLIMB_WARNED = True
    logger.warning(
        "agent slice %s: memory.events.local high counter climbed %d -> %d — "
        "aggregate agent memory crossed the slice MemoryHigh ceiling and the "
        "kernel is throttling the whole agent subtree; agents run slowly "
        "(not hung) until aggregate memory drops. See "
        "docs/architecture/resource-protection.md.",
        _CGROUP_AGENTS_SLICE,
        previous,
        current,
    )


# Serializes reconciliation workers. Deliberately a plain blocking mutex held
# for the whole reconcile body: every schedule spawns its own short-lived
# worker and workers queue on the mutex, so concurrent spawns never interleave
# systemctl calls and a failed apply is retried by the next spawn's worker.
# The desired value is host-derived and process-constant (no config input) —
# the mutex guards the apply/retry handoff, not value freshness. Redundant
# workers are near-free (the applied-value check reduces them to a string
# compare), and thread count is bounded by concurrent agent spawns.
_SLICE_MEMHIGH_MUTEX = threading.Lock()


def _reconcile_slice_memory_high_off_thread() -> None:
    """Reconcile ``MemoryHigh`` and check slice throttling in a daemon thread.

    The reconciliation reads config and shells out to ``systemctl`` (up to
    10s), and its caller sits on the agent-spawn path, which runs on the
    gateway event loop — so it must never execute inline. Fire-and-forget is
    semantically safe: ``MemoryHigh`` set on a slice applies to members that
    are already running, so a reconciliation that lands moments after the
    spawn still bounds it, and every later spawn re-reconciles. The worker
    also runs :func:`_check_slice_memory_pressure`, so throttle visibility
    shares the reconcile cadence (per spawn) and its kill switch.

    The worker first applies the slice's hard aggregate ceiling
    (:func:`_reapply_slice_limits_for_new_session`) when the current login
    session never got it, as for a gateway that started while the user was
    logged out: no other caller would apply it there.
    """
    global _SLICE_MEMHIGH_DISABLED, _SLICE_MEMHIGH_EPOCH
    if _SLICE_MEMHIGH_DISABLED and not _slice_memory_high_is_stale():
        return

    def _worker() -> None:
        with _SLICE_MEMHIGH_MUTEX:
            _reapply_slice_limits_for_new_session()
            _ensure_agent_slice_memory_high()
            _check_slice_memory_pressure()

    try:
        threading.Thread(target=_worker, name="agent-slice-memhigh", daemon=True).start()
    except RuntimeError as exc:
        # Thread exhaustion. This sits on the agent-spawn path, so it must
        # never abort the spawn. Disarm like any other reconciliation
        # failure: per-scope MemoryMax still applies.
        _SLICE_MEMHIGH_DISABLED = True
        _SLICE_MEMHIGH_EPOCH = _cgroup_scope_epoch()
        logger.warning(
            "SECURITY: could not start the MemoryHigh reconciliation thread "
            "(%s); the AGGREGATE agent memory ceiling is NOT enforced on this "
            "host — per-scope MemoryMax still applies.",
            exc,
        )


# ``resource_limits`` keys whose only enforcement is a cgroup/systemd property
# or a POSIX rlimit. The Windows Job object carries per-spawn processes and
# memory only, so a value set for any of these is accepted but not enforced.
_WINDOWS_INERT_RESOURCE_LIMIT_KEYS = (
    "cpu_weight",
    "max_cpu_percent",
    "max_cpu_seconds",
    "max_open_files",
    "max_total_memory_mb",
    "max_total_processes",
    "scope_memory_high_pct",
    "slice_reclaim_trigger_pct",
)
_WINDOWS_INERT_LIMITS_WARNED = False


def _warn_windows_inert_resource_limits_once() -> None:
    """Log ONE warning naming each set ``resource_limits`` key Windows ignores.

    A key counts as set when it is non-null and non-zero. Nothing is logged
    when no such key is configured, and later calls are no-ops either way.
    """
    global _WINDOWS_INERT_LIMITS_WARNED
    if _WINDOWS_INERT_LIMITS_WARNED:
        return
    _WINDOWS_INERT_LIMITS_WARNED = True
    try:
        # circular import: same function-level pattern as _cgroup_limits_from_config.
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
    except Exception:
        logger.debug("inert resource_limits check: config unavailable")
        return
    inert = [key for key in _WINDOWS_INERT_RESOURCE_LIMIT_KEYS if getattr(rl, key, None)]
    if inert:
        logger.warning(
            "resource_limits.%s %s set but not enforced on Windows: the Job "
            "object bounds only per-spawn max_processes / max_memory_mb, and "
            "these keys need a systemd user manager or POSIX rlimits.",
            ", resource_limits.".join(inert),
            "is" if len(inert) == 1 else "are",
        )


def apply_windows_resource_ceiling(pid: int) -> bool:
    """Windows counterpart to :func:`cgroup_scope_argv`, applied AFTER the spawn.

    ``cgroup_scope_argv`` bounds an agent subprocess and all its descendants by
    prepending ``systemd-run --user --scope`` with ``TasksMax`` / ``MemoryMax``.
    That has no Windows equivalent expressible as an argv prefix, so there it
    returns argv unchanged and logs a one-time loud SECURITY warning — the
    fork-bomb and memory-DoS ceilings were simply absent on that platform.

    A Job object is the native mechanism (limits cover every process in the job,
    and a member's descendants join automatically), but it must be applied to a
    live pid rather than baked into argv. Callers therefore invoke this right
    after the spawn returns, in ADDITION to the ``cgroup_scope_argv`` call they
    already make (a no-op on Windows), and while the child is still suspended —
    see :func:`platform_compat.apply_job_limits` for why that ordering is what
    makes the ceiling airtight.

    Reads the SAME ``resource_limits`` config as the cgroup path, so one operator
    setting governs both platforms.

    Returns ``True`` when a ceiling was installed; ``False`` on non-Windows
    (nothing to do — the cgroup wrapper owns it) or on any failure, which
    :func:`platform_compat.apply_job_limits` has already logged as a SECURITY
    warning. Never raises: a missing ceiling must not fail the spawn, matching
    how an unavailable cgroup scope is handled.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    _warn_windows_inert_resource_limits_once()
    max_procs, max_mem_mb, _cpu_weight, _max_cpu_percent = _cgroup_limits_from_config()
    return platform_compat.apply_job_limits(
        pid,
        max_procs=max_procs,
        max_memory_bytes=max_mem_mb * 1024 * 1024,
    )


# Prefix of the scope unit name a caller gets from ``scope_unit_name``. Carries
# the owning gateway's product name so a human reading ``systemctl --user
# list-units`` can tell an agent scope from anything else in the slice, and is
# the token the reverse lookup matches on.
_SCOPE_UNIT_PREFIX = "kirocrew-rt-"
# systemd unit names accept alphanumerics and ``:-_.\`` plus escapes; anything
# else has to be escaped to be a legal name. Rather than escape, a token
# carrying something else is REFUSED (see ``scope_unit_name``), because every
# caller mints its own token and a token needing escapes is a caller bug.
_SCOPE_UNIT_TOKEN_SAFE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def scope_unit_name(token: str) -> str | None:
    """The scope unit name for a spawn identified by *token*, or None.

    ``cgroup_scope_argv`` otherwise lets systemd auto-name the scope
    ``run-u<N>.scope``, whose only content is "the Nth transient unit this user
    manager made". One scope holds one spawn, and one spawn is one agent
    RUNTIME, which may serve several sessions -- so an anonymous scope name is
    the reason a reader holding a scope (the slice OOM report at
    :func:`check_agents_slice_pressure`, ``systemd-cgls``, an operator reading
    ``systemctl --user list-units``) can name the victim's directory but not the
    runtime it held, nor the sessions leasing that runtime. Naming the scope
    after the runtime's own spawn token closes that: the SAME token travels in
    the child's environment as ``KIROCREW_SPAWN_INSTANCE``, so a scope name and
    a live process both resolve to one incarnation. For the case that matters
    most -- a scope the kernel already killed, where both the environment and the
    in-memory ``_process_instance`` are gone -- ``AcpRuntime`` logs this unit name
    beside its pid at initialization, and sessions are logged against that same
    pid as they are created, so the join survives the process.

    Returns ``None`` when *token* cannot form a legal unit name, and the caller
    then wraps the spawn exactly as before -- anonymously, but bounded. That
    direction is deliberate: the scope's JOB is the DoS ceiling, and a naming
    defect must never be able to cost a spawn its ceiling or fail the spawn
    outright. ``token`` is expected to be an opaque random identifier, so it is
    validated rather than escaped; a value needing escapes is a caller bug and
    is refused rather than mangled into a name a reverse lookup would miss.
    """
    if not _SCOPE_UNIT_TOKEN_SAFE.match(token or ""):
        return None
    return f"{_SCOPE_UNIT_PREFIX}{token}.scope"


def cgroup_scope_argv(argv: list[str]) -> list[str]:
    """Wrap *argv* in a transient systemd --user --scope with cgroup v2 limits.

    Prepends ``systemd-run --user --scope`` with ``TasksMax`` (pids.max, the
    fork-bomb ceiling), ``MemoryMax`` + ``MemorySwapMax=0`` (memory.max, the
    RSS balloon ceiling), ``MemoryHigh`` (memory.high, a soft ceiling at
    ``resource_limits.scope_memory_high_pct`` of MemoryMax, default 90%, so a
    runtime that fills page cache reclaims and is throttled on its own before
    the shared slice ceiling slows every runtime; see
    :func:`_scope_memory_high_mb`), and — when the cpu controller is delegated —
    ``CPUWeight`` (proportional fair-share: agents run full speed on an idle
    host but yield to interactive work under contention; never a hard
    throttle) plus an OPT-IN ``CPUQuota`` hard cap
    (``resource_limits.max_cpu_percent``, off by default because hard quotas
    slow legitimate builds), so the spawned agent AND all its MCP-server/tool
    descendants are bounded as one cgroup and the kernel kills the scope on
    breach. ``--scope`` execs into the target (it does NOT fork a wrapper), so
    the returned argv's eventual PID is the real child — parent PID tracking,
    ``killpg``, and descendant scans are unaffected.

    Every scope is parented under a per-instance child of
    :data:`_CGROUP_AGENTS_SLICE` (see :func:`_agents_slice_name`, which explains
    why the owner has to be expressible), and the slice-level soft ceiling
    (``MemoryHigh``, see :func:`_ensure_agent_slice_memory_high`) — a
    host-derived constant (75% of RAM) — is ensured before each wrap on the
    PARENT, throttling the SUM of all concurrent agent trees before the slice's
    hard ``MemoryMax`` (:func:`ensure_agents_slice_limits`) OOM-kills a scope.
    cgroup v2 bounds a descendant by the minimum effective limit along its
    ancestor chain, so nesting one level deeper does not loosen either ceiling.

    Layers OUTSIDE the OS-level sandbox: callers pass the already-``wrap_argv``-ed
    argv here so the child is filesystem-isolated AND cgroup-bounded.

    The ceiling is per SPAWN, and therefore per agent RUNTIME rather than per
    SESSION: one process can serve several sessions (a parent and the subagents
    whose sessions are created on its runtime), and cgroup v2 kills a breaching
    scope as ONE unit, so every session on that runtime goes together. The
    per-scope values come from :func:`_cgroup_limits_from_config`, which is where
    a ceiling that scales with the number of sessions a runtime serves would
    attach -- it is the single place both the value and its config source are
    resolved, so a scaling factor applied there reaches every caller without any
    spawn site being taught about sessions.

    The scope itself is left ANONYMOUS here -- systemd auto-names it
    ``run-u<N>.scope``. A caller that can name the runtime it is spawning passes
    the result through :func:`name_scope_unit`, which is a separate step so that
    this function's argv stays byte-identical for the many callers wrapping a
    one-off tool or app subprocess that no reader needs to resolve back to a
    runtime.

    On a host without cgroup v2 delegation (older Linux, no systemd user
    session, macOS), returns *argv* unchanged and logs a one-time loud SECURITY
    warning — the RLIMIT_NOFILE preexec still applies, but the fork-bomb/memory
    DoS ceiling is NOT enforced there. The same degradation applies when
    ``systemd-run`` resolves outside the trusted system directories: prepending
    an unpinned wrapper name would trade a DoS ceiling for an exec-hijack
    channel, which is the worse of the two.

    The returned wrapper is an ABSOLUTE path, so callers may hand the result to
    a spawn whose ``env`` carries a config-declared PATH without that PATH being
    able to redirect argv[0].
    """
    available, reason = _probe_cgroup_scope()
    if not available:
        _warn_cgroup_unavailable(reason)
        return argv
    # SECURITY: the wrapper this function prepends becomes argv[0], and a caller
    # that hands the result to a spawn with a config-influenced ``env`` has
    # CPython resolve a slash-less argv[0] through THAT env's PATH
    # (os.get_exec_path) -- so a bare name here is an exec-hijack channel that
    # runs BEFORE ``--scope`` establishes confinement. Pin it at the layer that
    # prepends it, so every caller inherits the protection rather than each
    # spawn site remembering to re-pin (the same reason ``sandbox_exec_argv``
    # pins its own wrappers). ``trusted_system_bin`` ignores PATH entirely: a
    # gateway's PATH can legitimately lead with agent-writable directories, so
    # resolving through it would leave the hole half-open. An unresolvable
    # wrapper degrades exactly like a missing cgroup backend -- no ceiling, loud
    # warning -- rather than emitting an unpinned name.
    systemd_run = platform_compat.trusted_system_bin("systemd-run")
    if not systemd_run:
        _warn_cgroup_unavailable("systemd-run is not in a trusted system directory")
        return argv
    # Reconcile the slice-level aggregate ceiling off-thread: this call site
    # runs on the gateway event loop during agent spawn, and reconciliation
    # does config reads + a systemctl subprocess. MemoryHigh on a slice
    # applies to already-running members, so the spawn need not wait for it.
    _reconcile_slice_memory_high_off_thread()
    # One config read per scope start, shared by every limit below.
    limits_config = _read_resource_limits()
    max_procs, max_mem_mb, cpu_weight, max_cpu_percent = _cgroup_limits_from_config(limits_config)
    # Per-scope soft ceiling: without it the scope sits at
    # memory.high=max, so one runtime's page-cache growth is reclaimed only at
    # the SHARED slice MemoryHigh, which throttles every runtime together.
    memory_high_mb = _scope_memory_high_mb(max_mem_mb, limits_config)
    memory_high = [] if memory_high_mb is None else ["-p", f"MemoryHigh={memory_high_mb}M"]
    # Under Shared Crew the scope joins its principal's slice, a child of the
    # instance slice, so every ceiling above it keeps applying. The lookup is in
    # memory; any failure takes the instance slice rather than costing the
    # spawn its ceiling.
    try:
        from kiro_crew.shared_crew.containment import scope_parent_slice

        parent_slice = scope_parent_slice() or _agents_slice_name()
    except Exception:
        parent_slice = _agents_slice_name()
    props = [
        "-p",
        f"TasksMax={max_procs}",
        "-p",
        f"MemoryMax={max_mem_mb}M",
        *memory_high,
        "-p",
        "MemorySwapMax=0",
    ]
    # CPU properties only when the cpu controller is delegated — otherwise the
    # kernel won't enforce them and systemd may warn on every spawn.
    if _cpu_controller_delegated():
        props += ["-p", f"CPUWeight={cpu_weight}"]
        if max_cpu_percent > 0:
            props += ["-p", f"CPUQuota={max_cpu_percent}%"]
    return [
        systemd_run,
        "--user",
        "--scope",
        "-q",
        f"--slice={parent_slice}",
        *props,
        "--",
        *argv,
    ]


def name_scope_unit(argv: list[str], token: str) -> list[str]:
    """Name the scope in a :func:`cgroup_scope_argv` result after *token*.

    Returns *argv* UNCHANGED unless it really is a systemd-run scope wrapper AND
    *token* forms a legal unit name (:func:`scope_unit_name`). Both degradations
    are one decision: the scope's job is the DoS ceiling, the name is a
    diagnostic, and a diagnostic must never cost a spawn its ceiling nor fail the
    spawn outright. A host without cgroup delegation (where ``cgroup_scope_argv``
    hands back the bare command) and a token that cannot be spelled as a unit
    therefore both leave the spawn exactly as it would otherwise have been.

    A separate step rather than a keyword on ``cgroup_scope_argv`` because that
    function has dozens of callers and is widely replaced by one-argument stubs in
    tests: a keyword there would make every one of those stubs refuse the call,
    for callers that have no runtime to name anyway. The recognition check here
    makes a stubbed wrap a silent no-op instead.

    ``--unit`` goes BEFORE the ``--`` separator. After it, systemd-run reads it as
    an argument to the wrapped command instead of a property of the scope.
    """
    unit = scope_unit_name(token)
    if unit is None or "--scope" not in argv or "--" not in argv:
        return argv
    at = argv.index("--")
    return [*argv[:at], "--unit", unit, *argv[at:]]


# ── aggregate ceiling on the parent slice ──
# The per-scope MemoryMax above bounds ONE spawn tree, but scopes are siblings:
# N concurrent spawns may collectively request N x 65% of host RAM with no
# single cgroup ever breaching its own limit. cgroup v2 enforces limits down
# the tree — a descendant is bounded by the MINIMUM effective limit of itself
# and all ancestors — so the parent slice every scope already nests under is
# the natural aggregate boundary. ensure_agents_slice_limits() puts a ceiling
# on it, yielding a two-level model: slice = aggregate across all concurrent
# agent work, scope = per-tree (unchanged).

# Aggregate memory.max as a fraction of physical RAM. Must sit ABOVE the
# per-scope fraction (0.65) — otherwise the slice would shrink a single
# spawn's existing headroom — and meaningfully below 1.0 so the OS and the
# gateway keep breathing room even when agent work saturates the ceiling.
_CGROUP_TOTAL_MEMORY_FRACTION = 0.80
# Fallback aggregate memory.max (MB) when physical RAM can't be read. Above
# the per-scope fallback (8192) for the same "never clamp a single scope
# tighter than its own ceiling" reason as the fraction.
_CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB = 12288
# Fallback aggregate pids.max across all concurrent scopes, used only when the
# host reading fails. The normal default is host-sized
# (:func:`_default_max_total_tasks`), and this value is also its floor.
_CGROUP_DEFAULT_MAX_TOTAL_TASKS = 32768


def _default_max_total_tasks(total_memory_mb: int | None = None) -> int:
    """Default aggregate ``pids.max`` for the agents slice, sized from the host.

    *total_memory_mb* is the slice ``MemoryMax`` being applied alongside it, so
    the task ceiling follows a configured memory ceiling. See
    :func:`kiro_crew.host_task_sizing.size_slice_tasks` for the formula.
    Falls back to :data:`_CGROUP_DEFAULT_MAX_TOTAL_TASKS` when the host cannot
    be read.
    """
    try:
        from kiro_crew.host_task_sizing import host_task_facts, size_slice_tasks

        return size_slice_tasks(host_task_facts(), total_memory_mb)
    except Exception:
        logger.debug("slice tasks: host reading failed, using the fixed default")
        return _CGROUP_DEFAULT_MAX_TOTAL_TASKS


def _default_max_total_memory_mb() -> int:
    """Default aggregate ``memory.max`` (MB) for the agents slice: a fixed
    fraction (:data:`_CGROUP_TOTAL_MEMORY_FRACTION`) of physical RAM, falling
    back to :data:`_CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB` when RAM is unreadable.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _CGROUP_TOTAL_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    return _CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB


def _slice_limits_from_config() -> tuple[int, int]:
    """Return ``(max_total_memory_mb, max_total_tasks)`` for the agents slice.

    Reads ``resource_limits.max_total_memory_mb`` / ``max_total_processes``
    from the same config block as the per-scope knobs. ``0`` or junk means
    "use default", where the task default is host-sized
    (:func:`_default_max_total_tasks`) — the aggregate ceiling is never left unset, matching the
    per-scope rule in :func:`_cgroup_limits_from_config`. The two memory knobs
    are deliberately independent of one another: per-scope answers "how big may
    one tree get", aggregate answers "how much may all trees claim together".
    """
    total_mem_mb = _default_max_total_memory_mb()
    total_tasks: int | None = None
    try:
        # circular import: same constraint as _cgroup_limits_from_config —
        # config.loader consumers import sandbox, so the import stays local.
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        # Same single validated read as the per-scope knobs. This function used
        # to test ``int(m) >= 1`` directly, which raises on a NaN/Infinity that
        # json.loads happily produces — and the raise landed in the except below,
        # discarding a VALID max_total_processes set alongside a junk memory
        # value. from_raw refuses both before int() sees them, per key.
        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
        if rl.max_total_memory_mb is not None and rl.max_total_memory_mb >= 1:
            total_mem_mb = rl.max_total_memory_mb
        if rl.max_total_processes is not None and rl.max_total_processes >= 1:
            total_tasks = rl.max_total_processes
    except Exception:
        logger.debug("slice limits: config unavailable, using defaults")
    if total_tasks is None:
        # Sized against the memory ceiling actually applied, configured or not.
        total_tasks = _default_max_total_tasks(total_mem_mb)
    return total_mem_mb, total_tasks


_SLICE_LIMITS_APPLIED = False
# The enforcement epoch (``_cgroup_scope_epoch``) of the last apply attempt that
# passed the capability probe, successful or not; None before any. One attempt
# per epoch keeps a broken systemctl from being retried on every spawn.
_SLICE_LIMITS_EPOCH: int | None = None


def _reapply_slice_limits_for_new_session() -> None:
    """Apply the slice's hard aggregate ceiling when this login session never got it.

    Covers a gateway that started while the user was logged out: the startup
    apply found no user manager, and the pressure sampler's self-heal only
    re-applies a ceiling that was applied before. A ceiling that is applied
    is left to that self-heal, so the two never act on the same state.

    Blocking (may shell out): runs only on the reconcile worker thread.
    """
    if _SLICE_LIMITS_APPLIED or _SLICE_LIMITS_EPOCH == _cgroup_scope_epoch():
        return
    ensure_agents_slice_limits()


def ensure_agents_slice_limits() -> bool:
    """Apply the aggregate cgroup ceiling to the agents slice. Idempotent.

    Runs ``systemctl --user set-property --runtime`` on
    :data:`_CGROUP_AGENTS_SLICE`, setting ``MemoryMax`` (aggregate across ALL
    concurrent agent scopes), ``MemorySwapMax=0`` (consistent with the
    per-scope property: a true RSS ceiling, no swap escape), and ``TasksMax``
    (aggregate fork-bomb ceiling). Called at gateway startup, and from the
    spawn path's reconcile worker when it was not applied in the current
    login session (:func:`_reapply_slice_limits_for_new_session`). A ceiling
    that was applied and then vanished at a logout is re-applied by the
    pressure sampler's self-heal (:func:`check_agents_slice_pressure`).

    ``--runtime`` over a shipped unit drop-in, deliberately: the property is
    re-derived from config and re-applied on every gateway start, so a config
    change can never leave a stale on-disk artifact behind, and uninstalling
    leaves nothing to clean up. The property persists on the user manager
    until logout/reboot.

    Gated on the same :func:`_probe_cgroup_scope` capability check as the
    per-scope wrapper: where delegation is unavailable this is skipped and the
    single shared SECURITY warning covers both layers — no second warning for
    the same host condition.

    Blocking (shells out): call off-loop (``asyncio.to_thread``).

    Returns True when the ceiling is in place (now or from an earlier call).
    """
    global _SLICE_LIMITS_APPLIED, _SLICE_LIMITS_EPOCH
    if _SLICE_LIMITS_APPLIED:
        return True
    available, reason = _probe_cgroup_scope()
    if not available:
        _warn_cgroup_unavailable(reason)
        return False
    _SLICE_LIMITS_EPOCH = _cgroup_scope_epoch()
    total_mem_mb, total_tasks = _slice_limits_from_config()
    # PATH can legitimately lead with agent-writable directories (a worktree
    # venv's bin, ~/.local/bin), so a bare "systemctl" would let a planted
    # shim run with the gateway's environment. Resolve from fixed system
    # directories only; unavailable = ceiling not applied (per-scope ceilings
    # still hold).
    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s: no trusted "
            "systemctl binary — per-scope ceilings still apply.",
            _CGROUP_AGENTS_SLICE,
        )
        return False
    cmd = [
        systemctl,
        "--user",
        "set-property",
        "--runtime",
        _CGROUP_AGENTS_SLICE,
        f"MemoryMax={total_mem_mb}M",
        "MemorySwapMax=0",
        f"TasksMax={total_tasks}",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s: %s — "
            "per-scope ceilings still apply, but N concurrent spawns may "
            "collectively exceed host RAM.",
            _CGROUP_AGENTS_SLICE,
            exc,
        )
        return False
    if proc.returncode != 0:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s (rc=%d): %s — "
            "per-scope ceilings still apply, but N concurrent spawns may "
            "collectively exceed host RAM.",
            _CGROUP_AGENTS_SLICE,
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return False
    _SLICE_LIMITS_APPLIED = True
    logger.info(
        "aggregate cgroup ceiling on %s: MemoryMax=%dM MemorySwapMax=0 TasksMax=%d "
        "(per-scope ceilings unchanged)",
        _CGROUP_AGENTS_SLICE,
        total_mem_mb,
        total_tasks,
    )
    return True


# cgroup v2 directory of the systemd user manager's subtree (transient
# --user units always live under user@<uid>.service on the unified
# hierarchy); ``{uid}`` is filled at resolve time. Module-level so tests can
# point the resolver at a fabricated tree.
_USER_MANAGER_CGROUP_BASE = "/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"


def _agents_slice_cgroup_dir() -> Path | None:
    """Resolve the agents slice's cgroup directory, or None when absent.

    systemd's dash-hierarchy places ``kirocrew-agents.slice`` under
    ``kirocrew.slice`` inside the user manager's subtree; the direct
    construction covers that. The shallow scan tolerates a manager that laid
    the slice out differently (one extra level only — never a recursive walk).
    The directory exists only while the slice is active (a runtime property or
    a live scope holds it); None simply means "no agent work to observe".
    """
    if sys.platform != "linux":
        return None
    base = Path(_USER_MANAGER_CGROUP_BASE.format(uid=os.getuid()))
    direct = base / "kirocrew.slice" / _CGROUP_AGENTS_SLICE
    if direct.is_dir():
        return direct
    try:
        for child in base.iterdir():
            cand = child / _CGROUP_AGENTS_SLICE
            if cand.is_dir():
                return cand
    except OSError:
        pass
    return None


def _read_cgroup_counters(path: Path) -> dict[str, int]:
    """Parse a ``memory.events``-style key/value cgroup file into a dict."""
    counters: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                counters[key] = int(value)
    except OSError:
        pass
    return counters


def read_cgroup_int(path: str | Path) -> int | None:
    """Read a single-value cgroup file (``memory.high``, ``memory.max`` and kin).

    The one reader for every single-integer cgroup file the product consults,
    here and in ``subagent``'s memory probe. ``None`` when the file is absent,
    unparseable, or holds the ``max`` sentinel the kernel writes for "no
    limit" -- every caller treats all three the same way, as "this bound does
    not constrain".
    """
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


# Last ``memory.events`` ``high`` counter seen by ``agents_slice_throttling``.
# Separate from ``_SLICE_MEMHIGH_EVENTS_SEEN``: that one paces a once-per-episode
# WARNING from the reconcile worker, this one answers a yes/no question for a
# caller that is about to commit a cold start. Sharing the baseline would let
# either reader consume the other's climb.
_SLICE_THROTTLE_PROBE_SEEN: int | None = None

# ``time.monotonic()`` of the most recent counter advance any probe observed.
# The advance itself is consumed by the probe that reads it (the baseline moves
# to the new value), so a second probe moments later would otherwise read a
# stable counter and answer ``False`` while the episode is still under way. The
# timestamp makes the edge a shared, time-bounded fact instead of a
# one-reader event: every probe inside the hold window agrees.
_SLICE_THROTTLE_EDGE_AT: float | None = None
_SLICE_THROTTLE_EDGE_HOLD_SECS = 60.0


def agents_slice_throttling() -> bool:
    """Whether the kernel is throttling the agents slice right now.

    Two signals, either suffices. ``memory.current >= memory.high`` is the
    kernel's own definition of "over the soft ceiling"; under sustained
    pressure reclaim holds usage AT the ceiling rather than above it, so the
    equality is the steady state, not an edge. The slice's own ``high`` event
    counter (:func:`_slice_own_high_events`) climbing since this function's previous read is the second signal:
    the kernel increments it every time it throttles, so during an episode it
    advances between any two probes even when reclaim has momentarily pushed
    usage under the line. The first read only baselines the counter. An
    observed advance is held for ``_SLICE_THROTTLE_EDGE_HOLD_SECS`` so that
    concurrent callers (two cold starts racing one counter tick) all read the
    same verdict instead of the first one consuming the edge.

    ``False`` whenever there is nothing to read (not Linux, no slice); an
    unmeasurable host is never reported as throttled.
    """
    global _SLICE_THROTTLE_PROBE_SEEN, _SLICE_THROTTLE_EDGE_AT
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return False
    counter = _slice_own_high_events(slice_dir)
    previous = _SLICE_THROTTLE_PROBE_SEEN
    now = time.monotonic()
    if counter is not None:
        _SLICE_THROTTLE_PROBE_SEEN = counter
        if previous is not None and counter > previous:
            _SLICE_THROTTLE_EDGE_AT = now
    high = read_cgroup_int(slice_dir / "memory.high")
    current = read_cgroup_int(slice_dir / "memory.current")
    if high is not None and current is not None and current >= high:
        return True
    edge_at = _SLICE_THROTTLE_EDGE_AT
    return edge_at is not None and (now - edge_at) < _SLICE_THROTTLE_EDGE_HOLD_SECS


# Last-seen slice-level OOM counters, so only NEW kills are reported. Seeded
# lazily from the current values on first read: kills that predate this
# process must not fire a spurious warning at boot.
_SLICE_OOM_SEEN: dict[str, int] | None = None


def check_agents_slice_pressure() -> str | None:
    """Report (and log) new OOM kills inside the agents slice, else None.

    With an aggregate ceiling on the slice, a breach OOM-kills SOME scope in
    it — the kernel picks the victim, not necessarily the spawn that grew.
    Without attribution the operator-visible failure is "a random subagent
    died". This turns it into a diagnosable event: which scopes took kills
    (each scope's own ``memory.events.local oom_kill``), the slice's
    ``memory.current`` vs ``memory.max`` at observation time, and whether the
    SLICE ceiling itself engaged (``memory.events.local max`` on the slice —
    the discriminator between a slice-level breach and a single scope hitting
    its own per-tree limit).

    Reads a handful of cgroup files; never raises. Polled from the resource
    pressure sampler's worker thread, so it is already off-loop. The same
    poll also re-applies the slice ceiling if a user-manager restart dropped
    the --runtime property (see the self-heal block below).
    """
    global _SLICE_OOM_SEEN, _SLICE_LIMITS_APPLIED
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return None
    # Self-heal: the ceiling is a --runtime property, so a user-manager
    # restart (logout/reboot) silently drops it while the gateway keeps
    # running. This sampler already reads the slice each tick — if the
    # ceiling we applied has vanished (memory.max reads "max"), re-apply it
    # here instead of waiting for the next gateway start. Only when WE
    # applied it before: a host that never passed the delegation gate must
    # not start shelling out from the sampler.
    if _SLICE_LIMITS_APPLIED:
        try:
            if (slice_dir / "memory.max").read_text().strip() == "max":
                _SLICE_LIMITS_APPLIED = False
                ensure_agents_slice_limits()
        except OSError:
            pass
    events = _read_cgroup_counters(slice_dir / "memory.events")
    local = _read_cgroup_counters(slice_dir / "memory.events.local")
    # ``oom_kill`` counts processes killed by ANY OOM killer, the host-wide
    # one included, while ``oom`` is raised only when a cgroup's own limit was
    # reached. Both are hierarchical in ``memory.events``, so a kill with no
    # ``oom`` event anywhere under the slice was the machine running out of
    # memory, not a per-scope or slice ceiling.
    current = {
        "oom_kill": events.get("oom_kill", 0),
        "oom": events.get("oom", 0),
        "max": local.get("max", 0),
    }
    if _SLICE_OOM_SEEN is None:
        _SLICE_OOM_SEEN = current
        return None
    new_kills = current["oom_kill"] - _SLICE_OOM_SEEN.get("oom_kill", 0)
    limit_ooms = current["oom"] - _SLICE_OOM_SEEN.get("oom", 0)
    slice_max_hits = current["max"] - _SLICE_OOM_SEEN.get("max", 0)
    _SLICE_OOM_SEEN = current
    if new_kills <= 0:
        return None
    victims: list[str] = []

    def _record_if_killed(scope_dir: Path) -> None:
        scope_local = _read_cgroup_counters(scope_dir / "memory.events.local")
        if scope_local.get("oom_kill", 0) > 0:
            victims.append(scope_dir.name)

    try:
        # Scopes sit one level deep: each instance places
        # its own under a per-instance child slice (_agents_slice_name), so
        # scanning only direct children would report "(already reaped)" for
        # every real victim. Both shapes are walked rather than just the nested
        # one, because the aggregate slice is shared host-wide: a co-resident
        # gateway still on an older build keeps putting scopes directly here,
        # and its OOM kills are still worth naming.
        for child in slice_dir.iterdir():
            if not child.is_dir():
                continue
            if child.suffix == ".scope":
                _record_if_killed(child)
            elif child.suffix == ".slice":
                for grandchild in child.iterdir():
                    if grandchild.suffix == ".scope" and grandchild.is_dir():
                        _record_if_killed(grandchild)
    except OSError:
        pass
    mem_current = -1
    try:
        mem_current = int((slice_dir / "memory.current").read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        mem_max = (slice_dir / "memory.max").read_text().strip()
    except OSError:
        mem_max = "?"
    if slice_max_hits > 0:
        cause = "yes"
    elif limit_ooms > 0:
        # The value the next scope from THIS gateway would get. A scope keeps
        # the ceiling it was started with, and a co-resident gateway may use
        # another config, so it is labelled as configuration, not as the
        # victim's enforced limit.
        per_scope_mb = _cgroup_limits_from_config()[1]
        cause = (
            "no (at least one scope hit its own per-tree limit; this gateway's "
            f"configured per-scope MemoryMax={per_scope_mb}M)"
        )
    else:
        # An ``oom`` event raised on a cgroup ABOVE the slice does not
        # propagate down into it, so this case cannot tell the host-wide OOM
        # killer from a limit on an enclosing slice; both name the victim in
        # the kernel log.
        cause = (
            "no (no cgroup limit inside this slice was reached: the host-wide "
            "kernel OOM killer or a limit above this slice fired; the kernel "
            "log says which)"
        )
    reaped = "(already reaped; the kernel log names the killed process)"
    message = (
        f"cgroup OOM kill inside {_CGROUP_AGENTS_SLICE}: {new_kills} new kill(s); "
        f"slice memory.current={mem_current} memory.max={mem_max}; "
        f"slice-level aggregate ceiling engaged: {cause}; "
        f"scopes with recorded kills: {victims or reaped}"
    )
    logger.warning("%s", message)
    return message


# ── proactive page-cache reclaim on the agents slice ──
# The kernel charges page cache to the cgroup that read the file, so ordinary
# file-heavy agent work (build trees, checkouts, logs) fills the slice's
# memory.high with CLEAN cache. memory.current then sits at memory.high and the
# kernel throttles the whole agent subtree while it reclaims, even though almost
# none of that memory is process memory. Writing the slice's memory.reclaim
# drops the cache up front, so the throttle never engages for cache alone. The
# resource-pressure sampler calls reclaim_agents_slice_page_cache() from its
# worker thread every tick; the writes themselves run in their own thread so a
# slow reclaim cannot eat the sampler's probe budget.

# Default trigger: reclaim once memory.current reaches this percent of
# memory.high. Overridden by resource_limits.slice_reclaim_trigger_pct (0 turns
# the feature off; 50..99 is accepted, anything else uses this default).
_SLICE_RECLAIM_DEFAULT_TRIGGER_PCT = 95
_SLICE_RECLAIM_MIN_TRIGGER_PCT = 50
_SLICE_RECLAIM_MAX_TRIGGER_PCT = 99
# Reclaim brings memory.current down to about this fraction of memory.high --
# or ten points under a lower configured trigger, so a reclaim always ends
# below the line that started it.
_SLICE_RECLAIM_TARGET_FRACTION = 0.85
_SLICE_RECLAIM_TARGET_GAP_PCT = 10
# Only reclaim when reclaimable file cache is at least this share of
# memory.current. Below it the pressure is process memory, which reclaim
# cannot free without swap, so writing would only stall the slice.
_SLICE_RECLAIM_FILE_SHARE_MIN = 0.5
# One memory.reclaim write asks for at most this much; the kernel holds the
# writer until it has reclaimed the amount or given up, so a bounded chunk
# bounds one write.
_SLICE_RECLAIM_CHUNK_BYTES = 1024 * 1024 * 1024
# An episode stops issuing chunks after this long; whatever is left waits for
# the next episode.
_SLICE_RECLAIM_EPISODE_BUDGET_SECS = 10.0
# At most one episode per this many seconds.
_SLICE_RECLAIM_MIN_INTERVAL_SECS = 60.0
# A planned reclaim smaller than this is skipped. Arbitrary: a few MiB is too
# little cache to be what holds a slice at its ceiling.
_SLICE_RECLAIM_MIN_BYTES = 4 * 1024 * 1024
# Host swap is read here. Module-level so tests can point it at a file.
_PROC_MEMINFO = "/proc/meminfo"

# Set after memory.reclaim proved unusable (missing on a pre-5.19 kernel,
# refused, or rejected); the feature then stays off for the process.
_SLICE_RECLAIM_DISARMED = False
# time.monotonic() when the last episode started, for the rate limit.
_SLICE_RECLAIM_LAST_AT: float | None = None
# Held by the running episode's thread, so episodes never overlap.
_SLICE_RECLAIM_LOCK = threading.Lock()
# Each "skipped" reason is logged once per process at INFO.
_SLICE_RECLAIM_SKIP_LOGGED: set[str] = set()


def _slice_reclaim_trigger_pct() -> int:
    """The configured trigger percent, ``0`` when the feature is off.

    ``resource_limits.slice_reclaim_trigger_pct``: unset uses
    :data:`_SLICE_RECLAIM_DEFAULT_TRIGGER_PCT`, ``0`` is off, 50..99 is used as
    written, and a value the parse refuses or one under 50 uses the default.
    """
    try:
        # circular import: same function-level pattern as _cgroup_limits_from_config.
        # The full load, so a value (the off switch included) set only in the
        # config.local.json overlay counts.
        from kiro_crew.config.loader import KiroCrewConfig

        pct = KiroCrewConfig.load().resource_limits.slice_reclaim_trigger_pct
    except Exception:
        logger.debug("slice reclaim: config unavailable, using the default trigger")
        return _SLICE_RECLAIM_DEFAULT_TRIGGER_PCT
    if pct == 0:
        return 0
    if pct is None or not (_SLICE_RECLAIM_MIN_TRIGGER_PCT <= pct <= _SLICE_RECLAIM_MAX_TRIGGER_PCT):
        return _SLICE_RECLAIM_DEFAULT_TRIGGER_PCT
    return pct


def _slice_reclaimable_file(stat: dict[str, int]) -> int:
    """Clean file cache reclaim can drop without writeback or swap.

    In cgroup v2 ``file`` includes ``shmem`` (tmpfs, shared anonymous memory),
    which reclaim can only swap, and dirty or in-flight pages need writeback
    first; none of those count.
    """
    reclaimable = (
        stat.get("file", 0)
        - stat.get("shmem", 0)
        - stat.get("file_dirty", 0)
        - stat.get("file_writeback", 0)
    )
    return max(reclaimable, 0)


def _plan_slice_reclaim(
    slice_dir: Path, trigger_pct: int
) -> tuple[int, int, dict[str, int]] | None:
    """``(bytes_to_reclaim, target_current, memory.stat)``, or None for no reclaim.

    All three must hold: ``memory.high`` is finite, ``memory.current`` is at
    least *trigger_pct* percent of it, and reclaimable file cache is at least
    :data:`_SLICE_RECLAIM_FILE_SHARE_MIN` of ``memory.current``. The amount
    brings ``memory.current`` to the target, capped at the reclaimable cache.
    """
    high = read_cgroup_int(slice_dir / "memory.high")
    current = read_cgroup_int(slice_dir / "memory.current")
    if not high or current is None or current * 100 < high * trigger_pct:
        return None
    stat = _read_cgroup_counters(slice_dir / "memory.stat")
    reclaimable = _slice_reclaimable_file(stat)
    if reclaimable < current * _SLICE_RECLAIM_FILE_SHARE_MIN:
        return None
    target_fraction = min(
        _SLICE_RECLAIM_TARGET_FRACTION, (trigger_pct - _SLICE_RECLAIM_TARGET_GAP_PCT) / 100
    )
    target = int(high * target_fraction)
    amount = min(current - target, reclaimable)
    if amount < _SLICE_RECLAIM_MIN_BYTES:
        return None
    return amount, target, stat


def _host_swap_total_kb() -> int | None:
    """``SwapTotal`` from ``/proc/meminfo`` in kB, or None when unreadable."""
    try:
        for line in Path(_PROC_MEMINFO).read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            if key == "SwapTotal":
                value = rest.split()
                return int(value[0]) if value and value[0].isdigit() else None
    except OSError:
        return None
    return None


def _slice_reclaim_cannot_swap(slice_dir: Path) -> bool:
    """Whether a reclaim on the slice can only drop cache, never swap anon out.

    True when the slice's ``memory.swap.max`` reads ``0`` (what
    ``ensure_agents_slice_limits`` sets with ``MemorySwapMax=0``) or the host
    has no swap at all. Anything unreadable counts as "might swap".
    """
    if read_cgroup_int(slice_dir / "memory.swap.max") == 0:
        return True
    return _host_swap_total_kb() == 0


def _write_memory_reclaim(path: Path, nbytes: int) -> None:
    """Ask the kernel to reclaim *nbytes* from the cgroup owning *path*.

    Raises ``OSError`` as the kernel reports it: ``EAGAIN`` means it reclaimed
    less than asked.
    """
    fd = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.write(fd, str(nbytes).encode("ascii"))
    finally:
        os.close(fd)


def _disarm_slice_reclaim(reason: object, *, level: int = logging.WARNING) -> None:
    """Turn the reclaim off for this process, with one log line at *level*."""
    global _SLICE_RECLAIM_DISARMED
    if _SLICE_RECLAIM_DISARMED:
        return
    _SLICE_RECLAIM_DISARMED = True
    logger.log(
        level,
        "agent slice %s: proactive page-cache reclaim is off for this process (%s); "
        "the slice MemoryHigh ceiling still applies. See "
        "docs/architecture/resource-protection.md.",
        _CGROUP_AGENTS_SLICE,
        reason,
    )


def _log_slice_reclaim_skip_once(reason: str) -> None:
    if reason in _SLICE_RECLAIM_SKIP_LOGGED:
        return
    _SLICE_RECLAIM_SKIP_LOGGED.add(reason)
    logger.info("agent slice %s: page-cache reclaim skipped: %s", _CGROUP_AGENTS_SLICE, reason)


def _run_slice_reclaim_episode(slice_dir: Path, amount: int, target: int) -> None:
    """Write *amount* bytes to the slice's ``memory.reclaim`` in bounded chunks.

    Stops early once ``memory.current`` reaches *target*, when the kernel
    answers ``EAGAIN`` (it reclaimed less than asked, which is normal), or when
    the episode's time budget runs out. Any other error disarms the feature.
    Logs one INFO line with the before and after figures. Never raises; always
    releases :data:`_SLICE_RECLAIM_LOCK`.
    """
    try:
        before_current = read_cgroup_int(slice_dir / "memory.current")
        before = _read_cgroup_counters(slice_dir / "memory.stat")
        reclaim_path = slice_dir / "memory.reclaim"
        deadline = time.monotonic() + _SLICE_RECLAIM_EPISODE_BUDGET_SECS
        remaining = amount
        requested = 0
        while remaining > 0:
            chunk = min(_SLICE_RECLAIM_CHUNK_BYTES, remaining)
            try:
                _write_memory_reclaim(reclaim_path, chunk)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    logger.debug("agent slice reclaim: kernel reclaimed less than %d bytes", chunk)
                    requested += chunk
                    break
                _disarm_slice_reclaim(f"memory.reclaim write failed: {exc}")
                return
            requested += chunk
            remaining -= chunk
            current = read_cgroup_int(slice_dir / "memory.current")
            if current is not None and current <= target:
                break
            if time.monotonic() >= deadline:
                break
        after_current = read_cgroup_int(slice_dir / "memory.current")
        after = _read_cgroup_counters(slice_dir / "memory.stat")
        logger.info(
            "agent slice %s: reclaimed page cache (asked %d bytes): memory.current "
            "%s -> %s, file %d -> %d, anon %d -> %d",
            _CGROUP_AGENTS_SLICE,
            requested,
            before_current,
            after_current,
            before.get("file", 0),
            after.get("file", 0),
            before.get("anon", 0),
            after.get("anon", 0),
        )
    except Exception:
        logger.debug("agent slice reclaim episode failed", exc_info=True)
    finally:
        _SLICE_RECLAIM_LOCK.release()


def reclaim_agents_slice_page_cache() -> threading.Thread | None:
    """Start a page-cache reclaim on the agents slice when clean cache holds it at its ceiling.

    Called each tick of the resource-pressure sampler, from its worker thread.
    The decision costs a handful of cgroup file reads and, once the slice is
    near its ceiling, one config load; when it says reclaim, the
    writes run in a new daemon thread, which is returned so a caller can wait
    for it. Returns None when nothing was started.

    Nothing happens when the slice cannot be read (not Linux, no cgroup v2, the
    slice not materialized), inside a pod (``KIROCREW_POD`` set: the slice is
    the user's live one), when the feature is off
    (``resource_limits.slice_reclaim_trigger_pct`` = 0) or disarmed, within
    :data:`_SLICE_RECLAIM_MIN_INTERVAL_SECS` of the last episode, or when a
    reclaim could push process memory to swap. Never raises.
    """
    global _SLICE_RECLAIM_LAST_AT
    if _SLICE_RECLAIM_DISARMED:
        return None
    # A pod gateway shares the user's slice with the live one; only a gateway
    # outside a pod reclaims it.
    if os.environ.get("KIROCREW_POD"):
        return None
    try:
        slice_dir = _agents_slice_cgroup_dir()
        if slice_dir is None:
            return None
        now = time.monotonic()
        last = _SLICE_RECLAIM_LAST_AT
        if last is not None and now - last < _SLICE_RECLAIM_MIN_INTERVAL_SECS:
            return None
        # Cheap pre-check before the config read: no accepted trigger is under
        # the minimum percent, so a slice below it never needs the config.
        high = read_cgroup_int(slice_dir / "memory.high")
        current = read_cgroup_int(slice_dir / "memory.current")
        if not high or current is None or current * 100 < high * _SLICE_RECLAIM_MIN_TRIGGER_PCT:
            return None
        trigger_pct = _slice_reclaim_trigger_pct()
        if trigger_pct == 0:
            return None
        plan = _plan_slice_reclaim(slice_dir, trigger_pct)
        if plan is None:
            return None
        amount, target, _stat = plan
        if not _slice_reclaim_cannot_swap(slice_dir):
            _log_slice_reclaim_skip_once(
                "the slice may swap (memory.swap.max is not 0 and the host has swap), "
                "so a reclaim could push agent memory to swap"
            )
            return None
        if not (slice_dir / "memory.reclaim").exists():
            # The normal state on an older kernel, not a fault.
            _disarm_slice_reclaim(
                "this kernel has no memory.reclaim (needs Linux 5.19+)", level=logging.INFO
            )
            return None
        if not _SLICE_RECLAIM_LOCK.acquire(blocking=False):
            return None
        _SLICE_RECLAIM_LAST_AT = now
        worker = threading.Thread(
            target=_run_slice_reclaim_episode,
            args=(slice_dir, amount, target),
            name="agent-slice-reclaim",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            _SLICE_RECLAIM_LOCK.release()
            logger.debug("agent slice reclaim: could not start the worker thread")
            return None
        return worker
    except Exception:
        logger.debug("agent slice reclaim check failed", exc_info=True)
        return None
