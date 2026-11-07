"""Resume must not publish a slot by name before it holds its history.

``api_chat_slot_resume`` reads the transcript off the event loop. If that await
sits between ``get_or_create_slot`` and the hydrate loop, the slot is reachable
from ``state._slots`` while still empty, so a concurrent request that appends a
prompt gets it ordered *before* the history the resume then appends.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, call

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.testing.wait import async_wait_until, await_until


def _app_scoped(state, declared_app: str) -> web.Application:
    """The resume app with a token middleware that publishes ``declared_app``."""
    app = _make_app(state)

    @web.middleware
    async def _publish_app(request: web.Request, handler):
        request["app"] = declared_app
        return await handler(request)

    app.middlewares.append(_publish_app)
    return app


@pytest.mark.asyncio
async def test_slot_is_never_reachable_unhydrated_during_the_threaded_read(tmp_path, monkeypatch):
    """No coroutine may observe the resumed slot published with zero messages.

    The threaded read is held open while another coroutine resolves the slot by
    name, which is what any concurrent request does. Seeing the slot present but
    empty there is the defect: whatever it appends lands ahead of the history.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    log.append("dashboard:race1", "user", "history-1")
    log.append("dashboard:race1", "assistant", "history-2")

    read_entered = threading.Event()
    may_finish = threading.Event()
    original = log.read_messages_chained

    def blocking_read(key):
        # Runs in the to_thread worker, so the loop is free: signal that resume
        # is suspended, then hold the await open until the racer has looked.
        read_entered.set()
        may_finish.wait(timeout=10)
        return original(key)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    observed = {}

    async def racer():
        await async_wait_until(read_entered.is_set)
        slot = state._slots.get("race1")
        observed["present"] = slot is not None
        observed["empty"] = slot is not None and len(slot.messages) == 0
        if slot is not None:
            slot.append("user", "NEW-PROMPT", "msg msg-u")
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/race1/resume", json={"key": "dashboard:race1"})
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        assert (await post).status == 200

    assert not observed["empty"], (
        "resume published the slot by name before hydrating it; a concurrent "
        "append would be ordered ahead of the restored history"
    )

    contents = [m.get("content") for m in state._slots["race1"].messages]
    history = [c for c in contents if c and c.startswith("history-")]
    assert history == ["history-1", "history-2"], f"history lost or reordered: {contents}"
    if "NEW-PROMPT" in contents:
        assert contents.index("NEW-PROMPT") > contents.index(
            "history-2"
        ), f"a concurrently appended prompt was ordered before the history: {contents}"


@pytest.mark.asyncio
async def test_a_slot_published_during_the_read_still_faces_the_ownership_gate(
    tmp_path, monkeypatch
):
    """The pre-await ownership gate must be re-applied after the threaded read.

    The read runs off the loop, so a concurrent resume can publish the slot while
    this request is suspended. ``get_or_create_slot`` returns that live slot by
    name and applies no ownership check of its own, so without a re-check a
    foreign app is handed a slot it does not own.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    log.append("dashboard:race2", "user", "history-1")
    log.append("dashboard:race2", "assistant", "history-2")
    # Record appB as owner so the handler reads the transcript (the app-isolation
    # gate refuses an unowned transcript before reaching the threaded read).
    await asyncio.to_thread(log.update_metadata, "dashboard:race2", {"app": "appB"})

    read_entered = threading.Event()
    may_finish = threading.Event()
    original = log.read_messages_chained

    def blocking_read(key):
        read_entered.set()
        may_finish.wait(timeout=10)
        return original(key)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    async def racer():
        # Wait for the threaded read to enter, then publish appA's slot while
        # the handler is suspended. The post-read ownership re-check must catch
        # this and return 404 rather than handing appB a slot it does not own.
        await async_wait_until(read_entered.is_set)
        foreign = state.get_or_create_slot("race2", app="appA")
        foreign._app = "appA"
        may_finish.set()

    async with TestClient(TestServer(_app_scoped(state, "appB"))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/race2/resume", json={"key": "dashboard:race2"})
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post

    assert resp.status == 404, (
        f"appB was handed appA's slot (status {resp.status}); the ownership gate was "
        "applied only before the await, so the publish that landed during it escaped it"
    )
    assert state._slots["race2"]._app == "appA", "the foreign slot's owner was overwritten"


@pytest.mark.asyncio
async def test_slot_is_not_reachable_unhydrated_during_the_folder_unhide(tmp_path, monkeypatch):
    """No await may sit between publishing the slot and hydrating it.

    The transcript read was hoisted above the publish, but the folder-unhide and
    closed-flag awaits ran after it and before the hydrate loop. A session filed in
    a folder therefore still published an empty slot across a suspension point, and
    a concurrent append there lands ahead of the restored history.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    log.append("dashboard:race3", "user", "history-1")
    log.append("dashboard:race3", "assistant", "history-2")
    log.update_metadata("dashboard:race3", {"folder_id": "fldr00000001"})

    unhide_entered = asyncio.Event()
    may_finish = asyncio.Event()

    async def slow_unhide(_state, _folder_id):
        unhide_entered.set()
        await may_finish.wait()
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._unhide_folder", slow_unhide)

    observed = {}

    async def racer():
        try:
            await await_until(unhide_entered, what="unhide entered")
        except AssertionError:
            observed["reached"] = False
            may_finish.set()
            return
        observed["reached"] = True
        slot = state._slots.get("race3")
        observed["present_and_empty"] = slot is not None and len(slot.messages) == 0
        if slot is not None:
            slot.append("user", "NEW-PROMPT", "msg msg-u")
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/race3/resume", json={"key": "dashboard:race3"})
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        assert (await post).status == 200

    assert observed.get("reached"), "the folder-unhide await never ran; fixture did not engage"
    assert not observed.get("present_and_empty"), (
        "the slot was published and still empty while the folder-unhide await was "
        "suspended; a concurrent append there is ordered ahead of the history"
    )
    contents = [m.get("content") for m in state._slots["race3"].messages]
    history = [c for c in contents if c and c.startswith("history-")]
    assert history == ["history-1", "history-2"], f"history lost or reordered: {contents}"
    if "NEW-PROMPT" in contents:
        assert contents.index("NEW-PROMPT") > contents.index(
            "history-2"
        ), f"a concurrent append was ordered before the restored history: {contents}"


@pytest.mark.asyncio
async def test_a_delete_during_the_read_does_not_republish_the_transcript(tmp_path, monkeypatch):
    """A session deleted while resume is suspended must not be resurrected.

    ``history.delete_session`` leaves no tombstone, and resume reads the transcript
    before publishing, so a delete inside that read would be undone by the next flush.
    The probe is ``get_metadata_status``: ``get_metadata`` returns ``{}`` for both
    "deleted" and "unreadable", and an unreadable line is not a deletion.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:del1"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    read_entered = threading.Event()
    may_finish = threading.Event()

    # Snapshot the transcript BEFORE patching: the parked read must return the
    # PRE-delete content, which is the actual window this guards. A read that
    # returned post-delete content would yield [] and describe a different bug
    # (publishing an empty slot), not the resurrection of a populated one.
    pre_delete = list(log.read_messages_chained(key))
    assert len(pre_delete) == 2, f"fixture expected 2 messages, got {len(pre_delete)}"

    def blocking_read(k):
        # Runs in the to_thread worker, so the loop is free for the racer.
        read_entered.set()
        may_finish.wait(timeout=10)
        return list(pre_delete)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    deleted = {"ok": False}

    async def racer():
        await async_wait_until(read_entered.is_set)
        assert read_entered.is_set(), "the transcript read never entered"
        # The permanent delete lands while resume holds the loaded transcript.
        deleted["ok"] = await asyncio.to_thread(log.delete_session, key)
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(client.post("/api/chat/slots/del1/resume", json={"key": key}))
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post

    assert deleted["ok"], "the fixture never deleted the session"
    assert resp.status == 409, (
        f"resume published a slot for a session deleted mid-read (status {resp.status}); "
        "its next flush would rewrite the transcript that was permanently deleted"
    )
    assert (
        "del1" not in state._slots
    ), "the slot was published for a deleted session, so a flush can resurrect it"


@pytest.mark.asyncio
async def test_a_delete_then_recreate_during_the_read_does_not_overwrite_the_replacement(
    tmp_path, monkeypatch
):
    """A session recreated while resume is suspended must not be overwritten.

    After delete-then-recreate the metadata exists but belongs to the NEW session, so
    an existence check would publish the OLD transcript over it. Identity separates
    them: every new metadata line stamps ``created_at`` and
    ``_rewrite_session_locked`` preserves it, so a differing value means a recreate.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:recreate1"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    original_created_at = log.get_metadata(key).get("created_at")
    assert original_created_at, "fixture expected the original session to carry created_at"

    read_entered = threading.Event()
    may_finish = threading.Event()

    # The parked read must serve the PRE-delete transcript: that is the content a
    # published slot would flush over the replacement.
    original_reader = log.read_messages_chained
    pre_delete = list(log.read_messages_chained(key))
    assert len(pre_delete) == 2, f"fixture expected 2 messages, got {len(pre_delete)}"

    def blocking_read(k):
        read_entered.set()
        may_finish.wait(timeout=10)
        return list(pre_delete)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    outcome = {"deleted": False, "recreated_created_at": None}

    async def racer():
        await async_wait_until(read_entered.is_set)
        assert read_entered.is_set(), "the transcript read never entered"

        def delete_then_recreate():
            ok = log.delete_session(key)
            # Recreate under the SAME key -- a new conversation the user is now
            # using. ``append`` mints a fresh metadata line, so ``created_at``
            # differs (microsecond resolution).
            log.append(key, "user", "replacement-1")
            return ok

        outcome["deleted"] = await asyncio.to_thread(delete_then_recreate)
        outcome["recreated_created_at"] = log.get_metadata(key).get("created_at")
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/recreate1/resume", json={"key": key})
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post

    assert outcome["deleted"], "the fixture never deleted the session"
    assert outcome["recreated_created_at"], "the fixture never recreated the session"
    assert outcome["recreated_created_at"] != original_created_at, (
        "fixture is not exercising a recreate: the replacement carries the same "
        "created_at, so identity cannot discriminate it"
    )

    assert resp.status == 409, (
        f"resume published a slot for a RECREATED session (status {resp.status}); "
        "it holds the deleted session's transcript and its next flush overwrites "
        "the replacement the user is now using"
    )
    assert (
        "recreate1" not in state._slots
    ), "the slot was published, so a flush can overwrite the replacement session"

    # The replacement transcript must still be its own: exactly the message the
    # recreate wrote, with none of the stale ones carried over. Read through the
    # ORIGINAL reader captured before the patch, so this measures the file rather
    # than the parked stub.
    survivors = [m.get("content") for m in original_reader(key)]
    assert "replacement-1" in survivors, f"the replacement message is gone: {survivors}"
    assert (
        "history-1" not in survivors and "history-2" not in survivors
    ), f"the deleted session's messages were written over the replacement: {survivors}"


@pytest.mark.asyncio
async def test_a_benign_metadata_rewrite_during_the_read_still_publishes(tmp_path, monkeypatch):
    """A metadata change that is NOT a recreate must not refuse the resume.

    A title/agent update keeps ``created_at``; a guard comparing the whole metadata
    dict would turn an ordinary concurrent rename into a failed resume.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:benign1"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    original_created_at = log.get_metadata(key).get("created_at")
    assert original_created_at, "fixture expected created_at"

    read_entered = threading.Event()
    may_finish = threading.Event()
    snapshot = list(log.read_messages_chained(key))

    def blocking_read(k):
        read_entered.set()
        may_finish.wait(timeout=10)
        return list(snapshot)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    changed = {"ok": False}

    async def racer():
        await async_wait_until(read_entered.is_set)
        assert read_entered.is_set(), "the transcript read never entered"
        await asyncio.to_thread(log.update_metadata, key, {"title": "renamed mid-read"})
        changed["ok"] = log.get_metadata(key).get("title") == "renamed mid-read"
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(client.post("/api/chat/slots/benign1/resume", json={"key": key}))
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post

    assert changed["ok"], "the fixture never rewrote the metadata"
    assert log.get_metadata(key).get("created_at") == original_created_at, (
        "fixture invalid: the benign rewrite changed created_at, so it is "
        "indistinguishable from a recreate"
    )
    assert resp.status == 200, (
        f"an ordinary concurrent metadata rewrite was refused (status {resp.status}); "
        "the identity arm is comparing more than the identity field"
    )
    assert "benign1" in state._slots, "the slot was not published for a live session"


@pytest.mark.asyncio
async def test_a_folder_filed_during_the_read_is_not_erased_by_a_stale_existence_verdict(
    tmp_path, monkeypatch
):
    """A folder assignment landing during the threaded read must survive resume.

    The folder-existence verdict comes from the PRE-read snapshot while ``folder_id``
    comes from the re-read. If a reconciliation files the session mid-read, applying
    the old folder's verdict would erase the new filing. A verdict is only about the
    folder it was computed for.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:folderrace1"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")

    # The PRE-read snapshot points at a folder that does not exist, so the real
    # ``_unhide_folder`` reports False -- the verdict that drives the drop.
    gone_id = "fldrGONE0001"
    live_id = "fldrLIVE0001"
    log.update_metadata(key, {"folder_id": gone_id})
    # ...and the folder the reconciliation files it into DOES exist.
    state._folders.append({"id": live_id, "name": "Filed By Reconciliation", "order": 0})
    assert not any(f["id"] == gone_id for f in state._folders), (
        "fixture expected the pre-read folder to be absent from the store, so the "
        "existence verdict is False"
    )

    read_entered = threading.Event()
    may_finish = threading.Event()
    pre_read = list(log.read_messages_chained(key))
    assert len(pre_read) == 2, f"fixture expected 2 messages, got {len(pre_read)}"

    def blocking_read(k):
        read_entered.set()
        may_finish.wait(timeout=10)
        return list(pre_read)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    observed = {"filed": False, "pre": None, "post": None}

    async def racer():
        await async_wait_until(read_entered.is_set)
        assert read_entered.is_set(), "the transcript read never entered"

        # Channel reconciliation files the session while resume is suspended.
        await asyncio.to_thread(log.update_metadata, key, {"folder_id": live_id})
        observed["post"] = log.get_metadata(key).get("folder_id")
        observed["filed"] = True
        may_finish.set()

    observed["pre"] = gone_id

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/folderrace1/resume", json={"key": key})
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post

    assert observed["filed"], "the fixture never re-filed the session"
    assert observed["post"] == live_id, f"the fixture did not land the new folder: {observed}"
    assert observed["pre"] != observed["post"], (
        "fixture is not exercising a snapshot split: both snapshots carry the same "
        "folder_id, so the test passes with or without the fix"
    )

    assert resp.status == 200, f"resume did not publish (status {resp.status})"
    slot = state._slots.get("folderrace1")
    assert slot is not None, "the slot was never published"
    assert slot.folder_id == live_id, (
        f"the live folder filed during the read was erased (folder_id={slot.folder_id!r}); "
        f"an existence verdict earned by {gone_id} was applied to {live_id}, and the "
        "dirty-slot flush persists that erasure"
    )
    assert (
        log.get_metadata(key).get("folder_id") == live_id
    ), "the persisted folder_id no longer matches the filing the reconciliation made"


@pytest.mark.asyncio
async def test_a_dangling_folder_id_unchanged_across_the_read_is_still_dropped(
    tmp_path, monkeypatch
):
    """CONTROL for the opposite direction: the drop must still fire.

    Scoping the existence verdict to the folder it was computed for must not stop
    it applying when the id did NOT move. A session filed in a folder deleted
    since it was last saved still has to resume plainly unfiled rather than
    pointing at nothing.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:folderdangle1"
    log.append(key, "user", "history-1")

    gone_id = "fldrGONE0002"
    log.update_metadata(key, {"folder_id": gone_id})
    assert not any(
        f["id"] == gone_id for f in state._folders
    ), "fixture expected the folder to be absent so the existence verdict is False"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/folderdangle1/resume", json={"key": key})

    assert resp.status == 200, f"resume did not publish (status {resp.status})"
    slot = state._slots.get("folderdangle1")
    assert slot is not None, "the slot was never published"
    assert slot.folder_id == "", (
        f"a dangling folder_id survived resume (folder_id={slot.folder_id!r}); the "
        "resumed session points at a folder that does not exist"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp_closed_at", [True, False])
async def test_a_close_landing_during_the_read_is_not_cleared_by_the_stale_snapshot(
    tmp_path, monkeypatch, stamp_closed_at
):
    """Resuming a closed session must not reopen a replacement closed mid-read.

    ``clear_closed`` acts on the pre-read ``meta`` and runs before the identity
    re-check's 409, so an unconditional clear would drop a replacement's ``closed``.
    ``stamp_closed_at=True`` is the app close path; ``False`` is a flag with no stamp,
    where the file's mtime stands in for the close instant.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:closerace1"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    # The SOURCE session is closed, so the resume takes the clear_closed path.
    log.update_metadata(key, {"closed": True, "closed_at": time.time()})
    assert log.get_metadata(key).get("closed"), "fixture expected a closed source session"

    read_entered = threading.Event()
    may_finish = threading.Event()
    original_reader = log.read_messages_chained
    pre_delete = list(log.read_messages_chained(key))
    assert len(pre_delete) == 2, f"fixture expected 2 messages, got {len(pre_delete)}"

    def blocking_read(k):
        read_entered.set()
        may_finish.wait(timeout=10)
        return list(pre_delete)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    outcome = {"recreated": False, "closed_after_recreate": False, "mtime": None}
    # Lower bound on the handler's internal boundary: it captures its epoch after
    # this point, so anything the racer writes later than t0 + WINDOW_S is later
    # than that boundary too.
    t0 = time.time()
    WINDOW_S = 0.3

    async def racer():
        await async_wait_until(read_entered.is_set)
        assert read_entered.is_set(), "the transcript read never entered"

        # A REAL interval, not a token one. Without ``closed_at`` the guard falls
        # back to the file's mtime, whose resolution can lag ``time.time()``: over
        # a sub-millisecond window a genuinely fresh write can still stamp older
        # than a wall-clock boundary, which would make this test pass vacuously.
        await asyncio.sleep(WINDOW_S)

        def delete_recreate_close():
            log.delete_session(key)
            # A NEW conversation under the same key, which the user then closes.
            log.append(key, "user", "replacement-1")
            flag = {"closed": True}
            if stamp_closed_at:
                flag["closed_at"] = time.time()
            log.update_metadata(key, flag)
            return log.get_metadata(key), log.mtime_of(key)

        meta_after, mtime_after = await asyncio.to_thread(delete_recreate_close)
        outcome["recreated"] = True
        outcome["closed_after_recreate"] = bool(meta_after.get("closed"))
        outcome["mtime"] = mtime_after
        assert ("closed_at" in meta_after) is stamp_closed_at, (
            "fixture did not produce the intended discriminator: "
            f"closed_at present={'closed_at' in meta_after}, wanted {stamp_closed_at}"
        )
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots/closerace1/resume", json={"key": key})
        )
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post

    assert outcome["recreated"], "the fixture never recreated the session"
    assert outcome["closed_after_recreate"], "the fixture never closed the replacement"
    if not stamp_closed_at:
        # The fallback can only discriminate if the recreated file's mtime really
        # is later than the handler's boundary. t0 precedes that boundary, so this
        # proves the close instant the guard reads is on the correct side of it.
        assert outcome["mtime"] is not None and outcome["mtime"] > t0 + WINDOW_S / 2, (
            "fixture is not exercising the mtime fallback: the recreated file's "
            f"mtime ({outcome['mtime']}) is not clearly later than the resume "
            f"boundary (>= {t0}), so a pass would not measure the guard"
        )

    # THE FINDING: the replacement's close must survive the stale clear.
    assert log.get_metadata(key).get("closed"), (
        "the replacement session's `closed` flag was cleared by a resume acting on "
        "a pre-read snapshot; the conversation the user closed is reopened on the "
        "next gateway restart"
    )
    # And the identity re-check must still refuse -- the clear guard must not
    # change the response contract.
    assert (
        resp.status == 409
    ), f"resume published a slot for a RECREATED session (status {resp.status})"
    survivors = [m.get("content") for m in original_reader(key)]
    assert "replacement-1" in survivors, f"the replacement message is gone: {survivors}"


@pytest.mark.asyncio
async def test_a_session_closed_before_the_resume_still_has_its_flag_cleared(tmp_path, monkeypatch):
    """CONTROL for the ordinary direction: a legitimate close must still clear.

    The compare-and-clear must only refuse a close instant at or after the resume
    boundary. A session the user closed before resuming has to reopen, or it
    vanishes again on the next gateway restart. Both failure directions here are
    silent, so this direction needs its own test.
    """
    import json
    from types import SimpleNamespace

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:closenormal1"
    log.append(key, "user", "history-1")
    closed_at = 1_700_000_000.0
    log.update_metadata(key, {"closed": True, "closed_at": closed_at})
    assert log.get_metadata(key).get("closed"), "fixture expected a closed session"
    persisted_before = json.loads(log._path(key).read_text(encoding="utf-8").splitlines()[0])
    assert persisted_before["closed"] is True
    assert persisted_before["closed_at"] == closed_at

    app = _make_app(state)
    # BEFORE means strictly earlier: consecutive wall-clock reads can share a
    # tick on Windows, and compare-and-clear intentionally preserves ties.
    # Pin only the handler's clock, not process-wide time or the real writer.
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.time",
        SimpleNamespace(time=lambda: closed_at + 1),
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/closenormal1/resume", json={"key": key})

    assert resp.status == 200, f"resume did not publish (status {resp.status})"
    assert not log.get_metadata(key).get("closed"), (
        "a session closed BEFORE the resume kept its `closed` flag; it will "
        "disappear again on the next gateway restart"
    )
    persisted_after = json.loads(log._path(key).read_text(encoding="utf-8").splitlines()[0])
    assert "closed" not in persisted_after and "closed_at" not in persisted_after
    assert persisted_after["created_at"] == persisted_before["created_at"]
    assert [m.get("content") for m in state._slots["closenormal1"].messages] == ["history-1"]
    assert [m.get("content") for m in log.read_messages_chained(key)] == ["history-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True])
async def test_a_metadata_only_session_removed_during_the_read_is_not_republished(
    tmp_path, monkeypatch, recreate
):
    """An EMPTY transcript must not disable the deletion and identity guards.

    A metadata-only session has no messages, so guards keyed on ``all_messages`` would
    let resume publish a removed session. The pre-read ``meta`` witnesses prior
    existence instead; an empty live session still resumes because ``not
    post_read_meta`` stays false and the identity arm needs two differing ``created_at``.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:mdonly1"
    # METADATA-ONLY: a metadata line and no messages at all.
    log.update_metadata(key, {"title": "metadata only"})
    pre_meta = log.get_metadata(key)
    assert pre_meta, "fixture expected a nonempty pre-read metadata line"
    assert pre_meta.get("created_at"), "fixture expected created_at for the identity arm"
    assert list(log.read_messages_chained(key)) == [], (
        "fixture is not exercising the empty-transcript path: the session has messages, "
        "so the old all_messages gate would have fired on its own"
    )

    read_entered = threading.Event()
    may_finish = threading.Event()
    original_reader = log.read_messages_chained

    def blocking_read(k):
        read_entered.set()
        may_finish.wait(timeout=10)
        return []

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    outcome = {"deleted": False, "post_meta": None}

    async def racer():
        await async_wait_until(read_entered.is_set)
        assert read_entered.is_set(), "the transcript read never entered"

        def remove():
            ok = log.delete_session(key)
            if recreate:
                # A DIFFERENT conversation under the same key, which the user is
                # now using. Its metadata line carries a fresh created_at.
                log.update_metadata(key, {"title": "replacement"})
            return ok

        outcome["deleted"] = await asyncio.to_thread(remove)
        outcome["post_meta"] = log.get_metadata(key)
        may_finish.set()

    async with TestClient(TestServer(_make_app(state))) as client:
        post = asyncio.create_task(client.post("/api/chat/slots/mdonly1/resume", json={"key": key}))
        await asyncio.gather(post, asyncio.create_task(racer()))
        resp = await post

    assert outcome["deleted"], "the fixture never deleted the session"
    if recreate:
        assert outcome["post_meta"], "the fixture never recreated the session"
        assert outcome["post_meta"].get("created_at") != pre_meta["created_at"], (
            "fixture is not exercising the identity arm: the replacement carries the "
            "same created_at, so identity cannot discriminate it"
        )
    else:
        assert not outcome["post_meta"], "the fixture left metadata behind after the delete"

    arm = "identity" if recreate else "deletion"
    assert resp.status == 409, (
        f"resume published a slot for a metadata-only session removed during the read "
        f"(status {resp.status}); the {arm} guard was disabled by the empty transcript, "
        "so the next flush recreates or overwrites the removed session"
    )
    assert (
        "mdonly1" not in state._slots
    ), "the slot was published, so a flush can write the removed session back"
    if recreate:
        survivors = [m.get("content") for m in original_reader(key)]
        assert (
            log.get_metadata(key).get("title") == "replacement"
        ), f"the replacement session's metadata was overwritten: {log.get_metadata(key)}"
        assert survivors == [], f"stale content was written into the replacement: {survivors}"


@pytest.mark.asyncio
async def test_a_legitimately_empty_session_that_still_exists_still_resumes(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: a metadata-only session that is still PRESENT resumes normally.

    A widened existence witness must not make every empty session answer 409
    ``resume_session_deleted``.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:mdonly2"
    log.update_metadata(key, {"title": "still here"})
    assert log.get_metadata(key), "fixture expected the session to exist"
    assert list(log.read_messages_chained(key)) == [], "fixture expected an empty transcript"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/mdonly2/resume", json={"key": key})

    assert resp.status == 200, (
        f"resuming a legitimately empty session that still exists returned {resp.status}; "
        "the guards must not refuse a session that is present"
    )
    assert "mdonly2" in state._slots, "the slot was not published for a live empty session"


@pytest.mark.asyncio
async def test_a_failing_slots_broadcast_does_not_500_a_committed_resume(
    tmp_path, monkeypatch, caplog
):
    """Every slots broadcast fails on a leading edge (a poisoned other slot): the
    resume has committed, so it answers 200 and the failures go to the log."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "dashboard:poisoned1"
    state.conversation_log.update_metadata(key, {"title": "still here"})

    def _fail():
        raise TypeError("Object of type object is not JSON serializable")

    monkeypatch.setattr(state, "_do_slots_broadcast", _fail)
    state._slots_broadcast_interval = 0.0  # every push lands on a leading edge

    with caplog.at_level("ERROR", logger="kiro_crew.dashboard.state"):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/poisoned1/resume", json={"key": key})

    assert resp.status == 200
    assert "poisoned1" in state._slots
    assert any(r.getMessage().startswith("Slots broadcast failed") for r in caplog.records)


@pytest.mark.asyncio
async def test_resuming_a_session_that_never_existed_is_not_refused(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: an absent key is not a deletion.

    Resuming a key with no metadata and no transcript must still publish. This is
    the case the widened witness could plausibly catch by accident, since both
    guards read ``meta_readable`` and an absent file is readable-and-empty.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:neverexisted1"
    assert not log.get_metadata(key), "fixture expected no metadata for an absent session"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/neverexisted1/resume", json={"key": key})

    assert resp.status == 200, (
        f"resuming a never-existed session returned {resp.status}; an absent key is a new "
        "conversation, not a deletion"
    )


@pytest.mark.asyncio
async def test_resume_does_not_apply_the_remote_binding_from_disk(tmp_path, monkeypatch):
    """The shared materialiser applies resume's field set and NEVER the remote binding.

    A session bound to a remote instance must open from History as LOCAL; remote
    rehydration is ``_rehydrate_slot_from_history``'s job (RFC 7.1b). This pins the
    negative, which a snapshot of the field list would not.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:remotebound1"
    log.append(key, "user", "history-1")
    log.append(key, "assistant", "history-2")
    # Plant the remote binding on the persisted metadata line, exactly as a
    # remote-invocation session persists it.
    log.update_metadata(
        key,
        {
            "executor": "remote",
            "instance_id": "some-instance",
            "remote_slot": "chat-99",
        },
    )
    assert log.get_metadata(key).get("executor") == "remote", "fixture did not plant the binding"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/remotebound1/resume", json={"key": key})

    assert resp.status == 200, f"resume did not publish (status {resp.status})"
    slot = state._slots.get("remotebound1")
    assert slot is not None, "the slot was never published"
    # The binding must NOT have been applied: a resumed session opens local.
    assert slot.executor == "local", (
        f"resume applied the remote executor from disk (executor={slot.executor!r}); a "
        "session that used to open locally would now try to relay to an instance that "
        "may not be connected"
    )
    assert not getattr(
        slot, "instance_id", ""
    ), f"resume applied instance_id from disk ({getattr(slot, 'instance_id', None)!r})"
    assert not getattr(
        slot, "remote_slot", ""
    ), f"resume applied remote_slot from disk ({getattr(slot, 'remote_slot', None)!r})"
    assert slot.executor != "remote", "the resumed slot is admitted as a relay archive"
    # The transcript still hydrated correctly — this is a real resume, not a
    # refusal that would pass the negatives vacuously.
    contents = [m.get("content") for m in slot.messages]
    assert [c for c in contents if c and c.startswith("history-")] == [
        "history-1",
        "history-2",
    ], f"history lost: {contents}"


@pytest.mark.asyncio
async def test_resume_still_windows_a_long_transcript_to_500(tmp_path, monkeypatch):
    """Resume keeps its 500-row on-disk window unchanged.

    ``window_limit`` lets import opt out, but resume still loads the newest 500 of 600
    rows and reports 100 older-on-disk.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:longresume1"
    for i in range(600):
        log.append(key, "assistant", f"row-{i}")

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/longresume1/resume", json={"key": key})
        data = await resp.json()

    assert resp.status == 200
    slot = state._slots.get("longresume1")
    assert slot is not None
    # Newest 500 in memory, 100 frozen on disk -- resume's window is intact.
    assert len(slot.messages) == 500, f"resume window changed: {len(slot.messages)}"
    assert slot._disk_older_count == 100, f"_disk_older_count={slot._disk_older_count}"
    assert data["total"] == 600


def test_serialize_slots_omits_a_slot_under_construction(tmp_path, monkeypatch):
    """serialize_slots hides a registered-but-under-construction slot.

    The slot stays in ``_slots`` for identity, but a client must not see a half-built
    session whose Layer B join is not yet written.
    """
    from kiro_crew.dashboard.state import _ChatSlot

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = _ChatSlot("dashboard:underconstruction1")
    state._slots[slot.key] = slot

    # Registered but under construction: hidden.
    state.begin_slot_construction(slot.key)
    keys_hidden = {s.get("key") for s in state.serialize_slots()}
    assert "dashboard:underconstruction1" not in keys_hidden, (
        "a slot under construction appeared in the payload; a client would see a "
        "half-built session"
    )

    # Construction ended: shown.
    state.end_slot_construction(slot.key)
    keys_shown = {s.get("key") for s in state.serialize_slots()}
    assert (
        "dashboard:underconstruction1" in keys_shown
    ), "the slot never became visible after construction ended"


@pytest.mark.asyncio
async def test_materialise_never_makes_the_slot_visible_before_it_is_ready(tmp_path, monkeypatch):
    """No push during materialisation may serialize the slot, including the creation frame.

    The construction mark must be set before ``get_or_create_slot``'s synchronous
    broadcast, so creation and ``begin_slot_construction`` share one
    ``suspend_slots_push`` block. Driven on a REAL DashboardState, because a stub that
    omits the broadcast would pass vacuously.
    """
    import kiro_crew.dashboard.chat_handlers as ch

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "dashboard:materialise-vis1"

    visible_at_delivery: list[bool] = []
    real_deliver = state._do_slots_broadcast

    def _spy_deliver():
        # Sample at actual DELIVERY, not at every push_slots_update call: a call
        # made while suspended is deferred and delivers nothing, so sampling
        # there would measure a frame no client receives. This fires only when a
        # broadcast is really sent.
        visible_at_delivery.append(
            any(s.get("key") == "materialise-vis1" for s in state.serialize_slots())
        )
        return real_deliver()

    monkeypatch.setattr(state, "_do_slots_broadcast", _spy_deliver)

    slot = ch._materialise_slot_from_history(
        state,
        name=key,
        history_key=key,
        meta={"title": "hi"},
        all_messages=[{"role": "assistant", "content": "a", "ts": ""}],
    )

    # The materialiser returns the slot registered + still under construction; the
    # caller publishes. So no broadcast DELIVERED during the call may serialize
    # the slot — the creation frame (deferred to the suspend block's exit, by
    # which point the construction mark is set) included.
    assert not any(visible_at_delivery), (
        f"a delivered broadcast serialized the slot during materialisation "
        f"({visible_at_delivery}); the creation frame advertised a not-yet-ready slot"
    )
    assert slot.key in state._slots, "the slot must stay registered for the same-key guard"
    assert (
        slot.key in state._slots_under_construction
    ), "the materialiser must return the slot still under construction for the caller to publish"
    # Once the caller ends construction and pushes, it becomes visible.
    state.end_slot_construction(slot.key)
    assert any(
        s.get("key") == "materialise-vis1" for s in state.serialize_slots()
    ), "the slot never became visible after construction ended"


def test_background_flush_and_open_slots_skip_an_under_construction_slot(tmp_path, monkeypatch):
    """Only the constructor persists a slot mid-build; flush and restart-restore skip it.

    A periodic ``_flush_dirty_slots`` would write a half-built transcript, and the
    open-slots set could let a crash resurrect it half-built.
    """
    from kiro_crew.dashboard.state import _ChatSlot

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = _ChatSlot("dashboard:flush1")
    state._slots[slot.key] = slot
    state.begin_slot_construction(slot.key)

    flushed: list[str] = []
    monkeypatch.setattr(
        state, "flush_slot_now", lambda s, takeover_basis=None: flushed.append(s.key)
    )

    state._flush_dirty_slots()
    assert (
        "dashboard:flush1" not in flushed
    ), "the background flush persisted a slot still under construction"

    # And it is not in the restart-restore open-slots set until construction ends.
    import json as _json

    open_slots_path = tmp_path / "open_slots.json"
    keys_hidden = set()
    if open_slots_path.exists():
        keys_hidden = set(_json.loads(open_slots_path.read_text()).get("keys", []))
    assert (
        "dashboard:flush1" not in keys_hidden
    ), "an under-construction slot was written into the restart-restore set"

    # After construction ends, the flush persists it normally.
    state.end_slot_construction(slot.key)
    flushed.clear()
    state._flush_dirty_slots()
    assert "dashboard:flush1" in flushed, "a ready slot was not flushed"


def test_live_slot_count_counts_an_under_construction_slot_once(tmp_path):
    """An in-flight resume/import must not be double-counted toward the ceiling.

    The fold keeps the slot REGISTERED in ``_slots`` throughout construction, so
    it is simultaneously in ``_slots`` and ``_slots_under_construction``. A naive
    sum of the two lengths would charge it twice and refuse admissible
    imports/forks/creates near ``MAX_LIVE_SLOTS`` with fewer real slots than that.
    """
    from kiro_crew.dashboard.slot_registry import SlotRegistry

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("count-uc")
    assert state.live_slot_count() == 1

    # Under construction AND still registered: must stay 1, not 2.
    state.begin_slot_construction(slot.key)
    assert slot.key in state._slots
    assert state.live_slot_count() == 1, "under-construction slot double-counted"

    state.end_slot_construction(slot.key)
    assert state.live_slot_count() == 1
    # SlotRegistry is imported to pin the delegation target exists.
    assert callable(SlotRegistry.live_slot_count)


def test_under_construction_slot_is_not_acquirable_during_the_async_tail(tmp_path):
    """A slot under construction is refused by both acquisition doors.

    Import holds ``begin_slot_construction`` across its async Layer B tail, so
    ``get_slot`` returns None and ``get_or_create_slot`` raises until it lands; raw
    ``_slots`` still resolves it so a same-key resume dedups.
    """
    import pytest

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("acq-tail")
    state.begin_slot_construction(slot.key)

    assert state.get_slot(slot.key) is None
    with pytest.raises(ValueError, match="still being built"):
        state.get_or_create_slot("acq-tail")
    # Registered throughout: dedup still resolves it via raw _slots.
    assert state._slots.get(slot.key) is slot

    state.end_slot_construction(slot.key)
    assert state.get_slot(slot.key) is slot
    assert state.get_or_create_slot("acq-tail") is slot


def test_raw_acquirer_mutators_refuse_an_under_construction_slot(tmp_path):
    """regenerate/rewind acquire via raw ``_slots.get``; they must still refuse
    an under-construction slot, so a crafted request against the predictable
    minted key cannot truncate+persist during the import Layer B async tail.
    """
    from kiro_crew.dashboard.chat_utils import reject_if_slot_under_construction

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("raw-uc")
    state.begin_slot_construction(slot.key)
    resp = reject_if_slot_under_construction(state, slot)
    assert resp is not None and resp.status == 409
    state.end_slot_construction(slot.key)
    assert reject_if_slot_under_construction(state, slot) is None


def test_redact_history_rows_only_touches_the_window_not_the_frozen_prefix():
    """``_redact_history_rows`` redacts only the newest ``window_limit`` rows.

    The frozen prefix is already redacted on disk and never re-serialized, so
    scrubbing it would only put regex work on the loop. ``window_limit=None`` (import)
    redacts everything.
    """
    from kiro_crew.dashboard.chat_handlers import _redact_history_rows

    secret = "ghp_" + "a" * 36  # a token-shaped string the credential scanner redacts
    rows = [{"role": "assistant", "content": f"p{i} {secret}", "ts": ""} for i in range(5)]

    out = _redact_history_rows(rows, window_limit=2)
    # Prefix (first 3) untouched: same objects, secret still present.
    assert out[0] is rows[0] and out[1] is rows[1] and out[2] is rows[2]
    assert secret in out[0]["content"]
    # Window (last 2) scrubbed: the token is gone.
    assert secret not in out[3]["content"] and secret not in out[4]["content"]

    # window_limit=None redacts every row (import's all-persisted case).
    out_all = _redact_history_rows(rows, window_limit=None)
    assert all(secret not in m["content"] for m in out_all)


def test_redact_history_rows_normalises_structured_content_to_a_redacted_string():
    """A non-string ``content`` is normalised to a redacted STRING.

    Keeping the structure is unsafe: a credential in a dict KEY would survive, and the
    save/display paths crash on non-string ``content``. This checks a token as both a
    value and a key does not survive, and that the row is still saveable.
    """
    from kiro_crew.dashboard.chat_handlers import _redact_history_rows
    from kiro_crew.dashboard.chat_persistence import _build_message_entry_uncached

    secret = "ghp_" + "b" * 36  # token-shaped; the credential scanner redacts it
    key_secret = "ghp_" + "d" * 36  # a credential sitting in a dict KEY
    nested = [
        {"type": "text", "text": f"leading {secret} trailing"},
        {"type": "text", "text": [f"deeper {secret}"]},  # list leaf, one level down
        {key_secret: "value under a secret key"},  # credential as a dict key
    ]
    original = {"role": "assistant", "content": nested, "ts": ""}
    rows = [original]

    out = _redact_history_rows(rows, window_limit=None)

    # Content is now a single string, and neither the value-token nor the
    # key-token survives anywhere in it.
    redacted = out[0]["content"]
    assert isinstance(redacted, str)
    assert secret not in redacted
    assert key_secret not in redacted
    # The caller's original nested object was not mutated in place.
    assert original["content"] is nested and secret in nested[0]["text"]
    # The accepted row is serialisable by the persistence path (which calls the
    # string-only redactors on ``content`` and would TypeError on a non-string).
    entry = _build_message_entry_uncached(out[0])
    assert entry is not None and isinstance(entry["content"], str)
    assert secret not in entry["content"] and key_secret not in entry["content"]


def test_normalise_structured_content_is_bounded_and_never_raises():
    """The normaliser must never raise and always return a string, even for a
    corrupt/hostile row: an unserialisable value or a pathologically large one
    collapses to a fixed placeholder (fail-closed) rather than leaking or
    crashing.
    """
    from kiro_crew.dashboard.chat_handlers import (
        _STRUCTURED_CONTENT_PLACEHOLDER,
        _normalise_structured_content,
    )

    secret = "ghp_" + "c" * 36
    # A normal nested structure: serialised and redacted, token gone, is a string.
    out = _normalise_structured_content([{"text": f"x {secret} y"}])
    assert isinstance(out, str) and secret not in out

    # An unserialisable value (a set contains an object json cannot encode with
    # default=str only for keys) -> a bare object is stringified via default=str,
    # but a truly non-serialisable container falls back to the placeholder.
    class _Boom:
        def __repr__(self):
            raise RuntimeError("nope")

    out2 = _normalise_structured_content({"k": _Boom()})
    assert out2 == _STRUCTURED_CONTENT_PLACEHOLDER

    # A non-container leaf still comes back as a (redacted) string.
    assert isinstance(_normalise_structured_content(42), str)
    assert _normalise_structured_content(None) == "null"


def test_named_create_on_a_retracted_under_construction_key_is_refused(tmp_path):
    """A named create on a retracted, under-construction minted key is refused.

    Import retracts the key from ``_slots`` for its async Layer B tail; a create there
    would mint a second slot sharing the effective session key. Once construction
    ends, the same named create succeeds.
    """
    import pytest

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-7-1700000000")
    key = slot.key
    state.begin_slot_construction(key)
    # Import retracts the slot from _slots for its async finalization tail.
    state._slots.pop(key, None)

    # The attack: a named create on the predicted key during the window.
    with pytest.raises(ValueError, match="still being built"):
        state.get_or_create_slot(key)
    # And it did not mint a colliding slot.
    assert key not in state._slots

    # Once finalization ends (import re-registers + ends construction), the key
    # is usable again.
    state._slots[key] = slot
    state.end_slot_construction(key)
    assert state.get_or_create_slot(key) is slot


def test_failed_hydration_does_not_leave_a_stale_restricted_marker(tmp_path, monkeypatch):
    """A hydration that raises after marking the key restricted rolls the marker back.

    Otherwise a later session at that key would be wrongly blocked from memory
    operations. The raise is forced after ``dashboard:{key}`` joins ``_restricted_keys``.
    """
    import kiro_crew.dashboard.chat_handlers as ch

    state = _make_state(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("malformed variant")

    monkeypatch.setattr(ch, "_attach_variants", _boom)

    with pytest.raises(RuntimeError, match="malformed variant"):
        ch._materialise_slot_from_history(
            state,
            name="priv-1",
            history_key="priv-1",
            meta={"memory_mode": "temporary"},
            all_messages=[{"role": "assistant", "content": "a", "ts": ""}],
        )

    assert (
        "dashboard:priv-1" not in state._restricted_keys
    ), "failed hydration left a stale restricted marker"
    assert "priv-1" not in state._slots


class _FlushAborted(BaseException):
    """What the suspend block's exit still lets through: it logs an ``Exception``."""


def test_flush_failure_at_construction_does_not_leak_the_reservation(tmp_path, monkeypatch):
    """If the suspend-block exit flush raises past its log (a ``BaseException``),
    materialisation must roll back fully: no leaked construction count, no
    registered slot. The rollback region covers creation and the context exit,
    not just the hydrate body.
    """
    state = _make_state(tmp_path)

    # Raise only on the DEFERRED exit flush, not the leading-edge push inside
    # get_or_create_slot. Count calls: the exit flush is the one that fires
    # after begin_slot_construction, with the slot already bound in the
    # materialiser scope.
    real_push = state.push_slots_update
    calls = {"n": 0}

    def _push(*a, **k):
        calls["n"] += 1
        if slot_created["done"]:
            raise _FlushAborted("poisoned broadcast")
        return real_push(*a, **k)

    slot_created = {"done": False}
    # begin_slot_construction runs after get_or_create_slot; flip the flag when
    # construction is marked so only the exit flush raises.
    real_begin = state.begin_slot_construction

    def _begin(key):
        slot_created["done"] = True
        return real_begin(key)

    monkeypatch.setattr(state, "begin_slot_construction", _begin)
    monkeypatch.setattr(state, "push_slots_update", _push)

    import kiro_crew.dashboard.chat_handlers as ch

    before = state.live_slot_count()
    with pytest.raises(_FlushAborted, match="poisoned broadcast"):
        ch._materialise_slot_from_history(
            state,
            name="flush-leak-1",
            history_key="flush-leak-1",
            meta={"title": "x"},
            all_messages=[{"role": "assistant", "content": "a", "ts": ""}],
        )
    assert "flush-leak-1" not in state._slots, "leaked a registered slot on flush failure"
    assert state.live_slot_count() == before, "leaked a construction reservation"
    assert "flush-leak-1" not in state._slots_under_construction


def test_a_failing_exit_flush_does_not_fail_materialisation(tmp_path, monkeypatch, caplog):
    """A suspend-block exit flush whose broadcast fails (a poisoned pre-existing
    slot) is logged, and the materialisation it trails completes:
    the block exited cleanly, so the slot stays registered and under
    construction for the caller's tail to finish, and hydration still runs.
    """
    state = _make_state(tmp_path)

    # Inside the block get_or_create_slot's push only marks one owed, so the
    # exit flush is the first broadcast.
    def _broadcast(*a, **k):
        raise RuntimeError("poisoned broadcast")

    monkeypatch.setattr(state, "_do_slots_broadcast", _broadcast)

    import kiro_crew.dashboard.chat_handlers as ch

    with caplog.at_level("ERROR", logger="kiro_crew.dashboard.state"):
        slot = ch._materialise_slot_from_history(
            state,
            name="flush-leak-1",
            history_key="flush-leak-1",
            meta={"title": "x"},
            all_messages=[{"role": "assistant", "content": "a", "ts": ""}],
        )
    assert state._slots.get("flush-leak-1") is slot
    assert "flush-leak-1" in state._slots_under_construction, "the caller's tail ends it"
    assert [m.get("content") for m in slot.messages] == ["a"], "hydration must still run"
    assert any(r.getMessage().startswith("Slots broadcast failed") for r in caplog.records)


@pytest.mark.asyncio
async def test_resuming_a_closed_session_refreshes_the_older_sessions_listing(
    tmp_path, monkeypatch
):
    """A resume moves the session out of Older Sessions, which every tab re-reads
    only on a ``history`` frame: publishing the slot pushes exactly one listing refresh."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:listed1"
    log.append(key, "user", "history-1")
    log.update_metadata(key, {"closed": True, "closed_at": 1_700_000_000.0})
    push_listing_refresh = MagicMock()
    monkeypatch.setattr(state, "push_listing_refresh", push_listing_refresh)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/listed1/resume", json={"key": key})

    assert resp.status == 200, f"resume did not publish (status {resp.status})"
    assert "listed1" in state._slots, "the slot was not published"
    push_listing_refresh.assert_called_once_with()


@pytest.mark.asyncio
async def test_resuming_an_already_live_session_pushes_no_history_refresh(tmp_path, monkeypatch):
    """A resume answered by the open slot publishes nothing new, so the listing
    has not changed and no ``history`` frame is sent."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    live = state.get_or_create_slot("open1")
    push_listing_refresh = MagicMock()
    monkeypatch.setattr(state, "push_listing_refresh", push_listing_refresh)
    push_refresh = MagicMock()
    monkeypatch.setattr(state, "push_refresh", push_refresh)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/open1/resume", json={"key": "dashboard:open1"})
        data = await resp.json()

    assert resp.status == 200, f"resume of a live slot failed (status {resp.status})"
    assert data["key"] == live.key and state._slots["open1"] is live, "not the dedup answer"
    push_listing_refresh.assert_not_called()
    assert call("history") not in push_refresh.call_args_list, push_refresh.call_args_list


@pytest.mark.asyncio
async def test_a_listing_during_the_resume_tail_is_corrected_on_publish(tmp_path, monkeypatch):
    """A reserved resume takes its slot out of ``_slots`` until its tail passes, after
    the creation sync already pushed ``history``. A listing served in that tail shows
    the session as an Older Sessions row, so publishing the slot pushes again."""
    from kiro_crew.dashboard.chat_handlers import resume_slot_from_history
    from kiro_crew.dashboard.chat_utils import listing_slot_snapshot

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:tail1"
    log.append(key, "user", "history-1")
    log.update_metadata(key, {"closed": True, "closed_at": 1_700_000_000.0})
    pushes: list[bool] = []
    monkeypatch.setattr(
        state, "push_listing_refresh", lambda: pushes.append("tail1" in state._slots)
    )
    listed: list[list[str]] = []

    async def _containment(_slot):
        listed.append([s.key for s in listing_slot_snapshot(state)])
        return None

    outcome = await resume_slot_from_history(
        state, name="tail1", history_key=key, containment=_containment, rebind_manager=False
    )

    assert outcome.refusal is None and "tail1" in state._slots, outcome
    assert listed and "tail1" not in listed[0], f"the tail did not run retracted: {listed}"
    assert pushes == [True, True], f"publish after the listing pushed {pushes}"
