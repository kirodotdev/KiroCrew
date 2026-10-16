"""Run a split module's owner functions on its facade's namespace.

When a large module is split into private owner modules and its original path stays
as the facade -- ``kiro_crew.slack.gateway``, ``kiro_crew.dashboard.chat_handlers``,
``kiro_crew.hooks`` and the rest -- the facade remains the one import path and the
one patch surface. Tests patch the facade's names (``chat_handlers.sel``,
``gateway.stream_and_collect``, ``hooks.validate_file_path`` and hundreds more). A
moved function that read its OWNER's globals would keep calling the unpatched object
and the test would pass while exercising nothing. :func:`compose` therefore rebinds
every function an owner defines -- its module functions, the methods of the classes it
defines and, for a facade with an orchestrator class, the methods that class took from
the owners -- onto the facade's module globals, so the facade stays ONE namespace, as
the one-module file was.

Each facade calls :func:`compose` once, at the foot of its body, and each owner package's
``__init__`` records what its facade relies on: which names the tests patch, what each
owner's imports may do, and where the facade's state lives. Every owner package's
composition-contract test forbids importing that package from outside its facade, which
is why the helper lives here, in a module no facade owns, rather than in any one of them.

``kiro_crew.dashboard.chat_runner`` is the one composed facade that does not call it
directly: it calls ``kiro_crew.dashboard.chat_turn.compose``, which binds on the runner
every name an owner defines that the runner lacks and then calls this one, and its
package finds the owners itself (``chat_turn.owner_modules()``). Those two behaviours
are the runner's contract alone, so they stay in its package rather than in this
shared helper.

This is not the write fan-out of :mod:`kiro_crew._facade_mirror` or of ``apps/backend.py``,
which copy a name written through the facade into every module that holds its own
binding; rebinding leaves one namespace to patch, so no copy can go stale. Nor is it
``subagent_manager._component.bind_component_globals``, which rebinds the ``*_impl``
methods of coordinator objects a manager holds: here the functions ARE the facade's
functions and methods, with no object in between for a ``__new__`` fixture to miss.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import CodeType, FunctionType, ModuleType
from typing import Any


def compose(
    namespace: dict[str, Any], owners: Iterable[ModuleType], host: type | None = None
) -> None:
    """Run every function *owners* define on *namespace*; bind *host*'s methods too.

    *namespace* is the facade's ``globals()``. The facade calls this once, after its
    own body has bound every name, so a single pass replaces every binding of an
    owner function -- in the facade, in the owners, in the owners' classes and in
    *host* -- with its rebound copy, and no module is left holding the original.

    *host* is the facade's orchestrator class when it has one (the Slack gateway's
    ``GatewayOrchestrator``, ``kiro_crew.subagent``'s ``SubagentManager``): an owner function whose first parameter is ``self``
    is bound as the class attribute of the same name, and this pass swaps that
    binding for the rebound copy and restores its ``__qualname__`` -- and the
    qualnames of the code nested in it -- to ``Host.<name>``, as the one-module
    class body spelled it. A facade with no such class passes nothing.

    A function is an owner's when its code was compiled from that owner's file,
    whatever globals it carries: a facade imported a second time into the same
    process rebinds the owners onto its fresh namespace. The owners are imported
    once per process, so their classes are shared by every facade import and their
    methods follow the most recent one.
    """
    owners = tuple(owners)
    rebound: dict[int, tuple[FunctionType, FunctionType]] = {}
    classes: list[type] = []
    for owner in owners:
        path = owner.__file__
        for value in list(vars(owner).values()):
            if isinstance(value, FunctionType) and value.__code__.co_filename == path:
                rebound[id(value)] = (value, rebind(value, namespace))
            elif isinstance(value, type) and value.__module__ == owner.__name__:
                classes.append(value)
                for member in vars(value).values():
                    for fn in _functions_of(member):
                        if fn.__code__.co_filename == path:
                            rebound[id(fn)] = (fn, rebind(fn, namespace))

    def _swap(value: Any) -> Any:
        if isinstance(value, FunctionType):
            pair = rebound.get(id(value))
            return pair[1] if pair is not None and pair[0] is value else value
        if isinstance(value, (staticmethod, classmethod)):
            fn = _swap(value.__func__)
            return value if fn is value.__func__ else type(value)(fn)
        if isinstance(value, property):
            parts = (_swap(value.fget), _swap(value.fset), _swap(value.fdel))
            if parts == (value.fget, value.fset, value.fdel):
                return value
            return property(*parts, value.__doc__)
        return value

    for holder in [namespace, *(vars(owner) for owner in owners)]:
        for name, value in list(holder.items()):
            swapped = _swap(value)
            if swapped is not value:
                holder[name] = swapped
    for cls in classes if host is None else [*classes, host]:
        for name, value in list(vars(cls).items()):
            swapped = _swap(value)
            if swapped is not value:
                setattr(cls, name, swapped)

    if host is None:
        return
    fresh = {id(new) for _, new in rebound.values()}
    prefix = f"{host.__qualname__}."
    for name, value in vars(host).items():
        for fn in _functions_of(value):
            if id(fn) in fresh:
                fn.__qualname__ = prefix + name
                fn.__code__ = _requalify(fn.__code__, prefix)


def _functions_of(member: Any) -> list[FunctionType]:
    """The plain functions a class member wraps: itself, a static/class method, a property."""
    if isinstance(member, FunctionType):
        return [member]
    if isinstance(member, (staticmethod, classmethod)) and isinstance(
        member.__func__, FunctionType
    ):
        return [member.__func__]
    if isinstance(member, property):
        return [
            fn for fn in (member.fget, member.fset, member.fdel) if isinstance(fn, FunctionType)
        ]
    return []


def rebind(fn: FunctionType, namespace: dict[str, Any]) -> FunctionType:
    """A copy of *fn* whose globals are *namespace*; every other attribute kept.

    Public because a facade calls it directly for a function :func:`compose` does not
    reach, such as the generator a ``@contextmanager`` wraps.
    """
    new = FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
    new.__kwdefaults__ = fn.__kwdefaults__
    new.__annotations__ = fn.__annotations__
    new.__dict__.update(fn.__dict__)
    new.__doc__ = fn.__doc__
    new.__qualname__ = fn.__qualname__
    new.__module__ = namespace["__name__"]
    new.__type_params__ = fn.__type_params__
    return new


def _requalify(code: CodeType, prefix: str) -> CodeType:
    """*code* and every code object nested in it, with *prefix* before the qualname.

    Idempotent, so a facade imported a second time does not stack the prefix.
    """
    if code.co_qualname.startswith(prefix):
        return code
    consts = tuple(_requalify(c, prefix) if isinstance(c, CodeType) else c for c in code.co_consts)
    return code.replace(co_qualname=prefix + code.co_qualname, co_consts=consts)
