"""The copy of each app's UI scripts that the dashboard runs, kept where the app cannot write.

The dashboard imports an app's UI module (``/apps/<name>/ui/<entry>``) into its own
origin, so a script served from there runs with the owner's session. The installed
tree, ``apps/<name>/``, is writable from the sandbox, and the directory can be renamed
aside and recreated, so the bytes on disk there are not the bytes the gateway installed.

So the gateway keeps its own copy of every script (``.js`` and ``.mjs``) under
``<data home>/member-records/app-ui/<name>/``, which the OS sandbox masks in every mode
and the agent file tools refuse (:mod:`kiro_crew.member_records`). The gateway takes the
copy inside its own transactions: an install and an update (an adoption runs one) copy
the scripts from the source they install, and an owner's registration of a
self-managed app copies them from the app's folder. The UI route serves scripts from
that copy; every other UI file (styles, images, fonts, data) is still read from the
app's own tree, as before.

A gateway that ran an older build has no copies yet. Right after the gateway's
listener binds, every installed app gets its copy taken from its tree as it stands,
once (:func:`capture_existing`). After that an app the gateway never installed has no
copy, and its scripts are not served.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

from kiro_crew import member_records
from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.platform_lock_compat import acquire_lock, open_create_or_existing, release_lock

logger = logging.getLogger(__name__)

#: Record kind under ``member-records/``; it is in ``member_records.RECORD_KINDS``.
APP_UI_KIND = "app-ui"

#: UI files the dashboard runs as code. Only these are served from the copy.
SCRIPT_EXTENSIONS: frozenset[str] = frozenset({".js", ".mjs"})

#: Written once every app installed by an older build has its copy.
CAPTURED_MARKER = ".captured-from-app-trees"
#: Written once the pass that wrote :data:`CAPTURED_MARKER` has finished.
_CAPTURE_DONE = ".captured-done"
_CAPTURE_LOCK = ".capturing"
_TMP_PREFIX = ".tmp-"
_OLD_PREFIX = ".old-"
#: Written by an uninstall; cleared when a new copy is recorded.
_WITHDRAWN_PREFIX = ".withdrawn-"

#: The UI route refuses larger files (``routes_static._UI_MAX_BYTES``), so none is kept.
_MAX_BYTES = 8 * 1024 * 1024
_INSTALLED_META = "installed.json"


def _kind_dir() -> Path:
    return member_records.records_dir(APP_UI_KIND)


def copy_root(app_name: str) -> Path:
    """``member-records/app-ui/<app>``, containment-checked. Raises ``ValueError``."""
    if not isinstance(app_name, str) or not APP_NAME_RE.fullmatch(app_name):
        raise ValueError(f"unsafe app name: {app_name!r}")
    root = _kind_dir().resolve()
    target = (root / app_name).resolve()
    if target.parent != root:
        raise ValueError(f"app name {app_name!r} escapes {root}")
    return target


def _kernel_spelling(root: Path) -> Path:
    """*root* as the kernel spells it (case, on a case-insensitive disk), else as given."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(root, flags)
    except OSError:
        return root  # Windows opens no directory here; its check is case-folded below
    try:
        real = fd_real_path(fd)
    finally:
        os.close(fd)
    return Path(real) if real else root


def _inside(path: str, root: Path) -> bool:
    if os.name == "nt":
        return Path(os.path.normcase(path)).is_relative_to(os.path.normcase(str(root)))
    return Path(path).is_relative_to(root)


def _copy_scripts(src_root: Path, dest_root: Path, *, allow_hardlinks: bool = False) -> None:
    """Copy every regular script file under *src_root* into *dest_root*.

    Links are never followed, at a file or a directory, so nothing outside the tree
    is read. *allow_hardlinks* is for the source an owner installs from, which the
    install copies whatever its link count; an app's own folder never gets it, since
    a hard link there could stand for a file outside the tree. shortcut: a script
    reached only through an in-tree directory link is not copied, so it is not
    served; follow contained links if an app ever needs one.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    kernel_root = _kernel_spelling(src_root)

    def _raise(exc: OSError) -> None:
        raise exc

    # The facade, not the owner: only the manager family imports its owners.
    from kiro_crew.apps.manager import _COPY_IGNORE, _DEPS_STAGING_SWEEP_RE

    def _skipped_dir(dir_path: str, name: str) -> bool:
        # The same names an install leaves out of the app tree (build inputs,
        # dependency trees, VCS data), and every link or junction.
        return (
            name in _COPY_IGNORE
            or _DEPS_STAGING_SWEEP_RE.fullmatch(name) is not None
            or is_link_or_junction(Path(dir_path) / name)
        )

    for dir_path, dirs, files in os.walk(src_root, onerror=_raise, followlinks=False):
        # A Windows junction is not a symlink to os.walk, so it is pruned by name.
        dirs[:] = [d for d in dirs if not _skipped_dir(dir_path, d)]
        rel_dir = Path(dir_path).relative_to(src_root)
        for file_name in files:
            if Path(file_name).suffix.lower() not in SCRIPT_EXTENSIONS:
                continue
            if is_link_or_junction(Path(dir_path) / file_name):
                continue
            try:
                fd = os.open(os.path.join(dir_path, file_name), flags)
            except ValueError:
                # A name the OS cannot encode: the route could never open it either.
                continue
            # Any other open failure raises: a copy missing a readable script would
            # answer 404 for it for good, so the caller keeps its previous copy.
            # The descriptor's own path must still sit inside the tree: a parent
            # swapped for a link after the walk listed it is refused, not followed.
            real = fd_real_path(fd)
            if real is None or not _inside(real, kernel_root):
                os.close(fd)
                continue
            with os.fdopen(fd, "rb") as src:
                st = os.fstat(src.fileno())
                if (
                    not stat.S_ISREG(st.st_mode)
                    or (st.st_nlink != 1 and not allow_hardlinks)
                    or st.st_size > _MAX_BYTES
                ):
                    continue
                data = bounded_read(src, _MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                continue
            out_dir = dest_root / rel_dir
            mkdirs_owner_only(out_dir)
            (out_dir / file_name).write_bytes(data)


def record(
    app_name: str, ui_dir: Path, container: Path, *, allow_hardlinks: bool = False
) -> Path | None:
    """Replace *app_name*'s copy with the scripts in *ui_dir*; return the previous copy.

    *container* is the tree *ui_dir* belongs to: a *ui_dir* that resolves outside it
    (a dev-mode link, an escaping link) copies nothing. *allow_hardlinks* is set
    only for the source an install or update copies from (:func:`_copy_scripts`).
    The previous copy is kept
    aside and returned, so a caller inside a transaction can put it back
    (:func:`restore`) or drop it (:func:`discard`). Raises ``OSError`` or
    ``ValueError`` with nothing changed.
    """
    target = copy_root(app_name)
    ui_real = Path(os.path.realpath(ui_dir))
    kind = target.parent
    mkdirs_owner_only(kind)
    tmp = kind / f"{_TMP_PREFIX}{app_name}-{secrets.token_hex(4)}"
    mkdirs_owner_only(tmp)
    previous: Path | None = None
    try:
        if ui_real.is_relative_to(os.path.realpath(container)) and ui_real.is_dir():
            _copy_scripts(ui_real, tmp, allow_hardlinks=allow_hardlinks)
        if target.exists():
            previous = kind / f"{_OLD_PREFIX}{app_name}-{secrets.token_hex(4)}"
            os.replace(target, previous)
        try:
            os.replace(tmp, target)
        except OSError:
            if previous is not None:
                os.replace(previous, target)
            raise
        _withdrawn_mark(app_name).unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return previous


def restore(app_name: str, previous: Path | None) -> None:
    """Put back the copy :func:`record` set aside, for a rolled-back update."""
    target = copy_root(app_name)
    shutil.rmtree(target, ignore_errors=True)
    if previous is not None:
        os.replace(previous, target)


def discard(previous: Path | None) -> None:
    """Drop the copy :func:`record` set aside, once the change it replaced commits."""
    if previous is not None:
        shutil.rmtree(previous, ignore_errors=True)


def enabled() -> bool:
    """Whether ``agent.apps_ui_protected_copy`` is on. Off, the default, is main's
    behaviour: scripts are served from the app's folder. Blocking (a config read)."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return bool(getattr(KiroCrewConfig.load().agent, "apps_ui_protected_copy", False))
    except Exception:  # noqa: BLE001 - an unreadable config keeps the default
        logger.warning("agent.apps_ui_protected_copy: config load failed", exc_info=True)
        return False


def _set_aside_copies(app_name: str) -> list[Path]:
    """The copies :func:`record` set aside for *app_name* and never dropped."""
    kind = _kind_dir()
    prefix = f"{_OLD_PREFIX}{app_name}-"
    if not kind.is_dir():
        return []
    found = []
    for p in kind.iterdir():
        suffix = p.name[len(prefix) :]
        if (
            p.name.startswith(prefix)
            and len(suffix) == 8
            and all(c in "0123456789abcdef" for c in suffix)
            and p.is_dir()
        ):
            found.append(p)
    return found


def _withdrawn_mark(app_name: str) -> Path:
    """Present while *app_name*'s copy must not be served (an uninstall withdrew it)."""
    return _kind_dir() / f"{_WITHDRAWN_PREFIX}{app_name}"


def withdraw(app_name: str) -> None:
    """Stop serving *app_name*'s copy, durably, before an uninstall deletes anything.

    The mark stays until an install, update or registration records a new copy, so
    a copy the later removal cannot delete (a file held open on Windows) is never
    served again. Raises ``OSError`` or ``ValueError`` so the uninstall refuses with
    nothing deleted.
    """
    copy_root(app_name)  # the name check, before anything is written
    mkdirs_owner_only(_kind_dir())
    atomic_write(_withdrawn_mark(app_name), "1\n")


def unwithdraw(app_name: str) -> None:
    """Undo :func:`withdraw`, for an uninstall that rolled back."""
    _withdrawn_mark(app_name).unlink(missing_ok=True)


def forget(app_name: str) -> None:
    """Remove *app_name*'s copy and any copy set aside for it, once an uninstall commits.

    The set-aside ones go too, so nothing brings the scripts back. A removal that
    fails is logged; the mark :func:`withdraw` wrote still keeps the copy unserved.
    """
    try:
        for aside in _set_aside_copies(app_name):
            shutil.rmtree(aside)
        shutil.rmtree(copy_root(app_name))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        logger.warning(
            "could not remove app %s's withdrawn UI script copy; it is not served",
            app_name,
            exc_info=True,
        )


def record_folder(apps_root: Path, app_name: str) -> None:
    """Replace *app_name*'s copy with the scripts in its own folder under *apps_root*.

    For a folder the gateway did not fill itself: an owner's registration of a
    self-managed app, and the start-up capture below. Raises ``OSError`` or
    ``ValueError`` with nothing changed.
    """
    # Anchored on the apps root, never resolved through the app's own entry, so a
    # link planted at ``apps/<name>`` makes its ``ui`` land outside and copy nothing.
    from kiro_crew.apps.manager import app_record_lock

    folder = Path(os.path.realpath(apps_root)) / app_name
    with app_record_lock(app_name):
        discard(record(app_name, folder / "ui", folder))


def _recover_set_aside(app_name: str) -> None:
    """Put back the copy :func:`record` set aside when a hard stop came between its renames.

    Read only from the protected directory, never rebuilt from the app tree. Held
    under the app's record lock, the lock every :func:`record` caller holds.
    """
    from kiro_crew.apps.manager import app_record_lock

    # Looked for before any lock: a name with nothing set aside, such as one an
    # unauthenticated request made up, takes no lock and leaves no file behind.
    if not _set_aside_copies(app_name):
        return
    with app_record_lock(app_name):
        target = copy_root(app_name)
        aside = _set_aside_copies(app_name)
        if target.exists() or not aside:
            return
        newest = max(aside, key=lambda p: p.stat().st_mtime_ns)
        os.replace(newest, target)


@contextlib.contextmanager
def _capture_lock() -> Iterator[None]:
    path = _kind_dir() / _CAPTURE_LOCK
    mkdirs_owner_only(path.parent)
    fd = open_create_or_existing(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        acquire_lock(fd, exclusive=True)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _eligible_names(apps_root: Path) -> list[str]:
    """The app folders with a record, at the moment of the first pass."""
    names: list[str] = []
    entries = sorted(apps_root.iterdir()) if apps_root.is_dir() else []
    for entry in entries:
        if not APP_NAME_RE.fullmatch(entry.name) or is_link_or_junction(entry):
            continue
        try:
            if entry.is_dir() and (entry / _INSTALLED_META).is_file():
                names.append(entry.name)
        except OSError:
            # One unreadable folder must not cost every other app its copy.
            logger.warning("could not read app folder %s; skipping it", entry, exc_info=True)
    return names


def capture_existing(apps_root: Path) -> None:
    """Give every app an older build installed its copy, in one pass. Blocking.

    The gateway runs it once right after its listener binds, after the sweep of
    interrupted updates, and a script read that comes first runs it itself. Each app
    is copied under its record lock, the lock an install or update holds, and one
    that already has a copy is left as it is. The marker is written after the pass
    whatever happened, so nothing is ever taken from an app tree later: an app whose
    copy failed has its scripts not found until the owner updates, reinstalls or
    registers it. shortcut: an app tree changed before this pass is copied as it
    stands, since no earlier copy exists to compare it with.
    """
    # The facade, not the owner: only the manager family imports its owners.
    from kiro_crew.apps.manager import app_record_lock

    kind = _kind_dir()
    marker = kind / CAPTURED_MARKER
    done = kind / _CAPTURE_DONE
    # Only a FINISHED pass skips the lock. A pass that has started but not finished
    # holds the lock, so a script read waits for it (off the event loop) instead of
    # answering 404 for an app whose copy is still being made.
    if done.is_file():
        return
    with _capture_lock():
        if marker.is_file():
            # A pass that started and then stopped (a hard kill) never rereads a tree.
            atomic_write(done, "1\n")
            return
        # The boundary is written BEFORE any app tree is read: if it cannot be
        # written, nothing is copied, and once it exists no later call reads a tree.
        # An app this pass misses stays unserved until it is updated, reinstalled
        # or registered again.
        atomic_write(marker, "1\n")
        for name in _eligible_names(apps_root):
            try:
                with app_record_lock(name):
                    if _withdrawn_mark(name).exists():
                        continue
                    _recover_set_aside(name)
                    if not copy_root(name).exists():
                        record_folder(apps_root, name)
            except (OSError, ValueError):
                logger.warning(
                    "could not keep a copy of app %s's UI scripts; its scripts are "
                    "not served until it is updated, reinstalled or registered again",
                    name,
                    exc_info=True,
                )
        atomic_write(done, "1\n")


def served_root(apps_root: Path, app_name: str) -> Path | None:
    """The directory *app_name*'s scripts are served from, or ``None`` when it has none.

    Takes the one-time copy of older installs itself when the gateway's start-up
    pass has not finished yet (:func:`capture_existing`).
    """
    try:
        capture_existing(apps_root)
    except OSError:
        logger.warning("could not finish copying installed apps' UI scripts", exc_info=True)
    try:
        root = copy_root(app_name)
        if _withdrawn_mark(app_name).exists():
            return None
        if not root.is_dir():
            _recover_set_aside(app_name)
    except (OSError, ValueError):
        return None
    return root if root.is_dir() else None
