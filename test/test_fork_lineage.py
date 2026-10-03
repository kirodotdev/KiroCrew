"""``kiro_crew.dashboard.fork_lineage`` — the one fork-ancestry helper both the
artifact asset endpoint and the permanent-delete reap consult."""

from __future__ import annotations

import json
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
    # Every spelling includes the transcript stems the key may occupy (a
    # channel-born tab is named by its stem); the tab and bare forms sit beside them.
    assert fl.owner_spellings("dashboard:chat-1") == {
        "dashboard:chat-1",
        "chat-1",
        "dashboard_chat-1",
    }
    assert fl.owner_spellings("cron:42") == {"cron:42", "cron-42", "cron_42"}
    assert fl.owner_spellings("cron_42") == {"cron_42", "cron-42"}
    assert fl.owner_spellings("dashboard:cron_42") == {
        "dashboard:cron_42",
        "cron_42",
        "cron-42",
        "dashboard_cron_42",
    }


def test_owner_spellings_cover_the_transcript_stem_a_channel_tab_is_named_by() -> None:
    """A channel-born tab is named by its transcript stem (`slack_<ts>`), so the
    copies it registers are owned by that stem while the delete handler holds the
    canonical session key `slack:<ts>`; the stem is a spelling, and so is the
    legacy bare-ts stem a pre-migration thread may still occupy."""
    spellings = fl.owner_spellings("slack:1700.42")
    assert {"slack:1700.42", "slack_1700.42"} <= spellings
    assert set(fl.transcript_stems("slack:1700.42")) <= spellings
    assert "dashboard_chat-1" in fl.owner_spellings("dashboard:chat-1")


def test_owner_spellings_cover_the_task_review_tab_of_a_taskrunner_session() -> None:
    """A task-runner review tab is `task-review-<token>` for the session key
    `taskrunner:<task_id>:chat:<token>`; the copies are owned by the tab, so a
    reap keyed by the session (or its file stem) must reach that spelling too."""
    assert {"taskrunner:t-1:chat:abc123", "task-review-abc123"} <= fl.owner_spellings(
        "taskrunner:t-1:chat:abc123"
    )
    assert {"taskrunner_t-1_chat_abc123", "task-review-abc123"} <= fl.owner_spellings(
        "taskrunner_t-1_chat_abc123"
    )
    # Not a review chat: no tab spelling is invented.
    assert not any(s.startswith("task-review-") for s in fl.owner_spellings("taskrunner:t-1"))
    assert not any(s.startswith("task-review-") for s in fl.owner_spellings("taskrunner:t-1:chat:"))
    assert not any(s.startswith("task-review-") for s in fl.owner_spellings("slack:1.2"))


def test_descends_from_accepts_a_fork_of_a_cron_session() -> None:
    """The fork records the cron SESSION key; the copy is owned by the cron TAB."""
    log = _log({"dashboard:fork": {"forked_from": "cron:42", "fork_ancestors": ["cron:42"]}})
    assert fl.descends_from(log, "fork", "cron-42")
    assert fl.descends_from(log, "cron-42", "cron:42")
    assert not fl.descends_from(log, "fork", "cron-43")


def test_a_present_but_invalid_fork_ancestors_record_is_unprovable() -> None:
    """Transcript metadata is user-editable JSON. A chain that is PRESENT but not
    a list may be hiding real ancestors, so it never raises into a request and it
    never degrades into "no chain": it reads as unprovable, every reader fails
    closed, and a save writes the fact back rather than erasing it. An ABSENT
    (or ``null``) record is "no chain recorded" and the ``forked_from`` walk
    takes over."""
    for bad in (1, "dashboard:a", {"x": 1}, fl.UNPROVABLE_CHAIN_RECORD):
        log = _log({"dashboard:b": {"forked_from": "dashboard:a", "fork_ancestors": bad}})
        assert fl.recorded_chain({"fork_ancestors": bad}) is None
        assert not fl.descends_from(log, "b", "a")
        assert fl.ancestry_chain(log, "dashboard:b") is None
        assert fl.chain_unprovable({"fork_ancestors": bad})
    for absent in (
        {"forked_from": "dashboard:a"},
        {"forked_from": "dashboard:a", "fork_ancestors": None},
    ):
        log = _log({"dashboard:b": absent})
        assert fl.recorded_chain(absent) == []
        assert fl.descends_from(log, "b", "a")
        assert fl.ancestry_chain(log, "dashboard:b") == ["dashboard:a"]
        assert not fl.chain_unprovable(absent)
    assert fl.recorded_chain("not a dict") == []
    assert fl.recorded_chain({"fork_ancestors": ["dashboard:a", "", None]}) == ["dashboard:a"]
    # What a save writes: the chain, the unprovable marker, or nothing.
    assert fl.chain_record_for_save(["dashboard:a"], False) == ["dashboard:a"]
    assert fl.chain_record_for_save([], True) == fl.UNPROVABLE_CHAIN_RECORD
    assert fl.chain_record_for_save([], False) is None
    assert fl.recorded_chain({"fork_ancestors": fl.chain_record_for_save([], True)}) is None


def test_materialize_ancestors_prepends_the_source_and_keeps_order() -> None:
    assert fl.materialize_ancestors("dashboard:b", ["dashboard:a"]) == [
        "dashboard:b",
        "dashboard:a",
    ]
    assert fl.materialize_ancestors("dashboard:a", None) == ["dashboard:a"]


def test_a_linked_sessions_tab_key_is_recorded_beside_its_session_key() -> None:
    """Image copies are owned by the source TAB (`slot.key`); `forked_from` is the
    effective SESSION key. For a dashboard tab they fold together and the tab is
    skipped; for a linked session they differ and the tab key must be in the chain
    or the fork can never reach the copies its source's tab owns."""
    # Dashboard tab: same stem, nothing added.
    assert fl.materialize_ancestors("dashboard:chat-1", None, source_slot_key="chat-1") == [
        "dashboard:chat-1"
    ]
    # Linked session (task-review tab running on a channel/cron session).
    chain = fl.materialize_ancestors(
        "slack:1700.42", ["dashboard:root"], source_slot_key="task-review-7"
    )
    assert chain == ["slack:1700.42", "task-review-7", "dashboard:root"]
    # The owner check accepts the fork for a copy owned by the tab.
    log = _log({"dashboard:fork": {"forked_from": "slack:1700.42", "fork_ancestors": chain}})
    assert fl.descends_from(log, "fork", "task-review-7")
    # The tab key is bounded like every other entry: over it, the chain cannot be
    # recorded whole and is unprovable (the fork is refused), never shortened.
    assert (
        fl.materialize_ancestors(
            "slack:1.2", None, source_slot_key="t" * (fl.MAX_ANCESTOR_KEY_CHARS + 1)
        )
        is None
    )


def test_an_over_bounds_chain_is_unprovable_not_truncated() -> None:
    """Metadata is agent-writable: a chain at the count bound or carrying an
    over-long entry is reported as ``None`` and every reader fails CLOSED on it
    — the owner check refuses, materialization stops, admission drops it."""
    too_many = [f"dashboard:s{i}" for i in range(fl.MAX_FORK_ANCESTORS)]
    assert fl.recorded_chain({"fork_ancestors": too_many}) is None
    too_long = ["dashboard:" + "x" * fl.MAX_ANCESTOR_KEY_CHARS]
    assert fl.recorded_chain({"fork_ancestors": too_long}) is None
    within = [f"dashboard:s{i}" for i in range(fl.MAX_FORK_ANCESTORS - 1)]
    assert fl.recorded_chain({"fork_ancestors": within}) == within
    assert fl.admitted_chain({"fork_ancestors": too_many}) == []
    assert fl.admitted_chain({"fork_ancestors": within}) == within

    # The real ancestor IS in the oversized record; it still may not be used.
    log = _log(
        {
            "dashboard:b": {
                "forked_from": "dashboard:a",
                "fork_ancestors": too_many + ["dashboard:a"],
            }
        }
    )
    assert fl.ancestors_of(log, "b") == set()
    assert not fl.descends_from(log, "b", "a")
    assert fl.ancestry_chain(log, "dashboard:b") is None


def test_a_chain_over_the_serialized_budget_is_unprovable_under_the_count_bound() -> None:
    """The count and per-entry bounds alone admit a chain far larger than the
    metadata line it is written into (1024 keys of 256 chars). A chain whose
    serialized form exceeds MAX_FORK_ANCESTRY_BYTES (half the line bound) is
    unprovable to the reader and refused at materialization, so admission can
    never persist a chain that would push its own line past the reader's bound."""
    assert fl.MAX_FORK_ANCESTRY_BYTES * 2 == fl.MAX_METADATA_LINE_BYTES
    key = "dashboard:" + "k" * (fl.MAX_ANCESTOR_KEY_CHARS - len("dashboard:") - 4)
    per_entry = len(json.dumps([key + "0000"])) - 1
    fits = fl.MAX_FORK_ANCESTRY_BYTES // per_entry - 1
    long_keys = [f"{key}{i:04d}" for i in range(fits + 2)]
    assert len(long_keys) < fl.MAX_FORK_ANCESTORS
    assert len(json.dumps(long_keys).encode()) > fl.MAX_FORK_ANCESTRY_BYTES
    assert fl.recorded_chain({"fork_ancestors": long_keys}) is None
    assert fl.chain_unprovable({"fork_ancestors": long_keys})
    assert fl.materialize_ancestors("dashboard:src", long_keys) is None

    within = long_keys[: fits - 1]
    assert len(json.dumps(within).encode()) <= fl.MAX_FORK_ANCESTRY_BYTES
    assert fl.recorded_chain({"fork_ancestors": within}) == within
    made = fl.materialize_ancestors("dashboard:src", within)
    assert made is not None and len(json.dumps(made).encode()) <= fl.MAX_FORK_ANCESTRY_BYTES


def test_non_string_lineage_values_are_unprovable_before_any_conversion() -> None:
    """Transcript metadata is agent-writable: a nested list or dict planted as
    an ancestry entry or as `forked_from` must be rejected on TYPE, before a
    `str()` could materialize it whole to measure its length."""
    from unittest.mock import MagicMock

    nested = {"deep": [["x" * 64] * 64] * 64}
    # recorded_chain: a non-string entry makes the whole chain unprovable.
    assert fl.recorded_chain({"fork_ancestors": ["dashboard:a", nested]}) is None
    assert fl.recorded_chain({"fork_ancestors": ["dashboard:a", 7]}) is None
    assert fl.recorded_chain({"fork_ancestors": ["dashboard:a", None, ""]}) == ["dashboard:a"]
    # parent_key: typed accessor for forked_from.
    assert fl.parent_key({"forked_from": "dashboard:a"}) == "dashboard:a"
    assert fl.parent_key({"forked_from": ""}) is None
    assert fl.parent_key({}) is None
    assert fl.parent_key("not a dict") is None
    assert fl.parent_key({"forked_from": nested}) is fl.UNPROVABLE
    assert fl.parent_key({"forked_from": ["dashboard:a"]}) is fl.UNPROVABLE
    assert fl.parent_key({"forked_from": "x" * (fl.MAX_ANCESTOR_KEY_CHARS + 1)}) is fl.UNPROVABLE
    # ancestors_of: a nested forked_from on the walk fails closed (no ancestors).
    log = MagicMock()
    log.get_metadata_status.side_effect = lambda k: (
        ({"forked_from": nested}, True) if "fork" in k else ({}, True)
    )
    assert fl.ancestors_of(log, "dashboard:fork") == set()
    # ancestry_chain: a non-string parent makes the walk unprovable, never converted.
    assert fl.ancestry_chain(log, "dashboard:fork") is None


def test_materialize_ancestors_over_the_bound_is_unprovable_not_shorter() -> None:
    """A source chain that would carry the fork past the count bound is not
    truncated: the whole chain is unprovable and the fork is refused."""
    src_chain = [f"dashboard:s{i}" for i in range(fl.MAX_FORK_ANCESTORS + 5)]
    assert fl.materialize_ancestors("dashboard:src", src_chain) is None
    # Just inside the bound the chain is recorded whole, source first.
    inside = [f"dashboard:s{i}" for i in range(fl.MAX_FORK_ANCESTORS - 2)]
    chain = fl.materialize_ancestors("dashboard:src", inside)
    assert chain is not None and chain[0] == "dashboard:src" and len(chain) == len(inside) + 1
    assert fl.recorded_chain({"fork_ancestors": chain}) == chain


def test_the_legacy_forked_from_walk_retains_nothing_past_the_bounds() -> None:
    """Pre-upgrade forks carry only ``forked_from``, read row by row from
    agent-writable metadata; the bound is applied at every append, so a planted
    over-long parent or an absurdly deep chain stops the walk instead of being
    kept and later persisted by ``materialize_ancestors``."""
    long_parent = "dashboard:" + "x" * fl.MAX_ANCESTOR_KEY_CHARS
    log = _log({"dashboard:b": {"forked_from": long_parent}})
    assert fl.ancestry_chain(log, "dashboard:b") is None

    # A forked_from chain deeper than the count bound is unprovable: the walk
    # reports None rather than the prefix it could read, so a fork is refused
    # instead of persisted with a chain that claims to be complete.
    deep = {f"dashboard:n{i}": {"forked_from": f"dashboard:n{i + 1}"} for i in range(2000)}
    assert fl.ancestry_chain(_log(deep), "dashboard:n0") is None
    # Just inside the bound the walk completes.
    short = {f"dashboard:m{i}": {"forked_from": f"dashboard:m{i + 1}"} for i in range(5)}
    assert fl.ancestry_chain(_log(short), "dashboard:m0") == [
        f"dashboard:m{i}" for i in range(1, 6)
    ]
    # The source key itself is bounded too: a refusal makes the chain unprovable,
    # never a shorter chain that omits the source.
    assert fl.materialize_ancestors(long_parent, ["dashboard:a"]) is None
    assert fl.materialize_ancestors("dashboard:b", ["dashboard:a", long_parent]) is None
    assert fl.materialize_ancestors("dashboard:b", [], source_slot_key=long_parent) is None
    # A slot name at the admission bound still fits once prefixed: the bound is
    # the tighter of the owner-key room and the transcript-filename room.
    at_bound = "dashboard:" + "n" * fl.MAX_SLOT_NAME_CHARS
    assert len(at_bound) <= fl.MAX_ANCESTOR_KEY_CHARS
    assert fl.materialize_ancestors(at_bound, []) == [at_bound]
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
    # y is unreadable: the chain past z is unprovable, so the walk reports None
    # rather than the one link it could read.
    assert fl.ancestry_chain(log, "dashboard:z") is None


def test_walk_ancestors_merges_recorded_chains_along_the_edge_walk() -> None:
    parent = {"c": "b", "b": "a"}
    known = {"c": ["b", "a"], "b": ["a"], "a": []}
    assert fl.walk_ancestors("c", parent.get, lambda s: known.get(s, [])) == {"b", "a"}
    # A chain recorded on a deleted intermediate still reaches the root when the
    # surviving node carries it itself.
    assert fl.walk_ancestors("c", {}.get, lambda s: {"c": ["b", "a"]}.get(s, [])) == {"b", "a"}


def test_walk_ancestors_applies_the_shared_bounds_and_reads_back_unprovable() -> None:
    """`forked_from` is agent-writable, so the edge walk is bounded like every
    other ancestry reader: at most MAX_FORK_ANCESTORS nodes visited, every
    retained key at most MAX_ANCESTOR_KEY_CHARS. Over either, the answer is
    None (unprovable) rather than a truncated set a consumer could act on."""
    # Depth: a legacy chain one node past the bound.
    deep = {f"n{i}": f"n{i + 1}" for i in range(fl.MAX_FORK_ANCESTORS + 1)}
    assert fl.walk_ancestors("n0", deep.get, lambda s: []) is None
    # Exactly at the bound is still provable.
    ok = {f"n{i}": f"n{i + 1}" for i in range(fl.MAX_FORK_ANCESTORS - 1)}
    walked = fl.walk_ancestors("n0", ok.get, lambda s: [])
    assert walked is not None and len(walked) == fl.MAX_FORK_ANCESTORS - 1
    # An over-long `forked_from` parent.
    long_key = "k" * (fl.MAX_ANCESTOR_KEY_CHARS + 1)
    assert fl.walk_ancestors("c", {"c": long_key}.get, lambda s: []) is None
    # An over-long entry inside a node's recorded chain.
    assert fl.walk_ancestors("c", {}.get, lambda s: [long_key] if s == "c" else []) is None
    # A recorded chain that is itself wider than the bound.
    wide = [f"w{i}" for i in range(fl.MAX_FORK_ANCESTORS + 1)]
    assert fl.walk_ancestors("c", {}.get, lambda s: wide if s == "c" else []) is None
    # An over-long START (the asset route's caller-supplied `?session=`) is
    # refused before it is folded or retained, and no callback is consulted.
    calls: list[str] = []

    def _recording(s: str) -> list[str]:
        calls.append(s)
        return []

    assert fl.walk_ancestors(long_key, {}.get, _recording) is None
    assert calls == []
    assert fl.walk_ancestors("k" * fl.MAX_ANCESTOR_KEY_CHARS, {}.get, _recording) == set()


def test_ancestors_of_treats_an_unprovable_walk_as_no_ancestors() -> None:
    """The asset owner check needs a POSITIVE match, so a walk that ran over the
    bounds must not yield one: `ancestors_of` answers empty."""
    long_key = "k" * (fl.MAX_ANCESTOR_KEY_CHARS + 1)
    log = MagicMock()
    log.get_metadata_status.side_effect = lambda k: (
        {"forked_from": long_key} if k in ("c", "dashboard:c") else {},
        True,
    )
    assert fl.ancestors_of(log, "c") == set()
    log.get_metadata_status.side_effect = lambda k: (
        {"forked_from": "b"} if k in ("c", "dashboard:c") else {},
        True,
    )
    assert fl.ancestors_of(log, "c") == {"b"}


def test_an_absent_alias_does_not_make_an_unreadable_record_look_readable() -> None:
    """The catalog answers `({}, readable)` for an ABSENT file. `_read_meta` tries
    every spelling of a session, so with the canonical record unreadable and the
    other spellings naming no transcript at all, the empty answer for an absent
    alias would read as a readable root and a fork would persist a one-link
    chain as complete. With `has_log` on the log, a spelling that has no file is
    skipped and the record stays unreadable: the walk is unprovable."""
    log = _log(
        {"dashboard:a": {}, "dashboard:b": {"forked_from": "dashboard:a"}},
        unreadable={"dashboard:b"},
    )
    existing = {"dashboard:a", "dashboard:b"}
    log.has_log.side_effect = lambda key: key in existing
    # Every alias of b is absent (b's own file is present but unreadable).
    assert fl._read_meta(log, "dashboard:b") == ({}, False)
    assert fl.ancestry_chain(log, "dashboard:b") is None
    assert fl.ancestors_of(log, "dashboard:c") == set()
    # A spelling that DOES exist and reads empty is a readable root, as before.
    assert fl._read_meta(log, "dashboard:a") == ({}, True)
    assert fl.ancestry_chain(log, "dashboard:a") == []


def test_the_live_catalog_walk_bounds_the_metadata_line_before_decoding_it(tmp_path) -> None:
    """The transcript directory is agent-writable and the catalog's metadata read
    decodes (and caches) the whole first line before `recorded_chain` can bound
    the `fork_ancestors` array inside it. `_read_meta` therefore pre-reads each
    spelling's first line with a bounded `readline` and refuses to decode one
    past MAX_METADATA_LINE_BYTES: the record is unreadable, so the chain is
    unprovable and no positive ancestry match can be drawn. A line within the
    bound reads as before, newline or not. The same check serves the lineage
    snapshot in `handlers.sessions`, so the two readers cannot drift apart."""
    import json

    from kiro_crew.dashboard.handlers import sessions as handlers
    from kiro_crew.history import ConversationLog

    d = tmp_path / "sessions"
    d.mkdir()
    fine = {
        "_type": "metadata",
        "forked_from": "dashboard:src",
        "fork_ancestors": ["dashboard:src"],
    }
    (d / "dashboard_src.jsonl").write_text("{}\n")
    (d / "dashboard_fine.jsonl").write_text(json.dumps(fine) + "\n")
    (d / "dashboard_nonewline.jsonl").write_text(json.dumps(fine))
    huge = {
        "_type": "metadata",
        "forked_from": "dashboard:src",
        "fork_ancestors": ["dashboard:" + "x" * 200] * (fl.MAX_METADATA_LINE_BYTES // 100),
    }
    huge_line = json.dumps(huge)
    assert len(huge_line) > fl.MAX_METADATA_LINE_BYTES
    (d / "dashboard_huge.jsonl").write_text(huge_line + "\n")
    log = ConversationLog(base_dir=d)

    assert fl.metadata_line_within_bound(log, "dashboard:fine")
    assert fl.metadata_line_within_bound(log, "dashboard:nonewline")
    assert not fl.metadata_line_within_bound(log, "dashboard:huge")
    assert handlers._metadata_line_within_bound(log, "dashboard:huge") is False

    assert fl._read_meta(log, "dashboard:fine")[0]["forked_from"] == "dashboard:src"
    assert fl._read_meta(log, "dashboard:nonewline")[0]["forked_from"] == "dashboard:src"
    assert fl.ancestry_chain(log, "dashboard:fine") == ["dashboard:src"]
    # The oversized line is never handed to the decoder: unreadable, unprovable.
    assert fl._read_meta(log, "dashboard:huge") == ({}, False)
    assert fl.ancestry_chain(log, "dashboard:huge") is None
    assert fl.descends_from(log, "dashboard:huge", "dashboard:src") is False
    assert fl.ancestors_of(log, "dashboard:huge") == set()

    # The pre-read goes through the no-link chokepoint pinned to the transcript
    # directory, which admits regular files only: a transcript NAME that is not
    # one (here a directory; a link or a hardlink outside the directory takes
    # the same refusal in the chokepoint) is refused by the read itself, so the
    # record is unreadable and the walk is unprovable.
    (d / "dashboard_notafile.jsonl").mkdir()
    assert not fl.metadata_line_within_bound(log, "dashboard:notafile")
    assert fl._read_meta(log, "dashboard:notafile") == ({}, False)
    assert fl.ancestry_chain(log, "dashboard:notafile") is None


def test_the_session_key_and_transcript_stem_bounds_are_one_constant() -> None:
    """A transcript stem is a folded session key, so the store's owner-key bound,
    the lineage ancestor-key bound and the transcript directory's stem bound
    describe one population. They are three re-exports of
    `constants.SESSION_KEY_MAX_CHARS`, never separate literals: a stem bound
    that drifted below the key bound would make `transcript_stems_on_disk`
    refuse an admitted session, the lineage snapshot would read as unreadable,
    and chat-image reclamation would be off process-wide."""
    from kiro_crew import artifacts, constants, history

    assert (
        fl.MAX_ANCESTOR_KEY_CHARS
        == artifacts.MAX_SESSION_KEY_CHARS
        == history.MAX_TRANSCRIPT_STEM_CHARS
        == constants.SESSION_KEY_MAX_CHARS
    )
    assert fl.MAX_ANCESTOR_KEY_CHARS is constants.SESSION_KEY_MAX_CHARS
    assert history.MAX_TRANSCRIPT_STEM_CHARS is constants.SESSION_KEY_MAX_CHARS
