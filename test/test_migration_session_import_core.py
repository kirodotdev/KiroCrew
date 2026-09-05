"""Slice 3 (circle 7) — extractable session-import core (issue #7577, Task 3.1).

``api_chat_slot_import`` is an aiohttp handler bound to DashboardState; the
migration importer needs the SAME 'validated bundle -> new slot -> new id'
logic without a request. This defines that shared core as a pure function over
an injected slot-lifecycle interface, so the handler and the migration path
call one implementation (DRY). The real handler is refactored to call this in a
later live-environment step; here we pin the contract against a fake.

Side-effect discipline: fake state, no aiohttp, no real slots.
"""

from __future__ import annotations

import pytest

from kiro_crew.migration.session_import_core import import_session_core


class FakeState:
    """Minimal slot-lifecycle stand-in matching the order api_chat_slot_import uses."""

    def __init__(self, live=0, cap=100):
        self._live = live
        self._cap = cap
        self.calls: list[str] = []
        self._counter = 0

    def live_slot_count(self):
        return self._live

    def get_or_create_slot(self, *, name, agent, app):
        self.calls.append("create")
        self._counter += 1
        return type("Slot", (), {"key": f"chat-{self._counter}", "agent": agent})()

    def begin_slot_construction(self, key):
        self.calls.append(f"begin:{key}")

    def finish_slot_construction(self, key):
        self.calls.append(f"finish:{key}")

    def publish(self, key):
        self.calls.append(f"publish:{key}")


def _bundle():
    return {
        "messages": [{"role": "user", "content": "hi"}],
        "agent": "kirocrew",
        "title": "My session",
    }


def test_import_core_creates_a_slot_and_returns_its_id():
    st = FakeState()
    new_id = import_session_core(st, _bundle(), resolve_agent=lambda h: h)
    assert new_id == "chat-1"
    assert "create" in st.calls


def test_import_core_finishes_construction_after_beginning_it():
    st = FakeState()
    import_session_core(st, _bundle(), resolve_agent=lambda h: h)
    begin = next(i for i, c in enumerate(st.calls) if c.startswith("begin:"))
    finish = next(i for i, c in enumerate(st.calls) if c.startswith("finish:"))
    assert begin < finish  # construction bracket ordered correctly


def test_import_core_refuses_when_slot_cap_reached():
    st = FakeState(live=100, cap=100)
    with pytest.raises(ValueError):
        import_session_core(st, _bundle(), resolve_agent=lambda h: h, cap=100)
    assert "create" not in st.calls  # nothing allocated when refused


def test_import_core_resolves_agent_hint_via_injected_resolver():
    st = FakeState()
    seen = {}

    def resolver(hint):
        seen["hint"] = hint
        return "resolved-" + hint

    import_session_core(st, _bundle(), resolve_agent=resolver)
    assert seen["hint"] == "kirocrew"


def test_import_core_empty_agent_hint_skips_resolution():
    st = FakeState()
    b = {**_bundle(), "agent": ""}
    called = {"n": 0}

    def resolver(hint):
        called["n"] += 1
        return hint

    import_session_core(st, b, resolve_agent=resolver)
    assert called["n"] == 0  # no resolution when hint is empty
