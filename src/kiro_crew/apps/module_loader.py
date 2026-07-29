"""Module Loader — isolated module loading for app hooks and routes.

Loads app modules using importlib.util.spec_from_file_location to avoid
sys.path pollution. Modules are registered in sys.modules with unique
namespaced keys to prevent collisions between apps.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Builtin apps ship inside the KiroCrew package and are trusted like core code.
# Anything loaded from outside this directory is a third-party / operator-installed
# app whose Python executes in-process with full gateway privileges (CSE SEC-012).
_BUILTINS_DIR = (Path(__file__).resolve().parent / "builtins")

# One-time-per-app guard so the SEC-012 trust warning is not logged on every hook load.
_warned_third_party_apps: set[str] = set()


def _is_builtin_app(app_resolved: Path) -> bool:
    """Return True if the resolved app dir lives under the shipped builtins dir."""
    try:
        return app_resolved.is_relative_to(_BUILTINS_DIR)
    except ValueError:
        return False


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


def _third_party_apps_allowed() -> bool:
    """Whether the operator permits running third-party (non-builtin) app Python.
    Defaults to True (apps are operator-installed and consented); set
    ``agent.apps_allow_third_party=false`` to refuse third-party app code
    entirely — both in-process module loads (here) and out-of-process backend
    spawns (see backend._start_app_backend_body) consult this switch (CSE
    SEC-012 interim hardening — a hard off switch until out-of-process isolation
    lands).

    Read lazily from config to avoid an import cycle with the config loader.
    On a config-load failure this fails CLOSED (returns False, deny): this is a
    security off-switch, so an unreadable policy must not silently re-enable
    third-party code the operator disabled. Failing closed is safe because only
    third-party apps consult this gate — builtins skip it entirely (see
    load_app_module and backend._start_app_backend_body), so a config error
    declines untrusted app code without bricking first-party apps.
    """
    try:
        # circular import: config.loader imports module_loader indirectly, so the
        # import is deferred to call time rather than module top.
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(getattr(KiroCrewConfig.load().agent, "apps_allow_third_party", True))
    except Exception as exc:  # noqa: BLE001 — config load must never hard-fail app loading
        logger.error(
            "apps_allow_third_party: config load failed (%s); failing closed "
            "(refusing third-party app code)",
            exc,
        )
        return False


def _module_namespace(app_name: str, dotted_path: str) -> str:
    """Build a unique sys.modules key for an app module."""
    return f"_kirocrew_app_{app_name}.{dotted_path}"


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

    # CSE SEC-012: third-party (operator-installed) app code executes unsandboxed
    # in the gateway process. Surface that trust boundary explicitly + auditably,
    # and let the operator refuse it entirely via config.
    third_party = not _is_builtin_app(app_resolved)
    if third_party:
        # Check the off-switch BEFORE warning, so a denied load does not emit a
        # log line asserting execution is happening right before the denial.
        if not _third_party_apps_allowed():
            sel().log_api_access(
                caller="gateway",
                operation="app_module_load",
                outcome="denied",
                resources=f"{app_name}:{module_path} (third_party)",
            )
            raise ImportError(
                f"Refusing to load third-party app {app_name!r} module "
                f"{module_path!r}: in-process execution of untrusted app code is "
                f"disabled by agent.apps_allow_third_party=false. Set it to true in "
                f"~/.kiro/crew/config.json to re-enable (accepting that app code runs "
                f"with full gateway privileges)."
            )
        _warn_third_party_execution(app_name)

    # Load the module
    spec = importlib.util.spec_from_file_location(unique_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to create module spec for {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # Clean up on failure
        sys.modules.pop(unique_name, None)
        raise ImportError(
            f"Failed to execute module {file_path}: {exc}"
        ) from exc

    # Get the callable
    if not hasattr(module, callable_name):
        sys.modules.pop(unique_name, None)
        raise ImportError(
            f"Module {file_path} has no attribute {callable_name!r}. "
            f"Available: {[a for a in dir(module) if not a.startswith('_')]}"
        )

    func = getattr(module, callable_name)
    if not callable(func):
        sys.modules.pop(unique_name, None)
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


def unload_app_modules(app_name: str) -> int:
    """Remove all cached modules for an app from sys.modules.

    Called on app disable to ensure re-enable loads fresh code.
    Returns the number of modules removed.
    """
    prefix = f"_kirocrew_app_{app_name}."
    to_remove = [k for k in sys.modules if k.startswith(prefix)]
    for k in to_remove:
        del sys.modules[k]
    if to_remove:
        logger.debug("Unloaded %d module(s) for app %s", len(to_remove), app_name)
    return len(to_remove)


def is_app_module_loaded(app_name: str) -> bool:
    """Check if any modules are loaded for an app."""
    prefix = f"_kirocrew_app_{app_name}."
    return any(k.startswith(prefix) for k in sys.modules)
