"""The slot index answers a repeated read from one ``stat`` of the unit root.

A data home holds a few thousand session units, and every fold that asks which units a
slot owns reaches ``session_units_by_slot``, so the listing must not be paid per read.
These tests pin the bound and the correctness it must keep: once a listing is confirmed,
a repeated call on an unchanged root lists nothing; a unit created or removed is seen on
the next call, by this process or another one; a create landing in the same filesystem
timestamp tick as the first listing is seen by the confirming listing; and a root whose
stamps are far ahead of or behind this host's clock behaves the same, because only the
root's own stamps are compared.

No test waits on a clock. The elapsed-time clock the confirming listing waits on
(``store_units._monotonic_ns``) is a value each test moves with ``clock.advance()``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.crew_log import store, store_units
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

SLOT = "chat-cache"
_DAY_NS = 86_400 * 1_000_000_000


class _Clock:
    """The elapsed-time clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000_000_000_000

    def __call__(self) -> int:
        return self.now

    def advance(self) -> None:
        """Move past one racy window."""
        self.now += store_units._RACY_WINDOW_NS + 1


def _root() -> Path:
    root = store.crew_log_root(KIND_SESSION)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _create(unit_id: str, slot: str = SLOT) -> None:
    CrewLog.create(KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    held = _Clock()
    monkeypatch.setattr(store, "_monotonic_ns", held)
    return held


def _confirm(clock: _Clock) -> "dict[str, tuple[str, ...]]":
    """List the root, then list it again a window later, which confirms the listing."""
    store.session_units_by_slot()
    clock.advance()
    return store.session_units_by_slot()


@pytest.fixture
def listings(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> Iterator[list[str]]:
    """Every listing of the session unit root, by either spelling, while the test runs."""
    root = _root()
    seen: list[str] = []
    real_listdir = os.listdir
    real_iterdir = Path.iterdir

    def counting_listdir(path: "os.PathLike[str] | str" = ".") -> list[str]:
        if Path(path) == root:
            seen.append("listdir")
        return real_listdir(path)

    def counting_iterdir(self: Path):  # type: ignore[no-untyped-def]
        if self == root:
            seen.append("iterdir")
        return real_iterdir(self)

    monkeypatch.setattr(os, "listdir", counting_listdir)
    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    monkeypatch.setattr(store, "_slot_index", None)
    store_units._SLOT_INDEX_TRUST[0] = None
    yield seen
    store_units._SLOT_INDEX_TRUST[0] = None


def test_repeated_reads_of_an_unchanged_root_list_nothing_after_it_is_confirmed(
    listings: list[str], clock: _Clock
) -> None:
    for index in range(5):
        _create(f"acp-cache-{index}")

    first = _confirm(clock)
    assert len(first[SLOT]) == 5
    assert len(listings) == 2, "one listing, then one confirming listing a window later"

    for _ in range(50):
        assert store.session_units_by_slot() == first
        assert store.session_units_for_slot(SLOT) == first[SLOT]
    assert len(listings) == 2, "a read on an unchanged, confirmed root must not list it"


def test_a_listing_is_not_trusted_before_a_window_has_passed(
    listings: list[str], clock: _Clock
) -> None:
    _create("acp-racy-a")
    store.session_units_by_slot()
    store.session_units_by_slot()
    store.session_units_by_slot()
    assert len(listings) == 3


def test_a_created_unit_is_seen_on_the_next_read(listings: list[str], clock: _Clock) -> None:
    _create("acp-before")
    assert _confirm(clock)[SLOT] == ("acp-before",)
    assert store.session_units_for_slot(SLOT) == ("acp-before",)

    _create("acp-after")
    assert set(store.session_units_for_slot(SLOT)) == {"acp-before", "acp-after"}


def test_a_removed_unit_is_gone_on_the_next_read(listings: list[str], clock: _Clock) -> None:
    _create("acp-keep")
    _create("acp-drop")
    assert set(_confirm(clock)[SLOT]) == {"acp-keep", "acp-drop"}

    status = store.remove_unit(KIND_SESSION, "acp-drop", guard=lambda _path: True)
    assert status == store.REMOVE_REMOVED
    assert store.session_units_for_slot(SLOT) == ("acp-keep",)


def test_a_create_in_the_same_tick_as_the_first_listing_is_seen(
    listings: list[str], clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coarse-timestamp filesystem: the second create leaves every stat field equal.

    The stat is frozen, which is what such a filesystem reports for two creates in one
    tick. The read right after it still lists, and the confirming listing a window later
    finds the second unit by name instead of trusting the equal stat.
    """
    frozen = (str(_root()), 1, 2, 3, 4, 5, 6)
    monkeypatch.setattr(store, "_root_stat_key", lambda _root: frozen)

    _create("acp-coarse-a")
    assert store.session_units_for_slot(SLOT) == ("acp-coarse-a",)
    _create("acp-coarse-b")
    assert set(store.session_units_for_slot(SLOT)) == {"acp-coarse-a", "acp-coarse-b"}

    _create("acp-coarse-c")
    clock.advance()
    assert len(store.session_units_for_slot(SLOT)) == 3, "the confirming listing missed it"


@pytest.mark.parametrize("skew_ns", [_DAY_NS, -_DAY_NS], ids=["ahead", "behind"])
def test_a_root_whose_stamps_are_off_this_hosts_clock_behaves_the_same(
    listings: list[str], clock: _Clock, skew_ns: int
) -> None:
    """A network data home whose server clock is a day ahead of or behind this host.

    The root's mtime is set a day off the local clock. The listing is still confirmed
    and then served without a listing, and a later create is still seen, because no
    filesystem stamp is compared with the local clock.
    """
    _create("acp-skew-a")
    root = _root()
    stat = root.stat()
    os.utime(root, ns=(stat.st_atime_ns, time.time_ns() + skew_ns))

    assert _confirm(clock)[SLOT] == ("acp-skew-a",)
    confirmed = len(listings)
    assert store.session_units_for_slot(SLOT) == ("acp-skew-a",)
    assert len(listings) == confirmed, "a skewed but unchanged root was listed again"

    _create("acp-skew-b")
    assert set(store.session_units_for_slot(SLOT)) == {"acp-skew-a", "acp-skew-b"}


def test_the_trust_record_is_confirmed_only_by_a_later_listing_under_the_same_stat() -> None:
    triple = object()
    window = store_units._RACY_WINDOW_NS
    key = ("r", 1, 2, 3, 4, 5, 6)
    store_units._SLOT_INDEX_TRUST[0] = None
    try:
        # The first listing started at 0 and completed at 100: it is stamped 100.
        first = store_units._next_trust(key, triple, 0, 100)
        assert first == (key, triple, 100, False)
        store_units._SLOT_INDEX_TRUST[0] = first
        late = 100 + window
        assert store_units._next_trust(key, triple, late - 1, late + 5) == (key, triple, 100, False)
        assert store_units._next_trust(key, triple, late, late + 5) == (key, triple, 100, True)
        other = ("r", 1, 2, 3, 4, 6, 6)
        assert store_units._next_trust(other, triple, late, late + 5) == (
            other,
            triple,
            late + 5,
            False,
        )
        assert store_units._next_trust(key, object(), late, late)[3] is False  # type: ignore[index]
        assert store_units._next_trust(None, triple, late, late) is None
    finally:
        store_units._SLOT_INDEX_TRUST[0] = None


def test_a_stalled_first_listing_is_timed_from_when_it_completed(
    listings: list[str], clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first listing stalls for a window between its start and its listing, and a
    create lands in that stall. A read right after cannot confirm it: the gap is counted
    from when the first listing completed, so a create in the same tick is still caught."""
    root = _root()
    frozen = (str(root), 1, 2, 3, 4, 5, 6)
    monkeypatch.setattr(store, "_root_stat_key", lambda _root: frozen)
    real_listdir = os.listdir

    def stalled_listdir(path: "os.PathLike[str] | str" = ".") -> list[str]:
        if Path(path) == root:
            clock.advance()
        return real_listdir(path)

    monkeypatch.setattr(store_units.os, "listdir", stalled_listdir)
    _create("acp-stall-a")
    assert store.session_units_for_slot(SLOT) == ("acp-stall-a",)
    monkeypatch.setattr(store_units.os, "listdir", real_listdir)

    store.session_units_for_slot(SLOT)
    _create("acp-stall-b")
    assert set(store.session_units_for_slot(SLOT)) == {"acp-stall-a", "acp-stall-b"}


def test_a_planted_index_is_not_served_on_a_stat_it_was_not_built_under(
    listings: list[str], clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create("acp-real")
    assert _confirm(clock)[SLOT] == ("acp-real",)
    count = len(listings)

    root = _root()
    fingerprint = store._slot_root_fingerprint(root, sorted(os.listdir(root)))
    monkeypatch.setattr(store, "_slot_index", (fingerprint, {SLOT: ("planted",)}, ()))
    assert store.session_units_for_slot(SLOT) == ("planted",)
    assert len(listings) > count + 1, "the planted triple went through the listing path"


def _child(code: str, cwd: Path) -> None:
    """Run *code* in a real child interpreter on this test's data home, from *cwd*."""
    env = dict(os.environ)
    subprocess.run([sys.executable, "-c", code], check=True, env=env, cwd=cwd, timeout=60)


def test_a_unit_another_process_creates_or_removes_is_seen_on_the_next_read(
    listings: list[str], clock: _Clock, tmp_path: Path
) -> None:
    _create("acp-parent")
    assert _confirm(clock)[SLOT] == ("acp-parent",)
    settled = len(listings)

    _child(
        "from kiro_crew.crew_log.store import CrewLog\n"
        "from kiro_crew.crew_log.schema import KIND_SESSION\n"
        f"CrewLog.create(KIND_SESSION, 'acp-child', owner='owner', agent='kirocrew', slot={SLOT!r})\n",
        tmp_path,
    )
    assert set(store.session_units_for_slot(SLOT)) == {"acp-parent", "acp-child"}
    assert len(listings) == settled + 1

    assert set(_confirm(clock)[SLOT]) == {"acp-parent", "acp-child"}
    _child(
        "from kiro_crew.crew_log import store\n"
        "from kiro_crew.crew_log.schema import KIND_SESSION\n"
        "assert store.remove_unit(KIND_SESSION, 'acp-child', guard=lambda _p: True)"
        " == store.REMOVE_REMOVED\n",
        tmp_path,
    )
    assert store.session_units_for_slot(SLOT) == ("acp-parent",)


def test_a_header_published_after_the_listing_is_still_seen_on_a_cache_hit(
    listings: list[str], clock: _Clock
) -> None:
    """``create`` makes the directory first and publishes the header second; a stat-only
    hit must keep re-checking the children the listing could not prove."""
    root = _root()
    pending = root / store._store_name("acp-pending")
    pending.mkdir()
    assert _confirm(clock).get(SLOT, ()) == ()
    assert store.session_units_for_slot(SLOT) == ()
    _create("acp-pending")
    assert store.session_units_for_slot(SLOT) == ("acp-pending",)


# --------------------------------------------------------------------------- #
# The eager folder's pass after each append lists the unit root at most once.
# --------------------------------------------------------------------------- #

BOARD = "member-cache-board"
BOARD_UNIT = "acp-cache-board"


def _board_work(**fields: Any) -> None:
    """One ``work/recorded`` on BOARD's board, appended and acknowledged."""
    from kiro_crew.crew_log import emit as crew_log_emit

    payload: dict[str, Any] = {"slot": BOARD, "actor": "conductor", "by": BOARD}
    payload.update(fields)
    assert crew_log_emit.on_work_recorded(BOARD_UNIT, payload, timeout=5.0) is True
    assert crew_log_emit.flush(), "the board append never settled"


def _append_item(index: int) -> None:
    _board_work(
        action="create",
        item_id=f"it_1000000{index}",
        title=f"item {index}",
        acceptance={"kind": "human_approval"},
        round=1,
    )


@pytest.fixture
def eager_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A board in its own data home, with the eager thread silenced so the test drives
    each pass itself and counts only that pass's work."""
    from kiro_crew.crew_log import eager as crew_log_eager
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import projection as crew_log_projection

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log_projection.forget_slot_folds()
    CrewLog.create(KIND_SESSION, BOARD_UNIT, owner="owner", agent="crew", slot=BOARD)
    _board_work(action="goal", goal="keep reads cheap", round=1)
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log_projection.forget_slot_folds()


@pytest.mark.parametrize("fold", ["work", "workstreams"])
def test_eager_passes_after_appends_do_not_list_the_unit_root_again(
    eager_board: None, listings: list[str], clock: _Clock, fold: str
) -> None:
    """K appends on an unchanged unit set, each followed by the eager pass that folds
    *fold*: once the listing is confirmed, no pass lists the unit root."""
    from kiro_crew.crew_log import eager as crew_log_eager

    crew_log_eager._advance(BOARD, fold)
    clock.advance()
    crew_log_eager._advance(BOARD, fold)
    warm_listings = len(listings)
    assert warm_listings >= 1, "the first pass never reached the unit listing"

    for index in range(8):
        _append_item(index)
        crew_log_eager._advance(BOARD, fold)

    assert len(listings) == warm_listings, "an eager pass listed the unit root again"
