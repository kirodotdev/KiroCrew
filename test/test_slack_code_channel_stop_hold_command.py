"""A held turn writes no ``/`` command either (``stop_hold``; GPT 6.1 on 6ba5919612).

A command turn, like a prompt, awaits inside the transport before its write (the
timeout resolution, the session's readiness, the first-turn hold, the write lock). The
hold's write-time ask (``stop_hold.write_gate``) goes with the command through both real
providers, on the native ``commands/execute`` path and on the prompt fallback, and is
asked under the write lock right before the frame: a Stop that landed in those awaits
writes nothing, and a person's message stamped after it still sends.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.slack.code_channels import stop_hold

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_pending() -> Any:
    stop_hold._keys.clear()
    stop_hold._channels.clear()
    yield
    stop_hold._keys.clear()
    stop_hold._channels.clear()


def _slot(key: str) -> Any:
    return SimpleNamespace(key=key, _stop_held=False, _stop_hold_seq=0)


def _press(slot: Any) -> None:
    """A Stop on *slot*: a newer press than any stamp read before it."""
    stop_hold._last = next(stop_hold._presses)
    stop_hold._hold(slot, stop_hold._last)


def _ask(slot: Any, stamp: str = "") -> Any:
    from kiro_crew.providers.acp import AcpProvider

    return stop_hold.write_gate(object.__new__(AcpProvider), slot, stamp, call="stream_command")[
        "prompt_gate"
    ]


# ── the selector reads the method that is called ────────────────────────────────


async def test_the_gate_is_offered_only_by_the_method_that_is_called() -> None:
    """An asymmetric client, both ways round: the keyword goes only to the method that
    declares it, so a client whose ``stream`` takes the gate and whose
    ``stream_command`` does not (or the reverse) is called exactly as before."""
    slot = _slot("chat-c-1")

    class PromptOnly:
        async def stream(self, message: str, *, prompt_gate: Any = None) -> Any:
            yield message

        async def stream_command(self, command: str) -> Any:
            yield command

    class CommandOnly:
        async def stream(self, message: str) -> Any:
            yield message

        async def stream_command(self, command: str, *, prompt_gate: Any = None) -> Any:
            yield command

    assert set(stop_hold.write_gate(PromptOnly(), slot, "")) == {"prompt_gate"}
    assert stop_hold.write_gate(PromptOnly(), slot, "", call="stream_command") == {}
    assert stop_hold.write_gate(CommandOnly(), slot, "") == {}
    assert set(stop_hold.write_gate(CommandOnly(), slot, "", call="stream_command")) == {
        "prompt_gate"
    }


async def test_both_real_providers_declare_the_gate_on_both_methods() -> None:
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    slot = _slot("chat-c-2")
    for cls in (AcpProvider, AcpSessionProvider):
        client = object.__new__(cls)
        for call in ("stream", "stream_command"):
            assert set(stop_hold.write_gate(client, slot, "", call=call)) == {"prompt_gate"}, (
                cls.__name__,
                call,
            )


# ── the transport asks it under the write lock ──────────────────────────────────


@pytest.mark.parametrize(
    ("command", "native"), [("/tools", True), ("/help", False)], ids=["native", "prompt"]
)
async def test_a_stop_while_the_command_waits_for_the_write_lock_writes_nothing(
    command: str, native: bool
) -> None:
    """The shared runtime's handle: the command waits behind another session's frame; a
    Stop there is asked under the lock, nothing is written or routed, and the handle
    stays reusable. Both the native ``commands/execute`` and the prompt fallback."""
    from acp_runtime_helpers import _make_runtime, _register

    from kiro_crew.acp.runtime import AcpSessionHandle
    from kiro_crew.acp.session_handle_turn import command_is_native
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.testing.wait import await_until, until_parked

    rt, _, proc = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    queues = _register(rt, "sA")
    handle = AcpSessionHandle("sA", queues["sA"], rt)
    assert command_is_native(handle, command) is native
    slot = _slot("chat-c-3")
    lock = rt._stdin_write_lock()
    await lock.acquire()  # another session's frame holds the shared stdin
    first = asyncio.ensure_future(
        handle.stream_command(command, prompt_gate=_ask(slot)).__anext__()
    )
    await until_parked(lock)
    _press(slot)  # the Stop lands while the command waits
    lock.release()
    with pytest.raises(stop_hold.HeldAtWrite):
        await await_until(first, what="the held command's refusal")
    proc.stdin.write.assert_not_called()
    assert rt._routed_requests == {} and handle.is_turn_active is False


async def test_a_stop_while_the_direct_client_gets_ready_writes_no_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct client's native path: a Stop during its readiness await (after the
    timeout resolution) is asked by ``_send_request`` under the write lock; nothing is
    written. A person's stamp from after the Stop writes the frame."""
    from kiro_crew.acp import client_turn_control, launch_tuning

    @contextlib.asynccontextmanager
    async def _barrier(_client: Any) -> Any:
        yield

    monkeypatch.setattr(launch_tuning, "switch_barrier", _barrier)

    async def _timeout(timeout: Any) -> float:
        return 5.0

    monkeypatch.setattr(client_turn_control, "_effective_prompt_timeout_async", _timeout)

    def client(slot: Any, stamp: str) -> Any:
        ready, entered = asyncio.Event(), asyncio.Event()
        written: list[str] = []

        async def ensure_ready() -> None:
            entered.set()
            await ready.wait()

        async def send_request(method: str, params: dict, *, before_write: Any = None) -> int:
            if before_write is not None:
                before_write()  # the transport calls it under its write lock
            written.append(method)
            return 1

        async def dispatch(*_a: Any, **_k: Any) -> Any:
            yield SimpleNamespace(kind="complete")

        fake = SimpleNamespace(
            _cancelled=True,
            _session_id="s1",
            ensure_ready=ensure_ready,
            _send_request=send_request,
            _dispatch_events=dispatch,
        )
        gen = client_turn_control.stream_command(fake, "/tools", prompt_gate=_ask(slot, stamp))
        return gen, ready, entered, written

    slot = _slot("chat-c-4")
    gen, ready, entered, written = client(slot, "")
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.wait_for(entered.wait(), 5)
    _press(slot)
    ready.set()
    with pytest.raises(stop_hold.HeldAtWrite):
        await asyncio.wait_for(first, 5)
    assert written == []
    person = stop_hold.stamp(stop_hold.seen(), True)  # a person's, after the Stop
    gen, ready, entered, written = client(slot, person)
    ready.set()
    assert (await asyncio.wait_for(gen.__anext__(), 5)).kind == "complete"
    assert written == ["_kiro.dev/commands/execute"]


async def test_an_old_stamp_never_releases_and_a_new_one_releases_only_its_slot() -> None:
    held, other = _slot("chat-c-5"), _slot("chat-c-6")
    old = stop_hold.stamp(stop_hold.seen(), True)  # read before the Stop
    _press(held)
    _press(other)
    with pytest.raises(stop_hold.HeldAtWrite):
        _ask(held, old)()
    new = stop_hold.stamp(stop_hold.seen(), True)
    _ask(held, new)()  # released for this session
    assert stop_hold.decide(other, "") == stop_hold.HELD
    _ask(_slot("chat-c-free"))()  # an unheld session: nothing asked of it changes


# ── both providers hand it on, native and fallback ──────────────────────────────


@pytest.mark.parametrize("backend", ["", "claude"], ids=["native", "prompt"])
async def test_the_direct_provider_hands_the_gate_to_the_transport(backend: str) -> None:
    from unittest.mock import MagicMock

    from test_acp_provider import _async_iter, _build_provider, _drain

    from kiro_crew.acp.types import AcpEvent

    provider = _build_provider(backend=backend)
    provider._client.stream_command = MagicMock(return_value=_async_iter([AcpEvent(kind="x")]))
    provider._client.stream_events = MagicMock(return_value=_async_iter([AcpEvent(kind="x")]))
    gate = _ask(_slot("chat-c-7"))
    await _drain(provider.stream_command("/compact", prompt_gate=gate))
    used = provider._client.stream_command if not backend else provider._client.stream_events
    unused = provider._client.stream_events if not backend else provider._client.stream_command
    used.assert_called_once_with("/compact", prompt_gate=gate)
    unused.assert_not_called()
    used.reset_mock()
    used.return_value = _async_iter([AcpEvent(kind="x")])
    await _drain(provider.stream_command("/compact"))
    used.assert_called_once_with("/compact")  # no gate given: called exactly as before


async def test_a_direct_provider_on_a_shared_session_hands_the_gate_through() -> None:
    """GPT prompt / Opus r27: on KAS or codex the direct provider's client is the
    shared ``AcpSessionProvider`` and ``/compact`` takes the prompt fallback through its
    ``stream_events``, which hands the gate on to the handle's prompt (and runs as
    before with none)."""
    from test_acp_provider import _build_provider, _drain
    from test_acp_session_provider import _make_handle, _make_runtime

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent

    seen: list[Any] = []

    async def prompt(message: str, *_a: Any, prompt_gate: Any = "unset", **_k: Any) -> Any:
        seen.append((message, prompt_gate))
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="")

    handle = _make_handle()
    handle.prompt = prompt
    shared = AcpSessionProvider(handle, _make_runtime(acp_backend="kas"))
    provider = _build_provider(backend="kas")
    provider._client = shared
    gate = _ask(_slot("chat-c-9"))
    await _drain(provider.stream_command("/compact", prompt_gate=gate))
    await _drain(provider.stream_command("/compact"))
    assert [g for _m, g in seen] == [gate, "unset"], seen


async def test_the_shared_provider_hands_the_gate_to_the_handle() -> None:
    from test_acp_session_provider import _make_handle, _make_runtime

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent

    seen: list[Any] = []

    async def stream_command(command: str, *, prompt_gate: Any = "unset") -> Any:
        seen.append((command, prompt_gate))
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="")

    handle = _make_handle()
    handle.stream_command = stream_command
    provider = AcpSessionProvider(handle, _make_runtime())
    gate = _ask(_slot("chat-c-8"))
    [e async for e in provider.stream_command("/tools", prompt_gate=gate)]
    [e async for e in provider.stream_command("/tools")]
    assert seen == [("/tools", gate), ("/tools", "unset")]


# ── the dashboard's command turn passes it ──────────────────────────────────────


@pytest.mark.parametrize("held", [False, True], ids=["free", "held-at-write"])
async def test_a_command_turn_passes_the_holds_ask_to_stream_command(held: bool) -> None:
    """``_run_chat``'s ``/`` branch passes the hold's ask: a free session runs the
    command as before; a Stop that lands before the write ends the turn quietly as
    stopped, with the command's frame never written."""
    from turn_harness import ScriptedProvider, TurnScript, run_turn

    asked: list[str] = []

    class GatedProvider(ScriptedProvider):
        def stream_command(self, command: str, *, prompt_gate: Any = None) -> Any:
            self.record("stream_command", command)
            assert prompt_gate is not None, "the command turn passed no gate"

            async def events() -> Any:
                await asyncio.sleep(0)  # one of the transport's awaits
                if held:
                    _press(self._ctx.slot)  # the Stop lands there
                prompt_gate()  # the transport's ask under its write lock
                asked.append(command)
                async for event in self._events():
                    yield event

            return events()

    record = await run_turn(TurnScript(message="/compact", provider=GatedProvider))
    assert [c.args[0] for c in record.calls("stream_command")] == ["/compact"]
    if held:
        assert asked == [] and record.stop_reason == "failed: channel_stopped"
        assert not record.rows("error")
    else:
        assert asked == ["/compact"]
