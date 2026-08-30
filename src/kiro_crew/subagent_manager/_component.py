"""Shared plumbing for composed SubagentManager coordinators."""

from __future__ import annotations

from types import FunctionType
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from ..subagent import SubagentManager


class ManagerComponent:
    """Hold the facade that owns all mutable manager state."""

    _manager: SubagentManager
    __slots__ = ("_manager",)

    def __init__(self, manager: SubagentManager) -> None:
        object.__setattr__(self, "_manager", manager)


def bind_component_globals(
    component_types: Iterable[type[ManagerComponent]], namespace: dict[str, Any]
) -> None:
    """Bind implementations to ``subagent`` globals for patch compatibility."""
    for component_type in component_types:
        for name, implementation in tuple(vars(component_type).items()):
            if not name.endswith("_impl") or not isinstance(implementation, FunctionType):
                continue
            if implementation.__globals__ is namespace:
                continue
            rebound = FunctionType(
                implementation.__code__,
                namespace,
                implementation.__name__,
                implementation.__defaults__,
                implementation.__closure__,
            )
            rebound.__kwdefaults__ = implementation.__kwdefaults__
            rebound.__annotations__ = implementation.__annotations__
            rebound.__dict__.update(implementation.__dict__)
            rebound.__doc__ = implementation.__doc__
            rebound.__module__ = implementation.__module__
            rebound.__qualname__ = implementation.__qualname__
            setattr(component_type, name, rebound)


def copy_component_docs(
    facade_type: type[Any], component_types: Iterable[type[ManagerComponent]]
) -> None:
    """Keep the facade's runtime method documentation intact."""
    for component_type in component_types:
        for name, implementation in vars(component_type).items():
            if not name.endswith("_impl") or not isinstance(implementation, FunctionType):
                continue
            facade_method = getattr(facade_type, name.removesuffix("_impl"))
            facade_method.__doc__ = implementation.__doc__
