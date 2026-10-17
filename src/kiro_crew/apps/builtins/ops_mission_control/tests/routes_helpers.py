"""Source-scan helpers shared by the ops-mission-control route tests (``test_routes*.py``).

The HTTP surface is ``routes`` plus every module of its ``http_routes`` package, so a guard
that scanned ``routes.py`` alone would pass vacuously once a handler's body lives in a
projection. These helpers return the source of the whole surface, or of one handler together
with the projection it delegates to.
"""

from pathlib import Path

from kiro_crew.apps.builtins.ops_mission_control.backend import routes


def _http_surface_modules() -> list:
    """``routes`` plus every module of the ``http_routes`` package whose handlers it composes.

    Enumerated from the package directory rather than listed, so a module added there later
    is scanned by every source guard below without anyone remembering to add it.
    """
    import importlib

    package = Path(routes.__file__).parent / "http_routes"
    modules = [routes]
    for path in sorted(package.rglob("*.py")):
        dotted = ".".join(path.relative_to(package).with_suffix("").parts)
        name = routes.__package__ + ".http_routes"
        if dotted != "__init__":
            name += "." + dotted.removesuffix(".__init__")
        modules.append(importlib.import_module(name))
    return modules


def _http_surface_source() -> str:
    """The source of the whole HTTP surface: the facade and every projection it composes."""
    import inspect

    return "\n".join(inspect.getsource(module) for module in _http_surface_modules())


def _handler_source(name: str) -> str:
    """``routes.<name>`` plus the same-named projection it delegates to, when it is a shell.

    A facade handler that forwards to a projection holds none of the code a scan is about,
    so scanning it alone would pass vacuously. Both are returned, so the scan covers the
    object the router serves AND the body that does the work.
    """
    import inspect

    served = getattr(routes, name)
    sources = [inspect.getsource(served)]
    for module in _http_surface_modules()[1:]:
        projection = getattr(module, name, None)
        if projection is not None and projection is not served:
            sources.append(inspect.getsource(projection))
    return "\n".join(sources)
