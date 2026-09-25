"""Config package.

The public config surface (``KiroCrewConfig`` and the path helpers) is exposed
lazily via :pep:`562` ``__getattr__`` so that importing a lightweight submodule
such as :mod:`kiro_crew.config.paths` does NOT eagerly pull in the heavy
``kiro_crew.config.loader`` (DTOs, schema validation, the process-global cache,
and the lazily-imported provider factory). ``from kiro_crew.config import X``
continues to work for every name in ``__all__`` — it just resolves on first
access instead of at package import.

A re-exported name lives in exactly one place, the loader. Reading it through
this package reads the loader and writing it through this package writes the
loader, so the two spellings of a name cannot hold different values.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

__all__ = [
    "KiroCrewConfig",
    "config_dir",
    "config_local_path",
    "config_path",
    "data_home",
    "ensure_data_home",
    "env_path",
    "resolve_agent_config_path",
]

if TYPE_CHECKING:  # imported for type checkers only; no runtime loader import
    from kiro_crew.config.loader import (
        KiroCrewConfig,
        config_dir,
        config_local_path,
        config_path,
        data_home,
        ensure_data_home,
        env_path,
        resolve_agent_config_path,
    )


#: Package attribute -> ``(owning module, symbol on that module)``. The loader owns
#: every public name, and is imported on first access rather than here:
#: ``config.loader`` imports ``kiro_crew.config.paths``, which triggers this package
#: ``__init__``, so a top-level import would create an init <-> loader runtime cycle
#: AND eagerly pull the heavy loader in whenever the config package is touched
#: (including from the lightweight ``config.paths`` leaf), defeating this PEP 562
#: lazy seam.
_OWNED: dict[str, tuple[str, str]] = {name: ("kiro_crew.config.loader", name) for name in __all__}

_OWNERS: dict[str, ModuleType] = {}


def _owner(name: str) -> ModuleType:
    """Return the module that defines ``name``, importing it on first use."""
    module_name = _OWNED[name][0]
    owner = _OWNERS.get(module_name)
    if owner is None:
        owner = _OWNERS[module_name] = importlib.import_module(module_name)
    return owner


def __getattr__(name: str) -> Any:
    """Read a re-exported name from the module that owns it (:pep:`562`)."""
    if name not in _OWNED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner(name), _OWNED[name][1])


class _ReExportModule(ModuleType):
    """Send a write to a re-exported name to the module that owns it.

    Binding the name in this package's own namespace instead would shadow the
    owner permanently, because ``__getattr__`` runs only for a name the package
    does not already hold: the shadow would win every later read, and the owner's
    value would become unreachable through this package.

    That makes such a write undoable, which matters for the restore-by-reassign
    protocol a test harness uses (``pytest``'s ``monkeypatch`` reads the attribute
    to remember it, then assigns the remembered value back). Against a shadowing
    write, the value it reads is whatever the owner holds AT THAT MOMENT -- so a
    harness that patches the owner first remembers the patched value, and its
    restore installs that value in the package for the life of the process.
    Forwarding the write leaves one value to remember and one to put back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _OWNED:
            setattr(_owner(name), _OWNED[name][1], value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _OWNED:
            delattr(_owner(name), _OWNED[name][1])
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _ReExportModule


def __dir__() -> list[str]:
    return sorted(__all__)
