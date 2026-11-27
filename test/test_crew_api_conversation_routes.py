"""``GET /v1/conversations``, ``GET /v1/conversations/{conversation_id}/items`` and the
``POST`` and ``DELETE`` of ``/v1/conversations/{conversation_id}``.

The routes run around the same stand-in for ``_run_chat`` as ``test_crew_api_responses.py``.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
import yaml
from test_crew_api_conversations import (
    NOT_IDS,
    PATH,
    RESPONSES,
    UNKNOWN,
    _conversations,
    _create,
    _files,
    _read,
    _refusal,
    _responses,
)
from test_crew_api_responses import AS_OWNER, ASK, CALLERS, MODEL, SPEC_DIR, _ask, _valid
from test_crew_api_responses import client as _responses_client

from kiro_crew.dashboard import crew_api_conversation_lists as lists
from kiro_crew.dashboard import crew_api_conversations as conversations
from kiro_crew.dashboard import crew_api_response_records as records
from kiro_crew.dashboard.crew_api_conversations import OWNER
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import await_until, default_timeout

client = _responses_client  # the route fixture of the POST /v1/responses tests


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


OPERATIONS = yaml.safe_load((SPEC_DIR / "crew-public.yaml").read_text(encoding="utf-8"))["paths"]
QUOTA = ["X-Crew-Quota-Limit", "X-Crew-Quota-Remaining", "X-Crew-Quota-Reset"]


async def _get(client, path: str, headers: dict[str, str] | None = None, **query: Any):
    response = await client.get(
        path,
        params={name: str(value) for name, value in query.items()},
        headers=AS_OWNER if headers is None else headers,
    )
    return response, await response.json()


async def _update(client, conversation_id: str, body: Any, headers: dict[str, str] | None = None):
    response = await client.post(
        f"{PATH}/{conversation_id}", json=body, headers=AS_OWNER if headers is None else headers
    )
    return response, await response.json()


async def _delete(client, conversation_id: str, headers: dict[str, str] | None = None):
    response = await client.delete(
        f"{PATH}/{conversation_id}", headers=AS_OWNER if headers is None else headers
    )
    return response, await response.json()


def _documented(response, method: str, path: str) -> None:
    assert str(response.status) in OPERATIONS[path][method]["responses"], response.status
    assert response.headers["Crew-API-Version"] == "1.0"
    assert response.headers["X-Request-Id"]


async def _spaced(client, monkeypatch, count: int) -> list[str]:
    """*count* conversations, each made one second after the one before it."""
    store = _conversations(client)
    now = [1_800_000_000.0]
    monkeypatch.setattr(store, "clock", lambda: now[0])
    made = []
    for _ in range(count):
        made.append(store.create({}, OWNER)["id"])
        now[0] += 1
    return made


def _texts(items: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(item["role"], item["status"], item["content"][0]["text"]) for item in items]


# ── the list ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_owner_lists_conversations_newest_first(client, monkeypatch) -> None:
    oldest, middle, newest = await _spaced(client, monkeypatch, 3)
    response, body = await _get(client, PATH)
    assert response.status == 200, body
    _documented(response, "get", PATH)
    _valid("ConversationList", body)
    assert [row["id"] for row in body["data"]] == [newest, middle, oldest]
    assert (body["first_id"], body["last_id"], body["has_more"]) == (newest, oldest, False)
    assert body["data"][0] == (await _read(client, newest))[1]
    response, body = await _get(client, PATH, order="asc")
    assert [row["id"] for row in body["data"]] == [oldest, middle, newest]
    for sent in QUOTA:
        assert sent not in response.headers
    assert OPERATIONS[PATH]["get"]["x-crew-not-served-yet"] == {"response-headers": QUOTA}


@pytest.mark.asyncio
async def test_no_conversation_is_an_empty_list(client) -> None:
    response, body = await _get(client, PATH)
    assert response.status == 200
    _valid("ConversationList", body)
    assert body == {
        "object": "list",
        "data": [],
        "first_id": None,
        "last_id": None,
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_the_list_pages_through_every_conversation_once(client, monkeypatch) -> None:
    made = await _spaced(client, monkeypatch, 5)
    seen: list[str] = []
    after = None
    for _ in range(5):
        query = {"limit": 2} if after is None else {"limit": 2, "after": after}
        _response, body = await _get(client, PATH, **query)
        seen += [row["id"] for row in body["data"]]
        after = body["last_id"]
        if not body["has_more"]:
            break
    assert seen == made[::-1]
    _response, body = await _get(client, PATH, order="asc", after=made[3])
    assert [row["id"] for row in body["data"]] == [made[4]]


@pytest.mark.asyncio
async def test_conversations_made_in_one_second_are_ordered_by_id(client, monkeypatch) -> None:
    store = _conversations(client)
    monkeypatch.setattr(store, "clock", lambda: 1_800_000_000.0)
    made = sorted(store.create({}, OWNER)["id"] for _ in range(4))
    _response, body = await _get(client, PATH, order="asc")
    assert [row["id"] for row in body["data"]] == made


@pytest.mark.asyncio
async def test_a_conversation_a_turn_started_for_itself_is_listed(client) -> None:
    _response, answered = await _ask(client)
    _response, body = await _get(client, PATH)
    (row,) = body["data"]
    assert row["id"] == answered["conversation"]["id"]
    assert row["crew"] == {"title": None, "status": "idle", "model": MODEL}


@pytest.mark.asyncio
async def test_a_conversation_keeps_its_place_while_turns_run_in_it(client, monkeypatch) -> None:
    oldest, _middle, newest = await _spaced(client, monkeypatch, 3)
    before = [row["id"] for row in (await _get(client, PATH))[1]["data"]]
    _response, answered = await _ask(client, {**ASK, "conversation": oldest})
    assert answered["status"] == "completed"
    _response, body = await _get(client, PATH)
    assert [row["id"] for row in body["data"]] == before
    assert body["data"][-1]["crew"]["model"] == MODEL and body["data"][0]["id"] == newest


@pytest.mark.asyncio
async def test_the_list_holds_only_what_is_the_callers_and_still_kept(client, monkeypatch) -> None:
    store = _conversations(client)
    now = [1_800_000_000.0]
    monkeypatch.setattr(store, "clock", lambda: now[0])
    theirs = store.create({}, conversations.Caller("grant_1"))["id"]
    old = store.create({}, OWNER)["id"]
    now[0] += records.RETENTION_SECS - 1
    kept = store.create({}, OWNER)["id"]
    now[0] += 1
    (store.directory / "notes.json").write_text("{}", encoding="utf-8")
    _response, body = await _get(client, PATH)
    assert [row["id"] for row in body["data"]] == [kept]
    assert theirs not in str(body) and old not in str(body)
    # Another caller's cursor is no id of this list, like one that names nothing.
    answers = []
    for after in (theirs, UNKNOWN, old):
        response, body = await _get(client, PATH, after=after)
        _refusal(response, body, 400, "invalid_request")
        body["error"].pop("request_id")
        answers.append(body)
    assert answers[0] == answers[1] == answers[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": "ten"},
        {"limit": "-1"},
        {"order": "newest"},
        {"after": "nope"},
    ],
)
async def test_a_query_value_the_contract_does_not_allow_is_refused(client, query) -> None:
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    for path in (PATH, f"{PATH}/{made['id']}/items"):
        response, body = await _get(client, path, **query)
        _refusal(response, body, 400, "invalid_request")
    _documented(response, "get", PATH + "/{conversation_id}/items")


@pytest.mark.asyncio
async def test_limit_takes_its_bounds(client, monkeypatch) -> None:
    await _spaced(client, monkeypatch, 3)
    _response, body = await _get(client, PATH, limit=1)
    assert len(body["data"]) == 1 and body["has_more"] is True
    _response, body = await _get(client, PATH, limit=100)
    assert len(body["data"]) == 3 and body["has_more"] is False


@pytest.mark.asyncio
async def test_records_that_cannot_be_read_are_unavailable(client, monkeypatch) -> None:
    _response, made = await _create(client)

    def _held(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("held by another process")

    monkeypatch.setattr(conversations, "read_bytes_with_retry", _held)
    for path in (PATH, f"{PATH}/{made['id']}/items"):
        response, body = await _get(client, path)
        _refusal(response, body, 503, "instance_unavailable")
    response, body = await _update(client, made["id"], {"metadata": {}})
    _refusal(response, body, 503, "instance_unavailable")
    response, body = await _delete(client, made["id"])
    _refusal(response, body, 503, "instance_unavailable")
    assert _conversations(client).is_busy(made["id"]) is False


# ── the history ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_history_is_each_turn_question_first_reply_second(client) -> None:
    _response, made = await _create(client)
    items_path = f"{PATH}/{made['id']}/items"
    response, body = await _get(client, items_path)
    assert (response.status, body["data"], body["has_more"]) == (200, [], False)
    client.turn.rows = [("chunk", "reply one", None)]
    await _ask(client, {"model": MODEL, "input": "question one", "conversation": made["id"]})
    client.turn.rows = [("chunk", "reply two", None)]
    two = [
        {"type": "message", "role": "developer", "content": "be brief"},
        {"type": "message", "role": "user", "content": "question two"},
    ]
    await _ask(client, {"model": MODEL, "input": two, "conversation": made["id"]})
    response, body = await _get(client, items_path, order="asc")
    assert response.status == 200, body
    _documented(response, "get", PATH + "/{conversation_id}/items")
    _valid("ConversationItemList", body)
    assert _texts(body["data"]) == [
        ("user", "completed", "question one"),
        ("assistant", "completed", "reply one"),
        ("developer", "completed", "be brief"),
        ("user", "completed", "question two"),
        ("assistant", "completed", "reply two"),
    ]
    ids = [item["id"] for item in body["data"]]
    assert len(set(ids)) == 5 and (body["first_id"], body["last_id"]) == (ids[0], ids[-1])
    # Newest first by default, and paged by an item's id.
    _response, body = await _get(client, items_path)
    assert [item["id"] for item in body["data"]] == ids[::-1]
    _response, body = await _get(client, items_path, limit=2, after=ids[3])
    assert [item["id"] for item in body["data"]] == [ids[2], ids[1]] and body["has_more"] is True
    _response, body = await _get(client, items_path, order="asc", after=ids[3])
    assert [item["id"] for item in body["data"]] == [ids[4]] and body["has_more"] is False
    assert OPERATIONS[PATH + "/{conversation_id}/items"]["get"]["x-crew-not-served-yet"] == {
        "response-headers": QUOTA
    }


@pytest.mark.asyncio
async def test_a_turn_that_did_not_complete_is_listed_as_incomplete(client) -> None:
    _response, made = await _create(client)
    client.turn.rows = [("error", "backend failed", None)]
    _response, failed = await _ask(
        client, {"model": MODEL, "input": "lost question", "conversation": made["id"]}
    )
    assert failed["status"] == "failed"
    client.turn.rows = [("chunk", "Fine.", None)]
    await _ask(client, {"model": MODEL, "input": "kept question", "conversation": made["id"]})
    _response, body = await _get(client, f"{PATH}/{made['id']}/items", order="asc")
    _valid("ConversationItemList", body)
    assert _texts(body["data"]) == [
        ("user", "incomplete", "lost question"),
        ("user", "completed", "kept question"),
        ("assistant", "completed", "Fine."),
    ]
    # The stored item is not changed: the turn's own input items read as they were kept.
    response = await client.get(f"{RESPONSES}/{failed['id']}/input_items", headers=AS_OWNER)
    assert (await response.json())["data"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_a_turn_still_running_is_not_in_the_history(client) -> None:
    _response, made = await _create(client)
    await _ask(client, {"model": MODEL, "input": "first", "conversation": made["id"]})
    client.turn.started.clear()
    client.turn.release = asyncio.Event()
    running = asyncio.ensure_future(
        _ask(client, {"model": MODEL, "input": "second", "conversation": made["id"]})
    )
    try:
        await await_until(client.turn.started, what="the second turn to start")
        _response, body = await _get(client, f"{PATH}/{made['id']}/items", order="asc")
        assert [text for _role, _status, text in _texts(body["data"])] == ["first", "Done."]
    finally:
        client.turn.release.set()
        await await_until(running, what="the second turn's answer")
    _response, body = await _get(client, f"{PATH}/{made['id']}/items", order="asc")
    assert [text for _role, _status, text in _texts(body["data"])][2:] == ["second", "Done."]


@pytest.mark.asyncio
async def test_the_history_is_redacted_on_its_way_out(client) -> None:
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    (turn,) = _conversations(client).load(made["id"], OWNER)["turns"]
    secret = github_classic(seed=7)
    path = _responses(client).directory / f"{turn}.json"
    path.write_text(path.read_text(encoding="utf-8").replace("hello", secret), encoding="utf-8")
    response = await client.get(f"{PATH}/{made['id']}/items", headers=AS_OWNER)
    text = await response.text()
    assert response.status == 200 and secret not in text and "REDACTED" in text


@pytest.mark.asyncio
async def test_the_history_never_holds_a_turn_of_another_conversation(client) -> None:
    _response, first = await _ask(client, {"model": MODEL, "input": "mine"})
    _response, other = await _ask(client, {"model": MODEL, "input": "theirs"})
    store = _conversations(client)
    record = store.load(first["conversation"]["id"], OWNER)
    record["turns"].append(other["id"])
    store._write(record)
    _response, body = await _get(client, f"{PATH}/{record['id']}/items", order="asc")
    assert [text for _role, _status, text in _texts(body["data"])] == ["mine", "Done."]
    # Nor a listed turn whose record is gone: what is kept of the others is served.
    (_responses(client).directory / f"{first['id']}.json").unlink()
    response, body = await _get(client, f"{PATH}/{record['id']}/items")
    assert (response.status, body["data"]) == (200, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [UNKNOWN, *NOT_IDS])
async def test_an_id_that_names_no_conversation_is_one_not_found(client, monkeypatch, name) -> None:
    opened: list[Any] = []
    monkeypatch.setattr(
        conversations, "read_bytes_with_retry", lambda path, **_kw: opened.append(path) or b"{}"
    )
    answers = []
    for answer in (
        await _get(client, f"{PATH}/{name}/items"),
        await _update(client, name, {"metadata": {}}),
        await _delete(client, name),
    ):
        _refusal(*answer, 404, "not_found")
        answer[1]["error"].pop("request_id")
        answers.append(answer[1])
    assert answers[0] == answers[1] == answers[2]
    # Only an id of this gateway's shape is looked for on the disk.
    assert (opened != []) is (name == UNKNOWN)
    assert _conversations(client)._busy == set()


# ── metadata ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_update_replaces_the_metadata_whole(client, monkeypatch) -> None:
    store = _conversations(client)
    now = [1_800_000_000.0]
    monkeypatch.setattr(store, "clock", lambda: now[0])
    _response, made = await _create(client, {"metadata": {"doc_id": "d42", "team": "docs"}})
    before = store.load(made["id"], OWNER)
    at = store._path(made["id"]).stat().st_mtime
    now[0] += 10 * 24 * 60 * 60
    response, body = await _update(client, made["id"], {"metadata": {"doc_id": "d43"}})
    assert response.status == 200, body
    _documented(response, "post", PATH + "/{conversation_id}")
    _valid("Conversation", body)
    assert body["metadata"] == {"doc_id": "d43"}
    assert body == (await _read(client, made["id"]))[1]
    after = store.load(made["id"], OWNER)
    # Nothing but the metadata changed: the conversation is kept from where it was.
    assert after == {**before, "metadata": {"doc_id": "d43"}}
    assert store._path(made["id"]).stat().st_mtime == at
    now[0] = before["updated_at"] + records.RETENTION_SECS
    response, body = await _update(client, made["id"], {"metadata": {}})
    _refusal(response, body, 404, "not_found")
    assert OPERATIONS[PATH + "/{conversation_id}"]["post"]["x-crew-not-served-yet"] == {
        "request-headers": ["Idempotency-Key"],
        "response-headers": QUOTA,
    }


@pytest.mark.asyncio
async def test_an_empty_metadata_removes_every_pair(client) -> None:
    _response, made = await _create(client, {"metadata": {"doc_id": "d42"}})
    again = {**AS_OWNER, "Idempotency-Key": "one-key"}
    response, body = await _update(client, made["id"], {"metadata": {}}, headers=again)
    assert (response.status, body["metadata"]) == (200, {})
    # The same key with another body is served: the header is not read.
    response, body = await _update(client, made["id"], {"metadata": {"k": "v"}}, headers=again)
    assert (response.status, body["metadata"]) == (200, {"k": "v"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        "text",
        {"metadata": None},
        {"metadata": {"k": 1}},
        {"metadata": {"k": "v"}, "title": "x"},
        {"metadata": {str(n): "v" for n in range(17)}},
        {"metadata": {"k" * 65: "v"}},
        {"metadata": {"k": "v" * 513}},
    ],
)
async def test_a_malformed_update_is_refused_and_nothing_changes(client, body) -> None:
    _response, made = await _create(client, {"metadata": {"doc_id": "d42"}})
    response, answer = await _update(client, made["id"], body)
    _refusal(response, answer, 400, "invalid_request")
    assert (await _read(client, made["id"]))[1]["metadata"] == {"doc_id": "d42"}
    response = await client.post(f"{PATH}/{made['id']}", data=b"{", headers=AS_OWNER)
    _refusal(response, await response.json(), 400, "invalid_request")


@pytest.mark.asyncio
async def test_a_conversation_is_updated_while_a_turn_runs_in_it(client) -> None:
    _response, made = await _create(client)
    client.turn.release = asyncio.Event()
    running = asyncio.ensure_future(_ask(client, {**ASK, "conversation": made["id"]}))
    try:
        await await_until(client.turn.started, what="the turn to start")
        response, body = await _update(client, made["id"], {"metadata": {"doc_id": "d43"}})
        assert (response.status, body["crew"]["status"]) == (200, "running")
    finally:
        client.turn.release.set()
        _response, answered = await await_until(running, what="the turn's answer")
    # The turn's end and the update each kept what the other wrote.
    record = _conversations(client).load(made["id"], OWNER)
    assert (record["metadata"], record["turns"]) == ({"doc_id": "d43"}, [answered["id"]])


# ── delete ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_deleted_conversation_and_its_turns_are_gone(client, monkeypatch) -> None:
    monkeypatch.setattr(conversations, "sel", lambda: client.audit)
    _response, made = await _create(client)
    _response, first = await _ask(client, {**ASK, "conversation": made["id"]})
    client.turn.rows = [("error", "backend failed", None)]
    _response, failed = await _ask(client, {**ASK, "conversation": made["id"]})
    _response, kept = await _ask(client)
    response, body = await _delete(client, made["id"])
    assert response.status == 200, body
    _documented(response, "delete", PATH + "/{conversation_id}")
    _valid("Deleted", body)
    assert body == {"id": made["id"], "object": "conversation.deleted", "deleted": True}
    # The conversation and both of its turns are off the disk; the other one stays.
    assert _files(_conversations(client).directory) == [f"{kept['conversation']['id']}.json"]
    assert _files(_responses(client).directory) == [f"{kept['id']}.json"]
    assert _conversations(client)._busy == set()
    for path in (
        f"{PATH}/{made['id']}",
        f"{PATH}/{made['id']}/items",
        f"{RESPONSES}/{first['id']}",
        f"{RESPONSES}/{failed['id']}/input_items",
    ):
        _refusal(*await _get(client, path), 404, "not_found")
    for names in ({"conversation": made["id"]}, {"previous_response_id": first["id"]}):
        response, body = await _ask(client, {**ASK, **names})
        assert (response.status, body["error"]["code"]) == (404, "not_found")
    _refusal(*await _delete(client, made["id"]), 404, "not_found")
    assert [row["id"] for row in (await _get(client, PATH))[1]["data"]] == [
        kept["conversation"]["id"]
    ]
    assert OPERATIONS[PATH + "/{conversation_id}"]["delete"]["x-crew-not-served-yet"] == {
        "response-headers": QUOTA
    }
    client.audit.log_api_access.assert_any_call(
        caller="U_OWNER",
        operation="crew_api.conversations.delete",
        outcome="allowed",
        source="crew_api",
        resources=f"conversation={made['id']} found=yes",
    )


@pytest.mark.asyncio
async def test_a_conversation_with_a_turn_running_is_not_deleted(client) -> None:
    _response, made = await _create(client)
    _response, first = await _ask(client, {**ASK, "conversation": made["id"]})
    client.turn.started.clear()
    client.turn.release = asyncio.Event()
    running = asyncio.ensure_future(_ask(client, {**ASK, "conversation": made["id"]}))
    try:
        await await_until(client.turn.started, what="the second turn to start")
        response, body = await _delete(client, made["id"])
        error = _refusal(response, body, 409, "response_in_progress")
        assert error["type"] == "conflict_error"
        _documented(response, "delete", PATH + "/{conversation_id}")
        # Nothing went, and the running turn still holds the conversation.
        assert _conversations(client).is_busy(made["id"]) is True
        assert (await _get(client, f"{RESPONSES}/{first['id']}"))[0].status == 200
    finally:
        client.turn.release.set()
        _response, second = await await_until(running, what="the second turn's answer")
    record = _conversations(client).load(made["id"], OWNER)
    assert record["turns"] == [first["id"], second["id"]]
    # Free once the turn ended: the delete goes through.
    response, body = await _delete(client, made["id"])
    assert response.status == 200, body


@pytest.mark.asyncio
async def test_a_new_conversation_is_claimed_before_its_record_is_readable(
    client, monkeypatch
) -> None:
    store = _conversations(client)
    create = store.create
    entered, proceed = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def _held(*args: Any, **kwargs: Any) -> dict[str, Any]:
        record = create(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(default_timeout()), "the create was never let on"
        return record

    monkeypatch.setattr(store, "create", _held)
    running = asyncio.ensure_future(_ask(client))
    try:
        await await_until(entered, what="the new conversation record to be written")
        _response, listed = await _get(client, PATH)
        (record,) = listed["data"]
        named = record["id"]
        _refusal(*await _delete(client, named), 409, "response_in_progress")
        assert store.is_busy(named) is True
        assert store.load(named, OWNER) is not None
        assert client.turn.entered == []
    finally:
        proceed.set()
        response, answered = await await_until(running, what="the new conversation's answer")
    assert (response.status, answered["status"]) == (200, "completed")
    assert answered["conversation"] == {"id": named}
    assert store.load(named, OWNER)["turns"] == [answered["id"]]
    assert store.is_busy(named) is False


@pytest.mark.asyncio
async def test_a_failed_create_releases_the_new_conversation_before_the_turn_runs(
    client, monkeypatch
) -> None:
    store = _conversations(client)
    entered, proceed = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    client.turn.release = asyncio.Event()

    def _full(*_args: Any, **_kwargs: Any) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(default_timeout()), "the create was never let on"
        raise OSError("disk full")

    monkeypatch.setattr(store, "create", _full)
    running = asyncio.ensure_future(_ask(client))
    try:
        await await_until(entered, what="the new conversation create to be attempted")
        assert len(store._busy) == 1
        (named,) = store._busy
        proceed.set()
        await await_until(client.turn.started, what="the turn to run without a conversation")
        assert store.is_busy(named) is False
        assert store.load(named, OWNER) is None
    finally:
        proceed.set()
        client.turn.release.set()
        response, answered = await await_until(running, what="the turn's answer")
    assert (response.status, answered["status"]) == (200, "completed")
    assert "conversation" not in answered
    assert store._busy == set()


@pytest.mark.asyncio
async def test_no_turn_starts_in_a_conversation_that_is_being_deleted(client, monkeypatch) -> None:
    _response, made = await _create(client)
    _response, first = await _ask(client, {**ASK, "conversation": made["id"]})
    store = _conversations(client)
    delete = store.delete
    entered, proceed = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def _held(*args: Any, **kwargs: Any) -> bool:
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(default_timeout()), "the delete was never let on"
        return delete(*args, **kwargs)

    monkeypatch.setattr(store, "delete", _held)
    deleting = asyncio.ensure_future(_delete(client, made["id"]))
    try:
        await await_until(entered, what="the delete to hold the conversation")
        for names in ({"conversation": made["id"]}, {"previous_response_id": first["id"]}):
            response, body = await _ask(client, {**ASK, **names})
            assert (response.status, body["error"]["code"]) == (409, "response_in_progress")
        _refusal(*await _delete(client, made["id"]), 409, "response_in_progress")
    finally:
        proceed.set()
        response, body = await await_until(deleting, what="the delete's answer")
    assert response.status == 200, body
    assert len(client.turn.entered) == 1 and store._busy == set()
    response, body = await _ask(client, {**ASK, "conversation": made["id"]})
    assert (response.status, body["error"]["code"]) == (404, "not_found")


@pytest.mark.asyncio
async def test_the_conversation_goes_before_its_turns(client, monkeypatch) -> None:
    """What a gateway that stops part of the way through a delete leaves."""
    _response, made = await _create(client)
    _response, first = await _ask(client, {**ASK, "conversation": made["id"]})
    store, responses = _conversations(client), _responses(client)
    seen: list[bool] = []

    def _fails(response_id: str) -> None:
        seen.append(store._path(made["id"]).exists())
        raise PermissionError("held by another process")

    monkeypatch.setattr(responses, "discard", _fails)
    response, body = await _delete(client, made["id"])
    # The conversation's record was already gone when the first turn was removed.
    assert (response.status, seen) == (200, [False])
    # The turn's record is still on disk, and reads as absent all the same.
    assert _files(responses.directory) == [f"{first['id']}.json"]
    _refusal(*await _get(client, f"{RESPONSES}/{first['id']}"), 404, "not_found")
    response, body = await _ask(client, {**ASK, "previous_response_id": first["id"]})
    assert (response.status, body["error"]["code"]) == (404, "not_found")


@pytest.mark.asyncio
async def test_a_conversation_that_cannot_be_removed_keeps_its_turns(client, monkeypatch) -> None:
    _response, made = await _create(client)
    _response, first = await _ask(client, {**ASK, "conversation": made["id"]})
    store = _conversations(client)
    path = type(store._path(made["id"]))
    unlink = path.unlink

    def _held(self: Any, *args: Any, **kwargs: Any) -> None:
        if self.parent == store.directory:
            raise PermissionError("held by another process")
        unlink(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(path, "unlink", _held)
        response, body = await _delete(client, made["id"])
    _refusal(response, body, 503, "instance_unavailable")
    assert store.is_busy(made["id"]) is False
    assert (await _get(client, f"{RESPONSES}/{first['id']}"))[0].status == 200
    assert store.load(made["id"], OWNER)["turns"] == [first["id"]]


def test_only_the_callers_own_conversation_is_updated_or_deleted(tmp_path) -> None:
    store = conversations.ConversationStore(directory=tmp_path / "conversations")
    responses = records.ResponseStore(directory=tmp_path / "responses")
    theirs = store.create({"k": "v"}, conversations.Caller("grant_1"))
    assert store.set_metadata(theirs["id"], OWNER, {}) is None
    assert store.delete(theirs["id"], OWNER, responses) is False
    assert store.load(theirs["id"], conversations.Caller("grant_1")) == theirs
    assert store.listed(OWNER) == []
    assert [record["id"] for record in store.listed(conversations.Caller("grant_1"))] == [
        theirs["id"]
    ]


# ── who is served ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_only_the_machine_owner_reaches_these_routes(client, monkeypatch, caller) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _response, made = await _create(client)
    await _ask(client, {**ASK, "conversation": made["id"]})
    store, responses = _conversations(client), _responses(client)
    before = (_files(store.directory), _files(responses.directory), store.load(made["id"], OWNER))

    def _no_read(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a refused caller must not reach the records")

    with monkeypatch.context() as patched:
        for name in ("load", "listed", "set_metadata", "delete", "claim"):
            patched.setattr(store, name, _no_read)
        patched.setattr(responses, "load", _no_read)
        _refusal(*await _get(client, PATH, headers=caller), 403, "owner_only")
        for send in (
            lambda name: _get(client, f"{PATH}/{name}/items", headers=caller),
            lambda name: _update(client, name, {"metadata": {}}, headers=caller),
            lambda name: _delete(client, name, headers=caller),
        ):
            answers = []
            for name in (made["id"], UNKNOWN):
                response, body = await send(name)
                _refusal(response, body, 403, "owner_only")
                body["error"].pop("request_id")
                answers.append(body)
            # A conversation that exists and one that does not read alike to such a caller.
            assert answers[0] == answers[1]
    assert (
        _files(store.directory),
        _files(responses.directory),
        store.load(made["id"], OWNER),
    ) == before
    assert store._busy == set()


def test_the_operation_names_say_what_the_handlers_do() -> None:
    from kiro_crew.dashboard import route_access
    from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES

    rows = {
        ("GET", PATH): lists.LIST_OPERATION,
        ("GET", PATH + "/{conversation_id}/items"): lists.ITEMS_OPERATION,
        ("POST", PATH + "/{conversation_id}"): conversations.UPDATE_OPERATION,
        ("DELETE", PATH + "/{conversation_id}"): conversations.DELETE_OPERATION,
    }
    for key, operation in rows.items():
        assert route_access.ROUTES[key].action == operation
        # Every method of a path the table names is behind the owner gate.
        assert key[1] in OWNER_ONLY_PUBLIC_ROUTES
        assert not operation.endswith((".read", ".list"))
