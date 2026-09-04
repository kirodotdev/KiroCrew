"""SessionMap's threading contract: the lock, the batch, and the ratchets.

Issue #2989. Every mutation rewrites the WHOLE map from ``_data``, so a
read-modify-write is atomic only while nothing else touches the structure.
Before ``_MAP_LOCK`` the event loop was the only thing providing that, which is
why offloading a single write made the map racy instead of non-blocking (a
``to_thread`` wrapper reverted on #2976 for exactly that reason).

Four properties are pinned here:

1. more than one thread may call the map, and no entry is lost;
2. a ``batched_save`` block is ONE critical section, not merely one write;
3. a lock-FREE reader never observes a half-built structure, which is the price
   of leaving the hot single-key probes unlocked;
4. the two rules that no runtime assertion can express are ratcheted
   structurally — every mutating/iterating method holds the lock, and no
   ``batched_save`` block anywhere in the tree awaits.
"""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from source_corpus import parsed_candidates

import kiro_crew.session_map as session_map_mod
from kiro_crew.session_map import SESSION_MAP_FILENAME, SessionMap

SRC = Path(session_map_mod.__file__).resolve().parent


@pytest.fixture
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestConcurrentMutation:
    """Property 1: any thread may call the map."""

    def test_parallel_writers_lose_no_entry(self, session_map, tmp_path):
        """Every key written from N threads survives in the persisted file.

        Without the lock this is the lost update the issue describes: each
        ``set`` rewrites the WHOLE map from ``_data``, so two threads that
        interleave read-modify-write drop one another's entries — and the file
        keeps whichever ``os.replace`` happened to land last.
        """
        errors: list[BaseException] = []
        start = threading.Barrier(8)

        def writer(n: int) -> None:
            try:
                start.wait(timeout=10)
                for i in range(25):
                    session_map.set(f"dashboard:t{n}-{i}", f"sid-{n}-{i}")
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        persisted = reloaded.mapped_sids_by_key()
        expected = {f"dashboard:t{n}-{i}": f"sid-{n}-{i}" for n in range(8) for i in range(25)}
        assert persisted == expected

    def test_iterating_reader_survives_concurrent_writer(self, session_map):
        """A scan does not blow up on a dict mutated underneath it.

        ``mapped_sids_by_key`` / ``find_key_by_sid`` iterate ``_data``. An
        unlocked scan racing a writer raises ``RuntimeError: dictionary changed
        size during iteration`` — a gateway-visible crash in whatever task
        happened to be scanning, not a subtle inconsistency.

        The switch interval is shortened for the duration: at the default 5 ms a
        scan of a few hundred keys usually finishes inside one slice, so the
        unlocked race is real but rarely observed, and a test that only
        SOMETIMES catches it reports coverage it does not have.
        """
        for i in range(600):
            session_map.set(f"dashboard:seed-{i}", f"sid-{i}")
        errors: list[BaseException] = []
        stop = threading.Event()
        running = threading.Event()

        def writer() -> None:
            i = 0
            try:
                while not stop.is_set():
                    session_map.set(f"dashboard:churn-{i}", f"sid-churn-{i}")
                    running.set()
                    session_map.delete(f"dashboard:churn-{i}")
                    i += 1
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        previous_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        t = threading.Thread(target=writer)
        t.start()
        try:
            assert running.wait(timeout=10)
            for _ in range(200):
                session_map.mapped_sids_by_key()
                session_map.find_key_by_sid("sid-599")
        finally:
            stop.set()
            t.join(timeout=30)
            sys.setswitchinterval(previous_interval)
        assert errors == []


class TestBatchIsOneCriticalSection:
    """Property 2: a batch excludes other threads, not just extra writes."""

    def test_writer_cannot_interleave_with_a_batch(self, session_map):
        entered = threading.Event()
        other_finished = threading.Event()

        def other_writer() -> None:
            entered.wait(timeout=10)
            session_map.set("dashboard:outsider", "sid-outsider")
            other_finished.set()

        t = threading.Thread(target=other_writer)
        t.start()
        try:
            with session_map.batched_save():
                session_map.set("dashboard:a", "sid-a")
                entered.set()
                # The other thread is now runnable and wants the same map. It
                # must not be able to complete its whole-map write inside this
                # block: doing so would put its entry in a snapshot this batch
                # then overwrites on exit.
                assert not other_finished.wait(timeout=0.5)
                session_map.set("dashboard:b", "sid-b")
        finally:
            entered.set()
            t.join(timeout=30)

        assert other_finished.is_set()
        keys = session_map.mapped_sids_by_key()
        assert {"dashboard:a", "dashboard:b", "dashboard:outsider"} <= set(keys)

    def test_batch_still_collapses_to_one_write(self, session_map):
        with patch.object(SessionMap, "_write", autospec=True) as write:
            with session_map.batched_save():
                session_map.set("dashboard:a", "sid-a")
                session_map.set("dashboard:b", "sid-b")
                session_map.set_flag("dashboard:a", "temporary", True)
        assert write.call_count == 1

    def test_lock_is_released_when_a_batch_raises(self, session_map):
        with pytest.raises(ValueError):
            with session_map.batched_save():
                session_map.set("dashboard:a", "sid-a")
                raise ValueError("boom")
        # A leaked lock would hang this call rather than fail it.
        done = threading.Event()

        def probe() -> None:
            session_map.set("dashboard:after", "sid-after")
            done.set()

        t = threading.Thread(target=probe)
        t.start()
        t.join(timeout=10)
        assert done.is_set()
        # The partial sequence still reached disk (unchanged pre-existing rule).
        assert "dashboard:a" in session_map.mapped_sids_by_key()


class TestLockFreeReadsSeeWholeStructures:
    """Property 3: what leaving the hot probes unlocked obliges writers to do."""

    def test_thread_index_probe_never_sees_a_missing_owner(self, session_map):
        """``get_session_for_thread`` resolves during a concurrent rebuild.

        The probe is lock-free — it is the hot path of every inbound Slack reply
        — so a rebuild that CLEARED the index and refilled it would give the
        probe a window where the thread has no owner. That is not a delay for
        the caller: an unowned thread forks the user's reply into a brand-new
        session. So the rebuild must publish a finished dict by rebinding.
        """
        for i in range(400):
            session_map.set_slack_link(f"dashboard:chat-{i}", f"ts-{i}", "C1")
        misses: list[str] = []
        stop = threading.Event()
        running = threading.Event()

        def rebuilder() -> None:
            while not stop.is_set():
                session_map._rebuild_thread_index()
                running.set()

        previous_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        t = threading.Thread(target=rebuilder)
        t.start()
        try:
            assert running.wait(timeout=10)
            for _ in range(4000):
                if session_map.get_session_for_thread("ts-399") != "dashboard:chat-399":
                    misses.append("unowned")
                    break
        finally:
            stop.set()
            t.join(timeout=30)
            sys.setswitchinterval(previous_interval)
        assert misses == []


class TestLockHoldIsBounded:
    """A caller can WAIT now, so what runs under the lock must stay bounded."""

    def test_worker_thread_construction_does_not_block_the_loop(self, session_map, tmp_path):
        """Building a `SessionMap()` off-loop must not stall a loop-side mutation.

        `handlers/session_storage._build_index` constructs one under
        `asyncio.to_thread`. If `_load` held the lock across its file read, a
        loop-side `set_slack_link` would block on a worker's disk I/O — and the
        stall is shared by every gateway task, including the heartbeat. The read
        is made slow here so the difference is a decision, not a race.
        """
        for i in range(300):
            session_map.set(f"dashboard:seed-{i}", f"sid-{i}")

        real_read = Path.read_text
        reading = threading.Event()
        release = threading.Event()

        def slow_read(self, *a, **kw):
            if self.name == SESSION_MAP_FILENAME:
                reading.set()
                release.wait(timeout=10)
            return real_read(self, *a, **kw)

        loop_side_done = threading.Event()

        def worker() -> None:
            with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
                with patch.object(Path, "read_text", slow_read):
                    SessionMap()

        t = threading.Thread(target=worker)
        t.start()
        try:
            assert reading.wait(timeout=10), "worker never reached the file read"
            # The worker is now parked INSIDE _load. A loop-side mutation must
            # complete anyway.
            mutator = threading.Thread(
                target=lambda: (
                    session_map.set_slack_link("dashboard:seed-0", "ts-0", "C1"),
                    loop_side_done.set(),
                )
            )
            mutator.start()
            assert loop_side_done.wait(timeout=5), (
                "a loop-side mutation blocked behind a worker thread's map file read"
            )
            mutator.join(timeout=10)
        finally:
            release.set()
            t.join(timeout=30)

    def test_no_guarded_method_reads_the_map_file(self):
        """Ratchet: the file read/parse stays outside the lock.

        This is the invariant behind the bound in the class contract. A future
        `reload()` decorated `@_guarded` would reintroduce exactly the stall the
        test above pins.
        """
        offenders = []
        for fn in _session_map_class().body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}
            if "_guarded" not in decorators:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in {"read_text", "loads", "load"}:
                    offenders.append(f"{fn.name} -> {func.attr}")
        assert offenders == [], (
            f"guarded methods read/parse the map file: {offenders}. Reading the "
            "file is the one unbounded step in this class; holding the lock "
            "across it lets an off-loop construction stall every gateway task."
        )

    def test_load_runs_only_from_init(self):
        """`_load`'s safety rests on the instance being unpublished.

        It is unguarded because `__init__` is the only caller, so no other thread
        can see `self` yet. A second call site from a live instance would make it
        an unsynchronized writer.
        """
        callers = []
        for fn in _session_map_class().body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_load"
                ):
                    callers.append(fn.name)
        assert callers == ["__init__"], (
            f"_load is called from {callers}; it is unguarded ONLY because the "
            "instance is not yet shared with any other thread."
        )


def _session_map_class() -> ast.ClassDef:
    tree = ast.parse((SRC / "session_map.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SessionMap":
            return node
    raise AssertionError("SessionMap class not found")


def _touches_map_structure(fn: ast.FunctionDef) -> bool:
    """True when *fn* mutates or iterates ``_data`` / ``_thread_to_session``.

    Structural, not name-based: a new method is caught by what it does, not by
    what it is called. Recognized shapes are assignment/``del`` to a subscript
    of the dict, a ``.pop``/``.clear``/``.setdefault`` call on it, iteration
    over it, and handing the whole dict to another callable (``json.dump``).
    """
    guarded = {"_data", "_thread_to_session"}

    def names_map(node: ast.AST) -> bool:
        return isinstance(node, ast.Attribute) and node.attr in guarded

    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if isinstance(tgt, ast.Subscript) and names_map(tgt.value):
                    return True
        if isinstance(node, ast.Delete):
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) and names_map(tgt.value):
                    return True
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"pop", "clear", "setdefault"}
                and names_map(func.value)
            ):
                return True
            if any(names_map(arg) for arg in node.args):
                return True
        if isinstance(node, (ast.For, ast.comprehension)):
            iterable = node.iter
            if names_map(iterable):
                return True
            if (
                isinstance(iterable, ast.Call)
                and isinstance(iterable.func, ast.Attribute)
                and iterable.func.attr in {"items", "keys", "values"}
                and names_map(iterable.func.value)
            ):
                return True
    return False


class TestLockRatchet:
    """Property 4a: a mutator cannot be added without the lock."""

    def test_every_structural_method_holds_the_lock(self):
        offenders = []
        for fn in _session_map_class().body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name == "batched_save":
                continue  # takes the lock explicitly around its yield
            decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}
            if _touches_map_structure(fn) and "_guarded" not in decorators:
                offenders.append(fn.name)
        assert offenders == [], (
            "SessionMap methods mutate or iterate the map without @_guarded: "
            f"{offenders}. The loop is no longer the only mutex — see the class "
            "docstring's threading contract."
        )

    def test_ratchet_detects_an_unguarded_mutator(self):
        """The ratchet above fails when the decorator is dropped.

        Without this, a detector that silently stopped matching would keep
        reporting an empty offender list forever.
        """
        fn = ast.parse(
            "def set_thing(self, k, v):\n    self._data[k] = v\n    self._save()\n"
        ).body[0]
        assert isinstance(fn, ast.FunctionDef)
        assert _touches_map_structure(fn)
        scan = ast.parse("def read_thing(self, k):\n    return self._data.get(k)\n").body[0]
        assert isinstance(scan, ast.FunctionDef)
        assert not _touches_map_structure(scan)


class TestNoLockAcrossFlushAwait:
    """The deferred flush must never hold ``_MAP_LOCK`` across its await.

    Extends property 4b to the flush path: serialization happens inside the
    guarded sync helpers, and only the already-serialized payload crosses into
    ``asyncio.to_thread``. Two shapes would break that, and both are ratcheted
    structurally: an ``async def`` in the module decorated ``@_guarded`` (the
    wrapper would take the lock only to build the coroutine object — a no-op
    that reads as protection), and a ``with _MAP_LOCK`` block inside an
    ``async def`` whose body awaits.
    """

    @staticmethod
    def _async_defs(tree: ast.AST):
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                yield node

    def test_no_async_def_is_guarded(self):
        offenders = []
        tree = ast.parse((SRC / "session_map.py").read_text(encoding="utf-8"))
        for fn in self._async_defs(tree):
            decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}
            if "_guarded" in decorators:
                offenders.append(fn.name)
        assert offenders == [], (
            f"async functions decorated @_guarded: {offenders}. The wrapper "
            "releases the lock as soon as the coroutine OBJECT is built, so the "
            "decoration is not protection — and making it real would hold the "
            "lock across an await."
        )

    def test_no_map_lock_block_awaits(self):
        offenders: list[str] = []
        tree = ast.parse((SRC / "session_map.py").read_text(encoding="utf-8"))
        for fn in self._async_defs(tree):
            for node in ast.walk(fn):
                if not isinstance(node, (ast.With, ast.AsyncWith)):
                    continue
                holds_map_lock = any(
                    isinstance(item.context_expr, ast.Name)
                    and item.context_expr.id == "_MAP_LOCK"
                    for item in node.items
                )
                if not holds_map_lock:
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                        offenders.append(f"{fn.name}:{inner.lineno}")
        assert offenders == [], (
            f"_MAP_LOCK is held across an await at: {offenders}. Serialize "
            "under the lock, write off-lock — a lock held across the flush "
            "await lets a coroutine interleave while worker threads stall."
        )

    def test_ratchet_detects_a_lock_held_across_await(self):
        tree = ast.parse(
            "async def f(s):\n"
            "    with _MAP_LOCK:\n"
            "        payload = s._serialize()\n"
            "        await asyncio.to_thread(s._write_payload, payload)\n"
        )
        fns = list(self._async_defs(tree))
        assert len(fns) == 1
        found = False
        for node in ast.walk(fns[0]):
            if isinstance(node, ast.With) and any(
                isinstance(i.context_expr, ast.Name) and i.context_expr.id == "_MAP_LOCK"
                for i in node.items
            ):
                found = found or any(isinstance(n, ast.Await) for n in ast.walk(node))
        assert found


class TestNoAwaitInsideBatch:
    """Property 4b: no ``batched_save`` block anywhere in the tree awaits."""

    @staticmethod
    def _batch_blocks(tree: ast.AST):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            for item in node.items:
                expr = item.context_expr
                if (
                    isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Attribute)
                    and expr.func.attr == "batched_save"
                ):
                    yield node
                    break

    #: A block can only be one this gate cares about if the module spells
    #: ``batched_save``, so nothing else is worth parsing.
    _REQUIRE_ALL = ("batched_save",)

    def test_no_batch_block_awaits(self):
        offenders: list[str] = []
        for path, _text, tree in parsed_candidates(self._REQUIRE_ALL):
            if "_vendor" in path.parts:
                continue
            for block in self._batch_blocks(tree):
                for inner in ast.walk(block):
                    if isinstance(inner, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                        offenders.append(f"{path.name}:{inner.lineno}")
        assert offenders == [], (
            "batched_save() is held across an await at: "
            f"{offenders}. The lock is reentrant per THREAD, so an await lets "
            "another coroutine on the same loop into the block while a worker "
            "thread waiting on the lock stalls until the await returns."
        )

    def test_ratchet_detects_an_awaiting_batch(self):
        tree = ast.parse(
            "async def f(s):\n"
            "    with s.batched_save():\n"
            "        s.set('k', 'v')\n"
            "        await s.flush()\n"
        )
        blocks = list(self._batch_blocks(tree))
        assert len(blocks) == 1
        assert any(isinstance(n, ast.Await) for n in ast.walk(blocks[0]))
