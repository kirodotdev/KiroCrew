"""The path-probe busy error keeps the module name callers import it from.

``_PathProbeBusy`` is defined in ``kiro_crew.dashboard.file_api.path_probe`` and served by
``kiro_crew.dashboard.handlers.files``, the import path the file routes, the terminal handler
and the Office slide handler use. A traceback or an error chain renders a raised class as
``module.qualname``, so the class names the facade, not the owner it is defined in.
"""

from __future__ import annotations

import traceback
from types import FunctionType

from kiro_crew.dashboard.file_api import path_probe
from kiro_crew.dashboard.handlers import files, office_slides, terminal

FACADE = "kiro_crew.dashboard.handlers.files"


def test_the_busy_error_names_the_facade_module() -> None:
    assert files._PathProbeBusy is path_probe._PathProbeBusy
    assert files._PathProbeBusy.__module__ == FACADE
    assert files._PathProbeBusy.__qualname__ == "_PathProbeBusy"


def test_a_raised_busy_error_renders_under_the_facade_name() -> None:
    try:
        raise files._PathProbeBusy("no worker")
    except files._PathProbeBusy as error:
        rendered = traceback.format_exception_only(type(error), error)[-1]
    assert rendered.startswith(f"{FACADE}._PathProbeBusy: no worker")


def test_every_importer_holds_the_one_class() -> None:
    assert terminal._PathProbeBusy is files._PathProbeBusy
    assert office_slides._files._PathProbeBusy is files._PathProbeBusy


def test_the_busy_error_defines_no_function_members() -> None:
    """``kiro_crew._facade_compose.compose`` rebinds the methods of a class whose
    ``__module__`` is its owner's. This class reports the facade's module, so compose never
    selects it, and a method defined on it would keep reading ``path_probe``'s globals instead
    of the facade's."""
    members = {
        name
        for name, value in vars(path_probe._PathProbeBusy).items()
        if isinstance(value, (FunctionType, staticmethod, classmethod, property))
    }
    assert members == set()
