"""The save path preserves queued context it never accounted for."""

import pytest


def test_a_failed_precommit_sidecar_sync_refuses_the_transcript_commit(tmp_path, monkeypatch):
    """A stale sidecar the commit did not update still hydrates, so the commit must not happen.

    The sidecar carries entries the metadata line is about to account for. If the line commits while
    the sidecar write failed, the file keeps DELIVERED entries and the next hydration folds them back
    and re-injects them -- against a transcript that says they were delivered.

    Safe to raise on every caller: ``best_effort=True`` logs and marks the slot dirty so the periodic
    flush retries, and the close paths pass ``best_effort=False`` to reach their restore arm.
    """
    from kiro_crew import history as h
    from kiro_crew.dashboard import chat_persistence as cp

    key = "chat-precommit-sync"
    # An existing spill is what makes the pre-commit write reachable with nothing over budget.
    h.write_ctx_overflow(key, [{"ctxId": "spilled-1", "content": "s" * 100}], tmp_path)

    def _refuse(*_a, **_kw):
        raise OSError("sidecar unwritable")

    monkeypatch.setattr(h, "sync_ctx_overflow", _refuse)

    with pytest.raises(h.CtxSpillFailed) as caught:
        cp.preserve_unaccounted_context(
            [{"ctxId": "arriving", "content": "a" * 100}],
            [],
            set(),
            archive_key=key,
            archive_base=tmp_path,
        )
    assert key in str(caught.value), f"the failure must name the transcript at risk: {caught.value}"


def test_an_unwritable_sidecar_still_leaves_the_metadata_line_bounded(tmp_path, monkeypatch):
    """An unwritable sidecar must fail the save, not commit a queue it did not persist.

    Two dispositions are both wrong. Putting the over-budget union on the metadata line is paid for
    in MESSAGE rows, because the line lives inside the transcript and the session rotates to fit it.
    Committing only the entries that fit reports a durable save for the rest, and the close removes
    the slot immediately after, so nothing retries them.

    So it raises, which reaches the close path's restore arm and keeps the slot. Asserting merely
    that the call did not return the union would pass on the truncating variant.
    """
    from kiro_crew import history as h
    from kiro_crew.dashboard import chat_persistence as cp

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)
    budget = max(1, int(40_000 // 2))

    def _refuse(*_a, **_kw):
        raise OSError("sidecar unwritable")

    monkeypatch.setattr(h, "sync_ctx_overflow", _refuse)

    entries = [
        {"ctxId": "fits-0", "content": "f" * 100, "injectedAt": 0.0},
        {"ctxId": "over-1", "content": "o" * 25_000, "injectedAt": 1.0},
        {"ctxId": "over-2", "content": "o" * 25_000, "injectedAt": 2.0},
    ]

    with pytest.raises(h.CtxSpillFailed) as caught:
        cp.preserve_unaccounted_context(
            entries, [], set(), final=True, archive_key="chat-unwritable", archive_base=tmp_path
        )

    assert "over-1" in str(caught.value), (
        f"the failure must name the entries it could not place, so the log identifies what is at "
        f"risk: {caught.value}"
    )
    assert not isinstance(caught.value, OSError), (
        "an OSError subclass is swallowed by the OSError arms on this save path, which would put "
        "the caller back to committing a save that persisted nothing"
    )
    assert budget > 0


def test_a_folded_spill_cannot_promote_itself_onto_the_metadata_line(tmp_path, monkeypatch):
    """Sidecar entries arrive inside the on-disk range, so keeping that side unbounded promoted them.

    The fold re-attaches spilled entries to ``pending_context`` on the way out of a read, which puts
    them among the entries a rewrite treats as already committed to the line. Keeping that side
    unconditionally let a spill migrate onto the line one save at a time -- and the line is inside the
    transcript, where rotation can only trim MESSAGE rows, so the growth is paid for in real rows.

    Asserts the resulting line is within budget. Asserting no entry was lost passes on the defect,
    because the defect moved entries rather than dropping them.
    """
    from kiro_crew import history as h
    from kiro_crew.dashboard import chat_persistence as cp

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)
    budget = max(1, int(40_000 // 2))
    key = "chat-folded-promotion"

    spilled = [
        {"ctxId": f"spill-{i}", "content": "s" * 9_000, "injectedAt": float(i)} for i in range(4)
    ]
    h.write_ctx_overflow(key, spilled, tmp_path)
    (tmp_path / f"{key}.jsonl").write_text(
        '{"role": "user", "content": "a turn"}\n', encoding="utf-8"
    )

    # What a hydration hands the next save: the line's own entry plus the folded sidecar.
    folded = [{"ctxId": "on-line-0", "content": "L" * 200, "injectedAt": -1.0}, *spilled]

    line = cp.preserve_unaccounted_context(
        [], folded, set(), archive_key=key, archive_base=tmp_path
    )
    cost = sum(h._ctx_entry_persist_cost(e) for e in line if isinstance(e, dict))
    ids = [e["ctxId"] for e in line if isinstance(e, dict)]

    assert cost <= budget, (
        f"the folded spill put {cost} bytes on the metadata line against a {budget} budget, so the "
        f"transcript must rotate MESSAGE rows away to fit context that already had a home. line={ids}"
    )
    survivors = {e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)}
    assert {"spill-0", "spill-1", "spill-2", "spill-3"} <= survivors | set(
        ids
    ), f"an entry was neither on the line nor in the sidecar: line={ids} sidecar={survivors}"


def test_the_final_save_spill_is_reachable_from_one_holder(tmp_path):
    """One holder reaches the spill: ceiling-length content costs bytes per CHARACTER, not per byte.

    ``_MAX_PERSISTED_CONTEXT_BYTES`` admits ``_JSON_WORST_CASE_BYTES_PER_CHAR`` per character, so an
    entry of ``MAX_CONTEXT_CONTENT`` escaping characters is accepted at several times its length --
    and a handful of them exceed the metadata budget with no co-holder anywhere. Sizing the same
    entry as ASCII understates its cost by that factor, which is what made the multi-holder setup
    look like the only way in.

    Both counts are DERIVED from the shipped constants, and the entry is asserted ADMISSIBLE first:
    an entry over the arrival ceiling would be refused before it could ever reach a save, which
    would make this a test of a case production cannot produce.
    """
    from kiro_crew import history as h
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.dashboard.state import (
        _MAX_PENDING_CONTEXT,
        _MAX_PERSISTED_CONTEXT_BYTES,
        MAX_CONTEXT_CONTENT,
    )

    # A character JSON must escape, so the serialized entry is far larger than its character count.
    content = "\x01" * MAX_CONTEXT_CONTENT
    budget = max(1, int(h._SESSION_MAX_BYTES // 2))
    probe = {"ctxId": "sizing", "content": content, "injectedAt": 0.0}
    per_entry = h._ctx_entry_persist_cost(probe)

    assert per_entry <= _MAX_PERSISTED_CONTEXT_BYTES, (
        f"an entry costing {per_entry} exceeds the {_MAX_PERSISTED_CONTEXT_BYTES}-byte arrival "
        "ceiling, so it would be refused on the way in and this test would measure an unreachable "
        "case"
    )
    needed = budget // per_entry + 1
    assert needed <= _MAX_PENDING_CONTEXT, (
        f"{needed} entries are required to exceed the budget but one holder seats only "
        f"{_MAX_PENDING_CONTEXT}; the single-holder spill is not reachable and this test is "
        "measuring the wrong mechanism"
    )

    entries = [
        {"ctxId": f"solo-e{i}", "content": content, "injectedAt": float(i)} for i in range(needed)
    ]
    assert (
        sum(h._ctx_entry_persist_cost(e) for e in entries) > budget
    ), "precondition: one holder's queue must actually exceed the budget"

    key = "chat-solo-spill"
    line = cp.preserve_unaccounted_context(
        entries, [], set(), final=True, archive_key=key, archive_base=tmp_path
    )

    spilled = [e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)]
    assert spilled, (
        f"{needed} entries from ONE holder at the shipped ceilings did not spill, so the sidecar "
        "guards a case a single slot cannot produce"
    )
    on_line = {e["ctxId"] for e in line if isinstance(e, dict)}
    assert on_line.isdisjoint(set(spilled)), "an entry must not be on the line AND in the sidecar"
    assert len(on_line) + len(spilled) == len(entries), (
        f"every acknowledged entry must have exactly one home: line={len(on_line)} "
        f"sidecar={len(spilled)} of {len(entries)}"
    )


def test_a_deferred_entry_keeps_a_smaller_later_entry_behind_it(tmp_path):
    """The non-final split walks the queue in order, so a refusal must carry the rest of it.

    Testing each entry against the REMAINING budget alone let a large entry defer while a later,
    smaller one still fitted -- so the metadata line came back holding a successor of an entry it
    had held back, returning the queue in a different order than the one its entries arrived in.
    """
    from kiro_crew import history as h
    from kiro_crew.dashboard import chat_persistence as cp
    from kiro_crew.dashboard.state import MAX_CONTEXT_CONTENT

    big = "\x01" * MAX_CONTEXT_CONTENT
    budget = max(1, int(h._SESSION_MAX_BYTES // 2))
    per_big = h._ctx_entry_persist_cost({"ctxId": "sizing", "content": big, "injectedAt": 0.0})
    count = budget // per_big + 1

    entries = [{"ctxId": f"big-{i}", "content": big, "injectedAt": float(i)} for i in range(count)]
    # The tail entry is TINY: under a per-entry fit test it slips onto the line behind entries
    # that were held back, which is the reordering this pins.
    entries.append({"ctxId": "tiny-last", "content": "x", "injectedAt": float(count)})

    line = cp.preserve_unaccounted_context(
        entries, [], set(), final=False, archive_key="chat-suffix", archive_base=tmp_path
    )
    on_line = [e["ctxId"] for e in line if isinstance(e, dict)]

    assert "tiny-last" not in on_line, (
        "a tiny trailing entry was kept on the line while larger entries queued BEFORE it were "
        f"deferred, so the queue is reordered: line={on_line[:4]}...{on_line[-2:]}"
    )
    kept_positions = [i for i, e in enumerate(entries) if e["ctxId"] in set(on_line)]
    assert kept_positions == list(
        range(len(kept_positions))
    ), f"the kept entries must be a PREFIX of the queue, got positions {kept_positions[:8]}"


def test_a_spilled_overflow_keeps_the_queue_in_order(tmp_path, monkeypatch):
    """A per-entry fit test left a SMALLER entry on-line ahead of its own spilled predecessor.

    The split walks the queue in order. Testing each entry against the remaining budget alone meant
    a large entry spilled while a later, smaller one still fitted -- so the metadata line held that
    later entry while the earlier one sat in the sidecar, and the fold recombined them with the
    queue's order inverted. Background context delivered out of order misinforms the turn it is
    meant to inform.

    Asserts the ORDER of the recombined queue, which is the property that broke; asserting only
    that nothing was lost passes on the defect, because the per-entry test lost nothing.
    """
    from kiro_crew import history as h
    from kiro_crew.dashboard import chat_persistence as cp

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)
    key = "chat-spill-order"

    # big-1 overflows the half-budget; small-2 would still fit on its own, which is the trap.
    entries = [
        {"ctxId": "small-0", "content": "s" * 100, "injectedAt": 0.0},
        {"ctxId": "big-1", "content": "b" * 25_000, "injectedAt": 1.0},
        {"ctxId": "small-2", "content": "s" * 100, "injectedAt": 2.0},
    ]

    kept = cp.preserve_unaccounted_context(
        entries, [], set(), final=True, archive_key=key, archive_base=tmp_path
    )
    kept_ids = [e["ctxId"] for e in kept if isinstance(e, dict)]
    spilled_ids = [e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)]

    assert "small-2" not in kept_ids, (
        "an entry AFTER the first overflow stayed on the metadata line, so the line holds it ahead "
        f"of its own spilled predecessor. kept={kept_ids} spilled={spilled_ids}"
    )
    # The recombined queue must read in the original order across both homes.
    assert kept_ids + [i for i in spilled_ids if i] == [
        "small-0",
        "big-1",
        "small-2",
    ], f"the spill inverted the queue: kept={kept_ids} spilled={spilled_ids}"


def test_an_empty_union_still_reconciles_the_overflow_sidecar(tmp_path, monkeypatch):
    """The preservation helper returned before the sync, so a drain left the spill.

    ``sync_ctx_overflow`` is reachable ONLY from inside ``_bounded_context_union``, so the two
    early returns in ``preserve_unaccounted_context`` skip the reconcile entirely. On the ordinary
    terminal save the union is empty -- everything was accounted for -- which is exactly when the
    sidecar most needs clearing, so the delivered entries stayed on disk and ``_fold_ctx_overflow``
    re-attached them on every later hydration. Self-perpetuating: they drain and hit it again.
    """
    from kiro_crew import history as h
    from kiro_crew.dashboard import chat_persistence as cp

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 60_000)
    key = "chat-empty-union"

    def spill() -> None:
        big = [
            {"ctxId": f"s-{n}", "content": "z" * 4_000, "source": "handover", "injectedAt": 1.0}
            for n in range(20)
        ]
        h.merge_pending_context([], big, final=True, archive_key=key, archive_base=tmp_path)
        assert h.read_ctx_overflow(key, tmp_path), "precondition: a spill exists"

    # THE NAMED PATH: everything is accounted for, so the union is empty.
    spill()
    cp.preserve_unaccounted_context(
        [], [], set(), final=True, archive_key=key, archive_base=tmp_path
    )
    left = [e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)]
    assert not left, (
        f"an empty union skipped the sidecar sync, leaving {len(left)} delivered entries on "
        f"disk ({left[:3]}...); every later hydration folds them back and re-injects them"
    )

    # THE SIBLING PATH, same defect: a non-list on-disk value also returned before the sync.
    spill()
    cp.preserve_unaccounted_context(
        [], None, set(), final=True, archive_key=key, archive_base=tmp_path
    )
    left_nonlist = [e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)]
    assert not left_nonlist, (
        f"a non-list on-disk value skipped the sidecar sync, leaving {len(left_nonlist)} "
        f"entries ({left_nonlist[:3]}...)"
    )


def test_a_replacement_full_save_keeps_a_handover_union_it_never_hydrated():
    """A replacement slot's full save erased the rows-only handover union.

    Reaching order: context is queued, a same-key handover writes the union to disk, then
    the REPLACEMENT slot performs an ordinary full save. The full save rebuilds
    `pending_context` from its own export and does not union with disk, so entries the
    replacement never hydrated were silently dropped -- acknowledged content with no other
    durable home on that file.

    The fix cannot simply carry `pending_context` forward: it is slot-owned precisely so
    that OMITTING it is what clears a delivered queue. So omission may only speak for
    entries this slot actually accounted for -- its `_ctx_origin_ids`. An entry absent from
    that set was never hydrated here, so its absence from the export is ignorance, not a
    clear, and it must survive.
    """
    from kiro_crew.dashboard.chat_persistence import preserve_unaccounted_context

    handover = [
        {"content": "from the closed twin", "ctxId": "id-handover", "injectedAt": 1.0},
    ]
    mine = [{"content": "my own live entry", "ctxId": "id-mine", "injectedAt": 2.0}]

    # The replacement hydrated ONLY its own entry, so the handover id is unaccounted for.
    kept = preserve_unaccounted_context(mine, handover, {"id-mine"})
    assert [e["ctxId"] for e in kept] == [
        "id-handover",
        "id-mine",
    ], f"an entry this slot never hydrated must survive its full save: {kept}"

    # The clear still works: an entry this slot DID account for and then dropped is gone.
    cleared = preserve_unaccounted_context([], handover, {"id-handover"})
    assert cleared == [], (
        "omitting an accounted-for entry is the delivery clear and must still empty the "
        f"queue, got {cleared}"
    )

    # Idempotent -- a second full save must not regrow the line.
    assert preserve_unaccounted_context(kept, kept, {"id-mine"}) == kept

    # A non-str ctxId is unaccountable, so it is PRESERVED rather than silently dropped.
    odd = [{"content": "unidentified", "injectedAt": 3.0}]
    assert preserve_unaccounted_context([], odd, {"id-mine"}) == odd
