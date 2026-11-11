"""Python import checks and cache handling for a signed app's tree.

Under a policy that requires signatures (``apps.admission``), the signature covers
an app's source files (``fileDigests``), and CPython would otherwise run a cached
``__pycache__/*.pyc`` instead of a verified source file whenever the cache looks
current, or always for an unchecked-hash ``.pyc``. Two places close that:

- In the gateway process, hook entries and normal imports from registered app
  roots compile source bytes whose SHA-256 matches the signed digest
  (:class:`VerifiedSourceLoader`, :class:`SignedAppFinder`). This includes top-level
  imports through ``sys.path``. Bytecode-only and unlisted source modules refuse
  normal imports. Direct file loaders bypass the finder; in-tree ``__pycache__``
  directories are removed before each signed hook loads (:func:`clear_app_bytecode`).
  When the gateway uses an external bytecode prefix, its app-specific mirrored
  subtree is removed too. Cleanup errors refuse the hook and unrelated cache
  subtrees are left alone.
- A Python child of the app (backend, lifecycle script, stdio MCP server) runs with
  ``PYTHONPYCACHEPREFIX`` set to a gateway-owned directory outside the app tree,
  emptied when it is handed out (:func:`pycache_prefix_env`), so the child never
  consults ``__pycache__`` in the app directory.

With no signature policy none of this applies and imports behave as before.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import importlib.abc
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import threading
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from types import CodeType, ModuleType
from typing import Optional

from kiro_crew.platform_compat import PinnedDirectory, pinned_directory

_lock = threading.Lock()
#: app name -> (resolved app root, signed ``fileDigests``)
_signed_roots: dict[str, tuple[Path, Mapping[str, str]]] = {}


class VerifiedSourceLoader(importlib.machinery.SourceFileLoader):
    """A source loader that compiles only bytes matching their signed digest.

    The bytes are read once, compared with the digest, then compiled and run, so
    what runs is what was compared. No cached bytecode is read or written.
    """

    def __init__(self, fullname: str, path: str, digest: str) -> None:
        super().__init__(fullname, path)
        self._digest = digest

    def get_code(self, fullname: str) -> CodeType:
        source = self.get_data(self.path)
        if not hmac.compare_digest(hashlib.sha256(source).hexdigest(), self._digest):
            raise ImportError(f"{self.path} does not match its signed digest", name=fullname)
        return compile(source, self.path, "exec", dont_inherit=True)


def _signed_lookup(origin: str) -> Optional[tuple[str, Mapping[str, str]]]:
    """``(relative path, digests)`` when *origin* lies in a registered signed tree."""
    try:
        resolved = Path(origin).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    with _lock:
        roots = list(_signed_roots.values())
    for root, digests in roots:
        if resolved.is_relative_to(root):
            rel = unicodedata.normalize("NFC", resolved.relative_to(root).as_posix())
            return rel, digests
    return None


class SignedAppFinder(importlib.abc.MetaPathFinder):
    """Gives every module found inside a signed app tree a verified source loader."""

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[str]],
        target: Optional[ModuleType] = None,
    ) -> Optional[importlib.machinery.ModuleSpec]:
        with _lock:
            if not _signed_roots:
                return None
        if path is not None and not any(
            _signed_lookup(entry or os.curdir) is not None for entry in path
        ):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not spec.origin or not spec.has_location:
            return spec
        found = _signed_lookup(spec.origin)
        if found is None:
            return spec
        rel, digests = found
        if isinstance(spec.loader, importlib.machinery.SourcelessFileLoader):
            raise ImportError(f"{rel} is bytecode in a signed app", name=fullname)
        if isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            digest = digests.get(rel)
            if not isinstance(digest, str):
                raise ImportError(f"{rel} is not in the signed file list", name=fullname)
            spec.loader = VerifiedSourceLoader(fullname, spec.origin, digest)
            spec.cached = None
        return spec


_finder = SignedAppFinder()


def trust_app_sources(
    app_name: str, app_root: Path, digests: Mapping[str, str], fullname: str, entry: Path
) -> VerifiedSourceLoader:
    """Register *app_root* as signed and return the loader for the hook *entry*.

    Imports the hook makes from the app's tree go through :class:`SignedAppFinder`
    from then on; a later call for the same app replaces its digests.
    """
    root = app_root.resolve()
    with _lock:
        _signed_roots[app_name] = (root, dict(digests))
        if _finder not in sys.meta_path:
            sys.meta_path.insert(0, _finder)
    found = _signed_lookup(str(entry))
    rel = found[0] if found else ""
    digest = digests.get(rel)
    if not isinstance(digest, str):
        raise ImportError(f"{rel or entry} is not in the signed file list", name=fullname)
    return VerifiedSourceLoader(fullname, str(entry), digest)


def forget_app_sources(app_name: str) -> None:
    """Stop treating *app_name*'s tree as signed (its hooks were unloaded)."""
    with _lock:
        _signed_roots.pop(app_name, None)
        if not _signed_roots and _finder in sys.meta_path:
            sys.meta_path.remove(_finder)


def _pin_cache_path(path: Path, stack: ExitStack) -> PinnedDirectory:
    """Pin the already-resolved directory itself.

    POSIX descendants use ``dir_fd``, so ancestors need traversal, not read
    permission. A Windows pin blocks renaming the directory and every ancestor
    while it is held, and a drive root cannot be opened as a pin at all.
    """
    return stack.enter_context(pinned_directory(path))


def _clear_bytecode_tree(directory: PinnedDirectory, *, remove_all: bool = False) -> None:
    """Descend through held parents; only cache subtrees lose their entries."""
    for name in directory.names():
        remove = remove_all or name == "__pycache__"
        try:
            child = directory.child(name)
        except OSError as exc:
            # The open itself refuses files and links. Unlinking such an entry
            # through its held parent cannot traverse a linked target.
            if isinstance(exc, NotADirectoryError) or exc.errno in (errno.ENOTDIR, errno.ELOOP):
                if remove:
                    directory.unlink(name)
                continue
            raise
        with child:
            _clear_bytecode_tree(child, remove_all=remove)
        if remove:
            directory.rmdir(name)


def clear_app_bytecode(app_root: Path) -> None:
    """Remove this app's caches through pinned directories, never screened names."""
    root = app_root.absolute()
    # Resolve the trusted root's parent once; the app itself and its children
    # are opened without following links.
    root = root.parent.resolve() / root.name
    with ExitStack() as stack:
        _clear_bytecode_tree(_pin_cache_path(root, stack))

    if sys.pycache_prefix is not None:
        prefix = Path(sys.pycache_prefix).absolute()
        mirrors = {
            Path(importlib.util.cache_from_source(str(path / "_cache_location.py")))
            .absolute()
            .parent
            for path in {app_root.absolute(), root}
        }
        for mirror in mirrors:
            if mirror == prefix or not mirror.is_relative_to(prefix):
                raise OSError("app bytecode mirror is outside the active cache prefix")
            parts = mirror.relative_to(prefix).parts
            with ExitStack() as stack:
                try:
                    parent = _pin_cache_path(prefix.resolve(), stack)
                    for name in parts[:-1]:
                        parent = stack.enter_context(parent.child(name))
                    child = parent.child(parts[-1])
                except FileNotFoundError:
                    continue
                except NotADirectoryError as exc:
                    raise OSError("app bytecode mirror contains a linked directory") from exc
                with child:
                    _clear_bytecode_tree(child, remove_all=True)
                parent.rmdir(parts[-1])


def remove_app_pycache(app_name: str) -> None:
    """Remove the gateway's child-cache directories for an uninstalled app."""
    from kiro_crew.config.loader import config_dir

    shutil.rmtree(config_dir() / "run" / "app-pycache" / app_name, ignore_errors=True)


def pycache_prefix_env(app_name: str, executed: Path) -> dict[str, str]:
    """``{"PYTHONPYCACHEPREFIX": <fresh empty dir>}`` for a signed app's child, else ``{}``.

    Applies when the admission policy requires signatures and *executed* lies in
    the installed tree (``admission.signed_tree_applies``). The directory is under
    the data home's ``run`` directory, outside every app tree, and the app's
    previous prefixes are removed first. Blocking.
    """
    from kiro_crew.apps.admission import _activation_root, signed_tree_applies
    from kiro_crew.config.loader import config_dir

    if not signed_tree_applies(app_name, executed):
        return {}
    clear_app_bytecode(_activation_root(app_name))
    base = config_dir() / "run" / "app-pycache" / app_name
    shutil.rmtree(base, ignore_errors=True)
    return _new_pycache_prefix_env(app_name)


def _new_pycache_prefix_env(app_name: str) -> dict[str, str]:
    """Allocate an empty cache without disturbing another running child."""
    from kiro_crew.config.loader import config_dir

    prefix = config_dir() / "run" / "app-pycache" / app_name / uuid.uuid4().hex
    os.makedirs(prefix, mode=0o700, exist_ok=True)
    return {"PYTHONPYCACHEPREFIX": str(prefix)}


def registry_pycache_prefix_env(app_name: str, app_root: Path | None = None) -> dict[str, str]:
    """Clear publisher bytecode and allocate a fresh cache for a signed checkout."""
    from kiro_crew.apps.admission import signature_required

    if not signature_required():
        return {}
    if app_root is not None:
        clear_app_bytecode(app_root)
    return _new_pycache_prefix_env(app_name)
