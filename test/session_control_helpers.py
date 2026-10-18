"""Shared fixtures and helpers for the session-control tests that import them.

Every importer binds the three autouse fixtures by import: ``_enabled`` patches
``sc.session_control_enabled`` to return True, while ``_fresh_stop_windows`` and
``_fresh_create_budget`` reset the ``stop_retry`` and ``create_rate_limit`` state
around a test. ``_enabled`` is declared as ``_enabled_fixture`` with
``name="_enabled"``, so a test-local ``def _enabled`` cannot shadow the import.

``_REAL_ENABLED`` keeps the unpatched ``sc.session_control_enabled``. ``_slot`` and
``_peer_target`` get or create a slot, ``_key`` returns its session key, ``_busy``
gives it an unfinished task, ``_steerable`` builds a steer-capable client, and
``_agent_resolves`` makes the effective agent resolve and bind to a workspace.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stop_retry
from kiro_crew.dashboard.chat_utils import slot_history_key

# The autouse fixture below replaces ``sc.session_control_enabled`` so every
# other test runs in the shipped (enabled) state without reading config. Keep a
# handle on the real function so the tests that are ABOUT that function can call
# it — it still resolves ``KiroCrewConfig`` through module globals, so patching
# the config class continues to work through this reference.
_REAL_ENABLED = sc.session_control_enabled


@pytest.fixture(name="_enabled", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse enable stub re-declared as _enabled_fixture under its fixture name with the body verbatim; no test in the test_session_control*.py modules calls monkeypatch.undo()
def _enabled_fixture(monkeypatch):
    """Default every test to the shipped state (enabled) without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_stop_windows():
    """The stop-retry window is process-wide module state.

    Left behind, one test's stop makes a later test's FIRST stop read as a repeat
    — so escalation would be withheld from a test that never retried anything.
    """
    stop_retry.reset_for_tests()
    yield
    stop_retry.reset_for_tests()


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state.

    Nearly every create test here calls as the same caller (``chat-1``), so the
    20-per-window budget is consumed by the file itself: left behind, the 21st
    create in a worker process is refused with ``create_rate_limited`` and the
    test that happens to be 21st fails for a reason it never asserted. Which
    test that is depends on xdist distribution, which is what made it flaky.
    """
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    """The session key the MCP process would present for *slot*."""
    return slot_history_key(slot)


def _peer_target(state, name: str, caller, **kwargs):
    """A peer session in the caller's workspace, addressable by the other verbs."""
    return state.get_or_create_slot(name, **kwargs)


def _busy(slot):
    """Make *slot* look like a turn is in flight.

    ``running`` is derived (``task is not None and not task.done()``), so a busy
    slot is expressed by its task — assigning ``running`` would raise.
    """
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _steerable(accepted: bool = True) -> MagicMock:
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=accepted)
    return client


def _agent_resolves(monkeypatch, workspace: str) -> None:
    """Make the effective agent resolve and be bound to *workspace*.

    Tests whose subject is not agent resolution still have to get past the binding
    check, which refuses a name nothing would dispatch. Forcing the honored flag
    keeps those tests focused without weakening the check -- the refusal itself is
    covered by `test_an_agent_name_that_does_not_resolve_is_refused`.
    """
    real = sc.resolve_agent_bindings
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: workspace)
    monkeypatch.setattr(
        sc,
        "resolve_agent_bindings",
        lambda cfg, agent_name=None, project_dir=None, **kwargs: dataclasses.replace(
            real(cfg, None, project_dir), requested_resolved=True
        ),
    )
