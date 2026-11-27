"""An id check refuses a value that ends in a newline.

In Python ``$`` matches at the end of the string and also just before a final
newline, so ``PATTERN.match(value)`` on a ``^...$`` pattern passes ``value + "\\n"``.
Each check below runs ``fullmatch``; one test per pattern drives a real call
site with a well-formed value (the control) and the same value plus a newline.

``test/test_anchored_pattern_match_ratchet.py`` holds the tree-wide count.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import history_threads, mcp_cron
from kiro_crew.acp import kas_subagent_targets
from kiro_crew.cli_setup import _verify_slack_owner_id
from kiro_crew.dashboard import chat_tags_store, chat_threads
from kiro_crew.dashboard.chat_tags_policy import _DEFAULT_COLOR
from kiro_crew.dashboard.handlers import api_send_message
from kiro_crew.mcp_tools import messaging as mcp_messaging
from kiro_crew.slack import team_ids
from kiro_crew.validation import infer_use_case

MID = "m-" + "0123456789abcdef"
REPLY_ID = "0123456789abcdef" * 2
REPLY = {"id": REPLY_ID, "role": "user", "content": "hello", "ts": "2026-11-26T01:02:03Z"}


def test_thread_mid():
    assert chat_threads._valid_mid(MID)
    assert not chat_threads._valid_mid(MID + "\n")


def test_thread_reply_id():
    assert history_threads._thread_reply_row(dict(REPLY)) is not None
    assert history_threads._thread_reply_row({**REPLY, "id": REPLY_ID + "\n"}) is None


def test_thread_reply_timestamp():
    assert history_threads._thread_reply_row({**REPLY, "ts": REPLY["ts"] + "\n"}) is None


@pytest.fixture
def mcp_core():
    with patch.object(mcp_messaging, "mcp_core") as core:
        core._post.return_value = {"ok": True}
        yield core


def test_channel_id(mcp_core):
    args = {"channel": "C0123ABCD\n", "ts": "1700000000.000100"}
    assert mcp_messaging.delete_message("delete_message", args) == (
        "Error: invalid channel ID format."
    )
    mcp_core._post.assert_not_called()
    mcp_messaging.delete_message("delete_message", {**args, "channel": "C0123ABCD"})
    mcp_core._post.assert_called_once()


def test_channel_id_as_the_default_cron_channel(monkeypatch):
    monkeypatch.setattr(mcp_cron, "_caller_channel_id", lambda: "")
    monkeypatch.setenv("KIROCREW_CHANNEL_ID", "C0123ABCD")
    assert mcp_cron._default_cron_channel() == "C0123ABCD"
    monkeypatch.setenv("KIROCREW_CHANNEL_ID", "C0123ABCD\n")
    assert mcp_cron._default_cron_channel() is None


def test_message_timestamp(mcp_core):
    args = {"channel": "C0123ABCD", "ts": "1700000000.000100\n"}
    assert mcp_messaging.delete_message("delete_message", args) == (
        "Error: invalid message timestamp format."
    )
    mcp_core._post.assert_not_called()


def test_user_id():
    refused, detail = _verify_slack_owner_id("", "U0123ABCD\n")
    assert refused is False
    assert detail.startswith("not a member ID")


def test_slack_thread_timestamp():
    assert infer_use_case("1700000000.000100") == "slack"
    assert infer_use_case("1700000000.000100\n") == "unknown"


def test_model_id():
    entry = {"name": "helper"}
    named = kas_subagent_targets._on_its_own_model(entry, {"model": "model-a.1"})
    assert named == {"name": "helper", "model": "model-a.1"}
    assert kas_subagent_targets._on_its_own_model(entry, {"model": "model-a.1\n"}) == entry


def test_tag_colour():
    assert chat_tags_store._valid_color("#aabbcc") == "#aabbcc"
    assert chat_tags_store._valid_color("#aabbcc\n") == _DEFAULT_COLOR


@pytest.fixture
def team_store():
    kept = team_ids._store
    yield team_ids.use_channel_team_store
    team_ids._store = kept


def test_team_lookup_channel_id(team_store):
    team_store({"C0123ABCD": "T0123ABCD", "C0123ABCD\n": "T0123ABCD"})
    assert team_ids.slack_team_id_for("C0123ABCD") == "T0123ABCD"
    assert team_ids.slack_team_id_for("C0123ABCD\n") == ""


def test_team_lookup_team_id(team_store):
    team_store({"C0123ABCD": "T0123ABCD\n"})
    assert team_ids.slack_team_id_for("C0123ABCD") == ""


async def _send_as(caller_session: str) -> MagicMock:
    """POST /api/send-message as *caller_session*; the Slack client it was offered."""
    state = MagicMock()
    state.slack_client = AsyncMock()
    state.owner_id = "U0123ABCD"
    state.crons.list_jobs.return_value = []
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/send-message", json={"text": "hi", "caller_session": caller_session}
            )
    return state.slack_client


@pytest.mark.asyncio
async def test_cron_session():
    # Only a cron caller gets the owner's Slack DM by default.
    assert (await _send_as("cron:abc123")).open_dm.await_count == 1
    assert (await _send_as("cron:abc123\n")).open_dm.await_count == 0
