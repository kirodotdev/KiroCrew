"""Tests for ``shielded_prepare_off_loop`` — the shared shield over the chokepoint.

Cancel-injection test: verifies that cancelling the awaiting coroutine while the
worker is still inside ``sandboxed_spawn_argv`` recovers the cleanup path and
unlinks the temp launcher/profile, rather than leaking it.

Executor test: the caller's pool is honoured, so consolidating the shield does
not silently collapse each site's ``executors.py`` pool onto the default one.

AST guard test: scans ``src/kiro_crew/`` for bare ``run_in_executor`` AND
``asyncio.to_thread`` hops referencing ``sandboxed_spawn_argv`` (the sync
version) without going through the shared shield. Prevents regressions.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the source tree is importable without pip install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from source_corpus import parsed_candidates, src_root  # noqa: E402

from kiro_crew.sandbox import (  # noqa: E402
    sandboxed_spawn_argv_async,
    shielded_prepare_off_loop,
    wrap_argv,
    wrap_argv_async,
)


def _prepare(argv: list[str]):
    """Bind the chokepoint the way every production call site does."""
    from kiro_crew import sandbox

    return functools.partial(sandbox.sandboxed_spawn_argv, argv)


class TestSandboxOffLoopCancellation:
    """Cancellation during the worker hop must not orphan the temp launcher."""

    @staticmethod
    def _blocking_sandbox(tmp_path):
        """A ``sandboxed_spawn_argv`` stub that parks the worker until released.

        Reproduces the race deterministically: the awaiting coroutine is
        cancelled while the thread is still inside the chokepoint, and the
        launcher only comes into being AFTER the cancellation landed.
        """
        entered = threading.Event()
        release = threading.Event()
        created: list[Path] = []

        def _fake(argv, mode="standard", **_kwargs):
            entered.set()
            assert release.wait(timeout=10), "test never released the worker"
            launcher = tmp_path / f"sb-launcher-{len(created)}"
            launcher.write_text("# fake sandbox launcher/profile")
            created.append(launcher)
            return argv, {}, str(launcher)

        return _fake, entered, release, created

    @pytest.mark.asyncio
    async def test_cancel_during_hop_drops_launcher(self, tmp_path):
        fake, entered, release, created = self._blocking_sandbox(tmp_path)
        with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=fake):
            task = asyncio.create_task(shielded_prepare_off_loop(_prepare(["/bin/true"])))
            # Wait for the worker to enter the stub (implies awaiter is
            # suspended at the shielded hop).
            await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        # The launcher was created by the worker thread and should have been
        # unlinked by the recovery path in the CancelledError handler.
        assert created, "stub was never called"
        assert not any(f.exists() for f in created), "launcher was NOT cleaned up on cancellation"

    @pytest.mark.asyncio
    async def test_cancel_on_a_caller_pool_still_drops_launcher(self, tmp_path):
        """The recovery works on a caller-supplied pool, not just the default one."""
        fake, entered, release, created = self._blocking_sandbox(tmp_path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=fake):
                task = asyncio.create_task(
                    shielded_prepare_off_loop(_prepare(["/bin/true"]), executor=pool)
                )
                await asyncio.to_thread(entered.wait, 10)
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert created, "stub was never called"
        assert not any(f.exists() for f in created), "launcher was NOT cleaned up on cancellation"

    @pytest.mark.asyncio
    async def test_the_callers_executor_is_the_one_used(self):
        """Pool choice stays with the caller — the whole point of the parameter.

        Pinned by the worker thread's identity: the preparation must run on a
        thread belonging to the pool that was passed in, not on the loop's
        default executor.
        """
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pinned-pool") as pool:
            names: list[str] = []

            def _fake(argv, mode="standard", **_kwargs):
                names.append(threading.current_thread().name)
                return argv, {}, None

            with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=_fake):
                await shielded_prepare_off_loop(_prepare(["/bin/true"]), executor=pool)
        assert names and names[0].startswith("pinned-pool"), names

    @pytest.mark.asyncio
    async def test_normal_return_passes_through(self, tmp_path):
        """Non-cancelled calls pass through the chokepoint result unchanged."""
        launcher = tmp_path / "sb-launcher"
        launcher.write_text("# fake")

        def _fake(argv, mode="standard", **_kwargs):
            return ["wrapped"] + argv, {"PATH": "/usr/bin"}, str(launcher)

        with patch("kiro_crew.sandbox.sandboxed_spawn_argv", side_effect=_fake):
            result = await shielded_prepare_off_loop(_prepare(["/bin/true"]))
        assert result == (
            ["wrapped", "/bin/true"],
            {"PATH": "/usr/bin"},
            str(launcher),
        )

    @pytest.mark.asyncio
    async def test_wrap_argv_async_runs_on_worker(self):
        loop_thread = threading.get_ident()
        worker_threads: list[int] = []

        def _fake(argv, mode="auto", **_kwargs):
            worker_threads.append(threading.get_ident())
            return ["wrapped", *argv], None

        result = await wrap_argv_async(["/bin/true"], _prepare=_fake)

        assert result == (["wrapped", "/bin/true"], None)
        assert worker_threads and worker_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_sandboxed_spawn_async_runs_on_worker(self):
        loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        prepared_env = {"PATH": "/usr/bin"}

        def _fake(argv, *, env):
            worker_threads.append(threading.get_ident())
            return ["wrapped", *argv], env, None

        result = await sandboxed_spawn_argv_async(["/bin/true"], env=prepared_env, _prepare=_fake)

        assert result == (["wrapped", "/bin/true"], prepared_env, None)
        assert worker_threads and worker_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_sync_wrap_rejects_event_loop(self):
        with pytest.raises(RuntimeError, match="await wrap_argv_async"):
            wrap_argv(["/bin/true"], mode="off")


class TestNoBareSandboxedSpawnArgvHops:
    """AST guard: no async code may hop to the sync chokepoint without a shield.

    Every async caller must go through ``shielded_prepare_off_loop`` so a
    cancelled awaiter cannot abandon the worker's tuple and leak the launcher.

    Two things this guard has to get right, or it passes vacuously:

    * BOTH spellings of a bare hop -- ``loop.run_in_executor(pool, fn, ...)``
      and ``asyncio.to_thread(fn, ...)``. The private copies this PR collapsed
      used the second one, so covering only the first closes half the class.
    * ONE LEVEL OF INDIRECTION. Most sites do not hand the chokepoint to the
      hop directly; they hand it a module-level preparer that calls it
      (``_prepare_probe``, ``_sandboxed``, ``_prepare_git_spawn``), whose name
      says nothing about the chokepoint. A guard matching only the literal
      ``sandboxed_spawn_argv`` at the hop site is blind to exactly the sites
      this PR had to fix, so per file we first collect the functions that call
      the chokepoint and treat a hop to any of them as a hop to it.

    There is deliberately no exemption list: a site that unlinks in its own
    ``finally`` is NOT covered, because a cancellation at the hop never binds
    the tuple, so that ``finally`` sees no cleanup path to remove.
    """

    _CHOKEPOINT = "sandboxed_spawn_argv"

    #: Literals a bare hop needs before its AST shape can possibly match: the
    #: chokepoint's own name (``reaching`` is computed per FILE, so a hop can
    #: only be handed a reaching name if this module spells the chokepoint) and
    #: one of the two hop spellings.
    _REQUIRE_ALL = ("sandboxed_spawn_argv",)
    _REQUIRE_ANY = ("run_in_executor", "to_thread")

    @staticmethod
    def _src_root() -> Path:
        return src_root()

    @classmethod
    def _names_that_reach_the_chokepoint(cls, tree: ast.Module) -> set[str]:
        """Names a hop may be handed that end up calling the chokepoint.

        The chokepoint itself, plus every function in this module whose body
        calls it — the preparer indirection every real call site uses.
        """
        reaching = {cls._CHOKEPOINT}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                fn = child.func
                hits = (isinstance(fn, ast.Name) and fn.id == cls._CHOKEPOINT) or (
                    isinstance(fn, ast.Attribute) and fn.attr == cls._CHOKEPOINT
                )
                if hits:
                    reaching.add(node.name)
                    break
        return reaching

    @staticmethod
    def _is_bare_hop_call(node: ast.Call) -> bool:
        """Return True for a ``*.run_in_executor(...)`` or ``*.to_thread(...)`` call."""
        func = node.func
        return isinstance(func, ast.Attribute) and func.attr in {
            "run_in_executor",
            "to_thread",
        }

    @staticmethod
    def _references_any(node: ast.Call, names: set[str]) -> str | None:
        """The first of ``names`` referenced anywhere in the call's arguments.

        Walks each argument, so a ``functools.partial(fn, ...)`` wrapper and a
        plain reference are both caught.
        """
        for arg in [*node.args, *[kw.value for kw in node.keywords]]:
            for child in ast.walk(arg):
                if isinstance(child, ast.Name) and child.id in names:
                    return child.id
                if isinstance(child, ast.Attribute) and child.attr in names:
                    return child.attr
        return None

    def test_no_bare_hops_to_sandboxed_spawn_argv(self):
        violations: list[str] = []
        for path, _text, tree in parsed_candidates(self._REQUIRE_ALL, self._REQUIRE_ANY):
            rel = path.relative_to(self._src_root())
            reaching = self._names_that_reach_the_chokepoint(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not self._is_bare_hop_call(node):
                    continue
                hit = self._references_any(node, reaching)
                if hit is not None:
                    violations.append(f"{rel}:{node.lineno}: bare hop to {hit}")
        assert not violations, (
            "Found bare run_in_executor / to_thread hops that reach sandboxed_spawn_argv "
            "(must go through sandbox.shielded_prepare_off_loop):\n" + "\n".join(violations)
        )

    def test_async_code_never_calls_sync_sandbox_chokepoints(self):
        violations: list[str] = []
        sync_chokepoints = {"wrap_argv", "sandboxed_spawn_argv"}

        class Visitor(ast.NodeVisitor):
            def __init__(self, path: Path):
                self.path = path
                self.in_async = 0

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self.in_async += 1
                self.generic_visit(node)
                self.in_async -= 1

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                if self.in_async:
                    return
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                if self.in_async:
                    fn = node.func
                    name = (
                        fn.id
                        if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else ""
                    )
                    if name in sync_chokepoints:
                        rel = self.path.relative_to(TestNoBareSandboxedSpawnArgvHops._src_root())
                        violations.append(f"{rel}:{node.lineno}: direct call to {name}")
                self.generic_visit(node)

        for path, _text, tree in parsed_candidates(
            require_any=("wrap_argv", "sandboxed_spawn_argv")
        ):
            Visitor(path).visit(tree)

        assert not violations, (
            "Async code called a blocking sandbox chokepoint directly; use the "
            "corresponding *_async helper:\n" + "\n".join(violations)
        )
