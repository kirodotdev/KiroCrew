"""The managed install's layout: where its trees live, and which interpreter to run.

The managed-venv layout and its detection, the pipx check, the interpreter a restart
re-executes and the path a persisted launcher records, and how the trees this engine
builds are named and recognised.

:mod:`kiro_crew.platform.wheel_engine` is the import path and the patch surface: callers
import these names from it and tests patch them there, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries such a patch to this module. This module never
imports the facade.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.platform.tree_liveness import TREE_MARKER
from kiro_crew.platform.wheel_engine_contract import _VERSION_RE, check_release_version
from kiro_crew.platform_compat import IS_POSIX


@dataclass(frozen=True)
class ManagedVenvLayout:
    """Where the managed install's trees live for this data home."""

    #: The legacy fixed venv cli.sh installs into (``crew-venv``). Never
    #: written by this engine; functional fallback per the first-migration
    #: protocol.
    legacy: Path
    #: The stable symlink (``crew-venv-current``) every launch path should
    #: resolve through. May not exist yet on an unmigrated install.
    stable_link: Path

    def versioned_tree(self, version: str) -> Path:
        """The sibling tree a given version installs into.

        Raises :class:`WheelUpdateError` for a version outside the release grammar
        (:func:`check_release_version`).
        """
        check_release_version(version)
        return self.stable_link.with_name(f"{self.legacy.name}-{version}")

    def is_managed_tree(self, path: Path) -> bool:
        """Is *path* inside one of this layout's trees (legacy or versioned)?"""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        prefix = f"{self.legacy.name}-"
        # Identity trust extends ONLY to the legacy root — the one path this
        # layout owns unconditionally. Every versioned sibling (a genuinely
        # promoted tree included) goes through the convention-plus-artifacts
        # branch below, which every real install satisfies (bin/kirocrew ships
        # with every completed build, the sentinel with every in-flight one).
        # The stable link's TARGET is deliberately NOT an identity root: the
        # respawn guard uses this predicate to detect a link repointed OUTSIDE
        # the layout, and trusting the target by identity would make that
        # check vacuously true.
        try:
            legacy_resolved = self.legacy.resolve()
            if resolved == legacy_resolved or resolved.is_relative_to(legacy_resolved):
                return True
        except OSError:
            pass
        # A tree created after this snapshot: matched by naming convention in
        # the same parent — but POSITIVELY identified, never by prefix alone.
        # `KIROCREW_VENV=/srv/crew` makes the prefix "crew-", which a sibling
        # like /srv/crew-dev (someone's unrelated venv) satisfies by name; a
        # restart would then resolve through /srv/crew-current and switch an
        # unmanaged process's runtime. A managed versioned tree additionally
        # carries this engine's own artifacts: the kirocrew console script
        # (every completed install ships it) or the build sentinel (every
        # in-flight build does).
        parent = self.legacy.parent
        try:
            if resolved.is_relative_to(parent.resolve()):
                rel = resolved.relative_to(parent.resolve())
                head = rel.parts[0] if rel.parts else ""
                if head == self.legacy.name:
                    return True
                if not head.startswith(prefix):
                    return False
                candidate = parent / head
                return (candidate / "bin" / "kirocrew").exists() or (
                    candidate / _SHADOW_SENTINEL
                ).exists()
        except OSError:
            pass
        return False


def managed_venv_layout() -> ManagedVenvLayout:
    """Resolve the managed-venv layout for this install.

    Mirrors cli.sh's path rule exactly: ``KIROCREW_VENV`` wins, else the venv
    lives BESIDE the data home (``${KIROCREW_HOME%/}-venv``) — never inside
    it, for the blast-radius reason cli.sh documents.
    """
    override = os.environ.get("KIROCREW_VENV", "").strip()
    if override:
        legacy = Path(os.path.abspath(override))
    else:
        legacy = Path(f"{str(data_home()).rstrip('/')}-venv")
    return ManagedVenvLayout(
        legacy=legacy,
        stable_link=legacy.with_name(f"{legacy.name}-current"),
    )


def running_from_managed_venv(layout: ManagedVenvLayout | None = None) -> bool:
    """Is THIS process served by the managed venv (legacy or versioned tree)?

    The dispatch predicate for the shadow path: a pipx install, a system
    Python, or a dev venv must never take it — their bytes are owned by
    something else, and building a sibling tree beside a data home they do not
    use would be litter at best. POSIX-only by construction: cli.sh is a POSIX
    installer, so on Windows there is no managed venv to detect.

    Identity comes from the interpreter's ``bin/`` directory, not the file:
    ``python -m venv`` symlinks ``bin/python3`` to the base interpreter, so
    resolving that file leaves the venv before the layout can recognize it.
    """
    if not IS_POSIX:
        return False
    if layout is None:
        layout = managed_venv_layout()
    return layout.is_managed_tree(Path(sys.executable).parent)


#: Name of the metadata file pipx writes at the root of every environment it
#: manages (``$PIPX_HOME/venvs/<env>/pipx_metadata.json``). pipx owns it,
#: rewrites it atomically after each operation, and reads it to drive its own
#: ``upgrade``/``reinstall``/``uninstall`` — so its presence beside the running
#: interpreter is an authoritative, version-stable "this venv is pipx's" signal
#: that needs no path-guessing against ``$PIPX_HOME``.
_PIPX_METADATA_NAME = "pipx_metadata.json"


def running_from_pipx() -> bool:
    """Is THIS process served by a pipx-managed environment?

    Distinguishes the one non-managed shape whose installer re-run is
    legitimate — ``cli.sh`` installs into a pipx venv when pipx is present, and
    a re-run upgrades that same pipx venv in place — from a user's own plain
    ``pip install`` into a venv they manage, where a re-run would build a
    SECOND copy beside the one actually serving the user.

    Identity is the metadata file pipx keeps at the environment root. A pipx
    venv's root is ``sys.prefix`` for a process launched from it (the console
    script runs the venv's own interpreter), so the marker sits one directory
    up from ``bin/``. Reading ``sys.prefix`` rather than resolving
    ``sys.executable`` keeps the answer stable across the ``bin/python3 ->``
    base-interpreter symlink that ``python -m venv`` writes.
    """
    try:
        return (Path(sys.prefix) / _PIPX_METADATA_NAME).is_file()
    except OSError:
        return False


def _legacy_nested_venv() -> Path:
    """The venv an earlier ``cli.sh`` created INSIDE the data home.

    Mirrors cli.sh's ``_OLD_VENV`` (``<data home>/venv``) exactly. The current
    installer retires it: a re-run that lands a working tree beside the data
    home repoints the stable link and the launcher at that tree, then
    ``rm -rf``s this one. An installer re-run while a gateway still serves
    from this venv deletes it underneath the running process.
    """
    return data_home() / "venv"


def _respawn_tree_is_managed(layout: ManagedVenvLayout) -> bool:
    """Is the tree serving THIS process one a restart may re-route?

    Identity is read from the interpreter's ``bin/`` directory, not from the
    interpreter file: ``python -m venv`` writes ``bin/python3`` as a symlink
    to the base interpreter, so resolving the file lands outside every venv
    and would answer "not managed" for every real install. The directory
    resolves through the layout's own links (stable link -> versioned tree)
    and stops there.

    Two identities qualify. A tree of this layout (legacy or versioned), and
    the retired in-data-home venv — our own environment from an earlier
    installer, which the same cli.sh re-run that lands the new tree deletes
    from under the running gateway; without the stable link that process has
    no interpreter left to exec. This is the respawn identity only: the
    dispatch predicate for the shadow-build path is
    :func:`running_from_managed_venv`, which keeps its own rule.
    """
    bin_dir = Path(sys.executable).parent
    if layout.is_managed_tree(bin_dir):
        return True
    try:
        resolved = bin_dir.resolve()
        nested = _legacy_nested_venv().resolve()
    except OSError:
        return False
    return resolved == nested or resolved.is_relative_to(nested)


def _interpreter_in(tree: Path) -> str | None:
    """The usable interpreter inside *tree*'s ``bin/``, or ``None``.

    Prefers this process's own interpreter name so a restart keeps the version
    it was launched under, and falls back to ``python3``, which every venv
    ships.
    """
    for name in (os.path.basename(sys.executable), "python3"):
        candidate = tree / "bin" / name
        try:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def _respawn_fallback(layout: ManagedVenvLayout) -> str:
    """The answer when the stable link cannot carry the restart.

    ``sys.executable`` whenever it still exists — a broken, absent, or
    outside-the-layout link must never take the restart path away from a
    process whose own interpreter is fine.

    When it does NOT exist, the cached path names nothing and the exec would
    raise ENOENT. That is reachable: the installer re-run that retires the
    in-data-home venv deletes it on the NEW tree's own import check, which is
    independent of the stable-link repoint — the repoint is skipped when a real
    directory sits at the stable name, and its failure is non-fatal. So the
    nested venv is gone while the link is unusable. The legacy fixed tree is
    the interpreter that re-run just built and import-verified, and this engine
    never prunes it, so it is the honest last resort. Validated the same way as
    the stable link's: inside the layout, present, executable.
    """
    if os.path.exists(sys.executable):
        return sys.executable
    if layout.is_managed_tree(layout.legacy):
        candidate = _interpreter_in(layout.legacy)
        if candidate is not None:
            return candidate
    return sys.executable


def respawn_executable() -> str:
    """The interpreter a gateway restart should exec.

    ``sys.executable`` is the answer for every install shape EXCEPT a managed
    venv that has been promoted past the tree this process started from: there
    the cached path resolves into the OLD versioned tree (still on disk, so
    the exec would succeed — and silently resurrect the old version). Routing
    through the stable link is what makes a restart pick up a promotion, and
    it is one of the four persisted launch paths RFC §3 requires to resolve
    through the stable name.

    Falls back to ``sys.executable`` whenever the stable link does not exist
    or does not carry a usable interpreter, so a broken or absent link can
    never take the restart path away — except when ``sys.executable`` itself is
    already gone, where :func:`_respawn_fallback` reaches the legacy tree
    instead of handing back a path that does not exist.

    One more shape routes here: a process still served by the retired
    in-data-home venv (:func:`_legacy_nested_venv`). The installer re-run that
    migrates it deletes that venv after repointing the stable link, so
    ``sys.executable`` is gone and the stable link is the only interpreter
    left; before that re-run there is no stable link and the fallback keeps
    the restart on the nested venv, unchanged.

    The answer names the link's RESOLVED tree (``crew-venv-<version>/bin/…``),
    never ``crew-venv-current/bin/…``. An interpreter started through the link
    takes its ``sys.prefix``, and so every later import, its own package data,
    its installed metadata and its ``sys.executable``, from the link, and the
    next promotion would swap all of that under the running process. Only the
    directory is resolved: ``bin/python3`` is itself a symlink to the base
    interpreter, and resolving it would leave the venv entirely.
    """
    if not IS_POSIX:
        return sys.executable
    layout = managed_venv_layout()
    if not _respawn_tree_is_managed(layout):
        return sys.executable
    # The link's TARGET must resolve inside this layout's own trees before it
    # is trusted with an exec: a stable link repointed outside the managed
    # parent answers sys.executable instead. Scope honestly stated: this pins
    # WHERE the interpreter may live, not WHO wrote it — an actor with shell
    # as the gateway's user can rewrite the trees themselves (sys.executable's
    # own tree included), which is the RFC's accepted local-code-execution gap
    # and is not widened by the link.
    if not layout.is_managed_tree(layout.stable_link):
        return _respawn_fallback(layout)
    try:
        target = Path(os.path.realpath(layout.stable_link))
    except OSError:
        return _respawn_fallback(layout)
    candidate = _interpreter_in(target)
    if candidate is not None:
        return candidate
    return _respawn_fallback(layout)


def runs_through_stable_link(path: str | None = None) -> bool:
    """Does *path* (``sys.prefix`` by default) name its tree through the stable link?

    Read lexically, the way the interpreter itself keeps it: a process started as
    ``crew-venv-current/bin/python3`` keeps that spelling in ``sys.prefix``, so
    every module it imports later resolves through the link, and the next
    promotion swaps them under it. Only each component's parent is resolved, so
    a symlinked home still matches; the link itself is never followed.
    """
    if not IS_POSIX:
        return False
    layout = managed_venv_layout()
    link_parent = _realpath(layout.stable_link.parent)
    if link_parent is None:
        return False
    link = link_parent / layout.stable_link.name
    lexical = Path(os.path.abspath(sys.prefix if path is None else path))
    for candidate in (lexical, *lexical.parents):
        parent = _realpath(candidate.parent)
        if candidate.name and parent is not None and parent / candidate.name == link:
            return True
    return False


def stable_launch_path(path: str) -> str:
    """*path* rewritten through the stable link when it lies in a versioned tree.

    For a value that is PERSISTED and later executed outside the writing
    process's lifetime (a service ``ExecStart``, a launchd launcher, the
    ``~/.local/bin`` shim): a path inside
    ``crew-venv-<version>/`` would keep naming that tree after the next update
    promoted another one, and the prune may delete it. Through
    ``crew-venv-current/`` it follows every promotion. That holds whichever
    tree the link targets now: a path in an older tree is rewritten too, since
    that tree is the one a later prune removes. Returned unchanged when *path*
    is not inside an engine-built tree of this layout, or when the link is
    dangling or its tree has no file at the same relative path.
    """
    if not IS_POSIX or not path:
        return path
    layout = managed_venv_layout()
    try:
        resolved = Path(os.path.realpath(path))
        parent = Path(os.path.realpath(layout.legacy.parent))
        rel = resolved.relative_to(parent)
    except (OSError, ValueError):
        return path
    if len(rel.parts) < 2 or not _is_owned_tree(layout, parent / rel.parts[0]):
        return path
    through = layout.stable_link.joinpath(*rel.parts[1:])
    try:
        if os.path.exists(through):
            return str(through)
    except OSError:
        pass
    return path


#: Build sentinel: written into a shadow directory the moment this engine
#: creates it, removed only after the tree is promoted. Its PRESENCE says the
#: build never completed; together with :data:`TREE_MARKER` it is what makes a
#: directory deletable debris, whatever else the directory does or does not hold
#: (an interrupted build can leave nothing but these two files).
_SHADOW_SENTINEL = ".kirocrew-shadow-incomplete"

#: Infix of a tree being deleted (``.crew-venv-<v>.deleting-<pid>``). A tree is
#: renamed to a tombstone first and removed after: an interrupted removal of the
#: tree in place would leave a directory under the versioned name that is not a
#: whole venv. The tombstone is deleted with its ownership proofs last
#: (:func:`_remove_tombstone`), so an interrupted removal stays sweepable.
_TOMBSTONE_INFIX = ".deleting-"


def _versioned_name(layout: ManagedVenvLayout) -> re.Pattern[str]:
    """The one spelling of a versioned sibling's name: ``<legacy>-<version>``."""
    return re.compile(re.escape(f"{layout.legacy.name}-") + _VERSION_RE.pattern[1:])


def _realpath(path: Path) -> Path | None:
    try:
        return Path(os.path.realpath(path))
    except (OSError, RuntimeError):
        return None


def _is_stable_target(layout: ManagedVenvLayout, tree: Path) -> bool:
    """Is *tree* what the stable link resolves to? The one form of this check."""
    target, real = _realpath(layout.stable_link), _realpath(tree)
    return target is not None and real is not None and target == real


def _is_owned_tree(layout: ManagedVenvLayout, tree: Path) -> bool:
    """Did THIS layout's engine build *tree*? Never raises; unreadable is "no".

    Positively identified, so another install that shares the parent directory
    (``KIROCREW_VENV=/srv/crew`` beside ``/srv/crew-beta``) is never touched: the
    exact versioned name (a tombstone of one counts), AND either the ownership
    marker naming this layout or, for a build an earlier engine left unfinished,
    no marker at all beside the build sentinel.
    """
    name = tree.name
    if name.startswith("."):
        head, sep, _pid = name[1:].rpartition(_TOMBSTONE_INFIX)
        if not sep:
            return False
        name = head
    if not _versioned_name(layout).fullmatch(name):
        return False
    try:
        if tree.is_symlink() or not tree.is_dir():
            return False
        marker = tree / TREE_MARKER
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip() == str(layout.legacy)
        return (tree / _SHADOW_SENTINEL).exists()
    except (OSError, UnicodeDecodeError):
        return False
