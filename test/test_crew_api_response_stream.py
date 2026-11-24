"""``POST /v1/responses`` with ``stream: true``: the events, and that they end as the JSON does.

The route runs around the same stand-in for ``_run_chat`` as ``test_crew_api_responses.py``;
the Stop tests press the real ``stop_slot_turn``.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import re
from typing import Any

import pytest
from test_crew_api_responses import (
    _STATUS_TABLE,
    _STOPS,
    AS_OWNER,
    ASK,
    PATH,
    SECRET,
    TRANSIENT_RETRY_KIND,
    _ask,
    _contract_error,
    _no_slot_left,
    _press_stop,
    _stop_reasons,
    _Turn,
    _valid,
)
from test_crew_api_responses import client as _responses_client

from kiro_crew.dashboard import crew_api_response_stream as stream
from kiro_crew.dashboard import crew_api_responses as responses
from kiro_crew.dashboard.openai_compat import _redact
from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
from kiro_crew.security import StreamRedactor, redaction
from kiro_crew.security.redaction_structured.encoded import TEXT_HOLD_MAX
from kiro_crew.security.text_redaction import _STREAM_HOLDBACK_JWT_MAX
from kiro_crew.testing.synthetic_credentials import synth
from kiro_crew.testing.wait import await_until

client = _responses_client  # the route fixture of the JSON tests
STREAMED = {**ASK, "stream": True}
ITEM = "msg_0123456789abcdef0123456789abcdef"
OPENING = ["response.created", "response.in_progress"]
ITEM_OPENS = ["response.output_item.added", "response.content_part.added"]
ITEM_CLOSES = [
    "response.output_text.done",
    "response.content_part.done",
    "response.output_item.done",
]


def _response(status: str, text: str = "") -> dict[str, Any]:
    output = []
    if text:
        part = {"type": "output_text", "text": text, "annotations": []}
        output.append(
            {
                "type": "message",
                "id": ITEM,
                "role": "assistant",
                "status": "completed" if status == "completed" else "incomplete",
                "content": [part],
            }
        )
    return {"id": "resp_1", "object": "response", "status": status, "output": output}


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def _deltas(events: list[dict[str, Any]]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")


def _events(raw: str) -> list[dict[str, Any]]:
    """The events of one SSE body; each ``event:`` line must name its ``data:`` object's type."""
    found = []
    for frame in raw.split("\n\n"):
        lines = [line for line in frame.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        name, data = lines
        event = json.loads(data.removeprefix("data: "))
        assert name == f"event: {event['type']}"
        found.append(event)
    return found


async def _ask_stream(client, body: Any = None):
    response = await client.post(PATH, json=STREAMED if body is None else body, headers=AS_OWNER)
    raw = await response.text()
    if response.content_type != "text/event-stream":
        return response, json.loads(raw)
    return response, _events(raw)


# -- the translator: rows in, events out, no I/O --------------------------------------


@pytest.mark.parametrize(
    ("status", "event"),
    [
        ("completed", "response.completed"),
        ("failed", "response.failed"),
        ("incomplete", "response.incomplete"),
        ("cancelled", "response.incomplete"),
    ],
)
def test_the_terminal_event_follows_the_status(status, event) -> None:
    assert stream.terminal_event(status) == event


def test_a_finished_turn_is_the_planned_events_in_order() -> None:
    events = stream.Events(ITEM)
    out = events.start(_response("in_progress"))
    out += events.text("Do") + events.text("ne.")
    out += events.end(_response("completed", "Done."))
    assert _types(out) == [
        *OPENING,
        *ITEM_OPENS,
        "response.output_text.delta",
        *ITEM_CLOSES,
        "response.completed",
    ]
    assert [event["sequence_number"] for event in out] == list(range(len(out)))
    assert _deltas(out) == "Done."
    added, part_added = out[2], out[3]
    assert (added["output_index"], added["item"]["id"], added["item"]["status"]) == (
        0,
        ITEM,
        "in_progress",
    )
    assert added["item"]["content"] == []
    assert part_added["part"] == {"type": "output_text", "text": "", "annotations": []}
    text_done, part_done, item_done, last = out[-4:]
    assert text_done["text"] == "Done."
    assert part_done["part"]["text"] == "Done."
    assert item_done["item"] == last["response"]["output"][0]
    for event in out[2:-1]:
        assert event.get("item_id", ITEM) == ITEM
        assert event["output_index"] == 0


def test_the_end_of_the_text_is_held_back_until_more_text_or_the_end() -> None:
    events = stream.Events(ITEM)
    text = "plain words still arriving"
    first = events.text(text)
    assert _deltas(first) == "plain words still "
    assert events.text("") == []
    last = events.end(_response("completed", text))
    assert _deltas(first + last) == text
    _assert_stream_text(first + last)
    assert _types(first + last) == [
        *ITEM_OPENS,
        "response.output_text.delta",
        "response.output_text.delta",
        *ITEM_CLOSES,
        "response.completed",
    ]


def test_a_credential_split_over_two_rows_never_leaves_in_a_delta() -> None:
    events = stream.Events(ITEM)
    full = f"key {SECRET} " + "ordinary words " * 40
    cut = 4 + len(SECRET) // 2
    out = events.text(full[:cut]) + events.text(full[cut:])
    out += events.end(_response("completed", _redact(full)))
    assert SECRET not in json.dumps(out)
    assert _deltas(out) == _redact(full)


def _redaction_cases() -> list[Any]:
    wrapped = base64.b64encode((SECRET + " trailing text" * 100).encode()).decode()
    return [
        pytest.param("before " + wrapped + " after", id="long-base64"),
        pytest.param("before " + SECRET + " after", id="credential"),
        pytest.param("plain words without a credential", id="plain"),
        pytest.param("before https://example.test/?data=" + SECRET + " after", id="exfil-url"),
    ]


def _assert_no_recoverable_secret(deltas: list[str], split: int) -> None:
    for delta in deltas:
        assert SECRET not in delta, ("raw credential", split)
        assert SECRET[:12] not in delta, ("raw credential prefix", split)
        for run in re.findall(r"[A-Za-z0-9+/]{4,}", delta):
            # The longest complete prefix decodes every shorter complete prefix too.
            decoded = base64.b64decode(run[: len(run) // 4 * 4], validate=True)
            assert SECRET[:12].encode() not in decoded, ("encoded credential prefix", split)


def _assert_stream_text(out: list[dict[str, Any]]) -> str:
    text = _deltas(out)
    output = out[-1]["response"]["output"]
    assert text == (output[0]["content"][0]["text"] if output else "")
    for event in out:
        if event["type"] == "response.output_text.done":
            assert event["text"] == text
        elif event["type"] == "response.content_part.done":
            assert event["part"]["text"] == text
        elif event["type"] == "response.output_item.done":
            assert event["item"] == output[0]
    return text


def _stream_rows(rows: list[str]) -> list[dict[str, Any]]:
    events = stream.Events(ITEM)
    out = []
    for row in rows:
        out += events.text(row)
    return out + events.end(_response("completed", _redact("".join(rows))))


@pytest.mark.parametrize("text", _redaction_cases())
def test_every_row_split_has_the_json_redaction_and_no_secret_delta(text: str) -> None:
    expected = _redact(text)
    for split in range(len(text) + 1):
        out = _stream_rows([text[:split], text[split:]])
        deltas = [event["delta"] for event in out if event["type"] == "response.output_text.delta"]
        _assert_no_recoverable_secret(deltas, split)
        assert _assert_stream_text(out) == expected, ("JSON redaction differs", split)


def test_an_encoded_block_longer_than_every_hold_never_leaves_in_a_delta() -> None:
    wrapped = base64.b64encode((SECRET + " trailing text" * 400).encode()).decode()
    assert len(wrapped) > max(_STREAM_HOLDBACK_JWT_MAX, TEXT_HOLD_MAX, 4096)
    text = "before " + wrapped + " after"
    out = _stream_rows([text[i : i + 700] for i in range(0, len(text), 700)])
    deltas = [event["delta"] for event in out if event["type"] == "response.output_text.delta"]
    _assert_no_recoverable_secret(deltas, 700)
    assert _assert_stream_text(out) == _redact(text)


@pytest.mark.parametrize("trim", [3, 4])
def test_an_unfinished_encoded_block_ends_with_exactly_the_json_text(trim) -> None:
    wrapped = base64.b64encode((SECRET + " trailing text" * 100).encode()).decode()
    text = "before " + wrapped[:-trim]
    expected = _redact(text)
    # A malformed final quantum is kept by JSON; a complete quantum is redacted.
    assert (expected == text) == (trim == 3)
    for split in range(len(text) + 1):
        out = _stream_rows([text[:split], text[split:]])
        deltas = [event["delta"] for event in out if event["type"] == "response.output_text.delta"]
        if expected != text:
            _assert_no_recoverable_secret(deltas, split)
        assert _assert_stream_text(out) == expected, split


@pytest.mark.asyncio
async def test_the_stream_redacts_an_approved_link_token_that_json_keeps(
    client, monkeypatch
) -> None:
    prefix = "https://approval.example.test/approve"
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", lambda: (prefix,))
    token = synth(24, seed=11)
    text = prefix + "?token=" + token
    assert _redact(text) == text
    expected = prefix + "?token=[REDACTED: credential]"
    for split in range(len(text) + 1):
        out = _stream_rows([text[:split], text[split:]])
        assert _assert_stream_text(out) == expected, split
        assert token not in json.dumps(out)
    client.turn.rows = [("chunk", text, None)]
    _json, body = await _ask(client)
    _sse, out = await _ask_stream(client)
    assert body["output"][0]["content"][0]["text"] == text
    assert _assert_stream_text(out) == expected


@pytest.mark.parametrize("feed_empty", [False, True])
def test_an_unused_redactor_and_empty_feed_flush_without_text(feed_empty) -> None:
    redactor = StreamRedactor(_redact)
    if feed_empty:
        assert redactor.feed("") == ""
    assert redactor.flush() == ""


@pytest.mark.asyncio
async def test_owner_redaction_off_does_not_bypass_the_response_reader(client, monkeypatch) -> None:
    from kiro_crew import redaction_seams
    from kiro_crew.security.redaction_switch import credential_pass_bypassed

    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    run_chat = responses._run_chat
    text = stream.Events.text
    reader_bypasses: list[bool] = []

    async def _owner_turn(*args, **kwargs) -> None:
        with redaction_seams.output_seam(redaction_seams.CHAT):
            assert credential_pass_bypassed()
            await run_chat(*args, **kwargs)

    def _read(events, content):
        reader_bypasses.append(credential_pass_bypassed())
        return text(events, content)

    monkeypatch.setattr(responses, "_run_chat", _owner_turn)
    monkeypatch.setattr(stream.Events, "text", _read)
    client.turn.rows = [("chunk", "key " + SECRET, None)]
    _json, body = await _ask(client)
    _sse, events = await _ask_stream(client)
    assert reader_bypasses == [False]
    assert _deltas(events) == body["output"][0]["content"][0]["text"] == _redact("key " + SECRET)
    assert SECRET not in _deltas(events)


@pytest.mark.parametrize("feed_empty", [False, True])
def test_a_turn_with_no_text_opens_no_item(feed_empty) -> None:
    events = stream.Events(ITEM)
    out = events.start(_response("in_progress"))
    if feed_empty:
        assert events.text("") == []
    out += events.end(_response("completed"))
    assert _types(out) == [*OPENING, "response.completed"]
    assert _assert_stream_text(out) == ""


def test_a_failed_turn_closes_the_item_it_opened_as_incomplete() -> None:
    events = stream.Events(ITEM)
    out = events.start(_response("in_progress")) + events.text("half")
    out += events.end(dict(_response("failed"), error={"code": "server_error", "message": "x"}))
    assert _types(out) == [
        *OPENING,
        *ITEM_OPENS,
        "response.output_text.delta",
        *ITEM_CLOSES,
        "response.failed",
    ]
    assert out[-2]["item"]["status"] == "incomplete"
    assert _assert_stream_text(out) == "half"
    assert out[-1]["response"]["error"]["message"] == "x"


def test_a_failed_turn_closes_its_item_with_the_credential_redacted() -> None:
    events = stream.Events(ITEM)
    out = events.text(f"key {SECRET}")
    out += events.end(dict(_response("failed"), error={"code": "server_error", "message": "x"}))
    assert SECRET not in json.dumps(out)
    assert _assert_stream_text(out) == _redact(f"key {SECRET}")


def test_a_turn_that_could_not_be_run_is_one_error_event() -> None:
    events = stream.Events(ITEM)
    out = events.start(_response("in_progress")) + events.error("the turn could not be run")
    assert out[-1] == {
        "type": "error",
        "sequence_number": 2,
        "code": "internal_error",
        "message": "the turn could not be run",
        "param": None,
    }


def test_an_event_is_written_as_its_type_and_one_json_line() -> None:
    event = {"type": "response.output_text.delta", "sequence_number": 4, "delta": "a\nb"}
    assert stream.encode(event) == (
        b"event: response.output_text.delta\n"
        b'data: {"type": "response.output_text.delta", "sequence_number": 4, "delta": "a\\nb"}\n\n'
    )


# -- the route ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_reads_the_reply_as_a_stream(client) -> None:
    response, events = await _ask_stream(client, {**STREAMED, "metadata": {"k": "v"}})
    assert response.status == 200
    assert response.content_type == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"
    # The headers leave before the owner gate could add them.
    assert response.headers["X-Request-Id"].startswith("req_")
    assert response.headers["Crew-API-Version"] == "1.0"
    assert _types(events) == [
        *OPENING,
        *ITEM_OPENS,
        "response.output_text.delta",
        *ITEM_CLOSES,
        "response.completed",
    ]
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    for event in events:
        _valid("ResponseStreamEvent", event)
    started, final = events[0]["response"], events[-1]["response"]
    _valid("Response", started)
    _valid("Response", final)
    assert (started["status"], started["output"]) == ("in_progress", [])
    assert events[1]["response"] == started
    assert (final["id"], final["metadata"]) == (started["id"], {"k": "v"})
    assert final["status"] == "completed"
    assert final["output"][0]["content"][0]["text"] == _deltas(events) == "Done."
    assert final["output"][0] == events[-2]["item"]
    item_id = final["output"][0]["id"]
    assert events[2]["item"]["id"] == item_id
    assert {event["item_id"] for event in events[3:-2]} == {item_id}
    _no_slot_left(client)


@pytest.mark.asyncio
async def test_text_reaches_the_reader_while_the_turn_still_runs(client, monkeypatch) -> None:
    go_on = asyncio.Event()
    early = "plain words still arriving"

    async def _talks_then_waits(_state: Any, slot: Any, _message: str, **_kwargs: Any) -> None:
        slot.append("chunk", early, "chunk")
        await go_on.wait()
        slot.append("chunk", " and the rest", "chunk")
        slot.append("done", "", "done")

    monkeypatch.setattr(responses, "_run_chat", _talks_then_waits)
    response = await client.post(PATH, json=STREAMED, headers=AS_OWNER)

    async def _first_delta() -> dict[str, Any]:
        while True:
            line = await response.content.readline()
            if line.startswith(b'data: {"type": "response.output_text.delta"'):
                return json.loads(line.removeprefix(b"data: "))

    delta = await await_until(asyncio.ensure_future(_first_delta()), what="the first delta")
    assert delta["delta"] == "plain words still "
    assert not go_on.is_set()
    go_on.set()
    rest = await await_until(asyncio.ensure_future(response.text()), what="the stream's end")
    events = _events(rest)
    assert delta["delta"] + _deltas(events) == early + " and the rest"
    assert events[-1]["response"]["status"] == "completed"


@pytest.mark.asyncio
async def test_the_stream_ends_with_the_response_the_json_answer_is(client) -> None:
    _response_json, body = await _ask(client, {**ASK, "temperature": 0.1})
    _response_sse, events = await _ask_stream(client, {**STREAMED, "temperature": 0.1})
    final = events[-1]["response"]
    for volatile in ("id", "created_at"):
        final.pop(volatile), body.pop(volatile)
    final["output"][0].pop("id"), body["output"][0].pop("id")
    assert final == body


@pytest.mark.asyncio
@pytest.mark.parametrize("last_word", _stop_reasons())
@pytest.mark.parametrize("stop", _STOPS)
async def test_a_stream_ends_on_the_status_the_json_gives_for_every_cell(
    client, last_word, stop
) -> None:
    """The Stop table drives both: one request reads the same as JSON and as a stream."""
    client.turn.stop_reason = last_word
    if stop == "none":
        _json, body = await _ask(client)
        _sse, events = await _ask_stream(client)
    else:
        _json, body = await _press_stop(client, stop)
        _sse, events = await _press_stop(client, stop, ask=_ask_stream)
    row = _STATUS_TABLE.get(last_word, _STATUS_TABLE[None])
    last = events[-1]
    assert body["status"] == last["response"]["status"] == row[_STOPS.index(stop)]
    assert last["type"] == stream.terminal_event(body["status"])
    assert last["response"]["output"][0]["content"][0]["text"] == _deltas(events) == "Done."
    assert last["response"]["incomplete_details"] is None
    _valid("Response", last["response"])
    _no_slot_left(client)


@pytest.mark.asyncio
async def test_a_stopped_turn_ends_the_stream_incomplete_with_status_cancelled(client) -> None:
    client.turn.rows = [("chunk", "half", None)]
    response, events = await _press_stop(client, "soft", ask=_ask_stream)
    assert response.status == 200
    assert _types(events)[-4:] == [*ITEM_CLOSES, "response.incomplete"]
    final = events[-1]["response"]
    assert (final["status"], final["incomplete_details"]) == ("cancelled", None)
    assert final["output"][0]["status"] == "incomplete"
    assert final["output"][0]["content"][0]["text"] == "half"


@pytest.mark.asyncio
async def test_a_turn_that_ended_on_its_error_ends_the_stream_failed(client) -> None:
    client.turn.rows = [("error", "the engine fell over", None)]
    response, events = await _ask_stream(client)
    assert response.status == 200
    assert _types(events) == [*OPENING, "response.failed"]
    final = events[-1]["response"]
    _valid("Response", final)
    assert final["status"] == "failed"
    assert final["error"] == {"code": "server_error", "message": "the engine fell over"}


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_its_stream_text_without_changing_the_json_answer(client) -> None:
    client.turn.rows = [("chunk", f"key {SECRET}", None), ("error", "the engine fell over", None)]
    body = {**ASK, "metadata": {"k": "v"}}
    _json, answer = await _ask(client, body)
    _sse, out = await _ask_stream(client, {**body, "stream": True})
    final = out[-1]["response"]
    assert answer["output"] == []
    assert final["status"] == answer["status"] == "failed"
    assert final["error"] == answer["error"]
    assert final["metadata"] == answer["metadata"] == {"k": "v"}
    assert final["id"] == out[0]["response"]["id"]
    assert final["output"][0]["status"] == "incomplete"
    assert final["output"][0]["id"] == out[2]["item"]["id"]
    assert _assert_stream_text(out) == _redact(f"key {SECRET}")
    for event in out:
        _valid("ResponseStreamEvent", event)


@pytest.mark.asyncio
async def test_a_turn_cut_at_the_ceiling_ends_the_stream_incomplete(client, monkeypatch) -> None:
    client.turn.release = asyncio.Event()  # never set: only the ceiling ends this turn
    ceiling = functools.partial(
        spawn_guarded_turn,
        timeout_secs=0.01,  # flake-ok: the ceiling under test; the turn waits on an event never set
        cancel_ack_timeout_secs=0.01,  # flake-ok: no engine acknowledges; the bound only ends the wait
    )
    monkeypatch.setattr(responses, "spawn_guarded_turn", ceiling)
    _response_sse, events = await _ask_stream(client)
    assert _types(events) == [*OPENING, "response.incomplete"]
    final = events[-1]["response"]
    assert (final["status"], final["incomplete_details"]) == (
        "incomplete",
        {"reason": "turn_time_limit"},
    )
    _no_slot_left(client)


@pytest.mark.asyncio
async def test_a_turn_task_that_raises_ends_the_stream_with_an_error_event(client) -> None:
    client.turn.raises = RuntimeError("boom")
    response, events = await _ask_stream(client)
    assert response.status == 200  # the headers left before the turn failed
    assert _types(events) == [*OPENING, "error"]
    assert (events[-1]["code"], events[-1]["message"]) == (
        "internal_error",
        "the turn could not be run",
    )
    assert "boom" not in json.dumps(events)
    _valid("ResponseStreamEvent", events[-1])
    _no_slot_left(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"model": "nobody"}, 404, "model_not_found"),
        ({"input": ""}, 400, "invalid_request"),
        ({"background": True}, 422, "capability_unavailable"),
    ],
)
async def test_a_request_refused_before_its_turn_is_answered_as_json(
    client, change, status, code
) -> None:
    response, body = await _ask_stream(client, {**STREAMED, **change})
    assert response.content_type == "application/json"
    _contract_error(response, body, status, code)
    assert client.turn.entered == []


@pytest.mark.asyncio
async def test_a_credential_in_the_reply_never_leaves_in_the_stream(client) -> None:
    client.turn.rows = [("chunk", f"key {SECRET[:12]}", None), ("chunk", SECRET[12:], None)]
    response = await client.post(PATH, json=STREAMED, headers=AS_OWNER)
    raw = await response.text()
    assert SECRET not in raw
    assert _deltas(_events(raw)) == _redact(f"key {SECRET}")


@pytest.mark.asyncio
async def test_a_silent_turn_gets_a_keepalive_comment(client, monkeypatch) -> None:
    client.turn.release = asyncio.Event()
    monkeypatch.setattr(responses, "WAKE_SECS", 0)
    response = await client.post(PATH, json=STREAMED, headers=AS_OWNER)

    async def _until_keepalive() -> list[bytes]:
        lines = []
        while b": keepalive\n" not in lines:
            lines.append(await response.content.readline())
        return lines

    lines = await await_until(asyncio.ensure_future(_until_keepalive()), what="a keepalive line")
    assert lines[0] == b"event: response.created\n"
    client.turn.release.set()
    rest = await await_until(asyncio.ensure_future(response.text()), what="the stream's end")
    assert _types(_events(rest))[-1] == "response.completed"


@pytest.mark.asyncio
async def test_a_stream_reader_that_left_stops_the_turn(client, monkeypatch) -> None:
    client.turn.release = asyncio.Event()  # never set: the turn waits until it is stopped
    stops: list[dict[str, Any]] = []

    async def _stop(_state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        stops.append(kwargs)
        slot.task.cancel()
        return {"ok": True}

    monkeypatch.setattr(responses, "stop_slot_turn", _stop)
    monkeypatch.setattr(responses, "_client_gone", lambda _request: client.turn.started.is_set())
    monkeypatch.setattr(responses, "WAKE_SECS", 0)
    await _ask_stream(client)
    assert stops == [{"source": "crew_api", "escalate": False}]
    _no_slot_left(client)


@pytest.mark.asyncio
async def test_a_write_the_reader_never_takes_stops_the_turn(client, monkeypatch) -> None:
    client.turn.release = asyncio.Event()  # never set: the turn waits until it is stopped
    stops: list[dict[str, Any]] = []

    async def _stop(_state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        stops.append(kwargs)
        slot.task.cancel()
        return {"ok": True}

    async def _refused(_request: Any, _resp: Any, _data: bytes) -> None:
        raise ConnectionResetError("the reader is gone")

    monkeypatch.setattr(responses, "stop_slot_turn", _stop)
    monkeypatch.setattr(responses, "write_sse_or_abort", _refused)
    response = await client.post(PATH, json=STREAMED, headers=AS_OWNER)
    assert await response.text() == ""
    assert stops == [{"source": "crew_api", "escalate": False}]
    _no_slot_left(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_fails", [False, True])
async def test_a_stream_follows_a_recovery_handoff(client, monkeypatch, recovery_fails) -> None:
    rows = (
        [("error", f"backend failed: {SECRET}", None)]
        if recovery_fails
        else [("chunk", "Recovered.", None)]
    )
    successor = _Turn(rows)
    successors: list[asyncio.Task[Any]] = []

    async def _hands_off(state: Any, slot: Any, message: str, **kwargs: Any) -> None:
        slot.append("error", "retrying", "msg msg-err", meta={"kind": TRANSIENT_RETRY_KIND})
        slot.task = spawn_guarded_turn(state, slot, successor(state, slot, message, **kwargs))
        successors.append(slot.task)
        # No done row: the successor owns the rest of this cycle.

    monkeypatch.setattr(responses, "_run_chat", _hands_off)
    response, events = await _ask_stream(client)
    assert response.status == 200
    final = events[-1]["response"]
    _valid("Response", final)
    if recovery_fails:
        assert _types(events) == [*OPENING, "response.failed"]
        assert final["error"] == {
            "code": "server_error",
            "message": _redact(f"backend failed: {SECRET}"),
        }
        assert SECRET not in json.dumps(events)
    else:
        assert events[-1]["type"] == "response.completed"
        assert _deltas(events) == "Recovered."
        assert final["output"][0]["content"][0]["text"] == "Recovered."
    assert successors[0].done()
    _no_slot_left(client)


@pytest.mark.asyncio
async def test_a_recovery_that_outlives_its_stream_is_cancelled(client, monkeypatch) -> None:
    successor = _Turn()
    successor.release = asyncio.Event()  # never set: the recovery cannot finish on its own
    successors: list[asyncio.Task[Any]] = []

    async def _hands_off(state: Any, slot: Any, message: str, **kwargs: Any) -> None:
        slot.task = spawn_guarded_turn(state, slot, successor(state, slot, message, **kwargs))
        successors.append(slot.task)

    async def _stops_nothing(_state: Any, _slot: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr(responses, "_run_chat", _hands_off)
    monkeypatch.setattr(responses, "stop_slot_turn", _stops_nothing)
    monkeypatch.setattr(responses, "_client_gone", lambda _request: successor.started.is_set())
    monkeypatch.setattr(responses, "WAKE_SECS", 0)
    budget = "native_cancel_ack_timeout_secs"
    monkeypatch.setattr(responses, budget, lambda: 0)  # flake-ok: no engine acknowledges the Stop
    try:
        await _ask_stream(client)
        task = successors[0]
        await await_until(asyncio.ensure_future(asyncio.wait({task})), what="the recovery to end")
        assert task.cancelled()
        _no_slot_left(client)
    finally:
        # Even an unfixed route must not leave the test's stand-in running.
        successors[0].cancel()
        await asyncio.wait({successors[0]})


@pytest.mark.asyncio
async def test_a_stream_names_the_request_id_the_caller_sent(client) -> None:
    response = await client.post(
        PATH, json=STREAMED, headers={**AS_OWNER, "X-Request-Id": "req_mine"}
    )
    await response.text()
    assert response.headers["X-Request-Id"] == "req_mine"


@pytest.mark.asyncio
async def test_a_failed_write_after_a_recovery_handoff_stops_the_successor(
    client, monkeypatch
) -> None:
    successor = _Turn()
    successor.release = asyncio.Event()  # never set: the recovery waits until it is stopped
    successors: list[asyncio.Task[Any]] = []
    stops: list[dict[str, Any]] = []

    async def _hands_off(state: Any, slot: Any, message: str, **kwargs: Any) -> None:
        slot.task = spawn_guarded_turn(state, slot, successor(state, slot, message, **kwargs))
        successors.append(slot.task)

    async def _stop(_state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        stops.append(kwargs)
        slot.task.cancel()
        return {"ok": True}

    async def _refused_once_recovered(_request: Any, _resp: Any, _data: bytes) -> None:
        if successor.started.is_set():
            raise ConnectionResetError("the reader is gone")

    monkeypatch.setattr(responses, "_run_chat", _hands_off)
    monkeypatch.setattr(responses, "stop_slot_turn", _stop)
    monkeypatch.setattr(responses, "write_sse_or_abort", _refused_once_recovered)
    monkeypatch.setattr(responses, "WAKE_SECS", 0)
    try:
        response = await client.post(PATH, json=STREAMED, headers=AS_OWNER)
        await response.text()
        assert stops == [{"source": "crew_api", "escalate": False}]
        await await_until(
            asyncio.ensure_future(asyncio.wait({successors[0]})), what="the recovery to end"
        )
        assert successors[0].cancelled()
        _no_slot_left(client)
    finally:
        # Even an unfixed route must not leave the test's stand-in running.
        successors[0].cancel()
        await asyncio.wait({successors[0]})
