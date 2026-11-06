"""Opening a Kiro Agent (KAS) chat in kiro-cli: kiro-cli's v3 session store.

What these pin was checked live against kiro-cli 2.21.1 on a pty, on a
throwaway session under a scratch ``HOME`` (never a live chat's):

* ``kiro-cli chat --agent-engine v3 --resume-id sess_<uuid>`` resumes a session
  the Kiro Agent harness made, from any folder, on the agent ``session.json``'s
  ``agentMode`` names, looked up among the agent files kiro-cli loads. ``--agent``
  is ignored on a resume, an agent it cannot find runs its default agent
  (``vibe``), and a v2-shaped spec "needs upgrading" and runs the default too.
* With ``agentMode`` naming a v3 agent file whose permissions allow the shell, a
  shell command ran without asking; with that allow rule left out, it asked.
* A workspace ``.kiro/settings/mcp.json`` server started even for an agent with
  ``includeMcpJson`` false.
* What the terminal adds lands in ``messages.jsonl``: ``user`` records for the
  prompts, ``assistant`` records with ``operationType`` ``Say`` for the replies.

Split from ``test_kiro_cli_launch.py`` (the file-line cap); the fixtures are
that module's.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid

import pytest
from test_kiro_cli_launch import (  # noqa: F401 -- the fixtures are bound for pytest
    KEY,
    _config_with_previews,
    _FakeMap,
    _isolate,
    _request,
    _slot,
    _state,
    switch_fixture,
    wired_fixture,
)

from kiro_crew import kiro_cli_handoff
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.config import live as config_live
from kiro_crew.dashboard.handlers import kiro_cli_launch as kcl
from kiro_crew.dashboard.handlers import kiro_cli_launch_v3 as v3

KAS_SID = "sess_21c8e44b-682a-4967-9f65-fa32a54cf68e"
#: The real checks, kept before ``wired`` answers them for the v2 tests.
_REAL_FILES_RESUMABLE = kcl._files_resumable
_REAL_REFUSAL = v3.workspace_config_refusal
#: Nested past any JSON parser's recursion limit.
_TOO_DEEP = "[" * 100_000 + "]" * 100_000


def _record(kind: str, text: str, **extra) -> str:
    """One ``messages.jsonl`` line of the shape kiro-cli v3 writes."""
    return json.dumps(
        {
            "id": f"{uuid.uuid4()}-say",
            "timestamp": "2026-11-04T23:41:45.868Z",
            "payload": {"type": kind, "content": text, **extra},
        },
        separators=(",", ":"),
    )


def _session_doc(agent: str = "myagent") -> dict:
    return {
        "schemaVersion": "1.0.0",
        "id": KAS_SID,
        "title": "t",
        "agentMode": agent,
        "workspacePaths": ["/w"],
        "autopilot": True,
        "_meta": {"kiro": {"customAgents": [{"id": agent, "prompt": "p", "tools": ["*"]}]}},
    }


@pytest.fixture(name="kas")
def kas_fixture(wired, monkeypatch):
    """``wired`` (a Kiro Agent chat whose conversation is a real v3 session
    folder on disk), with the real session-file check and an earlier turn."""
    (wired.v3 / "messages.jsonl").write_text(_record("user", "earlier") + "\n")
    monkeypatch.setattr(kcl, "_files_resumable", _REAL_FILES_RESUMABLE)
    return wired


def _kas_state(slot, provider="kas", sid=KAS_SID):
    return _state(slot, smap=_FakeMap(sid=sid, provider=provider))


def _doc(kas) -> dict:
    return json.loads((kas.v3 / "session.json").read_text())


# ── the gate: Kiro Agent only; Kiro CLI, Claude Code and Codex refused ───────


@pytest.mark.asyncio
async def test_a_kiro_agent_chat_opens_on_kiro_clis_v3_engine(kas):
    plan = await kcl.plan_kiro_cli_launch(_request(_kas_state(_slot(kas.project))), "main")
    assert plan.ok, plan.code
    assert plan.engine == kcl.ENGINE_V3
    assert plan.argv() == [
        str(kas.binary),
        "chat",
        "--agent-engine",
        "v3",
        "--resume-id",
        KAS_SID,
    ], "v3 resumes on the agent session.json names: no --agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
async def test_claude_code_and_codex_chats_are_refused_by_the_server(kas, monkeypatch, backend):
    """Refused where every handover passes, whatever the menu shows: a chat
    whose conversation Claude Code or Codex wrote, and a Kiro Agent chat now
    set to start on one of them."""
    state = _state(_slot(kas.project), smap=_FakeMap(sid=KAS_SID, provider=backend))
    monkeypatch.setattr(kcl, "_configured_backend", lambda key: backend)
    assert (await kcl.plan_kiro_cli_launch(_request(state), "main")).code == kcl.CODE_NOT_KIRO
    state = _kas_state(_slot(kas.project))
    assert (await kcl.plan_kiro_cli_launch(_request(state), "main")).code == kcl.CODE_NOT_KIRO
    assert kcl.CODE_NOT_KIRO in kcl.KIRO_CLI_REFUSAL_CODES


@pytest.mark.asyncio
async def test_a_kiro_cli_harness_chat_is_refused_by_the_server(kas, monkeypatch):
    """Kiro Agent only: a chat on the Kiro CLI harness (its conversation in
    kiro-cli's v2 store) is refused where every handover passes, with the code
    the menu leaves the row out for, before any of its session files is read."""
    seen = []
    monkeypatch.setattr(kcl, "_files_resumable", lambda sid: seen.append(sid) or True)
    monkeypatch.setattr(kcl, "_configured_backend", lambda key: ACP_BACKEND_KIRO)
    state = _state(
        _slot(kas.project),
        smap=_FakeMap(sid="0b6f1c3e-9a4d-4e2b-8f11-2c3d4e5f6a7b", provider="acp"),
    )
    plan = await kcl.plan_kiro_cli_launch(_request(state), "main")
    assert plan.code == kcl.CODE_NOT_KIRO
    assert seen == [], "refused before the session's files are looked at"
    assert "Kiro Agent" in kcl.refusal_message(kcl.CODE_NOT_KIRO)


@pytest.mark.asyncio
async def test_a_chat_moved_between_the_two_kiro_harnesses_is_refused(kas, monkeypatch):
    """The harness that wrote the conversation and the one the chat starts on
    must both be Kiro Agent: a Kiro CLI conversation under a chat now on Kiro
    Agent (or the reverse) is not one this opens."""
    monkeypatch.setattr(kcl, "_configured_backend", lambda key: ACP_BACKEND_KAS)
    state = _state(_slot(kas.project), smap=_FakeMap(sid=KAS_SID, provider="acp"))
    assert (await kcl.plan_kiro_cli_launch(_request(state), "main")).code == kcl.CODE_NOT_KIRO
    monkeypatch.setattr(kcl, "_configured_backend", lambda key: ACP_BACKEND_KIRO)
    state = _kas_state(_slot(kas.project))
    assert (await kcl.plan_kiro_cli_launch(_request(state), "main")).code == kcl.CODE_NOT_KIRO


@pytest.mark.asyncio
async def test_the_preview_switch_still_wins_on_a_kiro_agent_chat(kas, monkeypatch):
    monkeypatch.setattr(
        config_live, "snapshot", lambda: _config_with_previews({kcl.PREVIEW_NAME: False})
    )
    plan = await kcl.plan_kiro_cli_launch(_request(_kas_state(_slot(kas.project))), "main")
    assert plan.code == kcl.CODE_PREVIEW_OFF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sid", ["0b6f1c3e-9a4d-4e2b-8f11-2c3d4e5f6a7b", "sess_../../etc", "sess_x", "--agent=evil"]
)
async def test_a_kas_session_id_not_in_v3s_shape_is_never_used(kas, sid):
    state = _kas_state(_slot(kas.project), sid=sid)
    assert (await kcl.plan_kiro_cli_launch(_request(state), "main")).code == kcl.CODE_NO_SESSION


# ── the session folder a sandboxed agent can write ───────────────────────────


def test_a_session_folder_is_found_by_its_id_alone(kas):
    assert v3.files_resumable(KAS_SID)
    assert not v3.files_resumable("sess_00000000-0000-0000-0000-000000000000")


def test_two_folders_of_the_same_id_are_refused_not_guessed_between(kas):
    twin = kas.v3_root / "e351f88c234ca1f7" / KAS_SID
    twin.mkdir(parents=True)
    (twin / "session.json").write_text(json.dumps(_session_doc("evil")))
    (twin / "messages.jsonl").write_text("")
    assert not v3.files_resumable(KAS_SID)
    with pytest.raises(kcl._SessionFileUnusable):
        v3.read_session_agent(KAS_SID)


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX symlinks; the Windows pin refuses a reparse point"
)
def test_a_session_folder_or_file_that_is_a_link_is_refused(kas, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    kas.v3.rename(elsewhere)
    kas.v3.symlink_to(elsewhere, target_is_directory=True)
    assert not v3.files_resumable(KAS_SID)
    with pytest.raises(kcl._SessionFileUnusable):
        v3.point_session_agent(KAS_SID, "x")
    assert json.loads((elsewhere / "session.json").read_text())["agentMode"] == "myagent"
    kas.v3.unlink()
    elsewhere.rename(kas.v3)
    (kas.v3 / "messages.jsonl").unlink()
    (kas.v3 / "messages.jsonl").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text(_record("user", "a secret") + "\n")
    assert not v3.files_resumable(KAS_SID)
    with pytest.raises(kcl._SessionFileUnusable):
        v3.read_turns(KAS_SID, 0)


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
def test_a_session_log_hard_linked_to_another_file_is_never_read(kas, tmp_path):
    """A hard link is no link to ``O_NOFOLLOW``: it would bring any file the
    owner can read into the chat, as kiro-cli's turns."""
    secret = tmp_path / "secret.jsonl"
    secret.write_text(_record("user", "the owner's secret") + "\n")
    (kas.v3 / "messages.jsonl").unlink()
    os.link(secret, kas.v3 / "messages.jsonl")
    with pytest.raises(kcl._SessionFileUnusable):
        v3.read_turns(KAS_SID, 0)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_for_the_log_is_refused_without_waiting(kas):
    (kas.v3 / "messages.jsonl").unlink()
    os.mkfifo(kas.v3 / "messages.jsonl")
    assert not v3.files_resumable(KAS_SID)
    with pytest.raises(kcl._SessionFileUnusable):
        v3.log_size(KAS_SID)


def test_a_session_json_naming_another_session_is_refused(kas):
    doc = _session_doc()
    doc["id"] = "sess_00000000-0000-0000-0000-000000000000"
    (kas.v3 / "session.json").write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        v3.read_session_agent(KAS_SID)


@pytest.mark.parametrize(
    "where",
    [
        ".kiro/agents/x.json",
        ".kiro/hooks/on-save.json",
        ".kiro/powers/p/POWER.md",
        ".kiro/extensions/e.json",
        ".kiro/settings/mcp.json",
        ".amazonq/mcp.json",
    ],
)
def test_v3_project_config_the_terminal_would_run_refuses_it(tmp_path, where):
    """kiro-cli v3 runs what the project's ``.kiro`` holds whatever the agent
    says, and the chat's sandboxed agent can write that folder."""
    project = tmp_path / "p"
    (project / ".kiro" / "steering").mkdir(parents=True)
    (project / ".kiro" / "steering" / "notes.md").write_text("text only")
    assert not _REAL_REFUSAL(str(project)), "steering is text, not commands"
    path = project / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert _REAL_REFUSAL(str(project))


@pytest.mark.asyncio
async def test_the_plan_refuses_a_kas_chat_whose_project_has_v3_config(kas, monkeypatch):
    monkeypatch.setattr(v3, "workspace_config_refusal", lambda cwd: True)
    plan = await kcl.plan_kiro_cli_launch(_request(_kas_state(_slot(kas.project))), "main")
    assert plan.code == kcl.CODE_PROJECT_AGENT


# ── the launch copy: the chat's agent, in v3's format, pre-approving nothing ─


def test_the_v3_launch_copy_keeps_the_agent_and_drops_every_preapproval(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("from a file")
    spec = {
        "name": "myagent",
        "prompt": "file://prompts/p.md",
        "model": "some-model",
        "tools": ["execute_bash", "fs_read", "@srv"],
        "allowedTools": ["execute_bash", "fs_read", "@srv"],
        "excludedTools": ["fs_write"],
        "toolsSettings": {"execute_bash": {"allowedCommands": ["ls"], "deniedCommands": ["rm .*"]}},
        "permissions": {
            "rules": [
                {"capability": "shell", "match": ["*"], "effect": "allow"},
                {"capability": "web_fetch", "effect": "deny"},
                {"capability": "fs_write", "effect": "ask"},
            ]
        },
        "mcpServers": {"srv": {"command": "srv-bin", "env": {"A": "1"}, "autoApprove": ["*"]}},
        "hooks": {"postToolUse": [{"command": "hook"}]},
        "resources": ["file://.kiro/steering/**/*.md"],
        "includeMcpJson": True,
    }
    view = v3.launch_view("myagent", "copy-name", spec, tmp_path)
    assert view["name"] == "copy-name"
    assert view["prompt"] == "from a file", "read in, as Crew sends it to the engine"
    assert all(r.get("effect") in ("deny", "ask") for r in view["permissions"]["rules"])
    assert {"capability": "web_fetch", "effect": "deny"} in view["permissions"]["rules"]
    assert {"capability": "fs_write", "effect": "ask"} in view["permissions"]["rules"]
    assert any(
        r.get("capability") == "shell" and r.get("effect") == "deny"
        for r in view["permissions"]["rules"]
    ), "toolsSettings' own denies are carried"
    assert view["mcpServers"] == {"srv": {"command": "srv-bin", "env": {"A": "1"}}}
    assert view["includeMcpJson"] is False and view["includePowers"] is False
    assert "read_file" in view["tools"], "v3's own names beside the spec's"
    assert "fs_write" in view["excludedTools"]
    assert view["resources"] == spec["resources"]
    # v3 runs a v2-shaped file as its default agent, so no v2 key may be left.
    assert not {"allowedTools", "toolsSettings", "hooks", "model", "useLegacyMcpJson"} & set(view)


@pytest.mark.asyncio
async def test_a_channel_link_made_after_the_plan_refuses_the_open(wired, switch, monkeypatch):
    """The plan refuses a chat a channel reaches, but a link can land during the
    open's awaits; read again under the hold, it refuses the open."""
    from kiro_crew.dashboard import chat_handlers

    state = _state(_slot(wired.project))

    async def reset(state_, slot, key, *, skip_if_busy=False):
        state.sessions.get_slack_link = lambda k: ("1712793600.123456", "C1")
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", reset)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert (launch, code) == (None, kcl.CODE_UNSUPPORTED)
    assert not kiro_cli_handoff.is_held(KEY)


def test_the_launch_copy_is_made_from_the_spec_a_pin_chooses(tmp_path, monkeypatch):
    """Two packages' specs declaring one name: the copy is made from the file
    ``agent.spec_pins`` names, the one the chat's sessions run, and with no pin
    neither counts as the agent's spec."""
    from test_kiro_cli_launch import _point_agents_dir

    from kiro_crew import agent as agent_mod
    from kiro_crew import agent_spec_pins

    agents = tmp_path / "agents"
    agents.mkdir()
    _point_agents_dir(monkeypatch, agents)
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)
    for pkg in ("PkgA", "PkgB"):
        (agents / f"{pkg}-helper.json").write_text(json.dumps({"name": "helper", "prompt": pkg}))
    pins: dict[str, str] = {}
    monkeypatch.setattr(agent_spec_pins, "spec_pins", lambda: dict(pins))
    assert kcl._agent_spec_where("helper", str(tmp_path)) == (False, False)
    pins["helper"] = "PkgB-helper.json"
    assert kcl._agent_spec_where("helper", str(tmp_path)) == (True, False)
    path, spec = kcl._read_user_agent_spec("helper")
    assert path.name == "PkgB-helper.json" and spec["prompt"] == "PkgB"


def test_an_unreadable_prompt_file_refuses_the_copy(tmp_path):
    with pytest.raises(ValueError):
        v3.launch_view("a", "n", {"prompt": "file://missing.md", "tools": []}, tmp_path)


# ── the handover, end to end on a v3 session ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_kas_chat_hands_over_and_comes_back_with_its_turns(kas, switch, monkeypatch):
    seen = []
    from kiro_crew.dashboard import chat_delivery

    real_sanitize = chat_delivery.sanitize_outbound
    monkeypatch.setattr(
        chat_delivery, "sanitize_outbound", lambda text: seen.append(text) or real_sanitize(text)
    )
    slot = _slot(kas.project)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_kas_state(slot)), "main", "t")
    assert code == "" and launch is not None
    copy_path = pathlib.Path(launch.launch_spec)
    assert launch.argv == [str(kas.binary), "chat", "--agent-engine", "v3", "--resume-id", KAS_SID]
    assert _doc(kas)["agentMode"] == copy_path.stem, "the terminal resumes on the launch copy"
    view = json.loads(copy_path.read_text())
    assert view["name"] == copy_path.stem and "allowedTools" not in view
    if os.name == "posix":
        assert copy_path.stat().st_mode & 0o077 == 0, "owner-only"
    [record] = [json.loads(p.read_text()) for p in kas.records.iterdir()]
    assert record["engine"] == "v3" and record["sid"] == KAS_SID
    assert record["session_agent"] == {"agentMode": "myagent"}
    assert record["log_offset"] == (kas.v3 / "messages.jsonl").stat().st_size
    assert kiro_cli_handoff.holder_of(KEY) == "t"
    with pytest.raises(Exception):
        kiro_cli_handoff.refuse_if_held(KEY)  # the chat's own turns stand down
    launch.announce()
    with (kas.v3 / "messages.jsonl").open("a") as fh:
        for line in (
            _record("user", "asked in kiro-cli"),
            _record("assistant", "thinking", operationType="Reasoning"),
            _record("tool_call", "ignored"),
            _record("assistant", "first part", operationType="Say"),
            _record("assistant", "second part", operationType="Say"),
        ):
            fh.write(line + "\n")
    doc = _doc(kas)
    doc["title"] = "renamed in kiro-cli"
    (kas.v3 / "session.json").write_text(json.dumps(doc))
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    imported = [(r, t) for r, t, m in slot.appended if m and m.get("origin") == "kiro-cli"]
    assert imported == [("user", "asked in kiro-cli"), ("assistant", "first part\n\nsecond part")]
    assert "asked in kiro-cli" in seen, "every imported row through the delivery sanitizer"
    after = _doc(kas)
    assert after["agentMode"] == "myagent", "the chat's own agent is back"
    assert after["title"] == "renamed in kiro-cli", "the rest is kiro-cli's"
    assert not copy_path.exists() and list(kas.records.iterdir()) == []
    kiro_cli_handoff.refuse_if_held(KEY)


@pytest.mark.asyncio
async def test_a_kas_agent_with_no_file_resumes_on_kiro_clis_default_not_what_was_named(
    kas, switch, monkeypatch
):
    """What ``agentMode`` names when the open starts is whatever the session
    folder holds, which a sandboxed agent can write: it is replaced either way."""
    monkeypatch.setattr(kcl, "_agent_spec_where", lambda agent, cwd: (False, False))
    doc = _session_doc("some-agent-with-preapprovals")
    (kas.v3 / "session.json").write_text(json.dumps(doc))
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_kas_state(_slot(kas.project))), "main", "t"
    )
    assert code == "" and launch.launch_spec == ""
    assert _doc(kas)["agentMode"] == v3.DEFAULT_AGENT
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert _doc(kas)["agentMode"] == "some-agent-with-preapprovals"


@pytest.mark.asyncio
async def test_a_kas_session_json_the_open_cannot_read_refuses_it(kas, switch):
    (kas.v3 / "session.json").write_text("not json")
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_kas_state(_slot(kas.project))), "main", "t"
    )
    assert (launch, code) == (None, kcl.CODE_SESSION_FILE)
    assert not kiro_cli_handoff.is_held(KEY)
    assert [p.name for p in kas.agents.iterdir()] == ["myagent.json"], "no launch copy left"


@pytest.mark.asyncio
async def test_after_a_restart_a_kas_chats_next_turn_brings_the_terminals_turns_in(kas, switch):
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_kas_state(_slot(kas.project))), "main", "t"
    )
    assert code == ""
    copy_path = pathlib.Path(launch.launch_spec)
    with (kas.v3 / "messages.jsonl").open("a") as fh:
        fh.write(
            _record("user", "q") + "\n" + _record("assistant", "a", operationType="Say") + "\n"
        )
    kiro_cli_handoff.reset_holds()  # the gateway stopped with the terminal open
    slot = _slot(kas.project)
    assert await kcl.recover_left_open(_kas_state(slot), slot, KEY)
    assert [(r, t) for r, t, _m in slot.appended[:2]] == [("user", "q"), ("assistant", "a")]
    assert _doc(kas)["agentMode"] == "myagent"
    assert not copy_path.exists() and list(kas.records.iterdir()) == []


# ── what an agent can write there, nested too deep or swapped mid-open ───────


@pytest.mark.asyncio
async def test_a_session_json_nested_past_the_parser_refuses_the_open_and_holds_nothing(
    kas, switch
):
    """A ``RecursionError`` is neither ``OSError`` nor ``ValueError``: escaping,
    it would skip every handler that gives the hold back."""
    (kas.v3 / "session.json").write_text(_TOO_DEEP)
    with pytest.raises(ValueError):
        v3.read_session_agent(KAS_SID)
    launch, code = await kcl.prepare_kiro_cli_launch(
        _request(_kas_state(_slot(kas.project))), "main", "t"
    )
    assert (launch, code) == (None, kcl.CODE_SESSION_FILE)
    assert not kiro_cli_handoff.is_held(KEY)


def test_a_log_record_nested_past_the_parser_is_skipped_not_raised(kas):
    deep = '{"payload":{"type":"user","content":' + _TOO_DEEP + "}}"
    with (kas.v3 / "messages.jsonl").open("a") as fh:
        fh.write(deep + "\n" + _record("user", "after it") + "\n")
    assert v3.read_turns(KAS_SID, 0) == ([("user", "earlier"), ("user", "after it")], 0)


@pytest.mark.asyncio
async def test_a_twin_folder_made_while_kiro_cli_ran_keeps_the_record_then_says_so(kas, switch):
    """The release cannot put ``agentMode`` back once a second folder of the id
    exists: the record stays rather than claiming it did, and the recovery the
    chat's next turn runs says so in the chat, removes the launch copy and
    frees the chat instead of refusing it for good."""
    slot = _slot(kas.project)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_kas_state(slot)), "main", "t")
    assert code == ""
    copy_path = pathlib.Path(launch.launch_spec)
    launch.announce()
    twin = kas.v3_root / "e351f88c234ca1f7" / KAS_SID
    twin.mkdir(parents=True)
    (twin / "session.json").write_text(json.dumps(_session_doc("evil")))
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert not kiro_cli_handoff.is_held(KEY)
    assert len(list(kas.records.iterdir())) == 1, "kept: agentMode was not put back"
    assert await kcl.recover_left_open(_kas_state(slot), slot, KEY)
    assert "could not be added" in slot.appended[-1][1]
    assert list(kas.records.iterdir()) == [] and not copy_path.exists()
    kiro_cli_handoff.refuse_if_held(KEY)


@pytest.mark.asyncio
async def test_after_a_restart_a_session_that_gained_a_twin_still_loses_its_launch_copy(
    kas, switch
):
    """The gateway stopped with the terminal open, so nothing released the copy;
    the record that names it is the only thing that can, and it goes now."""
    slot = _slot(kas.project)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_kas_state(slot)), "main", "t")
    assert code == ""
    copy_path = pathlib.Path(launch.launch_spec)
    kiro_cli_handoff.reset_holds()
    twin = kas.v3_root / "e351f88c234ca1f7" / KAS_SID
    twin.mkdir(parents=True)
    assert await kcl.recover_left_open(_kas_state(slot), slot, KEY)
    assert not copy_path.exists(), "removed with the record that named it"
    assert list(kas.records.iterdir()) == []
