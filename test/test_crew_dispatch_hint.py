"""Crew Mode dispatch hint: one notice row in the conductor's chat per person turn.

A successful ``session_create`` from a stamped Crew Mode root writes one ``notice``
row (``meta.kind == "crew_dispatch_hint"``) that names the Workers tab. A second
dispatch in the same turn adds nothing; a dispatch after the next ``user`` row writes
one more. A plain chat, an unstamped conductor, a worker's own sub-dispatch and a
refused create write none.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew import crew_mode
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.session_control_parts.caller_access import SessionControlError
from kiro_crew.dashboard.session_control_parts.crew_dispatch_hint import (
    CREW_DISPATCH_HINT_KIND,
    CREW_DISPATCH_HINT_TEXT,
)


@pytest.fixture
def crew_conductor(request):
    """The configured Crew Mode conductor is ``kirocrew`` for the test's span."""
    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="kirocrew")))
    request.addfinalizer(
        lambda: crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="")))
    )


def _root(state, *, stamped: bool = True):
    caller = _slot(state, "chat-1")
    caller.agent_kind = "template"
    caller.agent = "kirocrew"
    caller.crew_mode_agent = caller.agent if stamped else ""
    return caller


def _hints(slot) -> list[dict]:
    return [
        row
        for row in slot.messages
        if row.get("role") == "notice"
        and (row.get("meta") or {}).get("kind") == CREW_DISPATCH_HINT_KIND
    ]


def _create(state, caller, **kwargs):
    return asyncio.run(sc.create_session(state, caller_session_key=_key(caller), **kwargs))


def test_one_dispatch_writes_one_hint(tmp_path, crew_conductor):
    state = _make_state(tmp_path)
    root = _root(state)
    root.append("user", "Fix the bug", "msg msg-u")

    created = _create(state, root)

    assert created.get("crew_mode") is True
    hints = _hints(root)
    assert len(hints) == 1
    assert hints[0]["content"] == CREW_DISPATCH_HINT_TEXT
    # The worker's own transcript is untouched.
    assert _hints(state.get_slot(created["target"])) == []


def test_several_dispatches_in_one_turn_write_one_hint(tmp_path, crew_conductor):
    state = _make_state(tmp_path)
    root = _root(state)
    root.append("user", "Split this three ways", "msg msg-u")

    for _ in range(3):
        _create(state, root)
    assert len(_hints(root)) == 1, "a batch of dispatches in one turn must not spam"

    # The next person turn gets its own hint.
    root.append("user", "One more", "msg msg-u")
    _create(state, root)
    assert len(_hints(root)) == 2


def test_a_plain_chat_gets_no_hint(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.append("user", "Open a helper", "msg msg-u")

    created = _create(state, caller)

    assert "crew_mode" not in created
    assert _hints(caller) == []


def test_an_unstamped_conductor_root_gets_no_hint(tmp_path, monkeypatch):
    """The hint follows the Crew Mode stamp, like the folder rule."""
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", "kirocrew")
    state = _make_state(tmp_path)
    root = _root(state, stamped=False)

    _create(state, root)

    assert _hints(root) == []


def test_a_workers_own_dispatch_writes_no_hint(tmp_path, crew_conductor):
    state = _make_state(tmp_path)
    root = _root(state)
    worker = state.get_slot(_create(state, root)["target"])
    before = len(_hints(root))

    _create(state, worker)

    assert len(_hints(root)) == before
    assert _hints(worker) == []


def test_a_refused_dispatch_writes_no_hint(tmp_path, crew_conductor):
    state = _make_state(tmp_path)
    root = _root(state)
    root.append("user", "Go", "msg msg-u")

    with pytest.raises(SessionControlError):
        _create(state, root, folder_id="fold0000dead")

    assert _hints(root) == []
