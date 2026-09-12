"""Resume must not publish a slot by name before it holds its history.

``api_chat_slot_resume`` reads the transcript off the event loop. If that await
sits between ``get_or_create_slot`` and the hydrate loop, the slot is reachable
from ``state._slots`` while still empty, so a concurrent request that appends a
prompt gets it ordered *before* the history the resume then appends.
"""

import asyncio
import threading
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


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
        for _ in range(200):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
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

    read_entered = threading.Event()
    may_finish = threading.Event()
    original = log.read_messages_chained

    def blocking_read(key):
        read_entered.set()
        may_finish.wait(timeout=10)
        return original(key)

    monkeypatch.setattr(log, "read_messages_chained", blocking_read)

    async def racer():
        for _ in range(200):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
        # A concurrent resume from a DIFFERENT app publishes the slot mid-read.
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
            await asyncio.wait_for(unhide_entered.wait(), timeout=5)
        except asyncio.TimeoutError:
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

    ``history.delete_session`` unlinks the file and leaves NO tombstone -- its own
    docstring notes that once the delete releases the lock "a concurrent writer
    can recreate the session". Resume reads the transcript BEFORE publishing the
    slot (so the await cannot expose an empty slot by name), which means a delete
    landing inside that read leaves us holding a fully populated transcript for a
    session that does not exist. Publishing a slot from that content rewrites
    the file on its next flush.

    The probe is ``get_metadata_status``, not ``get_metadata``: the latter returns
    ``{}`` for both "deleted" and "unreadable", and treating an unreadable
    metadata line as a deletion would discard a live session.
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
        for _ in range(300):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
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

    The sibling test above covers the DELETE case, where the guard fires on
    metadata being ABSENT. Deleting and then RECREATING inside the same window
    errs in the opposite direction: ``delete_session`` leaves no tombstone and
    its docstring notes a concurrent writer can recreate the session, so the
    post-read metadata is a NON-EMPTY dict belonging to the NEW session. An
    existence-keyed guard reads that as "still here" and publishes a slot
    holding the OLD transcript, whose next flush overwrites a LIVE replacement.

    Existence cannot separate the two; identity can. Every path that mints a
    metadata line stamps ``created_at`` (``append`` on creation,
    ``_update_metadata_locked`` when the line is missing) and
    ``_rewrite_session_locked`` PRESERVES it, so a differing ``created_at`` on a
    transcript that carried one means the file was recreated rather than merely
    rewritten.
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
        for _ in range(300):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
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

    This is the control for the identity arm's breadth. A title/agent update
    rewrites the metadata line while ``_rewrite_session_locked`` and
    ``_update_metadata_locked`` both PRESERVE ``created_at``, so the session is
    the same one. A guard that compared the whole metadata dict -- rather than
    the identity field -- would refuse here, turning an ordinary concurrent
    rename into a failed resume.
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
        for _ in range(300):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
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

    The folder-existence check was hoisted above the publish so no await sits
    between the re-checks and the hydrate loop. That hoist moved it onto the
    PRE-read metadata snapshot, while the hydrate binds ``folder_id`` from the
    snapshot re-read after the last await. A channel reconciliation filing the
    session mid-read makes the two ids differ, and the verdict earned by the OLD
    folder is then applied to the NEW one: a dangling old id yields
    ``folder_unhidden=False``, which erases the live filing the reconciliation
    just made. The dirty-slot flush persists that erasure.

    A verdict is only about the folder it was computed for.
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
        for _ in range(300):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
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

    ``clear_closed`` acts on the ``meta`` snapshot taken before the threaded
    transcript read. If the session is deleted, recreated and closed inside that
    window, an unconditional clear drops a ``closed`` flag belonging to a
    DIFFERENT conversation -- and it runs BEFORE the identity re-check returns
    409, so the 409 does not undo it. The user's close is silently reversed.

    Both discriminators are covered. ``stamp_closed_at=True`` is the app close
    path (chat_handlers stamps ``closed_at``); ``False`` is a legacy flag with no
    stamp, where the file's mtime approximates the close instant -- and a
    recreated file's mtime is necessarily fresh, which is what makes the
    delete/recreate case comparable at all.
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
        for _ in range(300):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
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
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    key = "dashboard:closenormal1"
    log.append(key, "user", "history-1")
    log.update_metadata(key, {"closed": True, "closed_at": time.time()})
    assert log.get_metadata(key).get("closed"), "fixture expected a closed session"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/closenormal1/resume", json={"key": key})

    assert resp.status == 200, f"resume did not publish (status {resp.status})"
    assert not log.get_metadata(key).get("closed"), (
        "a session closed BEFORE the resume kept its `closed` flag; it will "
        "disappear again on the next gateway restart"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [False, True])
async def test_a_metadata_only_session_removed_during_the_read_is_not_republished(
    tmp_path, monkeypatch, recreate
):
    """An EMPTY transcript must not disable the deletion and identity guards.

    Both re-checks were gated on ``all_messages``, so a metadata-only session --
    one carrying a metadata line and no messages, which ``update_metadata``
    creates on upsert -- slipped past both: the transcript is empty, the term is
    falsy, and the resume published a slot for a session that had been removed
    under it. Its next flush recreates the deleted session (``recreate=False``)
    or overwrites the live replacement (``recreate=True``).

    ``all_messages`` was the wrong witness of prior existence. The pre-read
    ``meta`` is the right one: it is read synchronously before the awaits, and a
    nonempty value means the session was there when we looked. The interest the
    old gate was protecting -- resuming a legitimately empty session must not
    409 -- is carried by the OTHER terms, not by this one: ``not
    post_read_meta`` stays false while the session is still present, and the
    identity arm needs two DIFFERING ``created_at`` values.
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
        for _ in range(300):
            if read_entered.is_set():
                break
            await asyncio.sleep(0.01)
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
    """NEGATIVE CONTROL: the interest the old gate protected must survive.

    A metadata-only session that is still PRESENT has to resume normally. If the
    existence witness is widened without the other terms doing their share, every
    empty session starts answering 409 ``resume_session_deleted`` -- one silent
    bug traded for another, and this is the assertion that catches it.
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
    """The load-bearing constraint of the fold (RFC 7.1b): the shared
    materialiser applies exactly resume's field set and NEVER the remote binding.

    A session bound to a remote instance is persisted with
    ``executor="remote"`` / ``instance_id`` / ``remote_slot`` in its metadata
    line. Opening it from History must come back LOCAL — remote rehydration is
    the startup restore path's job (``_rehydrate_slot_from_history``), a
    different code path. This asserts the NEGATIVE against the exact failure
    mode, so it fails if anyone starts applying the remote binding here, rather
    than merely characterising the current field list (which a snapshot would,
    and which would still pass if the applied set were widened and the snapshot
    updated in the same commit).
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
    assert slot.is_remote is False, "the resumed slot is admitted as remote"
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

    The window/older-count is now a parameter (``window_limit``) so import can opt
    out, but resume must still pass the default 500 and report the earlier rows as
    frozen on disk. This pins that the parameterization did not quietly widen or
    narrow resume's own window: a transcript of 600 rows loads the newest 500 and
    reports 100 older-on-disk.
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

    This is the primitive the whole materialisation-visibility design rests on: a
    slot stays in ``_slots`` for identity while being built, but must not appear
    in the payload until construction ends, or a client sees (and can click) a
    half-built session whose Layer B join is not yet written. Registered +
    begin_slot_construction -> absent from serialize_slots; end -> present.
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
    """No push during materialisation may serialize the slot — including the
    leading-edge frame ``get_or_create_slot`` fires on creation.

    This is the F3 regression First Principles and GPT caught: the construction
    mark was set AFTER get_or_create_slot's synchronous broadcast, so the empty
    slot rode the creation frame before the filter could hide it. The fix wraps
    creation + begin_slot_construction in one suspend_slots_push block, deferring
    that broadcast until the mark is set. Driven on a REAL DashboardState (not a
    stub whose get_or_create_slot omits the broadcast — the fidelity gap that let
    the earlier test pass vacuously): every serialize_slots taken at a push during
    the materialise call must omit the slot, and it must appear only after.
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
    """Only the constructor persists a slot mid-build; the background flush and
    the restart-restore set both skip it.

    A periodic ``_flush_dirty_slots`` landing on an under-construction slot would
    write a half-built transcript and could race the constructor's own save; and
    the open-slots set must not list a slot a crash could then resurrect
    half-built. Both skip on ``_slots_under_construction`` membership.
    """
    from kiro_crew.dashboard.state import _ChatSlot

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = _ChatSlot("dashboard:flush1")
    state._slots[slot.key] = slot
    state.begin_slot_construction(slot.key)

    flushed: list[str] = []
    monkeypatch.setattr(state, "flush_slot_now", lambda s: flushed.append(s.key))

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

    Construction is synchronous through the hydrate loop, but the import path
    holds ``begin_slot_construction`` across its async Layer B finalization tail,
    so the slot is registered-but-not-yet-resumable for that span. ``get_slot``
    returns None and ``get_or_create_slot`` raises, so a concurrent acquirer
    cannot treat it as a live session before Layer B lands; raw ``_slots`` still
    resolves it so a same-key resume dedups.
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

    Redacting the frozen prefix would put transcript-sized GIL regex on the loop
    for bytes that are never re-serialized (the prefix is already redacted on disk
    and only counted). With ``window_limit=2`` on 5 rows, the 3 prefix rows are
    returned untouched (same objects) and only the last 2 non-user rows are
    scrubbed. ``window_limit=None`` (import) redacts everything.
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


def test_named_create_on_a_retracted_under_construction_key_is_refused(tmp_path):
    """The retract-window hijack: a named create on the predictable minted key,
    while that key is retracted from ``_slots`` but still under construction,
    must be refused -- otherwise it mints a second slot sharing the effective
    session key the import's Layer B was joined to.

    Reproduces the exact window: begin construction on the key, then RETRACT it
    from ``_slots`` (as import does for its async Layer B tail), then issue a
    ``get_or_create_slot(name=<key>)`` -- the create path, since the key is absent
    from ``_slots``. It must raise, not mint. Once construction ends, the same
    named create succeeds.
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
    """A hydration that raises after marking the key restricted must roll the
    marker back, or a later session at that key is wrongly blocked from memory
    operations (fail-closed over-restriction).

    Non-persistent ``memory_mode`` adds ``dashboard:{key}`` to
    ``_restricted_keys`` early in hydration; force a raise AFTER that add and
    assert the except path discarded it (and dropped the slot).
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


def test_flush_failure_at_construction_does_not_leak_the_reservation(tmp_path, monkeypatch):
    """If the suspend-block exit flush raises (a poisoned pre-existing slot's
    push_slots_update), materialisation must roll back fully: no leaked
    construction count, no registered slot. The rollback region covers creation
    and the context exit, not just the hydrate body.
    """
    state = _make_state(tmp_path)

    # Raise only on the DEFERRED exit flush (F2's scenario: a poisoned slot
    # field fails serialize_slots at block exit), not the leading-edge push
    # inside get_or_create_slot. Count calls: the exit flush is the one that
    # fires after begin_slot_construction, with the slot already bound in the
    # materialiser scope.
    real_push = state.push_slots_update
    calls = {"n": 0}

    def _push(*a, **k):
        calls["n"] += 1
        if slot_created["done"]:
            raise RuntimeError("poisoned broadcast")
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
    with pytest.raises(RuntimeError, match="poisoned broadcast"):
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
