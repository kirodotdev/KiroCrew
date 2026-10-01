"""``agent.dedupe_agent_prompt``: the agent prompt is delivered once, not twice.

kiro-cli reads a spec's ``prompt`` off disk for ``--agent`` and KAS has it inlined
onto the wire, so on those harnesses the model receives the prompt as its system
instruction on every request. ``context.py`` ALSO injects it as the
``[AGENT SYSTEM PROMPT]`` block at session start and after a compaction. With the
knob on, a block that is byte-identical to what the harness recorded as its own
delivery (``template://<agent>#prompt`` in ``native_context_documents``) is withheld;
every other case -- knob off, no record, a record that differs, the managed stub --
keeps the block exactly as before.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp.skill_projection import NativeSkillProjection
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.agent import _NATIVE_PROMPT_STUB
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.member_essential_context import ESSENTIAL_MAX_CHARS
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import LLMEvent, LLMProvider
from kiro_crew.skills import SkillsLoader

BLOCK = "[AGENT SYSTEM PROMPT]\n"
PROMPT = "You are a bespoke reviewer.\nReport findings as FINDING lines."


class _RecordingProvider(LLMProvider):
    """A real provider type (``context_provider_of`` admits only those) holding a
    native-document record, which is all the prompt builder reads from it."""

    def __init__(self, native: dict[str, str]) -> None:
        self._native = dict(native)

    @property
    def native_context_documents(self) -> dict[str, str]:
        return dict(self._native)

    async def start(self) -> None:  # pragma: no cover - never driven here
        pass

    async def shutdown(self) -> None:  # pragma: no cover - never driven here
        pass

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:  # pragma: no cover
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=message)
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id, *, always: bool = False) -> None:  # pragma: no cover
        pass

    async def reject_tool(self, request_id) -> None:  # pragma: no cover
        pass

    def context_usage_pct(self) -> float:  # pragma: no cover
        return 0.0


def _write_spec(tmp_path, monkeypatch, prompt: str, name: str = "test") -> None:
    agents_dir = tmp_path / ".kiro" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.json").write_text(
        json.dumps({"name": name, "prompt": prompt}), encoding="utf-8"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents_dir)


def _managed_contract(tmp_path, monkeypatch) -> None:
    from kiro_crew import agent

    package = tmp_path / "installed-package" / "config"
    package.mkdir(parents=True)
    (package / "prompt.md").write_text("RESOLVED_CONTRACT", encoding="utf-8")
    monkeypatch.setattr(agent, "_BUNDLED_CFG_DIR", package)
    monkeypatch.setattr(agent, "_project_dir", lambda: None)


def _knob(monkeypatch, enabled: bool) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.dedupe_agent_prompt = enabled
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)


def _builder(tmp_path) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


def _block_text(msg: str) -> str:
    start = msg.index(BLOCK) + len(BLOCK)
    return msg[start : msg.index("\n[END AGENT SYSTEM PROMPT]", start)]


class TestSessionStart:
    def test_knob_off_keeps_the_block_even_with_an_identical_record(self, tmp_path, monkeypatch):
        """Default off = today's behavior byte for byte, record or no record."""
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, False)
        provider = _RecordingProvider({"template://test#prompt": PROMPT})
        msg, _ = _builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        assert msg.count(BLOCK) == 1
        assert _block_text(msg) == PROMPT

    def test_knob_on_withholds_a_block_the_harness_already_delivered(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        provider = _RecordingProvider({"template://test#prompt": PROMPT})
        msg, _ = _builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        assert BLOCK not in msg
        assert PROMPT not in msg
        # Nothing else about the turn moves: the request and the session context stay.
        assert msg.rstrip().endswith("hello")
        assert "[SESSION CONTEXT" in msg

    def test_knob_on_changes_nothing_but_the_block(self, tmp_path, monkeypatch):
        """The whole turn with the knob on is the knob-off turn minus exactly the block."""
        _write_spec(tmp_path, monkeypatch, PROMPT)
        provider = _RecordingProvider({"template://test#prompt": PROMPT})
        builder = _builder(tmp_path)
        _knob(monkeypatch, False)
        off, _ = builder.build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        _knob(monkeypatch, True)
        on, _ = builder.build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        block = f"[AGENT SYSTEM PROMPT]\n{PROMPT}\n[END AGENT SYSTEM PROMPT]\n\n"
        assert block in off
        assert off.replace(block, "", 1) == on

    def test_knob_on_without_a_record_keeps_the_block(self, tmp_path, monkeypatch):
        """A harness that projects no prompt (the mirrors) records nothing, so the
        block stays its only copy. Same for a turn built with no provider at all."""
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        builder = _builder(tmp_path)
        with_provider, _ = builder.build_message(
            "hello", is_new_session=True, agent="test", context_provider=_RecordingProvider({})
        )
        without_provider, _ = builder.build_message("hello", is_new_session=True, agent="test")
        assert _block_text(with_provider) == PROMPT
        assert _block_text(without_provider) == PROMPT

    def test_knob_on_keeps_the_block_when_the_record_differs(self, tmp_path, monkeypatch):
        """kiro-cli delivers the RAW spec text; a template token resolves to something
        else in the block, so the two copies are not the same and both stay."""
        _write_spec(tmp_path, monkeypatch, "Fan out to at most {{MAX_SUBAGENTS}} workers.")
        _knob(monkeypatch, True)
        provider = _RecordingProvider(
            {"template://test#prompt": "Fan out to at most {{MAX_SUBAGENTS}} workers."}
        )
        msg, _ = _builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        assert "{{MAX_SUBAGENTS}}" not in _block_text(msg)
        assert _block_text(msg).startswith("Fan out to at most ")

    def test_knob_on_keeps_the_managed_contract_whose_spec_is_a_stub(self, tmp_path, monkeypatch):
        """The default agent's spec carries the stub, so kiro-cli delivers the STUB and
        the block is the contract's only copy -- the record is the stub, never the
        contract, and the comparison fails as it must."""
        _managed_contract(tmp_path, monkeypatch)
        _write_spec(tmp_path, monkeypatch, _NATIVE_PROMPT_STUB)
        _knob(monkeypatch, True)
        provider = _RecordingProvider({"template://test#prompt": _NATIVE_PROMPT_STUB})
        msg, _ = _builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        assert _block_text(msg) == "RESOLVED_CONTRACT"

    def test_record_for_another_agent_does_not_count(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        provider = _RecordingProvider({"template://other#prompt": PROMPT})
        msg, _ = _builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        assert _block_text(msg) == PROMPT


class TestReinjection:
    """After a compaction the block is restored -- unless the harness still carries it."""

    def test_knob_off_reinjects(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, False)
        provider = _RecordingProvider({"template://test#prompt": PROMPT})
        builder = _builder(tmp_path)
        builder.build_message("first", is_new_session=True, agent="test", context_provider=provider)
        msg, _ = builder.build_message(
            "carry on",
            is_new_session=False,
            needs_reinjection=True,
            agent="test",
            context_provider=provider,
        )
        assert _block_text(msg) == PROMPT

    def test_knob_on_does_not_reinject_a_natively_carried_prompt(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        provider = _RecordingProvider({"template://test#prompt": PROMPT})
        builder = _builder(tmp_path)
        builder.build_message("first", is_new_session=True, agent="test", context_provider=provider)
        msg, _ = builder.build_message(
            "carry on",
            is_new_session=False,
            needs_reinjection=True,
            agent="test",
            context_provider=provider,
        )
        assert BLOCK not in msg
        assert msg.rstrip().endswith("carry on")

    def test_knob_on_reinjects_when_nothing_was_recorded(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        builder = _builder(tmp_path)
        msg, _ = builder.build_message(
            "carry on", is_new_session=False, needs_reinjection=True, agent="test"
        )
        assert _block_text(msg) == PROMPT


def _projection(**specs: dict) -> NativeSkillProjection:
    """A spawn skill projection holding the view specs kiro-cli was started on."""
    return NativeSkillProjection(
        aliases={name: f"kirocrew-skill-view-{i:024x}" for i, name in enumerate(specs)},
        specs=dict(specs),
    )


class TestProjectionRecord:
    """What the runtime records for kiro-cli: the prompt of the view spec it wrote
    and started the process on, never a fresh read of the user-editable source."""

    def test_knob_off_records_nothing(self, monkeypatch):
        _knob(monkeypatch, False)
        assert (
            acp_runtime._native_prompt_from_projection(_projection(test={"prompt": PROMPT}), "test")
            is None
        )

    def test_inline_prompt_is_the_record(self, monkeypatch):
        _knob(monkeypatch, True)
        assert (
            acp_runtime._native_prompt_from_projection(_projection(test={"prompt": PROMPT}), "test")
            == PROMPT
        )

    def test_managed_stub_is_recorded_as_the_stub(self, monkeypatch):
        """kiro-cli sees the stub, so the stub is the record; the block carries the
        resolved contract, so the comparison fails and the block stays."""
        _knob(monkeypatch, True)
        assert (
            acp_runtime._native_prompt_from_projection(
                _projection(test={"prompt": _NATIVE_PROMPT_STUB}), "test"
            )
            == _NATIVE_PROMPT_STUB
        )

    def test_file_prompt_is_declined(self, monkeypatch):
        """kiro-cli read that file on its own; its bytes are not in hand."""
        _knob(monkeypatch, True)
        view = {"prompt": "file:///somewhere/persona.md"}
        assert acp_runtime._native_prompt_from_projection(_projection(test=view), "test") is None

    def test_oversized_prompt_is_not_retained(self, monkeypatch):
        """The record lives on every handle, so it carries the bound the member
        envelope already puts on a template prompt."""
        _knob(monkeypatch, True)
        over = _projection(test={"prompt": "x" * (ESSENTIAL_MAX_CHARS + 1)})
        at = _projection(test={"prompt": "y" * ESSENTIAL_MAX_CHARS})
        assert acp_runtime._native_prompt_from_projection(over, "test") is None
        assert acp_runtime._native_prompt_from_projection(at, "test") == "y" * ESSENTIAL_MAX_CHARS

    @pytest.mark.parametrize("view", [{}, {"prompt": ""}, {"prompt": None}, {"prompt": 7}])
    def test_prompt_less_or_malformed_view_records_nothing(self, monkeypatch, view):
        _knob(monkeypatch, True)
        assert acp_runtime._native_prompt_from_projection(_projection(test=view), "test") is None

    def test_an_agent_the_projection_does_not_know_records_nothing(self, monkeypatch):
        _knob(monkeypatch, True)
        assert (
            acp_runtime._native_prompt_from_projection(
                _projection(test={"prompt": PROMPT}), "other"
            )
            is None
        )

    def test_an_unreadable_config_or_projection_never_raises(self, monkeypatch):
        """An optimisation must not be able to fail a session start."""
        monkeypatch.setattr(
            KiroCrewConfig, "load", lambda: SimpleNamespace(agent=SimpleNamespace())
        )
        assert (
            acp_runtime._native_prompt_from_projection(_projection(test={"prompt": PROMPT}), "test")
            is None
        )

        def _boom():
            raise RuntimeError("config store unavailable")

        monkeypatch.setattr(KiroCrewConfig, "load", _boom)
        assert (
            acp_runtime._native_prompt_from_projection(_projection(test={"prompt": PROMPT}), "test")
            is None
        )
        _knob(monkeypatch, True)
        assert acp_runtime._native_prompt_from_projection(object(), "test") is None


class TestRuntimeHandleRecord:
    """The record lands on the session handle, under the key the prompt builder reads.

    Driven on a real ``AcpRuntime`` and a real handle, so renaming or dropping the
    key, or skipping the record step, goes red here rather than only in the helper
    tests above.
    """

    @staticmethod
    def _runtime(tmp_path, backend=ACP_BACKEND_KIRO):
        return acp_runtime.AcpRuntime(work_dir=str(tmp_path), agent="test", acp_backend=backend)

    @staticmethod
    def _handle(rt):
        return acp_runtime.AcpSessionHandle("s-dedupe", asyncio.Queue(), rt)

    @pytest.mark.asyncio
    async def test_launch_prompt_is_recorded_and_withholds_the_block(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        rt = self._runtime(tmp_path)
        rt._spawn_skill_projection = _projection(test={"prompt": PROMPT})
        handle = self._handle(rt)
        await rt._record_native_prompt(handle, "test", rt._session_projection(False))
        assert handle.native_context_documents == {"template://test#prompt": PROMPT}
        # End to end: the builder reads that record and withholds the block.
        provider = _RecordingProvider(handle.native_context_documents)
        msg, _ = _builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        assert BLOCK not in msg

    @pytest.mark.asyncio
    async def test_a_source_spec_edited_after_the_spawn_keeps_the_block(
        self, tmp_path, monkeypatch
    ):
        """The record is what kiro-cli loaded; the block is built from the source
        spec. Rewrite the source and the two differ, so the block stays and the
        model is never left with only the older prompt the harness still runs."""
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        rt = self._runtime(tmp_path)
        rt._spawn_skill_projection = _projection(test={"prompt": PROMPT})
        handle = self._handle(rt)
        await rt._record_native_prompt(handle, "test", rt._session_projection(False))
        _write_spec(tmp_path, monkeypatch, PROMPT + "\nA rule added after the spawn.")
        provider = _RecordingProvider(handle.native_context_documents)
        msg, _ = _builder(tmp_path).build_message(
            "hello", is_new_session=True, agent="test", context_provider=provider
        )
        assert _block_text(msg) == PROMPT + "\nA rule added after the spawn."

    @pytest.mark.asyncio
    async def test_no_projection_records_nothing(self, tmp_path, monkeypatch):
        """A spawn that fell back to the authored agents, or a harness that never
        projects, leaves the block as the only copy."""
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        rt = self._runtime(tmp_path)
        assert rt._session_projection(False) is None
        assert rt._session_projection(True) is None
        handle = self._handle(rt)
        await rt._record_native_prompt(handle, "test", rt._session_projection(False))
        assert handle.native_context_documents == {}

    def test_an_activated_session_runs_on_the_current_projection(self, tmp_path):
        """set_mode re-prepares and adopts a projection before activating the agent,
        so an activated session's evidence is the CURRENT projection; a session that
        ran no set_mode is on the process's launch agent, so its evidence is the
        spawn's."""
        rt = self._runtime(tmp_path)
        spawn = _projection(test={"prompt": PROMPT})
        current = _projection(test={"prompt": PROMPT + " v2"}, other={"prompt": "Other persona."})
        rt._spawn_skill_projection = spawn
        rt._native_skill_projection = current
        assert rt._session_projection(False) is spawn
        assert rt._session_projection(True) is current


class TestCreateSessionRecord:
    """Through ``create_session`` / ``load_session`` themselves, with a fake wire: the
    record lands on the handle the caller receives, follows the projection the
    session was activated on, and a cancellation inside the record step ends the
    session instead of orphaning it."""

    SPAWN = {"prompt": PROMPT}
    CURRENT = {"prompt": PROMPT + " as re-projected at set_mode"}

    @staticmethod
    def _runtime(tmp_path):
        rt = acp_runtime.AcpRuntime(work_dir=str(tmp_path), agent="test")
        rt._process = SimpleNamespace(returncode=None, pid=4242, stdin=None)
        rt._pid = 4242
        rt._initialized = True
        rt._expect_mcp_reports = False
        rt._spawn_skill_projection = _projection(test=TestCreateSessionRecord.SPAWN)
        return rt

    @staticmethod
    def _fake_wire(rt, monkeypatch, *, modes=None):
        async def _fake_send(method, params, timeout=None):
            if method in (acp_runtime.METHOD_SESSION_NEW, acp_runtime.METHOD_SESSION_LOAD):
                resp = {"sessionId": "sid-dedupe"}
                if modes is not None:
                    resp["modes"] = {
                        "currentModeId": "test",
                        "availableModes": [{"id": m, "name": m} for m in modes],
                    }
                return resp
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    @staticmethod
    def _activation_adopts(rt, monkeypatch, projection):
        """Stand in for ``_activate_mode_bracketed``: the real one re-prepares and
        adopts a projection before sending set_mode; model just the adoption."""

        async def _activate(session_id, mode_agent, **kwargs):
            rt._native_skill_projection = projection

        monkeypatch.setattr(rt, "_activate_mode_bracketed", _activate)

    @pytest.mark.asyncio
    async def test_record_reaches_the_returned_handle(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        rt = self._runtime(tmp_path)
        self._fake_wire(rt, monkeypatch)
        handle = await rt.create_session(cwd=str(tmp_path), mcp_servers=[])
        assert handle.native_context_documents.get("template://test#prompt") == PROMPT

    @pytest.mark.asyncio
    async def test_knob_off_leaves_the_handle_without_a_record(self, tmp_path, monkeypatch):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, False)
        rt = self._runtime(tmp_path)
        self._fake_wire(rt, monkeypatch)
        handle = await rt.create_session(cwd=str(tmp_path), mcp_servers=[])
        assert "template://test#prompt" not in handle.native_context_documents

    @pytest.mark.asyncio
    async def test_an_activated_session_records_the_projection_set_mode_adopted(
        self, tmp_path, monkeypatch
    ):
        """With a projection re-prepared at set_mode, the session runs ITS view, not
        the spawn's; the record says so even for an agent other than the launch one."""
        _knob(monkeypatch, True)
        rt = self._runtime(tmp_path)
        current = _projection(test=self.CURRENT, other={"prompt": "Other persona."})
        self._activation_adopts(rt, monkeypatch, current)
        self._fake_wire(rt, monkeypatch, modes=["test", "other"])
        same_agent = await rt.create_session(cwd=str(tmp_path), agent="test", mcp_servers=[])
        assert same_agent.native_context_documents.get("template://test#prompt") == (
            self.CURRENT["prompt"]
        )
        switched = await rt.create_session(cwd=str(tmp_path), agent="other", mcp_servers=[])
        assert switched.native_context_documents.get("template://other#prompt") == "Other persona."

    @pytest.mark.asyncio
    async def test_a_resumed_session_records_too(self, tmp_path, monkeypatch):
        _knob(monkeypatch, True)
        rt = self._runtime(tmp_path)
        rt._can_load_session = True
        self._fake_wire(rt, monkeypatch, modes=["test"])  # a genuine resume echoes modes
        handle = await rt.load_session(
            str(tmp_path / "sid-dedupe.json"), "sid-dedupe", cwd=str(tmp_path)
        )
        assert handle.native_context_documents.get("template://test#prompt") == PROMPT

    @pytest.mark.asyncio
    async def test_a_cancellation_during_the_record_terminates_the_session(
        self, tmp_path, monkeypatch
    ):
        _write_spec(tmp_path, monkeypatch, PROMPT)
        _knob(monkeypatch, True)
        rt = self._runtime(tmp_path)
        self._fake_wire(rt, monkeypatch)
        terminated: list[str] = []

        async def _terminate(session_id, *args, **kwargs):
            terminated.append(session_id)

        async def _cancelled(handle, active_agent, projection):
            raise asyncio.CancelledError()

        monkeypatch.setattr(rt, "terminate_session", _terminate)
        monkeypatch.setattr(rt, "_record_native_prompt", _cancelled)
        with pytest.raises(asyncio.CancelledError):
            await rt.create_session(cwd=str(tmp_path), mcp_servers=[])
        assert terminated == ["sid-dedupe"]


def test_knob_defaults_off():
    assert KiroCrewConfig().agent.dedupe_agent_prompt is False
