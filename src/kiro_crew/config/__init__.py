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

from typing import TYPE_CHECKING

from kiro_crew import lazy_exports as _lazy_exports

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


#: The loader owns every public name, and is imported on first access rather than
#: here: ``config.loader`` imports ``kiro_crew.config.paths``, which triggers this
#: package ``__init__``, so a top-level import would create an init <-> loader
#: runtime cycle AND eagerly pull the heavy loader in whenever the config package
#: is touched (including from the lightweight ``config.paths`` leaf), defeating
#: this PEP 562 lazy seam.
__getattr__ = _lazy_exports.bind(
    __name__, {name: ("kiro_crew.config.loader", name) for name in __all__}
)


def __dir__() -> list[str]:
    return sorted(__all__)
