"""Fake ``sys.modules`` entries for a block without ever emptying the table.

``mock.patch.dict(sys.modules, ...)`` restores on exit with ``clear()`` followed by
``update(original)``. Between the two calls the table is empty, and a thread that
imports ``kiro_crew.something`` in that gap starts loading a second ``kiro_crew``
package; when it registers the package after the refill, ``sys.modules["kiro_crew"]``
is that new object, which has none of the submodule attributes the already-imported
modules were bound under. Every later ``monkeypatch.setattr("kiro_crew.mod.name", ...)``
on that worker then fails with ``AttributeError``.

:func:`patched_sys_modules` gives a block the same view ``patch.dict`` gives it and
restores key by key: an entry the block added is removed, an entry it changed or
removed is put back, and every other entry stays in the table throughout.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, MutableMapping
from types import TracebackType
from typing import Any

__all__ = ["patched_sys_modules"]

_ABSENT: Any = object()


class patched_sys_modules:  # noqa: N801 - used as a function, like ``patch.dict``
    """Context manager: ``sys.modules`` with *values* set, restored key by key on exit.

    ``patched_sys_modules({"pip": None})`` makes ``import pip`` fail inside the block;
    ``patched_sys_modules()`` only restores. On exit the table holds exactly what it
    held on entry, as after ``patch.dict``: a module first imported inside the block
    is removed again. One instance may be entered more than once, nested or in turn.
    """

    def __init__(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        _table: MutableMapping[str, Any] | None = None,
    ) -> None:
        self._values = dict(values or {})
        self._table = sys.modules if _table is None else _table
        self._entered: list[dict[str, Any]] = []

    def __enter__(self) -> MutableMapping[str, Any]:
        self._entered.append(dict(self._table))
        self._table.update(self._values)
        return self._table

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        table = self._table
        before = self._entered.pop()
        for name in [name for name in list(table) if name not in before]:
            table.pop(name, None)
        for name, module in before.items():
            if table.get(name, _ABSENT) is not module:
                table[name] = module
