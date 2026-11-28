"""``POST /v1/conversations``, ``GET /v1/conversations/{conversation_id}`` and a turn
that continues a conversation (``conversation`` and ``previous_response_id`` of
``POST /v1/responses``).

The routes run around the same stand-in for ``_run_chat`` as ``test_crew_api_responses.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from test_crew_api_responses import (
    AS_OWNER,
    ASK,
    CALLERS,
    MODEL,
    SECRET,
    SPEC_DIR,
    _ask,
    _contract_error,
    _no_slot_left,
    _valid,
)
from test_crew_api_responses import client as _responses_client

from kiro_crew import member_records
from kiro_crew.dashboard import crew_api_conversations as conversations
from kiro_crew.dashboard import crew_api_response_records as records
from kiro_crew.dashboard import crew_api_responses as responses
from kiro_crew.dashboard.crew_api_conversations import OWNER, Caller, ConversationStore
from kiro_crew.dashboard.crew_api_response_records import ResponseStore
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import await_until, default_timeout

client = _responses_client  # the route fixture of the POST /v1/responses tests
PATH = "/v1/conversations"
RESPONSES = "/v1/responses"

UNKNOWN = "conv_" + "0" * 32
#: Ids that are not this gateway's: each reads as a conversation that does not exist.
NOT_IDS = ["conv_1", "nope", "conv_" + "A" * 32, "conv_" + "0" * 33, "resp_" + "0" * 32]
DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(tmp_path, clock) -> ConversationStore:
    return ConversationStore(directory=tmp_path / "conversations", clock=clock)


def _files(directory: Path | None) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory and directory.exists() else []


# ── the record ───────────────────────────────────────────────────────────────


def test_a_conversation_is_kept_and_read_by_the_caller_that_made_it(store) -> None:
    record = store.create({"doc": "d42"}, OWNER)
    assert re.fullmatch(r"conv_[0-9a-f]{32}", record["id"])
    assert record["created_by_grant"] is None
    assert store.load(record["id"], OWNER) == record
    assert record["turns"] == [] and record["model"] is None
    assert record["namespace"] == records.NAMESPACE


def test_a_conversation_of_another_caller_reads_as_absent(store) -> None:
    owners = store.create({}, OWNER)
    grants = store.create({}, Caller("grant_a"))
    assert grants["created_by_grant"] == "grant_a"
    assert store.load(grants["id"], Caller("grant_a")) == grants
    # Neither the owner, nor another grant, nor a grant reading the owner's.
    assert store.load(grants["id"], OWNER) is None
    assert store.load(grants["id"], Caller("grant_b")) is None
    assert store.load(owners["id"], Caller("grant_a")) is None
    # And none of them can note a turn in it.
    assert store.turn_ended(grants["id"], OWNER, model="m", kept=None) is None
    assert store.load(grants["id"], Caller("grant_a")) == grants


@pytest.mark.parametrize("name", [*NOT_IDS, "../conv_" + "0" * 32, "", "conv_" + "0" * 32 + "/x"])
def test_an_id_of_another_shape_never_reaches_the_disk(store, monkeypatch, name) -> None:
    def _no_read(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("an id of another shape must not become a path")

    monkeypatch.setattr(conversations, "read_bytes_with_retry", _no_read)
    assert store.load(name, OWNER) is None
    assert store.turn_ended(name, OWNER, model="m", kept=None) is None


def test_a_conversation_is_kept_thirty_days_after_its_latest_turn(store, clock) -> None:
    assert conversations.RETENTION_SECS == 30 * DAY
    idle = store.create({}, OWNER)
    used = store.create({}, OWNER)
    clock.now += 29 * DAY
    assert store.turn_ended(used["id"], OWNER, model=MODEL, kept=None) is not None
    clock.now += DAY - 1
    assert store.load(idle["id"], OWNER) is not None
    clock.now += 1
    # Thirty days after it was made, with no turn in it.
    assert store.load(idle["id"], OWNER) is None
    assert _files(store.directory) == [used["id"] + ".json"]
    clock.now += 29 * DAY - 1
    assert store.load(used["id"], OWNER) is not None
    clock.now += 1
    assert store.load(used["id"], OWNER) is None
    assert _files(store.directory) == []


def test_the_sweep_removes_what_nobody_read_again(store, clock) -> None:
    old = store.create({}, OWNER)
    clock.now += 30 * DAY
    fresh = store.create({}, OWNER)  # a write sweeps
    assert _files(store.directory) == [fresh["id"] + ".json"]
    assert store.load(old["id"], OWNER) is None


@pytest.mark.parametrize(
    "damage",
    [
        lambda record: record.pop("turns"),
        lambda record: record.update(extra=1),
        lambda record: record.update(id=UNKNOWN),
        lambda record: record.update(turns=["resp_1"]),
        lambda record: record.update(turns=["resp_" + "0" * 32] * 2),
        lambda record: record.update(metadata={"k": 1}),
        lambda record: record.update(model=7),
        lambda record: record.update(created_by_grant=7),
        lambda record: record.update(updated_at="now"),
    ],
)
def test_a_record_that_cannot_be_believed_is_moved_aside(store, damage) -> None:
    record = store.create({}, OWNER)
    path = store.directory / (record["id"] + ".json")
    changed = json.loads(path.read_text(encoding="utf-8"))
    damage(changed)
    path.write_text(json.dumps(changed), encoding="utf-8")
    assert store.load(record["id"], OWNER) is None
    (kept,) = _files(store.directory)
    assert kept.startswith(record["id"] + ".json.damaged-")


def test_a_turn_whose_record_was_kept_is_listed_once(store, clock) -> None:
    record = store.create({}, OWNER)
    first, second = "resp_" + "1" * 32, "resp_" + "2" * 32
    clock.now += 5
    assert store.turn_ended(record["id"], OWNER, model=MODEL, kept=first)["turns"] == [first]
    assert store.turn_ended(record["id"], OWNER, model=MODEL, kept=first)["turns"] == [first]
    assert store.turn_ended(record["id"], OWNER, model="scout", kept=None)["turns"] == [first]
    noted = store.turn_ended(record["id"], OWNER, model="scout", kept=second)
    read = store.load(record["id"], OWNER)
    assert noted == read
    assert read["turns"] == [first, second]
    assert read["model"] == "scout"
    assert (read["created_at"], read["updated_at"]) == (record["created_at"], int(clock.now))


def test_a_conversation_runs_one_turn_at_a_time_and_a_restart_leaves_none_busy(
    store, clock
) -> None:
    record = store.create({}, OWNER)
    assert store.is_busy(record["id"]) is False
    assert store.claim(record["id"]) is True
    assert store.claim(record["id"]) is False
    assert store.is_busy(record["id"]) is True
    # The gateway's next run: the same directory, nothing taken.
    restarted = ConversationStore(directory=store.directory, clock=clock)
    assert restarted.is_busy(record["id"]) is False
    assert restarted.claim(record["id"]) is True
    store.release(record["id"])
    store.release(record["id"])  # twice changes nothing
    assert store.claim(record["id"]) is True


def test_the_records_live_in_the_directory_no_sandbox_reads(tmp_path) -> None:
    made = ConversationStore().create({}, OWNER)
    directory = member_records.records_dir(conversations.CONVERSATIONS_KIND)
    assert directory == tmp_path / member_records.MEMBER_RECORDS_LEAF / "crew-api-conversations"
    assert _files(directory) == [made["id"] + ".json"]


def test_a_record_too_large_to_read_back_is_not_written(store, monkeypatch) -> None:
    monkeypatch.setattr(conversations, "MAX_FILE_BYTES", 64)
    with pytest.raises(ValueError):
        store.create({"k": "v" * 64}, OWNER)
    assert _files(store.directory) == []


# ── the two routes ───────────────────────────────────────────────────────────


def _conversations(client) -> ConversationStore:
    return client.app[conversations.CONVERSATIONS_KEY]


def _responses(client) -> ResponseStore:
    return client.app[records.STORE_KEY]


async def _create(client, body: Any = None, headers: dict[str, str] | None = None):
    response = await client.post(
        PATH, json={} if body is None else body, headers=AS_OWNER if headers is None else headers
    )
    return response, await response.json()


async def _read(client, conversation_id: str, headers: dict[str, str] | None = None):
    response = await client.get(
        f"{PATH}/{conversation_id}", headers=AS_OWNER if headers is None else headers
    )
    return response, await response.json()


def _refusal(response, body: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status == status, body
    _valid("Error", body)
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers["X-Request-Id"]
    assert response.headers["Crew-API-Version"] == "1.0"
    return body["error"]


@pytest.mark.asyncio
async def test_the_owner_makes_a_conversation_and_reads_it(client, monkeypatch) -> None:
    monkeypatch.setattr(conversations, "sel", lambda: client.audit)
    response, made = await _create(client, {"metadata": {"doc_id": "d42"}})
    assert response.status == 200, made
    _valid("Conversation", made)
    assert re.fullmatch(r"conv_[0-9a-f]{32}", made["id"])
    assert made["object"] == "conversation"
    assert made["metadata"] == {"doc_id": "d42"}
    assert made["crew"] == {"title": None, "status": "idle"}
    assert response.headers["X-Request-Id"].startswith("req_")
    assert response.headers["Crew-API-Version"] == "1.0"
    response, read = await _read(client, made["id"])
    assert response.status == 200, read
    assert read == made
    client.audit.log_api_access.assert_any_call(
        caller="U_OWNER",
        operation="crew_api.conversations.create",
        outcome="allowed",
        source="crew_api",
        resources=f"conversation={made['id']}",
    )
    client.audit.log_api_access.assert_any_call(
        caller="U_OWNER",
        operation="crew_api.conversations.resolve",
        outcome="allowed",
        source="crew_api",
        resources=f"conversation={made['id']} found=yes",
    )


@pytest.mark.asyncio
async def test_what_the_spec_marks_not_served_yet_is_not_served(client) -> None:
    paths = yaml.safe_load((SPEC_DIR / "crew-public.yaml").read_text(encoding="utf-8"))["paths"]
    quota = ["X-Crew-Quota-Limit", "X-Crew-Quota-Remaining", "X-Crew-Quota-Reset"]
    assert paths[PATH]["post"]["x-crew-not-served-yet"] == {
        "request-headers": ["Idempotency-Key"],
        "response-headers": quota,
    }
    assert paths[PATH + "/{conversation_id}"]["get"]["x-crew-not-served-yet"] == {
        "response-headers": quota
    }
    # The same key twice makes two conversations: the header is not read.
    again = {**AS_OWNER, "Idempotency-Key": "one-key"}
    first = await client.post(PATH, json={}, headers=again)
    second = await client.post(PATH, json={}, headers=again)
    made, other = await first.json(), await second.json()
    assert made["id"] != other["id"]
    read = await client.get(f"{PATH}/{made['id']}", headers=AS_OWNER)
    for response in (first, second, read):
        assert response.status == 200
        assert not [name for name in quota if name in response.headers]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"items": []}, {"items": None}, {"metadata": None}])
async def test_a_conversation_needs_no_field(client, body) -> None:
    response, made = await _create(client, body)
    assert response.status == 200, made
    assert made["metadata"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        "text",
        {"items": [{"type": "message", "role": "user", "content": "hi"}]},
        {"items": "none"},
        {"metadata": ["a"]},
        {"metadata": {"k": 1}},
        {"metadata": {f"k{n}": "v" for n in range(conversations.METADATA_MAX_PAIRS + 1)}},
        {"metadata": {"k" * (conversations.METADATA_KEY_MAX + 1): "v"}},
        {"metadata": {"k": "v" * (conversations.METADATA_VALUE_MAX + 1)}},
    ],
)
async def test_a_malformed_conversation_is_refused_and_nothing_is_kept(client, body) -> None:
    response, answered = await _create(client, body)
    _refusal(response, answered, 400, "invalid_request")
    assert _files(_conversations(client).directory) == []


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_refused(client) -> None:
    response = await client.post(
        PATH, data=b"{", headers={**AS_OWNER, "Content-Type": "application/json"}
    )
    _refusal(response, await response.json(), 400, "invalid_request")


@pytest.mark.asyncio
async def test_a_conversation_that_cannot_be_written_is_unavailable(client, monkeypatch) -> None:
    def _full(_metadata: dict[str, str], _caller: Caller) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_conversations(client), "create", _full)
    response, answered = await _create(client)
    _refusal(response, answered, 503, "instance_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [UNKNOWN, *NOT_IDS])
async def test_an_id_that_names_no_conversation_is_one_not_found(client, monkeypatch, name) -> None:
    monkeypatch.setattr(conversations, "sel", lambda: client.audit)
    response, body = await _read(client, name)
    error = _refusal(response, body, 404, "not_found")
    assert error["message"] == "no such conversation, or it is no longer kept"
    shown = name if name == UNKNOWN else "invalid"
    client.audit.log_api_access.assert_any_call(
        caller="U_OWNER",
        operation="crew_api.conversations.resolve",
        outcome="allowed",
        source="crew_api",
        resources=f"conversation={shown} found=no",
    )


@pytest.mark.asyncio
async def test_a_conversation_whose_time_is_up_is_not_found(client, monkeypatch) -> None:
    _response, made = await _create(client)
    clock = _Clock()
    clock.now = made["created_at"] + conversations.RETENTION_SECS
    monkeypatch.setattr(_conversations(client), "clock", clock)
    response, body = await _read(client, made["id"])
    _refusal(response, body, 404, "not_found")


@pytest.mark.asyncio
async def test_records_that_cannot_be_read_are_unavailable(client, monkeypatch) -> None:
    _response, made = await _create(client)

    def _held(*_args: Any, **_kwargs: Any) -> bytes:
        raise PermissionError("held by another process")

    monkeypatch.setattr(conversations, "read_bytes_with_retry", _held)
    response, body = await _read(client, made["id"])
    _refusal(response, body, 503, "instance_unavailable")
    response, body = await _ask(client, {**ASK, "conversation": made["id"]})
    _contract_error(response, body, 503, "instance_unavailable")
    assert client.turn.entered == []
    assert _conversations(client).is_busy(made["id"]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_only_the_machine_owner_reaches_a_conversation(client, monkeypatch, caller) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _response, made = await _create(client)
    before = _files(_conversations(client).directory)

    def _no_read(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a refused caller must not reach the records")

    monkeypatch.setattr(_conversations(client), "load", _no_read)
    monkeypatch.setattr(_conversations(client), "create", _no_read)
    answers = []
    for name in (made["id"], UNKNOWN):
        response, body = await _read(client, name, headers=caller)
        _refusal(response, body, 403, "owner_only")
        body["error"].pop("request_id")
        answers.append(body)
    # A conversation that exists and one that does not read alike to such a caller.
    assert answers[0] == answers[1]
    response, body = await _create(client, headers=caller)
    _refusal(response, body, 403, "owner_only")
    response, body = await _ask(client, {**ASK, "conversation": made["id"]}, headers=caller)
    _contract_error(response, body, 403, "owner_only")
    assert _files(_conversations(client).directory) == before
    assert client.turn.entered == []


# ── a turn in a conversation ─────────────────────────────────────────────────


def _context(prompt: str) -> tuple[str, str]:
    """The context ahead of a prompt's own message, and that message."""
    context, _, turn = prompt.partition("--- USER MESSAGE BEGIN ---")
    return context, turn.partition("--- USER MESSAGE END ---")[0].strip()


@pytest.mark.asyncio
async def test_a_turn_with_no_conversation_starts_one(client) -> None:
    response, first = await _ask(client)
    assert response.status == 200, first
    _valid("Response", first)
    named = first["conversation"]["id"]
    _response, other = await _ask(client)
    assert other["conversation"]["id"] != named
    response, read = await _read(client, named)
    assert response.status == 200, read
    assert read["crew"] == {"title": None, "status": "idle", "model": MODEL}
    assert read["metadata"] == {}
    assert _conversations(client).load(named, OWNER)["turns"] == [first["id"]]
    # The turn reads back in the conversation it ran in.
    response = await client.get(f"{RESPONSES}/{first['id']}", headers=AS_OWNER)
    assert (await response.json())["conversation"] == {"id": named}


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["conversation", "previous_response_id", "both"])
async def test_a_later_turn_is_given_the_turns_before_it(client, by) -> None:
    _response, made = await _create(client)
    _response, first = await _ask(client, {**ASK, "conversation": made["id"]})
    assert first["conversation"] == {"id": made["id"]}
    assert client.turn.entered[0]["message"] == "hello"
    names = {
        "conversation": {"conversation": made["id"]},
        "previous_response_id": {"previous_response_id": first["id"]},
        "both": {"conversation": made["id"], "previous_response_id": first["id"]},
    }[by]
    client.turn.rows = [("chunk", "Second reply.", None)]
    response, second = await _ask(
        client, {"model": MODEL, "input": "and then?", "instructions": "be brief", **names}
    )
    assert response.status == 200, second
    _valid("Response", second)
    assert second["conversation"] == {"id": made["id"]}
    context, turn = _context(client.turn.entered[1]["message"])
    assert turn == "and then?"
    assert context.index("[SYSTEM] be brief") < context.index("[Previous user message] hello")
    assert context.index("[Previous user message] hello") < context.index(
        "[Previous assistant response] Done."
    )
    # A third turn is given both, in the order they ran, and no earlier instructions.
    _response, third = await _ask(
        client, {"model": MODEL, "input": "last", "previous_response_id": first["id"]}
    )
    context, turn = _context(client.turn.entered[2]["message"])
    assert turn == "last"
    assert "be brief" not in context
    assert [line for line in context.splitlines() if line.startswith("[Previous")] == [
        "[Previous user message] hello",
        "[Previous assistant response] Done.",
        "[Previous user message] and then?",
        "[Previous assistant response] Second reply.",
    ]
    assert _conversations(client).load(made["id"], OWNER)["turns"] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    _no_slot_left(client)


def test_earlier_messages_come_out_question_first_reply_second() -> None:
    ran = [
        {"role": "system", "content": "be brief"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2a"},
        {"role": "user", "content": "q2b"},
        {"role": "assistant", "content": "a2"},
        {"role": "assistant", "content": "a2 more"},
        {"role": "system", "content": "use metric units"},
        {"role": "user", "content": "q3"},
    ]
    arranged = responses._replies_after_the_next_question(ran)
    assert sorted(map(str, arranged)) == sorted(map(str, ran))
    context, _, turn = responses._flatten_messages(arranged).partition("--- USER MESSAGE BEGIN ---")
    assert context.splitlines()[1:-2] == [
        "[SYSTEM] be brief",
        "[SYSTEM] use metric units",
        "[Previous assistant response] a0",
        "[Previous user message] q1",
        "[Previous assistant response] a1",
        "[Previous user message] q2a",
        "[Previous user message] q2b",
        "[Previous assistant response] a2",
        "[Previous assistant response] a2 more",
    ]
    assert turn.startswith("\nq3\n")
    # Handed over as they ran, the same messages come out reply first.
    as_ran = responses._flatten_messages(ran).splitlines()
    assert as_ran.index("[Previous assistant response] a1") < as_ran.index(
        "[Previous user message] q1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", ["too many sessions", "engine not signed in"])
async def test_a_turn_refused_before_it_is_let_in_starts_no_conversation(
    client, monkeypatch, refused
) -> None:
    if refused == "too many sessions":
        monkeypatch.setattr(responses, "MAX_LIVE_SLOTS", 0)
    else:

        async def _not_signed_in(*_args: Any, **_kwargs: Any) -> responses.Refusal:
            return responses.Refusal(503, "instance_unavailable", "sign in first")

        monkeypatch.setattr(responses, "_engine_refusal", _not_signed_in)
    response, body = await _ask(client)
    status, code = (
        (429, "concurrency_limit")
        if refused == "too many sessions"
        else (503, "instance_unavailable")
    )
    _contract_error(response, body, status, code)
    assert client.turn.entered == []
    assert _files(_conversations(client).directory) == []
    assert _conversations(client)._busy == set()


@pytest.mark.asyncio
async def test_a_conversations_turns_are_kept_as_long_as_the_conversation(
    client, monkeypatch
) -> None:
    clock = _Clock()
    clock.now = time.time()
    monkeypatch.setattr(_conversations(client), "clock", clock)
    monkeypatch.setattr(_responses(client), "clock", clock)
    # A turn's ``created_at`` is the real time, days behind this clock, so a sweep
    # at a write would take the turn just written. The sweep is called below.
    monkeypatch.setattr(_responses(client), "sweep", lambda: 0)
    sweep = ResponseStore.sweep
    _response, first = await _ask(client, {"model": MODEL, "input": "day zero"})
    named = first["conversation"]["id"]
    by = {"conversation": named}
    clock.now += 20 * DAY
    _response, second = await _ask(client, {"model": MODEL, "input": "day twenty", **by})
    # Thirty-one days after it began, and used eleven days ago: it is continued, and
    # the turn it began with is given and read back.
    clock.now += 11 * DAY
    response, third = await _ask(client, {"model": MODEL, "input": "day thirty-one", **by})
    assert (response.status, third["status"]) == (200, "completed")
    context, _turn = _context(client.turn.entered[2]["message"])
    assert "[Previous user message] day zero" in context
    for turn in (first, second, third):
        response = await client.get(f"{RESPONSES}/{turn['id']}", headers=AS_OWNER)
        assert response.status == 200
        items = await client.get(f"{RESPONSES}/{turn['id']}/input_items", headers=AS_OWNER)
        assert items.status == 200
    # The sweep takes no turn of the conversation either.
    assert sweep(_responses(client)) == 0
    kept = _files(_responses(client).directory)
    assert {turn["id"] + ".json" for turn in (first, second, third)} == set(kept)
    # Thirty days after its latest turn the conversation is gone, and its turns with it.
    clock.now += 30 * DAY - 1
    response, _body = await _read(client, named)
    assert response.status == 200
    clock.now += 1
    response, body = await _read(client, named)
    _refusal(response, body, 404, "not_found")
    for turn in (first, second, third):
        response = await client.get(f"{RESPONSES}/{turn['id']}", headers=AS_OWNER)
        assert response.status == 404
        items = await client.get(f"{RESPONSES}/{turn['id']}/input_items", headers=AS_OWNER)
        assert items.status == 404
    response, body = await _ask(client, {"model": MODEL, "input": "too late", **by})
    _contract_error(response, body, 404, "not_found")
    response, body = await _ask(client, {**ASK, "previous_response_id": first["id"]})
    _contract_error(response, body, 404, "not_found")
    assert len(client.turn.entered) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["read", "sweep"])
@pytest.mark.parametrize("first_store", ["conversation", "response"])
async def test_a_continued_conversation_survives_its_retention_boundary(
    client, monkeypatch, clock, during, first_store
) -> None:
    clock.now = int(time.time())
    monkeypatch.setattr(_conversations(client), "clock", clock)
    monkeypatch.setattr(_responses(client), "clock", clock)
    # The route stamps a turn's ``created_at`` from its own ``time``: one clock for both.
    stamped = ManualClock(start=clock.now)
    stamped.install(monkeypatch, responses)
    response, first = await _ask(client)
    assert response.status == 200
    named = first["conversation"]["id"]
    clock.now += records.RETENTION_SECS - 1
    stamped.advance(records.RETENTION_SECS - 1)
    client.turn.started.clear()
    client.turn.release = asyncio.Event()
    running = asyncio.ensure_future(_ask(client, {**ASK, "conversation": named}))
    try:
        await await_until(client.turn.started, what="the continued turn to start")
        clock.now += 2
        if during == "sweep":
            stores = [_conversations(client), _responses(client)]
            if first_store == "response":
                stores.reverse()
            for kept_store in stores:
                assert kept_store.sweep() == 0
        if first_store == "response":
            response = await client.get(f"{RESPONSES}/{first['id']}", headers=AS_OWNER)
            assert response.status == 200, await response.json()
        response, read = await _read(client, named)
        assert response.status == 200, read
        assert read["crew"]["status"] == "running"
        response = await client.get(f"{RESPONSES}/{first['id']}", headers=AS_OWNER)
        assert response.status == 200, await response.json()
    finally:
        client.turn.release.set()
        response, second = await await_until(running, what="the continued turn's answer")
    assert (response.status, second["status"]) == (200, "completed")
    assert second["conversation"] == {"id": named}
    record = _conversations(client).load(named, OWNER)
    assert record["turns"] == [first["id"], second["id"]]
    assert record["updated_at"] == int(clock.now)
    for turn in (first, second):
        response = await client.get(f"{RESPONSES}/{turn['id']}", headers=AS_OWNER)
        assert response.status == 200, await response.json()
    clock.now += records.RETENTION_SECS
    assert _conversations(client).load(named, OWNER) is None
    assert all(_responses(client).load(turn["id"]) is None for turn in (first, second))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["conversation", "response"])
async def test_a_sweep_reads_current_file_times_not_cached_directory_entries(
    client, monkeypatch, clock, kind
) -> None:
    clock.now = int(time.time())
    conversation_store, response_store = _conversations(client), _responses(client)
    monkeypatch.setattr(conversation_store, "clock", clock)
    monkeypatch.setattr(response_store, "clock", clock)
    _response, renewed = await _ask(client)
    _response, expired = await _ask(client)
    subject = conversation_store if kind == "conversation" else response_store
    module = conversations if kind == "conversation" else records

    class CachedEntry:
        def __init__(self, entry) -> None:
            self.name, self.path = entry.name, entry.path
            self.cached = entry.stat(follow_symlinks=False)

        def stat(self, *, follow_symlinks: bool):
            assert follow_symlinks is False
            return self.cached

    with os.scandir(subject.directory) as entries:
        cached = [CachedEntry(entry) for entry in entries]
    clock.now += records.RETENTION_SECS - 1
    conversation_store.turn_started(
        renewed["conversation"]["id"], OWNER, keep_turns=response_store.keep_with
    )
    clock.now += 2

    def _listed(directory):
        assert directory == subject.directory
        return cached

    # Replace the module's binding, not the shared stdlib os object.
    monkeypatch.setattr(
        module, "os", SimpleNamespace(scandir=_listed, stat=os.stat, unlink=os.unlink)
    )
    assert all(entry.cached.st_mtime + records.RETENTION_SECS <= clock.now for entry in cached)
    assert await asyncio.to_thread(subject.sweep) == 1
    renewed_id = renewed["conversation"]["id"] if kind == "conversation" else renewed["id"]
    expired_id = expired["conversation"]["id"] if kind == "conversation" else expired["id"]
    assert subject._path(renewed_id).exists()
    assert not subject._path(expired_id).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["conversation", "response"])
@pytest.mark.parametrize("operation", ["load", "sweep"])
@pytest.mark.parametrize("renewal", ["start", "end"])
async def test_expiry_and_renewal_share_one_critical_section(
    client, monkeypatch, clock, kind, operation, renewal
) -> None:
    clock.now = int(time.time())
    conversation_store, response_store = _conversations(client), _responses(client)
    monkeypatch.setattr(conversation_store, "clock", clock)
    monkeypatch.setattr(response_store, "clock", clock)
    _response, first = await _ask(client)
    named = first["conversation"]["id"]
    clock.now += records.RETENTION_SECS - 1
    lock = RLock()
    monkeypatch.setattr(records, "RETENTION_LOCK", lock, raising=False)
    monkeypatch.setattr(conversations, "RETENTION_LOCK", lock, raising=False)
    subject = conversation_store if kind == "conversation" else response_store
    expired = subject._expired
    attempted = Event()
    acquired: list[bool] = []

    def _renew() -> None:
        got_lock = lock.acquire(blocking=False)
        acquired.append(got_lock)
        if got_lock:
            lock.release()
        attempted.set()
        if renewal == "start":
            renewed = conversation_store.turn_started(
                named, OWNER, keep_turns=response_store.keep_with
            )
        else:
            renewed = conversation_store.turn_ended(
                named, OWNER, model=MODEL, kept=None, keep_turns=response_store.keep_with
            )
        assert renewed is not None
        clock.now += 2

    def _interleaved() -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = None
            checked = False

            def _check(at: float) -> bool:
                nonlocal pending, checked
                if checked:
                    return expired(at)
                checked = True
                # The reader has its retention value. Probe lock ownership before
                # allowing renewal to run, so scheduling speed cannot decide the test.
                before = expired(at)
                pending = executor.submit(_renew)
                assert attempted.wait(default_timeout()), "renewal did not attempt the lock"
                if acquired == [True]:
                    pending.result(timeout=default_timeout())
                    return expired(at)
                return before

            monkeypatch.setattr(subject, "_expired", _check)
            try:
                if operation == "sweep":
                    assert subject.sweep() == 0
                elif kind == "conversation":
                    assert subject.load(named, OWNER) is not None
                else:
                    assert subject.load(first["id"]) is not None
            finally:
                monkeypatch.setattr(subject, "_expired", expired)
                if pending is not None:
                    pending.result(timeout=default_timeout())
        assert acquired == [False]
        assert conversation_store.load(named, OWNER) is not None
        assert response_store.load(first["id"]) is not None

    await asyncio.to_thread(_interleaved)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load", "sweep"])
async def test_admission_protects_listed_turns_before_keep_with(
    client, monkeypatch, clock, operation
) -> None:
    clock.now = int(time.time())
    conversation_store, response_store = _conversations(client), _responses(client)
    monkeypatch.setattr(conversation_store, "clock", clock)
    monkeypatch.setattr(response_store, "clock", clock)
    _response, first = await _ask(client)
    named = first["conversation"]["id"]
    clock.now += records.RETENTION_SECS - 1
    lock = RLock()
    monkeypatch.setattr(records, "RETENTION_LOCK", lock, raising=False)
    monkeypatch.setattr(conversations, "RETENTION_LOCK", lock, raising=False)
    attempted = Event()
    acquired: list[bool] = []

    def _expire():
        got_lock = lock.acquire(blocking=False)
        acquired.append(got_lock)
        if got_lock:
            lock.release()
        attempted.set()
        if operation == "sweep":
            return response_store.sweep()
        return response_store.load(first["id"])

    def _interleaved() -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = None

            def _keep(turns: list[str], at: float) -> None:
                nonlocal pending
                # Admission checked the conversation; its turns still have the old time.
                clock.now += 2
                pending = executor.submit(_expire)
                assert attempted.wait(default_timeout()), "expiry did not attempt the lock"
                if acquired == [True]:
                    pending.result(timeout=default_timeout())
                response_store.keep_with(turns, at)

            renewed = conversation_store.turn_started(named, OWNER, keep_turns=_keep)
            assert renewed is not None
            assert pending is not None
            result = pending.result(timeout=default_timeout())
        assert result == 0 if operation == "sweep" else result is not None
        assert acquired == [False]
        assert conversation_store.load(named, OWNER)["turns"] == [first["id"]]
        assert response_store.load(first["id"]) is not None

    await asyncio.to_thread(_interleaved)


@pytest.mark.asyncio
async def test_a_turn_that_ran_in_no_conversation_is_kept_from_its_own_start(
    client, monkeypatch
) -> None:
    store = _responses(client)
    _response, answered = await _ask(client)
    record = store.load(answered["id"])
    record["response"].pop("conversation")
    store.save(record)
    path = store.directory / (answered["id"] + ".json")
    later = record["created_at"] + 20 * DAY
    os.utime(path, (later, later))
    clock = _Clock()
    clock.now = record["created_at"] + records.RETENTION_SECS - 1
    monkeypatch.setattr(store, "clock", clock)
    assert store.load(answered["id"]) is not None
    clock.now += 1
    # Its file's time is not what such a turn is kept by.
    assert store.load(answered["id"]) is None


@pytest.mark.asyncio
async def test_keeping_a_turn_with_its_conversation_only_moves_its_time_later(client) -> None:
    store = _responses(client)
    _response, answered = await _ask(client)
    path = store.directory / (answered["id"] + ".json")
    at = path.stat().st_mtime
    store.keep_with([answered["id"], "resp_" + "0" * 32, "../nope"], at - 5 * DAY)
    assert path.stat().st_mtime == at
    store.keep_with([answered["id"]], at + 5 * DAY)
    assert path.stat().st_mtime == at + 5 * DAY


@pytest.mark.asyncio
async def test_a_time_that_cannot_be_set_is_told_to_the_caller(client, monkeypatch) -> None:
    store = _responses(client)
    _response, answered = await _ask(client)

    def _refused(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(records.os, "utime", _refused)
    with pytest.raises(PermissionError):
        store.keep_with([answered["id"]], time.time() + 5 * DAY)


def test_admission_keeps_turns_before_renewing_the_conversation(store, clock) -> None:
    record = store.create({}, OWNER)
    turn = "resp_" + "1" * 32
    store.turn_ended(record["id"], OWNER, model=MODEL, kept=turn)
    clock.now += records.RETENTION_SECS - 1
    seen: list[Any] = []

    def _keep(turns: list[str], at: float) -> None:
        before = store.load(record["id"], OWNER)
        seen.append((list(turns), at, before["updated_at"]))

    renewed = store.turn_started(record["id"], OWNER, keep_turns=_keep)
    assert seen == [([turn], int(clock.now), record["updated_at"])]
    assert renewed["updated_at"] == int(clock.now)
    assert renewed["turns"] == [turn]
    assert renewed["model"] == MODEL


@pytest.mark.asyncio
async def test_a_continuation_is_not_run_when_admission_cannot_renew_retention(
    client, monkeypatch, clock
) -> None:
    clock.now = int(time.time())
    monkeypatch.setattr(_conversations(client), "clock", clock)
    _response, first = await _ask(client)
    named = first["conversation"]["id"]
    before = _conversations(client).load(named, OWNER)
    clock.now += records.RETENTION_SECS - 1

    def _fails(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(_responses(client), "keep_with", _fails)
    response, body = await _ask(client, {**ASK, "conversation": named})
    _contract_error(response, body, 503, "instance_unavailable")
    assert len(client.turn.entered) == 1
    assert _conversations(client).load(named, OWNER) == before
    assert not _conversations(client).is_busy(named)
    _no_slot_left(client)


def test_the_turns_are_kept_before_the_conversation_says_so(store, clock) -> None:
    record = store.create({}, OWNER)
    turn, later = "resp_" + "1" * 32, "resp_" + "2" * 32
    clock.now += 5
    seen: list[Any] = []

    def _keep(turns: list[str], at: float) -> None:
        seen.append((list(turns), at, store.load(record["id"], OWNER)["turns"]))

    store.turn_ended(record["id"], OWNER, model=MODEL, kept=turn, keep_turns=_keep)
    # Called with what the record will say, while the record still says the old.
    assert seen == [([turn], int(clock.now), [])]

    def _fails(_turns: list[str], _at: float) -> None:
        raise OSError("read-only")

    clock.now += 5
    with pytest.raises(OSError):
        store.turn_ended(record["id"], OWNER, model=MODEL, kept=later, keep_turns=_fails)
    read = store.load(record["id"], OWNER)
    assert (read["turns"], read["updated_at"]) == ([turn], int(clock.now) - 5)


@pytest.mark.asyncio
async def test_a_turn_is_not_listed_when_the_turns_before_it_cannot_be_kept_longer(
    client, monkeypatch
) -> None:
    _response, first = await _ask(client)

    keep_with = _responses(client).keep_with
    calls = 0

    def _fails_at_completion(turns: list[str], at: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            keep_with(turns, at)
        else:
            raise OSError("read-only")

    monkeypatch.setattr(_responses(client), "keep_with", _fails_at_completion)
    response, second = await _ask(client, {**ASK, "previous_response_id": first["id"]})
    assert (response.status, second["status"]) == (200, "completed")
    assert calls == 2
    record = _conversations(client).load(first["conversation"]["id"], OWNER)
    assert record["turns"] == [first["id"]]
    assert _responses(client).load(second["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("left", ["not listed", "conversation gone"])
async def test_a_record_its_conversation_does_not_list_is_not_read_and_not_named(
    client, left
) -> None:
    """What a gateway that stops between the turn's record and the conversation's
    leaves: the turn's record, kept, and a conversation that does not list it."""
    _response, first = await _ask(client)
    _response, second = await _ask(client, {**ASK, "previous_response_id": first["id"]})
    reads = [f"{RESPONSES}/{second['id']}", f"{RESPONSES}/{second['id']}/input_items"]
    for path in reads:
        assert (await client.get(path, headers=AS_OWNER)).status == 200
    store = _conversations(client)
    record = store.load(first["conversation"]["id"], OWNER)
    if left == "not listed":
        record["turns"].remove(second["id"])
        store._write(record)
    else:
        store._path(record["id"]).unlink()
    assert _responses(client).load(second["id"]) is not None
    for path in reads:
        assert (await client.get(path, headers=AS_OWNER)).status == 404
    response, body = await _ask(client, {**ASK, "previous_response_id": second["id"]})
    _contract_error(response, body, 404, "not_found")
    assert len(client.turn.entered) == 2


@pytest.mark.asyncio
async def test_a_turn_is_given_what_the_record_keeps_of_an_earlier_one(client) -> None:
    client.turn.rows = [("chunk", f"the key is {SECRET}", None)]
    _response, first = await _ask(client, {"model": MODEL, "input": f"keep {SECRET}"})
    await _ask(client, {"model": MODEL, "input": "use it", "previous_response_id": first["id"]})
    again = client.turn.entered[1]["message"]
    assert SECRET not in again
    assert responses._redact(f"keep {SECRET}") in again


@pytest.mark.asyncio
async def test_a_conversation_is_never_given_a_turn_of_another(client) -> None:
    _response, one = await _ask(client, {"model": MODEL, "input": "apples"})
    _response, other = await _ask(client, {"model": MODEL, "input": "pears"})
    await _ask(client, {"model": MODEL, "input": "more", "previous_response_id": one["id"]})
    context, _turn = _context(client.turn.entered[2]["message"])
    assert "apples" in context and "pears" not in context
    # A record that lists another conversation's turn: refused, and no turn runs.
    store = _conversations(client)
    record = store.load(one["conversation"]["id"], OWNER)
    record["turns"].append(other["id"])
    store._write(record)
    response, body = await _ask(
        client, {"model": MODEL, "input": "again", "conversation": one["conversation"]["id"]}
    )
    _contract_error(response, body, 500, "internal_error")
    assert len(client.turn.entered) == 3
    assert store.is_busy(one["conversation"]["id"]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [UNKNOWN, *NOT_IDS])
async def test_a_turn_in_a_conversation_that_does_not_exist_is_not_run(client, name) -> None:
    response, body = await _ask(client, {**ASK, "conversation": name})
    error = _contract_error(response, body, 404, "not_found")
    assert error["message"] == "no such conversation, or it is no longer kept"
    assert client.turn.entered == []
    assert _files(_responses(client).directory) == []
    assert _conversations(client).is_busy(name) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["resp_" + "0" * 32, "resp_1", "nope", UNKNOWN])
async def test_a_previous_response_that_does_not_exist_is_not_found(client, name) -> None:
    response, body = await _ask(client, {**ASK, "previous_response_id": name})
    error = _contract_error(response, body, 404, "not_found")
    assert error["message"] == "no such response, or it is no longer kept"
    assert client.turn.entered == []
    assert _files(_conversations(client).directory) == []


@pytest.mark.asyncio
async def test_a_previous_response_kept_before_conversations_cannot_be_continued(
    client,
) -> None:
    _response, old = await _ask(client)
    store = _responses(client)
    record = store.load(old["id"])
    record["response"].pop("conversation")
    store.save(record)
    # Still read back, as the build that kept it answered it.
    response = await client.get(f"{RESPONSES}/{old['id']}", headers=AS_OWNER)
    assert response.status == 200
    assert "conversation" not in await response.json()
    response, body = await _ask(client, {**ASK, "previous_response_id": old["id"]})
    _contract_error(response, body, 400, "invalid_request")
    assert len(client.turn.entered) == 1


@pytest.mark.asyncio
async def test_two_names_for_different_conversations_are_refused(client) -> None:
    _response, one = await _ask(client)
    _response, other = await _create(client)
    response, body = await _ask(
        client, {**ASK, "conversation": other["id"], "previous_response_id": one["id"]}
    )
    _contract_error(response, body, 400, "invalid_request")
    assert len(client.turn.entered) == 1
    assert _conversations(client).is_busy(other["id"]) is False
    assert _conversations(client).is_busy(one["conversation"]["id"]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["conversation", "previous_response_id"])
async def test_a_conversation_runs_one_turn_at_a_time(client, by) -> None:
    _response, made = await _create(client)
    client.turn.release = asyncio.Event()
    running = asyncio.ensure_future(_ask(client, {**ASK, "conversation": made["id"]}))
    await await_until(client.turn.started, what="the first turn to start")
    response, read = await _read(client, made["id"])
    assert read["crew"]["status"] == "running"
    (live,) = _responses(client).live
    names = {"conversation": made["id"]} if by == "conversation" else {"previous_response_id": live}
    response, body = await _ask(client, {**ASK, **names})
    error = _contract_error(response, body, 409, "response_in_progress")
    assert error["type"] == "conflict_error"
    assert len(client.turn.entered) == 1
    client.turn.release.set()
    _response, first = await await_until(running, what="the first turn's answer")
    assert first["status"] == "completed"
    _response, read = await _read(client, made["id"])
    assert read["crew"]["status"] == "idle"
    # Free again once the turn ended: the next one runs.
    response, second = await _ask(client, {**ASK, "conversation": made["id"]})
    assert (response.status, second["status"]) == (200, "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["raises", "fails", "cancelled", "unanswerable", "too large"])
async def test_no_ending_leaves_a_conversation_taken(client, monkeypatch, ending) -> None:
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    store = _conversations(client)
    answer, line = responses._answer, responses._MAX_PROMPT_BYTES
    if ending == "raises":
        client.turn.raises = RuntimeError("the engine broke")
    elif ending == "fails":
        client.turn.rows = [("error", "backend failed", None)]
    elif ending == "cancelled":

        async def _cancelled(*_args: Any, **_kwargs: Any) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(responses, "_answer", _cancelled)
    elif ending == "unanswerable":

        async def _broken(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the reader broke")

        monkeypatch.setattr(responses, "_answer", _broken)
    else:
        monkeypatch.setattr(responses, "_MAX_PROMPT_BYTES", 8)
    try:
        response, body = await _ask(client, {**ASK, "conversation": made["id"]})
    except Exception:  # the handler's own failure is the server's 500 or a lost connection
        response, body = None, None
    if ending == "raises":
        _contract_error(response, body, 500, "internal_error")
    elif ending == "fails":
        assert body["status"] == "failed"
    elif ending == "too large":
        _contract_error(response, body, 413, "payload_too_large")
    assert store.is_busy(made["id"]) is False
    _no_slot_left(client)
    # The next turn in it runs: nothing answers 409 for a turn that is gone.
    client.turn.raises = None
    client.turn.rows = [("chunk", "Back.", None)]
    monkeypatch.setattr(responses, "_answer", answer)
    monkeypatch.setattr(responses, "_MAX_PROMPT_BYTES", line)
    response, after = await _ask(client, {**ASK, "conversation": made["id"]})
    assert (response.status, after["status"]) == (200, "completed")


@pytest.mark.asyncio
async def test_only_a_turn_that_completed_is_given_to_a_later_one(client) -> None:
    _response, made = await _create(client)
    client.turn.rows = [("error", "backend failed", None)]
    _response, failed = await _ask(
        client, {"model": MODEL, "input": "lost question", "conversation": made["id"]}
    )
    assert failed["status"] == "failed"
    client.turn.rows = [("chunk", "Fine.", None)]
    _response, kept = await _ask(
        client, {"model": MODEL, "input": "kept question", "conversation": made["id"]}
    )
    await _ask(client, {"model": MODEL, "input": "now", "conversation": made["id"]})
    context, _turn = _context(client.turn.entered[2]["message"])
    assert "kept question" in context and "Fine." in context
    assert "lost question" not in context and "backend failed" not in context
    # The failed turn is listed, so it is kept with the conversation, and it is
    # not given to a later turn.
    listed = _conversations(client).load(made["id"], OWNER)["turns"]
    assert listed[:2] == [failed["id"], kept["id"]] and len(listed) == 3
    # The failed turn still reads back, in its conversation.
    response = await client.get(f"{RESPONSES}/{failed['id']}", headers=AS_OWNER)
    assert (await response.json())["conversation"] == {"id": made["id"]}


@pytest.mark.asyncio
async def test_earlier_turns_and_the_input_fit_one_prompt_or_the_turn_is_refused(
    client, monkeypatch
) -> None:
    _response, first = await _ask(client, {"model": MODEL, "input": "a" * 40})
    room = len(client.turn.entered[0]["message"].encode()) + 400
    monkeypatch.setattr(responses, "_MAX_PROMPT_BYTES", room)
    by = {"previous_response_id": first["id"]}
    response, body = await _ask(client, {"model": MODEL, "input": "b" * 40, **by})
    assert (response.status, body["status"]) == (200, "completed")
    exact = len(client.turn.entered[1]["message"].encode())
    # The third turn's prompt holds the second's two messages as well: over the line.
    monkeypatch.setattr(responses, "_MAX_PROMPT_BYTES", exact)
    response, body = await _ask(client, {"model": MODEL, "input": "b" * 40, **by})
    error = _contract_error(response, body, 413, "payload_too_large")
    assert "start a new conversation" in error["message"]
    assert len(client.turn.entered) == 2
    # The same input in a conversation of its own is within the line.
    response, body = await _ask(client, {"model": MODEL, "input": "b" * 40})
    assert (response.status, body["status"]) == (200, "completed")


@pytest.mark.asyncio
async def test_a_prompt_exactly_at_the_line_is_run(client, monkeypatch) -> None:
    _response, first = await _ask(client, {"model": MODEL, "input": "a" * 40})
    by = {"previous_response_id": first["id"]}
    await _ask(client, {"model": MODEL, "input": "b" * 40, **by})
    # What the next turn's prompt will be: the same turns, and an input as long.
    monkeypatch.setattr(responses, "_MAX_PROMPT_BYTES", 10**6)
    _response, probe = await _ask(client, {"model": MODEL, "input": "c" * 40})
    by_probe = {"previous_response_id": probe["id"]}
    await _ask(client, {"model": MODEL, "input": "d" * 40, **by_probe})
    size = len(client.turn.entered[-1]["message"].encode())
    monkeypatch.setattr(responses, "_MAX_PROMPT_BYTES", size)
    _response, one = await _ask(client, {"model": MODEL, "input": "e" * 40})
    response, body = await _ask(
        client, {"model": MODEL, "input": "f" * 40, "previous_response_id": one["id"]}
    )
    assert (response.status, body["status"]) == (200, "completed")
    monkeypatch.setattr(responses, "_MAX_PROMPT_BYTES", size - 1)
    _response, two = await _ask(client, {"model": MODEL, "input": "g" * 40})
    response, body = await _ask(
        client, {"model": MODEL, "input": "h" * 40, "previous_response_id": two["id"]}
    )
    _contract_error(response, body, 413, "payload_too_large")


@pytest.mark.asyncio
async def test_a_request_too_large_to_keep_is_refused_in_a_conversation_only(
    client, monkeypatch
) -> None:
    _response, made = await _create(client)
    monkeypatch.setattr(responses, "MAX_REQUEST_BYTES", 64)
    large = {"model": MODEL, "input": "x" * 65}
    response, body = await _ask(client, {**large, "conversation": made["id"]})
    _contract_error(response, body, 413, "payload_too_large")
    assert client.turn.entered == []
    assert _conversations(client).is_busy(made["id"]) is False
    # With no conversation named it is run as ever, and not kept. It starts no
    # conversation: one could not hold it, and a later turn would run without it.
    response, body = await _ask(client, large)
    assert (response.status, body["status"]) == (200, "completed")
    assert "conversation" not in body
    assert len(_files(_conversations(client).directory)) == 1
    response, body = await _ask(client, {**ASK, "previous_response_id": body["id"]})
    _contract_error(response, body, 404, "not_found")


@pytest.mark.asyncio
async def test_every_turn_is_judged_by_its_own_model(client, monkeypatch) -> None:
    _response, first = await _ask(client)
    by = {"previous_response_id": first["id"]}
    # Another model the caller can use: the conversation follows.
    response, second = await _ask(client, {"model": "scout", "input": "hi", **by})
    assert (response.status, second["model"]) == (200, "scout")
    assert client.turn.entered[1]["slot"].agent == "scout"
    _response, read = await _read(client, first["conversation"]["id"])
    assert read["crew"]["model"] == "scout"

    async def _fewer() -> list[dict[str, Any]]:
        return [{"id": MODEL}]

    # The conversation's latest model is not one the caller can use now.
    monkeypatch.setattr(responses, "usable_models_off_loop", _fewer)
    response, body = await _ask(client, {"model": "scout", "input": "hi", **by})
    _contract_error(response, body, 404, "model_not_found")
    assert len(client.turn.entered) == 2
    assert _conversations(client).is_busy(first["conversation"]["id"]) is False
    # What the record says its model is allows nothing.
    record = _conversations(client).load(first["conversation"]["id"], OWNER)
    assert record["model"] == "scout"
    response, body = await _ask(client, {"model": "scout", "input": "hi", **by})
    _contract_error(response, body, 404, "model_not_found")


@pytest.mark.asyncio
async def test_a_conversation_with_a_turn_no_longer_kept_cannot_be_continued(
    client, monkeypatch
) -> None:
    _response, first = await _ask(client)
    _response, second = await _ask(client, {**ASK, "previous_response_id": first["id"]})
    path = _responses(client).directory / (first["id"] + ".json")
    path.unlink()
    response, body = await _ask(client, {**ASK, "previous_response_id": second["id"]})
    error = _contract_error(response, body, 404, "not_found")
    assert "cannot be continued" in error["message"]
    assert len(client.turn.entered) == 2
    assert _conversations(client).is_busy(first["conversation"]["id"]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_a_conversation_that_cannot_be_recorded_does_not_change_the_answer(
    client, monkeypatch, stream
) -> None:
    def _full(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    store = _conversations(client)
    create = store.create
    monkeypatch.setattr(store, "create", _full)
    response = await client.post(RESPONSES, json={**ASK, "stream": stream}, headers=AS_OWNER)
    assert response.status == 200
    text = await response.text()
    assert "conv_" not in text
    assert '"completed"' in text
    # Nor does a conversation that cannot be updated when its turn ends. The turn is
    # then not kept: it is not read back and not named while its conversation lacks it.
    before = _files(_responses(client).directory)
    monkeypatch.setattr(store, "create", create)
    monkeypatch.setattr(store, "turn_ended", _full)
    response = await client.post(RESPONSES, json={**ASK, "stream": stream}, headers=AS_OWNER)
    assert response.status == 200
    text = await response.text()
    assert '"completed"' in text and "conv_" in text
    assert _conversations(client)._busy == set()
    assert _files(_responses(client).directory) == before
    unlisted = re.search(r"resp_[0-9a-f]{32}", text).group(0)
    response = await client.get(f"{RESPONSES}/{unlisted}", headers=AS_OWNER)
    assert response.status == 404
    response, body = await _ask(client, {**ASK, "previous_response_id": unlisted})
    _contract_error(response, body, 404, "not_found")


@pytest.mark.asyncio
async def test_a_streamed_turn_names_its_conversation_in_every_response_it_sends(client) -> None:
    _response, made = await _create(client)
    response = await client.post(
        RESPONSES, json={**ASK, "stream": True, "conversation": made["id"]}, headers=AS_OWNER
    )
    carried = [
        json.loads(line[len("data: ") :])
        for line in (await response.text()).splitlines()
        if line.startswith("data: ")
    ]
    with_response = [event["response"] for event in carried if "response" in event]
    assert len(with_response) >= 3
    assert all(sent["conversation"] == {"id": made["id"]} for sent in with_response)
    listed = _conversations(client).load(made["id"], OWNER)["turns"]
    assert listed == [with_response[-1]["id"]]
    # And a turn follows a streamed one as it follows any other.
    await _ask(client, {"model": MODEL, "input": "next", "conversation": made["id"]})
    context, _turn = _context(client.turn.entered[1]["message"])
    assert "[Previous assistant response] Done." in context


# ── what a response record holds of its conversation ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "named",
    [{"id": "conv_1"}, {"id": UNKNOWN, "extra": 1}, UNKNOWN, None, {"id": 7}, {}],
)
async def test_a_response_record_with_another_conversation_shape_is_not_believed(
    client, named
) -> None:
    _response, answered = await _ask(client)
    store = _responses(client)
    path = store.directory / (answered["id"] + ".json")
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["response"]["conversation"] = named
    path.write_text(json.dumps(changed), encoding="utf-8")
    assert store.load(answered["id"]) is None
    response, body = await _ask(client, {**ASK, "previous_response_id": answered["id"]})
    _contract_error(response, body, 404, "not_found")
    assert len(client.turn.entered) == 1
