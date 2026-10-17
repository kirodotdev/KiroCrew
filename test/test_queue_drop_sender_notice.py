"""A dropped cross-session delivery is reported back to its SENDER.

``session_send`` into a busy target answers ``started: False``: the message was
accepted onto the target's queue and will run on its next turn. When the drain
later drops that entry because the target's containment lapsed, every signal of
the drop is on the TARGET — the retracted queue card, the visible notice, the
broadcast — and the sender reads none of them, so a caller coordinating several
sessions keeps waiting for a reply that cannot arrive.

These tests pin the two halves that close it. The admission stamps the sending
session into the entry's durable ``meta`` beside the containment snapshot, as a
slot key plus that slot's tab identity (``send_origin_meta``), and the drop reads
that stamp back and appends a notice to the sender's own transcript
(``notify_send_origin_dropped``), naming the target, the constraint that changed
and an excerpt of the dropped text. They also pin the five cases that
deliberately produce NO notice: a human-typed entry (no sender), a session that
queued onto itself (the target's own notice is the one it reads), a sender closed
while the message waited (no transcript left), a key whose current occupant is a
different tab (the sender is gone, wearing its name), and a structurally exempt
entry (never dropped at all).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    slot_history_key,
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Run in the shipped (enabled) session-control state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _inline_audit(monkeypatch):
    """Route SEL writes inline to a mock: assertable, and no executor thread
    outlives the test."""
    fake = MagicMock()
    monkeypatch.setattr(sc, "sel", lambda: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    return fake


def _busy(slot):
    """``running`` is derived (``task is not None and not task.done()``)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _link(slot):
    slot.linked_session_key = "C0LINKED|1700000000.000100"
    return slot


async def _never_runs(state, slot, prompt):  # pragma: no cover - queued, not run
    raise AssertionError("a queued prompt must not start a turn at enqueue")


def _send(state, caller, target_key, message):
    """One ``session_send`` into a busy target, which queues it."""
    return asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=slot_history_key(caller),
            target=target_key,
            message=message,
        )
    )


def _notices(slot):
    return [m["content"] for m in slot.messages if m.get("role") == "notice"]


# ── The admission stamps the sender ──────────────────────────────────────────


def test_session_send_stamps_the_sending_slot(tmp_path):
    """The queue arm records WHO sent it, beside the containment snapshot the
    drain reads for its own decision."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))

    out = _send(state, caller, "chat-2", "queued relay")

    assert out["started"] is False
    entry_meta = target._queue[0]["meta"]
    assert entry_meta[sc.SEND_ORIGIN_META_KEY] == {"slot": caller.key, "tab": caller._tab_id}
    # The stamp rides ALONGSIDE the containment snapshot; it never displaces it,
    # because that snapshot is the drain's authorization input.
    assert entry_meta[sc.QUEUED_CONTAINMENT_META_KEY]


def test_human_typed_enqueue_carries_no_sender_stamp(tmp_path):
    """The composer has no peer waiting on the message, so the key is absent
    rather than present and blank — absent is what means "nobody to tell"."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))

    slot.enqueue_or_run_prompt("typed by a person", _never_runs, state)

    assert sc.SEND_ORIGIN_META_KEY not in slot._queue[0].get("meta", {})


def test_extra_meta_cannot_displace_the_containment_snapshot(tmp_path):
    """A caller's extra fields are descriptive. The drain's own authorization
    input must not be replaceable through them."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))

    slot.enqueue_or_run_prompt(
        "forged",
        _never_runs,
        state,
        extra_meta={sc.QUEUED_CONTAINMENT_META_KEY: {"linked": True}},
    )

    snap = slot._queue[0]["meta"][sc.QUEUED_CONTAINMENT_META_KEY]
    assert snap["linked"] is False


# ── The drop reports back ────────────────────────────────────────────────────


def test_drop_notifies_a_live_sender(tmp_path):
    """The whole point: the sender's own transcript learns the message will
    never run, naming the target and the constraint that changed."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "please summarize the log")

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert target._queue == []
    sent = _notices(caller)
    assert sent, "the sending session was told nothing"
    assert "chat-2" in sent[-1]
    assert "linked to a channel" in sent[-1]
    assert "will not run" in sent[-1]
    # The target keeps its own notice: both sides are told, neither replaces
    # the other.
    assert any("Queued message dropped" in n for n in _notices(target))


def test_sender_notice_quotes_the_dropped_text(tmp_path):
    """A caller can hold several deliveries in flight, so the target's key alone
    does not say WHICH message went."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "rebuild the index")

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert "rebuild the index" in _notices(caller)[-1]


def test_each_sender_is_told_about_its_own_message(tmp_path):
    """One sweep can drop entries from several senders. Each learns about its
    own delivery and is not told about a peer's."""
    state = _make_state(tmp_path)
    first = state.get_or_create_slot("chat-1")
    second = state.get_or_create_slot("chat-2")
    target = _busy(state.get_or_create_slot("chat-3"))
    _send(state, first, "chat-3", "alpha task")
    _send(state, second, "chat-3", "beta task")

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert target._queue == []
    assert "alpha task" in _notices(first)[-1]
    assert not any("beta task" in n for n in _notices(first))
    assert "beta task" in _notices(second)[-1]
    assert not any("alpha task" in n for n in _notices(second))


def test_requeued_steer_notifies_its_sender(tmp_path):
    """A steer that the turn's teardown degrades to a queue card faces the drain
    like any queued delivery, and the requeue carries the send's stamp onto the
    entry, so its drop reports back the same way."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target._pending_steers = ["stop and reread the spec"]
    target._steer_admissions["stop and reread the spec"] = {
        **sc.containment_meta(state, target),
        **sc.send_origin_meta(state, caller.key),
    }

    cr._requeue_unconsumed_steers(state, target)
    assert target._queue[0]["meta"][sc.SEND_ORIGIN_META_KEY] == {
        "slot": caller.key,
        "tab": caller._tab_id,
    }

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert "stop and reread the spec" in _notices(caller)[-1]


def test_drop_audit_names_the_sender(tmp_path, _inline_audit):
    """The SEL row names who was waiting on the dropped message, so the outcome
    stays recoverable from the trail even when the sender's session is gone."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "queued relay")

    _link(target)
    cr._drop_stale_admissions(state, target)

    call = _inline_audit.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "denied"
    assert call.kwargs["metadata"]["origin"] == caller.key


# ── The four deliberate non-notices ──────────────────────────────────────────


def test_human_typed_drop_notifies_nobody(tmp_path):
    """An unstamped entry is a person's own typing. The target's notice is the
    whole report, exactly as it is without this feature."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("typed by a person", _never_runs, state)

    _link(slot)
    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []
    assert len(_notices(slot)) == 1
    assert "Queued message dropped" in _notices(slot)[0]


def test_drop_audit_omits_origin_when_there_is_no_sender(tmp_path, _inline_audit):
    """Omitted, not recorded blank: a reader must be able to tell "a person typed
    this" from "a cross-session delivery whose sender is unnamed"."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("typed by a person", _never_runs, state)

    _link(slot)
    cr._drop_stale_admissions(state, slot)

    call = _inline_audit.log_tool_invocation.call_args
    assert "origin" not in call.kwargs["metadata"]


def test_self_send_gets_one_notice_not_two(tmp_path):
    """A session that queued onto itself already reads the target notice. A
    second row would report one drop twice."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt(
        "my own follow-up",
        _never_runs,
        state,
        extra_meta=sc.send_origin_meta(state, slot.key),
    )

    _link(slot)
    cr._drop_stale_admissions(state, slot)

    assert len(_notices(slot)) == 1


def test_closed_sender_drops_silently_and_keeps_the_audit(tmp_path, _inline_audit):
    """The sender was closed while the message waited, so there is no transcript
    to write to. The drop still happens and the trail still names it."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "queued relay")
    state._slots.pop(caller.key)

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert target._queue == []
    assert _notices(caller) == []
    assert _inline_audit.log_tool_invocation.call_args.kwargs["metadata"]["origin"] == caller.key


def test_exempt_entries_are_not_dropped_so_nobody_is_told(tmp_path):
    """Cron notifications and sub-agent completions are the runner's own
    machinery. They never reach the drop path, so they never notify."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    slot = _busy(_link(state.get_or_create_slot("chat-2")))
    origin = sc.send_origin_meta(state, caller.key)
    slot.queue_append("cron fired", CRON_NOTIFICATION_KIND, dict(origin))
    slot.queue_append("subagent finished", SUBAGENT_COMPLETION_KIND, dict(origin))

    cr._drop_stale_admissions(state, slot)

    assert len(slot._queue) == 2
    assert _notices(caller) == []


# ── The helpers' own edges ───────────────────────────────────────────────────


def test_send_origin_meta_omits_an_empty_key(tmp_path):
    """A caller with no slot of its own stamps nothing, so the absent key keeps
    its one meaning."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-7")

    assert sc.send_origin_meta(state, "") == {}
    assert sc.send_origin_meta(state, "chat-7") == {
        sc.SEND_ORIGIN_META_KEY: {"slot": "chat-7", "tab": caller._tab_id}
    }


def test_send_origin_meta_omits_a_key_with_no_tab_identity(tmp_path):
    """A stamp whose identity cannot be checked later must not exist.

    Two shapes reach here: a key naming no live slot, and a live slot carrying no
    ``_tab_id``. Stamping either would leave the drop resolving a recipient it
    cannot verify, which is the whole hazard, so both omit the key instead.
    """
    state = _make_state(tmp_path)
    tabless = state.get_or_create_slot("chat-9")
    tabless._tab_id = ""

    assert sc.send_origin_meta(state, "chat-never-existed") == {}
    assert sc.send_origin_meta(state, "chat-9") == {}


def test_the_stamp_carries_both_fields_or_neither(tmp_path):
    """The two readers fail closed on the same shapes.

    A half-written stamp must not answer the key question while the identity
    question silently passes, because a blank tab compared against a live slot's
    real tab is the one comparison that has to refuse.
    """
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    meta = sc.send_origin_meta(state, caller.key)

    assert sc.send_origin_slot(meta) == caller.key
    assert sc.send_origin_tab(meta) == caller._tab_id
    assert caller._tab_id


@pytest.mark.parametrize(
    "meta",
    [
        None,
        "not a dict",
        42,
        {},
        {sc.SEND_ORIGIN_META_KEY: 17},
        {sc.SEND_ORIGIN_META_KEY: None},
        {sc.SEND_ORIGIN_META_KEY: "chat-1"},
        {sc.SEND_ORIGIN_META_KEY: {"slot": 17, "tab": 17}},
        {sc.SEND_ORIGIN_META_KEY: {}},
    ],
)
def test_send_origin_readers_read_junk_as_no_sender(meta):
    """Entry meta is plumbing of any shape, so anything that is not the two-field
    stamp reads as no sender and the drop proceeds unchanged. A bare string is in
    this list deliberately: it is the shape a stamp without identity would take,
    and it must not be honoured."""
    assert sc.send_origin_slot(meta) == ""
    assert sc.send_origin_tab(meta) == ""


def test_send_drop_excerpt_collapses_and_marks_the_cut():
    """A multi-line prompt stays one line in the notice, and a truncated quote is
    never mistaken for the whole text."""
    assert sc.send_drop_excerpt("two\n\nlines  here") == "two lines here"
    long = "x" * (sc.SEND_DROP_EXCERPT_CHARS + 50)
    cut = sc.send_drop_excerpt(long)
    assert len(cut) == sc.SEND_DROP_EXCERPT_CHARS + 1
    assert cut.endswith("…")
    assert sc.send_drop_excerpt(None) == ""


def test_notifier_answers_false_without_writing_when_there_is_no_sender(tmp_path):
    """The four non-notice cases are reported as False and are not failures."""
    state = _make_state(tmp_path)
    target = state.get_or_create_slot("chat-2")

    assert (
        sc.notify_send_origin_dropped(
            state, origin="", target_slot=target, text="t", constraints=["linked"]
        )
        is False
    )
    assert (
        sc.notify_send_origin_dropped(
            state, origin=target.key, target_slot=target, text="t", constraints=["linked"]
        )
        is False
    )
    assert (
        sc.notify_send_origin_dropped(
            state, origin="chat-gone", target_slot=target, text="t", constraints=["linked"]
        )
        is False
    )
    sender = state.get_or_create_slot("chat-1")
    assert (
        sc.notify_send_origin_dropped(
            state,
            origin=sender.key,
            origin_tab="a-tab-that-never-existed",
            target_slot=target,
            text="t",
            constraints=["linked"],
        )
        is False
    )
    assert _notices(sender) == []


def test_an_absent_tab_cannot_skip_the_identity_check(tmp_path):
    """Omitting the check's input must not pass the check.

    A live slot always carries a ``_tab_id``, so an empty ``origin_tab`` can only
    come from a stamp that was never identity-bearing. It answers False rather
    than defaulting to "close enough", which is what stops the check from being
    optional at the call site.
    """
    state = _make_state(tmp_path)
    sender = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    assert (
        sc.notify_send_origin_dropped(
            state, origin=sender.key, target_slot=target, text="t", constraints=["linked"]
        )
        is False
    )
    assert _notices(sender) == []


def test_a_reused_slot_key_does_not_receive_the_previous_tenants_notice(tmp_path):
    """The hazard the tab identity exists for.

    Slot keys are reused: a plain ``get_or_create_slot(name)`` mints a fresh slot
    object on a free key, and the explicitly-named keys are deterministic
    (``cron-{job.id}``, ``workflow-{run_id}``, a channel's own), so a closed
    sender's key is handed to the next occupant, whose link scope and audience are
    declared per creation. Resolving the notice from the key alone puts the old
    sender's text, excerpt included, on a session that never sent it and cannot
    retract it.
    """
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "the previous tenant's secret")

    # The sender closes and the SAME key is recreated by a different occupant.
    del state._slots["chat-1"]
    replacement = state.get_or_create_slot("chat-1")
    assert replacement._tab_id != caller._tab_id

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert _notices(replacement) == []
    assert not any("previous tenant's secret" in m["content"] for m in replacement.messages)


def test_the_original_sender_is_still_told_on_its_own_tab(tmp_path):
    """The control for the test above: with the SAME slot object still live, the
    identity matches and the notice lands. Without this, a check that refused
    everything would look correct."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "still mine")

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert len(_notices(caller)) == 1
    assert "still mine" in _notices(caller)[-1]


def test_a_failing_notice_does_not_block_the_drop(tmp_path, monkeypatch):
    """Withholding the message is the authorization decision. It must not depend
    on the report landing."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "queued relay")
    monkeypatch.setattr(
        sc, "send_drop_excerpt", MagicMock(side_effect=RuntimeError("excerpt failed"))
    )

    _link(target)
    cr._drop_stale_admissions(state, target)

    assert target._queue == []
    assert any("Queued message dropped" in n for n in _notices(target))
    assert _notices(caller) == []


def test_unverified_mirror_wording_reaches_the_sender_too(tmp_path):
    """A drain-side probe failure refuses delivery but must not assert a mirror
    appeared — in the sender's notice as much as the target's."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    _send(state, caller, "chat-2", "queued relay")
    state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store down"))

    cr._drop_stale_admissions(state, target)

    assert target._queue == []
    sent = _notices(caller)[-1]
    assert "could not be verified" in sent
    assert "gained an outbound channel mirror" not in sent
