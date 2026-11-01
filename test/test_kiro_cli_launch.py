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
import errno
import json
import os
import pathlib
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import kiro_cli_handoff
from kiro_crew.dashboard import chat_handlers, chat_utils
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
    # A lock a contended recovery bound to one test's event loop must not reach
    # the next test's loop.
    _floor_monkeypatch.setattr(kcl, "_recovery_locks", {})
    yield
    kiro_cli_handoff.reset_holds()


#: A kiro-cli session envelope (``<sid>.json``) of the shape kiro-cli 2.21.1 writes.
_ENVELOPE = {
    "session_id": SID,
    "cwd": "/w",
    "title": "t",
    "session_state": {
        "version": "v1",
        "agent_name": "kirocrew-skill-view-0123",
        "permissions": {
            "filesystem": {
                "allowed_read_paths": ["/w", "/elsewhere"],
                "allowed_write_paths": ["/w"],
                "denied_read_paths": ["/w/secret"],
                "denied_write_paths": [],
            },
            "trusted_tools": [{"BuiltIn": "executeCmd"}],
            "denied_tools": [{"BuiltIn": "webFetch"}],
            "allowed_commands": ["git status"],
        },
        "goal": None,
    },
}


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
        running=False,
        _eager_spawn_task=None,
        _lock=asyncio.Lock(),
        appended=[],
        user_broadcasts=[],
    )

    slot.messages = []

    def append(role, content, cls, *, broadcast_user=False, meta=None):
        slot.appended.append((role, content, meta))
        slot.messages.append({"role": role, "content": content, "meta": meta or {}})
        if role == "user":
            slot.user_broadcasts.append(broadcast_user)

    slot.append = append
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
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.push_slots_update = MagicMock()
    return state


def _request(state, query=None):
    return SimpleNamespace(app={"state": state}, query=query or {}, get=lambda k, d=None: d)


def _point_agents_dir(monkeypatch, directory):
    """Make *directory* the user's agents folder for both the open's own writes
    and Crew's shared spec resolver."""
    from kiro_crew import agent
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: directory)
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", directory)


@pytest.fixture(name="wired")
def wired_fixture(monkeypatch, tmp_path):
    """Every disk and config read the plan makes, answered for a ready chat."""
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "bin" / "kiro-cli"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    calls = SimpleNamespace(resumable=[], agent=[])
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(chat_utils, "slot_history_key", lambda slot: f"dashboard:{slot.key}")
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
    # The session's envelope as Crew's own kiro-cli process leaves it: the agent
    # the chat last ran, and tools it trusted for the session.
    (sessions / f"{SID}.json").write_text(json.dumps(_ENVELOPE))
    # The user's agents directory: the chat's agent spec, and where the open
    # writes its launch copy. Never the real one.
    agents = tmp_path / "kiro-agents"
    agents.mkdir()
    (agents / "myagent.json").write_text(json.dumps({"name": "myagent", "prompt": "hi"}))
    _point_agents_dir(monkeypatch, agents)
    records = tmp_path / "kiro-cli-opens"
    monkeypatch.setattr(kiro_cli_handoff, "_records_dir", lambda: records)
    # The live chat's transcript write (save_slot_off_loop on the real state),
    # answered here; ``saves_land`` set False makes it fail.
    calls.saves = []
    calls.saves_land = True

    async def persist(state, slot, history_key):
        calls.saves.append((slot, history_key, len(getattr(slot, "appended", []))))
        if not calls.saves_land:
            raise OSError("disk full")
        return True

    monkeypatch.setattr(kcl, "_persist_live_slot", persist)
    return SimpleNamespace(
        project=project,
        binary=binary,
        calls=calls,
        tmp=tmp_path,
        sessions=sessions,
        agents=agents,
        records=records,
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
    home_agents = tmp_path / "home-agents"
    home_agents.mkdir()
    _point_agents_dir(monkeypatch, home_agents)
    project = tmp_path / "p"
    (project / ".kiro" / "agents").mkdir(parents=True)
    assert kcl._agent_spec_where("a", str(project)) == (False, False)
    (home_agents / "a.json").write_text("{}")
    assert kcl._agent_spec_where("a", str(project)) == (True, False)
    # Found by the name a spec declares, as Crew resolves agents everywhere.
    (home_agents / "pkg-d.json").write_text(json.dumps({"name": "d"}))
    assert kcl._agent_spec_where("d", str(project)) == (True, False)
    # Two specs declaring one name: kiro-cli's pick is undefined, so neither counts.
    (home_agents / "other-d.json").write_text(json.dumps({"name": "d"}))
    assert kcl._agent_spec_where("d", str(project)) == (False, False)
    (project / ".kiro" / "agents" / "b.json").write_text("{}")
    assert kcl._agent_spec_where("b", str(project)) == (False, True)
    # A dangling symlink is still a file kiro-cli would try: it counts.
    (project / ".kiro" / "agents" / "c.json").symlink_to(tmp_path / "missing.json")
    assert kcl._agent_spec_where("c", str(project)) == (False, True)


def test_project_kiro_cli_config_the_open_would_run_refuses_it(tmp_path, monkeypatch):
    """The project's agent files and workspace MCP file are writable by the
    chat's sandboxed agent; kiro-cli started there would run them unsandboxed."""
    home_agents = tmp_path / "home-agents"
    home_agents.mkdir()
    _point_agents_dir(monkeypatch, home_agents)
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
    # Anything in the project's agents folder refuses every agent: kiro-cli
    # matches by the name declared INSIDE a file, and reads *.md specs too.
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    for planted, body in (
        ("x.json", "{}"),
        ("other.json", json.dumps({"name": "sealed", "mcpServers": {"m": {"command": "x"}}})),
        ("other.md", "---\nname: sealed\n---\n"),
        ("notes.txt", "anything"),
    ):
        (agents / planted).write_text(body)
        for agent in ("", "sealed"):
            assert kcl._workspace_config_refusal(p, agent), (planted, agent)
        (agents / planted).unlink()
    assert not kcl._workspace_config_refusal(p, "sealed"), "an empty agents folder is fine"
    # An agents path that cannot be listed is refused, not assumed empty.
    real_listdir = os.listdir
    monkeypatch.setattr(
        kcl.os,
        "listdir",
        lambda path: (
            (_ for _ in ()).throw(PermissionError()) if "agents" in path else real_listdir(path)
        ),
    )
    assert kcl._workspace_config_refusal(p, "sealed")
    monkeypatch.setattr(kcl.os, "listdir", real_listdir)
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


def test_records_past_the_byte_cap_are_skipped_and_a_long_reply_is_counted(tmp_path, monkeypatch):
    """The log is read through the repo's bounded JSONL reader: a tool result
    past the cap costs nothing, a reply past it is left out and counted, and
    the turns after both still arrive. CRLF records parse like LF ones."""
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
    log.write_bytes(("\r\n".join(lines) + "\r\n").encode())
    rows, left_out = kcl._read_terminal_turns(str(log), 0)
    assert rows == [("user", "go"), ("user", "again"), ("assistant", "fine")]
    assert left_out == 1, "the long reply is said, the tool result is not"


def test_the_shared_reader_is_what_reads_the_log(tmp_path, monkeypatch):
    from kiro_crew import jsonl_util

    calls = []
    real = jsonl_util.bounded_raw_records_with_offsets

    def spy(handle, path, *, cap, label="read"):
        calls.append(cap)
        return real(handle, path, cap=cap, label=label)

    monkeypatch.setattr(jsonl_util, "bounded_raw_records_with_offsets", spy)
    log = tmp_path / "s.jsonl"
    log.write_text(_log_line("Prompt", "hi") + "\n")
    assert kcl._read_terminal_turns(str(log), 0) == ([("user", "hi")], 0)
    assert calls == [kcl._TURN_LINE_MAX_BYTES]


def test_an_oversized_line_kiro_cli_never_finished_is_no_message(tmp_path, monkeypatch):
    """A prompt or reply past the cap with no line end is kiro-cli stopped
    mid-write: it neither counts as left out nor evicts a row."""
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

    async def record(turns, left_out=0):
        seen_held.append(kiro_cli_handoff.is_held(KEY))
        return await real_record(turns, left_out)

    launch.record_turns = record
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert seen_held == [True], "recorded while the hold still stands"
    assert not kiro_cli_handoff.is_held(KEY)
    rows = [(role, text) for role, text, _meta in slot.appended]
    assert rows[-3:-1] == [("user", "asked in kiro-cli"), ("assistant", "answered in kiro-cli")]
    assert all(meta["origin"] == "kiro-cli" for _r, _t, meta in slot.appended[-3:-1])
    assert slot.user_broadcasts == [True], "a prompt typed in the terminal is shown live"
    assert "kiro-cli exited" in rows[-1][1]


async def _closed_chat_run(wired, tmp_path, *, prompts, before_exit=None):
    """Open, let kiro-cli append *prompts* (each answered "Done."), close the tab,
    run *before_exit*, then let kiro-cli exit. Returns ``(conversation_log, slot)``."""
    from kiro_crew.history import ConversationLog

    conversation_log = ConversationLog(base_dir=tmp_path / "history")
    conversation_log.append(KEY, "user", "continue")
    conversation_log.append(KEY, "assistant", "Done.")
    slot = _slot(wired.project)
    slot._disk_meta_created_at = str(conversation_log.get_metadata(KEY).get("created_at") or "")
    state = _state(slot)
    state.conversation_log = conversation_log
    log = wired.sessions / f"{SID}.jsonl"
    log.write_text("")
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    launch.announce()
    with log.open("a") as fh:
        for prompt in prompts:
            fh.write(_log_line("Prompt", prompt) + "\n")
            fh.write(_log_line("AssistantMessage", "Done.") + "\n")
    state._slots.pop("main")  # the tab was closed
    if before_exit is not None:
        before_exit(conversation_log)
    before = len(slot.appended)
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert len(slot.appended) == before, "the closed tab's window is not written"
    assert not kiro_cli_handoff.is_held(KEY)
    return conversation_log, slot


@pytest.mark.asyncio
async def test_a_chat_closed_while_kiro_cli_ran_gets_its_turns_in_its_archived_log(
    wired, switch, tmp_path
):
    """Closing the chat tab does not end the terminal. When kiro-cli exits after
    that, its turns go to the chat's archived log, which a resume replays: each
    one, even when its text repeats an earlier row."""
    conversation_log, _slot_ = await _closed_chat_run(
        wired, tmp_path, prompts=["continue", "continue"]
    )
    rows = conversation_log.read_messages(KEY)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "continue"),
        ("assistant", "Done."),
    ] * 3
    imported = rows[2:]
    assert all((r.get("meta") or {}).get("origin") == "kiro-cli" for r in imported)
    assert len({(r.get("meta") or {}).get("mid") for r in imported}) == 4, "each row its own id"


@pytest.mark.asyncio
@pytest.mark.parametrize("reopened", [True, False])
async def test_turns_reach_a_reopened_chat_but_never_a_new_one_under_the_name(
    wired, switch, reopened
):
    """Closed and reopened from the same transcript: the chat gets the turns
    and the notice. Deleted and made anew under the same name: it gets neither."""
    slot = _slot(wired.project, _disk_meta_created_at="2026-10-28T20:00:00Z")
    state = _state(slot)
    state.conversation_log = None
    log = wired.sessions / f"{SID}.jsonl"
    log.write_text("")
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    launch.announce()
    log.write_text(
        _log_line("Prompt", "asked") + "\n" + _log_line("AssistantMessage", "answered") + "\n"
    )
    stamp = slot._disk_meta_created_at if reopened else "2026-10-28T21:00:00Z"
    other = _slot(wired.project, _disk_meta_created_at=stamp)
    state._slots["main"] = other
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    texts = [text for _r, text, _m in other.appended]
    if reopened:
        assert texts[:2] == ["asked", "answered"] and "kiro-cli exited" in texts[-1]
    else:
        assert texts == []


@pytest.mark.asyncio
async def test_a_transcript_deleted_while_kiro_cli_ran_is_not_recreated(wired, switch, tmp_path):
    conversation_log, _slot_ = await _closed_chat_run(
        wired, tmp_path, prompts=["more"], before_exit=lambda cl: cl.delete_session(KEY)
    )
    assert not conversation_log.has_log(KEY)


@pytest.mark.asyncio
async def test_a_transcript_replaced_while_kiro_cli_ran_is_left_alone(wired, switch, tmp_path):
    def replace(cl):
        cl.delete_session(KEY)
        cl.append(KEY, "user", "a different chat")

    conversation_log, _slot_ = await _closed_chat_run(
        wired, tmp_path, prompts=["more"], before_exit=replace
    )
    assert [r["content"] for r in conversation_log.read_messages(KEY)] == ["a different chat"]


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
    assert "1 messages from kiro-cli could not be added here" in texts[1]
    assert texts[2:4] == ["q1", "q2"]
    # A row of the import, so the transcript keeps it, and saved with the rows.
    assert slot.messages[1]["meta"].get(kcl._IMPORT_META_KEY), "tagged as part of the import"
    assert wired.calls.saves and wired.calls.saves[-1][2] >= 4


@pytest.mark.asyncio
async def test_a_closed_chats_transcript_says_how_many_rows_were_left_out(
    wired, switch, tmp_path, monkeypatch
):
    """Closed while kiro-cli ran, the chat gets its turns in its archived log;
    the count of the ones left out goes there too, ahead of the kept ones."""
    monkeypatch.setattr(kcl, "_TURNS_MAX_ROWS", 2)
    conversation_log, _slot_ = await _closed_chat_run(wired, tmp_path, prompts=["a", "b"])
    contents = [r["content"] for r in conversation_log.read_messages(KEY)]
    assert contents[:2] == ["continue", "Done."]
    assert "2 messages from kiro-cli could not be added here" in contents[2]
    assert contents[3:] == ["b", "Done."]


@pytest.mark.asyncio
async def test_a_restarted_opens_left_out_count_reaches_a_closed_chat(wired, monkeypatch):
    monkeypatch.setattr(kcl, "_TURNS_MAX_ROWS", 1)
    _leave_an_open(wired, "", [_log_line("Prompt", "q1"), _log_line("Prompt", "q2")])
    written = []
    monkeypatch.setattr(
        kcl, "_append_to_archived_transcript", lambda *a: written.append(a) or len(a[3])
    )
    state = _state(None)
    state.conversation_log = object()
    assert await kcl.recover_left_open(state, None, KEY)
    [(_log, _key, _c, rows)] = written
    assert "1 messages from kiro-cli could not be added here" in rows[0][1]
    assert [r[1] for r in rows[1:]] == ["q2", kcl._RECOVERED_NOTICE]


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


@pytest.fixture(name="switch")
def switch_fixture(monkeypatch):
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
    spec_name = pathlib.Path(launch.launch_spec).stem
    assert spec_name.startswith(kcl._LAUNCH_SPEC_PREFIX)
    assert launch.argv[-4:] == ["--resume-id", SID, "--agent", spec_name]
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
async def test_an_open_leaves_a_record_on_disk_until_its_hold_is_released(wired, switch):
    """A gateway that stops while the terminal runs never releases the hold, so
    what the release would import is on disk from the open to the release."""
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", "t"
    )
    assert code == "" and launch.recorded
    [path] = list(wired.records.iterdir())
    record = json.loads(path.read_text())
    assert record["session_key"] == KEY and record["sid"] == SID
    assert record["log_offset"] == launch.log_offset
    assert record["launch_spec"] == launch.launch_spec
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0, "owner-only"
    kiro_cli_handoff.release(KEY, "t")
    assert list(wired.records.iterdir()) == []


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_written_refuses_the_open(wired, switch, monkeypatch):
    def fail(*_a, **_k):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(kiro_cli_handoff, "record_open", fail)
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", "t"
    )
    assert (launch, code) == (None, kcl.CODE_RECORD)
    assert code in kcl.KIRO_CLI_REFUSAL_CODES
    assert not kiro_cli_handoff.is_held(KEY)
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"], "no launch copy left"


def _leave_an_open(wired, before: str, after: list[str]) -> None:
    """What a gateway that stopped mid-open leaves: the record, and kiro-cli's
    log grown past the record's offset."""
    log = wired.sessions / f"{SID}.jsonl"
    log.write_text(before)
    offset = log.stat().st_size
    with log.open("a") as fh:
        fh.write("".join(line + "\n" for line in after))
    spec = wired.agents / f"{kcl._LAUNCH_SPEC_PREFIX}0123456789abcdef.json"
    spec.write_text("{}")
    kiro_cli_handoff.record_open(
        KEY,
        {
            "history_key": KEY,
            "created_at": "",
            "log_offset": offset,
            "sid": SID,
            "launch_spec": str(spec),
        },
    )


@pytest.mark.asyncio
async def test_after_a_restart_the_chats_next_turn_brings_the_terminals_turns_in(wired):
    _leave_an_open(
        wired,
        _log_line("Prompt", "old") + "\n",
        [_log_line("Prompt", "asked in kiro-cli"), _log_line("AssistantMessage", "answered")],
    )
    slot = _slot(wired.project)
    state = _state(slot)
    # Nothing may allocate the session from a transcript missing those turns.
    with pytest.raises(Exception) as refused:
        kiro_cli_handoff.refuse_if_held(KEY)
    assert str(refused.value) == kiro_cli_handoff.LEFT_OPEN_REFUSAL
    assert await kcl.recover_left_open(state, slot, KEY)
    assert [(r, t) for r, t, _m in slot.appended[:2]] == [
        ("user", "asked in kiro-cli"),
        ("assistant", "answered"),
    ]
    assert all(m["origin"] == "kiro-cli" for _r, _t, m in slot.appended[:2])
    assert "restarted" in slot.appended[-1][1]
    assert list(wired.records.iterdir()) == []
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"], "launch copy removed"
    kiro_cli_handoff.refuse_if_held(KEY)  # the session allocates again
    assert not await kcl.recover_left_open(state, slot, KEY), "claimed once"


@pytest.mark.asyncio
async def test_a_restarted_open_of_a_closed_chat_goes_to_its_transcript(wired, monkeypatch):
    _leave_an_open(wired, "", [_log_line("Prompt", "q"), _log_line("AssistantMessage", "a")])
    written = []
    monkeypatch.setattr(
        kcl, "_append_to_archived_transcript", lambda *a: written.append(a) or len(a[3])
    )
    state = _state(None)
    state.conversation_log = object()
    assert await kcl.recover_left_open(state, None, KEY)
    assert [(key, [r[:3] for r in rows]) for _log, key, _c, rows in written] == [
        (
            KEY,
            [
                ("user", "q", "msg msg-u"),
                ("assistant", "a", "msg msg-a"),
                ("assistant", kcl._RECOVERED_NOTICE, "msg msg-a"),
            ],
        )
    ]
    assert list(wired.records.iterdir()) == []


def test_a_retried_archive_import_writes_each_row_once(tmp_path):
    """Rows carry their import's tag, so a retry after a partial write adds
    only what is missing."""
    from kiro_crew.history import ConversationLog

    conversation_log = ConversationLog(base_dir=tmp_path / "history")
    conversation_log.append(KEY, "user", "earlier")
    rows = kcl._import_entries([("user", "q", "msg msg-u"), ("assistant", "a", "msg msg-a")], "x1")
    assert kcl._append_to_archived_transcript(conversation_log, KEY, "", rows[:1]) == 1
    assert kcl._append_to_archived_transcript(conversation_log, KEY, "", rows) == 1
    assert [m["content"] for m in conversation_log.read_messages(KEY)] == ["earlier", "q", "a"]


@pytest.mark.asyncio
async def test_a_record_naming_a_path_outside_the_sessions_folder_reads_nothing(wired):
    """The log is found by the session id, and only a launch copy is removed."""
    victim = wired.tmp / "keep.json"
    victim.write_text("{}")
    kiro_cli_handoff.record_open(
        KEY,
        {"history_key": KEY, "sid": "../../x", "log_offset": 0, "launch_spec": str(victim)},
    )
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert victim.exists()
    assert [r for r, _t, _m in slot.appended if r == "user"] == []


@pytest.mark.asyncio
async def test_a_send_brings_a_restarted_opens_turns_in_before_its_own_message(
    tmp_path, monkeypatch
):
    """The send recovers before it records the new message, so the transcript
    reads in order: kiro-cli's turns, then what the person sent next."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.state import _ChatSlot

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-restarted")
    state._slots[slot.key] = slot
    key = chat_utils.effective_session_key(slot)
    seen = []

    async def recover(st, sl, session_key):
        seen.append((sl is slot, session_key, len(sl.messages)))
        # A second send's turn admitted while this one awaited: the busy read
        # after the recovery must see it, so this send queues behind it.
        sl._turn_admission_reserved = True
        return True

    monkeypatch.setattr(kcl, "recover_left_open", recover)

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "next"})
        assert resp.status == 200
        body = await resp.json()
    assert seen == [(True, key, 0)]
    assert body.get("queued") is True, "the busy read ran after the recovery"
    assert [m for m in slot.messages if m["role"] == "user"] == []


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
@pytest.mark.parametrize("link", ["inbound-mirror", "slack-thread", "unreadable"])
async def test_a_chat_a_channel_reaches_cannot_open(wired, link):
    """A channel that resumed the chat (or a Slack thread following it) runs
    its turns on this session without the dashboard admission, so no hold."""
    state = _state(_slot(wired.project))
    if link == "inbound-mirror":
        state.sessions.mirror_accepts_inbound.return_value = True
    elif link == "slack-thread":
        state.sessions.get_slack_link.return_value = ("1712793600.123456", "C0123")
    else:
        state.sessions.mirror_accepts_inbound.side_effect = OSError("map unreadable")
    plan = await kcl.plan_kiro_cli_launch(_request(state), "main")
    assert plan.code == kcl.CODE_UNSUPPORTED


@pytest.mark.asyncio
async def test_a_send_dispatched_while_the_plan_is_rebuilt_refuses_the_open(
    wired, switch, monkeypatch
):
    """The second plan awaits file reads; a send that dispatches then sets the
    chat running, and the open refuses instead of taking the chat from it."""
    busy = {"now": False}
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *a, **k: busy["now"])
    real_plan = kcl.plan_kiro_cli_launch
    plans = []

    async def plan(request, slot_name):
        plans.append(slot_name)
        result = await real_plan(request, slot_name)
        if len(plans) == 2:
            busy["now"] = True  # a send dispatched during the rebuild's awaits
        return result

    monkeypatch.setattr(kcl, "plan_kiro_cli_launch", plan)
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", "t"
    )
    assert (launch, code) == (None, kcl.CODE_BUSY)
    assert len(plans) == 2
    assert not kiro_cli_handoff.is_held(KEY)
    assert switch.reset == []


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
        kiro_cli_handoff.release(KEY, "t")
        monkeypatch.setattr(terminal, "_enabled_cache", [False, time.monotonic()])
        body = await (await client.get("/api/terminal/kiro-cli/main")).json()
        assert body["code"] == kcl.CODE_TERMINAL_DISABLED


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["running", "queued"])
async def test_the_availability_route_answers_busy_while_the_chat_streams(wired, busy):
    """The menu disables the row mid-turn and the open refuses then, so the
    route says busy too rather than available."""
    slot = _slot(wired.project)
    state = _state(slot)
    async with TestClient(TestServer(_route_app(state))) as client:
        body = await (await client.get("/api/terminal/kiro-cli/main")).json()
        assert body["available"] is True
        if busy == "running":
            slot.running = True
        else:
            slot.queue_depth = 1
        body = await (await client.get("/api/terminal/kiro-cli/main")).json()
        assert body == {
            "available": False,
            "code": kcl.CODE_BUSY,
            "reason": kcl._why(kcl.CODE_BUSY),
        }


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
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.get_provider = MagicMock(return_value=None)
    state.push_slots_update = MagicMock()
    async with TestClient(TestServer(app)) as client:
        # The client's own cwd and any argv-looking param are ignored.
        ws = await client.ws_connect(
            "/api/ws/terminal/kc1?kiro_cli_slot=main&kiro_cli_open=1&cwd=/&argv=sh", headers=ORIGIN
        )
        try:
            out = await _read_until(ws, f"CWD:{os.path.realpath(wired.project)}".encode())
            assert (
                f"ARGV:[chat][--resume-id][{SID}][--agent][{kcl._LAUNCH_SPEC_PREFIX}".encode()
                in out
            )
            assert b"[--agent][myagent]" not in out, "never the spec with its pre-approvals"
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
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
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
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
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
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
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


def test_an_imported_reply_is_redacted_on_the_live_wire():
    """The imported rows reach clients through the chat's own broadcast, which
    redacts every non-user row; the durable write redacts at its boundary."""
    from kiro_crew.dashboard.state_parts.transcript_rows import chat_message_note

    key_like = "AKIA" + "IOSFODNN7EXAMPLE"
    row = {"role": "assistant", "content": f"the key is {key_like}", "meta": {"origin": "kiro-cli"}}
    assert key_like not in json.dumps(chat_message_note("main", row, workspace=None))


@pytest.mark.asyncio
async def test_a_channel_linked_after_the_handover_is_refused_where_sessions_are_allocated(
    wired, switch
):
    """The open checked the chat's channel links; a Slack resume or another
    channel linked AFTER it reaches the session through
    ``SessionManager.get_or_create``, never the dashboard admission. That one
    door refuses while kiro-cli holds the session, with wording a channel
    shows as is, and allocates again once the terminal lets go."""
    from kiro_crew.agent_sdk.drivers.acp_vocab import is_user_worded_failure
    from kiro_crew.session_parts import claims

    state = _state(_slot(wired.project))
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert code == "" and launch is not None
    # A channel resumes the chat now: the plan's link check has already run.
    state.sessions.mirror_accepts_inbound.return_value = True
    allocated = []

    class _Boundary:
        async def get_or_create(self, key, **kwargs):
            allocated.append(key)
            return object(), True, False

    manager = SimpleNamespace(_allocation_boundary=lambda: _Boundary())
    with pytest.raises(Exception) as refused:
        await claims.get_or_create(manager, KEY)
    assert is_user_worded_failure(refused.value)
    assert str(refused.value) == kiro_cli_handoff.HELD_TURN_REFUSAL
    assert allocated == [], "nothing was allocated for the channel"
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    await claims.get_or_create(manager, KEY)
    assert allocated == [KEY]


@pytest.mark.asyncio
async def test_an_allocation_past_the_front_door_is_refused_under_the_allocators_lock():
    """A channel message that passed ``SessionManager.get_or_create``'s door
    before the hold still meets it at the allocator's locked admission, beside
    the reservation it would take, so it starts no provider."""
    from session_pool_helpers import _make_manager

    from kiro_crew.agent_sdk.drivers.acp_vocab import is_user_worded_failure

    mgr, factory = _make_manager()
    assert kiro_cli_handoff.hold(KEY, "t")
    with pytest.raises(Exception) as refused:
        await mgr._allocation_boundary().get_or_create(KEY)
    assert is_user_worded_failure(refused.value)
    factory.assert_not_called()
    assert not mgr._has_allocation_reservation(KEY), "the refusal took no reservation"


@pytest.mark.asyncio
async def test_an_allocation_already_in_flight_refuses_the_open(wired, switch):
    """A claim or cold start admitted before the open would register a second
    writer after the hold, so the open refuses while one holds a reservation."""
    state = _state(_slot(wired.project))
    state.sessions._has_allocation_reservation.return_value = True
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert (launch, code) == (None, kcl.CODE_BUSY)
    assert not kiro_cli_handoff.is_held(KEY) and switch.reset == []


@pytest.mark.asyncio
async def test_an_incognito_or_temporary_chat_cannot_open(wired):
    """Shutting such a chat's process down deletes its session files, and the
    handover's reset does that: nothing would be left for kiro-cli to resume."""
    plan = await kcl.plan_kiro_cli_launch(
        _request(_state(_slot(wired.project, is_restricted=True))), "main"
    )
    assert plan.code == kcl.CODE_UNSUPPORTED


def test_a_reply_past_the_character_cap_keeps_its_result_and_says_it_was_cut(tmp_path, monkeypatch):
    """A reply that grows past the cap before its final segment keeps the END
    (the result the conversation continues from), marked as cut."""
    monkeypatch.setattr(kcl, "_TURN_MAX_CHARS", 200)
    log = tmp_path / "s.jsonl"
    log.write_text(
        "\n".join(
            [
                _log_line("Prompt", "go"),
                _log_line("AssistantMessage", "x" * 500),
                _log_line("AssistantMessage", "y" * 500),
                _log_line("AssistantMessage", "THE RESULT"),
            ]
        )
        + "\n"
    )
    rows, left_out = kcl._read_terminal_turns(str(log), 0)
    assert left_out == 0 and rows[0] == ("user", "go")
    role, text = rows[1]
    assert role == "assistant" and len(text) <= 200
    assert text.startswith(kcl._CLIPPED_PREFIX) and text.endswith("THE RESULT")
    assert text.count(kcl._CLIPPED_PREFIX) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("evictable", [False, True])
async def test_at_the_terminal_cap_the_open_hands_over_only_when_a_slot_can_be_freed(
    monkeypatch, evictable
):
    """At the cap with no detached shell to give way, the open is refused before
    the chat is handed over. With one that can give way, the probe puts it back
    untouched and the open goes on to the handover."""
    from terminal_handler_helpers import _make_request, _make_session

    from kiro_crew.dashboard.handlers import terminal

    disconnect = -1e9 if evictable else None
    registry = {
        "old": _make_session(session_id="old", alive=True, disconnect=disconnect),
        "busy": _make_session(session_id="busy"),
    }
    before = dict(registry)
    req = _make_request(registry=registry, session_id="new")
    req.query = {"kiro_cli_slot": "main", "kiro_cli_open": "1"}
    seen = {}

    async def prepare(_request, slot_name, terminal_id):
        seen["registry"] = dict(registry)
        return None, kcl.CODE_BUSY  # stop here: what matters is that it was reached

    monkeypatch.setattr(kcl, "prepare_kiro_cli_launch", prepare)
    monkeypatch.setattr(terminal, "_get_config", lambda _r: {"enabled": True, "max_sessions": 2})
    sel = MagicMock()
    monkeypatch.setattr(terminal, "_sel", lambda: sel)
    monkeypatch.setattr(terminal.web, "WebSocketResponse", lambda **kw: AsyncMock())
    monkeypatch.setattr(terminal, "_close_terminal_ws_bounded", AsyncMock())
    resp = await terminal.api_terminal_ws(req)
    if evictable:
        assert seen["registry"] == before, "the probed shell went back untouched"
    else:
        assert isinstance(resp, web.Response) and resp.status == 429
        assert "registry" not in seen, "nothing was handed over"
    assert registry == before


def test_a_finished_long_reply_at_the_end_of_the_log_is_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(kcl, "_TURN_LINE_MAX_BYTES", 8 * 1024)
    log = tmp_path / "s.jsonl"
    log.write_text(
        _log_line("Prompt", "go") + "\n" + _log_line("AssistantMessage", "y" * 50_000) + "\n"
    )
    assert kcl._read_terminal_turns(str(log), 0) == ([("user", "go")], 1)


def test_regenerate_and_edit_resend_refuse_a_chat_open_in_kiro_cli(monkeypatch):
    from kiro_crew.dashboard import chat_regenerate

    monkeypatch.setattr(chat_regenerate, "effective_session_key", lambda slot: KEY)
    slot = SimpleNamespace(turn_running=False, running=False, is_closing=False)
    assert chat_regenerate._destructive_history_busy(slot) is None
    assert kiro_cli_handoff.hold(KEY, "t")
    refused = chat_regenerate._destructive_history_busy(slot)
    assert refused.status == 409
    assert json.loads(refused.text)["code"] == "kiro_cli_open"


@pytest.mark.asyncio
async def test_discarding_a_held_conversation_is_refused_at_the_session_manager():
    """Every caller that discards a native conversation (rewind, regenerate, a
    channel's clear, the context-cap reset) goes through one method; it changes
    nothing while kiro-cli holds the session, and works once it lets go."""
    from session_pool_helpers import _make_manager

    mgr, _factory = _make_manager()
    assert kiro_cli_handoff.hold(KEY, "t")
    assert await mgr.discard_conversation(KEY) is False
    assert await mgr.discard_conversation(KEY, skip_if_busy=True) is False
    kiro_cli_handoff.release(KEY, "t")
    assert await mgr.discard_conversation(KEY) is not None


def test_a_prompt_too_long_to_read_back_still_separates_the_replies_around_it(
    tmp_path, monkeypatch
):
    """A prompt past the byte cap (a pasted image is stored as an integer
    array) is left out and counted; the reply after it starts its own row
    instead of being glued onto the reply before it. An over-cap tool result
    between two segments of one reply still lets them join."""
    monkeypatch.setattr(kcl, "_TURN_LINE_MAX_BYTES", 8 * 1024)
    log = tmp_path / "s.jsonl"
    tool = json.dumps(
        {"version": "v1", "kind": "ToolResults", "data": {"content": ["x" * 20_000]}},
        separators=(",", ":"),
    )
    log.write_text(
        "\n".join(
            [
                _log_line("Prompt", "first"),
                _log_line("AssistantMessage", "reply A"),
                _log_line("Prompt", "z" * 20_000),
                _log_line("AssistantMessage", "reply B part 1"),
                tool,
                _log_line("AssistantMessage", "part 2"),
            ]
        )
        + "\n"
    )
    rows, left_out = kcl._read_terminal_turns(str(log), 0)
    assert rows == [
        ("user", "first"),
        ("assistant", "reply A"),
        ("assistant", "reply B part 1\n\npart 2"),
    ]
    assert left_out == 1


@pytest.mark.asyncio
async def test_a_failed_transcript_write_frees_the_chat_and_keeps_the_record_to_retry(
    wired, switch, caplog
):
    """The record goes only once the turns are on disk. A write that fails
    frees the chat, keeps the record, and the chat's next turn retries it
    without adding any row twice."""
    slot = _slot(wired.project)
    state = _state(slot)
    log = wired.sessions / f"{SID}.jsonl"
    log.write_text("")
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    launch.announce()
    log.write_text(
        _log_line("Prompt", "asked") + "\n" + _log_line("AssistantMessage", "answered") + "\n"
    )
    wired.calls.saves_land = False
    with caplog.at_level("WARNING", logger=kcl.logger.name):
        assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert "could not add the terminal's turns" in caplog.text
    assert not kiro_cli_handoff.is_held(KEY)
    assert len(list(wired.records.iterdir())) == 1, "kept to retry"
    with pytest.raises(Exception):
        kiro_cli_handoff.refuse_if_held(KEY)
    wired.calls.saves_land = True
    assert await kcl.recover_left_open(state, slot, KEY)
    texts = [text for _r, text, _m in slot.appended]
    assert texts.count("asked") == 1 and texts.count("answered") == 1, "nothing added twice"
    assert list(wired.records.iterdir()) == []
    kiro_cli_handoff.refuse_if_held(KEY)


@pytest.mark.asyncio
async def test_a_log_that_cannot_be_read_at_release_keeps_the_record(wired, switch, monkeypatch):
    """An unreadable log is not "no turns": the record stays to retry from."""
    slot = _slot(wired.project)
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    launch.announce()

    def unreadable(path, offset):
        raise PermissionError(errno.EACCES, "Permission denied", path)

    monkeypatch.setattr(kcl, "_read_terminal_turns", unreadable)
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert not kiro_cli_handoff.is_held(KEY)
    assert len(list(wired.records.iterdir())) == 1
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert len(list(wired.records.iterdir())) == 1, "still unreadable: still kept"


@pytest.mark.asyncio
async def test_a_new_open_is_refused_while_an_earlier_opens_turns_are_unsaved(wired, switch):
    """Its record is the only copy of where those turns start: a new open would
    replace it, so it waits until they are saved."""
    _leave_an_open(wired, "", [_log_line("Prompt", "q")])
    wired.calls.saves_land = False
    slot = _slot(wired.project)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    assert (launch, code) == (None, kcl.CODE_RECORD)
    assert not kiro_cli_handoff.is_held(KEY)
    [path] = list(wired.records.iterdir())
    assert json.loads(path.read_text())["log_offset"] == 0, "the earlier record, untouched"
    wired.calls.saves_land = True
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    assert code == "" and launch is not None
    assert [t for _r, t, _m in slot.appended].count("q") == 1
    launch.release()


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_read_is_not_replaced_by_a_new_open(
    wired, switch, monkeypatch
):
    """A record that is there but unreadable is not "no record": a new open
    would replace the only pointer to the turns behind it, so it is refused,
    and the session's allocation stays refused until the record reads."""
    _leave_an_open(wired, "", [_log_line("Prompt", "q")])
    [path] = list(wired.records.iterdir())
    before = path.read_text()
    real_open = open

    def unreadable(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError(errno.EACCES, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", unreadable)
    slot = _slot(wired.project)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    assert (launch, code) == (None, kcl.CODE_RECORD)
    with pytest.raises(Exception) as refused:
        kiro_cli_handoff.refuse_if_held(KEY)
    assert str(refused.value) == kiro_cli_handoff.LEFT_OPEN_REFUSAL
    monkeypatch.setattr("builtins.open", real_open)
    assert path.read_text() == before, "the earlier record, untouched"


@pytest.mark.asyncio
async def test_a_live_import_is_on_disk_before_the_record_goes(wired, switch):
    slot = _slot(wired.project)
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    launch.announce()
    (wired.sessions / f"{SID}.jsonl").write_text(_log_line("Prompt", "asked") + "\n")
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    [(saved_slot, history_key, rows_then)] = wired.calls.saves
    assert saved_slot is slot and history_key == KEY
    assert rows_then >= 2, "the save ran after the rows were added"
    assert list(wired.records.iterdir()) == []


@pytest.mark.asyncio
async def test_overlapping_recoveries_import_once(wired):
    """A second entrant waits for the first and then finds nothing to do, so
    no message can go ahead of the turns it is bringing in."""
    _leave_an_open(wired, "", [_log_line("Prompt", "q"), _log_line("AssistantMessage", "a")])
    slot = _slot(wired.project)
    state = _state(slot)
    first, second = await asyncio.gather(
        kcl.recover_left_open(state, slot, KEY), kcl.recover_left_open(state, slot, KEY)
    )
    assert sorted([first, second]) == [False, True]
    assert [t for _r, t, _m in slot.appended].count("q") == 1
    assert len(wired.calls.saves) == 1


def test_an_image_only_prompt_still_separates_the_replies_around_it(tmp_path):
    log = tmp_path / "s.jsonl"
    log.write_text(
        "\n".join(
            [
                _log_line("Prompt", "first"),
                _log_line("AssistantMessage", "reply A"),
                _log_line("Prompt", None, extra=[{"kind": "image", "data": {"format": "png"}}]),
                _log_line("AssistantMessage", "reply about the image"),
            ]
        )
        + "\n"
    )
    assert kcl._read_terminal_turns(str(log), 0) == (
        [("user", "first"), ("assistant", "reply A"), ("assistant", "reply about the image")],
        0,
    )


@pytest.mark.asyncio
async def test_a_discard_waiting_on_the_manager_lock_refuses_a_hold_taken_meanwhile():
    """The discard reads the hold again under the session manager's lock, so a
    handover that took the hold while the discard waited still wins."""
    from session_pool_helpers import _make_manager

    mgr, _factory = _make_manager()
    async with mgr._lock:
        pending = asyncio.ensure_future(mgr.discard_conversation(KEY))
        await asyncio.sleep(0)  # the discard passed its first read and waits on the lock
        assert kiro_cli_handoff.hold(KEY, "t")
    assert await pending is False


@pytest.mark.asyncio
async def test_a_discard_that_committed_before_the_hold_refuses_the_open(
    wired, switch, monkeypatch
):
    """A reset or rewind admitted before the hold may clear the resume pointer
    while the handover's reset waits; the handover then refuses instead of
    launching an id the chat does not resume."""
    smap = _FakeMap()
    state = _state(_slot(wired.project), smap=smap)

    async def reset(state_, slot, key, *, skip_if_busy=False):
        switch.reset.append((key, skip_if_busy))
        smap.sid = ""  # the discard's clear landed meanwhile
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", reset)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert (launch, code) == (None, kcl.CODE_BUSY)
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_the_terminal_gets_the_chats_agent_without_its_tool_preapprovals(wired, switch):
    """The terminal runs outside Crew's sandbox, so the chat's agent goes in as a
    per-open copy that keeps its prompt, MCP servers, model and restrictions but
    drops every tool pre-approval: kiro-cli asks the owner before each tool.
    The copy is owner-only and removed when the hold is released."""
    spec = {
        "name": "myagent",
        "prompt": "file://prompts/agent.md",
        "model": "some-model",
        "tools": ["fs_read", "web_fetch", "@srv"],
        "allowedTools": ["fs_read", "web_fetch", "@srv"],
        "toolsSettings": {
            "fs_read": {"allowedPaths": ["~"], "deniedPaths": ["~/.ssh"]},
            "execute_bash": {"allowedCommands": ["ls"], "autoAllowReadonly": True},
            "subagent": {"trustedAgents": ["helper"], "availableAgents": ["helper"]},
        },
        "includeMcpJson": True,
        "permissions": {
            "rules": [
                {"capability": "fs_read", "effect": "allow"},
                {"capability": "shell", "effect": "deny"},
            ]
        },
        "mcpServers": {"srv": {"command": "srv-bin", "autoApprove": ["read"]}},
        "hooks": {"postToolUse": [{"command": "hook"}]},
    }
    (wired.agents / "myagent.json").write_text(json.dumps(spec))
    slot = _slot(wired.project)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    assert code == ""
    copy_path = pathlib.Path(launch.launch_spec)
    assert copy_path.parent == wired.agents
    assert launch.argv[-2:] == ["--agent", copy_path.stem]
    if os.name == "posix":  # Windows files carry no group/other mode bits
        assert copy_path.stat().st_mode & 0o077 == 0, "owner-only"
    view = json.loads(copy_path.read_text())
    assert view["name"] == copy_path.stem
    assert view["allowedTools"] == []
    assert view["toolsSettings"] == {
        "fs_read": {"deniedPaths": ["~/.ssh"]},
        "execute_bash": {},
        "subagent": {"availableAgents": ["helper"]},
    }, "every allowed*/trusted*/auto* grant goes, restrictions stay"
    assert view["includeMcpJson"] is False, "the global mcp.json carries its own autoApprove"
    if "permissions" in view:  # kept only where the kiro-cli version accepts the block
        assert {"capability": "shell", "effect": "deny"} in view["permissions"]["rules"]
        assert all(r.get("effect") != "allow" for r in view["permissions"]["rules"])
    assert view["mcpServers"] == {"srv": {"command": "srv-bin"}}
    assert view["tools"] == spec["tools"] and view["model"] == "some-model"
    assert view["hooks"] == spec["hooks"]
    assert view["prompt"] == "file://" + (wired.agents / "prompts" / "agent.md").as_posix()
    assert json.loads((wired.agents / "myagent.json").read_text()) == spec, "original untouched"
    launch.announce()
    assert "kiro-cli asks you there before each tool" in slot.appended[-1][1]
    kiro_cli_handoff.release(KEY, "t")
    assert not copy_path.exists(), "removed with the hold"


def test_the_launch_copys_permissions_keep_their_own_deny_and_ask_rules(monkeypatch):
    """Where kiro-cli accepts a ``permissions`` block, the copy keeps the agent's
    own deny and ask rules and loses its allows, through the same grant-emptying
    the side reply's read-only spec uses."""
    from kiro_crew.agent_materialization import auto_approve
    from kiro_crew.dashboard.side_readonly_spec import empty_backend_grants

    monkeypatch.setattr(auto_approve, "_spec_permissions_accepted", lambda: True)
    spec = {
        "allowedTools": ["fs_read"],
        "permissions": {
            "rules": [
                {"capability": "fs_read", "effect": "allow"},
                {"capability": "shell", "effect": "deny"},
                {"capability": "fs_write", "effect": "ask"},
            ]
        },
    }
    kept = json.loads(json.dumps(spec))
    empty_backend_grants(kept, agent_filename="copy.json", keep_narrowing=True)
    rules = kept["permissions"]["rules"]
    assert {"capability": "shell", "effect": "deny"} in rules
    assert {"capability": "fs_write", "effect": "ask"} in rules
    assert all(r["effect"] != "allow" for r in rules)
    emptied = json.loads(json.dumps(spec))
    empty_backend_grants(emptied, agent_filename="ro.json")
    assert emptied["permissions"] == {"rules": []}, "the read-only spec empties it whole"


@pytest.mark.asyncio
async def test_the_launch_copy_is_built_from_the_spec_that_declares_the_agent(wired, switch):
    """A spec's declared name beats its file name, as everywhere Crew resolves an
    agent: ``myagent.json`` here declares another agent, so the copy must come
    from ``pkg-myagent.json``, which declares ``myagent``."""
    (wired.agents / "myagent.json").write_text(
        json.dumps({"name": "someone-else", "prompt": "wrong", "allowedTools": ["*"]})
    )
    (wired.agents / "pkg-myagent.json").write_text(
        json.dumps({"name": "myagent", "prompt": "right", "allowedTools": ["fs_read"]})
    )
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", "t"
    )
    assert code == ""
    view = json.loads(pathlib.Path(launch.launch_spec).read_text())
    assert view["prompt"] == "right"
    assert view["allowedTools"] == []
    launch.release()


@pytest.mark.asyncio
async def test_another_gateways_launch_copy_is_left_alone_and_a_failed_write_refuses(
    wired, switch, monkeypatch
):
    """A gateway with its own data home may share ~/.kiro/agents and hold a
    live copy there; only this gateway's own records name copies it removes."""
    theirs = wired.agents / f"{kcl._LAUNCH_SPEC_PREFIX}deadbeefdeadbeef.json"
    theirs.write_text("{}")
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", "t"
    )
    assert code == "" and theirs.exists()
    launch.release()
    assert theirs.exists()
    theirs.unlink()
    (wired.agents / "myagent.json").write_text("not json")
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", "t2"
    )
    assert (launch, code) == (None, kcl.CODE_LAUNCH_SPEC)
    assert not kiro_cli_handoff.is_held(KEY)
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"]
    assert list(wired.records.iterdir()) == [], "the record goes with the refusal"
    assert json.loads((wired.sessions / f"{SID}.json").read_text()) == _ENVELOPE


@pytest.mark.asyncio
async def test_a_launch_copy_that_cannot_be_written_refuses_with_its_own_reason(
    wired, switch, monkeypatch
):
    """A read-only ~/.kiro/agents refuses the open with a reason that says so,
    not the code for channel and app chats, and leaves the chat free."""
    real_open = os.open

    def read_only(path, *args, **kwargs):
        if os.path.basename(str(path)).startswith(kcl._LAUNCH_SPEC_PREFIX):
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(kcl.os, "open", read_only)
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", "t"
    )
    assert (launch, code) == (None, kcl.CODE_LAUNCH_SPEC)
    assert code in kcl.KIRO_CLI_REFUSAL_CODES
    assert "~/.kiro/agents" in kcl.refusal_message(code)
    assert not kiro_cli_handoff.is_held(KEY)
