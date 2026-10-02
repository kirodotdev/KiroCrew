"""Tests for ``chat_folder_update`` on the kirocrew-dashboard server.

The tool renames a sidebar folder and sets its icon and color through the
existing ``PATCH /api/chat/folders/{id}`` route. HTTP helpers are patched here;
the endpoint's ownership rule for renames is tested in
``test_chat_folder_ownership.py`` (``test_an_app_cannot_rename_the_persons_folder``
and its neighbours).
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
from kiro_crew.mcp_dashboard import _call_tool_inner
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


def _rows(path: str) -> list[dict]:
    if path == "/api/chat/folders":
        return [dict(f) for f in _FOLDERS]
    if path == "/api/chat/slots":
        return [dict(_CALLER_ROW)]
    raise AssertionError(f"unexpected GET {path}")


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


def _run(args: dict[str, Any], patch_result: dict | None = None) -> tuple[str, Any]:
    with (
        patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
        patch(
            "kiro_crew.mcp_dashboard._patch",
            return_value=patch_result if patch_result is not None else {"id": "x"},
        ) as mock_patch,
    ):
        out = _call_tool_inner("chat_folder_update", args)
    return out, mock_patch


class TestWrites:
    def test_renames_by_path(self) -> None:
        out, mock_patch = _run({"folder": "kirocrew/0811", "name": "Sept"})
        path, body = mock_patch.call_args.args
        assert path == "/api/chat/folders/bbbbbbbbbbbb"
        assert body == {"name": "Sept"}
        assert mock_patch.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert "kirocrew/0811" in out and "renamed to `Sept`" in out

    def test_sets_icon_and_color_by_id(self) -> None:
        out, mock_patch = _run({"folder": "cccccccccccc", "icon": "✈️", "color": "#22C55E"})
        path, body = mock_patch.call_args.args
        assert path == "/api/chat/folders/cccccccccccc"
        # Color is lowercased to match the palette allowlist's spelling.
        assert body == {"icon": "✈️", "color": "#22c55e"}
        assert "icon ✈️" in out and "color #22c55e" in out

    def test_empty_values_clear_back_to_default(self) -> None:
        out, mock_patch = _run({"folder": "Travel", "icon": "", "color": ""})
        assert mock_patch.call_args.args[1] == {"icon": "", "color": ""}
        assert "icon (default)" in out and "color (default)" in out

    def test_body_carries_only_the_three_fields(self) -> None:
        _out, mock_patch = _run(
            {"folder": "Travel", "name": "Trips", "icon": "🧳", "color": "#94a3b8"}
        )
        assert set(mock_patch.call_args.args[1]) == {"name", "icon", "color"}


class TestRefusals:
    def test_json_null_is_treated_as_absent(self) -> None:
        """A null must not reach the endpoint as the string "None"."""
        out, mock_patch = _run({"folder": "Travel", "name": None, "icon": None, "color": "#22c55e"})
        assert mock_patch.call_args.args[1] == {"color": "#22c55e"}
        assert "None" not in out

    def test_all_null_is_nothing_to_change(self) -> None:
        out, mock_patch = _run({"folder": "Travel", "name": None, "icon": None, "color": None})
        assert out.startswith("Error:") and "at least one" in out
        mock_patch.assert_not_called()

    @pytest.mark.parametrize("field", ["project_dir", "default_agent", "steering_dirs"])
    def test_keep_off_fields_are_refused_by_name(self, field: str) -> None:
        value: Any = ["/tmp"] if field == "steering_dirs" else "x"
        with pytest.raises(ValidationError) as exc:
            mcp_dashboard._validate_args("chat_folder_update", {"folder": "Travel", field: value})
        assert exc.value.field == field
        assert "every future session" in exc.value.message

    def test_keep_off_refusal_reaches_the_caller_without_a_write(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = mcp_dashboard._call_tool(
                "chat_folder_update", {"folder": "Travel", "project_dir": "/tmp"}
            )
        assert out.startswith("Error:") and "project_dir" in out
        mock_patch.assert_not_called()

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
        out, mock_patch = _run({"folder": "Travel", "name": "A/B"})
        assert out.startswith("Error:") and "'/'" in out
        mock_patch.assert_not_called()

    def test_nothing_to_change_is_refused(self) -> None:
        out, mock_patch = _run({"folder": "Travel"})
        assert out.startswith("Error:") and "at least one" in out
        mock_patch.assert_not_called()

    def test_root_is_not_a_folder(self) -> None:
        out, mock_patch = _run({"folder": "root", "name": "X"})
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_unknown_folder_is_refused_not_created(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_update", {"folder": "Nope", "name": "X"})
        assert out.startswith("Error:") and "folder not found" in out
        mock_post.assert_not_called()
        mock_patch.assert_not_called()

    def test_name_too_long_after_redaction_is_refused(self) -> None:
        with patch("kiro_crew.mcp_dashboard.redact", side_effect=lambda s: s + "x" * 200):
            out, mock_patch = _run({"folder": "Travel", "name": "short"})
        assert out.startswith("Error:") and "too long after redaction" in out
        mock_patch.assert_not_called()

    def test_unverifiable_caller_is_refused(self) -> None:
        with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""):
            out, mock_patch = _run({"folder": "Travel", "name": "X"})
        assert out.startswith("Error:") and "cannot verify" in out
        mock_patch.assert_not_called()

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
    """The two channel-binding reads the reachability check makes."""

    def __init__(self, *, inbound: bool = False, slack_ts: str | None = None) -> None:
        self.inbound = inbound
        self.slack_ts = slack_ts

    def mirror_accepts_inbound(self, key: str) -> bool:
        return self.inbound

    def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
        return self.slack_ts, ("C1" if self.slack_ts else None)


def _folder_state(folders: list[dict], links: Any = None, linked_key: str = "") -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = folders
    slot = _ChatSlot("chat-1-100")
    slot.linked_session_key = linked_key
    state._slots = {slot.key: slot}
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
    pytest.param({"links": _Links(inbound=True)}, id="resume-binding"),
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
        _tree(), _C["id"], {"parent_id": ""}, internal=True, links=_Links(inbound=True)
    )
    assert status == 200


@pytest.mark.asyncio
async def test_a_failed_binding_lookup_refuses() -> None:
    class _Broken(_Links):
        def mirror_accepts_inbound(self, key: str) -> bool:
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
    tool = next(t for t in mcp_dashboard._tool_definitions() if t["name"] == "chat_folder_update")
    for field in mcp_dashboard._FOLDER_UPDATE_REFUSED_FIELDS:
        assert field in tool["description"]
    assert set(tool["inputSchema"]["properties"]) == {"folder", "name", "icon", "color"}
