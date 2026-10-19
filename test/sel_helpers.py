"""Fixtures and helpers shared by the SEL test modules.

The autouse ``reset_singleton`` (a fresh ``SecurityEventLog`` singleton for every test),
the ``sel_dir``, ``log`` and ``small_segments`` fixtures (declared as ``<name>_fixture``
functions with ``name=``, so no test parameter shadows an import), ``_make_event`` and
``_fill``. ``test_sel.py`` and the topic modules its docstring names import them by name;
each is read by two or more of those modules.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.sel import SecurityEvent, SecurityEventLog


@pytest.fixture(name="small_segments")
def small_segments_fixture(monkeypatch):
    """Shrink the size cap so a handful of events triggers real rotation."""
    monkeypatch.setattr(sel_mod, "_SEGMENT_MAX_BYTES", 4 * 1024)
    monkeypatch.setattr(sel_mod, "_SEGMENT_KEEP", 3)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the SEL singleton between tests."""
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False
    yield
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False


@pytest.fixture(name="sel_dir")
def sel_dir_fixture(tmp_path):
    """Provide a temp directory for SEL storage."""
    return tmp_path


@pytest.fixture(name="log")
def log_fixture(sel_dir):
    """Create a fresh SEL instance in a temp dir.

    sync=True so events are written inline — these tests read the raw log file
    immediately after logging. The async background writer is covered
    separately in TestAsyncWriter.
    """
    return SecurityEventLog(base_dir=sel_dir, sync=True)


def _make_event(**overrides) -> SecurityEvent:
    """Build a SecurityEvent with sensible defaults for edge-case tests."""
    base = {
        "event_id": "extras-evt-0001",
        "timestamp": "2026-05-13T00:00:00+00:00",
        "event_type": "tool_invocation",
        "caller_identity": "dashboard:abc",
        "agent": "kirocrew",
        "source": "dashboard",
        "operation": "execute_bash",
    }
    base.update(overrides)
    return SecurityEvent(**base)


def _fill(log: SecurityEventLog, count: int, *, start: int = 0, step_secs: int = 1) -> None:
    """Write *count* chronologically increasing events, seq=<n> in ``resources``."""
    base = datetime(2026, 8, 21, tzinfo=timezone.utc)
    for i in range(start, start + count):
        log.log(
            _make_event(
                event_id=f"evt{i:06d}",
                timestamp=(base + timedelta(seconds=i * step_secs)).isoformat(),
                resources=f"seq={i}",
            )
        )
