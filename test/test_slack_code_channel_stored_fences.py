"""A code-channel store field that does not read back never lifts a fence.

Every lineage or ownership fence the code-channel dispatch exemption relies on
reads ``slack-code-channels.json``. A row whose fields are missing, malformed or
of the wrong type fails closed: the store is unavailable, or the row is kept and
fences its own channel, and a session a code channel ran in stays fenced. These
tests write such rows and read the fences back.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from test_session_control_owner_dm import _state
from test_slack_code_channel_dispatch_exemption import (  # noqa: F401 - _enabled is autouse
    ANCHOR,
    CHANNEL,
    TEAM,
    _enabled,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.slack.code_channels.store import Binding, CodeChannelStore, set_store


def test_a_conductor_whose_row_is_quarantined_stays_ownership_fenced(tmp_path, monkeypatch):
    """Review: a code channel's session whose binding row does not read back (a
    field of the wrong type) has no live binding, no ``created_by`` and, after a
    restart, no channel link -- it must still not read as a person's own tab."""
    state = _state(tmp_path, monkeypatch)
    conductor = state.get_or_create_slot("chat-quarantined-conductor")
    conductor._created_by = ""
    state.get_or_create_slot("chat-the-persons-own-tab")
    row = {
        "channel_id": CHANNEL,
        "team_id": TEAM,
        "session_key": conductor.key,
        "anchor_ts": ANCHOR,
        "created_at": "x",
    }
    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps({"bindings": {CHANNEL: row}}), encoding="utf-8")
    loaded = CodeChannelStore(path=path)
    loaded.load()
    set_store(loaded)
    assert loaded.is_quarantined(CHANNEL) and loaded.unreadable is False
    assert sc.is_code_channel_slot(state, conductor) is False
    assert sc._caller_is_ownership_fenced(state, conductor.key) is True
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state,
            caller_session_key=slot_history_key(conductor),
            target="chat-the-persons-own-tab",
            operation="send",
        )
    assert exc.value.code == "not_creator"


def test_a_retired_conductor_stays_ownership_fenced(tmp_path, monkeypatch):
    """A channel Slack unbound keeps its session's key retired: that session is
    still not a person's own tab once the channel link is gone."""
    state = _state(tmp_path, monkeypatch)
    conductor = state.get_or_create_slot("chat-retired-conductor")
    conductor._created_by = ""
    store = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    store.load()
    store.put_binding(
        Binding(channel_id=CHANNEL, team_id=TEAM, session_key=conductor.key, anchor_ts=ANCHOR)
    )
    set_store(store)
    assert store.drop_binding(CHANNEL) is True
    assert sc.is_code_channel_slot(state, conductor) is False
    assert sc._caller_is_ownership_fenced(state, conductor.key) is True
    tab = state.get_or_create_slot("chat-unrelated-tab")
    assert sc._caller_is_ownership_fenced(state, tab.key) is False
    # A live slot answering to the retired key (its conductor's tab closed) is that
    # session to every caller-key lookup, so it is fenced too (review).
    state._slots.pop(conductor.key)
    stand_in = state.get_or_create_slot("chat-stand-in")
    stand_in.linked_session_key = conductor.key
    assert conductor.key in {slot_history_key(stand_in), stand_in.key}
    assert sc._caller_is_ownership_fenced(state, stand_in.key) is True


def test_every_stored_record_field_has_a_type_the_load_check_reads():
    """``_well_typed`` fails any field whose annotation it does not know, which would
    quarantine every row of that table: a new field must use a type it reads."""
    from kiro_crew.slack.code_channels.store import Revoked, Route

    for record in (Route, Binding, Revoked):
        for f in dataclasses.fields(record):
            assert f.type in ("str", "bool", "int", "float"), (record.__name__, f.name, f.type)


@pytest.mark.parametrize(
    "bad_route",
    [
        {"created_at": "not a number"},
        {"created_at": None},
        {"created_at": float("nan")},
        {"created_at": float("inf")},
        {"seeded_at": "x"},
        {"decision": 7},
        {"origin_ts": ["a"]},
    ],
)
def test_a_malformed_route_field_never_lifts_a_workers_lineage_fence(
    tmp_path, monkeypatch, bad_route
):
    """GPT: a route whose ``created_at`` is not a number passed the parse, then raised
    while the routes were bounded, OUTSIDE the guarded load, leaving the store
    unloaded and the worker read off its (edited-away) transcript lineage. A
    malformed route is dropped inside the guard; the worker stays fenced."""
    state = _state(tmp_path, monkeypatch)
    worker = state.get_or_create_slot("chat-route-worker")
    worker._created_by = ""
    route = {
        "session_id": "sid-1",
        "origin_channel_id": "C0ORIGIN01",
        "origin_ts": "1.1",
        "created_at": 1.0,
        **bad_route,
    }
    path = tmp_path / "slack-code-channels.json"
    path.write_text(
        json.dumps({"routes": {"sid-1": route}, "workers": {worker.key: "chat-the-conductor"}}),
        encoding="utf-8",
    )
    loaded = CodeChannelStore(path=path)
    loaded.load()
    set_store(loaded)
    assert loaded.loaded is True and loaded.unreadable is False
    assert loaded.get_route("sid-1") is None
    assert sc.code_channel_root(state, worker) == "chat-the-conductor"
    assert sc._caller_is_ownership_fenced(state, worker.key) is True


def test_a_load_step_that_raises_marks_the_store_unavailable(tmp_path, monkeypatch):
    """Whatever breaks while the tables are normalised, the store ends loaded and
    unreadable, so every lineage reader refuses rather than vouching for nobody."""
    from kiro_crew.slack.code_channels import store as store_mod

    state = _state(tmp_path, monkeypatch)
    worker = state.get_or_create_slot("chat-other-worker")
    worker._created_by = ""
    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps({"workers": {worker.key: "chat-c"}}), encoding="utf-8")

    def _boom(*_a, **_k):
        raise TypeError("normalisation failed")

    monkeypatch.setattr(store_mod, "_bounded_routes", _boom)
    loaded = CodeChannelStore(path=path)
    loaded.load()
    set_store(loaded)
    assert loaded.loaded is True and loaded.unreadable is True
    assert sc.code_channel_root(state, worker) == sc.LINEAGE_UNAVAILABLE
    assert sc._caller_is_ownership_fenced(state, worker.key) is True


@pytest.mark.parametrize(
    "raw", [{"at": "yesterday"}, {"at": True}, {"at": 1.0, "anchor_ts": 5}, "revoked", None]
)
def test_a_malformed_revoked_row_fences_its_channel_and_is_kept(tmp_path, raw):
    """A revocation is a fence (old controls and the old anchor thread stay closed).
    A row that does not read back fences the channel rather than lifting it, and
    every write carries it back unchanged."""
    from kiro_crew.slack.code_channels.store import StoreWriteError

    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps({"revoked": {"C0WASREVOK": raw}}), encoding="utf-8")
    loaded = CodeChannelStore(path=path)
    loaded.load()
    assert loaded.unreadable is False
    assert loaded.is_quarantined("C0WASREVOK") is True
    with pytest.raises(StoreWriteError):
        loaded.put_binding(
            Binding(channel_id="C0WASREVOK", team_id=TEAM, session_key="chat-x", anchor_ts="1.1")
        )
    other = Binding(channel_id="C0FRESHONE", team_id=TEAM, session_key="chat-y", anchor_ts="2.2")
    loaded.put_binding(other)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["revoked"]["C0WASREVOK"] == raw
    assert "C0WASREVOK" not in written["bindings"]


@pytest.mark.parametrize("table", [[], False, 0, "", ["C0WASREVOK"]])
def test_a_revoked_table_that_is_not_an_object_never_reads_as_none_revoked(tmp_path, table):
    """Only an absent or ``null`` table is "none revoked" (review)."""
    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps({"revoked": table}), encoding="utf-8")
    loaded = CodeChannelStore(path=path)
    loaded.load()
    assert loaded.unreadable is True
