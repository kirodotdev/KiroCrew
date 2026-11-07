"""A model the backend refuses for capacity or a rollout settles the turn.

The capacity/rollout refusal ("Model 'X' is unavailable on the backend right
now") stays transient: the in-turn retry ladder and the ``agent.fallback_model``
chain still run. What changes is the terminal card a spent ladder leaves: it
carries the ``model_unavailable`` kind, so the frontend offers the model picker
instead of Resume, and ``is_turn_interrupted`` reads it as the turn's settled
ending, so neither the card nor the composer offers Resume, ``session_retry``
refuses it, and the sidebar does not file the chat as an interrupted session.
When a fallback walk ran, the card is judged by the selected model's own error
(the model a Resume returns to), not by the last candidate's.

Pinned alongside: the inputs that keep today's Resume and interrupted-session
behaviour (a generic throttle, a 5xx, a connection drop, a user Stop) and an
unentitled model, which keeps its own card.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from test_dashboard_chat import TestRunChatModelFallback as _FallbackSuite

from kiro_crew.acp.client import AcpError, _raise_acp_error
from kiro_crew.dashboard.chat_utils import MODEL_UNAVAILABLE_KIND, MODEL_UNENTITLED_KIND
from kiro_crew.dashboard.state import is_model_unavailable_row, is_turn_interrupted
from kiro_crew.llm_helpers import TRANSIENT_RETRIES

# kiro-cli's named capacity/rollout refusal, on a model the account IS served.
_NAMED_FRAME = {
    "code": -32603,
    "message": "Prompt failed",
    "data": "The model 'claude-haiku-5.5' is not available right now.",
}
# kiro-cli >= 2.16 wording, which names no model.
_NAMELESS_FRAME = {
    "code": -32603,
    "message": "Prompt failed",
    "data": (
        "The model you\u2019ve selected is temporarily unavailable. Please use "
        "'/model' to select a different model and try again."
    ),
}
_THROTTLE_FRAME = {
    "code": -32603,
    "message": "Prompt failed",
    "data": {
        "message": "Too many requests, please wait before trying again.",
        "errorType": "ClientThrottleError",
        "retryErrorType": "THROTTLING",
    },
}
_FIVE_XX_FRAME = {"code": -32603, "message": "Internal error: API Error: Internal server error"}
_CONNECTION_FRAME = {"code": -32603, "message": "Prompt failed", "data": "dispatch failure"}
_ADVERTISED = ["claude-haiku-5.5", "gpt-5.6-terra"]


def _raised(frame, advertised=_ADVERTISED) -> AcpError:
    try:
        _raise_acp_error(frame, advertised)
    except AcpError as exc:
        return exc
    raise AssertionError("the frame raised nothing")


# ---- the raise-time tag -------------------------------------------------------


@pytest.mark.parametrize("frame", [_NAMED_FRAME, _NAMELESS_FRAME], ids=["named", "nameless"])
def test_capacity_refusal_is_tagged_and_stays_transient(frame):
    exc = _raised(frame)
    assert exc.model_unavailable is True
    # Still retried in-turn, and still walks the fallback chain.
    assert exc.transient is True


@pytest.mark.parametrize(
    "frame",
    [_THROTTLE_FRAME, _FIVE_XX_FRAME, _CONNECTION_FRAME],
    ids=["throttle", "5xx", "connection"],
)
def test_other_transients_are_not_tagged(frame):
    exc = _raised(frame)
    assert exc.transient is True
    assert exc.model_unavailable is False


def test_unentitled_model_keeps_its_own_tag():
    # Same wording, but the model is absent from what the account is served:
    # entitlement, terminal, and its own card.
    exc = _raised(_NAMED_FRAME, ["gpt-5.6-terra"])
    assert exc.transient is False
    assert exc.model_unavailable is False


def test_phrase_echoed_only_in_message_does_not_tag():
    # The model-unavailable patterns read the provider's `data` field alone.
    exc = _raised(
        {
            "code": -32603,
            "message": "The model 'claude-haiku-5.5' is not available",
            "data": "HTTP 503 Service Unavailable",
        }
    )
    assert exc.model_unavailable is False


# ---- the terminal card's kind ---------------------------------------------------


def test_terminal_error_meta_reads_the_tag():
    from kiro_crew.dashboard.chat_runner import _terminal_error_meta

    assert _terminal_error_meta(_raised(_NAMED_FRAME)) == {"kind": MODEL_UNAVAILABLE_KIND}
    assert _terminal_error_meta(_raised(_THROTTLE_FRAME)) is None
    # A plain error carrying no tag keeps today's plain row.
    assert _terminal_error_meta(AcpError("x")) is None


def test_terminal_error_meta_ranks_the_tag_last():
    from kiro_crew.dashboard.chat_runner import _terminal_error_meta

    exc = AcpError("x", transient=False)
    exc.rejected_model = "auto"
    exc.advertised = ["gpt-5.6-terra"]
    exc.model_unavailable = True
    assert _terminal_error_meta(exc) == {"kind": MODEL_UNENTITLED_KIND}


def test_transcript_twin_matches_chat_utils():
    from kiro_crew.dashboard.state_parts import transcript_rows

    assert transcript_rows._MODEL_UNAVAILABLE_KIND == MODEL_UNAVAILABLE_KIND


# ---- is_turn_interrupted ----------------------------------------------------------


def _user(text="hello"):
    return {"role": "user", "content": text}


def _card(kind_carrier="meta"):
    row = {"role": "error", "content": "❌ Model 'claude-haiku-5.5' is unavailable..."}
    if kind_carrier == "meta":
        row["meta"] = {"kind": MODEL_UNAVAILABLE_KIND}
    else:
        row["kind"] = MODEL_UNAVAILABLE_KIND
    return row


@pytest.mark.parametrize("carrier", ["meta", "top-level"])
def test_refused_model_card_is_not_an_interruption(carrier):
    assert is_model_unavailable_row(_card(carrier))
    assert is_turn_interrupted([_user(), _card(carrier)]) is False


def test_retry_notices_before_the_card_do_not_revive_it():
    retrying = {"role": "error", "content": "⟳ Connection unstable — retrying…"}
    assert is_turn_interrupted([_user(), retrying, retrying, _card()]) is False


def test_partial_reply_then_refused_card_is_settled():
    rows = [_user(), {"role": "assistant", "content": "partial"}, _card()]
    assert is_turn_interrupted(rows) is False


def test_a_later_unanswered_turn_is_still_an_interruption():
    # The card settles only its own turn: the next message that got no reply
    # is interrupted as before.
    assert is_turn_interrupted([_user(), _card(), _user("again")]) is True
    rows = [_user(), _card(), _user("again"), {"role": "error", "content": "❌ HTTP 503"}]
    assert is_turn_interrupted(rows) is True


def test_a_later_subagent_completion_turn_is_still_an_interruption():
    # A drained sub-agent completion opens a turn of its own; when that turn
    # dies, the older card must not hide it.
    sub = {"role": "subagent", "content": "[sub-agent completion]"}
    assert is_turn_interrupted([_user(), _card(), sub]) is True
    rows = [_user(), _card(), sub, {"role": "error", "content": "❌ HTTP 503"}]
    assert is_turn_interrupted(rows) is True


@pytest.mark.parametrize(
    "error_row",
    [
        {"role": "error", "content": "❌ Bedrock is throttling requests."},
        {"role": "error", "content": "⟳ Connection unstable — please try again."},
        {"role": "error", "content": "❌ HTTP 503", "meta": {"kind": "usage_limit"}},
        # A non-error row carrying the kind does not count.
        {"role": "assistant", "content": "", "meta": {"kind": MODEL_UNAVAILABLE_KIND}},
    ],
    ids=["throttle", "transient-give-up", "other-kind", "wrong-role"],
)
def test_other_errors_keep_resume(error_row):
    assert is_turn_interrupted([_user(), error_row]) is True


def test_a_user_stop_is_unchanged():
    stop = {"role": "system", "content": "stopped", "kind": "stop_event"}
    assert is_turn_interrupted([_user(), stop]) is False


def test_restart_reconcile_still_lands_a_row_for_a_later_nudge_turn(tmp_path, monkeypatch):
    """The card settles only its own turn: a monitor cycle after it whose marker
    outlived the process gets the restart-interruption row as before."""
    from kiro_crew.dashboard.slot_persistence.turn_marker import _reconcile_local_turn_marker

    state = _FallbackSuite._make_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hello", broadcast=False)
    slot.append("error", "❌ unavailable", "msg msg-err", broadcast=False, meta=_card()["meta"])
    slot.append("nudge", "monitor cycle 3", broadcast=False)
    slot.append(
        "tool",
        "nothing_to_do",
        broadcast=False,
        meta={"ends_turn": True, "tool_name": "nothing_to_do"},
    )
    before = len(slot.messages)
    assert _reconcile_local_turn_marker(slot, 1) is True
    assert len(slot.messages) == before + 1
    assert slot.messages[-1]["role"] == "error"


def test_restart_reconcile_lands_a_row_for_a_later_subagent_turn(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_persistence.turn_marker import (
        _latest_turn_settled_by_refused_model,
    )

    sub = {"role": "subagent", "content": "[sub-agent completion]"}
    assert _latest_turn_settled_by_refused_model([_user(), _card()]) is True
    assert _latest_turn_settled_by_refused_model([_user(), _card(), sub]) is False


def test_restart_reconcile_adds_no_resume_row_after_the_card(tmp_path, monkeypatch):
    """A local-turn marker that outlived its process after the card landed must
    not append the restart-interruption row, which would offer Resume."""
    from kiro_crew.dashboard.slot_persistence.turn_marker import _reconcile_local_turn_marker

    state = _FallbackSuite._make_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hello", broadcast=False)
    slot.append("error", "❌ unavailable", "msg msg-err", broadcast=False, meta=_card()["meta"])
    before = len(slot.messages)
    assert _reconcile_local_turn_marker(slot, 1) is True
    assert len(slot.messages) == before
    assert is_turn_interrupted(slot.messages) is False


# ---- the interactive turn end to end ------------------------------------------------


class TestRefusedModelTurn:
    _make_state = staticmethod(_FallbackSuite._make_state)
    _wire_sessions = staticmethod(_FallbackSuite._wire_sessions)
    _drain_bg = staticmethod(_FallbackSuite._drain_bg)
    _client = staticmethod(_FallbackSuite._client)

    async def _run(
        self,
        tmp_path,
        monkeypatch,
        frame,
        *,
        chain=(),
        succeed_on_fallback=False,
        fallback_frame=None,
    ):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: chain)
        calls: list[str] = []

        async def _stream(msg):
            model = str(client.served_model)
            calls.append(model)
            if succeed_on_fallback and model != "claude-test-model":
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="served")
                yield LLMEvent(kind=EVENT_COMPLETE)
                return
            failing = frame if model == "claude-test-model" else (fallback_frame or frame)
            raise _raised(failing, ["claude-test-model", "fallback-model"])

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        no_backoff = patch("asyncio.sleep", new_callable=AsyncMock)  # flake-ok: no time assertion
        with no_backoff:
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)
        return slot, calls

    @staticmethod
    def _terminal(slot):
        return [
            m for m in slot.messages if m.get("role") == "error" and m["content"].startswith("❌")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "frame",
        [
            {**_NAMED_FRAME, "data": "The model 'claude-test-model' is not available right now."},
            _NAMELESS_FRAME,
        ],
        ids=["named", "nameless"],
    )
    async def test_spent_ladder_shows_the_card_once_and_settles(self, tmp_path, monkeypatch, frame):
        slot, calls = await self._run(tmp_path, monkeypatch, frame)
        # The ladder still ran in full on the refused model.
        assert len(calls) == TRANSIENT_RETRIES + 1
        terminal = self._terminal(slot)
        assert len(terminal) == 1
        assert (terminal[0].get("meta") or {}).get("kind") == MODEL_UNAVAILABLE_KIND
        # No Resume (the Continue endpoint and the composer read this) and no
        # interrupted session in the sidebar (slot projection reads this).
        assert is_turn_interrupted(slot.messages) is False

    @pytest.mark.asyncio
    async def test_a_served_fallback_takes_the_turn(self, tmp_path, monkeypatch):
        frame = {
            **_NAMED_FRAME,
            "data": "The model 'claude-test-model' is not available right now.",
        }
        slot, calls = await self._run(
            tmp_path, monkeypatch, frame, chain=("fallback-model",), succeed_on_fallback=True
        )
        assert calls[-1] == "fallback-model"
        assert not self._terminal(slot)
        assert is_turn_interrupted(slot.messages) is False

    @pytest.mark.asyncio
    async def test_a_generic_throttle_keeps_resume(self, tmp_path, monkeypatch):
        slot, calls = await self._run(tmp_path, monkeypatch, _THROTTLE_FRAME)
        assert len(calls) == TRANSIENT_RETRIES + 1
        terminal = self._terminal(slot)
        assert len(terminal) == 1
        assert not (terminal[0].get("meta") or {}).get("kind")
        assert is_turn_interrupted(slot.messages) is True

    @pytest.mark.asyncio
    async def test_refused_primary_with_a_throttled_candidate_settles(self, tmp_path, monkeypatch):
        # Resume would return to the refused primary (the restore probe), so the
        # card is judged by the primary's refusal, not the candidate's throttle.
        frame = {
            **_NAMED_FRAME,
            "data": "The model 'claude-test-model' is not available right now.",
        }
        slot, calls = await self._run(
            tmp_path,
            monkeypatch,
            frame,
            chain=("fallback-model",),
            fallback_frame=_THROTTLE_FRAME,
        )
        assert "fallback-model" in calls
        terminal = self._terminal(slot)
        assert len(terminal) == 1
        assert (terminal[0].get("meta") or {}).get("kind") == MODEL_UNAVAILABLE_KIND
        assert is_turn_interrupted(slot.messages) is False

    @pytest.mark.asyncio
    async def test_throttled_primary_with_a_refused_candidate_keeps_resume(
        self, tmp_path, monkeypatch
    ):
        slot, calls = await self._run(
            tmp_path,
            monkeypatch,
            _THROTTLE_FRAME,
            chain=("fallback-model",),
            fallback_frame=_NAMELESS_FRAME,
        )
        assert "fallback-model" in calls
        terminal = self._terminal(slot)
        assert len(terminal) == 1
        assert not (terminal[0].get("meta") or {}).get("kind")
        assert is_turn_interrupted(slot.messages) is True
