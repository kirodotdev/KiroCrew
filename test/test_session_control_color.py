"""``session_set_color``: tint the caller itself or a session it created.

The verb gates through ``authorize_target`` with
``allow_self``, then a creator fence for every caller. The tests cover the
reach, the refusal classes, the color grammar, the route and the MCP tool.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _key(slot) -> str:
    return slot_history_key(slot)


def _color(state, caller, target: str, color: str) -> dict:
    return asyncio.run(
        sc.set_color_target(state, caller_session_key=_key(caller), target=target, color=color)
    )


def _owner_and_child(state):
    """An owner (not ownership-fenced) caller and a session it created."""
    caller = state.get_or_create_slot("chat-1")
    child = state.get_or_create_slot("chat-2")
    child._created_by = "chat-1"
    return caller, child


# ── Reach ────────────────────────────────────────────────────────────────────


def test_a_created_session_is_colored_with_a_palette_swatch(tmp_path):
    state = _make_state(tmp_path)
    caller, child = _owner_and_child(state)
    child.color_hex = "#123456"

    out = _color(state, caller, "chat-2", "3")

    assert out == {"ok": True, "target": "chat-2", "color_index": 3}
    # A swatch clears a custom hex, as the menu's PATCH does.
    assert (child.color_index, child.color_hex) == (3, None)
    assert child._dirty is True


def test_a_custom_hex_is_refused_and_nothing_changes(tmp_path):
    """The seven swatches are the whole grammar; the menu's custom cell is not offered."""
    state = _make_state(tmp_path)
    caller, child = _owner_and_child(state)
    child.color_index = 2

    with pytest.raises(sc.SessionControlError) as exc:
        _color(state, caller, "chat-2", "#A1B2C3")

    assert exc.value.code == "invalid_color"
    assert (child.color_index, child.color_hex) == (2, None)


def test_an_empty_color_clears_both_fields(tmp_path):
    state = _make_state(tmp_path)
    caller, child = _owner_and_child(state)
    child.color_index = 4

    _color(state, caller, "chat-2", "")

    assert (child.color_index, child.color_hex) == (None, None)


def test_a_session_may_color_itself(tmp_path):
    """Mutation guard: without ``allow_self`` this is the ``self_target`` refusal."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    # An agent-created worker: its ``_created_by`` names its parent, not itself.
    caller._created_by = "chat-0"

    _color(state, caller, "chat-1", "1")

    assert caller.color_index == 1


def test_an_owner_caller_cannot_color_a_session_it_did_not_create(tmp_path):
    """The fence the lane adds on top of ``authorize_target``: an owner session
    with session control on is NOT ownership-fenced, and ``session_stop`` would
    reach this target. The color verb still refuses it."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    person = state.get_or_create_slot("chat-2")
    assert not sc._caller_is_ownership_fenced(state, "chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        _color(state, caller, "chat-2", "2")

    assert exc.value.code == "not_creator"
    assert (person.color_index, person.color_hex) == (None, None)


def test_a_session_created_by_someone_else_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    sibling = state.get_or_create_slot("chat-2")
    sibling._created_by = "chat-9"

    with pytest.raises(sc.SessionControlError) as exc:
        _color(state, caller, "chat-2", "2")

    assert exc.value.code == "not_creator"
    assert sibling.color_index is None


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda s: setattr(s, "memory_mode", "incognito"), "ephemeral_target"),
        (lambda s: setattr(s, "linked_session_key", "slack:1786300000.000100"), None),
        (lambda s: setattr(s, "_app", "some-app"), "app_scoped_target"),
    ],
)
def test_out_of_bounds_created_targets_are_still_refused(tmp_path, setup, code):
    state = _make_state(tmp_path)
    caller, child = _owner_and_child(state)
    setup(child)

    with pytest.raises(sc.SessionControlError) as exc:
        _color(state, caller, "chat-2", "2")

    if code:
        assert exc.value.code == code
    assert child.color_index is None


def test_a_session_that_is_not_open_is_not_found(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        _color(state, caller, "chat-archived", "1")

    assert exc.value.code == "target_not_found"


# ── Color rules ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "color",
    [
        "7",
        "-1",
        "10",
        " 1",
        "1 ",
        "٣",
        "#abc",
        "#abcdef",
        "#abcdefg",
        "red",
        "rgb(1,2,3)",
        "1\n",
    ],
)
def test_a_color_outside_the_menu_is_refused_before_the_gate(tmp_path, color):
    """Refused before the target lookup, so a bad argument never reads as an
    access decision."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        _color(state, caller, "chat-does-not-exist", color)

    assert exc.value.code == "invalid_color"
    assert exc.value.status == 400


def test_the_palette_size_matches_the_sidebar_menu():
    """The menu draws ``PALETTE_SIZE`` swatches; an agent is held to the same set."""
    ts = (REPO / "website/src/utils/sessionColors.ts").read_text(encoding="utf-8")
    m = re.search(r"export const PALETTE_SIZE = (\d+)", ts)
    assert m is not None
    assert sc.SESSION_PALETTE_SIZE == int(m.group(1))


def _route_request(state, caller, *, internal: bool = True, body: dict):
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/set-color"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )

    async def _json():
        return body

    request.json = _json
    return request


# ── Routes ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("handler", "body"),
    [
        ("api_session_control_set_color", {"target": "chat-2", "color": "1"}),
    ],
)
def test_routes_without_the_secret_are_forbidden(tmp_path, handler, body):
    state = _make_state(tmp_path)
    caller, _ = _owner_and_child(state)
    req = _route_request(state, caller, internal=False, body=body)
    resp = asyncio.run(getattr(handlers_sc, handler)(req))
    assert resp.status == 403


def test_color_route_refuses_a_non_string_color(tmp_path):
    state = _make_state(tmp_path)
    caller, _ = _owner_and_child(state)
    req = _route_request(state, caller, body={"target": "chat-2", "color": 3})
    resp = asyncio.run(handlers_sc.api_session_control_set_color(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_request"


def test_color_route_colors_the_target(tmp_path):
    state = _make_state(tmp_path)
    caller, child = _owner_and_child(state)
    req = _route_request(state, caller, body={"target": "chat-2", "color": "5"})
    resp = asyncio.run(handlers_sc.api_session_control_set_color(req))
    assert resp.status == 200
    assert child.color_index == 5


def test_the_route_is_registered_strict_internal():
    """An unlisted session-control path falls through to cookie auth, and the
    MCP caller's secret is then ignored in production."""
    from kiro_crew.dashboard import server

    assert "/api/session-control/set-color" in server._STRICT_INTERNAL_API_PATHS


# ── MCP tools ────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"


@pytest.mark.parametrize(
    ("resp", "expected"),
    [
        ({"ok": True, "target": "chat-2", "color_index": 2}, "palette swatch 2"),
        ({"ok": True, "target": "chat-2", "color_index": None}, "Cleared"),
    ],
)
def test_color_tool_carries_the_verified_key_and_reports_the_result(resp, expected):
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=resp) as post,
    ):
        out = _call_tool_inner("session_set_color", {"target": "chat-2", "color": "2"})
    assert post.call_args.args[0] == "/api/session-control/set-color"
    assert post.call_args.args[1] == {"target": "chat-2", "color": "2"}
    assert post.call_args.kwargs["session_key"] == _VERIFIED
    assert expected in out


def test_color_tool_sends_an_empty_color_to_clear():
    """An empty color is the clear value; the schema must not refuse it."""
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"ok": True, "target": "chat-2", "color_index": None},
        ) as post,
    ):
        out = _call_tool_inner("session_set_color", {"target": "chat-2", "color": ""})
    assert post.call_args.args[1] == {"target": "chat-2", "color": ""}
    assert "Cleared" in out


@pytest.mark.parametrize("color", ["\u200b", "\u200b1", "1\u200b", "\ufeff", " "])
def test_color_tool_sends_the_raw_value_so_the_route_refuses_it(color):
    """Sanitizing strips invisible characters, so the tool forwards the RAW
    argument: "\\u200b" must reach the route as itself (which refuses it), never
    as "" and a clear."""
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"error": "color must be a sidebar palette swatch index"},
        ) as post,
    ):
        out = _call_tool_inner("session_set_color", {"target": "chat-2", "color": color})
    assert post.call_args.args[1]["color"] == color
    assert out.startswith("Error: could not set that session's color")


@pytest.mark.parametrize("color", ["\u200b", "\ufeff", "\u200b1"])
def test_the_route_refuses_invisible_color_values(tmp_path, color):
    state = _make_state(tmp_path)
    caller, child = _owner_and_child(state)
    child.color_index = 2

    with pytest.raises(sc.SessionControlError) as exc:
        _color(state, caller, "chat-2", color)

    assert exc.value.code == "invalid_color"
    assert child.color_index == 2


@pytest.mark.parametrize("color", ["\u200b", "\ufeff", "\u200b1"])
def test_the_wrapper_carries_the_raw_color_to_the_route(color):
    """The real entry point validates (and sanitizes) before dispatch; the color
    must still reach the route as sent, so "\\u200b" is refused there rather than
    arriving as "" and clearing the tint."""
    from kiro_crew.mcp_dashboard import _call_tool

    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"error": "color must be a sidebar palette swatch index"},
        ) as post,
    ):
        _call_tool("session_set_color", {"target": "chat-2", "color": color})
    assert post.call_args.args[1]["color"] == color


def test_color_tool_refuses_a_missing_color_without_a_request():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post") as post,
    ):
        out = _call_tool_inner("session_set_color", {"target": "chat-2"})
    assert out.startswith("Error: color is required")
    post.assert_not_called()


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("session_set_color", {"target": "chat-2", "color": "1"}),
    ],
)
def test_tools_refuse_an_unverifiable_caller_without_a_request(tool, args):
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_dashboard._post") as post,
    ):
        out = _call_tool_inner(tool, args)
    assert out.startswith("Error:")
    post.assert_not_called()


def test_tools_report_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"error": "this verb reaches only this session and sessions it created"},
        ),
    ):
        color = _call_tool_inner("session_set_color", {"target": "chat-2", "color": "1"})
    assert color.startswith("Error: could not set that session's color:")
