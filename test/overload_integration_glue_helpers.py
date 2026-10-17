"""The clock, task store and runner doubles the overload integration-glue tests share.

``test_overload_integration_glue.py`` and its ``_store_off_loop`` and ``_spawn_queue``
siblings import these by name. ``clock``, ``store`` and ``no_memory_pressure`` are declared
``name=`` on ``*_fixture`` functions so no test parameter shadows the import.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from overload_fakes import Clock

from kiro_crew.taskq.store import TaskStore


@pytest.fixture(name="clock")
def clock_fixture() -> Clock:
    return Clock()


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, clock: Clock):
    s = TaskStore(tmp_path / "tasks.db", clock=clock).open()
    yield s
    s.close()


# ── gateway wiring ───────────────────────────────────────────────────────────


class _Runner:
    def __init__(self) -> None:
        self.attached: list[Any] = []

    def attach_task_admission(self, adm: Any) -> None:
        self.attached.append(adm)


# ── gateway wiring: the store-ready boundary ─────────────────────────────────


@pytest.fixture(name="no_memory_pressure")
def no_memory_pressure_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the probe ``runner_admission_for`` reads for itself.

    ``healthy_host_memory`` pins only the floor reading ``subagent`` spawns on;
    the runner admission reads the posture from ``resource_status``, so without
    this a loaded machine turns an admission into a defer.
    """
    from kiro_crew import resource_status

    monkeypatch.setattr(
        resource_status,
        "cached_admission_check",
        lambda: SimpleNamespace(admitted=True, reason=""),
    )
