"""``session_create`` on the kirocrew-dashboard server, and its advertised tool set.

The model id, the agent-kind selection and the folder a new session is filed in
reach the create route as asked; the server advertises its whole tool set. Each
case runs one tools/call frame through ``mcp_dashboard.TABLE`` against an
in-memory dashboard.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp_dashboard_folders_helpers import _call, _minting_post, _reads, _writes

from kiro_crew.mcp_dashboard import _list_tools
from kiro_crew.mcp_tools.table import Caller


class TestSessionCreateModel:
    """`session_create.model` — the model id reaches the create route verbatim."""

    def test_model_rides_the_create_payload(self) -> None:
        created = {"target": "chat-9-900", "title": "worker", "model": "claude-sonnet-4.6"}
        out, dash = _call(
            "session_create",
            {"title": "worker", "model": "claude-sonnet-4.6"},
            {"POST /api/session-control/create": created},
        )
        (post,) = dash.requests
        assert post.path == "/api/session-control/create"
        assert post.body["model"] == "claude-sonnet-4.6"
        assert "claude-sonnet-4.6" in out

    def test_omitted_model_sends_no_key(self) -> None:
        _, dash = _call(
            "session_create",
            {"title": "w"},
            {"POST /api/session-control/create": {"target": "chat-9-900", "title": "w"}},
        )
        assert "model" not in dash.requests[-1].body

    def test_a_malformed_model_is_refused_before_any_call(self) -> None:
        out, dash = _call("session_create", {"title": "w", "model": "x; rm -rf"}, {})
        assert out.startswith("Error: model:")
        assert dash.requests == []


class TestSessionCreateAgentKind:
    """`session_create.agent_kind` — the selection namespace rides the payload, and a
    `memory_delegation_denied` refusal tells the caller what it can actually do.

    The refusal is terse on the wire by design (it names neither store nor
    member), and that terseness left callers inventing a remedy that does not
    exist — a memory-store setting under Settings > Crew. The tool reply names
    the two real ones: re-send as a template dispatch, or the owner's own tab.
    """

    _DENIED = {
        "error": "cannot verify delegation within the caller's memory assignment",
        "code": "memory_delegation_denied",
    }

    def test_agent_kind_rides_the_create_payload(self) -> None:
        created = {"target": "chat-9-900", "title": "worker"}
        _, dash = _call(
            "session_create",
            {"title": "worker", "agent": "kirocrew-worker", "agent_kind": "template"},
            {"POST /api/session-control/create": created},
        )
        (post,) = dash.requests
        assert post.body["agent"] == "kirocrew-worker"
        assert post.body["agent_kind"] == "template"

    def test_omitted_agent_kind_sends_no_key(self) -> None:
        _, dash = _call(
            "session_create",
            {"title": "w", "agent": "kirocrew-worker"},
            {"POST /api/session-control/create": {"target": "chat-9-900", "title": "w"}},
        )
        assert "agent_kind" not in dash.requests[-1].body

    @pytest.mark.parametrize("kind", ["crew", "member"])
    def test_a_kind_outside_the_set_is_refused_before_any_call(self, kind: str) -> None:
        # The set is `template` alone: a bare name already selects a member.
        out, dash = _call(
            "session_create", {"title": "w", "agent": "kirocrew-worker", "agent_kind": kind}, {}
        )
        assert out.startswith("Error:")
        assert "agent_kind" in out
        assert dash.requests == []

    def test_a_delegation_refusal_names_the_template_dispatch_and_the_owner(self) -> None:
        out, _ = _call(
            "session_create",
            {"title": "worker", "agent": "kirocrew-worker"},
            {"POST /api/session-control/create": self._DENIED},
        )
        assert out.startswith("Error: could not create a session:")
        assert "cannot verify delegation" in out
        assert 'agent_kind: "template"' in out
        assert "own chat tab" in out
        # The remedy says the binding is not a setting, so no caller goes looking
        # for one -- and it still names neither the store nor the member.
        assert "cannot be changed in Settings" in out
        assert "member-" not in out

    def test_a_refused_template_dispatch_points_at_the_owner_not_at_itself(self) -> None:
        out, _ = _call(
            "session_create",
            {"title": "worker", "agent": "kirocrew-worker", "agent_kind": "template"},
            {"POST /api/session-control/create": self._DENIED},
        )
        assert out.startswith("Error: could not create a session:")
        assert 'agent_kind: "template"' not in out
        assert "re-select" in out
        assert "own chat tab" in out

    def test_other_refusals_carry_no_remedy(self) -> None:
        out, _ = _call(
            "session_create",
            {"title": "worker", "agent": "kirocrew-worker"},
            {
                "POST /api/session-control/create": {
                    "error": "'kirocrew-worker' does not resolve to a configured agent",
                    "code": "agent_unresolved",
                }
            },
        )
        assert out == (
            "Error: could not create a session: 'kirocrew-worker' does not resolve to a "
            "configured agent"
        )

    def test_the_preflight_refusal_carries_the_same_remedy(self) -> None:
        # The dry-run probe that guards the folder walk renders the same refusal
        # text as the real create, remedy included, with nothing created.
        out, dash = _call(
            "session_create",
            {"title": "worker", "agent": "kirocrew-worker", "folder": "Goal/kirocrew-worker"},
            {
                **_reads(),
                "POST /api/session-control/create": self._DENIED,
                "POST /api/chat/folders": _minting_post(),
            },
        )
        assert out.startswith("Error: could not create a session:")
        assert "(no folder was created)" in out
        assert 'agent_kind: "template"' in out
        assert all(r.path != "/api/chat/folders" for r in _writes(dash))


class TestSessionCreateFolder:
    """`session_create.folder` — filing atomic with creation.

    The reference resolves with `chat_folder_create`'s `parent` semantics
    (missing segments created), which is tree shaping — so the SAME gate
    applies, not a second authorization path. The endpoint receives the
    resolved id and re-confirms it in the create's own synchronous window;
    these cases cover the MCP half: gate, resolution, payload shape, refusal.
    """

    CREATED = {"target": "chat-9-900", "title": "worker", "folder_id": "bbbbbbbbbbbb"}

    def _routes(self, slots: list[dict] | None = None, **extra: Any) -> dict[str, Any]:
        return {
            **_reads(slots=slots),
            "POST /api/session-control/create": lambda req: (
                {"ok": True}
                if req.body.get("dry_run")
                else {**self.CREATED, "folder_id": req.body.get("folder_id", "")}
            ),
            **extra,
        }

    def test_files_at_creation_with_the_resolved_id(self) -> None:
        """One create, already carrying the folder id — filing is not a second call."""
        out, dash = _call(
            "session_create", {"title": "worker", "folder": "kirocrew/0811"}, self._routes()
        )
        (create,) = _writes(dash)
        assert create.path == "/api/session-control/create"
        assert create.body["folder_id"] == "bbbbbbbbbbbb"
        assert "kirocrew/0811" in out

    def test_missing_segments_are_created_like_a_parent_ref(self) -> None:
        """mkdir -p over the reference, then the create rides the new leaf id."""
        made = {"id": "dddddddddddd", "name": "fresh", "parent_id": "aaaaaaaaaaaa"}
        out, dash = _call(
            "session_create",
            {"title": "worker", "folder": "kirocrew/fresh"},
            self._routes(**{"POST /api/chat/folders": made}),
        )
        create = _writes(dash)[-1]
        assert create.path == "/api/session-control/create"
        assert create.body["folder_id"] == "dddddddddddd"
        assert "created folder path" in out

    def test_an_unresolvable_folder_refuses_the_whole_create(self) -> None:
        """No session may exist when the filing half cannot be honored.

        An id-shaped reference that does not exist is a lookup failure even
        under mkdir -p (ids are minted server-side), and 'created but unfiled'
        would silently honor half the request — so the create never fires.
        """
        out, dash = _call(
            "session_create", {"title": "worker", "folder": "ffffffffffff"}, self._routes()
        )
        assert out.startswith("Error:")
        assert "folder not found" in out
        assert _writes(dash) == []

    def test_folder_resolution_rides_the_tree_shaping_gate(self) -> None:
        """A caller the tree-shaping gate refuses cannot file-by-naming at create time.

        Resolution can CREATE folders, so it is tree shaping: reusing the gate —
        rather than a second authorization path — is what keeps 'could not
        reshape the tree by creating a folder' and 'cannot reach the same write
        through session_create' the same statement. A cron caller passes the
        session-control gate (its key is verified) and is refused at this one.
        """
        out, dash = _call(
            "session_create",
            {"title": "worker", "folder": "kirocrew/0811"},
            self._routes(),
            Caller.strict("cron:job-1"),
        )
        assert out.startswith("Error: cannot establish what this caller is allowed to change")
        assert "filing a new session at creation is refused" in out
        assert _writes(dash) == []

    def test_no_folder_means_no_gate(self) -> None:
        """A plain create is not tree shaping and must not grow that refusal.

        The same cron caller the gate refuses above creates freely without a
        folder, and nothing reads the slot or folder lists.
        """
        out, dash = _call(
            "session_create", {"title": "worker"}, self._routes(), Caller.strict("cron:job-1")
        )
        assert not out.startswith("Error:")
        assert [r.route for r in dash.requests] == ["POST /api/session-control/create"]
        assert "folder_id" not in dash.requests[0].body

    def test_an_app_scoped_caller_cannot_leave_folder_segments_behind(self) -> None:
        """The endpoint refuses `app_scoped_caller`, so resolution must not run.

        An app may create folders in its own subtree, but it can never complete
        session_create — resolving (and mkdir -p creating) the folder for it
        would leave path segments behind for a call that cannot succeed. The
        refusal here is a side-effect guard; the endpoint's own refusal stays
        authoritative.
        """
        out, dash = _call(
            "session_create",
            {"title": "worker", "folder": "kirocrew/fresh"},
            self._routes(slots=[{"key": "chat-1-100", "title": "Caller", "app": "some-app"}]),
        )
        assert out.startswith("Error:")
        assert "app-scoped" in out
        assert _writes(dash) == []

    def test_segment_creation_writes_under_the_gates_verified_key(self) -> None:
        """The gate's returned key is what the folder writes carry — its contract."""
        made = {"id": "dddddddddddd", "name": "fresh", "parent_id": "aaaaaaaaaaaa"}
        _, dash = _call(
            "session_create",
            {"title": "worker", "folder": "kirocrew/fresh"},
            self._routes(
                slots=[{"key": "gate-key", "title": "Gate"}],
                **{"POST /api/chat/folders": made},
            ),
            Caller.strict("dashboard:gate-key"),
        )
        # The create's dry run comes first (it refuses before any folder
        # exists); the folder write is the one request to the folder route.
        (folder_write,) = dash.sent("POST /api/chat/folders")
        assert folder_write.session_key == "dashboard:gate-key"


class TestAdvertisedSet:
    """Reaching this server means an agent spec referenced it.

    The assignment happened in that spec, so the process has nothing left to
    decide: it advertises its whole set.
    """

    def test_the_whole_set_is_advertised(self) -> None:
        names = {t["name"] for t in _list_tools()}
        assert names == {
            "chat_folder_tree",
            "chat_folder_create",
            "chat_folder_move",
            "chat_folder_update",
            "chat_folder_move_session",
            "chat_folder_delete",
            "chat_folder_file_self",
            "chat_tag_list",
            "chat_tag_create",
            "chat_tag_update",
            "chat_tag_assign",
            "chat_tag_column_list",
            "chat_tag_column_create",
            "chat_tag_column_move",
            "session_move_to_column",
            "chat_session_pin",
            "chat_session_pin_move",
            "session_create",
            "session_fork",
            "session_stop",
            "session_end_wait",
            "session_retry",
            "session_continue",
            "session_set_model",
            "session_reload",
            "session_rename",
            "session_generate_title",
            "session_close",
            "session_revive",
            "session_history_list",
            "session_send",
            "session_broadcast",
            "session_status",
            "session_read_message",
            "session_summary",
            "session_adopt",
            "session_release",
        }
