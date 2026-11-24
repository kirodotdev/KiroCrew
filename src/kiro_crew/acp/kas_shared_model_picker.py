"""The account's shared model picker, read from KAS for the chat model picker.

A Kiro user hides models from the model picker and orders it on Kiro Web or in the
Kiro CLI; the choice is stored per account, and KAS applies it to the ``model``
select it sends a client. A client that declares the ``sharedModelPicker`` client
capability (:func:`with_shared_model_picker`) is sent the whole served list
instead, with the hidden models as the ``modelPickerHidden`` row of its
connection-level configuration view. Kiro Crew declares it so the Bot and role
model pickers keep every served model, and hides the account's list only in the
chat picker, on top of its own ``dashboard.model_picker_hidden_models``. The
account's order is not read: Crew keeps the served order. Nothing is written
back: the account's list is managed where it was made, as the fallback chains are.

The row is read once after ``initialize`` (:func:`request_params`, answered as
``settings``) and followed on each pushed ``_kiro/configuration/state`` of the
connection view (:func:`hidden_models_from_state`). An engine without the
capability states no row, and the chat picker then hides nothing from the
account.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.model_registry import ADVERTISED_MODEL_ID_MAX_CHARS

#: The client capability, under ``clientCapabilities._meta.kiro``.
SHARED_MODEL_PICKER_CAPABILITY = "sharedModelPicker"
#: The client capability that makes the engine push a changed configuration view
#: (``_kiro/configuration/state``); without it the row is read once and never follows.
CONFIGURATION_STATE_CAPABILITY = "configurationState"
#: The connection view's row naming the account's hidden models.
MODEL_PICKER_HIDDEN_KRN = "krn:::setting/modelPickerHidden"
#: The extension method that answers the connection view.
GET_STATE_METHOD = "_kiro/configuration/getState"
#: The notification that pushes a changed view.
STATE_NOTIFICATION = "_kiro/configuration/state"
#: The view this reader asks for and follows.
CONNECTION_OBSERVER = "connection"
#: Longest model id kept, the bound the advertised-id store applies.
_MODEL_ID_MAX_CHARS = ADVERTISED_MODEL_ID_MAX_CHARS
#: Most hidden ids kept from one row.
_HIDDEN_MAX_IDS = 512


def with_shared_model_picker(capabilities: dict[str, Any]) -> dict[str, Any]:
    """A copy of *capabilities* whose ``_meta.kiro`` declares the shared picker.

    ``configurationState`` goes with it: the engine reads the account's picker
    after ``initialize`` and states no ``modelPickerHidden`` row until that read
    lands, so the one read taken at the start may see no row, and the real row
    then arrives as a pushed view. A row that is present is a read row, ``[]``
    included.
    """
    meta = dict(capabilities.get("_meta") or {})
    meta["kiro"] = {
        **(meta.get("kiro") or {}),
        SHARED_MODEL_PICKER_CAPABILITY: True,
        CONFIGURATION_STATE_CAPABILITY: True,
    }
    return {**capabilities, "_meta": meta}


def serves_shared_picker(agent_capabilities: object) -> bool:
    """Whether the engine's ``agentCapabilities`` say it serves the shared picker.

    Both halves are needed, and both live under ``agentCapabilities._meta.kiro``:
    the ``sharedModelPicker`` flag, which says the engine lists every served model
    and states the row for this client, and ``_kiro/configuration/getState`` among
    ``extensionMethods``, which is how the row is read. An older engine has neither.
    """
    if not isinstance(agent_capabilities, dict):
        return False
    meta = agent_capabilities.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    if not isinstance(kiro, dict):
        return False
    methods = kiro.get("extensionMethods")
    return (
        kiro.get(SHARED_MODEL_PICKER_CAPABILITY) is True
        and isinstance(methods, list)
        and GET_STATE_METHOD in methods
    )


def request_params() -> dict[str, str]:
    """The ``_kiro/configuration/getState`` params for the connection view."""
    return {"observer": CONNECTION_OBSERVER}


def hidden_models_from_settings(settings: object) -> list[str] | None:
    """The ``modelPickerHidden`` row's ids, or ``None`` when *settings* states no row.

    ``None`` and ``[]`` differ: an engine that states no row has not applied the
    account's picker, and the chat picker then hides nothing from the account;
    an empty row is an account that hid nothing.
    """
    if not isinstance(settings, list):
        return None
    for setting in settings:
        if isinstance(setting, dict) and setting.get("krn") == MODEL_PICKER_HIDDEN_KRN:
            return _model_ids(setting.get("value"))
    return None


def hidden_models_from_state(params: object) -> list[str] | None:
    """The hidden ids a pushed ``_kiro/configuration/state`` of the connection view carries.

    ``None`` for another view's push, a push with no row, or a malformed one.
    """
    if not isinstance(params, dict):
        return None
    view = params.get("view")
    if not isinstance(view, dict) or view.get("observer") != CONNECTION_OBSERVER:
        return None
    return hidden_models_from_settings(params.get("settings"))


def _model_ids(value: object) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        model_id = item.strip()
        if not model_id or len(model_id) > _MODEL_ID_MAX_CHARS or model_id in seen:
            continue
        seen.add(model_id)
        ids.append(model_id)
        if len(ids) >= _HIDDEN_MAX_IDS:
            break
    return ids
