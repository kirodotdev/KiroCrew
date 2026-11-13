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

from kiro_crew.dashboard.handlers.files import api_slack_upload_file
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
    # The production route table, not a hand-added route: what these tests post
    # to is the ``/api/channel/upload-file`` the gateway serves.
    from kiro_crew.dashboard.routes import taskrunner

    state = MagicMock(spec=DashboardState)
    state.sessions = MagicMock()
    app = web.Application()
    app["state"] = state
    taskrunner.register(app)
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
async def test_a_description_at_the_cap_reaches_the_transport_in_full(tmp_path, outbox_file):
    """A description at the channel leg's ceiling reaches the transport in full.

    Telegram's caption cap is 1024; a description of exactly 1024 fits, so it is
    not refused and the whole caption reaches ``send_document``."""
    from kiro_crew.telegram.client import TELEGRAM_MAX_CAPTION

    caption = "x" * TELEGRAM_MAX_CAPTION
    payload = {"file_path": str(outbox_file), "filename": "report.txt", "description": caption}
    transport = _transport()
    status, body = await _post_channel(tmp_path, outbox_file, payload, transport)
    assert status == 200 and body["delivered"] is True, body
    kwargs = transport.send_document.call_args.kwargs
    assert kwargs["caption"] == caption, "the whole caption must reach the transport"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type,limit", [("telegram", 1024), ("discord", 2000)])
async def test_the_channel_leg_refuses_a_description_over_its_cap(
    tmp_path, outbox_file, channel_type, limit
):
    """A preamble over the leg's caption cap gets a 400 ``too_long_for_channel``,
    naming the channel and the number -- not a silent slice on the way out."""
    caption = "x" * (limit + 1)
    payload = {"file_path": str(outbox_file), "filename": "report.txt", "description": caption}
    transport = _transport()
    with (
        patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(ChannelLink(channel_type=channel_type, channel_id="42"), transport),
        ),
        patch("kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent),
        patch("kiro_crew.config.loader.workspace_root", return_value=tmp_path),
    ):
        async with TestClient(TestServer(_channel_app())) as client:
            resp = await client.post(
                "/api/channel/upload-file",
                json=payload,
                headers={"X-Session-Key": f"{channel_type}:1"},
            )
            status, body = resp.status, _json_or_text(await resp.text())
    assert status == 400, body
    assert body["code"] == "too_long_for_channel", body
    assert str(limit) in body["error"], body
    assert (
        transport.send_document.call_count == 0
    ), "the file must not ship when the caption is refused"


async def _post_linked(tmp_path, outbox_file, channel_type, caption, transport):
    payload = {"file_path": str(outbox_file), "filename": "report.txt", "description": caption}
    with (
        patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(ChannelLink(channel_type=channel_type, channel_id="42"), transport),
        ),
        patch("kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent),
        patch("kiro_crew.config.loader.workspace_root", return_value=tmp_path),
    ):
        async with TestClient(TestServer(_channel_app())) as client:
            resp = await client.post(
                "/api/channel/upload-file",
                json=payload,
                headers={"X-Session-Key": f"{channel_type}:1"},
            )
            return resp.status, _json_or_text(await resp.text())


@pytest.mark.asyncio
async def test_telegram_counts_the_caption_in_utf16_units(tmp_path, outbox_file):
    """Telegram measures a caption in UTF-16 units, where an emoji is two. A caption
    under the cap in Python characters but over it in UTF-16 units is refused here
    with ``too_long_for_channel``, instead of passing and failing the whole upload
    at Telegram."""
    from kiro_crew.telegram.client import TELEGRAM_MAX_CAPTION

    caption = "a" * (TELEGRAM_MAX_CAPTION - 11) + "\U0001f600" * 6
    assert len(caption) < TELEGRAM_MAX_CAPTION < len(caption.encode("utf-16-le")) // 2
    transport = _transport()
    status, body = await _post_linked(tmp_path, outbox_file, "telegram", caption, transport)
    assert status == 400 and body["code"] == "too_long_for_channel", body
    assert "UTF-16" in body["error"], body
    assert transport.send_document.call_count == 0


@pytest.mark.asyncio
async def test_wecom_delivers_a_caption_over_its_byte_cap(tmp_path, outbox_file):
    """WeCom has no caption cap at this route, as on main: a direct POST with a
    caption past ``WECOM_MAX_REPLY_BYTES`` still delivers the file, and the full
    caption reaches the transport, which cuts its text push itself."""
    from kiro_crew.wecom.client import WECOM_MAX_REPLY_BYTES

    caption = "\u20ac" * (WECOM_MAX_REPLY_BYTES // 3 + 1)
    transport = _transport()
    status, body = await _post_linked(tmp_path, outbox_file, "wecom", caption, transport)
    assert status == 200 and body["delivered"] is True, body
    assert transport.send_document.call_args.kwargs["caption"] == caption


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
