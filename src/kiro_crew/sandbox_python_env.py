"""The gateway's own Python environment, and the spawns that may write it.

A packaged install runs from an environment (a virtual environment, the desktop
bundle's embedded interpreter, or the ``site-packages`` directory of a shared
interpreter) whose contents the gateway loads on its next start. The planner seals it
read-only beside the package directory (:func:`kiro_crew.sandbox_tiers.install_root_targets_for`)
for every sandboxed spawn but a provisioning one: a gateway-run step that installs
Python packages into that environment for the gateway, which asks for it with
``wrap_argv_async(..., provisioning=True)``.
"""

from __future__ import annotations

import contextvars
import functools
import os
import sys
from types import ModuleType
from typing import Callable, TypeVar

from kiro_crew import platform_compat, sandbox_tiers


def python_env_targets_for(
    package_dir: str, *, prefix: str, base_prefix: str, bundled: bool, home: str
) -> list[str]:
    """The Python environment to seal read-only around a package at *package_dir*.

    Empty unless the package is a packaged install
    (:func:`~kiro_crew.sandbox_tiers.install_root_targets_for`). When the interpreter's
    own tree (``sys.prefix``, given as *prefix*) belongs to the install alone -- a
    virtual environment, where it differs from *base_prefix*, or the desktop bundle's
    embedded interpreter (*bundled*) -- and holds the package, the whole tree is the
    environment: its ``site-packages`` with every ``.pth`` file and dependency, its
    ``bin`` scripts and its ``pyvenv.cfg``. Any other layout (a system or ``--user``
    install) seals the ``site-packages`` directory holding the package, never the
    shared interpreter beside it, and so does a tree that is or contains *home*.

    Lexical only, so it never stats; :func:`resolved_python_env_targets` adds the
    canonical spellings.
    """
    return _python_env_targets_with(package_dir, prefix, base_prefix, bundled, home, os.path)


def _python_env_targets_with(
    package_dir: str, prefix: str, base_prefix: str, bundled: bool, home: str, paths: ModuleType
) -> list[str]:
    """:func:`python_env_targets_for` against one platform's path module."""
    packaged = sandbox_tiers._install_root_targets_with(package_dir, paths)
    if not packaged:
        return []
    site = paths.dirname(packaged[0])
    root = paths.normpath(paths.abspath(prefix))
    base = paths.normpath(paths.abspath(base_prefix))
    own_tree = bundled or paths.normcase(root) != paths.normcase(base)
    home_dir = paths.normpath(paths.abspath(home))
    if own_tree and _within(site, root, paths) and not _within(home_dir, root, paths):
        return [root]
    return [site]


def _within(path: str, parent: str, paths: ModuleType) -> bool:
    """Is *path* at or below *parent*, compared lexically?"""
    try:
        return paths.normcase(paths.commonpath([path, parent])) == paths.normcase(parent)
    except ValueError:
        return False


@functools.lru_cache(maxsize=1)
def _python_env_targets_cached(
    package_dir: str, prefix: str, base_prefix: str, executable: str, home: str
) -> tuple[str, ...]:
    # ``executable`` keys the cache: the bundled-interpreter check reads it.
    lexical = python_env_targets_for(
        package_dir,
        prefix=prefix,
        base_prefix=base_prefix,
        bundled=platform_compat.is_bundled_interpreter(),
        home=home,
    )
    spellings: list[str] = []
    for target in lexical:
        spellings.append(target)
        try:
            spellings.append(os.path.realpath(target))
        except (OSError, ValueError):
            pass
    if lexical:
        spellings.extend(_managed_stable_link_spellings())
    return tuple(dict.fromkeys(spellings))


def _managed_stable_link_spellings() -> list[str]:
    """The managed install's stable link and the tree it names, when this process runs
    from that install.

    The link is the name the gateway's next start resolves through, so it is sealed
    beside the tree the process runs from. Seatbelt matches the link's own path; the
    namespace launcher seals the tree it resolves to.
    """
    try:
        from kiro_crew.platform import wheel_engine

        layout = wheel_engine.managed_venv_layout()
        if not wheel_engine.running_from_managed_venv(layout):
            return []
        link = os.path.normpath(str(layout.stable_link))
        return [link, os.path.realpath(link)]
    except Exception:
        return []


def resolved_python_env_targets() -> list[str]:
    """The running gateway's own Python environment, sealed read-only for every
    sandboxed child but a provisioning one.

    Empty for an editable or from-source checkout (see :func:`python_env_targets_for`).
    Computed once per process, like the package directory's seal. Never raises.
    """
    try:
        return list(
            _python_env_targets_cached(
                sandbox_tiers._gateway_package_dir(),
                sys.prefix,
                sys.base_prefix,
                sys.executable,
                os.path.expanduser("~"),
            )
        )
    except Exception:
        return []


#: True only inside :func:`run_as_provisioning`, which ``wrap_argv_async`` uses for a
#: spawn its caller marks ``provisioning=True``. Read by the planner into
#: :attr:`kiro_crew.sandbox_plan.SandboxRequest.provisioning`.
_PROVISIONING_SPAWN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kiro_crew_sandbox_provisioning_spawn", default=False
)

_T = TypeVar("_T")


def run_as_provisioning(prepare: Callable[[], _T]) -> _T:
    """Run *prepare*, one spawn's sandbox preparation, as a provisioning spawn.

    A provisioning spawn installs Python packages into the gateway's own environment
    for the gateway (an optional extra, an app's Python build), so its plan leaves
    that environment writable; the package directory stays sealed. Set and reset in
    the calling thread, so it reaches exactly the preparation it wraps.
    """
    token = _PROVISIONING_SPAWN.set(True)
    try:
        return prepare()
    finally:
        _PROVISIONING_SPAWN.reset(token)


def provisioning_spawn_requested() -> bool:
    """Is the spawn being prepared in this context a provisioning spawn?"""
    return _PROVISIONING_SPAWN.get()
