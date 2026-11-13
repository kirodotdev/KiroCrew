"""Saving a Bot's model or effort brings the Bot's own chat back onto the Bot's values.

The Bot's chat can hold a model and effort of its own that outrank the Bot's profile
on every turn, and that chat shows no control for them. A save of the Bot's
``model`` or ``reasoning_effort`` through ``PUT /api/agents/{name}`` (the editor and
the Bot's ``set_model`` card) clears that field's own value in the Bot's own chat,
so the turn's selection (``slot.model or <Bot's model>``, and the effort override
``slot.reasoning_effort or None``) lands on the Bot's value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

BOT = "atlas"
KEY = "member-atlas-0123456789ab"
BOT_MODEL = "claude-opus-5.5"
OLD_MODEL = "claude-opus-4.8"


def _turn_selection(slot, cfg: KiroCrewConfig) -> tuple[str, str | None]:
    """What a turn asks for, as ``turn_admission`` selects it."""
    agent = cfg.agents[BOT]
    return (
        slot.model or agent.model or "",
        slot.reasoning_effort or agent.reasoning_effort or None,
    )


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    """Exercise the agent handler past its owner-auth boundary."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    cfg = KiroCrewConfig.load()
    cfg.agents[BOT] = KiroCrewAgentConfig(
        kiro_agent="kirocrew",
        workspace="default",
        memory_store="default",
        model=BOT_MODEL,
        reasoning_effort="medium",
    )
    cfg.save()
    state = _make_state(tmp_path)
    # A live session, so a clear queues the reset the next turn runs.
    state.sessions.get_provider = MagicMock(return_value=MagicMock())
    state.sessions.refresh_defaults = AsyncMock()
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock(return_value=None))
    chat = state.get_or_create_slot(KEY, agent=BOT, mode="member", model=OLD_MODEL)
    chat.reasoning_effort = "max"
    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app["state"] = state
    return SimpleNamespace(state=state, chat=chat, app=app)


async def _put(app, body) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(f"/api/agents/{BOT}", json=body)
        assert resp.status == 200, await resp.text()


class TestTheEditorSave:
    @pytest.mark.asyncio
    async def test_saving_the_bots_effort_moves_the_chat_onto_it(self, bot):
        await _put(bot.app, {"reasoning_effort": "medium"})

        assert bot.chat.reasoning_effort == ""
        assert _turn_selection(bot.chat, KiroCrewConfig.load())[1] == "medium"
        # The live session started on "max" is restarted at the next turn, not now.
        assert bot.chat._pending_reset_history_key == f"dashboard:{KEY}"
        bot.state.sessions.reset.assert_not_called()
        # Only the saved field: the chat's own model stays.
        assert bot.chat.model == OLD_MODEL

    @pytest.mark.asyncio
    async def test_saving_the_bots_model_moves_the_chat_onto_it(self, bot):
        await _put(bot.app, {"model": BOT_MODEL})

        assert bot.chat.model == ""
        assert _turn_selection(bot.chat, KiroCrewConfig.load())[0] == BOT_MODEL
        assert bot.chat._pending_reset_history_key == f"dashboard:{KEY}"
        bot.state.sessions.reset.assert_not_called()
        assert bot.chat.reasoning_effort == "max"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("record", ["_active_fallback_model", "_refusal_fallback_primary"])
    # The chat's own model equals the Bot's, or it has none and inherits the Bot's.
    @pytest.mark.parametrize("own_model", [BOT_MODEL, ""])
    async def test_a_save_moves_a_chat_off_a_serving_fallback(self, bot, record, own_model):
        # A fallback serves the session, so the wire model is not the Bot's.
        bot.chat.model = own_model
        setattr(bot.chat, record, OLD_MODEL)

        await _put(bot.app, {"model": BOT_MODEL})

        assert bot.chat.model == ""
        assert bot.chat._pending_reset_history_key == f"dashboard:{KEY}"

    @pytest.mark.asyncio
    async def test_a_resave_with_unchanged_values_still_clears(self, bot):
        # The editor sends both fields on every save; these equal the stored ones.
        await _put(bot.app, {"model": BOT_MODEL, "reasoning_effort": "medium", "display_name": ""})

        assert (bot.chat.model, bot.chat.reasoning_effort) == ("", "")
        assert _turn_selection(bot.chat, KiroCrewConfig.load()) == (BOT_MODEL, "medium")

    @pytest.mark.asyncio
    async def test_an_auto_bot_model_leaves_the_chat_inheriting(self, bot):
        await _put(bot.app, {"model": "auto"})

        assert bot.chat.model == ""
        assert KiroCrewConfig.load().agents[BOT].model == ""

    @pytest.mark.asyncio
    async def test_a_model_pick_made_after_the_save_wins_until_the_next_save(self, bot):
        await _put(bot.app, {"model": BOT_MODEL})
        assert bot.chat.model == ""
        # `/model` typed in the chat afterwards.
        bot.chat.model = OLD_MODEL

        await _put(bot.app, {"description": "unrelated"})
        assert _turn_selection(bot.chat, KiroCrewConfig.load())[0] == OLD_MODEL

        await _put(bot.app, {"model": BOT_MODEL})
        assert bot.chat.model == ""

    @pytest.mark.asyncio
    async def test_other_chats_and_the_bots_workers_keep_their_own_picks(self, bot):
        plain = bot.state.get_or_create_slot("chat-1-1", agent=BOT, model=OLD_MODEL)
        plain.reasoning_effort = "max"
        worker = bot.state.get_or_create_slot("chat-2-2", agent=BOT, model=OLD_MODEL)
        worker.reasoning_effort = "max"
        worker._created_by = f"dashboard:{KEY}"

        await _put(
            bot.app,
            {
                "model": BOT_MODEL,
                "reasoning_effort": "medium",
                "worker_model": "auto",
                "worker_reasoning_effort": "low",
            },
        )

        assert (bot.chat.model, bot.chat.reasoning_effort) == ("", "")
        for other in (plain, worker):
            assert (other.model, other.reasoning_effort) == (OLD_MODEL, "max")
            assert not other._pending_reset_history_key

    @pytest.mark.asyncio
    @pytest.mark.parametrize("saved", ["model", "reasoning_effort"])
    async def test_a_save_supersedes_only_its_own_half_of_a_queued_pick(self, bot, saved):
        from kiro_crew.dashboard.session_control_parts.model_switch import PendingModelPick

        # A session_set_model pick queued for the idle chat, carrying both halves,
        # while the chat holds no model or effort of its own.
        bot.chat.model = ""
        bot.chat.reasoning_effort = ""
        pick = PendingModelPick(
            model=OLD_MODEL,
            caller_session_key="dashboard:chat-9-9",
            caller_tab_id="tab",
            caller_fenced=False,
            pick_gen=bot.chat._model_pick_gen,
            reasoning_effort="max",
            effort_gen=bot.chat._effort_pick_gen,
        )
        bot.chat._pending_model_pick = pick

        await _put(bot.app, {saved: BOT_MODEL if saved == "model" else "medium"})

        # The pick stays queued; apply_pending_model_pick drops the half whose
        # generation moved and keeps the other.
        assert bot.chat._pending_model_pick is pick
        model_superseded = bot.chat._model_pick_gen != pick.pick_gen
        effort_superseded = bot.chat._effort_pick_gen != pick.effort_gen
        assert (model_superseded, effort_superseded) == (saved == "model", saved != "model")

    @pytest.mark.asyncio
    async def test_the_factory_is_refreshed_before_the_chat_reset_is_armed(self, bot):
        # A changed effort rebuilds the provider factory; a turn that consumed the
        # chat's reset before that rebuild would restart on the old Bot effort.
        seen: list[tuple[str, str | None]] = []

        async def refresh(*_a, **_kw):
            seen.append((bot.chat.reasoning_effort, bot.chat._pending_reset_history_key))

        bot.state.sessions.refresh_defaults = AsyncMock(side_effect=refresh)

        await _put(bot.app, {"reasoning_effort": "high"})

        assert seen == [("max", None)]
        assert bot.chat.reasoning_effort == ""
        assert bot.chat._pending_reset_history_key == f"dashboard:{KEY}"

    @pytest.mark.asyncio
    async def test_a_changed_model_refreshes_the_defaults_before_the_reset(self, bot):
        # An inheriting Bot model reaches the session through the session manager's
        # own config, so a reset consumed before that config is current would start
        # the chat on the old Bot model.
        seen: list[tuple[str, str | None]] = []

        async def refresh(*_a, **_kw):
            seen.append((bot.chat.model, bot.chat._pending_reset_history_key))

        bot.state.sessions.refresh_defaults = AsyncMock(side_effect=refresh)

        await _put(bot.app, {"model": "auto"})

        assert seen == [(OLD_MODEL, None)]
        assert bot.chat.model == ""
        assert bot.chat._pending_reset_history_key == f"dashboard:{KEY}"

    @pytest.mark.asyncio
    async def test_an_unchanged_save_rebuilds_nothing(self, bot):
        await _put(bot.app, {"model": BOT_MODEL, "reasoning_effort": "medium"})

        bot.state.sessions.refresh_defaults.assert_not_awaited()
        assert (bot.chat.model, bot.chat.reasoning_effort) == ("", "")

    @pytest.mark.asyncio
    async def test_a_worker_pair_save_alone_leaves_the_bots_chat_alone(self, bot):
        await _put(bot.app, {"worker_model": "auto", "worker_reasoning_effort": "low"})

        assert (bot.chat.model, bot.chat.reasoning_effort) == (OLD_MODEL, "max")


class TestAClosedBotChat:
    @pytest.mark.asyncio
    async def test_reopens_on_the_bots_values(self, bot):
        import asyncio

        from kiro_crew import members
        from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

        member_id = "atlas-0123456789ab"
        cfg = KiroCrewConfig.load()
        cfg.agents[BOT].member_id = member_id
        cfg.save()
        key = members.member_slot_key(member_id, "")
        members.write_dm_binding(member_id, member=BOT, slot_key=key, memory_store="")
        log = bot.state.conversation_log
        history_key = members.member_thread_session_alias(member_id, "")
        await asyncio.to_thread(log.append, history_key, "user", "keep this conversation")
        await asyncio.to_thread(
            log.update_metadata,
            history_key,
            {
                "agent": BOT,
                "model": OLD_MODEL,
                "reasoning_effort": "max",
                "closed": True,
                "closed_at": 1.0,
            },
        )
        bot.state._slots.clear()

        await _put(bot.app, {"reasoning_effort": "medium"})

        meta = await asyncio.to_thread(log.get_metadata, history_key)
        assert (meta["model"], meta["reasoning_effort"]) == (OLD_MODEL, "")
        restored = await rehydrate_slot_from_history_async(bot.state, key, adopt_closed=True)
        assert restored is not None
        assert restored.reasoning_effort == ""
