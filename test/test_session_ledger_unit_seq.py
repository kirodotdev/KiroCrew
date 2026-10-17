"""The ledger's landing proof tells a crew log it could not READ from an empty one.

``session_ledger`` proves an append landed by reading the unit's newest seq on both
sides of it (``_unit_last_seq``), and three decisions rest on that proof: whether the
update is acknowledged durable, whether the unit's precedence is published to the
slot's ``unit-order`` file, and whether a legacy carry is committed or released.
Answering 0 for a log that refused to open -- a seq a unit can really have -- would
let one transient refusal decide all three wrongly, and silently.

Both halves of every test are injected, so nothing here waits on a clock:

* the opener is the seam: ``session_ledger._projection`` is replaced by a view whose
  ``open_session_log`` refuses exactly the opens a test names and hands every other
  name to the real fold package. Only ``_unit_last_seq`` opens a log through it, so
  the fold and every other reader keep the real opener;
* the crew log writer is made synchronous, so an entry is on disk when its append
  returns and ``flush`` has nothing to wait for. The real writer drains on its own
  thread within a five-second budget, and a test that depended on that budget would
  be the flake this file exists to remove.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger as sl
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.errors import CODE_BAD_HEADER, CrewLogError

SLOT = "chat-1"
RETIRED = "acp-retired"
LIVE = "acp-live"

#: Builds the refusal an open raises. A FRESH exception per open, never one shared
#: instance: re-raising an instance extends its traceback, and a traceback kept alive
#: by a module global keeps every frame on it -- and any crew log handle in them.
_Refusal = Callable[[], BaseException]


def _sharing_violation() -> BaseException:
    """What Windows raises while another handle holds a file it cannot share."""
    return PermissionError(13, "The process cannot access the file")


def _vanished_segment() -> BaseException:
    return FileNotFoundError(2, "a segment vanished between being listed and opened")


def _lock_ceiling() -> BaseException:
    return OSError("ledger lock is held by another process; try again")


def _damaged_header() -> BaseException:
    return CrewLogError("crew log has no readable header line", code=CODE_BAD_HEADER)


class _Opener:
    """``open_session_log`` that refuses the next opens it is told to, then reads.

    ``refused`` counts only the refusals injected here. A real open can be refused too
    -- on Windows that is the very event under test -- and the code retries it, so an
    assertion on every open would count the host's refusals as well as the test's.
    """

    def __init__(self) -> None:
        self.opens = 0
        self.refused = 0
        self._refusals: list[_Refusal] = []
        self._forever: _Refusal | None = None

    def refuse(self, refusal: _Refusal, times: int = 1) -> None:
        self._refusals.extend([refusal] * times)

    def refuse_forever(self, refusal: _Refusal) -> None:
        self._forever = refusal

    def __call__(self, unit_id: str):
        self.opens += 1
        if self._refusals:
            self.refused += 1
            raise self._refusals.pop(0)()
        if self._forever is not None:
            self.refused += 1
            raise self._forever()
        return crew_log.open_session_log(unit_id)


class _ProjectionView:
    """The fold package as ``session_ledger`` sees it, with one opener swapped in."""

    def __init__(self, opener: _Opener) -> None:
        self.open_session_log = opener

    def __getattr__(self, name: str):
        return getattr(crew_log, name)


class _SyncWriter:
    """The crew log writer, synchronous: an entry is on disk when its append returns.

    ``after_append`` runs once the entry is written, which is how a test arms a
    refusal for the reads that follow the append and only those. ``losing`` drops the
    entry instead of writing it -- an append that never reached the file.
    """

    def __init__(self) -> None:
        self.after_append = lambda: None
        self.losing = False

    def on_ledger_recorded(self, session_id: str, data: dict) -> None:
        if not self.losing:
            CrewLog.open(lg.KIND_SESSION, session_id).append(
                sl.LEDGER_ENTRY_TYPE, data, src="gateway"
            )
        self.after_append()

    def flush(self, timeout: float = 0.0) -> bool:
        return True

    def dropped_writes(self) -> int:
        return 0


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Own data home with the crew log on, no writer state carried in or out, and no
    retry pause to wait out."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(sl, "_SEQ_READ_BACKOFF_SECS", 0.0)
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


@pytest.fixture
def on_windows(monkeypatch) -> None:
    """Classify refusals as Windows does, where ``PermissionError`` is a sharing violation.

    Only ``session_ledger``'s own binding is changed, so nothing else in the process
    starts taking Windows code paths.
    """
    monkeypatch.setattr(sl, "IS_WINDOWS", True)


@pytest.fixture
def opener(home, monkeypatch) -> _Opener:
    fake = _Opener()
    view = _ProjectionView(fake)
    monkeypatch.setattr(sl, "_projection", lambda: view)
    return fake


@pytest.fixture
def writer(home, monkeypatch) -> _SyncWriter:
    fake = _SyncWriter()
    monkeypatch.setattr(crew_log_emit, "on_ledger_recorded", fake.on_ledger_recorded)
    monkeypatch.setattr(crew_log_emit, "flush", fake.flush)
    monkeypatch.setattr(crew_log_emit, "dropped_writes", fake.dropped_writes)
    return fake


def _unit(unit_id: str) -> None:
    """One session crew log on SLOT, with the creating handle dropped at once."""
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=SLOT)


def _legacy_document(**fields) -> None:
    """A pre-projection ``state.json`` for SLOT, as the old writer left it."""
    directory = sl.ledger_dir(SLOT)
    directory.mkdir(parents=True, exist_ok=True)
    state = sl._empty_state()
    state.update(fields)
    (directory / sl._STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    (directory / sl._KEY_FILE).write_text(SLOT + "\n", encoding="utf-8")


def _a_slot_that_moved_on() -> None:
    """RETIRED recorded, then LIVE replaced it and recorded after it.

    The order file says LIVE is newest, so a read with no live session id answers
    LIVE's goal: the state every test below starts from.
    """
    _unit(RETIRED)
    _unit(LIVE)
    sl.record(SLOT, session_id=RETIRED, goal="from the retired session")
    sl.record(SLOT, session_id=LIVE, goal="the live conversation")
    assert sl._recorded_unit_order(SLOT) == (RETIRED, LIVE)


def _goal_without_a_live_session() -> str:
    """The goal a fold reads when only the order file can say which unit is newest."""
    crew_log.forget_slot_folds()
    return sl.read_state(SLOT)["goal"]


def _ledger_warnings(caplog) -> list[str]:
    """This module's own warnings, never another logger's that shared the window."""
    return [r.getMessage() for r in caplog.records if r.name == sl.logger.name]


# --------------------------------------------------------------------------- #
# _unit_last_seq
# --------------------------------------------------------------------------- #


def test_a_unit_with_no_log_reads_as_zero_not_as_unreadable(opener, caplog):
    """0 is a FACT about a unit that never wrote, so it is answered without a warning."""
    with caplog.at_level(logging.WARNING, logger=sl.logger.name):
        assert sl._unit_last_seq("acp-never-created") == 0
    assert opener.opens == 1
    assert _ledger_warnings(caplog) == []


def test_a_transient_refusal_is_retried_and_reads_the_real_seq(opener, writer, on_windows):
    _unit(LIVE)
    sl.record(SLOT, session_id=LIVE, goal="g")
    real = crew_log.open_session_log(LIVE).last_seq
    assert real > 0

    opener.refuse(_sharing_violation)
    assert sl._unit_last_seq(LIVE) == real
    assert opener.refused == 1


@pytest.mark.parametrize(
    ("refusal", "windows"),
    [(_sharing_violation, True), (_vanished_segment, True), (_vanished_segment, False)],
    ids=["sharing-violation-windows", "vanished-segment-windows", "vanished-segment-posix"],
)
def test_a_log_that_keeps_refusing_is_unreadable_after_a_bounded_retry(
    opener, caplog, monkeypatch, refusal, windows
):
    """``None``, never 0, and after exactly the named number of opens: bounded by count.

    A vanished segment is retried on every platform; only ``PermissionError`` is
    Windows-only (see the POSIX case below).
    """
    monkeypatch.setattr(sl, "IS_WINDOWS", windows)
    _unit(LIVE)
    opener.refuse_forever(refusal)
    with caplog.at_level(logging.WARNING, logger=sl.logger.name):
        assert sl._unit_last_seq(LIVE) is None
    assert opener.refused == sl._SEQ_READ_ATTEMPTS
    named = type(refusal()).__name__
    messages = _ledger_warnings(caplog)
    assert any("could not read this unit's crew log" in m and named in m for m in messages)


@pytest.mark.parametrize("refusal", [_lock_ceiling, _damaged_header], ids=["lock", "header"])
def test_a_refusal_a_retry_cannot_clear_is_unreadable_at_once(opener, on_windows, refusal):
    """The lock's own refusal already waited out its ceiling; a retry would wait it again."""
    _unit(LIVE)
    opener.refuse_forever(refusal)
    assert sl._unit_last_seq(LIVE) is None
    assert opener.refused == 1


def test_a_permission_error_off_windows_is_a_real_fault_and_is_not_retried(opener, monkeypatch):
    """On POSIX nothing shares a file away from a reader, so the error is answered at once."""
    monkeypatch.setattr(sl, "IS_WINDOWS", False)
    _unit(LIVE)
    opener.refuse_forever(_sharing_violation)
    assert sl._unit_last_seq(LIVE) is None
    assert opener.refused == 1


def test_an_unreadable_sample_never_counts_as_growth():
    assert sl._grew(3, 4)
    assert not sl._grew(4, 4)
    assert not sl._grew(None, 4)
    assert not sl._grew(3, None)
    assert not sl._grew(None, None)


# --------------------------------------------------------------------------- #
# record_update: durability and published precedence
# --------------------------------------------------------------------------- #


def test_a_transient_refusal_after_the_append_still_publishes_precedence(
    opener, writer, on_windows
):
    """One refused open after a landed append must not cost the live unit its place.

    Read as seq 0, the refusal would make the append look lost: LIVE would not move
    back to the end of the order file, RETIRED -- which recorded in between -- would
    stay newest, and every read with no live session id would answer its goal.
    """
    _a_slot_that_moved_on()
    # A delayed request from the retired session records, and genuinely is newest.
    sl.record(SLOT, session_id=RETIRED, next_step="late")
    assert sl._recorded_unit_order(SLOT) == (LIVE, RETIRED)

    writer.after_append = lambda: opener.refuse(_sharing_violation)
    _, durable = sl.record_update(SLOT, session_id=LIVE, goal="the live conversation, again")

    assert durable
    assert sl._recorded_unit_order(SLOT) == (RETIRED, LIVE)
    assert _goal_without_a_live_session() == "the live conversation, again"


def test_a_log_unreadable_after_the_append_is_not_durable_and_says_why(
    opener, writer, caplog, on_windows
):
    """Persistent unreadability is unproved, and the call names which read failed."""
    _a_slot_that_moved_on()
    sl.record(SLOT, session_id=RETIRED, next_step="late")

    writer.after_append = lambda: opener.refuse_forever(_sharing_violation)
    with caplog.at_level(logging.WARNING, logger=sl.logger.name):
        _, durable = sl.record_update(SLOT, session_id=LIVE, next_step="unproved")

    assert not durable
    assert sl._recorded_unit_order(SLOT) == (LIVE, RETIRED)
    # Every injected refusal came after the append: the bounded retry, and no more.
    assert opener.refused == sl._SEQ_READ_ATTEMPTS
    messages = _ledger_warnings(caplog)
    assert any("could not be read after the append" in m for m in messages), messages
    assert not any("refused an append" in m for m in messages), messages


def test_a_log_unreadable_on_both_sides_names_both_reads(opener, writer, caplog, on_windows):
    """The warning says which read failed, so when both did it names both."""
    _a_slot_that_moved_on()

    opener.refuse_forever(_sharing_violation)
    with caplog.at_level(logging.WARNING, logger=sl.logger.name):
        _, durable = sl.record_update(SLOT, session_id=LIVE, next_step="unproved")

    assert not durable
    assert sl._recorded_unit_order(SLOT) == (RETIRED, LIVE)
    assert opener.refused == 2 * sl._SEQ_READ_ATTEMPTS
    messages = _ledger_warnings(caplog)
    assert any("could not be read before and after the append" in m for m in messages), messages


def test_a_transient_refusal_before_the_append_is_not_read_as_an_empty_log(
    opener, writer, on_windows
):
    """The other direction: an append that did NOT land must not publish precedence.

    Read as seq 0 before the append, RETIRED's existing entries would make its unchanged
    log look grown: a lost update acknowledged durable, and RETIRED pinned newest over
    the live unit until LIVE records again.
    """
    _a_slot_that_moved_on()

    opener.refuse(_sharing_violation)
    writer.losing = True
    _, durable = sl.record_update(SLOT, session_id=RETIRED, goal="never written")

    assert not durable
    assert sl._recorded_unit_order(SLOT) == (RETIRED, LIVE)
    assert _goal_without_a_live_session() == "the live conversation"


def test_a_log_unreadable_before_the_append_withholds_precedence_until_a_proved_one(
    opener, writer, caplog, on_windows
):
    """Fail closed, and recover: the note is withheld rather than guessed.

    The append lands, but nothing proves it did, so the update is reported not durable
    and LIVE's precedence is not published. A missing note is the state a crash between
    an entry and its note already leaves, and LIVE's next proved record restores it.
    """
    _a_slot_that_moved_on()
    sl.record(SLOT, session_id=RETIRED, next_step="late")

    opener.refuse(_sharing_violation, times=sl._SEQ_READ_ATTEMPTS)
    with caplog.at_level(logging.WARNING, logger=sl.logger.name):
        _, durable = sl.record_update(SLOT, session_id=LIVE, goal="the live conversation, again")

    assert not durable
    assert sl._recorded_unit_order(SLOT) == (LIVE, RETIRED)
    assert any("could not be read before the append" in m for m in _ledger_warnings(caplog))

    _, durable = sl.record_update(SLOT, session_id=LIVE, next_step="proved")
    assert durable
    assert sl._recorded_unit_order(SLOT) == (RETIRED, LIVE)
    assert _goal_without_a_live_session() == "the live conversation, again"


# --------------------------------------------------------------------------- #
# The legacy carry's claim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("side", ["before", "after"])
def test_a_carry_whose_seq_could_not_be_read_keeps_its_claim(opener, writer, on_windows, side):
    """Unproved is not proved-lost: releasing would let a retry carry a second time.

    Either sample can be the unreadable one, and the carry lands both times, so only the
    claim that stays ``pending`` stops a retry whose own fold cannot read that log from
    carrying the document again.
    """
    _legacy_document(goal="owed")
    _unit(LIVE)

    if side == "before":
        opener.refuse(_sharing_violation, times=sl._SEQ_READ_ATTEMPTS)
    else:
        writer.after_append = lambda: opener.refuse_forever(_sharing_violation)
    with pytest.raises(sl.LedgerUnavailable):
        sl.record(SLOT, session_id=LIVE, next_step="n")

    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    assert marker.exists(), "the claim was released as if the carry were proved lost"
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_PENDING
    assert sl._claim_carry(SLOT) == sl._CLAIM_BUSY


def test_a_carry_with_a_transient_refusal_after_its_append_commits(opener, writer, on_windows):
    """A landed carry is committed through one refused open, and the update proceeds."""
    _legacy_document(goal="owed", phase="implementing")
    _unit(LIVE)

    arm_once = [True]

    def _refuse_after_the_carry_only() -> None:
        if arm_once:
            arm_once.clear()
            opener.refuse(_sharing_violation)

    writer.after_append = _refuse_after_the_carry_only
    _, durable = sl.record_update(SLOT, session_id=LIVE, next_step="then record normally")

    assert durable
    marker = sl.control_dir(SLOT) / sl._CARRIED_FILE
    assert marker.read_text(encoding="utf-8").strip() == sl._CARRY_COMMITTED
    state = sl.read_state(SLOT, LIVE)
    assert (state["goal"], state["phase"], state["next"]) == (
        "owed",
        "implementing",
        "then record normally",
    )
