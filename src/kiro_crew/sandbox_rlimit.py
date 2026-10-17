"""Apply resource limits to a spawned child.

The ``preexec_fn`` limit profiles, the spawn shim that applies them without one, and
the limited ``create_subprocess`` / ``run`` / ``Popen`` wrappers. One of the owners
``kiro_crew.sandbox`` is carved into: every name here stays readable and patchable as
``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.sandbox_workspace import _directory_ancestor_identities, _open_directory_descriptor

try:
    import resource as _resource_mod
except ImportError:  # non-POSIX (Windows)
    _resource_mod = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Any

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


# Cached preexec_fn shared by every agent-influenced spawn. Built once from the
# loaded config (limits are process-global, not per-spawn) so the hot path adds
# nothing but a dict lookup. ``_UNSET`` distinguishes "not built yet" from the
# legitimate ``None`` result on non-POSIX platforms.
_UNSET = object()
_RESOURCE_PREEXEC: object = _UNSET


def resource_limit_preexec() -> "Callable[[], None] | None":
    """Return the shared ``preexec_fn`` that caps a spawned child's resources.

    This is the companion to :func:`sandboxed_spawn_argv`: the sandbox wrapper
    gives a child filesystem + credential isolation, and this gives it a
    kernel-enforced ceiling on processes / file descriptors / CPU / memory so a
    fork bomb or runaway allocation in a compromised tool or MCP server cannot
    exhaust the host out from under the gateway. Call sites do not use this
    directly: agent-influenced spawns go through
    :func:`create_subprocess_limited` / :func:`run_limited` /
    :func:`popen_limited`, which deliver the same limits AFTER ``exec`` via the
    spawn shim and fall back to this ``preexec_fn`` only on a host with no
    usable shim (see ``docs/architecture/resource-protection.md``).

    Returns the callable from :func:`kiro_crew.security.apply_resource_limits`,
    or ``None`` on non-POSIX platforms (where there is nothing to enforce and
    ``preexec_fn`` must be ``None``). The callable and the underlying config
    read are computed once and cached — the limits are a host-global policy, not
    a per-spawn decision.
    """
    global _RESOURCE_PREEXEC
    if _RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            # Non-POSIX (Windows): preexec_fn is unsupported by
            # create_subprocess_exec and MUST be None — passing any callable
            # (even a no-op) raises ValueError. Cache None to honor the return
            # contract. (apply_resource_limits also no-ops there, but it returns
            # a callable, so we must not forward it.)
            _RESOURCE_PREEXEC = None
            return None
        # Lazy imports: sandbox is a low-level module (see the SEL import note in
        # wrap_argv) and must not import config/security at module load.
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            # Raw config.json (process-cached) — carries the unrecognized
            # ``resource_limits`` key an operator may add; the typed config
            # schema drops unknown keys, so read the raw dict here.
            from kiro_crew.config.loader import _raw_config

            cfg = _raw_config()
        except Exception:
            # Config unavailable (early boot, tests) — apply_resource_limits
            # falls back to its safe built-in defaults.
            logger.debug("resource_limit_preexec: config unavailable, using defaults")
        # POSIX: apply_resource_limits returns a callable (a no-op only when
        # every limit is disabled). Cache it; passing a no-op preexec_fn is fine.
        _RESOURCE_PREEXEC = apply_resource_limits(cfg)
    return _RESOURCE_PREEXEC  # type: ignore[return-value]


_EXTRACTOR_PREEXEC: object = _UNSET


def extractor_resource_limit_preexec() -> "Callable[[], None] | None":
    """Legacy ``preexec_fn`` for :data:`RLIMIT_PROFILE_EXTRACTOR`, shim-less hosts only.

    Same fixed ceiling ``_rlimit_spec`` emits for the profile, applied post-fork
    through :func:`kiro_crew.security.apply_resource_limits`. ``None`` off POSIX,
    where ``preexec_fn`` must not be passed.
    """
    global _EXTRACTOR_PREEXEC
    if _EXTRACTOR_PREEXEC is _UNSET:
        if os.name != "posix":
            _EXTRACTOR_PREEXEC = None
            return None
        from kiro_crew.security import apply_resource_limits

        _EXTRACTOR_PREEXEC = apply_resource_limits(
            {
                "resource_limits": {
                    "max_memory_mb": _EXTRACTOR_MAX_AS_BYTES // (1024 * 1024),
                    "max_cpu_seconds": _EXTRACTOR_MAX_CPU_SECS,
                    "max_open_files": _EXTRACTOR_MAX_NOFILE,
                }
            }
        )
    return _EXTRACTOR_PREEXEC  # type: ignore[return-value]


# Cached ``--rlimits=`` argv fragment for the process-group supervisor. Same
# policy as ``resource_limit_preexec``, delivered post-exec instead of post-fork.
_RESOURCE_SUPERVISOR_ARGV: object = _UNSET


def resource_limit_supervisor_argv() -> "tuple[str, ...]":
    """Return the supervisor's ``--rlimits=`` argv fragment (empty if none apply).

    The alternative to :func:`resource_limit_preexec` for the one spawn that
    already prepends ``_process_group_supervisor.py``. Passing ``preexec_fn=``
    forces CPython to ``fork()`` the multi-GB, ~118-thread gateway and run Python
    in the child before ``exec``; a lock another thread held at fork time is
    unreleasable there, and that is how a child deadlocked in a futex, never
    exec'd, and pinned every fd it had inherited. Handing the limits to the
    supervisor moves the same ``setrlimit`` calls after ``exec``, where the
    process is single-threaded, and the exec'd child inherits them either way.

    The values are policy numbers, not secrets, so argv (world-readable via
    ``ps``) is a fine channel.
    """
    global _RESOURCE_SUPERVISOR_ARGV
    if _RESOURCE_SUPERVISOR_ARGV is _UNSET:
        if os.name != "posix":
            _RESOURCE_SUPERVISOR_ARGV = ()
            return ()
        from kiro_crew.security import resource_limit_spec

        cfg: dict | None = None
        try:
            from kiro_crew.config.loader import _raw_config

            cfg = _raw_config()
        except Exception:
            logger.debug("resource_limit_supervisor_argv: config unavailable, using defaults")
        spec = ",".join(f"{name}:{value}" for name, value in resource_limit_spec(cfg))
        _RESOURCE_SUPERVISOR_ARGV = (f"--rlimits={spec}",) if spec else ()
    return _RESOURCE_SUPERVISOR_ARGV  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Session host preexec — the inverse of resource_limit_preexec.
# ---------------------------------------------------------------------------

_SESSION_HOST_PREEXEC: object = _UNSET


def session_host_preexec() -> "Callable[[], None] | None":
    """Return a ``preexec_fn`` that *raises* NOFILE for a session host process.

    Session hosts (kiro-cli-chat / claude-agent-acp) are **trusted** internal
    processes — they manage a tree of MCP server subprocesses, each consuming
    pipe fd pairs for stdin/stdout communication.  A single session host may
    hold 100-200 fds under normal operation (10+ MCP servers × pipe pairs +
    sockets + log files).

    The default ``resource_limit_preexec()`` caps NOFILE at 1024 to defend
    against compromised *tool* processes, but applying the same cap to the
    trusted session host causes "Too many open files" crashes when subagent
    concurrency or MCP server count is high.

    This preexec raises NOFILE soft+hard to the *gateway's* inherited hard
    limit (typically 10240 from the systemd unit, or 524288 kernel max) so
    the session host has headroom proportional to the gateway itself.  Other
    resource limits (NPROC, CPU, AS) are left inherited (this preexec touches NOFILE only) — a
    session host has no legitimate reason to fork-bomb or allocate unbounded
    memory.

    Returns ``None`` on non-POSIX platforms (preexec_fn must be None there).
    """
    global _SESSION_HOST_PREEXEC
    if _SESSION_HOST_PREEXEC is _UNSET:
        if os.name != "posix" or _resource_mod is None:
            _SESSION_HOST_PREEXEC = None
            return None

        res = _resource_mod

        def _raise_nofile() -> None:
            """Raise NOFILE to the hard limit in the child process."""
            try:
                _soft, hard = res.getrlimit(res.RLIMIT_NOFILE)
                if hard == res.RLIM_INFINITY:
                    # Kernel allows unlimited — cap at a sane maximum but never
                    # reduce below the inherited soft limit.
                    target = max(_soft, 65536)
                else:
                    target = hard
                res.setrlimit(res.RLIMIT_NOFILE, (target, hard))
            except (ValueError, OSError):
                pass  # Leave inherited — better than failing the spawn.

        _SESSION_HOST_PREEXEC = _raise_nofile
    return _SESSION_HOST_PREEXEC  # type: ignore[return-value]


# Build workloads (vite/npm/pip) legitimately hold thousands of descriptors —
# the default 1024 NOFILE ceiling EMFILEs them while still being the right cap
# for one-shot tools. Same policy, higher finite descriptor ceiling; every
# other limit still comes from the operator config. Cached like the default.
_BUILD_NOFILE_CEILING = 65536
_BUILD_RESOURCE_PREEXEC: object = _UNSET


def build_resource_limit_preexec() -> "Callable[[], None] | None":
    """``resource_limit_preexec`` variant for build-class children.

    Identical policy except ``max_open_files`` is raised to a still-finite
    65536 (matching the gateway service's own ``LimitNOFILE``); an operator
    ``resource_limits.max_open_files`` override higher than the default wins.
    """
    global _BUILD_RESOURCE_PREEXEC
    if _BUILD_RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            _BUILD_RESOURCE_PREEXEC = None
            return None
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            from kiro_crew.config.loader import _raw_config

            cfg = dict(_raw_config() or {})
        except Exception:
            cfg = {}
        raw_limits = (cfg or {}).get("resource_limits")
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        # Malformed operator values must not break the spawn — the shared parse
        # ignores anything out of domain and returns None, which floors to the
        # build ceiling here. Going through it keeps this path from being a
        # second rule for a key security.resource_limit_spec also reads.
        from kiro_crew.config.loader import ResourceLimitsConfig

        configured = ResourceLimitsConfig.from_raw(raw_limits).max_open_files or 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        _BUILD_RESOURCE_PREEXEC = apply_resource_limits({**(cfg or {}), "resource_limits": limits})
    return _BUILD_RESOURCE_PREEXEC  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Post-exec resource limits (the replacement for ``preexec_fn=``)
# ---------------------------------------------------------------------------

_SPAWN_SHIM_SOURCE = str(Path(__file__).with_name("_spawn_exec_shim.py"))
try:
    # Captured once, at gateway import, and passed to the interpreter as a ``-c``
    # source string. Loading it from the package path at SPAWN time would let a
    # same-UID agent rewrite the file between capture and use.
    _SPAWN_SHIM_CODE = Path(_SPAWN_SHIM_SOURCE).read_text(encoding="utf-8")
except OSError:  # pragma: no cover - only if the install is truncated
    _SPAWN_SHIM_CODE = ""

# Resource-limit profiles. ``tool`` is the default for every agent-influenced
# spawn; the others exist because a single policy cannot serve a one-shot tool, a
# build, a session host, and the user's own terminal at once. Each one is a
# faithful port of the ``preexec_fn`` variant it replaces -- including which ones
# bias the OOM killer, which is not uniform across them.
RLIMIT_PROFILE_TOOL = "tool"
RLIMIT_PROFILE_BUILD = "build"
RLIMIT_PROFILE_SESSION_HOST = "session_host"
# A first-party document parser fed untrusted bytes (``pdf_extract_child``). Its
# ceiling is FIXED, not read from ``resource_limits``: the ``tool`` profile only
# applies RLIMIT_AS when an operator sets ``max_memory_mb`` (default 0), and a
# parser whose allocation precedes any length check needs a memory bound that
# is on by default. RLIMIT_AS caps VIRTUAL address space, which is why ``tool``
# leaves it opt-in (Node/V8 reserves far more than it touches); this child is
# pure CPython plus ``pdfplumber``, measured at ~270 MB VmPeak on a one-page
# document, so 1 GiB is headroom for a large document and a hard stop for a
# Flate bomb. RLIMIT_CPU ends a parse that never finishes; NOFILE matches the
# ``tool`` default. Biases the OOM killer like ``tool``: this is the process to
# lose.
RLIMIT_PROFILE_EXTRACTOR = "extractor"
_EXTRACTOR_MAX_AS_BYTES = 1024 * 1024 * 1024
_EXTRACTOR_MAX_CPU_SECS = 60
_EXTRACTOR_MAX_NOFILE = 1024
# No limits and no OOM bias: the interactive terminal is the user's own shell,
# not agent-executed code, and never carried either.
RLIMIT_PROFILE_NONE = "none"

# profile -> (biases the OOM killer, legacy preexec accessor name)
_PROFILE_OOM_BIAS = {
    RLIMIT_PROFILE_TOOL: True,
    RLIMIT_PROFILE_BUILD: True,
    RLIMIT_PROFILE_EXTRACTOR: True,
    # session_host_preexec raises NOFILE and does nothing else -- notably it does
    # NOT bias the OOM score, and a trusted session host should not be the
    # preferred kill target.
    RLIMIT_PROFILE_SESSION_HOST: False,
    RLIMIT_PROFILE_NONE: False,
}

# The shim's own argv contract, mirrored here so a cached prefix can be extended
# for one spawn. Kept as literals rather than imported from the shim module: the
# shim is consumed as a source string captured at import time, never imported from
# the (agent-writable) package directory at spawn time.
_SHIM_ARGV_SEPARATOR = "--"
_SHIM_CHDIR_FD_FLAG = "--chdir-fd="
_SHIM_CTTY_FD_FLAG = "--ctty-fd="

_SHIM_ARGV_CACHE: dict[str, tuple[str, ...]] = {}
_SHIM_UNAVAILABLE_LOGGED = False


def _rlimit_spec(profile: str) -> str:
    """Build the shim's ``--rlimits=`` payload for *profile*.

    The values are policy numbers, not secrets, so argv (world-readable through
    ``ps``) is a fine channel for them.
    """
    from kiro_crew.security import resource_limit_spec

    if profile == RLIMIT_PROFILE_NONE:
        return ""
    if profile == RLIMIT_PROFILE_SESSION_HOST:
        # Faithful port of session_host_preexec: it touches NOFILE only, and
        # deliberately leaves NPROC/CPU/AS inherited. A session host multiplexes
        # pipe pairs for a whole tree of MCP servers, and the tool-grade 1024 cap
        # EMFILE-crashed it.
        return "RLIMIT_NOFILE:hard"
    if profile == RLIMIT_PROFILE_EXTRACTOR:
        # Fixed policy, independent of ``resource_limits``: see the constants.
        return (
            f"RLIMIT_AS:{_EXTRACTOR_MAX_AS_BYTES},"
            f"RLIMIT_CPU:{_EXTRACTOR_MAX_CPU_SECS},"
            f"RLIMIT_NOFILE:{_EXTRACTOR_MAX_NOFILE}"
        )

    cfg: dict | None = None
    try:
        from kiro_crew.config.loader import _raw_config

        cfg = _raw_config()
    except Exception:
        logger.debug("_rlimit_spec: config unavailable, using defaults")
    if profile == RLIMIT_PROFILE_BUILD:
        raw_limits = (cfg or {}).get("resource_limits")
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        # Same shared parse as the post-fork build path above, so the two
        # spellings of "raise the build NOFILE floor" cannot drift apart.
        from kiro_crew.config.loader import ResourceLimitsConfig

        configured = ResourceLimitsConfig.from_raw(raw_limits).max_open_files or 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        cfg = {**(cfg or {}), "resource_limits": limits}
    return ",".join(f"{name}:{value}" for name, value in resource_limit_spec(cfg))


def spawn_shim_argv(
    profile: str = RLIMIT_PROFILE_TOOL, *, ctty_fd: int | None = None
) -> tuple[str, ...]:
    """Return the argv prefix that applies *profile*'s policy AFTER ``exec``.

    Prepend it to a command and pass ``preexec_fn=None``; the shim replaces
    itself with the command, so PID, process group, inherited fds, and exit
    status all stay the command's own. See ``_spawn_exec_shim.py`` for why this
    cannot ride on ``preexec_fn``: that forks this multi-threaded gateway and runs
    Python in the child, where a wedged child blocks the spawning thread inside
    ``Popen`` and pins every fd it inherited.

    *ctty_fd* asks the shim to make the terminal on that inherited descriptor the
    child's controlling terminal, which is what lets Ctrl+C reach an interactive
    shell. It is a spawn-scoped request rather than part of a profile: the
    descriptor belongs to one PTY, and the same profile serves spawns with no
    terminal at all. Passing it also redirects the child's stdin, stdout and
    stderr onto that descriptor.

    Returns an empty tuple when there is nothing for a shim to do -- on Windows
    (no POSIX rlimits), for a profile that asks for nothing and no *ctty_fd*, or
    if the shim source could not be captured. An empty result on a profile that
    DOES carry policy means the caller must fall back to ``preexec_fn`` rather
    than drop it. A caller that asked for *ctty_fd* must NOT fall back that way:
    reintroducing the fork is the defect the request exists to avoid, so it spawns
    without the shim and accepts a shell with no controlling terminal.
    """
    global _SHIM_UNAVAILABLE_LOGGED
    if os.name != "posix":
        return ()
    key = profile if ctty_fd is None else f"{profile}|ctty={ctty_fd}"
    cached = _SHIM_ARGV_CACHE.get(key)
    if cached is not None:
        return cached
    if not _SPAWN_SHIM_CODE or not sys.executable:
        if not _SHIM_UNAVAILABLE_LOGGED:
            _SHIM_UNAVAILABLE_LOGGED = True
            logger.warning(
                "post-exec spawn shim unavailable (source_captured=%s, executable=%r); "
                "falling back to preexec_fn",
                bool(_SPAWN_SHIM_CODE),
                sys.executable,
            )
        return ()
    spec = _rlimit_spec(profile)
    bias = _PROFILE_OOM_BIAS.get(profile, True)
    if not spec and not bias and ctty_fd is None:
        # Nothing to do post-exec: skip the interpreter hop entirely rather than
        # pay ~10ms to exec a shim that would only exec again.
        _SHIM_ARGV_CACHE[key] = ()
        return ()
    argv = [sys.executable, "-I", "-S", "-c", _SPAWN_SHIM_CODE]
    if spec:
        argv.append(f"--rlimits={spec}")
    if bias:
        argv.append("--oom-bias")
    if ctty_fd is not None:
        argv.append(f"{_SHIM_CTTY_FD_FLAG}{ctty_fd}")
    argv.append(_SHIM_ARGV_SEPARATOR)
    resolved = tuple(argv)
    _SHIM_ARGV_CACHE[key] = resolved
    return resolved


def _shim_prefix_entering_fd(prefix: "tuple[str, ...]", descriptor: int) -> "tuple[str, ...]":
    """Return *prefix* with ``--chdir-fd`` inserted ahead of its argv separator.

    Copied rather than mutated: the prefix is cached per profile, while the
    descriptor belongs to a single spawn.
    """
    if not prefix or prefix[-1] != _SHIM_ARGV_SEPARATOR:
        raise RuntimeError("spawn shim prefix is missing its argv separator")
    return prefix[:-1] + (f"{_SHIM_CHDIR_FD_FLAG}{descriptor}", _SHIM_ARGV_SEPARATOR)


def _pass_fds_including(passed: Any, descriptor: int) -> "tuple[int, ...]":
    """Return *passed* with *descriptor* inherited, leaving its order alone.

    The shim can only ``fchdir`` a descriptor the child actually holds, and
    ``pass_fds`` is what carries it there: it exempts the fd from
    ``_close_open_fds`` and clears the ``O_CLOEXEC`` the binder opens with. Owned
    here rather than left to each caller so the flag and the inheritance cannot
    drift apart.
    """
    existing = tuple(passed or ())
    if descriptor in existing:
        return existing
    return existing + (descriptor,)


def _preexec_for_profile(profile: str) -> "Callable[[], None] | None":
    """Legacy ``preexec_fn`` for *profile*, used only when the shim is missing."""
    if profile == RLIMIT_PROFILE_NONE:
        return None
    if profile == RLIMIT_PROFILE_SESSION_HOST:
        return session_host_preexec()
    if profile == RLIMIT_PROFILE_BUILD:
        return build_resource_limit_preexec()
    if profile == RLIMIT_PROFILE_EXTRACTOR:
        return extractor_resource_limit_preexec()
    return resource_limit_preexec()


def _resolve_spawn_target(
    argv: "Sequence[str]", env: "Mapping[str, str] | None", cwd: Any = None
) -> str:
    """Resolve a bare command NAME against the child's ``PATH``.

    The shim ``execv``s without a PATH search, so a command given as a bare name
    has to be resolved by someone. It is resolved HERE, in the gateway, on
    purpose: the call sites that vet an executable (provider allowlists, binary
    trust checks) do it in the parent, and letting the shim run its own search
    would add a second resolution path that nothing vetted.

    Resolving here also keeps the contract call sites already depend on -- a
    command that is not on ``PATH`` raises ``FileNotFoundError`` from the spawn
    itself, exactly as ``Popen`` raised it when the child's ``execvpe`` failed.

    This touches the filesystem (``shutil.which`` stats each ``PATH`` entry), so
    the caller runs it in a worker thread: a stalled NFS/autofs ``PATH`` entry
    would otherwise block the event loop, which the child-side search never did.

    ``PATH`` comes from the child's own environment, matching ``Popen``'s
    ``os.get_exec_path(env)``. A relative ``PATH`` entry is resolved against the
    child's *cwd* for the same reason: that is where ``execvpe`` would have looked
    from, not where the gateway happens to be running.
    """
    name = argv[0]
    if os.sep in name or (os.altsep and os.altsep in name):
        # An explicit path: exec resolves it (against cwd when relative), and
        # stat-ing it here would only pre-empt a failure exec reports anyway.
        return name
    search_path = (env or os.environ).get("PATH") or os.defpath
    if cwd:
        base = os.fspath(cwd)
        search_path = os.pathsep.join(
            entry if os.path.isabs(entry) else os.path.join(base, entry)
            for entry in search_path.split(os.pathsep)
        )
    found = shutil.which(name, path=search_path)
    if not found:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    return found


def _pinned_spawn_path(
    env: "Mapping[str, str] | None", *, chdir_fd: int | None = None
) -> "dict[str, str]":
    """A copy of *env* whose ``PATH`` keeps only entries safe under a pinned cwd.

    For resolving a command when the child's working directory is pinned by
    descriptor. Two screens, cheapest first:

    * **Lexical** -- only absolute entries survive. A relative entry (``''``,
      ``.``, ``tools``) is resolved against the pinned directory, which is the
      one place the pin says not to trust by name.
    * **Identity** (when *chdir_fd* is given) -- an absolute entry that IS the
      pinned directory, or lives anywhere beneath it, is dropped too.
      ``PATH=/home/me/.kiro/crew/workspace/bin:/usr/bin`` passes the lexical
      screen unchanged, yet a binary planted behind such an entry wins the
      child's own later lookup the moment the shim has entered the pinned
      directory. Entries are compared by ``(st_dev, st_ino)`` ancestry walked
      over descriptors -- never by pathname -- so a symlink or other alias of
      the pinned directory cannot dodge the screen. A kept entry is emitted as
      the OPENED descriptor's own canonical path, never the caller's spelling:
      the child re-resolves its ``PATH`` strings later, so a spelling that
      traverses a retargetable symlink could be pointed somewhere else between
      this screen and that lookup. An entry that cannot be opened, walked, or
      re-spelled is dropped, fail-closed per entry: an unopenable entry cannot
      contribute a resolvable binary today, and dropping is the direction that
      cannot be gamed by making a directory un-``stat``-able.

    When the BOUND descriptor's own identity cannot be read there is nothing to
    compare entries against, so the lexical screen stands alone for that spawn.
    That is a deliberate degrade, not a silent fallback: in production
    ``chdir_fd`` always originates from a real opened directory descriptor, and
    one that cannot be ``fstat``-ed is one the shim's own ``fchdir`` rejects
    before any command runs.

    Dropping entries can leave ``PATH`` empty, and that is the intended outcome
    -- the resolve then raises ``FileNotFoundError`` exactly as an unresolvable
    command already did, rather than silently searching somewhere else.
    """
    source = dict(env if env is not None else os.environ)
    raw = source.get("PATH") or os.defpath
    entries = [entry for entry in raw.split(os.pathsep) if entry and os.path.isabs(entry)]
    bound_identity: "tuple[int, int] | None" = None
    if chdir_fd is not None:
        try:
            bound_info = os.fstat(chdir_fd)
        except OSError:
            bound_identity = None
        else:
            bound_identity = (bound_info.st_dev, bound_info.st_ino)
    if bound_identity is not None:
        # Local import: hooks imports sandbox at call time, so a module-level
        # dependency would be circular. `_fd_real_path` is private but already
        # borrowed this way by `bound_agent_workspace_target` above.
        from kiro_crew.hooks import _fd_real_path

        screened: list[str] = []
        for entry in entries:
            try:
                entry_fd = _open_directory_descriptor(entry)
            except OSError:
                continue
            try:
                ancestors = _directory_ancestor_identities(entry_fd)
                if bound_identity in ancestors:
                    # The walk yields the entry's OWN identity first, so one
                    # membership test covers both "the entry IS the pinned
                    # directory" and "the entry lives beneath it".
                    continue
                # Keep the OPENED descriptor's own canonical path, never the
                # caller's spelling. The child re-resolves whatever string ends
                # up in its PATH, so a kept spelling that traverses a symlink
                # could be retargeted between this screen and that lookup --
                # the identity verified here must be the identity the child
                # reaches. A canonical path has no symlink components, and one
                # inside the pinned directory cannot exist here (its target
                # would have failed the ancestry test above). Unresolvable ==
                # dropped: falling back to the mutable spelling would reopen
                # the window this screen exists to close.
                resolved_entry = _fd_real_path(entry_fd)
            except OSError:
                continue
            finally:
                os.close(entry_fd)
            if resolved_entry is None:
                continue
            screened.append(resolved_entry)
        entries = screened
    source["PATH"] = os.pathsep.join(entries)
    return source


def _needs_path_search(argv: "Sequence[str]") -> bool:
    """Whether ``argv[0]`` is a bare name, i.e. whether resolution touches disk."""
    name = argv[0]
    return not (os.sep in name or (os.altsep and os.altsep in name))


async def create_subprocess_limited(
    *argv: str,
    profile: str = RLIMIT_PROFILE_TOOL,
    chdir_fd: int | None = None,
    windows_cleanup_owner: platform_compat._PendingWindowsTreeCleanup | None = None,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` with resource limits applied post-exec.

    The drop-in replacement for ``create_subprocess_exec(..., preexec_fn=
    resource_limit_preexec())``. Every keyword argument is forwarded untouched
    except ``preexec_fn``, which this owns: passing one would reintroduce the fork
    hazard the shim exists to remove, so it is refused.

    The returned ``Process`` describes the command itself, not a wrapper -- the
    shim ``exec``s in place -- so ``pid``, ``returncode``, signal delivery, and
    ``platform_compat.kill_process_tree`` all behave as they did before.

    ``chdir_fd`` pins the child's working directory to a directory IDENTITY
    rather than to a name: the descriptor is inherited, the shim ``fchdir``s into
    it and closes it, and only then is the command exec'd. Callers pass it when a
    pathname re-resolved in the child could be retargeted between the check and
    the chdir. It is deliberately not spelled ``cwd="/dev/fd/<n>"`` -- that is a
    Linux-only trick, and macOS refuses ``chdir()`` on those entries (``EACCES`` or
    ``ENOTDIR`` depending on the OS version). It needs the shim, and is refused rather than quietly downgraded
    to ``cwd``'s pathname when the shim is missing: entering a name nobody
    re-verified would reopen the window the descriptor exists to close.

    Setting it also DROPS ``cwd`` from the spawn, since ``Popen`` would otherwise
    chdir that pathname in the fork child before the shim ever runs, and screens
    ``PATH`` by directory IDENTITY -- for the search that resolves a bare command
    name here AND for the child's own environment. Relative entries are dropped
    (``execvpe`` resolved them against the child's cwd, the directory this
    descriptor exists to distrust), and so is any absolute entry that is the
    pinned directory itself or lives beneath it, compared by ``(st_dev, st_ino)``
    ancestry rather than by pathname. Resolving ``argv[0]`` is not the last
    lookup that happens: the wrapper this spawns looks its own target up on
    ``PATH`` after the shim has entered that directory. ``PATH=.:/usr/bin`` --
    or the same directory spelled absolutely -- would otherwise exec a binary
    out of the agent's own workspace, ahead of the sandbox meant to contain it.

    A spawn landing while the launcher interpreter's install tree is being
    rebuilt is retried, on the same budget and by the same discriminator as
    :func:`popen_limited` -- this wrapper puts the same ``sys.executable`` at
    the head of the spawned argv, so it was exposed to the identical blip. The
    backoff uses ``asyncio.sleep``: a blocking sleep would freeze the event loop
    for up to ~3.75s, which is precisely the hazard this wrapper exists to
    avoid. Only the shim-prefixed spawn is retried -- the no-shim fallback below
    execs the caller's own ``argv[0]``, so an ENOENT there is the caller's own
    missing binary and must still surface on the first attempt.

    There is deliberately no ``abort_retry`` hook here, unlike
    :func:`popen_limited`: no caller mediates this wrapper's cancellation
    through a registry keyed on the live child, so the lost-cancel window that
    hook exists to close has no consumer. Add one when a caller needs it.
    """
    if "preexec_fn" in kwargs:
        raise TypeError(
            "create_subprocess_limited owns preexec_fn: limits are applied "
            "post-exec by the spawn shim, not post-fork"
        )
    if not argv:
        raise ValueError("create_subprocess_limited requires a command")
    prefix = spawn_shim_argv(profile)
    if not prefix:
        if chdir_fd is not None:
            raise RuntimeError(
                "a descriptor-pinned working directory requires the post-exec "
                "spawn shim; refusing to enter an unverified pathname instead"
            )
        # No shim (Windows, a no-op profile, or a truncated install): keep
        # whatever policy the profile carries on the legacy fork path. Dropping
        # the caps silently would be worse than the fork hazard.
        if platform_compat.IS_WINDOWS and windows_cleanup_owner is not None:
            return await platform_compat._create_windows_subprocess_owned(
                windows_cleanup_owner, *argv, preexec_fn=_preexec_for_profile(profile), **kwargs
            )
        return await asyncio.create_subprocess_exec(
            *argv, preexec_fn=_preexec_for_profile(profile), **kwargs
        )
    search_cwd = kwargs.get("cwd")
    search_env = kwargs.get("env")
    if chdir_fd is not None:
        prefix = _shim_prefix_entering_fd(prefix, chdir_fd)
        kwargs["pass_fds"] = _pass_fds_including(kwargs.get("pass_fds"), chdir_fd)
        # THE INVARIANT: while the cwd is pinned by descriptor, NO resolution of a
        # program name -- not the one below, and not one the child performs later --
        # may consult a relative PATH entry, the pinned directory, or anything
        # inside it. Three things enforce it together, and each was a hole on its
        # own:
        #
        # (a) `cwd` leaves the spawn. ``Popen`` chdirs it in the fork child BEFORE it
        #     execs the shim, so leaving it in place would resolve the very pathname
        #     the descriptor exists to bypass -- and fail the spawn outright
        #     (EACCES/ENOENT/ENOTDIR) if that name was removed or retargeted since the
        #     bind, with the pinned descriptor never reached.
        # (b) The search below gets no cwd and a PATH screened by directory IDENTITY:
        #     relative entries are dropped, and so is any absolute entry that IS the
        #     pinned directory or lives beneath it -- compared by (st_dev, st_ino)
        #     ancestry, so an alias cannot dodge it; kept entries are re-spelled from
        #     the verified descriptor, so a retargetable symlink in the caller's
        #     spelling cannot redirect the child's later lookup. A bare name IS the
        #     normal shape here -- the macOS sandbox wrapper hands back "env" as
        #     argv[0] and the Linux cgroup wrapper hands back "systemd-run" -- so the
        #     search cannot simply be refused, and `execvpe` resolved a relative entry
        #     against the child's cwd, i.e. the pinned workspace. An absolute entry
        #     pointing INTO that workspace reaches the same binary by a different
        #     spelling.
        # (c) The CHILD gets that same screened PATH. Resolving argv[0] here is
        #     not the last resolution that happens: `env` looks `sandbox-exec` up on
        #     PATH itself, inside the child, after the shim has already entered the
        #     workspace. Narrowing only (b) left `PATH=.:/usr/bin` exec'ing a
        #     `sandbox-exec` the agent dropped in its own workspace -- ahead of the
        #     sandbox that was supposed to contain it. One sanitized PATH, used for
        #     both, is what makes the invariant hold rather than move down a level.
        #
        # Those two wrapper names are spelled in prose on purpose: test_spawn_audit
        # matches its routed-through-the-sandbox tokens against this function's raw
        # source, comments included, so writing either identifier here would make the
        # spawn chokepoint read as if it routed on its own behalf.
        kwargs.pop("cwd", None)
        pinned_env = search_env

        def _screened_spawn_plan() -> "tuple[dict[str, str], str]":
            # One worker-thread hop covers the identity screen AND the resolve:
            # the screen opens and walks PATH entries and the resolve stats
            # them, so a stalled NFS/autofs entry would block either one, and
            # neither may freeze the event loop. Returning the screened env
            # alongside the resolved target keeps clauses (b) and (c) fed from
            # the SAME value by construction.
            screened = _pinned_spawn_path(pinned_env, chdir_fd=chdir_fd)
            if _needs_path_search(argv):
                return screened, _resolve_spawn_target(argv, screened, None)
            return screened, argv[0]

        search_env, resolved = await asyncio.to_thread(_screened_spawn_plan)
        kwargs["env"] = search_env
    elif not _needs_path_search(argv):
        # Explicit path: nothing to resolve, so no filesystem access and no
        # thread hop -- exec does the work.
        resolved = argv[0]
    else:
        # A PATH search stats every entry, so it runs off the loop. One stalled
        # NFS/autofs entry would otherwise freeze the gateway.
        resolved = await asyncio.to_thread(_resolve_spawn_target, argv, search_env, search_cwd)
    for delay in _INTERPRETER_ENOENT_DELAYS:
        try:
            return await asyncio.create_subprocess_exec(
                *prefix, resolved, *argv[1:], preexec_fn=None, **kwargs
            )
        except FileNotFoundError as exc:
            # ``prefix`` IS the head of the argv actually spawned, and prefix[0]
            # is this process's own sys.executable -- the only shape the
            # discriminator accepts.
            if not _retry_interpreter_enoent(exc, prefix, delay):
                raise
            # asyncio.sleep, NOT time.sleep: a blocking sleep here would freeze
            # the event loop for up to ~3.75s, which is the hazard this wrapper
            # exists to avoid.
            await asyncio.sleep(delay)
    # Budget spent. Deliberately unguarded, exactly as in popen_limited: a
    # genuinely broken install reports the error it reports today, ~4s later.
    return await asyncio.create_subprocess_exec(
        *prefix, resolved, *argv[1:], preexec_fn=None, **kwargs
    )


def _prepare_limited_spawn(
    argv: "Sequence[str]", profile: str, kwargs: "dict[str, Any]", caller: str
) -> "tuple[list[str], Callable[[], None] | None]":
    """Resolve *argv* into the command to spawn plus the ``preexec_fn`` to pass.

    Shared by :func:`run_limited` and :func:`popen_limited`, which differ only in
    which ``subprocess`` entry point they hand the result to.

    Two things make this the sync twin of :func:`create_subprocess_limited`
    rather than a copy of it:

    * The PATH search runs INLINE. The async wrapper hops to a worker thread
      because ``shutil.which`` stats every ``PATH`` entry and one stalled
      NFS/autofs mount would freeze the event loop. A synchronous caller is
      already off the loop, so the hop would buy nothing and cost a thread.
    * ``shell=True`` is refused. The shim ``exec``s an argv vector, so there is
      no correct place to put a prefix in front of a shell command STRING;
      wrapping it anyway would change what the shell parses.
    """
    if "preexec_fn" in kwargs:
        raise TypeError(
            f"{caller} owns preexec_fn: limits are applied post-exec by the "
            "spawn shim, not post-fork"
        )
    if kwargs.get("shell"):
        raise TypeError(
            f"{caller} cannot wrap shell=True: the shim prefixes an argv "
            "vector, and a shell command is a single string"
        )
    if not argv:
        raise ValueError(f"{caller} requires a command")
    prefix = spawn_shim_argv(profile)
    if not prefix:
        # No shim (Windows, a no-op profile, or a truncated install): keep
        # whatever policy the profile carries on the legacy fork path. Dropping
        # the caps silently would be worse than the fork hazard.
        return list(argv), _preexec_for_profile(profile)
    if not _needs_path_search(argv):
        # Explicit path: exec resolves it, so stat-ing it here would only
        # pre-empt a failure exec reports anyway.
        resolved = argv[0]
    else:
        resolved = _resolve_spawn_target(argv, kwargs.get("env"), kwargs.get("cwd"))
    return [*prefix, resolved, *argv[1:]], None


def run_limited(
    argv: "Sequence[str]",
    *,
    profile: str = RLIMIT_PROFILE_TOOL,
    **kwargs: "Any",
) -> "subprocess.CompletedProcess[Any]":
    """``subprocess.run`` with resource limits applied AFTER ``exec``.

    The synchronous counterpart of :func:`create_subprocess_limited`, and the
    drop-in replacement for ``subprocess.run(..., preexec_fn=
    resource_limit_preexec())``. Every keyword argument is forwarded untouched
    except ``preexec_fn``, which this owns.

    A synchronous spawn wedges the calling worker thread rather than the event
    loop, so it does not take the whole gateway down the way the async hazard
    did -- but it is the same ``fork()`` of the same multi-GB, ~118-thread
    process, and the child still inherits a duplicate of every open fd until it
    ``exec``s. Taking ``preexec_fn`` out of the picture removes both.

    ``CompletedProcess.args`` and the ``cmd`` of a ``CalledProcessError`` /
    ``TimeoutExpired`` are the command's own argv, not the shim's. That is
    maintained here rather than free: the shim source rides in argv as a ~8 KB
    ``-c`` string, and both exceptions render ``cmd`` into their message, so
    reporting the spawned argv would put the whole shim in every failure log
    line.

    A spawn landing while the launcher interpreter's install tree is being
    rebuilt is retried, on the same budget and by the same discriminator as
    :func:`popen_limited` -- this wrapper puts the same ``sys.executable`` at
    ``cmd[0]``, so it was exposed to the identical blip. The retry sits INSIDE
    the ``cmd``-rewriting handler so a ``CalledProcessError`` or
    ``TimeoutExpired`` still reports the caller's own argv.

    There is deliberately no ``abort_retry`` hook here, unlike
    :func:`popen_limited`: this wrapper never hands back a handle, so no
    cancellation registry can be keyed on the child and the lost-cancel hazard
    that hook exists to close cannot arise. Add one when a caller needs it.
    """
    cmd, preexec = _prepare_limited_spawn(argv, profile, kwargs, "run_limited")
    reported = list(argv)
    try:
        for delay in _INTERPRETER_ENOENT_DELAYS:
            try:
                result = subprocess.run(cmd, preexec_fn=preexec, **kwargs)
                break
            except FileNotFoundError as exc:
                if not _retry_interpreter_enoent(exc, cmd, delay):
                    raise
                time.sleep(delay)
        else:
            # Budget spent. Deliberately unguarded, exactly as in
            # popen_limited: whatever this raises reaches the caller unchanged,
            # so a genuinely broken install still reports the error it reports
            # today -- just ~4s later.
            result = subprocess.run(cmd, preexec_fn=preexec, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        exc.cmd = reported
        raise
    result.args = reported
    return result


#: Backoff between spawn attempts when the launcher interpreter is transiently
#: absent. Five attempts, ~3.75s of waiting in total.
#:
#: ``sys.executable`` is often a symlink into a managed install tree, and
#: rebuilding that tree DELETES and re-creates its entries -- including the
#: interpreter :func:`wrap_argv` prepends to EVERY sandboxed argv. The tree is
#: whole again in about a second, so a spawn landing inside that window dies with
#: ENOENT on an interpreter that both existed before it and exists after it. The
#: caller cannot tell that apart from a broken install: a cron records a hard
#: failure (and counts a strike toward auto-pause) for a condition that already
#: healed itself, and several crons sharing one tick fail together. Any packaging
#: that relinks an interpreter in place reaches this -- an environment rebuild, a
#: toolchain reinstall, a swapped container layer.
_INTERPRETER_ENOENT_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0)


def _is_transient_interpreter_enoent(exc: OSError, cmd: "Sequence[str]") -> bool:
    """True when ``exc`` is ENOENT for the interpreter WE prepended to ``cmd``.

    Deliberately narrow, because ENOENT from ``Popen`` is ambiguous: it is raised
    for a missing ``cwd`` and for a missing program alike, and a genuinely absent
    user binary MUST still fail on the first attempt rather than after a delay.
    So the only retryable shape is an ENOENT whose ``filename`` IS ``cmd[0]`` and
    whose ``cmd[0]`` is this process's own ``sys.executable``. A missing-``cwd``
    ENOENT names the directory and a missing user binary names that binary, so
    both fall through untouched.

    ``filename`` being populated is not guaranteed, so when it is absent fall
    back to a live check that the interpreter really is gone from disk -- an
    observation, rather than an assumption that this ENOENT must be ours.
    """
    if not cmd or cmd[0] != sys.executable:
        return False
    if exc.filename is not None:
        return exc.filename == cmd[0]
    return not os.path.exists(sys.executable)


def _retry_interpreter_enoent(exc: OSError, cmd: "Sequence[str]", delay: float) -> bool:
    """Decide whether *exc* is a retryable interpreter blip, and log it if so.

    The single shared implementation behind all three spawn wrappers
    (:func:`run_limited`, :func:`popen_limited`,
    :func:`create_subprocess_limited`), which all put ``sys.executable`` at
    ``cmd[0]`` via :func:`spawn_shim_argv` and are therefore exposed to the same
    transient ENOENT. Returning ``False`` means the caller must re-raise
    untouched.

    What is deliberately NOT shared is the spawn itself, and the WAIT. The spawn
    stays lexically inside each wrapper because both spawn audits key on
    ``<relpath>::<enclosing function>``: hoisting any of the three into a common
    helper would migrate its audit key, stranding the existing ``_SYNC_ALLOWED``
    and ``BENIGN_SPAWNS`` entries as stale while the relocated call read as a
    brand-new unrouted spawn. The wait is per-flavour because the async wrapper
    must ``await asyncio.sleep`` -- a ``time.sleep`` there would block the event
    loop for up to ~3.75s, which is the very hazard the async wrapper exists to
    avoid. So each caller keeps its own two lines (wait, then re-check abort) and
    shares the DECISION, which is where the subtlety actually lives.
    """
    if not _is_transient_interpreter_enoent(exc, cmd):
        return False
    # Log every retry: a silently-absorbed spawn failure would hide an install
    # tree that has genuinely stopped converging.
    logger.warning(
        "sandbox launcher interpreter %r is absent; retrying spawn in "
        "%.2fs (its install tree is probably mid-rebuild)",
        sys.executable,
        delay,
    )
    return True


def popen_limited(
    argv: "Sequence[str]",
    *,
    profile: str = RLIMIT_PROFILE_TOOL,
    abort_retry: "Callable[[], bool] | None" = None,
    **kwargs: "Any",
) -> "subprocess.Popen[Any]":
    """``subprocess.Popen`` with resource limits applied AFTER ``exec``.

    Same contract as :func:`run_limited`, for callers that need the handle
    rather than the result -- a long-running child they will ``communicate()``
    with, poll, or signal later.

    The returned ``Popen`` is the command's own process, not a wrapper's, so
    ``pid``, ``returncode``, signal delivery, and
    ``platform_compat.kill_process_tree`` behave as they did before.

    ``Popen.args`` is reset to the command's own argv for the same reason
    :func:`run_limited` rewrites ``cmd``: ``communicate(timeout=...)`` builds its
    ``TimeoutExpired`` from ``self.args``, so leaving the shim there would put
    ~8 KB of shim source into the timeout message. Nothing in CPython reads
    ``self.args`` functionally -- only ``__repr__`` and that exception.

    A spawn that lands while the launcher interpreter's install tree is being
    rebuilt is retried rather than surfaced -- see
    :data:`_INTERPRETER_ENOENT_DELAYS`. The retry loop is INLINE rather than
    extracted into a helper on purpose: both spawn audits key on
    ``<relpath>::<enclosing function>``, so moving this ``Popen`` into its own
    function would migrate its key, stranding the ``popen_limited`` entries in
    ``_SYNC_ALLOWED`` and ``BENIGN_SPAWNS`` as stale while the relocated call read
    as a brand-new unrouted spawn.

    ``abort_retry`` is consulted after each backoff, and matters only to a caller
    whose cancellation is mediated by a REGISTRY keyed on the live child -- for
    those, the backoff is a window in which a cancel is silently LOST rather than
    merely delayed, because the canceller finds no registered child and records
    nothing, and the retry then launches work the caller already cancelled.
    Returning ``True`` re-raises the ENOENT instead of spawning, so the caller's
    own cancellation path reports the run rather than running it. A caller that
    holds the ``Popen`` handle itself and polls a stop flag (such as
    ``auto_improvement.spine.agent_runner``, which calls ``_terminate_group`` on
    the handle) loses nothing by omitting it: the stop is observed after the
    spawn returns and the child is signalled then.
    """
    cmd, preexec = _prepare_limited_spawn(argv, profile, kwargs, "popen_limited")
    for delay in _INTERPRETER_ENOENT_DELAYS:
        try:
            proc = subprocess.Popen(cmd, preexec_fn=preexec, **kwargs)
            break
        except FileNotFoundError as exc:
            if not _retry_interpreter_enoent(exc, cmd, delay):
                raise
            time.sleep(delay)
            # Checked AFTER the sleep, because that is when a cancellation
            # racing the backoff will have landed. Spawning now would run work
            # the caller has already cancelled, and the exit status would not
            # say so.
            if abort_retry is not None and abort_retry():
                raise
    else:
        # Budget spent. Deliberately unguarded: whatever this raises reaches the
        # caller unchanged, so a genuinely broken install still reports exactly
        # the error it reports today -- just ~4s later.
        proc = subprocess.Popen(cmd, preexec_fn=preexec, **kwargs)
    proc.args = list(argv)
    return proc
