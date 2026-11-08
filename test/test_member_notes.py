# SPDX-License-Identifier: Apache-2.0
"""``save_notes``: a crewmate keeps its briefing without a file tool.

A crewmate's chat runs on the conductor, which mounts no file writer, so its
``[CURRENT ASSIGNMENT]`` notes go through the ``kirocrew-mate`` tool and the
gateway writes ``members/<slug>/briefing.md`` with the read side's open-time
controls. Driven through the real writer, reader, MCP dispatch and handler.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from kiro_crew import mcp_mate, member_goals, member_notes, members

_requires_nofollow = pytest.mark.skipif(
    not members.member_briefing_supported(),
    reason="briefing reads and writes fail closed without O_NOFOLLOW",
)


@_requires_nofollow
def test_saved_notes_are_what_the_next_turn_reads():
    assert member_notes.write_member_notes("ada", "  Ship the release notes.\n ") == 23
    assert members.read_member_briefing("ada") == "Ship the release notes."
    # The whole text is replaced, never appended.
    member_notes.write_member_notes("ada", "Only this now.")
    assert members.read_member_briefing("ada") == "Only this now."
    member_notes.write_member_notes("ada", "")
    assert members.read_member_briefing("ada") == ""


@_requires_nofollow
def test_notes_past_what_the_briefing_injects_are_refused():
    with pytest.raises(member_notes.NotesError) as refused:
        member_notes.write_member_notes("ada", "x" * (members.MEMBER_BRIEFING_MAX_CHARS + 1))
    assert refused.value.code == "notes_too_long"
    assert member_goals.NOTES_MAX_CHARS == members.MEMBER_BRIEFING_MAX_CHARS


@_requires_nofollow
def test_a_symlinked_briefing_is_refused_not_followed(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched", encoding="utf-8")
    directory = members.members_root() / "ada"
    directory.mkdir(parents=True, exist_ok=True)
    os.symlink(target, directory / members.BRIEFING_FILE_NAME)
    with pytest.raises(member_notes.NotesError) as refused:
        member_notes.write_member_notes("ada", "redirected")
    assert refused.value.code == "notes_unwritable"
    assert target.read_text(encoding="utf-8") == "untouched"


def test_notes_that_are_not_text_are_refused_before_any_write():
    with pytest.raises(member_notes.NotesError) as refused:
        member_notes.write_member_notes("ada", ["not", "text"])  # type: ignore[arg-type]
    assert refused.value.code == "invalid_notes"
    assert not (members.members_root() / "ada").exists()


def test_a_platform_without_the_open_time_controls_refuses_the_write(monkeypatch):
    monkeypatch.setattr(members, "member_briefing_supported", lambda: False)
    with pytest.raises(member_notes.NotesError) as refused:
        member_notes.write_member_notes("ada", "anything")
    assert refused.value.code == "notes_unsupported"
    assert not (members.members_root() / "ada").exists()


@_requires_nofollow
def test_a_plain_file_where_the_member_directory_belongs_is_refused():
    root = members.members_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "ada").write_text("not a directory", encoding="utf-8")
    with pytest.raises(member_notes.NotesError) as refused:
        member_notes.write_member_notes("ada", "notes")
    assert refused.value.code == "notes_unwritable"
    assert (root / "ada").read_text(encoding="utf-8") == "not a directory"


@_requires_nofollow
def test_a_failed_write_keeps_the_previous_notes(monkeypatch):
    member_notes.write_member_notes("ada", "The notes before.")

    def failing_write(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(member_notes, "atomic_write_at", failing_write)
    with pytest.raises(member_notes.NotesError) as refused:
        member_notes.write_member_notes("ada", "The notes after.")
    assert refused.value.code == "notes_unwritable"
    assert members.read_member_briefing("ada") == "The notes before."


def test_the_tool_is_listed_validated_and_granted():
    from kiro_crew import agent
    from kiro_crew.validation import MCP_MATE_SCHEMAS

    names = {tool["name"] for tool in mcp_mate._tool_definitions()}
    assert "save_notes" in names and "save_notes" in MCP_MATE_SCHEMAS
    assert "@kirocrew-mate/save_notes" in agent._MATE_AUTO_GRANTS


def test_the_tool_posts_the_text_to_the_callers_own_notes(monkeypatch):
    calls = []

    def fake_post(path, payload, session_key=""):
        calls.append((path, payload, session_key))
        return {"ok": True, "chars": 4}

    monkeypatch.setattr(mcp_mate, "_post", fake_post)
    monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("dashboard_chat-1", ""))
    out = mcp_mate._call_tool_inner("save_notes", {"text": "next"})
    assert calls == [("/api/member/self/notes", {"text": "next"}, "dashboard_chat-1")]
    assert json.loads(out)["ok"] is True


@_requires_nofollow
def test_the_handler_writes_only_the_resolved_crewmates_notes(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import member_goals as handler

    async def caller(_request, _operation):
        return "dashboard_chat-1", "ada", "Ada"

    async def body(_request):
        return {"text": "From the tool."}

    monkeypatch.setattr(handler, "_agent_member", caller)
    monkeypatch.setattr(handler, "_agent_body", body)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.guide._audit", lambda *a, **k: None)
    request = make_mocked_request("POST", "/api/member/self/notes")
    response = asyncio.run(handler.api_member_self_notes(request))
    assert response.status == 200
    assert members.read_member_briefing("ada") == "From the tool."
