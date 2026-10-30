"""Name the members a split class binds from its owner modules after the class.

A line-cap split can move a large class's method bodies into owner modules and bind each
one back as a class attribute (``prompt = _sh_turn.prompt``). The owner's function
annotates ``self`` so mypy checks its body as a method body, and its ``__qualname__``
names the owner function, not the class attribute. :func:`own_methods` runs once on the
built class and makes each bound member read as the method the one-module class had:
its ``inspect.signature`` (no ``self`` annotation) and its ``__qualname__``
(``AcpRuntime._spawn_admitted``), which tracebacks, logs and coroutine and task reprs
read. Every split class calls this one helper, so a change to the rule is made once.

A member counts as bound from an owner only when its function's ``__module__`` is in the
explicit *owners* set the caller passes. A module-prefix match is not used: a prefix can
also catch sibling modules that are not owners.
"""

from __future__ import annotations

from typing import TypeVar

_C = TypeVar("_C", bound=type)


def own_methods(cls: _C, owners: frozenset[str], *, module: str | None = None) -> _C:
    """Make each member *cls* binds from a module in *owners* read as a method of *cls*.

    For each class attribute, the function under a ``staticmethod`` or ``classmethod``
    is reached through ``__func__``, and a wrapper (``contextmanager`` and the like) is
    followed through ``__wrapped__`` while each function it reaches is an owner's: each
    loses its ``self`` annotation and is named ``<class qualname>.<attribute>``. Only
    ``__module__`` keeps naming the owner that holds the body.

    When *module* is given, the members are re-homed as well: ``__module__`` of each
    function is set to *module*, and the ``staticmethod`` or ``classmethod`` object
    itself is renamed and re-homed too, so every object reachable from the class
    attribute names the class's module. Returns *cls*, so this can decorate a class.
    """
    for name, member in vars(cls).items():
        function = member if module is not None else getattr(member, "__func__", member)
        while getattr(function, "__module__", None) in owners:
            function.__annotations__.pop("self", None)
            function.__qualname__ = f"{cls.__qualname__}.{name}"
            if module is not None:
                function.__module__ = module
            function = getattr(function, "__wrapped__", None)
    return cls
