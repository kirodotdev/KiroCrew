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
