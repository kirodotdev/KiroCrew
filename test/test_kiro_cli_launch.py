"""Opening a chat's own kiro-cli conversation in the built-in terminal.

Three properties, each pinned here:

* the command is the gateway's own: only the chat slot's name comes from the
  request, and every argv word, the folder and the session id are read from the
  slot, the session map and the files on disk (``plan_kiro_cli_launch``);
* one writer per kiro-cli session: the open refuses a busy chat, and while the
  terminal holds the conversation the chat's own turns and its pre-spawn stand
  down, until the terminal's teardown releases it (``prepare_kiro_cli_launch``,
  ``kiro_cli_handoff``);
* owner-only and gated on the terminal panel, like the terminal itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers, chat_utils, kiro_cli_handoff
from kiro_crew.dashboard.handlers import kiro_cli_launch as kcl
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.wait import async_wait_until

SID = "0b6f1c3e-9a4d-4e2b-8f11-2c3d4e5f6a7b"
KEY = "dashboard:main"
ORIGIN = {"Origin": "http://localhost:5476"}


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch):
    kiro_cli_handoff.reset_holds()
    _floor_monkeypatch.setattr(terminal, "_enabled_cache", [True, time.monotonic()])
    yield
    kiro_cli_handoff.reset_holds()


class _FakeMap:
    def __init__(self, sid=SID, provider="acp", cwd=""):
        self.sid, self.provider, self.cwd = sid, provider, cwd

    def live_sid(self, key):
        return (self.sid, self.provider, self.cwd) if key == KEY else ("", "", "")


def _slot(project, **kw):
    slot = SimpleNamespace(
        key="main",
        project=str(project),
        agent="myagent",
        agent_kind="",
        _app=None,
        app_agent_owner="",
        queue_depth=0,
        _eager_spawn_task=None,
        _lock=asyncio.Lock(),
        appended=[],
    )
    slot.append = lambda role, content, cls, meta=None: slot.appended.append((role, content, meta))
    for name, value in kw.items():
        setattr(slot, name, value)
    return slot


def _state(slot, smap=None, provider=None):
    state = MagicMock()
    state._slots = {"main": slot} if slot is not None else {}
    state._terminal_sessions = {}
    state.owner_id = "owner"
    state.sessions = MagicMock()
    state.sessions._session_map = smap if smap is not None else _FakeMap()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.push_slots_update = MagicMock()
    return state


def _request(state, query=None):
    return SimpleNamespace(app={"state": state}, query=query or {}, get=lambda k, d=None: d)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Every disk and config read the plan makes, answered for a ready chat."""
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "bin" / "kiro-cli"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    calls = SimpleNamespace(resumable=[], agent=[])
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(kcl, "_configured_backend_is_kiro", lambda key: True)

    def resumable(sid):
        calls.resumable.append(sid)
        return True

    monkeypatch.setattr(kcl, "_files_resumable", resumable)
    monkeypatch.setattr(kcl, "_resolve_kiro_binary", lambda: str(binary))
    monkeypatch.setattr(kcl, "_resolve_agent", lambda key, agent, kind: agent)
    monkeypatch.setattr(kcl, "_agent_spec_where", lambda agent, cwd: (True, False))
    monkeypatch.setattr(kcl, "_workspace_config_refusal", lambda cwd, agent: False)
    from kiro_crew.config import paths

    sessions = tmp_path / "kiro-sessions"
    sessions.mkdir()
    monkeypatch.setattr(paths, "kiro_sessions_dir", lambda: sessions)
    return SimpleNamespace(
        project=project, binary=binary, calls=calls, tmp=tmp_path, sessions=sessions
    )


# ── the plan: what would run, built from the gateway's own state ─────────────


@pytest.mark.asyncio
async def test_the_argv_is_built_from_the_chats_own_session_agent_and_folder(wired, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(wired.project)
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(link))), "main")
    assert plan.ok
    assert plan.argv() == [str(wired.binary), "chat", "--resume-id", SID, "--agent", "myagent"]
    assert plan.cwd == str(wired.project.resolve())
    assert plan.session_key == KEY
    assert wired.calls.resumable == [SID]


@pytest.mark.asyncio
async def test_a_chat_that_never_started_has_no_session_to_open(wired):
    state = _state(_slot(wired.project), smap=_FakeMap(sid=""))
    plan = await kcl.plan_kiro_cli_launch(_request(state), "main")
    assert plan.code == kcl.CODE_NO_SESSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sid", ["--agent=evil", "../../etc/passwd", "0b6f1c3e-9a4d-4e2b-8f11-2c3d4e5f6a7b x", "abc"]
)
async def test_a_session_id_not_in_kiro_clis_uuid_shape_is_never_used(wired, sid):
    state = _state(_slot(wired.project), smap=_FakeMap(sid=sid))
    plan = await kcl.plan_kiro_cli_launch(_request(state), "main")
    assert plan.code == kcl.CODE_NO_SESSION
    assert wired.calls.resumable == [], "a malformed id must not even reach the file check"


@pytest.mark.asyncio
async def test_a_missing_replay_log_refuses(wired, monkeypatch):
    monkeypatch.setattr(kcl, "_files_resumable", lambda sid: False)
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(wired.project))), "main")
    assert plan.code == kcl.CODE_NO_LOG


@pytest.mark.asyncio
async def test_a_chat_on_another_backend_refuses(wired, monkeypatch):
    state = _state(_slot(wired.project), smap=_FakeMap(provider="codex"))
    assert (await kcl.plan_kiro_cli_launch(_request(state), "main")).code == kcl.CODE_NOT_KIRO
    monkeypatch.setattr(kcl, "_configured_backend_is_kiro", lambda key: False)
    state = _state(_slot(wired.project))
    assert (await kcl.plan_kiro_cli_launch(_request(state), "main")).code == kcl.CODE_NOT_KIRO


@pytest.mark.asyncio
async def test_a_deleted_project_folder_refuses(wired):
    gone = wired.tmp / "gone"
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(gone))), "main")
    assert plan.code == kcl.CODE_NO_PROJECT


@pytest.mark.asyncio
async def test_no_kiro_cli_installed_refuses(wired, monkeypatch):
    monkeypatch.setattr(kcl, "_resolve_kiro_binary", lambda: "")
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(wired.project))), "main")
    assert plan.code == kcl.CODE_NOT_INSTALLED


@pytest.mark.asyncio
async def test_unknown_channel_and_app_chats_refuse(wired, monkeypatch):
    assert (await kcl.plan_kiro_cli_launch(_request(_state(None)), "main")).code == (
        kcl.CODE_UNKNOWN_CHAT
    )
    app_slot = _slot(wired.project, _app="someapp")
    assert (await kcl.plan_kiro_cli_launch(_request(_state(app_slot)), "main")).code == (
        kcl.CODE_UNSUPPORTED
    )
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: "slack:C1/123.4")
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(wired.project))), "main")
    assert plan.code == kcl.CODE_UNSUPPORTED


@pytest.mark.asyncio
async def test_a_chat_on_a_remote_environment_refuses(wired, monkeypatch):
    monkeypatch.setattr(
        terminal,
        "_local_terminal_refusal",
        AsyncMock(return_value=("remote", "terminal_wrong_machine")),
    )
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(wired.project))), "main")
    assert plan.code == kcl.CODE_REMOTE


@pytest.mark.asyncio
async def test_an_agent_without_a_spec_file_falls_back_to_kiro_clis_default(wired, monkeypatch):
    monkeypatch.setattr(kcl, "_agent_spec_where", lambda agent, cwd: (False, False))
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(wired.project))), "main")
    assert plan.ok
    assert plan.argv() == [str(wired.binary), "chat", "--resume-id", SID]
    assert plan.agent == "" and plan.agent_without_spec == "myagent"


@pytest.mark.asyncio
@pytest.mark.parametrize("in_user", [True, False])
async def test_a_project_agent_file_of_the_same_name_refuses_the_open(wired, monkeypatch, in_user):
    """kiro-cli loads ``--agent <name>`` from the project first, and the chat's
    sandboxed agent can write that folder: its file would run unsandboxed here."""
    monkeypatch.setattr(kcl, "_agent_spec_where", lambda agent, cwd: (in_user, True))
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(wired.project))), "main")
    assert plan.code == kcl.CODE_PROJECT_AGENT


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["-rf", "a/b", "../x", "x y", ""])
async def test_an_agent_name_that_is_not_a_plain_name_is_never_passed(wired, agent):
    plan = await kcl.plan_kiro_cli_launch(
        _request(_state(_slot(wired.project, agent=agent))), "main"
    )
    assert plan.ok
    assert "--agent" not in plan.argv()


def test_agent_specs_are_told_apart_by_where_they_live(tmp_path, monkeypatch):
    from kiro_crew.config import paths

    home_agents = tmp_path / "home-agents"
    home_agents.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: home_agents)
    project = tmp_path / "p"
    (project / ".kiro" / "agents").mkdir(parents=True)
    assert kcl._agent_spec_where("a", str(project)) == (False, False)
    (home_agents / "a.json").write_text("{}")
    assert kcl._agent_spec_where("a", str(project)) == (True, False)
    (project / ".kiro" / "agents" / "b.json").write_text("{}")
    assert kcl._agent_spec_where("b", str(project)) == (False, True)
    # A dangling symlink is still a file kiro-cli would try: it counts.
    (project / ".kiro" / "agents" / "c.json").symlink_to(tmp_path / "missing.json")
    assert kcl._agent_spec_where("c", str(project)) == (False, True)


def test_project_kiro_cli_config_the_open_would_run_refuses_it(tmp_path, monkeypatch):
    """The project's agent files and workspace MCP file are writable by the
    chat's sandboxed agent; kiro-cli started there would run them unsandboxed."""
    from kiro_crew.config import paths

    home_agents = tmp_path / "home-agents"
    home_agents.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: home_agents)
    (home_agents / "sealed.json").write_text(json.dumps({"includeMcpJson": False}))
    (home_agents / "open.json").write_text(json.dumps({"includeMcpJson": True}))
    (home_agents / "silent.json").write_text(json.dumps({}))
    (home_agents / "legacy.json").write_text(
        json.dumps({"includeMcpJson": False, "useLegacyMcpJson": True})
    )
    project = tmp_path / "p"
    project.mkdir()
    p = str(project)
    # A clean project: nothing refuses.
    for agent in ("", "sealed", "open"):
        assert not kcl._workspace_config_refusal(p, agent)
    # Any project agent file refuses kiro-cli's default agent (it may be that one).
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "x.json").write_text("{}")
    assert kcl._workspace_config_refusal(p, "")
    assert not kcl._workspace_config_refusal(p, "sealed")
    (project / ".kiro" / "agents" / "x.json").unlink()
    # A workspace MCP file refuses every agent that may include it.
    for mcp in (project / ".kiro" / "settings" / "mcp.json", project / ".amazonq" / "mcp.json"):
        mcp.parent.mkdir(parents=True, exist_ok=True)
        mcp.write_text("{}")
        assert kcl._workspace_config_refusal(p, "")
        assert kcl._workspace_config_refusal(p, "open")
        assert kcl._workspace_config_refusal(p, "silent"), "an unset flag may include it"
        assert kcl._workspace_config_refusal(p, "missing"), "an unreadable spec may include it"
        assert not kcl._workspace_config_refusal(p, "sealed")
        mcp.unlink()
    (project / ".amazonq" / "mcp.json").write_text("{}")
    assert kcl._workspace_config_refusal(p, "legacy")


@pytest.mark.asyncio
async def test_the_plan_refuses_when_the_project_config_would_run(wired, monkeypatch):
    seen = []

    def refusal(cwd, agent):
        seen.append(agent)
        return True

    monkeypatch.setattr(kcl, "_workspace_config_refusal", refusal)
    plan = await kcl.plan_kiro_cli_launch(_request(_state(_slot(wired.project))), "main")
    assert plan.code == kcl.CODE_PROJECT_AGENT
    assert seen == ["myagent"], "asked about the agent kiro-cli will actually load"


def _log_line(kind, text, extra=()):
    content = [{"kind": "thinking", "data": {"text": ""}}, *extra]
    if text is not None:
        content.append({"kind": "text", "data": text})
    return json.dumps(
        {"version": "v1", "kind": kind, "data": {"message_id": "m", "content": content}}
    )


def test_the_turns_kiro_cli_appended_are_read_back_after_the_offset(tmp_path):
    log = tmp_path / "s.jsonl"
    before = (
        _log_line("Prompt", "old question") + "\n" + _log_line("AssistantMessage", "old") + "\n"
    )
    log.write_text(before)
    offset = log.stat().st_size
    with log.open("a") as fh:
        fh.write(_log_line("Prompt", "What word?") + "\n")
        fh.write(_log_line("AssistantMessage", "Let me check.") + "\n")
        fh.write(
            json.dumps({"version": "v1", "kind": "ToolResults", "data": {"content": []}}) + "\n"
        )
        fh.write(_log_line("AssistantMessage", "Harbor") + "\n")
        fh.write(_log_line("AssistantMessage", None, [{"kind": "toolUse", "data": {}}]) + "\n")
        fh.write('{"version": "v1", "kind": "Prompt", "da')  # a partial trailing line
    assert kcl._read_terminal_turns(str(log), offset) == (
        [("user", "What word?"), ("assistant", "Let me check.\n\nHarbor")],
        0,
    )
    # A log rewritten shorter than the offset: nothing, never the whole log again.
    log.write_text(_log_line("Prompt", "x") + "\n")
    assert kcl._read_terminal_turns(str(log), offset) == ([], 0)
    assert kcl._read_terminal_turns(str(tmp_path / "missing.jsonl"), 0) == ([], 0)


def test_a_huge_tool_result_costs_the_turns_after_it_nothing(tmp_path):
    """Kiro-cli's own compact lines, with a tool result far past any byte cap:
    the reply after it is still read."""
    log = tmp_path / "s.jsonl"

    def compact(kind, text):
        content = [{"kind": "text", "data": text}]
        return json.dumps(
            {"version": "v1", "kind": kind, "data": {"content": content}}, separators=(",", ":")
        )

    blob = json.dumps(
        {"version": "v1", "kind": "ToolResults", "data": {"content": ["x" * (9 * 1024 * 1024)]}},
        separators=(",", ":"),
    )
    log.write_text(
        "\n".join([compact("Prompt", "run it"), blob, compact("AssistantMessage", "done")]) + "\n"
    )
    assert kcl._read_terminal_turns(str(log), 0) == (
        [("user", "run it"), ("assistant", "done")],
        0,
    )


def test_no_line_is_held_whole_and_an_oversized_reply_gets_a_stand_in(tmp_path, monkeypatch):
    """A tool result is drained in chunks however large it grows, and a reply
    past the line cap is never held whole: it is recorded as a stand-in, and
    the reply after it still arrives."""
    monkeypatch.setattr(kcl, "_READ_CHUNK_BYTES", 1024)
    monkeypatch.setattr(kcl, "_TURN_LINE_MAX_BYTES", 8 * 1024)
    log = tmp_path / "s.jsonl"
    tool = json.dumps(
        {"version": "v1", "kind": "ToolResults", "data": {"content": ["x" * 200_000]}},
        separators=(",", ":"),
    )
    lines = [
        _log_line("Prompt", "go"),
        tool,
        _log_line("AssistantMessage", "y" * 50_000),
        _log_line("Prompt", "again"),
        _log_line("AssistantMessage", "fine"),
    ]
    log.write_text("\n".join(lines) + "\n")

    reads: list[int] = []
    real_open = open

    class _Counting:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

        def readline(self, limit=-1):
            assert 0 < limit <= 1024, "a line must be read in bounded pieces"
            piece = self._fh.readline(limit)
            reads.append(len(piece))
            return piece

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def counting_open(path, mode="r", *a, **kw):
        return _Counting(real_open(path, mode, *a, **kw))

    monkeypatch.setattr(kcl, "open", counting_open, raising=False)
    rows, left_out = kcl._read_terminal_turns(str(log), 0)
    assert rows == [
        ("user", "go"),
        ("assistant", kcl._TOO_LONG_TEXT),
        ("user", "again"),
        ("assistant", "fine"),
    ]
    assert left_out == 0
    assert max(reads) <= 1024


def test_an_oversized_line_kiro_cli_never_finished_is_no_message(tmp_path, monkeypatch):
    """A prompt or reply past the cap with no line end is kiro-cli stopped
    mid-write: no stand-in, and it neither counts as left out nor evicts a row."""
    monkeypatch.setattr(kcl, "_READ_CHUNK_BYTES", 1024)
    monkeypatch.setattr(kcl, "_TURN_LINE_MAX_BYTES", 8 * 1024)
    monkeypatch.setattr(kcl, "_TURNS_MAX_ROWS", 2)
    log = tmp_path / "s.jsonl"
    for kind in ("Prompt", "AssistantMessage"):
        unfinished = _log_line(kind, "z" * 50_000)[:-10]
        log.write_text(
            "\n".join([_log_line("Prompt", "hi"), _log_line("AssistantMessage", "yo"), unfinished])
        )
        assert kcl._read_terminal_turns(str(log), 0) == ([("user", "hi"), ("assistant", "yo")], 0)


def test_past_the_row_cap_the_newest_turns_are_kept_and_the_rest_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(kcl, "_TURNS_MAX_ROWS", 4)
    log = tmp_path / "s.jsonl"
    lines = []
    for n in range(5):
        lines += [_log_line("Prompt", f"q{n}"), _log_line("AssistantMessage", f"a{n}")]
    log.write_text("\n".join(lines) + "\n")
    rows, left_out = kcl._read_terminal_turns(str(log), 0)
    assert rows == [("user", "q3"), ("assistant", "a3"), ("user", "q4"), ("assistant", "a4")]
    assert left_out == 6


@pytest.mark.asyncio
async def test_the_terminal_turns_reach_the_transcript_before_the_chat_runs_again(wired, switch):
    """With Tool Search on (the default) the chat's next turn REPLAYS its own
    transcript into a fresh session, so kiro-cli's turns must be in it first."""
    slot = _slot(wired.project)
    state = _state(slot)
    log = wired.sessions / f"{SID}.jsonl"
    log.write_text(_log_line("Prompt", "before") + "\n")
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert launch.log_offset == log.stat().st_size
    launch.announce()
    with log.open("a") as fh:
        fh.write(_log_line("Prompt", "asked in kiro-cli") + "\n")
        fh.write(_log_line("AssistantMessage", "answered in kiro-cli") + "\n")
    seen_held = []
    real_record = launch.record_turns

    def record(turns):
        seen_held.append(kiro_cli_handoff.is_held(KEY))
        real_record(turns)

    launch.record_turns = record
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert seen_held == [True], "recorded while the hold still stands"
    assert not kiro_cli_handoff.is_held(KEY)
    rows = [(role, text) for role, text, _meta in slot.appended]
    assert rows[-3:-1] == [("user", "asked in kiro-cli"), ("assistant", "answered in kiro-cli")]
    assert all(meta == {"origin": "kiro-cli"} for _r, _t, meta in slot.appended[-3:-1])
    assert "kiro-cli exited" in rows[-1][1]


@pytest.mark.asyncio
async def test_turns_left_out_are_said_not_dropped_silently(wired, switch, monkeypatch):
    monkeypatch.setattr(kcl, "_TURNS_MAX_ROWS", 2)
    slot = _slot(wired.project)
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    launch.announce()
    log = wired.sessions / f"{SID}.jsonl"
    log.write_text("\n".join(_log_line("Prompt", f"q{n}") for n in range(3)) + "\n")
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    texts = [text for _role, text, _meta in slot.appended]
    assert "1 earlier messages from kiro-cli were too many" in texts[1]
    assert texts[2:4] == ["q1", "q2"]


@pytest.mark.asyncio
async def test_a_failing_transcript_step_still_frees_the_chat(monkeypatch):
    async def boom():
        raise RuntimeError("disk gone")

    kiro_cli_handoff.hold(KEY, "t", before_release=boom)
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_the_handover_runs_the_plan_the_chat_has_once_the_locks_are_held(
    wired, switch, tmp_path
):
    """A project switch landing before the locks are taken is what runs."""
    moved = tmp_path / "moved"
    moved.mkdir()
    slot = _slot(wired.project)
    state = _state(slot)
    real_plan = kcl.plan_kiro_cli_launch
    calls = []

    async def plan_then_switch(request, name):
        calls.append(name)
        result = await real_plan(request, name)
        if len(calls) == 1:
            slot.project = str(moved)  # the switch commits after the first read
        return result

    import unittest.mock

    with unittest.mock.patch.object(kcl, "plan_kiro_cli_launch", plan_then_switch):
        launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert code == "" and len(calls) == 2
    assert launch.cwd == str(moved.resolve())


@pytest.mark.asyncio
async def test_the_project_folder_falls_back_to_the_folder_the_session_ran_in(wired):
    state = _state(_slot(""), smap=_FakeMap(cwd=str(wired.project)))
    plan = await kcl.plan_kiro_cli_launch(_request(state), "main")
    assert plan.ok and plan.cwd == str(wired.project.resolve())


def test_the_session_map_answers_the_live_resume_target_only(tmp_path, monkeypatch):
    """``live_sid`` folds the history alias, never offers a discarded id, and
    neither stats nor prunes (the plan checks the files itself)."""
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    smap = SessionMap()
    assert smap.live_sid(KEY) == ("", "", "")
    smap.set(KEY, SID, provider="acp", cwd="/w")
    assert smap.live_sid(KEY) == (SID, "acp", "/w")
    # A chat reopened from history is keyed dashboard:dashboard_<name>.
    assert smap.live_sid("dashboard:dashboard_main") == (SID, "acp", "/w")
    # No transcript on disk: still answered, and the entry is not pruned.
    assert smap.live_sid(KEY)[0] == SID
    smap.clear_sid(KEY)
    assert smap.mapped_sid(KEY) == SID, "the history read still names the discarded id"
    assert smap.live_sid(KEY)[0] == "", "a discarded id is never resumed"


# ── the handover: one writer per kiro-cli session ────────────────────────────


@pytest.fixture
def switch(monkeypatch):
    """The reload's helpers the handover reuses, answered for an idle chat."""
    calls = SimpleNamespace(reset=[])

    async def reset(state, slot, key, *, skip_if_busy=False):
        calls.reset.append((key, skip_if_busy))
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", reset)
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *a, **k: False)
    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", AsyncMock(return_value=None))
    return calls


@pytest.mark.asyncio
async def test_the_handover_resets_the_chats_process_and_holds_the_session(wired, switch):
    slot = _slot(wired.project)
    state = _state(slot)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "term1")
    assert code == "" and launch is not None
    assert launch.argv[-4:] == ["--resume-id", SID, "--agent", "myagent"]
    assert switch.reset == [(KEY, True)]
    assert kiro_cli_handoff.holder_of(KEY) == "term1"
    assert slot.appended == [], "nothing is said until the terminal has started"
    launch.announce()
    assert "Opened in kiro-cli" in slot.appended[-1][1]
    # Released by the terminal's teardown: the chat says so and runs again.
    kiro_cli_handoff.release(KEY, "term1")
    assert not kiro_cli_handoff.is_held(KEY)
    assert "kiro-cli exited" in slot.appended[-1][1]
    launch.release()  # idempotent: no second notice
    assert sum("kiro-cli exited" in row[1] for row in slot.appended) == 1


@pytest.mark.asyncio
async def test_an_open_that_never_started_says_nothing_to_the_chat(wired, switch):
    slot = _slot(wired.project)
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    launch.release()
    assert not kiro_cli_handoff.is_held(KEY)
    assert slot.appended == []
    launch.announce()  # too late: the hold is gone
    assert slot.appended == []


@pytest.mark.asyncio
async def test_a_busy_chat_is_refused_and_nothing_is_held(wired, switch, monkeypatch):
    state = _state(_slot(wired.project))
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *a, **k: True)
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (None, kcl.CODE_BUSY)
    state = _state(_slot(wired.project, queue_depth=2))
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *a, **k: False)
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (None, kcl.CODE_BUSY)
    assert switch.reset == []
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_attached_subagents_refuse(wired, switch, monkeypatch):
    monkeypatch.setattr(
        chat_handlers,
        "_subagents_attached_response",
        AsyncMock(return_value=web.Response(status=409)),
    )
    state = _state(_slot(wired.project))
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (
        None,
        kcl.CODE_SUBAGENTS,
    )
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_second_terminal_cannot_take_a_held_session(wired, switch):
    state = _state(_slot(wired.project))
    first, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t1")
    assert first is not None
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t2") == (
        None,
        kcl.CODE_ALREADY_OPEN,
    )
    # A late release by the refused terminal frees nothing.
    assert not kiro_cli_handoff.release(KEY, "t2")
    assert kiro_cli_handoff.holder_of(KEY) == "t1"


@pytest.mark.asyncio
async def test_a_turn_that_slipped_in_declines_the_reset_and_gives_the_hold_back(
    wired, switch, monkeypatch
):
    provider = object()
    state = _state(_slot(wired.project))
    # No provider at the busy read; one registered by the time the reset runs.
    state.sessions.get_provider = MagicMock(side_effect=[None, provider, provider])
    monkeypatch.setattr(chat_handlers, "_reset_slot_session", AsyncMock(return_value=False))
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (None, kcl.CODE_BUSY)
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_pending_pre_spawn_is_cancelled_before_the_reset(wired, switch):
    started = asyncio.Event()

    async def pre_spawn():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(pre_spawn())
    await started.wait()
    state = _state(_slot(wired.project, _eager_spawn_task=task))
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert launch is not None
    assert task.cancelled()


@pytest.mark.asyncio
async def test_the_eager_pre_spawn_stands_down_while_held():
    from kiro_crew.dashboard.chat_turn import eager_spawn

    assert not eager_spawn._open_in_kiro_cli(KEY)
    kiro_cli_handoff.hold(KEY, "t")
    assert eager_spawn._open_in_kiro_cli(KEY)


@pytest.mark.asyncio
async def test_a_dashboard_turn_on_a_held_chat_is_refused_then_runs_after_release(tmp_path):
    """The chat keeps working once kiro-cli exits: the admission refuses only
    while the hold stands, and afterwards the turn reaches provider allocation."""
    from chat_test_helpers import _make_state

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_runner import _run_chat

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    reached = []

    async def get_or_create(*args, **kwargs):
        reached.append(True)
        raise RuntimeError("stop here: the turn reached provider allocation")

    state.sessions.get_or_create = get_or_create
    state.sessions.record_failure = AsyncMock()
    slot = state.get_or_create_slot("chat-kiro-cli-held")
    slot.append("user", "hello", "msg msg-u")
    held_key = chat_utils.effective_session_key(slot)
    kiro_cli_handoff.hold(held_key, "t")
    from unittest.mock import patch

    with (
        patch("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", return_value=KiroCrewConfig()),
        patch("kiro_crew.autonudge.get_instance", return_value=MagicMock()),
    ):
        await _run_chat(state, slot, "hello")
        errors = [m["content"] for m in slot.messages if m["role"] == "error"]
        assert errors == [kiro_cli_handoff.HELD_TURN_REFUSAL]
        assert reached == []
        kiro_cli_handoff.release(held_key, "t")
        await _run_chat(state, slot, "hello again")
    assert reached == [True]


@pytest.mark.asyncio
async def test_a_send_to_a_held_chat_is_refused_before_the_user_row_is_recorded(tmp_path):
    """The composer's send answers 409 and records nothing, so a refused send
    never leaves a user row that reads as an interrupted turn."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.state import _ChatSlot

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-held")
    slot.append("user", "first question", "msg msg-u")
    slot.append("assistant", "first answer", "msg msg-a")
    state._slots[slot.key] = slot
    before = list(slot.messages)
    kiro_cli_handoff.hold(chat_utils.effective_session_key(slot), "t")

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "again"})
        assert resp.status == 409
        body = await resp.json()
        assert body == {"error": kiro_cli_handoff.HELD_TURN_REFUSAL, "code": "kiro_cli_open"}
    assert slot.messages == before
    assert slot.task is None


# ── the availability route: owner-only, terminal-gated ───────────────────────


def _route_app(state, enabled=True, monkeypatch=None):
    @web.middleware
    async def fake_auth(request, handler):
        if "X-Test-NoUser" not in request.headers:
            request["user"] = request.headers.get("X-Test-User", "owner")
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    app["state"] = state
    app.router.add_get("/api/terminal/kiro-cli/{slot}", kcl.api_chat_slot_kiro_cli)
    return app


@pytest.mark.asyncio
async def test_the_availability_route_is_owner_only_and_terminal_gated(wired, monkeypatch):
    state = _state(_slot(wired.project))
    async with TestClient(TestServer(_route_app(state))) as client:
        assert (
            await client.get("/api/terminal/kiro-cli/main", headers={"X-Test-NoUser": "1"})
        ).status == 401
        r = await client.get("/api/terminal/kiro-cli/main", headers={"X-Test-User": "someone"})
        assert r.status == 403
        r = await client.get("/api/terminal/kiro-cli/main")
        body = await r.json()
        assert body == {"available": True, "agent": "myagent", "agent_without_spec": ""}
        assert SID not in json.dumps(body) and str(wired.project) not in json.dumps(body)
        kiro_cli_handoff.hold(KEY, "t")
        body = await (await client.get("/api/terminal/kiro-cli/main")).json()
        assert body["available"] is False and body["code"] == kcl.CODE_ALREADY_OPEN
        monkeypatch.setattr(terminal, "_enabled_cache", [False, time.monotonic()])
        body = await (await client.get("/api/terminal/kiro-cli/main")).json()
        assert body["code"] == kcl.CODE_TERMINAL_DISABLED


@pytest.mark.asyncio
async def test_the_terminal_socket_is_refused_when_the_terminal_is_disabled(monkeypatch):
    from terminal_handler_helpers import _make_app

    monkeypatch.setattr(terminal, "_enabled_cache", [False, time.monotonic()])
    monkeypatch.setattr(terminal, "_get_config", lambda request: {"enabled": False})
    prepare = AsyncMock()
    monkeypatch.setattr(kcl, "prepare_kiro_cli_launch", prepare)
    async with TestClient(TestServer(_make_app())) as client:
        with pytest.raises(Exception):
            await client.ws_connect("/api/ws/terminal/kc1?kiro_cli_slot=main", headers=ORIGIN)
    prepare.assert_not_called()


# ── end to end through the terminal WebSocket ────────────────────────────────


def _fake_kiro_cli(path):
    """A stand-in kiro-cli: prints its argv and folder, exits on one input line."""
    path.write_text(
        "#!/bin/sh\n"
        'printf "ARGV:"; for a in "$@"; do printf "[%s]" "$a"; done; printf "\\n"\n'
        'printf "CWD:%s\\n" "$(pwd -P)"\n'
        "read line\n"
        "exit 0\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


async def _read_until(ws, needle: bytes, timeout=20.0) -> bytes:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    buf = b""
    while needle not in buf:
        remaining = deadline - loop.time()
        assert remaining > 0, f"no {needle!r} in {buf!r}"
        msg = await ws.receive(timeout=remaining)
        if msg.type == web.WSMsgType.BINARY:
            buf += msg.data
        elif msg.type != web.WSMsgType.TEXT:
            raise AssertionError(f"socket closed before {needle!r}: {buf!r}")
    return buf


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")
@pytest.mark.asyncio
async def test_the_terminal_runs_the_gateways_argv_and_releases_on_exit(wired, switch):
    from terminal_handler_helpers import _make_app

    _fake_kiro_cli(wired.binary)
    slot = _slot(wired.project)
    app = _make_app()
    state = app["state"]
    state._slots = {"main": slot}
    state.sessions._session_map = _FakeMap()
    state.sessions.get_provider = MagicMock(return_value=None)
    state.push_slots_update = MagicMock()
    async with TestClient(TestServer(app)) as client:
        # The client's own cwd and any argv-looking param are ignored.
        ws = await client.ws_connect(
            "/api/ws/terminal/kc1?kiro_cli_slot=main&kiro_cli_open=1&cwd=/&argv=sh", headers=ORIGIN
        )
        try:
            out = await _read_until(ws, f"CWD:{os.path.realpath(wired.project)}".encode())
            assert f"ARGV:[chat][--resume-id][{SID}][--agent][myagent]".encode() in out
            assert kiro_cli_handoff.holder_of(KEY) == "kc1"
            registry = state._terminal_sessions
            assert registry["kc1"].kiro_cli_session_key == KEY
            assert registry["kc1"].chat_slot == "main"
            await ws.send_bytes(b"q\n")
            await async_wait_until(
                lambda: not kiro_cli_handoff.is_held(KEY), describe=lambda: "the hold released"
            )
            assert "kc1" not in registry
            assert "kiro-cli exited" in slot.appended[-1][1]
        finally:
            await ws.close()
            for sid in list(state._terminal_sessions):
                sess = state._terminal_sessions.pop(sid, None)
                if sess is not None:
                    await terminal._kill_session(sess)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")
@pytest.mark.asyncio
async def test_a_refused_open_sends_a_coded_error_and_spawns_nothing(wired, switch):
    from terminal_handler_helpers import _make_app

    app = _make_app()
    state = app["state"]
    state._slots = {"main": _slot(wired.project)}
    state.sessions._session_map = _FakeMap(sid="")
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect(
            "/api/ws/terminal/kc2?kiro_cli_slot=main&kiro_cli_open=1", headers=ORIGIN
        )
        frame = await ws.receive_json(timeout=10)
        assert frame["type"] == "error" and frame["code"] == kcl.CODE_NO_SESSION
        assert "no kiro-cli conversation" in frame["message"]
        await ws.receive(timeout=10)
        assert ws.closed
    assert "kc2" not in state._terminal_sessions
    assert switch.reset == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")
@pytest.mark.asyncio
async def test_a_redial_after_kiro_cli_ended_never_reopens_by_itself(wired, switch):
    """A tab redialing a session that is gone (a restart, the orphan reaper)
    sends no ``kiro_cli_open``: it is refused, the chat is not reset or held."""
    from terminal_handler_helpers import _make_app

    app = _make_app()
    state = app["state"]
    slot = _slot(wired.project)
    state._slots = {"main": slot}
    state.sessions._session_map = _FakeMap()
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/api/ws/terminal/kc4?kiro_cli_slot=main", headers=ORIGIN)
        frame = await ws.receive_json(timeout=10)
        assert frame["type"] == "error" and frame["code"] == kcl.CODE_ENDED
        await ws.receive(timeout=10)
        assert ws.closed
    assert "kc4" not in state._terminal_sessions
    assert switch.reset == []
    assert not kiro_cli_handoff.is_held(KEY)
    assert slot.appended == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")
@pytest.mark.asyncio
async def test_a_spawn_that_fails_gives_the_hold_back(wired, switch):
    from terminal_handler_helpers import _make_app

    wired.binary.unlink()  # resolved, then gone before the spawn
    app = _make_app()
    state = app["state"]
    state._slots = {"main": _slot(wired.project)}
    state.sessions._session_map = _FakeMap()
    state.sessions.get_provider = MagicMock(return_value=None)
    state.push_slots_update = MagicMock()
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect(
            "/api/ws/terminal/kc3?kiro_cli_slot=main&kiro_cli_open=1", headers=ORIGIN
        )
        try:
            await async_wait_until(
                lambda: not kiro_cli_handoff.is_held(KEY) and "kc3" not in state._terminal_sessions,
                describe=lambda: "the failed spawn released its hold",
            )
        finally:
            await ws.close()
