"""Real child-process exits and cross-process SQLite publication races."""

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_embedding_rebuild_generation import rebuild_home as _rebuild_home
from test_embedding_rebuild_generation import vector_blobs

from kiro_crew import embeddings as emb
from kiro_crew.dashboard.handlers import memory
from kiro_crew.testing.wait import wait_until

rebuild_home = _rebuild_home

_CHILD = r"""
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from kiro_crew import embeddings as emb
from kiro_crew.dashboard.handlers import memory
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database
home = Path(sys.argv[1])
mode = sys.argv[2]
model = home / 'model.gguf'

def open_store(path):
    if path.parent.name == 'member-late':
        return open_member_database(path, member_id='late', store_id='member-late', embedding_dim=2)
    store = VectorMemoryStore(db_path=path, embedding_dim=2)
    store.init()
    return store

backend = emb.default_embedding_backend()
backend._llm = SimpleNamespace(create_embedding=lambda texts: {'data': [{'embedding': [1., 0.]} for _ in texts]}, close=lambda: None)
emb.install_shared_embedder(backend)
(home / 'child-started').write_text('started')
if mode == 'rollback':
    async def run_rollback():
        rollback, _ = await memory._write_embed_model_config(str(model), 2)
        (home / 'child-ready').write_text('ready')
        assert sys.stdin.readline().strip() == 'release'
        try:
            await rollback()
        except ValueError:
            print('refused')
        else:
            print('restored')
    asyncio.run(run_rollback())
    emb.reset_shared_embedder()
    sys.exit(0)
if mode == 'crash':
    asyncio.run(memory._write_embed_model_config(str(model), 2))
    paths = [home / 'memory.db', home / 'memory_stores/legacy/memory.db', home / 'memory_stores/member-late/memory.db']
    for path in paths[:int(sys.argv[3])]:
        store = open_store(path)
        emb.align_store_embedding_space(store)
    os._exit(23)
name = sys.argv[3] if len(sys.argv) > 3 else 'default'
path = home / 'memory.db' if name == 'default' else home / 'memory_stores' / name / 'memory.db'
store = open_store(path)
if mode.startswith('backfill-'):
    if mode == 'backfill-episode':
        store.write_episodic('child process deployment record', defer_embedding=True)
    elif mode == 'backfill-fact':
        store.set_semantic('project.child', 'child process deployment fact', 1., 'user_explicit')
    else:
        store.write_lesson('Check child process deployment safely')
store.embed_fn = emb.make_sync_embed_fn()
original = store._try_embed
first = True
def paused(*args, **kwargs):
    global first
    vector = original(*args, **kwargs)
    if first:
        first = False
        assert vector is not None
        (home / 'child-ready').write_text('ready')
        assert sys.stdin.readline().strip() == 'release'
    return vector
store._try_embed = paused
if mode == 'episode':
    store.write_episodic('child process deployment record')
elif mode == 'fact':
    store.set_semantic('project.child', 'child process deployment fact', 1., 'user_explicit')
elif mode == 'lesson':
    store.write_lesson('Check child process deployment safely')
else:
    store.backfill_missing_embeddings(pace=False)
store.close()
emb.reset_shared_embedder()
"""


def child(home, mode, *args):
    env = dict(os.environ, KIROCREW_HOME=str(home))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(home), mode, *map(str, args)],
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


#: A lost-run ceiling, not a budget the assertions rely on: it bounds only the
#: child's process spawn and its own import of `kiro_crew.embeddings` and
#: `VectorMemoryStore`, which cost about 1.7s on a Linux runner and 2.1s on a
#: Windows one, and are not what any test here asserts. Keeping that cost off the
#: timed path is what lets the bounds below stay at their original values.
_STARTUP_CEILING = 60.0

#: The original bound on the handshake this suite does assert: once the child has
#: announced its imports are done, reaching its pause point is quick on every
#: platform, so this stays tight enough to fail a child that never gets there.
_HANDSHAKE_TIMEOUT = 10.0

#: The original bounds on a child exiting: after the release for the paused modes,
#: and for the whole of the crash mode's own work, which has no pause point.
_EXIT_TIMEOUT = 10.0
_CRASH_EXIT_TIMEOUT = 15.0

#: Only ever spent draining a child that has already been killed, so it bounds
#: cleanup rather than the test.
_DRAIN_TIMEOUT = 5.0


def _child_output(process: "subprocess.Popen[str]") -> str:
    """The child's own stdout and stderr, for a failure message.

    Call only once the child is dead -- it exited by itself, or the caller killed
    it -- because `communicate()` on a live child blocks until it exits.
    """
    try:
        out, err = process.communicate(timeout=_DRAIN_TIMEOUT)
    except subprocess.TimeoutExpired:  # pragma: no cover - needs a wedged child
        return "child output unavailable: it did not close its pipes"
    return f"child stdout={out!r} stderr={err!r}"


def _wait_for_marker(
    process: "subprocess.Popen[str]", home_root: Path, name: str, budget: float
) -> None:
    """Block until the child writes *name*, for at most *budget* seconds.

    A timeout raises with the marker it awaited, the budget, the elapsed wait and
    the child's own stdout and stderr, because the child's output is the only
    evidence of where it got stuck.

    The failure is a raised message rather than an assertion on the clock, so a
    timeout can never surface as a comparison of two equal readings: on Windows
    `time.monotonic()` reports whole milliseconds off GetTickCount64, whose
    15.625ms tick divides ten seconds exactly, so a deadline set that far ahead
    lands on the grid and `assert monotonic() < deadline` renders both operands
    identically.
    """
    marker = home_root / name
    started = time.monotonic()
    try:
        wait_until(lambda: marker.exists() or process.poll() is not None, timeout=budget)
    except AssertionError:
        if marker.exists():  # written as the budget ran out
            return
        waited = time.monotonic() - started
        process.kill()
        raise AssertionError(
            f"the child never wrote {marker.name} within {budget}s "
            f"(waited {waited:.1f}s, still running): {_child_output(process)}"
        ) from None
    if marker.exists():
        return
    raise AssertionError(
        f"the child exited with {process.returncode} before writing "
        f"{marker.name}: {_child_output(process)}"
    )


def _wait_for_child_ready(process: "subprocess.Popen[str]", home_root: Path) -> None:
    """Wait for the child to reach its pause point, in two separately bounded legs.

    The child's spawn and its own imports are bounded as a lost run and nothing
    tighter, because that cost belongs to starting a Python process and not to
    anything this suite asserts. Only once the child says those are done does the
    handshake this suite does assert get its own, tight bound.
    """
    _wait_for_marker(process, home_root, "child-started", _STARTUP_CEILING)
    _wait_for_marker(process, home_root, "child-ready", _HANDSHAKE_TIMEOUT)


def _communicate(
    process: "subprocess.Popen[str]", payload: str | None = None, *, timeout: float = _EXIT_TIMEOUT
) -> tuple[str, str]:
    """`Popen.communicate`, but a timeout names the child and carries its output.

    A bare `TimeoutExpired` reports the bound it broke and nothing about the child
    that broke it, which leaves the crash-mode and release paths as undiagnosable
    as an unmessaged assertion.
    """
    try:
        return process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        # The documented cleanup for a timed-out `communicate()`: kill, then
        # communicate again to reap the child and collect what it did write.
        raise AssertionError(
            f"the child did not exit within {timeout}s of "
            f"{'the release' if payload else 'its start'}: {_child_output(process)}"
        ) from None


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_process_exit_after_config_or_partial_invalidation(rebuild_home, completed):
    home = rebuild_home
    originals = [home.global_store, *home.late]
    for store in originals:
        store.close()
    process = child(home.root, "crash", completed)
    try:
        # Crash mode has no pause point, so its own work and exit are bounded at
        # the original value once the spawn and imports are behind it.
        _wait_for_marker(process, home.root, "child-started", _STARTUP_CEILING)
        out, err = _communicate(process, timeout=_CRASH_EXIT_TIMEOUT)
        assert process.returncode == 23, (out, err)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=_DRAIN_TIMEOUT)
    generation = emb.embedding_rebuild_generation()
    assert generation
    for original in originals:
        reopened = home.open_store(original._db_path)
        emb.align_store_embedding_space(reopened)
        assert reopened.recorded_rebuild_generation() == generation
        assert vector_blobs(reopened) == [None, None]
        assert emb.align_store_embedding_space(reopened) == 0


@pytest.mark.parametrize(
    "kind", ["episode", "fact", "lesson", "backfill-episode", "backfill-fact", "backfill-lesson"]
)
@pytest.mark.parametrize("name", ["default", "legacy", "member-late"])
def test_cross_process_old_vector_cannot_publish_after_rebuild(rebuild_home, kind, name):
    home = rebuild_home
    target = {"default": home.global_store, "legacy": home.late[0], "member-late": home.late[1]}[
        name
    ]
    process = child(home.root, kind, name)
    try:
        _wait_for_child_ready(process, home.root)
        asyncio.run(memory._write_embed_model_config(str(home.model), 2))
        emb.align_store_embedding_space(target)
        out, err = _communicate(process, "release\n")
        assert process.returncode == 0, (out, err)
        if kind.endswith("episode"):
            row = target.db.execute(
                "SELECT embedding FROM episodic_memories WHERE text = ?",
                ("child process deployment record",),
            ).fetchone()
        elif kind.endswith("fact"):
            row = target.db.execute(
                "SELECT embedding FROM semantic_memory WHERE key = 'project.child'"
            ).fetchone()
        else:
            row = target.db.execute(
                "SELECT embedding FROM semantic_memory WHERE key LIKE 'lesson.%'"
            ).fetchone()
        assert row is not None and row[0] is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=_DRAIN_TIMEOUT)


@pytest.mark.parametrize("edit", ["unrelated", "model", "request"])
def test_child_config_rollback_keeps_competing_edits(rebuild_home, edit):
    import json

    from kiro_crew.config.loader import update_config_locked

    home = rebuild_home
    process = child(home.root, "rollback")
    try:
        _wait_for_child_ready(process, home.root)
        generation = emb.embedding_rebuild_generation()

        def modify(data):
            data["unrelated"] = "retained"
            if edit == "model":
                data["memory"]["embed_model_id"] = "competing-model"
            if edit == "request":
                data["memory"]["embed_rebuild_generation"] = "competing-request"
            return data

        update_config_locked(home.config, mutate=modify)
        output, errors = _communicate(process, "release\n")
        assert process.returncode == 0, errors
        assert ("restored" if edit == "unrelated" else "refused") in output
        data = json.loads(home.config.read_text(encoding="utf-8"))
        assert data["unrelated"] == "retained"
        assert data["memory"]["embed_rebuild_generation"] == (
            "competing-request" if edit == "request" else generation
        )
        if edit == "model":
            assert data["memory"]["embed_model_id"] == "competing-model"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=_DRAIN_TIMEOUT)


def _kill_and_reap(process: "subprocess.Popen[str]") -> None:
    """Kill *process* unless it has already exited, and reap it either way.

    Reaping belongs here rather than to `Popen.__exit__`: when that method is
    unwinding a `KeyboardInterrupt` it waits only `_sigint_wait_secs` (0.25s) and
    then returns, never reaching its unbounded `wait()`. A child still being torn
    down at that moment would outlive the teardown as a zombie, so the kill and
    the reap stay together.
    """
    if process.poll() is None:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_DRAIN_TIMEOUT)


@pytest.fixture
def trivial_child():
    """Spawn cheap stand-in children, and guarantee none outlives the test.

    Cleanup is registered at spawn time rather than written into each test body,
    so a child cannot survive a test that never reaches its own cleanup: the
    stack kills every survivor and closes its pipes as it unwinds, however the
    test leaves.

    The real `child()` imports the embedding stack, which is the cost the waits
    above are sized for; an assertion about reporting does not need to pay it.
    """
    with contextlib.ExitStack() as stack:

        def spawn(body: str) -> "subprocess.Popen[str]":
            process = stack.enter_context(
                subprocess.Popen(
                    [sys.executable, "-c", body],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
            )
            # Registered AFTER the process, so it unwinds BEFORE it: the kill and
            # reap land first, which is also what keeps a `KeyboardInterrupt`
            # unwind from leaving a zombie, since `Popen.__exit__` gives up
            # waiting after 0.25s on that path.
            stack.callback(_kill_and_reap, process)
            return process

        yield spawn


def _print_then_sleep(line: str, printed: Path) -> str:
    """A child body that prints *line*, then writes *printed*, then sleeps."""
    return (
        f"import pathlib, sys, time; print({line!r}); sys.stdout.flush(); "
        f"pathlib.Path({str(printed)!r}).write_text('1'); time.sleep(30)"
    )


def _expire_marker_waits(monkeypatch, ready) -> list[float]:
    """Record each timeout `_wait_for_marker` hands its poll, and expire the unmet ones.

    A predicate already true returns at once. Otherwise the stand-in waits (lost-run
    bound) for *ready* -- the child's own evidence that it got as far as the test
    needs -- and then raises as an expired poll does, so no real budget is sat out
    and a loaded host cannot expire the wait before the child has spoken.
    """
    budgets: list[float] = []
    real_wait_until = wait_until

    def expire(predicate, *, timeout=None, **kwargs):
        budgets.append(timeout)
        if predicate():
            return True
        real_wait_until(ready, describe=lambda: "the child never reached its pause point")
        raise AssertionError(f"poll expired after its {timeout}s budget")

    monkeypatch.setitem(globals(), "wait_until", expire)
    return budgets


def test_handshake_timeout_names_the_marker_and_quotes_the_child(
    tmp_path, trivial_child, monkeypatch
):
    """A timed-out handshake reports what it awaited and what the child said.

    Guards the reporting itself, not just the waiting: an unmessaged
    `assert time.monotonic() < deadline` also fails at the right moment, and tells
    a reader nothing about which handshake broke or why.
    """
    printed = tmp_path / "printed"
    process = trivial_child(_print_then_sleep("mid-import", printed))
    budgets = _expire_marker_waits(monkeypatch, printed.exists)
    with pytest.raises(AssertionError) as caught:
        _wait_for_marker(process, tmp_path, "child-ready", 1.0)
    message = str(caught.value)
    assert "child-ready" in message, message
    assert "1.0s" in message, message
    assert "mid-import" in message, message
    # The budget is honoured, not merely quoted: a wait carrying its own hardcoded
    # bound would render the same sentence while handing the poll another timeout.
    assert budgets == [1.0], f"the poll was handed {budgets} for a 1.0s budget"
    assert process.poll() is not None, "a timed-out wait leaves no child behind"


def test_child_exiting_before_handshake_reports_its_code_and_output(tmp_path, trivial_child):
    """An early exit is reported as an exit, not as a timeout it never reached."""
    process = trivial_child(
        "import sys; print('got this far'); print('why', file=sys.stderr); sys.exit(7)"
    )
    with pytest.raises(AssertionError) as caught:
        _wait_for_marker(process, tmp_path, "child-ready", _HANDSHAKE_TIMEOUT)
    message = str(caught.value)
    assert "exited with 7" in message, message
    assert "got this far" in message, message
    assert "why" in message, message


def test_handshake_already_written_does_not_wait(tmp_path, trivial_child, monkeypatch):
    """A budget is an upper bound on a failure, not a cost a passing run pays."""
    import kiro_crew.testing.wait as wait_mod

    (tmp_path / "child-ready").write_text("ready", encoding="utf-8")
    process = trivial_child("import time; time.sleep(30)")
    slept: list[float] = []
    real_time = time

    waiter = threading.get_ident()

    def _refuse_sleep(secs: float) -> None:
        if threading.get_ident() != waiter:
            return real_time.sleep(secs)  # another thread's poll is not this wait
        slept.append(secs)
        raise AssertionError(f"waited {secs}s for a marker that was already written")

    class _NoSleepTime:
        """This module's ``time`` without ``sleep``, so a helper poll fails by name."""

        sleep = staticmethod(_refuse_sleep)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(real_time, name)

    # Any wait, however short, is a sleep: in wait_until's poll (its defining
    # module's ``_sleep``) or in the helper itself. `< _STARTUP_CEILING` on a clock
    # would also hold for a wait that sat out most of the budget.
    monkeypatch.setattr(wait_mod, "_sleep", _refuse_sleep)
    monkeypatch.setitem(globals(), "time", _NoSleepTime())
    _wait_for_marker(process, tmp_path, "child-ready", _STARTUP_CEILING)
    assert slept == [], f"the wait slept {slept} with the marker already written"


def test_the_startup_leg_is_bounded_separately_from_the_handshake(tmp_path, trivial_child):
    """Spawn and imports are bounded as a lost run; the handshake stays tight.

    Which is what lets the asserted bound keep its original value: a child slow to
    import spends the startup ceiling, not the bound this suite measures against.
    """
    assert _HANDSHAKE_TIMEOUT < _STARTUP_CEILING
    process = trivial_child("import time; time.sleep(30)")
    with pytest.raises(AssertionError) as caught:
        _wait_for_marker(process, tmp_path, "child-started", 1.0)
    assert "child-started" in str(caught.value), str(caught.value)


def test_the_handshake_leg_gets_the_tight_bound_not_the_ceiling(
    tmp_path, trivial_child, monkeypatch
):
    """Which leg gets which bound, asserted on the wiring rather than the clock.

    The two constants are shrunk to distinct small values so this costs no real
    seconds; their true values are asserted directly above. Handing the handshake
    leg the ceiling is what would put the suite back to bounding an asserted wait
    by a number sized for a process spawn.
    """
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_STARTUP_CEILING", 3.0)
    monkeypatch.setattr(module, "_HANDSHAKE_TIMEOUT", 0.3)
    (tmp_path / "child-started").write_text("started", encoding="utf-8")
    process = trivial_child("import time; time.sleep(30)")
    budgets = _expire_marker_waits(monkeypatch, lambda: True)
    with pytest.raises(AssertionError) as caught:
        _wait_for_child_ready(process, tmp_path)
    assert "child-ready" in str(caught.value), str(caught.value)
    assert budgets == [3.0, 0.3], f"the legs were handed {budgets}: startup 3.0, handshake 0.3"


def test_kill_and_reap_spares_a_child_that_already_exited(trivial_child):
    """Reaping is conditional on liveness, and complete either way.

    Signalling a reaped pid is not harmless in general: the number can be reused,
    so an unconditional kill in teardown is a kill aimed at whatever holds that
    pid next. Reaping unconditionally is safe, and is what keeps a killed child
    from lingering as a zombie.
    """
    exited = trivial_child("raise SystemExit(0)")
    exited.communicate(timeout=_DRAIN_TIMEOUT)
    assert exited.poll() == 0
    _kill_and_reap(exited)
    assert exited.returncode == 0, "an exited child keeps its own status"

    running = trivial_child("import time; time.sleep(30)")
    assert running.poll() is None
    _kill_and_reap(running)
    assert running.returncode is not None, "a running child is killed AND reaped"


def test_communicate_timeout_reports_the_child_instead_of_raising_bare(
    tmp_path, trivial_child, monkeypatch
):
    """A child that never exits fails as a sentence, not a bare TimeoutExpired."""
    printed = tmp_path / "printed"
    process = trivial_child(_print_then_sleep("alive", printed))
    real_communicate = process.communicate
    timeouts: list[float | None] = []

    def expire_first(payload=None, timeout=None):
        if timeouts:  # the drain after the kill is real
            return real_communicate(payload, timeout=timeout)
        timeouts.append(timeout)
        wait_until(printed.exists, describe=lambda: "the child never printed")
        raise subprocess.TimeoutExpired(process.args, timeout)

    monkeypatch.setattr(process, "communicate", expire_first)
    with pytest.raises(AssertionError) as caught:
        _communicate(process, "release\n", timeout=1.0)
    message = str(caught.value)
    assert "did not exit within 1.0s" in message, message
    assert "alive" in message, message
    assert timeouts == [1.0], f"communicate was handed {timeouts} for a 1.0s budget"
    assert process.poll() is not None, "a timed-out communicate leaves no child behind"
