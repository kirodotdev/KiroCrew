"""Shared harness for ``test_acp_runtime.py`` and the modules split out of it.

The three autouse fixtures here are imported by every one of those modules, so
each keeps the same autouse set. The rest builds an ``AcpRuntime`` over a fake
subprocess, drives its reader loop, and waits on the frames it routes.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION, JsonRpcMessage
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse pin of test_acp_runtime.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A runtime start materialises the agent spec (``ensure_agent_materialized`` ->
    ``rebuild_agent_config``) and a worker install writes one
    (``_install_worker_agent`` -> ``_write_worker_spec``); both end in
    ``_write_derived_permissions``, which reads ``installed_kiro_cli_version``
    function-locally from ``kiro_crew.kiro_cli``: one real ``kiro-cli --version``
    spawn per binary identity, process-cached, so whichever test in the worker
    writes a spec first pays it against the HOST's install with the checkout as
    the child's cwd. Pinned to the floor release, as ``test_agent.py`` and the
    generated-writer suites pin it.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse ceiling of test_acp_runtime.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _fast_no_report_ceiling(monkeypatch):
    """Shrink drain_init()'s no-report ceiling for every test in this module.

    Many tests drive the real create_session()/load_session() path against a
    fake backend that never emits MCP registration frames; at the production
    ceiling each would stall drain_init() for seconds. drain_init() resolves
    the module constant at call time precisely so this patch takes effect.
    Tests that exercise the ceiling itself pass an explicit value instead.
    """
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)


def _spawn_client_mod():
    """The module that DEFINES the trusted-binary resolver every spawn uses.

    A harness resolves it there at call time, so a patch aimed at some other
    module's re-export would leave the real filesystem search running while the
    test believed it was stubbed.
    """
    import kiro_crew.acp.client as client_mod

    return client_mod


class _RunningLoopStreamReader(asyncio.StreamReader):
    """A ``StreamReader`` that binds its event loop to the RUNNING loop, resolved
    lazily on the first read that has to suspend rather than eagerly pinned at
    construction.

    ``asyncio.StreamReader.__init__`` resolves ``get_event_loop()`` immediately,
    pinning the reader to whatever loop was current then. A fake stdout built at
    synchronous fixture-setup time is thus pinned to the setup loop; under a
    ``pytest-asyncio`` whose test loop differs, the ``readuntil`` waiter --
    created from ``_loop`` -- would belong to the wrong loop and raise "got
    Future attached to a different loop". Deferring the bind to the first
    ``_wait_for_data`` (which only ever runs inside the test's running loop)
    picks up the right loop for any consumer of the reader, without reaching into
    the private ``_loop`` attribute from outside. ``feed_data`` is pure buffer
    work and never needs the loop, so eager feeding before the first read is fine.
    """

    def __init__(self, limit=asyncio.streams._DEFAULT_LIMIT):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop running at construction (the synchronous fixture-setup
            # case): initialise the base reader's own fields WITHOUT resolving a
            # loop (``StreamReader.__init__`` would call the deprecated
            # ``get_event_loop()``), and bind lazily on the first suspending read.
            if limit <= 0:
                raise ValueError("Limit cannot be <= 0")
            self._limit = limit
            self._loop = None
            self._buffer = bytearray()
            self._eof = False
            self._waiter = None
            self._exception = None
            self._transport = None
            self._paused = False
        else:
            super().__init__(limit=limit, loop=loop)

    async def _wait_for_data(self, func_name):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return await super()._wait_for_data(func_name)


def _make_runtime(reader: "asyncio.StreamReader | None" = None):
    """An initialized AcpRuntime wired to a fake subprocess.

    stdout is a real StreamReader we feed lines into; stdin is a mock that
    records writes; the reader loop can run against it without a real process.

    The default stdout is a ``_RunningLoopStreamReader`` so the reader binds its
    loop lazily, on the first suspending read, to the loop the test actually runs
    on -- not whatever loop was current when the runtime was built. That removes
    the need to rebind the private ``_loop`` attribute (as ``_start_reader`` once
    did) and makes a reader built from a synchronous fixture correct for any
    consumer. Callers may still pass an explicit ``reader``.
    """
    rt = AcpRuntime(work_dir="/tmp")
    if reader is None:
        reader = _RunningLoopStreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the fake process, moved verbatim; tracked for a de-flake pass
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    return rt, reader, proc


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


def _register(rt: AcpRuntime, *session_ids: str) -> dict[str, asyncio.Queue]:
    queues = {sid: asyncio.Queue() for sid in session_ids}
    rt._session_queues.update(queues)
    return queues


def _replying_send(queue_for, *, error: dict | None = None, req_id: int = 1) -> AsyncMock:
    """A ``send_request`` stand-in that answers each request the way an adapter does.

    The reply lands on the session's queue, which is where the handle's
    ``_wait_for_response`` reads it; ``queue_for(params)`` names that queue.
    """

    async def _send(method, params, **kwargs):
        on_reserved = kwargs.get("on_reserved")
        if on_reserved is not None:
            on_reserved(req_id)
        reply = (
            JsonRpcMessage(id=req_id, error=error)
            if error
            else JsonRpcMessage(id=req_id, result={})
        )
        queue_for(params).put_nowait(reply)
        return req_id

    return AsyncMock(side_effect=_send)


async def _start_reader(rt: AcpRuntime) -> asyncio.Task:
    # The fake stdout is a ``_RunningLoopStreamReader`` that binds its loop
    # lazily on its first suspending read, so its ``readuntil`` waiter is created
    # on the loop this coroutine runs on -- no cross-loop rebind needed.
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)  # let the loop reach its first readline
    return task


def _permission_msg(request_id: int) -> JsonRpcMessage:
    """A server→client permission REQUEST, the shape the answerer is given."""
    return JsonRpcMessage(
        id=request_id,
        method=METHOD_REQUEST_PERMISSION,
        params={"sessionId": "sA", "options": []},
    )


async def _stop_reader(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _bounded(coro, *, what: str):
    """Await a coroutine the code under test should finish, under the lost-run bound.

    The replacement for ``asyncio.wait_for(coro, <short N>)`` in this file (rule
    D1): the coroutine runs as its own task and ``await_until`` awaits that task
    for half the test's timeout, so a hang fails by name instead of a bet on the
    runner's speed deciding pass versus fail. Past the bound the task is
    cancelled and settled before the error propagates, so a teardown that closes
    the generator or stops the reader does not meet a step still running.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await await_until(task, what=what)
    except AssertionError:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


async def _next(queue: asyncio.Queue, *, what: str = "the next frame on the session queue"):
    """The next item on ``queue``: the frame the demux routes IS the signal."""
    return await _bounded(queue.get(), what=what)


async def _await_routed(
    rt: AcpRuntime, *session_ids: str, timeout: float | None = None
) -> dict[str, int]:
    """Wait until the runtime has an in-flight request for each session, and
    return the ``{session_id: request_id}`` map.

    This replaces the ``await asyncio.sleep(0.05); req_id = rt._next_id - 1``
    idiom, which was wrong in two independent ways.

    The **timing** problem: 50ms is a guess at how long a driver task takes to
    reach ``send_request``. It holds on an idle machine and fails on a loaded
    Windows CI runner, where the driver may not have run yet. The test then reads
    an id belonging to no request, feeds a response nothing is waiting for, and
    fails much later as an opaque ``TimeoutError`` in ``wait_for`` rather than at
    the line that guessed wrong.

    The **correctness** problem: ``_next_id - 1`` assumes the most recently
    allocated id belongs to *this* prompt. That is only true when nothing else
    allocated an id in between, which no test actually enforces.
    ``_routed_requests`` maps request id to session id, so looking a session up
    there is exact regardless of what else is in flight.

    Waiting on ``_routed_requests`` is the right signal: ``send_request``
    populates it in the same synchronous block that allocates the id
    (``runtime.py``), so the entry is visible as soon as the request exists.
    """
    wanted = set(session_ids)

    def routed_now() -> dict[str, int]:
        return {sid: rid for rid, sid in rt._routed_requests.items()}

    def every_session_routed() -> bool:
        return wanted <= set(rt._routed_requests.values())

    # ``async_wait_until`` re-reads between loop turns, so the driver task gets
    # the loop; ``timeout`` is the lost-run bound (half the test's by default) and
    # the failure quotes the sessions waited for and what was routed.
    await async_wait_until(
        every_session_routed,
        timeout=timeout,
        describe=lambda: (
            f"waiting for in-flight requests for {sorted(wanted)}; "
            f"currently routed: {routed_now()}"
        ),
    )
    return routed_now()


async def _await_pending(
    rt: AcpRuntime, *, exclude: set[int] | None = None, timeout: float | None = None
) -> int:
    """Wait for an in-flight control-plane request and return its id.

    The ``_pending_requests`` counterpart to :func:`_await_routed`, and it exists
    for the same reason. It replaces the
    ``await asyncio.sleep(0); next(iter(rt._pending_requests))`` idiom, which
    assumes one loop iteration is enough for the caller to reach
    ``_send_and_await``. ``create_session`` first awaits ``asyncio.to_thread`` to
    resolve the MCP-gateway overlay off the loop, so a single yield leaves
    ``_pending_requests`` empty — and ``next()`` on an empty iterator raises
    ``StopIteration``, which PEP 479 converts into
    ``RuntimeError("coroutine raised StopIteration")`` on its way out of a
    coroutine. That names neither the stale assumption nor the line that made it.

    ``_send_and_await`` registers the future in the same synchronous block that
    allocates the id (``runtime.py``), so the entry is visible as soon as the
    request exists. ``exclude`` drops ids the caller already consumed, so a test
    driving a second request cannot pick up a leftover entry from the first.
    """
    seen = exclude or set()

    def fresh_requests() -> list[int]:
        return [rid for rid in rt._pending_requests if rid not in seen]

    # Re-read between loop turns so the caller progresses; ``timeout`` is the
    # lost-run bound and the failure quotes what was pending.
    await async_wait_until(
        fresh_requests,
        timeout=timeout,
        describe=lambda: (
            "waiting for an in-flight request; "
            f"currently pending: {sorted(rt._pending_requests)}"
        ),
    )
    fresh = fresh_requests()
    # These tests keep exactly one control-plane request in flight, so more
    # than one means the id being returned is a coin flip.
    assert len(fresh) == 1, f"expected one in-flight request, got {fresh}"
    return fresh[0]


async def _drain(reader: asyncio.StreamReader, timeout: float = 5.0) -> None:
    """Wait until the reader loop has consumed everything fed, or fail loudly.

    A fixed ``asyncio.sleep(0.05)`` encodes an assumption about scheduler
    latency that a loaded CI runner breaks -- it is why this suite's Windows
    shard failed while its siblings passed. Waiting on the observable condition
    (stdout buffer drained, then a bounded number of turns for the handler that
    follows ``readline``) is deterministic under load and faster locally.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while reader._buffer:
        if loop.time() >= deadline:
            raise AssertionError("reader loop did not consume the fed frames in time")
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0)


def _last_written_frame(proc) -> dict:
    """The most recent JSON frame written to the fake process stdin."""
    assert proc.stdin.write.call_args is not None, "nothing was written to stdin"
    raw = proc.stdin.write.call_args[0][0]
    return json.loads(raw.decode())


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the unroutable-permission tests' SEL stub, moved verbatim; no module split out of test_acp_runtime.py calls monkeypatch.undo()
def _stub_sel_for_permission_tests(request, monkeypatch):
    """Stub the SEL for the auto-reject tests in this section.

    The production path fires the audit on a background ``asyncio.to_thread``
    task; letting it hit the real SEL from unit tests is slow (first-use
    filesystem setup) and races the test's event-loop teardown ("Event loop is
    closed" noise on loaded CI shards). Scoped by test-name prefix so the rest
    of the module keeps its behavior.
    """
    if not request.node.name.startswith(
        ("test_unroutable", "test_registered_session_permission", "test_ambiguous")
    ):
        yield
        return
    import kiro_crew.sel as sel_mod

    class _StubSel:
        def log_tool_invocation(self, **kwargs):  # noqa: D401 - stub
            return None

    monkeypatch.setattr(sel_mod, "sel", lambda: _StubSel())
    yield


async def _drain_audits(rt) -> None:
    """Await in-flight audit tasks so none outlives the test's event loop."""
    if rt._audit_tasks:
        await asyncio.gather(*list(rt._audit_tasks), return_exceptions=True)
