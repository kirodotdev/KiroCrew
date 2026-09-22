"""Tests for the subprocess-pool executor and its realpath child."""

from __future__ import annotations

import ast
import io
import os
import pathlib
import struct
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from kiro_crew.security.paths import _resolved_spellings
from kiro_crew.subprocess_pool import _child_realpath as child_mod
from kiro_crew.subprocess_pool.executor import (
    OP_REALPATH_SPELLINGS,
    SubprocessPoolExecutor,
    SubprocessPoolUnavailable,
    pack_strings,
    unpack_strings,
)

_CHILD = pathlib.Path(sys.modules["kiro_crew.subprocess_pool.executor"].__file__).with_name(
    "_child_realpath.py"
)

# Windows rejects a newline in a filename outright and stores names as UTF-16, so a
# byte sequence that is not valid UTF-8 cannot round-trip through one. The framing
# that handles both is tested directly in TestFraming, which is pure-function and
# runs everywhere; what cannot run off POSIX is creating such a file to compare the
# child against the thread.
posix_names_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX filename semantics: newline and non-UTF-8 names"
)

# A newline is a legal APFS/HFS+ name unit, but a byte sequence that is not valid
# UTF-8 is not: ``os.mkdir`` on macOS refuses it with EILSEQ before the resolver is
# ever reached, so that ONE case is skipped there as well (the same skip every other
# non-UTF-8 path test in this suite carries).
utf8_relaxed_names_only = pytest.mark.skipif(
    os.name == "nt" or sys.platform == "darwin",
    reason="non-UTF-8 bytes are not legal NTFS or APFS/HFS+ name units",
)

# The per-request read deadline is armed with select, which on Windows accepts only
# sockets and so cannot watch the child's pipe. There the ceiling reaper bounds a
# wedge instead, so these three assert a POSIX capability rather than shared
# behaviour.
posix_read_deadline_only = pytest.mark.skipif(
    os.name == "nt", reason="select cannot watch a pipe on Windows; the reaper bounds it there"
)


@pytest.fixture
def pool():
    executor = SubprocessPoolExecutor(workers=2)
    try:
        yield executor
    finally:
        executor.shutdown(wait=False)


def _request_frame(request_id: int, op: int, payload: bytes) -> bytes:
    body = struct.pack(">IB", request_id, op) + payload
    return struct.pack(">I", len(body)) + body


def _first_response_ok(stdout: bytes) -> bool:
    (length,) = struct.unpack_from(">I", stdout, 0)
    body = stdout[4 : 4 + length]
    return body[4] == 0 and bool(unpack_strings(body[5:]))


class TestFraming:
    """Length prefixes, because a POSIX filename may contain a newline."""

    @pytest.mark.parametrize(
        "values",
        [
            [],
            [b""],
            [b"/plain/path"],
            [b"/two\nline/name", b"/second"],
            [b"/trailing\n"],
            [b"\n"],
            [b"/non\xffutf8/name"],
            [b"/a" * 1000],
            [b"/a\nb", b"", b"/c\n\nd", b"\xff\n\xfe"],
        ],
    )
    def test_protocol_round_trips(self, values: list[bytes]) -> None:
        assert unpack_strings(pack_strings(values)) == values

    def test_a_newline_in_a_name_does_not_split_one_value_into_two(self) -> None:
        # The regression this framing exists to prevent: newline framing would
        # read b"/a\nb" as two values and shift every later answer by one.
        assert unpack_strings(pack_strings([b"/a\nb"])) == [b"/a\nb"]

    @pytest.mark.parametrize("payload", [b"", b"\x00\x00\x00\x01", b"\x00\x00\x00\x01\x00\x00"])
    def test_a_truncated_frame_is_rejected_not_guessed(self, payload: bytes) -> None:
        with pytest.raises(ValueError):
            unpack_strings(payload)


class TestChildIsolation:
    """The child must never import kiro_crew: the cold-start budget depends on it."""

    def test_the_child_imports_stdlib_only(self) -> None:
        tree = ast.parse(_CHILD.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    pytest.fail("the child must not use a relative import")
                if node.module:
                    imported.add(node.module.split(".")[0])
        assert "kiro_crew" not in imported
        assert imported <= {"__future__", "os", "pathlib", "struct", "sys"}

    def test_the_child_answers_without_site_packages(self, tmp_path: pathlib.Path) -> None:
        # Launched exactly as the pool launches it, with -S. If the child ever
        # needed an installed package this would fail rather than silently
        # regressing start-up to a dependency-graph walk.
        target = tmp_path / "real"
        target.mkdir()
        proc = subprocess.run(
            [sys.executable, "-S", str(_CHILD)],
            input=_request_frame(1, OP_REALPATH_SPELLINGS, os.fsencode(str(target))),
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == 0
        assert _first_response_ok(proc.stdout)


class TestResolutionParity:
    """The child must answer what the thread it replaces answered."""

    def test_a_symlink_resolves_to_its_target(self, pool, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        assert pool.realpath_spellings(str(link)) == _resolved_spellings(str(link))
        assert str(target.resolve()) in pool.realpath_spellings(str(link))

    @posix_names_only
    def test_a_name_containing_a_newline_resolves(self, pool, tmp_path: pathlib.Path) -> None:
        odd = tmp_path / "two\nline"
        odd.mkdir()
        assert pool.realpath_spellings(str(odd)) == _resolved_spellings(str(odd))

    @utf8_relaxed_names_only
    def test_a_name_that_is_not_utf8_round_trips(self, pool, tmp_path: pathlib.Path) -> None:
        raw = os.path.join(os.fsencode(str(tmp_path)), b"not\xffutf8")
        os.mkdir(raw)
        name = os.fsdecode(raw)
        assert pool.realpath_spellings(name) == _resolved_spellings(name)

    def test_a_missing_path_resolves_lexically_like_the_thread_did(
        self, pool, tmp_path: pathlib.Path
    ) -> None:
        missing = str(tmp_path / "nope" / "deeper")
        assert pool.realpath_spellings(missing) == _resolved_spellings(missing)

    def test_a_symlink_repointed_after_a_first_answer_is_not_served_stale(
        self, pool, tmp_path: pathlib.Path
    ) -> None:
        # The child holds no cache of its own. A second question about the same
        # spelling must see the new target, or the gate could be answered from a
        # pre-repoint reading.
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        link = tmp_path / "link"
        link.symlink_to(first)
        assert str(first.resolve()) in pool.realpath_spellings(str(link))
        link.unlink()
        link.symlink_to(second)
        answer = pool.realpath_spellings(str(link))
        assert str(second.resolve()) in answer
        assert str(first.resolve()) not in answer


class TestRelativePathsResolveLikeTheParent:
    """A relative path must resolve against the CALLER's directory, not the child's.

    ``realpath`` anchors a relative path to the working directory, so a child with
    its own CWD would answer a different question than the in-process resolver it
    replaces. On a sensitive-path gate that direction of error fails OPEN: the
    resolved form lands somewhere harmless and never matches the denylist.
    """

    def test_a_relative_path_matches_what_the_thread_answers(
        self, pool, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        target = tmp_path / "target"
        target.mkdir()
        monkeypatch.chdir(tmp_path)
        assert pool.realpath_spellings("target") == _resolved_spellings("target")

    def test_a_relative_symlink_still_resolves_to_its_real_target(
        self, pool, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        # The fail-open shape: were the relative name anchored to the package
        # directory instead of here, this would resolve under the package and the
        # caller's denylist comparison would simply not match.
        secret = tmp_path / "credential-store"
        secret.mkdir()
        link = tmp_path / "link"
        link.symlink_to(secret)
        monkeypatch.chdir(tmp_path)
        answer = pool.realpath_spellings("link")
        assert str(secret.resolve()) in answer
        package_dir = str(pathlib.Path(_CHILD).parent)
        assert not any(spelling.startswith(package_dir) for spelling in answer)

    def test_a_dot_relative_spelling_resolves_here(
        self, pool, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        target = tmp_path / "nested" / "leaf"
        target.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        assert pool.realpath_spellings("./nested/leaf") == _resolved_spellings("./nested/leaf")

    def test_a_relative_path_is_anchored_after_the_caller_changes_directory(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        """The case inheriting the CWD does NOT cover, so the one that pins the fix.

        A child is long-lived and its CWD is whatever it inherited when it spawned.
        Once the caller chdirs, that inherited CWD is stale, so only absolutizing in
        the parent keeps a relative path anchored where the caller means.

        It takes ``workers=1`` to see this. The tests above cannot: they chdir before
        the first request, so the child inherits the new directory; and with the
        default two children the second request is served by the one that has not
        spawned yet, which starts in the new directory too. Both mask the bug.
        """
        executor = SubprocessPoolExecutor(workers=1)
        try:
            before = tmp_path / "before"
            before.mkdir()
            monkeypatch.chdir(before)
            executor.realpath_spellings(str(before))  # the single child spawns HERE

            after = tmp_path / "after"
            (after / "leaf").mkdir(parents=True)
            monkeypatch.chdir(after)
            answer = executor.realpath_spellings("leaf")
            assert answer == _resolved_spellings("leaf")
            assert str((after / "leaf").resolve()) in answer
        finally:
            executor.shutdown(wait=False)


class TestFailClosed:
    """A child that cannot answer must raise, never return an empty answer."""

    def test_a_killed_child_is_respawned_and_never_answers_empty(
        self, pool, tmp_path: pathlib.Path
    ) -> None:
        target = tmp_path / "real"
        target.mkdir()
        assert pool.realpath_spellings(str(target))  # spawn it
        for child in pool._children:
            proc = child.proc
            if proc is not None:
                proc.kill()
                proc.wait(timeout=30)
        # Restart-on-demand: the next call respawns and answers, and at no point
        # is an empty set returned for a path that does resolve.
        assert pool.realpath_spellings(str(target))

    def test_a_child_that_never_answers_is_killed_at_the_ceiling(
        self, tmp_path: pathlib.Path
    ) -> None:
        script = tmp_path / "hang.py"
        script.write_text(
            textwrap.dedent("""
                import sys, time
                sys.stdin.buffer.read(4)
                time.sleep(3600)
                """),
            encoding="utf-8",
        )
        executor = SubprocessPoolExecutor(workers=2, script=str(script), ceiling_secs=1.0)
        try:
            with pytest.raises(SubprocessPoolUnavailable):
                executor.submit_op(OP_REALPATH_SPELLINGS, b"/x").result(timeout=60)
        finally:
            executor.shutdown(wait=False)

    def test_an_unknown_op_raises_unavailable(self, pool) -> None:
        with pytest.raises(SubprocessPoolUnavailable):
            pool.submit_op(99, b"/x").result(timeout=60)

    def test_an_error_response_carries_no_path(self, pool, tmp_path: pathlib.Path) -> None:
        secret = str(tmp_path / "credential-store-name")
        with pytest.raises(SubprocessPoolUnavailable) as caught:
            pool.submit_op(99, os.fsencode(secret)).result(timeout=60)
        assert "credential-store-name" not in str(caught.value)


class TestCallerThreadDispatch:
    """The transport must stay on the thread that wants the answer.

    This is the regression that matters most here, because it is invisible to every
    other test: a variant that routes child work through a
    ``ThreadPoolExecutor`` passes all of them while being 33x slower under load.
    Measured with the same child and 48 pure-Python contenders, 81 ms on the
    calling thread against 2681 ms through a pool -- past the 2000 ms resolver
    budget, so the pooled shape does not fix the bug at all.
    """

    def _record_threads(self, pool, monkeypatch) -> list[int]:
        seen: list[int] = []
        child_type = type(pool._children[0])
        original = child_type.request

        def _recording(child, op, payload, deadline=None):
            seen.append(threading.get_ident())
            return original(child, op, payload, deadline)

        monkeypatch.setattr(child_type, "request", _recording)
        return seen

    def test_the_convenience_call_runs_the_transport_on_the_calling_thread(
        self, pool, monkeypatch, tmp_path: pathlib.Path
    ) -> None:
        seen = self._record_threads(pool, monkeypatch)
        pool.realpath_spellings(str(tmp_path))
        assert seen == [threading.get_ident()]

    def test_an_awaited_future_runs_the_transport_on_the_awaiting_thread(
        self, pool, monkeypatch, tmp_path: pathlib.Path
    ) -> None:
        seen = self._record_threads(pool, monkeypatch)
        future = pool.submit_op(OP_REALPATH_SPELLINGS, os.fsencode(str(tmp_path)))
        assert seen == [], "submitting must not hand the work to another thread"
        future.result(timeout=60)
        assert seen == [threading.get_ident()]

    def test_the_executor_keeps_no_worker_pool_for_child_work(self, pool) -> None:
        # A pool here would be the defect re-entering by the back door.
        assert not hasattr(pool, "_pool")

    def test_an_unawaited_future_cancels_cleanly_because_nothing_ran(
        self, pool, monkeypatch, tmp_path: pathlib.Path
    ) -> None:
        seen = self._record_threads(pool, monkeypatch)
        future = pool.submit_op(OP_REALPATH_SPELLINGS, os.fsencode(str(tmp_path)))
        assert future.cancel() is True
        assert future.cancelled() is True
        assert seen == []

    def test_a_future_already_awaited_refuses_to_cancel(self, pool, tmp_path: pathlib.Path) -> None:
        # Truthful the other way round: the work did happen, so a caller cannot
        # read a successful cancel as "nothing ran".
        future = pool.submit_op(OP_REALPATH_SPELLINGS, os.fsencode(str(tmp_path)))
        future.result(timeout=60)
        assert future.cancel() is False

    def test_a_second_await_reuses_the_answer_rather_than_asking_again(
        self, pool, monkeypatch, tmp_path: pathlib.Path
    ) -> None:
        seen = self._record_threads(pool, monkeypatch)
        future = pool.submit_op(OP_REALPATH_SPELLINGS, os.fsencode(str(tmp_path)))
        first = future.result(timeout=60)
        assert future.result(timeout=60) == first
        assert len(seen) == 1


class TestCallerBudget:
    """A budget that expires means the mount did not answer, and only that."""

    @staticmethod
    def _hanging(tmp_path: pathlib.Path) -> str:
        script = tmp_path / "hang.py"
        script.write_text(
            textwrap.dedent("""
                import sys, time
                sys.stdin.buffer.read(4)
                time.sleep(3600)
                """),
            encoding="utf-8",
        )
        return str(script)

    @posix_read_deadline_only
    def test_a_silent_child_raises_timeout_at_the_callers_budget(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The ceiling is parked far away so the CALLER's budget is what fires.
        executor = SubprocessPoolExecutor(
            workers=2, script=self._hanging(tmp_path), ceiling_secs=600.0
        )
        try:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                executor.call_op(OP_REALPATH_SPELLINGS, b"/x", timeout=1.0)
            assert time.monotonic() - started < 30.0
        finally:
            executor.shutdown(wait=False)

    @posix_read_deadline_only
    def test_a_timed_out_child_is_destroyed_rather_than_reused(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Its pipe is mid-frame: reusing it could pair one path's answer with
        # another path's question, which is the one fault this protocol must not have.
        executor = SubprocessPoolExecutor(
            workers=2, script=self._hanging(tmp_path), ceiling_secs=600.0
        )
        try:
            with pytest.raises(TimeoutError):
                executor.call_op(OP_REALPATH_SPELLINGS, b"/x", timeout=1.0)
            assert all(child.proc is None for child in executor._children)
        finally:
            executor.shutdown(wait=False)

    @posix_read_deadline_only
    def test_an_awaited_future_surfaces_the_budget_as_the_standard_timeout(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Same exception class a ThreadPoolExecutor future raises, so an existing
        # timeout arm needs no change to keep working.
        executor = SubprocessPoolExecutor(
            workers=2, script=self._hanging(tmp_path), ceiling_secs=600.0
        )
        try:
            future = executor.submit_op(OP_REALPATH_SPELLINGS, b"/x")
            with pytest.raises(TimeoutError):
                future.result(timeout=1.0)
        finally:
            executor.shutdown(wait=False)

    def test_a_healthy_child_answers_well_inside_a_small_budget(
        self, pool, tmp_path: pathlib.Path
    ) -> None:
        assert pool.realpath_spellings(str(tmp_path), timeout=10.0)


class TestExecutorContract:
    def test_zero_workers_is_refused(self) -> None:
        with pytest.raises(ValueError):
            SubprocessPoolExecutor(workers=0)

    def test_a_single_worker_is_permitted_and_answers(self, tmp_path: pathlib.Path) -> None:
        # One child is enough for throughput; the second is wedge isolation. An
        # adopter that can tolerate one wedge refusing everything may run with one.
        executor = SubprocessPoolExecutor(workers=1)
        try:
            assert executor.realpath_spellings(str(tmp_path))
        finally:
            executor.shutdown(wait=False)

    def test_submit_still_runs_an_arbitrary_callable_on_a_thread(self, pool) -> None:
        # The drop-in property: unrecognised work behaves exactly as it does on a
        # ThreadPoolExecutor, including a local closure, which is what the
        # resolver's caller submits and what ProcessPoolExecutor cannot pickle.
        def outer():
            def inner(value: str) -> str:
                return value.upper()

            return inner

        assert pool.submit(outer(), "abc").result(timeout=60) == "ABC"

    def test_map_still_works(self, pool) -> None:
        assert list(pool.map(str.strip, [" a ", " b "])) == ["a", "b"]


class TestChildFunctionsInProcess:
    """Exercise the child module's own functions in THIS interpreter.

    The end-to-end tests above drive the child through a real subprocess, which
    proves the integration but measures no coverage: the child's lines execute in
    another interpreter that the parent's coverage never sees. Instrumenting the
    child is not an option -- it would mean importing ``coverage`` inside a process
    whose entire value is importing stdlib only and starting in about eleven
    milliseconds, and under ``-S`` the usual ``.pth`` hook does not run either. So
    its logic is exercised directly here, and the subprocess tests remain as the
    integration proof. Importing the module is safe because it guards its entry
    point with ``__name__ == "__main__"``.
    """

    def test_the_child_packs_what_the_parent_unpacks(self) -> None:
        # The two protocol implementations are deliberate copies, since sharing
        # them would mean importing kiro_crew in the child. This is what pins the
        # copies to each other.
        values = [b"/a", b"/two\nline", b"", b"/non\xffutf8", b"/x" * 500]
        assert unpack_strings(child_mod._pack_strings(values)) == values

    def test_the_child_resolves_a_symlink_like_the_thread_does(
        self, tmp_path: pathlib.Path
    ) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        spellings = {os.fsdecode(v) for v in child_mod.realpath_spellings(os.fsencode(str(link)))}
        assert spellings == _resolved_spellings(str(link))

    def test_the_child_resolves_a_missing_path_like_the_thread_does(
        self, tmp_path: pathlib.Path
    ) -> None:
        missing = str(tmp_path / "nope" / "deeper")
        spellings = {os.fsdecode(v) for v in child_mod.realpath_spellings(os.fsencode(missing))}
        assert spellings == _resolved_spellings(missing)

    def test_the_child_deduplicates_identical_spellings(self, tmp_path: pathlib.Path) -> None:
        # Both resolvers usually agree, and the answer must not carry the same
        # spelling twice: the parent turns this into a set, so a duplicate would be
        # silent here and only show up as a wrong count in the wire frame.
        out = child_mod.realpath_spellings(os.fsencode(str(tmp_path)))
        assert len(out) == len(set(out))

    def test_a_valid_request_answers_ok_with_the_echoed_id(self, tmp_path: pathlib.Path) -> None:
        body = struct.pack(">IB", 4242, OP_REALPATH_SPELLINGS) + os.fsencode(str(tmp_path))
        response = child_mod._handle(body)
        (echoed,) = struct.unpack_from(">I", response, 0)
        assert echoed == 4242
        assert response[4] == 0  # STATUS_OK
        assert unpack_strings(response[5:])

    def test_an_unknown_op_answers_error_with_a_class_name_and_no_path(
        self, tmp_path: pathlib.Path
    ) -> None:
        secret = str(tmp_path / "credential-store-name")
        body = struct.pack(">IB", 7, 99) + os.fsencode(secret)
        response = child_mod._handle(body)
        (echoed,) = struct.unpack_from(">I", response, 0)
        assert echoed == 7
        assert response[4] == 1  # STATUS_ERROR
        detail = response[5:].decode("ascii", "replace")
        assert detail == "ValueError"
        assert "credential-store-name" not in detail

    def test_a_truncated_request_body_answers_error_rather_than_raising(self) -> None:
        # A malformed frame must not kill the child: a dead child is a wedge. The id
        # is 0 because the header could not be read, and the parent never issues 0,
        # so it reads as "unreadable frame", fails closed and discards the child.
        response = child_mod._handle(b"\x00\x00")
        (echoed,) = struct.unpack_from(">I", response, 0)
        assert echoed == 0
        assert response[4] == 1  # STATUS_ERROR
        assert response[5:].decode("ascii", "replace") == "error"  # struct.error

    def test_the_parent_never_issues_request_id_zero(self, pool, tmp_path: pathlib.Path) -> None:
        # Reserving 0 is what makes the unreadable-frame answer unambiguous.
        target = pool._children[0]
        target._next_id = 0xFFFFFFFF  # the largest id; the next one must not be 0
        pool.realpath_spellings(str(tmp_path))
        assert target._next_id != 0

    def test_read_exactly_returns_none_at_a_clean_end_of_input(self) -> None:
        assert child_mod._read_exactly(io.BytesIO(b""), 4) is None

    def test_read_exactly_returns_none_on_a_partial_tail(self) -> None:
        assert child_mod._read_exactly(io.BytesIO(b"ab"), 4) is None

    def test_read_exactly_loops_over_short_reads(self) -> None:
        class _Dribble:
            """A pipe that hands back one byte at a time, which is legal."""

            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self, count: int) -> bytes:
                chunk, self._data = self._data[:1], self._data[1:]
                return chunk

        assert child_mod._read_exactly(_Dribble(b"abcd"), 4) == b"abcd"

    def test_the_main_loop_serves_a_request_then_exits_at_eof(
        self, tmp_path: pathlib.Path, monkeypatch
    ) -> None:
        body = struct.pack(">IB", 1, OP_REALPATH_SPELLINGS) + os.fsencode(str(tmp_path))
        frame = struct.pack(">I", len(body)) + body

        class _Stream:
            def __init__(self, data: bytes = b"") -> None:
                self.buffer = io.BytesIO(data)

        monkeypatch.setattr(child_mod.sys, "stdin", _Stream(frame))
        out = _Stream()
        monkeypatch.setattr(child_mod.sys, "stdout", out)

        assert child_mod.main() == 0  # returns 0 when the parent closes its end

        written = out.buffer.getvalue()
        (length,) = struct.unpack_from(">I", written, 0)
        response = written[4 : 4 + length]
        assert response[4] == 0
        assert unpack_strings(response[5:])


class TestChildRunsIsolated:
    """The child must not honour PYTHONPATH, or an injected import is a credential read.

    This child is allowlisted to run OUTSIDE the agent sandbox precisely so it can
    read the paths the sensitive-path gate checks. That makes code execution inside
    it worth having, and the interpreter imports four stdlib modules at startup that
    a writable ``sys.path`` entry could shadow. ``-I`` is what closes it. The third
    test is a negative control: without it the second test would pass for free and
    prove nothing.
    """

    @staticmethod
    def _planted(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        """A directory holding a pathlib.py that marks the filesystem when imported."""
        evil = tmp_path / "planted"
        evil.mkdir()
        marker = tmp_path / "planted-code-ran"
        (evil / "pathlib.py").write_text(
            f"open({str(marker)!r}, 'w').close()\n"
            "class Path:\n"
            "    def __init__(self, *a, **k):\n"
            "        pass\n"
            "    def resolve(self):\n"
            "        return self\n",
            encoding="utf-8",
        )
        return evil, marker

    @staticmethod
    def _run_child(args: list[str], evil: pathlib.Path, target: pathlib.Path):
        return subprocess.run(
            args,
            input=_request_frame(1, OP_REALPATH_SPELLINGS, os.fsencode(str(target))),
            capture_output=True,
            timeout=60,
            env=dict(os.environ, PYTHONPATH=str(evil)),
        )

    def test_the_pool_launches_the_child_isolated(self, pool, tmp_path: pathlib.Path) -> None:
        # Pinned on the live argv so the flag cannot be dropped unnoticed.
        pool.realpath_spellings(str(tmp_path))
        proc = pool._children[0].proc
        assert proc is not None
        assert "-I" in proc.args

    def test_a_planted_stdlib_module_does_not_run_in_the_isolated_child(
        self, tmp_path: pathlib.Path
    ) -> None:
        evil, marker = self._planted(tmp_path)
        target = tmp_path / "real"
        target.mkdir()
        done = self._run_child([sys.executable, "-I", "-S", str(_CHILD)], evil, target)
        assert done.returncode == 0
        assert _first_response_ok(done.stdout)
        assert not marker.exists(), "PYTHONPATH was honoured and planted code executed"

    def test_the_control_shows_the_planted_module_would_otherwise_run(
        self, tmp_path: pathlib.Path
    ) -> None:
        # Drop -I and the same plant executes, which is what makes the test above a
        # measurement rather than a formality.
        evil, marker = self._planted(tmp_path)
        target = tmp_path / "real"
        target.mkdir()
        self._run_child([sys.executable, "-S", str(_CHILD)], evil, target)
        assert marker.exists(), "control failed: the plant never ran even without -I"
