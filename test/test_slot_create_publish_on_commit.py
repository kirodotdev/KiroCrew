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
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_folders, chat_handlers
from kiro_crew.dashboard import slot_create_transaction as txn
from kiro_crew.execution_context import read_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.memory_stores import provision_member_memory

#: The bound on every wait in this file.
WAIT = 5


def _config(**members: Any) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    for name in members:
        cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, name)
    return cfg


@pytest.fixture
def state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    dashboard_state = _make_state(tmp_path)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    return dashboard_state


def _use_config(monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig) -> None:
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))


def _count_pushes(state: Any, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every slots frame the create publishes, as the slot keys it carries."""
    frames: list[list[str]] = []
    original = state._do_slots_broadcast

    def broadcast(*args: Any, **kwargs: Any) -> Any:
        frames.append(sorted(state._slots))
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "_do_slots_broadcast", broadcast)
    return frames


def _slots_app(state: Any) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_patch("/api/chat/slots/{slot}/folder", chat_folders.api_chat_slot_folder)
    return app


async def _post(state: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        resp = await asyncio.wait_for(client.post("/api/chat/slots", json=payload), WAIT)
        return resp.status, await resp.json()


def _not_published(state: Any, key: str = "demo") -> bool:
    return key not in state._slots and key in state._slots_under_construction


async def _drain(*tasks: asyncio.Task[Any]) -> None:
    """Cancel whatever is still running and wait for all of it, bounded."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), WAIT)


class _HeldRecord:
    """The first create's selection write stops until released, then writes or refuses."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, refuse: bool) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.seen: list[bool] = []
        self._calls = 0
        self._refuse = refuse
        self._real = chat_handlers._record_explicit_agent_selection
        monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", self._held)

    async def _held(self, *args: Any, **kwargs: Any) -> Any:
        self._calls += 1
        if self._calls == 1:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), WAIT)
            if self._refuse:
                raise OSError("selection write refused")
        return await self._real(*args, **kwargs)


# -- the reported repro: a retry adds no tab -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["demo", None])
async def test_a_retry_after_a_refused_create_leaves_no_ghost_tab(
    state: Any, monkeypatch: pytest.MonkeyPatch, name: str | None
) -> None:
    # An agent the config does not know, created with no project: the
    # assignment resolves it as a member and the member identity check refuses.
    _use_config(monkeypatch, _config())
    before = set(state._slots)
    payload: dict[str, Any] = {"agent": "project-only-agent"}
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
    _use_config(monkeypatch, _config())
    built: list[Any] = []
    original = state.prepare_slot

    def spy(*args: Any, **kwargs: Any) -> Any:
        built.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "prepare_slot", spy)
    status, _body = await _post(state, {"name": "demo", "agent": "project-only-agent"})
    assert status == 503
    assert built == [], "the refused create reached the slot constructor"


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
            assert await asyncio.to_thread(written.wait, WAIT)
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


async def _until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), WAIT)


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


def _pending(state: Any, name: str = "demo", **kwargs: Any) -> txn.PendingSlotCreate:
    existing, pending = state.prepare_slot(name, **kwargs)
    assert existing is None and pending is not None
    return txn.PendingSlotCreate(state, pending)


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


# -- the other openers behave as on main ----------------------------------------


def _cron_job() -> Any:
    return SimpleNamespace(
        id="job1",
        name="nightly",
        member_id="",
        agent_id="default",
        memory_store="",
        message="",
        chat_folder_id="",
        persistent_session=True,
        hide_in_chat=False,
    )


@pytest.mark.asyncio
async def test_a_cron_bind_of_the_same_key_waits_and_opens_the_creates_slot(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run-start cron bind on the SAME key as a pending create ends as on main.

    On main the create registered first, so the bind got the create's slot,
    with its folder, inherited tags and member pin, and linked it to the job's
    transcript. Here the bind waits for the create to publish and then does
    exactly that.

    Mutation pin: drop the wait in ``ensure_cron_slot`` and the bind raises
    "still being built".

    The bind's history read is held until the create has answered, so its
    rebind cannot race the create's own off-loop birth save and filing save
    (both pinned to the transcript the create authorized, and refused when a
    rebind lands mid-write): the order on main, where the create finished first.
    """
    from kiro_crew.dashboard import cron_inject

    _use_config(monkeypatch, _config(worker={}))
    state._tags.append({"id": "t1", "name": "T"})
    state._tags_authoritative = True
    state._folders.append({"id": "f1", "name": "F", "tags": ["t1"]})
    held = _HeldRecord(monkeypatch, refuse=False)
    create_answered = asyncio.Event()
    real_prefetch = cron_inject.prefetch_cron_history

    async def prefetch_after_the_create(*args: Any, **kwargs: Any) -> Any:
        # Reached only once the bind's wait saw the create publish.
        assert "cron-job1" in state._slots, "the bind did not wait for the create"
        await asyncio.wait_for(create_answered.wait(), WAIT)
        return await real_prefetch(*args, **kwargs)

    monkeypatch.setattr(cron_inject, "prefetch_cron_history", prefetch_after_the_create)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots",
                json={"name": "cron-job1", "agent": "worker", "folder_id": "f1"},
            )
        )
        bind: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            bind = asyncio.create_task(cron_inject.ensure_cron_slot(state, _cron_job()))
            await asyncio.sleep(0)
            assert not bind.done()
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == "f1"
            create_answered.set()
            await asyncio.wait_for(bind, WAIT)
        finally:
            held.release.set()
            create_answered.set()
            await _drain(post, *([bind] if bind is not None else []))
    slot = state._slots["cron-job1"]
    assert slot.linked_session_key == "cron:job1"
    assert slot.folder_id == "f1" and slot.tags == ["t1"]
    assert read_session_execution("dashboard:cron-job1") is not None


@pytest.mark.asyncio
async def test_a_cron_result_past_the_wait_is_kept_in_its_transcript(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result whose tab cannot be bound yet is written to the job's transcript.

    The next bind hydrates the tab from it, so no cron result is dropped.

    Mutation pin: let ``_bind_cron_slot``'s ValueError propagate and the
    injection raises with nothing written.
    """
    from kiro_crew.dashboard import cron_inject

    create = _pending(state, "cron-job1")
    try:
        cron_inject.inject_cron_result_to_dashboard(
            state, _cron_job(), "the nightly answer", history=[]
        )

        def written() -> bool:
            rows = state.conversation_log.read_messages("cron:job1")
            return any("the nightly answer" in str(row.get("content")) for row in rows)

        await _until(written)
    finally:
        await create.settle()
    slot = cron_inject._bind_cron_slot(
        state, _cron_job(), state.conversation_log.read_messages("cron:job1")
    )
    assert slot is not None
    assert any("the nightly answer" in str(m.get("content")) for m in slot.messages)


@pytest.mark.asyncio
async def test_a_cron_run_injected_during_a_same_key_create_shows_once_after_publish(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract for the synchronous cron injector during a pending create.

    A run injected while a create of the same ``cron-{id}`` key is still
    building it cannot bind the tab, so it is kept in the job's transcript. Once
    the create publishes, the next bind shows the run's prompt and result rows
    in the tab, each exactly once, however often the tab is bound again. This
    is why the synchronous ``_bind_cron_slot`` may refuse mid-create.

    Mutation pin: drop ``_keep_run_in_transcript`` from the injector's
    ValueError branch and the tab never shows the run.
    """
    from kiro_crew.dashboard import cron_inject

    _use_config(monkeypatch, _config(worker={}))
    job = _cron_job()
    job.message = "summarise the night"
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post("/api/chat/slots", json={"name": "cron-job1", "agent": "worker"})
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            cron_inject.inject_cron_result_to_dashboard(
                state, job, "the nightly answer", history=[]
            )

            def written() -> bool:
                rows = state.conversation_log.read_messages("cron:job1")
                return any("the nightly answer" in str(row.get("content")) for row in rows)

            await _until(written)
            held.release.set()
            assert (await asyncio.wait_for(post, WAIT)).status == 200
        finally:
            held.release.set()
            await _drain(post)
    for _ in range(2):
        await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    slot = state._slots["cron-job1"]
    contents = [str(m.get("content")) for m in slot.messages]
    prompts = [c for c in contents if c.startswith("# Cron Run:") and "summarise the night" in c]
    results = [c for c in contents if c.startswith("# Cron Job Result:")]
    assert len(prompts) == 1 and len(results) == 1
    assert "the nightly answer" in results[0]
    assert contents.index(prompts[0]) < contents.index(results[0])


@pytest.mark.asyncio
async def test_a_channel_opener_of_a_pending_key_is_surfaced_on_its_next_pass(
    state: Any,
) -> None:
    """``surface_channel_session`` is synchronous: it skips a key under construction
    (its existing ValueError handling) and surfaces it on the reconciler's next pass.
    """
    from kiro_crew.dashboard import channel_slots

    def surface() -> Any:
        return channel_slots.surface_channel_session(
            state,
            {"key": "slack_1700000000.000100"},
            {},
            [],
            session_key="slack:1700000000.000100",
        )

    create = _pending(state, "slack_1700000000.000100")
    assert surface() is None
    await create.settle()
    slot = surface()
    assert slot is not None and state._slots.get(slot.key) is slot


#: Openers that wait for a pending create inside, so calling one counts as waiting.
#: Each is checked to call ``wait_for_pending_create`` (or another entry) itself.
_WAITING_OPENERS = {
    "rehydrate_slot_from_history_async": "wait_for_pending_create",
    "resume_slot_from_history": "wait_for_pending_create",
    "ensure_cron_slot": "wait_for_pending_create",
    "_restore_worker_transcript": "rehydrate_slot_from_history_async",
}

#: Calls that open a named key: a key a pending create holds makes each refuse
#: with ValueError ("still being built").
_OPENING_CALLS = ("get_or_create_slot", "_rehydrate_slot_from_history", "_bind_cron_slot")

#: The hold a close takes on its key before it pops the slot: a create of the key
#: is refused while it is held, so a put-back after it meets no pending create.
_CLOSE_HOLD = "close_holds_key"

#: Single opens past a suspension that no create can meet, keyed by the function
#: and the key expression the open names, each with the reason.
_OPENS_NO_CREATE_CAN_HOLD = {
    ("kiro_crew/dashboard/openai_compat.py:api_completions", "slot_name"): (
        "the ephemeral branch's key is oai-<uuid4>, minted on this request"
    ),
    ("kiro_crew/dashboard/chat_api/resume.py:resume_slot_from_history", "state._slots[slot.key]"): (
        "publishes the slot this resume built under its own construction mark, "
        "which a create's mint refuses (DashboardState.prepare_slot)"
    ),
}

#: Openers of a named key that neither wait nor sit behind one that does, each
#: with the reason it cannot meet a pending create's key, or what it does instead.
_OPENERS_THAT_DO_NOT_WAIT = {
    "kiro_crew/dashboard/chat_persistence.py:_apply_restored_open_slot": (
        "boot restore, awaited in on_startup before the dashboard serves a create"
    ),
    "kiro_crew/dashboard/chat_persistence.py:_apply_recent_session": (
        "boot restore, awaited in on_startup before the dashboard serves a create"
    ),
    "kiro_crew/dashboard/handlers/taskrunner.py:_task_result_slot": (
        "the key carries a fresh uuid4 token minted on this call"
    ),
    "kiro_crew/dashboard/slot_registry.py:put_slot": (
        "the registry's own write, reached only through the state's publishers"
    ),
    "kiro_crew/dashboard/session_transfer.py:_install_arrived_bundle": (
        "import mints its key and holds it under its own construction mark"
    ),
}


class _OpenSites:
    """Every place in ``kiro_crew`` that opens a named key, with what precedes it.

    An open is a call in ``_OPENING_CALLS`` that names a key, or a direct
    ``<x>._slots[<k>] = ...`` write. For each one this records, in its own
    function body and before it in source order, whether the function waited
    (``wait_for_pending_create`` or a waiting opener), and whether the open sits
    in a ``try`` whose handler catches ValueError (the named fallback) or after
    the close's hold on the key (``close_holds_key``), which no suspension ends.
    """

    def __init__(self) -> None:
        import ast
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).resolve().parent
        self.waiting = {"wait_for_pending_create", *_WAITING_OPENERS}
        #: ``rel:function`` -> its open sites, as ``(what, line, satisfied)``.
        self.sites: dict[str, list[tuple[str, int, bool]]] = {}
        #: called name -> every ``(rel:function, line, satisfied, call)`` calling it.
        self.calls: dict[str, list[tuple[str, int, bool, Any]]] = {}
        #: ``rel:function`` -> every name it calls.
        self.calls_in: dict[str, set[str]] = {}
        #: The ``_OPENS_NO_CREATE_CAN_HOLD`` entries met, so a stale one fails.
        self.exempt_seen: set[tuple[str, str]] = set()
        needles = (*_OPENING_CALLS, "._slots[", *_WAITING_OPENERS)
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root.parent).as_posix()
            if "/tests/" in rel:
                continue
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                self._scan(rel, ast.parse(text))

    @staticmethod
    def _catches_value_error(handler: Any) -> bool:
        import ast

        kind = handler.type
        names = kind.elts if isinstance(kind, ast.Tuple) else [kind]
        return any(isinstance(n, ast.Name) and n.id == "ValueError" for n in names)

    @staticmethod
    def _call_name(call: Any) -> str:
        import ast

        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr
        return func.id if isinstance(func, ast.Name) else ""

    @staticmethod
    def _checks_construction_mark(test: Any) -> bool:
        """Whether an ``if`` test reads ``_slots_under_construction``: the synchronous fallback."""
        import ast

        return any(
            (isinstance(n, ast.Attribute) and n.attr == "_slots_under_construction")
            or (isinstance(n, ast.Constant) and n.value == "_slots_under_construction")
            for n in ast.walk(test)
        )

    def _scan(self, rel: str, tree: Any) -> None:
        import ast

        def walk_function(fn: Any) -> None:
            owner = f"{rel}:{fn.name}"
            self.sites.setdefault(owner, [])
            self.calls_in.setdefault(owner, set())
            waits: list[int] = []
            #: Every other suspension point: a create can start during any of them.
            suspends: list[int] = []
            found: list[tuple[str, int, bool, Any]] = []
            #: Where the function takes a close's hold on a key.
            holds: list[int] = []

            def visit(node: Any, guarded: bool) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        walk_function(child)
                        continue
                    if isinstance(child, ast.Try):
                        catches = any(self._catches_value_error(h) for h in child.handlers)
                        for stmt in child.body:
                            visit_one(stmt, guarded or catches)
                        for part in (*child.handlers, *child.orelse, *child.finalbody):
                            visit_one(part, guarded)
                        continue
                    if isinstance(child, ast.If):
                        if self._checks_construction_mark(child.test):
                            waits.append(child.lineno)
                        visit_one(child.test, guarded)
                        for stmt in child.body:
                            visit_one(stmt, guarded)
                        for stmt in child.orelse:
                            visit_one(stmt, guarded)
                        continue
                    visit_one(child, guarded)

            def visit_one(child: Any, guarded: bool) -> None:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk_function(child)
                    return
                if isinstance(child, (ast.AsyncWith, ast.AsyncFor)) or (
                    isinstance(child, ast.Await)
                    and not (
                        isinstance(child.value, ast.Call)
                        and self._call_name(child.value) in self.waiting
                    )
                ):
                    suspends.append(child.lineno)
                if isinstance(child, ast.Call):
                    name = self._call_name(child)
                    self.calls_in[owner].add(name)
                    if name in self.waiting:
                        waits.append(child.lineno)
                    if name == _CLOSE_HOLD:
                        holds.append(child.lineno)
                    found.append((name, child.lineno, guarded, child))
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Attribute)
                            and target.value.attr == "_slots"
                        ):
                            found.append(("_slots[]=", child.lineno, guarded, target))
                visit(child, guarded)

            visit(fn, False)
            for name, line, guarded, node in found:
                call = None if name == "_slots[]=" else node
                # A wait counts only when nothing suspends between it and the
                # open: a create that starts during that await holds the key again.
                ok = (
                    guarded
                    or any(h < line for h in holds)
                    or any(w < line and not any(w < a < line for a in suspends) for w in waits)
                )
                if not ok and (name == "_slots[]=" or name in _OPENING_CALLS):
                    key = (owner, ast.unparse(node if name == "_slots[]=" else _key_arg(node)))
                    if key in _OPENS_NO_CREATE_CAN_HOLD:
                        self.exempt_seen.add(key)
                        ok = True
                if name == "_slots[]=" or (
                    name in _OPENING_CALLS and (name != "get_or_create_slot" or _names_a_key(call))
                ):
                    self.sites[owner].append((name, line, ok))
                if call is not None:
                    self.calls.setdefault(name, []).append((owner, line, ok, call))

        def outer(node: Any) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk_function(child)
                elif not isinstance(child, ast.expr):
                    outer(child)

        outer(tree)


def _key_arg(call: Any) -> Any:
    """The key an opening call names: its first argument or its ``name=``."""
    import ast

    arg = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "name":
            arg = keyword.value
    return arg if arg is not None else ast.Constant(None)


def _names_a_key(call: Any) -> bool:
    import ast

    arg = _key_arg(call)
    return not (isinstance(arg, ast.Constant) and arg.value is None)


def _passes_no_name(call: Any) -> bool:
    """``name=None``: the call mints a fresh key, which no create can hold."""
    import ast

    return any(
        keyword.arg == "name"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is None
        for keyword in call.keywords
    )


@pytest.mark.asyncio
async def test_a_member_thread_open_waits_for_a_create_begun_after_its_rehydrate(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create of the member key that begins after the rehydrate's wait is waited on.

    The create starts in the window between ``rehydrate_slot_from_history_async``
    (which waits at its top) and the open, where the workspace resolution
    awaits. The open waits for it right before the open, and once the create
    gives the key up, mints the member's slot: 200, never the bare "still being
    built" ValueError as a 500.

    Mutation pin: drop the wait before the open (or put an await back between
    it and the open) and this answers 500; the opener scan fails too.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.handlers import members

    # No private memory: the open under test is the slot's, not the store's.
    cfg = KiroCrewConfig()
    cfg.agents = {"code-reviewer": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "code-reviewer"
    cfg.save()
    monkeypatch.setattr(members, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    real_rehydrate = members.rehydrate_slot_from_history_async
    real_wait = members.wait_for_pending_create
    held: dict[str, Any] = {}

    async def rehydrate_then_create(st: Any, key: str, **kwargs: Any) -> Any:
        restored = await real_rehydrate(st, key, **kwargs)
        assert restored is None
        held["create"] = _pending(st, key, agent="code-reviewer", mode=members_mod.DM_SLOT_MODE)
        return None

    async def observed_wait(st: Any, key: str) -> None:
        create = held.get("create")
        if create is None or held.get("waited"):
            return await real_wait(st, key)
        held["waited"] = key
        waiting = asyncio.ensure_future(real_wait(st, key))
        await asyncio.sleep(0)
        assert not waiting.done()
        await create.settle()
        await waiting

    monkeypatch.setattr(members, "rehydrate_slot_from_history_async", rehydrate_then_create)
    monkeypatch.setattr(members, "wait_for_pending_create", observed_wait)
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/members/{slug}/thread", members.api_member_thread)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await asyncio.wait_for(client.post("/api/members/code-reviewer/thread"), WAIT)
        assert resp.status == 200, await resp.text()
    key = held["waited"]
    assert state._slots[key].mode == members_mod.DM_SLOT_MODE
    assert key not in state._slots_under_construction


@pytest.mark.parametrize(
    ("between", "safe"),
    [
        ("", True),
        ("    await asyncio.to_thread(read)\n", False),
        ("    async with lock:\n        pass\n", False),
        ("    if key in state._slots_under_construction:\n        return None\n", True),
    ],
)
def test_the_opener_scan_refuses_a_suspension_between_the_wait_and_the_open(
    between: str, safe: bool
) -> None:
    """A wait followed by an await, before the open, does not count as waiting.

    A synchronous ``_slots_under_construction`` check after the last await is the
    named fallback and does count.
    """
    import ast

    source = (
        "async def opener(state, key):\n"
        "    await wait_for_pending_create(state, key)\n"
        f"{between}"
        "    return state.get_or_create_slot(key)\n"
    )
    scan = object.__new__(_OpenSites)
    scan.waiting = {"wait_for_pending_create", *_WAITING_OPENERS}
    scan.sites, scan.calls, scan.calls_in, scan.exempt_seen = {}, {}, {}, set()
    scan._scan("kiro_crew/synthetic.py", ast.parse(source))
    [(what, _line, ok)] = scan.sites["kiro_crew/synthetic.py:opener"]
    assert what == "get_or_create_slot" and ok is safe


def test_every_opener_of_a_named_key_waits_for_a_pending_create() -> None:
    """Found, not listed: every open of a named key in ``kiro_crew`` meets a pending
    create safely, sync and async alike, and direct ``_slots[k] = ...`` writes too.

    Safe means, BEFORE the open in its own function body: a wait for the pending
    create, the named fallback (a ``try`` that catches ValueError around it), or a
    close's hold on the key (``close_holds_key``), which refuses a create of it. Otherwise every call reaching the
    function must be safe in the same sense, or the function is named in
    ``_OPENERS_THAT_DO_NOT_WAIT`` with its reason.

    A wait followed by any other ``await`` (or ``async with`` / ``async for``)
    before the open does not count: a create can start during that suspension.

    A new opener fails here until it waits, falls back, or says why it need not.
    Mutation pins: move a wait after its open, put an await between a wait and
    its open, or drop ``ensure_cron_slot``'s fallback together with its wait, and
    the opener is listed.
    """
    scan = _OpenSites()
    for opener, inner in _WAITING_OPENERS.items():
        defined = [names for key, names in scan.calls_in.items() if key.endswith(f":{opener}")]
        assert defined and all(inner in names for names in defined), opener

    def reached_safely(owner: str, seen: frozenset[str] = frozenset()) -> bool:
        if owner in _OPENERS_THAT_DO_NOT_WAIT:
            return True
        if owner in seen:
            return False
        name = owner.rsplit(":", 1)[1]
        callers = scan.calls.get(name, [])
        return bool(callers) and all(
            ok or _passes_no_name(call) or reached_safely(caller, seen | {owner})
            for caller, _line, ok, call in callers
        )

    unsafe = sorted(
        f"{owner}:{line} {what}"
        for owner, sites in scan.sites.items()
        for what, line, ok in sites
        if not ok and not reached_safely(owner)
    )
    assert unsafe == []
    # Every reason still names a real opener.
    openers = {owner for owner, sites in scan.sites.items() if sites}
    assert sorted(set(_OPENERS_THAT_DO_NOT_WAIT) - openers) == []
    assert sorted(set(_OPENS_NO_CREATE_CAN_HOLD) - scan.exempt_seen) == []
    # The direct writers the review named are all seen, and each is guarded.
    writers = {
        owner.split(":")[0]
        for owner, sites in scan.sites.items()
        for what, _line, _ok in sites
        if what == "_slots[]="
    }
    assert {
        "kiro_crew/dashboard/chat_api/slot_lifecycle.py",
        "kiro_crew/dashboard/chat_api/resume.py",
        "kiro_crew/apps/builtins/spec_builder/backend/runtime.py",
        "kiro_crew/apps/builtins/spec_builder/backend/orchestration/execution_state.py",
    } <= writers


# -- an app learns nothing from the name wait ------------------------------------


async def _app_post(client: TestClient, name: str) -> tuple[int, dict[str, Any]]:
    resp = await asyncio.wait_for(
        client.post("/api/chat/slots", json={"name": name}, headers={"X-Test-App": "app-a"}),
        WAIT,
    )
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_an_app_cannot_tell_a_busy_name_from_one_it_may_not_use(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is judged before the name wait, and a wait that runs out is the same 404.

    ``theirs`` is another app's slot and ``free`` is nobody's. Another create
    holds both names. The app's create of ``theirs`` is refused at once, without
    waiting for the name; its create of ``free`` is refused at once too, with the
    same answer, because another principal's create holds it.

    Mutation pins: take the name wait before the ownership check, or drop the
    other-principal check, and a request outlasts WAIT.
    """
    _use_config(monkeypatch, _config())
    state.get_or_create_slot("theirs", app="app-b")
    releases = [
        await txn.acquire_slot_create_name(state, "theirs"),
        await txn.acquire_slot_create_name(state, "free"),
    ]
    try:
        async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
            # The real bound is far past WAIT: only an answer that never waits fits.
            monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 3 * WAIT)
            not_yours = await _app_post(client, "theirs")
            busy = await _app_post(client, "free")
    finally:
        for release in releases:
            assert release is not None
            release()
    assert not_yours == busy == (404, {"error": "not found", "code": "slot_not_found"})
    assert state._slots["theirs"]._app == "app-b"
    assert "free" not in state._slots


# -- a tag deleted while the create waits is not published ------------------------


@pytest.mark.asyncio
async def test_a_folder_tag_deleted_while_the_create_waits_is_not_published(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inherited tags are filtered again under ``tags_write_lock`` at the commit.

    The tag-delete sweep cannot see a slot nobody registered, so a tag deleted
    while the create waits in its selection write would otherwise be published
    on the new slot. The re-filter runs with the session-switch lock held, the
    order every holder of both takes (switch lock, then tags lock).

    Mutation pin: publish without the re-filter and the deleted id ships.
    """
    from kiro_crew.dashboard import chat_tags
    from kiro_crew.llm_helpers import slot_switch_session_lock

    _use_config(monkeypatch, _config(worker={}))
    state._tags.extend([{"id": "keep", "name": "Keep"}, {"id": "gone", "name": "Gone"}])
    state._tags_authoritative = True
    state._folders.append({"id": "f1", "name": "F", "tags": ["keep", "gone"]})
    switch_held_at_tags: list[bool] = []
    real_lock = chat_tags.tags_write_lock

    class _Watched:
        def __init__(self, lock: Any) -> None:
            self._lock = lock

        async def acquire(self) -> bool:
            switch_held_at_tags.append(slot_switch_session_lock("dashboard:demo").locked())
            return await self._lock.acquire()

        def release(self) -> None:
            self._lock.release()

        async def __aenter__(self) -> None:
            await self._lock.acquire()

        async def __aexit__(self, *exc: Any) -> None:
            self._lock.release()

    monkeypatch.setattr(chat_handlers, "tags_write_lock", lambda st: _Watched(real_lock(st)))
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            async with real_lock(state):
                state._tags[:] = [t for t in state._tags if t["id"] != "gone"]
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200, await resp.text()
        finally:
            held.release.set()
            await _drain(post)
    assert state._slots["demo"].tags == ["keep"]
    # The inheritance takes the tags lock alone; the commit takes it inside the switch lock.
    assert switch_held_at_tags == [False, True]


def test_no_holder_of_the_tags_lock_takes_the_session_switch_lock() -> None:
    """Lock order: switch lock, then ``tags_write_lock``, never the reverse.

    Every ``async with tags_write_lock(...)`` body in the package is scanned
    for a session-switch lock; the create's commit takes the tags lock inside
    the switch lock, so one such body would be a lock-order inversion.
    """
    import ast
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    switch_names = {"slot_switch_session_lock", "_slot_switch_session_lock"}
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "tags_write_lock" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.AsyncWith):
                continue
            if not any("tags_write_lock" in ast.unparse(item.context_expr) for item in node.items):
                continue
            body = "\n".join(ast.unparse(stmt) for stmt in node.body)
            if any(name in body for name in switch_names):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


# -- a create publishes unfiled, then files through the folder-move path ---------


def _agent_folder(state: Any, *, hidden: bool = False) -> dict[str, Any]:
    """An empty folder the agent session ``agent`` created, as the store holds it."""
    folder: dict[str, Any] = {
        "id": "f1",
        "name": "F",
        chat_folders.CREATED_BY_SESSION: "dashboard:agent",
    }
    if hidden:
        folder["hidden"] = True
    state._folders.append(folder)
    return folder


def _folder_is_untouched(folder: dict[str, Any]) -> bool:
    """Still the agent's hidden folder: no un-hide, no claim."""
    return (
        folder.get(chat_folders.CREATED_BY_SESSION) == "dashboard:agent"
        and folder.get("hidden") is True
    )


async def _agent_delete_if_empty(state: Any) -> int:
    """The ``chat_folder_delete`` call the agent session ``agent`` makes; its status."""
    from chat_test_helpers import _make_folder_app

    state.get_or_create_slot("agent")
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        resp = await asyncio.wait_for(
            client.delete(
                "/api/chat/folders/f1?if_empty=true",
                headers={
                    "X-Internal-Secret": "s3cret",
                    "X-Internal-Caller": "kirocrew-dashboard",
                    "X-Session-Key": "dashboard:agent",
                },
            ),
            WAIT,
        )
        return resp.status


@pytest.mark.asyncio
async def test_a_refused_create_leaves_the_folder_untouched(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create that never publishes writes nothing to the folder, live or on disk.

    Mutation pin: un-hide or claim the folder before the awaits (``_unhide_folder``
    with ``claim_for_person`` at assignment) and the mark is gone.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    await state.mutate_folders(lambda folders: (True, None))
    on_disk = _folders_file(state).read_bytes()
    before = [dict(f) for f in state._folders]
    generation = state.folders_generation()
    _HeldRecord(monkeypatch, refuse=True).release.set()
    status, _body = await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})
    assert status == 503
    assert "demo" not in state._slots
    assert state._folders == before
    assert state.folders_generation() == generation
    assert _folders_file(state).read_bytes() == on_disk
    # Still the agent's, so its empty-only delete goes through.
    assert await _agent_delete_if_empty(state) == 200


@pytest.mark.asyncio
async def test_a_person_claim_during_a_refused_create_still_stops_the_agent_delete(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person renames the folder while the create awaits; the create then refuses.

    The rename removed the agent's mark, and the refusal has nothing to put
    back, so the agent's empty-only delete is still refused.

    Mutation pin: claim at assignment and restore the mark on refusal (the
    rollback this replaced) and the agent deletes the person's folder.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state)
    held = _HeldRecord(monkeypatch, refuse=True)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            # The person's rename: an edit that claims the folder.
            await state.mutate_folders(
                lambda folders: (
                    bool(folders[0].pop(chat_folders.CREATED_BY_SESSION, None))
                    | bool(folders[0].update(name="Mine")),
                    None,
                )
            )
            held.release.set()
            assert (await asyncio.wait_for(post, WAIT)).status == 503
        finally:
            held.release.set()
            await _drain(post)
    folder = next(f for f in state._folders if f["id"] == "f1")
    assert chat_folders.CREATED_BY_SESSION not in folder and folder["name"] == "Mine"
    assert await _agent_delete_if_empty(state) == 403
    assert any(f["id"] == "f1" for f in state._folders)


@pytest.mark.asyncio
async def test_a_published_create_claims_and_unhides_its_folder_at_publish(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim and the un-hide land in the filing step after publication, not before.

    Mutation pin: drop the filing step (``file_once_published``) and the folder
    keeps the agent's mark.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            pending_folder = next(f for f in state._folders if f["id"] == "f1")
            assert pending_folder.get(chat_folders.CREATED_BY_SESSION) == "dashboard:agent"
            assert pending_folder.get("hidden") is True
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200
            assert (await resp.json())["folder_id"] == "f1"
        finally:
            held.release.set()
            await _drain(post)
    folder = next(f for f in state._folders if f["id"] == "f1")
    assert chat_folders.CREATED_BY_SESSION not in folder
    assert not folder.get("hidden")
    assert state._slots["demo"].folder_id == "f1"


@pytest.mark.asyncio
async def test_a_folder_deleted_before_publish_leaves_the_slot_unfiled(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder deleted while the create builds: the move path refuses the filing.

    The create still answers success with its published slot, unfiled: the
    response's ``folder_id`` is empty.

    Mutation pin: return the move path's refusal instead of the slot and the
    create answers 400 for a slot it published.
    """
    _use_config(monkeypatch, _config(worker={}))
    state._folders.append({"id": "f1", "name": "F"})
    held = _HeldRecord(monkeypatch, refuse=False)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            await state.mutate_folders(lambda folders: (bool(folders.clear()) or True, None))
            held.release.set()
            resp = await asyncio.wait_for(post, WAIT)
            assert resp.status == 200
            body = await resp.json()
        finally:
            held.release.set()
            await _drain(post)
    assert body["key"] == "demo" and body["folder_id"] == ""
    assert body["folder_id"] == "" and "filing_error" not in body
    slot = state._slots["demo"]
    assert slot.folder_id == "" and "demo" not in state._slots_under_construction


@pytest.mark.asyncio
async def test_a_create_files_only_after_it_published_through_the_move_path(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filing is the ordinary folder-move path, entered once the slot is published.

    Mutation pin: file before ``create.publish()`` and the move path finds no
    slot under the key (``seen`` records False).
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_move = chat_folders.file_slot_into_folder
    seen: list[tuple[bool, str | None]] = []

    async def watched(
        request: Any, st: Any, name: str, target: str | None = None, **kwargs: Any
    ) -> Any:
        published = name in st._slots and name not in st._slots_under_construction
        seen.append((published, target))
        return await real_move(request, st, name, target, **kwargs)

    monkeypatch.setattr(chat_folders, "file_slot_into_folder", watched)
    status, body = await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})
    assert status == 200 and body["folder_id"] == "f1"
    assert seen == [(True, "f1")]
    assert state._slots["demo"].folder_id == "f1"


@pytest.mark.asyncio
async def test_a_filing_the_move_path_refuses_leaves_the_published_slot_unfiled(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move path's own authorization refuses the filing: success, empty folder_id.

    The folder is not written, and the slot the create published stays.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    await state.mutate_folders(lambda folders: (True, None))
    on_disk = _folders_file(state).read_bytes()

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    monkeypatch.setattr(chat_folders, "member_slot_write_refused", refused)
    status, body = await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    assert state._slots["demo"].folder_id == ""
    assert "demo" not in state._slots_under_construction
    assert _folders_file(state).read_bytes() == on_disk
    assert _folder_is_untouched(next(f for f in state._folders if f["id"] == "f1"))


def _replace_published(state: Any, key: str = "demo") -> Any:
    """Delete *key*'s slot and open the same key again: a different slot object.

    The replacement writes its own metadata line, as a send to it does, so the
    shared transcript has a line a stale save could overwrite.
    """
    original = state._slots.pop(key)
    replacement = state.get_or_create_slot(key)
    assert replacement is not original and state._slots[key] is replacement
    state.conversation_log.update_metadata(f"dashboard:{key}", {"folder_id": ""})
    return replacement


async def _create_filed_demo(state: Any) -> tuple[int, dict[str, Any]]:
    return await _post(state, {"name": "demo", "agent": "worker", "folder_id": "f1"})


@pytest.mark.asyncio
async def test_a_slot_replaced_before_the_filing_is_not_filed(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete plus a same-key open during the birth save: the replacement stays unfiled.

    The create answers 200 for the slot it published, with an empty
    ``folder_id``. The replacement keeps its own (empty) folder, and the birth
    save does not write the original onto its transcript.

    Mutation pin: drop the identity check before the dispatch AND the
    ``expected_slot`` the move path re-checks, and the move path resolves the
    replacement by key and files it into ``f1``.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_save = chat_handlers.save_slot_off_loop
    seen: dict[str, Any] = {}

    async def save_then_replaced(st: Any, slot: Any, *args: Any, **kwargs: Any) -> bool:
        if slot.key == "demo" and "replacement" not in seen:
            seen["replacement"] = _replace_published(st)
        seen["saved"] = await real_save(st, slot, *args, **kwargs)
        return seen["saved"]

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", save_then_replaced)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    replacement = seen["replacement"]
    assert state._slots["demo"] is replacement
    assert replacement.folder_id == ""
    # The birth save's object check refused the write onto the replacement.
    assert seen["saved"] is False
    assert _folder_is_untouched(next(f for f in state._folders if f["id"] == "f1"))


@pytest.mark.asyncio
async def test_a_slot_replaced_during_the_move_un_hide_is_not_filed(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is reused while the move path awaits its folder un-hide.

    The move path re-checks the slot object after that await and refuses with
    its own ``session_gone`` before it persists anything; the create answers 200
    with an empty ``folder_id``.

    Mutation pin: drop the re-check after ``_unhide_folder`` and the move path
    reaches its save (``saves`` is not empty).
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_unhide = chat_folders._unhide_folder
    real_save = chat_folders.save_slot_off_loop
    seen: dict[str, Any] = {}
    saves: list[str] = []

    async def unhide_then_replaced(st: Any, *args: Any, **kwargs: Any) -> bool:
        ok = await real_unhide(st, *args, **kwargs)
        seen["original"] = st._slots["demo"]
        seen["replacement"] = _replace_published(st)
        return ok

    async def counted_save(st: Any, slot: Any, *args: Any, **kwargs: Any) -> bool:
        saves.append(slot.key)
        return await real_save(st, slot, *args, **kwargs)

    monkeypatch.setattr(chat_folders, "_unhide_folder", unhide_then_replaced)
    monkeypatch.setattr(chat_folders, "save_slot_off_loop", counted_save)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    assert saves == []
    assert state._slots["demo"] is seen["replacement"]
    assert seen["replacement"].folder_id == ""
    assert seen["original"].folder_id == ""


@pytest.mark.asyncio
async def test_a_slot_replaced_during_the_move_save_is_not_filed(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is reused while the move path's save awaits the history lock.

    The save re-checks the slot object at its locked commit and writes nothing,
    so the move path answers ``session_gone`` and the replacement stays unfiled.

    Mutation pin: drop ``expected_slot_name`` from the move path's save and it
    commits the original's folder onto the shared transcript and answers 200
    with ``folder_id == "f1"``.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    real_save = chat_folders.save_slot_off_loop
    seen: dict[str, Any] = {}

    async def replaced_then_save(st: Any, slot: Any, *args: Any, **kwargs: Any) -> bool:
        seen["replacement"] = _replace_published(st)
        seen["saved"] = await real_save(st, slot, *args, **kwargs)
        return seen["saved"]

    monkeypatch.setattr(chat_folders, "save_slot_off_loop", replaced_then_save)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == "" and "filing_error" not in body
    assert seen["saved"] is False
    assert state._slots["demo"] is seen["replacement"]
    assert seen["replacement"].folder_id == ""
    assert state.conversation_log.get_metadata("dashboard:demo").get("folder_id") == ""


def _fail_filing_step(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    """Make one step of the folder-move path raise, as a failed store write does."""

    async def fails(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("folder store write failed")

    monkeypatch.setattr(chat_folders, step, fails)


def _folder_seen_by_frames(state: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The ``demo`` slot's folder as each slots frame carries it."""
    seen: list[str] = []
    original = state._do_slots_broadcast

    def broadcast(*args: Any, **kwargs: Any) -> Any:
        slot = state._slots.get("demo")
        if slot is not None:
            seen.append(slot.folder_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "_do_slots_broadcast", broadcast)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["_unhide_folder", "save_slot_off_loop"])
async def test_a_folder_store_failure_during_the_filing_leaves_the_slot_unfiled(
    state: Any, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """A step of the move path raises mid-filing: the published slot is unfiled everywhere.

    The create answers 200 with an empty ``folder_id``. The live slot
    shows no folder, so the coalesced frame does not show it filed; nothing on
    disk names the folder; and the slot is marked dirty so a flush that ran
    meanwhile reconverges.

    Mutation pin: drop the rollback around the move path's un-hide and save,
    and the live slot (and the frame) keep ``folder_id == "f1"`` with nothing
    durable behind it.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    _fail_filing_step(monkeypatch, step)
    frames = _folder_seen_by_frames(state, monkeypatch)
    status, body = await _create_filed_demo(state)
    assert status == 200
    assert body["folder_id"] == ""
    assert body["folder_id"] == "" and "filing_error" not in body
    slot = state._slots["demo"]
    assert slot.folder_id == "" and slot._dirty is True
    assert frames and set(frames) == {""}
    assert state.conversation_log.get_metadata("dashboard:demo").get("folder_id", "") == ""
    if step == "_unhide_folder":
        # The un-hide wrote nothing; a save failure follows an un-hide that the
        # move route has always kept (the folder is shown, the slot is not in it).
        assert _folder_is_untouched(next(f for f in state._folders if f["id"] == "f1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["_unhide_folder", "save_slot_off_loop"])
async def test_a_folder_store_failure_on_the_move_route_leaves_the_slot_where_it_was(
    state: Any, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """The PATCH move route shares the rollback: a raise leaves the slot unfiled, live and on disk.

    Mutation pin: drop the rollback and the route's 500 leaves the live slot
    filed into ``f1`` while nothing durable holds it.
    """
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    status, body = await _post(state, {"name": "demo", "agent": "worker"})
    assert status == 200, body
    slot = state._slots["demo"]
    slot._dirty = False
    _fail_filing_step(monkeypatch, step)
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        resp = await asyncio.wait_for(
            client.patch("/api/chat/slots/demo/folder", json={"folder_id": "f1"}), WAIT
        )
    assert resp.status == 500
    assert slot.folder_id == "" and slot._folder_changed is False and slot._dirty is True
    assert state.conversation_log.get_metadata("dashboard:demo").get("folder_id", "") == ""


def test_every_python_caller_of_the_create_route_reads_the_folder_id() -> None:
    """A Python module that POSTs ``/api/chat/slots`` reads the answered ``folder_id``.

    A create whose filing is refused still answers 200, with an empty
    ``folder_id``, so a caller that asked for a folder learns of the refusal
    only by comparing it. Today that is ``cron_script.open_session`` alone: the
    MCP ``session_create`` tool goes through ``/api/session-control/create``,
    which files inline and has no deferred filing. A new caller that never
    reads the field fails here.
    """
    import re
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).resolve().parent
    # ``self._post(...)`` / ``client.post(...)``, never the router's ``add_post``.
    poster = re.compile(r"""(?<!\w)_?post\(\s*["']/api/chat/slots["']""")
    callers = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if poster.search(path.read_text(encoding="utf-8"))
    }
    assert sorted(callers) == ["cron_script.py"]
    assert [name for name, text in callers.items() if '.get("folder_id")' not in text] == []


# -- a cancelled create writes nothing to the folder ------------------------------


class _CancellableCreate:
    """Serves the create route and keeps the task running each request's handler."""

    def __init__(self, state: Any) -> None:
        self.task: asyncio.Task[Any] | None = None
        self.started = asyncio.Event()

        async def create(request: web.Request) -> web.StreamResponse:
            self.task = asyncio.current_task()
            self.started.set()
            return await chat_handlers.api_chat_slot_create(request)

        self.app = web.Application()
        self.app["state"] = state
        self.app.router.add_post("/api/chat/slots", create)

    async def cancel_and_join(self, then: Callable[[], Any] = lambda: None) -> None:
        """Cancel the handler, run *then*, and wait for the handler to finish."""
        assert self.task is not None
        self.task.cancel()
        then()
        await asyncio.wait_for(asyncio.wait((self.task,)), WAIT)


def _folders_file(state: Any) -> Any:
    from kiro_crew.dashboard import state as state_module

    return state_module.config_dir() / state._FOLDERS_FILE


@pytest.mark.asyncio
async def test_a_create_cancelled_before_its_folder_write_leaves_the_folder_untouched(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create cancelled before it publishes writes nothing and gives the key back."""
    _use_config(monkeypatch, _config(worker={}))
    _agent_folder(state, hidden=True)
    await state.mutate_folders(lambda folders: (True, None))
    on_disk = _folders_file(state).read_bytes()
    before = [dict(f) for f in state._folders]
    generation = state.folders_generation()
    held = _HeldRecord(monkeypatch, refuse=False)
    route = _CancellableCreate(state)
    async with TestClient(TestServer(as_owner(route.app))) as client:
        post = asyncio.create_task(
            client.post(
                "/api/chat/slots", json={"name": "demo", "agent": "worker", "folder_id": "f1"}
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), WAIT)
            await route.cancel_and_join(then=held.release.set)
        finally:
            held.release.set()
            await _drain(post)
    assert "demo" not in state._slots and "demo" not in state._slots_under_construction
    assert "demo" not in txn._PENDING_CREATES.get(state, {})
    assert state._folders == before and state.folders_generation() == generation
    assert _folders_file(state).read_bytes() == on_disk
    assert await _agent_delete_if_empty(state) == 200


# -- the create's own lock waits are bounded --------------------------------------


@pytest.mark.asyncio
async def test_a_create_whose_switch_lock_never_frees_is_refused_in_bound(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held session-switch lock ends the create with the retryable 409, publishing nothing.

    Mutation pin: take the lock without the deadline and the request outlasts WAIT.
    """
    from kiro_crew.llm_helpers import slot_switch_session_lock

    _use_config(monkeypatch, _config(worker={}))
    # The create's lock budget only: the name wait keeps its own bound.
    monkeypatch.setattr(txn.CreateDeadline, "remaining", lambda _self: 0.05)
    lock = slot_switch_session_lock("dashboard:demo")
    await lock.acquire()
    try:
        status, body = await _post(state, {"name": "demo", "agent": "worker"})
    finally:
        lock.release()
    assert status == 409 and body["code"] == "slot_under_construction"
    assert "demo" not in state._slots and not state._slots_under_construction
    assert read_session_execution("dashboard:demo") is None


# -- a closing slot owns its key until its close settles ---------------------------


def _held_by_close(state: Any, key: str = "demo") -> bool:
    return key in txn._CLOSING_KEYS.get(state, {})


class _HeldArchive:
    """The close's archive of *slot* stops until released, then fails or commits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, slot: Any, *, fail: bool) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        real_save = chat_handlers.save_slot_off_loop

        async def archive(st: Any, saved: Any, *args: Any, **kwargs: Any) -> Any:
            if saved is slot and kwargs.get("closed"):
                self.entered.set()
                await asyncio.wait_for(self.release.wait(), WAIT)
                if fail:
                    raise OSError("archive refused")
            return await real_save(st, saved, *args, **kwargs)

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", archive)


@pytest.mark.asyncio
async def test_a_create_during_a_failing_close_is_refused_and_the_slot_comes_back(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive's transcript I/O fails while a create of the same key arrives.

    The create answers 409 at once, the failed close puts its own slot back with
    the unsaved row (left to the periodic flush), and a later create succeeds.

    Mutation pin: drop ``close_holds_key`` from ``close_slot`` and the create
    publishes a replacement while the close is still archiving.
    """
    old = state.get_or_create_slot("demo")
    old.append("user", "an unsaved line")
    rows = list(old.messages)
    archive = _HeldArchive(monkeypatch, old, fail=True)
    close = asyncio.create_task(chat_handlers.close_slot(state, old, "demo"))
    try:
        await asyncio.wait_for(archive.entered.wait(), WAIT)
        assert "demo" not in state._slots and _held_by_close(state)
        status, body = await _post(state, {"name": "demo"})
        assert status == 409 and body.get("code") == "slot_under_construction", body
        assert "demo" not in state._slots and not state._slots_under_construction
        archive.release.set()
        with pytest.raises(chat_handlers.SlotCloseError):
            await asyncio.wait_for(close, WAIT)
    finally:
        archive.release.set()
        await _drain(close)
    assert state._slots["demo"] is old and old.messages == rows
    assert not _held_by_close(state)
    status, body = await _post(state, {"name": "demo"})
    assert status == 200, body


@pytest.mark.asyncio
async def test_a_create_while_a_close_is_still_before_its_pop_is_refused(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close holds its key from before its pop, so a create meanwhile is refused.

    The close stops in its nudge retirement, while its slot is still registered.
    A create of the name (given unfolded, as a client may send it) answers 409
    ``slot_under_construction`` at once, not 200 with the slot the close goes
    on to archive. Once the close commits, a create builds a new slot.

    Mutation pin: check the hold only on the mint branch (after
    ``prepare_slot``) and the create answers 200 with the closing slot.
    """
    old = state.get_or_create_slot("demo")
    entered = asyncio.Event()
    release = asyncio.Event()
    real_retire = chat_handlers._retire_slot_nudge_loop

    async def held_retire(name: str) -> Any:
        entered.set()
        await asyncio.wait_for(release.wait(), WAIT)
        return await real_retire(name)

    monkeypatch.setattr(chat_handlers, "_retire_slot_nudge_loop", held_retire)
    close = asyncio.create_task(chat_handlers.close_slot(state, old, "demo"))
    try:
        await asyncio.wait_for(entered.wait(), WAIT)
        assert _held_by_close(state) and state._slots.get("demo") is old
        status, body = await asyncio.wait_for(_post(state, {"name": "dashboard:demo"}), WAIT)
        assert status == 409 and body.get("code") == "slot_under_construction", body
        assert not close.done() and state._slots.get("demo") is old
        release.set()
        await asyncio.wait_for(close, WAIT)
    finally:
        release.set()
        await _drain(close)
    assert not _held_by_close(state) and "demo" not in state._slots
    status, body = await _post(state, {"name": "demo"})
    assert status == 200, body
    assert state._slots["demo"] is not old


@pytest.mark.asyncio
async def test_an_app_create_of_its_own_closing_slot_carries_the_code(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app re-creating its own slot while a close holds the key gets the coded 409.

    The refusal names only the app's own slot, so it keeps the 409 text and
    carries ``slot_under_construction``, the code the dashboard answer carries.
    """
    _use_config(monkeypatch, _config())
    mine = state.get_or_create_slot("mine", app="app-a")
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        with txn.close_holds_key(state, "mine"):
            status, body = await _app_post(client, "mine")
        assert status == 409 and body.get("code") == "slot_under_construction", body
        assert state._slots["mine"] is mine
        status, body = await _app_post(client, "mine")
    assert status == 200, body


@pytest.mark.asyncio
async def test_an_opener_given_a_raw_name_waits_for_the_create_of_its_folded_key(
    state: Any,
) -> None:
    """``wait_for_pending_create`` folds the key, as the create holding it did.

    A create of ``demo:1`` holds ``demo_1``. An opener given the raw name, here
    ``acquire_worker_slot``, waits for that create instead of meeting its mark.

    Mutation pin: drop the fold in ``wait_for_pending_create`` and both the
    bare wait and the acquire return while the create is still pending.
    """
    from kiro_crew.apps.worker_slots import acquire_worker_slot

    create = _pending(state, "demo:1")
    assert create.slot.key == "demo_1"
    waiter = asyncio.create_task(txn.wait_for_pending_create(state, "demo:1"))
    acquire = asyncio.create_task(
        acquire_worker_slot(state, "demo-app", "demo:1", project="/work/repo")
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not waiter.done() and not acquire.done(), (
            acquire.exception() if acquire.done() else None
        )
        await create.settle()
        await asyncio.wait_for(waiter, WAIT)
        lease = await asyncio.wait_for(acquire, WAIT)
        assert lease.key == "demo_1"
        await lease.release()
    finally:
        await create.settle()
        await _drain(waiter, acquire)


@pytest.mark.asyncio
async def test_a_close_that_commits_frees_its_key(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the archive commits the key is free, and a create of it builds a new slot."""
    old = state.get_or_create_slot("demo")
    archive = _HeldArchive(monkeypatch, old, fail=False)
    archive.release.set()
    await asyncio.wait_for(chat_handlers.close_slot(state, old, "demo"), WAIT)
    assert not _held_by_close(state)
    status, body = await _post(state, {"name": "demo"})
    assert status == 200, body
    assert state._slots["demo"] is not old


@pytest.mark.asyncio
async def test_a_cancelled_close_releases_its_key(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close cancelled mid-archive leaves no hold behind, so the key is not stuck."""
    old = state.get_or_create_slot("demo")
    archive = _HeldArchive(monkeypatch, old, fail=True)
    close = asyncio.create_task(chat_handlers.close_slot(state, old, "demo"))
    await asyncio.wait_for(archive.entered.wait(), WAIT)
    assert _held_by_close(state)
    await _drain(close)
    assert not _held_by_close(state)
    assert txn._CLOSING_KEYS.get(state) == {}


@pytest.mark.asyncio
async def test_the_cleanup_sweep_holds_each_key_it_archives(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep refuses a create of a key while its archive runs, and lets go after.

    Mutation pin: drop the sweep's ``close_holds_key`` and the key reads free.
    """
    from kiro_crew.dashboard.chat_api import slot_lifecycle

    old = state.get_or_create_slot("demo")
    seen: list[bool] = []

    async def archive(st: Any, saved: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(_held_by_close(st))
        raise OSError("archive refused")

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", archive)
    monkeypatch.setattr(chat_handlers, "select_idle_slot_keys", lambda *a, **k: (["demo"], False))
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/cleanup", slot_lifecycle.api_chat_slots_cleanup)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await asyncio.wait_for(client.post("/api/chat/slots/cleanup", json={}), WAIT)
        body = await resp.json()
    assert body["failed"] == ["demo"], body
    assert seen == [True]
    assert state._slots["demo"] is old
    assert not _held_by_close(state)


_SPEC_KEY = "spec-builder-s-1234abcd"


def _spec_slot(state: Any) -> Any:
    from kiro_crew.apps.builtins.spec_builder.backend.runtime import APP_NAME

    slot = state.get_or_create_slot(_SPEC_KEY)
    slot._app = APP_NAME
    slot.messages.append({"role": "user", "content": "an unsaved line"})
    return slot


@pytest.mark.asyncio
async def test_a_failed_spec_close_refuses_a_create_and_goes_back_in_place(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Spec Builder delete whose archive fails holds its key, then puts its slot back.

    Mutation pin: drop ``close_holds_key`` from ``_teardown_worker_slot`` and a
    create of the key is admitted while the archive runs.
    """
    from kiro_crew.apps.builtins.spec_builder.backend import runtime
    from kiro_crew.dashboard import chat_persistence

    old = _spec_slot(state)
    rows = list(old.messages)
    refused: list[str] = []

    async def failing_archive(st: Any, slot: Any, *args: Any, **kwargs: Any) -> Any:
        assert slot is old and kwargs.get("closed")
        with pytest.raises(ValueError) as exc:
            txn.refuse_create_while_closing(st, _SPEC_KEY)
        refused.append(str(exc.value))
        raise OSError("archive refused")

    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", failing_archive)
    archived = await runtime._teardown_worker_slot(state, "s", only_slot=old, require_archive=True)
    assert archived is False and len(refused) == 1
    assert state._slots.get(_SPEC_KEY) is old and old.messages == rows
    assert not _held_by_close(state, _SPEC_KEY)


@pytest.mark.asyncio
async def test_a_still_running_orphan_holds_its_key_through_its_put_back(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orphan recovery keeps the key from teardown to put-back, then lets it go.

    Mutation pin: drop ``close_holds_key`` from ``execution_state`` and the
    teardown sees a free key.
    """
    from kiro_crew.apps.builtins.spec_builder.backend.orchestration import execution_state

    old = _spec_slot(state)
    old.task = asyncio.get_running_loop().create_future()
    held: list[bool] = []

    async def teardown(st: Any, name: str, *, only_slot: Any, require_archive: bool) -> bool:
        assert only_slot is old and require_archive
        st._slots.pop(_SPEC_KEY)
        held.append(_held_by_close(st, _SPEC_KEY))
        return True

    class _Service:
        async def deactivate_and_wait(self, loop_id: str, *, stopped_reason: str) -> bool:
            return True

    monkeypatch.setattr(
        execution_state, "_matching_execution_loops", lambda *a, **k: {_SPEC_KEY: "loop-1"}
    )
    monkeypatch.setattr(execution_state, "_unindexed_observed_slot_keys", lambda: set())
    monkeypatch.setattr(execution_state, "_teardown_worker_slot", teardown)
    try:
        with pytest.raises(RuntimeError, match="still running"):
            await execution_state._remove_orphaned_executions_with_service(state, _Service())
        assert held == [True]
        assert state._slots.get(_SPEC_KEY) is old
        assert not _held_by_close(state, _SPEC_KEY)
    finally:
        old.task.cancel()


def test_a_close_hold_counts_nested_holders() -> None:
    """Two holders of one key: the key stays held until the last one lets go."""

    class _Owner:
        pass

    owner = _Owner()
    with txn.close_holds_key(owner, "k"):
        with txn.close_holds_key(owner, "k"):
            pass
        with pytest.raises(ValueError):
            txn.refuse_create_while_closing(owner, "k")
    txn.refuse_create_while_closing(owner, "k")
    assert txn._CLOSING_KEYS.get(owner) == {}


# -- an app never waits behind another principal's create of a name ---------------


@pytest.mark.asyncio
async def test_an_app_never_enters_the_wait_for_a_name_another_principal_holds(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard holds ``free``; an app's create of it is answered without waiting.

    The real bound stays in force (30 s), and the answer is the 404 a name the
    app may not use gets. Deterministic: the app never calls the name wait.

    Mutation pin: drop the ``name_held_by_another`` check and the app joins the
    gate and waits out the bound.
    """
    _use_config(monkeypatch, _config())
    state.get_or_create_slot("theirs", app="app-b")
    entered: list[str] = []
    real_acquire = chat_handlers.acquire_slot_create_name

    async def watched(st: Any, key: str, principal: str = "") -> Any:
        entered.append(principal)
        return await real_acquire(st, key, principal)

    monkeypatch.setattr(chat_handlers, "acquire_slot_create_name", watched)
    release = await txn.acquire_slot_create_name(state, "free")
    assert release is not None
    assert txn.SLOT_CREATE_NAME_WAIT_SECONDS > WAIT
    try:
        async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
            busy = await _app_post(client, "free")
            not_yours = await _app_post(client, "theirs")
    finally:
        release()
    assert busy == not_yours == (404, {"error": "not found", "code": "slot_not_found"})
    assert entered == [], "the app entered the name wait"
    assert "free" not in state._slots
    assert txn._NAME_GATES.get(state, {}).get("free") is None


@pytest.mark.asyncio
async def test_an_app_still_waits_behind_its_own_create_of_a_name(state: Any) -> None:
    """The check is per principal: the app's own earlier create is not another's."""
    release = await txn.acquire_slot_create_name(state, "mine", "app-a")
    assert release is not None
    try:
        assert not txn.name_held_by_another(state, "mine", "app-a")
        assert txn.name_held_by_another(state, "mine", "app-b")
        assert txn.name_held_by_another(state, "mine", "")
    finally:
        release()
    assert not txn.name_held_by_another(state, "mine", "app-b")
    assert txn._NAME_GATES.get(state, {}).get("mine") is None


# -- the cron run-start bind has a fallback past the wait -------------------------


@pytest.mark.asyncio
async def test_a_cron_run_start_past_the_wait_bound_falls_back_without_raising(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_cron_slot`` meets a create of its key that outlasts the bounded wait.

    It starts the run without a tab, as the result injection keeps the run in
    the transcript, instead of raising "still being built" into the run.

    Mutation pin: call ``_bind_cron_slot`` with no ValueError fallback and this raises.
    """
    from kiro_crew.dashboard import cron_inject

    monkeypatch.setattr(txn, "SLOT_CREATE_NAME_WAIT_SECONDS", 0.01)
    job = SimpleNamespace(
        id="j1",
        name="J",
        persistent_session=True,
        hide_in_chat=False,
        member_id="",
        agent_id="default",
        memory_store="",
        message="",
        chat_folder_id="",
    )
    pending = SimpleNamespace(slot=SimpleNamespace(key="cron-j1", linked_session_key=""))
    create = txn.PendingSlotCreate(state, pending)
    try:
        await asyncio.wait_for(cron_inject.ensure_cron_slot(state, job), WAIT)
    finally:
        await create.settle()
    assert "cron-j1" not in state._slots
