"""The session tree projection -- one test per promise that makes it safe, not fast.

Speed is the easy half and is not what these tests defend. The projection replaces a
scan with in-memory state, so the properties a second implementation would be free to
break are the ones that decide whether the state is ever WRONG: a read does no I/O at
all, the checkpoint is a shortcut that is discarded rather than trusted when it does not
match this build, the tail replay pays for the delta rather than the store, and the
emitter advances the fold only after the append that justifies it has succeeded.

The zero-I/O claims are asserted by COUNTING calls to the store's own readers rather
than by timing anything: a timing test would pass on a machine fast enough to hide a
scan, which is exactly the regression worth catching.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log.session_tree import OpenedRecord
from kiro_crew.crew_log.session_tree_projection import (
    CHECKPOINT_NAME,
    CHECKPOINT_VERSION,
    SessionTreeProjection,
)
from kiro_crew.session_ledger import _store_name

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one.

    The process-wide projection is dropped too: it is bound to one store, so a test
    inheriting the previous test's fold would be reading another store's records.

    No write is armed on the real maintenance pool either. ``_debounced_write`` checks
    the cancellation epoch under the lock but saves OUTSIDE it, so a worker that has
    already passed that check can still write after teardown bumped the epoch -- landing
    a file in this test's tmp home after pytest considers it finished. Every test here
    that wants a checkpoint on disk calls ``flush_checkpoint`` synchronously, so nothing
    needs the pool and refusing to submit costs no coverage.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    # The emitter is inert without the flag, and the hook tests below exercise the
    # emitter's real path rather than a hand-written entry.
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()


def _rec(sid: str, slot: str, created: int = 1, parent: str | None = None) -> OpenedRecord:
    return OpenedRecord(sid=sid, slot=slot, created_at=created, parent_slot=parent)


def _log(sid: str, slot: str) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)


def _opened(handle: CrewLog, slot: str, *, parent: dict[str, str] | None = None) -> None:
    data = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent is not None:
        data["parent"] = parent
    handle.append("session/opened", data, src=GATEWAY)


def _write_unit(sid: str, slot: str, *, parent: str | None = None) -> None:
    """A real, announced unit on disk, written the way the GATEWAY writes one.

    Through ``emit.on_session_opened`` rather than a hand-built append, because that
    is the path carrying the projection hook -- a test that wrote the entry directly
    would prove the fold and nothing about what advances it.
    """
    emit.on_session_opened(
        sid,
        agent="kirocrew",
        slot=slot,
        model="claude-opus-5",
        cwd="/w",
        owner="raymond",
        parent_slot=parent or "",
    )


def _checkpoint_path() -> Path:
    from kiro_crew.crew_log.store import crew_log_root

    return crew_log_root(lg.KIND_SESSION) / "projections" / CHECKPOINT_NAME


def _remove_unit(sid: str) -> str:
    """``remove_unit`` with the guard it requires. Unconditional here: the guard is
    retention's own re-check of expiry, which these tests are not exercising."""
    return crew_store.remove_unit(lg.KIND_SESSION, sid, guard=lambda _directory: True)


def _write_unit_unheld(sid: str, slot: str, *, parent: str | None = None) -> None:
    """A real announced unit whose lease nobody holds, for the removal tests.

    ``remove_unit`` asks for SOLE ownership and is refused while any handle that wrote
    still exists -- and the emitter keeps its handle, which is correct for a live
    session and is exactly what retention waits out. So a removal test writes the unit
    directly and drops the handle, rather than asserting a refusal it did not mean to
    set up.
    """
    handle = _log(sid, slot)
    _opened(handle, slot, parent={"slot": parent} if parent else None)
    del handle


class _StoreSpy:
    """Counts the store reads a projection makes. Zero is the claim under test."""

    def __init__(self, monkeypatch) -> None:
        self.unit_dirs = 0
        self.oldest_segment = 0
        self.read_head = 0
        self.iterdir = 0
        real_unit_dirs = crew_store.unit_dirs
        real_oldest = crew_store.oldest_segment
        real_head = crew_store.read_head

        def spy_unit_dirs(*a, **k):
            self.unit_dirs += 1
            return real_unit_dirs(*a, **k)

        def spy_oldest(*a, **k):
            self.oldest_segment += 1
            return real_oldest(*a, **k)

        def spy_head(*a, **k):
            self.read_head += 1
            return real_head(*a, **k)

        monkeypatch.setattr(crew_store, "unit_dirs", spy_unit_dirs)
        monkeypatch.setattr(crew_store, "oldest_segment", spy_oldest)
        monkeypatch.setattr(crew_store, "read_head", spy_head)

    @property
    def total(self) -> int:
        return self.unit_dirs + self.oldest_segment + self.read_head


# ── apply, forget, and the same-reference rule ─────────────────────────────


def test_apply_adds_an_edge_without_touching_the_store(monkeypatch):
    """The whole point: a delta folded in memory, with no disk access at all."""
    spy = _StoreSpy(monkeypatch)
    proj = SessionTreeProjection()

    proj.apply(_rec("s-parent", "slot-a"))
    proj.apply(_rec("s-child", "slot-b", created=2, parent="slot-a"))
    nodes = proj.nodes()

    assert nodes["slot-b"].parent_slot == "slot-a"
    assert spy.total == 0, "a projection read the store; it is supposed to be pure memory"


def test_a_parentless_record_is_what_lets_a_child_edge_be_followed(monkeypatch):
    """A root's own record is load-bearing, so the hook cannot skip parentless entries.

    ``fold_tree`` follows an edge only when the cited parent slot ``has_log`` -- and a
    root conductor session's record is the ONLY thing that puts its slot there. Drop
    it and the common case (a root session that opens children) silently renders every
    child as a root, which is the feature failing rather than degrading.
    """
    proj = SessionTreeProjection()
    proj.apply(_rec("s-child", "slot-b", created=2, parent="slot-a"))

    # The parent's own record has not arrived: a citation, not yet a place in the tree.
    assert proj.nodes()["slot-b"].parent_slot == "slot-a"
    assert "slot-a" not in proj.nodes()

    proj.apply(_rec("s-parent", "slot-a", created=1))
    assert "slot-a" in proj.nodes(), "the parentless record must establish the parent slot"


def test_forget_drops_the_record(monkeypatch):
    spy = _StoreSpy(monkeypatch)
    proj = SessionTreeProjection()
    proj.apply(_rec("s-parent", "slot-a"))
    proj.apply(_rec("s-child", "slot-b", created=2, parent="slot-a"))

    proj.forget("s-child")

    assert "slot-b" not in proj.nodes()
    assert "slot-a" in proj.nodes()
    assert spy.total == 0


def test_forget_is_silent_about_a_record_it_does_not_hold():
    proj = SessionTreeProjection()
    proj.forget("never-seen")
    proj.forget("")
    assert proj.nodes() == {}


def test_nodes_returns_the_same_object_while_unchanged():
    """dsh's same-reference rule: a timer-driven reader does no work and may compare
    by identity."""
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "slot-a"))

    first = proj.nodes()
    assert proj.nodes() is first

    # A record already held is not a change, so it must not invalidate the fold.
    proj.apply(_rec("s-a", "slot-a"))
    assert proj.nodes() is first

    # A real delta must.
    proj.apply(_rec("s-b", "slot-b", created=2, parent="slot-a"))
    assert proj.nodes() is not first


def test_a_record_without_a_sid_is_dropped():
    """The state is keyed by sid, so a blank key would collide every such record."""
    proj = SessionTreeProjection()
    proj.apply(_rec("", "slot-a"))
    assert proj.nodes() == {}


# ── the checkpoint ─────────────────────────────────────────────────────────


def test_checkpoint_round_trip():
    """A checkpoint written by one process seeds the next one with the same tree."""
    _write_unit("s-parent", "slot-a")
    _write_unit("s-child", "slot-b", parent="slot-a")

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert proj.flush_checkpoint() is True

    payload = json.loads(_checkpoint_path().read_text(encoding="utf-8"))
    assert payload["ver"] == CHECKPOINT_VERSION
    assert {row["sid"] for row in payload["records"]} == {"s-parent", "s-child"}

    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert revived.nodes()["slot-b"].parent_slot == "slot-a"


def test_a_checkpoint_record_whose_unit_is_gone_is_dropped_on_revival():
    """The checkpoint is a shortcut, never an authority: it cannot resurrect a unit
    that is absent from the store."""
    _write_unit("s-parent", "slot-a")
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    proj.apply(_rec("s-ghost", "slot-ghost", created=9, parent="slot-a"))
    assert proj.flush_checkpoint() is True

    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert "slot-ghost" not in revived.nodes()
    assert "slot-a" in revived.nodes()


def test_ver_mismatch_discards_the_checkpoint_and_rebuilds_cold(monkeypatch):
    """A checkpoint from another build is DISCARDED, never migrated."""
    _write_unit("s-parent", "slot-a")
    _write_unit("s-child", "slot-b", parent="slot-a")

    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ver": CHECKPOINT_VERSION + 1, "records": [{"sid": "ghost", "slot": "gone"}]}),
        encoding="utf-8",
    )

    proj = SessionTreeProjection()
    proj.ensure_seeded()

    nodes = proj.nodes()
    assert "gone" not in nodes, "a foreign-version checkpoint was trusted"
    assert nodes["slot-b"].parent_slot == "slot-a", "the cold rebuild did not run"


def test_an_unparseable_checkpoint_rebuilds_rather_than_raising():
    _write_unit("s-parent", "slot-a")
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert "slot-a" in proj.nodes()


def test_a_malformed_row_is_dropped_not_coerced():
    """A row this cannot READ costs a re-read of that unit's head. A row COERCED into
    the wrong shape would instead be folded as though it had been read."""
    _write_unit("good", "slot-a")
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ver": CHECKPOINT_VERSION,
                # Without the root this payload describes no store, so the load
                # discards it whole and the rows below are never examined -- the
                # assertions would then pass for the wrong reason.
                "root": stp._current_root(),
                "records": [
                    {"sid": "good", "slot": "slot-a", "at": 1},
                    {"sid": "bad-at", "slot": "slot-b", "at": "not-an-int"},
                    {"slot": "no-sid", "at": 1},
                    "not-a-dict",
                ],
            }
        ),
        encoding="utf-8",
    )
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    nodes = proj.nodes()
    assert "slot-a" in nodes
    assert "slot-b" not in nodes


def test_a_second_seed_waits_instead_of_clearing_the_first_ones_removal_record(monkeypatch):
    """Two seeds must not overlap, and the cost of overlap is a resurrected unit.

    ``_forgotten_while_seeding`` belongs to ONE seed. A second seed entering while the
    first one's scan is still running clears it, so the deletion the first seed had
    recorded is lost and its install puts the removed unit back. The System page's
    sampling and the sidebar's lazy seed are exactly two such callers.

    The second caller is let in at the one moment where overlap is possible: from
    inside the first scan, which is when the first seed holds its removal record and
    does not hold ``_lock``.
    """
    from kiro_crew.crew_log.session_tree import SessionTree, TreeReading, fold_tree

    _write_unit_unheld("s-parent", "slot-a")
    _write_unit_unheld("s-child", "slot-b", parent="slot-a")

    proj = SessionTreeProjection()
    parent = _rec("s-parent", "slot-a")
    child = _rec("s-child", "slot-b", created=2, parent="slot-a")
    entered: list[str] = []
    second_caller: list[threading.Thread] = []
    waited: list[bool] = []

    def scan_then_delete_then_reenter(_self, live_sids=()):
        entered.append("scan")
        if len(entered) == 1:
            proj.forget("s-child")
            # A second caller arrives mid-scan. Serialized, it blocks until this seed
            # finishes and then takes the already-seeded fast path, so this list gains
            # no second entry. Recorded rather than asserted here: ``ensure_seeded``
            # swallows everything its seed raises, so an assert inside this scan would
            # be reported as a seed that merely failed.
            second = threading.Thread(target=proj.ensure_seeded)
            second.start()
            second.join(timeout=2.0)
            waited.append(second.is_alive())
            second_caller.append(second)
        return TreeReading(
            nodes=fold_tree([parent, child]),
            incomplete=False,
            records=(parent, child),
        )

    monkeypatch.setattr(SessionTree, "reading", scan_then_delete_then_reenter)
    proj.ensure_seeded()
    assert second_caller, "the scan did not run, so the race was never exercised"
    second_caller[0].join(timeout=5.0)
    assert not second_caller[0].is_alive()
    assert waited == [True], "the second caller did not wait for the seed in flight"

    assert entered == ["scan"], "the second caller started its own scan"
    nodes = proj.nodes()
    assert "slot-b" not in nodes, "a concurrent seed resurrected a unit removed during the scan"
    assert "slot-a" in nodes


def test_an_oversized_checkpoint_field_is_refused_not_retained():
    """The checkpoint is bounded on the same terms as a unit's head.

    These strings are RETAINED for the projection's life, so the checkpoint cannot be
    the one path that admits a value the scanner would refuse. Each row below is
    well-typed and would have been accepted on types alone.
    """
    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

    _write_unit("good", "slot-a")
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ver": CHECKPOINT_VERSION,
                # Without the root this payload describes no store, so the load
                # discards it whole and the rows below are never examined -- the
                # assertions would then pass for the wrong reason.
                "root": stp._current_root(),
                "records": [
                    {"sid": "good", "slot": "slot-a", "at": 1},
                    {"sid": "x" * (MAX_ACP_SESSION_ID_LEN + 1), "slot": "slot-sid", "at": 1},
                    {"sid": "long-slot", "slot": "s" * (MAX_SHORT_STRING + 1), "at": 1},
                    {
                        "sid": "long-parent",
                        "slot": "slot-parent",
                        "at": 1,
                        "parent": "p" * (MAX_SHORT_STRING + 1),
                    },
                    {
                        "sid": "long-prev",
                        "slot": "slot-prev",
                        "at": 1,
                        "prev": "q" * (MAX_ACP_SESSION_ID_LEN + 1),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    nodes = proj.nodes()
    assert "slot-a" in nodes
    for refused in ("slot-sid", "slot-parent", "slot-prev"):
        assert refused not in nodes, f"{refused} was retained past its bound"
    assert not any(len(key) > MAX_SHORT_STRING for key in nodes)


def test_a_root_change_abandons_a_write_armed_for_the_previous_store(tmp_path, monkeypatch):
    """An armed write pins the OLD store's path but builds its payload when it WAKES.

    So a data home that moves mid-debounce would have that worker write the NEW store's
    records into the OLD store's checkpoint -- and a later read of that file would serve
    one store's lineage as the other's, with nothing to detect it. The root change moves
    the cancellation epoch, which makes the armed worker a no-op.
    """
    _write_unit("s-one", "slot-a")
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    old_target = _checkpoint_path()
    armed_epoch = proj._cancel_epoch

    wrote: list[tuple[dict, object]] = []
    monkeypatch.setattr(
        stp, "_save_checkpoint", lambda payload, path=None: wrote.append((payload, path)) or True
    )
    monkeypatch.setattr(stp, "CHECKPOINT_DEBOUNCE_SECS", 0)

    # The home moves, and the projection re-seeds against the new store.
    moved = tmp_path / "other-home"
    moved.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(moved))
    proj.ensure_seeded()

    # The worker armed before the move now wakes.
    proj._debounced_write(old_target, armed_epoch)
    assert wrote == [], "a write armed for the previous store still fired after the home moved"


def test_a_checkpoint_naming_another_store_is_discarded_not_trusted():
    """The file says which store it describes, and a mismatch is rebuilt from the log.

    The path alone cannot establish it: a write armed before the home moved, or a home
    restored from elsewhere, puts a syntactically valid file at the right path holding
    another store's records. Trusting it is silent, unrecoverable lineage corruption.
    """
    _write_unit("s-real", "slot-real")
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ver": CHECKPOINT_VERSION,
                "root": "/elsewhere/store/sessions",
                "records": [{"sid": "s-foreign", "slot": "slot-foreign", "at": 1}],
            }
        ),
        encoding="utf-8",
    )
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    nodes = proj.nodes()
    assert "slot-foreign" not in nodes, "another store's checkpoint was trusted"
    # Rebuilt from the log instead, so this store's own unit is still there.
    assert "slot-real" in nodes


def test_the_live_path_refuses_an_over_long_record_but_accepts_one_at_the_limit():
    """``apply`` is a door into the state, so it carries the same bound as the others.

    The emitter passes through whatever the BACKEND named the session; this process does
    not choose that id and cannot assume it is sane. An over-long one would be retained
    for the projection's life -- up to ``TREE_UNIT_CAP`` of them -- and then written into
    the checkpoint. Refused, not truncated: a truncated id is a DIFFERENT id.

    The at-limit half matters as much as the over-limit half: a bound that rejects a
    legal value is an outage, not a defence.
    """
    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

    proj = SessionTreeProjection()
    proj.ensure_seeded()

    proj.apply(_rec("x" * (MAX_ACP_SESSION_ID_LEN + 1), "slot-long-sid"))
    proj.apply(_rec("s-long-slot", "s" * (MAX_SHORT_STRING + 1)))
    proj.apply(_rec("s-long-parent", "slot-lp", parent="p" * (MAX_SHORT_STRING + 1)))
    nodes = proj.nodes()
    for refused in ("slot-long-sid", "slot-lp"):
        assert refused not in nodes, f"{refused} was retained past its bound"
    assert not any(len(key) > MAX_SHORT_STRING for key in nodes)

    # Exactly at the limit is legal and must land.
    proj.apply(_rec("a" * MAX_ACP_SESSION_ID_LEN, "b" * MAX_SHORT_STRING))
    assert "b" * MAX_SHORT_STRING in proj.nodes(), "a record at the limit was refused"


def test_a_resume_during_the_scan_does_not_erase_the_scanned_creator_edge(monkeypatch):
    """A held record is better evidence the session EXISTS, not that it has no creator.

    A resume appends its own ``session/opened`` that need not repeat the creator. If that
    lands while the seed's scan is running, the held record is PARENTLESS for a sid the
    scan found parented -- and merging it over the scan persists that child as a root.
    ``apply`` cannot save it: during a seed there is nothing held yet for it to preserve,
    so the rule has to live in the merge as well.
    """
    from kiro_crew.crew_log.session_tree import SessionTree, TreeReading, fold_tree

    _write_unit_unheld("s-parent", "slot-a")
    _write_unit_unheld("s-child", "slot-b", parent="slot-a")

    proj = SessionTreeProjection()
    parent = _rec("s-parent", "slot-a")
    child = _rec("s-child", "slot-b", created=2, parent="slot-a")

    def scan_then_resume(_self, live_sids=()):
        # The resumed opener commits while the scan runs, naming no creator.
        proj.apply(_rec("s-child", "slot-b", created=3))
        return TreeReading(
            nodes=fold_tree([parent, child]),
            incomplete=False,
            records=(parent, child),
        )

    monkeypatch.setattr(SessionTree, "reading", scan_then_resume)
    proj.ensure_seeded()

    nodes = proj.nodes()
    assert "slot-b" in nodes
    assert nodes["slot-b"].parent_slot == "slot-a", "a resume during the scan orphaned the child"


def test_retracting_a_parent_keeps_the_record_so_children_are_not_orphaned():
    """The one case where clearing a citation is right, and it must not drop the record.

    The creating segment is gone while later ones survive, so a scan of that unit now
    contributes its slot with no parent. Dropping the record instead would orphan the
    unit's CHILDREN: they cite its slot, and a slot with no record reads as a creator that
    never existed rather than one whose own creator is unknown.
    """
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    proj.apply(_rec("s-top", "slot-top"))
    proj.apply(_rec("s-mid", "slot-mid", created=2, parent="slot-top"))
    proj.apply(_rec("s-low", "slot-low", created=3, parent="slot-mid"))

    proj.retract_parent("s-mid")

    nodes = proj.nodes()
    # The middle slot is still there, now a root.
    assert "slot-mid" in nodes
    assert nodes["slot-mid"].parent_slot is None
    # And its child still nests under it, which is what dropping the record would break.
    assert nodes["slot-low"].parent_slot == "slot-mid"


def test_retracting_a_parent_is_silent_about_a_record_it_does_not_hold():
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    proj.retract_parent("s-absent")
    proj.retract_parent("")
    assert proj.nodes() == {}


# ── the tail replay ────────────────────────────────────────────────────────


def test_tail_replay_reads_heads_only_for_the_delta(monkeypatch):
    """The replay pays for the GAP, not for the history.

    Three units are on disk and two are in the checkpoint, so exactly ONE head may be
    read. A replay that re-read the store would read three.
    """
    _write_unit("s-parent", "slot-a")
    _write_unit("s-child", "slot-b", parent="slot-a")

    seed = SessionTreeProjection()
    seed.apply(_rec("s-parent", "slot-a"))
    seed.apply(_rec("s-child", "slot-b", created=2, parent="slot-a"))
    assert seed.flush_checkpoint() is True

    # Written AFTER the checkpoint -- the crash-gap case the replay exists for.
    _write_unit("s-late", "slot-c", parent="slot-a")

    spy = _StoreSpy(monkeypatch)
    proj = SessionTreeProjection()
    proj.ensure_seeded()

    assert proj.nodes()["slot-c"].parent_slot == "slot-a"
    assert spy.read_head == 1, f"expected one head read for the delta, got {spy.read_head}"
    assert spy.unit_dirs == 0, "the replay called the scanner's unit walk"


def test_a_resumed_opener_does_not_retract_its_creator_edge():
    """A resume appends its OWN ``session/opened``, and that entry need not repeat the
    creator the first one named. Replacing outright would drop the edge on resume and
    leave the session rendering as a root until the process re-seeds from disk.

    Not the reverse of a real change: the crew log has no entry meaning "this session
    was not opened by anyone after all", so nothing legitimately retracts a citation.
    """
    proj = SessionTreeProjection()
    proj.apply(_rec("s-child", "slot-b", created=1, parent="slot-a"))
    proj.apply(_rec("s-parent", "slot-a"))
    assert proj.nodes()["slot-b"].parent_slot == "slot-a"

    # The resume: same session, same slot, no creator named this time.
    proj.apply(_rec("s-child", "slot-b", created=2, parent=None))

    assert proj.nodes()["slot-b"].parent_slot == "slot-a", "the resume retracted the edge"


def test_a_removal_during_the_scan_is_not_resurrected_by_the_seed(monkeypatch):
    """The cold scan runs OFF the lock, so a unit can be deleted after it was listed.

    Installing the scan's copy would put that record back and nothing would take it out
    again: the emitter fires once per opening, and the ``forget`` that accompanied the
    deletion ran against a state that was still empty, so it had nothing to pop.
    """
    from kiro_crew.crew_log.session_tree import SessionTree, TreeReading, fold_tree

    _write_unit_unheld("s-parent", "slot-a")
    _write_unit_unheld("s-child", "slot-b", parent="slot-a")

    proj = SessionTreeProjection()
    parent = _rec("s-parent", "slot-a")
    child = _rec("s-child", "slot-b", created=2, parent="slot-a")

    def scan_then_delete(_self, live_sids=()):
        # The deletion lands AFTER this scan listed the unit. That ordering is the whole
        # race: the reading below still carries a record whose unit is already deleted.
        proj.forget("s-child")
        return TreeReading(
            nodes=fold_tree([parent, child]),
            incomplete=False,
            records=(parent, child),
        )

    monkeypatch.setattr(SessionTree, "reading", scan_then_delete)
    proj.ensure_seeded()

    nodes = proj.nodes()
    assert "slot-b" not in nodes, "a unit removed during the scan was put back by the seed"
    assert "slot-a" in nodes


def test_tail_replay_drops_a_removed_unit(monkeypatch):
    _write_unit("s-parent", "slot-a")
    # Lease-free: this unit's DIRECTORY is deleted below, and Windows refuses to unlink
    # a file another handle still has open -- the lease a live handle holds is exactly
    # such a file.
    _write_unit_unheld("s-gone", "slot-b", parent="slot-a")

    seed = SessionTreeProjection()
    seed.apply(_rec("s-parent", "slot-a"))
    seed.apply(_rec("s-gone", "slot-b", created=2, parent="slot-a"))
    assert seed.flush_checkpoint() is True

    import shutil

    from kiro_crew.crew_log.store import crew_log_root

    shutil.rmtree(crew_log_root(lg.KIND_SESSION) / _store_name("s-gone"))

    proj = SessionTreeProjection()
    proj.ensure_seeded()

    nodes = proj.nodes()
    assert "slot-b" not in nodes, "a unit that is gone from disk stayed in the fold"
    assert "slot-a" in nodes


def test_a_read_after_seeding_does_no_io_at_all(monkeypatch):
    """The regression that matters: seeding is once per process, reads are free."""
    _write_unit("s-parent", "slot-a")
    _write_unit("s-child", "slot-b", parent="slot-a")

    proj = SessionTreeProjection()
    proj.ensure_seeded()

    spy = _StoreSpy(monkeypatch)
    for _ in range(50):
        proj.nodes()
        proj.reading()
        proj.ensure_seeded()

    assert spy.total == 0, "a poll-shaped read touched the store"


def test_a_moved_data_home_re_seeds_instead_of_serving_the_old_store(tmp_path, monkeypatch):
    """The fold is the image of ONE store; a home that moves must not inherit it."""
    _write_unit("s-parent", "slot-a")
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert "slot-a" in proj.nodes()

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "other-home"))
    _write_unit("s-other", "slot-z")
    proj.ensure_seeded()

    nodes = proj.nodes()
    assert "slot-a" not in nodes, "the previous store's records were served for a new home"
    assert "slot-z" in nodes


def test_no_checkpoint_seeds_from_one_cold_scan():
    _write_unit("s-parent", "slot-a")
    _write_unit("s-child", "slot-b", parent="slot-a")
    assert not _checkpoint_path().exists()

    proj = SessionTreeProjection()
    proj.ensure_seeded()

    assert proj.nodes()["slot-b"].parent_slot == "slot-a"


def test_an_empty_store_seeds_to_an_empty_tree():
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert proj.nodes() == {}
    assert proj.reading().incomplete is False


# ── the emitter hook ───────────────────────────────────────────────────────


def test_the_emitter_applies_one_record_per_opened_entry(monkeypatch):
    """The hook fires once per ``session/opened``, carrying the parent when there is
    one, and the record it applies is built from what was just WRITTEN."""
    applied: list[OpenedRecord] = []
    real_apply = SessionTreeProjection.apply

    def spy_apply(self, record):
        applied.append(record)
        return real_apply(self, record)

    monkeypatch.setattr(SessionTreeProjection, "apply", spy_apply)

    _write_unit("s-parent", "slot-a")
    assert len(applied) == 1
    assert applied[0].sid == "s-parent"
    assert applied[0].slot == "slot-a"
    assert applied[0].parent_slot is None

    _write_unit("s-child", "slot-b", parent="slot-a")
    assert len(applied) == 2, "the hook did not fire exactly once for the second entry"
    assert applied[1].sid == "s-child"
    assert applied[1].parent_slot == "slot-a"

    # The live projection now holds the edge with no scan behind it.
    assert stp.projection().nodes()["slot-b"].parent_slot == "slot-a"


def test_a_warm_reuse_writes_no_entry_and_applies_nothing(monkeypatch):
    """The emitter is silent on a warm reuse of a session that already has a log, so
    the hook must be silent too: once per opened ENTRY, not once per claim."""
    applied: list[OpenedRecord] = []
    real_apply = SessionTreeProjection.apply

    def spy_apply(self, record):
        applied.append(record)
        return real_apply(self, record)

    monkeypatch.setattr(SessionTreeProjection, "apply", spy_apply)

    _write_unit("s-a", "slot-a")
    assert len(applied) == 1

    _write_unit("s-a", "slot-a")
    assert len(applied) == 1, "a warm reuse re-applied a record"


def test_the_hook_does_not_fire_for_other_entry_kinds(monkeypatch):
    applied: list[OpenedRecord] = []
    real_apply = SessionTreeProjection.apply

    def spy_apply(self, record):
        applied.append(record)
        return real_apply(self, record)

    monkeypatch.setattr(SessionTreeProjection, "apply", spy_apply)

    _write_unit("s-a", "slot-a")
    assert len(applied) == 1

    handle = CrewLog.open(lg.KIND_SESSION, "s-a")
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append("session/closed", {"reason": "done"}, src=GATEWAY)
    assert len(applied) == 1, "a non-opened entry advanced the projection"


def test_the_record_is_applied_only_after_the_append_landed(monkeypatch):
    """Durability first, then memory: when the fold is advanced, the entry that
    justifies it is ALREADY on disk. Ordered the other way, an append that then failed
    would leave a creator edge no log records."""
    seen_on_disk: list[bool] = []
    real_apply = SessionTreeProjection.apply

    def spy_apply(self, record):
        from kiro_crew.crew_log.store import crew_log_root

        directory = crew_log_root(lg.KIND_SESSION) / _store_name(record.sid)
        segment = crew_store.oldest_segment(directory)
        body = segment.read_text(encoding="utf-8") if segment is not None else ""
        seen_on_disk.append("session/opened" in body)
        return real_apply(self, record)

    monkeypatch.setattr(SessionTreeProjection, "apply", spy_apply)

    _write_unit("s-a", "slot-a")

    assert seen_on_disk == [True], "the projection was advanced before the append landed"


def test_a_projection_failure_does_not_fail_the_session_open(monkeypatch):
    """An append that already succeeded must not be reported as failed because the
    memory image of it did not land."""

    def boom(self, record):
        raise RuntimeError("projection is broken")

    monkeypatch.setattr(SessionTreeProjection, "apply", boom)

    _write_unit("s-a", "slot-a")  # must not raise

    from kiro_crew.crew_log.store import crew_log_root

    assert (crew_log_root(lg.KIND_SESSION) / _store_name("s-a")).is_dir()


# ── removal ────────────────────────────────────────────────────────────────


def test_removing_a_unit_forgets_its_record():
    """``remove_unit`` is the one point a unit is established as gone, so the fold
    drops it there rather than waiting for a cold start."""
    _write_unit_unheld("s-parent", "slot-a")
    _write_unit_unheld("s-child", "slot-b", parent="slot-a")

    proj = stp.projection()
    proj.ensure_seeded()
    assert "slot-b" in proj.nodes()

    status = _remove_unit("s-child")

    assert status == crew_store.REMOVE_REMOVED
    assert "slot-b" not in proj.nodes(), "a removed unit's edge survived in memory"
    assert "slot-a" in proj.nodes()


def test_a_removal_that_did_not_happen_keeps_the_record():
    """``REMOVE_ABSENT`` is not a removal, so nothing is forgotten on the strength of
    it -- the fold must not drop an edge because a sweep aimed at the wrong id."""
    _write_unit_unheld("s-parent", "slot-a")
    proj = stp.projection()
    proj.ensure_seeded()

    status = _remove_unit("never-existed")

    assert status == crew_store.REMOVE_ABSENT
    assert "slot-a" in proj.nodes()
