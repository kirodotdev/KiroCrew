"""A failed Mochi queue write-back does not lose the done markers of the tasks that ran.

On Windows a write-back onto a queue file another process holds open fails. The
poller keeps the done markers of that write-back, matched on the whole task, so:

* a task that ran is not run again, and the next write saves its marker;
* a new task that reuses its id (mid-poll or between polls) still runs;
* a reminder ``full_replace`` carries over unchanged keeps its marker;
* a deleted queue drops the markers, while an unreadable one keeps them.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def held_queue(monkeypatch):
    """Make the poller's next N write-backs fail as a held file does on Windows."""
    from kiro_crew.apps.builtins.mochi import queue_file as qf

    real_write = qf.write_queue_atomic
    held = [0]

    def write(path, queue):
        if held[0]:
            held[0] -= 1
            raise PermissionError(32, "The process cannot access the file")
        return real_write(path, queue)

    monkeypatch.setattr(qf, "write_queue_atomic", write)
    return held


@pytest.mark.asyncio
async def test_a_task_whose_write_back_failed_does_not_run_again(tmp_path, held_queue):
    from test_mochi_queue_poller_coverage import _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 1

    await rig.poll()
    assert rig.ran() == [("notify", "t1")]
    assert rig.task("t1")["done"] is False  # the write-back never landed

    await rig.poll()
    assert rig.ran() == [("notify", "t1")]  # not run a second time
    assert rig.task("t1")["done"] is True  # and the next write-back saved it


@pytest.mark.asyncio
async def test_a_reset_queue_forgets_the_unsaved_markers(tmp_path, held_queue):
    """A reset removes the tasks, so a later task with the same id is new and runs."""
    from test_mochi_queue_poller_coverage import _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 1
    await rig.poll()

    # Reset between the poll's read and its write-back: the merge writes nothing.
    rig.during = lambda: rig.path.unlink()
    rig.write(_task("t2", "notify"))
    await rig.poll()
    rig.during = None

    rig.write(_task("t1", "notify"))
    await rig.poll()
    assert rig.ran() == [("notify", "t1"), ("notify", "t2"), ("notify", "t1")]


@pytest.mark.asyncio
async def test_a_replan_during_the_poll_gets_no_done_marker_from_the_old_plan(tmp_path, held_queue):
    """A new task reusing the id, written mid-poll, gets no marker from the old one."""
    from test_mochi_queue_poller_coverage import NOW, _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 1
    await rig.poll()  # t1 runs; its marker is held back

    rig.write(_task("t1", "notify"), _task("t2", "notify"))
    replanned = []

    def replan_once():
        if not replanned:
            replanned.append(True)
            # A new task that reuses the id: different content, a new plan.
            rig.write(_task("t1", "notify", message="new"), planned_at=qf_iso(NOW - 60_000))

    rig.during = replan_once
    await rig.poll()  # t1 is skipped; t2 runs and the plan is replaced under it
    rig.during = None
    assert rig.task("t1")["done"] is False  # the new plan's t1 has not run

    await rig.poll()
    assert rig.ran() == [("notify", "t1"), ("notify", "t2"), ("notify", "t1")]


@pytest.mark.asyncio
async def test_an_unreadable_queue_keeps_the_unsaved_markers(tmp_path, held_queue, monkeypatch):
    """Only a deleted queue is a reset: a read that fails keeps the markers."""
    from test_mochi_queue_poller_coverage import _Rig, _task

    from kiro_crew.apps.builtins.mochi import queue_file as qf

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 1
    await rig.poll()  # t1 runs; its marker is held back

    real_read = qf.read_queue
    reads = [0]

    def second_read_fails(path):
        reads[0] += 1
        return None if reads[0] == 2 else real_read(path)

    monkeypatch.setattr(qf, "read_queue", second_read_fails)
    await rig.poll()  # the poll read works; the write-back read does not
    monkeypatch.setattr(qf, "read_queue", real_read)

    await rig.poll()
    assert rig.ran() == [("notify", "t1")]
    assert rig.task("t1")["done"] is True


@pytest.mark.asyncio
async def test_a_new_plan_between_polls_forgets_the_unsaved_markers(tmp_path, held_queue):
    """A replan between polls may reuse an id for a new task, which still runs."""
    from test_mochi_queue_poller_coverage import NOW, _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 1
    await rig.poll()

    rig.write(_task("t1", "notify", message="new"), planned_at=qf_iso(NOW - 60_000))
    await rig.poll()
    assert rig.ran() == [("notify", "t1"), ("notify", "t1")]


@pytest.mark.asyncio
async def test_a_reminder_carried_into_a_new_plan_keeps_its_done_marker(tmp_path, held_queue):
    """full_replace carries an undone reminder over unchanged: it is the task that ran."""
    from test_mochi_queue_poller_coverage import NOW, _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    reminder = _task("r1", "notify", category="reminder")
    rig.write(reminder)
    held_queue[0] = 1
    await rig.poll()  # r1 runs; its marker is held back

    rig.write(reminder, planned_at=qf_iso(NOW - 60_000))  # what full_replace keeps
    await rig.poll()
    assert rig.ran() == [("notify", "r1")]
    assert rig.task("r1")["done"] is True


@pytest.mark.asyncio
async def test_a_queue_deleted_between_polls_forgets_the_unsaved_markers(tmp_path, held_queue):
    """A reset that deletes the queue between polls: the same task submitted again runs."""
    from test_mochi_queue_poller_coverage import _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 1
    await rig.poll()  # t1 runs; its marker is held back

    rig.path.unlink()
    await rig.poll()  # the poll sees no queue

    rig.write(_task("t1", "notify"))  # the same fields, submitted again
    await rig.poll()
    assert rig.ran() == [("notify", "t1"), ("notify", "t1")]


@pytest.mark.asyncio
async def test_a_failed_write_back_read_keeps_this_polls_markers(tmp_path, held_queue, monkeypatch):
    """The write-back cannot re-read a queue that still exists: what ran is kept."""
    from test_mochi_queue_poller_coverage import _Rig, _task

    from kiro_crew.apps.builtins.mochi import queue_file as qf

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    real_read = qf.read_queue
    reads = [0]

    def second_read_fails(path):
        reads[0] += 1
        return None if reads[0] == 2 else real_read(path)

    monkeypatch.setattr(qf, "read_queue", second_read_fails)
    await rig.poll()  # t1 runs; the write-back read fails on a file that exists
    monkeypatch.setattr(qf, "read_queue", real_read)

    await rig.poll()
    assert rig.ran() == [("notify", "t1")]
    assert rig.task("t1")["done"] is True


@pytest.mark.asyncio
async def test_a_changed_task_reusing_the_id_is_not_cached_as_done(tmp_path, held_queue):
    """A full_replace lands mid-poll with a changed t1, then the write fails."""
    from test_mochi_queue_poller_coverage import NOW, _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 1
    replaced = []

    def replace_once():
        if not replaced:
            replaced.append(True)
            rig.write(_task("t1", "notify", message="new"), planned_at=qf_iso(NOW - 60_000))

    rig.during = replace_once
    await rig.poll()  # the old t1 runs; the write-back fails
    rig.during = None

    await rig.poll()  # the changed t1 is a different task: it runs
    assert rig.ran() == [("notify", "t1"), ("notify", "t1")]
    assert rig.calls[-1][1].get("message") == "new"


@pytest.mark.asyncio
async def test_markers_outlive_the_done_task_cleanup_while_writes_keep_failing(
    tmp_path, held_queue
):
    """Writes failing for longer than the done-task TTL still never re-run the task."""
    from test_mochi_queue_poller_coverage import _Rig, _task

    from kiro_crew.apps.builtins.mochi import queue_file as qf

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    held_queue[0] = 3
    await rig.poll()  # t1 runs; the write-back fails
    rig.now += qf.DEFAULT_TTL_MS + 60_000
    await rig.poll()  # still failing, past the cleanup cutoff
    await rig.poll()  # still failing
    assert rig.ran() == [("notify", "t1")]


@pytest.mark.asyncio
async def test_an_expired_queue_still_saves_the_held_back_marker(tmp_path, held_queue):
    """The plan/replan route writes back a held-back marker though nothing else changed."""
    from test_mochi_queue_poller_coverage import NOW, _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"), planned_until=qf_iso(NOW - 1_000))
    held_queue[0] = 1
    await rig.poll()  # t1 runs; the route is "plan" and its write-back fails
    assert rig.task("t1")["done"] is False

    await rig.poll()  # t1 is skipped, so nothing marks the queue modified
    assert rig.ran() == [("notify", "t1")]
    assert rig.task("t1")["done"] is True


@pytest.fixture
def refused_lock(monkeypatch):
    """Make the next N queue-lock takes fail closed, as a lock held past its ceiling does."""
    import contextlib

    from kiro_crew.apps.builtins.mochi import queue_file as qf

    real_lock = qf.file_lock
    refused = [0]

    @contextlib.contextmanager
    def file_lock(fd, exclusive=False):
        if refused[0]:
            refused[0] -= 1
            raise OSError("could not acquire exclusive file lock: still held")
        with real_lock(fd, exclusive=exclusive):
            yield

    monkeypatch.setattr(qf, "file_lock", file_lock)
    return refused


@pytest.fixture
def unopenable_lock_file(monkeypatch):
    """Make the next N opens of the queue's ``.lock`` file fail."""
    from kiro_crew import platform_compat

    real_open = platform_compat.open_create_or_existing
    failing = [0]

    def open_create_or_existing(path, *args, **kwargs):
        if failing[0]:
            failing[0] -= 1
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(platform_compat, "open_create_or_existing", open_create_or_existing)
    return failing


async def _assert_ran_once_then_saved(rig):
    """After a poll whose lock failed: the task is held back, not re-run, then saved."""
    from kiro_crew.apps.builtins.mochi.queue_poller import _task_identity

    t1 = rig.task("t1")
    assert t1["done"] is False  # nothing was written without the lock
    ran_at = rig.poller._unwritten_done.get(_task_identity(t1))
    assert ran_at  # the poller kept what ran

    rig.now += 60_000  # a re-run would stamp a later executed_at
    await rig.poll()
    assert rig.ran() == [("notify", "t1")]  # not run a second time
    assert rig.task("t1")["done"] is True
    assert rig.task("t1")["executed_at"] == ran_at


@pytest.mark.asyncio
async def test_a_refused_queue_lock_keeps_the_done_markers(tmp_path, refused_lock):
    from test_mochi_queue_poller_coverage import _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    refused_lock[0] = 1

    await rig.poll()  # t1 runs; the write-back cannot take the lock
    assert rig.ran() == [("notify", "t1")]
    await _assert_ran_once_then_saved(rig)


@pytest.mark.asyncio
async def test_a_refused_queue_lock_on_the_plan_route_keeps_the_done_markers(
    tmp_path, refused_lock
):
    from test_mochi_queue_poller_coverage import NOW, _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"), planned_until=qf_iso(NOW - 1_000))
    refused_lock[0] = 1

    await rig.poll()  # t1 runs; the plan-route write-back cannot take the lock
    assert rig.ran() == [("notify", "t1")]
    await _assert_ran_once_then_saved(rig)


@pytest.mark.asyncio
async def test_an_unopenable_lock_file_keeps_the_done_markers(tmp_path, unopenable_lock_file):
    from test_mochi_queue_poller_coverage import _Rig, _task

    rig = _Rig(tmp_path / "mochi-queue.json")
    rig.write(_task("t1", "notify"))
    unopenable_lock_file[0] = 1

    await rig.poll()  # t1 runs; the lock file cannot be opened
    assert rig.ran() == [("notify", "t1")]
    await _assert_ran_once_then_saved(rig)


def qf_iso(ms: int) -> str:
    from kiro_crew.apps.builtins.mochi import queue_file as qf

    return qf._iso(ms)
