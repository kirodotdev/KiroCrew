"""Pure filesystem path primitives for KiroCrew configuration.

This is a **leaf module**: it depends only on the standard library
(``os``, ``sys``, ``pathlib``, ``logging``) and imports nothing from
``kiro_crew``. Modules that only need to locate ``~/.kirocrew/`` should import
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
import sys
from collections.abc import Mapping
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

# Memo for ``config_dir()``: ``(raw KIROCREW_HOME, _resolved_home at the time,
# result)``. ``config_dir()`` is called from 323 sites and each uncached call
# does a ``Path.resolve()`` + ``mkdir`` and, on the default path, a breadcrumb
# read/write — measured 94.9us per call. Keying
# on the RAW env value keeps the override honoured the moment it changes
# (``KIROCREW_HOME`` is repointed per test by the suite's isolation fixture, and
# by pods/worktrees at runtime), and keying on ``_resolved_home`` by identity
# ties the default-path entry to the resolution cache below — so clearing
# ``_resolved_home`` (which the test suite does per test) invalidates this memo
# too instead of pinning a stale home. In a real process both keys are stable
# after the first call, which is what makes the breadcrumb write
# effectively once-per-process rather than once-per-call.
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
        # we don't churn the file on every process start.
        if crumb.is_file():
            try:
                if str(data_home) in crumb.read_text(encoding="utf-8"):
                    return
            except OSError:
                pass
        crumb.write_text(content, encoding="utf-8")
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
    _config_dir_memo = (override_raw, _resolved_home, d)
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


def ensure_data_home() -> Path:
    """Eagerly resolve and create the data home — call BEFORE the loop.

    ``config_dir()`` resolves the data home lazily on its first call and also
    ``mkdir``s it and refreshes the recovery breadcrumb. Every real entrypoint
    calls this ONCE from its synchronous prologue, before ``asyncio.run``, so the
    resolution happens on the main thread and every later on-loop ``config_dir()``
    is a cheap cached lookup. Idempotent (the process-lifetime cache makes a
    second call a no-op) and safe to call unconditionally. Returns the resolved
    data home.
    """
    return config_dir()


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

    SCOPE CAVEAT — read before setting this. ``KIRO_HOME`` redirects kiro-cli's
    WHOLE user directory (agents, prompts, skills, steering, settings, sessions),
    but KiroCrew currently resolves the host ``~/.kiro`` for most of those readers
    (session transcripts in ``session_map.py`` / ``acp/*`` / ``providers/acp.py``
    / ``dashboard/handlers/usage.py``, and the ``settings/mcp.json`` registry).
    Setting ``KIRO_HOME`` therefore moves where kiro-cli WRITES sessions without
    moving where KiroCrew READS them, which breaks session resume. Only the agents
    directory follows it today, so this is not yet a supported way to isolate an
    instance — ``build_pod_env()`` deliberately does not set it. Bringing the
    remaining readers through this resolver is the prerequisite.

    Rejects the same unsafe targets as :func:`_valid_override_home` (a
    filesystem/drive root, or a known POSIX system directory) so a malformed
    override degrades to the default rather than scattering agent JSON across
    ``/`` or ``/usr``.
    """
    override = os.environ.get("KIRO_HOME")
    if not override:
        return Path.home() / ".kiro"
    p = Path(override).expanduser().resolve()
    if _is_unsafe_home(p):
        logger.warning("KIRO_HOME=%s is a system directory, ignoring", override)
        return Path.home() / ".kiro"
    return p


def kiro_sessions_dir() -> Path:
    """Where kiro-cli stores its chat transcripts: ``<kiro home>/sessions/cli``.

    Honors ``KIRO_HOME`` for the same reason :func:`kiro_agents_dir` does. This
    matters because ``KIRO_HOME`` is directory-wide: it moves the transcripts too,
    so an instance that redirects its agent specs and then has KiroCrew read
    transcripts from the machine-wide path loses session resume and has its
    mappings pruned. Routing both through the resolver keeps writer and reader in
    agreement.
    """
    return kiro_home() / "sessions" / "cli"


def isolated_agents_dir(data_home: Path) -> Path:
    """The dedicated agents dir an ISOLATED instance may own: ``<data home>/kiro/agents``.

    Single definition so the write guard's privacy test and the documented
    ``KIRO_HOME=<data home>/kiro`` recipe cannot drift apart. Deliberately an
    EXACT location rather than "anywhere beneath the data home": an ancestry test
    treats the machine-wide ``~/.kiro/agents`` as private whenever the data home
    happens to be an ancestor of it (``KIROCREW_HOME=$HOME`` is enough), which
    hands an ephemeral instance the shared specs.
    """
    return data_home / "kiro" / "agents"


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
    """
    return kiro_home() / "agents"


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
