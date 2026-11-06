"""Slack config writers leave a non-object ``agent`` / ``slack`` entry as written.

``_set_default_agent`` and the users / channels select handlers each set one
key inside a config section. When that section holds a list, string, number or
``null`` instead of an object, the write must change nothing: the file keeps
its bytes and the in-memory state keeps its value. ``_set_default_agent``
refuses with ``ValueError``, the error its callers already turn into a reply.

Config writes land in the per-test ``KIROCREW_HOME`` that ``conftest.py`` pins.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_interactions_coverage_helpers import _mock_aiohttp_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _restore_module_globals  # noqa: F401
from slack_interactions_coverage_helpers import ix_logs_fixture  # noqa: F401
from slack_interactions_coverage_helpers import orch_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _ix_messages, _payload

from kiro_crew.config.loader import config_path
from kiro_crew.slack import events
from kiro_crew.slack import handler as sh
from kiro_crew.slack import interactions as ix

NOT_OBJECTS = [["x"], "x", 7, None]
NOT_OBJECT_IDS = ["list", "str", "int", "null"]


def _write_config(section: str, value: object) -> bytes:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({section: value, "keep": {"me": 1}}, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return raw


@pytest.mark.parametrize("value", NOT_OBJECTS, ids=NOT_OBJECT_IDS)
def test_set_default_agent_refuses_and_keeps_file(
    value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _write_config("agent", value)
    monkeypatch.setattr(sh, "_cached_default_agent", "previous")
    with pytest.raises(ValueError, match="'agent' is not an object"):
        sh._set_default_agent("demo")
    assert config_path().read_bytes() == before
    assert sh._cached_default_agent == "previous"


@pytest.mark.parametrize("value", NOT_OBJECTS, ids=NOT_OBJECT_IDS)
@pytest.mark.asyncio
async def test_users_select_keeps_file_and_runtime(
    value: object, orch: MagicMock, ix_logs: pytest.LogCaptureFixture
) -> None:
    before = _write_config("slack", value)
    orch._allowed_users = {"Uold"}
    action = {"action_id": "mc_users_select", "selected_users": ["Ua"]}
    await ix._handle_users_select(_payload(), action, "C1", "m1", "U1")
    assert config_path().read_bytes() == before
    assert orch._allowed_users == {"Uold"}
    assert any("'slack' is not an object" in m for m in _ix_messages(ix_logs))


@pytest.mark.parametrize("value", NOT_OBJECTS, ids=NOT_OBJECT_IDS)
@pytest.mark.asyncio
async def test_channels_select_keeps_file_and_runtime(
    value: object, orch: MagicMock, ix_logs: pytest.LogCaptureFixture
) -> None:
    before = _write_config("slack", value)
    orch._tracking_channels = {"Cold"}
    action = {"action_id": "mc_channels_select", "selected_channels": ["Ca"]}
    await ix._handle_channels_select(_payload(), action, "C1", "m1", "U1")
    assert config_path().read_bytes() == before
    assert orch._tracking_channels == {"Cold"}
    assert any("'slack' is not an object" in m for m in _ix_messages(ix_logs))


@pytest.mark.parametrize("arg", ["off", "demo"])
@pytest.mark.asyncio
async def test_slash_agent_replies_with_the_refusal(
    arg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _write_config("agent", ["x"])
    monkeypatch.setattr(sh, "is_owner", lambda uid: True)
    monkeypatch.setattr(sh, "_resolve_agent_name", lambda name, *a: name)
    respond = AsyncMock()
    await events._handle_agent(MagicMock(), "U1", arg, respond)
    assert config_path().read_bytes() == before
    respond.assert_awaited_once()
    assert "'agent' is not an object" in respond.await_args.args[0]
