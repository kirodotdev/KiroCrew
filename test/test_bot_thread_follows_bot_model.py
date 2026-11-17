"""A Bot's thread with no pick of its own follows the Bot's model.

``slot.model`` holds only a person's pick. A thread that inherits its Bot's model
keeps it empty, so a later change to the Bot's model reaches its next turn, and a
model the engine moves the session onto (a fallback promotion) lasts only until the
session's next fresh start. A person's pick in the thread still wins and still takes
the promotion, as kiro-cli's own client does.

The REAL ``_run_chat`` runs against the mocked session boundary of
``test_chat_send_agent_model_default``.
"""

from __future__ import annotations

import unittest.mock

import pytest
from test_chat_runner_coverage import _complete, _drive, _set_stream, _slot
from test_chat_send_agent_model_default import (
    CREW_PIN,
    SLOT_PIN,
    _config,
    _grant_researcher,
    _requested,
    _turn_state,
)

from kiro_crew.acp.engine_fallback import ModelFallbackNotice
from kiro_crew.acp.types import EVENT_MODEL_FALLBACK, AcpEvent
from kiro_crew.dashboard import chat_runner


@pytest.fixture
def _runner_config():
    """Serve one real config object to every ``KiroCrewConfig.load()`` in the turn."""
    patchers: list = []

    def _use(cfg) -> None:
        patcher = unittest.mock.patch.object(
            chat_runner.KiroCrewConfig, "load", unittest.mock.MagicMock(return_value=cfg)
        )
        patcher.start()
        patchers.append(patcher)

    yield _use
    for p in patchers:
        p.stop()


#: The model kiro-cli moves a refused turn onto and promotes.
ENGINE_TARGET = "claude-fable-5"
#: The Bot's model after its owner changes it in the Bot's editor.
NEW_BOT_MODEL = "claude-opus-6"


def _engine_promotes(client, frm: str, to: str = ENGINE_TARGET) -> None:
    """The next turn: *frm* refuses and kiro-cli answers on *to*, promoted for the session."""
    notices = [
        ModelFallbackNotice(frm, to, "refused", engine="v2"),
        ModelFallbackNotice(frm, to, "refused", promoted=True, engine="v2"),
    ]
    _set_stream(
        client,
        [AcpEvent(kind=EVENT_MODEL_FALLBACK, model_fallback=n) for n in notices] + [_complete()],
    )


def _next_turn(state, client) -> None:
    state.sessions.get_or_create.reset_mock()
    _set_stream(client, [_complete()])


def _bot_thread(tmp_path, _runner_config):
    cfg = _config(tmp_path, crew_model=CREW_PIN, default="auto")
    _runner_config(cfg)
    _grant_researcher(cfg)
    state, client = _turn_state(tmp_path)
    # The backend reports the model it serves, as a live session does.
    client.client._model = CREW_PIN
    slot = _slot()
    slot.agent = "researcher"
    return cfg, state, client, slot


@pytest.mark.asyncio
async def test_an_engine_switch_lasts_until_the_next_fresh_start(tmp_path, _runner_config):
    _cfg, state, client, slot = _bot_thread(tmp_path, _runner_config)
    await _drive(state, slot)
    assert _requested(state) == CREW_PIN

    _next_turn(state, client)
    _engine_promotes(client, CREW_PIN)
    await _drive(state, slot)

    # Nothing was picked in the thread, so nothing is stored for it.
    assert slot.model == ""
    # The session is gone (respawn, eviction, restart); the next one starts on the Bot's model.
    _next_turn(state, client)
    await _drive(state, slot)
    assert _requested(state) == CREW_PIN


@pytest.mark.asyncio
async def test_a_pick_in_a_bot_thread_wins_and_takes_the_promotion(tmp_path, _runner_config):
    _cfg, state, client, slot = _bot_thread(tmp_path, _runner_config)
    slot.model = SLOT_PIN
    client.client._model = SLOT_PIN
    await _drive(state, slot)
    assert _requested(state) == SLOT_PIN

    _next_turn(state, client)
    _engine_promotes(client, SLOT_PIN)
    await _drive(state, slot)

    assert slot.model == ENGINE_TARGET
    _next_turn(state, client)
    await _drive(state, slot)
    assert _requested(state) == ENGINE_TARGET


@pytest.mark.asyncio
async def test_a_bot_model_change_reaches_the_next_turn(tmp_path, _runner_config):
    cfg, state, client, slot = _bot_thread(tmp_path, _runner_config)
    await _drive(state, slot)
    assert slot.model == ""

    # The owner changes the Bot's model while its thread holds a live session.
    cfg.agents["researcher"].model = NEW_BOT_MODEL
    client.has_active_turn = unittest.mock.MagicMock(return_value=False)
    state.sessions.get_provider = unittest.mock.MagicMock(return_value=client)
    state.sessions.reset = unittest.mock.AsyncMock(return_value=True)
    _next_turn(state, client)
    await _drive(state, slot)

    assert _requested(state) == NEW_BOT_MODEL
    resets = [
        c for c in state.sessions.reset.await_args_list if c.args[0] == "dashboard:chat-cov-1"
    ]
    assert resets and all(c.kwargs.get("skip_if_busy") is True for c in resets)
