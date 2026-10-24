"""A refused or failed ``POST /api/chat/slots`` leaves no tab behind.

A slot registered before a step that then refuses would answer 503 and stay
registered, so the coalesced broadcast would publish it and every retry would
add another tab with no session behind it.

The create checks the assignment before it builds anything, builds the slot
privately with its key reserved, and publishes it only after the last step that
can refuse (``slot_create_transaction.PendingSlotCreate``). Every wait below is
bounded and every held step is released or cancelled on the way out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import slot_create_publish_helpers
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from slot_create_publish_helpers import (
    WAIT,
    _config,
    _drain,
    _HeldRecord,
    _pending,
    _post,
    _slots_app,
    _until,
    _use_config,
)

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import slot_create_transaction as txn
from kiro_crew.execution_context import read_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import async_wait_until

#: The shared fixture, bound here so pytest finds it for this module's tests.
state = slot_create_publish_helpers.state


#: A crewmate whose private store has no persisted identity: the member
#: identity check refuses it, which is the 503 a create can meet.
_UNPROVISIONED = "crew-unprovisioned"


def _config_with_unprovisioned_member() -> KiroCrewConfig:
    cfg = _config(**{_UNPROVISIONED: None})
    # Its private store exists; the identity that owns it does not.
    cfg.agents[_UNPROVISIONED].member_id = ""
    return cfg


def _count_pushes(state: Any, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every slots frame the create publishes, as the slot keys it carries."""
    frames: list[list[str]] = []
    original = state._do_slots_broadcast

    def broadcast(*args: Any, **kwargs: Any) -> Any:
        frames.append(sorted(state._slots))
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "_do_slots_broadcast", broadcast)
    return frames


def _not_published(state: Any, key: str = "demo") -> bool:
    return key not in state._slots and key in state._slots_under_construction


# -- the reported repro: a retry adds no tab -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["demo", None])
async def test_a_retry_after_a_refused_create_leaves_no_ghost_tab(
    state: Any, monkeypatch: pytest.MonkeyPatch, name: str | None
) -> None:
    # A crewmate whose memory identity was never persisted: the member
    # identity check refuses its assignment.
    _use_config(monkeypatch, _config_with_unprovisioned_member())
    before = set(state._slots)
    payload: dict[str, Any] = {"agent": _UNPROVISIONED}
    if name:
        payload["name"] = name
    for _attempt in range(3):
        status, body = await _post(state, payload)
        assert status == 503, body
        assert body["code"] == "store_unavailable"
    assert set(state._slots) == before
    assert not state._slots_under_construction
    assert not ConversationLog().has_log("dashboard:demo")


@pytest.mark.asyncio
async def test_a_refusal_found_before_the_build_builds_nothing(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(monkeypatch, _config_with_unprovisioned_member())
    built: list[Any] = []
    original = state.prepare_slot

    def spy(*args: Any, **kwargs: Any) -> Any:
        built.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "prepare_slot", spy)
    status, _body = await _post(state, {"name": "demo", "agent": _UNPROVISIONED})
    assert status == 503
    assert built == [], "the refused create reached the slot constructor"


@pytest.mark.asyncio
async def test_a_refusal_found_before_the_build_makes_no_session_folder(
    state: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # The pre-build assignment check runs before ``create_session_folder``,
    # which makes a directory the give-up does not remove. A refused crewmate
    # must leave the session folder root as it was.
    #
    # Mutation pin: drop the pre-build check (``_check_new_slot_assignment``)
    # and the assignment refuses later, after the session folder was made.
    cfg = _config_with_unprovisioned_member()
    root = tmp_path / "session-root"
    root.mkdir()
    cfg.dashboard.session_folder_root = str(root)
    _use_config(monkeypatch, cfg)
    status, body = await _post(state, {"agent": _UNPROVISIONED})
    assert status == 503, body
    assert body["code"] == "store_unavailable"
    assert list(root.iterdir()) == [], "the refused create left a session folder behind"
    assert not state._slots_under_construction


# -- a refusal after the build publishes nothing, one test per fallible step ----


def _refuse_after_build(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    """Make *step* refuse while the pre-build check still passes."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(f"{step} refused")

    async def refuse_async(*_args: Any, **_kwargs: Any) -> Any:
        refuse()

    if step == "pin":
        monkeypatch.setattr(chat_handlers, "pin_private_agent_store", refuse_async)
    elif step == "record":
        monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", refuse_async)
    elif step == "resolve":
        calls = {"n": 0}
        real = chat_handlers.resolve_agent_bindings

        def resolve_once(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            # The handler's own resolution and the pre-build check pass; the
            # assignment's resolution refuses.
            if calls["n"] >= 3:
                refuse()
            return real(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve_once)


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["pin", "resolve", "record"])
async def test_a_step_refused_before_publish_leaves_no_slot(
    state: Any, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    _use_config(monkeypatch, _config(worker={}))
    frames = _count_pushes(state, monkeypatch)
    _refuse_after_build(monkeypatch, step)
    status, body = await _post(state, {"name": "demo", "agent": "worker"})
    assert status == 503, body
    assert "demo" not in state._slots
    assert not state._slots_under_construction
    assert all("demo" not in frame for frame in frames)
    # The pin (written before the selection) is put back by compare-and-set.
    assert read_session_execution("dashboard:demo") is None
    if step != "record":
        # Nothing was written at all. A selection refused after the pin wrote
        # leaves that write's metadata-only file: no transcript is deleted.
        assert not ConversationLog().has_log("dashboard:demo")


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["deny", "resolve", "pin", "record"])
async def test_every_fallible_step_runs_before_the_slot_is_published(
    state: Any, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Mutation pin: publish the slot before any of these steps and this goes red."""
    _use_config(monkeypatch, _config(worker={}))
    seen: list[bool] = []
    if step == "deny":
        real_deny = chat_handlers.deny_app_slot_session_access

        def deny(*args: Any, **kwargs: Any) -> Any:
            seen.append(_not_published(state))
            return real_deny(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "deny_app_slot_session_access", deny)
    elif step == "resolve":
        real_resolve = chat_handlers.resolve_agent_bindings

        def resolve(*args: Any, **kwargs: Any) -> Any:
            if "demo" in state._slots_under_construction:
                seen.append(_not_published(state))
            return real_resolve(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
    elif step == "pin":
        real_pin = chat_handlers.pin_private_agent_store

        async def observed_pin(*args: Any, **kwargs: Any) -> Any:
            seen.append(_not_published(state))
            return await real_pin(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "pin_private_agent_store", observed_pin)
    else:
        real_record = chat_handlers._record_explicit_agent_selection

        async def observed_record(*args: Any, **kwargs: Any) -> Any:
            seen.append(_not_published(state))
            return await real_record(*args, **kwargs)

        monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", observed_record)
    status, body = await _post(state, {"name": "demo", "agent": "worker"})
    assert status == 200, body
    assert seen and all(seen), f"the slot was published before {step} ran"
    assert "demo" in state._slots and not state._slots_under_construction


@pytest.mark.asyncio
async def test_an_app_denial_after_the_build_publishes_nothing(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(monkeypatch, _config())

    def deny(*_args: Any, **_kwargs: Any) -> web.Response:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    monkeypatch.setattr(chat_handlers, "deny_app_slot_session_access", deny)
    status, _body = await _post(state, {"name": "demo"})
    assert status == 404
    assert "demo" not in state._slots
    assert not state._slots_under_construction


@pytest.mark.asyncio
async def test_a_committed_create_keeps_its_slot_and_binding(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(monkeypatch, _config(worker={}))
    status, body = await _post(state, {"name": "demo", "agent": "worker"})
    assert status == 200, body
    assert state._slots["demo"].agent == "worker"
    assert read_session_execution("dashboard:demo") is not None


# -- cancellation --------------------------------------------------------------


def _capturing_app(state: Any) -> tuple[list[asyncio.Task[Any]], web.Application]:
    tasks: list[asyncio.Task[Any]] = []

    async def capture(request: web.Request) -> web.Response:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        return await chat_handlers.api_chat_slot_create(request)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", capture)
    return tasks, app


@pytest.mark.asyncio
async def test_a_cancelled_create_publishes_nothing(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=False)
    tasks, app = _capturing_app(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            assert _not_published(state)
            await _drain(tasks[0])
        finally:
            held.release.set()
            await _drain(post, *tasks)
    assert "demo" not in state._slots
    assert not state._slots_under_construction
    assert read_session_execution("dashboard:demo") is None


@pytest.mark.asyncio
async def test_a_create_cancelled_while_its_pin_waits_leaves_no_orphan_binding(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin writes from a thread a cancellation cannot stop.

    Mutation pin: await the pin directly (no ``run_to_completion``) and the
    create unwinds before the pin writes, so its binding outlives the create.
    """
    import threading

    _use_config(monkeypatch, _config(worker={}))
    loop = asyncio.get_running_loop()
    real_pin = chat_handlers.pin_private_agent_store
    entered = asyncio.Event()
    gate, written = threading.Event(), threading.Event()

    async def pin_in_a_thread(*args: Any, **kwargs: Any) -> str:
        def write() -> str:
            gate.wait(WAIT)
            try:
                return asyncio.run_coroutine_threadsafe(real_pin(*args, **kwargs), loop).result(
                    WAIT
                )
            finally:
                written.set()

        entered.set()
        return await asyncio.to_thread(write)

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin_in_a_thread)
    tasks, app = _capturing_app(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(entered.wait(), WAIT)
            tasks[0].cancel()
            gate.set()
            await _drain(tasks[0])
            await async_wait_until(written.is_set, describe=lambda: "the pin never finished")
        finally:
            gate.set()
            await _drain(post, *tasks)
    assert "demo" not in state._slots
    assert not state._slots_under_construction
    assert read_session_execution("dashboard:demo") is None, "the pin outlived the create"


# -- other requests while a create is pending ----------------------------------


@pytest.mark.asyncio
async def test_a_send_to_a_pending_name_waits_and_lands_on_the_committed_slot(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No turn can start on a slot that is not published, and the send is not refused.

    An opener that is not a create waits for the pending create
    (``wait_for_pending_create``), then opens the slot it published, with the
    create's agent and member pin: the end state of main, where the create
    registered first and the opener got its slot back.

    Mutation pin: return at once from ``wait_for_pending_create`` and the opener
    is refused "still being built".
    """
    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=False)

    async def send_opener() -> Any:
        await txn.wait_for_pending_create(state, "demo")
        return state.get_or_create_slot("demo")

    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        create = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        opener: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            assert state.get_slot("demo") is None
            opener = asyncio.create_task(send_opener())
            await asyncio.sleep(0)
            assert not opener.done()
            held.release.set()
            assert (await asyncio.wait_for(create, WAIT)).status == 200
            opened = await asyncio.wait_for(opener, WAIT)
        finally:
            held.release.set()
            await _drain(create, *([opener] if opener is not None else []))
    assert state._slots["demo"] is opened
    assert opened.agent == "worker"
    assert read_session_execution("dashboard:demo") is not None


@pytest.mark.asyncio
async def test_a_hung_create_does_not_stall_an_opener_of_another_key(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait is per key: a create held on ``demo`` leaves an opener of ``other`` alone.

    The opener returns and opens its own slot while the ``demo`` create is still
    unpublished. That is shown by order: the test releases the held create only
    after the opener has returned.

    Mutation pin: make ``wait_for_pending_create`` wait for any pending create
    and the opener blocks until ``held.release``, which only this test sets and
    only after the opener returns, so the wait times out.
    """
    _use_config(monkeypatch, _config(worker={}))
    monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 3 * WAIT)
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        create = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            await asyncio.wait_for(txn.wait_for_pending_create(state, "other"), WAIT)
            opened = state.get_or_create_slot("other")
            assert not held.release.is_set()
            assert txn._pending_create(state, "demo") is not None
            assert state.get_slot("demo") is None
            held.release.set()
            assert (await asyncio.wait_for(create, WAIT)).status == 200
        finally:
            held.release.set()
            await _drain(create)
    assert state._slots["other"] is opened


@pytest.mark.asyncio
async def test_an_app_worker_slot_acquire_waits_for_a_same_key_create(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``acquire_worker_slot`` waits for a pending same-key create, then judges its slot.

    The create publishes a person's slot, which an app may not adopt, so the
    acquire is refused for that reason and not for the key being mid-build.

    Mutation pin: drop the wait in ``acquire_worker_slot`` and the acquire is
    refused "still being built" while the create is held.
    """
    from kiro_crew.apps.worker_slots import acquire_worker_slot

    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        create = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        acquire: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            acquire = asyncio.create_task(
                acquire_worker_slot(state, "demo-app", "demo", project="/work/repo")
            )
            await asyncio.sleep(0)
            assert not acquire.done()
            held.release.set()
            assert (await asyncio.wait_for(create, WAIT)).status == 200
            with pytest.raises(ValueError, match="belongs to a person"):
                await asyncio.wait_for(acquire, WAIT)
        finally:
            held.release.set()
            await _drain(create, *([acquire] if acquire is not None else []))
    assert state._slots["demo"]._app == ""


@pytest.mark.asyncio
async def test_a_wait_for_a_refused_create_opens_the_free_key(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=True)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        create = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        waiter: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            waiter = asyncio.create_task(txn.wait_for_pending_create(state, "demo"))
            await asyncio.sleep(0)
            assert not waiter.done()
            held.release.set()
            assert (await asyncio.wait_for(create, WAIT)).status == 503
            await asyncio.wait_for(waiter, WAIT)
        finally:
            held.release.set()
            await _drain(create, *([waiter] if waiter is not None else []))
    assert "demo" not in state._slots and not state._slots_under_construction
    assert state.get_or_create_slot("demo") is state._slots["demo"]


@pytest.mark.asyncio
async def test_the_wait_for_a_pending_create_is_bounded(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = _pending(state)
    monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 0)
    await asyncio.wait_for(txn.wait_for_pending_create(state, "demo"), WAIT)
    with pytest.raises(ValueError, match="still being built"):
        state.get_or_create_slot("demo")
    await create.settle()
    await asyncio.wait_for(txn.wait_for_pending_create(state, "demo"), WAIT)
    assert not txn._PENDING_CREATES.get(state)


@pytest.mark.asyncio
async def test_a_create_begun_before_the_waiter_resumes_is_waited_for_too(state: Any) -> None:
    """An opener waits out a second create of the key within the one bound.

    A gives the key up and wakes the opener; B begins before the opener runs.
    The opener waits for B as well, and opens the slot B publishes.

    Mutation pin: wait only for the create found at entry and the open is
    refused "still being built" while B holds the key.
    """
    a = _pending(state)
    opening = asyncio.ensure_future(txn.open_named_slot(state, "demo"))
    try:
        await asyncio.sleep(0)
        assert not opening.done()
        await a.settle()
        b = _pending(state)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not opening.done(), opening.exception() if opening.done() else None
        published = b.publish()
        assert await asyncio.wait_for(opening, WAIT) is published
    finally:
        await _drain(opening)
    assert not txn._PENDING_CREATES.get(state)


@pytest.mark.asyncio
async def test_a_close_put_back_under_a_pending_create_wins_and_refuses_the_create(
    state: Any,
) -> None:
    """``publish_slot`` refuses a key a close put its slot back under meanwhile.

    The close pops its slot, the create reserves the free key, and the close's
    failed archive puts the slot back. The put-back wins: the publish
    raises, the create gives the key up, and the close's slot stays registered.

    Mutation pin: drop the occupied-key check in ``publish_slot`` and the create
    silently replaces the slot the close put back.
    """
    old = state.get_or_create_slot("demo")
    state._slots.pop("demo")
    create = _pending(state)
    state._slots["demo"] = old
    with pytest.raises(ValueError, match="opened while it was being built"):
        create.publish()
    await create.settle()
    assert state._slots["demo"] is old
    assert "demo" not in state._slots_under_construction
    assert not txn._PENDING_CREATES.get(state)


@pytest.mark.asyncio
async def test_a_create_answers_409_when_a_close_put_back_lands_before_it_publishes(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/chat/slots`` answers the retryable 409 and leaves the close's slot alone.

    Mutation pin: drop the occupied-key check in ``publish_slot`` and the create
    answers 200 over a slot it replaced.
    """
    _use_config(monkeypatch, _config(worker={}))
    old = state.get_or_create_slot("demo")
    old.title = "kept"
    state._slots.pop("demo")
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        create = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            state._slots["demo"] = old
            held.release.set()
            resp = await asyncio.wait_for(create, WAIT)
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "slot_under_construction"
        finally:
            held.release.set()
            await _drain(create)
    assert state._slots["demo"] is old
    assert old.title == "kept" and old.agent != "worker"
    assert "demo" not in state._slots_under_construction
    assert not txn._PENDING_CREATES.get(state)


@pytest.mark.asyncio
async def test_a_folder_patch_on_a_pending_name_is_404_and_nothing_is_lost(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metadata write cannot land on a slot that is not published.

    It is answered 404, as for any missing name, so no acknowledged write can
    be taken away afterwards.
    """
    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        create = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            patch = await asyncio.wait_for(
                client.patch("/api/chat/slots/demo/folder", json={"folder_id": ""}), WAIT
            )
            assert patch.status == 404
            held.release.set()
            assert (await asyncio.wait_for(create, WAIT)).status == 200
        finally:
            held.release.set()
            await _drain(create)
    assert "demo" in state._slots


@pytest.mark.asyncio
async def test_a_channel_append_on_a_pending_key_survives_the_refusal(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that appends to the transcript directly (a Slack delivery) is never undone.

    The create deletes no transcript, so the message stays whatever the create
    decides.
    """
    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=True)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        create = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            await asyncio.to_thread(
                ConversationLog().append, "dashboard:demo", "user", "from the channel"
            )
            held.release.set()
            assert (await asyncio.wait_for(create, WAIT)).status == 503
        finally:
            held.release.set()
            await _drain(create)
    assert "demo" not in state._slots
    assert ConversationLog().has_messages("dashboard:demo"), "the channel message was deleted"


def _waiting_creates(state: Any, key: str = "demo") -> int:
    gate = txn._NAME_GATES.get(state, {}).get(key)
    return gate.users if gate is not None else 0


async def _second_create_while_first_holds(
    state: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    refuse: bool,
    check: Callable[[Any, Any], Awaitable[None]],
) -> None:
    held = _HeldRecord(monkeypatch, refuse=refuse)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        a = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        b: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            b = asyncio.create_task(
                client.post("/api/chat/slots", json={"name": "demo", "title": "B's chat"})
            )
            # B is waiting for A's name, not running beside it.
            await _until(lambda: _waiting_creates(state) == 2)
            assert not b.done()
            held.release.set()
            await check(await asyncio.wait_for(a, WAIT), await asyncio.wait_for(b, WAIT))
        finally:
            held.release.set()
            await _drain(a, *([b] if b is not None else []))
    assert _waiting_creates(state) == 0


@pytest.mark.asyncio
async def test_a_same_name_create_waits_and_opens_the_committed_slot(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(monkeypatch, _config(worker={}))

    async def check(a: Any, b: Any) -> None:
        assert a.status == 200, await a.text()
        assert b.status == 200, await b.text()
        assert (await b.json())["title"] == "B's chat"

    await _second_create_while_first_holds(state, monkeypatch, refuse=False, check=check)
    assert state._slots["demo"].agent == "worker"
    assert state._slots["demo"].title == "B's chat"
    assert read_session_execution("dashboard:demo") is not None


@pytest.mark.asyncio
async def test_a_same_name_create_waits_and_opens_the_name_a_refusal_left_free(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal of the first create leaves the second create's slot and title.

    B waits on the name while A is held, so B never sees A's slot and A's
    refusal has nothing of B's to remove. B opens the name once A has given it
    up, and keeps its slot and its title.
    """
    _use_config(monkeypatch, _config(worker={}))

    async def check(a: Any, b: Any) -> None:
        assert a.status == 503, await a.text()
        assert b.status == 200, await b.text()
        assert (await b.json())["title"] == "B's chat"

    await _second_create_while_first_holds(state, monkeypatch, refuse=True, check=check)
    assert state._slots["demo"].title == "B's chat"
    assert ConversationLog().has_log("dashboard:demo"), "B's title write was lost"


@pytest.mark.asyncio
async def test_a_same_name_create_past_the_wait_bound_is_refused(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(monkeypatch, _config(worker={}))
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        a = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            # A holds the name; B may not wait at all.
            monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 0)
            b = await asyncio.wait_for(client.post("/api/chat/slots", json={"name": "demo"}), WAIT)
            assert b.status == 409
            assert "still being built" in (await b.json())["error"]
            held.release.set()
            assert (await asyncio.wait_for(a, WAIT)).status == 200
        finally:
            held.release.set()
            await _drain(a)
    assert _waiting_creates(state) == 0


@pytest.mark.asyncio
async def test_a_cron_create_waiting_on_the_name_is_judged_after_the_first_commits(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cron admission runs inside the name wait, against what the first create published.

    The owner's create of ``demo`` is pending (no slot, no transcript), so a
    cron create of ``demo`` judged before the wait would be admitted as a mint,
    then open the owner's slot and refile it. Judged inside the wait, it sees
    the owner's slot and is refused ``not_creator``.

    Mutation pin: take the name wait after ``cron_creator_admission`` and the
    cron gets a 200 on a slot it did not create.
    """
    _use_config(monkeypatch, _config(worker={}))

    async def creator(request: web.Request) -> str:
        return request.headers.get("X-Test-Cron", "")

    monkeypatch.setattr(chat_handlers, "cron_slot_creator", creator)
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        owner = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "demo", "agent": "worker"})
        )
        cron: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            cron = asyncio.create_task(
                client.post(
                    "/api/chat/slots",
                    json={"name": "demo", "artifact": "cron-doc"},
                    headers={"X-Test-Cron": "cron:job1"},
                )
            )
            await _until(lambda: _waiting_creates(state) == 2)
            held.release.set()
            assert (await asyncio.wait_for(owner, WAIT)).status == 200
            refused = await asyncio.wait_for(cron, WAIT)
            assert refused.status == 403, await refused.text()
            assert (await refused.json())["code"] == "not_creator"
        finally:
            held.release.set()
            await _drain(owner, *([cron] if cron is not None else []))
    assert state._slots["demo"]._artifact != "cron-doc"
    assert not state._slots["demo"]._created_by


# -- a sync failure does not mask the create's answer ---------------------------


@pytest.mark.asyncio
async def test_a_sync_failure_does_not_mask_the_refusal(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal's 503 reaches the caller even when the active-slot sync raises."""
    _use_config(monkeypatch, _config(worker={}))

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("pin refused")

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", refuse)
    state.sessions.set_active_dashboard_slots.side_effect = RuntimeError("sync failed")
    status, _body = await _post(state, {"name": "demo", "agent": "worker"})
    assert status == 503
    assert "demo" not in state._slots


# -- PendingSlotCreate and the name gate on their own --------------------------


@pytest.mark.asyncio
async def test_a_create_given_up_releases_its_key_and_undoes_its_own_writes(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    undone: list[tuple[str, Any]] = []
    monkeypatch.setattr(txn, "restore_agent_selection", lambda key, change: undone.append(key))
    create = _pending(state)
    assert _not_published(state)
    create.binding_written("dashboard:demo", (None, {"written": True}))
    create.binding_written("slack:1700000000.000100", (None, {"written": True}))
    await create.settle()
    await create.settle()
    assert undone == ["dashboard:demo"], "a write outside the reserved key was undone"
    assert "demo" not in state._slots and not state._slots_under_construction


@pytest.mark.asyncio
async def test_a_linked_slot_never_undoes_a_write_to_its_published_session(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    undone: list[str] = []
    monkeypatch.setattr(txn, "restore_agent_selection", lambda key, change: undone.append(key))
    create = _pending(state, linked_session_key="slack:1700000000.000100")
    create.binding_written("slack:1700000000.000100", (None, {"written": True}))
    await create.settle()
    assert undone == []


@pytest.mark.asyncio
async def test_an_undo_that_fails_is_logged_and_still_frees_the_key(
    state: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*_args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(txn, "restore_agent_selection", fail)
    create = _pending(state)
    create.binding_written("dashboard:demo", (None, {"written": True}))
    with caplog.at_level(logging.WARNING, logger=txn.__name__):
        await create.settle()
    assert "could not put its binding back" in caplog.text
    assert not state._slots_under_construction


def _write_line(
    log: ConversationLog, key: str, fields: dict[str, Any]
) -> tuple[bytes, bytes | None]:
    """Merge *fields* into *key*'s line as the birth write does; (written, prior)."""
    with log._locked(key):
        prior = log.first_line_bytes_locked(key)
        log._update_metadata_locked(key, fields)
        written = log.first_line_bytes_locked(key)
    assert written is not None
    return written, prior


def _reterminate(log: ConversationLog, key: str, eol: bytes) -> bytes:
    """End every line of *key*'s file with *eol*, so each shape runs on every OS."""
    path = log._path(key)
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", eol)
    path.write_bytes(data)
    log._invalidate_cache(key)
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf", "crlf"])
async def test_a_given_up_create_puts_its_line_back_byte_for_byte(state: Any, eol: bytes) -> None:
    log = state.conversation_log
    log.update_metadata("dashboard:demo", {"title": "Kept", "pinned": True})
    log.append("dashboard:demo", "user", "an earlier turn")
    path = log._path("dashboard:demo")
    before = _reterminate(log, "dashboard:demo", eol)
    create = _pending(state)
    written, prior = _write_line(log, "dashboard:demo", {"title": "", "pinned": False})
    create.line_written(log, "dashboard:demo", written, prior)
    assert path.read_bytes() != before
    await create.settle()
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_a_given_up_create_removes_a_transcript_only_it_wrote(state: Any) -> None:
    log = state.conversation_log
    create = _pending(state)
    written, prior = _write_line(log, "dashboard:demo", {"title": "newborn"})
    assert prior is None
    create.line_written(log, "dashboard:demo", written, prior)
    await create.settle()
    assert not log._path("dashboard:demo").exists()


@pytest.mark.asyncio
async def test_a_given_up_create_leaves_a_line_rewritten_since(state: Any) -> None:
    # The restore is a compare-and-set: a line someone else wrote after the
    # create's write is theirs, and so is every row after it.
    log = state.conversation_log
    log.update_metadata("dashboard:demo", {"title": "Kept"})
    create = _pending(state)
    written, prior = _write_line(log, "dashboard:demo", {"title": "newborn"})
    create.line_written(log, "dashboard:demo", written, prior)
    log.update_metadata("dashboard:demo", {"title": "Later"})
    log.append("dashboard:demo", "user", "a later turn")
    after = log._path("dashboard:demo").read_bytes()
    await create.settle()
    assert log._path("dashboard:demo").read_bytes() == after


@pytest.mark.asyncio
async def test_a_published_create_keeps_its_line(state: Any) -> None:
    log = state.conversation_log
    create = _pending(state)
    written, prior = _write_line(log, "dashboard:demo", {"title": "newborn"})
    create.line_written(log, "dashboard:demo", written, prior)
    create.publish()
    await create.settle()
    assert log._path("dashboard:demo").read_bytes() == written


@pytest.mark.asyncio
async def test_a_cancel_during_the_line_undo_still_undoes_the_bindings(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A shutdown cancels the request while the settle puts the metadata line
    # back. The line undo runs on a worker thread that cannot be stopped, and
    # the binding undo after it must run too before the cancellation propagates:
    # ``settle`` runs once, so a skipped binding undo is never retried.
    #
    # Mutation pin: take the binding undo in ``_undo_unpublished`` out of the
    # ``finally`` around the line undo and the cancellation leaves after the
    # line undo, with the binding still written.
    import threading

    undone: list[str] = []
    monkeypatch.setattr(txn, "restore_agent_selection", lambda key, change: undone.append(key))
    log = state.conversation_log
    started = threading.Event()
    release = threading.Event()
    real_restore = log.restore_first_line_if

    def held_restore(*args: Any) -> Any:
        started.set()
        assert release.wait(30), "the test never released the line undo"
        return real_restore(*args)

    monkeypatch.setattr(log, "restore_first_line_if", held_restore)
    create = _pending(state)
    written, prior = _write_line(log, "dashboard:demo", {"title": "newborn"})
    create.line_written(log, "dashboard:demo", written, prior)
    create.binding_written("dashboard:demo", (None, {"written": True}))
    settling = asyncio.ensure_future(create.settle())
    await async_wait_until(started.is_set, describe=lambda: "the line undo never started")
    settling.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(settling), WAIT)
    assert undone == ["dashboard:demo"], "the cancellation skipped the binding undo"
    assert not log._path("dashboard:demo").exists()
    assert "demo" not in state._slots and not state._slots_under_construction


@pytest.mark.asyncio
async def test_loop_teardown_cancelling_the_undo_task_still_undoes_the_bindings(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``asyncio.run``'s teardown cancels every task, including the inner one
    # ``drained_to_thread`` shields. That cancellation surfaces from the line
    # undo's await once its thread is done, and the binding undo must still run.
    #
    # Mutation pin: take the binding undo in ``_undo_unpublished`` out of the
    # ``finally`` around the line undo, and the teardown cancellation skips it.
    import threading

    undone: list[str] = []
    monkeypatch.setattr(txn, "restore_agent_selection", lambda key, change: undone.append(key))
    log = state.conversation_log
    started = threading.Event()
    release = threading.Event()
    restored = threading.Event()
    real_restore = log.restore_first_line_if

    def held_restore(*args: Any) -> Any:
        started.set()
        assert release.wait(30), "the test never released the line undo"
        try:
            return real_restore(*args)
        finally:
            restored.set()

    monkeypatch.setattr(log, "restore_first_line_if", held_restore)
    create = _pending(state)
    written, prior = _write_line(log, "dashboard:demo", {"title": "newborn"})
    create.line_written(log, "dashboard:demo", written, prior)
    create.binding_written("dashboard:demo", (None, {"written": True}))
    before = asyncio.all_tasks()
    settling = asyncio.ensure_future(create.settle())
    await async_wait_until(started.is_set, describe=lambda: "the line undo never started")
    # Cancel what teardown would: the settle and the shielded undo task it runs.
    spawned = asyncio.all_tasks() - before
    assert settling in spawned and len(spawned) >= 2, "the undo did not run as its own task"
    for task in spawned:
        task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(settling), WAIT)
    assert undone == ["dashboard:demo"], "the teardown cancellation skipped the binding undo"
    # Teardown can cancel the worker thread's own wrapper task too; the thread
    # still finishes the line undo, which is what this waits for.
    await async_wait_until(restored.is_set, describe=lambda: "the line undo never finished")
    assert not log._path("dashboard:demo").exists()
    assert "demo" not in state._slots and not state._slots_under_construction


@pytest.mark.asyncio
async def test_a_published_create_keeps_everything(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    undone: list[str] = []
    monkeypatch.setattr(txn, "restore_agent_selection", lambda key, change: undone.append(key))
    create = _pending(state)
    create.binding_written("dashboard:demo", (None, {"written": True}))
    slot = create.publish()
    await create.settle()
    assert state._slots.get("demo") is slot
    assert not state._slots_under_construction
    assert undone == []


@pytest.mark.asyncio
async def test_the_name_gate_forgets_a_name_once_no_create_holds_it(state: Any) -> None:
    release = await txn.acquire_slot_create_name(state, "demo")
    assert release is not None and _waiting_creates(state) == 1
    release()
    release()
    assert "demo" not in txn._NAME_GATES.get(state, {})
