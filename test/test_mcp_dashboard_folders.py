"""Tests for the chat (sidebar) folder tools on the kirocrew-dashboard server.

Covers ``chat_folder_tree/create/move/move_session/delete/file_self`` — schema
validation, path→id resolution, mkdir -p, session-reference resolution, the
requests sent, and result formatting. Each case runs one tools/call frame
through ``mcp_dashboard.TABLE`` against an in-memory dashboard and asserts on
the reply and on what the dashboard was asked; the endpoints themselves are
tested by ``test_folder_store_writer.py`` and ``test_dashboard_chat.py``.
"""

from __future__ import annotations

import inspect
import json
import pathlib
from typing import Any

import pytest

from kiro_crew import mcp_dashboard
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.mcp_dashboard import TABLE, _list_tools
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

# Representative GET /api/chat/folders body — a bare JSON array (no envelope),
# each row carrying only parent_id (the human path is derived client-side).
_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": "", "history_count": 3},
    {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa", "history_count": 0},
    {"id": "cccccccccccc", "name": "Travel", "parent_id": "", "history_count": 0},
]

#: The caller slot's birth stamp — ``created`` on every real row. It rides on
#: the self-filing PATCH as ``expected_created`` so the endpoint can refuse a
#: write aimed at a slot that was recreated under the same key.
_CALLER_BORN = "2026-09-14T05:00:00.000001+00:00"

_SLOTS = [
    {
        "key": "chat-1-100",
        "title": "Backup M1",
        "folder_id": "aaaaaaaaaaaa",
        "running": True,
        "created": _CALLER_BORN,
    },
    {"key": "chat-2-200", "title": "Folder MCP", "folder_id": "bbbbbbbbbbbb"},
    {"key": "chat-3-300", "title": "Scratch", "folder_id": ""},
]

#: The caller's OWN slot, which the raw ``/api/chat/slots`` list always carries.
#: A live dashboard session is necessarily in that list, so a fixture that omits
#: it models a state production cannot reach — and one that is now refused,
#: because a ``dashboard:`` key naming an absent slot means the tab was closed
#: mid-call or the key is wrong.
#:
#: Marked non-persistent so it stays LOCATABLE (the scope resolver reads the raw
#: rows) while being filtered out of the RENDERED list — which is what the
#: tree-shape cases below want to assert.
_CALLER_ROW = {
    "key": "chat-1-100",
    "title": "Caller",
    "folder_id": "",
    "memory_mode": "incognito",
    "created": _CALLER_BORN,
}

#: Filing another session requires an identity the gateway vouches for, so every
#: case runs as this verified dashboard session unless it names another caller.
_CALLER = Caller.strict("dashboard:chat-1-100")

#: A caller only the lenient resolver can name — what a spawned subagent's
#: process walk looks like — so every strict check refuses it.
_UNVERIFIED = Caller.unverified("dashboard:chat-1-100")


def _slots_with_caller(*extra: dict) -> list[dict]:
    """The caller's own row plus whatever the case under test needs."""
    return [dict(_CALLER_ROW), *[dict(e) for e in extra]]


def _reads(folders: list[dict] | None = None, slots: list[dict] | None = None) -> dict[str, Any]:
    """The two array endpoints every folder tool reads."""
    return {
        "GET /api/chat/folders": _FOLDERS if folders is None else folders,
        "GET /api/chat/slots": _SLOTS if slots is None else slots,
    }


def _call(
    name: str,
    args: dict[str, Any],
    routes: dict[str, Any] | None = None,
    caller: Caller = _CALLER,
) -> tuple[str, InMemoryDashboardClient]:
    """One tools/call frame against an in-memory dashboard answering ``routes``."""
    dash = InMemoryDashboardClient(_reads() if routes is None else routes)
    return TABLE.call(name, args, ToolContext(dash, caller)), dash


def _writes(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    """Every request that could change the dashboard."""
    return [r for r in dash.requests if r.method != "GET"]


def _minting_post() -> Any:
    """A folder-create route that mints a fresh id per call."""
    count = {"n": 0}

    def _post(req: DashboardRequest) -> dict:
        count["n"] += 1
        return {
            "id": f"new{count['n']:09d}",
            "name": req.body["name"],
            "parent_id": req.body["parent_id"],
        }

    return _post


def _set_folder_sort(value: str) -> None:
    """Write the person's folder sort mode where the dashboard keeps it."""
    (config_dir() / "config.json").write_text(
        json.dumps({"dashboard": {"folder_sort": value}}), encoding="utf-8"
    )


def _config_answering(value: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the config loader answer ``value`` verbatim, or raise it.

    For the two readings a real config file cannot produce: a value the loader
    would never store, and a loader that fails outright.
    """

    def _load(*_args: object, **_kwargs: object) -> Any:
        if isinstance(value, BaseException):
            raise value
        return type("Cfg", (), {"dashboard": type("Dash", (), {"folder_sort": value})()})()

    monkeypatch.setattr(KiroCrewConfig, "load", _load)


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
        src = inspect.getsource(mcp_dashboard)
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
            "chat_session_pin",
            "chat_session_pin_move",
            "session_create",
            "session_fork",
            "session_stop",
            "session_end_wait",
            "session_set_model",
            "session_reload",
            "session_close",
            "session_revive",
            "session_send",
            "session_broadcast",
            "session_status",
            "session_read_message",
            "session_summary",
            "session_adopt",
            "session_release",
        }


#: Four root folders with explicit, contiguous positions — the shape a sidebar
#: drag leaves behind (``computeReorderedFolders`` renumbers 0..n-1), so these
#: cases assert the tool writes what a drag would have written.
_ORDERED = [
    {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
    {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 1},
    {"id": "cccccccccccc", "name": "Charlie", "parent_id": "", "order": 2},
    {"id": "dddddddddddd", "name": "Delta", "parent_id": "", "order": 3},
]


def _positioning(
    folders: list[dict] | None = None,
    slots: list[dict] | None = None,
    patch: Any = None,
    reorder: Any = None,
) -> dict[str, Any]:
    """Routes for a ``chat_folder_move`` that may PATCH a row and POST a reorder.

    A PATCH answers with the row it names, merged with what was written, unless
    ``patch`` says otherwise; the reorder answers ``{"ok": True}`` unless
    ``reorder`` does.
    """
    rows = _ORDERED if folders is None else folders

    def _patch_row(req: DashboardRequest) -> dict:
        fid = req.path.rsplit("/", 1)[-1]
        row = next((dict(f) for f in rows if f["id"] == fid), {"id": fid})
        return {**row, **(req.body or {})}

    return {
        **_reads(folders=rows, slots=_slots_with_caller() if slots is None else slots),
        "PATCH /api/chat/folders/{folder}": _patch_row if patch is None else patch,
        "POST /api/chat/folders/reorder": {"ok": True} if reorder is None else reorder,
    }


def _patched_orders(dash: InMemoryDashboardClient) -> dict[str, int]:
    """``{folder_id: order}`` over every PATCH the call issued."""
    return {
        r.path.rsplit("/", 1)[-1]: r.body["order"]
        for r in dash.sent("PATCH /api/chat/folders/{folder}")
        if "order" in r.body
    }


def _posted_orders(dash: InMemoryDashboardClient) -> dict[str, int]:
    """``{folder_id: order}`` over every atomic reorder POST the call issued.

    The renumber path sends the whole ``{"orders": [{id, order}, ...]}`` list to
    ``/api/chat/folders/reorder`` in ONE request, so a renumber is read off that
    route rather than off per-row PATCHes.
    """
    return {
        str(entry["id"]): entry["order"]
        for r in dash.sent("POST /api/chat/folders/reorder")
        for entry in r.body.get("orders", [])
    }


class TestTheTwoSidesCompareNamesIdentically:
    """The tool's sort key and the sidebar's comparator must not disagree.

    `chat_folder_tree` is where an agent picks a `before`/`after` anchor, so a
    sequence that differs from the rendered one makes the anchor point at the
    wrong gap.

    The key consults no Unicode table. `str.lower()` reads the interpreter's tables
    and `String.prototype.toLowerCase` reads the browser's, so a character whose case
    mapping differs between those two versions would fold differently on each side,
    and neither side owns both tables.

    `A`-`Z` is the exception, folded through a literal table here and by arithmetic on
    the frontend. That range is fixed in every Unicode version, so folding it costs no
    version dependency — and a store written before `order` existed has every sibling
    tied at 0, which makes this tie-break the whole sort for those sidebars.
    """

    def test_ascii_case_folds_so_ordering_stays_alphabetical(self) -> None:
        """`Apple` belongs next to `apricot`, not ahead of every lowercase name."""
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "apricot", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Apple", "parent_id": "", "order": 0},
            {"id": "cccccccccccc", "name": "banana", "parent_id": "", "order": 0},
        ]
        names = [str(f["name"]) for f in mcp_dashboard._chat_folder_siblings(rows, "")]
        assert names == ["Apple", "apricot", "banana"]

    def test_only_ascii_letters_are_folded(self) -> None:
        """Pins the fold's exact reach, so widening it back to a table is caught.

        Every character outside `A`-`Z` must survive byte-for-byte: those are the
        ones whose case mapping can differ between the two runtimes.
        """
        for name in ("ßeta", "İstanbul", "Éclair", "ǅ", "straße"):
            key = mcp_dashboard._chat_folder_name_key({"name": name})
            assert key == name.encode("utf-16-be", "surrogatepass"), name
        assert mcp_dashboard._chat_folder_name_key({"name": "STRASSE"}) == (
            "strasse".encode("utf-16-be")
        )

    def test_a_dotted_capital_i_is_not_folded(self) -> None:
        """The concrete skew shape: a character whose fold is version-dependent.

        `İ` (LATIN CAPITAL I WITH DOT ABOVE) lowercases to a two-character
        sequence in Python, and the exact result has moved across Unicode versions.
        Unfolded it is one code unit on both sides and cannot skew.
        """
        key = mcp_dashboard._chat_folder_name_key({"name": "İ"})
        assert key == b"\x01\x30"
        assert len(key) == 2

    def test_an_astral_name_sorts_by_utf16_code_unit_not_code_point(self) -> None:
        """Python orders str by code point; JavaScript orders by UTF-16 code unit.

        Above U+FFFF the two disagree: an astral character's surrogates begin at
        0xD800, so U+1F600 sorts AFTER U+FF21 by code point and BEFORE it by code
        unit. The frontend comparator uses `<` on strings, so the tool has to speak
        code units or the anchor an agent picks lands in the wrong gap.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "\U0001f600", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Ａ", "parent_id": "", "order": 0},
        ]
        names = [str(f["name"]) for f in mcp_dashboard._chat_folder_siblings(rows, "")]
        assert names == ["\U0001f600", "Ａ"]
        # And the naive key would have put them the other way round.
        assert sorted((str(r["name"]) for r in rows)) == ["Ａ", "\U0001f600"]

    def test_the_key_is_utf16_bytes(self) -> None:
        """Pins the encoding, so a future edit back to a str key is caught."""
        key = mcp_dashboard._chat_folder_name_key({"name": "Ab\U0001f600"})
        assert key == "ab\U0001f600".encode("utf-16-be")


def _fixture_name(row: dict) -> dict:
    """Materialise a fixture row's `name_code_units` into a `name`.

    An unpaired surrogate is legal in a JSON string escape but strict parsers
    reject it, so the shared fixture carries that one name as UTF-16 code units
    and each side builds the identical string from them.
    """
    units = row.get("name_code_units")
    if units is None:
        return {}
    return {"name": "".join(chr(u) for u in units)}


class TestFolderPosition:
    """``before``/``after`` set a folder's place among its siblings.

    The position is written as contiguous 0..n-1 over the destination's
    siblings, which is exactly what a sidebar drag writes — so a tool call and a
    drag leave one convention in the store rather than two.
    """

    def test_after_an_anchor_lands_immediately_behind_it(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert not out.startswith("Error:")
        # Alpha 0, Delta 1, Bravo 2, Charlie 3 -- the whole renumber, including the
        # moved row, lands in ONE atomic reorder request rather than per-row PATCHes.
        assert _posted_orders(dash) == {
            "dddddddddddd": 1,
            "bbbbbbbbbbbb": 2,
            "cccccccccccc": 3,
        }
        # No reparent PATCH: the folder is already at the top level, so nothing
        # but the atomic reorder is written.
        assert not dash.sent("PATCH /api/chat/folders/{folder}")
        assert "after `Alpha`" in out

    def test_before_an_anchor_lands_immediately_ahead_of_it(self) -> None:
        out, dash = _call(
            "chat_folder_move", {"folder": "Delta", "before": "Bravo"}, _positioning()
        )
        assert not out.startswith("Error:")
        assert _posted_orders(dash) == {
            "dddddddddddd": 1,
            "bbbbbbbbbbbb": 2,
            "cccccccccccc": 3,
        }
        assert not dash.sent("PATCH /api/chat/folders/{folder}")
        assert "before `Bravo`" in out

    def test_the_renumber_states_its_container_to_the_endpoint(self) -> None:
        """The reorder POST carries ``expected_parent`` naming the destination.

        The batch is computed from a snapshot of the tree, so the claim is what
        lets the endpoint refuse the renumber (409) when a concurrent reparent
        moves a sibling between that read and the write. The destination here is
        the root lane, and the claim for it is the empty string -- a real value,
        present in the body, not an omitted key.
        """
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert not out.startswith("Error:")
        (reorder,) = dash.sent("POST /api/chat/folders/reorder")
        assert reorder.body["expected_parent"] == ""

    def test_an_anchor_alone_reorders_without_moving(self) -> None:
        """The reason an anchor may stand in for ``new_parent``.

        An omitted ``new_parent`` means the TOP LEVEL, so demanding one would
        make repositioning inside a folder inexpressible: every call would drag
        the folder out to the root as the price of ordering it.
        """
        nested = [
            {"id": "pppppppppppp", "name": "Parent", "parent_id": "", "order": 0},
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "pppppppppppp", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "pppppppppppp", "order": 1},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Parent/Bravo", "before": "Parent/Alpha"},
            _positioning(folders=nested),
        )
        assert not out.startswith("Error:")
        # Stays inside Parent — the anchor chose the destination. Alpha sits at 0
        # and nothing precedes it, so one write puts Bravo ahead of it; and since
        # Bravo stays inside Parent, parent_id is omitted and only the position
        # is written.
        assert [r.body for r in _writes(dash)] == [{"order": -1}]

    def test_only_folders_whose_position_changes_are_written(self) -> None:
        """A no-op reposition must not spend a write per sibling."""
        out, dash = _call("chat_folder_move", {"folder": "Bravo", "after": "Alpha"}, _positioning())
        assert not out.startswith("Error:")
        # Bravo already sits right after Alpha, so every sibling keeps its
        # number -- and with the same parent AND the position it already holds,
        # there is nothing to write at all.
        assert _writes(dash) == []

    def test_a_same_parent_reposition_is_not_reported_as_a_move(self) -> None:
        """ "Moved to `Delta`" would name the folder's OWN path as a destination.

        The reparent did not happen, so the result names what did change.
        """
        out, _ = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert out.startswith("Repositioned folder")
        assert "after `Alpha`" in out and "(top level)" in out
        assert "Moved" not in out

    def test_a_reparent_that_also_positions_says_both(self) -> None:
        out, _ = _call(
            "chat_folder_move",
            {"folder": "Travel", "after": "kirocrew/0811"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert out.startswith("Moved folder")
        assert "kirocrew/Travel" in out and "after `kirocrew/0811`" in out

    def test_a_free_slot_makes_the_whole_reposition_one_write(self) -> None:
        """The answer to "several writes cannot be atomic": usually there is one.

        Several writes CAN land half-applied, since the endpoint takes one row at a
        time. So a position the store already has room for is written as a single
        PATCH and cannot be partial at all; only adjacent neighbours force the
        renumber. Alpha is first, so the slot ahead of it is free.
        """
        out, dash = _call(
            "chat_folder_move", {"folder": "Delta", "before": "Alpha"}, _positioning()
        )
        assert not out.startswith("Error:")
        assert len(_writes(dash)) == 1
        assert _patched_orders(dash) == {"dddddddddddd": -1}

    def test_a_gap_between_neighbours_is_used_instead_of_renumbering(self) -> None:
        """A deleted folder leaves a gap, and a gap is a free slot."""
        gapped = [
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 10},
            {"id": "dddddddddddd", "name": "Delta", "parent_id": "", "order": 20},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Delta", "after": "Alpha"},
            _positioning(folders=gapped),
        )
        assert not out.startswith("Error:")
        assert len(_writes(dash)) == 1
        # Midpoint of the 0..10 gap, so a later insert on either side still fits.
        assert _patched_orders(dash) == {"dddddddddddd": 5}

    def test_an_app_cannot_position_a_folder_whose_subtree_holds_a_foreign_one(self) -> None:
        """Positioning takes the descendants with it, so the blast radius is the subtree.

        The app owns the row it names, so the moved-folder check passes. But the
        person's folder is nested inside it, and repositioning the parent relocates
        the child -- the same violation the endpoint refuses on a reparent, reached
        one level down. Placing AppRoot after AppTwo has a free slot, so it is a
        single order PATCH on AppRoot's own row; the endpoint refuses that write
        because AppRoot's subtree holds the person's folder, and the tool surfaces
        the refusal rather than pre-checking it.
        """
        rows = [
            {
                "id": "aaaaaaaaaaaa",
                "name": "AppRoot",
                "parent_id": "",
                "order": 0,
                "owner_app": "x",
            },
            {"id": "pppppppppppp", "name": "Person", "parent_id": "aaaaaaaaaaaa", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "AppTwo", "parent_id": "", "order": 1, "owner_app": "x"},
        ]
        refused = {"error": "this app does not own that folder", "code": "folder_not_owned"}
        out, dash = _call(
            "chat_folder_move",
            {"folder": "AppRoot", "after": "AppTwo"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}], patch=refused),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # The order PATCH on AppRoot was attempted and refused by the endpoint's
        # subtree guard, not pre-empted in the tool.
        assert dash.sent("PATCH /api/chat/folders/{folder}")

    def test_a_renumber_that_rewrites_a_foreign_row_is_refused_by_the_endpoint(
        self,
    ) -> None:
        """Ownership lives in the endpoint, not a tool-layer pre-check.

        When a renumber's batch includes a row the app does not own, the reorder
        endpoint re-validates every row under the store lock and refuses the whole
        batch, leaving the order untouched. The tool does not pre-check this; it
        sends the batch and surfaces the endpoint's atomic refusal.

        Moving AppTwo just after AppOne renumbers the contiguous 0,1,2 set, so
        Person's row (the person's, not the app's) is one of the writes -- which is
        what the endpoint refuses.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "AppOne", "parent_id": "", "order": 0, "owner_app": "x"},
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 1},
            {"id": "bbbbbbbbbbbb", "name": "AppTwo", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        refused = {
            "error": "this app does not own one of those folders",
            "code": "folder_not_owned",
        }
        out, dash = _call(
            "chat_folder_move",
            {"folder": "AppTwo", "after": "AppOne"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}], reorder=refused),
            Caller.strict("dashboard:chat-1-1"),
        )
        # A refused renumber is reported on the reposition line (not an "Error:"
        # prefix): the reparent, if any, landed and the ordering did not.
        assert "ordering was refused" in out, out
        assert "does not own" in out, out
        assert "stored order is unchanged" in out, out
        # The batch really did name Person (the foreign row), so the endpoint had
        # something to refuse -- the renumber is not silently app-only.
        assert "pppppppppppp" in _posted_orders(dash), out

    def test_an_app_cannot_position_a_folder_it_does_not_own(self) -> None:
        """The tool refuses the position BEFORE any write -- the pinned no-write case.

        Positioning is relative, so it does not need a write to its own target: a
        pure reposition sends no `parent_id`, so the endpoint's reparent rule never
        fires, and renumbering the app's OWN siblings around the person's folder
        changes where the person's folder renders with no write to it for the
        endpoint to refuse. The moved-folder ownership refusal therefore lives in
        the tool, and it fires before any PATCH or reorder is issued -- this test
        pins that no write is attempted at all.
        """
        rows = [
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 0},
            {"id": "aaaaaaaaaaaa", "name": "AppOne", "parent_id": "", "order": 1, "owner_app": "x"},
            {"id": "bbbbbbbbbbbb", "name": "AppTwo", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Person", "after": "AppTwo"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}]),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # No write of any kind: not the free-slot PATCH on the moved row, and not a
        # batched reorder of the app's siblings around it. The refusal is the tool's,
        # not the endpoint's, because a relative renumber can name only owned rows.
        assert _writes(dash) == []

    def test_a_relative_renumber_around_a_foreign_folder_is_refused_with_no_write(
        self,
    ) -> None:
        """The exact reachable gap: the moved row keeps its order, only siblings write.

        Person(order 1) sits between AppA(0) and AppB(2). Placing Person after AppA
        leaves Person at the index it already occupies, so `own_pos is None` and no
        PATCH names Person; without the moved-folder refusal the only writes would
        renumber the app's OWN siblings, every one owned, and both endpoints would
        allow it -- the person's folder relocated by an app with nothing refused.
        The tool-layer moved-folder check closes this: it refuses before computing
        or issuing any write.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "AppA", "parent_id": "", "order": 0, "owner_app": "x"},
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 1},
            {"id": "bbbbbbbbbbbb", "name": "AppB", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Person", "after": "AppA"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}]),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        assert _writes(dash) == []

    def test_a_relative_renumber_of_a_folder_with_a_foreign_subtree_is_refused_with_no_write(
        self,
    ) -> None:
        """The subtree half of the same gap: the moved folder is owned, its child is not.

        AppMid(order 1, app-owned) sits between AppA(0) and AppB(2) and holds the
        person's PersonKid inside it. Placing AppMid after AppA leaves it at index 1,
        so `own_pos is None` and no write names AppMid; the moved-folder OWNERSHIP
        check passes (the app owns AppMid), and without the subtree check the only
        writes would renumber owned siblings, so both endpoints would allow it -- the
        person's nested folder relocated with nothing refused. The tool-layer
        moved-folder SUBTREE check closes this: it refuses before any write.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "AppA", "parent_id": "", "order": 0, "owner_app": "x"},
            {"id": "mmmmmmmmmmmm", "name": "AppMid", "parent_id": "", "order": 1, "owner_app": "x"},
            {"id": "pppppppppppp", "name": "PersonKid", "parent_id": "mmmmmmmmmmmm", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "AppB", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "AppMid", "after": "AppA"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}]),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # No write of any kind: the subtree refusal fires before the renumber.
        assert _writes(dash) == []

    def test_a_row_deeper_than_the_sidebar_draws_is_listed_at_the_cap_depth(self) -> None:
        """Clamp the indentation, keep the row.

        `renderFolderBlock` returns nothing for `depth > 10`, and the store caps
        folder count but not nesting, so a deeper chain is legal. Reporting depth 14
        would claim a level the sidebar never draws — but omitting the row would hide
        the folder AND the live sessions filed in it from an agent's only tree view,
        to buy indentation parity on a row nobody can see.
        """
        rows = [
            {
                "id": f"{i:012d}",
                "name": f"L{i}",
                "parent_id": "" if i == 0 else f"{i - 1:012d}",
                "order": 0,
            }
            for i in range(14)
        ]
        order = mcp_dashboard._chat_folder_render_order(rows)
        assert len(order) == 14, "every folder is listed"
        depths = [d for _fid, d in order]
        assert max(depths) == mcp_dashboard._SIDEBAR_MAX_DRAWN_DEPTH
        assert depths.count(0) == 1, "the deep rows are not relocated to the top level"
        assert depths == list(range(11)) + [10, 10, 10]

    def test_a_saturated_edge_has_no_free_slot(self) -> None:
        """One past the bound reads back AS the bound, so it is not outside anything.

        `_free_slot_order` earns its single write by naming an order no sibling
        holds. At the numeric limit that is impossible: the value it would return
        comes back through the same clamp as the anchor's own order, so the pair
        ties and the name tie-break — not the requested side — decides where the
        folder lands. There is no representable slot, so the caller must renumber.
        """
        limit = mcp_dashboard._CHAT_FOLDER_ORDER_LIMIT
        at_top = [{"id": "aaaaaaaaaaaa", "name": "A", "parent_id": "", "order": limit}]
        at_bottom = [{"id": "bbbbbbbbbbbb", "name": "B", "parent_id": "", "order": -limit}]
        assert mcp_dashboard._free_slot_order(at_top, 1) is None
        assert mcp_dashboard._free_slot_order(at_bottom, 0) is None
        # The other edge of each is still free: only the saturated side is refused.
        assert mcp_dashboard._free_slot_order(at_top, 0) == limit - 1
        assert mcp_dashboard._free_slot_order(at_bottom, 1) == -limit + 1

    def test_the_shared_golden_fixture_orders_identically_on_this_side(self) -> None:
        """One fixture, both suites — see test/fixtures/chat_folder_sibling_order.json.

        `folderTree.test.ts` drives these same rows through `bySidebarOrder`, so the
        agreement between the tool's order and the sidebar's is checked by one
        artifact rather than asserted in prose on each side. A coercion that changes
        on either side fails one of the two runs.
        """
        spec = json.loads(
            (
                pathlib.Path(__file__).parent / "fixtures" / "chat_folder_sibling_order.json"
            ).read_text()
        )
        modes_seen: set[str] = set()
        for case in spec["cases"]:
            rows = [{**r, "parent_id": "", **_fixture_name(r)} for r in case["rows"]]
            mode = case.get("mode", "custom")
            modes_seen.add(mode)
            got = [f["id"] for f in mcp_dashboard._chat_folder_siblings(rows, "", mode)]
            assert got == case["expected"], case["name"]
            # The listing walk is the sort the tree tool prints; it must agree
            # with the sibling sort for the same mode, at the root as at depth.
            walked = [fid for fid, _depth in mcp_dashboard._chat_folder_render_order(rows, mode)]
            assert walked == case["expected"], f"render order: {case['name']}"
        # Every mode the loader admits is exercised by at least one case, so a
        # fourth mode cannot land with no parity row.
        assert modes_seen == set(mcp_dashboard.FOLDER_SORT_MODES)

    def test_no_persisted_order_shape_can_raise_out_of_the_sort_key(self) -> None:
        """Totality over the store, checked as a set rather than one shape at a time.

        The folder store is read with a bare `json.loads`, so `order` can arrive as
        anything JSON expresses. `1e999` parses to `inf`, and `int(inf)` raises
        `OverflowError` — which is neither `TypeError` nor `ValueError`. An exception
        escaping a SORT KEY aborts the whole sort, so one row would take down every
        folder tool.
        """
        junk = [float("inf"), float("-inf"), float("nan"), "abc", [1], {"a": 1}, None, True]
        for value in junk:
            row = {"id": "aaaaaaaaaaaa", "name": "X", "parent_id": "", "order": value}
            assert isinstance(mcp_dashboard._chat_folder_order(row), int), value
            # And through the sorts that consume it, which is where a raise lands.
            assert mcp_dashboard._chat_folder_siblings([row], "") == [row], value
            assert mcp_dashboard._chat_folder_render_order([row]) == [("aaaaaaaaaaaa", 0)], value

    def test_a_lone_surrogate_name_does_not_crash_the_sort_key(self) -> None:
        """A raised encoder inside a sort key takes down every folder tool.

        A folder name is persisted JSON, so it can hold a LONE surrogate, and the
        strict `utf-16-be` codec refuses one outright. `surrogatepass` both survives
        it and keeps the byte-for-byte match with the frontend, whose string holds
        that same unit and compares it as 0xD800.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "a\ud800b", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "plain", "parent_id": "", "order": 1},
        ]
        names = [str(f["name"]) for f in mcp_dashboard._chat_folder_siblings(rows, "")]
        assert names == ["a\ud800b", "plain"]
        key = mcp_dashboard._chat_folder_name_key({"name": "a\ud800b"})
        assert key == b"\x00a\xd8\x00\x00b"

    def test_a_same_parent_reposition_sends_no_parent_id(self) -> None:
        """The endpoint reads a present `parent_id` as a reparent.

        It then applies the reparent-only rule that a subtree holding a folder the
        caller does not own cannot be moved. Sending the CURRENT parent back would
        put a pure reposition through a guard about a move that is not happening,
        and an app reordering its own folder that contains one of the person's would
        be refused for no reason.
        """
        out, dash = _call(
            "chat_folder_move", {"folder": "Delta", "before": "Alpha"}, _positioning()
        )
        assert not out.startswith("Error:")
        for write in dash.sent("PATCH /api/chat/folders/{folder}"):
            assert "parent_id" not in write.body, write.body

    def test_a_real_reparent_still_sends_parent_id(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "new_parent": "kirocrew"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert not out.startswith("Error:")
        assert dash.sent("PATCH /api/chat/folders/{folder}")[0].body == {
            "parent_id": "aaaaaaaaaaaa"
        }

    def test_both_anchors_at_once_is_refused(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Delta", "before": "Alpha", "after": "Bravo"},
            _positioning(),
        )
        assert out.startswith("Error:") and "not both" in out
        assert _writes(dash) == []

    def test_an_anchor_outside_the_destination_is_refused(self) -> None:
        """``before``/``after`` names a SIBLING, so it must live in the destination."""
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "new_parent": "root", "after": "kirocrew/0811"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert out.startswith("Error:") and "not in the destination" in out
        assert _writes(dash) == []

    def test_a_folder_cannot_anchor_on_itself(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "Delta"}, _positioning())
        assert out.startswith("Error:") and "relative to itself" in out
        assert _writes(dash) == []

    def test_root_is_not_an_anchor(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "root"}, _positioning())
        assert out.startswith("Error:") and "SIBLING" in out
        assert _writes(dash) == []

    def test_a_missing_anchor_is_refused_before_any_write(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "Echo"}, _positioning())
        assert out.startswith("Error:") and "folder not found" in out
        assert _writes(dash) == []

    def test_a_failed_order_write_says_the_position_itself_landed(self) -> None:
        """A refused reorder is reported as atomic, not half-applied.

        The renumber is one atomic request now, so a refusal leaves the stored
        order untouched -- the message says exactly that and tells the caller to
        re-run. With the parent unchanged there was no move, so it names a
        reposition, not a reparent that did not occur -- in the one message a
        caller reads while deciding what to retry.
        """
        out, _ = _call(
            "chat_folder_move",
            {"folder": "Delta", "after": "Alpha"},
            _positioning(reorder={"error": "this app does not own one of those folders"}),
        )
        assert "ordering was refused" in out, out
        assert "stored order is unchanged" in out, out
        assert "Repositioned folder" in out, out
        assert "Moved folder" not in out, out
        assert not out.startswith("Error:")


class TestPositionRenumberIsAllOrNothingForAnApp:
    """An app may not half-shuffle the person's sidebar.

    Repositioning several siblings is ONE atomic reorder request, and the
    endpoint re-validates this app's ownership of every row under the store lock.
    A batch that names a row the app does not own is refused whole, leaving the
    order untouched -- so the tool relies on the endpoint rather than pre-checking,
    and a refusal cannot land midway.
    """

    OWNED = [
        {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
        {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 1},
        {
            "id": "cccccccccccc",
            "name": "Radar out",
            "parent_id": "",
            "order": 2,
            "owner_app": "issue-radar",
        },
    ]
    RADAR_ROWS = [
        {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"}
    ]
    REFUSED = {"error": "this app does not own one of those folders", "code": "folder_not_owned"}

    def test_renumbering_a_folder_the_app_does_not_own_is_refused_by_the_endpoint(
        self,
    ) -> None:
        # Alpha and Bravo hold adjacent integers, so landing BETWEEN them has no
        # free slot and can only be reached by renumbering the person's two rows --
        # which the reorder endpoint refuses atomically, leaving the order intact.
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "after": "Alpha"},
            _positioning(folders=self.OWNED, slots=self.RADAR_ROWS, reorder=self.REFUSED),
        )
        assert "ordering was refused" in out and "does not own" in out, out
        # The person's Bravo was named in the atomic batch, so there was a foreign
        # row for the endpoint to refuse; no per-row PATCH was ever issued.
        assert "bbbbbbbbbbbb" in _posted_orders(dash), out
        assert not dash.sent("PATCH /api/chat/folders/{folder}")

    def test_an_app_may_place_its_own_folder_where_a_slot_is_free(self) -> None:
        """The rule is about renumbering the person's rows, not about positioning.

        Ahead of Alpha the slot is free, so the app writes only its OWN row: none
        of the person's folders change, and the endpoint judges that single write.
        """
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "before": "Alpha"},
            _positioning(folders=self.OWNED, slots=self.RADAR_ROWS),
        )
        assert not out.startswith("Error:")
        assert len(_writes(dash)) == 1
        assert _patched_orders(dash) == {"cccccccccccc": -1}

    def test_the_same_move_without_a_position_still_reaches_the_endpoint(self) -> None:
        """The refusal is about the RENUMBER, not about moving at all."""
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "new_parent": "Alpha"},
            _positioning(folders=self.OWNED, slots=self.RADAR_ROWS),
        )
        assert not out.startswith("Error:")
        assert dash.sent("PATCH /api/chat/folders/{folder}")

    def test_a_lone_FOREIGN_order_write_is_still_refused(self) -> None:
        """A renumber can change exactly ONE row, and not the moved folder's.

        The endpoint re-validates EVERY row in the batch, so a single foreign row
        (the person's Bravo) is refused whole -- the count is not the question,
        whether any row belongs to someone else is. Here the app's folder already
        holds its target position, so the only row whose order changes is Bravo.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 1},
            {
                "id": "cccccccccccc",
                "name": "Radar out",
                "parent_id": "",
                "order": 1,
                "owner_app": "issue-radar",
            },
        ]
        # Alpha(0) and the app's own Radar out(1) are adjacent, so landing
        # between them renumbers; Radar out keeps position 1 and only the
        # person's Bravo has to move.
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "after": "Alpha"},
            _positioning(folders=rows, slots=self.RADAR_ROWS, reorder=self.REFUSED),
        )
        assert "ordering was refused" in out and "does not own" in out, out
        # The batch named the person's Bravo, which is what the endpoint refuses.
        assert "bbbbbbbbbbbb" in _posted_orders(dash), out
        assert not dash.sent("PATCH /api/chat/folders/{folder}")

    def test_a_person_reordering_their_own_tree_is_not_gated(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Delta", "before": "Alpha"},
            _positioning(
                slots=[{"key": "chat-1-100", "title": "Raymond", "folder_id": "", "app": ""}]
            ),
        )
        assert not out.startswith("Error:")
        # Alpha is first, so the slot ahead of it is free -- one write, no renumber.
        assert _patched_orders(dash) == {"dddddddddddd": -1}

    def test_a_cross_parent_move_needing_a_foreign_renumber_refuses_before_the_reparent(
        self,
    ) -> None:
        """The reparent must NOT commit when the renumber it needs would be refused.

        A cross-parent move sends a reparent PATCH first, then an atomic reorder.
        When the destination has no free slot, the reorder names sibling rows to
        renumber; if one of those is the person's, the endpoint refuses the reorder
        -- but the reparent PATCH has already landed, leaving the folder moved into
        the new parent yet unpositioned. The tool preflights the batch's ownership
        for the reparent case, so a batch that would be refused writes NOTHING: no
        reparent PATCH, no reorder POST. This pins that no write is attempted.
        """
        rows = [
            {
                "id": "tttttttttttt",
                "name": "AppTop",
                "parent_id": "",
                "order": 0,
                "owner_app": "issue-radar",
            },
            {"id": "pppppppppppp", "name": "PersonTop", "parent_id": "", "order": 1},
            {
                "id": "oooooooooooo",
                "name": "Other",
                "parent_id": "",
                "order": 2,
                "owner_app": "issue-radar",
            },
            {
                "id": "cccccccccccc",
                "name": "Mover",
                "parent_id": "oooooooooooo",
                "order": 0,
                "owner_app": "issue-radar",
            },
        ]
        # Mover (own, nested under Other) up to the top level after AppTop(0):
        # AppTop and PersonTop are adjacent, so landing between them renumbers,
        # and the batch names PersonTop (the person's). The move also reparents
        # Mover (parent Other -> top level), so without the preflight the
        # reparent PATCH would commit before the reorder is refused.
        out, dash = _call(
            "chat_folder_move",
            {"folder": "cccccccccccc", "after": "tttttttttttt"},
            _positioning(folders=rows, slots=self.RADAR_ROWS),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # No write of any kind: not the reparent PATCH, not the atomic reorder.
        assert _writes(dash) == []


class TestTreeListsInSidebarOrder:
    """The tree is what an anchor is picked from, so it must show the order the
    person sees. Listing it by path would show a sequence that exists nowhere
    and make every ``before``/``after`` a guess."""

    def test_folders_follow_their_stored_order_not_the_alphabet(self) -> None:
        reversed_alpha = [
            {"id": "aaaaaaaaaaaa", "name": "Zulu", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Mike", "parent_id": "", "order": 1},
            {"id": "cccccccccccc", "name": "Alpha", "parent_id": "", "order": 2},
        ]
        out, _ = _call(
            "chat_folder_tree", {}, _reads(folders=reversed_alpha, slots=_slots_with_caller())
        )
        lines = [ln for ln in out.splitlines() if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert [ln.split()[1] for ln in lines] == ["Zulu", "Mike", "Alpha"]

    def test_a_child_still_follows_its_own_parent(self) -> None:
        """Order sequences siblings; it never reparents the render."""
        nested = [
            {"id": "aaaaaaaaaaaa", "name": "First", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Deep", "parent_id": "aaaaaaaaaaaa", "order": 99},
            {"id": "cccccccccccc", "name": "Second", "parent_id": "", "order": 1},
        ]
        out, _ = _call("chat_folder_tree", {}, _reads(folders=nested, slots=_slots_with_caller()))
        body = out.splitlines()
        order = [ln.split()[1] for ln in body if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert order == ["First", "First/Deep", "Second"]

    def test_a_parent_cycle_neither_hangs_nor_swallows_its_folders(self) -> None:
        cyclic = [
            {"id": "aaaaaaaaaaaa", "name": "Ping", "parent_id": "bbbbbbbbbbbb", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Pong", "parent_id": "aaaaaaaaaaaa", "order": 1},
            {"id": "cccccccccccc", "name": "Sane", "parent_id": "", "order": 2},
        ]
        out, _ = _call("chat_folder_tree", {}, _reads(folders=cyclic, slots=_slots_with_caller()))
        for fid in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            assert fid in out


#: The reporter's scheme: zero-padded prefixes whose stored positions were set
#: by placing them, so the custom order and the name order disagree.
_NUMBERED = [
    {"id": "aaaaaaaaaaaa", "name": "10. Zulu", "parent_id": "", "order": 0},
    {"id": "bbbbbbbbbbbb", "name": "99. Omega", "parent_id": "", "order": 1},
    {"id": "cccccccccccc", "name": "02. Mike", "parent_id": "", "order": 2},
    {"id": "dddddddddddd", "name": "01. Alpha", "parent_id": "", "order": 3},
]
#: The numbered folders as the dashboard serves them; the sort mode is the
#: person's config, written with ``_set_folder_sort`` where a case needs one.
_NUMBERED_ROUTES = _reads(folders=_NUMBERED, slots=_slots_with_caller())


def _tree_folder_names(out: str) -> list[str]:
    return [
        ln.split()[1]
        for ln in out.splitlines()
        if ln.strip().startswith(("aaaa", "bbbb", "cccc", "dddd"))
    ]


class TestTreeHonoursTheFolderSortMode:
    """The tree lists what the sidebar draws, and the sidebar draws the person's
    folder sort mode. The header names the mode so an agent can tell whether the
    sequence it reads is the one a before/after anchor lands in."""

    def test_custom_is_the_stored_order_and_the_header_says_so(self) -> None:
        _set_folder_sort("custom")
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert out.splitlines()[0].endswith("(folder order: custom):")
        assert _tree_folder_names(out) == ["10.", "99.", "02.", "01."]
        assert "chat_folder_move" not in out, "no caveat in custom mode: the anchor IS the view"

    def test_no_setting_reads_as_custom(self) -> None:
        """A dashboard that never saved a mode lists the stored order."""
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert out.splitlines()[0].endswith("(folder order: custom):")

    def test_name_mode_lists_by_natural_name_and_warns_about_anchors(self) -> None:
        _set_folder_sort("name")
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        lines = out.splitlines()
        assert lines[0].endswith("(folder order: name):")
        # The caveat is the SECOND line, before any folder, so it is read before
        # the sequence it qualifies.
        assert lines[1].startswith("Folders are sorted by name, so a before/after anchor")
        assert "stored (custom) position" in lines[1]
        assert _tree_folder_names(out) == ["01.", "02.", "10.", "99."]

    def test_created_mode_lists_newest_first_at_every_depth(self) -> None:
        rows = [
            {
                "id": "aaaaaaaaaaaa",
                "name": "Old root",
                "parent_id": "",
                "order": 0,
                "created_at": 100,
            },
            {
                "id": "bbbbbbbbbbbb",
                "name": "New root",
                "parent_id": "",
                "order": 1,
                "created_at": 300,
            },
            {
                "id": "cccccccccccc",
                "name": "Old child",
                "parent_id": "bbbbbbbbbbbb",
                "order": 0,
                "created_at": 150,
            },
            {
                "id": "dddddddddddd",
                "name": "New child",
                "parent_id": "bbbbbbbbbbbb",
                "order": 1,
                "created_at": 250,
            },
        ]
        _set_folder_sort("created")
        out, _ = _call("chat_folder_tree", {}, _reads(folders=rows, slots=_slots_with_caller()))
        assert out.splitlines()[0].endswith("(folder order: created):")
        ids = [
            ln.split()[0]
            for ln in out.splitlines()
            if ln.strip().startswith(("aaaa", "bbbb", "cccc", "dddd"))
        ]
        # Newest root first, and under it the newest child first -- the same
        # comparator at both depths, as the sidebar applies it.
        assert ids == ["bbbbbbbbbbbb", "dddddddddddd", "cccccccccccc", "aaaaaaaaaaaa"]
        # Every row is stamped, so nothing is said about unstamped ones.
        assert "with no created_at" not in out

    def test_created_mode_says_when_unstamped_folders_sit_last_in_stored_order(self) -> None:
        """A folder from before the stamp existed has none and lists after every
        stamped row in the stored order -- on a pre-upgrade tree that is the order
        the person already had. The header says so, before the rows, the same fact
        the sidebar's menu states under its rows; an agent must not read that tail
        as a date order."""
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "Old A", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "New", "parent_id": "", "order": 1, "created_at": 300},
            {"id": "cccccccccccc", "name": "Old C", "parent_id": "", "order": 2},
        ]
        _set_folder_sort("created")
        out, _ = _call("chat_folder_tree", {}, _reads(folders=rows, slots=_slots_with_caller()))
        lines = out.splitlines()
        assert lines[1].startswith("Folders are sorted by created, so a before/after anchor")
        assert lines[2] == (
            "2 folders with no created_at (made before the stamp existed) list last, "
            "in the stored (custom) order, not by date."
        )
        ids = [ln.split()[0] for ln in lines if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert ids == ["bbbbbbbbbbbb", "aaaaaaaaaaaa", "cccccccccccc"]

    def test_an_unreadable_setting_is_said_rather_than_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The listing survives, in the stored order, and the header says the
        order is assumed -- an agent must not read a failed lookup as a fact."""
        _config_answering(OSError("config unavailable"), monkeypatch)
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        head = out.splitlines()[0]
        assert "folder order: custom, assumed" in head
        assert "config unavailable" in head
        assert _tree_folder_names(out) == ["10.", "99.", "02.", "01."]

    def test_a_value_the_loader_would_never_store_reads_as_custom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_answering("sideways", monkeypatch)
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert out.splitlines()[0].endswith("(folder order: custom):")

    def test_the_mode_reading_is_its_readers_answer(self) -> None:
        """``_chat_folder_sort_mode`` over each reading its config read can give."""

        def _raises() -> object:
            raise OSError("gone")

        assert mcp_dashboard._chat_folder_sort_mode(lambda: "created") == ("created", None)
        assert mcp_dashboard._chat_folder_sort_mode(lambda: "sideways") == ("custom", None)
        assert mcp_dashboard._chat_folder_sort_mode(lambda: 7) == ("custom", None)
        assert mcp_dashboard._chat_folder_sort_mode(_raises) == ("custom", "OSError: gone")

    def test_the_setting_is_read_from_the_config_file_not_over_http(self) -> None:
        """``GET /api/config/kirocrew`` is cookie-only -- it is in neither
        internal-secret allowlist -- so a tool that fetched it would always read
        an auth error and always list the stored order. The read goes through the
        loader instead, like the other dashboard settings the MCP tools read."""
        _set_folder_sort("name")
        assert mcp_dashboard._read_folder_sort_setting() == "name"
        out, dash = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert all("/api/config" not in r.path for r in dash.requests)
        assert out.splitlines()[0].endswith("(folder order: name):")

    def test_a_position_is_computed_in_the_stored_order_whatever_the_mode(self) -> None:
        """Choosing a view mode never rewrites the stored positions, and a
        before/after anchor is a stored-position concept: with the sidebar sorted
        by name, "after 01. Alpha" still lands in the gap after Alpha's STORED
        position (3, the last), not after its displayed one (first)."""
        _set_folder_sort("name")
        out, dash = _call(
            "chat_folder_move",
            {"folder": "aaaaaaaaaaaa", "after": "dddddddddddd"},
            _positioning(folders=_NUMBERED),
        )
        assert not out.startswith("Error"), out
        assert not dash.sent(
            "POST /api/chat/folders/reorder"
        ), "a free slot after the last stored position needs no renumber"
        assert [(r.path, r.body) for r in _writes(dash)] == [
            ("/api/chat/folders/aaaaaaaaaaaa", {"order": 4})
        ]

    def test_the_default_sibling_sort_is_the_custom_order(self) -> None:
        """The placement helpers call ``_chat_folder_siblings`` without a mode and
        mean the stored order; pinning the default keeps a future caller from
        computing a gap in a view that has none."""
        rows = [dict(f) for f in _NUMBERED]
        assert [f["id"] for f in mcp_dashboard._chat_folder_siblings(rows, "")] == [
            f["id"] for f in mcp_dashboard._chat_folder_siblings(rows, "", "custom")
        ]
        assert mcp_dashboard.FOLDER_SORT_DEFAULT == "custom"

    def test_both_tool_descriptions_name_the_mode_contract(self) -> None:
        tools = {t["name"]: t["description"] for t in _list_tools()}
        assert "folder sort mode" in tools["chat_folder_tree"]
        assert "header line names the active mode" in tools["chat_folder_tree"]
        assert "STORED position" in tools["chat_folder_move"]
        assert "chat_folder_tree's header says which" in tools["chat_folder_move"]

    def test_no_persisted_created_at_shape_can_raise_out_of_the_sort_key(self) -> None:
        """Same totality rule as ``order``: the stamp is read with a bare
        ``json.loads``, so every JSON shape must sort rather than raise."""
        junk = [
            float("inf"),
            float("-inf"),
            float("nan"),
            "abc",
            [1],
            {"a": 1},
            None,
            True,
            10**400,
        ]
        for value in junk:
            row = {"id": "aaaaaaaaaaaa", "name": "X", "parent_id": "", "created_at": value}
            assert mcp_dashboard._chat_folder_created(row) is None or value == 10**400, value
            assert mcp_dashboard._chat_folder_siblings([row], "", "created") == [row], value
            assert mcp_dashboard._chat_folder_render_order([row], "created") == [
                ("aaaaaaaaaaaa", 0)
            ]
        assert mcp_dashboard._chat_folder_created({"created_at": 10**400}) == float(
            mcp_dashboard._CHAT_FOLDER_ORDER_LIMIT
        )


class TestPositionIsAdvertised:
    def test_the_tool_declares_both_anchors(self) -> None:
        move = next(t for t in _list_tools() if t["name"] == "chat_folder_move")
        props = move["inputSchema"]["properties"]
        assert "before" in props and "after" in props
        assert move["inputSchema"]["required"] == ["folder"]

    def test_an_anchor_is_accepted_by_the_schema(self) -> None:
        """The schema rejects unknown fields, so an undeclared anchor would 400
        before the handler ever ran."""
        out, _ = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert "unknown field" not in out

    def test_an_unknown_field_is_still_rejected(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "position": "first"})
        assert out.startswith("Error: position: unknown field")
        assert dash.requests == []


class TestFolderFileSelf:
    """``chat_folder_file_self`` files the CALLER's own slot and nothing else.

    It exists because the conductor grant is name-scoped: ``allowedTools`` can
    admit a tool but not an argument, so ``chat_folder_move_session`` (target
    from the arguments) stays behind a prompt on an unattended conductor, and
    the conductor could not put ITSELF in the goal's folder — it floated at the
    top level while its workers sat inside. This verb takes no ``session``
    argument at all; the target is the verified caller key, so the one placement
    it can write is its own.
    """

    @staticmethod
    def _routes(slots: list[dict] | None = None, **extra: Any) -> dict[str, Any]:
        return {
            **_reads(slots=slots),
            "PATCH /api/chat/slots/{slot}/folder": {"ok": True},
            **extra,
        }

    def test_files_the_callers_own_slot_never_an_argument(self) -> None:
        out, dash = _call("chat_folder_file_self", {"folder": "Travel"}, self._routes())
        (patch,) = _writes(dash)
        # Every case runs as the verified caller dashboard:chat-1-100.
        assert patch.path == "/api/chat/slots/chat-1-100/folder"
        assert patch.body == {"folder_id": "cccccccccccc", "expected_created": _CALLER_BORN}
        assert patch.session_key == "dashboard:chat-1-100"
        assert "Travel" in out and "chat-1-100" in out

    def test_the_patch_pins_the_slot_generation_it_resolved(self) -> None:
        """Between the rows read and the PATCH this tab can close and its key be
        recreated for another conversation; the recreated slot shares the
        ``dashboard:<key>`` transcript key, so the endpoint's history pin alone
        cannot tell them apart. The row's ``created`` goes along as
        ``expected_created`` and the endpoint refuses on a mismatch — the write
        can land only on the slot generation this call actually resolved."""
        _, dash = _call("chat_folder_file_self", {"folder": "Travel"}, self._routes())
        assert _writes(dash)[-1].body["expected_created"] == _CALLER_BORN

    def test_a_row_without_a_birth_stamp_sends_no_token(self) -> None:
        """The token is a pin, not a requirement: a row with no ``created``
        (an older gateway) files without one rather than failing."""
        bare = [{k: v for k, v in _SLOTS[0].items() if k != "created"}]
        _, dash = _call("chat_folder_file_self", {"folder": "Travel"}, self._routes(slots=bare))
        assert _writes(dash)[-1].body == {"folder_id": "cccccccccccc"}

    def test_a_session_argument_is_rejected_by_the_schema(self) -> None:
        """No argument may name the target — that is the whole grant argument."""
        out, dash = _call(
            "chat_folder_file_self", {"session": "chat-3-300", "folder": "Travel"}, self._routes()
        )
        assert out.startswith("Error: session: unknown field")
        assert dash.requests == []

    def test_the_tool_advertises_no_session_field(self) -> None:
        tool = next(t for t in _list_tools() if t["name"] == "chat_folder_file_self")
        assert set(tool["inputSchema"]["properties"]) == {"folder"}
        assert "required" not in tool["inputSchema"]

    def test_creates_the_missing_path_under_the_verified_key(self) -> None:
        """mkdir -p, like session_create's ``folder``: one call stands up
        ``<goal>/<agent>`` and files the caller in the leaf."""
        out, dash = _call(
            "chat_folder_file_self",
            {"folder": "Flaky backlog/kirocrew-conductor"},
            self._routes(**{"POST /api/chat/folders": _minting_post()}),
        )
        creates = dash.sent("POST /api/chat/folders")
        assert len(creates) == 2
        assert {r.session_key for r in creates} == {"dashboard:chat-1-100"}
        # Filed in the LEAF the walk just created, not the first segment.
        assert dash.sent("PATCH /api/chat/slots/{slot}/folder")[-1].body == {
            "folder_id": "new000000002",
            "expected_created": _CALLER_BORN,
        }
        assert "created folder path: Flaky backlog/kirocrew-conductor" in out

    def test_unfiles_when_no_folder_is_given(self) -> None:
        out, dash = _call("chat_folder_file_self", {}, self._routes())
        (patch,) = _writes(dash)
        assert (patch.path, patch.body) == (
            "/api/chat/slots/chat-1-100/folder",
            {"folder_id": "", "expected_created": _CALLER_BORN},
        )
        assert out.startswith("Unfiled")

    def test_an_unverifiable_caller_is_refused(self) -> None:
        out, dash = _call(
            "chat_folder_file_self", {"folder": "Travel"}, self._routes(), _UNVERIFIED
        )
        assert out.startswith("Error:") and "cannot verify which session" in out
        assert _writes(dash) == []

    def test_a_caller_with_no_sidebar_slot_has_nothing_to_file(self) -> None:
        """A Slack thread passes the tree-shaping gate (it is the person, with no
        app to be confined to) but owns no slot, so there is no placement."""
        out, dash = _call(
            "chat_folder_file_self",
            {"folder": "Travel"},
            self._routes(),
            Caller.strict("slack:C0123:1700000000.000100"),
        )
        assert out.startswith("Error:") and "no sidebar slot" in out
        assert _writes(dash) == []

    def test_a_closed_tab_mid_call_is_refused_not_filed_as_someone_else(self) -> None:
        """A ``dashboard:`` key naming a slot that is gone is the closed-tab
        race; it must not resolve to any other row."""
        out, dash = _call(
            "chat_folder_file_self",
            {"folder": "Travel"},
            self._routes(),
            Caller.strict("dashboard:chat-9-999"),
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_a_linked_session_is_refused_not_matched_on_its_binding(self) -> None:
        """A channel-bound slot presents ``linked_session_key``, and that binding
        is rebound on live slots with no running gate — so a match on it at
        read time could name a different conversation by the time the PATCH
        lands. Refused, never raced: the slot key is the only stable handle."""
        linked = _slots_with_caller(
            {
                "key": "chat-7-700",
                "title": "Telegram bridge",
                "folder_id": "",
                "linked_session_key": "telegram:4242",
            }
        )
        out, dash = _call(
            "chat_folder_file_self",
            {"folder": "Travel"},
            self._routes(slots=linked),
            Caller.strict("telegram:4242"),
        )
        assert out.startswith("Error:") and "no sidebar slot" in out
        assert _writes(dash) == []

    def test_a_crew_members_pinned_thread_is_not_filed(self) -> None:
        """The member DM thread (``mode == "member"``) spans every goal the
        member runs and lives on the Crew page, outside the sidebar tree. A
        conductor running as a member gets a refusal that names the alternative
        (workers under ``<goal>/<agent>``), and nothing is written."""
        member = [
            {
                "key": "member-atlas",
                "title": "Atlas",
                "folder_id": "",
                "mode": "member",
                "memory_mode": "persistent",
            }
        ]
        out, dash = _call(
            "chat_folder_file_self",
            {"folder": "Travel"},
            self._routes(slots=member),
            Caller.strict("dashboard:member-atlas"),
        )
        assert out.startswith("Error:") and "Crew page" in out and "<goal>/<agent>" in out
        assert _writes(dash) == []

    def test_a_private_session_cannot_file_itself(self) -> None:
        """The caller row in ``_slots_with_caller`` is incognito on purpose."""
        out, dash = _call(
            "chat_folder_file_self", {"folder": "Travel"}, self._routes(slots=_slots_with_caller())
        )
        assert out.startswith("Error:") and "private" in out
        assert _writes(dash) == []

    def test_an_unresolvable_folder_writes_no_placement(self) -> None:
        """Refuse the whole call rather than file into the wrong folder; the
        ambiguity fixture has two ``0811`` siblings under ``kirocrew``."""
        dup = [*_FOLDERS, {"id": "dddddddddddd", "name": "0811", "parent_id": "aaaaaaaaaaaa"}]
        out, dash = _call(
            "chat_folder_file_self",
            {"folder": "kirocrew/0811"},
            {**self._routes(), "GET /api/chat/folders": dup},
        )
        assert out.startswith("Error:")
        assert _writes(dash) == []
