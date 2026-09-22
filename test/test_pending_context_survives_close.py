"""Undrained pending context survives a close, a reopen, and a gateway restart.

`slot._pending_context` was in-memory ONLY. Nothing serialized it, and the close
path pops the slot from `state._slots`, so an entry a producer was told was
accepted (a 200 from `/context` or `/note`) was discarded with no trace.

These tests pin the round trip through a REAL ConversationLog (`_make_state`
supplies one), so they exercise the actual metadata line rather than a mock of it.
`test_close_then_rehydrate_recovers_context` fails on an unfixed tree.

Clearing matters as much as recovery: `pending_context` is a SLOT-OWNED key, so
absence means "cleared", and that is what retires the persisted copy once a drain
empties the queue. FOUR HYDRATION SITES exist for such a key and each is covered,
because a site that skips the restore comes up empty and lets the next forced save
DELETE the stored copy -- worse than never persisting it.
"""

from __future__ import annotations

import json
import time
import uuid

from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_slots as cs
from kiro_crew.dashboard.chat_persistence import (
    _apply_recent_session,
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.chat_runner import drain_pending_context
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import (
    _MAX_PENDING_CONTEXT,
    _MAX_PERSISTED_CONTEXT_BYTES,
    _ChatSlot,
)
from kiro_crew.history import SLOT_OWNED_META_KEYS


def _entry(
    content: str,
    *,
    source: str = "test",
    max_age: float | None = 86400,
    injected_at: float | None = None,
    **extra: object,
) -> dict:
    """A pending-context entry in the shape `_build_pending_context_entry` produces.

    No ``ephemeral`` key: the builder omits it unless a caller asks, and it now means
    MEMORY-ONLY, so stamping every fixture entry would withhold the whole queue from
    disk and leave these tests asserting over an empty file.
    """
    e: dict = {
        "content": content,
        "source": source,
        "injectedAt": time.time() if injected_at is None else injected_at,
        "maxAge": max_age,
        "ctxId": uuid.uuid4().hex,
    }
    e.update(extra)
    return e


def _seed(state, key: str, entries: list[dict]) -> _ChatSlot:
    """A titled, published slot carrying *entries*."""
    slot = _ChatSlot(key)
    slot.title = f"title-{key}"
    slot._titled = True
    slot.append(role="user", content="a real message", cls="msg msg-u")
    for e in entries:
        slot.append_pending_context(e)
    state._slots[key] = slot
    return slot


def _saved_meta(state, slot) -> dict:
    """Metadata read through the key the SAVE writes under.

    The bare slot name returns {} for every session, which would make an absence
    assertion pass vacuously.
    """
    return state.conversation_log.get_metadata(slot_history_key(slot))


# ── ownership ────────────────────────────────────────────────────────────────


def test_pending_context_is_a_slot_owned_key():
    """Absence must CLEAR, which is what retires the copy after a drain."""
    assert "pending_context" in SLOT_OWNED_META_KEYS


# ── the four hydration sites ─────────────────────────────────────────────────


def test_close_then_rehydrate_recovers_context(tmp_path):
    """Site 1 of 4: `_rehydrate_slot_from_history` (gateway restart)."""
    state = _make_state(tmp_path)
    key = "chat-ctx-1"
    _seed(state, key, [_entry("first"), _entry("second")])

    _save_slot_to_history(state, state._slots[key], closed=True, closed_at=time.time())
    # The close pops the slot; the reopen must not read in-memory leftovers.
    state._slots.pop(key)

    restored = _rehydrate_slot_from_history(state, key, adopt_closed=True)
    assert restored is not None
    assert [e["content"] for e in restored._pending_context] == ["first", "second"]


def test_apply_recent_session_recovers_context(tmp_path):
    """Site 3 of 4: `_apply_recent_session`.

    Uncovered, this path hydrates an empty queue and the next forced save DELETES
    the stored copy, so the omission lost context rather than merely failing to
    restore it.
    """
    state = _make_state(tmp_path)
    key = "chat-ctx-recent"
    slot = _seed(state, key, [_entry("via recent")])
    _save_slot_to_history(state, slot, closed=True, closed_at=time.time())
    meta = _saved_meta(state, slot)
    assert meta.get("pending_context"), "precondition: the copy must be on disk"

    fresh_name = f"{key}-restored"
    _apply_recent_session(
        state,
        slot_history_key(slot),
        fresh_name,
        {},
        meta,
        [],
        conv_log=state.conversation_log,
        kiro_model_map={},
        restore_cfg=None,
        member_identity=None,
    )
    assert fresh_name in state._slots
    assert [e["content"] for e in state._slots[fresh_name]._pending_context] == ["via recent"]


def test_channel_surfacing_recovers_context(tmp_path):
    """Site 4 of 4: `surface_channel_session` (the Slack backfill shares this queue).

    Calls the real function rather than `restore_pending_context` directly — a test
    that reaches past the hydrate leaves site 4 unpinned, since deleting its call
    site would not fail anything.
    """
    state = _make_state(tmp_path)
    src = _ChatSlot("chat-ctx-chan-src")
    src.append_pending_context(_entry("via channel"))
    meta = {"pending_context": src.export_pending_context()}

    slot = cs.surface_channel_session(
        state,
        {"key": "slack_1712_44"},
        meta,
        [],
        session_key="slack:1712.44",
    )
    assert slot is not None, "the session must be newly surfaced for this to assert anything"
    assert [e["content"] for e in slot._pending_context] == ["via channel"]


# ── expiry ───────────────────────────────────────────────────────────────────


def test_expiry_is_wall_clock_across_the_close(tmp_path):
    """maxAge keeps running while shut, so stale context does not come back."""
    state = _make_state(tmp_path)
    key = "chat-ctx-2"
    stale = _entry("stale", max_age=60, injected_at=time.time() - 3600)
    live = _entry("live", max_age=86400)
    slot = _seed(state, key, [live])
    # Seated directly: append_pending_context refuses an already-dead entry, and
    # this test is about the entry being dead on the way BACK, not on the way in.
    slot._pending_context.insert(0, stale)

    _save_slot_to_history(state, slot, closed=True, closed_at=time.time())
    # The stale entry must not even reach disk -- otherwise the "only inflates the
    # metadata line" rationale for filtering at export is untested.
    assert [e["content"] for e in _saved_meta(state, slot)["pending_context"]] == ["live"]
    state._slots.pop(key)

    restored = _rehydrate_slot_from_history(state, key, adopt_closed=True)
    assert restored is not None
    assert [e["content"] for e in restored._pending_context] == ["live"]


def test_the_budget_is_derived_from_the_escaped_width():
    from kiro_crew.dashboard.state import MAX_CONTEXT_CONTENT

    escaped = len(json.dumps("\U0001f600" * MAX_CONTEXT_CONTENT).encode("utf-8"))
    assert escaped <= _MAX_PERSISTED_CONTEXT_BYTES


def test_an_ephemeral_entry_is_never_written_to_disk():
    """Design suggestion: honour `ephemeral` rather than silently ignoring it.

    The flag was free while every queue was memory-only; persisting the queue is what
    gave it teeth, so it is honoured at the one seam between the queue and disk.
    """
    slot = _ChatSlot("chat-ctx-eph")
    durable = _entry("keep")
    transient = _entry("transient", ephemeral=True)
    slot._pending_context = [durable, transient]

    exported = [e.get("ctxId") for e in slot.export_pending_context()]
    assert exported == [durable["ctxId"]], f"an ephemeral entry must not persist: {exported}"
    # Still injectable: the flag bounds DURABILITY, not delivery.
    assert len(slot._pending_context) == 2, "the live queue is unaffected by the flag"


def test_an_omitted_ephemeral_flag_stays_memory_only(tmp_path):
    """Restored: an omitted flag must NOT begin writing a caller's content to disk.

    Two lanes independently flagged the inverted default as a one-way contract change for every
    external caller that omitted the flag, with both in-repo callers passing it explicitly. The
    default is memory-only again, so durability is opt-IN via an explicit ``ephemeral: false``.
    """
    import inspect

    from kiro_crew.dashboard import chat_handlers as ch

    for fn in (ch.api_chat_slot_context, ch.api_chat_slot_note):
        src = " ".join(inspect.getsource(fn).split())
        assert 'body.get("ephemeral", True)' in src, (
            f"{fn.__name__} still defaults `ephemeral` to False, so a caller that names nothing "
            "has its content written to disk -- a contract change it never asked for"
        )
        # CONTROL: the flag must still be READ, or a default of True would be unreachable.
        assert 'body.get("ephemeral"' in src


def test_a_rows_only_handover_keeps_both_holders_queued_context():
    """GPT BLOCKING: a rows-only handover dropped the writing slot's queued context.

    `pending_context` is inside `ROWS_ONLY_DEFERRED_META_KEYS` by construction (it is
    slot-owned, and the rows-only set is a difference of that), so the branch carried
    the OTHER holder's copy back verbatim and the writing slot's acknowledged entries
    reached no durable home on that file.

    Both are acknowledged, so the union keeps both. Asserted on the union helper,
    which is the single place the rule lives.
    """
    from kiro_crew.history import ROWS_ONLY_DEFERRED_META_KEYS, merge_pending_context

    assert "pending_context" in ROWS_ONLY_DEFERRED_META_KEYS, (
        "precondition: the deferred set is what drops it, so if this ever stops "
        "holding the union below is guarding nothing"
    )

    holder = [{"content": "theirs", "ctxId": "id-holder", "injectedAt": 1.0}]
    writer = [{"content": "mine", "ctxId": "id-writer", "injectedAt": 2.0}]

    merged = merge_pending_context(holder, writer)
    assert [e["content"] for e in merged] == [
        "theirs",
        "mine",
    ], f"neither holder's acknowledged context may be dropped: {merged}"

    # Idempotent: a second rows-only save re-unions its own output without growing it.
    assert merge_pending_context(merged, writer) == merged, "the union must not grow"

    # Un-identified legacy entries dedupe on content/stamp/source instead of ctxId.
    legacy = [{"content": "old", "injectedAt": 3.0, "source": "app"}]
    assert len(merge_pending_context(legacy, legacy)) == 1, "legacy entries must dedupe"

    # GPT BLOCKING (round two): a byte budget that skipped entries not fitting it
    # discarded acknowledged context -- this union's own defect, reborn as a size cap.
    big = [{"content": "x" * 40_000, "ctxId": "id-big-a", "injectedAt": 4.0}]
    big_two = [{"content": "y" * 40_000, "ctxId": "id-big-b", "injectedAt": 5.0}]
    both_big = merge_pending_context(big, big_two)
    assert [e["ctxId"] for e in both_big] == [
        "id-big-a",
        "id-big-b",
    ], f"size must never discard acknowledged context: {[e['ctxId'] for e in both_big]}"
    # The byte budget is gone BY CONSTRUCTION, not merely unused at the call site: the union
    # takes no size parameter at all, so no caller can reintroduce a size-based discard.
    import inspect

    assert "max_bytes" not in inspect.signature(merge_pending_context).parameters


def test_the_union_never_sheds_an_entry_that_only_exists_on_disk(monkeypatch):
    """GPT BLOCKING F1 (round two): the bound shed acknowledged content with no recovery path.

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
    """GPT BLOCKING F1: repeated same-key handovers grew the line until rotation ate the transcript.

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


def test_restore_respects_the_queue_ceiling():
    """A restore cannot overflow the per-slot cap."""
    slot = _ChatSlot("chat-ctx-7")
    slot.restore_pending_context([_entry(f"e{i}") for i in range(_MAX_PENDING_CONTEXT + 10)])
    assert len(slot._pending_context) <= _MAX_PENDING_CONTEXT
    assert slot._pending_context, "the cap must not empty the queue"


def test_restore_seats_a_valid_entry():
    """Guards against a restore that validates everything away."""
    slot = _ChatSlot("chat-ctx-8")
    slot.restore_pending_context([_entry("a"), _entry("b")])
    assert [e["content"] for e in slot._pending_context] == ["a", "b"]


def test_restore_rejects_a_non_positive_max_age():
    """Restore must agree with the boundary, which 400s a non-positive TTL.

    `_validate_max_age` rejects `<= 0` at the HTTP boundary, and nothing
    revalidates an entry arriving from disk, so the same rule has to run here.

    THE FUTURE `injectedAt` IS LOAD-BEARING, not scene-setting. With
    `injectedAt=now` a `maxAge` of 0 is ALREADY EXPIRED, so
    `append_pending_context` refuses it downstream and the entry never seats --
    which makes the obvious version of this test pass with the guard deleted, i.e.
    prove nothing. Dating `injectedAt` forward puts `injected_at + max_age` in the
    future, so `context_entry_expired` reports False and the ONLY thing that can
    drop these entries is the restore-time check under test.
    """
    ahead = time.time() + 3600
    slot = _ChatSlot("chat-ctx-8b")
    slot.restore_pending_context(
        [
            _entry("zero", max_age=0, injected_at=ahead),
            _entry("negative", max_age=-1, injected_at=ahead),
            _entry("kept", max_age=86400, injected_at=ahead),
        ]
    )
    assert [e["content"] for e in slot._pending_context] == [
        "kept"
    ], "a non-positive maxAge must not be seated, and a valid entry must survive"


def test_restore_returns_nothing():
    """The seated count had no consumer; it was removed rather than kept for a test."""
    slot = _ChatSlot("chat-ctx-9")
    assert slot.restore_pending_context([_entry("a")]) is None


def test_a_restored_entry_over_the_live_limit_is_refused(tmp_path):
    """GPT FINDING: restored content was only checked non-empty.

    A metadata line is operator-editable, so a 40,001-character entry bypassed the
    boundary `api_chat_slot_context` enforces on the live path.
    """
    state = _make_state(tmp_path)
    slot = _seed(state, "chat-ctx-oversize", [])

    from kiro_crew.dashboard import state as st

    over = dict(_entry("x"))
    over["content"] = "z" * (st.MAX_CONTEXT_CONTENT + 1)
    at_limit = dict(_entry("y"))
    at_limit["content"] = "z" * st.MAX_CONTEXT_CONTENT

    slot.restore_pending_context([over, at_limit])

    seated = [len(e["content"]) for e in slot._pending_context]
    assert seated == [st.MAX_CONTEXT_CONTENT], (
        "the over-limit entry must be refused and the at-limit one seated, so this "
        f"agrees with the live boundary: {seated}"
    )


def test_every_hydration_site_performs_the_restore_ritual():
    """DESIGN: the save sites were censused, the hydration sites were not.

    "Absence means cleared" makes each hydration site load-bearing in the same way a save
    site is. A restore path that reads the metadata line and skips the queue does not merely
    fail to recover it -- the slot comes up empty and the next forced save DELETES the stored
    copy, which is worse than never having persisted it at all. Enumerated here so adding a
    fifth site cannot skip the restore silently.
    """
    import inspect

    from kiro_crew.dashboard import channel_slots as cs
    from kiro_crew.dashboard import chat_handlers as ch
    from kiro_crew.dashboard import chat_persistence as cp

    sites = {
        "_rehydrate_slot_from_history": inspect.getsource(cp._rehydrate_slot_from_history),
        "_apply_recent_session": inspect.getsource(cp._apply_recent_session),
        "api_chat_slot_resume": inspect.getsource(ch.api_chat_slot_resume),
        "surface_channel_session": inspect.getsource(cs.surface_channel_session),
    }
    ritual = ("restore_pending_context(",)
    for name, body in sites.items():
        for step in ritual:
            assert step in body, f"{name} hydrates pending context WITHOUT {step}"

    # The census is only worth its cost if it covers EVERY caller, so the site list must be
    # complete. A fabricated token proves the sweep can return zero for a real absence.
    found = set()
    for mod in (cp, ch, cs):
        for line in inspect.getsource(mod).splitlines():
            if "slot.restore_pending_context(" in line:
                found.add(mod.__name__)
    assert len(sites) == 4, f"the ritual list names {len(sites)} sites, not 4"
    assert found == {
        "kiro_crew.dashboard.chat_persistence",
        "kiro_crew.dashboard.chat_handlers",
        "kiro_crew.dashboard.channel_slots",
    }, f"a hydration site moved module, so this census no longer enumerates them: {found}"
    for mod in (cp, ch, cs):
        assert "slot.restore_pending_context_CONTROL(" not in inspect.getsource(
            mod
        ), "control token matched, so the sweep above cannot distinguish present from absent"


def test_rehydrate_survives_a_mangled_timing_field(tmp_path):
    """The restart path must not pop the slot and silently lose the whole tab."""
    state = _make_state(tmp_path)
    key = "chat-ctx-mangled-2"
    slot = _seed(state, key, [_entry("good")])
    _save_slot_to_history(state, slot, closed=True, closed_at=time.time())
    state.conversation_log.update_metadata(
        slot_history_key(slot),
        {"pending_context": [{"content": "bad", "maxAge": "60"}, _entry("kept")]},
    )
    state._slots.pop(key)

    restored = _rehydrate_slot_from_history(state, key, adopt_closed=True)
    assert restored is not None, "the tab must still restore"
    assert [e["content"] for e in restored._pending_context] == ["kept"]


# ── the persistable budget ───────────────────────────────────────────────────


def test_the_union_defers_a_suffix_rather_than_reordering_the_queue(monkeypatch):
    """A refusal must carry the REST of the queue, not just the entry that did not fit.

    Testing each entry against the remaining budget alone let a large entry defer while a
    later, smaller one still fitted, so the metadata line came back holding a successor of an
    entry it had held back -- returning the queue in a different order than its entries
    arrived in, which changes what the next drain puts in front of the model.
    """
    from kiro_crew import history as h

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)
    budget = max(1, int(h._SESSION_MAX_BYTES // 2))
    big = "x" * 4_000
    per_big = h._ctx_entry_persist_cost({"ctxId": "sizing", "content": big, "injectedAt": 0.0})
    count = budget // per_big + 1

    mine = [{"ctxId": f"big-{i}", "content": big, "injectedAt": float(i)} for i in range(count)]
    # TINY tail entry: under a per-entry fit test it slips onto the line behind entries queued
    # before it, which is the reordering this pins.
    mine.append({"ctxId": "tiny-last", "content": "x", "injectedAt": float(count)})

    on_line = [e["ctxId"] for e in h.merge_pending_context(None, mine)]
    assert "tiny-last" not in on_line, (
        "a tiny trailing entry was kept while larger entries queued BEFORE it were deferred, "
        f"so the queue is reordered: {on_line[:3]}...{on_line[-2:]}"
    )
    assert on_line == [
        e["ctxId"] for e in mine[: len(on_line)]
    ], f"the kept entries must be a PREFIX of the queue, got {on_line[:8]}"
    # POSITIVE CONTROL: the same input under a budget that fits defers nothing, so the
    # assertions above measure the budget rather than some unrelated refusal.
    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000_000, raising=True)
    assert len(h.merge_pending_context(None, mine)) == len(mine)


def test_a_terminal_save_defers_nothing_because_no_later_save_can_retry(monkeypatch):
    """``final`` suspends the deferral, and it has to.

    The deferral is safe only because a later save retries it: nothing clears the live queue
    but a drain, so a deferred entry stays queued in memory. A close has no later save and the
    slot is being popped, so deferring there discards content a 200 acknowledged -- permanently,
    and with no surface reporting the loss.
    """
    from kiro_crew import history as h

    monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)
    on_disk = [
        {"content": "d" * 4_000, "ctxId": f"disk-{i}", "injectedAt": float(i)} for i in range(9)
    ]
    mine = [{"content": "newest acknowledged", "ctxId": "mine-new", "injectedAt": 99.0}]

    deferred = h.merge_pending_context(on_disk, mine)
    assert "mine-new" not in {e["ctxId"] for e in deferred}, "precondition: it defers by default"

    kept = h.merge_pending_context(on_disk, mine, final=True)
    assert "mine-new" in {e["ctxId"] for e in kept}, (
        "a terminal save dropped the slot's own newest acknowledged entry; the slot is going "
        "away, so no later save can retry it and the content is permanently gone"
    )


def test_every_union_call_in_the_save_marks_a_terminal_save_final():
    """CENSUS, because the defect this guards is a CALLER omission, not a broken helper.

    ``rows_only`` counts as terminal beside ``closed`` because its only producer runs after the
    slot has been popped. The empty-window merge counts too: its gate is an empty message
    window, and a close of a slot with no messages comes through it.
    """
    import inspect

    from kiro_crew.dashboard import chat_persistence as cp

    src = inspect.getsource(cp._save_slot_to_history)
    calls = src.count("merge_pending_context(")
    assert calls > 0, "positive control: the census can see the union call at all"
    assert src.count("final=closed or rows_only") == calls, (
        f"{calls} union call(s) inside the save but not all pass the terminal predicate; "
        "an unflagged one defers on a path where nothing retries"
    )
    # A narrower predicate is the exact defect this test exists to catch.
    assert "final=closed)" not in src, "final=closed alone misses the popped rows_only path"


def test_a_drained_entry_does_not_come_back_from_this_slots_own_line(tmp_path):
    """The disk side is taken only on a FOREIGN line, and this is why.

    A drain removes an entry in the same step that appends its user row, and that row carries
    no ``noteId``-style mark, so nothing downstream can tell a consumed entry from a queued
    one. Unioning against this slot's OWN line would therefore read the drain's own output
    back off disk and re-seat it -- on every save, forever, with no row-derived retirement
    able to stop it.
    """
    state = _make_state(tmp_path)
    key = "chat-ctx-drained"
    slot = _seed(state, key, [_entry("background note")])
    _save_slot_to_history(state, slot)
    assert _saved_meta(state, slot).get("pending_context"), "precondition: it was persisted"

    # The drain is what retires the entry; it clears the queue and appends the row.
    assert drain_pending_context(slot), "precondition: the drain returned the queued content"
    assert slot._pending_context == [], "precondition: the drain cleared the live queue"

    _save_slot_to_history(state, slot)
    assert not _saved_meta(state, slot).get("pending_context"), (
        "the save read its own drained entry back off the line and re-seated it; absence is "
        "what retires the persisted copy, so a union against this slot's own line never clears"
    )

    state._slots.pop(key)
    restored = _rehydrate_slot_from_history(state, key, adopt_closed=True)
    assert restored is not None
    assert restored._pending_context == [], "a delivered entry must not be re-injected on reopen"
