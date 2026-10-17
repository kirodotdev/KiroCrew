"""Private owners composed by :mod:`kiro_crew.apps.backend`.

No production module outside this package imports them: the backend facade re-exports
every name they hold and is the only import path and patch surface. The composition note
at the end of ``backend.py`` says how a write through the facade reaches the owners, and
``docs/system-specs/modules/app-kit-platform.md`` §21 says which owner holds which rule.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

#: The facade's module name. Every owner logs through the facade's own logger, so a
#: handler, a log filter or a ``caplog`` capture keyed on ``kiro_crew.apps.backend``
#: keeps seeing every line the backend writes, wherever the line now lives.
_FACADE = __name__.rpartition(".")[0] + ".backend"


def _facade() -> ModuleType:
    """The facade module, resolved at call time.

    Two constructs stay in ``backend.py`` because repository guards read them there by
    path: the spawn transaction (``start_app_backend``, ``_start_app_backend`` and the
    spawn body) and ``_pid_alive``. The owners that call them look the facade up here
    when they run, never at import, so no owner imports the facade and a patch of
    ``backend._start_app_backend`` or ``backend._pid_alive`` still reaches its caller.

    :data:`sys.modules` answers first, as it does for the facade's own owner reads:
    ``importlib.import_module`` is an attribute any test can patch, and resolving these
    calls through it would hand them to that patch while it is installed. The facade is
    always loaded before an owner function can run -- nothing but the facade imports an
    owner -- so the import answers only a facade purged from ``sys.modules``.
    """
    try:
        return sys.modules[_FACADE]
    except KeyError:
        return importlib.import_module(_FACADE)
