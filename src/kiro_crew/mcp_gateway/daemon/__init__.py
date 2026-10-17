"""Private owners composed by :mod:`kiro_crew.mcp_gateway.gatewayd`.

``gatewayd`` is the daemon's executable, its only import path and its one patch
surface. Every name an owner here holds is re-exported there, and nothing outside
``gatewayd`` imports these modules. Which owner holds which rule is mapped in
``docs/system-specs/modules/mcp-gateway-daemon-lifecycle.md`` under "Where the
daemon's code lives".

Two objects make ``gatewayd``'s namespace the one the owners run against:

* :data:`facade` resolves an attribute on the running ``gatewayd`` module at the
  moment of use. An owner reads every name a test patches on ``gatewayd`` through
  it (``facade._acquire_backend(...)``), so the patch reaches the owner's call site
  exactly as it reaches ``gatewayd``'s own code. Every other name an owner uses
  resolves in that owner's own globals. The composition contract test derives the
  patched names from the test tree and fails on an owner that reads one bare.
* :data:`logger` is ``gatewayd.logger``, read at each use, so every line the daemon
  writes keeps the one logger it always had: ``kiro_crew.mcp_gateway.gatewayd`` when
  imported, ``__main__`` under ``python -m``.

The facade is looked up, never imported by an owner at module scope: ``gatewayd``
imports every owner, so it is loaded before any owner function runs. Under
``python -m`` the running module is ``__main__``, and ``gatewayd`` registers it under
its import name before ``main()`` runs, so the lookup finds the module that is
actually running rather than importing a second copy.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

#: ``gatewayd``'s import name, the key the running facade is found under.
_FACADE = __name__.rpartition(".")[0] + ".gatewayd"


def _facade_module() -> ModuleType:
    """The running facade module, resolved at call time.

    :data:`sys.modules` answers first: ``importlib.import_module`` is an attribute a
    test can patch, and routing every read through it would hand the daemon to that
    patch while it is installed. The import answers only a facade that was purged.
    """
    try:
        return sys.modules[_FACADE]
    except KeyError:
        return importlib.import_module(_FACADE)


class _FacadeNamespace:
    """``gatewayd``'s namespace, read at the moment of use and never written through."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(_facade_module(), name)


class _FacadeLogger:
    """``gatewayd.logger``, read at the moment of use."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(_facade_module().logger, name)


if TYPE_CHECKING:
    import logging

    # Owners import ``gatewayd`` itself as ``facade`` for the type checker.
    logger: logging.Logger
else:
    facade = _FacadeNamespace()
    logger = _FacadeLogger()
