"""A claude session whose Crew tools a settings file withholds says why, and what to do.

The seed declines to deliver the ``mcpServers`` array when a folder's ``.claude``
settings file cannot be set aside: the installed claude-agent-acp is below the
``settingSources`` floor, the project file sets ``hooks`` or ``sandbox`` settings or
cannot be read, or the file is a link. These tests pin:

1. ``settings_withheld_reason`` answers one clause per refusal, or per cause when the
   refusal recorded none, ``""`` exactly when the tools are delivered, and the remedy
   only when asked (``with_fix``); the writer clears it on every run.
2. ``AcpProvider`` forwards it, keyword included.
3. The chat's seed-declined row carries the clause and the remedy.
4. A sub-agent run records the clause without the remedy and adds no log line; its own
   prompt, its completion event, its ``spawn_sub_agents`` record and
   ``GET /api/spawn/{id}`` all say so, and none tells a model to edit the project's
   settings. The ``spawn_status`` tool's line is pinned with its sibling notes in
   ``test_status_tools_carry_completion_warnings.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from acp_seed_provenance_helpers import (  # noqa: F401 -- autouse fixtures register by import
    _pinned_kiro_cli_version,
    isolated_records,
)
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from test_subagent_no_tool_calls_note import TestCompletionEventNote as _CompletionHarness
from test_subagent_no_tool_calls_note import TestSpawnStatusSaysWhetherToolsWereCalled as _Status
from test_subagent_no_tool_calls_note import TestSpawnSubAgentsResultNote as _WaveHarness

from kiro_crew import model_registry as mr
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_turn_control import CLAUDE_ACP_TESTED_VERSION, untested_version_issue
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subagent import CREW_OWN_TOOLS, CREW_TOOLS_WITHHELD_NOTE, SubagentInfo
from kiro_crew.subagent_manager.run import RunEventCoordinator

_SERVED = ["global.anthropic.claude-opus-5[1m]"]
_HOOKS = {"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command"}]}]}}


@pytest.fixture(autouse=True)
def _warm_model_cache(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": _SERVED})


def _project_file(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    path = tmp_path / ".claude" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return path


def _seeded(tmp_path: Path, *, adapter: str = "0.94.0", **kw: object) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)
    client._claude_adapter_disk_version = adapter
    client._write_claude_local_settings()
    return client


# ── part 1: the client records the reason ────────────────────────────────────


class TestClientRecordsTheReason:
    def test_project_file_with_hooks_names_the_file_and_the_fix(self, tmp_path: Path) -> None:
        _project_file(tmp_path, "settings.local.json", _HOOKS)
        client = _seeded(tmp_path)
        assert client.settings_withheld_cause() == "project"
        why = client.settings_withheld_reason()
        assert why.startswith("the folder's .claude/settings.local.json sets hooks or sandbox")
        assert "remove those settings" not in why
        assert client.settings_withheld_reason(with_fix=True) == (
            f"{why}. To use Crew's tools here, remove those settings from it, or work in "
            "another folder, then start a new session."
        )

    def test_checked_in_settings_json_with_sandbox_is_the_one_named(self, tmp_path: Path) -> None:
        _project_file(tmp_path, "settings.local.json", {"permissions": {"allow": ["Read"]}})
        _project_file(tmp_path, "settings.json", {"sandbox": {"enabled": True}})
        reason = _seeded(tmp_path).settings_withheld_reason()
        assert ".claude/settings.json sets hooks or sandbox settings" in reason

    def test_adapter_below_the_floor_names_the_tested_install_command(self, tmp_path: Path) -> None:
        _project_file(tmp_path, "settings.local.json", {"permissions": {"allow": ["Read"]}})
        client = _seeded(tmp_path, adapter="0.83.0")
        assert "too old" in client.settings_withheld_reason()
        assert "0.84.0 or newer" in client.settings_withheld_reason()
        fix = client.settings_withheld_reason(with_fix=True)
        # The command the tested-version notice prints, so a chat names one release.
        tested = untested_version_issue(ACP_BACKEND_CLAUDE, "0.83.0")
        command = f"npm i -g @agentclientprotocol/claude-agent-acp@{CLAUDE_ACP_TESTED_VERSION}"
        assert f"'{command}'" in fix and f"'{command}'" in tested
        assert "CLAUDE_AGENT_ACP_BIN" in fix

    def test_requested_mode_that_cannot_be_kept_names_it(self, tmp_path: Path) -> None:
        _project_file(tmp_path, "settings.local.json", {})
        client = _seeded(tmp_path, permission_mode="acceptEdits")
        why = client.settings_withheld_reason()
        assert "the acceptEdits permission mode, which Crew cannot keep" in why
        assert client.settings_withheld_reason(with_fix=True) == (
            f"{why}. Choose another permission mode, or work in another folder, "
            "then start a new session."
        )

    def test_a_linked_settings_file_says_replace_the_link(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.json"
        target.write_text("{}\n", encoding="utf-8")
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.local.json").symlink_to(target)
        client = _seeded(tmp_path)
        assert client.settings_withheld_cause() == "project"
        assert "is a link" in client.settings_withheld_reason()
        assert "Replace it with a regular file" in client.settings_withheld_reason(with_fix=True)

    def test_another_sessions_seed_is_named_as_such(self, tmp_path: Path) -> None:
        _seeded(tmp_path, permission_mode="default")
        sibling = _seeded(tmp_path, permission_mode="acceptEdits")
        assert sibling.settings_withheld_cause() == "session"
        assert sibling.settings_withheld_reason().startswith("another open Crew session")
        assert sibling.settings_withheld_reason(with_fix=True).endswith(
            "close that session, then start a new session here."
        )

    def test_a_project_decline_that_recorded_nothing_gets_the_general_clause(
        self, tmp_path: Path
    ) -> None:
        _project_file(tmp_path, "settings.local.json", _HOOKS)
        client = _seeded(tmp_path)
        client._claude_tools_withheld_reason = None
        assert client.settings_withheld_reason() == (
            "the folder's own .claude/settings.local.json controls tool approvals there, "
            "and Crew could not safely set it aside"
        )
        assert "work in another folder" in client.settings_withheld_reason(with_fix=True)

    def test_a_governed_session_has_no_reason(self, tmp_path: Path) -> None:
        client = _seeded(tmp_path)
        assert client._permission_surface_governed
        assert client.settings_withheld_reason(with_fix=True) == ""

    def test_a_project_file_crew_can_leave_out_has_no_reason(self, tmp_path: Path) -> None:
        _project_file(tmp_path, "settings.local.json", {"permissions": {"allow": ["Read"]}})
        client = _seeded(tmp_path)
        assert client._claude_local_settings_excluded
        assert client.settings_withheld_reason(with_fix=True) == ""

    def test_the_next_run_of_the_writer_clears_it(self, tmp_path: Path) -> None:
        path = _project_file(tmp_path, "settings.local.json", _HOOKS)
        client = _seeded(tmp_path)
        assert client.settings_withheld_reason()
        path.write_text("{}\n", encoding="utf-8")
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded
        assert client._claude_tools_withheld_reason is None
        assert client.settings_withheld_reason() == ""

    def test_a_backend_that_seeds_nothing_has_no_reason(self, tmp_path: Path) -> None:
        assert AcpClient(work_dir=tmp_path).settings_withheld_reason(with_fix=True) == ""


# ── part 2: the provider forwards it ─────────────────────────────────────────


class TestProviderForwards:
    def _read(self, client: Any, **kw: Any) -> str:
        return AcpProvider.settings_withheld_reason(SimpleNamespace(_client=client), **kw)  # type: ignore[arg-type]

    def test_forwards_the_client_answer_and_the_keyword(self) -> None:
        client = SimpleNamespace(settings_withheld_reason=lambda with_fix=False: f"why {with_fix}")
        assert self._read(client) == "why False"
        assert self._read(client, with_fix=True) == "why True"

    @pytest.mark.parametrize(
        "client",
        [SimpleNamespace(), SimpleNamespace(settings_withheld_reason=lambda with_fix=False: 1)],
    )
    def test_anything_else_is_empty(self, client: Any) -> None:
        assert self._read(client) == ""


# ── part 3: the chat's notice row ────────────────────────────────────────────


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **kw: seen.append(text),
    )
    return seen


def _notice(reason: Any) -> None:
    def read(*, with_fix: bool = False) -> Any:
        if reason is RuntimeError:
            raise RuntimeError("unreadable")
        return f"{reason}. Do this." if with_fix and isinstance(reason, str) and reason else reason

    client = SimpleNamespace(settings_withheld_reason=read)
    asyncio.run(chat_runner._notice_seed_declined(SimpleNamespace(), SimpleNamespace(), client))


class TestNoticeCarriesTheReason:
    def test_the_row_is_the_clause_and_the_remedy(self, notices: list[str]) -> None:
        _notice("the folder's .claude/settings.json sets hooks")
        assert notices == [
            "ℹ Crew's tools are off in this session because the folder's "
            ".claude/settings.json sets hooks. Do this."
        ]

    @pytest.mark.parametrize("reason", ["", None, 1, RuntimeError])
    def test_no_row_when_not_withheld_or_unreadable(self, notices: list[str], reason: Any) -> None:
        _notice(reason)
        assert notices == []

    def test_real_client_row_names_the_hooks_and_the_fix(
        self, tmp_path: Path, notices: list[str]
    ) -> None:
        _project_file(tmp_path, "settings.local.json", _HOOKS)
        client = _seeded(tmp_path)
        asyncio.run(chat_runner._notice_seed_declined(SimpleNamespace(), SimpleNamespace(), client))
        [text] = notices
        assert "sets hooks or sandbox settings" in text
        assert "remove those settings from it" in text
        assert "gateway log" not in text


# ── part 4: a sub-agent run says it on its result ────────────────────────────


class TestRunRecordsTheReason:
    def _read(self, client: Any) -> str:
        info = SubagentInfo(id="a1", task="t")
        runner = RunEventCoordinator.__new__(RunEventCoordinator)
        return asyncio.run(runner._crew_tools_withheld_reason(info, client))  # type: ignore[arg-type]

    def test_the_clause_is_kept_without_the_remedy(self) -> None:
        asked: list[bool] = []

        def read(*, with_fix: bool = False) -> str:
            asked.append(with_fix)
            return "the folder's file sets hooks"

        assert self._read(SimpleNamespace(settings_withheld_reason=read)) == (
            "the folder's file sets hooks"
        )
        assert asked == [False]

    @pytest.mark.parametrize("client", [None, SimpleNamespace(), MagicMock()])
    def test_nothing_without_a_str_answer(self, client: Any) -> None:
        if isinstance(client, MagicMock):
            client.settings_withheld_reason.return_value = MagicMock()
        assert self._read(client) == ""

    def test_a_failing_read_says_nothing(self) -> None:
        def boom() -> str:
            raise OSError("unreadable")

        assert self._read(SimpleNamespace(settings_withheld_reason=boom)) == ""

    def test_a_real_withheld_session_adds_no_log_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project_file(tmp_path, "settings.local.json", _HOOKS)
        provider = AcpProvider.__new__(AcpProvider)
        provider._client = _seeded(tmp_path)
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.subagent_manager.run"):
            reason = self._read(provider)
        assert reason.startswith("the folder's .claude/settings.local.json sets hooks")
        assert "remove those settings" not in reason
        assert [r for r in caplog.records if r.name == "kiro_crew.subagent_manager.run"] == []

    def test_the_note_carries_the_reason(self) -> None:
        info = SubagentInfo(id="a1", task="t")
        assert info.crew_tools_withheld_note == ""
        info.crew_tools_withheld = "x"
        assert info.crew_tools_withheld_note == CREW_TOOLS_WITHHELD_NOTE.format(reason="x")


class TestRunPromptSaysIt:
    def _runner(self) -> RunEventCoordinator:
        return RunEventCoordinator.__new__(RunEventCoordinator)

    def test_the_prompt_says_why_instead_of_not_configured(self) -> None:
        notice = self._runner()._spawn_mcp_notice("", _REASON)
        # The same words for Crew's tools as the parent's note (``CREW_OWN_TOOLS``).
        assert f"None of {CREW_OWN_TOOLS} are in this session, because {_REASON}." in notice
        assert CREW_OWN_TOOLS in CREW_TOOLS_WITHHELD_NOTE
        assert "not configured" not in notice and "UNTRUSTED" not in notice
        assert notice.endswith("\n\n")

    def test_a_withheld_run_skips_the_per_server_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        info = SubagentInfo(id="a1", task="t", crew_tools_withheld=_REASON)
        client = MagicMock()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.run"):
            assert self._runner()._warn_unusable_mcp_servers(info, client) == ""
        assert caplog.records == []
        client.mcp_session_report.assert_not_called()


_REASON = "the folder's .claude/settings.local.json sets hooks or sandbox settings"
_LINE = f"Warning: Agent `a1` {CREW_TOOLS_WITHHELD_NOTE.format(reason=_REASON)}"


class TestCompletionEventSaysIt:
    _harness = _CompletionHarness()

    def _withheld(self, **kw: Any) -> SubagentInfo:
        info = self._harness._info("a1", 0, **kw)
        info.crew_tools_withheld = _REASON
        return info

    @pytest.mark.asyncio
    async def test_a_lone_completed_run_carries_the_line(self) -> None:
        (event,) = await self._harness._deliver([self._withheld()])
        assert _LINE in event

    @pytest.mark.asyncio
    async def test_a_failed_run_carries_it_too(self) -> None:
        info = self._withheld()
        info.error = "boom"
        (event,) = await self._harness._deliver([info])
        assert _LINE in event

    @pytest.mark.asyncio
    async def test_a_run_that_had_the_tools_carries_none(self) -> None:
        (event,) = await self._harness._deliver([self._harness._info("a1", 2)])
        assert "ran without Crew's own tools" not in event

    @pytest.mark.asyncio
    async def test_a_wave_digest_flags_only_that_member(self) -> None:
        wave = {"batch_id": "w", "batch_total": 2}
        withheld = self._withheld(**wave)
        other = self._harness._info("m1", 2, **wave)
        (digest,) = await self._harness._deliver([withheld, other])
        assert digest.count("ran without Crew's own tools") == 1
        assert _LINE in digest


class TestStatusReadsSayIt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kw", [{}, {"error": "boom"}])
    async def test_spawn_status_endpoint_reports_it(self, kw: dict) -> None:
        status = _Status()
        info = status._done(crew_tools_withheld=_REASON, **kw)
        payload = await status._status(info)
        assert payload["crew_tools_withheld"] == info.crew_tools_withheld_note

    @pytest.mark.asyncio
    async def test_a_run_that_had_the_tools_reports_nothing(self) -> None:
        status = _Status()
        assert "crew_tools_withheld" not in await status._status(status._done())

    def test_spawn_sub_agents_record_carries_it(self) -> None:
        record = _WaveHarness()._collect({"crew_tools_withheld": "ran without them. Do X."})
        assert record["crew_tools_withheld"] == "This agent ran without them. Do X."

    @pytest.mark.parametrize("value", [None, "", True])
    def test_spawn_sub_agents_record_ignores_anything_else(self, value: Any) -> None:
        assert "crew_tools_withheld" not in _WaveHarness()._collect({"crew_tools_withheld": value})
