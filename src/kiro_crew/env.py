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
from collections.abc import MutableMapping
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

# Common directories where MCP server binaries may be installed.
# Order matters — earlier entries take precedence.
_EXTRA_PATH_DIRS = (
    "{home}/.local/bin",
    "{home}/.toolbox/bin",
    "{home}/.npm-packages/bin",
    "{home}/.local/share/mise/shims",
    "{home}/.volta/bin",
    "/opt/homebrew/bin",  # Apple Silicon Homebrew node / global npm bins
)


@functools.lru_cache(maxsize=1)
def _node_version_manager_bins(home: str) -> list[str]:
    """Return node bin dirs from version managers with dynamic version paths.

    nvm and fnm install each Node version under a versioned directory, so the
    bin path cannot be a static template in ``_EXTRA_PATH_DIRS``.  Glob the
    install roots and return every ``bin`` dir, newest version first.  A
    non-login gateway (launchd / systemd) does not inherit these on ``$PATH``,
    so adding them lets us find globally-installed MCP binaries such as
    ``claude-agent-acp`` that were installed via ``npm i -g`` under nvm/fnm.

    Cached for the process lifetime (``lru_cache(maxsize=1)``, ``home`` is
    constant per process): the filesystem glob must run exactly once — repeating
    it risks a GIL-contention wedge.  Trade-off: a node version
    installed via nvm/fnm *while the long-lived gateway is running* is not
    visible until the gateway restarts.  Acceptable — installing node mid-session
    is rare, and a restart picks it up.  Call ``cache_clear()`` if that ever
    needs to be re-discovered without a restart.
    """
    bins: list[str] = []
    roots = (
        Path(home) / ".nvm" / "versions" / "node",
        Path(home) / ".fnm" / "node-versions",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for ver_dir in sorted(root.glob("*"), reverse=True):
            bin_dir = ver_dir / "bin"
            if bin_dir.is_dir():
                bins.append(str(bin_dir))
    return bins


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
# Marker file written by ``ensure-node.sh`` recording the node bin dir it
# resolved. The Makefile already consumes it; this keeps Python callers on the
# same answer instead of re-deriving one.
_NODE_BIN_DIR_MARKER = "node-bin-dir"
# Operator escape hatch: an explicit absolute node bin dir, for hosts whose
# toolchain lives somewhere none of the layouts above cover.
_NODE_BIN_DIR_ENV = "KIROCREW_NODE_BIN_DIR"


def _mise_data_dir(home: str) -> str:
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

    Every entry is verified to contain an executable ``node``, so the result is
    only ever real toolchain directories. Only the BEST version per manager root
    is returned: these entries go on the PATH of build subprocesses, and mise
    alone can contribute ~18 install and alias directories on a developer box --
    a PATH that long slows every exec lookup and buries the intended toolchain
    behind stale majors (node 16/18 against a ``20 || >=22`` engines field).

    Why this exists: ``install.sh --mise`` and ``ensure-node.sh`` -- the
    supported install path -- put node under ``$HOME``. A non-login gateway
    (systemd / launchd) does not inherit those on ``$PATH``, and build callers
    additionally pin PATH to system dirs, so without this the build cannot see
    the very node Kiro Crew installed for it.

    Cached for the process lifetime: the globs must run once, matching
    :func:`_node_version_manager_bins`. A node installed while a long-lived
    gateway is running is not seen until restart; call ``cache_clear()`` if it
    ever needs re-discovery without one.
    """
    home = os.path.expanduser("~")
    mise_data = _mise_data_dir(home)
    ordered: list[str] = []

    override = _validated_bin_dir(os.environ.get(_NODE_BIN_DIR_ENV, ""))
    if override:
        ordered.append(override)
    marker = _marker_node_bin_dir()
    if marker:
        ordered.append(marker)

    for pattern in _NODE_MANAGER_GLOBS:
        root, _, leaf = pattern.format(home=home, mise_data=mise_data).partition("/*")
        try:
            matches = sorted(
                (p for p in Path(root).glob("*" + leaf) if _has_node(p)),
                # The version dir is the child of `root`; with a deeper leaf
                # (fnm's `<ver>/installation/bin`) that is not p.parent, so
                # index it off the root instead of walking up a fixed count.
                key=lambda p: _node_version_key(p.relative_to(root).parts[0]),
                reverse=True,
            )
        except (OSError, ValueError):
            continue
        if matches:
            ordered.append(str(matches[0]))

    ordered.extend(d.format(home=home, mise_data=mise_data) for d in _NODE_MANAGER_DIRS)

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


def node_augmented_path(base_path: str = "") -> str:
    """Return *base_path* with :func:`node_bin_dirs` PREPENDED.

    Prepended, not appended: a distribution's system ``node`` can be older than
    what ``website/package.json`` declares in ``engines`` (Amazon Linux 2023
    ships node 18 against a ``20 || >=22`` requirement), whereas
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
                text=True,
                timeout=5,
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


def augmented_path(base_path: str = "") -> str:
    """Return *base_path* prepended with well-known MCP binary directories.

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
    """
    home = os.path.expanduser("~")
    extra = [d.format(home=home) for d in _EXTRA_PATH_DIRS]
    extra += _node_version_manager_bins(home)
    parts = extra + ([base_path] if base_path else [])
    parts.append(str(Path(sys.executable).parent))
    return os.pathsep.join(parts)


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
