"""Pure filesystem path primitives for KiroCrew configuration.

This is a **leaf module**: it depends only on the standard library
(``os``, ``sys``, ``pathlib``, ``logging``) and imports nothing from
``kiro_crew`` at import time. The one exception is a lazy, in-function import
of :mod:`kiro_crew.atomic_write` inside ``_write_recovery_breadcrumb`` (that
helper itself imports this module lazily, so there is no cycle). Modules that
only need to locate ``~/.kirocrew/`` should import
from here directly::

    from kiro_crew.config.paths import config_dir

so they don't transitively pull in the full config loader (DTOs, schema
validation, the process-global cache, and the lazily-imported provider
factory) the way ``from kiro_crew.config.loader import config_dir`` does.

Only the genuinely pure primitives live here. The *dir-derived* helpers
(``config_path``, ``config_local_path``, ``workspace_root``, ``workspace_dir_for``,
``outbox_dir``, ``env_path``, …) remain in :mod:`kiro_crew.config.loader` so that
their ``config_dir()`` lookups resolve in the loader namespace — preserving the
``patch("kiro_crew.config.loader.config_dir", ...)`` test seam used across the
suite.

All names here are also re-exported from ``kiro_crew.config.loader`` for
backward compatibility, so existing callers continue to work unchanged.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# KiroCrew's data root nests UNDER kiro-cli's own home ``~/.kiro/`` (Labs product
# decision: all Kiro-family apps share the ``~/.kiro/`` base so a user has a
# single place to secure). ``config_dir()`` therefore resolves to
# ``~/.kiro/crew`` by default. ``CONFIG_DIR_NAME`` is the segment(s) appended to
# ``~/`` — kept as a POSIX-style relative literal so downstream string checks
# (e.g. the security keystone) can match it uniformly.
KIRO_BASE_DIR_NAME = ".kiro"
CONFIG_DIR_LEAF = "crew"
CONFIG_DIR_NAME = f"{KIRO_BASE_DIR_NAME}/{CONFIG_DIR_LEAF}"  # ".kiro/crew"

# The pre-move top-level home (``~/.kirocrew``). Retained as a constant (not an
# inline literal) so the security keystone, autonudge, seed and the other
# legacy-path consumers reference the same source of truth.
LEGACY_CONFIG_DIR_NAME = ".kirocrew"

# Recovery-pointer breadcrumb written at the TOP-LEVEL home (``~/.kirocrew.breadcrumb``),
# deliberately OUTSIDE ``~/.kiro/``. The data home now nests under kiro-cli's
# ``~/.kiro/`` base, so a hypothetical Kiro-family uninstaller that wipes
# ``~/.kiro/`` would take Kiro Crew's data with it. This tiny, non-secret
# pointer survives such a wipe (it lives beside ``~/.kiro``, not inside it) and
# records where the data
# home is, so a user/support script can find any surviving data or understand
# what was lost. It is NOT a backup — just a durable signpost. Only written on
# the default (non-override) path; a ``KIROCREW_HOME`` override is the user's own chosen
# location and carries no ``~/.kiro/`` wipe risk.
RECOVERY_BREADCRUMB_NAME = ".kirocrew.breadcrumb"

OUTBOX_DIR_NAME = "outbox"

# Cross-platform workspace root for LLM working directories.
# Override: KIROCREW_WORKSPACE env var or <config_dir>/workspace_dir
# macOS: /Volumes/workplace/kirocrew-workspace (fallback ~/workplace)
# Linux: ~/workplace/kirocrew-workspace
_WORKSPACE_DIR_NAME = "kirocrew-workspace"

# Once-per-process cache of the RESOLVED default data home so config_dir()
# resolves it once and every later call returns the same directory with no extra
# filesystem probing. ``None`` means "not yet resolved this process".
_resolved_home: Path | None = None

# Memo for ``config_dir()``: ``(raw KIROCREW_HOME, the home the entry was built
# from, result)``. ``config_dir()`` is called from 323 sites and each uncached
# call does a ``Path.resolve()`` + ``mkdir`` and, on the default path, a
# breadcrumb read/write — measured 94.9us per call. Keying on the RAW env value
# keeps the override honoured the moment it changes (``KIROCREW_HOME`` is
# repointed per test by the suite's isolation fixture, and by pods/worktrees at
# runtime), and keying on the resolved home by identity ties the default-path
# entry to the resolution cache above — so clearing ``_resolved_home`` (which
# the test suite does per test) invalidates this memo too instead of pinning a
# stale home. For that invalidation to hold, the default path stores the home it
# RETURNED rather than a re-read of the global; see ``config_dir``. In a real
# process both keys are stable after the first call, which is what makes the
# breadcrumb write effectively once-per-process rather than once-per-call.
_config_dir_memo: tuple[str | None, Path | None, Path] | None = None


def _default_home() -> Path:
    """Resolve the default (non-override) data root: ``~/.kiro/crew``."""
    return Path.home() / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF


def _legacy_home() -> Path:
    """Resolve the pre-move top-level home: ``~/.kirocrew``."""
    return Path.home() / LEGACY_CONFIG_DIR_NAME


def legacy_home() -> Path:
    """Public alias for the pre-move top-level home (``~/.kirocrew``).

    Exported so modules that legitimately need to recognise a legacy-rooted
    path — e.g. ``autonudge.repair_sentinel_path`` re-homing a persisted
    kill-switch path — can do so without reaching into the private
    ``_legacy_home``.
    """
    return _legacy_home()


def _resolve_default_home() -> Path:
    """Resolve the default data root (``~/.kiro/crew``), caching it for the process.

    The resolved home is memoized in :data:`_resolved_home` so every later
    ``config_dir()`` / ``data_home()`` call returns the same directory without
    re-probing the filesystem. ``None`` in the cache means "not yet resolved this
    process".
    """
    global _resolved_home
    if _resolved_home is None:
        _resolved_home = _default_home()
    return _resolved_home


def _write_recovery_breadcrumb(data_home: Path) -> None:
    """Drop a recovery-pointer breadcrumb at ``~/.kirocrew.breadcrumb`` (best effort).

    Lives OUTSIDE ``~/.kiro/`` so it survives a ``~/.kiro/``-wide uninstaller wipe
    and records where the data home is (see ``RECOVERY_BREADCRUMB_NAME``). Written
    once (skipped if already present and already points at *data_home*), never
    raises, never blocks startup, and contains NO secrets — only the path. Only
    called on the default (non-override) resolution path.
    """
    try:
        crumb = Path.home() / RECOVERY_BREADCRUMB_NAME
        content = (
            "KiroCrew data-home location pointer (safe to delete).\n"
            "\n"
            "KiroCrew stores its data (config, credentials, history, DBs) at:\n"
            f"    {data_home}\n"
            "\n"
            "This pointer lives outside ~/.kiro/ on purpose: if a Kiro-family\n"
            "uninstaller ever removes ~/.kiro/, this file survives so you can find\n"
            "any surviving data or know where it had been. It is NOT a backup.\n"
        )
        # Idempotent: only (re)write when absent or the recorded path changed, so
        # we don't churn the file on every process start. Read through a
        # no-follow descriptor and confirm it is a regular file, so a planted
        # symlink or FIFO can neither redirect the read nor hang or bloat
        # startup (``read_text`` would follow it). Where O_NOFOLLOW does not
        # exist (Windows), skip the read entirely - an open there would follow
        # a symlink (e.g. to an unreachable UNC path, stalling startup) - and
        # fall through to the atomic rewrite.
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if no_follow:
            try:
                read_flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
                crumb_fd = os.open(crumb, read_flags)
                try:
                    if stat.S_ISREG(os.fstat(crumb_fd).st_mode):
                        existing = os.read(crumb_fd, 65536).decode("utf-8", errors="replace")
                        if str(data_home) in existing:
                            return
                finally:
                    os.close(crumb_fd)
            except OSError:
                pass

        # Write via the repo's mandated atomic-write helper: unique temp file,
        # fchmod on the descriptor (never a path-based chmod), rename with the
        # Windows sharing-violation retry, cleanup on failure. The rename
        # replaces whatever directory entry sits at the breadcrumb path without
        # following it, so a planted symlink is consumed - never written
        # through - and its target is untouched. Imported lazily: this module
        # stays an import-time leaf (see module docstring).
        from kiro_crew.atomic_write import atomic_write

        atomic_write(crumb, content, mode=0o600)
    except OSError:  # pragma: no cover - defensive: a breadcrumb is best-effort
        logger.debug("could not write recovery breadcrumb", exc_info=True)


# System directory trees no resolved home may live under, matched on the first
# two path components.
_UNSAFE_HOME_PREFIXES = frozenset(
    {
        ("/", "usr"),
        ("/", "System"),
        ("/", "etc"),
    }
)

# macOS resolves ``/etc`` to ``/private/etc``, so an override spelled ``/etc``
# reaches the guard already resolved and would otherwise miss the check above.
#
# Matched as a THREE-component prefix, so the whole ``/private/etc`` TREE is
# refused — not just the bare directory. ``("/", "etc")`` above is a prefix on
# Linux and refuses ``/etc/anything``; an exact match here would have left
# ``KIROCREW_HOME=/etc/kirocrew`` accepted on macOS only, so the two platforms
# would disagree about the same path.
#
# Deliberately scoped to ``/private/etc`` rather than all of ``/private``:
# ``tempfile.gettempdir()`` resolves under ``/private/var/folders/<...>/T`` on
# macOS, so refusing that tree would reject every legitimate temp-dir data home
# that tests, pods and worktree previews rely on.
_UNSAFE_RESOLVED_PREFIXES = frozenset(
    {
        ("/", "private", "etc"),
    }
)


def _is_unsafe_home(p: Path) -> bool:
    """Whether *p* is too dangerous to use as a resolved home directory.

    ``p == p.parent`` is the portable "is a root" test: a filesystem root or
    Windows drive root is its own parent (``/`` -> ``/``, ``C:\\`` -> ``C:\\``),
    so this refuses a bare "/" on every OS (not just POSIX).

    Both system-directory checks are PREFIX matches, so a system directory and
    everything under it are refused together. That symmetry is the point: the
    ``("/", "etc")`` entry already refuses ``/etc/anything`` on Linux, so the
    macOS-resolved form has to cover ``/private/etc/anything`` too, or the same
    override would be rejected on one platform and accepted on the other.

    The macOS case exists because callers pass an already-``resolve()``d path:
    ``KIRO_HOME=/etc`` arrives here as ``/private/etc``, whose ``parts[:2]`` is
    ``("/", "private")``. A check that knew only the unresolved spelling let it
    through, and KiroCrew would then create agent JSON inside ``/etc``.

    Deliberately NOT refusing the whole ``/private`` tree: on macOS
    ``tempfile.gettempdir()`` resolves under ``/private/var/folders/...``, so a
    prefix match there would reject every temp-dir data home — which tests, pods
    and worktree previews legitimately use.

    Shared by :func:`_valid_override_home` (``KIROCREW_HOME``) and
    :func:`kiro_home` (``KIRO_HOME``) so both overrides refuse the same targets.
    """
    if p == p.parent:
        return True
    if p.parts[:2] in _UNSAFE_HOME_PREFIXES:
        return True
    return p.parts[:3] in _UNSAFE_RESOLVED_PREFIXES


def _valid_override_home() -> Path | None:
    """Return the resolved ``KIROCREW_HOME`` override iff it is set AND valid.

    A filesystem/drive root (``/`` on POSIX, ``C:\\`` on Windows) or a known
    POSIX system directory (``/usr``, ``/System``, ``/etc``) is refused —
    ``config_dir()`` ignores it and falls back to the default home. Shared by
    ``config_dir()`` and ``data_home()`` so both agree on when an override is
    actually selected.
    """
    override = os.environ.get("KIROCREW_HOME")
    if not override:
        return None
    p = Path(override).expanduser().resolve()
    if _is_unsafe_home(p):
        return None
    return p


def shared_kiro_settings_writable() -> bool:
    """False when this process must not write the user's kiro-cli settings.

    ``~/.kiro/settings/mcp.json`` belongs to the kiro-cli installation, not to a
    Kiro Crew data home, so it is resolved from the real home and a throwaway
    instance shares it with the operator's live one. A pod is throwaway by
    construction: its home is empty, so any decision it reaches about which MCP
    servers should exist is a decision about a different install. Writing that
    decision to the shared file disarms the live instance -- a pod boots with no
    browser-mode marker, concludes browsing is off, and deletes the operator's
    browse entry.

    Keyed on ``KIROCREW_POD`` rather than on the presence of a data-home override,
    because a custom ``KIROCREW_HOME`` is a normal single-instance install (the
    desktop build uses one) and must keep its own registration working. Only an
    instance that declares itself a pod is refused.

    Reads stay allowed. Only the write side is refused, so a pod can still report
    what it sees.
    """
    return not os.environ.get("KIROCREW_POD")


def config_dir() -> Path:
    global _config_dir_memo
    override_raw = os.environ.get("KIROCREW_HOME")
    memo = _config_dir_memo
    if memo is not None and memo[0] == override_raw and memo[1] is _resolved_home:
        return memo[2]
    p = _valid_override_home()
    if p is not None:
        p.mkdir(parents=True, exist_ok=True)
        _config_dir_memo = (override_raw, _resolved_home, p)
        return p
    if os.environ.get("KIROCREW_HOME"):
        logger.warning(
            "KIROCREW_HOME=%s is a system directory, ignoring",
            os.environ.get("KIROCREW_HOME"),
        )
    d = _resolve_default_home()
    d.mkdir(parents=True, exist_ok=True)
    # Drop the recovery-pointer breadcrumb outside ~/.kiro/ (default path only).
    # Best-effort + idempotent; guarded so a breadcrumb failure never blocks the
    # data-home resolution the whole app depends on.
    _write_recovery_breadcrumb(d)
    # Key on ``d``, the home this call actually resolved, NOT on a re-read of
    # ``_resolved_home``. The two normally hold the same object, but not always:
    # the global is written only by ``_resolve_default_home``, so a resolution
    # that bypasses it (a stubbed resolver in a test, or a reset of the global
    # landing between the resolve above and this line) leaves the global ``None``
    # while ``d`` is a real path. Storing that ``None`` as the key records an
    # entry whose key is satisfied by every later "no override, home not yet
    # resolved" call — the state the suite's isolation fixture recreates per
    # test — so the unrelated ``d`` would be served as if freshly resolved.
    # Keying on ``d`` keeps the invariant above literal: the entry is live only
    # while the resolution cache still holds the same home.
    _config_dir_memo = (override_raw, d, d)
    return d


def data_home() -> Path:
    """The resolved data home, WITHOUT re-running start-of-process maintenance.

    :func:`config_dir` is *resolve + maintain*: besides resolving the home it
    also ``mkdir``s it and refreshes the recovery breadcrumb (a stat + a read).
    That work belongs to process start -- :func:`ensure_data_home` is the startup
    hook -- not to every caller that merely needs a path. While callers bound the
    result to a module constant at import the distinction did not matter;
    resolving per call makes it load-bearing, because a request handler would
    otherwise refresh the breadcrumb on the event loop as a side effect of asking
    where a directory is.

    Use this from any hot or async path. Three cases, in order:

    1. A **valid** ``KIROCREW_HOME`` override -> delegate to :func:`config_dir`
       on every call, so an override set *after* this module was imported is
       still honoured. That branch does not refresh the breadcrumb -- only a
       cheap ``mkdir`` -- so it is already safe.

       The test is :func:`_valid_override_home`, i.e. the SAME predicate
       :func:`config_dir` gates on -- not merely "is the env var set". An
       override naming a system directory is *rejected* there and resolution
       falls through to the default home, so gating on the raw env var would
       send every call down the maintenance path per request. The two predicates
       must not drift apart.
    2. Default home already resolved -> return the cached value directly. No
       ``mkdir``, no breadcrumb.
    3. Not yet resolved -> delegate to :func:`config_dir`, so the FIRST
       resolution in a process still creates the home and refreshes the
       breadcrumb exactly once, per *start*.

    Deliberately NOT a cache of its own: case 1 must stay live, and case 2 reads
    the same ``_resolved_home`` that :func:`config_dir` populates, so there is
    one source of truth for where the home is.
    """
    if _valid_override_home() is not None:
        return config_dir()
    if _resolved_home is not None:
        return _resolved_home
    return config_dir()


def peek_data_home() -> Path:
    """Where the data home IS, without creating or maintaining it.

    :func:`config_dir` and :func:`data_home` both create the home on first
    resolution (and refresh the recovery breadcrumb). A caller that only wants
    to know whether a file *would* be there -- an import-time cache load, a
    read-only inspector -- must not turn "import the package" into "mkdir
    ``~/.kiro/crew``": a test collector imports before any isolation runs, and a
    tool that reports state should not create it. Applies the SAME override
    predicate :func:`config_dir` gates on, so a valid ``KIROCREW_HOME`` and the
    default home agree between reader and writer, and reads nothing else.
    """
    override = _valid_override_home()
    if override is not None:
        return override
    return _resolve_default_home()


def private_runtime_log_dir() -> Path | None:
    """Diagnostics routing only; this NEVER grants session or memory authority.

    The namespace publishes a readonly marker and an execution-scoped log mount
    before Python starts, so early configuration diagnostics do not race the
    gateway's later PID publication. Seatbelt receives a path hint but confines
    writes to that exact directory independently of the hint.
    """
    home = config_dir()
    if sys.platform == "linux":
        from kiro_crew.platform_compat import is_readonly_filesystem

        if not is_readonly_filesystem(home):
            return None
        marker = home / ".private-member-runtime"
        try:
            if not marker.is_symlink() and marker.stat().st_mode & 0o222 == 0:
                with marker.open("rb") as handle:
                    if handle.read(2) == b"1":
                        return home / "agent-logs"
        except OSError:
            pass
    elif sys.platform == "darwin":
        hint = os.environ.get("_KIROCREW_PRIVATE_LOG_DIRECTORY", "")
        if hint:
            path = Path(hint)
            # circular import: this module is a leaf (see the module docstring) and must
            # not import from ``kiro_crew`` at import time.
            from kiro_crew.memory_stores import EXECUTION_LOGS_DIR_NAME, MEMORY_STORES_DIR_NAME

            if path.parent == home / MEMORY_STORES_DIR_NAME / EXECUTION_LOGS_DIR_NAME and (
                path.name.startswith("member-")
            ):
                return path
    return None


def ensure_data_home() -> Path:
    """Eagerly resolve and create the data home — call BEFORE the loop.

    ``config_dir()`` resolves the data home lazily on its first call and also
    ``mkdir``s it and refreshes the recovery breadcrumb. Every real entrypoint
    calls this ONCE from its synchronous prologue, before ``asyncio.run``, so the
    resolution happens on the main thread and every later on-loop ``config_dir()``
    is a cheap cached lookup. Idempotent (the process-lifetime cache makes a
    second call a no-op) and safe to call unconditionally. Returns the resolved
    data home.

    Also the one place the data home itself is tightened to owner-only, which
    makes the guarantee a property of establishing the home rather than of one
    subsystem happening to write there. The alternative — leaning on
    ``VectorMemoryStore.init``'s ``make_owner_only_dir(db_path.parent)`` — only
    reaches the data home while the DEFAULT store's ``memory.db`` sits directly
    in it, so a home whose crews all use NAMED memory stores would never be
    tightened at all. The per-store call still tightens its own directory, which
    is what covers the files SQLite creates inside it.

    Best-effort: ``restrict_dir_to_owner`` is fail-loud by contract, and a home
    that could not be tightened must still be usable — a permission warning is
    the right outcome, an unbootable gateway is not.
    """
    home = config_dir()
    if (
        sys.platform == "linux"
        and private_runtime_log_dir() is not None
        and home.stat().st_mode & 0o777 == 0o700
    ):
        # The private launcher already established the home. Its namespace view
        # is readonly; attempting chmod there produces a false security warning.
        return home
    try:
        from kiro_crew.platform_compat import restrict_dir_to_owner

        restrict_dir_to_owner(home)
    except OSError:
        logger.warning(
            "Cannot restrict the data home to owner-only; it may be readable by other users",
            exc_info=True,
        )
    return home


def config_package_dir() -> Path:
    """Return the installed ``kiro_crew/config/`` directory.

    This is the source of truth for bundled config data files (``defaults.json``,
    ``prompt.md``, persona/orchestrator prompts). ``paths.py`` lives directly in
    the config package, so this is simply its parent directory.
    """
    return Path(__file__).resolve().parent


def _in_ephemeral_tree(path: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether *path* lives inside an AppImage's ephemeral runtime mount.

    An AppImage runs from a squashfs the runtime mounts under a randomized
    ``/tmp/.mount_<name>XXXXXX`` directory and unmounts on exit, so anything
    resolved there is valid ONLY for the life of that process. A machine-wide
    launcher aimed into it dangles the moment the app quits — the same hazard as
    :func:`_in_linked_git_worktree`, from a different direction.

    ``$APPDIR`` (the mount point) is exported by the AppImage runtime and is the
    authoritative signal; ``$APPIMAGE`` names the outer image file rather than the
    mount, so it cannot answer an ancestry test. The ``.mount_`` path component is
    the fallback for a child process that inherited no environment, matched on the
    RESOLVED path so a symlink into the mount cannot slip past.

    Deliberately NOT "anything under the temp directory". A scratch tree in
    ``/tmp`` is every bit as ephemeral, but a blanket temp-dir rule cannot tell a
    reaped work directory from a legitimate install a developer or test placed
    there, and the launcher those produce is caught precisely by
    :func:`_bin_is_usable` instead — by the interpreter being gone, which is the
    property that actually breaks the command.

    Stdlib-only and subprocess-free for the same reason as the worktree guard:
    this runs on the gateway start path.
    """
    env = os.environ if env is None else env
    appdir = (env.get("APPDIR") or "").strip()
    if appdir:
        try:
            if path == Path(appdir) or Path(appdir) in path.parents:
                return True
        except (OSError, ValueError):
            pass
    # `.mount_` is the AppImage runtime's own prefix, so this does not condemn
    # unrelated temp paths.
    return any(part.startswith(".mount_") for part in path.parts)


def _under_system_tmp(path: Path) -> bool:
    """Whether *path* lives under the system temp directory.

    Ephemerality signal for the shared-agent-home guard
    (``agent._decline_shared_agent_home``): a CHECKOUT under the temp root is
    throwaway by construction — the OS reaps it on reboot, CI reaps it per job,
    and the automation that clones a per-task scratch tree deletes it when the
    task ends. A machine-wide agent spec stamped from such a checkout outlives
    it and leaves every managed MCP server pointing at a launcher (and possibly
    a pinned data home) that is gone.

    Deliberately NOT folded into :func:`_in_ephemeral_tree`. That predicate
    serves the launcher installer, which rejected a blanket temp-dir rule on
    its own grounds (see its docstring): a dangling launcher is caught by the
    interpreter being gone, and declining costs a legitimate temp install its
    global command. The agent-home guard makes the opposite call because its
    artifact is shared machine-wide and declining costs nothing durable — the
    instance simply uses the specs that already worked. It does subtract the one
    temp resident that is a durable install in disguise: an AppImage's runtime
    mount, excluded at that call site via :func:`_in_ephemeral_tree`. This
    predicate stays a plain "is it under the temp root" answer so each caller
    keeps its own exemptions.

    Compared on RESOLVED paths on both sides, so a symlinked temp root (macOS
    ``/tmp`` -> ``/private/tmp``, ``/var/folders`` -> ``/private/var/folders``)
    and a symlink into the tree land in the same namespace.

    TWO roots are answered against, not one. ``tempfile.gettempdir()`` is the
    root as configured AT CALL TIME (it honours ``$TMPDIR``), matching how the
    rest of this module treats redirected environments — but on macOS launchd
    sets ``$TMPDIR`` to a per-user ``/var/folders/.../T``, so ``gettempdir()``
    alone does NOT contain ``/tmp``, and ``/tmp/<scratch clone>`` would read as
    durable there. POSIX ``/tmp`` is
    therefore checked as well: it is reaped on reboot by contract, so nothing
    durable lives under it. ``/var/tmp`` deliberately is not: POSIX has it
    PRESERVED across reboots, which is the opposite claim.

    That literal is added on POSIX ONLY. ``Path("/tmp")`` is drive-RELATIVE on
    Windows, so ``.resolve()`` anchors it to the current drive and yields
    ``C:\\tmp`` — a path with none of the reboot-reaped meaning the rule rests on,
    and one a checkout may legitimately live under. Windows keeps
    ``gettempdir()`` (``%TEMP%``) as its only root. A root that cannot be
    resolved is skipped rather than guessed.
    """
    roots: list[Path] = []
    candidates = [tempfile.gettempdir()]
    if os.name == "posix":
        candidates.append("/tmp")
    for candidate in candidates:
        try:
            roots.append(Path(candidate).resolve())
        except (OSError, ValueError):  # pragma: no cover - defensive: unusable root
            continue
    # The PATH is resolved too, not just the roots. Resolving one side only makes the
    # comparison cross namespaces on exactly the platform this rule exists for:
    # macOS resolves `/tmp` to `/private/tmp`, so a checkout at `/tmp/<scratch clone>`
    # keeps `/tmp` among its parents, matches nothing, and reads as DURABLE. The guard
    # then stamps a machine-wide agent spec from a tree the OS reaps at reboot, which
    # is the outcome it exists to prevent.
    #
    # Resolving is also the safe direction for a symlink pointing OUT of the temp tree:
    # the checkout really lives at the target, so a durable target correctly stops
    # matching rather than being declined for the shape of its path.
    try:
        target = path.resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive: unresolvable path
        target = path
    return any(target == root or root in target.parents for root in roots)


def _in_linked_git_worktree(path: Path) -> bool:
    """Whether *path* lives inside a git **linked worktree** (``git worktree add``).

    A linked worktree's ``.git`` is a FILE holding
    ``gitdir: <git-dir>/worktrees/<name>``; an ordinary clone's ``.git`` is a
    DIRECTORY. Walks up from *path* and answers on the nearest repository marker,
    so a linked worktree is distinguished from the main clone it belongs to.

    The pointer is matched on the ``/worktrees/`` segment rather than
    ``/.git/worktrees/``, because a **bare** repository's git dir *is* the repo
    dir and carries no ``.git`` component — ``git -C myrepo.git worktree add``
    writes ``gitdir: /…/myrepo.git/worktrees/<name>``, which a ``/.git/`` match
    would miss, silently reopening the very bypass this guard exists to close.
    ``/worktrees/`` stays precise: the only other producer of a ``gitdir:``
    ``.git`` file is a submodule, which points into ``modules/`` instead.

    Deliberately stdlib-only and subprocess-free: this runs on the gateway start
    path, where shelling out to ``git`` would add latency and fail wherever git is
    absent (notably the packaged desktop app).
    """
    for parent in (path, *path.parents):
        marker = parent / ".git"
        if marker.is_dir():
            return False  # ordinary clone — nearest marker wins
        if marker.is_file():
            try:
                head = marker.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                return False
            # Normalize separators so a Windows-style gitdir also matches.
            return head.startswith("gitdir:") and "/worktrees/" in head.replace("\\", "/")
    return False


def kiro_home() -> Path:
    """Return the kiro home (``~/.kiro``), honoring a ``KIRO_HOME`` override.

    ``KIRO_HOME`` is kiro-cli's own documented override for its user-level
    directory. Honoring it here is what lets the agents directory — the specs that
    define which MCP servers exist — stop being unconditionally machine-wide, so a
    non-default instance can be told to own its own copy instead of rewriting the
    real install's (see ``agent._decline_shared_agent_home`` for what that
    rewriting costs: managed MCP servers that read one credential while calling a
    gateway that expects another, and 403 on every call).

    SCOPE — read before setting this. ``KIRO_HOME`` redirects kiro-cli's WHOLE
    user directory (agents, prompts, skills, steering, settings, sessions). The
    two subtrees Kiro Crew itself reads follow it through this resolver:
    the agents dir (:func:`kiro_agents_dir`) and the chat transcripts
    (:func:`kiro_sessions_dir`), so writer and reader stay in agreement and
    session resume survives the redirect. That is what lets an isolated instance
    own ``KIRO_HOME=<data home>/kiro`` -- ``build_pod_env`` sets it for a pod,
    the E2E harness for its test gateway, and :func:`adopt_isolated_kiro_home`
    for any other non-default data home. kiro-cli's login state lives
    outside ``~/.kiro`` and is unaffected.

    What does NOT follow it -- and therefore splits under a redirect, because
    kiro-cli reads ``<KIRO_HOME>/<subtree>`` while Kiro Crew keeps reading the
    HOST ``~/.kiro/<subtree>``:

    * ``settings/mcp.json``, the kiro-cli global MCP registry
      (``agent._KIRO_MCP_JSON``, ``apps/bridges.py``, ``mcp_discovery.py``,
      ``dashboard/handlers/mcp.py``). Inert for the servers kiro-cli actually
      connects: the specs Kiro Crew emits pin ``includeMcpJson`` off and carry
      the host registry merged in, so only a user editing the registry by hand
      and expecting kiro-cli's own reading of it to move would notice.
      ``pod/runtime.py`` records the same residual for ``kirocrew app`` in a pod.
    * ``skills`` (``agent.py`` skill-path discovery, ``dashboard/handlers/
      _shared.py``), ``hooks`` (``agent._DEFAULT_KIRO_HOOKS_DIR``), ``steering``
      (``dashboard/handlers/steering.py``) and ``prompts``
      (``dashboard/handlers/prompts.py``): the dashboard shows and edits the host
      copies while a redirected kiro-cli loads its own. For an install that
      relocated its data home permanently this is the visible cost of
      :func:`adopt_isolated_kiro_home`, and ``KIRO_HOME=~/.kiro`` is the opt-out;
      routing those readers through this resolver is the follow-up that removes
      it.

    Rejects the same unsafe targets as :func:`_valid_override_home` (a
    filesystem/drive root, or a known POSIX system directory) so a malformed
    override degrades to the default rather than scattering agent JSON across
    ``/`` or ``/usr``.
    """
    override = os.environ.get("KIRO_HOME")
    if not override:
        return default_kiro_home()
    p = explicit_kiro_home()
    if p is None:
        logger.warning("KIRO_HOME=%s is a system directory or unresolvable, ignoring", override)
        return default_kiro_home()
    return p


def default_kiro_home() -> Path:
    """The machine-wide kiro-cli home, ``~/.kiro``, regardless of ``KIRO_HOME``.

    The one spelling of the host home, for the few callers that must name it
    while the process itself reads another: the doctor line that says what the
    host still holds, and the transcript reclaim that moves an adopted
    instance's files out of it. Everything else goes through :func:`kiro_home`.
    """
    return Path.home() / ".kiro"


def explicit_kiro_home() -> Path | None:
    """The resolved ``KIRO_HOME`` override iff it is set AND valid; else ``None``.

    The ``KIRO_HOME`` twin of :func:`_valid_override_home`. :func:`kiro_home`
    falls back to the machine-wide ``~/.kiro`` when the override names a
    filesystem root or a system directory, so any caller asking "did the
    operator choose a kiro home?" must apply the SAME test -- reading the raw
    variable would count ``KIRO_HOME=/etc`` as a choice while the resolver had
    already discarded it. Resolving: not for the boot prologue.

    A value that cannot be RESOLVED -- a symlink cycle (``RuntimeError`` from
    pathlib), an unreadable ancestor (``OSError``) -- is invalid too, not a crash:
    an environment variable is not a place a process may be aborted from.
    """
    override = os.environ.get("KIRO_HOME")
    if not override:
        return None
    try:
        p = Path(override).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if _is_unsafe_home(p):
        return None
    return p


#: Test/tooling redirect for :func:`kiro_sessions_dir`, consulted on every call
#: (``None`` = resolve from the environment). Same shape as
#: :data:`_agents_dir_override` and for the same reason: several modules bind
#: ``kiro_sessions_dir`` by name (``from ... import kiro_sessions_dir``), which
#: copies the function OBJECT, so patching this module's attribute would never
#: reach them. A value read inside the function BODY does, because a function's
#: globals are always its defining module's.
_sessions_dir_override: Callable[[], Path] | None = None


def kiro_sessions_dir() -> Path:
    """Where kiro-cli stores its chat transcripts: ``<kiro home>/sessions/cli``.

    Honors ``KIRO_HOME`` for the same reason :func:`kiro_agents_dir` does. This
    matters because ``KIRO_HOME`` is directory-wide: it moves the transcripts too,
    so an instance that redirects its agent specs and then has KiroCrew read
    transcripts from the machine-wide path loses session resume and has its
    mappings pruned. Routing both through the resolver keeps writer and reader in
    agreement.

    Honours :data:`_sessions_dir_override` when one is installed, the same lever
    :func:`kiro_agents_dir` offers for the agent-spec home — this is the third
    ``~/.kiro`` axis (agents, transcripts, and the data home ``KIROCREW_HOME``
    already covers) and it needs its own hook because it is resolved lazily
    (``kiro_home()`` -> ``$KIRO_HOME`` or ``Path.home()/.kiro``) at every call, so
    neither ``KIROCREW_HOME`` nor an import-time path pin can reach it.
    """
    if _sessions_dir_override is not None:
        return _sessions_dir_override()
    return kiro_home() / "sessions" / "cli"


def kiro_oauth_cache_home() -> Path:
    """The OS-level home whose ``.aws/sso/cache`` kiro-cli's MCP OAuth grant pairs
    resolve under, honoring a ``KIROCREW_OS_HOME`` override.

    kiro-cli derives its MCP OAuth artifact directory from the process's real
    ``$HOME`` (``mcp_grant.kiro_oauth_cache_dir()`` calls :func:`Path.home` by
    default) -- unlike the agent-specs/sessions tree above, there is no
    documented kiro-cli env var that relocates just this one subtree.
    ``KIRO_HOME`` does not help either: it moves agents/prompts/skills/sessions,
    not ``~/.aws``, which sits outside ``~/.kiro`` entirely.

    ``KIROCREW_OS_HOME`` is therefore an override owned by Kiro Crew, consulted by
    :func:`kiro_crew.mcp_grant.kiro_oauth_cache_dir` and set by
    :func:`kiro_crew.pod.runtime.build_pod_env` to a pod-owned directory. Setting
    it repoints where every ``mcp_grant`` caller (mint, status, disconnect,
    mcp_discovery's remote probe) STATS and unlinks grant artifacts -- it does
    NOT by itself repoint kiro-cli's own writes, which follow the CHILD
    process's ``$HOME``. The two must be set together: the ACP spawn path
    remaps a pod-spawned kiro-cli child's ``HOME`` to this same directory (see
    ``acp/client.py`` and ``acp/runtime.py``), so kiro-cli's OWN writes and
    every ``mcp_grant`` reader agree on one location -- the same "one resolver,
    both sides read it" shape :func:`kiro_home` uses for agent specs.

    Honoured ONLY when ``KIROCREW_POD`` is exactly ``"1"``, which is the same
    gate the write side (``acp.client._apply_pod_home_remap``) applies. Reads and
    writes must turn on together: an override honoured here but not there would
    repoint grant READS while kiro-cli kept WRITING under the real home,
    recreating precisely the read/write split this resolver exists to close.
    ``build_pod_env`` is the only writer of either variable and sets both, so the
    paired gate costs nothing and removes the asymmetry.

    Rejects the same unsafe targets as :func:`kiro_home` (a filesystem/drive
    root, or a known POSIX system directory), degrading to :func:`Path.home` so
    a malformed override cannot scatter OAuth artifacts across ``/`` or
    ``/usr``.
    """
    if os.environ.get("KIROCREW_POD") != "1":
        return Path.home()
    override = os.environ.get("KIROCREW_OS_HOME")
    if not override:
        return Path.home()
    p = Path(override).expanduser().resolve()
    if _is_unsafe_home(p):
        logger.warning("KIROCREW_OS_HOME=%s is a system directory, ignoring", override)
        return Path.home()
    return p


def isolated_kiro_home(data_home: Path) -> Path:
    """The kiro-cli user home an ISOLATED instance owns: ``<data home>/kiro``.

    The one recipe every isolated instance uses -- ``build_pod_env``, the offline
    E2E harness and :func:`adopt_isolated_kiro_home` spell ``KIRO_HOME`` through
    this function, and the GUI user-test rig (``scripts/gui-user-test/boot.sh``)
    hand-spells the same ``<data home>/kiro`` -- so it is defined once and
    :func:`isolated_agents_dir` derives from it rather than the two being
    hand-written side by side.
    """
    return data_home / "kiro"


def isolated_agents_dir(data_home: Path) -> Path:
    """The dedicated agents dir an ISOLATED instance may own: ``<data home>/kiro/agents``.

    Single definition so the write guard's privacy test and the documented
    ``KIRO_HOME=<data home>/kiro`` recipe cannot drift apart. Deliberately an
    EXACT location rather than "anywhere beneath the data home": an ancestry test
    treats the machine-wide ``~/.kiro/agents`` as private whenever the data home
    happens to be an ancestor of it (``KIROCREW_HOME=$HOME`` is enough), which
    hands an ephemeral instance the shared specs.
    """
    return isolated_kiro_home(data_home) / "agents"


def _main_homes() -> tuple[Path, ...]:
    """Lexical spellings of the data homes that OWN the machine-wide ``~/.kiro``.

    Two, not one: the default ``~/.kiro/crew`` and the pre-move ``~/.kirocrew``.
    An install still running on the legacy home (or one that names either
    explicitly through ``KIROCREW_HOME``) is the operator's real, single
    instance, and the shared kiro-cli home is its to write.

    Deliberately NOT resolved: this is consulted from the CLI prologue of every
    subcommand, before the gateway binds, and ``Path.resolve()`` on a roaming or
    UNC-backed home is a network round-trip that can stall boot. The comparison in
    :func:`foreign_data_home` is therefore lexical on both sides.
    """
    return (_default_home(), _legacy_home())


def foreign_data_home() -> Path | None:
    """The data home this process runs on iff it is NOT the operator's main one.

    ``None`` on a default install, on the legacy home, and when ``KIROCREW_HOME``
    merely re-spells either of those (``~``, a trailing slash, ``..`` segments) --
    all of them are the machine's one real instance. A valid override pointing
    anywhere else -- a ``.kirocrew-dev`` tree, a scratch home for a screenshot
    run, a relocated data directory -- is returned as :func:`config_dir` resolved
    it.

    This is the predicate the agent-spec write guard and
    :func:`adopt_isolated_kiro_home` share, so "which instance owns
    ``~/.kiro/agents``" is answered in one place. The comparison is against the
    default/legacy homes, not "override set vs unset": ``config_dir()`` honours a
    valid override, so an override that merely names the default home must still
    read as main.

    Both the RESOLVED override and its RAW spelling (lexically normalised) are
    tested, so ``KIROCREW_HOME=~/.kiro/crew`` reads as the main instance even
    when ``$HOME`` sits behind a symlink and resolution would move it. The gap
    this leaves is deliberate: an alias the filesystem alone can reveal
    (``KIROCREW_HOME=~/my-link`` with ``my-link -> ~/.kiro/crew``) compares
    unequal when ``$HOME`` is itself symlinked, and is then treated as foreign --
    i.e. given its own kiro home. Closing that would need ``resolve()`` on the
    default home, which is the boot-path network round-trip this function
    refuses to add; an operator in that position spells the default home
    literally, or unsets ``KIROCREW_HOME``, which IS the default.

    Adds no filesystem work of its own: the override's resolution is
    :func:`config_dir`'s, memoised by the ``ensure_data_home()`` call that
    precedes this in every prologue, so this is a dictionary lookup.
    """
    raw = os.environ.get("KIROCREW_HOME", "")
    if not raw:
        return None
    # ``config_dir()`` HONOURS the override when it is valid and falls back to the
    # default home when it is not (a system directory) -- in which case the
    # comparison below reads it as main, exactly like an unset override. Memoised
    # on the raw env value, so after the prologue's ``ensure_data_home()`` this
    # resolves nothing.
    home = config_dir()
    mains = _main_homes()
    if home in mains:
        return None
    if Path(os.path.normpath(os.path.expanduser(raw))) in mains:
        return None
    return home


def _reprime_kiro_home_memos(agents_root: Path) -> None:
    """Refresh every already-loaded memo keyed on ``KIRO_HOME`` after we change it.

    ``kiro_crew.hooks`` memoizes ``kiro_agents_dir()`` (the UNC gate's trusted
    root) keyed on the raw ``KIRO_HOME`` and primes it at ITS import, which on the
    CLI path precedes the prologue. Left alone after the export, the first gate
    check would resolve the path on whatever thread asked -- on an async
    validation path the event loop, and on a UNC-shaped home an SMB round-trip.
    Re-resolving it here would move that round-trip onto the boot path instead,
    ahead of the socket bind -- so the memo is SEEDED with *agents_root*, which
    the caller already knows lexically from the resolved data home, and nothing
    is stat'ed.

    Coupled HERE, inside the one function that changes the variable, rather than
    hand-spelled at each caller: a third entrypoint that adopts and forgets to
    re-prime would reintroduce the stale memo silently. Reached through
    ``sys.modules`` on purpose -- this module is a stdlib-only leaf and must not
    import ``hooks``; a module that is not loaded yet has no stale memo, and its
    import-time priming will see the adopted value.
    """
    hooks_mod = sys.modules.get("kiro_crew.hooks")
    if hooks_mod is not None:
        hooks_mod.seed_unc_agents_root(agents_root)


def adopt_isolated_kiro_home() -> Path | None:
    """Give a non-default data home its OWN kiro-cli home, unless one is declared.

    Exports ``KIRO_HOME=<data home>/kiro`` into this process's environment when
    the process runs on a foreign data home (:func:`foreign_data_home`) and no
    ``KIRO_HOME`` is set. Returns the adopted home, or ``None`` when nothing
    changed. Idempotent; call it once from a process's synchronous prologue,
    before anything resolves :func:`kiro_home` or spawns kiro-cli.

    Why the PROCESS environment rather than a resolver-side rule in
    :func:`kiro_home`: the value has to reach kiro-cli, which reads ``KIRO_HOME``
    from its own environment and inherits ours (``acp/client.py`` spawns with
    ``{**os.environ}``), and every managed MCP shim under it inherits the same.
    One export in the prologue makes the gateway, its kiro-cli children, their
    ``kirocrew mcp-*`` stubs, and every ``kirocrew`` CLI verb run on this home
    agree on one kiro home -- exactly what ``build_pod_env`` already arranges for
    a pod by hand.

    Why at all: the agents directory is ``<kiro home>/agents``, and the default
    kiro home is the machine-wide ``~/.kiro`` -- NOT under ``KIROCREW_HOME``. A
    gateway booted with ``KIROCREW_HOME=<scratch>`` and no ``KIRO_HOME`` would
    otherwise rebuild the operator's shared ``~/.kiro/agents/*.json`` on boot and
    pin its own scratch home into every managed MCP server's ``env``
    (``agent._managed_mcp_env``). From then on every ``kirocrew-core`` stub the
    REAL gateway's sessions spawn resolves ``config_dir()`` to the scratch home,
    looks for its signed session-pid mapping there, finds nothing, and every
    strict-identity tool is refused with "signed pid mapping did not verify" --
    on a machine whose trust root is healthy. With its own kiro home the scratch
    instance writes ``<scratch>/kiro/agents`` instead, the write guard's
    private-target exemption admits that, and the shared specs stay the default
    instance's.

    What it deliberately does NOT do: touch a process that already carries a
    ``KIRO_HOME`` (an explicit choice: the operator decides which kiro home
    kiro-cli READS -- ``KIRO_HOME=~/.kiro`` keeps a relocated install on the
    machine-wide steering, skills and transcripts, at the cost that its managed
    MCP servers then follow the DEFAULT instance's specs, because a foreign data
    home never writes the shared ones), or a process on the default or legacy
    home. The declared value is NOT validated here: judging it means resolving
    it, and this is the boot path. :func:`kiro_home` applies its test where the
    value is read -- a ``KIRO_HOME`` naming ``/etc`` or an unresolvable path
    falls back to the shared ``~/.kiro`` there, with a warning, which lands the
    process exactly where the ``KIRO_HOME=~/.kiro`` opt-out does: reading the
    default instance's specs, never writing them (the write guard's ownership
    arm takes no environment variable as consent). And it is not a resolver:
    :func:`kiro_home` keeps reading the environment, so a test that isolates it
    with ``patch("pathlib.Path.home", ...)`` is unaffected.

    Scope note for an install that relocated its data home permanently: its
    kiro-cli children read agents, sessions, steering, skills and settings from
    ``<data home>/kiro`` rather than ``~/.kiro`` (kiro-cli's login state lives
    elsewhere and is unaffected). The adoption line names the opt-out above, and
    ``kirocrew doctor`` (Data Home) lists what the host ``~/.kiro`` still holds
    that such an instance does not read.

    Filesystem-free, deliberately: the only resolution it relies on is the one
    ``ensure_data_home()`` already paid for ``config_dir()``, and it neither stats
    nor resolves the adopted path, the declared ``KIRO_HOME``, or anything else.
    This runs before the gateway binds, and a probe on a roaming or UNC-backed
    home is a network round-trip that would hold readiness hostage. Whether the
    adopted path is a real directory is judged by the write guard before
    anything is written there.
    """
    if os.environ.get("KIRO_HOME"):
        return None
    own_home = foreign_data_home()
    if own_home is None:
        return None
    adopted = isolated_kiro_home(own_home)
    # No filesystem access here, by rule: this is the shared CLI prologue, ahead
    # of the gateway binding its socket, and a stat on a roaming or UNC-backed
    # home is a network round-trip that would hold readiness hostage. What the
    # adopted path IS on disk -- a real directory, a planted link to ``~/.kiro``,
    # a stray regular file -- is judged where it matters, by the write guard
    # (``agent._private_isolated_agents_dir``) before any spec is written there;
    # until then the export only decides what kiro-cli READS.
    os.environ["KIRO_HOME"] = str(adopted)
    _reprime_kiro_home_memos(isolated_agents_dir(own_home))
    logger.info(
        "KIROCREW_HOME=%s is not the default data home, so this instance owns its own "
        "kiro-cli home: KIRO_HOME=%s (kiro-cli reads agents, sessions, steering, skills "
        "and settings there, not under ~/.kiro). If this install relocated its data "
        "home on purpose and wants kiro-cli to keep reading ~/.kiro, export "
        "KIRO_HOME=%s -- its Kiro Crew MCP servers then follow the default instance's "
        "specs, since a non-default data home never writes the shared ones. "
        "`kirocrew doctor` (Data Home) reports the same.",
        own_home,
        adopted,
        Path.home() / ".kiro",
    )
    return adopted


#: Test/tooling redirect for :func:`kiro_agents_dir`, consulted on every call
#: (``None`` = resolve from the environment). A CALLABLE rather than a path so a
#: caller can decide per call — the suite's own redirect defers to a test that
#: moved ``Path.home`` or ``$KIRO_HOME`` itself, which it cannot know in advance.
#:
#: Why it lives HERE rather than as a per-module hook: 16 modules bind
#: ``kiro_agents_dir`` by name (``from ... import kiro_agents_dir``), and that
#: copies the function OBJECT, so patching this module's attribute never reaches
#: them. A value read inside the function BODY does reach them, because a
#: function's globals are always its defining module's -- so one assignment here
#: redirects every consumer, present and future, with no per-module registration
#: to keep in sync.
_agents_dir_override: Callable[[], Path] | None = None


def ambient_agents_dir() -> Path:
    """The agents dir the AMBIENT ENVIRONMENT resolves, ignoring any override.

    Exists for exactly one caller: ``agent._decline_shared_agent_home`` asks "is
    the target this instance is about to write the one every instance under this
    environment shares?". That question is about the environment, so it must not
    see a redirect — with the override applied to both sides the comparison is
    trivially equal and the guard mistakes a privately redirected target for the
    shared one, then refuses the write from any ephemeral checkout.

    Not a general-purpose reader. Anything that WRITES or reads the specs in use
    wants :func:`kiro_agents_dir`, which honours the redirect.
    """
    return kiro_home() / "agents"


def kiro_agents_dir() -> Path:
    """Return the kiro agents directory (``<kiro home>/agents``).

    Lives in this leaf module so :mod:`kiro_crew.config.loader` can locate
    installed agent JSONs without importing :mod:`kiro_crew.agent` — which
    imports ``config.loader`` at module load and would create an import cycle.

    Single-valued on purpose: this is the WRITE target as well as the user-level
    read scope. ``apps.bridges._register_agents`` materializes app agents here and
    ``agent.rebuild_agent_config`` writes the managed specs here, so widening it
    to a search path would leave those writers without one obvious destination.
    Project-local discovery is a separate READ-only scope — see
    :func:`project_agents_dir`.

    Honours :data:`_agents_dir_override` when one is installed, which is what
    makes this the single accessor every consumer already routes through. See
    :func:`ambient_agents_dir` for the one caller that must NOT follow it.

    The no-override branch DELEGATES to that function rather than re-spelling
    ``kiro_home() / "agents"``. Two hand-written copies of the default would let a
    later change to the layout land in only one, and the write guard compares this
    resolver's answer against that one -- a stale comparison there reads a shared
    target as private and fails OPEN on the machine-wide home, which is the
    failure class this whole seam exists to prevent.
    """
    if _agents_dir_override is not None:
        return _agents_dir_override()
    return ambient_agents_dir()


def project_agents_dir(project_dir: str | Path) -> Path:
    """The kiro-cli *workspace* agents dir of a project: ``<project>/.kiro/agents``.

    kiro-cli resolves ``--agent <name>`` against ``$PWD/.kiro/agents`` before the
    user-level directory, with NO upward walk — invoked from a subdirectory it does
    not find the repo root's agents. Kiro Crew launches kiro-cli with the session's
    project directory as its cwd, so this is exactly the directory the backend
    itself searches for that session.

    Read-only by construction: nothing in Kiro Crew writes here, because the
    directory belongs to the user's checkout and is typically version-controlled.
    """
    return Path(project_dir) / ".kiro" / "agents"


def project_kiro_dir(project_dir: str | Path) -> Path:
    """The ``<project>/.kiro`` directory itself, which also holds agent specs.

    Distinct from :func:`project_agents_dir` because Kiro Crew additionally honors
    ``<project>/.kiro/*.agent-spec.json`` — a Kiro Crew-only convention that
    predates ``.kiro/agents/`` and remains in use by projects driven from Slack.
    """
    return Path(project_dir) / ".kiro"


def _default_workspace_base() -> Path:
    """Return the platform-specific default base for the workspace."""
    if sys.platform == "darwin":
        vol = Path("/Volumes/workplace")
        return vol if vol.is_dir() else Path.home() / "workplace"
    return Path.home() / "workplace"


def _safe_dir_name(key: str) -> str:
    """Sanitize a session key into a safe directory name."""
    return key.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")
