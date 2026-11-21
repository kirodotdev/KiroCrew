"""Registry checkout probes run off the event loop and keep every restore guarantee.

``_git_fetch_ref`` and ``_git_clone_or_pull`` stat the install destination before
each decision. A stat on a mount that stops answering would freeze the whole gateway
loop, so every probe runs in a worker thread. Moving them adds await points,
and an await between a move-aside and the ``try`` that restores it would let a cancel
strand the user's checkout, so these tests pin both halves: which thread runs each
probe, and where a cancel can land.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import kiro_crew.apps.registry as registry
from kiro_crew.apps.registry_pipeline import checkout, recovery
from kiro_crew.apps.registry_pipeline.subprocess_env import anonymous_git_env

_URL = "https://example.com/o/testapp.git"
_STALE_URL = "https://old.example.com/o/testapp.git"
_COMMIT = "a" * 40
_BLOCK_TIMEOUT = 10.0
_OWN_FILES = {
    os.path.realpath(checkout.__file__),
    os.path.realpath(recovery.__file__),
}


class _Recorder:
    """Records each ``Path.is_dir``/``Path.exists`` the code under test makes.

    A call counts only when its immediate caller is a checkout.py or recovery.py
    frame, so the fakes' own ``mkdir(exist_ok=True)`` and the test's asserts are
    never recorded. One call can be made to block (off the loop thread only) or to
    raise.
    """

    def __init__(self, loop_ident: int) -> None:
        self.loop_ident = loop_ident
        self.records: list[tuple[str, int, str, str]] = []
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}
        self._block_key: tuple[str, int] | None = None
        self._raise_key: tuple[str, int] | None = None
        self.release = threading.Event()
        self.entered: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.timed_out = False

    def block(self, caller: str, nth: int = 1, *, then_raise: bool = False) -> None:
        self._block_key = (caller, nth)
        if then_raise:
            self._raise_key = (caller, nth)
        self._loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()

    def fail(self, caller: str, nth: int = 1) -> None:
        self._raise_key = (caller, nth)

    def observe(self, path: Path, method: str, frame) -> None:
        if os.path.realpath(frame.f_code.co_filename) not in _OWN_FILES:
            return
        caller = frame.f_code.co_name
        ident = threading.get_ident()
        with self._lock:
            self.records.append((os.path.realpath(path), ident, caller, method))
            self._seen[caller] = self._seen.get(caller, 0) + 1
            key = (caller, self._seen[caller])
        if key == self._block_key and ident != self.loop_ident:
            assert self._loop is not None and self.entered is not None
            self._loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(
                timeout=_BLOCK_TIMEOUT
            ):  # flake-ok: bounded worker-side wait the test releases
                self.timed_out = True
        if key == self._raise_key:
            raise PermissionError(13, "probe refused", str(path))

    def calls(self, caller: str) -> list[tuple[str, int, str, str]]:
        return [r for r in self.records if r[2] == caller]


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder(threading.get_ident())
    real_is_dir, real_exists = Path.is_dir, Path.exists

    def is_dir(self, *args, **kwargs):
        rec.observe(self, "is_dir", sys._getframe(1))
        return real_is_dir(self, *args, **kwargs)

    def exists(self, *args, **kwargs):
        rec.observe(self, "exists", sys._getframe(1))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "exists", exists)
    try:
        yield rec
    finally:
        rec.release.set()


@pytest.fixture
def counts(monkeypatch):
    """Count calls to the two probe helpers, keeping the real ones as the stat callers."""
    seen = {"_is_checkout_sync": 0, "_fetch_dest_state_sync": 0}
    for name in seen:
        real = getattr(checkout, name)

        def wrapper(dest, _real=real, _name=name):
            seen[_name] += 1
            return _real(dest)

        monkeypatch.setattr(registry, name, wrapper)
    return seen


@pytest.fixture
def mkdirs(monkeypatch):
    """Record the thread of each ``_ensure_directory`` call; optionally block one."""
    state = {"idents": [], "block": False, "entered": None, "loop": None}
    release = threading.Event()
    real = checkout._ensure_directory

    def wrapper(path, *args, **kwargs):
        state["idents"].append(threading.get_ident())
        if state["block"] and threading.get_ident() != state["loop_ident"]:
            state["block"] = False
            state["loop"].call_soon_threadsafe(state["entered"].set)
            release.wait(
                timeout=_BLOCK_TIMEOUT
            )  # flake-ok: bounded worker-side wait the test releases
        return real(path, *args, **kwargs)

    state["loop_ident"] = threading.get_ident()
    state["release"] = release
    monkeypatch.setattr(registry, "_ensure_directory", wrapper)
    try:
        yield state
    finally:
        release.set()


class _Proc:
    def __init__(self, returncode: int, output: bytes = b"") -> None:
        self.returncode = returncode
        self.pid = 4242
        self._output = output

    async def communicate(self, *_args, **_kwargs):
        return self._output, b""

    async def wait(self):
        return self.returncode


class _Git:
    """A fake git: ``clone``/``init`` make the tree, ``fetch`` and ``clone`` may fail."""

    def __init__(self, dest: Path, *, fail: str = "") -> None:
        self.dest = dest
        self.fail = fail
        self.spawned: list[list[str]] = []

    async def spawn(self, *argv, **_kwargs):
        argv = [str(a) for a in argv]
        self.spawned.append(argv)
        if "clone" in argv:
            self.dest.mkdir(parents=True, exist_ok=True)
            if self.fail == "clone":
                (self.dest / "partial.pack").write_text("half", encoding="utf-8")
                return _Proc(128, b"fatal: early EOF")
            (self.dest / ".git").mkdir(exist_ok=True)
            (self.dest / "fresh.txt").write_text("new clone", encoding="utf-8")
        elif "init" in argv:
            (self.dest / ".git").mkdir(parents=True, exist_ok=True)
        elif "fetch" in argv and self.fail == "fetch":
            return _Proc(128, b"fatal: unable to access")
        return _Proc(0)


def _install_git(monkeypatch, dest: Path, **kwargs) -> _Git:
    git = _Git(dest, **kwargs)
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda _url: True)
    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kw: (list(cmd), None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: cmd)
    monkeypatch.setattr(registry, "create_subprocess_limited", git.spawn)
    monkeypatch.setattr(registry, "_resolved_clone_commit", lambda _root: _COMMIT)
    return git


def _dest(tmp_path: Path) -> Path:
    root = tmp_path / "app-sources"
    root.mkdir()
    return root / "testapp"


def _existing_checkout(dest: Path) -> None:
    (dest / ".git").mkdir(parents=True)
    (dest / ".git" / "marker").write_text("old repo", encoding="utf-8")
    (dest / "my-local-work.txt").write_text("important", encoding="utf-8")


def _siblings(dest: Path, kind: str) -> list[Path]:
    return sorted(dest.parent.glob(f"{dest.name}.{kind}-*"))


def _origin(monkeypatch, url: str, branch: str = "main") -> None:
    monkeypatch.setattr(registry, "_clone_origin_url", AsyncMock(return_value=url))
    monkeypatch.setattr(registry, "_read_clone_branch", lambda _dest: branch)


async def _clone(dest: Path, *, commit: str = "") -> dict | None:
    return await registry._git_clone_or_pull(_URL, "main", dest, [], commit=commit)


async def _fetch_commit(dest: Path) -> dict | None:
    return await registry._git_fetch_commit(
        _URL, _COMMIT, dest, [], clone_env=anonymous_git_env(), sandbox_mode="strict"
    )


def _assert_off_loop(recorder: _Recorder) -> None:
    on_loop = [r for r in recorder.records if r[1] == recorder.loop_ident]
    assert not on_loop, f"probes ran on the event loop thread: {on_loop}"
    assert not recorder.timed_out


async def _wait(awaitable, timeout: float = _BLOCK_TIMEOUT):
    return await asyncio.wait_for(awaitable, timeout)


class TestEveryProbeRunsOffTheLoop:
    """T1: which helper runs each probe, how often, and on which thread."""

    @pytest.mark.asyncio
    async def test_s1_branch_clone(self, tmp_path, monkeypatch, recorder, counts, mkdirs):
        dest = _dest(tmp_path)
        _install_git(monkeypatch, dest)

        assert await _wait(_clone(dest)) is None

        assert counts == {"_is_checkout_sync": 3, "_fetch_dest_state_sync": 0}
        assert len(recorder.calls("_is_checkout_sync")) == 3
        assert len(mkdirs["idents"]) == 1
        assert all(i != recorder.loop_ident for i in mkdirs["idents"])
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    async def test_s1_pinned_clone(self, tmp_path, monkeypatch, recorder, counts):
        dest = _dest(tmp_path)
        _install_git(monkeypatch, dest)

        assert await _wait(_clone(dest, commit=_COMMIT)) is None

        assert counts == {"_is_checkout_sync": 2, "_fetch_dest_state_sync": 1}
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    async def test_s1_fetch_ref_direct(self, tmp_path, monkeypatch, recorder, counts, mkdirs):
        dest = _dest(tmp_path)
        _install_git(monkeypatch, dest)

        assert await _wait(_fetch_commit(dest)) is None

        assert counts == {"_is_checkout_sync": 0, "_fetch_dest_state_sync": 1}
        # `.git` is not a directory, then `dest` does not exist: both stats recorded.
        assert [r[3] for r in recorder.calls("_fetch_dest_state_sync")] == ["is_dir", "exists"]
        assert len(mkdirs["idents"]) == 1
        assert all(i != recorder.loop_ident for i in mkdirs["idents"])
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    async def test_s2_update_in_place(self, tmp_path, monkeypatch, recorder, counts):
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        git = _install_git(monkeypatch, dest)
        _origin(monkeypatch, _URL)

        assert await _wait(_clone(dest)) is None

        assert counts == {"_is_checkout_sync": 3, "_fetch_dest_state_sync": 0}
        assert any("pull" in argv for argv in git.spawned)
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("git_file", [False, True], ids=["plain-dir", "git-file"])
    async def test_s3_refusal(self, tmp_path, monkeypatch, recorder, counts, git_file):
        dest = _dest(tmp_path)
        dest.mkdir()
        if git_file:
            (dest / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        _install_git(monkeypatch, dest)

        result = await _wait(_fetch_commit(dest))

        assert result is not None and result["code"] == "destination_not_a_checkout"
        assert counts == {"_is_checkout_sync": 0, "_fetch_dest_state_sync": 1}
        assert dest.exists()
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    async def test_s4_origin_mismatch(self, tmp_path, monkeypatch, recorder, counts):
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        _install_git(monkeypatch, dest)
        _origin(monkeypatch, _STALE_URL)

        assert await _wait(_clone(dest)) is None

        assert counts == {"_is_checkout_sync": 1, "_fetch_dest_state_sync": 0}
        assert len(_siblings(dest, "stale")) == 1
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    async def test_s5_branch_drift(self, tmp_path, monkeypatch, recorder, counts):
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        _install_git(monkeypatch, dest)
        _origin(monkeypatch, _URL, branch="other")

        assert await _wait(_clone(dest)) is None

        assert counts == {"_is_checkout_sync": 2, "_fetch_dest_state_sync": 0}
        assert len(_siblings(dest, "stale")) == 1
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    async def test_s6_pinned_reinstall(self, tmp_path, monkeypatch, recorder, counts):
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        _install_git(monkeypatch, dest)
        _origin(monkeypatch, _URL)

        assert await _wait(_clone(dest, commit=_COMMIT)) is None

        assert counts == {"_is_checkout_sync": 2, "_fetch_dest_state_sync": 1}
        _assert_off_loop(recorder)

    @pytest.mark.asyncio
    async def test_s7_failed_fetch_restores(self, tmp_path, monkeypatch, recorder, counts):
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        _install_git(monkeypatch, dest, fail="fetch")
        _origin(monkeypatch, _URL)

        result = await _wait(_clone(dest, commit=_COMMIT))

        assert result is not None and result["ok"] is False
        assert counts == {"_is_checkout_sync": 2, "_fetch_dest_state_sync": 1}
        assert len(recorder.calls("_exists_sync")) == 1
        assert (dest / "my-local-work.txt").read_text(encoding="utf-8") == "important"
        assert not _siblings(dest, "stale")
        _assert_off_loop(recorder)


class TestTheLoopStaysFree:
    """T2: a probe stuck in its worker leaves other coroutines running."""

    @pytest.mark.asyncio
    async def test_other_work_completes_while_a_probe_blocks(self, tmp_path, monkeypatch, recorder):
        dest = _dest(tmp_path)
        _install_git(monkeypatch, dest)
        recorder.block("_is_checkout_sync", 1)
        task = asyncio.create_task(_clone(dest))
        try:
            await _wait(recorder.entered.wait())
            ran = asyncio.Event()

            async def other():
                await asyncio.sleep(0)
                ran.set()

            await _wait(other())
            assert ran.is_set() and not task.done()
        finally:
            recorder.release.set()
        assert await _wait(task) is None
        _assert_off_loop(recorder)


class TestACancelBeforeAnythingIsOwed:
    """T3: a cancel at each new await before a move-aside or a create leaves no trace."""

    async def _cancel_while_blocked(self, task, entered, release, git):
        try:
            await _wait(entered.wait())
            spawned_before = len(git.spawned)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await _wait(task)
        return spawned_before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("nth", [1, 2, 3], ids=["928", "1018", "1082"])
    async def test_s2_probes(self, tmp_path, monkeypatch, recorder, nth):
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        git = _install_git(monkeypatch, dest)
        _origin(monkeypatch, _URL)
        recorder.block("_is_checkout_sync", nth)

        before = await self._cancel_while_blocked(
            asyncio.create_task(_clone(dest)), recorder.entered, recorder.release, git
        )

        assert (dest / "my-local-work.txt").read_text(encoding="utf-8") == "important"
        assert not _siblings(dest, "stale")
        assert len(git.spawned) == before == 0

    @pytest.mark.asyncio
    async def test_s1_origin_probe(self, tmp_path, monkeypatch, recorder):
        dest = _dest(tmp_path)
        git = _install_git(monkeypatch, dest)
        recorder.block("_is_checkout_sync", 1)

        await self._cancel_while_blocked(
            asyncio.create_task(_clone(dest)), recorder.entered, recorder.release, git
        )

        assert not dest.exists() and not git.spawned

    @pytest.mark.asyncio
    async def test_s1_fetch_ref_probe(self, tmp_path, monkeypatch, recorder):
        dest = _dest(tmp_path)
        git = _install_git(monkeypatch, dest)
        recorder.block("_fetch_dest_state_sync", 1)

        await self._cancel_while_blocked(
            asyncio.create_task(_fetch_commit(dest)), recorder.entered, recorder.release, git
        )

        assert not dest.exists() and not git.spawned

    @pytest.mark.asyncio
    async def test_s1_fetch_ref_mkdir(self, tmp_path, monkeypatch, mkdirs):
        dest = _dest(tmp_path)
        git = _install_git(monkeypatch, dest)
        mkdirs.update(block=True, entered=asyncio.Event(), loop=asyncio.get_running_loop())

        await self._cancel_while_blocked(
            asyncio.create_task(_fetch_commit(dest)), mkdirs["entered"], mkdirs["release"], git
        )

        assert not dest.exists() and not git.spawned


class TestACancelRightAfterAMoveAsideRestores:
    """T4: no await runs between a move-aside and the ``try`` that restores it."""

    def _cancel_on_return(self, monkeypatch, *, await_in_window: bool = False):
        real = checkout._move_checkout_aside

        async def wrapper(dest, log_lines):
            aside = await real(dest, log_lines)
            if aside is not None:
                asyncio.current_task().cancel()
                if await_in_window:
                    await asyncio.sleep(0)
            return aside

        monkeypatch.setattr(registry, "_move_checkout_aside", wrapper)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("origin", "branch", "commit"),
        [(_STALE_URL, "main", ""), (_URL, "other", ""), (_URL, "main", _COMMIT)],
        ids=["s4-origin-mismatch", "s5-branch-drift", "s6-pinned"],
    )
    async def test_the_checkout_is_back(self, tmp_path, monkeypatch, origin, branch, commit):
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        _install_git(monkeypatch, dest)
        _origin(monkeypatch, origin, branch)
        self._cancel_on_return(monkeypatch)

        with pytest.raises(asyncio.CancelledError):
            await _wait(_clone(dest, commit=commit))

        assert (dest / ".git" / "marker").read_text(encoding="utf-8") == "old repo"
        assert (dest / "my-local-work.txt").read_text(encoding="utf-8") == "important"
        assert not _siblings(dest, "stale")

    @pytest.mark.asyncio
    async def test_an_await_in_the_window_would_strand_it(self, tmp_path, monkeypatch):
        """Self-check: the same cancel with one await before the ``try`` strands the tree."""
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        _install_git(monkeypatch, dest)
        _origin(monkeypatch, _STALE_URL)
        self._cancel_on_return(monkeypatch, await_in_window=True)

        with pytest.raises(asyncio.CancelledError):
            await _wait(_clone(dest))

        assert not dest.exists()
        assert len(_siblings(dest, "stale")) == 1


class TestTheFinallyProbeHoldsACancel:
    """T5 and T6: the probe after a failed clone, under a cancel and an error."""

    def _failing_clone_after_move_aside(self, tmp_path, monkeypatch) -> Path:
        dest = _dest(tmp_path)
        _existing_checkout(dest)
        _install_git(monkeypatch, dest, fail="clone")
        _origin(monkeypatch, _STALE_URL)

        async def leave_in_place(_path):
            return None

        monkeypatch.setattr(registry, "_rmtree_force_settled", leave_in_place)
        return dest

    @pytest.mark.asyncio
    async def test_a_cancel_waits_for_the_restore(self, tmp_path, monkeypatch, recorder):
        dest = self._failing_clone_after_move_aside(tmp_path, monkeypatch)
        recorder.block("_exists_sync", 1)
        task = asyncio.create_task(_clone(dest))
        try:
            await _wait(recorder.entered.wait())
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            recorder.release.set()

        with pytest.raises(asyncio.CancelledError):
            await _wait(task)

        assert (dest / "my-local-work.txt").read_text(encoding="utf-8") == "important"
        partials = _siblings(dest, "partial")
        assert len(partials) == 1 and (partials[0] / "partial.pack").exists()
        assert not _siblings(dest, "stale")

    @pytest.mark.asyncio
    async def test_a_cancel_of_every_task_still_restores(self, tmp_path, monkeypatch, recorder):
        """A teardown sweep cancels every task, so the probe's worker must not be one."""
        dest = self._failing_clone_after_move_aside(tmp_path, monkeypatch)
        recorder.block("_exists_sync", 1)
        task = asyncio.create_task(_clone(dest))
        me = asyncio.current_task()
        try:
            await _wait(recorder.entered.wait())
            for other in asyncio.all_tasks():
                if other is not me:
                    other.cancel()
            await asyncio.sleep(0)
        finally:
            recorder.release.set()

        with pytest.raises(asyncio.CancelledError):
            await _wait(task)

        assert (dest / "my-local-work.txt").read_text(encoding="utf-8") == "important"
        assert not _siblings(dest, "stale")

    @pytest.mark.asyncio
    async def test_a_probe_error_propagates(self, tmp_path, monkeypatch, recorder):
        dest = self._failing_clone_after_move_aside(tmp_path, monkeypatch)
        recorder.fail("_exists_sync", 1)

        with pytest.raises(PermissionError):
            await _wait(_clone(dest))

    @pytest.mark.asyncio
    async def test_a_probe_error_under_a_cancel_stays_a_cancel(
        self, tmp_path, monkeypatch, recorder
    ):
        dest = self._failing_clone_after_move_aside(tmp_path, monkeypatch)
        recorder.block("_exists_sync", 1, then_raise=True)
        task = asyncio.create_task(_clone(dest))
        try:
            await _wait(recorder.entered.wait())
            task.cancel()
        finally:
            recorder.release.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await _wait(task)
        assert isinstance(caught.value.__cause__, PermissionError)


class TestProbeErrorsPropagate:
    """T6 at R2 and R3: a probe's ``OSError`` reaches the caller unchanged."""

    @pytest.mark.asyncio
    async def test_fetch_ref_probe(self, tmp_path, monkeypatch, recorder):
        dest = _dest(tmp_path)
        _install_git(monkeypatch, dest)
        recorder.fail("_fetch_dest_state_sync", 1)

        with pytest.raises(PermissionError):
            await _wait(_fetch_commit(dest))

    @pytest.mark.asyncio
    async def test_origin_probe(self, tmp_path, monkeypatch, recorder):
        dest = _dest(tmp_path)
        _install_git(monkeypatch, dest)
        recorder.fail("_is_checkout_sync", 1)

        with pytest.raises(PermissionError):
            await _wait(_clone(dest))


def _cancel_handler(tree: ast.Module) -> ast.ExceptHandler:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_move_checkout_aside":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.ExceptHandler)
                    and inner.type is not None
                    and ast.unparse(inner.type) == "asyncio.CancelledError"
                ):
                    return inner
    raise AssertionError("_move_checkout_aside has no CancelledError handler")


def _handler_problems(source: str) -> list[str]:
    handler = _cancel_handler(ast.parse(source))
    loops = [
        i
        for i, stmt in enumerate(handler.body)
        if isinstance(stmt, ast.While) and ast.unparse(stmt.test) == "not worker.done()"
    ]
    if len(loops) != 1:
        return ["no single `while not worker.done():` settle loop"]
    after = handler.body[loops[0] + 1 :]
    problems = []
    suspending = (ast.Await, ast.AsyncWith, ast.AsyncFor)
    for stmt in after:
        for node in ast.walk(stmt):
            if isinstance(node, suspending):
                problems.append(f"(a) {type(node).__name__} at line {node.lineno}")
    awaited = {
        id(n)
        for stmt in handler.body
        for a in ast.walk(stmt)
        if isinstance(a, ast.Await)
        for n in ast.walk(a)
    }
    stats = [
        n
        for stmt in handler.body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call)
        and ast.unparse(n.func) == "aside.exists"
        and not n.args
        and id(n) not in awaited
    ]
    if not stats:
        problems.append("(b) no synchronous aside.exists() call")
    lines = source.splitlines()
    for call in stats:
        _code, sep, comment = lines[call.lineno - 1].partition("# on-loop-io-ok:")
        if not sep or not comment.strip():
            problems.append(f"(c) no on-loop-io-ok reason on line {call.lineno}")
    return problems


class TestTheMoveAsideCancelHandlerNeverAwaitsAfterSettling:
    """T8: an await after the settle loop would let a repeated cancel skip the undo.

    Structural, because no timing of cancels lands one reliably between the worker
    finishing and the handler's stat: the cancellation-safety tests in
    ``test_external_registry_move_aside.py`` pass with the stat awaited.
    """

    def test_the_handler_is_await_free_after_the_loop(self):
        source = Path(recovery.__file__).read_text(encoding="utf-8")
        assert _handler_problems(source) == []

    @pytest.mark.parametrize(
        "rewrite",
        [
            lambda line: line.replace(
                "if aside.exists():", "if await asyncio.to_thread(aside.exists):"
            ),
            lambda line: line.replace(
                "if aside.exists():", "await asyncio.sleep(0)\n        if aside.exists():"
            ),
        ],
        ids=["awaited-stat", "await-before-stat"],
    )
    def test_the_check_fails_on_an_await_after_the_loop(self, rewrite: Callable[[str], str]):
        source = Path(recovery.__file__).read_text(encoding="utf-8")
        target = next(line for line in source.splitlines() if "if aside.exists():" in line)
        mutated = source.replace(target, rewrite(target), 1)
        assert mutated != source
        assert any(p.startswith("(a)") for p in _handler_problems(mutated))
