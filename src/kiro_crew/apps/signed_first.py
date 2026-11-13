"""Signed-first imports for a signed app's Python, in the gateway and in its children.

Under the fleet signature policy a module name that the verified ``signedFiles``
inventory lists resolves to that listed file, so an unlisted ``helpers/__init__.py``,
``helpers.so`` or ``__pycache__`` entry cannot stand in for a signed ``helpers.py``.
The gateway installs the finder in-process (``module_loader``). A Python child the
gateway builds the argv for runs this file by absolute path ahead of its target::

    python [options] /abs/kiro_crew/apps/signed_first.py <app> \
        (<script.py> | -m <module> | -c <code>) [args...]

The checked list (the app, its resolved root and the verified ``signedFiles``) is
never in a file the app can write. The gateway hands it over at launch, and the shim reads it once,
before any app code runs, then forgets the channel (:func:`read_checked`): a pipe
whose read end the child inherits (:data:`FD_ENV` names the fd on POSIX, the
handle on Windows) when the gateway spawns the child itself, or for a stdio server
kiro-cli spawns a file the gateway writes in the read-only kiro agents tree, whose
path alone the launch config's environment carries (:data:`LIST_ENV`).
What the app writes afterwards, to a file, the environment or a new fd, cannot
change a list already read. Stdlib-only, so any interpreter an app launches can
run it, and it runs the target through ``deps_boot``'s dispatch, loaded from this
file's own directory.
"""

from __future__ import annotations

import sys

#: What was imported before this file's own imports: Python's startup modules, and
#: for an in-process caller everything already loaded. Only modules cached after
#: this point can be dropped for a signed app module (:func:`_yield_names`).
_PRELOADED = frozenset(sys.modules)

# isort: split

import base64  # noqa: E402 - after the snapshot above, on purpose
import hashlib  # noqa: E402
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
from pathlib import Path, PurePosixPath  # noqa: E402
from typing import Any  # noqa: E402

#: Modules never dropped for an app module of the same name: the interpreter and the
#: import system the shim and the target both run on.
_NEVER_DROPPED = frozenset({"sys", "os", "builtins", "importlib", "posix", "nt", "_imp"})

#: The inherited pipe the gateway wrote the checked list into: an fd on POSIX, a
#: handle on Windows.
FD_ENV = "KIROCREW_SIGNED_FIRST_FD"
#: The path of the checked-list file, for a launch the gateway does not spawn
#: (kiro-cli's stdio servers): the gateway writes it in the kiro agents tree, which
#: the sandbox mounts read-only; the environment carries only the path.
LIST_ENV = "KIROCREW_SIGNED_FIRST_LIST"

#: Where the shim leaves a cron script body the gateway verified, for the cron
#: launcher to run in place of reading the file again; popped by that launcher.
VERIFIED_SCRIPT_ATTR = "_kirocrew_verified_script"


class _SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """A source loader that never reads bytecode.

    Under the fleet signature policy the ``.py`` files an app ships are verified,
    but a ``__pycache__`` entry beside one is not, and the stock loader runs that
    cached bytecode in place of the source whenever its header matches. This one
    compiles the source bytes every time.
    """

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


#: Resolved app roots whose modules load signed-first, mapped to the app's signed
#: ``signedFiles`` paths (the ``/``-separated keys of the verified inventory).
_source_only_roots: dict[str, frozenset[str]] = {}

_LOADERS: tuple[tuple[type, list[str]], ...] = (
    (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
    (_SourceOnlyLoader, importlib.machinery.SOURCE_SUFFIXES),
    (importlib.machinery.SourcelessFileLoader, importlib.machinery.BYTECODE_SUFFIXES),
)


def _owning_root(path: str) -> str | None:
    resolved = os.path.realpath(path)
    for root in _source_only_roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return root
    return None


class _SignedFirstFinder(importlib.machinery.FileFinder):
    """A finder for a directory inside a signed app: a signed file wins over an unlisted one.

    For each module name the candidates are the stock ones in the stock order (a
    package directory before a module; extension, source, then bytecode). When a
    candidate is a file the verified ``signedFiles`` lists, the first such one is
    loaded, so an unlisted ``helpers.so`` or ``helpers/__init__.py`` cannot stand in
    for a signed ``helpers.py``. With no listed candidate the stock search runs, so
    an unlisted module still imports when nothing signed shares its name. Source
    loads through :class:`_SourceOnlyLoader`; a bare listed ``.pyc`` still loads.
    """

    def __init__(self, path: str, root: str) -> None:
        super().__init__(path, *_LOADERS)
        self._root = root

    def find_spec(self, fullname: str, target: Any = None) -> Any:
        listed = _source_only_roots.get(self._root, frozenset())
        directory = os.path.realpath(self.path or os.getcwd())
        tail = fullname.rpartition(".")[2]
        package_dir = os.path.join(directory, tail)
        candidates: list[tuple[str, type, bool]] = []
        for loader, suffixes in _LOADERS:
            candidates.extend(
                (os.path.join(package_dir, "__init__" + s), loader, True) for s in suffixes
            )
        for loader, suffixes in _LOADERS:
            candidates.extend((os.path.join(directory, tail + s), loader, False) for s in suffixes)
        for full, loader, is_package in candidates:
            try:
                rel = Path(full).relative_to(self._root).as_posix()
            except ValueError:
                continue
            if rel in listed and os.path.isfile(full):
                return importlib.util.spec_from_file_location(
                    fullname,
                    full,
                    loader=loader(fullname, full),
                    submodule_search_locations=[package_dir] if is_package else None,
                )
        return super().find_spec(fullname, target)


def _source_only_path_hook(path: str) -> importlib.machinery.FileFinder:
    """``sys.path_hooks`` entry: :class:`_SignedFirstFinder` for directories inside a signed app.

    A sibling module an app hook imports is found through the namespace packages
    ``module_loader`` registers, whose ``__path__`` points inside the app. Every
    other directory falls through to the stock hooks.
    """
    # A directory only, as ``FileFinder.path_hook``: ``runpy.run_path(<script>)`` asks
    # for an importer for the script file itself and must get none.
    if not os.path.isdir(path or os.getcwd()):
        raise ImportError("only directories are searched")
    root = _owning_root(path)
    if root is None:
        raise ImportError("not inside a signed app")
    return _SignedFirstFinder(path, root)


def drop_cached_finders(root: str) -> None:
    """Forget the finders cached for *root*'s directories; the other hooks may have made them."""
    for cached in [
        key
        for key in list(sys.path_importer_cache)
        if isinstance(key, str)
        and (os.path.realpath(key) == root or os.path.realpath(key).startswith(root + os.sep))
    ]:
        sys.path_importer_cache.pop(cached, None)


def install(root: str, listed: frozenset[str]) -> None:
    """Load the modules under *root* (a resolved path) signed-first against *listed*."""
    _source_only_roots[root] = listed
    if _source_only_path_hook not in sys.path_hooks:
        sys.path_hooks.insert(0, _source_only_path_hook)
    drop_cached_finders(root)


def _read_inherited(value: str) -> bytes:
    """Every byte of the inherited pipe *value* names, then close it."""
    if sys.platform == "win32":
        import msvcrt

        fd = msvcrt.open_osfhandle(int(value), os.O_RDONLY)
    else:
        fd = int(value)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _read_list_file(path: str) -> bytes:
    """The bytes of the regular checked-list file at *path*, no link followed at it."""
    # O_NONBLOCK (POSIX): a FIFO here reaches the refusal below instead of hanging the open.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("the checked list is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


#: ``FILE_ATTRIBUTE_REPARSE_POINT``: a Windows link or junction at a name.
_REPARSE_POINT = 0x400


def _no_link_on_the_way(root: str, parts: tuple[str, ...]) -> None:
    """Raise ValueError when a directory or the file under *root* is a link or reparse point."""
    for depth in range(1, len(parts) + 1):
        named = os.lstat(os.path.join(root, *parts[:depth]))
        if stat.S_ISLNK(named.st_mode) or getattr(named, "st_file_attributes", 0) & _REPARSE_POINT:
            raise ValueError(f"{PurePosixPath(*parts[:depth]).as_posix()!r} is a link")


def _file_sha256(root: str, rel: str) -> str:
    """SHA-256 of the listed regular file *rel* under *root*, read through one descriptor.

    No link or junction is followed anywhere below *root*, so a listed file reached
    through a linked directory is refused, not only a link at its own name. POSIX
    walks the directories with ``O_DIRECTORY | O_NOFOLLOW`` descriptors and opens the
    file relative to the last with ``O_NOFOLLOW``. Windows has neither, so every step
    is checked for a reparse point before and after the open, and the name must then
    be the very file the descriptor holds (``samestat``). Stdlib only: this runs in
    the child before anything else is importable, so ``platform_compat`` is not.
    """
    parts = Path(rel).parts
    path = os.path.join(root, *parts)
    if sys.platform != "win32":
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                inner = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = inner
            # O_NONBLOCK: a FIFO in a listed file's place must reach the regular-file
            # refusal below, not hang the open waiting for a writer.
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
    else:
        _no_link_on_the_way(root, parts)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode):
            raise ValueError(f"{rel!r} is not a regular file")
        if sys.platform == "win32":
            _no_link_on_the_way(root, parts)
            if not os.path.samestat(held, os.lstat(path)):
                raise ValueError(f"{rel!r} changed while it was opened")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1 << 20):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def unchanged_refusal(
    root: str, files: dict[str, str], unchecked: frozenset[str] = frozenset()
) -> str | None:
    """Why a listed file under *root* does not have its signed digest, or None.

    The launch-time recheck for a child the gateway cannot check at its own spawn
    (a stdio server kiro-cli starts): every listed file is hashed once, except the
    *unchecked* ones the gateway named (files this spawn hides from the child, and
    the ``ui/`` files an operator's dev-mode grant leaves out at admission).
    """
    for rel, expected in files.items():
        if rel in unchecked:
            continue
        try:
            actual = _file_sha256(root, rel)
        except (OSError, ValueError):
            return f"signed file {rel!r} is missing, a link, or not a regular file"
        if actual != expected:
            return f"signed file {rel!r} does not match the signed inventory"
    return None


#: Suffixes under which Python loads a module (source, bytecode, extension).
_MODULE_SUFFIXES = tuple(importlib.machinery.all_suffixes())


def _is_link(path: str) -> bool:
    """Whether *path* is a symlink or a Windows junction (``islink`` misses the latter).

    ``os.path.isjunction`` is 3.12+, and an app may name an older interpreter, so
    without it a Windows reparse point is read from ``lstat`` instead.
    """
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", _REPARSE_POINT))


def _is_package_dir(path: str) -> bool:
    """Whether *path* is a directory Python imports as a regular package."""
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(name == "__init__" + suffix for name in names for suffix in _MODULE_SUFFIXES)


def unlisted_package_files(root: str, listed: frozenset[str]) -> list[str]:
    """Module files under a package that shadows a listed module, which are not listed.

    Python prefers a package directory ``worker/`` over a module ``worker.py`` beside
    it, so a signed ``worker.py`` protects nothing while that package is unsigned.
    For each listed ``.py`` whose stem is also a package directory under *root*,
    every module file in that package's tree (``__pycache__`` aside) must be listed.
    Links are not followed. Sorted ``/``-separated paths.
    """
    found: list[str] = []
    for rel in sorted(listed):
        if not rel.endswith(".py"):
            continue
        package = os.path.join(root, *Path(rel[: -len(".py")]).parts)
        if _is_link(package) or not _is_package_dir(package):
            continue
        for current, dirs, names in os.walk(package):
            dirs[:] = [name for name in dirs if name != "__pycache__"]
            for name in names:
                if not name.endswith(_MODULE_SUFFIXES):
                    continue
                child = Path(os.path.relpath(os.path.join(current, name), root)).as_posix()
                if child not in listed:
                    found.append(child)
    return sorted(set(found))


def read_checked(app: str) -> tuple[str, dict[str, str], frozenset[str], bytes | None]:
    """The checked list the launch handed over for *app*.

    ``(root, file digests, files left unchecked, verified cron script body)``.

    Read once and the channel dropped, so neither a descendant nor later app code
    sees it. Raises ``SystemExit`` (the target never runs) when the launch carried
    no list, or one for another app.
    """
    fd_value = os.environ.pop(FD_ENV, None)
    inline = os.environ.pop(LIST_ENV, None)
    try:
        if fd_value is not None:
            checked = json.loads(_read_inherited(fd_value))
        elif inline is not None:
            checked = json.loads(_read_list_file(inline))
        else:
            checked = None
        if not isinstance(checked, dict) or checked.get("app") != app:
            raise ValueError("no checked list for this app")
        files = checked["files"]
        if not isinstance(files, dict):
            raise ValueError("the checked list carries no digests")
        script = checked.get("script")
        body = base64.b64decode(script, validate=True) if isinstance(script, str) else None
        unchecked = frozenset(str(rel) for rel in checked.get("unchecked") or ())
        digests = {str(rel): str(sha) for rel, sha in files.items()}
        return str(checked["root"]), digests, unchecked, body
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"signed_first: refusing to run {app!r}: {exc}") from None


def _yield_names(root: str, files: frozenset[str], target: list[str]) -> None:
    """Drop modules cached since the shim started that a signed app module shadows.

    Every module imported after :data:`_PRELOADED` (by the shim, ``deps_boot`` or
    ``runpy``) whose top-level name a listed file provides at the top of the app's
    import path (``<name>.py`` or ``<name>/__init__.py``; the top is the target
    script's directory, or the app root for ``-m`` and ``-c``) leaves
    ``sys.modules`` with its submodules, so the app's own module wins, as in a plain
    launch. The shim keeps working through the module objects its globals hold;
    :data:`_NEVER_DROPPED` and anything imported before the shim stay.
    """
    if target[0] in ("-m", "-c"):
        top = os.path.realpath(root)
    else:
        top = os.path.dirname(os.path.realpath(os.path.abspath(target[0])))
    try:
        prefix = Path(top).relative_to(os.path.realpath(root)).as_posix()
    except ValueError:
        return
    prefix = "" if prefix == "." else prefix + "/"
    cached = {key.partition(".")[0] for key in list(sys.modules) if key not in _PRELOADED}
    for name in sorted(cached - _NEVER_DROPPED):
        if name in _PRELOADED:
            continue
        if f"{prefix}{name}.py" in files or f"{prefix}{name}/__init__.py" in files:
            for key in [k for k in list(sys.modules) if k == name or k.startswith(name + ".")]:
                del sys.modules[key]


def main(argv: list[str]) -> None:
    # Run by path, CPython put this file's directory (kiro_crew/apps/) first on
    # sys.path; drop it so the target cannot import gateway modules by bare name.
    own = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.abspath(sys.path[0] or os.curdir) == own:
        del sys.path[0]
    if len(argv) < 2 or (argv[1] in ("-m", "-c") and len(argv) < 3):
        sys.stderr.write(
            "usage: python signed_first.py <app> "
            "(<script.py> | -m <module> | -c <code>) [args...]\n"
        )
        raise SystemExit(2)
    root, files, unchecked, script = read_checked(argv[0])
    changed = unchanged_refusal(root, files, unchecked)
    shadowing = unlisted_package_files(root, frozenset(files))
    if changed is None and shadowing:
        changed = f"the unsigned package file {shadowing[0]!r} shadows a signed module"
    if changed is not None:
        raise SystemExit(f"signed_first: refusing to run {argv[0]!r}: {changed}")
    install(root, frozenset(files))
    if script is not None:
        setattr(sys, VERIFIED_SCRIPT_ATTR, script)
    # deps_boot's dispatch, by path: under -I this directory is not on sys.path.
    spec = importlib.util.spec_from_file_location(
        "_kiro_crew_signed_first_dispatch", os.path.join(own, "deps_boot.py")
    )
    if spec is None or spec.loader is None:
        raise SystemExit("signed_first: the launch dispatch is missing beside this file")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    target = argv[1:]
    _yield_names(root, frozenset(files), target)
    sys.path[:0] = dispatch.launch_entries(target)
    dispatch.run_target(target)


if __name__ == "__main__":
    main(sys.argv[1:])
