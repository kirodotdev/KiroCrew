"""An agent in a session a channel can drive cannot reshape the person's sidebar.

A conversation resumed from a channel into a dashboard session keeps that session's
``dashboard:`` key, so the folder, tag and board routes refuse on the session's channel
binding, not on the key. The person's browser, and an agent in an unlinked session, still
write. The pin route's half is in ``test_pinned_session_order.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import chat_tags_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state, _make_tags_app

from kiro_crew.dashboard.chat_folders import api_chat_folder_reorder
from kiro_crew.messaging.link import ChannelLink

_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret

_AGENT = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}

#: Each write as ``(app, method, path, body)``, with the status a caller the rule does
#: not refuse gets. ``{alpha}``, ``{beta}`` and ``{tag}`` are filled in from the setup.
_WRITES = {
    "folder-create": ("folders", "POST", "/api/chat/folders", {"name": "Gamma"}, 201),
    "folder-move": ("folders", "PATCH", "/api/chat/folders/{alpha}", {"parent_id": "{beta}"}, 200),
    "folder-reorder": (
        "folders",
        "POST",
        "/api/chat/folders/reorder",
        {"orders": [{"id": "{alpha}", "order": 5}]},
        200,
    ),
    "session-file": ("folders", "PATCH", "/api/chat/slots/a/folder", {"folder_id": "{alpha}"}, 200),
    "tag-create": ("tags", "POST", "/api/chat/tags", {"name": "Fresh"}, 201),
    "tag-rename": ("tags", "PATCH", "/api/chat/tags/{tag}", {"name": "New"}, 200),
    # No tag change, so the agent tag-grants policy has nothing to judge.
    "session-tag": ("tags", "PUT", "/api/chat/slots/a/tags", {"tags": []}, 200),
    "column-create": ("tags", "POST", "/api/chat/tag-columns", {"name": "C", "tag_ids": []}, 201),
    "column-reorder": ("tags", "PUT", "/api/chat/tag-columns/order", {"ids": []}, 200),
    # A column id that does not exist: past the rule, the route answers 404.
    "session-drop": ("tags", "POST", "/api/chat/slots/a/drop", {"column_id": "nope"}, 404),
}


def _fill(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value


def _app(state: Any, which: str) -> web.Application:
    if which == "tags":
        return _make_tags_app(state)
    app = _make_folder_app(state)
    app.router.add_post("/api/chat/folders/reorder", api_chat_folder_reorder)
    return app


def _store(state: Any) -> list[Any]:
    slot = state._slots["a"]
    return [
        [dict(f) for f in state._folders],
        [dict(t) for t in state._tags],
        [dict(c) for c in state._tag_boards],
        (slot.folder_id, list(slot.tags)),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("write", sorted(_WRITES))
@pytest.mark.parametrize("caller", ["linked-agent", "linked-browser", "unlinked-agent"])
async def test_a_channel_reachable_agent_cannot_reshape_the_sidebar(
    tmp_path, monkeypatch, write: str, caller: str
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot("a")
    which, method, path, body, allowed = _WRITES[write]
    async with TestClient(TestServer(_app(state, which))) as client:
        ids = {"alpha": "", "beta": "", "tag": ""}
        if which == "tags":
            made = await client.post("/api/chat/tags", json={"name": "Old"})
            ids["tag"] = (await made.json())["id"]
        else:
            for name in ("alpha", "beta"):
                made = await client.post("/api/chat/folders", json={"name": name.title()})
                ids[name] = (await made.json())["id"]
        link = ChannelLink(channel_type="discord", channel_id="42")
        mirror = MagicMock(return_value=None if caller == "unlinked-agent" else link)
        monkeypatch.setattr(state.sessions, "get_mirror_link", mirror)
        before = _store(state)
        headers = {"X-Session-Key": "dashboard:a"}
        if caller != "linked-browser":
            headers |= _AGENT
        resp = await client.request(
            method, _fill(path, ids), json=_fill(body, ids), headers=headers
        )
        reply = await resp.json()
    if caller == "linked-agent":
        assert resp.status == 403 and reply["code"] == "channel_reachable_caller"
        assert _store(state) == before
    else:
        assert resp.status == allowed, reply
