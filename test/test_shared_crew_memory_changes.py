"""Shared Crew memory: newest preference wins, everyone is told, and the change is audited.

Crew memory is one store every admitted person teaches. A new lesson that
conflicts with a stored one replaces it, whoever wrote either, with no owner
priority. The write records who made it (``written_by``); a write that replaced
lessons writes one ``memory_change`` audit record, and in a shared Crew also
posts a notice in its session and a frame to every collaborator.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.history import ConversationLog
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.shared_crew import audit, memory_changes
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    MESSAGE_AUTHOR_META_KEY,
    ActorContext,
    Principal,
    Provenance,
    Transport,
)
from kiro_crew.shared_crew.identity import tailnet_principal
from kiro_crew.vector_memory import VectorMemoryStore

ALICE = tailnet_principal(
    "alice@example.com", "a", Provenance.TAILSCALE_SERVE, display_name="Alice"
)
BOB = tailnet_principal("bob@example.com", "b", Provenance.TAILSCALE_SERVE, display_name="Bob")
SESSION = "dashboard:chat-1"

CHINESE = "Always reply in Chinese"
ENGLISH = "Always reply in English"
LINT = "Run the linter before every commit"


def _store(tmp_path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


def _values(store: VectorMemoryStore) -> list[dict[str, Any]]:
    return [json.loads(row["value_json"]) for row in store.get_lessons()]


class _Slot:
    """The two things the notice path reads off a slot."""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.key = SESSION
        self.messages = messages or []
        self.appended: list[tuple[str, str, dict[str, Any]]] = []

    def append(self, role: str, content: str, cls: str = "", **kw: Any) -> dict[str, Any]:
        self.appended.append((role, content, kw.get("meta") or {}))
        return {}


def _Request(state: Any, body: dict[str, Any], **published: Any) -> MagicMock:
    """An aiohttp request stand-in whose ``get`` reads published request keys."""
    request = MagicMock()
    request.get.side_effect = lambda key, default=None: published.get(key, default)
    request.app = {"state": state}
    request.headers = {"X-Session-Key": SESSION}
    attach_body(request, body)
    return request


def _state(slot: _Slot) -> MagicMock:
    state = MagicMock()
    state.conversation_log = ConversationLog()
    state._background_tasks = set()
    state._slots = {SESSION: slot}
    state._ws_clients = []
    state.owner_id = ""
    return state


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(principal=principal, request_id="r", transport=Transport.HTTP)


@pytest.fixture
def audit_events(monkeypatch):
    events: list[Any] = []
    monkeypatch.setattr(audit, "_default_writer", events.append)
    return events


async def _drain_audits() -> None:
    pending = list(audit._pending_audits)
    if pending:
        await asyncio.gather(*pending)


async def _post(
    state: Any,
    rule: str,
    *,
    principal: Principal | None,
    vector_store: Any,
    category: str = "preference",
    shared: bool = True,
    internal: bool = False,
    jsonl_store: LessonStore | None = None,
    memory_store: str = "",
) -> dict[str, Any]:
    from kiro_crew.dashboard.handlers import cron

    published: dict[str, Any] = {}
    if principal is not None:
        published[ACTOR_REQUEST_KEY] = _actor(principal)
    if internal:
        published["internal_auth"] = True
    request = _Request(state, {"rule": rule, "category": category}, **published)
    patches = [
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vector_store)),
        patch.object(cron, "_is_restricted_session", return_value=False),
        patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
        # The Crew's one store, whichever session wrote.
        patch.object(
            cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(memory_store, None))
        ),
        patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(
            cron.ContextBuilder, "get_memory_for", return_value=MagicMock(vector_store=vector_store)
        ),
        patch.object(cron, "_sel"),
        patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
        patch.object(memory_changes, "crew_is_shared", return_value=shared),
    ]
    if jsonl_store is not None:
        patches.append(patch.object(cron, "_lesson_jsonl_store", return_value=jsonl_store))
    for p in patches:
        p.start()
    try:
        resp = await cron.api_lessons_create(request)
    finally:
        for p in reversed(patches):
            p.stop()
    await _drain_audits()
    return json.loads(resp.text)


@pytest.mark.asyncio
async def test_conflicting_preferences_from_two_people_keep_only_the_newest(tmp_path, audit_events):
    store = _store(tmp_path)
    slot = _Slot()
    state = _state(slot)
    try:
        first = await _post(state, CHINESE, principal=ALICE, vector_store=store)
        assert first["outcome"] == "inserted"
        second = await _post(state, ENGLISH, principal=BOB, vector_store=store)
        assert second["superseded"] == [CHINESE]

        values = _values(store)
        assert [v["rule"] for v in values] == [ENGLISH], "the old preference is gone"
        assert values[0]["written_by"] == {"id": BOB.id, "kind": "human", "name": "Bob"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_change_is_posted_to_the_session_and_sent_to_collaborators(
    tmp_path, audit_events
):
    store = _store(tmp_path)
    slot = _Slot()
    state = _state(slot)
    try:
        await _post(state, CHINESE, principal=ALICE, vector_store=store)
        assert slot.appended == [], "a write that replaced nothing posts no notice"
        await _post(state, ENGLISH, principal=BOB, vector_store=store)
    finally:
        store.close()

    ((role, content, meta),) = slot.appended
    assert role == "assistant"
    assert content == f"Bob changed a Crew preference: {ENGLISH}"
    assert meta["kind"] == memory_changes.NOTICE_KIND
    change = meta[memory_changes.NOTICE_META_KEY]
    assert change == {
        "actor": {"id": BOB.id, "kind": "human", "name": "Bob"},
        "session_key": SESSION,
        "category": "preference",
        "new": ENGLISH,
        "old": [CHINESE],
    }
    state.broadcast_ws_owners.assert_called_once()
    frame_type, frame = state.broadcast_ws_owners.call_args.args
    assert frame_type == memory_changes.WS_FRAME == "shared_crew.memory_change"
    assert frame == {"type": frame_type, "schema_version": 1, "change": change}


@pytest.mark.asyncio
async def test_the_audit_record_names_the_actor_and_the_old_and_new_summary(tmp_path, audit_events):
    store = _store(tmp_path)
    state = _state(_Slot())
    try:
        await _post(state, CHINESE, principal=ALICE, vector_store=store)
        assert audit_events == [], "an insert that replaced nothing is not a memory change"
        await _post(state, ENGLISH, principal=BOB, vector_store=store)
    finally:
        store.close()

    (event,) = audit_events
    assert event.event_type == "shared_crew_action"
    assert event.operation == "shared_crew.memory_change"
    assert event.outcome == "applied"
    assert event.caller_identity == BOB.id
    assert event.metadata["initiator"]["id"] == BOB.id
    assert event.metadata["session_key"] == SESSION
    assert event.metadata["memory_change"] == {
        "category": "preference",
        "old": [CHINESE],
        "new": ENGLISH,
    }
    assert event.timestamp


@pytest.mark.asyncio
async def test_non_conflicting_lessons_are_both_kept(tmp_path, audit_events):
    store = _store(tmp_path)
    slot = _Slot()
    state = _state(slot)
    try:
        await _post(state, CHINESE, principal=ALICE, vector_store=store)
        body = await _post(state, LINT, principal=BOB, vector_store=store, category="tool")
        assert body["superseded"] == []
        assert sorted(v["rule"] for v in _values(store)) == sorted([CHINESE, LINT])
    finally:
        store.close()
    assert slot.appended == []
    assert audit_events == []


@pytest.mark.asyncio
async def test_a_personal_crew_still_replaces_but_posts_no_changed_by_notice(
    tmp_path, audit_events
):
    store = _store(tmp_path)
    slot = _Slot()
    state = _state(slot)
    try:
        await _post(state, CHINESE, principal=ALICE, vector_store=store, shared=False)
        body = await _post(state, ENGLISH, principal=ALICE, vector_store=store, shared=False)
        assert body["superseded"] == [CHINESE]
        assert [v["rule"] for v in _values(store)] == [ENGLISH]
    finally:
        store.close()
    assert slot.appended == []
    state.broadcast_ws_owners.assert_not_called()
    assert len(audit_events) == 1, "the audit record is kept in every mode"


@pytest.mark.asyncio
async def test_an_agent_write_is_attributed_to_the_author_of_the_latest_turn(
    tmp_path, audit_events
):
    """``learn_add`` posts through the internal caller; the turn's author is the writer."""
    store = _store(tmp_path)
    slot = _Slot(
        [
            {
                "role": "user",
                "content": "x",
                "meta": {MESSAGE_AUTHOR_META_KEY: ALICE.to_public_dict()},
            },
            {"role": "assistant", "content": "y"},
            {
                "role": "user",
                "content": "z",
                "meta": {MESSAGE_AUTHOR_META_KEY: BOB.to_public_dict()},
            },
        ]
    )
    state = _state(slot)
    try:
        # A published actor on an internal call is the caller, never the person.
        await _post(state, CHINESE, principal=ALICE, vector_store=store, internal=True)
        assert _values(store)[0]["written_by"]["id"] == BOB.id
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_write_with_no_known_person_keeps_the_old_row_shape(tmp_path, audit_events):
    store = _store(tmp_path)
    state = _state(_Slot())
    try:
        await _post(state, CHINESE, principal=None, vector_store=store)
        (value,) = _values(store)
        assert "written_by" not in value
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_jsonl_store_route_replaces_and_announces_the_same_way(tmp_path, audit_events):
    jsonl = LessonStore(base_dir=tmp_path)
    slot = _Slot()
    state = _state(slot)
    await _post(state, CHINESE, principal=ALICE, vector_store=None, jsonl_store=jsonl)
    body = await _post(state, ENGLISH, principal=BOB, vector_store=None, jsonl_store=jsonl)
    assert body["superseded"] == [CHINESE]
    rows = jsonl.load_all()
    assert [le.rule for le in rows] == [ENGLISH]
    assert rows[0].written_by == {"id": BOB.id, "kind": "human", "name": "Bob"}
    assert len(slot.appended) == 1
    assert len(audit_events) == 1


def test_jsonl_newest_preference_wins_and_reports_what_it_replaced(tmp_path):
    store = LessonStore(base_dir=tmp_path)
    store.save_or_enrich(Lesson(ts="1", rule=CHINESE, category="preference"))
    store.save_or_enrich(Lesson(ts="2", rule=LINT, category="tool"))
    replaced: list[str] = []
    assert (
        store.save_or_enrich(
            Lesson(ts="3", rule=ENGLISH, category="preference"), superseded_out=replaced
        )
        == "inserted"
    )
    assert replaced == [CHINESE]
    assert [le.rule for le in store.load_all()] == [LINT, ENGLISH]


def test_jsonl_keeps_non_conflicting_preferences_and_never_replaces_other_categories(tmp_path):
    store = LessonStore(base_dir=tmp_path)
    store.save_or_enrich(Lesson(ts="1", rule=CHINESE, category="preference"))
    replaced: list[str] = []
    store.save_or_enrich(
        Lesson(ts="2", rule="Use dark mode screenshots", category="preference"),
        superseded_out=replaced,
    )
    store.save_or_enrich(
        Lesson(ts="3", rule=ENGLISH, category="knowledge"), superseded_out=replaced
    )
    assert replaced == []
    assert len(store.load_all()) == 3


def test_jsonl_automatic_writers_never_replace(tmp_path):
    store = LessonStore(base_dir=tmp_path)
    store.save(Lesson(ts="1", rule=CHINESE, category="preference"))
    store.save(Lesson(ts="2", rule=ENGLISH, category="preference"))
    assert len(store.load_all()) == 2


def test_legacy_lessons_without_a_writer_still_load(tmp_path):
    path = tmp_path / "lessons.jsonl"
    path.write_text(
        json.dumps({"ts": "1", "rule": CHINESE, "category": "preference", "negative": None})
        + "\n"
        + json.dumps({"ts": "2", "rule": LINT, "category": "tool", "written_by": "not-a-mapping"})
        + "\n"
    )
    rows = LessonStore(base_dir=tmp_path).load_all()
    assert [(le.rule, le.written_by) for le in rows] == [(CHINESE, None), (LINT, None)]


def test_legacy_vector_rows_without_a_writer_still_read_and_get_replaced(tmp_path):
    store = _store(tmp_path)
    try:
        store.write_lesson(CHINESE, "preference")
        (value,) = _values(store)
        assert value == {"rule": CHINESE, "category": "preference", "negative": None}
        result = store.write_lesson(
            ENGLISH, "preference", written_by=memory_changes.writer_stamp(BOB)
        )
        assert result.superseded == (CHINESE,)
        assert [v["rule"] for v in _values(store)] == [ENGLISH]
    finally:
        store.close()


def test_a_writer_label_does_not_shrink_the_rule_capacity(tmp_path):
    store = _store(tmp_path)
    try:
        stamp = memory_changes.writer_stamp(BOB)
        assert store.write_lesson("x" * 900, "knowledge", written_by=stamp)
    finally:
        store.close()


def test_summaries_are_clipped_to_one_short_line():
    long = "word " * 100
    out = memory_changes.summarize(long + "\nmore")
    assert len(out) == memory_changes.SUMMARY_MAX
    assert "\n" not in out
    change = memory_changes.build_change(
        BOB, session_key=SESSION, category="preference", new_rule=long, replaced=[long] * 9
    )
    assert change is not None
    assert len(change.old_summaries) == memory_changes.OLD_MAX
    assert (
        memory_changes.build_change(
            BOB, session_key=SESSION, category="preference", new_rule="x", replaced=[]
        )
        is None
    )


def test_stamp_reads_back_only_well_formed_values():
    assert memory_changes.stamp_of(None) is None
    assert memory_changes.stamp_of({"id": "", "kind": "human"}) is None
    assert memory_changes.stamp_of({"id": BOB.id, "kind": "human"}) == {
        "id": BOB.id,
        "kind": "human",
        "name": "",
    }
    assert memory_changes.writer_stamp(Principal.unknown()) is None


def test_a_crew_with_only_its_operator_is_personal(monkeypatch):
    from kiro_crew.shared_crew import access, people

    monkeypatch.setattr(access, "setup_paths_dict", lambda: {})
    monkeypatch.setattr(people, "registry", lambda: MagicMock(snapshot=lambda: []))
    assert (
        memory_changes.crew_is_shared(MagicMock(owner_id=""), ALICE) is True
    ), "a writer who came in over the tailnet means a team path exists"
    from kiro_crew.shared_crew.identity import local_operator_principal

    operator = local_operator_principal("owner")
    assert memory_changes.crew_is_shared(MagicMock(owner_id=""), operator) is False
    observed = MagicMock(principal=BOB)
    monkeypatch.setattr(people, "registry", lambda: MagicMock(snapshot=lambda: [observed]))
    assert memory_changes.crew_is_shared(MagicMock(owner_id=""), operator) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_store", ["", "owner-notes"])
async def test_replacement_notice_matches_store_read_audience(tmp_path, audit_events, memory_store):
    store = _store(tmp_path)
    slot = _Slot()
    state = _state(slot)
    collaborator = MagicMock(closed=False)
    claims = {"_is_dashboard_user": True, "_app": "", "_shared_crew_actor": _actor(BOB)}
    collaborator.get.side_effect = lambda key, default=None: claims.get(key, default)
    state._ws_clients = [collaborator]
    state._owner_ws_clients = set()
    try:
        await _post(state, CHINESE, principal=ALICE, vector_store=store, memory_store=memory_store)
        result = await _post(
            state, ENGLISH, principal=ALICE, vector_store=store, memory_store=memory_store
        )
    finally:
        store.close()
    assert result["superseded"] == [CHINESE]
    assert len(audit_events) == 1
    state.broadcast_ws_owners.assert_called_once()
    if memory_store:
        state._spawn_ws_send.assert_not_called()
        assert slot.appended == []
    else:
        state._spawn_ws_send.assert_called_once()
        assert state._spawn_ws_send.call_args.args[0] is collaborator
        assert len(slot.appended) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_store", ["", "owner-notes"])
@pytest.mark.parametrize("owner", [False, True])
async def test_lesson_store_audience_uses_existing_owner_gate(monkeypatch, memory_store, owner):
    from kiro_crew.dashboard.handlers import _shared

    state = _state(_Slot())
    state.owner_id = "owner"
    request = _Request(state, {}, user="owner" if owner else "collaborator", app="")
    request.__contains__.side_effect = lambda key: key in {"user", "app"}
    request.__getitem__.side_effect = lambda key: request.get(key)
    monkeypatch.setattr(_shared, "_session_memory_store", lambda *_: memory_store)
    monkeypatch.setattr(_shared, "memory_startup_refusal", lambda *args: None)
    store, refusal = await _shared.resolve_lesson_memory_store(request, state, "lessons.list")
    assert _shared.lesson_store_owner_only(memory_store) is bool(memory_store)
    if memory_store and not owner:
        assert store == ""
        assert refusal is not None and refusal.status == 403
    else:
        assert store == memory_store and refusal is None
