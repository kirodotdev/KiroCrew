"""The one display cut of a KAS engine sub-agent's text, and how much it reads.

``engine_subagent_cards.shown_cut`` makes every display cut of a model-written string
an engine card, its approval card or its record shows. Two things are pinned here:

* what it returns: with credential redaction in force, the text redacted and then
  cut; where the owner's setting has the pass stand down, the text as written unless
  the cut would split a credential, and then the masked text;
* how much it reads: at most the bound plus a fixed reach, however long the string
  is, and once per card. The session handle keeps each string as the engine sent it
  and scans nothing, so a dispatch frame repeated on the wire costs no scan, and a
  session whose cards never show these strings costs none at all.

"Scanned" is what reaches the credential scanner itself
(``security.redaction._credential_redaction_plan``), which every redaction pass
calls with the text it reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew import redaction_seams
from kiro_crew.acp import session_handle_native_children as children
from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, EVENT_SUBAGENT_LIST, AcpEvent
from kiro_crew.dashboard import engine_subagent_cards as cards
from kiro_crew.security import redact, redaction
from kiro_crew.testing.synthetic_credentials import github_classic

SUBTASK = "31c14746-6447-4300-8ee9-074e74540ae3"
CALL = "invoke_subagent_toolu_bdrk_019Hyz9XEeDzAmxkRpDxDj4q"
#: How far past a bound the cut reads.
_REACH = 4096
#: Far past every bound and its reach.
_HUGE = 1_000_000
#: A word that tells one field's text from another's in what the scanner was handed.
_TASK, _AGENT, _WHY, _ASKER, _TITLE = "TASKWORD", "AGENTWORD", "WHYWORD", "ASKERWORD", "TITLEWORD"
_SHOWN = {
    _TASK: children.NATIVE_CHILD_TASK_CAP,
    _AGENT: children.NATIVE_CHILD_LABEL_CAP,
    _WHY: children.NATIVE_CHILD_LABEL_CAP,
    _ASKER: children.REQUESTER_NAME_CAP,
    _TITLE: cards.ENGINE_TOOL_TITLE_MAX,
}


@pytest.fixture
def scanned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text handed to the credential scanner, in order."""
    seen: list[str] = []
    real = redaction._credential_redaction_plan

    def plan(text: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(redaction, "_credential_redaction_plan", plan)
    return seen


def _long(word: str, size: int = _HUGE) -> str:
    return f"{word} " + "t" * size


def _dispatch(status: str, task: str, agent: str, **more: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": CALL,
        "title": "Sub-agent: kirocrew-helper",
        "status": status,
        "rawInput": {"name": agent, "prompt": task},
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": SUBTASK}},
    }
    frame.update(more)
    return frame


def _asking(name: str) -> dict[str, Any]:
    return {"_meta": {"kiro": {"requester": {"subagent": {"agentName": name, "depth": 1}}}}}


def _handle() -> Any:
    return SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )


def _parse(handle: Any, frame: dict[str, Any]) -> list[dict[str, Any]]:
    [event] = children._handle_kas_subagent(handle, frame) or []
    assert event.kind == EVENT_SUBAGENT_LIST
    return event.subagents


def _slot() -> Any:
    slot = MagicMock()
    slot.key = "slot-1"
    slot._native_subagent_tracker = {}
    slot._native_subagent_output = {}
    return slot


def _turn(slot: Any, *, engine: bool) -> Any:
    state = MagicMock()
    state._native_cards = {}
    return SimpleNamespace(
        state=state,
        slot=slot,
        _engine_children=engine,
        _engine_dispatch_tasks={},
        _native_tracker=slot._native_subagent_tracker,
        _native_tc_card={},
        _native_child_tc=set(),
        _native_card_output=slot._native_subagent_output,
        _native_card_output_len={},
        _turn_native_activity=False,
    )


async def _roster(turn: Any, handle: Any, frame: dict[str, Any]) -> None:
    """One dispatch frame: parsed by the handle, then drawn by the turn's own handler."""
    rows = _parse(handle, frame)
    await cr._on_subagent_list(turn, AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=rows))


def _holding(scanned: list[str], *words: str) -> list[str]:
    return [text for text in scanned if any(word in text for word in words)]


# ── how much is read ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_however_long_a_shown_string_is_the_scan_reads_its_bound_and_a_fixed_reach(
    scanned: list[str],
) -> None:
    handle, slot = _handle(), _slot()
    turn = _turn(slot, engine=True)

    await _roster(turn, handle, _dispatch("in_progress", _long(_TASK), _long(_AGENT)))
    held = children.kas_requesting_subagent(_asking(_long(_ASKER)))
    cards.approval_card_fields(turn, SimpleNamespace(tool_call_id=CALL, requesting_subagent=held))
    await cr._on_subagent_activity(
        turn,
        AcpEvent(
            kind=EVENT_SUBAGENT_ACTIVITY,
            sub_session_id=SUBTASK,
            tool_call_id="c1",
            title=_long(_TITLE),
        ),
    )
    failed = _dispatch("failed", _long(_TASK), _long(_AGENT), rawOutput=_long(_WHY))
    await _roster(turn, handle, failed)

    for word, shown in _SHOWN.items():
        lengths = [len(text) for text in _holding(scanned, word)]
        assert lengths, f"{word} was never scanned: the card did not show it"
        assert max(lengths) <= shown + _REACH, word
    # And nothing the handle keeps is longer than what the cut reads.
    [row] = handle._kas_subagent_roster.values()
    assert len(row["prompt"]) == _SHOWN[_TASK] + _REACH
    assert max(len(row["target"]), len(row["failure"])) == _SHOWN[_AGENT] + _REACH
    assert len(held) == _SHOWN[_ASKER] + _REACH


def test_the_reach_is_the_stream_redactors_longest_hold_back() -> None:
    from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX

    assert cards.SHOWN_LOOKAHEAD == children.NATIVE_CHILD_SHOWN_LOOKAHEAD == _REACH
    assert cards.SHOWN_LOOKAHEAD >= _STREAM_HOLDBACK_JWT_MAX


@pytest.mark.asyncio
async def test_a_dispatch_frame_repeated_on_the_wire_is_not_scanned_again(
    scanned: list[str],
) -> None:
    """The engine sends a dispatch's ``rawInput`` on each of its frames: the card
    reads the task and the agent when it is drawn, and never again."""
    handle, slot = _handle(), _slot()
    turn = _turn(slot, engine=True)
    frame = _dispatch("in_progress", f"{_TASK} run ls and report", f"{_AGENT}-helper")

    await _roster(turn, handle, frame)
    drawn = len(_holding(scanned, _TASK, _AGENT))
    assert drawn, "the card's own cut scans each once"

    for _ in range(5):
        await _roster(turn, handle, frame)
    assert len(_holding(scanned, _TASK, _AGENT)) == drawn


@pytest.mark.asyncio
async def test_a_session_whose_cards_show_none_of_these_strings_scans_none_of_them(
    scanned: list[str],
) -> None:
    """A chat that mounted nothing (the preview off or never set, a custom agent's own
    sub-agents): its native cards show the row's title, and the handle keeps the
    dispatch's strings unread. Only an approval card scans the task and the asker,
    when it shows them."""
    handle, slot = _handle(), _slot()
    turn = _turn(slot, engine=False)
    task, agent = f"{_TASK} run ls and report", f"{_AGENT}-helper"

    await _roster(turn, handle, _dispatch("in_progress", task, agent))
    await _roster(turn, handle, _dispatch("failed", task, agent, rawOutput=f"{_WHY} refused"))
    held = children.kas_requesting_subagent(_asking(f"{_ASKER}-helper"))

    assert scanned, "the card's own title is still scanned, as it was"
    assert _holding(scanned, _TASK, _AGENT, _WHY, _ASKER) == []

    event = SimpleNamespace(tool_call_id=CALL, requesting_subagent=held)
    assert set(cards.approval_card_fields(turn, event)) == {"subagent_task", "asked_by_subagent"}
    assert _holding(scanned, _AGENT, _WHY) == []


# ── what the cut returns ──────────────────────────────────────────────────────

_PW = "Pw" + "7" * 40
_URI = f"postgres://admin:{_PW}@db.internal/app"


@pytest.fixture
def standing_down(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The owner's setting off, inside the owner's own chat turn: the pass stands down."""
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    with redaction_seams.output_seam(redaction_seams.CHAT):
        yield


def test_in_force_the_text_is_redacted_and_then_cut() -> None:
    text = f"use {_URI} " + "x" * 500
    assert cards.shown_cut(text, 100) == redact(text)[:100]
    assert _PW not in cards.shown_cut(text, 100)
    # A cut that would split it changes nothing: the pass ran first.
    split = len("use postgres://admin:") + len(_PW)
    assert _PW not in cards.shown_cut(text, split)


def test_standing_down_a_text_no_cut_splits_stays_as_written(standing_down: Any) -> None:
    text = f"use {_URI} " + "x" * 500
    assert cards.shown_cut(text, 100) == text[:100]
    assert cards.shown_cut("short", 100) == "short"
    # Nothing is cut, so nothing can be split: a whole text is left to its readers' pass.
    assert cards.shown_cut(f"use {_URI}", 100) == f"use {_URI}"


@pytest.mark.parametrize(
    ("text", "bound", "secret"),
    [
        # The cut removes the ``@host`` that makes the password read as one.
        (f"use {_URI} now", len("use postgres://admin:") + len(_PW), _PW[:16]),
        # The cut falls inside the password.
        (f"use {_URI} now", len("use postgres://admin:") + 20, _PW[:16]),
        # The cut falls inside a token.
        (f"push {github_classic(seed=3)} now", len("push ") + 20, github_classic(seed=3)[:16]),
    ],
    ids=["host-cut-off", "inside-the-password", "inside-a-token"],
)
def test_standing_down_a_text_the_cut_would_split_is_stored_masked(
    standing_down: Any, text: str, bound: int, secret: str
) -> None:
    assert secret in text[:bound], "the plain cut would keep part of it"
    shown = cards.shown_cut(text, bound)
    assert secret not in shown
    assert len(shown) <= bound
    # What any other reader's pass makes of it is what is stored: nothing left to find.
    with redaction_seams.output_seam(redaction_seams.CHANNELS):
        assert redact(shown) == shown


def test_a_text_already_shown_is_cut_the_same_way_whatever_the_setting_reads() -> None:
    """The record cuts a card's task shorter after the turn: a task shown as written
    is checked, and one redacted when it was shown passes unchanged."""
    written = f"use {_URI} now and then report"
    split = len("use postgres://admin:") + len(_PW)
    assert _PW[:16] not in cards.cut_unsplit(written, split)
    assert cards.cut_unsplit(written, len(written) - 3) == written[:-3]
    masked = redact(written)
    assert cards.cut_unsplit(masked, 30) == masked[:30]
    assert cards.cut_unsplit(masked, 500) == masked


# ── why a child failed: the request id the engine put last ────────────────────

_REQUEST_ID = "(request_id: 0a1b2c3d-0000-4000-8000-000000000001)"
_LONG_REASON = "The engine refused the call: " + "x" * 300 + " " + _REQUEST_ID


def test_a_long_reason_keeps_the_request_id_the_engine_put_last() -> None:
    shown = cards.shown_reason(_LONG_REASON)
    assert len(shown) == cards.ENGINE_REASON_MAX
    assert shown.endswith(f"... {_REQUEST_ID}")
    assert shown.startswith("The engine refused the call: xxx")
    # What the card's own clip makes of the same text: one rule, one pattern.
    assert shown == cr._clip_card_error(redact(_LONG_REASON))


@pytest.mark.parametrize(
    "reason",
    [
        "Sub-agent execution was cancelled",
        f"refused {_REQUEST_ID}",
        "no id here: " + "y" * 400,
        f"an id that is not last {_REQUEST_ID} " + "z" * 400,
        f"push {github_classic(seed=5)} failed: " + "w" * 300 + " " + _REQUEST_ID,
    ],
    ids=["short", "short-with-id", "long-no-id", "id-not-last", "credential-and-id"],
)
def test_in_force_a_reason_is_what_the_cards_own_clip_makes_of_the_redacted_text(
    reason: str,
) -> None:
    shown = cards.shown_reason(reason)
    assert shown == cr._clip_card_error(redact(reason))
    assert len(shown) <= cards.ENGINE_REASON_MAX
    assert github_classic(seed=5) not in shown


@pytest.mark.asyncio
async def test_a_failed_childs_card_and_record_keep_the_request_id() -> None:
    from kiro_crew.dashboard import engine_subagent_record as record

    handle, slot = _handle(), _slot()
    turn = _turn(slot, engine=True)
    await _roster(turn, handle, _dispatch("in_progress", "run ls", "kirocrew-helper"))
    failed = _dispatch("failed", "run ls", "kirocrew-helper", rawOutput=_LONG_REASON)
    await _roster(turn, handle, failed)

    sent = turn.state.broadcast_ws.call_args_list
    [done] = [call.args[1] for call in sent if call.args[0] == "subagent_done"]
    assert done["error"].endswith(f"... {_REQUEST_ID}")
    assert len(done["error"]) == cards.ENGINE_REASON_MAX
    [entry] = record.engine_record(slot._native_subagent_tracker)["children"]
    assert entry["reason"] == done["error"]


def test_standing_down_a_long_reason_keeps_the_request_id_as_written(standing_down: Any) -> None:
    shown = cards.shown_reason(_LONG_REASON)
    assert shown == cr._clip_card_error(_LONG_REASON)
    assert shown.endswith(f"... {_REQUEST_ID}")
    # A credential the clip does not touch stays as the owner wrote it.
    whole = f"use {_URI}: " + "x" * 300 + " " + _REQUEST_ID
    assert cards.shown_reason(whole) == cr._clip_card_error(whole)
    assert _PW in cards.shown_reason(whole)


def test_standing_down_a_credential_just_before_the_request_id_is_not_split(
    standing_down: Any,
) -> None:
    """The clip cuts the text before the id to make room for it. A credential that
    cut would split is stored masked, and the id is still there."""
    password = "Pw" + "7" * 150
    reason = f"refused: postgres://admin:{password}@db.internal/app {_REQUEST_ID}"
    assert len(reason) > cards.ENGINE_REASON_MAX
    assert password[:32] in cr._clip_card_error(reason), "the plain clip would keep part of it"

    shown = cards.shown_reason(reason)

    assert password[:32] not in shown
    assert shown.endswith(_REQUEST_ID)
    assert "@db.internal/app" in shown
    assert len(shown) <= cards.ENGINE_REASON_MAX
    # What any other reader's pass makes of it is what is stored: nothing left to find.
    with redaction_seams.output_seam(redaction_seams.CHANNELS):
        assert redact(shown) == shown
