"""``kiro_crew.dashboard.fork_lineage`` — the one fork-ancestry helper both the
artifact asset endpoint and the permanent-delete reap consult."""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard import fork_lineage as fl


def _log(meta_by_key: dict[str, dict], unreadable: set[str] = frozenset()) -> MagicMock:
    log = MagicMock()

    def _status(key: str) -> tuple[dict, bool]:
        if key in unreadable:
            return {}, False
        return meta_by_key.get(key, {}), True

    log.get_metadata_status.side_effect = _status
    return log


def test_fold_unifies_every_spelling_of_one_session() -> None:
    assert fl.fold("chat-1") == fl.fold("dashboard:chat-1") == fl.fold("dashboard_chat-1")
    assert fl.fold("slack:1700.42") == fl.fold("slack_1700.42")
    assert fl.fold("chat-1") != fl.fold("chat-2")


def test_fold_maps_a_cron_tab_onto_its_session() -> None:
    """A cron job's tab is ``cron-<id>`` while its session is ``cron:<id>``; the
    stem keeps ``-`` and folds ``:`` to ``_``, so without the bridge an image
    copy owned by the tab would never match the session in ``forked_from``."""
    assert fl.fold("cron-42") == fl.fold("cron:42") == fl.fold("cron_42")
    assert fl.fold("dashboard:cron-42") == fl.fold("cron:42")
    # A per-run key is a different session from the job's own.
    assert fl.fold("cron-42") != fl.fold("cron:42:run7")


def test_owner_spellings_cover_dashboard_and_cron_tab_names() -> None:
    assert fl.owner_spellings("dashboard:chat-1") == {"dashboard:chat-1", "chat-1"}
    assert fl.owner_spellings("cron:42") == {"cron:42", "cron-42"}
    assert fl.owner_spellings("cron_42") == {"cron_42", "cron-42"}
    assert fl.owner_spellings("dashboard:cron_42") == {"dashboard:cron_42", "cron_42", "cron-42"}
    assert fl.owner_spellings("slack:1.2") == {"slack:1.2"}


def test_descends_from_accepts_a_fork_of_a_cron_session() -> None:
    """The fork records the cron SESSION key; the copy is owned by the cron TAB."""
    log = _log({"dashboard:fork": {"forked_from": "cron:42", "fork_ancestors": ["cron:42"]}})
    assert fl.descends_from(log, "fork", "cron-42")
    assert fl.descends_from(log, "cron-42", "cron:42")
    assert not fl.descends_from(log, "fork", "cron-43")


def test_malformed_fork_ancestors_degrades_to_the_forked_from_walk() -> None:
    """Transcript metadata is user-editable JSON; a non-list chain must not raise
    into an asset request — the walk falls back to ``forked_from``."""
    for bad in (1, "dashboard:a", {"x": 1}, None):
        log = _log({"dashboard:b": {"forked_from": "dashboard:a", "fork_ancestors": bad}})
        assert fl.descends_from(log, "b", "a")
        assert fl.ancestry_chain(log, "dashboard:b") == ["dashboard:a"]
    assert fl.recorded_chain("not a dict") == []
    assert fl.recorded_chain({"fork_ancestors": ["dashboard:a", "", None]}) == ["dashboard:a"]


def test_materialize_ancestors_prepends_the_source_and_keeps_order() -> None:
    assert fl.materialize_ancestors("dashboard:b", ["dashboard:a"]) == [
        "dashboard:b",
        "dashboard:a",
    ]
    assert fl.materialize_ancestors("dashboard:a", None) == ["dashboard:a"]
    # Duplicates and blanks are dropped.
    assert fl.materialize_ancestors("dashboard:b", ["dashboard:b", "", "dashboard:a"]) == [
        "dashboard:b",
        "dashboard:a",
    ]


def test_descends_from_accepts_owner_forks_and_grandforks() -> None:
    log = _log(
        {
            "dashboard:fork": {"forked_from": "dashboard:src", "fork_ancestors": ["dashboard:src"]},
            "dashboard:grand": {
                "forked_from": "dashboard:fork",
                "fork_ancestors": ["dashboard:fork", "dashboard:src"],
            },
        }
    )
    assert fl.descends_from(log, "src", "src")
    assert fl.descends_from(log, "fork", "src")
    assert fl.descends_from(log, "dashboard_grand", "src")
    assert not fl.descends_from(log, "other", "src")


def test_descends_from_survives_a_deleted_intermediate_via_the_materialized_chain() -> None:
    # `fork` is gone from the catalog; `grand` still names `src` in its own chain.
    log = _log(
        {
            "dashboard:grand": {
                "forked_from": "dashboard:fork",
                "fork_ancestors": ["dashboard:fork", "dashboard:src"],
            }
        }
    )
    assert fl.descends_from(log, "grand", "src")


def test_descends_from_walks_forked_from_for_legacy_forks_without_a_chain() -> None:
    log = _log(
        {
            "dashboard:fork": {"forked_from": "dashboard:src"},
            "dashboard:grand": {"forked_from": "dashboard:fork"},
        }
    )
    assert fl.descends_from(log, "grand", "src")


def test_descends_from_fails_closed_on_unreadable_lineage() -> None:
    log = _log(
        {"dashboard:fork": {"forked_from": "dashboard:src"}}, unreadable={"dashboard:fork", "fork"}
    )
    assert not fl.descends_from(log, "fork", "src")


def test_ancestors_of_terminates_on_a_cycle() -> None:
    log = _log(
        {
            "dashboard:a": {"forked_from": "dashboard:b"},
            "dashboard:b": {"forked_from": "dashboard:a"},
        }
    )
    assert fl.ancestors_of(log, "a") == {fl.fold("b")}


def test_ancestry_chain_reconstructs_a_legacy_source_before_materializing() -> None:
    # A <- B <- C where B and C predate `fork_ancestors` (forked_from only).
    # Forking C must record [C, B, A], not just [C].
    log = _log(
        {
            "dashboard:b": {"forked_from": "dashboard:a"},
            "dashboard:c": {"forked_from": "dashboard:b"},
        }
    )
    chain = fl.ancestry_chain(log, "dashboard:c")
    assert chain == ["dashboard:b", "dashboard:a"]
    assert fl.materialize_ancestors("dashboard:c", chain) == [
        "dashboard:c",
        "dashboard:b",
        "dashboard:a",
    ]


def test_ancestry_chain_prefers_a_recorded_chain_and_stops_when_unreadable() -> None:
    log = _log(
        {
            "dashboard:c": {
                "forked_from": "dashboard:b",
                "fork_ancestors": ["dashboard:b", "dashboard:a"],
            },
            "dashboard:z": {"forked_from": "dashboard:y"},
        },
        unreadable={"dashboard:y", "y"},
    )
    assert fl.ancestry_chain(log, "dashboard:c") == ["dashboard:b", "dashboard:a"]
    # y is unreadable: what is provable (z's parent) is recorded, no further.
    assert fl.ancestry_chain(log, "dashboard:z") == ["dashboard:y"]


def test_walk_ancestors_merges_recorded_chains_along_the_edge_walk() -> None:
    parent = {"c": "b", "b": "a"}
    known = {"c": ["b", "a"], "b": ["a"], "a": []}
    assert fl.walk_ancestors("c", parent.get, lambda s: known.get(s, [])) == {"b", "a"}
    # A chain recorded on a deleted intermediate still reaches the root when the
    # surviving node carries it itself.
    assert fl.walk_ancestors("c", {}.get, lambda s: {"c": ["b", "a"]}.get(s, [])) == {"b", "a"}
