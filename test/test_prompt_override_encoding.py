"""A misencoded user prompt override degrades to the shipped prompt.

``~/.kiro/crew/prompt.md`` is a user-authored override that ``_prompt_path``
returns ahead of the shipped prompt whenever it exists. On Windows the default
PowerShell 5.1 redirection writes UTF-16LE with a BOM, so a user who creates the
override with ``... > $env:USERPROFILE\\.kiro\\crew\\prompt.md`` produces a file a
strict UTF-8 ``read_text`` cannot decode. ``UnicodeDecodeError`` is a
``ValueError``, not an ``OSError``, so the default-agent branch's
``except OSError`` let it escape ``_resolve_agent_prompt`` and fail every chat
turn.

These tests pin the contract for every signature the shared reader now covers:

* a UTF-16 (BOM) override -> shipped prompt, one WARNING naming the file;
* an unreadable override (a genuine ``OSError``) -> shipped prompt, same WARNING;
* a valid UTF-8 override -> still used verbatim (no behaviour change);
* the Claude-Code branch reads through the same reader;
* a custom agent whose spec carries the managed contract reads through it too.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew import context as ctx_mod
from kiro_crew.agent import _shipped_prompt
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_UTF16_OVERRIDE = b"\xff\xfe" + "You are helpful.".encode("utf-16-le")
_UTF8_OVERRIDE = "UTF8-OVERRIDE-MARKER: you are the override prompt.\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private data home so the override lives where ``_prompt_path`` looks."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@pytest.fixture
def builder(tmp_path: Path, opened, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    # The shipped prompt's ``{{MAX_SUBAGENTS}}`` figure is a live reading of
    # host free memory, and a session start takes a fresh one. These tests
    # compare a control render with an override render, so an unpinned figure
    # can move between the two on a busy xdist worker and fail a prompt that did
    # degrade. Pin it, as the composition contract does.
    monkeypatch.setattr(ContextBuilder, "_live_cap_figure", staticmethod(lambda: "4"))
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=opened(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)),
    )


def _resolve(builder: ContextBuilder, *, is_cc: bool = False) -> str:
    return builder._resolve_agent_prompt(
        None,
        project=None,
        mode="",
        session_key="sess-15491",
        is_cc=is_cc,
        private_owner=False,
        session_start=True,
    )


def _shipped_text() -> str:
    return _shipped_prompt().read_text(encoding="utf-8")


class TestUtf16OverrideDegradesToShippedPrompt:
    def test_utf16_override_does_not_raise_and_yields_shipped_prompt(
        self, home: Path, builder: ContextBuilder, caplog: pytest.LogCaptureFixture
    ) -> None:
        control = _resolve(builder)
        assert control, "control run without an override must produce the shipped prompt"

        override = home / "prompt.md"
        override.write_bytes(_UTF16_OVERRIDE)
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            resolved = _resolve(builder)

        assert resolved == control
        assert "You are helpful." not in resolved
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        message = warnings[0].getMessage()
        assert str(override) in message
        assert "utf-8" in message.lower()

    def test_unreadable_override_still_degrades_to_shipped_prompt(
        self,
        home: Path,
        builder: ContextBuilder,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A directory named like the override: read_text raises an OSError on
        # every platform (IsADirectoryError / PermissionError), which is the
        # existing guard's own case -- it must keep degrading, and now to the
        # shipped prompt rather than to an empty contract.
        control = _resolve(builder)
        assert control

        broken = home / "prompt-dir"
        broken.mkdir()
        monkeypatch.setattr(ctx_mod, "_prompt_path", lambda mode="": broken)
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            resolved = _resolve(builder)

        assert resolved == control
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(broken) in warnings[0].getMessage()

    def test_control_comparison_holds_while_the_host_cap_moves(
        self, home: Path, builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The auto-sized cap is read from host free memory on every session
        # start. Make it move between the two renders, as it does on a
        # loaded worker; the control comparison above must not depend on it.
        sizes = iter(range(15, 0, -1))
        monkeypatch.setattr("kiro_crew.subagent.compute_max_subagents", lambda cfg: next(sizes))
        monkeypatch.setattr("kiro_crew.resource_status.adaptive_exec_cap", lambda: 0)
        assert _resolve(builder) == _resolve(builder)

    def test_valid_utf8_override_is_still_used(
        self, home: Path, builder: ContextBuilder, caplog: pytest.LogCaptureFixture
    ) -> None:
        (home / "prompt.md").write_text(_UTF8_OVERRIDE, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            resolved = _resolve(builder)

        assert "UTF8-OVERRIDE-MARKER" in resolved
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_claude_code_branch_reads_through_the_same_reader(
        self, home: Path, builder: ContextBuilder, caplog: pytest.LogCaptureFixture
    ) -> None:
        control = _resolve(builder, is_cc=True)
        assert control

        (home / "prompt.md").write_bytes(_UTF16_OVERRIDE)
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            resolved = _resolve(builder, is_cc=True)

        assert resolved == control
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


class TestManagedContractAgent:
    """A fork or template copy carrying ``_NATIVE_PROMPT_STUB`` reads the same file."""

    @staticmethod
    def _write_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt: str) -> None:
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "fork.json").write_text(
            json.dumps({"name": "fork", "prompt": prompt}), encoding="utf-8"
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents_dir)

    def test_utf16_override_degrades_to_shipped_contract(
        self,
        home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.agent import _NATIVE_PROMPT_STUB

        shipped = tmp_path / "pkg" / "prompt.md"
        shipped.parent.mkdir()
        shipped.write_text("SHIPPED_CONTRACT", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.agent._BUNDLED_CFG_DIR", shipped.parent)
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: None)
        self._write_spec(tmp_path, monkeypatch, _NATIVE_PROMPT_STUB)

        assert ContextBuilder._load_agent_prompt("fork") == "SHIPPED_CONTRACT"

        override = home / "prompt.md"
        override.write_bytes(_UTF16_OVERRIDE)
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            loaded = ContextBuilder._load_agent_prompt("fork")

        assert loaded == "SHIPPED_CONTRACT"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(override) in warnings[0].getMessage()

    def test_own_file_prompt_keeps_the_bounded_reader(
        self,
        home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        own = tmp_path / "own-prompt.md"
        own.write_bytes(_UTF16_OVERRIDE)
        self._write_spec(tmp_path, monkeypatch, f"file://{own}")
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            assert ContextBuilder._load_agent_prompt("fork") == ""
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestReadPromptFile:
    """The reader itself, so a future caller sees the contract without a builder."""

    def test_returns_text_of_a_readable_file(self, tmp_path: Path) -> None:
        p = tmp_path / "prompt.md"
        p.write_text("plain\n", encoding="utf-8")
        assert ctx_mod._read_prompt_file(p) == "plain\n"

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"\xff\xfe" + "x".encode("utf-16-le"), id="utf-16-le-bom"),
            pytest.param(b"\xfe\xff" + "x".encode("utf-16-be"), id="utf-16-be-bom"),
            pytest.param(b"\x80\x81", id="invalid-utf-8"),
        ],
    )
    def test_misencoded_file_falls_back_to_shipped(
        self, tmp_path: Path, payload: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = tmp_path / "prompt.md"
        p.write_bytes(payload)
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            assert ctx_mod._read_prompt_file(p) == _shipped_text()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_symlink_to_a_sensitive_path_is_refused_and_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An in-sandbox agent may plant the override; a link into a credential
        # directory must not deliver those bytes as the agent prompt.
        fake_home = tmp_path / "home"
        secret_dir = fake_home / ".aws"
        secret_dir.mkdir(parents=True)
        secret = secret_dir / "credentials"
        secret.write_text("AKIA-SECRET\n", encoding="utf-8")
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        monkeypatch.setenv("HOME", str(fake_home))
        link = tmp_path / "prompt.md"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this host")
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            resolved = ctx_mod._read_prompt_file(link)
        assert resolved == _shipped_text()
        assert "AKIA-SECRET" not in resolved
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(link) in warnings[0].getMessage()

    def test_shipped_prompt_itself_unreadable_yields_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Terminal case: the fallback IS the file that failed. Returning "" is
        # the pre-existing contract for "no prompt"; no second read attempt.
        p = tmp_path / "prompt.md"
        p.write_bytes(_UTF16_OVERRIDE)
        monkeypatch.setattr(ctx_mod, "_shipped_prompt", lambda: p)
        with caplog.at_level(logging.WARNING, logger=ctx_mod.logger.name):
            assert ctx_mod._read_prompt_file(p) == ""
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
