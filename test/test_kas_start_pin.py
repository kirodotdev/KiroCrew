"""A config-option write takes its answer off the session queue, so a start can send it early.

KAS answers every request late while it connects a new session's MCP servers. A
configured model sent once the start's MCP wait was over could outlast its 10 s
bound, so the start paid the bound and recorded the session on the wrong model.
The start now sends the pin when the mode is set (``create_session``'s
``on_mode_set``), and ``set_config_option`` reads its answer straight from the
runtime: the MCP wait drains the session queue and throws every response on it
away. Its wait lasts the bound from the send, or until the start's MCP wait ends
when that is later.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _feed, _make_runtime, _start_reader, _stop_reader

from kiro_crew.acp import session_handle_model_selection as model_selection
from kiro_crew.acp.client import AcpError, AcpTimeoutError
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SET_CONFIG_OPTION
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.testing.wait import async_wait_until, finished

PIN = "claude-opus-5.5"
#: A model KAS serves somewhere, but not on this session's select.
UNLISTED = "claude-sonnet-4.5"
#: The model a session the account does not serve ``auto`` is switched to.
SERVED = "claude-haiku-4.5"
SESSION = "sess-b"
LEVELS = ["low", "medium", "high", "xhigh", "max"]

#: The options KAS answers a concrete model write with: it serves ``effortLevel`` now.
OPTIONS_WITH_MODEL = [
    {
        "type": "select",
        "id": "model",
        "currentValue": PIN,
        "options": [{"value": v, "name": v} for v in ("auto", PIN)],
    },
    {
        "type": "select",
        "id": "effortLevel",
        "currentValue": "medium",
        "options": [{"value": v, "name": v} for v in LEVELS],
    },
]
#: A new session's ``model`` select: on ``auto``, which the account is served.
AUTO_SELECT = dict(OPTIONS_WITH_MODEL[0], currentValue="auto")


def _rig(backend: str = ACP_BACKEND_KAS, *, select: bool = True):
    """A handle on a real ``AcpRuntime`` whose fake process the test answers for.

    ``select=False`` is an engine whose ``session/new`` answer carries no ``model``
    select (KAS 0.66.117 and later send it in a ``config_option_update``).
    """
    rt, reader, proc = _make_runtime()
    rt._acp_backend = backend
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues[SESSION] = queue
    handle = AcpSessionHandle(
        session_id=SESSION, queue=queue, runtime=rt, watchdog=WatchdogSettings()
    )
    if select:
        handle.store_session_config({"sessionId": SESSION, "configOptions": [AUTO_SELECT]})
    return rt, reader, proc, handle


def _model_frames(proc) -> list[dict]:
    return [
        w
        for w in _written(proc)
        if w.get("method") == METHOD_SET_CONFIG_OPTION
        and (w.get("params") or {}).get("configId") == "model"
    ]


def _model_writes(proc) -> list[str]:
    return [w["params"]["value"] for w in _model_frames(proc)]


async def _model_write(proc, index: int) -> dict:
    """The session's *index*-th model write, once it is out."""
    await async_wait_until(
        lambda: len(_model_frames(proc)) > index,
        describe=lambda: f"no model write #{index} in {_written(proc)}",
    )
    return _model_frames(proc)[index]


def _written(proc) -> list[dict]:
    return [json.loads(c.args[0].decode()) for c in proc.stdin.write.call_args_list]


async def _written_write(proc, config_id: str) -> dict:
    """The config-option request the session wrote for *config_id*, once it is out."""

    def frame() -> dict | None:
        for sent in _written(proc):
            params = sent.get("params") or {}
            if (
                sent.get("method") == METHOD_SET_CONFIG_OPTION
                and params.get("configId") == config_id
            ):
                return sent
        return None

    await async_wait_until(frame, describe=lambda: f"no {config_id} write in {_written(proc)}")
    found = frame()
    assert found is not None
    return found


def _answer(reader, request: dict, result: dict | None = None, error: dict | None = None) -> None:
    body = {"error": error} if error is not None else {"result": result or {}}
    _feed(reader, {"jsonrpc": "2.0", "id": request["id"], **body})


def _as_on_mode_set(drain_done: asyncio.Event, start, *args):
    """Call *start* as ``create_session`` calls ``on_mode_set``: the tasks it starts see *drain_done*."""
    previous = model_selection.START_WAIT_END.get()
    model_selection.START_WAIT_END.set(drain_done)
    try:
        return start(*args)
    finally:
        model_selection.START_WAIT_END.set(previous)


@pytest.mark.asyncio
async def test_a_drain_running_alongside_no_longer_loses_the_reply() -> None:
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    # A start's drain reads the session queue and discards every response on it.
    drain = asyncio.ensure_future(
        handle.drain_init(duration=60.0, idle_exit=60.0, no_report_ceiling=0.0)
    )
    write = asyncio.ensure_future(handle.set_config_option("model", PIN))
    try:
        _answer(reader, await _written_write(proc, "model"), {"configOptions": OPTIONS_WITH_MODEL})
        await finished(write, "the model write, answered while the drain reads the queue")
        assert handle.supports_config_option("effortLevel")
    finally:
        drain.cancel()
        write.cancel()
        await asyncio.wait({drain, write})
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_an_answer_after_the_bound_counts_while_the_start_still_waits(monkeypatch) -> None:
    # The bound is spent at once, so only the start's own wait keeps the write open.
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    write = _as_on_mode_set(
        asyncio.Event(), asyncio.ensure_future, handle.set_config_option("model", PIN)
    )
    try:
        _answer(reader, await _written_write(proc, "model"), {"configOptions": OPTIONS_WITH_MODEL})
        await finished(write, "the model write, answered during the start's MCP wait")
        assert handle.supports_config_option("effortLevel")
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_no_answer_by_the_end_of_the_start_wait_times_out(monkeypatch) -> None:
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    drain_done = asyncio.Event()
    write = _as_on_mode_set(
        drain_done, asyncio.ensure_future, handle.set_config_option("model", PIN)
    )
    try:
        await _written_write(proc, "model")
        assert not write.done()
        drain_done.set()
        with pytest.raises(AcpTimeoutError):
            await finished(write, "the model write after the start's MCP wait ended")
        assert rt._pending_requests == {}
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_without_a_start_wait_the_bound_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    try:
        assert model_selection.START_WAIT_END.get() is None
        with pytest.raises(AcpTimeoutError):
            await finished(handle.set_config_option("model", PIN), "the unanswered model write")
        assert rt._pending_requests == {}
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_an_error_answer_keeps_the_mapping_and_its_code() -> None:
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    write = asyncio.ensure_future(handle.set_config_option("model", PIN))
    try:
        _answer(
            reader,
            await _written_write(proc, "model"),
            error={"code": -32602, "message": "Invalid params"},
        )
        with pytest.raises(AcpError) as refused:
            await finished(write, "the refused model write")
        assert refused.value.code == -32602
    finally:
        await _stop_reader(reader_task)


# --------------------------------------------------------------------------- #
# create_session sends the caller's early writes before its MCP wait.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_write_sent_when_the_mode_is_set_is_answered_inside_the_mcp_wait(
    monkeypatch,
) -> None:
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "sessionId": SESSION,
                "modes": {
                    "currentModeId": "kirocrew",
                    "availableModes": [{"id": "kirocrew"}, {"id": "ops"}],
                },
            },
            {},
        ]
    )
    reader_task = await _start_reader(rt)
    mcp_ready = asyncio.Event()
    in_wait = asyncio.Event()

    async def _mcp_wait(*_args, **_kwargs) -> None:
        # The start's MCP wait, which ends only when the test says so.
        in_wait.set()
        await mcp_ready.wait()

    writes: list[asyncio.Task[None]] = []

    def _on_mode_set(handle: AcpSessionHandle) -> None:
        writes.append(asyncio.ensure_future(handle.set_config_option("model", PIN)))

    with patch.object(AcpSessionHandle, "drain_init", _mcp_wait):
        start = asyncio.ensure_future(
            rt.create_session(agent="ops", mcp_servers=[], on_mode_set=_on_mode_set)
        )
        try:
            _answer(
                reader, await _written_write(proc, "model"), {"configOptions": OPTIONS_WITH_MODEL}
            )
            await finished(writes[0], "the early model write")
            assert in_wait.is_set() and not start.done()
            mcp_ready.set()
            handle = await finished(start, "create_session")
        finally:
            mcp_ready.set()
            await _stop_reader(reader_task)
    assert handle.supports_config_option("effortLevel")
    assert model_selection.START_WAIT_END.get() is None


@pytest.mark.asyncio
async def test_a_write_the_mcp_wait_sends_itself_keeps_its_own_bound(monkeypatch) -> None:
    # The served-default switch writes from inside the wait: were it to wait for the
    # wait's end, it would wait for itself, and an engine that never answers would
    # hold the start for good.
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, _reader, _proc = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "sessionId": SESSION,
                "modes": {
                    "currentModeId": "kirocrew",
                    "availableModes": [{"id": "kirocrew"}, {"id": "ops"}],
                },
            },
            {},
        ]
    )
    reader_task = await _start_reader(rt)
    own_write: list[BaseException] = []

    async def _mcp_wait(handle: AcpSessionHandle, *_args, **_kwargs) -> None:
        try:
            await handle.set_config_option("model", PIN)
        except AcpTimeoutError as exc:
            own_write.append(exc)

    with patch.object(AcpSessionHandle, "drain_init", _mcp_wait):
        try:
            await finished(
                rt.create_session(agent="ops", mcp_servers=[], on_mode_set=lambda _handle: None),
                "create_session, whose MCP wait writes and gets no answer",
            )
        finally:
            await _stop_reader(reader_task)
    assert len(own_write) == 1


# --------------------------------------------------------------------------- #
# The provider's early pin: model first, then the effort the model's answer offers.
# --------------------------------------------------------------------------- #


def _pin_module():
    """``kas_start_pin``, imported in the test body so the handle tests above still run without it."""
    from kiro_crew.providers import kas_start_pin

    return kas_start_pin


def _provider(effort: str = "xhigh") -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
    client = MagicMock()
    client.backend = ACP_BACKEND_KAS
    client._model = PIN
    provider._client = client
    provider._effort_per_model = {PIN: effort} if effort else {}
    provider._effort_defaults = {}
    return provider


@pytest.mark.asyncio
async def test_the_pin_and_its_effort_land_while_the_mcp_wait_runs(monkeypatch) -> None:
    kas_start_pin = _pin_module()
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    provider = _provider()
    tasks: list[asyncio.Task[float | None]] = []
    drain_done = asyncio.Event()
    try:
        _as_on_mode_set(drain_done, kas_start_pin.start_pin_early, provider, PIN, tasks, handle)
        assert len(tasks) == 1
        _answer(reader, await _written_write(proc, "model"), {"configOptions": OPTIONS_WITH_MODEL})
        effort = await _written_write(proc, "effortLevel")
        assert effort["params"]["value"] == "xhigh"
        _answer(reader, effort, {"configOptions": OPTIONS_WITH_MODEL})
        await finished(asyncio.wait(tasks), "the early pin and its effort")
        drain_done.set()
        sent, waited_ms = await kas_start_pin.finish_early_pin(tasks)
    finally:
        await _stop_reader(reader_task)
    assert sent is True and waited_ms is not None
    assert handle.served_model == PIN
    assert provider._start_effort_applied is True


@pytest.mark.asyncio
async def test_the_early_effort_uses_the_model_the_session_serves() -> None:
    rt, reader, proc, handle = _rig()
    handle.store_session_config(
        {
            "sessionId": SESSION,
            "configOptions": [
                dict(OPTIONS_WITH_MODEL[0], currentValue="auto"),
                OPTIONS_WITH_MODEL[1],
            ],
        }
    )
    handle._model = ""
    handle._resolved_model_id = UNLISTED
    reader_task = await _start_reader(rt)
    provider = _provider()
    provider._effort_per_model = {PIN: "xhigh", UNLISTED: "low"}
    write = asyncio.ensure_future(provider._apply_initial_effort(client=handle))
    try:
        assert handle.model == ""
        assert handle.served_model == UNLISTED
        effort = await _written_write(proc, "effortLevel")
        assert effort["params"]["value"] == "low"
        _answer(reader, effort, {"configOptions": OPTIONS_WITH_MODEL})
        await finished(write, "the early effort keyed on the model the session serves")
    finally:
        write.cancel()
        await asyncio.wait({write})
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_the_set_model_phase_counts_the_pin_write_and_not_the_effort(monkeypatch) -> None:
    kas_start_pin = _pin_module()
    clock = [100.0]
    monkeypatch.setattr(kas_start_pin, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    provider = _provider()
    tasks: list[asyncio.Task[float | None]] = []
    try:
        _as_on_mode_set(
            asyncio.Event(), kas_start_pin.start_pin_early, provider, PIN, tasks, handle
        )
        # ``create_session`` has returned and the start waits for the pin from t=100.
        finish = asyncio.ensure_future(kas_start_pin.finish_early_pin(tasks))
        model = await _written_write(proc, "model")
        clock[0] = 102.0
        _answer(reader, model, {"configOptions": OPTIONS_WITH_MODEL})
        effort = await _written_write(proc, "effortLevel")
        clock[0] = 107.0
        _answer(reader, effort, {"configOptions": OPTIONS_WITH_MODEL})
        sent, waited_ms = await finished(finish, "the start's wait for the early pin")
    finally:
        await _stop_reader(reader_task)
    assert sent is True
    assert waited_ms == 2000.0


@pytest.mark.asyncio
async def test_a_refused_pin_is_still_recorded_and_pushes_no_effort(monkeypatch) -> None:
    kas_start_pin = _pin_module()
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    provider = _provider()
    tasks: list[asyncio.Task[float | None]] = []
    drain_done = asyncio.Event()
    try:
        # A pin the session's own select does not list is refused before the wire.
        _as_on_mode_set(
            drain_done, kas_start_pin.start_pin_early, provider, UNLISTED, tasks, handle
        )
        await finished(asyncio.wait(tasks), "the refused early pin")
        assert handle.model_pin_refused == UNLISTED
        # An engine that refuses a listed pin leaves the session on its default.
        tasks.clear()
        _as_on_mode_set(drain_done, kas_start_pin.start_pin_early, provider, PIN, tasks, handle)
        _answer(
            reader, await _written_write(proc, "model"), error={"code": -32603, "message": "boom"}
        )
        await finished(asyncio.wait(tasks), "the early pin the engine refused")
    finally:
        await _stop_reader(reader_task)
    assert handle.served_model != PIN
    assert [
        w for w in _written(proc) if (w.get("params") or {}).get("configId") == "effortLevel"
    ] == []


@pytest.mark.asyncio
async def test_an_unpinned_start_sends_no_early_model() -> None:
    kas_start_pin = _pin_module()
    _rt, _reader, _proc, handle = _rig()
    tasks: list[asyncio.Task] = []
    kas_start_pin.start_pin_early(_provider(), "", tasks, handle)
    kas_start_pin.start_pin_early(_provider(), "auto", tasks, handle)
    assert tasks == []
    assert handle.served_default_after_pin is False


@pytest.mark.asyncio
async def test_a_select_that_comes_after_session_new_still_sends_the_pin_in_the_wait(
    monkeypatch,
) -> None:
    kas_start_pin = _pin_module()
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig(select=False)
    reader_task = await _start_reader(rt)
    tasks: list[asyncio.Task] = []
    drain_done = asyncio.Event()
    try:
        _as_on_mode_set(drain_done, kas_start_pin.start_pin_early, _provider(), PIN, tasks, handle)
        # The engine's late config_option_update, as the MCP wait's drain stores it.
        handle.absorb_config_options([AUTO_SELECT])
        _answer(reader, await _written_write(proc, "model"), {"configOptions": OPTIONS_WITH_MODEL})
        _answer(
            reader, await _written_write(proc, "effortLevel"), {"configOptions": OPTIONS_WITH_MODEL}
        )
        await finished(asyncio.wait(tasks), "the early pin, once the late select came")
        assert not drain_done.is_set()
        drain_done.set()
        handled, _waited_ms = await kas_start_pin.finish_early_pin(tasks)
    finally:
        await _stop_reader(reader_task)
    assert handled is True
    assert handle.served_model == PIN


@pytest.mark.asyncio
async def test_a_select_that_never_comes_sends_nothing_early() -> None:
    kas_start_pin = _pin_module()
    _rt, _reader, proc, handle = _rig(select=False)
    tasks: list[asyncio.Task] = []
    drain_done = asyncio.Event()
    _as_on_mode_set(drain_done, kas_start_pin.start_pin_early, _provider(), PIN, tasks, handle)
    drain_done.set()
    handled, waited_ms = await finished(
        kas_start_pin.finish_early_pin(tasks), "the early pin whose select never came"
    )
    # The start sends the pin itself after the wait, as it does with no early pin.
    assert (handled, waited_ms) == (False, None)
    assert handle.served_default_after_pin is False
    assert _model_writes(proc) == []


def _absorb_select_without_auto(handle: AcpSessionHandle) -> None:
    """A select on ``auto`` that this account is not served: the default needs a switch."""
    handle.absorb_config_options(
        [
            {
                "type": "select",
                "id": "model",
                "currentValue": "auto",
                "options": [{"value": v, "name": v} for v in (SERVED, PIN)],
            }
        ]
    )
    assert handle._kas_auto_fallback == SERVED


@pytest.mark.asyncio
async def test_the_served_default_switch_never_lands_on_the_early_pin(monkeypatch) -> None:
    kas_start_pin = _pin_module()
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig(select=False)
    _absorb_select_without_auto(handle)
    reader_task = await _start_reader(rt)
    tasks: list[asyncio.Task] = []
    try:
        _as_on_mode_set(
            asyncio.Event(), kas_start_pin.start_pin_early, _provider(effort=""), PIN, tasks, handle
        )
        pin = await _written_write(proc, "model")
        # The MCP wait's own check, while the pin is out.
        await finished(handle.ensure_served_default(), "the check inside the MCP wait")
        _answer(reader, pin, {"configOptions": OPTIONS_WITH_MODEL})
        await finished(asyncio.wait(tasks), "the early pin")
        # The same check once the pin has landed, as the MCP wait's settle makes it.
        await finished(handle.ensure_served_default(), "the check after the pin landed")
    finally:
        await _stop_reader(reader_task)
    assert _model_writes(proc) == [PIN]
    assert handle.served_model == PIN


@pytest.mark.asyncio
async def test_a_pin_that_does_not_land_gets_the_served_default_switch_after_it(
    monkeypatch,
) -> None:
    kas_start_pin = _pin_module()
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig(select=False)
    _absorb_select_without_auto(handle)
    reader_task = await _start_reader(rt)
    tasks: list[asyncio.Task] = []
    try:
        _as_on_mode_set(
            asyncio.Event(), kas_start_pin.start_pin_early, _provider(effort=""), PIN, tasks, handle
        )
        await finished(handle.ensure_served_default(), "the check inside the MCP wait")
        _answer(reader, await _model_write(proc, 0), error={"code": -32603, "message": "boom"})
        _answer(
            reader,
            await _model_write(proc, 1),
            {"configOptions": [dict(AUTO_SELECT, currentValue=SERVED)]},
        )
        await finished(asyncio.wait(tasks), "the refused early pin and the switch after it")
    finally:
        await _stop_reader(reader_task)
    assert _model_writes(proc) == [PIN, SERVED]
    assert handle.served_model == SERVED


@pytest.mark.asyncio
async def test_a_withheld_pin_stays_recorded_after_the_switch(monkeypatch) -> None:
    kas_start_pin = _pin_module()
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, reader, proc, handle = _rig(select=False)
    _absorb_select_without_auto(handle)
    reader_task = await _start_reader(rt)
    tasks: list[asyncio.Task] = []
    try:
        # A pin the select does not list is withheld; the switch follows it.
        _as_on_mode_set(
            asyncio.Event(),
            kas_start_pin.start_pin_early,
            _provider(effort=""),
            UNLISTED,
            tasks,
            handle,
        )
        switch = await _model_write(proc, 0)
        # The MCP wait's own check, while the task's switch is still out.
        await finished(handle.ensure_served_default(), "the check inside the MCP wait")
        _answer(reader, switch, {"configOptions": [dict(AUTO_SELECT, currentValue=SERVED)]})
        await finished(asyncio.wait(tasks), "the withheld pin and the switch after it")
    finally:
        await _stop_reader(reader_task)
    # One switch, sent by the pin's task, and the refusal survives it.
    assert _model_writes(proc) == [SERVED]
    assert handle.served_model == SERVED
    assert handle.model_pin_refused == UNLISTED


@pytest.mark.asyncio
async def test_an_unanswered_pin_does_not_hold_the_mcp_wait(monkeypatch) -> None:
    kas_start_pin = _pin_module()
    monkeypatch.setattr(model_selection, "_CONFIG_OPTION_REPLY_TIMEOUT", 0.0)
    rt, _reader, proc, handle = _rig()
    reader_task = await _start_reader(rt)
    tasks: list[asyncio.Task] = []
    drain_done = asyncio.Event()
    try:
        _as_on_mode_set(drain_done, kas_start_pin.start_pin_early, _provider(), PIN, tasks, handle)
        await _written_write(proc, "model")
        # The pin's answer waits for the MCP wait's end, so the wait's own check
        # must not wait for the pin.
        await finished(handle.ensure_served_default(), "the check inside the MCP wait")
        assert not tasks[0].done()
        drain_done.set()
        handled, _waited_ms = await finished(
            kas_start_pin.finish_early_pin(tasks), "the unanswered early pin"
        )
    finally:
        await _stop_reader(reader_task)
    assert handled is True
    assert handle.served_model != PIN
