"""The eager barrier waits for a ``slot_prime`` import another thread is running."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest

from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.testing.wait import default_timeout


@pytest.fixture(autouse=True)
def _live_crew_log(tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        patch.setenv("KIROCREW_CREW_LOG", "1")
        crew_log_emit.reset_caches()
        crew_log_eager.stop_for_tests()
        yield patch
        crew_log_emit.flush()
        crew_log_eager.stop_for_tests()
        crew_log_emit.reset_caches()


def _mid_import(patch: pytest.MonkeyPatch, reader: Any, name: str) -> list[str]:
    """List *name* as another thread's import leaves it before its body ran.

    ``import_module`` is what waits for that import; it is stood in for here on
    *reader* (the module that looks *name* up), returning the finished module, so the
    test needs no second thread racing the first import.
    """
    finished = types.SimpleNamespace(drain=lambda _t: True)
    asked: list[str] = []

    def _import(target: str) -> Any:
        asked.append(target)
        return finished if target == name else importlib.import_module(target)

    patch.setitem(sys.modules, name, types.ModuleType(name))
    patch.setattr(reader, "importlib", types.SimpleNamespace(import_module=_import), raising=False)
    return asked


def test_the_eager_barrier_waits_for_the_prime_module_its_worker_is_importing():
    """A half-imported ``slot_prime`` has no ``drain`` yet; reading it raised."""
    name = "kiro_crew.crew_log.slot_prime"
    with pytest.MonkeyPatch.context() as patch:
        asked = _mid_import(patch, crew_log_eager, name)
        drained = crew_log_eager.drain(timeout=default_timeout())

    assert drained
    assert name in asked
