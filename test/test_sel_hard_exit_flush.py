"""The SEL audit tail must survive every gateway hard exit.

SEL logging is asynchronous: :meth:`SecurityEventLog.log` enqueues onto an
unbounded queue drained by a **daemon** writer thread, and the only thing that
guarantees the queue reaches disk is a drain registered with :mod:`atexit`.

``os._exit`` runs no ``atexit`` handler and does not join daemon threads. So on
any hard-exit path that does not flush for itself, the events recorded while the
gateway was shutting down -- the last audit records before the process is gone,
and the ones an investigator reads first -- are dropped with no error and no gap
marker. The restart path has flushed for exactly this reason for a while; these
tests hold the other two hard exits to the same contract, and the ratchet at the
bottom keeps a fourth from being added without one.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
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

    def test_queued_tail_reaches_disk(self, tmp_path: Path) -> None:
        """The records enqueued immediately before a hard exit are on disk when
        the drain returns -- the whole reason for calling it."""
        log = SecurityEventLog(base_dir=tmp_path)  # async writer, as in prod
        log.log(_event("shutdown-tail"))
        flush_audit_queue()
        written = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8")
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

    def test_a_wedged_writer_cannot_hang_the_exit(self, tmp_path: Path) -> None:
        """A writer stuck on a full disk or an unreachable sink must not hold
        the process on its way out. Credit a pending event no writer will ever
        clear, so the REAL flush waits on its condition variable and only its
        own deadline can end the wait."""
        log = SecurityEventLog(base_dir=tmp_path)
        with log._pending_cond:
            log._pending = 1
        started = time.monotonic()
        try:
            flush_audit_queue(timeout=0.2)
        finally:
            with log._pending_cond:
                log._pending = 0
        assert time.monotonic() - started < 5.0

    def test_a_raising_flush_never_escapes(self, tmp_path: Path) -> None:
        """A hard exit must not be blocked, or replaced, by auditing."""
        log = SecurityEventLog(base_dir=tmp_path)

        def boom(**_kwargs):
            raise RuntimeError("writer exploded")

        log.flush = boom  # type: ignore[method-assign]
        flush_audit_queue()  # does not raise


class TestFlushAuditQueueBeforeHardExit:
    """The async wrapper, used by the two coroutine hard-exit paths."""

    @pytest.mark.asyncio
    async def test_queued_tail_reaches_disk(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path)
        log.log(_event("async-shutdown-tail"))
        await flush_audit_queue_before_hard_exit()
        written = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8")
        assert "async-shutdown-tail" in written

    @pytest.mark.asyncio
    async def test_the_blocking_drain_runs_off_the_event_loop(self, tmp_path) -> None:
        """flush() blocks on a condition variable. Doing that inline would park
        the loop for the whole deadline, which is what the executor hop buys."""
        log = SecurityEventLog(base_dir=tmp_path)
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
    async def test_a_wedged_writer_delays_neither_the_loop_nor_the_exit(self, tmp_path):
        """The outer deadline holds even when the offloaded drain does not."""
        log = SecurityEventLog(base_dir=tmp_path)
        released = threading.Event()

        def wedged(**_kwargs):
            released.wait(30.0)

        log.flush = wedged  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await flush_audit_queue_before_hard_exit(timeout=0.2)
        finally:
            released.set()
        assert loop.time() - started < 20.0


class TestEveryGatewayHardExitFlushesTheAuditQueue:
    """Ratchet: the audit log only survives if EVERY hard exit in the gateway
    process drains it. ``slack/gateway.py`` and ``slack/events.py`` are the two
    modules that call ``os._exit`` from inside the long-lived gateway process --
    the other ``os._exit`` sites in the tree (``sandbox.py``,
    ``_process_group_supervisor.py``) run in forked/pre-exec children that never
    initialize a SEL singleton.

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
    )
    _HELPERS = {"flush_audit_queue", "flush_audit_queue_before_hard_exit"}

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
        """(function node, line) for each ``os._exit(...)`` call, attributed to
        the nearest enclosing function."""
        parents: "dict[ast.AST, ast.AST]" = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        found = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_exit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                continue
            cur = parents.get(node)
            while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cur = parents.get(cur)
            if cur is not None:
                found.append((cur, node.lineno))
        return found

    def test_the_ratchet_actually_finds_the_hard_exits(self):
        """A scan that matches nothing would pass vacuously."""
        total = 0
        for path in self._MODULES:
            total += len(self._hard_exit_functions(ast.parse(path.read_text(encoding="utf-8"))))
        assert total >= 3, f"expected the known os._exit sites, found {total}"

    def test_no_hard_exit_strands_the_queued_audit_tail(self):
        violations = []
        for path in self._MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn, lineno in self._hard_exit_functions(tree):
                if not self._drains(fn):
                    violations.append(f"{path.name}:{lineno} in {fn.name}()")
        assert not violations, (
            "os._exit runs no atexit handler and does not join the SEL daemon "
            "writer, so these hard exits drop the queued audit tail; await "
            "flush_audit_queue_before_hard_exit() (or call flush_audit_queue("
            "timeout=...) from a sync handler) first: " + ", ".join(violations)
        )
