"""Unit tests for ``_is_interrupted`` — which turn endings count as interrupted.

This predicate has two consumers that must not disagree: it selects the
continuation body handed to the model (``_MANUAL_RESUME_MSG`` vs
``_MANUAL_CONTINUE_MSG``), and its frontend mirror ``selectTurnInterrupted``
(``website/src/store/chatSlice.ts``) decides whether the composer offers the
Resume control at all. A divergence means the button promises one thing and the
agent is told another.

The stop cases below are the reason this file exists. Pressing Stop *before* the
reply emitted any text leaves ``[user, stop_event]`` — tail-identical to a
gateway that died before the first output — so without an explicit stop branch
the same visible user action read as "interrupted" or "finished" depending only
on whether a reply segment had flushed first.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from kiro_crew.dashboard.chat_handlers import _is_interrupted


def slot(*messages: dict) -> SimpleNamespace:
    """Minimal stand-in — ``_is_interrupted`` only reads ``.messages``."""
    return SimpleNamespace(messages=list(messages))


def user(content: str = "do the thing") -> dict:
    return {"role": "user", "content": content}


def assistant(content: str = "on it") -> dict:
    return {"role": "assistant", "content": content}


def error(content: str = "connection lost") -> dict:
    return {"role": "error", "content": content}


def stop_event(*, top_level: bool = False) -> dict:
    """The card recorded when the user presses Stop.

    ``top_level=True`` exercises the ``kind`` field alone: the websocket path
    sets both ``kind`` and ``meta.kind``, but a row rehydrated from disk can
    arrive carrying only one, so both spellings must be recognised.
    """
    if top_level:
        return {"role": "system", "content": "Stopped", "kind": "stop_event"}
    return {"role": "system", "content": "Stopped", "meta": {"kind": "stop_event"}}


def stop_event_live(state: str = "stopping") -> dict:
    """The shape a stop ACTUALLY has in the live in-memory window.

    This is the production shape and the one that matters most. The route does
    ``slot.append("system", stop_msg, stop_msg)`` with no ``meta=`` kwarg, so
    ``append`` never creates a ``meta`` key: the discriminator exists only as
    JSON inside ``cls`` (and ``content``). ``parse_cls_meta()`` unpacks it on
    the way out to a client, which is why the frontend sees ``meta.kind`` and
    this module does not.

    An earlier version of the stop branch checked only ``kind``/``meta.kind``
    and so matched the two fixtures above while never matching a real stop --
    the tests passed and the behaviour was still broken. Keep this case.
    """
    payload = json.dumps(
        {"kind": "stop_event", "id": "stop-abc123", "state": state, "outcome": None}
    )
    return {"role": "system", "content": payload, "cls": payload}


class TestInterrupted:
    def test_nothing_came_back_is_interrupted(self):
        # A gateway restart mid-turn leaves exactly this.
        assert _is_interrupted(slot(user())) is True

    def test_error_trailing_a_reply_is_interrupted(self):
        assert _is_interrupted(slot(user(), assistant(), error())) is True

    def test_clean_completion_is_not_interrupted(self):
        assert _is_interrupted(slot(user(), assistant())) is False

    def test_superseded_error_is_not_interrupted(self):
        # The failure is history; the newest turn finished.
        assert _is_interrupted(slot(user(), error(), user(), assistant())) is False

    def test_empty_transcript_is_not_interrupted(self):
        assert _is_interrupted(slot()) is False


class TestDeliberateStop:
    """A user-initiated Stop is an ENDING, not an interruption."""

    def test_stop_before_any_reply_text(self):
        assert _is_interrupted(slot(user(), stop_event())) is False

    def test_stop_mid_reply(self):
        assert _is_interrupted(slot(user(), assistant(), stop_event())) is False

    def test_both_stop_shapes_agree(self):
        # The whole point: the two differ only by invisible timing, so a user
        # pressing Stop must get the same answer either way.
        early = _is_interrupted(slot(user(), stop_event()))
        late = _is_interrupted(slot(user(), assistant(), stop_event()))
        assert early == late is False

    def test_top_level_kind_field_is_recognised(self):
        assert _is_interrupted(slot(user(), stop_event(top_level=True))) is False

    # ---- the production shape (regression: this is what was actually broken) --

    def test_live_window_shape_before_any_reply(self):
        # The real thing: kind only inside the JSON `cls`, no `meta` key at all.
        assert _is_interrupted(slot(user(), stop_event_live())) is False

    def test_live_window_shape_mid_reply(self):
        assert _is_interrupted(slot(user(), assistant(), stop_event_live())) is False

    def test_all_three_carriers_agree(self):
        # Whichever door the row came through, one user action means one answer.
        assert (
            _is_interrupted(slot(user(), stop_event()))
            == _is_interrupted(slot(user(), stop_event(top_level=True)))
            == _is_interrupted(slot(user(), stop_event_live()))
            is False
        )

    def test_unparseable_cls_does_not_crash_or_swallow(self):
        # A non-JSON `cls` must not raise and must not be mistaken for a stop:
        # this row is a plain system line, so the user row still governs.
        row = {"role": "system", "content": "note", "cls": "msg msg-sys"}
        assert _is_interrupted(slot(user(), row)) is True

    def test_other_json_cls_kinds_are_not_stops(self):
        # `cls` carries JSON for several card kinds; only stop_event may end a turn.
        row = {
            "role": "system",
            "content": "x",
            "cls": json.dumps({"kind": "permission_request", "id": "p1"}),
        }
        assert _is_interrupted(slot(user(), row)) is True

    def test_older_stop_does_not_suppress_a_later_failure(self):
        # A stop card deeper in history must not mask a genuine interruption on
        # the newest turn.
        assert _is_interrupted(slot(user(), stop_event(), user())) is True

    def test_older_stop_does_not_suppress_a_later_error(self):
        assert (
            _is_interrupted(slot(user(), stop_event(), user(), assistant(), error()))
            is True
        )


def compaction_notice(content: str = "Conversation compacted: summary") -> dict:
    """A compaction result row: an assistant row tagged ``meta.kind="compaction"``.

    Four writers emit this shape -- ``chat_utils._append_compaction_notice``
    plus three direct ``slot.append`` sites in ``state.py`` (the proactive
    auto-compact paths, which bypass the chat_utils helper to avoid an import
    cycle). The tag, not the writer, is what this predicate keys on.
    """
    return {"role": "assistant", "content": content, "meta": {"kind": "compaction"}}


class TestCompletedCompaction:
    """A ``/compact`` answered by its compaction notice is FINISHED.

    The five tail shapes pin the discriminator from both sides: the tag alone
    must not decide (case D is a real interruption carrying the same tagged
    row), and the request alone must not decide (case C got nothing back). Only
    the pair -- a ``/compact`` user row whose compaction result row is present
    -- reads as a completed turn.
    """

    def test_compact_answered_by_notice_is_finished(self):
        # Case A: the slash command IS the request and the notice IS its result.
        assert (
            _is_interrupted(
                slot(user(), assistant(), user("/compact"), compaction_notice())
            )
            is False
        )

    def test_untagged_lookalike_text_reads_as_an_ordinary_reply(self):
        # Case B: the same text without the tag is a plain assistant reply, so
        # it already reads as the floor. Isolates the tag as A's trigger.
        assert (
            _is_interrupted(
                slot(user("/compact"), assistant("Conversation compacted: summary"))
            )
            is False
        )

    def test_compact_with_nothing_back_is_interrupted(self):
        # Case C: the request went out and no result row ever arrived.
        assert _is_interrupted(slot(user(), assistant(), user("/compact"))) is True

    def test_auto_compaction_inside_an_unanswered_turn_is_interrupted(self):
        # Case D: an automatic compaction wrote its notice inside a turn whose
        # real reply never came. Skipping the notice is deliberate and correct
        # here -- the tag alone must not flip this shape.
        assert _is_interrupted(slot(user("do the thing"), compaction_notice())) is True

    def test_ordinary_completed_turn_is_finished(self):
        # Case E: the unchanged baseline.
        assert _is_interrupted(slot(user(), assistant())) is False

    def test_compact_with_arguments_still_counts(self):
        # The runner keys ``user_requested_compaction`` on the first whitespace
        # token, so trailing text does not change what the turn asked for.
        assert (
            _is_interrupted(slot(user("/compact focus on tests"), compaction_notice()))
            is False
        )

    def test_stale_compact_does_not_mask_a_later_unanswered_turn(self):
        # The pair must belong to the NEWEST turn: a later user row that got
        # nothing back is a genuine interruption whatever happened before it.
        assert (
            _is_interrupted(slot(user("/compact"), compaction_notice(), user()))
            is True
        )

    def test_error_trailing_the_notice_is_still_interrupted(self):
        # The same evidence rule as the plain-assistant branch: a completed
        # compaction followed by an error row ended badly, and hiding the
        # Resume control on that tail would strand the user.
        assert (
            _is_interrupted(slot(user("/compact"), compaction_notice(), error()))
            is True
        )

    def test_first_token_match_uses_pythons_whitespace_rule(self):
        # The runner keys on content.split()[0], where U+0085 separates tokens
        # and U+FEFF does not. The TS mirror pins the same pair, so the two
        # sides cannot split identical content differently.
        assert (
            _is_interrupted(slot(user("/compact\x85focus"), compaction_notice()))
            is False
        )
        assert (
            _is_interrupted(
                slot(user("/compact\ufeffcontinue"), compaction_notice())
            )
            is True
        )
        assert (
            _is_interrupted(slot(user("\ufeff/compact"), compaction_notice()))
            is True
        )

    def test_borrowed_tag_notices_do_not_complete_a_compact(self):
        # The recycle and stuck-turn notices reuse kind="compaction" for the
        # follow-up scan's skip and mark themselves with meta["notice"]. A
        # stuck /compact is the opposite of a completed one: the turn stays
        # interrupted so Resume remains offered.
        for notice_kind in ("stuck_turn", "session_recycled"):
            row = {
                "role": "assistant",
                "content": "notice text",
                "meta": {"kind": "compaction", "notice": notice_kind},
            }
            assert _is_interrupted(slot(user("/compact"), row)) is True
