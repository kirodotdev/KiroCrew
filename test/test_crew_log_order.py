"""``seq`` must follow causality at every emit site, not merely be contiguous.

The crew log's whole premise is that a reader folds entries in ``seq`` order, so an
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
from crew_log_drain import assert_drained

from kiro_crew.acp.types import (
    EVENT_CLEAR_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.crew_log import crew_log_path, emit
from kiro_crew.dashboard.chat_runner import _run_chat

SESSION = "acp-order-0001"


@pytest.fixture(autouse=True)
def _log_home(tmp_path, monkeypatch):
    """Own data home, emitter on, and no state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    yield
    emit.reset_caches()


def _entries() -> list[dict]:
    """Every crew log line after the header, in file order."""
    path = crew_log_path("session", SESSION)
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
    # resolves the crew log key from there rather than from the provider -- so the
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

    ``_flush_segment`` appends the narration's ``message/sent``, so ``tool/called``
    must come after that flush. Otherwise a seq-order fold reads the narration as the
    call's result.
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
    assert_drained()

    entries = _entries()
    types = [e["type"] for e in entries]
    assert "message/sent" in types, f"the narration never reached the log: {types}"
    assert "tool/called" in types, f"the tool call never reached the log: {types}"
    first_sent = min(e["seq"] for e in entries if e["type"] == "message/sent")
    assert first_sent < _seq_of(entries, "tool/called"), (
        "the tool call landed at a lower seq than the text the model spoke before "
        f"it: {[(e['seq'], e['type']) for e in entries]}"
    )
    # The rest of the turn's order, so a fix that only moved this one pair cannot
    # pass while breaking a neighbour.
    assert _seq_of(entries, "tool/called") < _seq_of(entries, "tool/completed")
    assert _seq_of(entries, "tool/completed") < _seq_of(entries, "turn/completed")
    assert _seq_of(entries, "turn/started") < first_sent


@pytest.mark.parametrize(
    "after",
    [[], [AcpEvent(kind=EVENT_TEXT_CHUNK, text=" Found it.")]],
    ids=["turn-ends", "text-follows"],
)
@pytest.mark.asyncio
async def test_the_last_word_held_at_a_tool_call_reaches_the_log_with_its_model_call(
    tmp_path, after
):
    """The narration's last word waits at the tool call, because the text after the
    call could finish a credential it began. The row's ``message/sent`` still lands
    before the call, without that word; the word, given back once the text after
    the call shows it is a word of its own, is written as its own entry under the
    SAME turn and model call. Joined, that call's entries are what the model wrote.
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
            *after,
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ],
    )

    await _run_chat(state, slot, "look at the config")
    assert emit.flush(timeout=20.0)  # flake-ok: 20s crew-log flush, as sibling order tests

    entries = _entries()
    sent = [e for e in entries if e["type"] == "message/sent"]
    assert sent, f"nothing the model wrote reached the log: {[e['type'] for e in entries]}"
    first_step = sent[0]["data"]["step"]
    narration = [e["data"]["text"] for e in sent if e["data"]["step"] == first_step]
    assert "".join(narration) == "Let me check the config."
    assert sent[0]["seq"] < _seq_of(entries, "tool/called")
    later = [e["data"]["text"] for e in sent if e["data"]["step"] != first_step]
    assert later == ([" Found it."] if after else [])
    assert max(e["seq"] for e in sent) < _seq_of(entries, "turn/completed")


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
    assert_drained()

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

    Recovery handlers persist the partial reply through ``slot.append``, not
    ``_flush_segment``, and record it via one shared helper. The body is written with
    ``interrupted`` and BEFORE the closers, the order it happened.
    """
    import kiro_crew.acp.client as _acp

    exc_cls = getattr(_acp, exc_name)
    state, slot = _state_and_slot(
        tmp_path,
        [AcpEvent(kind=EVENT_TEXT_CHUNK, text="I got partway through this")],
        raises=exc_cls("backend failed mid-stream"),
    )

    await _run_chat(state, slot, "do the thing")
    assert_drained()

    entries = _entries()
    sent = [e for e in entries if e["type"] == "message/sent"]
    kinds = [e["type"] for e in entries]
    assert len(sent) == 1, f"the partial reply never reached the log: {kinds}"
    assert sent[0]["data"]["text"] == "I got partway through this"
    assert sent[0]["data"]["interrupted"] is True, "it is what the turn had, not a finished reply"
    closer = _seq_of(entries, "turn/completed")
    assert sent[0]["seq"] < closer, "the closer must follow the output it closes over"


@pytest.mark.asyncio
async def test_a_recovery_path_persists_a_glued_option_marker_reflowed(tmp_path):
    """The interrupted body is rendered by the same grammar as a finished one.

    ``_flush_segment`` reflows a glued ``[OPTIONS: ...]`` marker before persisting;
    a turn that dies right after gluing one goes through ``_persist_partial_reply``
    instead, and must not be the one path that stores the marker unrepaired.
    """
    from kiro_crew.acp.client import AcpProcessDied

    state, slot = _state_and_slot(
        tmp_path,
        [AcpEvent(kind=EVENT_TEXT_CHUNK, text="Pick.\n[OPTIONS: A | B]Anytime.")],
        raises=AcpProcessDied("backend failed mid-stream"),
    )

    await _run_chat(state, slot, "do the thing")

    stored = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
    from kiro_crew.constants import GLUED_FOOTER_TEXT_LABEL

    assert stored == [f"Pick.\n[OPTIONS: A | B]\n{GLUED_FOOTER_TEXT_LABEL}\nAnytime."]


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
    assert_drained()

    entries = _entries()
    if "message/sent" in [e["type"] for e in entries]:
        assert _seq_of(entries, "message/sent") < _seq_of(entries, "turn/completed")
    assert _seq_of(entries, "step/completed") < _seq_of(entries, "turn/completed")


def test_no_recovery_handler_persists_a_partial_reply_without_recording_it():
    """The claim that a new handler cannot forget the log, tested structurally.

    Each handler branch needs its own error class, retry counter and depth to reach,
    so this checks the source: only the recording helper purges chunk rows and appends
    the partial text.
    """
    from kiro_crew.dashboard import chat_runner

    src = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [
            *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
            Path(chat_runner.__file__),
        ]
        if turn_file.name != "__init__.py"
    ).splitlines()
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

    A seq-order fold must see the cause first. This pins the two a turn always
    produces; `context/composed` writes nothing when no blocks were injected.
    """
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    await _run_chat(state, slot, "do the thing")
    assert_drained()

    entries = _entries()
    assert _seq_of(entries, "message/received") < _seq_of(
        entries, "request/configured"
    ), "the request was configured before the log admitted the message arrived"


@pytest.mark.asyncio
async def test_message_received_records_entry_text_before_prompt_expansion(tmp_path):
    """The crew log records the accepted text, while the model sees its expansion."""
    from kiro_crew.dashboard import chat_runner

    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    async def _expand(_message, _state, _slot, *, project=None):
        return "injected prompt body", "ok"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_runner, "_expand_prompt_mention_off_loop", _expand)
        await _run_chat(state, slot, "@daily explain this")
    assert_drained()

    received = [e for e in _entries() if e["type"] == "message/received"]
    assert len(received) == 1
    assert (
        received[0]["data"]["text"] == "@daily explain this"
    ), "message/received recorded expanded prompt content instead of entry-time user text"


def test_the_model_recorded_is_the_one_the_session_runs_on():
    """`slot.model` is the configured pin and can name a model never used.

    A withheld pin stays on the slot (the composer chip shows it) while the session
    runs on the backend default. The log records `served_model`, empty when the backend
    serves its own default.
    """
    from kiro_crew.dashboard.chat_runner import _crew_log_model

    class _Withheld:
        model = "some-pinned-model"
        served_model = ""  # what a withheld pin leaves: the backend default

    class _Pinned:
        model = "some-pinned-model"
        served_model = "some-pinned-model"

    class _Double:  # a test double that cannot report the fact at all
        model = "some-pinned-model"

    assert _crew_log_model(_Withheld()) == "", "a withheld pin was recorded as served"
    assert _crew_log_model(_Pinned()) == "some-pinned-model"
    assert _crew_log_model(_Double(), "fallback") == "fallback"


@pytest.mark.asyncio
async def test_an_accepted_attachment_is_named_in_the_message_entry(tmp_path):
    """A turn's input includes what was attached to it.

    The ids come from the site that ACCEPTED them (the handler or the queue drain), not
    from the slot's last user row, which would hand a recovery turn the previous files.
    """
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    await _run_chat(
        state,
        slot,
        "summarize the attached report",
        _attachments=["/tmp/report one.pdf", "/tmp/notes"],
    )
    assert_drained()

    received = [e for e in _entries() if e["type"] == "message/received"]
    assert len(received) == 1
    assert received[0]["data"]["attachments"] == ["/tmp/report one.pdf", "/tmp/notes"]


@pytest.mark.asyncio
async def test_a_turn_with_no_attachment_says_nothing_about_attachments(tmp_path):
    """Absent rather than empty, like every other unobserved field here."""
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    await _run_chat(state, slot, "just text")
    assert_drained()

    received = [e for e in _entries() if e["type"] == "message/received"]
    assert len(received) == 1
    assert "attachments" not in received[0]["data"]


def test_a_segment_cut_by_a_steer_is_recorded_as_interrupted():
    """The one fact the cut site can prove about a steer.

    There is no steer type: whether the steer was consumed is known only from the
    backend echo, on another coroutine. The cut site does know the reply was cut, so it
    marks it interrupted; without the mark it reads as a completed reply.
    """
    import inspect

    from kiro_crew.dashboard import chat_runner

    flush_src = inspect.getsource(chat_runner._flush_segment)
    assert "interrupted: bool = False" in flush_src, "the flush cannot carry the fact"
    assert "interrupted=interrupted" in flush_src, "the flush does not pass it to the crew log"

    # The steer cut is the caller that must set it. Read from the source of the
    # runner and its owners because the callback is the cut bound to each turn.
    runner_src = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [
            *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
            Path(chat_runner.__file__),
        ]
        if turn_file.name != "__init__.py"
    )
    cut_at = runner_src.index("def _steer_segment_cut(")
    cut_body = runner_src[cut_at : cut_at + 4000]
    # A steer cut is interrupted unless its caller says the reply goes on
    # (a crewmate send that waits behind the turn).
    signature = cut_body[: cut_body.index(")") + 1]
    assert "interrupted: bool = True" in signature, "a steer cut does not default to interrupted"
    call_at = cut_body.index("_flush_segment(")
    call = cut_body[call_at : call_at + 300]
    assert (
        "interrupted=interrupted" in call
    ), "the pre-steer flush does not mark its text interrupted"


def test_an_ordinary_segment_is_not_marked_interrupted():
    """The default has to stay false, or every reply reads as cut off."""
    import inspect

    from kiro_crew.dashboard import chat_runner

    signature = inspect.signature(chat_runner._flush_segment)
    assert signature.parameters["interrupted"].default is False


@pytest.mark.asyncio
async def test_the_turn_ordinal_keeps_rising_past_the_message_cap(tmp_path, monkeypatch):
    """A long session's turns must not collapse into one.

    The ordinal is the absolute durable position, not `len(slot.messages)`, which stops
    growing at `_MAX_SLOT_MESSAGES`; a repeated ordinal is filed as a retry.

    Both turns run with the window already AT the cap (patched low so the trim runs),
    so a length-based ordinal would collide.
    """
    from kiro_crew.dashboard import state as state_mod

    monkeypatch.setattr(state_mod, "_MAX_SLOT_MESSAGES", 8)

    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])

    # Saturate the window BEFORE the first turn, so both turns see it clamped.
    for n in range(20):
        slot.append("assistant", f"filler a{n}", "msg msg-a")
    assert len(slot.messages) <= 8, "the cap was not patched low enough to trim"

    await _run_chat(state, slot, "first")
    assert_drained()
    first = [e for e in _entries() if e["type"] == "turn/started"][-1]["data"]["turn"]

    for n in range(20):
        slot.append("assistant", f"filler b{n}", "msg msg-a")
    assert len(slot.messages) <= 8

    await _run_chat(state, slot, "second")
    assert_drained()
    starts = [e for e in _entries() if e["type"] == "turn/started"]
    assert len(starts) == 2, f"expected two turn/started entries, got {len(starts)}"
    second = starts[-1]["data"]["turn"]

    assert second > first, (
        f"the second turn reused ordinal {second} after the trim (first was {first}); "
        "distinct turns are being recorded as retries of one"
    )
    assert starts[-1]["data"].get("attempt", 1) == 1, (
        "the second turn was recorded as a retry, which is what a repeated ordinal "
        "makes the emitter do"
    )


@pytest.mark.asyncio
async def test_the_turn_ordinal_continues_from_a_restored_window_base(tmp_path):
    """A resumed session must not restart its turn numbering.

    A resume keeps only the newest window, with the earlier count in
    `_disk_older_durable_count`. The ordinal is base + window; the window alone would
    restart near 1 and be filed as retries of old turns.
    """
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])
    # What a restore leaves: a window holding only the recent rows, and a base saying
    # how many durable rows are already on disk in front of them.
    slot._disk_older_durable_count = 4096

    await _run_chat(state, slot, "after the resume")
    assert_drained()

    starts = [e for e in _entries() if e["type"] == "turn/started"]
    assert starts, "no turn/started was written"
    turn = starts[-1]["data"]["turn"]
    assert turn > 4096, (
        f"the resumed session numbered its turn {turn}, at or below the {4096} durable "
        "rows already on disk; it will be read as a retry of a turn that already ended"
    )
    assert starts[-1]["data"].get("attempt", 1) == 1, "the first turn after a resume is not a retry"


@pytest.mark.asyncio
async def test_a_rerun_after_the_window_is_truncated_keeps_the_same_ordinal(tmp_path):
    """Regenerate and rewind re-run a turn, and the log has to say so.

    Both truncate the window and dispatch again, so the ordinal repeats and the emitter
    records another `attempt`. A monotonic counter would make the two answers to one
    question unrelated turns.
    """
    state, slot = _state_and_slot(tmp_path, [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")])
    await _run_chat(state, slot, "first ask")
    assert_drained()
    first = [e for e in _entries() if e["type"] == "turn/started"][-1]["data"]
    window_before = list(slot.messages)

    # What regenerate and rewind do before dispatching again: put the window back to
    # the state it had at the start of the turn being re-run.
    del slot.messages[len(window_before) - 2 :]
    await _run_chat(state, slot, "first ask")
    assert_drained()

    starts = [e for e in _entries() if e["type"] == "turn/started"]
    assert len(starts) == 2, f"expected two turn/started entries, got {len(starts)}"
    second = starts[-1]["data"]
    assert second["turn"] == first["turn"], (
        f"the re-run reported turn {second['turn']} where the original was "
        f"{first['turn']}; a regenerate would read as an unrelated new turn"
    )
    assert second.get("attempt", 1) == first.get("attempt", 1) + 1, (
        f"the re-run was not recorded as a further attempt: {second.get('attempt')} "
        f"after {first.get('attempt')}"
    )


@pytest.mark.asyncio
async def test_the_turn_ordinal_keeps_rising_across_a_native_clear(tmp_path):
    """The first turn after a native clear must not reuse an ordinal.

    A confirmed `/clear` empties `slot.messages`, so it must advance the durable base
    by the rows it evicts, as trim and restore do. Otherwise the next turn reuses an
    ordinal and is filed as a retry.

    Three turns: an ordinary one, the clear, and the first turn afterwards.
    """
    from chat_test_helpers import pin_kiro_cli_harness

    pin_kiro_cli_harness()
    events: list[AcpEvent] = [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")]
    state, slot = _state_and_slot(tmp_path, events)
    # Several prior rows, so a window rebuilt from scratch is visibly shorter.
    for n in range(6):
        slot.append("assistant", f"earlier reply {n}", "msg msg-a")

    await _run_chat(state, slot, "before the clear")
    assert_drained()
    before = [e for e in _entries() if e["type"] == "turn/started"][-1]["data"]["turn"]

    # The clear turn: the backend confirms the native clear, the runner empties
    # the window and appends only its confirmation row.
    events[:] = [AcpEvent(kind=EVENT_CLEAR_STATUS)]
    await _run_chat(state, slot, "/clear")
    assert_drained()
    cleared = [e for e in _entries() if e["type"] == "turn/started"][-1]["data"]["turn"]
    assert cleared > before, "the clear turn itself is numbered before the window empties"
    assert len(slot.messages) <= 2, "the native clear did not empty the window"

    events[:] = [AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")]
    await _run_chat(state, slot, "after the clear")
    assert_drained()

    starts = [e for e in _entries() if e["type"] == "turn/started"]
    assert len(starts) == 3, f"expected three turn/started entries, got {len(starts)}"
    after = starts[-1]["data"]
    assert after["turn"] > cleared, (
        f"the first turn after the clear reused ordinal {after['turn']} (the clear turn "
        f"was {cleared}, the one before it {before}); two unrelated turns now share one "
        "crew log identity"
    )
    assert after.get("attempt", 1) == 1, (
        "the first turn after the clear was recorded as a retry, which is what a "
        "repeated ordinal makes the emitter do"
    )


@pytest.mark.asyncio
async def test_a_process_death_closer_names_the_exception_it_caught(tmp_path):
    """The closer for a dead backend must name the class, like every other handler.

    ``turn/failed`` carries the error a reader uses to tell one failure mode from
    another, and the handler that caught the exception is the only site that holds
    its class. An empty ``error`` there does not read as "the process died", it
    reads as a failure nobody could name -- in a file that is never rewritten.
    """
    import kiro_crew.acp.client as _acp

    state, slot = _state_and_slot(
        tmp_path,
        [AcpEvent(kind=EVENT_TEXT_CHUNK, text="starting on it")],
        raises=_acp.AcpProcessDied("backend pipe closed"),
    )

    await _run_chat(state, slot, "do the thing")
    assert_drained()

    closer = [e for e in _entries() if e["type"] == "turn/completed"]
    assert len(closer) == 1, "the turn was left open, so this proves nothing"
    data = closer[0]["data"]
    assert data["stop_reason"] == "failed"
    assert data["error"] == "AcpProcessDied", (
        f"the closer reported error={data.get('error')!r} for a turn this process "
        "watched die on a closed backend pipe"
    )


@pytest.mark.asyncio
async def test_a_status_only_result_keeps_the_output_an_earlier_frame_persisted(tmp_path):
    """A terminal frame with no output states a STATUS, not an empty output.

    An output-less terminal frame yields a result event so the outcome is not swept as
    ``unknown``. It must not overwrite stored output, or the detail panel goes empty
    after a reload.
    """
    call = "call_status_only"
    state, slot = _state_and_slot(
        tmp_path,
        [
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=call, tool_name="read", title="read"),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=call,
                tool_name="read",
                tool_output="the body the tool actually returned",
                tool_status="completed",
                tool_final=True,
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=call,
                tool_name="read",
                tool_status="completed",
                tool_final=True,
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ],
    )

    await _run_chat(state, slot, "read the config")
    assert_drained()

    rows = [m for m in slot.messages if m.get("role") == "tool"]
    assert rows, "no tool row was persisted, so this proves nothing"
    outputs = [m.get("meta", {}).get("output") for m in rows]
    assert any(
        o == "the body the tool actually returned" for o in outputs
    ), f"the status-only frame erased the persisted output: {outputs}"
