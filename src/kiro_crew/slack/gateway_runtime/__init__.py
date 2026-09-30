"""Private owners composed by :mod:`kiro_crew.slack.gateway`.

The gateway is split by responsibility across the modules of this package, and
``kiro_crew.slack.gateway`` stays its only import path and its only patch surface:
nothing but the facade imports an owner.

Each owner holds functions and classes taken verbatim from the one-module gateway.
A function whose first parameter is ``self`` is an orchestrator method: the facade
binds it as the ``GatewayOrchestrator`` attribute of the same name, so every call
site, ``patch.object`` target, ``GatewayOrchestrator.__new__`` fixture and unbound
``GatewayOrchestrator.<name>(stub, ...)`` call reaches it exactly as before. The
orchestrator remains the one holder of the state those methods act on; an owner
keeps no state of its own.

:func:`compose` makes every function an owner defines -- its module functions,
those methods, and the methods of the classes it defines -- run on the facade's
module globals rather than its own. Tests patch the gateway's names
(``kiro_crew.slack.gateway.sel``, ``.stream_and_collect``, ``.CronService`` and a
hundred more). A function that read its owner's globals would keep calling the
unpatched object and the test would pass while exercising nothing, so the gateway
stays ONE namespace, as the one-module file was. Two consequences follow:

* An owner's imports are inert for its functions. They sit under
  ``TYPE_CHECKING`` and name the facade, for the type checker and the linter;
  every name a function reads must exist in ``kiro_crew.slack.gateway``, and
  ``test_slack_gateway_composition_contract.py`` sweeps each function's bytecode
  to prove it does.
* A function's ``__module__`` and ``__qualname__`` read as they did before the
  split -- ``kiro_crew.slack.gateway`` and ``GatewayOrchestrator.<name>`` -- so
  reprs, pickling by reference and the names of nested functions are unchanged;
  only its source file differs. An owner's classes keep their own module, which is
  where ``inspect`` looks for a class's source.

This is not ``subagent_manager._component.bind_component_globals``: that rebinds the
``*_impl`` methods of coordinator objects the manager holds, while here the
functions ARE the orchestrator's methods and module functions, with no object in
between for a ``__new__`` fixture to miss. Nor is it the write fan-out facade
``apps/backend.py`` uses, which copies each patched name into every module that
holds it; rebinding leaves one namespace to patch, so no copy can go stale.

Which responsibility each owner holds, and which constructs stay in the facade
because repository guards read them there, is recorded in
``docs/system-specs/modules/slack-gateway.md`` (Composition).
"""

from __future__ import annotations

from collections.abc import Iterable
from types import CodeType, FunctionType, ModuleType
from typing import Any


def compose(namespace: dict[str, Any], host: type, owners: Iterable[ModuleType]) -> None:
    """Run every function *owners* define on *namespace*, and bind *host*'s methods.

    *namespace* is the facade's ``globals()`` and *host* its orchestrator class. The
    facade calls this once, after the class body has bound the owner methods, so a
    single pass replaces every binding of an owner function -- in the facade, in
    the owners, in the owners' classes and in *host* -- with its rebound copy, and
    no module is left holding the original.

    A function is an owner's when its code was compiled from that owner's file,
    whatever globals it carries: a facade imported a second time into the same
    process rebinds the owners onto its fresh namespace. The owners are imported
    once per process, so their classes are shared by every facade import and
    their methods follow the most recent one.
    """
    owners = tuple(owners)
    rebound: dict[int, tuple[FunctionType, FunctionType]] = {}
    classes: list[type] = []
    for owner in owners:
        path = owner.__file__
        for value in list(vars(owner).values()):
            if isinstance(value, FunctionType) and value.__code__.co_filename == path:
                rebound[id(value)] = (value, _rebind(value, namespace))
            elif isinstance(value, type) and value.__module__ == owner.__name__:
                classes.append(value)
                for member in vars(value).values():
                    for fn in _functions_of(member):
                        if fn.__code__.co_filename == path:
                            rebound[id(fn)] = (fn, _rebind(fn, namespace))

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
    for cls in [*classes, host]:
        for name, value in list(vars(cls).items()):
            swapped = _swap(value)
            if swapped is not value:
                setattr(cls, name, swapped)

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


def _rebind(fn: FunctionType, namespace: dict[str, Any]) -> FunctionType:
    """A copy of *fn* whose globals are *namespace*; every other attribute kept."""
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
