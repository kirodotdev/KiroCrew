"""Session trust across a crash, continued: owner stops, CLI restarts, the
exit-site pin, permanent deletes and Session Storage cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from session_trust_helpers import (  # noqa: F401  (_hermetic is an autouse fixture)
    _REAL_HOST_BOOT_ID,
    APP_TOKEN,
    OWNER,
    _app,
    _grant,
    _hermetic,
    _mode,
    _queued_store_writes,
    _state,
    _until,
)

from kiro_crew.dashboard import chat_trust_persistence
from kiro_crew.dashboard import session_trust_store as store
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import async_wait_until, await_until


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
        await async_wait_until(entered.is_set)
        stop = asyncio.create_task(chat_trust_persistence.clear_on_shutdown_async(0))
        # The clear has queued behind the grant still writing.
        await _until(lambda: _queued_store_writes() >= 1, "the clear to queue")
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


@pytest.mark.parametrize("mode", ["normal", "trust_reads"])
@pytest.mark.parametrize("scoped", [False, True], ids=["all-chats", "one-chat"])
@pytest.mark.asyncio
async def test_a_trust_click_saved_during_the_off_switch_teardown_does_not_survive_it(
    tmp_path, mode, scoped
):
    """The off switch removes the record, awaits the scope teardown, then clears
    live trust. A Trust click saved inside that await is cleared live too, so its
    record must not outlive the switch for a crash to restore."""
    from kiro_crew.dashboard import chat_handlers

    state = _state(tmp_path)
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    state.get_or_create_slot("s2", origin=SlotOrigin.USER)
    target = "s1" if scoped else "s2"
    real_teardown = chat_handlers._end_trust_scopes
    fired = {"done": False}
    async with TestClient(TestServer(_app(state, OWNER))) as client:

        async def teardown_with_a_concurrent_grant(slots, audit_caller):
            # Fire once: the off switch's second removal does not tear down again,
            # but a guard keeps any re-entry from recursing into another grant.
            if not fired["done"]:
                fired["done"] = True
                resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": target})
                assert resp.status == 200, await resp.text()
                assert store.load().sessions, "the concurrent grant was not saved"
            await real_teardown(slots, audit_caller)

        body = {"mode": mode, "slot": "s1"} if scoped else {"mode": mode}
        with patch.object(chat_handlers, "_end_trust_scopes", teardown_with_a_concurrent_grant):
            resp = await client.post("/api/chat/mode", json=body)
        assert resp.status == 200, await resp.text()
    assert fired["done"]
    live = state._slots[target]
    assert live._trust is False
    assert store.load().sessions == ()


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
        # The incumbent held the lock before the restart; a replacement holds it after.
        patch.object(cli_server, "_lock_holder_pid", side_effect=[1111, 2222, 2222]),
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


def test_a_failed_cli_restart_keeps_its_mark_once_a_stop_was_delivered():
    """A gateway still shutting down past the timeout will not write the mark again."""
    from kiro_crew import cli_server

    def delivered_then_timed_out(_port):
        cli_server._restart_incumbent["pid"] = 1111
        cli_server._stop_delivery["sent"] = True
        raise SystemExit(1)

    with (
        patch.object(cli_server, "_lock_holder_pid", return_value=1111),
        patch.object(cli_server, "_restart_gateway", side_effect=delivered_then_timed_out),
    ):
        with pytest.raises(SystemExit):
            cli_server._restart(None)
    assert store.owner_stop_marked()


def test_a_service_restart_the_manager_ran_keeps_its_mark(monkeypatch):
    """A unit that did not come back up was still stopped, so the mark stands."""
    from kiro_crew import cli_server

    monkeypatch.setattr(cli_server.run_marker, "read_pid", lambda _port: 4242)
    monkeypatch.setattr(cli_server, "_lock_holder_pid", lambda: 4242)

    from kiro_crew.service.common import RESTART_NOT_UP, RestartReport, ScopeRestart

    report = RestartReport(
        outcomes=(
            ScopeRestart(scope="user", ok=False, reason="not up", kind=RESTART_NOT_UP, hint="x"),
        )
    )
    monkeypatch.setattr(cli_server.service_controller, "restart_service", lambda: report)
    monkeypatch.setattr(cli_server.service_controller, "is_service_active", lambda: True)
    with pytest.raises(SystemExit):
        cli_server._restart(None)
    assert store.owner_stop_marked()


def test_the_listener_restart_records_a_delivered_stop():
    """`_stop` returning, or an acknowledged shutdown, counts as a delivered stop."""
    import inspect

    from kiro_crew import cli_server

    restart_src = inspect.getsource(cli_server._restart_gateway)
    assert 'stop_returned = True\n            _stop_delivery["sent"] = True' in restart_src
    assert 'elif _report_authenticated_shutdown(port):\n        _stop_delivery["sent"] = True' in (
        restart_src
    )
    assert '_stop_delivery["sent"] = True' in inspect.getsource(cli_server._stop)


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
    assert taken.ok
    assert store.load().sessions == ("dashboard:other",)
    state.notify.assert_not_called()


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

    for handler in (sessions._delete_claimed_session, sessions._clear_history_rows):
        assert "chat_trust_persistence.forget_deleted_session(" in inspect.getsource(handler)


def test_no_delete_path_writes_removed_trust_back():
    """A refused or failed delete leaves the saved grant removed: the kept chat asks again."""
    import inspect

    from kiro_crew.dashboard.handlers import session_storage, sessions

    for handler in (
        sessions._delete_claimed_session,
        sessions._clear_history_rows,
        session_storage.api_session_storage_cleanup,
    ):
        source = inspect.getsource(handler)
        for writer in ("persist_grant", "grant_counting", "session_trust_store.grant"):
            assert writer not in source, (handler.__name__, writer)


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
    elsewhere.mkdir()
    # Windows junctions need an existing target, so a dangling link is made by
    # removing the target after linking.
    make_dir_link(tmp_path / "session-trust-stop", elsewhere)
    if not target_exists:
        elsewhere.rmdir()
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
        await async_wait_until(entered.is_set)
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


def test_a_stop_marked_while_an_old_stop_settles_keeps_its_marker():
    """The marker is written under the settle lock, so it cannot land between a running
    settle's age check and its removal, where that settle would remove it."""
    held: list[bool] = []
    real_write = store.atomic_write

    def spy(*args, **kwargs):
        held.append(store._SETTLE_LOCK.locked())
        return real_write(*args, **kwargs)

    with patch.object(store, "atomic_write", side_effect=spy):
        assert store.mark_owner_stop()
    assert held == [True]
    assert not store._SETTLE_LOCK.locked()
    assert store.settle_owner_stop()  # the settle still takes the lock it shares


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

    for handler in (sessions._delete_claimed_session, sessions._clear_history_rows):
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


#: Every hard exit or in-place exec in the package, keyed by the INNERMOST function
#: that makes the call, and what it does to saved chat trust. The pin below walks
#: the whole package, so a new site anywhere fails it until it is classified here;
#: a role ending in "clears" must also clear saved trust before it exits.
_NOT_GATEWAY = "another process: never holds saved chat trust"
_GATEWAY_EXIT_SITES = {
    # The gateway's own exits and execs.
    ("slack/gateway_runtime/exit_path.py", "_on_signal"): "owner stop: the first signal marks it",
    (
        "slack/gateway_runtime/exit_path.py",
        "_shutdown_and_exit",
    ): "exit 0 clears; non-zero is a self-restart",
    (
        "slack/gateway_runtime/update_schedule.py",
        "_restart_after_update_claimed",
    ): "automatic restart after an outside install",
    ("slack/events_slash.py", "_handle_restart"): "owner restart from Slack: clears",
    ("dashboard/handlers/updates_apply.py", "_restart_gateway"): "owner restart: clears",
    ("platform_compat_runtime.py", "reexec_launcher"): "the exec its callers above classify",
    ("platform_compat_runtime.py", "reexec_python_module"): "the exec its callers above classify",
    ("platform_compat_runtime.py", "hard_exit"): "the exit its callers above classify",
    (
        "platform_compat_runtime.py",
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
    (
        "apps/builtins/aws_control/crew/runtime/container/microvm/hooks.py",
        "on_signal",
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
    ("mcp_gateway/stub_fallback.py", "_fallback_spawn_child"): _NOT_GATEWAY,
    ("mcp_gateway/stub_fallback.py", "fallback_exec"): _NOT_GATEWAY,
    ("mcp_gateway/stub.py", "_hard_exit"): _NOT_GATEWAY,
    ("mcp_shared.py", "_hard_exit_on_signal"): _NOT_GATEWAY,
    ("sandbox_probe.py", "_probe_child_sequence"): "a forked probe child, not the gateway",
    ("sandbox_probe.py", "_probe_unshare_via_fork"): "a forked probe child, not the gateway",
    (
        "sandbox_launcher_parts/exec_lockdown.py",
        "exec_agent",
    ): "the sandbox launcher child, not the gateway",
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
        "hard_exit",
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

    from kiro_crew.slack.gateway_runtime import exit_path

    src = inspect.getsource(exit_path)
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
async def test_a_row_deleted_by_its_stem_fences_its_session_key():
    """A row deleted as ``dashboard_<n>`` fences the ``dashboard:<n>`` key a grant saves."""
    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
    ):
        async with chat_trust_persistence.delete_fence() as fence:
            fence.hold(["dashboard_s1"])
            assert await chat_trust_persistence.persist_grant(request, [slot]) is False
        assert store.load().sessions == ()
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
async def test_a_refused_grant_keeps_a_chat_saved_before_it(tmp_path):
    """The roll-back of a superseded all-chats grant removes only what it added."""
    state = _state(tmp_path)
    a = state.get_or_create_slot("a1", origin=SlotOrigin.USER)
    state.get_or_create_slot("b1", origin=SlotOrigin.USER)
    a_key = effective_session_key(a)
    assert _grant([a_key])
    real = chat_trust_persistence.persist_grant

    async def overtaken(request, slots, **kw):
        result = await real(request, slots, **kw)
        chat_trust_persistence._bump_revoke_generation()
        return result

    with patch.object(chat_trust_persistence, "persist_grant", side_effect=overtaken):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust"})
    assert resp.status == 409
    assert store.load().sessions == (a_key,)


@pytest.mark.asyncio
async def test_a_grant_never_lifts_a_revoke_that_started_during_its_write():
    """Chat B's revoke lands while chat A's grant is writing: B stays withheld."""
    import asyncio
    import threading

    request = MagicMock()
    slot = MagicMock(_app="", key="a", linked_session_key="")
    entered, release = threading.Event(), threading.Event()
    # B's own store write stays parked until both assertions have run, so a
    # revoke that completes (and correctly lifts B) cannot race them.
    revoke_release = threading.Event()
    real = store.grant_counting
    real_revoke = store.revoke

    def slow(*args, **kwargs):
        entered.set()
        release.wait(5.0)
        return real(*args, **kwargs)

    def parked_revoke(*args, **kwargs):
        revoke_release.wait(5.0)
        return real_revoke(*args, **kwargs)

    with (
        patch.object(chat_trust_persistence, "persists_grant", return_value=True),
        patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:a"]),
        patch.object(store, "grant_counting", side_effect=slow),
        patch.object(store, "revoke", side_effect=parked_revoke),
    ):
        grant = asyncio.create_task(chat_trust_persistence.persist_grant(request, [slot]))
        assert await async_wait_until(entered.is_set)
        # B's revoke withholds B on the loop before it queues behind the grant.
        revoke = asyncio.create_task(chat_trust_persistence.persist_revoke_keys(["dashboard:b"]))
        for _ in range(100):
            if chat_trust_persistence.is_withdrawn("dashboard:b"):
                break
            await asyncio.sleep(0)
        assert chat_trust_persistence.is_withdrawn("dashboard:b")
        release.set()
        assert await grant is True
        assert chat_trust_persistence.is_withdrawn("dashboard:b")
        revoke_release.set()
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


@pytest.mark.asyncio
async def test_a_chat_whose_transcript_link_names_another_chat_gets_no_trust(tmp_path):
    """A link rewritten in a transcript must not carry another chat's saved trust."""
    trusted = _state(tmp_path).get_or_create_slot("s1")
    assert _grant([effective_session_key(trusted)])

    fresh = _state(tmp_path)
    forged = fresh.get_or_create_slot("s2")
    forged.linked_session_key = effective_session_key(trusted)

    assert await chat_trust_persistence.restore_persisted_trust_async(fresh) == 0
    assert forged._trust is False
    assert chat_trust_persistence.apply_restored_trust(fresh, forged) is False
    assert forged._trust is False


@pytest.mark.asyncio
async def test_a_channel_chat_never_gets_trust_back_even_if_its_key_is_stored(tmp_path):
    """A channel key on disk (written by hand, or an older build) restores nothing."""
    assert _grant(["slack:123.456"])

    fresh = _state(tmp_path)
    slot = fresh.get_or_create_slot("slack_123.456")
    slot.linked_session_key = "slack:123.456"

    assert await chat_trust_persistence.restore_persisted_trust_async(fresh) == 0
    assert slot._trust is False


@pytest.mark.asyncio
async def test_an_app_trust_grant_reaches_the_live_grant_without_reading_the_store(tmp_path):
    """No await between an app's authorization and its live grant."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    with (
        patch("kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True),
        patch.object(
            chat_trust_persistence, "saved_keys", side_effect=AssertionError("store read")
        ),
    ):
        await _mode(state, APP_TOKEN, {"mode": "trust", "slot": "s1"})
    assert slot._trust is True


@pytest.mark.asyncio
async def test_a_timed_out_card_save_that_lands_later_is_taken_back(tmp_path):
    """A refused card grant whose save outlived its bound still gets removed."""
    import asyncio
    import threading

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
    entered = threading.Event()
    release = threading.Event()
    landed = threading.Event()
    timed_out = asyncio.Event()
    real_grant = store.grant_counting

    def stalled(keys, **kw):
        entered.set()
        release.wait(5)
        try:
            return real_grant(keys, **kw)
        finally:
            landed.set()

    real_persist = chat_trust_persistence.persist_grant

    async def persist_then_rejected_elsewhere(request, slots, **kw):
        answer = await real_persist(request, slots, **kw)
        # The bounded wait gave up while the write is still parked.
        timed_out.set()
        fut.set_result("rejected")
        return answer

    try:
        with (
            patch.object(chat_trust_persistence, "_GRANT_TIMEOUT", 0.2),
            patch.object(store, "grant_counting", side_effect=stalled),
            patch.object(
                chat_trust_persistence, "persist_grant", side_effect=persist_then_rejected_elsewhere
            ),
        ):
            async with TestClient(TestServer(_app(state, OWNER))) as client:
                pending = asyncio.ensure_future(
                    client.post(
                        "/api/chat/slots/s1/approve",
                        json={"action": "trust", "request_id": "req-1"},
                    )
                )
                assert await async_wait_until(entered.is_set)
                await await_until(timed_out, what="the grant save timed out")
                assert not landed.is_set()
                release.set()
                resp = await pending
                assert resp.status == 409
            # The late write has landed, and every take-back queued behind it ran.
            assert await async_wait_until(landed.is_set)
            await asyncio.gather(*list(chat_trust_persistence._pending_writes))
    finally:
        release.set()
    assert slot._trust is False
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_revoke_between_the_held_read_and_the_save_is_not_undone(tmp_path):
    """A refused Trust must not write back a saved grant a racing Normal removed."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    key = effective_session_key(slot)
    assert _grant([key])
    real_saved = chat_trust_persistence.saved_keys

    async def held_then_revoked(keys):
        held = await real_saved(keys)
        assert await chat_trust_persistence.persist_revoke_keys([key])
        return held

    with patch.object(chat_trust_persistence, "saved_keys", side_effect=held_then_revoked):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
    assert resp.status == 409
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_withdrawn_record_is_not_held_for_a_refused_grants_take_back():
    """A record a failed revoke left on disk must not shield a refused grant's key."""
    key = "dashboard:s1"
    assert _grant([key])
    chat_trust_persistence._withdraw([key])
    assert await chat_trust_persistence.saved_keys([key]) == frozenset()


@pytest.mark.asyncio
async def test_a_held_read_overtaken_by_a_revoke_holds_nothing():
    """A revoke that starts while the held read is off-loop makes that read stale."""
    key = "dashboard:s1"
    assert _grant([key])
    real_load = store.load

    def load_then_revoke_starts():
        snapshot = real_load()
        chat_trust_persistence._bump_revoke_generation()
        return snapshot

    with patch.object(store, "load", side_effect=load_then_revoke_starts):
        assert await chat_trust_persistence.saved_keys([key]) == frozenset()
    assert await chat_trust_persistence.saved_keys([key]) == frozenset({key})


@pytest.mark.parametrize("claim_names_the_sibling", [False, True])
def test_a_stacked_row_never_reaches_its_siblings_session_key(claim_names_the_sibling):
    """``dashboard_dashboard_<n>`` is another file than ``dashboard_<n>``."""
    from kiro_crew.dashboard.handlers import sessions

    sibling = "dashboard:chat-3-300"
    claim = MagicMock(
        session_key=sibling if claim_names_the_sibling else None,
        history_key=sibling if claim_names_the_sibling else None,
        path_match_verified=False,
    )
    keys = sessions._trust_keys_for_delete("dashboard_dashboard_chat-3-300", claim)
    assert sibling not in keys
    assert keys == ()


@pytest.mark.asyncio
async def test_a_rebind_before_the_save_leaves_nothing_durable(tmp_path):
    """The save writes the keys captured with the click, so the take-back removes them."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    real_saved = chat_trust_persistence.saved_keys

    async def held_then_rebound(keys):
        held = await real_saved(keys)
        slot.linked_session_key = "slack:999.000"
        return held

    with patch.object(chat_trust_persistence, "saved_keys", side_effect=held_then_rebound):
        async with TestClient(TestServer(_app(state, OWNER))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
    assert resp.status == 409
    assert store.load().sessions == ()


def test_a_junction_store_directory_is_never_written_through(tmp_path):
    """A Windows junction lstat()s as a directory; it is refused by name."""
    real = store.platform_compat.is_link_or_junction
    store_dir = store._path_override.parent

    def junction(path):
        return Path(path) == store_dir or real(path)

    with patch.object(store.platform_compat, "is_link_or_junction", side_effect=junction):
        assert _grant(["dashboard:s1"]) is False
    assert not (store_dir / "grants.json").exists()


def test_a_junction_marker_directory_reads_as_marked(tmp_path):
    marker_dir = store._marker_path().parent
    marker_dir.mkdir(parents=True, exist_ok=True)
    real = store.platform_compat.is_link_or_junction

    def junction(path):
        return Path(path) == marker_dir or real(path)

    with patch.object(store.platform_compat, "is_link_or_junction", side_effect=junction):
        assert store.owner_stop_marked() is True


def test_the_record_is_read_through_the_no_reparse_open():
    """Windows has no O_NOFOLLOW: the record's own name must be opened without following."""
    import errno

    assert _grant(["dashboard:s1"])
    refused = OSError(errno.ELOOP, "reparse point at the record")
    with patch.object(
        store.platform_compat, "open_file_no_reparse", side_effect=refused
    ) as no_reparse:
        assert store.load().sessions == ()
    no_reparse.assert_called_once()
    assert no_reparse.call_args.kwargs.get("links_only") is True


def test_a_deeply_nested_record_is_unreadable_and_a_fresh_grant_replaces_it():
    """A planted record nested past the recursion limit must not 500 the Trust click."""
    path = store._path_override
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    assert store.load().readable is False
    assert _grant(["dashboard:s1"])
    assert store.load().holds("dashboard:s1")


@pytest.mark.asyncio
async def test_a_timed_out_revoke_all_still_clears_once_the_write_order_reaches_it():
    """The bound must not cancel the clear: a crash would otherwise restore every chat."""
    import asyncio
    import threading

    assert _grant(["dashboard:old"])
    entered, release = threading.Event(), threading.Event()
    real = store.grant_counting

    def stalled(*args, **kwargs):
        entered.set()
        release.wait(5.0)
        return real(*args, **kwargs)

    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    try:
        with (
            patch.object(chat_trust_persistence, "_GRANT_TIMEOUT", 0.05),
            patch.object(chat_trust_persistence, "_REVOKE_TIMEOUT", 0.05),
            patch.object(chat_trust_persistence, "persists_grant", return_value=True),
            patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
            patch.object(store, "grant_counting", side_effect=stalled),
        ):
            grant = asyncio.create_task(chat_trust_persistence.persist_grant(request, [slot]))
            assert await async_wait_until(entered.is_set)
            # The clear times out while the stalled grant holds the write order.
            assert await chat_trust_persistence.persist_revoke_all() is False
            release.set()
            await grant
            await await_until(
                asyncio.gather(*list(chat_trust_persistence._pending_writes)),
                what="the queued store writes",
            )
    finally:
        release.set()
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_an_off_switch_takes_back_a_grant_whose_save_timed_out_during_it():
    """A grant queued between the off switch's two removals counts even when its
    save outlives the bound: the second removal queues behind it, so the late
    write cannot leave a record a crash would restore."""
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()
    real = store.grant_counting

    def stalled(*args, **kwargs):
        entered.set()
        release.wait(5.0)
        return real(*args, **kwargs)

    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    try:
        with (
            patch.object(chat_trust_persistence, "_GRANT_TIMEOUT", 0.05),
            patch.object(chat_trust_persistence, "_REVOKE_TIMEOUT", 0.05),
            patch.object(chat_trust_persistence, "persists_grant", return_value=True),
            patch.object(chat_trust_persistence, "session_keys", return_value=["dashboard:s1"]),
            patch.object(store, "grant_counting", side_effect=stalled),
        ):
            # The all-chats Normal switch: count, first removal, then (during its
            # scope teardown) the owner clicks Trust and that save stalls.
            since = chat_trust_persistence.grant_count()
            assert await chat_trust_persistence.persist_revoke_all() is True
            assert await chat_trust_persistence.persist_grant(request, [slot]) is False
            assert await async_wait_until(entered.is_set)
            await chat_trust_persistence.revoke_again_if_granted(since)
            release.set()
            await await_until(
                asyncio.gather(*list(chat_trust_persistence._pending_writes)),
                what="the queued store writes",
            )
    finally:
        release.set()
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_an_unbound_channel_chat_trust_stays_live_only(tmp_path):
    """Its dashboard: fallback key names no transcript of its own, so a delete could not find it."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("slack_1785370133.085469", origin=SlotOrigin.USER)
    slot.channel_origin = True
    slot.linked_session_key = ""
    await _mode(state, OWNER, {"mode": "trust", "slot": slot.key})
    assert slot._trust is True
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_linked_channel_chat_trust_is_live_only(tmp_path):
    """Only dashboard chats save trust: a Slack chat's grant applies but is not saved."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("slack_1785370133.085469", origin=SlotOrigin.USER)
    slot.channel_origin = True
    slot.linked_session_key = "slack:1785370133.085469"
    await _mode(state, OWNER, {"mode": "trust", "slot": slot.key})
    assert slot._trust is True
    assert store.load().sessions == ()


@pytest.mark.asyncio
async def test_a_channel_key_is_never_saved_even_when_named_directly():
    """The key gate holds when a caller passes keys itself (a Discord DM's several colons)."""
    request = MagicMock()
    slot = MagicMock(_app="", key="s1", linked_session_key="")
    with patch.object(chat_trust_persistence, "persists_grant", return_value=True):
        result = await chat_trust_persistence.persist_grant(
            request, [slot], keys=["discord:assistant:direct:123", "slack:123.456"]
        )
    assert result is None
    assert store.load().sessions == ()


@pytest.mark.parametrize(
    ("key", "saveable"),
    [
        ("dashboard:chat-3-300", True),
        ("dashboard:s1", True),
        ("slack:123.456", False),
        ("discord:assistant:direct:123", False),
        ("dashboard:a:b", False),
        ("dashboard:", False),
        ("dashboard_s1", False),
    ],
)
def test_only_a_dashboard_chats_own_key_is_saveable(key, saveable):
    assert chat_trust_persistence.is_saveable_key(key) is saveable


def test_deleting_a_channel_row_names_no_trust_key():
    """A channel row never saved trust, so its delete has nothing to remove or read."""
    from kiro_crew.dashboard.handlers import sessions

    claim = MagicMock(
        session_key="discord:assistant:direct:123",
        history_key="discord:assistant:direct:123",
        path_match_verified=True,
    )
    assert sessions._trust_keys_for_delete("discord_assistant_direct_123", claim) == ()
    assert sessions._trust_keys_for_delete("slack_123.456", claim) == ()


@pytest.mark.asyncio
async def test_a_revoke_that_finishes_during_the_restore_read_still_wins(tmp_path):
    """The revoke started before the read, so its generation is unchanged; its lift is not."""
    import asyncio
    import threading

    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    key = effective_session_key(slot)
    assert _grant([key])
    release = threading.Event()
    real_revoke = store.revoke

    def parked_revoke(keys):
        release.wait(5.0)
        return real_revoke(keys)

    real_load = chat_trust_persistence._load_for_restore

    def read_old_then_let_the_revoke_finish():
        snapshot = real_load()  # still holds the grant
        release.set()
        for _ in range(500):  # bounded wait for the revoke to land and lift
            if not chat_trust_persistence.is_withdrawn(key):
                break
            threading.Event().wait(0.01)  # flake-ok: tick of a bounded poll
        return snapshot

    with patch.object(store, "revoke", side_effect=parked_revoke):
        revoking = asyncio.create_task(chat_trust_persistence.persist_revoke_keys([key]))
        await _until(lambda: chat_trust_persistence.is_withdrawn(key), "the revoke to withhold")
        calls = {"n": 0}

        def first_read_races():
            calls["n"] += 1
            if calls["n"] == 1:
                return read_old_then_let_the_revoke_finish()
            return real_load()

        with patch.object(
            chat_trust_persistence, "_load_for_restore", side_effect=first_read_races
        ):
            restored = await chat_trust_persistence.restore_persisted_trust_async(state)
        assert await revoking is True
    assert restored == 0
    assert slot._trust is False


@pytest.mark.asyncio
async def test_a_second_concurrent_card_trust_never_takes_back_the_winners_grant(tmp_path):
    """Two Trust clicks on one card: the second is refused before it reads or writes."""
    import asyncio
    import threading

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
    entered, release = threading.Event(), threading.Event()
    real_grant = store.grant_counting

    def parked(keys, **kw):
        entered.set()
        release.wait(5.0)
        return real_grant(keys, **kw)

    body = {"action": "trust", "request_id": "req-1"}
    try:
        with patch.object(store, "grant_counting", side_effect=parked):
            async with TestClient(TestServer(_app(state, OWNER))) as client:
                first = asyncio.ensure_future(client.post("/api/chat/slots/s1/approve", json=body))
                assert await async_wait_until(entered.is_set)
                second = await client.post("/api/chat/slots/s1/approve", json=body)
                assert second.status == 409
                release.set()
                won = await first
                assert won.status == 200
    finally:
        release.set()
    assert slot._trust is True
    assert store.load().holds(effective_session_key(slot))


def test_a_refused_wrong_port_restart_takes_back_its_mark_while_this_homes_gateway_serves():
    """No run marker on the targeted port, no stop delivered: this home's gateway keeps serving."""
    from kiro_crew import cli_server

    def refused(_port):
        # Aimed at an unused port: nothing there, nothing stopped.
        cli_server._restart_incumbent["pid"] = None
        raise SystemExit(1)

    with (
        patch.object(cli_server, "_lock_holder_pid", return_value=4242),
        patch.object(cli_server, "_restart_gateway", side_effect=refused),
    ):
        with pytest.raises(SystemExit):
            cli_server._restart(5999)
    assert not store.owner_stop_marked()


@pytest.mark.asyncio
async def test_a_mode_trust_during_a_card_trust_save_never_erases_the_saved_grant(tmp_path):
    """The two Trust controls claim the same chat: the second is refused before it reads."""
    import asyncio
    import threading

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
    entered, release = threading.Event(), threading.Event()
    real_grant = store.grant_counting

    def parked(keys, **kw):
        entered.set()
        release.wait(5.0)
        return real_grant(keys, **kw)

    try:
        with patch.object(store, "grant_counting", side_effect=parked):
            async with TestClient(TestServer(_app(state, OWNER))) as client:
                card = asyncio.ensure_future(
                    client.post(
                        "/api/chat/slots/s1/approve",
                        json={"action": "trust", "request_id": "req-1"},
                    )
                )
                assert await async_wait_until(entered.is_set)
                mode = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
                assert mode.status == 409
                assert (await mode.json())["code"] == "trust_save_in_progress"
                release.set()
                assert (await card).status == 200
    finally:
        release.set()
    assert slot._trust is True
    assert store.load().holds(effective_session_key(slot))


def test_a_dashboard_grant_under_a_channel_shaped_name_is_not_restored(tmp_path):
    """No link, but the name restores the channel transcript: the grant names another chat."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("slack_1785370133.085469", origin=SlotOrigin.USER)
    slot.linked_session_key = ""
    assert chat_trust_persistence.apply_restored_trust(state, slot) is False
    assert slot._trust is False


def test_a_plain_dashboard_chat_with_no_link_still_restores(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.linked_session_key = ""
    assert chat_trust_persistence.apply_restored_trust(state, slot) is True


def test_a_failed_restart_that_stopped_another_homes_gateway_takes_back_its_mark():
    """The signals reached a different gateway; this home's own one kept serving."""
    from kiro_crew import cli_server

    def stopped_elsewhere(_port):
        cli_server._stop_delivery["sent"] = True
        cli_server._stopped_pids[:] = [9999]
        raise SystemExit(1)

    with (
        patch.object(cli_server, "_lock_holder_pid", return_value=4242),
        patch.object(cli_server, "_restart_gateway", side_effect=stopped_elsewhere),
    ):
        with pytest.raises(SystemExit):
            cli_server._restart(5999)
    assert not store.owner_stop_marked()


def test_a_failed_restart_that_stopped_this_homes_gateway_keeps_its_mark():
    from kiro_crew import cli_server

    def stopped_here(_port):
        cli_server._stop_delivery["sent"] = True
        cli_server._stopped_pids[:] = [4242]
        raise SystemExit(1)

    with (
        patch.object(cli_server, "_lock_holder_pid", return_value=4242),
        patch.object(cli_server, "_restart_gateway", side_effect=stopped_here),
    ):
        with pytest.raises(SystemExit):
            cli_server._restart(None)
    assert store.owner_stop_marked()


def test_a_grant_saved_before_a_reboot_restores_nothing(monkeypatch):
    """A crash leaves no stop mark; a reboot afterwards must still withdraw trust."""
    assert store.grant_counting(["dashboard:s1"])[0]
    assert store.load().holds("dashboard:s1")  # same boot: crash recovery holds
    monkeypatch.setattr(store.platform_compat, "host_boot_id", lambda: "boot-2")
    snap = store.load()
    assert not snap.holds("dashboard:s1")
    assert snap.sessions == ()
    # Expired, not unreadable: the owner is not told their trust could not be read.
    assert snap.readable


def test_an_empty_record_from_before_a_reboot_reads_as_expired(monkeypatch):
    """A revoke leaves a signed empty record; after a reboot it is no read failure."""
    assert store.grant_counting(["dashboard:s1"])[0]
    assert store.clear()
    monkeypatch.setattr(store.platform_compat, "host_boot_id", lambda: "boot-2")
    snap = store.load()
    assert snap.readable
    assert snap.sessions == ()


def test_no_readable_boot_identity_saves_no_trust(monkeypatch):
    monkeypatch.setattr(store.platform_compat, "host_boot_id", lambda: None)
    assert not store.grant_counting(["dashboard:s1"])[0]
    assert not store.load().holds("dashboard:s1")


def test_the_host_boot_identity_is_stable_within_this_boot():
    first = _REAL_HOST_BOOT_ID()
    assert first is None or first == _REAL_HOST_BOOT_ID()


@pytest.mark.parametrize(("value", "expected"), [(7, "windows:7"), ("7", None)])
def test_the_windows_boot_identity_is_the_kernel_boot_counter(monkeypatch, value, expected):
    """Reboot-unique or nothing: no wall-clock stand-in that two boots could share."""
    import contextlib
    import sys
    import types

    from kiro_crew import platform_compat

    opened: list[str] = []

    @contextlib.contextmanager
    def open_key(_hive, path):
        opened.append(path)
        yield object()

    fake = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=open_key,
        QueryValueEx=lambda _key, name: (value if name == "BootId" else None, 4),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(platform_compat.sys, "platform", "win32")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert _REAL_HOST_BOOT_ID() == expected
    assert opened and opened[0].endswith("PrefetchParameters")


def test_an_unreadable_windows_boot_counter_is_no_identity(monkeypatch):
    import sys
    import types

    from kiro_crew import platform_compat

    def refuse(*_a):
        raise OSError("no such key")

    monkeypatch.setitem(
        sys.modules, "winreg", types.SimpleNamespace(HKEY_LOCAL_MACHINE=0, OpenKey=refuse)
    )
    monkeypatch.setattr(platform_compat.sys, "platform", "win32")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert _REAL_HOST_BOOT_ID() is None


def test_a_signal_during_boot_marks_the_owner_stop_and_still_ends_the_process(monkeypatch):
    """A SIGTERM or Ctrl-C before the gateway's handlers exist is still an owner stop.

    The handler only resets both signals and starts a worker; the worker writes the
    marker and then re-sends the signal. Nothing is written or waited on the thread
    the signal interrupted.
    """
    import inspect
    import signal

    from kiro_crew import cli_server

    installed: dict[int, object] = {}
    started: list[tuple[object, tuple]] = []
    kills: list[int] = []

    class _Recorded:
        def __init__(self, *, target, args=(), **_kw):
            self.target, self.args = target, args

        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))
    monkeypatch.setattr(signal, "raise_signal", kills.append)

    chat_trust_persistence.install_early_stop_marker()
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    early = installed[signal.SIGTERM]

    with monkeypatch.context() as m:
        m.setattr(chat_trust_persistence.threading, "Thread", _Recorded)
        early(signal.SIGTERM, None)
    assert installed == {signal.SIGINT: signal.SIG_DFL, signal.SIGTERM: signal.SIG_DFL}
    assert started == [(chat_trust_persistence._finish_early_stop, (signal.SIGTERM,))]
    assert not store.owner_stop_marked() and kills == []

    chat_trust_persistence._finish_early_stop(signal.SIGTERM)
    assert store.owner_stop_marked()
    assert kills == [signal.SIGTERM]

    # Installed before the first await of boot work, right after the gateway lock.
    gateway_src = inspect.getsource(cli_server._gateway)
    assert gateway_src.index("install_early_stop_marker()") < gateway_src.index("await ")


def test_a_signal_during_boot_on_a_stalled_disk_still_ends_the_process(monkeypatch):
    """A marker write that never returns holds the stop only for the bound."""
    import signal
    import threading

    release = threading.Event()
    joins: list[float | None] = []
    kills: list[int] = []

    class _JoinSpy(threading.Thread):
        def join(self, timeout=None):
            joins.append(timeout)
            super().join(0)

    monkeypatch.setattr(store, "mark_owner_stop", lambda: release.wait(30))
    monkeypatch.setattr(chat_trust_persistence.threading, "Thread", _JoinSpy)
    monkeypatch.setattr(signal, "raise_signal", kills.append)
    try:
        chat_trust_persistence._finish_early_stop(signal.SIGTERM)
        assert joins == [chat_trust_persistence.EARLY_STOP_MARK_TIMEOUT]
        assert kills == [signal.SIGTERM]
    finally:
        release.set()


def _cleanup_request(monkeypatch, stems, *, move=None):
    """A Session Storage cleanup that selects one old unit with *stems*."""
    from aiohttp.test_utils import make_mocked_request
    from body_stream_helpers import BodyStreamPayload

    from kiro_crew.dashboard.handlers import session_storage as handler
    from kiro_crew.session_storage import SessionUnit, TrashBatch

    unit = SessionUnit(uid="u1", sid="s1", stems=tuple(stems), bytes=10, mtime=0.0, active=False)
    moved: list[list[str]] = []

    def fake_move(uids, **_kw):
        moved.append(list(uids))
        if move is not None:
            move()
        return TrashBatch(batch_id="b1", created_at=0.0, reason="policy", sessions=1, bytes=10)

    monkeypatch.setattr(handler, "_build_index", lambda: object())
    monkeypatch.setattr(handler, "select_reclaimable", lambda _i, _t: [unit])
    monkeypatch.setattr(handler, "move_to_trash", fake_move)
    monkeypatch.setattr(handler, "_activation", lambda _s: None)
    monkeypatch.setattr(handler, "_MapBackedRefresh", lambda: None)
    monkeypatch.setattr(handler, "_sel", lambda: MagicMock())
    raw = json.dumps({"older_than_days": 30}).encode()
    req = make_mocked_request(
        "POST",
        "/api/system/session-storage/cleanup",
        headers={
            "X-Session-Key": "dashboard:ui",
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
        },
        payload=BodyStreamPayload(raw),
    )
    state = MagicMock()
    state._restricted_keys = set()
    req.app["state"] = state
    return handler, req, moved


@pytest.mark.asyncio
async def test_session_storage_cleanup_removes_the_saved_trust_of_what_it_moves(monkeypatch):
    """A bulk cleanup frees transcript names, so their saved trust goes first."""
    _grant(["dashboard:old", "dashboard:kept"])
    handler, req, moved = _cleanup_request(monkeypatch, ["dashboard_old"])

    resp = await handler.api_session_storage_cleanup(req)

    assert resp.status == 200 and moved == [["u1"]]
    snapshot = store.load()
    assert not snapshot.holds("dashboard:old")
    assert snapshot.holds("dashboard:kept")


@pytest.mark.asyncio
async def test_session_storage_cleanup_moves_nothing_when_trust_cannot_be_removed(monkeypatch):
    _grant(["dashboard:old"])
    handler, req, moved = _cleanup_request(monkeypatch, ["dashboard_old"])
    monkeypatch.setattr(
        chat_trust_persistence,
        "forget_deleted_session",
        AsyncMock(return_value=chat_trust_persistence.DeletedTrust(ok=False)),
    )

    resp = await handler.api_session_storage_cleanup(req)

    assert resp.status == 409 and json.loads(resp.body)["code"] == "trust_revoke_not_durable"
    assert moved == []


@pytest.mark.asyncio
async def test_a_failed_session_storage_cleanup_keeps_the_trust_removed(monkeypatch):
    """A move that fails may have moved some transcripts, so nothing is put back."""
    from kiro_crew.session_storage import SessionStorageError

    _grant(["dashboard:old"])

    def refuse():
        raise SessionStorageError("refused")

    handler, req, _moved = _cleanup_request(monkeypatch, ["dashboard_old"], move=refuse)

    resp = await handler.api_session_storage_cleanup(req)

    assert resp.status != 200
    assert not store.load().holds("dashboard:old")


@pytest.mark.asyncio
async def test_session_storage_cleanup_of_a_stacked_duplicate_keeps_its_siblings_trust(
    monkeypatch,
):
    """``dashboard_dashboard_<n>`` normalizes onto the kept sibling's key; it is not
    that sibling's file, so the sibling's saved trust stays."""
    _grant(["dashboard:chat-3-300"])
    handler, req, moved = _cleanup_request(monkeypatch, ["dashboard_dashboard_chat-3-300"])

    resp = await handler.api_session_storage_cleanup(req)

    assert resp.status == 200 and moved == [["u1"]]
    assert store.load().holds("dashboard:chat-3-300")
