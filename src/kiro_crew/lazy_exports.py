"""One storage location for a lazily re-exported name.

A package that re-exports its public surface lazily (:pep:`562`) has two places
each name could live: the submodule that defines it, and the package's own
namespace. A module-level ``__getattr__`` runs only for a name the package does
not already hold, so the moment anything binds the name in the package -- a
``__getattr__`` that memoises what it resolved, or an ordinary ``setattr`` on the
package -- that binding wins every later read and the owner's value is
unreachable through the package.

That is what makes such a write undoable, and restore-by-reassign is the protocol
a test harness uses: ``pytest``'s ``monkeypatch`` reads an attribute to remember
it, then assigns the remembered value back. A harness that patches the owner
first and the package second reads its package baseline THROUGH the already
patched owner, so what it remembers is the patched value, and its teardown
installs that value in the package for the rest of the process. The next test in
the same worker reads it as its own baseline. The test that caused it passes;
another one fails, in another file, under some orderings and shard splits and not
others.

:func:`bind` gives one storage location instead:

* the ``__getattr__`` it returns caches the IMPORT and never the value, so every
  read resolves the owner's current value;
* the :class:`~types.ModuleType` subclass it installs sends ``setattr`` and
  ``delattr`` for a re-exported name to the submodule that owns it.

One value to remember, and one value to put back.

Stdlib-only on purpose. These packages re-export lazily to keep a heavy import
off a cold path, so a helper that pulled anything in would spend what they save;
``importlib``, ``sys`` and ``types`` are already resident in any interpreter.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any, Callable


def bind(package: str, owners: dict[str, tuple[str, str]]) -> Callable[[str], Any]:
    """Give every name in *owners* one storage location, and return ``__getattr__``.

    *owners* maps a package attribute to ``(absolute owner module, symbol on that
    owner)``. The attribute and the symbol are separate because they are allowed
    to differ: ``mcp_gateway.stub_main`` is ``mcp_gateway.stub.main``.

    Call from a package ``__init__`` and assign the result::

        __getattr__ = lazy_exports.bind(
            __name__, {name: (f"{__name__}.{owner}", name) for name, owner in _EXPORTS.items()}
        )

    A name in *owners* must not also name a submodule of *package*: the import
    system binds a submodule onto its parent with ``setattr``, which this would
    forward to the owner instead. ``test_lazy_reexport_storage`` pins that no
    caller has such a name.

    An owner module is imported on first use and never at ``bind`` time, so a
    package whose owner imports back into the package (``config.loader`` reaches
    ``config.paths``) keeps working, and a caller that only wants a leaf
    submodule still pays nothing for the heavy one.
    """
    imported: dict[str, ModuleType] = {}

    def owner_of(name: str) -> ModuleType:
        module_name = owners[name][0]
        module = imported.get(module_name)
        if module is None:
            module = imported[module_name] = importlib.import_module(module_name)
        return module

    def module_getattr(name: str) -> Any:
        """Read a re-exported name from the submodule that owns it (:pep:`562`)."""
        if name not in owners:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        return getattr(owner_of(name), owners[name][1])

    class _ReExportModule(ModuleType):
        """Send a write to a re-exported name to the submodule that owns it."""

        def __setattr__(self, name: str, value: Any) -> None:
            if name in owners:
                setattr(owner_of(name), owners[name][1], value)
            else:
                super().__setattr__(name, value)

        def __delattr__(self, name: str) -> None:
            if name in owners:
                delattr(owner_of(name), owners[name][1])
            else:
                super().__delattr__(name)

    sys.modules[package].__class__ = _ReExportModule
    return module_getattr
