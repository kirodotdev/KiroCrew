"""Shared environment helpers for subprocess spawning."""

from __future__ import annotations

import functools
import getpass
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

# Common directories where MCP server binaries may be installed.
# Order matters — earlier entries take precedence. ``{mise_data}`` resolves via
# :func:`mise_data_dir`, so a relocated mise data dir (``MISE_DATA_DIR`` /
# ``XDG_DATA_HOME``) keeps its shims ahead of the per-version install bins that
# :func:`node_all_bin_dirs` appends after this list — the shim honours the
# project's version pin, the raw install bin does not.
_EXTRA_PATH_DIRS = (
    "{home}/.local/bin",
    "{home}/.toolbox/bin",
    "{home}/.npm-packages/bin",
    "{mise_data}/shims",
    "{home}/.volta/bin",
    "/opt/homebrew/bin",  # Apple Silicon Homebrew node / global npm bins
    "/usr/local/bin",  # Intel Homebrew + pkg-installer symlink dir
)


# --- extending the MCP binary search path -------------------------------------
#
# ``_EXTRA_PATH_DIRS`` above is a fixed set of guesses and can never be
# complete: WHICH directory a package manager drops an MCP launcher into is
# ecosystem-, distribution- and site-specific. A launcher installed outside that
# list is invisible to every consumer of :func:`augmented_path` — the MCP probe,
# the agent-config command resolver, and gatewayd's rewriter — so a server
# declared by bare name never launches and the session simply comes up short of
# tools, which reads as a missing capability rather than a launch failure.
#
# So the list is extensible from two sides, merged in :func:`mcp_search_path`
# (issue #5083). Fixing it at that one function is deliberate: it is the path all
# three of those consumers resolve against, so one contributed directory reaches
# all of them without any of them knowing it exists.
#
# * ``mcp.extra_path_dirs`` in the config — an operator on one host.
# * :func:`register_mcp_path_dirs` — a packaged/downstream build or an embedding
#   runtime, which has no per-user config file to edit.
#
# Both go through the same absolute-only :func:`_validated_bin_dir` gate as this
# module's own entries, and both rank ahead of :func:`augmented_path`'s guesses:
# a directory somebody named explicitly is more specific than a built-in guess,
# so it must win when the same command name exists in both. A spec's own
# ``env.PATH`` still outranks everything -- an operator pin cannot be displaced.
#
# The contribution reaches ONLY the resolution path, never
# :func:`spec_env_path` (whose result :func:`emit_env` persists into consumed
# config files) and never :func:`augmented_path` (the generic composition). Both
# exclusions are load-bearing and are argued at :func:`mcp_search_path`.
#
# The config value is PUSHED here by the loader (:func:`publish_config_path_dirs`,
# called from ``KiroCrewConfig.load``) rather than read here. That is not
# indirection for its own sake: :func:`mcp_search_path` is reached from the event
# loop by every MCP probe (``probe_server``) and by the agent-config resolver, so
# pulling the config here would stat/read/validate ``config.json`` on the loop.
# Pushing keeps this module's whole search-path construction free of IO, and
# costs nothing: every process that spawns an MCP server loads the config at
# startup, and each later ``load()`` refreshes the snapshot, so an edited setting
# takes effect without a restart.
_registered_path_dirs: tuple[str, ...] = ()
_config_path_dirs: object = ()
_path_dirs_lock = threading.Lock()

#: Rejected operator-supplied bin dirs already logged, so a per-spawn call site
#: does not re-log the same bad entry on every subprocess launch. Bounded by the
#: number of distinct bad values an operator authored.
_warned_bad_bin_dirs: set[str] = set()


def _warn_rejected_bin_dir(raw: object, source: str) -> None:
    """Log a rejected bin dir once per distinct value.

    Silently dropping it would reproduce the failure this seam exists to fix:
    a directory that looks configured but contributes nothing, with no trace.
    """
    key = f"{source}\0{raw!r}"
    if key in _warned_bad_bin_dirs:
        return
    _warned_bad_bin_dirs.add(key)
    logger.warning(
        "%s: ignoring %r — expected a single absolute directory path",
        source,
        raw,
    )


def _validated_bin_dirs(values: object, source: str) -> list[str]:
    """Validate an operator-supplied LIST of bin directories, dropping bad ones.

    ``~`` is expanded first (a config file is where a human writes
    ``~/.pixi/bin``), then each entry passes the same
    :func:`_validated_bin_dir` gate as this module's own well-known dirs — one
    absolute directory, no path separator, no NUL. A rejected entry is dropped
    rather than failing the whole list: one typo must not cost an operator every
    other directory they declared, nor abort PATH construction for every spawn.

    *values* is typed ``object`` because both sources are hand-editable and can
    legally parse as something other than a list of strings.
    """
    if not isinstance(values, (list, tuple)):
        if values not in (None, ""):
            _warn_rejected_bin_dir(values, source)
        return []
    out: list[str] = []
    for raw in values:
        entry = _validated_bin_dir(os.path.expanduser(raw)) if isinstance(raw, str) else None
        if entry is None:
            _warn_rejected_bin_dir(raw, source)
            continue
        out.append(entry)
    return out


def register_mcp_path_dirs(*dirs: str) -> tuple[str, ...]:
    """Contribute directories to the MCP binary search path. Returns the accepted ones.

    The programmatic half of the seam described above: a downstream build, a
    packaged distribution, or a host embedding Kiro Crew can name the directories
    ITS package manager installs MCP launchers into, without forking this module
    and without a per-user config file. Call it during startup, before the first
    session or MCP probe; :func:`augmented_path` reads the registry on every
    call, so a later registration still takes effect (nothing is cached).

    Entries are validated (:func:`_validated_bin_dirs`) and deduped, so calling
    this twice with the same directory is a no-op rather than a doubled PATH
    entry — an idempotent installer hook can just call it again. Registration
    order is precedence order, and the whole registry ranks behind
    ``mcp.extra_path_dirs``: an operator's own host setting outranks a
    distribution default.
    """
    global _registered_path_dirs
    accepted = _validated_bin_dirs(list(dirs), "register_mcp_path_dirs")
    with _path_dirs_lock:
        merged = _dedup_dirs([*_registered_path_dirs, *accepted])
        _registered_path_dirs = tuple(merged)
    return tuple(accepted)


def publish_config_path_dirs(values: object) -> None:
    """Record ``mcp.extra_path_dirs`` for :func:`augmented_path` to merge.

    Called by ``KiroCrewConfig.load`` on every load, which is what keeps the
    snapshot current without this module ever reading the config -- see the
    section comment above for why the direction matters. Stores the value AS
    AUTHORED and validates it at use: validation is pure string work, so keeping
    it out of here keeps the load path's cost to one tuple assignment.

    Idempotent and safe from any thread; a load that reports no setting clears
    the snapshot, so removing the setting takes effect too. A malformed value is
    stored as-is and rejected (with a warning) at use, so this cannot turn a
    scalar into a directory entry.
    """
    global _config_path_dirs
    # Copied to a tuple so a caller mutating its list afterwards cannot mutate
    # the search path; a non-sequence is kept as-is for _validated_bin_dirs to
    # reject and report.
    snapshot: object = tuple(values) if isinstance(values, (list, tuple)) else values
    with _path_dirs_lock:
        _config_path_dirs = snapshot


def _extra_mcp_path_dirs() -> list[str]:
    """Operator/downstream-contributed MCP bin dirs, highest precedence first.

    Pure: no IO and no config read, so it is safe on the event loop (the MCP
    probe and the agent-config resolver both reach it from there). Validation of
    the config-supplied entries happens here rather than at publish time because
    it is string-only work.
    """
    return [
        *_validated_bin_dirs(_config_path_dirs, "mcp.extra_path_dirs"),
        *_registered_path_dirs,
    ]


# --- node build toolchain -----------------------------------------------------
#
# Directories a Node VERSION MANAGER installs a real ``node``/``npm`` into.
# Deliberately NARROW and separate from ``_EXTRA_PATH_DIRS``: that list is a
# broad "where might an MCP binary live" search path (it includes
# ``~/.local/bin`` and, via :func:`augmented_path`, the running interpreter's
# own ``bin``). Build callers prepend these entries to a PINNED PATH, so
# reusing the broad list would defeat the pinning.
#
# Candidates are generous on purpose -- each is validated by probing for an
# executable ``node`` inside it, so a layout guess that does not exist on this
# host simply drops out instead of polluting PATH.
_NODE_MANAGER_GLOBS = (
    # mise -- the manager ``install.sh --mise`` and ``ensure-node.sh`` use.
    "{mise_data}/installs/node/*/bin",
    # asdf's nodejs plugin.
    "{home}/.asdf/installs/nodejs/*/bin",
    # nvm.
    "{home}/.nvm/versions/node/*/bin",
    # fnm, both layouts: XDG default and legacy ``~/.fnm``.
    "{home}/.local/share/fnm/node-versions/*/installation/bin",
    "{home}/.fnm/node-versions/*/installation/bin",
    # The layout the retired nvm/fnm scan also globbed (``<ver>/bin`` directly
    # under the fnm root). Real fnm never produces it, but keeping the glob
    # makes the consolidated search a strict superset of what it replaced —
    # entries are validated/deduped downstream, so a layout that does not
    # exist on this host simply drops out.
    "{home}/.fnm/node-versions/*/bin",
)
# Shim / single-dir managers, which have no per-version path to glob.
# Two of these (mise shims, volta) also appear in ``_EXTRA_PATH_DIRS`` above.
# The repetition is deliberate, not an oversight: that list is the broad
# MCP-binary search path and this one is the narrow build toolchain, and entries
# here are additionally gated on actually containing an executable ``node``. If
# a manager moves its shim dir, BOTH lists need editing.
_NODE_MANAGER_DIRS = (
    "{mise_data}/shims",
    "{home}/.volta/bin",
    "{home}/n/bin",
)
# Standalone Node TREES -- an unpacked distribution rather than a manager's
# per-version store, so there is no version to glob and no shim to consult.
# These are where an operator unpacking a nodejs.org tarball by hand puts one.
# Only ``bin`` dirs of a Node-only tree belong here, never a general-purpose bin
# dir: every entry is PREPENDED to the pinned PATH of build subprocesses, where a
# dir full of unrelated user binaries would shadow the system tools that pinning
# exists to guarantee.
_NODE_TREE_DIRS = (
    "{home}/.local/node/bin",
    "{home}/.local/share/node/bin",
)
# Standalone trees under the DATA home -- where ``ensure-node.sh`` unpacks the
# unofficial glibc-2.17 build, i.e. a Node Kiro Crew installed itself. Relative
# paths, resolved against ``data_home()`` separately from the ``$HOME`` templates
# above because that call can fail (see :func:`node_bin_dirs`).
_NODE_TREE_DATA_HOME_DIRS = ("node-glibc217/bin",)
# Marker file written by ``ensure-node.sh`` recording the node bin dir it
# resolved. The Makefile already consumes it; this keeps Python callers on the
# same answer instead of re-deriving one.
_NODE_BIN_DIR_MARKER = "node-bin-dir"
# Operator escape hatch: an explicit absolute node bin dir, for hosts whose
# toolchain lives somewhere none of the layouts above cover.
_NODE_BIN_DIR_ENV = "KIROCREW_NODE_BIN_DIR"


def mise_data_dir(home: str) -> str:
    """mise's data dir, honouring ``MISE_DATA_DIR`` then ``XDG_DATA_HOME``."""
    explicit = os.environ.get("MISE_DATA_DIR")
    if explicit:
        return explicit
    xdg = os.environ.get("XDG_DATA_HOME")
    base = xdg if xdg else os.path.join(home, ".local", "share")
    return os.path.join(base, "mise")


def _has_node(d: Path) -> bool:
    """True when *d* holds an executable ``node``.

    This is the definition of "a node bin dir", and it is what lets the
    candidate lists above stay generous: a directory that does not actually
    provide node is not one.
    """
    names = ("node.exe", "node") if platform_compat.IS_WINDOWS else ("node",)
    return any(platform_compat.is_executable_file(d / n) for n in names)


def _node_version_key(name: str) -> tuple[int, tuple[int, ...], str]:
    """Sort key ranking a manager's version directory, highest/most-specific first.

    Version managers create ALIAS directories beside the real installs (mise
    alone has ``lts``, ``latest``, ``lts-jod``, plus truncations like ``22`` next
    to ``22.22.2``). Plain reverse-lexicographic order puts ``lts-krypton`` above
    ``24.16.0``, so the "newest" pick would be an arbitrary alias name. Rank
    parseable versions above unparseable aliases and compare them numerically.
    """
    stripped = name[1:] if name[:1] in ("v", "V") else name
    parts = stripped.split(".")
    if parts and all(p.isdigit() for p in parts):
        return (1, tuple(int(p) for p in parts), name)
    return (0, (), name)


def _manager_version_bin_dirs(home: str, mise_data: str, *, all_versions: bool) -> list[str]:
    """Scan the per-version manager roots (:data:`_NODE_MANAGER_GLOBS`).

    The two callers need DIFFERENT policies, chosen deliberately:

    - ``all_versions=False`` (build PATH, :func:`node_bin_dirs`): only the BEST
      version per root, and only dirs that actually hold an executable ``node``
      (:func:`_has_node`) — a build subprocess wants exactly one real toolchain,
      not every stale major on the box.
    - ``all_versions=True`` (MCP binary discovery, :func:`node_all_bin_dirs`):
      EVERY version's bin dir that exists. A globally-installed MCP binary
      (``npm i -g``) can live under any installed Node version — not just the
      newest — and the dir does not need ``node`` beside it to be worth
      searching, so filtering to the best version (or requiring ``node``) would
      silently stop finding binaries that were found before.

    Within each root, entries are ordered best version first
    (:func:`_node_version_key`: numeric versions outrank alias names).
    """
    out: list[str] = []
    for pattern in _NODE_MANAGER_GLOBS:
        root, _, leaf = pattern.format(home=home, mise_data=mise_data).partition("/*")
        keep = Path.is_dir if all_versions else _has_node
        try:
            matches = sorted(
                (p for p in Path(root).glob("*" + leaf) if keep(p)),
                # The version dir is the child of `root`; with a deeper leaf
                # (fnm's `<ver>/installation/bin`) that is not p.parent, so
                # index it off the root instead of walking up a fixed count.
                key=lambda p: _node_version_key(p.relative_to(root).parts[0]),
                reverse=True,
            )
        except (OSError, ValueError):
            continue
        out.extend(str(m) for m in (matches if all_versions else matches[:1]))
    return out


def _validated_bin_dir(val: str) -> str | None:
    """Accept *val* as a single absolute bin directory, else ``None``.

    Used by BOTH untrusted-ish sources of a bin dir -- the
    ``KIROCREW_NODE_BIN_DIR`` override and the ``ensure-node.sh`` marker file --
    so they cannot drift apart. Each names ONE directory; a value carrying a path
    separator would smuggle extra entries into every PATH built from it, so it is
    rejected rather than split. (``os.path.isabs("/a:/b")`` is True on POSIX, so
    the absolute check alone does not catch that.)
    """
    val = val.strip()
    if not val or os.pathsep in val or "\0" in val or not os.path.isabs(val):
        return None
    return val


def _marker_node_bin_dir() -> str | None:
    """Read the node bin dir recorded by ``ensure-node.sh``, or ``None``."""
    try:
        raw = (data_home() / _NODE_BIN_DIR_MARKER).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return _validated_bin_dir(raw.strip().splitlines()[0] if raw.strip() else "")


@functools.lru_cache(maxsize=1)
def node_bin_dirs() -> tuple[str, ...]:
    """Directories holding a version-manager-installed node, best first.

    Resolution order, most specific first:

    1. ``KIROCREW_NODE_BIN_DIR`` -- explicit operator override.
    2. ``<data-home>/node-bin-dir`` -- the marker ``ensure-node.sh`` writes
       after installing/locating node. Preferred over a bare filesystem scan
       because it names the version that script decided on.
    3. The highest version found under each per-version manager root
       (mise / asdf / nvm / fnm).
    4. Shim dirs (mise shims, volta, n).
    5. Standalone Node trees -- the glibc-2.17 build ``ensure-node.sh`` unpacks
       into the data home (:data:`_NODE_TREE_DATA_HOME_DIRS`), then a
       hand-unpacked nodejs.org tarball under ``~/.local``
       (:data:`_NODE_TREE_DIRS`). Last because a manager's install is the version
       the build was resolved against; a tree found here is a fallback that keeps
       a plainly-installed Node from reading as "no Node at all" on a daemon
       whose ``$PATH`` omits it.

    Every entry is verified to contain an executable ``node``, so the result is
    only ever real toolchain directories. Only the BEST version per manager root
    is returned: these entries go on the PATH of build subprocesses, and mise
    alone can contribute ~18 install and alias directories on a developer box --
    a PATH that long slows every exec lookup and buries the intended toolchain
    behind stale majors (node 16/18 against a ``>=22`` engines field).

    Why this exists: ``install.sh --mise`` and ``ensure-node.sh`` -- the
    supported install path -- put node under ``$HOME``. A non-login gateway
    (systemd / launchd) does not inherit those on ``$PATH``, and build callers
    additionally pin PATH to system dirs, so without this the build cannot see
    the very node Kiro Crew installed for it.

    Cached for the process lifetime: the globs must run once, matching
    :func:`node_all_bin_dirs`. A node installed while a long-lived
    gateway is running is not seen until restart; call ``cache_clear()`` if it
    ever needs re-discovery without one.
    """
    home = os.path.expanduser("~")
    mise_data = mise_data_dir(home)
    ordered: list[str] = []

    override = _validated_bin_dir(os.environ.get(_NODE_BIN_DIR_ENV, ""))
    if override:
        ordered.append(override)
    marker = _marker_node_bin_dir()
    if marker:
        ordered.append(marker)

    ordered.extend(_manager_version_bin_dirs(home, mise_data, all_versions=False))

    ordered.extend(d.format(home=home, mise_data=mise_data) for d in _NODE_MANAGER_DIRS)
    # Under a KIROCREW_HOME override data_home() mkdirs, so it can raise on an
    # unwritable path. Only the data-home candidates depend on it; swallowing here
    # keeps a failure from taking out the manager and $HOME tiers with them.
    try:
        dh: Path | None = data_home()
    except OSError:
        dh = None
    if dh is not None:
        ordered.extend(str(dh / rel) for rel in _NODE_TREE_DATA_HOME_DIRS)
    ordered.extend(d.format(home=home) for d in _NODE_TREE_DIRS)

    out: list[str] = []
    seen: set[str] = set()
    for d in ordered:
        # Normalize BEFORE the dedup check and before emitting. The glob branch
        # yields `str(Path(...))` while the template branch yields the format
        # string verbatim, so without this one function emits two spellings of
        # the same directory -- on Windows "C:\home/.volta/bin" alongside
        # "C:\home\.volta\bin". Windows tolerates forward slashes for filesystem
        # calls (so the dir is still FOUND), but these strings are joined onto
        # PATH and compared, and two spellings would also slip past `seen`.
        d = os.path.normpath(d)
        if d in seen:
            continue
        seen.add(d)
        try:
            if _has_node(Path(d)):
                out.append(d)
        except OSError:
            continue
    return tuple(out)


@functools.lru_cache(maxsize=1)
def _node_all_bin_dirs(home: str, mise_data: str) -> tuple[str, ...]:
    """Cached body of :func:`node_all_bin_dirs`, keyed on its inputs.

    Keyed on ``(home, mise_data)`` — matching the retired helper's ``home``
    keying — so a caller under a different HOME (tests patching
    ``expanduser``) gets a fresh scan instead of the previous key's dirs,
    while the steady-state gateway still globs exactly once.
    """
    out: list[str] = []
    seen: set[str] = set()
    for d in _manager_version_bin_dirs(home, mise_data, all_versions=True):
        d = os.path.normpath(d)
        # Only absolute entries may reach a spawned subprocess's PATH: a
        # relative one (possible via a relative MISE_DATA_DIR) would be
        # re-resolved against the CHILD's cwd, letting a work-dir-relative
        # ``npx`` shadow the system tool. Matches _validated_bin_dir's posture.
        if d in seen or not os.path.isabs(d):
            continue
        seen.add(d)
        out.append(d)
    return tuple(out)


def node_all_bin_dirs() -> tuple[str, ...]:
    """EVERY per-version manager bin dir (mise / asdf / nvm / fnm), all versions.

    The broad MCP-binary search companion to :func:`node_bin_dirs`: a
    globally-installed MCP binary (``npm i -g``) lands in the bin dir of
    whichever Node version was active at install time, so PATH-based discovery
    (:func:`augmented_path`) must see every version's bin dir — narrowing to
    the best version per root would silently stop finding binaries installed
    under a non-best version, with no error message. Dirs are included when
    they exist; unlike the build tier they are NOT required to hold ``node``
    (see :func:`_manager_version_bin_dirs` for the policy split).

    Ordered best version first within each manager root — numeric versions
    outrank alias names (:func:`_node_version_key`), so ``24.16.0`` is searched
    before an ``lts-krypton`` alias rather than after it.

    Cached for the process lifetime via :func:`_node_all_bin_dirs` (keyed on
    the live ``home``/``mise_data``), matching :func:`node_bin_dirs`: the
    filesystem glob must run exactly once — repeating it risks a GIL-contention
    wedge. A Node version installed while the long-lived gateway is running is
    not visible until restart; call ``_node_all_bin_dirs.cache_clear()`` if it
    ever needs re-discovery without one.
    """
    home = os.path.expanduser("~")
    return _node_all_bin_dirs(home, mise_data_dir(home))


def node_augmented_path(base_path: str = "") -> str:
    """Return *base_path* with :func:`node_bin_dirs` PREPENDED.

    Prepended, not appended: a distribution's system ``node`` can be older than
    what ``website/package.json`` declares in ``engines`` (Amazon Linux 2023
    ships node 18 against a ``>=22`` requirement), whereas
    ``ensure-node.sh`` installs a version chosen to satisfy the build. Where
    both exist the managed toolchain is the one that works.
    """
    parts = [*node_bin_dirs()]
    if base_path:
        parts.append(base_path)
    return os.pathsep.join(parts)


def find_node_tool(name: str, base_path: str | None = None) -> str | None:
    """Resolve a node-toolchain executable (``npm``, ``node``, ``npx``) absolutely.

    Searches :func:`node_bin_dirs` first, then *base_path* (default: the
    inherited ``PATH``). Returns ``None`` when the tool is nowhere -- callers
    must surface an actionable message rather than spawning a bare name and
    letting the OS raise ``FileNotFoundError``.

    Absolute by design: on Windows npm is ``npm.CMD``, which PATHEXT-aware
    ``shutil.which`` finds but ``CreateProcess`` cannot spawn by bare name.
    """
    base = os.environ.get("PATH", "") if base_path is None else base_path
    return shutil.which(name, path=node_augmented_path(base))


def _ensure_node_script() -> Path | None:
    """Locate the bundled ``ensure-node.sh``, or ``None`` on a wheel install.

    Search order mirrors :func:`kiro_crew.cli._ensure_node`: the explicit
    ``KIROCREW_PROJECT_DIR`` first, then the source-tree root two levels above
    this module. A pip/wheel install ships no shell script, so this returns
    ``None`` and the caller falls back to whatever Node is already on PATH.
    """
    env_dir = os.environ.get("KIROCREW_PROJECT_DIR")
    candidates = (
        Path(env_dir) / "ensure-node.sh" if env_dir else None,
        Path(__file__).resolve().parent.parent.parent / "ensure-node.sh",
    )
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    return None


def ensure_node(timeout: float = 180.0) -> str | None:
    """Guarantee a usable ``node`` is resolvable, bootstrapping it if needed.

    Returns the absolute ``node`` path when one is (or becomes) available, else
    ``None``. Resolution: use an already-resolvable Node; otherwise invoke the
    bundled ``ensure-node.sh`` (mise / nvm / the nodejs glibc-217 tarball on old
    hosts), which records its bin dir in the ``node-bin-dir`` marker
    :func:`node_bin_dirs` reads — so a freshly bootstrapped toolchain is found
    without a restart. On Windows, where the bash installer cannot run, this only
    reports what is already present.

    Blocking (spawns a subprocess and walks the filesystem) — never call it on
    the event loop; offload with ``asyncio.to_thread`` / ``run_in_executor``.
    """
    node = find_node_tool("node")
    if node:
        return node
    script = _ensure_node_script()
    if script is None or platform_compat.IS_WINDOWS:
        return None
    try:
        subprocess.run(["bash", str(script)], timeout=timeout, capture_output=True)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("ensure-node.sh failed: %s", type(exc).__name__)
        return None
    node_bin_dirs.cache_clear()  # the marker/bin dir may have just appeared
    _node_all_bin_dirs.cache_clear()
    return find_node_tool("node")


@functools.lru_cache(maxsize=1)
def is_toolbox_install() -> bool:
    """Return True if the running kirocrew binary was installed via Toolbox."""
    exe = Path(sys.executable).resolve()
    toolbox_dir = (Path.home() / ".toolbox").resolve()
    try:
        exe.relative_to(toolbox_dir)
        return True
    except ValueError:
        return False


@functools.lru_cache(maxsize=1)
def git_build_info() -> tuple[str, str]:
    """Return ``(branch, short_commit)`` for the running source checkout.

    Reads ``KIROCREW_PROJECT_DIR`` (the git tree the gateway runs from) and
    shells out to ``git`` once. The result is cached for the process lifetime
    (``lru_cache(maxsize=1)``): the running build's branch and commit cannot
    change without a restart, and status snapshots are emitted on every SSE /
    WebSocket tick, so this must not spawn ``git`` on the hot path repeatedly.

    Returns ``("", "")`` when there is no source tree to inspect — toolbox /
    pip-wheel installs (no ``KIROCREW_PROJECT_DIR`` or no ``.git``) — so callers
    can omit the fields gracefully. Any git failure also fails open to empty
    strings.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj or not (Path(proj) / ".git").exists():
        return ("", "")

    def _run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=proj,
                capture_output=True,
                timeout=5,
                **UTF8_TEXT,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    return (
        _run("rev-parse", "--abbrev-ref", "HEAD"),
        _run("rev-parse", "--short", "HEAD"),
    )


def augmented_path(base_path: str = "", *, home: str | None = None) -> str:
    """Return *base_path* prepended with well-known MCP binary directories.

    ``home`` pins user-relative candidates for callers already resolving a
    specific account instead of whichever account owns the current process.

    When KiroCrew runs under systemd or another non-login shell the
    inherited ``$PATH`` rarely includes directories like
    ``~/.local/bin``.  Both the MCP-probe code and the kiro-cli
    spawn code need the same augmentation — this helper keeps them in
    sync.

    On Windows a launched (non-shell) gateway inherits a ``PATH`` that does
    not include the venv's ``Scripts\\`` directory, so ``shutil.which`` fails
    to resolve the ``kirocrew`` / ``kirocrew-core`` console-script wrappers
    pip generated for MCP-server spawn. Append ``sys.executable``'s parent
    directory as the LAST entry so the running interpreter's own
    console-scripts (``Scripts\\`` on Windows, ``bin/`` on POSIX) are always
    discoverable. Last, not first: the interpreter dir also contains
    ``python``/``pip``, and placing it ahead of ``base_path`` would silently
    rebind a user MCP spec's bare ``"command": "python"`` (and the spawned
    agent's own ``python``/``pip`` shell calls) to the gateway's venv
    interpreter. As a pure fallback it resolves only names found nowhere
    else — exactly the console-script-wrapper case.

    Deliberately NOT extended by ``mcp.extra_path_dirs`` /
    :func:`register_mcp_path_dirs`: callers here include the resolvers for the
    trusted agent runtime (``kiro_cli.known_kiro_cli_dirs``,
    ``acp.client``'s claude-backend resolvers), which must not consume a
    contributed directory. That contribution lives on :func:`mcp_search_path` —
    see the section comment near ``_registered_path_dirs``.
    """
    resolved_home = home or os.path.expanduser("~")
    mise_data = mise_data_dir(resolved_home)
    # Filter each formatted entry through the same absolute-only validation as
    # the other PATH sources (_validated_bin_dir): a relative MISE_DATA_DIR
    # would otherwise put a relative "{mise_data}/shims" entry on every spawned
    # subprocess's PATH, re-resolved against the CHILD's cwd — letting a
    # work-dir-relative executable shadow the configured command.
    extra = [
        e
        for d in _EXTRA_PATH_DIRS
        if (e := _validated_bin_dir(d.format(home=resolved_home, mise_data=mise_data)))
    ]
    extra += _node_all_bin_dirs(resolved_home, mise_data)
    parts = extra + ([base_path] if base_path else [])
    parts.append(str(Path(sys.executable).parent))
    return os.pathsep.join(parts)


#: How many directories :func:`describe_search_path` names before truncating.
#: The effective search path carries a bin dir per installed Node version, so an
#: unbounded dump would push the actionable first entries out of a log reader's
#: view.
_SEARCH_PATH_REPORT_LIMIT = 40

#: Appended to "MCP command not found" warnings. Naming the directories searched
#: tells a reader the binary is installed somewhere uncovered; this tells them
#: what to do about it, so the diagnosis and the remedy arrive together instead
#: of the remedy living only in the source (issue #5083).
MCP_PATH_HINT = "if it is installed elsewhere, add that directory to mcp.extra_path_dirs"


def describe_search_path(path: str) -> str:
    """Render *path* as a human-readable list of searched directories.

    ``command not found: <name>`` never said WHERE it looked, so a reader could
    not tell "this install directory is not covered by the search path" from
    "this binary is not installed" -- two different problems with two different
    fixes -- without reading the source. Every caller formats the search path
    through here so the two failures read differently everywhere they are
    reported.

    Truncated at :data:`_SEARCH_PATH_REPORT_LIMIT`; the count is always stated,
    so a truncated tail is visible rather than silent.
    """
    dirs = [d for d in path.split(os.pathsep) if d]
    if not dirs:
        return "searched no directories (empty PATH)"
    shown = dirs[:_SEARCH_PATH_REPORT_LIMIT]
    suffix = f" (+{len(dirs) - len(shown)} more)" if len(dirs) > len(shown) else ""
    return f"searched {len(dirs)} directories: {', '.join(shown)}{suffix}"


def _dedup_dirs(dirs: Iterable[str]) -> list[str]:
    """Drop repeated (and empty) directory entries, keeping the first of each.

    Entries are compared through ``normcase(normpath(...))`` but emitted in
    their original spelling. On Windows ``os.path`` IS ``ntpath``, so that folds
    case and separator flavour together -- ``C:\\Tools`` and ``C:/tools`` name
    one directory, and emitting both would put two spellings of it on the
    child's PATH. Matches the normalization :func:`node_bin_dirs` applies for
    the same reason. The original spelling is kept rather than the normalized
    one so the value stays byte-comparable against what a caller authored.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in dirs:
        if not entry:
            continue
        key = os.path.normcase(os.path.normpath(entry))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def dedup_path(path: str) -> str:
    """Drop repeated entries from a ``PATH`` string, keeping the first of each.

    First-wins so precedence is preserved, and so :func:`spec_env_path` is
    idempotent: feeding an already-expanded value back in contributes only
    duplicates, which collapse to the same string. Comparison rules are
    :func:`_dedup_dirs`'.
    """
    return os.pathsep.join(_dedup_dirs(path.split(os.pathsep)))


def _spec_path_entries(env_path: str) -> list[str]:
    """The usable entries of a spec-authored ``PATH``, in order.

    Drops anything not absolute, applying to caller-authored entries the rule
    :func:`_validated_bin_dir` already applies to this module's own well-known
    dirs: a relative entry is re-resolved against the CHILD's cwd, so it lets a
    work-dir-relative executable shadow the configured command -- and a spec's
    entries lead the emitted PATH, which is the strongest position to shadow
    from. NUL is rejected for the same reason it is there: it cannot survive
    ``execve``, so keeping it only converts a bad entry into a failed spawn.
    """
    out: list[str] = []
    for entry in env_path.split(os.pathsep):
        if not entry:
            continue
        if not os.path.isabs(entry) or "\0" in entry:
            logger.debug("ignoring non-absolute MCP spec PATH entry: %r", entry)
            continue
        out.append(entry)
    return out


def spec_env_path(env_path: str) -> str:
    """Expand an MCP spec's ``env.PATH`` into the PATH its child actually needs.

    A spec's ``env`` is applied per key by whatever spawns the server, so
    declaring ``PATH`` REPLACES the inherited one for that child instead of
    extending it. This module's own backend spawn takes that shape
    (``mcp_gateway.gatewayd`` builds its child env as ``dict(env)`` then
    ``update(declared)``), and the pptx-maker engine already composes a
    COMPLETE ``PATH`` into its spec's ``env`` for the same reason -- see
    ``pptx_maker.backend.provision.mcp_tools_path``, whose docstring notes that
    nothing the gateway does to its own subprocesses reaches a server the agent
    CLI spawns.

    So a spec that names one directory to add -- a Node version manager's shim
    dir, say -- hands the server a PATH holding *only* that directory, and
    anything the server resolves at runtime disappears. The failure is silent
    and asymmetric: a launcher that is itself a wrapper (``exec
    <sibling-binary> ...``) dies with "not found" for a binary that is plainly
    installed, while the dashboard probe -- which merged rather than replaced --
    reports the same server healthy. Nothing in the UI can distinguish that from
    a working server.

    Expanding the value before it is written into the agent config closes the
    gap: the child is launched with the PATH the probe validates and the command
    resolves against, so "probes healthy" and "works in a session" cannot
    diverge.     The spec's own entries stay FIRST, ahead of both the inherited PATH
    and the augmentation, so a spec that pins a toolchain still wins.

    Idempotent: re-expanding an already-expanded value contributes only
    duplicates, which :func:`dedup_path` collapses. That matters because the
    agent config is rewritten on every gateway start.

    The result is a SNAPSHOT of the rebuild-time environment, so it names this
    host's directories (the mise data dir, each installed Node version's bin,
    the running interpreter's bin) and two starts can legitimately differ if the
    host's own PATH or installed toolchains changed. That is the cost of the
    only lever available for a child this process does not spawn: the config
    handed to the spawner. A config carrying an expanded value is therefore not
    portable to another machine, and the emitted PATH is long enough that
    reading it by eye is unpleasant.

    A non-string value degrades to no override rather than raising. This runs
    once per candidate for every server on every rebuild, so a single
    malformed ``env.PATH`` in any config file would otherwise turn one bad
    entry into a failed gateway start.

    Only for a spec that already declares ``env.PATH``: one that does not
    inherits a usable PATH untouched, so it is left alone and keeps a config
    that stays portable.
    """
    if not isinstance(env_path, str):
        logger.debug("ignoring non-string MCP spec PATH: %s", type(env_path).__name__)
        env_path = ""
    parts = [*_spec_path_entries(env_path), augmented_path(os.environ.get("PATH", ""))]
    return dedup_path(os.pathsep.join(filter(None, parts)))


def mcp_search_path(env_path: str) -> str:
    """:func:`spec_env_path` plus the contributed MCP directories (issue #5083).

    The path an MCP command is RESOLVED against -- the probe, the agent-config
    command resolver, and gatewayd's rewriter all use this one. Separate from
    :func:`spec_env_path` on purpose, and the separation is the whole safety
    argument for the feature, on two counts:

    * :func:`spec_env_path`'s result is PERSISTED. :func:`emit_env` writes it into
      consumed config files (the agent config, the kiro-global ``mcp.json``, the
      Claude Code sidecar), and those files are read back as a spec's authored
      ``env.PATH`` on the next rebuild -- which is why that function documents
      being idempotent under re-expansion. A contributed directory written there
      would become indistinguishable from an authored entry, so clearing
      ``mcp.extra_path_dirs`` could never remove it again. Resolution is
      recomputed every time and stored nowhere, so a removed directory stops
      being searched immediately.
    * It also keeps the contribution off :func:`augmented_path`, whose callers
      include the resolvers for the trusted agent runtime -- see the section
      comment near ``_registered_path_dirs``.

    Contributed dirs sit BETWEEN the spec's own entries and the generic
    augmentation: a spec that pins a toolchain still wins, while a directory an
    operator or a packaged build named explicitly outranks this module's built-in
    guesses. ``dedup_path`` collapses the overlap a contributed directory has
    with the built-in list.
    """
    extra = _extra_mcp_path_dirs()
    if not extra:
        # Byte-identical to the unextended path when nothing is contributed, so
        # an install that never uses the setting cannot be perturbed by it.
        return spec_env_path(env_path)
    if not isinstance(env_path, str):
        logger.debug("ignoring non-string MCP spec PATH: %s", type(env_path).__name__)
        env_path = ""
    parts = [
        *_spec_path_entries(env_path),
        *extra,
        augmented_path(os.environ.get("PATH", "")),
    ]
    return dedup_path(os.pathsep.join(filter(None, parts)))


# Env keys a spec's declared ``env`` must never set on a process WE spawn.
#
# Both families execute attacker-controlled code in the LAUNCHER — the process
# that goes on to establish confinement — so they run before any sandbox exists:
#
# * ``LD_*`` / ``DYLD_*`` are dynamic-loader channels honoured by every
#   ELF/Mach-O binary in the spawn chain, the sandbox wrapper included.
# * The ``PYTHON`` namespace matters because Kiro Crew's Linux sandbox launcher
#   IS a Python process: ``sandbox.namespace_argv`` returns
#   ``[sys.executable, "-I", "-S", <generated script>, *argv]`` (sandbox.py), and
#   that interpreter starts with the env we hand ``Popen``. A declared
#   ``PYTHONPATH`` carrying ``sitecustomize.py`` — or a shadowing ``os.py`` —
#   would be imported at interpreter startup, i.e. before ``unshare`` and before
#   the target is exec'd. ``PYTHONSTARTUP``/``PYTHONHOME`` are the same channel.
#   ``PYTHONUSERBASE`` is too: it relocates user-site, whose ``.pth`` files are
#   EXECUTED during startup.
#
# NOTE the ``PYTHON`` entry covers that whole namespace by prefix, but this is
# still a PREFIX set rather than a general glob: it does not cover ``HOME``,
# which also derives the user-site path when ``PYTHONUSERBASE`` is unset, and
# stripping ``HOME`` from a spec overlay would break servers that legitimately
# need it. The launcher's ``-I -S`` is what
# actually closes that class (site processing never happens, so no ``.pth`` runs
# whatever the paths point at); these prefixes are defense in depth for it and the
# primary control for any future launcher that forgets those flags.
#
# ``PYTHON`` is denied as a NAMESPACE, not as a list of the dangerous names in
# it, for the same reason ``KIROCREW_`` is below: an enumeration cannot cover the
# variable nobody has added yet. ``python3 --help-env`` documents 29 ``PYTHON*``
# variables on 3.12 alone, plus platform-only spellings like
# ``PYTHONEXECUTABLE``, and several are execution channels rather than
# preferences -- ``PYTHONPATH`` places a ``sitecustomize`` module,
# ``PYTHONSTARTUP`` names a file, ``PYTHONBREAKPOINT`` takes a dotted callable
# and imports it, ``PYTHONWARNINGS`` resolves a dotted filter category through
# ``warnings._getcategory``'s ``__import__``, ``PYTHONHOME`` and
# ``PYTHONPLATLIBDIR`` move where the standard library is found. Naming them
# one at a time was tried and did not converge: this set reached six entries by
# accretion while 23 documented siblings stayed open.
#
# The cost is a benign ``PYTHON*`` variable in a spec being refused --
# ``PYTHONUNBUFFERED=1`` on a user's own Python MCP server is the realistic
# example. That is a logged refusal rather than a broken server (see the
# asymmetry note below), and ``denied_spec_env_keys`` names the key so the probe
# explains itself instead of reading as a bug. Every first-party ``PYTHON*`` use
# in this tree sets the variable directly on a child process rather than
# declaring it in a spec ``env``, so none of them route through here.
#
# Prefix-matched, case-insensitively (Windows env is case-insensitive).
#
# KNOWN ASYMMETRY, accepted deliberately: ``emit_env`` does NOT strip these, so
# a kiro-cli session still receives a declared ``PYTHONPATH`` — kiro-cli spawns
# the server itself and no Python launcher of ours is in that chain. A Python
# MCP server configured through ``env.PYTHONPATH`` therefore works in a session
# while its PROBE reports an error, which is a visible, logged inconsistency
# rather than a silent one (each dropped key warns). Closing it properly means
# teaching the launcher to apply child env AFTER confinement, which belongs to
# the sandbox module; letting the variable into the launcher instead would trade
# a reporting inconsistency for arbitrary unsandboxed execution.
_SPEC_ENV_DENIED_PREFIXES: tuple[str, ...] = (
    "LD_",
    "DYLD_",
    "PYTHON",
)

# The env namespace Kiro Crew reserves for ITSELF. A spec-declared ``env`` may
# not set any key in it.
#
# This is an AUTHORIZATION boundary, and it is a different class from the loader
# prefixes above — not a variant of them. Several ``KIROCREW_*`` variables are
# how the gateway tells a process it spawns WHO IS CALLING, and the consumers
# treat them as vouched-for:
#
# * ``KIROCREW_CLI`` — no consumer reads it. It is covered because the deny is on
#   the NAMESPACE rather than on a list of keys, which is the same property that
#   covers the next identity variable somebody adds.
# * ``KIROCREW_SESSION_KEY`` / ``KIROCREW_HOST_PID`` — two of the three sources
#   ``mcp_core._resolve_session_key_strict()`` accepts, chosen precisely because
#   they are, in its own words, sources "the gateway authors and an agent cannot
#   write". A spec overlay authoring one makes that claim false, which is worse
#   than the loader class: it corrupts the resolver the ownership checks are
#   built on rather than the process that hosts them. These two are the live
#   reason this boundary exists.
# * ``KIROCREW_OWNER_ID`` / ``KIROCREW_INTERNAL_SECRET`` — the Slack owner
#   identity and the loopback shared secret. ``cron_script`` already denies these
#   two on its own path; putting them here extends the same control to the probe,
#   which had no such deny.
#
# Denying the NAMESPACE rather than those five keys is deliberate. Dozens of
# ``KIROCREW_*`` variables are read across the tree today, several of them
# security-relevant beyond identity (sandbox level, approval mode, admission
# policy), and a key-by-key list fails open for the next one somebody adds —
# the reviewable property we want is "a config cannot author our namespace",
# which a prefix states and a list only approximates.
#
# Nothing legitimately needs the namespace in a spec overlay. Every caller of
# this sanitizer builds its child env from ``os.environ`` FIRST and overlays the
# spec on top (``cron_script._clean_cron_env()``, ``mcp_discovery``'s
# ``dict(os.environ)``), so a gateway-authored ``KIROCREW_*`` value is INHERITED
# either way. Denying it here removes only a config file's ability to OVERRIDE
# one — exactly the capability being abused, and nothing else. Kiro Crew's own
# spawn paths set these variables directly on the child env
# (``apps.backend``'s ``KIROCREW_APP_NAME``, the gateway's
# ``KIROCREW_MCP_TARGET_*``), never by declaring them in an ``mcpServers``
# ``env`` block, so no first-party surface depends on the overlay either.
#
# Prefix-matched case-insensitively, like the loader set above.
_SPEC_ENV_RESERVED_PREFIXES: tuple[str, ...] = ("KIROCREW_",)


def sanitize_spec_env(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Drop loader-injection and reserved-namespace keys from a spec env.

    For paths that let a config-declared ``env`` reach a child process. Two
    shapes qualify and both are covered: a spawn path that launches the child
    ITSELF (the probe), and a path that EMITS the env into an agent spec for
    ``kiro-cli`` to launch from (``agent._enforce_managed_mcp_ownership``, for
    a managed server's entry merged out of the agent-writable
    ``agent.json``). The launcher's identity does not change the argument --
    what matters is that config-file text becomes a child's environment.

    That second caller does NOT disturb the ``emit_env`` asymmetry noted above.
    The asymmetry protects a USER'S OWN Python MCP server, which may
    legitimately be configured through ``env.PYTHONPATH``; the managed
    population is ours, its ``command``/``args`` are ours, and it runs as the
    entry point holding the internal API secret, so no declared loader or
    namespace variable on it is legitimate in the first place.

    The declared env is config-file
    text -- the same trust level as the command itself, which those paths
    already refuse to run unsandboxed -- so a key that executes code in the
    launcher before confinement is established must not pass through.

    Two independent classes are dropped, for two different reasons:

    * ``_SPEC_ENV_DENIED_PREFIXES`` — loader/interpreter channels that execute
      code in the launcher before confinement exists.
    * ``_SPEC_ENV_RESERVED_PREFIXES`` — Kiro Crew's own namespace, which carries
      the caller identity our authorization checks read. Sandboxing does not
      mitigate this one at all: confinement bounds what the child may touch, not
      whose jobs Kiro Crew believes the child is entitled to. That is why "the
      command is equally config-authored, and it runs sandboxed" does not settle
      this class the way it settles the command itself. This prefix set is depth,
      not the authorization control, and it cannot be: it stops the ``env`` block
      from spelling an identity key, while the same config's ``command`` field
      reaches the identical consumer (``sh -c 'KIROCREW_SESSION_KEY=... exec
      ...'``). The control belongs at the CONSUMER, which must not grant
      authority from ambient environment at all. Keep this set regardless:
      ``KIROCREW_SESSION_KEY`` / ``KIROCREW_HOST_PID`` have legitimate producers,
      so for them the consumer-side answer is a strict resolver, and this prefix
      keeps a config out of the overlay it reads.

    Matching is case-INSENSITIVE on purpose: Windows environment variables
    are case-insensitive, so ``pythonpath`` reaches Python exactly like
    ``PYTHONPATH`` there. On POSIX a lowercase spelling is inert, and
    dropping it anyway costs a benign oddly-named variable at most — the
    asymmetry (fail closed everywhere vs. bypass on one OS) decides it.
    Dropped keys are logged at WARNING: a spec relying on one is broken by
    policy, not by accident, and silence would read as the credential-drop
    bug this sanitizer's caller exists to fix.

    An entry whose key or value is not a string is dropped for a different
    reason -- not policy but type. The signature promises ``str -> str`` and
    every caller builds ``pairs`` from parsed JSON, so without this the
    ill-typed value reaches an emitted spec whose schema is a string map.
    """
    out: dict[str, str] = {}
    for key, value in pairs:
        # Honor this function's own str -> str signature. Every caller builds
        # `pairs` from parsed JSON, where a value may be any JSON type, so an
        # `env` of {"PORT": 3000} would otherwise be copied through into an
        # emitted spec whose schema is a string map -- and a spec kiro-cli
        # rejects costs the user every Crew MCP tool, from one mistyped config
        # value. Dropped rather than coerced with str(), matching how a
        # malformed non-dict `env` is dropped by the managed-entry caller: a
        # value we invent is not the one the user wrote. A non-string KEY is
        # unreachable from JSON (object keys are always strings) but would
        # crash `key.upper()` below, so it is guarded in the same place.
        if not isinstance(key, str) or not isinstance(value, str):
            logger.warning(
                "dropping spec env entry %r: keys and values must be strings, got "
                "key %s and value %s",
                key,
                type(key).__name__,
                type(value).__name__,
            )
            continue
        folded = key.upper()
        if any(folded.startswith(p) for p in _SPEC_ENV_DENIED_PREFIXES):
            logger.warning(
                "dropping spec env key %r: loader/interpreter injection channel", key
            )
            continue
        if any(folded.startswith(p) for p in _SPEC_ENV_RESERVED_PREFIXES):
            # Distinct message on purpose: reporting a forged KIROCREW_CLI as a
            # "loader injection channel" would send the next reader looking for a
            # sandbox-escape that is not there, and hide the one that is.
            logger.warning(
                "dropping spec env key %r: the KIROCREW_ namespace is reserved for "
                "the gateway's own caller-identity channel and cannot be declared "
                "by a config",
                key,
            )
            continue
        out[key] = value
    return out


def denied_spec_env_keys(env: "Mapping[str, object]") -> list[str]:
    """The keys :func:`sanitize_spec_env` would drop from *env*, in spec order.

    Exists so a caller can EXPLAIN itself. The sanitizer's WARNING lands in the
    gateway log, which is not where someone staring at a red status badge is
    looking: a Python server configured through ``env.PYTHONPATH`` probes as an
    error while working fine in a session, and without naming the dropped key
    that reads as a probe bug rather than a policy decision.

    Deliberately covers the LOADER prefixes only, not
    ``_SPEC_ENV_RESERVED_PREFIXES``. The consumer
    (``mcp_discovery._note_denied_env``) tells the reader the drop happens
    because the key "execute[s] in the sandbox launcher before confinement, so
    the probe cannot honour them — a session still does", and every clause of
    that is false for a reserved key: it is not a launcher-execution channel, and
    a session must not honour a forged caller identity either. Reserved-namespace
    drops are therefore log-only. A spec declaring one is a policy violation to
    be recorded, not a working server to be apologised to — so this stays the
    "here is why your legitimate config was refused" surface, and does not become
    a hint sheet for the identity boundary.
    """
    return [
        k
        for k in env
        if isinstance(k, str)
        and any(k.upper().startswith(p) for p in _SPEC_ENV_DENIED_PREFIXES)
    ]


def spec_path_key(env: "Mapping[str, object]") -> str | None:
    """The key under which *env* declares a PATH, or ``None``.

    Windows environment variables are case-insensitive, so a spec written on a
    Windows host legitimately says ``"Path"`` (the spelling ``os.environ`` and
    the Windows shells themselves use) and the child's loader treats it as
    PATH. An exact ``"PATH"`` lookup therefore misses it: the fragment would be
    emitted verbatim and REPLACE the child's inherited PATH — the exact
    "declared a fragment, lost everything else" failure this module exists to
    prevent, just spelled differently.

    Matched case-insensitively on every platform rather than only on Windows: a
    config file is portable, and one authored on Windows must not behave
    differently after being copied to a POSIX host. The key the caller wrote is
    returned so a reader can fetch the value; :func:`emit_env` then writes the
    expanded result under the canonical ``PATH``, because that is the only
    spelling a POSIX child honours and the probe applies it under that name.

    A spec carrying BOTH spellings is ambiguous — the OS would pick one and this
    code cannot know which — so the exact ``PATH`` wins, which is what a POSIX
    child would do.
    """
    if "PATH" in env:
        return "PATH"
    for key in env:
        if isinstance(key, str) and key.upper() == "PATH":
            return key
    return None


def emit_env(env: dict) -> dict:
    """Normalize an MCP spec's ``env`` for emission into a consumed config file.

    The single normalization point for every surface some OTHER process
    launches MCP servers from: the agent config (kiro-cli sessions), the
    kiro-global ``mcp.json`` (ACP runtime), and the Claude Code ``~/.mcp.json``
    sidecar. Each of those spawners applies a declared ``env`` per key, so a
    declared ``PATH`` replaces the child's inherited one — the same premise
    :func:`spec_env_path` documents. Routing every writer through one function
    is what keeps the surfaces from diverging: a writer that forgets to expand
    is a server that starts under the probe and dies in a session.

    Returns a NEW dict; the caller's env (typically a source config's own
    object reached through a shallow copy) is never mutated through. The
    ``PATH`` branches, exhaustively: a STRING — empty included — is expanded
    via :func:`spec_env_path`, because that is exactly what the probe and the
    command resolver do with it (``spec_env_path("")`` yields the augmented
    inherited PATH), and emitting the raw empty string instead hands the
    session a child with NO path at all while the probe shows green — the
    divergence this function exists to close. A NON-string passes through
    verbatim: rewriting a malformed value would hide the config error behind
    a working-looking PATH, and the consumer's own rejection is the honest
    surface for it. Every other key passes through untouched.

    The PATH key is found case-insensitively (see :func:`spec_path_key`) and the
    expanded value is emitted under the CANONICAL ``PATH``, with any
    alternate-case spelling dropped. Canonicalizing rather than preserving the
    author's spelling is what keeps the probe and the session in agreement: the
    probe applies a declared search path as ``PATH`` (the only spelling a POSIX
    child honours), so emitting ``Path`` would hand the session a junk variable
    while the probe pinned the real one — the divergence this whole path
    exists to close, reintroduced by spelling. On Windows the two names are the
    same variable, so nothing changes there; on POSIX the child gains the pin
    ON TOP of its inherited PATH (``spec_env_path`` always appends the
    augmented inherited value), so nothing is lost either.
    """
    key = spec_path_key(env)
    if key is None:
        return dict(env)
    path = env[key]
    if not isinstance(path, str):
        # Malformed value: pass the whole env through untouched, author's
        # spelling included, so the config error stays visible.
        return dict(env)
    out = {k: v for k, v in env.items() if not (isinstance(k, str) and k.upper() == "PATH")}
    out["PATH"] = spec_env_path(path)
    return out


@functools.lru_cache(maxsize=1)
def _mise_bin() -> str | None:
    """Locate the ``mise`` binary in a non-login (daemon) context.

    A systemd / launchd gateway does not source the user's shell rc, so
    ``~/.local/bin`` (mise's default install dir) is often absent from the
    inherited ``$PATH``.  Try ``$PATH`` first, then fall back to the canonical
    install location before giving up.
    """
    found = shutil.which("mise")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "mise"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def activate_mise(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Merge mise's resolved environment into *env* (defaults to ``os.environ``).

    Run once at gateway start so every subprocess the gateway later spawns —
    MCP servers, script crons, kiro-cli — inherits the user's mise-managed
    toolchain (Node, Python, kubectl, …) exactly as an interactive shell
    would.  This prevents the most common MCP failure mode: a Node-based MCP
    server spawned against the system ``/usr/bin/node`` (v18 on AL2) instead of
    the user's mise ``node@20+``, which exits during ``initialize`` with a
    stderr-only "Node version 18 detected, but version 20 or higher is
    required" error and surfaces only as "MCP server disconnected during
    'initialize' call".

    Best-effort and non-fatal: a no-op (returns ``[]``) when mise is not
    installed, when disabled via ``KIROCREW_NO_MISE``, or when invoking /
    parsing mise fails — the gateway always starts regardless.  Returns the
    sorted list of env var names that were added or changed, for logging.

    ``mise env --json`` returns only the variables mise manages (PATH plus any
    ``[env]`` / tool-provided vars), not the whole environment, so the merge is
    bounded.  We pass the current env in and resolve from ``$HOME`` so the
    user's *global* mise config is used (not whatever ``.mise.toml`` happens to
    sit in the daemon's cwd), and ``--json`` avoids fragile ``export NAME=VALUE``
    shell-quoting parsing.
    """
    target = os.environ if env is None else env
    if target.get("KIROCREW_NO_MISE"):
        logger.debug("mise activation skipped: KIROCREW_NO_MISE set")
        return []
    mise = _mise_bin()
    if not mise:
        return []
    try:
        proc = subprocess.run(
            [mise, "env", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            env=dict(target),
            cwd=str(Path.home()),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mise activation skipped: %s", type(exc).__name__)
        return []
    if proc.returncode != 0:
        logger.debug(
            "mise env --json exited %s: %s",
            proc.returncode,
            proc.stderr.strip()[:200],
        )
        return []
    try:
        resolved = json.loads(proc.stdout)
    except ValueError as exc:
        logger.debug("mise env --json unparsable: %s", type(exc).__name__)
        return []
    if not isinstance(resolved, dict):
        return []
    changed: list[str] = []
    for key, value in resolved.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if target.get(key) != value:
            target[key] = value
            changed.append(key)
    return sorted(changed)


def resolve_krb5_ccname(env: dict[str, str]) -> None:
    """Point *env* at a FILE: Kerberos ccache, mutating it in place.

    The gateway is a long-lived, non-login process.  On AL2023 the default
    ``krb5.conf`` uses ``KEYRING:persistent:<uid>`` for the ccache, and kernel
    keyrings are session-scoped — they are NOT visible to subprocesses spawned
    by a background daemon.  So a child (kiro-cli / claude / a pooled MCP
    backend) inheriting ``os.environ`` sees no usable ticket, and Kerberos-gated
    MCP servers (e.g. an SSO-backed MCP server) fail with
    "no Kerberos ticket" even though ``kinit`` succeeded in the user's shell.

    This mirrors :func:`_resolve_ssh_auth_sock` in ``acp.client``: repair the
    credential pointer at spawn time rather than trusting the daemon's stale
    env.  Resolution rules:

    * If ``KRB5CCNAME`` already names a non-default scheme (``FILE:`` operator
      override, or a platform-native ``KCM:`` / ``DIR:`` / ``API:`` cache),
      leave it — the caller already has a working, non-keyring ccache.
    * Only act on Linux: the ``/tmp/krb5cc_<uid>`` workaround targets the
      AL2023 ``KEYRING:persistent`` default.  On macOS the default is the
      ``KCM:`` daemon, so blindly pointing at a stale ``/tmp`` file (e.g. left
      by a prior Linux session or container mount) would hijack a working
      ccache — gate the whole thing on ``sys.platform == "linux"``.
    * Else, if ``/tmp/krb5cc_<uid>`` resolves to a regular file we own, point
      at it.
    * Else, do nothing — no ticket to find; let the MCP surface its own
      auth error rather than masking it.

    The candidate lives in ``/tmp`` (world-writable, sticky-bit), so we ``lstat``
    it first and require ownership by the current uid.  We do NOT reject a
    uid-owned symlink: sssd-krb5 / systemd-pam-krb5 legitimately ship
    ``/tmp/krb5cc_<uid>`` as a symlink into ``/run/user/<uid>/krb5cc/...`` — the
    exact keyring-default distros this fix targets.  For a uid-owned symlink we
    follow it (``os.stat``) and require the *resolved* target to be a regular
    file owned by the current uid.  A symlink or file owned by anyone else is
    rejected, which preserves the co-tenant defense (a foreign user cannot plant
    ``/tmp/krb5cc_<victim_uid>`` and have us trust it).

    ``KRB5CCNAME`` is intentionally absent from the MCP-gateway scrub list
    (``mcp_gateway.manager._SENSITIVE_ENV_PREFIXES``), so a value set here
    propagates to pooled backends as well.
    """
    current = env.get("KRB5CCNAME", "")
    # FILE: = explicit operator override; KCM:/DIR:/API: = platform-native
    # schemes (KCM: is the macOS default). Any of these is already a working,
    # subprocess-visible ccache — never override it.
    if current.startswith(("FILE:", "KCM:", "DIR:", "API:")):
        return
    # The /tmp/krb5cc_<uid> workaround only applies to the Linux kernel-keyring
    # default. On macOS/other platforms the keyring-isolation problem does not
    # exist and a stray /tmp file must not hijack the native ccache. Routing
    # through ``platform_compat`` (rather than a raw ``sys.platform`` compare)
    # keeps this consistent with the rest of the codebase's POSIX/Linux gates
    # and gives Windows the same no-op behaviour it needs (no ``os.getuid``).
    if not platform_compat.IS_LINUX:
        return
    # The kernel's default FILE ccache is named by numeric UID
    # (``/tmp/krb5cc_<uid>``) — this is also what the documented workaround
    # ``kinit -c /tmp/krb5cc_$(id -u)`` produces.  Some setups instead use the
    # login name, so check that as a fallback.  ``getpass.getuser()`` is only
    # evaluated for the fallback path.
    candidates = [f"/tmp/krb5cc_{os.getuid()}"]
    try:
        candidates.append(f"/tmp/krb5cc_{getpass.getuser()}")
    except Exception as exc:  # getuser() can raise without a passwd entry / env
        logger.debug("krb5 ccache username fallback skipped: %s", type(exc).__name__)
    rejected: list[str] = []
    for cache in candidates:
        reason = _reject_reason(cache)
        if reason is None:
            env["KRB5CCNAME"] = f"FILE:{cache}"
            logger.debug("resolved KRB5CCNAME to FILE:%s", cache)
            return
        if reason != "absent":
            # A candidate physically exists but failed the ownership/type gate.
            # Log it so this is distinguishable from the plain "no ccache" case —
            # otherwise it reproduces the silent-failure gap this resolver fixes.
            rejected.append(f"{cache} ({reason})")
    if rejected:
        logger.debug("KRB5CCNAME left unset; rejected ccache candidate(s): %s", ", ".join(rejected))


def _reject_reason(cache: str) -> str | None:
    """Return ``None`` if *cache* is a usable FILE ccache, else a rejection reason.

    Accepts a regular file owned by us, or a uid-owned symlink whose resolved
    target is a regular file owned by us (sssd/systemd ship the ccache as a
    symlink into ``/run/user/<uid>/krb5cc/...``).  Rejects anything owned by
    another uid — a co-tenant on a shared ``/tmp`` cannot make us trust a
    planted file or symlink.

    Reasons are coarse, log-only labels (``absent`` means the path does not
    exist, i.e. the ordinary no-op case — callers skip logging it).
    """
    uid = os.getuid()
    try:
        st = os.lstat(cache)  # lstat: inspect the link itself, do not follow yet
    except OSError:
        return "absent"
    if stat.S_ISLNK(st.st_mode):
        # A foreign-owned symlink is an attack vector; a uid-owned one may
        # legitimately point at /run/user/<uid>/krb5cc/... — follow and validate.
        if st.st_uid != uid:
            return "foreign-owned-symlink"
        try:
            st = os.stat(cache)  # resolves the symlink to its target
        except OSError:
            return "dangling-symlink"
        if not stat.S_ISREG(st.st_mode):
            return "symlink-target-not-regular"
        if st.st_uid != uid:
            return "symlink-target-foreign-owned"
        return None
    if not stat.S_ISREG(st.st_mode):
        return "not-regular"
    if st.st_uid != uid:
        return "foreign-owned"
    return None
