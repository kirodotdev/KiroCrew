"""A code channel's Stop holds a hook command up to the OS spawn, on every platform.

On Windows the gateway's loop builds each child on a worker thread
(``platform_compat_offloop_spawn``), so a build can wait in that pool's queue after
every ask on the loop. The hook's ask travels with the build (``spawn_admit``) and
is asked on the worker right before ``Popen``. These tests drive that path on any
platform with a stub ``Popen`` and transport, and a one-worker pool the test holds.
The hook's ask itself is read-only (``stop_hold.held_now``), so it is safe there.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew.slack.code_channels import stop_hold
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import await_until, default_timeout

#: The stub transports' ``_start`` builds through ``_stub_spawn.Popen`` (the module
#: global the off-loop spawn re-points at the child its worker built).
_stub_spawn = SimpleNamespace(Popen=None)


class _StubTransport:
    """A subprocess transport that builds through ``_stub_spawn.Popen`` and binds nothing."""

    def __init__(self, loop: Any, protocol: Any, args: Any, shell: bool, *rest: Any, **kw: Any):
        self._start(args=args, shell=shell)
        loop.call_soon(kw["waiter"].set_result, None)

    def _start(self, args: Any, shell: bool, **kwargs: Any) -> None:
        self.child = _stub_spawn.Popen(args, shell=shell)

    def close(self) -> None:
        return None

    async def _wait(self) -> int:
        return 0


class _Pool(ThreadPoolExecutor):
    """One worker; a submission past the first sets *queued* (on the loop)."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(max_workers=1)
        self.loop, self.submitted = loop, 0
        self.queued = asyncio.Event()

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        self.submitted += 1
        if self.submitted > 1:
            self.loop.call_soon_threadsafe(self.queued.set)
        return super().submit(*args, **kwargs)


class _Children:
    """The stub ``Popen``: records every child built, builds none."""

    def __init__(self) -> None:
        self.built: list[Any] = []

    def __call__(self, args: Any, **_kw: Any) -> Any:
        self.built.append(args)
        return SimpleNamespace(args=args, pid=UNALLOCATABLE_PID, returncode=None)


class _OffLoop(asyncio.SelectorEventLoop):
    """A loop whose subprocess transports build their child off the loop, as the
    Windows gateway loop does, with the stub ``Popen`` and transport."""

    children: _Children

    async def _make_subprocess_transport(  # type: ignore[override]
        self,
        protocol: Any,
        args: Any,
        shell: bool,
        stdin: Any,
        stdout: Any,
        stderr: Any,
        bufsize: int,
        extra: Any = None,
        **kwargs: Any,
    ) -> Any:
        return await offloop.make_subprocess_transport_off_loop(
            self,
            protocol,
            args,
            shell,
            stdin,
            stdout,
            stderr,
            bufsize,
            extra,
            popen_factory=self.children,
            transport_cls=_StubTransport,
            popen_global="_stub_spawn",
            **kwargs,
        )


def _run(monkeypatch: pytest.MonkeyPatch, body: Any) -> Any:
    """Run *body(loop, pool, release)* on an :class:`_OffLoop` whose spawn pool's one
    worker is busy until the test sets *release*."""
    with asyncio.Runner(loop_factory=_OffLoop) as runner:
        loop = runner.get_loop()
        assert isinstance(loop, _OffLoop)
        loop.children = _Children()
        pool = _Pool(loop)
        monkeypatch.setattr(offloop, "spawn_executor", lambda: pool)
        release = threading.Event()
        pool.submit(release.wait, default_timeout())  # the busy worker
        try:
            return runner.run(body(loop, pool, release))
        finally:
            release.set()
            pool.shutdown(wait=True)


def _direct_spawn(loop: Any, ask: Any = None) -> asyncio.Task[Any]:
    """A spawn in its own task, whose own context carries *ask* (``spawn_admit``)."""
    context = contextvars.copy_context()
    context.run(offloop.spawn_admit.set, ask)
    spawn = offloop.make_subprocess_transport_off_loop(
        loop,
        None,
        ["x"],
        False,
        None,
        None,
        None,
        0,
        popen_factory=loop.children,
        transport_cls=_StubTransport,
        popen_global="_stub_spawn",
    )
    return loop.create_task(spawn, context=context)


def _hook_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, *, wrap: bool) -> Any:
    """``run_script_hook`` on the Windows spawn forms, everything before the spawn
    stubbed; returns the sandbox's temporary file."""
    import kiro_crew.hook_event_governance as governance
    import kiro_crew.sandbox as sandbox
    import kiro_crew.sandbox_rlimit as rlimit
    from kiro_crew import hooks

    leftover = tmp_path / "sandbox-profile"
    leftover.write_text("x", encoding="utf-8")

    async def wrapped(argv: Any, **kw: Any) -> Any:
        return ([*argv, "--wrapped"] if wrap else list(argv)), kw.get("env"), str(leftover)

    monkeypatch.setattr(hooks.platform_compat, "IS_WINDOWS", True)  # the shell form
    monkeypatch.setattr(rlimit, "spawn_shim_argv", lambda _profile: [])  # the no-shim exec
    monkeypatch.setattr(sandbox, "sandboxed_spawn_argv_async", wrapped)
    monkeypatch.setattr("kiro_crew.hooks._script_hooks_capability_denied", lambda _sk: None)
    monkeypatch.setattr(governance, "event_refusal", lambda *_a: None)
    monkeypatch.setattr(governance, "hook_refusal", lambda *_a: None)
    return leftover


def _hook() -> Any:
    from kiro_crew import hooks
    from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT

    return hooks.ScriptHook(id="h", name="h", event=HOOK_EVENT_USER_PROMPT_SUBMIT, command="true")


@pytest.mark.parametrize("form", ["shell", "no-shim exec"])
def test_a_stop_while_the_build_waits_for_a_worker_builds_no_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, form: str
) -> None:
    """(a) and (b): the hook passed every ask on the loop; its build waits behind a
    busy worker; the Stop lands. The worker asks before ``Popen``: no child, the hook
    answers held, the sandbox's file is removed, and the ask is unset afterwards."""
    from kiro_crew import hooks

    leftover = _hook_env(monkeypatch, tmp_path, wrap=form != "shell")
    stopped = [False]

    async def body(loop: _OffLoop, pool: _Pool, release: threading.Event) -> Any:
        hook_ctx = contextvars.copy_context()  # the hook task's own context, read after
        run = loop.create_task(
            hooks.run_script_hook(_hook(), "ctx", admit=lambda: not stopped[0]),
            context=hook_ctx,
        )
        await await_until(pool.queued, what="the build queued behind the busy worker")
        stopped[0] = True  # the Stop, handled on the loop while the build waits
        release.set()
        result = await await_until(run, what="the hook's answer")
        return result, hook_ctx.get(offloop.spawn_admit)

    result, left_set = _run(monkeypatch, body)
    assert result.refused and result.exit_code == 2 and "Stop" in (result.error or "")
    assert not leftover.exists() and left_set is None


def test_the_ask_raising_on_the_worker_builds_no_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ask that raises on the worker thread is a refusal too: no child."""

    def broken() -> bool:
        raise RuntimeError("the ask failed")

    async def body(loop: _OffLoop, pool: _Pool, release: threading.Event) -> Any:
        release.set()
        with pytest.raises(RuntimeError, match="the ask failed"):
            await _direct_spawn(loop, broken)
        return loop.children.built

    assert _run(monkeypatch, body) == []


def test_a_cancelled_build_the_stop_held_builds_no_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c): the turn is cancelled while its build is queued, with the Stop set: the
    build refuses, no child, and the task sees CancelledError as today."""
    stopped = [False]

    async def body(loop: _OffLoop, pool: _Pool, release: threading.Event) -> Any:
        spawn = _direct_spawn(loop, lambda: not stopped[0])
        await await_until(pool.queued, what="the build queued")
        stopped[0] = True
        spawn.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await spawn
        return loop.children.built

    assert _run(monkeypatch, body) == []


def test_an_admitted_build_spawns_as_today_and_an_unset_ask_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) and (e): admitted, the child is built once; with no ask set, nothing is
    asked and the child is built as before."""
    asked: list[int] = []

    def admit() -> bool:
        asked.append(1)
        return True

    async def body(loop: _OffLoop, pool: _Pool, release: threading.Event) -> Any:
        release.set()
        admitted = await _direct_spawn(loop, admit)
        unset = await _direct_spawn(loop)
        return admitted, unset, loop.children.built

    admitted, unset, built = _run(monkeypatch, body)
    assert asked == [1], "asked once, for the admitted build only"
    assert admitted.child is not None and unset.child is not None and len(built) == 2


# ── the hooks' read-only ask (``stop_hold.held_now``) ────────────────────────────


_CASES = ["free", "record", "channel fact", "newer", "older", "other boot", "empty"]


@pytest.mark.parametrize("case", _CASES)
def test_held_now_answers_as_queues(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    slot = _ChatSlot("chat-now-1")
    press = next(stop_hold._presses)
    monkeypatch.setattr(stop_hold, "_last", press)
    if case == "channel fact":
        stop_hold._channels["C-now"] = (slot.key, press)
    elif case != "free":
        stop_hold._keys[slot.key] = press
    stamp = {
        "newer": stop_hold.stamp(press, True),
        "older": stop_hold.stamp(press - 1, True),
        "other boot": f"0123456789ab:{press}",
    }.get(case, "")
    now = stop_hold.held_now(slot, stamp)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(stop_hold, "_save_soon", lambda *_a: None)
        assert now == stop_hold.queues(slot, stamp), case
    assert now is (case not in ("free", "newer"))


def test_held_now_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    saved: list[Any] = []
    monkeypatch.setattr(stop_hold, "_save_soon", lambda *a: saved.append(a))
    slot = _ChatSlot("chat-now-2")
    stop_hold._channels["C-now"] = (slot.key, next(stop_hold._presses))
    before = (
        dict(stop_hold._keys),
        set(stop_hold._owed),
        dict(stop_hold._channels),
        slot._stop_held,
        slot._stop_hold_seq,
    )
    assert stop_hold.held_now(slot, "")
    after = (
        dict(stop_hold._keys),
        set(stop_hold._owed),
        dict(stop_hold._channels),
        slot._stop_held,
        slot._stop_hold_seq,
    )
    assert after == before and saved == []


@pytest.mark.asyncio
async def test_a_hook_held_by_the_channels_fact_alone_still_writes_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hooks' ask is read-only, so the loop's ask right after the hooks writes the
    key's record and mirror and saves them: here for a key the Stop held only by the
    channel's fact."""
    from test_slack_code_channel_stop_hold_write import _Hooks, _wrote_prompt
    from turn_harness import TurnScript, run_turn

    ctxs: list[Any] = []
    saved: list[Any] = []

    def stop() -> None:  # the channel's fact only, as a Stop that found no live slot
        monkeypatch.setattr(stop_hold, "_last", next(stop_hold._presses))
        stop_hold._channels["C-fact"] = (ctxs[0].slot.key, stop_hold._last)

    hooks = _Hooks(stop=stop, on="UserPromptSubmit")
    monkeypatch.setattr(stop_hold, "_save_soon", lambda _state, slot: saved.append(slot))

    def setup(ctx: Any) -> None:
        ctxs.append(ctx)
        ctx.state._hook_store = hooks
        stop_hold._state_seen = ctx.state

    record = await run_turn(TurnScript(events=[], setup=setup))
    slot = ctxs[0].slot
    assert hooks.fired[-1] == "UserPromptSubmit" and "UserPromptSubmit" not in hooks.spawned
    assert stop_hold._keys.get(slot.key) == stop_hold._last, "the key's record"
    assert slot._stop_held is True and slot._stop_hold_seq == stop_hold._last, "the mirror"
    published = stop_hold._published(ctxs[0].state, slot)
    assert published and saved == [slot], "the first-ask save"
    assert not _wrote_prompt(record)
