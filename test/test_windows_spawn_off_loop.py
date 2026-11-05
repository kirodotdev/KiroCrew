"""Process creation runs off the event loop (``platform_compat_offloop_spawn``).

A host whose endpoint protection holds ``CreateProcess`` for 15-20 s held the
gateway loop for that long, because CPython's Proactor transport builds its child
before its first await, and the 25 s loop-stall watchdog killed the gateway.

The POSIX tests drive the shared helper with the interpreter's own Unix subprocess
transport and a ``Popen`` factory the test holds closed, so a slow spawn is
simulated without a sleep: the loop must keep running while the factory is held,
and a cancellation while it is held must still reap the child it builds. The
Windows tests drive the real Proactor path with ``_winapi.CreateProcess`` held the
same way. The structural tests pin that the gateway and MCP daemon loops use the
off-loop factory and that no new code builds a subprocess transport or a Proactor
loop of its own.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew.platform_compat_native import IS_WINDOWS
from kiro_crew.testing.wait import await_until, finished

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: How long a held factory waits before giving up. A lost-run bound only: the
#: tests release the factory themselves, and a factory still held at this bound
#: means the loop never got to release it, which fails the test by name.
_HOLD_BOUND_SECS = 10.0

#: Loop turns the test takes while the spawn is held. Any number above zero
#: proves the loop was free; a few dozen makes the claim visible in a failure.
_TICKS = 50


class _HeldFactory:
    """A ``Popen`` factory that blocks its worker until the test releases it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, popen: Any = subprocess.Popen) -> None:
        self._loop = loop
        self._popen = popen
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.threads: list[int] = []
        self.built: list[Any] = []

    def __call__(self, args: Any, **kwargs: Any) -> Any:
        self.threads.append(threading.get_ident())
        self._loop.call_soon_threadsafe(self.entered.set)
        if not self.release.wait(_HOLD_BOUND_SECS):
            raise AssertionError("the spawn was never released: the loop was held")
        popen = self._popen(args, **kwargs)
        self.built.append(popen)
        return popen


def _watch_exit(loop: asyncio.AbstractEventLoop, transport: Any) -> None:
    """Report the child's exit to *transport*, as a child watcher would."""
    popen = transport.get_extra_info("subprocess")

    def wait() -> None:
        popen.wait()
        loop.call_soon_threadsafe(transport._process_exited, popen.returncode)

    threading.Thread(target=wait, daemon=True).start()


class _PosixOffLoopSpawnLoop(asyncio.SelectorEventLoop):
    """The POSIX twin of ``OffLoopSpawnProactorEventLoop``, for these tests only."""

    factory: _HeldFactory

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
        # Imported here: asyncio.unix_events raises ImportError on Windows, and
        # this module must still collect there for its Windows tests.
        from asyncio import unix_events

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
            popen_factory=self.factory,
            transport_cls=unix_events._UnixSubprocessTransport,
            popen_global="subprocess",
            after_init=functools.partial(_watch_exit, self),
            **kwargs,
        )


def _run_posix(body: Any) -> Any:
    with asyncio.Runner(loop_factory=_PosixOffLoopSpawnLoop) as runner:
        loop = runner.get_loop()
        assert isinstance(loop, _PosixOffLoopSpawnLoop)
        loop.factory = _HeldFactory(loop)
        return runner.run(body(loop.factory))


posix_only = pytest.mark.skipif(IS_WINDOWS, reason="drives the Unix subprocess transport")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="drives the Proactor loop")


async def _ticks_while_pending(task: asyncio.Task[Any]) -> int:
    ticks = 0
    for _ in range(_TICKS):
        await asyncio.sleep(0)
        if task.done():
            break
        ticks += 1
    return ticks


@posix_only
def test_loop_keeps_running_while_the_child_is_built() -> None:
    async def body(factory: _HeldFactory) -> None:
        loop_thread = threading.get_ident()
        spawn = asyncio.ensure_future(
            asyncio.create_subprocess_exec(
                sys.executable, "-c", "print('hi')", stdout=asyncio.subprocess.PIPE
            )
        )
        await await_until(factory.entered, what="the factory entered")
        assert await _ticks_while_pending(spawn) == _TICKS
        assert not spawn.done()
        factory.release.set()
        process = await finished(spawn, "the spawn")
        out, _ = await finished(process.communicate(), "the child's output")
        assert out.strip() == b"hi"
        assert process.returncode == 0
        assert factory.threads and loop_thread not in factory.threads

    _run_posix(body)


@posix_only
def test_cancel_during_the_build_reaps_the_child_it_builds() -> None:
    async def body(factory: _HeldFactory) -> None:
        spawn = asyncio.ensure_future(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import sys; sys.stdin.read()",
                stdin=asyncio.subprocess.PIPE,
            )
        )
        await await_until(factory.entered, what="the factory entered")
        spawn.cancel()
        # The cancel cannot stop the worker, so the spawn waits for it rather
        # than abandoning a child nobody would own.
        await _ticks_while_pending(spawn)
        assert not spawn.done()
        factory.release.set()
        with pytest.raises(asyncio.CancelledError):
            await finished(spawn, "the cancelled spawn")
        assert len(factory.built) == 1
        child = factory.built[0]
        # Reaped, not merely signalled: the transport waited for the exit.
        assert child.returncode is not None
        assert child.returncode != 0

    _run_posix(body)


@posix_only
def test_a_build_that_fails_after_a_cancel_still_raises_the_cancel() -> None:
    """A caller catching ``OSError`` must not carry on after it was cancelled."""

    async def body(factory: _HeldFactory) -> None:
        spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(str(_SRC / "no-such-program")))
        await await_until(factory.entered, what="the factory entered")
        spawn.cancel()
        await _ticks_while_pending(spawn)
        assert not spawn.done()
        factory.release.set()
        with pytest.raises((asyncio.CancelledError, FileNotFoundError)) as raised:
            await finished(spawn, "the cancelled spawn")
        assert raised.type is asyncio.CancelledError, raised.value
        assert isinstance(raised.value.__cause__, FileNotFoundError)
        assert factory.built == []

    _run_posix(body)


@posix_only
def test_a_failed_build_raises_its_own_error() -> None:
    async def body(factory: _HeldFactory) -> None:
        factory.release.set()
        with pytest.raises(FileNotFoundError):
            await finished(
                asyncio.create_subprocess_exec(str(_SRC / "no-such-program")), "the spawn"
            )
        assert factory.built == []

    _run_posix(body)


@posix_only
def test_a_failed_build_leaves_nothing_holding_the_half_built_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's future must not keep a failed build's ``Popen`` alive.

    A pool worker holds its last future until it takes another item, and on a busy
    host that is after the caller has already handled the failure. Windows tree
    cleanup asserts the failed ``Popen``/transport graph is collectable then, so
    this executor keeps every future it hands out, as such a worker does.
    """
    import gc
    import weakref
    from concurrent.futures import ThreadPoolExecutor

    kept: list[Any] = []

    class _Retaining(ThreadPoolExecutor):
        def submit(self, *args: Any, **kwargs: Any) -> Any:
            future = super().submit(*args, **kwargs)
            kept.append(future)
            return future

    pool = _Retaining(max_workers=1)
    monkeypatch.setattr(offloop, "spawn_executor", lambda: pool)
    built: list[Any] = []

    class _HalfBuilt:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            built.append(weakref.ref(self))
            raise OSError("fixture failure after the child was created")

    async def body() -> None:
        from asyncio import unix_events

        with pytest.raises(OSError, match="fixture failure"):
            await offloop.make_subprocess_transport_off_loop(
                asyncio.get_running_loop(),
                None,
                ["x"],
                False,
                None,
                None,
                None,
                0,
                popen_factory=_HalfBuilt,
                transport_cls=unix_events._UnixSubprocessTransport,
                popen_global="subprocess",
            )

    try:
        asyncio.run(body())
        gc.collect()
        assert len(kept) == 1 and kept[0].done()
        assert len(built) == 1
        assert built[0]() is None, "the worker's future kept the half-built child alive"
    finally:
        pool.shutdown(wait=True)


def test_off_loop_spawn_refuses_a_transport_that_spawns_elsewhere() -> None:
    class _Elsewhere:
        def _start(self, *args: Any, **kwargs: Any) -> None:
            self._proc = None

    async def body() -> None:
        with pytest.raises(RuntimeError, match="off-loop spawn refused"):
            await offloop.make_subprocess_transport_off_loop(
                asyncio.get_running_loop(),
                None,
                ["x"],
                False,
                None,
                None,
                None,
                0,
                popen_factory=subprocess.Popen,
                transport_cls=_Elsewhere,
            )

    asyncio.run(body())


def test_gateway_loop_factory_is_the_off_loop_proactor_on_windows_only() -> None:
    factory = offloop.gateway_loop_factory()
    if IS_WINDOWS:
        assert factory is offloop.OffLoopSpawnProactorEventLoop
    else:
        assert factory is None


# ── Windows: the real Proactor path ──


def _held_create_process(monkeypatch: pytest.MonkeyPatch, loop: Any) -> _HeldFactory:
    import _winapi  # type: ignore[import-not-found]

    native = _winapi.CreateProcess
    held = _HeldFactory(loop, popen=None)

    def create_process(*args: Any, **kwargs: Any) -> Any:
        held.threads.append(threading.get_ident())
        loop.call_soon_threadsafe(held.entered.set)
        if not held.release.wait(_HOLD_BOUND_SECS):
            raise AssertionError("CreateProcess was never released: the loop was held")
        result = native(*args, **kwargs)
        held.built.append(result[2])  # the child's pid
        return result

    monkeypatch.setattr(_winapi, "CreateProcess", create_process)
    return held


def _run_windows(body: Any, monkeypatch: pytest.MonkeyPatch, loop_factory: Any = None) -> Any:
    factory = loop_factory or offloop.gateway_loop_factory()
    with asyncio.Runner(loop_factory=factory) as runner:
        held = _held_create_process(monkeypatch, runner.get_loop())
        return runner.run(body(held))


@windows_only
@pytest.mark.parametrize("shell", [False, True], ids=["exec", "shell"])
def test_windows_loop_keeps_running_inside_create_process(
    monkeypatch: pytest.MonkeyPatch, shell: bool
) -> None:
    async def body(held: _HeldFactory) -> None:
        loop_thread = threading.get_ident()
        if shell:
            # The script-hook arm: create_subprocess_shell outside the chokepoint.
            spawn = asyncio.ensure_future(
                asyncio.create_subprocess_shell("echo hi", stdout=asyncio.subprocess.PIPE)
            )
        else:
            spawn = asyncio.ensure_future(
                asyncio.create_subprocess_exec(
                    sys.executable, "-c", "print('hi')", stdout=asyncio.subprocess.PIPE
                )
            )
        await await_until(held.entered, what="CreateProcess entered")
        assert await _ticks_while_pending(spawn) == _TICKS
        held.release.set()
        process = await finished(spawn, "the spawn")
        out, _ = await finished(process.communicate(), "the child's output")
        assert out.strip() == b"hi"
        assert held.threads and loop_thread not in held.threads

    _run_windows(body, monkeypatch)


@windows_only
def test_windows_cancel_inside_create_process_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import platform_compat

    async def body(held: _HeldFactory) -> None:
        spawn = asyncio.ensure_future(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import sys; sys.stdin.read()",
                stdin=asyncio.subprocess.PIPE,
            )
        )
        await await_until(held.entered, what="CreateProcess entered")
        spawn.cancel()
        await _ticks_while_pending(spawn)
        assert not spawn.done()
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            await finished(spawn, "the cancelled spawn")
        # The transport waited for the exit before re-raising, so the child is gone.
        assert len(held.built) == 1
        assert not platform_compat.pid_exists(held.built[0])

    _run_windows(body, monkeypatch)


@windows_only
def test_windows_owned_session_spawn_runs_create_process_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import platform_compat
    from kiro_crew.sandbox_rlimit import RLIMIT_PROFILE_SESSION_HOST, create_subprocess_limited

    async def body(held: _HeldFactory) -> None:
        loop_thread = threading.get_ident()
        spawn = asyncio.ensure_future(
            platform_compat.create_windows_cleanup_owned_process(
                functools.partial(
                    create_subprocess_limited,
                    "cmd.exe",
                    "/c",
                    "exit 0",
                    profile=RLIMIT_PROFILE_SESSION_HOST,
                    creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                )
            )
        )
        await await_until(held.entered, what="CreateProcess entered")
        assert await _ticks_while_pending(spawn) == _TICKS
        held.release.set()
        process = await finished(spawn, "the owned spawn")
        assert await finished(process.wait(), "the child's exit") == 0
        assert held.threads and loop_thread not in held.threads
        await finished(platform_compat.terminate_windows_asyncio_tree(process), "the drain")

    # The owned path offloads on any Proactor loop, not only the gateway's.
    _run_windows(body, monkeypatch, loop_factory=asyncio.ProactorEventLoop)  # type: ignore[attr-defined]


# ── Structure: no new on-loop spawn path ──

#: Modules allowed to name the interpreter's private subprocess-transport seam.
#: Anything else building a transport, or overriding how one is made, would be a
#: spawn that can run ``CreateProcess`` on the loop.
_TRANSPORT_SEAM_OWNERS = {
    "platform_compat_offloop_spawn.py",
    "platform_compat_wintree.py",
}
_TRANSPORT_SEAM_NAMES = {"_make_subprocess_transport", "_WindowsSubprocessTransport"}

#: Modules allowed to construct or subclass a Proactor loop. A new one would run
#: its subprocess transports with the child built on the loop.
_PROACTOR_OWNERS = {"platform_compat_offloop_spawn.py"}
_PROACTOR_NAMES = {"ProactorEventLoop", "WindowsProactorEventLoopPolicy"}


@functools.cache
def _parsed_sources() -> tuple[tuple[str, ast.Module], ...]:
    """Product modules that mention a seam name, parsed once for both scans.

    A module whose text names none of them cannot reference one in its AST, so
    only the few that do are parsed; parsing every module costs most of a minute.
    """
    needles = _TRANSPORT_SEAM_NAMES | _PROACTOR_NAMES
    parsed = []
    for path in sorted(_SRC.rglob("*.py")):
        if "/tests/" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in needles):
            parsed.append((path.name, ast.parse(text)))
    return tuple(parsed)


def _named(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def test_only_the_offload_owners_touch_the_subprocess_transport_seam() -> None:
    found: dict[str, set[str]] = {}
    for name_of_file, tree in _parsed_sources():
        for node in ast.walk(tree):
            name = _named(node)
            if name in _TRANSPORT_SEAM_NAMES:
                found.setdefault(name_of_file, set()).add(name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in _TRANSPORT_SEAM_NAMES:
                    found.setdefault(name_of_file, set()).add(node.name)
    assert set(found) <= _TRANSPORT_SEAM_OWNERS, found


def test_only_the_offload_module_builds_a_proactor_loop() -> None:
    found: set[str] = set()
    for name_of_file, tree in _parsed_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _named(node.func) in _PROACTOR_NAMES:
                found.add(name_of_file)
            if isinstance(node, ast.ClassDef) and any(
                _named(base) in _PROACTOR_NAMES for base in node.bases
            ):
                found.add(name_of_file)
    assert found <= _PROACTOR_OWNERS, found


def _calls_named(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _named(n.func) == name]


@pytest.mark.parametrize(
    ("relpath", "coro"),
    [("cli.py", "_gateway"), ("mcp_gateway/gatewayd.py", "_amain")],
    ids=["gateway", "mcp-daemon"],
)
def test_long_lived_loops_use_the_off_loop_factory(relpath: str, coro: str) -> None:
    runs = [
        call
        for call in _calls_named(_SRC / relpath, "run")
        if call.args and isinstance(call.args[0], ast.Call) and _named(call.args[0].func) == coro
    ]
    assert len(runs) == 1, f"{relpath}: expected one asyncio.run({coro}(...))"
    factories = [kw.value for kw in runs[0].keywords if kw.arg == "loop_factory"]
    assert len(factories) == 1, f"{relpath}: asyncio.run({coro}(...)) has no loop_factory"
    value = factories[0]
    assert isinstance(value, ast.Call) and _named(value.func) == "gateway_loop_factory"


def test_the_owned_session_spawn_builds_its_child_off_the_loop() -> None:
    names = {
        _named(node)
        for node in ast.walk(ast.parse((_SRC / "platform_compat_wintree.py").read_text("utf-8")))
    }
    assert "make_subprocess_transport_off_loop" in names
