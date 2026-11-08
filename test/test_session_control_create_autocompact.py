"""Session control create: the new session's own ``autocompact_pct``.

Covers ``sc.create_session(autocompact_pct=...)`` and the create route that
forwards it: an accepted value lands on the slot, in the birth metadata and in
the live gate; it beats the agent's own default; an omitted value changes
nothing; a bad value (out of range, infinite, NaN, an int too large for a
float, a bool, a string) allocates nothing, dry run included. The gate is
seeded after the birth write, under the slider route's per-transcript lock, and
never for a failed write or a slot closed mid-create. A create cancelled after
its birth write landed still seeds the gate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import test_session_control_routes as routes
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import AutocompactPctError, parse_autocompact_pct
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.session import SessionManager, _Session

# ── the shared validator ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5.0), (5.0, 5.0), (70, 70.0), (89.9, 89.9), (90, 90.0)],
    ids=["min-int", "min-float", "mid", "just-below-max", "max"],
)
def test_parse_autocompact_pct_takes_the_whole_range(value, expected):
    assert parse_autocompact_pct(value) == expected


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (4.99, "pct_out_of_range"),
        (90.01, "pct_out_of_range"),
        (0, "pct_out_of_range"),
        (-10, "pct_out_of_range"),
        (float("inf"), "pct_out_of_range"),
        (float("-inf"), "pct_out_of_range"),
        (float("nan"), "pct_not_finite"),
        (10**400, "pct_not_finite"),
    ],
    ids=["below-min", "above-max", "zero", "negative", "inf", "-inf", "nan", "int-overflow"],
)
def test_parse_autocompact_pct_refuses_with_the_slot_routes_codes(value, code):
    with pytest.raises(AutocompactPctError) as exc:
        parse_autocompact_pct(value, name="autocompact_pct")
    assert exc.value.code == code
    assert str(exc.value).startswith("autocompact_pct must be")


# ── create_session ───────────────────────────────────────────────────


def test_create_sets_the_requested_autocompact_pct_at_birth(tmp_path):
    """A threshold lands on the slot, in the live gate AND in the birth metadata.

    The slot field is what every later save writes, the session manager's map is
    what the compaction gate reads, and the birth line is the only durable record
    an idle newborn has, so restore can re-seed the gate after a restart.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=85)
    )

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.autocompact_pct == 85.0
    state.sessions.set_autocompact_pct.assert_called_once_with(effective_session_key(child), 85.0)
    assert created["autocompact_pct"] == 85.0
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("autocompact_pct") == 85.0
    assert caller.autocompact_pct is None, "the creator's own threshold must not move"


def test_a_created_sessions_threshold_wins_over_its_agents_default(tmp_path):
    """Two workers of one agent compact at different numbers.

    The create-time threshold lands in the same per-session override the
    context slider sets, which the gate reads ABOVE the agent default. Driven
    against a real session manager, so a threshold that never reached the
    override would leave the child at the agent's 40.
    """
    cfg = KiroCrewConfig()
    cfg.agents = {"orch": KiroCrewAgentConfig(kiro_agent="kirocrew", autocompact_pct=40.0)}
    manager = SessionManager(cfg, provider_factory=lambda *a, **k: object())
    state = _make_state(tmp_path)
    state.sessions.set_autocompact_pct = manager.set_autocompact_pct
    caller = _slot(state, "chat-1")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=85)
    )
    child = state.get_slot(created["target"])
    assert child is not None
    child_key = effective_session_key(child)

    for key in (child_key, "chat-sibling"):
        live = _Session(provider=MagicMock(), agent="kirocrew")
        live.capability_member = "orch"
        manager._sessions[manager._fold_key(key)] = live

    assert manager.effective_autocompact_pct(child_key) == 85.0
    assert manager.effective_autocompact_pct("chat-sibling") == 40.0


def test_create_without_an_autocompact_pct_changes_nothing(tmp_path):
    """Omitting the threshold: no override, no gate call, no key anywhere."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.autocompact_pct is None
    state.sessions.set_autocompact_pct.assert_not_called()
    assert "autocompact_pct" not in created
    assert "autocompact_pct" not in state.conversation_log.get_metadata(slot_history_key(child))


@pytest.mark.parametrize(
    ("bad_pct", "code"),
    [
        (4.9, "pct_out_of_range"),
        (90.5, "pct_out_of_range"),
        (float("inf"), "pct_out_of_range"),
        (float("nan"), "pct_not_finite"),
        (10**400, "pct_not_finite"),
        (True, "invalid_field_type"),
        ("85", "invalid_field_type"),
    ],
    ids=["below-min", "above-max", "inf", "nan", "int-overflow", "bool", "string"],
)
def test_create_refuses_a_bad_autocompact_pct_and_allocates_nothing(tmp_path, bad_pct, code):
    """The HTTP route runs no schema, so create checks the value itself."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=bad_pct)
        )

    assert exc.value.code == code
    assert state.live_slot_count() == before, "a refused create must not leave a slot behind"
    state.sessions.set_autocompact_pct.assert_not_called()


def test_a_dry_run_refuses_a_bad_autocompact_pct_and_seeds_nothing(tmp_path):
    """The MCP folder preflight is a dry run, so the check must precede it."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(
                state, caller_session_key=_key(caller), autocompact_pct=95, dry_run=True
            )
        )
    assert exc.value.code == "pct_out_of_range"

    preview = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=85, dry_run=True)
    )
    assert preview == {"dry_run": True}
    assert state.live_slot_count() == before
    state.sessions.set_autocompact_pct.assert_not_called()


def test_the_birth_write_and_gate_seed_run_under_the_slider_lock(tmp_path, monkeypatch):
    """A slider POST on the newborn must wait until record and gate hold the birth value.

    Without the lock, a slider commit landing during the birth write would be
    overwritten in the live gate by the birth value seeded after it.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    seen: list[tuple[str, bool]] = []
    real_update = state.conversation_log.update_metadata

    def _held(key):
        lock = chat_handlers._autocompact_txn_locks.get(key)
        return lock is not None and lock.locked()

    def record_birth(key, metadata):
        if "autocompact_pct" in metadata:
            seen.append(("birth write", _held(key)))
        real_update(key, metadata)

    def record_seed(session_key, pct):
        child = next(s for s in state._slots.values() if effective_session_key(s) == session_key)
        seen.append(("gate seed", _held(slot_history_key(child))))

    monkeypatch.setattr(state.conversation_log, "update_metadata", record_birth)
    state.sessions.set_autocompact_pct.side_effect = record_seed

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=85)
    )

    assert seen == [("birth write", True), ("gate seed", True)]
    child = state.get_slot(created["target"])
    assert not _held(slot_history_key(child)), "the lock must be released after the create"


def test_a_failed_birth_write_leaves_no_autocompact_gate_entry(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def fail_history(key, metadata):
        raise OSError("birth metadata could not be written")

    monkeypatch.setattr(state.conversation_log, "update_metadata", fail_history)
    with pytest.raises(OSError, match="birth metadata"):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=85))

    assert set(state._slots) == before, "the unpersisted newborn must be retracted"
    state.sessions.set_autocompact_pct.assert_not_called()


def test_a_slot_closed_mid_create_takes_no_autocompact_gate_entry(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    real_update = state.conversation_log.update_metadata

    def write_then_close(key, metadata):
        real_update(key, metadata)
        for held in [s for s in state._slots.values() if slot_history_key(s) == key]:
            state._slots.pop(held.key)

    monkeypatch.setattr(state.conversation_log, "update_metadata", write_then_close)

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=85)
    )

    state.sessions.set_autocompact_pct.assert_not_called()
    assert state.get_slot(created["target"]) is None, "the slot closed before the gate seed"


def test_a_create_cancelled_after_its_birth_write_still_seeds_the_gate(tmp_path, monkeypatch):
    """The kept slot's record says 85, so the live gate must say 85 too.

    A client disconnect during the birth write cancels the create after the
    record landed. The slot stays, so a gate left unseeded would compact at the
    default while the meter shows 85.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    real_update = state.conversation_log.update_metadata
    running: dict[str, asyncio.Task] = {}

    def write_then_cancel(key, metadata):
        if "autocompact_pct" in metadata:
            # Queued on the loop BEFORE this worker returns, so the loop runs the
            # cancel ahead of the thread's completion callback: the create is
            # cancelled while it awaits the write, and the write still lands.
            task = running["create"]
            task.get_loop().call_soon_threadsafe(task.cancel)
        real_update(key, metadata)

    monkeypatch.setattr(state.conversation_log, "update_metadata", write_then_cancel)

    async def scenario():
        running["create"] = asyncio.create_task(
            sc.create_session(state, caller_session_key=_key(caller), autocompact_pct=85)
        )
        with pytest.raises(asyncio.CancelledError):
            await running["create"]

    asyncio.run(scenario())

    kept = [s for s in state._slots.values() if s.autocompact_pct == 85.0]
    assert len(kept) == 1, "a slot whose birth record landed is kept"
    state.sessions.set_autocompact_pct.assert_called_once_with(effective_session_key(kept[0]), 85.0)


# ── the create route ─────────────────────────────────────────────────


class TestTheCreateRouteForwardsTheThreshold:
    _route = routes.TestTheRoutesRequireTheInternalSecret()

    def _post(self, tmp_path, body):
        req = self._route._request(tmp_path, internal=True, path="/api/session-control/create")

        async def _json():
            return body

        req.json = _json
        return req, asyncio.run(handlers_sc.api_session_control_create(req))

    def test_create_route_forwards_the_autocompact_pct(self, tmp_path):
        """A route dropping autocompact_pct sets nothing while handler tests stay green."""
        req, resp = self._post(tmp_path, {"title": "worker", "autocompact_pct": 85})

        assert resp.status == 200
        payload = self._route._body(resp)
        assert payload["autocompact_pct"] == 85.0
        created = req.app["state"].get_slot(payload["target"])
        assert created is not None
        assert created.autocompact_pct == 85.0

    @pytest.mark.parametrize("bad_pct", [True, "85", [85]], ids=["bool", "string", "list"])
    def test_create_route_refuses_a_non_number_autocompact_pct(self, tmp_path, bad_pct):
        """bool is an int subclass, so True must be refused, not read as a threshold of 1."""
        req, resp = self._post(tmp_path, {"title": "worker", "autocompact_pct": bad_pct})

        assert resp.status == 400
        assert self._route._body(resp)["code"] == "invalid_field_type"
        assert req.app["state"].live_slot_count() == 2, "only the two fixture slots remain"

    def test_create_route_refuses_an_out_of_range_autocompact_pct(self, tmp_path):
        req, resp = self._post(tmp_path, {"title": "worker", "autocompact_pct": 95})

        assert resp.status == 400
        assert self._route._body(resp)["code"] == "pct_out_of_range"
