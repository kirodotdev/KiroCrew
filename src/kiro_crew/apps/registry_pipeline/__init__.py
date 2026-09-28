"""Private owners composed by :mod:`kiro_crew.apps.registry`.

No production module outside this package imports them: the registry facade
re-exports every name they hold and is the only import path and patch surface. The
composition note at the end of ``registry.py`` says how a write through the facade
reaches the owners, and ``docs/system-specs/modules/app-kit-platform.md`` §20 says
which owner holds which rule.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

#: The facade's module name. Every owner logs through the facade's own logger, so a
#: handler, a log filter or a ``caplog`` capture keyed on ``kiro_crew.apps.registry``
#: keeps seeing every line the registry writes, wherever the line now lives.
_FACADE = __name__.rpartition(".")[0] + ".registry"


def _facade() -> ModuleType:
    """The facade module, resolved at call time.

    Three constructs stay in ``registry.py`` because repository guards read them there
    by path: the build step and the two index-entry name gates. The owners that call
    them look the facade up here when they run, never at import, so no owner imports
    the facade and a patch of ``registry._run_app_build`` still reaches its caller.

    :data:`sys.modules` answers first, as it does for the facade's own owner reads:
    ``importlib.import_module`` is an attribute any test can patch, and resolving the
    name gates through it would hand them to that patch while it is installed. The
    facade is always loaded before an owner function can run -- importing any owner
    imports ``kiro_crew.apps``, which imports the facade -- so the import answers only
    a facade purged from ``sys.modules``.
    """
    try:
        return sys.modules[_FACADE]
    except KeyError:
        return importlib.import_module(_FACADE)
