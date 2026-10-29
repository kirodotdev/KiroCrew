"""Warm-mint tests: the engine's cancellation-safety invariant and session-handle ownership."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import time
from pathlib import Path
from typing import Any

import connections_warm_helpers
import pytest
from connections_warm_helpers import _claim, _provider, _result, _Runtime, _SessionHandle

from kiro_crew.connections import warm
from kiro_crew.connections.mint import _mints

# Bound here by name: pytest registers the fixtures from this namespace, and other
# suites import the rest from this module.
_agents_dir = connections_warm_helpers._agents_dir
_clean_mint_table = connections_warm_helpers._clean_mint_table
_clean_warm_runtime = connections_warm_helpers._clean_warm_runtime
_mimic_spec = connections_warm_helpers._mimic_spec
_ours_shaped_spec_text = connections_warm_helpers._ours_shaped_spec_text
_parked_session = connections_warm_helpers._parked_session
_private_warm_home = connections_warm_helpers._private_warm_home
_rearm_registry = connections_warm_helpers._rearm_registry
_returns = connections_warm_helpers._returns
_stub_activation = connections_warm_helpers._stub_activation


# ── defect: the stand-down -> spawn transition is not BaseException-safe ──


@pytest.mark.asyncio
async def test_a_cancel_during_the_spec_write_still_arms_the_drain(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """Third route into the parked-generation leak. The park has already cancelled the old
    reaper, so a cancellation before a replacement exists leaves the parked process with
    nothing that will ever sweep it."""
    parked_runtime = _Runtime(True)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, parked_runtime)])

    def _cancel(plan, agents_dir):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_write_warm_mint_specs", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._ensure_locked([_provider("linear")])

    drain = warm._warm_mint._reaper
    assert drain is not None, "a park with no replacement reaper needs a drain armed"
    drain.cancel()


@pytest.mark.asyncio
async def test_a_cancel_during_the_spawn_kills_the_partial_runtime(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """A runtime that was constructed and may have forked a child must not be dropped on
    the floor when the spawn await is cancelled."""
    killed: list[Any] = []

    async def _record_kill(runtime):
        killed.append(runtime)
        return True

    class _CancelsOnSpawn:
        def __init__(self, **kwargs):
            pass

        async def spawn(self, start_priority=None):
            raise asyncio.CancelledError()

        def is_alive(self):
            return False

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _CancelsOnSpawn)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._ensure_locked([_provider("linear")])

    assert len(killed) == 1, "the constructed runtime was leaked"


# ── defect: a failed mint spawn logs only the exception class, not its cause ──


@pytest.mark.asyncio
async def test_a_failed_spawn_logs_the_exception_message_not_only_its_class(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, _agents_dir: Path
):
    """A spawn that raises degrades to the cold path, and the warning it leaves must carry
    the exception's own message. ``AcpRuntimeError``, ``SandboxUnavailableError`` and the
    spawn gates each author a sentence naming what failed and the remedy -- the Windows
    internal-sandbox delegation path raises one such sentence -- so logging the class name
    alone leaves an operator unable to tell a missing kiro-cli from a sandbox a host cannot
    provide, which is the only signal the cold path's per-provider retries do not repeat."""
    detail = "no OS-level sandbox backend is available on this host"

    class _RaisesWithDetail:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def spawn(self, start_priority=None) -> None:
            raise RuntimeError(detail)

        def is_alive(self) -> bool:
            return False

    async def _record_kill(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_write_warm_mint_specs", lambda plan, agents_dir: None)
    monkeypatch.setattr(warm, "_unowned_plan_specs", lambda plan, agents_dir: [])
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _RaisesWithDetail)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])

    with caplog.at_level("WARNING", logger="kiro_crew.connections.warm"):
        result = await warm._warm_mint._ensure_locked([_provider("linear")])

    assert result is None, "a failed spawn must degrade to the cold path"
    spawn_warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "kiro_crew.connections.warm"
        and "Shared mint process spawn failed" in rec.getMessage()
    ]
    assert spawn_warnings, "the failed spawn left no warning"
    message = spawn_warnings[0]
    assert "RuntimeError" in message, "the warning dropped the exception class"
    assert detail in message, "the warning dropped the exception's own cause"


# ── defect: settlement is not reached on every post-activation exit ──


@pytest.mark.asyncio
async def test_a_cancel_during_the_credential_screen_still_settles_the_session(
    monkeypatch: pytest.MonkeyPatch,
):
    """The screen's `to_thread` await sits between the session's creation and its
    settlement. A session left `settled=False` is permanently ineligible for the sweep, so
    it and its loopback callback servers accumulate for the life of the process."""
    settled: list[int] = []

    async def _record_settle(activation: int, in_use: set[int]) -> None:
        settled.append(activation)

    def _cancel(urls):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm._warm_mint, "settle_activation", _record_settle)
    monkeypatch.setattr(warm, "_credential_bearing_slugs", _cancel)

    claims = await _claim("linear")
    result = _result(
        [_provider("linear")],
        [{"serverName": "linear", "oauthUrl": "https://l/consent"}],
        activation=11,
    )

    with pytest.raises(asyncio.CancelledError):
        await warm._absorb_warm_requests(result, claims)

    assert settled == [11], "an unsettled session can never be collected"


@pytest.mark.asyncio
async def test_a_failing_absorb_still_settles_the_session(monkeypatch: pytest.MonkeyPatch):
    """Not only cancellation: any raise after the activation exists has the same
    consequence, so the settlement belongs in a finally rather than on one branch."""
    settled: list[int] = []

    async def _record_settle(activation: int, in_use: set[int]) -> None:
        settled.append(activation)

    def _explode(urls):
        raise OSError("the operator endpoint file is unreadable")

    monkeypatch.setattr(warm._warm_mint, "settle_activation", _record_settle)
    monkeypatch.setattr(warm, "_credential_bearing_slugs", _explode)

    claims = await _claim("linear")
    result = _result(
        [_provider("linear")], [{"serverName": "linear", "oauthUrl": "https://l/consent"}]
    )

    with pytest.raises(OSError):
        await warm._absorb_warm_requests(result, claims)

    assert settled == [3]


# ── defect (found by the systematic audit, not the lane): the retiring sweep drops
# generations from its own list before killing them ──


@pytest.mark.asyncio
async def test_a_cancel_mid_sweep_leaves_the_unkilled_generations_parked(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_sweep_retiring_locked` assigns the keep-list before it kills the drop-list, so a
    cancellation partway through would remove BOTH generations from `_retiring` while only
    one was actually killed -- `parked_count()` then reads zero and the drain exits, so
    nothing ever retries. A generation whose kill completed is gone; one whose kill was
    interrupted stays parked for a later sweep."""
    first, second = _Runtime(False), _Runtime(False)
    killed: list[Any] = []

    async def _kill_then_cancel(runtime):
        killed.append(runtime)
        if len(killed) == 2:
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _kill_then_cancel)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(1, first), (2, second)])

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint.sweep_retiring()

    assert killed == [first, second]
    assert warm._warm_mint.parked_count() == 1, "the ungathered generation must stay parked"
    assert warm._warm_mint._retiring == [(2, second)]


# ── a kill that TIMES OUT is not a kill ──
#
# ``_kill_quietly`` swallowed every ``Exception`` and returned None, so a kill that timed out
# was indistinguishable from one that worked: each caller then dropped the only reference to
# a child that is still running. ``asyncio.TimeoutError`` IS an ``Exception``, so none of the
# three retention paths above -- all built for a ``CancelledError``, which is not -- ever saw
# it. Repeated activations therefore accumulated live kiro-cli processes, their sessions and
# their loopback listeners, with nothing left that could ever retire them.


class _UnkillableRuntime:
    """A process whose kill fails the way a timeout does: a plain ``Exception``.

    Fails ``failures`` times and then succeeds, so one test can pin both halves of the
    contract -- retained while the kill does not take, released once it does.
    """

    def __init__(self, failures: int = 1) -> None:
        self._failures = failures
        self.kill_attempts = 0

    def is_alive(self) -> bool:
        return True

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        self.kill_attempts += 1
        if self.kill_attempts <= self._failures:
            raise TimeoutError("kill timed out")


@pytest.fixture
def _no_drain(monkeypatch: pytest.MonkeyPatch):
    """Neutralize the drain task and the spec sweep, keeping the arming observable."""

    async def _noop() -> None:
        return None

    monkeypatch.setattr(warm, "_drain_parked_generations", _noop)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)


@pytest.mark.asyncio
async def test_a_sweep_whose_kill_times_out_keeps_the_generation_parked(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """RED before the fix: the pair was popped and the surviving child forgotten."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, doomed)])

    await warm._warm_mint.sweep_retiring()

    assert doomed.kill_attempts == 1
    assert warm._warm_mint._retiring == [(5, doomed)], "a surviving child must stay tracked"
    assert warm._warm_mint.parked_count() == 1

    # The next pass is what retires it -- and only then does the list clear.
    await warm._warm_mint.sweep_retiring()

    assert doomed.kill_attempts == 2
    assert warm._warm_mint._retiring == []


@pytest.mark.asyncio
async def test_a_stand_down_whose_kill_times_out_re_parks_the_process(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """``_park_or_kill_locked`` clears ``_runtime`` first, so this list is the last reference."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_runtime", doomed)
    monkeypatch.setattr(warm._warm_mint, "_generation", 7)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    await warm._warm_mint._park_or_kill_locked()

    assert warm._warm_mint._retiring == [(7, doomed)]
    assert warm._warm_mint._reaper is not None, "nothing was scheduled to retry the kill"


@pytest.mark.asyncio
async def test_a_hard_teardown_whose_kill_times_out_keeps_the_child_tracked(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """``shutdown()`` empties both lists up front; an unkilled child must come back."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_runtime", doomed)
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    await warm._warm_mint.shutdown()

    assert warm._warm_mint._retiring == [(9, doomed)]
    assert warm._warm_mint._reaper is not None


@pytest.mark.asyncio
async def test_an_abandoned_spawn_whose_kill_times_out_is_tracked_not_dropped(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """The one path with no generation of its own: tracked under a key no row can carry, so
    every sweep reads it as needed by nobody and retries the kill until it takes."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    await warm._warm_mint._abandon_spawn_locked(doomed)

    assert warm._warm_mint._retiring == [(warm._WARM_UNKEYED_GENERATION, doomed)]
    assert warm._WARM_UNKEYED_GENERATION < 0, "a real generation would collide with its rows"
    assert not warm._generation_holds_live_rows(warm._WARM_UNKEYED_GENERATION)


@pytest.mark.asyncio
async def test_a_spec_scope_is_retained_while_an_unkilled_child_still_needs_it(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A spec tree removed under a process that still runs strands that process."""
    doomed = _UnkillableRuntime(failures=1)
    doomed.pid = 6060
    monkeypatch.setattr(warm.platform_compat, "process_start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm.platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")

    def _liveness(pid: int) -> str:
        """Report the doomed child dead only once its kill has actually taken.

        Stated rather than left to the host: releasing the tree now requires positive proof
        the runtime is gone, and an arbitrary fake PID can collide with a real process on
        the machine running the suite -- which would otherwise make this test's outcome
        depend on who happens to own PID 6060.
        """
        if pid != doomed.pid:
            return warm.platform_compat.PID_ALIVE
        return (
            warm.platform_compat.PID_DEAD
            if doomed.kill_attempts > 1
            else warm.platform_compat.PID_ALIVE
        )

    monkeypatch.setattr(warm.platform_compat, "pid_liveness", _liveness)
    work_dir = warm._create_warm_generation_dir()
    warm._bind_warm_generation(doomed, work_dir)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])

    assert await warm._warm_mint._kill_generation(3, doomed) is False
    assert work_dir.is_dir(), "the surviving child's private specs were removed"

    assert await warm._warm_mint._kill_generation(3, doomed) is True
    assert not work_dir.exists(), "a confirmed kill must release its private specs"


# ── the invariant itself, pinned where it is mechanically checkable ──


def _await_protections(func: Any) -> list[tuple[str, list[str]]]:
    """Every await in ``func`` as ``(source text, enclosing try protections)``.

    A protection reads ``finally`` or ``except BaseException`` -- the only two shapes a
    ``CancelledError`` cannot walk past. An ``except Exception`` is recorded so the guard can
    say what WAS there rather than only that nothing qualified.
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    found: list[tuple[str, list[str]]] = []

    def label(node: ast.Try) -> str:
        kinds = ["finally"] if node.finalbody else []
        for handler in node.handlers:
            if handler.type is None:
                kinds.append("except BaseException")
            elif isinstance(handler.type, ast.Name):
                kinds.append(f"except {handler.type.id}")
            else:
                kinds.append("except <expr>")
        return "+".join(kinds)

    def walk(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, ast.Try):
            here = label(node)
            for child in node.body:
                walk(child, stack + [here])
            for handler in node.handlers:
                for child in handler.body:
                    walk(child, stack)
            for child in node.finalbody + node.orelse:
                walk(child, stack)
            return
        if isinstance(node, ast.Await):
            found.append((ast.get_source_segment(source, node) or "", list(stack)))
        for child in ast.iter_child_nodes(node):
            walk(child, stack)

    for child in ast.iter_child_nodes(tree):
        walk(child, [])
    return found


#: The awaits that sit between a state mutation and that mutation's settlement or cleanup.
#: Named individually rather than "this function has SOME protected try", because the coarse
#: form passed while a sibling await in the same function was still bare.
_MUST_BE_PROTECTED = [
    ("warm_mint_all", "_warm_activate", warm.warm_mint_all),
    ("warm_mint_all", "_absorb_warm_requests", warm.warm_mint_all),
    ("warm_mint_all", "_dispose_displaced_rows", warm.warm_mint_all),
    ("_absorb_warm_requests", "_credential_bearing_slugs", warm._absorb_warm_requests),
    (
        "_activate_locked",
        "asyncio.shield(create)",
        warm._WarmMintRuntime._activate_locked,
    ),
    (
        "_activate_locked",
        "_collect_oauth_requests",
        warm._WarmMintRuntime._activate_locked,
    ),
    (
        "_recover_redrained_requests",
        "_dispose_displaced_rows",
        warm._recover_redrained_requests,
    ),
    (
        "_recover_redrained_requests",
        "_absorb_warm_requests",
        warm._recover_redrained_requests,
    ),
    (
        "_abandon_session_creation_locked",
        "_destroy_session_quietly",
        warm._WarmMintRuntime._abandon_session_creation_locked,
    ),
    (
        "_sweep_sessions_locked",
        "_destroy_session_quietly",
        warm._WarmMintRuntime._sweep_sessions_locked,
    ),
    ("_ensure_locked", "_write_warm_mint_specs", warm._WarmMintRuntime._ensure_locked),
    ("_ensure_locked", "runtime.spawn()", warm._WarmMintRuntime._ensure_locked),
    ("shutdown", "asyncio.gather(rearm", warm._WarmMintRuntime.shutdown),
    ("_sweep_retiring_locked", "_kill_generation", warm._WarmMintRuntime._sweep_retiring_locked),
    ("_park_or_kill_locked", "_kill_generation", warm._WarmMintRuntime._park_or_kill_locked),
    ("_retire_locked", "_kill_quietly", warm._WarmMintRuntime._retire_locked),
]

_PROTECTING = ("finally", "except BaseException")


def test_every_post_mutation_await_is_individually_protected():
    """An await between a state mutation and its settlement or cleanup needs more than an
    `except Exception`, which a CancelledError walks straight past. Bound to the SPECIFIC
    await rather than the function, so a newly-added sibling await in an already-protected
    function cannot pass by association."""
    for name, target, func in _MUST_BE_PROTECTED:
        awaits = _await_protections(func)
        matching = [(text, stack) for text, stack in awaits if target in text]
        assert matching, (
            f"{name}: no await matching {target!r} -- the guard is reading the wrong code, "
            "so update this table rather than deleting the entry"
        )
        for text, stack in matching:
            assert any(any(kind in entry for kind in _PROTECTING) for entry in stack), (
                f"{name}: `{text}` is guarded only by {stack or ['nothing']} -- a "
                "CancelledError walks past every `except Exception`, which is exactly the "
                "bug class three review rounds found"
            )


# ── two more of the same class, found by the systematic audit ──


@pytest.mark.asyncio
async def test_a_cancel_during_the_stand_down_kill_re_parks_the_process(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_park_or_kill_locked` clears `_runtime` and cancels the reaper BEFORE it awaits the
    kill, so a cancellation inside that kill would drop the only reference to a process
    that is still running -- neither registered, nor parked, nor dead."""
    doomed = _Runtime(False)

    async def _cancel(runtime):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_kill_quietly", _cancel)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", doomed)
    monkeypatch.setattr(warm._warm_mint, "_generation", 7)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._park_or_kill_locked()

    assert warm._warm_mint._retiring == [(7, doomed)], "an unkilled process must stay tracked"
    drain = warm._warm_mint._reaper
    assert drain is not None, "and something must be scheduled to retire it"
    drain.cancel()


@pytest.mark.asyncio
async def test_a_cancel_during_the_hard_teardown_re_parks_what_is_left(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_retire_locked` empties `_retiring` and clears `_runtime` up front, so a
    cancellation partway through its kill loop would leak every remaining process with no
    reference left anywhere."""
    parked, current = _Runtime(False), _Runtime(False)
    killed: list[Any] = []

    async def _kill_then_cancel(runtime):
        killed.append(runtime)
        if len(killed) == 2:
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _kill_then_cancel)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", current)
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(8, parked)])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._retire_locked()

    # The parked one was killed first and is gone; the current one was interrupted.
    assert killed == [parked, current]
    assert warm._warm_mint._retiring == [(9, current)]
    drain = warm._warm_mint._reaper
    assert drain is not None
    drain.cancel()


# ── defect: session-handle ownership transfers are not cancellation-safe ──


@pytest.mark.asyncio
async def test_an_abandoned_create_still_destroys_the_session_the_backend_made(
    monkeypatch: pytest.MonkeyPatch,
):
    """The backend's session/new has already succeeded when our wait is abandoned. The
    handle is the ONLY way to terminate that session and its loopback callback children, so
    dropping it leaks them until the whole runtime is retired."""
    created: list[Any] = []
    exists = asyncio.Event()

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            handle = _SessionHandle(created)  # the backend session now EXISTS
            exists.set()
            await asyncio.sleep(0.2)  # ...and our wait is abandoned during this
            return handle

    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    task = asyncio.get_running_loop().create_task(
        warm._warm_mint._activate_locked("agent", frozenset())
    )
    await exists.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(created) == 1
    assert created[0].destroyed is True, "an abandoned session must still be terminated"
    assert warm._warm_mint._sessions == {}


@pytest.mark.asyncio
async def test_a_cancel_mid_session_destroy_keeps_the_record_for_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    """The sweep popped the record before awaiting the destroy, so a cancellation there lost
    the only reference to a session that is still listening."""

    async def _cancels(handle: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_destroy_session_quietly", _cancels)
    record = warm._WarmSession(generation=1, handle=object(), expires_at=0.0, settled=True)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {5: record})

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint.sweep_sessions(set())

    assert warm._warm_mint._sessions == {5: record}, "the only retry record must survive"


@pytest.mark.asyncio
async def test_a_cancel_destroying_an_unstamped_session_leaves_it_sweepable(
    monkeypatch: pytest.MonkeyPatch,
):
    """The activation's own failure path has the same shape: it popped the record, so a
    cancellation mid-destroy lost it. Retained instead -- and marked settled and expired, so
    the next sweep is the retry rather than nothing being."""
    created: list[Any] = []

    class _PollExplodes:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            return _SessionHandle(created)

    async def _cancels(handle: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_destroy_session_quietly", _cancels)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _PollExplodes())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    monkeypatch.setattr(warm, "_WARM_OAUTH_SETTLE_ROUNDS", 1)
    # The oauth poll raises, which is what sends the activation down its teardown path.
    monkeypatch.setattr(
        _SessionHandle,
        "pop_pending_oauth_requests",
        lambda self: (_ for _ in ()).throw(RuntimeError("frame decode failed")),
    )

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._activate_locked("agent", frozenset())

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "an undestroyed session must stay tracked"
    record = next(iter(sessions.values()))
    assert (
        record.settled is True and record.expires_at <= time.monotonic()
    ), "and must be eligible for the sweep, or retaining it just moves the leak"


@pytest.mark.asyncio
async def test_a_cancel_destroying_an_abandoned_session_leaves_it_sweepable(
    monkeypatch: pytest.MonkeyPatch,
):
    """The reaped handle enters the SAME ownership rule as every other session: registered
    settled-and-expired before the destroy, so an interrupted destroy is retried by the
    ordinary sweep rather than losing the only reference."""
    created: list[Any] = []
    exists = asyncio.Event()

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            handle = _SessionHandle(created)
            exists.set()
            await asyncio.sleep(0.2)
            return handle

    async def _cancels(handle: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_destroy_session_quietly", _cancels)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    task = asyncio.get_running_loop().create_task(
        warm._warm_mint._activate_locked("agent", frozenset())
    )
    await exists.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "an undestroyed reaped session must stay tracked"
    record = next(iter(sessions.values()))
    assert record.settled is True and record.expires_at <= time.monotonic()


# ── defect: an unaddressable abandoned session accumulated without bound ──
#
# The no-handle abandon path's acceptance was "reaped when the runtime is retired". But
# retirement is not guaranteed to arrive: any card holding a URL keeps `_shared_mints_pending`
# true, which resets the reaper's idle clock every cycle, while `_ensure_locked`'s
# digest-equality fast path keeps the SAME generation reusable -- so every repetition parked
# another orphan session and its callback children on one live process.


@pytest.mark.asyncio
async def test_an_unaddressable_abandoned_session_quarantines_its_generation(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """Quarantining bounds the residual to ONE generation's worth: the next activation finds
    the resident plan unservable, stands the generation down, and the orphan dies with the
    process."""
    monkeypatch.setattr(warm, "_WARM_SESSION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(warm, "_WARM_SESSION_REAP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_write_warm_mint_specs", lambda plan, agents_dir: None)
    monkeypatch.setattr(warm, "_unowned_plan_specs", lambda plan, agents_dir: [])

    class _NeverReturnsAHandle:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            # No handle ever becomes reachable to us, so nothing is addressable to destroy.
            await asyncio.Event().wait()

    stale = _NeverReturnsAHandle()
    # The REAL resident plan, so the digest fast path is genuinely reachable -- otherwise the
    # test would respawn for an unrelated reason and prove nothing.
    resident = warm._warm_spec_plan([_provider("linear")])
    monkeypatch.setattr(warm._warm_mint, "_runtime", stale)
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])

    with pytest.raises(BaseException):
        await warm._warm_mint._activate_locked("agent", frozenset())

    assert warm._warm_mint._plan is None, "the generation must be quarantined"
    assert warm._warm_mint._digest == ""

    # A subsequent activation must stand the quarantined generation DOWN, not reuse it.
    killed: list[Any] = []

    async def _record_kill(runtime: Any) -> bool:
        killed.append(runtime)
        return True

    class _Spawnable:
        def __init__(self, **kwargs) -> None:
            pass

        async def spawn(self, start_priority=None) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Spawnable)

    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is not None
    assert killed == [stale], "the process holding the orphan session was reused instead"
    reaper = warm._warm_mint._reaper
    if reaper is not None:
        reaper.cancel()


@pytest.mark.asyncio
async def test_a_recovered_handle_does_not_quarantine_the_generation(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """The opposite failure mode: the RECOVERED-handle path registers and destroys cleanly,
    so it needs no stand-down. Quarantining there would cost a respawn on every transient
    timeout that the reap already fully repaired."""
    monkeypatch.setattr(warm, "_WARM_SESSION_TIMEOUT_SECONDS", 0.01)
    created: list[Any] = []

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            handle = _SessionHandle(created)
            await asyncio.sleep(0.05)
            return handle

    resident = warm._warm_spec_plan([_provider("linear")])
    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    with pytest.raises(BaseException):
        await warm._warm_mint._activate_locked("agent", frozenset())

    assert created and created[0].destroyed is True, "the reap must still have worked"
    assert warm._warm_mint._plan is resident, "a fully repaired reap must not force a respawn"
    assert warm._warm_mint._digest == resident.digest


# ── defect: the settle loop never CONSUMED the session queue ──
#
# `pop_pending_oauth_requests()` reads a list that only `drain_init` appends to, and
# `create_session` runs exactly one drain before returning the handle. So `asyncio.sleep`
# between pops moved nothing: a frame arriving after that create-time drain's idle exit was
# unreachable no matter how many rounds elapsed. The budget was never the binding
# constraint -- the loop had no mechanism to absorb a late frame at all.


class _QueuedOauthHandle:
    """A session handle with the real contract: a frame is poppable only after a DRAIN.

    ``available_after_drains`` is how many ``drain_init`` calls must have happened before
    that provider's frame moves onto the poppable list -- 0 models a frame staged during
    ``session/new`` and already drained by ``create_session``.
    """

    def __init__(self, schedule: dict[str, int]) -> None:
        self._schedule = dict(schedule)
        self._poppable: list[dict[str, str]] = []
        self.drains = 0
        self.destroyed = False
        self._promote()

    def _promote(self) -> None:
        for name, due in sorted(self._schedule.items()):
            if due <= self.drains:
                self._poppable.append({"serverName": name, "oauthUrl": f"https://{name}/consent"})
                del self._schedule[name]

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        out, self._poppable = list(self._poppable), []
        return out

    async def drain_init(self, **kwargs) -> None:
        self.drains += 1
        self._promote()

    async def destroy(self) -> None:
        self.destroyed = True


def _queued_runtime(handle: Any) -> Any:
    class _Runtime:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            return handle

    return _Runtime()


@pytest.mark.asyncio
async def test_a_frame_arriving_after_the_create_drain_is_still_absorbed(
    monkeypatch: pytest.MonkeyPatch,
):
    """`linear`'s frame was staged during session/new; `vercel`'s lands three windows later.
    Sleeping past it consumed nothing, so it was never poppable."""
    handle = _QueuedOauthHandle({"linear": 0, "vercel": 3})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    activation, requests = await warm._warm_mint._activate_locked(
        "agent", frozenset({"linear", "vercel"})
    )

    names = sorted(str(request["serverName"]) for request in requests)
    assert names == ["linear", "vercel"], "a late frame must be consumed, not slept past"
    assert activation in warm._warm_mint._sessions
    assert handle.destroyed is False


@pytest.mark.asyncio
async def test_each_new_frame_renews_the_quiet_budget_past_the_old_fixed_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    handle = _QueuedOauthHandle({"linear": 5, "vercel": 10})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    _, requests = await warm._warm_mint._activate_locked("agent", frozenset({"linear", "vercel"}))

    assert sorted(str(request["serverName"]) for request in requests) == ["linear", "vercel"]
    assert handle.drains == 10


@pytest.mark.asyncio
async def test_progress_is_still_bounded_by_the_expected_roster(
    monkeypatch: pytest.MonkeyPatch,
):
    wanted = frozenset({"linear", "vercel", "missing"})
    handle = _QueuedOauthHandle({"linear": 6, "vercel": 12})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    _, requests = await warm._warm_mint._activate_locked("agent", wanted)

    assert sorted(str(request["serverName"]) for request in requests) == ["linear", "vercel"]
    assert handle.drains == len(wanted) * warm._WARM_OAUTH_SETTLE_ROUNDS


@pytest.mark.asyncio
async def test_the_settle_loop_stops_as_soon_as_every_wanted_frame_is_in(
    monkeypatch: pytest.MonkeyPatch,
):
    """Consuming must not cost latency when the frames are already staged: the loop still
    short-circuits on the first pop and opens no drain window at all."""
    handle = _QueuedOauthHandle({"linear": 0})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    _, requests = await warm._warm_mint._activate_locked("agent", frozenset({"linear"}))

    assert [str(r["serverName"]) for r in requests] == ["linear"]
    assert handle.drains == 0, "an already-satisfied activation must open no drain window"


@pytest.mark.asyncio
async def test_the_settle_loop_consumes_on_every_round_it_waits(
    monkeypatch: pytest.MonkeyPatch,
):
    """The budget is only meaningful if every waiting round is a CONSUMING round -- one
    bare sleep anywhere in the loop is a window a frame can land in unseen."""
    handle = _QueuedOauthHandle({"linear": 0, "never": 99})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    await warm._warm_mint._activate_locked("agent", frozenset({"linear", "never"}))

    assert (
        handle.drains == warm._WARM_OAUTH_SETTLE_ROUNDS
    ), "every quiet round must consume before the inactivity budget is exhausted"


@pytest.mark.asyncio
async def test_a_frame_past_the_whole_budget_releases_its_claim_cleanly(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """Beyond the budget the cold path is the correct answer, so the claim must be RELEASED
    -- no row left minting, and the token fence still refuses a late absorb."""
    provider = _provider("slowpoke")
    claims = await _claim("slowpoke")
    token = claims["slowpoke"]

    # The activation produced no frame for it at all.
    empty = _result([provider], [])
    assert await warm._absorb_warm_requests(empty, claims) == []
    assert "slowpoke" not in _mints, "an unfulfilled claim must not sit in the table"

    # A later activation re-claims the slug; the first claim's token must not resurrect it.
    fresh = await _claim("slowpoke")
    assert fresh["slowpoke"] != token
    late = _result([provider], [{"serverName": "slowpoke", "oauthUrl": "https://s/consent"}])
    assert await warm._absorb_warm_requests(late, claims) == []
    assert _mints["slowpoke"]["state"] == "minting"
    assert _mints["slowpoke"]["token"] == fresh["slowpoke"]


# ── a session destroy that TIMES OUT is not a destroy ──
#
# `_destroy_session_quietly` swallowed every `Exception` and returned None, so a destroy that
# timed out was indistinguishable from one that worked. The record each caller then dropped
# is the ONLY reference to the handle, so a session that is still listening -- and the
# loopback callback children it owns -- became unaddressable, with nothing that could retry.
# `asyncio.TimeoutError` IS an `Exception`, so the retention every call site already had --
# each written for a `CancelledError`, which is not -- never covered the case. The same class
# as `_kill_quietly` above, one layer down.


class _UndestroyableHandle:
    """A session whose destroy fails the way a timeout does: a plain ``Exception``.

    Fails ``failures`` times and then succeeds, so one test pins both halves of the
    contract -- retained while the destroy does not take, forgotten once it does.
    """

    def __init__(self, failures: int = 1) -> None:
        self._failures = failures
        self.destroy_attempts = 0

    async def destroy(self) -> None:
        self.destroy_attempts += 1
        if self.destroy_attempts <= self._failures:
            raise TimeoutError("session destroy timed out")

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        return []


@pytest.mark.asyncio
async def test_a_session_destroy_that_times_out_reports_false():
    """The verdict itself: without it no caller can tell the two outcomes apart."""
    handle = _UndestroyableHandle(failures=1)

    assert await warm._destroy_session_quietly(handle) is False
    assert await warm._destroy_session_quietly(handle) is True


@pytest.mark.asyncio
async def test_a_session_sweep_whose_destroy_times_out_keeps_the_record(
    monkeypatch: pytest.MonkeyPatch,
):
    """RED before the fix: the record was popped and the live session forgotten."""
    handle = _UndestroyableHandle(failures=1)
    record = warm._WarmSession(generation=1, handle=handle, expires_at=0.0, settled=True)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {5: record})

    await warm._warm_mint.sweep_sessions(set())

    assert handle.destroy_attempts == 1
    assert warm._warm_mint._sessions == {5: record}, "the last reference left must survive"

    # The next sweep is the retry -- and only then is the record forgotten.
    await warm._warm_mint.sweep_sessions(set())

    assert handle.destroy_attempts == 2
    assert warm._warm_mint._sessions == {}


@pytest.mark.asyncio
async def test_an_activation_teardown_whose_destroy_times_out_leaves_it_sweepable(
    monkeypatch: pytest.MonkeyPatch,
):
    """The activation's own failure path popped unconditionally, so the sweep it marks the
    record for could never run."""
    handle = _UndestroyableHandle(failures=99)

    class _PollExplodes:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            return handle

    monkeypatch.setattr(warm._warm_mint, "_runtime", _PollExplodes())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    monkeypatch.setattr(warm, "_WARM_OAUTH_SETTLE_ROUNDS", 1)
    # The oauth poll raises, which is what sends the activation down its teardown path.
    monkeypatch.setattr(
        _UndestroyableHandle,
        "pop_pending_oauth_requests",
        lambda self: (_ for _ in ()).throw(RuntimeError("frame decode failed")),
    )

    with pytest.raises(RuntimeError):
        await warm._warm_mint._activate_locked("agent", frozenset())

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "a session that may still be listening must stay tracked"
    record = next(iter(sessions.values()))
    assert record.handle is handle
    assert (
        record.settled is True and record.expires_at <= time.monotonic()
    ), "and must be eligible for the sweep, or retaining it just moves the leak"


@pytest.mark.asyncio
async def test_a_reaped_session_whose_destroy_times_out_stays_tracked(
    monkeypatch: pytest.MonkeyPatch,
):
    """The recovered-handle path registers the reaped session settled-and-expired for exactly
    this reason, so a destroy that does not take must leave the record for the sweep."""
    handle = _UndestroyableHandle(failures=99)

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            await asyncio.sleep(0.05)
            return handle

    monkeypatch.setattr(warm, "_WARM_SESSION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    with pytest.raises(asyncio.TimeoutError):
        await warm._warm_mint._activate_locked("agent", frozenset())

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "the reaped session may still be listening; keep it tracked"
    record = next(iter(sessions.values()))
    assert record.handle is handle
    assert record.settled is True and record.expires_at <= time.monotonic()
