"""Tests for ``chat_folder_update`` on the kirocrew-dashboard server.

The tool renames a sidebar folder and sets its icon and color through the
existing ``PATCH /api/chat/folders/{id}`` route. The tool cases run one
tools/call frame through ``mcp_dashboard.TABLE`` against an in-memory
dashboard; the endpoint cases drive the real handler. The endpoint's ownership
rule for renames is tested in ``test_chat_folder_ownership.py``
(``test_an_app_cannot_rename_the_persons_folder`` and its neighbours).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_dashboard
from kiro_crew.dashboard.chat_folders import api_chat_folder_update
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.validation import ValidationError

_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": ""},
    {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
    {"id": "cccccccccccc", "name": "Travel", "parent_id": ""},
]

_CALLER_ROW = {
    "key": "chat-1-100",
    "title": "Caller",
    "folder_id": "",
    "memory_mode": "incognito",
    "created": "2026-09-14T05:00:00.000001+00:00",
}

_CALLER = Caller.strict("dashboard:chat-1-100")


def _run(
    args: dict[str, Any], patch_result: dict | None = None, caller: Caller = _CALLER
) -> tuple[str, list[DashboardRequest]]:
    """One tools/call frame; returns the reply and every PATCH the tool sent."""
    dash = InMemoryDashboardClient(
        {
            "GET /api/chat/folders": [dict(f) for f in _FOLDERS],
            "GET /api/chat/slots": [dict(_CALLER_ROW)],
            "PATCH /api/chat/folders/{folder}": (
                patch_result if patch_result is not None else {"id": "x"}
            ),
        }
    )
    out = TABLE.call("chat_folder_update", args, ToolContext(dash, caller))
    return out, [r for r in dash.requests if r.method != "GET"]


class TestWrites:
    def test_renames_by_path(self) -> None:
        out, writes = _run({"folder": "kirocrew/0811", "name": "Sept"})
        (req,) = writes
        assert (req.method, req.path) == ("PATCH", "/api/chat/folders/bbbbbbbbbbbb")
        assert req.body == {"name": "Sept"}
        assert req.session_key == "dashboard:chat-1-100"
        assert "kirocrew/0811" in out and "renamed to `Sept`" in out

    def test_sets_icon_and_color_by_id(self) -> None:
        out, writes = _run({"folder": "cccccccccccc", "icon": "✈️", "color": "#22C55E"})
        (req,) = writes
        assert req.path == "/api/chat/folders/cccccccccccc"
        # Color is lowercased to match the palette allowlist's spelling.
        assert req.body == {"icon": "✈️", "color": "#22c55e"}
        assert "icon ✈️" in out and "color #22c55e" in out

    def test_empty_values_clear_back_to_default(self) -> None:
        out, writes = _run({"folder": "Travel", "icon": "", "color": ""})
        assert writes[0].body == {"icon": "", "color": ""}
        assert "icon (default)" in out and "color (default)" in out

    def test_body_carries_only_the_three_fields(self) -> None:
        _out, writes = _run({"folder": "Travel", "name": "Trips", "icon": "🧳", "color": "#94a3b8"})
        assert set(writes[0].body) == {"name", "icon", "color"}


class TestRefusals:
    def test_json_null_is_treated_as_absent(self) -> None:
        """A null must not reach the endpoint as the string "None"."""
        out, writes = _run({"folder": "Travel", "name": None, "icon": None, "color": "#22c55e"})
        assert writes[0].body == {"color": "#22c55e"}
        assert "None" not in out

    def test_all_null_is_nothing_to_change(self) -> None:
        out, writes = _run({"folder": "Travel", "name": None, "icon": None, "color": None})
        assert out.startswith("Error:") and "at least one" in out
        assert writes == []

    @pytest.mark.parametrize("field", ["project_dir", "default_agent", "steering_dirs"])
    def test_keep_off_fields_are_refused_without_a_write(self, field: str) -> None:
        """Not in the schema, so the generic unknown-field refusal covers them."""
        value: Any = ["/tmp"] if field == "steering_dirs" else "x"
        out, writes = _run({"folder": "Travel", field: value})
        assert out.startswith("Error:") and field in out
        assert writes == []

    @pytest.mark.parametrize("field", ["tags", "order", "parent_id", "hidden", "collapsed"])
    def test_other_folder_fields_are_not_in_the_schema(self, field: str) -> None:
        with pytest.raises(ValidationError):
            mcp_dashboard._validate_args("chat_folder_update", {"folder": "Travel", field: "x"})

    def test_the_endpoints_sibling_refusal_is_explained(self) -> None:
        out, _ = _run(
            {"folder": "kirocrew", "name": "Travel"},
            patch_result={
                "error": "a sibling folder already has that name",
                "code": "folder_name_exists",
            },
        )
        assert out.startswith("Error:") and "cannot be told apart by path" in out

    def test_slash_in_name_is_refused(self) -> None:
        out, writes = _run({"folder": "Travel", "name": "A/B"})
        assert out.startswith("Error:") and "'/'" in out
        assert writes == []

    def test_nothing_to_change_is_refused(self) -> None:
        out, writes = _run({"folder": "Travel"})
        assert out.startswith("Error:") and "at least one" in out
        assert writes == []

    def test_root_is_not_a_folder(self) -> None:
        out, writes = _run({"folder": "root", "name": "X"})
        assert out.startswith("Error:")
        assert writes == []

    def test_unknown_folder_is_refused_not_created(self) -> None:
        out, writes = _run({"folder": "Nope", "name": "X"})
        assert out.startswith("Error:") and "folder not found" in out
        assert writes == []

    def test_name_too_long_after_redaction_is_refused(self) -> None:
        with patch("kiro_crew.mcp_dashboard.redact", side_effect=lambda s: s + "x" * 200):
            out, writes = _run({"folder": "Travel", "name": "short"})
        assert out.startswith("Error:") and "too long after redaction" in out
        assert writes == []

    def test_unverifiable_caller_is_refused(self) -> None:
        out, writes = _run(
            {"folder": "Travel", "name": "X"}, caller=Caller.unverified("dashboard:chat-1-100")
        )
        assert out.startswith("Error:") and "cannot verify" in out
        assert writes == []

    def test_endpoint_ownership_refusal_is_explained(self) -> None:
        out, _ = _run(
            {"folder": "Travel", "name": "X"},
            patch_result={
                "error": "this app does not own that folder",
                "code": "folder_not_owned",
            },
        )
        assert out.startswith("Error:") and "only a folder it created" in out

    def test_endpoint_validation_error_surfaces(self) -> None:
        out, _ = _run(
            {"folder": "Travel", "color": "#123456"},
            patch_result={
                "error": "color must be one of the folder palette values",
                "code": "color_invalid",
            },
        )
        assert out.startswith("Error:") and "palette" in out


def test_the_tool_is_blocked_for_channel_agents() -> None:
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS, _blocked_tool_named

    assert "chat_folder_update" in CHANNEL_AGENT_BLOCKED_TOOLS
    assert _blocked_tool_named("kirocrew-dashboard___chat_folder_update") is True


# -- The endpoint's sibling-name rule, decided under the folder-store lock --

_A = {"id": "fldr0000000a", "name": "Alpha", "parent_id": ""}
_B = {"id": "fldr0000000b", "name": "Bravo", "parent_id": ""}
_C = {"id": "fldr0000000c", "name": "Alpha", "parent_id": "fldr0000000b"}


class _Links:
    """The two channel-binding reads ``session_control._has_channel_mirror`` makes."""

    def __init__(self, *, mirror: bool = False, slack_ts: str | None = None) -> None:
        self.mirror = mirror
        self.slack_ts = slack_ts

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        return ChannelLink(channel_type="telegram", channel_id="42") if self.mirror else None

    def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
        return self.slack_ts, ("C1" if self.slack_ts else None)


def _folder_state(folders: list[dict], links: Any = None, linked_key: str = "") -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = folders
    slot = _ChatSlot("chat-1-100")
    slot.linked_session_key = linked_key
    state._slots = {slot.key: slot}
    state.get_slot = state._slots.get
    state.sessions = links if links is not None else _Links()
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    state.mutate_folders = _mutate
    return state


async def _patch_folder(
    folders: list[dict],
    fid: str,
    body: dict,
    *,
    internal: bool,
    links: Any = None,
    linked_key: str = "",
    caller_key: str = "dashboard:chat-1-100",
) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = _folder_state(folders, links, linked_key)

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    headers = {"X-Session-Key": caller_key}
    if internal:
        headers |= {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch(f"/api/chat/folders/{fid}", json=body, headers=headers)
        return resp.status, await resp.json()


def _tree() -> list[dict]:
    return [dict(_A), dict(_B), dict(_C)]


@pytest.mark.asyncio
async def test_an_agent_cannot_rename_onto_a_siblings_name() -> None:
    """Case-folded like the create rule: ' alpha ' still collides with Alpha."""
    folders = _tree()
    status, body = await _patch_folder(folders, _B["id"], {"name": " alpha "}, internal=True)
    assert status == 409 and body["code"] == "folder_name_exists"
    assert next(f for f in folders if f["id"] == _B["id"])["name"] == "Bravo"


@pytest.mark.asyncio
async def test_an_agent_may_recase_its_folders_own_name() -> None:
    folders = _tree()
    status, _ = await _patch_folder(folders, _A["id"], {"name": "ALPHA"}, internal=True)
    assert status == 200
    assert next(f for f in folders if f["id"] == _A["id"])["name"] == "ALPHA"


@pytest.mark.asyncio
async def test_the_rename_rule_is_per_parent() -> None:
    """The nested Alpha may take Bravo's name: Bravo is its parent, not a sibling."""
    folders = _tree()
    status, _ = await _patch_folder(folders, _C["id"], {"name": "Bravo"}, internal=True)
    assert status == 200


@pytest.mark.asyncio
async def test_the_person_may_still_rename_two_folders_alike() -> None:
    folders = _tree()
    status, _ = await _patch_folder(folders, _B["id"], {"name": "Alpha"}, internal=False)
    assert status == 200


@pytest.mark.asyncio
async def test_a_non_name_change_is_not_checked() -> None:
    """A colour change on a folder that already has a twin is not refused."""
    folders = _tree() + [dict(_A, id="fldr0000000d")]
    status, _ = await _patch_folder(folders, _A["id"], {"color": "#22c55e"}, internal=True)
    assert status == 200


# A channel conversation resumed into a dashboard session runs under that
# session's ``dashboard:`` key, so the MCP dispatch check on the key cannot see
# it. The endpoint refuses on what it can see: the session's channel bindings.
_REACHABLE = [
    pytest.param({"links": _Links(mirror=True)}, id="channel-mirror"),
    pytest.param({"links": _Links(slack_ts="1712793600.123456")}, id="slack-thread"),
    pytest.param({"linked_key": "slack:1712793600.123456"}, id="channel-born-slot"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "icon", "color"])
@pytest.mark.parametrize("reach", _REACHABLE)
async def test_an_agent_in_a_channel_reachable_session_cannot_restyle(
    field: str, reach: dict
) -> None:
    folders = _tree()
    value = {"name": "Renamed", "icon": "🚀", "color": "#22c55e"}[field]
    status, body = await _patch_folder(folders, _A["id"], {field: value}, internal=True, **reach)
    assert status == 403 and body["code"] == "channel_reachable_caller"
    assert next(f for f in folders if f["id"] == _A["id"]) == _A


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller_key", ["dashboard:chat-1-100", "dashboard_chat-1-100", "chat-1-100"]
)
@pytest.mark.parametrize("reach", _REACHABLE)
async def test_every_spelling_of_the_caller_key_finds_the_same_slot(
    caller_key: str, reach: dict
) -> None:
    """The PATCH gate resolves the slot the way the work-ledger gate does.

    One session is spelled ``dashboard:chat-X``, ``dashboard_chat-X`` or
    ``chat-X`` depending on the surface; every spelling must reach the same
    refusal, or the two gates answer differently for one session.
    """
    folders = _tree()
    status, body = await _patch_folder(
        folders, _A["id"], {"name": "Renamed"}, internal=True, caller_key=caller_key, **reach
    )
    assert status == 403 and body["code"] == "channel_reachable_caller"
    assert next(f for f in folders if f["id"] == _A["id"]) == _A


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller_key",
    ["channel:slack:C1:1.2", "slack:1712793600.123456", "telegram:42", "discord_99"],
)
async def test_a_channel_caller_is_refused_at_the_endpoint(caller_key: str) -> None:
    """The tool has no dispatch check of its own; the endpoint carries it.

    Per-transport keys (``slack:<ts>``, ``telegram:<id>``) are channel sessions
    too, so the refusal must not rely on the ``channel:`` prefix alone.
    """
    folders = _tree()
    status, body = await _patch_folder(
        folders, _A["id"], {"name": "X"}, internal=True, caller_key=caller_key
    )
    assert status == 403 and body["code"] == "channel_reachable_caller"
    assert next(f for f in folders if f["id"] == _A["id"]) == _A


@pytest.mark.asyncio
@pytest.mark.parametrize("reach", _REACHABLE)
async def test_the_person_may_rename_in_a_channel_reachable_session(reach: dict) -> None:
    status, _ = await _patch_folder(_tree(), _A["id"], {"name": "Mine"}, internal=False, **reach)
    assert status == 200


@pytest.mark.asyncio
async def test_a_move_is_left_to_its_own_tool() -> None:
    """``chat_folder_move`` reparents through the same route and keeps its own channel rule."""
    status, _ = await _patch_folder(
        _tree(), _C["id"], {"parent_id": ""}, internal=True, links=_Links(mirror=True)
    )
    assert status == 200


@pytest.mark.asyncio
async def test_a_failed_binding_lookup_refuses() -> None:
    class _Broken(_Links):
        def get_mirror_link(self, key: str) -> ChannelLink | None:
            raise RuntimeError("session map unreadable")

    status, body = await _patch_folder(
        _tree(), _A["id"], {"name": "X"}, internal=True, links=_Broken()
    )
    assert status == 403 and body["code"] == "channel_reachable_caller"


def test_the_endpoints_channel_refusal_is_explained() -> None:
    out, _ = _run(
        {"folder": "Travel", "name": "Trips"},
        patch_result={"error": "linked", "code": "channel_reachable_caller"},
    )
    assert out.startswith("Error:") and "linked to a channel conversation" in out


def test_description_names_the_refused_fields() -> None:
    tool = next(t for t in mcp_dashboard._list_tools() if t["name"] == "chat_folder_update")
    for field in ("project_dir", "default_agent", "steering_dirs"):
        assert field in tool["description"]
    assert set(tool["inputSchema"]["properties"]) == {"folder", "name", "icon", "color"}
