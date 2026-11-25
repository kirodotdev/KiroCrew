"""Conductor mode "Pause all" at the pause file's size cap.

An owed turn the file refused is kept in memory beside the stored records, so a
write that shrinks the file, or a refused Continue or Resume all putting members
back, still fits, and Resume all still continues that turn.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_crew_pause import _pause_store, _restart  # noqa: F401 - the autouse pause-store reset

from kiro_crew import crew_pause

#: A member key long enough that storing its owed turn takes more bytes than a
#: small crew's record frees.
_LONG = "chat-" + "m" * 400


def _owe_at_the_cap(monkeypatch: Any, key: str = _LONG) -> None:
    """Fill the pause file to the cap, then have the gate end *key*'s held turn."""
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", crew_pause._path().stat().st_size)
    assert crew_pause.owe_turn(key) is True
    stored = json.loads(crew_pause._path().read_text(encoding="utf-8"))["paused"]
    assert key not in stored["root"]["owed"]  # the cap refused it: memory only


def test_a_write_that_shrinks_the_file_fits_while_an_owed_turn_is_kept_in_memory(monkeypatch):
    crew_pause.pause("root", ["w1", _LONG], ["w1"])
    crew_pause.pause("small", [], [])
    _owe_at_the_cap(monkeypatch)
    # Resume all on the other, smaller crew and a member's own Continue both fit.
    assert crew_pause.resume("small") is not None
    assert crew_pause.release("w1")
    _restart()
    assert crew_pause.record("small") is None
    assert crew_pause.record("root")["held"] == [_LONG]


def test_resume_all_continues_an_owed_turn_kept_in_memory(monkeypatch):
    crew_pause.pause("root", ["w1", _LONG], ["w1"])
    _owe_at_the_cap(monkeypatch)
    assert crew_pause.record("root")["owed"] == [_LONG]
    assert crew_pause.roots_naming(_LONG) == ["root"]
    assert crew_pause.resume("root")["stopped"] == sorted(["w1", _LONG])
    # Stored as resuming, so a restart before its hand-back holds it again.
    _restart()
    assert _LONG in crew_pause.record("root")["stopped"]


def test_a_released_owed_turn_does_not_strip_a_later_pauses_fresh_stop(monkeypatch):
    crew_pause.pause("root", ["w1", "w2"], ["w1"])
    _owe_at_the_cap(monkeypatch, "w2")
    assert crew_pause.release("w2")["root"]["memory_only"] == {
        "owed": ["w2"],
        "stopped": ["w2"],
    }
    assert crew_pause._memory_only == {}
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", 256 * 1024)
    crew_pause.pause("root", ["w1", "w2"], ["w2"])
    _restart()
    assert crew_pause.record("root")["stopped"] == ["w1", "w2"]


@pytest.mark.parametrize("record_ends", [False, True])
def test_a_refused_continue_puts_back_an_owed_turn_kept_in_memory(monkeypatch, record_ends):
    if record_ends:
        # A Resume all left only this member held: its Continue ends the record.
        crew_pause.keep_pending("root", [_LONG], root_pending=False)
    else:
        crew_pause.pause("root", ["w1", _LONG], ["w1"])
    _owe_at_the_cap(monkeypatch)
    removed = crew_pause.release(_LONG)
    assert (removed["root"]["dropped"] is not None) is record_ends
    # The Continue is refused: putting the member back fits under the cap.
    crew_pause.rehold(_LONG, removed)
    rec = crew_pause.record("root")
    assert _LONG in rec["held"] and _LONG in rec["stopped"] and rec["owed"] == [_LONG]
    assert crew_pause.holds_turn(_LONG)
    assert _LONG in crew_pause.resume("root")["stopped"]


def test_a_refused_resume_holds_an_owed_turn_kept_in_memory_again(monkeypatch):
    crew_pause.pause("root", ["w1", _LONG], ["w1"])
    crew_pause.pause("small", [], [])
    _owe_at_the_cap(monkeypatch)
    rec = crew_pause.resume("root")
    assert rec["memory_only"] == [_LONG]
    # No hand-back happened: both are held again for the next Resume all, and the
    # stop the file had refused comes back in memory only, so the write fits.
    pending = [k for k in rec["stopped"] if k != "root"]
    assert crew_pause.forget_resuming(
        rec["stopped"], root="root", pending=pending, memory_only=rec["memory_only"]
    )
    assert crew_pause.record("root")["stopped"] == sorted(["w1", _LONG])
    assert crew_pause.holds_turn(_LONG)
    # A later write that shrinks the file still fits.
    assert crew_pause.resume("small") is not None
    # The file had no room for _LONG's stop: a restart forgets it, as before.
    _restart()
    assert crew_pause.record("root")["stopped"] == ["w1"]
    assert crew_pause.holds_turn(_LONG)


def test_a_refused_resume_stores_a_refused_stop_once_the_file_fits_it(monkeypatch):
    crew_pause.pause("root", ["w1", _LONG], ["w1"])
    _owe_at_the_cap(monkeypatch)
    rec = crew_pause.resume("root")
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", 256 * 1024)
    pending = [k for k in rec["stopped"] if k != "root"]
    # Another member's Continue is open: that holds back other memory-only turns, not this one.
    with crew_pause.continuing("other"):
        assert crew_pause.forget_resuming(
            rec["stopped"], root="root", pending=pending, memory_only=rec["memory_only"]
        )
    _restart()
    assert crew_pause.record("root")["stopped"] == sorted(["w1", _LONG])


def test_an_owed_turn_kept_in_memory_is_stored_once_the_file_has_room(monkeypatch):
    crew_pause.pause("root", ["w1", "w2"], ["w1"])
    crew_pause.pause("big", [f"chat-member-{i}" for i in range(40)], [])
    _owe_at_the_cap(monkeypatch, "w2")
    # Resume all on a crew with nothing stopped has nothing to hold again, so the
    # room it frees stores the owed turn.
    assert crew_pause.resume("big") is not None
    assert crew_pause._memory_only == {}
    _restart()
    assert crew_pause.record("root")["owed"] == ["w2"]


@pytest.mark.parametrize("next_write", ["continue", "resume_all"])
def test_an_owed_turn_the_disk_refused_is_stored_by_the_next_continue_or_resume(
    monkeypatch, next_write
):
    crew_pause.pause("root", ["w1", "w2"], ["w1"])
    crew_pause.pause("other", ["o1"], ["o1"])

    def _fail(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(crew_pause, "atomic_write", _fail)
        assert crew_pause.owe_turn("w2") is True
    # The disk recovers; the next write is another member's Continue or Resume all.
    if next_write == "continue":
        assert crew_pause.release("w1")
    else:
        assert crew_pause.resume("other") is not None
    assert crew_pause._memory_only == {}
    _restart()
    assert crew_pause.record("root")["owed"] == ["w2"]


def test_a_stop_a_later_pause_stored_stays_stored_through_a_refused_continue(monkeypatch):
    crew_pause.pause("root", ["w1", "w2"], ["w1"])
    _owe_at_the_cap(monkeypatch, "w2")
    # Room for a second Pause all to store w2's stop, but not its owed turn too.
    stopped = crew_pause._copy_records()
    stopped["root"]["stopped"] = ["w1", "w2"]
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", 1 << 20)
    size = len(crew_pause._body(stopped, {}).encode("utf-8"))
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", size)
    crew_pause.pause("root", ["w1", "w2"], ["w2"])
    assert crew_pause._memory_only == {"root": {"owed": ["w2"]}}
    removed = crew_pause.release("w2")
    crew_pause.rehold("w2", removed)  # its Continue is refused
    _restart()
    assert crew_pause.record("root")["stopped"] == ["w1", "w2"]


def test_a_refused_continue_fits_back_beside_another_owed_turn_kept_in_memory(monkeypatch):
    crew_pause.pause("root", ["w2", _LONG], [_LONG])
    _owe_at_the_cap(monkeypatch, "w2")
    # Its Continue frees room, which stays free: w2 is still memory only.
    removed = crew_pause.release(_LONG)
    assert crew_pause._memory_only == {"root": {"owed": ["w2"], "stopped": ["w2"]}}
    crew_pause.rehold(_LONG, removed)
    rec = crew_pause.record("root")
    assert rec["stopped"] == sorted(["w2", _LONG]) and _LONG in rec["held"]
    _restart()
    assert _LONG in crew_pause.record("root")["stopped"]


def test_another_crews_continue_stores_no_memory_only_turn_while_a_continue_is_open(monkeypatch):
    owed = "chat-" + "b" * 100  # smaller than _LONG's record, bigger than c1's entries
    # Crew a: a Resume all left only _LONG held, so its Continue ends the record.
    crew_pause.keep_pending("a", [_LONG], root_pending=False)
    crew_pause.pause("b", ["b1", owed], ["b1"])
    crew_pause.pause("c", ["c1", "c2"], ["c1"])
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", crew_pause._path().stat().st_size)
    assert crew_pause.owe_turn(owed) is True  # the cap refused it: memory only
    with crew_pause.continuing(_LONG):
        removed = crew_pause.release(_LONG)
        # Crew c's own Continue lands while a's is still being decided: b's owed
        # turn stays memory only, so the room a freed is still free.
        with crew_pause.continuing("c1"):
            assert crew_pause.release("c1")
        assert crew_pause._memory_only == {"b": {"owed": [owed], "stopped": [owed]}}
        # a's Continue is refused: a comes back within the cap.
        crew_pause.rehold(_LONG, removed)
    assert crew_pause.holds_turn(_LONG) and crew_pause.record("b")["owed"] == [owed]
    stored = json.loads(crew_pause._path().read_text(encoding="utf-8"))["paused"]
    assert stored["a"]["held"] == [_LONG]
    # Both Continues are decided: the next write stores b's owed turn once it fits.
    monkeypatch.setattr(crew_pause, "MAX_FILE_BYTES", 256 * 1024)
    assert crew_pause.release("c2")
    _restart()
    assert _LONG in crew_pause.record("a")["held"] and crew_pause.record("b")["owed"] == [owed]
