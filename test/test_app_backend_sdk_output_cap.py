"""``max_output_bytes`` on the backend SDK's ``run_sandboxed_async`` (seam level 17).

What is pinned is what an app cannot check for itself: the cap only lowers what the
run returns (``None`` keeps the uncapped read, and any other value that is not a
positive int is refused before anything is prepared); a child past the cap is stopped
with at most the cap kept of each pipe; no exit of a capped run (past the cap, a
timeout, a cancel) reads a pipe without a bound, so a descendant that survives the kill
and keeps writing cannot grow this process; both pipes and the child's input are
pumped at once, so a child that fills one pipe cannot wedge the read; and the keyword
and its exception are published at level 17.

The cap logic is driven through in-process fake pipes. One real interpreter child, in
the last test, proves the cap stops a child that never stops writing.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tracemalloc

import pytest

from kiro_crew import platform_compat, sandbox
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps.backend_sdk import spawn as sdk_spawn
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: One read of a capped drain.
_CHUNK = sdk_spawn._PIPE_CHUNK_BYTES
_CAP = 1024
#: What a descendant that survived the kill writes to each pipe afterwards.
_SURVIVOR_BYTES = 32 * 1024 * 1024
#: Allocation the event loop's own bookkeeping (tasks, futures, events) may add.
_LOOP_HEADROOM = 1024 * 1024


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=FALLBACK_TIMEOUT_SECS))


class _Rebound:
    """A stand-in for a module, bound in the module under test alone (D11).

    The *overrides* answer, and every other attribute is the real module's, so a fake
    spawn or kill never reaches other code in the worker that shares the real module.
    """

    def __init__(self, real: object, **overrides: object) -> None:
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class _Pipe:
    """One pipe of a fake child: *head* while the child lives, then EOF.

    With *hold*, a read past *head* waits for the kill instead, as a live child with
    nothing more to say does. Once the child is killed, *tail* more bytes arrive from a
    descendant that survived the kill, then EOF. The size of every read is recorded,
    so a test sees an unbounded one (``read()``, which is what ``communicate()`` does).
    """

    def __init__(self, child: _Child, head: bytes, tail: int, hold: bool) -> None:
        self._child = child
        self._head = head
        self._tail = tail
        self._hold = hold
        self.sizes: list[int] = []

    @property
    def tail_left(self) -> int:
        return self._tail

    async def read(self, n: int = -1) -> bytes:
        self.sizes.append(n)
        if self._head:
            size = len(self._head) if n < 0 else n
            chunk, self._head = self._head[:size], self._head[size:]
            return chunk
        if not self._child.killed.is_set():
            if not self._hold:
                return b""
            self._child.parked.set()
            await self._child.killed.wait()
        size = self._tail if n < 0 else min(n, self._tail)
        self._tail -= size
        return b"s" * size


class _Child:
    """A fake child process whose pipes are :class:`_Pipe` objects."""

    pid = None

    def __init__(
        self,
        *,
        out: bytes = b"",
        err: bytes = b"",
        tail: int = 0,
        hold: bool = False,
        parked: asyncio.Event | None = None,
        stdin: object = None,
    ) -> None:
        self.returncode: int | None = None
        self.killed = asyncio.Event()
        self.parked = parked if parked is not None else asyncio.Event()
        self.stdin = stdin
        self.stdout = _Pipe(self, out, tail, hold)
        self.stderr = _Pipe(self, err, tail, hold)
        self.communicated = False

    def kill(self) -> None:
        self.killed.set()

    async def communicate(self, input=None):
        self.communicated = True
        return await self.stdout.read(), await self.stderr.read()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed.is_set() else 0
        return self.returncode


@pytest.fixture()
def fake_child(monkeypatch, tmp_path):
    """Patch the preparation, the spawn and the kills; return the recorded state.

    ``state["spec"]`` holds the keywords the next spawned :class:`_Child` is built with,
    or a ready-made child under ``state["child"]``.
    """
    state: dict = {"prepared": [], "reaped": [], "tree_kills": [], "procs": [], "spec": {}}
    cleanup = tmp_path / "launcher.sh"
    cleanup.write_text("x", encoding="utf-8")
    state["cleanup"] = cleanup

    async def fake_prepare(argv, mode, *, env, strip_python_env):
        state["prepared"].append(list(argv))
        return list(argv), {}, str(cleanup)

    async def fake_spawn(*argv, **kwargs):
        proc = state.get("child") or _Child(**state["spec"])
        state["procs"].append(proc)
        return proc

    async def fake_kill_and_reap(proc, *, timeout=None):
        state["reaped"].append(proc)

    async def fake_tree_kill(pid, sig):
        state["tree_kills"].append(pid)

    monkeypatch.setattr(
        sdk_spawn,
        "sandbox",
        _Rebound(
            sandbox, sandboxed_spawn_argv_async=fake_prepare, create_subprocess_limited=fake_spawn
        ),
    )
    monkeypatch.setattr(
        sdk_spawn,
        "platform_compat",
        _Rebound(
            platform_compat,
            kill_and_reap=fake_kill_and_reap,
            kill_process_tree_async=fake_tree_kill,
            # A fake child has no real pid, so it is never in this process's own group.
            _shares_own_process_group=lambda _pid: False,
        ),
    )
    return state


def test_the_keyword_and_its_exception_are_published_at_level_seventeen() -> None:
    assert "max_output_bytes" in inspect.signature(sdk.run_sandboxed_async).parameters
    assert inspect.signature(sdk.run_sandboxed_async).parameters["max_output_bytes"].default is None
    assert sdk._KEYWORD_LEVELS["max_output_bytes"] == 17
    assert "max_output_bytes" not in sdk.__all__
    assert sdk._EXPORT_LEVELS["OutputLimitExceeded"] == 17
    assert issubclass(sdk.OutputLimitExceeded, subprocess.SubprocessError)
    assert sdk.BACKEND_SDK_LEVEL < 17 <= sdk.SDK_LEVEL


@pytest.mark.parametrize("cap", [0, -1, True, False, 1.5, "4096"])
def test_a_cap_that_is_not_a_positive_int_is_refused_before_preparing(fake_child, cap) -> None:
    with pytest.raises(ValueError):
        _run(sdk.run_sandboxed_async(["x"], max_output_bytes=cap))
    assert fake_child["prepared"] == []


def test_no_cap_keeps_the_uncapped_read(fake_child) -> None:
    fake_child["spec"] = {"out": b"o" * 200_000}
    done = _run(sdk.run_sandboxed_async(["x"]))
    assert done.stdout == b"o" * 200_000
    assert fake_child["procs"][0].communicated is True
    assert fake_child["reaped"] == []


def test_no_cap_still_stops_through_kill_and_reap(fake_child) -> None:
    # The uncapped run's cleanup is unchanged: a timeout reaps through kill_and_reap.
    fake_child["spec"] = {"hold": True}
    with pytest.raises(subprocess.TimeoutExpired):
        _run(sdk.run_sandboxed_async(["x"], timeout=0))
    assert fake_child["reaped"] == fake_child["procs"]


def test_output_at_the_cap_is_returned_whole(fake_child) -> None:
    fake_child["spec"] = {"out": b"o" * _CAP, "err": b"e" * _CAP}
    done = _run(sdk.run_sandboxed_async(["x"], max_output_bytes=_CAP))
    assert (done.stdout, done.stderr) == (b"o" * _CAP, b"e" * _CAP)
    (child,) = fake_child["procs"]
    assert child.communicated is False
    assert not child.killed.is_set()
    assert not fake_child["cleanup"].exists()


@pytest.mark.parametrize("pipe", ["out", "err"])
def test_a_child_past_the_cap_is_stopped_and_keeps_at_most_the_cap(fake_child, pipe) -> None:
    fake_child["spec"] = {pipe: b"z" * 5000, "hold": True}
    with pytest.raises(sdk.OutputLimitExceeded) as caught:
        _run(sdk.run_sandboxed_async(["secret-argument"], max_output_bytes=1000))
    exc = caught.value
    assert exc.limit == 1000
    assert getattr(exc, "stdout" if pipe == "out" else "stderr") == b"z" * 1000
    assert len(exc.stdout) <= 1000 and len(exc.stderr) <= 1000
    assert exc.cmd == ["secret-argument"]
    assert "secret-argument" not in str(exc)
    (child,) = fake_child["procs"]
    assert child.killed.is_set()
    assert child.returncode is not None, "the stopped child was not reaped"
    assert fake_child["tree_kills"] == [None]
    assert fake_child["reaped"] == [], "a capped run reaped through communicate()"
    assert not fake_child["cleanup"].exists()


async def _traced(scenario):
    """Await *scenario()* under the fallback bound; return what it raised and its peak
    traced allocation."""
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        raised: BaseException | None = None
        try:
            await asyncio.wait_for(scenario(), timeout=FALLBACK_TIMEOUT_SECS)
        except BaseException as exc:  # the exit path under test ends in a raise
            raised = exc
        return raised, tracemalloc.get_traced_memory()[1] - base
    finally:
        if started:
            tracemalloc.stop()


@pytest.mark.parametrize(
    ("exit_path", "expected"),
    [
        ("overflow", sdk.OutputLimitExceeded),
        ("timeout", subprocess.TimeoutExpired),
        ("cancel", asyncio.CancelledError),
    ],
)
def test_a_survivor_of_the_kill_leaves_memory_bounded_by_the_cap(
    fake_child, exit_path, expected
) -> None:
    # A descendant that setsid()-ed out of the child's group survives the kill holding
    # both pipes and writes 32 MiB more to each. The cleanup drains that in bounded
    # reads and keeps none of it, so the run peaks near the cap plus one chunk; a reap
    # through communicate() would hold all 64 MiB.
    async def scenario():
        parked = asyncio.Event()
        fake_child["spec"] = {
            "out": b"z" * _CHUNK if exit_path == "overflow" else b"",
            "tail": _SURVIVOR_BYTES,
            "hold": True,
            "parked": parked,
        }
        # Literal 0 is the already-expired deadline: the timeout path without a wait.
        expired = {"timeout": 0} if exit_path == "timeout" else {}
        run = sdk.run_sandboxed_async(["x"], max_output_bytes=_CAP, **expired)
        if exit_path != "cancel":
            await run
            return
        task = asyncio.ensure_future(run)
        await asyncio.wait_for(parked.wait(), timeout=FALLBACK_TIMEOUT_SECS)
        task.cancel()
        await asyncio.wait_for(task, timeout=FALLBACK_TIMEOUT_SECS)

    raised, peak = asyncio.run(_traced(scenario))
    assert isinstance(raised, expected), raised
    (child,) = fake_child["procs"]
    assert child.communicated is False
    assert fake_child["reaped"] == [], "a capped run reaped through communicate()"
    for pipe in (child.stdout, child.stderr):
        assert pipe.sizes and all(0 < size <= _CHUNK for size in pipe.sizes), pipe.sizes
        assert pipe.tail_left == 0, "the survivor's output was not drained"
    assert child.returncode is not None, "the stopped child was not reaped"
    assert peak < _CAP + _CHUNK + _LOOP_HEADROOM, peak


class _Stdin:
    def __init__(self, closed: asyncio.Event) -> None:
        self.data = bytearray()
        self._closed = closed

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed.set()


class _GatedPipe:
    """Yields *payload* in reads of the size asked, only once every gate is set."""

    def __init__(self, payload: bytes, gates: list, on_first_read: asyncio.Event | None) -> None:
        self._payload = payload
        self._gates = gates
        self._on_first_read = on_first_read

    async def read(self, n: int = -1) -> bytes:
        if self._on_first_read is not None:
            self._on_first_read.set()
        for gate in self._gates:
            await gate.wait()
        chunk, self._payload = self._payload[:n], self._payload[n:]
        return chunk


def test_input_is_fed_while_both_pipes_are_drained(fake_child) -> None:
    # stdout answers only once stderr is being read and the input is closed, and stderr
    # only once the input is closed: a read that wrote the input first, or drained one
    # pipe before starting the other, never gets its answer.
    payload = b"p" * 300_000

    async def scenario():
        input_closed = asyncio.Event()
        stderr_read = asyncio.Event()
        child = _Child(stdin=_Stdin(input_closed))
        child.stdout = _GatedPipe(payload, [stderr_read, input_closed], None)
        child.stderr = _GatedPipe(payload, [input_closed], stderr_read)
        fake_child["child"] = child
        done = await sdk.run_sandboxed_async(["x"], input=payload, max_output_bytes=400_000)
        return done, bytes(child.stdin.data)

    done, fed = _run(scenario())
    assert done.returncode == 0
    assert done.stdout == payload and done.stderr == payload
    assert fed == payload


# ── a real child ────────────────────────────────────────────────────────────


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="process groups are POSIX")
def test_a_child_that_never_stops_writing_is_stopped_by_the_cap(monkeypatch) -> None:
    # The one real interpreter child in this module. It writes until it is killed, so
    # only the cap can end the run before the outer deadline.
    procs: list = []
    real_spawn = sandbox.create_subprocess_limited

    async def identity_prepare(argv, mode, *, env, strip_python_env):
        return list(argv), {"PATH": os.environ.get("PATH", "")}, None

    async def recording_spawn(*argv, **kwargs):
        proc = await real_spawn(*argv, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(
        sdk_spawn,
        "sandbox",
        _Rebound(
            sandbox,
            sandboxed_spawn_argv_async=identity_prepare,
            create_subprocess_limited=recording_spawn,
        ),
    )
    flood = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n"
    with pytest.raises(sdk.OutputLimitExceeded) as caught:
        _run(sdk.run_sandboxed_async([sys.executable, "-c", flood], max_output_bytes=100_000))
    assert caught.value.stdout == b"x" * 100_000
    (proc,) = procs
    assert proc.returncode is not None, "the flooding child was left running"
