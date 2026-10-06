"""Resume must not publish a slot for a session whose delete is still in flight.

``api_chat_slot_resume`` re-checks existence and identity after its threaded
transcript read, but that re-check reads the metadata line without the
transcript lock. A permanent delete holds that lock across its whole
transaction (search-index drop, attachment and reply-thread staging, the
unlink), so a resume that re-checks while the delete is inside that
transaction sees the file still present and publishes. The delete handler then
removes only the slot it captured before its own first await, so the slot the
resume published survives, and its next flush writes the deleted transcript
back.

Resume refuses with a retryable ``resume_conflict`` while such a delete is in
flight, leaving the session as it found it; a retry after the delete ends finds
the session gone or opens it. The interleaving is forced, not timed: the delete
is parked inside its locked section (at the attachment staging step, which runs
after the lock is taken and before the unlink), the resume's read is released
while it is parked, and the delete is let go only once the resume answered.
"""

import asyncio
import contextlib
import threading
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew import history_projection


def _park_delete_inside_its_lock(monkeypatch):
    """Hold ``delete_session`` after it took the lock and before it unlinks."""
    entered = threading.Event()
    release = threading.Event()
    original = history_projection.stage_attachments_removal

    def parked(parent, stem):
        entered.set()
        assert release.wait(timeout=60), "the test never released the parked delete"
        return original(parent, stem)

    monkeypatch.setattr(history_projection, "stage_attachments_removal", parked)
    return entered, release


def _park_resume_read(monkeypatch, log, key):
    """Hold the resume's threaded transcript read until released."""
    entered = threading.Event()
    release = threading.Event()
    snapshot = list(log.read_messages_chained(key))
    assert len(snapshot) == 2, f"fixture expected 2 messages, got {len(snapshot)}"

    def parked(_k):
        entered.set()
        assert release.wait(timeout=60), "the test never released the parked read"
        return list(snapshot)

    monkeypatch.setattr(log, "read_messages_chained", parked)
    return entered, release


async def _wait_for(event: threading.Event, what: str) -> None:
    ok = await asyncio.to_thread(event.wait, 30)
    assert ok, f"{what} never happened"


async def _conflict(resp) -> None:
    body = await resp.json()
    assert resp.status == 409, f"resume was not refused (status {resp.status}): {body}"
    assert body.get("code") == "resume_conflict", body


@pytest.mark.asyncio
async def test_a_resume_that_rechecks_while_the_delete_holds_its_lock_is_refused(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight1"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    read_entered, read_release = _park_resume_read(monkeypatch, log, key)
    delete_entered, delete_release = _park_delete_inside_its_lock(monkeypatch)

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/inflight1/resume", json={"key": key})
        )
        await _wait_for(read_entered, "the resume's transcript read")
        delete = asyncio.create_task(asyncio.to_thread(log.delete_session, key))
        try:
            await _wait_for(delete_entered, "the delete's locked section")
            # The delete is mid-transaction: the file still exists and the lock is
            # held. Let the resume finish its read and re-check now.
            read_release.set()
            resp = await asyncio.wait_for(post, timeout=30)
            await _conflict(resp)
            assert "inflight1" not in state._slots, "resume published during the delete"
            delete_release.set()
        finally:
            read_release.set()
            delete_release.set()
        deleted = await delete

    assert deleted, "the fixture never deleted the session"
    assert resp.status == 409, (
        f"resume published a slot while the session's delete was in flight "
        f"(status {resp.status}); the delete's cleanup only removes the slot it "
        "captured before it started, so this one survives and its flush "
        "resurrects the deleted transcript"
    )
    assert "inflight1" not in state._slots, "a slot was published for a session being deleted"


@pytest.mark.asyncio
async def test_a_skipped_delete_leaves_nothing_that_refuses_a_later_resume(tmp_path, monkeypatch):
    """A delete that did NOT go through (pinned session, skip_pinned) releases its window.

    Nothing it leaves behind may refuse a later resume of the live session.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight2"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    log.update_metadata(key, {"pinned": True})

    assert log.delete_session(key, skip_pinned=True) is None, "a pinned session was deleted"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/inflight2/resume", json={"key": key})
    assert (
        resp.status == 200
    ), f"a skipped delete left the session refusing resume (status {resp.status})"
    assert "inflight2" in state._slots


@pytest.mark.asyncio
async def test_a_resume_racing_the_delete_handler_before_its_unlink_is_refused(
    tmp_path, monkeypatch
):
    """The delete handler's window opens at its slot claim, not at the unlink.

    ``DELETE /api/sessions/{key}`` captures which slot to remove before its
    first await and removes only that one afterwards. A resume that publishes
    between that capture and ``delete_session`` taking the lock is in no claim,
    so it would survive the delete as an open tab of a deleted conversation.
    The handler is parked at its first await after the capture (the cron-owner
    scan) while the resume re-checks.
    """
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.crons = None  # no cron store in this fixture; the owner scan answers empty
    log = state.conversation_log
    key = "dashboard:inflight3"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    read_entered, read_release = _park_resume_read(monkeypatch, log, key)
    scan_entered = asyncio.Event()
    scan_release = asyncio.Event()
    original_scan = sessions_mod._owner_keys_bound_to_transcript

    async def parked_scan(crons, keys):
        scan_entered.set()
        await asyncio.wait_for(scan_release.wait(), timeout=60)
        return await original_scan(crons, keys)

    monkeypatch.setattr(sessions_mod, "_owner_keys_bound_to_transcript", parked_scan)

    app = _make_app(state)
    app.router.add_delete("/api/sessions/{key}", sessions_mod.api_session_delete)
    async with TestClient(TestServer(app)) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/inflight3/resume", json={"key": key})
        )
        await _wait_for(read_entered, "the resume's transcript read")
        delete = asyncio.create_task(client.delete(f"/api/sessions/{key}"))
        try:
            await asyncio.wait_for(scan_entered.wait(), timeout=30)
            read_release.set()
            resp = await asyncio.wait_for(post, timeout=30)
            await _conflict(resp)
            scan_release.set()
        finally:
            read_release.set()
            scan_release.set()
        delete_resp = await delete
        delete_body = await delete_resp.json()

    assert delete_body.get("ok") is True, f"the fixture's delete did not go through: {delete_body}"
    assert resp.status == 409, (
        f"resume published a slot after the delete handler had captured its claim "
        f"(status {resp.status}); the handler removes only the claimed slot, so this "
        "one survives the delete"
    )
    assert "inflight3" not in state._slots, "a slot for the deleted session survived the delete"


@pytest.mark.asyncio
async def test_a_bulk_clear_racing_a_resume_before_its_unlink_is_refused(tmp_path, monkeypatch):
    """``DELETE /api/sessions`` claims each row's slot the same way, so it opens
    the same window. The clear is parked inside its worker, after the row's claim
    and before ``delete_session`` opens its own window."""
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.crons = None  # no cron store in this fixture; the owner scan answers empty
    log = state.conversation_log
    key = "dashboard:inflight4"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    read_entered, read_release = _park_resume_read(monkeypatch, log, key)
    resolve_entered = threading.Event()
    resolve_release = threading.Event()
    original_resolve = sessions_mod._resolve_history_delete_claim

    def parked_resolve(*args, **kwargs):
        resolve_entered.set()
        assert resolve_release.wait(timeout=60), "the test never released the parked clear"
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(sessions_mod, "_resolve_history_delete_claim", parked_resolve)

    app = _make_app(state)
    app.router.add_delete("/api/sessions", sessions_mod.api_sessions_clear)
    async with TestClient(TestServer(app)) as client:
        # The clear lists clearable rows first, so it starts before the resume
        # can publish the tab that would make the row "open".
        clear = asyncio.create_task(client.delete("/api/sessions"))
        try:
            await _wait_for(resolve_entered, "the bulk clear's locked section")
            post = asyncio.create_task(
                client.post("/api/chat/slots/inflight4/resume", json={"key": key})
            )
            await _wait_for(read_entered, "the resume's transcript read")
            read_release.set()
            resp = await asyncio.wait_for(post, timeout=30)
            await _conflict(resp)
            resolve_release.set()
        finally:
            read_release.set()
            resolve_release.set()
        clear_body = await (await clear).json()

    assert clear_body.get("cleared") == 1, f"the fixture's clear did not go through: {clear_body}"
    assert (
        resp.status == 409
    ), f"resume published a slot while a bulk clear was deleting it (status {resp.status})"
    assert "inflight4" not in state._slots, "a slot for the cleared session survived the clear"


@pytest.mark.asyncio
async def test_a_refused_resume_leaves_a_closed_session_closed_and_a_retry_opens_it(
    tmp_path, monkeypatch
):
    """A delete that does not go through (a pinned row under a bulk clear).

    The reopen write runs only after construction, so the refusal leaves the
    marker set: a session with no tab does not come back
    as open at the next start, and the resume holds nothing afterwards. Once the
    delete ends, a retry opens the session.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight5"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    closed_at = time.time() - 60
    log.update_metadata(key, {"closed": True, "closed_at": closed_at})

    url = "/api/chat/slots/inflight5/resume"
    async with TestClient(TestServer(_make_app(state))) as client:
        with log.delete_in_flight_window(key):
            await _conflict(await client.post(url, json={"key": key}))
            meta = log.get_metadata(key)
            assert meta.get("closed") is True, "a refused resume left a tabless session open"
            assert meta.get("closed_at") == closed_at
            assert "inflight5" not in state._slots
            assert "inflight5" not in state._slots_under_construction, "the resume leaked its hold"
        resp = await client.post(url, json={"key": key})

    assert resp.status == 200, f"a retry after the delete did not open it (status {resp.status})"
    assert "inflight5" in state._slots
    assert "closed" not in log.get_metadata(key)


@pytest.mark.asyncio
async def test_the_reopen_write_lands_before_the_publish(tmp_path, monkeypatch):
    """A hook-less resume of a closed session publishes only after ``closed`` is cleared.

    The resume is parked inside its marker clear. The slot is not visible yet, so
    a second resume arriving meanwhile is refused ``resume_in_progress`` and
    writes nothing. Once the clear lands the first resume publishes, and the
    session is open on disk before any client sees its tab.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight10"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    log.update_metadata(key, {"closed": True, "closed_at": time.time() - 60})
    clear_entered = threading.Event()
    clear_release = threading.Event()
    clears = []
    original_clear = log.clear_closed

    def parked_clear(*args, **kwargs):
        clears.append("inflight10" in state._slots)
        clear_entered.set()
        assert clear_release.wait(timeout=60), "the test never released the parked clear"
        return original_clear(*args, **kwargs)

    monkeypatch.setattr(log, "clear_closed", parked_clear)

    url = "/api/chat/slots/inflight10/resume"
    async with TestClient(TestServer(_make_app(state))) as client:
        first = asyncio.create_task(client.post(url, json={"key": key}))
        try:
            await _wait_for(clear_entered, "the first resume's marker clear")
            assert log.get_metadata(key).get("closed") is True
            second = await asyncio.wait_for(client.post(url, json={"key": key}), timeout=30)
            second_body = await second.json()
            assert not first.done()
        finally:
            clear_release.set()
        resp = await first

    assert clears == [False], f"the slot was published before the reopen write: {clears}"
    assert second.status == 409, f"second resume got {second.status}, not resume_in_progress"
    assert second_body.get("code") == "resume_in_progress"
    assert resp.status == 200, f"first resume did not go ahead (status {resp.status})"
    assert "inflight10" in state._slots
    assert "closed" not in log.get_metadata(key), "the reopen write did not land"
    assert "inflight10" not in state._slots_under_construction


@pytest.mark.asyncio
async def test_a_reopen_write_that_fails_publishes_nothing(tmp_path, monkeypatch):
    """A hook-less resume whose ``closed`` clear raises refuses and leaves no tab.

    Publishing anyway would open a tab whose session is still marked closed, so
    it would vanish at the next start while the person keeps typing into it.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight11"
    log.append(key, "user", "history-1")
    log.update_metadata(key, {"closed": True, "closed_at": time.time() - 60})

    def failing_clear(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(log, "clear_closed", failing_clear)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/inflight11/resume", json={"key": key})
        body = await resp.json()

    assert resp.status == 503, f"a failed reopen write still answered {resp.status}"
    assert body.get("code") == "reopen_failed"
    assert "inflight11" not in state._slots, "a tab was published over a failed reopen write"
    assert "inflight11" not in state._slots_under_construction
    assert log.get_metadata(key).get("closed") is True


@pytest.mark.asyncio
async def test_a_delete_starting_during_the_member_binding_read_is_seen(tmp_path, monkeypatch):
    """The in-flight check is the LAST synchronous step before construction.

    A member thread's resume awaits a second binding read after the existence
    and identity barrier. A delete whose window opens during that await (its
    claim captured then, before the slot exists) must still stop the publish,
    so the check cannot sit ahead of that await.
    """
    from kiro_crew import members as members_mod

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    name = "member-alice"
    key = "dashboard:member-alice"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    log.update_metadata(key, {"mode": members_mod.DM_SLOT_MODE, "agent": "alice"})

    calls = {"n": 0}
    late_entered = threading.Event()
    late_release = threading.Event()

    def binding(_slot_key):
        calls["n"] += 1
        if calls["n"] == 2:
            # The late barrier's read: park it so a delete can start inside it.
            late_entered.set()
            assert late_release.wait(timeout=60), "the test never released the binding read"
        return {"member": "alice"}

    monkeypatch.setattr(members_mod, "read_dm_binding_for_slot", binding)
    monkeypatch.setattr(members_mod, "is_dispatchable_member_name", lambda _v: True)

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(client.post(f"/api/chat/slots/{name}/resume", json={"key": key}))
        await _wait_for(late_entered, "the late member-binding read")
        # A delete handler opens its window (and takes its claim) now, while the
        # resume is suspended past its existence and identity barrier.
        with log.delete_in_flight_window(key):
            late_release.set()
            resp = await asyncio.wait_for(post, timeout=30)
            assert name not in state._slots, "resume published during the delete"
            assert await asyncio.to_thread(log.delete_session, key), "the fixture did not delete"

    assert calls["n"] == 2, f"the fixture did not reach the late binding read ({calls['n']})"
    assert resp.status == 409, (
        f"resume published a member slot for a session deleted after its last "
        f"await (status {resp.status})"
    )
    assert name not in state._slots, "a slot was published for a deleted session"


@pytest.mark.asyncio
async def test_a_finished_delete_releases_its_window(tmp_path, monkeypatch):
    """After a delete that went through, nothing it left behind refuses the key.

    The key names no transcript now, so a resume of it opens a fresh slot;
    a marker left open would answer ``resume_conflict`` instead, and none of the
    deleted rows come back.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight6"
    log.append(key, "user", "history-1")

    assert log.delete_session(key), "the fixture did not delete the session"
    assert not log.delete_in_flight(key), "a finished delete left its window open"
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/inflight6/resume", json={"key": key})
        body = await resp.json()
    assert resp.status == 200, f"a finished delete still refuses the key: {body}"
    assert log.read_messages(key) == [], "the deleted rows came back"


def test_a_delete_that_raises_releases_its_window(tmp_path):
    """A delete whose transaction raises leaves no marker that would refuse every later resume."""
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight7"
    log.append(key, "user", "history-1")

    def failing_delete(*_args, **_kwargs):
        assert log.delete_in_flight(key), "the window was not open during the delete"
        raise OSError("disk full")

    log._metadata_projection.delete_session = failing_delete
    with pytest.raises(OSError, match="disk full"):
        log.delete_session(key)
    assert not log.delete_in_flight(key), "a delete that raised left its window open"


@pytest.mark.asyncio
async def test_a_cancelled_delete_releases_its_window(tmp_path):
    """A delete handler cancelled while its window is open (a torn-down request) releases it."""
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight8"
    entered = asyncio.Event()

    async def handler_body():
        with contextlib.ExitStack() as windows:
            windows.enter_context(log.delete_in_flight_window(key))
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(handler_body())
    await asyncio.wait_for(entered.wait(), timeout=30)
    assert log.delete_in_flight(key)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not log.delete_in_flight(key), "a cancelled delete left its window open"


@pytest.mark.asyncio
async def test_a_delete_opening_during_the_reopen_write_refuses_the_resume(tmp_path, monkeypatch):
    """The reserved tail's own in-flight arm, after construction.

    A closed session's resume clears ``closed`` in an awaited worker call after
    the slot is built. A delete whose window opens during that call still finds
    the file on the lock-free verification read, so only the tail's in-flight
    check stops the publish. The resume must refuse ``resume_conflict``, publish
    nothing, release its construction mark, and put the ``closed`` marker back.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:inflight12"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    closed_at = time.time() - 60
    log.update_metadata(key, {"closed": True, "closed_at": closed_at})
    original_clear = log.clear_closed
    windows = contextlib.ExitStack()
    opened = []

    def clear_while_a_delete_opens(*args, **kwargs):
        windows.enter_context(log.delete_in_flight_window(key))
        opened.append(True)
        return original_clear(*args, **kwargs)

    monkeypatch.setattr(log, "clear_closed", clear_while_a_delete_opens)
    with windows:
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/inflight12/resume", json={"key": key})
            await _conflict(resp)
        assert opened, "the resume never reached its reopen write"
        assert "inflight12" not in state._slots, "resume published during the delete"
        assert "inflight12" not in state._slots_under_construction, "the resume leaked its hold"
        meta = log.get_metadata(key)
        assert meta.get("closed") is True, "the refused resume left the session open"
        assert meta.get("closed_at") == closed_at
