"""The SEL audit tail must survive every gateway hard exit.

SEL logging is asynchronous: :meth:`SecurityEventLog.log` enqueues onto an
unbounded queue drained by a **daemon** writer thread, and the only thing that
guarantees the queue reaches disk is a drain registered with :mod:`atexit`.

``os._exit`` runs no ``atexit`` handler and does not join daemon threads. So on
any hard-exit path that does not flush for itself, the events recorded while the
gateway was shutting down -- the last audit records before the process is gone,
and the ones an investigator reads first -- are dropped with no error and no gap
marker. Every gateway hard exit must explicitly drain this queue.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from kiro_crew.sel import (
    SecurityEvent,
    SecurityEventLog,
    flush_audit_queue,
    flush_audit_queue_before_hard_exit,
)


def _retire_writer(log: SecurityEventLog) -> None:
    """Stop the daemon writer this test started, before dropping the singleton.

    Clearing ``_instance`` on its own abandons a live thread: it keeps holding
    the queue, and the test's ``tmp_path``, for the rest of the worker. That is
    the leak the repo's testing guidance names -- a singleton with a daemon
    thread beats every filesystem cleanup -- and these tests start a real writer
    precisely because a mock would not prove the drain.

    Flush first so nothing queued is lost, then the ``None`` sentinel makes
    ``_writer_loop`` return, then join. The flush is best-effort: several tests
    here deliberately replace ``flush`` with a raising or wedged stub, and
    teardown must retire the thread regardless.
    """
    writer = log._writer
    if writer is None or not writer.is_alive():
        return
    try:
        log.flush(timeout=5.0)
    except Exception:
        pass
    log._queue.put(None)
    writer.join(timeout=5.0)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the SEL singleton between tests, retiring any writer it started."""
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False
    yield
    live = SecurityEventLog._instance
    if live is not None:
        _retire_writer(live)
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False


@pytest.fixture
def sel_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A unique base dir for a real-writer SEL instance, retained for the whole
    session rather than deleted at this test's teardown.

    These tests start a REAL daemon writer (a mock would not prove the drain).
    ``_retire_writer`` joins that thread with a timeout, which bounds but does
    not guarantee termination, so a writer could briefly outlive teardown. If
    its base dir were the per-test ``tmp_path`` pytest deletes, a late
    append could recreate the just-deleted tree through the writer's
    mkdir/retry path -- a cross-test filesystem side effect. A
    ``tmp_path_factory`` dir is unique per test and lives until the session
    ends, so a straggler write lands in a directory nothing else observes and
    pytest cleans up at the end.
    """
    return tmp_path_factory.mktemp("sel")


def _event(event_id: str) -> SecurityEvent:
    return SecurityEvent(
        event_id=event_id,
        timestamp="2026-05-13T00:00:00+00:00",
        event_type="tool_invocation",
        caller_identity="dashboard:abc",
        agent="kirocrew",
        source="dashboard",
        operation="execute_bash",
        outcome="approved",
    )


class TestFlushAuditQueue:
    """The synchronous drain, used by the sync signal handler."""

    def test_queued_tail_reaches_disk(self, sel_dir: Path) -> None:
        """The records enqueued immediately before a hard exit are on disk when
        the drain returns -- the whole reason for calling it."""
        log = SecurityEventLog(base_dir=sel_dir)  # async writer, as in prod
        log.log(_event("shutdown-tail"))
        flush_audit_queue()
        written = (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")
        assert "shutdown-tail" in written

    def test_no_singleton_is_a_no_op_and_creates_nothing(self, tmp_path, monkeypatch):
        """With no live SEL there is nothing queued, and constructing one to
        find that out would create the trust directory and HMAC key as a side
        effect of leaving. The drain must not reach for the filesystem at all."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        before = sorted(p.name for p in tmp_path.iterdir())
        flush_audit_queue()
        assert SecurityEventLog._instance is None
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_a_wedged_writer_cannot_hang_the_exit(self, sel_dir: Path) -> None:
        """A writer stuck on a full disk or an unreachable sink must not hold
        the process on its way out. ``flush_audit_queue`` carries its own
        bounded deadline into ``flush`` so the wait can only end on that
        deadline, never block unbounded. Assert the propagation at that seam
        rather than measuring an elapsed wall duration, which a paused runner
        would inflate past any fixed bound and fail a correct drain."""
        log = SecurityEventLog(base_dir=sel_dir)
        seen_timeout: list[float] = []

        def recording_flush(*, timeout: float) -> None:
            seen_timeout.append(timeout)

        log.flush = recording_flush  # type: ignore[method-assign]
        flush_audit_queue(timeout=0.2)
        assert seen_timeout == [0.2]

    def test_a_raising_flush_never_escapes(self, sel_dir: Path) -> None:
        """A hard exit must not be blocked, or replaced, by auditing."""
        log = SecurityEventLog(base_dir=sel_dir)

        def boom(**_kwargs):
            raise RuntimeError("writer exploded")

        log.flush = boom  # type: ignore[method-assign]
        flush_audit_queue()  # does not raise


class TestFlushAuditQueueBeforeHardExit:
    """The async wrapper, used by the two coroutine hard-exit paths."""

    @pytest.mark.asyncio
    async def test_queued_tail_reaches_disk(self, sel_dir: Path) -> None:
        log = SecurityEventLog(base_dir=sel_dir)
        log.log(_event("async-shutdown-tail"))
        await flush_audit_queue_before_hard_exit()
        written = (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")
        assert "async-shutdown-tail" in written

    @pytest.mark.asyncio
    async def test_the_blocking_drain_runs_off_the_event_loop(self, sel_dir) -> None:
        """flush() blocks on a condition variable. Doing that inline would park
        the loop for the whole deadline, which is what the executor hop buys."""
        log = SecurityEventLog(base_dir=sel_dir)
        flush_threads: list[int] = []
        real_flush = log.flush

        def recording_flush(**kwargs):
            flush_threads.append(threading.get_ident())
            return real_flush(**kwargs)

        log.flush = recording_flush  # type: ignore[method-assign]
        log.log(_event("off-loop"))
        await flush_audit_queue_before_hard_exit()
        assert flush_threads, "the drain never ran"
        assert threading.get_ident() not in flush_threads

    @pytest.mark.asyncio
    async def test_a_wedged_writer_delays_neither_the_loop_nor_the_exit(self, sel_dir, monkeypatch):
        """The outer deadline holds even when the offloaded drain does not.
        The wrapper caps the executor hop with ``asyncio.wait_for`` at
        ``timeout + 1.0``; assert that cap is applied and that the wedged drain
        does not escape it, rather than timing an elapsed wall duration a
        paused runner could inflate past a fixed bound."""
        log = SecurityEventLog(base_dir=sel_dir)
        released = threading.Event()

        def wedged(**_kwargs):
            released.wait(30.0)

        log.flush = wedged  # type: ignore[method-assign]

        real_wait_for = asyncio.wait_for
        seen_timeout: list[float | None] = []

        async def recording_wait_for(aw, timeout):
            seen_timeout.append(timeout)
            return await real_wait_for(aw, timeout)

        monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
        try:
            # Returns (does not raise, does not hang) on the outer deadline.
            await flush_audit_queue_before_hard_exit(timeout=0.2)
        finally:
            released.set()
        # The hop was bounded at timeout + 1.0, and the wedged drain was
        # swallowed rather than propagating its 30s block.
        assert seen_timeout == [pytest.approx(1.2)]


class TestEveryGatewayHardExitFlushesTheAuditQueue:
    """Ratchet: the audit log only survives if EVERY hard exit in the gateway
    process drains it. ``slack/gateway.py``, ``slack/events.py`` and
    ``platform_compat.py`` are the modules that call ``os._exit`` or
    ``platform_compat.hard_exit`` from inside the long-lived gateway process --
    the other ``os._exit`` sites in the tree (``sandbox.py``,
    ``_process_group_supervisor.py``) run in forked/pre-exec children that never
    initialize a SEL singleton.

    ``platform_compat.hard_exit`` itself is the shared ``os._exit`` primitive the
    gateway's signal force-exit and the restart path both route through, each
    after draining SEL in its OWN body. Like the gateway.log sibling's
    ``drain_log_queue_before_hard_exit``, the primitive does not drain -- it is a
    sync pass-through a signal handler calls and cannot be made to await -- so the
    scan exempts it BY NAME. The exemption is the one function ``hard_exit`` in
    ``platform_compat.py``; every other ``os._exit`` in that module (such as
    ``exit_after_failed_restart_exec``) is still held to the contract, which is
    exactly the gap this ratchet now closes.

    The check is per-function and does NOT look inside nested functions, so a
    flush in a sibling closure cannot vouch for its parent. It mirrors the
    gateway.log ratchet in ``test_cli_logging.py``: the two sinks fail by one
    mechanism, so they are held to one contract.

    What it requires is that the queue is drained, not that a particular
    function is called: the restart path's own inline drain satisfies it.
    """

    _MODULES = (
        Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/gateway.py",
        Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/events.py",
        Path(__file__).resolve().parents[1] / "src/kiro_crew/platform_compat.py",
    )
    _HELPERS = {"flush_audit_queue", "flush_audit_queue_before_hard_exit"}

    #: The shared ``os._exit`` primitive, exempt by name (see the class docstring).
    #: A ``module name -> {function names}`` map so the exemption cannot leak to a
    #: same-named function in another scanned module.
    _EXEMPT = {"platform_compat.py": {"hard_exit"}}

    @staticmethod
    def _has_inline_drain(fn):
        """True when ``fn``'s own body contains the ``sel().flush`` expression.

        The restart path drains the queue that way instead of through the
        helper. It is a different spelling of the same contract, not a
        violation, so the ratchet accepts it -- but it is matched
        STRUCTURALLY, not as a co-occurrence of the names ``sel`` and
        ``flush``. ``_dispatch``-sized functions mention both incidentally, and
        a name-pair rule would hand them a pass they have not earned.
        """
        found = False

        class _Walk(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # nested def: not fn's own body
                if node is fn:
                    self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Attribute(self, node):
                nonlocal found
                value = node.value
                if (
                    node.attr == "flush"
                    and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "sel"
                ):
                    found = True
                self.generic_visit(node)

        _Walk().visit(fn)
        return found

    def _drains(self, fn):
        return bool(self._own_body_names(fn) & self._HELPERS) or self._has_inline_drain(fn)

    @staticmethod
    def _own_body_names(fn):
        """Every Name/Attribute identifier in ``fn``'s own body, skipping the
        bodies of functions nested inside it."""
        names: set[str] = set()

        class _Walk(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # nested def: not fn's own body
                if node is fn:
                    self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Name(self, node):
                names.add(node.id)

            def visit_Attribute(self, node):
                names.add(node.attr)
                self.generic_visit(node)

        _Walk().visit(fn)
        return names

    def _hard_exit_functions(self, tree):
        """(function node, line) for each direct or wrapped hard exit, attributed
        to the nearest enclosing function."""
        parents: "dict[ast.AST, ast.AST]" = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        found = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (node.func.value.id, node.func.attr)
                in {("os", "_exit"), ("platform_compat", "hard_exit")}
            ):
                continue
            cur = parents.get(node)
            while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cur = parents.get(cur)
            if cur is not None:
                found.append((cur, node.lineno))
        return found

    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            ("os._exit(0)", True),
            ("platform_compat.hard_exit(0)", True),
            ("other._exit(0)", False),
            ("other.hard_exit(0)", False),
            ("os.hard_exit(0)", False),
            ("platform_compat._exit(0)", False),
            ("hard_exit(0)", False),
            ("platform_compat.hard_exit", False),
        ],
    )
    def test_matcher_recognizes_only_known_hard_exit_calls(self, call, expected):
        tree = ast.parse(f"def shutdown():\n    {call}\n")
        found = self._hard_exit_functions(tree)
        assert found == ([(tree.body[0], 2)] if expected else [])

    def test_the_ratchet_actually_finds_the_hard_exits(self):
        """A scan that matches nothing would pass vacuously."""
        total = 0
        for path in self._MODULES:
            total += len(self._hard_exit_functions(ast.parse(path.read_text(encoding="utf-8"))))
        assert total >= 4, f"expected the known hard-exit sites, found {total}"

    def test_no_hard_exit_strands_the_queued_audit_tail(self):
        violations = []
        for path in self._MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            exempt = self._EXEMPT.get(path.name, set())
            for fn, lineno in self._hard_exit_functions(tree):
                if fn.name in exempt:
                    continue
                if not self._drains(fn):
                    violations.append(f"{path.name}:{lineno} in {fn.name}()")
        assert not violations, (
            "os._exit runs no atexit handler and does not join the SEL daemon "
            "writer, so these hard exits drop the queued audit tail; await "
            "flush_audit_queue_before_hard_exit() (or call flush_audit_queue("
            "timeout=...) from a sync handler) first: " + ", ".join(violations)
        )

    def test_platform_compat_restart_exec_exit_drains(self):
        """``platform_compat.py`` is scanned, so ``exit_after_failed_restart_exec``
        -- a gateway hard exit -- is held to the drain contract and must satisfy
        it."""
        path = Path(__file__).resolve().parents[1] / "src/kiro_crew/platform_compat.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        by_name = {fn.name: fn for fn, _ in self._hard_exit_functions(tree)}
        assert (
            "exit_after_failed_restart_exec" in by_name
        ), "the restart-exec-failure hard exit must be scanned"
        assert self._drains(by_name["exit_after_failed_restart_exec"])

    def test_signal_handler_hands_the_force_exit_to_its_own_thread(self):
        """``_on_signal`` is a loop callback, so it must not block on the drains,
        and the second Ctrl-C is the escape hatch for a shutdown stuck on wedged
        executor work, so it must not queue behind a task or a shared pool. The
        handler starts ``_start_force_exit`` and uses neither."""
        path = Path(__file__).resolve().parents[1] / "src/kiro_crew/slack/gateway.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        handlers = [
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef) and fn.name == "_on_signal"
        ]
        assert len(handlers) == 1, "expected exactly one _on_signal handler"
        names = self._own_body_names(handlers[0])
        assert "_start_force_exit" in names
        # Repeat signals return before the print/cleanup: one force exit only.
        assert "_FORCE_EXIT" in names
        blocking_or_queued = {
            "flush_audit_queue",
            "_stop_log_queue_listener",
            "drain_for_shutdown",
            "create_task",
            "ensure_future",
            "to_thread",
            "run_in_executor",
        }
        assert not names & blocking_or_queued, names & blocking_or_queued


class TestForceExitThread:
    """The force exit runs its drains on a dedicated thread with an independent
    deadline: never on the calling thread, never on a shared executor."""

    @staticmethod
    def _patch(monkeypatch, sel_drain):
        from kiro_crew import cli, eventlog_hooks, platform_compat
        from kiro_crew.slack import gateway

        exited = threading.Event()
        calls: list[str] = []
        monkeypatch.setattr(eventlog_hooks, "drain_for_shutdown", lambda: calls.append("event"))
        monkeypatch.setattr(gateway, "flush_audit_queue", sel_drain(calls))
        monkeypatch.setattr(cli, "_stop_log_queue_listener", lambda timeout: calls.append("log"))

        def _exit(code):
            calls.append(f"exit:{code}:{threading.current_thread().name}")
            exited.set()

        monkeypatch.setattr(platform_compat, "hard_exit", _exit)
        # Single-shot per process: each test starts from "no force exit yet".
        monkeypatch.setattr(gateway, "_FORCE_EXIT", None)
        return gateway, calls, exited

    def test_a_repeat_signal_starts_nothing_and_keeps_the_running_exit(self, monkeypatch):
        """A third Ctrl-C, even mid-drain, must not start a second drain (which
        would find the gateway.log listener already taken and exit early) nor
        restart the deadline."""
        release = threading.Event()

        def _sel(calls):
            def _slow(timeout):
                calls.append("sel")
                release.wait(5.0)

            return _slow

        gateway, calls, exited = self._patch(monkeypatch, _sel)
        first = gateway._start_force_exit(deadline_secs=30.0)
        try:
            again = gateway._start_force_exit(deadline_secs=30.0)
            assert again is first
            drains = [t for t in threading.enumerate() if t.name == "force-exit-drain"]
            assert drains == [first[0]]
        finally:
            release.set()
            first[1].cancel()
            first[0].join(5.0)
            first[1].join(5.0)
        assert exited.is_set()
        assert [c for c in calls if c.startswith("exit")] == ["exit:0:force-exit-drain"]

    def test_drains_in_order_then_exits_off_the_calling_thread(self, monkeypatch):
        def _sel(calls):
            return lambda timeout: calls.append("sel")

        gateway, calls, exited = self._patch(monkeypatch, _sel)
        drain, deadline = gateway._start_force_exit(deadline_secs=30.0)
        try:
            assert exited.wait(5.0), "force exit never reached hard_exit"
            assert calls[:3] == ["event", "sel", "log"]
            assert calls[3] == "exit:0:force-exit-drain"
        finally:
            # Both threads must be finished before monkeypatch restores the
            # real hard_exit, or a late call would end the test worker.
            deadline.cancel()
            drain.join(5.0)
            deadline.join(5.0)
            assert not drain.is_alive() and not deadline.is_alive()

    def test_a_wedged_drain_cannot_hold_the_exit(self, monkeypatch):
        release = threading.Event()

        def _sel(calls):
            def _wedged(timeout):
                release.wait(30.0)  # ignores its own bound

            return _wedged

        gateway, calls, exited = self._patch(monkeypatch, _sel)
        drain, deadline = gateway._start_force_exit(deadline_secs=0.2)
        try:
            assert exited.wait(5.0), "the deadline did not end a wedged force exit"
            assert calls[-1] == "exit:0:force-exit-deadline"
        finally:
            # Release the drain and join both threads while hard_exit is still
            # patched (see the test above).
            release.set()
            deadline.cancel()
            drain.join(5.0)
            deadline.join(5.0)
            assert not drain.is_alive() and not deadline.is_alive()


class TestConcurrentGatewayLogDrain:
    """Two exits can drain gateway.log at once: a repeat force-exit signal, or a
    force exit racing the normal shutdown's drain. The second caller must not
    return (and hard-exit) while the first is still writing the tail."""

    @staticmethod
    def _listener(monkeypatch):
        import logging
        import queue
        from logging.handlers import QueueListener

        from kiro_crew import cli

        release = threading.Event()
        written: list[str] = []

        class _SlowHandler(logging.Handler):
            def emit(self, record):
                release.wait(5.0)
                written.append(record.getMessage())

        q: queue.Queue = queue.Queue()
        listener = QueueListener(q, _SlowHandler())
        listener.start()
        monkeypatch.setattr(cli, "_LOG_QUEUE_LISTENER", listener)
        monkeypatch.setattr(cli, "_LOG_QUEUE_STOPPING", None)
        q.put(logging.makeLogRecord({"msg": "the tail"}))
        return cli, release, written

    @pytest.mark.parametrize("first", ["force", "normal"])
    def test_the_second_drain_waits_for_the_first(self, monkeypatch, first):
        cli, release, written = self._listener(monkeypatch)

        def _first():
            if first == "force":
                cli._stop_log_queue_listener(timeout=5.0)
            else:
                asyncio.run(cli.drain_log_queue_before_hard_exit(timeout=5.0))

        a = threading.Thread(target=_first)
        a.start()
        try:
            for _ in range(200):  # until the first caller has taken the listener
                if cli._LOG_QUEUE_LISTENER is None:
                    break
                threading.Event().wait(0.01)
            assert cli._LOG_QUEUE_LISTENER is None
            b = threading.Thread(target=cli._stop_log_queue_listener, kwargs={"timeout": 5.0})
            b.start()
            b.join(0.3)
            assert b.is_alive(), "second drain returned while the tail was unwritten"
            release.set()
            b.join(5.0)
            assert not b.is_alive()
            assert written == ["the tail"]
        finally:
            release.set()
            a.join(5.0)


class TestSelFlushPrecedesGatewayLogDrain:
    """On every hard-exit path that drains both sinks, the SEL flush must come
    BEFORE the gateway.log drain -- the order ``slack/events.py`` already uses.

    The gateway.log drain stops the QueueListener. A SEL write failure logs its
    ``"SEL dropped %d events after write failures"`` line through that same
    gateway.log queue, so draining gateway.log FIRST sends that line into a dead
    queue -- a detached gateway has no console handler and ``os._exit`` throws the
    record away, leaving the audit loss with no trace. Flushing SEL first keeps
    the listener alive long enough to record a drain failure.

    Matched structurally in source order rather than by running the exits (a real
    ``os._exit`` would end the test worker): for each named function, the first
    statement line that references a SEL drain must precede the first that
    references a gateway.log drain.
    """

    _SEL_DRAINS = {"flush_audit_queue", "flush_audit_queue_before_hard_exit", "sel"}
    _LOG_DRAINS = {"_stop_log_queue_listener", "drain_log_queue_before_hard_exit"}

    # module path -> function names that drain BOTH sinks before a hard exit.
    # ``_handle_restart`` drains SEL with the inline ``sel().flush`` spelling,
    # which the ``sel`` marker in ``_SEL_DRAINS`` catches. The second-signal
    # force exit drains in ``_force_exit_drain_then_exit``, on its own thread.
    _PATHS = {
        "src/kiro_crew/slack/gateway.py": {
            "_force_exit_drain_then_exit",
            "_shutdown_and_exit",
        },
        "src/kiro_crew/platform_compat.py": {"exit_after_failed_restart_exec"},
        "src/kiro_crew/slack/events.py": {"_handle_restart"},
    }

    @staticmethod
    def _first_line_referencing(fn, names):
        """Lowest source line within ``fn``'s own body that uses any identifier
        in ``names`` (skipping nested function bodies). None if absent."""
        hits: list[int] = []

        class _Walk(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # nested def: not fn's own body
                if node is fn:
                    self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Name(self, node):
                if node.id in names:
                    hits.append(node.lineno)

            def visit_Attribute(self, node):
                if node.attr in names:
                    hits.append(node.lineno)
                self.generic_visit(node)

        _Walk().visit(fn)
        return min(hits) if hits else None

    def _functions_by_name(self, tree):
        out: dict[str, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = node
        return out

    def test_sel_drain_comes_before_the_gateway_log_drain_on_every_both_sink_path(self):
        root = Path(__file__).resolve().parents[1]
        checked = 0
        for rel, want in self._PATHS.items():
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            funcs = self._functions_by_name(tree)
            for name in want:
                assert name in funcs, f"{rel}: expected hard-exit function {name}() not found"
                fn = funcs[name]
                sel_line = self._first_line_referencing(fn, self._SEL_DRAINS)
                log_line = self._first_line_referencing(fn, self._LOG_DRAINS)
                assert sel_line is not None, f"{rel}:{name} has no SEL drain"
                assert log_line is not None, f"{rel}:{name} has no gateway.log drain"
                assert sel_line < log_line, (
                    f"{rel}:{name} drains gateway.log (line {log_line}) before SEL "
                    f"(line {sel_line}); flush SEL first so a drain-failure log line "
                    f"still reaches a live gateway.log listener"
                )
                checked += 1
        assert checked >= 4, f"expected to check every both-sink path, checked {checked}"
