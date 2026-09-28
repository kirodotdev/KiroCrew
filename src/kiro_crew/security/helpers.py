"""Foundation layer for the security controls.

Everything here is reachable from every other cluster in the package and reaches
none of them: this module imports nothing from ``kiro_crew.security``, which is
what makes it the bottom of the dependency order and keeps the split acyclic.

Two things live here. The public prompt-injection screen, because the shared
vocabulary it reads is the whole of its dependency -- a screen that cannot run
must not silently pass untrusted content, so that vocabulary is imported at
module scope with no lazy path. And the resource-limit policy reader with the
``preexec_fn`` builder it feeds, because they bound a SPAWN rather than judge a
path or a command, so no other cluster has a claim on them.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

try:
    import resource as _resource
except ImportError:
    _resource = None  # type: ignore[assignment]  # Windows/non-POSIX

from kiro_crew.vector_memory_constants import _contains_injection

if TYPE_CHECKING:
    from collections.abc import Callable


def contains_injection(text: str | None) -> bool:
    """Return True if *text* matches a known prompt-injection pattern.

    Accepts ``None`` (returns ``False``) so callers can screen optional
    fetched content — e.g. a Slack ``thread_parent_text`` that may be unset —
    without a separate None check.

    Public wrapper over the shared ``_INJECTION_PATTERNS`` set (defined in the
    dependency-free ``vector_memory_constants`` module) so untrusted content
    pulled from external surfaces — e.g. Slack thread-parent / thread-metadata
    fetched from arbitrary, possibly non-owner authors — can be screened
    before it is injected into the LLM prompt. The pattern set lives in the
    light constants module (not ``vector_memory``, whose numpy/faiss/stemmer
    deps are heavy), so it is imported at module top level with no lazy import
    and no fail-open path: a screen that cannot run must not silently pass
    untrusted content through.
    """
    if not text:
        return False
    return _contains_injection(text)


# ── Resource Limits (preexec_fn) ──
# Applied to agent-influenced subprocess spawns to bound resource-exhaustion
# attacks (fork bombs, FD exhaustion, runaway memory/CPU) so a compromised or
# buggy tool/MCP server cannot starve the host out from under the gateway.
# Uses POSIX resource limits (setrlimit); see docs/architecture/resource-protection.md.

# Default ceilings. Only RLIMIT_NOFILE is default-on: it is per-PROCESS,
# generous enough that no legitimate tool trips it, yet finite so a descriptor
# leak (which climbs unbounded) is arrested. The other three default to 0
# (disabled) ON PURPOSE — each is unsafe as a blanket default (see the caveats
# below) — but all four stay operator-configurable per deployment.
#
# Why not a default-on fork-bomb / memory cap? RLIMIT is the wrong tool for
# those defaults: RLIMIT_NPROC is per-UID (not per-subtree) and RLIMIT_AS caps
# virtual (not resident) memory. cgroup v2 ``pids.max`` / ``memory.max`` are the
# correct per-cgroup fork-bomb and RSS ceilings and are tracked as future work
# (see docs/architecture/resource-protection.md); the ticket itself lists cgroup v2 as the
# alternative. This helper delivers the safe RLIMIT subset now and leaves the
# hazardous knobs opt-in.
_RLIMIT_DEFAULTS = {
    # RLIMIT_NOFILE: max open file descriptors (per-process). Caps FD leaks.
    "max_open_files": 1024,
    # RLIMIT_NPROC: max processes for the child's real UID. 0 = disabled
    # (default). CAVEAT: this is enforced per real-UID against the count of ALL
    # the user's existing processes AND threads — NOT the spawn's own subtree.
    # A busy login/desktop UID can already hold thousands of threads (a fork
    # bomb is bounded only relative to that shared total), so any fixed cap that
    # is tight enough to matter is below a real host's baseline and would make
    # EVERY spawn fail to fork (EAGAIN). Safe to enable ONLY when the gateway
    # runs as its own dedicated UID; operators opt in via config there.
    # NOTE: the fork-bomb defense that IS default-on is the cgroup v2 scope
    # (sandbox.cgroup_scope_argv → pids.max), which is per-cgroup not per-UID.
    # This same ``max_processes`` key sets that cgroup pids.max ceiling (default
    # 8192 there); the RLIMIT_NPROC path below stays opt-in for the reasons above.
    "max_processes": 0,
    # RLIMIT_CPU: CPU-seconds. 0 = disabled (default). CAVEAT: this counts
    # against the WHOLE lifetime of a long-lived process — the root agent runs
    # up to a 30-min wall-clock turn and a busy tool-heavy session can
    # legitimately burn hundreds of CPU-seconds, so a non-zero global cap
    # SIGXCPU-kills healthy sessions. Set per-deployment only if the spawn
    # population is exclusively short-lived tools.
    "max_cpu_seconds": 0,
    # RLIMIT_AS: virtual address space (bytes-worth, expressed in MB). 0 =
    # disabled (default). CAVEAT: RLIMIT_AS caps VIRTUAL memory, not resident
    # memory, and Node/V8 (kiro-cli, claude-agent-acp, every npm MCP server)
    # reserves huge virtual mappings far exceeding real use — measured ~2GB VSZ
    # for 4 idle worker threads, ~3.4GB for 8 — so even a "generous" 4GB cap
    # SIGKILLs normal MCP-heavy sessions with spurious ENOMEM. Do NOT enable
    # globally for Node-backed spawns. The default-on memory ceiling is instead
    # the cgroup v2 scope (sandbox.cgroup_scope_argv → memory.max, an RSS cap,
    # host-proportional by default — 65% of physical RAM, so ~10.6 GB on a
    # 16 GB box / ~21.3 GB on 32 GB), which this same ``max_memory_mb`` key
    # overrides; the RLIMIT_AS path here stays opt-in for non-Node fleets.
    "max_memory_mb": 0,
}


def _bias_child_oom_score() -> None:
    """Bias the kernel OOM killer toward the calling process (``oom_score_adj``
    = 1000, inherited by descendants) so a memory-ballooning tool subprocess is
    killed BEFORE the cgroup ``memory.max`` ceiling takes out the whole agent
    scope. Linux-only, unprivileged, best-effort — never raises. Kept
    async-signal-safe (single open/write/close, no allocation-heavy work) so it
    is callable from a ``preexec_fn``. Pattern from OpenClaw's linux-oom-score
    child shim.
    """
    if sys.platform != "linux":
        return
    try:
        fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)
        try:
            os.write(fd, b"1000")
        finally:
            os.close(fd)
    except OSError:
        pass


def resource_limit_spec(config: dict | None = None) -> list[tuple[str, int]]:
    """Resolve the configured rlimits as ``(RLIMIT_* name, value)`` pairs.

    Split out of :func:`apply_resource_limits` so one policy reader serves both
    ways of applying the limits:

    * **post-fork**, as the ``preexec_fn`` :func:`apply_resource_limits` builds;
    * **post-exec**, by the process-group supervisor
      (``_process_group_supervisor.py``), which receives these pairs on its argv
      because it cannot import this module -- it runs under ``python -I -c`` from
      an immutable gateway-captured source string, and that is deliberate: a
      mutable package path would let a same-UID agent swap the code out.

    Names, not ``resource`` constants: the consumer resolves them with
    ``getattr`` and skips any its platform lacks. A value of ``0`` means "leave
    inherited" and is dropped here -- the OPPOSITE of what ``0`` means on the
    cgroup path, which reads two of these same keys and treats ``0`` as "use the
    module default" because systemd rejects a zero property. Both domains are
    stated on ``ResourceLimitsConfig``, which is where the coercion lives.
    """
    limits = dict(_RLIMIT_DEFAULTS)
    if config:
        # The one validated parse for this block. Two things this replaces a
        # local ``val >= 0`` test to get: an Infinity from json.loads passes that
        # test and then raises OverflowError inside ``int()`` -- with no
        # try/except on this path, so it propagates out of resource_limit_preexec
        # and fails the spawn; and a fraction in (0, 1) floors to 0,
        # which is this path's "leave inherited" sentinel, silently dropping a
        # limit the operator asked for. from_raw refuses both and says so.
        # circular import: config.loader reaches back into this module (it
        # imports security.is_sensitive_path function-locally for the same
        # reason), so importing the loader at security's module scope would
        # close the cycle. Kept function-level, matching sandbox and
        # resource_status, which read the same block under the same constraint.
        from kiro_crew.config.loader import ResourceLimitsConfig

        parsed = ResourceLimitsConfig.from_raw(config.get("resource_limits"))
        for key in _RLIMIT_DEFAULTS:
            # None means "not usable" -- keep the documented default rather than
            # inventing a number. An explicit 0 survives, because disabling a
            # limit is a real request here.
            val = getattr(parsed, key, None)
            if val is not None:
                limits[key] = val

    # (rlimit name, requested soft/hard value in the rlimit's native unit).
    max_memory_bytes = limits["max_memory_mb"] * 1024 * 1024
    specs = [
        ("RLIMIT_NPROC", limits["max_processes"]),
        ("RLIMIT_NOFILE", limits["max_open_files"]),
        ("RLIMIT_CPU", limits["max_cpu_seconds"]),
        ("RLIMIT_AS", max_memory_bytes),
    ]
    return [(name, value) for name, value in specs if value > 0]


def apply_resource_limits(config: dict | None = None) -> "Callable[[], None]":
    """Return a preexec_fn that applies POSIX resource limits to a child process.

    Reads limits from the ``resource_limits`` config section:
      - ``max_processes``: RLIMIT_NPROC (process count for the child's UID).
      - ``max_open_files``: RLIMIT_NOFILE (open file descriptors).
      - ``max_cpu_seconds``: RLIMIT_CPU in seconds (``0`` disables — default).
      - ``max_memory_mb``: RLIMIT_AS (virtual address space) in MB (``0``
        disables — default; see the RLIMIT_AS caveat in ``_RLIMIT_DEFAULTS``).

    Each key accepts a positive integer to set that limit, or ``0`` to leave the
    limit unchanged (inherited). Missing keys fall back to ``_RLIMIT_DEFAULTS``.
    A requested limit is always clamped DOWN to the inherited hard limit — we
    never try to *raise* a ceiling (an unprivileged child cannot, and the
    attempt would raise), so this can only tighten, never loosen, the child's
    budget.

    The returned callable is intended for use as ``preexec_fn`` in
    ``subprocess.Popen`` / ``asyncio.create_subprocess_exec``. It runs in the
    child process after fork but before exec — setrlimit calls here only affect
    the child. It is a no-op on non-POSIX platforms (``resource`` unavailable)
    and degrades gracefully per-limit on platforms lacking a specific rlimit
    (e.g. macOS has no RLIMIT_NPROC / a flaky RLIMIT_AS).

    NOTE: ``preexec_fn`` runs post-fork in a subprocess that may be
    multi-threaded; it MUST stay async-signal-safe — only ``getrlimit`` /
    ``setrlimit`` here, no allocation-heavy or lock-taking work.

    Args:
        config: Full Kiro Crew config dict (or any subset containing
            ``resource_limits``). Pass None for defaults.

    Returns:
        A no-arg callable suitable for ``preexec_fn``.
    """
    if _resource is None:
        # Non-POSIX (Windows): nothing to enforce.
        return lambda: None
    # Bind a non-None local so the nested preexec closure keeps the narrowed
    # type (closures don't inherit the guard's narrowing of the module global).
    res = _resource

    # Resolve the rlimit constants once in the parent (cheap, keeps the
    # post-fork callable minimal). Skip any this platform lacks.
    resolved = [
        (getattr(res, name), value)
        for name, value in resource_limit_spec(config)
        if hasattr(res, name)
    ]

    def _set_limits() -> None:
        """Apply resource limits in the child process (preexec_fn).

        Runs post-fork/pre-exec. Clamps each requested limit down to the
        inherited hard limit so we only ever tighten, and swallows per-limit
        failures so an unsupported rlimit never blocks the spawn.
        """
        for res_id, requested in resolved:
            try:
                _soft, hard = res.getrlimit(res_id)
                # Never exceed the inherited hard cap (RLIM_INFINITY == -1 means
                # "no ceiling", so any finite request is fine against it).
                if hard != res.RLIM_INFINITY:
                    requested = min(requested, hard)
                # Set BOTH soft and hard to the effective value: lowering the
                # hard cap (always permitted unprivileged) stops the child from
                # raising its own soft limit back up to escape the ceiling.
                res.setrlimit(res_id, (requested, requested))
            except (ValueError, OSError):
                # Platform doesn't support this rlimit, or the kernel rejected
                # the value — leave it inherited rather than fail the spawn.
                continue
        # Bias the OOM killer toward this child (see _bias_child_oom_score).
        _bias_child_oom_score()

    return _set_limits
