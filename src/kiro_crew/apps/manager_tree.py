"""Turning an app's source directory into the tree an install leaves behind.

Validating a source directory and its manifest, the copy itself (what it skips and
which generated dependency artifacts it sweeps), and the rules that keep a preserved
``data/`` directory in place across a copy (``gateway_data_dir_obstruction``,
``copy_app_tree_as_installed``).

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.manager_records import (
    APP_MANIFEST_FILENAME,
    APPROVED_GRANTS_FILENAME,
    INSTALLED_META_FILENAME,
    app_dir,
)
from kiro_crew.apps.manifest import RESERVED_APP_NAME_CODE, AppManifest, is_reserved_app_name
from kiro_crew.platform_compat import is_link_or_junction

logger = logging.getLogger("kiro_crew.apps.manager")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_source_path(source: Path) -> list[str]:
    """Validate that a source directory looks like a valid app."""
    errors: list[str] = []
    manifest_path = source / APP_MANIFEST_FILENAME
    if not manifest_path.is_file():
        errors.append(f"missing {APP_MANIFEST_FILENAME} in {source}")
        return errors
    try:
        manifest = AppManifest.from_json_file(manifest_path)
    except ValueError as exc:
        errors.append(f"invalid {APP_MANIFEST_FILENAME}: {exc}")
        return errors
    errors.extend(manifest.validate(app_root=source))
    # `ui.overlays` replaces a host surface by naming an overlay component compiled
    # into the dashboard bundle. An installed app has no way to supply one -- there is
    # no per-overlay `entryPoint` the way `ui.pages` has -- so accepting the manifest
    # here would install an app whose declaration can only fail later as a browser
    # console warning, the one channel an app author never reads. Refuse at install,
    # which is the channel they do read. Builtins are validated by discovery.py and
    # are unaffected.
    if manifest.ui.overlays:
        errors.append(
            "ui.overlays is not available to installed apps: an overlay must name a "
            "component compiled into the dashboard bundle, so a declaration here can "
            "never render"
        )
    if manifest.minKiroCrewVersion:
        ver_err = _check_min_version(manifest.minKiroCrewVersion)
        if ver_err:
            errors.append(ver_err)
    return errors


def _reserved_name_code(source: Path) -> str:
    """Return ``RESERVED_APP_NAME_CODE`` if *source*'s manifest names a reserved app.

    Called on the ``_validate_source_path`` failure path, where the joined prose
    may bundle several findings — the reserved-name refusal is the one the
    frontend needs to distinguish (it can offer "pick another name", not just
    display English). Parses defensively: an unreadable manifest already failed
    validation for its own reason and carries no code.
    """
    try:
        manifest = AppManifest.from_json_file(source / APP_MANIFEST_FILENAME)
    except (OSError, ValueError):
        return ""
    return RESERVED_APP_NAME_CODE if is_reserved_app_name(manifest.name) else ""


def _check_min_version(min_version: str) -> str | None:
    """Return error string if current Kiro Crew version is too old, else None."""
    from kiro_crew.apps.version import check_min_version

    return check_min_version(min_version)


def _check_path_safety(path: str) -> bool:
    """Return True if a resource path is safe (no traversal).

    Rejects ``..``, ``/``, and ``\\`` to prevent directory traversal
    when the path is used as a key in file-system lookups (e.g.
    ``apps_dir() / name``).
    """
    return ".." not in path and "/" not in path and "\\" not in path


# Build-input / VCS directories never needed at runtime.  The app-kit runtime
# layout is ``app.json`` + backend code + ``ui/dist/`` — ``node_modules`` is
# npm build input and ``.git`` comes from cloned registry sources.
# ``.kirocrew-deps`` (plus its transient staging/prior siblings) is the
# gateway's own ``pip --target`` provisioning of the app's requirements.txt:
# machine- and platform-specific, re-provisioned at the destination on first
# spawn, and copying it would put a foreign wheel tree FIRST on the child's
# PYTHONPATH, shadowing the correctly provisioned copy.
# ``shutil.ignore_patterns`` matches by basename at every depth, so both
# ``node_modules`` and ``ui/node_modules`` are dropped.  ``build`` is
# deliberately NOT listed: the manifest may reference runtime paths anywhere
# under the app root, and silently dropping a manifest-referenced directory
# would record a successful install with missing files.  A ``build`` symlink
# into a huge build tree is already neutralized by ``symlinks=True``.
_COPY_IGNORE = (
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    INSTALLED_META_FILENAME,
    APPROVED_GRANTS_FILENAME,
    ".kirocrew-deps",
    ".kirocrew-deps-staging",
    ".kirocrew-deps-prior",
    ".kirocrew-deps.lock",
)


# The bare fixed name is reserved too (nothing generates it today, but it
# is inside the gateway-owned namespace and a plantable look-alike), so the
# per-transaction suffix is optional. An app-owned name with any OTHER
# suffix shape (e.g. "-assets") does not match and is preserved data.
_DEPS_STAGING_SWEEP_RE = re.compile(r"\.kirocrew-deps-staging(-\d+-[0-9a-f]{8})?")


def _is_generated_deps_artifact_name(n: str) -> bool:
    """True only for the EXACT names the gateway's provisioning generates.

    The uninstall sweep deletes what matches; a loose ``.kirocrew-deps*``
    prefix glob also swallowed app-owned entries that merely share the
    prefix (e.g. a user's ``.kirocrew-deps-backup``) and permanently
    deleted preserved data. Generated names are closed-form: the live tree,
    the prior tree, the lock, and pid-nonce staging dirs.
    """
    return (
        n in (".kirocrew-deps", ".kirocrew-deps-prior", ".kirocrew-deps.lock")
        or _DEPS_STAGING_SWEEP_RE.fullmatch(n) is not None
    )


class InstalledTreeRefused(Exception):
    """The tree a copy produced is one the install turns away.

    Raised by :func:`copy_app_tree_as_installed` on the preview copy the desktop
    gate judges, where :func:`install_app` / :func:`update_app` return the same
    refusal on their own copy -- the one predicate asked in both places
    (:func:`gateway_data_dir_obstruction`), so a preview never admits a tree the
    install refuses. Not an :class:`OSError`: the copy succeeded, the layout is the
    app author's to fix, and a retry cannot change it.
    """


InstalledTreeRefused.__module__ = "kiro_crew.apps.manager"


def gateway_data_dir_obstruction(root: Path) -> str:
    """Why :func:`app_data_dir` could not stand in *root*, or ``""`` when it can.

    The gateway's per-app data directory is ``<app dir>/data``, created with
    ``mkdir(exist_ok=True)`` once the install is otherwise complete, and MOVED --
    aside before every update's copy and back after it, to ``.<name>-data-tmp``
    beside the app directory -- so the runtime's question is two questions: can
    ``mkdir`` stand there, and can the gateway relocate what stands there and
    put it back whole. A real directory answers both. A link answers neither
    safely: ``mkdir`` may follow one that resolves to a directory, but the move
    relocates the LINK, and a relative link moved out of its tree dangles -- the
    directory it named stays inside the tree the update retires and is deleted
    with it, silently, on the update's own success path. So a link at ``data`` is
    refused whatever it resolves to, and so is anything else that is not a
    directory: a regular file, a dangling link, a link to a file. Asked of the
    copied tree AFTER the gateway's own part (a preserved ``data/`` put back over
    the copy) and BEFORE the installed record is written, so a source shipping
    such an entry is refused whole instead of leaving ``installed.json`` behind a
    raise; asked of the preview copy by :func:`copy_app_tree_as_installed`, so
    the desktop gate refuses the same tree before any transaction touches the app
    directory; and asked by :func:`install_app` of an app directory ALREADY
    standing at its destination with no installed record -- a prior default
    uninstall's leftover, or an orphaned partial copy -- BEFORE its transaction
    opens, so a pre-existing link or file at ``data`` is refused instead of being
    unlinked by the orphan cleanup that clears such a directory before the copy
    (the same refusal :func:`update_app` and :func:`uninstall_app` make of that
    shape before they mutate anything); and asked by :func:`update_app` of the
    INSTALLED tree in its preflight, BEFORE its transaction opens, so an installed
    ``data`` that is a link or a file -- from before the refusal, or made by the
    app's own runtime -- is refused with the old tree and record untouched instead
    of being skipped by the move-aside and deleted with the retired tree on the
    update's success path. The sentence names the obstruction: it is
    the only explanation the install log, the ``AppResult`` and the audit record
    carry.
    """
    data = root / "data"
    if not os.path.lexists(data):
        return ""
    if is_link_or_junction(data):
        shape = "link"
    else:
        try:
            if data.is_dir():
                return ""
        except OSError:
            pass  # exists, and cannot be followed to a directory: not one
        shape = "file"
    return (
        f"`data` in the app tree is a {shape}; Kiro Crew creates the app's data "
        "directory at that path and cannot install beside it."
    )


def _owned_data_dir(path: Path) -> bool:
    """A directory the gateway can move aside and put back: a real one, not a link.

    The shape :func:`install_app` and :func:`update_app` preserve across the copy
    and :func:`preserved_data_awaits` predicts. ``is_dir`` alone follows a link
    and would call a relative link a directory, then the move would relocate the
    link out of its tree and lose what it named (see
    :func:`gateway_data_dir_obstruction`). ``is_symlink`` alone misses a Windows
    directory junction, which the move would relocate the same way while the
    tree it names is retired with the old app files, so the link test is
    :func:`~kiro_crew.platform_compat.is_link_or_junction`, the one the rest of
    this module uses for that distinction.
    """
    return path.is_dir() and not is_link_or_junction(path)


def _temp_data_name_obstruction(tmp_data: Path) -> str:
    """Why the shared ``.<name>-data-tmp`` name cannot hold the preserved ``data/``,
    or ``""`` when it can.

    :func:`install_app` and :func:`update_app` move the app's ``data/`` to that
    name beside the app directory while they replace the app files, and put it
    back afterwards; the name is shared with the uninstall so a copy a crashed
    sibling stranded there is reclaimable by whichever lifecycle runs next. It is
    reclaimable only when what stands there is a real directory the gateway made.
    Anything else at the name was planted by something else, and moving onto it
    is not a move aside: ``shutil.move`` onto a directory LINK deposits ``data/``
    inside the link's target, and the restore would then rename the link -- not
    the data -- back into the app directory, stranding or deleting what was
    preserved. So a link or a file at the name is refused before anything moves,
    and the sentence names it.
    """
    if not os.path.lexists(tmp_data) or _owned_data_dir(tmp_data):
        return ""
    shape = "link" if is_link_or_junction(tmp_data) else "file"
    return (
        f"{tmp_data} is a {shape}; Kiro Crew keeps the app's data directory at that "
        f"name while it replaces the app files and cannot use it -- remove it first"
    )


def _copy_app_tree(source: Path, dest: Path) -> None:
    """Copy an app source tree for install/update.

    - Symlinks are never followed. A symlink whose resolved target stays
      inside ``source`` is preserved as a symlink (e.g. an in-tree relative
      link); a symlink resolving OUTSIDE the source root is omitted
      entirely.  This makes the historic failure mode (a ``build`` symlink
      into a multi-GB build tree walked on copy) structurally impossible,
      and it prevents a link like ``ui -> ~/.docker`` from either copying
      or later serving sensitive files through the app UI route (same
      intent as ``snapshot._copytree_safe``).
    - ``ignore``: drop build-input/VCS dirs never needed at runtime.

    Callers on the asyncio event loop must run this off-loop (executor /
    ``asyncio.to_thread``) — a large copy is blocking filesystem I/O.
    """
    src_root = os.path.realpath(source)
    # os.path.isjunction: Python 3.12+ (always False off-Windows). Windows
    # directory junctions are reparse points NOT reported by islink(), and
    # copytree would descend into them despite symlinks=True — omit them.
    _isjunction = getattr(os.path, "isjunction", None)

    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        # Staging dirs carry unique per-transaction suffixes
        # (.kirocrew-deps-staging-<pid>-<nonce>), and an interrupted
        # install's leftover must neither be copied on update nor survive -
        # but the match is the STRICT generated pattern, never a bare
        # prefix: an app-owned name that merely shares the prefix (e.g.
        # ".kirocrew-deps-staging-assets") is the app's data and must copy.
        skip = {
            n for n in names if n in _COPY_IGNORE or _DEPS_STAGING_SWEEP_RE.fullmatch(n) is not None
        }
        for n in names:
            if n in skip:
                continue
            p = os.path.join(dir_path, n)
            if _isjunction is not None and _isjunction(p):
                # Junctions cannot be preserved as links by copytree; never
                # copy through one (it may point at a sensitive location).
                logger.warning("Omitting directory junction in app source: %s", p)
                skip.add(n)
                continue
            if os.path.islink(p):
                try:
                    target = os.path.realpath(p)
                    escapes = os.path.commonpath([src_root, target]) != src_root
                except ValueError:
                    # commonpath raises for paths on different drives
                    # (Windows) or mixed abs/rel — treat as escaping.
                    escapes = True
                if escapes:
                    logger.warning("Omitting symlink escaping app source root: %s", p)
                    skip.add(n)
        return skip

    shutil.copytree(
        source,
        dest,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=_ignore,
    )

    # Rewrite preserved ABSOLUTE in-tree symlinks to relative form: an
    # absolute link copied verbatim still points into the *source* tree, so
    # the installed copy would silently depend on (and break with) the local
    # source directory. Relative in-tree links are already correct as-is.
    for root, dirs, files in os.walk(dest):
        for n in dirs + files:
            p = os.path.join(root, n)
            if not os.path.islink(p):
                continue
            raw = os.readlink(p)
            if not os.path.isabs(raw):
                continue
            rel_to_src = os.path.relpath(os.path.realpath(p), src_root)
            os.remove(p)
            os.symlink(os.path.relpath(os.path.join(dest, rel_to_src), os.path.dirname(p)), p)


def preserved_data_awaits(name: str) -> bool:
    """Whether an install of *name* will restore a preserved ``data/`` over the copy.

    :func:`install_app` and :func:`update_app` move an existing ``data/`` aside
    before the copy and put it back afterwards, replacing whatever the source
    shipped under that name: the installed app's own directory on an update, one
    a default uninstall left behind, or the ``.{name}-data-tmp`` copy a crashed
    sibling operation stranded (restored the same way). With none of those on
    disk -- a first install -- the source's ``data/`` is what the runtime meets.
    A LINK at either name is not a preserved directory: the install refuses one
    before its record (:func:`gateway_data_dir_obstruction`) and the update
    refuses to move one (:func:`_owned_data_dir`), so nothing of the gateway's is
    put back over the copy for it.
    """
    dest = app_dir(name)
    return _owned_data_dir(dest / "data") or _owned_data_dir(dest.parent / f".{name}-data-tmp")


def copy_app_tree_as_installed(source: Path, dest: Path, *, data_preserved: bool) -> None:
    """Produce, at *dest*, the tree an install of *source* leaves for the runtime.

    The copy is :func:`_copy_app_tree` itself -- the same call :func:`install_app`
    and :func:`update_app` make, so whatever it drops, omits, preserves or rewrites
    (ignored names at any depth, escaping links, in-tree links kept as links,
    absolute in-tree links rewritten) is not predicted here but produced. Then the
    gateway's own part is applied the way the runtime will meet it: ``.app_secret``
    is removed on every install -- :func:`install_app` and :func:`update_app`
    remove the copied entry, unfollowed, before writing the gateway's own file or
    moving the preserved one back (:func:`_remove_any_shape`, the call made here
    too) -- and ``data`` is removed when *data_preserved* says the install will
    put a preserved directory back over the copied one
    (:func:`preserved_data_awaits`), and kept otherwise -- a first install carries
    the source's ``data/`` as itself, so an entry point under it is the source's
    there and only stops being so on the first update, which this same gate then
    refuses. Both removals are made by the direct path the install itself uses
    (``dest / ".app_secret"``, ``dest / "data"``), so the filesystem answers the one
    question the install's own removal and :func:`app_data_dir`'s ``mkdir`` put to
    it: where names fold case (APFS, NTFS) that path IS a shipped ``Data/``, which
    goes here as the install removes it there; where they do not (a Linux desktop)
    ``Data/`` is the app's own directory, carried as itself by the install and by
    this copy alike. Nothing is probed or listed -- a preview that read the tree by
    any rule other than the install's could only disagree with it. Then the one
    question the install asks of its own copy before writing the record is asked
    of this one: a root ``data`` entry left standing that is not a directory
    (:func:`gateway_data_dir_obstruction`) raises :class:`InstalledTreeRefused`
    with the install's own refusal, so the gate never admits a tree the install
    turns away -- and the refusal lands before any transaction touches the app
    directory, not after ``installed.json`` is written.

    The install-time desktop gate judges this tree instead of the checkout, so a
    layout the copy does not carry into the app directory is missing here exactly
    as it will be missing there. Blocking filesystem work: callers on the event
    loop run it off-loop, as they do the copy.
    """
    _copy_app_tree(source, dest)
    _remove_any_shape(dest / ".app_secret")
    if data_preserved:
        _remove_any_shape(dest / "data")
    refusal = gateway_data_dir_obstruction(dest)
    if refusal:
        raise InstalledTreeRefused(refusal)


def _remove_any_shape(path: Path) -> None:
    """Delete ``path`` whatever it is: tree, file, or dangling link.

    ``shutil.rmtree`` refuses non-directories, so a file-shaped dependency
    artifact (an app writing a FILE named like a deps tree) would survive
    every uninstall and poison the next quarantine rename. Links are
    unlinked, never traversed. Missing is fine.
    """
    if platform_compat.is_link_or_junction(path):
        platform_compat.unlink_link_or_junction(path)
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
