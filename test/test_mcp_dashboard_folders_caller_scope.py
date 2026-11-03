"""The chat folder tools act for the verified caller, and only see what it may.

Every folder write carries the key the gate verified strictly, the session list a
reference resolves against is scoped to the caller, incognito and temporary
sessions stay invisible. Each case runs one tools/call frame through
``mcp_dashboard.TABLE`` against an in-memory dashboard.
"""

from __future__ import annotations

from mcp_dashboard_folders_helpers import (
    _UNVERIFIED,
    _call,
    _reads,
    _slots_with_caller,
    _writes,
)

from kiro_crew.mcp_tools.table import Caller


class TestEveryFolderWriteCarriesTheVerifiedKey:
    """The endpoint's ownership rule is only as good as the identity that
    reaches it, so the key the gate STRICTLY verified must be the key the write
    sends. A request that names no key carries the lenient attribution key,
    whose /proc ancestor walk can land on a different slot -- for an app-owned
    session that makes the write arrive looking like the unconfined person, which
    would check one identity and write under another.
    """

    MIXED = _reads(
        slots=[{"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"}]
    )
    RADAR = Caller.strict("dashboard:chat-1-100")

    def test_create_sends_the_verified_key(self) -> None:
        made = {"id": "new000000001", "name": "Runs", "parent_id": ""}
        _, dash = _call(
            "chat_folder_create",
            {"name": "Runs"},
            {**self.MIXED, "POST /api/chat/folders": made},
            self.RADAR,
        )
        assert _writes(dash)[-1].session_key == "dashboard:chat-1-100"

    def test_mkdir_p_segments_are_created_under_the_verified_key(self) -> None:
        """The intermediate segments are real folders, so each write needs it too."""
        made = {"id": "new000000001", "name": "seg", "parent_id": ""}
        _, dash = _call(
            "chat_folder_create",
            {"name": "Leaf", "parent": "Fresh/Deep"},
            {**self.MIXED, "POST /api/chat/folders": made},
            self.RADAR,
        )
        assert len(_writes(dash)) > 1
        assert {r.session_key for r in _writes(dash)} == {"dashboard:chat-1-100"}

    def test_move_sends_the_verified_key(self) -> None:
        moved = {"id": "fldr00000002", "name": "0811", "parent_id": "fldr00000003"}
        _, dash = _call(
            "chat_folder_move",
            {"folder": "kirocrew/0811", "new_parent": "Travel"},
            {**self.MIXED, "PATCH /api/chat/folders/{folder}": moved},
            self.RADAR,
        )
        assert _writes(dash)[-1].session_key == "dashboard:chat-1-100"


class TestTheSessionListIsScopedToTheCaller:
    """The endpoint is not app-scoped, so this server has to be.

    Without it an app agent holding the set reads every session's title and key
    — across other apps and the person's own work — through `chat_folder_tree`.
    """

    MIXED = _reads(
        folders=[{"id": "aaaaaaaaaaaa", "name": "Work", "parent_id": ""}],
        slots=[
            {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"},
            {"key": "chat-2-200", "title": "Spec draft", "folder_id": "", "app": "spec-builder"},
            {"key": "chat-3-300", "title": "Raymond's own work", "folder_id": "", "app": ""},
        ],
    )

    def test_an_app_sees_only_its_own_sessions(self) -> None:
        out, _ = _call("chat_folder_tree", {}, self.MIXED, Caller.strict("dashboard:chat-1-100"))
        assert "Radar run" in out
        assert "Spec draft" not in out
        assert "Raymond's own work" not in out

    def test_the_user_sees_every_session(self) -> None:
        """An unscoped caller is the person's own agent — the point of the tools."""
        out, _ = _call("chat_folder_tree", {}, self.MIXED, Caller.strict("dashboard:chat-3-300"))
        assert "Radar run" in out and "Spec draft" in out and "Raymond's own work" in out

    def test_an_unverifiable_caller_is_shown_nothing(self) -> None:
        out, _ = _call("chat_folder_tree", {}, self.MIXED, _UNVERIFIED)
        assert out.startswith("Error:")
        assert "cannot verify which session is calling" in out
        assert "Radar run" not in out and "Spec draft" not in out

    def test_an_app_cannot_resolve_a_foreign_session_by_title(self) -> None:
        """The resolver reads the same filtered list, so scope covers writes too."""
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "Spec draft", "folder": "Work"},
            {**self.MIXED, "PATCH /api/chat/slots/{slot}/folder": {"ok": True}},
            Caller.strict("dashboard:chat-1-100"),
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []


class TestPrivateSessionsAreInvisible:
    """Incognito and temporary sessions are out of the record by the user's choice.

    ``/api/chat/slots`` returns them like any other row, so these tools filter
    them: an agent tidying folders must not learn a private session's title or
    key, and must not be able to file one anywhere.
    """

    MIXED = {
        **_reads(
            slots=[
                {
                    "key": "chat-1-100",
                    "title": "Public work",
                    "folder_id": "",
                    "memory_mode": "persistent",
                },
                {
                    "key": "chat-9-900",
                    "title": "Secret thing",
                    "folder_id": "",
                    "memory_mode": "incognito",
                },
                {
                    "key": "chat-8-800",
                    "title": "Scratch pad",
                    "folder_id": "",
                    "memory_mode": "temporary",
                },
            ]
        ),
        "PATCH /api/chat/slots/{slot}/folder": {"ok": True},
    }

    def test_no_archived_count_is_rendered(self) -> None:
        """An archived transcript may be a private one, so the count stays out.

        ``history_count`` counts filed history with no ``memory_mode`` to filter
        on, so a folder holding one incognito conversation would disclose it as a
        number. The invariant is per-server, not per-tool: nothing rendered here
        may reveal a non-persistent session, and a count this server cannot prove
        clean is therefore never emitted — not even when it is large.
        """
        folders = [{"id": "aaaaaaaaaaaa", "name": "Work", "parent_id": "", "history_count": 42}]
        out, _ = _call("chat_folder_tree", {}, _reads(folders=folders, slots=_slots_with_caller()))
        assert "Work" in out
        assert "42" not in out and "archived" not in out

    def test_the_tree_omits_them(self) -> None:
        out, _ = _call("chat_folder_tree", {}, self.MIXED)
        assert "Public work" in out
        assert "Secret thing" not in out and "chat-9-900" not in out
        assert "Scratch pad" not in out and "chat-8-800" not in out

    def test_one_cannot_be_moved_by_key(self) -> None:
        out, dash = _call(
            "chat_folder_move_session", {"session": "chat-9-900", "folder": "Work"}, self.MIXED
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_one_cannot_be_moved_by_title(self) -> None:
        out, dash = _call(
            "chat_folder_move_session", {"session": "Secret thing", "folder": "Work"}, self.MIXED
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []


class TestTheVerifiedCallerKeyReachesTheRequest:
    """The gate resolves the caller strictly; the request must SEND that key.

    Gating on the strict resolver and then letting the transport resolve again
    authorizes the check and the action as potentially different sessions: the
    lenient walk reads mutable process state, so what it answers at request time
    need not be what the gate approved. The endpoint authorizes on the key it
    receives, which makes the sent key the security-relevant one.
    """

    VERIFIED = Caller.strict("dashboard:chat-verified")

    def test_create_carries_the_verified_key(self) -> None:
        _, dash = _call(
            "session_create",
            {"title": "worker"},
            {"POST /api/session-control/create": {"target": "chat-2", "title": "w"}},
            self.VERIFIED,
        )
        assert dash.requests[-1].session_key == "dashboard:chat-verified"

    def test_stop_carries_the_verified_key(self) -> None:
        _, dash = _call(
            "session_stop",
            {"target": "peer"},
            {"POST /api/session-control/stop": {"ok": True}},
            self.VERIFIED,
        )
        assert dash.requests[-1].session_key == "dashboard:chat-verified"

    def test_read_carries_the_verified_key(self) -> None:
        _, dash = _call(
            "session_read_message",
            {"target": "peer"},
            {"GET /api/session-control/read": {"messages": [], "total": 0}},
            self.VERIFIED,
        )
        assert dash.requests[-1].session_key == "dashboard:chat-verified"

    def test_a_re_sent_stop_is_not_reported_as_nothing_to_stop(self) -> None:
        """A de-duplicated retry lands on the no-op reply routinely.

        Its earlier cooperative stop IS still in flight, so rendering it the way a
        never-running target is rendered would tell the caller the opposite of what
        happened — and invite it to act as though the target were free-running.

        Mutation guard: ignoring `already_stopping` restores "nothing to stop".
        """
        out, _ = _call(
            "session_stop",
            {"target": "peer"},
            {
                "POST /api/session-control/stop": {
                    "ok": True,
                    "target": "peer",
                    "info": "stop already in progress",
                    "already_stopping": True,
                }
            },
            self.VERIFIED,
        )
        assert "stop already in progress" in out
        assert "nothing to stop" not in out
        assert "the earlier stop still stands" in out

    def test_a_target_that_was_never_running_still_says_nothing_to_stop(self) -> None:
        out, _ = _call(
            "session_stop",
            {"target": "peer"},
            {
                "POST /api/session-control/stop": {
                    "ok": True,
                    "target": "peer",
                    "info": "not running",
                    "already_stopping": False,
                }
            },
            self.VERIFIED,
        )
        assert "nothing to stop" in out

    def test_an_empty_window_still_hands_back_the_cursor(self) -> None:
        """A poll loop's commonest answer is empty, and it must not lose its place.

        Without the cursor the caller either re-reads with no `since` -- taking the
        tail, which skips everything older than the last `limit` rows once the
        target answers in a burst -- or reuses a stale position and re-reads rows it
        has already seen.

        Mutation guard: returning only the head line and "No messages" fails here.
        """
        out, _ = _call(
            "session_read_message",
            {"target": "peer"},
            {"GET /api/session-control/read": {"messages": [], "total": 7, "next_since": 7}},
            self.VERIFIED,
        )
        assert "since=7" in out, "an empty window must still carry next_since"

    def test_a_trimmed_transcript_invents_no_cursor_on_an_empty_window(self) -> None:
        """The renderer never invents a cursor the response did not carry.

        The live server now returns `next_since` on trimmed sessions too, so
        this pins the renderer's defensive behaviour for a cursor-less
        response shape, whatever produces one.
        """
        out, _ = _call(
            "session_read_message",
            {"target": "peer"},
            {"GET /api/session-control/read": {"messages": [], "total": 7}},
            self.VERIFIED,
        )
        assert "since=" not in out, "no cursor may be invented once rows are trimmed"

    def test_an_unverifiable_caller_never_reaches_the_request(self) -> None:
        """The refusal must precede the call, not merely alter its key."""
        for tool, args in (
            ("session_create", {"title": "worker"}),
            ("session_fork", {"title": "worker"}),
            ("session_stop", {"target": "peer"}),
            ("session_read_message", {"target": "peer"}),
        ):
            out, dash = _call(tool, args, {}, _UNVERIFIED)
            assert "cannot be identified" in out
            assert dash.requests == []
