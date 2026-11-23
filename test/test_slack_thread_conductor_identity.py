"""A Conductor thread's identity and dispatch (thread-conductor RFC, part A).

A Conductor thread is one thread of an ordinary Slack channel or DM bound to a
dashboard session, recorded in the code-channel store's ``thread_bindings``. The
code-channel dispatch exemption judges it with the same clauses as a code channel,
on its own switch (``slack.thread_conductor``, which cannot turn on before the
thread runtime exists) and with its room admitted: the owner's DM, a private
channel, or a public one only by opt-in. These tests pin both directions, with
code channels OFF (no code-channel scope) throughout.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from test_session_control_owner_dm import _key, _state

from kiro_crew.config import live as live_config_mod
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.code_channels import audience
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import shared_refusal
from kiro_crew.slack.code_channels import surface as cc_surface
from kiro_crew.slack.code_channels.models import ChannelInfo
from kiro_crew.slack.code_channels.store import (
    ROOM_IM,
    ROOM_PRIVATE,
    ROOM_PUBLIC,
    THREAD_ENDED_TTL_SECS,
    Binding,
    CodeChannelStore,
    StoreWriteError,
    set_store,
    thread_key,
)

OWNER = "U0OWNER0001"
STRANGER = "U0STRANGER1"
SHARED = "U0SHARED001"
TEAM = "T0TEAM00001"
PRIVATE_CHANNEL = "G0PRIVATE01"
PUBLIC_CHANNEL = "C0PUBLIC001"
DM = "D0OWNERDM01"
ANCHOR = "1700000000.000100"
FOLDER = "fold-thread"
#: Read at import, before the autouse fixture patches it on.
_SHIPPED_RUNTIME_BUILT = cc_binding.THREAD_CONDUCTOR_RUNTIME_BUILT


def _cfg(*, on: bool = True, public: bool = False) -> Any:
    return SimpleNamespace(
        slack=SimpleNamespace(
            thread_conductor=on,
            thread_conductor_public_channels=public,
            code_channel_dispatch=False,
        )
    )


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch, tmp_path):
    """Session control on, code channels OFF, the thread runtime built and switched on."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(cc_binding, "code_channels_on", lambda cfg: False)
    _floor_monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", True)
    _floor_monkeypatch.setattr(cce, "_live_config", lambda: _cfg())
    _floor_monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: team == TEAM)
    _floor_monkeypatch.setattr(slack_handler, "is_owner", lambda user: user == OWNER)
    create_rate_limit.reset_for_tests()
    audience.clear()
    cce.forget_senders()
    live_config_mod._clear_torn_files()
    yield
    set_store(None)
    audience.clear()
    cce.forget_senders()
    live_config_mod._clear_torn_files()
    create_rate_limit.reset_for_tests()


def _private_reading(channel: str, *, private: bool = True) -> None:
    audience.note_channel_info(
        ChannelInfo(
            channel_id=channel,
            is_code_channel=False,
            is_private=private,
            shared=False,
            archived=False,
        )
    )


def _thread(state, tmp_path, *, channel: str = PRIVATE_CHANNEL, room: str = ROOM_PRIVATE):
    """A Conductor thread as the start builds it: a dashboard slot filed in its own
    folder, linked to the thread's parent ts, bound in ``thread_bindings``."""
    state._folders = [{"id": FOLDER, "name": "Conductor threads", "parent_id": ""}]
    slot = state.get_or_create_slot("chat-thread-conductor")
    slot.folder_id = FOLDER
    state.link_slack(slot.key, ANCHOR, channel)
    store = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    store.load()
    store.put_thread_binding(
        Binding(
            channel_id=channel,
            team_id=TEAM,
            session_key=slot.key,
            anchor_ts=ANCHOR,
            thread=True,
            room=room,
        )
    )
    set_store(store)
    if channel[0] in ("C", "G"):
        _private_reading(channel, private=room == ROOM_PRIVATE)
    return slot, store


def _owner_turn(slot) -> None:
    assert cce.note_thread_conductor_start(slot, OWNER) is True
    slot._turn_channel_origin = True


def _create(state, slot) -> dict[str, Any]:
    return asyncio.run(sc.create_session(state, caller_session_key=_key(slot)))


def _refused(state, slot) -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as exc:
        _create(state, slot)
    return exc.value


# ── admitted ──────────────────────────────────────────────────────────────


def test_an_owner_started_private_thread_creates_a_worker_beside_it(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, store = _thread(state, tmp_path)
    _owner_turn(slot)

    worker = state.get_slot(_create(state, slot)["target"])
    assert worker._created_by == slot.key
    assert worker.folder_id == FOLDER
    # The worker is recorded as the thread's, so it stays one level deep.
    assert store.worker_conductor(worker.key) == slot.key
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(worker)))
    assert exc.value.code == "code_channel_worker_caller"


def test_a_worker_never_starts_above_the_threads_approval_mode(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    _owner_turn(slot)
    assert not slot._trust and not slot._trust_reads  # the thread starts on Ask
    worker = state.get_slot(_create(state, slot)["target"])
    assert not worker._trust and not worker._trust_reads


def test_the_owners_dm_is_admitted_without_a_share_reading(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path, channel=DM, room=ROOM_IM)
    _owner_turn(slot)
    assert _create(state, slot)["target"]


def test_a_public_channel_is_admitted_only_with_the_opt_in(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path, channel=PUBLIC_CHANNEL, room=ROOM_PUBLIC)
    _owner_turn(slot)
    exc = _refused(state, slot)
    assert "public channels are off" in exc.message

    monkeypatch.setattr(cce, "_live_config", lambda: _cfg(public=True))
    assert _create(state, slot)["target"]


# ── refused ───────────────────────────────────────────────────────────────


def test_the_switch_off_refuses(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    _owner_turn(slot)
    monkeypatch.setattr(cce, "_live_config", lambda: _cfg(on=False))
    exc = _refused(state, slot)
    assert exc.code == "mirrored_caller"
    assert cce.THREAD_SWITCH_OFF_REFUSAL in exc.message


def test_the_switch_cannot_turn_on_before_the_thread_runtime_is_built(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    _owner_turn(slot)
    monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", False)
    assert cc_binding.thread_conductor_on(_cfg(on=True)) is False
    assert cce.THREAD_SWITCH_OFF_REFUSAL in _refused(state, slot).message


def test_shipped_default_keeps_the_switch_off():
    """The value this build ships (read before any fixture patched it) keeps the
    feature off until the thread runtime (part D) flips it."""
    assert _SHIPPED_RUNTIME_BUILT is False


def test_a_turn_another_channel_member_drove_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    _owner_turn(slot)
    cce.note_channel_sender(slot, STRANGER)
    assert "someone other than the owner" in _refused(state, slot).message


def test_a_shared_user_driven_turn_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    _owner_turn(slot)
    cce.note_channel_sender(slot, SHARED)
    assert "someone other than the owner" in _refused(state, slot).message


def test_a_start_by_anyone_but_the_owner_records_nothing(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    assert cce.note_thread_conductor_start(slot, STRANGER) is False
    slot._turn_channel_origin = True
    assert "not on record" in _refused(state, slot).message


def test_a_private_channel_made_public_stops_dispatching(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    _owner_turn(slot)
    _private_reading(PRIVATE_CHANNEL, private=False)
    assert "public channels are off" in _refused(state, slot).message


def test_an_unknown_privacy_reading_refuses(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    _owner_turn(slot)
    audience.forget(PRIVATE_CHANNEL)
    monkeypatch.setattr(audience, "refresh_soon", lambda channel: False)
    assert cce.THREAD_PRIVACY_UNREAD_REFUSAL in _refused(state, slot).message


def test_a_room_recorded_as_a_dm_must_be_a_dm(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path, channel=PUBLIC_CHANNEL, room=ROOM_IM)
    _owner_turn(slot)
    assert "is not a DM" in _refused(state, slot).message


def test_an_ended_thread_refuses(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, store = _thread(state, tmp_path)
    _owner_turn(slot)
    store.end_thread_binding(PRIVATE_CHANNEL, ANCHOR)
    assert cce.THREAD_ENDED_REFUSAL in _refused(state, slot).message


def test_a_slot_the_thread_binding_does_not_name_is_not_admitted(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _thread(state, tmp_path)
    other = state.get_or_create_slot("chat-other")
    other.folder_id = FOLDER
    state.link_slack(other.key, "1700000000.000999", PRIVATE_CHANNEL)
    cce.note_thread_conductor_start(other, OWNER)
    other._turn_channel_origin = True
    assert _refused(state, other).code == "mirrored_caller"


# ── shared users ──────────────────────────────────────────────────────────


def test_shared_users_are_refused_in_a_conductor_thread_only(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, store = _thread(state, tmp_path, channel=DM, room=ROOM_IM)
    assert shared_refusal.shared_user_refused_in(DM, ANCHOR) is True
    assert shared_refusal.shared_user_refused_in(DM, "1700000000.000999") is False
    assert shared_refusal.shared_user_refused_for_slot(slot.key, "") is True
    assert shared_refusal.shared_user_refused_for_slot("chat-unrelated", ANCHOR) is True
    assert shared_refusal.shared_user_refused_for_slot("chat-unrelated", "1.2") is False
    # Ended and then pruned: the thread stays closed to them.
    store.end_thread_binding(DM, ANCHOR, at=1.0)
    assert store.prune_ended_threads(now=1.0 + THREAD_ENDED_TTL_SECS) == 1
    assert shared_refusal.shared_user_refused_in(DM, ANCHOR) is True


# ── the store ─────────────────────────────────────────────────────────────


def _tb(channel: str = PRIVATE_CHANNEL, anchor: str = ANCHOR, key: str = "chat-1") -> Binding:
    return Binding(
        channel_id=channel,
        team_id=TEAM,
        session_key=key,
        anchor_ts=anchor,
        thread=True,
        room=ROOM_PRIVATE,
    )


def test_thread_bindings_round_trip_and_stay_out_of_channel_bindings(tmp_path):
    path = tmp_path / "s.json"
    store = CodeChannelStore(path=path)
    store.put_thread_binding(_tb(key="chat-1"))
    store.put_thread_binding(_tb(anchor="1700000000.000200", key="chat-2"))
    assert store.bindings() == [] and store.get_binding(PRIVATE_CHANNEL) is None

    reread = CodeChannelStore(path=path)
    reread.load()
    got = reread.get_thread_binding(PRIVATE_CHANNEL, ANCHOR)
    assert got is not None and got.team_id == TEAM and got.room == ROOM_PRIVATE
    assert len(reread.thread_bindings()) == 2
    assert {"chat-1", "chat-2"} <= reread.session_keys()
    assert json.loads(path.read_text())["thread_bindings"].keys() == {
        thread_key(PRIVATE_CHANNEL, ANCHOR),
        thread_key(PRIVATE_CHANNEL, "1700000000.000200"),
    }


def test_a_file_without_thread_bindings_loads_unchanged(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"bindings": {}, "retired_session_keys": []}), encoding="utf-8")
    store = CodeChannelStore(path=path)
    store.load()
    assert store.thread_bindings() == [] and not store.unreadable


def test_a_thread_is_bound_once(tmp_path):
    store = CodeChannelStore(path=tmp_path / "s.json")
    first = store.put_thread_binding(_tb(key="chat-1"))
    again = store.put_thread_binding(_tb(key="chat-2"))
    assert again == first and again.session_key == "chat-1"


@pytest.mark.parametrize(
    "fields",
    [
        {"thread": False},
        {"team_id": ""},
        {"room": "mpim"},
        {"session_key": ""},
        {"ended_at": 5.0},
    ],
)
def test_an_incomplete_thread_binding_is_refused(tmp_path, fields):
    store = CodeChannelStore(path=tmp_path / "s.json")
    with pytest.raises(ValueError):
        store.put_thread_binding(Binding(**{**_tb().__dict__, **fields}))


def test_a_code_channels_anchor_cannot_become_a_conductor_thread(tmp_path):
    store = CodeChannelStore(path=tmp_path / "s.json")
    store.put_binding(
        Binding(channel_id="C0CODE00001", team_id=TEAM, session_key="chat-c", anchor_ts=ANCHOR)
    )
    with pytest.raises(StoreWriteError):
        store.put_thread_binding(_tb())


def test_a_malformed_thread_row_fences_its_thread(tmp_path):
    path = tmp_path / "s.json"
    key = thread_key(PRIVATE_CHANNEL, ANCHOR)
    path.write_text(
        json.dumps({"thread_bindings": {key: {"session_key": "chat-bad", "thread": "yes"}}}),
        encoding="utf-8",
    )
    store = CodeChannelStore(path=path)
    store.load()
    assert store.thread_bindings() == []
    assert store.is_thread_quarantined(PRIVATE_CHANNEL, ANCHOR)
    assert "chat-bad" in store.session_keys()
    with pytest.raises(StoreWriteError):
        store.put_thread_binding(_tb())
    # Written back unchanged by any later write.
    store.record_worker("chat-w", "chat-x")
    assert json.loads(path.read_text())["thread_bindings"][key]["thread"] == "yes"


def test_an_ended_thread_moves_to_revoked_and_its_session_is_retired(tmp_path):
    store = CodeChannelStore(path=tmp_path / "s.json")
    store.put_thread_binding(_tb(key="chat-1"))
    store.end_thread_binding(PRIVATE_CHANNEL, ANCHOR, at=100.0)
    assert store.prune_ended_threads(now=100.0 + THREAD_ENDED_TTL_SECS - 1) == 0
    assert store.prune_ended_threads(now=100.0 + THREAD_ENDED_TTL_SECS) == 1
    assert store.get_thread_binding(PRIVATE_CHANNEL, ANCHOR) is None
    assert store.revoked_anchor(ANCHOR)
    assert "chat-1" in store.session_keys()  # its workers stay one level deep
    with pytest.raises(StoreWriteError):
        store.put_thread_binding(_tb(key="chat-9"))  # never a Conductor thread again


def test_a_live_thread_is_never_pruned(tmp_path):
    store = CodeChannelStore(path=tmp_path / "s.json")
    store.put_thread_binding(_tb())
    assert store.prune_ended_threads(now=10**12) == 0


def test_the_anchor_guard_covers_conductor_threads(tmp_path):
    store = CodeChannelStore(path=tmp_path / "s.json")
    store.load()
    store.put_thread_binding(_tb(key="chat-1"))
    set_store(store)
    assert cc_binding.anchor_owner_by_ts(ANCHOR) == "chat-1"
    cc_binding.check_anchor_handoff(ANCHOR, "chat-1")
    with pytest.raises(cc_binding.AnchorHandoffRefused):
        cc_binding.check_anchor_handoff(ANCHOR, "chat-2")
