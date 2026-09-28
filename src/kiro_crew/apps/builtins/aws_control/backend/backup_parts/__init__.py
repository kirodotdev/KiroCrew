"""Private owners composed by :mod:`kiro_crew.apps.builtins.aws_control.backend.backup`.

No production module outside this package imports them except the backup facade,
which re-exports every name they hold and is the only import path and patch surface.
The composition note at the end of ``backup.py`` says how a write through the facade
reaches the owners.
"""

#: The facade's module name. Every part logs through the facade's own logger, so a
#: handler, a log filter or a ``caplog`` capture keyed on ``...backend.backup`` keeps
#: seeing every line the backup engine writes, wherever the line now lives.
_FACADE_MODULE = __name__.rpartition(".")[0] + ".backup"
