"""Tests for the chat (sidebar) folder tools on the kirocrew-dashboard server.

Covers ``chat_folder_tree/create/move/move_session/delete/file_self`` — schema
validation, path→id resolution, mkdir -p, session-reference resolution, the
requests sent, and result formatting. Each case runs one tools/call frame
through ``mcp_dashboard.TABLE`` against an in-memory dashboard and asserts on
the reply and on what the dashboard was asked; the endpoints themselves are
tested by ``test_folder_store_writer.py`` and ``test_dashboard_chat_folders.py``.

This file holds the tree, create, move, move_session and delete cases, the
folder-name rules and the folder policy. The siblings hold the rest:
``test_mcp_dashboard_folders_caller_scope.py`` (which caller a request speaks
for, and ``chat_folder_file_self``), ``test_mcp_dashboard_folders_session_create.py``
(``session_create`` and the advertised tool set) and
``test_mcp_dashboard_folders_position.py`` (sibling order: ``before``/``after``
and the folder sort mode). The shared rows and call helpers are in
``mcp_dashboard_folders_helpers.py``.
"""

from __future__ import annotations

import inspect
from typing import Any

from mcp_dashboard_folders_helpers import (
    _FOLDERS,
    _SLOTS,
    _UNVERIFIED,
    _call,
    _minting_post,
    _reads,
    _slots_with_caller,
    _writes,
)

from kiro_crew import mcp_dashboard, mcp_dashboard_caller_scope
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest
from kiro_crew.mcp_tools.table import Caller


class TestFolderTree:
    def test_renders_paths_sessions_and_unfiled(self) -> None:
        out, _ = _call("chat_folder_tree", {})
        # Derived human path, not just the leaf name.
        assert "kirocrew/0811" in out
        # Sessions nest under their folder, with the slot key the move tool takes.
        assert "chat-2-200" in out and "Folder MCP" in out
        assert "running" in out  # live state surfaces
        # The folders endpoint reports history_count, but this server does not
        # render it: an archived count covers filed incognito/temporary
        # transcripts with no memory_mode to filter on, so a folder holding one
        # would disclose it as a number. See TestPrivateSessionsAreInvisible.
        assert "3 archived" not in out and "archived" not in out
        assert "(unfiled" in out and "chat-3-300" in out
        assert "3 folders, 3 live sessions" in out

    def test_renders_each_folders_declared_steering(self) -> None:
        """The list a ``folder.steering`` card would replace is visible here."""
        folders = [dict(_FOLDERS[0], steering_dirs=["/srv/standards", "/srv/team"]), *_FOLDERS[1:]]
        out, _ = _call("chat_folder_tree", {}, _reads(folders=folders))
        assert "steering=[/srv/standards, /srv/team]" in out
        assert out.count("steering=") == 1

    def test_slot_pointing_at_unknown_folder_falls_back_to_unfiled(self) -> None:
        """A dangling folder_id must not make the session disappear."""
        orphan = _slots_with_caller(
            {"key": "chat-9-900", "title": "Orphan", "folder_id": "deadbeefdead"}
        )
        out, _ = _call("chat_folder_tree", {}, _reads(slots=orphan))
        assert "(unfiled" in out and "chat-9-900" in out

    def test_empty_tree(self) -> None:
        # No folders, and the caller's own (incognito) session is the only
        # slot — so nothing is RENDERED while the caller stays locatable.
        out, _ = _call("chat_folder_tree", {}, _reads(folders=[], slots=_slots_with_caller()))
        assert "No sidebar folders and no live sessions." == out

    def test_folder_endpoint_error_is_not_reported_as_empty(self) -> None:
        out, _ = _call("chat_folder_tree", {}, {"GET /api/{route}": {"error": "Token required"}})
        assert out.startswith("Error:") and "Token required" in out

    def test_unexpected_body_shape_is_an_error(self) -> None:
        out, _ = _call("chat_folder_tree", {}, {"GET /api/{route}": {"folders": []}})
        assert out.startswith("Error:") and "unexpected response shape" in out


class TestArgumentsAreSanitizedTwice:
    """A dashboard body runs on arguments validated twice, as every arm always did.

    ``sanitize_string`` composes (NFC) before it strips format characters, so a
    hidden one between a letter and its combining mark survives composition on
    the first pass and only composes on the second. Mutation proof: building the
    table's second pass removed sends the decomposed spelling and both fail.
    """

    def test_a_folder_named_through_a_soft_hyphen_resolves_to_the_composed_one(self) -> None:
        cafe = {"id": "dddddddddddd", "name": "caf\u00e9", "parent_id": ""}
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-3-300", "folder": "cafe\u00ad\u0301"},
            {
                **_reads(folders=[*_FOLDERS, cafe]),
                "PATCH /api/chat/slots/{slot}/folder": {"ok": True},
            },
        )
        (patch,) = _writes(dash)
        assert patch.route == "PATCH /api/chat/slots/chat-3-300/folder"
        assert patch.body["folder_id"] == "dddddddddddd", out

    def test_a_created_folder_carries_the_composed_name(self) -> None:
        _, dash = _call(
            "chat_folder_create",
            {"name": "cafe\u200b\u0301"},
            {
                **_reads(),
                "POST /api/chat/folders": {"id": "eeeeeeeeeeee", "name": "x", "parent_id": ""},
            },
        )
        (post,) = _writes(dash)
        assert post.body["name"] == "caf\u00e9"


class TestFolderCreate:
    def test_a_name_exists_code_is_the_lost_race_even_without_an_error(self) -> None:
        """The walk reads the endpoint's code before any error, so a reply that
        names the race is re-read, not taken as a created folder.

        Mutation guard: checking the error first reads ``{"code": ...}`` alone as
        a create and goes on to file under a folder that does not exist.
        """
        out, dash = _call(
            "chat_folder_create",
            {"name": "Leaf", "parent": "Travel/New"},
            {**_reads(), "POST /api/chat/folders": {"code": "folder_name_exists"}},
        )
        assert out.startswith("Error:") and "already exists there" in out
        assert len(dash.sent("POST /api/chat/folders")) == 1
        assert [r.route for r in dash.requests].count("GET /api/chat/folders") == 2

    def test_creates_subfolder_under_existing_parent_path(self) -> None:
        made = {"id": "dddddddddddd", "name": "0812", "parent_id": "aaaaaaaaaaaa"}
        out, dash = _call(
            "chat_folder_create",
            {"name": "0812", "parent": "kirocrew"},
            {**_reads(), "POST /api/chat/folders": made},
        )
        (post,) = _writes(dash)
        assert post.path == "/api/chat/folders"
        assert post.body == {"name": "0812", "parent_id": "aaaaaaaaaaaa"}
        assert "kirocrew/0812" in out and "dddddddddddd" in out

    def test_accepts_parent_by_id(self) -> None:
        made = {"id": "dddddddddddd", "name": "x", "parent_id": "bbbbbbbbbbbb"}
        _, dash = _call(
            "chat_folder_create",
            {"name": "x", "parent": "bbbbbbbbbbbb"},
            {**_reads(), "POST /api/chat/folders": made},
        )
        assert dash.sent("POST /api/chat/folders")[-1].body["parent_id"] == "bbbbbbbbbbbb"

    def test_top_level_when_parent_omitted(self) -> None:
        made = {"id": "eeeeeeeeeeee", "name": "Solo", "parent_id": ""}
        _, dash = _call(
            "chat_folder_create", {"name": "Solo"}, {**_reads(), "POST /api/chat/folders": made}
        )
        assert dash.sent("POST /api/chat/folders")[-1].body == {"name": "Solo", "parent_id": ""}

    def test_a_redacted_segment_is_not_recreated_on_every_call(self) -> None:
        """The stored name is the redacted one, so the LOOKUP must use it too.

        Redacting only at the write meant the next call searched for the raw
        text, never matched the folder this walk had just created, and made
        another one — an unbounded pile of same-named siblings and an ambiguous
        path, from a caller simply retrying the same request.
        """
        store: list[dict] = [dict(f) for f in _FOLDERS]
        mint = _minting_post()

        def _post(req: DashboardRequest) -> dict:
            made = mint(req)
            store.append(made)
            return made

        routes = {
            "GET /api/chat/folders": lambda _req: store,
            "GET /api/chat/slots": _SLOTS,
            "POST /api/chat/folders": _post,
        }
        secret = "AKIAIOSFODNN7EXAMPLE"
        args = {"name": "leaf", "parent": f"keys-{secret}"}
        _, first = _call("chat_folder_create", args, routes)
        _, second = _call("chat_folder_create", args, routes)

        posts = [r.body for r in (*_writes(first), *_writes(second))]
        # First call creates the parent + the leaf; the second finds both.
        parents = [p for p in posts if p["name"] != "leaf"]
        assert len(parents) == 1, f"parent recreated: {[p['name'] for p in posts]}"
        assert secret not in parents[0]["name"]
        # And the second call did not re-mint the parent it could now see.
        assert [r.body["name"] for r in _writes(second)] == ["leaf"]

    def test_the_length_limit_is_measured_on_what_gets_stored(self) -> None:
        """Redaction can change the length, and the endpoint truncates the
        redacted form — so a segment that only overruns AFTER redaction must
        still be refused, or it comes back truncated and unmatchable.

        The segment is exactly 100 characters as typed; its access key redacts
        to a longer marker, so the stored form would be 102.
        """
        seg = "k" * 79 + " AKIAIOSFODNN7EXAMPLE"
        assert len(seg) == 100
        out, dash = _call("chat_folder_create", {"name": "leaf", "parent": seg})
        assert "too long" in out
        assert _writes(dash) == []

    def test_a_name_that_only_overruns_after_redaction_is_refused(self) -> None:
        """The schema caps the CALLER's name; redaction can make it longer.

        The endpoint stores ``name[:100]``, so a name that grew past the limit
        during redaction would be persisted truncated — unmatchable by any later
        path, the same mismatch the segment walk refuses.
        """
        out, dash = _call("chat_folder_create", {"name": "k" * 79 + " AKIAIOSFODNN7EXAMPLE"})
        assert "too long after redaction" in out
        assert _writes(dash) == []

    def test_mkdir_p_creates_missing_parent_segments(self) -> None:
        out, dash = _call(
            "chat_folder_create",
            {"name": "week1", "parent": "kirocrew/2026/august"},
            {**_reads(), "POST /api/chat/folders": _minting_post()},
        )
        posts = [r.body for r in _writes(dash)]
        # "kirocrew" exists; "2026" and "august" are created, then the leaf.
        assert [p["name"] for p in posts] == ["2026", "august", "week1"]
        assert posts[0]["parent_id"] == "aaaaaaaaaaaa"
        # Each created segment becomes the next one's parent — the walk threads
        # the freshly minted id through instead of restarting at the top level.
        assert posts[1]["parent_id"] == "new000000001"
        assert posts[2]["parent_id"] == "new000000002"
        assert "created parent path: 2026/august" in out

    def test_partial_mkdir_p_reports_what_was_created(self) -> None:
        replies = iter(
            [
                {"id": "new000000001", "name": "new", "parent_id": ""},
                {"error": "name required"},
            ]
        )
        out, _ = _call(
            "chat_folder_create",
            {"name": "leaf", "parent": "new/deeper"},
            {**_reads(), "POST /api/chat/folders": lambda _req: next(replies)},
        )
        assert out.startswith("Error:")
        assert "created parent path: new" in out

    def test_stale_id_reference_is_not_created_as_a_folder_name(self) -> None:
        """An id-shaped parent that does not exist is a lookup failure.

        Treating it as a path segment would create a folder literally named
        after the hex id.
        """
        out, dash = _call("chat_folder_create", {"name": "x", "parent": "0123456789ab"})
        assert out.startswith("Error:") and "folder not found" in out
        assert _writes(dash) == []

    def test_name_is_required(self) -> None:
        # The schema is registered in validation's dashboard registry, so the
        # table refuses the call before the tool runs, as a clean "Error: ...".
        out, dash = _call("chat_folder_create", {"parent": "kirocrew"})
        assert out == "Error: name: required"
        assert dash.requests == []


class TestAmbiguousFolderPaths:
    """Folder names are not unique within a parent, so a path can be ambiguous.

    Taking the first match would create under — or move into — an arbitrary
    sibling, which is a silent wrong-placement rather than a visible failure.
    """

    # Two folders named "0811" under the same parent, as the sidebar allows.
    DUPES = [
        {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": ""},
        {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
        {"id": "cccccccccccc", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
    ]
    ROUTES = {
        "GET /api/chat/folders": DUPES,
        "GET /api/chat/slots": [{"key": "chat-1-100", "title": "S", "folder_id": ""}],
        "PATCH /api/chat/slots/{slot}/folder": {"ok": True},
    }

    def test_create_refuses_an_ambiguous_parent_path(self) -> None:
        out, dash = _call(
            "chat_folder_create", {"name": "x", "parent": "kirocrew/0811"}, self.ROUTES
        )
        assert out.startswith("Error:")
        assert "bbbbbbbbbbbb" in out and "cccccccccccc" in out
        assert _writes(dash) == []

    def test_move_refuses_an_ambiguous_destination_path(self) -> None:
        out, dash = _call(
            "chat_folder_move", {"folder": "kirocrew", "new_parent": "kirocrew/0811"}, self.ROUTES
        )
        assert out.startswith("Error:") and "pass the folder id" in out
        assert _writes(dash) == []

    def test_session_move_refuses_an_ambiguous_destination_path(self) -> None:
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "kirocrew/0811"},
            self.ROUTES,
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_an_id_still_addresses_one_of_the_duplicates(self) -> None:
        """The refusal must leave a way through: the id is unambiguous."""
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "cccccccccccc"},
            self.ROUTES,
        )
        assert not out.startswith("Error:")
        assert _writes(dash)[-1].body == {"folder_id": "cccccccccccc"}

    def test_mkdir_p_does_not_add_a_third_duplicate_sibling(self) -> None:
        out, dash = _call(
            "chat_folder_create", {"name": "leaf", "parent": "kirocrew/0811/deeper"}, self.ROUTES
        )
        # Ambiguity found mid-walk (not at the final segment), so this is the
        # segment refusal — it must name both duplicates.
        assert out.startswith("Error:") and "share the same parent" in out
        assert "bbbbbbbbbbbb" in out and "cccccccccccc" in out
        assert _writes(dash) == []


class TestSlashBearingFolderNames:
    """A folder NAME may contain '/', so a rendered path has two readings.

    The sidebar permits a folder literally named ``A/B``, which renders exactly
    like ``B`` nested inside ``A``. Resolving the agent's own displayed path to
    the nested pair would act on a different folder than the one it read.
    """

    LITERAL = [{"id": "aaaaaaaaaaaa", "name": "A/B", "parent_id": ""}]
    BOTH = [
        {"id": "aaaaaaaaaaaa", "name": "A/B", "parent_id": ""},
        {"id": "bbbbbbbbbbbb", "name": "A", "parent_id": ""},
        {"id": "cccccccccccc", "name": "B", "parent_id": "bbbbbbbbbbbb"},
    ]

    @staticmethod
    def _routes(folders: list[dict], **writes: Any) -> dict[str, Any]:
        return {
            "GET /api/chat/folders": folders,
            "GET /api/chat/slots": [{"key": "chat-1-100", "title": "S", "folder_id": ""}],
            "PATCH /api/chat/slots/{slot}/folder": {"ok": True},
            "POST /api/chat/folders": writes.get("made", {"error": "unexpected create"}),
        }

    def test_the_literal_folder_wins_when_no_nested_pair_exists(self) -> None:
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "A/B"},
            self._routes(self.LITERAL),
        )
        assert not out.startswith("Error:")
        assert _writes(dash)[-1].body == {"folder_id": "aaaaaaaaaaaa"}

    def test_create_under_a_literal_slash_name_does_not_build_a_nested_pair(self) -> None:
        made = {"id": "dddddddddddd", "name": "leaf", "parent_id": "aaaaaaaaaaaa"}
        _, dash = _call(
            "chat_folder_create",
            {"name": "leaf", "parent": "A/B"},
            self._routes(self.LITERAL, made=made),
        )
        # Exactly one POST: the leaf. No "A" and no "B" were manufactured.
        assert [r.body for r in _writes(dash)] == [{"name": "leaf", "parent_id": "aaaaaaaaaaaa"}]

    def test_collision_between_the_two_readings_is_refused(self) -> None:
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "A/B"},
            self._routes(self.BOTH),
        )
        assert out.startswith("Error:") and "render the same path" in out
        # Both candidate ids are named so the caller can choose one.
        assert "aaaaaaaaaaaa" in out and "cccccccccccc" in out
        assert _writes(dash) == []

    def test_a_reading_divergence_that_survives_the_path_render_is_refused(self) -> None:
        """The two readings can differ without rendering the same path.

        A leading space inside the nested name makes the pair render ``A/ B``
        while the literal folder renders ``A/B``, so the duplicate-path check
        passes and the walk-vs-exact disagreement (the walk strips each segment)
        is the only thing left to catch it.
        """
        padded = [
            {"id": "aaaaaaaaaaaa", "name": "A/B", "parent_id": ""},
            {"id": "bbbbbbbbbbbb", "name": "A", "parent_id": ""},
            {"id": "cccccccccccc", "name": " B", "parent_id": "bbbbbbbbbbbb"},
        ]
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "A/B"},
            self._routes(padded),
        )
        assert out.startswith("Error:") and "ambiguous" in out
        assert "aaaaaaaaaaaa" in out and "cccccccccccc" in out
        assert _writes(dash) == []

    def test_an_id_resolves_either_way(self) -> None:
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "cccccccccccc"},
            self._routes(self.BOTH),
        )
        assert not out.startswith("Error:")
        assert _writes(dash)[-1].body == {"folder_id": "cccccccccccc"}

    def test_the_agent_cannot_mint_a_new_slash_bearing_name(self) -> None:
        """The tool refuses to grow the ambiguity its resolver exists to refuse.

        The sidebar keeps its freedom — a human may still name a folder ``A/B``;
        this only stops the agent adding more unaddressable-by-path names.
        """
        out, dash = _call("chat_folder_create", {"name": "Projects/Web"})
        assert out.startswith("Error:") and "cannot contain '/'" in out
        assert _writes(dash) == []


class TestFolderNameRedaction:
    """A folder name is agent-authored and the sidebar re-renders it forever.

    Persisting a credential the agent quoted into a name would re-display it on
    every visit, so the name takes the egress pass BEFORE the write.
    """

    LEAKY = "AKIAIOSFODNN7EXAMPLE"

    def test_leaf_name_is_redacted_before_the_write(self) -> None:
        _, dash = _call(
            "chat_folder_create",
            {"name": self.LEAKY},
            {
                **_reads(),
                "POST /api/chat/folders": {"id": "dddddddddddd", "name": "x", "parent_id": ""},
            },
        )
        assert self.LEAKY not in _writes(dash)[-1].body["name"]

    def test_created_parent_segments_are_redacted_before_the_write(self) -> None:
        out, dash = _call(
            "chat_folder_create",
            {"name": "leaf", "parent": f"kirocrew/{self.LEAKY}"},
            {**_reads(), "POST /api/chat/folders": _minting_post()},
        )
        assert all(self.LEAKY not in r.body["name"] for r in _writes(dash))
        assert self.LEAKY not in out


class TestFolderMove:
    def test_reparents_by_path(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "new_parent": "kirocrew"},
            {
                **_reads(),
                "PATCH /api/chat/folders/{folder}": {
                    "id": "cccccccccccc",
                    "name": "Travel",
                    "parent_id": "aaaaaaaaaaaa",
                },
            },
        )
        (patch,) = _writes(dash)
        assert patch.path == "/api/chat/folders/cccccccccccc"
        assert patch.body == {"parent_id": "aaaaaaaaaaaa"}
        assert "kirocrew/Travel" in out

    def test_move_to_root(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "kirocrew/0811", "new_parent": "root"},
            {
                **_reads(),
                "PATCH /api/chat/folders/{folder}": {
                    "id": "bbbbbbbbbbbb",
                    "name": "0811",
                    "parent_id": "",
                },
            },
        )
        assert _writes(dash)[-1].body == {"parent_id": ""}
        assert "0811" in out

    def test_root_is_not_a_movable_subject(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "root"})
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_cycle_verdict_comes_from_the_endpoint(self) -> None:
        out, _ = _call(
            "chat_folder_move",
            {"folder": "kirocrew", "new_parent": "kirocrew/0811"},
            {
                **_reads(),
                "PATCH /api/chat/folders/{folder}": {
                    "error": "cannot move a folder into its own descendant"
                },
            },
        )
        assert out.startswith("Error:") and "own descendant" in out

    def test_unknown_folder_errors(self) -> None:
        """Move RESOLVES a folder; it must never create one on the way."""
        out, dash = _call("chat_folder_move", {"folder": "Nope/Missing"})
        assert out.startswith("Error:") and "folder not found" in out
        assert _writes(dash) == []

    def test_resolve_only_walk_refuses_a_mid_path_duplicate(self) -> None:
        """Ambiguity below the addressed path is refused in resolve mode too."""
        dupes = [
            {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": ""},
            {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
            {"id": "cccccccccccc", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "kirocrew/0811/deeper"},
            _reads(folders=dupes, slots=_slots_with_caller()),
        )
        assert out.startswith("Error:") and "share the same parent" in out
        assert "bbbbbbbbbbbb" in out and "cccccccccccc" in out
        assert _writes(dash) == []


class TestFolderDelete:
    """``chat_folder_delete`` asks the endpoint for its empty-only delete."""

    _URL = "/api/chat/folders/cccccccccccc?if_empty=true"

    @staticmethod
    def _routes(reply: Any) -> dict[str, Any]:
        return {**_reads(), "DELETE /api/chat/folders/{folder}": reply}

    def test_deletes_an_empty_folder_by_path(self) -> None:
        out, dash = _call("chat_folder_delete", {"folder": "Travel"}, self._routes({"ok": True}))
        (delete,) = _writes(dash)
        assert (delete.path, delete.body) == (self._URL, None)
        # The verified caller key is what the write carries, not a re-resolved one.
        assert delete.session_key == "dashboard:chat-1-100"
        assert out == "Deleted empty folder `Travel` (id=cccccccccccc)."

    def test_the_tool_does_not_decide_emptiness_itself(self) -> None:
        """No pre-check: a folder with children still goes to the endpoint.

        A read here would be stale by the time the DELETE lands; the endpoint
        decides occupancy under the folder-store lock instead.
        """
        out, dash = _call(
            "chat_folder_delete",
            {"folder": "kirocrew"},
            self._routes({"error": "folder has subfolders", "code": "folder_not_empty"}),
        )
        assert _writes(dash)[-1].path.endswith("?if_empty=true")
        assert out.startswith("Error:") and "subfolders" in out
        assert "chat_folder_move_session" in out

    def test_a_not_empty_refusal_names_no_session(self) -> None:
        out, _ = _call(
            "chat_folder_delete",
            {"folder": "Travel"},
            self._routes({"error": "folder still holds live sessions", "code": "folder_not_empty"}),
        )
        assert out.startswith("Error:") and "live sessions" in out
        assert "chat-" not in out

    def test_a_folder_that_is_not_the_callers_is_left_for_the_person(self) -> None:
        out, _ = _call(
            "chat_folder_delete",
            {"folder": "Travel"},
            self._routes({"error": "x", "code": "folder_not_agent_owned"}),
        )
        assert out.startswith("Error:") and "did not create it" in out
        assert "Leave it for the person" in out

    def test_root_is_not_a_deletable_subject(self) -> None:
        out, dash = _call("chat_folder_delete", {"folder": "root"})
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_unknown_folder_errors(self) -> None:
        out, dash = _call("chat_folder_delete", {"folder": "Nope"})
        assert out.startswith("Error:") and "folder not found" in out
        assert _writes(dash) == []

    def test_the_endpoint_refusal_is_surfaced(self) -> None:
        """An app or crew-member caller is refused by the endpoint, not here."""
        out, _ = _call(
            "chat_folder_delete",
            {"folder": "Travel"},
            self._routes({"error": "an app cannot delete folders"}),
        )
        assert out == "Error: an app cannot delete folders"

    def test_an_unverifiable_caller_cannot_delete(self) -> None:
        out, dash = _call(
            "chat_folder_delete", {"folder": "Travel"}, self._routes({"ok": True}), _UNVERIFIED
        )
        assert out.startswith("Error:") and "deleting a folder" in out
        assert _writes(dash) == []

    def test_folder_is_required(self) -> None:
        out, dash = _call("chat_folder_delete", {})
        assert out == "Error: folder: required"
        assert dash.requests == []


class TestFolderMoveSession:
    @staticmethod
    def _routes(slots: list[dict] | None = None, reply: Any = None) -> dict[str, Any]:
        return {
            **_reads(slots=slots),
            "PATCH /api/chat/slots/{slot}/folder": {"ok": True} if reply is None else reply,
        }

    def test_an_unverifiable_caller_cannot_file_another_session(self) -> None:
        """The lenient resolver would hand a subagent its parent's authority.

        An unresolved identity also reaches the endpoint as no session header at
        all, where it reads as the unconfined dashboard user — so the write is
        refused here rather than sent with an authority nobody can name.
        """
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-3-300", "folder": "kirocrew/0811"},
            self._routes(),
            _UNVERIFIED,
        )
        assert out.startswith("Error:")
        assert "cannot verify which session is calling" in out
        assert _writes(dash) == []

    def test_the_verified_key_is_passed_through_unchanged(self) -> None:
        """Re-resolving inside the transport would carry a different authority.

        The key names a slot the fixture actually holds: a ``dashboard:`` key
        with no matching row is the closed-tab race and is refused, so an absent
        one would exercise that refusal instead of the pass-through.
        """
        _, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-3-300", "folder": "kirocrew/0811"},
            self._routes(),
            Caller.strict("dashboard:chat-2-200"),
        )
        assert _writes(dash)[-1].session_key == "dashboard:chat-2-200"

    def test_moves_by_slot_key_into_a_path(self) -> None:
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-3-300", "folder": "kirocrew/0811"},
            self._routes(reply={"ok": True, "folder_id": "bbbbbbbbbbbb"}),
        )
        (patch,) = _writes(dash)
        assert patch.path == "/api/chat/slots/chat-3-300/folder"
        assert patch.body == {"folder_id": "bbbbbbbbbbbb"}
        assert "kirocrew/0811" in out

    def test_accepts_a_dashboard_session_key(self) -> None:
        _, dash = _call(
            "chat_folder_move_session",
            {"session": "dashboard:chat-1-100", "folder": "Travel"},
            self._routes(),
        )
        assert _writes(dash)[-1].path == "/api/chat/slots/chat-1-100/folder"

    def test_accepts_an_exact_unique_title(self) -> None:
        _, dash = _call(
            "chat_folder_move_session",
            {"session": "folder mcp", "folder": "Travel"},
            self._routes(),
        )
        assert _writes(dash)[-1].path == "/api/chat/slots/chat-2-200/folder"

    def test_ambiguous_title_refuses_rather_than_guessing(self) -> None:
        dupes = [
            {"key": "chat-1-100", "title": "Same", "folder_id": ""},
            {"key": "chat-2-200", "title": "Same", "folder_id": ""},
        ]
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "Same", "folder": "Travel"},
            self._routes(slots=dupes),
        )
        assert out.startswith("Error:") and "chat-1-100" in out and "chat-2-200" in out
        assert _writes(dash) == []

    def test_partial_title_is_not_a_match(self) -> None:
        out, dash = _call(
            "chat_folder_move_session", {"session": "Folder", "folder": "Travel"}, self._routes()
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_unknown_session_names_the_archived_limitation(self) -> None:
        out, _ = _call(
            "chat_folder_move_session",
            {"session": "chat-nope-1", "folder": "Travel"},
            self._routes(),
        )
        assert out.startswith("Error:") and "ARCHIVED" in out

    def test_an_explicit_key_never_falls_through_to_title_matching(self) -> None:
        """`dashboard:` asserts a KEY, so an absent key must not resolve by title.

        Honouring the title here would file a session the caller did not name,
        which is the opposite of what the prefix says.
        """
        rows = [
            {"key": "chat-1-100", "title": "dashboard:chat-9-999", "folder_id": ""},
            {"key": "chat-2-200", "title": "Other", "folder_id": ""},
        ]
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "dashboard:chat-9-999", "folder": "Travel"},
            self._routes(slots=rows),
        )
        assert out.startswith("Error:") and "no live session has the key" in out
        assert _writes(dash) == []

    def test_a_key_that_is_also_another_session_title_is_refused(self) -> None:
        """One session's key can be another session's title — that is ambiguous."""
        rows = [
            {"key": "chat-1-100", "title": "Real one", "folder_id": ""},
            {"key": "chat-2-200", "title": "chat-1-100", "folder_id": ""},
        ]
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "Travel"},
            self._routes(slots=rows),
        )
        assert out.startswith("Error:")
        assert "chat-1-100" in out and "chat-2-200" in out
        assert _writes(dash) == []

    def test_the_dashboard_prefix_selects_the_key_through_that_collision(self) -> None:
        rows = [
            {"key": "chat-1-100", "title": "Real one", "folder_id": ""},
            {"key": "chat-2-200", "title": "chat-1-100", "folder_id": ""},
        ]
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "dashboard:chat-1-100", "folder": "Travel"},
            self._routes(slots=rows),
        )
        assert not out.startswith("Error:")
        assert _writes(dash)[-1].path == "/api/chat/slots/chat-1-100/folder"

    def test_unfile_to_top_level(self) -> None:
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100"},
            self._routes(reply={"ok": True, "folder_id": ""}),
        )
        assert _writes(dash)[-1].body == {"folder_id": ""}
        assert "top level" in out

    def test_unknown_destination_folder_never_reaches_the_endpoint(self) -> None:
        out, dash = _call(
            "chat_folder_move_session", {"session": "chat-1-100", "folder": "Nope"}, self._routes()
        )
        assert out.startswith("Error:") and "folder not found" in out
        assert _writes(dash) == []

    def test_slot_key_is_url_quoted(self) -> None:
        """A slot key can be a folded human name; it must not break the path."""
        odd = _slots_with_caller({"key": "Artifact: My Doc", "title": "Doc", "folder_id": ""})
        _, dash = _call(
            "chat_folder_move_session",
            {"session": "Artifact: My Doc", "folder": "Travel"},
            self._routes(slots=odd),
        )
        assert _writes(dash)[-1].path == "/api/chat/slots/Artifact%3A%20My%20Doc/folder"

    def test_unfile_result_is_redacted(self) -> None:
        """A slot key is a folded human name — it can carry a pasted credential.

        Every other return in these tools goes through redact(); the unfile leg
        echoes the key verbatim, so it needs the same pass or a secret reaches
        the model and the tool-result audit.
        """
        leaky = "AKIAIOSFODNN7EXAMPLE"
        rows = _slots_with_caller({"key": leaky, "title": "Leaky", "folder_id": "aaaaaaaaaaaa"})
        out, _ = _call(
            "chat_folder_move_session",
            {"session": leaky},
            self._routes(slots=rows, reply={"ok": True, "folder_id": ""}),
        )
        assert leaky not in out
        assert "top level" in out

    def test_ambiguous_session_refusal_is_redacted(self) -> None:
        """The refusal lists candidate slot keys — those need redaction too."""
        leaky = "AKIAIOSFODNN7EXAMPLE"
        rows = [
            {"key": leaky, "title": "Same", "folder_id": ""},
            {"key": "chat-2-200", "title": "Same", "folder_id": ""},
        ]
        out, _ = _call(
            "chat_folder_move_session",
            {"session": "Same", "folder": "Travel"},
            self._routes(slots=rows),
        )
        assert out.startswith("Error:")
        assert leaky not in out


class TestNamesTheEndpointWouldTruncate:
    """A name longer than the endpoint's limit is refused, not posted.

    The folder endpoints store ``name[:100]``. Posting a longer one creates a
    folder under a name this server cannot match afterwards, so the NEXT call
    walks the same path, still misses, and creates another sibling — silent
    duplicates under a path the caller never asked for.

    The two arguments are bounded in different places, which is why both are
    tested: ``name`` is capped by its own schema field, while ``parent`` is a
    PATH bounded at 4096, so a single overlong SEGMENT inside it reaches the
    walk and has to be refused there.
    """

    LONG = "x" * 101

    def test_the_schema_refuses_an_overlong_leaf_name(self) -> None:
        out, dash = _call("chat_folder_create", {"name": self.LONG})
        assert out.startswith("Error: name: exceeds max length 100")
        assert dash.requests == []

    def test_a_parent_segment_is_refused_before_any_write(self) -> None:
        """The schema's 4096-char path bound cannot see a per-segment overrun."""
        out, dash = _call("chat_folder_create", {"name": "leaf", "parent": f"Travel/{self.LONG}"})
        assert out.startswith("Error:") and "too long" in out
        assert _writes(dash) == []

    def test_the_refusal_does_not_echo_a_credential(self) -> None:
        """The refusal quotes the name back, so it redacts what it quotes.

        Every refusal this resolver mints redacts at the source, and it has to:
        ``chat_folder_move`` and ``chat_folder_move_session`` return a resolver
        error verbatim, with no redaction at their own return boundary. So this
        is exercised through ``chat_folder_move`` — testing it through
        ``chat_folder_create`` proves nothing, because that tool wraps its whole
        error in ``redact()`` and would mask an unredacted message.
        """
        leaky = "AKIAIOSFODNN7EXAMPLE" + "z" * 90
        out, dash = _call("chat_folder_move", {"folder": f"Travel/{leaky}"})
        assert "too long" in out
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert _writes(dash) == []

    def test_a_segment_at_the_limit_still_creates(self) -> None:
        """Exactly at the limit round-trips, so the guard is off-by-one clean."""
        at_limit = "y" * 100
        made = {"id": "ffffffffffff", "name": at_limit, "parent_id": ""}
        out, dash = _call(
            "chat_folder_create",
            {"name": "leaf", "parent": at_limit},
            {**_reads(), "POST /api/chat/folders": made},
        )
        assert not out.startswith("Error:")
        assert len(_writes(dash)) == 2  # the parent segment, then the leaf


class TestASubagentCannotOutrankItsParent:
    """A subagent key matches no slot, but absence must not read as "no app".

    An app that may not touch a foreign session would otherwise gain that reach
    simply by spawning a helper: the helper's key resolves to nothing, and reading
    "nothing" as unscoped is what grants it. A subagent inherits authority; it
    never mints it.
    """

    MIXED = _reads(
        slots=[
            {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"},
            {"key": "chat-3-300", "title": "Raymond's own", "folder_id": "", "app": ""},
        ]
    )

    def test_a_subagent_is_shown_no_sessions(self) -> None:
        out, _ = _call("chat_folder_tree", {}, self.MIXED, Caller.strict("subagent:abc123"))
        assert out.startswith("Error:")
        assert "runs on behalf of whatever created it" in out
        assert "Radar run" not in out and "Raymond's own" not in out

    def test_a_subagent_cannot_reshape_the_tree(self) -> None:
        out, dash = _call(
            "chat_folder_create", {"name": "Output"}, self.MIXED, Caller.strict("subagent:abc123")
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_a_subagent_cannot_file_a_session(self) -> None:
        """The resolver reads the withheld list, so the write is refused too."""
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "kirocrew/0811"},
            self.MIXED,
            Caller.strict("subagent:abc123"),
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_an_app_cron_is_shown_no_sessions(self) -> None:
        """A cron can be app-created, so a cron key can carry an app's reach."""
        out, _ = _call("chat_folder_tree", {}, self.MIXED, Caller.strict("cron:job-abc123"))
        assert out.startswith("Error:")
        assert "Radar run" not in out and "Raymond's own" not in out

    def test_a_cron_cannot_reshape_the_tree(self) -> None:
        out, dash = _call(
            "chat_folder_create", {"name": "Output"}, self.MIXED, Caller.strict("cron:job-abc123")
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_the_delegated_list_is_knowingly_incomplete(self) -> None:
        """Pins the position, not a claim of completeness.

        The prefix tuple enumerates the delegated key forms that exist today; a
        form added later reads as unscoped until someone adds it here. That gap
        is accepted deliberately, so this asserts the two known forms are
        covered AND that the code says the list is incomplete — if someone
        deletes that admission, this fails and they have to re-argue it.
        """
        assert set(mcp_dashboard._DELEGATED_CALLER_PREFIXES) == {"subagent:", "cron:"}
        src = inspect.getsource(mcp_dashboard_caller_scope)
        marker = src.split("_DELEGATED_CALLER_PREFIXES = ")[0]
        assert "KNOWINGLY INCOMPLETE" in marker

    def test_a_dashboard_caller_whose_slot_is_gone_is_refused(self) -> None:
        """The closed-tab race: the slot is popped, the call is still in flight.

        Slot removal is synchronous and does not drain in-flight MCP calls, so an
        app-owned session's agent can outlive its own row. Reading that absence
        as "no app" would hand it the authority the app does not have.
        """
        out, _ = _call("chat_folder_tree", {}, self.MIXED, Caller.strict("dashboard:chat-gone-999"))
        assert out.startswith("Error:")
        assert "Radar run" not in out and "Raymond's own" not in out

    def test_a_vanished_dashboard_caller_cannot_reshape_the_tree(self) -> None:
        out, dash = _call(
            "chat_folder_create",
            {"name": "Output"},
            self.MIXED,
            Caller.strict("dashboard:chat-gone-999"),
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_the_dashboard_refusal_is_not_the_declined_inversion(self) -> None:
        """Pins the SCOPE of this refusal, which is the reason it is acceptable.

        Refusing every unplaceable caller would also cost Slack threads and
        channel sessions these tools — a tradeoff that was weighed and declined.
        This refusal is narrower by construction: it keys on the ``dashboard:``
        prefix, which those callers do not carry. If someone later widens it
        into the blanket inversion, this test fails and says so.
        """
        rows: list[dict] = []
        assert mcp_dashboard._caller_app_scope("dashboard:gone", rows) is None
        # These have no slot either, and must STAY unscoped.
        assert mcp_dashboard._caller_app_scope("slack:T1:C1:1777", rows) == ""
        assert mcp_dashboard._caller_app_scope("channel:C123", rows) == ""

    def test_a_slack_or_channel_caller_is_still_unscoped(self) -> None:
        """The exemption covers delegated callers only — these have no app."""
        out, _ = _call("chat_folder_tree", {}, self.MIXED, Caller.strict("slack:T1:C1:1777"))
        assert not out.startswith("Error:")
        assert "Radar run" in out and "Raymond's own" in out

    def test_a_subagent_WITH_a_slot_uses_that_slot(self) -> None:
        """Absence is the trigger, not the prefix: a locatable one is scoped."""
        rows = [
            {"key": "abc123", "title": "Helper", "folder_id": "", "app": "issue-radar"},
            {"key": "chat-2-200", "title": "Other app", "folder_id": "", "app": "spec-builder"},
        ]
        out, _ = _call("chat_folder_tree", {}, _reads(slots=rows), Caller.strict("subagent:abc123"))
        assert not out.startswith("Error:")
        assert "Helper" in out
        assert "Other app" not in out


class TestTheFolderPolicyIsTheEndpointsNotThisServers:
    """Folders carry an owner now, so an app HAS a folder of its own to write to
    and this server stops deciding the policy: the tool call reaches the
    endpoint, which bounds the write to the caller's own folders under the store
    lock. A second copy of that rule here could only drift or race it.

    What this layer still decides is the one thing the endpoint cannot — whether
    the caller can be placed at all.
    """

    MIXED = _reads(
        slots=[
            {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"},
            {"key": "chat-3-300", "title": "Raymond's own", "folder_id": "", "app": ""},
        ]
    )
    RADAR = Caller.strict("dashboard:chat-1-100")
    PERSON = Caller.strict("dashboard:chat-3-300")

    def test_an_apps_create_reaches_the_endpoint(self) -> None:
        """An app's create reaches the endpoint, which stamps the owner."""
        made = {"id": "new000000001", "name": "Radar output", "parent_id": ""}
        out, dash = _call(
            "chat_folder_create",
            {"name": "Radar output"},
            {**self.MIXED, "POST /api/chat/folders": made},
            self.RADAR,
        )
        assert not out.startswith("Error:")
        assert dash.sent("POST /api/chat/folders")

    def test_an_apps_move_reaches_the_endpoint(self) -> None:
        moved = {"id": "fldr00000002", "name": "0811", "parent_id": "fldr00000003"}
        out, dash = _call(
            "chat_folder_move",
            {"folder": "kirocrew/0811", "new_parent": "Travel"},
            {**self.MIXED, "PATCH /api/chat/folders/{folder}": moved},
            self.RADAR,
        )
        assert not out.startswith("Error:")
        assert dash.sent("PATCH /api/chat/folders/{folder}")

    def test_the_endpoints_ownership_refusal_is_surfaced_not_reinvented(self) -> None:
        """The tool must report the endpoint's verdict rather than pre-judging
        it — that is what keeps one rule in one place."""
        denied = {"error": "this app does not own that folder", "code": "folder_not_owned"}
        out, _ = _call(
            "chat_folder_move",
            {"folder": "kirocrew/0811", "new_parent": "Travel"},
            {**self.MIXED, "PATCH /api/chat/folders/{folder}": denied},
            self.RADAR,
        )
        assert out.startswith("Error:")
        assert "does not own that folder" in out

    def test_an_app_can_still_file_its_own_session(self) -> None:
        out, dash = _call(
            "chat_folder_move_session",
            {"session": "chat-1-100", "folder": "kirocrew/0811"},
            {**self.MIXED, "PATCH /api/chat/slots/{slot}/folder": {"ok": True}},
            self.RADAR,
        )
        assert not out.startswith("Error:")
        assert dash.sent("PATCH /api/chat/slots/{slot}/folder")

    def test_an_app_can_still_read_the_tree(self) -> None:
        out, _ = _call("chat_folder_tree", {}, self.MIXED, self.RADAR)
        assert not out.startswith("Error:")
        assert "kirocrew" in out

    def test_the_person_keeps_full_authority(self) -> None:
        """Reorganising sessions is the point of the tools — an unscoped caller
        is the person's own agent and is not confined."""
        made = {"id": "new000000001", "name": "Q3", "parent_id": ""}
        out, dash = _call(
            "chat_folder_create",
            {"name": "Q3"},
            {**self.MIXED, "POST /api/chat/folders": made},
            self.PERSON,
        )
        assert not out.startswith("Error:")
        assert dash.sent("POST /api/chat/folders")

    def test_an_unverifiable_caller_cannot_reshape_it_either(self) -> None:
        out, dash = _call("chat_folder_create", {"name": "Q3"}, self.MIXED, _UNVERIFIED)
        assert out.startswith("Error:")
        assert "cannot verify which session is calling" in out
        assert _writes(dash) == []

    def test_an_app_owned_linked_session_is_refused(self) -> None:
        """A channel- or cron-bound slot runs under linked_session_key, and the
        endpoint's staleness guard is dashboard:-only BY DESIGN -- for any other
        shape, absence cannot be told from "never had a slot". So this layer,
        which resolved the scope positively, has to refuse it."""
        rows = [
            {
                "key": "chat-5-500",
                "title": "Radar channel",
                "folder_id": "",
                "app": "issue-radar",
                "linked_session_key": "channel:C123",
            }
        ]
        out, dash = _call(
            "chat_folder_create",
            {"name": "Runs"},
            _reads(slots=rows),
            Caller.strict("channel:C123"),
        )
        assert out.startswith("Error:")
        assert "channel- or schedule-bound" in out
        assert _writes(dash) == []

    def test_a_channel_session_with_no_app_still_works(self) -> None:
        """The refusal is scoped to an APP-owned linked session. A person's own
        channel session never had an app and keeps full authority."""
        rows = [{"key": "chat-5-500", "title": "Mine", "folder_id": "", "app": ""}]
        made = {"id": "new000000001", "name": "Runs", "parent_id": ""}
        out, dash = _call(
            "chat_folder_create",
            {"name": "Runs"},
            {**_reads(slots=rows), "POST /api/chat/folders": made},
            Caller.strict("slack:T1/C1"),
        )
        assert not out.startswith("Error:")
        assert dash.sent("POST /api/chat/folders")
