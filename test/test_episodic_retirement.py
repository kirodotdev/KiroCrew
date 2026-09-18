"""Private V2 retirement is bounded, literal and reversible.

Every candidate is given the same mocked cosine so a similarity-only rule cannot
pass the precision checks. V2's rule uses literal assertions and never needs that
scorer. Global V1's unchanged heuristic is pinned separately in the member
algorithm suite.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import memory_stores
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_constants import _MAX_EPISODIC_RETIRED_PER_WRITE

pytestmark = pytest.mark.xdist_group("episodic_retirement")

#: Four episodes that literally restate the superseded value, and four that are merely
#: on the same topic. Under a stubbed scorer ALL eight score identically, so the split
#: below can only come from the containment test.
_RESTATES = (
    "pref.color: red for the theme",
    "pref.color is red for this option",
    "pref.color: red for the accent",
    "pref.color = red is the selected theme",
)
_SAME_TOPIC = (
    "the palette discussion went long",
    "colour choices were debated",
    "theme work continued",
    "styling was adjusted",
)

_KEY = "pref.color"
_OLD_VALUE = "red"


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    """A store holding the eight episodes, closed on the way out.

    Closed in a ``finally`` so ``tmp_path`` teardown succeeds on Windows, where an
    open sqlite handle blocks the directory removal.
    """
    root = tmp_path / "memory_stores"
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: root)
    directory = declare_v2_store(tmp_path, "member-color")
    st = VectorMemoryStore(db_path=directory / "memory.db")
    st.init()
    try:
        for text in (*_RESTATES, *_SAME_TOPIC):
            st.write_episodic(text, conversation_id="c1", importance=0.6)
        yield st
    finally:
        st.close()


def _force_vector_arm(store: VectorMemoryStore, score: float = 0.9):
    """Make every active episode a candidate at *score*.

    Two patches, both load-bearing. Without ``_try_embed`` the vector arm is skipped
    entirely on a host with no embedder; without ``search_episodic`` the candidate
    order and scores depend on a real vector space, which would make the assertions
    below about the embedder rather than about the rule.
    """
    rows = [dict(r, cosine_sim=score) for r in store.get_episodic_list(limit=50)]
    return mock.patch.multiple(
        store,
        _try_embed=mock.Mock(return_value=[0.1] * 4),
        search_episodic=mock.Mock(return_value=rows),
    )


def _texts(rows: list[dict]) -> set[str]:
    return {r["text"] for r in rows}


class TestSupersessionIsBounded:
    def test_one_write_retires_no_more_than_the_cap(self, store: VectorMemoryStore) -> None:
        """Eight equally-scoring candidates, and only the cap holds the rest back.

        Every candidate is above the cosine bar AND contains the value in the first
        four, so an uncapped rule would take every one it is allowed to. Asserting the
        exact cap rather than "fewer than 8" is what notices the cap being raised.
        """
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)

        retired = store.get_retired_episodic(limit=50)
        assert len(retired) == _MAX_EPISODIC_RETIRED_PER_WRITE
        assert len(store.get_episodic_list(limit=50)) == 8 - _MAX_EPISODIC_RETIRED_PER_WRITE

    def test_a_candidate_beyond_the_cap_stays_ALIVE(self, store: VectorMemoryStore) -> None:
        """The overflow direction is "keep", which is the safe one.

        A stale episode is outranked by the newer semantic row that contradicts it; a
        wrongly retired one is invisible to every reader. So the cap must drop the
        DELETE, never defer it into a queue that drains later.
        """
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)
        survivors = _texts(store.get_episodic_list(limit=50))
        # One of the four value-bearing episodes is over the cap and must have survived.
        assert len(survivors & set(_RESTATES)) == len(_RESTATES) - _MAX_EPISODIC_RETIRED_PER_WRITE


class TestSupersessionIsPrecise:
    def test_only_an_episode_restating_the_value_is_retired(self, store: VectorMemoryStore) -> None:
        """Cosine alone must not be enough, because every candidate here clears it."""
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)

        retired = _texts(store.get_retired_episodic(limit=50))
        assert retired <= set(_RESTATES)
        assert all(_OLD_VALUE in text for text in retired)

    def test_an_episode_merely_on_the_same_topic_survives(self, store: VectorMemoryStore) -> None:
        """The regression this exists for: same-topic rows scored 0.9 and were taken."""
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)
        assert set(_SAME_TOPIC) <= _texts(store.get_episodic_list(limit=50))

    def test_the_match_ignores_case_and_json_quoting(self, store: VectorMemoryStore) -> None:
        """The consolidator's value arrives JSON-encoded and cased however it was written.

        A containment test that compared raw strings would silently retire nothing for
        a value spelled ``"Red"`` — failing OPEN into never-forgetting, which is quiet
        rather than loud, so it needs its own assertion. The LIKE probe runs on the
        PRE-casefold value because SQLite LIKE folds ASCII only: a folded needle can be
        WIDER than LIKE matches, which would make the guard narrower than the test it
        guards. This one call exercises both.
        """
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, '"RED"')
        assert len(store.get_retired_episodic(limit=50)) == _MAX_EPISODIC_RETIRED_PER_WRITE

    def test_nothing_is_retired_when_no_candidate_carries_the_value(
        self, store: VectorMemoryStore
    ) -> None:
        """A superseded value nobody restated retires nothing, however well rows score."""
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, "chartreuse")
        assert store.get_retired_episodic(limit=50) == []
        assert len(store.get_episodic_list(limit=50)) == 8


class TestSupersessionSkipsTheWorkWhenNothingCanMatch:
    """The containment requirement makes a cheap existence probe provably sufficient.

    A candidate must contain the superseded value, and both text-fallback patterns are
    substrings of it, so ``text LIKE '%value%'`` is a SUPERSET of everything either arm
    can act on. A miss therefore means the embed and the vector search are pure waste —
    and that is the common case, since consolidation rewrites the same keys every cycle
    and most rewrites supersede a value no episode ever restated.
    """

    def test_no_embed_and_no_search_when_no_episode_carries_the_value(
        self, store: VectorMemoryStore
    ) -> None:
        with (
            mock.patch.object(store, "_try_embed") as embed,
            mock.patch.object(store, "search_episodic") as search,
        ):
            store._retire_stale_episodic(_KEY, "a-value-nobody-ever-wrote")
        assert embed.call_count == 0
        assert search.call_count == 0
        assert len(store.get_episodic_list(limit=50)) == 8

    def test_a_value_that_strips_to_nothing_does_not_probe_or_retire(
        self, store: VectorMemoryStore
    ) -> None:
        """``'\"\"\"'`` strips to empty, and an empty needle skips the containment test.

        Without the ``if raw_needle`` gate the probe would be ``LIKE '%%'`` — matching
        every row — and the arms would then retire on cosine alone.
        """
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, '"""')
        assert store.get_retired_episodic(limit=50) == []
        assert len(store.get_episodic_list(limit=50)) == 8


class TestSupersessionIsReversible:
    def test_a_retired_episode_keeps_its_text_and_is_listable(
        self, store: VectorMemoryStore
    ) -> None:
        """The row survives the tombstone, which is the only reason a guess may delete."""
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)

        retired = store.get_retired_episodic(limit=50)
        assert retired, "a retired row must be visible somewhere"
        row = retired[0]
        assert row["text"] in _RESTATES
        # The semantic key that superseded it -- the question a reader deciding
        # whether to restore actually asks.
        assert row["superseded_by"] == _KEY
        assert row["retired_at"]
        # created_at is the row's own, not the retirement's: a reader needs both.
        assert row["created_at"] and row["created_at"] != row["retired_at"]

    def test_restore_brings_it_back_under_its_own_id(self, store: VectorMemoryStore) -> None:
        """Restored, not re-inserted: a new row would re-enter the dedup that removed it."""
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)
        row = store.get_retired_episodic(limit=50)[0]

        assert store.restore_episodic(row["id"]) is True
        active = store.get_episodic_list(limit=50)
        assert row["text"] in _texts(active)
        assert row["id"] in {a["id"] for a in active}
        assert row["text"] not in _texts(store.get_retired_episodic(limit=50))

    def test_restoring_twice_is_not_an_error_and_not_a_duplicate(
        self, store: VectorMemoryStore
    ) -> None:
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)
        mem_id = store.get_retired_episodic(limit=50)[0]["id"]

        assert store.restore_episodic(mem_id) is True
        assert store.restore_episodic(mem_id) is False
        assert len([a for a in store.get_episodic_list(limit=50) if a["id"] == mem_id]) == 1

    def test_restoring_an_unknown_id_is_false_rather_than_a_raise(
        self, store: VectorMemoryStore
    ) -> None:
        assert store.restore_episodic("no-such-id") is False

    def test_a_row_retired_twice_is_listed_once_with_a_count(
        self, store: VectorMemoryStore
    ) -> None:
        """The event log is append-only, so an ungrouped join listed the episode twice.

        That made ``limit`` page a number of EVENTS while the caller asked for a number of
        episodes. ``retired_times`` keeps the information the grouping would otherwise
        flatten: a row that keeps coming back means the rule and the operator disagree
        about it.
        """
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)
        first = store.get_retired_episodic(limit=50)[0]
        assert store.restore_episodic(first["id"]) is True
        with _force_vector_arm(store):
            store._retire_stale_episodic(_KEY, _OLD_VALUE)

        listed = [row for row in store.get_retired_episodic(limit=50) if row["id"] == first["id"]]
        assert len(listed) == 1
        assert listed[0]["retired_times"] == 2
        # The MOST RECENT retirement, not the first.
        assert listed[0]["retired_at"] >= first["retired_at"]

    def test_a_users_own_delete_is_not_listed_as_superseded(self, store: VectorMemoryStore) -> None:
        """The two deletions mean different things and only one of them was a guess.

        Listing a deliberate delete as recoverable-by-mistake would invite undoing it,
        so the listing keys on the ``conflict_retire`` / ``semantic_update`` pair rather
        than on ``is_deleted`` alone.
        """
        mine = store.get_episodic_list(limit=50)[0]
        assert store.delete_episodic(mine["id"]) is True
        assert mine["text"] not in _texts(store.get_retired_episodic(limit=50))
