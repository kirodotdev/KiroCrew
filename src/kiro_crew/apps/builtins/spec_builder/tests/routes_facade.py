"""Test-only access to Spec Builder backend implementation seams."""

from __future__ import annotations

import inspect
from types import ModuleType

from ..backend import decisions, handlers, parsers, repository
from ..backend import routes as composition
from ..backend import runtime

BACKEND_MODULES: tuple[ModuleType, ...] = (
    parsers,
    repository,
    decisions,
    runtime,
    handlers,
    composition,
)


class _RoutesFacade:
    """Fan test patches out to every module that captured a shared binding."""

    __slots__ = ("_hidden", "_modules")

    def __init__(self, modules: tuple[ModuleType, ...]) -> None:
        object.__setattr__(self, "_modules", modules)
        object.__setattr__(self, "_hidden", set())

    def __getattr__(self, name: str):
        if name.startswith("__") or name in self._hidden:
            raise AttributeError(name)
        for module in self._modules:
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        matched = False
        self._hidden.discard(name)
        for module in self._modules:
            if name in vars(module):
                setattr(module, name, value)
                matched = True
        if not matched:
            raise AttributeError(name)

    def __delattr__(self, name: str) -> None:
        if name.startswith("__") or not any(name in vars(module) for module in self._modules):
            raise AttributeError(name)
        self._hidden.add(name)

    def __dir__(self) -> list[str]:
        names = set().union(*(vars(module) for module in self._modules))
        return sorted(name for name in names if name not in self._hidden)


routes = _RoutesFacade(BACKEND_MODULES)


def backend_namespace() -> dict[str, object]:
    """Return the implementation namespace visible through ``routes``."""
    merged: dict[str, object] = {}
    for module in reversed(BACKEND_MODULES):
        merged.update(vars(module))
    return merged


def routes_source() -> str:
    """Return parseable aggregate source for cross-module invariant tests."""
    sources = []
    for module in BACKEND_MODULES:
        source = inspect.getsource(module)
        source = source.replace("from __future__ import annotations\n", "")
        sources.append(source)
    return "\n\n".join(sources)
