"""A superseded unit another slot's work ledger read still folds is kept until that slot carries.

The shipped shape: a dashboard chat ``chat-x`` reset once while it was open, so its second
unit ``a2`` carries the chat's ledger and its first unit ``a1`` is closed ``superseded``.
The chat is then gone and a workflow's result lands in a fallback tab ``workflow-<run>``
linked to ``dashboard:chat-x``. The tab's ledger callers read under ``chat-x``, and that
read folds the ``chat-x`` units first and then the tab's own, applying the tab's carries
and never the chat's. So the chat's records reach the tab from ``a1`` itself until the tab
holds a ledger carry of its own.

Two halves keep them reading. The tab's FIRST unit carries the ledger over that join, with
the chat's own carry applied inside the chat's units, so it holds the chat's records
however many of the chat's units are left. And the sweep keeps ``a1`` while a slot that
joins ``chat-x`` holds no ledger carry, reading only the crew log tree.

Every record goes through ``session_ledger.record_update`` and every unit through the live
emitter, with the session key the dashboard's turn admission states. Unit ids are per
test, for the store header memo reason ``test_crew_log_slot_carry.py`` gives.
"""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog, eager, emit, projection
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store
from kiro_crew.session_ledger import _store_name
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

CHAT = "chat-x"
CHAT_KEY = "dashboard:chat-x"
#: What the fallback tab reads under ``chat-x``: the chat's goal and next step and both
#: of its events, then the tab's own.
EXPECTED = ("g-chat", "n2", ["e1", "e2", "e3"])
#: What the chat's own read shows.
CHAT_EXPECTED = ("g-chat", "n2", ["e1", "e2"])


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Through the floor's own ``MonkeyPatch``, so a test's ``monkeypatch.undo()`` keeps it."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter with a session-key probe that maps nothing.

    The carry's fold runs inline, so a drain covers the opening, the carry and the closes.
    A test that needs a fold held back replaces ``_submit_carry_fold`` itself.
    """
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    emit.set_session_key_probe(lambda session_id: None)
    try:
        yield
    finally:
        emit.set_session_key_probe(None)
        settle()
        tree = stp.projection()
        stp.reset_for_tests()
        wait_until(
            lambda: not tree._write_scheduled,
            describe=lambda: "the session tree's debounced checkpoint is still sleeping",
        )


def _view(state: dict) -> tuple:
    return state["goal"], state["next"], [event["text"] for event in state["events"]]


def _open(unit_id: str, slot: str, *, previous: str = "", key: str = CHAT_KEY, **kwargs) -> None:
    emit.on_session_opened(
        unit_id, agent="kirocrew", slot=slot, previous_sid=previous, session_key=key, **kwargs
    )
    assert_drained()


def _chat_reset_once(tag: str, *, records: bool = True) -> tuple[str, str]:
    """``chat-x`` records into ``a1``, moves to ``a2`` (its carry closes ``a1``), records again."""
    a1, a2 = f"{tag}-a1", f"{tag}-a2"
    _open(a1, CHAT)
    if records:
        session_ledger.record_update(CHAT, session_id=a1, goal="g-chat", event="e1")
        assert_drained()
    _open(a2, CHAT, previous=a1)
    if records:
        session_ledger.record_update(CHAT, session_id=a2, next_step="n2", event="e2")
        assert_drained()
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, a1))
    return a1, a2


def _tab_records(unit_id: str) -> None:
    session_ledger.record_update(CHAT, session_id=unit_id, event="e3")
    assert_drained()


def _read(live: str = "") -> tuple:
    projection.forget_slot_folds()
    return _view(session_ledger.read_state(CHAT, live))


def _carried(unit_id: str) -> list[dict]:
    path = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit_id) / "log.jsonl"
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [entry["data"] for entry in entries if entry.get("type") == "session/carried"]


def _ledger_carries(unit_id: str, slot: str) -> int:
    return sum(
        1 for data in _carried(unit_id) if data.get("fold") == "ledger" and data.get("slot") == slot
    )


def _settle_eager() -> None:
    # The eager folder holds a unit's non-sole lease while it folds a wake, and the
    # sweep's sole lease then reads as owned, so it settles first.
    assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS), "the eager folder never settled"


def _sweep_now() -> tuple[int, int]:
    return store.sweep_expired(0, now=time.time() + 60)


def _sweep() -> tuple[int, int]:
    _settle_eager()
    return _sweep_now()


@pytest.mark.usefixtures("carrying")
def test_the_tabs_first_unit_carries_the_chats_ledger_so_the_chats_older_unit_may_go():
    """The rv8 recipe: ``a1`` is collected while the tab is still on its first unit.

    The tab's first unit carries the ledger, and only the ledger, over the ``chat-x``
    units, so the read under ``chat-x`` keeps the chat's goal and first event after
    ``a1`` is gone. The tab's next units carry that on, each over the carry before it,
    after the unit holding that carry is collected too.
    """
    a1, _a2 = _chat_reset_once("first")
    tab, w1, w2, w3 = "workflow-first", "first-w1", "first-w2", "first-w3"
    _open(w1, tab)
    _tab_records(w1)

    assert [(data["fold"], data["slot"]) for data in _carried(w1)] == [("ledger", tab)]
    before = _read(w1)
    assert _sweep() == (1, 0)
    assert not CrewLog.exists(lg.KIND_SESSION, a1)
    after = _read(w1)
    chat_after = _read()
    _open(w2, tab, previous=w1)
    after_its_carry = _read(w2)
    assert _sweep() == (1, 0)
    assert not CrewLog.exists(lg.KIND_SESSION, w1)
    _open(w3, tab, previous=w2)
    after_the_next = _read(w3)

    assert before == EXPECTED
    assert after == EXPECTED
    assert after_its_carry == EXPECTED
    assert after_the_next == EXPECTED
    assert chat_after == CHAT_EXPECTED


@pytest.mark.usefixtures("carrying")
def test_the_sweep_keeps_the_chats_older_unit_until_the_tabs_first_carry_lands(monkeypatch):
    """Until then the tab's read folds the chat's records from ``a1`` itself.

    The tab's fold is held back, the window between its opening and its carry, and the
    sweep keeps ``a1``. Once the carry lands the next sweep collects it.
    """
    a1, _a2 = _chat_reset_once("held")
    tab, w1 = "workflow-held", "held-w1"
    held: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", held.append)
    _open(w1, tab)
    _tab_records(w1)

    assert len(held) == 1
    assert _ledger_carries(w1, tab) == 0
    assert _sweep() == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, a1)
    kept = _read(w1)
    held[0]()
    assert_drained()
    assert _ledger_carries(w1, tab) == 1
    assert _sweep() == (1, 0)
    after = _read(w1)

    assert kept == EXPECTED
    assert after == EXPECTED
    assert not CrewLog.exists(lg.KIND_SESSION, a1)


@pytest.mark.usefixtures("carrying")
def test_the_sweep_keeps_the_link_between_a_kept_unit_and_the_unit_that_carries_it(monkeypatch):
    """``a2`` holds no record, but it is the chain between kept ``a1`` and carrying ``a3``.

    Collected, ``a3`` would be a chain root ordered against ``a1`` by the header clock, and
    a backward step would fold ``a1`` after the carry. The sweep keeps ``a2`` while ``a1``
    stays, and collects both once the tab's first carry lands.
    """
    a1, a2, a3 = "link-a1", "link-a2", "link-a3"
    _open(a1, CHAT)
    session_ledger.record_update(CHAT, session_id=a1, goal="g-chat", event="e1")
    assert_drained()
    _open(a2, CHAT, previous=a1)
    _open(a3, CHAT, previous=a2)
    session_ledger.record_update(CHAT, session_id=a3, next_step="n2", event="e2")
    assert_drained()
    tab, w1 = "workflow-link", "link-w1"
    held: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", held.append)
    _open(w1, tab)
    _tab_records(w1)

    assert _sweep() == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, a1)
    assert CrewLog.exists(lg.KIND_SESSION, a2)
    held[0]()
    assert_drained()
    assert _sweep() == (2, 0)
    assert not CrewLog.exists(lg.KIND_SESSION, a1)
    assert not CrewLog.exists(lg.KIND_SESSION, a2)


@pytest.mark.usefixtures("carrying")
def test_a_tab_that_joins_after_the_chats_older_unit_went_still_reads_its_records():
    """``a1`` went while nothing joined ``chat-x``; the chat's own carry in ``a2`` holds it.

    The tab's first carry applies that carry inside the ``chat-x`` units, so its read keeps
    the chat's goal and first event, and so does the tab's next carry.
    """
    a1, _a2 = _chat_reset_once("late")
    assert _sweep() == (1, 0)
    assert not CrewLog.exists(lg.KIND_SESSION, a1)
    tab, w1, w2 = "workflow-late", "late-w1", "late-w2"
    _open(w1, tab)
    _tab_records(w1)
    tab_read = _read(w1)
    _open(w2, tab, previous=w1)
    after_its_carry = _read(w2)

    assert tab_read == EXPECTED
    assert after_its_carry == EXPECTED
    assert _read() == CHAT_EXPECTED


@pytest.mark.usefixtures("carrying")
def test_an_older_unit_resumed_under_the_chats_key_joins_it_from_its_newest_opening():
    """A unit an older build opened states no key on line 2; its resume by this one does.

    The tab never opened a unit of its own in this build, so it holds no carry, and its
    resumed unit's newest opening is what says its read joins ``chat-x``.
    """
    a1, _a2 = _chat_reset_once("resumed")
    tab, w1 = "workflow-resumed", "resumed-w1"
    _open(w1, tab, key="")
    # A new gateway process re-attaches: caches gone, the file is all that is left.
    emit.reset_caches()
    _open(w1, tab, resumed=True)
    _tab_records(w1)

    assert store.unit_opened_session_key(lg.KIND_SESSION, w1) == ""
    assert _ledger_carries(w1, tab) == 0
    assert _sweep() == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, a1)
    assert _read(w1) == EXPECTED


@pytest.mark.usefixtures("carrying")
def test_a_superseded_unit_with_no_ledger_record_goes_whoever_joins_it(monkeypatch):
    """No ledger read can lose anything from a unit that holds no ledger record."""
    a1, _a2 = _chat_reset_once("bare", records=False)
    held: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", held.append)
    _open("bare-w1", "workflow-bare")

    assert _sweep() == (1, 0)
    assert not CrewLog.exists(lg.KIND_SESSION, a1)


@pytest.mark.usefixtures("carrying")
def test_the_superseded_units_no_other_slot_joins_are_collected():
    """The chat's own key's ledger spelling is the chat, so nothing joins it."""
    a1, a2 = _chat_reset_once("alone")

    assert _sweep() == (1, 0)
    assert not CrewLog.exists(lg.KIND_SESSION, a1)
    assert CrewLog.exists(lg.KIND_SESSION, a2)
    assert _read() == CHAT_EXPECTED


@pytest.mark.usefixtures("carrying")
def test_a_raw_keys_own_older_unit_is_kept_while_the_slot_named_from_it_has_no_carry(
    monkeypatch,
):
    """The header half: ``cron-42`` is the tab the gateway names from ``cron:42``.

    Units headed with the raw key carried and closed their own older one, and the tab's
    unit states its own dashboard key, so only the header names the join. Until the tab's
    first carry lands, its read folds the raw key's units as they are, so the sweep keeps
    the closed one.
    """
    raw, tab = "cron:42", "cron-42"
    r1, r2, w1 = "raw-r1", "raw-r2", "raw-w1"
    _open(r1, raw, key=raw)
    session_ledger.record_update(raw, session_id=r1, goal="g-raw", event="r-event")
    assert_drained()
    _open(r2, raw, previous=r1, key=raw)
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, r1))
    held: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", held.append)
    _open(w1, tab, key=f"dashboard:{tab}")

    assert len(held) == 1
    assert _sweep() == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, r1)
    projection.forget_slot_folds()
    assert _view(session_ledger.read_state(raw, w1)) == ("g-raw", "", ["r-event"])
    held[0]()
    assert_drained()
    assert _ledger_carries(w1, tab) == 1
    assert _sweep() == (1, 0)
    projection.forget_slot_folds()
    assert _view(session_ledger.read_state(raw, w1)) == ("g-raw", "", ["r-event"])


@pytest.mark.usefixtures("carrying")
@pytest.mark.parametrize("fault", ["listing-raises", "scan-raises", "close-unseen"])
def test_joins_that_cannot_be_read_keep_the_unit(monkeypatch, fault):
    a1, _a2 = _chat_reset_once(f"fault-{fault}")
    held: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", held.append)
    _open(f"fault-{fault}-w1", f"workflow-fault-{fault}")
    from kiro_crew.crew_log import retention_joins

    def _raise(*_args, **_kwargs):
        raise OSError("cannot be read")

    with pytest.MonkeyPatch.context() as patch:
        if fault == "listing-raises":
            patch.setattr(store, "session_units_by_slot", _raise)
        elif fault == "scan-raises":
            patch.setattr(retention_joins, "_holds", _raise)
        else:
            # The second read of the unit's close sees none; the join check still runs.
            patch.setattr(store, "_newest_close", lambda _directory: None)
        swept = _sweep()

    assert swept == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, a1)


@pytest.mark.usefixtures("carrying")
def test_a_unit_whose_header_cannot_be_proved_does_not_hold_the_others_back():
    """No spelling's listing names such a unit, so no ledger read folds it."""
    import shutil

    a1, a2 = _chat_reset_once("stray")
    root = lg.crew_log_root(lg.KIND_SESSION)
    shutil.copytree(root / _store_name(a2), root / "stray-copy-of-a2")
    with pytest.raises(lg.CrewLogError):
        store.session_units_by_slot(strict=True)

    assert _sweep() == (1, 0)
    assert not CrewLog.exists(lg.KIND_SESSION, a1)
    assert (root / "stray-copy-of-a2").is_dir()


@pytest.mark.usefixtures("carrying")
def test_the_join_check_reads_no_file_outside_the_log_tree(monkeypatch):
    """Like the rest of the sweep: nothing an agent can write keeps or drops a unit."""
    a1, _a2 = _chat_reset_once("tree")
    held: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", held.append)
    _open("tree-w1", "workflow-tree")
    _tab_records("tree-w1")
    from kiro_crew.crew_log import projection_kernel, retention_joins  # noqa: F401

    root = lg.crew_log_root(lg.KIND_SESSION).resolve()
    opened: list[str] = []
    real_open = io.open

    def _record_open(file, *args, **kwargs):
        opened.append(os.path.realpath(str(file)))
        return real_open(file, *args, **kwargs)

    _settle_eager()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(io, "open", _record_open)
        patch.setattr("builtins.open", _record_open)
        swept = _sweep_now()

    assert swept == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, a1)
    assert opened, "the join check read nothing -- the scan went blind"
    strays = [path for path in opened if not path.startswith(str(root))]
    assert strays == [], f"the join check read outside the crew log tree: {strays}"


def test_a_join_carries_nothing_when_a_later_spelling_holds_its_own_carry():
    """Only the leading spelling's own carry can replace the state exactly.

    The tab joins two spellings, ``chat-a`` first, and ``chat-b``'s unit carries
    ``chat-b``'s ledger. Replacing the state there would drop ``chat-a``'s record, so the
    joined fold answers ``None`` and the carry stands down. With ``chat-b`` alone it applies.
    """

    def _unit(unit_id: str, slot: str) -> CrewLog:
        log = CrewLog.create(lg.KIND_SESSION, unit_id, owner="default", agent="kirocrew", slot=slot)
        opened = {
            "agent": "kirocrew",
            "slot": slot,
            "model": "",
            "cwd": "",
            "owner": "default",
            "resumed": False,
        }
        log.append("session/opened", opened, src="gateway")
        return log

    first = _unit("join-a1", "chat-a")
    first.append(
        "ledger/recorded",
        {
            "slot": "chat-a",
            "goal": "from a",
            "phase": "design",
            "event": "a-event",
            "event_kind": "phase",
        },
        src="gateway",
    )
    carried = projection.fold_slot_checkpoint("ledger", (), slot="chat-b").state
    second = _unit("join-b2", "chat-b")
    second.append(
        "session/carried",
        {
            "fold": "ledger",
            "slot": "chat-b",
            "version": projection.fold_state_version("ledger"),
            "state": json.dumps({**carried, "goal": "carried for b"}),
        },
        src="gateway",
    )
    own = _unit("join-t1", "workflow-join")
    del first, second, own

    both = projection.fold_joined_checkpoint(
        "ledger", ("join-a1", "join-b2", "join-t1"), slot="workflow-join"
    )
    alone = projection.fold_joined_checkpoint(
        "ledger", ("join-b2", "join-t1"), slot="workflow-join"
    )
    plain = projection.fold_slot_checkpoint("ledger", ("join-b2", "join-t1"), slot="workflow-join")

    assert both is None
    assert alone is not None
    assert projection.projection_of(alone).value["goal"] == "carried for b"
    assert projection.projection_of(plain).value["goal"] != "carried for b"
