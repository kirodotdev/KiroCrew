"""Leave a module's package attribute naming a different copy of that module.

Importing a module binds it onto its parent package. A test that imports a module
fresh behind a ``sys.modules`` patch and restores only the ``sys.modules`` entry
leaves the package attribute naming the fresh copy for the rest of the worker, so
``from package import module`` and ``sys.modules["package.module"]`` disagree.

The re-export facades resolve an owner through ``sys.modules``. A test that checks
what a facade wrote must read the owner from the same place, and this context
manager recreates the split so such a test can prove it does.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType


@contextmanager
def package_attribute_replaced(dotted: str) -> Iterator[ModuleType]:
    """Bind a copy of module *dotted* onto its package, and yield that copy.

    The module is imported first, because a facade may load its owners lazily. The
    ``sys.modules`` entry is left alone and the original attribute is put back on
    exit, so the split lasts only for the ``with`` block.
    """
    module = importlib.import_module(dotted)
    parent, _, leaf = dotted.rpartition(".")
    package = sys.modules[parent]
    bound = getattr(package, leaf)
    stale = ModuleType(dotted)
    stale.__dict__.update(vars(module))
    setattr(package, leaf, stale)
    try:
        yield stale
    finally:
        setattr(package, leaf, bound)
