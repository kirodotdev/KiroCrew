"""``GET /v1/responses/{response_id}`` and its ``input_items``: a turn read back.

The routes run around the same stand-in for ``_run_chat`` as ``test_crew_api_responses.py``.
"""

from __future__ import annotations

import asyncio
import functools
import json
from typing import Any

import pytest
import yaml
from test_crew_api_response_stream import _ask_stream, _events
from test_crew_api_responses import (
    AS_OWNER,
    ASK,
    CALLERS,
    MODEL,
    PATH,
    SECRET,
    SPEC_DIR,
    _ask,
    _contract_error,
    _valid,
)
from test_crew_api_responses import client as _responses_client

from kiro_crew.dashboard import crew_api_response_reads as reads
from kiro_crew.dashboard import crew_api_response_records as records
from kiro_crew.dashboard import crew_api_responses as responses
from kiro_crew.dashboard.crew_api_response_records import STORE_KEY, ResponseStore
from kiro_crew.dashboard.crew_api_response_stream import encode
from kiro_crew.dashboard.openai_compat import _redact
from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
from kiro_crew.security import redaction
from kiro_crew.testing.synthetic_credentials import synth
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

client = _responses_client  # the route fixture of the POST tests
PUBLIC_SPEC = yaml.safe_load((SPEC_DIR / "crew-public.yaml").read_text(encoding="utf-8"))
UNKNOWN = "resp_" + "0" * 32
TALK = {
    "model": MODEL,
    "instructions": "be brief",
    "input": [
        {"role": "developer", "content": "use metric units"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "input_text", "text": "second"}]},
    ],
}


def _store(client) -> ResponseStore:
    return client.app[STORE_KEY]


async def _get(client, path: str, headers: dict[str, str] | None = None):
    response = await client.get(path, headers=AS_OWNER if headers is None else headers)
    return response, await response.json()


async def _replayed(client, response_id: str, query: str = "stream=true"):
    response = await client.get(f"{PATH}/{response_id}?{query}", headers=AS_OWNER)
    assert response.status == 200
    assert response.content_type == "text/event-stream"
    assert response.headers["X-Request-Id"].startswith("req_")
    assert response.headers["Crew-API-Version"] == "1.0"
    return _events(await response.text())


def _files(client) -> list[str]:
    directory = _store(client).directory
    return sorted(p.name for p in directory.iterdir()) if directory and directory.exists() else []


# -- the Response ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_reads_back_as_the_response_it_was_answered_with(client) -> None:
    _response, answered = await _ask(client, {**ASK, "temperature": 0.1, "metadata": {"k": "v"}})
    response, body = await _get(client, f"{PATH}/{answered['id']}")
    assert response.status == 200
    assert body == answered
    _valid("Response", body)
    assert response.headers["X-Request-Id"].startswith("req_")
    assert response.headers["Crew-API-Version"] == "1.0"


@pytest.mark.asyncio
async def test_a_streamed_turn_reads_back_as_the_response_its_stream_ended_on(client) -> None:
    _response, sent = await _ask_stream(client)
    _response, body = await _get(client, f"{PATH}/{sent[-1]['response']['id']}")
    assert body == sent[-1]["response"]
    assert body["status"] == "completed"


@pytest.mark.asyncio
async def test_a_streamed_turn_sends_its_events_again(client) -> None:
    _response, sent = await _ask_stream(client)
    response_id = sent[-1]["response"]["id"]
    assert await _replayed(client, response_id) == sent
    after = await _replayed(client, response_id, "stream=true&starting_after=3")
    assert after == sent[4:]
    assert [event["sequence_number"] for event in after][0] == 4
    assert await _replayed(client, response_id, "stream=true&starting_after=999") == []


@pytest.mark.asyncio
async def test_a_turn_answered_as_json_gets_events_made_from_its_response(client) -> None:
    _response, answered = await _ask(client)
    events = await _replayed(client, answered["id"])
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert [event["type"] for event in events][:2] == ["response.created", "response.in_progress"]
    assert events[0]["response"]["status"] == "in_progress"
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"] == answered
    deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
    assert "".join(deltas) == "Done."
    for event in events:
        _valid("ResponseStreamEvent", event)


@pytest.mark.asyncio
async def test_a_turn_whose_stream_ended_on_an_error_reads_back_failed(client) -> None:
    client.turn.raises = RuntimeError("engine fell over")
    _response, sent = await _ask_stream(client)
    assert sent[-1]["type"] == "error"
    response_id = sent[0]["response"]["id"]
    _response, body = await _get(client, f"{PATH}/{response_id}")
    assert body["status"] == "failed"
    assert body["error"]["message"] == sent[-1]["message"]
    assert await _replayed(client, response_id) == sent


@pytest.mark.asyncio
async def test_a_turn_whose_reader_left_reads_back_cancelled(client, monkeypatch) -> None:
    client.turn.release = asyncio.Event()  # never set: the turn waits until it is stopped

    async def _stop(_state: Any, slot: Any, **_kwargs: Any) -> dict[str, Any]:
        slot.task.cancel()
        return {"ok": True}

    async def _refused(_request: Any, _resp: Any, _data: bytes) -> None:
        raise ConnectionResetError("the reader is gone")

    monkeypatch.setattr(responses, "stop_slot_turn", _stop)
    monkeypatch.setattr(responses, "write_sse_or_abort", _refused)
    response = await client.post(PATH, json={**ASK, "stream": True}, headers=AS_OWNER)
    await response.read()  # the body ends when the handler has returned
    (name,) = _files(client)
    response_id = name.removesuffix(".json")
    _response, body = await _get(client, f"{PATH}/{response_id}")
    assert (body["status"], body["output"]) == ("cancelled", [])
    events = await _replayed(client, response_id)
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.incomplete",
    ]
    assert events[-1]["response"] == body


@pytest.mark.asyncio
async def test_a_turn_stopped_for_a_lost_connection_reads_back_cancelled(
    client, monkeypatch
) -> None:
    client.turn.release = asyncio.Event()  # never set: the turn waits until it is stopped

    async def _stop(_state: Any, slot: Any, **_kwargs: Any) -> dict[str, Any]:
        slot.task.cancel()
        return {"ok": True}

    monkeypatch.setattr(responses, "stop_slot_turn", _stop)
    monkeypatch.setattr(responses, "_client_gone", lambda _request: client.turn.started.is_set())
    monkeypatch.setattr(responses, "WAKE_SECS", 0)
    _response, sent = await _ask_stream(client)
    assert [event["type"] for event in sent] == ["response.created", "response.in_progress"]
    _response, body = await _get(client, f"{PATH}/{sent[0]['response']['id']}")
    assert (body["status"], body["error"]) == ("cancelled", None)
    events = await _replayed(client, body["id"])
    assert events[-1]["type"] == "response.incomplete"
    assert events[-1]["response"] == body


@pytest.mark.asyncio
async def test_a_turn_that_ends_while_it_is_being_read_is_still_found(client, monkeypatch) -> None:
    client.turn.release = asyncio.Event()
    asking = asyncio.ensure_future(_ask(client))
    await asyncio.wait_for(client.turn.started.wait(), FALLBACK_TIMEOUT_SECS)
    store = _store(client)
    (response_id,) = store.live

    def _ends_meanwhile(_response_id: str) -> None:
        # The disk had no record yet; by the time the read returns the turn is over.
        store.live.clear()

    monkeypatch.setattr(store, "load", _ends_meanwhile)
    _response, body = await _get(client, f"{PATH}/{response_id}")
    assert (body["id"], body["status"]) == (response_id, "in_progress")
    client.turn.release.set()
    await asyncio.wait_for(asking, FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
async def test_a_running_turn_reads_in_progress_and_its_stream_is_its_opening(client) -> None:
    client.turn.release = asyncio.Event()
    asking = asyncio.ensure_future(_ask(client, TALK))
    await asyncio.wait_for(client.turn.started.wait(), FALLBACK_TIMEOUT_SECS)
    (response_id,) = _store(client).live
    _response, body = await _get(client, f"{PATH}/{response_id}")
    assert (body["status"], body["output"]) == ("in_progress", [])
    _valid("Response", body)
    events = await _replayed(client, response_id)
    assert [event["type"] for event in events] == ["response.created", "response.in_progress"]
    _response, items = await _get(client, f"{PATH}/{response_id}/input_items?order=asc")
    assert [item["role"] for item in items["data"]] == ["developer", "user", "assistant", "user"]
    assert _files(client) == []
    client.turn.release.set()
    _response, answered = await asyncio.wait_for(asking, FALLBACK_TIMEOUT_SECS)
    assert _store(client).live == {}
    _response, body = await _get(client, f"{PATH}/{response_id}")
    assert body == answered
    assert body["status"] == "completed"


# -- the input items -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_input_items_are_the_items_sent_newest_first(client) -> None:
    _response, answered = await _ask(client, TALK)
    response, body = await _get(client, f"{PATH}/{answered['id']}/input_items")
    assert response.status == 200
    _valid("InputItemList", body)
    assert body["object"] == "list" and body["has_more"] is False
    data = body["data"]
    assert [item["role"] for item in data] == ["user", "assistant", "user", "developer"]
    assert [item["content"] for item in data] == [
        [{"type": "input_text", "text": text}]
        for text in ("second", "reply", "first", "use metric units")
    ]
    assert all(item["type"] == "message" and item["status"] == "completed" for item in data)
    assert all(item["id"].startswith("msg_") for item in data)
    assert len({item["id"] for item in data}) == 4
    assert (body["first_id"], body["last_id"]) == (data[0]["id"], data[-1]["id"])
    # ``instructions`` is not an input item.
    assert "be brief" not in json.dumps(body)


@pytest.mark.asyncio
async def test_the_input_items_page_by_after_limit_and_order(client) -> None:
    _response, answered = await _ask(client, TALK)
    path = f"{PATH}/{answered['id']}/input_items"
    _response, whole = await _get(client, f"{path}?order=asc")
    ids = [item["id"] for item in whole["data"]]
    _response, first = await _get(client, f"{path}?order=asc&limit=2")
    assert [item["id"] for item in first["data"]] == ids[:2]
    assert first["has_more"] is True and first["last_id"] == ids[1]
    _response, rest = await _get(client, f"{path}?order=asc&limit=2&after={ids[1]}")
    assert [item["id"] for item in rest["data"]] == ids[2:]
    assert rest["has_more"] is False
    _response, empty = await _get(client, f"{path}?order=asc&after={ids[-1]}")
    assert empty == {
        "object": "list",
        "data": [],
        "first_id": None,
        "last_id": None,
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_a_string_input_is_one_user_item(client) -> None:
    _response, answered = await _ask(client)
    _response, body = await _get(client, f"{PATH}/{answered['id']}/input_items")
    (item,) = body["data"]
    assert (item["role"], item["content"]) == ("user", [{"type": "input_text", "text": "hello"}])


# -- what is kept ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_credential_in_the_input_or_the_reply_never_reaches_the_file(client) -> None:
    client.turn.rows = [("chunk", f"the key is {SECRET}", None)]
    _response, answered = await _ask(client, {**ASK, "input": f"my token is {SECRET}"})
    (name,) = _files(client)
    assert name == answered["id"] + ".json"
    kept = (_store(client).directory / name).read_text(encoding="utf-8")
    assert SECRET not in kept
    _response, items = await _get(client, f"{PATH}/{answered['id']}/input_items")
    assert SECRET not in json.dumps(items)
    assert "my token is" in items["data"][0]["content"][0]["text"]
    for query in ("", "?stream=true"):
        raw = await (await client.get(f"{PATH}/{answered['id']}{query}", headers=AS_OWNER)).text()
        assert SECRET not in raw


@pytest.mark.asyncio
async def test_a_streamed_credential_never_reaches_the_file(client) -> None:
    client.turn.rows = [("chunk", "the key is ", None), ("chunk", SECRET, None)]
    _response, sent = await _ask_stream(client, {**ASK, "stream": True, "input": SECRET})
    (name,) = _files(client)
    assert SECRET not in (_store(client).directory / name).read_text(encoding="utf-8")
    assert await _replayed(client, sent[-1]["response"]["id"]) == sent


@pytest.mark.parametrize("text", [f"the key is {SECRET}", f"{SECRET} {SECRET}", "plain", ""])
def test_redacting_text_the_redaction_already_passed_changes_nothing(text) -> None:
    once = _redact(text)
    assert _redact(once) == once
    assert SECRET not in once


@pytest.mark.asyncio
async def test_a_record_someone_else_wrote_is_redacted_on_its_way_out(client) -> None:
    client.turn.rows = [("chunk", "Do", None), ("chunk", "ne.", None)]
    _response, sent = await _ask_stream(client)
    (name,) = _files(client)
    path = _store(client).directory / name
    kept = json.loads(path.read_text(encoding="utf-8"))
    half = len(SECRET) // 2
    leak = f"the key is {SECRET}"
    kept["response"]["output"][0]["content"][0]["text"] = leak
    kept["input"][0]["content"][0]["text"] = leak
    # In the stream the credential lies across two deltas: neither holds it whole.
    kept["stream"]["deltas"] = ["the key is " + SECRET[:half], SECRET[half:]]
    path.write_text(json.dumps(kept), encoding="utf-8")
    response_id = sent[-1]["response"]["id"]
    for suffix in ("", "/input_items", "?stream=true"):
        raw = await (await client.get(f"{PATH}/{response_id}{suffix}", headers=AS_OWNER)).text()
        assert SECRET not in raw
        assert "the key is" in raw
    events = await _replayed(client, response_id)
    assert events[-1]["type"] == "response.completed"
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    carried = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    assert carried == _redact(leak)


@pytest.mark.asyncio
async def test_a_credential_in_the_text_of_a_stream_closed_on_an_error_event_stays_in(
    client,
) -> None:
    client.turn.raises = RuntimeError("engine fell over")
    _response, sent = await _ask_stream(client)
    (name,) = _files(client)
    path = _store(client).directory / name
    kept = json.loads(path.read_text(encoding="utf-8"))
    half = len(SECRET) // 2
    # The stream carried text before it closed on its error event: nothing else holds it.
    kept["stream"]["deltas"] = ["the key is " + SECRET[:half], SECRET[half:]]
    path.write_text(json.dumps(kept), encoding="utf-8")
    events = await _replayed(client, sent[0]["response"]["id"])
    assert SECRET not in json.dumps(events)
    assert events[-1]["type"] == "response.failed"
    _response, body = await _get(client, f"{PATH}/{sent[0]['response']['id']}")
    assert events[-1]["response"] == body


@pytest.mark.asyncio
async def test_a_reply_with_an_approved_link_reads_the_same_as_json_and_as_a_stream(
    client, monkeypatch
) -> None:
    prefix = "https://approval.example.test/approve"
    monkeypatch.setattr(redaction, "_token_param_exempt_prefixes", lambda: (prefix,))
    link = f"{prefix}?token={synth(24, seed=11)}"
    client.turn.rows = [("chunk", f"open {link}", None)]
    _response, answered = await _ask(client)
    assert link in answered["output"][0]["content"][0]["text"]
    _response, body = await _get(client, f"{PATH}/{answered['id']}")
    assert body == answered
    events = await _replayed(client, answered["id"])
    assert events[-1]["response"] == answered
    assert _stream_fault("".join(encode(event).decode() for event in events), answered) == ""


@pytest.mark.asyncio
async def test_metadata_is_kept_and_read_back_as_the_owner_sent_it(client) -> None:
    pairs = {"trace": SECRET, "text": "a note"}
    _response, answered = await _ask(client, {**ASK, "metadata": pairs})
    assert answered["metadata"] == pairs
    _response, body = await _get(client, f"{PATH}/{answered['id']}")
    assert body == answered


async def _answered_whole(client, streamed: bool) -> dict[str, Any]:
    """Ask for the five-byte reply ``Done.`` and check the turn was answered in full."""
    if streamed:
        _response, sent = await _ask_stream(client)
        answered = sent[-1]["response"]
        assert sent[-1]["type"] == "response.completed"
    else:
        response, answered = await _ask(client)
        assert response.status == 200
    assert answered["status"] == "completed"
    assert answered["output"][0]["content"][0]["text"] == "Done."
    return answered


HOW_ANSWERED = pytest.mark.parametrize(
    "streamed", [False, True], ids=["answered as JSON", "answered as a stream"]
)


@pytest.mark.asyncio
@HOW_ANSWERED
async def test_a_reply_of_exactly_the_cap_is_kept_and_read_back(
    client, monkeypatch, streamed
) -> None:
    monkeypatch.setattr(records, "MAX_REPLY_BYTES", len("Done."))
    answered = await _answered_whole(client, streamed)
    _response, body = await _get(client, f"{PATH}/{answered['id']}")
    assert body == answered


@pytest.mark.asyncio
@HOW_ANSWERED
async def test_a_reply_one_byte_over_the_cap_is_answered_and_then_not_found(
    client, monkeypatch, caplog, streamed
) -> None:
    monkeypatch.setattr(records, "MAX_REPLY_BYTES", len("Done.") - 1)
    with caplog.at_level("WARNING", logger=responses.logger.name):
        answered = await _answered_whole(client, streamed)
    assert _files(client) == []
    assert [r.getMessage() for r in caplog.records if "not recorded" in r.getMessage()] == [
        f"Crew API response {answered['id']} was not recorded: ValueError"
    ]
    for suffix in READS:
        response, body = await _get(client, f"{PATH}/{answered['id']}{suffix}")
        _contract_error(response, body, 404, "not_found")


@pytest.mark.asyncio
async def test_a_request_that_is_refused_keeps_nothing(client) -> None:
    response, _body = await _ask(client, {"model": MODEL})
    assert response.status == 400
    response, _body = await _ask(client, {**ASK, "model": "nobody"})
    assert response.status == 404
    assert _files(client) == []
    assert _store(client).live == {}


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_written_does_not_change_the_answer(
    client, monkeypatch
) -> None:
    def _full(_record: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_store(client), "save", _full)
    response, answered = await _ask(client)
    assert (response.status, answered["status"]) == (200, "completed")
    response, body = await _get(client, f"{PATH}/{answered['id']}")
    _contract_error(response, body, 404, "not_found")


# -- refusals --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("suffix", ["", "/input_items"])
async def test_only_the_machine_owner_reads_a_turn_back(
    client, monkeypatch, caller, suffix
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _response, answered = await _ask(client)

    def _no_read(_response_id: str) -> None:
        raise AssertionError("a refused caller must not reach the records")

    monkeypatch.setattr(_store(client), "load", _no_read)
    response, body = await _get(client, f"{PATH}/{answered['id']}{suffix}", headers=caller)
    _contract_error(response, body, 403, "owner_only")


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/input_items", "?stream=true"])
@pytest.mark.parametrize("response_id", [UNKNOWN, "resp_1", "nope", "resp_" + "A" * 32])
async def test_an_id_that_names_no_turn_is_not_found(client, suffix, response_id) -> None:
    await _ask(client)
    response, body = await _get(client, f"{PATH}/{response_id}{suffix}")
    _contract_error(response, body, 404, "not_found")
    assert len(_files(client)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "?stream=yes",
        "?stream=1",
        "?stream=true&starting_after=-1",
        "?starting_after=abc",
        "?starting_after=1.5",
        "/input_items?limit=0",
        "/input_items?limit=101",
        "/input_items?limit=ten",
        "/input_items?order=up",
        "/input_items?after=msg_nobody",
    ],
)
async def test_a_query_value_the_contract_does_not_allow_is_refused(client, query) -> None:
    _response, answered = await _ask(client)
    response, body = await _get(client, f"{PATH}/{answered['id']}{query}")
    _contract_error(response, body, 400, "invalid_request")


SMALL_CAP = 64


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1], ids=["exactly the cap", "one byte over"])
async def test_the_request_cap_is_the_line_between_kept_and_not(client, monkeypatch, extra) -> None:
    monkeypatch.setattr(responses, "MAX_REQUEST_BYTES", SMALL_CAP)
    _response, answered = await _ask(client, {"model": MODEL, "input": "x" * (SMALL_CAP + extra)})
    assert answered["status"] == "completed"
    response = await client.get(f"{PATH}/{answered['id']}", headers=AS_OWNER)
    assert response.status == (404 if extra else 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("short", [1, 0], ids=["one byte under the cap", "exactly the cap"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_a_request_within_the_cap_is_kept_when_redaction_makes_it_longer(
    client, monkeypatch, short, stream
) -> None:
    grows = " CONTEXT ENTRY BEGIN ---\nhttps://example.test/?token=x"
    cap = len(grows) + 8
    monkeypatch.setattr(responses, "MAX_REQUEST_BYTES", cap)
    # Also where a record is written, so a second count there would drop this turn.
    monkeypatch.setattr(records, "MAX_REQUEST_BYTES", cap)
    sent = "-" * (cap - short - len(grows)) + grows
    asked = {"model": MODEL, "input": sent, "stream": stream}
    response = await client.post(PATH, json=asked, headers=AS_OWNER)
    assert response.status == 200
    await response.read()
    (name,) = _files(client)
    response_id = name.removesuffix(".json")
    for suffix in READS:
        response = await client.get(f"{PATH}/{response_id}{suffix}", headers=AS_OWNER)
        assert response.status == 200
    _response, body = await _get(client, f"{PATH}/{response_id}/input_items")
    (item,) = body["data"]
    kept = item["content"][0]["text"]
    # Redacted, and longer for it than the line the request as sent stayed within.
    assert "token=x" not in kept and len(kept.encode()) > cap


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_read_is_unavailable_and_stays_where_it_is(
    client, monkeypatch, caplog
) -> None:
    _response, answered = await _ask(client)
    (name,) = _files(client)
    kept = (_store(client).directory / name).read_bytes()

    def _held(*_args: Any, **_kwargs: Any) -> bytes:
        raise PermissionError(13, "another process holds the file", name)

    with monkeypatch.context() as patched, caplog.at_level("WARNING", logger=reads.logger.name):
        patched.setattr(records, "read_bytes_with_retry", _held)
        for suffix in READS:
            response, body = await _get(client, f"{PATH}/{answered['id']}{suffix}")
            _contract_error(response, body, 503, "instance_unavailable")
    # Unreadable is not damaged: nothing was moved, and the warning names the path alone.
    assert _files(client) == [name]
    assert (_store(client).directory / name).read_bytes() == kept
    assert {r.getMessage() for r in caplog.records} == {
        f"Crew API response record could not be read: {name}"
    }
    for suffix in READS:
        response = await client.get(f"{PATH}/{answered['id']}{suffix}", headers=AS_OWNER)
        assert response.status == 200
    _response, body = await _get(client, f"{PATH}/{answered['id']}")
    assert body == answered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asked",
    [
        {"input": "x" * (SMALL_CAP + 1)},
        # Shrinks to a few bytes of prompt, and would be kept as it was sent.
        {"input": "-" * (SMALL_CAP + 1) + " CONTEXT ENTRY BEGIN ---\nhello"},
        {"input": "hello", "x" * SMALL_CAP: 1},
    ],
    ids=["a long input", "an input that shrinks into the prompt", "a long ignored field name"],
)
async def test_a_request_too_large_to_keep_is_answered_and_then_not_found(
    client, monkeypatch, caplog, asked
) -> None:
    # The route reads the cap it imported; a small one keeps the request small.
    monkeypatch.setattr(responses, "MAX_REQUEST_BYTES", SMALL_CAP)
    with caplog.at_level("WARNING", logger=responses.logger.name):
        response, answered = await _ask(client, {"model": MODEL, **asked})
    assert (response.status, answered["status"]) == (200, "completed")
    assert answered["output"][0]["content"][0]["text"] == "Done."
    assert _files(client) == [] and _store(client).live == {}
    assert [r.getMessage() for r in caplog.records if "not recorded" in r.getMessage()] == [
        f"Crew API response {answered['id']} was not recorded: its request is too large"
    ]
    for suffix in READS:
        response, body = await _get(client, f"{PATH}/{answered['id']}{suffix}")
        _contract_error(response, body, 404, "not_found")


@pytest.mark.asyncio
async def test_each_read_writes_an_audit_row_that_names_only_a_real_id(client, monkeypatch) -> None:
    monkeypatch.setattr(reads, "sel", lambda: client.audit)
    _response, answered = await _ask(client, {**ASK, "input": "a private question"})
    await _get(client, f"{PATH}/{answered['id']}")
    await _get(client, f"{PATH}/{answered['id']}/input_items")
    await _get(client, f"{PATH}/not-an-id")
    # Refused for its own value, after the turn was found and read.
    response, _body = await _get(client, f"{PATH}/{answered['id']}/input_items?after=msg_nobody")
    assert response.status == 400
    rows = [
        call.kwargs
        for call in client.audit.log_api_access.call_args_list
        if call.kwargs.get("operation", "").startswith("crew_api.responses.")
        and call.kwargs["operation"] != "crew_api.responses.create"
    ]
    assert [(row["operation"], row["resources"]) for row in rows] == [
        ("crew_api.responses.resolve", f"response={answered['id']} found=yes"),
        ("crew_api.responses.input_items.resolve", f"response={answered['id']} found=yes"),
        ("crew_api.responses.resolve", "response=invalid found=no"),
        ("crew_api.responses.input_items.resolve", f"response={answered['id']} found=yes"),
    ]
    assert all(row["outcome"] == "allowed" for row in rows)
    assert "a private question" not in json.dumps(rows)


# -- the spec's marks ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/input_items"])
async def test_the_response_headers_left_out_are_the_ones_the_mark_names(client, suffix) -> None:
    operation = PUBLIC_SPEC["paths"]["/v1/responses/{response_id}" + suffix]["get"]
    mark = operation["x-crew-not-served-yet"]
    assert set(mark) == {"response-headers"}
    declared = set(operation["responses"]["200"]["headers"])
    _response, answered = await _ask(client)
    response, _body = await _get(client, f"{PATH}/{answered['id']}{suffix}")
    sent = {name for name in declared if name in response.headers}
    assert declared - sent == set(mark["response-headers"])
    assert mark["response-headers"] == sorted(mark["response-headers"])


# -- the invariant: whatever the record file holds -------------------------------------
#
# One kept turn, changed at every path in its file (a credential, another type, removed,
# and for a text every other text the record holds), read in three ways.
# The three reads of one changed file agree: all 404 with the file moved aside, or all
# 200 with no trace of the planted credential. A 200 stream is whole (numbered without
# a gap, opened, closed) and ends on the very Response the JSON read answers. Never a
# 500 and never a stream that stops short. A field added to the record is walked like
# the rest.

#: Where the planted credential may come back as it is: the fields ruled to go back as
#: the machine owner sent them. Only that owner makes a record and reads it, and
#: redacting a pair would break an id that looks like a key.
AS_SENT = ("metadata", "model", "ignored_parameters")
ENDED = {
    "completed": "response.completed",
    "failed": "response.failed",
    "incomplete": "response.incomplete",
    "cancelled": "response.incomplete",
}
READS = ("", "?stream=true", "/input_items")


def _paths(node: Any, at: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found = [at] if at else []
    if isinstance(node, dict):
        for key in node:
            found += _paths(node[key], (*at, key))
    elif isinstance(node, list):
        for index in range(len(node)):
            found += _paths(node[index], (*at, index))
    return found


def _another_type(value: Any) -> Any:
    if isinstance(value, dict):
        return []
    if isinstance(value, list):
        return {}
    return [value] if isinstance(value, str) else "seven"


def _at(record: dict[str, Any], path: tuple[Any, ...]) -> Any:
    node: Any = record
    for step in path:
        node = node[step]
    return node


def _changes(record: dict[str, Any], path: tuple[Any, ...]) -> list[tuple[str, str]]:
    """Every changed copy of *record* at *path*, as its name and the file's new text.

    A credential in its place, a value of another type, the value removed, and, for a
    text, the empty text, every other text the record holds and every status a
    Response can have: a value that is well formed and belongs somewhere else.
    """
    value = _at(record, path)
    new: list[tuple[str, Any]] = [("credential", SECRET), ("another type", _another_type(value))]
    if isinstance(value, str):
        held = [_at(record, at) for at in _paths(record)]
        elsewhere = {v for v in held if isinstance(v, str)} | {"", "in_progress", *ENDED}
        texts = sorted(elsewhere - {value})
        new += [(f"as {text!r}", text) for text in texts]
    made = []
    for how, put in new:
        changed = json.loads(json.dumps(record))
        _at(changed, path[:-1])[path[-1]] = put
        made.append((how, json.dumps(changed)))
    changed = json.loads(json.dumps(record))
    del _at(changed, path[:-1])[path[-1]]
    return [*made, ("removed", json.dumps(changed))]


def _stream_fault(raw: str, answered: Any) -> str:
    """Why the stream *raw* is not the whole stream of the Response *answered*, or ``""``."""
    try:
        events = _events(raw)
        for event in events:
            _valid("ResponseStreamEvent", event)
    except Exception as exc:  # any frame that is not an event of the contract
        return f"a frame is not a stream event ({type(exc).__name__})"
    if [event["sequence_number"] for event in events] != list(range(len(events))):
        return "the events are not numbered from 0 without a gap"
    if [event["type"] for event in events[:2]] != ["response.created", "response.in_progress"]:
        return "the stream does not open"
    last = events[-1]
    if last["type"] == "error":
        if answered["status"] != "failed" or last["message"] != answered["error"]["message"]:
            return "the error event is not the failure the Response reports"
        return ""
    if last["type"] != ENDED.get(answered["status"]):
        return "the stream does not close on the event of the Response's status"
    if last.get("response") != answered:
        return "the stream closes on another Response than the one read as JSON"
    carried = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    text = answered["output"][0]["content"][0]["text"] if answered["output"] else ""
    if carried != text:
        return "the deltas do not carry the Response's text"
    return ""


async def _broken_reads(client, record: dict[str, Any]) -> list[str]:
    """Every read of every changed copy of *record* that breaks the invariant."""
    directory = _store(client).directory
    path = directory / f"{record['id']}.json"
    broken = []
    for at in _paths(record):
        for how, changed in _changes(record, at):
            where = f"{'/'.join(map(str, at))} {how}"
            statuses, bodies = [], {}
            for read in READS:
                path.write_text(changed, encoding="utf-8")
                response = await client.get(f"{PATH}/{record['id']}{read}", headers=AS_OWNER)
                raw = bodies[read] = await response.text()
                statuses.append(response.status)
                aside = [p for p in directory.iterdir() if ".damaged-" in p.name]
                for moved in aside:
                    moved.unlink()
                if response.status == 404:
                    if path.exists() or len(aside) != 1 or '"not_found"' not in raw:
                        broken.append(f"{where}, GET {read}: 404 without the file moved aside")
                elif response.status != 200:
                    broken.append(f"{where}, GET {read}: {response.status}")
                elif aside or not path.exists():
                    broken.append(f"{where}, GET {read}: 200 and the file was moved")
                elif SECRET in raw and not (how == "credential" and set(at) & set(AS_SENT)):
                    broken.append(f"{where}, GET {read}: the credential came back")
            if len(set(statuses)) != 1:
                broken.append(f"{where}: the three reads disagree, {statuses}")
            elif statuses[0] == 200:
                fault = _stream_fault(bodies["?stream=true"], json.loads(bodies[""]))
                if fault:
                    broken.append(f"{where}, GET ?stream=true: {fault}")
    return broken


async def _kept_json(client, monkeypatch) -> str:
    _response, answered = await _ask(client, WALKED)
    return answered["id"]


async def _kept_stream(client, monkeypatch) -> str:
    _response, sent = await _ask_stream(client, {**WALKED, "stream": True})
    return sent[0]["response"]["id"]


async def _kept_error_stream(client, monkeypatch) -> str:
    client.turn.raises = RuntimeError("engine fell over")
    _response, sent = await _ask_stream(client, {**WALKED, "stream": True})
    assert sent[-1]["type"] == "error"
    return sent[0]["response"]["id"]


async def _kept_cut_short(client, monkeypatch) -> str:
    client.turn.release = asyncio.Event()  # never set: only the ceiling ends this turn
    ceiling = functools.partial(
        spawn_guarded_turn,
        timeout_secs=0.01,  # flake-ok: the ceiling under test; the turn waits on an event never set
        cancel_ack_timeout_secs=0.01,  # flake-ok: no engine acknowledges; the bound only ends the wait
    )
    monkeypatch.setattr(responses, "spawn_guarded_turn", ceiling)
    _response, answered = await _ask(client, WALKED)
    assert answered["incomplete_details"] == {"reason": "turn_time_limit"}
    return answered["id"]


WALKED = {**TALK, "metadata": {"trace": "t-1"}, "temperature": 0.1}
#: A kept turn of each kind, so every field a record can hold is on the walk: the
#: stream's deltas, the failure of a stream closed on an error event, the cut-short reason.
KINDS = {
    "answered as JSON": (_kept_json, {"text", "status", "id"}),
    "answered as a stream": (_kept_stream, {"deltas", "item_id"}),
    "a stream closed on an error event": (_kept_error_stream, {"error", "code", "message"}),
    "cut at the time limit": (_kept_cut_short, {"reason"}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_no_change_to_a_record_file_gets_a_credential_or_a_broken_answer_out(
    client, monkeypatch, kind
) -> None:
    monkeypatch.setattr(reads, "sel", lambda: client.audit)
    make, reached = KINDS[kind]
    response_id = await make(client, monkeypatch)
    path = _store(client).directory / f"{response_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    walked = {step for at in _paths(record) for step in at if isinstance(step, str)}
    assert {*reached, *AS_SENT} <= walked
    assert await _broken_reads(client, record) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("read", READS)
async def test_a_record_no_answer_can_be_built_from_is_moved_aside_and_not_found(
    client, monkeypatch, read
) -> None:
    def _cannot(*_args: Any, **_kwargs: Any) -> None:
        raise KeyError("a field the answer needs")

    _response, answered = await _ask(client)
    # Whatever fails while the answer is built, the read fails closed the same way.
    monkeypatch.setattr(reads, "_redacted", _cannot)
    response, body = await _get(client, f"{PATH}/{answered['id']}{read}")
    _contract_error(response, body, 404, "not_found")
    (name,) = _files(client)
    assert name.startswith(f"{answered['id']}.json.damaged-")
