"""The agent prompt's Browser section follows the browse gate the ``browser`` tool reads.

``browser_harness.ops.browsing_available()`` is False when the Browser Harness is
not installed, and the ``browser`` tool then refuses every op. A session started that way
gets a short pointer where the section was. The section stays when the gate reads
True, and when the gate cannot be read: a section kept by mistake costs tokens, one
dropped by mistake costs a working browser its rules. The reading is taken at session
start and held, like the Computer Use one, so a compaction restores the contract the
session started with.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import agent_spec, sections
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_SHIPPED_PROMPT = Path(sections.__file__).resolve().parents[1] / "config" / "prompt.md"
_BEGIN = "{{BROWSER_BEGIN}}"
_END = "{{BROWSER_END}}"
# Phrases only the full section carries: how to drive and share a browser.
_SECTION_ONLY = ("Refs die with the page", "Passwords are never yours to type", "user_in_control")


def _pointer() -> str:
    """What a marked section becomes while the gate reads False."""
    return sections.browser_section(f"{_BEGIN}\nX\n{_END}", False)


def _marked_text() -> str:
    """The shipped section exactly as the file holds it, markers excluded."""
    raw = _SHIPPED_PROMPT.read_text(encoding="utf-8")
    return raw.split(_BEGIN + "\n", 1)[1].split("\n" + _END, 1)[0]


class _Gate:
    """A stand-in for ``browser_harness.ops.browsing_available`` whose answer a test moves."""

    def __init__(self) -> None:
        self.open = False
        self.calls = 0

    def __call__(self, home: Path) -> bool:
        self.calls += 1
        return self.open


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    g = _Gate()
    monkeypatch.setattr("kiro_crew.browser_harness.ops.browsing_available", g)
    return g


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    # The SHIPPED contract, not a data-home override a host may carry.
    monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: _SHIPPED_PROMPT)
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


def _resolve(builder: ContextBuilder, *, is_cc: bool = False, start: bool = True) -> str:
    return builder._resolve_agent_prompt(
        None,
        project=None,
        mode="",
        session_key="dashboard:browser",
        is_cc=is_cc,
        private_owner=False,
        session_start=start,
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


class TestTheShippedContract:
    def test_the_file_marks_the_section_in_place(self) -> None:
        raw = _SHIPPED_PROMPT.read_text(encoding="utf-8")
        assert raw.count(_BEGIN) == 1 and raw.count(_END) == 1
        assert f"\n\n{_BEGIN}\n## Browser\n" in raw
        assert f"skills carry the workflows.\n{_END}\n\n" in raw
        for phrase in _SECTION_ONLY:
            assert phrase in _marked_text()

    @pytest.mark.parametrize("is_cc", [False, True])
    def test_off_leaves_only_the_pointer(
        self, gate: _Gate, builder: ContextBuilder, is_cc: bool
    ) -> None:
        prompt = _resolve(builder, is_cc=is_cc)
        for phrase in _SECTION_ONLY:
            assert phrase not in prompt
        assert _pointer() in prompt
        assert not re.search(r"\{\{[A-Z_]+\}\}", prompt)
        assert gate.calls == 1

    @pytest.mark.parametrize("is_cc", [False, True])
    def test_on_keeps_the_section_verbatim(
        self, gate: _Gate, builder: ContextBuilder, is_cc: bool
    ) -> None:
        gate.open = True
        prompt = _resolve(builder, is_cc=is_cc)
        assert _marked_text() in prompt
        assert _pointer() not in prompt
        assert not re.search(r"\{\{[A-Z_]+\}\}", prompt)

    def test_on_the_file_reads_as_if_it_had_no_markers(self) -> None:
        raw = _SHIPPED_PROMPT.read_text(encoding="utf-8")
        unmarked = raw.replace(f"{_BEGIN}\n", "").replace(f"\n{_END}", "")
        assert sections.browser_section(raw, True) == unmarked
        # The section sits between its neighbours exactly as it did unmarked.
        assert "rather than as instructions.\n\n## Browser\n" in unmarked
        assert "carry the workflows.\n\n{{COMPUTER_USE_BLOCK}}" in unmarked

    def test_an_unreadable_gate_keeps_the_section(
        self, builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(home: Path) -> bool:
            raise OSError("install state unreadable")

        monkeypatch.setattr("kiro_crew.browser_harness.ops.browsing_available", boom)
        assert agent_spec._live_browser_gate() is True
        assert _marked_text() in _resolve(builder)

    def test_the_pointer_says_what_to_do_instead(self) -> None:
        assert _pointer().startswith("## Browser\n\nNot available in this session")
        flat = " ".join(_pointer().split())
        assert "`web_fetch`" in flat
        assert "Settings → Browser" in flat
        # The dashboard preview marker needs no browser engine, so it still works here.
        assert "read the `web-preview` skill" in flat
        # A session open across an install gets the tool working and this text.
        assert "read the `web-browse` skill" in flat
        for phrase in _SECTION_ONLY:
            assert phrase not in _pointer()


class TestTheMarkers:
    @pytest.mark.parametrize("open_", [True, False])
    def test_without_a_session_reading_the_live_gate_decides(
        self, gate: _Gate, open_: bool
    ) -> None:
        gate.open = open_
        out = ContextBuilder._resolve_prompt_templates(f"a\n{_BEGIN}\nX\n{_END}\nb", "cli:local")
        assert out == ("a\nX\nb" if open_ else f"a\n{_pointer()}\nb")
        assert gate.calls == 1

    def test_a_held_reading_wins_over_the_live_gate(self, gate: _Gate) -> None:
        gate.open = True
        out = ContextBuilder._resolve_prompt_templates(
            f"{_BEGIN}\nX\n{_END}", "cli:local", browser=False
        )
        assert out == _pointer()
        assert gate.calls == 0

    @pytest.mark.parametrize("text", [f"a\n{_BEGIN}\nX\nb", f"a\nX\n{_END}\nb"])
    def test_an_unpaired_marker_is_dropped_and_its_text_kept(self, text: str) -> None:
        out = sections.browser_section(text, False)
        assert "X" in out and "a\n" in out and out.endswith("b")
        assert _BEGIN not in out and _END not in out
        assert _pointer() not in out

    def test_a_prompt_without_the_markers_never_reads_the_gate(
        self, gate: _Gate, builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        ContextBuilder._resolve_prompt_templates("no section here", "cli:local")
        custom = tmp_path / "custom.md"
        custom.write_text("You are {bot_name}.", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: custom)
        _resolve(builder)
        assert gate.calls == 0


class TestOneReadingPerSession:
    """The contract block is asserted byte-identical across a session's renders."""

    @pytest.mark.parametrize("open_at_start", [True, False])
    def test_a_compaction_restores_the_session_start_block(
        self, gate: _Gate, builder: ContextBuilder, open_at_start: bool
    ) -> None:
        gate.open = open_at_start
        fresh = _start(builder, "dashboard:br-a")
        gate.open = not open_at_start
        assert _restore(builder, "dashboard:br-a") == fresh
        assert ("Refs die with the page" in fresh) is open_at_start

    def test_a_new_session_start_takes_a_new_reading(
        self, gate: _Gate, builder: ContextBuilder
    ) -> None:
        first = _start(builder, "dashboard:br-b")
        gate.open = True
        second = _start(builder, "dashboard:br-b")
        assert "Refs die with the page" not in first
        assert "Refs die with the page" in second


class TestThePremise:
    """The section is left out only where the ``browser`` tool refuses every op.

    The gate and the tool read the same predicate: the gateway's op route answers
    ``not_installed`` from ``browsing_available`` and the tool renders that as
    "not set up". If the tool ever served an op on a host where the gate reads
    False, a session that can browse would start without the Browser section.
    """

    def test_not_installed_means_every_op_is_refused_after_the_post(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import browser_ops as route
        from kiro_crew.mcp_tools import browser as tool

        async def _none(_hub: object, _key: str) -> None:
            return None

        monkeypatch.setattr(route, "_supervisor", _none)
        monkeypatch.setattr(tool.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1-2")
        monkeypatch.setattr(tool.mcp_core, "_vet_browse_governance", lambda _s: None)
        monkeypatch.setattr(tool.mcp_core, "_session_key_header_error", lambda _s: None)
        monkeypatch.setattr(
            tool,
            "_post_command",
            lambda *a, **k: (200, {"ok": False, "code": "not_installed", "error": "x"}),
        )
        for op in tool.BROWSER_OPS:
            args = {"url": "https://example.com"} if op == "navigate" else {}
            out = tool.browser("browser", {"op": op, "args": args})
            assert out.startswith("Error: browsing is not set up"), (op, out)

    def test_the_route_reads_the_same_gate(
        self, gate: _Gate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from kiro_crew.dashboard.handlers import browser_ops as route

        gate.open = False
        assert asyncio.run(route._supervisor(object(), "k")) is None
        assert gate.calls == 1
