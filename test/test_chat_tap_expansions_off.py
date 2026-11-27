"""A chat option tap's label reaches the model as written.

The label is model-authored, so the dashboard turn it starts -- at once, from the
queue, or as a recovery retry -- expands no ``@prompt``, ``$skill`` or leading quick
prompt; a typed message keeps all three.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks, pin_kiro_cli_harness

# Imported before any test patches the runner: a first import under the patch would
# bind the stub ``_run_chat`` by name and keep it after the patch is undone.
from kiro_crew.dashboard import chat_handlers  # noqa: F401
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.session_control import containment_meta

pytestmark = pytest.mark.asyncio

_LABEL = "@release-notes then $deploy"
#: The queue entry's meta key, spelled out: the durable queue file keeps it across a restart.
_STAMP = "expansions_off"


def _state(tmp_path, monkeypatch):
    pin_kiro_cli_harness()
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _client(stream) -> MagicMock:
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.stream = client.stream_command = stream
    client.shutdown = AsyncMock()
    return client


@pytest.mark.parametrize("off", [True, False])
async def test_a_tap_turn_sends_its_label_as_written(tmp_path, monkeypatch, off) -> None:
    """A marked turn expands no ``@prompt`` or ``$skill`` and asks the context build
    to leave a leading quick prompt as written; an unmarked turn does all three."""
    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory import MemoryStore
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
    from kiro_crew.skills import SkillsLoader

    state = _state(tmp_path, monkeypatch)
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    builder.build_message = MagicMock(return_value=(_LABEL, None))
    builder.ensure_store = AsyncMock(return_value=object())
    state.context_builder = builder
    expanded: list[str] = []

    async def _prompt(message, _state, _slot, *, project=None):
        expanded.append("prompt")
        return message, "not_found"

    def _skills(message, _state, _slot, _key, project=None):
        expanded.append("skill")
        return message, 0

    async def _stream(_msg):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    monkeypatch.setattr(cr, "_expand_prompt_mention_off_loop", _prompt)
    monkeypatch.setattr(cr, "_expand_dollar_skills", _skills)
    monkeypatch.setattr(cr, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(cr, "generate_session_summary", AsyncMock())
    state.sessions.get_or_create = AsyncMock(return_value=(_client(_stream), True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()

    await asyncio.wait_for(
        cr._run_chat(state, state.get_or_create_slot("s1"), _LABEL, _expansions_off=off), 30
    )
    await asyncio.wait_for(drain_background_tasks(state), 30)

    assert expanded == ([] if off else ["prompt", "skill"])
    assert builder.build_message.call_args.args[0] == _LABEL
    assert builder.build_message.call_args.kwargs["quick_prompts"] is (not off)


async def test_the_drain_runs_a_stamped_entry_with_expansions_off(tmp_path, monkeypatch) -> None:
    """The busy hand-off's stamp reaches the turn; an unstamped entry carries nothing."""
    state = _make_state(tmp_path)
    state.subagents = None
    captured: list[dict] = []

    def _stub_run_chat(_state, _slot, _prompt, **kwargs):
        captured.append(kwargs)

        async def _done():
            return None

        return _done()

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
    monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)
    for name, stamp in (("chat-1", {_STAMP: True}), ("chat-2", {})):
        slot = state.get_or_create_slot(name)
        slot.queue_append(
            _LABEL,
            meta={**containment_meta(state, slot), **stamp},
            directive_user_origin=True,
            directive_channel_origin=True,
        )
        assert await cr._start_next_queued_turn(state, slot) is True

    assert captured[0].get("_expansions_off") is True
    assert "_expansions_off" not in captured[1]


async def test_a_merge_run_never_folds_typed_words_into_a_tap(tmp_path) -> None:
    """A typed ``$skill`` queued before a tap runs as its own turn, so the tap's
    stamp never keeps the typed mentions as text."""
    from kiro_crew.dashboard.chat_utils import _dequeue_next_message

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    for content, stamp in (("$deploy staging", {}), (_LABEL, {_STAMP: True})):
        slot.queue_append(
            content,
            meta={**containment_meta(state, slot), **stamp},
            directive_user_origin=True,
            directive_channel_origin=True,
        )

    _text, typed = _dequeue_next_message(slot, merge_enabled=True)
    assert [item["content"] for item in typed] == ["$deploy staging"]
    _text, tap = _dequeue_next_message(slot, merge_enabled=True)
    assert [item["content"] for item in tap] == [_LABEL]


async def test_an_edit_that_changes_a_taps_words_lets_them_expand(tmp_path) -> None:
    """The editor's words are typed, so the stamp goes; an edit keeping the label keeps it."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    kept, edited = (
        slot.queue_append(
            _LABEL,
            meta={**containment_meta(state, slot), _STAMP: True},
            directive_user_origin=True,
            directive_channel_origin=True,
        )
        for _ in range(2)
    )
    assert slot.queue_edit_by_id(kept, _LABEL, directive_user_origin=True) is True
    assert slot.queue_edit_by_id(edited, "$deploy staging", directive_user_origin=True) is True

    stamps = {item["id"]: (item.get("meta") or {}).get(_STAMP) for item in slot._queue}
    assert stamps == {kept: True, edited: None}


@pytest.mark.parametrize("off", [True, False])
async def test_a_recovery_requeue_keeps_the_stamp(tmp_path, monkeypatch, off) -> None:
    """A tap turn that dies before output is requeued verbatim; the retry keeps
    the label's mentions as text, and a typed turn's retry still expands them."""
    from kiro_crew.acp.client import AcpError

    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")

    async def _die(_msg):
        raise AcpError("ACP process exited (code=-15)")
        yield  # an async generator

    async def _no_drain(_state, _slot, **_kwargs) -> bool:
        return False

    state.sessions.get_or_create = AsyncMock(return_value=(_client(_die), True, False))
    state.sessions.reset = AsyncMock()
    monkeypatch.setattr(cr, "_start_next_queued_turn", _no_drain)

    await asyncio.wait_for(cr._run_chat(state, slot, _LABEL, _expansions_off=off), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)

    requeued = [q for q in slot._queue if q.get("content") == _LABEL]
    assert requeued, f"expected the verbatim recovery requeue, queue={slot._queue}"
    assert (requeued[0].get("meta") or {}).get(_STAMP) is (True if off else None)
