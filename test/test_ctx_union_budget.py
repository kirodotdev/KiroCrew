"""The pending-context union, its byte budget and its overflow spill."""


def test_a_within_budget_save_writes_no_sidecar(tmp_path, monkeypatch):
    """The sidecar sat on the common path: every save holding queued context wrote one.

    A save whose entries all fit the budget has nothing at risk during the commit window -- each
    one is going onto the metadata line -- so a sidecar there is a second copy of already-safe
    content, plus a file the post-commit reconcile exists only to prune. An at-risk save is the
    control: with an entry over the budget the sidecar MUST still be written, because for that
    entry the file is the only durable copy until the commit lands.
    """
    from kiro_crew import history as h

    key = "chat-narrow-sidecar"
    log = h.ConversationLog(tmp_path)
    log.append(key, "user", "a turn")

    small = {"ctxId": "fits-1", "content": "x", "source": "probe"}
    h.merge_pending_context([], [small], archive_key=key, archive_base=tmp_path)
    present = [p.name for p in h._ctx_overflow_paths(key, tmp_path) if p.exists()]
    assert present == [], (
        f"a within-budget save wrote a sidecar it does not need: {present}; every entry in it is "
        "going onto the metadata line this same save"
    )

    # CONTROL: an over-budget entry must still spill, or the narrowing has removed the guarantee.
    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 400)
    over = {"ctxId": "over-1", "content": "y" * 600, "source": "probe"}
    h.merge_pending_context([], [small, over], archive_key=key, archive_base=tmp_path)
    held_ids = {e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path) if isinstance(e, dict)}
    assert (
        "over-1" in held_ids
    ), f"the over-budget entry has no durable copy: sidecar holds {sorted(held_ids)}"


def test_a_folded_spill_keeps_a_durable_copy_across_the_commit_window(tmp_path):
    """The pre-commit sidecar rewrite dropped exactly the entries it was protecting.

    ``entries[:on_disk]`` means "already on the metadata line", which keeps its own durable
    copy until ``atomic_write`` replaces it -- so writing only ``kept[on_disk:]`` was safe. The
    folding read (``get_metadata_status_with_overflow``) puts SIDECAR entries on that same side,
    and their only durable copy is the sidecar: rewriting it without them left a window, before
    the transcript commit, in which acknowledged content existed in NEITHER file.
    """
    from kiro_crew import history as h

    key = "chat-folded-spill-window"
    spilled = {"ctxId": "spill-1", "content": "acknowledged, sidecar-only", "injectedAt": 1.0}
    h.write_ctx_overflow(key, [spilled], tmp_path)

    # The fold has already put the spilled entry on the DISK side, which is how the save sees it.
    line = h.merge_pending_context(
        [spilled], [], final=False, archive_key=key, archive_base=tmp_path
    )

    survivors = [e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path)]
    assert "spill-1" in survivors, (
        "the pre-commit sidecar rewrite dropped the folded spill, so between it and "
        f"`atomic_write` the entry had no durable copy at all: sidecar={survivors}"
    )
    # CONTROL: the entry must ALSO still reach the metadata line, or this would pass for a fix
    # that merely stopped promoting it.
    assert [e.get("ctxId") for e in line] == ["spill-1"]


def test_the_save_retires_a_stale_delivered_entry_from_the_sidecar(tmp_path):
    """The SAVE is the retry: it owns the write, so it is where a stale copy is dropped.

    A delivered entry is in neither the queue nor the metadata line, so it is absent from the
    union the save writes -- which is exactly what retires it. This is the half that lets the
    fold stay read-only without a failed prune leaving delivered content recoverable forever.
    """
    from kiro_crew import history as h

    key = "chat-save-retires"
    stale = {"ctxId": "delivered-1", "content": "already delivered"}
    live = [{"ctxId": "queued-1", "content": "still queued"}]
    h.write_ctx_overflow(key, [stale, *live], tmp_path)

    # The save's union sees only what the queue and the line still hold.
    h.merge_pending_context([], live, archive_key=key, archive_base=tmp_path)

    left = {e.get("ctxId") for e in h.read_ctx_overflow(key, tmp_path) if isinstance(e, dict)}
    assert "delivered-1" not in left, (
        "the save left a delivered entry in the sidecar, so with the fold read-only nothing "
        "retires it and every hydration re-offers it"
    )
    assert "queued-1" in left, "the save must not drop an entry that is still queued"


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


def test_a_shrinking_queue_retires_the_overflow_sidecar(tmp_path, monkeypatch):
    """A stale sidecar re-injected content that had already been delivered.

    The spill was written but never reconciled: once its entries were re-seated and drained, the
    next save wrote a SHORTER ``pending_context`` while the sidecar still held the old copy, and
    ``_fold_ctx_overflow`` dedups only against the line -- which is empty after a drain. So every
    later hydration resurrected retired context. The sidecar must therefore hold exactly what is
    NOT on the line, which means a save that spills nothing has to remove it.
    """
    from kiro_crew import history as h

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 60_000)
    log = h.ConversationLog(tmp_path)
    key = "chat-sidecar-retire"
    log.append(key, "user", "a turn")

    big = [
        {"ctxId": f"b-{n}", "content": "x" * 4_000, "source": "handover", "injectedAt": 1.0}
        for n in range(20)
    ]
    kept = h.merge_pending_context([], big, final=True, archive_key=key, archive_base=tmp_path)
    log.update_metadata(key, {"pending_context": kept})
    assert h.read_ctx_overflow(key, tmp_path), "precondition: a spill exists"

    # THE DRAIN: everything was delivered, so the next terminal save carries an empty queue.
    survivor = h.merge_pending_context([], [], final=True, archive_key=key, archive_base=tmp_path)
    assert survivor == [], "precondition: the save itself spills nothing"
    log.update_metadata(key, {"pending_context": []})

    resurrected = [
        e.get("ctxId")
        for e in (log.get_metadata(key) or {}).get("pending_context", [])
        if isinstance(e, dict)
    ]
    assert not resurrected, (
        f"the stale sidecar re-injected {len(resurrected)} already-delivered entries "
        f"({resurrected[:3]}...); nothing retires it, so every hydration replays them"
    )


def test_deleting_a_session_unlinks_its_overflow_sidecar(tmp_path, monkeypatch):
    """A reused key inherited the previous session's spilled context.

    ``delete_session`` removed the transcript and left the sidecar, so a new session created at
    the same key hydrated foreign background context -- content its own boundary never accepted.
    """
    from kiro_crew import history as h

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 60_000)
    log = h.ConversationLog(tmp_path)
    key = "chat-sidecar-reuse"
    log.append(key, "user", "the first session")

    big = [
        {"ctxId": f"old-{n}", "content": "y" * 4_000, "source": "handover", "injectedAt": 1.0}
        for n in range(20)
    ]
    kept = h.merge_pending_context([], big, final=True, archive_key=key, archive_base=tmp_path)
    log.update_metadata(key, {"pending_context": kept})
    assert h.read_ctx_overflow(key, tmp_path), "precondition: the first session spilled"

    log.delete_session(key)

    # A NEW SESSION AT THE SAME KEY. Its queue must be its own.
    log.append(key, "user", "a different session")
    inherited = [
        e.get("ctxId")
        for e in (log.get_metadata(key) or {}).get("pending_context", [])
        if isinstance(e, dict)
    ]
    assert not inherited, (
        f"the reused key inherited {len(inherited)} entries from the deleted session "
        f"({inherited[:3]}...); the sidecar outlived the transcript it belonged to"
    )


def test_a_terminal_union_spills_past_the_ceiling_into_the_sidecar(tmp_path, monkeypatch):
    """The ``final`` path returned every entry, so a close could oversize the line.

    ``_bounded_context_union`` suspended the budget entirely on a terminal save. Enough
    maximum-size same-key handovers then produced a metadata line past the session ceiling, and
    ``_maybe_rotate`` can only drop MESSAGE rows -- so the next append evicted real transcript
    rows to make room for the queue. The entries themselves must still not be dropped, so the
    excess goes to a sidecar the metadata read folds back: bounded line, nothing lost.
    """
    from kiro_crew import history as h

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 60_000)
    budget = h._SESSION_MAX_BYTES // 2

    # Every entry is a maximum-size handover, which is the finding's own precondition.
    disk = [
        {"ctxId": f"disk-{n}", "content": "d" * 4_000, "source": "handover", "injectedAt": 1.0}
        for n in range(12)
    ]
    mine = [
        {"ctxId": f"mine-{n}", "content": "m" * 4_000, "source": "handover", "injectedAt": 2.0}
        for n in range(12)
    ]
    assert (
        sum(h._ctx_entry_persist_cost(e) for e in [*disk, *mine]) > budget
    ), "precondition: the union alone exceeds the persistable budget"

    merged = h.merge_pending_context(
        disk, mine, final=True, archive_key="chat-terminal-ceiling", archive_base=tmp_path
    )

    kept_cost = sum(h._ctx_entry_persist_cost(e) for e in merged)
    assert kept_cost <= budget, (
        f"a terminal save wrote {kept_cost} bytes of queued context onto one metadata line "
        f"against a {budget}-byte budget; the next append rotates transcript rows away to fit it"
    )

    # NOTHING MAY BE LOST, only relocated: every entry absent from the line is in the archive.
    kept_ids = {e["ctxId"] for e in merged}
    missing = {e["ctxId"] for e in [*disk, *mine]} - kept_ids
    assert missing, "precondition: the bound actually had to shed something"
    archived: set[str] = set()
    for row in h.read_ctx_overflow("chat-terminal-ceiling", tmp_path):
        if isinstance(row.get("ctxId"), str):
            archived.add(row["ctxId"])
    assert missing <= archived, f"shed without a durable copy: {sorted(missing - archived)}"


def test_a_close_save_defers_nothing_because_no_later_save_can_retry(tmp_path, monkeypatch):
    """Deferring on close discarded acknowledged context permanently.

    The deferral is safe only because a later save retries it -- a save does not clear
    ``_pending_context``. On CLOSE there is no later save and the slot goes away, so a deferred
    entry is silently lost. Nothing is therefore held back for a retry; entries past the budget
    are SPILLED to the durable archive rather than deferred, so every one keeps a copy.
    """
    from kiro_crew import history as h
    from kiro_crew.history import merge_pending_context

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)

    disk = [
        {"content": "d" * 4_000, "ctxId": f"disk-{i}", "injectedAt": float(i)} for i in range(6)
    ]
    mine = [
        {"content": "m" * 4_000, "ctxId": f"mine-{i}", "injectedAt": 100.0 + i} for i in range(6)
    ]

    # An ORDINARY save still defers -- that arm is what the budget exists for.
    ordinary = {e["ctxId"] for e in merge_pending_context(disk, mine)}
    assert not all(e["ctxId"] in ordinary for e in mine), "precondition: a normal save defers"

    closing = {
        e["ctxId"]
        for e in merge_pending_context(
            disk, mine, final=True, archive_key="chat-close", archive_base=tmp_path
        )
    }
    spilled: set[str] = set()
    for row in h.read_ctx_overflow("chat-close", tmp_path):
        if isinstance(row.get("ctxId"), str):
            spilled.add(row["ctxId"])
    lost = [e["ctxId"] for e in (*disk, *mine) if e["ctxId"] not in (closing | spilled)]
    assert not lost, (
        f"the close save dropped acknowledged entries {lost}; no later save exists to retry "
        "them and the slot is going away, so the content is permanently gone"
    )


def test_the_union_never_sheds_an_entry_that_only_exists_on_disk(monkeypatch):
    """The bound shed acknowledged content with no recovery path.

    The two sides differ in kind. An ON-DISK entry's only home is the line being rewritten, so
    dropping it destroys it. The WRITER's own entries stay in ``_pending_context`` -- a save does
    not clear it, only ``drain_pending_context`` does -- so holding one back defers it to the
    next save instead of losing it. Shedding was therefore only ever safe on the writer's side.
    """
    from kiro_crew import history as h
    from kiro_crew.history import merge_pending_context

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)

    disk = [
        {"content": "d" * 4_000, "ctxId": f"disk-{i}", "injectedAt": float(i)} for i in range(9)
    ]
    mine = [
        {"content": "m" * 4_000, "ctxId": f"mine-{i}", "injectedAt": 100.0 + i} for i in range(9)
    ]
    # PRECONDITION: the on-disk side ALONE is already past the half budget, so a bound that
    # trims indiscriminately must reach into it.
    assert sum(h._ctx_entry_persist_cost(e) for e in disk) > h._SESSION_MAX_BYTES // 2

    merged = merge_pending_context(disk, mine)
    kept = {e["ctxId"] for e in merged}
    missing_disk = [e["ctxId"] for e in disk if e["ctxId"] not in kept]
    assert not missing_disk, (
        f"the union dropped on-disk entries {missing_disk}; the line being rewritten is their "
        "only durable home, so that is unrecoverable loss of content a 200 acknowledged"
    )

    # The writer's side IS gated -- that is the bound doing its job -- and those entries stay
    # queued in memory, so the disposition is a deferral rather than a drop.
    assert not all(e["ctxId"] in kept for e in mine), "the writer's side must still be bounded"


def test_a_repeated_handover_union_cannot_oversize_the_metadata_line(monkeypatch, tmp_path):
    """Repeated same-key handovers grew the line until rotation ate the transcript.

    Per-slot admission bounds EACH queue, but the rows-only union merges a DIFFERENT holder's
    queue onto the same line and re-checks no aggregate. ``_maybe_rotate`` can only drop MESSAGE
    lines -- never the metadata one -- so an oversized line evicts real transcript rows instead.
    """
    import json

    from kiro_crew import history as h
    from kiro_crew.history import merge_pending_context

    # A small session budget makes the boundary reachable without allocating 10MB; the bound
    # reads this value live, exactly as the rotation path does.
    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 60_000, raising=True)

    def _holder(tag, n):
        return [
            {"content": tag * 4_000, "ctxId": f"id-{tag}-{i}", "injectedAt": float(i)}
            for i in range(n)
        ]

    merged = merge_pending_context(_holder("a", 10), _holder("b", 10))
    line = json.dumps({"_type": "session", "pending_context": merged}) + "\n"
    line_bytes = len(line.encode("utf-8"))
    assert line_bytes <= h._SESSION_MAX_BYTES, (
        f"the handover union produced a {line_bytes}-byte metadata line against a "
        f"{h._SESSION_MAX_BYTES}-byte session budget; rotation can only drop message lines, "
        "so this silently evicts real transcript rows"
    )

    # THE NAMED HARM, exercised through the real rotation path rather than asserted about.
    path = tmp_path / "dashboard_chat-ctx-rotate.jsonl"
    rows = [json.dumps({"role": "user", "content": f"row-{i}"}) + "\n" for i in range(12)]
    path.write_text(line + "".join(rows), encoding="utf-8")
    h.ConversationLog(tmp_path)._maybe_rotate(path, "dashboard_chat-ctx-rotate")
    survived = [ln for ln in path.read_text(encoding="utf-8").splitlines() if '"role"' in ln]
    assert len(survived) == len(rows), (
        f"rotation kept only {len(survived)} of {len(rows)} ordinary transcript rows -- the "
        "oversized context line pushed real history out"
    )
