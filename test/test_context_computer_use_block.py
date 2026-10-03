"""`{{COMPUTER_USE_BLOCK}}` follows the spec gate that mounts `kirocrew-computer`.

``agent._computer_use_spec_gate`` keeps the server out of the emitted spec whenever
the feature is off or the OS has no driver, so such a session has no ``computer_*``
tool. The default agent prompt's Computer Use slot resolves against that same gate:
the full section when it is open, a short pointer to the setting when it is closed.
The reading is taken once per session, the way the delegation-cap figure is, so a
compaction restores the contract the session started with.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import sections
from kiro_crew.context_assembly.markers import _MULTIBYTE_TABLE
from kiro_crew.memory import MemoryStore
from kiro_crew.session import SessionManager
from kiro_crew.skills import SkillsLoader

_HEADING = "## Computer Use (native desktop apps)"
# Phrases only the full section carries: how to call the tools.
_SECTION_ONLY = ("computer_get_state", 'click_method: "global"', "computer_launch_app")
_POINTER = sections.computer_use_block(False)
_SECTION = sections.computer_use_block(True)
# `build_message` folds its final text through this table (em dash to `--` and so
# on), so a whole message carries the folded spelling of each block.
_POINTER_FOLDED = _POINTER.translate(_MULTIBYTE_TABLE)
_SECTION_FOLDED = _SECTION.translate(_MULTIBYTE_TABLE)
_SHIPPED_PROMPT = Path(sections.__file__).resolve().parents[1] / "config" / "prompt.md"


class _Gate:
    """A stand-in for ``agent._computer_use_spec_gate`` whose answer a test moves."""

    def __init__(self, open_: bool) -> None:
        self.open = open_
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.open


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    g = _Gate(False)
    monkeypatch.setattr("kiro_crew.agent._computer_use_spec_gate", g)
    return g


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    # The SHIPPED contract, not a data-home override a host may carry.
    monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: _SHIPPED_PROMPT)
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


def _contract(message: str) -> str:
    start = message.index("[AGENT SYSTEM PROMPT]\n") + len("[AGENT SYSTEM PROMPT]\n")
    return message[start : message.index("\n[END AGENT SYSTEM PROMPT]", start)]


def _start(builder: ContextBuilder, key: str) -> str:
    message, _ = builder.build_message("first turn", is_new_session=True, session_key=key)
    return _contract(message)


def _restore(builder: ContextBuilder, key: str) -> str:
    message, _ = builder.build_message(
        "carry on", is_new_session=False, needs_reinjection=True, session_key=key
    )
    return _contract(message)


class TestTheSlot:
    def test_the_shipped_prompt_carries_the_slot_not_the_section(self) -> None:
        text = _SHIPPED_PROMPT.read_text(encoding="utf-8")
        assert text.count(ContextBuilder._COMPUTER_USE_TOKEN) == 1
        assert _HEADING not in text
        for phrase in _SECTION_ONLY:
            assert phrase not in text

    def test_closed_gate_resolves_to_the_pointer(self) -> None:
        out = ContextBuilder._resolve_prompt_templates(
            "a\n{{COMPUTER_USE_BLOCK}}\nb", "cli:local", computer_use=False
        )
        assert out == f"a\n{_POINTER}\nb"
        for phrase in _SECTION_ONLY:
            assert phrase not in out
        flat = " ".join(out.split())
        assert "Settings → Computer Use" in flat
        assert "no `computer_*` tools are mounted" in flat
        # A session left open across an enable resumes with this text and the tools.
        assert "tools are in your tool list anyway" in flat
        assert "read the `computer-use` skill before your first call" in flat

    def test_open_gate_resolves_to_the_section_verbatim(self) -> None:
        out = ContextBuilder._resolve_prompt_templates(
            "a\n{{COMPUTER_USE_BLOCK}}\nb", "cli:local", computer_use=True
        )
        assert out == f"a\n{_SECTION}\nb"
        assert _SECTION.startswith(_HEADING + "\n\n")
        assert _SECTION.endswith("Read the `computer-use` skill before your first call.")

    @pytest.mark.parametrize("open_", [True, False])
    def test_without_a_session_reading_the_live_gate_decides(
        self, gate: _Gate, open_: bool
    ) -> None:
        gate.open = open_
        out = ContextBuilder._resolve_prompt_templates("{{COMPUTER_USE_BLOCK}}", "cli:local")
        assert out == (_SECTION if open_ else _POINTER)
        assert gate.calls == 1

    def test_an_unreadable_gate_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> bool:
            raise OSError("keystone unreadable")

        monkeypatch.setattr("kiro_crew.agent._computer_use_spec_gate", boom)
        assert ContextBuilder._live_computer_use_gate() is False
        out = ContextBuilder._resolve_prompt_templates("{{COMPUTER_USE_BLOCK}}", "cli:local")
        assert out == _POINTER

    def test_a_prompt_without_the_slot_never_reads_the_gate(
        self, gate: _Gate, builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        ContextBuilder._resolve_prompt_templates("no slot here", "cli:local")
        custom = tmp_path / "custom.md"
        custom.write_text("You are {bot_name}.", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: custom)
        builder._resolve_agent_prompt(
            None,
            project=None,
            mode="",
            session_key="dashboard:no-slot",
            is_cc=False,
            private_owner=False,
            session_start=True,
        )
        assert gate.calls == 0


class TestTheShippedContract:
    """The default agent's real contract, resolved the way session start resolves it."""

    @staticmethod
    def _resolve(builder: ContextBuilder, *, is_cc: bool = False) -> str:
        return builder._resolve_agent_prompt(
            None,
            project=None,
            mode="",
            session_key="dashboard:shipped",
            is_cc=is_cc,
            private_owner=False,
            session_start=True,
        )

    @pytest.mark.parametrize("is_cc", [False, True])
    def test_closed_gate_leaves_only_the_pointer(
        self, gate: _Gate, builder: ContextBuilder, is_cc: bool
    ) -> None:
        gate.open = False
        prompt = self._resolve(builder, is_cc=is_cc)
        assert _POINTER in prompt
        for phrase in _SECTION_ONLY:
            assert phrase not in prompt
        assert not re.search(r"\{\{[A-Z_]+\}\}", prompt)

    @pytest.mark.parametrize("is_cc", [False, True])
    def test_open_gate_carries_the_section_verbatim(
        self, gate: _Gate, builder: ContextBuilder, is_cc: bool
    ) -> None:
        gate.open = True
        prompt = self._resolve(builder, is_cc=is_cc)
        assert _SECTION in prompt
        assert _POINTER not in prompt
        assert not re.search(r"\{\{[A-Z_]+\}\}", prompt)

    def test_the_open_render_is_the_shipped_text_with_the_section_in_place(
        self, gate: _Gate, builder: ContextBuilder
    ) -> None:
        """Gated on, the slot reproduces the file as it read before the move:
        the section sits between the Browser section and the widget slot."""
        gate.open = True
        prompt = self._resolve(builder)
        browser_tail = "`browser-auth` carries logged-in sessions.\n\n"
        assert browser_tail + _SECTION + "\n\n" in prompt + "\n\n"

    def test_residual_the_slot_follows_the_host_gate_not_an_agents_tools_list(
        self,
        gate: _Gate,
        builder: ContextBuilder,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PINNED RESIDUAL, not a contract to keep. The slot reads the same
        host-wide predicate the spec writers do, so a fork of the managed contract
        whose own ``tools`` list leaves out ``@kirocrew-computer`` still gets the
        full section while the gate is open. Narrowing it per agent means reading
        the named agent's own spec (its ``mcpServers`` and ``tools``), which this
        change does not do."""
        import json

        from kiro_crew.agent import _NATIVE_PROMPT_STUB

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "fork.json").write_text(
            json.dumps({"name": "fork", "prompt": _NATIVE_PROMPT_STUB, "tools": []}),
            encoding="utf-8",
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents_dir)
        gate.open = True
        prompt = builder._resolve_agent_prompt(
            "fork",
            project=None,
            mode="",
            session_key="dashboard:fork",
            is_cc=False,
            private_owner=False,
            session_start=True,
        )
        assert _SECTION in prompt


class TestOneReadingPerSession:
    """The contract block is asserted byte-identical across a session's renders."""

    @pytest.mark.parametrize("open_at_start", [True, False])
    def test_a_compaction_restores_the_session_start_block(
        self, gate: _Gate, builder: ContextBuilder, open_at_start: bool
    ) -> None:
        gate.open = open_at_start
        fresh = _start(builder, "dashboard:cu-a")
        gate.open = not open_at_start
        restored = _restore(builder, "dashboard:cu-a")
        assert restored == fresh
        assert (_SECTION_FOLDED in fresh) is open_at_start
        assert (_POINTER_FOLDED in fresh) is not open_at_start

    def test_a_new_session_start_takes_a_new_reading(
        self, gate: _Gate, builder: ContextBuilder
    ) -> None:
        gate.open = False
        first = _start(builder, "dashboard:cu-b")
        gate.open = True
        second = _start(builder, "dashboard:cu-b")
        assert _POINTER_FOLDED in first and _SECTION_FOLDED not in first
        assert _SECTION_FOLDED in second and _POINTER_FOLDED not in second

    def test_a_sibling_session_start_leaves_this_contract_alone(
        self, gate: _Gate, builder: ContextBuilder
    ) -> None:
        gate.open = True
        fresh = _start(builder, "dashboard:cu-c")
        gate.open = False
        _start(builder, "dashboard:cu-d")
        assert _restore(builder, "dashboard:cu-c") == fresh

    @pytest.mark.parametrize("open_at_start", [True, False])
    def test_a_resume_after_the_switch_restores_the_new_tools_block(
        self, gate: _Gate, builder: ContextBuilder, open_at_start: bool
    ) -> None:
        """Switching Computer Use rebuilds the spec and resets every session, and
        the session's next turn resumes on a backend spawned from that spec. A
        later compaction must restore the block for the tools THAT backend has."""
        gate.open = open_at_start
        _start(builder, "dashboard:cu-r")
        gate.open = not open_at_start
        builder.build_message(
            "back again", is_new_session=True, resumed=True, session_key="dashboard:cu-r"
        )
        restored = _restore(builder, "dashboard:cu-r")
        assert (_SECTION_FOLDED in restored) is (not open_at_start)
        assert (_POINTER_FOLDED in restored) is open_at_start

    def test_a_resume_without_a_switch_keeps_the_contract(
        self, gate: _Gate, builder: ContextBuilder
    ) -> None:
        gate.open = True
        fresh = _start(builder, "dashboard:cu-s")
        builder.build_message(
            "back again", is_new_session=True, resumed=True, session_key="dashboard:cu-s"
        )
        assert _restore(builder, "dashboard:cu-s") == fresh

    def test_the_reading_is_memoised_under_the_shared_bound(
        self, gate: _Gate, builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ContextBuilder, "_CAP_FIGURE_SESSIONS", 2)
        gate.open = True
        for key in ("dashboard:m-a", "dashboard:m-b", "dashboard:m-c"):
            assert builder._session_computer_use_gate(key, refresh=True) is True
        assert len(builder._computer_use_gates) == 2
        assert ContextBuilder._cap_memo_key("dashboard:m-a") not in builder._computer_use_gates
        # A cached False is a reading, not a miss.
        gate.open = False
        assert builder._session_computer_use_gate("dashboard:m-z", refresh=True) is False
        gate.open = True
        assert builder._session_computer_use_gate("dashboard:m-z", refresh=False) is False


class TestASessionOpenAcrossTheSwitch:
    """Enabling Computer Use rebuilds the spec and resets every session.

    Which happens next decides what the reset session's prompt must say: a fresh
    start re-renders the contract against the new gate, a resume restores the old
    transcript and re-sends no contract at all.
    """

    @pytest.mark.asyncio
    async def test_the_reset_keeps_the_resume_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_reset_all_sessions` reloads the factory and drains sessions and pool;
        none of the three drops the key's stored session id, so the next turn
        resumes the transcript (``session/load``) rather than starting fresh."""
        from unittest.mock import AsyncMock, MagicMock

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.memory_mode = kwargs.get("memory_mode", "persistent")
            m.is_process_alive = lambda: True
            m.disown_work_dir = MagicMock()
            m.context_usage_pct = lambda: 0.0
            return m

        # A resumable kiro-cli transcript, which is what `SessionMap.get` checks
        # for before it hands the sid out.
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sid-before-reset.json").write_text("{}", encoding="utf-8")
        (sessions_dir / "sid-before-reset.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", sessions_dir)

        mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
        await mgr.get_or_create("dashboard:reset-me")
        mgr.release("dashboard:reset-me")
        mgr._session_map.set("dashboard:reset-me", "sid-before-reset")
        assert mgr._session_map.get("dashboard:reset-me") == "sid-before-reset"

        await mgr.reload_provider_factory()
        await mgr.drain_all_providers()
        await mgr.drain_warm_pool()

        assert mgr._session_map.get("dashboard:reset-me") == "sid-before-reset"

    def test_a_resumed_session_is_not_sent_the_contract_again(
        self, gate: _Gate, builder: ContextBuilder
    ) -> None:
        """So a session open across an enable keeps the pointer it started with
        while its new backend mounts the tools: the pointer, not a re-render, is
        what has to tell it the tools may now be there. The resume does take a
        fresh reading, for the next compaction to restore."""
        gate.open = True
        message, _ = builder.build_message(
            "back again", is_new_session=True, resumed=True, session_key="dashboard:resumed"
        )
        assert "[AGENT SYSTEM PROMPT]" not in message
        assert gate.calls == 1
