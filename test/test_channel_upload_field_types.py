"""A channel upload whose file_path, filename or description is not a string gets a 400.

The shared admission gate hands ``filename`` to ``redact()`` and ``file_path`` to
``Path()``, and the channel leg hands ``description`` to ``redact_for_display()``;
each raises ``TypeError`` on a number, list, object, bool or null, which answered
500. An EMPTY description of any type still reads as no description, as before.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.files import api_channel_upload_file, api_slack_upload_file
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.link import ChannelLink

_NOT_STRINGS = [5, True, ["a"], {"a": 1}]


@pytest.fixture
def outbox_file(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "report.txt"
    f.write_text("hello world", encoding="utf-8")
    return f


def _channel_app() -> web.Application:
    state = MagicMock(spec=DashboardState)
    state.sessions = MagicMock()
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/channel/upload-file", api_channel_upload_file)
    return app


def _slack_app(slack) -> web.Application:
    state = MagicMock(spec=DashboardState)
    state.slack_client = slack
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/slack/upload-file", api_slack_upload_file)
    return app


async def _post_channel(tmp_path, outbox_file, payload, transport):
    with (
        patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(ChannelLink(channel_type="telegram", channel_id="42"), transport),
        ),
        patch("kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent),
        patch("kiro_crew.config.loader.workspace_root", return_value=tmp_path),
    ):
        async with TestClient(TestServer(_channel_app())) as client:
            resp = await client.post(
                "/api/channel/upload-file", json=payload, headers={"X-Session-Key": "telegram:1"}
            )
            return resp.status, _json_or_text(await resp.text())


def _json_or_text(text: str) -> dict:
    """The JSON body, or the raw text under ``_text`` (a 500 answers plain text)."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"_text": text}
    return parsed if isinstance(parsed, dict) else {"_text": text}


def _transport():
    transport = MagicMock(spec_set=["send_document"])
    transport.send_document = AsyncMock(return_value="900")
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["file_path", "filename"])
@pytest.mark.parametrize("value", _NOT_STRINGS)
async def test_the_channel_leg_refuses_a_non_string_path_or_name(
    tmp_path, outbox_file, field, value
):
    payload = {"file_path": str(outbox_file), "filename": "report.txt", field: value}
    transport = _transport()
    status, body = await _post_channel(tmp_path, outbox_file, payload, transport)
    assert status == 400, body
    assert body["code"] == "invalid_field_type"
    transport.send_document.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["file_path", "filename"])
@pytest.mark.parametrize("value", [None, 0, False, [], {}])
async def test_an_empty_path_or_name_of_any_type_is_still_missing(
    tmp_path, outbox_file, field, value
):
    """Back-compat: an empty value answered missing_required_fields, and still does."""
    payload = {"file_path": str(outbox_file), "filename": "report.txt", field: value}
    status, body = await _post_channel(tmp_path, outbox_file, payload, _transport())
    assert status == 400
    assert body["code"] == "missing_required_fields"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [5, True, ["a"], {"a": 1}])
async def test_the_channel_leg_refuses_a_non_empty_non_string_description(
    tmp_path, outbox_file, value
):
    payload = {"file_path": str(outbox_file), "filename": "report.txt", "description": value}
    transport = _transport()
    status, body = await _post_channel(tmp_path, outbox_file, payload, transport)
    assert status == 400, body
    assert body == {"error": "description must be a string", "code": "invalid_field_type"}
    transport.send_document.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, False, [], {}, ""])
async def test_an_empty_description_of_any_type_still_delivers(tmp_path, outbox_file, value):
    """Back-compat: ``description or ""`` always read an empty value as no caption."""
    payload = {"file_path": str(outbox_file), "filename": "report.txt", "description": value}
    transport = _transport()
    status, body = await _post_channel(tmp_path, outbox_file, payload, transport)
    assert status == 200 and body["delivered"] is True, body
    transport.send_document.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["file_path", "filename"])
@pytest.mark.parametrize("value", _NOT_STRINGS)
async def test_the_slack_leg_refuses_a_non_string_path_or_name(tmp_path, outbox_file, field, value):
    slack = MagicMock()
    slack.upload_file = AsyncMock()
    payload = {"file_path": str(outbox_file), "filename": "report.txt", field: value}
    with (
        patch("kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent),
        patch("kiro_crew.config.loader.workspace_root", return_value=tmp_path),
    ):
        async with TestClient(TestServer(_slack_app(slack))) as client:
            resp = await client.post("/api/slack/upload-file", json=payload)
            body = _json_or_text(await resp.text())
    assert resp.status == 400, body
    assert body["code"] == "invalid_field_type"
    slack.upload_file.assert_not_called()
