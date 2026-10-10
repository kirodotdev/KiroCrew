"""``chat_folder_steering_set`` — the agent surface for a folder's steering dirs.

The dashboard's Folder settings → Additional steering writes ``steering_dirs``
through ``PATCH /api/chat/folders/{id}``. This tool is the same write from an
agent, so what these tests pin is that it is the SAME write: one PATCH carrying
only that field under the verified caller key, with every verdict about the
paths (validity, the person-only principal gate, the Windows refusal) left to
the endpoint and surfaced verbatim. The one rule the tool adds — a non-empty
list only from a ``dashboard:`` caller — is pinned here too, along with the
tree's read half and the channel-agent containment.

Each case runs one tools/call frame through ``mcp_dashboard.TABLE`` against an
in-memory dashboard, as ``test_mcp_dashboard_folders.py`` does; the endpoint's
own behaviour is ``test_folder_steering_principal_gate.py`` and
``test_dashboard_chat.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.mcp_dashboard import STEERING_APPROVAL_CLIENT_TIMEOUT, TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_STEERED = ["/srv/standards/org", "/srv/standards/python"]

_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": "", "steering_dirs": _STEERED[:1]},
    {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
    {"id": "cccccccccccc", "name": "Travel", "parent_id": ""},
]

_CALLER_ROW = {"key": "chat-1-100", "title": "Caller", "folder_id": ""}

#: The person's own dashboard tab, verified by the gateway.
_CALLER = Caller.strict("dashboard:chat-1-100")

_TOOL = "chat_folder_steering_set"
_PATCH_ROUTE = "PATCH /api/chat/folders/{folder}"


def _call(
    args: dict,
    patch_reply: Any = None,
    *,
    caller: Caller = _CALLER,
    caller_row: dict | None = None,
    tool: str = _TOOL,
) -> tuple[str, InMemoryDashboardClient]:
    """One tools/call frame; the slot list carries *caller_row* as the caller's own row."""
    routes: dict[str, Any] = {
        "GET /api/chat/folders": [dict(f) for f in _FOLDERS],
        "GET /api/chat/slots": [dict(_CALLER_ROW if caller_row is None else caller_row)],
        _PATCH_ROUTE: {} if patch_reply is None else patch_reply,
    }
    dash = InMemoryDashboardClient(routes)
    return TABLE.call(tool, args, ToolContext(dash, caller)), dash


def _patches(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    return dash.sent(_PATCH_ROUTE)


def _read_paths(dash: InMemoryDashboardClient) -> set[str]:
    return {r.path for r in dash.requests if r.method == "GET"}


class TestTheWriteIsTheEndpointsWrite:
    def test_one_patch_carrying_only_steering_dirs_under_the_verified_key(self) -> None:
        out, dash = _call(
            {"folder": "kirocrew/0811", "steering_dirs": _STEERED},
            {"id": "bbbbbbbbbbbb", "name": "0811", "steering_dirs": _STEERED},
        )
        (sent,) = _patches(dash)
        assert sent.path == "/api/chat/folders/bbbbbbbbbbbb"
        assert sent.body == {"steering_dirs": _STEERED}
        assert sent.session_key == "dashboard:chat-1-100"
        # The endpoint holds the request open for the person's approval.
        assert sent.timeout == STEERING_APPROVAL_CLIENT_TIMEOUT
        assert "kirocrew/0811" in out
        for entry in _STEERED:
            assert entry in out

    def test_accepts_the_folder_by_id(self) -> None:
        _out, dash = _call(
            {"folder": "cccccccccccc", "steering_dirs": _STEERED[:1]},
            {"id": "cccccccccccc", "steering_dirs": _STEERED[:1]},
        )
        assert [r.path for r in _patches(dash)] == ["/api/chat/folders/cccccccccccc"]

    def test_the_echo_is_what_the_endpoint_stored_not_what_was_sent(self) -> None:
        """The endpoint resolves each entry (realpath); later chats read THAT."""
        out, _dash = _call(
            {"folder": "Travel", "steering_dirs": ["/srv/link"]},
            {"id": "cccccccccccc", "steering_dirs": ["/srv/real"]},
        )
        assert "/srv/real" in out
        assert "/srv/link" not in out

    def test_an_empty_list_clears_and_says_so(self) -> None:
        out, dash = _call(
            {"folder": "kirocrew", "steering_dirs": []},
            {"id": "aaaaaaaaaaaa", "name": "kirocrew"},
        )
        (sent,) = _patches(dash)
        assert sent.body == {"steering_dirs": []}
        assert "Cleared" in out

    def test_root_is_not_a_folder(self) -> None:
        out, dash = _call({"folder": "root", "steering_dirs": _STEERED})
        assert out.startswith("Error:")
        assert not _patches(dash)

    def test_an_unknown_folder_never_reaches_the_endpoint(self) -> None:
        out, dash = _call({"folder": "nope/never", "steering_dirs": _STEERED})
        assert out.startswith("Error:")
        assert not _patches(dash)


class TestEveryPathVerdictIsTheEndpoints:
    """No re-derivation: the tool surfaces the endpoint's refusal and stops."""

    def test_the_principal_gate_refusal_is_surfaced_with_the_ui_route(self) -> None:
        out, _dash = _call(
            {"folder": "Travel", "steering_dirs": _STEERED},
            {
                "error": "steering_dirs may be declared only from the person's own session",
                "code": "steering_dirs_forbidden",
            },
        )
        assert out.startswith("Error: steering_dirs may be declared only")
        assert "Folder settings" in out

    def test_an_invalid_path_verdict_is_surfaced_verbatim(self) -> None:
        out, _dash = _call(
            {"folder": "Travel", "steering_dirs": ["/nope"]},
            {
                "error": "steering_dirs must be an existing directory",
                "code": "steering_dirs_invalid",
            },
        )
        assert out == "Error: steering_dirs must be an existing directory"

    def test_no_local_existence_or_sensitivity_check_precedes_the_write(self) -> None:
        """A path that does not exist here still goes to the endpoint.

        The gateway is the process that owns the verdict (it reads the tree, and
        it knows the Windows and sensitive-path rules); a tool-side stat would be
        a second copy that drifts.
        """
        _out, dash = _call(
            {"folder": "Travel", "steering_dirs": ["/definitely/not/here"]},
            {"id": "cccccccccccc", "steering_dirs": ["/definitely/not/here"]},
        )
        assert len(_patches(dash)) == 1


_CHANNEL_CALLER = Caller.strict("slack:C0AMVG4AVE1:1790259905.866239")


class TestOnlyTheDashboardMayDeclare:
    def test_a_channel_bound_caller_cannot_declare(self) -> None:
        out, dash = _call({"folder": "Travel", "steering_dirs": _STEERED}, caller=_CHANNEL_CALLER)
        assert out.startswith("Error:")
        assert "dashboard" in out
        assert not _patches(dash)

    def test_a_channel_bound_caller_cannot_clear_either(self) -> None:
        """An empty list is not an exemption from the dashboard-only rule.

        The endpoint's principal gate lets ANY principal clear (an empty list only
        removes reads), and a channel-bound, app-less caller reaches it with the
        person's authority. Were the tool to skip its own check on ``[]``, that
        caller could erase a folder's stored steering -- config with no prior
        value retained, whose loss shows only as later chats silently missing
        the documents. The containment list does not cover it: it fires at the
        permission event, and an auto-approved MCP call never emits one. So the
        refusal is keyed on the caller alone, and nothing is read or written.
        """
        out, dash = _call({"folder": "Travel", "steering_dirs": []}, caller=_CHANNEL_CALLER)
        assert out.startswith("Error:")
        assert "cleared" in out
        assert not _patches(dash)
        # Only the slot list is read (the identity gate's, then the tool's own
        # shape check); the folder list is never fetched, so the refusal
        # precedes every read about the target.
        assert _read_paths(dash) == {"/api/chat/slots"}

    def test_the_dashboard_caller_may_clear(self) -> None:
        """The contrast case: the same empty list from the person's tab is the clear."""
        out, dash = _call({"folder": "Travel", "steering_dirs": []}, {"id": "cccccccccccc"})
        assert "Cleared" in out
        (sent,) = _patches(dash)
        assert sent.body == {"steering_dirs": []}
        assert sent.session_key == "dashboard:chat-1-100"
        # The endpoint holds the request open for the person's approval.
        assert sent.timeout == STEERING_APPROVAL_CLIENT_TIMEOUT

    def test_an_unverifiable_caller_is_refused_before_any_read(self) -> None:
        out, dash = _call({"folder": "Travel", "steering_dirs": []}, caller=Caller.unverified(""))
        assert out.startswith("Error: cannot verify which session is calling")
        assert not dash.requests

    def test_a_delegated_caller_is_refused(self) -> None:
        out, dash = _call(
            {"folder": "Travel", "steering_dirs": _STEERED},
            caller=Caller.strict("subagent:abc123"),
        )
        assert out.startswith("Error:")
        assert not _patches(dash)

    def test_the_verb_is_on_the_channel_agent_containment_list(self) -> None:
        from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS, _blocked_tool_named

        assert "chat_folder_steering_set" in CHANNEL_AGENT_BLOCKED_TOOLS
        assert _blocked_tool_named("kirocrew-dashboard___chat_folder_steering_set")


class TestOnlyThePersonMayClearToo:
    """The endpoint's asymmetry is not inherited: a non-person principal clears nothing.

    ``_refuse_principal_steering_dirs`` refuses an app or member a NON-EMPTY list
    but lets it clear a folder it owns -- and only the person could have put
    steering there. So an app-owned or member-owned dashboard session sending
    ``[]`` would erase the person's declaration with no prior value retained.
    The tool refuses both principals for set and clear, before touching the
    folder list or writing anything.
    """

    @pytest.mark.parametrize("dirs", [_STEERED, []], ids=["set", "clear"])
    def test_an_app_owned_dashboard_session_is_refused(self, dirs: list) -> None:
        row = {**_CALLER_ROW, "app": "acme-widgets"}
        out, dash = _call({"folder": "Travel", "steering_dirs": dirs}, caller_row=row)
        assert out.startswith("Error:")
        assert "acme-widgets" in out
        assert "only the person" in out
        assert not _patches(dash)
        # Refused on the identity gate's own scope: the folder list is never read.
        assert "/api/chat/folders" not in _read_paths(dash)

    @pytest.mark.parametrize("dirs", [_STEERED, []], ids=["set", "clear"])
    def test_a_crew_member_session_is_refused(self, dirs: list) -> None:
        row = {**_CALLER_ROW, "mode": "member"}
        out, dash = _call({"folder": "Travel", "steering_dirs": dirs}, caller_row=row)
        assert out.startswith("Error:")
        assert "crew member" in out
        assert not _patches(dash)
        assert "/api/chat/folders" not in _read_paths(dash)

    def test_the_persons_own_tab_still_clears(self) -> None:
        """The contrast: no ``app``, not ``member`` mode, no link -- the clear lands."""
        row = {**_CALLER_ROW, "app": "", "mode": ""}
        out, dash = _call(
            {"folder": "Travel", "steering_dirs": []}, {"id": "cccccccccccc"}, caller_row=row
        )
        assert "Cleared" in out
        assert len(_patches(dash)) == 1


class TestAgentAuthoredPathsAreScreened:
    LEAKY = "AKIAIOSFODNN7EXAMPLE"

    def test_a_credential_shaped_entry_is_refused_not_rewritten(self) -> None:
        """A redacted path names a different directory, so refuse, never store."""
        out, dash = _call({"folder": "Travel", "steering_dirs": [f"/srv/{self.LEAKY}/steering"]})
        assert out.startswith("Error:")
        assert self.LEAKY not in out
        assert not _patches(dash)


class TestTheSchemaMirrorsTheEndpoint:
    """A schema refusal is the frame's ``Error:`` reply, sent before any request."""

    @pytest.mark.parametrize(
        "args",
        [
            # steering_dirs is required, so an omission is not a clear.
            pytest.param({"folder": "Travel"}, id="steering-dirs-required"),
            pytest.param({"steering_dirs": []}, id="folder-required"),
            pytest.param({"folder": "Travel", "steering_dirs": [1]}, id="non-string-entry"),
        ],
    )
    def test_a_malformed_call_is_refused_before_any_request(self, args: dict) -> None:
        out, dash = _call(args)
        assert out.startswith("Error:")
        assert not dash.requests

    def test_the_count_and_length_caps_are_the_endpoints(self) -> None:
        from kiro_crew import validation
        from kiro_crew.dashboard import chat_folders

        assert validation._CHAT_FOLDER_STEERING_DIRS_MAX == chat_folders.MAX_FOLDER_STEERING_DIRS
        assert (
            validation._CHAT_FOLDER_STEERING_DIR_LEN_MAX == chat_folders.MAX_FOLDER_STEERING_DIR_LEN
        )

    def test_over_the_count_cap_is_refused_before_any_read(self) -> None:
        from kiro_crew.dashboard.chat_folders import MAX_FOLDER_STEERING_DIRS

        too_many = [f"/srv/s{i}" for i in range(MAX_FOLDER_STEERING_DIRS + 1)]
        out, dash = _call({"folder": "Travel", "steering_dirs": too_many})
        assert out.startswith("Error:")
        assert not dash.requests


class TestTheTreeIsTheReadHalf:
    def test_a_folder_that_declares_steering_shows_it(self) -> None:
        out, _dash = _call({}, tool="chat_folder_tree")
        kirocrew_line = next(line for line in out.splitlines() if "aaaaaaaaaaaa" in line)
        assert f"steering=[{_STEERED[0]}]" in kirocrew_line
        travel_line = next(line for line in out.splitlines() if "cccccccccccc" in line)
        assert "steering=" not in travel_line

    def test_the_tool_description_points_at_the_tree_for_the_read(self) -> None:
        from kiro_crew.mcp_dashboard import _tool_definitions

        by_name = {t["name"]: t for t in _tool_definitions()}
        assert "chat_folder_tree" in by_name["chat_folder_steering_set"]["description"]
        assert by_name["chat_folder_steering_set"]["inputSchema"]["required"] == [
            "folder",
            "steering_dirs",
        ]

    def test_the_row_declares_exactly_the_routes_it_sends(self) -> None:
        """The table scopes each body to its declared routes, so the row is the list."""
        from kiro_crew.mcp_dashboard import _folder_tools

        (row,) = [t for t in _folder_tools() if t.name == _TOOL]
        assert set(row.routes) == {
            "GET /api/chat/slots",
            "GET /api/chat/folders",
            "PATCH /api/chat/folders/{folder}",
        }


def test_the_approval_wait_stays_under_the_gateway_ping_stale_bound() -> None:
    """The steering call holds one dashboard ``tools/call`` open while the card
    waits. That server runs calls on one worker and, on native Windows, answers
    no ``ping`` meanwhile, so the client wait must end before the gateway's
    ``PING_STALE_SECS`` would recycle it as wedged -- and must still outlast the
    endpoint's own window so the agent reads the endpoint's refusal."""
    from kiro_crew.dashboard.chat_folders import STEERING_APPROVAL_TIMEOUT_SECS
    from kiro_crew.mcp_dashboard import STEERING_APPROVAL_CLIENT_TIMEOUT
    from kiro_crew.mcp_gateway.backend import PING_STALE_SECS

    assert STEERING_APPROVAL_TIMEOUT_SECS < STEERING_APPROVAL_CLIENT_TIMEOUT
    assert STEERING_APPROVAL_CLIENT_TIMEOUT < PING_STALE_SECS


def test_the_loopback_client_forwards_the_patch_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production adapter passes the approval wait down to ``mcp_core._patch``.

    The in-memory dashboard records the timeout the body asked for; this pins
    that the loopback client, which the gateway actually runs, does not drop it
    and fall back to the 30s default the card would outlast.
    """
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.dashboard_client import LoopbackDashboardClient

    seen: dict[str, Any] = {}

    def _fake_patch(path: str, body: Any = None, **kwargs: Any) -> dict:
        seen.update(kwargs, path=path)
        return {}

    monkeypatch.setattr(mcp_core, "_patch", _fake_patch)
    LoopbackDashboardClient().patch("/api/chat/folders/x", {}, session_key="k", timeout=99)
    assert seen["timeout"] == 99
    seen.clear()
    LoopbackDashboardClient().patch("/api/chat/folders/x", {}, session_key="k")
    assert "timeout" not in seen, "no timeout asked for means mcp_core's own default"
