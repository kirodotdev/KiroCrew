"""The persona snapshot is taken when kiro-cli reads the spec, never at ``session/new``.

kiro-cli reads an agent's spec at two moments only: when its process spawns
(the ``--agent`` spec) and at a successful ``session/set_mode``. A plain
``session/new`` re-reads nothing (measured on kiro-cli 2.28.0: a spec edited
between the spawn and the next session still answers with the spawn-time
text). So the recorder stores the runtime's spawn-time copy, not the file as
it reads at session start: a persona edited in between must leave the block
IN, because kiro-cli does not hold the edited text. Each copy is read from the
spec kiro-cli is actually given -- the skill-view alias when a view was
prepared, a copy of the spec taken at preparation, else the authored name --
once, immediately before the request that makes kiro-cli read it. At
``set_mode`` the bracket RETURNS that pair; it never writes the map. The
recorder is the map's one writer, runs after the bracket and after every
merge, and prefers that pair over the spawn copy, so a launch source that
already holds the persona is never joined by a second record of the same text.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from kiro_crew.acp.runtime import METHOD_SET_MODE, AcpRuntime
from kiro_crew.member_essential_context import native_prompt_document_key

AGENT = "kirocrew"
ALIAS = "kirocrew-skill-view-" + "a" * 24
SPAWN_TEXT = "You are the persona kiro-cli loaded at spawn."
EDITED_TEXT = "You are the persona edited after spawn; kiro-cli never saw this."
_COPY = "kiro_crew.member_essential_context.native_spec_prompt_copy"
_FRESH = "kiro_crew.agent.require_fresh_derived_spec"
_UNCHANGED = "kiro_crew.agent.require_unchanged_derived_spec"
_PREPARE = "kiro_crew.acp.skill_projection.prepare_native_skill_projection"


async def _run_bracket(rt: AcpRuntime, handle, reads: list, *, wire_registered: bool = False):
    """Drive ``_activate_mode_bracketed`` on a runtime with no skill projection.

    ``reads`` are the values the persona read returns in order (one per send
    attempt). The derived-spec checks are stubbed because the test owns no spec
    file; the ``set_mode`` send is an AsyncMock. Returns (bracket result,
    persona mock, send mock).
    """
    rt._native_skill_projection = None
    send = AsyncMock(return_value={})
    rt._send_and_await = send  # type: ignore[method-assign]
    with (
        patch(_FRESH, return_value=object()),
        patch(_UNCHANGED, return_value=None),
        patch.object(rt, "_native_persona_text", side_effect=reads) as persona,
    ):
        result = await rt._activate_mode_bracketed(
            "s1",
            AGENT,
            budget=30.0,
            payload_snapshot=None,
            wire_registered=wire_registered,
            handle=handle,
        )
    return result, persona, send


def _runtime(tmp_path) -> AcpRuntime:
    return AcpRuntime(work_dir=str(tmp_path))


def _handle() -> SimpleNamespace:
    # The recorder and the set_mode bracket touch one handle attribute only.
    return SimpleNamespace(native_context_documents={})


@pytest.mark.asyncio
async def test_recorder_stores_the_spawn_copy_not_the_edited_file(tmp_path):
    """T1: the spec is edited between the spawn and ``session/new``. The record
    is the spawn-time text and the file is not read at all, so the turn-time
    compare differs from the edited persona and the block is kept."""
    rt = _runtime(tmp_path)
    rt._spawn_persona_snapshot = (AGENT, SPAWN_TEXT)
    handle = _handle()
    with patch(_COPY, return_value=EDITED_TEXT) as copy:
        await rt._record_native_persona_snapshot(handle, AGENT, None)
    copy.assert_not_called()
    assert handle.native_context_documents == {native_prompt_document_key(AGENT): SPAWN_TEXT}
    assert EDITED_TEXT not in handle.native_context_documents.values()


@pytest.mark.asyncio
async def test_recorder_records_nothing_for_an_agent_kiro_cli_did_not_load(tmp_path):
    """T3: the active agent is neither the spawn agent nor one set through
    ``set_mode``: no record (block kept), whatever the file says now."""
    rt = _runtime(tmp_path)
    rt._spawn_persona_snapshot = ("other-agent", SPAWN_TEXT)
    handle = _handle()
    with patch(_COPY, return_value=EDITED_TEXT) as copy:
        await rt._record_native_persona_snapshot(handle, AGENT, None)
    copy.assert_not_called()
    assert handle.native_context_documents == {}


@pytest.mark.asyncio
async def test_recorder_records_nothing_without_a_spawn_snapshot(tmp_path):
    """T3b: a spawn whose two reads disagreed, or found no spec, left no
    snapshot: no record, and still no file read at session time."""
    rt = _runtime(tmp_path)
    assert rt._spawn_persona_snapshot is None
    handle = _handle()
    with patch(_COPY, return_value=EDITED_TEXT) as copy:
        await rt._record_native_persona_snapshot(handle, AGENT, None)
    copy.assert_not_called()
    assert handle.native_context_documents == {}


@pytest.mark.asyncio
async def test_recorder_adds_nothing_when_a_launch_source_already_holds_the_persona(tmp_path):
    """Create path, member launch: the spawn plan's launch sources were merged
    before the recorder ran and already hold the persona under its source path.
    The ``set_mode`` pair carries the same text, so the recorder enters nothing:
    one record per loaded document, and no second copy under the prompt key."""
    rt = _runtime(tmp_path)
    rt._spawn_persona_snapshot = (AGENT, SPAWN_TEXT)
    handle = _handle()
    source_key = "/project/.kiro/agents/kirocrew.json"
    handle.native_context_documents[source_key] = SPAWN_TEXT
    with patch(_COPY, return_value=EDITED_TEXT) as copy:
        await rt._record_native_persona_snapshot(
            handle, AGENT, None, set_mode_persona=(AGENT, SPAWN_TEXT)
        )
    copy.assert_not_called()
    assert handle.native_context_documents == {source_key: SPAWN_TEXT}


@pytest.mark.asyncio
async def test_recorder_prefers_the_set_mode_pair_over_the_spawn_copy(tmp_path):
    """The spec was edited between the spawn and ``set_mode``. kiro-cli re-read
    it at ``set_mode``, so the pair the bracket returned is the text it holds
    now, and that is the one record made; the spawn copy never enters."""
    rt = _runtime(tmp_path)
    rt._spawn_persona_snapshot = (AGENT, SPAWN_TEXT)
    handle = _handle()
    with patch(_COPY, return_value="the file as it reads now") as copy:
        await rt._record_native_persona_snapshot(
            handle, AGENT, None, set_mode_persona=(AGENT, EDITED_TEXT)
        )
    copy.assert_not_called()
    assert handle.native_context_documents == {native_prompt_document_key(AGENT): EDITED_TEXT}


@pytest.mark.asyncio
async def test_recorder_uses_the_spawn_copy_when_the_set_mode_pair_is_another_agents(tmp_path):
    """Either copy counts only when it is the active agent's: a pair read for
    some other agent falls through to the spawn copy of this one."""
    rt = _runtime(tmp_path)
    rt._spawn_persona_snapshot = (AGENT, SPAWN_TEXT)
    handle = _handle()
    await rt._record_native_persona_snapshot(
        handle, AGENT, None, set_mode_persona=("other-agent", EDITED_TEXT)
    )
    assert handle.native_context_documents == {native_prompt_document_key(AGENT): SPAWN_TEXT}


@pytest.mark.asyncio
async def test_bracket_returns_the_text_read_before_the_set_mode_request(tmp_path):
    """T2: kiro-cli re-reads the spec at ``set_mode``, so the bracket reads the
    spec the request names immediately before sending it -- the authored name
    when no view was prepared -- and returns the pair. One read: an edit landing
    after it leaves the file different from the snapshot, so the block is sent,
    never withheld. It writes nothing itself: the recorder is the map's one
    writer."""
    rt = _runtime(tmp_path)
    handle = _handle()
    result, persona, send = await _run_bracket(rt, handle, [(AGENT, EDITED_TEXT)])
    assert persona.call_args_list == [call(AGENT, None)]
    assert send.await_count == 1
    assert send.await_args.args[0] == METHOD_SET_MODE
    assert result == (AGENT, EDITED_TEXT)
    assert handle.native_context_documents == {}


@pytest.mark.asyncio
async def test_bracket_reads_the_skill_view_alias_it_sends(tmp_path):
    """T4: with a view prepared, ``set_mode`` names the skill-view alias, a copy
    of the spec taken at preparation; kiro-cli loads THAT, so the bracket reads
    the alias (keyed by the agent), not the source an edit may since have
    changed. Reading the source here would record the revised text and withhold
    the block while kiro-cli runs the old copy."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt = _runtime(tmp_path)
    handle = _handle()
    prepared = NativeSkillProjection({AGENT: ALIAS})
    rt._native_skill_projection = prepared
    send = AsyncMock(return_value={})
    rt._send_and_await = send  # type: ignore[method-assign]
    with (
        patch(_PREPARE, return_value=prepared),
        patch(_FRESH, return_value=object()),
        patch(_UNCHANGED, return_value=None),
        patch.object(rt, "_native_persona_text", return_value=(AGENT, SPAWN_TEXT)) as persona,
    ):
        result = await rt._activate_mode_bracketed(
            "s1", AGENT, budget=30.0, payload_snapshot=None, wire_registered=False, handle=handle
        )
    assert persona.call_args_list == [call(AGENT, ALIAS)]
    assert send.await_count == 1
    assert send.await_args.args[1]["modeId"] == ALIAS
    assert result == (AGENT, SPAWN_TEXT)
    assert handle.native_context_documents == {}


def test_spawn_reads_the_translated_agent_once_before_the_launch():
    """The spawn path hands kiro-cli the translated ``--agent`` (the alias when a
    view was prepared) and reads the persona of that spec, once, immediately
    before the launch; there is no second read after ``initialize``."""
    src = inspect.getsource(AcpRuntime._spawn_admitted)
    assert "spawn_transport = self._agent" in src
    assert "spawn_transport = argv[agent_position]" in src
    assert src.count("_spawn_persona_text") == 1
    read = src.index("self._spawn_persona_text, spawn_transport")
    assert (
        src.index("spawn_transport = argv[agent_position]")
        < read
        < src.index("launched = await launch(")
    )
    assert "persona_after_init" not in src


def test_spawn_persona_text_reads_the_transport_spec(tmp_path):
    """``_spawn_persona_text`` resolves the spawn agent's copy under the name
    kiro-cli was given, scoped to the runtime's work dir (the process cwd
    kiro-cli resolves a plain name from)."""
    rt = _runtime(tmp_path)
    with patch(_COPY, return_value=SPAWN_TEXT) as copy:
        assert rt._spawn_persona_text(ALIAS) == (AGENT, SPAWN_TEXT)
    copy.assert_called_once_with(AGENT, transport=ALIAS, work_dir=rt._work_dir)
    assert rt._work_dir == tmp_path


def test_plain_name_read_is_scoped_to_the_runtimes_work_dir(tmp_path):
    """With no skill view the plain name goes to kiro-cli, which resolves it from
    its process cwd before the user level; the read is handed that same dir so a
    checkout spec that shadows the user-level one is the copy recorded."""
    rt = _runtime(tmp_path)
    with patch(_COPY, return_value=SPAWN_TEXT) as copy:
        assert rt._native_persona_text(AGENT, AGENT) == (AGENT, SPAWN_TEXT)
        assert rt._native_persona_text(AGENT, None) == (AGENT, SPAWN_TEXT)
    assert copy.call_args_list == [
        call(AGENT, transport=AGENT, work_dir=tmp_path),
        call(AGENT, transport=None, work_dir=tmp_path),
    ]


@pytest.mark.asyncio
async def test_resume_records_the_set_mode_pair_not_the_spawn_copy(tmp_path):
    """T5: on ``session/load`` the bracket runs first and the recorder after it,
    with the bracket's pair, so the one record is the text kiro-cli re-read at
    ``set_mode``; the spawn-time copy never enters the map."""
    rt = _runtime(tmp_path)
    rt._spawn_persona_snapshot = (AGENT, SPAWN_TEXT)
    handle = _handle()
    key = native_prompt_document_key(AGENT)
    pair, _persona, _send = await _run_bracket(rt, handle, [(AGENT, EDITED_TEXT)])
    assert handle.native_context_documents == {}
    with patch(_COPY, return_value="the file as it reads now") as copy:
        await rt._record_native_persona_snapshot(handle, AGENT, None, set_mode_persona=pair)
    copy.assert_not_called()
    assert handle.native_context_documents == {key: EDITED_TEXT}
    assert SPAWN_TEXT not in handle.native_context_documents.values()


@pytest.mark.asyncio
async def test_bracket_reads_nothing_for_a_wire_registered_host(tmp_path):
    """A host that received the spec on the wire (KAS) is recorded from its
    payload by the recorder; the bracket must not read the file for it and
    returns ``None``."""
    rt = _runtime(tmp_path)
    handle = _handle()
    result, persona, send = await _run_bracket(rt, handle, [], wire_registered=True)
    persona.assert_not_called()
    assert send.await_count == 1
    assert result is None
    assert handle.native_context_documents == {}
