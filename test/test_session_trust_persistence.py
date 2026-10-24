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
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from session_trust_helpers import (  # noqa: F401  (_hermetic is an autouse fixture)
    _REAL_SIGNING_ROOT,
    APP_TOKEN,
    INTERNAL,
    OWNER,
    _app,
    _grant,
    _hermetic,
    _join_trust_workers,
    _mode,
    _queued_store_writes,
    _restart,
    _state,
    _until,
)

from kiro_crew.dashboard import chat_trust_persistence
from kiro_crew.dashboard import session_trust_store as store
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import async_wait_until

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
    """The route refuses the internal credential; the persistence gate refuses it too."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    async with TestClient(TestServer(_app(state, INTERNAL))) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
    assert resp.status == 403
    assert slot._trust is False
    assert store.load().sessions == ()
    request = MagicMock()
    request.get = INTERNAL.get
    assert chat_trust_persistence.persists_grant(request) is False


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


@pytest.mark.parametrize("identity", [APP_TOKEN])
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

    async def persist_then_rejected_elsewhere(request, slots, **kw):
        answer = await real_persist(request, slots, **kw)
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
    assert clear < src.index("hard_exit(0)")


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

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def stalled() -> bool:
        entered.set()
        release.wait(5.0)
        finished.set()
        return True

    with patch.object(chat_trust_persistence, "clear_on_operator_stop_sync", side_effect=stalled):
        worker = chat_trust_persistence.start_clear_on_signal()
        # Returned while the clear is still parked: no wall-clock ceiling needed.
        returned_while_parked = not finished.is_set()
        assert entered.wait(5.0)
        release.set()
        worker.join(5.0)
    assert returned_while_parked


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

    async def persist_then_rejected_elsewhere(request, slots, **kw):
        answer = await real_persist(request, slots, **kw)
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


@pytest.mark.asyncio
async def test_a_card_answered_during_the_take_back_approves_no_background_tool(tmp_path):
    """A card answered while its overtaken grant is taken back never falls to the
    state-level resolver, which a background approval may share the id with."""
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
    state.resolve_state_approval = MagicMock(return_value=True)
    real_revoke = chat_trust_persistence.persist_revoke_keys

    async def answered_elsewhere_during_take_back(keys):
        fut.set_result("approved")
        return await real_revoke(keys)

    with (
        _superseding_grant(),
        patch.object(
            chat_trust_persistence,
            "persist_revoke_keys",
            side_effect=answered_elsewhere_during_take_back,
        ),
    ):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
            payload = await resp.json()
    assert resp.status == 409
    assert payload["code"] == "approval_already_resolved"
    state.resolve_state_approval.assert_not_called()
    assert slot._trust is False
    assert store.load().sessions == ()


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

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def stalled() -> bool:
        entered.set()
        release.wait(5.0)
        finished.set()
        return True

    with patch.object(chat_trust_persistence, "clear_on_operator_stop_sync", side_effect=stalled):
        chat_trust_persistence.clear_on_force_exit()
        returned_while_parked = not finished.is_set()
        assert entered.wait(5.0)
        release.set()
        _join_force_clear()
    assert returned_while_parked


def test_the_boot_restore_does_not_hold_the_listener():
    """Started as a task, never awaited on the boot path."""
    import inspect

    from kiro_crew.dashboard.server_runtime import session_restore

    src = inspect.getsource(session_restore._restore_dashboard_sessions)
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
        _sign_in_refresher_task=None,
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
        await async_wait_until(revoke_started.is_set)
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            granting = asyncio.create_task(
                client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            )
            # The grant has queued behind the stalled revoke in the write order.
            await _until(lambda: _queued_store_writes() >= 1, "the grant to queue")
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
    assert "elif not _was_marked and session_trust_store.store_path().exists():" in src
    assert "Stopping anyway" in src


def test_a_service_stop_of_a_crash_looping_unit_leaves_the_owner_stop_marker(monkeypatch):
    """A unit in crash backoff stops with no gateway alive to forget its saved trust."""
    from kiro_crew import cli_server

    monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: True)
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 8765)
    monkeypatch.setattr(cli_server, "_stop_mcp_gateway_daemon", lambda: None)
    monkeypatch.setattr(cli_server, "sel", lambda: MagicMock())
    assert not store.owner_stop_marked()
    cli_server._stop()
    assert store.owner_stop_marked()


def _windows_stop(monkeypatch, kill):
    """Run the Windows ``kirocrew stop`` against this home's gateway, killing via *kill*."""
    from kiro_crew import cli_server, platform_compat

    monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: False)
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda _p: 8765)
    monkeypatch.setattr(cli_server, "_stop_mcp_gateway_daemon", lambda: None)
    monkeypatch.setattr(cli_server, "sel", lambda: MagicMock())
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda _port: [4242])
    monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda _pid: True)
    monkeypatch.setattr(cli_server, "_targets_another_homes_gateway", lambda _pids: False)
    monkeypatch.setattr(cli_server, "_pid_exited", lambda _pid: True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(platform_compat, "kill_process_tree", kill)
    # Swap only cli_server's own ``time`` binding: patching the shared stdlib
    # module would stop every thread in the worker from sleeping.
    monkeypatch.setattr(cli_server, "time", _NoSleepTime())
    cli_server._stop(8765)


class _NoSleepTime:
    """A stand-in for ``time`` whose ``sleep`` returns at once; all else is real."""

    @staticmethod
    def sleep(_seconds):
        return None

    def __getattr__(self, name):
        import time

        return getattr(time, name)


def test_a_windows_stop_survives_a_grant_settling_an_older_marker(monkeypatch):
    """A Trust click settling a previous stop's marker mid-stop must not end this one's."""
    assert store.mark_owner_stop()  # a previous stop whose clear failed

    def kill(_pid, _sig):
        # The racing grant settles the older marker (and saves) just before the kill.
        assert store.settle_owner_stop()
        assert not store.owner_stop_marked()

    _windows_stop(monkeypatch, kill)
    assert store.owner_stop_marked()


def test_a_failed_windows_kill_keeps_a_previous_stops_marker(monkeypatch):
    """Only a marker this stop created is taken back when no kill landed."""
    assert store.mark_owner_stop()

    def kill(_pid, _sig):
        raise ProcessLookupError

    with pytest.raises(SystemExit):  # "process already exited"
        _windows_stop(monkeypatch, kill)
    assert store.owner_stop_marked()


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
    make_dir_link(tmp_path / "session-trust-stop", elsewhere)
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
    reached = threading.Event()
    marked_before_clear = {}

    def stalled() -> bool:
        marked_before_clear["value"] = store.owner_stop_marked()
        reached.set()
        release.wait(5.0)
        return True

    with patch.object(chat_trust_persistence, "clear_on_operator_stop_sync", side_effect=stalled):
        worker = chat_trust_persistence.start_clear_on_signal()
        assert reached.wait(5.0)
        assert store.owner_stop_marked()
        release.set()
        worker.join(5.0)
    assert marked_before_clear == {"value": True}


def test_the_force_exit_never_waits_even_on_a_stalled_marker_write():
    """The force exit runs on the event loop: it must not block on the disk at all."""
    import threading

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def stalled_mark() -> bool:
        entered.set()
        release.wait(5.0)
        finished.set()
        return True

    with patch.object(store, "mark_owner_stop", side_effect=stalled_mark):
        chat_trust_persistence.clear_on_force_exit()
        returned_while_parked = not finished.is_set()
        assert entered.wait(5.0)
        release.set()
        _join_force_clear()
    assert returned_while_parked


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


def _removing_grant(state, slot_key):
    """``grant_counting`` that saves, then has *slot_key*'s slot removed (its session deleted)."""
    real_grant = store.grant_counting

    def grant_then_removed(keys, **kw):
        answer = real_grant(keys, **kw)
        state._slots.pop(slot_key, None)
        return answer

    return patch.object(store, "grant_counting", side_effect=grant_then_removed)


@pytest.mark.asyncio
async def test_an_all_chats_grant_whose_chat_is_removed_during_the_save_takes_its_key_back(
    tmp_path,
):
    """Even a key saved before the click goes: a chat recreated under it must not inherit it."""
    state = _state(tmp_path)
    gone = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    state.get_or_create_slot("s2", origin=SlotOrigin.USER)
    gone_key = effective_session_key(gone)
    assert _grant([gone_key])
    with _removing_grant(state, "s1"):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
            payload = await resp.json()
    assert payload["code"] == "trust_superseded"
    assert gone_key not in store.load().sessions


@pytest.mark.asyncio
async def test_a_card_grant_whose_chat_is_removed_during_the_save_takes_its_key_back(tmp_path):
    import asyncio

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    key = effective_session_key(slot)
    assert _grant([key])
    fut = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = fut
    slot.messages.append(
        {
            "role": "permission",
            "content": "Running: ls",
            "cls": json.dumps({"request_id": "req-1", "trust_grantable": "1"}),
        }
    )
    with _removing_grant(state, "s1"):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            await client.post(
                "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
            )
    assert slot._trust is False
    assert key not in store.load().sessions


@pytest.mark.asyncio
async def test_a_stalled_grant_write_degrades_to_live_only():
    """A wedged store must not hang the trust click or the call a card approves."""
    import threading

    release = threading.Event()

    def stalled(*_a, **_kw):
        release.wait(5)
        return True, 0

    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    try:
        with (
            patch.object(chat_trust_persistence, "_GRANT_TIMEOUT", 0.2),
            patch.object(chat_trust_persistence, "persists_grant", return_value=True),
            patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
            patch.object(store, "grant_counting", side_effect=stalled),
        ):
            assert await chat_trust_persistence.persist_grant(request, [slot]) is False
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_grant_queued_during_a_delete_is_not_saved_after_it_commits():
    """The fence is gone by the time a stalled write order lets the save run."""
    import asyncio

    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
    ):
        async with chat_trust_persistence._write_order():
            async with chat_trust_persistence.delete_fence() as fence:
                fence.hold(["dashboard:s1"])
                grant = asyncio.create_task(chat_trust_persistence.persist_grant(request, [slot]))
                await asyncio.sleep(0)
            # The delete committed and released its fence; the write order frees next.
        assert await grant is False
        await asyncio.gather(*list(chat_trust_persistence._pending_writes))
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_stalled_held_keys_read_turns_trust_on_live_only(tmp_path):
    """The read before the save is bounded too: a wedged disk cannot hang the click."""
    import threading

    release = threading.Event()

    def stalled():
        release.wait(5)
        return store.load(), False

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    save = MagicMock()
    try:
        with (
            patch.object(chat_trust_persistence, "_GRANT_TIMEOUT", 0.05),
            patch.object(chat_trust_persistence, "_load_with_stop_marker", side_effect=stalled),
            patch.object(chat_trust_persistence, "persist_grant", save),
        ):
            assert await chat_trust_persistence.saved_keys(["dashboard:s1"]) is None
            await _mode(state, OWNER, {"mode": "trust", "slot": "s1"})
    finally:
        release.set()
    save.assert_not_called()
    assert slot._trust is True


@pytest.mark.asyncio
async def test_a_stalled_held_keys_read_on_the_approval_card_turns_trust_on_live_only(tmp_path):
    """The card path bounds the read too, and then saves nothing and approves the call."""
    import asyncio
    import threading

    release = threading.Event()

    def stalled():
        release.wait(5)
        return store.load(), False

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
    save = MagicMock()
    try:
        with (
            patch.object(chat_trust_persistence, "_GRANT_TIMEOUT", 0.05),
            patch.object(chat_trust_persistence, "_load_with_stop_marker", side_effect=stalled),
            patch.object(chat_trust_persistence, "persist_grant", save),
        ):
            async with TestClient(TestServer(_app(state, OWNER))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/approve", json={"action": "trust", "request_id": "req-1"}
                )
                assert resp.status == 200
    finally:
        release.set()
    save.assert_not_called()
    assert fut.result() == "approved"
    assert slot._trust is True
