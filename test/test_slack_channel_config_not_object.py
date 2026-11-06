"""Slack channel and list writers leave a wrong-shaped config entry as written.

``_persist_channel_config`` sets keys under ``slack.channels.<id>``, the
allowlist / tracking helpers add or drop an item in a ``slack.<list>``, and the
config modal replaces ``slack.tracking_channels``. When an entry on that path
holds the wrong JSON type, each writer must keep ``config.json`` byte for byte.
``_persist_channel_config`` and the list helpers raise ``ValueError`` naming
the entry: ``!channel`` answers it in the thread and the channels-modal selects
log it. The list buttons keep their order and let the error end the click, as an
unreadable config does, so none reports a success it did not save. The config
modal logs a warning and writes nothing.

Config writes land in the per-test ``KIROCREW_HOME`` that ``conftest.py`` pins.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from slack_handler_coverage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _clean_handler_state,
    _texts,
    owner_fixture,
    sessions_fixture,
    slack_fixture,
)
from slack_interactions_coverage_helpers import _mock_aiohttp_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _restore_module_globals  # noqa: F401
from slack_interactions_coverage_helpers import ix_logs_fixture  # noqa: F401
from slack_interactions_coverage_helpers import orch_fixture  # noqa: F401
from slack_interactions_coverage_helpers import sel_log_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _ix_messages, _payload

from conftest import MockSlackClient
from kiro_crew.config.loader import config_path
from kiro_crew.slack import handler as sh
from kiro_crew.slack import interactions as ix
from kiro_crew.slack.allowlist import (
    ACTION_ALLOWLIST_APPROVE,
    ACTION_ALLOWLIST_DENY,
    ACTION_TRACK_APPROVE,
    ACTION_TRACK_DENY,
    persist_allowed_user,
    persist_tracking_channel,
)

NOT_OBJECTS = [["x"], "x", 7, None]
NOT_OBJECT_IDS = ["list", "str", "int", "null"]
NOT_LISTS = [{"a": 1}, "x", 7, None]
NOT_LIST_IDS = ["object", "str", "int", "null"]


def _write(doc: dict) -> bytes:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({**doc, "keep": {"me": 1}}, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _channel_docs(value: object) -> list[dict]:
    return [
        {"slack": value},
        {"slack": {"channels": value}},
        {"slack": {"channels": {"C1": value}}},
    ]


@pytest.mark.parametrize("level", [0, 1, 2], ids=["slack", "channels", "channel"])
@pytest.mark.parametrize("value", NOT_OBJECTS, ids=NOT_OBJECT_IDS)
def test_persist_channel_config_refuses_and_keeps_file(value: object, level: int) -> None:
    before = _write(_channel_docs(value)[level])
    with pytest.raises(ValueError, match="is not an object"):
        sh._persist_channel_config("C1", activation="always")
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("cmd", ["!channel observe", "!channel agent off"])
@pytest.mark.asyncio
async def test_channel_command_replies_with_the_refusal(
    cmd: str,
    slack: MockSlackClient,
    sessions: MagicMock,
    owner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _write({"slack": ["x"]})
    reload = MagicMock()
    monkeypatch.setattr(sh, "_reload_orch_cfg", reload)
    await sh._handle_slash_command(cmd, slack, sessions, "C1", "t1", "msg1", "t1", "U1", None)
    assert config_path().read_bytes() == before
    assert "'slack' is not an object" in _texts(slack)
    reload.assert_not_called()


@pytest.mark.asyncio
async def test_channels_modal_handlers_keep_file_and_log(
    orch: MagicMock, ix_logs: pytest.LogCaptureFixture, sel_log: MagicMock
) -> None:
    before = _write({"slack": {"channels": "x"}})
    await ix._handle_ch_activation(
        _payload(), {"action_id": "mc_ch_activation_C7", "selected_option": {"value": "always"}}
    )
    await ix._handle_ch_agent(
        _payload(), {"action_id": "mc_ch_agent_C7", "selected_option": {"value": "demo"}}
    )
    assert config_path().read_bytes() == before
    logged = [m for m in _ix_messages(ix_logs) if "'slack.channels' is not an object" in m]
    assert len(logged) == 2
    sel_log.log_api_access.assert_not_called()


@pytest.mark.parametrize("value", NOT_OBJECTS, ids=NOT_OBJECT_IDS)
@pytest.mark.parametrize("remove", [False, True], ids=["add", "remove"])
def test_list_helpers_refuse_when_slack_is_not_an_object(value: object, remove: bool) -> None:
    before = _write({"slack": value})
    with pytest.raises(ValueError, match="'slack' is not an object"):
        persist_allowed_user("U1", remove=remove)
    with pytest.raises(ValueError, match="'slack' is not an object"):
        persist_tracking_channel("C1", remove=remove)
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("value", NOT_LISTS, ids=NOT_LIST_IDS)
@pytest.mark.parametrize("remove", [False, True], ids=["add", "remove"])
def test_list_helpers_refuse_when_the_list_is_not_a_list(value: object, remove: bool) -> None:
    before = _write({"slack": {"allowed_users": value, "tracking_channels": value}})
    with pytest.raises(ValueError, match="'slack.allowed_users' is not a list"):
        persist_allowed_user("U1", remove=remove)
    with pytest.raises(ValueError, match="'slack.tracking_channels' is not a list"):
        persist_tracking_channel("C1", remove=remove)
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("value", NOT_OBJECTS, ids=NOT_OBJECT_IDS)
@pytest.mark.asyncio
async def test_config_modal_keeps_file_and_runtime(
    value: object, orch: MagicMock, ix_logs: pytest.LogCaptureFixture
) -> None:
    before = _write({"slack": value})
    orch._tracking_channels = {"Cold"}
    payload = {
        "user": {"id": "U1"},
        "view": {
            "state": {
                "values": {"channels_block": {"mc_config_channels": {"selected_channels": ["Ca"]}}}
            }
        },
    }
    await ix._handle_config_submission(payload)
    assert config_path().read_bytes() == before
    assert orch._tracking_channels == {"Cold"}
    assert any("'slack' is not an object" in m for m in _ix_messages(ix_logs))


@pytest.mark.parametrize(
    "action_id,value",
    [
        (ACTION_ALLOWLIST_APPROVE, "U9:Nine"),
        (ACTION_ALLOWLIST_DENY, "Uold:Old"),
        (ACTION_TRACK_APPROVE, "C9:general"),
        (ACTION_TRACK_DENY, "Cold:old"),
    ],
    ids=["allow-approve", "allow-deny", "track-approve", "track-deny"],
)
@pytest.mark.asyncio
async def test_approval_buttons_fail_with_the_refusal(
    action_id: str, value: str, orch: MagicMock, sel_log: MagicMock
) -> None:
    before = _write({"slack": "x"})
    handler = ix._handle_allowlist if "allowlist" in action_id else ix._handle_track_channel
    with pytest.raises(ValueError, match="'slack' is not an object"):
        await handler(_payload(), {"value": value}, action_id, "C1", "m1", "U1")
    assert config_path().read_bytes() == before
    sel_log.log_api_access.assert_not_called()
    orch.slack.update_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_buttons_fail_with_the_refusal(orch: MagicMock, sel_log: MagicMock) -> None:
    before = _write({"slack": {"allowed_users": 7, "tracking_channels": 7}})
    orch._allowed_users = {"Uold"}
    orch._tracking_channels = {"Cold"}
    clicks = [
        ix._handle_ch_add(_payload(), {"selected_conversation": "C9"}),
        ix._handle_ch_remove(_payload(), {"value": "Cold"}),
        ix._handle_allowlist_remove(_payload(), {"value": "Uold"}, "C1", "m1", "U1"),
        ix._handle_channel_remove(_payload(), {"value": "Cold"}, "C1", "m1", "U1"),
    ]
    for click in clicks:
        with pytest.raises(ValueError, match="is not a list"):
            await click
    assert config_path().read_bytes() == before
    sel_log.log_api_access.assert_not_called()
    orch.slack.update_message.assert_not_awaited()
    orch.slack.views_update.assert_not_awaited()
