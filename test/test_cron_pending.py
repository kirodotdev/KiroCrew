"""The cron pending store's bounds and fallbacks (``dashboard/cron_pending.py``).

The end-to-end cases (a run held past a pending create, a restart, the tab's
save confirming) live in ``test_slot_create_publish_cron.py``. These pin
the store's own edges: a written run makes room under the cap, an entry that
cannot be written is held in memory, an unreadable entry is left alone, a retry
lands on the run's own entry, and the no-loop path does the same steps.
"""

from __future__ import annotations

import asyncio
import logging
import random
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard import chat_persistence, cron_pending
from kiro_crew.history import ConversationLog

#: Bound on every wait here; a passing run never reaches it.
WAIT = 30


class _State:
    """The one attribute the store reads off a dashboard state."""

    def __init__(self, log: ConversationLog) -> None:
        self.conversation_log = log


class _Slot:
    key = "cron-job1"

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def append(self, role: str, content: str, cls: str, meta: Any = None) -> None:
        self.messages.append({"role": role, "content": content, "cls": cls, "meta": meta})


def _job(ts: str) -> Any:
    return SimpleNamespace(id="job1", agent_id="default", last_result_ts=ts)


def _rows(text: str) -> list[Any]:
    return [("assistant", f"# Cron Job Result: nightly\n\n{text}", "msg msg-a", None)]


@pytest.fixture
def state(tmp_path: Any) -> _State:
    return _State(ConversationLog(base_dir=tmp_path / "sessions"))


def _entries(state: _State) -> list[str]:
    return sorted(p.stem for p in state.conversation_log.cron_pending_dir("job1").glob("*.json"))


async def _settle() -> None:
    await asyncio.wait_for(asyncio.gather(*cron_pending.writers()), WAIT)


@pytest.mark.asyncio
async def test_a_written_run_makes_room_under_the_cap(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the cap, the oldest run the transcript already holds gives way to a new one.

    Mutation pin: count only unwritten runs as evictable in ``_make_room`` and
    the third run is refused.
    """
    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", 2)
    for n in range(3):
        cron_pending.admit(state, _job(f"10{n}.5"), _rows(f"answer {n}"))
        await _settle()
    assert _entries(state) == ["101.5", "102.5"]
    assert cron_pending._INDEX[state]["job1"].refused == 0


@pytest.mark.asyncio
async def test_a_retry_of_a_run_lands_on_its_own_entry(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry is keyed by the run's result timestamp, so a retry is not a new run."""
    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", 1)
    for _ in range(2):
        cron_pending.admit(state, _job("100.5"), _rows("answer"))
        await _settle()
    assert _entries(state) == ["100.5"]


@pytest.mark.asyncio
async def test_a_run_refused_at_the_cap_still_reaches_its_transcript(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run refused past the cap is still appended to ``cron:{id}``, which the tab hydrates from.

    Mutation pin: skip the append in ``_hold_and_write`` when the run is not
    held and the refused run is missing from the transcript.
    """
    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", 1)
    log = state.conversation_log
    real_append = log.append_if_absent
    failing = {"on": True}

    def append_if_absent(key: str, *args: Any, **kwargs: Any) -> Any:
        if failing["on"]:
            raise OSError("transcript unavailable")
        return real_append(key, *args, **kwargs)

    monkeypatch.setattr(log, "append_if_absent", append_if_absent)
    cron_pending.admit(state, _job("100.5"), _rows("answer 0"))
    await _settle()
    failing["on"] = False
    cron_pending.admit(state, _job("101.5"), _rows("answer 1"))
    await _settle()
    assert cron_pending._INDEX[state]["job1"].refused == 1
    assert _entries(state) == ["100.5"]
    transcript = [str(r.get("content")) for r in log.read_messages("cron:job1")]
    assert _run_in(transcript, 1)


@pytest.mark.asyncio
async def test_an_entry_that_cannot_be_written_is_held_in_memory_and_merged(
    state: _State, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed entry write is an ERROR, and the rows are still shown on the next bind."""

    def refuse(path: Any, payload: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cron_pending, "_write_entry", refuse)
    with caplog.at_level(logging.ERROR, logger=cron_pending.logger.name):
        cron_pending.admit(state, _job("100.5"), _rows("answer"))
        await _settle()
    assert any("held in memory only" in r.getMessage() for r in caplog.records)
    assert _entries(state) == []

    async def saved(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", saved)
    slot = _Slot()
    await cron_pending.load(state, "job1")
    cron_pending.merge_into(state, "job1", slot)
    await _settle()
    assert [m["content"] for m in slot.messages] == [_rows("answer")[0][1]]
    assert cron_pending._INDEX[state]["job1"].refs == {}


@pytest.mark.asyncio
async def test_an_unreadable_entry_is_left_in_place(
    state: _State, caplog: pytest.LogCaptureFixture
) -> None:
    directory = state.conversation_log.cron_pending_dir("job1")
    directory.mkdir(parents=True)
    (directory / "100.5.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=cron_pending.logger.name):
        await cron_pending.load(state, "job1")
    assert any("unreadable" in r.getMessage() for r in caplog.records)
    assert cron_pending._INDEX[state]["job1"].refs == {}
    assert _entries(state) == ["100.5"]


@pytest.mark.asyncio
async def test_a_save_that_raises_keeps_the_merged_entry(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    cron_pending.admit(state, _job("100.5"), _rows("answer"))
    await _settle()

    async def raising(*args: Any, **kwargs: Any) -> bool:
        raise OSError("locked")

    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", raising)
    await cron_pending.load(state, "job1")
    cron_pending.merge_into(state, "job1", _Slot())
    await _settle()
    assert _entries(state) == ["100.5"]
    ref = cron_pending._INDEX[state]["job1"].refs["100.5"]
    assert not ref.merging and ref.rows is None


def test_with_no_running_loop_the_same_steps_run_inline(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No loop: the entry is written, the append tried, and the release saves then deletes."""
    cron_pending.admit(state, _job("100.5"), _rows("answer"))
    assert _entries(state) == ["100.5"]
    assert cron_pending._INDEX[state]["job1"].refs["100.5"].written
    contents = [r["content"] for r in state.conversation_log.read_messages("cron:job1")]
    assert contents == [_rows("answer")[0][1]]

    outcomes = iter([False, True])
    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", lambda *a, **k: next(outcomes))
    ref = cron_pending._INDEX[state]["job1"].refs["100.5"]
    ref.rows = _rows("answer")
    cron_pending.merge_into(state, "job1", _Slot())
    assert _entries(state) == ["100.5"]
    ref.rows = _rows("answer")
    cron_pending.merge_into(state, "job1", _Slot())
    assert _entries(state) == []


def _refuse_delete_of(monkeypatch: pytest.MonkeyPatch, name: str, times: int) -> list[str]:
    """Make deleting entry *name* raise ``PermissionError`` *times* times, as a held file does on Windows."""
    real = cron_pending._unlink
    refused: list[str] = []

    def unlink(path: Any) -> None:
        if path.stem == name and len(refused) < times:
            refused.append(path.stem)
            raise PermissionError(13, "file in use", str(path))
        real(path)

    monkeypatch.setattr(cron_pending, "_unlink", unlink)
    return refused


def _transcript(state: _State) -> list[str]:
    return [r["content"] for r in state.conversation_log.read_messages("cron:job1")]


@pytest.mark.asyncio
async def test_a_victim_that_cannot_be_deleted_keeps_the_new_run_and_itself(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the cap, a failed delete of the run giving way loses neither run.

    The new run is on disk, in ``cron:{id}`` and merged by the next bind. The
    victim stays listed outside the cap and its delete is retried by the next
    load. Mutation pin: drop the ``except OSError`` round the eviction delete
    and the new run is neither written nor appended.
    """
    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", 2)
    for n in range(2):
        cron_pending.admit(state, _job(f"10{n}.5"), _rows(f"answer {n}"))
        await _settle()
    refused = _refuse_delete_of(monkeypatch, "100.5", 2)
    cron_pending.admit(state, _job("102.5"), _rows("answer 2"))
    await _settle()

    assert refused == ["100.5"]
    assert _entries(state) == ["100.5", "101.5", "102.5"]
    assert _rows("answer 2")[0][1] in _transcript(state)
    refs = cron_pending._INDEX[state]["job1"].refs
    assert refs["100.5"].unremoved and refs["102.5"].written

    async def saved(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", saved)
    slot = _Slot()
    await cron_pending.load(state, "job1")
    assert refused == ["100.5", "100.5"]
    assert _entries(state) == ["100.5", "101.5", "102.5"]
    cron_pending.merge_into(state, "job1", slot)
    await _settle()
    assert [m["content"] for m in slot.messages] == [
        _rows("answer 1")[0][1],
        _rows("answer 2")[0][1],
    ]
    await cron_pending.load(state, "job1")
    assert _entries(state) == []
    assert cron_pending._INDEX[state]["job1"].refs == {}


@pytest.mark.asyncio
async def test_a_failed_delete_is_retried_by_the_next_admit(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A victim kept after a failed delete does not hold a cap place, and goes on the next admit."""
    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", 1)
    cron_pending.admit(state, _job("100.5"), _rows("answer 0"))
    await _settle()
    _refuse_delete_of(monkeypatch, "100.5", 1)
    cron_pending.admit(state, _job("101.5"), _rows("answer 1"))
    await _settle()
    assert _entries(state) == ["100.5", "101.5"]
    cron_pending.admit(state, _job("102.5"), _rows("answer 2"))
    await _settle()
    assert _entries(state) == ["102.5"]
    assert cron_pending._INDEX[state]["job1"].refused == 0
    assert _transcript(state)[-1] == _rows("answer 2")[0][1]


def test_with_no_running_loop_a_failed_delete_keeps_both_runs(
    state: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inline writer handles a failed eviction delete the same way, and retries it."""
    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", 1)
    cron_pending.admit(state, _job("100.5"), _rows("answer 0"))
    _refuse_delete_of(monkeypatch, "100.5", 1)
    cron_pending.admit(state, _job("101.5"), _rows("answer 1"))
    assert _entries(state) == ["100.5", "101.5"]
    assert _rows("answer 1")[0][1] in _transcript(state)
    assert cron_pending._INDEX[state]["job1"].refs["101.5"].written
    cron_pending.admit(state, _job("102.5"), _rows("answer 2"))
    assert _entries(state) == ["102.5"]


@pytest.mark.asyncio
async def test_join_writers_waits_for_a_held_run_to_reach_disk(state: _State) -> None:
    cron_pending.admit(state, _job("100.5"), _rows("answer"))
    await cron_pending.join_writers(WAIT)
    assert _entries(state) == ["100.5"]


def _run_in(contents: list[str], n: int) -> bool:
    return any(f"answer {n}" in c for c in contents)


@pytest.mark.parametrize("seed", range(300))
def test_no_held_run_is_lost_and_the_cap_holds_across_any_sequence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """Seeded sequences of runs, binds, saves and restarts, on the no-loop path.

    Each run's append lands or keeps failing, each tab save confirms or not, an
    entry delete can raise, and a restart drops the process's memory and the
    tab. Invariants after every step: the job never has more than the cap of
    entries on disk whose run is not yet in the transcript (none at all over it
    while no delete has failed), every refusal is counted, and every run that
    was not refused is in the transcript or on disk, so a restart can still
    show it.
    """
    rng = random.Random(seed)
    cap = rng.randint(1, 4)
    monkeypatch.setattr(cron_pending, "PENDING_RUNS_PER_JOB", cap)
    log = ConversationLog(base_dir=tmp_path / "sessions")
    state = _State(log)
    real_append = log.append_if_absent
    failing = {"on": False}

    def append_if_absent(key: str, *args: Any, **kwargs: Any) -> Any:
        if failing["on"]:
            raise OSError("transcript unavailable")
        return real_append(key, *args, **kwargs)

    monkeypatch.setattr(log, "append_if_absent", append_if_absent)
    saves = {"ok": True}

    def save(_state: Any, slot: _Slot, **kwargs: Any) -> bool:
        if not saves["ok"]:
            return False
        for m in slot.messages:
            real_append("cron:job1", m["role"], m["content"], cls=m["cls"])
        return True

    monkeypatch.setattr(chat_persistence, "_save_slot_to_history", save)
    real_unlink = cron_pending._unlink
    deletes = {"failed": 0, "rate": rng.choice([0.0, 0.3])}

    def unlink(path: Any) -> None:
        if rng.random() < deletes["rate"]:
            deletes["failed"] += 1
            raise PermissionError(13, "file in use", str(path))
        real_unlink(path)

    monkeypatch.setattr(cron_pending, "_unlink", unlink)
    tab = _Slot()
    kept: set[int] = set()
    refused = 0
    for n in range(rng.randint(4, 14)):
        op = rng.choice(["run", "run", "bind", "restart"])
        if op == "run":
            failing["on"] = rng.random() < 0.6
            before = cron_pending._INDEX.get(state, {}).get("job1")
            count = before.refused if before else 0
            cron_pending.admit(state, _job(f"{1000 + n}.5"), _rows(f"answer {n}"))
            failing["on"] = False
            if cron_pending._INDEX[state]["job1"].refused > count:
                refused += 1
            else:
                kept.add(n)
        elif op == "bind":
            saves["ok"] = rng.random() < 0.5
            asyncio.run(cron_pending.load(state, "job1"))
            cron_pending.merge_into(state, "job1", tab)
        else:
            cron_pending._INDEX.pop(state, None)
            tab = _Slot()
            refused = 0
        job = cron_pending._INDEX.get(state, {}).get("job1")
        assert (job.refused if job else 0) == refused
        transcript = [str(r.get("content")) for r in log.read_messages("cron:job1")]
        entries = [
            [str(row[1]) for row in (cron_pending._read_entry(p) or {"rows": []})["rows"]]
            for p in log.cron_pending_dir("job1").glob("*.json")
        ]
        on_disk = [content for entry in entries for content in entry]
        unshown = [e for e in entries if not all(c in transcript for c in e)]
        assert len(unshown) <= cap, (seed, op)
        if not deletes["failed"]:
            assert len(entries) <= cap, (seed, op)
        for k in kept:
            assert _run_in(transcript, k) or _run_in(on_disk, k), (seed, op, k)
