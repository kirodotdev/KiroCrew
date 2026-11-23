"""A reopened KAS chat's ``model`` select names only the model the session runs.

Measured on kiro-cli 3.4.1 (``kiro-cli acp --agent-engine v3``): ``session/new``
lists every model the account is served, ``session/load`` of the same session
lists one, its current model, and ``session/set_config_option`` on the reloaded
session accepts a model outside that one-entry list. Read as the account's list,
that select refused every other pick on a reopened chat and replaced the
recorded catalog with one row.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpError, AcpModelUnavailable, AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SET_CONFIG_OPTION, MODEL_CONFIG_ID
from kiro_crew.agent_sdk import kas_model_catalog as catalog

#: The vault identity fingerprint the session's requests are stamped with.
_ID = "fingerprint-of-account-a"
#: What a ``session/new`` select lists for the account.
_SERVED = ["auto", "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5"]


@pytest.fixture(autouse=True)
def _cold_catalog(_floor_monkeypatch):
    # The floor's own MonkeyPatch, as in test_kas_model_catalog.py: the catalog
    # is process-wide, so each test starts cold and leaves nothing behind.
    _floor_monkeypatch.setattr(catalog, "_recorded", None)
    _floor_monkeypatch.setattr(catalog, "_recorded_identity", "")


def _session(values: list[str], current: str) -> dict:
    """A KAS session response: no ``models`` object, the list only in the select."""
    return {
        "sessionId": "sess-kas",
        "configOptions": [
            {
                "type": "select",
                "id": "model",
                "category": "model",
                "currentValue": current,
                "options": [{"value": v, "name": v, "description": ""} for v in values],
            }
        ],
    }


#: What ``session/load`` answers for a chat that runs claude-sonnet-5.5.
_RELOADED = _session(["claude-sonnet-5.5"], "claude-sonnet-5.5")


def _handle() -> tuple[AcpSessionHandle, MagicMock]:
    runtime = MagicMock()
    runtime.acp_backend = ACP_BACKEND_KAS
    runtime.is_alive.return_value = True
    runtime.send_request = AsyncMock(return_value=1)
    handle = AcpSessionHandle("sess-kas", asyncio.Queue(), runtime)
    handle._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle.request_vault_identity = _ID
    return handle, runtime


def _sent(runtime: MagicMock) -> list[tuple[str, dict]]:
    return [(c.args[0], c.args[1]) for c in runtime.send_request.await_args_list]


def test_a_reopened_chat_switches_to_a_model_its_load_select_omits() -> None:
    handle, runtime = _handle()
    handle.store_session_config(_RELOADED)
    assert handle.served_model == "claude-sonnet-5.5"
    asyncio.run(handle.set_model("claude-opus-5.5"))
    assert handle.model_pin_refused == ""
    # The model option, never ``session/set_model`` (which kiro-cli 3.4 fails).
    assert _sent(runtime) == [
        (
            METHOD_SET_CONFIG_OPTION,
            {"sessionId": "sess-kas", "configId": MODEL_CONFIG_ID, "value": "claude-opus-5.5"},
        )
    ]
    assert handle.served_model == "claude-opus-5.5"


@pytest.mark.parametrize(
    "responses,sent",
    [
        # A new session's select lists auto.
        ([_session(_SERVED, "claude-opus-5.5")], True),
        # A reopened chat's select names only its current model: not judged.
        ([_RELOADED], True),
        # Already on auto, or on a model not yet known: nothing to move.
        ([_session(_SERVED, "auto")], None),
        ([], None),
        # The session's own full select leaves auto out, before or after a reload.
        ([_session(_SERVED[1:], "claude-opus-5.5")], False),
        ([_session(_SERVED[1:], "claude-opus-5.5"), _RELOADED], False),
    ],
)
def test_auto_is_sent_unless_the_sessions_select_leaves_it_out(
    responses: list[dict], sent: bool | None
) -> None:
    # KAS advertises no list for ``resolve_usable_model`` to find ``auto`` in,
    # so that rule alone drops it unsent.
    handle, runtime = _handle()
    for response in responses:
        handle.store_session_config(response)
    asyncio.run(handle.set_model("auto"))
    if sent:
        assert _sent(runtime) == [
            (
                METHOD_SET_CONFIG_OPTION,
                {"sessionId": "sess-kas", "configId": MODEL_CONFIG_ID, "value": "auto"},
            )
        ]
        assert handle.served_model == "auto"
        assert handle.model_pin_refused == ""
    else:
        assert _sent(runtime) == []
        assert handle.model_pin_refused == ("" if sent is None else "auto")


@pytest.mark.parametrize(
    "recorded,identity,refused",
    [
        # The account's catalog names concrete models and no auto: refused.
        (_SERVED[1:], _ID, True),
        # The catalog lists auto, or belongs to another account: not judged.
        (_SERVED, _ID, False),
        (_SERVED[1:], "fingerprint-of-account-b", False),
    ],
)
def test_auto_on_a_reopened_chat_is_judged_by_the_accounts_recorded_catalog(
    recorded: list[str], identity: str, refused: bool
) -> None:
    # A region that serves no auto: KAS takes ``auto`` and fails every prompt.
    # The reloaded handle's one-entry select leaves ``_kas_served_ids`` empty.
    assert catalog.record_catalog(_session(recorded, recorded[0]), ACP_BACKEND_KAS, identity=_ID)
    handle, runtime = _handle()
    handle.request_vault_identity = identity
    handle.store_session_config(_RELOADED)
    assert handle._kas_served_ids == []
    asyncio.run(handle.set_model("auto"))
    if refused:
        assert _sent(runtime) == []
        assert handle.model_pin_refused == "auto"
        assert handle.served_model == "claude-sonnet-5.5"
    else:
        assert [c[1]["value"] for c in _sent(runtime)] == ["auto"]
        assert handle.model_pin_refused == ""


def test_an_engine_error_on_the_switch_reaches_the_caller_as_itself() -> None:
    handle, runtime = _handle()
    handle.store_session_config(_RELOADED)
    handle._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpError("Internal error", code=-32603)
    )
    with pytest.raises(AcpError) as raised:
        asyncio.run(handle.set_model("claude-opus-5.5"))
    # An engine fault, not an entitlement verdict: no "not available on your account".
    assert not isinstance(raised.value, AcpModelUnavailable)
    assert raised.value.code == -32603
    assert handle.model_pin_refused == ""
    assert [method for method, _ in _sent(runtime)] == [METHOD_SET_CONFIG_OPTION]


def test_a_load_select_keeps_the_served_list_an_earlier_answer_gave() -> None:
    handle, runtime = _handle()
    handle.store_session_config(_session(_SERVED, "auto"))
    handle.store_session_config(_RELOADED)
    assert handle._kas_served_ids == _SERVED
    assert handle.served_model == "claude-sonnet-5.5"
    # A pin the account's list omits is still withheld.
    asyncio.run(handle.set_model("claude-nonexistent-9"))
    assert handle.model_pin_refused == "claude-nonexistent-9"
    assert _sent(runtime) == []


def test_a_load_select_does_not_replace_the_recorded_catalog() -> None:
    handle, _ = _handle()
    handle.store_session_config(_session(_SERVED, "auto"))
    handle.store_session_config(_RELOADED)
    rows = catalog.recorded_catalog_snapshot(_ID)
    assert [row["model_id"] for row in rows or []] == _SERVED


def test_the_catalog_refuses_a_select_naming_only_its_current_model() -> None:
    assert catalog.record_catalog(_RELOADED, ACP_BACKEND_KAS, identity=_ID) is False
    assert catalog.recorded_catalog_snapshot(_ID) is None
    # An ``auto``-only select is not that shape and is recorded as before.
    assert catalog.record_catalog(_session(["auto"], "auto"), ACP_BACKEND_KAS, identity=_ID)
    assert [r["model_id"] for r in catalog.recorded_catalog_snapshot(_ID) or []] == ["auto"]
