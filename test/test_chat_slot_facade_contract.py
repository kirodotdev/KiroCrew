"""Characterize the storage and projection facade exposed by ``_ChatSlot``.

These contracts are deliberately observable from outside the slot.  Dashboard
callers replace several of its containers wholesale during replay, rewind, and
cleanup, while REST and WebSocket snapshots share the ordered ``to_dict``
projection.  A composed implementation must therefore keep consulting the
facade's current containers rather than retaining an initialization-time alias.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.dashboard import state as state_module
from kiro_crew.dashboard.state import _ChatSlot

_TO_DICT_KEYS = (
    "key",
    "title",
    "agent",
    "effective_agent",
    "model",
    "model_withheld",
    "reasoning_effort",
    "mode",
    "surface",
    "workspace",
    "project",
    # Remote-execution binding: present on EVERY slot, so the frontend can tell
    # "runs locally" from "the field is missing on an older gateway".
    "executor",
    "instance_id",
    "artifact",
    "messages",
    "running",
    "orchestrating",
    "queue_depth",
    "stopping",
    "pending_approval",
    "pending_approval_info",
    "last_activity_ts",
    "waiting_for_input",
    "needs_input",
    "interrupted",
    "stop_state",
    "wait_state",
    "created",
    "last_ts",
    "last_turn_ts",
    "last_message",
    "source_links",
    "source_links_total",
    "todo",
    "mcp_report",
    "has_options",
    "options",
    "prompt_preview",
    "trust",
    "trust_reads",
    "trusted_patterns_count",
    "slack_linked",
    "slack_channel",
    "slack_thread_ts",
    "folder_id",
    "pinned",
    "tags",
    "color_index",
    "color_hex",
    "color_theme",
    "theme_consent",
    "theme_consent_sha",
    "memory_mode",
    "forked_from",
    "linked_session_key",
    "app",
    "origin",
)


def test_facade_methods_consult_wholesale_replacement_containers() -> None:
    slot = _ChatSlot("replace-containers")
    original_messages = slot.messages
    original_pending = slot._pending
    original_queue = slot._queue
    original_context = slot._pending_context
    original_deferred = slot._deferred_notes
    original_approvals = slot._approval_futures
    original_questions = slot._question_pending

    messages: list[dict] = []
    pending: list[dict] = []
    queue: list[dict] = []
    pending_context: list[dict] = []
    deferred_notes = [{"content": "held", "context": {"content": "context"}}]
    approval_futures = {"approval-1": SimpleNamespace(done=lambda: False)}
    questions = {"question-1": {"blocking": True}}
    slot.messages = messages
    slot._pending = pending
    slot._queue = queue
    slot._pending_context = pending_context
    slot._deferred_notes = deferred_notes
    slot._approval_futures = approval_futures  # type: ignore[assignment]
    slot._question_pending = questions

    row = slot.append("assistant", "new row", ts="t1", broadcast=False)
    queue_id = slot.queue_append("queued row")
    context = {"content": "new context", "source": "test"}
    slot.append_pending_context(context)
    payload = slot.to_dict()

    assert slot.messages is messages and messages == [row]
    assert slot._pending is pending and pending == [row]
    assert slot._queue is queue and queue == [{"id": queue_id, "content": "queued row", "kind": ""}]
    assert slot._pending_context is pending_context and pending_context == [context]
    assert slot._deferred_notes is deferred_notes
    assert slot.deferred_context_count() == 1
    assert slot._approval_futures is approval_futures
    assert payload["pending_approval"] is True
    assert slot._question_pending is questions
    assert payload["needs_input"] is True

    assert original_messages == []
    assert original_pending == []
    assert original_queue == []
    assert original_context == []
    assert original_deferred == []
    assert original_approvals == {}
    assert original_questions == {}


def test_append_callbacks_observe_pretrim_publication_order(monkeypatch) -> None:
    monkeypatch.setattr(state_module, "_MAX_SLOT_MESSAGES", 2)
    slot = _ChatSlot("append-order")
    slot.messages = [
        {"role": "assistant", "content": "oldest", "cls": "", "ts": "t1"},
        {"role": "assistant", "content": "newer", "cls": "", "ts": "t2"},
    ]
    slot.total_messages = 2
    slot._disk_window_len = 2
    slot._question_pending = {
        "stateless": {"blocking": False},
        "blocking": {"blocking": True},
    }
    events: list[tuple[str, dict]] = []

    def on_questions_retired(key: str, retired: list[str]) -> None:
        events.append(
            (
                "retired",
                {
                    "key": key,
                    "retired": retired,
                    "contents": [message["content"] for message in slot.messages],
                    "pending": list(slot._pending),
                    "event": slot.event.is_set(),
                    "dirty": slot._dirty,
                    "total": slot.total_messages,
                    "questions": dict(slot._question_pending),
                },
            )
        )

    def on_message(key: str, message: dict) -> None:
        events.append(
            (
                "message",
                {
                    "key": key,
                    "message": message,
                    "contents": [item["content"] for item in slot.messages],
                    "pending_is_message": slot._pending == [message]
                    and slot._pending[0] is message,
                    "event": slot.event.is_set(),
                    "dirty": slot._dirty,
                    "total": slot.total_messages,
                },
            )
        )

    slot._on_question_retired = on_questions_retired
    slot._on_message = on_message

    row = slot.append(
        "user",
        "live",
        ts="t3",
        broadcast=True,
        broadcast_user=True,
    )

    assert [name for name, _snapshot in events] == ["retired", "message"]
    retired = events[0][1]
    assert retired == {
        "key": "append-order",
        "retired": ["stateless"],
        "contents": ["oldest", "newer"],
        "pending": [],
        "event": False,
        "dirty": False,
        "total": 2,
        "questions": {"blocking": {"blocking": True}},
    }
    published = events[1][1]
    assert published["key"] == "append-order"
    assert published["message"] is row
    assert published["contents"] == ["oldest", "newer", "live"]
    assert published["pending_is_message"] is True
    assert published["event"] is True
    assert published["dirty"] is True
    assert published["total"] == 3
    assert [message["content"] for message in slot.messages] == ["newer", "live"]
    assert slot.messages[-1] is row


def test_replay_append_does_not_retire_pending_questions() -> None:
    slot = _ChatSlot("replay-question")
    questions = {
        "stateless": {"blocking": False},
        "blocking": {"blocking": True},
    }
    retired: list[tuple[str, list[str]]] = []
    broadcasted: list[dict] = []
    slot._question_pending = questions
    slot._on_question_retired = lambda key, ids: retired.append((key, ids))
    slot._on_message = lambda _key, message: broadcasted.append(message)

    row = slot.append("nudge", "historical", ts="t1", broadcast=False)

    assert slot._question_pending is questions
    assert slot._question_pending == {
        "stateless": {"blocking": False},
        "blocking": {"blocking": True},
    }
    assert retired == []
    assert broadcasted == []
    assert slot.messages == [row]
    assert slot._pending == [row]


def test_queue_meta_ownership_and_mutations_preserve_enqueue_time() -> None:
    slot = _ChatSlot("queue-contract")
    appended_meta = {"source": "append"}
    appended_id = slot.queue_append("appended", meta=appended_meta)
    appended = next(item for item in slot._queue if item["id"] == appended_id)
    assert appended["meta"] is appended_meta
    appended_meta["after_enqueue"] = True
    assert appended["meta"]["after_enqueue"] is True

    inserted_meta = {"source": "insert"}
    inserted_id = slot.queue_insert(0, "inserted", meta=inserted_meta)
    inserted = next(item for item in slot._queue if item["id"] == inserted_id)
    assert inserted["meta"] == inserted_meta
    assert inserted["meta"] is not inserted_meta
    inserted_meta["source"] = "changed outside"
    assert inserted["meta"] == {"source": "insert"}

    slot._last_enqueue_ts = "enqueue-sentinel"
    assert slot.queue_edit_by_id(appended_id, "edited") is True
    assert slot._last_enqueue_ts == "enqueue-sentinel"
    assert slot.queue_promote_by_id(appended_id) is True
    assert slot._last_enqueue_ts == "enqueue-sentinel"
    assert slot.queue_remove_by_id(inserted_id) == "inserted"
    assert slot._last_enqueue_ts == "enqueue-sentinel"
    assert slot.queue_pop() is appended
    assert slot._last_enqueue_ts == "enqueue-sentinel"


def test_to_dict_key_order_and_single_source_scan(monkeypatch) -> None:
    slot = _ChatSlot("projection-contract")
    link = {
        "provider": "github",
        "number": 12,
        "url": "https://github.com/acme/widgets/pull/12",
        "kind": "change",
        "label": "acme/widgets#12",
    }
    calls: list[_ChatSlot] = []

    def source_links(current: _ChatSlot) -> list[dict]:
        calls.append(current)
        return [link]

    monkeypatch.setattr(_ChatSlot, "_pr_source_links", source_links)

    payload = slot.to_dict()

    assert tuple(payload) == _TO_DICT_KEYS
    assert calls == [slot]
    assert payload["source_links"] == [link]
    assert payload["source_links_total"] == 1
