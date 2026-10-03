"""The owner's per-chat "Trust this session" survives a gateway restart.

Before this, the grant lived only in gateway memory, so a crash-restart silently
took trust away from every chat and unattended work in them stalled on approval
prompts nobody was there to answer. These tests pin:

* the owner's click persists and a fresh gateway restores it after a crash (the
  headline), while an owner stop clears it so a deliberate restart still asks
  again;
* ONLY the owner's own dashboard session makes it durable -- an app token and
  the agent's internal credential do not, and an app-owned chat never does;
* every revoke path removes it (normal, trust_reads, the all-chats off switch),
  including a revoke that races an in-flight grant write;
* an unreadable store restores nothing and tells the owner, and a revoke whose
  saved copy survived is not handed back by a restore in the same process;
* the record sits in a leaf the sandbox seals read-only and the file-edit gate
  write-protects, so the agent cannot grant itself trust by writing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_trust_persistence
from kiro_crew.dashboard import session_trust_store as store
from kiro_crew.dashboard.chat_handlers import api_chat_mode, api_chat_slot_approve
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.safety_override import reset_singleton

#: Captured before the autouse fixture stubs it, for the tests of the real key path.
_REAL_SIGNING_ROOT = store._signing_root


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(store, "_path_override", tmp_path / "session-trust" / "grants.json")
    monkeypatch.setattr(store, "_signing_root", lambda: b"k" * 32)
    monkeypatch.setattr(chat_trust_persistence, "_owner_stopped", False)
    monkeypatch.setattr(chat_trust_persistence, "_withdrawn_keys", set())
    monkeypatch.setattr(chat_trust_persistence, "_withdrawn_all", False)
    monkeypatch.setattr(chat_trust_persistence, "_stopping", False)
    # Markers a test writes stand for a previous run's stop unless it says otherwise.
    monkeypatch.setattr(chat_trust_persistence, "_STARTED", float("inf"))
    chat_trust_persistence._stop_marked.clear()
    reset_singleton()
    yield
    # Before monkeypatch unwinds the path overrides: a clear worker still running
    # would otherwise act on the real data home.
    _join_trust_workers()
    reset_singleton()


def _join_trust_workers() -> None:
    import threading

    for worker in threading.enumerate():
        if worker.name in ("session-trust-force-clear", "session-trust-signal-clear"):
            worker.join(5.0)
            assert not worker.is_alive(), worker.name


def _grant(keys) -> bool:
    """Remember *keys* the way the one production writer does."""
    return store.grant_counting(keys)[0]


def _state(tmp_path):
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.notify = MagicMock()
    return st


def _app(state, identity: dict) -> web.Application:
    @web.middleware
    async def _auth(request: web.Request, handler):
        for k, v in identity.items():
            request[k] = v
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


OWNER = {"app": "", "user": "local-app"}
APP_TOKEN = {"app": "crew-keyboard", "user": ""}
INTERNAL = {"app": "", "user": "local-app", "internal_auth": True}


async def _mode(state, identity, body):
    async with TestClient(TestServer(_app(state, identity))) as client:
        resp = await client.post("/api/chat/mode", json=body)
        assert resp.status == 200, await resp.text()


async def _restart(tmp_path, *slot_keys, app: str = ""):
    """A fresh gateway: new state, the same chats rebuilt, then the boot restore."""
    fresh = _state(tmp_path)
    slots = [
        fresh.get_or_create_slot(k, app=app) if app else fresh.get_or_create_slot(k)
        for k in slot_keys
    ]
    restored = await chat_trust_persistence.restore_persisted_trust_async(fresh)
    return fresh, slots, restored


# ── the headline ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_trust_survives_a_restart(tmp_path):
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    fresh, (slot,), restored = await _restart(tmp_path, "s1")

    assert restored == 1
    assert slot._trust is True
    fresh.sessions.set_approval_policy.assert_any_call(effective_session_key(slot), "auto")
    fresh.notify.assert_not_called()


@pytest.mark.asyncio
async def test_an_untrusted_chat_stays_untrusted_after_restart(tmp_path):
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    state.get_or_create_slot("s2", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    _, (s1, s2), restored = await _restart(tmp_path, "s1", "s2")

    assert restored == 1
    assert s1._trust is True
    assert s2._trust is False


@pytest.mark.asyncio
async def test_approval_card_trust_survives_a_restart(tmp_path):
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    async with TestClient(TestServer(_app(state, OWNER))) as client:
        resp = await client.post(
            "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
        )
        assert resp.status == 200
    assert fut.result() == "approved"

    _, (restored_slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 1
    assert restored_slot._trust is True


@pytest.mark.asyncio
async def test_approval_card_trust_that_cannot_be_saved_goes_live_only(tmp_path):
    """The call is approved and the chat is trusted until the next restart; the owner is told."""
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    with patch.object(store, "_write", side_effect=OSError("read-only")):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
            payload = await resp.json()
    assert resp.status == 200
    assert payload == {"ok": True}
    assert fut.result() == "approved"
    assert slot._trust is True
    assert store.load().sessions == ()
    titles = [c.args[1] for c in state.notify.call_args_list]
    assert "Chat trust is on until the next restart" in titles


# ── only the owner's own click is durable ─────────────────────────────────


@pytest.mark.asyncio
async def test_app_token_trust_is_not_persisted(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    with patch("kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True):
        await _mode(state, APP_TOKEN, {"mode": "trust", "slot": "s1"})
    assert slot._trust is True  # the live grant still works

    _, (restored_slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert restored_slot._trust is False


@pytest.mark.asyncio
async def test_internal_credential_trust_is_not_persisted(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, INTERNAL, {"mode": "trust", "slot": "s1"})
    assert slot._trust is True

    _, (restored_slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert restored_slot._trust is False


def test_non_owner_dashboard_login_does_not_persist():
    request = MagicMock()
    data = {"app": "", "user": "someone-else"}
    request.get = data.get
    request.__contains__ = lambda _self, k: k in data
    request.__getitem__ = lambda _self, k: data[k]
    request.app = {"state": MagicMock(owner_id="the-owner")}
    assert chat_trust_persistence.persists_grant(request) is False


@pytest.mark.asyncio
async def test_owner_trust_on_an_app_owned_chat_is_not_persisted(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", app="crew-keyboard")
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})
    assert slot._trust is True
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_restore_skips_an_app_owned_chat_even_when_its_key_is_stored(tmp_path):
    probe = _state(tmp_path).get_or_create_slot("s1")
    assert _grant([effective_session_key(probe)])

    _, (slot,), restored = await _restart(tmp_path, "s1", app="crew-keyboard")
    assert restored == 0
    assert slot._trust is False


# ── every revoke removes it ───────────────────────────────────────────────


@pytest.mark.parametrize("revoke_mode", ["normal", "trust_reads"])
@pytest.mark.asyncio
async def test_slot_revoke_removes_the_persisted_grant(tmp_path, revoke_mode):
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})
    await _mode(state, OWNER, {"mode": revoke_mode, "slot": "s1"})

    _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False


@pytest.mark.parametrize("identity", [APP_TOKEN, INTERNAL])
@pytest.mark.asyncio
async def test_a_revoke_from_any_permitted_caller_removes_it(tmp_path, identity):
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})
    with patch("kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True):
        await _mode(state, identity, {"mode": "normal", "slot": "s1"})
    assert store.load().sessions == ()


@pytest.mark.parametrize("revoke_mode", ["normal", "trust_reads"])
@pytest.mark.asyncio
async def test_all_chats_off_switch_forgets_chats_that_are_not_open(tmp_path, revoke_mode):
    assert _grant(["dashboard:closed-tab"])
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})
    await _mode(state, OWNER, {"mode": revoke_mode})
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_revoke_that_races_the_grant_write_wins(tmp_path):
    """A revoke whose loop half lands while the grant is being saved wins."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    real_grant = store.grant_counting

    def grant_then_revoked_on_the_loop(keys, **kw):
        answer = real_grant(keys, **kw)
        # The revoke's synchronous half ran while this write was in flight.
        chat_trust_persistence._bump_revoke_generation()
        return answer

    with patch.object(store, "grant_counting", side_effect=grant_then_revoked_on_the_loop):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            payload = await resp.json()

    assert resp.status == 409
    assert payload["code"] == "trust_superseded"
    assert slot._trust is False
    assert store.load().sessions == ()


# ── restore is gated, and fails closed ────────────────────────────────────


@pytest.mark.parametrize("granted", [True, False])
@pytest.mark.asyncio
async def test_a_chat_reopened_after_boot_gets_its_trust_back(tmp_path, granted):
    """The on-demand rebuild (a closed tab reopened, a loop firing into it)."""
    from kiro_crew.dashboard.chat_persistence import (
        rehydrate_slot_from_history_async,
        save_all_slots_to_history,
    )

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.append("user", "hello")
    slot._dirty = True
    save_all_slots_to_history(state)
    if granted:
        await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    fresh = _state(tmp_path)
    rebuilt = await rehydrate_slot_from_history_async(fresh, "s1")
    assert rebuilt is not None
    assert rebuilt._trust is granted


# ── restore is gated, and fails closed (boot) ─────────────────────────────


def test_restore_does_not_consult_the_approval_modes_ceiling():
    """``trust`` is non-deniable, so the restore must not pretend to re-check it."""
    import inspect

    assert "approval_mode_permitted" not in inspect.getsource(chat_trust_persistence)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"version": 2, "sessions": ["dashboard:s1"]}),
        json.dumps({"version": 1, "sessions": "dashboard:s1"}),
        json.dumps(["dashboard:s1"]),
    ],
)
@pytest.mark.asyncio
async def test_an_unreadable_store_restores_nothing_and_says_so(tmp_path, content):
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    fresh, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False
    fresh.notify.assert_called_once()


def test_restore_verdict_needs_the_record_and_no_withdrawal(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_trust_persistence, "_withdrawn_keys", set())
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is False
    assert _grant(["dashboard:s1"])
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is True
    chat_trust_persistence._withdraw(["dashboard:s1"])
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is False


# ── the store ─────────────────────────────────────────────────────────────


def test_absent_store_is_readable_and_empty():
    snap = store.load()
    assert snap.readable is True
    assert snap.sessions == ()


def test_off_shape_entries_are_dropped_and_the_rest_kept():
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    sessions = ["dashboard:a", 7, "", " pad", "x\ny", "dashboard:b"]
    doc = {"version": store._VERSION, "sessions": sessions, "mac": store._mac(sessions)}
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert store.load().sessions == ("dashboard:a", "dashboard:b")


@pytest.mark.parametrize(
    "doc",
    [
        # What an agent could have planted on a build before this store existed.
        {"version": 1, "sessions": ["dashboard:s1"]},
        # The current format with no MAC, or with one it made up.
        {"version": 2, "sessions": ["dashboard:s1"]},
        {"version": 2, "sessions": ["dashboard:s1"], "mac": "0" * 64},
    ],
)
@pytest.mark.asyncio
async def test_a_record_the_gateway_did_not_sign_restores_nothing(tmp_path, doc):
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    fresh, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False
    fresh.notify.assert_called_once()


@pytest.mark.asyncio
async def test_a_signed_record_whose_list_was_edited_restores_nothing(tmp_path):
    probe = _state(tmp_path).get_or_create_slot("s1")
    assert _grant(["dashboard:other"])
    path = store.store_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["sessions"].append(effective_session_key(probe))  # keep the old MAC
    path.write_text(json.dumps(doc), encoding="utf-8")
    _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False


def test_an_oversized_record_is_refused_before_it_is_read(monkeypatch):
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b" " * 16)
    monkeypatch.setattr(store, "_MAX_RECORD_BYTES", 8)
    snapshot = store.load()
    assert snapshot.readable is False
    assert snapshot.sessions == ()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_a_linked_record_is_refused(tmp_path):
    assert _grant(["dashboard:a"])
    real = store.store_path()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(real.read_bytes())
    real.unlink()
    real.symlink_to(elsewhere)
    snapshot = store.load()
    assert snapshot.readable is False
    assert snapshot.sessions == ()


@pytest.mark.asyncio
async def test_a_card_answered_elsewhere_during_the_save_grants_nothing(tmp_path):
    """A second click resolving the card while the grant saves wins."""
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    real_persist = chat_trust_persistence.persist_grant

    async def persist_then_rejected_elsewhere(request, slots):
        answer = await real_persist(request, slots)
        fut.set_result("rejected")
        return answer

    with patch.object(
        chat_trust_persistence, "persist_grant", side_effect=persist_then_rejected_elsewhere
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
            payload = await resp.json()
    assert resp.status == 409
    assert payload["code"] == "approval_already_resolved"
    assert fut.result() == "rejected"
    assert slot._trust is False
    assert store.load().sessions == ()


def test_records_are_signed_under_the_token_key_not_the_sel_root():
    """The SEL root is read inside the sandbox; the token-signing key is not."""
    import inspect

    src = inspect.getsource(store)  # the module: the fixture swaps the function
    assert "token_secret._get_secret()" in src
    assert "_load_hmac_key" not in src


def test_with_no_trust_root_nothing_is_saved(monkeypatch):
    monkeypatch.setattr(store, "_signing_root", lambda: None)
    assert store.grant_counting(["dashboard:a"]) == (False, 0)
    assert not store.store_path().exists()


def test_the_store_is_bounded_and_drops_the_oldest_grant():
    for i in range(store.MAX_SESSIONS + 3):
        assert _grant([f"dashboard:s{i}"])
    on_disk = json.loads(store.store_path().read_text(encoding="utf-8"))["sessions"]
    assert len(on_disk) == store.MAX_SESSIONS
    sessions = store.load().sessions
    assert len(sessions) == store.MAX_SESSIONS
    assert "dashboard:s0" not in sessions
    assert sessions[-1] == f"dashboard:s{store.MAX_SESSIONS + 2}"


def test_a_revoke_that_cannot_be_written_removes_the_whole_store():
    assert _grant(["dashboard:a", "dashboard:b"])
    with patch.object(store, "_write", side_effect=OSError("disk full")):
        assert store.revoke(["dashboard:a"]) is True
    assert not store.store_path().exists()
    assert store.load().sessions == ()


def test_clear_forgets_everything():
    assert _grant(["dashboard:a"])
    assert store.clear() is True
    assert store.load().sessions == ()


def test_the_file_is_owner_only():
    import os
    import stat

    assert _grant(["dashboard:a"])
    if os.name == "posix":
        assert stat.S_IMODE(store.store_path().stat().st_mode) == 0o600


# ── the agent cannot write the record ─────────────────────────────────────


def test_default_store_lives_in_the_sealed_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_path_override", None)
    assert store.store_path().parent.name == "session-trust"


def test_agent_tools_can_neither_write_nor_read_the_record():
    """Read too, even with no OS sandbox: only the gateway ever reads it."""
    from kiro_crew.security.paths import is_sensitive_path, is_sensitive_write_path

    path = str(Path.home() / ".kiro" / "crew" / "session-trust" / "grants.json")
    assert is_sensitive_write_path(path)
    assert is_sensitive_path(path)


def test_the_record_is_hidden_in_the_sandbox():
    """No in-sandbox code reads it, so it is masked, not merely read-only."""
    from kiro_crew import sandbox

    assert "session-trust" in sandbox._CREW_HIDDEN_LEAVES
    assert "session-trust" in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert "session-trust" not in sandbox._CREW_READONLY_LEAVES
    assert "session-trust" not in sandbox._CREW_CHILD_READABLE_LEAVES


# ── crash-only: an owner stop forgets, anything else keeps ────────────────


@pytest.mark.asyncio
async def test_an_owner_stop_clears_trust_so_the_next_boot_asks_again(tmp_path):
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    assert await chat_trust_persistence.clear_on_shutdown_async(0) is True

    _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False
    assert store.load().sessions == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [1, 75, 3])
async def test_a_self_initiated_restart_keeps_trust(tmp_path, exit_code):
    """A non-zero shutdown exists only to be relaunched -- no one asked for it."""
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    assert await chat_trust_persistence.clear_on_shutdown_async(exit_code) is False

    _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 1
    assert slot._trust is True


@pytest.mark.asyncio
async def test_a_crash_that_never_reaches_shutdown_keeps_trust(tmp_path):
    """A crash, an OOM kill or the stall watchdog's _exit runs no shutdown at all."""
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 1
    assert slot._trust is True


def test_an_owner_stop_is_audited():
    assert _grant(["dashboard:a"])
    with patch.object(chat_trust_persistence, "sel") as sel_mock:
        assert chat_trust_persistence.clear_on_operator_stop_sync() is True
    kwargs = sel_mock.return_value.log_api_access.call_args.kwargs
    assert kwargs["operation"] == "session_trust:cleared_on_stop"
    assert kwargs["outcome"] == "cleared"


def test_a_clear_that_cannot_be_written_still_removes_the_record():
    """Fail toward re-consent: an owner stop never leaves trust for the next boot."""
    assert _grant(["dashboard:a"])
    with patch.object(store, "_write", side_effect=OSError("disk full")):
        assert chat_trust_persistence.clear_on_operator_stop_sync() is True
    assert not store.store_path().exists()


@pytest.mark.asyncio
async def test_a_clear_that_cannot_remove_the_record_leaves_an_owner_stop_marker(tmp_path):
    """The record survived the stop, so the marker makes the next boot restore nothing."""
    probe = _state(tmp_path).get_or_create_slot("s1")
    assert _grant([effective_session_key(probe)])
    with (
        patch.object(store, "_write", side_effect=OSError("disk full")),
        patch.object(store.os, "remove", side_effect=PermissionError("ro")),
    ):
        assert chat_trust_persistence.clear_on_operator_stop_sync() is True
    assert store.load().sessions  # the record itself survived

    _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False
    assert store.load().sessions == ()  # boot retried the clear
    # A chat rebuilt later in this process is not restored either.
    assert _grant([effective_session_key(probe)])
    assert chat_trust_persistence.restore_verdict(effective_session_key(probe)) is False


def test_a_clear_and_a_marker_that_both_fail_say_so():
    assert _grant(["dashboard:a"])
    with (
        patch.object(store, "_write", side_effect=OSError("disk full")),
        patch.object(store.os, "remove", side_effect=PermissionError("ro")),
        patch.object(store, "mark_owner_stop", return_value=False),
        patch.object(chat_trust_persistence, "sel") as sel_mock,
    ):
        assert chat_trust_persistence.clear_on_operator_stop_sync() is False
    assert sel_mock.return_value.log_api_access.call_args.kwargs["outcome"] == "clear_failed"


def test_the_gateway_shutdown_clears_trust_before_teardown():
    """Wired into the one shutdown path, keyed on its exit status, ahead of teardown."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator._shutdown_and_exit)
    call = src.index("clear_on_shutdown_async(exit_code)")
    assert src.index("exit_code = (") < call
    assert call < src.index("run_marker")
    assert call < src.index("self._shutdown()")


def test_the_force_exit_signal_path_clears_trust_too():
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator._install_shutdown_signal_handlers)
    clear = src.index("clear_on_force_exit(")
    assert clear < src.index("os._exit(0)")


# ── review hardening: failed revokes, revoke races, bounded exits ─────────


@pytest.mark.parametrize("body", [{"mode": "normal", "slot": "s1"}, {"mode": "normal"}])
@pytest.mark.asyncio
async def test_a_revoke_whose_saved_grant_cannot_be_removed_is_not_reported_done(tmp_path, body):
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    with (
        patch.object(store, "_write", side_effect=OSError("read-only")),
        patch.object(store.os, "remove", side_effect=PermissionError("read-only")),
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json=body)
            payload = await resp.json()

    assert resp.status == 500
    assert payload["ok"] is False
    # The live revoke still applied.
    assert state._slots["s1"]._trust is False


@pytest.mark.asyncio
async def test_a_revoke_during_the_rehydrate_verdict_read_wins(tmp_path):
    """A stale verdict read before an all-chats revoke must not re-trust the chat."""
    from kiro_crew.dashboard.chat_persistence import (
        rehydrate_slot_from_history_async,
        save_all_slots_to_history,
    )

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.append("user", "hello")
    slot._dirty = True
    save_all_slots_to_history(state)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    fresh = _state(tmp_path)
    real_verdict = chat_trust_persistence.restore_verdict

    def verdict_then_revoked(key):
        answer = real_verdict(key)
        chat_trust_persistence._bump_revoke_generation()  # the revoke's loop half
        return answer

    with patch.object(chat_trust_persistence, "restore_verdict", side_effect=verdict_then_revoked):
        rebuilt = await rehydrate_slot_from_history_async(fresh, "s1")

    assert rebuilt is not None
    assert rebuilt._trust is False


@pytest.mark.asyncio
async def test_a_revoke_during_the_boot_restore_read_wins(tmp_path):
    probe = _state(tmp_path).get_or_create_slot("s1")
    assert _grant([effective_session_key(probe)])
    real_load = chat_trust_persistence._load_for_restore

    def load_then_revoked():
        answer = real_load()
        chat_trust_persistence._bump_revoke_generation()
        return answer

    with patch.object(chat_trust_persistence, "_load_for_restore", side_effect=load_then_revoked):
        _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False


@pytest.mark.asyncio
async def test_one_revoke_during_the_boot_read_does_not_unrestore_the_others(tmp_path):
    """The stale snapshot is read again; the revoked chat stays out, the rest come back."""
    probe = _state(tmp_path)
    s1, s2 = probe.get_or_create_slot("s1"), probe.get_or_create_slot("s2")
    assert _grant([effective_session_key(s1), effective_session_key(s2)])
    real_load = chat_trust_persistence._load_for_restore
    calls = {"n": 0}

    def first_read_races_a_revoke():
        answer = real_load()
        calls["n"] += 1
        if calls["n"] == 1:
            store.revoke([effective_session_key(s1)])
            chat_trust_persistence._bump_revoke_generation()
        return answer

    with patch.object(
        chat_trust_persistence, "_load_for_restore", side_effect=first_read_races_a_revoke
    ):
        _, (r1, r2), restored = await _restart(tmp_path, "s1", "s2")
    assert restored == 1
    assert r1._trust is False
    assert r2._trust is True


def test_the_first_signal_clear_does_not_block_the_loop():
    """It runs on the event loop, so it only starts the clear and returns."""
    import threading
    import time

    release = threading.Event()

    def stalled() -> bool:
        release.wait(5.0)
        return True

    with patch.object(chat_trust_persistence, "clear_on_operator_stop_sync", side_effect=stalled):
        started = time.monotonic()
        worker = chat_trust_persistence.start_clear_on_signal()
        elapsed = time.monotonic() - started
    release.set()
    worker.join(5.0)
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_a_failed_boot_clear_keeps_the_owner_stop_marker(tmp_path):
    """The marker goes only once the clear succeeded, so a later crash cannot restore."""
    probe = _state(tmp_path).get_or_create_slot("s1")
    assert _grant([effective_session_key(probe)])
    assert store.mark_owner_stop()
    with patch.object(store, "clear", return_value=False):
        _, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False
    assert store.owner_stop_marked()


@pytest.mark.asyncio
async def test_a_superseded_grant_whose_rollback_fails_says_so(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    real_grant = store.grant_counting

    def grant_then_revoked_on_the_loop(keys, **kw):
        answer = real_grant(keys, **kw)
        chat_trust_persistence._bump_revoke_generation()
        return answer

    with (
        patch.object(store, "grant_counting", side_effect=grant_then_revoked_on_the_loop),
        patch.object(store, "revoke", return_value=False),
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            payload = await resp.json()
    assert resp.status == 500
    assert payload["code"] == "trust_revoke_not_durable"
    assert slot._trust is False


def test_an_ephemeral_signing_key_saves_nothing(monkeypatch):
    """The in-memory fallback key would sign a grant no later boot can read."""
    from kiro_crew.dashboard import token_secret

    monkeypatch.setattr(token_secret, "_SECRET", token_secret._ephemeral_secret())
    assert _REAL_SIGNING_ROOT() is None
    key = b"e" * 32
    monkeypatch.setattr(token_secret, "_SECRET", key)
    assert _REAL_SIGNING_ROOT() == key


def test_the_token_key_fallback_is_typed_ephemeral(monkeypatch):
    """Both fallback returns produce the ephemeral type; a persisted key does not."""
    import inspect

    from kiro_crew.dashboard import token_secret

    src = inspect.getsource(token_secret._load_or_create_secret)
    assert "return os.urandom(" not in src
    assert src.count("return _ephemeral_secret()") == 2
    monkeypatch.setattr(token_secret, "_SECRET", None)
    monkeypatch.setattr(token_secret, "_load_or_create_secret", token_secret._ephemeral_secret)
    assert token_secret.secret_is_persisted() is False
    monkeypatch.setattr(token_secret, "_SECRET", None)
    monkeypatch.setattr(token_secret, "_load_or_create_secret", lambda: b"p" * 32)
    assert token_secret.secret_is_persisted() is True


@pytest.mark.asyncio
async def test_a_revoke_whose_record_survived_is_not_restored_in_this_process(tmp_path):
    """A rebuilt tab must not get back trust the owner revoked, even off a stale record."""
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})
    with (
        patch.object(store, "_write", side_effect=OSError("read-only")),
        patch.object(store.os, "remove", side_effect=PermissionError("read-only")),
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
        assert resp.status == 500
    # The record survived on disk ...
    assert store.load().holds("dashboard:s1")
    # ... but neither restore path hands it back.
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is False
    _fresh, (slot,), restored = await _restart(tmp_path, "s1")
    assert restored == 0
    assert slot._trust is False


@pytest.mark.asyncio
async def test_an_all_chats_revoke_whose_clear_failed_restores_nothing(tmp_path):
    assert _grant(["dashboard:s1"])
    with (
        patch.object(store, "_write", side_effect=OSError("read-only")),
        patch.object(store.os, "remove", side_effect=PermissionError("read-only")),
    ):
        assert await chat_trust_persistence.persist_revoke_all() is False
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is False
    # A clear that later succeeds lifts it, and a fresh grant restores again.
    assert await chat_trust_persistence.persist_revoke_all() is True
    assert _grant(["dashboard:s1"])
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is True


@pytest.mark.asyncio
async def test_a_card_answered_elsewhere_whose_take_back_fails_says_so(tmp_path):
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    real_persist = chat_trust_persistence.persist_grant

    async def persist_then_rejected_elsewhere(request, slots):
        answer = await real_persist(request, slots)
        fut.set_result("rejected")
        return answer

    with (
        patch.object(
            chat_trust_persistence, "persist_grant", side_effect=persist_then_rejected_elsewhere
        ),
        patch.object(store, "revoke", return_value=False),
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
            payload = await resp.json()
    assert resp.status == 500
    assert payload["code"] == "trust_revoke_not_durable"
    assert slot._trust is False


@pytest.mark.asyncio
async def test_a_card_grant_overtaken_by_a_revoke_whose_take_back_fails_says_so(tmp_path):
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    real_grant = store.grant_counting

    def grant_then_revoked_on_the_loop(keys, **kw):
        answer = real_grant(keys, **kw)
        chat_trust_persistence._bump_revoke_generation()
        return answer

    with (
        patch.object(store, "grant_counting", side_effect=grant_then_revoked_on_the_loop),
        patch.object(store, "revoke", return_value=False),
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
            payload = await resp.json()
    assert resp.status == 500
    assert payload["code"] == "trust_revoke_not_durable"
    assert payload["approved"] is True
    assert slot._trust is False


def _superseding_grant():
    """``grant_counting`` that saves, then has a revoke land while it was in flight."""
    real_grant = store.grant_counting

    def grant_then_revoked_on_the_loop(keys, **kw):
        answer = real_grant(keys, **kw)
        chat_trust_persistence._bump_revoke_generation()
        return answer

    return patch.object(store, "grant_counting", side_effect=grant_then_revoked_on_the_loop)


@pytest.mark.asyncio
async def test_a_refused_grant_still_reconciles_policies_and_sweeps_nothing(tmp_path):
    """The refusal is answered after the shared policy pass, and approves no card."""
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    with _superseding_grant():
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
    assert resp.status == 409
    state.sessions.set_approval_policy.assert_any_call(effective_session_key(slot), "")
    state.push_slots_update.assert_called()
    assert not fut.done()


@pytest.mark.asyncio
async def test_a_refused_grant_is_audited(tmp_path):
    from kiro_crew.dashboard import chat_handlers

    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    with (
        _superseding_grant(),
        patch.object(chat_handlers, "sel") as sel_mock,
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
    assert resp.status == 409
    outcomes = [
        c.kwargs.get("outcome") for c in sel_mock.return_value.log_api_access.call_args_list
    ]
    assert "refused:trust_superseded" in outcomes


def _join_force_clear() -> None:
    """Join every clear worker inside the test, while its own patches still hold.

    A worker that finishes after the test's patches or the autouse fixture unwind
    would run the real clear against the real data home's record.
    """
    _join_trust_workers()


def test_the_force_exit_clear_never_waits():
    """The force exit runs on the loop: a stalled disk must not delay it at all."""
    import threading
    import time

    release = threading.Event()

    def stalled() -> bool:
        release.wait(5.0)
        return True

    with patch.object(chat_trust_persistence, "clear_on_operator_stop_sync", side_effect=stalled):
        started = time.monotonic()
        chat_trust_persistence.clear_on_force_exit()
        elapsed = time.monotonic() - started
        release.set()
        _join_force_clear()
    assert elapsed < 0.1


def test_the_boot_restore_does_not_hold_the_listener():
    """Started as a task, never awaited on the boot path."""
    import inspect

    from kiro_crew.dashboard import server

    src = inspect.getsource(server.start_dashboard)
    assert "chat_trust_persistence.schedule_restore(state)" in src
    assert "await chat_trust_persistence.restore" not in src


@pytest.mark.asyncio
async def test_schedule_restore_runs_in_the_background(tmp_path):
    probe = _state(tmp_path).get_or_create_slot("s1")
    assert _grant([effective_session_key(probe)])
    fresh = _state(tmp_path)
    slot = fresh.get_or_create_slot("s1")
    task = chat_trust_persistence.schedule_restore(fresh)
    assert slot._trust is False  # nothing ran on the caller's turn
    assert await task == 1
    assert slot._trust is True


@pytest.mark.asyncio
async def test_the_background_restore_pushes_the_restored_trust(tmp_path):
    probe = _state(tmp_path).get_or_create_slot("s1")
    assert _grant([effective_session_key(probe)])
    fresh = _state(tmp_path)
    slot = fresh.get_or_create_slot("s1")
    assert await chat_trust_persistence.restore_in_background(fresh) == 1
    assert slot._trust is True
    fresh.push_slots_update.assert_called()


@pytest.mark.asyncio
async def test_the_background_restore_never_raises(tmp_path):
    with patch.object(
        chat_trust_persistence, "restore_persisted_trust_async", side_effect=RuntimeError("x")
    ):
        assert await chat_trust_persistence.restore_in_background(_state(tmp_path)) == 0


@pytest.mark.asyncio
async def test_a_grant_that_cannot_be_saved_goes_live_only(tmp_path):
    """Trust works as it did before it was ever saved; the owner hears it won't survive."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    with patch.object(store, "_write", side_effect=OSError("read-only")):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
    assert resp.status == 200
    assert slot._trust is True
    state.sessions.set_approval_policy.assert_any_call(effective_session_key(slot), "auto")
    assert store.load().sessions == ()
    titles = [c.args[1] for c in state.notify.call_args_list]
    assert "Chat trust is on until the next restart" in titles


@pytest.mark.asyncio
async def test_a_non_durable_grant_still_goes_ahead(tmp_path):
    """An app token's grant never persists, so a broken store cannot refuse it."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    with (
        patch("kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True),
        patch.object(store, "_write", side_effect=OSError("read-only")),
    ):
        await _mode(state, APP_TOKEN, {"mode": "trust", "slot": "s1"})
    assert slot._trust is True


def test_a_full_store_tells_the_owner_what_it_dropped():
    import asyncio

    keys = [f"dashboard:s{i}" for i in range(store.MAX_SESSIONS)]
    assert _grant(keys)
    state = MagicMock()
    request = MagicMock()
    request.app = {"state": state}
    slot = MagicMock(_app="", _trust=True)
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:new"]),
    ):
        assert asyncio.run(chat_trust_persistence.persist_grant(request, [slot])) is True
    state.notify.assert_called_once()
    assert "1" in state.notify.call_args.args[2]


class _Exited(BaseException):
    """Stands in for ``os._exit`` so the gateway's exit path can run in-process."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _raise_exit(code: int) -> None:
    raise _Exited(code)


def _quiet_exit_path(monkeypatch) -> None:
    """Stub the process-wide work around the gateway's hard exit."""
    import kiro_crew.cli as crew_cli
    from kiro_crew import eventlog_hooks, session
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.slack import gateway as gw

    async def _no_drain() -> None:
        return None

    monkeypatch.setattr(gw.os, "_exit", _raise_exit)
    monkeypatch.setattr(session, "cleanup_orphaned_sessions", lambda **_kw: None)
    monkeypatch.setattr(crew_log_emit, "drain_for_shutdown", lambda: True)
    monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", lambda: True)
    monkeypatch.setattr(crew_cli, "drain_log_queue_before_hard_exit", _no_drain)
    monkeypatch.setattr(crew_cli, "_stop_log_queue_listener", lambda timeout=0.0: None)


def _fake_gateway():
    from types import SimpleNamespace

    async def _shutdown() -> None:
        return None

    return SimpleNamespace(
        dashboard_state=None,
        _dashboard_port=0,
        _marker_write_task=None,
        _shutdown=_shutdown,
    )


@pytest.mark.asyncio
async def test_the_gateway_shutdown_path_clears_trust_on_an_owner_stop(monkeypatch):
    """Driven, not read: an owner stop through ``_shutdown_and_exit`` exits 0 and forgets."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    _quiet_exit_path(monkeypatch)
    assert _grant(["dashboard:s1"])

    with pytest.raises(_Exited) as exited:
        await GatewayOrchestrator._shutdown_and_exit(_fake_gateway(), None)

    assert exited.value.code == 0
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_the_gateway_shutdown_path_keeps_trust_on_a_self_restart(monkeypatch):
    """The stale-asset self-restart exits non-zero to be relaunched; trust survives it."""
    import asyncio

    from kiro_crew.dashboard.stale_asset_watchdog import STALE_ASSET_EXIT_CODE
    from kiro_crew.slack.gateway import GatewayOrchestrator

    _quiet_exit_path(monkeypatch)
    assert _grant(["dashboard:s1"])
    fired: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    fired.set_result(True)

    with pytest.raises(_Exited) as exited:
        await GatewayOrchestrator._shutdown_and_exit(_fake_gateway(), fired)

    assert exited.value.code == STALE_ASSET_EXIT_CODE
    assert STALE_ASSET_EXIT_CODE != 0
    assert store.load().sessions == ("dashboard:s1",)


@pytest.mark.asyncio
async def test_the_force_exit_signal_clears_trust(monkeypatch):
    """A second Ctrl-C/SIGTERM hard-exits from the handler; it still forgets."""
    import asyncio

    from kiro_crew.slack import gateway as gw
    from kiro_crew.slack.gateway import GatewayOrchestrator

    _quiet_exit_path(monkeypatch)
    monkeypatch.setattr(gw, "shutdown_event", asyncio.Event())
    handlers: list = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda _sig, cb: handlers.append(cb))
    assert _grant(["dashboard:s1"])

    GatewayOrchestrator._install_shutdown_signal_handlers(_fake_gateway())
    on_signal = handlers[0]
    on_signal()  # first signal: graceful shutdown requested
    assert gw.shutdown_event.is_set()
    # Cleared on receipt (on a thread the callback does not wait for): a SIGKILL
    # that follows a slow shutdown runs nothing.
    import threading

    for t in threading.enumerate():
        if t.name == "session-trust-signal-clear":
            t.join(5.0)
    assert store.load().sessions == ()
    assert _grant(["dashboard:s1"])
    with pytest.raises(_Exited) as exited:
        on_signal()  # second signal: force exit

    assert exited.value.code == 0
    # The force exit does not wait for its clear; the test does.
    for t in threading.enumerate():
        if t.name == "session-trust-force-clear":
            t.join(5.0)
    assert store.load().sessions == ()


def test_the_stall_watchdog_exits_without_touching_trust():
    """The watchdog dumps and exits from a signal; it must never clear the record."""
    import inspect

    from kiro_crew.dashboard import loop_watchdog

    src = inspect.getsource(loop_watchdog)
    assert "session_trust" not in src
    assert "chat_trust_persistence" not in src
    assert "exit=True" in src  # faulthandler's own _exit, not the shutdown path


# ── store-directory links, write order, and grants after an owner stop ─────


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks need privilege on Windows")
def test_a_planted_store_directory_link_is_never_followed(tmp_path):
    """An older build could have left ``session-trust`` as a link to elsewhere."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "grants.json"
    victim.write_text("keep me", encoding="utf-8")
    store_dir = store.store_path().parent
    store_dir.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(elsewhere, store_dir)

    assert store.grant_counting(["dashboard:s1"]) == (False, 0)
    assert store.load().readable is False
    assert store.revoke(["dashboard:s1"]) is False
    assert store.clear() is False
    assert victim.read_text(encoding="utf-8") == "keep me"


@pytest.mark.asyncio
async def test_a_revoke_issued_first_cannot_erase_a_later_grant(tmp_path):
    """Store writes land in the order the loop issued them, not the threads'."""
    import asyncio
    import threading

    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot = state._slots["s1"]
    revoke_started = threading.Event()
    release = threading.Event()
    real_revoke = store.revoke

    def slow_revoke(keys):
        revoke_started.set()
        release.wait(5.0)
        return real_revoke(keys)

    with patch.object(store, "revoke", side_effect=slow_revoke):
        revoking = asyncio.create_task(
            chat_trust_persistence.persist_revoke_keys(chat_trust_persistence.session_keys([slot]))
        )
        await asyncio.to_thread(revoke_started.wait, 5.0)
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            granting = asyncio.create_task(
                client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            )
            await asyncio.sleep(0.2)
            release.set()
            await revoking
            resp = await granting
            await resp.read()
    # The grant was issued after the revoke, so the grant is what stays saved.
    assert store.load().holds("dashboard:s1")


@pytest.mark.asyncio
async def test_a_fresh_grant_after_an_unsettled_owner_stop_sticks(tmp_path, monkeypatch):
    """Boot withheld an old record; the owner's next click must still survive."""
    assert _grant(["dashboard:old"])
    marker = store._marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("owner-stop\n", encoding="utf-8")
    monkeypatch.setattr(chat_trust_persistence, "_owner_stopped", True)

    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})

    assert chat_trust_persistence._owner_stopped is False
    assert not marker.exists()
    # The stale pre-stop record is gone; only the fresh grant is restorable.
    assert store.load().sessions == ("dashboard:s1",)
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is True
    assert chat_trust_persistence.restore_verdict("dashboard:old") is False


@pytest.mark.asyncio
async def test_a_grant_after_an_owner_stop_that_cannot_settle_goes_live_only(tmp_path, monkeypatch):
    """Not saved (the stale stop could not be settled), so live-only, and nothing written."""
    monkeypatch.setattr(chat_trust_persistence, "_owner_stopped", True)
    monkeypatch.setattr(store, "settle_owner_stop", lambda *_a: False)
    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    async with TestClient(TestServer(_app(state, OWNER))) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
    assert resp.status == 200
    assert state._slots["s1"]._trust is True
    assert store.load().sessions == ()
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is False


def test_the_in_app_restart_clears_trust_before_it_drains():
    """The dashboard Restart and update restarts exec in place, skipping shutdown."""
    import inspect

    from kiro_crew.dashboard.handlers import updates

    src = inspect.getsource(updates._restart_gateway)
    clear = src.index("chat_trust_persistence.clear_on_shutdown_async(0)")
    assert clear < src.index("await state.sessions.close_all()")
    assert clear < src.rindex("execv")
    # After the refusals that leave the gateway serving: a refused restart keeps trust.
    assert src.index("return False") < clear


def test_a_windows_cli_stop_leaves_the_owner_stop_marker():
    """The Windows CLI stop runs no shutdown in the gateway, so the CLI marks the stop."""
    import inspect

    from kiro_crew import cli_server

    cli_server._mark_owner_stop_for_session_trust()
    assert store.owner_stop_marked()
    src = inspect.getsource(cli_server._stop)
    mark = src.index("_mark_owner_stop_for_session_trust()")
    withdraw = src.index("session_trust_store.withdraw_owner_stop()")
    # BEFORE the kill, so a kill that lands cannot be followed by a failed write;
    # taken back only when no kill landed, since the gateway is then still serving.
    assert mark < src.index("process_tree(") < withdraw
    assert src.index("if platform_compat.IS_WINDOWS and _windows_marked_here and not sent:") < (
        withdraw
    )
    # A marker that cannot be written still stops, with a warning.
    assert "elif session_trust_store.store_path().exists():" in src
    assert "Stopping anyway" in src


# ── the owner-stop marker is as sealed as the record ──────────────────────


def test_the_owner_stop_marker_lives_in_its_own_sealed_leaf(tmp_path, monkeypatch):
    """Deleting it would hand back trust an owner stop withdrew, so no agent may."""
    from kiro_crew import sandbox
    from kiro_crew.security.paths import is_sensitive_path, is_sensitive_write_path

    monkeypatch.setattr(store, "_path_override", None)
    assert store._marker_path().parent.name == store.MARKER_DIR == "session-trust-stop"
    assert store._marker_path().parent != store.store_path().parent
    assert "session-trust-stop" in sandbox._CREW_HIDDEN_LEAVES
    assert "session-trust-stop" in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    path = str(Path.home() / ".kiro" / "crew" / "session-trust-stop" / "owner-stop")
    assert is_sensitive_write_path(path)
    assert is_sensitive_path(path)


def test_the_owner_stop_marker_refuses_a_linked_directory(tmp_path):
    """A planted link must not redirect the marker's write or its removal."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "session-trust-stop").symlink_to(elsewhere, target_is_directory=True)
    assert store.mark_owner_stop() is False
    assert not (elsewhere / "owner-stop").exists()
    (elsewhere / "owner-stop").write_text("owner-stop\n")
    assert store.settle_owner_stop() is False
    assert (elsewhere / "owner-stop").exists()


def test_a_non_ascii_mac_restores_nothing_instead_of_raising(tmp_path):
    """compare_digest raises TypeError on a non-ASCII str; the store must not."""
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "sessions": ["dashboard:s1"], "mac": "é" * 64}))
    snapshot = store.load()
    assert snapshot.readable is False
    assert snapshot.sessions == ()
    assert store.grant_counting(["dashboard:s2"]) == (True, 0)
    assert store.revoke(["dashboard:s2"]) is True


@pytest.mark.asyncio
async def test_an_in_app_restart_whose_clear_fails_is_refused_before_the_drain(
    monkeypatch, tmp_path
):
    """A restart that exec'd anyway would come back restoring trust it withdrew."""
    from unittest.mock import AsyncMock, Mock

    from test_restart_pruned_interpreter_guard import _executable
    from test_restart_pruned_interpreter_guard import _state as restart_state

    from kiro_crew.dashboard.handlers import updates

    module_exec = Mock()
    monkeypatch.setattr(updates, "reexec_python_module", module_exec)
    monkeypatch.setattr(updates, "reexec_launcher", Mock())
    monkeypatch.setattr(updates, "resolve_restart_launcher", lambda: None)
    monkeypatch.setattr(
        chat_trust_persistence, "clear_on_shutdown_async", AsyncMock(return_value=False)
    )
    state = restart_state()
    exe = _executable(tmp_path, "python")
    assert await updates._restart_gateway(state, resolver=lambda: exe) is False
    state.sessions.close_all.assert_not_awaited()
    module_exec.assert_not_called()
    assert "error" in [c.args[0] for c in state.push_update_progress.call_args_list]


def test_the_first_signal_writes_the_owner_stop_marker_before_clearing():
    """A clear the exit kills mid-write still leaves the next boot restoring nothing."""
    import threading

    release = threading.Event()
    marked_before_clear = {}

    def stalled() -> bool:
        marked_before_clear["value"] = store.owner_stop_marked()
        release.wait(5.0)
        return True

    with patch.object(chat_trust_persistence, "clear_on_operator_stop_sync", side_effect=stalled):
        worker = chat_trust_persistence.start_clear_on_signal()
        for _ in range(100):
            if "value" in marked_before_clear:
                break
            threading.Event().wait(0.02)
        assert store.owner_stop_marked()
        release.set()
        worker.join(5.0)
    assert marked_before_clear == {"value": True}


def test_the_force_exit_never_waits_even_on_a_stalled_marker_write():
    """The force exit runs on the event loop: it must not block on the disk at all."""
    import threading
    import time

    release = threading.Event()

    def stalled_mark() -> bool:
        release.wait(5.0)
        return True

    with patch.object(store, "mark_owner_stop", side_effect=stalled_mark):
        started = time.monotonic()
        chat_trust_persistence.clear_on_force_exit()
        elapsed = time.monotonic() - started
        release.set()
        _join_force_clear()
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_a_refused_card_trust_does_not_read_trusted(tmp_path):
    """The card resolves as a plain approval when a revoke overtook its save."""
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    with _superseding_grant():
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
            payload = await resp.json()
    assert resp.status == 200
    assert payload["code"] == "trust_superseded"
    assert slot._trust is False
    assert json.loads(slot.messages[-1]["cls"])["resolved"] == "approved"


# ── a grant is fenced to the identity it saved, and to an owner stop ──────


def _rebinding_grant(slot, new_key):
    """``grant_counting`` that saves, then has the slot's session key rebound."""
    real_grant = store.grant_counting

    def grant_then_rebound(keys, **kw):
        answer = real_grant(keys, **kw)
        slot.linked_session_key = new_key
        return answer

    return patch.object(store, "grant_counting", side_effect=grant_then_rebound)


@pytest.mark.asyncio
async def test_a_grant_whose_chat_is_rebound_during_the_save_is_refused(tmp_path):
    """The saved key would not be the trusted key, so a crash would lose the grant."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    before = effective_session_key(slot)
    with _rebinding_grant(slot, "slack:1712793600.123456"):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            payload = await resp.json()
    assert effective_session_key(slot) != before
    assert resp.status == 409
    assert payload["code"] == "trust_superseded"
    assert slot._trust is False
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_card_grant_whose_chat_is_rebound_during_the_save_is_refused(tmp_path):
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    with _rebinding_grant(slot, "slack:1712793600.123456"):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
            payload = await resp.json()
    assert payload["code"] == "trust_superseded"
    assert fut.result() == "approved"
    assert slot._trust is False
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_grant_after_an_owner_stop_began_is_not_saved():
    """The stop's clear must not be overtaken by a record written after it."""
    assert await chat_trust_persistence.clear_on_shutdown_async(0) is True
    request = MagicMock()
    with patch.object(chat_trust_persistence, "persists_grant", return_value=True):
        slot = MagicMock(_app="", key="s1", linked_session_key="")
        with patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]):
            assert await chat_trust_persistence.persist_grant(request, [slot]) is False
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_the_owner_stop_clear_waits_for_a_grant_already_writing():
    """A grant mid-write finishes first, then the clear removes it with the rest."""
    import asyncio
    import threading

    entered = threading.Event()
    release = threading.Event()
    real_grant = store.grant_counting

    def slow_grant(keys, **kw):
        entered.set()
        release.wait(5.0)
        return real_grant(keys, **kw)

    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
        patch.object(store, "grant_counting", side_effect=slow_grant),
    ):
        grant = asyncio.create_task(chat_trust_persistence.persist_grant(request, [slot]))
        await asyncio.to_thread(entered.wait, 5.0)
        stop = asyncio.create_task(chat_trust_persistence.clear_on_shutdown_async(0))
        await asyncio.sleep(0.05)
        release.set()
        assert await grant is True
        assert await stop is True
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_refused_restart_lets_grants_save_again():
    with (
        patch.object(chat_trust_persistence, "clear_on_operator_stop_sync", return_value=False),
        patch.object(store, "mark_owner_stop", return_value=False),
    ):
        assert await chat_trust_persistence.clear_on_shutdown_async(0) is False
    assert chat_trust_persistence._stopping is False


@pytest.mark.asyncio
async def test_an_owner_stop_whose_clear_times_out_still_marks_the_stop(monkeypatch):
    """A write holding the store's order past the bound must not let the stop keep trust."""
    assert _grant(["dashboard:s1"])
    monkeypatch.setattr(chat_trust_persistence, "_SHUTDOWN_CLEAR_TIMEOUT", 0.1)
    async with chat_trust_persistence._write_order():
        assert await chat_trust_persistence.clear_on_shutdown_async(0) is True
    assert store.owner_stop_marked()
    assert store.load().sessions == ("dashboard:s1",)  # the record survived
    monkeypatch.setattr(chat_trust_persistence, "_owner_stopped", False)
    assert chat_trust_persistence._load_for_restore().sessions == ()


@pytest.mark.asyncio
async def test_a_revoke_that_cannot_remove_the_record_notifies_the_owner(tmp_path):
    """The picker drops the response code, so the notice is what the owner sees."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    assert _grant([effective_session_key(slot)])
    with patch.object(store, "revoke", return_value=False):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
    assert resp.status == 500
    titles = [c.args[1] for c in state.notify.call_args_list]
    assert "Saved chat trust could not be removed" in titles


def test_an_early_restore_verdict_honours_the_owner_stop_marker():
    """A rehydration before the boot restore must not hand back what a stop withdrew."""
    assert _grant(["dashboard:s1"])
    assert store.mark_owner_stop()
    assert chat_trust_persistence._owner_stopped is False  # no boot restore ran yet
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is False
    assert chat_trust_persistence._owner_stopped is True
    # Settled: the surviving record is cleared and the marker removed.
    assert store.load().sessions == ()
    assert not store.owner_stop_marked()


def test_a_cli_restart_after_a_crash_restores_nothing():
    """No incumbent clears its own record, so the restart marks the stop first."""
    from kiro_crew import cli_server

    assert _grant(["dashboard:s1"])
    seen = {}

    def replacement_boots(_port):
        seen["marked"] = store.owner_stop_marked()

    with patch.object(cli_server, "_restart_gateway", side_effect=replacement_boots):
        cli_server._restart(None)
    assert seen["marked"] is True
    assert chat_trust_persistence._load_for_restore().sessions == ()


@pytest.mark.parametrize("failure", [SystemExit(1), RuntimeError("boom")])
def test_a_failed_cli_restart_takes_its_mark_back(failure):
    """The gateway a refused restart leaves running must keep its later grants."""
    from kiro_crew import cli_server

    with (
        patch.object(cli_server, "_restart_gateway", side_effect=failure),
        patch.object(cli_server, "_incumbent_still_serving", return_value=True),
    ):
        with pytest.raises(type(failure)):
            cli_server._restart(None)
    assert not store.owner_stop_marked()


def test_a_failed_cli_restart_with_nothing_serving_keeps_its_mark():
    """A replacement that died leaves no gateway to keep grants for: stay re-consent."""
    from kiro_crew import cli_server

    with (
        patch.object(cli_server, "_restart_gateway", side_effect=SystemExit(1)),
        patch.object(cli_server, "_incumbent_still_serving", return_value=False),
    ):
        with pytest.raises(SystemExit):
            cli_server._restart(None)
    assert store.owner_stop_marked()


def test_a_cli_restart_that_cannot_mark_the_stop_refuses(capsys):
    from kiro_crew import cli_server

    assert _grant(["dashboard:s1"])
    with (
        patch.object(store, "mark_owner_stop", return_value=False),
        patch.object(cli_server, "_restart_gateway") as restart,
    ):
        with pytest.raises(SystemExit) as exc:
            cli_server._restart(None)
    assert exc.value.code == 1
    restart.assert_not_called()
    assert "Not restarting" in capsys.readouterr().out


def test_a_failed_cli_restart_keeps_a_mark_it_did_not_write():
    from kiro_crew import cli_server

    assert store.mark_owner_stop()
    with patch.object(cli_server, "_restart_gateway", side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            cli_server._restart(None)
    assert store.owner_stop_marked()


@pytest.mark.parametrize("mode", ["normal", "trust_reads"])
@pytest.mark.asyncio
async def test_a_revoke_removes_the_saved_grant_before_turning_trust_off_live(tmp_path, mode):
    """Durable first: a crash between the two steps must not leave a restorable grant."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._trust = True
    key = effective_session_key(slot)
    assert _grant([key])
    real_revoke = store.revoke
    seen = {}

    def revoke_and_look(keys):
        seen["keys"] = list(keys)
        seen["live_trust_at_revoke"] = slot._trust
        return real_revoke(keys)

    with patch.object(store, "revoke", side_effect=revoke_and_look):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": "s1"})
    assert resp.status == 200
    assert seen == {"keys": [key], "live_trust_at_revoke": True}
    assert slot._trust is False
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_the_all_chats_revoke_clears_the_store_before_turning_trust_off_live(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._trust = True
    assert _grant([effective_session_key(slot)])
    real_clear = store.clear
    seen = {}

    def clear_and_look():
        seen["live_trust_at_clear"] = slot._trust
        return real_clear()

    with patch.object(store, "clear", side_effect=clear_and_look):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
    assert resp.status == 200
    assert seen == {"live_trust_at_clear": True}
    assert slot._trust is False


def _failing_restart(cli_server, incumbent):
    """A ``_restart_gateway`` that found *incumbent* by its run marker, then failed."""

    def restart(_port):
        cli_server._restart_incumbent["pid"] = incumbent
        raise SystemExit(1)

    return restart


def test_a_failed_cli_restart_keeps_its_mark_when_a_replacement_took_the_lock():
    """A different lock holder means the restart happened: its mark must stand."""
    from kiro_crew import cli_server

    with (
        patch.object(cli_server, "_lock_holder_pid", return_value=2222),
        patch.object(
            cli_server, "_restart_gateway", side_effect=_failing_restart(cli_server, 1111)
        ),
    ):
        with pytest.raises(SystemExit):
            cli_server._restart(None)
    assert store.owner_stop_marked()


def test_a_failed_cli_restart_takes_back_its_mark_when_the_incumbent_still_serves():
    from kiro_crew import cli_server

    with (
        patch.object(cli_server, "_lock_holder_pid", return_value=1111),
        patch.object(
            cli_server, "_restart_gateway", side_effect=_failing_restart(cli_server, 1111)
        ),
    ):
        with pytest.raises(SystemExit):
            cli_server._restart(None)
    assert not store.owner_stop_marked()


def test_a_cli_restart_with_nothing_saved_proceeds_when_the_marker_cannot_be_written():
    """No record means nothing to withdraw, so an unwritable marker blocks nothing."""
    from kiro_crew import cli_server

    assert not store.store_path().exists()
    with (
        patch.object(store, "mark_owner_stop", return_value=False),
        patch.object(cli_server, "_restart_gateway") as restart,
    ):
        cli_server._restart(None)
    restart.assert_called_once()


@pytest.mark.asyncio
async def test_a_grant_after_a_failed_all_chats_revoke_does_not_republish_the_old_grants():
    """The rewrite replaces the store rather than merging the record the revoke left."""
    assert _grant(["dashboard:old"])
    with patch.object(store, "clear", return_value=False):
        assert await chat_trust_persistence.persist_revoke_all() is False
    assert store.load().sessions == ("dashboard:old",)  # survived on disk
    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
    ):
        assert await chat_trust_persistence.persist_grant(request, [slot]) is True
    assert store.load().sessions == ("dashboard:s1",)
    assert chat_trust_persistence._withdrawn_all is False


@pytest.mark.asyncio
async def test_a_grant_after_a_failed_revoke_does_not_republish_that_grant():
    assert _grant(["dashboard:old", "dashboard:keep"])
    with patch.object(store, "_rewrite_or_remove", return_value=False):
        assert await chat_trust_persistence.persist_revoke_keys(["dashboard:old"]) is False
    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
    ):
        assert await chat_trust_persistence.persist_grant(request, [slot]) is True
    assert store.load().sessions == ("dashboard:keep", "dashboard:s1")


@pytest.mark.asyncio
async def test_a_deleted_session_takes_its_saved_trust_with_it(tmp_path):
    state = _state(tmp_path)
    assert _grant(["dashboard:s1", "dashboard:other"])
    taken = await chat_trust_persistence.forget_deleted_session(
        state, ("dashboard:s1", None, "dashboard:s1")
    )
    assert taken.ok and taken.held == ("dashboard:s1",)
    assert store.load().sessions == ("dashboard:other",)
    state.notify.assert_not_called()
    # A delete that then does not commit puts the grant back.
    await chat_trust_persistence.restore_deleted_session_trust(taken)
    assert set(store.load().sessions) == {"dashboard:other", "dashboard:s1"}


@pytest.mark.asyncio
async def test_a_deleted_session_whose_trust_cannot_be_removed_notifies(tmp_path):
    state = _state(tmp_path)
    assert _grant(["dashboard:s1"])
    with patch.object(store, "revoke", return_value=False):
        taken = await chat_trust_persistence.forget_deleted_session(state, ("dashboard:s1",))
        assert not taken.ok
    titles = [c.args[1] for c in state.notify.call_args_list]
    assert "Saved chat trust could not be removed" in titles


def test_both_history_delete_paths_forget_saved_trust():
    import inspect

    from kiro_crew.dashboard.handlers import sessions

    for handler in (sessions._delete_history_row, sessions._clear_history_rows):
        assert "chat_trust_persistence.forget_deleted_session(" in inspect.getsource(handler)


def test_a_refused_service_restart_still_names_its_incumbent(monkeypatch):
    """The service branch exits before the listener path; the mark must still come back."""
    from kiro_crew import cli_server

    monkeypatch.setattr(cli_server.run_marker, "read_pid", lambda _port: 4242)
    monkeypatch.setattr(cli_server, "_lock_holder_pid", lambda: 4242)

    class _Report:
        attempted = True
        restarted: list = []
        failures: list = []

        def __bool__(self):
            return False

    monkeypatch.setattr(cli_server.service_controller, "restart_service", lambda: _Report())
    monkeypatch.setattr(cli_server.service_controller, "is_service_active", lambda: True)
    with pytest.raises(SystemExit):
        cli_server._restart(None)
    assert cli_server._restart_incumbent.get("pid") == 4242
    assert not store.owner_stop_marked()


@pytest.mark.parametrize("target_exists", [True, False])
def test_a_linked_marker_directory_reads_as_marked(tmp_path, target_exists):
    """A planted link, even a dangling one, must not read as 'no owner stop'."""
    elsewhere = tmp_path / "elsewhere"
    if target_exists:
        elsewhere.mkdir()
    (tmp_path / "session-trust-stop").symlink_to(elsewhere, target_is_directory=True)
    assert store.owner_stop_marked() is True
    assert _grant(["dashboard:s1"])
    assert chat_trust_persistence.restore_verdict("dashboard:s1") is False


def test_a_marker_path_that_is_not_a_directory_reads_as_marked(tmp_path):
    (tmp_path / "session-trust-stop").write_text("x")
    assert store.owner_stop_marked() is True


def test_an_absent_marker_reads_unmarked(tmp_path):
    assert store.owner_stop_marked() is False
    (tmp_path / "session-trust-stop").mkdir()
    assert store.owner_stop_marked() is False
    assert store.mark_owner_stop()
    assert store.owner_stop_marked() is True


@pytest.mark.asyncio
async def test_a_revoke_is_withheld_before_its_store_write_lands():
    """A restore verdict read while the removal is in flight already sees the revoke."""
    import asyncio
    import threading

    assert _grant(["dashboard:s1"])
    entered = threading.Event()
    release = threading.Event()
    real_revoke = store.revoke

    def slow_revoke(keys):
        entered.set()
        release.wait(5.0)
        return real_revoke(keys)

    with patch.object(store, "revoke", side_effect=slow_revoke):
        revoking = asyncio.ensure_future(
            chat_trust_persistence.persist_revoke_keys(["dashboard:s1"])
        )
        await asyncio.to_thread(entered.wait, 5.0)
        assert chat_trust_persistence.is_withdrawn("dashboard:s1")
        assert (
            await asyncio.to_thread(chat_trust_persistence.restore_verdict, "dashboard:s1") is False
        )
        release.set()
        assert await revoking is True
    # Removed for good, so the in-memory withholding is lifted.
    assert not chat_trust_persistence.is_withdrawn("dashboard:s1")


def test_a_late_settle_cannot_erase_a_grant_made_after_the_marker_was_settled():
    """A boot restore that settles after a grant already did finds no marker and clears nothing."""
    assert store.mark_owner_stop()
    assert store.settle_owner_stop()  # the grant path's settle, in the write order
    assert _grant(["dashboard:fresh"])
    assert store.settle_owner_stop()  # the delayed restore's settle
    assert store.load().sessions == ("dashboard:fresh",)


@pytest.mark.asyncio
async def test_a_grant_settles_an_on_disk_marker_this_process_has_not_read():
    """The marker the boot restore has not reached yet is settled in the grant's order."""
    assert _grant(["dashboard:stale"])
    assert store.mark_owner_stop()
    assert chat_trust_persistence._owner_stopped is False
    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
    ):
        assert await chat_trust_persistence.persist_grant(request, [slot]) is True
    assert not store.owner_stop_marked()
    assert store.load().sessions == ("dashboard:s1",)
    # The late boot restore now restores the fresh grant, not nothing-and-wipe.
    assert chat_trust_persistence._load_for_restore().sessions == ("dashboard:s1",)


def test_both_history_deletes_remove_saved_trust_before_unlinking():
    import inspect

    from kiro_crew.dashboard.handlers import sessions

    for handler in (sessions._delete_history_row, sessions._clear_history_rows):
        src = inspect.getsource(handler)
        assert src.index("chat_trust_persistence.forget_deleted_session(") < src.index(
            "_delete_history_session,"
        )


def test_a_closed_dashboard_row_resolves_to_its_saved_session_key():
    """A closed row is named by its transcript stem; its trust sits under the session key."""
    from kiro_crew.dashboard.handlers import sessions

    claim = MagicMock(session_key=None, history_key=None)
    keys = sessions._trust_keys_for_delete("dashboard_foo", claim)
    assert "dashboard:foo" in keys


def test_every_refused_or_failed_delete_puts_the_trust_back():
    """Each non-committing branch of both delete paths restores what it took."""
    import inspect

    from kiro_crew.dashboard.handlers import sessions

    single = inspect.getsource(sessions._delete_history_row)
    assert single.count("restore_deleted_session_trust(trust_taken)") >= 3
    bulk = inspect.getsource(sessions._clear_history_rows)
    assert bulk.count("restore_deleted_session_trust(trust_taken)") >= 5


#: Every hard exit or in-place exec in the package, keyed by the INNERMOST function
#: that makes the call, and what it does to saved chat trust. The pin below walks
#: the whole package, so a new site anywhere fails it until it is classified here;
#: a role ending in "clears" must also clear saved trust before it exits.
_NOT_GATEWAY = "another process: never holds saved chat trust"
_GATEWAY_EXIT_SITES = {
    # The gateway's own exits and execs.
    ("slack/gateway.py", "_on_signal"): "owner stop: the first signal marks it",
    ("slack/gateway.py", "_shutdown_and_exit"): "exit 0 clears; non-zero is a self-restart",
    ("slack/gateway.py", "_restart_after_update"): "automatic restart after an outside install",
    ("slack/events.py", "_handle_restart"): "owner restart from Slack: clears",
    ("dashboard/handlers/updates.py", "_restart_gateway"): "owner restart: clears",
    ("platform_compat.py", "reexec_launcher"): "the exec its callers above classify",
    ("platform_compat.py", "reexec_python_module"): "the exec its callers above classify",
    (
        "platform_compat.py",
        "exit_after_failed_restart_exec",
    ): "after a restart exec its caller classified",
    ("service/live_target.py", "maybe_reexec"): "boot handoff before any trust is restored",
    ("pod/runtime_boot.py", "exec_in_pod"): "pod boot before any trust is restored",
    ("pod/runtime_boot.py", "_boot_unguarded"): "pod boot before any trust is restored",
    ("apps/deps_boot.py", "main"): "boot handoff before any trust is restored",
    # Processes other than the gateway.
    ("_process_group_supervisor.py", "_ps_group_members"): _NOT_GATEWAY,
    ("_process_group_supervisor.py", "main"): _NOT_GATEWAY,
    ("_spawn_exec_shim.py", "main"): _NOT_GATEWAY,
    (
        "apps/builtins/aws_control/crew/runtime/container/supervisor/__main__.py",
        "_user_namespaces_available",
    ): _NOT_GATEWAY,
    ("apps/builtins/dev_fleet/skills/pod-e2e/scripts/pod-playwright.py", "_bail_teardown"): (
        _NOT_GATEWAY
    ),
    ("apps/builtins/dev_fleet/skills/pod-e2e/scripts/pod-playwright.py", "_bail"): _NOT_GATEWAY,
    ("cli_config.py", "_run_config_cmd"): _NOT_GATEWAY,
    ("cli_server.py", "_logs_cmd"): _NOT_GATEWAY,
    ("decisions/local_servers/laya_cpu.py", "_exit_when_gateway_lets_go"): _NOT_GATEWAY,
    ("decisions/local_servers/plumb_cpu.py", "_exit_when_gateway_lets_go"): _NOT_GATEWAY,
    ("decisions/local_servers/strands_decider_cpu.py", "_exit_when_gateway_lets_go"): (
        _NOT_GATEWAY
    ),
    ("mcp_gateway/stub.py", "_fallback_spawn_child"): _NOT_GATEWAY,
    ("mcp_gateway/stub.py", "fallback_exec"): _NOT_GATEWAY,
    ("mcp_gateway/stub.py", "_hard_exit"): _NOT_GATEWAY,
    ("sandbox.py", "_probe_child_sequence"): "a forked probe child, not the gateway",
    ("sandbox.py", "_probe_unshare_via_fork"): "a forked probe child, not the gateway",
}

_EXIT_CALLS = frozenset(
    {
        "_exit",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "reexec_launcher",
        "reexec_python_module",
    }
)


def _exit_sites(root) -> set[tuple[str, str]]:
    """(file, innermost enclosing function) for every exit or exec call under *root*."""
    import ast

    found: set[tuple[str, str]] = set()

    def visit(node, rel, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, rel, child.name)
                continue
            if isinstance(child, ast.Call):
                name = getattr(child.func, "attr", getattr(child.func, "id", ""))
                if name in _EXIT_CALLS:
                    found.add((rel, fn))
            visit(child, rel, fn)

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        visit(ast.parse(path.read_text(encoding="utf-8")), rel, "<module>")
    return found


def test_every_gateway_exit_and_exec_site_is_classified_for_session_trust():
    from pathlib import Path as _Path

    import kiro_crew

    root = _Path(kiro_crew.__file__).parent
    found = _exit_sites(root)
    assert found == set(_GATEWAY_EXIT_SITES), {
        "unclassified": sorted(found - set(_GATEWAY_EXIT_SITES)),
        "gone": sorted(set(_GATEWAY_EXIT_SITES) - found),
    }
    for (rel, name), role in _GATEWAY_EXIT_SITES.items():
        if role.endswith("clears"):
            src = (root / rel).read_text(encoding="utf-8")
            body = src[src.index(f"def {name}(") :]
            assert "clear_on_shutdown_async(0)" in body.split("\ndef ", 1)[0], (rel, name)


def test_the_exit_site_scan_sees_a_new_unlisted_site(tmp_path):
    """The pin is two-directional: a site in no listed file is still found."""
    (tmp_path / "fresh.py").write_text(
        "import os\ndef owner_button():\n    def inner():\n        os._exit(0)\n",
        encoding="utf-8",
    )
    assert _exit_sites(tmp_path) == {("fresh.py", "inner")}


@pytest.mark.asyncio
async def test_a_slack_owner_restart_whose_clear_fails_is_refused():
    from kiro_crew.slack import events

    respond = AsyncMock()
    with (
        patch.object(events, "is_owner", return_value=True),
        patch.dict(os.environ, {"INVOCATION_ID": "x"}),
        patch.object(
            chat_trust_persistence, "clear_on_shutdown_async", AsyncMock(return_value=False)
        ),
        patch.object(events.os, "_exit") as hard_exit,
    ):
        await events._handle_restart(MagicMock(), "U1", "", respond)
    hard_exit.assert_not_called()
    assert "Not restarting" in respond.call_args.args[0]


def test_the_force_exit_is_held_once_until_the_stop_is_recorded():
    """Read, never waited on: the hold is a flag the first-signal worker sets."""
    import inspect

    from kiro_crew.slack import gateway

    src = inspect.getsource(gateway)
    hold = src.index("chat_trust_persistence.owner_stop_recorded()")
    assert hold < src.index('print("\\n👻 Force exit!")')
    assert "_force_exit_deferred = True" in src
    chat_trust_persistence._stop_marked.clear()
    assert chat_trust_persistence.owner_stop_recorded() is False
    chat_trust_persistence._signal_stop()
    assert chat_trust_persistence.owner_stop_recorded() is True
    chat_trust_persistence._stop_marked.clear()


@pytest.mark.asyncio
async def test_a_stalled_revoke_still_turns_trust_off_live(tmp_path, monkeypatch):
    """The durable removal is bounded; a timeout is a failed removal, withheld in memory."""
    import threading

    monkeypatch.setattr(chat_trust_persistence, "_REVOKE_TIMEOUT", 0.1)
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._trust = True
    key = effective_session_key(slot)
    assert _grant([key])
    release = threading.Event()

    def stalled(keys):
        release.wait(5.0)
        return True

    with patch.object(store, "revoke", side_effect=stalled):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
        release.set()
    assert resp.status == 500
    assert slot._trust is False
    assert chat_trust_persistence.is_withdrawn(key)


def test_a_channel_row_never_reaches_a_dashboard_session_key():
    """`slack_<ts>` is not a dashboard stem: no `dashboard:` spelling is synthesized."""
    from kiro_crew.dashboard.handlers import sessions

    claim = MagicMock(session_key=None, history_key=None)
    keys = sessions._trust_keys_for_delete("slack_1785370133.085469", claim)
    assert not any(k.startswith("dashboard:") for k in keys)


@pytest.mark.asyncio
async def test_a_grant_during_a_delete_is_not_saved_until_the_delete_is_over():
    """The fence holds the deleted session's keys from before the removal to the end."""
    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    state = MagicMock()
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
    ):
        async with chat_trust_persistence.delete_fence() as fence:
            taken = await chat_trust_persistence.forget_deleted_session(
                state, ("dashboard:s1",), fence
            )
            assert taken.ok
            assert await chat_trust_persistence.persist_grant(request, [slot]) is False
            assert store.load().sessions == ()
        assert chat_trust_persistence._delete_fenced == {}
        assert await chat_trust_persistence.persist_grant(request, [slot]) is True
    assert store.load().sessions == ("dashboard:s1",)


@pytest.mark.asyncio
async def test_a_grant_never_settles_an_owner_stop_aimed_at_this_run(monkeypatch):
    """A marker newer than this process is a stop in progress (Windows CLI, restart)."""
    import os as _os

    assert _grant(["dashboard:old"])
    assert store.mark_owner_stop()
    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
    ):
        monkeypatch.setattr(chat_trust_persistence, "_STARTED", 0.0)
        assert await chat_trust_persistence.persist_grant(request, [slot]) is False
        assert store.owner_stop_marked()
        assert "dashboard:s1" not in store.load().sessions
        # A marker a previous run left (older than this process) is settled.
        monkeypatch.setattr(
            chat_trust_persistence, "_STARTED", _os.lstat(store._marker_path()).st_mtime + 1
        )
        assert await chat_trust_persistence.persist_grant(request, [slot]) is True
    assert not store.owner_stop_marked()
    assert store.load().sessions == ("dashboard:s1",)


@pytest.mark.asyncio
async def test_a_kept_delete_never_restores_a_grant_a_revoke_withdrew():
    """A withdrawn-but-still-on-disk grant is not the session's to get back."""
    state = MagicMock()
    assert _grant(["dashboard:a", "dashboard:b"])
    chat_trust_persistence._withdraw(["dashboard:a"])
    taken = await chat_trust_persistence.forget_deleted_session(state, ("dashboard:a",))
    assert taken.ok and taken.held == ()
    await chat_trust_persistence.restore_deleted_session_trust(taken)
    assert "dashboard:a" not in store.load().sessions


@pytest.mark.asyncio
async def test_a_kept_delete_does_not_restore_after_a_later_revoke_or_owner_stop(monkeypatch):
    state = MagicMock()
    assert _grant(["dashboard:a", "dashboard:b"])
    taken = await chat_trust_persistence.forget_deleted_session(state, ("dashboard:a",))
    assert taken.held == ("dashboard:a",)
    # Another revoke moved the generation: the restore is skipped.
    assert await chat_trust_persistence.persist_revoke_keys(["dashboard:b"])
    await chat_trust_persistence.restore_deleted_session_trust(taken)
    assert store.load().sessions == ()
    # An owner stop in progress: never re-populated either.
    assert _grant(["dashboard:a"])
    taken = await chat_trust_persistence.forget_deleted_session(state, ("dashboard:a",))
    monkeypatch.setattr(chat_trust_persistence, "_stopping", True)
    await chat_trust_persistence.restore_deleted_session_trust(taken)
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_refused_grant_keeps_a_chat_saved_before_it(tmp_path):
    """The roll-back of a superseded all-chats grant removes only what it added."""
    state = _state(tmp_path)
    a = state.get_or_create_slot("a1", origin=SlotOrigin.USER)
    state.get_or_create_slot("b1", origin=SlotOrigin.USER)
    a_key = effective_session_key(a)
    assert _grant([a_key])
    real = chat_trust_persistence.persist_grant

    async def overtaken(request, slots):
        result = await real(request, slots)
        chat_trust_persistence._bump_revoke_generation()
        return result

    with patch.object(chat_trust_persistence, "persist_grant", side_effect=overtaken):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
    assert resp.status == 409
    assert store.load().sessions == (a_key,)


@pytest.mark.asyncio
async def test_a_channel_row_with_no_live_slot_revokes_its_linked_session_key():
    from kiro_crew.dashboard.handlers import sessions

    claim = MagicMock(session_key=None, history_key=None)
    with patch.object(
        sessions, "_linked_session_key_for_history_key", return_value=("slack:1.2", True)
    ):
        keys = await sessions._trust_keys_for_delete_async(MagicMock(), "slack_1.2", claim)
    assert "slack:1.2" in keys


@pytest.mark.asyncio
async def test_a_channel_row_whose_linked_key_was_stripped_still_revokes_its_grant():
    """The signed store, not the agent-writable transcript, names the row's own key."""
    from kiro_crew.dashboard.handlers import sessions

    assert _grant(["slack:1785370133.085469", "dashboard:other"])
    claim = MagicMock(session_key=None, history_key=None)
    with patch.object(sessions, "_linked_session_key_for_history_key", return_value=("", True)):
        keys = await sessions._trust_keys_for_delete_async(
            MagicMock(), "slack_1785370133.085469", claim
        )
    assert "slack:1785370133.085469" in keys
    assert "dashboard:other" not in keys


@pytest.mark.asyncio
async def test_a_grant_never_lifts_a_revoke_that_started_during_its_write():
    """Chat B's revoke lands while chat A's grant is writing: B stays withheld."""
    import asyncio
    import threading

    request = MagicMock()
    slot = MagicMock(_app="", key="a", linked_session_key="")
    entered, release = threading.Event(), threading.Event()
    real = store.grant_counting

    def slow(*args, **kwargs):
        entered.set()
        release.wait(5.0)
        return real(*args, **kwargs)

    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:a"]),
        patch.object(store, "grant_counting", side_effect=slow),
    ):
        grant = asyncio.create_task(chat_trust_persistence.persist_grant(request, [slot]))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        # B's revoke withholds B on the loop before it queues behind the grant.
        revoke = asyncio.create_task(chat_trust_persistence.persist_revoke_keys(["dashboard:b"]))
        await asyncio.sleep(0.05)
        assert chat_trust_persistence.is_withdrawn("dashboard:b")
        release.set()
        assert await grant is True
        assert chat_trust_persistence.is_withdrawn("dashboard:b")
        await revoke


def test_a_restore_during_an_owner_stop_aimed_at_this_run_neither_settles_nor_latches(
    monkeypatch,
):
    """A rehydration inside the CLI's mark-then-kill window must not erase the stop."""
    assert _grant(["dashboard:s1"])
    assert store.mark_owner_stop()
    monkeypatch.setattr(chat_trust_persistence, "_STARTED", 0.0)
    assert chat_trust_persistence._load_for_restore().sessions == ()
    assert store.owner_stop_marked()
    assert store.load().sessions == ("dashboard:s1",)
    assert chat_trust_persistence._owner_stopped is False
    # The stop is refused and withdrawn: the gateway keeps serving and restores.
    assert store.withdraw_owner_stop()
    assert chat_trust_persistence._load_for_restore().sessions == ("dashboard:s1",)
