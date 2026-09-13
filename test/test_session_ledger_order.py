"""``seq`` must follow causality at every emit site, not merely be contiguous.

The ledger's whole premise is that a reader folds entries in ``seq`` order, so an
entry appearing before the facts that happened first is not a cosmetic problem: it
is a wrong answer the fold cannot detect, in a file that is never rewritten. These
tests drive the real ``_run_chat`` over a scripted ACP stream and assert on the
FILE, so what is pinned is the order entries actually reach disk rather than the
order the call sites appear in the source.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import session_ledger_emit as emit
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.ledger import ledger_path

SESSION = "acp-order-0001"


@pytest.fixture(autouse=True)
def _ledger_home(tmp_path, monkeypatch):
    """Own data home, emitter on, and no state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    emit.reset_caches()
    yield
    emit.reset_caches()


def _entries() -> list[dict]:
    """Every ledger line after the header, in file order."""
    path = ledger_path("session", SESSION)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()][1:]


def _seq_of(entries: list[dict], entry_type: str) -> int:
    """The seq of the one entry of *entry_type*, asserting it is unique."""
    hits = [e["seq"] for e in entries if e["type"] == entry_type]
    assert len(hits) == 1, f"expected exactly one {entry_type}, got {len(hits)}"
    return hits[0]


def _state_and_slot(tmp_path: Path, events, *, raises: BaseException | None = None):
    """A slot whose backend streams *events*, then optionally raises.

    The session id is set explicitly: ``session_id_of`` requires a real ``str``,
    and a bare mock attribute reads as absent, which would make every emit in this
    module a silent no-op and the assertions vacuous.
    """
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = SESSION
    # The runner publishes the INNER client on the slot, and `_flush_segment`
    # resolves the ledger key from there rather than from the provider -- so the
    # inner one has to carry the id too, or every `message/sent` is a silent no-op
    # and the ordering assertions below are vacuous.
    client.client._session_id = SESSION
    client.shutdown = AsyncMock()

    async def _stream(*_a, **_kw):
        for event in events:
            yield event
        if raises is not None:
            raise raises

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("order-slot")
    slot.append("user", "hello", "msg msg-u")
    return state, slot


@pytest.mark.asyncio
async def test_text_the_model_spoke_before_a_tool_call_lands_before_it(tmp_path):
    """The common turn shape: the model narrates, then calls a tool.

    The narration is flushed by ``_flush_segment``, which is what appends this
    turn's ``message/sent`` -- so emitting ``tool/called`` before that flush put the
    call at a LOWER seq than the text that preceded it. A fold reading in seq order
    then sees the narration after the call and reads it as the call's result.
    """
    state, slot = _state_and_slot(
        tmp_path,
        [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Let me check the config."),
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc-1",
                title="fs_read",
                tool_name="fs_read",
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_output="ok", tool_final=True
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ],
    )

    await _run_chat(state, slot, "look at the config")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    types = [e["type"] for e in entries]
    assert "message/sent" in types, f"the narration never reached the log: {types}"
    assert "tool/called" in types, f"the tool call never reached the log: {types}"
    assert _seq_of(entries, "message/sent") < _seq_of(entries, "tool/called"), (
        "the tool call landed at a lower seq than the text the model spoke before "
        f"it: {[(e['seq'], e['type']) for e in entries]}"
    )
    # The rest of the turn's order, so a fix that only moved this one pair cannot
    # pass while breaking a neighbour.
    assert _seq_of(entries, "tool/called") < _seq_of(entries, "tool/completed")
    assert _seq_of(entries, "tool/completed") < _seq_of(entries, "turn/completed")
    assert _seq_of(entries, "turn/started") < _seq_of(entries, "message/sent")


@pytest.mark.asyncio
async def test_a_stream_that_dies_mid_turn_still_closes_the_turn(tmp_path):
    """A turn the process WATCHED end must not be left open.

    An open ``turn/started`` says the writer died mid-turn, which this process
    being alive contradicts -- and nothing here would correct it, because the
    interrupted-turn repair is opt-in and only a resume asks for it. So a later
    resume would close it as an interruption that never happened.
    """
    state, slot = _state_and_slot(
        tmp_path,
        [AcpEvent(kind=EVENT_TEXT_CHUNK, text="starting on it")],
        raises=RuntimeError("stream died"),
    )

    await _run_chat(state, slot, "do the thing")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    types = [e["type"] for e in entries]
    assert "turn/started" in types, "the turn never started -- the test proves nothing"
    closer = [e for e in entries if e["type"] == "turn/completed"]
    assert len(closer) == 1, f"the turn was left open in the file: {types}"
    data = closer[0]["data"]
    assert data["stop_reason"] == "failed"
    assert data["error"] == "RuntimeError", "the closer must name the exception class"
    # Absent, not zeroed: no usage event arrived, so nothing was measured, and a
    # turn that streamed real text must not carry a line claiming it cost nothing.
    assert "tokens" not in data
    assert "credits" not in data
    # Position still carries meaning: the closer follows every entry of its turn.
    assert closer[0]["seq"] == max(e["seq"] for e in entries)


@pytest.mark.parametrize("exc_name", ["AcpProcessDied", "AcpError"])
@pytest.mark.asyncio
async def test_a_recovery_path_records_the_partial_reply_it_persists(tmp_path, exc_name):
    """A turn that died still has to say what it had produced.

    Seven recovery handlers persist the partial reply straight through
    ``slot.append`` rather than ``_flush_segment``, because they purge the chunk
    rows and must not broadcast a segment. They reach the log through one shared
    helper, so the transcript cannot hold text the user watched stream while the
    ledger carries a turn closer and nothing else -- a closer asserting an end for
    output the record never mentions. The body is written with ``interrupted`` and
    BEFORE the closers, which is the order it happened.
    """
    import kiro_crew.acp.client as _acp

    exc_cls = getattr(_acp, exc_name)
    state, slot = _state_and_slot(
        tmp_path,
        [AcpEvent(kind=EVENT_TEXT_CHUNK, text="I got partway through this")],
        raises=exc_cls("backend failed mid-stream"),
    )

    await _run_chat(state, slot, "do the thing")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    sent = [e for e in entries if e["type"] == "message/sent"]
    kinds = [e["type"] for e in entries]
    assert len(sent) == 1, f"the partial reply never reached the log: {kinds}"
    assert sent[0]["data"]["text"] == "I got partway through this"
    assert sent[0]["data"]["interrupted"] is True, "it is what the turn had, not a finished reply"
    closer = _seq_of(entries, "turn/completed")
    assert sent[0]["seq"] < closer, "the closer must follow the output it closes over"


@pytest.mark.asyncio
async def test_a_failed_turn_closer_follows_the_text_it_closes_over(tmp_path):
    """The closer is the turn's boundary, so partial output belongs above it.

    A stream that dies after speaking still flushed that text, and a closer written
    before the flush would put the turn's own end ahead of output belonging to it.
    """
    state, slot = _state_and_slot(
        tmp_path,
        [AcpEvent(kind=EVENT_TEXT_CHUNK, text="here is what I found so far")],
        raises=RuntimeError("stream died"),
    )

    await _run_chat(state, slot, "explain it")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    if "message/sent" in [e["type"] for e in entries]:
        assert _seq_of(entries, "message/sent") < _seq_of(entries, "turn/completed")
    assert _seq_of(entries, "step/completed") < _seq_of(entries, "turn/completed")


def test_no_recovery_handler_persists_a_partial_reply_without_recording_it():
    """The claim that a new handler cannot forget the log, tested structurally.

    Seven handlers persist a partial reply, in two spellings, and a per-handler test
    cannot pin them -- each branch needs its own error class, retry counter and depth
    to reach. So the invariant is asserted over the source: the only place that purges
    chunk rows and appends the assistant's partial text is the one helper that also
    records it.

    Mutation guard: re-inlining any of those blocks reddens this.
    """
    import inspect

    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner).splitlines()
    offenders = []
    for i, line in enumerate(src):
        if "slot.purge_chunks()" not in line:
            continue
        window = "\n".join(src[i : i + 4])
        if 'slot.append("assistant"' not in window:
            continue
        # Walk back to the enclosing def.
        owner = next(
            (src[j].strip() for j in range(i, -1, -1) if src[j].lstrip().startswith("def ")),
            "<module>",
        )
        if "_persist_partial_reply" not in owner:
            offenders.append(f"line {i + 1} in {owner}")
    assert not offenders, (
        "a recovery path persists partial assistant text outside the helper that "
        f"records it: {offenders}"
    )


@pytest.mark.asyncio
async def test_the_input_is_recorded_before_what_was_derived_from_it(tmp_path):
    """`request/configured` and `context/composed` are derived FROM the message.

    A reader folds on seq. With them first, the fold sees a derived fact before its
    cause -- a request configured, and context assembled, for a message the log has
    not yet admitted arrived. All three are written at one site in the order input,
    configuration, composition; this pins the two a turn always produces, since
    `context/composed` writes nothing when no blocks were injected and is the literal
    next call after `request/configured`.
    """
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    await _run_chat(state, slot, "do the thing")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    assert _seq_of(entries, "message/received") < _seq_of(
        entries, "request/configured"
    ), "the request was configured before the log admitted the message arrived"


def test_the_model_recorded_is_the_one_the_session_runs_on():
    """`slot.model` is the configured pin and can name a model never used.

    A withheld pin is KEPT on the slot on purpose -- the composer chip still shows
    it, and clearing it would delete the user's setting from one session's advertised
    list -- while the session runs on the backend default. Writing that pin into an
    append-only entry states a model the session did not run, in a file nothing
    rewrites. `served_model` is the session fact, empty when the backend serves its
    own default, and the ledger records that emptiness rather than naming a model.

    Mutation guard: reading `slot.model` at either site reddens this.
    """
    from kiro_crew.dashboard.chat_runner import _ledger_model

    class _Withheld:
        model = "some-pinned-model"
        served_model = ""  # what a withheld pin leaves: the backend default

    class _Pinned:
        model = "some-pinned-model"
        served_model = "some-pinned-model"

    class _Double:  # a test double that cannot report the fact at all
        model = "some-pinned-model"

    assert _ledger_model(_Withheld()) == "", "a withheld pin was recorded as served"
    assert _ledger_model(_Pinned()) == "some-pinned-model"
    assert _ledger_model(_Double(), "fallback") == "fallback"


@pytest.mark.asyncio
async def test_an_accepted_attachment_is_named_in_the_message_entry(tmp_path):
    """A turn's input includes what was attached to it.

    `on_message_received` has always accepted the ids; the call site did not pass
    them, so a message whose whole point was a file left the ledger describing text
    alone and a reader could not tell why the turn did what it did. The ids come from
    the site that ACCEPTED them -- the handler, or the queue drain for a row that
    waited -- rather than being read back off the slot's last user row, which would
    hand a synthetic or recovery turn the previous turn's files.

    Mutation guard: dropping the argument at the emit site reddens this.
    """
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    await _run_chat(
        state,
        slot,
        "summarize the attached report",
        _attachments=["/tmp/report one.pdf", "/tmp/notes"],
    )
    assert emit.flush(timeout=20.0)

    received = [e for e in _entries() if e["type"] == "message/received"]
    assert len(received) == 1
    assert received[0]["data"]["attachments"] == ["/tmp/report one.pdf", "/tmp/notes"]


@pytest.mark.asyncio
async def test_a_turn_with_no_attachment_says_nothing_about_attachments(tmp_path):
    """Absent rather than empty, like every other unobserved field here."""
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    await _run_chat(state, slot, "just text")
    assert emit.flush(timeout=20.0)

    received = [e for e in _entries() if e["type"] == "message/received"]
    assert len(received) == 1
    assert "attachments" not in received[0]["data"]
