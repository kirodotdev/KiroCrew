"""The post-commit reconcile settles what the sidecar still holds."""

import json

import pytest


def test_a_failed_sidecar_cleanup_cannot_leave_delivered_context_recoverable(tmp_path, monkeypatch):
    """Delivered context must not come back after a failed cleanup -- via the RETRY, not deletion.

    An earlier revision of this test asserted the fallback DELETED the file. That was rejected,
    because deleting also destroys the undelivered remainder whose only durable copy it is. The
    property still holds and is what this pins: the first hydration after the failure re-prunes
    every entry the metadata line already carries, so none survives to be re-seated.
    """
    from kiro_crew import history as h

    log = h.ConversationLog(tmp_path)
    key = "chat-cleanup-failure"
    log.append(key, "user", "a turn")
    delivered = [{"ctxId": f"dlv-{i}", "content": f"delivered {i}"} for i in range(5)]
    still_queued = [{"ctxId": "keep-1", "content": "not yet on the line"}]
    log.update_metadata(key, {"pending_context": delivered})
    h.write_ctx_overflow(key, delivered + still_queued, tmp_path)

    def _refuse(*_a, **_kw):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as _mp:
        _mp.setattr(h, "write_ctx_overflow", _refuse)
        h.reconcile_ctx_overflow(key, {e["ctxId"] for e in delivered}, tmp_path)

    # The SAVE is the retry: it owns the write, so it is where the stale copy is dropped. The
    # fold is read-only, because a rewrite from its possibly-stale read can erase a live spill.
    h.merge_pending_context([], [still_queued[0]], archive_key=key, archive_base=tmp_path)

    recoverable = {
        e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path) if isinstance(e, dict)
    }
    resurrected = sorted(recoverable & {e["ctxId"] for e in delivered})
    assert not resurrected, (
        f"{len(resurrected)} already-delivered entr(ies) {resurrected} are still recoverable "
        "after the next save, so a later fold re-injects them"
    )
    assert "keep-1" in recoverable, "the undelivered remainder must survive the whole sequence"


def test_a_failed_prune_preserves_the_undelivered_overflow(tmp_path, monkeypatch):
    """Deleting on prune failure destroyed the ONLY copy of still-undelivered context.

    The prune keeps what the transcript did not commit. When its rewrite fails there are only
    two reachable states -- keep the stale file or delete it -- and deleting takes acknowledged
    content that was never delivered with it. Preserving costs at most a DUPLICATE of something
    already delivered, which the fold dedups by ``ctxId`` and the next hydration re-prunes.
    """
    from kiro_crew import history as h

    key = "chat-prune-failure"
    delivered = [{"ctxId": f"dlv-{i}", "content": f"delivered {i}"} for i in range(3)]
    undelivered = [{"ctxId": f"keep-{i}", "content": f"still queued {i}"} for i in range(4)]
    h.write_ctx_overflow(key, delivered + undelivered, tmp_path)

    def _refuse(*_a, **_kw):
        raise OSError("ENOSPC")

    with pytest.MonkeyPatch.context() as _mp:
        _mp.setattr(h, "write_ctx_overflow", _refuse)
        h.reconcile_ctx_overflow(key, {e["ctxId"] for e in delivered}, tmp_path)

    survivors = {e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path) if isinstance(e, dict)}
    lost = sorted({e["ctxId"] for e in undelivered} - survivors)
    assert not lost, (
        f"{len(lost)} undelivered entr(ies) {lost} were destroyed by the prune-failure "
        "fallback; that file was their only durable copy"
    )


def test_a_mixed_spill_keeps_its_undelivered_half_when_the_post_commit_read_fails(
    tmp_path, monkeypatch
):
    """Quarantining an unreadable MIXED spill stranded the entries the commit never carried.

    The spill holds two kinds at once: entries the just-committed metadata line now carries, and
    entries it does not. Quarantine is what stops the first kind re-injecting after the commit,
    but it moves the WHOLE file off the hydration stem -- and for the second kind the sidecar was
    the only durable home, so they became unreachable by every hydration.

    Asserts the undelivered entry is RECOVERABLE from the hydration stem afterwards, and that the
    delivered one is not. Asserting merely that the file left the stem passes on the defect.
    """
    import kiro_crew.history as hist

    key = "chat-mixed-spill"
    hist.write_ctx_overflow(
        key,
        [
            {"ctxId": "delivered-1", "content": "already delivered"},
            {"ctxId": "undelivered-1", "content": "still owed to the model"},
        ],
        tmp_path,
    )
    spill = hist._ctx_overflow_path(key, tmp_path)
    assert spill.exists(), "precondition: a mixed spill exists"

    calls = {"n": 0}

    def _boom_once(_key, _base=None):
        calls["n"] += 1
        raise hist.CtxOverflowUnreadable("simulated transient post-commit I/O failure")

    with pytest.MonkeyPatch.context() as _mp:
        _mp.setattr(hist, "read_ctx_overflow", _boom_once)
        hist.reconcile_ctx_overflow(key, {"delivered-1"}, tmp_path)
        assert calls["n"] == 1, "precondition: the post-commit read really did fail"

    seated = [e.get("ctxId") for e in hist.read_ctx_overflow(key, tmp_path)]
    assert "undelivered-1" in seated, (
        "the undelivered entry was stranded off the hydration stem: the API answered 200 for it "
        f"and no hydration can now reach it. seated={seated}"
    )
    assert "delivered-1" not in seated, (
        "the delivered entry came back onto the stem, so a restart re-injects context the "
        f"session already delivered. seated={seated}"
    )


def test_an_unreadable_sidecar_after_the_commit_cannot_reinject(tmp_path, monkeypatch):
    """A transient read failure after the metadata commit left a stale sidecar hydratable.

    `reconcile_ctx_overflow` is the SHRINK half of the write and runs after the transcript's
    `atomic_write`, so by the time it reads the spill the line has already committed. The read
    raises on ordinary transient I/O and was not wrapped, so the stale file survived unpruned and
    a later restart re-injected context that had already been delivered.

    Asserts the file stops being HYDRATABLE while its bytes survive, which is what distinguishes
    containment from deleting the only durable copy of anything still undelivered.
    """
    import kiro_crew.history as hist

    key = "chat-unreadable-after-commit"
    hist.write_ctx_overflow(
        key, [{"ctxId": "delivered-1", "content": "already delivered"}], tmp_path
    )
    spill = hist._ctx_overflow_path(key, tmp_path)
    assert spill.exists(), "precondition: a sidecar exists to go stale"

    def _boom(_key, _base=None):
        raise hist.CtxOverflowUnreadable("simulated transient I/O failure")

    monkeypatch.setattr(hist, "read_ctx_overflow", _boom)
    hist.reconcile_ctx_overflow(key, {"delivered-1"}, tmp_path)

    assert not spill.exists(), (
        "the unreadable sidecar stayed on the hydration stem after the commit, so a restart "
        "re-injects context the session already delivered"
    )
    assert list(tmp_path.rglob("*.orphaned-*")), "the bytes were deleted rather than quarantined"


def test_the_ordinary_save_keeps_promoted_entries_in_the_sidecar(tmp_path):
    """Clearing the sidecar before the transcript commits leaves no durable copy.

    The ordinary union branch promoted a spilled entry onto the metadata line and rewrote the
    sidecar with only the deferred remainder -- but that write happens BEFORE ``atomic_write``,
    so a crash in between lost the promoted entries entirely. The sidecar has to stay a superset
    across the commit window; ``reconcile_ctx_overflow`` prunes it once the transcript is proven.
    """
    from kiro_crew import history as h

    key = "chat-superset-ordinary"
    spilled = [{"ctxId": f"sp-{i}", "content": f"spilled {i}"} for i in range(6)]
    h.write_ctx_overflow(key, spilled, tmp_path)

    # NOT `final`: this is the ordinary save, the branch the finding names.
    h.merge_pending_context([], list(spilled), archive_key=key, archive_base=tmp_path)

    survivors = {e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)}
    lost = sorted({e["ctxId"] for e in spilled} - survivors)
    assert not lost, (
        f"{len(lost)} promoted entr(ies) {lost[:4]} left the sidecar before the transcript "
        "carrying them was written; a crash in that window loses them outright"
    )


def test_the_sidecar_keeps_promoted_entries_until_the_transcript_commits(tmp_path, monkeypatch):
    """Shrinking the sidecar before the transcript write opened a loss window.

    ``sync_ctx_overflow`` ran ``os.replace`` while the caller's ``atomic_write`` was still ahead
    of it, so when freed capacity moved old spill entries onto the metadata payload they were
    removed from the sidecar BEFORE the line carrying them existed. A crash in that window left
    them in neither durable file, and the terminal close-save has no later retry. The sidecar
    must stay a SUPERSET across the window -- a duplicate is recoverable, a loss is not.
    """
    from kiro_crew import history as h

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 60_000)
    key = "chat-crash-window"

    # A prior spill this save has room to promote back onto the line.
    old = [
        {"ctxId": f"old-{n}", "content": "o" * 2_000, "source": "handover", "injectedAt": 1.0}
        for n in range(3)
    ]
    h.write_ctx_overflow(key, old, tmp_path)

    # This save admits the old entries and pushes newer ones past the budget.
    fresh = [
        {"ctxId": f"new-{n}", "content": "n" * 4_000, "source": "handover", "injectedAt": 2.0}
        for n in range(12)
    ]
    kept = h.merge_pending_context(old, fresh, final=True, archive_key=key, archive_base=tmp_path)
    kept_ids = {e["ctxId"] for e in kept}
    assert {"old-0", "old-1", "old-2"} <= kept_ids, "precondition: the old spill was promoted"

    held = {e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)}
    assert {"old-0", "old-1", "old-2"} <= held, (
        "the sidecar dropped promoted entries before the transcript carrying them was written; "
        "a crash in that window loses API-acknowledged context from BOTH durable files"
    )

    # AFTER the commit the line is durable, so the sidecar prunes down to the real excess.
    h.reconcile_ctx_overflow(key, kept_ids, tmp_path)
    after = {e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)}
    assert not (
        after & kept_ids
    ), f"committed entries left in the sidecar: {sorted(after & kept_ids)}"
    assert after, "the genuine excess must still be held"


def test_the_reconcile_reseats_from_the_holdings_the_read_already_made(tmp_path, monkeypatch):
    """A second clear finds an EMPTY stem, so the re-seat was handed nothing to recover from.

    `read_ctx_overflow` quarantines an over-ceiling spill off the hydration stem BEFORE raising, so
    the reconcile's own `clear_ctx_overflow` had nothing left to move and passed an empty list --
    the undelivered half was never even looked for, and no log named the file holding it. Reusing
    the raiser's own moves restores that attempt; dropping the reuse re-fails this.
    """
    from kiro_crew import history as h

    key = "chat-strand-1"
    (tmp_path / h.CTX_OVERFLOW_DIR_NAME).mkdir(parents=True)
    path = h._ctx_overflow_path(key, tmp_path)
    entries = [{"ctxId": f"d{i}"} for i in range(h._MAX_CTX_OVERFLOW_ENTRIES + 1)]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    handed: list[list] = []
    real_reseat = h._reseat_undelivered_from_quarantine
    monkeypatch.setattr(
        h,
        "_reseat_undelivered_from_quarantine",
        lambda k, q, c, b: (handed.append(list(q)), real_reseat(k, q, c, b))[1],
    )

    h.reconcile_ctx_overflow(key, {"d0"}, tmp_path)

    assert handed, "precondition: the reconcile must reach the re-seat at all"
    assert handed[0], "the re-seat was handed no holdings, so nothing undelivered could come back"
    holding = handed[0][0][1]
    assert holding.exists(), f"the holding named to the re-seat is not on disk: {holding}"
