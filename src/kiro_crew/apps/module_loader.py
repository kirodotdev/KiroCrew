"""Module Loader — isolated module loading for app hooks and routes.

Loads app modules using importlib.util.spec_from_file_location to avoid
sys.path pollution. Modules are registered in sys.modules with unique
namespaced keys to prevent collisions between apps.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from kiro_crew.apps.execution import app_execution_denied, is_builtin_app
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Builtin apps ship inside the KiroCrew package and are trusted like core code.
# Anything loaded from outside this directory is a third-party / operator-installed
# app whose Python executes in-process with full gateway privileges (CSE SEC-012).
_BUILTINS_DIR = (Path(__file__).resolve().parent / "builtins")

# One-time-per-app guard so the SEC-012 trust warning is not logged on every hook load.
_warned_third_party_apps: set[str] = set()

#: Per-app cache of the resolved ``on_shutdown`` callable, populated at ENABLE
#: time (``cache_shutdown_callable``) while the app's files still exist. Teardown
#: after a CLI uninstall consults this FIRST: ``resolve_loaded_callable`` only
#: finds a module still in ``sys.modules``, which misses an ``on_shutdown`` living
#: in a module that startup never imported (separate startup/shutdown modules) --
#: the disk fallback then raises on the deleted files and the background task the
#: app spawned would survive trust removal. Caching the bound callable up front
#: closes that gap: the resident function object stops the running code without
#: touching disk. Cleared by ``clear_shutdown_callable`` on teardown/unload.
_shutdown_callables: dict[str, tuple[int, Callable[..., Any]]] = {}

#: Per-app load generation. Bumped every time an app's modules are unloaded (a
#: disable, or the gateway teardown sweep), so a shutdown callable captured under
#: one generation can be told apart from the code loaded by a LATER enable. A
#: cached callable is only honoured while its generation is still current: after
#: an unload+re-enable, the stale v1 callable is ignored rather than used to tear
#: down the freshly loaded v2 worker (it would leave v2 running).
_app_load_generation: dict[str, int] = {}


def _current_generation(app_name: str) -> int:
    return _app_load_generation.get(app_name, 0)


def cache_shutdown_callable(app_name: str, func: Callable[..., Any]) -> None:
    """Remember an app's resolved ``on_shutdown`` callable for post-uninstall teardown.

    Tagged with the app's CURRENT load generation so a later reload invalidates it
    (see ``_app_load_generation``); ``resolve_loaded_callable`` drops a stale one.
    """
    _shutdown_callables[app_name] = (_current_generation(app_name), func)


def clear_shutdown_callable(app_name: str) -> None:
    """Drop the cached ``on_shutdown`` callable (teardown complete / module unloaded)."""
    _shutdown_callables.pop(app_name, None)


def clear_all_shutdown_callables() -> None:
    """Drop every cached ``on_shutdown`` callable — for the gateway teardown sweep.

    The sweep tears hooks down without going through per-app ``unload_app_modules``,
    so without this a v1 callable would survive into an in-process restart and be
    selected to stop a v2 worker. Clearing wholesale on teardown closes that.
    """
    _shutdown_callables.clear()


def _is_builtin_app(app_name: str, app_resolved: Path) -> bool:
    """Return True when this app name owns the resolved shipped package path."""
    return is_builtin_app(app_name=app_name, app_root=app_resolved)


def _warn_third_party_execution(app_name: str) -> None:
    """Loudly surface (once per app) that untrusted third-party app code is being
    executed in-process. The app permission system only gates the SDK tool surface
    passed to the app context — it does NOT restrict ``import``, filesystem, network,
    or access to in-memory credentials. Installing an app is therefore equivalent to
    granting it full gateway-process privileges. Process-level isolation is tracked
    as separate future work; until then this boundary is made explicit + auditable.
    """
    if app_name in _warned_third_party_apps:
        return
    _warned_third_party_apps.add(app_name)
    logger.warning(
        "SECURITY: executing third-party app %r Python in-process — it runs with "
        "full gateway privileges (filesystem, network, in-memory credentials) and "
        "is NOT sandboxed. The app permission system gates only the SDK tool "
        "surface, not arbitrary code. Only enable apps you trust.",
        app_name,
    )


def _app_namespace_root(app_name: str) -> str:
    """Build the synthetic top-level package name that owns one app's modules.

    Per-app isolation rests on the app-name grammar: ``KEBAB_RE`` in
    ``apps/manifest.py`` admits only ``[a-z0-9]`` and ``-``, so a name can never
    contain a dot. That is what keeps one app's root from becoming a dotted
    prefix of another's -- ``_kirocrew_app_foo`` and ``_kirocrew_app_foo-bar``
    are separated by ``-``, so neither is under the other's ``root + "."``
    namespace. Relaxing that grammar to allow dots would let app ``foo`` own
    keys belonging to app ``foo.bar``.
    """
    return f"_kirocrew_app_{app_name}"


def _module_namespace(app_name: str, dotted_path: str) -> str:
    """Build a unique sys.modules key for an app module."""
    return f"{_app_namespace_root(app_name)}.{dotted_path}"


def _app_module_keys(app_name: str) -> list[str]:
    """Every sys.modules key owned by this app — the synthetic root included."""
    root = _app_namespace_root(app_name)
    prefix = f"{root}."
    return [k for k in sys.modules if k == root or k.startswith(prefix)]


def _ensure_namespace_packages(app_name: str, dotted_path: str, app_dir: Path) -> None:
    """Register the synthetic parent packages a dotted module key implies.

    ``_module_namespace`` deliberately builds a DOTTED sys.modules key so two
    apps shipping identically-named modules cannot collide. A dotted name makes
    the loaded module's ``__package__`` point at a parent package, and CPython
    then requires that parent — and every ancestor above it, up to the top-level
    name — to be present in ``sys.modules`` before a relative import inside the
    module body can resolve. Registering only the immediate parent is not enough:
    the import machinery walks to the root, so ``from . import config`` in a
    ``backend.routes`` hook entry file still raises ``ModuleNotFoundError`` for
    the top-level name.

    Each ancestor is registered as a namespace package whose ``__path__`` points
    at the matching directory inside the app, so sibling resolution stays scoped
    to the app's own tree and no ``sys.path`` mutation is needed — the property
    this module's docstring commits to.
    """
    segments = dotted_path.split(".")
    # (name, search directory) for the synthetic root plus one package per
    # intermediate segment. The final segment is the module itself, not a package.
    ancestors: list[tuple[str, Path]] = [(_app_namespace_root(app_name), app_dir)]
    for segment in segments[:-1]:
        parent_name, parent_dir = ancestors[-1]
        ancestors.append((f"{parent_name}.{segment}", parent_dir / segment))

    for name, search_dir in ancestors:
        if name in sys.modules:
            continue
        pkg = importlib.util.module_from_spec(
            importlib.machinery.ModuleSpec(name, None, is_package=True)
        )
        pkg.__path__ = [str(search_dir)]
        sys.modules[name] = pkg


def _app_module_snapshot(app_name: str) -> dict[str, Any]:
    """Snapshot this app's sys.modules entries as name -> module OBJECT.

    Names alone are not enough. Two hooks may name the same module file (the
    documented ``on_startup`` / ``on_shutdown`` pair does exactly that), so the
    second load overwrites the first's ``sys.modules`` entry under a key that is
    already present. A name-only snapshot would treat that key as pre-existing
    and leave the overwriting object in place on rollback, diverging from the
    module the first hook's registered handlers came from.
    """
    return {k: sys.modules[k] for k in _app_module_keys(app_name)}


def _rollback_app_modules(app_name: str, keep: dict[str, Any]) -> None:
    """Undo one load's sys.modules footprint, leaving earlier loads intact.

    A failed load can have touched three kinds of key: the leaf module, the
    synthetic ancestor packages, and any sibling the module body imported before
    it raised. Removing everything this app owns would evict a hook that loaded
    successfully earlier, so ``keep`` is the snapshot taken before this load:
    a key absent from it is removed, and a key whose OBJECT this load replaced is
    restored to the object the snapshot holds.

    Dropping the key is not enough either. CPython's import machinery binds a
    submodule as an ATTRIBUTE on its parent package, so a sibling imported by the
    failing body stays reachable as ``parent.sibling`` after its key is gone --
    invisible to ``unload_app_modules`` and ``is_app_module_loaded``, which reason
    over ``sys.modules`` alone, and worse: a later ``from . import sibling`` finds
    the attribute, skips the import entirely, and silently reuses the stale module
    instead of re-reading the file. So the parent attribute follows the same
    decision as the key, identity-checked so an unrelated attribute of the same
    name is never touched.
    """
    for key in _app_module_keys(app_name):
        current = sys.modules.get(key)
        original = keep.get(key)
        if original is current:
            continue
        if original is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original
        parent_name, _, leaf_name = key.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and getattr(parent, leaf_name, None) is current:
            if original is None:
                delattr(parent, leaf_name)
            else:
                setattr(parent, leaf_name, original)


def load_app_module(app_name: str, app_dir: Path, module_path: str) -> Callable[..., Any]:
    """Load an app module and return the specified callable.

    Uses importlib.util.spec_from_file_location to load directly from
    the file path, avoiding sys.path manipulation entirely.

    Module is registered in sys.modules as ``_kirocrew_app_{app_name}.{module_name}``
    to prevent collisions between apps that have identically-named modules.

    Args:
        app_name: The app's identifier.
        app_dir: The app's root directory.
        module_path: Hook path in format ``module.path:callable_name``.

    Returns:
        The callable (function/class) specified by the hook path.

    Raises:
        ImportError: If the module file is not found, escapes the app directory,
                     or the callable doesn't exist in the module.
        ValueError: If the module_path format is invalid.
    """
    # Parse "backend.routes:register_routes" → file path + callable name
    if ":" not in module_path:
        raise ValueError(
            f"Invalid hook path format (missing ':'): {module_path!r}. "
            f"Expected 'module.path:callable_name'"
        )

    dotted_path, callable_name = module_path.rsplit(":", 1)
    if not dotted_path or not callable_name:
        raise ValueError(f"Invalid hook path format: {module_path!r}")

    rel_path = dotted_path.replace(".", "/") + ".py"
    file_path = app_dir / rel_path

    if not file_path.is_file():
        raise ImportError(
            f"Module file not found: {file_path} "
            f"(app={app_name}, hook={module_path})"
        )

    # Path containment check — reject paths that escape the app root
    try:
        resolved = file_path.resolve()
        app_resolved = app_dir.resolve()
        if not resolved.is_relative_to(app_resolved):
            raise ImportError(
                f"Module path escapes app directory: {file_path} "
                f"(resolved to {resolved}, app root is {app_resolved})"
            )
    except (OSError, ValueError) as exc:
        raise ImportError(f"Path resolution failed: {exc}") from exc

    # Unique module name to avoid sys.modules collisions
    unique_name = _module_namespace(app_name, dotted_path)

    # CSE SEC-012: third-party Python runs in-process. Use the same central
    # decision as every other execution surface before emitting the trust warning.
    third_party = not _is_builtin_app(app_name, app_resolved)
    if third_party:
        denied = app_execution_denied(
            app_name,
            action="module_load",
            app_root=app_resolved,
            caller="gateway",
        )
        if denied:
            raise ImportError(
                f"Refusing to load third-party app {app_name!r} module "
                f"{module_path!r}: {denied}. App code would run in-process with "
                "full gateway privileges."
            )
        _warn_third_party_execution(app_name)

    spec = importlib.util.spec_from_file_location(unique_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to create module spec for {file_path}")

    # Snapshot before any registration so a failed load can be undone precisely,
    # without evicting a hook of the same app that already loaded successfully.
    # Objects, not just names: a second hook naming the same module file replaces
    # an entry that is already present.
    preexisting = _app_module_snapshot(app_name)

    # The dotted key makes __package__ point at a synthetic parent — register the
    # whole ancestor chain so relative imports in the module body can resolve.
    _ensure_namespace_packages(app_name, dotted_path, app_dir)

    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # Clean up on failure
        _rollback_app_modules(app_name, preexisting)
        raise ImportError(
            f"Failed to execute module {file_path}: {exc}"
        ) from exc

    # Mirror normal import semantics: bind the module on its parent package so a
    # sibling can reach it as an attribute (``from .routes import register``).
    parent_name, _, leaf_name = unique_name.rpartition(".")
    parent_module = sys.modules.get(parent_name)
    if parent_module is not None:
        setattr(parent_module, leaf_name, module)

    if not hasattr(module, callable_name):
        _rollback_app_modules(app_name, preexisting)
        raise ImportError(
            f"Module {file_path} has no attribute {callable_name!r}. "
            f"Available: {[a for a in dir(module) if not a.startswith('_')]}"
        )

    func = getattr(module, callable_name)
    if not callable(func):
        _rollback_app_modules(app_name, preexisting)
        raise ImportError(
            f"{callable_name!r} in {file_path} is not callable "
            f"(got {type(func).__name__})"
        )

    logger.debug(
        "Loaded app module: %s -> %s:%s", unique_name, file_path, callable_name
    )
    sel().log_api_access(
        caller="gateway",
        operation="app_module_load",
        outcome="ok",
        resources=f"{app_name}:{module_path} ({'third_party' if third_party else 'builtin'})",
    )
    return func


def resolve_loaded_callable(app_name: str, module_path: str) -> Callable[..., Any] | None:
    """Resolve ``module.path:callable`` from this app's ALREADY-LOADED module.

    For teardown of a gone (uninstalled) app: CLI uninstall deletes the app's
    files, so ``load_app_module`` -- which reads from disk -- would raise and the
    ``on_shutdown`` hook would never run, leaving a background task the app's
    ``on_startup`` spawned alive after trust was removed. But the module the
    gateway actually imported is still resident in ``sys.modules`` under the
    app's unique namespace key, so the callable can be resolved from THAT without
    touching disk. Returns the callable, or ``None`` when no such module/attr is
    cached (nothing was ever loaded) so the caller can fall back to the disk
    loader. Deliberately does NOT import anything: it only reads what is already
    loaded, which is exactly the code whose ``on_shutdown`` must stop it.

    Checks the enable-time ``_shutdown_callables`` cache FIRST: ``sys.modules``
    only holds modules startup actually imported, so an ``on_shutdown`` in a
    module startup never touched (separate startup/shutdown modules) is absent
    there and the disk fallback would raise on the deleted files. The cache holds
    the bound callable captured at enable while the files existed, closing that
    gap without touching disk.
    """
    cached = _shutdown_callables.get(app_name)
    if cached is not None:
        gen, func = cached
        if gen == _current_generation(app_name):
            return func
        # Stale generation: this callable belongs to code that has since been
        # unloaded/reloaded. Drop it and fall through to what is loaded NOW, so a
        # v1 shutdown is never used against a v2 worker.
        _shutdown_callables.pop(app_name, None)
    if ":" not in module_path:
        return None
    dotted_path, callable_name = module_path.rsplit(":", 1)
    if not dotted_path or not callable_name:
        return None
    module = sys.modules.get(_module_namespace(app_name, dotted_path))
    if module is None:
        return None
    resolved = getattr(module, callable_name, None)
    return resolved if callable(resolved) else None


def unload_app_modules(app_name: str) -> int:
    """Remove all cached modules for an app from sys.modules.

    Called on app disable to ensure re-enable loads fresh code. This covers the
    synthetic ancestor packages too — leaving one behind would pin a stale
    ``__path__`` at the app's old location across an uninstall/reinstall.

    Returns the number of sys.modules entries removed.
    """
    to_remove = _app_module_keys(app_name)
    for k in to_remove:
        del sys.modules[k]
    clear_shutdown_callable(app_name)
    # Bump the load generation so a NEXT enable's cache entry is distinguishable
    # from this one -- any callable still cached under the old generation (e.g.
    # captured by a concurrent path) is treated as stale by resolve_loaded_callable.
    _app_load_generation[app_name] = _current_generation(app_name) + 1
    if to_remove:
        logger.debug("Unloaded %d module(s) for app %s", len(to_remove), app_name)
    return len(to_remove)


def is_app_module_loaded(app_name: str) -> bool:
    """Check if any modules are loaded for an app."""
    return bool(_app_module_keys(app_name))
