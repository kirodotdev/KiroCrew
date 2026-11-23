"""The pre-turn model settle keeps a person's model pick.

A model pick the picker took mid-turn commits ``slot.model = ""`` (Auto) and
marks ``_deferred_model_switch_key`` for the boundary to apply. If
``_settle_session_model`` wrote the served (old) model over that ``""``, the
boundary would apply the old model, silently undoing the pick.

A KAS slot set to Auto (the picker's literal ``auto``) whose fresh or reloaded
session comes back on a concrete model is moved to ``auto`` before the turn: at
once for a pick made since the session last ran a turn, and for an older value
only when a known model list judges it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner  # noqa: F401 - binds model_fallback's shared names
from kiro_crew.dashboard.chat_turn.model_fallback import _settle_session_model
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.providers.acp import AcpProvider


class _Client:
    """The provider double: ``client.client._model`` is the served model."""

    class client:  # noqa: N801 - mirrors the real attribute name the provider carries
        _model = "served-model"


async def _settle(slot: _ChatSlot) -> None:
    await _settle_session_model(
        slot,
        _Client(),
        "acp",
        "hello",
        is_new=True,
        resumed=False,
        _is_refusal_retry_turn=False,
        _synthetic_recovery_turn=False,
    )


@pytest.mark.asyncio
async def test_a_deferred_pick_is_not_overwritten_by_the_pre_turn_backfill():
    slot = _ChatSlot("s1")
    slot.model = ""  # the mid-turn Auto pick
    slot._deferred_model_switch_key = "s1"  # marked for the boundary

    await _settle(slot)

    assert slot.model == "", (
        "the pre-turn settle must leave a pending deferred pick alone; "
        f"slot.model is {slot.model!r}"
    )


def _select(values: tuple[str, ...] | list[str], current: str) -> list[dict]:
    return [
        {
            "type": "select",
            "id": "model",
            "currentValue": current,
            "options": [{"value": v, "name": v} for v in values],
        }
    ]


def _session_client(running: str, listed: tuple[str, ...] = (), *, identity: str = ""):
    """A real ``AcpSessionProvider`` around a real KAS handle, as a KAS start makes.

    The dashboard's ``AcpProvider.client`` is this object, not the handle, so the
    settle step reads the handle's Auto verdict only through what it exposes.
    *listed* is the session's select (a reloaded one-entry select when empty),
    *identity* the vault account its request went out under.
    """
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.session_provider import AcpSessionProvider

    runtime = MagicMock()
    runtime.acp_backend = ACP_BACKEND_KAS
    runtime.is_alive.return_value = True
    runtime.send_request = AsyncMock(return_value=1)
    handle = AcpSessionHandle("sess-kas", asyncio.Queue(), runtime)
    handle._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle.request_vault_identity = identity
    handle.store_session_config({"configOptions": _select(listed or (running,), running)})
    return AcpSessionProvider(handle, runtime), handle, runtime


def _provider(backend: str, session: object) -> MagicMock:
    """An AcpProvider double whose session client is *session*."""
    provider = MagicMock(spec=AcpProvider)
    provider.capabilities = capabilities_for(backend)
    provider.is_kas_backend = backend == ACP_BACKEND_KAS
    provider.available_models.return_value = []
    provider.set_model = AsyncMock()
    provider.client = session
    return provider


@pytest.fixture
def _no_recorded_catalog(_floor_monkeypatch):
    from kiro_crew.agent_sdk import kas_model_catalog as catalog

    _floor_monkeypatch.setattr(catalog, "_recorded", None)
    _floor_monkeypatch.setattr(catalog, "_recorded_identity", "")
    return catalog


async def _settle_reloaded(slot: _ChatSlot, provider: MagicMock, *, is_new=False, resumed=True):
    await _settle_session_model(
        slot,
        provider,
        "acp",
        "hello",
        is_new=is_new,
        resumed=resumed,
        _is_refusal_retry_turn=False,
        _synthetic_recovery_turn=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,pick,running,listed,is_new,resumed,moved",
    [
        # A reopened KAS chat picked Auto after it last ran comes back on the
        # model it ran.
        (ACP_BACKEND_KAS, "auto", "claude-haiku-5.5", (), False, True, True),
        # A fresh KAS session that inherited kiro-cli's chat.defaultModel.
        (
            ACP_BACKEND_KAS,
            "auto",
            "claude-opus-5.5",
            ("auto", "claude-opus-5.5"),
            True,
            False,
            True,
        ),
        # kiro-cli keeps what it does without this step.
        (ACP_BACKEND_KIRO, "auto", "claude-haiku-5.5", (), False, True, False),
        # A chat that never picked keeps what the session inherits.
        (ACP_BACKEND_KAS, "", "claude-opus-5.5", (), False, True, False),
        # Already on auto, or a warm session no settle judges.
        (ACP_BACKEND_KAS, "auto", "auto", (), False, True, False),
        (ACP_BACKEND_KAS, "auto", "claude-haiku-5.5", (), False, False, False),
        # A KAS select that leaves auto out is not sent it.
        (ACP_BACKEND_KAS, "auto", "claude-opus-5.5", ("claude-opus-5.5", "x"), True, False, False),
    ],
)
async def test_a_slot_set_to_auto_moves_its_new_or_reloaded_session_to_auto(
    _no_recorded_catalog, backend, pick, running, listed, is_new, resumed, moved
):
    slot = _ChatSlot("s1")
    slot.model = pick
    slot._model_pick_gen += 1  # the pick, made since the session last ran a turn
    session, _handle, _runtime = _session_client(running, listed)
    provider = _provider(backend, session)

    await _settle_reloaded(slot, provider, is_new=is_new, resumed=resumed)

    if moved:
        provider.set_model.assert_awaited_once_with("auto")
    else:
        provider.set_model.assert_not_awaited()
    assert slot.model == pick


@pytest.mark.asyncio
async def test_a_reopened_kas_chat_whose_account_lists_no_auto_is_not_sent_it(
    _no_recorded_catalog, caplog
):
    # A region that serves no auto: KAS takes ``auto`` and then fails every
    # prompt. The reopened chat's one-entry select judges nothing, so only the
    # catalog recorded for its account can say auto is not served.
    served = ["claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5"]
    assert _no_recorded_catalog.record_catalog(
        {"configOptions": _select(served, served[0])}, ACP_BACKEND_KAS, identity="account-a"
    )
    session, handle, runtime = _session_client("claude-opus-5.5", identity="account-a")
    provider = _provider(ACP_BACKEND_KAS, session)

    async def _set_model(model: str) -> None:
        await handle.set_model(model)
        provider.model_pin_refused = handle.model_pin_refused

    provider.set_model = AsyncMock(side_effect=_set_model)
    slot = _ChatSlot("s1")
    slot.model = "auto"
    slot._model_pick_gen += 1  # a pick: judged by the handle itself

    with caplog.at_level("INFO", logger="kiro_crew"):
        await _settle_reloaded(slot, provider)

    assert runtime.send_request.await_args_list == []
    assert handle.served_model == "claude-opus-5.5"
    assert "moved its session off" not in caplog.text
    assert slot.model == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "picked,listed,recorded,sent",
    [
        # After a restart the stored ``auto`` is older than the reloaded session,
        # whose one-entry select and cold catalog judge nothing: left alone.
        (False, (), None, False),
        # A new session (a new conversation, a cold start that creates one) whose
        # own full select lists auto: moved off its inherited model.
        (False, ("auto", "claude-opus-5.5", "claude-sonnet-5.5"), None, True),
        # A reopened chat once this process recorded its account's catalog
        # (another chat started first) that lists auto: moved.
        (False, (), ("auto", "claude-opus-5.5", "claude-sonnet-5.5"), True),
        # The session's select, or its account's catalog, leaves auto out: kept.
        (False, ("claude-opus-5.5", "claude-sonnet-5.5"), None, False),
        (False, (), ("claude-opus-5.5", "claude-sonnet-5.5"), False),
        # A pick made since the session last ran is sent, as a live pick is.
        (True, (), None, True),
    ],
)
async def test_an_older_auto_moves_a_kas_session_only_on_a_judged_list(
    _no_recorded_catalog, picked, listed, recorded, sent
):
    # The provider's session client is the real ``AcpSessionProvider``, which is
    # what a KAS start installs, so the verdict is read the way production reads it.
    if recorded:
        assert _no_recorded_catalog.record_catalog(
            {"configOptions": _select(recorded, recorded[1])},
            ACP_BACKEND_KAS,
            identity="account-a",
        )
    slot = _ChatSlot("s1")
    slot.model = "auto"
    if picked:
        slot._model_pick_gen += 1
    session, _handle, _runtime = _session_client("claude-opus-5.5", listed, identity="account-a")
    provider = _provider(ACP_BACKEND_KAS, session)

    await _settle_reloaded(slot, provider, is_new=bool(listed), resumed=not listed)

    if sent:
        provider.set_model.assert_awaited_once_with("auto")
    else:
        provider.set_model.assert_not_awaited()
    # The turn runs under this pick now, so the next reload reads it as older.
    assert slot._turn_pick_gen == slot._model_pick_gen


def test_the_session_provider_answers_the_handles_auto_verdict(_no_recorded_catalog):
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.agent_sdk.kas_model_catalog import session_lists_auto

    listed = ("auto", "claude-opus-5.5")
    session, handle, _runtime = _session_client("claude-opus-5.5", listed)
    assert session.kas_auto_judged_ids == list(listed)
    assert session_lists_auto(session) is True
    # The judged list is a copy: a reader cannot edit the handle's select.
    session.kas_auto_judged_ids.append("x")
    assert handle._kas_served_ids == list(listed)
    # The handle itself, or the placeholder client before a start, is unknown.
    assert session_lists_auto(handle) is False
    assert session_lists_auto(AcpClient.__new__(AcpClient)) is False
