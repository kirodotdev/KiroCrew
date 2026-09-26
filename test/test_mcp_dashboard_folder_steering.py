"""``chat_folder_steering_set`` — the agent surface for a folder's steering dirs.

The dashboard's Folder settings → Additional steering writes ``steering_dirs``
through ``PATCH /api/chat/folders/{id}``. This tool is the same write from an
agent, so what these tests pin is that it is the SAME write: one PATCH carrying
only that field under the verified caller key, with every verdict about the
paths (validity, the person-only principal gate, the Windows refusal) left to
the endpoint and surfaced verbatim. The one rule the tool adds — a non-empty
list only from a ``dashboard:`` caller — is pinned here too, along with the
tree's read half and the channel-agent containment.

HTTP helpers are patched as in ``test_mcp_dashboard_folders.py``; the endpoint's
own behaviour is ``test_folder_steering_principal_gate.py`` and
``test_dashboard_chat.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.mcp_dashboard import STEERING_APPROVAL_CLIENT_TIMEOUT, _call_tool_inner
from kiro_crew.validation import ValidationError

_STEERED = ["/srv/standards/org", "/srv/standards/python"]

_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": "", "steering_dirs": _STEERED[:1]},
    {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
    {"id": "cccccccccccc", "name": "Travel", "parent_id": ""},
]

_CALLER_ROW = {"key": "chat-1-100", "title": "Caller", "folder_id": "", "slot_generation": 7}


def _rows(path: str, *_a: Any, **_k: Any) -> list[dict]:
    if path == "/api/chat/folders":
        return [dict(f) for f in _FOLDERS]
    if path == "/api/chat/slots":
        return [dict(_CALLER_ROW)]
    raise AssertionError(f"unexpected GET {path}")


@pytest.fixture(autouse=True)
def _verified_dashboard_caller() -> Any:
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


def _set(args: dict, patch_reply: dict) -> tuple[str, Any]:
    with (
        patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
        patch("kiro_crew.mcp_dashboard._patch", return_value=patch_reply) as mock_patch,
    ):
        out = _call_tool_inner("chat_folder_steering_set", args)
    return out, mock_patch


class TestTheWriteIsTheEndpointsWrite:
    def test_one_patch_carrying_only_steering_dirs_under_the_verified_key(self) -> None:
        out, mock_patch = _set(
            {"folder": "kirocrew/0811", "steering_dirs": _STEERED},
            {"id": "bbbbbbbbbbbb", "name": "0811", "steering_dirs": _STEERED},
        )
        assert mock_patch.call_count == 1
        path, body = mock_patch.call_args.args
        assert path == "/api/chat/folders/bbbbbbbbbbbb"
        assert body == {"steering_dirs": _STEERED, "steering_caller_generation": 7}
        assert mock_patch.call_args.kwargs == {
            "session_key": "dashboard:chat-1-100",
            # The endpoint holds the request open for the person's approval.
            "timeout": STEERING_APPROVAL_CLIENT_TIMEOUT,
        }
        assert "kirocrew/0811" in out
        for entry in _STEERED:
            assert entry in out

    def test_accepts_the_folder_by_id(self) -> None:
        _out, mock_patch = _set(
            {"folder": "cccccccccccc", "steering_dirs": _STEERED[:1]},
            {"id": "cccccccccccc", "steering_dirs": _STEERED[:1]},
        )
        assert mock_patch.call_args.args[0] == "/api/chat/folders/cccccccccccc"

    def test_the_echo_is_what_the_endpoint_stored_not_what_was_sent(self) -> None:
        """The endpoint resolves each entry (realpath); later chats read THAT."""
        out, _mock = _set(
            {"folder": "Travel", "steering_dirs": ["/srv/link"]},
            {"id": "cccccccccccc", "steering_dirs": ["/srv/real"]},
        )
        assert "/srv/real" in out
        assert "/srv/link" not in out

    def test_an_empty_list_clears_and_says_so(self) -> None:
        out, mock_patch = _set(
            {"folder": "kirocrew", "steering_dirs": []},
            {"id": "aaaaaaaaaaaa", "name": "kirocrew"},
        )
        assert mock_patch.call_args.args[1] == {
            "steering_dirs": [],
            "steering_caller_generation": 7,
        }
        assert "Cleared" in out

    def test_root_is_not_a_folder(self) -> None:
        out, mock_patch = _set({"folder": "root", "steering_dirs": _STEERED}, {})
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_an_unknown_folder_never_reaches_the_endpoint(self) -> None:
        out, mock_patch = _set({"folder": "nope/never", "steering_dirs": _STEERED}, {})
        assert out.startswith("Error:")
        mock_patch.assert_not_called()


class TestEveryPathVerdictIsTheEndpoints:
    """No re-derivation: the tool surfaces the endpoint's refusal and stops."""

    def test_the_principal_gate_refusal_is_surfaced_with_the_ui_route(self) -> None:
        out, _mock = _set(
            {"folder": "Travel", "steering_dirs": _STEERED},
            {
                "error": "steering_dirs may be declared only from the person's own session",
                "code": "steering_dirs_forbidden",
            },
        )
        assert out.startswith("Error: steering_dirs may be declared only")
        assert "Folder settings" in out

    def test_an_invalid_path_verdict_is_surfaced_verbatim(self) -> None:
        out, _mock = _set(
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
        _out, mock_patch = _set(
            {"folder": "Travel", "steering_dirs": ["/definitely/not/here"]},
            {"id": "cccccccccccc", "steering_dirs": ["/definitely/not/here"]},
        )
        mock_patch.assert_called_once()


class TestOnlyTheDashboardMayDeclare:
    def test_a_channel_bound_caller_cannot_declare(self) -> None:
        with (
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="slack:C0AMVG4AVE1:1790259905.866239",
            ),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": _STEERED}
            )
        assert out.startswith("Error:")
        assert "dashboard" in out
        mock_patch.assert_not_called()

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
        with (
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="slack:C0AMVG4AVE1:1790259905.866239",
            ),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows) as mock_get,
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": []}
            )
        assert out.startswith("Error:")
        assert "cleared" in out
        mock_patch.assert_not_called()
        # Only the slot list is read (the identity gate's, then the tool's own
        # shape check); the folder list is never fetched, so the refusal
        # precedes every read about the target.
        assert set(c.args[0] for c in mock_get.call_args_list) == {"/api/chat/slots"}

    def test_the_dashboard_caller_may_clear(self) -> None:
        """The contrast case: the same empty list from the person's tab is the clear."""
        out, mock_patch = _set({"folder": "Travel", "steering_dirs": []}, {"id": "cccccccccccc"})
        assert "Cleared" in out
        assert mock_patch.call_args.args[1] == {
            "steering_dirs": [],
            "steering_caller_generation": 7,
        }
        assert mock_patch.call_args.kwargs == {
            "session_key": "dashboard:chat-1-100",
            # The endpoint holds the request open for the person's approval.
            "timeout": STEERING_APPROVAL_CLIENT_TIMEOUT,
        }

    def test_an_unverifiable_caller_is_refused_before_any_read(self) -> None:
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._get") as mock_get,
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": []}
            )
        assert out.startswith("Error: cannot verify which session is calling")
        mock_get.assert_not_called()
        mock_patch.assert_not_called()

    def test_a_delegated_caller_is_refused(self) -> None:
        with (
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="subagent:abc123",
            ),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": _STEERED}
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

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
        row = {**_CALLER_ROW, "app": "acme-widgets", "links": []}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(row)) as mock_get,
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": dirs}
            )
        assert out.startswith("Error:")
        assert "acme-widgets" in out
        assert "only the person" in out
        mock_patch.assert_not_called()
        # Refused on the identity gate's own scope: the folder list is never read.
        assert "/api/chat/folders" not in {c.args[0] for c in mock_get.call_args_list}

    @pytest.mark.parametrize("dirs", [_STEERED, []], ids=["set", "clear"])
    def test_a_crew_member_session_is_refused(self, dirs: list) -> None:
        row = {**_CALLER_ROW, "mode": "member", "links": []}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(row)) as mock_get,
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": dirs}
            )
        assert out.startswith("Error:")
        assert "crew member" in out
        mock_patch.assert_not_called()
        assert "/api/chat/folders" not in {c.args[0] for c in mock_get.call_args_list}

    def test_the_persons_own_tab_still_clears(self) -> None:
        """The contrast: no ``app``, not ``member`` mode, no link -- the clear lands."""
        row = {**_CALLER_ROW, "app": "", "mode": "", "links": []}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(row)),
            patch(
                "kiro_crew.mcp_dashboard._patch", return_value={"id": "cccccccccccc"}
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": []}
            )
        assert "Cleared" in out
        mock_patch.assert_called_once()


class TestAConversationChannelWordsEnteredIsMarkedForGood:
    """The conversation, not the turn, is the unit channel words taint.

    Once a reply from a linked Slack thread has entered a conversation, its text
    is in the context, and ANY later turn can act on it: the successor synthesis
    turn, a subagent-completion delivery (both dispatched with no channel flag),
    or a turn the person types after unlinking. A per-turn provenance bit is
    False on every one of those, and link state is mutable. So the slot carries
    a sticky ``_channel_turn_seen`` mark -- set on enqueue and at ``_run_chat``
    entry, never cleared, persisted -- the projection reports it as
    ``channel_turn_seen``, and the tool refuses on it before the link clause.
    """

    _UNLINKED_AFTERWARDS = {
        **_CALLER_ROW,
        "running": False,
        "channel_turn_seen": True,
        "links": [],
        "slack_linked": False,
        "slack_channel": "",
        "slack_thread_ts": "",
        "linked_session_key": "",
    }

    @pytest.mark.parametrize("dirs", [_STEERED, []], ids=["set", "clear"])
    def test_a_marked_conversation_is_refused_with_every_link_field_clean(self, dirs: list) -> None:
        """The post-unlink, idle row: no link, no running turn -- still refused."""
        with (
            patch(
                "kiro_crew.mcp_dashboard._get",
                side_effect=_rows_with_caller(self._UNLINKED_AFTERWARDS),
            ),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": dirs}
            )
        assert out.startswith("Error:")
        assert "received messages from a channel" in out
        mock_patch.assert_not_called()

    def test_the_mark_reason_wins_when_a_link_is_also_present(self) -> None:
        row = {**self._UNLINKED_AFTERWARDS, "slack_linked": True}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(row)),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": _STEERED}
            )
        assert "received messages from a channel" in out
        mock_patch.assert_not_called()

    def test_only_the_boolean_true_counts(self) -> None:
        """A truthy non-boolean is not the mark: the projection emits a real bool."""
        row = {**self._UNLINKED_AFTERWARDS, "channel_turn_seen": "yes"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(row)),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "cccccccccccc", "steering_dirs": _STEERED},
            ) as mock_patch,
        ):
            _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": _STEERED}
            )
        mock_patch.assert_called_once()

    def test_a_channel_enqueue_marks_and_nothing_unmarks(self) -> None:
        """Queued channel text marks the slot; later unflagged activity cannot clear it."""
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        assert slot.to_dict()["channel_turn_seen"] is False
        slot.queue_append("typed by the person")
        assert slot.to_dict()["channel_turn_seen"] is False
        slot.queue_append("from the thread", directive_channel_origin=True)
        assert slot.to_dict()["channel_turn_seen"] is True
        # A successor / completion / typed message arrives with no flag.
        slot.queue_append("subagent result")
        slot.queue_insert(0, "synthesis follow-up")
        slot._slack_linked = False
        assert slot.to_dict()["channel_turn_seen"] is True

    def test_queue_insert_marks_too(self) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s2")
        slot.queue_insert(0, "from the thread", directive_channel_origin=True)
        assert slot.to_dict()["channel_turn_seen"] is True

    def test_the_runner_marks_a_direct_dispatch_and_never_unmarks(self) -> None:
        """The idle-slot intercept starts ``_run_chat`` without the queue.

        So the runner's entry must mark on the flag -- and, being sticky, must not
        write False for a successor dispatched without it.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        source = inspect.getsource(chat_runner._run_chat)
        assert (
            "if _directive_channel_origin or _turn_actor in _FOREIGN_TURN_ACTORS:\n"
            "        slot._channel_turn_seen = True"
        ) in source
        assert "slot._channel_turn_seen = False" not in source
        assert "_channel_turn_seen = bool(" not in source

    @pytest.mark.parametrize("actor", ["cron", "app", "crew", "other"])
    def test_a_turn_a_foreign_actor_starts_marks_the_conversation(self, actor: str) -> None:
        """A cron notification reporting into its originating dashboard tab runs
        a turn under that tab's ``dashboard:`` key with no channel flag and no
        link; the actor is what says the words are not the person's."""
        from kiro_crew.dashboard import chat_runner

        assert actor in chat_runner._FOREIGN_TURN_ACTORS

    @pytest.mark.parametrize("actor", ["", "user", "autonudge", "subagent", "gateway"])
    def test_the_person_and_this_sessions_own_work_do_not_mark(self, actor: str) -> None:
        from kiro_crew.dashboard import chat_runner

        assert actor not in chat_runner._FOREIGN_TURN_ACTORS

    def test_every_crew_log_actor_is_classified(self) -> None:
        """A new actor must be placed on one side deliberately, not default to trusted."""
        from kiro_crew.crew_log.emit import ACTORS
        from kiro_crew.dashboard import chat_runner

        trusted = {"user", "autonudge", "subagent", "gateway"}
        assert ACTORS == trusted | chat_runner._FOREIGN_TURN_ACTORS

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("actor", "marked"), [("cron", True), ("", False)])
    async def test_the_runner_marks_on_a_cron_turn(self, actor: str, marked: bool) -> None:
        """Drive the real runner entry: the mark lands before anything else runs.

        The state is a sentinel that raises on first use, so the call stops right
        after the entry stamp; what the slot carries then is the stamp's verdict.
        """
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.state import _ChatSlot

        class _Stop(Exception):
            pass

        class _Exploding:
            def __getattr__(self, name: str) -> Any:
                raise _Stop(name)

        slot = _ChatSlot("chat-1-100")
        with pytest.raises(Exception):
            await chat_runner._run_chat(_Exploding(), slot, "report", _turn_actor=actor)  # type: ignore[arg-type]
        assert slot._channel_turn_seen is marked

    def test_the_mark_survives_a_restart(self) -> None:
        """Every hydration path goes through the fail-closed restore."""
        import inspect

        from kiro_crew.dashboard import chat_persistence

        source = inspect.getsource(chat_persistence)
        assert source.count("restore_channel_mark(slot, meta)") == 2

    @pytest.mark.parametrize(
        ("module_name", "function_name"),
        [
            # open_slots.json restore
            ("kiro_crew.dashboard.chat_persistence", "_rehydrate_slot_from_history"),
            # folder'd / pinned / recent restore of a slot not in open_slots.json
            ("kiro_crew.dashboard.chat_persistence", "_apply_recent_session"),
            # History resume and session import
            ("kiro_crew.dashboard.chat_handlers", "_hydrate_slot_from_history"),
        ],
    )
    def test_every_hydration_path_restores_the_mark(
        self, module_name: str, function_name: str
    ) -> None:
        """Each path that rebuilds a transcript rebuilds its channel mark too.

        A tab that ran replies from a linked thread and was then unlinked comes
        back with those replies in context whichever path restores it; if any
        one path dropped the mark, the row would read clean (no links, no mark)
        and the verb would be permitted in a conversation channel words entered.
        """
        import importlib
        import inspect

        module = importlib.import_module(module_name)
        source = inspect.getsource(getattr(module, function_name))
        assert "restore_channel_mark(slot, meta)" in source

    @pytest.mark.parametrize(
        ("meta", "marked"),
        [
            # Any restored transcript is marked: its metadata line is agent-
            # writable, so no value in it can prove the conversation clean --
            # including a forged "unmarked" line.
            ({"title": "old tab", "created_at": "2026-01-01"}, True),
            ({"title": "old tab", "channel_turn_seen": False}, True),
            ({"title": "t", "channel_provenance_tracked": True}, True),
            ({"channel_provenance_tracked": True, "channel_turn_seen": False}, True),
            ({"title": "[imported] t", "agent": "kirocrew"}, True),
            # An unreadable or legacy first line reads back as {} while the
            # transcript still carries rows: marked like any other restore.
            ({}, True),
        ],
    )
    def test_restore_fails_closed_on_every_hydrated_transcript(
        self, meta: dict, marked: bool
    ) -> None:
        from kiro_crew.dashboard.chat_persistence import restore_channel_mark
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("chat-1-100")
        restore_channel_mark(slot, meta)
        assert slot._channel_turn_seen is marked

    def test_restore_never_clears_a_live_mark(self) -> None:
        from kiro_crew.dashboard.chat_persistence import restore_channel_mark
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("chat-1-100")
        slot._channel_turn_seen = True
        restore_channel_mark(slot, {})
        assert slot._channel_turn_seen is True

    def test_the_mark_is_never_read_from_or_written_to_the_transcript(self) -> None:
        """The durable metadata line is agent-writable; a mark persisted there
        could be forged away, so it is neither written nor read back."""
        import inspect

        from kiro_crew.dashboard import chat_handlers, chat_persistence, session_control

        for module in (chat_persistence, chat_handlers, session_control):
            source = inspect.getsource(module)
            assert '"channel_turn_seen"] = True' not in source
            assert 'meta.get("channel_turn_seen")' not in source
            assert "channel_provenance_tracked" not in source

    def test_a_tab_born_to_display_a_channel_transcript_is_marked(self) -> None:
        """A revived or surfaced channel transcript is channel text from turn one.

        ``channel_origin`` is set when a tab is created to DISPLAY a channel
        conversation (the reconciler, a History revive), and such a tab can be
        left unbound -- no ``links``, ``slack_linked`` False -- when its thread
        cannot be named. No channel-flagged message is ever queued into it, so
        ``_channel_turn_seen`` alone stays False; the projection folds the
        persisted origin flag in so the row still reads as marked.
        """
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("revived")
        assert slot.to_dict()["channel_turn_seen"] is False
        slot.channel_origin = True
        row = slot.to_dict()
        assert row["channel_turn_seen"] is True
        assert row["slack_linked"] is False
        assert not row.get("links")

    def test_the_revived_transcript_row_is_refused_end_to_end(self) -> None:
        """Projection output fed to the tool: unbound, unmarked by queue, still refused."""
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("chat-1-100")
        slot.channel_origin = True
        projected = slot.to_dict()
        row = {
            **_CALLER_ROW,
            "channel_turn_seen": projected["channel_turn_seen"],
            "slack_linked": projected["slack_linked"],
            "links": [],
            "linked_session_key": "",
        }
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(row)),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": _STEERED}
            )
        assert "received messages from a channel" in out
        mock_patch.assert_not_called()


def _rows_with_caller(caller_row: dict) -> Any:
    """A GET stand-in whose slot list carries *caller_row* as the caller's own row."""

    def _get(path: str, *_a: Any, **_k: Any) -> list[dict]:
        if path == "/api/chat/folders":
            return [dict(f) for f in _FOLDERS]
        if path == "/api/chat/slots":
            return [dict(caller_row)]
        raise AssertionError(f"unexpected GET {path}")

    return _get


class TestTheDashboardKeyIsNotProofOfDashboardWords:
    """A ``dashboard:`` key names the slot a turn runs in, not where its words came from.

    A tab linked to a Slack thread keeps its ``dashboard:<slot>`` key and runs a
    reply posted in that thread as a turn; a channel-born session is brought back
    by a human reply the same way. The prefix test alone would admit exactly the
    channel-origin caller this verb exists to exclude, with no endpoint layer
    behind it (the person's principal passes the principal gate). So the tool
    refuses on the slot's recorded SHAPE -- any channel link -- for every turn
    in that tab, set or clear, typed or not.
    """

    @pytest.mark.parametrize(
        "shape",
        [
            pytest.param(
                {"slack_linked": True, "slack_channel": "C1", "slack_thread_ts": "1.2"},
                id="slack-linked-thread",
            ),
            pytest.param(
                {"links": [{"channel": "slack", "direction": "out", "paused": False}]},
                id="links-row-out",
            ),
            pytest.param(
                {"links": [{"channel": "telegram", "direction": "origin"}]},
                id="channel-born-origin-link",
            ),
            pytest.param({"linked_session_key": "slack:C1:1.2"}, id="linked-session-key"),
        ],
    )
    @pytest.mark.parametrize("dirs", [_STEERED, []], ids=["set", "clear"])
    def test_a_channel_reachable_dashboard_slot_is_refused(self, shape: dict, dirs: list) -> None:
        with (
            patch(
                "kiro_crew.mcp_dashboard._get",
                side_effect=_rows_with_caller({**_CALLER_ROW, **shape}),
            ),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": dirs}
            )
        assert out.startswith("Error:")
        assert "linked to a channel" in out
        mock_patch.assert_not_called()

    def test_a_missing_own_row_is_refused_not_assumed_clean(self) -> None:
        """No row for the caller's key: the tab closed mid-call or the key is wrong."""
        other = {"key": "chat-9-900", "title": "Someone else", "folder_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(other)),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": _STEERED}
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_an_unlinked_dashboard_slot_writes(self) -> None:
        """The contrast: an empty ``links`` list and no binding is the ordinary tab."""
        clean = {**_CALLER_ROW, "links": [], "slack_linked": False, "linked_session_key": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_caller(clean)),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "cccccccccccc", "steering_dirs": _STEERED},
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": _STEERED}
            )
        assert "Set the steering directories" in out
        mock_patch.assert_called_once()


class TestAgentAuthoredPathsAreScreened:
    LEAKY = "AKIAIOSFODNN7EXAMPLE"

    def test_a_credential_shaped_entry_is_refused_not_rewritten(self) -> None:
        """A redacted path names a different directory, so refuse, never store."""
        out, mock_patch = _set(
            {"folder": "Travel", "steering_dirs": [f"/srv/{self.LEAKY}/steering"]}, {}
        )
        assert out.startswith("Error:")
        assert self.LEAKY not in out
        mock_patch.assert_not_called()


class TestTheSchemaMirrorsTheEndpoint:
    def test_steering_dirs_is_required_so_an_omission_is_not_a_clear(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_folder_steering_set", {"folder": "Travel"})

    def test_folder_is_required(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_folder_steering_set", {"steering_dirs": []})

    def test_a_non_string_entry_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_folder_steering_set", {"folder": "Travel", "steering_dirs": [1]})

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
        with patch("kiro_crew.mcp_dashboard._get") as mock_get, pytest.raises(ValidationError):
            _call_tool_inner(
                "chat_folder_steering_set", {"folder": "Travel", "steering_dirs": too_many}
            )
        mock_get.assert_not_called()


class TestTheTreeIsTheReadHalf:
    def test_a_folder_that_declares_steering_shows_it(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", side_effect=_rows):
            out = _call_tool_inner("chat_folder_tree", {})
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
