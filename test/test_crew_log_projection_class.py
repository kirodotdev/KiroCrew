"""The ``class`` fold: the most restrictive class a session's log ever held.

A class that never moved folds to the opening one, a move only ever tightens it, and
a dropped write, a seq gap or an unreadable move marks the history damaged. Shared
logs, entries and fixture writers live in ``crew_log_projection_helpers.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from crew_log_projection_helpers import (  # noqa: F401 -- fixtures are used by name
    GATEWAY,
    _entries,
    _isolated_home,
    _log,
    _opened,
    _turn,
)

from kiro_crew.crew_log import CrewLog, CrewLogError
from kiro_crew.crew_log import projection as crew_log

# --------------------------------------------------------------------------- #
# class
# --------------------------------------------------------------------------- #


def _opened_with_class(handle: CrewLog, **members: Any) -> None:
    """``session/opened`` carrying a ``class`` object, the shape the emitter writes."""
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "dashboard:1",
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
            "class": {"memory": "persistent", **members},
        },
        src=GATEWAY,
    )


def _class_moved(handle: CrewLog, **members: Any) -> None:
    handle.append("session/class", {"memory": "persistent", **members}, src=GATEWAY)


def test_a_class_that_never_moved_folds_to_the_opening_one():
    """The ordinary session: recorded, complete, and nothing restrictive.

    This is the admitting case, so it is what stops the fold being satisfied by
    something that simply reports every log as restricted.
    """
    handle = _log()
    _opened_with_class(handle)

    value = crew_log.fold("class", _entries(handle))
    assert value["recorded"] is True
    assert value["complete"] is True
    assert value["channel"] is False
    assert value["app"] == ""
    assert value["memory"] == "persistent"


def test_a_class_folds_to_the_most_restrictive_value_it_ever_held():
    """MUTATION-SENSITIVE: restrictive-ever, not latest.

    The log is published to a channel for one turn and then unpublished. Those turns
    are still in this file, so a fold taking the LATEST value would report a log that
    holds a third party's words as readable. Each member is checked separately
    because they latch by different rules: ``channel`` is a flag, ``app`` keeps its
    first owner, ``memory`` keeps its first non-persistent mode.
    """
    handle = _log()
    _opened_with_class(handle)
    _class_moved(handle, channel=True, app="travel-desk", memory="incognito")
    _class_moved(handle)

    value = crew_log.fold("class", _entries(handle))
    assert value["channel"] is True, "a channel the log ever had stays recorded"
    assert value["app"] == "travel-desk", "an app that ever owned it stays recorded"
    assert value["memory"] == "incognito", "a non-persistent mode stays recorded"


def test_a_history_with_only_moves_is_recorded_but_not_complete():
    """MUTATION-SENSITIVE: ``complete`` is what says the history has a BEGINNING.

    Retention can take the segment carrying the opening entry. What survives states a
    class, so ``recorded`` is true and a reader keying on that alone would admit --
    while the earliest class this log held is unknown. ``complete`` is separately
    false, which is what a reader refuses on.
    """
    handle = _log()
    _class_moved(handle)

    value = crew_log.fold("class", _entries(handle))
    assert value["recorded"] is True
    assert value["complete"] is False


def test_an_opener_with_no_class_object_records_nothing():
    """A log written before the class was recorded, which must not read as unrestricted."""
    handle = _log()
    _opened(handle)

    value = crew_log.fold("class", _entries(handle))
    assert value["recorded"] is False
    assert value["complete"] is False


def test_a_class_object_with_no_memory_mode_is_refused_at_append():
    """``memory`` is required INSIDE the object, so a fragment never reaches a log.

    This is the strong half of the guarantee: the declaration enforces it, so a
    well-formed log cannot carry a class whose memory mode is unknown.
    """
    handle = _log()
    with pytest.raises(CrewLogError) as caught:
        handle.append(
            "session/opened",
            {
                "agent": "kirocrew",
                "slot": "dashboard:1",
                "model": "opus",
                "cwd": "/w",
                "owner": "raymond",
                "resumed": False,
                "class": {"channel": True},
            },
            src=GATEWAY,
        )
    assert "class.memory" in str(caught.value)


def test_a_recorded_dropped_write_marks_the_history_damaged():
    """MUTATION-SENSITIVE: the log's own admission that an append was lost.

    The writer sheds nothing on backpressure, but it has hard ceilings for a
    filesystem that has stopped answering, and an entry refused there is recorded as
    ``write/dropped``. For a fold whose answer is an authorization ceiling that is a
    hole: the lost append may have been the class move that restricted this session,
    and nothing else records it.

    This is what lets the RECORDER be best-effort where it is called. A class move is
    handed to the writer without waiting, matching the listener contract it runs
    under, and if it never reaches the file the log says so here -- so a move that was
    lost cannot leave the log reading as permissive.
    """
    handle = _log()
    _opened_with_class(handle)
    handle.append("write/dropped", {"dropped_count": 1, "dropped_bytes": 120}, src=GATEWAY)

    value = crew_log.fold("class", _entries(handle))
    assert value["damaged"] is True
    assert value["complete"] is True, "the beginning is intact; what is lost came later"


def test_a_gap_in_the_seqs_marks_the_history_damaged():
    """MUTATION-SENSITIVE: a hole in the MIDDLE, which the short-fold check cannot see.

    The store skips an unreadable interior line on purpose and does not check interior
    seq continuity -- one damaged record must not make a whole file unreadable. That is
    right for a fold accumulating totals and wrong for this one: the skipped line can be
    the sole record of a restriction, and the fold still reaches the file's tail, so
    nothing stopped early and the short-fold refusal never fires. Only this fold's own
    contiguity check sees it.

    The entries are handed to the fold directly because a skipped line is exactly what
    no writer produces: the seq that is missing was never delivered.
    """
    from kiro_crew.crew_log.schema import Entry

    opener = Entry(
        seq=1,
        time=1000,
        type="session/opened",
        src=GATEWAY,
        data={"class": {"memory": "persistent"}},
    )
    after_the_hole = Entry(
        seq=3,
        time=1002,
        type="session/class",
        src=GATEWAY,
        data={"memory": "persistent"},
    )

    value = crew_log.fold("class", [opener, after_the_hole])
    assert value["damaged"] is True, "seq 2 was never delivered, so a record is missing"
    assert value["complete"] is True, "the log's beginning is intact; the hole is later"
    assert value["channel"] is False, (
        "the fold cannot invent what the lost line said -- which is why the reader "
        "refuses on damage rather than on the value"
    )


def test_a_class_move_that_cannot_be_read_marks_the_history_damaged():
    """MUTATION-SENSITIVE: a move whose content is unreadable is a hole, not a no-op.

    Append validation refuses a fragment, so this line can only come from damage -- and
    what a move entry says is its whole content, so skipping it discards the transition.
    The direction it discards is always toward permissive, because a class move is worth
    writing only when it restricts.
    """
    from kiro_crew.crew_log.schema import Entry

    opener = Entry(
        seq=1,
        time=1000,
        type="session/opened",
        src=GATEWAY,
        data={"class": {"memory": "persistent"}},
    )
    unreadable_move = Entry(
        seq=2,
        time=1001,
        type="session/class",
        src=GATEWAY,
        data={"channel": True},
    )

    value = crew_log.fold("class", [opener, unreadable_move])
    assert value["damaged"] is True
    assert value["channel"] is False, "the unreadable move was not absorbed"


def test_an_absent_class_object_is_a_date_not_a_hole():
    """MUTATION-SENSITIVE: the control that keeps damage from swallowing AGE.

    An opener with no ``class`` object at all is a log written before the field existed.
    That is answered by ``complete`` staying false, and it must not also read as damaged:
    if it did, every pre-field log would report a hole and the two reasons a reader
    refuses -- too old to say, and missing a record -- would be indistinguishable in the
    log. An unreadable object that is PRESENT is the damaged case, and the test above
    covers it.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)

    value = crew_log.fold("class", _entries(handle))
    assert value["damaged"] is False, "an absent object is a date, not a hole"
    assert value["complete"] is False


def test_the_reader_treats_a_memory_less_class_as_nothing_stated():
    """MUTATION-SENSITIVE: the reader defends itself rather than trusting the writer.

    Append validation binds the WRITER, and a damaged segment or a planted line is
    exactly the input that ignores it -- so the entries are handed to the fold
    directly here, which is the only way to reach that line. A fragment must read as
    nothing stated rather than as a class whose memory mode is unknown, otherwise it
    would satisfy a reader's recorded-and-complete test.
    """
    from kiro_crew.crew_log.schema import Entry

    planted = Entry(
        seq=1,
        time=1000,
        type="session/opened",
        src=GATEWAY,
        data={"class": {"channel": True}},
    )

    value = crew_log.fold("class", [planted])
    assert value["recorded"] is False
    assert value["complete"] is False
    assert value["channel"] is False, "no member is read off an object with no memory mode"
    assert value["damaged"] is True, (
        "the object is present and unreadable, which is a hole rather than a date -- "
        "so a reader refuses on it even though nothing was stated"
    )


def test_a_later_opener_cannot_date_a_log_whose_first_one_stated_no_class():
    """MUTATION-SENSITIVE: only the log's FIRST opener may supply its beginning.

    A log carries an opening entry PER RE-ATTACHMENT, so an old log written before the
    class was recorded gains a later opener that does state one the moment a current
    build re-attaches to it. Reading that as the beginning would date the log by an
    entry written long after the stretch whose class is unknown -- and everything in
    that stretch is still in this file. ``complete`` therefore stays false, and the
    reader refuses, even though the log now visibly states a class.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    _opened_with_class(handle)

    value = crew_log.fold("class", _entries(handle))
    assert value["recorded"] is True, "the later opener did state a class"
    assert (
        value["complete"] is False
    ), "a later opener must not supply a beginning the log's own first one did not"


def test_a_resume_cannot_widen_a_class_the_log_already_moved_away_from():
    """A re-attach writes ``session/opened`` again, and it must not reset the history.

    The same file gets a second opener with a clean class, which is truthful about the
    session as re-attached and says nothing about the turns already in the log. The
    fold is ever-held, so the earlier channel survives it.
    """
    handle = _log()
    _opened_with_class(handle)
    _class_moved(handle, channel=True)
    _opened_with_class(handle)

    value = crew_log.fold("class", _entries(handle))
    assert value["channel"] is True
    assert value["complete"] is True
